import json
from pathlib import Path

from skharness.arena.models import ChallengeSpec


def test_frozen_reference_challenge_parses_and_has_stable_hash():
    path = Path(__file__).parent / "data/arena-reference-challenge-v1.json"
    spec = ChallengeSpec.model_validate_json(path.read_text())
    assert spec.id == "skharness-reference-patch"
    assert spec.repetitions == len(spec.seeds) == 3
    # Deliberately frozen from the canonical Pydantic serialization, not the file's
    # whitespace. A contract or fixture change must explicitly rotate this value.
    assert spec.content_hash == "sha256:640658f18c4403c47c17609b51c1523bd3829ec4d7e47f7bd4cc40bd34f16ffd"
    assert "hidden-test-read" in spec.prohibited_optimizations


def test_fixture_contains_no_private_evaluation_content():
    raw = json.loads((Path(__file__).parent / "data/arena-reference-challenge-v1.json").read_text())
    assert set(raw["withheld_dataset_ref"]) == {"uri", "digest"}
    assert raw["withheld_dataset_ref"]["uri"].startswith("private://")
