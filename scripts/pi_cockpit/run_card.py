#!/usr/bin/env python3
"""One pane, one card, one pi-harness run -- the manual cockpit's worker.

This is the human-driven entry point promised by coord card P6 / `08963fbb`:
`fleet_dispatch` fails closed while the fleet is frozen, so no *scheduled*
placement can run right now. Nothing here calls `fleet_dispatch` or touches
`_freeze.json` -- Chef IS the scheduler, this script just drives the SAME
merge choke point (`skharness.autocode.engineering.EngineeringExecutor`) the
real fleet drives, by hand, for one card he picked.

What this does, in order, all real (no dry-run, no simulation):
  1. Read the card off the real coord board (read-only until the claim).
  2. Pick a skgateway model for it (model_routing.choose_model) and PRINT the
     choice and why, so Chef can disagree at a glance.
  3. Build a WorkItem and hand it to a REAL EngineeringExecutor bound to a
     real Board + a real per-run journal handle + the real digest module --
     the exact objects `orchestrator.build_executors` wires up, constructed
     here instead of by the (frozen) fleet dispatch path.
  4. `ex.run(item, harness)`: claims the card, builds it in an isolated
     `autopilot/<ref>` git worktree (EngineeringExecutor.make_worktree --
     never the shared checkout), runs the Ralph loop up to 4 rounds, grades
     each round behind the twin gate (LLM 5/5 + promise AND external CI
     green AND diff coverage), exactly as production does.
  5. On a genuine pass: `ex.finalize(item, result)` -- commits, pushes,
     opens a PR. `automerge_repos` is `[]` in every live config and this
     script never touches that list, so this NEVER merges; it only ever
     leaves a PR queued for human review, same as production.
  6. On a "salvage" outcome (grader inconclusive, CI green + coverage held):
     `run()` itself already committed, pushed, and opened a review PR
     (`_salvage_to_review`) -- also never auto-merged.
  7. On anything else (ci_red / no_op / a lost claim): FAILS loudly, in the
     pane, with the real notes/score/CI status. No green tick.

Terminal states are exactly two: "branch + diff ready for Chef" or "failed,
here is why" -- there is no third state and nothing here ever calls
`gh pr merge` or `git merge`.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import model_routing  # noqa: E402
import status  # noqa: E402  (local module, path set above)


def _load_task(card_id: str) -> dict:
    """The card as a plain dict, read straight off the real coord board.
    Read-only: does not claim, does not mutate anything on disk."""
    from skcapstone.coordination import Board
    from skcapstone.mcp_tools._helpers import _shared_root

    board = Board(_shared_root())
    for task in board.load_tasks():
        if task.id == card_id:
            return task.model_dump(mode="json")
    raise SystemExit(f"card {card_id!r} not found on the coord board")


def _resolve_repo_name(task: dict) -> str:
    names = [t.split(":", 1)[1] for t in (task.get("tags") or [])
             if t.startswith("repo:")]
    if len(names) != 1:
        raise SystemExit(
            f"card {task['id']} carries {len(names)} repo: tag(s) "
            f"({names!r}); this cockpit needs exactly one")
    return names[0]


def _pr_url(repo_path: str, branch: str) -> str | None:
    """Best-effort PR url lookup after finalize() (which opens the PR but
    does not hand the url back to its caller). Never raises: a lookup
    failure just means the pane prints '(see gh pr list)' instead of a url,
    it does not change what actually happened on GitHub."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "url", "-q", ".url"],
            cwd=repo_path, capture_output=True, text=True, timeout=30)
        return proc.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


class _ObservedHarness:
    """A thin passthrough over a real Harness that prints round-by-round
    progress into the pane and touches the status file per phase. It adds NO
    gate logic of its own -- every call is forwarded unchanged to `inner`;
    this exists purely so a human watching the pane can see the Ralph loop
    breathing instead of staring at a silent process for minutes."""

    def __init__(self, inner, status_dir: Path, card_id: str):
        self._inner = inner
        self._status_dir = status_dir
        self._card_id = card_id
        self._round = 0

    @property
    def name(self):
        return self._inner.name

    def capabilities(self):
        return self._inner.capabilities()

    def assess(self, brief):
        return self._inner.assess(brief)

    def decompose(self, brief):
        return self._inner.decompose(brief)

    def run_task(self, brief):
        self._round += 1
        print(f"\n=== round {self._round}: implement "
              f"({self._inner.name}/{self._inner.model}) ===", flush=True)
        status.write(self._status_dir, self._card_id,
                     phase=f"round {self._round}: build")
        t0 = time.monotonic()
        result = self._inner.run_task(brief)
        print(f"    implement done in {time.monotonic() - t0:.1f}s "
              f"ok={result.ok} tokens={result.tokens} "
              f"cost_usd={result.cost_usd:.4f}", flush=True)
        return result

    def grade(self, brief):
        print(f"--- round {self._round}: grade "
              f"(ci={brief.ci_status} diff_coverage={brief.diff_coverage}) "
              "---", flush=True)
        status.write(self._status_dir, self._card_id,
                     phase=f"round {self._round}: grade")
        t0 = time.monotonic()
        result = self._inner.grade(brief)
        note = (result.notes or "")[:300].replace("\n", " ")
        print(f"    grade done in {time.monotonic() - t0:.1f}s "
              f"score={result.score} notes={note!r}", flush=True)
        return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--card", required=True, help="coord task id, e.g. a7e3ca15")
    ap.add_argument("--status-dir", required=True, type=Path)
    ap.add_argument("--agent-name", default="pi-cockpit",
                     help="claim identity on the coord board (default: pi-cockpit)")
    ap.add_argument("--model", default=None,
                     help="override the routed model id (Chef disagreeing)")
    args = ap.parse_args(argv)

    from skcapstone.coordination import Board
    from skcapstone.mcp_tools._helpers import _shared_root

    from skharness.autocode import config as autocode_config
    from skharness.autocode import digest as digest_mod
    from skharness.autocode import journal as run_journal
    from skharness.autocode.adapters.pi import PiAdapter
    from skharness.autocode.engineering import EngineeringExecutor
    from skharness.autocode.types import ClaimRaced, WorkItem

    card = args.card
    status.write(args.status_dir, card, phase="loading card", model="", reason="")

    task = _load_task(card)
    repo_name = _resolve_repo_name(task)
    config = autocode_config.load()
    repo = config.repo_map.get(repo_name)
    if repo is None:
        status.write(args.status_dir, card, phase="done", outcome="failed",
                     detail=f"repo {repo_name!r} is not in this config's repo_map")
        print(f"FAILED: repo {repo_name!r} is not in repo_map "
              f"({sorted(config.repo_map)})", file=sys.stderr)
        return 1

    model, reason = (args.model, "operator override (--model)") \
        if args.model else model_routing.choose_model(task)

    print(f"card    : {card} -- {task.get('title', '')}")
    print(f"repo    : {repo_name}  ({repo.path})")
    print(f"priority: {task.get('priority')}   tags: {task.get('tags')}")
    print(f"model   : {model}")
    print(f"why     : {reason}")
    print(f"config  : harness={config.harness} base_url={config.harness_base_url}")
    print("-" * 72, flush=True)
    status.write(args.status_dir, card, phase="claiming", model=model, reason=reason,
                 title=task.get("title", ""), repo=repo_name)

    payload = dict(task)
    payload["acceptance"] = task.get("acceptance_criteria") or []
    payload["unblocked"] = True
    payload["verdict"] = "valid"
    item = WorkItem(kind="engineering", ref=card, source="coord",
                    repo=repo_name, payload=payload)

    board = Board(_shared_root())
    run_id = f"pi-cockpit-{card}-{int(time.time())}"
    handle = run_journal.handle(run_id)
    ex = EngineeringExecutor(config, board, handle, digest_mod,
                             agent_name=args.agent_name)

    real_harness = PiAdapter(
        model=model, base_url=config.harness_base_url,
        egress_hosts=config.mcp_endpoints, live_execution=config.live_execution,
        image=repo.sandbox_image or config.sandbox_image,
        max_tokens=config.harness_max_tokens)
    harness = _ObservedHarness(real_harness, args.status_dir, card)

    try:
        result = ex.run(item, harness)
    except ClaimRaced as exc:
        print(f"\nFAILED: could not claim {card}: {exc}", file=sys.stderr)
        status.write(args.status_dir, card, phase="done", outcome="failed",
                     detail=f"claim raced: {exc}")
        return 1
    except Exception as exc:                       # noqa: BLE001 - report, don't vanish
        traceback.print_exc()
        status.write(args.status_dir, card, phase="done", outcome="failed",
                     detail=f"{type(exc).__name__}: {exc}")
        return 1

    wt = handle.worktree_for(card)
    print("\n" + "=" * 72)
    if wt:
        diffstat = subprocess.run(["git", "-C", wt, "diff", "--stat", repo.base_branch],
                                  capture_output=True, text=True).stdout.strip()
        print(f"diff (worktree {wt}, vs {repo.base_branch}):")
        print(diffstat or "  (no diff)")
    print(f"score={result.score} outcome={result.outcome} passed={result.passed}")
    print(f"notes: {result.notes}")

    branch = f"autopilot/{card}"
    if result.passed:
        try:
            ex.finalize(item, result)
        except Exception as exc:                     # noqa: BLE001 - surface, never swallow
            traceback.print_exc()
            status.write(args.status_dir, card, phase="done", outcome="failed",
                         detail=f"gate PASSED but finalize failed: "
                                f"{type(exc).__name__}: {exc}; branch {branch} "
                                "may already be pushed -- check by hand")
            return 1
        pr = _pr_url(repo.path, branch)
        print(f"\n>>> BRANCH + DIFF READY FOR CHEF: {branch}"
              + (f"  PR: {pr}" if pr else "  (PR opened; see `gh pr list`)"))
        status.write(args.status_dir, card, phase="done", outcome="pr_ready",
                     score=result.score, branch=branch, pr_url=pr,
                     detail=result.notes[:2000])
        return 0

    if result.outcome == "salvage" and result.artifact:
        # run()'s _salvage_to_review already committed+pushed+opened the PR
        # (grader inconclusive, CI green + coverage held). Never auto-merged.
        print(f"\n>>> BRANCH + DIFF READY FOR CHEF (salvage -- grade "
              f"inconclusive, CI green): {branch}  PR: {result.artifact}")
        status.write(args.status_dir, card, phase="done", outcome="pr_ready_salvage",
                     score=result.score, branch=branch, pr_url=result.artifact,
                     detail=result.notes[:2000])
        return 0

    print(f"\n>>> FAILED ({result.outcome}): {result.notes}")
    status.write(args.status_dir, card, phase="done", outcome="failed",
                 score=result.score, detail=result.notes[:2000])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
