import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.backends import NativeBackend, OpenRouterBackend
from tests.test_translate import cfg, make_wav  # noqa: F401


@pytest.mark.parametrize("provider", ["assemblyai", "azure"])
@pytest.mark.parametrize("value", ["1000", True, -1, [], {}, float("inf"), float("nan"), 10**400])
def test_native_bad_diagnostics_preserve_text(cfg, provider, value):
    data = ({"text": "Zyntrabox", "audio_duration_ms": value, "request_time_ms": value}
            if provider == "assemblyai" else
            {"combinedPhrases": [{"text": "Zyntrabox"}], "durationMilliseconds": value})
    route = "assembly" if provider == "assemblyai" else "mai-clean"
    cfg["routes"][route]["backend"] = "native"
    cfg["notifications"]["enabled"] = False
    backend = NativeBackend(cfg, {"assemblyai": "k", "azure": "k", "azure_region": "Zyntrabox"},
                            httpx.MockTransport(lambda r: httpx.Response(200, content=json.dumps(data))))
    with TestClient(create_app(cfg, backends={"native": backend}), base_url="http://127.0.0.1:18765") as client:
        response = client.post("/v1/audio/transcriptions",
                               data={"model": route, "response_format": "verbose_json"},
                               files={"file": ("unknown.m4a", b"Zyntrabox")})
        assert response.status_code == 200
        assert response.json()["text"] == "Zyntrabox"
        row = client.app.state.db.rows_since()[0]
        assert row["status"] == "ok"
        assert row["audio_duration_s"] is None
        assert row["provider_request_ms"] is None
        assert row["estimated_cost"] is None
    assert not list(Path(cfg["logging"]["failed_clips_dir"]).glob("*.json"))


@pytest.mark.parametrize("data", [[], {"data": []}, {"data": "Zyntrabox"},
                                 {"data": {"latency": "fast", "provider_name": []}}])
def test_generation_ignores_bad_optional_diagnostics(cfg, data):
    async def run():
        backend = OpenRouterBackend(cfg, "k", httpx.MockTransport(lambda r: httpx.Response(200, json=data)))
        try:
            result = await backend.generation("Zyntrabox")
            assert result is None or result == {"latency": None, "provider_name": None}
        finally:
            await backend.aclose()
    asyncio.run(run())


def test_keep_warm_uses_replaced_settings_after_sleep(cfg):
    class Backend:
        def __init__(self):
            self.warmups = 0

        async def transcribe(self, *args, **kwargs):
            return {"text": "Zyntrabox", "provider_latency_ms": 1, "provider_model": "test"}

        async def warmup(self):
            self.warmups += 1

        async def aclose(self):
            pass

    backend = Backend()
    cfg["keep_warm"] = {"enabled": True, "interval_s": 0.15, "active_window_min": 30}
    with TestClient(create_app(cfg, backends={"openrouter": backend}), base_url="http://127.0.0.1:18765") as client:
        client.post("/v1/audio/transcriptions", files={"file": ("x.wav", make_wav())})
        deadline = time.monotonic() + 2
        while backend.warmups == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert backend.warmups > 0
        cfg["keep_warm"] = {"enabled": False, "interval_s": 0.15, "active_window_min": 30}
        before = backend.warmups
        time.sleep(0.35)
        assert backend.warmups == before
