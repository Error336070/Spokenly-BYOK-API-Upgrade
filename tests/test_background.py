import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.backends import OpenRouterBackend
from proxy.notify import Notifier
from tests.helpers import make_wav


def wait_until(pred, timeout=2):
    """Poll instead of a fixed sleep, so a busy machine doesn't fail the test."""
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.01)
    return pred()


def test_watcher_recovers_after_initial_stat_error(cfg, monkeypatch, tmp_path):
    path = tmp_path / "Zyntrabox.yaml"
    path.write_text("x")
    original = Path.stat
    calls = []

    def stat(self, *a, **kw):
        if self == path:
            calls.append(1)
            if len(calls) == 1:
                raise OSError("Zyntrabox")
        return original(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", stat)
    cfg["notifications"]["enabled"] = False
    with TestClient(create_app(cfg, backends={}, config_path=path, watch_interval_s=0.01), base_url="http://127.0.0.1:18765") as c:
        assert wait_until(lambda: len(calls) > 1)
        assert c.get("/health").json()["background"]["watch_config"] == "running"


def test_unexpected_task_failure_logs_and_notifies(cfg):
    cfg["openrouter"]["generation_stats_delays_s"] = [0]
    sent = []
    backend = OpenRouterBackend(cfg, "k", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"text": "Zyntrabox"}, headers={"x-generation-id": "x"})))

    async def fail(gen_id):
        raise RuntimeError("Zyntrabox")

    backend.generation = fail
    with TestClient(create_app(cfg, backends={"openrouter": backend},
                              notifier=Notifier(cfg, send=lambda t, b: sent.append(t))), base_url="http://127.0.0.1:18765") as c:
        c.post("/v1/audio/transcriptions", files={"file": ("a.wav", make_wav())})
        assert wait_until(lambda: any("Background task failed" in t for t in sent))


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


def test_keep_warm_only_while_active(cfg):
    class FakeBackend:
        def __init__(self):
            self.warmups = 0

        async def transcribe(self, *a, **kw):
            return {"text": "hi", "provider_latency_ms": 1, "provider_model": "x"}

        async def warmup(self):
            self.warmups += 1

        async def aclose(self):
            pass

    b = FakeBackend()
    cfg["keep_warm"] = {"enabled": True, "interval_s": 0.05, "active_window_min": 30}
    with TestClient(create_app(cfg, backends={"openrouter": b}), base_url="http://127.0.0.1:18765") as c:
        time.sleep(0.2)
        assert b.warmups == 0                      # no dictation yet: stay quiet
        c.post("/v1/audio/transcriptions", files={"file": ("a.wav", make_wav(), "audio/wav")})
        deadline = time.monotonic() + 2
        while b.warmups < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert b.warmups >= 2                      # active: pings
        cfg["keep_warm"]["active_window_min"] = 0  # window passed (hot-reload style change)
        time.sleep(0.1)
        n = b.warmups
        time.sleep(0.2)
        assert b.warmups == n                      # idle: pings stop
