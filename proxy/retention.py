"""Delete saved clips and request-log rows older than logging.retention_days."""
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("proxy")


def cleanup(cfg: dict, db) -> None:
    days = cfg["logging"].get("retention_days", 7)
    if not days:
        return
    cutoff = time.time() - days * 86400
    removed = 0
    for directory in {cfg["logging"]["clips_dir"], cfg["logging"].get("failed_clips_dir", "data/failed")}:
        clips = Path(directory)
        if clips.is_dir():
            for f in clips.iterdir():
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    removed += 1
    rows = db.delete_before((datetime.now() - timedelta(days=days)).isoformat(timespec="milliseconds"))
    if removed or rows:
        log.info("retention: deleted %d clips and %d log rows older than %d days", removed, rows, days)
