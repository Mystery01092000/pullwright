"""Approved plans become GitHub issues; labels on those issues are the work queue.

An issue body is written for people (who asked, what, the plan) and ends with a hidden block that the runner
reads back, so the issue alone is enough to start the work: no shared database between control plane and runner.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..pipeline.plan import Plan, render_markdown
from .client import GitHub

LABEL = "pullwright"
QUEUED = "pullwright:queued"
WORKING = "pullwright:working"
PR_OPEN = "pullwright:pr-open"
FAILED = "pullwright:failed"
REVISING = "pullwright:revising"  # on a PR: a runner is addressing review feedback
STATE_LABELS = (QUEUED, WORKING, PR_OPEN, FAILED, REVISING)
LABEL_SPECS = {
    LABEL: ("5319e7", "Planned and implemented by Pullwright"),
    QUEUED: ("c5def5", "Approved; waiting for a runner"),
    WORKING: ("fbca04", "A runner is implementing this"),
    PR_OPEN: ("0e8a16", "Pull request opened; waiting for review"),
    FAILED: ("d93f0b", "The runner could not get every check to pass"),
    REVISING: ("fbca04", "A runner is addressing review feedback on this pull request"),
}
_META = re.compile(r"<!--\s*pullwright:(\{.*?\})\s*-->", re.S)


@dataclass
class IssueTask:
    """What the runner needs, read back from an issue."""
    repo: str
    number: int
    title: str
    task: str
    spec: dict[str, Any]
    requester_login: str
    requester_name: str
    thread_ref: dict[str, Any]
    created_at: str = ""  # ISO time the issue was opened: the queue order across repos


def title_for(plan: Plan) -> str:
    """The describer's title; the first line of the request when there is none (older plans)."""
    title = (plan.spec.get("title") or "").strip() or plan.request.task.strip().splitlines()[0]
    return title if len(title) <= 80 else title[:77].rstrip() + "..."


def render_body(plan: Plan) -> str:
    who = plan.request.requester
    mention = f"@{who.github_login}" if who.github_login else who.name
    approver = plan.approved_by
    lines = [
        f"**Requested by** {who.name} ({mention}) via {who.channel or 'chat'}"
        + (f", [thread]({plan.request.thread_url})" if plan.request.thread_url else ""),
        f"**Approved by** {approver.name}" + (f" (@{approver.github_login})" if approver.github_login else "")
        + f" after {plan.revisions} revision{'s' if plan.revisions != 1 else ''}",
        "",
        "### Request",
        "",
        "> " + plan.request.task.strip().replace("\n", "\n> "),
        "",
        "### Plan",
        "",
        render_markdown(plan.spec, plan.verification),
        "",
        f"<sub>Planned by Pullwright for ${plan.cost_usd:.2f}. A runner picks queued issues one at a time.</sub>",
        "",
        "<!-- pullwright:" + json.dumps({
            "v": 1, "task": plan.request.task, "spec": plan.spec,
            "requester": {"login": who.github_login, "name": who.name, "channel": who.channel, "user_id": who.channel_user_id},
            "thread_ref": plan.request.thread_ref,
        }, separators=(",", ":")) + " -->",
    ]
    return "\n".join(lines)


def parse(repo: str, issue: dict[str, Any]) -> IssueTask | None:
    """The task carried by an issue's hidden block, or None if the issue was not written by Pullwright."""
    m = _META.search(issue.get("body") or "")
    if not m:
        return None
    meta = json.loads(m.group(1))
    req = meta.get("requester", {})
    return IssueTask(repo, issue["number"], issue["title"], meta["task"], meta["spec"], req.get("login", ""), req.get("name", ""),
                     meta.get("thread_ref", {}), issue.get("created_at", ""))


def ensure_labels(gh: GitHub, repo: str) -> None:
    for name, (color, desc) in LABEL_SPECS.items():
        gh.ensure_label(repo, name, color, desc)


def open_issue(gh: GitHub, plan: Plan) -> dict[str, Any]:
    """Create the issue for an approved plan, queued for a runner, assigned to the developer who asked."""
    repo = plan.request.repo
    ensure_labels(gh, repo)
    login = plan.request.requester.github_login
    return gh.create_issue(repo, title_for(plan), render_body(plan), [LABEL, QUEUED], [login] if login else None)


def set_state(gh: GitHub, repo: str, number: int, state: str) -> None:
    """Move an issue to one queue state: exactly one of the state labels is on it afterwards."""
    for label in STATE_LABELS:
        if label != state:
            gh.remove_label(repo, number, label)
    gh.add_labels(repo, number, [state])


def queued(gh: GitHub, repo: str) -> list[IssueTask]:
    """Queued Pullwright issues in approval order (oldest first)."""
    return [t for i in gh.list_issues(repo, QUEUED) if (t := parse(repo, i)) is not None]
