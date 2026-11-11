import base64
import json
from collections import Counter

import httpx

from pullwright.github.client import API, GitHub, PatAuth
from pullwright.knowledge import KnowledgeBase, RepoProfile, build_profile, rank, tokens
from pullwright.store import Store
from tests.fake_client import FakeClient, text, turn

PORTFOLIO = RepoProfile("arcanetech-ai/portfolio", "Pranav's portfolio site (Vite + React + TypeScript)", ["portfolio", "react"],
                        ["TypeScript", "CSS"], "# portfolio\nPersonal site with projects and open source work.",
                        ["src/App.tsx", "src/components/Hero.tsx", "src/components/Projects.tsx", "src/styles.css", "src/data.ts"])
SANDBOX = RepoProfile("arcanetech-ai/pullwright-sandbox", "Sandbox for testing Pullwright end to end", [],
                      ["Python"], "# pullwright-sandbox\nA tiny order service: orders, totals, store.",
                      ["src/shop/orders.py", "tests/test_orders.py", "pyproject.toml"])
BILLING = RepoProfile("arcanetech-ai/billing", "Invoices and payments API", ["payments"], ["Python"],
                      "# billing\nInvoices, refunds, order totals.", ["billing/invoices.py", "billing/orders.py"])


def test_tokens_split_code_identifiers():
    # task verbs (add, remove, fix...) are stop words: they say nothing about which repository
    assert tokens("Add OrderStore.remove to the shop_api") == ["order", "store", "shop", "api"]


def test_rank_prefers_the_repo_whose_code_and_docs_match():
    cands = rank("show the hero section with a dark theme toggle in the components", [PORTFOLIO, SANDBOX])
    assert cands[0].repo == "arcanetech-ai/portfolio" and cands[0].score > cands[1].score
    assert "hero" in cands[0].reasons[0]


def test_rare_words_count_more_than_shared_ones():
    # "orders" is in two repos, "refunds" only in billing
    assert rank("handle refunds for orders", [SANDBOX, BILLING])[0].repo == "arcanetech-ai/billing"


def test_developer_history_breaks_ties():
    cands = rank("fix the orders total", [SANDBOX, BILLING], Counter({"arcanetech-ai/billing": 4}))
    assert cands[0].repo == "arcanetech-ai/billing" and "asked about it 4 times" in cands[0].reasons[-1]


def _kb(profiles, client=None):
    store = Store()
    kb = KnowledgeBase(GitHub(PatAuth("t")), store, (lambda: client) if client else None)
    for p in profiles:
        kb.save(p)
    return kb, store


def test_resolve_by_name_keywords_model_or_ask():
    kb, _ = _kb([PORTFOLIO, SANDBOX, BILLING])
    repos = [PORTFOLIO.repo, SANDBOX.repo, BILLING.repo]
    named = kb.resolve("add a contact form to my portfolio", repos)
    assert (named.repo, named.method) == (PORTFOLIO.repo, "named")
    kw = kb.resolve("make the Hero component animate on load", repos)
    assert (kw.repo, kw.method) == (PORTFOLIO.repo, "keywords")
    unclear = kb.resolve("improve performance", repos)
    assert unclear.repo == "" and unclear.method == "ask"


def test_close_calls_go_to_one_haiku_call():
    client = FakeClient([turn(text(json.dumps({"repo": BILLING.repo, "reason": "orders totals live in billing"})), model="claude-haiku-5-5")])
    kb, _ = _kb([SANDBOX, BILLING], client)
    res = kb.resolve("fix rounding in order totals", [SANDBOX.repo, BILLING.repo])
    assert (res.repo, res.method, res.reason) == (BILLING.repo, "model", "orders totals live in billing")
    call = client.calls[0]
    assert call["model"] == "claude-haiku-5-5" and call["tools"] == [] and "fallbacks" not in call


def test_model_answer_outside_the_candidates_is_ignored():
    client = FakeClient([turn(text(json.dumps({"repo": "evil/repo", "reason": "x"})))])
    kb, _ = _kb([SANDBOX, BILLING], client)
    assert kb.resolve("fix rounding in order totals", [SANDBOX.repo, BILLING.repo]).method == "ask"


def test_build_profile_from_the_github_api():
    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/repos/arcanetech-ai/portfolio":
            return httpx.Response(200, json={"description": "Portfolio", "topics": ["react"], "default_branch": "main"})
        if p.endswith("/languages"):
            return httpx.Response(200, json={"CSS": 100, "TypeScript": 900})
        if p.endswith("/readme"):
            return httpx.Response(200, json={"content": base64.b64encode(b"# hello").decode()})
        if p.endswith("/git/trees/main"):
            return httpx.Response(200, json={"tree": [{"path": "src", "type": "tree"}, {"path": "src/App.tsx", "type": "blob"}]})
        return httpx.Response(404, json={"message": "nope"})

    gh = GitHub(PatAuth("t"), http=httpx.Client(base_url=API, transport=httpx.MockTransport(handler)))
    prof = build_profile(gh, "arcanetech-ai/portfolio")
    assert (prof.description, prof.languages, prof.readme, prof.paths) == ("Portfolio", ["TypeScript", "CSS"], "# hello", ["src/App.tsx"])


def test_intake_picks_the_repo_when_the_task_names_none(tmp_path):
    from tests.test_intake import ASHA, FakeChannel, _mention

    class NoPlan:
        def submit(self, *a, **k):
            pass

    from pullwright.intake import Intake

    kb, store = _kb([PORTFOLIO, SANDBOX])
    store.link_identity("slack", "U1", "asha", "Asha Rao")
    intake = Intake(store, GitHub(PatAuth("t")), lambda: None, tmp_path, allowed_repos={PORTFOLIO.repo, SANDBOX.repo}, knowledge=kb)
    intake._pool = NoPlan()  # do not start planning; this test is about choosing the repository
    ch = FakeChannel()
    intake.register(ch)
    rid = intake.on_mention(_mention(ASHA, "add a contact form to my portfolio"))
    assert store.request(rid)["repo"] == PORTFOLIO.repo
    assert ch.replies[0][1].startswith("Picked `arcanetech-ai/portfolio` (the task names it)")
    # nothing in the task matches: history alone does not pick, it orders the question
    assert intake.on_mention(_mention(ASHA, "improve performance")) is None
    assert ch.replies[-1][1].startswith("Which repository? It could be `arcanetech-ai/portfolio`")
