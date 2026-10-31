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


@pytest.mark.parametrize("extra,request_id,server_ms", [
    ({"session_id": "Zyntrabox-1", "request_time_ms": 300}, "Zyntrabox-1", 300.0),
    ({}, None, None),
    ({"session_id": 7, "request_time_ms": "slow"}, None, None),
])
def test_assemblyai_diagnostics_and_estimate_logged(cfg, extra, request_id, server_ms):  # noqa: F811
    cfg["routes"]["assembly"]["backend"] = "native"
    cfg["credit"] = {"enabled": False, "assemblyai_hourly_rate_usd": 0.36}
    with _app(cfg, lambda r: httpx.Response(200, json={"text": "Zyntrabox", **extra})) as c:
        c.post("/v1/audio/transcriptions", data={"model": "assembly"},
               files={"file": ("a.wav", make_wav(10.0), "audio/wav")})
        row = c.app.state.db._conn.execute(
            "SELECT provider_request_id, provider_request_ms, estimated_cost, cost FROM requests").fetchone()
    assert row[:2] == (request_id, server_ms)
    assert row[2] == pytest.approx(0.001) and row[3] is None  # estimate only, no billed cost


def test_openrouter_rows_have_no_native_estimate(cfg):  # noqa: F811
    cfg["credit"] = {"enabled": False, "assemblyai_hourly_rate_usd": 0.36}
    orb = OpenRouterBackend(cfg, "k", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"text": "x", "usage": {"cost": 0.002}})))
    with TestClient(create_app(cfg, backends={"openrouter": orb}), base_url="http://127.0.0.1:18765") as c:
        c.post("/v1/audio/transcriptions", data={"model": "gpt"},
               files={"file": ("a.wav", make_wav(10.0), "audio/wav")})
        row = c.app.state.db._conn.execute(
            "SELECT cost, estimated_cost, provider_request_id FROM requests").fetchone()
    assert row == (0.002, None, None)


def test_old_database_gains_diagnostic_columns(tmp_path):
    import sqlite3
    from proxy.storage import RequestLog
    path = str(tmp_path / "Zyntrabox.sqlite")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, cost REAL)")
    old.execute("INSERT INTO requests (ts, cost) VALUES ('2026-01-01', 0.5)")
    old.commit()
    old.close()
    for _ in range(2):  # reopening is idempotent
        db = RequestLog(path)
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(requests)")}
        assert {"estimated_cost", "provider_request_id", "provider_request_ms"} <= cols
        assert db._conn.execute("SELECT cost, estimated_cost FROM requests").fetchall() == [(0.5, None)]
        db.close()
