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
- [ ] **LLM proxy in the control plane**, so the Anthropic key never enters a runner and budgets are enforced centrally
- [ ] **Planning and issue evals** with cost and pass rate per issue; nightly eval workflow
- [ ] Follow-ups on a PR: `@pullwright` in a PR review comment sends the runner back with the review as the brief
- [ ] Post a cost breakdown per stage on the PR, and show it in a web cost view
- [ ] Postgres option for the control plane store
- [ ] Publish the runner image to GHCR from CI on tagged releases

## Ideas

- Repository memory: conventions learned from past reviews, kept in `.pullwright/instructions.md` through PRs
- Server-side compaction for very long worker tasks
- Linear and Jira as task sources next to chat

Want to take one? Open an issue or comment on an existing one first ([CONTRIBUTING.md](../CONTRIBUTING.md)).
