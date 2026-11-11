"""Scripted stand-in for anthropic.Anthropic(): each call to stream() pops the next scripted turn."""
from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any


def text(s: str) -> NS:
    return NS(type="text", text=s)


def thinking(s: str) -> NS:
    return NS(type="thinking", thinking=s)


def tool_use(name: str, inp: dict[str, Any], id_: str = "tu_1") -> NS:
    return NS(type="tool_use", id=id_, name=name, input=inp)


def turn(*blocks: NS, stop_reason: str | None = None, in_tok: int = 100, out_tok: int = 50, model: str = "claude-opus-5-5") -> NS:
    if stop_reason is None:
        stop_reason = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    return NS(
        content=list(blocks),
        stop_reason=stop_reason,
        stop_details=None,
        model=model,
        usage=NS(input_tokens=in_tok, output_tokens=out_tok, cache_creation_input_tokens=0, cache_read_input_tokens=0),
    )


class _Stream:
    def __init__(self, msg: NS) -> None:
        self.msg = msg

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *a: Any) -> None:
        pass

    def __iter__(self):
        for b in self.msg.content:
            if b.type == "thinking":
                yield NS(type="content_block_delta", delta=NS(type="thinking_delta", thinking=b.thinking))
            elif b.type == "text":
                yield NS(type="content_block_delta", delta=NS(type="text_delta", text=b.text))

    def get_final_message(self) -> NS:
        return self.msg


class FakeClient:
    def __init__(self, turns: list[NS]) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **kw: Any) -> _Stream:
        self.calls.append(kw)
        if not self.turns:
            raise AssertionError("fake client ran out of scripted turns")
        return _Stream(self.turns.pop(0))
