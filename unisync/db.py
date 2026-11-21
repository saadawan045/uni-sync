"""Small SQLite store for state. Message *text* is never stored — only ids/hashes."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class DB:
    def __init__(self, data_dir: Path):
        self.conn = sqlite3.connect(data_dir / "state.db")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen (source TEXT, id TEXT, at REAL, PRIMARY KEY (source, id));
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            -- portal occurrences the user's chats/emails cancelled or moved
            CREATE TABLE IF NOT EXISTS overrides (event_id TEXT PRIMARY KEY, kind TEXT, reason TEXT, at REAL);
            """
        )
        self.conn.commit()
        from .paths import private
        private(data_dir / "state.db")

    # seen-message tracking
    def seen(self, source: str, id_: str) -> bool:
        return self.conn.execute("SELECT 1 FROM seen WHERE source=? AND id=?", (source, id_)).fetchone() is not None

    def mark_seen(self, source: str, id_: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO seen VALUES (?,?,?)", (source, id_, time.time()))
        self.conn.commit()

    def prune(self, days: int = 30) -> None:
        cutoff = time.time() - days * 86400
        self.conn.execute("DELETE FROM seen WHERE at < ?", (cutoff,))
        self.conn.execute("DELETE FROM overrides WHERE at < ?", (time.time() - 200 * 86400,))
        self.conn.commit()

    # key/value
    def get(self, k: str, default=None):
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, k: str, v) -> None:
        self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))
        self.conn.commit()

    # overrides
    def add_override(self, event_id: str, kind: str, reason: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO overrides VALUES (?,?,?,?)", (event_id, kind, reason, time.time()))
        self.conn.commit()

    def overridden(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT event_id FROM overrides")}
