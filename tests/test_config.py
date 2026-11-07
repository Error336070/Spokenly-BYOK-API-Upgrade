import copy

import pytest

from proxy.config import validate
import time
import httpx
import yaml
from fastapi.testclient import TestClient
from proxy.app import create_app
from proxy.backends import OpenRouterBackend
from proxy.config import ROOT, load_config
from proxy.notify import Notifier
from tests.helpers import make_wav
import json
from proxy import app as app_module


@pytest.mark.parametrize("path,value", [
    ("timeouts", {}), ("timeouts.request_s", "15"), ("timeouts.connect_s", -1),
    ("keep_warm", True), ("keep_warm.active_window_min", 31), ("logging", {}),
    ("logging.retention_days", -1), ("logging.save_audio", "false"),
    ("routes.gpt.openrouter.vocab_template", "{oops}"),
    ("routes.gpt.backend", "Zyntrabox"), ("routes.gpt.openrouter.body", []),
    ("routes.gpt.openrouter.vocab_max", -1), ("vocabulary", "Zyntrabox"),
    ("openrouter.generation_stats_delays_s", ["5"]), ("server.port", True),
    ("notifications.cooldown_s", float("nan")),
    ("native.assemblyai.base_url", "https://example.invalid:Zyntrabox"),
    ("native.assemblyai.base_url", "https://example.invalid:70000"),
    ("routes.assembly.native.model", "Zyntrabox\nbroken"),
    ("routes.assembly.native.model", "Zyntrabox\u0131"),
    ("routes.mai-clean.native.body.enhancedMode", "Zyntrabox"),
])
def test_reject_invalid_config(cfg, path, value):
    data = copy.deepcopy(cfg)
    keys = path.split(".")
    block = data
    for key in keys[:-1]:
        block = block[key]
    block[keys[-1]] = value
    with pytest.raises(ValueError):
        validate(data)


def test_zero_retention_keeps_data(cfg):
    cfg["logging"]["retention_days"] = 0
    validate(cfg)


def test_vocabulary_file_merged_and_hot_reloaded(tmp_path):
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw["credit"] = {"enabled": False}
    raw["server"]["port"] = 18765
    raw["logging"].update(db_path=str(tmp_path / "db.sqlite"), clips_dir=str(tmp_path / "clips"))
    raw["vocabulary"] = ["Shared"]
    raw["vocabulary_file"] = str(tmp_path / "vocab.txt")
    (tmp_path / "vocab.txt").write_text("# comment\nAlpha\n\nBeta  # trailing comment\n", encoding="utf-8")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    cfg = load_config(path)
    assert cfg["vocabulary"] == ["Shared", "Alpha", "Beta"]

    backend = OpenRouterBackend(cfg, "k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"text": "x"})))
    with TestClient(create_app(cfg, backends={"openrouter": backend}, config_path=path, watch_interval_s=0.05), base_url="http://127.0.0.1:18765"):
        time.sleep(0.05)
        (tmp_path / "vocab.txt").write_text("Alpha\nGamma\n", encoding="utf-8")
        for _ in range(60):
            if cfg["vocabulary"] == ["Shared", "Alpha", "Gamma"]:
                break
            time.sleep(0.05)
        assert cfg["vocabulary"] == ["Shared", "Alpha", "Gamma"]


def test_hot_reload_applies_valid_and_rejects_invalid(tmp_path):
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw["credit"] = {"enabled": False}
    raw["server"]["port"] = 18765
    raw["logging"].update(db_path=str(tmp_path / "db.sqlite"), clips_dir=str(tmp_path / "clips"))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    cfg = load_config(path)
    sent = []
    backend = OpenRouterBackend(cfg, "k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"text": "x"})))
    app = create_app(cfg, backends={"openrouter": backend}, config_path=path,
                     notifier=Notifier(cfg, send=lambda t, b: sent.append(t)), watch_interval_s=0.05)

    def wait_for(pred):
        for _ in range(60):
            if pred():
                return True
            time.sleep(0.05)
        return False

    with TestClient(app, base_url="http://127.0.0.1:18765") as c:
        raw["routes"]["new-route"] = {"backend": "openrouter", "openrouter": {"model": "a/b"}}
        raw["timeouts"]["request_s"] = 7
        time.sleep(0.05)
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        assert wait_for(lambda: "new-route" in [m["id"] for m in c.get("/v1/models").json()["data"]])
        assert backend.cfg["timeouts"]["request_s"] == 7   # backend sees the same live dict

        time.sleep(0.05)
        path.write_text("default_route: nope\nroutes: {}\n", encoding="utf-8")
        assert wait_for(lambda: sent)
        assert "config.yaml has an error" in sent[0]
        assert "new-route" in [m["id"] for m in c.get("/v1/models").json()["data"]]  # previous config kept


def test_reload_during_encoding_uses_one_snapshot(cfg, monkeypatch):
    cfg["vocabulary"] = ["Zyntrabox"]
    original = app_module.prepare_upload
    seen = []

    def encode(audio, fmt, settings):
        cfg["vocabulary"] = ["Zyntracube"]
        cfg["routes"]["gpt"]["openrouter"]["model"] = "Zyntracube/new"
        return original(audio, fmt, settings)

    monkeypatch.setattr(app_module, "prepare_upload", encode)
    backend = OpenRouterBackend(cfg, "k", httpx.MockTransport(
        lambda r: seen.append(json.loads(r.content)) or httpx.Response(200, json={"text": "Zyntrabox"})))
    with TestClient(create_app(cfg, backends={"openrouter": backend}), base_url="http://127.0.0.1:18765") as c:
        c.post("/v1/audio/transcriptions", data={"model": "gpt"}, files={"file": ("x.wav", make_wav())})
    assert seen[0]["model"] == "openai/gpt-transcribe"
    assert seen[0]["provider"]["options"]["openai"]["prompt"] == "Vocabulary: Zyntrabox"
