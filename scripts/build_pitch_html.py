"""Build docs/pitch/pullwright-overview.html from pullwright-overview.md: the same text, with drawn SVG diagrams in
place of the Mermaid blocks, one self-contained file (no scripts, no external requests), light and dark themes.

    uv run --with markdown python scripts/build_pitch_html.py
"""
from __future__ import annotations

import html
import re
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "pitch" / "pullwright-overview.md"
OUT = ROOT / "docs" / "pitch" / "pullwright-overview.html"


def esc(s: str) -> str:
    return html.escape(s, quote=True)


ARROW = "<defs><marker id='{id}' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='6' markerHeight='6' orient='auto-start-reverse'><path d='M0 0L10 5L0 10z' class='head'/></marker></defs>"


def box(cx: float, cy: float, w: float, h: float, name: str, sub: str = "", accent: bool = False) -> str:
    cls = "box accent" if accent else "box"
    out = [f"<rect x='{cx - w / 2}' y='{cy - h / 2}' width='{w}' height='{h}' rx='8' class='{cls}'/>"]
    if sub:
        out.append(f"<text x='{cx}' y='{cy - 4}' class='name' text-anchor='middle'>{esc(name)}</text>")
        out.append(f"<text x='{cx}' y='{cy + 14}' class='sub{' strong' if accent else ''}' text-anchor='middle'>{esc(sub)}</text>")
    else:
        out.append(f"<text x='{cx}' y='{cy + 4}' class='name' text-anchor='middle'>{esc(name)}</text>")
    return "".join(out)


def label(x: float, y: float, words: str, anchor: str = "start", cls: str = "sub") -> str:
    return f"<text x='{x}' y='{y}' class='{cls}' text-anchor='{anchor}'>{esc(words)}</text>"


def flow_svg() -> str:
    W, H, c1, c2, c3, r1, r2, r3, r4 = 208, 56, 136, 380, 624, 120, 230, 368, 478
    title = "Two human gates: the plan is approved before code, and a person merges"
    s = [f"<svg viewBox='0 0 760 546' role='img' aria-label='{esc(title)}'>", ARROW.format(id="run-arrow"),
         f"<text x='24' y='30' class='title'>{esc(title)}</text>",
         "<rect x='16' y='52' width='728' height='226' rx='8' class='zone'/>", label(32, 74, "Control plane: plan on a read-only clone", cls="zonename"),
         "<rect x='16' y='300' width='728' height='222' rx='8' class='zone tint'/>", label(728, 322, "Runner: one isolated container per issue", "end", "zonename")]
    main = ["M240 120H276", "M484 120H520", "M624 148V202", "M520 230H484", "M276 230H240", "M136 258V340", "M240 368H276",
            "M484 368H520", "M624 396V450", "M520 478H484", "M276 478H240"]
    s += [f"<path d='{d}' class='line' marker-end='url(#run-arrow)'/>" for d in main]
    s += ["<path d='M380 202V175H580V148' class='line dashed' marker-end='url(#run-arrow)'/>", "<path d='M548 396V423' class='line dashed'/>",
          "<path d='M600 450V423' class='line dashed'/>", "<path d='M600 423H400V396' class='line dashed' marker-end='url(#run-arrow)'/>",
          "<path d='M110 450V396' class='line dashed' marker-end='url(#run-arrow)'/>"]
    for cx, cy, n, sub, acc in [(c1, r1, "1. Ask in chat", "Slack, Teams or the web UI", False), (c2, r1, "2. Intake", "linked account, allowed repo", False),
                                (c3, r1, "3. Describer drafts", "plan on a read-only clone", False), (c3, r2, "4. Verifier checks", "corrections sent back once", False),
                                (c2, r2, "5. Approve the plan", "write access; or revise", True), (c1, r2, "6. Issue is queued", "label pullwright:queued", False),
                                (c1, r3, "7. Runner starts", "container, 1-hour token", False), (c2, r3, "8. Supervisor + workers", "specialists, skills, hooks", False),
                                (c3, r3, "9. Project checks", "tests, lint, types, build", False), (c3, r4, "10. Independent review", "against acceptance criteria", False),
                                (c2, r4, "11. Pull request", "push, review request, log", False), (c1, r4, "12. Human review, merge", "people merge, never agents", True)]:
        s.append(box(cx, cy, W, H, n, sub, acc))
    s += [label(480, 168, "revise: describer redrafts", "middle"), label(392, 437, "fail or gaps: retry (max 4)", "end"), label(118, 437, "review comments"), "</svg>"]
    return "".join(s)


def arch_svg() -> str:
    W, H, k1, k2, rA, rB, rC = 236, 56, 150, 406, 188, 260, 332
    title = "Code and keys stay on your infrastructure; runners get a capped token"
    s = [f"<svg viewBox='0 0 760 560' role='img' aria-label='{esc(title)}'>", ARROW.format(id="sys-arrow"), f"<text x='24' y='30' class='title'>{esc(title)}</text>"]
    for x0, n, sub in [(16, "Slack", "Socket Mode"), (196, "Microsoft Teams", "Bot Framework"), (376, "Web UI", "GitHub login")]:
        s.append(box(x0 + 81, 76, 162, 48, n, ""))
        s[-1] = s[-1].replace(f"y='{76 + 4}'", "y='72'") + label(x0 + 81, 90, sub, "middle")
    s += ["<rect x='16' y='124' width='524' height='252' rx='8' class='zone'/>", label(32, 146, "Control plane: your host or container", cls="zonename"),
          "<rect x='16' y='400' width='524' height='108' rx='8' class='zone tint'/>", label(170, 422, "Runner: per issue, no keys", cls="zonename")]
    for d in ["M97 100V124", "M277 100V124", "M457 100V124", "M524 188H568", "M568 260H524", "M524 332H568", "M150 360V436", "M406 436V360", "M278 492V528H752V224H744"]:
        s.append(f"<path d='{d}' class='line' marker-end='url(#sys-arrow)'/>")
    for cx, cy, n, sub, acc in [(k1, rA, "Web API + UI", "requests, timeline, queue, costs", False), (k2, rA, "Intake + planning", "describer, verifier; no hooks", False),
                                (k1, rB, "Store", "SQLite or Postgres; every event", False), (k2, rB, "Org config sync", "skills, agents, hooks; pinned", False),
                                (k1, rC, "Dispatcher", "oldest queued issue first", False), (k2, rC, "LLM proxy", "job tokens, dollar caps", True)]:
        s.append(box(cx, cy, W, H, n, sub, acc))
    s.append(box(278, 464, 492, 56, "Supervisor, workers and specialists, then checks and review", "the project's own tests, lint, types and build decide when it is done"))
    s += ["<rect x='568' y='160' width='176' height='128' rx='8' class='box'/>", label(656, 184, "GitHub", "middle", "name")]
    s += [label(584, y, t) for y, t in [(204, "App on chosen repos"), (220, "issues are the queue"), (236, "branches and PRs"), (252, "org config repository")]]
    s += ["<rect x='568' y='304' width='176' height='88' rx='8' class='box'/>", label(656, 328, "Model providers", "middle", "name")]
    s += [label(584, y, t) for y, t in [(348, "Anthropic, OpenAI,"), (364, "Gemini, OpenRouter,"), (380, "or self-hosted")]]
    s += [label(158, 392, "starts it, 1-hour token"), label(414, 392, "model calls"), label(520, 546, "push the branch, open the PR", "middle"), "</svg>"]
    return "".join(s)


def pilot_svg() -> str:
    title = "A six-week pilot ends in a go or no-go decision on measured results"
    s = [f"<svg viewBox='0 0 760 300' role='img' aria-label='{esc(title)}'>", f"<text x='24' y='34' class='title'>{esc(title)}</text>"]
    phases = [(24, "1. Set up", "Week 1", ["App on 2-3 repos", "control plane deployed", "providers approved", "shared config pinned"], False),
              (204, "2. Supervised use", "Weeks 2-3", ["2-3 engineers", "small, testable tasks", "every PR reviewed"], False),
              (384, "3. Measure, tune", "Weeks 4-5", ["merge rate, cost", "tune shared agents", "run the evals"], False),
              (564, "4. Decide", "Week 6", ["readout to leads", "expand, adjust", "or stop"], True)]
    for x0, n, when, lines, acc in phases:
        s.append(f"<rect x='{x0}' y='60' width='164' height='152' rx='8' class='{'box accent' if acc else 'box'}'/>")
        s.append(f"<text x='{x0 + 16}' y='84' class='name'>{esc(n)}</text>")
        s.append(label(x0 + 16, 102, when))
        s += [f"<text x='{x0 + 16}' y='{128 + 16 * i}' class='sub strong'>{esc(t)}</text>" for i, t in enumerate(lines)]
    s.append("<line x1='24' x2='728' y1='236' y2='236' class='line'/>")
    for cx, a, b in [(194, "Sandbox task green", "end to end"), (374, "10+ PRs reviewed", "no guardrail breach"), (554, "Metrics collected", "for the readout")]:
        s.append(f"<path d='M{cx} 226l10 10l-10 10l-10 -10z' class='gate'/>")
        s.append(f"<text x='{cx}' y='266' class='sub strong bold' text-anchor='middle'>{esc(a)}</text>")
        s.append(label(cx, 282, b, "middle"))
    s.append("</svg>")
    return "".join(s)


CSS = """
:root{--bg:#f7f7f5;--paper:#fff;--ink:#16171b;--quiet:#55585f;--line:#d9dbe0;--axis:#8d9098;--tint:#f1f2f4;--accent:#2a6fd6;--accent-fill:rgba(42,111,214,.10);--code:#f1f2f4}
@media (prefers-color-scheme:dark){:root{--bg:#0f1013;--paper:#16171b;--ink:#eceef2;--quiet:#a6a9b1;--line:#2c2e35;--axis:#80838c;--tint:#1d1f24;--accent:#5b9cf0;--accent-fill:rgba(91,156,240,.14);--code:#1f2126}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:860px;margin:0 auto;padding:48px 24px 80px}
.hero{background:var(--paper);border:1px solid var(--line);border-radius:16px;padding:32px;margin-bottom:32px}
.hero h1{margin:0 0 8px;font-size:32px;line-height:1.2;letter-spacing:-.02em}
.hero p.tag{margin:0;color:var(--quiet);font-size:17px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:24px}
.stat{border:1px solid var(--line);border-radius:12px;padding:12px 14px}.stat b{display:block;font-size:22px;font-variant-numeric:tabular-nums}.stat span{color:var(--quiet);font-size:13px}
h2{font-size:22px;letter-spacing:-.01em;margin:40px 0 12px;padding-top:8px;border-top:1px solid var(--line)}
p,li{max-width:72ch}a{color:var(--accent)}code{font:13px ui-monospace,"Cascadia Code",Consolas,monospace;background:var(--code);padding:.1em .35em;border-radius:5px}
blockquote{margin:0;color:var(--quiet)}
.tablewrap{overflow-x:auto;margin:16px 0;border:1px solid var(--line);border-radius:12px;background:var(--paper)}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;vertical-align:top;padding:9px 12px;border-bottom:1px solid var(--line)}
th{font-weight:600;color:var(--quiet);font-size:13px;background:var(--tint)}tr:last-child td{border-bottom:0}
figure{margin:20px 0;background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:12px}
figure svg{width:100%;height:auto;display:block}
svg .title{font-size:15px;font-weight:600;fill:var(--ink)}svg .name{font-size:13px;font-weight:600;fill:var(--ink)}
svg .sub{font-size:11.5px;fill:var(--quiet)}svg .sub.strong{fill:var(--ink)}svg .bold{font-weight:600}svg .zonename{font-size:11.5px;font-weight:600;fill:var(--quiet)}
svg .box{fill:none;stroke:var(--axis);stroke-width:1.25}svg .box.accent{fill:var(--accent-fill);stroke:var(--accent);stroke-width:2}
svg .zone{fill:none;stroke:var(--line);stroke-width:1.25}svg .zone.tint{fill:var(--tint)}
svg .line{fill:none;stroke:var(--axis);stroke-width:1.25}svg .dashed{stroke-dasharray:4 3}svg .head{fill:var(--axis)}svg .gate{fill:var(--tint);stroke:var(--axis);stroke-width:1.25}
footer{margin-top:48px;color:var(--quiet);font-size:14px}
@media (max-width:600px){main{padding:24px 16px 56px}.hero{padding:22px}.hero h1{font-size:26px}}
@media print{body{background:#fff}main{max-width:none;padding:0}.hero,figure,.tablewrap{break-inside:avoid}h2{break-after:avoid}}
"""


def build() -> str:
    md = SRC.read_text(encoding="utf-8")
    md = re.sub(r"^> A formatted version.*\n\n?", "", md, flags=re.M)
    title = re.search(r"^# (.+)$", md, re.M).group(1)
    tagline = re.search(r"^\*(.+)\*$", md, re.M).group(1)
    md = re.sub(r"^# .+\n\n\*.+\*\n", "", md, count=1, flags=re.M)
    figs = iter([flow_svg(), arch_svg()])
    md = re.sub(r"```mermaid\n.*?```", lambda _m: f"\n<figure>{next(figs)}</figure>\n", md, flags=re.S)
    md = md.replace("## Pilot plan\n", f"## Pilot plan\n\n<figure>{pilot_svg()}</figure>\n", 1)
    md = md.replace("- [ ] ", "- ")
    body = markdown.markdown(md, extensions=["tables"])
    body = body.replace("<table>", "<div class='tablewrap'><table>").replace("</table>", "</table></div>")
    stats = ("<div class='stats'><div class='stat'><b>$0.108 to $0.135</b><span>per task, chat to ready PR, measured</span></div>"
             "<div class='stat'><b>2 human gates</b><span>plan approval and merge</span></div>"
             "<div class='stat'><b>0 keys in runners</b><span>with the LLM proxy on</span></div>"
             "<div class='stat'><b>Apache-2.0</b><span>self-hosted, auditable</span></div></div>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<meta name='color-scheme' content='light dark'><title>{esc(title)}</title><style>{CSS}</style></head><body><main>"
            f"<header class='hero'><h1>{esc(title)}</h1><p class='tag'>{esc(tagline)}</p>{stats}</header>{body}"
            f"<footer>Generated from pullwright-overview.md. Source and setup guides: "
            f"<a href='https://github.com/Mystery01092000/pullwright'>github.com/Mystery01092000/pullwright</a></footer></main></body></html>")


if __name__ == "__main__":
    OUT.write_text(build(), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB)")
