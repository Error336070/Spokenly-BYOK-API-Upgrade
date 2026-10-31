import json
import multiprocessing
import sys
import os
import time
import copy
import logging
from pathlib import Path

import httpx
import pytest

from proxy import failed
from proxy.storage import RequestLog
from proxy.usage import read_calibration
from proxy.retention import cleanup
from scripts import retry, stats
from tests.test_translate import cfg, make_wav  # noqa: F401


def test_retention_keeps_the_recovery_lock_file(tmp_path):
    path = failed.save(tmp_path, make_wav(), "wav", {"route": "gpt"})
    lock = tmp_path / failed.LOCK_FILENAME
    for entry in (path, failed.sidecar(path), lock):
        os.utime(entry, (1, 1))
    db = RequestLog(str(tmp_path / "Zyntrabox.sqlite"))
    try:
        cleanup({"logging": {"retention_days": 7, "clips_dir": str(tmp_path),
                             "failed_clips_dir": str(tmp_path)}}, db)
        assert lock.exists() and not path.exists() and not failed.sidecar(path).exists()
        # A retry after cleanup uses the same lock file, not a different lock inode.
        fresh = failed.save(tmp_path, make_wav(), "wav", {"route": "gpt"})
        assert failed.read_sidecar(fresh)["status"] == "pending"
    finally:
        db.close()


def test_hot_reload_reports_newly_unready_route(cfg, tmp_path, monkeypatch, caplog):
    import proxy.app as app_module
    from proxy.backends import NativeBackend, OpenRouterBackend
    from fastapi.testclient import TestClient

    cfg["notifications"]["enabled"] = False
    cfg["vocabulary_file"] = None
    changed = copy.deepcopy(cfg)
    changed["routes"]["assembly"]["backend"] = "openrouter"
    config_path = tmp_path / "Zyntrabox.yaml"
    config_path.write_text("first", encoding="utf-8")
    monkeypatch.setattr(app_module, "load_config", lambda path: changed)
    backends = {"openrouter": OpenRouterBackend(cfg, ""), "native": NativeBackend(cfg, {"assemblyai": "fake"})}
    with caplog.at_level(logging.WARNING, logger="proxy"):
        with TestClient(app_module.create_app(cfg, backends=backends, config_path=config_path,
                                             watch_interval_s=0.01), base_url="http://127.0.0.1:18765") as client:
            assert client.get("/health").json()["route_readiness"]["assembly"]["ready"] is True
            time.sleep(0.05)
            caplog.clear()
            os.utime(config_path, (time.time() + 2,) * 2)
            deadline = time.monotonic() + 3
            while cfg["routes"]["assembly"]["backend"] != "openrouter" and time.monotonic() < deadline:
                time.sleep(0.01)
            health = client.get("/health").json()
            assert health["status"] == "ok"
            assert health["route_readiness"]["assembly"]["ready"] is False
            assert "route assembly (backend openrouter) will fail" in caplog.text


def test_failed_clip_identity_includes_route_and_language(tmp_path):
    audio = make_wav()
    first = failed.save(tmp_path, audio, "wav", {"route": "gpt", "language": "en"})
    second = failed.save(tmp_path, audio, "wav", {"route": "assembly", "language": "en"})
    third = failed.save(tmp_path, audio, "wav", {"route": "gpt", "language": "de"})
    assert len({first, second, third}) == 3
    assert failed.read_sidecar(second)["route"] == "assembly"
    assert failed.read_sidecar(third)["language"] == "de"


def test_new_failure_reopens_a_recovered_clip(tmp_path):
    audio = make_wav()
    metadata = {"route": "gpt", "language": "en"}
    path = failed.save(tmp_path, audio, "wav", metadata)
    failed.update_sidecar(path, status="recovered")
    assert failed.save(tmp_path, audio, "wav", metadata) == path
    assert retry.pending(tmp_path) == [(path, failed.read_sidecar(path))]
    assert failed.read_sidecar(path)["status"] == "pending"


@pytest.mark.parametrize("value", [None, 3, [], {}])
def test_malformed_retry_text_is_a_readable_error(cfg, tmp_path, value):
    path = failed.save(tmp_path, make_wav(), "wav", {"route": "gpt"})
    with httpx.Client(base_url="http://127.0.0.1:18765", transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"text": value}))) as client:
        with pytest.raises(retry.RetryError, match="unreadable response"):
            retry.retry_clip(path, cfg, client)
    assert failed.read_sidecar(path)["status"] == "pending"


@pytest.mark.parametrize("changes", [{"retries": "bad"}, {"failures": -1}, {"status": []}, {"language": []}])
def test_malformed_sidecar_state_is_not_selected(tmp_path, changes):
    path = tmp_path / "Zyntrabox.wav"
    path.write_bytes(make_wav())
    failed.sidecar(path).write_text(json.dumps({"route": "gpt", **changes}), encoding="utf-8")
    assert failed.read_sidecar(path) is None


@pytest.mark.parametrize("balance", ["nan", "inf", "-inf"])
def test_calibration_rejects_nonfinite_input_before_opening_db(monkeypatch, balance):
    monkeypatch.setattr(stats, "load_config", lambda: pytest.fail("invalid input reached the database"))
    monkeypatch.setattr(sys, "argv", ["stats", f"--calibrate-balance={balance}"])
    with pytest.raises(SystemExit) as error:
        stats.main()
    assert error.value.code == 2


@pytest.mark.parametrize("value", [
    {"balance_usd": float("nan"), "tracked_seconds": 0, "at": "2026-01-01"},
    {"balance_usd": 10, "tracked_seconds": -1, "at": "2026-01-01"},
    {"balance_usd": 10, "tracked_seconds": float("inf"), "at": "2026-01-01"},
])
def test_invalid_saved_calibration_is_ignored(tmp_path, value):
    db = RequestLog(str(tmp_path / "Zyntrabox.sqlite"))
    try:
        db.set_meta("assemblyai_calibration", json.dumps(value))
        assert read_calibration(db) is None
    finally:
        db.close()


def _update_from_another_process(directory, gate):
    gate.wait(15)
    directory = Path(directory)
    for _ in range(10):
        path = failed.save(directory, make_wav(), "wav", {"route": "gpt", "language": "en"})
        failed.record_retry(path, "HTTP 502", started_generation=failed.read_sidecar(path)["generation"])


def test_proxy_and_retry_processes_preserve_all_updates(tmp_path):
    path = failed.save(tmp_path, make_wav(), "wav", {"route": "gpt", "language": "en"})
    context = multiprocessing.get_context("spawn")
    gate = context.Barrier(4)
    workers = [context.Process(target=_update_from_another_process, args=(str(tmp_path), gate)) for _ in range(4)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(25)
        assert [worker.exitcode for worker in workers] == [0] * 4
        assert failed.clips(tmp_path) == [path]
        metadata = failed.read_sidecar(path)
        assert metadata["failures"] == 41
        assert metadata["retries"] == 40
        assert metadata["status"] == "pending"
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
