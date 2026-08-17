import importlib.util

import pytest


_HAVE_SKCAPSTONE = importlib.util.find_spec("skcapstone") is not None


def pytest_collection_modifyitems(config, items):
    """Auto-skip tests marked `needs_skcapstone` when the optional sibling
    skcapstone package is not importable (e.g. in CI, which installs only skos).
    skcapstone is not a declared skos dependency, so its absence must not turn
    the suite red."""
    if _HAVE_SKCAPSTONE:
        return
    skip = pytest.mark.skip(reason="optional sibling skcapstone not installed")
    for item in items:
        if "needs_skcapstone" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _allow_empty_store(monkeypatch):
    """Tests run against throwaway, empty stores. The skos cold-start guard
    correctly refuses to emit from an un-restored store in production, but in the
    suite that (recently added) guard turns valid tests red. Opt the test process
    into the documented fresh-init bypass."""
    monkeypatch.setenv("SKOS_ALLOW_EMPTY_STORE", "1")


@pytest.fixture(autouse=True)
def _isolate_health(tmp_path_factory, monkeypatch):
    """Point harness health telemetry at a throwaway file for EVERY test. Any test
    that exercises _run/assess records events; without isolation those fake events
    would land in the real ~/.skcapstone health log and skew the adaptive retry
    budget (which reads that log) in production. Isolation keeps telemetry a pure
    observation of real runs."""
    hp = tmp_path_factory.mktemp("health") / "health.jsonl"
    monkeypatch.setenv("SKHARNESS_HEALTH_PATH", str(hp))


@pytest.fixture(autouse=True)
def _isolate_cost_dir(tmp_path_factory, monkeypatch):
    """Point the autopilot cost ledger AND the settlement journal at a throwaway
    dir for EVERY test. The gated settle path consults the settlement journal for
    its double-settle guard and appends to it on a real settlement, so without
    this a finalize test would read and write the live, Syncthing-synced
    ~/.skcapstone/autopilot-cost tree. Test suites writing to the real fleet is a
    standing hazard here; the per-file fixtures that already set SKAI_COST_DIR
    still win, this only closes the default."""
    cd = tmp_path_factory.mktemp("autopilot-cost")
    monkeypatch.setenv("SKAI_COST_DIR", str(cd))


@pytest.fixture(autouse=True)
def _hermetic_fleet(monkeypatch, tmp_path):
    """Point the fleet dispatch gate at an empty tree so orchestrator tests
    never consult the live ~/.skcapstone/fleet. The gate stays inert (no
    admitted nodes) unless a test builds its own tree under this root or
    overrides SKFLEET_ROOT itself."""
    monkeypatch.setenv("SKFLEET_ROOT", str(tmp_path / "fleet-hermetic"))


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """Point SK_DATA_ROOT at a throwaway dir for every test."""
    root = tmp_path / "skdata"
    monkeypatch.setenv("SK_DATA_ROOT", str(root))
    monkeypatch.delenv("SKOS_PROFILE", raising=False)
    return root


@pytest.fixture
def vault_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("SKOS_VAULT_KEY", Fernet.generate_key().decode())
