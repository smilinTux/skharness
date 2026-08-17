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


def _wallet_fingerprints() -> dict[str, tuple[int, int]]:
    """(size, line count) of every real wallet ledger and snapshot on this box."""
    prints: dict[str, tuple[int, int]] = {}
    agents = Path.home() / ".skcapstone" / "agents"
    if not agents.is_dir():
        return prints
    for pattern in ("*/wallet/transactions.jsonl", "*/wallet/joules.json"):
        for p in sorted(agents.glob(pattern)):
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            prints[str(p)] = (len(raw), raw.count(b"\n"))
    return prints


@pytest.fixture(scope="session", autouse=True)
def _live_wallets_must_not_change():
    """The negative control, wired into the suite instead of run by hand once.

    Fingerprint every real wallet before the session and assert it is unchanged
    afterwards. Isolation asserted in a single test file stops being asserted the
    moment someone adds a new write path, so the check belongs at the session
    boundary where it covers every test that ran, including ones not yet written.

    A caveat worth knowing before you debug a red here: this reads files another
    process on this box can legitimately append to (a real autopilot settlement
    during a long run). That is why the failure message prints the exact deltas
    rather than only a boolean, so a genuine concurrent write is one read away
    from being told apart from a leak.
    """
    before = _wallet_fingerprints()
    yield
    after = _wallet_fingerprints()
    if after == before:
        return
    deltas = []
    for path in sorted(set(before) | set(after)):
        b, a = before.get(path), after.get(path)
        if b != a:
            deltas.append(f"  {path}: {b} -> {a}")
    raise AssertionError(
        "a REAL joule wallet changed during this test run. The suite must never "
        "write to the operator's ledger.\n" + "\n".join(deltas))


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
