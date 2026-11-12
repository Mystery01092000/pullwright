# Coding agents

The runner's agent steps ("implement the approved plan" and "fix these failing checks") can be done by:

| Coder | `PULLWRIGHT_CODER` | What it is |
|---|---|---|
| Pullwright's own | `native` (default) | A Sonnet supervisor that delegates to Haiku workers, with per-agent costs |
| Claude Code | `claude-code` | Anthropic's coding agent, headless (`claude -p`), pinned in the runner image |
| goose | `goose` | The open-source agent (aaif-goose/goose), headless (`goose run`), pinned in the runner image |

Pullwright stays the workflow around the coder, whichever one runs. Stripe's Minions call this a *blueprint*: the setup, the deterministic checks, the independent review, commit, push and the PR are always Pullwright's. Only the agent steps change hands.

```
setup -> coder -> checks --fail--> coder (with the failures) -> checks --fail--> draft PR for a human
                    | pass
                    v
                 review (Pullwright's diagnosis agent) -> commit -> push -> PR
```

## Choosing

```bash
PULLWRIGHT_CODER=claude-code                                     # every repository
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

## Versions

The runner image pins both agents (`runner/Dockerfile`: `CLAUDE_CODE_VERSION`, `GOOSE_VERSION`). Rebuild the image to upgrade, and check that `goose run --help` still has `--instructions`, `--no-profile`, `--with-builtin`, `--output-format stream-json` and `--max-turns`.
