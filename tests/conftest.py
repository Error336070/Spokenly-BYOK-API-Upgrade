"""Run tests with temporary defaults, no personal credentials and mocked HTTP only."""
from pathlib import Path

import httpx
import pytest

from proxy import config


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
