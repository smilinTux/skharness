"""SessionEvent - the typed, ordered event a Harness stream emits (spec 3.1).

Read-only in the P0 MVP: assistant text deltas, tool calls/results, diffs,
status transitions, and needs-input markers. No write payloads exist here.

v2 (skcode Code-section card C-1, spec 2026-08-11 section 5.1) adds three
fields ADDITIVELY on top of the original four (type/text/ts/data): ``seq``,
``sid``, ``source``. No existing field is renamed or retyped, so the old
iframe client (skharness/src/skharness/client/index.html) keeps working
unchanged against the new payload; it simply ignores the unknown keys.

``seq`` is a per-session monotonic int ASSIGNED AT APPEND by the session
buffer (see skharness.session_events.SessionEventStore), not by the emitter
here. It resets to 1 the moment the daemon process restarts, ON PURPOSE: the
counter is in-memory only, so a fresh process has a fresh counter while ``ts``
keeps climbing. That is exactly why a client's dedup/scroll-anchor key is
``(sid, seq, ts)`` and never ``seq`` alone (spec 5.1, porting Buzz's
documented seq trap). Do not "fix" the reset; a test asserts it explicitly.
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


# SessionEvent.source values (spec 5.1). Not enforced as an enum on the
# dataclass (fail-soft like the rest of this module's parsing): a value
# outside this set is passed through rather than raised, so an older/newer
# emitter can never crash the daemon over a source string it does not know.
SOURCE_INTERACTIVE = "interactive"   # dispatched or attached operator sessions
SOURCE_AUTOCODE = "autocode"         # autocode orchestrator runs
SOURCE_ATTACH = "attach"
KNOWN_SOURCES = frozenset({SOURCE_INTERACTIVE, SOURCE_AUTOCODE, SOURCE_ATTACH})


@dataclass
class SessionEvent:
    type: EventType
    text: str = ""
    ts: float = 0.0
    data: dict = field(default_factory=dict)
    # --- v2 fields (additive only, see module docstring) ---
    seq: int = 0
    sid: str = ""
    source: str = SOURCE_INTERACTIVE

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SessionEvent":
        d = dict(d)
        d["type"] = EventType(d.get("type", "status"))
        known = {"type", "text", "ts", "data", "seq", "sid", "source"}
        return cls(**{k: v for k, v in d.items() if k in known})
