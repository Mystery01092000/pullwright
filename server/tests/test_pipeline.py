import json
import subprocess
import sys
from pathlib import Path

import pytest

from pullwright import git
from pullwright.checks import CheckPlan
from pullwright.pipeline.plan import Developer, PlanCancelled, PlanRequest, PlanRun, Review, render_markdown
from pullwright.pipeline.work import WorkRun, WorkSpec
from pullwright.session import Session
from tests.fake_client import FakeClient, text, tool_use, turn


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@t"], ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


DEV = Developer(name="Asha Rao", github_login="asha", channel="slack", channel_user_id="U1")
SPEC = {
    "summary": "Add hello() to app.py",
    "relevant_files": [{"path": "app.py", "why": "where hello() goes"}],
    "plan": ["Add def hello() returning 'hi' to app.py"],
    "acceptance_criteria": ["hello() returns 'hi'"],
    "test_plan": ["python -c 'import app; assert app.hello() == \"hi\"'"],
    "risks": [],
    "questions": [],
}
OK = json.dumps({"approved": True, "corrections": [], "evidence": "opened app.py"})
BAD = json.dumps({"approved": False, "corrections": ["step 1 must also add a docstring"], "evidence": "opened app.py"})


class ScriptedReviewer:
    def __init__(self, *reviews: Review):
        self.reviews = list(reviews)
        self.seen: list[tuple[dict, dict, int]] = []

    def review_plan(self, request, spec, verification, revision):
        self.seen.append((spec, verification, revision))
        return self.reviews.pop(0)


def _plan_run(repo: Path, turns, *reviews: Review) -> tuple[PlanRun, FakeClient, ScriptedReviewer]:
    client = FakeClient(turns)
    reviewer = ScriptedReviewer(*reviews)
    req = PlanRequest(task="add a hello() function", repo="arcane/app", requester=DEV)
    return PlanRun(Session(cwd=repo, client=client), req, reviewer), client, reviewer


def _prompt(call: dict) -> str:
    return call["messages"][0]["content"]


def test_plan_describer_then_verifier_then_developer_approval(repo: Path):
    run, client, reviewer = _plan_run(repo, [turn(text(json.dumps(SPEC))), turn(text(OK))], Review("approve", reviewer=DEV))
    plan = run.run()
    assert plan.spec == SPEC and plan.approved_by.github_login == "asha" and plan.revisions == 0
    assert [c["model"] for c in client.calls] == ["claude-sonnet-5-5", "claude-sonnet-5-5"]
    assert "Repository map" in _prompt(client.calls[0]) and "app.py" in _prompt(client.calls[0])
    assert "Plan to verify" in _prompt(client.calls[1])
    # read-only: neither planning agent can write or run commands
    for call in client.calls:
        names = {t["name"] for t in call["tools"]}
        assert names <= {"read_file", "glob", "grep"}
    assert reviewer.seen[0][1]["approved"] is True


def test_plan_verifier_objection_gets_one_describer_revision(repo: Path):
    revised = {**SPEC, "plan": ["Add def hello() with a docstring returning 'hi'"]}
    turns = [turn(text(json.dumps(SPEC))), turn(text(BAD)), turn(text(json.dumps(revised))), turn(text(OK))]
    run, client, reviewer = _plan_run(repo, turns, Review("approve"))
    plan = run.run()
    assert plan.spec == revised
    assert "step 1 must also add a docstring" in _prompt(client.calls[2])
    assert plan.approved_by == DEV  # no reviewer identity given: the requester


def test_plan_developer_feedback_revises_then_cancel_raises(repo: Path):
    turns = [turn(text(json.dumps(SPEC))), turn(text(OK)), turn(text(json.dumps(SPEC))), turn(text(OK))]
    run, client, reviewer = _plan_run(repo, turns, Review("revise", "also log the call"), Review("cancel", "not now"))
    with pytest.raises(PlanCancelled, match="not now"):
        run.run()
    assert "also log the call" in _prompt(client.calls[2])
    assert [r for _, _, r in reviewer.seen] == [0, 1]


def test_render_markdown_reads_as_a_plan():
    md = render_markdown(SPEC, json.loads(OK))
    assert "1. Add def hello()" in md and "- [ ] hello() returns 'hi'" in md and "checked against the code" in md


# --- work -------------------------------------------------------------------

PY = sys.executable
HELLO_CHECK = f'"{PY}" -c "import app, sys; sys.exit(0 if getattr(app, \'hello\', lambda: None)() == \'hi\' else 1)"'
DIAG_OK = json.dumps({"complete": True, "gaps": [], "evidence": "read the diff"})
DIAG_GAP = json.dumps({"complete": False, "gaps": ["hello() has no docstring"], "evidence": "read the diff"})


def _worker_edit(new: str, id_: str) -> list:
    """supervisor -> spawn_worker -> worker writes app.py -> worker done -> supervisor done"""
    return [
        turn(tool_use("spawn_worker", {"task": "write hello", "context": "app.py"}, f"s_{id_}")),
        turn(tool_use("write_file", {"path": "app.py", "content": new}, f"w_{id_}"), model="claude-haiku-5-5"),
        turn(text("wrote app.py"), model="claude-haiku-5-5"),
        turn(text("delegated and checked")),
    ]


def _work(repo: Path, turns, checks: CheckPlan | None = None, max_rounds: int = 3, **session_kw) -> tuple[WorkRun, FakeClient]:
    client = FakeClient(turns)
    spec = WorkSpec(repo="arcane/app", issue_number=7, title="Add hello", task="add a hello() function", spec=SPEC, requester_login="asha")
    plan = checks or CheckPlan(checks={"test": HELLO_CHECK})
    return WorkRun(Session(cwd=repo, client=client, **session_kw), spec, plan, max_rounds=max_rounds), client


def test_work_loops_on_failing_checks_until_green(repo: Path):
    turns = [
        *_worker_edit("x = 1\n\ndef hello():\n    return 'hey'\n", "1"),  # round 1: wrong value, check fails
        *_worker_edit("x = 1\n\ndef hello():\n    return 'hi'\n", "2"),  # round 2: fixed
        turn(text(DIAG_OK)),
    ]
    run, client = _work(repo, turns)
    res = run.run()
    assert res.status == "green" and res.rounds == 2
    assert res.branch == "pullwright/7-add-hello" and git.current_branch(repo) == res.branch and res.commit
    assert "These checks fail" in _prompt(client.calls[4]) and "fail (exit 1)" in _prompt(client.calls[4])
    worker_calls = [c for c in client.calls if c["model"] == "claude-haiku-5-5"]
    assert len(worker_calls) == 4 and all("fallbacks" not in c for c in worker_calls)
    assert {c["model"] for c in client.calls if c not in worker_calls} == {"claude-sonnet-5-5"}
    leftover = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout.splitlines()
    assert all(not line or "__pycache__" in line for line in leftover)  # everything but build junk is committed


def test_work_reviewer_gaps_send_the_supervisor_back(repo: Path):
    good = "x = 1\n\ndef hello():\n    return 'hi'\n"
    turns = [*_worker_edit(good, "1"), turn(text(DIAG_GAP)), *_worker_edit('x = 1\n\ndef hello():\n    """Greet."""\n    return \'hi\'\n', "2"), turn(text(DIAG_OK))]
    run, client = _work(repo, turns)
    res = run.run()
    assert res.status == "green" and res.rounds == 2
    assert "hello() has no docstring" in _prompt(client.calls[5])


def test_work_stops_red_after_max_rounds_and_still_commits(repo: Path):
    bad = "x = 1\n\ndef hello():\n    return 'nope'\n"
    run, _ = _work(repo, [*_worker_edit(bad, "1"), *_worker_edit(bad, "2"), *_worker_edit(bad, "3")])
    res = run.run()
    assert res.status == "failed" and res.rounds == 3 and res.commit
    assert not res.checks[0].ok


def test_work_setup_failure_stops_before_any_agent(repo: Path):
    plan = CheckPlan(setup=[f'"{PY}" -c "import sys; sys.exit(3)"'], checks={"test": HELLO_CHECK})
    run, client = _work(repo, [], checks=plan)
    res = run.run()
    assert res.status == "setup_failed" and client.calls == [] and "dependency setup failed" in res.error


def test_work_budget_cap_ends_the_run(repo: Path):
    run, _ = _work(repo, [*_worker_edit("x = 2\n", "1"), *_worker_edit("x = 3\n", "2")], max_cost_usd=0.000001)
    res = run.run()
    assert res.status == "budget" and "exceeds cap" in res.error


def test_runner_agents_cannot_push(repo: Path):
    run, _ = _work(repo, [])
    from pullwright.tools import execute

    out, err = execute(run.s.ctx(), "bash", {"command": "git push origin HEAD"}, ["bash"], None)
    assert err and "denylisted" in out
