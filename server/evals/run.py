"""Agent-quality evals: real tasks on throwaway repos, graded by commands, with cost and time.

    uv run python -m evals.run                 # every case in evals/cases
    uv run python -m evals.run python-subtract # one case
    uv run python -m evals.run --max-cost 1.5  # per-case dollar cap

A case is JSON: {"task", "files": {path: content}, "checks": [{"cmd", "expect_exit"?, "expect_stdout"?}], "max_cost_usd"?}.
Checks run in the repo after the run finishes; the case passes when the run reached `done` and every check passes.
Results go to evals/results/<timestamp>.json plus a markdown table on stdout. Every run spends real money.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from pullwright.lifecycle import Run

HERE = Path(__file__).parent
CASES = HERE / "cases"
RESULTS = HERE / "results"


class AutoIO:
    """Approves the plan and every command, declines the PR. Evals are unattended."""

    def confirm_intake(self, intake: dict[str, Any]) -> tuple[bool, str]:
        return True, ""

    def approve_command(self, command: str) -> bool:
        return True

    def want_pr(self, summary: str) -> bool:
        return False


def load_cases(names: list[str] | None = None) -> list[dict[str, Any]]:
    cases = []
    for p in sorted(CASES.glob("*.json")):
        if names and p.stem not in names:
            continue
        cases.append({"name": p.stem, **json.loads(p.read_text(encoding="utf-8"))})
    return cases


def _setup_repo(case: dict[str, Any]) -> Path:
    repo = Path(tempfile.mkdtemp(prefix=f"eval-{case['name']}-"))
    for rel, content in case.get("files", {}).items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "eval@pullwright"], ["git", "config", "user.name", "eval"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "fixture"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    return repo


def _check(repo: Path, chk: dict[str, Any]) -> dict[str, Any]:
    r = subprocess.run(chk["cmd"], shell=True, cwd=repo, capture_output=True, text=True, timeout=chk.get("timeout", 300))
    ok = r.returncode == chk.get("expect_exit", 0) and (chk.get("expect_stdout", "") in r.stdout)
    return {"cmd": chk["cmd"], "ok": ok, "exit": r.returncode, "stdout": r.stdout[-1500:], "stderr": r.stderr[-1500:]}


def run_case(case: dict[str, Any], client: Any, max_cost: float | None = None, **run_kw: Any) -> dict[str, Any]:
    repo = _setup_repo(case)
    cap = max_cost if max_cost is not None else float(case.get("max_cost_usd", 3.0))
    t0 = time.time()
    run = Run(task=case["task"], cwd=repo, io=AutoIO(), client=client, max_cost_usd=cap, **run_kw)
    error = ""
    try:
        run.run()
    except Exception as e:  # the case fails; the eval keeps going
        error = f"{type(e).__name__}: {e}"
    checks = [_check(repo, c) for c in case.get("checks", [])] if run.state == "done" else []
    return {
        "name": case["name"],
        "passed": run.state == "done" and all(c["ok"] for c in checks),
        "state": run.state,
        "error": error,
        "cost_usd": round(run.cost_usd, 4),
        "seconds": round(time.time() - t0, 1),
        "diagnosis": run.diagnosis,
        "checks": checks,
        "repo": str(repo),
    }


def table(results: list[dict[str, Any]]) -> str:
    rows = ["| case | pass | state | checks | cost | time |", "|---|---|---|---|---|---|"]
    for r in results:
        n_ok = sum(c["ok"] for c in r["checks"])
        rows.append(f"| {r['name']} | {'yes' if r['passed'] else 'NO'} | {r['state']} | {n_ok}/{len(r['checks'])} | ${r['cost_usd']:.3f} | {r['seconds']}s |")
    total = sum(r["cost_usd"] for r in results)
    rows.append(f"| **total** | {sum(r['passed'] for r in results)}/{len(results)} | | | ${total:.3f} | |")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cases", nargs="*", help="case names (default: all)")
    ap.add_argument("--max-cost", type=float, default=None, help="per-case dollar cap")
    ap.add_argument("--main-model", default="claude-opus-5-5")
    ap.add_argument("--worker-model", default="claude-sonnet-5-5")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    import anthropic

    client = anthropic.Anthropic()
    results = []
    for case in load_cases(a.cases or None):
        print(f"running {case['name']}…", flush=True)
        r = run_case(case, client, a.max_cost, main_model=a.main_model, worker_model=a.worker_model)
        print(f"  {'PASS' if r['passed'] else 'FAIL'} state={r['state']} cost=${r['cost_usd']:.3f} {r['error']}", flush=True)
        results.append(r)
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"main_model": a.main_model, "worker_model": a.worker_model, "results": results}, indent=2), encoding="utf-8")
    print("\n" + table(results) + f"\n\nsaved {out}")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
