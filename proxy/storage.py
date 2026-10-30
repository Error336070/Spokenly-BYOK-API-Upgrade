"""SQLite request log."""
import sqlite3
import threading
from pathlib import Path

# (name, type); new columns are added to existing databases on startup.
_COLUMNS = [
    ("ts", "TEXT NOT NULL"),
    ("requested_model", "TEXT"),
    ("route", "TEXT"),
    ("backend", "TEXT"),
    ("provider_model", "TEXT"),
    ("audio_format", "TEXT"),
    ("audio_duration_s", "REAL"),
    ("upload_bytes", "INTEGER"),
    ("upload_read_ms", "REAL"),        # receiving the upload from Spokenly
    ("sent_format", "TEXT"),           # format actually sent to the provider (flac when re-encoded)
    ("sent_bytes", "INTEGER"),
    ("encode_ms", "REAL"),             # re-encoding time (0 when sent as received)
    ("provider_latency_ms", "REAL"),   # our request to the provider, upload + processing + response
    ("or_latency_ms", "REAL"),         # OpenRouter-reported latency (filled in shortly after the request)
    ("or_provider", "TEXT"),           # provider OpenRouter actually routed to
    ("total_latency_ms", "REAL"),      # whole request inside the proxy
    ("new_connection", "INTEGER"),    # NULL when the transport exposes no trace events
    ("cost", "REAL"),
    ("generation_id", "TEXT"),
    ("status", "TEXT"),
    ("http_status", "INTEGER"),
    ("error", "TEXT"),
    ("transcript", "TEXT"),
    ("clip_path", "TEXT"),
    ("details", "TEXT"),
]


class RequestLog:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        cols = ", ".join(f"{n} {t}" for n, t in _COLUMNS)
        self._conn.execute(f"CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY AUTOINCREMENT, {cols})")
        existing = {r[1] for r in self._conn.execute("PRAGMA table_info(requests)")}
        for name, typ in _COLUMNS:
            if name not in existing:
                self._conn.execute(f"ALTER TABLE requests ADD COLUMN {name} {typ.replace(' NOT NULL', '')}")
        self._conn.commit()
        self._lock = threading.Lock()

    def insert(self, row: dict) -> int:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self._lock:
            cur = self._conn.execute(f"INSERT INTO requests ({cols}) VALUES ({marks})", list(row.values()))
            self._conn.commit()
            return cur.lastrowid

    def delete_before(self, ts: str) -> int:
        with self._lock:
            n = self._conn.execute("DELETE FROM requests WHERE ts < ?", (ts,)).rowcount
            self._conn.commit()
            return n

    def update(self, row_id: int, **values) -> None:
        sets = ", ".join(f"{k} = ?" for k in values)
        with self._lock:
            self._conn.execute(f"UPDATE requests SET {sets} WHERE id = ?", [*values.values(), row_id])
            self._conn.commit()
