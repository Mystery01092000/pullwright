"""The runner's coding agent: Pullwright's own supervisor + workers, or an external agent (Claude Code, goose).

Pullwright stays the workflow around it (a "blueprint"): setup, the deterministic checks, the independent review,
commit, push and the PR. Only the agent steps, "implement the plan" and "fix these failures", are handed to the coder.

    PULLWRIGHT_CODER=native|claude-code|goose          the default for every repository (goose)
    PULLWRIGHT_CODER_BY_REPO='{"acme/app": "goose"}'   per repository
    PULLWRIGHT_CODER_MODEL=claude-sonnet-5-5           the external agent's model (default: the supervisor's)
    PULLWRIGHT_CODER_MAX_TURNS=120                     its turn limit per step

An external coder runs as a subprocess in the checkout and streams JSON, which becomes Pullwright events (agent
start and end, text, tool calls and results, usage), so the web UI's runner panel and the cost breakdown work as
with the native coder. It gets:
- the LLM proxy as its Anthropic endpoint and the job token as its key, so the job's dollar cap still holds;
- the organisation's and repository's skills under its own HOME (`~/.claude/skills`, `~/.agents/skills`), and the
  instructions in its prompt;
- no GitHub token in its environment, and guard commands first on PATH that refuse `git push`, `gh`, `pkill` and
  `killall`. The pipeline pushes and opens the PR itself, and killing by name would also kill the runner.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from . import pricing
from .events import Event
from .models import MAIN_MODEL, role_model
from .tools import AGENT_SCRUB_ENV

CODERS = ("native", "claude-code", "goose")
# goose is the default: in the 2026-11-13 comparisons it followed the org skills most closely (input bounds, explained
# results, more tests) at the same cost as Claude Code (docs/coders.md). Repositories can still pick another.
DEFAULT_CODER = "goose"
SCRUB_ENV = tuple(k for k in AGENT_SCRUB_ENV if k != "PULLWRIGHT_LLM_TOKEN")  # its job token becomes ANTHROPIC_API_KEY
GUARD = """#!/bin/sh
# Pullwright runner guard: {why}
{body}
"""
GUARDS = {
    "pkill": ("killing by name also matches the runner itself", 'echo "pkill is not allowed in a Pullwright runner. Stop a background process by its PID: cmd & pid=$!; ...; kill $pid" >&2; exit 1'),
    "killall": ("killing by name also matches the runner itself", 'echo "killall is not allowed in a Pullwright runner. Stop a background process by its PID: cmd & pid=$!; ...; kill $pid" >&2; exit 1'),
    "gh": ("the pipeline talks to GitHub itself", 'echo "gh is not available to agents in a Pullwright runner; the pipeline opens the PR." >&2; exit 1'),
    # find git's subcommand: skip global options, and the value after the ones that take one (-c k=v, -C dir, ...)
    "git": ("the pipeline pushes itself", 'skip=0\nfor a in "$@"; do\n  if [ "$skip" = 1 ]; then skip=0; continue; fi\n'
            '  case "$a" in\n    -c|-C|--git-dir|--work-tree|--namespace|--exec-path|--config-env|--super-prefix) skip=1;;\n'
            '    push) echo "git push is not allowed in a Pullwright runner; the pipeline pushes when the checks pass." >&2; exit 1;;\n'
            '    -*) ;;\n    *) break;;\n  esac\ndone\nexec "{real_git}" "$@"'),
}


def coder_for(repo: str, env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    try:
        by_repo = json.loads(env.get("PULLWRIGHT_CODER_BY_REPO") or "{}")
    except ValueError:
        by_repo = {}
    kind = (by_repo.get(repo) or env.get("PULLWRIGHT_CODER") or DEFAULT_CODER).strip().lower()
    if kind not in CODERS:
        raise ValueError(f"unknown coder {kind!r}; use one of {', '.join(CODERS)}")
    return kind


def _hint(name: str, inp: dict[str, Any]) -> str:
    for k in ("command", "file_path", "path", "pattern", "url", "description"):
        if isinstance(inp.get(k), str):
            return inp[k].splitlines()[0][:160] if inp[k] else ""
    return json.dumps(inp)[:160]


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text_of(c.get("text", c.get("content", ""))) if isinstance(c, dict) else str(c) for c in content)
    if isinstance(content, dict):
        return _text_of(content.get("text") or content.get("content") or "")
    return ""


def _usage(model: str, u: dict[str, Any]) -> dict[str, Any]:
    usage = {k: int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
    priced = pricing.price(model) is not None
    return {"model": model, **usage, "cost_usd": pricing.cost_usd(model, usage), "priced": priced}


class ClaudeStream:
    """`claude -p --output-format stream-json --verbose`: system init, assistant and user messages, a final result."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.final = ""
        self.stop = "end_turn"
        self._seen: set[str] = set()
        self._tools: dict[str, str] = {}

    def feed(self, obj: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
        t = obj.get("type")
        if t == "system" and obj.get("subtype") == "init":
            self.model = obj.get("model") or self.model
        elif t == "assistant":
            msg = obj.get("message") or {}
            self.model = msg.get("model") or self.model
            for block in msg.get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    yield "text", {"delta": block["text"]}
                elif block.get("type") == "thinking" and block.get("thinking"):
                    yield "thinking", {"delta": block["thinking"]}
                elif block.get("type") == "tool_use":
                    self._tools[block.get("id", "")] = block.get("name", "")
                    inp = block.get("input") or {}
                    yield "tool_call", {"tool_use_id": block.get("id"), "name": block.get("name"), "input": inp, "hint": _hint(block.get("name", ""), inp)}
            mid = msg.get("id") or ""
            if msg.get("usage") and mid not in self._seen:  # one line per content block, the same usage on each
                self._seen.add(mid)
                yield "usage", _usage(self.model, msg["usage"])
        elif t == "user":
            for block in (obj.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tid = block.get("tool_use_id", "")
                    yield "tool_result", {"tool_use_id": tid, "name": self._tools.get(tid, ""), "output": _text_of(block.get("content"))[:4000],
                                          "is_error": bool(block.get("is_error"))}
        elif t == "result":
            self.final = obj.get("result") or self.final
            self.stop = "end_turn" if obj.get("subtype") == "success" and not obj.get("is_error") else str(obj.get("subtype") or "error")


class GooseStream:
    """`goose run --output-format stream-json`: message events (text, toolRequest, toolResponse), then complete
    with the run's token totals. goose reports usage once, at the end; the LLM proxy enforces the cap meanwhile.

    `spent()` returns the job's spend by the proxy's count. When it is available, the step's cost is the proxy's
    figure (spend after minus spend before): goose's totals don't split cached from uncached input, and pricing them
    as plain input counted the quickcalc run at twice its real cost."""

    def __init__(self, model: str, spent: Callable[[], float | None] | None = None) -> None:
        self.model = model
        self.final = ""
        self.stop = "end_turn"
        self._text: list[str] = []
        self._tools: dict[str, str] = {}
        self._spent = spent
        self._start = spent() if spent else None

    def feed(self, obj: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
        t = obj.get("type")
        if t == "message":
            msg = obj.get("message") or {}
            for c in msg.get("content") or []:
                kind = c.get("type")
                if kind == "text" and msg.get("role") == "assistant" and c.get("text"):
                    self._text.append(c["text"])
                    yield "text", {"delta": c["text"]}
                elif kind == "thinking" and c.get("thinking"):
                    yield "thinking", {"delta": c["thinking"]}
                elif kind == "toolRequest":
                    call = (c.get("toolCall") or {}).get("value") or {}
                    name, inp = call.get("name", ""), call.get("arguments") or {}
                    self._tools[c.get("id", "")] = name
                    yield "tool_call", {"tool_use_id": c.get("id"), "name": name, "input": inp, "hint": _hint(name, inp)}
                elif kind == "toolResponse":
                    res = c.get("toolResult") or {}
                    value = res.get("value")
                    out = _text_of((value or {}).get("content") if isinstance(value, dict) else value) or _text_of(res.get("error"))
                    yield "tool_result", {"tool_use_id": c.get("id"), "name": self._tools.get(c.get("id", ""), ""), "output": out[:4000],
                                          "is_error": res.get("status") == "error"}
        elif t == "error":
            self.stop = "error"
            self.final = str(obj.get("error", ""))
        elif t == "complete":
            u = {"input_tokens": obj.get("input_tokens") or 0, "output_tokens": obj.get("output_tokens") or 0,
                 "cache_read_input_tokens": obj.get("cache_read_input_tokens") or 0,
                 "cache_creation_input_tokens": obj.get("cache_write_input_tokens") or 0}
            if not obj.get("input_tokens") and obj.get("total_tokens"):
                u["input_tokens"] = int(obj["total_tokens"])  # only a total: count it as input (the cheaper side)
            payload = _usage(self.model, u)
            now = self._spent() if (self._spent and self._start is not None) else None
            if now is not None:
                payload["cost_usd"], payload["cost_source"] = round(max(0.0, now - self._start), 6), "proxy"
            elif not payload["priced"] and obj.get("cost_usd"):
                payload["cost_usd"] = float(obj["cost_usd"])
            yield "usage", payload
            self.final = self.final or "".join(self._text)[-4000:]


class ExternalCoder:
    def __init__(self, kind: str, model: str | None = None, max_turns: int | None = None, timeout_s: int = 50 * 60,
                 env: dict[str, str] | None = None, exe: list[str] | None = None) -> None:
        if kind not in ("claude-code", "goose"):
            raise ValueError(f"not an external coder: {kind!r}")
        self.kind = kind
        self.exe = exe  # replaces the program name (tests use a stand-in)
        self.env = dict(os.environ if env is None else env)
        self.model = model or self.env.get("PULLWRIGHT_CODER_MODEL") or role_model("supervisor") or MAIN_MODEL
        self.model = self.model.split(":", 1)[1] if self.model.startswith("anthropic:") else self.model
        self.max_turns = max_turns or int(self.env.get("PULLWRIGHT_CODER_MAX_TURNS", "120"))
        self.timeout_s = timeout_s

    # --- environment ------------------------------------------------------------------------------

    def _home(self, base: Path, skills: dict[str, Path]) -> Path:
        """A HOME of its own: skills where the agent looks for them, guard commands, and its config."""
        home = base / f"{self.kind}-home"
        for d in (home / ".claude" / "skills", home / ".agents" / "skills", home / ".config" / "goose", home / "bin"):
            d.mkdir(parents=True, exist_ok=True)
        for name, folder in skills.items():
            for root in (home / ".claude" / "skills", home / ".agents" / "skills"):
                dest = root / name
                if not dest.exists():
                    shutil.copytree(folder, dest)
        real_git = shutil.which("git", path=self.env.get("PATH")) or "/usr/bin/git"
        for cmd, (why, body) in GUARDS.items():
            p = home / "bin" / cmd
            p.write_text(GUARD.format(why=why, body=body.replace("{real_git}", real_git)), encoding="utf-8")
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return home

    def proxy_spend(self) -> float | None:
        """The job's spend so far by the LLM proxy's count, or None when it can't be read."""
        proxy, token = self.env.get("PULLWRIGHT_LLM_PROXY", "").rstrip("/"), self.env.get("PULLWRIGHT_LLM_TOKEN", "")
        if not (proxy and token):
            return None
        try:
            import httpx

            r = httpx.get(f"{proxy}/spend", headers={"authorization": f"Bearer {token}"}, timeout=10)
            return float(r.json()["spent_usd"]) if r.status_code == 200 else None
        except Exception:
            return None

    def process_env(self, home: Path) -> dict[str, str]:
        env = {k: v for k, v in self.env.items() if k not in SCRUB_ENV}
        real_home = self.env.get("HOME") or str(Path.home())
        # keep the shared caches (warm runners) although HOME moves
        env.setdefault("XDG_CACHE_HOME", f"{real_home}/.cache")
        env.setdefault("npm_config_cache", f"{real_home}/.cache/npm")
        env.setdefault("UV_CACHE_DIR", f"{real_home}/.cache/uv")
        env.setdefault("GOPATH", f"{real_home}/go")
        env["HOME"] = str(home)
        env["PATH"] = f"{home / 'bin'}{os.pathsep}{self.env.get('PATH', '')}"
        proxy, token = self.env.get("PULLWRIGHT_LLM_PROXY", "").rstrip("/"), self.env.get("PULLWRIGHT_LLM_TOKEN", "")
        if proxy:
            env["ANTHROPIC_BASE_URL"] = env["ANTHROPIC_HOST"] = f"{proxy}/anthropic"
            env["ANTHROPIC_API_KEY"] = token
        if self.kind == "claude-code":
            env.update({"DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        else:
            env.update({"GOOSE_PROVIDER": "anthropic", "GOOSE_MODEL": self.model, "GOOSE_MODE": "auto",
                        "GOOSE_DISABLE_KEYRING": "1", "GOOSE_CLI_MIN_PRIORITY": "0.2"})
        return env

    def argv(self, prompt_file: Path) -> list[str]:
        if self.kind == "claude-code":
            args = ["-p", "--output-format", "stream-json", "--verbose", "--model", self.model,
                    "--max-turns", str(self.max_turns), "--permission-mode", "bypassPermissions",
                    "--disallowedTools", "Bash(git push:*)", "Bash(gh:*)", "Bash(pkill:*)", "Bash(killall:*)", "WebFetch", "WebSearch"]
            return [*(self.exe or ["claude"]), *args]
        args = ["run", "--instructions", str(prompt_file), "--no-session", "--no-profile", "--with-builtin", "developer",
                "--output-format", "stream-json", "--max-turns", str(self.max_turns), "--provider", "anthropic", "--model", self.model]
        return [*(self.exe or ["goose"]), *args]

    # --- the run ----------------------------------------------------------------------------------

    def run(self, session: Any, prompt: str, agent_id: str, skills: dict[str, Path] | None = None,
            instructions: str = "") -> str:
        """Run one step to completion; its events go on the session's bus. Returns the agent's final report."""
        full = prompt if not instructions else f"{instructions}\n\n---\n\n{prompt}"
        with tempfile.TemporaryDirectory(prefix=f"pw-{self.kind}-") as tmp:
            base = Path(tmp)
            home = self._home(base, skills or {})
            prompt_file = base / "task.md"
            prompt_file.write_text(full, encoding="utf-8")
            parser: ClaudeStream | GooseStream = (ClaudeStream(self.model) if self.kind == "claude-code"
                                                  else GooseStream(self.model, self.proxy_spend if self.env.get("PULLWRIGHT_LLM_PROXY") else None))

            def emit(type_: str, payload: dict[str, Any]) -> None:
                session.bus.emit(Event(type_, session.run_id, agent_id, payload))

            emit("agent_start", {"name": self.kind, "model": self.model, "effort": "", "tools": []})
            stdin = open(prompt_file, encoding="utf-8") if self.kind == "claude-code" else subprocess.DEVNULL  # noqa: SIM115
            try:
                proc = subprocess.Popen(self.argv(prompt_file), cwd=session.cwd, env=self.process_env(home), stdin=stdin,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
            except FileNotFoundError as e:
                emit("agent_end", {"stop": "error"})
                raise RuntimeError(f"{self.kind} is not installed in this runner image ({e})") from e
            finally:
                if stdin is not subprocess.DEVNULL:
                    stdin.close()
            stderr: list[str] = []
            drain = threading.Thread(target=lambda: stderr.extend(proc.stderr), daemon=True)  # type: ignore[arg-type]
            drain.start()
            timer = threading.Timer(self.timeout_s, proc.kill)
            timer.start()
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    for type_, payload in parser.feed(obj):
                        emit(type_, payload)
                    if session.cancelled.is_set():
                        proc.kill()
                code = proc.wait()
            finally:
                timer.cancel()
                drain.join(timeout=5)
            stop = parser.stop if code == 0 else (parser.stop if parser.stop != "end_turn" else f"exit {code}")
            emit("agent_end", {"stop": stop})
        session.check_cancel()
        session.check_budget()
        if code != 0 and not parser.final:
            raise RuntimeError(f"{self.kind} exited with {code}: {''.join(stderr)[-1500:]}")
        return parser.final or f"({self.kind} finished with exit code {code})"
