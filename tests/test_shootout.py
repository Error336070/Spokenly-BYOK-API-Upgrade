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
from tests.helpers import SCRIPTS, make_wav
from datetime import datetime
import json
from types import SimpleNamespace

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


def test_flac_duration_scales_deadline_and_saves_long_empty(cfg):
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
def test_unsupported_or_malformed_formats_keep_unknown_duration(cfg, name, audio):
    timeouts = []
    backend = OpenRouterBackend(cfg, "k", httpx.MockTransport(
        lambda r: timeouts.append(r.extensions["timeout"]["read"]) or httpx.Response(200, json={"text": "x"})))
    with TestClient(create_app(cfg, backends={"openrouter": backend}), base_url="http://127.0.0.1:18765") as c:
        r = c.post("/v1/audio/transcriptions", data={"model": "gpt"}, files={"file": (name, audio)})
        assert r.status_code == 200
        assert c.app.state.db._conn.execute("SELECT audio_duration_s FROM requests").fetchone() == (None,)
    assert timeouts == [15]


def _import_database(shoot, paths):
    db = sqlite3.connect(shoot["logging"]["db_path"])
    with db:
        db.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, status TEXT, clip_path TEXT, transcript TEXT)")
        db.executemany("INSERT INTO requests (status, clip_path) VALUES ('ok', ?)", [(str(p),) for p in paths])
    db.close()


def test_import_missing_source_preserves_existing_clip(shoot, tmp_path):
    shootout.CLIPS.mkdir(parents=True)
    old = shootout.CLIPS / "mid1.wav"
    old.write_bytes(b"original")
    _import_database(shoot, [tmp_path / "missing.wav"])
    with pytest.raises(SystemExit, match="missing"):
        shootout.cmd_import(Namespace(ids=["mid1"], skip=0))
    assert old.read_bytes() == b"original"


def test_import_staging_failure_preserves_existing_clip(shoot, tmp_path, monkeypatch):
    shootout.CLIPS.mkdir(parents=True)
    old = shootout.CLIPS / "mid1.wav"
    old.write_bytes(b"original")
    source = tmp_path / "new.wav"
    source.write_bytes(b"replacement")
    _import_database(shoot, [source])

    def fail_copy(*args):
        raise OSError("Zyntrabox full disk")

    monkeypatch.setattr(shootout.shutil, "copy2", fail_copy)
    with pytest.raises(OSError, match="full disk"):
        shootout.cmd_import(Namespace(ids=["mid1"], skip=0))
    assert old.read_bytes() == b"original"


def test_import_only_replaces_audio(shoot, tmp_path):
    shootout.CLIPS.mkdir(parents=True)
    old = shootout.CLIPS / "mid1.mp3"
    old.write_bytes(b"old audio")
    notes = shootout.CLIPS / "mid1.txt"
    notes.write_text("Zyntrabox notes", encoding="utf-8")
    source = tmp_path / "new.wav"
    source.write_bytes(b"new audio")
    _import_database(shoot, [source])
    shootout.cmd_import(Namespace(ids=["mid1"], skip=0))
    assert not old.exists()
    assert (shootout.CLIPS / "mid1.wav").read_bytes() == b"new audio"
    assert notes.read_text(encoding="utf-8") == "Zyntrabox notes"


@pytest.mark.parametrize("ids", [["unknown"], ["mid1", "mid1"], ["../outside"]])
def test_import_rejects_bad_ids_before_reading_database(shoot, ids):
    with pytest.raises(SystemExit):
        shootout.cmd_import(Namespace(ids=ids, skip=0))
    assert not Path(shoot["logging"]["db_path"]).exists()


@pytest.mark.parametrize("overrides", [{"routes": ["typo"]}, {"repeat": 0}, {"backend": "typo"},
                                      {"routes": ["gpt"], "backend": "native"}, {"paths": []}])
def test_run_rejects_bad_selection_before_building_clients(shoot, tmp_path, monkeypatch, overrides):
    source = tmp_path / "clip.wav"
    source.write_bytes(b"audio")
    monkeypatch.setattr(shootout, "build_backends", lambda *args: pytest.fail("built clients for invalid input"))
    args = Namespace(**({"paths": [str(source)], "routes": ["assembly"], "repeat": 1, "backend": None} | overrides))
    with pytest.raises(SystemExit):
        asyncio.run(shootout.cmd_run(args))


def test_run_warms_only_selected_backend(shoot, tmp_path, monkeypatch):
    source = tmp_path / "clip.wav"
    source.write_bytes(b"audio")
    warmed = []

    class Backend:
        def __init__(self, name):
            self.name = name

        async def warmup(self):
            warmed.append(self.name)

        async def transcribe(self, *args, **kwargs):
            return {"text": "Zyntrabox", "provider_latency_ms": 1, "provider_model": "test"}

        async def aclose(self):
            pass

    monkeypatch.setattr(shootout, "build_backends", lambda *args: {n: Backend(n) for n in ("native", "openrouter")})
    asyncio.run(shootout.cmd_run(Namespace(paths=[str(source)], routes=["gpt"], backend=None, repeat=1)))
    assert warmed == ["openrouter"]


def _shootout_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(shootout, "SHOOTOUT", tmp_path)
    monkeypatch.setattr(shootout, "audio_info", lambda *a: None)
    monkeypatch.setattr(shootout, "prepare_upload", lambda *a: (b"synthetic audio", "wav"))
    clip = tmp_path / "synthetic.wav"
    clip.write_bytes(b"synthetic audio")
    return clip, {"synthetic": {"backend": "openrouter"}}


@pytest.mark.parametrize("exception,status", [(asyncio.CancelledError, "interrupted"), (RuntimeError, "failed")])
def test_shootout_keeps_completed_requests_when_run_is_interrupted(tmp_path, monkeypatch, exception, status):
    clip, routes = _shootout_inputs(tmp_path, monkeypatch)

    class Backend:
        calls = 0

        async def warmup(self):
            pass

        async def transcribe(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                output = next((tmp_path / "results").glob("*.csv"))
                with output.open(encoding="utf-8", newline="") as f:
                    assert list(csv.DictReader(f))[0]["text"] == "Zyntrabox"
                raise exception()
            return {"text": "Zyntrabox", "provider_latency_ms": 7, "provider_model": "synthetic", "cost": 0.01}

    with pytest.raises(exception):
        asyncio.run(shootout.run_clips(SimpleNamespace(repeat=2), {}, {}, {"openrouter": Backend()}, routes, [clip]))
    output = next((tmp_path / "results").glob("*.csv"))
    with output.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1 and rows[0]["cost"] == "0.01" and rows[0]["rep"] == "0"
    state = json.loads(output.with_suffix(".json").read_text())
    assert state["status"] == status and state["completed_requests"] == 1


def test_completed_shootouts_do_not_overwrite_same_second_runs(tmp_path, monkeypatch):
    clip, routes = _shootout_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(shootout, "datetime", SimpleNamespace(now=lambda: datetime(2026, 1, 1)))

    class Backend:
        async def warmup(self):
            pass

        async def transcribe(self, *a, **kw):
            return {"text": "Zyntrabox", "provider_latency_ms": 1, "provider_model": "synthetic"}

    for _ in range(2):
        asyncio.run(shootout.run_clips(SimpleNamespace(repeat=1), {}, {}, {"openrouter": Backend()}, routes, [clip]))
    outputs = list((tmp_path / "results").glob("*.csv"))
    assert len(outputs) == 2
    for output in outputs:
        state = json.loads(output.with_suffix(".json").read_text())
        assert state["status"] == "completed" and state["completed_requests"] == 1


def test_invalid_reference_stops_shootout_before_provider_calls(tmp_path, monkeypatch):
    clip, routes = _shootout_inputs(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="reference must contain word tokens"):
        asyncio.run(shootout.run_clips(SimpleNamespace(repeat=1), {}, {"synthetic": {"expected": "😀"}},
                                      {"openrouter": object()}, routes, [clip]))
    assert not (tmp_path / "results").exists()
