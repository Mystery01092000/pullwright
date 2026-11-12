# Cost

Pullwright is built to make the expensive model do little and the cheap model do most of the work, and to stop rather than run away.

## Where the money goes

| Stage | Agents | Model (default) | Price per million tokens (in / out) | Typical share |
|---|---|---|---|---|
| Planning | describer, verifier | Claude Sonnet 5.5 | $2 / $10 (cache reads $0.10) | small: read-only, a few files |
| Implementation | workers | Claude Haiku 5.5 | $0.10 / $0.50 | most of the tokens: reading, editing, running commands |
| Supervision | supervisor | Claude Sonnet 5.5 | $2 / $10 | splitting work, reading diffs |
| Review | diagnosis | Claude Sonnet 5.5 | $2 / $10 | one pass over the diff per green round |
| Checks | (commands) | none | $0 | tests, lint and type checks never call a model |

Every model call records its input, output, cache-write and cache-read tokens and a dollar figure (`pricing.py`). Totals appear on the plan card (planning), in the issue body (planning), in the PR body (implementation), in the web Queue view, and per agent in the trace view.

## The controls

- **Dollar caps.** `PULLWRIGHT_PLAN_MAX_COST_USD` caps each plan, including revisions (default $1). `PULLWRIGHT_MAX_COST_USD` caps each issue (default $5). Each agent definition can set a lower `max_cost_usd`. When a cap is hit the run stops: planning reports the failure in the thread; implementation opens a draft PR and labels the issue `failed`.
- **Round limit.** `PULLWRIGHT_MAX_ROUNDS` (default 4) bounds how many supervisor → checks cycles one issue gets.
- **One issue at a time.** `PULLWRIGHT_MAX_PARALLEL=1` keeps spend predictable. Raise it for throughput.
- **Prompt caching.** The system prompt and the growing conversation tail are cached, so each turn of a long agent loop rereads cached tokens at a tenth of the input price.
- **A shared repository map.** It is built once per commit and given to every agent, so none of them spends turns listing directories.
- **Truncated failure output.** Only the failing lines and the tail of a check's output go back to the model.
- **Fresh context per worker.** Each worker starts with only its brief, so context (and price) doesn't accumulate across tasks.
- **Effort per role.** `effort: low|medium|high` in each agent file trades thoroughness against tokens. Workers and planners default to `medium`.

## Changing models

Set `model:` in `.pullwright/agents/<role>.md` (one repository), `PULLWRIGHT_MODEL_<ROLE>` (the deployment) or edit `server/pullwright/defaults/agents/` (all repositories). Any provider works: see [providers](providers.md). Before changing a default, measure it: run the [evals](evals.md) with the current and the proposed model and compare pass rate and cost per task. Judge cost per **completed** task, not per request: a cheaper model that needs more rounds can cost more in the end.
