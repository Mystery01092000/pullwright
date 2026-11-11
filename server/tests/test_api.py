import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pullwright import api
from pullwright.store import Store
from tests.fake_client import FakeClient, text, tool_use, turn

INTAKE = json.dumps({"understanding": "u", "plan": ["p"], "questions": []})
DIAG_OK = json.dumps({"complete": True, "gaps": [], "evidence": "e"})


def _repo(tmp_path: Path) -> Path:
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@t"], ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("a")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.fixture
def client(tmp_path: Path):
    api.app.state.store = Store(":memory:")
    api.app.state.live = {}
    turns = [
        turn(text(INTAKE)),
        turn(tool_use("spawn_worker", {"task": "t", "context": "c"})),
        turn(tool_use("write_file", {"path": "b.txt", "content": "b"}, "tu_w")),
        turn(text("worker done")),
        turn(text("sup done")),
        turn(text(DIAG_OK)),
        turn(text("summary\nTITLE: t")),
    ]
    api.app.state.client_factory = lambda: FakeClient(turns)
    with TestClient(api.app) as c:
        yield c


def _wait(c: TestClient, run_id: str, pred, timeout: float = 10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/runs/{run_id}").json()
        if pred(r):
            return r
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting; last={r}")


def test_run_via_api(client: TestClient, tmp_path: Path):
    repo = _repo(tmp_path)
    run_id = client.post("/runs", json={"task": "add b.txt", "cwd": str(repo)}).json()["id"]

    r = _wait(client, run_id, lambda r: r["pending"] and r["pending"]["kind"] == "confirm_intake")
    assert r["state"] == "awaiting_confirmation" and r["pending"]["intake"]["understanding"] == "u"
    assert client.post(f"/runs/{run_id}/decide", json={"approved": True}).json() == {"ok": True}

    r = _wait(client, run_id, lambda r: r["pending"] and r["pending"]["kind"] == "want_pr")
    assert "summary" in r["pending"]["summary"]
    client.post(f"/runs/{run_id}/decide", json={"approved": False})

    r = _wait(client, run_id, lambda r: r["state"] == "done")
    assert r["branch"] == "pullwright/add-b-txt" and r["commit_sha"] and r["cost_usd"] > 0
    assert (repo / "b.txt").read_text() == "b"

    evs = client.get(f"/runs/{run_id}/events").json()
    types = [e["type"] for e in evs]
    assert types[0] == "state" and types.count("usage") == 7 and "decision_needed" in types and "tool_call" in types
    worker = next(e for e in evs if e["type"] == "agent_start" and e["agent_id"].startswith("worker"))
    assert worker["parent_id"].startswith("supervisor")

    cost = client.get(f"/runs/{run_id}/cost").json()
    assert cost["total"]["calls"] == 7 and abs(cost["total"]["cost_usd"] - r["cost_usd"]) < 1e-9
    assert {a["k"].split("-")[0] for a in cost["by_agent"]} == {"intake", "supervisor", "worker1", "diagnosis", "summarizer"}

    assert client.get("/runs").json()[0]["id"] == run_id
    assert client.post(f"/runs/{run_id}/decide", json={"approved": True}).status_code == 409
    only = client.get(f"/runs/{run_id}/events", params={"types": "state,usage"}).json()
    assert {e["type"] for e in only} == {"state", "usage"}


def test_stream_replays_finished_run(client: TestClient, tmp_path: Path):
    repo = _repo(tmp_path)
    run_id = client.post("/runs", json={"task": "x", "cwd": str(repo)}).json()["id"]
    _wait(client, run_id, lambda r: r["pending"])
    client.post(f"/runs/{run_id}/decide", json={"approved": True})
    _wait(client, run_id, lambda r: r["pending"] and r["pending"]["kind"] == "want_pr")
    client.post(f"/runs/{run_id}/decide", json={"approved": False})
    _wait(client, run_id, lambda r: r["state"] == "done")
    with client.stream("GET", f"/runs/{run_id}/stream") as s:
        body = "".join(s.iter_text())
    assert body.count("event: ") >= 10 and '"state": "done"' in body


def test_cancel_unblocks_pending_decision(client: TestClient, tmp_path: Path):
    repo = _repo(tmp_path)
    run_id = client.post("/runs", json={"task": "x", "cwd": str(repo)}).json()["id"]
    _wait(client, run_id, lambda r: r["pending"])
    assert client.post(f"/runs/{run_id}/cancel").json() == {"ok": True}
    r = _wait(client, run_id, lambda r: r["state"] == "cancelled")
    assert r["pending"] is None
    assert client.post(f"/runs/{run_id}/cancel").status_code == 409
    assert any(e["payload"].get("state") == "cancelled" for e in client.get(f"/runs/{run_id}/events", params={"types": "state"}).json())


def test_orphan_sweep_closes_runs_left_working():
    from pullwright.api import _orphan_sweep
    from pullwright.events import Event
    s = Store(":memory:")
    s.create_run("r1", "t", ".")
    s.update_run("r1", state="working")
    s.append(Event("state", "r1", "run", {"state": "working"}))
    s.create_run("r2", "t", ".")
    s.update_run("r2", state="cancelled")  # row fixed by hand, log still says working
    s.append(Event("state", "r2", "run", {"state": "working"}))
    _orphan_sweep(s)
    for rid in ("r1", "r2"):
        assert s.run(rid)["state"] == "cancelled"
        assert s.events(rid, types=["state"])[-1]["payload"]["state"] == "cancelled"


def _finish(c: TestClient, run_id: str):
    _wait(c, run_id, lambda r: r["pending"] and r["pending"]["kind"] == "confirm_intake")
    c.post(f"/runs/{run_id}/decide", json={"approved": True})
    r = _wait(c, run_id, lambda r: r["state"] == "done" or (r["pending"] and r["pending"]["kind"] == "want_pr"))
    if r["pending"]:  # no PR prompt when there was nothing to commit
        c.post(f"/runs/{run_id}/decide", json={"approved": False})
    return _wait(c, run_id, lambda r: r["state"] == "done")


def test_continue_run_shares_thread_branch_and_context(client: TestClient, tmp_path: Path):
    repo = _repo(tmp_path)
    first = client.post("/runs", json={"task": "add b.txt", "cwd": str(repo)}).json()["id"]
    r1 = _finish(client, first)
    assert r1["thread_id"] == first and r1["branch"] == "pullwright/add-b-txt"

    assert client.post("/runs", json={"task": "x", "continue_from": "nope"}).status_code == 404
    second = client.post("/runs", json={"task": "also add c.txt", "continue_from": first}).json()["id"]
    r2 = _finish(client, second)
    assert r2["thread_id"] == first and r2["cwd"] == r1["cwd"]
    assert r2["branch"] == "pullwright/add-b-txt"  # continued on the same branch, no new one
    branches = subprocess.run(["git", "branch"], cwd=repo, capture_output=True, text=True).stdout
    assert branches.count("pullwright/") == 1

    # the intake prompt of the continuation carries the thread history
    live = api.app.state.live[second]
    first_prompt = live.run.request
    assert "History of this thread" in first_prompt and "add b.txt" in first_prompt and "The user now says:\nalso add c.txt" in first_prompt

    thread = client.get("/runs", params={"thread": first}).json()
    assert [r["id"] for r in thread] == [first, second]
    assert all("thread_id" in r for r in client.get("/runs").json())
