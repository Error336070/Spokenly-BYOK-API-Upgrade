import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.backends import OpenRouterBackend
from proxy.notify import Notifier
from tests.test_translate import cfg, make_wav  # noqa: F401


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
    with TestClient(create_app(cfg, backends={}, config_path=path, watch_interval_s=0.01)) as c:
        time.sleep(0.1)
        assert len(calls) > 1
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
                              notifier=Notifier(cfg, send=lambda t, b: sent.append(t)))) as c:
        c.post("/v1/audio/transcriptions", files={"file": ("a.wav", make_wav())})
        time.sleep(0.05)
        assert any("Background task failed" in t for t in sent)
