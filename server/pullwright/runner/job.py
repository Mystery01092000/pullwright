"""One job, inside one isolated runner: issue -> clone -> setup -> implement until green -> push -> PR.

Started as `pullwright-work owner/repo#123` (an approved issue) or `pullwright-work --pr owner/repo#45` (review
feedback on an open Pullwright PR). Everything it needs comes from GitHub and the environment:

    PULLWRIGHT_GITHUB_TOKEN   short-lived installation token for this one repository (minted by the dispatcher)
    ANTHROPIC_API_KEY         model access
    PULLWRIGHT_MAX_COST_USD   dollar cap for this issue (default 5)
    PULLWRIGHT_MAX_ROUNDS     supervisor -> checks rounds (default 4)
    PULLWRIGHT_FEEDBACK_TRIGGER  any (default) | mention: which PR comments count as feedback

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

from .. import git, worklog
from ..events import Event, EventBus
from ..github import feedback, issues, prs
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
    kind: str = "issue"  # issue | feedback
    pr: int | None = None
    addressed: int = 0  # feedback items handled (feedback jobs)

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
        gh.comment(repo, pr["number"], worklog.render(_log_entry("issue", result, handled_until=worklog.now_iso())))
        log(f"opened {pr['html_url']} ({result.status}, ${result.cost_usd:.2f})")
        return JobResult(repo, number, result.status, pr["html_url"], result.branch, result.cost_usd, result.rounds, result.error,
                         pr=pr["number"])
    except Exception as e:  # anything unexpected: leave the issue marked, never silently "working"
        issues.set_state(gh, repo, number, issues.FAILED)
        gh.comment(repo, number, f"The Pullwright runner failed: {type(e).__name__}: {str(e)[:1500]}")
        raise


def _log_entry(kind: str, result: Any, handled_until: str, addressed: list[str] | None = None,
               feedback: list[str] | None = None) -> worklog.LogEntry:
    return worklog.LogEntry(
        kind=kind, status=result.status, rounds=result.rounds, commit=result.commit, cost_usd=round(result.cost_usd, 4),
        report=result.report, checks=[{"name": c.name, "ok": c.ok} for c in result.checks],
        gaps=list((result.diagnosis or {}).get("gaps", [])) if result.diagnosis else [],
        addressed=addressed or [], feedback=feedback or [], handled_until=handled_until)


def run_feedback(repo: str, pr_number: int, gh: GitHub, client: Any, workdir: Path, *, bus: EventBus | None = None,
                 max_cost_usd: float = 5.0, max_rounds: int = 4, mention_only: bool = False,
                 log: Callable[[str], None] = print) -> JobResult:
    """A follow-up on an open Pullwright PR: address the review feedback that arrived since the last run, on the same
    branch, with the original plan and every earlier run's log as context."""
    pr = gh.get_pr(repo, pr_number)
    issue_number = prs.linked_issue(pr)
    task = issues.parse(repo, gh.get_issue(repo, issue_number)) if issue_number else None
    if task is None:
        raise ValueError(f"{repo}#{pr_number} is not a Pullwright pull request (no linked Pullwright issue)")
    log_entries = worklog.entries(gh.issue_comments(repo, pr_number))
    since = log_entries[-1].handled_until if log_entries else pr.get("created_at", "")
    started = worklog.now_iso()  # feedback written while this run works is picked up by the next one
    items = feedback.collect(gh, repo, pr_number, since=since, mention_only=mention_only, handled=worklog.handled_ids(log_entries))
    branch = pr["head"]["ref"]
    if not items:
        issues.set_state(gh, repo, pr_number, issues.PR_OPEN)
        return JobResult(repo, issue_number, "no_feedback", pr["html_url"], branch, kind="feedback", pr=pr_number)
    issues.set_state(gh, repo, pr_number, issues.REVISING)
    gh.comment(repo, pr_number, f"Pullwright is working on {len(items)} review comment(s).")
    dest = workdir / repo.replace("/", "__")
    try:
        git.clone(gh.clone_url(repo), dest, branch=branch)
        git.set_identity(dest, BOT_NAME, BOT_EMAIL)
        session = Session(cwd=dest, client=client, run_id=f"{repo.split('/')[1]}-pr{pr_number}-{len(log_entries) + 1}",
                          bus=bus or EventBus(), max_cost_usd=max_cost_usd, approve=lambda _cmd: True)
        spec = WorkSpec(repo=repo, issue_number=issue_number, title=task.title, task=task.task, spec=task.spec,
                        requester_login=task.requester_login)
        context = "\n\n".join(x for x in (worklog.history(log_entries), feedback.brief(items)) if x)
        result = WorkRun(session, spec, max_rounds=max_rounds, branch=branch, context=context,
                         commit_message=f"Address review feedback on #{pr_number}").run()
        if result.commit:
            git.push(dest, gh.clone_url(repo), branch)
        entry = _log_entry("feedback", result, handled_until=started, addressed=[f.id for f in items], feedback=[f.summary() for f in items])
        gh.comment(repo, pr_number, worklog.render(entry))
        verdict = f"Addressed in {result.commit[:10]}" if result.commit and result.green else f"Tried in {result.commit[:10] or 'no commit'}, status {result.status}"
        for f in items:
            if f.kind == "review_comment":
                gh.reply_to_review_comment(repo, pr_number, f.github_id, f"{verdict}. See the work log comment for details.")
        issues.set_state(gh, repo, pr_number, issues.PR_OPEN if result.green else issues.FAILED)
        log(f"feedback on {pr['html_url']}: {result.status}, ${result.cost_usd:.2f}")
        return JobResult(repo, issue_number, result.status, pr["html_url"], branch, result.cost_usd, result.rounds, result.error,
                         kind="feedback", pr=pr_number, addressed=len(items))
    except Exception as e:
        issues.set_state(gh, repo, pr_number, issues.FAILED)
        gh.comment(repo, pr_number, f"The Pullwright runner failed on this feedback: {type(e).__name__}: {str(e)[:1500]}")
        raise


def _stdout_sink(ev: Event) -> None:
    if ev.type in ("thinking", "text"):
        return  # deltas are too chatty for a log stream; the turn's usage and tool events carry the story
    print("PWEVENT " + json.dumps({"type": ev.type, "run_id": ev.run_id, "agent_id": ev.agent_id, "parent_id": ev.parent_id,
                                   "turn": ev.turn, "payload": ev.payload}, default=str), flush=True)


def main(argv: list[str] | None = None) -> int:
    import anthropic

    argv = sys.argv[1:] if argv is None else argv
    pr_mode = bool(argv) and argv[0] == "--pr"
    args = argv[1:] if pr_mode else argv
    if len(args) != 1:
        print("usage: pullwright-work owner/repo#issue | pullwright-work --pr owner/repo#pr", file=sys.stderr)
        return 2
    repo, number = parse_ref(args[0])
    token = os.environ.get("PULLWRIGHT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("PULLWRIGHT_GITHUB_TOKEN is not set", file=sys.stderr)
        return 2
    bus = EventBus()
    bus.subscribe(_stdout_sink)
    with tempfile.TemporaryDirectory(prefix="pullwright-") as tmp:
        caps = {"max_cost_usd": float(os.environ.get("PULLWRIGHT_MAX_COST_USD", "5")),
                "max_rounds": int(os.environ.get("PULLWRIGHT_MAX_ROUNDS", "4"))}
        gh, client = GitHub(PatAuth(token)), anthropic.Anthropic()
        if pr_mode:
            mention_only = os.environ.get("PULLWRIGHT_FEEDBACK_TRIGGER", "any") == "mention"
            res = run_feedback(repo, number, gh, client, Path(tmp), bus=bus, mention_only=mention_only, **caps)
        else:
            res = run_job(repo, number, gh, client, Path(tmp), bus=bus, **caps)
    print("PWRESULT " + json.dumps(res.to_dict()), flush=True)
    return 0 if res.status in ("green", "no_feedback") else 1


if __name__ == "__main__":
    raise SystemExit(main())
