"""Review feedback on a Pullwright pull request: the comments a follow-up run must address.

Three kinds count, all from people with write access to the repository:
  - inline review comments on the diff (with file, line and the surrounding hunk),
  - review summaries (the text submitted with "Comment" or "Request changes"),
  - ordinary conversation comments on the PR.
Bots (Pullwright itself included) and readers without write access are ignored, so a stranger's comment on a public
repository cannot steer the agents. A comment that starts with `@pullwright ignore` is skipped.

`PULLWRIGHT_FEEDBACK_TRIGGER=mention` narrows this to comments that mention @pullwright.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .client import GitHub

IGNORE = re.compile(r"^\s*@pullwright\s+ignore\b", re.I)
MENTION = re.compile(r"@pullwright\b", re.I)


@dataclass
class Feedback:
    id: str  # "<kind>:<github id>", unique across the three kinds
    kind: str  # review_comment | review | comment
    author: str
    body: str
    created_at: str
    url: str
    path: str = ""
    line: int | None = None
    hunk: str = ""
    github_id: int = 0

    def summary(self) -> str:
        where = f"{self.path}:{self.line}" if self.path else ("review" if self.kind == "review" else "comment")
        first = " ".join(self.body.split())[:140]
        return f"{where} ({self.author}): {first}"


def _human(item: dict[str, Any]) -> str | None:
    user = item.get("user") or {}
    if user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]"):
        return None
    return user.get("login")


def collect(gh: GitHub, repo: str, number: int, since: str = "", mention_only: bool = False,
            can_write: dict[str, bool] | None = None, handled: set[str] | None = None) -> list[Feedback]:
    """New feedback on PR `number`, oldest first: created at or after `since` (GitHub times have one-second
    resolution, so the boundary second is included) and not already in `handled` (ids from the work log)."""
    cache = can_write if can_write is not None else {}
    found: list[Feedback] = []

    def keep(item: dict[str, Any], body: str, created: str) -> str | None:
        author = _human(item)
        if not author or not body.strip() or (since and created < since) or IGNORE.match(body):
            return None
        if mention_only and not MENTION.search(body):
            return None
        if author not in cache:
            cache[author] = gh.can_write(repo, author)
        return author if cache[author] else None

    done = handled or set()
    for c in gh.review_comments(repo, number):
        if (author := keep(c, c.get("body") or "", c.get("created_at", ""))):
            found.append(Feedback(f"review_comment:{c['id']}", "review_comment", author, c["body"], c["created_at"], c.get("html_url", ""),
                                  c.get("path", ""), c.get("line") or c.get("original_line"), c.get("diff_hunk", "")[-1200:], c["id"]))
    for r in gh.reviews(repo, number):
        created = r.get("submitted_at") or ""
        if (author := keep(r, r.get("body") or "", created)):
            found.append(Feedback(f"review:{r['id']}", "review", author, r["body"], created, r.get("html_url", ""), github_id=r["id"]))
    for c in gh.issue_comments(repo, number):
        if (author := keep(c, c.get("body") or "", c.get("created_at", ""))):
            found.append(Feedback(f"comment:{c['id']}", "comment", author, c["body"], c["created_at"], c.get("html_url", ""), github_id=c["id"]))
    return sorted((f for f in found if f.id not in done), key=lambda f: f.created_at)


def brief(items: list[Feedback]) -> str:
    """The reviewer's comments, as the supervisor and the reviewer agent read them."""
    parts = ["Review feedback to address in this run (from people with write access):"]
    for i, f in enumerate(items, 1):
        if f.path:
            parts.append(f"\n{i}. On {f.path} line {f.line} ({f.author}):\n{f.body.strip()}\nDiff context:\n```\n{f.hunk}\n```")
        else:
            parts.append(f"\n{i}. {f.kind.replace('_', ' ')} by {f.author}:\n{f.body.strip()}")
    return "\n".join(parts)
