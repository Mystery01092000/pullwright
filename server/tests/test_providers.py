import json
from pathlib import Path

import httpx
import pytest

from pullwright.agent import AgentDef, run_agent
from pullwright.events import EventBus
from pullwright.pricing import PRICES, cost_usd
from pullwright.providers import ProviderError, Router, as_router, load_provider_configs, parse_model
from pullwright.providers.openai_compat import OpenAICompatProvider, to_openai_messages
from pullwright.session import Session
from pullwright.tools import ToolContext
from tests.fake_client import FakeClient, text, turn


def _chat(message: dict, finish: str = "stop", usage: dict | None = None) -> dict:
    return {"choices": [{"message": message, "finish_reason": finish}],
            "usage": usage or {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 400}}}


def _provider(responses: list, cfg_name: str = "openai", env: dict | None = None):
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append({"url": str(req.url), "auth": req.headers.get("authorization"), "body": json.loads(req.content)})
        r = responses.pop(0)
        return r if isinstance(r, httpx.Response) else httpx.Response(200, json=r)

    router = Router(env={"OPENAI_API_KEY": "sk-test", **(env or {})})
    cfg = router.configs[cfg_name]
    p = OpenAICompatProvider(cfg, cfg.base_url, "sk-test", http=httpx.Client(transport=httpx.MockTransport(handler)), retries=2)
    router._providers[cfg_name] = p
    return router, seen


def test_parse_model():
    assert parse_model("claude-sonnet-5-5") == ("anthropic", "claude-sonnet-5-5")
    assert parse_model("openai:gpt-5.1") == ("openai", "gpt-5.1")
    assert parse_model("ollama:qwen3-coder:30b") == ("ollama", "qwen3-coder:30b")  # ollama tags keep their colon


def test_tool_loop_on_an_openai_compatible_model(tmp_path: Path, monkeypatch):
    monkeypatch.setitem(PRICES, "openai:gpt-test", {"in": 1.0, "out": 4.0, "cache_read": 0.1})
    router, seen = _provider([
        _chat({"content": None, "tool_calls": [{"id": "call_1", "type": "function",
                                                "function": {"name": "write_file", "arguments": json.dumps({"path": "a.txt", "content": "hi"})}}]},
              finish="tool_calls"),
        _chat({"content": "done"}),
    ])
    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    agent = AgentDef(name="w", system="be brief", model="openai:gpt-test", tools=["write_file"])
    res = run_agent(router, agent, [{"role": "user", "content": "go"}], ToolContext(root=tmp_path), bus, run_id="r")

    assert res.stop == "end_turn" and res.text == "done"
    assert (tmp_path / "a.txt").read_text() == "hi"
    first, second = seen[0]["body"], seen[1]["body"]
    assert seen[0]["url"] == "https://api.openai.com/v1/chat/completions" and seen[0]["auth"] == "Bearer sk-test"
    assert first["model"] == "gpt-test" and first["messages"][0] == {"role": "system", "content": "be brief"}
    assert first["tools"][0]["function"]["name"] == "write_file" and "parameters" in first["tools"][0]["function"]
    assert "max_completion_tokens" not in first  # no limit configured: the provider's default
    # the tool result goes back as a tool message tied to the call id
    assert second["messages"][2]["tool_calls"][0]["id"] == "call_1"
    assert second["messages"][3] == {"role": "tool", "tool_call_id": "call_1", "content": second["messages"][3]["content"]}
    # usage: cached prompt tokens are split out and priced under the full model name
    usage = [e for e in events if e.type == "usage"]
    assert usage[0].payload["input_tokens"] == 600 and usage[0].payload["cache_read_input_tokens"] == 400
    assert usage[0].payload["model"] == "openai:gpt-test" and usage[0].payload["priced"] is True
    assert abs(res.cost_usd - 2 * cost_usd("openai:gpt-test", {"input_tokens": 600, "output_tokens": 200, "cache_read_input_tokens": 400})) < 1e-12


def test_unpriced_model_is_flagged(tmp_path: Path):
    router, _ = _provider([_chat({"content": "ok"})])
    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    res = run_agent(router, AgentDef(name="w", system="s", model="openai:no-price", tools=[]), [{"role": "user", "content": "x"}],
                    ToolContext(root=tmp_path), bus, run_id="r")
    assert res.cost_usd == 0
    assert [e.payload["priced"] for e in events if e.type == "usage"] == [False]


def test_structured_output_and_effort(tmp_path: Path):
    router, seen = _provider([_chat({"content": '{"ok": true}'})])
    router.configs["openai"].reasoning_effort = True
    router.configs["openai"].max_output_tokens = 4096
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    agent = AgentDef(name="v", system="s", model="openai:o-test", tools=[], effort="xhigh", output_schema=schema)
    run_agent(router, agent, [{"role": "user", "content": "x"}], ToolContext(root=tmp_path), EventBus(), run_id="r")
    body = seen[0]["body"]
    assert body["reasoning_effort"] == "high" and body["max_completion_tokens"] == 4096
    assert body["response_format"]["json_schema"]["schema"] == schema


def test_retries_rate_limits_then_fails_on_client_errors(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("pullwright.providers.openai_compat.time.sleep", lambda s: None)
    router, seen = _provider([httpx.Response(429, headers={"retry-after": "1"}, text="slow down"), _chat({"content": "ok"})])
    res = run_agent(router, AgentDef(name="w", system="s", model="openai:x", tools=[]), [{"role": "user", "content": "x"}],
                    ToolContext(root=tmp_path), EventBus(), run_id="r")
    assert res.text == "ok" and len(seen) == 2

    router, _ = _provider([httpx.Response(400, text="bad tool schema")])
    with pytest.raises(ProviderError, match="HTTP 400: bad tool schema"):
        run_agent(router, AgentDef(name="w", system="s", model="openai:x", tools=[]), [{"role": "user", "content": "x"}],
                  ToolContext(root=tmp_path), EventBus(), run_id="r")


def test_history_translation_drops_thinking_and_keeps_errors():
    msgs = to_openai_messages("sys", [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "reading"},
                                          {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "no such file", "is_error": True}]},
    ])
    assert msgs[2] == {"role": "assistant", "content": "reading",
                       "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "x"}'}}]}
    assert msgs[3] == {"role": "tool", "tool_call_id": "t1", "content": "ERROR: no such file"}


def test_anthropic_models_still_use_the_injected_client(tmp_path: Path):
    client = FakeClient([turn(text("hi"))])
    res = run_agent(client, AgentDef(name="w", system="s", model="anthropic:claude-sonnet-5-5", tools=[]),
                    [{"role": "user", "content": "x"}], ToolContext(root=tmp_path), EventBus(), run_id="r")
    assert res.text == "hi" and client.calls[0]["model"] == "claude-sonnet-5-5" and client.calls[0]["fallbacks"] == "default"
    r = as_router(client)
    assert as_router(r) is r


def test_provider_config_file_and_errors(tmp_path: Path):
    f = tmp_path / "providers.json"
    f.write_text(json.dumps({"vllm": {"base_url": "http://gpu:8000/v1", "api_key_env": None, "prices": {"qwen": {"in": 0.2, "out": 0.8}}},
                             "openai": {"max_output_tokens": 2048}}))
    cfgs = load_provider_configs({"PULLWRIGHT_PROVIDERS": str(f)})
    assert cfgs["vllm"].base_url == "http://gpu:8000/v1" and cfgs["openai"].max_output_tokens == 2048
    assert cfgs["openai"].base_url == "https://api.openai.com/v1"  # the preset's other fields stay
    assert PRICES["vllm:qwen"]["cache_read"] == 0.2

    router = Router(env={})
    with pytest.raises(ProviderError, match="unknown provider 'nope'"):
        router.provider("nope:model")
    with pytest.raises(ProviderError, match="OPENAI_API_KEY is not set"):
        router.provider("openai:gpt-x")
    p, model = router.provider("ollama:llama4")  # local: no key needed
    assert model == "llama4" and p.url == "http://localhost:11434/v1/chat/completions"


def test_proxy_routes_every_provider_through_the_control_plane():
    router = Router(env={"PULLWRIGHT_LLM_PROXY": "http://cp:8765/llm/", "PULLWRIGHT_LLM_TOKEN": "job-tok"})
    p, _ = router.provider("gemini:gemini-x")  # no GEMINI_API_KEY needed in the runner
    assert p.url == "http://cp:8765/llm/gemini/chat/completions" and p.headers["authorization"] == "Bearer job-tok"


def test_role_model_precedence(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PULLWRIGHT_MODEL_VERIFIER", "gemini:gemini-x")
    s = Session(cwd=tmp_path, client=None)
    assert s.agent("verifier").model == "gemini:gemini-x"
    assert s.agent("describer").model == s.main_model
    agents = tmp_path / ".pullwright" / "agents"
    agents.mkdir(parents=True)
    (agents / "verifier.md").write_text("---\nmodel: openai:gpt-x\n---\nVerify.")
    assert Session(cwd=tmp_path, client=None).agent("verifier").model == "openai:gpt-x"  # the repo's choice wins
