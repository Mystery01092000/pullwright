"""The organisation's shared agents, skills, hooks, instructions and guardrails, from one repository.

    PULLWRIGHT_ORG_CONFIG=acme/agent-config            # default branch
    PULLWRIGHT_ORG_CONFIG=acme/agent-config@v3         # a tag, branch or commit
    PULLWRIGHT_ORG_CONFIG=acme/agent-config@main:pullwright   # a folder inside it

The control plane fetches it (read-only, through the GitHub App, so the App must be installed on that repository)
on start and every PULLWRIGHT_ORG_CONFIG_REFRESH seconds (default 300), and records the commit it resolved. Each
runner job gets that exact commit (PULLWRIGHT_ORG_CONFIG_SHA) and a read-only token for that one repository, so a
plan and its implementation use the same version even if the repository changes in between.

The folder is read by config.read_layer: `.pullwright/` layout or Claude Code's `.claude/` layout. Layering and
merge rules: config.py. For local development, PULLWRIGHT_ORG_CONFIG_DIR points at a folder directly.

The repository can live on any account (an organisation or a personal one). When the GitHub App is not installed
there, set PULLWRIGHT_ORG_CONFIG_READ_TOKEN to a fine-grained token limited to that one repository with
Contents: read-only; it is then used instead of an App token, by the control plane and by runners.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("pullwright.orgconfig")
_SPEC = re.compile(r"^(?P<repo>[\w.-]+/[\w.-]+)(?:@(?P<ref>[^:]+))?(?::(?P<sub>.+))?$")
_state: dict[str, Any] = {"dir": None, "sha": "", "spec": ""}
_lock = threading.Lock()


@dataclass
class OrgSpec:
    repo: str
    ref: str = ""
    subdir: str = ""

    @classmethod
    def parse(cls, value: str) -> OrgSpec:
        m = _SPEC.match(value.strip())
        if not m:
            raise ValueError(f"PULLWRIGHT_ORG_CONFIG must be owner/repo[@ref][:folder], got {value!r}")
        sub = (m.group("sub") or "").strip("/")
        if ".." in Path(sub).parts:
            raise ValueError("the org config folder may not leave the repository")
        return cls(m.group("repo"), m.group("ref") or "", sub)


def current_dir() -> Path | None:
    """The organisation layer for this process: PULLWRIGHT_ORG_CONFIG_DIR, or what sync() last fetched."""
    env = os.environ.get("PULLWRIGHT_ORG_CONFIG_DIR")
    if env:
        return Path(env)
    return _state["dir"]


def current_sha() -> str:
    return _state["sha"]


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {re.sub(r'https://[^@/]+@', 'https://', r.stderr.strip())}")
    return r.stdout.strip()


def fetch(url: str, spec: OrgSpec, dest: Path, sha: str = "") -> tuple[Path, str]:
    """Fetch `spec` (or the exact commit `sha`) into `dest` and check it out. Returns (config folder, commit). The token in
    `url` is used once and never written to the clone's config."""
    dest.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").is_dir():
        _git(dest, "init", "-q")
    _git(dest, "fetch", "--quiet", "--depth", "1", url, sha or spec.ref or "HEAD")
    _git(dest, "checkout", "--quiet", "--force", "FETCH_HEAD")
    commit = _git(dest, "rev-parse", "HEAD")
    folder = (dest / spec.subdir).resolve() if spec.subdir else dest
    if dest.resolve() not in (folder, *folder.parents) or not folder.is_dir():
        raise RuntimeError(f"org config folder {spec.subdir!r} not found in {spec.repo}")
    return folder, commit


def _read_token(gh: Any, repo: str) -> str:
    """A token that can read the config repository: the dedicated read token if set, else a read-only App token."""
    return os.environ.get("PULLWRIGHT_ORG_CONFIG_READ_TOKEN") or gh.auth.token(repo, read_only=True)


def sync(gh: Any, value: str, cache: Path) -> tuple[Path, str]:
    """Fetch the organisation repository (read-only token) and make it this process's layer."""
    spec = OrgSpec.parse(value)
    url = f"https://x-access-token:{_read_token(gh, spec.repo)}@github.com/{spec.repo}.git"
    with _lock:
        folder, commit = fetch(url, spec, cache / spec.repo.replace("/", "__"))
        if commit != _state["sha"]:
            log.info("organisation config %s at %s", spec.repo, commit[:12])
        _state.update(dir=folder, sha=commit, spec=value)
    return folder, commit


def runner_env(gh: Any) -> dict[str, str]:
    """What a runner needs to load the same organisation layer: the spec, the pinned commit, a read-only token."""
    if not _state["spec"]:
        return {}
    spec = OrgSpec.parse(_state["spec"])
    return {"PULLWRIGHT_ORG_CONFIG": _state["spec"], "PULLWRIGHT_ORG_CONFIG_SHA": _state["sha"],
            "PULLWRIGHT_ORG_CONFIG_TOKEN": _read_token(gh, spec.repo)}


def load_in_runner(workdir: Path) -> str:
    """Inside a runner: fetch the pinned commit the dispatcher passed. Returns the commit, or '' when none is configured."""
    value, token = os.environ.get("PULLWRIGHT_ORG_CONFIG", ""), os.environ.get("PULLWRIGHT_ORG_CONFIG_TOKEN", "")
    if not value or not token:
        return ""
    spec = OrgSpec.parse(value)
    folder, commit = fetch(f"https://x-access-token:{token}@github.com/{spec.repo}.git", spec, workdir / "org-config",
                           os.environ.get("PULLWRIGHT_ORG_CONFIG_SHA", ""))
    _state.update(dir=folder, sha=commit, spec=value)
    return commit


def keep_synced(gh: Any, value: str, cache: Path, every_s: float, stop: threading.Event) -> None:
    while not stop.wait(every_s):
        try:
            sync(gh, value, cache)
        except Exception:
            log.exception("refreshing the organisation config failed; keeping %s", _state["sha"][:12] or "none")
