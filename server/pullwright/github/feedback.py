"""Review feedback on a Pullwright pull request: the comments a follow-up run must address.

Three kinds count, all from people with write access to the repository:
  - inline review comments on the diff (with file, line and the surrounding hunk),
  - review summaries (the text submitted with "Comment" or "Request changes"),
  - ordinary conversation comments on the PR.
Bots and readers without write access are ignored, so a stranger's comment on a public repository cannot steer
the agents. A comment that starts with `@pullwright ignore` is skipped.

One exception: a message a developer sends in the Pullwright chat while the PR is open is posted on the PR by
Pullwright's own App account, with a hidden marker naming that developer (`chat_comment`). Such a comment counts as
theirs, and their write access is checked like anyone else's, but only when it comes from `trusted_bot` (the App's
own `<slug>[bot]` login). The same marker from any other account is ignored.

`PULLWRIGHT_FEEDBACK_TRIGGER=mention` narrows this to comments that mention @pullwright (chat-relayed comments
always count: the chat is addressed to Pullwright).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .client import GitHub

IGNORE = re.compile(r"^\s*@pullwright\s+ignore\b", re.I)
MENTION = re.compile(r"@pullwright\b", re.I)
CHAT_MARKER = re.compile(r"<!-- pullwright:chat (\{.*?\}) -->")
CHAT_TRAILER = re.compile(r"\n*<sub>[^\n]*via the Pullwright chat</sub>\s*$")


def chat_comment(text: str, name: str, login: str) -> str:
    """A chat message as a PR comment, attributed to the developer who wrote it."""
    meta = json.dumps({"login": login}, separators=(",", ":"))
    return f"{text.strip()}\n\n<sub>{name} (@{login}) via the Pullwright chat</sub>\n<!-- pullwright:chat {meta} -->"


def chat_author(body: str) -> str:
    """The developer named in a chat-relayed comment's marker, or ''."""
    m = CHAT_MARKER.search(body or "")
    if not m:
        return ""
    try:
        return str(json.loads(m.group(1)).get("login") or "")
    except ValueError:
        return ""


def clean(body: str) -> str:
    """The comment without Pullwright's chat trailer and marker."""
    return CHAT_TRAILER.sub("", CHAT_MARKER.sub("", body or "").rstrip()).strip()


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


def _human(item: dict[str, Any], trusted_bot: str = "") -> str | None:
    """Who a comment counts as: its human author; or, for a chat-relayed comment from Pullwright's own App, the
    developer named in its marker; None for any other bot."""
    user = item.get("user") or {}
    login = str(user.get("login", ""))
    if user.get("type") == "Bot" or login.endswith("[bot]"):
        if trusted_bot and login == trusted_bot:
            return chat_author(item.get("body") or "") or None
        return None
    return login or None


def collect(gh: GitHub, repo: str, number: int, since: str = "", mention_only: bool = False,
            can_write: dict[str, bool] | None = None, handled: set[str] | None = None, trusted_bot: str = "") -> list[Feedback]:
    """New feedback on PR `number`, oldest first: created at or after `since` (GitHub times have one-second
    resolution, so the boundary second is included) and not already in `handled` (ids from the work log)."""
    cache = can_write if can_write is not None else {}
    found: list[Feedback] = []

    def keep(item: dict[str, Any], body: str, created: str) -> str | None:
        author = _human(item, trusted_bot)
        if not author or not body.strip() or (since and created < since) or IGNORE.match(body):
            return None
        if mention_only and not MENTION.search(body) and not chat_author(body):  # the chat is addressed to Pullwright
            return None
        if author not in cache:
            cache[author] = gh.can_write(repo, author)
        return author if cache[author] else None

    done = handled or set()
    for c in gh.review_comments(repo, number):
        if (author := keep(c, c.get("body") or "", c.get("created_at", ""))):
            found.append(Feedback(f"review_comment:{c['id']}", "review_comment", author, clean(c["body"]), c["created_at"], c.get("html_url", ""),
                                  c.get("path", ""), c.get("line") or c.get("original_line"), c.get("diff_hunk", "")[-1200:], c["id"]))
    for r in gh.reviews(repo, number):
        created = r.get("submitted_at") or ""
        if (author := keep(r, r.get("body") or "", created)):
            found.append(Feedback(f"review:{r['id']}", "review", author, clean(r["body"]), created, r.get("html_url", ""), github_id=r["id"]))
    for c in gh.issue_comments(repo, number):
        if (author := keep(c, c.get("body") or "", c.get("created_at", ""))):
            found.append(Feedback(f"comment:{c['id']}", "comment", author, clean(c["body"]), c["created_at"], c.get("html_url", ""), github_id=c["id"]))
    return sorted((f for f in found if f.id not in done), key=lambda f: f.created_at)


def conversation(gh: GitHub, repo: str, number: int) -> list[dict[str, Any]]:
    """Everything people said on PR `number`, for the chat: conversation comments, inline review comments and reviews
    (with their verdict), oldest first. Bots and chat-relayed comments are left out (the chat already has those)."""
    out: list[dict[str, Any]] = []

    def person(item: dict[str, Any]) -> str:
        user = item.get("user") or {}
        login = str(user.get("login", ""))
        if user.get("type") == "Bot" or login.endswith("[bot]") or chat_author(item.get("body") or ""):
            return ""
        return login

    for c in gh.issue_comments(repo, number):
        if (who := person(c)) and (c.get("body") or "").strip():
            out.append({"id": f"comment:{c['id']}", "kind": "comment", "author": who, "body": c["body"], "url": c.get("html_url", ""),
                        "at": c.get("created_at", "")})
    for c in gh.review_comments(repo, number):
        if (who := person(c)) and (c.get("body") or "").strip():
            out.append({"id": f"review_comment:{c['id']}", "kind": "review_comment", "author": who, "body": c["body"],
                        "url": c.get("html_url", ""), "at": c.get("created_at", ""), "path": c.get("path", ""),
                        "line": c.get("line") or c.get("original_line")})
    for r in gh.reviews(repo, number):
        state = (r.get("state") or "").upper()
        if (who := person(r)) and ((r.get("body") or "").strip() or state in ("APPROVED", "CHANGES_REQUESTED")):
            out.append({"id": f"review:{r['id']}", "kind": "review", "author": who, "body": r.get("body") or "", "state": state,
                        "url": r.get("html_url", ""), "at": r.get("submitted_at", "")})
    return sorted(out, key=lambda x: x["at"])


def brief(items: list[Feedback]) -> str:
    """The reviewer's comments, as the supervisor and the reviewer agent read them."""
    parts = ["Review feedback to address in this run (from people with write access):"]
    for i, f in enumerate(items, 1):
        if f.path:
            parts.append(f"\n{i}. On {f.path} line {f.line} ({f.author}):\n{f.body.strip()}\nDiff context:\n```\n{f.hunk}\n```")
        else:
            parts.append(f"\n{i}. {f.kind.replace('_', ' ')} by {f.author}:\n{f.body.strip()}")
    return "\n".join(parts)
