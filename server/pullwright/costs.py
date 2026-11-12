"""Where the money went: cost per stage (plan, work, review) and per model, built from a run's events.

A CostMeter listens on a Session's bus. `agent_start` names each agent, and each `usage` event is charged to that
agent's stage and to the model that answered. The resulting breakdown travels with the plan, the runner's
result, the PR body and the work log, and the store adds it up per request for the web cost view.
"""
from __future__ import annotations

import re
from typing import Any

from .events import Event

STAGES = ("plan", "work", "review", "other")
_ROLE_STAGE = {"describer": "plan", "verifier": "plan", "intake": "plan", "router": "plan", "responder": "plan", "claude-code": "work", "goose": "work",
               "supervisor": "work", "worker": "work", "diagnosis": "review", "reviewer": "review", "summarizer": "review"}
STAGE_LABEL = {"plan": "Planning", "work": "Implementation", "review": "Review", "other": "Other"}


def stage_of(agent_name: str) -> str:
    """Roles map to their stage; a numbered specialist (`security-reviewer3`, spawned by the supervisor) is work."""
    base = re.sub(r"\d+$", "", agent_name)
    if base in _ROLE_STAGE:
        return _ROLE_STAGE[base]
    return "work" if base != agent_name else "other"


def empty() -> dict[str, Any]:
    return {"total_usd": 0.0, "calls": 0, "by_stage": {}, "by_model": {}, "unpriced": []}


def _bucket(d: dict[str, Any], key: str) -> dict[str, Any]:
    return d.setdefault(key, {"cost_usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0})


def _add(b: dict[str, Any], cost: float, calls: int, tin: int, tout: int) -> None:
    b["cost_usd"] = round(b["cost_usd"] + cost, 6)
    b["calls"] += calls
    b["input_tokens"] += tin
    b["output_tokens"] += tout


class CostMeter:
    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.data = empty()

    def __call__(self, ev: Event) -> None:
        if ev.type == "agent_start":
            self.names[ev.agent_id] = ev.payload.get("name", "")
        elif ev.type == "usage":
            p = ev.payload
            tin = int(p.get("input_tokens", 0)) + int(p.get("cache_read_input_tokens", 0)) + int(p.get("cache_creation_input_tokens", 0))
            tout = int(p.get("output_tokens", 0))
            cost = float(p.get("cost_usd", 0.0))
            model = p.get("model", "")
            _add(_bucket(self.data["by_stage"], stage_of(self.names.get(ev.agent_id, ""))), cost, 1, tin, tout)
            _add(_bucket(self.data["by_model"], model), cost, 1, tin, tout)
            self.data["total_usd"] = round(self.data["total_usd"] + cost, 6)
            self.data["calls"] += 1
            if p.get("priced") is False and model not in self.data["unpriced"]:
                self.data["unpriced"].append(model)

    def to_dict(self) -> dict[str, Any]:
        return {**self.data, "by_stage": dict(self.data["by_stage"]), "by_model": dict(self.data["by_model"]), "unpriced": list(self.data["unpriced"])}


def merge(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any]:
    """The sum of two breakdowns (a request's planning plus each runner job)."""
    out = empty()
    for src in (a or {}, b or {}):
        out["total_usd"] = round(out["total_usd"] + float(src.get("total_usd", 0.0)), 6)
        out["calls"] += int(src.get("calls", 0))
        for dim in ("by_stage", "by_model"):
            for k, v in (src.get(dim) or {}).items():
                _add(_bucket(out[dim], k), float(v.get("cost_usd", 0.0)), int(v.get("calls", 0)), int(v.get("input_tokens", 0)), int(v.get("output_tokens", 0)))
        out["unpriced"] += [m for m in src.get("unpriced") or [] if m not in out["unpriced"]]
    return out


def markdown(costs: dict[str, Any] | None, title: str = "Cost") -> list[str]:
    """A small table for a PR body or work log: one row per stage, then the models used."""
    if not costs or not costs.get("calls"):
        return []
    lines = [f"<details><summary>{title}: ${costs['total_usd']:.3f} over {costs['calls']} model call(s)</summary>", "",
             "| Stage | Cost | Calls | Tokens in / out |", "|---|---:|---:|---:|"]
    for stage in STAGES:
        b = costs["by_stage"].get(stage)
        if b:
            lines.append(f"| {STAGE_LABEL[stage]} | ${b['cost_usd']:.3f} | {b['calls']} | {b['input_tokens']:,} / {b['output_tokens']:,} |")
    lines += ["", "| Model | Cost | Calls |", "|---|---:|---:|"]
    for model, b in sorted(costs["by_model"].items(), key=lambda kv: -kv[1]["cost_usd"]):
        flag = " (unpriced)" if model in costs.get("unpriced", []) else ""
        lines.append(f"| `{model}`{flag} | ${b['cost_usd']:.3f} | {b['calls']} |")
    return [*lines, "", "</details>"]


def summarize(requests: list[dict[str, Any]], since: float = 0.0) -> dict[str, Any]:
    """The web cost view: totals per stage, model, repository, requester and day over requests created since `since`."""
    import time as _time

    rows = [r for r in requests if (r.get("created_at") or 0) >= since]
    total = empty()
    by_repo: dict[str, dict[str, Any]] = {}
    by_user: dict[str, dict[str, Any]] = {}
    by_day: dict[str, float] = {}
    for r in rows:
        c = r.get("costs") or {"total_usd": float(r.get("cost_usd") or 0.0), "calls": 0, "by_stage": {}, "by_model": {}}
        total = merge(total, c)
        usd = float(c.get("total_usd", 0.0))
        for d, k in ((by_repo, r.get("repo") or "?"), (by_user, r.get("requester_login") or "?")):
            b = d.setdefault(k, {"cost_usd": 0.0, "requests": 0})
            b["cost_usd"] = round(b["cost_usd"] + usd, 6)
            b["requests"] += 1
        day = _time.strftime("%Y-%m-%d", _time.gmtime(r.get("created_at") or 0))
        by_day[day] = round(by_day.get(day, 0.0) + usd, 6)
    top = sorted(rows, key=lambda r: -float((r.get("costs") or {}).get("total_usd", r.get("cost_usd") or 0.0)))[:10]
    return {
        **total,
        "requests": len(rows),
        "by_repo": by_repo,
        "by_requester": by_user,
        "by_day": [{"day": d, "cost_usd": v} for d, v in sorted(by_day.items())],
        "top": [{"id": r["id"], "repo": r.get("repo"), "task": r.get("task"), "state": r.get("state"), "pr_url": r.get("pr_url"),
                 "issue_url": r.get("issue_url"), "cost_usd": float((r.get("costs") or {}).get("total_usd", r.get("cost_usd") or 0.0)),
                 "by_stage": (r.get("costs") or {}).get("by_stage", {})} for r in top],
    }
