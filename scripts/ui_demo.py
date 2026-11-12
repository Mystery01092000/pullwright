"""See every screen of the web UI with sample data, without GitHub or any model spend.

    cd server && uv run python ../scripts/ui_demo.py

Seeds an in-memory store (a plan awaiting approval, a runner in round 2, an open PR, older requests for the Costs
view), serves the built UI (web/dist) on http://127.0.0.1:8790 and prints a session cookie: in the browser console,
`document.cookie = "pw_session=<value>; path=/"`, then reload. Decisions are accepted but do nothing.
"""
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from pullwright.channels.base import PlanCard
from pullwright.events import Event
from pullwright.store import Store
from pullwright.web import COOKIE, Signer, build_router


class StubIntake:
    allowed_repos = {"example/orders-api", "example/web"}

    def __init__(self, store):
        self.store = store

    def on_decision(self, d):
        return "Approved. Opening the issue."

    def on_mention(self, m):
        return None


store = Store()
now = time.time()
SPEC = {
    "title": "Paginate GET /orders with an opaque cursor",
    "summary": "GET /orders returns every order in one response. Add cursor pagination (limit + next_cursor) so large accounts load fast, keeping the default response shape for existing clients.",
    "relevant_files": [{"path": "app/routes/orders.py", "why": "the list endpoint"}, {"path": "app/store.py", "why": "OrderStore.list() needs a keyset query"},
                       {"path": "tests/test_orders.py", "why": "existing endpoint tests"}],
    "plan": ["Add OrderStore.page(after_id, limit) using a keyset on (created_at, id)", "Accept ?limit= and ?cursor= on GET /orders; encode the cursor as base64 JSON",
             "Return next_cursor when more rows exist", "Document the parameters in the OpenAPI schema"],
    "acceptance_criteria": ["GET /orders?limit=2 returns 2 orders and a next_cursor", "Following next_cursor returns the next page with no duplicates", "Without parameters the response is unchanged"],
    "test_plan": ["Unit test page() boundaries", "API test walking three pages", "Regression test for the unpaginated response"],
    "risks": ["Clients that ignore next_cursor keep getting only the first page if they pass limit"],
    "questions": ["Should the maximum page size be 100 or 500?"],
}


def req(rid, repo, task, state, login="asha", **kw):
    store.create_request(rid, "web", repo, task, login, login.title(), {"thread": f"t-{rid}"})
    store.db.execute("UPDATE requests SET created_at = ? WHERE id = ?", (now - kw.pop("age", 0), rid))
    store.db.commit()
    store.update_request(rid, state=state, **kw)


def ev(run, agent, type_, **p):
    store.append(Event(type_, run, agent, p))


def cost(rid, plan, work, review=0.0, models=None):
    by_stage = {k: {"cost_usd": v, "calls": max(1, int(v * 60)), "input_tokens": int(v * 400000), "output_tokens": int(v * 30000)}
                for k, v in (("plan", plan), ("work", work), ("review", review)) if v}
    total = plan + work + review
    models = models or {"claude-sonnet-5-5": total * 0.8, "claude-haiku-5-5": total * 0.2}
    store.add_request_costs(rid, {"total_usd": total, "calls": sum(b["calls"] for b in by_stage.values()), "by_stage": by_stage,
                                  "by_model": {m: {"cost_usd": c, "calls": 10, "input_tokens": 1000, "output_tokens": 100} for m, c in models.items()},
                                  "unpriced": [m for m, c in models.items() if c == 0]})


# 1. awaiting approval with a structured plan
req("r1", "example/orders-api", "Paginate GET /orders with a cursor", "awaiting_approval")
store.add_message("t-r1", "user", {"text": "Paginate GET /orders with a cursor"})
ev("req-r1", "run", "state", state="describing")
ev("req-r1", "run", "state", state="verifying")
ev("req-r1", "run", "state", state="awaiting_approval", revision=0)
store.add_message("t-r1", "plan", PlanCard("r1", "example/orders-api", "Paginate", "", 0, 0.071, SPEC["questions"], spec=SPEC,
                                          verification={"approved": True, "corrections": [], "evidence": "read orders.py"}).__dict__)

# 2. runner working, round 2, checks failing then running
req("r2", "example/orders-api", "Add a /health endpoint that returns the app version", "working", issue_number=12,
    issue_url="https://github.com/example/orders-api/issues/12", age=3600)
store.add_message("t-r2", "user", {"text": "Add a /health endpoint that returns the app version"})
for s in ("describing", "verifying", "awaiting_approval", "approved"):
    ev("req-r2", "run", "state", state=s)
store.link_run("r2", "orders-api-12")
R = "orders-api-12"
ev(R, "run", "state", state="setup")
ev(R, "sup-1", "agent_start", name="supervisor", model="claude-sonnet-5-5")
ev(R, "run", "state", state="working")
ev(R, "w1", "agent_start", name="worker1", model="openai:gpt-5.1-mini")
ev(R, "w1", "tool_call", name="read_file", input={"path": "app/main.py"})
ev(R, "w1", "tool_call", name="write_file", input={"path": "app/routes/health.py", "content": "x"})
ev(R, "w1", "usage", model="openai:gpt-5.1-mini", cost_usd=0.004)
ev(R, "w1", "agent_end", stop="end_turn")
ev(R, "run", "state", state="checking")
ev(R, "run", "checks", green=False, results=[{"name": "lint", "ok": True, "seconds": 1.2, "command": "ruff check"},
                                            {"name": "test", "ok": False, "seconds": 8.4, "command": "pytest -q"}])
ev(R, "run", "state", state="working")
ev(R, "w2", "agent_start", name="worker2", model="openai:gpt-5.1-mini")
ev(R, "w2", "tool_call", name="bash", input={"command": "pytest -q tests/test_health.py -x"})
ev(R, "w2", "tool_call", name="edit_file", input={"path": "tests/test_health.py"})
ev(R, "sup-1", "usage", model="claude-sonnet-5-5", cost_usd=0.031)
ev(R, "run", "state", state="checking")
cost("r2", 0.064, 0.035)

# 3. PR open with a follow-up
req("r3", "example/web", "Fix the flaky date test in tests/test_reports.py", "pr_open", login="vik", issue_number=40,
    issue_url="https://github.com/example/web/issues/40", pr_url="https://github.com/example/web/pull/41", age=86400 * 2)
store.add_message("t-r3", "user", {"text": "Fix the flaky date test"})
store.add_message("t-r3", "reply", {"text": "Pull request for #40: https://github.com/example/web/pull/41. All checks pass."})
for s in ("describing", "verifying", "awaiting_approval", "approved"):
    ev("req-r3", "run", "state", state=s)
store.link_run("r3", "web-40")
ev("web-40", "run", "state", state="setup")
ev("web-40", "s", "agent_start", name="supervisor", model="claude-sonnet-5-5")
ev("web-40", "run", "state", state="checking")
ev("web-40", "run", "checks", green=True, results=[{"name": "test", "ok": True, "seconds": 22.0, "command": "npm test"}])
ev("web-40", "run", "state", state="reviewing")
ev("web-40", "run", "state", state="green")
cost("r3", 0.05, 0.21, 0.02, {"claude-sonnet-5-5": 0.2, "claude-haiku-5-5": 0.03, "ollama:qwen3-coder": 0.0, "gemini:gemini-3-pro": 0.05})

# 4. a few older ones for the cost view
for i, (state, d) in enumerate((("failed", 5), ("cancelled", 9), ("pr_draft", 12), ("pr_open", 20))):
    req(f"o{i}", "example/orders-api" if i % 2 else "example/web", f"Older task {i}", state, login="asha" if i % 2 else "vik", age=86400 * d,
        issue_number=100 + i if state != "cancelled" else None)
    cost(f"o{i}", 0.04, 0.1 * (i + 1), 0.01 * i)
req("p1", "example/web", "Add dark mode to the settings page", "planning", age=60)
ev("req-p1", "run", "state", state="describing")

signer = Signer(b"demo-secret-not-for-production")
app = FastAPI()
auth, api = build_router(StubIntake(store), store, signer, None, None, "http://127.0.0.1:8790")
app.include_router(auth)
app.include_router(api, prefix="/api")
app.mount("/", StaticFiles(directory=Path(__file__).resolve().parents[1] / "web" / "dist", html=True), name="web")
print("COOKIE", COOKIE, signer.dumps({"login": "asha", "name": "Asha Rao"}, 86400 * 7), flush=True)
uvicorn.run(app, host="127.0.0.1", port=8790, log_level="warning")
