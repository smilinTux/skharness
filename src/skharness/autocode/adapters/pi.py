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

from .base import BaseCliAdapter, parse_event_stream

# Attribution header values must be plain, inert tokens. pi treats a LEADING `!`
# in a header value as "run this shell command and use its stdout", re-executed on
# every request, and a leading `$` as "read this environment variable" (`$$`/`$!`
# escape them). Our ids are hex today, so rather than escaping we refuse anything
# outside a conservative token charset: the magic prefixes, whitespace, control
# characters and header-splitting bytes are all excluded by construction. This is
# an assertion, not a filter -- a value that fails is a bug upstream, and dropping
# it silently would leave the run unattributable with nothing to show for it.
_ATTRIBUTION_TOKEN = re.compile(r"\A[A-Za-z0-9._:@/=+-]{1,200}\Z")


def _attribution_value(name: str, value):
    """Validate one attribution header value. None -> None (header is omitted)."""
    if value is None:
        return None
    value = str(value)
    if not _ATTRIBUTION_TOKEN.fullmatch(value):
        raise ValueError(
            f"unsafe pi attribution header value for {name!r}: {value!r}. Allowed: "
            "1-200 chars of [A-Za-z0-9._:@/=+-]. A leading '!' or '$' is a pi magic "
            "prefix (shell exec / env lookup), so such a value is never passed through.")
    return value


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

    def __init__(self, sandbox=None, model=None, base_url=None, egress_hosts=None,
                 live_execution: bool = False, image=None, max_tokens=None,
                 run_timeout=None, session_id=None, card_id=None):
        from ..sandbox import Sandbox
        self.model = model
        self.base_url = base_url
        # Attribution ids. Optional and None by default: a caller that does not know
        # who it is sends NO attribution headers at all, rather than empty strings.
        # "no session" and "session is the empty string" are different facts and the
        # gateway must be able to tell them apart. A follow-up card wires
        # autocode.identity.resolve_identity() in here; this adapter deliberately does
        # not import it, so it stays independently mergeable and independently usable.
        self.session_id = _attribution_value(self._H_SESSION, session_id)
        self.card_id = _attribution_value(self._H_CARD, card_id)
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
        return {"session_resume": True, "structured_output": "json",
                "sandbox": True, "tool_restrictions": True,
                "task_plane": True, "session_plane": False,
                "headless_api": "none", "hot_set_model": False}

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
        return model if model is not None else self.model

    def _argv(self, prompt: str, light: bool = False,
              model: str | None = None) -> list[str]:
        # light (assess/grade judgment) accepted for the unified seam; pi's
        # --no-session already runs a single non-agentic shot.
        eff = self._effective_model(model)
        if not eff:
            return ["pi", "-p", prompt, "--mode", "json", "--no-session"]
        return ["pi", "-p", prompt, "--mode", "json", "--no-session",
                "--model", f"skgw/{eff}", "--api-key", "sk-local"]

    def _image(self) -> str:
        return self.image

    def _auth_mounts(self):
        return []                              # local skgateway: no external cred

    def _auth_env(self):
        # points pi at the injected config dir (models.json); do NOT set
        # OPENAI_BASE_URL, pi ignores it and hits real OpenAI instead.
        return {"PI_CODING_AGENT_DIR": "/agent"}

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
        if not self.base_url:
            return {}
        eff = self._effective_model(model)
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
            "models": [{"id": eff,
                        "limit": {"context": self.max_tokens,
                                  "output": self.max_tokens}}],
        }
        headers = self._attribution_headers(session_id=session_id, card_id=card_id)
        if headers:                       # absent, never {}, when we know no ids
            skgw["headers"] = headers
        return {"/agent/models.json": json.dumps({"providers": {"skgw": skgw}})}

    def _parse(self, raw: dict) -> dict:
        if not isinstance(raw, dict):
            return {}
        # already the model reply dict
        if any(k in raw for k in ("verdict", "score", "passed")):
            return raw
        body = raw.get("result")
        if isinstance(body, dict):
            return body
        if isinstance(body, str):
            # pi `--mode json` is an event stream (same shape family as opencode);
            # Sandbox.spawn hands it over as result=<stream> when not a lone object.
            obj = parse_event_stream(body)
            if obj:
                return obj
            try:                                   # single-object fallback
                single = json.loads(body)
                if isinstance(single, dict):
                    return single
            except (json.JSONDecodeError, TypeError):
                pass
        return {}
