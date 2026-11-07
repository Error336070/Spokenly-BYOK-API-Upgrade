import os
import time
import httpx
from proxy import failed
from proxy.storage import RequestLog
from proxy.retention import cleanup
from tests.helpers import make_wav
import sqlite3
from contextlib import closing
from fastapi.testclient import TestClient
from proxy.app import create_app
from proxy.backends import OpenRouterBackend
import asyncio
import threading
"""test_storage"""


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


def test_retention_deletes_old_clips_and_rows(tmp_path):
    clips = tmp_path / "clips"
    clips.mkdir()
    old, new = clips / "old.wav", clips / "new.wav"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    os.utime(old, (time.time() - 8 * 86400,) * 2)
    with closing(RequestLog(str(tmp_path / "db.sqlite"))) as db:
        db.insert({"ts": "2000-01-01T00:00:00.000"})
        db.insert({"ts": "2999-01-01T00:00:00.000"})
        cleanup({"logging": {"retention_days": 7, "clips_dir": str(clips)}}, db)
        assert not old.exists() and new.exists()
        assert db._conn.execute("SELECT ts FROM requests").fetchall() == [("2999-01-01T00:00:00.000",)]


def test_log_failure_preserves_transcript_off_loop(cfg, monkeypatch):
    threads = []

    def fail(self, row):
        threads.append(threading.current_thread().name)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise AssertionError("database write on event loop")
        raise sqlite3.OperationalError("Zyntrabox locked")

    monkeypatch.setattr(RequestLog, "insert", fail)
    backend = OpenRouterBackend(cfg, "k", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"text": "Zyntrabox"})))
    with TestClient(create_app(cfg, backends={"openrouter": backend}), base_url="http://127.0.0.1:18765") as c:
        response = c.post("/v1/audio/transcriptions", files={"file": ("a.wav", make_wav())})
        assert response.json() == {"text": "Zyntrabox"}
    assert threads


def test_database_uses_wal(tmp_path):
    with closing(RequestLog(str(tmp_path / "log.sqlite"))) as db:
        assert db._conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert db._conn.execute("PRAGMA synchronous").fetchone() == (1,)


def test_retention_truncates_the_write_ahead_log(tmp_path):
    from pathlib import Path

    from proxy.storage import RequestLog
    db = RequestLog(str(tmp_path / "Zyntrabox.sqlite"))
    try:
        for i in range(200):
            db.insert({"ts": f"2026-01-01T00:00:{i % 60:02}", "transcript": "Zyntrabox " * 50})
        wal = Path(str(tmp_path / "Zyntrabox.sqlite") + "-wal")
        assert wal.stat().st_size > 0
        assert db.delete_before("2027-01-01") == 200
        assert wal.stat().st_size == 0
        indexes = {r[1] for r in db._conn.execute("PRAGMA index_list(requests)")}
        assert "requests_ts" in indexes
    finally:
        db.close()
