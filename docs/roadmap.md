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

## Next

- [ ] Live verification with a real GitHub App, Slack workspace and Teams tenant (tracked in PROGRESS.md)
- [ ] **EC2 runner**: an instance per issue from a launch template, spot by default, event callback to the control plane
- [x] **LLM proxy in the control plane**, so no provider key enters a runner and budgets are enforced centrally ([providers](providers.md))
- [x] **Any provider per role**: OpenAI, Gemini, OpenRouter, Groq, DeepSeek, Mistral, xAI, Together, Ollama and custom OpenAI-compatible servers
- [x] **Planning evals**: describer file recall/precision and verifier catch rate on planted mistakes, with cost per plan ([evals](evals.md))
- [ ] Issue evals and a nightly eval workflow
- [x] Follow-ups on a PR: review comments from people with write access (or only `@pullwright` mentions) send the runner back to the same branch with the review as the brief, and every run posts a work log on the PR
- [x] Post a cost breakdown per stage on the PR, and show it in a web cost view
- [ ] Postgres option for the control plane store
- [x] Publish the runner and control-plane images to GHCR (amd64 + arm64) on tagged releases
- [x] Docker Compose file for the control plane

## Ideas

- Repository memory: conventions learned from past reviews, kept in `.pullwright/instructions.md` through PRs
- Server-side compaction for very long worker tasks
- Linear and Jira as task sources next to chat

Want to take one? Open an issue or comment on an existing one first ([CONTRIBUTING.md](../CONTRIBUTING.md)).
