import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.backends import OpenRouterBackend
from proxy.notify import Notifier
from tests.test_translate import cfg  # noqa: F401


@pytest.mark.parametrize("data,remaining", [
    ({"limit_remaining": 0.5}, 0.5), ({"limit": 2, "usage": 1.5}, 0.5),
    ({"limit_remaining": None, "limit": None}, None), ({}, None),
])
def test_key_info_remaining(cfg, data, remaining):
    async def run():
        seen = []
        backend = OpenRouterBackend(cfg, "k", httpx.MockTransport(
            lambda r: seen.append((r.method, r.url.path)) or httpx.Response(200, json={"data": data})))
        try:
            assert await backend.remaining_credit() == remaining
            assert seen == [("GET", "/api/v1/key")]
        finally:
            await backend.aclose()
    asyncio.run(run())


def test_credit_loop_checks_at_start_and_notifies(cfg):
    cfg["credit"] = {"enabled": True, "interval_s": 0.02, "low_usd": 1}
    sent, seen = [], []
    backend = OpenRouterBackend(cfg, "k", httpx.MockTransport(
        lambda r: seen.append(r.url.path) or httpx.Response(200, json={"data": {"limit_remaining": 0.5}})))
    with TestClient(create_app(cfg, backends={"openrouter": backend},
                              notifier=Notifier(cfg, send=lambda t, b: sent.append(t))),
                    base_url="http://127.0.0.1:18765"):
        time.sleep(0.08)
        assert len(seen) >= 2
        assert len(sent) == 1 and "credit" in sent[0]
