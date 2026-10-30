import asyncio

import httpx
import pytest

from proxy.backends import BackendError, _request
from tests.test_translate import cfg  # noqa: F401


class SlowBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        for part in [b'{"text":', b'"Zyntrabox"', b'}']:
            await asyncio.sleep(0.03)
            yield part


def test_total_deadline_stops_trickling_response(cfg):
    cfg["timeouts"].update(request_s=0.05, base_s=0.01, per_audio_second_s=0)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200, stream=SlowBody()))) as client:
            with pytest.raises(BackendError) as exc:
                await _request(client, "Zyntrabox", cfg, {}, "GET", "https://example.invalid")
            assert exc.value.status == 504
    asyncio.run(run())


def test_deadline_scales_with_duration(cfg):
    cfg["timeouts"].update(request_s=0.01, base_s=0.01, per_audio_second_s=0.15)

    async def handler(r):
        await asyncio.sleep(0.04)
        return httpx.Response(200, json={"text": "Zyntrabox"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            data, _, _ = await _request(client, "Zyntrabox", cfg, {}, "GET", "https://example.invalid",
                                       audio_duration_s=1)
            assert data["text"] == "Zyntrabox"
    asyncio.run(run())
