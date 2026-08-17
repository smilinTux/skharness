import importlib.util
import json
import sys
from pathlib import Path

import pytest

from skharness.autocode import joules
from skharness.autocode import ledger_correction
from skharness.autocode import wallet_correction


_HAVE_SKCAPSTONE = importlib.util.find_spec("skcapstone") is not None


# --------------------------------------------------------------------------- #
# Session-scoped production-store guard (S29, card 60245d49)                   #
# --------------------------------------------------------------------------- #
#
# Fingerprint BOTH append-only production stores at session start and assert
# them unchanged at session end.
#
# WHY A SESSION GUARD AND NOT A UNIT TEST. The leak this closes was invisible to
# every per-file check: each suspect file ran clean on its own, so six files
# were each cleared honestly and all six were innocent. A defect that only
# exists in the aggregate needs a check that only runs in the aggregate.
#
# WHY A FIXTURE-SIGNATURE COUNT AND NOT A WHOLE-FILE COMPARISON. Several agents
# run suites on this box at once and other processes legitimately append to both
# files while this suite is running, so a naive before/after byte or row
# comparison produces false alarms. Worse, it produces the WRONG INFERENCE: the
# card this guard comes from concluded from exactly such a count that our own
# ThreadPoolExecutor was leaking, when the writer was a different repo's suite
# entirely (docs/S29-cost-ledger-leak-attribution.md). A row count on a shared
# file cannot attribute a write. So the fingerprint counts only rows carrying a
# TEST FIXTURE signature: for the ledger a card id no real card can have, for
# the wallet the exact description string the finalize fixtures mint. A genuine
# row appended by another session moves neither count.
#
# WHY IT FAILS AND DOES NOT WARN. A warning in a 1500-test run is invisible;
# that is a fact about attention, not about pytest. This sets the session exit
# status and prints a banner.

_LIVE_LEDGER = Path("~/.skcapstone/autopilot-cost/ledger.jsonl").expanduser()
_LIVE_WALLET = Path("~/.skcapstone/agents/lumina/wallet/transactions.jsonl").expanduser()


def _wallet_fixture_rows(path: Path) -> int:
    """Count wallet rows carrying the fixture mint signature. READ ONLY, and
    never raises: a guard that can break the suite it guards is a liability."""
    if not path.exists():
        return 0
    total = 0
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(row, dict)
                        and str(row.get("description", "")).strip()
                        == wallet_correction.FIXTURE_DESCRIPTION):
                    total += 1
    except OSError:
        return total
    return total


def _production_fixture_counts() -> dict:
    try:
        return {
            "cost ledger": (str(_LIVE_LEDGER),
                            ledger_correction.count_fixture_rows(_LIVE_LEDGER)),
            "joule wallet": (str(_LIVE_WALLET), _wallet_fixture_rows(_LIVE_WALLET)),
        }
    except Exception:  # noqa: BLE001 -- never let the guard break the suite
        return {}


def pytest_sessionstart(session):
    session.config._s29_store_baseline = _production_fixture_counts()


def pytest_sessionfinish(session, exitstatus):
    before = getattr(session.config, "_s29_store_baseline", None)
    if not before:
        return
    after = _production_fixture_counts()
    moved = [
        (name, path, before[name][1], count)
        for name, (path, count) in after.items()
        if name in before and count > before[name][1]
    ]
    if not moved:
        return

    # Attribution, so the failure says WHICH process wrote. This one is
    # deterministic where the file count is not: the writer guard in
    # autopilot_cost refuses a production append from any pytest process, so a
    # row that appears while this suite runs came from a checkout that does not
    # have that guard yet. Saying so is the difference between a red that
    # teaches and a red that gets muted.
    banner = [
        "",
        "=" * 78,
        "PRODUCTION STORE CORRUPTED DURING THIS TEST SESSION",
        "=" * 78,
    ]
    for name, path, was, now in moved:
        banner.append(f"  {name}: fixture rows {was} -> {now}  (+{now - was})")
        banner.append(f"    {path}")
    banner += [
        "",
        "  These stores are append-only and Syncthing-synced. A fixture row",
        "  written into them cannot be taken back; it can only be corrected",
        "  beside them (skharness.autocode.ledger_correction,",
        "  skharness.autocode.wallet_correction).",
        "",
        "  If this suite wrote them, an isolation fixture in tests/conftest.py",
        "  regressed. If it did not, another checkout on this box is running a",
        "  suite without the writer guard: ~/clawd/skos drives this package",
        "  through the skos.autopilot.orchestrator shim and its tests/conftest.py",
        "  has no _isolate_cost_dir. See docs/S29-cost-ledger-leak-attribution.md.",
        "=" * 78,
        "",
    ]
    print("\n".join(banner), file=sys.stderr)
    # wrap_session returns session.exitstatus AFTER this hook runs, so setting
    # it here is what actually turns the run red. Only ever raises the status;
    # a real test failure must not be downgraded into this one.
    if not exitstatus:
        session.exitstatus = 1


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
