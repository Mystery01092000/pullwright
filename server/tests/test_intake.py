import json
import threading
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from pullwright.channels.base import ChatUser, Decision, Mention, PlanCard
from pullwright.channels.slack import SlackChannel, plan_blocks
from pullwright.channels.teams import AuthError, BotAuth, TeamsChannel, clean_text
from pullwright.intake import Intake, LinkSigner, split_repo
from pullwright.store import Store
from tests.fake_client import FakeClient, text, turn
from tests.fake_github import FakeGitHub
from tests.test_runner import LocalGitHub, remote  # noqa: F401  (fixture)

SPEC = {"summary": "Add hello()", "relevant_files": [{"path": "app.py", "why": "target"}], "plan": ["add hello"],
        "acceptance_criteria": ["hello() returns 'hi'"], "test_plan": ["checks"], "risks": [], "questions": []}
OK = json.dumps({"approved": True, "corrections": [], "evidence": "read app.py"})


def test_split_repo():
    assert split_repo("arcane/app: add rate limiting") == ("arcane/app", "add rate limiting")
    assert split_repo("in arcane/app add a /health route") == ("arcane/app", "add a /health route")
    assert split_repo("add caching", "arcane/api") == ("arcane/api", "add caching")
    assert split_repo("add caching") == ("", "add caching")
    # slashed words are not repositories unless they lead the message or are configured
    known = {"arcane/app", "arcane/api"}
    msg = "margin is long only, so drop the long/short selector"
    assert split_repo(msg, "arcane/app", known) == ("arcane/app", msg)
    assert split_repo("long/short positions are fine", "arcane/app", known) == ("arcane/app", "long/short positions are fine")
    assert split_repo("also update src/app.ts in arcane/api", "arcane/app", known) == ("arcane/api", "also update src/app.ts in")
    assert split_repo("other/repo: add hello", "", known) == ("other/repo", "add hello")  # explicit: rejected later
    assert split_repo("in Arcane/App add a route", "", known) == ("Arcane/App", "add a route")


def test_link_tokens_are_signed_and_expire():
    s = LinkSigner(b"secret")
    tok = s.sign("slack", "U1", "Asha")
    assert s.verify(tok) == ("slack", "U1", "Asha")
    import base64
    raw = base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4)).decode()
    forged = base64.urlsafe_b64encode(raw.replace("U1", "U2").encode()).decode().rstrip("=")
    assert s.verify(forged) is None  # tampered user id, old signature
    assert s.verify("not-a-token") is None
    assert s.verify(s.sign("slack", "U1", "Asha", ttl_s=-1)) is None  # expired
    assert LinkSigner(b"other").verify(tok) is None


class FakeChannel:
    name = "slack"

    def __init__(self) -> None:
        self.replies: list[tuple[dict, str]] = []
        self.cards: list[PlanCard] = []
        self.changed = threading.Event()

    def reply(self, thread_ref, text):
        self.replies.append((thread_ref, text))
        self.changed.set()

    def show_plan(self, thread_ref, card):
        self.cards.append(card)
        self.changed.set()

    def wait_for(self, predicate, timeout=20.0):
        deadline = time.monotonic() + timeout
        while not predicate():
            assert time.monotonic() < deadline, f"timed out; replies={self.replies}"
            self.changed.wait(0.05)
            self.changed.clear()


ASHA = ChatUser("slack", "U1", "Asha Rao")
VIEWER = ChatUser("slack", "U2", "Vik")
THREAD = {"channel": "C1", "ts": "1.1"}


@pytest.fixture
def setup(tmp_path: Path, remote: Path):  # noqa: F811
    fake = FakeGitHub()
    gh = LocalGitHub(fake, remote)
    store = Store()
    turns = [turn(text(json.dumps(SPEC))), turn(text(OK)), turn(text(json.dumps({**SPEC, "summary": "Add hello() with logging"}))), turn(text(OK))]
    client = FakeClient(turns)
    intake = Intake(store, gh, lambda: client, tmp_path / "clones", link_url=lambda t: f"https://pw.example/link?token={t}")
    ch = FakeChannel()
    intake.register(ch)
    return intake, ch, store, fake, client


def _mention(user, text_, thread=THREAD):
    return Mention(user, text_, dict(thread), "https://slack.example/p1")


def test_unlinked_user_is_asked_to_link_and_the_link_binds_github(setup):
    intake, ch, store, *_ = setup
    assert intake.on_mention(_mention(ASHA, "arcane/app: add hello")) is None
    link = ch.replies[-1][1]
    assert "https://pw.example/link?token=" in link
    token = link.split("token=")[1].split(" ")[0]
    assert intake.complete_link(token, "asha").github_login == "asha"
    assert store.identity("slack", "U1")["github_login"] == "asha"


def test_mention_plans_waits_for_approval_and_opens_the_issue(setup):
    intake, ch, store, fake, client = setup
    store.link_identity("slack", "U1", "asha", "Asha Rao")
    store.link_identity("slack", "U2", "viewer", "Vik")
    rid = intake.on_mention(_mention(ASHA, "arcane/app: add hello"))
    ch.wait_for(lambda: ch.cards)
    card = ch.cards[0]
    assert card.request_id == rid and card.repo == "arcane/app" and "1. add hello" in card.plan_markdown
    assert store.request(rid)["state"] == "awaiting_approval"
    # a read-only collaborator cannot approve
    assert "write access" in intake.on_decision(Decision(rid, "approve", VIEWER))
    # revise with feedback -> a new plan card
    intake.on_decision(Decision(rid, "revise", ASHA, "also log the call"))
    ch.wait_for(lambda: len(ch.cards) == 2)
    assert ch.cards[1].revision == 1 and "logging" in ch.cards[1].plan_markdown
    assert "also log the call" in client.calls[2]["messages"][0]["content"]
    intake.on_decision(Decision(rid, "approve", ASHA))
    ch.wait_for(lambda: any("Issue opened" in r for _, r in ch.replies))
    req = store.request(rid)
    assert req["state"] == "queued" and req["issue_number"] == 1 and req["plan"]["summary"].startswith("Add hello() with logging")
    issue = fake.issues[1]
    assert "pullwright:queued" in issue["labels"] and issue["assignees"] == ["asha"]
    assert "Requested by** Asha Rao (@asha) via slack" in issue["body"]
    assert all(t == THREAD for t, _ in ch.replies)  # every reply went to the task's thread

    intake.on_job_event("arcane/app", 1, {"type": "usage", "payload": {}})
    assert store.request(rid)["state"] == "queued"
    intake.on_job_event("arcane/app", 1, {"type": "state", "payload": {"state": "setup"}})
    assert store.request(rid)["state"] == "working"  # the web Queue shows the runner on it
    planned = store.request(rid)
    assert planned["costs"]["by_stage"]["plan"]["calls"] >= 2 and planned["costs"]["runs"][0]["kind"] == "plan"
    work = {"total_usd": 0.25, "calls": 3, "by_stage": {"work": {"cost_usd": 0.25, "calls": 3, "input_tokens": 1, "output_tokens": 1}},
            "by_model": {}, "unpriced": []}
    intake.on_job_done("arcane/app", 1, {"status": "green", "pr_url": "https://github.com/arcane/app/pull/100", "cost_usd": 0.25, "costs": work},
                       {"spent_usd": 0.25, "calls": 3, "by_model": {}})
    assert "Pull request for #1: https://github.com/arcane/app/pull/100. All checks pass" in ch.replies[-1][1]
    done = store.request(rid)
    assert done["state"] == "pr_open" and abs(done["cost_usd"] - (planned["cost_usd"] + 0.25)) < 1e-9
    assert done["costs"]["runs"][-1] == {**done["costs"]["runs"][-1], "kind": "issue", "status": "green", "proxy_usd": 0.25}


def test_several_tasks_from_one_chat_are_separate_requests(setup):
    intake, ch, store, *_ = setup
    store.link_identity("slack", "U1", "asha", "Asha Rao")
    a = intake.on_mention(_mention(ASHA, "arcane/app: add hello", {"channel": "C1", "ts": "1.1"}))
    b = intake.on_mention(_mention(ASHA, "arcane/app: add bye", {"channel": "C1", "ts": "2.2"}))
    assert a != b and {r["id"] for r in store.requests("asha")} == {a, b}
    ch.wait_for(lambda: len(ch.cards) == 2)
    intake.on_decision(Decision(b, "cancel", ASHA, "not now"))
    intake.on_decision(Decision(a, "cancel", ASHA))
    ch.wait_for(lambda: store.request(a)["state"] == "cancelled" and store.request(b)["state"] == "cancelled")


def test_missing_repo_and_disallowed_repo_are_answered(setup):
    intake, ch, store, *_ = setup
    store.link_identity("slack", "U1", "asha", "Asha Rao")
    assert intake.on_mention(_mention(ASHA, "add hello")) is None and "Which repository?" in ch.replies[-1][1]
    intake.allowed_repos = {"arcane/app"}
    assert intake.on_mention(_mention(ASHA, "other/repo: add hello")) is None and "not set up for `other/repo`" in ch.replies[-1][1]


# --- Slack ------------------------------------------------------------------

class FakeSlack:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        def call(**kw):
            self.calls.append((name, kw))
            if name == "users_info":
                return {"user": {"real_name": "Asha Rao"}}
            if name == "chat_getPermalink":
                return {"permalink": "https://slack.example/p1"}
            return {"ok": True}
        return call


class RecordingIntake:
    def __init__(self) -> None:
        self.mentions: list[Mention] = []
        self.decisions: list[Decision] = []

    def on_mention(self, m):
        self.mentions.append(m)
        return "rid"

    def on_decision(self, d):
        self.decisions.append(d)
        return "ok"


def test_slack_mention_strips_the_bot_and_uses_the_channel_repo():
    intake, client = RecordingIntake(), FakeSlack()
    ch = SlackChannel(intake, client, {"C9": "arcane/app"})
    ch.handle_mention({"channel": "C9", "user": "U1", "ts": "5.5", "text": "<@UBOT> add a /health route"})
    m = intake.mentions[0]
    assert m.text == "add a /health route" and m.default_repo == "arcane/app" and m.thread_ref == {"channel": "C9", "ts": "5.5"}
    assert m.user == ChatUser("slack", "U1", "Asha Rao") and m.thread_url == "https://slack.example/p1"


def test_slack_buttons_and_revise_modal():
    intake, client = RecordingIntake(), FakeSlack()
    ch = SlackChannel(intake, client)
    body = {"user": {"id": "U1"}, "channel": {"id": "C9"}, "trigger_id": "T1", "message": {"ts": "5.5"}}
    ch.handle_action({**body, "actions": [{"action_id": "pw_approve", "value": "r1"}]})
    assert intake.decisions[-1].decision == "approve" and intake.decisions[-1].request_id == "r1"
    ch.handle_action({**body, "actions": [{"action_id": "pw_revise", "value": "r1"}]})
    assert client.calls[-1][0] == "views_open" and client.calls[-1][1]["view"]["private_metadata"] == "r1"
    ch.handle_revise_submit({"user": {"id": "U1"}}, {"private_metadata": "r1", "state": {"values": {"feedback": {"text": {"value": "smaller"}}}}})
    assert intake.decisions[-1].decision == "revise" and intake.decisions[-1].feedback == "smaller"


def test_slack_plan_card_has_the_three_actions_and_fits_block_limits():
    card = PlanCard("r1", "arcane/app", "task", "**Plan**\n" + "x" * 7000, 1, 0.12)
    blocks = plan_blocks(card)
    assert all(len(b["text"]["text"]) <= 3000 for b in blocks if b["type"] == "section")
    assert [e["action_id"] for e in blocks[-1]["elements"]] == ["pw_approve", "pw_revise", "pw_cancel"]
    assert "*Plan*" in blocks[1]["text"]["text"]  # markdown bold -> Slack bold


# --- Teams ------------------------------------------------------------------

def _teams(intake) -> tuple[TeamsChannel, list[httpx.Request]]:
    sent: list[httpx.Request] = []
    auth = BotAuth("app-id", "secret")
    auth._token = ("bot-token", time.time() + 3600)
    http = httpx.Client(transport=httpx.MockTransport(lambda r: (sent.append(r), httpx.Response(200, json={}))[1]))
    return TeamsChannel(intake, auth, {"19:chan": "arcane/app"}, http=http), sent


ACTIVITY = {"type": "message", "id": "act1", "serviceUrl": "https://smba.example/amer/", "text": "<at>Pullwright</at> add a /health route",
            "from": {"id": "29:x", "aadObjectId": "aad-1", "name": "Asha Rao"}, "conversation": {"id": "19:chan;messageid=1"},
            "channelData": {"channel": {"id": "19:chan"}}}


def test_teams_mention_and_card_submit():
    intake = RecordingIntake()
    ch, sent = _teams(intake)
    ch.handle_activity(ACTIVITY)
    m = intake.mentions[0]
    assert m.text == "add a /health route" and m.default_repo == "arcane/app" and m.user.user_id == "aad-1"
    ch.handle_activity({**ACTIVITY, "text": "", "value": {"pw": "revise", "request_id": "r1", "feedback": "smaller"}})
    assert intake.decisions[-1] == Decision("r1", "revise", ChatUser("teams", "aad-1", "Asha Rao"), "smaller")
    req = sent[-1]
    assert str(req.url) == "https://smba.example/amer/v3/conversations/19:chan;messageid=1/activities/act1"
    assert req.headers["Authorization"] == "Bearer bot-token"


def test_teams_plan_is_an_adaptive_card():
    ch, sent = _teams(RecordingIntake())
    ch.show_plan({"service_url": "https://smba.example", "conversation_id": "c1", "activity_id": ""}, PlanCard("r1", "arcane/app", "t", "plan", 0, 0.1))
    att = json.loads(sent[0].content)["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert [a["data"]["pw"] for a in att["content"]["actions"]] == ["approve", "revise", "cancel"]


def test_teams_verifies_the_bot_framework_token():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class Keys:
        def get_signing_key_from_jwt(self, _raw):
            return type("K", (), {"key": key.public_key()})()

    auth = BotAuth("app-id", "secret")
    auth._jwks = Keys()
    now = int(time.time())
    good = jwt.encode({"iss": "https://api.botframework.com", "aud": "app-id", "iat": now, "exp": now + 600,
                       "serviceurl": "https://smba.example/amer/"}, key, algorithm="RS256")
    assert auth.verify(f"Bearer {good}", "https://smba.example/amer")["aud"] == "app-id"
    wrong_aud = jwt.encode({"iss": "https://api.botframework.com", "aud": "other", "iat": now, "exp": now + 600}, key, algorithm="RS256")
    with pytest.raises(AuthError):
        auth.verify(f"Bearer {wrong_aud}", "https://smba.example/amer")
    with pytest.raises(AuthError):
        auth.verify(f"Bearer {good}", "https://evil.example")
    with pytest.raises(AuthError):
        auth.verify("", "https://smba.example")


def test_clean_text():
    assert clean_text("<at>Pullwright</at> add <b>x</b> &amp; y") == "add x & y"


# --- conversation: answers, chat about a waiting plan, typed decisions ------------------------

def _chat_intake(tmp_path, remote, turns):  # noqa: F811
    fake = FakeGitHub()
    store = Store()
    client = FakeClient(turns)
    intake = Intake(store, LocalGitHub(fake, remote), lambda: client, tmp_path / "clones")
    ch = FakeChannel()
    intake.register(ch)
    store.link_identity("slack", "U1", "asha", "Asha Rao")
    return intake, ch, store, client


def _says(intent, reply, task=""):
    return turn(text(json.dumps({"intent": intent, "reply": reply, "task": task})))


def test_quick_intent():
    from pullwright.intake import quick_intent
    assert quick_intent("add a /health route", False, True) == ("change", "add a /health route")
    assert quick_intent("Give me the summary of this project", False, True) is None  # a question: the responder answers
    assert quick_intent("add that too", False, False) is None  # mid-conversation: the responder restates the task
    assert quick_intent("fix it?", False, True) is None
    assert quick_intent("LGTM!", True, False) == ("approve", "")
    assert quick_intent("cancel", True, False) == ("cancel", "")
    assert quick_intent("revise: also log it", True, False) == ("revise", "also log it")
    assert quick_intent("what does step 2 do?", True, False) is None


def test_a_question_is_answered_without_a_plan_or_an_issue(tmp_path, remote):  # noqa: F811
    intake, ch, store, client = _chat_intake(tmp_path, remote, [_says("answer", "A tiny app.\n\n- `app.py`: the entry point")])
    rid = intake.on_mention(_mention(ASHA, "arcane/app: give me the summary of this project"))
    ch.wait_for(lambda: store.request(rid)["state"] == "answered")
    assert ch.replies[-1][1].startswith("A tiny app.") and not ch.cards
    req = store.request(rid)
    assert req["issue_number"] is None and req["costs"]["runs"][-1]["kind"] == "chat"
    assert client.calls[0]["output_config"]["format"]["schema"]["properties"]["intent"]["enum"][0] == "answer"


def test_chat_then_confirm_plans_answers_about_the_plan_and_approves_by_text(tmp_path, remote):  # noqa: F811
    turns = [_says("answer", "It prints a greeting."),
             _says("change", "I'll plan that.", "Add hello() to app.py that returns 'hi', with a test"),
             turn(text(json.dumps(SPEC))), turn(text(OK)),
             _says("answer", "It checks that hello() returns 'hi'.")]
    intake, ch, store, client = _chat_intake(tmp_path, remote, turns)
    first = intake.on_mention(_mention(ASHA, "arcane/app: what does app.py do?"))
    ch.wait_for(lambda: store.request(first)["state"] == "answered")
    # "do it" mid-conversation: the responder turns the thread into a full task, which is planned
    rid = intake.on_mention(_mention(ASHA, "ok, add a hello function then"))
    assert rid != first
    ch.wait_for(lambda: ch.cards)
    assert "Developer: arcane/app: what does app.py do?" in client.calls[1]["messages"][0]["content"]
    assert store.request(rid)["task"] == "Add hello() to app.py that returns 'hi', with a test"
    assert "Add hello() to app.py" in client.calls[2]["messages"][0]["content"]  # the describer got the restated task
    # a question about the waiting plan is answered; the plan keeps waiting
    assert intake.on_mention(_mention(ASHA, "what will the test check?")) == rid
    ch.wait_for(lambda: any("checks that hello()" in r for _, r in ch.replies))
    assert "Plan waiting for the developer's approval" in client.calls[4]["messages"][0]["content"]
    assert store.request(rid)["state"] == "awaiting_approval"
    # a typed approval goes through the same checks as the button
    intake.on_mention(_mention(ASHA, "looks good"))
    ch.wait_for(lambda: store.request(rid)["state"] == "queued")
    assert store.request(rid)["issue_number"] == 1 and len(client.calls) == 5  # "looks good" needed no LLM call


def test_a_message_while_the_plan_is_drafted_is_held_back(tmp_path, remote):  # noqa: F811
    intake, ch, store, client = _chat_intake(tmp_path, remote, [])
    store.create_request("r1", "slack", "arcane/app", "add hello", "asha", "Asha Rao", dict(THREAD))  # state: planning
    assert intake.on_mention(_mention(ASHA, "and also bye")) is None
    assert "still drafting" in ch.replies[-1][1] and not client.calls


def test_recover_closes_plans_orphaned_by_a_restart(tmp_path, remote):  # noqa: F811
    intake, ch, store, _ = _chat_intake(tmp_path, remote, [])
    store.create_request("r1", "slack", "arcane/app", "add hello", "asha", "Asha Rao", dict(THREAD))
    store.update_request("r1", state="awaiting_approval")
    store.create_request("r2", "slack", "arcane/app", "add bye", "asha", "Asha Rao", dict(THREAD))
    store.update_request("r2", state="queued")  # on GitHub already: the dispatcher picks it up, nothing to close
    assert intake.recover() == ["r1"]
    assert store.request("r1")["state"] == "cancelled" and store.request("r2")["state"] == "queued"
    assert "restarted" in ch.replies[-1][1]
