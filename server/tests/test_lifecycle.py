import json
import subprocess
from pathlib import Path

import pytest

from pullwright import git
from pullwright.lifecycle import Run
from tests.fake_client import FakeClient, text, tool_use, turn


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


class ScriptedIO:
    def __init__(self, confirm=(True, ""), pr=False):
        self.confirm, self.pr, self.asked = confirm, pr, []

    def confirm_intake(self, intake):
        self.asked.append(("confirm", intake))
        return self.confirm

    def approve_command(self, cmd):
        return True

    def want_pr(self, summary):
        self.asked.append(("pr", summary))
        return self.pr


INTAKE = json.dumps({"understanding": "add hello()", "plan": ["write hello in app.py"], "questions": []})
DIAG_OK = json.dumps({"complete": True, "gaps": [], "evidence": "read app.py"})
DIAG_BAD = json.dumps({"complete": False, "gaps": ["hello() missing docstring"], "evidence": "read app.py"})


def test_full_lifecycle(repo: Path):
    turns = [
        turn(text(INTAKE)),  # intake
        turn(tool_use("spawn_worker", {"task": "add hello()", "context": "app.py"})),  # supervisor
        turn(tool_use("edit_file", {"path": "app.py", "old": "x = 1\n", "new": "x = 1\n\ndef hello():\n    return 'hi'\n"}, "tu_w")),  # worker
        turn(text("added hello()")),  # worker done
        turn(text("delegated and verified")),  # supervisor done
        turn(text(DIAG_OK)),  # diagnosis
        turn(text("## Summary\nAdded hello().\nTITLE: add hello")),  # summarizer
    ]
    io = ScriptedIO(pr=False)
    states = []
    run = Run(task="add a hello() function", cwd=repo, io=io, client=FakeClient(turns))
    run.bus.subscribe(lambda e: states.append(e.payload["state"]) if e.type == "state" else None)
    run.run()

    assert run.state == "done"
    assert states == ["intake", "awaiting_confirmation", "working", "diagnosing", "summarizing", "awaiting_pr_decision", "done"]
    assert run.branch == "pullwright/add-a-hello-function" and git.current_branch(repo) == run.branch
    assert run.commit and "def hello" in (repo / "app.py").read_text()
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout == ""
    assert [k for k, _ in io.asked] == ["confirm", "pr"]
    assert run.cost_usd == pytest.approx(7 * (100 * 4 + 50 * 20) / 1e6)


def test_intake_feedback_then_diagnosis_retry(repo: Path):
    turns = [
        turn(text(INTAKE)),  # intake v1 -> rejected
        turn(text(INTAKE)),  # intake v2 -> accepted
        turn(text("done")),  # supervisor (does nothing)
        turn(text(DIAG_BAD)),  # diagnosis: gaps
        turn(text("fixed")),  # supervisor retry
        turn(text(DIAG_OK)),  # diagnosis ok
        turn(text("sum\nTITLE: t")),
    ]
    class RejectOnce(ScriptedIO):
        def confirm_intake(self, intake):
            self.asked.append(("confirm", intake))
            return (len(self.asked) > 1, "be more specific")

    io = RejectOnce()
    client = FakeClient(turns)
    run = Run(task="t", cwd=repo, io=io, client=client).run()
    assert run.state == "done" and run.diagnosis["complete"]
    assert "User feedback:\nbe more specific" in client.calls[1]["messages"][0]["content"]
    assert "hello() missing docstring" in client.calls[4]["messages"][0]["content"]
    assert client.calls[0]["output_config"]["format"]["type"] == "json_schema"


def test_failure_sets_state(repo: Path):
    run = Run(task="t", cwd=repo, io=ScriptedIO(), client=FakeClient([turn(text("not json"))]))
    with pytest.raises(json.JSONDecodeError):
        run.run()
    assert run.state == "failed"


def test_commit_skips_pycache(repo: Path):
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "app.cpython-313.pyc").write_bytes(b"\x00")
    (repo / "new.py").write_text("y = 2\n")
    assert git.commit_all(repo, "c")
    tracked = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True).stdout.split()
    assert "new.py" in tracked and not any("pycache" in t for t in tracked)
    assert "pycache" not in git.diff(repo)
