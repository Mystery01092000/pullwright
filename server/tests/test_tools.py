from pathlib import Path

import pytest

from pullwright.tools import ALL_TOOLS, ToolContext, execute


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("hello world\n")
    return ToolContext(root=tmp_path)


def run(ctx, name, **inp):
    return execute(ctx, name, inp, ALL_TOOLS)


def test_read_write_edit(ctx):
    out, err = run(ctx, "read_file", path="a.py")
    assert not err and out.startswith("1\tdef f():")
    out, err = run(ctx, "edit_file", path="a.py", old="return 1", new="return 2")
    assert not err
    assert "return 2" in (ctx.root / "a.py").read_text()
    out, err = run(ctx, "edit_file", path="a.py", old="nope", new="x")
    assert err and "0 times" in out
    out, err = run(ctx, "write_file", path="new/dir/c.txt", content="x")
    assert not err and (ctx.root / "new/dir/c.txt").read_text() == "x"


def test_glob_grep(ctx):
    out, _ = run(ctx, "glob", pattern="**/*.txt")
    assert out == "sub/b.txt"
    out, _ = run(ctx, "grep", pattern="hello", path=".")
    assert out == "sub/b.txt:1:hello world"


def test_sandbox_escape(ctx):
    for p in ["../x", "../../etc/passwd", "C:/Windows/x"]:
        out, err = run(ctx, p and "read_file", path=p)
        assert err, p


def test_allowlist_and_schema(ctx):
    out, err = execute(ctx, "bash", {"command": "echo hi"}, ["read_file"])
    assert err and "not allowed" in out
    out, err = run(ctx, "read_file", path=1)
    assert err and "invalid input" in out
    out, err = execute(ctx, "read_file", {"path": "a.py", "extra": "x"}, ALL_TOOLS)
    assert err


def test_bash_runs_and_guards(ctx):
    out, err = run(ctx, "bash", command="echo hi")
    assert not err and out.startswith("exit 0") and "hi" in out
    out, err = run(ctx, "bash", command="rm -rf /")
    assert err and "denylisted" in out
    out, err = run(ctx, "bash", command="rm a.py")
    assert err and "approval" in out
    ctx.approve = lambda cmd: True
    out, err = run(ctx, "bash", command="rm a.py")
    assert not err and not (ctx.root / "a.py").exists()
