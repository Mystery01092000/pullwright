"""Single-agent loop: stream a turn, run tools, append, repeat. History is append-only (preserved thinking)."""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from .events import Event, EventBus
from .models import MAIN_MODEL
from .pricing import cost_usd, price
from .providers import as_router
from .providers.anthropic import FALLBACK_BETA  # noqa: F401  (re-exported for callers of the old name)
from .tools import ALL_TOOLS, TOOLS, Registry, ToolContext, execute, schemas


@dataclass
class AgentDef:
    name: str
    system: str
    model: str = MAIN_MODEL
    effort: str = "high"  # low | medium | high | xhigh | max
    tools: list[str] = field(default_factory=lambda: list(ALL_TOOLS))
    max_turns: int = 50
    max_tokens: int = 64_000  # per response
    max_tokens_total: int = 2_000_000  # in + out across the run
    max_cost_usd: float = 10.0
    output_schema: dict[str, Any] | None = None  # structured output: final text is JSON matching this
    extra_tools: Registry = field(default_factory=dict)  # per-run tools (e.g. spawn_worker), merged over TOOLS


@dataclass
class RunResult:
    text: str
    stop: str  # end_turn | refusal | max_turns | budget | truncated
    messages: list[Any]
    usage: dict[str, int]
    cost_usd: float


def run_agent(
    client: Any,
    agent: AgentDef,
    messages: list[Any],
    ctx: ToolContext,
    bus: EventBus,
    *,
    run_id: str,
    agent_id: str | None = None,
    parent_id: str | None = None,
    cancel: threading.Event | None = None,
) -> RunResult:
    agent_id = agent_id or f"{agent.name}-{uuid.uuid4().hex[:6]}"
    total = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    cost = 0.0
    text = ""
    stop = "max_turns"

    def emit(type_: str, turn: int | None = None, **payload: Any) -> None:
        bus.emit(Event(type_, run_id, agent_id, payload, parent_id, turn))

    emit("agent_start", name=agent.name, model=agent.model, effort=agent.effort, tools=agent.tools)
    registry: Registry = {**TOOLS, **agent.extra_tools}
    provider, model_id = as_router(client).provider(agent.model)
    tools = schemas(agent.tools, registry)

    for turn in range(1, agent.max_turns + 1):
        if cancel is not None and cancel.is_set():
            stop = "cancelled"
            break
        resp = provider.turn(agent, model_id, messages, tools, lambda t, **p: emit(t, turn, **p))  # noqa: B023

        messages.append({"role": "assistant", "content": resp.content})
        u = resp.usage
        c = cost_usd(resp.model, u)
        cost += c
        for k in total:
            total[k] += u[k]
        emit("usage", turn, model=resp.model, cost_usd=c, total_cost_usd=cost, priced=price(resp.model) is not None, **u)
        text = "".join(b.text for b in resp.content if b.type == "text")

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason == "refusal":
            stop = "refusal"
            emit("error", turn, reason="refusal", category=getattr(resp.stop_details, "category", None))
            break
        if resp.stop_reason == "pause_turn":
            continue
        if resp.stop_reason == "max_tokens" and tool_uses:
            stop = "truncated"
            emit("error", turn, reason="tool input truncated by max_tokens")
            break
        if not tool_uses:
            stop = "end_turn"
            break

        results = []
        for tu in tool_uses:
            emit("tool_call", turn, tool_use_id=tu.id, name=tu.name, input=tu.input)
            out, is_err = execute(ctx, tu.name, tu.input, agent.tools, registry)
            emit("tool_result", turn, tool_use_id=tu.id, name=tu.name, is_error=is_err, output=out[:4000])
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out, "is_error": is_err})
        messages.append({"role": "user", "content": results})

        if total["input_tokens"] + total["output_tokens"] > agent.max_tokens_total or cost > agent.max_cost_usd:
            stop = "budget"
            emit("error", turn, reason="budget exceeded", tokens=total, cost_usd=cost)
            break

    emit("agent_end", stop=stop, cost_usd=cost, **total)
    return RunResult(text, stop, messages, total, cost)
