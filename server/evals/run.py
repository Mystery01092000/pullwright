"""Agent-quality evals: real tasks on throwaway repos, graded by commands, with cost and time.

    uv run python -m evals.run                 # every case in evals/cases
    uv run python -m evals.run python-subtract # one case
    uv run python -m evals.run --max-cost 1.5  # per-case dollar cap
    uv run python -m evals.run --pipeline local  # the local-mode lifecycle instead of the team pipeline

A case is JSON: {"task", "files": {path: content}, "checks": [{"cmd", "expect_exit"?, "expect_stdout"?}], "max_cost_usd"?}.
Two pipelines can be measured:
  team (default)  what an approved issue goes through: describer + verifier plan (auto-approved), then the runner's
                  loop (Sonnet supervisor, Haiku workers, the case's checks as the project checks, independent review)
  local           the local-mode lifecycle: intake, supervisor, workers, diagnosis, summarizer
The case's checks run again in the repo afterwards as the grade; it passes when the pipeline finished (done / green)
and every check passes. Costs are reported per stage.
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

from pullwright.checks import CheckPlan
from pullwright.lifecycle import Run
from pullwright.models import MAIN_MODEL, WORKER_MODEL
from pullwright.pipeline.plan import Developer, PlanRequest, PlanRun, Review
from pullwright.pipeline.work import WorkRun, WorkSpec
from pullwright.session import Session

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


class AutoApprove:
    """The developer's Approve click, for unattended team-pipeline evals."""

    def review_plan(self, request: Any, spec: dict[str, Any], verification: dict[str, Any], revision: int) -> Review:
        return Review("approve")


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


def run_team_case(case: dict[str, Any], client: Any, max_cost: float | None = None, main_model: str = MAIN_MODEL,
                  worker_model: str = WORKER_MODEL, max_rounds: int = 4) -> dict[str, Any]:
    """Plan (describer + verifier, auto-approved) and implement (runner loop) a case, as an approved issue would be."""
    repo = _setup_repo(case)
    cap = max_cost if max_cost is not None else float(case.get("max_cost_usd", 3.0))
    t0, error, state, plan_spec, diagnosis = time.time(), "", "failed", None, None
    models = {"main_model": main_model, "worker_model": worker_model}
    plan_s = Session(cwd=repo, client=client, max_cost_usd=min(1.0, cap), **models)
    work_s = Session(cwd=repo, client=client, max_cost_usd=cap, approve=lambda _cmd: True, **models)
    try:
        plan = PlanRun(plan_s, PlanRequest(task=case["task"], repo=f"eval/{case['name']}", requester=Developer("eval")), AutoApprove()).run()
        plan_spec = plan.spec
        checks = CheckPlan(setup=list(case.get("setup", [])),
                           checks={f"check{i}": c["cmd"] for i, c in enumerate(case.get("checks", []), 1) if c.get("expect_exit", 0) == 0})
        spec = WorkSpec(repo=f"eval/{case['name']}", issue_number=0, title=plan.spec.get("title") or case["name"], task=case["task"], spec=plan.spec)
        result = WorkRun(work_s, spec, checks, max_rounds=max_rounds).run()
        state, diagnosis, error = result.status, result.diagnosis, result.error
    except Exception as e:  # the case fails; the eval keeps going
        error = f"{type(e).__name__}: {e}"
    graded = [_check(repo, c) for c in case.get("checks", [])] if state == "green" else []
    return {
        "name": case["name"], "pipeline": "team",
        "passed": state == "green" and all(c["ok"] for c in graded),
        "state": state, "error": error,
        "cost_usd": round(plan_s.cost_usd + work_s.cost_usd, 4),
        "stage_cost_usd": {"plan": round(plan_s.cost_usd, 4), "work": round(work_s.cost_usd, 4)},
        "seconds": round(time.time() - t0, 1),
        "plan": plan_spec, "diagnosis": diagnosis, "checks": graded, "repo": str(repo),
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
    ap.add_argument("--main-model", default=MAIN_MODEL)
    ap.add_argument("--worker-model", default=WORKER_MODEL)
    ap.add_argument("--pipeline", choices=["team", "local"], default="team", help="what to measure (default: team)")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    import anthropic

    client = anthropic.Anthropic()
    results = []
    for case in load_cases(a.cases or None):
        print(f"running {case['name']}…", flush=True)
        run = run_team_case if a.pipeline == "team" else run_case
        r = run(case, client, a.max_cost, main_model=a.main_model, worker_model=a.worker_model)
        print(f"  {'PASS' if r['passed'] else 'FAIL'} state={r['state']} cost=${r['cost_usd']:.3f} {r['error']}", flush=True)
        results.append(r)
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"pipeline": a.pipeline, "main_model": a.main_model, "worker_model": a.worker_model, "results": results}, indent=2),
                   encoding="utf-8")
    print("\n" + table(results) + f"\n\nsaved {out}")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
