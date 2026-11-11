"""An in-memory stand-in for the GitHub REST API (served through httpx.MockTransport)."""
import json
from typing import Any

import httpx

BOT = {"login": "pullwright-app[bot]", "type": "Bot"}


class FakeGitHub:
    """Just enough of the REST API, in memory, to exercise issues, labels, PRs, comments, reviews and permissions.

    Like GitHub, a pull request is also an issue: it shares the number space, labels and conversation comments.
    Requests made through the app are authored by BOT; tests add human comments with `human_comment` & co.
    """

    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self.labels: set[str] = set()
        self.prs: list[dict[str, Any]] = []
        self.comments: list[tuple[int, str]] = []  # (number, body) of every comment posted through the API
        self.issue_comments: dict[int, list[dict[str, Any]]] = {}
        self.review_comments: dict[int, list[dict[str, Any]]] = {}
        self.reviews: dict[int, list[dict[str, Any]]] = {}
        self.replies: list[tuple[int, int, str]] = []  # (pr, comment id, body)
        self.reviewers: list[tuple[int, list[str]]] = []
        self.auth_headers: list[str] = []
        self.next = 1
        self.next_id = 1000
        self.clock = 0

    # --- helpers for tests --------------------------------------------------------------------

    def _ts(self) -> str:
        self.clock += 1
        return f"2026-11-11T10:{self.clock // 60:02d}:{self.clock % 60:02d}Z"

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def human_comment(self, number: int, login: str, body: str, at: str | None = None) -> None:
        self.issue_comments.setdefault(number, []).append(
            {"id": self._id(), "user": {"login": login, "type": "User"}, "body": body, "created_at": at or self._ts(), "html_url": "u"})

    def human_review_comment(self, number: int, login: str, body: str, path: str, line: int, at: str | None = None) -> None:
        self.review_comments.setdefault(number, []).append(
            {"id": self._id(), "user": {"login": login, "type": "User"}, "body": body, "path": path, "line": line,
             "diff_hunk": "@@ -1 +1 @@\n-x = 1\n+x = 2", "created_at": at or self._ts(), "html_url": "u"})

    def human_review(self, number: int, login: str, body: str, state: str = "CHANGES_REQUESTED", at: str | None = None) -> None:
        self.reviews.setdefault(number, []).append(
            {"id": self._id(), "user": {"login": login, "type": "User"}, "body": body, "state": state, "submitted_at": at or self._ts(), "html_url": "u"})

    # --- the API ------------------------------------------------------------------------------

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.auth_headers.append(req.headers.get("Authorization", ""))
        path, m = req.url.path, req.method
        body = json.loads(req.content) if req.content else {}
        parts = path.split("/")
        if path == "/repos/arcane/app" and m == "GET":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/repos/arcane/app/labels" and m == "POST":
            if body["name"] in self.labels:
                return httpx.Response(422, json={"message": "already_exists"})
            self.labels.add(body["name"])
            return httpx.Response(201, json=body)
        if path == "/repos/arcane/app/issues" and m == "POST":
            n = self.next
            self.next += 1
            self.issues[n] = {"number": n, "title": body["title"], "body": body["body"], "labels": list(body["labels"]),
                              "created_at": f"2026-11-11T09:00:{n:02d}Z", "user": BOT,
                              "assignees": body.get("assignees", []), "html_url": f"https://github.com/arcane/app/issues/{n}"}
            return httpx.Response(201, json=self.issues[n])
        if path == "/repos/arcane/app/issues" and m == "GET":
            label = req.url.params["labels"]
            return httpx.Response(200, json=[i for i in sorted(self.issues.values(), key=lambda i: i["number"]) if label in i["labels"]])
        if path.startswith("/repos/arcane/app/issues/") and len(parts) == 6 and m == "GET":
            return httpx.Response(200, json=self.issues[int(parts[5])])
        if path.startswith("/repos/arcane/app/issues/") and path.endswith("/labels") and m == "POST":
            n = int(parts[5])
            self.issues[n]["labels"] += [lbl for lbl in body["labels"] if lbl not in self.issues[n]["labels"]]
            return httpx.Response(200, json=[])
        if "/labels/" in path and m == "DELETE":
            n, label = int(parts[5]), path.split("/labels/")[1].replace("%3A", ":")
            if label not in self.issues[n]["labels"]:
                return httpx.Response(404, json={"message": "Label does not exist"})
            self.issues[n]["labels"].remove(label)
            return httpx.Response(200, json=[])
        if path.startswith("/repos/arcane/app/issues/") and path.endswith("/comments"):
            n = int(parts[5])
            if m == "GET":
                return httpx.Response(200, json=self.issue_comments.get(n, []))
            self.comments.append((n, body["body"]))
            c = {"id": self._id(), "user": BOT, "body": body["body"], "created_at": self._ts(), "html_url": "u"}
            self.issue_comments.setdefault(n, []).append(c)
            return httpx.Response(201, json=c)
        if path == "/repos/arcane/app/pulls" and m == "POST":
            n = self.next
            self.next += 1
            pr = {**body, "number": n, "html_url": f"https://github.com/arcane/app/pull/{n}", "created_at": self._ts()}
            self.prs.append(pr)  # as sent: "head" is the branch name
            self.issues[n] = {"number": n, "title": body["title"], "body": body["body"], "labels": [], "pull_request": {},
                              "user": BOT, "html_url": pr["html_url"], "created_at": pr["created_at"]}
            return httpx.Response(201, json=pr)
        if path.startswith("/repos/arcane/app/pulls/") and len(parts) == 6 and m == "GET":
            pr = next(p for p in self.prs if p["number"] == int(parts[5]))
            return httpx.Response(200, json={**pr, "head": {"ref": pr["head"]}, "base": {"ref": pr["base"]}})  # the API's shape
        if path.endswith("/requested_reviewers") and m == "POST":
            self.reviewers.append((int(parts[5]), body["reviewers"]))
            return httpx.Response(201, json={})
        if path.startswith("/repos/arcane/app/pulls/") and path.endswith("/comments") and m == "GET":
            return httpx.Response(200, json=self.review_comments.get(int(parts[5]), []))
        if path.startswith("/repos/arcane/app/pulls/") and path.endswith("/reviews") and m == "GET":
            return httpx.Response(200, json=self.reviews.get(int(parts[5]), []))
        if path.endswith("/replies") and m == "POST":
            self.replies.append((int(parts[5]), int(parts[7]), body["body"]))
            return httpx.Response(201, json={})
        if path.startswith("/repos/arcane/app/collaborators/"):
            login = parts[5]
            role = {"asha": "write", "viewer": "read"}.get(login)
            return httpx.Response(200, json={"permission": role, "role_name": role}) if role else httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404, json={"message": f"unhandled {m} {path}"})
