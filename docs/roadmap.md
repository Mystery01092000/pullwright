# Roadmap

Done means implemented, tested with the scripted model and fake GitHub, and documented. Live means also verified against the real service. [PROGRESS.md](../PROGRESS.md) tracks the live column.

## v1: chat to PR (implemented)

- [x] Slack (Socket Mode), Microsoft Teams (Bot Framework REST) and web (GitHub login) channels
- [x] Account linking: chat user → GitHub login through a signed link
- [x] Describer + verifier planning on a read-only clone, revise loop, approval limited to users with write access
- [x] GitHub App auth with per-repository installation tokens, issues as the queue (labels), PRs with review requests
- [x] Dispatcher: oldest queued issue first, one at a time (configurable)
- [x] Docker runner (non-root, resource limits, secrets by name) and local runner
- [x] Work loop: Sonnet supervisor, Haiku workers, deterministic checks, independent review, bounded by rounds and dollars
- [x] Check detection for Python, Node, Go, Rust, Java; per-repository overrides
- [x] Web Requests and Queue screens; local mode kept
- [x] CI: tests, lint, web build, runner image build

## v1.1: conversation and coders (implemented, verified live 2026-11-12/13)

- [x] Chat routing: questions answered by a read-only responder; changes planned; typed approve / revise / cancel on a waiting plan
- [x] The PR and the chat in sync: PR comments and reviews appear in the thread; change requests from the thread are posted on the PR and handled by the follow-up loop
- [x] Pluggable coders: goose (default), Claude Code, or Pullwright's own supervisor and workers, per repository
- [x] Minions-style limits: 2 rounds by default, then a draft PR; warm caches; links read before planning
- [x] Fresh GitHub tokens per job, refreshed before the push; agents' commands never see GitHub credentials

## Next

- [ ] Live verification with a real Slack workspace and Teams tenant (the GitHub App and the web UI are verified; tracked in PROGRESS.md)
- [ ] **Generic runner image** with `mise`: toolchains installed from a repository's own version files (`go.mod`, `.nvmrc`, `mise.toml`), so a new stack needs no new image; per-repository image override
- [ ] **Survive restarts**: plans waiting for approval and LLM-proxy job tokens live in memory today, so a control-plane restart closes waiting plans and breaks running jobs
- [ ] **Optional planning**: one-shot mode for small or trusted tasks (no approval step), as in Minions
- [ ] **Merge and close awareness**: mark a request merged or closed when its PR is, and say so in the thread
- [ ] **Runner network lockdown** after setup (Minions run without internet)
- [ ] **EC2 runner**: an instance per issue from a launch template, spot by default, event callback to the control plane
- [x] **LLM proxy in the control plane**, so no provider key enters a runner and budgets are enforced centrally ([providers](providers.md))
- [x] **Any provider per role**: OpenAI, Gemini, OpenRouter, Groq, DeepSeek, Mistral, xAI, Together, Ollama and custom OpenAI-compatible servers
- [x] **Planning evals**: describer file recall/precision and verifier catch rate on planted mistakes, with cost per plan ([evals](evals.md))
- [ ] Issue evals and a nightly eval workflow
- [x] Follow-ups on a PR: review comments from people with write access (or only `@pullwright` mentions) send the runner back to the same branch with the review as the brief, and every run posts a work log on the PR
- [x] Post a cost breakdown per stage on the PR, and show it in a web cost view
- [x] Postgres option for the control plane store (`PULLWRIGHT_DB=postgresql://...`)
- [x] Publish the runner and control-plane images to GHCR (amd64 + arm64) on tagged releases
- [x] Docker Compose file for the control plane

- [x] **Organisation config repository**: shared agents (role overrides and specialists), skills, hooks, instructions and guardrails, pinned per run; Claude Code layout supported ([org-config](org-config.md))

## Ideas

- Slack / Teams buttons on the PR cards (approve the PR, request changes) from the chat
- Webhooks instead of polling for queued issues and PR comments
- Repository memory: conventions learned from past reviews, kept in `.pullwright/instructions.md` through PRs
- Server-side compaction for very long worker tasks
- Linear and Jira as task sources next to chat

Want to take one? Open an issue or comment on an existing one first ([CONTRIBUTING.md](../CONTRIBUTING.md)).
