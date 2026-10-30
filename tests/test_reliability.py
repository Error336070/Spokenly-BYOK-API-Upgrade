import asyncio
import sqlite3
import threading

import httpx
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.backends import OpenRouterBackend
from proxy.storage import RequestLog
from tests.test_translate import cfg, make_wav  # noqa: F401


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
    db = RequestLog(str(tmp_path / "log.sqlite"))
    assert db._conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert db._conn.execute("PRAGMA synchronous").fetchone() == (1,)
