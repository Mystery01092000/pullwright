"""Deterministic checks: the commands that decide whether an issue is done. A model never judges these.

Configured in <repo>/.pullwright/checks.json:

    {"setup": ["uv sync"], "test": "uv run pytest -q", "lint": "uv run ruff check .", "typecheck": null}

`setup` runs once after the clone (installing dependencies); every other key is a check that must exit 0.
A key set to null turns that check off. Anything not configured is detected from the project files.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import CONFIG_DIRS

CHECK_ORDER = ("build", "lint", "typecheck", "test")
_FAIL_LINE = re.compile(r"(FAIL|Error|error:|ERROR|failed|Traceback|assert|panic:|✗|×)")


@dataclass
class CheckPlan:
    setup: list[str] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)  # name -> shell command
    source: str = "detected"  # detected | config | mixed

    def describe(self) -> str:
        lines = [f"- setup: `{c}`" for c in self.setup] + [f"- {n}: `{c}`" for n, c in self.checks.items()]
        return "\n".join(lines) or "- (no checks found)"


@dataclass
class CheckResult:
    name: str
    command: str
    exit_code: int
    seconds: float
    output: str  # full combined output
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def excerpt(self, max_chars: int = 4000) -> str:
        """The part of the output a model needs to fix the failure: failure lines with context, then the tail."""
        return failure_excerpt(self.output, max_chars)


def failure_excerpt(output: str, max_chars: int = 4000) -> str:
    lines = output.splitlines()
    if len(output) <= max_chars:
        return output
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if _FAIL_LINE.search(line):
            keep.update(range(max(0, i - 2), min(len(lines), i + 6)))
    keep.update(range(max(0, len(lines) - 30), len(lines)))
    out, prev = [], -2
    for i in sorted(keep):
        if i != prev + 1:
            out.append("...")
        out.append(lines[i])
        prev = i
    text = "\n".join(out)
    return text if len(text) <= max_chars else "...\n" + text[-max_chars:]


def _has(root: Path, *names: str) -> bool:
    return any((root / n).exists() for n in names)


def _node_runner(root: Path) -> str:
    if _has(root, "pnpm-lock.yaml"):
        return "pnpm"
    if _has(root, "yarn.lock"):
        return "yarn"
    if _has(root, "bun.lockb", "bun.lock"):
        return "bun"
    return "npm"


def detect(root: Path) -> CheckPlan:
    """Best-effort setup and checks from the project files. One ecosystem per repo root."""
    plan = CheckPlan()
    if _has(root, "pyproject.toml", "requirements.txt", "setup.py"):
        uv = _has(root, "uv.lock") or "[tool.uv]" in _read(root / "pyproject.toml")
        if uv:
            plan.setup.append("uv sync")
            run = "uv run "
        else:
            plan.setup.append("python -m pip install -e ." if _has(root, "pyproject.toml", "setup.py") else "python -m pip install -r requirements.txt")
            run = "python -m "
        pyproject = _read(root / "pyproject.toml")
        if "ruff" in pyproject:
            plan.checks["lint"] = f"{run}ruff check ."
        if "mypy" in pyproject:
            plan.checks["typecheck"] = f"{run}mypy ."
        if _has(root, "tests", "test") or "pytest" in pyproject:
            plan.checks["test"] = f"{run}pytest -q"
    elif _has(root, "package.json"):
        runner = _node_runner(root)
        plan.setup.append({"npm": "npm ci" if _has(root, "package-lock.json") else "npm install", "pnpm": "pnpm install --frozen-lockfile",
                           "yarn": "yarn install --frozen-lockfile", "bun": "bun install"}[runner])
        scripts = _package_scripts(root)
        for name, script in (("build", "build"), ("lint", "lint"), ("typecheck", "typecheck"), ("test", "test")):
            if script in scripts:
                plan.checks[name] = f"{runner} run {script}" if runner != "npm" or script != "test" else "npm test"
    elif _has(root, "go.mod"):
        plan.setup.append("go mod download")
        plan.checks.update({"build": "go build ./...", "lint": "go vet ./...", "test": "go test ./..."})
    elif _has(root, "Cargo.toml"):
        plan.setup.append("cargo fetch")
        plan.checks.update({"build": "cargo build --quiet", "test": "cargo test --quiet"})
    elif _has(root, "pom.xml"):
        plan.checks["test"] = "mvn -q -B test"
    elif _has(root, "build.gradle", "build.gradle.kts"):
        plan.checks["test"] = "./gradlew test --quiet"
    return plan


def load(root: Path) -> CheckPlan:
    """Configured checks layered over detected ones. A configured null removes a detected check."""
    plan = detect(root)
    cfg_file = next((root / d / "checks.json" for d in CONFIG_DIRS if (root / d / "checks.json").is_file()), None)
    if cfg_file is None:
        return plan
    cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
    plan.source = "mixed" if plan.checks or plan.setup else "config"
    if "setup" in cfg:
        setup = cfg["setup"]
        plan.setup = [] if setup is None else ([setup] if isinstance(setup, str) else list(setup))
    for name, cmd in cfg.items():
        if name == "setup":
            continue
        if cmd is None:
            plan.checks.pop(name, None)
        else:
            plan.checks[name] = cmd
    plan.checks = dict(sorted(plan.checks.items(), key=lambda kv: (CHECK_ORDER.index(kv[0]) if kv[0] in CHECK_ORDER else len(CHECK_ORDER), kv[0])))
    return plan


def run_command(root: Path, name: str, command: str, timeout: int = 900) -> CheckResult:
    start = time.monotonic()
    try:
        r = subprocess.run(command, shell=True, cwd=root, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return CheckResult(name, command, r.returncode, time.monotonic() - start, (r.stdout or "") + (r.stderr or ""))
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return CheckResult(name, command, -1, time.monotonic() - start, out + f"\n(timed out after {timeout}s)", timed_out=True)


def run_checks(root: Path, plan: CheckPlan, timeout: int = 900) -> list[CheckResult]:
    """Run every check, in order, even after a failure: the model gets the whole picture in one round."""
    return [run_command(root, name, cmd, timeout) for name, cmd in plan.checks.items()]


def report(results: list[CheckResult], max_chars_each: int = 3000) -> str:
    """Markdown for the supervisor (failures with excerpts) and for the PR body (one line per check)."""
    lines = []
    for r in results:
        mark = "pass" if r.ok else ("timeout" if r.timed_out else f"fail (exit {r.exit_code})")
        lines.append(f"- **{r.name}** `{r.command}`: {mark}, {r.seconds:.1f}s")
        if not r.ok:
            lines.append(f"```\n{r.excerpt(max_chars_each)}\n```")
    return "\n".join(lines)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


def _package_scripts(root: Path) -> dict[str, str]:
    try:
        return dict(json.loads(_read(root / "package.json")).get("scripts", {}))
    except (ValueError, AttributeError):
        return {}
