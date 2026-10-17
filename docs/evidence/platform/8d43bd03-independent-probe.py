from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

NOW = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[3]
PARENT = ROOT.parent / "parent" / "src/skharness/pi_spawn_control.py"
CANDIDATE = ROOT.parent / "candidate" / "src/skharness/pi_spawn_control.py"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def rewrite(gate, mutate) -> None:
    state = json.loads(gate.path.read_text(encoding="utf-8"))
    mutate(state)
    gate.path.write_text(json.dumps(state), encoding="utf-8")


def fake_launch(module, path: Path, worker_id: str, kind: str):
    calls: list[list[str]] = []
    gate = module.SpawnControl(path, clock=lambda: NOW)
    try:
        gate.guarded_run(
            ["isolated-fake-launch", "--no-live-action"],
            worker_id=worker_id,
            actor="pi-codex-chiap02-8d43bd03",
            scope="pi:all",
            kind=kind,
            runner=lambda argv, **kwargs: calls.append(list(argv))
            or subprocess.CompletedProcess(argv, 0),
        )
        outcome = "runner_reached"
    except module.SpawnControlError as exc:
        outcome = f"fail_closed:{type(exc).__name__}"
    return outcome, calls


def inconsistent_pause(module, path: Path, candidate_shape: bool):
    gate = module.SpawnControl(path, clock=lambda: NOW)
    gate.bootstrap(actor="installer")
    operation = {
        "command": "pause",
        "owner": "operator-a",
        "reason": "reserve canary",
        "scope": "pi:all",
        "ttl_seconds": 300,
        "expected_fence": 0,
    }
    if candidate_shape:
        operation["applied_at"] = "2026-08-25T15:00:00Z"
    rewrite(gate, lambda state: state.update(last_operation=operation))
    return fake_launch(module, path, "pause-history", "process")


def mismatched_resume(module, path: Path, candidate_shape: bool):
    gate = module.SpawnControl(path, clock=lambda: NOW)
    gate.bootstrap(actor="installer")
    operation = {"command": "resume", "owner": "operator-a", "fence": 99}
    if candidate_shape:
        operation.update(
            mode="pause",
            reason="reserve canary",
            scope="pi:all",
            expires_at="2026-08-25T15:05:00Z",
            applied_at="2026-08-25T15:00:00Z",
        )
    rewrite(gate, lambda state: state.update(last_operation=operation))
    return fake_launch(module, path, "resume-history", "tmux")


def future_worker(module, path: Path, kind: str):
    gate = module.SpawnControl(path, clock=lambda: NOW)
    gate.bootstrap(actor="installer")
    prior = gate.reserve(
        "existing-worker", actor="pool", scope="pi:all", kind="process"
    )
    rewrite(
        gate,
        lambda state: state["workers"]["existing-worker"].update(
            reserved_at="2026-08-25T16:00:00Z"
        ),
    )
    outcome, calls = fake_launch(module, path, f"future-{kind}", kind)
    state = json.loads(path.read_text(encoding="utf-8"))
    existing_untouched = (
        state["workers"]["existing-worker"]["token"] == prior.token
        and state["workers"]["existing-worker"]["reserved_at"]
        == "2026-08-25T16:00:00Z"
    )
    return outcome, calls, existing_untouched


def main() -> None:
    parent = load("card_8d43bd03_parent", PARENT)
    candidate = load("card_8d43bd03_candidate", CANDIDATE)
    with tempfile.TemporaryDirectory(prefix="8d43bd03-") as raw:
        root = Path(raw)
        results = {
            "launch_boundary": "injected fake runner only",
            "parent_inconsistent_pause_process": inconsistent_pause(
                parent, root / "parent-pause.json", False
            ),
            "candidate_inconsistent_pause_process": inconsistent_pause(
                candidate, root / "candidate-pause.json", True
            ),
            "parent_mismatched_resume_tmux": mismatched_resume(
                parent, root / "parent-resume.json", False
            ),
            "candidate_mismatched_resume_tmux": mismatched_resume(
                candidate, root / "candidate-resume.json", True
            ),
            "candidate_future_reserved_at_process": future_worker(
                candidate, root / "candidate-future-process.json", "process"
            ),
            "candidate_future_reserved_at_tmux": future_worker(
                candidate, root / "candidate-future-tmux.json", "tmux"
            ),
        }
    print(json.dumps(results, indent=2, sort_keys=True))
    assert results["parent_inconsistent_pause_process"][0] == "runner_reached"
    assert results["candidate_inconsistent_pause_process"][0].startswith("fail_closed:")
    assert results["parent_mismatched_resume_tmux"][0] == "runner_reached"
    assert results["candidate_mismatched_resume_tmux"][0].startswith("fail_closed:")
    assert results["candidate_future_reserved_at_process"][0] == "runner_reached"
    assert results["candidate_future_reserved_at_tmux"][0] == "runner_reached"
    assert results["candidate_future_reserved_at_process"][2] is True
    assert results["candidate_future_reserved_at_tmux"][2] is True


if __name__ == "__main__":
    main()
