"""PiAdapter: pi (pi.dev) on the shared BaseCliAdapter. Sovereign target harness:
pi routes to a local model via skgateway, keeping all egress on-tailnet. Verified
live end-to-end in the confined sandbox against skgateway/ornith-tiny.

Routing recipe (all proven): inject /agent/models.json (skgw provider, api=
`openai-completions`, `compat.supportsDeveloperRole: false` -- REQUIRED for ornith
or it 400s) + PI_CODING_AGENT_DIR=/agent; `--model skgw/<model>` + `--api-key`;
`--no-session` (else the container-uid session mkdir EACCESes). pi IGNORES the
OPENAI_BASE_URL env (it hits real OpenAI), so routing MUST go through models.json.
The sandbox proxy forwards plain HTTP for allowlisted hosts, so the internal-net
container reaches the local http skgateway through it. pi's `--mode json` reply is
the assistant `message_end` event's content[].text; `_parse` handles that event
stream plus the single-object shape.

Attribution (card A6.1): pi otherwise forwards nothing identifying (measured: host,
Accept, User-Agent, the OpenAI-JS X-Stainless-* block, authorization, content-type,
and nothing else), which is why skgateway `request_log` rows have NULL agent_id and
session_id for every harness run. pi reads a provider-level `headers` map from the
same models.json we already generate, so the fix is a config change here rather than
new plumbing. See _attribution_headers for the literal-values rule."""

from __future__ import annotations

import json
import re

from .base import BaseCliAdapter, extract_json, parse_event_stream
from ...arena.pi_bridge import PI_PROFILES, BridgeDeniedError
from ..types import HarnessProvenanceReason

# Attribution header values must be plain, inert tokens. pi treats a LEADING `!`
# in a header value as "run this shell command and use its stdout", re-executed on
# every request, and a leading `$` as "read this environment variable" (`$$`/`$!`
# escape them). Our ids are hex today, so rather than escaping we refuse anything
# outside a conservative token charset: the magic prefixes, whitespace, control
# characters and header-splitting bytes are all excluded by construction. This is
# an assertion, not a filter -- a value that fails is a bug upstream, and dropping
# it silently would leave the run unattributable with nothing to show for it.
_ATTRIBUTION_TOKEN = re.compile(r"\A[A-Za-z0-9._:@/=+-]{1,200}\Z")

# Exact stdout event vocabulary from pi-coding-agent 0.84.2's
# JsonAgentSessionEvent (core/agent-session.d.ts + pi-agent-core/types.d.ts).
# An upgrade that emits a new event must update this pin deliberately; silently
# accepting a future envelope would make provenance semantics version-dependent.
_PI_0842_EVENT_TYPES = frozenset(
    {
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
_PI_0842_MESSAGE_ROLES = frozenset(
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


def _attribution_value(name: str, value):
    """Validate one attribution header value. None -> None (header is omitted)."""
    if value is None:
        return None
    value = str(value)
    if not _ATTRIBUTION_TOKEN.fullmatch(value):
        raise ValueError(
            f"unsafe pi attribution header value for {name!r}: {value!r}. Allowed: "
            "1-200 chars of [A-Za-z0-9._:@/=+-]. A leading '!' or '$' is a pi magic "
            "prefix (shell exec / env lookup), so such a value is never passed through."
        )
    return value


def _model_id(value: str | None) -> str | None:
    """Normalize a model id while refusing the ambiguous blank route."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("pi model id must be a string or None")
    normalized = value.strip()
    if not normalized:
        raise ValueError("pi model id must not be blank")
    return normalized


def _valid_pi_event_envelope(event) -> bool:
    """Minimum trusted envelope contract for pinned Pi 0.84.2 JSON events."""
    if not isinstance(event, dict) or event.get("type") not in _PI_0842_EVENT_TYPES:
        return False
    if event["type"] == "message_end":
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") not in _PI_0842_MESSAGE_ROLES:
            return False
        # Assistant content is required by Pi's AssistantMessage. Validate the
        # portion needed by parsing/provenance rather than pretending a role-only
        # object is a complete provider event.
        if message["role"] == "assistant" and not isinstance(message.get("content"), list):
            return False
    return True


def _pi_event_scan(raw) -> tuple[list[dict], bool]:
    """Return Pi envelopes plus whether nonblank output was not an event.

    Normal Pi output is NDJSON under ``raw["result"]``.  A one-event stream is
    valid JSON, though, so Sandbox may return that event directly. Malformed,
    truncated, scalar, and non-event JSON lines are retained as an incomplete
    signal rather than silently discarded. Neither a model reply dict nor
    arbitrary nested JSON is promoted to an event.
    """
    candidate = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
    if isinstance(candidate, dict):
        if _valid_pi_event_envelope(candidate):
            return [candidate], False
        return [], bool(candidate)
    if not isinstance(candidate, str):
        return [], False
    events = []
    incomplete = False
    for line in candidate.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            incomplete = True
            continue
        if _valid_pi_event_envelope(event):
            events.append(event)
        else:
            incomplete = True
    return events, incomplete


def _pi_events(raw) -> list[dict]:
    """Compatibility view of valid envelopes; trust callers use scan status too."""
    return _pi_event_scan(raw)[0]


def _assistant_message_events(raw) -> list[tuple[dict, dict]]:
    """Only provider-owned assistant ``message_end`` events count as calls."""
    result = []
    for event in _pi_events(raw):
        message = event.get("message")
        if (
            event.get("type") == "message_end"
            and isinstance(message, dict)
            and message.get("role") == "assistant"
        ):
            result.append((event, message))
    return result


def _event_response_models(event: dict, message: dict) -> tuple[str, ...]:
    """Distinct non-blank responseModel values in one trusted event."""
    values = []
    for candidate in (message.get("responseModel"), event.get("responseModel")):
        if isinstance(candidate, str) and candidate.strip():
            value = candidate.strip()
            if value not in values:
                values.append(value)
    return tuple(values)


def _model_served_evidence(raw) -> tuple[str | None, HarnessProvenanceReason | None]:
    """Aggregate every assistant call without collapsing gaps or conflicts."""
    events, incomplete = _pi_event_scan(raw)
    messages = []
    for event in events:
        message = event.get("message")
        if (
            event.get("type") == "message_end"
            and isinstance(message, dict)
            and message.get("role") == "assistant"
        ):
            messages.append((event, message))
    observed = []
    missing = False
    for event, message in messages:
        values = _event_response_models(event, message)
        if len(values) > 1:
            return None, HarnessProvenanceReason.MODEL_SERVED_CONFLICT
        if not values:
            missing = True
        else:
            observed.append(values[0])

    if len(set(observed)) > 1:
        return None, HarnessProvenanceReason.MODEL_SERVED_CONFLICT
    if incomplete:
        return None, HarnessProvenanceReason.MODEL_SERVED_INCOMPLETE_STREAM
    if observed and missing:
        return None, HarnessProvenanceReason.MODEL_SERVED_PARTIAL
    if observed:
        return observed[0], None
    return None, HarnessProvenanceReason.MODEL_SERVED_NOT_OBSERVED


def _observed_served_model(raw) -> str | None:
    """Compatibility helper: return a model only for complete, agreeing evidence."""
    return _model_served_evidence(raw)[0]


_ASSISTANT_PROVENANCE_FIELDS = frozenset(
    {
        "model_requested",
        "model_served",
        "backend_served",
        "gateway_req_id",
        "model_served_reason",
        "backend_served_reason",
        "gateway_req_id_reason",
    }
)


def _without_assistant_provenance(value: dict) -> dict:
    """Copy a model reply while removing every controller-owned provenance key."""
    return {key: item for key, item in value.items() if key not in _ASSISTANT_PROVENANCE_FIELDS}


def _assistant_event_result(event: dict, message: dict) -> dict | None:
    """Parse one assistant reply and annotate it only from that same event."""
    chunks = [
        part.get("text")
        for part in (message.get("content") or [])
        if isinstance(part, dict)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    ]
    obj = extract_json("".join(chunks)) if chunks else None
    if not obj:
        return None
    clean = _without_assistant_provenance(obj)
    if not clean:
        # Provider metadata must not turn an otherwise empty/forged assistant
        # reply into a usable assess/grade result.
        return None
    response_models = _event_response_models(event, message)
    if len(response_models) == 1:
        clean["model_served"] = response_models[0]
    return clean


class PiAdapter(BaseCliAdapter):
    name = "pi"

    # ornith-big (Tyler's server) has no reasoning/output cap, so we give pi a
    # generous ceiling rather than a small one that a thinking model exhausts on
    # reasoning before emitting content. Overridable via config.harness_max_tokens.
    _DEFAULT_MAX_TOKENS = 131072

    # Attribution headers pi sends to skgateway so a harness run can be joined to
    # its gateway request_log row (card A6.1). Names are fixed here; the VALUES are
    # supplied by the caller.
    _H_SESSION = "x-session-id"
    _H_CARD = "x-sk-card-id"

    def __init__(
        self,
        sandbox=None,
        model=None,
        base_url=None,
        egress_hosts=None,
        live_execution: bool = False,
        image=None,
        max_tokens=None,
        run_timeout=None,
        session_id=None,
        card_id=None,
        capability_profile=None,
    ):
        from ..sandbox import Sandbox

        self.model = _model_id(model)
        self.base_url = base_url
        # Attribution ids. Optional and None by default: a caller that does not know
        # who it is sends NO attribution headers at all, rather than empty strings.
        # "no session" and "session is the empty string" are different facts and the
        # gateway must be able to tell them apart. A follow-up card wires
        # autocode.identity.resolve_identity() in here; this adapter deliberately does
        # not import it, so it stays independently mergeable and independently usable.
        self.session_id = _attribution_value(self._H_SESSION, session_id)
        self.card_id = _attribution_value(self._H_CARD, card_id)
        if capability_profile is not None and capability_profile not in PI_PROFILES:
            raise BridgeDeniedError(f"unknown Pi capability profile: {capability_profile!r}")
        self.capability_profile = capability_profile
        self.image = image or "sandbox-pi:1"
        self.max_tokens = int(max_tokens) if max_tokens else self._DEFAULT_MAX_TOKENS
        # pi does one turn and terminates (measured ~3.6s for a classification prompt
        # against ornith-tiny), so unlike opencode it needs no aggressive cap -- it
        # keeps the sandbox default. run_timeout is exposed only so a caller can bound
        # a long coding run if wanted; None -> the Sandbox default.
        sb = sandbox
        if sb is None:
            kw = {"live_execution": live_execution}
            if run_timeout:
                kw["run_timeout"] = int(run_timeout)
            sb = Sandbox(**kw)
        super().__init__(sb, egress_hosts=egress_hosts, live_execution=live_execution)

    def capabilities(self):
        return {
            "session_resume": True,
            "structured_output": "json",
            "sandbox": True,
            "tool_restrictions": True,
            "task_plane": True,
            "session_plane": False,
            "headless_api": "none",
            "hot_set_model": False,
        }

    def supports_model_override(self) -> bool:
        # pi honours it in both places that name a model: _argv (the REQUEST) and
        # _config_files (the DECLARATION). Both read _effective_model, so they
        # cannot disagree.
        return True

    def _effective_model(self, model: str | None = None) -> str | None:
        """The model id this ONE call uses: the per-call override when a dispatcher
        pinned one (a graded skgateway bucket, see buckets.py), else the adapter's
        statically configured model. Single source of truth for _argv and
        _config_files: pi DECLARES a provider model in models.json and REQUESTS one
        on the command line, and if those two disagree pi asks skgw for a model it
        never declared."""
        return _model_id(model) if model is not None else self.model

    def _argv(self, prompt: str, light: bool = False, model: str | None = None) -> list[str]:
        # light (assess/grade judgment) accepted for the unified seam; pi's
        # --no-session already runs a single non-agentic shot.
        eff = self._effective_model(model)
        argv = ["pi", "-p", prompt, "--mode", "json", "--no-session"]
        if self.capability_profile:
            profile = PI_PROFILES[self.capability_profile]
            argv.extend(
                [
                    "--no-extensions",
                    "-e",
                    "/opt/skharness/pi/sk-bridge.ts",
                    "--tools",
                    ",".join(profile.pi_tools),
                ]
            )
        if eff:
            argv.extend(["--model", f"skgw/{eff}", "--api-key", "sk-local"])
        return argv

    def _image(self) -> str:
        return self.image

    def _auth_mounts(self):
        return []  # local skgateway: no external cred

    def _auth_env(self):
        # points pi at the injected config dir (models.json); do NOT set
        # OPENAI_BASE_URL, pi ignores it and hits real OpenAI instead.
        env = {"PI_CODING_AGENT_DIR": "/agent"}
        if self.capability_profile:
            env["SKHARNESS_PI_PROFILE"] = self.capability_profile
        return env

    def _required_commands(self) -> list[str]:
        # arena-build promises a test toolchain. Refuse a minimal pi-core image
        # before spending an agent turn trying to bootstrap pytest in /tmp.
        return ["pytest"] if self.capability_profile == "arena-build" else []

    def _required_checks(self) -> list[list[str]]:
        # Presence of a `pytest` executable is weaker than the arena-build image
        # contract: the plugins and sovereign sibling schemas can still be absent
        # or binary-incompatible. Execute the immutable in-image probe before the
        # model receives a turn; verified runs never install dependencies at runtime.
        if self.capability_profile == "arena-build":
            return [["/usr/local/bin/skharness-pi-python-test-preflight"]]
        return []

    def _attribution_headers(self, session_id=None, card_id=None) -> dict:
        """The provider-level `headers` map, or {} when we have nothing to attribute.

        Values are baked in as LITERALS and must never be written as `$VAR` env
        interpolation. pi supports that form, but with the variable unset it makes NO
        request at all, reports an internal error, and STILL EXITS 0 -- `_parse` then
        returns {} and the whole call has failed in a way no exit code reveals.
        `_config_files` is regenerated per call with the values already in hand, so
        there is no reason to reach for interpolation. Proven in the card evidence:
        coordination/evidence/4852c56d-pi-custom-headers/ (variant J).

        Each id is independent: supplying one and not the other emits one header, and
        supplying neither emits no `headers` key at all (see _config_files)."""
        sid = _attribution_value(self._H_SESSION, session_id) or self.session_id
        cid = _attribution_value(self._H_CARD, card_id) or self.card_id
        headers = {}
        if sid:
            headers[self._H_SESSION] = sid
        if cid:
            headers[self._H_CARD] = cid
        return headers

    def _config_files(self, model: str | None = None, session_id=None, card_id=None):
        eff = self._effective_model(model)
        if not self.base_url:
            return {}
        skgw = {
            "baseUrl": self.base_url,
            "api": "openai-completions",
            "apiKey": "sk-local",
            # SINGLE SOURCE OF x-session-id: skharness. pi can mint its own
            # x-session-id via compat.sendSessionAffinityHeaders +
            # sessionAffinityFormat: "openrouter", which would be a random UUID that
            # fights ours on the same header name. We deliberately do not set either
            # key, so the only x-session-id skgateway ever sees from pi is the one the
            # harness put there and can join back to a run.
            "compat": {"supportsDeveloperRole": False},
            "models": [
                {"id": eff, "limit": {"context": self.max_tokens, "output": self.max_tokens}}
            ],
        }
        headers = self._attribution_headers(session_id=session_id, card_id=card_id)
        if headers:  # absent, never {}, when we know no ids
            skgw["headers"] = headers
        return {"/agent/models.json": json.dumps({"providers": {"skgw": skgw}})}

    def _result_provenance(self, raw: dict, model: str | None = None) -> dict:
        """Facts Pi's provider-owned event stream actually exposes.

        ``responseModel`` is part of Pi's AssistantMessage contract.  Its
        ``provider`` is the configured provider (``skgw``), not the backend that
        served the request, and ``responseId`` is an upstream response identifier,
        not SKGateway's ``x-sk-req-id`` response header.  Pi does not expose those
        response headers in JSON mode, so neither field may be coerced into gateway
        attribution.  The closed reasons keep each absence explicit.
        """
        served, served_reason = _model_served_evidence(raw)
        return {
            "model_requested": self._effective_model(model),
            "model_served": served,
            "backend_served": None,
            "gateway_req_id": None,
            "model_served_reason": served_reason,
            "backend_served_reason": (HarnessProvenanceReason.BACKEND_SERVED_NOT_OBSERVED),
            "gateway_req_id_reason": (HarnessProvenanceReason.GATEWAY_REQ_ID_NOT_OBSERVED),
        }

    def _parse(self, raw: dict) -> dict:
        if not isinstance(raw, dict):
            return {}
        # already the model reply dict
        if any(k in raw for k in ("verdict", "score", "passed")):
            return _without_assistant_provenance(raw)

        # Pi can emit several assistant message_end events in one agentic turn.
        # Parse the first usable reply as before, but bind responseModel ONLY from
        # that exact event.  Aggregating metadata across the stream here would
        # cross-associate one assistant's JSON with another provider call.
        for event, message in _assistant_message_events(raw):
            if obj := _assistant_event_result(event, message):
                return obj

        body = raw.get("result")
        if isinstance(body, dict):
            return _without_assistant_provenance(body)
        if isinstance(body, str):
            # Compatibility fallback for the older opencode-like text event shape.
            # It has no Pi AssistantMessage envelope, so it cannot contribute
            # provider attribution.
            obj = parse_event_stream(body)
            if obj:
                return _without_assistant_provenance(obj)
            try:  # single-object fallback
                single = json.loads(body)
                if isinstance(single, dict) and "type" not in single:
                    return _without_assistant_provenance(single)
            except (json.JSONDecodeError, TypeError):
                pass
        return {}
