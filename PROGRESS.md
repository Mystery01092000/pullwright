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
| PR review feedback loop + work log on the PR (#7) | x | x | | | review comments from writers send the runner back to the same branch |
| Any provider per role (Anthropic, OpenAI-compatible presets, custom) | x | x | x | | live: Claude Haiku and Ollama qwen2.5 tool runs |
| Control-plane LLM proxy (#3): job tokens, central dollar cap | x | x | x | | live: Haiku (streamed) and Ollama through the proxy; spend matched the agent's count |
| Cost per stage and model: PR body, work log, per request, /api/costs | x | x | | | |
| Web: request timeline, live runner panel, structured plan card, Costs view, light theme | x | build | x | | checked in a browser against a seeded store |
| Docker Compose + control-plane image (#8) | x | n/a | x | | image built; API, UI, proxy, Docker CLI and healthcheck verified |
| GHCR release workflow, amd64 + arm64 (#5) | x | n/a | | | runs on the first v* tag |
| Planning evals: describer file recall/precision, verifier catch rate (#4) | x | x | x | | live: 4/4, $0.018 per plan on Sonnet 5.5 |
| Postgres option for the control-plane store | x | x | x | | store tests and the control-plane image verified on postgres:17 |
| EC2 runner | | | | | roadmap |

126 tests pass (`cd server && uv run pytest -q`; set `PULLWRIGHT_TEST_PG` to include Postgres), `ruff check` is clean, `npm run build` passes.

## Live runs

| Date | Task | Result |
|---|---|---|
| 2026-11-11 | arcanetech-ai/pullwright-sandbox#1: `OrderStore.remove` + half-up percentage discount, with tests | Plan: 9 Sonnet calls, $0.088, verifier approved. Runner: Docker, `uv sync` 4s, 1 Haiku worker, lint+tests green in round 1, review complete. PR #2 ready, `Closes #1`, review requested from the requester, commits by pullwright[bot]. **$0.135 total, 72s runner time.** |
| 2026-11-11 | sandbox#3 from the **web UI** (GitHub OAuth login): `OrderStore.list()` sorted by total | Plan $0.067, approved in the web plan card (write access checked live), issue #3, runner green in round 1 ($0.041), PR #4 with the describer's title, PR link posted back in the web thread. **$0.108 total.** |

## 2026-11-12: conversation and routing

- Intake routes each message: questions are answered in the thread by a read-only **responder**; changes are planned; messages on a waiting plan become approve, revise, cancel or an answer. Typed approvals use the button's write-access check. Tests: `test_quick_intent`, `test_a_question_is_answered_without_a_plan_or_an_issue`, `test_chat_then_confirm_plans_answers_about_the_plan_and_approves_by_text`, `test_a_message_while_the_plan_is_drafted_is_held_back`.
- Describer summaries are capped at three sentences.
- Web: Verifier, Done when and Risks fold closed on the plan card; long summaries clamp; the composer grows with its text and takes chat at every stage except drafting; the lifecycle docks on the right in landscape (at least 1024 px).
- Org config demo: `arcanetech-ai/pullwright-skills`; Go runner image `runner/go/Dockerfile`.

## 2026-11-13: coders, rounds, caches, links (Stripe Minions alignment)

- `coders.py`: the runner's agent steps run on `native`, `claude-code` (2.1.322) or `goose` (v1.59.0), chosen per repository. Their stream-json becomes Pullwright events; skills go into the agent's own HOME; the LLM proxy keeps the job cap. They get no GitHub credentials, and guard commands block `git push`, `gh`, `pkill` and `killall`.
- Default `PULLWRIGHT_MAX_ROUNDS` is now 2 (Minions: at most two CI runs).
- Warm caches: a shared Docker volume for npm, uv, pip and Go caches.
- Links in requests are fetched before planning (GitHub items through the client; other pages https-only and public-only).
- Fixes found in live runs:
  - `pkill -f fincalc` killed fincalc#1's own runner, so runner agents can't kill by name or group any more;
  - the Go image's caches were under /root;
  - a cached GitHub token expired before fincalc#1's push. Jobs now get a fresh token and refresh it before pushing (`/llm/github-token`), and agents' commands no longer inherit the runner's GitHub token.

### The PR and the chat in sync (2026-11-13)

- **GitHub → chat:** a review comment on fincalc-goose#2 was copied into the web thread on the first sync, as a GitHub card.
- **Chat → GitHub:** a change typed in the thread ("show total interest as a percentage of principal") was posted on the PR by the App, with the chat marker. The follow-up loop picked it up as the developer's feedback.
- **Default coder:** goose (`coders.DEFAULT_CODER`).

### Coder comparison (2026-11-13, same plan, both through the LLM proxy)

| Task | Coder | Rounds | Runner time | Cost (plan + runner, proxy count) | Result |
|---|---|---|---|---|---|
| quickcalc (Go API, 1 endpoint) | Claude Code | 1 | 3.2 min | $0.13 + $0.26 | PR, 487 lines; no upper bounds on inputs |
| quickcalc | goose | 1 | 4.6 min | $0.11 + $0.25 | PR, 589 lines; bounds and a body limit |
| fincalc (Go API + React UI) | Claude Code | 2 | 5.6 min | $0.18 + $0.92 | PR, 28 UI tests, coverage 100/98% |
| fincalc | goose | 1 | 6.9 min | $0.17 + $0.84 | PR, 54 UI tests, coverage 100/94%; richer UI (units, ranges, explained results) |

Claude Code's second fincalc round partly came from a plan criterion asking for "three commits", which no coder can meet because the pipeline commits. The describer and the reviewer now ignore git structure.

### Web UI redesign (2026-11-13)

- Designed on Claude Design first (`docs/design/`), then built: dark and flat, rounded, every message with its own header.
- `Thread.tsx`: agent tool calls as folded activity rails (`ThinkingState`), `thinking-orbs` orbs for each live state, an approval card (Approve, Revise with feedback, Cancel), GitHub comments in place, and the newest reply streamed.
- The plan card reads as one column: what it found, numbered steps (a bold lead with the details under it), files tagged new, edit or read, how it will check the work, then Verifier, Done when and Risks folded.
- Answers to questions lead with a **Summary** card; the responder writes a `**Summary:**` line first.
- The responder and the describer can read git history with the read-only `git_log` and `git_show` tools ("what did the last commit change?").
- Tested live in Chrome: a question, a plan → approve → runner → PR #4 on quickcalc-goose ($0.23), a git-history question ($0.02), the theme toggle and a phone-width layout.

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
