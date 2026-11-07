import sqlite3
import time
from types import SimpleNamespace
import httpx
import pytest
from fastapi.testclient import TestClient
from proxy import app as app_module
from proxy.app import create_app
from proxy.backends import BackendError, NativeBackend, OpenRouterBackend
from proxy.storage import RequestLog
from tests.helpers import make_wav
import asyncio
import importlib
import sys
"""test_app"""


@pytest.mark.parametrize("provider_language,request_language,expected", [("de", "fr", "de"), (None, None, None)])
def test_verbose_language_prefers_provider_and_keeps_unknown_null(cfg, provider_language, request_language, expected):
    cfg["language"] = ""
    cfg["notifications"]["enabled"] = False
    payload = {"text": "Zyntrabox", "language": provider_language}
    backend = OpenRouterBackend(cfg, "synthetic", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    fields = {"model": "gpt", "response_format": "verbose_json"}
    if request_language is not None:
        fields["language"] = request_language
    with TestClient(create_app(cfg, backends={"openrouter": backend}), base_url="http://127.0.0.1:18765") as client:
        response = client.post("/v1/audio/transcriptions", data=fields,
                               files={"file": ("synthetic.wav", make_wav(), "audio/wav")})
    assert response.status_code == 200
    assert response.json()["language"] == expected


@pytest.mark.parametrize("provider_status", [200, 502])
@pytest.mark.parametrize("log_fails", [False, True])
def test_server_timing_includes_waited_logging_and_preserves_result(cfg, monkeypatch, provider_status, log_fails):
    cfg["notifications"]["enabled"] = False
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(app_module, "time", SimpleNamespace(perf_counter=lambda: clock.now, monotonic=time.monotonic))
    writes = []
    original_insert = RequestLog.insert

    def slow_insert(db, row):
        writes.append(row.copy())
        clock.now += 0.3
        if log_fails:
            raise sqlite3.OperationalError("synthetic write failure")
        return original_insert(db, row)

    monkeypatch.setattr(RequestLog, "insert", slow_insert)

    class Backend:
        async def transcribe(self, *args, **kwargs):
            clock.now += 0.025
            if provider_status != 200:
                raise BackendError("synthetic upstream failure", provider_status)
            return {"text": "Zyntrabox", "provider_model": "synthetic", "provider_latency_ms": 25}

        async def aclose(self):
            pass

    with TestClient(create_app(cfg, backends={"openrouter": Backend()}), base_url="http://127.0.0.1:18765") as client:
        response = client.post("/v1/audio/transcriptions", data={"model": "gpt"},
                               files={"file": ("synthetic.wav", make_wav(), "audio/wav")})
    assert response.status_code == provider_status
    if provider_status == 200:
        assert response.json() == {"text": "Zyntrabox"}
    else:
        assert response.json()["error"]["message"] == "synthetic upstream failure"
    assert response.headers["Server-Timing"] == "db;dur=300.0, handler;dur=325.0"
    assert len(writes) == 1
    assert writes[0]["processing_ms"] == 25.0


@pytest.mark.parametrize("headers", [
    {"origin": "https://example.invalid"}, {"origin": "null"},
    {"host": "example.invalid:18765"}, {"host": "localhost:9"}, {"host": "localhost"},
])
def test_reject_browser_and_rebinding_requests(cfg, headers):
    cfg["server"]["port"] = 18765
    with TestClient(create_app(cfg, backends={}), base_url="http://127.0.0.1:18765") as c:
        response = c.get("/v1/models", headers=headers)
        assert response.status_code == 403
        assert response.json()["error"]["type"] == "invalid_request_error"


def setup(cfg, provider, data):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=data))
    if provider == "openrouter":
        return OpenRouterBackend(cfg, "k", transport), cfg["routes"]["gpt"]
    return NativeBackend(cfg, {"assemblyai": "k", "azure": "k", "azure_region": "Zyntrabox"}, transport), {
        "native": {"provider": provider, "body": {}}}


@pytest.mark.parametrize("provider", ["openrouter", "assemblyai", "azure"])
@pytest.mark.parametrize("data", [{}, {"error": {"message": "Zyntrabox"}}, {"text": None}])
def test_missing_transcript_is_failure(cfg, provider, data):
    async def run():
        backend, route = setup(cfg, provider, data)
        try:
            with pytest.raises(BackendError) as exc:
                await backend.transcribe(b"audio", "wav", route, "en")
            assert exc.value.status == 502
        finally:
            await backend.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openrouter", "assemblyai", "azure"])
def test_explicit_silence_is_valid(cfg, provider):
    async def run():
        data = {"combinedPhrases": []} if provider == "azure" else {"text": ""}
        backend, route = setup(cfg, provider, data)
        try:
            assert (await backend.transcribe(b"audio", "wav", route, "en"))["text"] == ""
        finally:
            await backend.aclose()
    asyncio.run(run())


def test_bad_startup_config_is_logged(tmp_path, monkeypatch):
    startup = importlib.import_module("proxy.__main__")
    monkeypatch.setattr(startup, "ROOT", tmp_path)
    path = tmp_path / "Zyntrabox.yaml"
    path.write_text("logging: {}")
    monkeypatch.setattr(sys, "argv", ["proxy", "--config", str(path)])
    with pytest.raises(ValueError):
        startup.main()
    assert "fatal startup error" in (tmp_path / "data" / "proxy.log").read_text()
