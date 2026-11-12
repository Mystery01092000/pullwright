"""The repository knowledge base: pick the right repository when a task does not name one.

For every repository Pullwright works on, a profile is built from GitHub (no clone): name, description, topics,
languages, README and file paths. A task is matched against the profiles:

  1. the repository's own name in the task ("...on my portfolio") decides outright;
  2. otherwise each word of the task scores in the fields it appears in, weighted by field (name > topics and
     description > paths > README) and by rarity across repositories (a word every repo has says nothing);
  3. the developer's own history adds a small prior (repositories they asked about before);
  4. a clear winner is picked; a close call goes to one cheap Haiku call over the top candidates, or back to the
     developer as a question.

The result always says which repository was picked and why, so a wrong guess is caught before anything is built.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .agent import AgentDef, run_agent
from .events import EventBus
from .github.client import GitHub
from .models import WORKER_MODEL, role_model
from .tools import ToolContext

STOP = frozenset("""a an and are as at be by can could do does for from has have how i if in into is it its make me my need
not of on or our please should so that the their them then there these this those to up use using want we when where which
while will with would you your add fix update change new create remove make support allow""".split())
WEIGHTS = {"name": 5.0, "topics": 3.0, "description": 3.0, "paths": 1.5, "readme": 1.0}
MAX_AGE_S = 24 * 3600


def tokens(text: str) -> list[str]:
    """Lowercase words, with camelCase and snake_case split: 'OrderStore.remove' -> ['order', 'store', 'remove']."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) >= 3 and t not in STOP and not t.isdigit()]


@dataclass
class RepoProfile:
    repo: str
    description: str = ""
    topics: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    readme: str = ""
    paths: list[str] = field(default_factory=list)
    built_at: float = field(default_factory=time.time)

    def field_tokens(self) -> dict[str, set[str]]:
        name = self.repo.split("/", 1)[1]
        return {
            "name": set(tokens(name.replace("-", " ").replace("_", " "))) | {name.lower()},
            "topics": set(tokens(" ".join(self.topics))),
            "description": set(tokens(self.description)),
            "paths": set(tokens(" ".join(self.paths))),
            "readme": set(tokens(self.readme)),
        }

    def summary(self) -> str:
        langs = ", ".join(self.languages[:4]) or "unknown"
        top = sorted({p.split("/")[0] for p in self.paths})[:12]
        return f"{self.repo}: {self.description or '(no description)'} | languages: {langs} | top-level: {', '.join(top)}"


def build_profile(gh: GitHub, repo: str) -> RepoProfile:
    info = gh.repo_info(repo)
    langs = gh.languages(repo)
    return RepoProfile(
        repo=repo,
        description=info.get("description") or "",
        topics=list(info.get("topics") or []),
        languages=[k for k, _ in sorted(langs.items(), key=lambda kv: -kv[1])],
        readme=gh.readme(repo)[:6000],
        paths=gh.tree(repo, info.get("default_branch", "main"))[:3000],
    )


@dataclass
class Candidate:
    repo: str
    score: float
    reasons: list[str]
    matched: bool = False  # some word of the task matched this repo (history alone never picks a repo)


@dataclass
class Resolution:
    repo: str  # "" when undecided
    method: str  # named | keywords | model | ask | none
    reason: str
    candidates: list[Candidate]


def rank(task: str, profiles: list[RepoProfile], history: Counter[str] | None = None) -> list[Candidate]:
    words = set(tokens(task))
    fields = {p.repo: p.field_tokens() for p in profiles}
    n = max(len(profiles), 1)
    df = Counter(w for f in fields.values() for w in set().union(*f.values()))
    out = []
    for p in profiles:
        score, hits = 0.0, Counter()
        for w in words:
            idf = math.log(1 + n / df[w]) if df[w] else 0.0
            for fname, toks in fields[p.repo].items():
                if w in toks:
                    score += WEIGHTS[fname] * idf
                    hits[w] += WEIGHTS[fname] * idf
        reasons = [f"matches {', '.join(w for w, _ in hits.most_common(4))}"] if hits else []
        asked = (history or Counter())[p.repo]
        if asked:
            score += 1.5 * math.log1p(asked)
            reasons.append(f"you asked about it {asked} time{'s' if asked != 1 else ''} before")
        out.append(Candidate(p.repo, round(score, 3), reasons, matched=bool(hits)))
    return sorted(out, key=lambda c: -c.score)


ROUTER_SCHEMA = {
    "type": "object",
    "properties": {"repo": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["repo", "reason"],
    "additionalProperties": False,
}


class KnowledgeBase:
    def __init__(self, gh: GitHub, store: Any, client_factory: Callable[[], Any] | None = None) -> None:
        self.gh, self.store, self.client_factory = gh, store, client_factory
        self._lock = threading.Lock()
        with store.lock:
            store.db.execute("CREATE TABLE IF NOT EXISTS repo_profiles (repo TEXT PRIMARY KEY, profile TEXT, built_at REAL)")
            store.db.commit()

    # --- profiles -------------------------------------------------------------------------------

    def save(self, profile: RepoProfile) -> None:
        with self.store.lock:
            self.store.db.execute("INSERT OR REPLACE INTO repo_profiles VALUES (?,?,?)", (profile.repo, json.dumps(asdict(profile)), profile.built_at))
            self.store.db.commit()

    def get(self, repo: str) -> RepoProfile | None:
        with self.store.lock:
            row = self.store.db.execute("SELECT profile FROM repo_profiles WHERE repo = ?", (repo,)).fetchone()
        return RepoProfile(**json.loads(row[0])) if row else None

    def refresh(self, repos: list[str], max_age_s: float = MAX_AGE_S) -> list[RepoProfile]:
        """Profiles for `repos`, rebuilding missing or stale ones. A repo GitHub cannot read is skipped."""
        out = []
        with self._lock:
            for repo in repos:
                prof = self.get(repo)
                if prof is None or time.time() - prof.built_at > max_age_s:
                    try:
                        prof = build_profile(self.gh, repo)
                        self.save(prof)
                    except Exception:
                        if prof is None:
                            continue
                out.append(prof)
        return out

    # --- resolving ------------------------------------------------------------------------------

    def history(self, login: str) -> Counter[str]:
        return Counter(r["repo"] for r in self.store.requests(login, 500)) if login else Counter()

    def resolve(self, task: str, repos: list[str], login: str = "") -> Resolution:
        profiles = self.refresh(repos)
        if not profiles:
            return Resolution("", "none", "no repository profiles available", [])
        low = task.lower()
        named = [p.repo for p in profiles if re.search(rf"\b{re.escape(p.repo.split('/', 1)[1].lower())}\b", low)]
        cands = rank(task, profiles, self.history(login))
        if len(named) == 1:
            return Resolution(named[0], "named", "the task names it", cands)
        if not cands or not cands[0].matched:
            return Resolution("", "ask", "nothing in the task points to a repository", cands)
        top, second = cands[0], (cands[1] if len(cands) > 1 else None)
        if second is None or top.score >= 1.5 * second.score + 0.5:
            return Resolution(top.repo, "keywords", "; ".join(top.reasons) or "best match", cands)
        close = [c for c in cands[:3] if c.matched]
        picked = self._ask_model(task, [p for p in profiles if p.repo in {c.repo for c in close}])
        if picked:
            return Resolution(picked[0], "model", picked[1], cands)
        return Resolution("", "ask", "more than one repository fits", cands)

    def _ask_model(self, task: str, profiles: list[RepoProfile]) -> tuple[str, str] | None:
        """One Haiku call over the close candidates' profiles. Cheap: a few hundred tokens, no tools."""
        if self.client_factory is None or not profiles:
            return None
        allowed = [p.repo for p in profiles]
        agent = AgentDef(name="router", model=role_model("router") or WORKER_MODEL, effort="low", tools=[], max_turns=1, max_cost_usd=0.05,
                         output_schema=ROUTER_SCHEMA,
                         system="Pick the repository a developer's task belongs to. Answer with one repository from the list "
                                "exactly as written, or an empty string if none clearly fits, and a one-sentence reason.")
        prompt = f"Task:\n{task}\n\nRepositories:\n" + "\n".join(f"- {p.summary()}" for p in profiles)
        try:
            res = run_agent(self.client_factory(), agent, [{"role": "user", "content": prompt}], ToolContext(root=Path(".")),
                            EventBus(), run_id="router")
            data = json.loads(res.text)
        except Exception:
            return None
        return (data["repo"], data["reason"]) if data.get("repo") in allowed else None
