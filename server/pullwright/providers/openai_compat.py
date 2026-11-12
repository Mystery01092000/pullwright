"""OpenAI-compatible Chat Completions (OpenAI, Gemini, OpenRouter, Groq, DeepSeek, Mistral, xAI, Together,
Ollama, vLLM...). The loop's Anthropic-shaped history is translated per request; the answer comes back as
Anthropic-shaped blocks so the loop needs no provider branches."""
from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace as NS
from typing import Any

import httpx

from . import ProviderConfig, ProviderError, Turn

STOP = {"stop": "end_turn", "tool_calls": "tool_use", "function_call": "tool_use", "length": "max_tokens", "content_filter": "refusal"}
EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


def _get(block: Any, key: str, default: Any = None) -> Any:
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(_get(b, "text", "") for b in content or [] if _get(b, "type") == "text")


def to_openai_messages(system: str, messages: list[Any]) -> list[dict[str, Any]]:
    """Anthropic-shaped history -> Chat Completions messages. Thinking blocks are dropped: they belong to
    the provider that wrote them."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        role, content = m["role"], m["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text = "".join(_get(b, "text", "") for b in content if _get(b, "type") == "text")
            calls = [{"id": _get(b, "id"), "type": "function",
                      "function": {"name": _get(b, "name"), "arguments": json.dumps(_get(b, "input") or {})}}
                     for b in content if _get(b, "type") == "tool_use"]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        texts = []
        for b in content:
            t = _get(b, "type")
            if t == "tool_result":
                body = _result_text(_get(b, "content"))
                out.append({"role": "tool", "tool_call_id": _get(b, "tool_use_id"),
                            "content": f"ERROR: {body}" if _get(b, "is_error") else body})
            elif t == "text":
                texts.append(_get(b, "text", ""))
        if texts:
            out.append({"role": "user", "content": "\n".join(texts)})
    return out


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"]}}
            for t in tools]


def from_openai_message(msg: dict[str, Any]) -> list[NS]:
    blocks: list[NS] = []
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        blocks.append(NS(type="thinking", thinking=reasoning))
    content = msg.get("content")
    if isinstance(content, list):  # some servers return parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if content:
        blocks.append(NS(type="text", text=content))
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        args = fn.get("arguments") or "{}"
        try:
            inp = json.loads(args) if isinstance(args, str) else dict(args)
        except (json.JSONDecodeError, TypeError, ValueError):
            inp = {"_unparsed_arguments": args}  # the tool rejects it and the model sees why
        blocks.append(NS(type="tool_use", id=c.get("id") or f"call_{uuid.uuid4().hex[:12]}", name=fn.get("name", ""), input=inp))
    return blocks


def usage_from(u: dict[str, Any] | None) -> dict[str, int]:
    u = u or {}
    prompt = int(u.get("prompt_tokens") or 0)
    cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or u.get("prompt_cache_hit_tokens") or 0)
    return {"input_tokens": max(prompt - cached, 0), "output_tokens": int(u.get("completion_tokens") or 0),
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": cached}


class OpenAICompatProvider:
    def __init__(self, cfg: ProviderConfig, base_url: str, api_key: str, http: httpx.Client | None = None, retries: int = 3) -> None:
        self.cfg = cfg
        self.url = base_url.rstrip("/") + "/chat/completions"
        headers = {"content-type": "application/json", **cfg.headers}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        self.http = http or httpx.Client(timeout=httpx.Timeout(600, connect=20))
        self.headers = headers
        self.retries = retries

    def request_body(self, agent: Any, model: str, messages: list[Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "messages": to_openai_messages(agent.system, messages)}
        if tools:
            body["tools"] = to_openai_tools(tools)
        limit = self.cfg.max_output_tokens
        if limit:
            body[self.cfg.max_tokens_param] = min(limit, agent.max_tokens)
        if self.cfg.reasoning_effort:
            body["reasoning_effort"] = EFFORT.get(agent.effort, "medium")
        if agent.output_schema:
            if self.cfg.json_schema:
                body["response_format"] = {"type": "json_schema", "json_schema": {"name": "output", "schema": agent.output_schema}}
            else:
                body["messages"][0]["content"] += ("\n\nYour final answer must be only JSON matching this schema:\n"
                                                   + json.dumps(agent.output_schema))
        return body

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        delay = 2.0
        for attempt in range(self.retries + 1):
            try:
                r = self.http.post(self.url, headers=self.headers, json=body)
            except httpx.TransportError as e:
                if attempt == self.retries:
                    raise ProviderError(f"{self.cfg.name}: {type(e).__name__}: {e}") from e
            else:
                if r.status_code < 400:
                    return r.json()
                if r.status_code not in RETRY_STATUS or attempt == self.retries:
                    raise ProviderError(f"{self.cfg.name} HTTP {r.status_code}: {r.text[:2000]}")
                ra = r.headers.get("retry-after")
                if ra and ra.replace(".", "", 1).isdigit():
                    delay = min(float(ra), 60.0)
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
        raise AssertionError("unreachable")

    def turn(self, agent: Any, model: str, messages: list[Any], tools: list[dict[str, Any]], emit: Any) -> Turn:
        data = self._post(self.request_body(agent, model, messages, tools))
        if data.get("error"):
            raise ProviderError(f"{self.cfg.name}: {data['error']}")
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.cfg.name}: response has no choices: {str(data)[:500]}")
        choice = choices[0]
        blocks = from_openai_message(choice.get("message") or {})
        for b in blocks:
            if b.type == "thinking":
                emit("thinking", delta=b.thinking)
            elif b.type == "text":
                emit("text", delta=b.text)
        stop = STOP.get(choice.get("finish_reason") or "stop", "end_turn")
        if stop == "end_turn" and any(b.type == "tool_use" for b in blocks):
            stop = "tool_use"  # some servers say "stop" next to tool calls
        return Turn(blocks, stop, usage_from(data.get("usage")), f"{self.cfg.name}:{model}")
