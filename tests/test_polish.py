import os
import sqlite3
import time

import httpx
import yaml
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.backends import OpenRouterBackend
from proxy.config import ROOT, load_config
from proxy.notify import Notifier
from proxy.retention import cleanup
from proxy.storage import RequestLog
from tests.test_translate import cfg, make_wav  # noqa: F401  (fixture)


def test_notifier_upstream_cooldown_and_repeated():
    sent = []
    n = Notifier({"notifications": {"consecutive_errors": 3, "cooldown_s": 600}}, send=lambda t, b: sent.append(t))
    n.error("voxtral", "x", 402)
    n.error("voxtral", "x", 402)          # same kind within cooldown: suppressed
    assert sent == ["Dictation Proxy: OpenRouter credits exhausted"]
    n.success()
    n.error("voxtral", "boom")
    n.error("voxtral", "boom")
    assert len(sent) == 1
    n.error("voxtral", "boom")            # third in a row
    assert sent[-1] == "Dictation Proxy: 3 failed requests in a row"
    Notifier({"notifications": {"enabled": False}}, send=lambda t, b: sent.append(t)).error("r", "x", 401)
    assert len(sent) == 2


def test_native_credit_warning_says_how_to_switch_back():
    sent = []
    n = Notifier({"notifications": {}}, send=lambda t, b: sent.append((t, b)))
    n.error("assembly", "AssemblyAI HTTP 402: insufficient balance", 402, backend="native")
    title, body = sent[0]
    assert "assembly" in title and "backend: openrouter" in body and "insufficient balance" in body


def test_upstream_402_triggers_toast(cfg):  # noqa: F811
    sent = []
    backend = OpenRouterBackend(cfg, "k", transport=httpx.MockTransport(
        lambda r: httpx.Response(402, json={"error": {"message": "no credits"}})))
    app = create_app(cfg, backends={"openrouter": backend}, notifier=Notifier(cfg, send=lambda t, b: sent.append(t)))
    with TestClient(app, base_url="http://127.0.0.1:18765") as c:
        r = c.post("/v1/audio/transcriptions", files={"file": ("a.wav", make_wav(), "audio/wav")})
        assert r.status_code == 502 and "credits" in r.json()["error"]["message"]
    assert sent == ["Dictation Proxy: OpenRouter credits exhausted"]


def test_keep_warm_only_while_active(cfg):  # noqa: F811
    class FakeBackend:
        def __init__(self):
            self.warmups = 0

        async def transcribe(self, *a, **kw):
            return {"text": "hi", "provider_latency_ms": 1, "provider_model": "x"}

        async def warmup(self):
            self.warmups += 1

        async def aclose(self):
            pass

    b = FakeBackend()
    cfg["keep_warm"] = {"enabled": True, "interval_s": 0.05, "active_window_min": 30}
    with TestClient(create_app(cfg, backends={"openrouter": b}), base_url="http://127.0.0.1:18765") as c:
        time.sleep(0.2)
        assert b.warmups == 0                      # no dictation yet: stay quiet
        c.post("/v1/audio/transcriptions", files={"file": ("a.wav", make_wav(), "audio/wav")})
        time.sleep(0.3)
        assert b.warmups >= 2                      # active: pings
        cfg["keep_warm"]["active_window_min"] = 0  # window passed (hot-reload style change)
        time.sleep(0.1)
        n = b.warmups
        time.sleep(0.2)
        assert b.warmups == n                      # idle: pings stop


def test_vocabulary_file_merged_and_hot_reloaded(tmp_path):
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
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


def test_retention_deletes_old_clips_and_rows(tmp_path):
    clips = tmp_path / "clips"
    clips.mkdir()
    old, new = clips / "old.wav", clips / "new.wav"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    os.utime(old, (time.time() - 8 * 86400,) * 2)
    db = RequestLog(str(tmp_path / "db.sqlite"))
    db.insert({"ts": "2000-01-01T00:00:00.000"})
    db.insert({"ts": "2999-01-01T00:00:00.000"})
    cleanup({"logging": {"retention_days": 7, "clips_dir": str(clips)}}, db)
    assert not old.exists() and new.exists()
    assert db._conn.execute("SELECT ts FROM requests").fetchall() == [("2999-01-01T00:00:00.000",)]


def test_hot_reload_applies_valid_and_rejects_invalid(tmp_path):
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
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
