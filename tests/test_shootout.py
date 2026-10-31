import asyncio
import csv
import io
import os
import sqlite3
from argparse import Namespace
from pathlib import Path

import httpx
import pytest
import soundfile
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.backends import NativeBackend, OpenRouterBackend
from proxy.scoring import score
from scripts import shootout
from tests.test_translate import cfg, make_wav  # noqa: F401

SCRIPTS = {"mid1": {"id": "mid1", "kind": "x", "read": "x", "expected": "Meet Wednesday at fifteen.",
                    "forbidden": ["next Tuesday", "MAI-Transcribe", "fifteen"]}}


@pytest.mark.parametrize("hypothesis,found", [
    ("Meet next Tuesday, no, Wednesday at 15.", ["next Tuesday", "fifteen"]),
    ("Switch to MAI Transcribe now.", ["MAI-Transcribe"]),
    ("Meet Wednesday at fifteen.", ["fifteen"]),
    ("Meet next week on Tuesday at 150.", []),           # words apart and a longer number don't match
    ("The Tuesdays next to MAI transcribers.", []),       # whole tokens only
])
def test_forbidden_phrases_and_numbers(hypothesis, found):
    assert score("x", hypothesis, forbidden=SCRIPTS["mid1"]["forbidden"])["forbidden_found"] == found


def test_empty_forbidden_phrase_never_matches():
    assert score("x", "anything", forbidden=["", "..."])["forbidden_found"] == []


@pytest.fixture
def shoot(cfg, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(shootout, "SHOOTOUT", tmp_path / "shootout")
    monkeypatch.setattr(shootout, "CLIPS", tmp_path / "shootout" / "clips")
    monkeypatch.setattr(shootout, "load_scripts", lambda: SCRIPTS)
    monkeypatch.setattr(shootout, "load_config", lambda: cfg)
    cfg["credit"] = {"enabled": False, "assemblyai_hourly_rate_usd": 0.36}
    return cfg


def results_csv(tmp_path):
    [out] = (tmp_path / "shootout" / "results").glob("*.csv")
    with open(out, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_run_reads_clip_once_and_records_duration(shoot, tmp_path, monkeypatch):
    clip = tmp_path / "mid1.wav"
    clip.write_bytes(make_wav(60.0))
    reads, seen = [], []
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: reads.append(self) or original(self))

    def handler(r):
        seen.append((r.url.path, r.extensions["timeout"]["read"] if "timeout" in r.extensions else None))
        if r.url.path.endswith("/v1/warm"):
            return httpx.Response(200)
        return httpx.Response(200, json={"text": "Meet next Tuesday at 15."})

    shoot["routes"]["assembly"]["backend"] = "openrouter"  # --backend native must still warm AssemblyAI
    built = {}

    def build(settings, env):
        built["native"] = NativeBackend(settings, {"assemblyai": "k"}, httpx.MockTransport(handler))
        built["openrouter"] = OpenRouterBackend(settings, "k", httpx.MockTransport(lambda r: httpx.Response(404)))
        return dict(built)

    monkeypatch.setattr(shootout, "build_backends", build)
    asyncio.run(shootout.cmd_run(Namespace(paths=[str(clip)], routes=["assembly"], backend="native", repeat=1)))
    assert reads.count(clip) == 1
    assert seen[0][0] == "/v1/warm"
    assert seen[1] == ("/v1/transcribe", pytest.approx(19))  # deadline scaled by the 60 s duration
    [row] = results_csv(tmp_path)
    assert row["audio_duration_s"] == "60.0" and float(row["estimated_cost"]) == pytest.approx(0.006)
    assert row["cost"] == "" and row["forbidden_found"] == "next Tuesday;fifteen"
    assert built["native"].clients["assemblyai"].is_closed


def test_run_closes_clients_when_a_backend_crashes(shoot, tmp_path, monkeypatch):
    clip = tmp_path / "mid1.wav"
    clip.write_bytes(make_wav())
    closed = []

    class Crashing:
        async def warmup(self):
            pass

        async def transcribe(self, *a, **kw):
            raise RuntimeError("Zyntrabox")

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(shootout, "build_backends", lambda settings, env: {"openrouter": Crashing()})
    with pytest.raises(RuntimeError):
        asyncio.run(shootout.cmd_run(Namespace(paths=[str(clip)], routes=["gpt"], backend=None, repeat=1)))
    assert closed == [True]


def test_rescore_reads_old_and_new_csv(shoot, tmp_path, capsys):
    path = tmp_path / "old.csv"
    path.write_text("clip,route,latency_ms,cost,error,text\n"
                    "mid1.wav,gpt,500,0.002,,Meet next Tuesday at 15.\n", encoding="utf-8")
    shootout.cmd_rescore(Namespace(csv=str(path)))
    out = capsys.readouterr().out
    assert "forbidden ['next Tuesday', 'fifteen']" in out and "0.00200" in out
    path.write_text("clip,route,latency_ms,cost,estimated_cost,error,text\n"
                    "mid1.wav,assembly,500,,0.001,,Meet Wednesday at 15.\n", encoding="utf-8")
    shootout.cmd_rescore(Namespace(csv=str(path)))
    assert "0.00100" in capsys.readouterr().out


def test_import_copies_clips_and_releases_database(shoot, tmp_path):
    db_path = shoot["logging"]["db_path"]
    clip = tmp_path / "saved.wav"
    clip.write_bytes(make_wav())
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, status TEXT, clip_path TEXT, transcript TEXT)")
    db.execute("INSERT INTO requests (status, clip_path, transcript) VALUES ('ok', ?, 'Zyntrabox')", (str(clip),))
    db.commit()
    db.close()
    shootout.cmd_import(Namespace(ids=["mid1"], skip=0))
    assert (tmp_path / "shootout" / "clips" / "mid1.wav").read_bytes() == clip.read_bytes()
    os.remove(db_path)  # the script closed its connection


def flac(seconds):
    out = io.BytesIO()
    soundfile.write(out, [0.0] * int(16000 * seconds), 16000, format="FLAC", subtype="PCM_16")
    return out.getvalue()


def test_flac_duration_scales_deadline_and_saves_long_empty(cfg):  # noqa: F811
    timeouts = []
    backend = OpenRouterBackend(cfg, "k", httpx.MockTransport(
        lambda r: timeouts.append(r.extensions["timeout"]["read"]) or httpx.Response(200, json={"text": ""})))
    audio = flac(120)
    with TestClient(create_app(cfg, backends={"openrouter": backend}), base_url="http://127.0.0.1:18765") as c:
        c.post("/v1/audio/transcriptions", data={"model": "gpt"}, files={"file": ("x.flac", audio)})
        fmt, duration, path = c.app.state.db._conn.execute(
            "SELECT audio_format, audio_duration_s, clip_path FROM requests").fetchone()
    assert (fmt, duration) == ("flac", 120)
    assert timeouts == [28]
    assert Path(path).read_bytes() == audio


@pytest.mark.parametrize("name,audio", [("x.m4a", b"\0\0\0\x20ftypM4A " + b"\0" * 40),
                                        ("x.flac", b"fLaC" + b"\0" * 40)])
def test_unsupported_or_malformed_formats_keep_unknown_duration(cfg, name, audio):  # noqa: F811
    timeouts = []
    backend = OpenRouterBackend(cfg, "k", httpx.MockTransport(
        lambda r: timeouts.append(r.extensions["timeout"]["read"]) or httpx.Response(200, json={"text": "x"})))
    with TestClient(create_app(cfg, backends={"openrouter": backend}), base_url="http://127.0.0.1:18765") as c:
        r = c.post("/v1/audio/transcriptions", data={"model": "gpt"}, files={"file": (name, audio)})
        assert r.status_code == 200
        assert c.app.state.db._conn.execute("SELECT audio_duration_s FROM requests").fetchone() == (None,)
    assert timeouts == [15]
