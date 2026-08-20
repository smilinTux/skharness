from __future__ import annotations

import math
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from skharness.arena import ArenaStore, Experiment, ExperimentState
from skharness.arena.metrics import MetricDirection, MetricObjective
from skharness.arena.models import (
    BudgetSpec,
    Measurement,
    Observation,
    Provenance,
    Result,
    VerificationState,
)
from skharness.arena.operations import ArenaJobService
from skharness.arena.scheduler import AttemptRequest, LeaseScheduler, ResourceRequest
from skharness.arena.status import ArenaStatusService, BoundedArenaMetrics, ProbeResult
from skharness.daemon import build_daemon_app
from skharness.harness import FakeHarness

NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def _experiment(identifier: str, parent: str | None = None) -> Experiment:
    return Experiment(
        id=identifier,
        parent_id=parent,
        changed_dimensions=("batch",) if parent else (),
        challenge_hash="sha256:challenge",
        actor="agent:test",
        harness="pi",
        card_id="card-test",
        run_id=f"run-{identifier}",
        repository_url="https://example.invalid/repo.git",
        repository_base_sha="a" * 40,
        image_digest="sha256:image",
        sbom_digest="sha256:sbom",
        requested_route="build",
        requested_model="qwen",
        configuration={},
        budgets=BudgetSpec(wall_seconds=10),
        created_at=NOW,
    )


def _result(identifier: str, throughput: float, latency: float, *, valid=True) -> Result:
    def measurement(name, unit, value):
        return Measurement(
            metric=name,
            unit=unit,
            observations=(Observation(value=value, recorded_at=NOW),),
            mean=value,
            standard_deviation=0,
        )

    return Result(
        experiment_id=identifier,
        experiment_hash=f"sha256:{identifier}",
        challenge_hash="sha256:challenge",
        verification=VerificationState.VALID if valid else VerificationState.INVALID,
        measurements=(
            measurement("throughput", "tps", throughput),
            measurement("latency", "ms", latency),
        ),
        created_at=NOW,
    )


def _service(tmp_path, **updates):
    values = dict(
        store=ArenaStore(tmp_path),
        scheduler=LeaseScheduler(ResourceRequest(cpu=4, ram_gb=8)),
        gateway_probe=lambda: True,
        verifier_probe=lambda: ProbeResult(True, "capacity available"),
        gpu_probe=lambda: True,
        serving_backend_probe=lambda: True,
        require_gpu=True,
    )
    values.update(updates)
    return ArenaStatusService(**values)


def _client(service):
    return TestClient(
        build_daemon_app(
            harness=FakeHarness(sessions=[], events={}),
            verify_caller=lambda token: token == "good",
            arena_status=service,
        )
    )


def test_liveness_is_process_only_and_required_unknown_gpu_fails_readiness(tmp_path):
    service = _service(tmp_path, gpu_probe=lambda: None)
    client = _client(service)

    assert client.get("/livez").json() == {"live": True, "component": "skharness-arena"}
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["dependencies"]["gpu"]["state"] == "unknown"
    assert response.json()["dependencies"]["gpu"]["required"] is True


def test_gpu_worker_is_unready_when_serving_backend_is_unknown(tmp_path):
    response = _client(_service(tmp_path, serving_backend_probe=lambda: None)).get("/readyz")
    assert response.status_code == 503
    backend = response.json()["dependencies"]["serving_backend"]
    assert backend == {"state": "unknown", "required": True, "detail": ""}


def test_ready_only_when_all_required_dependencies_are_observed_healthy(tmp_path):
    client = _client(_service(tmp_path))
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["ready"] is True


def test_probe_exception_becomes_honest_error_not_false_success_or_500(tmp_path):
    def broken():
        raise RuntimeError("gateway unavailable")

    response = _client(_service(tmp_path, gateway_probe=broken)).get("/readyz")
    assert response.status_code == 503
    signal = response.json()["dependencies"]["skgateway"]
    assert signal["state"] == "error"
    assert "gateway unavailable" in signal["detail"]


def test_arena_queries_are_read_scoped_and_lineage_is_structured(tmp_path):
    experiments = (_experiment("root"), _experiment("child", "root"))
    client = _client(_service(tmp_path, experiments=lambda: experiments))
    assert client.get("/api/v1/arena/status").status_code == 401
    assert client.get("/api/v1/arena/lineage/child").status_code == 401

    headers = {"authorization": "Bearer good"}
    row = client.get("/api/v1/arena/lineage/child", headers=headers)
    assert row.status_code == 200
    assert row.json()["ancestors"] == ["root"]
    assert client.get("/api/v1/arena/lineage/missing", headers=headers).status_code == 404


def test_frontier_uses_only_verified_results_and_multiple_objectives(tmp_path):
    results = (
        _result("balanced", 100, 10),
        _result("fast", 120, 15),
        _result("dominated", 90, 20),
        _result("unverified-fast", 1000, 1, valid=False),
    )
    client = _client(_service(tmp_path, results=lambda: results))
    response = client.get(
        "/api/v1/arena/frontier",
        params={
            "challenge_hash": "sha256:challenge",
            "objectives": "throughput:maximize,latency:minimize",
        },
        headers={"authorization": "Bearer good"},
    )
    assert response.status_code == 200
    assert [row["experiment_id"] for row in response.json()["frontier"]] == ["balanced", "fast"]


def test_attempt_view_keeps_identity_in_json_not_metric_labels(tmp_path):
    service = _service(tmp_path)
    controller_event = service.store.append_event(
        __import__("skharness.arena.models", fromlist=["ExperimentEvent"]).ExperimentEvent(
            event_id="evt",
            writer_id="worker",
            sequence=1,
            experiment_id="secret-exp-id",
            to_state=ExperimentState.PROPOSED,
            timestamp=NOW,
            provenance=Provenance(
                actor="a", node="n", session_id="s", action="propose", target="secret-exp-id"
            ),
            payload={"challenge_id": "tiny"},
        )
    )
    client = _client(service)
    headers = {"authorization": "Bearer good"}
    attempts = client.get("/api/v1/arena/attempts", headers=headers).json()["attempts"]
    assert attempts[0]["experiment_id"] == "secret-exp-id"

    service.metrics.transition("proposed")
    text = client.get("/api/v1/arena/metrics", headers=headers).text
    assert 'state="proposed"' in text
    assert "secret-exp-id" not in text
    assert controller_event.event_hash not in text


def test_metric_registry_rejects_unbounded_labels():
    registry = BoundedArenaMetrics()
    with pytest.raises(ValueError, match="unsupported state label"):
        registry.transition("experiment-123")
    with pytest.raises(ValueError, match="unsupported verdict label"):
        registry.verification("agent-provided-value")
    with pytest.raises(ValueError, match="unsupported signal label"):
        registry.add("experiment-123")
    with pytest.raises(ValueError, match="finite and non-negative"):
        registry.add("tokens", math.inf)


def test_status_counts_latest_attempt_state_and_scheduler_without_ids(tmp_path):
    service = _service(tmp_path)
    assert service.status()["attempts"] == {"total": 0, "by_state": {}}
    assert service.status()["scheduler"]["active_leases"] == 0


def test_authenticated_lease_records_are_detailed_but_metrics_are_bounded(tmp_path):
    service = _service(tmp_path)
    admission = service.scheduler.admit(
        AttemptRequest(
            challenge_id="challenge-private-id",
            experiment_id="experiment-private-id",
            attempt_id="1",
            idempotency_key="delivery-private-id",
        )
    )
    assert admission.admitted
    client = _client(service)
    path = "/api/v1/arena/leases"
    assert client.get(path).status_code == 401
    headers = {"authorization": "Bearer good"}
    lease = client.get(path, headers=headers).json()["leases"][0]
    assert lease["experiment_id"] == "experiment-private-id"
    assert lease["lease_id"] == admission.lease.lease_id
    metrics = client.get("/api/v1/arena/metrics", headers=headers).text
    assert "experiment-private-id" not in metrics
    assert admission.lease.lease_id not in metrics


def test_domain_frontier_api_matches_direct_service(tmp_path):
    service = _service(tmp_path, results=lambda: (_result("one", 10, 5),))
    rows = service.frontier(
        "sha256:challenge", (MetricObjective("throughput", MetricDirection.MAXIMIZE),)
    )
    assert rows[0]["experiment_id"] == "one"


def test_complete_status_and_run_ledger_report_observed_fields(tmp_path):
    experiment = _experiment("observed").model_copy(
        update={
            "served_model": "qwen-served",
            "gateway_backend_id": "reg:ornith",
            "gateway_request_id": "req-1",
        }
    )
    result = _result("observed", 10, 5)
    scheduled = (
        {
            "job": "arena-nightly",
            "status": "failed",
            "ok": False,
            "failure": "gateway unavailable",
            "stale": True,
        },
    )
    refinements = (
        SimpleNamespace(to_state=SimpleNamespace(value="promoted")),
        SimpleNamespace(to_state=SimpleNamespace(value="rolled_back")),
    )
    service = _service(
        tmp_path,
        experiments=lambda: (experiment,),
        results=lambda: (result,),
        refinements=lambda: refinements,
        scheduled_runs=lambda: scheduled,
    )
    service.metrics.add("frontier_movement", 2)
    service.store.append_event(
        __import__("skharness.arena.models", fromlist=["ExperimentEvent"]).ExperimentEvent(
            event_id="evt-observed",
            writer_id="worker",
            sequence=1,
            experiment_id="observed",
            to_state=ExperimentState.PROPOSED,
            timestamp=NOW,
            provenance=Provenance(
                actor="a", node="n", session_id="s", action="propose", target="observed"
            ),
            payload={"challenge_id": "tiny"},
        )
    )

    status = service.status()
    assert status["queue"]["depth"] == 1
    assert status["budgets"]["configured"]["wall_seconds"] == 1
    assert status["models"]["by_value"] == {"qwen-served": 1}
    assert status["backends"]["by_value"] == {"reg:ornith": 1}
    assert status["verifications"] == {"valid": 1}
    assert (status["promotions"], status["rollbacks"]) == (1, 1)
    assert status["observability"]["signals"]["frontier_movement"] == 2
    assert status["scheduled_jobs"] == {"known": 1, "failed_runs": 1, "stale": 1}
    run = service.runs()[0]
    assert run["gateway_request_id"] == "req-1"
    assert run["budgets"]["wall_seconds"] == 10
    assert run["verification"] == "valid"


def test_every_arena_observability_route_is_read_scoped(tmp_path):
    client = _client(_service(tmp_path))
    paths = (
        "status",
        "challenges",
        "attempts",
        "runs",
        "jobs",
        "failures",
        "verifications",
        "leases",
        "metrics",
    )
    for suffix in paths:
        assert client.get(f"/api/v1/arena/{suffix}").status_code == 401
        assert (
            client.get(
                f"/api/v1/arena/{suffix}", headers={"authorization": "Bearer good"}
            ).status_code
            == 200
        )


def test_arena_job_service_ledgers_success_and_captures_failure(tmp_path):
    captured = []
    alerted = []
    ticks = iter((100.0, 102.5, 200.0, 201.0))
    service = ArenaJobService(
        tmp_path / "arena-runs.jsonl",
        node="node-1",
        capture_failure=captured.append,
        alert_failure=alerted.append,
        clock=lambda: next(ticks),
    )
    assert service.run("arena-smoke", lambda: "ok", trigger="on_demand") == "ok"
    with pytest.raises(RuntimeError, match="gateway down"):
        service.run(
            "arena-nightly",
            lambda: (_ for _ in ()).throw(RuntimeError("gateway down")),
            trigger="scheduled",
        )
    rows = service.status()
    assert [row["status"] for row in rows] == ["ok", "failed"]
    assert rows[0]["dur_s"] == 2.5
    assert captured == alerted
    assert captured[0]["failure_type"] == "RuntimeError"
