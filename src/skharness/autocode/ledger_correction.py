"""Correct the autopilot cost ledger for pytest fixture contamination, without
touching it.

WHAT HAPPENED
-------------
``autopilot_cost.record_run`` had no test-mode guard. Two suites reached it with
no isolation and appended fixture rows to the operator's live, Syncthing-synced
``~/.skcapstone/autopilot-cost/ledger.jsonl``:

* the agent-run bridge tests, card ``task-abc`` against repo ``skrender``, run
  ids ``airun-task-abc-<stamp>``;
* the orchestrator tests as they exist in the ``skos`` repo, cards ``t-1 t-2
  t-B keep`` against repo ``skos``, run ids ``r1 rc rr``. ``skos`` still carries
  the pre-extraction copy of those tests and drives this package through the
  ``skos.autopilot.orchestrator`` shim, with none of this repo's isolation
  fixtures. See ``docs/S29-cost-ledger-leak-attribution.md``.

The leak is fixed at the writer
(:func:`autopilot_cost.assert_not_production_ledger_in_test`); this module
corrects the record it left behind.

WHY THIS IS A SIDECAR AND NOT AN EDIT
-------------------------------------
Same posture as :mod:`wallet_correction`, and for the same reason. The ledger is
append-only production state synced by Syncthing to several machines. Rewriting
it in place would be a second unlogged corruption of the exact store being
corrected, and it would destroy the evidence of the first. So NOTHING here
writes to the ledger. The correction is published beside it.

WHY THIS ONE IS A SCALAR SET AND NOT A PER-ROW SERIES
-----------------------------------------------------
The wallet needed a per-row series because ``balance_after`` is a running total,
so one fabricated mint poisons every later row. The cost ledger carries no
running total: each row is independent and everything downstream
(``day_total``, ``overview``, the daily-cap alert) is a plain sum over a
selection. The correct correction is therefore the corrected AGGREGATES, per day
and overall, which is what this module publishes.

WHAT THE CORRECTION TURNED OUT TO BE
------------------------------------
Every row in the live ledger matches a fixture signature. 253 of 253 at the time
of writing. The corrected ledger is empty and every corrected aggregate is zero:
this store has never recorded a real autopilot run. That is worth stating
plainly rather than burying, because it is also the reason nobody noticed for
weeks. The fabricated rows all carry ``tokens=0`` and ``cost_usd=0.0``, so they
never moved a total, never tripped the daily cap, and never looked wrong to any
consumer that reads the aggregates instead of the row count.

No em/en dashes anywhere (SKWorld hard rule).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: Card ids that only a pytest fixture ever produces. Genuine coord card ids are
#: 8-hex handles (``60245d49``), so this set is disjoint from anything real by
#: construction rather than by luck. ``task-abc`` belongs to the agent-run bridge
#: tests; the rest belong to the orchestrator tests in both repos, including the
#: ``t-0``/``t-3``/``t-9``/``t-A`` cards that have never actually leaked but
#: would be the next ones to.
FIXTURE_CARD_IDS = frozenset({
    "task-abc",
    "t-0", "t-1", "t-2", "t-3", "t-9", "t-A", "t-B",
    "keep", "drop", "stale", "target", "other",
})

#: Run ids the orchestrator fixtures hardcode. Genuine orchestrator runs stamp a
#: UTC timestamp (``20260817T031234Z``) and the bridge stamps
#: ``airun-<card>-<stamp>``, so no real run can produce one of these.
FIXTURE_RUN_IDS = frozenset({"r1", "rc", "rr", "rp", "rk", "rdry"})

#: The bridge fixtures build a real-looking run id, so they are matched by prefix
#: instead. The prefix embeds the fixture card id, which is what keeps it tight.
FIXTURE_RUN_ID_PREFIX = "airun-task-abc-"

#: Published next to the ledger, never inside it.
SIDECAR_NAME = "ledger.correction.json"

DEFAULT_LEDGER = Path.home() / ".skcapstone/autopilot-cost/ledger.jsonl"


# --------------------------------------------------------------------------- #
# classifier                                                                   #
# --------------------------------------------------------------------------- #

def is_fixture_row(row: dict) -> bool:
    """True when ``row`` is pytest output.

    CONJUNCTIVE on purpose, same discipline as ``wallet_correction.is_fabricated``
    and for the same reason: a classifier that over-matches does not merely miss
    the correction, it corrupts it, by deleting real runs from the corrected
    aggregates and understating what the fleet actually spent. A row must carry

    * a ``card_id`` in :data:`FIXTURE_CARD_IDS`, AND
    * a ``run_id`` in :data:`FIXTURE_RUN_IDS`, or one starting with
      :data:`FIXTURE_RUN_ID_PREFIX`.

    CAN A GENUINE ROW MATCH? It would take a real coord card literally named
    ``t-1`` or ``keep`` that also ran under a run id literally named ``r1``.
    Coord ids are 8-hex and run ids are timestamps, so both halves would have to
    be hand-forged. Measured against the live ledger at the time of writing: 253
    of 253 rows match, and every one of them carries ``tokens=0``,
    ``cost_usd=0.0`` and a repo of ``skos`` or ``skrender``, which no real run
    does.

    Deliberately NOT part of the signature: ``repo``, ``tokens`` and
    ``cost_usd``. Matching on repo would break the moment a fixture used a
    different tag, and matching on a zero cost would quietly reclassify any
    future fixture that happens to stamp a number. The identity of the row is
    the card and the run; the rest is payload.
    """
    if row.get("card_id") not in FIXTURE_CARD_IDS:
        return False
    run_id = row.get("run_id")
    if not isinstance(run_id, str):
        return False
    return run_id in FIXTURE_RUN_IDS or run_id.startswith(FIXTURE_RUN_ID_PREFIX)


def count_fixture_rows(path: Path | str) -> int:
    """How many fixture rows the ledger at ``path`` currently holds.

    Tolerates a missing file (0) and skips malformed lines, because this is the
    read the session guard in ``tests/conftest.py`` runs at session start and
    session end and it must never be the thing that breaks a suite. READ ONLY.
    """
    p = Path(path)
    if not p.exists():
        return 0
    total = 0
    try:
        with p.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and is_fixture_row(row):
                    total += 1
    except OSError:
        return total
    return total


# --------------------------------------------------------------------------- #
# result types                                                                 #
# --------------------------------------------------------------------------- #

@dataclass
class DayCorrection:
    """One date's recorded totals beside what they should have been."""

    date: str
    recorded_runs: int
    corrected_runs: int
    recorded_tokens: int
    corrected_tokens: int
    recorded_cost_usd: float
    corrected_cost_usd: float
    recorded_joules: int
    corrected_joules: int


@dataclass
class Correction:
    """The published correction for one cost ledger."""

    total_rows: int = 0
    fixture_rows: int = 0
    genuine_rows: int = 0
    recorded_tokens: int = 0
    corrected_tokens: int = 0
    recorded_cost_usd: float = 0.0
    corrected_cost_usd: float = 0.0
    recorded_joules: int = 0
    corrected_joules: int = 0
    first_fixture_timestamp: str | None = None
    last_fixture_timestamp: str | None = None
    fixture_cards: dict = field(default_factory=dict)
    days: list[DayCorrection] = field(default_factory=list)

    @property
    def fraction_fixture(self) -> float:
        """Share of the ledger's ROWS that are pytest output."""
        return (self.fixture_rows / self.total_rows) if self.total_rows else 0.0

    def summary(self) -> dict:
        return {
            "total_rows": self.total_rows,
            "fixture_rows": self.fixture_rows,
            "genuine_rows": self.genuine_rows,
            "fraction_fixture": round(self.fraction_fixture, 6),
            "recorded_tokens": self.recorded_tokens,
            "corrected_tokens": self.corrected_tokens,
            "recorded_cost_usd": round(self.recorded_cost_usd, 6),
            "corrected_cost_usd": round(self.corrected_cost_usd, 6),
            "recorded_joules": self.recorded_joules,
            "corrected_joules": self.corrected_joules,
            "first_fixture_timestamp": self.first_fixture_timestamp,
            "last_fixture_timestamp": self.last_fixture_timestamp,
            "fixture_cards": dict(sorted(self.fixture_cards.items())),
            "days": len(self.days),
        }


# --------------------------------------------------------------------------- #
# correction                                                                   #
# --------------------------------------------------------------------------- #

def load_ledger(path: Path | str) -> list[dict]:
    """Read the append-only ledger. READ ONLY: nothing here ever opens it for
    write. Malformed lines are skipped rather than fatal, matching
    ``autopilot_cost._read_ledger``, so a correction can still be published for
    a ledger with one bad line in it."""
    rows: list[dict] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _tokens(row: dict) -> int:
    try:
        return int(row.get("tokens") or 0)
    except (TypeError, ValueError):
        return 0


def _usd(row: dict) -> float:
    try:
        return float(row.get("cost_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _joules(row: dict) -> int:
    try:
        return int(row.get("joules") or 0)
    except (TypeError, ValueError):
        return 0


def correct_ledger(rows: list[dict]) -> Correction:
    """Build the corrected aggregates for ``rows``."""
    result = Correction(total_rows=len(rows))
    per_day: dict[str, dict] = defaultdict(lambda: {
        "recorded_runs": 0, "corrected_runs": 0,
        "recorded_tokens": 0, "corrected_tokens": 0,
        "recorded_cost_usd": 0.0, "corrected_cost_usd": 0.0,
        "recorded_joules": 0, "corrected_joules": 0,
    })

    for row in rows:
        fixture = is_fixture_row(row)
        tokens, usd, joules = _tokens(row), _usd(row), _joules(row)
        date = str(row.get("date") or (row.get("ts") or "")[:10] or "unknown")
        bucket = per_day[date]

        result.recorded_tokens += tokens
        result.recorded_cost_usd += usd
        result.recorded_joules += joules
        bucket["recorded_runs"] += 1
        bucket["recorded_tokens"] += tokens
        bucket["recorded_cost_usd"] += usd
        bucket["recorded_joules"] += joules

        if fixture:
            result.fixture_rows += 1
            card = str(row.get("card_id"))
            result.fixture_cards[card] = result.fixture_cards.get(card, 0) + 1
            ts = row.get("ts")
            if isinstance(ts, str) and ts:
                if result.first_fixture_timestamp is None:
                    result.first_fixture_timestamp = ts
                result.last_fixture_timestamp = ts
            continue

        result.genuine_rows += 1
        result.corrected_tokens += tokens
        result.corrected_cost_usd += usd
        result.corrected_joules += joules
        bucket["corrected_runs"] += 1
        bucket["corrected_tokens"] += tokens
        bucket["corrected_cost_usd"] += usd
        bucket["corrected_joules"] += joules

    result.days = [
        DayCorrection(
            date=date,
            recorded_runs=b["recorded_runs"], corrected_runs=b["corrected_runs"],
            recorded_tokens=b["recorded_tokens"], corrected_tokens=b["corrected_tokens"],
            recorded_cost_usd=round(b["recorded_cost_usd"], 6),
            corrected_cost_usd=round(b["corrected_cost_usd"], 6),
            recorded_joules=b["recorded_joules"], corrected_joules=b["corrected_joules"],
        )
        for date, b in sorted(per_day.items())
    ]
    return result


def write_sidecar(ledger: Path | str, *, sidecar: Path | str | None = None) -> Path:
    """Compute the correction for ``ledger`` and publish it BESIDE the ledger.

    Returns the sidecar path. The ledger is opened for reading only.
    """
    ledger = Path(ledger)
    target = Path(sidecar) if sidecar is not None else ledger.parent / SIDECAR_NAME
    if target.resolve() == ledger.resolve():
        raise ValueError("refusing to write the correction into the ledger itself")

    correction = correct_ledger(load_ledger(ledger))
    payload = {
        "ledger": str(ledger),
        "note": (
            "Correction for pytest fixture contamination. The ledger itself is "
            "append-only and unmodified. corrected_* is what the aggregate would "
            "have been had the fixture rows never been written. See "
            "docs/S29-cost-ledger-leak-attribution.md."
        ),
        "summary": correction.summary(),
        "days": [asdict(d) for d in correction.days],
    }
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


def summary_text(correction: Correction) -> str:
    """Human readable correction summary."""
    pct = correction.fraction_fixture * 100
    lines = [
        "AUTOPILOT COST LEDGER CORRECTION (pytest fixture contamination)",
        f"  ledger rows          : {correction.total_rows}",
        f"  fixture rows         : {correction.fixture_rows} ({pct:.1f}% of rows)",
        f"  genuine rows         : {correction.genuine_rows}",
        f"  recorded tokens      : {correction.recorded_tokens}",
        f"  corrected tokens     : {correction.corrected_tokens}",
        f"  recorded cost (USD)  : {correction.recorded_cost_usd:.4f}",
        f"  corrected cost (USD) : {correction.corrected_cost_usd:.4f}",
        f"  recorded joules      : {correction.recorded_joules}",
        f"  corrected joules     : {correction.corrected_joules}",
    ]
    if correction.first_fixture_timestamp:
        lines.append(
            f"  contaminated range   : {correction.first_fixture_timestamp} "
            f"to {correction.last_fixture_timestamp}"
        )
    if correction.fixture_cards:
        cards = ", ".join(f"{k}={v}" for k, v in sorted(correction.fixture_cards.items()))
        lines.append(f"  fixture cards        : {cards}")
    if correction.total_rows and correction.genuine_rows == 0:
        lines.append("  VERDICT              : every row is pytest output; this "
                     "ledger has never recorded a real run")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    """Entry point: ``python -m skharness.autocode.ledger_correction``.

    Read only by default. ``--write-sidecar`` publishes the corrected aggregates
    next to the ledger. Neither mode ever writes to the ledger.
    """
    parser = argparse.ArgumentParser(
        prog="skharness-ledger-correction",
        description=(
            "Publish the fixture-contamination correction for an autopilot cost "
            "ledger. Never modifies the ledger."
        ),
    )
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="ledger path")
    parser.add_argument("--sidecar", default=None, help="override the sidecar path")
    parser.add_argument("--json", action="store_true", help="emit the summary as JSON")
    parser.add_argument("--write-sidecar", action="store_true",
                        help="publish the corrected aggregates")
    args = parser.parse_args(argv)

    ledger = Path(args.ledger)
    if not ledger.exists():
        print(f"no such ledger: {ledger}", file=sys.stderr)
        return 2

    correction = correct_ledger(load_ledger(ledger))

    if args.write_sidecar:
        target = write_sidecar(ledger, sidecar=args.sidecar)
        if not args.json:
            print(f"wrote {target}")

    if args.json:
        print(json.dumps(correction.summary(), indent=2))
    else:
        print(summary_text(correction))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
