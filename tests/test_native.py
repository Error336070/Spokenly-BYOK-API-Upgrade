import json

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.backends import NativeBackend, OpenRouterBackend
from tests.helpers import make_wav
from pathlib import Path

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


def _app(cfg, handler, keys=KEYS):
    native = NativeBackend(cfg, keys, transport=httpx.MockTransport(handler))
    orb = OpenRouterBackend(cfg, "k", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    return TestClient(create_app(cfg, backends={"openrouter": orb, "native": native}), base_url="http://127.0.0.1:18765")


def test_assemblyai_request_and_response(cfg):
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


def test_azure_request_and_response(cfg):
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
def test_missing_native_keys(cfg, route, keys, needle):
    cfg["routes"][route]["backend"] = "native"
    with _app(cfg, lambda r: httpx.Response(200), keys=keys) as c:
        r = c.post("/v1/audio/transcriptions", data={"model": route},
                   files={"file": ("a.wav", make_wav(), "audio/wav")})
        assert r.status_code == 500 and needle in r.json()["error"]["message"]


def test_native_upstream_errors(cfg):
    cfg["routes"]["assembly"]["backend"] = "native"
    problem = {"type": "about:blank", "title": "Unauthorized", "status": 401, "detail": "Invalid API key"}
    with _app(cfg, lambda r: httpx.Response(401, json=problem)) as c:
        r = c.post("/v1/audio/transcriptions", data={"model": "assembly"},
                   files={"file": ("a.wav", make_wav(), "audio/wav")})
        msg = r.json()["error"]["message"]
        assert r.status_code == 502 and "Invalid API key" in msg and "ASSEMBLYAI_API_KEY" in msg
        # A failed row still names the model the route called.
        row = c.app.state.db._conn.execute("SELECT status, provider_model FROM requests").fetchone()
        assert row == ("error", "assemblyai/universal-3-5-pro")


@pytest.mark.parametrize("extra,request_id,server_ms", [
    ({"session_id": "Zyntrabox-1", "request_time_ms": 300}, "Zyntrabox-1", 300.0),
    ({}, None, None),
    ({"session_id": 7, "request_time_ms": "slow"}, None, None),
])
def test_assemblyai_diagnostics_and_estimate_logged(cfg, extra, request_id, server_ms):
    cfg["routes"]["assembly"]["backend"] = "native"
    cfg["credit"] = {"enabled": False, "assemblyai_hourly_rate_usd": 0.36}
    with _app(cfg, lambda r: httpx.Response(200, json={"text": "Zyntrabox", **extra})) as c:
        c.post("/v1/audio/transcriptions", data={"model": "assembly"},
               files={"file": ("a.wav", make_wav(10.0), "audio/wav")})
        row = c.app.state.db._conn.execute(
            "SELECT provider_request_id, provider_request_ms, estimated_cost, cost FROM requests").fetchone()
    assert row[:2] == (request_id, server_ms)
    assert row[2] == pytest.approx(0.001) and row[3] is None  # estimate only, no billed cost


def test_openrouter_rows_have_no_native_estimate(cfg):
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


def test_old_latency_column_is_renamed_and_kept(tmp_path):
    import sqlite3
    from proxy.storage import RequestLog
    path = str(tmp_path / "Zyntrabox.sqlite")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, total_latency_ms REAL)")
    old.execute("INSERT INTO requests (ts, total_latency_ms) VALUES ('2026-01-01', 25.0)")
    old.commit()
    old.close()
    for _ in range(2):  # reopening is idempotent
        db = RequestLog(path)
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(requests)")}
        assert "processing_ms" in cols and "total_latency_ms" not in cols
        assert db.rows_since()[0]["processing_ms"] == 25.0
        db.close()


@pytest.mark.parametrize("provider,route_name", [("assemblyai", "assembly"), ("azure", "mai-clean")])
@pytest.mark.parametrize("request_language,override,expected", [
    ("de", None, "de"), (None, None, "fr"), ("fr", "en", "en"), ("fr", "en,de", None), ("fr", "", None)])
def test_native_verbose_response_preserves_resolved_language(cfg, provider, route_name, request_language, override, expected):
    cfg["language"] = "fr"
    cfg["notifications"]["enabled"] = False
    cfg["routes"][route_name]["backend"] = "native"
    field = "language_codes" if provider == "assemblyai" else "locales"
    if override is not None:
        cfg["routes"][route_name]["native"].setdefault("body", {})[field] = (
            override if provider == "assemblyai" else override.split(",") if override else [])
    seen = {}

    def handler(request):
        parts = _parts(request)
        payload = json.loads(parts["config" if provider == "assemblyai" else "definition"][1])
        seen["language"] = payload[field]
        data = {"text": "Zyntrabox"} if provider == "assemblyai" else {"combinedPhrases": [{"text": "Zyntrabox"}]}
        return httpx.Response(200, json=data)

    backend = NativeBackend(cfg, {"assemblyai": "synthetic", "azure": "synthetic", "azure_region": "Zyntrabox"},
                            transport=httpx.MockTransport(handler))
    fields = {"model": route_name, "response_format": "verbose_json"}
    if request_language is not None:
        fields["language"] = request_language
    with TestClient(create_app(cfg, backends={"native": backend}), base_url="http://127.0.0.1:18765") as client:
        response = client.post("/v1/audio/transcriptions", data=fields,
                               files={"file": ("synthetic.wav", make_wav(), "audio/wav")})
    assert response.status_code == 200
    upstream_language = override if override is not None else request_language or "fr"
    assert seen["language"] == (upstream_language if provider == "assemblyai" else upstream_language.split(",") if upstream_language else [])
    assert response.json()["language"] == expected


@pytest.mark.parametrize("provider", ["assemblyai", "azure"])
@pytest.mark.parametrize("value", ["1000", True, -1, [], {}, float("inf"), float("nan"), 10**400])
def test_native_bad_diagnostics_preserve_text(cfg, provider, value):
    data = ({"text": "Zyntrabox", "audio_duration_ms": value, "request_time_ms": value}
            if provider == "assemblyai" else
            {"combinedPhrases": [{"text": "Zyntrabox"}], "durationMilliseconds": value})
    route = "assembly" if provider == "assemblyai" else "mai-clean"
    cfg["routes"][route]["backend"] = "native"
    cfg["notifications"]["enabled"] = False
    backend = NativeBackend(cfg, {"assemblyai": "k", "azure": "k", "azure_region": "Zyntrabox"},
                            httpx.MockTransport(lambda r: httpx.Response(200, content=json.dumps(data))))
    with TestClient(create_app(cfg, backends={"native": backend}), base_url="http://127.0.0.1:18765") as client:
        response = client.post("/v1/audio/transcriptions",
                               data={"model": route, "response_format": "verbose_json"},
                               files={"file": ("unknown.m4a", b"Zyntrabox")})
        assert response.status_code == 200
        assert response.json()["text"] == "Zyntrabox"
        row = client.app.state.db.rows_since()[0]
        assert row["status"] == "ok"
        assert row["audio_duration_s"] is None
        assert row["provider_request_ms"] is None
        assert row["estimated_cost"] is None
    assert not list(Path(cfg["logging"]["failed_clips_dir"]).glob("*.json"))
