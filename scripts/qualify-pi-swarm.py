#!/usr/bin/env python3
"""Run the frozen v0.3.38 Pi S/M/L swarm qualification, fail closed.

Without ``--execute`` this command only validates and prints the immutable plan.
Live Docker workers require an exact digest reference, the qualified .41 host,
clean isolated worktrees, and the explicit execution flag. It never mutates the
coordination board, pushes a branch, or treats a worker result as completion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from skharness.activity import ActivityJournal
from skharness.arena.atlas_control import SwarmAtlasControlOwner
from skharness.arena.controller import ArenaController
from skharness.arena.models import canonical_digest
from skharness.arena.runner import PiExperimentRunner, SandboxProcessSupervisor, pi_launch_spec
from skharness.arena.scheduler import AttemptRequest, LeaseScheduler, ResourceRequest
from skharness.arena.store import ArenaStore
from skharness.arena.swarm import (
    BudgetUsage,
    ExecutionBudget,
    SubagentContract,
    SwarmIdentity,
    SwarmPhaseSpec,
    SwarmPlan,
    SwarmRole,
    TeamBudget,
)
from skharness.arena.swarm_control import SwarmScheduler
from skharness.arena.swarm_orchestrator import (
    A2AJournal,
    TrustedSwarmOrchestrator,
    WorkerExecution,
)
from skharness.arena.swarm_pi import (
    CancellationToken,
    PiSwarmLaunch,
    PiSwarmWorkerRuntime,
    WorkerCancellationError,
)
from skharness.arena.swarm_verifier import SwarmCompletionGate
from skharness.arena.trajectory import CardSize, PhaseBudget
from skharness.autocode.adapters.pi import PiAdapter
from skharness.autocode.pi_events import (
    assistant_message_events,
    scan_pi_events,
    served_model_evidence,
)
from skharness.autocode.sandbox import Sandbox
from skharness.autocode.sandbox_lifecycle import (
    LIFECYCLE_SCHEMA,
    MANAGED_LABEL,
    OWNERSHIP_AUTHORITY_LABEL,
    RESOURCE_ROLE_LABEL,
    RUN_ID_LABEL,
    SCHEMA_LABEL,
    SandboxOwnership,
)
from skharness.control import ControlJournal

SCHEMA = "skharness.pi-swarm.sml.v2"
QUALIFIED_HOST = "cbrd21-laptop12thgenintelcore"
WORKER_BASE_COMMIT = "2e8e4d89aac1967fb297c0558b311998a9bc1e9a"
QUALIFIED_IMAGE = (
    "ghcr.io/smilintux/skharness-pi-python-test@"
    "sha256:8e991c893e7553522369a35d10b78ae2e831eb62b9f127ba53a7dabd045e2c7d"
)
RELEASE_EVIDENCE_PATH = Path("docs/evidence/pi-python-test-v0.3.38.release.json")
RELEASE_EVIDENCE = {
    "image": QUALIFIED_IMAGE,
    "package_version": "0.3.38",
    "publish_job": (
        "https://github.com/smilinTux/skharness/actions/runs/32532127259/job/96926059810"
    ),
    "publish_run": "https://github.com/smilinTux/skharness/actions/runs/32532127259",
    "schema": "skharness.pi-image-release.v1",
    "signature_identity": (
        "https://github.com/smilinTux/skharness/.github/workflows/"
        "pi-image.yml@refs/tags/v0.3.38"
    ),
    "signature_issuer": "https://token.actions.githubusercontent.com",
    "source_commit": WORKER_BASE_COMMIT,
    "tag": "v0.3.38",
    "vulnerability_job": (
        "https://github.com/smilinTux/skharness/actions/runs/32532127259/job/96926773625"
    ),
}
GATEWAY = "http://100.86.156.5:18780/v1"
MODEL = "ornith-1.5-9b"
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
CONTROLLER_MODULES = (
    "skharness",
    "skharness.activity",
    "skharness.control",
    "skharness.arena.atlas_control",
    "skharness.arena.runner",
    "skharness.arena.swarm",
    "skharness.arena.swarm_control",
    "skharness.arena.swarm_orchestrator",
    "skharness.arena.swarm_pi",
    "skharness.arena.swarm_verifier",
    "skharness.autocode.adapters.pi",
    "skharness.autocode.pi_events",
    "skharness.autocode.sandbox",
    "skharness.autocode.sandbox_lifecycle",
)
CONTROLLER_TEST_TIMEOUT_S = 90
CONTROLLER_RUFF_TIMEOUT_S = 30
CONTROLLER_CLEANUP_TIMEOUT_S = 10
CONTROLLER_GIT_TIMEOUT_S = 5
CONTROLLER_GIT_COMMAND_LIMIT = 6
CONTROLLER_OVERHEAD_RESERVE_S = 10
BUILDER_POST_RUN_RESERVE_S = (
    CONTROLLER_TEST_TIMEOUT_S
    + CONTROLLER_CLEANUP_TIMEOUT_S
    + CONTROLLER_RUFF_TIMEOUT_S
    + CONTROLLER_CLEANUP_TIMEOUT_S
    + CONTROLLER_GIT_TIMEOUT_S * CONTROLLER_GIT_COMMAND_LIMIT
    + CONTROLLER_OVERHEAD_RESERVE_S
)
CONTROLLER_STOP_DRAIN_TIMEOUT_S = 20
QUALIFIER_DOCKER_INVENTORY_TIMEOUT_S = 15


class QualificationQuiescenceError(RuntimeError):
    """The controller cannot prove all worker and post-run activity stopped."""


@dataclass(frozen=True)
class WorkerTemplate:
    contract_id: str
    phase_id: str
    role: SwarmRole
    task: str
    readable_paths: tuple[str, ...]
    writable_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    tools: tuple[str, ...]
    wall_seconds: int
    pi_wall_seconds: int
    controller_reserve_seconds: int
    token_limit: int
    tool_limit: int
    phase_budget: PhaseBudget


@dataclass(frozen=True)
class Candidate:
    card_id: str
    size: CardSize
    card_hash: str
    suitability: str
    phases: tuple[tuple[str, SwarmRole, tuple[str, ...], tuple[str, ...]], ...]
    workers: tuple[WorkerTemplate, ...]
    allowed_changes: frozenset[str]
    required_changes: frozenset[str]
    controller_tests: tuple[str, ...]
    max_concurrency: int


COMMON_READ = (".git", "src", "tests", "docs", "pyproject.toml", "SOP.md")
READ_TOOLS = ("read", "bash", "grep", "find", "ls")
BUILD_TOOLS = ("read", "edit", "write", "bash", "grep", "find", "ls")

S_PROMPT = """Implement card 0f34e285 in only the mounted /work checkout.
The frozen policy decision is that DIRECT is not exempt from the work grade's
sensitivity ceiling: reuse the existing inherited _dispatch_model and canonical
attach_dispatch_model seam on the direct TaskBrief. Do not add a grader, merge,
automerge, network call, alternate bucket vocabulary, or fallback. Preserve the
ungated human-review-only DIRECT lifecycle. Add the focused negative controls to
the existing toggle tests and document the decision once. Run only the predefined
focused tests. Do not commit or push; the trusted controller owns provisional Git
state. If the card is already satisfied, emit STATUS: BLOCKED with exact paths."""

M_SCOUT_PROMPT = """Read only the mounted /work checkout for card 5b88d88c.
Using targeted commands, prove whether the immutable RunRecord schema, process
session identity, provider-owned served-model observation, and existing atomic
autopilot journal are all present at this exact commit. Also determine whether the
high-level card has been superseded by a narrower writer card. Do not inspect the
host, environment, secrets, or network. Missing or ambiguous prerequisites require
SCOUT_ASSESSMENT: BLOCKED. Otherwise return ACTIONABLE with concrete path findings."""

M_BUILD_PROMPT = """Implement card 5b88d88c only from the typed predecessor scout
evidence. Add the first RunRecord writer to the existing atomic run journal; do not
create another store. Validate before persistence, make duplicate delivery of the
same (run_id, card_id, round, content hash) idempotent, reject conflicting content,
and leave absent firsthand attribution absent. Wire only an execution boundary that
already owns every required observation. A writer failure must be observable and
must not invent or overwrite a record. Add focused tests in
tests/test_autocode_run_record_writer.py and preserve the accepted store-boundary
ADR. Do not modify lifecycle storage, backfill history, push, or mutate the board.
Do not commit; the trusted controller owns provisional Git state."""

M_TEST_PROMPT = """Independently inspect the exact builder commit for card 5b88d88c.
Run only the predefined RunRecord, journal, writer, and orchestrator tests with
bytecode/cache writes disabled. Inspect the diff for a fourth store, projection-
derived facts, overwrite-on-conflict, or worker-authored weakening of existing tests.
Do not edit, commit, use the network, or inspect host secrets. Emit STATUS: FAILED
for any missing criterion; exit zero is not completion authority."""

L_SCOUT_SOURCE_PROMPT = """Read only the mounted skharness checkout for card
41077231. Locate exact hostd HTTP and WebSocket bearer/scope enforcement and identify
which required SKWorld and SKChat source is absent from this single-repository mount.
Never inspect the host, environment, secrets, or network; never print a token. This
qualification deliberately has no L-card builder. Emit SCOUT_ASSESSMENT: BLOCKED
unless this checkout alone contains byte-for-byte evidence for every repository and
the live transport. Similar docs or old evidence are not substitutes."""

L_SCOUT_EVIDENCE_PROMPT = """Read only the mounted skharness checkout for card
41077231. Independently seek immutable evidence binding the exact SKHarness,
SKWorld-app, and SKChat commits plus direct and proxied WebSocket request bytes from
one freshly minted non-secret test token. Do not use the network, inspect the host or
environment, or reveal token material. This qualification deliberately has no L-card
builder. If any repository, commit, or fresh byte evidence is unavailable inside
/work, stop and emit SCOUT_ASSESSMENT: BLOCKED with concrete missing inputs."""


CANDIDATES = {
    "0f34e285": Candidate(
        card_id="0f34e285",
        size=CardSize.SMALL,
        card_hash="sha256:e6a2971747260c4089f84b4b0cd5d9540321a7004f1ff8b717f175c05dc445d6",
        suitability="safe_single_builder_provisional",
        phases=(("phase-build", SwarmRole.BUILDER, ("0f34e285-builder",), ()),),
        workers=(
            WorkerTemplate(
                "0f34e285-builder", "phase-build", SwarmRole.BUILDER, S_PROMPT,
                (
                    ".git", "src/skharness/autocode/direct.py",
                    "src/skharness/autocode/engineering.py",
                    "src/skharness/autocode/buckets.py", "tests/test_autocode_toggle.py",
                    "tests/test_autocode_buckets.py", "tests/test_routing_guard.py",
                    "docs/architecture/continual-harness.md", "SOP.md",
                ),
                (
                    "src/skharness/autocode/direct.py", "tests/test_autocode_toggle.py",
                    "docs/architecture/continual-harness.md", "SOP.md",
                ),
                ("src/skharness/autocode/engineering.py", "src/skharness/autocode/buckets.py"),
                BUILD_TOOLS, 540, 360, 180, 65_536, 24,
                PhaseBudget(20, 50, 190, 100),
            ),
        ),
        allowed_changes=frozenset(
            {
                "src/skharness/autocode/direct.py", "tests/test_autocode_toggle.py",
                "docs/architecture/continual-harness.md", "SOP.md",
            }
        ),
        required_changes=frozenset(
            {"src/skharness/autocode/direct.py", "tests/test_autocode_toggle.py"}
        ),
        controller_tests=(
            "tests/test_autocode_toggle.py", "tests/test_autocode_buckets.py",
            "tests/test_routing_guard.py",
        ),
        max_concurrency=1,
    ),
    "5b88d88c": Candidate(
        card_id="5b88d88c",
        size=CardSize.MEDIUM,
        card_hash="sha256:cecffcaa49b7b22e84d7425049fa3e00d80bee9648ebe1f1f2d94320a7287b26",
        suitability="conditional_on_actionable_prerequisite_scout",
        phases=(
            ("phase-scout", SwarmRole.SCOUT, ("5b88d88c-scout",), ()),
            ("phase-build", SwarmRole.BUILDER, ("5b88d88c-builder",), ("phase-scout",)),
            ("phase-test", SwarmRole.TESTER, ("5b88d88c-tester",), ("phase-build",)),
        ),
        workers=(
            WorkerTemplate(
                "5b88d88c-scout", "phase-scout", SwarmRole.SCOUT, M_SCOUT_PROMPT,
                COMMON_READ, (), (), READ_TOOLS, 180, 180, 0, 32_768, 16,
                PhaseBudget(20, 154, 5, 1),
            ),
            WorkerTemplate(
                "5b88d88c-builder", "phase-build", SwarmRole.BUILDER, M_BUILD_PROMPT,
                (
                    ".git", "src/skharness/autocode", "tests", "docs", "pyproject.toml",
                    "SOP.md", "CHANGELOG.md",
                ),
                ("src/skharness/autocode", "tests", "docs", "SOP.md", "CHANGELOG.md"),
                (), BUILD_TOOLS, 480, 300, 180, 98_304, 40,
                PhaseBudget(20, 55, 185, 40),
            ),
            WorkerTemplate(
                "5b88d88c-tester", "phase-test", SwarmRole.TESTER, M_TEST_PROMPT,
                COMMON_READ + ("CHANGELOG.md",), (), (), READ_TOOLS,
                240, 240, 0, 32_768, 20,
                PhaseBudget(20, 40, 1, 179),
            ),
        ),
        allowed_changes=frozenset(
            {
                "src/skharness/autocode/journal.py",
                "src/skharness/autocode/orchestrator.py",
                "src/skharness/autocode/engineering.py",
                "src/skharness/autocode/direct.py",
                "src/skharness/autocode/run_record.py",
                "src/skharness/autocode/run_record_writer.py",
                "tests/test_autocode_run_record_writer.py",
                "docs/architecture/run-record-store-boundary.md",
                "SOP.md", "CHANGELOG.md",
            }
        ),
        required_changes=frozenset(
            {"src/skharness/autocode/journal.py", "tests/test_autocode_run_record_writer.py"}
        ),
        controller_tests=(
            "tests/test_autocode_run_record.py", "tests/test_autopilot_journal_api.py",
            "tests/test_autopilot_orchestrator.py", "tests/test_autocode_run_record_writer.py",
        ),
        max_concurrency=1,
    ),
    "41077231": Candidate(
        card_id="41077231",
        size=CardSize.LARGE,
        card_hash="sha256:728d2c3af5cd438b1ee6780591f085ce6e0ef03c9ddac89b135d629ed80bb2df",
        suitability="read_only_fail_closed_scout_only",
        phases=(
            (
                "phase-scout", SwarmRole.SCOUT,
                ("41077231-scout-source", "41077231-scout-evidence"), (),
            ),
        ),
        workers=(
            WorkerTemplate(
                "41077231-scout-source", "phase-scout", SwarmRole.SCOUT,
                L_SCOUT_SOURCE_PROMPT, COMMON_READ, (), (), READ_TOOLS,
                150, 150, 0, 32_768, 12,
                PhaseBudget(20, 124, 5, 1),
            ),
            WorkerTemplate(
                "41077231-scout-evidence", "phase-scout", SwarmRole.SCOUT,
                L_SCOUT_EVIDENCE_PROMPT, COMMON_READ, (), (), READ_TOOLS,
                150, 150, 0, 32_768, 12,
                PhaseBudget(20, 124, 5, 1),
            ),
        ),
        allowed_changes=frozenset(), required_changes=frozenset(), controller_tests=(),
        max_concurrency=2,
    ),
}

# Remediation cards are deliberately explicit rather than accepting arbitrary
# CardStore prompts.  A card must have a reviewed topology, path allowlist,
# budget, and immutable content hash before a worker can be admitted.
REMEDIATION_CANDIDATES = {
    "c278b5c0": Candidate(
        card_id="c278b5c0", size=CardSize.SMALL,
        card_hash="sha256:37cc7a375db5e6ba63c7593ffe13d37ab540d97594ece8bf47046016a753548f",
        suitability="safe_single_builder_provisional",
        phases=(("phase-build", SwarmRole.BUILDER, ("c278b5c0-builder",), ()),),
        workers=(WorkerTemplate(
            "c278b5c0-builder", "phase-build", SwarmRole.BUILDER,
            """Implement card c278b5c0 in the mounted /work checkout. Make cleanup
            idempotent only for exact already-absent managed containers, proxies, or
            networks; preserve real cleanup failures and original worker errors.
            Add focused supervisor and qualifier regression tests. Do not broaden
            resource matching, inspect secrets or network, commit, push, or mutate
            the board. If already satisfied emit STATUS: BLOCKED with paths.""",
            (".git", "src/skharness", "tests", "docs", "pyproject.toml", "SOP.md"),
            ("src/skharness", "tests", "docs", "SOP.md"), (), BUILD_TOOLS,
            360, 240, 120, 65_536, 24, PhaseBudget(20, 50, 130, 40),
        ),),
        allowed_changes=frozenset({"src/skharness", "tests", "docs", "SOP.md"}),
        required_changes=frozenset({"src/skharness", "tests"}),
        controller_tests=("tests/test_sandbox_spawn.py", "tests/test_qualify_pi_swarm_script.py"),
        max_concurrency=1,
    ),
    "400bf174": Candidate(
        card_id="400bf174", size=CardSize.MEDIUM,
        card_hash="sha256:236fe4ffa7dc2b5d45f522646307e2d493416190f65ca01aea71e2c1e0f3a3f7",
        suitability="conditional_on_actionable_prerequisite_scout",
        phases=(
            ("phase-scout", SwarmRole.SCOUT, ("400bf174-scout",), ()),
            ("phase-build", SwarmRole.BUILDER, ("400bf174-builder",), ("phase-scout",)),
            ("phase-test", SwarmRole.TESTER, ("400bf174-tester",), ("phase-build",)),
        ),
        workers=(
            WorkerTemplate(
                "400bf174-scout", "phase-scout", SwarmRole.SCOUT,
                """Read the mounted checkout and identify the existing inspection budget,
                command-aware path parser, and activity/A2A telemetry seams. Return
                ACTIONABLE only with exact paths and safe compatibility constraints;
                otherwise SCOUT_ASSESSMENT: BLOCKED. Do not edit or use network.""",
                COMMON_READ + ("src/skharness/arena",), (), (), READ_TOOLS,
                180, 180, 0, 32_768, 16, PhaseBudget(20, 154, 5, 1),
            ),
            WorkerTemplate(
                "400bf174-builder", "phase-build", SwarmRole.BUILDER,
                """Implement card 400bf174 only from the typed scout evidence. Make
                S/M/L inspection budgets explicit and expose remaining calls, denial
                reason, stream completeness, and terminal status in trusted activity
                records. Preserve fail-closed admission and path isolation. Add tests;
                do not commit, push, or mutate the board.""",
                (".git", "src/skharness", "tests", "docs", "pyproject.toml", "SOP.md"),
                ("src/skharness", "tests", "docs", "SOP.md"), (), BUILD_TOOLS,
                480, 300, 180, 98_304, 40, PhaseBudget(20, 55, 185, 40),
            ),
            WorkerTemplate(
                "400bf174-tester", "phase-test", SwarmRole.TESTER,
                """Independently test the exact builder work for card 400bf174. Verify
                budget scaling, telemetry completeness, malformed/truncated output
                blocking, and cleanup. Do not edit or claim completion.""",
                COMMON_READ + ("src/skharness/arena",), (), (), READ_TOOLS,
                240, 240, 0, 32_768, 20, PhaseBudget(20, 40, 1, 179),
            ),
        ),
        allowed_changes=frozenset({"src/skharness", "tests", "docs", "SOP.md"}),
        required_changes=frozenset({"src/skharness", "tests"}),
        controller_tests=("tests/test_qualify_pi_swarm_script.py", "tests/test_swarm_orchestrator.py"),
        max_concurrency=1,
    ),
}


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout_s: float | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout_s,
    )


def require_image_digest(image: str | None) -> str:
    if image != QUALIFIED_IMAGE:
        raise ValueError("worker image must equal the qualified v0.3.38 pi-python-test digest")
    return image


def require_controller_commit(commit: str | None) -> str:
    if not commit or COMMIT_RE.fullmatch(commit) is None:
        raise ValueError("controller commit must be an exact lowercase 40-character Git SHA")
    return commit


def normalized_card(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": card.get("id"),
        "title": card.get("title"),
        "description": card.get("description"),
        "acceptance_criteria": card.get("acceptance_criteria") or [],
        "dependencies": card.get("dependencies") or [],
    }


def candidate_catalog(card_ids: tuple[str, ...] | None = None) -> dict[str, Candidate]:
    """Return only reviewed topologies; arbitrary CardStore prompts are refused."""
    if not card_ids:
        return CANDIDATES
    unknown = set(card_ids) - set(REMEDIATION_CANDIDATES)
    if unknown:
        raise ValueError(
            "card lacks a reviewed qualification topology: " + ", ".join(sorted(unknown))
        )
    return {card_id: REMEDIATION_CANDIDATES[card_id] for card_id in card_ids}


def load_card_snapshots(
    card_root: Path, card_ids: tuple[str, ...] | None = None,
) -> dict[str, dict[str, Any]]:
    """Load immutable card content from canonical CardStore core snapshots.

    The folded kanban JSON intentionally omits acceptance criteria and is therefore
    unsuitable for content hashing. Lifecycle metadata may change independently;
    the frozen qualification binds only these canonical content fields.
    """
    resolved_root = card_root.resolve(strict=True)
    found: dict[str, dict[str, Any]] = {}
    candidates = candidate_catalog(card_ids)
    for card_id in candidates:
        core_path = (resolved_root / card_id / "core.json").resolve(strict=True)
        if core_path.parent.parent != resolved_root or core_path.parent.name != card_id:
            raise RuntimeError(f"canonical card path escaped card root for {card_id}")
        card = json.loads(core_path.read_text(encoding="utf-8"))
        if card.get("id") != card_id:
            raise RuntimeError(f"canonical card ID mismatch for {card_id}")
        found[card_id] = normalized_card(card)
    for card_id, snapshot in found.items():
        observed = canonical_digest(snapshot)
        expected = candidates[card_id].card_hash
        if observed != expected:
            raise RuntimeError(f"card content drift for {card_id}: {observed} != {expected}")
    return found


def git(repo: Path, *args: str, check: bool = True) -> str:
    return run(["git", "-C", str(repo), *args], check=check).stdout.strip()


def validate_source_repo(source: Path, expected_commit: str | None = None) -> str:
    if not source.is_dir():
        raise RuntimeError(f"source repository missing: {source}")
    if git(source, "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("source repository must be clean before worktree creation")
    git(source, "cat-file", "-e", f"{WORKER_BASE_COMMIT}^{{commit}}")
    observed = git(source, "rev-parse", "HEAD")
    if expected_commit is not None and observed != require_controller_commit(expected_commit):
        raise RuntimeError(
            f"controller source drift: {observed} != {expected_commit}"
        )
    return observed


def _require_tracked_file(source: Path, path: Path, commit: str) -> str:
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(source.resolve(strict=True)).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"controller file is outside the pinned source: {resolved}") from exc
    expected_blob = git(source, "rev-parse", f"{commit}:{relative}")
    observed_blob = git(source, "hash-object", str(resolved))
    if observed_blob != expected_blob:
        raise RuntimeError(f"controller file does not match {commit}: {relative}")
    return relative


def validate_release_evidence(source: Path, controller_commit: str) -> dict[str, Any]:
    path = source / RELEASE_EVIDENCE_PATH
    _require_tracked_file(source, path, controller_commit)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload != RELEASE_EVIDENCE:
        raise RuntimeError("checked-in v0.3.38 release evidence does not match the frozen record")
    tag_commit = git(source, "rev-list", "-n", "1", RELEASE_EVIDENCE["tag"])
    if tag_commit != WORKER_BASE_COMMIT:
        raise RuntimeError("v0.3.38 tag does not peel to the frozen worker source commit")
    return {
        "path": RELEASE_EVIDENCE_PATH.as_posix(),
        "content_hash": canonical_digest(payload),
        "record": payload,
    }


def validate_controller_source(source: Path, expected_commit: str) -> dict[str, Any]:
    source = source.resolve(strict=True)
    commit = validate_source_repo(source, expected_commit)
    ancestry = run(
        ["git", "-C", str(source), "merge-base", "--is-ancestor", WORKER_BASE_COMMIT, commit],
        check=False,
    )
    if ancestry.returncode != 0:
        raise RuntimeError("controller commit does not descend from the frozen worker source")
    script_relative = _require_tracked_file(source, Path(__file__), commit)
    source_root = (source / "src").resolve(strict=True)
    module_paths: dict[str, str] = {}
    for name in CONTROLLER_MODULES:
        module = sys.modules.get(name)
        module_file = getattr(module, "__file__", None)
        if not isinstance(module_file, str):
            raise RuntimeError(f"controller module has no source path: {name}")
        resolved = Path(module_file).resolve(strict=True)
        try:
            resolved.relative_to(source_root)
        except ValueError as exc:
            raise RuntimeError(
                f"controller module was imported outside the pinned checkout: {name}"
            ) from exc
        _require_tracked_file(source, resolved, commit)
        module_paths[name] = str(resolved)
    return {
        "commit": commit,
        "source_root": str(source),
        "script_path": script_relative,
        "module_paths": module_paths,
        "release_evidence": validate_release_evidence(source, commit),
    }


def validate_local_image(image: str) -> dict[str, Any]:
    image = require_image_digest(image)
    payload = json.loads(run(["docker", "image", "inspect", image]).stdout)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RuntimeError("Docker returned an invalid image inspection record")
    record = payload[0]
    repo_digests = record.get("RepoDigests")
    if not isinstance(repo_digests, list) or image not in repo_digests:
        raise RuntimeError("local image does not expose the exact qualified RepoDigest")
    labels = ((record.get("Config") or {}).get("Labels") or {})
    expected_labels = {
        "org.opencontainers.image.version": "0.3.38",
        "org.opencontainers.image.ref.name": "v0.3.38",
        "org.opencontainers.image.revision": WORKER_BASE_COMMIT,
        "io.skharness.image.build-mode": "release",
    }
    if not isinstance(labels, dict) or any(
        labels.get(key) != value for key, value in expected_labels.items()
    ):
        raise RuntimeError("local image OCI provenance labels do not match v0.3.38")
    return {
        "image_id": record.get("Id"),
        "repo_digest": image,
        "labels": expected_labels,
    }


def _managed_ids(argv: list[str]) -> list[str]:
    result = run(argv, timeout_s=QUALIFIER_DOCKER_INVENTORY_TIMEOUT_S)
    ids = [item.strip() for item in result.stdout.splitlines() if item.strip()]
    if len(ids) > 512:
        raise RuntimeError("managed Docker inventory exceeds the bounded qualifier limit")
    if any(any(char.isspace() for char in item) for item in ids):
        raise RuntimeError("managed Docker inventory contains a malformed resource ID")
    return ids


def managed_docker_inventory(docker: str = "docker") -> dict[str, Any]:
    """Return bounded label evidence for all SKHarness-managed Docker resources."""
    containers = _managed_ids(
        [
            docker, "ps", "-a", "--filter", f"label={MANAGED_LABEL}=true",
            "--format", "{{.ID}}",
        ]
    )
    networks = _managed_ids(
        [
            docker, "network", "ls", "--filter", f"label={MANAGED_LABEL}=true",
            "--format", "{{.ID}}",
        ]
    )
    records: list[dict[str, Any]] = []
    if containers:
        payload = json.loads(
            run(
                [docker, "inspect", *containers],
                timeout_s=QUALIFIER_DOCKER_INVENTORY_TIMEOUT_S,
            ).stdout
        )
        if not isinstance(payload, list) or len(payload) != len(containers):
            raise RuntimeError("managed container inspection is incomplete")
        for item in payload:
            if not isinstance(item, dict):
                raise RuntimeError("managed container inspection contains a non-object")
            labels = ((item.get("Config") or {}).get("Labels") or {})
            records.append(_managed_resource_record(item, labels, "container"))
    if networks:
        payload = json.loads(
            run(
                [docker, "network", "inspect", *networks],
                timeout_s=QUALIFIER_DOCKER_INVENTORY_TIMEOUT_S,
            ).stdout
        )
        if not isinstance(payload, list) or len(payload) != len(networks):
            raise RuntimeError("managed network inspection is incomplete")
        for item in payload:
            if not isinstance(item, dict):
                raise RuntimeError("managed network inspection contains a non-object")
            records.append(_managed_resource_record(item, item.get("Labels") or {}, "network"))
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "managed_label": f"{MANAGED_LABEL}=true",
        "resources": records,
        "resource_count": len(records),
    }


def _managed_resource_record(
    item: dict[str, Any], labels: object, resource_type: str
) -> dict[str, Any]:
    if not isinstance(labels, dict) or labels.get(MANAGED_LABEL) != "true":
        raise RuntimeError(f"managed {resource_type} lacks the required ownership label")
    selected = {
        key: labels.get(key)
        for key in (
            MANAGED_LABEL, RUN_ID_LABEL, RESOURCE_ROLE_LABEL,
            OWNERSHIP_AUTHORITY_LABEL, SCHEMA_LABEL,
        )
    }
    if selected[SCHEMA_LABEL] != LIFECYCLE_SCHEMA:
        raise RuntimeError(f"managed {resource_type} has an unknown lifecycle schema")
    if not all(isinstance(value, str) and value for value in selected.values()):
        raise RuntimeError(f"managed {resource_type} has incomplete ownership labels")
    return {
        "id": str(item.get("Id") or "")[:64],
        "name": str(item.get("Name") or "")[:128],
        "type": resource_type,
        "running": (
            bool((item.get("State") or {}).get("Running"))
            if resource_type == "container"
            else None
        ),
        "labels": selected,
    }


def require_no_managed_resources(
    inventory: dict[str, Any], *, run_ids: set[str] | frozenset[str] | None = None,
    run_id_prefixes: tuple[str, ...] = (), exact_ids: set[str] | frozenset[str] | None = None,
    ignore_foreign: bool = False,
) -> None:
    """Reject only resources belonging to this run; foreign valid resources are inert.

    Inventory parsing remains strict for every managed object.  Filtering happens
    only after ownership labels are validated, and duplicate IDs with conflicting
    ownership are always ambiguous and therefore fail closed.
    """
    resources = inventory.get("resources")
    if not isinstance(resources, list):
        raise RuntimeError("managed Docker inventory is malformed")
    seen: dict[str, tuple[str, str]] = {}
    for item in resources:
        if not isinstance(item, dict) or not isinstance(item.get("labels"), dict):
            raise RuntimeError("managed Docker inventory contains ambiguous ownership")
        resource_id = str(item.get("id") or "")
        labels = item["labels"]
        ownership = (str(labels.get(RUN_ID_LABEL) or ""), str(labels.get(RESOURCE_ROLE_LABEL) or ""))
        prior = seen.get(resource_id)
        if prior is not None and prior != ownership:
            raise RuntimeError(f"managed Docker resource has ambiguous ownership: {resource_id[:12]}")
        seen[resource_id] = ownership
    if ignore_foreign or run_ids is not None or run_id_prefixes or exact_ids is not None:
        selected_ids = exact_ids or frozenset()
        selected_runs = run_ids or frozenset()
        relevant = [
            item for item in resources
            if str(item.get("id") or "") in selected_ids
            or str((item.get("labels") or {}).get(RUN_ID_LABEL) or "") in selected_runs
            or any(
                str((item.get("labels") or {}).get(RUN_ID_LABEL) or "").startswith(prefix)
                for prefix in run_id_prefixes
            )
        ]
    else:
        relevant = resources
    if relevant:
        identities = ", ".join(
            f"{item.get('type')}:{str(item.get('id'))[:12]}" for item in relevant[:8]
        )
        raise RuntimeError(f"managed Docker resources remain after cleanup: {identities}")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def write_bundle_digest(root: Path) -> dict[str, Any]:
    destination = root / "qualification-bundle-digest.json"
    files = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": _file_sha256(path),
            "size": path.stat().st_size,
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path != destination
    ]
    payload = {
        "schema": "skharness.qualification-bundle-digest.v1",
        "files": files,
        "bundle_digest": canonical_digest(files),
    }
    destination.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


def validate_worktree(worktree: Path) -> None:
    if git(worktree, "rev-parse", "HEAD") != WORKER_BASE_COMMIT:
        raise RuntimeError(f"worktree {worktree} is not pinned to {WORKER_BASE_COMMIT}")
    if git(worktree, "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError(f"worktree {worktree} is not clean")
    dotgit = worktree / ".git"
    if not dotgit.is_file() or not dotgit.read_text(encoding="utf-8").startswith("gitdir: "):
        raise RuntimeError(f"worktree {worktree} is not an isolated linked worktree")


def create_worktree(source: Path, target: Path) -> None:
    if target.exists():
        raise RuntimeError(f"refusing existing worktree target: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "-C", str(source), "worktree", "add", "--detach", str(target), WORKER_BASE_COMMIT])
    validate_worktree(target)


def _stop_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Synchronously drain a bounded host process after cancellation/timeout."""
    process.terminate()
    try:
        return process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate()


def run_controller_command(
    argv: list[str],
    *,
    cancellation: CancellationToken,
    timeout_s: float,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded host command with cooperative cancellation and no orphan."""
    cancellation.raise_if_cancelled()
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + timeout_s
    while True:
        if cancellation.cancelled:
            stdout, stderr = _stop_process(process)
            raise WorkerCancellationError(
                "controller command cancelled: " + " ".join(argv[:3])
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stdout, stderr = _stop_process(process)
            raise RuntimeError(
                f"controller command timed out after {timeout_s}s: "
                + " ".join(argv[:3])
                + f"; stdout={stdout[-256:]!r}; stderr={stderr[-256:]!r}"
            )
        try:
            stdout, stderr = process.communicate(timeout=min(0.1, remaining))
            break
        except subprocess.TimeoutExpired:
            continue
    cancellation.raise_if_cancelled()
    result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    if check and result.returncode:
        raise subprocess.CalledProcessError(
            result.returncode, argv, output=stdout, stderr=stderr
        )
    return result


def controller_git(
    worktree: Path,
    cancellation: CancellationToken,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run bounded hookless and signing-disabled Git for provisional controller work."""
    return run_controller_command(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(worktree),
            *args,
        ],
        cancellation=cancellation,
        timeout_s=CONTROLLER_GIT_TIMEOUT_S,
        check=check,
    )


def changed_paths(
    worktree: Path, cancellation: CancellationToken
) -> frozenset[str]:
    paths = set()
    status = controller_git(
        worktree,
        cancellation,
        "status",
        "--porcelain",
        "--untracked-files=all",
    ).stdout
    for line in status.splitlines():
        path = line[3:]
        if " -> " in path or not path:
            raise RuntimeError("renames and malformed paths are outside the qualification scope")
        paths.add(path)
    return frozenset(paths)


def require_scoped_changes(candidate: Candidate, paths: frozenset[str]) -> None:
    if not paths:
        raise RuntimeError("builder returned success without a worktree change")
    outside = paths - candidate.allowed_changes
    missing = candidate.required_changes - paths
    if outside:
        raise RuntimeError("builder changed out-of-scope paths: " + ", ".join(sorted(outside)))
    if missing:
        raise RuntimeError("builder omitted required paths: " + ", ".join(sorted(missing)))


def _controller_validation_identity(run_id: str, kind: str) -> tuple[str, SandboxOwnership]:
    digest = hashlib.sha256(f"{run_id}:{kind}".encode()).hexdigest()[:24]
    ownership = SandboxOwnership(f"qual-{digest}")
    return f"skharness-qual-{kind}-{digest}", ownership


def image_test_argv(
    image: str,
    worktree: Path,
    candidate: Candidate,
    *,
    validation_run_id: str = "preflight-test",
) -> list[str]:
    command = [
        "python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
        *candidate.controller_tests,
    ]
    name, ownership = _controller_validation_identity(validation_run_id, "pytest")
    return [
        "docker", "run", "--name", name, *ownership.docker_args("worker"),
        "--network", "none", "--read-only",
        "--tmpfs", "/tmp:mode=1777", "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL", "--pids-limit", "512", "--cpus", "2",
        "--memory", "4g", "--memory-swap", "4g",
        "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONPATH=/work/src",
        "--mount", f"type=bind,src={worktree.resolve()},dst=/work,readonly",
        "--workdir", "/work", image, *command,
    ]


def image_ruff_argv(
    image: str,
    worktree: Path,
    lint_paths: list[str],
    *,
    validation_run_id: str,
) -> list[str]:
    name, ownership = _controller_validation_identity(validation_run_id, "ruff")
    return [
        "docker", "run", "--name", name, *ownership.docker_args("worker"),
        "--network", "none", "--read-only", "--tmpfs", "/tmp:mode=1777",
        "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
        "--pids-limit", "256", "--cpus", "1", "--memory", "2g",
        "--memory-swap", "2g", "--mount",
        f"type=bind,src={worktree.resolve()},dst=/work,readonly",
        "--workdir", "/work", image, "python", "-m", "ruff", "check", *lint_paths,
    ]


def run_controller_validation(
    argv: list[str],
    *,
    name: str,
    timeout_s: int,
    log_path: Path,
    cancellation: CancellationToken,
    cleanup_timeout_s: int = CONTROLLER_CLEANUP_TIMEOUT_S,
) -> None:
    """Run one bounded labeled validator and prove its exact container removal."""
    primary_error: str | None = None
    cancelled = False
    process: subprocess.Popen[str] | None = None
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cancellation.raise_if_cancelled()
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, text=True)
            deadline = time.monotonic() + timeout_s
            while process.poll() is None:
                if cancellation.wait(min(0.1, max(0.0, deadline - time.monotonic()))):
                    cancelled = True
                    primary_error = "validation cancelled by controller"
                    break
                if time.monotonic() >= deadline:
                    primary_error = f"validation timed out after {timeout_s}s"
                    break
            if primary_error is None and process.returncode:
                primary_error = f"validation exited {process.returncode}"
    except WorkerCancellationError:
        cancelled = True
        primary_error = "validation cancelled by controller before launch"
    except Exception as exc:  # noqa: BLE001 - cleanup must still run
        primary_error = f"validation launch failed: {type(exc).__name__}: {exc}"
    cleanup_error: str | None = None
    try:
        cleanup = subprocess.run(
            [argv[0], "rm", "-f", name],
            capture_output=True,
            text=True,
            timeout=cleanup_timeout_s,
            check=False,
        )
        detail = (cleanup.stderr or cleanup.stdout or "").casefold()
        if cleanup.returncode != 0 and not any(
            marker in detail for marker in ("no such container", "not found")
        ):
            cleanup_error = f"validator cleanup exited {cleanup.returncode}"
    except subprocess.TimeoutExpired:
        cleanup_error = f"validator cleanup timed out after {cleanup_timeout_s}s"
    except Exception as exc:  # noqa: BLE001 - report after the attempt
        cleanup_error = f"validator cleanup failed: {type(exc).__name__}: {exc}"
    if process is not None and process.poll() is None:
        try:
            process.wait(timeout=cleanup_timeout_s)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            cleanup_error = cleanup_error or "validator client did not drain after cleanup"
    with log_path.open("a", encoding="utf-8") as log:
        if primary_error:
            log.write(f"\ncontroller_error: {primary_error}\n")
        if cleanup_error:
            log.write(f"cleanup_error: {cleanup_error}\n")
    errors = [item for item in (primary_error, cleanup_error) if item]
    if errors:
        if cancelled and cleanup_error is None:
            raise WorkerCancellationError(primary_error)
        raise RuntimeError("; ".join(errors))


def controller_commit(
    worktree: Path,
    candidate: Candidate,
    image: str,
    evidence_root: Path,
    *,
    validation_run_id: str,
    cancellation: CancellationToken,
) -> str:
    """Validate and commit a worker diff from the trusted host controller."""
    cancellation.raise_if_cancelled()
    paths = changed_paths(worktree, cancellation)
    require_scoped_changes(candidate, paths)
    diff_check = controller_git(
        worktree, cancellation, "diff", "--check", check=False
    )
    cancellation.raise_if_cancelled()
    (evidence_root / "diff-check.log").write_text(
        diff_check.stdout + diff_check.stderr, encoding="utf-8"
    )
    cancellation.raise_if_cancelled()
    if diff_check.returncode:
        raise RuntimeError("git diff --check failed")
    test_argv = image_test_argv(
        image, worktree, candidate, validation_run_id=validation_run_id
    )
    test_name = test_argv[test_argv.index("--name") + 1]
    run_controller_validation(
        test_argv,
        name=test_name,
        timeout_s=CONTROLLER_TEST_TIMEOUT_S,
        log_path=evidence_root / "controller-tests.log",
        cancellation=cancellation,
    )
    lint_paths = sorted(path for path in paths if path.endswith(".py"))
    if lint_paths:
        lint_argv = image_ruff_argv(
            image, worktree, lint_paths, validation_run_id=validation_run_id
        )
        lint_name = lint_argv[lint_argv.index("--name") + 1]
        run_controller_validation(
            lint_argv,
            name=lint_name,
            timeout_s=CONTROLLER_RUFF_TIMEOUT_S,
            log_path=evidence_root / "controller-ruff.log",
            cancellation=cancellation,
        )
    cancellation.raise_if_cancelled()
    controller_git(worktree, cancellation, "add", "--", *sorted(paths))
    cancellation.raise_if_cancelled()
    controller_git(
        worktree,
        cancellation,
        "-c",
        "user.name=SKHarness Controller",
        "-c",
        "user.email=skharness-controller@localhost",
        "commit",
        "--no-verify",
        "--no-gpg-sign",
        "-m",
        f"harness({candidate.card_id}): provisional Pi swarm output",
    )
    cancellation.raise_if_cancelled()
    if changed_paths(worktree, cancellation):
        raise RuntimeError("controller commit did not leave a clean worktree")
    head = controller_git(worktree, cancellation, "rev-parse", "HEAD").stdout.strip()
    cancellation.raise_if_cancelled()
    return head


class FixedBudgetPiExperimentRunner(PiExperimentRunner):
    """Pass the same frozen phase budget to prompt and runtime enforcement."""

    def __init__(self, *args, phase_budget: PhaseBudget, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.phase_budget = phase_budget

    def execute(self, request, spec, **kwargs):
        return super().execute(request, spec, phase_budget=self.phase_budget, **kwargs)


def make_plan_and_contracts(
    candidate: Candidate, identity: SwarmIdentity, now: datetime, worktree: Path
) -> tuple[SwarmPlan, tuple[SubagentContract, ...]]:
    plan = SwarmPlan(
        plan_id=f"plan-{identity.trajectory_id}",
        identity=identity,
        phases=tuple(
            SwarmPhaseSpec(
                phase_id=phase_id, role=role, contract_ids=contract_ids,
                predecessor_phase_ids=predecessors,
            )
            for phase_id, role, contract_ids, predecessors in candidate.phases
        ),
        created_at=now,
    )
    contracts = tuple(
        SubagentContract(
            contract_id=worker.contract_id,
            team_id=f"team-{identity.trajectory_id}",
            identity=identity,
            plan_hash=plan.content_hash,
            phase_id=worker.phase_id,
            parent_agent_id="lumina-orchestrator",
            child_agent_id=f"pi-{worker.contract_id}",
            role=worker.role,
            task=worker.task,
            readable_paths=worker.readable_paths,
            writable_paths=worker.writable_paths,
            protected_paths=worker.protected_paths,
            tool_allowlist=worker.tools,
            budget=ExecutionBudget(
                wall_seconds=worker.wall_seconds, token_limit=worker.token_limit,
                tool_call_limit=worker.tool_limit, cost_limit=0,
            ),
            lease_id=f"lease-{identity.trajectory_id}-{worker.contract_id}",
            worktree_id=worktree.name,
            issued_at=now,
        )
        for worker in candidate.workers
    )
    return plan, contracts


def candidate_manifest(candidate: Candidate, snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "card_id": candidate.card_id,
        "size": candidate.size.value,
        "card_hash": candidate.card_hash,
        "card_snapshot": snapshot,
        "suitability": candidate.suitability,
        "phases": [
            {
                "phase_id": phase_id, "role": role.value,
                "contract_ids": contract_ids, "predecessor_phase_ids": predecessors,
            }
            for phase_id, role, contract_ids, predecessors in candidate.phases
        ],
        "workers": [
            {
                "contract_id": item.contract_id, "role": item.role.value,
                "task": item.task, "readable_paths": item.readable_paths,
                "writable_paths": item.writable_paths, "protected_paths": item.protected_paths,
                "tool_allowlist": item.tools,
                "budget": {
                    "wall_seconds": item.wall_seconds, "token_limit": item.token_limit,
                    "tool_call_limit": item.tool_limit, "cost_limit": 0,
                    "pi_wall_seconds": item.pi_wall_seconds,
                    "controller_reserve_seconds": item.controller_reserve_seconds,
                    "phase_seconds": {
                        "assess": item.phase_budget.assess_s,
                        "inspect": item.phase_budget.inspect_s,
                        "build": item.phase_budget.build_s,
                        "test": item.phase_budget.test_s,
                    },
                },
            }
            for item in candidate.workers
        ],
        "allowed_changes": sorted(candidate.allowed_changes),
        "required_changes": sorted(candidate.required_changes),
        "controller_tests": candidate.controller_tests,
    }


def build_manifest(
    image: str,
    snapshots: dict[str, dict[str, Any]],
    controller_provenance: dict[str, Any],
    candidates: dict[str, Candidate] | None = None,
) -> dict[str, Any]:
    require_image_digest(image)
    candidates = candidates or CANDIDATES
    return {
        "schema": SCHEMA,
        "mode": "preflight_only",
        "worker_base_commit": WORKER_BASE_COMMIT,
        "image": image,
        "release_evidence": controller_provenance["release_evidence"],
        "controller": controller_provenance,
        "gateway": GATEWAY,
        "requested_model": MODEL,
        "host_required": QUALIFIED_HOST,
        "controller_post_run_bounds": {
            "pytest_seconds": CONTROLLER_TEST_TIMEOUT_S,
            "pytest_cleanup_seconds": CONTROLLER_CLEANUP_TIMEOUT_S,
            "ruff_seconds": CONTROLLER_RUFF_TIMEOUT_S,
            "ruff_cleanup_seconds": CONTROLLER_CLEANUP_TIMEOUT_S,
            "git_command_seconds": CONTROLLER_GIT_TIMEOUT_S,
            "git_command_limit": CONTROLLER_GIT_COMMAND_LIMIT,
            "overhead_seconds": CONTROLLER_OVERHEAD_RESERVE_S,
            "builder_reserve_seconds": BUILDER_POST_RUN_RESERVE_S,
            "stop_drain_seconds": CONTROLLER_STOP_DRAIN_TIMEOUT_S,
        },
        "cards": [candidate_manifest(candidates[item], snapshots[item]) for item in candidates],
        "completion_authority": "none; independent verifier attestation required",
        "board_mutation": "forbidden",
    }


def _observed(value: object) -> dict[str, Any]:
    return {"state": "observed", "value": value, "reason": None}


def _unknown(reason: str) -> dict[str, Any]:
    return {"state": "unknown", "value": None, "reason": reason}


def _unknown_observation(reason: str, artifact: str | None = None) -> dict[str, Any]:
    return {
        "artifact": artifact,
        "classification": None,
        "duration_s": _unknown(reason),
        "requested_model": _unknown(reason),
        "served_model": _unknown(reason),
        "tokens": _unknown(reason),
        "tool_calls": _unknown(reason),
        "cost": _unknown(reason),
    }


def trusted_attempt_observation(run_path: Path) -> dict[str, Any]:
    """Derive comparison telemetry only from controller-captured run artifacts."""
    try:
        payload = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _unknown_observation(
            f"run_artifact_invalid:{type(exc).__name__}", str(run_path)
        )
    if not isinstance(payload, dict):
        return _unknown_observation("run_artifact_not_object", str(run_path))
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    stdout_path = run_path.with_name("stdout.log")
    stdout_exists = stdout_path.is_file()
    raw = stdout_path.read_bytes() if stdout_exists else None
    scan = scan_pi_events(raw)
    assistant_events = assistant_message_events(scan)
    model, model_reason = served_model_evidence(scan)
    if not stdout_exists:
        served_model = _unknown("raw_stdout_missing")
    elif model is not None:
        served_model = _observed(model)
    else:
        served_model = _unknown(
            model_reason.value if model_reason is not None else "served_model_unknown"
        )

    requested = metrics.get("requested_model")
    if requested == MODEL:
        requested_model = _observed(requested)
    elif requested is None:
        requested_model = _unknown("requested_model_missing")
    else:
        requested_model = _unknown("requested_model_mismatch")

    duration = metrics.get("duration_s")
    duration_s = (
        _observed(float(duration))
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0
        else _unknown("duration_missing_or_invalid")
    )

    def aggregate_usage(kind: str) -> dict[str, Any]:
        if not stdout_exists:
            return _unknown("raw_stdout_missing")
        if scan.incomplete:
            return _unknown("pi_event_stream_incomplete")
        if not assistant_events:
            return _unknown("assistant_usage_not_observed")
        values: list[int | float] = []
        for _event, message in assistant_events:
            usage = message.get("usage")
            if not isinstance(usage, dict):
                return _unknown("assistant_usage_partial")
            if kind == "tokens":
                value = usage.get("totalTokens")
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    return _unknown("assistant_token_usage_partial")
            else:
                costs = usage.get("cost")
                value = costs.get("total") if isinstance(costs, dict) else None
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or value < 0
                ):
                    return _unknown("assistant_cost_usage_partial")
            values.append(value)
        total = sum(values)
        return _observed(int(total) if kind == "tokens" else round(float(total), 8))

    if not stdout_exists:
        tool_calls = _unknown("raw_stdout_missing")
    elif scan.incomplete:
        tool_calls = _unknown("pi_event_stream_incomplete")
    elif not scan.events:
        tool_calls = _unknown("pi_events_not_observed")
    else:
        tool_calls = _observed(
            sum(1 for event in scan.events if event.get("type") == "tool_execution_start")
        )
    return {
        "artifact": str(run_path),
        "classification": payload.get("classification"),
        "duration_s": duration_s,
        "requested_model": requested_model,
        "served_model": served_model,
        "tokens": aggregate_usage("tokens"),
        "tool_calls": tool_calls,
        "cost": aggregate_usage("cost"),
    }


def accounting_usage(
    observation: dict[str, Any], contract: SubagentContract
) -> tuple[BudgetUsage, dict[str, Any]]:
    """Charge the reservation for every unobserved dimension without calling it measured."""
    dimensions = {
        "wall_seconds": ("duration_s", contract.budget.wall_seconds),
        "tokens": ("tokens", contract.budget.token_limit),
        "tool_calls": ("tool_calls", contract.budget.tool_call_limit),
        "cost": ("cost", contract.budget.cost_limit),
    }
    values: dict[str, int | float] = {}
    basis: dict[str, str] = {}
    for target, (source, reservation) in dimensions.items():
        item = observation[source]
        if item["state"] == "observed":
            value = item["value"]
            values[target] = math.ceil(value) if target == "wall_seconds" else value
            basis[target] = "observed"
        else:
            values[target] = reservation
            basis[target] = "reservation"
    usage = BudgetUsage.model_validate(values)
    return usage, {
        "basis_by_dimension": basis,
        "charged": usage.model_dump(mode="json"),
        "comparison_uses_charged_values": False,
    }


def include_controller_wall_time(
    usage: BudgetUsage,
    accounting: dict[str, Any],
    runtime_usage: BudgetUsage,
) -> tuple[BudgetUsage, dict[str, Any]]:
    """Charge end-to-end controller wall time without relabeling it as Pi latency."""
    if runtime_usage.wall_seconds <= usage.wall_seconds:
        return usage, accounting
    usage = usage.model_copy(update={"wall_seconds": runtime_usage.wall_seconds})
    accounting = {
        **accounting,
        "basis_by_dimension": {
            **accounting["basis_by_dimension"],
            "wall_seconds": "controller_elapsed",
        },
        "charged": usage.model_dump(mode="json"),
        "controller_elapsed_wall_seconds": runtime_usage.wall_seconds,
    }
    return usage, accounting


def find_attempt_observation(root: Path) -> dict[str, Any]:
    paths = sorted(root.glob("**/run.json"))
    if len(paths) != 1:
        reason = "attempt_artifact_missing" if not paths else "attempt_artifact_ambiguous"
        return _unknown_observation(reason)
    return trusted_attempt_observation(paths[0])


def collect_attempt_summaries(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted((root / "attempts").glob("**/run.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows.append(
            {
                "path": str(path.relative_to(root)),
                "classification": payload.get("classification"),
                "exit_code": payload.get("exit_code"),
                "stdout_digest": payload.get("stdout_digest"),
                "stderr_digest": payload.get("stderr_digest"),
                "scout_assessment": payload.get("scout_assessment"),
                "scout_findings": payload.get("scout_findings", []),
                "trusted_observation": trusted_attempt_observation(path),
            }
        )
    return rows


def execute_candidate(
    candidate: Candidate,
    snapshot: dict[str, Any],
    *,
    source: Path,
    worktree_root: Path,
    evidence_root: Path,
    image: str,
    run_stamp: str,
) -> dict[str, Any]:
    card_root = evidence_root / candidate.card_id
    card_root.mkdir(parents=True, exist_ok=False)
    worktree = worktree_root / f"pi-swarm-v038-{candidate.card_id}-{run_stamp}"
    create_worktree(source, worktree)
    now = datetime.now(timezone.utc)
    trajectory_id = f"pi-swarm-v038-{candidate.card_id}-{run_stamp}"
    identity = SwarmIdentity(
        card_id=candidate.card_id,
        card_hash=candidate.card_hash,
        base_commit=WORKER_BASE_COMMIT,
        evidence_id=canonical_digest(
            {
                "schema": SCHEMA, "trajectory_id": trajectory_id, "image": image,
                "gateway": GATEWAY, "model": MODEL, "card_hash": candidate.card_hash,
            }
        ),
        trajectory_id=trajectory_id,
    )
    plan, contracts = make_plan_and_contracts(candidate, identity, now, worktree)
    total_wall = sum(int(item.budget.wall_seconds) for item in contracts)
    total_tokens = sum(item.budget.token_limit for item in contracts)
    total_tools = sum(item.budget.tool_call_limit for item in contracts)
    scheduler = SwarmScheduler(
        TeamBudget(
            team_id=f"team-{trajectory_id}", wall_seconds=total_wall,
            token_limit=total_tokens, tool_call_limit=total_tools, cost_limit=0,
            max_concurrency=candidate.max_concurrency,
        ),
        identity=identity,
        orchestrator_id="lumina-orchestrator",
        lease_ttl_s=20,
        state_path=card_root / "swarm-state.json",
    )
    gate = SwarmCompletionGate(
        plan=plan,
        required_criteria=(f"card:{candidate.card_id}:acceptance",),
        trusted_verifier_ids=("independent-verifier",),
        verify_signature=lambda _item: False,
    )
    worker_by_id = {item.contract_id: item for item in candidate.workers}
    executed: list[str] = []
    telemetry: dict[str, dict[str, Any]] = {}
    executed_lock = threading.Lock()
    gateway_host = urlsplit(GATEWAY).hostname
    if not gateway_host:
        raise RuntimeError("frozen gateway has no hostname")
    activity_journal = ActivityJournal()

    def runner_factory(contract: SubagentContract) -> PiExperimentRunner:
        worker = worker_by_id[contract.contract_id]
        experiment_id = f"{candidate.card_id}-{contract.contract_id}"
        store = ArenaStore(card_root / "arena" / contract.contract_id)
        controller = ArenaController(
            store,
            LeaseScheduler(ResourceRequest(cpu=2, ram_gb=4, gateway_slots=1), lease_ttl_s=20),
            writer_id=f"writer-{contract.contract_id}",
            actor="lumina-orchestrator", node=QUALIFIED_HOST,
            session_id=identity.trajectory_id,
        )
        controller.propose(experiment_id)
        return FixedBudgetPiExperimentRunner(
            controller,
            SandboxProcessSupervisor(
                Sandbox(live_execution=True, run_timeout=worker.wall_seconds + 30),
                shutdown_grace_s=5,
            ),
            card_root / "attempts" / contract.contract_id,
            phase_budget=worker.phase_budget,
            activity_journal=activity_journal,
        )

    def launch_factory(contract: SubagentContract) -> PiSwarmLaunch:
        worker = worker_by_id[contract.contract_id]
        profile = "arena-build" if contract.role is SwarmRole.BUILDER else "arena-verify"
        adapter = PiAdapter(
            model=MODEL, base_url=GATEWAY, egress_hosts=[gateway_host],
            live_execution=True, image=image,
            max_tokens=min(32_768, contract.budget.token_limit),
            run_timeout=worker.wall_seconds,
            session_id=identity.trajectory_id, card_id=candidate.card_id,
            capability_profile=profile,
        )
        return PiSwarmLaunch(
            request=AttemptRequest(
                challenge_id=candidate.card_id,
                experiment_id=f"{candidate.card_id}-{contract.contract_id}",
                attempt_id="1",
                idempotency_key=f"{identity.trajectory_id}:{contract.contract_id}",
                resources=ResourceRequest(cpu=1.5, ram_gb=3, gateway_slots=1),
            ),
            spec=pi_launch_spec(
                adapter, prompt=contract.task, worktree=str(worktree), model=MODEL,
                card_size=candidate.size, phase_budget=worker.phase_budget,
            ),
            card_size=candidate.size, requested_model=MODEL,
            timeout_s=worker.wall_seconds,
        )

    runtime = PiSwarmWorkerRuntime(
        runner_factory=runner_factory,
        launch_factory=launch_factory,
        observe_commit=lambda contract, cancellation: controller_commit(
            worktree,
            candidate,
            image,
            card_root,
            validation_run_id=f"{identity.trajectory_id}:{contract.contract_id}",
            cancellation=cancellation,
        ),
        post_run_reserve_s=BUILDER_POST_RUN_RESERVE_S,
        stop_drain_timeout_s=CONTROLLER_STOP_DRAIN_TIMEOUT_S,
    )

    def execute(contract: SubagentContract) -> WorkerExecution:
        with executed_lock:
            executed.append(contract.contract_id)
        execution = runtime.execute(contract)
        observation = find_attempt_observation(
            card_root / "attempts" / contract.contract_id
        )
        usage, accounting = accounting_usage(observation, contract)
        usage, accounting = include_controller_wall_time(
            usage, accounting, execution.usage
        )
        with executed_lock:
            telemetry[contract.contract_id] = {
                "contract_id": contract.contract_id,
                "measured": observation,
                "accounting": accounting,
            }
        return replace(execution, usage=usage)

    atlas_owner = SwarmAtlasControlOwner(
        scheduler=scheduler,
        contracts=contracts,
        stop_worker=runtime.stop,
        control_journal=ControlJournal(),
        activity_journal=activity_journal,
    )
    atlas_owner.start()
    try:
        report = TrustedSwarmOrchestrator(
            scheduler, gate, A2AJournal(card_root / "a2a.jsonl"), plan,
            shutdown_grace_s=15,
            activity_journal=activity_journal,
        ).run(
            contracts, execute=execute, stop=runtime.stop,
            # This qualification deliberately cannot complete a board card. An
            # independent signed verifier may evaluate the retained lineage later.
            attest=lambda _results, _receipts: None,
        )
    finally:
        atlas_owner.stop()
    try:
        runtime.assert_idle()
    except RuntimeError as exc:
        raise QualificationQuiescenceError(str(exc)) from exc
    lifecycle_terminal = any(
        reason.startswith(
            (
                "worker_timed_out:",
                "worker_stop_not_quiescent:",
                "worker_shutdown_grace_exceeded:",
            )
        )
        for reason in report.failure_reasons
    )
    status_after = git(worktree, "status", "--porcelain", "--untracked-files=all")
    outcome = {
        "schema": SCHEMA,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "card": candidate_manifest(candidate, snapshot),
        "node": socket.gethostname(),
        "controller_source_commit": git(source, "rev-parse", "HEAD"),
        "worker_base_commit": WORKER_BASE_COMMIT,
        "worktree": str(worktree),
        "worktree_head": git(worktree, "rev-parse", "HEAD"),
        "worktree_status_after": status_after,
        "image": image,
        "gateway": GATEWAY,
        "requested_model": MODEL,
        "identity": identity.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
        "plan_hash": plan.content_hash,
        "contracts": [item.model_dump(mode="json") for item in contracts],
        "executed_contract_ids": executed,
        "results": [item.model_dump(mode="json") for item in report.results],
        "phase_receipts": [item.model_dump(mode="json") for item in report.phase_receipts],
        "phase_authorization_hashes": report.phase_authorization_hashes,
        "completion": report.completion.model_dump(mode="json"),
        "failure_reasons": report.failure_reasons,
        "cancelled_lease_ids": report.cancelled_lease_ids,
        "scheduler": scheduler.snapshot(),
        "attempts": collect_attempt_summaries(card_root),
        "telemetry": [telemetry[item] for item in sorted(telemetry)],
        "board_mutated": False,
        "disposition": "failed" if lifecycle_terminal else "review_required",
        "lifecycle_terminal": lifecycle_terminal,
        "next_card_admitted": not lifecycle_terminal,
    }
    (card_root / "qualification.json").write_text(
        json.dumps(outcome, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return outcome


def execute_all(
    args: argparse.Namespace,
    snapshots: dict[str, dict[str, Any]],
    controller_provenance: dict[str, Any],
    candidates: dict[str, Candidate] | None = None,
) -> dict[str, Any]:
    candidates = candidates or CANDIDATES
    image = require_image_digest(args.image)
    if socket.gethostname() != QUALIFIED_HOST:
        raise RuntimeError(f"live execution is pinned to {QUALIFIED_HOST}")
    source = args.source.resolve()
    observed_controller = validate_controller_source(source, args.controller_commit)
    if observed_controller != controller_provenance:
        raise RuntimeError("controller provenance changed after preflight")
    image_provenance = validate_local_image(image)
    if args.evidence_root.exists():
        raise RuntimeError(f"refusing existing evidence root: {args.evidence_root}")
    args.evidence_root.mkdir(parents=True, exist_ok=False)
    args.worktree_root.mkdir(parents=True, exist_ok=True)
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pre_inventory = managed_docker_inventory()
    (args.evidence_root / "pre-run-docker-inventory.json").write_text(
        json.dumps(pre_inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    # Valid resources from another qualification are not this run's blockers;
    # every managed record was already label-validated by inventory collection.
    require_no_managed_resources(pre_inventory, ignore_foreign=True)
    manifest = build_manifest(image, snapshots, controller_provenance, candidates) | {
        "mode": "execute",
        "run_stamp": run_stamp,
        "controller_source_commit": controller_provenance["commit"],
        "local_image_provenance": image_provenance,
    }
    (args.evidence_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    outcomes = []
    for card_id in candidates:
        candidate_outcome: dict[str, Any] | None = None
        candidate_error: Exception | None = None
        try:
            if validate_controller_source(source, args.controller_commit) != controller_provenance:
                raise RuntimeError("controller provenance changed before card admission")
            candidate_outcome = execute_candidate(
                candidates[card_id], snapshots[card_id], source=source,
                worktree_root=args.worktree_root, evidence_root=args.evidence_root,
                image=image, run_stamp=run_stamp,
            )
        except Exception as exc:  # fail one card closed without erasing the other trials
            candidate_error = exc

        cleanup_inventory: dict[str, Any] | None = None
        cleanup_error: Exception | None = None
        try:
            cleanup_inventory = managed_docker_inventory()
            (args.evidence_root / f"{card_id}-post-cleanup-inventory.json").write_text(
                json.dumps(cleanup_inventory, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            run_prefix = f"pi-swarm-v038-{card_id}-{run_stamp}"
            require_no_managed_resources(
                cleanup_inventory, run_id_prefixes=(run_prefix,),
            )
            if validate_controller_source(source, args.controller_commit) != controller_provenance:
                raise RuntimeError("controller provenance changed during card execution")
        except Exception as exc:
            cleanup_error = exc

        if cleanup_error is not None:
            failure = {
                "card_id": card_id,
                "disposition": "failed",
                "error_type": type(cleanup_error).__name__,
                "error": f"cleanup_not_proven:{str(cleanup_error)[:900]}",
                "candidate_error": (
                    f"{type(candidate_error).__name__}:{str(candidate_error)[:500]}"
                    if candidate_error is not None
                    else None
                ),
                "cleanup_inventory": cleanup_inventory,
                "next_card_admitted": False,
            }
            outcomes.append(failure)
            (args.evidence_root / f"{card_id}-failure.json").write_text(
                json.dumps(failure, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            if isinstance(candidate_error, QualificationQuiescenceError):
                raise candidate_error
            raise QualificationQuiescenceError(
                f"post-card cleanup is not proven for {card_id}: {cleanup_error}"
            ) from cleanup_error
        if candidate_error is not None:
            failure = {
                "card_id": card_id, "disposition": "failed",
                "error_type": type(candidate_error).__name__,
                "error": str(candidate_error)[:1000],
                "cleanup_inventory": cleanup_inventory,
                "cleanup_verified": True,
                "next_card_admitted": not isinstance(
                    candidate_error, QualificationQuiescenceError
                ),
            }
            outcomes.append(failure)
            (args.evidence_root / f"{card_id}-failure.json").write_text(
                json.dumps(failure, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            if isinstance(candidate_error, QualificationQuiescenceError):
                raise candidate_error
            continue
        assert candidate_outcome is not None
        candidate_outcome["cleanup_inventory"] = cleanup_inventory
        candidate_outcome["cleanup_verified"] = True
        outcomes.append(candidate_outcome)
        if candidate_outcome.get("lifecycle_terminal"):
            break
    summary = manifest | {"outcomes": outcomes}
    (args.evidence_root / "qualification-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_bundle_digest(args.evidence_root)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="start the frozen live workers")
    parser.add_argument(
        "--card-id", action="append", dest="card_ids", metavar="ID",
        help=("run a reviewed remediation topology (repeatable): c278b5c0 or "
              "400bf174; arbitrary CardStore cards are refused"),
    )
    parser.add_argument(
        "--image", default=os.environ.get("SKHARNESS_PI_SWARM_IMAGE"),
        help="required equality-pinned v0.3.38 pi-python-test image",
    )
    parser.add_argument(
        "--controller-commit",
        default=os.environ.get("SKHARNESS_PI_SWARM_CONTROLLER_COMMIT"),
        help="required exact reviewed commit containing this controller driver",
    )
    parser.add_argument(
        "--source", type=Path,
        default=Path("/home/cbrd21/clawd/skcapstone-repos/skharness"),
    )
    parser.add_argument(
        "--worktree-root", type=Path,
        default=Path("/home/cbrd21/clawd/worktrees"),
    )
    parser.add_argument(
        "--evidence-root", type=Path,
        default=Path("/home/cbrd21/.skcapstone/qualification/pi-swarm-v038"),
    )
    parser.add_argument(
        "--card-root", type=Path, default=Path("/home/cbrd21/.skcapstone/cards"),
        help="canonical CardStore root containing <card-id>/core.json",
    )
    parser.add_argument("--output", type=Path, help="optional preflight manifest path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        image = require_image_digest(args.image)
        controller_commit = require_controller_commit(args.controller_commit)
        controller_provenance = validate_controller_source(args.source, controller_commit)
        card_ids = tuple(args.card_ids) if args.card_ids else None
        candidates = candidate_catalog(card_ids)
        snapshots = (
            load_card_snapshots(args.card_root)
            if card_ids is None
            else load_card_snapshots(args.card_root, card_ids)
        )
        if args.execute:
            result = execute_all(args, snapshots, controller_provenance, candidates)
        else:
            result = build_manifest(image, snapshots, controller_provenance, candidates)
        encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        sys.stdout.write(encoded)
        if args.execute and any(
            item.get("disposition") == "failed" for item in result.get("outcomes", [])
        ):
            return 1
        return 0
    except Exception as exc:
        sys.stderr.write(f"qualification refused: {type(exc).__name__}: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
