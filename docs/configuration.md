# Configuration

There are two layers:
- **Environment variables** configure the control plane and runners (`server/.env` is loaded automatically).
- **A `.pullwright/` folder** in each target repository configures how agents behave there.

## Environment variables

### Required

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Model access for the control plane (planning) and, passed through, for runners. Other providers' keys (`OPENAI_API_KEY`, `GEMINI_API_KEY`, ...) work the same way; see [providers](providers.md). |
| `PULLWRIGHT_SECRET` | Any long random string. It signs web session cookies and account-link tokens. Changing it logs everyone out and invalidates outstanding links. |
| `PULLWRIGHT_PUBLIC_URL` | The URL people reach the control plane at, e.g. `https://pullwright.arcane.tech`. It is used for OAuth callbacks and link URLs. Default `http://localhost:8765`. |
| `GITHUB_APP_ID` + `GITHUB_APP_PRIVATE_KEY_FILE` (or `GITHUB_APP_PRIVATE_KEY`) | The GitHub App ([setup](setup-github-app.md)). |
| `GITHUB_TOKEN` | Instead of an App, for local development only: a fine-grained personal token with contents, issues and pull requests (read/write) on the target repositories. |

### Web login

| Variable | Purpose |
|---|---|
| `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET` | A GitHub OAuth app whose callback URL is `$PULLWRIGHT_PUBLIC_URL/auth/github/callback`. Without it the web UI cannot log in, and Slack and Teams users cannot link accounts. |

### Repositories and the queue

| Variable | Default | Purpose |
|---|---|---|
| `PULLWRIGHT_REPOS` | (none) | Comma-separated `owner/name`. The dispatcher watches these, tasks for other repositories are refused, and the knowledge base profiles these to pick a repository when a task names none. If it's empty, the dispatcher does not run. |
| `PULLWRIGHT_MAX_PARALLEL` | `1` | How many issues are implemented at once. |
| `PULLWRIGHT_POLL_SECONDS` | `30` | How often the dispatcher looks for queued issues and PR feedback. |
| `PULLWRIGHT_FEEDBACK_QUIET_SECONDS` | `120` | After the newest review comment on a Pullwright PR, how long to wait before a follow-up starts (lets a reviewer finish). |
| `PULLWRIGHT_FEEDBACK_TRIGGER` | `any` | `any`: every comment from someone with write access is feedback; `mention`: only comments that mention `@pullwright`. A comment starting with `@pullwright ignore` never counts. |
| `PULLWRIGHT_DISPATCHER` | `on` | Set to `off` to run a planning-only control plane. |
| `PULLWRIGHT_RUNNER` | `docker` | `docker` (a container per issue) or `local` (a subprocess on this machine with **no isolation**; for development). |
| `PULLWRIGHT_RUNNER_IMAGE` | `ghcr.io/mystery01092000/pullwright-runner:latest` | The image for Docker runners ([runners](runners.md)). |

### Models, providers and the LLM proxy ([details](providers.md))

| Variable | Default | Meaning |
|---|---|---|
| `PULLWRIGHT_MAIN_MODEL` | `claude-sonnet-5-5` | Model for the main roles. Takes `provider:model`, such as `openai:<model>`. |
| `PULLWRIGHT_WORKER_MODEL` | `claude-haiku-5-5` | Model for workers. |
| `PULLWRIGHT_MODEL_<ROLE>` | unset | Model for one role, for example `PULLWRIGHT_MODEL_VERIFIER`. |
| `PULLWRIGHT_PROVIDERS` | unset | JSON, inline or as a file, that defines or tunes providers. |
| `PULLWRIGHT_PRICES` | unset | JSON, inline or as a file, with prices for models outside Anthropic. |
| `PULLWRIGHT_LLM_PROXY_URL` | unset | Set it to route runners through the control plane's LLM proxy, so no provider key enters a runner. |

### Cost and effort caps

| Variable | Default | Applies to |
|---|---|---|
| `PULLWRIGHT_PLAN_MAX_COST_USD` | `1` | describer + verifier, per plan (all revisions) |
| `PULLWRIGHT_MAX_COST_USD` | `5` | supervisor + workers + reviewer, per issue |
| `PULLWRIGHT_MAX_ROUNDS` | `4` | supervisor → checks rounds per issue |

### Slack ([setup](setup-slack.md))

| Variable | Purpose |
|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-...` |
| `SLACK_APP_TOKEN` | `xapp-...` with `connections:write` (Socket Mode) |
| `PULLWRIGHT_SLACK_REPOS` | Optional JSON mapping a channel id to its default repository: `{"C0123ABC": "arcane/app"}` |

### Microsoft Teams ([setup](setup-teams.md))

| Variable | Purpose |
|---|---|
| `PULLWRIGHT_TEAMS_APP_ID` | The Azure Bot's Microsoft App ID |
| `PULLWRIGHT_TEAMS_APP_PASSWORD` | Its client secret |
| `PULLWRIGHT_TEAMS_TENANT_ID` | For single-tenant bots |
| `PULLWRIGHT_TEAMS_REPOS` | Optional JSON mapping a Teams channel id to its default repository |

### Storage and server

`PULLWRIGHT_DB` is a SQLite file path (the default, `pullwright.db`) or a Postgres URL (`postgresql://user:pass@host:5432/pullwright`). Postgres needs the `postgres` extra (`uv sync --extra postgres`, already in the control-plane image). Tables are created on first start.


| Variable | Default | Purpose |
|---|---|---|
| `PULLWRIGHT_DB` | `pullwright.db` | SQLite file, or a Postgres URL |
| `PULLWRIGHT_WORKDIR` | `.pullwright-work` | Cached read-only clones used for planning |
| `PULLWRIGHT_WEB_DIST` | `web/dist` | The built UI, served at `/` |
| `PULLWRIGHT_HOST`, `PULLWRIGHT_PORT` | `0.0.0.0`, `8765` | Listen address |
| `PULLWRIGHT_LOG_LEVEL` | `INFO` | |

## The `.pullwright/` folder (per repository)

Put it at the root of a target repository. Every file is optional. Anything missing falls back to the defaults shipped in `server/pullwright/defaults/`. The older name `.harness/` is still read. A complete example is in [`examples/.pullwright/`](../examples/.pullwright/).

```
.pullwright/
  checks.json             setup and checks for the runner
  instructions.md         appended to every agent's system prompt (conventions, things to avoid)
  agents/<role>.md        replace a role's model, effort, tools, limits or prompt
  guardrails.json         command deny / ask / allow rules for bash
  hooks.json              shell hooks on tool calls and state changes
  skills/<name>/SKILL.md  instructions agents load on demand
```

### `checks.json`

```json
{
  "setup": ["uv sync"],
  "lint": "uv run ruff check .",
  "typecheck": "uv run mypy src",
  "test": "uv run pytest -q",
  "build": null
}
```

- `setup` runs once after the clone. It is a string or a list, and `null` means none.
- Every other key is a check that must exit 0. Order: build, lint, typecheck, test, then any others alphabetically.
- `null` removes a check that was detected automatically.
- Without this file, checks are detected from `pyproject.toml`/`uv.lock`, `package.json` scripts (with npm, pnpm, yarn or bun from the lockfile), `go.mod`, `Cargo.toml`, `pom.xml` or `build.gradle`.

### `agents/<role>.md`

Roles: `describer`, `verifier`, `supervisor`, `worker`, `diagnosis`, and for local mode `intake` and `summarizer`.

```markdown
---
model: claude-sonnet-5-5
effort: high
tools: read_file, glob, grep, bash
max_turns: 30
max_cost_usd: 2
---
You are a software engineering worker on a Django codebase. ...
```

If `tools:` is omitted, the role gets every tool (MCP tools included). If `tools:` is empty, it gets none.

### `guardrails.json`

```json
{ "deny": ["\\bterraform\\s+apply\\b"], "ask": ["\\brm\\b"], "allow": ["^go test"], "bash_timeout": 300 }
```

- **`deny`:** the command never runs.
- **`ask`:** the command needs approval. On a runner there is no human, so `ask` commands run, because the container is the isolation. Locally they pause for you.
- **`allow`:** always runs, even if it matches `ask`.

On runners, `git push` and `gh` are always denied to agents.

### `hooks.json`

```json
[{ "event": "pre_tool", "match": "write_file|edit_file", "command": "python scripts/protect_generated.py" }]
```

- Hooks receive the event as JSON on stdin.
- A `pre_tool` hook that exits non-zero blocks the call, and its stderr is returned to the agent as the reason.
- `post_tool` and `state` hooks are fire-and-forget.

### `skills/`

Each skill's `description` (in its `SKILL.md` frontmatter) is listed in every agent's prompt. Agents call `load_skill(name)` to read the full instructions when they need them.
