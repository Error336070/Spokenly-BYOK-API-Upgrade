import json

import httpx
from fastapi.testclient import TestClient

from proxy import app as app_module
from proxy.app import create_app
from proxy.backends import OpenRouterBackend
from tests.test_translate import cfg, make_wav  # noqa: F401


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
