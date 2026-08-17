"""The test suite must never mint real joules into a real wallet.

Background. ``joules.settle()`` is called on the twin-gate pass path, the
finalize tests exercise the pass path, and ``JouleWallet(agent)`` resolves the
operator's live ``~/.skcapstone`` home when nothing overrides it. So for weeks
every suite run appended well formed mints to the real ledger: 1,433 rows
carrying the fixture description ``autocode task_complete t1``, 107,475 joules,
in a wallet whose balance is read by the joule economy as real. The rows are
individually valid and indistinguishable from genuine ones except by their
description string, which is the fleet's signature failure shape: broken and
healthy look identical.

The tests below are built as a matched pair, because a "the live file did not
change" assertion on its own proves nothing. It passes just as happily when the
write path is dead, when settle() no-ops, or when the fixture never reached the
mint at all.

  POSITIVE CONTROL  the write path is live and lands where the override says
                    (``test_writer_honours_the_env_override``), and an
                    un-isolated run is DETECTED rather than silently written
                    (``test_guard_fires_on_an_unisolated_run``).
  NEGATIVE CONTROL  with isolation on, the real ledger is byte-identical
                    (``test_live_wallet_is_untouched_by_a_real_settlement``).

Note how the un-isolated cases are exercised. They stand up a DECOY production
root by monkeypatching ``skjoule.SHARED_ROOT``, so the un-isolated path can be
driven honestly without the demonstration itself minting into the operator's
wallet. That is also why ``_production_roots()`` reads that attribute live
instead of caching it at import: the guard must see whatever the writer sees.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from skharness.autocode import joules
from skharness.autocode.joules import (
    BuildUsage,
    ProductionWalletInTestError,
    settle,
)


requires_skjoule = pytest.mark.skipif(
    not joules._skjoule_available(),
    reason="optional sibling skcapstone/skjoule not installed",
)

LIVE_WALLET = Path.home() / ".skcapstone" / "agents" / "lumina" / "wallet" / "transactions.jsonl"


def _fingerprint(path: Path) -> tuple[int, int, str] | None:
    """(size, line count, sha256) of a ledger, or None if it does not exist."""
    if not path.exists():
        return None
    raw = path.read_bytes()
    return (len(raw), raw.count(b"\n"), hashlib.sha256(raw).hexdigest())


def _ledger_rows(root: Path, agent: str) -> list[dict]:
    """Every transaction written under *root* for *agent*."""
    p = root / "agents" / agent / "wallet" / "transactions.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Resolution and precedence                                                    #
# --------------------------------------------------------------------------- #

def test_env_override_is_honoured_when_no_home_is_passed(monkeypatch, tmp_path):
    monkeypatch.setenv(joules.WALLET_HOME_ENV, str(tmp_path / "from-env"))
    assert joules.wallet_home() == tmp_path / "from-env"


def test_explicit_home_beats_the_env_override(monkeypatch, tmp_path):
    """A caller that already isolated itself keeps its choice, exactly as the
    per-file SKAI_COST_DIR fixtures still win over the conftest default."""
    monkeypatch.setenv(joules.WALLET_HOME_ENV, str(tmp_path / "from-env"))
    assert joules.wallet_home(tmp_path / "explicit") == tmp_path / "explicit"


def test_unset_override_defers_to_skjoule(monkeypatch):
    """No override means "let skjoule decide", which is what production wants.
    The guard, not the resolver, is what makes that unsafe inside a test."""
    monkeypatch.delenv(joules.WALLET_HOME_ENV, raising=False)
    assert joules.wallet_home() is None


# --------------------------------------------------------------------------- #
# POSITIVE CONTROL 1: the guard detects an un-isolated run                      #
# --------------------------------------------------------------------------- #

@requires_skjoule
def test_guard_fires_on_an_unisolated_run(monkeypatch, tmp_path):
    """Drop the isolation and the run must FAIL, not write.

    The decoy root stands in for ~/.skcapstone so this can drive the exact
    un-isolated code path without the test itself minting into the real ledger.
    """
    from skcapstone import skjoule

    decoy = tmp_path / "decoy-production"
    monkeypatch.setattr(skjoule, "SHARED_ROOT", str(decoy))
    monkeypatch.delenv(joules.WALLET_HOME_ENV, raising=False)

    with pytest.raises(ProductionWalletInTestError) as excinfo:
        settle(
            "lumina", "t1",
            priority="medium", score=5,
            usage=BuildUsage(cost_usd=0.5, output_tokens=100),
        )

    assert joules.WALLET_HOME_ENV in str(excinfo.value)
    # And it raised BEFORE writing: no wallet was created under the decoy root.
    assert _ledger_rows(decoy, "lumina") == []


@requires_skjoule
def test_guard_fires_for_an_explicit_production_home(monkeypatch, tmp_path):
    """Passing the production root explicitly is not a way around the guard.
    The guard checks the path the WRITER will use, not how it was chosen."""
    from skcapstone import skjoule

    decoy = tmp_path / "decoy-production"
    decoy.mkdir()
    monkeypatch.setattr(skjoule, "SHARED_ROOT", str(decoy))

    with pytest.raises(ProductionWalletInTestError):
        settle(
            "lumina", "t1",
            priority="medium", score=5,
            usage=BuildUsage(cost_usd=0.5),
            home=decoy,
        )


def test_guard_is_inert_outside_a_test_run(monkeypatch, tmp_path):
    """Production must never be refused a settlement. The guard is a test-run
    assertion, not a runtime policy."""
    from skcapstone import skjoule

    decoy = tmp_path / "decoy-production"
    monkeypatch.setattr(skjoule, "SHARED_ROOT", str(decoy), raising=False)
    monkeypatch.setattr(joules, "_in_test_run", lambda: False)
    # Does not raise.
    joules.assert_not_production_wallet_in_test(decoy)


# --------------------------------------------------------------------------- #
# POSITIVE CONTROL 2: the WRITER honours the override, not just the reader      #
# --------------------------------------------------------------------------- #

@requires_skjoule
def test_writer_honours_the_env_override(monkeypatch, tmp_path):
    """The direction that has burned this fleet before.

    In the npm-test-clobbers-the-production-cache incident the reader honoured
    the env override and the writer ignored it, so every freshness check passed
    while production was being overwritten. Asserting that the resolver returns
    the override is therefore not enough. This asserts on the BYTES that landed
    on disk: the mint is in the override tree, with the right amount, and the
    default tree was never created.
    """
    from skcapstone import skjoule

    decoy = tmp_path / "decoy-production"
    isolated = tmp_path / "isolated"
    monkeypatch.setattr(skjoule, "SHARED_ROOT", str(decoy))
    monkeypatch.setenv(joules.WALLET_HOME_ENV, str(isolated))

    econ = settle(
        "wallet-isolation-agent", "t1",
        priority="medium", score=5,
        usage=BuildUsage(model="claude-code", output_tokens=100, cost_usd=0.10),
    )

    assert econ.recorded is True, "the write path must be LIVE for this to prove anything"
    assert econ.minted == 75

    rows = _ledger_rows(isolated, "wallet-isolation-agent")
    descriptions = [r.get("description", "") for r in rows]
    assert "autocode task_complete t1" in descriptions, (
        f"the mint did not land in the override tree; got {descriptions}")

    # The writer did not ALSO write to the default root, and did not write there
    # instead. Neither the ledger nor the usage ledger appeared under the decoy.
    assert not decoy.exists(), f"writer touched the default root anyway: {sorted(decoy.rglob('*'))}"


# --------------------------------------------------------------------------- #
# NEGATIVE CONTROL: the live ledger is untouched                                #
# --------------------------------------------------------------------------- #

@requires_skjoule
def test_live_wallet_is_untouched_by_a_real_settlement():
    """A settlement runs under the conftest default isolation, and the
    operator's real ledger comes out byte-identical.

    This runs with NO monkeypatching of the environment at all: it is the
    default configuration every other test in the suite gets. Its value depends
    entirely on the positive controls above, which establish that a settlement
    under these conditions really does write somewhere.
    """
    before = _fingerprint(LIVE_WALLET)

    econ = settle(
        "lumina", "t1",
        priority="medium", score=5,
        usage=BuildUsage(model="claude-code", output_tokens=100, cost_usd=0.10),
    )
    assert econ.recorded is True, "settlement must have actually happened"
    assert econ.minted == 75

    after = _fingerprint(LIVE_WALLET)
    assert after == before, (
        f"the live joule ledger CHANGED during a test run.\n"
        f"  before (size, lines, sha256) = {before}\n"
        f"  after  (size, lines, sha256) = {after}")


@requires_skjoule
def test_session_guard_blocks_any_direct_wallet_construction():
    """The conftest session guard wraps JouleWallet.__init__, so it catches write
    paths that never go through settle() at all.

    Constructing the wallet is itself the write: _load_or_create_snapshot()
    creates joules.json. So this asserts on the constructor, not on mint().
    """
    from skcapstone.skjoule import JouleWallet

    with pytest.raises(ProductionWalletInTestError):
        JouleWallet("lumina", home=Path.home() / ".skcapstone")


def test_conftest_isolates_the_wallet_by_default():
    """Isolation is ON by default, not opt-in. Opt-in isolation fails exactly
    when someone forgets, which is the case that matters."""
    override = os.environ.get(joules.WALLET_HOME_ENV)
    assert override, f"{joules.WALLET_HOME_ENV} is not set by conftest"
    resolved = Path(override).expanduser().resolve()
    assert resolved not in joules._production_roots()
    assert Path.home() / ".skcapstone" != resolved
