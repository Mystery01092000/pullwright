# Testing Pullwright

Two layers: the automated suites, which need no network and no API key, and a manual checklist that exercises every feature against a real repository. Set the app up first with the [user guide](user-guide.md).

## Automated tests

```bash
cd server
uv run pytest -q                       # 134 tests: scripted model, fake GitHub, no network
uv run ruff check .                    # lint
PULLWRIGHT_TEST_PG=postgresql://postgres:pw@localhost:55432/pullwright uv run pytest -q tests/test_store_backends.py
cd ../web && npm run build             # type check and build the UI
```

For the Postgres run, start a throwaway database first. Its tables are dropped at the start of the run:

```bash
docker run -d --name pw-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=pullwright -p 55432:5432 postgres:17-alpine
```

| Suite | Covers |
|---|---|
| `test_agent`, `test_providers` | The agent loop, Anthropic and OpenAI-compatible providers, retries, structured output, per-role models |
| `test_llm_proxy` | Job tokens, real-key swap, streamed and plain metering, the dollar cutoff, revocation, runner environment |
| `test_pipeline`, `test_runner`, `test_feedback` | Planning, the work loop, the runner job, review follow-ups and work logs |
| `test_intake`, `test_web`, `test_serve` | Chat requests, approvals, the web API and login, the assembled control plane |
| `test_costs` | Cost per stage and model, request totals, the Costs and activity APIs |
| `test_orgconfig`, `test_config` | Layered config, Claude Code layout, specialists, hook rules, pinned fetch, the sample in `examples/org-config` |
| `test_store_backends` | The store on SQLite and Postgres |
| `test_checks`, `test_github`, `test_tools`, `test_mcp`, `test_evals` | Check detection, GitHub layer, tool sandbox, MCP tools, the eval runners |

CI runs all of this on every push, against a Postgres service, and builds both container images.

## Evals (real model calls, real cost)

```bash
cd server
uv run python -m evals.planning                 # describer and verifier, about $0.07 for the 4 cases
uv run python -m evals.run python-subtract      # one implementation case end to end
uv run python -m evals.planning --model ollama:qwen2.5:1.5b   # the same cases on another provider
```

See [evals.md](evals.md) for what each case checks.

## Manual checklist

Use a small test repository with a working test suite, listed in `PULLWRIGHT_REPOS`. Run the server as in the user guide, ideally with `PULLWRIGHT_LLM_PROXY_URL` set so the proxy is tested too. Each line says what to do and what you should see.

### Web UI and requests

- [ ] **Log in.** Open http://localhost:8765 and log in with GitHub. Your `@login` shows at the bottom of the sidebar.
- [ ] **Unknown repository.** Send a task for `someone/not-configured`. The thread replies "I'm not set up for …" with the configured list. A message box appears under the thread; send a task for a configured repository there and planning starts in the same thread.
- [ ] **No repository named.** Send a task without `owner/repo:`. Pullwright either picks one and says why, or asks which one.
- [ ] **Planning.** A placeholder shows the describer, then the verifier. The plan card shows the verifier's verdict, steps, files, done-when, tests, risks, open questions and the planning cost. The progress bar shows Plan done and Approve active.
- [ ] **Revise.** Click Revise, write a change, send it. A new card appears marked "revision 1".
- [ ] **Cancel.** On another request, click Cancel. It shows as cancelled, and the Closed filter finds it.
- [ ] **Approve.** Click Approve and open issue. The issue appears on GitHub labelled `pullwright:queued` and assigned to you. The progress bar moves to Queue, and the request shows in the Queue view.

### Runner

- [ ] **Runner starts** within `PULLWRIGHT_POLL_SECONDS` (default 30). The issue gets the working label and a comment. The runner panel shows "Installing dependencies", then the supervisor and workers with their models, and recent tool calls.
- [ ] **Checks.** The panel lists each check with pass or fail and its round. A failure starts another round, up to `PULLWRIGHT_MAX_ROUNDS`.
- [ ] **Pull request.** A ready PR opens with "Closes #n", a review request (App mode), a cost table per stage and model, and a work-log comment. The thread posts the link, and the progress bar ends on Pull request.
- [ ] **No key in the runner** (proxy on). While a runner works, `docker inspect <container> --format '{{.Config.Env}}'` lists `PULLWRIGHT_LLM_TOKEN` and no `ANTHROPIC_API_KEY`. Afterwards, the request's cost record carries `proxy_usd`.
- [ ] **Review follow-up.** Leave a review comment on the PR. About two minutes later the runner panel shows "Review follow-up", the branch gets a new commit, the inline comment gets a reply, and a second work log appears.
- [ ] **A run that can't go green.** Ask for something the tests contradict (for example "make test_x expect 3 while the function returns 2, without changing the function"). After the round limit a draft PR opens that explains why it stopped.
- [ ] **Budget cap.** Restart with `PULLWRIGHT_MAX_COST_USD=0.02` and approve a task. The run stops with status `budget` and says so on the issue.

### Costs and the rest of the UI

- [ ] **Costs view.** Totals match the requests you ran, the day bars show values on hover, and the stage split and per-model table add up.
- [ ] **Unpriced model.** Run a request with an unpriced model (next section). The Costs view warns about it.
- [ ] **Themes and phone width.** The theme button cycles system, light and dark. At phone width the sidebar opens as an overlay and the Queue becomes cards.

### Providers

- [ ] **A local model.** Start Ollama and pull a small tool-capable model:

  ```bash
  docker run -d --name pw-ollama -p 11434:11434 ollama/ollama
  docker exec pw-ollama ollama pull qwen2.5:1.5b
  ```

  Restart with `PULLWRIGHT_WORKER_MODEL=ollama:qwen2.5:1.5b` (and `OLLAMA_BASE_URL=http://host.docker.internal:11434/v1` so runners can reach it). The runner panel shows workers on `ollama:qwen2.5:1.5b`. Small local models are weak at real coding tasks, so this mostly proves the plumbing.
- [ ] **Another hosted provider.** Set its key (for example `OPENAI_API_KEY`) and `PULLWRIGHT_MODEL_VERIFIER=openai:<model>`. The plan card is verified by that model, and its usage shows under that name in Costs.

### Shared skills, agents and hooks

- [ ] **Load the sample.** Restart with `PULLWRIGHT_ORG_CONFIG_DIR=<repo>/examples/org-config`. Run a task that touches input handling. The supervisor can call `spawn_specialist` with `security-reviewer`, which then appears in the runner panel's agents.
- [ ] **The hook blocks secrets.** Ask for a task whose natural first step prints `.env`, or check it directly: `cd server && uv run pytest -q tests/test_orgconfig.py -k sample`.
- [ ] **From a repository.** Push `examples/org-config` to a repository of your own, then set `PULLWRIGHT_ORG_CONFIG=<you>/<repo>` with either the App installed there or `PULLWRIGHT_ORG_CONFIG_READ_TOKEN`. The server log shows "organisation config … at <commit>" on start, and each runner log shows the same commit.
- [ ] **Fails closed.** Point `PULLWRIGHT_ORG_CONFIG` at a repository the token can't read. The server refuses to start.

### Deployment

- [ ] **Compose.** `docker compose -f deploy/docker-compose.yml up -d --build`, then http://localhost:8765 works and `docker compose ps` shows the container as healthy.
- [ ] **Postgres.** Set `PULLWRIGHT_DB=postgresql://...` and start the server. Tables are created, and requests survive a restart.

### Seeing the UI without GitHub

To look at every screen with sample data (a plan awaiting approval, a live runner, an open PR, costs) without a GitHub App or any spend:

```bash
cd server
uv run python ../scripts/ui_demo.py     # prints a session cookie; open http://127.0.0.1:8790
```

In the browser's console, run `document.cookie = "pw_session=<printed value>; path=/"` and reload.
