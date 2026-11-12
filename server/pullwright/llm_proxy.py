"""The control plane's LLM proxy: runners never hold a provider key, and a job's dollar cap is enforced here.

The dispatcher issues each job a token with a cap. The runner reaches every provider through
`<PULLWRIGHT_LLM_PROXY>/<provider>` with that token (providers.Router does this when PULLWRIGHT_LLM_PROXY is set):

    POST /llm/anthropic/v1/messages          Anthropic Messages, streamed or not
    POST /llm/<provider>/chat/completions    any OpenAI-compatible provider in providers.load_provider_configs()

The proxy swaps the token for the real key, forwards the request, reads the usage from the response and charges
the job. Once a job has spent its cap, further calls get HTTP 402 and the run stops on a budget error. A call that
is already in flight can overshoot the cap by one response.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .pricing import cost_usd, price
from .providers import ProviderConfig, load_provider_configs
from .providers.openai_compat import usage_from

ANTHROPIC_URL = "https://api.anthropic.com"
FORWARD_HEADERS = ("anthropic-version", "anthropic-beta", "content-type", "accept")


@dataclass
class Grant:
    token: str
    job: str
    cap_usd: float
    expires_at: float
    spent_usd: float = 0.0
    calls: int = 0
    by_model: dict[str, dict[str, float]] = field(default_factory=dict)  # model -> {cost_usd, calls, in, out, unpriced}

    def to_dict(self) -> dict[str, Any]:
        return {"job": self.job, "cap_usd": self.cap_usd, "spent_usd": round(self.spent_usd, 6), "calls": self.calls, "by_model": self.by_model}


class Ledger:
    """Job tokens and what each job has spent. In memory: a control-plane restart invalidates running jobs' tokens."""

    def __init__(self, ttl_s: float = 4 * 3600) -> None:
        self.ttl_s = ttl_s
        self._grants: dict[str, Grant] = {}
        self._lock = threading.Lock()

    def issue(self, job: str, cap_usd: float) -> str:
        token = "pwj_" + secrets.token_urlsafe(32)
        with self._lock:
            self._grants[token] = Grant(token, job, cap_usd, time.time() + self.ttl_s)
        return token

    def get(self, token: str) -> Grant | None:
        with self._lock:
            g = self._grants.get(token)
            if g is not None and g.expires_at < time.time():
                del self._grants[token]
                return None
            return g

    def revoke(self, token: str) -> Grant | None:
        with self._lock:
            return self._grants.pop(token, None)

    def charge(self, token: str, model: str, usage: Mapping[str, int]) -> float:
        c = cost_usd(model, dict(usage))
        with self._lock:
            g = self._grants.get(token)
            if g is None:
                return c
            g.spent_usd += c
            g.calls += 1
            m = g.by_model.setdefault(model, {"cost_usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0, "unpriced": 0})
            m["cost_usd"] += c
            m["calls"] += 1
            m["input_tokens"] += int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0)) + int(usage.get("cache_creation_input_tokens", 0))
            m["output_tokens"] += int(usage.get("output_tokens", 0))
            if price(model) is None:
                m["unpriced"] = 1
        return c


def _token(headers: Mapping[str, str]) -> str:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("x-api-key", "").strip()


def _anthropic_usage(u: Mapping[str, Any] | None) -> dict[str, int]:
    u = u or {}
    return {k: int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}


class _SSEUsage:
    """Reads usage out of an Anthropic event stream as it passes through: message_start carries the input side,
    message_delta the running output count."""

    def __init__(self) -> None:
        self.buf = b""
        self.model = ""
        self.usage = _anthropic_usage(None)

    def feed(self, chunk: bytes) -> None:
        self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            if not line.startswith(b"data:"):
                continue
            try:
                ev = json.loads(line[5:].strip() or b"{}")
            except ValueError:
                continue
            if ev.get("type") == "message_start":
                msg = ev.get("message") or {}
                self.model = msg.get("model", self.model)
                self.usage.update({k: v for k, v in _anthropic_usage(msg.get("usage")).items() if v})
            elif ev.get("type") == "message_delta":
                for k, v in _anthropic_usage(ev.get("usage")).items():
                    if v:
                        self.usage[k] = v


def build_proxy(ledger: Ledger, env: Mapping[str, str] | None = None, transport: httpx.AsyncBaseTransport | None = None) -> Any:

    env = dict(os.environ if env is None else env)
    configs: dict[str, ProviderConfig] = load_provider_configs(env)
    anthropic_url = env.get("PULLWRIGHT_ANTHROPIC_UPSTREAM", ANTHROPIC_URL).rstrip("/")
    router = APIRouter(prefix="/llm")

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(900, connect=20), transport=transport)

    def deny(status: int, msg: str, kind: str = "authentication_error") -> JSONResponse:
        # Anthropic and OpenAI SDKs both surface `error.message`
        return JSONResponse({"type": "error", "error": {"type": kind, "message": msg}}, status_code=status)

    def admit(request: Request) -> Grant | JSONResponse:
        g = ledger.get(_token(request.headers))
        if g is None:
            return deny(401, "unknown or expired Pullwright job token")
        if g.spent_usd >= g.cap_usd:
            return deny(402, f"job budget exhausted: ${g.spent_usd:.2f} of ${g.cap_usd:.2f}", "budget_exceeded")
        return g

    @router.post("/anthropic/v1/messages")
    async def anthropic_messages(request: Request) -> Any:
        g = admit(request)
        if not isinstance(g, Grant):
            return g
        key = env.get("ANTHROPIC_API_KEY", "")
        if not key:
            return deny(503, "the control plane has no ANTHROPIC_API_KEY", "api_error")
        body = await request.body()
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_HEADERS}
        headers["x-api-key"] = key
        streaming = bool(json.loads(body or b"{}").get("stream"))
        http = client()
        req = http.build_request("POST", f"{anthropic_url}/v1/messages", content=body, headers=headers,
                                 params=dict(request.query_params))
        upstream = await http.send(req, stream=True)
        if not streaming or upstream.status_code >= 400:
            data = await upstream.aread()
            await upstream.aclose()
            await http.aclose()
            if upstream.status_code < 400:
                msg = json.loads(data)
                ledger.charge(g.token, msg.get("model", ""), _anthropic_usage(msg.get("usage")))
            return Response(data, status_code=upstream.status_code, media_type=upstream.headers.get("content-type"))

        meter = _SSEUsage()

        async def relay() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_bytes():  # decoded: content-encoding is not forwarded
                    meter.feed(chunk)
                    yield chunk
            finally:
                await upstream.aclose()
                await http.aclose()
                ledger.charge(g.token, meter.model, meter.usage)

        return StreamingResponse(relay(), status_code=upstream.status_code, media_type="text/event-stream")

    @router.post("/{provider}/chat/completions")
    async def chat_completions(provider: str, request: Request) -> Any:
        g = admit(request)
        if not isinstance(g, Grant):
            return g
        cfg = configs.get(provider.lower())
        if cfg is None or cfg.kind != "openai" or not cfg.base_url:
            return deny(404, f"unknown provider {provider!r}", "not_found_error")
        payload = await request.json()
        if payload.get("stream"):
            return deny(400, "streaming is not supported through the Pullwright proxy", "invalid_request_error")
        headers = {"content-type": "application/json", **cfg.headers}
        if cfg.api_key_env:
            key = env.get(cfg.api_key_env, "")
            if not key:
                return deny(503, f"the control plane has no {cfg.api_key_env}", "api_error")
            headers["authorization"] = f"Bearer {key}"
        async with client() as http:
            r = await http.post(f"{cfg.base_url.rstrip('/')}/chat/completions", json=payload, headers=headers)
        if r.status_code < 400:
            ledger.charge(g.token, f"{cfg.name}:{payload.get('model', '')}", usage_from(r.json().get("usage")))
        return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))

    return router
