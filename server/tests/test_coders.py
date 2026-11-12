import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pullwright import coders
from pullwright.coders import ClaudeStream, ExternalCoder, GooseStream, coder_for
from pullwright.costs import stage_of
from pullwright.runners.base import Job
from pullwright.runners.docker import DockerRunner
from pullwright.session import Session
from tests.fake_client import FakeClient

CLAUDE_LINES = [
    {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5", "tools": ["Bash", "Edit"]},
    {"type": "assistant", "message": {"id": "m1", "model": "claude-sonnet-5-5", "content": [{"type": "text", "text": "Writing the test first."}],
                                      "usage": {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 0}}},
    {"type": "assistant", "message": {"id": "m1", "model": "claude-sonnet-5-5",
                                      "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "go test ./..."}}],
                                      "usage": {"input_tokens": 1000, "output_tokens": 100}}},  # same message: usage counted once
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok  fincalc/internal/calc", "is_error": False}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "Added SIP with tests; go test passes.", "total_cost_usd": 0.01},
]
GOOSE_LINES = [
    {"type": "message", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Running the tests."},
        {"type": "toolRequest", "id": "r1", "toolCall": {"status": "success", "value": {"name": "developer__shell", "arguments": {"command": "npm test"}}}}]}},
    {"type": "message", "message": {"role": "user", "content": [
        {"type": "toolResponse", "id": "r1", "toolResult": {"status": "success", "value": {"content": [{"type": "text", "text": "Tests 12 passed"}]}}}]}},
    {"type": "complete", "total_tokens": 3000, "input_tokens": 2500, "output_tokens": 500},
]


def _events(parser, lines):
    return [ev for line in lines for ev in parser.feed(line)]


def test_coder_choice_per_repository():
    env = {"PULLWRIGHT_CODER": "claude-code", "PULLWRIGHT_CODER_BY_REPO": json.dumps({"acme/api": "goose"})}
    assert coder_for("acme/api", env) == "goose" and coder_for("acme/web", env) == "claude-code"
    assert coder_for("acme/web", {}) == "goose"  # the default
    with pytest.raises(ValueError):
        coder_for("acme/web", {"PULLWRIGHT_CODER": "cursor"})
    assert stage_of("claude-code") == stage_of("goose") == "work"


def test_claude_stream_becomes_pullwright_events():
    p = ClaudeStream("claude-sonnet-5-5")
    evs = _events(p, CLAUDE_LINES)
    kinds = [t for t, _ in evs]
    assert kinds == ["text", "usage", "tool_call", "tool_result"]
    call = evs[2][1]
    assert call["name"] == "Bash" and call["hint"] == "go test ./..." and evs[3][1]["output"].startswith("ok")
    usage = evs[1][1]
    assert usage["input_tokens"] == 1000 and usage["cost_usd"] > 0 and usage["priced"]
    assert p.final.startswith("Added SIP") and p.stop == "end_turn"


def test_goose_stream_reports_usage_at_the_end():
    p = GooseStream("claude-sonnet-5-5")
    evs = _events(p, GOOSE_LINES)
    assert [t for t, _ in evs] == ["text", "tool_call", "tool_result", "usage"]
    assert evs[1][1]["name"] == "developer__shell" and evs[2][1]["output"] == "Tests 12 passed"
    assert evs[3][1]["output_tokens"] == 500 and evs[3][1]["cost_usd"] > 0
    assert p.final == "Running the tests."


FAKE_AGENT = r'''
import json, os, sys
lines = json.loads(%r)
for l in lines:
    print(json.dumps(l), flush=True)
# what the agent's environment looks like, for the test to inspect
open(os.path.join(os.getcwd(), "agent-env.json"), "w").write(json.dumps({
    "home": os.environ["HOME"], "github": os.environ.get("GITHUB_TOKEN"), "pw_token": os.environ.get("PULLWRIGHT_GITHUB_TOKEN"),
    "base_url": os.environ.get("ANTHROPIC_BASE_URL"), "key": os.environ.get("ANTHROPIC_API_KEY"),
    "skills": sorted(os.listdir(os.path.join(os.environ["HOME"], ".claude", "skills"))),
    "npm_cache": os.environ.get("npm_config_cache"), "stdin": sys.stdin.read()[:200]}))
'''


def test_external_coder_runs_streams_events_and_isolates_secrets(tmp_path: Path):
    agent = tmp_path / "fake_claude.py"
    agent.write_text(FAKE_AGENT % json.dumps(CLAUDE_LINES), encoding="utf-8")
    skill = tmp_path / "skills" / "tdd"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\ndescription: test first\n---\nWrite the test first.")
    work = tmp_path / "repo"
    work.mkdir()
    session = Session(cwd=work, client=FakeClient([]), run_id="app-1", max_cost_usd=5.0)
    seen = []
    session.bus.subscribe(seen.append)
    env = {**os.environ, "HOME": str(tmp_path / "home"), "GITHUB_TOKEN": "ghs_secret", "PULLWRIGHT_GITHUB_TOKEN": "ghs_secret2",
           "PULLWRIGHT_LLM_PROXY": "http://host.docker.internal:8765/llm", "PULLWRIGHT_LLM_TOKEN": "pwj_job"}
    coder = ExternalCoder("claude-code", env=env, exe=[sys.executable, str(agent)])
    report = coder.run(session, "Implement the plan.", "claude-code-app-1-1", skills={"tdd": skill}, instructions="Org rules: TDD.")
    assert report.startswith("Added SIP")
    types = [e.type for e in seen if e.agent_id == "claude-code-app-1-1"]
    assert types[0] == "agent_start" and types[-1] == "agent_end" and "tool_call" in types
    assert session.cost_usd > 0  # usage events reach the session's meter and budget
    got = json.loads((work / "agent-env.json").read_text())
    assert got["github"] is None and got["pw_token"] is None  # no GitHub credentials for the agent
    assert got["base_url"] == "http://host.docker.internal:8765/llm/anthropic" and got["key"] == "pwj_job"  # through the proxy
    assert got["skills"] == ["tdd"] and got["home"] != str(tmp_path / "home")  # its own HOME, with the skills
    assert got["npm_cache"] == f"{tmp_path / 'home'}/.cache/npm"  # but the shared caches
    assert got["stdin"].startswith("Org rules: TDD.") and "Implement the plan." in got["stdin"]


def test_external_coder_missing_binary_is_a_clear_error(tmp_path: Path):
    session = Session(cwd=tmp_path, client=FakeClient([]), run_id="app-1")
    coder = ExternalCoder("goose", env={**os.environ, "HOME": str(tmp_path)}, exe=["definitely-not-installed-goose"])
    with pytest.raises(RuntimeError, match="not installed"):
        coder.run(session, "x", "goose-app-1-1")


@pytest.mark.skipif(shutil.which("sh") is None or os.name == "nt", reason="guard commands are POSIX shell scripts")
def test_guards_refuse_push_gh_and_kill_by_name(tmp_path: Path):
    coder = ExternalCoder("goose", env={**os.environ, "HOME": str(tmp_path)})
    home = coder._home(tmp_path, {})
    env = coder.process_env(home)
    run = lambda cmd: subprocess.run(["sh", "-c", cmd], env=env, capture_output=True, text=True, cwd=tmp_path)  # noqa: E731
    for cmd in ("pkill -f app", "killall node", "gh pr create", "git push origin main", "git -c x=y push"):
        r = run(cmd)
        assert r.returncode == 1 and "not" in r.stderr, cmd
    assert run("git --version").returncode == 0  # other git commands reach the real git


def test_docker_runner_mounts_the_shared_cache():
    job = Job(repo="acme/app", issue=1, github_token="t")
    argv = DockerRunner(image="img", cache_volume="pullwright-cache").argv(job)
    assert "-v" in argv and argv[argv.index("-v") + 1] == "pullwright-cache:/home/runner/.cache"
    assert "-v" not in DockerRunner(image="img", cache_volume="").argv(job)


def test_work_run_hands_agent_steps_to_the_external_coder(tmp_path: Path, monkeypatch):
    from pullwright.checks import CheckPlan
    from pullwright.pipeline.work import WorkRun, WorkSpec

    calls = []

    def fake_run(self, session, prompt, agent_id, skills=None, instructions=""):
        calls.append((self.kind, agent_id, prompt, instructions))
        (session.cwd / "done.txt").write_text("ok")
        return "done"

    monkeypatch.setattr(coders.ExternalCoder, "run", fake_run)
    session = Session(cwd=tmp_path, client=FakeClient([]), run_id="app-1")
    spec = WorkSpec(repo="acme/app", issue_number=1, title="Add done.txt", task="add done.txt", spec={"plan": ["write done.txt"]})
    plan = CheckPlan(checks={"test": "test -f done.txt" if os.name != "nt" else "python -c \"import os,sys; sys.exit(0 if os.path.exists('done.txt') else 1)\""})
    run = WorkRun(session, spec, plan=plan, coder="goose")
    run.diagnose = lambda results: {"complete": True, "gaps": [], "evidence": "read done.txt"}  # the reviewer is Pullwright's own
    result = run.run()
    assert result.status == "green" and run.max_rounds == 2
    assert calls[0][0] == "goose" and calls[0][1] == "goose-app-1-1" and "Never use pkill" in calls[0][3]


def test_goose_cost_comes_from_the_proxy_when_it_can():
    spends = iter([1.25, 1.50])  # before the step, after it
    p = GooseStream("claude-sonnet-5-5", spent=lambda: next(spends))
    usage = [pl for t, pl in _events(p, GOOSE_LINES) if t == "usage"][0]
    assert usage["cost_usd"] == 0.25 and usage["cost_source"] == "proxy" and usage["output_tokens"] == 500
