"""SQLite event/usage store. One row per Event; cost rollups via json_extract over usage events."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .events import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, task TEXT, cwd TEXT, state TEXT, cost_usd REAL DEFAULT 0,
  branch TEXT DEFAULT '', commit_sha TEXT DEFAULT '', pr_url TEXT DEFAULT '', created_at REAL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, agent_id TEXT, parent_id TEXT, turn INTEGER,
  type TEXT, payload TEXT, ts REAL
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()  # ponytail: one connection + lock; move to a pool if writes contend
        with self.lock:
            self.db.executescript(SCHEMA)
            if "thread_id" not in [r[1] for r in self.db.execute("PRAGMA table_info(runs)")]:
                self.db.execute("ALTER TABLE runs ADD COLUMN thread_id TEXT")
            self.db.execute("UPDATE runs SET thread_id = id WHERE thread_id IS NULL")
            self.db.commit()

    def create_run(self, run_id: str, task: str, cwd: str, thread_id: str | None = None) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO runs (id, task, cwd, state, created_at, thread_id) VALUES (?,?,?,?,?,?)",
                (run_id, task, cwd, "created", time.time(), thread_id or run_id),
            )
            self.db.commit()

    def update_run(self, run_id: str, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id))
            self.db.commit()

    def append(self, ev: Event) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO events (run_id, agent_id, parent_id, turn, type, payload, ts) VALUES (?,?,?,?,?,?,?)",
                (ev.run_id, ev.agent_id, ev.parent_id, ev.turn, ev.type, json.dumps(ev.payload, default=str), ev.ts),
            )
            self.db.commit()
            return int(cur.lastrowid or 0)

    def runs(self, thread_id: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            if thread_id:
                return [dict(r) for r in self.db.execute("SELECT * FROM runs WHERE thread_id = ? ORDER BY created_at", (thread_id,))]
            return [dict(r) for r in self.db.execute("SELECT * FROM runs ORDER BY created_at DESC")]

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(r) if r else None

    def events(self, run_id: str, after: int = 0, types: list[str] | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM events WHERE run_id = ? AND id > ?"
        args: list[Any] = [run_id, after]
        if types:
            q += f" AND type IN ({','.join('?' * len(types))})"
            args += types
        with self.lock:
            rows = self.db.execute(q + " ORDER BY id", args).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def cost(self, run_id: str) -> dict[str, Any]:
        q = """
        SELECT {grp} AS k,
               SUM(json_extract(payload,'$.input_tokens')) AS input_tokens,
               SUM(json_extract(payload,'$.output_tokens')) AS output_tokens,
               SUM(json_extract(payload,'$.cache_read_input_tokens')) AS cache_read_input_tokens,
               SUM(json_extract(payload,'$.cache_creation_input_tokens')) AS cache_creation_input_tokens,
               SUM(json_extract(payload,'$.cost_usd')) AS cost_usd, COUNT(*) AS calls
        FROM events WHERE run_id = ? AND type = 'usage' GROUP BY k
        """
        with self.lock:
            by_agent = [dict(r) for r in self.db.execute(q.format(grp="agent_id"), (run_id,))]
            by_model = [dict(r) for r in self.db.execute(q.format(grp="json_extract(payload,'$.model')"), (run_id,))]
        total = {k: sum(r[k] or 0 for r in by_agent) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "cost_usd", "calls")}
        return {"total": total, "by_agent": by_agent, "by_model": by_model}
