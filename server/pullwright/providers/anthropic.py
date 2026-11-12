"""The Anthropic Messages API: streamed, adaptive thinking, prompt caching, effort and the refusal fallback."""
from __future__ import annotations

from typing import Any

from ..models import supports_fallbacks
from . import Turn

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _usage(u: Any) -> dict[str, int]:
    return {k: int(getattr(u, k, 0) or 0)
            for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}


class AnthropicProvider:
    def __init__(self, client: Any, native: bool = True) -> None:
        self.client = client
        self.native = native  # the first-party API (fallbacks are a first-party beta)

    def turn(self, agent: Any, model: str, messages: list[Any], tools: list[dict[str, Any]], emit: Any) -> Turn:
        output_config: dict[str, Any] = {"effort": agent.effort}
        if agent.output_schema:
            output_config["format"] = {"type": "json_schema", "schema": agent.output_schema}
        # Server-side refusal fallback where the model has one (Claude Haiku 5.5 does not).
        fallback: dict[str, Any] = (
            {"betas": [FALLBACK_BETA], "fallbacks": "default"} if self.native and supports_fallbacks(model) else {}
        )
        with self.client.beta.messages.stream(
            model=model,
            max_tokens=agent.max_tokens,
            system=[{"type": "text", "text": agent.system, "cache_control": {"type": "ephemeral"}}],
            cache_control={"type": "ephemeral"},  # also cache the growing conversation tail
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config=output_config,
            **fallback,
        ) as stream:
            for ev in stream:
                if ev.type == "content_block_delta":
                    if ev.delta.type == "thinking_delta" and ev.delta.thinking:
                        emit("thinking", delta=ev.delta.thinking)
                    elif ev.delta.type == "text_delta":
                        emit("text", delta=ev.delta.text)
            resp = stream.get_final_message()
        return Turn(list(resp.content), resp.stop_reason, _usage(resp.usage), resp.model, getattr(resp, "stop_details", None))
