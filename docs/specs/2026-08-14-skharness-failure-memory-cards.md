# Coord Cards: skharness Failure Memory (`attempts[]`)

**Spec:** `skharness/docs/specs/2026-08-14-skharness-failure-memory.md` (read for all detail)
**Status:** DRAFT for Chef review. Do NOT materialize until released.
**Card design rule:** each card is minimal-context. It points to a spec section; it does not restate it. If a card needs the spec to be understood, that is correct.

---

## EPIC FM-0: Cross-run failure memory

**Objective:** Persist each terminal non-pass as one distilled card entry (`meta.autopilot.attempts[]`), read it back at run start, forget helpfully so cards never bloat.
**Lane:** engineering. **Size:** L (epic). **Autopilot:** staged / Proposed until Chef releases.
**Children (in dependency order):** FM-1 → FM-2 → FM-3 → FM-4 → FM-5 → FM-6 → FM-7.
**Done when:** all children pass; fail→read-back→pass round trip green (FM-7).

---

## FM-1: skcoord `record_attempt()` + `attempts[]` contract

**Intent:** Add the writer that appends/replaces a distilled failure entry on the card.
**Touches:** `skcoord/…/coordination.py` : new `record_attempt(task_id, run_id, round, outcome, tried, why_failed, replacement_hint="")`.
**Acceptance:**
- [ ] Mirrors `score_task` shape: `_mutate` closure + `_write_task_raw`.
- [ ] `setdefault` chain builds `meta.autopilot.attempts`.
- [ ] Idempotent on `(run_id, outcome)`: replace in place else append.
- [ ] Hard cap `attempts[:] = attempts[-10:]` after write.
**Depends on:** none.
**Context:** spec §API `record_attempt`, §Data contract. **Size:** M. **Lane:** engineering.

---

## FM-2: skcoord `clear_attempts()`

**Intent:** Wipe the card array and hand the removed entries back to the caller (skcoord has no journal code; skharness archives them, see FM-6).
**Touches:** `skcoord/…/coordination.py` : new `clear_attempts(task_id) -> list[dict]`.
**Acceptance:**
- [ ] Set card `attempts = []` via `_write_task_raw`.
- [ ] Return the removed entries verbatim. No journal write here.
**Depends on:** FM-1.
**Context:** spec §API `clear_attempts`, §Forgetting policy rule 6. **Size:** S. **Lane:** engineering.

---

## FM-3: skharness `build_prior_feedback(item.payload)` renderer

**Intent:** The forgetting renderer, turn `attempts[]` into ≤600-char distilled context or `None`. Input is `item.payload` (the card dict the executor holds); tolerate a missing `meta` → `None`.
**Touches:** new shared module under `skharness/autocode/`.
**Acceptance:**
- [ ] Empty/missing → `None`.
- [ ] Dedup on `(outcome, why_failed.strip().lower())`, newest wins.
- [ ] Last 3 distinct by `ts`; correct hint-present vs hint-empty template.
- [ ] Block ≤600 chars, truncate oldest first, no raw logs inlined.
**Depends on:** FM-1 (contract).
**Context:** spec §API + §Forgetting policy rules 1-5. **Size:** M. **Lane:** engineering.

---

## FM-4: engineering.py write sites + read-back seed

**Intent:** Record the 2 engineering terminal returns; seed `feedback` from the card at run start.
**Touches:** `skharness/…/engineering.py` : ~L317 no-op bail (`no_op`), post-loop did-not-converge (`ci_red`); read-back at run() start ~L279.
**Acceptance:**
- [ ] Both sites distill `why_failed` to one line (test id + assertion, not raw notes) and call `record_attempt`.
- [ ] NOT in `escalate()` (called on every non-pass → double-write) and NOT on the salvage return (~L349, CI-green human PR = success).
- [ ] `feedback = build_prior_feedback(item.payload)` replaces the fresh-`None` seed; in-run grade feedback still overwrites round-to-round.
**Depends on:** FM-1, FM-3.
**Context:** spec §Call-site changes / engineering.py. **Size:** M. **Lane:** engineering.

---

## FM-5: direct.py write site + read-back

**Intent:** Record `direct_fail`; read back prior feedback.
**Touches:** `skharness/…/direct.py` : write at run() L45 when `passed` False; replace `prior_feedback=None` at L55 with `build_prior_feedback(item.payload)`.
**Acceptance:**
- [ ] Failing single round calls `record_attempt(outcome="direct_fail")`.
- [ ] L55 seeds from the renderer.
**Depends on:** FM-1, FM-3.
**Context:** spec §Call-site changes / direct.py. **Size:** S. **Lane:** engineering.

---

## FM-6: clear-on-pass wiring

**Intent:** On final PASS, clear the card and archive the removed entries to the run journal so a flake cannot haunt forever.
**Touches:** `skharness/…/engineering.py` finalize/twin-gate-green path.
**Acceptance:**
- [ ] On final PASS, `board.clear_attempts(task_id)` fires and skharness writes the returned entries to the run's journal entry (`journal.write_run`).
- [ ] Direct human-merge case out of scope (noted).
**Depends on:** FM-2, FM-4.
**Context:** spec §Forgetting policy rule 6. **Size:** S. **Lane:** engineering.

---

## FM-7: tests (units + round-trip integration)

**Intent:** Lock every forgetting rule and the fail→read-back→pass loop.
**Touches:** skcoord unit tests, skharness unit tests, one integration test.
**Acceptance:**
- [ ] skcoord: idempotent-replace, append-distinct, cap-10, clear archives+wipes, pre-existing-card tolerance.
- [ ] skharness: none-on-empty, dedup, 3-distinct bound, templates, 600-char ceiling.
- [ ] Integration: force fail → assert entry+journal → fresh run seeds non-`None` → drive to PASS → assert cleared.
**Depends on:** FM-1..FM-6.
**Context:** spec §Test plan. **Size:** M. **Lane:** engineering.

---

## Materialize on the board (real CLI, verified 2026-08-14; NOT executed here)

Real surface: `skcapstone coord create` takes `--title --desc --priority --tag --criteria --dep` (no `--lane/--status/--parent/--after/--spec`). Deps are `--dep <id>` (repeatable, blockers). Spec pointer via `skcapstone coord link <id> doc <path>`. `create` prints the new task id; capture it.

```bash
SPEC=skharness/docs/specs/2026-08-14-skharness-failure-memory.md

# Epic (tag failure-memory groups the set; autopilot-staged keeps it in the Proposed lane)
EPIC=$(skcapstone coord create --by lumina --priority high \
  --tag failure-memory --tag epic --tag autopilot-staged \
  --title "FM-0 skharness cross-run failure memory (attempts[])" \
  --desc "See $SPEC" | awk '{print $NF}')   # confirm how your build prints the id
skcapstone coord link "$EPIC" doc "$SPEC"

mk() {  # title, then dep ids
  local t="$1"; shift; local deps=(); for d in "$@"; do deps+=(--dep "$d"); done
  skcapstone coord create --by lumina --priority high \
    --tag failure-memory --tag autopilot-staged --dep "$EPIC" "${deps[@]}" \
    --title "$t" --desc "See $SPEC (card of same name)" | awk '{print $NF}'
}

FM1=$(mk "FM-1 skcoord record_attempt + attempts[] contract")
FM2=$(mk "FM-2 skcoord clear_attempts"                     "$FM1")
FM3=$(mk "FM-3 skharness build_prior_feedback renderer"    "$FM1")
FM4=$(mk "FM-4 engineering.py write sites + read-back seed" "$FM1" "$FM3")
FM5=$(mk "FM-5 direct.py write site + read-back"            "$FM1" "$FM3")
FM6=$(mk "FM-6 clear-on-pass wiring"                        "$FM2" "$FM4")
FM7=$(mk "FM-7 tests (units + round-trip)"      "$FM1" "$FM2" "$FM3" "$FM4" "$FM5" "$FM6")

# Release the staged epic into the buildable backlog when ready:
skos autopilot release "$EPIC"
```

**Note on staging:** `skos autopilot` has no `stage` verb. The Proposed lane is entered via the `autopilot-staged` tag (hidden from OPEN, never auto-built); `skos autopilot release <epic>` strips it. Alternatively skip hand-creation entirely and run `skos autopilot triage --tasks <epic>` to let autopilot decompose the epic into staged children, using the cards above as the review reference. Confirm the `create` id-capture (`awk '{print $NF}'`) against your build's output format before batch-running.
