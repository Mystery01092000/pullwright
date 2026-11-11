"""Thin git/gh wrappers. All functions take the repo path; non-repos return empty strings."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


def _git(cwd: Path, *args: str, check: bool = False) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def is_repo(cwd: Path) -> bool:
    return _git(cwd, "rev-parse", "--is-inside-work-tree") == "true"


def slugify(text: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n] or "task"


def create_branch(cwd: Path, slug: str) -> str:
    name = f"pullwright/{slug}"
    existing = _git(cwd, "branch", "--list", f"{name}*").split()
    if name in existing:
        name = f"{name}-{len(existing) + 1}"
    _git(cwd, "checkout", "-b", name, check=True)
    return name


def checkout(cwd: Path, name: str) -> str:
    """Switch to an existing branch (creating it if it vanished). Returns the branch name."""
    if name in _git(cwd, "branch", "--list", name).split():
        _git(cwd, "checkout", "-q", name, check=True)
    else:
        _git(cwd, "checkout", "-q", "-b", name, check=True)
    return name


def current_branch(cwd: Path) -> str:
    return _git(cwd, "branch", "--show-current")


JUNK = ["__pycache__", "*.pyc", ".pytest_cache", "node_modules", ".venv", "*.egg-info"]


def _stage(cwd: Path, *flags: str) -> None:
    """Stage everything except build junk, even when the repo has no .gitignore."""
    _git(cwd, "add", *flags, ".")
    _git(cwd, "reset", "-q", "--", *JUNK)


def diff(cwd: Path, max_chars: int = 60_000) -> str:
    _stage(cwd, "-N")  # intent-to-add so untracked files show in diff
    d = _git(cwd, "diff")
    return d if len(d) <= max_chars else d[:max_chars] + "\n...(diff truncated)"


def diff_stat(cwd: Path) -> str:
    _stage(cwd, "-N")
    return _git(cwd, "diff", "--stat")


def commit_all(cwd: Path, message: str) -> str:
    _stage(cwd, "-A")
    if not _git(cwd, "status", "--porcelain"):
        return ""
    _git(cwd, "commit", "-q", "-m", message, check=True)
    return _git(cwd, "rev-parse", "--short", "HEAD")


def create_pr(cwd: Path, title: str, body: str) -> str:
    branch = current_branch(cwd)
    _git(cwd, "push", "-u", "origin", branch, check=True)
    r = subprocess.run(["gh", "pr", "create", "--title", title, "--body", body, "--head", branch], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh pr create: {r.stderr.strip()}")
    return r.stdout.strip()
