"""Picks queued issues and hands them to a runner, `max_parallel` at a time (default 1: one issue at a time).

The queue is the issues labeled `pullwright:queued` in the watched repositories, oldest first across all of
them. Many tasks can come in from chat; they wait here as issues and are worked through in order.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .github import issues
from .github.client import GitHub
from .runners.base import Job, JobOutcome, Runner, follow

log = logging.getLogger("pullwright.dispatcher")
PASS_ENV = ("ANTHROPIC_API_KEY", "PULLWRIGHT_MAX_COST_USD", "PULLWRIGHT_MAX_ROUNDS")


@dataclass
class Dispatcher:
    gh: GitHub
    runner: Runner
    repos: list[str]
    max_parallel: int = int(os.environ.get("PULLWRIGHT_MAX_PARALLEL", "1"))
    on_event: Callable[[Job, dict[str, Any]], None] | None = None  # runner events, for the store / web UI
    on_done: Callable[[Job, JobOutcome], None] | None = None  # e.g. post the PR link back to the chat thread
    active: dict[str, threading.Thread] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def next_job(self) -> Job | None:
        """The oldest queued issue across the watched repos that is not already running."""
        candidates: list[tuple[str, Any]] = []
        for repo in self.repos:
            for t in issues.queued(self.gh, repo):
                if f"{repo}#{t.number}" not in self.active:
                    candidates.append((t.created_at or f"{t.number:010d}", t))
        if not candidates:
            return None
        _, task = min(candidates, key=lambda c: c[0])
        env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
        return Job(task.repo, task.number, self.gh.auth.token(task.repo), env)

    def _run(self, job: Job) -> None:
        try:
            handle = self.runner.start(job)
            outcome = follow(handle, on_event=(lambda ev: self.on_event(job, ev)) if self.on_event else None,
                             on_log=lambda line: log.info("[%s] %s", job.ref, line))
            if outcome.result is None:  # crashed before it could report: make sure the issue does not stay "working"
                issues.set_state(self.gh, job.repo, job.issue, issues.FAILED)
                self.gh.comment(job.repo, job.issue, f"The runner exited with code {outcome.exit_code} before finishing.")
            if self.on_done is not None:
                self.on_done(job, outcome)
        except Exception:
            log.exception("job %s failed", job.ref)
        finally:
            with self._lock:
                self.active.pop(job.ref, None)

    def tick(self) -> Job | None:
        """Start one job if there is capacity and work. Returns the job started, if any."""
        with self._lock:
            if len(self.active) >= self.max_parallel:
                return None
            job = self.next_job()
            if job is None:
                return None
            issues.set_state(self.gh, job.repo, job.issue, issues.WORKING)  # claim it before the runner boots
            t = threading.Thread(target=self._run, args=(job,), name=job.name, daemon=True)
            self.active[job.ref] = t
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
            t.join(max(0.0, deadline - time.monotonic()))
