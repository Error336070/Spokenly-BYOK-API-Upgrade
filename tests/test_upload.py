import io
import json

import httpx
import soundfile
import numpy as np
import pytest
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.audio import prepare_upload, to_flac
from proxy.backends import OpenRouterBackend
from tests.test_native import _parts
from tests.test_translate import cfg, make_wav  # noqa: F401  (fixture)


def test_flac_is_lossless_and_smaller():
    signal = (10000 * np.sin(np.arange(32000) * 0.07)).astype("int16")
    source = io.BytesIO()
    soundfile.write(source, signal, 16000, format="WAV", subtype="PCM_16")
    wav = source.getvalue()
    flac = to_flac(wav)
    assert flac[:4] == b"fLaC" and len(flac) < len(wav)
    a, _ = soundfile.read(io.BytesIO(wav), dtype="int16")
    b, rate = soundfile.read(io.BytesIO(flac), dtype="int16")
    assert rate == 16000 and (a == b).all()


@pytest.mark.parametrize("subtype", ["FLOAT", "PCM_24"])
def test_non16bit_wav_preserves_signal(subtype):
    signal = 0.5 * np.sin(np.arange(16000) * 0.07)
    source = io.BytesIO()
    soundfile.write(source, signal, 16000, format="WAV", subtype=subtype)
    flac = to_flac(source.getvalue())
    decoded, rate = soundfile.read(io.BytesIO(flac))
    assert rate == 16000
    assert np.max(np.abs(decoded - signal)) < 1e-6


def test_prepare_upload_switch_and_fallback():
    wav = make_wav()
    assert prepare_upload(wav, "wav", {"upload": {"flac": True}})[1] == "flac"
    assert prepare_upload(wav, "wav", {"upload": {"flac": False}}) == (wav, "wav")
    assert prepare_upload(b"OggS...", "ogg", {})[1] == "ogg"           # only WAV is re-encoded
    assert prepare_upload(b"RIFFbroken", "wav", {}) == (b"RIFFbroken", "wav")  # encode failure: send as-is


def test_openrouter_multipart_flac_end_to_end(cfg):  # noqa: F811
    cfg["openrouter"]["multipart"] = True
    cfg["upload"] = {"flac": True}
    cfg["vocabulary"] = ["Zyntrabox"]
    seen = {}

    def handler(req):
        seen["req"] = req
        return httpx.Response(200, json={"text": "ok"})

    backend = OpenRouterBackend(cfg, "k", transport=httpx.MockTransport(handler))
    with TestClient(create_app(cfg, backends={"openrouter": backend}), base_url="http://127.0.0.1:18765") as c:
        r = c.post("/v1/audio/transcriptions", data={"model": "mai-clean"},
                   files={"file": ("a.wav", make_wav(1.0), "audio/wav")})
        assert r.json() == {"text": "ok"}
        p = _parts(seen["req"])
        assert p["file"][0] == "audio/flac" and p["file"][1][:4] == b"fLaC"
        assert p["model"][1] == b"microsoft/mai-transcribe-2" and p["language"][1] == b"en"
        azure = json.loads(p["provider"][1])["options"]["azure"]
        assert azure["enhancedMode"]["modelOptions"]["transcribeStyle"] == "clean"
        assert azure["phraseList"]["phrases"] == ["Zyntrabox"]
        row = c.app.state.db._conn.execute(
            "SELECT audio_format, sent_format, upload_bytes > sent_bytes FROM requests").fetchone()
        assert row == ("wav", "flac", 1)
