from pathlib import Path

from pullwright import costs
from pullwright.events import Event
from pullwright.session import Session
from pullwright.store import Store
from tests.fake_client import FakeClient, text, turn
from tests.test_web import COOKIE, _client


def _usage(agent: str, model: str, cost: float, priced: bool = True) -> Event:
    return Event("usage", "r", agent, {"model": model, "cost_usd": cost, "input_tokens": 100, "output_tokens": 10,
                                       "cache_read_input_tokens": 50, "priced": priced})


def test_meter_charges_each_call_to_its_stage_and_model():
    m = costs.CostMeter()
    for aid, name in (("d1", "describer"), ("s1", "supervisor"), ("w1", "worker3"), ("x1", "diagnosis"), ("q", "mystery")):
        m(Event("agent_start", "r", aid, {"name": name}))
    m(_usage("d1", "claude-sonnet-5-5", 0.05))
    m(_usage("s1", "claude-sonnet-5-5", 0.10))
    m(_usage("w1", "openai:gpt-x", 0.0, priced=False))
    m(_usage("x1", "gemini:g", 0.02))
    m(_usage("q", "claude-haiku-5-5", 0.001))
    d = m.to_dict()
    assert {k: round(v["cost_usd"], 3) for k, v in d["by_stage"].items()} == {"plan": 0.05, "work": 0.1, "review": 0.02, "other": 0.001}
    assert d["by_model"]["claude-sonnet-5-5"]["calls"] == 2 and d["by_stage"]["plan"]["input_tokens"] == 150
    assert d["unpriced"] == ["openai:gpt-x"] and d["calls"] == 5 and abs(d["total_usd"] - 0.171) < 1e-9
    md = "\n".join(costs.markdown(d))
    assert "| Planning | $0.050 | 1 |" in md and "`openai:gpt-x` (unpriced)" in md


def test_session_meters_its_agents(tmp_path: Path):
    s = Session(cwd=tmp_path, client=FakeClient([turn(text("plan"), model="claude-sonnet-5-5")]))
    s.run(s.agent("describer"), "go")
    assert s.meter.to_dict()["by_stage"]["plan"]["calls"] == 1 and s.meter.to_dict()["total_usd"] == round(s.cost_usd, 6)


def test_request_costs_accumulate_planning_and_jobs():
    store = Store()
    store.create_request("r1", "web", "o/app", "task", "asha", "Asha", {"thread": "t"})
    plan = {"total_usd": 0.1, "calls": 2, "by_stage": {"plan": {"cost_usd": 0.1, "calls": 2, "input_tokens": 5, "output_tokens": 1}},
            "by_model": {"claude-sonnet-5-5": {"cost_usd": 0.1, "calls": 2, "input_tokens": 5, "output_tokens": 1}}, "unpriced": []}
    work = {"total_usd": 0.3, "calls": 4, "by_stage": {"work": {"cost_usd": 0.3, "calls": 4, "input_tokens": 9, "output_tokens": 2}},
            "by_model": {"claude-sonnet-5-5": {"cost_usd": 0.3, "calls": 4, "input_tokens": 9, "output_tokens": 2}}, "unpriced": []}
    store.add_request_costs("r1", plan, {"kind": "plan", "status": "approved", "cost_usd": 0.1})
    store.add_request_costs("r1", work, {"kind": "issue", "status": "green", "cost_usd": 0.3, "proxy_usd": 0.3})
    r = store.request("r1")
    assert abs(r["cost_usd"] - 0.4) < 1e-9 and set(r["costs"]["by_stage"]) == {"plan", "work"}
    assert r["costs"]["by_model"]["claude-sonnet-5-5"]["calls"] == 6
    assert [x["kind"] for x in r["costs"]["runs"]] == ["plan", "issue"] and r["costs"]["runs"][1]["proxy_usd"] == 0.3


def test_cost_view_endpoint():
    c, _, store, signer = _client()
    store.create_request("r1", "web", "o/app", "add hello", "asha", "Asha", {"thread": "t1"})
    store.create_request("r2", "slack", "o/api", "fix bug", "vik", "Vik", {})
    store.add_request_costs("r1", {"total_usd": 0.5, "calls": 3, "by_stage": {"work": {"cost_usd": 0.5, "calls": 3, "input_tokens": 1, "output_tokens": 1}},
                                   "by_model": {"m": {"cost_usd": 0.5, "calls": 3, "input_tokens": 1, "output_tokens": 1}}, "unpriced": []})
    store.add_request_costs("r2", {"total_usd": 0.2, "calls": 1, "by_stage": {}, "by_model": {}, "unpriced": []})
    assert c.get("/api/costs").status_code == 401
    c.cookies.set(COOKIE, signer.dumps({"login": "asha", "name": "Asha Rao"}, 3600))
    d = c.get("/api/costs", params={"days": 7}).json()
    assert abs(d["total_usd"] - 0.7) < 1e-9 and d["requests"] == 2
    assert d["by_repo"]["o/app"]["cost_usd"] == 0.5 and d["by_requester"]["vik"]["requests"] == 1
    assert d["top"][0]["id"] == "r1" and d["by_stage"]["work"]["calls"] == 3 and len(d["by_day"]) == 1
