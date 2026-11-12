import json

from evals.run import load_cases, run_case, run_team_case, table
from tests.fake_client import FakeClient, text, tool_use, turn
from tests.test_lifecycle import DIAG_OK, INTAKE

CALC = "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n"
ADD_ONLY = "def add(a, b):\n    return a + b\n"
TEST = "from calc import add, subtract\n\n\ndef test_both():\n    assert add(1, 2) == 3 and subtract(5, 3) == 2\n"


def _turns(good: bool):
    return [
        turn(text(INTAKE)),
        turn(tool_use("spawn_worker", {"task": "t", "context": "c"})),
        turn(tool_use("write_file", {"path": "calc.py", "content": CALC if good else "def add(a, b):\n    return a + b\n"}, "tu_1")),
        turn(tool_use("write_file", {"path": "test_calc.py", "content": TEST}, "tu_2")),
        turn(text("done")),
        turn(text("sup done")),
        turn(text(DIAG_OK)),
        turn(text("sum\nTITLE: t")),
    ]


def test_cases_load():
    names = [c["name"] for c in load_cases()]
    assert {"python-subtract", "python-bugfix", "go-todo-api"} <= set(names)
    assert load_cases(["python-subtract"])[0]["checks"]


def test_run_case_pass_and_fail():
    case = load_cases(["python-subtract"])[0]
    ok = run_case(case, FakeClient(_turns(True)))
    assert ok["passed"] and ok["state"] == "done" and all(c["ok"] for c in ok["checks"]) and ok["cost_usd"] > 0
    bad = run_case(case, FakeClient(_turns(False)))
    assert not bad["passed"] and bad["state"] == "done" and not bad["checks"][0]["ok"]
    md = table([ok, bad])
    assert "| python-subtract | yes |" in md and "| python-subtract | NO |" in md and "1/2" in md


PLAN = json.dumps({"title": "Add subtract", "summary": "add subtract()", "relevant_files": [{"path": "calc.py", "why": "target"}],
                   "plan": ["add subtract"], "acceptance_criteria": ["subtract works"], "test_plan": ["pytest"], "risks": [], "questions": []})
VERIFIED = json.dumps({"approved": True, "corrections": [], "evidence": "read"})


def _team_turns(good: bool):
    return [
        turn(text(PLAN)), turn(text(VERIFIED)),  # describer, verifier
        turn(tool_use("spawn_worker", {"task": "t", "context": "c"})),  # supervisor
        turn(tool_use("write_file", {"path": "calc.py", "content": CALC if good else ADD_ONLY}, "tu_1"), model="claude-haiku-5-5"),
        turn(tool_use("write_file", {"path": "test_calc.py", "content": TEST}, "tu_2"), model="claude-haiku-5-5"),
        turn(text("done"), model="claude-haiku-5-5"),
        turn(text("sup done")),
        turn(text(DIAG_OK)),  # reviewer (only reached when the checks pass)
    ]


def test_team_pipeline_eval_measures_plan_and_work_separately():
    case = load_cases(["python-subtract"])[0]
    ok = run_team_case(case, FakeClient(_team_turns(True)), max_rounds=1)
    assert ok["passed"] and ok["state"] == "green" and ok["plan"]["title"] == "Add subtract"
    assert ok["stage_cost_usd"]["plan"] > 0 and ok["stage_cost_usd"]["work"] > 0
    bad = run_team_case(case, FakeClient(_team_turns(False)[:-1]), max_rounds=1)  # checks fail, no review
    assert not bad["passed"] and bad["state"] == "failed" and bad["checks"] == []


def test_planning_evals_score_files_and_planted_mistakes():
    from evals.planning import file_scores, load_cases, run_case, table

    assert file_scores(["./orders/api.py", "orders/store.py", "README.md"], ["orders/api.py", "orders/store.py"]) == (1.0, 0.667)
    cases = {c["name"]: c for c in load_cases()}
    assert {"orders-pagination", "verify-missing-function"} <= set(cases)

    good = json.dumps({"title": "Paginate", "summary": "s", "plan": ["p"], "acceptance_criteria": ["a"], "test_plan": ["t"], "risks": [], "questions": [],
                       "relevant_files": [{"path": p, "why": "w"} for p in ("orders/api.py", "orders/store.py", "tests/test_api.py", "README.md")]})
    r = run_case(cases["orders-pagination"], FakeClient([turn(text(good))]))
    assert r["passed"] and r["recall"] == 1.0 and r["precision"] == 0.75 and r["cost_usd"] > 0

    caught = json.dumps({"approved": False, "corrections": ["OrderStore has no find_by_customer method; add it"], "evidence": "read store.py"})
    fooled = json.dumps({"approved": True, "corrections": [], "evidence": "looks fine"})
    vague = json.dumps({"approved": False, "corrections": ["add more tests"], "evidence": "x"})
    rs = [run_case(cases["verify-missing-function"], FakeClient([turn(text(t))])) for t in (caught, fooled, vague)]
    assert [x["passed"] for x in rs] == [True, False, False]
    md = table([r, *rs])
    assert "approved the bad plan" in md and "rejected, missed the mistake" in md and "2/4" in md
