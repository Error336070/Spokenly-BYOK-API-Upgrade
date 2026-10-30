import json
import io
import struct
from pathlib import Path

import httpx
import pytest
import soundfile
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.backends import OpenRouterBackend
from tests.test_translate import cfg, make_wav  # noqa: F401
from scripts.retry import retry_clip


@pytest.mark.parametrize("status,text", [(502, ""), (200, "")])
def test_failed_or_long_empty_clip_saved(cfg, tmp_path, status, text):
    cfg["logging"].update(save_audio=False, failed_clips_dir=str(tmp_path / "failed"))
    cfg["notifications"]["enabled"] = False
    backend = OpenRouterBackend(cfg, "k", transport=httpx.MockTransport(
        lambda r: httpx.Response(status, json={"text": text})))
    audio = make_wav(3)
    with TestClient(create_app(cfg, backends={"openrouter": backend}), base_url="http://127.0.0.1:18765") as c:
        r = c.post("/v1/audio/transcriptions", data={"model": "gpt"}, files={"file": ("x.wav", audio)})
        assert r.status_code == status
        saved = Path(c.app.state.db._conn.execute("SELECT clip_path FROM requests").fetchone()[0])
        assert saved.read_bytes() == audio
        assert json.loads(saved.with_suffix(saved.suffix + ".json").read_text())["route"] == "gpt"


def test_float_wav_duration_scales_deadline_and_saves_silence(cfg):
    source = io.BytesIO()
    soundfile.write(source, [0.2] * 1920000, 16000, format="WAV", subtype="FLOAT")
    timeouts = []
    backend = OpenRouterBackend(cfg, "k", httpx.MockTransport(
        lambda r: timeouts.append(r.extensions["timeout"]["read"]) or httpx.Response(200, json={"text": ""})))
    with TestClient(create_app(cfg, backends={"openrouter": backend}), base_url="http://127.0.0.1:18765") as c:
        c.post("/v1/audio/transcriptions", files={"file": ("x.wav", source.getvalue())})
        duration, path = c.app.state.db._conn.execute("SELECT audio_duration_s, clip_path FROM requests").fetchone()
        assert duration == 120
        assert Path(path).read_bytes() == source.getvalue()
    assert timeouts == [28]


def test_malformed_wav_metadata_preserves_failed_audio(cfg):
    payload = b"WAVEJUNK" + struct.pack("<I", 1000) + b"x"
    audio = b"RIFF" + struct.pack("<I", len(payload)) + payload
    backend = OpenRouterBackend(cfg, "k", httpx.MockTransport(lambda r: httpx.Response(502)))
    with TestClient(create_app(cfg, backends={"openrouter": backend}), base_url="http://127.0.0.1:18765") as c:
        response = c.post("/v1/audio/transcriptions", files={"file": ("x.wav", audio)})
        assert response.status_code == 502
        path = c.app.state.db._conn.execute("SELECT clip_path FROM requests").fetchone()[0]
        assert Path(path).read_bytes() == audio


def test_manual_retry_preserves_saved_route_and_audio(cfg, tmp_path):
    path = tmp_path / "Zyntrabox.wav"
    path.write_bytes(make_wav())
    path.with_suffix(".wav.json").write_text(json.dumps({"route": "gpt", "language": "en"}))
    seen = []
    with httpx.Client(base_url="http://127.0.0.1:18765", transport=httpx.MockTransport(
            lambda r: seen.append(r.content) or httpx.Response(200, json={"text": "Zyntrabox"}))) as client:
        assert retry_clip(path, cfg, client) == "Zyntrabox"
        assert b'name="model"\r\n\r\ngpt' in seen[0]
        assert path.read_bytes() in seen[0]
        del cfg["routes"]["gpt"]
        with pytest.raises(ValueError, match="saved route"):
            retry_clip(path, cfg, client)
    assert len(seen) == 1
