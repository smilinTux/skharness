"""Calibration v1 is bounded reporting and cannot become routing or gating."""
from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

from skharness.autocode import calibration


def _grade(size="S", risk="low", sensitivity="internal", model_class="S"):
    return {"size": size, "risk": risk, "sensitivity": sensitivity,
            "model_class": model_class}


def test_retry_evidence_from_all_three_sinks_becomes_one_undergrade_candidate():
    report = calibration.build_report(
        health_events=[{"kind": "build_economics", "task": "card-1", "retries": 2,
                        "rounds_cap": 4, "ts": 1787695200}],
        outcome_rows=[{"card_id": "card-1", "retries": 3, "work_grade": _grade(),
                       "ts": "2026-08-25T10:00:00Z"}],
        run_records=[{"card_id": "card-1", "retries": 1, "task_shape": _grade(),
                      "recorded_at": "2026-08-25T11:00:00Z"}],
        now=datetime(2026, 8, 26, tzinfo=timezone.utc),
    )

    assert report["backlog_count"] == 1
    candidate = report["candidates"][0]
    assert candidate["kind"] == calibration.UNDERGRADE
    assert candidate["schema"].endswith(".v1")
    assert candidate["retries"] == 3
    assert candidate["rounds_cap"] == 4
    assert candidate["evidence_sources"] == ["health", "outcome", "run_record"]
    assert candidate["authority"] == "report_only"


def test_health_retry_joins_to_the_cards_stored_meta_grade():
    candidates = calibration.undergrade_candidates(
        health_events=[{"kind": "build_economics", "task": "card-2", "retries": 1}],
        cards=[{"id": "card-2", "meta": {"grade": _grade()}}],
    )
    assert len(candidates) == 1
    assert candidates[0]["work_grade"] == _grade()


def test_quick_large_model_pass_does_not_create_an_overgrade_candidate():
    """A quick pass under a large model is confounded and excluded from v1."""
    report = calibration.build_report(
        outcome_rows=[{"card_id": "easy-or-big-model", "retries": 0,
                       "work_grade": _grade(size="XL", model_class="XL"),
                       "passed": True, "model_served": "sk-xl-internal"}],
    )
    assert report["backlog_count"] == 0
    assert "overgrade_candidates" not in calibration.__dict__


def test_every_explicit_sensitivity_override_is_captured_without_interpretation():
    cards = [
        {"id": "public-card", "created_at": "2026-08-20T00:00:00Z",
         "meta": {"sensitivity_override": "public"}},
        {"id": "invalid-card", "created_at": "2026-08-21T00:00:00Z",
         "meta": {"sensitivity_override": "publik"}},
        {"id": "missing-card", "meta": {}},
    ]
    candidates = calibration.sensitivity_override_candidates(cards)
    assert [(item["card_id"], item["sensitivity_override"]) for item in candidates] == [
        ("invalid-card", "invalid"), ("public-card", "public")]
    assert all(item["authority"] == "report_only" for item in candidates)


def test_digest_line_mandates_backlog_count_oldest_age_and_three_dispositions():
    cards = [
        {"id": f"card-{index}", "created_at": "2026-07-16T00:00:00Z",
         "meta": {"sensitivity_override": "secret"}}
        for index in range(14)
    ]
    report = calibration.build_report(
        cards=cards,
        now=datetime(2026, 8, 26, tzinfo=timezone.utc),
        show_limit=3,
    )
    line = calibration.digest_line(report)
    assert "showing 3 of 14" in line
    assert "oldest 41 days" in line
    assert "[promote/dismiss/defer]" in line
    assert line.count("\n") == 0


def test_existing_numbered_digest_renders_exactly_one_calibration_line():
    from skharness.autocode import digest

    report = {"backlog_count": 14, "shown_count": 3, "oldest_days": 41,
              "dispositions": list(calibration.DISPOSITIONS)}
    manifest = digest.build_manifest(
        [], digest_date="2026-08-26", calibration_report=report,
    )
    text = digest.build_digest_text(manifest)
    lines = [line for line in text.splitlines() if line.startswith("Calibration candidates:")]
    assert lines == [
        "Calibration candidates: showing 3 of 14, oldest 41 days "
        "[promote/dismiss/defer]."
    ]


def test_digest_summary_does_not_leak_sensitive_override_content():
    sensitive = "not-a-real-secret-but-must-not-be-rendered"
    report = calibration.build_report(cards=[{
        "id": "sensitive-card",
        "meta": {"sensitivity_override": sensitive},
    }])
    line = calibration.digest_line(report)
    assert sensitive not in repr(report)
    assert sensitive not in line
    assert "sensitive-card" not in line
    assert "showing 1 of 1" in line


def test_reporting_module_has_no_writer_gate_or_route_import():
    path = Path(calibration.__file__).resolve()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    opened_modes = []
    forbidden_calls = {"write_text", "write_bytes", "rename",
                       "unlink", "bucket_for_payload", "bucket_id", "attach_dispatch_model",
                       "twin_gate_passed", "queue_decision", "ratify_entry"}
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            else:
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            called.add(name)
            if name == "open":
                mode = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else None
                opened_modes.append(mode)
    assert not ({"buckets", "grading", "sensitivity", "engineering", "orchestrator",
                 "resolver", "golden_set"} & imported), imported
    assert not (forbidden_calls & called), forbidden_calls & called
    assert all(mode in (None, "r") for mode in opened_modes)


def test_calibration_payload_cannot_change_dispatch_or_gate_result():
    from skharness.autocode.engineering import EngineeringExecutor, twin_gate_passed
    from skharness.autocode.types import GateResult, RepoSpec, WorkItem

    base = {"id": "card-1", "work_grade": _grade()}
    loaded = {**base, "calibration_candidate": {
        "schema": calibration.CALIBRATION_CANDIDATE_SCHEMA_V1,
        "authority": "report_only",
        "disposition": "promote",
    }}
    make = lambda payload: WorkItem(  # noqa: E731
        kind="engineering", ref="card-1", source="coord", repo="skharness",
        payload=payload,
    )
    assert EngineeringExecutor._dispatch_model(None, make(base)) == \
        EngineeringExecutor._dispatch_model(None, make(loaded))

    result = GateResult(score=5, passed=True, artifact=None,
                        notes="done <promise>COMPLETE</promise>", outcome="pass")
    repo = RepoSpec(
        name="skharness", path="/tmp/unused", base_branch="main",
        integration_branch="main", test_cmd="true", ci="none",
        min_diff_coverage=0.8,
    )
    clean = twin_gate_passed(result, "green", 0.9, repo)
    loaded["calibration_report"] = {"backlog_count": 999, "authority": "gate"}
    assert twin_gate_passed(result, "green", 0.9, repo) is clean is True
