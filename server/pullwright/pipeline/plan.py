"""Planning: describer drafts, verifier checks, the developer approves. Read-only; nothing in the repo runs.

    request (chat) -> describer -> verifier --rejects, once--> describer (with corrections)
                                    | approves
                                    v
                          developer review --revise--> describer (with feedback) ... --approve--> Plan
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

from .. import repomap
from ..session import Session

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Imperative issue / PR title, at most 70 characters"},
        "summary": {"type": "string"},
        "relevant_files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "why": {"type": "string"}},
                "required": ["path", "why"],
                "additionalProperties": False,
            },
        },
        "plan": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "test_plan": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary", "relevant_files", "plan", "acceptance_criteria", "test_plan", "risks", "questions"],
    "additionalProperties": False,
}
VERIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "corrections": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "string"},
    },
    "required": ["approved", "corrections", "evidence"],
    "additionalProperties": False,
}


@dataclass
class Developer:
    """Who asked. `github_login` is what issues and PRs tag; `name` is what people read."""
    name: str
    github_login: str = ""
    channel: str = ""  # slack | teams | web
    channel_user_id: str = ""


@dataclass
class PlanRequest:
    task: str
    repo: str  # owner/name
    requester: Developer
    thread_url: str = ""  # link back to the chat thread, shown on the issue
    thread_ref: dict[str, Any] = field(default_factory=dict)  # channel-specific address of the thread
    links: str = ""  # what the request's links say, fetched before planning (pullwright.links)


@dataclass
class Review:
    decision: Literal["approve", "revise", "cancel"]
    feedback: str = ""
    reviewer: Developer | None = None


class Reviewer(Protocol):
    """Implemented by each channel: show the plan in the thread and wait for Approve / Revise / Cancel."""

    def review_plan(self, request: PlanRequest, spec: dict[str, Any], verification: dict[str, Any], revision: int) -> Review: ...


class PlanCancelled(RuntimeError):
    pass


@dataclass
class Plan:
    request: PlanRequest
    spec: dict[str, Any]  # PLAN_SCHEMA
    verification: dict[str, Any]  # VERIFY_SCHEMA of the version the developer approved
    approved_by: Developer
    revisions: int
    cost_usd: float
    costs: dict[str, Any] = field(default_factory=dict)  # costs.CostMeter breakdown

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PlanRun:
    def __init__(self, session: Session, request: PlanRequest, reviewer: Reviewer, max_revisions: int = 3) -> None:
        self.s = session
        self.request = request
        self.reviewer = reviewer
        self.max_revisions = max_revisions
        self.spec: dict[str, Any] | None = None
        self.verification: dict[str, Any] | None = None

    def _context(self) -> str:
        links = f"\n\n{self.request.links}" if self.request.links else ""
        return f"Repository: {self.request.repo}\n\n{repomap.build(self.s.cwd)}{links}"

    def describe(self, feedback: str = "") -> dict[str, Any]:
        prompt = f"Request from {self.request.requester.name}:\n{self.request.task}\n\n{self._context()}"
        if self.spec is not None and feedback:
            prompt += f"\n\nYour previous plan:\n{json.dumps(self.spec, indent=2)}\n\nChange it as follows:\n{feedback}"
        self.s.emit("state", state="describing")
        res = self.s.run(self.s.agent("describer", output_schema=PLAN_SCHEMA), prompt)
        self.spec = json.loads(res.text)
        self.s.emit("plan_draft", spec=self.spec)
        return self.spec

    def verify(self) -> dict[str, Any]:
        prompt = (f"Request from {self.request.requester.name}:\n{self.request.task}\n\n"
                  f"Plan to verify:\n{json.dumps(self.spec, indent=2)}\n\n{self._context()}")
        self.s.emit("state", state="verifying")
        res = self.s.run(self.s.agent("verifier", output_schema=VERIFY_SCHEMA), prompt)
        self.verification = json.loads(res.text)
        self.s.emit("plan_verification", **self.verification)
        return self.verification

    def draft(self, feedback: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
        """One describer pass, checked by the verifier; one more describer pass if the verifier objects."""
        self.describe(feedback)
        if not self.verify()["approved"]:
            self.describe("The plan verifier found these problems; fix every one:\n- " + "\n- ".join(self.verification["corrections"]))
            self.verify()
        return self.spec, self.verification

    def run(self) -> Plan:
        feedback = ""
        for revision in range(self.max_revisions + 1):
            spec, verification = self.draft(feedback)
            self.s.emit("state", state="awaiting_approval", revision=revision)
            review = self.reviewer.review_plan(self.request, spec, verification, revision)
            self.s.emit("plan_review", decision=review.decision, feedback=review.feedback,
                        reviewer=review.reviewer.github_login if review.reviewer else "")
            if review.decision == "approve":
                self.s.emit("state", state="approved")
                return Plan(self.request, spec, verification, review.reviewer or self.request.requester, revision, self.s.cost_usd, self.s.meter.to_dict())
            if review.decision == "cancel":
                self.s.emit("state", state="cancelled")
                raise PlanCancelled(review.feedback or "cancelled by the developer")
            feedback = review.feedback
        self.s.emit("state", state="cancelled")
        raise PlanCancelled(f"not approved after {self.max_revisions} revisions")


def render_markdown(spec: dict[str, Any], verification: dict[str, Any] | None = None) -> str:
    """The plan as the developer reads it in chat and on the issue."""
    out = [spec["summary"], ""]
    if spec["relevant_files"]:
        out += ["**Relevant files**"] + [f"- `{f['path']}`: {f['why']}" for f in spec["relevant_files"]] + [""]
    out += ["**Plan**"] + [f"{i}. {step}" for i, step in enumerate(spec["plan"], 1)] + [""]
    out += ["**Acceptance criteria**"] + [f"- [ ] {c}" for c in spec["acceptance_criteria"]] + [""]
    if spec["test_plan"]:
        out += ["**Test plan**"] + [f"- {t}" for t in spec["test_plan"]] + [""]
    if spec["risks"]:
        out += ["**Risks and out of scope**"] + [f"- {r}" for r in spec["risks"]] + [""]
    if spec["questions"]:
        out += ["**Questions**"] + [f"- {q}" for q in spec["questions"]] + [""]
    if verification is not None:
        verdict = "checked against the code" if verification["approved"] else "verifier still has concerns: " + "; ".join(verification["corrections"])
        out.append(f"_Plan verifier: {verdict}._")
    return "\n".join(out).rstrip()
