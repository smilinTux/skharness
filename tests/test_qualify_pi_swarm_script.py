from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from skharness.arena.models import canonical_digest
from skharness.arena.swarm import BudgetUsage, SwarmIdentity, SwarmRole
from skharness.autocode.sandbox import LaunchSpec, Sandbox

SCRIPT = Path(__file__).parents[1] / "scripts" / "qualify-pi-swarm.py"
SPEC = importlib.util.spec_from_file_location("qualify_pi_swarm", SCRIPT)
QUALIFY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = QUALIFY
SPEC.loader.exec_module(QUALIFY)

IMAGE = QUALIFY.QUALIFIED_IMAGE
OTHER_IMAGE = "registry.example/skharness@sha256:" + "a" * 64
DIGEST = "sha256:" + "b" * 64
CONTROLLER_COMMIT = "c" * 40
CONTROLLER = {
    "commit": CONTROLLER_COMMIT,
    "source_root": "/source",
    "script_path": "scripts/qualify-pi-swarm.py",
    "module_paths": {},
    "release_evidence": {
        "path": QUALIFY.RELEASE_EVIDENCE_PATH.as_posix(),
        "content_hash": DIGEST,
        "record": QUALIFY.RELEASE_EVIDENCE,
    },
}


class FakeValidationProcess:
    def __init__(self, *, running=False, on_poll=None):
        self.returncode = None if running else 0
        self._on_poll = on_poll

    def poll(self):
        if self._on_poll is not None:
            callback, self._on_poll = self._on_poll, None
            callback()
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 137 if self.returncode is None else self.returncode
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_image_must_equal_the_qualified_release_digest():
    assert QUALIFY.require_image_digest(IMAGE) == IMAGE
    for bad in (
        None,
        "registry.example/skharness:v0.3.38",
        "sha256:" + "a" * 64,
        OTHER_IMAGE,
    ):
        with pytest.raises(ValueError, match="must equal"):
            QUALIFY.require_image_digest(bad)


def test_local_image_must_bind_exact_repo_digest_and_release_labels(monkeypatch):
    record = {
        "Id": "sha256:image-id",
        "RepoDigests": [IMAGE],
        "Config": {
            "Labels": {
                "org.opencontainers.image.version": "0.3.38",
                "org.opencontainers.image.ref.name": "v0.3.38",
                "org.opencontainers.image.revision": QUALIFY.WORKER_BASE_COMMIT,
                "io.skharness.image.build-mode": "release",
            }
        },
    }
    monkeypatch.setattr(
        QUALIFY, "run", lambda _argv: SimpleNamespace(stdout=json.dumps([record]))
    )

    assert QUALIFY.validate_local_image(IMAGE)["repo_digest"] == IMAGE
    record["RepoDigests"] = [OTHER_IMAGE]
    with pytest.raises(RuntimeError, match="RepoDigest"):
        QUALIFY.validate_local_image(IMAGE)
    record["RepoDigests"] = [IMAGE]
    record["Config"]["Labels"]["org.opencontainers.image.version"] = "0.3.37"
    with pytest.raises(RuntimeError, match="provenance labels"):
        QUALIFY.validate_local_image(IMAGE)


def test_frozen_candidates_pin_hash_route_topology_and_budget():
    assert QUALIFY.WORKER_BASE_COMMIT == "2e8e4d89aac1967fb297c0558b311998a9bc1e9a"
    assert QUALIFY.MODEL == "ornith-1.5-9b"
    assert QUALIFY.GATEWAY == "http://100.86.156.5:18780/v1"
    assert {
        "skharness.autocode.pi_events",
        "skharness.autocode.sandbox_lifecycle",
    } <= set(QUALIFY.CONTROLLER_MODULES)
    assert QUALIFY.BUILDER_POST_RUN_RESERVE_S == 180
    assert {item: candidate.card_hash for item, candidate in QUALIFY.CANDIDATES.items()} == {
        "0f34e285": "sha256:e6a2971747260c4089f84b4b0cd5d9540321a7004f1ff8b717f175c05dc445d6",
        "5b88d88c": "sha256:cecffcaa49b7b22e84d7425049fa3e00d80bee9648ebe1f1f2d94320a7287b26",
        "41077231": "sha256:728d2c3af5cd438b1ee6780591f085ce6e0ef03c9ddac89b135d629ed80bb2df",
    }

    small, medium, large = (QUALIFY.CANDIDATES[item] for item in QUALIFY.CANDIDATES)
    assert [worker.role for worker in small.workers] == [SwarmRole.BUILDER]
    assert [worker.role for worker in medium.workers] == [
        SwarmRole.SCOUT,
        SwarmRole.BUILDER,
        SwarmRole.TESTER,
    ]
    assert [worker.role for worker in large.workers] == [SwarmRole.SCOUT, SwarmRole.SCOUT]
    assert large.allowed_changes == frozenset()
    assert "scout_only" in large.suitability
    for candidate in QUALIFY.CANDIDATES.values():
        for worker in candidate.workers:
            assert worker.phase_budget.total_s == worker.pi_wall_seconds
            assert (
                worker.pi_wall_seconds + worker.controller_reserve_seconds
                == worker.wall_seconds
            )
            assert worker.controller_reserve_seconds == (
                QUALIFY.BUILDER_POST_RUN_RESERVE_S
                if worker.role is SwarmRole.BUILDER
                else 0
            )
            assert worker.tool_limit > 0
            assert worker.token_limit > 0


def test_remediation_profile_is_explicit_and_refuses_arbitrary_cards():
    selected = QUALIFY.candidate_catalog(("c278b5c0", "400bf174"))
    assert tuple(selected) == ("c278b5c0", "400bf174")
    assert selected["c278b5c0"].size.value == "S"
    assert selected["400bf174"].size.value == "M"
    with pytest.raises(ValueError, match="reviewed qualification topology"):
        QUALIFY.candidate_catalog(("unreviewed-card",))


def test_remediation_snapshot_hashes_are_immutable_and_profile_is_narrow():
    assert {
        card_id: candidate.card_hash
        for card_id, candidate in QUALIFY.REMEDIATION_CANDIDATES.items()
    } == {
        "c278b5c0": "sha256:37cc7a375db5e6ba63c7593ffe13d37ab540d97594ece8bf47046016a753548f",
        "400bf174": "sha256:236fe4ffa7dc2b5d45f522646307e2d493416190f65ca01aea71e2c1e0f3a3f7",
    }
    # A selected profile does not silently include the frozen S/M/L cards.
    assert set(QUALIFY.candidate_catalog(("c278b5c0",))) == {"c278b5c0"}


def test_small_cleanup_remediation_has_bounded_read_only_preflight():
    candidate = QUALIFY.REMEDIATION_CANDIDATES["c278b5c0"]
    assert candidate.phases == (
        ("phase-preflight", SwarmRole.SCOUT, ("c278b5c0-preflight",), ()),
        ("phase-build", SwarmRole.BUILDER, ("c278b5c0-builder",), ("phase-preflight",)),
    )
    preflight, builder = candidate.workers
    assert preflight.role is SwarmRole.SCOUT
    assert preflight.writable_paths == ()
    assert preflight.tool_limit == 8
    assert preflight.phase_budget.total_s == preflight.pi_wall_seconds
    assert preflight.phase_budget.inspect_s == 35
    assert builder.phase_id == "phase-build"
    assert set(builder.writable_paths) == set(candidate.allowed_changes)
    assert "ACTIONABLE" in preflight.task and "BLOCKED" in preflight.task
    assert "preflight findings" in builder.task


def test_every_writable_scope_is_an_exact_readable_mount(tmp_path):
    all_paths = {
        path
        for candidate in QUALIFY.CANDIDATES.values()
        for worker in candidate.workers
        for path in worker.readable_paths
    }
    for relative in sorted(all_paths):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)

    for candidate in QUALIFY.CANDIDATES.values():
        for worker in candidate.workers:
            assert set(worker.writable_paths) <= set(worker.readable_paths)
            spec = LaunchSpec(
                name=worker.contract_id,
                argv=["pi"],
                image=IMAGE,
                worktree=str(tmp_path),
                scoped_readable_paths=worker.readable_paths,
                scoped_writable_paths=worker.writable_paths,
            )
            mounts = Sandbox._scoped_worktree_mounts(spec, str(tmp_path))
            assert {mount.dst for mount in mounts if not mount.ro} == {
                f"/work/{path}" for path in worker.writable_paths
            }


def test_card_snapshot_uses_canonical_core_not_lossy_kanban(tmp_path):
    card = {
        "id": "card-x",
        "title": "Exact title",
        "description": "Exact description",
        "acceptance_criteria": ["criterion"],
        "dependencies": ["dep"],
        "status": "backlog",
        "owner": "lumina",
        "updated_at": "volatile",
    }
    snapshot = QUALIFY.normalized_card(card)
    candidate = replace(
        QUALIFY.CANDIDATES["0f34e285"],
        card_id="card-x",
        card_hash=canonical_digest(snapshot),
    )
    original = QUALIFY.CANDIDATES
    QUALIFY.CANDIDATES = {"card-x": candidate}
    card_dir = tmp_path / "card-x"
    card_dir.mkdir()
    (card_dir / "core.json").write_text(json.dumps(card), encoding="utf-8")
    try:
        assert QUALIFY.load_card_snapshots(tmp_path) == {"card-x": snapshot}

        # This is the shape returned by ``coord kanban --json``. Its missing
        # acceptance criteria produce a different digest and cannot define the
        # immutable execution input.
        lossy_kanban_card = dict(card)
        lossy_kanban_card.pop("acceptance_criteria")
        assert canonical_digest(QUALIFY.normalized_card(lossy_kanban_card)) != candidate.card_hash

        card["description"] = "drifted"
        (card_dir / "core.json").write_text(json.dumps(card), encoding="utf-8")
        with pytest.raises(RuntimeError, match="card content drift"):
            QUALIFY.load_card_snapshots(tmp_path)
    finally:
        QUALIFY.CANDIDATES = original


def test_production_plan_binds_exact_identity_and_phase_lineage(tmp_path):
    now = datetime(2026, 8, 21, tzinfo=timezone.utc)
    for candidate in QUALIFY.CANDIDATES.values():
        identity = SwarmIdentity(
            card_id=candidate.card_id,
            card_hash=candidate.card_hash,
            base_commit=QUALIFY.WORKER_BASE_COMMIT,
            evidence_id=DIGEST,
            trajectory_id=f"test-{candidate.card_id}",
        )
        plan, contracts = QUALIFY.make_plan_and_contracts(
            candidate, identity, now, tmp_path / candidate.card_id
        )
        assert plan.identity == identity
        assert tuple(item.contract_id for item in contracts) == tuple(
            worker.contract_id for worker in candidate.workers
        )
        assert all(item.plan_hash == plan.content_hash for item in contracts)
        assert all(item.identity == identity for item in contracts)
        for phase_id, _role, contract_ids, predecessors in candidate.phases:
            phase = plan.phase(phase_id)
            assert phase.contract_ids == contract_ids
            assert phase.predecessor_phase_ids == predecessors


def test_source_and_linked_worktree_preflight_refuse_dirty_or_wrong_commit(tmp_path):
    source = tmp_path / "source"
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "test"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    (source / "tracked").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "tracked"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "base"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(source), "tag", "v0.3.38", commit], check=True)

    original = QUALIFY.WORKER_BASE_COMMIT
    QUALIFY.WORKER_BASE_COMMIT = commit
    try:
        assert QUALIFY.validate_source_repo(source) == commit
        linked = tmp_path / "linked"
        QUALIFY.create_worktree(source, linked)
        QUALIFY.validate_worktree(linked)
        (linked / "untracked").write_text("dirty\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="not clean"):
            QUALIFY.validate_worktree(linked)
        (source / "dirty").write_text("dirty\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="must be clean"):
            QUALIFY.validate_source_repo(source)
    finally:
        QUALIFY.WORKER_BASE_COMMIT = original


def test_controller_source_binds_commit_script_modules_and_release_record(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "test"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    script = source / "scripts" / "qualify-pi-swarm.py"
    module_file = source / "src" / "qualified_controller.py"
    release = source / QUALIFY.RELEASE_EVIDENCE_PATH
    for path in (script, module_file, release):
        path.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# pinned controller\n", encoding="utf-8")
    module_file.write_text("VALUE = 1\n", encoding="utf-8")
    release.write_text(
        json.dumps(QUALIFY.RELEASE_EVIDENCE, sort_keys=True) + "\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "controller"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(source), "tag", QUALIFY.RELEASE_EVIDENCE["tag"], commit],
        check=True,
    )
    module_name = "qualified_controller_test_module"
    monkeypatch.setattr(QUALIFY, "WORKER_BASE_COMMIT", commit)
    monkeypatch.setattr(QUALIFY, "CONTROLLER_MODULES", (module_name,))
    monkeypatch.setattr(QUALIFY, "__file__", str(script))
    monkeypatch.setitem(sys.modules, module_name, SimpleNamespace(__file__=str(module_file)))

    provenance = QUALIFY.validate_controller_source(source, commit)
    assert provenance["commit"] == commit
    assert provenance["module_paths"][module_name] == str(module_file)
    assert provenance["release_evidence"]["record"] == QUALIFY.RELEASE_EVIDENCE

    with pytest.raises(RuntimeError, match="source drift"):
        QUALIFY.validate_controller_source(source, "d" * 40)
    monkeypatch.setitem(
        sys.modules, module_name, SimpleNamespace(__file__=str(tmp_path / "outside.py"))
    )
    (tmp_path / "outside.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="outside the pinned checkout"):
        QUALIFY.validate_controller_source(source, commit)


def test_release_evidence_content_drift_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / QUALIFY.RELEASE_EVIDENCE_PATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(QUALIFY.RELEASE_EVIDENCE), encoding="utf-8")
    monkeypatch.setattr(QUALIFY, "_require_tracked_file", lambda *_args: "release.json")
    monkeypatch.setattr(QUALIFY, "git", lambda *_args: QUALIFY.WORKER_BASE_COMMIT)

    assert QUALIFY.validate_release_evidence(tmp_path, CONTROLLER_COMMIT)["record"] == (
        QUALIFY.RELEASE_EVIDENCE
    )
    payload = dict(QUALIFY.RELEASE_EVIDENCE)
    payload["tag"] = "v0.3.39"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="does not match"):
        QUALIFY.validate_release_evidence(tmp_path, CONTROLLER_COMMIT)


def test_managed_inventory_is_label_bound_and_leftovers_fail_closed(monkeypatch):
    labels = {
        QUALIFY.MANAGED_LABEL: "true",
        QUALIFY.RUN_ID_LABEL: "qualified-run",
        QUALIFY.RESOURCE_ROLE_LABEL: "worker",
        QUALIFY.OWNERSHIP_AUTHORITY_LABEL: "ephemeral",
        QUALIFY.SCHEMA_LABEL: QUALIFY.LIFECYCLE_SCHEMA,
    }

    def fake_run(argv, **_kwargs):
        if argv[1:3] == ["ps", "-a"]:
            return SimpleNamespace(stdout="container-id\n")
        if argv[1:3] == ["network", "ls"]:
            return SimpleNamespace(stdout="")
        if argv[1] == "inspect":
            return SimpleNamespace(
                stdout=json.dumps(
                    [
                        {
                            "Id": "container-id",
                            "Name": "/qualified-worker",
                            "Config": {"Labels": labels},
                            "State": {"Running": True},
                        }
                    ]
                )
            )
        raise AssertionError(argv)

    monkeypatch.setattr(QUALIFY, "run", fake_run)
    inventory = QUALIFY.managed_docker_inventory()
    assert inventory["resource_count"] == 1
    assert inventory["resources"][0]["running"] is True
    with pytest.raises(RuntimeError, match="remain after cleanup"):
        QUALIFY.require_no_managed_resources(inventory)


def test_managed_inventory_command_error_never_becomes_empty(monkeypatch):
    def failed(_argv, **_kwargs):
        raise subprocess.CalledProcessError(1, ["docker", "ps"])

    monkeypatch.setattr(QUALIFY, "run", failed)
    with pytest.raises(subprocess.CalledProcessError):
        QUALIFY.managed_docker_inventory()


def test_run_scoped_inventory_ignores_foreign_resources_but_blocks_exact_ids():
    labels = {
        QUALIFY.MANAGED_LABEL: "true",
        QUALIFY.RUN_ID_LABEL: "foreign-run",
        QUALIFY.RESOURCE_ROLE_LABEL: "network",
        QUALIFY.OWNERSHIP_AUTHORITY_LABEL: "ephemeral",
        QUALIFY.SCHEMA_LABEL: QUALIFY.LIFECYCLE_SCHEMA,
    }
    inventory = {"resources": [{"id": "foreign-net", "type": "network", "labels": labels}]}
    QUALIFY.require_no_managed_resources(inventory, ignore_foreign=True)
    with pytest.raises(RuntimeError, match="remain after cleanup"):
        QUALIFY.require_no_managed_resources(inventory, exact_ids={"foreign-net"})


def test_run_scoped_inventory_rejects_ambiguous_duplicate_ownership():
    base = {
        "id": "same-id", "type": "network",
        "labels": {
            QUALIFY.MANAGED_LABEL: "true", QUALIFY.RUN_ID_LABEL: "run-a",
            QUALIFY.RESOURCE_ROLE_LABEL: "network",
            QUALIFY.OWNERSHIP_AUTHORITY_LABEL: "ephemeral",
            QUALIFY.SCHEMA_LABEL: QUALIFY.LIFECYCLE_SCHEMA,
        },
    }
    other = json.loads(json.dumps(base))
    other["labels"][QUALIFY.RUN_ID_LABEL] = "run-b"
    with pytest.raises(RuntimeError, match="ambiguous ownership"):
        QUALIFY.require_no_managed_resources({"resources": [base, other]}, ignore_foreign=True)


def _message_end(model=None, *, tokens=3, cost=0.25):
    message = {
        "role": "assistant",
        "content": [],
        "usage": {"totalTokens": tokens, "cost": {"total": cost}},
    }
    if model is not None:
        message["responseModel"] = model
    return {"type": "message_end", "message": message}


def _attempt_observation(tmp_path, events):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    (attempt / "run.json").write_text(
        json.dumps(
            {
                "classification": "exit",
                "metrics": {"duration_s": 1.25, "requested_model": QUALIFY.MODEL},
            }
        ),
        encoding="utf-8",
    )
    (attempt / "stdout.log").write_text(
        "\n".join(json.dumps(item) if isinstance(item, dict) else item for item in events),
        encoding="utf-8",
    )
    return QUALIFY.trusted_attempt_observation(attempt / "run.json")


def test_trusted_observation_aggregates_only_complete_agreeing_provider_events(tmp_path):
    observation = _attempt_observation(
        tmp_path,
        [
            {"type": "tool_execution_start", "toolName": "read", "args": {}},
            _message_end("ornith-served", tokens=3, cost=0.25),
            _message_end("ornith-served", tokens=4, cost=0.5),
        ],
    )

    assert observation["served_model"] == {
        "state": "observed", "value": "ornith-served", "reason": None
    }
    assert observation["tokens"]["value"] == 7
    assert observation["cost"]["value"] == 0.75
    assert observation["tool_calls"]["value"] == 1


@pytest.mark.parametrize(
    ("events", "reason"),
    [
        (
            [_message_end("model-a"), _message_end(None)],
            "provider_events_partial_response_model",
        ),
        (
            [_message_end("model-a"), _message_end("model-b")],
            "provider_events_conflicting_response_models",
        ),
        (
            [_message_end("model-a"), "not-json"],
            "provider_event_stream_malformed_or_incomplete",
        ),
        ([], "provider_event_missing_response_model"),
    ],
)
def test_trusted_observation_preserves_partial_conflict_incomplete_and_missing_model(
    tmp_path, events, reason
):
    observation = _attempt_observation(tmp_path, events)
    assert observation["served_model"] == {
        "state": "unknown", "value": None, "reason": reason
    }
    if reason == "provider_event_stream_malformed_or_incomplete":
        assert observation["tokens"]["state"] == "unknown"
        assert observation["cost"]["state"] == "unknown"
        assert observation["tool_calls"]["state"] == "unknown"
    if not events:
        assert observation["tokens"]["reason"] == "assistant_usage_not_observed"
        assert observation["tool_calls"]["reason"] == "pi_events_not_observed"


def test_unknown_usage_charges_reservation_without_becoming_measured(tmp_path):
    observation = _attempt_observation(tmp_path, [])
    candidate = QUALIFY.CANDIDATES["0f34e285"]
    identity = SwarmIdentity(
        card_id=candidate.card_id,
        card_hash=candidate.card_hash,
        base_commit=QUALIFY.WORKER_BASE_COMMIT,
        evidence_id=DIGEST,
        trajectory_id="reservation-test",
    )
    _plan, contracts = QUALIFY.make_plan_and_contracts(
        candidate, identity, datetime.now(timezone.utc), tmp_path
    )

    usage, accounting = QUALIFY.accounting_usage(observation, contracts[0])

    assert observation["tokens"]["value"] is None
    assert usage.tokens == contracts[0].budget.token_limit
    assert usage.tool_calls == contracts[0].budget.tool_call_limit
    assert accounting["basis_by_dimension"]["tokens"] == "reservation"
    assert accounting["comparison_uses_charged_values"] is False


def test_missing_cost_remains_unknown_while_firsthand_tokens_stay_observed(tmp_path):
    event = _message_end("ornith-served", tokens=9)
    event["message"]["usage"].pop("cost")
    observation = _attempt_observation(tmp_path, [event])

    assert observation["tokens"]["value"] == 9
    assert observation["cost"] == {
        "state": "unknown", "value": None, "reason": "assistant_cost_usage_partial"
    }


def test_controller_elapsed_wall_is_charged_without_becoming_pi_latency(tmp_path):
    observation = _attempt_observation(tmp_path, [_message_end("ornith-served")])
    candidate = QUALIFY.CANDIDATES["0f34e285"]
    identity = SwarmIdentity(
        card_id=candidate.card_id,
        card_hash=candidate.card_hash,
        base_commit=QUALIFY.WORKER_BASE_COMMIT,
        evidence_id=DIGEST,
        trajectory_id="controller-wall-test",
    )
    _plan, contracts = QUALIFY.make_plan_and_contracts(
        candidate, identity, datetime.now(timezone.utc), tmp_path
    )
    usage, accounting = QUALIFY.accounting_usage(observation, contracts[0])

    usage, accounting = QUALIFY.include_controller_wall_time(
        usage, accounting, BudgetUsage(wall_seconds=50)
    )

    assert observation["duration_s"]["value"] == 1.25
    assert usage.wall_seconds == 50
    assert accounting["basis_by_dimension"]["wall_seconds"] == "controller_elapsed"
    assert accounting["comparison_uses_charged_values"] is False


def test_controller_scope_refuses_empty_missing_and_foreign_paths():
    candidate = QUALIFY.CANDIDATES["0f34e285"]
    with pytest.raises(RuntimeError, match="without a worktree change"):
        QUALIFY.require_scoped_changes(candidate, frozenset())
    with pytest.raises(RuntimeError, match="omitted required"):
        QUALIFY.require_scoped_changes(
            candidate, frozenset({"src/skharness/autocode/direct.py"})
        )
    with pytest.raises(RuntimeError, match="out-of-scope"):
        QUALIFY.require_scoped_changes(
            candidate,
            candidate.required_changes | {"src/skharness/auth.py"},
        )
    QUALIFY.require_scoped_changes(candidate, candidate.required_changes)


def test_controller_checks_use_same_digest_image_without_network(tmp_path):
    candidate = QUALIFY.CANDIDATES["0f34e285"]
    argv = QUALIFY.image_test_argv(
        IMAGE, tmp_path, candidate, validation_run_id="test-validation"
    )
    assert argv[:3] == ["docker", "run", "--name"]
    assert "--rm" not in argv
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert IMAGE in argv
    assert f"{QUALIFY.MANAGED_LABEL}=true" in argv
    assert f"{QUALIFY.RESOURCE_ROLE_LABEL}=worker" in argv
    assert "no:cacheprovider" in argv
    assert argv[-3:] == list(candidate.controller_tests[-3:])


def test_hanging_controller_validation_is_timed_out_and_exactly_removed(
    tmp_path, monkeypatch
):
    candidate = QUALIFY.CANDIDATES["0f34e285"]
    argv = QUALIFY.image_test_argv(
        IMAGE, tmp_path, candidate, validation_run_id="hanging-validation"
    )
    name = argv[argv.index("--name") + 1]
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(
        QUALIFY.subprocess, "Popen", lambda *_args, **_kwargs: FakeValidationProcess(running=True)
    )
    monkeypatch.setattr(QUALIFY.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="timed out after 0s"):
        QUALIFY.run_controller_validation(
            argv,
            name=name,
            timeout_s=0,
            cancellation=QUALIFY.CancellationToken(),
            log_path=tmp_path / "validation.log",
        )

    assert calls[-1] == ["docker", "rm", "-f", name]
    assert "timed out" in (tmp_path / "validation.log").read_text(encoding="utf-8")


def test_controller_validation_cleanup_failure_is_terminal(tmp_path, monkeypatch):
    argv = ["docker", "run", "--name", "validation", IMAGE, "true"]
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="permission denied"
        )

    monkeypatch.setattr(
        QUALIFY.subprocess, "Popen", lambda *_args, **_kwargs: FakeValidationProcess()
    )
    monkeypatch.setattr(QUALIFY.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="cleanup exited 1"):
        QUALIFY.run_controller_validation(
            argv,
            name="validation",
            timeout_s=3,
            cleanup_timeout_s=2,
            cancellation=QUALIFY.CancellationToken(),
            log_path=tmp_path / "validation.log",
        )

    assert calls == [["docker", "rm", "-f", "validation"]]
    assert "cleanup_error" in (tmp_path / "validation.log").read_text(encoding="utf-8")


def test_controller_validation_cleanup_timeout_is_terminal(tmp_path, monkeypatch):
    argv = ["docker", "run", "--name", "validation", IMAGE, "true"]

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(
        QUALIFY.subprocess, "Popen", lambda *_args, **_kwargs: FakeValidationProcess()
    )
    monkeypatch.setattr(QUALIFY.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="cleanup timed out after 2s"):
        QUALIFY.run_controller_validation(
            argv,
            name="validation",
            timeout_s=3,
            cleanup_timeout_s=2,
            cancellation=QUALIFY.CancellationToken(),
            log_path=tmp_path / "validation.log",
        )


def test_controller_validation_cancellation_drains_before_return(tmp_path, monkeypatch):
    token = QUALIFY.CancellationToken()
    argv = ["docker", "run", "--name", "validation", IMAGE, "true"]
    process = FakeValidationProcess(running=True, on_poll=token.cancel)
    cleanup = []
    monkeypatch.setattr(QUALIFY.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        QUALIFY.subprocess,
        "run",
        lambda command, **_kwargs: cleanup.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    with pytest.raises(QUALIFY.WorkerCancellationError, match="cancelled"):
        QUALIFY.run_controller_validation(
            argv,
            name="validation",
            timeout_s=3,
            cleanup_timeout_s=2,
            cancellation=token,
            log_path=tmp_path / "validation.log",
        )

    assert cleanup == [["docker", "rm", "-f", "validation"]]
    assert process.returncode is not None


def test_cancel_during_validation_never_adds_or_commits(tmp_path, monkeypatch):
    candidate = QUALIFY.CANDIDATES["0f34e285"]
    token = QUALIFY.CancellationToken()
    git_commands = []
    monkeypatch.setattr(
        QUALIFY,
        "changed_paths",
        lambda _worktree, _cancellation: candidate.required_changes,
    )

    def fake_git(_worktree, _cancellation, *args, **_kwargs):
        git_commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def cancel_validation(*_args, **_kwargs):
        token.cancel()
        raise QUALIFY.WorkerCancellationError("validation cancelled")

    monkeypatch.setattr(QUALIFY, "controller_git", fake_git)
    monkeypatch.setattr(QUALIFY, "run_controller_validation", cancel_validation)

    with pytest.raises(QUALIFY.WorkerCancellationError, match="cancelled"):
        QUALIFY.controller_commit(
            tmp_path,
            candidate,
            IMAGE,
            tmp_path,
            validation_run_id="cancel-before-git-mutation",
            cancellation=token,
        )

    assert any(command[:2] == ("diff", "--check") for command in git_commands)
    assert not any("add" in command or "commit" in command for command in git_commands)


def test_default_cli_is_preflight_only_and_never_executes(monkeypatch, capsys):
    snapshots = {
        item: {
            "id": item,
            "title": "title",
            "description": "description",
            "acceptance_criteria": [],
            "dependencies": [],
        }
        for item in QUALIFY.CANDIDATES
    }
    monkeypatch.setattr(QUALIFY, "load_card_snapshots", lambda _root: snapshots)
    monkeypatch.setattr(
        QUALIFY, "validate_controller_source", lambda _source, _commit: CONTROLLER
    )
    monkeypatch.setattr(
        QUALIFY,
        "execute_all",
        lambda *_args, **_kwargs: pytest.fail("dry preflight started live execution"),
    )

    assert QUALIFY.main(
        ["--image", IMAGE, "--controller-commit", CONTROLLER_COMMIT]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "preflight_only"
    assert result["completion_authority"].startswith("none")
    assert result["board_mutation"] == "forbidden"


def test_execute_cli_returns_nonzero_when_any_card_fails(monkeypatch, capsys):
    snapshots = {
        item: {
            "id": item,
            "title": "title",
            "description": "description",
            "acceptance_criteria": [],
            "dependencies": [],
        }
        for item in QUALIFY.CANDIDATES
    }
    monkeypatch.setattr(QUALIFY, "load_card_snapshots", lambda _root: snapshots)
    monkeypatch.setattr(
        QUALIFY, "validate_controller_source", lambda _source, _commit: CONTROLLER
    )
    monkeypatch.setattr(
        QUALIFY,
        "execute_all",
        lambda *_args, **_kwargs: {
            "mode": "execute",
            "outcomes": [{"card_id": "0f34e285", "disposition": "failed"}],
        },
    )

    assert QUALIFY.main(
        ["--image", IMAGE, "--controller-commit", CONTROLLER_COMMIT, "--execute"]
    ) == 1
    assert json.loads(capsys.readouterr().out)["outcomes"][0]["disposition"] == "failed"


def test_execute_stops_before_next_card_when_post_cleanup_inventory_is_not_empty(
    tmp_path, monkeypatch
):
    candidates = dict(list(QUALIFY.CANDIDATES.items())[:2])
    snapshots = {
        card_id: {
            "id": card_id,
            "title": "title",
            "description": "description",
            "acceptance_criteria": [],
            "dependencies": [],
        }
        for card_id in candidates
    }
    empty = {"resources": [], "resource_count": 0}
    leaked = {
        "resources": [{"type": "container", "id": "leftover-worker"}],
        "resource_count": 1,
    }
    inventories = iter((empty, leaked))
    executed = []
    monkeypatch.setattr(QUALIFY, "CANDIDATES", candidates)
    monkeypatch.setattr(QUALIFY.socket, "gethostname", lambda: QUALIFY.QUALIFIED_HOST)
    monkeypatch.setattr(
        QUALIFY, "validate_controller_source", lambda _source, _commit: CONTROLLER
    )
    monkeypatch.setattr(
        QUALIFY,
        "validate_local_image",
        lambda _image: {"repo_digest": IMAGE, "labels": {}},
    )
    monkeypatch.setattr(
        QUALIFY, "managed_docker_inventory", lambda: next(inventories)
    )
    monkeypatch.setattr(
        QUALIFY,
        "execute_candidate",
        lambda candidate, *_args, **_kwargs: executed.append(candidate.card_id)
        or {"card_id": candidate.card_id, "disposition": "review_required"},
    )
    args = SimpleNamespace(
        image=IMAGE,
        controller_commit=CONTROLLER_COMMIT,
        source=tmp_path / "source",
        evidence_root=tmp_path / "evidence",
        worktree_root=tmp_path / "worktrees",
    )

    with pytest.raises(QUALIFY.QualificationQuiescenceError, match="not proven"):
        QUALIFY.execute_all(args, snapshots, CONTROLLER)

    assert executed == [next(iter(candidates))]
    failure = json.loads(
        (args.evidence_root / f"{next(iter(candidates))}-failure.json").read_text()
    )
    assert failure["disposition"] == "failed"
    assert failure["next_card_admitted"] is False
    assert not (args.evidence_root / "qualification-summary.json").exists()
    assert not (args.evidence_root / "qualification-bundle-digest.json").exists()


def test_post_cleanup_inventory_exception_never_seals_or_starts_next_card(
    tmp_path, monkeypatch
):
    candidates = dict(list(QUALIFY.CANDIDATES.items())[:2])
    snapshots = {card_id: {"id": card_id} for card_id in candidates}
    first_card = next(iter(candidates))
    executed = []
    inventories = iter(
        (
            {"resources": [], "resource_count": 0},
            RuntimeError("docker inventory unavailable"),
        )
    )

    def inventory():
        result = next(inventories)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(QUALIFY, "CANDIDATES", candidates)
    monkeypatch.setattr(QUALIFY.socket, "gethostname", lambda: QUALIFY.QUALIFIED_HOST)
    monkeypatch.setattr(
        QUALIFY, "validate_controller_source", lambda _source, _commit: CONTROLLER
    )
    monkeypatch.setattr(
        QUALIFY, "validate_local_image", lambda _image: {"repo_digest": IMAGE}
    )
    monkeypatch.setattr(QUALIFY, "managed_docker_inventory", inventory)
    monkeypatch.setattr(
        QUALIFY,
        "execute_candidate",
        lambda candidate, *_args, **_kwargs: executed.append(candidate.card_id)
        or {"card_id": candidate.card_id, "disposition": "review_required"},
    )
    args = SimpleNamespace(
        image=IMAGE,
        controller_commit=CONTROLLER_COMMIT,
        source=tmp_path / "source",
        evidence_root=tmp_path / "evidence",
        worktree_root=tmp_path / "worktrees",
    )

    with pytest.raises(QUALIFY.QualificationQuiescenceError, match="not proven"):
        QUALIFY.execute_all(args, snapshots, CONTROLLER)

    assert executed == [first_card]
    failure = json.loads(
        (args.evidence_root / f"{first_card}-failure.json").read_text()
    )
    assert failure["next_card_admitted"] is False
    assert "docker inventory unavailable" in failure["error"]
    assert not (args.evidence_root / "qualification-summary.json").exists()
    assert not (args.evidence_root / "qualification-bundle-digest.json").exists()


def test_execute_stops_before_next_card_after_lifecycle_terminal(tmp_path, monkeypatch):
    candidates = dict(list(QUALIFY.CANDIDATES.items())[:2])
    snapshots = {card_id: {"id": card_id} for card_id in candidates}
    executed = []
    monkeypatch.setattr(QUALIFY, "CANDIDATES", candidates)
    monkeypatch.setattr(QUALIFY.socket, "gethostname", lambda: QUALIFY.QUALIFIED_HOST)
    monkeypatch.setattr(
        QUALIFY, "validate_controller_source", lambda _source, _commit: CONTROLLER
    )
    monkeypatch.setattr(
        QUALIFY, "validate_local_image", lambda _image: {"repo_digest": IMAGE}
    )
    monkeypatch.setattr(
        QUALIFY,
        "managed_docker_inventory",
        lambda: {"resources": [], "resource_count": 0},
    )
    monkeypatch.setattr(
        QUALIFY,
        "execute_candidate",
        lambda candidate, *_args, **_kwargs: executed.append(candidate.card_id)
        or {
            "card_id": candidate.card_id,
            "disposition": "failed",
            "lifecycle_terminal": True,
            "next_card_admitted": False,
        },
    )
    args = SimpleNamespace(
        image=IMAGE,
        controller_commit=CONTROLLER_COMMIT,
        source=tmp_path / "source",
        evidence_root=tmp_path / "evidence",
        worktree_root=tmp_path / "worktrees",
    )

    summary = QUALIFY.execute_all(args, snapshots, CONTROLLER)

    assert executed == [next(iter(candidates))]
    assert summary["outcomes"][0]["next_card_admitted"] is False


def test_unproven_runtime_quiescence_never_starts_next_card_or_seals_bundle(
    tmp_path, monkeypatch
):
    candidates = dict(list(QUALIFY.CANDIDATES.items())[:2])
    snapshots = {card_id: {"id": card_id} for card_id in candidates}
    executed = []
    monkeypatch.setattr(QUALIFY, "CANDIDATES", candidates)
    monkeypatch.setattr(QUALIFY.socket, "gethostname", lambda: QUALIFY.QUALIFIED_HOST)
    monkeypatch.setattr(
        QUALIFY, "validate_controller_source", lambda _source, _commit: CONTROLLER
    )
    monkeypatch.setattr(
        QUALIFY, "validate_local_image", lambda _image: {"repo_digest": IMAGE}
    )
    monkeypatch.setattr(
        QUALIFY,
        "managed_docker_inventory",
        lambda: {"resources": [], "resource_count": 0},
    )

    def fail_quiescence(candidate, *_args, **_kwargs):
        executed.append(candidate.card_id)
        raise QUALIFY.QualificationQuiescenceError("post-run controller still active")

    monkeypatch.setattr(QUALIFY, "execute_candidate", fail_quiescence)
    args = SimpleNamespace(
        image=IMAGE,
        controller_commit=CONTROLLER_COMMIT,
        source=tmp_path / "source",
        evidence_root=tmp_path / "evidence",
        worktree_root=tmp_path / "worktrees",
    )

    with pytest.raises(QUALIFY.QualificationQuiescenceError):
        QUALIFY.execute_all(args, snapshots, CONTROLLER)

    assert executed == [next(iter(candidates))]
    assert not (args.evidence_root / "qualification-summary.json").exists()
    assert not (args.evidence_root / "qualification-bundle-digest.json").exists()


def test_bundle_digest_binds_every_preexisting_evidence_file(tmp_path):
    (tmp_path / "manifest.json").write_text("{}\n", encoding="utf-8")
    nested = tmp_path / "card" / "run.json"
    nested.parent.mkdir()
    nested.write_text('{"state":"failed"}\n', encoding="utf-8")

    first = QUALIFY.write_bundle_digest(tmp_path)
    nested.write_text('{"state":"blocked"}\n', encoding="utf-8")
    second = QUALIFY.write_bundle_digest(tmp_path)

    assert first["bundle_digest"] != second["bundle_digest"]
    assert {item["path"] for item in second["files"]} == {
        "card/run.json", "manifest.json"
    }
