import asyncio
import os
import sqlite3
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.backends import OpenRouterBackend
from proxy.storage import RequestLog
from tests.test_translate import cfg, make_wav  # noqa: F401


def _files(path):
    return [path, path + "-wal", path + "-shm"]


def test_repeated_open_close_releases_file(tmp_path):
    path = str(tmp_path / "Zyntrabox.sqlite")
    for _ in range(3):
        db = RequestLog(path)
        db.insert({"ts": "2026-01-01T00:00:00"})
        db.close()
        db.close()  # a second close is harmless
        with pytest.raises(sqlite3.ProgrammingError):
            db.insert({"ts": "2026-01-01T00:00:01"})
    for f in _files(path):
        if os.path.exists(f):
            os.remove(f)  # fails on Windows while a handle is open
    assert not os.path.exists(path)


def test_lifespan_exit_closes_database(cfg):
    backend = OpenRouterBackend(cfg, "k", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"text": "Zyntrabox"})))
    with TestClient(create_app(cfg, backends={"openrouter": backend}), base_url="http://127.0.0.1:18765") as c:
        c.post("/v1/audio/transcriptions", files={"file": ("a.wav", make_wav())})
        db = c.app.state.db
    with pytest.raises(sqlite3.ProgrammingError):
        db.insert({"ts": "2026-01-01T00:00:00"})
    for f in _files(cfg["logging"]["db_path"]):
        if os.path.exists(f):
            os.remove(f)


def test_close_waits_for_running_call(tmp_path):
    db = RequestLog(str(tmp_path / "Zyntrabox.sqlite"))
    started, release, order = threading.Event(), threading.Event(), []
    original = db._conn

    class Slow:
        def execute(self, *a):
            started.set()
            release.wait(2)
            order.append("insert")
            return original.execute(*a)

        def __getattr__(self, name):
            return getattr(original, name)

    db._conn = Slow()
    worker = threading.Thread(target=db.insert, args=({"ts": "2026-01-01T00:00:00"},))
    worker.start()
    started.wait(2)
    closer = threading.Thread(target=lambda: (db.close(), order.append("close")))
    closer.start()
    time.sleep(0.05)
    assert order == []  # close is waiting for the insert
    release.set()
    worker.join(2)
    closer.join(2)
    assert order == ["insert", "close"]
    original.close()


def test_shutdown_waits_for_cancelled_db_thread(cfg, monkeypatch):
    """A background DB call still running in its thread at shutdown finishes before close()."""
    import proxy.app as app_module
    started, order = threading.Event(), []

    def slow_cleanup(settings, db):
        started.set()
        time.sleep(0.2)
        db.insert({"ts": "2026-01-01T00:00:00"})
        order.append("cleanup")

    monkeypatch.setattr(app_module, "cleanup", slow_cleanup)
    original_close = RequestLog.close
    monkeypatch.setattr(RequestLog, "close", lambda self: (order.append("close"), original_close(self)))
    with TestClient(create_app(cfg, backends={}), base_url="http://127.0.0.1:18765"):
        assert started.wait(2)
    assert order == ["cleanup", "close"]
