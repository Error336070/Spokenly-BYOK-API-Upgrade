"""Failed-request clips in logging.failed_clips_dir, each with a JSON sidecar of route metadata and state.

The sidecar's status is "pending" until scripts/retry.py gets a nonempty transcript, then "recovered".
Every saved failure increases the sidecar's "generation". A retry records the generation it read before sending,
and its result only changes the status if no newer failure has arrived since, so a slow successful retry cannot
hide a failure that happened while it was in flight. Sidecars written before this field count as generation 0.
A clip whose bytes, route and language match one already saved reuses that file, so failed retries don't
pile up copies or inherit a different route. No client-supplied path determines the saved file.
"""
import json
import os
import threading
import uuid
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path

_lock = threading.Lock()
LOCK_FILENAME = ".recovery.lock"


@contextmanager
def directory_lock(directory: Path):
    """Serialize recovery records across proxy threads, retry processes and retention."""
    with _lock:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / LOCK_FILENAME).open("a+b") as handle:
            if os.name == "nt":
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def sidecar(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".json")


def read_sidecar(path: Path) -> dict | None:
    """Read a recovery record without racing a writer or retention."""
    if not path.parent.is_dir():
        return None
    with directory_lock(path.parent):
        return _read_sidecar(path)


def _read_sidecar(path: Path) -> dict | None:
    """The metadata, or None when the sidecar is missing, unreadable or has no route."""
    try:
        data = json.loads(sidecar(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("route"), str) or not data["route"].strip():
        return None
    if data.get("status", "pending") not in ("pending", "recovered"):
        return None
    if data.get("language") is not None and not isinstance(data["language"], str):
        return None
    for key in ("failures", "retries", "generation"):
        if key in data and (type(data[key]) is not int or data[key] < 0):
            return None
    return data


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _write_sidecar(path: Path, metadata: dict) -> None:
    _write_atomic(sidecar(path), json.dumps(metadata).encode("utf-8"))


def update_sidecar(path: Path, **changes) -> dict:
    with directory_lock(path.parent):
        metadata = _read_sidecar(path)
        if metadata is None:
            raise ValueError("the clip metadata is missing or unreadable")
        metadata.update(changes)
        _write_sidecar(path, metadata)
        return metadata


def record_retry(path: Path, error: str | None, *, started_generation: int) -> dict:
    """Count a completed attempt and apply its outcome if it still describes the latest failure.

    started_generation is the sidecar's generation when the attempt read it. If a newer failure was saved since,
    the result is stale: it is counted, but it neither recovers the clip nor records its error. A failed attempt
    also does not reopen a generation that another retry has already recovered.
    """
    now = datetime.now().isoformat(timespec="seconds")
    with directory_lock(path.parent):
        metadata = _read_sidecar(path)
        if metadata is None:
            raise ValueError("the clip metadata is missing or unreadable")
        metadata.update(retries=metadata.get("retries", 0) + 1, last_retry_at=now)
        current = metadata.get("generation", 0) == started_generation
        if current and error is None:
            metadata.update(status="recovered", recovered_at=now)
            metadata.pop("last_retry_error", None)
        elif current and metadata.get("status") != "recovered":
            metadata.update(status="pending", last_retry_error=error)
            metadata.pop("recovered_at", None)
        os.utime(path)
        _write_sidecar(path, metadata)
        return metadata


def clips(directory: Path) -> list[Path]:
    """Audio files in the directory, newest first."""
    if not directory.is_dir():
        return []
    found = [p for p in directory.iterdir() if p.is_file() and p.suffix != ".json" and not p.name.startswith(".")]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def _same_content(directory: Path, audio: bytes, fmt: str, metadata: dict) -> Path | None:
    for p in clips(directory):
        old = _read_sidecar(p)
        if (old is not None and old["route"] == metadata["route"]
                and (old.get("language") or "") == (metadata.get("language") or "")
                and p.suffix == f".{fmt}" and p.stat().st_size == len(audio) and p.read_bytes() == audio):
            return p
    return None


def save(directory: Path, audio: bytes, fmt: str, metadata: dict) -> Path:
    """Save a failed clip, or reuse the file already holding the same bytes. Returns its path."""
    now = datetime.now().isoformat(timespec="seconds")
    with directory_lock(directory):
        existing = _same_content(directory, audio, fmt, metadata)
        if existing is not None:
            old = _read_sidecar(existing)
            old.update(status="pending", failures=old.get("failures", 1) + 1, last_failed_at=now,
                       generation=old.get("generation", 0) + 1)
            old.pop("recovered_at", None)
            _write_sidecar(existing, old)
            os.utime(existing)
            return existing
        path = directory / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.{fmt}"
        _write_atomic(path, audio)
        _write_sidecar(path, {**metadata, "status": "pending", "failures": 1, "generation": 1,
                                  "saved_at": now})
        return path
