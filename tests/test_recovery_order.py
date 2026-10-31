"""A retry result applies only to the failure generation it started from."""
import multiprocessing
import threading
from pathlib import Path

import httpx
import pytest

from proxy import failed
from scripts import retry
from tests.test_translate import cfg, make_wav  # noqa: F401

METADATA = {"route": "gpt", "language": "en"}


def save(directory, audio=None):
    return failed.save(directory, audio or make_wav(), "wav", dict(METADATA))


def client(handler):
    return httpx.Client(base_url="http://127.0.0.1:18765", transport=httpx.MockTransport(handler))


def test_each_failure_starts_a_new_generation(tmp_path):
    path = save(tmp_path)
    assert failed.read_sidecar(path)["generation"] == 1
    save(tmp_path)
    assert failed.read_sidecar(path)["generation"] == 2
    failed.record_retry(path, None, started_generation=2)
    assert failed.read_sidecar(path)["generation"] == 2


def test_newer_failure_during_successful_retry_stays_pending(cfg, tmp_path, capsys):  # noqa: F811
    cfg["logging"]["failed_clips_dir"] = str(tmp_path)
    path = save(tmp_path)

    def handler(request):
        # The successful response exists, but before the retry records it the proxy fails on the same audio again.
        assert save(tmp_path) == path
        return httpx.Response(200, json={"text": "Zyntrabox"})

    with client(handler) as c:
        assert retry.retry_clip(path, cfg, c) == "Zyntrabox"
    metadata = failed.read_sidecar(path)
    assert metadata["status"] == "pending" and metadata["failures"] == 2 and metadata["retries"] == 1
    assert "recovered_at" not in metadata and "last_retry_error" not in metadata
    assert retry.pending(tmp_path) == [(path, metadata)]
    assert "newer failure" in capsys.readouterr().err

    # A retry that starts after that failure recovers the clip.
    with client(lambda r: httpx.Response(200, json={"text": "Zyntrabox"})) as c:
        assert retry.retry_clip(path, cfg, c) == "Zyntrabox"
    metadata = failed.read_sidecar(path)
    assert metadata["status"] == "recovered" and metadata["failures"] == 2 and metadata["retries"] == 2
    assert retry.pending(tmp_path) == []


def test_failure_saved_while_retry_reads_its_response_stays_pending(cfg, tmp_path, monkeypatch):  # noqa: F811
    path = save(tmp_path)
    reading, release = threading.Event(), threading.Event()
    original_json = httpx.Response.json

    def paused_json(self, **kwargs):
        reading.set()
        assert release.wait(10)
        return original_json(self, **kwargs)

    monkeypatch.setattr(httpx.Response, "json", paused_json)
    result = {}

    def run_retry():
        with client(lambda r: httpx.Response(200, json={"text": "Zyntrabox"})) as c:
            result["text"] = retry.retry_clip(path, cfg, c)

    worker = threading.Thread(target=run_retry)
    worker.start()
    assert reading.wait(10)
    save(tmp_path)
    release.set()
    worker.join(10)
    assert result["text"] == "Zyntrabox"
    metadata = failed.read_sidecar(path)
    assert metadata["status"] == "pending" and metadata["failures"] == 2
    assert retry.pending(tmp_path) == [(path, metadata)]


def test_stale_retry_error_is_not_recorded_on_a_newer_failure(cfg, tmp_path):  # noqa: F811
    path = save(tmp_path)

    def handler(request):
        save(tmp_path)
        return httpx.Response(502, json={"error": {"message": "Zyntrabox down"}})

    with client(handler) as c:
        with pytest.raises(retry.RetryError, match="HTTP 502"):
            retry.retry_clip(path, cfg, c)
    metadata = failed.read_sidecar(path)
    assert metadata["status"] == "pending" and metadata["retries"] == 1 and metadata["failures"] == 2
    assert "last_retry_error" not in metadata


def test_older_failed_retry_does_not_reopen_a_recovered_generation(tmp_path):
    path = save(tmp_path)
    generation = failed.read_sidecar(path)["generation"]
    failed.record_retry(path, None, started_generation=generation)  # the faster retry recovers
    metadata = failed.record_retry(path, "HTTP 502", started_generation=generation)  # the slower one fails
    assert metadata["status"] == "recovered" and metadata["retries"] == 2
    assert "last_retry_error" not in metadata and "recovered_at" in metadata


def test_failed_retry_on_current_generation_clears_nothing_stale(tmp_path):
    path = save(tmp_path)
    metadata = failed.record_retry(path, "HTTP 502", started_generation=1)
    assert metadata["status"] == "pending" and metadata["last_retry_error"] == "HTTP 502"
    metadata = failed.record_retry(path, None, started_generation=1)
    assert metadata["status"] == "recovered" and "last_retry_error" not in metadata


def test_sidecar_without_generation_counts_as_zero(tmp_path):
    path = save(tmp_path)
    metadata = failed.read_sidecar(path)
    del metadata["generation"]
    failed._write_sidecar(path, metadata)
    assert failed.read_sidecar(path).get("generation", 0) == 0
    assert failed.record_retry(path, None, started_generation=0)["status"] == "recovered"
    save(tmp_path)
    assert failed.read_sidecar(path)["generation"] == 1


@pytest.mark.parametrize("value", ["2", -1, True, 1.5])
def test_malformed_generation_is_unreadable(tmp_path, value):
    path = save(tmp_path)
    failed.update_sidecar(path, generation=value)
    assert failed.read_sidecar(path) is None


def _fail_again(directory):
    save(Path(directory))


def _recover(directory, generation):
    [path] = failed.clips(Path(directory))
    failed.record_retry(path, None, started_generation=generation)


def test_newer_failure_from_another_process_blocks_stale_recovery(tmp_path):
    path = save(tmp_path)
    started = failed.read_sidecar(path)["generation"]
    context = multiprocessing.get_context("spawn")
    worker = context.Process(target=_fail_again, args=(str(tmp_path),))
    worker.start()
    worker.join(25)
    assert worker.exitcode == 0
    metadata = failed.record_retry(path, None, started_generation=started)
    assert metadata["status"] == "pending" and metadata["failures"] == 2

    worker = context.Process(target=_recover, args=(str(tmp_path), metadata["generation"]))
    worker.start()
    worker.join(25)
    assert worker.exitcode == 0
    assert failed.read_sidecar(path)["status"] == "recovered"
