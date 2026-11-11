"""Per-repository configuration from <repo>/.pullwright/ (or the older .harness/), layered over the defaults
shipped in pullwright/defaults/.

.pullwright/
  agents/<name>.md      frontmatter (model, effort, tools, max_turns, max_cost_usd) + system prompt body
  instructions.md       appended to every agent's system prompt (the CLAUDE.md of this repo for the agents)
  guardrails.json       {"deny": [regex], "ask": [regex], "allow": [regex], "bash_timeout": 120}
  hooks.json            [{"event": "pre_tool|post_tool|state", "match": regex, "command": "shell"}]
  skills/<name>/SKILL.md  frontmatter (description) + instructions; agents load them on demand
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULTS = Path(__file__).parent / "defaults"
CONFIG_DIRS = (".pullwright", ".harness")  # first found wins; .harness is the pre-rename name


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """`---\\nkey: value\\n---\\nbody` -> ({key: value}, body). No YAML dependency: one scalar per line."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, m.group(2)


@dataclass
class AgentSpec:
    name: str
    system: str
    model: str | None = None
    effort: str | None = None
    tools: list[str] | None = None
    max_turns: int | None = None
    max_cost_usd: float | None = None
    source: str = "default"

    @classmethod
    def from_file(cls, path: Path, source: str) -> "AgentSpec":
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        tools = [t.strip() for t in meta["tools"].split(",") if t.strip()] if "tools" in meta else None
        return cls(
            name=path.stem,
            system=body.strip(),
            model=meta.get("model") or None,
            effort=meta.get("effort") or None,
            tools=tools,
            max_turns=int(meta["max_turns"]) if meta.get("max_turns") else None,
            max_cost_usd=float(meta["max_cost_usd"]) if meta.get("max_cost_usd") else None,
            source=source,
        )


@dataclass
class Skill:
    name: str
    description: str
    path: Path  # the SKILL.md

    def body(self) -> str:
        _, body = parse_frontmatter(self.path.read_text(encoding="utf-8"))
        files = sorted(p.relative_to(self.path.parent).as_posix() for p in self.path.parent.rglob("*") if p.is_file() and p != self.path)
        return body.strip() + (f"\n\nFiles in this skill: {', '.join(files)}" if files else "")


@dataclass
class Hook:
    event: str  # pre_tool | post_tool | state
    command: str
    match: str = ".*"  # regex against the tool name (tool hooks) or the state name


@dataclass
class HarnessConfig:
    root: Path | None  # the .pullwright (or .harness) dir, if present
    agents: dict[str, AgentSpec]
    instructions: str = ""
    guardrails: dict[str, Any] = field(default_factory=dict)
    hooks: list[Hook] = field(default_factory=list)
    skills: dict[str, Skill] = field(default_factory=dict)

    def agent(self, name: str) -> AgentSpec:
        if name not in self.agents:
            raise KeyError(f"no agent definition for {name!r}; add .pullwright/agents/{name}.md")
        return self.agents[name]

    def system_prompt(self, name: str) -> str:
        """Agent prompt + project instructions + skill index."""
        parts = [self.agent(name).system]
        if self.instructions:
            parts.append(f"# Project instructions\n{self.instructions}")
        if self.skills:
            index = "\n".join(f"- {s.name}: {s.description}" for s in self.skills.values())
            parts.append(f"# Skills\nThese skills are available. Call load_skill(name) to read one before doing related work.\n{index}")
        return "\n\n".join(parts)

    def run_hooks(self, event: str, key: str, payload: dict[str, Any], cwd: Path) -> str | None:
        """Run matching hooks with the payload as JSON on stdin. For pre_tool, a non-zero exit blocks the call;
        the returned string is the reason. Other events are fire-and-forget."""
        for h in self.hooks:
            if h.event != event or not re.search(h.match, key):
                continue
            try:
                r = subprocess.run(h.command, shell=True, cwd=cwd, input=json.dumps(payload), capture_output=True, text=True, timeout=30)
            except subprocess.TimeoutExpired:
                if event == "pre_tool":
                    return f"hook timed out: {h.command}"
                continue
            if event == "pre_tool" and r.returncode != 0:
                return (r.stderr or r.stdout).strip() or f"blocked by hook: {h.command}"
        return None


def _load_agents(d: Path, source: str) -> dict[str, AgentSpec]:
    if not d.is_dir():
        return {}
    return {p.stem: AgentSpec.from_file(p, source) for p in sorted(d.glob("*.md"))}


def _load_skills(d: Path) -> dict[str, Skill]:
    out: dict[str, Skill] = {}
    if d.is_dir():
        for md in sorted(d.glob("*/SKILL.md")):
            meta, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
            out[md.parent.name] = Skill(md.parent.name, meta.get("description", ""), md)
    return out


def load_config(cwd: Path) -> HarnessConfig:
    root = next((cwd / d for d in CONFIG_DIRS if (cwd / d).is_dir()), cwd / CONFIG_DIRS[0])
    agents = {**_load_agents(DEFAULTS / "agents", "default"), **_load_agents(root / "agents", "repo")}
    cfg = HarnessConfig(root=root if root.is_dir() else None, agents=agents)
    if cfg.root is None:
        return cfg
    if (root / "instructions.md").is_file():
        cfg.instructions = (root / "instructions.md").read_text(encoding="utf-8").strip()
    if (root / "guardrails.json").is_file():
        cfg.guardrails = json.loads((root / "guardrails.json").read_text(encoding="utf-8"))
    if (root / "hooks.json").is_file():
        cfg.hooks = [Hook(**h) for h in json.loads((root / "hooks.json").read_text(encoding="utf-8"))]
    cfg.skills = _load_skills(root / "skills")
    return cfg
