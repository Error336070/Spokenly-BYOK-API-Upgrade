"""Failed-request clips in logging.failed_clips_dir, each with a JSON sidecar of route metadata and state.

The sidecar's status is "pending" until scripts/retry.py gets a nonempty transcript, then "recovered".
A clip whose bytes match one already in the directory reuses that file, so repeated failures of the same
audio (for example a failed retry) don't pile up copies. Identity is the content only, never a client path.
"""
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

_lock = threading.Lock()  # one proxy process; serializes the find-or-create and sidecar rewrites


def sidecar(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".json")


def read_sidecar(path: Path) -> dict | None:
    """The metadata, or None when the sidecar is missing, unreadable or has no route."""
    try:
        data = json.loads(sidecar(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("route"), str) else None


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_sidecar(path: Path, metadata: dict) -> None:
    _write_atomic(sidecar(path), json.dumps(metadata).encode("utf-8"))


def update_sidecar(path: Path, **changes) -> dict:
    with _lock:
        metadata = read_sidecar(path) or {}
        metadata.update(changes)
        write_sidecar(path, metadata)
        return metadata


def clips(directory: Path) -> list[Path]:
    """Audio files in the directory, newest first."""
    if not directory.is_dir():
        return []
    found = [p for p in directory.iterdir() if p.is_file() and p.suffix != ".json" and not p.name.startswith(".")]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def _same_content(directory: Path, audio: bytes, fmt: str) -> Path | None:
    for p in clips(directory):
        if p.suffix == f".{fmt}" and p.stat().st_size == len(audio) and p.read_bytes() == audio:
            return p
    return None


def save(directory: Path, audio: bytes, fmt: str, metadata: dict) -> Path:
    """Save a failed clip, or reuse the file already holding the same bytes. Returns its path."""
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        directory.mkdir(parents=True, exist_ok=True)
        existing = _same_content(directory, audio, fmt)
        if existing is not None:
            # keep the original route and state; count the failure and keep the clip from expiring
            old = read_sidecar(existing) or {**metadata, "status": "pending", "failures": 0}
            write_sidecar(existing, {**old, "failures": old.get("failures", 1) + 1, "last_failed_at": now})
            os.utime(existing)
            return existing
        path = directory / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.{fmt}"
        _write_atomic(path, audio)
        write_sidecar(path, {**metadata, "status": "pending", "failures": 1, "saved_at": now})
        return path
