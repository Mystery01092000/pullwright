# How a task flows through Pullwright

This walks one task from the moment someone types it to the moment the PR is open. It covers what runs where, when a model is called, how both kinds of GitHub authentication work, and what you actually have to run. The numbers come from the first live runs on `arcanetech-ai/pullwright-sandbox`.

## What you run

| | What | Lifetime |
|---|---|---|
| **1** | `pullwright-serve`: one Python process holding the web UI and API, the Slack connection, the Teams webhook, the planning agents, the dispatcher and an SQLite file | always on |
| **2** | A Docker daemon on the same machine | always on |
| per task | One runner container (`pullwright-runner` image) | about 1–10 minutes, removed when done |

Outside services it calls: the **GitHub API**, the **Anthropic API**, and **Slack** and/or **Teams** if you use them.

There is no Redis, no message broker and no database server. The queue lives on GitHub (issue labels), and state lives in one SQLite file. That is enough for one organization. For more scale, you would add EC2 runners and Postgres (see the [roadmap](roadmap.md)).

## The timeline of one task

```
 t=0s    you: "arcanetech-ai/portfolio: add a dark/light theme toggle"        (web, Slack or Teams)
 t=0s    control plane: request created, "On it" posted in your thread
 t=1s    control plane: refresh a read-only clone of the repo, build the repo map
 t=2-25s describer (Sonnet) reads files and writes the plan; verifier (Sonnet) checks it    ~$0.07-0.09
 t=25s   plan card in your thread                                            waits for you
 t=?     you click Approve -> GitHub write-access check -> issue #N opened, labeled queued
 t+0-30s dispatcher sees the queued issue, labels it working, mints a repo-scoped token,
         starts a container
 t+15s   container: clone, `npm ci` / `uv sync`
 t+20s   supervisor (Sonnet) splits the plan, spawns Haiku workers that edit and run commands
 t+50s   checks (build, typecheck, lint, test): commands, no model
 t+55s   red -> failures go back to the supervisor, next round; green -> reviewer (Sonnet)
 t+70s   push branch, open PR "Closes #N", request your review, label pr-open
 t+70s   the PR link appears in your thread                                  ~$0.04-0.05
```

## Where the LLM is called

Every model call goes through one function, `run_agent()` in `server/pullwright/agent.py`. It calls the Anthropic Messages API with the official Python SDK:

```python
client.beta.messages.stream(
    model=agent.model,                         # claude-sonnet-5-5 or claude-haiku-5-5
    system=[{..., "cache_control": ephemeral}],  # the role's prompt, cached
    cache_control=ephemeral,                   # also cache the growing conversation
    messages=messages,                         # the conversation so far (append-only)
    tools=schemas(agent.tools),                # read_file, edit_file, bash, spawn_worker, ...
    thinking={"type": "adaptive", "display": "summarized"},
    output_config={"effort": ..., "format": <json schema>},  # structured output for planner, verifier, reviewer
    betas=[...], fallbacks="default",           # refusal fallback (not sent to Haiku, which has none)
)
```

It loops: stream a response → if the model asked for tools, run them (sandboxed to the repository) → send back the results → repeat until the model finishes, runs out of turns, or hits its dollar cap. Each response emits a `usage` event with tokens and dollars, and those events are the cost figures you see everywhere.

Which agent runs where:

| Agent | Model | Runs in | Can do | Called from |
|---|---|---|---|---|
| describer | Sonnet 5.5 | control plane | read files only | `pipeline/plan.py` `PlanRun.describe` |
| verifier | Sonnet 5.5 | control plane | read files only | `PlanRun.verify` |
| supervisor | Sonnet 5.5 | runner container | read, run commands, `spawn_worker` | `pipeline/work.py` `WorkRun.supervise` |
| worker | Haiku 5.5 | runner container | read, write, edit, run commands | the supervisor's `spawn_worker` tool (`session.py`) |
| reviewer (diagnosis) | Sonnet 5.5 | runner container | read, run commands | `WorkRun.diagnose` |

The planning agents run in the control plane with the key from `server/.env`. The runner agents run inside the container. The dispatcher passes the key to it by environment-variable name, so it never appears on a command line. A [roadmap](roadmap.md) item moves it behind a proxy so it never enters a runner at all.

The checks (`npm test`, `pytest`, `ruff`, `tsc`) are **not** model calls. Whether a task is done is decided by their exit codes.

## The two kinds of GitHub authentication

There are two separate mechanisms with different jobs.

### 1. GitHub OAuth app: who is this person? (web login)

It's used only to learn a developer's GitHub login.

```
browser -> /auth/github/login
   server builds a signed, 10-minute `state` and redirects to
   github.com/login/oauth/authorize?client_id=...&redirect_uri=.../auth/github/callback&scope=read:user&state=...
browser -> GitHub: you approve "Pullwright wants to know your username"
GitHub -> /auth/github/callback?code=...&state=...
   server checks the state's signature and expiry
   server POSTs code + client secret to github.com/login/oauth/access_token -> a user token
   server GETs api.github.com/user with it -> {"login": "Mystery01092000", "name": ...}
   server sets cookie pw_session = HMAC-signed {login, name, expiry 14 days}; the user token is discarded
```

From then on, every `/api/...` request carries that cookie, and the server checks its signature with `PULLWRIGHT_SECRET`. There are no server-side sessions, and Pullwright never keeps your GitHub user token.

**Slack and Teams users** link once. When the bot doesn't know them, it replies with `/link?token=...`, a token signed with the same secret that holds their chat id and expires in an hour. Opening the link runs the same OAuth login, and the callback stores *chat id → GitHub login* in the `identities` table.

### 2. GitHub App: act on repositories

It's used for everything that touches a repository: clone, issues, labels, comments, push, PRs.

```
server signs a JWT with the app's private key (.pem): {iss: APP_ID, exp: +9 min}, RS256
  GET  /repos/{owner}/{repo}/installation            (Authorization: Bearer <JWT>) -> installation id
  POST /app/installations/{id}/access_tokens  {"repositories": ["portfolio"]}  -> token, expires in 1 hour
every API call and git operation for that repo uses this token
```

The token works for **one repository** and dies within the hour. Pullwright caches it until five minutes before expiry. This token is what the dispatcher hands a runner. Issues, comments and PRs are authored by `pullwright-arcanetechai[bot]`, and commits by `pullwright[bot]`.

## How an issue is raised

When you click **Approve** (`intake.py` `on_decision`):

1. **Permission:** `GET /repos/{repo}/collaborators/{you}/permission` must return write, maintain or admin. Otherwise the approval is refused.
2. **Labels:** `pullwright`, `pullwright:queued`, `:working`, `:pr-open` and `:failed` are created if missing. A 422 "already exists" response is expected.
3. **Issue:** `POST /repos/{repo}/issues`, assigned to you, labeled `pullwright` and `pullwright:queued` (`github/issues.py` `open_issue`).
   - The title is the describer's short title.
   - The body has who asked and approved, the request, and the plan with acceptance-criteria checkboxes.
   - The body ends with a hidden HTML comment, `<!-- pullwright:{"task": ..., "spec": {...the whole plan...}, "requester": ...} -->`. That block is how the runner gets the task: no database is shared between the control plane and runners.
4. **Back to you:** the issue link is posted in your thread, and the request's state becomes `queued`.

## How the agent picks it up and works on it

**Dispatcher** (`dispatcher.py`, a thread inside `pullwright-serve`):

1. Every 30 seconds it asks GitHub for open issues labeled `pullwright:queued` in each repository in `PULLWRIGHT_REPOS`, oldest first.
2. If fewer than `PULLWRIGHT_MAX_PARALLEL` jobs are running (default 1), it takes the oldest one. It relabels it `pullwright:working` *before* starting anything, so it can't be picked twice, and mints that repository's installation token.
3. It runs the container:
   ```
   docker run --rm --user 1000:1000 --cpus 2 --memory 6g --pids-limit 2048 --security-opt no-new-privileges \
     -e PULLWRIGHT_GITHUB_TOKEN -e ANTHROPIC_API_KEY ... pullwright-runner timeout 90m pullwright-work arcanetech-ai/portfolio#5
   ```
4. It reads the container's stdout line by line. Each `PWEVENT {...}` line goes into the event log (so the trace shows in the web UI), and the request flips to `working`. The final `PWRESULT {...}` line is the outcome.

**Inside the container** (`runner/job.py`, then `pipeline/work.py`):

1. Read issue #5 and parse the hidden block back into the task and plan. Comment "A Pullwright runner picked this up."
2. Clone with the token, then remove the token from the saved remote. Set the commit author to `pullwright[bot]`, and create the branch `pullwright/5-<title-slug>`.
3. Run setup (`npm ci` here; `uv sync` for Python). If it fails, stop: the issue is labeled `failed` with the reason.
4. Run up to `PULLWRIGHT_MAX_ROUNDS` (default 4) rounds:
   1. **The supervisor** gets the request, the approved plan, the list of checks and the repository map. It splits the work and calls `spawn_worker(task, context)` for each piece.
   2. **Each worker** is a fresh Haiku agent that sees only its brief. It reads files, edits them and runs the command it was given, then reports back.
   3. **The supervisor** reads the diff and reruns commands to verify each worker, sending corrections when needed.
   4. **The checks run.** If any fail, their failing lines go back to the supervisor as the next round's brief.
   5. **If they all pass, the reviewer** (a separate Sonnet agent that saw none of the conversations) compares the diff with the acceptance criteria. Gaps go back to the supervisor; no gaps means done.
   6. **The dollar cap** (`PULLWRIGHT_MAX_COST_USD`, default $5) is checked before every model call.
5. Commit, push, then `POST /repos/{repo}/pulls`:
   - **Green:** a ready PR with `Closes #5` and a review request to you. The issue is labeled `pr-open`.
   - **Red or over budget:** a draft PR showing the failures. The issue is labeled `failed`.
6. Print `PWRESULT`. The dispatcher posts the PR link in your original thread, and the container is removed.

Agents in the container can't `git push` or run `gh`: those commands are denied in their tool sandbox, and only the pipeline itself pushes.

## Where to look when something goes wrong

| Symptom | Look at |
|---|---|
| no plan appears | server log (planning errors are also posted in the thread) |
| issue not picked up | is the repo in `PULLWRIGHT_REPOS`? does it have the `pullwright:queued` label? server log lines with `dispatcher` |
| runner failed | the comment the runner left on the issue; server log lines prefixed with `[owner/repo#n]` |
| a check fails in the runner but not locally | the runner image lacks a tool your project needs; see [runners](runners.md) |
| cost | the plan card, issue body, PR body and the web Queue view show it; the `events` table has every call |
