import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pullwright.checks import CheckResult
from pullwright.github import issues, prs
from pullwright.github.client import API, AppAuth, GitHub, PatAuth
from pullwright.pipeline.plan import Developer, Plan, PlanRequest
from pullwright.pipeline.work import WorkResult
from tests.fake_github import FakeGitHub


@pytest.fixture
def gh() -> tuple[GitHub, FakeGitHub]:
    fake = FakeGitHub()
    client = httpx.Client(base_url=API, transport=httpx.MockTransport(fake.handler))
    return GitHub(PatAuth("tok"), http=client), fake


DEV = Developer(name="Asha Rao", github_login="asha", channel="slack", channel_user_id="U1")
SPEC = {"summary": "Add hello()", "relevant_files": [{"path": "app.py", "why": "target"}], "plan": ["add hello"],
        "acceptance_criteria": ["hello() returns 'hi'"], "test_plan": ["pytest"], "risks": [], "questions": []}
VERIFIED = {"approved": True, "corrections": [], "evidence": "read app.py"}


def _plan(task="Add a hello() function\nwith a docstring") -> Plan:
    req = PlanRequest(task=task, repo="arcane/app", requester=DEV, thread_url="https://slack.example/t/1", thread_ref={"channel": "C1", "ts": "1.2"})
    return Plan(req, SPEC, VERIFIED, DEV, 1, 0.042)


def test_open_issue_carries_the_developer_the_plan_and_a_machine_block(gh):
    client, fake = gh
    issue = issues.open_issue(client, _plan())
    assert issue["title"] == "Add a hello() function"
    assert issue["labels"] == ["pullwright", "pullwright:queued"] and issue["assignees"] == ["asha"]
    body = issue["body"]
    assert "Requested by** Asha Rao (@asha) via slack, [thread](https://slack.example/t/1)" in body
    assert "- [ ] hello() returns 'hi'" in body and "1 revision" in body
    assert set(issues.LABEL_SPECS) <= fake.labels
    task = issues.parse("arcane/app", issue)
    assert task.spec == SPEC and task.requester_login == "asha" and task.thread_ref == {"channel": "C1", "ts": "1.2"}
    assert task.task.startswith("Add a hello() function\nwith")


def test_queue_is_oldest_first_and_ignores_foreign_issues(gh):
    client, fake = gh
    first = issues.open_issue(client, _plan("first task"))
    second = issues.open_issue(client, _plan("second task"))
    client.create_issue("arcane/app", "human issue", "no block here", ["pullwright:queued"])
    assert [t.number for t in issues.queued(client, "arcane/app")] == [first["number"], second["number"]]
    issues.set_state(client, "arcane/app", first["number"], issues.WORKING)
    assert fake.issues[first["number"]]["labels"] == ["pullwright", "pullwright:working"]
    assert [t.number for t in issues.queued(client, "arcane/app")] == [second["number"]]


def _result(green: bool) -> WorkResult:
    check = CheckResult("test", "pytest -q", 0 if green else 1, 2.0, "1 passed" if green else "E assert 1 == 2\n1 failed")
    diag = {"complete": True, "gaps": [], "evidence": "read diff"} if green else None
    return WorkResult(status="green" if green else "failed", rounds=2, branch="pullwright/1-add-hello", commit="abc123",
                      checks=[check], diagnosis=diag, report="Added hello() in app.py.", cost_usd=0.31)


def test_green_run_opens_a_ready_pr_tagging_the_developer(gh):
    client, fake = gh
    task = issues.parse("arcane/app", issues.open_issue(client, _plan()))
    pr = prs.open_pr(client, task, _result(green=True))
    assert pr["draft"] is False and pr["head"] == "pullwright/1-add-hello" and pr["base"] == "main"
    assert pr["body"].startswith(f"Closes #{task.number}. Ready for review, @asha.")
    assert "**test** `pytest -q`: pass" in pr["body"] and "$0.31" in pr["body"]
    assert fake.reviewers == [(pr["number"], ["asha"])]
    assert fake.issues[task.number]["labels"] == ["pullwright", "pullwright:pr-open"]
    assert fake.comments[-1] == (task.number, f"Pull request opened: {pr['html_url']}")


def test_red_run_opens_a_draft_and_marks_the_issue_failed(gh):
    client, fake = gh
    task = issues.parse("arcane/app", issues.open_issue(client, _plan()))
    pr = prs.open_pr(client, task, _result(green=False))
    assert pr["draft"] is True and pr["body"].startswith(f"Refs #{task.number}. Draft")
    assert "E assert 1 == 2" in pr["body"]
    assert fake.issues[task.number]["labels"] == ["pullwright", "pullwright:failed"]


def test_write_permission_decides_who_may_approve(gh):
    client, _ = gh
    assert client.can_write("arcane/app", "asha") and not client.can_write("arcane/app", "viewer")
    assert not client.can_write("arcane/app", "stranger")


def test_app_auth_signs_a_jwt_and_scopes_the_installation_token_to_one_repo():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/repos/arcane/app/installation":
            return httpx.Response(200, json={"id": 42})
        if req.url.path == "/app/installations/42/access_tokens":
            return httpx.Response(201, json={"token": "ghs_x", "expires_at": "2999-01-01T00:00:00Z"})
        return httpx.Response(404)

    auth = AppAuth("123", pem, http=httpx.Client(base_url=API, transport=httpx.MockTransport(handler)))
    assert auth.token("arcane/app") == "ghs_x"
    assert auth.token("arcane/app") == "ghs_x" and len(seen) == 2  # cached until near expiry
    claims = jwt.decode(seen[0].headers["Authorization"].split()[1], key.public_key(), algorithms=["RS256"])
    assert claims["iss"] == "123"
    assert json.loads(seen[1].content) == {"repositories": ["app"]}


def test_issue_title_comes_from_the_plan_when_the_describer_wrote_one(gh):
    client, _ = gh
    plan = _plan("a very long request " * 10)
    plan.spec = {**SPEC, "title": "Add hello() with a docstring"}
    assert issues.open_issue(client, plan)["title"] == "Add hello() with a docstring"
