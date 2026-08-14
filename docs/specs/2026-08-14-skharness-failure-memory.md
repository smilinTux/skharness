# skharness Cross-Run Failure Memory (`attempts[]` + helpful forgetting)

**Date:** 2026-08-14 · **Status:** Draft for review (design approved via deep-dive) · **Owners:** skcoord (schema/write), skharness (behavior)

## Problem

A terminal non-pass in the autocode runner vanishes. `engineering.py` seeds in-run `feedback` from grade notes (`strip_promise(gr.notes)`, line 355) and threads it forward each round (`prior_feedback=feedback`, line 294), but that state dies when `run()` returns. `direct.py:55` hardcodes `prior_feedback=None`, so a fresh run walks in blind. The terminal non-passes get logged to the run journal and decisions queue but leave **nothing on the card**. Result: the next run of the same card rebuilds into the identical wall. Fix: a small, distilled failure memory (`meta.autopilot.attempts[]`) written at the executor's terminal-return sites and read back at run start, governed by a "helpful forgetting" renderer so the card never bloats.

**Where we write (verified):** the executor's own terminal returns are the truth source - `engineering.py` no-op double-empty bail and did-not-converge return, plus `direct.py` fail return. We do **not** write inside `escalate()`: `orchestrator.py:665` calls `ex.escalate(...)` for *every* non-passed result, so writing there would double-record each failure. We also **exclude the salvage return** (`engineering.py:349`, `passed=False` but a human-review PR was opened with CI green) - that is a success, not a failure, and must never poison future context.

## Goals

- Persist each terminal non-pass as one distilled card entry the next run can read.
- Wire read-back: `engineering.py` run() and `direct.py:55` seed `feedback`/`prior_feedback` from the card.
- Keep the active state SMALL: distill to one consequence line per failure; raw detail stays in the run journal.
- Clear-on-pass so a flaky-CI false failure cannot haunt every future run.
- Additive-only, tolerant-reader, zero migration across the Syncthing fleet.

## Non-goals

- No change to the plan: `description`/`acceptance_criteria` stay the brief; only `update_task`/`edits[]` touch them. `attempts[]` is failure memory ONLY.
- No shared `progress.md` or any per-card prose file. Card field + run journal are the only stores.
- No inlining of raw logs, diffs, tracebacks, or test output onto cards or into briefs.
- No new lane, tag, phase, or CLI surface. No ITIL incident, no gtd-ingest adapter.
- No CardStore mirror extension, no multi-node write path, no retro-backfill, no cross-card memory.

## Data contract

`meta.autopilot.attempts[]` - sibling of `scores[]`/`edits[]`/`obsolete`/`decomposed`. Entry:

```json
{
  "run_id": "…",
  "ts": "<_now_iso()>",
  "round": 3,
  "outcome": "no_op | ci_red | direct_fail",
  "tried": "one line: what the approach was",
  "why_failed": "one line: distilled cause (failing test id + assertion, not the traceback)",
  "replacement_hint": "one line: 'try Y', or empty string"
}
```

| Field | Type | Semantics |
|---|---|---|
| `run_id` | str | Journal join key. Points at `autopilot/runs/<run_id>` for raw detail. |
| `ts` | str | `_now_iso()` write time. Sort/bound key for the reader. |
| `round` | int | Round index at time of failure. Only non-string field. |
| `outcome` | str | Closed vocab: `no_op`, `ci_red`, `direct_fail`. Escalation is orchestrator's downstream action on a non-passed result, not a distinct write site, so it is not an outcome value. New value needs a spec amendment. |
| `tried` | str | One line, the approach taken. |
| `why_failed` | str | One line distilled cause. Distilled at the call site; skcoord stores verbatim. |
| `replacement_hint` | str | One line "try Y", or `""`. Phrased as a consequence, never an instruction. |

**Dedup key (reader):** `(outcome, why_failed.strip().lower())`.
**Idempotency key (writer):** `(run_id, outcome)`.

## API

### `record_attempt()` - skcoord `coordination.py`

```
record_attempt(task_id, run_id, round, outcome, tried, why_failed, replacement_hint="")
```

Mirrors `score_task` (line 355) EXACTLY:

- `_mutate(d)` closure + `self._write_task_raw(task_id, _mutate)`. Harness never touches card JSON directly.
- `d.setdefault("meta", {}).setdefault("autopilot", {}).setdefault("attempts", [])`.
- `ts = _now_iso()`.
- **Idempotent by `(run_id, outcome)`:** iterate existing entries; on a matching key REPLACE in place, else APPEND. (Same loop shape as `score_task`'s `(round, harness)` match.) Makes retried finalize paths and crash-resume safe.
- **Hard cap after write:** `attempts[:] = attempts[-10:]`. Corruption guard, NOT the forgetting policy.

### `clear_attempts()` - skcoord `coordination.py`

```
clear_attempts(task_id) -> list[dict]   # returns the entries it removed
```

Card mutation, rides `_write_task_raw` like everything else. Behavior: read the card's current `attempts[]`, set `attempts` to `[]` on the card, and **return the removed entries**. skcoord does NOT write the journal - it has no journal code, and the run journal is skharness-owned (`orchestrator.py` `journal.write_run`). Journal archival is the caller's job: on final PASS the skharness finalize path calls `clear_attempts(task_id)` and writes the returned entries to that run's journal entry. Keeps the "skcoord stores facts, skharness decides" split intact.

### `build_prior_feedback(payload) -> str | None` - skharness `autocode/` shared module

The forgetting renderer. Shared by engineering + direct. Input is the card dict as the executor already holds it: **`item.payload`** (built at `orchestrator.py:194` as `payload={**task, ...}`). It may lack `meta` on older/thin cards - a tolerant reader returns `None` in that case (no board re-read). Renders harness prompt context from card data (skcoord stores facts; skharness decides what an agent reads). Contract in §Forgetting policy.

## Call-site changes

Line numbers from verified source (2026-08-14). Distillation of `why_failed` happens at each call site.

### `engineering.py`

| Site | Anchor | `outcome` | `why_failed` / `replacement_hint` |
|---|---|---|---|
| No-op double-empty bail | the `empty_rounds >= 2` return (~line 317) | `no_op` | wf: "no diff in 2 rounds; acceptance likely already satisfied or agent cannot write". rh: "verify against base branch before re-implementing". |
| Did-not-converge | terminal return after the round loop (grades via `board.score_task(item.ref, round=rnd, …)` at 332) | `ci_red` | wf = distilled failing-test tail (test id + one assertion line), NOT `strip_promise(last.notes)` wholesale. rh: "" or targeted hint. |

**Do NOT write in `escalate()`** (def ~line 271): `orchestrator.py:665` calls it on every non-passed result, so a write there double-records. **Do NOT write on the salvage return** (`engineering.py:349`, `gr.score is None and ci_status == "green"`): it opened a human-review PR - that is a success, exclude it explicitly. Record only at the two terminal returns above.

**Read-back:** at `run()` start (line 279), initialize `feedback = build_prior_feedback(item.payload)` **instead of** the current fresh-`None` seed. In-run grade feedback (seeded at 355, threaded at 294) then overwrites it round-to-round exactly as today.

### `direct.py`

| Site | Anchor | `outcome` | `why_failed` |
|---|---|---|---|
| Terminal fail | `run()` (line 45) when `passed` is False | `direct_fail` | "empty diff or run not ok in ungated single round". |

**Read-back:** `direct.py:55` - replace `prior_feedback=None` with `prior_feedback=build_prior_feedback(item.payload)`.

## Forgetting policy

Implemented in `build_prior_feedback(card)`. Each rule maps to a test.

1. **Empty → None.** Missing/empty `meta.autopilot.attempts` returns `None` - byte-identical to today's fresh start.
2. **Dedup.** Key `(outcome, why_failed.strip().lower())`. Keep the NEWEST entry per key (iterate reversed, first-seen wins).
3. **Bound.** After dedup, take the last **3** distinct failures by `ts`.
4. **Distill.** Exactly one line per failure:
   - hint present: `This failed for {why_failed}, try {replacement_hint}.`
   - hint empty: `This previously failed for {why_failed}; avoid repeating that approach.`
   - Block header (one line): `Prior attempts on this card (distilled):`
5. **Ceiling.** Rendered block ≤ **600 chars**; truncate OLDEST lines first. Never inline diffs/tracebacks/test output. Optional single pointer line: `Raw history: autopilot/runs/<run_id>`.
6. **Clear-on-pass.** On final PASS (twin gate green in engineering; human merge is out of scope for direct) the skharness finalize path calls `board.clear_attempts(task_id)` (card `attempts = []`, returns the removed entries) and writes those returned entries to the passing run's journal entry. skcoord clears; skharness archives. A flake haunts at most until the next pass.

Storage vs context: writer cap 10 (storage guard) is distinct from reader bound 3 (context policy).

## Test plan

### Unit - skcoord `record_attempt` / `clear_attempts`

- **Idempotent replace:** two `record_attempt` calls, same `(run_id, outcome)` → one entry, second replaces first in place.
- **Append on distinct key:** differing `outcome` or `run_id` → two entries.
- **Cap:** 12 writes → `len(attempts) == 10`, keeps the newest 10.
- **Clear-on-pass (skcoord unit):** `clear_attempts(task_id)` leaves `attempts == []` on the card AND returns the removed entries verbatim.
- **Clear-on-pass (skharness unit):** finalize takes the returned entries and writes them to the run-journal entry for `run_id`.
- **Pre-existing card tolerance:** card with no `meta`/`autopilot` → `setdefault` chain builds it, no error.

### Unit - skharness `build_prior_feedback`

- **None on empty:** no attempts (and missing-key card) → `None`.
- **Dedup:** two identical `ci_red` failures collapse to one line.
- **3-distinct bound:** 5 distinct failures render 3 lines, newest by `ts`.
- **Distillation templates:** hint present vs empty select the correct one-liner.
- **600-char ceiling:** oversized set truncates oldest first, result ≤ 600 chars, no raw log substrings.

### Integration - round trip

1. Force a terminal fail (e.g. direct-mode empty diff) → assert one `attempts[]` entry on the card and raw detail in the journal.
2. Start a fresh run on the same card → assert `build_prior_feedback(card)` is non-`None` and the seeded `feedback`/`prior_feedback` carries the distilled line.
3. Drive that run to PASS → assert `clear_attempts` fired: card `attempts == []`, journal holds the archived copy.

## Rollout

- **Additive-only:** one new optional array under `meta.autopilot`. No migration. Written only via `_write_task_raw`, so it cannot clobber `scores[]`/`edits[]`/`obsolete`/`decomposed` and they cannot clobber it.
- **Fleet / Syncthing:** `~/.skcapstone` replicates everywhere; the field propagates as data. No reader anywhere may error on its absence or on unknown entry keys (tolerant reader). Cards predating the field behave exactly as today (`None` on read).
- **Write authority:** `_write_task_raw`'s single-writer precondition stands; autopilot-daily is pinned to noroc2027. `record_attempt`/`clear_attempts` add NO new writer on any node. Do not relax the pin for this feature.
- **CardStore:** `_write_task_raw` is not wrapped by the CardStore mirror; `meta.autopilot` has never mirrored. Acceptable, noted, not extended here.
- **`skos autopilot staged/release`:** unaffected; released children start with no `attempts[]` and earn their own.
- **Sequencing:** land skcoord `record_attempt`/`clear_attempts` + unit tests first (harmless until called), then skharness call sites + read-back + renderer.
