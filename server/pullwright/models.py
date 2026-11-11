"""Default models per role, and which models accept which request options.

The cost model: Haiku workers do the volume (reading, editing, running commands); Sonnet plans,
supervises and verifies. A repository can override any role's model in .pullwright/agents/<role>.md.
"""
from __future__ import annotations

MAIN_MODEL = "claude-sonnet-5-5"  # describer, verifier, supervisor, diagnosis, summarizer
WORKER_MODEL = "claude-haiku-5-5"  # workers spawned by the supervisor

# Models that accept the server-side refusal fallback (`fallbacks: "default"` + its beta).
# Claude Haiku 5.5 has none, so a request to it must not carry the parameter.
FALLBACK_MODELS = frozenset({
    "claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5",
})


def supports_fallbacks(model: str) -> bool:
    return model in FALLBACK_MODELS
