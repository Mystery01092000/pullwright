import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from pullwright.dispatcher import Dispatcher
from pullwright.github import issues
from pullwright.github.client import API, GitHub, PatAuth
from pullwright.pipeline.plan import Developer, Plan, PlanRequest
from pullwright.runner.job import parse_ref, run_job
from pullwright.runners.base import Job
from pullwright.runners.docker import DockerRunner
from tests.fake_client import FakeClient, text, tool_use, turn
from tests.fake_github import FakeGitHub

DEV = Developer(name="Asha Rao", github_login="asha", channel="slack")
SPEC = {"summary": "Add hello()", "relevant_files": [{"path": "app.py", "why": "target"}], "plan": ["add hello"],
        "acceptance_criteria": ["hello() returns 'hi'"], "test_plan": ["checks"], "risks": [], "questions": []}


class LocalGitHub(GitHub):
    """The fake API, plus a bare repository on disk standing in for github.com/arcane/app.git."""

    def __init__(self, fake: FakeGitHub, remote: Path) -> None:
        super().__init__(PatAuth("tok"), http=httpx.Client(base_url=API, transport=httpx.MockTransport(fake.handler)))
        self.remote = remote

    def clone_url(self, repo: str) -> str:
        return str(self.remote)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """A bare repo with app.py and a checks.json whose test passes only once hello() returns 'hi'."""
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "t")
    (src / "app.py").write_text("x = 1\n")
    (src / ".pullwright").mkdir()
    check = f'"{sys.executable}" -c "import app, sys; sys.exit(0 if getattr(app, \'hello\', lambda: 0)() == \'hi\' else 1)"'
    (src / ".pullwright" / "checks.json").write_text(json.dumps({"setup": None, "test": check}))
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "init")
    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "-q", "--bare", str(src), str(bare))
    return bare


def _queue_issue(gh: GitHub, task: str = "Add hello") -> int:
    plan = Plan(PlanRequest(task=task, repo="arcane/app", requester=DEV), SPEC, {"approved": True, "corrections": [], "evidence": ""}, DEV, 0, 0.01)
    return issues.open_issue(gh, plan)["number"]


def test_job_implements_pushes_and_opens_the_pr(tmp_path: Path, remote: Path):
    fake = FakeGitHub()
    gh = LocalGitHub(fake, remote)
    number = _queue_issue(gh)
    turns = [
        turn(tool_use("spawn_worker", {"task": "add hello", "context": "app.py"}, "s1")),
        turn(tool_use("write_file", {"path": "app.py", "content": "x = 1\n\ndef hello():\n    return 'hi'\n"}, "w1"), model="claude-haiku-5-5"),
        turn(text("done"), model="claude-haiku-5-5"),
        turn(text("Added hello() to app.py.")),
        turn(text(json.dumps({"complete": True, "gaps": [], "evidence": "diff"}))),
    ]
    work = tmp_path / "work"
    work.mkdir()
    res = run_job("arcane/app", number, gh, FakeClient(turns), work)
    assert res.status == "green" and res.branch == f"pullwright/{number}-add-hello"
    assert res.branch in _git(remote, "branch", "--list")  # pushed
    assert "def hello" in _git(remote, "show", f"{res.branch}:app.py")
    assert _git(remote, "log", "-1", "--format=%an", res.branch).strip() == "pullwright[bot]"
    pr = fake.prs[0]
    assert pr["head"] == res.branch and pr["draft"] is False and pr["body"].startswith(f"Closes #{number}.")
    assert fake.issues[number]["labels"] == ["pullwright", "pullwright:pr-open"]
    assert fake.comments[0] == (number, "A Pullwright runner picked this up.")


def test_job_with_no_changes_marks_the_issue_failed(tmp_path: Path, remote: Path):
    fake = FakeGitHub()
    gh = LocalGitHub(fake, remote)
    number = _queue_issue(gh)
    # the supervisor does nothing, three times
    work = tmp_path / "work"
    work.mkdir()
    res = run_job("arcane/app", number, gh, FakeClient([turn(text("nothing to do"))] * 4), work, max_rounds=3)
    assert res.status == "failed" and not res.pr_url and fake.prs == []
    assert fake.issues[number]["labels"] == ["pullwright", "pullwright:failed"]
    assert "stopped without changes" in fake.comments[-1][1]


def test_parse_ref():
    assert parse_ref("arcane/app#12") == ("arcane/app", 12)
    with pytest.raises(ValueError):
        parse_ref("arcane/app")


class ScriptedHandle:
    def __init__(self, lines: list[str], code: int = 0) -> None:
        self._lines, self.code = lines, code

    def lines(self) -> Iterator[str]:
        yield from self._lines

    def wait(self) -> int:
        return self.code

    def cancel(self) -> None:
        pass


class ScriptedRunner:
    def __init__(self, crash: bool = False) -> None:
        self.started: list[Job] = []
        self.crash = crash

    def start(self, job: Job) -> ScriptedHandle:
        self.started.append(job)
        if self.crash:
            return ScriptedHandle(["Traceback (most recent call last):", "boom"], code=1)
        event = json.dumps({"type": "state", "payload": {"state": "working"}})
        result = json.dumps({"repo": job.repo, "issue": job.issue, "status": "green", "pr_url": "https://pr"})
        return ScriptedHandle([f"PWEVENT {event}", "plain log line", f"PWRESULT {result}"])


def _dispatcher(runner, **kw) -> tuple[Dispatcher, FakeGitHub, GitHub]:
    fake = FakeGitHub()
    gh = GitHub(PatAuth("tok"), http=httpx.Client(base_url=API, transport=httpx.MockTransport(fake.handler)))
    return Dispatcher(gh, runner, ["arcane/app"], **kw), fake, gh


def test_dispatcher_runs_one_issue_at_a_time_oldest_first():
    runner = ScriptedRunner()
    events, done = [], []
    d, fake, gh = _dispatcher(runner, max_parallel=1, on_event=lambda j, e: events.append((j.issue, e["type"])),
                              on_done=lambda j, o: done.append((j.issue, o.result["status"])))
    first, second = _queue_issue(gh, "first"), _queue_issue(gh, "second")
    job = d.tick()
    assert job.issue == first and job.github_token == "tok"
    assert fake.issues[first]["labels"] == ["pullwright", "pullwright:working"]  # claimed before the runner boots
    d.drain()
    assert done == [(first, "green")] and events == [(first, "state")]
    assert d.tick().issue == second
    d.drain()
    assert d.tick() is None and [j.issue for j in runner.started] == [first, second]


def test_dispatcher_respects_capacity():
    d, _, gh = _dispatcher(ScriptedRunner(), max_parallel=1)
    _queue_issue(gh, "a")
    _queue_issue(gh, "b")
    d.active["arcane/app#999"] = None  # one job already running
    assert d.tick() is None


def test_crashed_runner_leaves_the_issue_failed_not_working():
    d, fake, gh = _dispatcher(ScriptedRunner(crash=True))
    number = _queue_issue(gh)
    d.tick()
    d.drain()
    assert fake.issues[number]["labels"] == ["pullwright", "pullwright:failed"]
    assert "exited with code 1 before finishing" in fake.comments[-1][1]


def test_docker_command_keeps_secrets_off_the_command_line():
    job = Job("arcane/app", 7, "ghs_secret", {"ANTHROPIC_API_KEY": "sk-secret"})
    argv = DockerRunner(image="img:1").argv(job)
    joined = " ".join(argv)
    assert "ghs_secret" not in joined and "sk-secret" not in joined
    assert ["-e", "PULLWRIGHT_GITHUB_TOKEN"] == argv[argv.index("PULLWRIGHT_GITHUB_TOKEN") - 1: argv.index("PULLWRIGHT_GITHUB_TOKEN") + 1]
    assert "--user" in argv and "1000:1000" in argv and "--rm" in argv and "no-new-privileges" in argv
    assert argv[-4:] == ["timeout", "90m", "pullwright-work", "arcane/app#7"]
