"""Picks the next piece of work and hands it to a runner, `max_parallel` at a time (default 1).

Two kinds of work, both read from GitHub:
  - feedback: an open Pullwright PR with new review comments from people with write access, quiet for
    `feedback_quiet_s` (so a reviewer can leave several comments before a run starts). Served first: finishing work
    in progress beats starting new work.
  - issue: the oldest issue labeled `pullwright:queued` across the watched repositories.
Many tasks can come in from chat; they wait here as labeled issues and are worked through in order.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import worklog
from .github import feedback, issues
from .github.client import GitHub
from .runners.base import Job, JobOutcome, Runner, follow

log = logging.getLogger("pullwright.dispatcher")
PASS_ENV = ("ANTHROPIC_API_KEY", "PULLWRIGHT_MAX_COST_USD", "PULLWRIGHT_MAX_ROUNDS", "PULLWRIGHT_FEEDBACK_TRIGGER")


def _age_s(iso: str) -> float:
    return (datetime.now(UTC) - datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)).total_seconds()


@dataclass
class Dispatcher:
    gh: GitHub
    runner: Runner
    repos: list[str]
    max_parallel: int = int(os.environ.get("PULLWRIGHT_MAX_PARALLEL", "1"))
    feedback_quiet_s: float = float(os.environ.get("PULLWRIGHT_FEEDBACK_QUIET_SECONDS", "120"))
    mention_only: bool = os.environ.get("PULLWRIGHT_FEEDBACK_TRIGGER", "any") == "mention"
    on_event: Callable[[Job, dict[str, Any]], None] | None = None  # runner events, for the store / web UI
    on_done: Callable[[Job, JobOutcome], None] | None = None  # e.g. post the PR link back to the chat thread
    active: dict[str, threading.Thread | None] = field(default_factory=dict)  # job.key -> thread
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _writers: dict[str, bool] = field(default_factory=dict)  # login -> can write (cached for the process)

    def _env(self) -> dict[str, str]:
        return {k: os.environ[k] for k in PASS_ENV if k in os.environ}

    def feedback_job(self) -> Job | None:
        """An open Pullwright PR whose newest feedback is older than the quiet period, oldest such feedback first."""
        ready: list[tuple[str, str, int]] = []
        for repo in self.repos:
            for pr in self.gh.open_prs(repo, issues.LABEL):
                labels = {lbl["name"] if isinstance(lbl, dict) else lbl for lbl in pr.get("labels", [])}
                if issues.REVISING in labels or f"feedback:{repo}#{pr['number']}" in self.active:
                    continue
                entries = worklog.entries(self.gh.issue_comments(repo, pr["number"]))
                since = entries[-1].handled_until if entries else pr.get("created_at", "")
                items = feedback.collect(self.gh, repo, pr["number"], since=since, mention_only=self.mention_only,
                                         can_write=self._writers, handled=worklog.handled_ids(entries))
                if items and _age_s(items[-1].created_at) >= self.feedback_quiet_s:
                    ready.append((items[0].created_at, repo, pr["number"]))
        if not ready:
            return None
        _, repo, number = min(ready)
        return Job(repo, number, self.gh.auth.token(repo), self._env(), kind="feedback")

    def issue_job(self) -> Job | None:
        """The oldest queued issue across the watched repos that is not already running."""
        candidates: list[tuple[str, Any]] = []
        for repo in self.repos:
            for t in issues.queued(self.gh, repo):
                if f"issue:{repo}#{t.number}" not in self.active:
                    candidates.append((t.created_at or f"{t.number:010d}", t))
        if not candidates:
            return None
        _, task = min(candidates, key=lambda c: c[0])
        return Job(task.repo, task.number, self.gh.auth.token(task.repo), self._env())

    def next_job(self) -> Job | None:
        return self.feedback_job() or self.issue_job()

    def _run(self, job: Job) -> None:
        try:
            handle = self.runner.start(job)
            outcome = follow(handle, on_event=(lambda ev: self.on_event(job, ev)) if self.on_event else None,
                             on_log=lambda line: log.info("[%s] %s", job.ref, line))
            if outcome.result is None:  # crashed before it could report: make sure nothing stays "working"
                issues.set_state(self.gh, job.repo, job.issue, issues.FAILED)
                self.gh.comment(job.repo, job.issue, f"The runner exited with code {outcome.exit_code} before finishing.")
            if self.on_done is not None:
                self.on_done(job, outcome)
        except Exception:
            log.exception("job %s failed", job.key)
        finally:
            with self._lock:
                self.active.pop(job.key, None)

    def tick(self) -> Job | None:
        """Start one job if there is capacity and work. Returns the job started, if any."""
        with self._lock:
            if len(self.active) >= self.max_parallel:
                return None
            job = self.next_job()
            if job is None:
                return None
            # claim it before the runner boots, so the next poll does not pick it again
            issues.set_state(self.gh, job.repo, job.issue, issues.REVISING if job.kind == "feedback" else issues.WORKING)
            t = threading.Thread(target=self._run, args=(job,), name=job.name, daemon=True)
            self.active[job.key] = t
        t.start()
        return job

    def serve(self, interval: float = 30.0, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                while self.tick() is not None:
                    pass
            except Exception:
                log.exception("dispatcher tick failed")
            stop.wait(interval)

    def drain(self, timeout: float = 600.0) -> None:
        """Wait for running jobs (tests and shutdown)."""
        deadline = time.monotonic() + timeout
        for t in list(self.active.values()):
            if t is not None:
                t.join(max(0.0, deadline - time.monotonic()))
