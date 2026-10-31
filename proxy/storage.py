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
    ("cost", "REAL"),                  # billed cost the provider reported (OpenRouter usage.cost)
    ("estimated_cost", "REAL"),        # our estimate from duration x configured rate; never a billed figure
    ("provider_request_id", "TEXT"),   # native provider's request id (AssemblyAI session_id)
    ("provider_request_ms", "REAL"),   # request time the native provider reports (AssemblyAI request_time_ms)
    ("generation_id", "TEXT"),
    ("status", "TEXT"),
    ("http_status", "INTEGER"),
    ("error", "TEXT"),
    ("transcript", "TEXT"),
    ("clip_path", "TEXT"),
    ("details", "TEXT"),
]


class RequestLog:
    """One shared connection. Every call holds the lock, so close() waits for a running call to finish."""

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            cols = ", ".join(f"{n} {t}" for n, t in _COLUMNS)
            self._conn.execute(f"CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY AUTOINCREMENT, {cols})")
            existing = {r[1] for r in self._conn.execute("PRAGMA table_info(requests)")}
            for name, typ in _COLUMNS:
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE requests ADD COLUMN {name} {typ.replace(' NOT NULL', '')}")
            self._conn.commit()
        except BaseException:
            self._conn.close()
            raise

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise sqlite3.ProgrammingError("the request log is closed")
        return self._conn

    def close(self) -> None:
        """Close the connection once; later calls raise sqlite3.ProgrammingError."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def insert(self, row: dict) -> int:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self._lock:
            conn = self._db()
            cur = conn.execute(f"INSERT INTO requests ({cols}) VALUES ({marks})", list(row.values()))
            conn.commit()
            return cur.lastrowid

    def delete_before(self, ts: str) -> int:
        with self._lock:
            conn = self._db()
            n = conn.execute("DELETE FROM requests WHERE ts < ?", (ts,)).rowcount
            conn.commit()
            return n

    def update(self, row_id: int, **values) -> None:
        sets = ", ".join(f"{k} = ?" for k in values)
        with self._lock:
            conn = self._db()
            conn.execute(f"UPDATE requests SET {sets} WHERE id = ?", [*values.values(), row_id])
            conn.commit()
