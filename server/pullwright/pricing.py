"""USD per million tokens. Cache write = 1.25x input. Source: Anthropic pricing, cached 2026-11-03."""
from __future__ import annotations

PRICES: dict[str, dict[str, float]] = {
    "claude-fable-5-2": {"in": 8.0, "out": 40.0, "cache_read": 0.20},
    "claude-fable-5-1": {"in": 10.0, "out": 50.0, "cache_read": 0.25},
    "claude-opus-5-5": {"in": 4.0, "out": 20.0, "cache_read": 0.20},
    "claude-opus-5": {"in": 5.0, "out": 25.0, "cache_read": 0.50},
    "claude-sonnet-5-5": {"in": 2.0, "out": 10.0, "cache_read": 0.10},
    "claude-sonnet-5": {"in": 2.0, "out": 10.0, "cache_read": 0.20},
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50, "cache_read": 0.01},
}


def cost_usd(model: str, usage: dict[str, int]) -> float:
    """usage keys: input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens."""
    p = PRICES.get(model)
    if p is None:
        return 0.0  # ponytail: unknown model costs 0; add to PRICES when you use one
    m = 1_000_000
    return (
        usage.get("input_tokens", 0) * p["in"]
        + usage.get("output_tokens", 0) * p["out"]
        + usage.get("cache_creation_input_tokens", 0) * p["in"] * 1.25
        + usage.get("cache_read_input_tokens", 0) * p["cache_read"]
    ) / m
