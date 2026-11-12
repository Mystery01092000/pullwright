"""Build docs/code-walkthrough.html from code-walkthrough.md: the same text, with drawn SVG diagrams in place of the
Mermaid blocks, a table of contents, and every `file.py:123` reference linked to the source on GitHub. One
self-contained file (no scripts, no external requests), light and dark themes. Styles come from the pitch page.

    uv run --no-project --with markdown python scripts/build_walkthrough_html.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import markdown

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_pitch_html import ARROW, CSS, box, esc, label  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "code-walkthrough.md"
OUT = ROOT / "docs" / "code-walkthrough.html"
REPO_URL = "https://github.com/Mystery01092000/pullwright/blob/main"

EXTRA_CSS = """
.toc{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin:0 0 28px}
.toc b{display:block;font-size:13px;color:var(--quiet);margin-bottom:6px}
.toc ul{margin:0;padding-left:0;list-style:none;columns:2;column-gap:28px}.toc li{font-size:14px;break-inside:avoid;padding:1px 0}
@media (max-width:600px){.toc ul{columns:1}}
h3{font-size:17px;margin:24px 0 8px}
pre{background:var(--code);border:1px solid var(--line);border-radius:10px;padding:12px 14px;overflow-x:auto;font-size:13px;line-height:1.45}
pre code{background:none;padding:0}
a code{color:var(--accent)}
svg .msg{font-size:11px;fill:var(--ink);paint-order:stroke;stroke:var(--paper);stroke-width:4px;stroke-linejoin:round}
svg .lane{stroke:var(--line);stroke-width:1.25;stroke-dasharray:3 4}
svg .num{font-size:10px;font-weight:600;fill:var(--accent)}
"""


# --- diagram 1: the pieces -------------------------------------------------------------------------------------------

def arch_svg() -> str:
    title = "One control plane, one throwaway runner per issue, GitHub as the queue"
    s = [f"<svg viewBox='0 0 760 540' role='img' aria-label='{esc(title)}'>", ARROW.format(id="wt-arch"),
         f"<text x='24' y='30' class='title'>{esc(title)}</text>",
         "<rect x='168' y='56' width='388' height='350' rx='8' class='zone'/>", label(184, 76, "Control plane: pullwright-serve", cls="zonename"),
         "<rect x='576' y='56' width='176' height='350' rx='8' class='zone tint'/>", label(592, 76, "Runner: per issue", cls="zonename")]
    lines = [
        ("M140 125H183", ""), ("M345 125H373", ""), ("M455 151V187", ""), ("M265 151V187", "dashed"),
        ("M265 293V241", "dashed"), ("M665 151V181", ""), ("M665 233V256", ""), ("M665 308V323", ""),
        ("M590 282H563V320H537", ""), ("M265 346V372H570V125H588", ""), ("M455 346V430H560V458", ""),
        ("M265 346V398H300V458", "dashed"), ("M742 125H752V512H402", ""),
    ]
    s += [f"<path d='{d}' class='line {c}' marker-end='url(#wt-arch)'/>" for d, c in lines]
    for cx, cy, w, h, n, sub, acc in [
        (80, 125, 120, 56, "React UI", "web/src", False),
        (265, 125, 160, 52, "web.py", "login, /api, threads", False),
        (455, 125, 160, 52, "intake.py", "route, plan, decide", False),
        (265, 215, 160, 52, "store.py", "SQLite or Postgres", False),
        (455, 215, 160, 52, "pipeline/plan.py", "describer + verifier", False),
        (265, 320, 160, 52, "dispatcher.py", "queue to runner", False),
        (455, 320, 160, 52, "llm_proxy.py", "job tokens, dollar caps", True),
        (665, 125, 154, 52, "runner/job.py", "clone, push, PR", False),
        (665, 207, 154, 52, "pipeline/work.py", "rounds, review", False),
        (665, 282, 154, 52, "coders.py", "goose, Claude, native", True),
        (665, 349, 154, 46, "checks.py", "commands decide", False),
        (300, 484, 200, 52, "GitHub", "issues, labels, PRs", False),
        (560, 484, 200, 52, "Anthropic API", "every model call", False),
    ]:
        s.append(box(cx, cy, w, h, n, sub, acc))
    s += [label(162, 112, "fetch /api", "middle"), label(362, 252, "events"), label(410, 366, "docker run", "middle"),
          label(472, 444, "model calls, capped"), label(306, 424, "poll queued label"), label(744, 500, "push, open PR", "end"), "</svg>"]
    return "".join(s)


# --- diagram 2: the flow, as a sequence diagram ------------------------------------------------------------------------

LANES = ["Developer", "React UI", "web.py", "intake.py", "PlanRun", "GitHub", "Dispatcher", "Runner", "LLM proxy"]
STEPS: list[tuple[str, str, str, str]] = [  # (from, to, label, kind: msg | ret | self)
    ("Developer", "React UI", "open the app", "msg"),
    ("React UI", "web.py", "GET /api/me (cookie)", "msg"),
    ("web.py", "React UI", "401: GitHub login, cookie set", "ret"),
    ("Developer", "React UI", "\"owner/repo: add X\"", "msg"),
    ("React UI", "web.py", "POST /api/requests", "msg"),
    ("web.py", "intake.py", "on_mention(Mention)", "msg"),
    ("intake.py", "intake.py", "question, change, or reply?", "self"),
    ("intake.py", "PlanRun", "plan (background)", "msg"),
    ("PlanRun", "intake.py", "plan card, waits for a decision", "ret"),
    ("React UI", "web.py", "GET /api/threads/{id} every 2 s", "msg"),
    ("Developer", "React UI", "Approve (button or \"lgtm\")", "msg"),
    ("React UI", "web.py", "POST /decision", "msg"),
    ("web.py", "intake.py", "on_decision: write access?", "msg"),
    ("intake.py", "GitHub", "open issue, label queued", "msg"),
    ("Dispatcher", "GitHub", "every 30 s: oldest queued", "msg"),
    ("Dispatcher", "Dispatcher", "fresh token, job token", "self"),
    ("Dispatcher", "Runner", "docker run pullwright-work", "msg"),
    ("Runner", "GitHub", "clone", "msg"),
    ("Runner", "LLM proxy", "coder's model calls", "msg"),
    ("Runner", "Runner", "checks, 1 fix round, review", "self"),
    ("Runner", "LLM proxy", "POST /llm/github-token", "msg"),
    ("Runner", "GitHub", "push branch, open PR", "msg"),
    ("Runner", "Dispatcher", "PWEVENT lines, PWRESULT", "ret"),
    ("Dispatcher", "intake.py", "on_job_done: PR link to thread", "msg"),
    ("React UI", "web.py", "GET /activity: runner panel", "msg"),
]


def seq_svg() -> str:
    title = "One request, start to finish"
    left, right, top, row = 92, 712, 70, 30
    gap = (right - left) / (len(LANES) - 1)
    x = {name: left + i * gap for i, name in enumerate(LANES)}
    height = top + 40 + len(STEPS) * row + 30
    s = [f"<svg viewBox='0 0 760 {height}' role='img' aria-label='{esc(title)}'>", ARROW.format(id="wt-seq"),
         f"<text x='24' y='30' class='title'>{esc(title)}</text>"]
    for name in LANES:
        s.append(f"<line x1='{x[name]}' y1='{top + 18}' x2='{x[name]}' y2='{height - 12}' class='lane'/>")
        s.append(box(x[name], top, 74, 34, name, "", name in ("intake.py", "Runner")))
    y = top + 50
    for i, (a, b, text, kind) in enumerate(STEPS, 1):
        if kind == "self":
            xa = x[a]
            if xa > 560:  # near the right edge: loop and label on the left
                s.append(f"<path d='M{xa} {y - 6}H{xa - 22}V{y + 8}H{xa - 3}' class='line' marker-end='url(#wt-seq)'/>")
                s.append(f"<text x='{xa - 28}' y='{y + 5}' class='msg' text-anchor='end'>{esc(text)}</text>")
            else:
                s.append(f"<path d='M{xa} {y - 6}H{xa + 22}V{y + 8}H{xa + 3}' class='line' marker-end='url(#wt-seq)'/>")
                s.append(f"<text x='{xa + 28}' y='{y + 5}' class='msg'>{esc(text)}</text>")
        else:
            xa, xb = x[a], x[b]
            end = xb - 3 if xb > xa else xb + 3
            s.append(f"<path d='M{xa} {y}H{end}' class='line{' dashed' if kind == 'ret' else ''}' marker-end='url(#wt-seq)'/>")
            mid = (xa + xb) / 2
            s.append(f"<text x='{mid}' y='{y - 5}' class='msg' text-anchor='middle'>{esc(text)}</text>")
        s.append(f"<text x='24' y='{y + 4}' class='num'>{i}</text>")  # step numbers in a gutter on the left
        y += row
    s.append("</svg>")
    return "".join(s)


# --- source links ----------------------------------------------------------------------------------------------------

def source_index() -> dict[str, str]:
    """`job.py`, `runner/job.py`, `server/pullwright/runner/job.py` ... -> repository path, when the name is unique."""
    files = [p.relative_to(ROOT).as_posix() for d in ("server/pullwright", "server/tests", "web/src") for p in (ROOT / d).rglob("*")
             if p.suffix in (".py", ".ts", ".tsx") and "__pycache__" not in p.parts]
    index: dict[str, list[str]] = {}
    for f in files:
        parts = f.split("/")
        for k in range(1, len(parts) + 1):
            index.setdefault("/".join(parts[-k:]), []).append(f)
    return {k: v[0] for k, v in index.items() if len(v) == 1}


def link_sources(body: str) -> str:
    idx = source_index()

    def repl(m: re.Match[str]) -> str:
        ref, line = m.group(1), m.group(2)
        path = idx.get(ref)
        if path is None:
            return m.group(0)
        anchor = f"#L{line.lstrip('~')}" if line else ""
        return f"<a href='{REPO_URL}/{path}{anchor}'>{m.group(0)}</a>"

    return re.sub(r"<code>([\w./-]+\.(?:py|tsx|ts))(?::(~?\d+))?</code>", repl, body)


def build() -> str:
    md = SRC.read_text(encoding="utf-8")
    md = re.sub(r"^> A formatted version.*\n\n?", "", md, flags=re.M)
    title = re.search(r"^# (.+)$", md, re.M).group(1)
    intro = re.search(r"^# [^\n]+\n\n(.+?)\n\n", md, re.S | re.M).group(1)
    md = re.sub(r"^# [^\n]+\n\n.+?\n\n", "", md, count=1, flags=re.S | re.M)
    figs = iter([arch_svg(), seq_svg()])
    md = re.sub(r"```mermaid\n.*?```", lambda _m: f"\n<figure>{next(figs)}</figure>\n", md, flags=re.S)
    body = markdown.markdown(md, extensions=["tables", "fenced_code", "toc"])
    body = body.replace("<table>", "<div class='tablewrap'><table>").replace("</table>", "</table></div>")
    body = link_sources(body)
    heads = re.findall(r"<h2 id=\"([^\"]+)\">(.+?)</h2>", body)
    toc = "<nav class='toc' aria-label='Contents'><b>Contents</b><ul>" + "".join(
        f"<li><a href='#{hid}'>{text}</a></li>" for hid, text in heads) + "</ul></nav>"
    intro_html = markdown.markdown(intro)
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<meta name='color-scheme' content='light dark'><title>Pullwright code walkthrough</title><style>{CSS}{EXTRA_CSS}</style></head><body><main>"
            f"<header class='hero'><h1>{esc(title)}</h1><div class='tag'>{intro_html}</div></header>{toc}{body}"
            f"<footer>Generated from code-walkthrough.md. Source: "
            f"<a href='https://github.com/Mystery01092000/pullwright'>github.com/Mystery01092000/pullwright</a></footer></main></body></html>")


if __name__ == "__main__":
    OUT.write_text(build(), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB)")
