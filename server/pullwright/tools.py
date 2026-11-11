"""Built-in tools. Every tool: (schema, fn(ctx, **input) -> str). Errors are returned as text, caller marks is_error."""
from __future__ import annotations

import glob as globmod
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}


class ToolError(Exception):
    pass


@dataclass
class ToolContext:
    root: Path
    approve: Callable[[str], bool] | None = None  # asked before risky bash; None = deny
    bash_timeout: int = 120
    deny: list[str] | None = None  # regexes; None = module DENY
    ask: list[str] | None = None
    allow: list[str] | None = None  # auto-approved even if they match ask
    pre_tool: Callable[[str, dict[str, Any]], str | None] | None = None  # returns a block reason, or None
    post_tool: Callable[[str, dict[str, Any], str, bool], None] | None = None

    def resolve(self, rel: str) -> Path:
        root = self.root.resolve()
        p = (root / rel).resolve()
        if not p.is_relative_to(root):
            raise ToolError(f"path escapes sandbox: {rel}")
        return p


def _schema(desc: str, props: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": desc,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": list(props),
            "additionalProperties": False,
        },
    }


S = {"type": "string"}


def read_file(ctx: ToolContext, path: str) -> str:
    p = ctx.resolve(path)
    if not p.is_file():
        raise ToolError(f"not a file: {path}")
    text = p.read_text(encoding="utf-8", errors="replace")
    return "\n".join(f"{i + 1}\t{line}" for i, line in enumerate(text.splitlines()))


def write_file(ctx: ToolContext, path: str, content: str) -> str:
    p = ctx.resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {path}"


def edit_file(ctx: ToolContext, path: str, old: str, new: str) -> str:
    p = ctx.resolve(path)
    text = p.read_text(encoding="utf-8")
    n = text.count(old)
    if n != 1:
        raise ToolError(f"old string occurs {n} times in {path}; must be exactly 1")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return f"edited {path}"


def _skipped(p: Path) -> bool:
    return bool(SKIP_DIRS & set(p.parts))


def glob_files(ctx: ToolContext, pattern: str) -> str:
    root = ctx.root.resolve()
    hits = [Path(h).relative_to(root) for h in globmod.glob(str(root / pattern), recursive=True)]
    hits = sorted(h.as_posix() for h in hits if not _skipped(h))
    return "\n".join(hits[:500]) or "(no matches)"


def grep(ctx: ToolContext, pattern: str, path: str) -> str:
    rx = re.compile(pattern)
    root = ctx.root.resolve()
    base = ctx.resolve(path)
    files = [base] if base.is_file() else [f for f in base.rglob("*") if f.is_file()]
    out: list[str] = []
    for f in files:
        if _skipped(f.relative_to(root)):
            continue
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines()):
                if rx.search(line):
                    out.append(f"{f.relative_to(root).as_posix()}:{i + 1}:{line}")
                    if len(out) >= 500:
                        return "\n".join(out)
        except OSError:
            continue
    return "\n".join(out) or "(no matches)"


# Commands that never run, and commands that need a human yes.
DENY = [r"\brm\s+-rf\s+[/~]", r"\bgit\s+push\b.*--force", r"\bgit\s+reset\s+--hard", r"\bmkfs\b", r"\bformat\s+[a-z]:"]
ASK = [r"\brm\b", r"\bgit\s+push\b", r"\bsudo\b", r"\bcurl\b.*\|\s*(ba)?sh", r"\bnpm\s+publish\b", r"\bpip\s+install\b", r"\buv\s+add\b"]


def bash(ctx: ToolContext, command: str) -> str:
    if any(re.search(p, command) for p in (DENY if ctx.deny is None else ctx.deny)):
        raise ToolError("command is denylisted")
    allowed = any(re.search(p, command) for p in (ctx.allow or []))
    if not allowed and any(re.search(p, command) for p in (ASK if ctx.ask is None else ctx.ask)):
        if ctx.approve is None or not ctx.approve(command):
            raise ToolError("command requires approval and was not approved")
    sh = shutil.which("bash")
    argv = [sh, "-lc", command] if sh else ["powershell", "-NoProfile", "-Command", command]
    try:
        r = subprocess.run(argv, cwd=ctx.root, capture_output=True, text=True, timeout=ctx.bash_timeout)
    except subprocess.TimeoutExpired:
        raise ToolError(f"timed out after {ctx.bash_timeout}s")
    out = (r.stdout + ("\n" + r.stderr if r.stderr else "")).strip()
    if len(out) > 30_000:
        out = out[:30_000] + "\n...(truncated)"
    return f"exit {r.returncode}\n{out}"


TOOLS: dict[str, tuple[dict[str, Any], Callable[..., str]]] = {
    "read_file": (_schema("Read a file; returns numbered lines.", {"path": S}), read_file),
    "write_file": (_schema("Create or overwrite a file.", {"path": S, "content": S}), write_file),
    "edit_file": (_schema("Replace exactly one occurrence of old with new in a file.", {"path": S, "old": S, "new": S}), edit_file),
    "glob": (_schema("List files matching a glob (supports **).", {"pattern": S}), glob_files),
    "grep": (_schema("Regex search in a file or directory.", {"pattern": S, "path": S}), grep),
    "bash": (_schema("Run a shell command in the project root.", {"command": S}), bash),
}
ALL_TOOLS = list(TOOLS)


Registry = dict[str, tuple[dict[str, Any], Callable[..., str]]]


def schemas(names: list[str], registry: Registry | None = None) -> list[dict[str, Any]]:
    reg = registry if registry is not None else TOOLS
    return [{"name": n, **reg[n][0]} for n in names]


def execute(ctx: ToolContext, name: str, inp: Any, allowed: list[str], registry: Registry | None = None) -> tuple[str, bool]:
    """Returns (text, is_error)."""
    reg = registry if registry is not None else TOOLS
    if name not in allowed or name not in reg:
        return f"tool not allowed: {name}", True
    schema, fn = reg[name]
    props = schema["input_schema"].get("properties", {})
    required = schema["input_schema"].get("required", [])
    if not isinstance(inp, dict) or not set(required) <= set(inp) or not set(inp) <= set(props):
        return f"invalid input for {name}: {inp!r}", True
    if schema.get("strict") and not all(isinstance(inp[k], str) for k in inp):
        return f"invalid input for {name}: {inp!r}", True
    if ctx.pre_tool is not None:
        reason = ctx.pre_tool(name, inp)
        if reason:
            return f"blocked by hook: {reason}", True
    try:
        out, err = fn(ctx, **inp), False
    except ToolError as e:
        out, err = str(e), True
    except Exception as e:  # a tool bug must not kill the loop
        out, err = f"{type(e).__name__}: {e}", True
    if ctx.post_tool is not None:
        ctx.post_tool(name, inp, out, err)
    return out, err
