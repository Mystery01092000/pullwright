"""The pull request a runner opens when it finishes an issue: ready for review when green, a draft otherwise."""
from __future__ import annotations

import re
from typing import Any

from .. import checks as checks_mod
from ..costs import markdown as costs_md
from ..pipeline.work import WorkResult
from .client import GitHub, GitHubError
from .issues import FAILED, LABEL, PR_OPEN, IssueTask, set_state


def render_body(task: IssueTask, result: WorkResult) -> str:
    who = f"@{task.requester_login}" if task.requester_login else task.requester_name
    head = (f"Closes #{task.number}. Ready for review, {who}." if result.green
            else f"Refs #{task.number}. Draft: the runner stopped with status **{result.status}** after {result.rounds} round(s), {who}.")
    lines = [head, "", "### What changed", "", result.report.strip() or "(no report)", "", "### Checks", "",
             checks_mod.report(result.checks) or "- (no checks configured)"]
    if result.diagnosis:
        verdict = "meets the acceptance criteria" if result.diagnosis["complete"] else "gaps remain"
        lines += ["", "### Independent review", "", f"Verdict: {verdict}.", ""]
        lines += [f"- {g}" for g in result.diagnosis["gaps"]]
        lines += ["", f"<sub>Evidence: {result.diagnosis['evidence']}</sub>"]
    if result.error:
        lines += ["", "### Why it stopped", "", f"```\n{result.error[:3000]}\n```"]
    if result.costs:
        lines += ["", *costs_md(result.costs, "Implementation cost")]
    models = ", ".join(f"`{m}`" for m in (result.costs or {}).get("by_model", {})) or "no model calls"
    lines += ["", f"<sub>Implemented by Pullwright: {result.rounds} round(s), ${result.cost_usd:.2f}. Models: {models}.</sub>"]
    return "\n".join(lines)


def open_pr(gh: GitHub, task: IssueTask, result: WorkResult, base: str | None = None) -> dict[str, Any]:
    """Open the PR for a finished run, ask the requester to review it, and move the issue's queue label."""
    base = base or gh.default_branch(task.repo)
    title = f"{task.title} (#{task.number})"
    pr = gh.create_pr(task.repo, result.branch, base, title, render_body(task, result), draft=not result.green)
    if task.requester_login:
        try:
            gh.request_reviewers(task.repo, pr["number"], [task.requester_login])
        except GitHubError as e:
            if e.status != 422:  # 422: the requester authored the head commit or is not a collaborator
                raise
    set_state(gh, task.repo, task.number, PR_OPEN if result.green else FAILED)
    gh.add_labels(task.repo, pr["number"], [LABEL, PR_OPEN if result.green else FAILED])  # how the dispatcher finds PRs to watch
    gh.comment(task.repo, task.number, f"{'Pull request' if result.green else 'Draft pull request'} opened: {pr['html_url']}")
    return pr


def linked_issue(pr: dict[str, Any]) -> int | None:
    """The issue a Pullwright PR was opened for ('Closes #n' / 'Refs #n' at the start of its body)."""
    m = re.match(r"\s*(?:Closes|Refs) #(\d+)", pr.get("body") or "")
    return int(m.group(1)) if m else None
