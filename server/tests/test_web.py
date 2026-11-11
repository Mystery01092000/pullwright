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
    app.include_router(build_router(intake, store, signer, teams, ("cid", "csecret"), "https://pw.example",
                                    http=httpx.Client(transport=httpx.MockTransport(_github))))
    return TestClient(app, base_url="https://pw.example"), intake, store, signer


def test_api_needs_a_login():
    c, *_ = _client()
    assert c.get("/me").status_code == 401
    assert c.post("/requests", json={"repo": "a/b", "task": "x"}).status_code == 401


def test_github_login_sets_a_signed_session_cookie():
    c, _, _, signer = _client()
    r = c.get("/auth/github/login", params={"next": "/queue"}, follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["client_id"] == ["cid"] and q["redirect_uri"] == ["https://pw.example/auth/github/callback"]
    r = c.get("/auth/github/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert r.headers["location"] == "/queue"
    assert signer.loads(r.cookies[COOKIE])["login"] == "asha"
    assert c.get("/me").json() == {"login": "asha", "name": "Asha Rao"}
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
    r = c.post("/requests", json={"repo": "arcane/app", "task": "add hello"}).json()
    m = intake.mentions[0]
    assert m.user == ChatUser("web", "asha", "Asha Rao", "asha") and m.text == "arcane/app: add hello"
    WebChannel(store).show_plan({"thread": r["thread"]}, PlanCard("r1", "arcane/app", "add hello", "1. do it", 0, 0.05))
    t = c.get(f"/threads/{r['thread']}").json()
    assert [msg["kind"] for msg in t["messages"]] == ["user", "plan"] and t["request"]["id"] == "r1"
    assert c.post("/requests/r1/decision", json={"decision": "approve"}).json() == {"message": "Approved. Opening the issue."}
    assert intake.decisions[0].decision == "approve" and intake.decisions[0].user.github_login == "asha"
    assert c.post("/requests/r1/decision", json={"decision": "merge"}).status_code == 422
    assert [x["id"] for x in c.get("/requests").json()] == ["r1"]


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
    assert c.post("/teams/messages", json=act).status_code == 401
    assert c.post("/teams/messages", json=act, headers={"Authorization": "Bearer good"}).status_code == 200
    assert teams.handled == [act]
