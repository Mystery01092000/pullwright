"""Planning evals: does the describer name the right files, and does the verifier catch a planted mistake?

    uv run python -m evals.planning                       # every case in evals/plan_cases
    uv run python -m evals.planning orders-pagination     # one case
    uv run python -m evals.planning --model openai:<m>    # describer and verifier on another model (any provider)

Two kinds of case (JSON in evals/plan_cases):

  describe  {"kind": "describe", "task", "files": {path: content}, "expect_files": [path], "min_recall"?: 1.0, "min_precision"?: 0.5}
            One describer pass. Scores recall and precision of plan.relevant_files against expect_files. It passes when
            both reach their minimums.
  verify    {"kind": "verify", "task", "files", "plan": {PLAN_SCHEMA with a planted mistake}, "mistake": [keyword, ...]}
            One verifier pass on the given plan. It passes when the verifier rejects the plan and a correction names
            the problem (any keyword, case-insensitive).

Every result carries its cost. Results go to evals/results/plan-<timestamp>.json plus a table on stdout. Every run
spends real money.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from pullwright.models import MAIN_MODEL
from pullwright.pipeline.plan import Developer, PlanRequest, PlanRun
from pullwright.session import Session

from .run import RESULTS, _setup_repo

HERE = Path(__file__).parent
CASES = HERE / "plan_cases"


def load_cases(names: list[str] | None = None) -> list[dict[str, Any]]:
    return [{"name": p.stem, **json.loads(p.read_text(encoding="utf-8"))}
            for p in sorted(CASES.glob("*.json")) if not names or p.stem in names]


def _norm(path: str) -> str:
    return path.strip().lstrip("./").replace("\\", "/")


def file_scores(named: list[str], expected: list[str]) -> tuple[float, float]:
    named_set, expected_set = {_norm(p) for p in named}, {_norm(p) for p in expected}
    hits = len(named_set & expected_set)
    recall = hits / len(expected_set) if expected_set else 1.0
    precision = hits / len(named_set) if named_set else 0.0
    return round(recall, 3), round(precision, 3)


class _NoReview:
    def review_plan(self, *a: Any) -> Any:  # never reached: the eval calls describe/verify directly
        raise AssertionError("planning evals do not review")


def run_case(case: dict[str, Any], client: Any, model: str = MAIN_MODEL, max_cost: float = 1.0) -> dict[str, Any]:
    repo = _setup_repo(case)
    s = Session(cwd=repo, client=client, main_model=model, max_cost_usd=max_cost)
    run = PlanRun(s, PlanRequest(task=case["task"], repo=f"eval/{case['name']}", requester=Developer("eval")), _NoReview())
    t0, out = time.time(), {"name": case["name"], "kind": case["kind"], "passed": False, "error": ""}
    try:
        if case["kind"] == "describe":
            spec = run.describe()
            named = [f["path"] for f in spec.get("relevant_files", [])]
            recall, precision = file_scores(named, case["expect_files"])
            out.update(named=named, recall=recall, precision=precision,
                       passed=recall >= case.get("min_recall", 1.0) and precision >= case.get("min_precision", 0.5))
        elif case["kind"] == "verify":
            run.spec = case["plan"]
            v = run.verify()
            text = " ".join(v.get("corrections", [])).lower()
            named_it = any(k.lower() in text for k in case["mistake"])
            out.update(approved=v.get("approved"), corrections=v.get("corrections", []), named_mistake=named_it,
                       passed=not v.get("approved") and named_it)
        else:
            raise ValueError(f"unknown case kind {case['kind']!r}")
    except Exception as e:  # the case fails; the eval keeps going
        out["error"] = f"{type(e).__name__}: {e}"
    out.update(cost_usd=round(s.cost_usd, 4), seconds=round(time.time() - t0, 1), model=model)
    return out


def table(results: list[dict[str, Any]]) -> str:
    rows = ["| case | kind | pass | score | cost | time |", "|---|---|---|---|---|---|"]
    for r in results:
        if r["kind"] == "describe" and "recall" in r:
            score = f"recall {r['recall']:.2f}, precision {r['precision']:.2f}"
        elif r["kind"] == "verify" and "approved" in r:
            score = "rejected, named it" if r["passed"] else ("approved the bad plan" if r["approved"] else "rejected, missed the mistake")
        else:
            score = r["error"][:60]
        rows.append(f"| {r['name']} | {r['kind']} | {'yes' if r['passed'] else 'NO'} | {score} | ${r['cost_usd']:.3f} | {r['seconds']}s |")
    total = sum(r["cost_usd"] for r in results)
    n = len(results) or 1
    rows.append(f"| **total** | | {sum(r['passed'] for r in results)}/{len(results)} | | ${total:.3f} (${total / n:.3f} per plan) | |")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cases", nargs="*", help="case names (default: all)")
    ap.add_argument("--model", default=MAIN_MODEL, help="model for the describer and verifier (provider:model works)")
    ap.add_argument("--max-cost", type=float, default=1.0, help="per-case dollar cap")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from pullwright.providers import Router

    client = Router()
    results = []
    for case in load_cases(a.cases or None):
        print(f"running {case['name']} ({case['kind']})…", flush=True)
        r = run_case(case, client, a.model, a.max_cost)
        print(f"  {'PASS' if r['passed'] else 'FAIL'} cost=${r['cost_usd']:.3f} {r['error']}", flush=True)
        results.append(r)
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"plan-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"model": a.model, "results": results}, indent=2), encoding="utf-8")
    print("\n" + table(results) + f"\n\nsaved {out}")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
