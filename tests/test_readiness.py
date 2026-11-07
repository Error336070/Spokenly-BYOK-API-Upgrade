import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.app import create_app, route_readiness
from proxy.backends import NativeBackend, OpenRouterBackend
import os
import time
import copy

SECRET = "secret-Zyntrabox-key"


def health(c):
    return c.get("/health").json()


def test_missing_native_key_is_unready_but_process_is_healthy(cfg, caplog):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"text": "x"}))
    backends = {"openrouter": OpenRouterBackend(cfg, SECRET, transport), "native": NativeBackend(cfg, {}, transport)}
    with caplog.at_level(logging.WARNING, logger="proxy"):
        with TestClient(create_app(cfg, backends=backends), base_url="http://127.0.0.1:18765") as c:
            h = health(c)
            text = c.get("/health").text
    assert h["status"] == "ok"
    assert h["route_readiness"]["assembly"] == {
        "backend": "native", "ready": False, "problem": "ASSEMBLYAI_API_KEY was not set when the proxy started"}
    assert h["route_readiness"]["gpt"] == {"backend": "openrouter", "ready": True}
    assert SECRET not in text and SECRET not in caplog.text
    assert "route assembly (backend native) will fail" in caplog.text


@pytest.mark.parametrize("keys,problem", [
    ({}, "AZURE_SPEECH_KEY and AZURE_SPEECH_REGION or AZURE_SPEECH_ENDPOINT was not set"),
    ({"azure": "k"}, "AZURE_SPEECH_REGION or AZURE_SPEECH_ENDPOINT was not set"),
    ({"azure_region": "Zyntrabox"}, "AZURE_SPEECH_KEY was not set"),
    ({"azure": "k", "azure_endpoint": "https://zyntrabox.invalid"}, None),
    ({"azure": "k", "azure_region": "Zyntrabox"}, None),
])
def test_azure_requirements(cfg, keys, problem):
    route = {**cfg["routes"]["mai-clean"], "backend": "native"}
    ready, got = NativeBackend(cfg, keys).readiness(route)
    assert ready is (problem is None)
    assert (got or "").startswith(problem or "")


def test_backend_switch_changes_readiness(cfg):
    backends = {"openrouter": OpenRouterBackend(cfg, ""), "native": NativeBackend(cfg, {"assemblyai": "k"})}
    assert route_readiness(cfg["routes"], backends)["assembly"]["ready"] is True
    cfg["routes"]["assembly"]["backend"] = "openrouter"
    r = route_readiness(cfg["routes"], backends)["assembly"]
    assert r == {"backend": "openrouter", "ready": False,
                 "problem": "OPENROUTER_API_KEY was not set when the proxy started"}
    assert route_readiness(cfg["routes"], {})["gpt"]["problem"] == "backend 'openrouter' is not available"
    cfg["routes"]["assembly"]["native"]["provider"] = "Zyntrabox"
    cfg["routes"]["assembly"]["backend"] = "native"
    assert "not implemented" in route_readiness(cfg["routes"], backends)["assembly"]["problem"]


def test_readiness_uses_startup_credentials_not_later_env(cfg, monkeypatch):
    for name in ("OPENROUTER_API_KEY", "ASSEMBLYAI_API_KEY", "AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION",
                 "AZURE_SPEECH_ENDPOINT"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", SECRET)
    with TestClient(create_app(cfg), base_url="http://127.0.0.1:18765") as c:
        assert health(c)["route_readiness"]["assembly"]["ready"] is True
        assert health(c)["route_readiness"]["gpt"]["ready"] is False
        monkeypatch.setenv("OPENROUTER_API_KEY", "added-later")  # the running client never sees it
        monkeypatch.setenv("ASSEMBLYAI_API_KEY", "")
        h = health(c)
        assert h["route_readiness"]["gpt"]["ready"] is False
        assert h["route_readiness"]["assembly"]["ready"] is True
        assert SECRET not in c.get("/health").text


def test_unknown_backend_readiness_is_none(cfg):
    class Fake:
        async def aclose(self):
            pass

    assert route_readiness(cfg["routes"], {"openrouter": Fake()})["gpt"] == {"backend": "openrouter", "ready": None}


def test_hot_reload_reports_newly_unready_route(cfg, tmp_path, monkeypatch, caplog):
    import proxy.app as app_module
    from proxy.backends import NativeBackend, OpenRouterBackend
    from fastapi.testclient import TestClient

    cfg["notifications"]["enabled"] = False
    cfg["vocabulary_file"] = None
    changed = copy.deepcopy(cfg)
    changed["routes"]["assembly"]["backend"] = "openrouter"
    config_path = tmp_path / "Zyntrabox.yaml"
    config_path.write_text("first", encoding="utf-8")
    monkeypatch.setattr(app_module, "load_config", lambda path: changed)
    backends = {"openrouter": OpenRouterBackend(cfg, ""), "native": NativeBackend(cfg, {"assemblyai": "fake"})}
    with caplog.at_level(logging.WARNING, logger="proxy"):
        with TestClient(app_module.create_app(cfg, backends=backends, config_path=config_path,
                                             watch_interval_s=0.01), base_url="http://127.0.0.1:18765") as client:
            assert client.get("/health").json()["route_readiness"]["assembly"]["ready"] is True
            time.sleep(0.05)
            caplog.clear()
            os.utime(config_path, (time.time() + 2,) * 2)
            deadline = time.monotonic() + 3
            while cfg["routes"]["assembly"]["backend"] != "openrouter" and time.monotonic() < deadline:
                time.sleep(0.01)
            health = client.get("/health").json()
            assert health["status"] == "ok"
            assert health["route_readiness"]["assembly"]["ready"] is False
            assert "route assembly (backend openrouter) will fail" in caplog.text
