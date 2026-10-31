import json
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import failed
from proxy.app import create_app
from proxy.backends import OpenRouterBackend
from scripts import retry
from tests.test_translate import cfg, make_wav  # noqa: F401


def saved_clip(tmp_path, route="gpt", audio=None):
    return failed.save(tmp_path / "failed", audio or make_wav(), "wav", {"route": route, "language": "en"})


def mock_client(handler):
    return httpx.Client(base_url="http://127.0.0.1:18765", transport=httpx.MockTransport(handler))


def test_save_reuses_file_with_same_content(tmp_path):
    first = saved_clip(tmp_path)
    again = saved_clip(tmp_path)
    other = saved_clip(tmp_path, audio=make_wav(2))
    assert again == first and other != first
    meta = failed.read_sidecar(first)
    assert meta["route"] == "gpt" and meta["failures"] == 2 and meta["status"] == "pending"
    assert len(failed.clips(tmp_path / "failed")) == 2


def test_concurrent_saves_of_same_audio_make_one_file(tmp_path):
    audio = make_wav(1.5)
    threads = [threading.Thread(target=saved_clip, args=(tmp_path, "gpt", audio)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    [path] = failed.clips(tmp_path / "failed")
    assert failed.read_sidecar(path)["failures"] == 8
    assert not [p for p in (tmp_path / "failed").iterdir() if p.name.endswith(".tmp")]


def test_success_marks_recovered_and_keeps_audio(cfg, tmp_path):  # noqa: F811
    path = saved_clip(tmp_path)
    seen = []
    with mock_client(lambda r: seen.append(r.content) or httpx.Response(200, json={"text": "Zyntrabox"})) as c:
        assert retry.retry_clip(path, cfg, c) == "Zyntrabox"
    assert b'name="model"\r\n\r\ngpt' in seen[0] and path.read_bytes() in seen[0]
    assert path.exists()
    meta = failed.read_sidecar(path)
    assert meta["status"] == "recovered" and meta["retries"] == 1 and meta["route"] == "gpt"
    assert retry.pending(tmp_path / "failed") == []


@pytest.mark.parametrize("response,needle", [
    (httpx.Response(502, json={"error": {"message": "Zyntrabox upstream down"}}), "HTTP 502: Zyntrabox upstream down"),
    (httpx.Response(200, json={"text": "  "}), "empty transcript again"),
])
def test_failed_or_empty_retry_stays_pending(cfg, tmp_path, response, needle):  # noqa: F811
    path = saved_clip(tmp_path)
    with mock_client(lambda r: response) as c:
        with pytest.raises(retry.RetryError, match=needle):
            retry.retry_clip(path, cfg, c)
    meta = failed.read_sidecar(path)
    assert meta["status"] == "pending" and meta["retries"] == 1
    assert retry.pending(tmp_path / "failed")[0][0] == path


def test_removed_route_and_bad_metadata_are_rejected(cfg, tmp_path):  # noqa: F811
    path = saved_clip(tmp_path, route="Zyntrabox-removed")
    calls = []
    with mock_client(lambda r: calls.append(r) or httpx.Response(200, json={"text": "x"})) as c:
        with pytest.raises(retry.RetryError, match="no longer exists"):
            retry.retry_clip(path, cfg, c)
        failed.sidecar(path).write_text("{not json", encoding="utf-8")
        with pytest.raises(retry.RetryError, match="no readable .json metadata"):
            retry.retry_clip(path, cfg, c)
        failed.sidecar(path).unlink()
        with pytest.raises(retry.RetryError, match="no readable .json metadata"):
            retry.retry_clip(path, cfg, c)
    assert calls == []


def test_unreachable_proxy_is_a_short_error(cfg, tmp_path):  # noqa: F811
    path = saved_clip(tmp_path)

    def refuse(request):
        raise httpx.ConnectError("refused")

    with mock_client(refuse) as c:
        with pytest.raises(retry.RetryError, match="could not reach the proxy"):
            retry.retry_clip(path, cfg, c)


def test_failed_retry_through_proxy_does_not_duplicate_clip(cfg, tmp_path):  # noqa: F811
    cfg["notifications"]["enabled"] = False
    backend = OpenRouterBackend(cfg, "k", transport=httpx.MockTransport(lambda r: httpx.Response(502)))
    with TestClient(create_app(cfg, backends={"openrouter": backend}), base_url="http://127.0.0.1:18765") as c:
        c.post("/v1/audio/transcriptions", data={"model": "gpt"}, files={"file": ("x.wav", make_wav(3))})
        [path] = failed.clips(Path(cfg["logging"]["failed_clips_dir"]))
        for _ in range(2):
            with pytest.raises(retry.RetryError, match="HTTP 502"):
                retry.retry_clip(path, cfg, c)
        assert failed.clips(Path(cfg["logging"]["failed_clips_dir"])) == [path]
        meta = failed.read_sidecar(path)
        assert meta["failures"] == 3 and meta["retries"] == 2 and meta["route"] == "gpt"
        assert meta["last_retry_error"] == "HTTP 502"
        # a client cannot point the proxy at another file: a different upload is a new clip
        c.post("/v1/audio/transcriptions", data={"model": "gpt", "file_path": str(path)},
               files={"file": ("x.wav", make_wav(4))})
        assert len(failed.clips(Path(cfg["logging"]["failed_clips_dir"]))) == 2
        assert path.read_bytes() == make_wav(3)


def test_cli_list_and_exit_codes(cfg, tmp_path, monkeypatch, capsys):  # noqa: F811
    cfg["logging"]["failed_clips_dir"] = str(tmp_path / "failed")
    monkeypatch.setattr(retry, "load_config", lambda: cfg)
    assert retry.main([]) == 1
    assert "no pending failed clips" in capsys.readouterr().err
    good = saved_clip(tmp_path)
    broken = saved_clip(tmp_path, audio=make_wav(2))
    failed.sidecar(broken).write_text("[]", encoding="utf-8")
    assert retry.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert f"{good.name}" in out and "route gpt" in out and "metadata missing or unreadable" in out
    assert "2 pending, 0 recovered" in out

    class Client(httpx.Client):
        def __init__(self, **kw):
            kw.pop("timeout", None)
            super().__init__(transport=httpx.MockTransport(
                lambda r: httpx.Response(502, json={"error": {"message": "Zyntrabox down"}})), **kw)

    monkeypatch.setattr(retry.httpx, "Client", Client)
    assert retry.main([str(good)]) == 1
    err = capsys.readouterr().err
    assert "retry failed for" in err and "HTTP 502: Zyntrabox down" in err and "Traceback" not in err
    failed.update_sidecar(good, status="recovered")
    assert retry.main(["--list"]) == 0
    assert "1 pending, 1 recovered" in capsys.readouterr().out


def test_sidecar_is_valid_json_after_updates(tmp_path):
    path = saved_clip(tmp_path)
    for i in range(5):
        failed.update_sidecar(path, retries=i)
    assert json.loads(failed.sidecar(path).read_text(encoding="utf-8"))["retries"] == 4


def test_default_selection_skips_unreadable_metadata(cfg, tmp_path, monkeypatch, capsys):  # noqa: F811
    import os
    cfg["logging"]["failed_clips_dir"] = str(tmp_path / "failed")
    monkeypatch.setattr(retry, "load_config", lambda: cfg)
    good = saved_clip(tmp_path)
    broken = saved_clip(tmp_path, audio=make_wav(2))
    os.utime(good, (1, 1))  # broken is the newest
    failed.sidecar(broken).unlink()
    sent = []

    class Client(httpx.Client):
        def __init__(self, **kw):
            kw.pop("timeout", None)
            super().__init__(transport=httpx.MockTransport(
                lambda r: sent.append(r.content) or httpx.Response(200, json={"text": "Zyntrabox"})), **kw)

    monkeypatch.setattr(retry.httpx, "Client", Client)
    assert retry.main([]) == 0
    assert capsys.readouterr().out.strip() == "Zyntrabox"
    assert good.read_bytes() in sent[0]
    assert retry.main([]) == 1
    assert "no pending failed clips with readable metadata" in capsys.readouterr().err


def test_cli_uses_the_configured_ipv6_host(cfg, tmp_path, monkeypatch, capsys):  # noqa: F811
    cfg["server"].update(host="::1", port=18765)
    cfg["logging"]["failed_clips_dir"] = str(tmp_path / "failed")
    monkeypatch.setattr(retry, "load_config", lambda: cfg)
    saved_clip(tmp_path)
    seen = {}

    class Client:
        def __init__(self, *, base_url, **kwargs):
            seen["base_url"] = base_url

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return httpx.Response(200, json={"text": "Zyntrabox"})

    monkeypatch.setattr(retry.httpx, "Client", Client)
    assert retry.main([]) == 0
    assert capsys.readouterr().out.strip() == "Zyntrabox"
    assert seen["base_url"] == "http://[::1]:18765"


def test_cli_ipv6_bypasses_environment_proxy(cfg, tmp_path, monkeypatch, capsys):  # noqa: F811
    cfg["server"].update(host="::1", port=18765)
    cfg["logging"]["failed_clips_dir"] = str(tmp_path / "failed")
    monkeypatch.setattr(retry, "load_config", lambda: cfg)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    path = saved_clip(tmp_path)
    connections = []

    def handle(transport, request):
        # Inspect httpx's actual transport selection, without opening a socket.
        connections.append(type(transport._pool).__name__)
        return httpx.Response(200, json={"text": "Zyntrabox"})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    assert retry.main([str(path)]) == 0
    assert capsys.readouterr().out.strip() == "Zyntrabox"
    assert connections == ["ConnectionPool"]
