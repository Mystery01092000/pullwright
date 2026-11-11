"""Run lifecycle: intake -> confirm -> supervisor(workers) -> diagnosis -> summarizer -> branch/PR.

User decisions go through UserIO so the CLI (input()) and the API (queues) share this code.
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import git
from .agent import AgentDef, RunResult
from .config import HarnessConfig, load_config
from .events import Event, EventBus
from .models import MAIN_MODEL, WORKER_MODEL
from .session import Session

INTAKE_SCHEMA = {
    "type": "object",
    "properties": {
        "understanding": {"type": "string", "description": "What the user wants, in 2-4 sentences"},
        "plan": {"type": "array", "items": {"type": "string"}, "description": "Concrete steps you think are needed"},
        "questions": {"type": "array", "items": {"type": "string"}, "description": "Open questions; empty if none"},
    },
    "required": ["understanding", "plan", "questions"],
    "additionalProperties": False,
}
DIAG_SCHEMA = {
    "type": "object",
    "properties": {
        "complete": {"type": "boolean"},
        "gaps": {"type": "array", "items": {"type": "string"}, "description": "What is missing or wrong vs the request"},
        "evidence": {"type": "string", "description": "What you checked (files, tests run, outputs)"},
    },
    "required": ["complete", "gaps", "evidence"],
    "additionalProperties": False,
}


def thread_context(history: list[dict[str, Any]]) -> str:
    """What the next run of a thread needs to know about the earlier ones. Each entry: task, state, branch, diagnosis, summary."""
    parts = ["This continues earlier work in the same repository and on the same branch. History of this thread:"]
    for i, r in enumerate(history, 1):
        parts.append(f"\n--- Run {i} ({r.get('state')}, branch {r.get('branch') or 'none'}) ---\nRequest:\n{r.get('task', '')}")
        diag = r.get("diagnosis")
        if diag:
            parts.append(f"Diagnosis: complete={diag.get('complete')} gaps={json.dumps(diag.get('gaps', []))}")
        if r.get("summary"):
            parts.append(f"Summary:\n{r['summary']}")
        if r.get("state") in ("cancelled", "failed"):
            parts.append("This run did not finish; any partial changes may still be uncommitted in the working tree. Check `git status` and `git diff` before planning.")
    return "\n".join(parts)


class UserIO(Protocol):
    def confirm_intake(self, intake: dict[str, Any]) -> tuple[bool, str]: ...  # (approved, feedback)
    def approve_command(self, command: str) -> bool: ...
    def want_pr(self, summary: str) -> bool: ...


@dataclass
class Run:
    task: str
    cwd: Path
    io: UserIO
    client: Any
    bus: EventBus = field(default_factory=EventBus)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    thread_id: str = ""  # runs that continue each other share a thread; empty = own id
    prior_context: str = ""  # what happened in the earlier run(s) of the thread, for a continuation
    worker_model: str = WORKER_MODEL
    main_model: str = MAIN_MODEL
    max_cost_usd: float = 20.0
    state: str = "created"
    intake: dict[str, Any] | None = None
    diagnosis: dict[str, Any] | None = None
    summary: str = ""
    branch: str = ""
    commit: str = ""
    pr_url: str = ""
    cost_usd: float = 0.0
    cancelled: threading.Event = field(default_factory=threading.Event)
    cfg: HarnessConfig | None = None  # loaded from <cwd>/.pullwright unless injected
    _sess: Session | None = None
    _supervisor_id: str | None = None

    def __post_init__(self) -> None:
        self.thread_id = self.thread_id or self.id
        self.cfg = self.cfg or load_config(self.cwd)
        self.bus.subscribe(self._track_cost)
        if self.cfg.hooks:
            self.bus.subscribe(self._state_hooks)

    def _state_hooks(self, ev: Event) -> None:
        if ev.type == "state":
            self.cfg.run_hooks("state", ev.payload["state"], {"run_id": self.id, "thread_id": self.thread_id, "task": self.task, **ev.payload}, self.cwd)

    @property
    def request(self) -> str:
        """The user's message, prefixed with the thread history when this run continues an earlier one."""
        if not self.prior_context:
            return self.task
        return f"{self.prior_context}\n\nThe user now says:\n{self.task}"

    def cancel(self) -> None:
        self.cancelled.set()

    def _check_cancel(self) -> None:
        if self.cancelled.is_set():
            raise RuntimeError("cancelled by user")

    def _track_cost(self, ev: Event) -> None:
        if ev.type == "usage":
            self.cost_usd += ev.payload["cost_usd"]

    def _set(self, state: str, **payload: Any) -> None:
        self.state = state
        self.bus.emit(Event("state", self.id, "run", {"state": state, **payload}))

    def _session(self) -> Session:
        if self._sess is None:
            self._sess = Session(
                cwd=self.cwd, client=self.client, run_id=self.id, bus=self.bus, cfg=self.cfg,
                main_model=self.main_model, worker_model=self.worker_model, max_cost_usd=self.max_cost_usd,
                approve=lambda cmd: self.io.approve_command(cmd), cancelled=self.cancelled,
            )
        return self._sess

    def _agent(self, role: str, name: str | None = None, **kw: Any) -> AgentDef:
        return self._session().agent(role, name, **kw)

    def _run(self, agent: AgentDef, prompt: str, parent: str | None = None) -> RunResult:
        self._check_cancel()
        if self.cost_usd > self.max_cost_usd:
            raise RuntimeError(f"run cost ${self.cost_usd:.2f} exceeds cap ${self.max_cost_usd:.2f}")
        res = self._session().run(agent, prompt, parent=parent)
        self._check_cancel()
        return res

    # --- phases -------------------------------------------------------------

    def run_intake(self) -> dict[str, Any]:
        self._set("intake")
        agent = self._agent("intake", output_schema=INTAKE_SCHEMA)
        prompt = f"User request:\n{self.request}"
        for _ in range(3):
            res = self._run(agent, prompt)
            self.intake = json.loads(res.text)
            self._set("awaiting_confirmation", intake=self.intake)
            ok, feedback = self.io.confirm_intake(self.intake)
            if ok:
                return self.intake
            prompt = f"User request:\n{self.request}\n\nYour previous understanding:\n{json.dumps(self.intake, indent=2)}\n\nUser feedback:\n{feedback}"
        raise RuntimeError("intake not confirmed after 3 attempts")

    def _spawn_worker_tool(self) -> dict[str, Any]:
        return self._session().spawn_worker_tool(parent=lambda: self._supervisor_id)

    def run_supervisor(self, extra: str = "") -> str:
        self._set("working")
        self._supervisor_id = f"supervisor-{uuid.uuid4().hex[:6]}"
        agent = self._agent("supervisor", extra_tools=self._spawn_worker_tool())
        prompt = f"User request:\n{self.request}\n\nConfirmed understanding and plan:\n{json.dumps(self.intake, indent=2)}"
        if extra:
            prompt += f"\n\n{extra}"
        self._check_cancel()
        res = self._session().run(agent, prompt, agent_id=self._supervisor_id)
        return res.text

    def run_diagnosis(self) -> dict[str, Any]:
        self._set("diagnosing")
        agent = self._agent("diagnosis", output_schema=DIAG_SCHEMA)
        prompt = (
            f"User request:\n{self.request}\n\nConfirmed plan:\n{json.dumps(self.intake, indent=2)}\n\n"
            f"Diff of the work:\n```\n{git.diff(self.cwd) if git.is_repo(self.cwd) else '(not a git repo)'}\n```"
        )
        self.diagnosis = json.loads(self._run(agent, prompt).text)
        self.bus.emit(Event("diagnosis", self.id, "run", self.diagnosis))
        return self.diagnosis

    def run_summary(self) -> str:
        self._set("summarizing")
        agent = self._agent("summarizer")
        prompt = (
            f"User request:\n{self.request}\n\nDiagnosis:\n{json.dumps(self.diagnosis, indent=2)}\n\n"
            f"Changes:\n{git.diff_stat(self.cwd) if git.is_repo(self.cwd) else '(not a git repo)'}\n\nBranch: {self.branch or '(none)'}"
        )
        self.summary = self._run(agent, prompt).text
        return self.summary

    def run(self) -> Run:
        try:
            self.run_intake()
            if git.is_repo(self.cwd):
                self.branch = git.checkout(self.cwd, self.branch) if self.branch else git.create_branch(self.cwd, git.slugify(self.task))
                self.bus.emit(Event("branch", self.id, "run", {"branch": self.branch}))
            self.run_supervisor()
            for attempt in range(2):
                diag = self.run_diagnosis()
                if diag["complete"] or attempt == 1:
                    break
                self.run_supervisor("Diagnosis found gaps that must be fixed:\n- " + "\n- ".join(diag["gaps"]))
            if self.branch:
                self.commit = git.commit_all(self.cwd, f"pullwright: {self.task[:60]}")
            self.run_summary()
            self._set("awaiting_pr_decision", summary=self.summary)
            if self.branch and self.commit and self.io.want_pr(self.summary):
                title = next((ln[7:].strip() for ln in reversed(self.summary.splitlines()) if ln.startswith("TITLE:")), self.task[:60])
                self.pr_url = git.create_pr(self.cwd, title, self.summary)
                self.bus.emit(Event("pr", self.id, "run", {"url": self.pr_url}))
            self._set("done", cost_usd=self.cost_usd, branch=self.branch, commit=self.commit, pr_url=self.pr_url)
        except Exception as e:
            if self.cancelled.is_set():
                self._set("cancelled", cost_usd=self.cost_usd, branch=self.branch)
                return self
            self._set("failed", error=f"{type(e).__name__}: {e}", cost_usd=self.cost_usd)
            raise
        return self
