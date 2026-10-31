"""SQLite request log, plus usage totals that outlive the retention period."""
import sqlite3
import threading
from datetime import datetime
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

# Retention deletes request rows (transcripts, clip paths). Before it does, their usage is added here,
# in the same transaction, so lifetime totals survive. Only counts and sums; no transcripts or audio.
_ROLLUP = """CREATE TABLE IF NOT EXISTS usage_rollup (
    backend TEXT NOT NULL DEFAULT '', provider_model TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
    requests INTEGER NOT NULL DEFAULT 0, audio_seconds REAL NOT NULL DEFAULT 0,
    missing_duration INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0, cost_count INTEGER NOT NULL DEFAULT 0,
    estimated_cost REAL NOT NULL DEFAULT 0, first_ts TEXT, last_ts TEXT,
    PRIMARY KEY (backend, provider_model, status))"""
_ROLL_UP_BEFORE = """INSERT INTO usage_rollup (backend, provider_model, status, requests, audio_seconds, missing_duration,
                              cost, cost_count, estimated_cost, first_ts, last_ts)
    SELECT COALESCE(backend, ''), COALESCE(provider_model, ''), COALESCE(status, ''), COUNT(*),
           COALESCE(SUM(audio_duration_s), 0), SUM(audio_duration_s IS NULL), COALESCE(SUM(cost), 0), COUNT(cost),
           COALESCE(SUM(estimated_cost), 0), MIN(ts), MAX(ts)
    FROM requests WHERE ts < ? GROUP BY 1, 2, 3
    ON CONFLICT (backend, provider_model, status) DO UPDATE SET
        requests = requests + excluded.requests, audio_seconds = audio_seconds + excluded.audio_seconds,
        missing_duration = missing_duration + excluded.missing_duration, cost = cost + excluded.cost,
        cost_count = cost_count + excluded.cost_count, estimated_cost = estimated_cost + excluded.estimated_cost,
        first_ts = MIN(COALESCE(first_ts, excluded.first_ts), excluded.first_ts),
        last_ts = MAX(COALESCE(last_ts, excluded.last_ts), excluded.last_ts)"""
_TOTALS = """SELECT backend, provider_model, status, SUM(requests) AS requests, SUM(audio_seconds) AS audio_seconds,
           SUM(missing_duration) AS missing_duration, SUM(cost) AS cost, SUM(cost_count) AS cost_count,
           SUM(estimated_cost) AS estimated_cost
    FROM (SELECT backend, provider_model, status, requests, audio_seconds, missing_duration, cost, cost_count,
                 estimated_cost FROM usage_rollup
          UNION ALL
          SELECT COALESCE(backend, ''), COALESCE(provider_model, ''), COALESCE(status, ''), 1,
                 COALESCE(audio_duration_s, 0), audio_duration_s IS NULL, COALESCE(cost, 0), cost IS NOT NULL,
                 COALESCE(estimated_cost, 0) FROM requests)
    GROUP BY backend, provider_model, status ORDER BY backend, provider_model, status"""


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
            self._conn.execute(_ROLLUP)
            self._conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            # Set once: the oldest row still present when totals started. Older deleted history is not recoverable.
            self._conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('usage_history_from', "
                               "COALESCE((SELECT MIN(ts) FROM requests), ?))",
                               (datetime.now().isoformat(timespec="milliseconds"),))
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
        """Add the rows' usage to usage_rollup and delete them in one transaction, so each counts once."""
        with self._lock:
            conn = self._db()
            with conn:  # commits both statements, or rolls both back
                conn.execute(_ROLL_UP_BEFORE, (ts,))
                return conn.execute("DELETE FROM requests WHERE ts < ?", (ts,)).rowcount

    def rows_since(self, ts: str = "") -> list[dict]:
        with self._lock:
            cur = self._db().execute("SELECT * FROM requests WHERE ts >= ? ORDER BY id", (ts,))
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def usage_totals(self) -> list[dict]:
        """Lifetime usage per (backend, provider_model, status): rolled-up deleted rows plus retained rows."""
        with self._lock:
            cur = self._db().execute(_TOTALS)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._db().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        """Store a value; None deletes the key."""
        with self._lock:
            conn = self._db()
            with conn:
                if value is None:
                    conn.execute("DELETE FROM meta WHERE key = ?", (key,))
                else:
                    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def update(self, row_id: int, **values) -> None:
        sets = ", ".join(f"{k} = ?" for k in values)
        with self._lock:
            conn = self._db()
            conn.execute(f"UPDATE requests SET {sets} WHERE id = ?", [*values.values(), row_id])
            conn.commit()
