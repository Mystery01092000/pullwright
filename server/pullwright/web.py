"""The web channel and the control plane's HTTP surface.

- GitHub OAuth login; the session is a signed cookie carrying the GitHub login (no server-side sessions).
- /link?token=...: the page a chat user lands on to bind their Slack or Teams id to their GitHub account.
- /requests, /threads/{id}: the web UI's chat: send a task for a repository, read the bot's messages, approve or revise.
- /api/teams/messages: the Teams webhook (set it as the bot's messaging endpoint).

Env: PULLWRIGHT_SECRET (signs cookies and link tokens), PULLWRIGHT_PUBLIC_URL (e.g. https://pullwright.arcane.tech),
GITHUB_OAUTH_CLIENT_ID, GITHUB_OAUTH_CLIENT_SECRET.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from .channels.base import ChatUser, Decision, Mention, PlanCard
from .channels.teams import AuthError, TeamsChannel

COOKIE = "pw_session"
SESSION_TTL_S = 14 * 24 * 3600


class Signer:
    def __init__(self, secret: bytes) -> None:
        self.secret = secret

    def dumps(self, data: dict[str, Any], ttl_s: int) -> str:
        raw = base64.urlsafe_b64encode(json.dumps({**data, "exp": int(time.time()) + ttl_s}, separators=(",", ":")).encode()).decode().rstrip("=")
        sig = hmac.new(self.secret, raw.encode(), hashlib.sha256).hexdigest()[:40]
        return f"{raw}.{sig}"

    def loads(self, token: str | None) -> dict[str, Any] | None:
        if not token or "." not in token:
            return None
        raw, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, hmac.new(self.secret, raw.encode(), hashlib.sha256).hexdigest()[:40]):
            return None
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        return data if data.get("exp", 0) >= time.time() else None


class WebChannel:
    """The web UI as a chat channel: the bot's replies and plan cards are stored per thread and polled by the UI."""
    name = "web"

    def __init__(self, store: Any) -> None:
        self.store = store

    def reply(self, thread_ref: dict[str, Any], text: str) -> None:
        self.store.add_message(thread_ref["thread"], "reply", {"text": text})

    def show_plan(self, thread_ref: dict[str, Any], card: PlanCard) -> None:
        self.store.add_message(thread_ref["thread"], "plan", card.__dict__)


class NewRequest(BaseModel):
    repo: str = ""  # empty: the knowledge base picks one from the task
    task: str


class DecisionBody(BaseModel):
    decision: str
    feedback: str = ""


def build_router(intake: Any, store: Any, signer: Signer, teams: TeamsChannel | None = None,
                 oauth: tuple[str, str] | None = None, public_url: str = "", http: httpx.Client | None = None) -> tuple[APIRouter, APIRouter]:
    """Two routers: `auth` (browser navigations: login, callback, link, logout) belongs at the site root;
    `api` (JSON for the web UI, and the Teams webhook) belongs under /api."""
    a, r = APIRouter(), APIRouter()
    http = http or httpx.Client(timeout=30)

    def me(request: Request) -> dict[str, Any]:
        user = signer.loads(request.cookies.get(COOKIE))
        if user is None:
            raise HTTPException(401, "log in with GitHub first")
        return user

    def chat_user(user: dict[str, Any]) -> ChatUser:
        return ChatUser("web", user["login"], user.get("name") or user["login"], user["login"])

    # --- login ----------------------------------------------------------------------------------

    @a.get("/auth/github/login")
    def login(next: str = "/", link: str = "") -> RedirectResponse:
        if oauth is None:
            raise HTTPException(503, "GitHub login is not configured (GITHUB_OAUTH_CLIENT_ID / _SECRET)")
        state = signer.dumps({"next": next if next.startswith("/") else "/", "link": link, "nonce": secrets.token_hex(8)}, 600)
        q = urlencode({"client_id": oauth[0], "redirect_uri": f"{public_url}/auth/github/callback", "scope": "read:user", "state": state})
        return RedirectResponse(f"https://github.com/login/oauth/authorize?{q}")

    @a.get("/auth/github/callback")
    def callback(code: str, state: str) -> RedirectResponse:
        st = signer.loads(state)
        if st is None or oauth is None:
            raise HTTPException(400, "login expired; try again")
        tok = http.post("https://github.com/login/oauth/access_token", headers={"Accept": "application/json"},
                        data={"client_id": oauth[0], "client_secret": oauth[1], "code": code}).json().get("access_token")
        if not tok:
            raise HTTPException(400, "GitHub did not return a token")
        gh_user = http.get("https://api.github.com/user", headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}).json()
        login_name, name = gh_user["login"], gh_user.get("name") or gh_user["login"]
        dest = st.get("next") or "/"
        if st.get("link"):
            linked = intake.complete_link(st["link"], login_name)
            dest = f"/?linked={linked.channel if linked else 'expired'}"
        resp = RedirectResponse(dest)
        resp.set_cookie(COOKIE, signer.dumps({"login": login_name, "name": name}, SESSION_TTL_S), max_age=SESSION_TTL_S,
                        httponly=True, samesite="lax", secure=public_url.startswith("https://"))
        return resp

    @a.get("/link")
    def link(token: str) -> RedirectResponse:
        return RedirectResponse(f"/auth/github/login?{urlencode({'link': token})}")

    @a.post("/auth/logout")
    def logout() -> JSONResponse:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE)
        return resp

    @r.get("/me")
    def whoami(request: Request) -> dict[str, Any]:
        u = me(request)
        return {"login": u["login"], "name": u.get("name", u["login"])}

    # --- requests (the web chat) ----------------------------------------------------------------

    @r.post("/requests")
    def create(body: NewRequest, request: Request) -> dict[str, Any]:
        u = me(request)
        thread = uuid.uuid4().hex[:12]
        text = f"{body.repo.strip()}: {body.task}" if body.repo.strip() else body.task
        store.add_message(thread, "user", {"text": text, "login": u["login"]})
        rid = intake.on_mention(Mention(chat_user(u), text, {"thread": thread}, f"{public_url}/?thread={thread}"))
        return {"thread": thread, "request_id": rid}

    @r.get("/requests")
    def mine(request: Request) -> list[dict[str, Any]]:
        return store.requests(me(request)["login"])

    @r.get("/threads/{thread}")
    def thread_messages(thread: str, request: Request, after: int = 0) -> dict[str, Any]:
        me(request)
        msgs = store.messages(thread, after)
        req = next((x for x in store.requests(None, 500) if x["thread_ref"].get("thread") == thread), None)
        return {"request": req, "messages": msgs}

    @r.post("/requests/{rid}/decision")
    def decide(rid: str, body: DecisionBody, request: Request) -> dict[str, str]:
        u = me(request)
        if body.decision not in ("approve", "revise", "cancel"):
            raise HTTPException(422, "decision must be approve, revise or cancel")
        req = store.request(rid)
        if req is None:
            raise HTTPException(404, "no such request")
        msg = intake.on_decision(Decision(rid, body.decision, chat_user(u), body.feedback))  # type: ignore[arg-type]
        if req["channel"] == "web":
            store.add_message(req["thread_ref"]["thread"], "user", {"text": f"{body.decision}" + (f": {body.feedback}" if body.feedback else ""), "login": u["login"]})
        return {"message": msg}

    @r.get("/repos")
    def repos(request: Request) -> list[dict[str, Any]]:
        """The repositories Pullwright works on, with their profile, for the web UI's picker."""
        me(request)
        kb = getattr(intake, "knowledge", None)
        allowed = sorted(getattr(intake, "allowed_repos", set()) or [])
        out = []
        for repo in allowed:
            prof = kb.get(repo) if kb is not None else None
            out.append({"repo": repo, "description": prof.description if prof else "", "languages": prof.languages[:3] if prof else []})
        return out

    @r.get("/queue")
    def queue(request: Request) -> list[dict[str, Any]]:
        me(request)
        return [x for x in store.requests(None, 500) if x["state"] in ("queued", "working", "pr_open", "pr_draft", "failed")]

    @r.get("/costs")
    def cost_summary(request: Request, days: int = 30) -> dict[str, Any]:
        """Spend over the last `days` days: per stage, model, repository, requester and day, and the costliest requests."""
        import time as _time

        from .costs import summarize

        me(request)
        return {"days": days, **summarize(store.requests(None, 5000), since=_time.time() - days * 86400 if days > 0 else 0.0)}

    # --- Teams webhook --------------------------------------------------------------------------

    @r.post("/teams/messages")
    async def teams_messages(request: Request, background: BackgroundTasks) -> dict[str, Any]:
        if teams is None:
            raise HTTPException(404, "Teams is not configured")
        activity = await request.json()
        try:
            teams.auth.verify(request.headers.get("Authorization", ""), activity.get("serviceUrl", ""))
        except AuthError as e:
            raise HTTPException(401, f"unauthorized: {e}") from e
        background.add_task(teams.handle_activity, activity)  # answer Teams within its timeout; work continues
        return {}

    return a, r


def secret_from_env() -> bytes:
    s = os.environ.get("PULLWRIGHT_SECRET")
    if not s:
        raise RuntimeError("PULLWRIGHT_SECRET is not set (any long random string; it signs cookies and link tokens)")
    return s.encode()
