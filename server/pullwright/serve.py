"""`pullwright-serve`: the control plane in one process.

    FastAPI (web UI + API + Teams webhook) + Slack Socket Mode + the dispatcher feeding runners.

Configuration is environment variables (see .env.example at the repository root and docs/configuration.md).
Anything not configured is simply off: no Slack tokens, no Slack; no Teams app id, no Teams webhook.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from .channels.slack import SlackChannel
from .channels.teams import BotAuth, TeamsChannel
from .dispatcher import Dispatcher
from .github.client import AppAuth, GitHub, PatAuth
from .intake import Intake, LinkSigner
from .runners.base import Job, JobOutcome
from .store import Store
from .web import Signer, WebChannel, build_router, secret_from_env

log = logging.getLogger("pullwright")


def _json_env(name: str) -> dict[str, str]:
    raw = os.environ.get(name, "").strip()
    return json.loads(raw) if raw else {}


def github_from_env() -> GitHub:
    app_id, key = os.environ.get("GITHUB_APP_ID"), os.environ.get("GITHUB_APP_PRIVATE_KEY")
    key_file = os.environ.get("GITHUB_APP_PRIVATE_KEY_FILE")
    if app_id and (key or key_file):
        return GitHub(AppAuth(app_id, key or Path(key_file).read_text(encoding="utf-8")))
    pat = os.environ.get("GITHUB_TOKEN")
    if pat:
        log.warning("using GITHUB_TOKEN (a personal token); use a GitHub App for an organization")
        return GitHub(PatAuth(pat))
    raise RuntimeError("set GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY(_FILE), or GITHUB_TOKEN for local development")


def runner_from_env() -> Any:
    kind = os.environ.get("PULLWRIGHT_RUNNER", "docker")
    if kind == "docker":
        from .runners.docker import DockerRunner

        return DockerRunner()
    if kind == "local":
        from .runners.local import LocalRunner

        log.warning("PULLWRIGHT_RUNNER=local runs agent commands on this machine without isolation")
        return LocalRunner()
    raise RuntimeError(f"unknown PULLWRIGHT_RUNNER {kind!r} (docker | local)")


def build(store: Store | None = None) -> tuple[Any, dict[str, Any]]:
    """Wire the control plane. Returns the root FastAPI app and the started components (for tests and shutdown).

        /auth/*, /link      GitHub login and chat-account linking (browser navigations)
        /api/*              the JSON API: local runs, team requests, queue, the Teams webhook
        /                   the built web UI (web/dist), when present
    """
    from fastapi import FastAPI

    from .api import app
    from .providers import Router

    secret = secret_from_env()
    store = store or Store(os.environ.get("PULLWRIGHT_DB", "pullwright.db"))
    gh = github_from_env()
    public = os.environ.get("PULLWRIGHT_PUBLIC_URL", "http://localhost:8765").rstrip("/")
    repos = [r.strip() for r in os.environ.get("PULLWRIGHT_REPOS", "").split(",") if r.strip()]
    intake = Intake(store, gh, Router, Path(os.environ.get("PULLWRIGHT_WORKDIR", ".pullwright-work")),
                    link_url=lambda t: f"{public}/link?token={t}", signer=LinkSigner(secret), allowed_repos=set(repos),
                    plan_cost_usd=float(os.environ.get("PULLWRIGHT_PLAN_MAX_COST_USD", "1")))
    parts: dict[str, Any] = {"store": store, "gh": gh, "intake": intake}
    org_spec = os.environ.get("PULLWRIGHT_ORG_CONFIG", "").strip()
    if org_spec:
        from . import orgconfig

        cache = Path(os.environ.get("PULLWRIGHT_WORKDIR", ".pullwright-work")) / "org-config"
        # fail closed: the organisation's deny lists and hooks must be in force before any task runs
        parts["org_config"] = orgconfig.sync(gh, org_spec, cache)[1]
        threading.Thread(target=orgconfig.keep_synced, args=(gh, org_spec, cache, float(os.environ.get("PULLWRIGHT_ORG_CONFIG_REFRESH", "300")),
                                                             threading.Event()), name="org-config", daemon=True).start()
    if repos:
        from .knowledge import KnowledgeBase

        intake.knowledge = KnowledgeBase(gh, store, Router)
        threading.Thread(target=intake.knowledge.refresh, args=(repos,), name="knowledge", daemon=True).start()

    intake.register(WebChannel(store))
    if os.environ.get("SLACK_BOT_TOKEN") and os.environ.get("SLACK_APP_TOKEN"):
        slack = SlackChannel(intake, None, _json_env("PULLWRIGHT_SLACK_REPOS"))
        intake.register(slack)
        parts["slack"] = slack.start(os.environ["SLACK_BOT_TOKEN"], os.environ["SLACK_APP_TOKEN"])
    teams = None
    if os.environ.get("PULLWRIGHT_TEAMS_APP_ID"):
        teams = TeamsChannel(intake, BotAuth(os.environ["PULLWRIGHT_TEAMS_APP_ID"], os.environ.get("PULLWRIGHT_TEAMS_APP_PASSWORD", ""),
                                             os.environ.get("PULLWRIGHT_TEAMS_TENANT_ID", "")), _json_env("PULLWRIGHT_TEAMS_REPOS"))
        intake.register(teams)
        parts["teams"] = teams

    ledger = None
    if os.environ.get("PULLWRIGHT_LLM_PROXY_URL"):
        from .llm_proxy import Ledger, build_proxy

        ledger = Ledger()
        parts["ledger"] = ledger
    oauth = (os.environ["GITHUB_OAUTH_CLIENT_ID"], os.environ["GITHUB_OAUTH_CLIENT_SECRET"]) if os.environ.get("GITHUB_OAUTH_CLIENT_ID") else None
    auth_routes, api_routes = build_router(intake, store, Signer(secret), teams, oauth, public)
    app.include_router(api_routes)
    root = FastAPI(title="pullwright")
    root.include_router(auth_routes)
    root.mount("/api", app)
    if ledger is not None:
        root.include_router(build_proxy(ledger))
    dist = Path(os.environ.get("PULLWRIGHT_WEB_DIST", Path(__file__).resolve().parents[2] / "web" / "dist"))
    if (dist / "index.html").is_file():
        from fastapi.staticfiles import StaticFiles

        root.mount("/", StaticFiles(directory=dist, html=True), name="web")

    if repos and os.environ.get("PULLWRIGHT_DISPATCHER", "on") != "off":
        def done(job: Job, outcome: JobOutcome) -> None:
            # a feedback job is keyed by its PR; the chat thread belongs to the issue the PR closes
            issue = (outcome.result or {}).get("issue") or job.issue
            intake.on_job_done(job.repo, issue, outcome.result, outcome.spend)

        def on_event(job: Job, ev: dict[str, Any]) -> None:
            from .events import Event

            store.append(Event(ev["type"], ev.get("run_id") or f"{job.repo}#{job.issue}", ev.get("agent_id") or "run",
                               ev.get("payload") or {}, ev.get("parent_id"), ev.get("turn")))
            intake.on_job_event(job.repo, job.issue, ev)

        dispatcher = Dispatcher(gh, runner_from_env(), repos, on_event=on_event, on_done=done, ledger=ledger)
        t = threading.Thread(target=dispatcher.serve, kwargs={"interval": float(os.environ.get("PULLWRIGHT_POLL_SECONDS", "30"))},
                             name="dispatcher", daemon=True)
        t.start()
        parts["dispatcher"] = dispatcher
    return root, parts


def main() -> None:
    import uvicorn

    logging.basicConfig(level=os.environ.get("PULLWRIGHT_LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app, parts = build()
    log.info("pullwright up: channels=%s dispatcher=%s", sorted(parts["intake"].channels), "dispatcher" in parts)
    uvicorn.run(app, host=os.environ.get("PULLWRIGHT_HOST", "0.0.0.0"), port=int(os.environ.get("PULLWRIGHT_PORT", "8765")))


if __name__ == "__main__":
    main()
