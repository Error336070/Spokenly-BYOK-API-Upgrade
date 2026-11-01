import asyncio
import sqlite3
from argparse import Namespace
from pathlib import Path

import pytest

from proxy.storage import RequestLog
from scripts import shootout, stats, vocab
from tests.test_shootout import shoot  # noqa: F401
from tests.test_translate import cfg  # noqa: F401


def test_stats_sorts_concurrent_requests_by_start(tmp_path, capsys):
    db = RequestLog(str(tmp_path / "requests.sqlite"))
    try:
        for second in (10, 0, 20):  # completion order differs from start order
            db.insert({"ts": f"2026-01-01T00:00:{second:02}", "route": "gpt", "backend": "openrouter",
                       "provider_model": "test", "status": "ok"})
        stats.report(db.rows_since(), {}, None, 0)
    finally:
        db.close()
    lines = [line for line in capsys.readouterr().out.splitlines() if "new TCP" in line]
    assert "00:00:00" in lines[0] and "gap      -" in lines[0]
    assert "gap    10s" in lines[1] and "gap    10s" in lines[2]


def test_vocab_before_first_request_is_empty_without_creating_database(tmp_path, monkeypatch):
    path = tmp_path / "missing" / "requests.sqlite"
    monkeypatch.setattr(vocab, "load_config", lambda: {"logging": {"db_path": str(path)}})
    assert vocab.transcripts() == []
    assert not path.exists()


def test_vocab_reads_database_with_uri_characters(tmp_path, monkeypatch):
    path = tmp_path / "Zyntrabox #1.sqlite"
    db = RequestLog(str(path))
    try:
        db.insert({"ts": "2026-01-01", "status": "ok", "transcript": "Zyntrabox"})
    finally:
        db.close()
    monkeypatch.setattr(vocab, "load_config", lambda: {"logging": {"db_path": str(path)}})
    assert vocab.transcripts() == ["Zyntrabox"]


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
