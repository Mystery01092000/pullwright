"""SQLite event/usage store. One row per Event; cost rollups via json_extract over usage events."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from . import costs as costs_mod
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
CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY, channel TEXT, repo TEXT, task TEXT, requester_login TEXT, requester_name TEXT,
  thread_ref TEXT, thread_url TEXT DEFAULT '', state TEXT, plan TEXT DEFAULT '', issue_number INTEGER,
  issue_url TEXT DEFAULT '', pr_url TEXT DEFAULT '', error TEXT DEFAULT '', cost_usd REAL DEFAULT 0, created_at REAL
);
CREATE INDEX IF NOT EXISTS requests_issue ON requests(repo, issue_number);
CREATE TABLE IF NOT EXISTS thread_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT, kind TEXT, body TEXT, ts REAL
);
CREATE INDEX IF NOT EXISTS thread_messages_thread ON thread_messages(thread_id, id);
CREATE TABLE IF NOT EXISTS identities (
  channel TEXT, channel_user_id TEXT, github_login TEXT, name TEXT, linked_at REAL,
  PRIMARY KEY (channel, channel_user_id)
);
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
            cols = [r[1] for r in self.db.execute("PRAGMA table_info(requests)")]
            if "costs" not in cols:
                self.db.execute("ALTER TABLE requests ADD COLUMN costs TEXT DEFAULT ''")
            if "runs" not in cols:
                self.db.execute("ALTER TABLE requests ADD COLUMN runs TEXT DEFAULT ''")
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

    # --- chat requests (one per task sent from Slack, Teams or the web) --------------------------

    def create_request(self, rid: str, channel: str, repo: str, task: str, login: str, name: str, thread_ref: dict[str, Any], thread_url: str = "") -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO requests (id, channel, repo, task, requester_login, requester_name, thread_ref, thread_url, state, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rid, channel, repo, task, login, name, json.dumps(thread_ref), thread_url, "planning", time.time()),
            )
            self.db.commit()

    def update_request(self, rid: str, **fields: Any) -> None:
        if "plan" in fields and not isinstance(fields["plan"], str):
            fields["plan"] = json.dumps(fields["plan"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE requests SET {cols} WHERE id = ?", (*fields.values(), rid))
            self.db.commit()

    def _request_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        d["thread_ref"] = json.loads(d["thread_ref"] or "{}")
        d["plan"] = json.loads(d["plan"]) if d["plan"] else None
        d["costs"] = json.loads(d["costs"]) if d.get("costs") else None
        d["runs"] = json.loads(d["runs"]) if d.get("runs") else []
        return d

    def request_for_pr(self, repo: str, pr: int) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM requests WHERE repo = ? AND pr_url LIKE ?", (repo, f"%/pull/{pr}")).fetchone()
        return self._request_row(row)

    def link_run(self, rid: str, run_id: str) -> None:
        """Remember that a runner run (its events' run_id) belongs to this request."""
        with self.lock:
            row = self.db.execute("SELECT runs FROM requests WHERE id = ?", (rid,)).fetchone()
            if row is None:
                return
            runs = json.loads(row["runs"]) if row["runs"] else []
            if run_id not in runs:
                self.db.execute("UPDATE requests SET runs = ? WHERE id = ?", (json.dumps([*runs, run_id]), rid))
                self.db.commit()

    ACTIVITY_TYPES = ("state", "checks", "agent_start", "agent_end", "tool_call", "error", "usage")

    def activity(self, rid: str, after: int = 0, limit: int = 400) -> list[dict[str, Any]]:
        """A request's progress: planning (run `req-<id>`) and every runner run linked to it, oldest first, with tool
        inputs cut down to a one-line summary."""
        req = self.request(rid)
        if req is None:
            return []
        run_ids = [f"req-{rid}", *req["runs"]]
        q = (f"SELECT * FROM events WHERE run_id IN ({','.join('?' * len(run_ids))}) AND id > ?"
             f" AND type IN ({','.join('?' * len(self.ACTIVITY_TYPES))}) ORDER BY id LIMIT ?")
        with self.lock:
            rows = self.db.execute(q, (*run_ids, after, *self.ACTIVITY_TYPES, limit)).fetchall()
        out = []
        for r in rows:
            p = json.loads(r["payload"])
            if r["type"] == "tool_call":
                inp = p.get("input") or {}
                hint = next((str(inp[k]) for k in ("path", "command", "pattern", "task", "name") if k in inp), "")
                p = {"name": p.get("name"), "hint": hint[:160]}
            elif r["type"] == "agent_start":
                p = {"name": p.get("name"), "model": p.get("model")}
            elif r["type"] == "checks":
                p = {"green": p.get("green"), "results": [{k: c.get(k) for k in ("name", "ok", "seconds", "command")} for c in p.get("results", [])]}
            out.append({"id": r["id"], "run_id": r["run_id"], "agent_id": r["agent_id"], "parent_id": r["parent_id"],
                        "type": r["type"], "ts": r["ts"], "payload": p})
        return out

    def add_request_costs(self, rid: str, breakdown: dict[str, Any] | None, run: dict[str, Any] | None = None) -> None:
        """Add one stage's or job's breakdown to a request; `run` ({kind, status, cost_usd, proxy_usd?}) joins its run list.
        cost_usd becomes the request's running total."""
        with self.lock:
            row = self.db.execute("SELECT costs FROM requests WHERE id = ?", (rid,)).fetchone()
            if row is None:
                return
            cur = json.loads(row["costs"]) if row["costs"] else costs_mod.empty()
            runs = list(cur.get("runs", []))
            merged = costs_mod.merge(cur, breakdown)
            if run is not None:
                runs.append({**run, "at": time.time()})
            merged["runs"] = runs
            self.db.execute("UPDATE requests SET costs = ?, cost_usd = ? WHERE id = ?", (json.dumps(merged), merged["total_usd"], rid))
            self.db.commit()

    def request(self, rid: str) -> dict[str, Any] | None:
        with self.lock:
            return self._request_row(self.db.execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone())

    def request_for_issue(self, repo: str, number: int) -> dict[str, Any] | None:
        with self.lock:
            return self._request_row(self.db.execute("SELECT * FROM requests WHERE repo = ? AND issue_number = ?", (repo, number)).fetchone())

    def requests(self, login: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM requests", []
        if login:
            q, args = q + " WHERE requester_login = ?", [login]
        with self.lock:
            rows = self.db.execute(q + " ORDER BY created_at DESC LIMIT ?", (*args, limit)).fetchall()
        return [self._request_row(r) for r in rows]

    # --- who is who: chat user -> GitHub login --------------------------------------------------

    def link_identity(self, channel: str, channel_user_id: str, github_login: str, name: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO identities VALUES (?,?,?,?,?)", (channel, channel_user_id, github_login, name, time.time()))
            self.db.commit()

    def identity(self, channel: str, channel_user_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM identities WHERE channel = ? AND channel_user_id = ?", (channel, channel_user_id)).fetchone()
        return dict(row) if row else None

    # --- web chat threads: what the bot said, so the web UI can show it like a chat --------------

    def add_message(self, thread_id: str, kind: str, body: Any) -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO thread_messages (thread_id, kind, body, ts) VALUES (?,?,?,?)",
                                  (thread_id, kind, json.dumps(body, default=str), time.time()))
            self.db.commit()
            return int(cur.lastrowid or 0)

    def messages(self, thread_id: str, after: int = 0) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM thread_messages WHERE thread_id = ? AND id > ? ORDER BY id", (thread_id, after)).fetchall()
        return [{"id": r["id"], "kind": r["kind"], "body": json.loads(r["body"]), "ts": r["ts"]} for r in rows]
