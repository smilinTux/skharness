"""Pinned Pi 0.84.2 JSON-event validation and model evidence aggregation.

Parsing assistant content may remain best effort, but controller trust decisions use
this module's complete-stream signal.  Unknown, malformed, truncated, scalar, and
plain-text records are never silently promoted to valid Pi event evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .types import HarnessProvenanceReason

# Exact stdout vocabulary from pi-coding-agent 0.84.2's
# JsonAgentSessionEvent (core/agent-session.d.ts + pi-agent-core/types.d.ts).
# New Pi versions must update this pin deliberately.
PI_0842_EVENT_TYPES = frozenset(
    {
        "session",
        "agent_start",
        "agent_end",
        "agent_settled",
        "turn_start",
        "turn_end",
        "message_start",
        "message_update",
        "message_end",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
        "queue_update",
        "compaction_start",
        "compaction_end",
        "entry_appended",
        "session_info_changed",
        "thinking_level_changed",
        "auto_retry_start",
        "auto_retry_end",
        "summarization_retry_scheduled",
        "summarization_retry_attempt_start",
        "summarization_retry_finished",
        "bash_execution_update",
    }
)
PI_0842_MESSAGE_ROLES = frozenset(
    {
        "user",
        "assistant",
        "toolResult",
        "bashExecution",
        "custom",
        "branchSummary",
        "compactionSummary",
    }
)


@dataclass(frozen=True)
class PiEventScan:
    """Valid ordered envelopes plus a fail-closed stream-completeness bit."""

    events: tuple[dict[str, Any], ...]
    incomplete: bool


def valid_pi_event_envelope(event: object) -> bool:
    """Validate the minimum trusted envelope contract for pinned Pi 0.84.2."""

    if not isinstance(event, dict) or event.get("type") not in PI_0842_EVENT_TYPES:
        return False
    if event["type"] == "message_end":
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") not in PI_0842_MESSAGE_ROLES:
            return False
        # Pi's AssistantMessage requires content. Validate the portion used by
        # parsing/provenance instead of accepting a role-only object.
        if message["role"] == "assistant" and not isinstance(message.get("content"), list):
            return False
    return True


def scan_pi_events(raw: object) -> PiEventScan:
    """Return valid Pi envelopes while retaining every non-event as incompleteness.

    Adapters normally pass Sandbox's raw mapping, whose ``result`` contains NDJSON.
    Arena passes the captured stdout bytes directly. A directly decoded one-event
    mapping is also accepted. Empty/None input means no events observed; nonblank
    data that is not a pinned event makes the stream incomplete.
    """

    candidate = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
    if isinstance(candidate, dict):
        if valid_pi_event_envelope(candidate):
            return PiEventScan((candidate,), False)
        return PiEventScan((), bool(candidate))
    decode_incomplete = False
    if isinstance(candidate, bytes):
        try:
            candidate = candidate.decode("utf-8")
        except UnicodeDecodeError:
            candidate = candidate.decode("utf-8", errors="replace")
            decode_incomplete = True
    if candidate is None:
        return PiEventScan((), False)
    if not isinstance(candidate, str):
        return PiEventScan((), True)

    events: list[dict[str, Any]] = []
    incomplete = decode_incomplete
    for line in candidate.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            incomplete = True
            continue
        if valid_pi_event_envelope(event):
            events.append(event)
        else:
            incomplete = True
    return PiEventScan(tuple(events), incomplete)


def assistant_message_events(
    raw_or_scan: object,
) -> tuple[tuple[dict[str, Any], dict[str, Any]], ...]:
    """Return only provider-owned assistant ``message_end`` call envelopes."""

    scan = raw_or_scan if isinstance(raw_or_scan, PiEventScan) else scan_pi_events(raw_or_scan)
    result = []
    for event in scan.events:
        message = event.get("message")
        if (
            event.get("type") == "message_end"
            and isinstance(message, dict)
            and message.get("role") == "assistant"
        ):
            result.append((event, message))
    return tuple(result)


def event_response_models(event: dict, message: dict) -> tuple[str, ...]:
    """Return distinct nonblank ``responseModel`` values in one trusted event."""

    values = []
    for candidate in (message.get("responseModel"), event.get("responseModel")):
        if isinstance(candidate, str) and candidate.strip():
            value = candidate.strip()
            if value not in values:
                values.append(value)
    return tuple(values)


def served_model_evidence(
    raw_or_scan: object,
) -> tuple[str | None, HarnessProvenanceReason | None]:
    """Aggregate every assistant call without collapsing gaps or conflicts."""

    scan = raw_or_scan if isinstance(raw_or_scan, PiEventScan) else scan_pi_events(raw_or_scan)
    observed = []
    missing = False
    for event, message in assistant_message_events(scan):
        values = event_response_models(event, message)
        if len(values) > 1:
            return None, HarnessProvenanceReason.MODEL_SERVED_CONFLICT
        if not values:
            missing = True
        else:
            observed.append(values[0])

    if len(set(observed)) > 1:
        return None, HarnessProvenanceReason.MODEL_SERVED_CONFLICT
    if scan.incomplete:
        return None, HarnessProvenanceReason.MODEL_SERVED_INCOMPLETE_STREAM
    if observed and missing:
        return None, HarnessProvenanceReason.MODEL_SERVED_PARTIAL
    if observed:
        return observed[0], None
    return None, HarnessProvenanceReason.MODEL_SERVED_NOT_OBSERVED
