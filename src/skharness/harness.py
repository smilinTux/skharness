"""The unified two-plane Harness contract (Fable Wave 1 "contract hoist").

ONE harness abstraction with two capability-gated planes, reconciling the autocode
verbs and the skcode remote-control verbs into a single seam (extraction ADR
Decision 2):

  * Task plane (sync, sandboxed, one-shot): assess / run_task / grade. These are
    exactly the existing ``skharness.autocode.harness.HarnessAdapter`` methods,
    kept verbatim in contract; the autocode loop drives them.
  * Session plane (async, long-lived): spawn / list_sessions / stream / inject /
    set_model / get_branch / background_tasks / archive. Declared per skcode ADR
    3.1; the remote-control daemon drives them. Bodies are NOT implemented here
    (the skcode P0 pivot fills them for claude-code); this module defines only the
    contract surface.

Reconciliation with the existing engine (behavior-preserving): the unified
registry IS the autocode registry (the SAME ``HARNESSES`` dict object). The engine
keeps calling ``skharness.autocode.harness.build_harness(config, name)`` unchanged;
this module adds the unified public surface (``build_harness(name, config)``,
name-first per the ADR) over the same dict, so "which harnesses exist on this host"
has one answer for both the engine and the daemon. The four existing adapters
(claude-code / codex / opencode / pi) register themselves into that one dict via
``skharness.autocode.adapters`` and now declare the merged capability cells; their
task-plane code is untouched.

Capability shape note (deviation from the task brief, matching the frozen ADR):
``HarnessCapabilities`` is a ``TypedDict`` (a dict at runtime), not a dataclass, so
the existing dict-based ``capabilities()`` returns and the ``warn_missing_capabilities``
``.get()`` pattern keep working verbatim; ``headless_api`` is a tier string
("server" | "pty" | "none") as the ADR 3.1 capability map specifies, not a bool.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, fields
from typing import TYPE_CHECKING, AsyncIterator, TypedDict

# The unified registry IS the autocode registry: one dict, one answer. Re-exported
# so callers use skharness.harness (public) while the engine keeps its own import.
from skharness.autocode.harness import (
    HARNESSES,  # noqa: F401  (re-export)
    register_harness,  # noqa: F401  (re-export)
    warn_missing_capabilities,  # noqa: F401
)

# The one typed stream event lives in skharness.events; re-exported here so the
# session-plane signatures and callers share a single SessionEvent (no placeholder).
from skharness.events import EventType, SessionEvent  # noqa: F401  (re-export)

if TYPE_CHECKING:                               # task-plane types (no runtime import)
    from skharness.autocode.types import (
        AssessBrief,
        GateResult,
        GradeBrief,
        HarnessResult,
        TaskBrief,
        Verdict,
    )


class SpawnRejected(ValueError):  # noqa: N818  (raised-participle name, as ClaimRaced)
    """Raised by a Harness.spawn when a dispatch request fails an RCE input guard.

    This is the input-validation floor that stands INDEPENDENT of auth (spec
    7.3): even a fully authenticated, authorized caller is refused here when the
    repo is not on the per-host allowlist, the branch fails ``git
    check-ref-format``, or the agent/session name breaks the ``[A-Za-z0-9_-]+``
    charset. The daemon maps it to an HTTP 400 (bad request), never a 5xx, and
    NOTHING is spawned. Fail closed: any uncertainty raises this rather than
    proceeding to a subprocess.
    """


# ---------------------------------------------------------------------------
# Capability shape
# ---------------------------------------------------------------------------

class HarnessCapabilities(TypedDict):
    """Merged capability cells. The four existing autocode cells (kept verbatim
    from ``ProviderCapabilities``) plus the four skcode cells (ADR 3.2)."""
    # existing autocode cells (harness.py ProviderCapabilities, verbatim)
    session_resume: bool
    structured_output: str          # "none" | "json" | "schema"
    sandbox: bool
    tool_restrictions: bool
    # merged skcode cells (skcode ADR 3.2 capability map)
    task_plane: bool                # assess/run_task/grade implemented
    session_plane: bool             # spawn/stream/inject implemented
    headless_api: str               # "server" | "pty" | "none"
    hot_set_model: bool


# ---------------------------------------------------------------------------
# Session-plane placeholder types
#
# The session plane is contract-only in this task; these light placeholders give
# the async signatures meaning without pulling in the (later) skcode session
# model. They will be unified with skharness.session / skcode when the plane lands.
# ---------------------------------------------------------------------------

@dataclass
class SessionDescriptor:
    """The one session record (skcode ADR 2).

    Captures a session both as a spawn request (all fields default, no sid yet)
    and as a live/historical row returned by the read-only session plane. This is
    what ``list_sessions()`` yields and the daemon serializes over ``/api/v1``.
    """
    sid: str = ""                    # session id (tmux window name for claude-code)
    host: str = ""                   # node id, e.g. ".158"
    harness: str = ""                # "pi" | "opencode" | "claude-code" | "fake"
    repo: str = ""                   # allowlisted repo root
    branch: str = ""                 # git branch / worktree ref
    model: str = ""                  # role or concrete id (resolved via skgateway)
    state: str = "running"           # "running" | "idle" | "ended" | "spawning"
    last_activity: float = 0.0       # epoch seconds of last observed activity
    last_message: str = ""           # last assistant line (for the list preview)
    quality: str = "sandbox"         # profile / quality tier: "full" | "sandbox"
    permission_mode: str = "manual"  # per-action approval posture: "manual" | "auto"
    mode: str = "direct"             # session mode: "direct" (print/one-shot) | "interactive" (stays open)
    # Additive (skcode Code-section card C-1, spec 2026-08-11 section 5.1): the
    # same source vocabulary as SessionEvent.source ("interactive" | "autocode"
    # | "attach"), so a session shows its origin in the rail before any event
    # has arrived. Defaults "interactive" (every session this plane produced
    # before v2), so an old record round-trips unchanged.
    source: str = "interactive"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SessionDescriptor":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class HarnessSession:
    """A live (or archived) session handle the write session plane returns
    (spawn/fork). Read-only P0 does not construct these; kept for the spawn
    signature the write plane will fill later."""
    sid: str
    descriptor: SessionDescriptor | None = None
    status: str = "spawning"        # spawning | running | archived
    branch: str = ""
    forked_from: str | None = None


@dataclass
class InputMessage:
    """Operator input injected into a session: text | transcribed voice | files."""
    kind: str                       # "text" | "voice" | "files"
    content: str = ""
    files: list[str] = field(default_factory=list)


@dataclass
class BackgroundTask:
    """One of the harness's own async/subagent tasks (mapped onto skcoord cards)."""
    id: str
    status: str
    description: str = ""


# ---------------------------------------------------------------------------
# The one contract
# ---------------------------------------------------------------------------

class Harness(ABC):
    """One harness, two capability-gated planes.

    ``capabilities()`` declares which planes exist; no adapter is forced to
    implement both. Task-plane adapters override assess/run_task/grade and leave
    the session plane at its gated default; session-plane adapters override the
    async methods and (optionally) gain the task plane via ``SessionTaskBridge``.
    Callers gate on ``capabilities()`` (see ``warn_missing_capabilities``) rather
    than probing methods, so a missing plane is DETECTED, never a mid-run crash.
    """

    name: str = "harness"

    @abstractmethod
    def capabilities(self) -> HarnessCapabilities:
        """Declare this harness's capability cells (both planes)."""

    # ---- task plane (sync, sandboxed, one-shot): the autocode loop drives it ---
    # Default to a gated raise so a SESSION-only adapter is not forced to
    # implement them. Task-plane adapters (BaseCliAdapter et al.) override these.

    def assess(self, brief: "AssessBrief") -> "Verdict":
        raise NotImplementedError(
            f"harness {self.name!r} does not implement the task plane (assess)")

    def run_task(self, brief: "TaskBrief") -> "HarnessResult":
        raise NotImplementedError(
            f"harness {self.name!r} does not implement the task plane (run_task)")

    def grade(self, brief: "GradeBrief") -> "GateResult":
        raise NotImplementedError(
            f"harness {self.name!r} does not implement the task plane (grade)")

    # ---- session plane (async, long-lived): skcode remote control drives it ----
    # Declared per skcode ADR 3.1. Bodies land in the skcode P0 pivot (claude-code
    # first, absorbing the TmuxSpawner/jarvis-heartbeat PTY pattern). Gated raise
    # by default so a TASK-only adapter is not forced to implement them.

    async def spawn(self, desc: SessionDescriptor, *, prompt: str) -> HarnessSession:
        """Start a NEW session (the Dispatch unlock)."""
        raise NotImplementedError(
            f"harness {self.name!r} does not implement the session plane (spawn)")

    async def list_sessions(self) -> list["SessionDescriptor"]:
        """Live (and, for claude-code, historical) sessions on THIS host."""
        raise NotImplementedError(
            f"harness {self.name!r} does not implement the session plane (list_sessions)")

    async def stream(self, sid: str) -> AsyncIterator[SessionEvent]:
        """Ordered event stream for a session."""
        raise NotImplementedError(
            f"harness {self.name!r} does not implement the session plane (stream)")

    async def inject(self, sid: str, text: str) -> dict:
        """Inject operator text into a running session as keystrokes (P1 WRITE).

        Returns a small result dict, e.g. ``{"sid": sid, "injected": True}`` on
        success, or ``{"sid": sid, "injected": False, "reason": ...}`` when the
        session is unknown/invalid (a clean no-op, never a raise, and it never
        touches the PTY in that case). This is a WRITE verb: the daemon route
        that drives it stays behind the capauth bearer gate (see daemon.py).
        """
        raise NotImplementedError(
            f"harness {self.name!r} does not implement the session plane (inject)")

    async def set_model(self, sid: str, selection: str) -> None:
        """Hot model switch: write the session:<id> resolver pin, rebind the loop."""
        raise NotImplementedError(
            f"harness {self.name!r} does not implement the session plane (set_model)")

    async def get_branch(self, sid: str) -> str:
        """Current git branch / worktree ref of the session."""
        raise NotImplementedError(
            f"harness {self.name!r} does not implement the session plane (get_branch)")

    async def background_tasks(self, sid: str) -> list[BackgroundTask]:
        """Enumerate the harness's own async/subagent tasks."""
        raise NotImplementedError(
            f"harness {self.name!r} does not implement the session plane (background_tasks)")

    async def archive(self, sid: str) -> dict:
        """Stop + persist the session (not a destructive kill).

        Returns a small result dict, e.g. ``{"sid": sid, "archived": True,
        "transcript_path": ...}`` on success, or ``{"sid": sid, "archived":
        False, "reason": ...}`` when the session is unknown/already gone (a
        clean no-op, never a raise). Implementations MUST persist the transcript
        BEFORE stopping the PTY so a failure never loses the record.
        """
        raise NotImplementedError(
            f"harness {self.name!r} does not implement the session plane (archive)")


# ---------------------------------------------------------------------------
# FakeHarness: the session-plane CI double (read-only).
#
# In-memory seeded sessions + per-sid event lists. Drives every skcode-hostd
# daemon test with no real tmux and no real bind. It overrides ONLY the read-only
# subset (list_sessions + stream); every write verb stays at the base gated raise,
# so the double has no write path of its own.
# ---------------------------------------------------------------------------

class FakeHarness(Harness):
    name = "fake"

    def __init__(
        self,
        *,
        sessions: list[SessionDescriptor] | None = None,
        events: dict[str, list[SessionEvent]] | None = None,
    ) -> None:
        self._sessions = list(sessions) if sessions else []
        self._events = dict(events) if events else {}

    def capabilities(self) -> HarnessCapabilities:
        return {"session_resume": False, "structured_output": "none",
                "sandbox": False, "tool_restrictions": False,
                "task_plane": False, "session_plane": True,
                "headless_api": "none", "hot_set_model": False}

    async def list_sessions(self) -> list[SessionDescriptor]:
        return list(self._sessions)

    async def stream(self, sid: str) -> AsyncIterator[SessionEvent]:
        for ev in self._events.get(sid, []):
            yield ev


# ---------------------------------------------------------------------------
# The two default bridges (one-directional, optional). Structure + docstrings
# only; fleshed out when the session plane lands.
# ---------------------------------------------------------------------------

class SessionTaskBridge:
    """Mixin: give a SESSION-capable adapter the task plane for free.

    The bridge maps each one-shot task verb onto the session plane: compose the
    brief into a prompt, ``spawn`` a one-shot sandboxed session, await its terminal
    status over ``stream``, then collect and parse the result. This is how a future
    server-API pi adapter serves the engine with no new code in the engine.

    Structure only for now: ``_oneshot`` drives the real async session plane, so it
    works the moment the mixed-in adapter implements spawn/stream/archive. A
    session-capable adapter mixes this in and declares ``task_plane: True``.
    """

    def _oneshot(self, descriptor: SessionDescriptor, prompt: str) -> dict:
        """Spawn a one-shot session, run to terminal, return the collected result.

        Deferred: drives ``self.spawn`` / ``self.stream`` / ``self.archive`` (the
        session plane) once those are implemented. Raises until then so a
        half-wired adapter fails loudly rather than silently returning nothing.
        """
        raise NotImplementedError(
            "SessionTaskBridge._oneshot lands with the session plane "
            "(skcode P0 pivot); it will drive spawn/stream/archive")

    def assess(self, brief: "AssessBrief") -> "Verdict":
        raise NotImplementedError("SessionTaskBridge.assess: see _oneshot")

    def run_task(self, brief: "TaskBrief") -> "HarnessResult":
        raise NotImplementedError("SessionTaskBridge.run_task: see _oneshot")

    def grade(self, brief: "GradeBrief") -> "GateResult":
        raise NotImplementedError("SessionTaskBridge.grade: see _oneshot")


class TaskSessionShim:
    """Mixin: give a one-shot TASK adapter a minimal read-only session plane.

    A running ``run_task`` is exposed as an observable session: ``stream`` yields the
    sandbox stdout as ``SessionEvent``s so skcode's UI can watch an autocode round
    live, and ``inject`` reports unsupported (the shim is read-only). This is a thin
    wrapper, not a real interactive session; ``capabilities()`` on the mixing adapter
    should advertise ``session_plane: True`` only with ``headless_api: "none"`` and
    ``hot_set_model: False`` so callers know the plane is observe-only.
    """

    async def stream(self, sid: str) -> AsyncIterator[SessionEvent]:
        """Yield the running run_task's sandbox stdout as SessionEvents.

        Deferred: wired to the sandbox stdout tail when the shim is activated for
        the CLI adapters. Declared async-generator-shaped so the contract type is
        right; raises until wired.
        """
        raise NotImplementedError(
            "TaskSessionShim.stream lands with the read-only session plane")
        if False:                               # pragma: no cover - keeps this an async generator
            yield SessionEvent(type=EventType.STATUS)

    async def inject(self, sid: str, text: str) -> dict:
        """Read-only shim: injection is unsupported (declared via capabilities)."""
        raise NotImplementedError(
            "TaskSessionShim is read-only; inject is unsupported "
            "(capabilities advertise hot_set_model=False)")


# ---------------------------------------------------------------------------
# Fail-closed builder (unified, name-first per ADR 2.4)
# ---------------------------------------------------------------------------

def build_harness(name: str, config) -> Harness:
    """Construct the harness registered under ``name`` from ``config``.

    Fail-closed: an unknown name raises ``ValueError`` listing the registered
    adapters. Name-first per the frozen public API (ADR 2.4); the engine's own
    ``skharness.autocode.harness.build_harness(config, name)`` stays as-is over the
    SAME registry dict.
    """
    import skharness.autocode.adapters  # noqa: F401  ensure adapters self-register
    factory = HARNESSES.get(name)
    if factory is None:
        raise ValueError(
            f"unknown harness {name!r}; registered: {sorted(HARNESSES)}")
    return factory(config)


__all__ = [
    "Harness", "HarnessCapabilities", "HARNESSES",
    "register_harness", "build_harness", "warn_missing_capabilities",
    "SessionTaskBridge", "TaskSessionShim", "FakeHarness",
    "SessionDescriptor", "HarnessSession", "SessionEvent", "EventType",
    "InputMessage", "BackgroundTask", "SpawnRejected",
]
