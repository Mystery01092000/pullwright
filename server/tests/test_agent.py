from pathlib import Path

from pullwright.agent import AgentDef, run_agent
from pullwright.events import EventBus
from pullwright.pricing import cost_usd
from pullwright.tools import ToolContext
from tests.fake_client import FakeClient, text, thinking, tool_use, turn


def _run(tmp_path: Path, turns, **agent_kw):
    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    client = FakeClient(turns)
    agent = AgentDef(name="w", system="sys", **agent_kw)
    res = run_agent(client, agent, [{"role": "user", "content": "go"}], ToolContext(root=tmp_path), bus, run_id="r1")
    return res, events, client


def test_tool_loop_and_events(tmp_path: Path):
    res, events, client = _run(
        tmp_path,
        [
            turn(thinking("plan"), tool_use("write_file", {"path": "x.txt", "content": "hi"})),
            turn(text("done")),
        ],
    )
    assert res.stop == "end_turn" and res.text == "done"
    assert (tmp_path / "x.txt").read_text() == "hi"
    types = [e.type for e in events]
    assert types == ["agent_start", "thinking", "usage", "tool_call", "tool_result", "text", "usage", "agent_end"]
    # history is append-only: user, assistant, user(tool_result), assistant
    roles = [m["role"] for m in res.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert res.messages[2]["content"][0]["tool_use_id"] == "tu_1"
    # request shape
    kw = client.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["thinking"]["display"] == "summarized"
    assert kw["fallbacks"] == "default" and all(t["strict"] for t in kw["tools"])


def test_usage_and_cost(tmp_path: Path):
    res, events, _ = _run(tmp_path, [turn(text("ok"), in_tok=1000, out_tok=500)])
    assert res.usage["input_tokens"] == 1000 and res.usage["output_tokens"] == 500
    assert abs(res.cost_usd - cost_usd("claude-opus-5-5", res.usage)) < 1e-12
    assert abs(res.cost_usd - (1000 * 4 + 500 * 20) / 1e6) < 1e-12
    end = events[-1]
    assert end.type == "agent_end" and end.payload["cost_usd"] == res.cost_usd


def test_budget_stop(tmp_path: Path):
    tu = tool_use("glob", {"pattern": "*"})
    res, events, _ = _run(tmp_path, [turn(tu), turn(tu), turn(text("x"))], max_cost_usd=0.001)
    assert res.stop == "budget"
    assert any(e.type == "error" and e.payload["reason"] == "budget exceeded" for e in events)


def test_max_turns_and_refusal(tmp_path: Path):
    tu = tool_use("glob", {"pattern": "*"})
    res, _, _ = _run(tmp_path, [turn(tu), turn(tu), turn(tu)], max_turns=2)
    assert res.stop == "max_turns"
    res, _, _ = _run(tmp_path, [turn(text(""), stop_reason="refusal")])
    assert res.stop == "refusal"


def test_disallowed_tool_is_error_result(tmp_path: Path):
    res, events, _ = _run(tmp_path, [turn(tool_use("bash", {"command": "echo"})), turn(text("ok"))], tools=["read_file"])
    tr = next(e for e in events if e.type == "tool_result")
    assert tr.payload["is_error"] and "not allowed" in tr.payload["output"]
    assert res.messages[2]["content"][0]["is_error"] is True
