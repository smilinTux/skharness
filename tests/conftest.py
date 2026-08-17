import importlib.util
from pathlib import Path

import pytest

from skharness.autocode import joules


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
def _isolate_joule_wallet(tmp_path_factory, monkeypatch):
    """Point the JOULE WALLET at a throwaway skcapstone root for EVERY test.

    joules.settle() mints and spends against JouleWallet(agent), which resolves
    the operator's real ~/.skcapstone home when nothing overrides it. settle() is
    the twin-gate pass path and the finalize tests exercise the pass path, so the
    suite minted well formed joules into the live ledger for weeks: 1,433 rows
    carrying the fixture description 'autocode task_complete t1' and 107,475
    joules, in a wallet the joule economy reads as real. The rows are individually
    valid and indistinguishable from genuine ones except by that string.

    SKAI_COST_DIR above closed the settlement JOURNAL half of this. This closes
    the WALLET half, and _usage_home rides the same override so the cost
    telemetry under {home}/usage is covered too.

    On by default rather than opt-in: opt-in isolation fails exactly when someone
    forgets, which is the case that matters. Per-file fixtures that pass an
    explicit home= still win, same precedence as SKAI_COST_DIR.
    """
    root = tmp_path_factory.mktemp("joule-wallet")
    monkeypatch.setenv(joules.WALLET_HOME_ENV, str(root))


@pytest.fixture(scope="session", autouse=True)
def _no_production_wallet_writes():
    """Guard the WRITER itself for the whole session: no JouleWallet in this
    process may ever open a directory under a real skcapstone root.

    This deliberately does NOT work by fingerprinting the live ledger before and
    after the session, which was the obvious implementation and is the wrong one.
    Other processes on this box append to that same file (this bug is live in
    every checkout that lacks the fix, so a parallel suite run mints into it
    while this one is running). A check that reads a shared file cannot tell
    "our tests leaked" from "someone else's did", and a gate that goes red for
    reasons unrelated to the branch under test stops being read as a signal.

    Wrapping JouleWallet.__init__ has neither problem. It observes THIS process's
    writer at the moment of construction, so it is deterministic, immune to
    concurrent writers, and strictly broader than guarding settle(): it catches
    any future code path that opens a wallet, not just the one that leaks today.
    """
    try:
        from skcapstone import skjoule
    except Exception:  # bare harness: nothing can write a wallet at all
        yield
        return

    original = skjoule.JouleWallet.__init__

    def _guarded(self, agent_name, home=None, *args, **kwargs):
        root = Path(home) if home else Path(skjoule.SHARED_ROOT).expanduser()
        try:
            resolved = root.expanduser().resolve()
        except OSError:
            resolved = root
        if resolved in joules._production_roots():
            raise joules.ProductionWalletInTestError(
                f"a test opened a PRODUCTION joule wallet: "
                f"{resolved}/agents/{agent_name}/wallet. Constructing a wallet "
                f"creates its snapshot, and minting into it writes fabricated "
                f"economic history to the operator's ledger. Pass home=tmp_path, "
                f"or let the autouse {joules.WALLET_HOME_ENV} default apply.")
        return original(self, agent_name, home=home, *args, **kwargs)

    skjoule.JouleWallet.__init__ = _guarded
    try:
        yield
    finally:
        skjoule.JouleWallet.__init__ = original


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
