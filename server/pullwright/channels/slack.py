"""Slack: tag the bot in a channel or DM it. Socket Mode, so no public URL is needed.

Env: SLACK_BOT_TOKEN (xoxb-...), SLACK_APP_TOKEN (xapp-..., connections:write).
Bot scopes: app_mentions:read, chat:write, users:read, im:history (DMs), commands (optional).
Map a channel to a default repository with PULLWRIGHT_SLACK_REPOS='{"C0123": "arcane/app"}'.

The handlers take plain Slack payloads and a WebClient, so they are tested without Slack; `start()` wires them
into slack-bolt.
"""
from __future__ import annotations

import re
import threading
from typing import Any

from .base import ChatUser, Decision, Mention, PlanCard

MENTION = re.compile(r"<@[A-Z0-9]+>\s*")
SECTION_LIMIT = 2900  # Slack section text limit is 3000 characters


def to_mrkdwn(md: str) -> str:
    """GitHub-flavoured markdown -> Slack mrkdwn (bold, links, checkboxes)."""
    md = re.sub(r"\*\*(.+?)\*\*", r"*\1*", md)
    md = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", md)
    return md.replace("- [ ] ", "• ")


def plan_blocks(card: PlanCard) -> list[dict[str, Any]]:
    text = to_mrkdwn(card.plan_markdown)
    chunks = [text[i:i + SECTION_LIMIT] for i in range(0, len(text), SECTION_LIMIT)] or [""]
    head = f"*Plan for `{card.repo}`*" + (f" (revision {card.revision})" if card.revision else "") + f"  ·  planning cost ${card.cost_usd:.2f}"
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]
    blocks += [{"type": "section", "text": {"type": "mrkdwn", "text": c}} for c in chunks[:45]]
    blocks.append({"type": "actions", "block_id": f"pw_plan_{card.request_id}", "elements": [
        {"type": "button", "action_id": "pw_approve", "style": "primary", "text": {"type": "plain_text", "text": "Approve"}, "value": card.request_id},
        {"type": "button", "action_id": "pw_revise", "text": {"type": "plain_text", "text": "Revise"}, "value": card.request_id},
        {"type": "button", "action_id": "pw_cancel", "style": "danger", "text": {"type": "plain_text", "text": "Cancel"}, "value": card.request_id},
    ]})
    return blocks


def revise_modal(request_id: str) -> dict[str, Any]:
    return {
        "type": "modal", "callback_id": "pw_revise_submit", "private_metadata": request_id,
        "title": {"type": "plain_text", "text": "Revise the plan"}, "submit": {"type": "plain_text", "text": "Send"},
        "blocks": [{"type": "input", "block_id": "feedback", "label": {"type": "plain_text", "text": "What should change?"},
                    "element": {"type": "plain_text_input", "action_id": "text", "multiline": True}}],
    }


class SlackChannel:
    name = "slack"

    def __init__(self, intake: Any, client: Any, channel_repos: dict[str, str] | None = None) -> None:
        self.intake = intake
        self.client = client  # slack_sdk.WebClient
        self.channel_repos = channel_repos or {}
        self._names: dict[str, str] = {}

    # --- outbound -------------------------------------------------------------------------------

    def reply(self, thread_ref: dict[str, Any], text: str) -> None:
        self.client.chat_postMessage(channel=thread_ref["channel"], thread_ts=thread_ref["ts"], text=to_mrkdwn(text))

    def show_plan(self, thread_ref: dict[str, Any], card: PlanCard) -> None:
        self.client.chat_postMessage(channel=thread_ref["channel"], thread_ts=thread_ref["ts"],
                                     text=f"Plan for {card.repo}: approve, revise or cancel", blocks=plan_blocks(card))

    # --- inbound --------------------------------------------------------------------------------

    def user(self, user_id: str) -> ChatUser:
        if user_id not in self._names:
            try:
                info = self.client.users_info(user=user_id)["user"]
                self._names[user_id] = info.get("real_name") or info.get("name") or user_id
            except Exception:
                self._names[user_id] = user_id
        return ChatUser("slack", user_id, self._names[user_id])

    def handle_mention(self, event: dict[str, Any]) -> str | None:
        """app_mention (or a DM): the thread is the message's thread, or the message itself."""
        thread = {"channel": event["channel"], "ts": event.get("thread_ts") or event["ts"]}
        try:
            url = self.client.chat_getPermalink(channel=event["channel"], message_ts=event["ts"])["permalink"]
        except Exception:
            url = ""
        m = Mention(self.user(event["user"]), MENTION.sub("", event.get("text", "")).strip(), thread, url,
                    self.channel_repos.get(event["channel"], ""))
        return self.intake.on_mention(m)

    def handle_action(self, body: dict[str, Any]) -> str | None:
        """Approve / Cancel decide at once; Revise opens a modal for the feedback."""
        action = body["actions"][0]
        rid, user = action["value"], self.user(body["user"]["id"])
        if action["action_id"] == "pw_revise":
            self.client.views_open(trigger_id=body["trigger_id"], view=revise_modal(rid))
            return None
        decision = "approve" if action["action_id"] == "pw_approve" else "cancel"
        msg = self.intake.on_decision(Decision(rid, decision, user))
        self.client.chat_postEphemeral(channel=body["channel"]["id"], user=user.user_id, text=msg,
                                       thread_ts=body.get("message", {}).get("thread_ts") or body.get("message", {}).get("ts"))
        return msg

    def handle_revise_submit(self, body: dict[str, Any], view: dict[str, Any]) -> str:
        feedback = view["state"]["values"]["feedback"]["text"]["value"] or ""
        return self.intake.on_decision(Decision(view["private_metadata"], "revise", self.user(body["user"]["id"]), feedback))

    # --- wiring ---------------------------------------------------------------------------------

    def start(self, bot_token: str, app_token: str) -> threading.Thread:
        """Connect over Socket Mode in a background thread."""
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler

        app = App(token=bot_token)
        self.client = app.client

        @app.event("app_mention")
        def _mention(event: dict[str, Any]) -> None:
            self.handle_mention(event)

        @app.event("message")
        def _dm(event: dict[str, Any]) -> None:
            if event.get("channel_type") == "im" and not event.get("bot_id") and not event.get("subtype"):
                self.handle_mention(event)

        @app.action(re.compile(r"^pw_(approve|revise|cancel)$"))
        def _action(ack: Any, body: dict[str, Any]) -> None:
            ack()
            self.handle_action(body)

        @app.view("pw_revise_submit")
        def _revise(ack: Any, body: dict[str, Any], view: dict[str, Any]) -> None:
            ack()
            self.handle_revise_submit(body, view)

        handler = SocketModeHandler(app, app_token)
        t = threading.Thread(target=handler.start, name="slack", daemon=True)
        t.start()
        return t
