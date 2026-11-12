"""The store behaves the same on SQLite and Postgres. Postgres runs when PULLWRIGHT_TEST_PG holds a URL to a
throwaway database (its tables are dropped first), e.g. postgresql://postgres:pw@localhost:55432/pullwright."""
import os
import time

import pytest

from pullwright.events import Event
from pullwright.knowledge import KnowledgeBase, RepoProfile
from pullwright.store import Store

PG = os.environ.get("PULLWRIGHT_TEST_PG", "")
TABLES = ("runs", "events", "requests", "thread_messages", "identities", "repo_profiles")


def _fresh_pg() -> Store:
    import psycopg

    with psycopg.connect(PG, autocommit=True) as c:
        for t in TABLES:
            c.execute(f"DROP TABLE IF EXISTS {t}")
    return Store(PG)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG, reason="PULLWRIGHT_TEST_PG not set"))])
def store(request) -> Store:
    return Store() if request.param == "sqlite" else _fresh_pg()


def test_runs_events_and_cost(store: Store):
    store.create_run("r1", "task", "/repo", "th1")
    store.update_run("r1", state="done", cost_usd=0.5)
    assert store.run("r1")["state"] == "done" and [r["id"] for r in store.runs("th1")] == ["r1"]
    t0 = time.time()
    a = store.append(Event("agent_start", "r1", "w1", {"name": "worker1"}))
    b = store.append(Event("usage", "r1", "w1", {"model": "m1", "cost_usd": 0.25, "input_tokens": 100, "output_tokens": 10}))
    c = store.append(Event("usage", "r1", "s1", {"model": "m2", "cost_usd": 0.5, "input_tokens": 7, "output_tokens": 3}))
    assert a < b < c
    assert [e["type"] for e in store.events("r1", after=a)] == ["usage", "usage"]
    assert [e["id"] for e in store.events("r1", types=["agent_start"])] == [a]
    assert store.events("r1")[0]["ts"] >= t0 - 1  # timestamps keep their precision (DOUBLE PRECISION on Postgres)
    cost = store.cost("r1")
    assert cost["total"]["calls"] == 2 and abs(cost["total"]["cost_usd"] - 0.75) < 1e-9 and cost["total"]["input_tokens"] == 107
    assert {r["k"] for r in cost["by_model"]} == {"m1", "m2"}


def test_requests_costs_activity_identities_messages(store: Store):
    store.create_request("q1", "web", "o/app", "task", "asha", "Asha", {"thread": "t1"})
    store.update_request("q1", state="queued", issue_number=3, pr_url="https://github.com/o/app/pull/9", plan={"title": "x"})
    store.add_request_costs("q1", {"total_usd": 0.2, "calls": 2, "by_stage": {"plan": {"cost_usd": 0.2, "calls": 2, "input_tokens": 1, "output_tokens": 1}},
                                   "by_model": {}, "unpriced": []}, {"kind": "plan", "status": "approved", "cost_usd": 0.2})
    store.link_run("q1", "app-3")
    store.link_run("q1", "app-3")
    store.append(Event("state", "app-3", "run", {"state": "setup"}))
    r = store.request("q1")
    assert r["plan"] == {"title": "x"} and abs(r["cost_usd"] - 0.2) < 1e-9 and r["runs"] == ["app-3"]
    assert store.request_for_issue("o/app", 3)["id"] == "q1" and store.request_for_pr("o/app", 9)["id"] == "q1"
    assert [e["payload"]["state"] for e in store.activity("q1")] == ["setup"]
    assert [x["id"] for x in store.requests("asha")] == ["q1"]

    store.link_identity("slack", "U1", "asha", "Asha")
    store.link_identity("slack", "U1", "asha2", "Asha R")  # upsert
    assert store.identity("slack", "U1")["github_login"] == "asha2"

    m1 = store.add_message("t1", "user", {"text": "hi"})
    m2 = store.add_message("t1", "reply", {"text": "ok"})
    assert m1 < m2 and [m["body"]["text"] for m in store.messages("t1", after=m1)] == ["ok"]


def test_knowledge_profiles(store: Store):
    kb = KnowledgeBase(gh=None, store=store)  # type: ignore[arg-type]
    kb.save(RepoProfile("o/app", description="first"))
    kb.save(RepoProfile("o/app", description="second", languages=["Python"]))
    p = kb.get("o/app")
    assert p is not None and p.description == "second" and p.languages == ["Python"]
    assert kb.get("o/none") is None
