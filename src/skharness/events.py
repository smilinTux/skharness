"""SessionEvent - the typed, ordered event a Harness stream emits (spec 3.1).

Read-only in the P0 MVP: assistant text deltas, tool calls/results, diffs,
status transitions, and needs-input markers. No write payloads exist here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class EventType(str, Enum):
    STATUS = "status"
    ASSISTANT_TEXT = "assistant_text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    DIFF = "diff"
    NEEDS_INPUT = "needs_input"


@dataclass
class SessionEvent:
    type: EventType
    text: str = ""
    ts: float = 0.0
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SessionEvent":
        d = dict(d)
        d["type"] = EventType(d.get("type", "status"))
        known = {"type", "text", "ts", "data"}
        return cls(**{k: v for k, v in d.items() if k in known})
