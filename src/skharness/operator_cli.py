"""skcode-hostd operator facet CLI (SKWorld platform spec 4.2, card R2.14).

The operator facet is the CLI-first seam Atlas drives to watch and steer
skcode-hostd. It exposes three verbs:

    skcode-hostd operator explain           # the operator-facet contract (JSON)
    skcode-hostd operator observe           # current conditions (JSON)
    skcode-hostd operator act <action>      # a reversible standard action

This is the CANONICAL side of the contract that ``skcapstone``'s Atlas adapter
(``operator_seat/skcode_adapter.py``) mirrors: that adapter's ``skcode_act``
shells exactly ``["skcode-hostd", "operator", "act", "archive-stale-session",
"--session", <sid>]``, so this CLI must honor that act. The two live in separate
repos; the shared schema in sk-standards is the source of truth, and the shapes
here are kept byte-compatible with ``skcode_explain`` / ``skcode_observe``.

Contract (spec 4.2 semantics):

Conditions:
  - HostdReady: the :9394 API answers on this host.
  - SessionsHealthy: no running session is stale past the wedge threshold (the
    runaway/wedge detector).
  - RegistryConsistent: every registry entry reconciles against a live PTY/tmux
    backing; an orphan (registry entry with no backing) flips it False.
  - AuthEnforced: a REAL verifier is active (not the P0 deny-all placeholder and
    not a permissive stub).

Actions:
  - restart-hostd (standard, reversible, low): systemctl --user restart + verify.
  - archive-stale-session (standard, reversible): archive is stop + persist, via
    ``harness.archive(sid)`` (never a destructive kill).
  - kill-runaway-session (NOT standard, reversible false): escalates as MAJOR by
    the irreversibility rule, so the CLI refuses to act and reports the escalation.
  - pause-dispatch (not standard, reversible, low): the emergency brake on the RCE
    surface (dispatch P2). Flips a persisted flag; while set, POST /dispatch returns
    503 regardless of auth. --resume clears it.

Every probe fails SAFE (reports healthy) when hostd is unreachable, mirroring the
adapter's ``_probe_hostd``. The observe probe and the act runner/harness are all
injectable so tests never touch real systemd, tmux, or the network.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

# --- contract constants (mirror operator_seat/skcode_adapter.py) --------------

CONDITIONS = ["HostdReady", "SessionsHealthy", "RegistryConsistent", "AuthEnforced"]

#: The kinds this operator facet describes (mirrors skcode_explain).
KINDS = ["hostd", "session", "registry", "dispatch"]

#: A running session with no event for longer than this is wedged/runaway.
_SESSION_STALE_S = 900
_HOSTD_UNIT = "skcode-hostd.service"
_HOSTD_HEALTH_URL = "http://localhost:9394/api/v1/hosts/self"

#: Action catalog, byte-compatible with skcode_adapter.py::_ACTIONS.
_ACTIONS = [
    {
        "name": "restart-hostd",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "systemctl --user restart skcode-hostd and verify HostdReady",
        "kedb_refs": ["ke-hostd-wedge"],
    },
    {
        "name": "archive-stale-session",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "archive a wedged session (stop + persist, never a destructive kill)",
        "kedb_refs": ["ke-session-runaway"],
    },
    {
        "name": "kill-runaway-session",
        "standard": False,
        "reversible": False,
        "blast_radius": "low",
        "runbook": "kill a runaway session (irreversible: escalates as MAJOR with options)",
        "kedb_refs": ["ke-session-runaway"],
    },
    {
        "name": "pause-dispatch",
        "standard": False,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "flip the dispatch-enable flag off (emergency brake on the RCE surface)",
        "kedb_refs": [],
    },
]

_ACTION_NAMES = {a["name"] for a in _ACTIONS}


def _b(value: bool) -> str:
    """Render a bool as the contract's string status ('True' | 'False')."""
    return "True" if value else "False"


# --- dispatch emergency brake: a simple persisted flag hostd reads -----------


def dispatch_pause_path() -> Path:
    """The persisted dispatch-pause flag file.

    Rooted at ``SKCODE_STATE_DIR`` when set (tests point it at a tmp dir), else
    ``~/.skcapstone/skcode``. The flag is presence-based: the file EXISTS iff
    dispatch is paused. The daemon reads it via :func:`dispatch_is_paused` and,
    while it exists, POST /api/v1/dispatch returns 503 regardless of auth.
    """
    root = os.environ.get("SKCODE_STATE_DIR")
    base = Path(root) if root else Path.home() / ".skcapstone" / "skcode"
    return base / "dispatch.paused"


def dispatch_is_paused() -> bool:
    """True iff the dispatch-pause flag file exists (the emergency brake is on)."""
    return dispatch_pause_path().exists()


def set_dispatch_paused(paused: bool) -> Path:
    """Flip the persisted dispatch-pause flag on (create) or off (remove).

    Reversible by construction: pausing writes the flag file, resuming removes it.
    Returns the flag path either way.
    """
    p = dispatch_pause_path()
    if paused:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"paused_at": time.time()}), encoding="utf-8")
    else:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    return p


# --- pure probe logic (unit-tested directly, mirrors the adapter) ------------


def _sessions_healthy(sessions: list[dict], stale_s: int = _SESSION_STALE_S) -> bool:
    """False when any running session's last event is older than the threshold.

    A session dict carries ``state`` and ``last_event_age_s``. Non-running
    sessions and sessions with unknown age never fire (fail safe).
    """
    for s in sessions or ():
        if s.get("state") != "running":
            continue
        age = s.get("last_event_age_s")
        if age is not None and age > stale_s:
            return False
    return True


def _registry_consistent(registry_ids, live_ids) -> bool:
    """False when a registry entry has no live backing (an orphan).

    Consistent means every registered session id is backed by a live PTY/tmux id.
    """
    return set(registry_ids or ()) <= set(live_ids or ())


# --- default (real) probe: reads the local hostd, fails SAFE = healthy -------


def _default_probe() -> dict:
    """Best-effort skcode-hostd read. Fails SAFE (all healthy) when unreachable.

    Mirrors ``skcode_adapter._probe_hostd``: hits the local :9394 API, parses the
    session list + auth flag, and derives the four condition inputs. ANY failure
    (connection refused, 401, malformed body) returns the all-healthy state so the
    operator loop never pages falsely.
    """
    try:
        import urllib.request

        url = os.environ.get("SKCODE_HOSTD_HEALTH", _HOSTD_HEALTH_URL)
        with urllib.request.urlopen(url, timeout=8) as r:  # noqa: S310 (local tailnet)
            body = json.loads(r.read())
        sessions = body.get("sessions", []) if isinstance(body, dict) else []
        registry_ids = [s.get("id") for s in sessions]
        live_ids = [s.get("id") for s in sessions if s.get("backing_alive", True)]
        auth = body.get("auth_enforced") if isinstance(body, dict) else None
        return {
            "hostd_ready": True,
            "sessions_healthy": _sessions_healthy(sessions),
            "registry_consistent": _registry_consistent(registry_ids, live_ids),
            "auth_enforced": True if auth is None else bool(auth),
        }
    except Exception:
        return {
            "hostd_ready": True,
            "sessions_healthy": True,
            "registry_consistent": True,
            "auth_enforced": True,
        }


# --- contract verbs ----------------------------------------------------------


def operator_explain() -> dict:
    """skcode-hostd's operator-facet self-description in the contract shape."""
    return {
        "kinds": list(KINDS),
        "conditions": list(CONDITIONS),
        "actions": [dict(a) for a in _ACTIONS],
    }


def operator_observe(probe: Callable[[], dict] | None = None) -> dict:
    """Read-only skcode-hostd health snapshot in the adapter-contract shape.

    Maps the probe's four boolean inputs onto the four conditions. Each condition
    defaults to healthy when its input is absent (fail safe). ``probe`` is
    injectable so tests drive each condition firing without any I/O.
    """
    st = (probe or _default_probe)()
    return {
        "conditions": [
            {
                "type": "HostdReady",
                "status": _b(bool(st.get("hostd_ready", True))),
                "object": "skcode-hostd",
            },
            {
                "type": "SessionsHealthy",
                "status": _b(bool(st.get("sessions_healthy", True))),
                "object": "sessions",
            },
            {
                "type": "RegistryConsistent",
                "status": _b(bool(st.get("registry_consistent", True))),
                "object": "registry",
            },
            {
                "type": "AuthEnforced",
                "status": _b(bool(st.get("auth_enforced", True))),
                "object": "verifier",
            },
        ]
    }


# --- act effects (injectable so tests never touch systemd/tmux) --------------


def _default_runner(argv: list[str]) -> subprocess.CompletedProcess:
    """Run a systemd/user command, returning the CompletedProcess (never raises)."""
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _default_harness():
    """The claude-code harness that backs this host's session plane."""
    from skharness.harnesses.claude_code import ClaudeCodeHarness

    return ClaudeCodeHarness(host=os.environ.get("SKCODE_HOST_ID", ".158"))


def operator_act(
    action: str,
    session: str | None = None,
    *,
    runner: Callable[[list[str]], object] | None = None,
    harness=None,
    resume: bool = False,
) -> dict:
    """Perform a reversible standard skcode action; report the outcome as a dict.

    - ``restart-hostd`` runs ``systemctl --user restart skcode-hostd.service`` via
      the injected ``runner`` (or the default subprocess runner).
    - ``archive-stale-session`` calls ``harness.archive(sid)`` (stop + persist, the
      documented reversible path; ``--session`` required). The session-plane body
      is deferred to skcode P1 (harness.py::archive), so on a harness that has not
      implemented it yet this reports ``performed: False`` with a clear reason
      rather than doing anything destructive.
    - ``kill-runaway-session`` is NOT standard and irreversible: it escalates as
      MAJOR by construction, so the CLI refuses to act and returns the escalation.
    - ``pause-dispatch`` flips the persisted dispatch-pause flag on (the RCE
      emergency brake); ``resume=True`` clears it. While set, the daemon returns
      503 for every POST /dispatch regardless of auth.

    Raises ``ValueError`` on an unknown action (the caller refuses cleanly).
    """
    if action not in _ACTION_NAMES:
        raise ValueError(
            f"unknown action {action!r}; known: {sorted(_ACTION_NAMES)}"
        )

    if action == "restart-hostd":
        run = runner or _default_runner
        cp = run(["systemctl", "--user", "restart", _HOSTD_UNIT])
        rc = getattr(cp, "returncode", 0)
        return {
            "performed": rc == 0,
            "action": action,
            "unit": _HOSTD_UNIT,
            "returncode": rc,
        }

    if action == "archive-stale-session":
        if not session:
            raise ValueError("archive-stale-session requires --session <sid>")
        h = harness if harness is not None else _default_harness()
        try:
            asyncio.run(h.archive(session))
        except NotImplementedError:
            # Honest stub: the session-plane archive body lands with skcode P1
            # (harness.py::archive). Never fall back to a destructive path.
            return {
                "performed": False,
                "action": action,
                "session": session,
                "reason": (
                    "archive is not implemented on this harness yet "
                    "(session plane P1, harness.py::archive)"
                ),
            }
        return {"performed": True, "action": action, "session": session}

    if action == "kill-runaway-session":
        return {
            "performed": False,
            "action": action,
            "escalated": True,
            "reason": (
                "kill-runaway-session is not a standard action: irreversible, so "
                "it escalates as MAJOR with options (archive vs kill vs inspect)"
            ),
        }

    # pause-dispatch: the emergency brake on the RCE surface (spec 7.5). Flips the
    # persisted flag ON so the daemon returns 503 for every POST /dispatch,
    # regardless of auth. Reversible: pass resume=True (the CLI --resume flag) to
    # remove the flag and re-arm dispatch.
    paused = not resume
    path = set_dispatch_paused(paused)
    return {
        "performed": True,
        "action": action,
        "paused": paused,
        "flag": str(path),
        "reason": (
            "dispatch paused: POST /api/v1/dispatch now returns 503 regardless of auth"
            if paused else
            "dispatch resumed: the pause flag was cleared"
        ),
    }


# --- CLI ---------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skcode-hostd operator",
        description="skcode-hostd operator facet (explain / observe / act)",
    )
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("explain", help="print the operator-facet contract as JSON")
    sub.add_parser("observe", help="print the current conditions as JSON")
    p_act = sub.add_parser("act", help="perform a reversible standard action")
    p_act.add_argument("action", help="one of: " + ", ".join(sorted(_ACTION_NAMES)))
    p_act.add_argument("--session", default=None, help="session id (for archive/kill)")
    p_act.add_argument("--resume", action="store_true",
                       help="for pause-dispatch: clear the pause flag (re-arm dispatch)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``skcode-hostd operator ...``. Returns a process exit code.

    0 = success (explain/observe, or an act that performed). 1 = a recognized act
    that did not perform (archive not implemented, kill escalated, pause not
    enabled, restart failed). 2 = unknown action or a usage error.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.verb == "explain":
        print(json.dumps(operator_explain(), indent=2))
        return 0

    if args.verb == "observe":
        print(json.dumps(operator_observe(), indent=2))
        return 0

    # act
    try:
        result = operator_act(args.action, session=args.session, resume=args.resume)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result.get("performed") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
