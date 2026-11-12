from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pullwright.channels.base import ChatUser, PlanCard
from pullwright.store import Store
from pullwright.web import COOKIE, Signer, WebChannel, build_router


class StubIntake:
    def __init__(self, store: Store) -> None:
        self.store, self.mentions, self.decisions, self.links = store, [], [], []

    def on_mention(self, m):
        self.mentions.append(m)
        self.store.create_request("r1", "web", "arcane/app", "add hello", m.user.github_login, m.user.name, m.thread_ref)
        return "r1"

    def on_decision(self, d):
        self.decisions.append(d)
        return "Approved. Opening the issue."

    def complete_link(self, token, login):
        self.links.append((token, login))
        return ChatUser("slack", "U1", "Asha", login)


def _github(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/login/oauth/access_token":
        return httpx.Response(200, json={"access_token": "gho_x"})
    if req.url.path == "/user":
        return httpx.Response(200, json={"login": "asha", "name": "Asha Rao"})
    return httpx.Response(404)


def _client(teams=None):
    store = Store()
    intake = StubIntake(store)
    signer = Signer(b"s3cret")
    app = FastAPI()
    auth, api = build_router(intake, store, signer, teams, ("cid", "csecret"), "https://pw.example",
                             http=httpx.Client(transport=httpx.MockTransport(_github)))
    app.include_router(auth)
    app.include_router(api, prefix="/api")
    return TestClient(app, base_url="https://pw.example"), intake, store, signer


def test_api_needs_a_login():
    c, *_ = _client()
    assert c.get("/api/me").status_code == 401
    assert c.post("/api/requests", json={"repo": "a/b", "task": "x"}).status_code == 401


def test_github_login_sets_a_signed_session_cookie():
    c, _, _, signer = _client()
    r = c.get("/auth/github/login", params={"next": "/queue"}, follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["client_id"] == ["cid"] and q["redirect_uri"] == ["https://pw.example/auth/github/callback"]
    r = c.get("/auth/github/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert r.headers["location"] == "/queue"
    assert signer.loads(r.cookies[COOKIE])["login"] == "asha"
    assert c.get("/api/me").json() == {"login": "asha", "name": "Asha Rao"}
    assert c.get("/auth/github/callback", params={"code": "c", "state": "forged.sig"}, follow_redirects=False).status_code == 400


def test_link_page_binds_the_chat_user_through_login():
    c, intake, *_ = _client()
    r = c.get("/link", params={"token": "tok123"}, follow_redirects=False)
    r = c.get(r.headers["location"], follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = c.get("/auth/github/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert intake.links == [("tok123", "asha")] and r.headers["location"] == "/?linked=slack"


def _logged_in(c, signer):
    c.cookies.set(COOKIE, signer.dumps({"login": "asha", "name": "Asha Rao"}, 3600))


def test_web_request_thread_and_decision():
    c, intake, store, signer = _client()
    _logged_in(c, signer)
    r = c.post("/api/requests", json={"repo": "arcane/app", "task": "add hello"}).json()
    m = intake.mentions[0]
    assert m.user == ChatUser("web", "asha", "Asha Rao", "asha") and m.text == "arcane/app: add hello"
    WebChannel(store).show_plan({"thread": r["thread"]}, PlanCard("r1", "arcane/app", "add hello", "1. do it", 0, 0.05))
    t = c.get(f"/api/threads/{r['thread']}").json()
    assert [msg["kind"] for msg in t["messages"]] == ["user", "plan"] and t["request"]["id"] == "r1"
    assert c.post("/api/requests/r1/decision", json={"decision": "approve"}).json() == {"message": "Approved. Opening the issue."}
    assert intake.decisions[0].decision == "approve" and intake.decisions[0].user.github_login == "asha"
    assert c.post("/api/requests/r1/decision", json={"decision": "merge"}).status_code == 422
    assert [x["id"] for x in c.get("/api/requests").json()] == ["r1"]


class StubTeams:
    def __init__(self) -> None:
        self.handled = []
        self.auth = self

    def verify(self, authorization, service_url):
        from pullwright.channels.teams import AuthError

        if authorization != "Bearer good":
            raise AuthError("bad token")
        return {}

    def handle_activity(self, activity):
        self.handled.append(activity)


def test_teams_webhook_rejects_unsigned_calls():
    teams = StubTeams()
    c, *_ = _client(teams)
    act = {"type": "message", "serviceUrl": "https://smba.example"}
    assert c.post("/api/teams/messages", json=act).status_code == 401
    assert c.post("/api/teams/messages", json=act, headers={"Authorization": "Bearer good"}).status_code == 200
    assert teams.handled == [act]


def test_a_thread_without_a_live_task_takes_another_message():
    c, intake, store, signer = _client()
    c.cookies.set(COOKIE, signer.dumps({"login": "asha", "name": "Asha Rao"}, 3600))
    intake.on_mention = lambda m: None  # e.g. "I'm not set up for that repository": no request is created
    t = c.post("/api/requests", json={"repo": "other/repo", "task": "x"}).json()
    assert t["request_id"] is None
    again = c.post("/api/requests", json={"repo": "arcane/app", "task": "y", "thread": t["thread"]}).json()
    assert again["thread"] == t["thread"] and [m["body"]["text"] for m in store.messages(t["thread"])] == ["other/repo: x", "arcane/app: y"]
    c.cookies.set(COOKIE, signer.dumps({"login": "vik", "name": "Vik"}, 3600))
    assert c.post("/api/requests", json={"task": "z", "thread": t["thread"]}).status_code == 404  # not their thread


class _Perms:
    """GitHub permissions for the access tests: login -> role on every repository."""
    def __init__(self, roles):
        self.roles = roles

    def permission(self, repo, login):
        return self.roles.get(login, "none")


def test_threads_are_visible_to_the_requester_and_repo_readers_only():
    c, intake, store, signer = _client()
    _logged_in(c, signer)  # asha makes the request
    r = c.post("/api/requests", json={"repo": "arcane/app", "task": "add hello"}).json()
    intake.gh = _Perms({"ravi": "read", "mei": "write"})
    assert c.get(f"/api/threads/{r['thread']}").json()["can_post"] is True

    def as_(login):
        c.cookies.set(COOKIE, signer.dumps({"login": login, "name": login}, 3600))

    as_("eve")  # no access to arcane/app: the thread does not exist for her
    assert c.get(f"/api/threads/{r['thread']}").status_code == 404
    assert c.get("/api/requests/r1/activity").status_code == 404
    assert c.post("/api/requests/r1/decision", json={"decision": "cancel"}).status_code == 404
    assert c.get("/api/queue").json() == [] or all(x["id"] != "r1" for x in c.get("/api/queue").json())

    as_("ravi")  # can read the repository: sees the thread, can't post to it or cancel the plan
    t = c.get(f"/api/threads/{r['thread']}").json()
    assert t["request"]["id"] == "r1" and t["can_post"] is False
    assert c.get("/api/requests/r1/activity").status_code == 200
    assert c.post("/api/requests/r1/decision", json={"decision": "cancel"}).status_code == 403
    assert not intake.decisions

    as_("mei")  # write access: may revise or cancel
    assert c.post("/api/requests/r1/decision", json={"decision": "revise", "feedback": "smaller"}).status_code == 200
    assert intake.decisions[-1].user.github_login == "mei"
