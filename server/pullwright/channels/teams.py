"""Microsoft Teams, over the Bot Framework REST protocol (no SDK).

Inbound: Teams POSTs activities to /api/teams/messages with a bearer JWT signed by the Bot Framework; we verify
it against the published signing keys and our app id. Outbound: we get an app token from Entra ID (client
credentials) and POST replies to the conversation's serviceUrl. The plan is an Adaptive Card whose Submit
actions come back as a message activity carrying `value`.

Env: PULLWRIGHT_TEAMS_APP_ID, PULLWRIGHT_TEAMS_APP_PASSWORD (client secret), PULLWRIGHT_TEAMS_TENANT_ID
(single-tenant bots; omit for multi-tenant). Map a channel to a repo with PULLWRIGHT_TEAMS_REPOS='{"19:...": "arcane/app"}'.
Setup: docs/setup-teams.md.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .base import ChatUser, Decision, Mention, PlanCard

OPENID_CONFIG = "https://login.botframework.com/v1/.well-known/openidconfiguration"
BOT_ISSUERS = ("https://api.botframework.com",)
AT_TAG = re.compile(r"<at>.*?</at>", re.S)
TAG = re.compile(r"<[^>]+>")


class AuthError(Exception):
    pass


@dataclass
class BotAuth:
    app_id: str
    app_password: str
    tenant_id: str = ""  # single-tenant bot; empty = multi-tenant (botframework.com)
    http: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=30))
    _token: tuple[str, float] = ("", 0.0)
    _jwks: Any = None

    def token(self) -> str:
        """App token for calling the Bot Connector (cached until shortly before expiry)."""
        if self._token[1] - time.time() > 300:
            return self._token[0]
        tenant = self.tenant_id or "botframework.com"
        r = self.http.post(f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", data={
            "grant_type": "client_credentials", "client_id": self.app_id, "client_secret": self.app_password,
            "scope": "https://api.botframework.com/.default"})
        r.raise_for_status()
        data = r.json()
        self._token = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
        return self._token[0]

    def verify(self, authorization: str, service_url: str) -> dict[str, Any]:
        """Check that an inbound request really comes from the Bot Framework, for this bot."""
        import jwt

        if not authorization.startswith("Bearer "):
            raise AuthError("missing bearer token")
        if self._jwks is None:
            jwks_uri = self.http.get(OPENID_CONFIG).json()["jwks_uri"]
            self._jwks = jwt.PyJWKClient(jwks_uri)
        raw = authorization[7:]
        try:
            key = self._jwks.get_signing_key_from_jwt(raw).key
            claims = jwt.decode(raw, key, algorithms=["RS256"], audience=self.app_id, issuer=list(BOT_ISSUERS), leeway=300)
        except jwt.PyJWTError as e:
            raise AuthError(str(e)) from e
        if claims.get("serviceurl") and claims["serviceurl"].rstrip("/") != service_url.rstrip("/"):
            raise AuthError("serviceUrl does not match the token")
        return claims


def clean_text(text: str) -> str:
    return html.unescape(TAG.sub("", AT_TAG.sub("", text or ""))).strip()


def plan_card(card: PlanCard) -> dict[str, Any]:
    head = f"Plan for {card.repo}" + (f" (revision {card.revision})" if card.revision else "")
    return {
        "type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "version": "1.5",
        "body": [
            {"type": "TextBlock", "text": head, "weight": "Bolder", "size": "Medium", "wrap": True},
            {"type": "TextBlock", "text": f"Planning cost ${card.cost_usd:.2f}", "isSubtle": True, "spacing": "None"},
            {"type": "TextBlock", "text": card.plan_markdown[:20000], "wrap": True},
            {"type": "Input.Text", "id": "feedback", "isMultiline": True, "placeholder": "What should change? (for Revise)"},
        ],
        "actions": [
            {"type": "Action.Submit", "title": "Approve", "style": "positive", "data": {"pw": "approve", "request_id": card.request_id}},
            {"type": "Action.Submit", "title": "Revise", "data": {"pw": "revise", "request_id": card.request_id}},
            {"type": "Action.Submit", "title": "Cancel", "style": "destructive", "data": {"pw": "cancel", "request_id": card.request_id}},
        ],
    }


class TeamsChannel:
    name = "teams"

    def __init__(self, intake: Any, auth: BotAuth, channel_repos: dict[str, str] | None = None, http: httpx.Client | None = None) -> None:
        self.intake = intake
        self.auth = auth
        self.channel_repos = channel_repos or {}
        self.http = http or httpx.Client(timeout=30)

    # --- outbound -------------------------------------------------------------------------------

    def _post(self, thread_ref: dict[str, Any], activity: dict[str, Any]) -> None:
        url = f"{thread_ref['service_url'].rstrip('/')}/v3/conversations/{thread_ref['conversation_id']}/activities"
        if thread_ref.get("activity_id"):
            url += f"/{thread_ref['activity_id']}"
        r = self.http.post(url, json=activity, headers={"Authorization": f"Bearer {self.auth.token()}"})
        r.raise_for_status()

    def reply(self, thread_ref: dict[str, Any], text: str) -> None:
        self._post(thread_ref, {"type": "message", "text": text, "textFormat": "markdown"})

    def show_plan(self, thread_ref: dict[str, Any], card: PlanCard) -> None:
        self._post(thread_ref, {"type": "message", "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": plan_card(card)}]})

    # --- inbound --------------------------------------------------------------------------------

    def handle_activity(self, activity: dict[str, Any]) -> str | None:
        if activity.get("type") != "message":
            return None  # conversationUpdate and the like need no answer
        frm = activity.get("from", {})
        user = ChatUser("teams", frm.get("aadObjectId") or frm.get("id", ""), frm.get("name", ""))
        thread = {"service_url": activity["serviceUrl"], "conversation_id": activity["conversation"]["id"], "activity_id": activity.get("id", "")}
        value = activity.get("value") or {}
        if value.get("pw") in ("approve", "revise", "cancel"):
            msg = self.intake.on_decision(Decision(value["request_id"], value["pw"], user, value.get("feedback", "") or ""))
            self.reply(thread, msg)
            return msg
        channel_id = (activity.get("channelData") or {}).get("channel", {}).get("id", "")
        key = channel_id or activity["conversation"]["id"].split(";")[0]
        return self.intake.on_mention(Mention(user, clean_text(activity.get("text", "")), thread, "", self.channel_repos.get(key, "")))
