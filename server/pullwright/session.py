"""What every pipeline shares: building agents from the repo config, the tool context, budgets and spawn_worker.

The local chat flow (lifecycle.Run), the planning pipeline (pipeline.plan) and the runner's work pipeline
(pipeline.work) each own a Session, so an agent is built, sandboxed, budgeted and traced the same way in all three.
"""
from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent import AgentDef, RunResult, run_agent
from .config import HarnessConfig, load_config
from .events import Event, EventBus
from .models import MAIN_MODEL, WORKER_MODEL, role_model
from .tools import TOOLS, ToolContext, ToolError


class Cancelled(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Session:
    cwd: Path
    client: Any
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    bus: EventBus = field(default_factory=EventBus)
    cfg: HarnessConfig | None = None  # loaded from <cwd>/.pullwright unless injected
    main_model: str = MAIN_MODEL
    worker_model: str = WORKER_MODEL
    max_cost_usd: float = 5.0
    approve: Callable[[str], bool] = lambda _cmd: False  # commands matching the guardrails' "ask" list
    extra_deny: list[str] = field(default_factory=list)  # appended to the repo's deny list (e.g. git push in a runner)
    cancelled: threading.Event = field(default_factory=threading.Event)
    cost_usd: float = 0.0
    state_hooks: bool = True  # run `state` hooks; lifecycle.Run fires its own
    _workers: int = 0

    def __post_init__(self) -> None:
        self.cfg = self.cfg or load_config(self.cwd)
        self.bus.subscribe(self._track_cost)
        if self.state_hooks and any(h.event == "state" for h in self.cfg.hooks):
            self.bus.subscribe(self._state_hooks)

    def _state_hooks(self, ev: Event) -> None:
        """`state` hooks in .pullwright/hooks.json run on every pipeline state (describing, working, checking, green...)."""
        if ev.type == "state" and ev.agent_id == "run":
            self.cfg.run_hooks("state", ev.payload.get("state", ""), {"run_id": self.run_id, **ev.payload}, self.cwd)

    def _track_cost(self, ev: Event) -> None:
        if ev.type == "usage":
            self.cost_usd += ev.payload["cost_usd"]

    def emit(self, type_: str, **payload: Any) -> None:
        self.bus.emit(Event(type_, self.run_id, "run", payload))

    def check_cancel(self) -> None:
        if self.cancelled.is_set():
            raise Cancelled("cancelled by user")

    def check_budget(self) -> None:
        if self.cost_usd > self.max_cost_usd:
            raise BudgetExceeded(f"cost ${self.cost_usd:.2f} exceeds cap ${self.max_cost_usd:.2f}")

    def ctx(self) -> ToolContext:
        g = self.cfg.guardrails
        hooks = self.cfg.hooks

        def pre(name: str, inp: dict[str, Any]) -> str | None:
            return self.cfg.run_hooks("pre_tool", name, {"run_id": self.run_id, "tool": name, "input": inp}, self.cwd)

        def post(name: str, inp: dict[str, Any], out: str, err: bool) -> None:
            self.cfg.run_hooks("post_tool", name, {"run_id": self.run_id, "tool": name, "input": inp, "output": out[:4000], "is_error": err}, self.cwd)

        deny = g.get("deny")
        if self.extra_deny:
            deny = [*(deny or _default_deny()), *self.extra_deny]
        return ToolContext(
            root=self.cwd,
            approve=self.approve,
            bash_timeout=int(g.get("bash_timeout", 120)),
            deny=deny,
            ask=g.get("ask"),
            allow=g.get("allow"),
            pre_tool=pre if hooks else None,
            post_tool=post if hooks else None,
        )

    def _load_skill_tool(self) -> tuple[dict[str, Any], Any]:
        schema = {
            "description": "Read the full instructions of a skill listed in the system prompt.",
            "strict": True,
            "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": False},
        }

        def load(ctx: ToolContext, name: str) -> str:
            skill = self.cfg.skills.get(name)
            if skill is None:
                raise ToolError(f"unknown skill {name!r}; available: {', '.join(self.cfg.skills) or 'none'}")
            return skill.body()

        return schema, load

    def agent(self, role: str, name: str | None = None, **kw: Any) -> AgentDef:
        """An AgentDef for a role from the repo/default definition, plus project instructions, skills and run-level models."""
        spec = self.cfg.agent(role)
        extra: dict[str, Any] = dict(kw.pop("extra_tools", {}))
        if self.cfg.skills:
            extra["load_skill"] = self._load_skill_tool()
        tools = list(TOOLS) if spec.tools is None else list(spec.tools)  # None = every tool, including MCP ones
        tools += [t for t in extra if t not in tools]
        return AgentDef(
            name=name or role,
            system=self.cfg.system_prompt(role),
            model=spec.model or role_model(role) or (self.worker_model if role == "worker" else self.main_model),
            effort=spec.effort or "high",
            tools=tools,
            max_turns=spec.max_turns or 50,
            max_cost_usd=min(spec.max_cost_usd or self.max_cost_usd, self.max_cost_usd),
            extra_tools=extra,
            **kw,
        )

    def run(self, agent: AgentDef, prompt: str, *, parent: str | None = None, agent_id: str | None = None) -> RunResult:
        self.check_cancel()
        self.check_budget()
        res = run_agent(self.client, agent, [{"role": "user", "content": prompt}], self.ctx(), self.bus,
                        run_id=self.run_id, agent_id=agent_id, parent_id=parent, cancel=self.cancelled)
        self.check_cancel()
        return res

    def spawn_worker_tool(self, parent: Callable[[], str | None]) -> dict[str, Any]:
        """The supervisor's spawn_worker tool. `parent` returns the supervisor's agent id at call time."""
        schema = {
            "description": "Run a worker agent with fresh context on one well-specified task. Returns the worker's final report.",
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Exactly what to do, with acceptance criteria"},
                    "context": {"type": "string", "description": "Relevant files, conventions, and constraints the worker must know"},
                },
                "required": ["task", "context"],
                "additionalProperties": False,
            },
        }

        def spawn(ctx: ToolContext, task: str, context: str) -> str:
            self._workers += 1
            worker = self.agent("worker", name=f"worker{self._workers}")
            res = self.run(worker, f"Task:\n{task}\n\nContext:\n{context}", parent=parent())
            return f"[worker stop={res.stop} cost=${res.cost_usd:.3f}]\n{res.text}"

        return {"spawn_worker": (schema, spawn)}


def _default_deny() -> list[str]:
    from .tools import DENY  # the built-in list ToolContext uses when deny is None

    return list(DENY)
