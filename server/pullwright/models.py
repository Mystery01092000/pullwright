"""Default models per role, and which models accept which request options.

The cost model: Haiku workers do the volume (reading, editing, running commands); Sonnet plans,
supervises and verifies. A repository can override any role's model in .pullwright/agents/<role>.md.
"""
from __future__ import annotations

import os

# Any role can run on any provider's model: `provider:model` (see pullwright/providers). Set the defaults with
# PULLWRIGHT_MAIN_MODEL / PULLWRIGHT_WORKER_MODEL, one role with PULLWRIGHT_MODEL_<ROLE> (PULLWRIGHT_MODEL_VERIFIER=
# gemini:gemini-3-pro), or one repository's role in .pullwright/agents/<role>.md (`model:` frontmatter, wins).
MAIN_MODEL = os.environ.get("PULLWRIGHT_MAIN_MODEL") or "claude-sonnet-5-5"  # describer, verifier, supervisor, diagnosis, summarizer
WORKER_MODEL = os.environ.get("PULLWRIGHT_WORKER_MODEL") or "claude-haiku-5-5"  # workers spawned by the supervisor


def role_model(role: str) -> str | None:
    """The deployment's model for one role (PULLWRIGHT_MODEL_<ROLE>), if set."""
    return os.environ.get(f"PULLWRIGHT_MODEL_{role.upper()}") or None

# Models that accept the server-side refusal fallback (`fallbacks: "default"` + its beta).
# Claude Haiku 5.5 has none, so a request to it must not carry the parameter.
FALLBACK_MODELS = frozenset({
    "claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5",
})


def supports_fallbacks(model: str) -> bool:
    return model.removeprefix("anthropic:") in FALLBACK_MODELS
