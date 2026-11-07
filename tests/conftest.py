"""Run tests with temporary defaults, no personal credentials and mocked HTTP only."""
from pathlib import Path
import sqlite3

import httpx
import pytest
import yaml

from proxy import config
from scripts import shootout
from tests.helpers import SCRIPTS


_CONFIG = (Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8")
_CREDENTIALS = ("PROXY_API_KEY", "OPENROUTER_API_KEY", "ASSEMBLYAI_API_KEY", "AZURE_SPEECH_KEY",
                "AZURE_SPEECH_REGION", "AZURE_SPEECH_ENDPOINT", "PROXY_CONFIG")


@pytest.fixture(autouse=True)
def isolated_defaults(tmp_path, monkeypatch):
    installation = tmp_path / "test-installation"
    installation.mkdir()
    for name in _CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(installation)
    monkeypatch.setattr(config, "ROOT", installation)
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: False)
    (installation / "config.yaml").write_text(_CONFIG, encoding="utf-8")

    def no_http(*args, **kwargs):
        pytest.fail("unmocked HTTP request; use httpx.MockTransport")

    async def no_async_http(*args, **kwargs):
        no_http()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_http)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_async_http)


@pytest.fixture(autouse=True)
def closed_databases(monkeypatch):
    """Attribute leaked SQLite connections to their test, including on Python before 3.13."""
    connect = sqlite3.connect
    connections = []

    def tracked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    yield
    leaked = 0
    for connection in connections:
        try:
            connection.execute("SELECT 1")
        except sqlite3.ProgrammingError as exc:
            if "closed" not in str(exc):
                raise
        else:
            connection.close()
            leaked += 1
    assert leaked == 0, f"test left {leaked} SQLite connection(s) open"


@pytest.fixture
def cfg(tmp_path):
    c = yaml.safe_load(_CONFIG)
    c["credit"] = {"enabled": False}
    c["server"]["port"] = 18765
    c["logging"].update(db_path=str(tmp_path / "db.sqlite"), clips_dir=str(tmp_path / "clips"))
    c["logging"]["failed_clips_dir"] = str(tmp_path / "failed")
    # these tests inspect the JSON/WAV request shape; test_upload.py covers FLAC + multipart
    c["openrouter"]["multipart"] = False
    c["upload"] = {"flac": False}
    return c


@pytest.fixture
def shoot(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(shootout, "SHOOTOUT", tmp_path / "shootout")
    monkeypatch.setattr(shootout, "CLIPS", tmp_path / "shootout" / "clips")
    monkeypatch.setattr(shootout, "load_scripts", lambda: SCRIPTS)
    monkeypatch.setattr(shootout, "load_config", lambda: cfg)
    cfg["credit"] = {"enabled": False, "assemblyai_hourly_rate_usd": 0.36}
    return cfg
