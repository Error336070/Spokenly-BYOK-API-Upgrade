import asyncio

import httpx
import pytest

from proxy.backends import NativeBackend, OpenRouterBackend
from tests.test_translate import cfg  # noqa: F401


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
