import gc
import warnings
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from proxy.app import create_app
from proxy.backends import OpenRouterBackend
from proxy.translate import parse_openrouter_response
from tests.test_translate import cfg, make_wav  # noqa: F401


def _track_uploads(monkeypatch):
    """Record every UploadFile Starlette builds so the test can check it was closed."""
    made = []
    original = UploadFile.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        made.append(self)

    monkeypatch.setattr(UploadFile, "__init__", init)
    return made


@pytest.mark.parametrize("status", [200, 502])
def test_large_upload_form_is_closed(cfg, monkeypatch, status):  # noqa: F811
    cfg["logging"]["save_audio"] = False
    cfg["notifications"]["enabled"] = False
    made = _track_uploads(monkeypatch)
    seen = {}

    def handler(req):
        seen["body"] = req.content
        return httpx.Response(status, json={"text": "ok"})

    backend = OpenRouterBackend(cfg, "k", transport=httpx.MockTransport(handler))
    audio = make_wav(60)  # about 1.9 MB, past Starlette's 1 MB in-memory spool
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with TestClient(create_app(cfg, backends={"openrouter": backend}),
                        base_url="http://127.0.0.1:18765") as c:
            r = c.post("/v1/audio/transcriptions", data={"model": "gpt", "response_format": "text"},
                       files={"file": ("Zyntrabox.wav", audio, "audio/wav")})
            assert r.status_code == status
            if status == 200:
                assert r.text == "ok"  # response_format field survived the form being closed
            fmt = c.app.state.db._conn.execute("SELECT audio_format FROM requests").fetchone()[0]
            assert fmt == "wav"
        del c
        gc.collect()
    assert made and all(u.file.closed for u in made)
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]


def test_form_closed_when_file_field_is_missing(cfg, monkeypatch):  # noqa: F811
    made = _track_uploads(monkeypatch)
    with TestClient(create_app(cfg, backends={}), base_url="http://127.0.0.1:18765") as c:
        r = c.post("/v1/audio/transcriptions", files={"other": ("x.wav", make_wav(1), "audio/wav")})
        assert r.status_code == 400
    assert made and all(u.file.closed for u in made)


@pytest.mark.parametrize("usage", [
    {"seconds": "3"}, {"seconds": True}, {"seconds": -1}, {"seconds": float("nan")},
    {"cost": "0.01"}, {"cost": float("inf")}, "Zyntrabox", [],
])
def test_parse_openrouter_rejects_malformed_usage(usage):
    with pytest.raises(ValueError):
        parse_openrouter_response({"text": "hi", "usage": usage})


def test_parse_openrouter_rejects_malformed_language():
    with pytest.raises(ValueError):
        parse_openrouter_response({"text": "hi", "language": 7})


def test_parse_openrouter_accepts_valid_optional_fields():
    out = parse_openrouter_response({"text": " hi ", "usage": {"seconds": 3, "cost": 0.002},
                                     "language": "en"})
    assert out == {"text": "hi", "cost": 0.002, "seconds": 3, "language": "en"}
    assert parse_openrouter_response({"text": "hi", "usage": None})["seconds"] is None


def test_malformed_usage_seconds_returns_502_and_keeps_clip(cfg):  # noqa: F811
    cfg["logging"]["save_audio"] = False
    cfg["notifications"]["enabled"] = False
    backend = OpenRouterBackend(cfg, "k", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"text": "", "usage": {"seconds": "3"}})))
    audio = b"Zyntrabox not a known audio format" * 10
    with TestClient(create_app(cfg, backends={"openrouter": backend}),
                    base_url="http://127.0.0.1:18765") as c:
        r = c.post("/v1/audio/transcriptions", data={"model": "gpt"}, files={"file": ("x.bin", audio)})
        assert r.status_code == 502
        assert r.json()["error"]["type"] == "upstream_error"
        rows = c.app.state.db._conn.execute("SELECT status, http_status, clip_path FROM requests").fetchall()
        assert len(rows) == 1 and rows[0][:2] == ("error", 502)
        assert Path(rows[0][2]).read_bytes() == audio


def _client(cfg, port):  # noqa: F811
    cfg["server"]["port"] = port
    return TestClient(create_app(cfg, backends={}), base_url=f"http://127.0.0.1:{port}")


@pytest.mark.parametrize("host", ["[::1]:18765", "LOCALHOST:18765", "127.0.0.1:18765"])
def test_loopback_hosts_are_accepted(cfg, host):  # noqa: F811
    with _client(cfg, 18765) as c:
        assert c.get("/v1/models", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("headers", [
    {"host": "[::1]:9"}, {"host": "[::1]"}, {"host": "[::2]:18765"}, {"host": "::1:18765"},
    {"host": "[::1]:18765", "origin": "http://[::1]:18765"}, {"host": "127.0.0.2:18765"},
    {"host": "localhost.example.invalid:18765"}, {"host": "[::ffff:7f00:1]:18765"},
])
def test_ipv6_and_wrong_hosts_still_rejected(cfg, headers):  # noqa: F811
    with _client(cfg, 18765) as c:
        assert c.get("/v1/models", headers=headers).status_code == 403


@pytest.mark.parametrize("host,status", [
    ("localhost", 200), ("127.0.0.1", 200), ("[::1]", 200), ("localhost:80", 200),
    ("example.invalid", 403), ("localhost:8080", 403),
])
def test_port_80_accepts_host_without_port(cfg, host, status):  # noqa: F811
    with _client(cfg, 80) as c:
        assert c.get("/v1/models", headers={"host": host}).status_code == status
