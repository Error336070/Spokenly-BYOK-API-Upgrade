import json

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.backends import NativeBackend, OpenRouterBackend
from tests.test_translate import cfg, make_wav  # noqa: F401  (fixture)

KEYS = {"assemblyai": "aai-key", "azure": "az-key", "azure_region": "northeurope", "azure_endpoint": ""}


def _parts(req: httpx.Request) -> dict:
    """name -> (content-type, bytes) for a multipart request."""
    boundary = req.headers["content-type"].split("boundary=")[1].encode()
    out = {}
    for chunk in req.content.split(b"--" + boundary)[1:-1]:
        head, _, body = chunk.strip(b"\r\n").partition(b"\r\n\r\n")
        h = head.decode()
        name = h.split('name="')[1].split('"')[0]
        ctype = h.split("Content-Type: ")[1].split("\r\n")[0] if "Content-Type: " in h else None
        out[name] = (ctype, body)
    return out


def _app(cfg, handler, keys=KEYS):  # noqa: F811
    native = NativeBackend(cfg, keys, transport=httpx.MockTransport(handler))
    orb = OpenRouterBackend(cfg, "k", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    return TestClient(create_app(cfg, backends={"openrouter": orb, "native": native}), base_url="http://127.0.0.1:18765")


def test_assemblyai_request_and_response(cfg):  # noqa: F811
    cfg["routes"]["assembly"]["backend"] = "native"
    cfg["vocabulary"] = ["Zyntrabox", "Spokenly"]
    seen = {}

    def handler(req):
        seen["req"] = req
        return httpx.Response(200, json={"text": " Hello there. ", "audio_duration_ms": 1000,
                                         "session_id": "s1", "request_time_ms": 300})

    with _app(cfg, handler) as c:
        r = c.post("/v1/audio/transcriptions", data={"model": "assembly", "language": "en"},
                   files={"file": ("a.wav", make_wav(1.0), "audio/wav")})
        assert r.json() == {"text": "Hello there."}
        req = seen["req"]
        assert str(req.url) == "https://sync.eu.assemblyai.com/v1/transcribe"
        assert req.headers["authorization"] == "aai-key"
        assert req.headers["x-aai-model"] == "universal-3-5-pro"
        parts = _parts(req)
        assert parts["audio"][0] == "audio/wav" and parts["audio"][1][:4] == b"RIFF"
        assert json.loads(parts["config"][1]) == {"keyterms_prompt": ["Zyntrabox", "Spokenly"], "language_codes": "en"}
        row = c.app.state.db._conn.execute("SELECT backend, provider_model, transcript FROM requests").fetchone()
        assert row == ("native", "assemblyai/universal-3-5-pro", "Hello there.")


def test_azure_request_and_response(cfg):  # noqa: F811
    cfg["routes"]["mai-clean"]["backend"] = "native"
    cfg["vocabulary"] = ["Zyntrabox"]
    seen = {}

    def handler(req):
        seen["req"] = req
        return httpx.Response(200, json={"durationMilliseconds": 1000,
                                         "combinedPhrases": [{"text": "Take zyntrabox at night."}], "phrases": []})

    with _app(cfg, handler) as c:
        r = c.post("/v1/audio/transcriptions", data={"model": "mai-clean"},
                   files={"file": ("a.wav", make_wav(1.0), "audio/wav")})
        assert r.json() == {"text": "Take zyntrabox at night."}
        req = seen["req"]
        assert req.url.host == "northeurope.api.cognitive.microsoft.com"
        assert req.url.path == "/speechtotext/transcriptions:transcribe"
        assert req.url.params["api-version"] == "2025-10-15"
        assert req.headers["ocp-apim-subscription-key"] == "az-key"
        d = json.loads(_parts(req)["definition"][1])
        assert d["enhancedMode"] == {"enabled": True, "model": "MAI-Transcribe-2",
                                     "modelOptions": {"transcribeStyle": "clean"}}
        assert d["phraseList"] == {"phrases": ["Zyntrabox"]}
        assert d["locales"] == ["en"]
        # route config is not mutated by per-request additions
        assert "phraseList" not in cfg["routes"]["mai-clean"]["native"]["body"]


@pytest.mark.parametrize("route,keys,needle", [
    ("assembly", {}, "ASSEMBLYAI_API_KEY"),
    ("mai-clean", {"azure": "k"}, "AZURE_SPEECH_REGION"),
])
def test_missing_native_keys(cfg, route, keys, needle):  # noqa: F811
    cfg["routes"][route]["backend"] = "native"
    with _app(cfg, lambda r: httpx.Response(200), keys=keys) as c:
        r = c.post("/v1/audio/transcriptions", data={"model": route},
                   files={"file": ("a.wav", make_wav(), "audio/wav")})
        assert r.status_code == 500 and needle in r.json()["error"]["message"]


def test_native_upstream_errors(cfg):  # noqa: F811
    cfg["routes"]["assembly"]["backend"] = "native"
    problem = {"type": "about:blank", "title": "Unauthorized", "status": 401, "detail": "Invalid API key"}
    with _app(cfg, lambda r: httpx.Response(401, json=problem)) as c:
        r = c.post("/v1/audio/transcriptions", data={"model": "assembly"},
                   files={"file": ("a.wav", make_wav(), "audio/wav")})
        msg = r.json()["error"]["message"]
        assert r.status_code == 502 and "Invalid API key" in msg and "ASSEMBLYAI_API_KEY" in msg
