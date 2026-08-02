from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path

from .models import Alarm, PtpSample

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS samples (
    timestamp_ns INTEGER PRIMARY KEY,
    offset_ns REAL NOT NULL,
    mean_path_delay_ns REAL,
    frequency_ppb REAL,
    port_state TEXT,
    master_clock_id TEXT,
    sequence_id INTEGER,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_time ON samples(timestamp_ns);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ns INTEGER NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alarms (
    code TEXT PRIMARY KEY,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    first_seen_ns INTEGER NOT NULL,
    last_seen_ns INTEGER NOT NULL,
    active INTEGER NOT NULL,
    acknowledged INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SampleStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def add_sample(self, sample: PtpSample) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO samples VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sample.timestamp_ns,
                    sample.offset_ns,
                    sample.mean_path_delay_ns,
                    sample.frequency_ppb,
                    sample.port_state,
                    sample.master_clock_id,
                    sample.sequence_id,
                    sample.source,
                ),
            )
            self._connection.commit()

    def add_event(self, timestamp_ns: int, kind: str, message: str) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO events(timestamp_ns, kind, message) VALUES (?, ?, ?)",
                (timestamp_ns, kind, message),
            )
            self._connection.commit()

    def save_alarm(self, alarm: Alarm) -> None:
        with self._lock:
            self._connection.execute(
                """INSERT OR REPLACE INTO alarms
                (code, severity, message, first_seen_ns, last_seen_ns, active, acknowledged)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    alarm.code,
                    alarm.severity,
                    alarm.message,
                    alarm.first_seen_ns,
                    alarm.last_seen_ns,
                    int(alarm.active),
                    int(alarm.acknowledged),
                ),
            )
            self._connection.commit()

    def load_samples(self, since_ns: int | None = None, limit: int = 20_000) -> list[PtpSample]:
        query = "SELECT * FROM samples"
        parameters: list[int] = []
        if since_ns is not None:
            query += " WHERE timestamp_ns >= ?"
            parameters.append(since_ns)
        query += " ORDER BY timestamp_ns DESC LIMIT ?"
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [PtpSample(*row) for row in reversed(rows)]

    def load_alarms(self) -> list[Alarm]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT code, severity, message, first_seen_ns, last_seen_ns,
                active, acknowledged FROM alarms ORDER BY last_seen_ns DESC"""
            ).fetchall()
        return [
            Alarm(code, severity, message, first_seen, last_seen, bool(active), bool(acknowledged))
            for code, severity, message, first_seen, last_seen, active, acknowledged in rows
        ]

    def save_setting(self, name: str, value: str) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO settings(name, value) VALUES (?, ?)", (name, value)
            )
            self._connection.commit()

    def load_setting(self, name: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM settings WHERE name = ?", (name,)
            ).fetchone()
        return str(row[0]) if row else None

    def export_rows(self, samples: Iterable[PtpSample]) -> Iterable[tuple[object, ...]]:
        for sample in samples:
            yield (
                sample.timestamp_ns,
                sample.offset_ns,
                sample.mean_path_delay_ns,
                sample.frequency_ppb,
                sample.port_state,
                sample.master_clock_id,
                sample.sequence_id,
                sample.source,
            )
