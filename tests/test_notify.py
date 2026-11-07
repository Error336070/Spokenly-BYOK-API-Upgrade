import httpx
from fastapi.testclient import TestClient
from proxy.app import create_app
from proxy.backends import OpenRouterBackend
from proxy.notify import Notifier
from tests.helpers import make_wav
"""test_notify"""


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


def test_upstream_402_triggers_toast(cfg):
    sent = []
    backend = OpenRouterBackend(cfg, "k", transport=httpx.MockTransport(
        lambda r: httpx.Response(402, json={"error": {"message": "no credits"}})))
    app = create_app(cfg, backends={"openrouter": backend}, notifier=Notifier(cfg, send=lambda t, b: sent.append(t)))
    with TestClient(app, base_url="http://127.0.0.1:18765") as c:
        r = c.post("/v1/audio/transcriptions", files={"file": ("a.wav", make_wav(), "audio/wav")})
        assert r.status_code == 502 and "credits" in r.json()["error"]["message"]
    assert sent == ["Dictation Proxy: OpenRouter credits exhausted"]
