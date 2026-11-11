import json
import subprocess
import sys
from pathlib import Path

from pullwright import checks, repomap


def test_detects_a_uv_python_project(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n[dependency-groups]\ndev=['pytest','ruff']\n")
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / "tests").mkdir()
    plan = checks.detect(tmp_path)
    assert plan.setup == ["uv sync"]
    assert plan.checks == {"lint": "uv run ruff check .", "test": "uv run pytest -q"}


def test_detects_pytest_from_standalone_test_modules(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    for filename in ("test_widget.py", "widget_test.py"):
        (tmp_path / filename).write_text("")
        assert checks.detect(tmp_path).checks == {"test": "python -m pytest -q"}
        (tmp_path / filename).unlink()


def test_detects_pytest_configuration_files(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    for filename, content in (
        ("pytest.ini", ""),
        ("setup.cfg", "[tool:pytest]\n"),
        ("tox.ini", "[tox]\n"),
    ):
        (tmp_path / filename).write_text(content)
        assert checks.detect(tmp_path).checks == {"test": "python -m pytest -q"}
        (tmp_path / filename).unlink()


def test_detects_node_scripts_with_the_lockfiles_runner(tmp_path: Path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "vite build", "test": "vitest", "lint": "eslint ."}}))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    plan = checks.detect(tmp_path)
    assert plan.setup == ["pnpm install --frozen-lockfile"]
    assert plan.checks == {"build": "pnpm run build", "lint": "pnpm run lint", "test": "pnpm run test"}


def test_detects_go(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module x\n")
    assert checks.detect(tmp_path).checks == {"build": "go build ./...", "lint": "go vet ./...", "test": "go test ./..."}


def test_config_overrides_adds_and_removes_detected_checks(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / ".pullwright").mkdir()
    (tmp_path / ".pullwright" / "checks.json").write_text(json.dumps(
        {"setup": "make deps", "lint": None, "test": "go test -race ./...", "typecheck": "staticcheck ./..."}))
    plan = checks.load(tmp_path)
    assert plan.setup == ["make deps"] and plan.source == "mixed"
    assert list(plan.checks) == ["build", "typecheck", "test"]  # lint removed, the rest in check order
    assert plan.checks["test"] == "go test -race ./..."


def test_run_checks_runs_every_check_and_reports_failures(tmp_path: Path):
    py = sys.executable
    plan = checks.CheckPlan(checks={
        "lint": f'"{py}" -c "print(\'clean\')"',
        "test": f'"{py}" -c "import sys; print(\'FAILED test_x\'); sys.exit(1)"',
    })
    results = checks.run_checks(tmp_path, plan)
    assert [(r.name, r.ok) for r in results] == [("lint", True), ("test", False)]
    text = checks.report(results)
    assert "**lint**" in text and "pass" in text
    assert "fail (exit 1)" in text and "FAILED test_x" in text


def test_timeout_is_a_failure(tmp_path: Path):
    r = checks.run_command(tmp_path, "slow", f'"{sys.executable}" -c "import time; time.sleep(5)"', timeout=1)
    assert r.timed_out and not r.ok


def test_failure_excerpt_keeps_failures_and_the_tail():
    noise = "\n".join(f"ok line {i}" for i in range(2000))
    out = noise + "\nE   AssertionError: expected 2\n" + noise + "\n== 1 failed, 99 passed =="
    ex = checks.failure_excerpt(out, max_chars=3000)
    assert len(ex) <= 3100
    assert "AssertionError: expected 2" in ex and "1 failed, 99 passed" in ex


def test_repomap_lists_files_and_symbols(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("import os\n\ndef hello():\n    pass\n\nclass Store:\n    pass\n\ndef _private():\n    pass\n")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "api.ts").write_text("export async function fetchRuns() {}\nexport const BASE = ''\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("function y() {}\n")
    subprocess.run(["git", "add", "-A", "-f"], cwd=tmp_path, check=True)
    text = repomap.build(tmp_path)
    assert "app.py  (hello, Store)" in text
    assert "web/api.ts  (fetchRuns, BASE)" in text
    assert "node_modules" not in text and "_private" not in text
