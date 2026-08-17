"""Two concurrent settlements must both land in the balance.

Background, found 2026-08-16 by the S27 wallet-correction work and carded as
S28. ``balance_after`` in the operator's live ledger is not a clean running
total of the ``amount`` column. Two of the breaks are lost updates: at
``2026-08-15T08:10:28.571`` two mints 35 MICROSECONDS apart both recorded
``balance_after=123309``, and at ``2026-08-17T02:39:51`` two mints 105 ms apart
both recorded ``balance_after=162168``. 25 J and 50 J of genuine, earned credit
are simply absent from the balance. The append-only journal survived both
races; the balance field did not.

The mechanism. ``JouleWallet`` loads its snapshot ONCE, in ``__init__``, and
guards mutations with a ``threading.Lock`` that is an INSTANCE attribute.
``settle()`` constructs a fresh wallet per call, so two settlements hold two
wallets, two snapshots and two locks that know nothing of each other. Each
reads balance B, each writes B+its own mint, and the last writer wins. Multiple
sessions run on one box, so the same shape recurs across processes where no
in-process lock could ever help.

WHY THE INTERLEAVING HERE IS DETERMINISTIC, not hoped for. A test that merely
starts two threads and hopes they collide is worthless: it would pass on a
green machine and prove nothing. So the read point itself is gated.
``_load_or_create_snapshot`` is patched to rendezvous through :class:`_ReadGate`
immediately AFTER it has read the balance. Each racing thread sets its own
event and waits on the other's:

  AGAINST THE BUG   neither thread holds anything, so both reach the gate, both
                    events are set, both waits return at once, and both threads
                    are now holding the SAME stale balance. The lost update is
                    forced, not sampled.
  AGAINST THE FIX   the first thread holds the settle lock, so the second never
                    reaches the gate. The first waits out ``GATE_TIMEOUT`` and
                    proceeds; the second then reads a FRESH balance and finds
                    the first thread's event already set, so it passes the gate
                    immediately. Both mints land. The gate waits with a timeout
                    rather than a barrier for exactly this reason: a
                    ``threading.Barrier`` would deadlock against a correct
                    implementation, which is the same as having no test.

The cost of the fix passing is therefore one ``GATE_TIMEOUT``, deliberately
short.
"""

from __future__ import annotations

import threading

import pytest

from skharness.autocode import joules
from skharness.autocode.joules import BuildUsage, settle

requires_skjoule = pytest.mark.skipif(
    not joules._skjoule_available(),
    reason="optional sibling skcapstone/skjoule not installed",
)

#: How long a thread waits at the gate for its counterpart. Only paid once, and
#: only by a CORRECT implementation, where the second thread is still queued on
#: the settle lock and so can never arrive.
GATE_TIMEOUT = 2.0

#: Thread names of the two racing settlers. The gate keys off the thread name so
#: that any other wallet load in the process (the seed mint, the final read) is
#: not gated at all.
RACERS = ("settler-a", "settler-b")


class _ReadGate:
    """Rendezvous placed immediately after a wallet reads its balance.

    ``arrive()`` sets the calling thread's event and then waits, with a timeout,
    on every other racer's event. Threads that are not racers pass straight
    through.
    """

    def __init__(self, names, timeout: float = GATE_TIMEOUT) -> None:
        self._events = {name: threading.Event() for name in names}
        self._timeout = timeout
        self.arrivals: list[str] = []
        self._mutex = threading.Lock()

    def arrive(self) -> None:
        me = threading.current_thread().name
        mine = self._events.get(me)
        if mine is None:
            return
        with self._mutex:
            self.arrivals.append(me)
        mine.set()
        for name, event in self._events.items():
            if name != me:
                event.wait(timeout=self._timeout)


@requires_skjoule
def test_two_concurrent_settlements_both_land_in_the_balance(tmp_path, monkeypatch):
    """The whole card. Two settlements race; the balance must carry both mints."""
    from skcapstone.skjoule import JouleWallet, replay_balance

    agent = "s28-race"

    # Seed an opening balance BEFORE the gate is installed, so the seed mint is
    # an ordinary uncontended write.
    JouleWallet(agent, home=tmp_path).mint(1000, description="opening balance")
    opening = JouleWallet(agent, home=tmp_path).balance
    assert opening == 1000

    gate = _ReadGate(RACERS)
    original_load = JouleWallet._load_or_create_snapshot

    def gated_load(self):
        snapshot = original_load(self)
        gate.arrive()
        return snapshot

    monkeypatch.setattr(JouleWallet, "_load_or_create_snapshot", gated_load)

    results: dict[str, object] = {}
    failures: dict[str, BaseException] = {}

    def run(name: str, priority: str) -> None:
        try:
            results[name] = settle(
                agent,
                f"s28-{name}",
                priority=priority,
                score=5,
                # cost_usd 0 keeps this a pure mint: the spend leg would only
                # add noise to what is being proven.
                usage=BuildUsage(model="test", output_tokens=10, cost_usd=0.0),
                home=tmp_path,
            )
        except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
            failures[name] = exc

    threads = [
        threading.Thread(target=run, args=("settler-a", "critical"), name="settler-a"),
        threading.Thread(target=run, args=("settler-b", "low"), name="settler-b"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), f"{thread.name} never finished; the gate deadlocked"

    assert not failures, f"a settler raised: {failures}"
    assert sorted(gate.arrivals) == sorted(RACERS), (
        f"both settlers must have reached the read gate, saw {gate.arrivals}"
    )

    first, second = results["settler-a"], results["settler-b"]
    assert first.recorded and second.recorded
    # Different priorities mint different amounts, so a lost update cannot hide
    # behind two identical numbers.
    assert first.minted > 0 and second.minted > 0
    assert first.minted != second.minted

    expected = opening + first.minted + second.minted
    fresh = JouleWallet(agent, home=tmp_path)
    assert fresh.balance == expected, (
        f"lost update: balance {fresh.balance} should be {expected} "
        f"({opening} + {first.minted} + {second.minted})"
    )
    assert fresh.total_minted == 1000 + first.minted + second.minted

    # The journal was always the durable side. After the fix the stored balance
    # agrees with it, which is the property the live ledger lost twice.
    assert replay_balance(agent, home=tmp_path) == expected


@requires_skjoule
def test_settle_lock_is_scoped_to_one_agent(tmp_path):
    """Two DIFFERENT agents settling at once must not serialise on each other.

    A lock wide enough to be correct but narrow enough not to be a fleet-wide
    chokepoint. Without this, every concurrent build in the fleet would queue
    behind one file no matter whose wallet it touched.
    """
    a = joules._settle_lock_path("agent-one", tmp_path)
    b = joules._settle_lock_path("agent-two", tmp_path)
    assert a != b
    with joules.settle_lock("agent-one", home=tmp_path):
        # Uncontended for a different agent, so a short timeout is plenty.
        with joules.settle_lock("agent-two", home=tmp_path, timeout=2.0):
            pass


@requires_skjoule
def test_settle_lock_excludes_a_second_holder(tmp_path):
    """Positive control for the lock itself, independent of settle().

    Without this, a lock that silently failed to lock anything would leave the
    race test passing for the wrong reason.
    """
    acquired_second = threading.Event()

    def second_holder():
        # Short timeout: this MUST fail to acquire while the main thread holds it.
        with joules.settle_lock("agent-one", home=tmp_path, timeout=0.3) as locked:
            if locked:
                acquired_second.set()

    with joules.settle_lock("agent-one", home=tmp_path) as locked:
        assert locked, "the first holder must actually hold the lock"
        thread = threading.Thread(target=second_holder)
        thread.start()
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert not acquired_second.is_set(), "two holders got the same wallet lock at once"
