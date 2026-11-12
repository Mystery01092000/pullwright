# User guide

How to set Pullwright up on a personal GitHub account or an organisation, run it, and use every part of the app. To check that everything works, follow [testing.md](testing.md).

- [1. Choose how Pullwright acts on GitHub](#1-choose-how-pullwright-acts-on-github)
- [2. Set up once](#2-set-up-once)
- [3. Run it](#3-run-it)
- [4. Using the app](#4-using-the-app)
- [5. Settings you will change most](#5-settings-you-will-change-most)
- [6. Troubleshooting](#6-troubleshooting)

## 1. Choose how Pullwright acts on GitHub

Pullwright works the same on a **personal account** and an **organisation**. It needs one of these two:

| | GitHub App (recommended) | Fine-grained personal access token (quickest) |
|---|---|---|
| Create it | Personal: avatar → Settings → Developer settings → GitHub Apps → New GitHub App. Organisation: the organisation's Settings → Developer settings → GitHub Apps. | Avatar → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token |
| Access | Install it and pick the repositories | "Only select repositories" |
| Permissions | Contents, Issues, Pull requests: read and write. Metadata: read. | Contents, Issues, Pull requests: read and write |
| Other settings | No webhook (Pullwright polls, so it needs no public URL), no callback URL, "Only on this account" | An expiry date |
| What a runner gets | A token for one repository that expires within an hour | Your token, for every repository it covers |
| Who authors issues and PRs | `<app-name>[bot]` | You. GitHub refuses a review request to yourself, so Pullwright skips it. |
| `server/.env` | `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_FILE` | `GITHUB_TOKEN` |

A private GitHub App can only be installed on the account that owns it. An App made under an organisation can't be installed on your personal account, so create a second one there. If both an App and a token are set, the App wins. Full App steps: [setup-github-app.md](setup-github-app.md).

**A shared skills, agents and hooks repository** can live on any account too (`PULLWRIGHT_ORG_CONFIG=<owner>/agent-config@v1`). Either install your App on that repository, or set `PULLWRIGHT_ORG_CONFIG_READ_TOKEN` to a fine-grained token limited to that one repository with Contents: read-only. See [org-config.md](org-config.md).

## 2. Set up once

You need Python 3.13 with [uv](https://docs.astral.sh/uv/), Node 22, git, Docker (Docker Desktop on Windows or macOS), a model provider key (`ANTHROPIC_API_KEY` by default), and a small repository with a working test suite.

1. **Get the code**

   ```bash
   git clone https://github.com/Mystery01092000/pullwright && cd pullwright
   cp .env.example server/.env
   ```

2. **Create the GitHub App or token** (section 1). Install the App on your test repository.
3. **Create a GitHub OAuth App for the web login:** Settings → Developer settings → OAuth Apps → New OAuth App. Set the homepage to `http://localhost:8765` and the callback to `http://localhost:8765/auth/github/callback`. Copy the client ID and generate a secret. This is separate from the App or token in step 2, and asks only for `read:user`.
4. **Fill in `server/.env`**

   ```bash
   ANTHROPIC_API_KEY=sk-ant-...
   PULLWRIGHT_SECRET=...                       # python -c "import secrets; print(secrets.token_urlsafe(32))"
   PULLWRIGHT_PUBLIC_URL=http://localhost:8765
   GITHUB_APP_ID=123456                        # or: GITHUB_TOKEN=github_pat_...
   GITHUB_APP_PRIVATE_KEY_FILE=/path/to/app.pem
   GITHUB_OAUTH_CLIENT_ID=...
   GITHUB_OAUTH_CLIENT_SECRET=...
   PULLWRIGHT_REPOS=<owner>/<test-repo>        # comma-separated; tasks for other repositories are refused
   PULLWRIGHT_RUNNER=docker
   PULLWRIGHT_RUNNER_IMAGE=pullwright-runner:dev
   ```

5. **Build the runner image and the web UI, and install the server**

   ```bash
   docker build -f runner/Dockerfile -t pullwright-runner:dev .
   cd web && npm ci && npm run build && cd ..
   cd server && uv sync && cd ..
   ```

Rebuild the runner image after pulling new server code: a runner runs whatever code is in its image.

## 3. Run it

**On your machine**

```bash
cd server
uv run pullwright-serve     # reads server/.env on start; logs print in this terminal (PULLWRIGHT_LOG_LEVEL=DEBUG for more)
# optional: keep provider keys out of runners
PULLWRIGHT_LLM_PROXY_URL=http://host.docker.internal:8765/llm uv run pullwright-serve
```

It's ready when the log shows `pullwright up: channels=['web'] dispatcher=True`. Open http://localhost:8765 and log in with GitHub. Stop it with Ctrl+C. Requests and history are kept in `server/pullwright.db`.

**With Docker Compose**

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f
docker compose -f deploy/docker-compose.yml down
```

Compose reads `server/.env`, keeps data in the `pullwright-data` volume and turns the LLM proxy on. Inside the container, point `GITHUB_APP_PRIVATE_KEY_FILE` at a mounted path, or put the key itself in `GITHUB_APP_PRIVATE_KEY`.

**Slack and Teams** turn on when their tokens are set: [setup-slack.md](setup-slack.md), [setup-teams.md](setup-teams.md).

Approved tasks open real issues and pull requests and spend real model credits. Spend is capped at $1 per plan and $5 per issue by default.

## 4. Using the app

The sidebar has four views: **Requests**, **Queue**, **Costs** and **Local**. The button next to **+ New** switches between the system, light and dark themes. On a phone the sidebar opens with the panel button.

### Ask a question or send a task

In **Requests**, click **+ New**. Enter `owner/repository` (or leave it empty and Pullwright picks one from the task), type your message, and press **Send** (Enter; Shift+Enter adds a line). The box grows with what you type.

Pullwright reads each message before it does anything:

| You write | What happens |
|---|---|
| A question: *"Give me a summary of this project"*, *"Where is auth handled?"* | A read-only **responder** answers in the thread from the code. No plan, no issue, no PR, a few cents. |
| A change: *"Add a `slugify(text)` helper with tests"* | The describer and verifier draft a **plan** for you to approve. |
| A discussion that ends in *"ok, do that"* | The responder rewrites the thread into one complete task, and that task is planned. |

A first message that starts with a change verb (*add, fix, update, refactor, ...*) goes straight to planning. Anything else, and every follow-up, goes through the responder, which turns a talk into a task only when you ask for the change. When it is unsure, it answers and asks whether you want the change made. Small, testable changes work best.

If the repository isn't configured, or Pullwright needs you to name one, it says so in the thread. Follow-ups in a thread use that thread's repository.

### Read and approve the plan

While planning runs, a placeholder says whether the describer is reading the repository or the verifier is checking the plan. Then the plan card shows:

- the title and a short summary (a long one is cut to a few lines, with **Show more**);
- **Steps**, **Files** (each with why it matters) and **Tests**;
- three folded sections, closed until you open them: **Verifier** (checked against the code, or the corrections it wants), **Done when** (acceptance criteria) and **Risks**. Each shows a count or verdict while closed;
- **open questions**, which you answer in the chat;
- the planning cost so far.

Use the buttons, or just talk in the message box under the thread while the plan waits:

- ask about it (*"why does step 3 touch the router?"*): you get an answer and the plan keeps waiting;
- ask for changes (*"also cover negative amounts"*): a new revision is drafted;
- *approve*, *lgtm*, *looks good*, *go ahead*: approves; *cancel*: stops; *revise: ...*: revises with what follows.

Approving, by button or by text, needs write access to the repository and opens the issue. While the runner works, the message box still takes questions; ask for further changes on the PR, or start a new thread for a separate task.

### Follow the work

On a landscape screen at least 1024 px wide, the request's lifecycle (progress steps, links, cost and the runner panel) sits in a column on the right, next to the chat. On narrower or portrait screens it sits above the chat.

The header of every request shows a five-step progress bar: Plan, Approve, Queue, Runner, Pull request. Each step carries a short status, for example "Running the checks · round 2". It also links the issue and the PR, and shows the request's total cost.

When a runner starts, the **runner panel** shows live:

- the current state (installing dependencies, implementing, running the checks, independent review);
- the round number and the run's spend;
- **Checks**: each check passed or failed, with its time and which round the results come from;
- **Agents**: the supervisor, each worker and specialist, and the model each one runs on;
- **Recent actions**: the last tool calls (files read and written, commands run).

When it finishes, a PR opens: ready for review if the checks passed, a draft that says why if they didn't. Its link appears in the thread. The PR body has a cost table per stage and model, and every run posts a work-log comment on the PR.

### The PR and the chat stay in sync

Once the PR is open, the request's chat thread and the PR's conversation mirror each other:

- **GitHub → chat.** Comments, inline review comments and reviews (approved / changes requested) on the PR appear in the thread as GitHub cards, with the author, the file and line for inline comments, and a link back. They're copied about every 30 seconds (`PULLWRIGHT_GITHUB_SYNC_SECONDS`). Pullwright's own bot comments aren't copied; the thread already has their summary.
- **Chat → GitHub.** A message in the thread while the PR is open is read by the responder. A question is answered in the chat. A change request is posted on the PR as your review comment: your words, signed "via the Pullwright chat", with Pullwright's fuller reading folded underneath. The PR follow-up loop then updates the same PR. Only people with write access to the repository can send changes this way. Merging and closing stay on GitHub.

### Ask for changes on the PR

Leave review comments on the PR, as you would for a colleague. After a quiet period (120 seconds by default) a follow-up run starts on the same branch, with the plan, earlier work logs and your comments as its brief. It replies to each inline comment and posts the next work log. The runner panel shows it as a "Review follow-up". Only comments from people with write access count. To make Pullwright react only when mentioned, set `PULLWRIGHT_FEEDBACK_TRIGGER=mention`.

### Queue

Every approved request with its issue, requester, state, PR link and cost. Approved issues are worked oldest first, one at a time (`PULLWRIGHT_MAX_PARALLEL` raises that). On a phone the table becomes cards.

### Costs

Spend over the last 7, 30 or 90 days:

- totals: spend, requests, average per request and model calls;
- spend per day (hover a bar for its value);
- the share per stage (planning, implementation, review);
- tables per model, repository and requester;
- the costliest requests, each with its stage split.

A model with no price shows a warning, because its calls count as $0. Add it to `PULLWRIGHT_PRICES`.

### Local

Local mode runs the agent loop on your own machine against a folder, without GitHub or a runner. It is for development and trying prompts. See [local-mode.md](local-mode.md).

### Sidebar search and filters

Search by task or repository. The filters are **All**, **Active** (planning, approval, queued, working), **To do** (needs your approval, or a PR waiting for your review) and **Closed**.

## 5. Settings you will change most

| Setting | Example | Effect |
|---|---|---|
| `PULLWRIGHT_REPOS` | `me/api,me/web` | Repositories Pullwright may work on |
| `PULLWRIGHT_MAIN_MODEL`, `PULLWRIGHT_WORKER_MODEL` | `claude-sonnet-5-5`, `ollama:qwen3-coder` | Models for the main roles and for workers |
| `PULLWRIGHT_MODEL_<ROLE>` | `PULLWRIGHT_MODEL_VERIFIER=gemini:<model>` | One role's model |
| `OPENAI_API_KEY`, `GEMINI_API_KEY`, ... | | Keys for other providers ([providers.md](providers.md)) |
| `PULLWRIGHT_PRICES` | `{"ollama:qwen3-coder": {"in": 0, "out": 0}}` | Prices for models outside Anthropic |
| `PULLWRIGHT_LLM_PROXY_URL` | `http://host.docker.internal:8765/llm` | Runners get a capped job token instead of keys |
| `PULLWRIGHT_ORG_CONFIG` | `me/agent-config@v1` | Shared agents, skills, hooks and guardrails ([org-config.md](org-config.md)) |
| `PULLWRIGHT_ORG_CONFIG_DIR` | `examples/org-config` | The same, from a local folder |
| `PULLWRIGHT_PLAN_MAX_COST_USD`, `PULLWRIGHT_MAX_COST_USD`, `PULLWRIGHT_MAX_ROUNDS` | `1`, `5`, `4` | Caps per plan, per issue, and rounds per issue |
| `PULLWRIGHT_DB` | `postgresql://user:pass@host/pullwright` | Postgres instead of SQLite |

Per repository, a `.pullwright/` folder can override agents, add skills, hooks, instructions, guardrails and check commands: [configuration.md](configuration.md). Every variable is listed there.

## 6. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "I'm not set up for owner/repo" | The repository isn't in `PULLWRIGHT_REPOS`. Add it and restart. The App or token must also cover it. |
| Login fails or loops | The OAuth App's callback must be exactly `$PULLWRIGHT_PUBLIC_URL/auth/github/callback`. |
| "Only people with write access can approve" | Your GitHub account needs write, maintain or admin on that repository. |
| Approved, but nothing starts | Docker isn't running, the runner image is missing (`docker images`), or the App isn't installed on the repository. The server log shows the dispatcher's error. |
| The runner fails at once with model errors | The image is older than the server code: rebuild it. With the proxy on, check that `PULLWRIGHT_LLM_PROXY_URL` is reachable from a container (`host.docker.internal`; Docker on Linux needs the host-gateway mapping, which Pullwright adds). |
| Checks never pass | Pullwright guessed the wrong commands. Add `.pullwright/checks.json` to the repository ([configuration.md](configuration.md#checksjson)). |
| A review comment did nothing | Wait out the quiet period (120 seconds by default). The comment must come from someone with write access, and must mention `@pullwright` if `PULLWRIGHT_FEEDBACK_TRIGGER=mention`. |
| A model shows as unpriced | Add it to `PULLWRIGHT_PRICES` or to the provider's `prices` in `PULLWRIGHT_PROVIDERS`. |
| The server won't start with `PULLWRIGHT_ORG_CONFIG` set | The shared repository couldn't be read: install the App on it, or set `PULLWRIGHT_ORG_CONFIG_READ_TOKEN`. Starting without the shared rules is refused on purpose. |
| Port 8765 is in use | Another server is running. Stop it, or set `PULLWRIGHT_PORT` and update `PULLWRIGHT_PUBLIC_URL` and the OAuth callback. |
