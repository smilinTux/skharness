"""Fail-closed lifecycle ownership and orphan reconciliation for Docker sandboxes."""

from __future__ import annotations

import json
import re
import secrets
import subprocess
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable, Iterable

MANAGED_LABEL = "io.skharness.managed"
RUN_ID_LABEL = "io.skharness.run-id"
RESOURCE_ROLE_LABEL = "io.skharness.resource-role"
OWNERSHIP_AUTHORITY_LABEL = "io.skharness.ownership-authority"
SCHEMA_LABEL = "io.skharness.lifecycle-schema"
LIFECYCLE_SCHEMA = "1"

RESOURCE_ROLES = frozenset({"worker", "proxy", "network"})
OWNERSHIP_AUTHORITIES = frozenset({"ephemeral", "lease"})
MAX_INVENTORY = 512
MAX_ERRORS = 8
MAX_ERROR_LENGTH = 180
_RUN_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")

DockerRun = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class SandboxOwnership:
    """One immutable identity shared by every Docker resource in a sandbox run."""

    run_id: str
    authority: str = "ephemeral"

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not _RUN_ID.fullmatch(self.run_id):
            raise ValueError("sandbox run id must be a safe 1-128 character label token")
        if self.authority not in OWNERSHIP_AUTHORITIES:
            raise ValueError("sandbox ownership authority must be ephemeral or lease")

    @classmethod
    def create(cls) -> "SandboxOwnership":
        """Mint a non-secret, collision-resistant run identity."""
        return cls(f"run-{secrets.token_hex(16)}")

    def labels(self, role: str) -> dict[str, str]:
        """Return the complete immutable Docker-label set for one resource."""
        if role not in RESOURCE_ROLES:
            raise ValueError(f"unknown sandbox resource role: {role!r}")
        return {
            MANAGED_LABEL: "true",
            RUN_ID_LABEL: self.run_id,
            RESOURCE_ROLE_LABEL: role,
            OWNERSHIP_AUTHORITY_LABEL: self.authority,
            SCHEMA_LABEL: LIFECYCLE_SCHEMA,
        }

    def docker_args(self, role: str) -> list[str]:
        """Render deterministic ``--label key=value`` Docker arguments."""
        args: list[str] = []
        for key, value in sorted(self.labels(role).items()):
            args.extend(("--label", f"{key}={value}"))
        return args


@dataclass(frozen=True)
class ReconciliationStatus:
    """Bounded operational result from one orphan-reconciliation pass."""

    outcome: str
    last_reconciled_at: float
    scanned_runs: int = 0
    scanned_resources: int = 0
    orphan_runs: int = 0
    orphan_resources: int = 0
    removed_containers: int = 0
    removed_networks: int = 0
    preserved_active_runs: int = 0
    preserved_young_runs: int = 0
    preserved_unproven_runs: int = 0
    inventory_truncated: bool = False
    errors: tuple[str, ...] = ()

    @classmethod
    def never(cls) -> "ReconciliationStatus":
        """Return the bounded status before the first pass."""
        return cls(outcome="never", last_reconciled_at=0.0)

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-compatible, bounded status mapping."""
        value = asdict(self)
        value["errors"] = list(self.errors)
        return value


@dataclass(frozen=True)
class _Resource:
    resource_id: str
    run_id: str
    role: str
    authority: str
    created_at: float | None
    running: bool = False
    endpoints: frozenset[str] = frozenset()


def _bounded_errors(errors: Iterable[str]) -> tuple[str, ...]:
    return tuple(str(error)[:MAX_ERROR_LENGTH] for error in list(errors)[:MAX_ERRORS])


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.timestamp()
    except ValueError:
        return None


def _checked(run: DockerRun, argv: list[str]) -> tuple[str | None, str | None]:
    try:
        result = run(argv, capture_output=True, text=True)
    except Exception as exc:  # noqa: BLE001 - reconciliation reports, never crashes admission
        return None, f"{' '.join(argv[:3])}: {type(exc).__name__}: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "docker command failed").strip()
        return None, f"{' '.join(argv[:3])}: {detail}"
    return result.stdout or "", None


def _owned_labels(
    labels: object, expected_role: str
) -> tuple[str | None, str | None, str | None]:
    if not isinstance(labels, dict) or labels.get(MANAGED_LABEL) != "true":
        return None, None, "managed resource is missing its ownership label"
    run_id = labels.get(RUN_ID_LABEL)
    role = labels.get(RESOURCE_ROLE_LABEL)
    authority = labels.get(OWNERSHIP_AUTHORITY_LABEL)
    if labels.get(SCHEMA_LABEL) != LIFECYCLE_SCHEMA:
        return None, None, "managed resource has an unknown lifecycle schema"
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        return None, None, "managed resource has an invalid run identity"
    if authority not in OWNERSHIP_AUTHORITIES:
        return run_id, None, "managed resource has an invalid ownership authority"
    if role != expected_role:
        return (
            run_id,
            str(authority),
            f"managed resource role {role!r} does not match {expected_role!r}",
        )
    return run_id, str(authority), None


def _parse_containers(payload: str) -> tuple[list[_Resource], list[str]]:
    resources: list[_Resource] = []
    errors: list[str] = []
    try:
        values = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        return [], [f"container inventory is not JSON: {exc}"]
    if not isinstance(values, list):
        return [], ["container inventory is not a list"]
    for value in values:
        if not isinstance(value, dict):
            errors.append("container inventory contains a non-object")
            continue
        labels = (value.get("Config") or {}).get("Labels")
        role = labels.get(RESOURCE_ROLE_LABEL) if isinstance(labels, dict) else None
        run_id, authority, label_error = _owned_labels(labels, str(role))
        if label_error or role not in {"worker", "proxy"}:
            errors.append(label_error or f"managed container has invalid role {role!r}")
        if run_id is None or authority is None or role not in {"worker", "proxy"}:
            continue
        resource_id = value.get("Id")
        if not isinstance(resource_id, str) or not resource_id:
            errors.append(f"managed {role} has no immutable Docker id")
            continue
        resources.append(
            _Resource(
                resource_id=resource_id,
                run_id=run_id,
                role=role,
                authority=authority,
                created_at=_timestamp(value.get("Created")),
                running=bool((value.get("State") or {}).get("Running")),
            )
        )
    return resources, errors


def _parse_networks(payload: str) -> tuple[list[_Resource], list[str]]:
    resources: list[_Resource] = []
    errors: list[str] = []
    try:
        values = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        return [], [f"network inventory is not JSON: {exc}"]
    if not isinstance(values, list):
        return [], ["network inventory is not a list"]
    for value in values:
        if not isinstance(value, dict):
            errors.append("network inventory contains a non-object")
            continue
        run_id, authority, label_error = _owned_labels(value.get("Labels"), "network")
        if label_error:
            errors.append(label_error)
        if run_id is None or authority is None:
            continue
        resource_id = value.get("Id")
        if not isinstance(resource_id, str) or not resource_id:
            errors.append("managed network has no immutable Docker id")
            continue
        endpoints = value.get("Containers") or {}
        if not isinstance(endpoints, dict):
            errors.append(f"managed network {resource_id[:12]} has malformed endpoints")
            endpoints = {"unproven": {}}
        resources.append(
            _Resource(
                resource_id=resource_id,
                run_id=run_id,
                role="network",
                authority=authority,
                created_at=_timestamp(value.get("Created")),
                endpoints=frozenset(str(item) for item in endpoints),
            )
        )
    return resources, errors


def _status_error(now: float, errors: Iterable[str]) -> ReconciliationStatus:
    return ReconciliationStatus(
        outcome="error",
        last_reconciled_at=round(now, 3),
        errors=_bounded_errors(errors),
    )


def reconcile_sandbox_orphans(
    *,
    docker: str,
    run: DockerRun,
    now: float,
    orphan_grace_s: float,
    active_run_ids: Iterable[str] = (),
    active_lease_ids_authoritative: bool = False,
) -> ReconciliationStatus:
    """Remove only old, fully proven orphan groups carrying SKHarness labels.

    A running worker or caller-declared active run preserves its entire group.
    Young resources, unknown timestamps, malformed ownership, and foreign network
    attachments fail closed and remain for inspection. Inventory is capped before
    inspection so an unexpected Docker population cannot create an unbounded pass.
    """
    if orphan_grace_s < 0:
        raise ValueError("orphan_grace_s must be non-negative")
    active = frozenset(str(item) for item in active_run_ids)
    errors: list[str] = []
    container_ids_raw, error = _checked(
        run,
        [
            docker,
            "ps",
            "-a",
            "--filter",
            f"label={MANAGED_LABEL}=true",
            "--format",
            "{{.ID}}",
        ],
    )
    if error:
        return _status_error(now, [error])
    network_ids_raw, error = _checked(
        run,
        [
            docker,
            "network",
            "ls",
            "--filter",
            f"label={MANAGED_LABEL}=true",
            "--format",
            "{{.ID}}",
        ],
    )
    if error:
        return _status_error(now, [error])
    container_ids = [item for item in (container_ids_raw or "").splitlines() if item]
    network_ids = [item for item in (network_ids_raw or "").splitlines() if item]
    if len(container_ids) > MAX_INVENTORY or len(network_ids) > MAX_INVENTORY:
        return ReconciliationStatus(
            outcome="inventory_truncated",
            last_reconciled_at=round(now, 3),
            scanned_resources=min(len(container_ids), MAX_INVENTORY)
            + min(len(network_ids), MAX_INVENTORY),
            inventory_truncated=True,
            errors=("managed Docker inventory exceeds the safe reconciliation cap",),
        )

    containers: list[_Resource] = []
    networks: list[_Resource] = []
    if container_ids:
        payload, error = _checked(run, [docker, "inspect", *container_ids])
        if error:
            return _status_error(now, [error])
        containers, parse_errors = _parse_containers(payload or "")
        errors.extend(parse_errors)
    if network_ids:
        payload, error = _checked(run, [docker, "network", "inspect", *network_ids])
        if error:
            return _status_error(now, [error])
        networks, parse_errors = _parse_networks(payload or "")
        errors.extend(parse_errors)

    grouped: dict[str, list[_Resource]] = defaultdict(list)
    for resource in (*containers, *networks):
        grouped[resource.run_id].append(resource)

    orphan_groups: list[list[_Resource]] = []
    preserved_active = 0
    preserved_young = 0
    preserved_unproven = 0
    for run_id, resources in sorted(grouped.items()):
        workers = [item for item in resources if item.role == "worker"]
        authorities = {item.authority for item in resources}
        if len(authorities) != 1:
            preserved_unproven += 1
            errors.append(f"run {run_id[:32]} has conflicting ownership authorities")
            continue
        authority = next(iter(authorities))
        if run_id in active:
            preserved_active += 1
            continue
        if authority == "lease":
            if not active_lease_ids_authoritative:
                preserved_unproven += 1
                errors.append(f"run {run_id[:32]} requires authoritative lease inventory")
                continue
        elif any(item.running for item in workers):
            preserved_active += 1
            continue
        if any(item.created_at is None for item in resources):
            preserved_unproven += 1
            errors.append(f"run {run_id[:32]} has an unparseable creation timestamp")
            continue
        group_container_ids = {
            item.resource_id for item in resources if item.role in {"worker", "proxy"}
        }
        foreign = {
            endpoint
            for item in resources
            if item.role == "network"
            for endpoint in item.endpoints
            if endpoint not in group_container_ids
        }
        if foreign:
            preserved_unproven += 1
            errors.append(f"run {run_id[:32]} network has a foreign attachment")
            continue
        newest_created = max(item.created_at or now for item in resources)
        if now - newest_created < orphan_grace_s:
            preserved_young += 1
            continue
        orphan_groups.append(resources)

    removed_containers = 0
    removed_networks = 0
    orphan_resources = sum(len(group) for group in orphan_groups)
    for resources in orphan_groups:
        for item in sorted(resources, key=lambda value: value.resource_id):
            if item.role == "network":
                continue
            _, error = _checked(run, [docker, "rm", "-f", item.resource_id])
            if error:
                errors.append(error)
            else:
                removed_containers += 1
        for item in sorted(resources, key=lambda value: value.resource_id):
            if item.role != "network":
                continue
            _, error = _checked(run, [docker, "network", "rm", item.resource_id])
            if error:
                errors.append(error)
            else:
                removed_networks += 1

    return ReconciliationStatus(
        outcome="partial" if errors else "ok",
        last_reconciled_at=round(now, 3),
        scanned_runs=len(grouped),
        scanned_resources=len(container_ids) + len(network_ids),
        orphan_runs=len(orphan_groups),
        orphan_resources=orphan_resources,
        removed_containers=removed_containers,
        removed_networks=removed_networks,
        preserved_active_runs=preserved_active,
        preserved_young_runs=preserved_young,
        preserved_unproven_runs=preserved_unproven,
        errors=_bounded_errors(errors),
    )
