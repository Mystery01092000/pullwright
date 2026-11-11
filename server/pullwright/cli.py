"""Terminal front end for pullwright.

    uv run python -m pullwright.cli "add a hello() function with a test" --cwd ../my-repo
    uv run python -m pullwright.cli "fix the flaky test" --cwd ../my-repo --worker     # one worker, no lifecycle
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import anthropic
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .agent import AgentDef, run_agent
from .config import load_config
from .events import Event, EventBus
from .lifecycle import Run, thread_context
from .models import MAIN_MODEL, WORKER_MODEL
from .tools import ToolContext

console = Console(highlight=False)

STATE_LABEL = {
    "intake": "Understanding the request",
    "awaiting_confirmation": "Waiting for your go-ahead",
    "working": "Working",
    "diagnosing": "Verifying the result",
    "summarizing": "Writing the summary",
    "awaiting_pr_decision": "Ready",
    "done": "Done",
    "failed": "Failed",
    "cancelled": "Cancelled",
}


def _brief(name: str, inp: dict[str, Any]) -> str:
    match name:
        case "read_file":
            return f"Read {inp.get('path')}"
        case "write_file":
            return f"Write {inp.get('path')}"
        case "edit_file":
            return f"Edit {inp.get('path')}"
        case "glob":
            return f"Find {inp.get('pattern')}"
        case "grep":
            return f"Search /{inp.get('pattern')}/ in {inp.get('path')}"
        case "bash":
            lines = str(inp.get("command", "")).splitlines() or [""]
            return f"$ {lines[0]}" + (f"  … +{len(lines) - 1} lines" if len(lines) > 1 else "")
        case "spawn_worker":
            return "Delegate: " + str(inp.get("task", "")).splitlines()[0]
        case "load_skill":
            return f"Load skill {inp.get('name')}"
        case _:
            return f"{name} {json.dumps(inp)[:120]}"


class Printer:
    """Renders the event stream. Thinking is dim and streamed; tool steps are one line each; text streams plainly."""

    def __init__(self) -> None:
        self.depth: dict[str, int] = {}
        self.names: dict[str, str] = {}
        self.cost: dict[str, float] = {}
        self.models: dict[str, str] = {}
        self.streaming: str | None = None  # "thinking" | "text" | None, to place newlines between modes

    def _indent(self, ev: Event) -> str:
        return "  " * self.depth.get(ev.agent_id, 0)

    def _break(self, mode: str | None) -> None:
        if self.streaming and self.streaming != mode:
            console.print()
        self.streaming = mode

    def __call__(self, ev: Event) -> None:
        p = ev.payload
        ind = self._indent(ev)
        match ev.type:
            case "state":
                self._break(None)
                if p["state"] not in ("awaiting_confirmation", "awaiting_pr_decision"):
                    console.print(Rule(Text(STATE_LABEL.get(p["state"], p["state"]), style="bold"), style="grey42"))
            case "agent_start":
                self._break(None)
                self.depth[ev.agent_id] = (self.depth.get(ev.parent_id or "", -1) + 1) if ev.parent_id else 0
                self.names[ev.agent_id] = p["name"]
                self.models[ev.agent_id] = p["model"]
                console.print(f"{ind}[bold]● {p['name']}[/]  [grey50]{p['model'].removeprefix('claude-')} · {p['effort']}[/]")
            case "thinking":
                if self.streaming != "thinking":
                    self._break("thinking")
                    console.print(f"{ind}  ", end="")
                console.print(Text(p["delta"], style="grey42 italic"), end="", soft_wrap=True)
            case "text":
                if self.names.get(ev.agent_id) in ("intake", "diagnosis", "summarizer"):
                    return  # structured JSON or the summary: rendered as a panel instead
                if self.streaming != "text":
                    self._break("text")
                    console.print(f"{ind}  ", end="")
                console.print(Text(p["delta"]), end="", soft_wrap=True)
            case "tool_call":
                self._break(None)
                console.print(f"{ind}  [cyan]▸[/] [cyan]{_brief(p['name'], p['input'])}[/]", soft_wrap=True)
            case "tool_result":
                first = (p["output"].splitlines() or [""])[0]
                if p["is_error"]:
                    console.print(f"{ind}    [red]✕ {first[:160]}[/]", soft_wrap=True)
                elif p["name"] != "spawn_worker":
                    console.print(f"{ind}    [grey42]{first[:120]}[/]", soft_wrap=True)
            case "usage":
                self.cost[ev.agent_id] = p["total_cost_usd"]
            case "error":
                self._break(None)
                console.print(f"{ind}  [red]! {p.get('reason') or p}[/]")
            case "agent_end":
                self._break(None)
                mark = "[green]✓[/]" if p["stop"] == "end_turn" else f"[yellow]■ {p['stop']}[/]"
                console.print(f"{ind}{mark} [bold]{self.names.get(ev.agent_id, ev.agent_id)}[/]  [grey50]${p['cost_usd']:.4f} · {p['input_tokens'] // 1000}k in · {p['output_tokens']} out · {p['cache_read_input_tokens'] // 1000}k cached[/]")
            case "branch":
                console.print(f"[grey50]branch {p['branch']}[/]")
            case "diagnosis":
                self._break(None)
                if p["complete"]:
                    console.print("[green]✓ Verified complete[/]")
                else:
                    console.print("[yellow]! Gaps found:[/]\n" + "\n".join(f"  - {g}" for g in p["gaps"]))
            case "pr":
                console.print(f"[green]PR: {p['url']}[/]")


class TerminalIO:
    def confirm_intake(self, intake: dict[str, Any]) -> tuple[bool, str]:
        console.print()
        body = intake["understanding"] + "\n\n**Plan**\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(intake["plan"]))
        if intake["questions"]:
            body += "\n\n**Questions**\n" + "\n".join(f"- {q}" for q in intake["questions"])
        console.print(Panel(Markdown(body), title="What I understood", border_style="cyan", padding=(1, 2)))
        ans = Prompt.ask("[bold]Proceed?[/] [grey50](y, or type what to change)[/]", default="y").strip()
        console.print()
        return (True, "") if ans.lower() in ("y", "yes") else (False, ans)

    def approve_command(self, command: str) -> bool:
        console.print(Panel(Text(command, style="bold"), title="Allow this command?", border_style="yellow"))
        ok = Confirm.ask("Allow", default=False)
        console.print()
        return ok

    def want_pr(self, summary: str) -> bool:
        console.print(Panel(Markdown(summary.split("\nTITLE:")[0]), title="Summary", border_style="green", padding=(1, 2)))
        ok = Confirm.ask("Open a pull request", default=False)
        console.print()
        return ok


def _report(run: Run, printer: Printer) -> None:
    t = Table(title="Cost", title_style="bold", header_style="grey50", box=None, padding=(0, 2))
    t.add_column("agent")
    t.add_column("model")
    t.add_column("usd", justify="right")
    for aid, c in printer.cost.items():
        t.add_row(printer.names.get(aid, aid), printer.models.get(aid, "").removeprefix("claude-"), f"{c:.4f}")
    t.add_row("[bold]total[/]", "", f"[bold]{run.cost_usd:.4f}[/]")
    console.print()
    console.print(t)
    color = {"done": "green", "failed": "red", "cancelled": "yellow"}.get(run.state, "white")
    lines = [f"[{color}]{STATE_LABEL.get(run.state, run.state)}[/]"]
    if run.branch:
        lines.append(f"branch  {run.branch}" + (f"  @ {run.commit}" if run.commit else "  (nothing committed)"))
    if run.pr_url:
        lines.append(f"PR      {run.pr_url}")
    console.print(Panel("\n".join(lines), border_style=color))


HELP = """[bold]Type a task[/] and press Enter. Intake proposes a plan, you approve it, the agents do the work on a branch.
After a run finishes, the next message [bold]continues the same thread[/] (same branch, with the history as context).

  /new    start a new thread (next task gets its own branch)
  /cost   spend so far in this session
  /help   this text
  /quit   leave (Ctrl+D or Ctrl+C twice also works)"""


class Session:
    """An interactive session: a sequence of runs in the same repo, grouped into threads like the UI."""

    def __init__(self, cwd: Path, client: Any, main_model: str, worker_model: str, max_cost: float) -> None:
        self.cwd, self.client = cwd, client
        self.main_model, self.worker_model, self.max_cost = main_model, worker_model, max_cost
        self.cfg = load_config(cwd)
        self.io = TerminalIO()
        self.history: list[Run] = []  # runs of the current thread
        self.spent = 0.0

    def new_thread(self) -> None:
        self.history.clear()

    def run(self, task: str) -> Run:
        printer = Printer()
        bus = EventBus()
        bus.subscribe(printer)
        prev = self.history[-1] if self.history else None
        run = Run(
            task=task, cwd=self.cwd, io=self.io, client=self.client, bus=bus, cfg=self.cfg,
            main_model=self.main_model, worker_model=self.worker_model, max_cost_usd=self.max_cost,
            thread_id=prev.thread_id if prev else "",
            branch=prev.branch if prev else "",
            prior_context=thread_context([{"task": r.task, "state": r.state, "branch": r.branch, "diagnosis": r.diagnosis, "summary": r.summary} for r in self.history]) if prev else "",
        )
        try:
            run.run()
        except KeyboardInterrupt:
            run.cancel()
            console.print("\n[yellow]Interrupted.[/]")
        except Exception as e:  # state is already "failed"
            console.print(f"[red]{type(e).__name__}: {e}[/]")
        _report(run, printer)
        self.history.append(run)
        self.spent += run.cost_usd
        return run


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    ap = argparse.ArgumentParser(prog="pullwright", description="Intake → confirm → supervisor + workers → diagnosis → summary → branch/PR. With no task, opens an interactive session.")
    ap.add_argument("task", nargs="?", help="a first task to run; omit to just open the prompt")
    ap.add_argument("--cwd", default=".", help="repository to work in (default: current directory)")
    ap.add_argument("--model", default=MAIN_MODEL, help="model for intake, supervisor, diagnosis and summary")
    ap.add_argument("--worker-model", default=WORKER_MODEL, help="model for workers")
    ap.add_argument("--max-cost", type=float, default=20.0, metavar="USD", help="stop a run past this spend")
    ap.add_argument("--worker", action="store_true", help="run a single worker agent on the task, no lifecycle (needs a task)")
    ap.add_argument("--once", action="store_true", help="run the task and exit instead of opening the prompt")
    a = ap.parse_args(argv)

    cwd = Path(a.cwd).resolve()
    client = anthropic.Anthropic()
    session = Session(cwd, client, a.model, a.worker_model, a.max_cost)

    head = Table.grid(padding=(0, 2))
    head.add_row("[grey50]repo[/]", str(cwd) + ("  [grey50](repo config found)[/]" if session.cfg.root else ""))
    head.add_row("[grey50]models[/]", f"{a.model.removeprefix('claude-')} / workers {a.worker_model.removeprefix('claude-')}")
    head.add_row("[grey50]cap[/]", f"${a.max_cost:.2f} per run")
    console.print(Panel(head, title="[bold]pullwright[/]", subtitle="[grey50]/help for commands[/]", border_style="grey42"))

    if a.worker:
        if not a.task:
            ap.error("--worker needs a task")
        bus = EventBus()
        bus.subscribe(Printer())
        agent = AgentDef(name="worker", system=session.cfg.system_prompt("worker"), model=a.model, max_cost_usd=a.max_cost)
        res = run_agent(client, agent, [{"role": "user", "content": a.task}], ToolContext(root=cwd, approve=session.io.approve_command), bus, run_id=uuid.uuid4().hex[:8])
        console.print(Panel(Markdown(res.text or "(no text)"), title="Worker report", border_style="green" if res.stop == "end_turn" else "yellow"))
        console.print(f"[grey50]${res.cost_usd:.4f} · stop={res.stop}[/]")
        return 0 if res.stop == "end_turn" else 1

    if a.task:
        run = session.run(a.task)
        if a.once:
            return 0 if run.state == "done" else 1

    interrupted = False
    while True:
        try:
            line = console.input("\n[bold cyan]›[/] ").strip()
            interrupted = False
        except EOFError:
            break
        except KeyboardInterrupt:
            if interrupted:
                break
            interrupted = True
            console.print("[grey50]Ctrl+C again to quit[/]")
            continue
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            break
        if line == "/help":
            console.print(HELP)
        elif line == "/new":
            session.new_thread()
            console.print("[grey50]New thread. The next task starts its own branch.[/]")
        elif line == "/cost":
            console.print(f"[grey50]session ${session.spent:.4f} across {len(session.history)} run(s) in this thread[/]")
        elif line.startswith("/"):
            console.print(f"[red]unknown command {line}[/]  [grey50]try /help[/]")
        else:
            session.run(line)
    console.print(f"[grey50]bye · session ${session.spent:.4f}[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
