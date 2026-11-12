# Getting started

This runs the whole flow on one machine: the web UI as the chat, a personal token in place of a GitHub App, and Docker runners. It takes about 15 minutes. Slack and Teams are added afterwards with their own guides.

## 1. Prerequisites

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- Node 22
- git and Docker (Docker Desktop on macOS or Windows)
- An Anthropic API key
- A GitHub repository to try it on. A small test repository with a test suite is ideal.

## 2. GitHub credentials

1. **Token for the repository.** Create a [fine-grained personal access token](https://github.com/settings/personal-access-tokens/new) for the test repository with **Contents**, **Issues** and **Pull requests** set to read and write. (For an organization, use a [GitHub App](setup-github-app.md) instead.)
2. **OAuth app for the web login.** Go to GitHub → Settings → Developer settings → OAuth Apps → New OAuth App.
   - Homepage URL: `http://localhost:8765`
   - Authorization callback URL: `http://localhost:8765/auth/github/callback`

   Copy the client ID and generate a client secret.

## 3. Configure

```bash
git clone https://github.com/Mystery01092000/pullwright && cd pullwright
cp .env.example server/.env
```

Fill in `server/.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
PULLWRIGHT_SECRET=<output of: python -c "import secrets; print(secrets.token_urlsafe(32))">
PULLWRIGHT_PUBLIC_URL=http://localhost:8765
GITHUB_TOKEN=github_pat_...
GITHUB_OAUTH_CLIENT_ID=...
GITHUB_OAUTH_CLIENT_SECRET=...
PULLWRIGHT_REPOS=your-name/your-test-repo
```

## 4. Build and run

```bash
docker build -f runner/Dockerfile -t ghcr.io/mystery01092000/pullwright-runner:latest .
cd web && npm ci && npm run build && cd ..
cd server && uv sync && uv run pullwright-serve
```

Open http://localhost:8765 and log in with GitHub.

### Or with Docker Compose

```bash
docker build -f runner/Dockerfile -t ghcr.io/mystery01092000/pullwright-runner:latest .   # or pull a released image
docker compose -f deploy/docker-compose.yml up -d --build
```

This runs `pullwright-serve` in a container built from the repository (web UI included), reads `server/.env`, and keeps the SQLite database and planning clones in the `pullwright-data` volume. It mounts the host's Docker socket to start runner containers. That gives the container root-equivalent control of the host's Docker daemon, so use a machine dedicated to Pullwright ([runners](runners.md#where-the-control-plane-runs)). By default it also turns on the [LLM proxy](providers.md#the-llm-proxy-no-provider-key-in-a-runner), so runners get a job token instead of your provider keys.

Released images are published to GHCR for amd64 and arm64 on every `v*` tag: `ghcr.io/mystery01092000/pullwright-runner` and `ghcr.io/mystery01092000/pullwright-server`.

## 5. Send a task

In **Requests**, enter `your-name/your-test-repo` and a small, testable task, for example *"Add a `slugify(text)` helper in utils with tests for spaces, punctuation and unicode"*. Then:

1. Within a minute or two a **plan card** appears with the relevant files, steps, acceptance criteria and test plan, and a note on whether the verifier agreed with it.
2. Click **Revise** to change it, or **Approve and open issue**.
3. The issue appears in the repository, labeled `pullwright:queued` and assigned to you.
4. Within `PULLWRIGHT_POLL_SECONDS` the dispatcher starts a container. The issue gets a `working` label and a comment, and the server log streams the runner's progress.
5. When the checks pass, a PR opens with `Closes #n` and a review request to you, and its link appears in your thread. The **Queue** tab shows every approved issue and its state.

Pullwright detects checks from the project files. If your test command is unusual, add `.pullwright/checks.json` to the repository ([configuration](configuration.md#checksjson)).

## Without Docker

Set `PULLWRIGHT_RUNNER=local` to run jobs as subprocesses of the server. This has **no isolation**: the agents' commands run on your machine as your user. Use it only to develop Pullwright itself.

## Local mode

The original single-developer flow (pick a local folder, chat, get a branch) is still under **Local runs** and in the terminal. See [local mode](local-mode.md).

## Next

- [Slack](setup-slack.md) and [Teams](setup-teams.md)
- [Configure checks, prompts and guardrails per repository](configuration.md)
- [Control cost](cost.md)
