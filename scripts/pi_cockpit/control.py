#!/usr/bin/env python3
"""The pi-cockpit control pane: a live table of every card being driven by a
sibling `run_card.py` pane in this tmux session.

Reads nothing but the status directory the launcher (`pi-cockpit.sh`) told
every pane to write to -- it never touches the coord board, git, or the
harness itself. Purely a dashboard over `status.py`'s files.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import status  # noqa: E402

_OUTCOME_LABEL = {
    "pr_ready": "PR READY",
    "pr_ready_salvage": "PR READY (salvage)",
    "failed": "FAILED",
}


def _fmt_elapsed(row: dict) -> str:
    started = row.get("started") or row.get("updated") or time.time()
    end = row.get("updated") if row.get("phase") == "done" else time.time()
    secs = max(0, int(end - started))
    return f"{secs // 60:02d}:{secs % 60:02d}"


def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def render(status_dir: Path, cards: list[str], config_path: str) -> str:
    rows = {r["card"]: r for r in status.read_all(status_dir)}
    width = shutil.get_terminal_size((100, 24)).columns
    lines = []
    lines.append("PI COCKPIT -- manual, human-scheduled skharness autopilot driver")
    lines.append(
        f"config={config_path}  status_dir={status_dir}  "
        f"panes={len(cards)}  {time.strftime('%H:%M:%S')}"
    )
    lines.append(
        "fleet_dispatch is FROZEN (card P6 / 08963fbb) -- this cockpit never calls "
        "it; Chef is the scheduler. NOTHING here ever merges."
    )
    lines.append("-" * min(width, 100))
    header = f"{'CARD':10} {'MODEL':26} {'PHASE':20} {'ELAPSED':8} {'OUTCOME':22}"
    lines.append(header)
    lines.append("-" * min(width, 100))
    for card in cards:
        row = rows.get(card)
        if row is None:
            lines.append(f"{card:10} {'(starting...)':26}")
            continue
        model = row.get("model") or "(routing...)"
        phase = row.get("phase") or ""
        elapsed = _fmt_elapsed(row)
        outcome = row.get("outcome")
        outcome_label = _OUTCOME_LABEL.get(outcome, "") if outcome else ""
        lines.append(f"{card:10} {model:26} {phase:20} {elapsed:8} {outcome_label:22}")
        why = row.get("reason")
        if why:
            lines.append(f"    why: {_truncate(why, min(width, 100) - 9)}")
        if outcome == "failed" and row.get("detail"):
            lines.append(f"    -> {_truncate(row['detail'], min(width, 100) - 9)}")
        if outcome in ("pr_ready", "pr_ready_salvage"):
            branch = row.get("branch") or ""
            pr = row.get("pr_url") or "(see `gh pr list`)"
            lines.append(f"    -> branch={branch}  PR={pr}")
    lines.append("-" * min(width, 100))
    done = sum(1 for r in rows.values() if r.get("phase") == "done")
    lines.append(f"{done}/{len(cards)} panes terminal. Ctrl-C to stop watching "
                 "(panes keep running).")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status-dir", required=True, type=Path)
    ap.add_argument("--cards", required=True, nargs="+", help="card ids, one per pane")
    ap.add_argument("--config", default="", help="autopilot config path, display only")
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args(argv)

    try:
        while True:
            print("\033[2J\033[H", end="")  # clear + home, so this reads like a dashboard
            print(render(args.status_dir, args.cards, args.config), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
