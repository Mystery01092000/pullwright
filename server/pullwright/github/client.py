"""GitHub REST access for the control plane and the runner.

Authentication is either a GitHub App (recommended for an organization: short-lived installation tokens,
scoped to the one repository a job touches) or a personal access token for local development.
"""
from __future__ import annotations

import calendar
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

API = "https://api.github.com"
_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "pullwright"}


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub {status}: {message}")
        self.status = status


class TokenProvider(Protocol):
    def token(self, repo: str) -> str:
        """A token that can read and write `repo` (owner/name)."""
        ...


@dataclass
class PatAuth:
    pat: str

    def token(self, repo: str) -> str:
        return self.pat


@dataclass
class AppAuth:
    """GitHub App: a JWT signed with the app's private key, exchanged per repository for an installation token
    that expires within an hour and can touch only that repository."""
    app_id: str
    private_key_pem: str
    http: httpx.Client = field(default_factory=lambda: httpx.Client(base_url=API, headers=_HEADERS, timeout=30))
    _cache: dict[str, tuple[str, float]] = field(default_factory=dict)

    def jwt(self) -> str:
        import jwt  # pyjwt

        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.private_key_pem, algorithm="RS256")

    def token(self, repo: str) -> str:
        cached = self._cache.get(repo)
        if cached and cached[1] - time.time() > 300:
            return cached[0]
        auth = {"Authorization": f"Bearer {self.jwt()}"}
        r = self.http.get(f"/repos/{repo}/installation", headers=auth)
        _raise(r)
        inst = r.json()["id"]
        r = self.http.post(f"/app/installations/{inst}/access_tokens", headers=auth, json={"repositories": [repo.split("/", 1)[1]]})
        _raise(r)
        data = r.json()
        expires = calendar.timegm(time.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))  # UTC
        self._cache[repo] = (data["token"], expires)
        return data["token"]


def _raise(r: httpx.Response) -> None:
    if r.status_code >= 400:
        try:
            msg = r.json().get("message", r.text)
        except ValueError:
            msg = r.text
        raise GitHubError(r.status_code, msg)


class GitHub:
    """The handful of REST calls Pullwright needs. Every call names its repository, so one client serves many."""

    def __init__(self, auth: TokenProvider, http: httpx.Client | None = None) -> None:
        self.auth = auth
        self.http = http or httpx.Client(base_url=API, headers=_HEADERS, timeout=30)

    def _req(self, method: str, repo: str, path: str, **kw: Any) -> Any:
        r = self.http.request(method, f"/repos/{repo}{path}", headers={"Authorization": f"Bearer {self.auth.token(repo)}"}, **kw)
        _raise(r)
        return r.json() if r.content else None

    # issues
    def create_issue(self, repo: str, title: str, body: str, labels: list[str], assignees: list[str] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "body": body, "labels": labels}
        if assignees:
            payload["assignees"] = assignees
        return self._req("POST", repo, "/issues", json=payload)

    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        return self._req("GET", repo, f"/issues/{number}")

    def list_issues(self, repo: str, label: str) -> list[dict[str, Any]]:
        """Open issues with `label`, oldest first (the queue order). Pull requests are filtered out."""
        items = self._req("GET", repo, "/issues", params={"labels": label, "state": "open", "sort": "created", "direction": "asc", "per_page": 100})
        return [i for i in items if "pull_request" not in i]

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self._req("POST", repo, f"/issues/{number}/labels", json={"labels": labels})

    def remove_label(self, repo: str, number: int, label: str) -> None:
        try:
            self._req("DELETE", repo, f"/issues/{number}/labels/{label}")
        except GitHubError as e:
            if e.status != 404:  # already gone
                raise

    def ensure_label(self, repo: str, name: str, color: str, description: str) -> None:
        try:
            self._req("POST", repo, "/labels", json={"name": name, "color": color, "description": description})
        except GitHubError as e:
            if e.status != 422:  # exists
                raise

    def comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        return self._req("POST", repo, f"/issues/{number}/comments", json={"body": body})

    # pull requests
    def create_pr(self, repo: str, head: str, base: str, title: str, body: str, draft: bool = False) -> dict[str, Any]:
        return self._req("POST", repo, "/pulls", json={"head": head, "base": base, "title": title, "body": body, "draft": draft})

    def request_reviewers(self, repo: str, number: int, reviewers: list[str]) -> None:
        self._req("POST", repo, f"/pulls/{number}/requested_reviewers", json={"reviewers": reviewers})

    def get_pr(self, repo: str, number: int) -> dict[str, Any]:
        return self._req("GET", repo, f"/pulls/{number}")

    def open_prs(self, repo: str, label: str) -> list[dict[str, Any]]:
        """Open pull requests carrying `label` (labels live on the PR's issue side)."""
        items = self._req("GET", repo, "/issues", params={"labels": label, "state": "open", "per_page": 100})
        return [i for i in items if "pull_request" in i]

    def issue_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        """Conversation comments on an issue or a pull request, oldest first."""
        return self._req("GET", repo, f"/issues/{number}/comments", params={"per_page": 100})

    def review_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        """Inline comments on a pull request's diff (path, line, diff_hunk), oldest first."""
        return self._req("GET", repo, f"/pulls/{number}/comments", params={"per_page": 100})

    def reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self._req("GET", repo, f"/pulls/{number}/reviews", params={"per_page": 100})

    def reply_to_review_comment(self, repo: str, number: int, comment_id: int, body: str) -> dict[str, Any]:
        return self._req("POST", repo, f"/pulls/{number}/comments/{comment_id}/replies", json={"body": body})

    # repository
    def default_branch(self, repo: str) -> str:
        return self._req("GET", repo, "")["default_branch"]

    def permission(self, repo: str, login: str) -> str:
        """admin | maintain | write | triage | read | none"""
        try:
            data = self._req("GET", repo, f"/collaborators/{login}/permission")
        except GitHubError as e:
            if e.status == 404:
                return "none"
            raise
        return data.get("role_name") or data.get("permission", "none")

    def can_write(self, repo: str, login: str) -> bool:
        return self.permission(repo, login) in ("admin", "maintain", "write")

    def clone_url(self, repo: str) -> str:
        """HTTPS URL carrying a token, for git clone/push inside a runner. Never log it."""
        return f"https://x-access-token:{self.auth.token(repo)}@github.com/{repo}.git"
