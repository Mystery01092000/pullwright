"""The work log: what every run did on a pull request, kept on the PR itself so a follow-up continues from it.

After each run the bot posts a comment on the PR. It reads as a short report for people and ends with a hidden
`<!-- pullwright-log:{json} -->` block. A follow-up runner reads all the blocks the bot wrote (the plan comes from the
issue, the code from the branch), so it knows what was tried, what the checks and the reviewer said, and which
review comments were already handled. GitHub stays the only state shared between control plane and runners.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

_LOG = re.compile(r"<!--\s*pullwright-log:(\{.*?\})\s*-->", re.S)


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class LogEntry:
    kind: str  # issue | feedback
    status: str  # green | failed | budget | setup_failed | cancelled
    rounds: int
    commit: str
    cost_usd: float
    report: str  # the supervisor's last report
    checks: list[dict[str, Any]] = field(default_factory=list)  # [{name, ok}]
    gaps: list[str] = field(default_factory=list)  # reviewer gaps left, if any
    addressed: list[str] = field(default_factory=list)  # feedback ids handled by this run
    feedback: list[str] = field(default_factory=list)  # one-line summaries of the feedback this run handled
    handled_until: str = ""  # feedback created after this time is new for the next run
    at: str = field(default_factory=now_iso)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


def render(entry: LogEntry) -> str:
    """The PR comment: a short report people read, and the block the next runner reads."""
    title = "Pullwright work log: first implementation" if entry.kind == "issue" else "Pullwright work log: review feedback"
    checks = ", ".join(f"{c['name']} {'pass' if c['ok'] else 'FAIL'}" for c in entry.checks) or "none"
    lines = [f"**{title}**", "",
             f"Status **{entry.status}** after {entry.rounds} round(s), commit `{entry.commit[:10] or 'none'}`, ${entry.cost_usd:.2f}. Checks: {checks}."]
    if entry.feedback:
        lines += ["", "Addressed:"] + [f"- {f}" for f in entry.feedback]
    if entry.gaps:
        lines += ["", "Still open:"] + [f"- {g}" for g in entry.gaps]
    if entry.report:
        lines += ["", "<details><summary>What the supervisor reported</summary>", "", entry.report.strip()[:6000], "", "</details>"]
    lines += ["", "<sub>Comment on this PR to request changes; Pullwright picks up comments from people with write access.</sub>",
              "", f"<!-- pullwright-log:{entry.to_json()} -->"]
    return "\n".join(lines)


def entries(comments: list[dict[str, Any]], trusted: set[str] | None = None) -> list[LogEntry]:
    """Log entries from PR comments, oldest first. Only blocks written by a bot (or a `trusted` login) count:
    anyone can paste a hidden block into a comment, and the log steers what the next run believes it already did."""
    out = []
    for c in comments:
        user = c.get("user") or {}
        if user.get("type") != "Bot" and user.get("login") not in (trusted or set()):
            continue
        m = _LOG.search(c.get("body") or "")
        if m:
            data = json.loads(m.group(1))
            out.append(LogEntry(**{k: v for k, v in data.items() if k in LogEntry.__dataclass_fields__}))
    return out


def handled_ids(log: list[LogEntry]) -> set[str]:
    return {i for e in log for i in e.addressed}


def history(log: list[LogEntry]) -> str:
    """Earlier runs, for the supervisor's and the reviewer's brief."""
    if not log:
        return ""
    parts = ["Earlier Pullwright runs on this pull request (the branch already contains their commits):"]
    for i, e in enumerate(log, 1):
        checks = ", ".join(f"{c['name']} {'pass' if c['ok'] else 'fail'}" for c in e.checks) or "none"
        parts.append(f"\nRun {i} ({e.kind}, {e.status}, {e.rounds} round(s), commit {e.commit[:10]}), checks: {checks}")
        if e.feedback:
            parts.append("Feedback it handled:\n" + "\n".join(f"- {f}" for f in e.feedback))
        if e.gaps:
            parts.append("Gaps the reviewer left open:\n" + "\n".join(f"- {g}" for g in e.gaps))
        if e.report:
            parts.append(f"Its report:\n{e.report.strip()[:3000]}")
    return "\n".join(parts)
