"""Event bus: every agent action becomes one Event, fanned out to subscribers (CLI, store, SSE)."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    type: str  # agent_start | thinking | text | tool_call | tool_result | usage | agent_end | error
    run_id: str
    agent_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None
    turn: int | None = None
    ts: float = field(default_factory=time.time)


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Callable[[Event], None]] = []

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._subs.append(fn)

    def emit(self, ev: Event) -> None:
        for fn in self._subs:
            fn(ev)
