"""skcapstone change_deploy dispatch bridge (P3.2, design doc
docs/specs/2026-08-13-change-management-cab-ai-arch.md section 5.2).

Wires the deploy seam (``skcapstone.change_deploy.set_deploy_dispatcher``,
mirroring the R1 execute seam in ``agent_run.py`` exactly) into the one and
only merge authority in the change-management pipeline. Unlike the prepare
bridge (``agentrun_bridge.py``, structurally incapable of merging), this
bridge's whole purpose is a gated, audited merge, so every guard here is
load-bearing.

The pipeline is ALL-OR-REFUSE: every guard failure returns a well-formed
refusal dict (``{"refused": True, "reason": ...}``) and nothing partial ever
happens. In order (design doc section 5.2):

1. Re-fold the change (``skcoord.itil.ITILManager``, the idiom used by
   skcapstone's ``itil_tools.py`` / ``agent_run.py``): require ``scheduled``
   status, the deploy window arrived, a ``prepared_pr``, and a passing
   ``validation``.
2. capauth ``decide(runner_subject, "change.deploy", ...)`` must allow, AND
   the runner's acting subject (resolved via
   ``capauth.resolve_agent_identity``, never trusted from ``context``) must
   not equal ``prepared_by`` -- layer 3 of the design doc's three-layer
   no-self-approval invariant (section 7).
3. For the default ``deploy_mode == "confirm"``, a human arm file
   (``cab-decisions/<chg>-<agent>.arm.json``) must exist, re-verified via
   capauth (the arm actor comes from a spoofable request header at the P2.3
   route, so this bridge never trusts the file's ``agent`` field at face
   value) and must not be the drafter. ``deploy_mode == "auto"`` is refused
   outright: out of scope until Phase 3b.
4. Freshness: the validation verdict's ``head_sha`` must equal the PR's
   CURRENT head SHA (``gh pr view --json headRefOid``); a stale verdict
   never deploys.
5. Only once every guard has passed: append ``status -> implementing``,
   ``gh pr ready`` + ``gh pr merge --squash``, then (best-effort per-repo
   ``deploy_cmd`` from ``autopilot.yaml`` when one exists; otherwise
   merge == deploy, matching publish-on-main) append ``status -> deployed``.
6. Any failure from this point on appends ``status -> failed`` with the
   error -- never a partial success, never a change left mid-flight without
   a terminal event.

Sits ALONGSIDE ``agentrun_bridge.py``, never imports from or modifies it:
the two bridges are structurally independent so the prepare path's no-merge
property stays permanent regardless of what this file does.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Callable, Optional

from .config import Config

#: GitHub PR URL -> repo name, for the optional per-repo `deploy_cmd` lookup
#: (design doc section 5.2 step 5). Best-effort only: a URL that does not
#: match simply means merge == deploy (publish-on-main), never a refusal.
_PR_URL_RE = re.compile(r"github\.com/[^/]+/([^/]+)/pull/\d+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _refuse(reason: str, **extra) -> dict:
    """A well-formed fail-closed refusal dict (never a partial side effect,
    never an exception escaping the seam)."""
    out: dict = {"refused": True, "reason": reason}
    out.update(extra)
    return out


def _repo_name_from_pr_url(pr_url: str) -> Optional[str]:
    m = _PR_URL_RE.search(pr_url or "")
    return m.group(1) if m else None


def _gh(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _gh_pr_head_sha(pr_url: str) -> Optional[str]:
    """``gh pr view <pr_url> --json headRefOid`` -> the PR's CURRENT head SHA
    (mirrors skdashboard's ``_gh_pr_head_sha``; never raises)."""
    try:
        proc = _gh(["gh", "pr", "view", pr_url, "--json", "headRefOid"], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return None
    return data.get("headRefOid") if isinstance(data, dict) else None


def _within_window(now: datetime, window_start, window_end) -> bool:
    """``window_start <= now <= window_end``, tolerant of naive ISO strings
    (assumed UTC) and any malformed input (refuses rather than raising)."""
    try:
        start = datetime.fromisoformat(str(window_start))
        end = datetime.fromisoformat(str(window_end))
    except (TypeError, ValueError):
        return False
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return start <= now <= end


def _check_arm(mgr, rid: str, prepared_by: Optional[str], decide_fn) -> tuple[bool, str]:
    """Locate a valid human arm for a scheduled change (design doc section
    5.2 step 3).

    The arm file's ``agent`` field is written by skdashboard's
    ``api_change_arm`` route from the ``X-SK-Actor`` request header, which
    the P2.3 route's own docstring calls out as spoofable at that layer --
    so it is re-verified here via ``capauth.decide`` rather than trusted at
    face value, and it must not equal ``prepared_by`` (a drafter cannot arm
    their own change). Multiple arm files may exist (one per agent who
    attempted to arm, mirroring the CAB vote per-agent-file pattern); the
    first one that is both capauth-allowed and not the drafter counts.
    """
    if not mgr.cab_dir.exists():
        return False, f"no human arm found for {rid} (deploy_mode=confirm requires one)"
    candidates = sorted(mgr.cab_dir.glob(f"{rid}-*.arm.json"))
    if not candidates:
        return False, f"no human arm found for {rid} (deploy_mode=confirm requires one)"
    reasons: list[str] = []
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not data.get("armed"):
            continue
        arm_subject = data.get("agent")
        if not arm_subject:
            continue
        if arm_subject == prepared_by:
            reasons.append(f"arm by {arm_subject!r} refused: drafter cannot arm their own change")
            continue
        decision = decide_fn(arm_subject, "change.deploy", resource={"id": rid})
        if not getattr(decision, "allow", False):
            reasons.append(
                f"arm by {arm_subject!r} refused: capauth denied change.deploy "
                f"({getattr(decision, 'reason', 'denied')})"
            )
            continue
        return True, f"armed by {arm_subject}"
    reason = "no valid human arm found"
    if reasons:
        reason += ": " + "; ".join(reasons)
    return False, reason


def build_deploy_dispatcher() -> Callable[[dict], dict] | None:
    """Check the static prerequisites and return the dispatcher closure, or
    ``None`` (fail-closed) when any is missing. Returning ``None`` is a
    first-class outcome, not an error -- the deploy seam simply stays
    unwired, exactly like R1's ``build_execute_dispatcher``.

    Prerequisites (design doc section 9 phase 3a, and the hard safety
    constraint that this bridge defaults fail-closed independent of whatever
    the skcapstone-side seam wiring does):

    1. ``SKAI_DEPLOY_BRIDGE=1`` is set. This is deliberately re-checked here
       (belt-and-suspenders, matching the fold's own no-self-approval guard
       in ``skcoord.itil``): the deploy bridge must refuse to build a live
       dispatcher even if some future caller forgets the seam-side flag
       check.
    2. ``gh`` is on PATH (the merge step is unusable without it).
    3. ``capauth.authz.decide`` and ``capauth.resolve_agent_identity`` are
       importable (the no-self-approval + capability checks are unusable
       without them).
    """
    import os

    if os.environ.get("SKAI_DEPLOY_BRIDGE") != "1":
        return None
    if shutil.which("gh") is None:
        return None
    try:
        from capauth import resolve_agent_identity  # noqa: F401
        from capauth.authz import decide  # noqa: F401
    except ImportError:
        return None
    return deploy_dispatch


def deploy_dispatch(context: dict) -> dict:
    """``fn(context) -> {"refused": bool, ...}``, the deploy seam's
    dispatcher contract. ``context`` carries only ``change_id``; the acting
    (runner) identity is never taken from ``context`` -- it is resolved
    fresh via ``capauth.resolve_agent_identity`` so no caller can spoof who
    is deploying.

    Every failure path returns a well-formed refusal dict; every failure
    that occurs after the change has been marked ``implementing`` also
    appends a terminal ``status -> failed`` event, so the record is never
    left mid-flight. No exception ever escapes this function.
    """
    change_id = context.get("change_id")
    if not change_id:
        return _refuse("change_id required")

    try:
        from capauth import resolve_agent_identity
        from capauth.authz import decide
        from skcapstone.mcp_tools._helpers import _shared_root
        from skcoord.itil import Change, ITILManager
    except Exception as exc:  # noqa: BLE001 -- prerequisites vanished between
        # build_deploy_dispatcher() and dispatch (e.g. a hot-reloaded env);
        # fail closed rather than raise.
        return _refuse(f"deploy bridge prerequisites unavailable: {exc}", change_id=change_id)

    try:
        home = _shared_root()
        mgr = ITILManager(home)
        rid = mgr._resolve_id(mgr.changes_dir, change_id)
        if mgr._load_core(mgr.changes_dir, rid) is None:
            return _refuse(f"change {change_id!r} not found", change_id=change_id)
        chg = mgr._fold_record(mgr.changes_dir, rid, Change)
        if chg is None:
            return _refuse(f"change {change_id!r} could not be folded", change_id=change_id)
    except Exception as exc:  # noqa: BLE001
        return _refuse(f"failed to fold change {change_id!r}: {exc}", change_id=change_id)

    # ---- Idempotency: never a double-fire (design doc section 5.3's claim
    # lease is the primary guard; this defends here too). Nothing has been
    # attempted yet, so a plain refusal (no new "failed" event) is correct.
    if chg.status.value in ("implementing", "deployed"):
        return _refuse(
            f"change {rid} is already {chg.status.value}; refusing a double deploy",
            change_id=rid,
            status=chg.status.value,
        )

    # ---- Step 1: re-fold preconditions -------------------------------
    if chg.status.value != "scheduled":
        return _refuse(
            f"change {rid} is not scheduled (status={chg.status.value})",
            change_id=rid,
            status=chg.status.value,
        )
    window = chg.scheduled_window or {}
    if not _within_window(
        datetime.now(timezone.utc), window.get("window_start"), window.get("window_end")
    ):
        return _refuse(
            f"deploy window has not arrived (or is missing) for {rid}",
            change_id=rid,
            window=window,
        )
    if not (chg.prepared_pr and chg.prepared_pr.get("url")):
        return _refuse(f"change {rid} has no prepared_pr", change_id=rid)
    if not (chg.validation and chg.validation.get("passed")):
        return _refuse(f"change {rid} has no passing validation", change_id=rid)

    prepared_by = chg.prepared_by

    # ---- Step 2: capauth decide + no-self-approval (design doc layer 3) --
    try:
        ident = resolve_agent_identity()
    except Exception as exc:  # noqa: BLE001
        return _refuse(f"runner identity resolution failed: {exc}", change_id=rid)
    runner_subject = ident.fqid or ident.capauth_uri

    decision = decide(
        runner_subject, "change.deploy", resource={"id": rid, "prepared_by": prepared_by}
    )
    if not getattr(decision, "allow", False):
        return _refuse(
            f"capauth denied change.deploy for {runner_subject}: "
            f"{getattr(decision, 'reason', 'denied')}",
            change_id=rid,
        )
    if prepared_by and runner_subject == prepared_by:
        return _refuse(
            f"no-self-approval: runner {runner_subject} equals prepared_by {prepared_by}",
            change_id=rid,
        )

    # ---- Step 3: arm check ------------------------------------------
    deploy_mode = window.get("deploy_mode") or "confirm"
    if deploy_mode == "auto":
        return _refuse("auto deploy_mode not enabled (Phase 3b)", change_id=rid)
    if deploy_mode != "confirm":
        return _refuse(f"unknown deploy_mode {deploy_mode!r}", change_id=rid)

    arm_ok, arm_reason = _check_arm(mgr, rid, prepared_by, decide)
    if not arm_ok:
        return _refuse(arm_reason, change_id=rid)

    # ---- Step 4: freshness -------------------------------------------
    pr_url = chg.prepared_pr["url"]
    validated_sha = chg.validation.get("head_sha")
    current_sha = _gh_pr_head_sha(pr_url)
    if not current_sha:
        return _refuse(f"could not resolve current head SHA for {pr_url}", change_id=rid)
    if current_sha != validated_sha:
        return _refuse(
            f"stale validation: validated head_sha {validated_sha!r} != current PR "
            f"head {current_sha!r}; re-validate before deploying",
            change_id=rid,
            validated_sha=validated_sha,
            current_sha=current_sha,
        )

    # ---- Every guard passed. From here on, ANY failure must land a
    # terminal `status -> failed` event: the change is about to leave
    # `scheduled`, so it must never be left mid-flight. --------------------
    def _fail(reason: str) -> dict:
        try:
            mgr._append_event(
                mgr.changes_dir, rid, runner_subject, "status", to="failed", note=reason
            )
        except Exception:  # noqa: BLE001 -- the refusal must still return even
            # if the append itself fails; this is a last-ditch defensive guard.
            pass
        return _refuse(reason, change_id=rid)

    try:
        mgr._append_event(
            mgr.changes_dir,
            rid,
            runner_subject,
            "status",
            to="implementing",
            note="deploy bridge: window arrived, capauth allowed, armed, validation fresh",
        )
    except Exception as exc:  # noqa: BLE001 -- nothing has mutated the repo
        # yet, so a plain refusal (no "failed" event, there is nothing to
        # fail from) is correct here.
        return _refuse(f"failed to record implementing: {exc}", change_id=rid)

    try:
        ready = _gh(["gh", "pr", "ready", pr_url], timeout=30)
        if ready.returncode != 0:
            return _fail(f"gh pr ready failed: {(ready.stderr or '').strip() or 'unknown error'}")

        merge = _gh(["gh", "pr", "merge", "--squash", pr_url], timeout=120)
        if merge.returncode != 0:
            return _fail(f"gh pr merge failed: {(merge.stderr or '').strip() or 'unknown error'}")

        deploy_note = "merge == deploy (publish-on-main)"
        repo_name = _repo_name_from_pr_url(pr_url)
        if repo_name:
            cfg = Config.load()
            repo = cfg.repo_map.get(repo_name)
            deploy_cmd = getattr(repo, "deploy_cmd", None) if repo else None
            if deploy_cmd:
                try:
                    proc = subprocess.run(
                        deploy_cmd,
                        shell=True,
                        cwd=(repo.path if repo else None),
                        capture_output=True,
                        text=True,
                        timeout=1800,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    return _fail(f"deploy_cmd failed to run: {exc}")
                if proc.returncode != 0:
                    return _fail(
                        f"deploy_cmd failed: {(proc.stderr or '').strip() or 'unknown error'}"
                    )
                deploy_note = f"deploy_cmd: {deploy_cmd}"

        mgr._append_event(
            mgr.changes_dir, rid, runner_subject, "status", to="deployed", note=deploy_note
        )
        return {"refused": False, "change_id": rid, "status": "deployed", "pr": pr_url}
    except Exception as exc:  # noqa: BLE001 -- fail closed: no exception ever
        # escapes the deploy seam once implementing has been recorded.
        return _fail(f"unexpected error during deploy: {exc}")
