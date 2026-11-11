"""Implementation of one approved issue inside the runner: supervisor + workers until every check passes.

    setup -> supervisor (Haiku workers) -> checks --fail--> supervisor (failure excerpts) -> checks ...
                                             | all pass
                                             v
                                         diagnosis --gaps--> supervisor (gaps) -> checks ...
                                             | complete
                                             v
                                         commit -> green

Bounded by `max_rounds` and the session's dollar cap. A round that ends red or over budget still commits,
so the runner can push a draft PR that shows how far it got.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from .. import checks as checks_mod
from .. import git, repomap
from ..checks import CheckPlan, CheckResult
from ..session import BudgetExceeded, Cancelled, Session

DIAG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "complete": {"type": "boolean"},
        "gaps": {"type": "array", "items": {"type": "string"}, "description": "Acceptance criteria not met, or other problems, each specific"},
        "evidence": {"type": "string", "description": "What you checked (files, commands, outputs)"},
    },
    "required": ["complete", "gaps", "evidence"],
    "additionalProperties": False,
}
RUNNER_DENY = [r"\bgit\s+push\b", r"\bgh\s+(pr|issue|release|repo)\b"]  # the pipeline pushes and opens the PR itself

Status = Literal["green", "failed", "setup_failed", "budget", "cancelled"]


@dataclass
class WorkSpec:
    repo: str  # owner/name
    issue_number: int
    title: str
    task: str  # the developer's words
    spec: dict[str, Any]  # the approved plan (pipeline.plan.PLAN_SCHEMA)
    requester_login: str = ""


@dataclass
class WorkResult:
    status: Status
    rounds: int
    branch: str
    commit: str
    checks: list[CheckResult] = field(default_factory=list)
    diagnosis: dict[str, Any] | None = None
    report: str = ""  # the supervisor's last report
    cost_usd: float = 0.0
    error: str = ""

    @property
    def green(self) -> bool:
        return self.status == "green"


class WorkRun:
    """`branch` set: continue on that existing branch (a follow-up on an open PR) instead of creating one.
    `context`: what the agents must know beyond the plan, e.g. earlier runs and the reviewer comments to address."""

    def __init__(self, session: Session, spec: WorkSpec, plan: CheckPlan | None = None, max_rounds: int = 4,
                 branch: str | None = None, context: str = "", commit_message: str = "") -> None:
        self.s = session
        self.w = spec
        self.checks = plan if plan is not None else checks_mod.load(session.cwd)
        self.max_rounds = max_rounds
        self.existing_branch = branch
        self.context = context
        self.commit_message = commit_message
        self.s.extra_deny = [*self.s.extra_deny, *RUNNER_DENY]
        self._supervisor_id: str | None = None
        self._supervisions = 0
        self.branch = ""

    # --- steps --------------------------------------------------------------

    def setup(self) -> list[CheckResult]:
        self.s.emit("state", state="setup", commands=self.checks.setup)
        results = [checks_mod.run_command(self.s.cwd, "setup", cmd) for cmd in self.checks.setup]
        self.s.emit("setup", results=[_summary(r) for r in results])
        return results

    def _brief(self) -> str:
        return (
            f"GitHub issue {self.w.repo}#{self.w.issue_number}: {self.w.title}\n\n"
            f"Developer's request:\n{self.w.task}\n\n"
            f"Approved plan:\n{json.dumps(self.w.spec, indent=2)}\n\n"
            f"Checks the pipeline runs after you finish (all must pass):\n{self.checks.describe()}\n\n"
            + (f"{self.context}\n\n" if self.context else "")
            + repomap.build(self.s.cwd)
        )

    def supervise(self, feedback: str = "") -> str:
        self.s.emit("state", state="working")
        self._supervisions += 1
        self._supervisor_id = f"supervisor-{self.s.run_id}-{self._supervisions}"
        agent = self.s.agent("supervisor", extra_tools=self.s.spawn_worker_tool(parent=lambda: self._supervisor_id))
        prompt = self._brief()
        if feedback:
            prompt += f"\n\nWork so far (git diff --stat):\n{git.diff_stat(self.s.cwd) or '(no changes yet)'}\n\nFix these first:\n{feedback}"
        return self.s.run(agent, prompt, agent_id=self._supervisor_id).text

    def run_checks(self) -> list[CheckResult]:
        self.s.emit("state", state="checking")
        results = checks_mod.run_checks(self.s.cwd, self.checks)
        self.s.emit("checks", results=[_summary(r) for r in results], green=all(r.ok for r in results))
        return results

    def diagnose(self, results: list[CheckResult]) -> dict[str, Any]:
        self.s.emit("state", state="reviewing")
        prompt = (
            f"Developer's request:\n{self.w.task}\n\nApproved plan and acceptance criteria:\n{json.dumps(self.w.spec, indent=2)}\n\n"
            + (f"{self.context}\n\nEverything listed above as feedback to address must be addressed too.\n\n" if self.context else "")
            + f"Check results (all passed):\n{checks_mod.report(results)}\n\n"
            f"Diff of the work:\n```\n{git.diff(self.s.cwd)}\n```"
        )
        diag = json.loads(self.s.run(self.s.agent("diagnosis", output_schema=DIAG_SCHEMA), prompt).text)
        self.s.emit("diagnosis", **diag)
        return diag

    # --- the loop -----------------------------------------------------------

    def run(self) -> WorkResult:
        result = WorkResult(status="failed", rounds=0, branch="", commit="")
        try:
            if git.is_repo(self.s.cwd):
                self.branch = (git.checkout(self.s.cwd, self.existing_branch) if self.existing_branch
                               else git.create_branch(self.s.cwd, f"{self.w.issue_number}-{git.slugify(self.w.title, 32)}"))
                result.branch = self.branch
                self.s.emit("branch", branch=self.branch)
            setup = self.setup()
            if not all(r.ok for r in setup):
                result.status, result.checks = "setup_failed", setup
                result.error = "dependency setup failed:\n" + checks_mod.report(setup)
                return self._finish(result)
            feedback = ""
            for rnd in range(1, self.max_rounds + 1):
                result.rounds = rnd
                result.report = self.supervise(feedback)
                results = self.run_checks()
                result.checks = results
                if not all(r.ok for r in results):
                    feedback = "These checks fail:\n" + checks_mod.report([r for r in results if not r.ok])
                    continue
                diag = self.diagnose(results)
                result.diagnosis = diag
                if diag["complete"]:
                    result.status = "green"
                    break
                feedback = "All checks pass, but the reviewer found gaps against the acceptance criteria:\n- " + "\n- ".join(diag["gaps"])
        except BudgetExceeded as e:
            result.status, result.error = "budget", str(e)
        except Cancelled as e:
            result.status, result.error = "cancelled", str(e)
        return self._finish(result)

    def _finish(self, result: WorkResult) -> WorkResult:
        if self.branch:
            verdict = "checks green" if result.green else result.status
            message = self.commit_message or f"{self.w.title} (#{self.w.issue_number})"
            result.commit = git.commit_all(self.s.cwd, f"{message}\n\nPullwright: {verdict}.")
        result.cost_usd = self.s.cost_usd
        self.s.emit("state", state=result.status, rounds=result.rounds, commit=result.commit, cost_usd=result.cost_usd)
        return result


def _summary(r: CheckResult) -> dict[str, Any]:
    return {"name": r.name, "command": r.command, "ok": r.ok, "exit_code": r.exit_code, "seconds": round(r.seconds, 1),
            "excerpt": "" if r.ok else r.excerpt(2000)}
