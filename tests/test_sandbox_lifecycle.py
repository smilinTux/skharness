import json
import subprocess

from skharness.autocode.sandbox import LaunchSpec, Sandbox
from skharness.autocode.sandbox_lifecycle import (
    OWNERSHIP_AUTHORITY_LABEL,
    RESOURCE_ROLE_LABEL,
    RUN_ID_LABEL,
    SandboxOwnership,
    reconcile_sandbox_orphans,
)


NOW = 2_000_000_000.0
OLD = "2020-01-01T00:00:00Z"
YOUNG = "2033-05-18T03:32:50Z"


def _completed(argv, *, stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


def _labels(run_id: str, role: str, authority: str = "ephemeral") -> dict[str, str]:
    return SandboxOwnership(run_id, authority=authority).labels(role)


def _container(
    cid: str,
    run_id: str,
    role: str,
    *,
    running=False,
    created=OLD,
    authority="ephemeral",
):
    return {
        "Id": cid,
        "Name": f"/{role}-{cid}",
        "Created": created,
        "Config": {"Labels": _labels(run_id, role, authority)},
        "State": {"Running": running},
    }


def _network(nid: str, run_id: str, endpoints=(), *, created=OLD, authority="ephemeral"):
    return {
        "Id": nid,
        "Name": f"network-{nid}",
        "Created": created,
        "Labels": _labels(run_id, "network", authority),
        "Containers": {cid: {} for cid in endpoints},
    }


class FakeDocker:
    def __init__(self, containers=(), networks=()):
        self.containers = {item["Id"]: item for item in containers}
        self.networks = {item["Id"]: item for item in networks}
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if argv[1:3] == ["ps", "-a"]:
            return _completed(argv, stdout="\n".join(self.containers))
        if argv[1:3] == ["network", "ls"]:
            return _completed(argv, stdout="\n".join(self.networks))
        if argv[1] == "inspect":
            values = [self.containers[cid] for cid in argv[2:]]
            return _completed(argv, stdout=json.dumps(values))
        if argv[1:3] == ["network", "inspect"]:
            values = [self.networks[nid] for nid in argv[3:]]
            return _completed(argv, stdout=json.dumps(values))
        if argv[1:3] == ["rm", "-f"]:
            self.containers.pop(argv[3], None)
            return _completed(argv)
        if argv[1:3] == ["network", "rm"]:
            self.networks.pop(argv[3], None)
            return _completed(argv)
        raise AssertionError(f"unexpected docker call: {argv}")


def _reconcile(fake: FakeDocker, **kwargs):
    return reconcile_sandbox_orphans(
        docker="docker",
        run=fake,
        now=NOW,
        orphan_grace_s=300,
        **kwargs,
    )


def test_crash_restart_reclaims_old_stopped_worker_proxy_and_network():
    fake = FakeDocker(
        containers=[
            _container("worker", "run-a", "worker"),
            _container("proxy", "run-a", "proxy", running=True),
        ],
        networks=[_network("network", "run-a", ("worker", "proxy"))],
    )

    status = _reconcile(fake)

    assert fake.containers == {}
    assert fake.networks == {}
    assert status.outcome == "ok"
    assert status.orphan_runs == 1
    assert status.removed_containers == 2
    assert status.removed_networks == 1


def test_controller_killed_before_worker_launch_reclaims_old_proxy_and_network():
    fake = FakeDocker(
        containers=[_container("proxy", "run-a", "proxy", running=True)],
        networks=[_network("network", "run-a", ("proxy",))],
    )

    status = _reconcile(fake)

    assert fake.containers == {}
    assert fake.networks == {}
    assert status.orphan_runs == 1


def test_running_worker_preserves_the_entire_active_run():
    fake = FakeDocker(
        containers=[
            _container("worker", "run-live", "worker", running=True),
            _container("proxy", "run-live", "proxy", running=True),
        ],
        networks=[_network("network", "run-live", ("worker", "proxy"))],
    )

    status = _reconcile(fake)

    assert set(fake.containers) == {"worker", "proxy"}
    assert set(fake.networks) == {"network"}
    assert status.preserved_active_runs == 1
    assert not any(call[1] == "rm" for call in fake.calls)


def test_restart_reclaims_running_worker_when_authoritative_lease_is_gone():
    fake = FakeDocker(
        containers=[
            _container("worker", "lease-gone", "worker", running=True, authority="lease"),
            _container("proxy", "lease-gone", "proxy", running=True, authority="lease"),
        ],
        networks=[
            _network(
                "network",
                "lease-gone",
                ("worker", "proxy"),
                authority="lease",
            )
        ],
    )

    status = _reconcile(fake, active_lease_ids_authoritative=True)

    assert status.orphan_runs == 1
    assert fake.containers == {}
    assert fake.networks == {}


def test_lease_resources_fail_closed_without_authoritative_lease_inventory():
    fake = FakeDocker(
        containers=[_container("worker", "lease-unknown", "worker", authority="lease")]
    )

    status = _reconcile(fake)

    assert status.preserved_unproven_runs == 1
    assert set(fake.containers) == {"worker"}


def test_active_lease_id_preserves_a_run_without_a_worker():
    fake = FakeDocker(
        containers=[
            _container("proxy", "leased-run", "proxy", running=True, authority="lease")
        ],
        networks=[_network("network", "leased-run", ("proxy",), authority="lease")],
    )

    status = _reconcile(fake, active_run_ids={"leased-run"})

    assert status.preserved_active_runs == 1
    assert set(fake.containers) == {"proxy"}
    assert set(fake.networks) == {"network"}


def test_young_bootstrap_and_foreign_network_attachment_fail_closed():
    fake = FakeDocker(
        containers=[
            _container("young-proxy", "young-run", "proxy", created=YOUNG),
            _container("old-proxy", "foreign-run", "proxy"),
        ],
        networks=[
            _network("young-net", "young-run", ("young-proxy",), created=YOUNG),
            _network("foreign-net", "foreign-run", ("old-proxy", "not-managed")),
        ],
    )

    status = _reconcile(fake)

    assert status.preserved_young_runs == 1
    assert status.preserved_unproven_runs == 1
    assert len(fake.containers) == 2
    assert len(fake.networks) == 2


def test_malformed_owned_event_is_reported_and_never_deleted():
    broken = _container("proxy", "run-a", "proxy")
    broken["Created"] = "not-a-timestamp"
    fake = FakeDocker(containers=[broken])

    status = _reconcile(fake)

    assert status.preserved_unproven_runs == 1
    assert set(fake.containers) == {"proxy"}
    assert status.errors


def test_timezone_less_creation_timestamp_is_unproven_and_preserved():
    fake = FakeDocker(
        containers=[_container("proxy", "run-a", "proxy", created="2020-01-01T00:00:00")]
    )

    status = _reconcile(fake)

    assert status.preserved_unproven_runs == 1
    assert set(fake.containers) == {"proxy"}


def test_reconciliation_status_and_errors_are_bounded():
    fake = FakeDocker(
        containers=[_container(f"proxy-{i}", f"run-{i}", "proxy") for i in range(20)]
    )

    def failing_remove(argv, **kwargs):
        result = fake(argv, **kwargs)
        if argv[1:3] == ["rm", "-f"]:
            return _completed(argv, returncode=1, stderr="x" * 500)
        return result

    status = reconcile_sandbox_orphans(
        docker="docker",
        run=failing_remove,
        now=NOW,
        orphan_grace_s=300,
    )

    assert status.outcome == "partial"
    assert len(status.errors) == 8
    assert all(len(error) <= 180 for error in status.errors)
    assert status.as_dict()["orphan_runs"] == 20


def test_oversized_managed_inventory_fails_closed_before_inspection_or_deletion():
    fake = FakeDocker(
        containers=[_container(f"proxy-{i}", f"run-{i}", "proxy") for i in range(513)]
    )

    status = _reconcile(fake)

    assert status.outcome == "inventory_truncated"
    assert status.inventory_truncated is True
    assert status.scanned_resources == 512
    assert not any(call[1] in {"inspect", "rm"} for call in fake.calls)


def test_sandbox_admission_reconciliation_is_startup_then_rate_limited(monkeypatch):
    monotonic = iter((10.0, 20.0, 80.0))
    sandbox = Sandbox(
        live_execution=True,
        reconcile_interval_s=60,
        monotonic=lambda: next(monotonic),
    )
    calls = []
    monkeypatch.setattr(
        sandbox,
        "reconcile_orphans",
        lambda **_kwargs: calls.append("reconciled") or {"outcome": "ok"},
    )

    sandbox.maybe_reconcile_orphans()
    sandbox.maybe_reconcile_orphans()
    sandbox.maybe_reconcile_orphans()

    assert calls == ["reconciled", "reconciled"]


def test_public_operational_status_exposes_bounded_last_outcome(monkeypatch):
    fake = FakeDocker(
        containers=[_container("proxy", "run-a", "proxy")],
        networks=[_network("network", "run-a", ("proxy",))],
    )
    sandbox = Sandbox(clock=lambda: NOW, orphan_grace_s=300)
    monkeypatch.setattr("skharness.autocode.sandbox.subprocess.run", fake)

    observed = sandbox.reconcile_orphans()

    assert observed == sandbox.reconciliation_status()
    assert observed["last_reconciled_at"] == NOW
    assert observed["outcome"] == "ok"
    assert observed["orphan_runs"] == 1
    assert observed["orphan_resources"] == 2
    assert observed["errors"] == []


def test_reconciliation_docker_timeout_is_bounded_and_fails_closed(monkeypatch):
    timeouts = []

    def hangs(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    sandbox = Sandbox(
        docker_command_timeout_s=3,
        reconcile_timeout_s=3,
        monotonic=lambda: 10,
        clock=lambda: NOW,
    )
    monkeypatch.setattr("skharness.autocode.sandbox.subprocess.run", hangs)

    observed = sandbox.reconcile_orphans()

    assert timeouts == [3]
    assert observed["outcome"] == "error"
    assert observed["removed_containers"] == 0
    assert observed["removed_networks"] == 0
    assert "TimeoutExpired" in observed["errors"][0]


def test_ownership_is_frozen_and_role_labels_share_one_run_id():
    ownership = SandboxOwnership("run-immutable")

    worker = ownership.labels("worker")
    proxy = ownership.labels("proxy")
    network = ownership.labels("network")

    assert {worker[RUN_ID_LABEL], proxy[RUN_ID_LABEL], network[RUN_ID_LABEL]} == {
        "run-immutable"
    }
    assert worker[RESOURCE_ROLE_LABEL] == "worker"
    assert proxy[RESOURCE_ROLE_LABEL] == "proxy"
    assert network[RESOURCE_ROLE_LABEL] == "network"
    assert {
        worker[OWNERSHIP_AUTHORITY_LABEL],
        proxy[OWNERSHIP_AUTHORITY_LABEL],
        network[OWNERSHIP_AUTHORITY_LABEL],
    } == {"ephemeral"}


def test_all_launch_commands_bind_the_same_immutable_run_identity():
    sandbox = Sandbox()
    ownership = SandboxOwnership("run-launch")
    spec = LaunchSpec("pi", ["pi"], "pi-image", "/tmp/worktree")

    commands = {
        "network": sandbox._network_create_argv("network", ownership),
        "proxy": sandbox._proxy_run_argv(
            name="proxy",
            network="network",
            alias="proxy",
            allow=[],
            ownership=ownership,
        ),
        "worker": sandbox._docker_run_argv(
            spec,
            "network",
            "proxy",
            container_name="worker",
            ownership=ownership,
        ),
    }

    for role, command in commands.items():
        labels = {
            value.split("=", 1)[0]: value.split("=", 1)[1]
            for index, value in enumerate(command)
            if index and command[index - 1] == "--label"
        }
        assert labels[RUN_ID_LABEL] == "run-launch"
        assert labels[RESOURCE_ROLE_LABEL] == role
