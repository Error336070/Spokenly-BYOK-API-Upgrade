import json
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import app as app_module
from proxy.app import create_app
from proxy.backends import BackendError, NativeBackend, OpenRouterBackend
from proxy.storage import RequestLog
from scripts.stats import report
from tests.test_native import _parts
from tests.test_translate import cfg, make_wav  # noqa: F401


@pytest.mark.parametrize("provider,route_name", [("assemblyai", "assembly"), ("azure", "mai-clean")])
@pytest.mark.parametrize("request_language,override,expected", [
    ("de", None, "de"), (None, None, "fr"), ("fr", "en", "en"), ("fr", "en,de", None), ("fr", "", None)])
def test_native_verbose_response_preserves_resolved_language(cfg, provider, route_name, request_language, override, expected):
    cfg["language"] = "fr"
    cfg["notifications"]["enabled"] = False
    cfg["routes"][route_name]["backend"] = "native"
    field = "language_codes" if provider == "assemblyai" else "locales"
    if override is not None:
        cfg["routes"][route_name]["native"].setdefault("body", {})[field] = (
            override if provider == "assemblyai" else override.split(",") if override else [])
    seen = {}

    def handler(request):
        parts = _parts(request)
        payload = json.loads(parts["config" if provider == "assemblyai" else "definition"][1])
        seen["language"] = payload[field]
        data = {"text": "Zyntrabox"} if provider == "assemblyai" else {"combinedPhrases": [{"text": "Zyntrabox"}]}
        return httpx.Response(200, json=data)

    backend = NativeBackend(cfg, {"assemblyai": "synthetic", "azure": "synthetic", "azure_region": "Zyntrabox"},
                            transport=httpx.MockTransport(handler))
    fields = {"model": route_name, "response_format": "verbose_json"}
    if request_language is not None:
        fields["language"] = request_language
    with TestClient(create_app(cfg, backends={"native": backend}), base_url="http://127.0.0.1:18765") as client:
        response = client.post("/v1/audio/transcriptions", data=fields,
                               files={"file": ("synthetic.wav", make_wav(), "audio/wav")})
    assert response.status_code == 200
    upstream_language = override if override is not None else request_language or "fr"
    assert seen["language"] == (upstream_language if provider == "assemblyai" else upstream_language.split(",") if upstream_language else [])
    assert response.json()["language"] == expected


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
    assert writes[0]["total_latency_ms"] == 25.0


def test_stats_labels_legacy_total_as_pre_log_processing(tmp_path, capsys):
    with closing(RequestLog(str(tmp_path / "synthetic.sqlite"))) as db:
        db.insert({"ts": "2026-01-01T00:00:00", "route": "gpt", "backend": "openrouter",
                   "provider_model": "synthetic", "status": "ok", "total_latency_ms": 25})
        rows = db.rows_since()
    report(rows, {}, None, 0)
    output = capsys.readouterr().out
    assert "pre-log p50" in output and "pre-log p90" in output
    assert "excludes the SQLite write and response delivery" in output
    assert "total p50" not in output
