"""Run a job as a subprocess of the control plane. For development and tests only: there is no isolation,
the agents' commands run on this machine with this user's rights."""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field

from .base import Job


class LocalHandle:
    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self.proc = proc

    def lines(self) -> Iterator[str]:
        assert self.proc.stdout is not None
        yield from self.proc.stdout

    def wait(self) -> int:
        return self.proc.wait()

    def cancel(self) -> None:
        self.proc.kill()


@dataclass
class LocalRunner:
    command: list[str] = field(default_factory=lambda: [sys.executable, "-m", "pullwright.runner.job"])

    def start(self, job: Job) -> LocalHandle:
        env = {**os.environ, **job.env, "PULLWRIGHT_GITHUB_TOKEN": job.github_token, "PYTHONUNBUFFERED": "1"}
        proc = subprocess.Popen([*self.command, job.ref], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", env=env)
        return LocalHandle(proc)
