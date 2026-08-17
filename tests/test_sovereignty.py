"""The one sovereignty definition, and the grader gate that consumes it.

Card a43cac2e. Every row used as a fixture below was READ OUT OF the live
skgateway ledger (`skgateway/data/metrics.db`, `energy_log`) on 2026-08-17,
opened read-only. They are not invented shapes: the point of this file is that
the old name-based rule certified rows that really exist.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skharness.autocode import orchestrator as orch
from skharness.autocode import sovereignty as sov

# --- the ledger rows this card exists because of ----------------------------
# (backend, basis, node, model) exactly as recorded.

CLOUD_SERVED_ALLOWLISTED_NAME = ("nvidia", "imputed_cloud", None,
                                 "meta/llama-3.3-70b-instruct")
#: `ornith-big` was `orchestrator.GRADER_MODEL` when this card was written. The
#: pin has since moved (the 35B was retired and the id 404s), but the fixture
#: does NOT move with it: it is a row the ledger actually holds, and repointing
#: it at whatever is pinned today would replace recorded evidence with a guess.
CLOUD_SERVED_THE_PINNED_GRADER = ("nvidia", "imputed_cloud", None, "ornith-big")
GENUINELY_SOVEREIGN = ("reg:ornith", "measured_gpu", "ollama", "sk-default")
SOVEREIGN_IMPUTED = ("reg:ornith", "imputed_local", None, "sk-default")
CONTRADICTORY_CLOUD_BASIS = ("anthropic", "imputed_local", None, "sk-heavy")


# --- negative controls: a cloud-served model with an allowlisted name --------

def test_a_cloud_served_model_with_an_allowlisted_name_is_not_sovereign():
    """THE required negative control. `meta/llama-3.3-70b-instruct` contains
    "llama", an allowlisted token in the old shell probe, and 76 rows like it
    ran on backend=nvidia / basis=imputed_cloud. A name check certifies every
    one of them. The definition must not."""
    backend, basis, node, _model = CLOUD_SERVED_ALLOWLISTED_NAME
    v = sov.classify(backend, basis, node)
    assert v.state == sov.VIOLATED
    assert v.sovereign is False
    assert "nvidia" in v.reason


def test_the_pinned_grader_model_served_by_cloud_is_not_sovereign():
    """`ornith-big` was `orchestrator.GRADER_MODEL`, the id this repo pinned as
    "sovereign". The ledger has it on backend=nvidia, basis=imputed_cloud. The
    old prefix rule returned True for that row, which means raw card text went
    to a cloud provider behind a green gate.

    The pin has since been repointed (the 35B behind it was retired and the id
    404s) and this fixture deliberately did not follow it. The row is evidence,
    and the guard that matters is not "the fixture matches today's pin" but that
    the verdict does not depend on the pin at all: `classify` never sees a model
    id, so the same row is refused whatever is pinned.
    """
    backend, basis, node, model = CLOUD_SERVED_THE_PINNED_GRADER
    assert model != orch.GRADER_MODEL, "the retired 35B must not be re-pinned"
    assert sov.classify(backend, basis, node).state == sov.VIOLATED


def test_a_third_party_backend_wins_over_a_local_looking_basis():
    """A real row: backend=anthropic with basis=imputed_local. That is an
    imputation bug, and an imputation bug must not buy a third party a
    sovereignty certificate. The denylist is checked first for this reason."""
    backend, basis, node, _model = CONTRADICTORY_CLOUD_BASIS
    assert sov.classify(backend, basis, node).state == sov.VIOLATED


# --- positive control: an always-false classifier must not pass --------------

def test_a_genuinely_sovereign_row_stays_sovereign():
    """Paired with the negative controls so an always-false classifier fails
    this file. measured_gpu plus a named node is the physical evidence."""
    backend, basis, node, _model = GENUINELY_SOVEREIGN
    v = sov.classify(backend, basis, node)
    assert v.state == sov.SOVEREIGN
    assert v.sovereign is True
    assert v.node == "ollama"


def test_a_local_backend_with_an_imputed_local_basis_is_sovereign():
    """Rank 2 evidence: config-grounded, no meter. Two `reg:ornith` /
    imputed_local rows exist, and refusing them would make the gate unusable on
    every node without an energy counter."""
    backend, basis, node, _model = SOVEREIGN_IMPUTED
    assert sov.classify(backend, basis, node).state == sov.SOVEREIGN


# --- the weights are not the variable ---------------------------------------

def test_the_definition_takes_no_model_parameter_at_all():
    """Structural pin. The defect was that a model id could decide this. It
    cannot decide it if it cannot be passed."""
    params = set(inspect.signature(sov.classify).parameters)
    assert params == {"backend", "basis", "node"}
    assert "model" not in params


def test_same_weights_opposite_jurisdictions_classify_oppositely():
    """The epic's sentence, executable. `ornith` weights served by nvidia is a
    violation; the same weights served by reg:ornith are not."""
    assert sov.classify("nvidia", "imputed_cloud", None).state == sov.VIOLATED
    assert sov.classify("reg:ornith", "measured_gpu", "ollama").state == sov.SOVEREIGN


# --- three states, and unknown is not sovereign ------------------------------

def test_nothing_observed_is_unobserved_and_not_sovereign():
    v = sov.classify(None, None, None)
    assert v.state == sov.UNOBSERVED
    assert v.sovereign is False


def test_an_unrecognised_backend_is_unobserved_not_sovereign():
    """A backend we have never seen has unknown jurisdiction. Fail closed, and
    say WHY rather than calling it a violation we did not observe."""
    v = sov.classify("some-new-provider", "imputed_local", None)
    assert v.state == sov.UNOBSERVED


def test_measured_gpu_without_a_node_is_unobserved():
    """measured_gpu is only physical evidence when something names the meter.
    A reading with no meter is a claim."""
    assert sov.classify("reg:ornith", "measured_gpu", None).state == sov.UNOBSERVED


def test_an_allowlisted_backend_reporting_a_cloud_basis_is_violated():
    """Contradictory observations are not certified. One of them is wrong and
    we cannot tell which."""
    assert sov.classify("reg:ornith", "imputed_cloud", None).state == sov.VIOLATED


def test_the_three_states_are_distinct():
    assert len({sov.SOVEREIGN, sov.VIOLATED, sov.UNOBSERVED}) == 3
    assert set(sov.STATES) == {sov.SOVEREIGN, sov.VIOLATED, sov.UNOBSERVED}


def test_a_non_string_observation_is_absent_not_coerced():
    """A MagicMock attribute is a repr, not an observation. Coercing it would
    make every mocked harness look like it reported a backend."""
    assert sov.classify(MagicMock(), MagicMock(), MagicMock()).state == sov.UNOBSERVED


# --- operator extension cannot defeat the denylist --------------------------

def test_an_operator_cannot_allowlist_a_third_party_backend(monkeypatch):
    """The extension point exists because new local backends appear. It must
    not be a way to relabel nvidia, so the denylist is checked first."""
    monkeypatch.setenv(sov.SOVEREIGN_BACKENDS_ENV, "nvidia,anthropic")
    assert sov.classify("nvidia", "measured_gpu", "ollama").state == sov.VIOLATED
    assert sov.classify("anthropic", "measured_gpu", "ollama").state == sov.VIOLATED


def test_an_operator_can_add_a_genuinely_new_local_backend(monkeypatch):
    assert sov.classify("mybox-vulkan", "imputed_local", None).state == sov.UNOBSERVED
    monkeypatch.setenv(sov.SOVEREIGN_BACKENDS_ENV, "mybox-")
    assert sov.classify("mybox-vulkan", "imputed_local", None).state == sov.SOVEREIGN


# --- the header seam, which is what both ends actually hold -----------------

def test_from_headers_reads_the_gateway_attribution_headers():
    """skgateway emits these today; verified live against localhost:18780,
    which answered x-sk-backend: reg:ornith / x-sk-energy-basis: measured_gpu /
    x-sk-energy-node: ollama."""
    v = sov.from_headers({"X-SK-Backend": "reg:ornith",
                          "x-sk-energy-basis": "measured_gpu",
                          "x-sk-energy-node": "ollama",
                          "x-sk-model-served": "ornith-1.0-9b"})
    assert v.state == sov.SOVEREIGN


def test_from_headers_ignores_the_model_served_header():
    """The response names ornith. It was served by nvidia. The header that
    matters is the backend."""
    v = sov.from_headers({"x-sk-backend": "nvidia",
                          "x-sk-energy-basis": "imputed_cloud",
                          "x-sk-model-served": "ornith-1.0-9b"})
    assert v.state == sov.VIOLATED


def test_a_response_with_no_attribution_headers_is_unobserved():
    """A gateway too old to report attribution has told us nothing, not that
    everything is fine."""
    assert sov.from_headers({"content-type": "application/json"}).state == sov.UNOBSERVED
    assert sov.from_headers(None).state == sov.UNOBSERVED


# --- the CLI seam the shell probe consumes ----------------------------------

def test_the_cli_exits_distinctly_per_state():
    """skcapstone's shell probe branches on these codes, so they are contract.
    0 sovereign, 1 violated, 2 unobserved: a shell must be able to tell "a
    cloud answered" from "nobody looked" without parsing prose."""
    def run(*args):
        return subprocess.run([sys.executable, "-m", "skharness.autocode.sovereignty",
                               *args], capture_output=True, text=True)

    ok = run("--backend", "reg:ornith", "--basis", "measured_gpu", "--node", "ollama")
    assert ok.returncode == 0 and ok.stdout.startswith("sovereign")

    bad = run("--backend", "nvidia", "--basis", "imputed_cloud")
    assert bad.returncode == 1 and bad.stdout.startswith("violated")

    unknown = run()
    assert unknown.returncode == 2 and unknown.stdout.startswith("unobserved")

    assert sov.EXIT_CODES == {sov.SOVEREIGN: 0, sov.VIOLATED: 1, sov.UNOBSERVED: 2}


def test_the_cli_emits_json_on_request():
    import json
    out = subprocess.run([sys.executable, "-m", "skharness.autocode.sovereignty",
                          "--backend", "nvidia", "--basis", "imputed_cloud", "--json"],
                         capture_output=True, text=True)
    payload = json.loads(out.stdout)
    assert payload["state"] == "violated"
    assert payload["backend"] == "nvidia"


# --- the grader gate now consumes the definition ----------------------------

def _observed(backend, basis, node=None):
    return SimpleNamespace(backend_served=backend, energy_basis=basis, energy_node=node)


def test_is_sovereign_grader_refuses_a_model_id_loudly():
    """The whole defect in one assertion. The old signature took a name and
    answered; answering at all is what made the gate wrong. A caller that
    still has a name in hand must get an error, not a verdict."""
    with pytest.raises(TypeError):
        orch.is_sovereign_grader("ornith-big")
    with pytest.raises(TypeError):
        orch.is_sovereign_grader("sk-default")


def test_is_sovereign_grader_reads_the_observed_backend():
    assert orch.is_sovereign_grader(_observed("reg:ornith", "measured_gpu", "ollama")) is True
    assert orch.is_sovereign_grader(_observed("nvidia", "imputed_cloud")) is False


def test_is_sovereign_grader_accepts_a_verdict():
    assert orch.is_sovereign_grader(sov.classify("reg:ornith", "imputed_local", None)) is True


def test_a_harness_that_observes_nothing_is_refused():
    """Fail closed. A harness with no attribution channel has not told us it
    ran sovereign; it has told us nothing, and nothing is not a pass."""
    assert orch.is_sovereign_grader(MagicMock()) is False
    assert orch.grader_sovereignty(MagicMock()).state == sov.UNOBSERVED


def test_grader_sovereignty_distinguishes_unobserved_from_violated():
    """Three states have to survive the trip through the orchestrator, or the
    operator cannot tell "wire the observation" from "fix the routing"."""
    assert orch.grader_sovereignty(_observed("nvidia", "imputed_cloud")).state == sov.VIOLATED
    assert orch.grader_sovereignty(_observed(None, None)).state == sov.UNOBSERVED
    assert orch.grader_sovereignty(
        _observed("reg:ornith", "measured_gpu", "ollama")).state == sov.SOVEREIGN


def test_the_requested_model_is_provenance_not_permission():
    """`requested_grader_model` still records what was ASKED FOR, because
    paired with rubric_version it is the only grade-drift signal. It just no
    longer decides anything."""
    assert orch.requested_grader_model(SimpleNamespace(model="ornith-1.0-35b")) == "ornith-1.0-35b"
    assert orch.requested_grader_model(SimpleNamespace()) == orch.GRADER_MODEL
    assert orch.requested_grader_model(MagicMock()) == orch.GRADER_MODEL


def test_the_model_name_allowlist_is_gone_from_the_orchestrator():
    """Structural pin: two definitions would be worse than either, and a
    leftover prefix tuple is a second definition waiting for a caller."""
    assert not hasattr(orch, "_SOVEREIGN_GRADER_PREFIXES")
    assert not hasattr(orch, "grader_model_for"), \
        "the old misleading name must not survive as an alias"
