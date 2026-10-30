import asyncio

import httpx
import pytest

from proxy.backends import NativeBackend, OpenRouterBackend
from proxy.app import create_app
from fastapi.testclient import TestClient
from tests.test_translate import cfg, make_wav  # noqa: F401


@pytest.mark.parametrize("connect", [True, False])
def test_connection_trace_is_recorded(cfg, connect):
    async def handler(request):
        trace = request.extensions["trace"]
        if connect:
            await trace("connection.connect_tcp.started", {})
        await trace("http11.send_request_headers.started", {})
        return httpx.Response(200, json={"text": "Zyntrabox"})

    async def run():
        backend = OpenRouterBackend(cfg, "k", httpx.MockTransport(handler))
        try:
            result = await backend.transcribe(b"x", "wav", cfg["routes"]["gpt"], "en")
            assert result["new_connection"] is connect
        finally:
            await backend.aclose()
    asyncio.run(run())


def test_warmup_uses_configured_assembly_model(cfg):
    cfg["routes"]["assembly"]["native"]["model"] = "Zyntrabox"
    seen = []

    async def run():
        backend = NativeBackend(cfg, {"assemblyai": "k"}, httpx.MockTransport(
            lambda r: seen.append(r.headers["x-aai-model"]) or httpx.Response(200)))
        await backend.warmup()
        await backend.aclose()
    asyncio.run(run())
    assert seen == ["Zyntrabox"]


@pytest.mark.parametrize("provider", ["openrouter", "assemblyai", "azure"])
@pytest.mark.parametrize("failure", ["timeout", "http", "parse"])
def test_failed_request_keeps_observed_connection(cfg, provider, failure):
    async def handler(request):
        await request.extensions["trace"]("connection.connect_tcp.started", {})
        if failure == "timeout":
            raise httpx.ReadTimeout("Zyntrabox")
        return httpx.Response(502 if failure == "http" else 200, json={})

    transport = httpx.MockTransport(handler)
    cfg["notifications"]["enabled"] = False
    if provider == "openrouter":
        backend, name = OpenRouterBackend(cfg, "k", transport), "gpt"
    else:
        backend = NativeBackend(cfg, {"assemblyai": "k", "azure": "k", "azure_region": "Zyntrabox"}, transport)
        name = "assembly" if provider == "assemblyai" else "mai-clean"
        cfg["routes"][name]["backend"] = "native"
    with TestClient(create_app(cfg, backends={backend.name: backend}), base_url="http://127.0.0.1:18765") as c:
        response = c.post("/v1/audio/transcriptions", data={"model": name}, files={"file": ("x.wav", make_wav())})
        assert response.status_code in (502, 504)
        assert c.app.state.db._conn.execute("SELECT new_connection FROM requests").fetchone() == (1,)
        model = c.app.state.db._conn.execute("SELECT provider_model FROM requests").fetchone()[0]
        assert model and model.startswith(provider if provider != "openrouter" else "openai/")
