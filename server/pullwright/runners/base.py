"""Where a job runs. Every backend runs the same command, `pullwright work owner/repo#n`, in a fresh, isolated
environment, and streams its stdout back so `PWEVENT` / `PWRESULT` lines reach the control plane.

    LocalRunner   a subprocess on this machine, for development and tests (no isolation)
    DockerRunner  a throwaway container per job (v1 production)
    EC2Runner     an instance per job (planned; same interface)
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Job:
    repo: str
    issue: int
    github_token: str  # short-lived, scoped to `repo`
    env: dict[str, str] = field(default_factory=dict)  # extra environment (ANTHROPIC_API_KEY, caps)

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.issue}"

    @property
    def name(self) -> str:
        return "pullwright-" + self.repo.replace("/", "-").lower() + f"-{self.issue}"


class Handle(Protocol):
    def lines(self) -> Iterator[str]:
        """The job's stdout, line by line, until it exits."""
        ...

    def wait(self) -> int: ...
    def cancel(self) -> None: ...


class Runner(Protocol):
    def start(self, job: Job) -> Handle: ...


@dataclass
class JobOutcome:
    exit_code: int
    result: dict[str, Any] | None  # the PWRESULT payload, if the job got that far
    events: int = 0


def follow(handle: Handle, on_event: Callable[[dict[str, Any]], None] | None = None, on_log: Callable[[str], None] | None = None) -> JobOutcome:
    """Read a running job to the end: events to `on_event`, other lines to `on_log`, and the final result."""
    result, events = None, 0
    for line in handle.lines():
        line = line.rstrip("\n")
        if line.startswith("PWEVENT "):
            events += 1
            if on_event is not None:
                try:
                    on_event(json.loads(line[8:]))
                except ValueError:
                    pass
        elif line.startswith("PWRESULT "):
            try:
                result = json.loads(line[9:])
            except ValueError:
                pass
        elif on_log is not None:
            on_log(line)
    return JobOutcome(handle.wait(), result, events)
