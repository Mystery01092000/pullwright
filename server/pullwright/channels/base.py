"""What every chat channel (Slack, Teams, the web UI) provides to the intake service, and what it gets back.

A channel turns its platform's events into `Mention`s and `Decision`s, and renders what intake asks it to post:
plain replies, the plan card with Approve / Revise / Cancel, and status updates in the same thread.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass
class ChatUser:
    channel: str  # slack | teams | web
    user_id: str  # the platform's id for this person
    name: str
    github_login: str = ""  # known for web users (they logged in with GitHub); looked up for chat users


@dataclass
class Mention:
    """Someone tagged the bot with a task."""
    user: ChatUser
    text: str  # the message with the bot mention removed
    thread_ref: dict[str, Any]  # where to reply (channel-specific)
    thread_url: str = ""
    default_repo: str = ""  # the repo this chat is mapped to, if any


@dataclass
class Decision:
    """A click on the plan card (or the web UI's buttons)."""
    request_id: str
    decision: Literal["approve", "revise", "cancel"]
    user: ChatUser
    feedback: str = ""


@dataclass
class PlanCard:
    request_id: str
    repo: str
    task: str
    plan_markdown: str
    revision: int
    cost_usd: float
    questions: list[str] = field(default_factory=list)
    spec: dict[str, Any] | None = None  # the structured plan (PLAN_SCHEMA), for channels that render it richly
    verification: dict[str, Any] | None = None  # the verifier's verdict on this revision


class Channel(Protocol):
    name: str

    def reply(self, thread_ref: dict[str, Any], text: str) -> None:
        """Post markdown text in the thread."""
        ...

    def show_plan(self, thread_ref: dict[str, Any], card: PlanCard) -> None:
        """Post the plan with Approve / Revise / Cancel actions that come back as a Decision."""
        ...
