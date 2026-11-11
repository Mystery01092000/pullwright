"""Load MCP stdio servers from mcp.json and register their tools as mcp__<server>__<tool>.

The agent loop is sync; MCP is async. One background thread owns an asyncio loop and all sessions.
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .tools import TOOLS, ToolContext, ToolError


class MCPManager:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()
        self._stop = asyncio.Event()  # bound to self.loop below
        self._stop._loop = self.loop  # type: ignore[attr-defined]
        self.sessions: dict[str, ClientSession] = {}
        self.tool_names: list[str] = []
        self._tasks: list[asyncio.Task[None]] = []

    def _run(self, coro: Any, timeout: float = 60) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _serve(self, name: str, params: StdioServerParameters, ready: asyncio.Event) -> None:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                self.sessions[name] = s
                for t in (await s.list_tools()).tools:
                    full = f"mcp__{name}__{t.name}"
                    schema = {"description": t.description or "", "input_schema": t.input_schema}
                    TOOLS[full] = (schema, self._make_caller(name, t.name))
                    self.tool_names.append(full)
                ready.set()
                await self._stop.wait()

    def _make_caller(self, server: str, tool: str) -> Any:
        def call(ctx: ToolContext, **inp: Any) -> str:
            async def go() -> str:
                res = await self.sessions[server].call_tool(tool, inp)
                text = "\n".join(getattr(c, "text", "") for c in (res.content or []) if getattr(c, "type", "") == "text")
                if getattr(res, "structured_content", None) and not text:
                    text = json.dumps(res.structured_content)
                if getattr(res, "is_error", False):
                    raise ToolError(text or "mcp tool error")
                return text

            return self._run(go(), timeout=ctx.bash_timeout)

        return call

    def add_server(self, name: str, command: str, args: list[str] | None = None, env: dict[str, str] | None = None, cwd: str | None = None) -> None:
        params = StdioServerParameters(command=command, args=args or [], env=env, cwd=cwd)

        async def start() -> None:
            ready = asyncio.Event()
            self._tasks.append(asyncio.create_task(self._serve(name, params, ready)))
            await asyncio.wait_for(ready.wait(), 30)

        self._run(start())

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self._stop.set)
        for t in self._tasks:
            try:
                asyncio.run_coroutine_threadsafe(asyncio.wait_for(t, 10), self.loop).result(15)
            except Exception:
                pass
        for n in self.tool_names:
            TOOLS.pop(n, None)
        self.loop.call_soon_threadsafe(self.loop.stop)


def load_mcp(config: Path) -> MCPManager:
    """mcp.json shape (same as Claude Desktop): {"mcpServers": {"name": {"command", "args", "env", "cwd"}}}"""
    mgr = MCPManager()
    if config.is_file():
        for name, spec in json.loads(config.read_text()).get("mcpServers", {}).items():
            mgr.add_server(name, spec["command"], spec.get("args"), spec.get("env"), spec.get("cwd"))
    return mgr
