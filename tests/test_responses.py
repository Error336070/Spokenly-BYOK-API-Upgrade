import asyncio

import httpx
import pytest

from proxy.backends import BackendError, NativeBackend, OpenRouterBackend
from tests.test_translate import cfg  # noqa: F401


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


def setup(cfg, provider, data):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=data))
    if provider == "openrouter":
        return OpenRouterBackend(cfg, "k", transport), cfg["routes"]["gpt"]
    return NativeBackend(cfg, {"assemblyai": "k", "azure": "k", "azure_region": "Zyntrabox"}, transport), {
        "native": {"provider": provider, "body": {}}}


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
