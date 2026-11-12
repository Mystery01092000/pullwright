# Coding agents

The runner's agent steps ("implement the approved plan" and "fix these failing checks") can be done by:

| Coder | `PULLWRIGHT_CODER` | What it is |
|---|---|---|
| goose | `goose` (**default**) | The open-source agent (aaif-goose/goose), headless (`goose run`), pinned in the runner image |
| Claude Code | `claude-code` | Anthropic's coding agent, headless (`claude -p`), pinned in the runner image |
| Pullwright's own | `native` | A Sonnet supervisor that delegates to Haiku workers, with per-agent costs |

goose is the default because in the comparisons below it followed the organisation's skills most closely, at the same cost as Claude Code. Claude Code was 20-30% faster. Both external coders need the runner image (they're installed there). With `PULLWRIGHT_RUNNER=local`, install goose on the machine or set `PULLWRIGHT_CODER=native`.

Pullwright stays the workflow around the coder, whichever one runs. Stripe's Minions call this a *blueprint*: the setup, the deterministic checks, the independent review, commit, push and the PR are always Pullwright's. Only the agent steps change hands.

```
setup -> coder -> checks --fail--> coder (with the failures) -> checks --fail--> draft PR for a human
                    | pass
                    v
                 review (Pullwright's diagnosis agent) -> commit -> push -> PR
```

## Choosing

```bash
PULLWRIGHT_CODER=claude-code                                     # every repository (default: goose)
PULLWRIGHT_CODER_BY_REPO='{"acme/api": "goose", "acme/web": "native"}'   # per repository
PULLWRIGHT_CODER_MODEL=claude-sonnet-5-5                          # default: the supervisor's model
PULLWRIGHT_CODER_MAX_TURNS=120                                    # per step
```

## What an external coder gets

- **The approved plan and the checks** in its prompt, with the organisation's and the repository's instructions first, plus the runner rules: write tests first, run the checks before finishing, don't commit or push, stop background processes by PID.
- **The skills** from the organisation config and the repository, under its own `HOME` (`~/.claude/skills` for Claude Code, `~/.agents/skills` for goose), so it loads them the way it normally does.
- **The LLM proxy** as its Anthropic endpoint (`ANTHROPIC_BASE_URL` / `ANTHROPIC_HOST`), with the job token as its key. The job's dollar cap is enforced there on every call. Claude Code reports usage per message, which feeds the cost breakdown as the run goes. goose reports its totals at the end.
- **The shared caches** (npm, uv, Go). Its `HOME` is its own, but the cache variables still point at `/home/runner/.cache`.

What it does not get:

- **GitHub credentials.** The GitHub token and the token-refresh secret are removed from its environment. Pullwright's own agents' commands run without them too.
- **Commands that end the job or bypass the pipeline.** Guard commands first on `PATH` refuse `git push` (with any global options before it), `gh`, `pkill` and `killall`. Claude Code also gets `--disallowedTools` for the same commands, and for web fetch and search.

## Events and the web UI

The coder's JSON stream becomes Pullwright events: agent start and end, text, tool calls and results, and usage. The runner panel and the Costs view work as they do for the native coder. The coder appears as one agent named `claude-code` or `goose`, and its cost counts as implementation.

## Rounds

`PULLWRIGHT_MAX_ROUNDS` defaults to **2**: the first checks, one fix round, then a draft PR with the failures for a human. Stripe's Minions allow at most two CI runs, because more rounds cost more and rarely help. The coder is told to run the checks itself before finishing, so most fixes happen inside its own loop.

## How they compared

The same approved plan, run by each coder, both through the LLM proxy (2026-11-13):

| Task | Coder | Rounds | Runner time | Cost (plan + runner) | Notes |
|---|---|---|---|---|---|
| Go API, one endpoint | Claude Code | 1 | 3.2 min | $0.13 + $0.26 | No upper bounds on inputs |
| Go API, one endpoint | goose | 1 | 4.6 min | $0.11 + $0.25 | Input bounds and a body size limit |
| Go API + React UI | Claude Code | 2 | 5.6 min | $0.18 + $0.92 | 28 UI tests; plain, clear UI |
| Go API + React UI | goose | 1 | 6.9 min | $0.17 + $0.84 | 54 UI tests; units, ranges and explained results in the UI |

Feedback on a goose PR ("label the theme button with its action") was picked up from the PR comment, fixed with a test in one round, and pushed to the same branch for $0.14.

## Versions

The runner image pins both agents (`runner/Dockerfile`: `CLAUDE_CODE_VERSION`, `GOOSE_VERSION`). Rebuild the image to upgrade, and check that `goose run --help` still has `--instructions`, `--no-profile`, `--with-builtin`, `--output-format stream-json` and `--max-turns`.
