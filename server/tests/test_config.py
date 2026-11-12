import json
import subprocess
import sys
from pathlib import Path

from pullwright.config import load_config, parse_frontmatter
from pullwright.lifecycle import Run
from pullwright.tools import ToolContext, execute
from tests.fake_client import FakeClient, text, tool_use, turn
from tests.test_lifecycle import DIAG_OK, INTAKE, ScriptedIO


def test_frontmatter():
    meta, body = parse_frontmatter("---\nmodel: x\ntools: a, b\n---\nhello\nworld")
    assert meta == {"model": "x", "tools": "a, b"} and body == "hello\nworld"
    assert parse_frontmatter("no front matter") == ({}, "no front matter")


def test_defaults_without_harness_dir(tmp_path: Path):
    cfg = load_config(tmp_path)
    assert cfg.root is None and set(cfg.agents) == {"intake", "supervisor", "worker", "diagnosis", "summarizer", "describer", "verifier", "responder"}
    # planning agents never write or run anything: they see the repo read-only
    assert cfg.agent("describer").tools == ["read_file", "glob", "grep"] and cfg.agent("verifier").tools == ["read_file", "glob", "grep"]
    assert cfg.agent("intake").tools == ["read_file", "glob", "grep"] and cfg.agent("intake").max_turns == 15
    assert cfg.agent("summarizer").tools == [] and cfg.agent("worker").tools is None
    assert cfg.system_prompt("worker") == cfg.agent("worker").system


def _harness(repo: Path) -> Path:
    h = repo / ".pullwright"
    (h / "agents").mkdir(parents=True)
    (h / "skills" / "deploy").mkdir(parents=True)
    (h / "agents" / "worker.md").write_text("---\nmodel: claude-haiku-5-5\neffort: low\nmax_turns: 7\n---\nCustom worker prompt.")
    (h / "instructions.md").write_text("Always use tabs.")
    (h / "guardrails.json").write_text(json.dumps({"deny": ["\\bcurl\\b"], "ask": [], "allow": ["^echo "], "bash_timeout": 5}))
    block = f'{sys.executable} -c "import sys,json; p=json.load(sys.stdin); sys.exit(2 if p[\'input\'].get(\'path\',\'\').endswith(\'.env\') else 0)"'
    (h / "hooks.json").write_text(json.dumps([{"event": "pre_tool", "match": "write_file|edit_file", "command": block}, {"event": "state", "match": "done", "command": "echo hi"}]))
    (h / "skills" / "deploy" / "SKILL.md").write_text("---\ndescription: How we deploy\n---\nRun make deploy.")
    (h / "skills" / "deploy" / "checklist.md").write_text("- x")
    return h


def test_repo_overrides_and_skills(tmp_path: Path):
    _harness(tmp_path)
    cfg = load_config(tmp_path)
    w = cfg.agent("worker")
    assert (w.model, w.effort, w.max_turns, w.source) == ("claude-haiku-5-5", "low", 7, "repo")
    assert cfg.agent("intake").source == "default"
    sp = cfg.system_prompt("worker")
    assert sp.startswith("Custom worker prompt.") and "Always use tabs." in sp and "deploy: How we deploy" in sp
    assert "Run make deploy." in cfg.skills["deploy"].body() and "checklist.md" in cfg.skills["deploy"].body()


def test_hooks_block_and_pass(tmp_path: Path):
    _harness(tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.run_hooks("pre_tool", "write_file", {"input": {"path": "x/.env"}}, tmp_path)
    assert cfg.run_hooks("pre_tool", "write_file", {"input": {"path": "x/app.py"}}, tmp_path) is None
    assert cfg.run_hooks("pre_tool", "bash", {"input": {"command": "rm .env"}}, tmp_path) is None  # no match
    assert cfg.run_hooks("state", "done", {}, tmp_path) is None


def test_guardrails_in_tool_context(tmp_path: Path):
    ctx = ToolContext(root=tmp_path, deny=["\\bcurl\\b"], ask=["\\becho\\b"], allow=["^echo ok"])
    out, err = execute(ctx, "bash", {"command": "curl x"}, ["bash"])
    assert err and out.startswith("command is denylisted")
    assert execute(ctx, "bash", {"command": "echo nope"}, ["bash"])[1] is True  # ask, no approver
    out, err = execute(ctx, "bash", {"command": "echo ok"}, ["bash"])
    assert not err and "ok" in out  # allow beats ask


def test_lifecycle_uses_config_hooks_and_skill_tool(tmp_path: Path):
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@t"], ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=tmp_path, check=True)
    _harness(tmp_path)
    turns = [
        turn(text(INTAKE)),
        turn(tool_use("spawn_worker", {"task": "t", "context": "c"})),
        turn(tool_use("load_skill", {"name": "deploy"}, "tu_s")),  # worker reads the skill
        turn(tool_use("write_file", {"path": ".env", "content": "x"}, "tu_e")),  # blocked by hook
        turn(tool_use("write_file", {"path": "ok.txt", "content": "x"}, "tu_w")),
        turn(text("done")),
        turn(text("sup done")),
        turn(text(DIAG_OK)),
        turn(text("sum\nTITLE: t")),
    ]
    events = []
    client = FakeClient(turns)
    run = Run(task="t", cwd=tmp_path, io=ScriptedIO(), client=client)
    run.bus.subscribe(events.append)
    run.run()
    assert run.state == "done"
    results = [e.payload for e in events if e.type == "tool_result"]
    assert any(r["name"] == "load_skill" and "Run make deploy." in r["output"] for r in results)
    blocked = [r for r in results if r["is_error"]]
    assert len(blocked) == 1 and "blocked by hook" in blocked[0]["output"]
    assert not (tmp_path / ".env").exists() and (tmp_path / "ok.txt").exists()
    worker_call = client.calls[2]
    assert worker_call["model"] == "claude-haiku-5-5" and worker_call["output_config"]["effort"] == "low"
    assert "Always use tabs." in worker_call["system"][0]["text"]
    assert any(t["name"] == "load_skill" for t in worker_call["tools"])


def test_env_file_parsing_strips_inline_comments_but_not_quoted_hashes():
    from pullwright import parse_env

    env = parse_env("# header\nA=1   # one\nB='x # not a comment'\nC=\"quoted\"\nD=url#frag\n\nE=\n")
    assert env == {"A": "1", "B": "x # not a comment", "C": "quoted", "D": "url#frag", "E": ""}
