import json
import sqlite3
import sys
import time

import pytest
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.notify import Notifier
from proxy.retention import cleanup
from proxy.storage import RequestLog
from proxy.usage import assemblyai_credit, credit_estimate
from scripts import stats
from tests.test_translate import cfg  # noqa: F401

CREDIT = {"credit": {"assemblyai_hourly_rate_usd": 0.45, "assemblyai_starting_credit_usd": 50}}
OLD, NEW = "2000-01-01T00:00:00.000", "2999-01-01T00:00:00.000"


def native_row(ts, seconds=3600, status="ok", model="assemblyai/Zyntrabox"):
    return {"ts": ts, "route": "assembly", "backend": "native", "provider_model": model, "status": status,
            "audio_duration_s": seconds, "transcript": "Zyntrabox"}


def remaining(db, settings=CREDIT):
    return credit_estimate(db, settings)["remaining_usd"]


def test_pruning_keeps_lifetime_usage_exactly_once(tmp_path):
    db = RequestLog(str(tmp_path / "Zyntrabox.sqlite"))
    db.insert(native_row(OLD))
    db.insert(native_row(NEW, seconds=None))
    assert remaining(db) == pytest.approx(49.55)
    for _ in range(3):  # repeated pruning moves the old row once and finds nothing after
        db.delete_before("2500-01-01")
        assert remaining(db) == pytest.approx(49.55)
    assert db._conn.execute("SELECT COUNT(*) FROM requests").fetchone() == (1,)
    # the rollup holds counts only, never transcripts
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(usage_rollup)")}
    assert "transcript" not in cols and "clip_path" not in cols
    estimate = credit_estimate(db, CREDIT)
    assert estimate["requests"] == 2 and estimate["missing_duration"] == 1
    db.close()


def test_retention_cleanup_rolls_up_and_survives_reopen(tmp_path):
    path = str(tmp_path / "Zyntrabox.sqlite")
    db = RequestLog(path)
    db.insert(native_row(OLD))
    settings = {**CREDIT, "logging": {"retention_days": 7, "clips_dir": str(tmp_path / "clips"),
                                      "failed_clips_dir": str(tmp_path / "failed")}}
    cleanup(settings, db)
    db.close()
    db = RequestLog(path)  # restart: totals persisted, migration does not double count
    assert db._conn.execute("SELECT COUNT(*) FROM requests").fetchone() == (0,)
    assert remaining(db) == pytest.approx(49.55)
    db.close()


def test_retention_zero_keeps_rows_and_counts_them(tmp_path):
    db = RequestLog(str(tmp_path / "Zyntrabox.sqlite"))
    db.insert(native_row(OLD))
    cleanup({"logging": {"retention_days": 0, "clips_dir": str(tmp_path)}}, db)
    assert db._conn.execute("SELECT COUNT(*) FROM requests").fetchone() == (1,)
    assert db._conn.execute("SELECT COUNT(*) FROM usage_rollup").fetchone() == (0,)
    assert remaining(db) == pytest.approx(49.55)
    db.close()


def test_failed_prune_rolls_back_both_steps(tmp_path):
    db = RequestLog(str(tmp_path / "Zyntrabox.sqlite"))
    db.insert(native_row(OLD))
    db._conn.execute("CREATE TRIGGER block BEFORE DELETE ON requests BEGIN SELECT RAISE(ABORT, 'Zyntrabox'); END")
    with pytest.raises(sqlite3.IntegrityError):
        db.delete_before("2500-01-01")
    assert db._conn.execute("SELECT COUNT(*) FROM usage_rollup").fetchone() == (0,)
    assert remaining(db) == pytest.approx(49.55)
    db._conn.execute("DROP TRIGGER block")
    assert db.delete_before("2500-01-01") == 1
    assert remaining(db) == pytest.approx(49.55)
    db.close()


def test_upgrade_from_old_database_does_not_double_count(tmp_path):
    path = str(tmp_path / "Zyntrabox.sqlite")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, backend TEXT, "
                "provider_model TEXT, status TEXT, audio_duration_s REAL)")
    old.execute("INSERT INTO requests (ts, backend, provider_model, status, audio_duration_s) "
                "VALUES ('2026-01-02T00:00:00', 'native', 'assemblyai/Zyntrabox', 'ok', 3600)")
    old.commit()
    old.close()
    for _ in range(2):
        db = RequestLog(path)
        assert remaining(db) == pytest.approx(49.55)
        # history starts at the oldest row present at upgrade; earlier deleted rows are not implied
        assert db.get_meta("usage_history_from") == "2026-01-02T00:00:00"
        db.close()


def test_failed_and_other_provider_rows_are_reported_not_counted(tmp_path):
    db = RequestLog(str(tmp_path / "Zyntrabox.sqlite"))
    db.insert(native_row(NEW, status="error"))
    db.insert(native_row(NEW, model="azure/Zyntrabox"))
    db.insert({**native_row(NEW), "backend": "openrouter"})
    estimate = credit_estimate(db, CREDIT)
    assert estimate["remaining_usd"] == 50 and estimate["failed"] == 1 and estimate["requests"] == 0
    db.close()


def test_unknown_rate_or_start(tmp_path):
    db = RequestLog(str(tmp_path / "Zyntrabox.sqlite"))
    db.insert(native_row(NEW))
    assert credit_estimate(db, {"credit": {}})["used_usd"] is None
    estimate = credit_estimate(db, {"credit": {"assemblyai_hourly_rate_usd": 0.45}})
    assert estimate["used_usd"] == pytest.approx(0.45) and estimate["remaining_usd"] is None
    db.close()


def test_calibration_replaces_starting_credit(tmp_path):
    db = RequestLog(str(tmp_path / "Zyntrabox.sqlite"))
    db.insert(native_row(OLD))
    stats.calibrate(db, CREDIT, 20.0)
    assert remaining(db) == pytest.approx(20.0)  # usage before the anchor is already in the balance
    db.insert(native_row(NEW, seconds=1800))
    assert remaining(db) == pytest.approx(19.775)
    db.delete_before("2500-01-01")  # pruning the pre-anchor row changes nothing
    assert remaining(db) == pytest.approx(19.775)
    db.set_meta("assemblyai_calibration", None)
    assert remaining(db) == pytest.approx(50 - 0.675)
    db.set_meta("assemblyai_calibration", "not json")  # unreadable: ignored, not a crash
    assert remaining(db) == pytest.approx(50 - 0.675)
    db.close()


def run_stats(monkeypatch, capsys, path, settings, *argv):
    monkeypatch.setattr(stats, "load_config", lambda: {"logging": {"db_path": path}, **settings})
    monkeypatch.setattr(sys, "argv", ["stats", *argv])
    stats.main()
    return capsys.readouterr().out


def test_since_filter_and_empty_window_keep_cumulative_estimate(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "Zyntrabox.sqlite")
    db = RequestLog(path)
    db.insert(native_row("2026-01-01T00:00:00"))
    db.close()
    full = run_stats(monkeypatch, capsys, path, CREDIT)
    empty = run_stats(monkeypatch, capsys, path, CREDIT, "--since", "2027")
    assert "no requests in the selected period" in empty
    for out in (full, empty):
        assert "estimated remaining $49.5500" in out and "not only the selected period" in out
    assert "native: 60.00 audio minutes" in full and "native: 60.00" not in empty


def test_stats_calibration_flags(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "Zyntrabox.sqlite")
    out = run_stats(monkeypatch, capsys, path, CREDIT, "--calibrate-balance", "12.5")
    assert "calibrated: $12.50" in out and "estimated remaining $12.5000 (from dashboard balance $12.50" in out
    out = run_stats(monkeypatch, capsys, path, CREDIT, "--clear-calibration")
    assert "calibration cleared" in out and "estimated remaining $50.0000 (from starting credit" in out


def test_low_assemblyai_estimate_notifies(cfg):  # noqa: F811
    cfg["credit"] = {"enabled": True, "interval_s": 0.02, "low_usd": 1,
                     "assemblyai_hourly_rate_usd": 0.45, "assemblyai_starting_credit_usd": 1.2}
    db = RequestLog(cfg["logging"]["db_path"])
    db.insert(native_row(NEW))
    db.close()
    sent = []
    with TestClient(create_app(cfg, backends={}, notifier=Notifier(cfg, send=lambda t, b: sent.append((t, b)))),
                    base_url="http://127.0.0.1:18765"):
        deadline = time.monotonic() + 2
        while not sent and time.monotonic() < deadline:
            time.sleep(0.01)
    assert len(sent) == 1 and "Estimated AssemblyAI credit is low" in sent[0][0] and "$0.75" in sent[0][1]


def test_no_assemblyai_estimate_warning_when_route_is_on_openrouter(cfg):  # noqa: F811
    cfg["routes"]["assembly"]["backend"] = "openrouter"
    cfg["credit"] = {"enabled": True, "interval_s": 0.02, "low_usd": 1,
                     "assemblyai_hourly_rate_usd": 0.45, "assemblyai_starting_credit_usd": 0}
    sent = []
    with TestClient(create_app(cfg, backends={}, notifier=Notifier(cfg, send=lambda t, b: sent.append(t))),
                    base_url="http://127.0.0.1:18765"):
        time.sleep(0.1)
    assert sent == []


def test_credit_labels_have_separate_cooldowns():
    sent = []
    n = Notifier({"notifications": {"cooldown_s": 600}}, send=lambda t, b: sent.append(t))
    n.low_credit(0.5)
    n.low_assemblyai_credit(0.5)
    n.low_assemblyai_credit(0.4)
    assert sent == ["Dictation Proxy: OpenRouter key credit is low",
                    "Dictation Proxy: Estimated AssemblyAI credit is low"]


def test_assemblyai_credit_pure():
    totals = [{"backend": "native", "provider_model": "assemblyai/x", "status": "ok",
               "requests": 2, "audio_seconds": 7200, "missing_duration": 0}]
    out = assemblyai_credit(totals, CREDIT, {"balance_usd": 10, "tracked_seconds": 3600, "at": "t"})
    assert out["remaining_usd"] == pytest.approx(9.55) and out["used_usd"] == pytest.approx(0.9)
    assert json.dumps(out)
