"""One job, inside one isolated runner: issue -> clone -> setup -> implement until green -> push -> PR.

Started as `pullwright work owner/repo#123`. Everything it needs comes from the issue and the environment:

    PULLWRIGHT_GITHUB_TOKEN   short-lived installation token for this one repository (minted by the dispatcher)
    ANTHROPIC_API_KEY         model access
    PULLWRIGHT_MAX_COST_USD   dollar cap for this issue (default 5)
    PULLWRIGHT_MAX_ROUNDS     supervisor -> checks rounds (default 4)

Progress goes to stdout as `PWEVENT {json}` lines (the dispatcher follows the container log and stores them,
so the web UI shows the run live) and ends with one `PWRESULT {json}` line.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import git
from ..events import Event, EventBus
from ..github import issues, prs
from ..github.client import GitHub, PatAuth
from ..pipeline.work import WorkRun, WorkSpec
from ..session import Session

BOT_NAME = "pullwright[bot]"
BOT_EMAIL = "pullwright@users.noreply.github.com"


@dataclass
class JobResult:
    repo: str
    issue: int
    status: str
    pr_url: str = ""
    branch: str = ""
    cost_usd: float = 0.0
    rounds: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def parse_ref(ref: str) -> tuple[str, int]:
    """'owner/repo#12' -> ('owner/repo', 12)"""
    repo, _, num = ref.partition("#")
    if repo.count("/") != 1 or not num.isdigit():
        raise ValueError(f"expected owner/repo#number, got {ref!r}")
    return repo, int(num)


def run_job(repo: str, number: int, gh: GitHub, client: Any, workdir: Path, *, bus: EventBus | None = None,
            max_cost_usd: float = 5.0, max_rounds: int = 4, log: Callable[[str], None] = print) -> JobResult:
    task = issues.parse(repo, gh.get_issue(repo, number))
    if task is None:
        raise ValueError(f"{repo}#{number} was not written by Pullwright (no pullwright block in the body)")
    issues.set_state(gh, repo, number, issues.WORKING)
    gh.comment(repo, number, "A Pullwright runner picked this up.")
    dest = workdir / repo.replace("/", "__")
    try:
        git.clone(gh.clone_url(repo), dest)
        git.set_identity(dest, BOT_NAME, BOT_EMAIL)
        bus = bus or EventBus()
        # The runner is a throwaway, isolated machine: commands on the guardrails' "ask" list run without a
        # human. The deny list still applies, and pushing or calling gh is denied to agents (pipeline.work).
        session = Session(cwd=dest, client=client, run_id=f"{repo.split('/')[1]}-{number}", bus=bus,
                          max_cost_usd=max_cost_usd, approve=lambda _cmd: True)
        spec = WorkSpec(repo=repo, issue_number=number, title=task.title, task=task.task, spec=task.spec, requester_login=task.requester_login)
        result = WorkRun(session, spec, max_rounds=max_rounds).run()
        if not result.commit:
            issues.set_state(gh, repo, number, issues.FAILED)
            reason = result.error or "the agents made no changes"
            gh.comment(repo, number, f"Pullwright stopped without changes ({result.status}): {reason[:1500]}")
            return JobResult(repo, number, result.status, branch=result.branch, cost_usd=result.cost_usd, rounds=result.rounds, error=reason)
        git.push(dest, gh.clone_url(repo), result.branch)
        pr = prs.open_pr(gh, task, result)
        log(f"opened {pr['html_url']} ({result.status}, ${result.cost_usd:.2f})")
        return JobResult(repo, number, result.status, pr["html_url"], result.branch, result.cost_usd, result.rounds, result.error)
    except Exception as e:  # anything unexpected: leave the issue marked, never silently "working"
        issues.set_state(gh, repo, number, issues.FAILED)
        gh.comment(repo, number, f"The Pullwright runner failed: {type(e).__name__}: {str(e)[:1500]}")
        raise


def _stdout_sink(ev: Event) -> None:
    if ev.type in ("thinking", "text"):
        return  # deltas are too chatty for a log stream; the turn's usage and tool events carry the story
    print("PWEVENT " + json.dumps({"type": ev.type, "run_id": ev.run_id, "agent_id": ev.agent_id, "parent_id": ev.parent_id,
                                   "turn": ev.turn, "payload": ev.payload}, default=str), flush=True)


def main(argv: list[str] | None = None) -> int:
    import anthropic

    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: pullwright work owner/repo#number", file=sys.stderr)
        return 2
    repo, number = parse_ref(argv[0])
    token = os.environ.get("PULLWRIGHT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("PULLWRIGHT_GITHUB_TOKEN is not set", file=sys.stderr)
        return 2
    bus = EventBus()
    bus.subscribe(_stdout_sink)
    with tempfile.TemporaryDirectory(prefix="pullwright-") as tmp:
        res = run_job(repo, number, GitHub(PatAuth(token)), anthropic.Anthropic(), Path(tmp), bus=bus,
                      max_cost_usd=float(os.environ.get("PULLWRIGHT_MAX_COST_USD", "5")),
                      max_rounds=int(os.environ.get("PULLWRIGHT_MAX_ROUNDS", "4")))
    print("PWRESULT " + json.dumps(res.to_dict()), flush=True)
    return 0 if res.status == "green" else 1


if __name__ == "__main__":
    raise SystemExit(main())
