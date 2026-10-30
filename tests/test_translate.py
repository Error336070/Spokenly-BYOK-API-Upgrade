import base64
import io
import json
import struct
import time
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.audio import sniff_format, wav_info
from proxy.backends import OpenRouterBackend
from proxy.config import ROOT
from proxy.translate import build_openrouter_body, format_response, trim_vocab

import yaml


def make_wav(seconds=1.0, rate=16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", 0) * int(seconds * rate))
    return buf.getvalue()


@pytest.fixture
def cfg(tmp_path):
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        c = yaml.safe_load(f)
    c["server"]["port"] = 18765
    c["logging"].update(db_path=str(tmp_path / "db.sqlite"), clips_dir=str(tmp_path / "clips"))
    c["logging"]["failed_clips_dir"] = str(tmp_path / "failed")
    # these tests inspect the JSON/WAV request shape; test_upload.py covers FLAC + multipart
    c["openrouter"]["multipart"] = False
    c["upload"] = {"flac": False}
    return c


def test_sniff_and_wav_info():
    wav = make_wav(2.5)
    assert sniff_format(wav) == "wav"
    assert wav_info(wav) == {"sample_rate": 16000, "channels": 1, "bits": 16, "duration_s": 2.5}
    assert sniff_format(b"fLaC" + b"\0" * 20) == "flac"
    assert sniff_format(b"OggS" + b"\0" * 20) == "ogg"
    assert sniff_format(b"\x1aE\xdf\xa3" + b"\0" * 20) == "webm"
    assert sniff_format(b"\0\0\0\x20ftypM4A " + b"\0" * 8) == "m4a"
    assert sniff_format(b"ID3\x04" + b"\0" * 20) == "mp3"
    assert sniff_format(b"junk", "clip.mp3") == "mp3"


def test_trim_vocab_dedupes_and_limits():
    assert trim_vocab(["a", "A", " b ", "", "c"], 2) == ["a", "b"]


def test_build_body_mai(cfg):
    route = cfg["routes"]["mai-clean"]["openrouter"]
    body = build_openrouter_body(b"abc", "wav", route, "en", ["Foo", "Bar"])
    assert body["model"] == "microsoft/mai-transcribe-2"
    assert body["input_audio"] == {"data": base64.b64encode(b"abc").decode(), "format": "wav"}
    assert body["language"] == "en"
    azure = body["provider"]["options"]["azure"]
    assert azure["enhancedMode"]["modelOptions"]["transcribeStyle"] == "clean"
    assert azure["phraseList"]["phrases"] == ["Foo", "Bar"]
    # route config must not be mutated between requests
    assert "phraseList" not in route["body"]["provider"]["options"]["azure"]


def test_build_body_voxtral_caps_vocab():
    # Voxtral's verified settings (route removed from config.yaml, kept here as the reference shape)
    route = {"model": "mistralai/voxtral-small-transcribe-3-0", "body": {"provider": {"order": ["mistral/eu"]}},
             "vocab_path": "provider.options.mistral.context_bias", "vocab_max": 100, "vocab_join": ","}
    body = build_openrouter_body(b"x", "webm", route, None, [f"t{i}" for i in range(150)])
    assert "language" not in body
    bias = body["provider"]["options"]["mistral"]["context_bias"]
    assert isinstance(bias, str) and len(bias.split(",")) == 100
    assert body["provider"]["order"] == ["mistral/eu"]


def test_vocab_template_and_all_routes_build(cfg):
    body = build_openrouter_body(b"x", "wav", cfg["routes"]["gpt"]["openrouter"], "en", ["Foo", "Bar"])
    assert body["provider"]["options"]["openai"]["prompt"] == "Vocabulary: Foo, Bar"
    for name, r in cfg["routes"].items():
        if r["backend"] == "openrouter":
            assert "/" in build_openrouter_body(b"x", "wav", r["openrouter"], "en", ["Foo"])["model"], name


@pytest.mark.parametrize("fmt,media,check", [
    (None, "application/json", lambda b: json.loads(b) == {"text": "hi"}),
    ("json", "application/json", lambda b: json.loads(b) == {"text": "hi"}),
    ("text", "text/plain", lambda b: b == "hi"),
    ("verbose_json", "application/json", lambda b: json.loads(b)["duration"] == 2.0),
    ("srt", "text/plain", lambda b: "00:00:02,000" in b),
    ("vtt", "text/vtt", lambda b: b.startswith("WEBVTT")),
])
def test_format_response(fmt, media, check):
    body, mt = format_response("hi", fmt, 2.0, None)
    assert mt.startswith(media) and check(body)


def _client(cfg, handler):
    backend = OpenRouterBackend(cfg, "test-key", transport=httpx.MockTransport(handler))
    return TestClient(create_app(cfg, backends={"openrouter": backend}), base_url="http://127.0.0.1:18765")


def test_end_to_end_success(cfg):
    seen = {}

    def handler(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        seen["headers"] = req.headers
        return httpx.Response(200, json={"text": " Hello world. ", "usage": {"seconds": 1, "cost": 0.001}})

    with _client(cfg, handler) as c:
        r = c.post("/v1/audio/transcriptions", data={"model": "mai-clean", "response_format": "json"},
                   files={"file": ("a.wav", make_wav(1.0), "audio/wav")})
        assert r.status_code == 200 and r.json() == {"text": "Hello world."}
        assert seen["body"]["model"] == "microsoft/mai-transcribe-2"
        assert seen["headers"]["authorization"] == "Bearer test-key"
        assert seen["headers"]["x-openrouter-title"] == "Dictation Proxy"
        row = c.app.state.db._conn.execute(
            "SELECT route, backend, audio_duration_s, cost, status, transcript FROM requests").fetchone()
        assert row == ("mai-clean", "openrouter", 1.0, 0.001, "ok", "Hello world.")

        # unknown model name falls back to default_route
        c.post("/v1/audio/transcriptions", data={"model": "whisper-1"},
               files={"file": ("a.wav", make_wav(), "audio/wav")})
        assert seen["body"]["model"] == "microsoft/mai-transcribe-2"
        assert c.get("/v1/models").json()["data"][0]["id"] == "mai-clean"
        assert c.get("/health").json()["status"] == "ok"


def test_upstream_error_and_timeout(cfg):
    def err(req):
        return httpx.Response(400, json={"error": {"message": "bad option"}})

    with _client(cfg, err) as c:
        r = c.post("/v1/audio/transcriptions", data={"model": "voxtral"},
                   files={"file": ("a.wav", make_wav(), "audio/wav")})
        assert r.status_code == 502
        assert "bad option" in r.json()["error"]["message"]
        detail = c.app.state.db._conn.execute("SELECT error FROM requests").fetchone()[0]
        assert "HTTP 400" in detail and "bad option" in detail

    def hang(req):
        raise httpx.ReadTimeout("timed out", request=req)

    with _client(cfg, hang) as c:
        r = c.post("/v1/audio/transcriptions", data={"model": "voxtral"},
                   files={"file": ("a.wav", make_wav(), "audio/wav")})
        assert r.status_code == 504 and r.json()["error"]["type"] == "timeout"
        row = c.app.state.db._conn.execute(
            "SELECT status, http_status FROM requests ORDER BY id DESC").fetchone()
        assert row == ("error", 504)

    def garbage(req):
        return httpx.Response(200, content=b"<html>oops</html>")

    with _client(cfg, garbage) as c:
        r = c.post("/v1/audio/transcriptions", data={"model": "voxtral"},
                   files={"file": ("a.wav", make_wav(), "audio/wav")})
        assert r.status_code == 502 and "unreadable" in r.json()["error"]["message"]


def test_request_errors_are_openai_shaped(cfg):
    cfg["routes"]["native-test"] = {"backend": "native"}
    with _client(cfg, lambda req: httpx.Response(500)) as c:
        r = c.post("/v1/audio/transcriptions", data={"model": "voxtral"})
        assert r.status_code == 400 and "missing 'file'" in r.json()["error"]["message"]
        r = c.post("/v1/audio/transcriptions", data={"model": "voxtral"},
                   files={"file": ("a.wav", b"", "audio/wav")})
        assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"
        r = c.post("/v1/audio/transcriptions", data={"model": "native-test"},
                   files={"file": ("a.wav", make_wav(), "audio/wav")})
        assert r.status_code == 501 and "native" in r.json()["error"]["message"]


def test_missing_api_key(cfg):
    backend = OpenRouterBackend(cfg, "", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with TestClient(create_app(cfg, backends={"openrouter": backend}), base_url="http://127.0.0.1:18765") as c:
        r = c.post("/v1/audio/transcriptions", files={"file": ("a.wav", make_wav(), "audio/wav")})
        assert r.status_code == 500 and "OPENROUTER_API_KEY" in r.json()["error"]["message"]


def test_generation_stats_backfilled(cfg):
    cfg["openrouter"]["generation_stats_delays_s"] = [0, 0.05]

    def handler(req):
        if req.url.path.endswith("/generation"):
            return httpx.Response(200, json={"data": {"latency": 612, "provider_name": "Mistral"}})
        return httpx.Response(200, json={"text": "hi"}, headers={"x-generation-id": "gen-1"})

    with _client(cfg, handler) as c:
        c.post("/v1/audio/transcriptions", files={"file": ("a.wav", make_wav(), "audio/wav")})
        q = "SELECT generation_id, or_latency_ms, or_provider FROM requests"
        for _ in range(50):
            row = c.app.state.db._conn.execute(q).fetchone()
            if row[1] is not None:
                break
            time.sleep(0.02)
        assert row == ("gen-1", 612, "Mistral")


def test_storage_migrates_old_schema(tmp_path):
    import sqlite3
    from proxy.storage import RequestLog
    p = tmp_path / "old.sqlite"
    sqlite3.connect(p).execute("CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL)")
    db = RequestLog(str(p))
    rid = db.insert({"ts": "x", "or_provider": "Azure"})
    db.update(rid, or_latency_ms=5)
    assert db._conn.execute("SELECT or_provider, or_latency_ms FROM requests").fetchone() == ("Azure", 5)
