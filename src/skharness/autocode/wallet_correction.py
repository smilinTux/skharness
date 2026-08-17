"""Correct the joule wallet for pytest fixture contamination, without touching it.

WHAT HAPPENED
-------------
``joules.settle()`` had no test-mode guard. The finalize tests exercise the twin
gate PASS path, so for roughly three weeks every run of the suite minted real,
well formed joules into the operator's live wallet. The rows carry the pytest
fixture task ref ``t1`` in their description and are otherwise indistinguishable
from genuine earnings. The leak is fixed (``tests/conftest.py`` autouse fixture
``_isolate_joule_wallet``); this module corrects the record it left behind.

WHY THIS IS A SIDECAR AND NOT AN EDIT
-------------------------------------
``transactions.jsonl`` is append-only production state, synced by Syncthing to
several machines, and it is the substrate of the 19 wallet reconciliation. The
operator rejected both partitioning and deleting: rewriting rows would break
``balance_after`` continuity and invalidate the reconciliation, and it would be a
second unlogged corruption of the exact ledger being corrected. So NOTHING here
writes to the wallet. The correction is published alongside it.

WHY A PER-ROW SERIES AND NOT A SCALAR
-------------------------------------
``balance_after`` is a running total. The first fabricated mint poisons every
subsequent row, genuine ones included, not just the fabricated rows. A single
scalar "subtract N joules" is only correct for the CURRENT balance and is
actively misleading for any historical point. The correction is therefore a
series: for each row, what the balance would have been had the fixture mints
never happened.

    corrected_balance[i] = recorded_balance[i] - cumulative_fabricated[i]

Note the shape of that definition. It corrects the recorded balance rather than
recomputing one from the amounts, and that is deliberate: the recorded balance is
authoritative and carries its own history (including an intentional reconciling
entry and two concurrency lost-updates, see :class:`ContinuityAnomaly`).
Recomputing from amounts would silently undo those as a side effect of fixing
something else. This module corrects exactly one defect and reports the rest.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: The exact description every fixture mint carries. ``t1`` is the task ref of
#: the pytest fixture card, hardcoded in the finalize tests.
FIXTURE_DESCRIPTION = "autocode task_complete t1"

#: The fixture card is a constant, so every leaked mint is the same shape: a
#: ``task_complete`` at base 25 J x medium priority x the fixture quality score.
FIXTURE_AMOUNT = 75
FIXTURE_KIND = "mint"
FIXTURE_COUNTERPARTY = "economy"

#: The last fixture row observed in the live wallet. The guard landed between
#: this row and the next, so a row carrying the marker AFTER this timestamp did
#: not come from the leak: it is either a genuine card literally named ``t1`` or
#: a regression of the guard. Either way the correction must not claim it, which
#: is what bounds the classifier's over-match risk. See ``is_fabricated``.
LAST_FIXTURE_TIMESTAMP = "2026-08-17T01:48:06.760525+00:00"

#: Published next to the wallet, never inside it.
SIDECAR_NAME = "transactions.correction.json"

DEFAULT_WALLET = Path.home() / ".skcapstone/agents/lumina/wallet/transactions.jsonl"


# --------------------------------------------------------------------------- #
# classifier                                                                   #
# --------------------------------------------------------------------------- #

def is_fabricated(row: dict, *, cutoff: str = LAST_FIXTURE_TIMESTAMP) -> bool:
    """True when ``row`` is a pytest fixture mint.

    The signature is CONJUNCTIVE on purpose. The marker string alone would be a
    loose match, and a classifier that over-matches does not merely miss the
    correction, it corrupts it: every falsely flagged row would subtract real
    joules from every later balance in the series. So a row must satisfy all of:

    * ``description`` exactly equals :data:`FIXTURE_DESCRIPTION`
    * ``kind`` is a mint, ``amount`` is exactly :data:`FIXTURE_AMOUNT`
    * ``counterparty`` is the economy
    * ``timestamp`` is at or before ``cutoff``

    CAN A GENUINE ROW MATCH? The residual risk is not zero, but it is bounded and
    small. Genuine autocode mints are written with a different template that
    carries the card id, ``"[<id>] Task completed: <title>"``, so a real card
    cannot produce this exact string through the normal path. It would take a
    card whose description was literally ``autocode task_complete t1``, minting
    exactly 75 J, before the cutoff. Measured against the live ledger at the time
    of writing: 1,452 rows match the full signature, all of them the identical
    string and amount, and ZERO rows outside that set contain the token ``t1``
    anywhere in their description. The cutoff caps the exposure going forward,
    since the leak is fixed and no further fixture rows can be written.
    """
    description = row.get("description")
    if not isinstance(description, str) or description.strip() != FIXTURE_DESCRIPTION:
        return False
    if row.get("kind") != FIXTURE_KIND:
        return False
    if row.get("amount") != FIXTURE_AMOUNT:
        return False
    if row.get("counterparty") != FIXTURE_COUNTERPARTY:
        return False
    timestamp = row.get("timestamp")
    if not isinstance(timestamp, str) or timestamp > cutoff:
        return False
    return True


# --------------------------------------------------------------------------- #
# result types                                                                 #
# --------------------------------------------------------------------------- #

@dataclass
class CorrectedRow:
    """One ledger row, with what its balance should have been."""

    index: int
    timestamp: str
    kind: str
    amount: int
    description: str
    fabricated: bool
    recorded_balance: int
    corrected_balance: int
    cumulative_fabricated: int


@dataclass
class ContinuityAnomaly:
    """A row whose recorded balance does not equal previous + delta.

    NOT caused by the fixture leak and NOT repaired here. Two kinds exist in the
    live ledger: concurrent ``settle()`` calls that read the same balance and
    both wrote it back (a lost update, genuine joules never credited), and one
    deliberate reconciling entry that adjusts the journal against an
    authoritative snapshot and so leaves the balance flat by design. They are
    surfaced because the corrected series inherits them.
    """

    index: int
    timestamp: str
    kind: str
    amount: int
    recorded_balance: int
    expected_balance: int
    description: str


@dataclass
class Correction:
    """The published correction for one ledger."""

    rows: list[CorrectedRow] = field(default_factory=list)
    total_rows: int = 0
    fabricated_count: int = 0
    fabricated_joules: int = 0
    recorded_balance: int = 0
    corrected_balance: int = 0
    first_fabricated_index: int | None = None
    first_fabricated_timestamp: str | None = None
    last_fabricated_timestamp: str | None = None
    min_corrected_balance: int = 0
    spends_requiring_fabricated_joules: int = 0
    continuity_anomalies: list[ContinuityAnomaly] = field(default_factory=list)

    @property
    def fraction_fabricated(self) -> float:
        """Share of the ledger's ROWS that are fixture output."""
        return (self.fabricated_count / self.total_rows) if self.total_rows else 0.0

    def summary(self) -> dict:
        return {
            "total_rows": self.total_rows,
            "fabricated_count": self.fabricated_count,
            "fabricated_joules": self.fabricated_joules,
            "recorded_balance": self.recorded_balance,
            "corrected_balance": self.corrected_balance,
            "fraction_fabricated": round(self.fraction_fabricated, 6),
            "first_fabricated_timestamp": self.first_fabricated_timestamp,
            "last_fabricated_timestamp": self.last_fabricated_timestamp,
            "min_corrected_balance": self.min_corrected_balance,
            "spends_requiring_fabricated_joules": self.spends_requiring_fabricated_joules,
            "continuity_anomalies": len(self.continuity_anomalies),
            "fixture_description": FIXTURE_DESCRIPTION,
            "cutoff": LAST_FIXTURE_TIMESTAMP,
        }


# --------------------------------------------------------------------------- #
# correction                                                                   #
# --------------------------------------------------------------------------- #

def load_ledger(path: Path | str) -> list[dict]:
    """Read the append-only ledger. READ ONLY: nothing here ever opens it for write."""
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _delta(row: dict) -> int:
    amount = row.get("amount", 0) or 0
    return amount if row.get("kind") == FIXTURE_KIND else -abs(amount)


def correct_ledger(rows: list[dict], *, cutoff: str = LAST_FIXTURE_TIMESTAMP) -> Correction:
    """Build the per-row corrected balance series for ``rows``."""
    result = Correction(total_rows=len(rows))
    cumulative = 0
    previous_recorded = 0

    for index, row in enumerate(rows):
        recorded = row.get("balance_after", 0)
        fabricated = is_fabricated(row, cutoff=cutoff)

        if fabricated:
            cumulative += row.get("amount", 0)
            result.fabricated_count += 1
            if result.first_fabricated_index is None:
                result.first_fabricated_index = index
                result.first_fabricated_timestamp = row.get("timestamp")
            result.last_fabricated_timestamp = row.get("timestamp")

        corrected = recorded - cumulative

        expected = previous_recorded + _delta(row)
        if expected != recorded:
            result.continuity_anomalies.append(
                ContinuityAnomaly(
                    index=index,
                    timestamp=row.get("timestamp", ""),
                    kind=row.get("kind", ""),
                    amount=row.get("amount", 0),
                    recorded_balance=recorded,
                    expected_balance=expected,
                    description=row.get("description", "") or "",
                )
            )

        if index == 0 or corrected < result.min_corrected_balance:
            result.min_corrected_balance = corrected
        if row.get("kind") != FIXTURE_KIND and corrected < 0:
            result.spends_requiring_fabricated_joules += 1

        result.rows.append(
            CorrectedRow(
                index=index,
                timestamp=row.get("timestamp", ""),
                kind=row.get("kind", ""),
                amount=row.get("amount", 0),
                description=row.get("description", "") or "",
                fabricated=fabricated,
                recorded_balance=recorded,
                corrected_balance=corrected,
                cumulative_fabricated=cumulative,
            )
        )
        previous_recorded = recorded

    result.fabricated_joules = cumulative
    result.recorded_balance = rows[-1].get("balance_after", 0) if rows else 0
    result.corrected_balance = result.recorded_balance - cumulative
    return result


def write_sidecar(
    wallet: Path | str,
    *,
    sidecar: Path | str | None = None,
    cutoff: str = LAST_FIXTURE_TIMESTAMP,
) -> Path:
    """Compute the correction for ``wallet`` and publish it BESIDE the wallet.

    Returns the sidecar path. The wallet is opened for reading only.
    """
    wallet = Path(wallet)
    target = Path(sidecar) if sidecar is not None else wallet.parent / SIDECAR_NAME
    if target.resolve() == wallet.resolve():
        raise ValueError("refusing to write the correction into the wallet itself")

    correction = correct_ledger(load_ledger(wallet), cutoff=cutoff)
    payload = {
        "wallet": str(wallet),
        "note": (
            "Correction for pytest fixture contamination. The wallet itself is "
            "append-only and unmodified. corrected_balance is what balance_after "
            "would have been had the fixture mints never been written."
        ),
        "summary": correction.summary(),
        "continuity_anomalies": [asdict(a) for a in correction.continuity_anomalies],
        "rows": [asdict(r) for r in correction.rows],
    }
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


def summary_text(correction: Correction) -> str:
    """Human readable correction summary."""
    pct = correction.fraction_fabricated * 100
    lines = [
        "JOULE WALLET CORRECTION (pytest fixture contamination)",
        f"  ledger rows          : {correction.total_rows}",
        f"  fabricated mints     : {correction.fabricated_count} ({pct:.1f}% of rows)",
        f"  fabricated joules    : {correction.fabricated_joules}",
        f"  recorded balance     : {correction.recorded_balance}",
        f"  corrected balance    : {correction.corrected_balance}",
    ]
    if correction.first_fabricated_timestamp:
        lines.append(
            f"  contaminated range   : {correction.first_fabricated_timestamp} "
            f"to {correction.last_fabricated_timestamp}"
        )
    lines.append(
        f"  spends needing fake J: {correction.spends_requiring_fabricated_joules}"
    )
    if correction.continuity_anomalies:
        lines.append(
            f"  continuity anomalies : {len(correction.continuity_anomalies)} "
            "(pre-existing, NOT repaired by this correction)"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    """Entry point: ``python -m skharness.autocode.wallet_correction``.

    Read only by default. ``--write-sidecar`` publishes the per-row series next
    to the wallet. Neither mode ever writes to the wallet.
    """
    parser = argparse.ArgumentParser(
        prog="skharness-wallet-correction",
        description=(
            "Publish the fixture-contamination correction for a joule wallet. "
            "Never modifies the wallet."
        ),
    )
    parser.add_argument("--wallet", default=str(DEFAULT_WALLET), help="ledger path")
    parser.add_argument("--sidecar", default=None, help="override the sidecar path")
    parser.add_argument("--cutoff", default=LAST_FIXTURE_TIMESTAMP, help="last fixture row")
    parser.add_argument("--json", action="store_true", help="emit the summary as JSON")
    parser.add_argument(
        "--write-sidecar", action="store_true", help="publish the per-row series"
    )
    args = parser.parse_args(argv)

    wallet = Path(args.wallet)
    if not wallet.exists():
        print(f"no such wallet: {wallet}", file=sys.stderr)
        return 2

    correction = correct_ledger(load_ledger(wallet), cutoff=args.cutoff)

    if args.write_sidecar:
        target = write_sidecar(wallet, sidecar=args.sidecar, cutoff=args.cutoff)
        if not args.json:
            print(f"wrote {target}")

    if args.json:
        print(json.dumps(correction.summary(), indent=2))
    else:
        print(summary_text(correction))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
