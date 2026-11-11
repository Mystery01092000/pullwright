import json
import sys
from pathlib import Path

import pytest

from pullwright.mcp_tools import load_mcp
from pullwright.tools import TOOLS, ToolContext, execute, schemas

HERE = Path(__file__).parent


@pytest.fixture
def mgr(tmp_path: Path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"echo": {"command": sys.executable, "args": [str(HERE / "mcp_echo_server.py")]}}}))
    m = load_mcp(cfg)
    yield m
    m.close()
    assert "mcp__echo__echo" not in TOOLS


def test_mcp_round_trip(mgr, tmp_path: Path):
    assert set(mgr.tool_names) == {"mcp__echo__echo", "mcp__echo__fail"}
    sch = schemas(["mcp__echo__echo"])[0]
    assert sch["name"] == "mcp__echo__echo" and "text" in sch["input_schema"]["properties"]
    ctx = ToolContext(root=tmp_path)
    out, err = execute(ctx, "mcp__echo__echo", {"text": "ab", "times": 2}, mgr.tool_names)
    assert (out, err) == ("abab", False)
    out, err = execute(ctx, "mcp__echo__fail", {"msg": "boom"}, mgr.tool_names)
    assert err and "fail" in out  # mcp 2.x masks the server exception text
    out, err = execute(ctx, "mcp__echo__echo", {"nope": 1}, mgr.tool_names)
    assert err and "invalid input" in out
