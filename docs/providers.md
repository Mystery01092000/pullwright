# Models, providers and the LLM proxy

## Any role on any provider

Every agent role (describer, verifier, supervisor, worker, diagnosis, summarizer, plus the repository router) can run on a model from any provider. Pullwright's own loop, tools, guardrails, budgets and traces stay the same, whatever model a role uses. You can run a Sonnet supervisor with GPT workers and a Gemini verifier.

A model is named `provider:model`. A bare name is an Anthropic model.

| Provider | Example model name | Key variable | Endpoint |
|---|---|---|---|
| `anthropic` (default) | `claude-sonnet-5-5` | `ANTHROPIC_API_KEY` | Messages API: streamed, adaptive thinking, prompt caching, effort, refusal fallback |
| `openai` | `openai:<model>` | `OPENAI_API_KEY` | Chat Completions |
| `gemini` | `gemini:<model>` | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | Gemini's OpenAI-compatible endpoint |
| `openrouter` | `openrouter:<vendor>/<model>` | `OPENROUTER_API_KEY` | |
| `groq`, `deepseek`, `mistral`, `xai`, `together` | `groq:<model>` and so on | `<NAME>_API_KEY` | |
| `ollama` | `ollama:qwen3-coder:30b` | none (`OLLAMA_BASE_URL`, default `http://localhost:11434/v1`) | local models |
| your own | `vllm:<model>` | defined by you | any OpenAI-compatible server |

### Choosing models

The most specific setting wins:

1. `model:` in `.pullwright/agents/<role>.md` (one repository, one role)
2. `PULLWRIGHT_MODEL_<ROLE>`, for example `PULLWRIGHT_MODEL_VERIFIER=gemini:gemini-3-pro` (the deployment, one role)
3. `PULLWRIGHT_MAIN_MODEL` / `PULLWRIGHT_WORKER_MODEL` (the deployment's defaults: main roles and workers)
4. the built-ins: Claude Sonnet 5.5 for main roles, Claude Haiku 5.5 for workers

### Defining or tuning providers

`PULLWRIGHT_PROVIDERS` holds JSON inline or names a JSON file. Each entry adds a new provider or changes a preset:

```json
{
  "vllm": {"base_url": "http://gpu-box:8000/v1", "api_key_env": "VLLM_KEY",
           "prices": {"qwen3-coder": {"in": 0.2, "out": 0.8, "cache_read": 0.02}}},
  "openai": {"reasoning_effort": true, "max_output_tokens": 32000}
}
```

| Field | Default | Meaning |
|---|---|---|
| `base_url` | preset | The OpenAI-compatible base URL (the part before `/chat/completions`). |
| `api_key_env` | preset | The name of the variable that holds the key. `null` means no key. |
| `max_output_tokens` | unset | Sent as the response limit. When unset, the provider's own default for the model applies. |
| `max_tokens_param` | `max_tokens` | `openai` uses `max_completion_tokens`. |
| `reasoning_effort` | `false` | Sends the role's effort as `reasoning_effort`. Turn it on only for reasoning models, because other models reject it. |
| `json_schema` | `true` | Structured output through `response_format`. When `false`, the schema goes into the system prompt instead. |
| `headers` | `{}` | Extra request headers. |
| `prices` | `{}` | USD per million tokens (`in`, `out`, `cache_read`) for this provider's models. |

### Prices

Anthropic models are priced out of the box. Models from other providers are priced under their full name, set either in a provider's `prices` or in `PULLWRIGHT_PRICES` (inline JSON or a file: `{"openai:gpt-x": {"in": 1.25, "out": 10, "cache_read": 0.125}}`). An unpriced model counts as $0, and its usage events carry `priced: false`, so the web cost view can flag it. Unpriced models also make dollar caps meaningless, so price every model you use.

### What differs between providers

- Thinking: only Anthropic models get adaptive thinking. Providers that return reasoning text (`reasoning_content`) have it shown in the trace, but it is not sent back to them.
- Prompt caching: Anthropic caching is explicit. On other providers, cached prompt tokens are read from `prompt_tokens_details.cached_tokens` and priced as cache reads.
- Effort: Anthropic's `effort` applies. On other providers it becomes `reasoning_effort` only if the provider has `reasoning_effort: true` (`xhigh` and `max` map to `high`).
- Streaming: Anthropic responses stream into the trace live. Other providers return each turn whole.

Measure before you switch a role: run the [evals](evals.md) with both models and compare pass rate and cost per completed task.

## The LLM proxy: no provider key in a runner

Without the proxy, the dispatcher passes the provider keys to each runner (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and so on, only those of configured providers). With the proxy, they stay on the control plane:

1. Set `PULLWRIGHT_LLM_PROXY_URL` to the address at which runners reach the control plane, followed by `/llm`. For Docker runners on the same host, use `http://host.docker.internal:8765/llm`, and on Linux also add `--add-host=host.docker.internal:host-gateway` to the runner.
2. For each job, the dispatcher issues a token whose cap is `PULLWRIGHT_MAX_COST_USD`, and passes the runner `PULLWRIGHT_LLM_PROXY` and `PULLWRIGHT_LLM_TOKEN` instead of any key.
3. The runner reaches every provider through `<proxy>/<provider>`: `/llm/anthropic/v1/messages` (streamed or not) and `/llm/<provider>/chat/completions`.
4. The proxy swaps the token for the real key, forwards the call and charges the job from the response's usage. Once a job has spent its cap, further calls get HTTP 402 and the run stops on a budget error. A call already in flight can overshoot the cap by one response.
5. When the job ends, the token is revoked. The proxy's spend record (per model: calls, tokens, dollars) is attached to the job outcome. A runner cannot alter this record.

Tokens live in the control plane's memory and expire after 4 hours, so restarting the control plane fails any running jobs. Streaming through `chat/completions` is refused, because Pullwright's runner doesn't use it.
