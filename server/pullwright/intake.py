"""Intake: chat mentions become planned, approved GitHub issues.

    Mention -> resolve the developer and the repo -> PlanRun (describer + verifier) in the background
            -> plan card in the thread -> Decision (approve / revise / cancel) -> issue (queued) -> link in thread

Each mention is its own request, so a developer can send several tasks from one chat; they plan in parallel
(bounded by `max_planning`) and their issues queue for the runner in approval order.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import git
from .channels.base import Channel, ChatUser, Decision, Mention, PlanCard
from .github import issues
from .github.client import GitHub
from .pipeline.plan import Developer, PlanCancelled, PlanRequest, PlanRun, Review, render_markdown
from .session import Session
from .store import Store

log = logging.getLogger("pullwright.intake")
REPO_RE = re.compile(r"(?:^|\s)(?:repo[:=]\s*)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?=\s|$|[,.:;])")
APPROVAL_TIMEOUT_S = 7 * 24 * 3600


def split_repo(text: str, default_repo: str = "") -> tuple[str, str]:
    """'in arcane/app: add X' -> ('arcane/app', 'add X'). The first owner/name token wins; else the chat's default."""
    m = REPO_RE.search(text)
    if not m:
        return default_repo, text.strip()
    task = (text[: m.start(1)] + text[m.end(1):]).strip()
    task = re.sub(r"^(in|for|on)?\s*(repo[:=]?)?\s*[:,-]?\s*", "", task, flags=re.I).strip()
    return m.group(1), task


@dataclass
class LinkSigner:
    """Signed, expiring links that bind a chat user to the GitHub account they log in with on the web UI."""
    secret: bytes

    def sign(self, channel: str, user_id: str, name: str, ttl_s: int = 3600) -> str:
        exp = str(int(time.time()) + ttl_s)
        msg = "|".join((channel, user_id, name, exp))
        sig = hmac.new(self.secret, msg.encode(), hashlib.sha256).hexdigest()[:32]
        return base64.urlsafe_b64encode(f"{msg}|{sig}".encode()).decode().rstrip("=")  # safe in a URL

    def verify(self, token: str) -> tuple[str, str, str] | None:
        try:
            parts = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode().split("|")
        except (ValueError, UnicodeDecodeError):
            return None
        if len(parts) != 5:
            return None
        channel, user_id, name, exp, sig = parts
        good = hmac.new(self.secret, "|".join(parts[:4]).encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, good) or int(exp) < time.time():
            return None
        return channel, user_id, name


class _PendingReview:
    def __init__(self) -> None:
        self.answers: queue.Queue[Decision] = queue.Queue()


@dataclass
class Intake:
    store: Store
    gh: GitHub
    client_factory: Callable[[], Any]  # -> providers.Router()
    workdir: Path  # cached read-only clones for planning
    channels: dict[str, Channel] = field(default_factory=dict)
    link_url: Callable[[str], str] = lambda token: f"/link?token={token}"
    signer: LinkSigner = field(default_factory=lambda: LinkSigner(uuid.uuid4().bytes))
    allowed_repos: set[str] = field(default_factory=set)  # empty = any repo the GitHub App can reach
    knowledge: Any = None  # knowledge.KnowledgeBase: picks the repository when a task does not name one
    max_planning: int = 3
    plan_cost_usd: float = 1.0
    _pending: dict[str, _PendingReview] = field(default_factory=dict)
    _pool: ThreadPoolExecutor | None = None
    _clone_locks: dict[str, threading.Lock] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=self.max_planning, thread_name_prefix="plan")

    def register(self, channel: Channel) -> None:
        self.channels[channel.name] = channel

    # --- who and where --------------------------------------------------------------------------

    def developer(self, user: ChatUser) -> Developer | None:
        login = user.github_login or (self.store.identity(user.channel, user.user_id) or {}).get("github_login", "")
        return Developer(name=user.name, github_login=login, channel=user.channel, channel_user_id=user.user_id) if login else None

    def link_prompt(self, user: ChatUser) -> str:
        token = self.signer.sign(user.channel, user.user_id, user.name)
        return (f"Before I take tasks from you, link your GitHub account so issues and pull requests can tag you: "
                f"{self.link_url(token)} (the link expires in an hour).")

    def complete_link(self, token: str, github_login: str) -> ChatUser | None:
        found = self.signer.verify(token)
        if found is None:
            return None
        channel, user_id, name = found
        self.store.link_identity(channel, user_id, github_login, name)
        return ChatUser(channel, user_id, name, github_login)

    def _repo_clone(self, repo: str) -> Path:
        """A read-only clone for the planners, refreshed before each plan. Planning never runs repository code."""
        lock = self._clone_locks.setdefault(repo, threading.Lock())
        dest = self.workdir / repo.replace("/", "__")
        with lock:
            if (dest / ".git").is_dir():
                url = self.gh.clone_url(repo)
                git._git(dest, "fetch", "--quiet", "--depth", "50", url, "HEAD", check=True)
                git._git(dest, "reset", "--quiet", "--hard", "FETCH_HEAD", check=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                git.clone(self.gh.clone_url(repo), dest)
        return dest

    # --- the flow -------------------------------------------------------------------------------

    def on_mention(self, m: Mention) -> str | None:
        """Handle a tagged message. Returns the request id, or None when the message was answered directly."""
        ch = self.channels[m.user.channel]
        text = m.text.strip()
        if text.lower() in ("link", "link github", "login"):
            ch.reply(m.thread_ref, self.link_prompt(m.user))
            return None
        if text.lower() in ("help", ""):
            ch.reply(m.thread_ref, "Tag me with a task, e.g. `@pullwright arcane/app: add rate limiting to /login`. "
                                   "I'll plan it, you approve, and a runner opens a PR that tags you.")
            return None
        dev = self.developer(m.user)
        if dev is None:
            ch.reply(m.thread_ref, self.link_prompt(m.user))
            return None
        repo, task = split_repo(text, m.default_repo)
        if not repo and self.knowledge is not None and self.allowed_repos:
            res = self.knowledge.resolve(task, sorted(self.allowed_repos), dev.github_login)
            if res.repo:
                repo = res.repo
                ch.reply(m.thread_ref, f"Picked `{repo}` ({res.reason}). To use another repository, start the task with `owner/name:`.")
            elif res.candidates:
                options = ", ".join(f"`{c.repo}`" for c in res.candidates[:4])
                ch.reply(m.thread_ref, f"Which repository? It could be {options}. Start the task with `owner/name:`.")
                return None
        if not repo:
            ch.reply(m.thread_ref, "Which repository? Start the task with `owner/name:`, e.g. `arcane/app: add rate limiting`.")
            return None
        if self.allowed_repos and repo not in self.allowed_repos:
            ch.reply(m.thread_ref, f"I'm not set up for `{repo}`. Repositories I work on: {', '.join(sorted(self.allowed_repos))}.")
            return None
        rid = uuid.uuid4().hex[:10]
        self.store.create_request(rid, m.user.channel, repo, task, dev.github_login, dev.name, m.thread_ref, m.thread_url)
        ch.reply(m.thread_ref, f"On it. Reading `{repo}` and drafting a plan for: _{task}_")
        req = PlanRequest(task=task, repo=repo, requester=dev, thread_url=m.thread_url, thread_ref=dict(m.thread_ref))
        self._pending[rid] = _PendingReview()
        self._pool.submit(self._plan, rid, req)
        return rid

    def on_decision(self, d: Decision) -> str:
        """A button click. Returns a short message for the clicker (an ephemeral reply or toast)."""
        pending = self._pending.get(d.request_id)
        if pending is None:
            return "This plan is no longer waiting for a decision."
        if d.decision == "approve":
            dev = self.developer(d.user)
            req = self.store.request(d.request_id)
            if dev is None:
                return self.link_prompt(d.user)
            if not self.gh.can_write(req["repo"], dev.github_login):
                return f"Only people with write access to `{req['repo']}` can approve this plan."
        pending.answers.put(d)
        return {"approve": "Approved. Opening the issue.", "revise": "Revising the plan.", "cancel": "Cancelled."}[d.decision]

    def _plan(self, rid: str, req: PlanRequest) -> None:
        ch = self.channels[req.requester.channel]
        thread = req.thread_ref
        try:
            cwd = self._repo_clone(req.repo)
            session = Session(cwd=cwd, client=self.client_factory(), run_id=f"req-{rid}", max_cost_usd=self.plan_cost_usd)
            session.bus.subscribe(self.store.append)
            reviewer = _ChannelReviewer(self, rid, ch, thread)
            reviewer.cost = lambda: session.cost_usd
            run = PlanRun(session, req, reviewer)
            plan = run.run()
            issue = issues.open_issue(self.gh, plan)
            self.store.update_request(rid, state="queued", plan=plan.spec, issue_number=issue["number"], issue_url=issue["html_url"])
            self.store.add_request_costs(rid, session.meter.to_dict(), {"kind": "plan", "status": "approved", "cost_usd": session.cost_usd})
            ch.reply(thread, f"Issue opened and queued: {issue['html_url']}. A runner will pick it up and open a PR that tags @{plan.request.requester.github_login}.")
        except PlanCancelled as e:
            self.store.update_request(rid, state="cancelled", error=str(e))
            ch.reply(thread, f"Cancelled: {e}")
        except Exception as e:
            log.exception("planning %s failed", rid)
            self.store.update_request(rid, state="failed", error=f"{type(e).__name__}: {e}")
            ch.reply(thread, f"Planning failed: {type(e).__name__}: {str(e)[:500]}")
        finally:
            self._pending.pop(rid, None)

    def on_job_event(self, repo: str, number: int, event: dict[str, Any]) -> None:
        """A runner event: link its run to the request (feedback jobs are keyed by their PR), and the first sign of life
        moves the request from queued to working."""
        req = self.store.request_for_issue(repo, number) or self.store.request_for_pr(repo, number)
        if req is None:
            return
        run_id = event.get("run_id")
        if run_id and run_id not in req["runs"]:
            self.store.link_run(req["id"], run_id)
        if event.get("type") == "state" and (event.get("payload") or {}).get("state") in ("setup", "working") and req["state"] == "queued":
            self.store.update_request(req["id"], state="working")

    def on_job_done(self, repo: str, number: int, result: dict[str, Any] | None, spend: dict[str, Any] | None = None) -> None:
        """The dispatcher finished a job: record its cost and tell the thread the task came from. `spend` is the LLM
        proxy's own count of the job, when runners go through the proxy."""
        req = self.store.request_for_issue(repo, number)
        if req is None:
            return
        if not (result and result.get("status") == "no_feedback"):
            run = {"kind": (result or {}).get("kind", "issue"), "status": (result or {}).get("status", "crashed"),
                   "cost_usd": float((result or {}).get("cost_usd", 0.0))}
            if spend is not None:
                run["proxy_usd"] = spend.get("spent_usd", 0.0)
            breakdown = (result or {}).get("costs") or None
            if breakdown is None and spend:  # crashed before reporting: the proxy's count is all there is
                breakdown = {"total_usd": spend.get("spent_usd", 0.0), "calls": spend.get("calls", 0),
                             "by_stage": {"other": {"cost_usd": spend.get("spent_usd", 0.0), "calls": spend.get("calls", 0),
                                                    "input_tokens": 0, "output_tokens": 0}},
                             "by_model": {m: {k: v[k] for k in ("cost_usd", "calls", "input_tokens", "output_tokens")}
                                          for m, v in (spend.get("by_model") or {}).items()}}
            self.store.add_request_costs(req["id"], breakdown, run)
        ch = self.channels.get(req["channel"])
        if result and result.get("kind") == "feedback":
            if result.get("status") == "no_feedback":
                return
            state = "pr_open" if result.get("status") == "green" else "pr_draft"
            self.store.update_request(req["id"], state=state)
            verdict = "all checks pass" if result.get("status") == "green" else f"it stopped at **{result.get('status')}**"
            msg = (f"Updated {result['pr_url']} after review: {result.get('addressed', 0)} comment(s) addressed, {verdict}. "
                   f"Review requested from @{req['requester_login']}.")
        elif result and result.get("pr_url"):
            self.store.update_request(req["id"], state="pr_open" if result.get("status") == "green" else "pr_draft", pr_url=result["pr_url"])
            verdict = "All checks pass" if result.get("status") == "green" else f"It stopped at **{result.get('status')}**; this is a draft"
            msg = f"Pull request for #{number}: {result['pr_url']}. {verdict}. Review requested from @{req['requester_login']}."
        else:
            self.store.update_request(req["id"], state="failed", error=(result or {}).get("error", "runner crashed"))
            msg = f"The runner could not finish #{number}. Details are on the issue: {req['issue_url']}"
        if ch is not None:
            ch.reply(req["thread_ref"], msg)


class _ChannelReviewer:
    """Bridges PlanRun's synchronous review to the channel's buttons: post the card, wait for the click."""

    def __init__(self, intake: Intake, rid: str, channel: Channel, thread: dict[str, Any]) -> None:
        self.intake, self.rid, self.channel, self.thread = intake, rid, channel, thread
        self.cost: Callable[[], float] = lambda: 0.0  # the planning session's spend so far

    def review_plan(self, request: PlanRequest, spec: dict[str, Any], verification: dict[str, Any], revision: int) -> Review:
        self.intake.store.update_request(self.rid, state="awaiting_approval", plan=spec)
        card = PlanCard(self.rid, request.repo, request.task, render_markdown(spec, verification), revision,
                        self.cost(), spec.get("questions", []), spec=spec, verification=verification)
        self.channel.show_plan(self.thread, card)
        try:
            d = self.intake._pending[self.rid].answers.get(timeout=APPROVAL_TIMEOUT_S)
        except queue.Empty:
            return Review("cancel", "no decision within a week")
        reviewer = self.intake.developer(d.user) or Developer(name=d.user.name, channel=d.user.channel, channel_user_id=d.user.user_id)
        if d.decision == "revise":
            self.intake.store.update_request(self.rid, state="planning")
            self.channel.reply(self.thread, f"Revising with {d.user.name}'s feedback.")
        return Review(d.decision, d.feedback, reviewer)
