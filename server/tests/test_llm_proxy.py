import json

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pullwright.dispatcher import runner_env
from pullwright.llm_proxy import Ledger, build_proxy
from pullwright.pricing import cost_usd

SSE = (
    'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-sonnet-5-5","usage":'
    '{"input_tokens":1000,"cache_read_input_tokens":500,"output_tokens":1}}}\n\n'
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
    'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":300}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def _app(seen: list, env: dict | None = None):
    def upstream(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        seen.append({"url": str(req.url), "headers": dict(req.headers), "body": body})
        if req.url.path == "/v1/messages":
            if body.get("stream"):
                return httpx.Response(200, content=SSE.encode(), headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json={"model": "claude-haiku-5-5", "content": [], "usage": {"input_tokens": 2000, "output_tokens": 100}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 10}})

    ledger = Ledger()
    app = FastAPI()
    app.include_router(build_proxy(ledger, env={"ANTHROPIC_API_KEY": "sk-real", "OPENAI_API_KEY": "sk-oai", **(env or {})},
                                   transport=httpx.MockTransport(upstream)))
    return TestClient(app), ledger


def test_anthropic_calls_are_forwarded_with_the_real_key_and_charged():
    seen: list = []
    c, ledger = _app(seen)
    tok = ledger.issue("issue:o/r#1", cap_usd=1.0)
    r = c.post("/llm/anthropic/v1/messages", json={"model": "claude-haiku-5-5", "max_tokens": 10, "messages": []},
               headers={"x-api-key": tok, "anthropic-version": "2023-06-01", "anthropic-beta": "x"})
    assert r.status_code == 200
    assert seen[0]["url"] == "https://api.anthropic.com/v1/messages"
    assert seen[0]["headers"]["x-api-key"] == "sk-real" and seen[0]["headers"]["anthropic-beta"] == "x"
    g = ledger.get(tok)
    assert abs(g.spent_usd - cost_usd("claude-haiku-5-5", {"input_tokens": 2000, "output_tokens": 100})) < 1e-12
    assert g.by_model["claude-haiku-5-5"]["calls"] == 1


def test_streamed_responses_pass_through_and_are_metered():
    seen: list = []
    c, ledger = _app(seen)
    tok = ledger.issue("j", 1.0)
    r = c.post("/llm/anthropic/v1/messages", json={"model": "claude-sonnet-5-5", "stream": True, "messages": []}, headers={"x-api-key": tok})
    assert r.status_code == 200 and '"text":"hi"' in r.text
    want = cost_usd("claude-sonnet-5-5", {"input_tokens": 1000, "cache_read_input_tokens": 500, "output_tokens": 300})
    assert abs(ledger.get(tok).spent_usd - want) < 1e-12


def test_openai_compatible_provider_and_budget_cutoff():
    seen: list = []
    c, ledger = _app(seen, env={"PULLWRIGHT_PRICES": ""})
    from pullwright.pricing import register_prices

    register_prices({"openai:gpt-t": {"in": 1000.0, "out": 1000.0}})  # $0.11 a call
    tok = ledger.issue("j", cap_usd=0.15)
    body = {"model": "gpt-t", "messages": [{"role": "user", "content": "x"}]}
    r = c.post("/llm/openai/chat/completions", json=body, headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and seen[0]["headers"]["authorization"] == "Bearer sk-oai"
    assert seen[0]["url"] == "https://api.openai.com/v1/chat/completions"
    assert c.post("/llm/openai/chat/completions", json=body, headers={"authorization": f"Bearer {tok}"}).status_code == 200
    # the cap is spent: the next call is refused before it reaches the provider
    r = c.post("/llm/openai/chat/completions", json=body, headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 402 and "budget" in r.json()["error"]["message"] and len(seen) == 2


def test_tokens_are_required_and_revocable():
    seen: list = []
    c, ledger = _app(seen)
    assert c.post("/llm/anthropic/v1/messages", json={}, headers={"x-api-key": "sk-real"}).status_code == 401  # a real key is not a job token
    tok = ledger.issue("j", 1.0)
    assert ledger.revoke(tok).job == "j"
    assert c.post("/llm/anthropic/v1/messages", json={}, headers={"x-api-key": tok}).status_code == 401
    tok = ledger.issue("j", 1.0)
    assert c.post("/llm/nope/chat/completions", json={}, headers={"authorization": f"Bearer {tok}"}).status_code == 404
    assert c.post("/llm/openai/chat/completions", json={"stream": True}, headers={"authorization": f"Bearer {tok}"}).status_code == 400
    assert not seen


def test_runner_env_keeps_keys_out_when_proxied(tmp_path):
    providers = tmp_path / "p.json"
    providers.write_text('{"vllm": {"base_url": "http://gpu/v1", "api_key_env": "VLLM_KEY"}}')
    environ = {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o", "VLLM_KEY": "v", "UNRELATED_SECRET": "x",
               "PULLWRIGHT_MODEL_WORKER": "openai:gpt-x", "PULLWRIGHT_PROVIDERS": str(providers), "PULLWRIGHT_MAX_ROUNDS": "3"}
    direct = runner_env(environ, None)
    assert {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "VLLM_KEY"} <= set(direct) and "UNRELATED_SECRET" not in direct
    assert direct["PULLWRIGHT_PROVIDERS"].startswith("{")  # the file's JSON, since the runner can't read the control plane's disk
    proxied = runner_env(environ, ("http://cp/llm", "pwj_t"))
    assert not {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "VLLM_KEY"} & set(proxied)
    assert proxied["PULLWRIGHT_LLM_TOKEN"] == "pwj_t" and proxied["PULLWRIGHT_MODEL_WORKER"] == "openai:gpt-x"
