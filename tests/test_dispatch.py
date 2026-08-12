"""skcode-hostd DISPATCH surface (P2): the RCE gate matrix.

POST /api/v1/dispatch spawns a NEW agent session. It is gated, in order and all
fail-closed: emergency-brake pause (503 regardless of auth) -> bearer + the
skcode.dispatch scope (401/403) -> a configured audit sink AND authz PDP (else
501) -> capauth.authz.decide == allow (else 403, audited) -> the harness spawn
RCE input guards (SpawnRejected -> 400). Driven with a fake spawner + a fake
authorizer + an AuthContext verifier, so no real capauth, tmux, or spawn.
"""
from dataclasses import dataclass, field

from fastapi.testclient import TestClient

from skharness.auth import AuthContext
from skharness.daemon import build_daemon_app
from skharness.harness import FakeHarness, HarnessSession, SessionDescriptor, SpawnRejected

# --- test doubles ------------------------------------------------------------


@dataclass
class _Obligation:
    kind: str = "audit"
    data: dict = field(default_factory=dict)


@dataclass
class _Decision:
    allow: bool
    reason: str = ""
    obligations: list = field(default_factory=list)


class _SpawningHarness(FakeHarness):
    """FakeHarness plus a spawn() that RECORDS the descriptor + prompt and returns
    a live HarnessSession, so the daemon dispatch route is driven with no real
    tmux/worktree. (FakeHarness itself leaves spawn at the base gated raise; the
    spawn path is proven here on a subclass, mirroring the inject/grade doubles.)"""

    def __init__(self):
        super().__init__(sessions=[], events={})
        self.spawned: list[tuple[SessionDescriptor, str]] = []

    async def spawn(self, desc, *, prompt):
        self.spawned.append((desc, prompt))
        return HarnessSession(sid="sandbox-deadbeef", descriptor=desc,
                              status="running", branch=desc.branch)


class _RejectingHarness(FakeHarness):
    """A spawn that always fails the RCE input guard (repo not allowlisted)."""

    def __init__(self):
        super().__init__(sessions=[], events={})

    async def spawn(self, desc, *, prompt):
        raise SpawnRejected("repo 'nope' is not on the dispatch allowlist")


def _verifier(*scopes, subject="phone@chef.skworld"):
    ctx = AuthContext(scopes=frozenset(scopes), subject=subject)
    return lambda token: ctx


def _allow_authorizer(record=None):
    def _a(subject, resource, context):
        if record is not None:
            record.append((subject, resource, context))
        return _Decision(allow=True, reason="granted",
                         obligations=[_Obligation(data={"decision": "allow",
                                                        "subject": subject})])
    return _a


def _deny_authorizer(record=None):
    def _a(subject, resource, context):
        if record is not None:
            record.append((subject, resource, context))
        return _Decision(allow=False, reason="insufficient enrollment mode",
                         obligations=[_Obligation(data={"decision": "deny",
                                                        "subject": subject})])
    return _a


_BODY = {"harness": "claude-code", "model": "ornith-tiny", "host": ".158",
         "repo": "/repos/skharness", "branch": "feat/x", "profile": "sandbox",
         "permission_mode": "manual", "prompt": "do the thing"}

_DISPATCH = _verifier("skcode.dispatch", "skcode.stream")


def _app(*, harness=None, verify=None, authorize=None, audit=None,
         paused=None, targets=None):
    return build_daemon_app(
        harness=harness if harness is not None else _SpawningHarness(),
        verify_caller=verify if verify is not None else _DISPATCH,
        authorize_dispatch=authorize,
        audit_log=audit,
        dispatch_paused=paused,
        dispatch_targets=targets,
    )


# --- the gate matrix ---------------------------------------------------------


def test_dispatch_no_token_is_401():
    c = TestClient(_app(authorize=_allow_authorizer(), audit=lambda s: None))
    assert c.post("/api/v1/dispatch", json=_BODY).status_code == 401


def test_dispatch_stream_only_token_is_403_insufficient_scope():
    harness = _SpawningHarness()
    c = TestClient(_app(harness=harness, verify=_verifier("skcode.stream"),
                        authorize=_allow_authorizer(), audit=lambda s: None))
    r = c.post("/api/v1/dispatch", json=_BODY, headers={"authorization": "Bearer t"})
    assert r.status_code == 403
    assert harness.spawned == []          # never reached spawn


def test_dispatch_scope_but_authz_deny_is_403_and_audited():
    harness = _SpawningHarness()
    audits, authz_calls = [], []
    c = TestClient(_app(harness=harness, authorize=_deny_authorizer(authz_calls),
                        audit=audits.append))
    r = c.post("/api/v1/dispatch", json=_BODY, headers={"authorization": "Bearer t"})
    assert r.status_code == 403
    # authz WAS consulted with the subject + resource, spawn was NOT reached
    assert authz_calls and authz_calls[0][0] == "phone@chef.skworld"
    assert authz_calls[0][1]["repo"] == "/repos/skharness"
    assert harness.spawned == []
    # a denied decision is audited (both the obligation and the summary record)
    blob = " ".join(audits)
    assert "deny" in blob


def test_dispatch_scope_and_authz_allow_spawns_and_audits():
    harness = _SpawningHarness()
    audits, authz_calls = [], []
    c = TestClient(_app(harness=harness, authorize=_allow_authorizer(authz_calls),
                        audit=audits.append))
    r = c.post("/api/v1/dispatch", json=_BODY, headers={"authorization": "Bearer t"})
    assert r.status_code == 200
    body = r.json()
    assert body["sid"] == "sandbox-deadbeef"
    assert body["profile"] == "sandbox"
    # spawn got the descriptor built from the request + the prompt as data
    assert len(harness.spawned) == 1
    desc, prompt = harness.spawned[0]
    assert desc.repo == "/repos/skharness" and desc.branch == "feat/x"
    assert desc.quality == "sandbox" and desc.permission_mode == "manual"
    assert prompt == "do the thing"
    # audited: the allow obligation + the spawned summary
    blob = " ".join(audits)
    assert "allow" in blob and "spawned" in blob
    # mode defaults to "direct" when the body omits it, and reaches the descriptor
    assert desc.mode == "direct"


def test_dispatch_passes_interactive_mode_to_the_descriptor():
    harness = _SpawningHarness()
    audits = []
    c = TestClient(_app(harness=harness, authorize=_allow_authorizer(),
                        audit=audits.append))
    body = dict(_BODY, mode="interactive")
    r = c.post("/api/v1/dispatch", json=body, headers={"authorization": "Bearer t"})
    assert r.status_code == 200
    assert r.json()["mode"] == "interactive"
    desc, _ = harness.spawned[0]
    assert desc.mode == "interactive"


def test_dispatch_rejects_invalid_mode_with_400():
    harness = _SpawningHarness()
    c = TestClient(_app(harness=harness, authorize=_allow_authorizer(),
                        audit=lambda s: None))
    body = dict(_BODY, mode="root")
    r = c.post("/api/v1/dispatch", json=body, headers={"authorization": "Bearer t"})
    assert r.status_code == 400
    assert "invalid mode" in r.json()["detail"]
    # a bad mode never reaches spawn
    assert harness.spawned == []


def test_dispatch_paused_is_503_regardless_of_auth():
    harness = _SpawningHarness()
    c = TestClient(_app(harness=harness, authorize=_allow_authorizer(),
                        audit=lambda s: None, paused=lambda: True))
    # no token -> still 503 (the brake precedes auth)
    assert c.post("/api/v1/dispatch", json=_BODY).status_code == 503
    # valid dispatch token -> still 503
    r = c.post("/api/v1/dispatch", json=_BODY, headers={"authorization": "Bearer t"})
    assert r.status_code == 503
    assert r.json()["detail"] == "dispatch paused"
    assert harness.spawned == []          # nothing spawned while paused


def test_dispatch_without_audit_sink_fails_closed_501():
    # authz configured but NO audit sink -> deny (never allow unaudited).
    c = TestClient(_app(authorize=_allow_authorizer(), audit=None))
    r = c.post("/api/v1/dispatch", json=_BODY, headers={"authorization": "Bearer t"})
    assert r.status_code == 501


def test_dispatch_without_authz_pdp_fails_closed_501():
    # audit configured but NO authz PDP -> deny (never allow unauthorized).
    c = TestClient(_app(authorize=None, audit=lambda s: None))
    r = c.post("/api/v1/dispatch", json=_BODY, headers={"authorization": "Bearer t"})
    assert r.status_code == 501


def test_dispatch_spawn_rejected_is_400_and_audited():
    audits = []
    c = TestClient(_app(harness=_RejectingHarness(), authorize=_allow_authorizer(),
                        audit=audits.append))
    r = c.post("/api/v1/dispatch", json=_BODY, headers={"authorization": "Bearer t"})
    assert r.status_code == 400
    assert "spawn rejected" in r.json()["detail"]
    assert any("rejected" in a for a in audits)


def test_dispatch_deny_all_verifier_403_default_preserved():
    # The P0 deny-all default: dispatch is 403 even with authz+audit wired, because
    # the bearer gate denies first. The RCE route is closed under deny-all.
    harness = _SpawningHarness()
    c = TestClient(_app(harness=harness, verify=lambda t: False,
                        authorize=_allow_authorizer(), audit=lambda s: None))
    r = c.post("/api/v1/dispatch", json=_BODY, headers={"authorization": "Bearer t"})
    assert r.status_code == 403
    assert harness.spawned == []


# --- /dispatch/targets : advisory only, dispatch-scope gated -----------------


def test_dispatch_targets_lists_advisory_options_for_dispatch_scope():
    c = TestClient(_app(targets=lambda: {"repos": ["/repos/skharness"]}))
    r = c.get("/api/v1/dispatch/targets", headers={"authorization": "Bearer t"})
    assert r.status_code == 200
    body = r.json()
    assert body["advisory"] is True
    assert body["repos"] == ["/repos/skharness"]
    assert "sandbox" in body["profiles"] and "full" in body["profiles"]
    assert body["harnesses"] == ["fake"]


def test_dispatch_targets_requires_dispatch_scope():
    # a stream/inject-only device sees no dispatch targets (403).
    c = TestClient(_app(verify=_verifier("skcode.stream", "skcode.inject"),
                        targets=lambda: {"repos": ["/repos/skharness"]}))
    r = c.get("/api/v1/dispatch/targets", headers={"authorization": "Bearer t"})
    assert r.status_code == 403


def test_dispatch_targets_no_token_is_401():
    c = TestClient(_app(targets=lambda: {"repos": []}))
    assert c.get("/api/v1/dispatch/targets").status_code == 401


# --- POST /sessions/{sid}/cancel : rides the SAME dispatch scope + PDP path --
# (card C-6, spec section 8: "it rides the dispatch scope through the same PDP
# decision path as dispatch and inject"). Proves the route is PDP-GATED, not
# merely present: no token -> 401, wrong scope -> 403, deny -> 403 (audited,
# harness.cancel never reached), allow -> harness.cancel called + audited, and
# cancelling an unknown/already-finished session is a clean 200 no-op.


class _CancellingHarness(FakeHarness):
    """FakeHarness plus a cancel() that RECORDS the sid and returns a canned
    idempotent-shaped result, so the daemon cancel route is driven with no real
    tmux/process. (FakeHarness leaves cancel at the base gated raise; the
    cancel path is proven here on a subclass, mirroring the dispatch/inject
    doubles.)"""

    def __init__(self, *, live=None):
        super().__init__(sessions=[], events={})
        self.cancelled: list[str] = []
        self._live = set(live) if live else set()

    async def cancel(self, sid):
        self.cancelled.append(sid)
        if sid not in self._live:
            return {"sid": sid, "cancelled": False,
                    "reason": "no live session (already ended or never running)"}
        self._live.discard(sid)
        return {"sid": sid, "cancelled": True}


def test_cancel_no_token_is_401():
    harness = _CancellingHarness(live=["lumina-abc12345"])
    c = TestClient(_app(harness=harness, authorize=_allow_authorizer(), audit=lambda s: None))
    r = c.post("/api/v1/sessions/lumina-abc12345/cancel")
    assert r.status_code == 401
    assert harness.cancelled == []


def test_cancel_stream_only_token_is_403_insufficient_scope():
    harness = _CancellingHarness(live=["lumina-abc12345"])
    c = TestClient(_app(harness=harness, verify=_verifier("skcode.stream"),
                        authorize=_allow_authorizer(), audit=lambda s: None))
    r = c.post("/api/v1/sessions/lumina-abc12345/cancel",
               headers={"authorization": "Bearer t"})
    assert r.status_code == 403
    assert harness.cancelled == []          # never reached harness.cancel


def test_cancel_scope_but_authz_deny_is_403_and_audited():
    harness = _CancellingHarness(live=["lumina-abc12345"])
    audits, authz_calls = [], []
    c = TestClient(_app(harness=harness, authorize=_deny_authorizer(authz_calls),
                        audit=audits.append))
    r = c.post("/api/v1/sessions/lumina-abc12345/cancel",
               headers={"authorization": "Bearer t"})
    assert r.status_code == 403
    assert authz_calls and authz_calls[0][0] == "phone@chef.skworld"
    assert authz_calls[0][1]["sid"] == "lumina-abc12345"
    assert harness.cancelled == []
    blob = " ".join(audits)
    assert "deny" in blob


def test_cancel_scope_and_authz_allow_calls_harness_and_audits():
    harness = _CancellingHarness(live=["lumina-abc12345"])
    audits, authz_calls = [], []
    c = TestClient(_app(harness=harness, authorize=_allow_authorizer(authz_calls),
                        audit=audits.append))
    r = c.post("/api/v1/sessions/lumina-abc12345/cancel",
               headers={"authorization": "Bearer t"})
    assert r.status_code == 200
    assert r.json() == {"sid": "lumina-abc12345", "cancelled": True}
    assert harness.cancelled == ["lumina-abc12345"]
    blob = " ".join(audits)
    assert "allow" in blob and "skcode.cancel" in blob


def test_cancel_unknown_session_is_a_clean_200_noop():
    """Idempotent + safe: cancelling an unknown/already-finished session never
    raises; it is a clean 200 with cancelled: False, not a 404/500."""
    harness = _CancellingHarness(live=[])
    c = TestClient(_app(harness=harness, authorize=_allow_authorizer(), audit=lambda s: None))
    r = c.post("/api/v1/sessions/never-existed/cancel",
               headers={"authorization": "Bearer t"})
    assert r.status_code == 200
    assert r.json() == {"sid": "never-existed", "cancelled": False,
                        "reason": "no live session (already ended or never running)"}
    assert harness.cancelled == ["never-existed"]


def test_cancel_without_audit_sink_fails_closed_501():
    harness = _CancellingHarness(live=["lumina-abc12345"])
    c = TestClient(_app(harness=harness, authorize=_allow_authorizer(), audit=None))
    r = c.post("/api/v1/sessions/lumina-abc12345/cancel",
               headers={"authorization": "Bearer t"})
    assert r.status_code == 501
    assert harness.cancelled == []


def test_cancel_without_authz_pdp_fails_closed_501():
    harness = _CancellingHarness(live=["lumina-abc12345"])
    c = TestClient(_app(harness=harness, authorize=None, audit=lambda s: None))
    r = c.post("/api/v1/sessions/lumina-abc12345/cancel",
               headers={"authorization": "Bearer t"})
    assert r.status_code == 501
    assert harness.cancelled == []


def test_cancel_deny_all_verifier_403_default_preserved():
    harness = _CancellingHarness(live=["lumina-abc12345"])
    c = TestClient(_app(harness=harness, verify=lambda t: False,
                        authorize=_allow_authorizer(), audit=lambda s: None))
    r = c.post("/api/v1/sessions/lumina-abc12345/cancel",
               headers={"authorization": "Bearer t"})
    assert r.status_code == 403
    assert harness.cancelled == []
