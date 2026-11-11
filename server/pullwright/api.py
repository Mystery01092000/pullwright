"""FastAPI surface: start runs, answer decisions, replay/stream events, cost rollups.

Run:  uv run uvicorn pullwright.api:app --reload --port 8765
Env:  PULLWRIGHT_DB (default pullwright.db), PULLWRIGHT_MCP (default mcp.json); HARNESS_* names still read
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .events import Event, EventBus
from .lifecycle import Run, thread_context
from .mcp_tools import load_mcp
from .store import Store


class QueueIO:
    """UserIO over HTTP: emits a decision_needed event, blocks the run thread until POST /decide."""

    def __init__(self, bus: EventBus, run_id: str) -> None:
        self.bus, self.run_id = bus, run_id
        self.q: queue.Queue[dict[str, Any]] = queue.Queue()
        self.pending: dict[str, Any] | None = None

    def _ask(self, kind: str, **payload: Any) -> dict[str, Any]:
        self.pending = {"kind": kind, **payload}
        self.bus.emit(Event("decision_needed", self.run_id, "run", self.pending))
        ans = self.q.get()
        self.pending = None
        self.bus.emit(Event("decision", self.run_id, "run", {"kind": kind, **ans}))
        return ans

    def confirm_intake(self, intake: dict[str, Any]) -> tuple[bool, str]:
        a = self._ask("confirm_intake", intake=intake)
        return bool(a.get("approved")), a.get("feedback", "")

    def approve_command(self, command: str) -> bool:
        return bool(self._ask("approve_command", command=command).get("approved"))

    def want_pr(self, summary: str) -> bool:
        return bool(self._ask("want_pr", summary=summary).get("approved"))


class LiveRun:
    def __init__(self, run: Run, io: QueueIO) -> None:
        self.run, self.io = run, io
        self.subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self.loop: asyncio.AbstractEventLoop | None = None


class StartRun(BaseModel):
    task: str
    cwd: str = "."
    main_model: str = "claude-opus-5-5"
    worker_model: str = "claude-sonnet-5-5"
    max_cost_usd: float = 20.0
    continue_from: str | None = None  # id of a finished run in the thread to continue


def _thread_context(store: Store, thread_id: str) -> str:
    """Rebuild the thread history from the event log and hand it to lifecycle.thread_context."""
    history = []
    for r in store.runs(thread_id):
        evs = store.events(r["id"], types=["state", "diagnosis"])
        history.append({
            "task": r["task"],
            "state": r["state"],
            "branch": r["branch"],
            "summary": next((e["payload"]["summary"] for e in reversed(evs) if e["payload"].get("summary")), ""),
            "diagnosis": next((e["payload"] for e in reversed(evs) if e["type"] == "diagnosis"), None),
        })
    return thread_context(history)


class Decision(BaseModel):
    approved: bool
    feedback: str = ""


TERMINAL = ("done", "failed", "cancelled")


def _orphan_sweep(store: Store) -> None:
    """Runs left mid-flight by a crash or restart can never resume: close them out in both the row and the log."""
    for r in store.runs():
        last = store.events(r["id"], types=["state"])
        last_state = last[-1]["payload"]["state"] if last else r["state"]
        if r["state"] not in TERMINAL or last_state not in TERMINAL:
            state = r["state"] if r["state"] in TERMINAL else "cancelled"
            store.update_run(r["id"], state=state)
            if last_state != state:
                store.append(Event("state", r["id"], "run", {"state": state, "error": "server restarted while the run was active"}))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _orphan_sweep(app.state.store)
    cfg = Path(os.environ.get("PULLWRIGHT_MCP") or os.environ.get("HARNESS_MCP", "mcp.json"))
    app.state.mcp = load_mcp(cfg) if cfg.is_file() else None
    yield
    if app.state.mcp:
        app.state.mcp.close()


app = FastAPI(title="pullwright", lifespan=_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.state.store = Store(os.environ.get("PULLWRIGHT_DB") or os.environ.get("HARNESS_DB", "pullwright.db"))
app.state.live: dict[str, LiveRun] = {}
app.state.client_factory = None  # tests inject a fake; None -> anthropic.Anthropic()


def _client() -> Any:
    if app.state.client_factory:
        return app.state.client_factory()
    import anthropic

    return anthropic.Anthropic()


def _serialize(ev: Event, row_id: int) -> dict[str, Any]:
    return {"id": row_id, **asdict(ev)}


@app.post("/runs")
async def start_run(body: StartRun) -> dict[str, Any]:
    store: Store = app.state.store
    bus = EventBus()
    cwd, thread_id, branch, prior = Path(body.cwd).resolve(), "", "", ""
    if body.continue_from:
        prev = store.run(body.continue_from)
        if not prev:
            raise HTTPException(404, "run to continue not found")
        if prev["state"] not in TERMINAL:
            raise HTTPException(409, "that run is still active")
        cwd, thread_id, branch = Path(prev["cwd"]), prev["thread_id"], prev["branch"] or ""
        prior = _thread_context(store, thread_id)
    run = Run(task=body.task, cwd=cwd, io=None, client=_client(), bus=bus, thread_id=thread_id, prior_context=prior, branch=branch, main_model=body.main_model, worker_model=body.worker_model, max_cost_usd=body.max_cost_usd)  # type: ignore[arg-type]
    io = QueueIO(bus, run.id)
    run.io = io
    live = LiveRun(run, io)
    live.loop = asyncio.get_running_loop()
    app.state.live[run.id] = live
    store.create_run(run.id, run.task, str(run.cwd), run.thread_id)

    def on_event(ev: Event) -> None:
        row_id = store.append(ev)
        if ev.type == "state":
            store.update_run(run.id, state=run.state, cost_usd=run.cost_usd, branch=run.branch, commit_sha=run.commit, pr_url=run.pr_url)
        data = _serialize(ev, row_id)
        if live.loop:
            for q in list(live.subscribers):
                live.loop.call_soon_threadsafe(q.put_nowait, data)

    bus.subscribe(on_event)

    def go() -> None:
        try:
            run.run()
        except Exception:
            pass  # state=failed already emitted by Run.run
        finally:
            store.update_run(run.id, state=run.state, cost_usd=run.cost_usd)

    threading.Thread(target=go, daemon=True, name=f"run-{run.id}").start()
    return {"id": run.id, "state": run.state}


@app.get("/runs")
def list_runs(thread: str | None = None) -> list[dict[str, Any]]:
    return app.state.store.runs(thread)


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    r = app.state.store.run(run_id)
    if not r:
        raise HTTPException(404)
    live = app.state.live.get(run_id)
    return {**r, "pending": live.io.pending if live else None}


@app.get("/runs/{run_id}/events")
def get_events(run_id: str, after: int = 0, types: str | None = None) -> list[dict[str, Any]]:
    return app.state.store.events(run_id, after, types.split(",") if types else None)


@app.get("/runs/{run_id}/cost")
def get_cost(run_id: str) -> dict[str, Any]:
    return app.state.store.cost(run_id)


@app.post("/runs/{run_id}/decide")
def decide(run_id: str, body: Decision) -> dict[str, Any]:
    live = app.state.live.get(run_id)
    if not live or not live.io.pending:
        raise HTTPException(409, "no pending decision")
    live.io.q.put(body.model_dump())
    return {"ok": True}


@app.post("/runs/{run_id}/cancel")
def cancel(run_id: str) -> dict[str, Any]:
    live = app.state.live.get(run_id)
    if not live or live.run.state in ("done", "failed", "cancelled"):
        raise HTTPException(409, "run is not active")
    live.run.cancel()
    if live.io.pending:  # unblock a thread waiting on a decision
        live.io.q.put({"approved": False, "feedback": "cancelled"})
    return {"ok": True}


@app.get("/runs/{run_id}/stream")
async def stream(run_id: str, after: int = 0) -> EventSourceResponse:
    store: Store = app.state.store
    live = app.state.live.get(run_id)
    if not store.run(run_id):
        raise HTTPException(404)

    async def gen():
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        if live:
            live.loop = asyncio.get_running_loop()
            live.subscribers.append(q)
        try:
            last = after
            for ev in store.events(run_id, after):  # replay
                last = ev["id"]
                yield {"event": ev["type"], "id": str(ev["id"]), "data": json.dumps(ev)}
            if not live or live.run.state in ("done", "failed", "cancelled"):
                return
            while True:
                ev = await q.get()
                if ev["id"] <= last:
                    continue
                yield {"event": ev["type"], "id": str(ev["id"]), "data": json.dumps(ev)}
                if ev["type"] == "state" and ev["payload"]["state"] in ("done", "failed", "cancelled"):
                    return
        finally:
            if live and q in live.subscribers:
                live.subscribers.remove(q)

    return EventSourceResponse(gen())
