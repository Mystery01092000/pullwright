"""Per-repository configuration from <repo>/.pullwright/ (or the older .harness/), layered over the defaults
shipped in pullwright/defaults/.

.pullwright/
  agents/<name>.md      frontmatter (model, effort, tools, max_turns, max_cost_usd) + system prompt body
  instructions.md       appended to every agent's system prompt (the CLAUDE.md of this repo for the agents)
  guardrails.json       {"deny": [regex], "ask": [regex], "allow": [regex], "bash_timeout": 120}
  hooks.json            [{"event": "pre_tool|post_tool|state", "match": regex, "command": "shell"}]
  skills/<name>/SKILL.md  frontmatter (description) + instructions; agents load them on demand

An organisation-wide layer sits between the built-in defaults and the repository (pullwright.orgconfig fetches it
from a shared repository, PULLWRIGHT_ORG_CONFIG). It is read in the same layout, or in Claude Code's (`.claude/agents`,
`.claude/skills`, hooks in `.claude/settings.json`). Layering: defaults < org < repo for agents, skills and
instructions; deny/ask/allow lists and hooks are COMBINED, so a repository cannot drop the organisation's rules.
Agents whose name is not a Pullwright role (describer, verifier, supervisor, worker, diagnosis, ...) become
specialists the supervisor can hand a task to.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULTS = Path(__file__).parent / "defaults"
CONFIG_DIRS = (".pullwright", ".harness")  # first found wins; .harness is the pre-rename name
ROLES = frozenset({"describer", "verifier", "supervisor", "worker", "diagnosis", "intake", "summarizer", "responder"})
# Claude Code tool names -> Pullwright's, so agents and hooks written for Claude Code work here
CLAUDE_TOOLS = {"Read": "read_file", "Write": "write_file", "Edit": "edit_file", "MultiEdit": "edit_file",
                "Glob": "glob", "Grep": "grep", "Bash": "bash", "LS": "glob"}
CLAUDE_MODELS = {"sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5", "opus": "claude-opus-5-5", "fable": "claude-fable-5-2"}
CLAUDE_EVENTS = {"PreToolUse": "pre_tool", "PostToolUse": "post_tool"}


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
    description: str = ""

    @classmethod
    def from_file(cls, path: Path, source: str) -> AgentSpec:
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        tools = None
        if "tools" in meta:  # present but empty = no tools at all (the summarizer); absent = every tool
            names = [CLAUDE_TOOLS.get(t.strip(), t.strip()) for t in meta["tools"].split(",") if t.strip()]
            tools = list(dict.fromkeys(names))  # Claude Code names mapped, duplicates (Edit + MultiEdit) dropped
        model = meta.get("model") or None
        if model == "inherit":
            model = None
        model = CLAUDE_MODELS.get(model or "", model)
        return cls(
            name=meta.get("name") or path.stem,
            system=body.strip(),
            description=meta.get("description", ""),
            model=model,
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
    source: str = "repo"  # repo | org
    block_exit: int | None = None  # pre_tool: None = any non-zero exit blocks; 2 = Claude Code's convention
    root: str = ""  # the config folder it came from (exported to the hook as PULLWRIGHT_CONFIG_DIR)


@dataclass
class HarnessConfig:
    root: Path | None  # the .pullwright (or .harness) dir, if present
    agents: dict[str, AgentSpec]
    instructions: str = ""
    guardrails: dict[str, Any] = field(default_factory=dict)
    hooks: list[Hook] = field(default_factory=list)
    skills: dict[str, Skill] = field(default_factory=dict)
    org_instructions: str = ""
    org_root: Path | None = None  # the organisation layer's folder, when one is configured
    specialists: dict[str, AgentSpec] = field(default_factory=dict)  # non-role agents the supervisor can delegate to

    def agent(self, name: str) -> AgentSpec:
        if name not in self.agents:
            raise KeyError(f"no agent definition for {name!r}; add .pullwright/agents/{name}.md")
        return self.agents[name]

    def system_prompt(self, name: str) -> str:
        """Agent prompt + project instructions + skill index."""
        parts = [self.agent(name).system]
        if self.org_instructions:
            parts.append(f"# Organisation instructions\n{self.org_instructions}")
        if self.instructions:
            parts.append(f"# Project instructions\n{self.instructions}")
        if self.skills:
            index = "\n".join(f"- {s.name}: {s.description}" for s in self.skills.values())
            parts.append(f"# Skills\nThese skills are available. Call load_skill(name) to read one before doing related work.\n{index}")
        if name == "supervisor" and self.specialists:
            index = "\n".join(f"- {s.name}: {s.description or 'no description'}" for s in self.specialists.values())
            parts.append("# Specialists\nBesides workers, these specialist agents are available through spawn_specialist(agent, task, context). "
                         f"Use one when a task matches its description.\n{index}")
        return "\n\n".join(parts)

    def specialist_prompt(self, name: str) -> str:
        spec = self.specialists[name]
        parts = [spec.system]
        if self.org_instructions:
            parts.append(f"# Organisation instructions\n{self.org_instructions}")
        if self.instructions:
            parts.append(f"# Project instructions\n{self.instructions}")
        return "\n\n".join(parts)

    def run_hooks(self, event: str, key: str, payload: dict[str, Any], cwd: Path) -> str | None:
        """Run matching hooks with the payload as JSON on stdin. For pre_tool, a non-zero exit blocks the call;
        the returned string is the reason. Other events are fire-and-forget."""
        claude_event = {"pre_tool": "PreToolUse", "post_tool": "PostToolUse"}.get(event, event)
        compat = {"hook_event_name": claude_event, "cwd": str(cwd)}  # Claude Code's field names, for hooks written for it
        if "tool" in payload:
            compat.update(tool_name=payload["tool"], tool_input=payload.get("input", {}))
        for h in self.hooks:
            if h.event != event or not re.search(h.match, key):
                continue
            own = {"PULLWRIGHT_CONFIG_DIR": h.root, "PULLWRIGHT_PROJECT_DIR": str(cwd), "CLAUDE_PROJECT_DIR": str(cwd)}
            env = {**os.environ, **own}
            command = h.command
            for k, v in own.items():  # substituted here too, so hooks also work where the shell does not expand $VARS (cmd.exe)
                command = command.replace("${" + k + "}", v).replace("$" + k, v)
            try:
                r = subprocess.run(command, shell=True, cwd=cwd, input=json.dumps({**compat, **payload}), capture_output=True,
                                   text=True, timeout=30, env=env)
            except subprocess.TimeoutExpired:
                if event == "pre_tool":
                    return f"hook timed out: {h.command}"
                continue
            blocked = r.returncode != 0 if h.block_exit is None else r.returncode == h.block_exit
            if event == "pre_tool" and blocked:
                who = "organisation hook" if h.source == "org" else "hook"
                return (r.stderr or r.stdout).strip() or f"blocked by {who}: {h.command}"
        return None


def _load_agents(d: Path, source: str) -> dict[str, AgentSpec]:
    if not d.is_dir():
        return {}
    specs = [AgentSpec.from_file(p, source) for p in sorted(d.glob("*.md"))]
    return {s.name: s for s in specs}


def _load_skills(d: Path) -> dict[str, Skill]:
    out: dict[str, Skill] = {}
    if d.is_dir():
        for md in sorted(d.glob("*/SKILL.md")):
            meta, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
            name = meta.get("name") or md.parent.name
            out[name] = Skill(name, meta.get("description", ""), md)
    return out


def _claude_tool_pattern(matcher: str) -> str:
    """A Claude Code hook matcher ("Bash", "Edit|Write", "", "*") as a regex over Pullwright's tool names."""
    if matcher in ("", "*", ".*"):
        return ".*"
    names = [CLAUDE_TOOLS.get(m.strip(), m.strip()) for m in matcher.split("|") if m.strip()]
    return "^(" + "|".join(re.escape(n) for n in dict.fromkeys(names)) + ")$"


def _load_hooks(root: Path, source: str) -> list[Hook]:
    hooks: list[Hook] = []
    for f in (root / "hooks.json", root / "hooks" / "hooks.json"):
        if f.is_file():
            hooks += [Hook(**{**h, "source": source, "root": str(root)}) for h in json.loads(f.read_text(encoding="utf-8"))]
    settings = root / "settings.json"
    if settings.is_file():  # Claude Code: {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "..."}]}]}}
        data = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
        for event, groups in data.items():
            if event not in CLAUDE_EVENTS:
                continue  # session and notification events have no Pullwright equivalent
            for g in groups:
                for h in g.get("hooks", []):
                    if h.get("type", "command") == "command" and h.get("command"):
                        hooks.append(Hook(CLAUDE_EVENTS[event], h["command"], _claude_tool_pattern(g.get("matcher", "")),
                                          source=source, block_exit=2, root=str(root)))
    return hooks


@dataclass
class Layer:
    root: Path
    agents: dict[str, AgentSpec]
    skills: dict[str, Skill]
    instructions: str
    guardrails: dict[str, Any]
    hooks: list[Hook]


def read_layer(path: Path, source: str) -> Layer:
    """One config folder: `.pullwright/` (or `.harness/`), Claude Code's `.claude/`, or the folder itself."""
    root = next((path / d for d in (*CONFIG_DIRS, ".claude") if (path / d).is_dir()), path)
    instructions = ""
    for f in (root / "instructions.md", path / "instructions.md", root / "CLAUDE.md", path / "CLAUDE.md", path / "AGENTS.md"):
        if f.is_file():
            instructions = f.read_text(encoding="utf-8").strip()
            break
    guardrails = json.loads((root / "guardrails.json").read_text(encoding="utf-8")) if (root / "guardrails.json").is_file() else {}
    return Layer(root, _load_agents(root / "agents", source), _load_skills(root / "skills"), instructions, guardrails, _load_hooks(root, source))


def _merge_guardrails(org: dict[str, Any], repo: dict[str, Any]) -> dict[str, Any]:
    out = {**org, **repo}
    for k in ("deny", "ask", "allow"):
        if k in org or k in repo:
            out[k] = list(dict.fromkeys([*(org.get(k) or []), *(repo.get(k) or [])]))
    if "bash_timeout" in org and "bash_timeout" in repo:
        out["bash_timeout"] = min(int(org["bash_timeout"]), int(repo["bash_timeout"]))
    return out


def org_config_dir() -> Path | None:
    from . import orgconfig

    return orgconfig.current_dir()


def load_config(cwd: Path, org_dir: Path | None = None) -> HarnessConfig:
    root = next((cwd / d for d in CONFIG_DIRS if (cwd / d).is_dir()), cwd / CONFIG_DIRS[0])
    org_dir = org_dir if org_dir is not None else org_config_dir()
    org = read_layer(org_dir, "org") if org_dir is not None and org_dir.is_dir() else None
    defaults = _load_agents(DEFAULTS / "agents", "default")
    repo_agents = _load_agents(root / "agents", "repo")
    all_agents = {**defaults, **(org.agents if org else {}), **repo_agents}
    agents = {k: v for k, v in all_agents.items() if k in defaults or k in ROLES}
    cfg = HarnessConfig(root=root if root.is_dir() else None, agents=agents,
                        specialists={k: v for k, v in all_agents.items() if k not in agents})
    if org is not None:
        cfg.org_root, cfg.org_instructions, cfg.guardrails = org.root, org.instructions, dict(org.guardrails)
        cfg.hooks, cfg.skills = list(org.hooks), dict(org.skills)
    if cfg.root is None:
        return cfg
    if (root / "instructions.md").is_file():
        cfg.instructions = (root / "instructions.md").read_text(encoding="utf-8").strip()
    if (root / "guardrails.json").is_file():
        cfg.guardrails = _merge_guardrails(cfg.guardrails, json.loads((root / "guardrails.json").read_text(encoding="utf-8")))
    if (root / "hooks.json").is_file():
        cfg.hooks += [Hook(**{**h, "source": "repo", "root": str(root)}) for h in json.loads((root / "hooks.json").read_text(encoding="utf-8"))]
    cfg.skills.update(_load_skills(root / "skills"))
    return cfg
