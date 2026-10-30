import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.backends import OpenRouterBackend
from proxy.notify import Notifier
from tests.test_translate import cfg, make_wav  # noqa: F401


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
