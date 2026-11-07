import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.backends import OpenRouterBackend
from proxy.notify import Notifier


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
        deadline = time.monotonic() + 2
        while (len(seen) < 2 or not sent) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(seen) >= 2
        assert len(sent) == 1 and "credit" in sent[0]


def test_unreachable_credit_check_retries_soon_without_a_toast(cfg, monkeypatch):
    # At logon or wake the network may not be up yet; that is not a broken background task.
    monkeypatch.setattr("proxy.app.CREDIT_RETRY_S", 0.02)
    cfg["credit"] = {"enabled": True, "interval_s": 3600, "low_usd": 1}
    sent, calls = [], []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            raise httpx.ConnectError("network is unreachable")
        return httpx.Response(200, json={"data": {"limit_remaining": 0.5}})

    backend = OpenRouterBackend(cfg, "k", httpx.MockTransport(handler))
    with TestClient(create_app(cfg, backends={"openrouter": backend},
                              notifier=Notifier(cfg, send=lambda t, b: sent.append(t))),
                    base_url="http://127.0.0.1:18765"):
        deadline = time.monotonic() + 2
        while not sent and time.monotonic() < deadline:
            time.sleep(0.01)
    assert len(calls) == 2
    assert len(sent) == 1 and "credit is low" in sent[0]
