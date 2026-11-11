"""An in-memory stand-in for the GitHub REST API (served through httpx.MockTransport)."""
import json
from typing import Any

import httpx


class FakeGitHub:
    """Just enough of the REST API, in memory, to exercise issues, labels, PRs and permissions."""

    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self.labels: set[str] = set()
        self.prs: list[dict[str, Any]] = []
        self.comments: list[tuple[int, str]] = []
        self.reviewers: list[tuple[int, list[str]]] = []
        self.auth_headers: list[str] = []
        self.next = 1

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.auth_headers.append(req.headers.get("Authorization", ""))
        path, m = req.url.path, req.method
        body = json.loads(req.content) if req.content else {}
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
                              "created_at": f"2026-11-11T10:00:{n:02d}Z",
                              "assignees": body.get("assignees", []), "html_url": f"https://github.com/arcane/app/issues/{n}"}
            return httpx.Response(201, json=self.issues[n])
        if path == "/repos/arcane/app/issues" and m == "GET":
            label = req.url.params["labels"]
            return httpx.Response(200, json=[i for i in sorted(self.issues.values(), key=lambda i: i["number"]) if label in i["labels"]])
        if path.startswith("/repos/arcane/app/issues/") and path.count("/") == 5 and m == "GET":
            return httpx.Response(200, json=self.issues[int(path.split("/")[5])])
        if path.startswith("/repos/arcane/app/issues/") and path.endswith("/labels") and m == "POST":
            n = int(path.split("/")[5])
            self.issues[n]["labels"] += [lbl for lbl in body["labels"] if lbl not in self.issues[n]["labels"]]
            return httpx.Response(200, json=[])
        if "/labels/" in path and m == "DELETE":
            n, label = int(path.split("/")[5]), path.split("/labels/")[1].replace("%3A", ":")
            if label not in self.issues[n]["labels"]:
                return httpx.Response(404, json={"message": "Label does not exist"})
            self.issues[n]["labels"].remove(label)
            return httpx.Response(200, json=[])
        if path.endswith("/comments") and m == "POST":
            self.comments.append((int(path.split("/")[5]), body["body"]))
            return httpx.Response(201, json={})
        if path == "/repos/arcane/app/pulls" and m == "POST":
            pr = {**body, "number": 100 + len(self.prs), "html_url": f"https://github.com/arcane/app/pull/{100 + len(self.prs)}"}
            self.prs.append(pr)
            return httpx.Response(201, json=pr)
        if path.endswith("/requested_reviewers") and m == "POST":
            self.reviewers.append((int(path.split("/")[5]), body["reviewers"]))
            return httpx.Response(201, json={})
        if path.startswith("/repos/arcane/app/collaborators/"):
            login = path.split("/")[5]
            role = {"asha": "write", "viewer": "read"}.get(login)
            return httpx.Response(200, json={"permission": role, "role_name": role}) if role else httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404, json={"message": f"unhandled {m} {path}"})
