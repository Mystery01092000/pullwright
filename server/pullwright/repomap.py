"""A compact map of a repository: the file tree and the top-level symbols of each source file.

Built once per commit and given to the describer, the verifier and the supervisor, so no agent spends
turns (and tokens) rediscovering the layout. Deterministic, no model calls.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

SYMBOLS: dict[str, re.Pattern[str]] = {
    ".py": re.compile(r"^(?:async\s+)?(?:def|class)\s+(\w+)"),
    ".ts": re.compile(r"^export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|interface|type|enum)\s+(\w+)"),
    ".tsx": re.compile(r"^export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|interface|type|enum)\s+(\w+)"),
    ".js": re.compile(r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function|class)\s+(\w+)"),
    ".go": re.compile(r"^(?:func(?:\s+\([^)]*\))?|type)\s+(\w+)"),
    ".rs": re.compile(r"^pub\s+(?:async\s+)?(?:fn|struct|enum|trait)\s+(\w+)"),
    ".java": re.compile(r"^\s*public\s+(?:final\s+|abstract\s+)?(?:class|interface|enum|record)\s+(\w+)"),
    ".rb": re.compile(r"^\s*(?:def|class|module)\s+([\w.:]+)"),
}
SKIP = re.compile(r"(^|/)(node_modules|dist|build|vendor|\.venv|__pycache__|\.git)/|\.(lock|min\.js|map|png|jpg|jpeg|gif|svg|ico|pdf|zip)$")
_cache: dict[tuple[str, str], str] = {}


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout if r.returncode == 0 else ""


def files(root: Path) -> list[str]:
    listed = _git(root, "ls-files").splitlines()
    if not listed:  # not a git repo: walk the tree
        listed = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    return sorted(f for f in listed if not SKIP.search(f))


def symbols(path: Path, limit: int = 12) -> list[str]:
    pat = SYMBOLS.get(path.suffix)
    if pat is None:
        return []
    found: list[str] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = pat.match(line)
                if m and not m.group(1).startswith("_"):
                    found.append(m.group(1))
                    if len(found) >= limit:
                        break
    except OSError:
        return []
    return found


def build(root: Path, max_chars: int = 24_000) -> str:
    """The map as text; cached per (repo, HEAD) so the planner and the runner reuse one build."""
    head = _git(root, "rev-parse", "HEAD").strip()
    key = (str(root.resolve()), head)
    if head and key in _cache:
        return _cache[key]
    lines = [f"Repository map ({len(fs := files(root))} files{', HEAD ' + head[:10] if head else ''}). Top-level symbols in parentheses."]
    for f in fs:
        syms = symbols(root / f)
        lines.append(f"{f}" + (f"  ({', '.join(syms)})" if syms else ""))
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0] + f"\n... ({len(fs)} files total; map truncated, use glob/grep for the rest)"
    if head:
        _cache[key] = text
    return text
