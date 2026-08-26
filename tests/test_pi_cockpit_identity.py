"""Identity wiring at the manual Pi cockpit production construction site."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).parents[1] / "scripts" / "pi_cockpit" / "run_card.py"
SPEC = importlib.util.spec_from_file_location("pi_cockpit_run_card", SCRIPT)
assert SPEC and SPEC.loader
RUN_CARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUN_CARD)


def _config(**overrides):
    values = {
        "harness_base_url": "http://gateway.test/v1",
        "mcp_endpoints": [],
        "live_execution": False,
        "sandbox_image": "sandbox-pi:1",
        "harness_max_tokens": 4096,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_cockpit_adapter_carries_local_run_and_card_ids():
    adapter = RUN_CARD._build_pi_adapter(
        _config(), SimpleNamespace(sandbox_image=None), "sk-s-public",
        session_id="pi-cockpit-a49d1c36-123", card_id="a49d1c36")

    assert adapter.session_id == "pi-cockpit-a49d1c36-123"
    assert adapter.card_id == "a49d1c36"


def test_cockpit_adapter_keeps_session_and_card_axes_independent():
    config = _config(sandbox_image=None, harness_max_tokens=None)
    repo = SimpleNamespace(sandbox_image="sandbox-pi:2")

    first = RUN_CARD._build_pi_adapter(
        config, repo, "sk-s-public", session_id="run-one", card_id="card-one")
    second = RUN_CARD._build_pi_adapter(
        config, repo, "sk-s-public", session_id="run-two", card_id="card-two")

    assert (first.session_id, first.card_id) == ("run-one", "card-one")
    assert (second.session_id, second.card_id) == ("run-two", "card-two")
