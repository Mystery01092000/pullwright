# Pullwright

**Tag it with a task in Slack, Teams or the web. It reads your repository, writes a plan you approve, opens the GitHub issue, and an isolated runner works the issue until every check passes. Then it opens a pull request that tags you.**

Pullwright is an open-source agent harness for engineering teams. It turns a sentence in chat into a reviewed pull request, with a human approving the plan before any code is written and reviewing the PR before anything merges. A wright is a maker (a shipwright builds ships); this one makes pull requests.

```
 @pullwright arcane/app: rate-limit /login to 5 attempts a minute per IP
   │
   ├─ describer + verifier read the repo (read-only) and draft a plan ──► plan card in your thread
   │                                                                       [Approve] [Revise] [Cancel]
   ├─ approved ──► GitHub issue #142, assigned to you, labeled pullwright:queued
   │
   ├─ runner (fresh Docker container) picks the oldest queued issue, one at a time
   │     clone ─► install deps ─► Sonnet supervisor ─► Haiku workers ─► tests / lint / types
   │                 ▲                                                        │
   │                 └──────────── failures, until every check passes ───────┘
   │     independent reviewer checks the diff against the acceptance criteria
   │
   └─ PR "Closes #142", review requested from you ──► link posted back in your thread
```

## Why it is built this way

- **Understand before writing.** A *describer* agent reads the repository and drafts a plan: relevant files, steps, acceptance criteria, test plan, risks. A separate *verifier* checks that plan against the code before you see it. Neither can write or run anything.
- **A person approves the plan.** Nothing is built until a developer with write access to the repository clicks Approve. The approved plan becomes a GitHub issue carrying the developer's name, so the work has an owner and a paper trail.
- **Commands decide when it is done, not a model.** The runner keeps iterating until the project's own checks (tests, lint, type checks, build) exit 0, and an independent reviewer agrees the acceptance criteria are met.
- **Cheap models do the volume.** Workers run on Claude Haiku 5.5 ($0.10 per million input tokens); a Claude Sonnet 5.5 supervisor splits the work and checks each result. Every agent has a dollar cap, and every call's tokens and cost are recorded.
- **Any provider, per role.** Each role can run on Anthropic, OpenAI, Gemini, OpenRouter, Groq, DeepSeek, Mistral, xAI, Ollama or any OpenAI-compatible server. An optional LLM proxy keeps every key on the control plane and enforces each job's dollar cap there.
- **Isolated execution.** Each issue gets a throwaway container with a GitHub token that can touch only that repository and expires within an hour. Agents there cannot push or call `gh`; the pipeline does that itself.
- **One issue at a time.** Many tasks can come in from chat. They queue as labeled issues and are implemented in approval order (concurrency is configurable).

## What is in the box

| Piece | What it does | Code |
|---|---|---|
| Chat channels | Slack (Socket Mode), Microsoft Teams (Bot Framework), and a web UI with GitHub login | `server/pullwright/channels/`, `web/` |
| Intake | Mentions → requests → planning → approval → issue; links chat users to GitHub accounts | `server/pullwright/intake.py` |
| Planning | Describer + verifier, structured plans, revise loop | `server/pullwright/pipeline/plan.py` |
| GitHub | App auth with per-repo tokens, issues as the queue, PRs with review requests | `server/pullwright/github/` |
| Dispatcher | Oldest queued issue → runner, `max_parallel` at a time | `server/pullwright/dispatcher.py` |
| Runners | Docker (one container per issue), local subprocess for development; EC2 planned | `server/pullwright/runners/`, `runner/Dockerfile` |
| Work loop | Supervisor + Haiku workers → checks → feedback → independent review | `server/pullwright/pipeline/work.py` |
| PR follow-ups | Review comments from people with write access go back to the runner on the same branch, with a work log of every earlier run as context | `server/pullwright/github/feedback.py`, `worklog.py` |
| Knowledge base | Profiles of every repository; picks the repository when a task names none (name, keywords, your history, one Haiku call for close calls) | `server/pullwright/knowledge.py` |
| Checks | Detects setup/build/lint/typecheck/test for Python, Node, Go, Rust, Java; overridable | `server/pullwright/checks.py` |
| Agent core | Streaming loop, tool sandbox, guardrails, budgets, MCP, skills, hooks, event log | `server/pullwright/{agent,tools,session,config}.py` |
| Evals | Real tasks on throwaway repos, graded by commands, with cost | `server/evals/` |

## Quick start (local development)

You need Python 3.13 with [uv](https://docs.astral.sh/uv/), Node 22, git, Docker, an Anthropic API key and a GitHub token.

```bash
git clone https://github.com/Mystery01092000/pullwright && cd pullwright
cp .env.example server/.env      # fill in ANTHROPIC_API_KEY, PULLWRIGHT_SECRET, GITHUB_TOKEN, PULLWRIGHT_REPOS
docker build -f runner/Dockerfile -t ghcr.io/mystery01092000/pullwright-runner:latest .
cd web && npm ci && npm run build && cd ..
cd server && uv sync && uv run pullwright-serve   # http://localhost:8765
```

Open the web UI, log in with GitHub (this needs an OAuth app; see [getting started](docs/getting-started.md)), and send a task for one of the repositories in `PULLWRIGHT_REPOS`. To use Slack or Teams, follow [docs/setup-slack.md](docs/setup-slack.md) or [docs/setup-teams.md](docs/setup-teams.md).

## Documentation

- [How a task flows](docs/how-it-works.md): one task end to end, covering where the model is called, how GitHub login and the GitHub App work, how issues are raised and picked up, and what you need to run.
- [Architecture](docs/architecture.md): components, the request and issue lifecycles, data flow, security boundaries.
- [Getting started](docs/getting-started.md): run the whole flow on your machine.
- [Configuration](docs/configuration.md): every environment variable, and the per-repository `.pullwright/` folder (agents, checks, guardrails, hooks, skills).
- Setup guides: [GitHub App](docs/setup-github-app.md) · [Slack](docs/setup-slack.md) · [Microsoft Teams](docs/setup-teams.md)
- [Runners](docs/runners.md): the Docker image, isolation, adding toolchains, the EC2 plan.
- [Cost](docs/cost.md): where the money goes and the knobs that control it.
- [Organisation config](docs/org-config.md): one repository of shared agents, skills, hooks and guardrails (Claude Code layout works) for every repository.
- [Models and providers](docs/providers.md): run any role on Anthropic, OpenAI, Gemini, OpenRouter, Ollama or any OpenAI-compatible server, and keep keys out of runners with the LLM proxy.
- [Evals](docs/evals.md): measure a prompt or model change instead of guessing.
- [Local mode](docs/local-mode.md): the single-developer chat UI and CLI that Pullwright grew from.
- [Roadmap](docs/roadmap.md)

## Status

The full flow (chat → plan → approval → issue → Docker runner → checks loop → PR → back to chat) is implemented and covered by 126 automated tests. They run with a scripted model and a fake GitHub, so they need no network or API key. Live verification against real Slack, Teams and GitHub App installations is in progress; [PROGRESS.md](PROGRESS.md) tracks what has been verified live. EC2 runners are next on the [roadmap](docs/roadmap.md).

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first; [`good first issue`](https://github.com/Mystery01092000/pullwright/labels/good%20first%20issue) issues are scoped for newcomers. Please report security problems privately ([SECURITY.md](SECURITY.md)).

## License

[Apache License 2.0](LICENSE).
