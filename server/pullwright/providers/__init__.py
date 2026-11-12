"""Model providers: any role can run on a model from any provider.

A model is named `provider:model` (`openai:gpt-5.1`, `gemini:gemini-3-pro`, `ollama:qwen3-coder`). A bare
name is an Anthropic model (`claude-sonnet-5-5`). The agent loop keeps one conversation format, the
Anthropic Messages shape; each provider translates it on the way out and normalizes the answer back, so
tools, guardrails, budgets and traces work the same whichever model a role runs on.

Providers:
- `anthropic`: the Anthropic Messages API (the default).
- OpenAI-compatible Chat Completions endpoints, with presets for openai, gemini, openrouter, groq,
  deepseek, mistral, xai, together and ollama, plus any you define.

Define or override providers in a JSON file named by PULLWRIGHT_PROVIDERS (or inline JSON in that variable):

    {"vllm": {"base_url": "http://gpu-box:8000/v1", "api_key_env": "VLLM_KEY",
              "max_output_tokens": 8192, "reasoning_effort": false,
              "prices": {"qwen3-coder": {"in": 0.2, "out": 0.8, "cache_read": 0.02}}}}

When PULLWRIGHT_LLM_PROXY is set (a runner talking to the control plane's LLM proxy), every provider is
reached through `<proxy>/<provider>` with PULLWRIGHT_LLM_TOKEN as its key, so no provider key enters the runner.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..pricing import register_prices

ANTHROPIC = "anthropic"


@dataclass
class ProviderConfig:
    name: str
    kind: str = "openai"  # openai (Chat Completions) | anthropic
    base_url: str = ""
    api_key_env: str | None = None  # None: no key (ollama)
    max_tokens_param: str = "max_tokens"  # openai's newer models take max_completion_tokens
    max_output_tokens: int | None = None  # None: let the provider pick the model's default
    reasoning_effort: bool = False  # send reasoning_effort (only reasoning models accept it)
    json_schema: bool = True  # supports response_format json_schema
    headers: dict[str, str] = field(default_factory=dict)
    prices: dict[str, dict[str, float]] = field(default_factory=dict)


PRESETS: dict[str, ProviderConfig] = {
    ANTHROPIC: ProviderConfig(ANTHROPIC, kind="anthropic", api_key_env="ANTHROPIC_API_KEY"),
    "openai": ProviderConfig("openai", base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY",
                             max_tokens_param="max_completion_tokens"),
    "gemini": ProviderConfig("gemini", base_url="https://generativelanguage.googleapis.com/v1beta/openai", api_key_env="GEMINI_API_KEY"),
    "openrouter": ProviderConfig("openrouter", base_url="https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY"),
    "groq": ProviderConfig("groq", base_url="https://api.groq.com/openai/v1", api_key_env="GROQ_API_KEY"),
    "deepseek": ProviderConfig("deepseek", base_url="https://api.deepseek.com/v1", api_key_env="DEEPSEEK_API_KEY"),
    "mistral": ProviderConfig("mistral", base_url="https://api.mistral.ai/v1", api_key_env="MISTRAL_API_KEY"),
    "xai": ProviderConfig("xai", base_url="https://api.x.ai/v1", api_key_env="XAI_API_KEY"),
    "together": ProviderConfig("together", base_url="https://api.together.xyz/v1", api_key_env="TOGETHER_API_KEY"),
    "ollama": ProviderConfig("ollama", base_url="http://localhost:11434/v1", api_key_env=None, json_schema=True),
}


def parse_model(model: str) -> tuple[str, str]:
    """`openai:gpt-5.1` -> ("openai", "gpt-5.1"); `claude-sonnet-5-5` -> ("anthropic", "claude-sonnet-5-5")."""
    if ":" in model:
        provider, _, name = model.partition(":")
        if provider and name:
            return provider.lower(), name
    return ANTHROPIC, model


@dataclass
class Turn:
    """One model response, normalized: Anthropic-shaped content blocks (objects with .type), an Anthropic
    stop reason (end_turn | tool_use | max_tokens | refusal | pause_turn), token usage and the priced model name."""
    content: list[Any]
    stop_reason: str
    usage: dict[str, int]
    model: str
    stop_details: Any = None


class Provider(Protocol):
    def turn(self, agent: Any, model: str, messages: list[Any], tools: list[dict[str, Any]], emit: Any) -> Turn: ...


class ProviderError(RuntimeError):
    pass


def load_provider_configs(env: Mapping[str, str] | None = None) -> dict[str, ProviderConfig]:
    env = os.environ if env is None else env
    configs = {k: ProviderConfig(**{**v.__dict__}) for k, v in PRESETS.items()}
    if env.get("GOOGLE_API_KEY") and not env.get("GEMINI_API_KEY"):
        configs["gemini"].api_key_env = "GOOGLE_API_KEY"
    if env.get("OLLAMA_BASE_URL"):
        configs["ollama"].base_url = env["OLLAMA_BASE_URL"].rstrip("/")
    raw = env.get("PULLWRIGHT_PROVIDERS", "").strip()
    if raw:
        data = json.loads(raw if raw.startswith("{") else Path(raw).read_text(encoding="utf-8"))
        for name, spec in data.items():
            name = name.lower()
            base = configs.get(name, ProviderConfig(name))
            configs[name] = ProviderConfig(**{**base.__dict__, **spec, "name": name})
    for name, c in configs.items():
        if c.prices:
            register_prices({f"{name}:{m}": p for m, p in c.prices.items()})
    return configs


class Router:
    """Picks the provider for a model name. Takes the place of the bare anthropic client everywhere.

    `anthropic` may be an injected client (tests pass a scripted one); otherwise one is created on first use,
    so a run whose roles all use other providers needs no Anthropic key."""

    def __init__(self, anthropic: Any = None, env: Mapping[str, str] | None = None) -> None:
        self.env = dict(os.environ if env is None else env)
        self.configs = load_provider_configs(self.env)
        self._anthropic = anthropic
        self._providers: dict[str, Provider] = {}

    @property
    def proxy(self) -> tuple[str, str] | None:
        url = self.env.get("PULLWRIGHT_LLM_PROXY", "").rstrip("/")
        return (url, self.env.get("PULLWRIGHT_LLM_TOKEN", "")) if url else None

    def anthropic_client(self) -> Any:
        if self._anthropic is None:
            import anthropic

            proxy = self.proxy
            self._anthropic = anthropic.Anthropic(base_url=f"{proxy[0]}/anthropic", api_key=proxy[1]) if proxy else anthropic.Anthropic()
        return self._anthropic

    def provider(self, model: str) -> tuple[Provider, str]:
        name, model_id = parse_model(model)
        cfg = self.configs.get(name)
        if cfg is None:
            raise ProviderError(f"unknown provider {name!r} in model {model!r}; known: {', '.join(sorted(self.configs))}. "
                                "Define it in PULLWRIGHT_PROVIDERS.")
        if name not in self._providers:
            if cfg.kind == "anthropic":
                from .anthropic import AnthropicProvider

                self._providers[name] = AnthropicProvider(self.anthropic_client(), native=name == ANTHROPIC)
            else:
                from .openai_compat import OpenAICompatProvider

                proxy = self.proxy
                if proxy:
                    base_url, key = f"{proxy[0]}/{name}", proxy[1]
                else:
                    base_url, key = cfg.base_url, (self.env.get(cfg.api_key_env, "") if cfg.api_key_env else "")
                    if cfg.api_key_env and not key:
                        raise ProviderError(f"{cfg.api_key_env} is not set (needed for model {model!r})")
                if not base_url:
                    raise ProviderError(f"provider {name!r} has no base_url")
                self._providers[name] = OpenAICompatProvider(cfg, base_url, key)
        return self._providers[name], model_id


def as_router(client: Any) -> Router:
    return client if isinstance(client, Router) else Router(anthropic=client)
