# Progress

Legend: **Dev** = implemented · **Tested** = automated tests pass (scripted model, fake GitHub, no network) · **Live** = verified against the real service · **Pushed** = on GitHub

## Pullwright (team flow)

| Item | Dev | Tested | Live | Pushed | Notes |
|---|---|---|---|---|---|
| Rename agent-loom → pullwright, `.pullwright/` config (`.harness/` still read) | x | x | n/a | | |
| Role models: Sonnet main roles, Haiku workers; fallbacks only where supported | x | x | x | | Haiku 5.5 has no server-side refusal fallback |
| `Session`: shared agent building, guardrails, budgets, spawn_worker | x | x | n/a | | lifecycle.Run uses it |
| Planning: describer + verifier + review loop | x | x | x | | |
| Work loop: supervisor + Haiku workers → checks → review, bounded | x | x | x | | |
| Checks detection + `.pullwright/checks.json`, failure excerpts | x | x | n/a | | |
| Repo map | x | x | n/a | | |
| GitHub App auth (per-repo tokens), issues as queue, PRs | x | x | x | | |
| Runner job (`pullwright-work`): clone, work, push, PR | x | x | x | | tested against a bare repo and fake GitHub |
| Docker runner + runner image | x | x | x | | image built and smoke-tested locally (uid 1000, node 22, uv, git) |
| Dispatcher: oldest first, one at a time | x | x | x | | |
| Intake: requests, account linking, approval by write access | x | x | x | | |
| Slack channel (Socket Mode, Block Kit, revise modal) | x | x | | | needs a Slack app (deploy/slack-manifest.yml) |
| Teams channel (Bot Framework REST, Adaptive Card, JWT verification) | x | x | | | needs an Azure Bot registration |
| Web channel: GitHub login, Requests and Queue screens | x | x (API), build | x | | |
| `pullwright-serve`: /auth at root, /api, UI at / | x | x | x | | |
| CI (tests, ruff, web build, runner image) | x | x | x | x | green on GitHub |
| Docs: README, architecture, configuration, setup guides, runners, cost, evals, roadmap | x | n/a | n/a | | |
| EC2 runner | | | | | roadmap |
| Control-plane LLM proxy | | | | | roadmap |

81 tests pass (`cd server && uv run pytest -q`), `ruff check` is clean, `npm run build` passes.

## Live runs

| Date | Task | Result |
|---|---|---|
| 2026-11-11 | arcanetech-ai/pullwright-sandbox#1: `OrderStore.remove` + half-up percentage discount, with tests | Plan: 9 Sonnet calls, $0.088, verifier approved. Runner: Docker, `uv sync` 4s, 1 Haiku worker, lint+tests green in round 1, review complete. PR #2 ready, `Closes #1`, review requested from the requester, commits by pullwright[bot]. **$0.135 total, 72s runner time.** |
| 2026-11-11 | sandbox#3 from the **web UI** (GitHub OAuth login): `OrderStore.list()` sorted by total | Plan $0.067, approved in the web plan card (write access checked live), issue #3, runner green in round 1 ($0.041), PR #4 with the describer's title, PR link posted back in the web thread. **$0.108 total.** |

## To verify live

1. ~~A GitHub App on a sandbox repository: an approved plan → issue → Docker runner → PR.~~ Done 2026-11-11.
2. A Slack test workspace: mention → plan card → approve → PR link in the thread.
3. ~~The web UI end to end with GitHub OAuth.~~ Done 2026-11-11.
4. Teams through a dev tunnel.

## Local mode (agent-loom history)

| Item | Dev | Tested | Live | Notes |
|---|---|---|---|---|
| Core loop, tools, sandbox, pricing, CLI | x | x | x | |
| Guardrails, MCP | x | x | x | |
| Lifecycle (intake/supervisor/worker/diagnosis/summarizer) | x | x | x | subtract() + 9 tests; Go todo API |
| SQLite store, FastAPI + SSE, React UI, trace view | x | x | x | |
| `.pullwright/` config layer, hooks, skills, evals runner | x | x | | evals not yet run against the real API |
