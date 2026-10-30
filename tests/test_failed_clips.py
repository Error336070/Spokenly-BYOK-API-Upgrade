import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.backends import OpenRouterBackend
from tests.test_translate import cfg, make_wav  # noqa: F401


@pytest.mark.parametrize("status,text", [(502, ""), (200, "")])
def test_failed_or_long_empty_clip_saved(cfg, tmp_path, status, text):
    cfg["logging"].update(save_audio=False, failed_clips_dir=str(tmp_path / "failed"))
    cfg["notifications"]["enabled"] = False
    backend = OpenRouterBackend(cfg, "k", transport=httpx.MockTransport(
        lambda r: httpx.Response(status, json={"text": text})))
    audio = make_wav(3)
    with TestClient(create_app(cfg, backends={"openrouter": backend})) as c:
        r = c.post("/v1/audio/transcriptions", data={"model": "gpt"}, files={"file": ("x.wav", audio)})
        assert r.status_code == status
        saved = Path(c.app.state.db._conn.execute("SELECT clip_path FROM requests").fetchone()[0])
        assert saved.read_bytes() == audio
        assert json.loads(saved.with_suffix(saved.suffix + ".json").read_text())["route"] == "gpt"
