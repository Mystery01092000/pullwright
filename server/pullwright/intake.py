"""Intake: chat messages become answers, or planned, approved GitHub issues.

    Mention -> resolve the developer and the repo -> route:
      question  -> responder (read-only) answers in the thread; no issue, no PR
      change    -> PlanRun (describer + verifier) -> plan card -> Decision (approve / revise / cancel)
                   -> issue (queued) -> link in thread
      a message in a thread whose plan is waiting -> the responder reads it as approve, revise, cancel,
                   or a question about the plan (answered; the plan keeps waiting)

A thread has at most one task in progress. Each new task is its own request, so a developer can send several from
one chat; they plan in parallel (bounded by `max_planning`) and their issues queue for the runner in approval order.
Obvious cases skip the responder: a first message that starts with a change verb ("add ...", "fix ...") goes
straight to planning, and "approve" / "cancel" / "revise: ..." on a waiting plan are decided without an LLM call.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
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

from . import git, links, repomap
from .channels.base import Channel, ChatUser, Decision, Mention, PlanCard
from .github import issues
from .github.client import GitHub
from .pipeline.plan import Developer, PlanCancelled, PlanRequest, PlanRun, Review, render_markdown
from .session import Session
from .store import Store

log = logging.getLogger("pullwright.intake")
REPO_RE = re.compile(r"(?:^|\s)(?:repo[:=]\s*)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?=\s|$|[,.:;])")
PREFIX_RE = re.compile(r"^\s*(?:(?:in|for|on)\s+)?(?:repo[:=]\s*)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:\s*(:)|(?=\s|$|[,.;]))", re.I)
APPROVAL_TIMEOUT_S = 7 * 24 * 3600
ACTIVE = ("planning", "awaiting_approval", "queued", "working")  # a thread's task in progress
HISTORY_MESSAGES = 12  # how much of the thread the responder reads

RESPOND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["answer", "change", "approve", "revise", "cancel"]},
        "reply": {"type": "string", "description": "Markdown for the chat: the answer, or one sentence on what happens next"},
        "task": {"type": "string", "description": "change: the full change request; revise: the changes to the plan; else empty"},
    },
    "required": ["intent", "reply", "task"],
    "additionalProperties": False,
}
CHANGE_VERB = re.compile(
    r"^(add|fix|implement|create|remove|delete|drop|refactor|rename|update|change|make|write|build|migrate|upgrade|bump"
    r"|replace|move|split|support|introduce|enable|disable|paginate|optimi[sz]e|improve|convert|extract|port|deprecate)\b", re.I)
APPROVE_WORDS = {"approve", "approved", "lgtm", "ship it", "go ahead", "looks good", "yes", "ok", "okay", "do it"}
CANCEL_WORDS = {"cancel", "stop", "abort", "never mind", "nevermind"}


def quick_intent(text: str, plan_waiting: bool, first_message: bool) -> tuple[str, str] | None:
    """(intent, task) for messages that need no LLM to understand, else None (the responder decides)."""
    t = text.strip().rstrip(".!").strip().lower()
    if plan_waiting:
        if t in APPROVE_WORDS:
            return "approve", ""
        if t in CANCEL_WORDS:
            return "cancel", ""
        if t.startswith("revise:"):
            return "revise", text.split(":", 1)[1].strip()
        return None
    if first_message and CHANGE_VERB.match(t) and not t.endswith("?"):
        return "change", text.strip()
    return None


def split_repo(text: str, default_repo: str = "", known: set[str] | frozenset[str] = frozenset()) -> tuple[str, str]:
    """'in arcane/app: add X' -> ('arcane/app', 'add X'); else the chat's default.

    A repository is named at the start of the message ('owner/name: ...', 'in owner/name ...'), or anywhere when it
    is one of `known`. Other slashed words ('long/short', 'and/or', 'src/app') are part of the text, not a repo."""
    known_l = {k.lower() for k in known}
    m = PREFIX_RE.match(text)
    if m and not m.group(2) and known_l and m.group(1).lower() not in known_l:
        m = None  # 'long/short positions ...': no colon, not a configured repository
    if m is None and known_l:
        m = next((x for x in REPO_RE.finditer(text) if x.group(1).lower() in known_l), None)
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
            self._reply(ch, m.thread_ref, self.link_prompt(m.user))
            return None
        if text.lower() in ("help", ""):
            self._reply(ch, m.thread_ref, "Tag me with a task, e.g. `@pullwright arcane/app: add rate limiting to /login`. "
                                   "I'll plan it, you approve, and a runner opens a PR that tags you.")
            return None
        dev = self.developer(m.user)
        if dev is None:
            self._reply(ch, m.thread_ref, self.link_prompt(m.user))
            return None
        in_thread = self._thread_requests(m.user.channel, m.thread_ref)  # newest first
        repo, task = split_repo(text, (in_thread[0]["repo"] if in_thread else "") or m.default_repo, self.allowed_repos)
        if not repo and self.knowledge is not None and self.allowed_repos:
            res = self.knowledge.resolve(task, sorted(self.allowed_repos), dev.github_login)
            if res.repo:
                repo = res.repo
                self._reply(ch, m.thread_ref, f"Picked `{repo}` ({res.reason}). To use another repository, start the task with `owner/name:`.")
            elif res.candidates:
                options = ", ".join(f"`{c.repo}`" for c in res.candidates[:4])
                self._reply(ch, m.thread_ref, f"Which repository? It could be {options}. Start the task with `owner/name:`.")
                return None
        if not repo:
            self._reply(ch, m.thread_ref, "Which repository? Start the task with `owner/name:`, e.g. `arcane/app: add rate limiting`.")
            return None
        if self.allowed_repos and repo not in self.allowed_repos:
            self._reply(ch, m.thread_ref, f"I'm not set up for `{repo}`. Repositories I work on: {', '.join(sorted(self.allowed_repos))}.")
            return None
        active = next((r for r in in_thread if r["state"] in ACTIVE), None)
        if active is not None and active["state"] == "planning":
            self._reply(ch, m.thread_ref, "I'm still drafting the plan for this thread. Send that again once it's posted.")
            return None
        history = self._history(ch, m.thread_ref, text)
        self._record(ch, m.thread_ref, "user", {"text": text, "login": dev.github_login})
        plan_waiting = active is not None and active["state"] == "awaiting_approval"
        quick = quick_intent(task, plan_waiting, first_message=not history)
        if active is not None:  # the thread's task is in progress: this message is about it
            if quick is not None:
                self._act_on_plan(active["id"], quick[0], quick[1], m.user, ch, m.thread_ref)
            else:
                self._pool.submit(self._converse, active, m.user, task, history)
            return active["id"]
        rid = uuid.uuid4().hex[:10]
        self.store.create_request(rid, m.user.channel, repo, task, dev.github_login, dev.name, m.thread_ref, m.thread_url)
        req = PlanRequest(task=task, repo=repo, requester=dev, thread_url=m.thread_url, thread_ref=dict(m.thread_ref))
        self._pending[rid] = _PendingReview()
        if quick is not None and quick[0] == "change":
            self._reply(ch, m.thread_ref, f"On it. Reading `{repo}` and drafting a plan for: _{task}_")
            self._pool.submit(self._plan, rid, req)
        else:
            self._pool.submit(self._respond, rid, req, history)
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

    def recover(self) -> list[str]:
        """After a restart: planning and waiting-for-approval live in this process's memory, so requests left in those
        states can no longer be decided. Close them and say so in their threads, instead of showing dead buttons."""
        closed = []
        for r in self.store.requests(None, 1000):
            if r["state"] in ("planning", "awaiting_approval") and r["id"] not in self._pending:
                self.store.update_request(r["id"], state="cancelled", error="the server restarted before a decision")
                ch = self.channels.get(r["channel"])
                if ch is not None:
                    self._reply(ch, r["thread_ref"], "Pullwright restarted, so this plan is no longer waiting for a decision. "
                                                     "Send the task again to plan it afresh.")
                closed.append(r["id"])
        return closed

    # --- conversation ---------------------------------------------------------------------------

    def _thread_key(self, ch: Channel, thread_ref: dict[str, Any]) -> str:
        if getattr(ch, "records_messages", False):  # the web channel stores its own thread by id
            return str(thread_ref.get("thread", ""))
        return f"{ch.name}:{json.dumps(thread_ref, sort_keys=True)}"

    def _record(self, ch: Channel, thread_ref: dict[str, Any], kind: str, body: dict[str, Any]) -> None:
        """Keep the thread's conversation for the responder; channels that store it themselves are skipped."""
        if not getattr(ch, "records_messages", False):
            self.store.add_message(self._thread_key(ch, thread_ref), kind, body)

    def _reply(self, ch: Channel, thread_ref: dict[str, Any], text: str) -> None:
        ch.reply(thread_ref, text)
        self._record(ch, thread_ref, "reply", {"text": text})

    def _thread_requests(self, channel: str, thread_ref: dict[str, Any]) -> list[dict[str, Any]]:
        return [r for r in self.store.requests(None, 500) if r["channel"] == channel and r["thread_ref"] == thread_ref]

    def _history(self, ch: Channel, thread_ref: dict[str, Any], current: str) -> list[str]:
        """The thread so far as `Who: text` lines, oldest first, without the message being handled."""
        msgs = self.store.messages(self._thread_key(ch, thread_ref))
        if msgs and msgs[-1]["kind"] == "user" and msgs[-1]["body"].get("text") == current:
            msgs = msgs[:-1]  # the web channel stored it before calling us
        out = []
        for m in msgs[-HISTORY_MESSAGES:]:
            b = m["body"]
            if m["kind"] == "user":
                out.append(f"Developer: {b.get('text', '')}"[:1500])
            elif m["kind"] == "plan":
                spec = b.get("spec") or {}
                out.append(f"Pullwright posted a plan (revision {b.get('revision', 0)}): {spec.get('title', '')}. {spec.get('summary', '')}"[:1500])
            else:
                out.append(f"Pullwright: {b.get('text', '')}"[:1500])
        return out

    def _links(self, text: str) -> str:
        """What the message's links say (pullwright.links); '' when it has none or they can't be read."""
        try:
            return links.hydrate(text, self.gh)
        except Exception:
            log.exception("reading links failed")
            return ""

    def _session(self, rid: str, repo: str) -> Session:
        cwd = self._repo_clone(repo)
        # hooks=False: planning and answering run on the control plane, so no repository- or org-configured command runs here
        session = Session(cwd=cwd, client=self.client_factory(), run_id=f"req-{rid}", max_cost_usd=self.plan_cost_usd, hooks=False)
        session.bus.subscribe(self.store.append)
        return session

    def _ask(self, session: Session, repo: str, who: str, text: str, history: list[str],
             plan: dict[str, Any] | None = None, note: str = "", linked: str | None = None) -> dict[str, Any]:
        """One responder pass: what the message means (RESPOND_SCHEMA), and the reply to post."""
        parts = [f"Repository: {repo}\n\n{repomap.build(session.cwd)}"]
        if history:
            parts.append("Conversation so far:\n" + "\n".join(history))
        if plan:
            parts.append("Plan waiting for the developer's approval:\n" + json.dumps(plan, indent=2))
        linked = self._links(text) if linked is None else linked
        if linked:
            parts.append(linked)
        if note:
            parts.append(note)
        parts.append(f"New message from {who}:\n{text}")
        session.emit("state", state="answering")
        res = session.run(session.agent("responder", output_schema=RESPOND_SCHEMA), "\n\n".join(parts))
        try:
            out = json.loads(res.text)
        except (TypeError, ValueError):
            out = None
        if not isinstance(out, dict) or out.get("intent") not in RESPOND_SCHEMA["properties"]["intent"]["enum"]:
            out = {"intent": "answer", "reply": res.text or "", "task": ""}  # unstructured: show it, change nothing
        session.emit("chat_reply", intent=out.get("intent"), reply=out.get("reply", ""))
        return out

    def _respond(self, rid: str, req: PlanRequest, history: list[str]) -> None:
        """A new message with no task in progress: answer it, or plan it when it asks for a change."""
        ch = self.channels[req.requester.channel]
        planning = False
        try:
            session = self._session(rid, req.repo)
            req.links = self._links(req.task)
            out = self._ask(session, req.repo, req.requester.name, req.task, history, linked=req.links)
            if out["intent"] in ("change", "approve", "revise"):  # with nothing waiting, approve/revise mean "make the change"
                task = out.get("task", "").strip() or req.task
                if history and task != req.task:  # "ok, do that": the restated task is what gets planned and shown
                    req.task = task
                    self.store.update_request(rid, task=task)
                self._reply(ch, req.thread_ref, f"{out.get('reply', '').strip() or 'On it.'} Drafting a plan for: _{task}_")
                planning = True
                self._plan(rid, req, session)
                return
            self.store.update_request(rid, state="answered")
            self.store.add_request_costs(rid, session.meter.to_dict(), {"kind": "chat", "status": "answered", "cost_usd": session.cost_usd})
            self._reply(ch, req.thread_ref, out.get("reply", "").strip() or "Nothing to do here.")
        except Exception as e:
            log.exception("answering %s failed", rid)
            self.store.update_request(rid, state="failed", error=f"{type(e).__name__}: {e}")
            self._reply(ch, req.thread_ref, f"I could not answer that: {type(e).__name__}: {str(e)[:500]}")
        finally:
            if not planning:
                self._pending.pop(rid, None)

    def _converse(self, active: dict[str, Any], user: ChatUser, text: str, history: list[str]) -> None:
        """A message in a thread whose task is in progress: decide on the waiting plan, or answer a question about it."""
        ch, thread, rid = self.channels[active["channel"]], active["thread_ref"], active["id"]
        waiting = active["state"] == "awaiting_approval"
        note = "" if waiting else (f"This thread's task is {active['state']} (issue {active.get('issue_url') or 'not opened yet'}). "
                                   "It cannot be changed from the chat now, so only answer.")
        try:
            session = self._session(f"{rid}", active["repo"])
            out = self._ask(session, active["repo"], user.name, text, history, active.get("plan") if waiting else None, note)
            self.store.add_request_costs(rid, session.meter.to_dict(), {"kind": "chat", "status": out["intent"], "cost_usd": session.cost_usd})
        except Exception as e:
            log.exception("chat on %s failed", rid)
            self._reply(ch, thread, f"I could not answer that: {type(e).__name__}: {str(e)[:500]}")
            return
        intent = out["intent"]
        if intent == "answer":
            self._reply(ch, thread, out.get("reply", "").strip() or "Nothing to add.")
        elif waiting:
            decision = "revise" if intent == "change" else intent
            self._act_on_plan(rid, decision, out.get("task", "").strip() or text, user, ch, thread)
        else:
            self._reply(ch, thread, f"This thread's task is still in progress ({active.get('issue_url') or active['state']}). "
                                    "Ask for more changes on its pull request, or start a new thread for a separate task.")

    def _act_on_plan(self, rid: str, decision: str, feedback: str, user: ChatUser, ch: Channel, thread: dict[str, Any]) -> None:
        """A typed approve / revise / cancel on the waiting plan: the same path, and checks, as the card's buttons."""
        msg = self.on_decision(Decision(rid, decision, user, feedback if decision == "revise" else ""))  # type: ignore[arg-type]
        if decision == "approve" or msg not in ("Revising the plan.", "Cancelled."):  # the planner posts those itself
            self._reply(ch, thread, msg)

    # --- planning -------------------------------------------------------------------------------

    def _plan(self, rid: str, req: PlanRequest, session: Session | None = None) -> None:
        ch = self.channels[req.requester.channel]
        thread = req.thread_ref
        try:
            session = session or self._session(rid, req.repo)
            if not req.links:
                req.links = self._links(req.task)
            reviewer = _ChannelReviewer(self, rid, ch, thread)
            reviewer.cost = lambda: session.cost_usd
            run = PlanRun(session, req, reviewer)
            plan = run.run()
            issue = issues.open_issue(self.gh, plan)
            self.store.update_request(rid, state="queued", plan=plan.spec, issue_number=issue["number"], issue_url=issue["html_url"])
            self.store.add_request_costs(rid, session.meter.to_dict(), {"kind": "plan", "status": "approved", "cost_usd": session.cost_usd})
            self._reply(ch, thread, f"Issue opened and queued: {issue['html_url']}. A runner will pick it up and open a PR that tags @{plan.request.requester.github_login}.")
        except PlanCancelled as e:
            self.store.update_request(rid, state="cancelled", error=str(e))
            self._reply(ch, thread, f"Cancelled: {e}")
        except Exception as e:
            log.exception("planning %s failed", rid)
            self.store.update_request(rid, state="failed", error=f"{type(e).__name__}: {e}")
            self._reply(ch, thread, f"Planning failed: {type(e).__name__}: {str(e)[:500]}")
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
            self._reply(ch, req["thread_ref"], msg)


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
        self.intake._record(self.channel, self.thread, "plan", card.__dict__)
        try:
            d = self.intake._pending[self.rid].answers.get(timeout=APPROVAL_TIMEOUT_S)
        except queue.Empty:
            return Review("cancel", "no decision within a week")
        reviewer = self.intake.developer(d.user) or Developer(name=d.user.name, channel=d.user.channel, channel_user_id=d.user.user_id)
        if d.decision == "revise":
            self.intake.store.update_request(self.rid, state="planning")
            self.intake._reply(self.channel, self.thread, f"Revising with {d.user.name}'s feedback.")
        return Review(d.decision, d.feedback, reviewer)
