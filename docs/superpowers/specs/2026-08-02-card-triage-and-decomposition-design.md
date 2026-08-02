# Card Assessment + Decomposition for the Autocode Harness — Design
> Fable-architected (3 design angles + adversarial synthesis), 2026-08-02.
> Solves: vague cards escalate at round 1 (assess is text-only + fails OPEN on vague).
> Grounded in skharness.autocode code. Safety unchanged: decomposition only creates
> planning cards; the twin gate still guards every merge.

I have the grounding I need. Key code facts confirmed:

- `Board.create_task(self, task: Task)` takes a **Task object**, but `orchestrator.deepdive_spawn` calls it with **kwargs** (`title=`, `description=`, `tags=`) at line 145 — a confirmed latent bug all three designs flagged.
- `Board.update_task` can edit description / acceptance_criteria / add_tags only — **not dependencies**. So parent-parking via a dependency edit is impossible today; a meta marker + new Board method is the zero-Board-change path.
- `close_task_obsolete` writes `meta.autopilot.obsolete` + a notes line; `phase0_assess` skips those cards at line 194. That is the exact symmetry to mirror for decomposition.
- `AssessBrief.codebase_context` is plumbed everywhere but always passed `""`.
- `_ASSESS_VERDICTS = ("valid","stale","obsolete","needs_decision")`; fail-open-to-valid on inconclusive; needs_decision already gets a second-opinion confirm (base.py:256-275).
- `Caps` has `new_tasks_per_run=10`; no decompose caps. `Config.repo(name)` returns a `RepoSpec` with `.path` — a real checked-out repo the host can read.

Here is the recommended design.

---

# Recommended design: card assessment + decomposition for the autocode harness

## 0. Verdict up front

The three angles are really three separate decisions wearing one hat, and the right answer takes a different one from each:

- **Where it runs** → Design 1 wins. In-harness, on the existing unscoped path. Design 3's mandatory separate stage introduces build-starvation (`require_triage=true` means a missed cron silently stalls all new work) and re-couples via its own inline-micro-pass escape hatch anyway. We keep triage *in* Phase 0, and additionally expose a `--triage-only` short-circuit through the *same* code path for dry sweeps. That gives Design 3's operational benefit (a cheap board-wide sweep you can schedule) without its failure mode.
- **How it assesses** → Design 2 wins. Host-side repo grounding + a concreteness gate is what actually kills the fail-open-on-vague root cause. But we *reject* Design 2/3's "stop failing open, route the tail to a human" — that just renames fail-open to fail-to-human-queue for every un-groundable card (itil/research/email). We keep fail-open-to-the-twin-gate for the un-groundable tail and only redirect **structurally-vague repo-tagged cards** to `decompose`.
- **How it decomposes** → Design 1's mechanism (a 5th verdict, a sibling `harness.decompose()` reusing `_run`, a symmetric `Board.mark_decomposed`), hardened with Design 2/3's guardrails (depth, idempotent content-hash children, net_new greenfield flag) and their shared warning that syntactic existence ≠ correctness (so `obsolete` never closes on a bare file-exists).

Everything below is that synthesis, made concrete.

---

## 1. Where it lives

**In-harness, inside the existing Phase 0, reached board-wide only on the unscoped path — plus a `--triage-only` mode through the same path. No separate engine, no mandatory pre-stage.**

Rationale:

- Chef's own framing ("as another function if you don't provide a specific list") is exactly the existing `scoped` flag. A `--task`/`--tasks`/`--tag` run does the named work and never mutates board structure — same gate that already suppresses `deepdive_spawn` at line 217.
- A mandatory separate triage stage (Design 3) makes `run` depend on triage having been run. A missed triage cron then stalls every new card, and the "inline micro-pass when unscoped" mitigation is just Phase 0 triage under another name. So we make Phase 0 triage the primary path and skip the coupling.
- We still get Design 3's *operational* win cheaply: `run_once(..., triage_only=True)` (surfaced as `skos autopilot triage [--tag X] [--repo Y] [--dry-run]`) runs phase0_assess to completion — refine/close/decompose/queue — and then returns before phase1 selects anything to build. Same function, an early return, not a parallel module. Cron can run it hourly ahead of the daily build, and it can never starve the build because the build's own unscoped path still triages.

**Touch points (all on the existing seam):**
1. `types.py` — extend `Verdict` (§4).
2. `adapters/base.py` — teach `assess` to use `codebase_context`, add the concreteness gate, add a sibling `decompose()`; grow `_ASSESS_VERDICTS` to 5.
3. New `skharness/autocode/grounding.py` — pure, host-side, model-free `ground_card()`.
4. `orchestrator.py::phase0_assess` — fill `codebase_context` per card, add the `decompose` arm, add the parked-parent skip; add the `triage_only` early return in `run_once`.
5. `skcapstone/coordination.py` — add `Board.mark_decomposed()`; fix `deepdive_spawn`'s kwargs→`Task` bug at the same time.

---

## 2. The assessment mechanism (repo grounding + concreteness gate)

### Tier 1 — host-side grounding fills the always-empty `codebase_context` (model-free, board-scale cheap)

New `grounding.ground_card(brief, repo_spec) -> Grounding`. For a card carrying `repo:<name>`, resolve `config.repo(name).path` (a repo the harness already owns checked out) and, **without any sandbox or model call**:

1. Build/load a **per-run, per-repo index once**, keyed on `git -C <path> rev-parse HEAD`: `git ls-files` + a cheap symbol list (grep for `^\s*(class|def|func|type|const)\s+\w+` across tracked source). Reused across every card for that repo.
2. Extract candidate anchors from title/acceptance: CamelCase symbols (`OpsExecutor`), path-ish tokens (`src/.../ops_executor.py`), quoted names.
3. Probe each anchor: `git -C <path> grep -l -F <sym>` / `ls-files <glob>` against the cached index (dict lookups, not new subprocesses per card where avoidable).
4. Emit a bounded (~1–2 KB) `codebase_context` string of **facts**: which named artifacts exist (with a matched path), which acceptance anchors resolve, and a short `ls` of the target dir.
5. Compute `concreteness = resolved_anchors / referenced_anchors` and a `net_new` flag (acceptance says create/add/new *and* nothing resolves → greenfield is concrete-by-intent).

Cache the whole string by `(repo_head_sha, card_content_hash)` so daily re-runs on an unchanged card cost nothing. **Safety valve:** if the working tree is dirty or on an unexpected branch, refuse to ground (return empty context, `concreteness=None`) and fall back to today's text-only assess — grounding never lies against a working tree that disagrees with `base_branch`.

Cards with **no `repo:` tag** get no grounding — and that is fine (see the gate below).

### Making the verdict reliable (the actual fix for fail-open-on-vague)

`assess`'s instruction is rewritten to treat `codebase_context` as authoritative facts, and the fail-open policy is made **surgical, not global**:

- **Un-groundable card** (no repo tag, or grounding refused): behavior is **unchanged** — inconclusive still fails OPEN to `valid` → twin gate. We do *not* dump the un-groundable tail into a human queue. The twin gate remains the real net for these, exactly as today.
- **Repo-tagged card:** a `valid` verdict must **earn** its way to build via a **concreteness gate**. `valid` is accepted only when `concreteness >= 0.34` **or** `net_new`. Otherwise the verdict is **downgraded to `decompose`** — a coherent card that references nothing resolvable and isn't greenfield is, by construction, too vague to build in one diff. This is precisely the `d1d33e5f` "OpsExecutor skeleton" case: grounding finds no `OpsExecutor` symbol, acceptance is abstract, not net_new → `decompose`, never a doomed build.
- **Quorum on the build-eligible edge only:** generalize the existing needs_decision second-opinion (base.py:256-275) so a would-be `valid` on a repo card also gets one confirming call; on disagreement the **safer** verdict (`decompose`/`needs_decision`) wins. Cheap (`light=True`), and it fires only on the valid→build transition, not on every card.
- **`obsolete` is guarded against the existence≠correctness trap** (Design 2/3's sharpest critique): a bare "file exists" grounding fact **never** closes a card. `obsolete` requires *both* the existence fact *and* an explicit model judgment that the work is no longer needed, *and* the second-opinion confirm. Ambiguous "exists but maybe stubbed" defaults to `stale` or `decompose`, never `close`. `close_task_obsolete` stays reversible.

Net: uncertainty on a groundable card routes to *more structure* (decompose), uncertainty on an un-groundable card still fails open to the gate, and the flakiest closes (obsolete) get the strongest confirmation.

---

## 3. The decomposition mechanism

### From vague card to buildable subtasks

When `phase0_assess` sees `v.verdict == "decompose"` (whether the model said so directly or the concreteness gate downgraded a `valid`), it calls a **new sibling** `harness.decompose(brief) -> list[dict]` — a second `light=True`, repo-grounded call reusing `BaseCliAdapter._run`'s retry/telemetry. Prompt: *"Using these grounding facts, split into 2–8 independently buildable subtasks; each acceptance must NAME real files/functions from the facts."* Each spec is `{title, description, acceptance}`.

Then `_decompose_card(...)`:
1. **Idempotency guard** — return immediately if `meta.autopilot.decomposed` is already set, or a child tagged `parent:<id>` with a matching content-hash exists. Re-runs are no-ops.
2. **Depth guard** — read `meta.autopilot.decomp_depth` (default 0); if `>= max_decompose_depth`, do **not** split again — queue `needs_decision` instead.
3. `specs = harness.decompose(brief)`; truncate to `max_subtasks_per_card`; if empty after retries → `needs_decision` (never a silent drop). If the model wants **more** than the cap → `needs_decision` (it's an epic, a human scopes it).
4. For each spec build a **real `Task(...)`** (not kwargs — this is the create_task signature the code actually has; fix `deepdive_spawn`'s latent kwargs bug in the same PR): `tags = <parent's repo:/quality: tags> + ["autopilot","autopilot-untriaged", f"parent:{tid}"]`, `acceptance_criteria = spec["acceptance"]`, `meta={"autopilot":{"parent":tid,"decomp_depth":depth+1,"fingerprint":<hash>}}`, `created_by="autopilot"`. Deterministic child id via `stable_qid(parent_id + title)` so accidental re-entry matches existing children and no-ops. `board.create_task(child)`.
5. **Park the parent** via new `Board.mark_decomposed(tid, child_ids, run_id)` — mirrors `close_task_obsolete`: writes `meta.autopilot.decomposed = {children, run_id, ts}` + a notes line. `phase0_assess` skips any card carrying that marker at the top of the loop, symmetric with the obsolete skip at line 194. (Parking via `dependencies += child_ids` would be more elegant, but `update_task` can't edit dependencies today; the meta-skip is the zero-Board-change path, with a `dependencies`-edit as a small follow-up.)

Children are born `autopilot-untriaged`, so the existing `is_untriaged()` check in `phase1_triage` (line 236) keeps them out of the **same run's** build pool. Next cycle they are re-grounded — now with concrete, path-naming acceptance — and build if concrete. A config flag `decompose_autobuild` (default **False**) can drop the untriaged tag to flow children into the same run for full autonomy when Chef wants it.

### The precise decision rule

| Verdict | Condition | Action |
|---|---|---|
| **valid** | Acceptance anchors resolve, **or** `net_new`; concrete enough for one diff | build |
| **stale** | Goal current, description drifted, grounding pins a **single** clear target | `update_task` rewrite in place, then build (preferred over decompose whenever one target) |
| **obsolete** | Artifact exists **AND** model confirms work no longer needed **AND** second-opinion confirms | `close_task_obsolete` (reversible) |
| **decompose** | Coherent + wanted, but references **multiple** distinct artifacts **OR** acceptance is abstract/skeleton/epic-shaped, grounding resolves nothing concrete, not `net_new`, and `depth < max_decompose_depth` | split into children, park parent |
| **needs_decision** | Self-contradictory/ambiguous goal; **or** would-decompose but at max depth or wants >max children; **or** empty decompose after retries | `DecisionItem` → human queue |

Refine-in-place beats decompose whenever grounding pins one target; decompose is only for genuinely multi-artifact or under-specified cards.

---

## 4. Extending the verdict flow

Purely additive. `_ASSESS_VERDICTS` grows from 4 to **5**: `valid | stale | obsolete | needs_decision | decompose`. `Verdict` gains two optional, back-compatible fields:

```python
@dataclass
class Verdict:
    verdict: str  # valid | stale | obsolete | needs_decision | decompose
    reason: str
    updated_description: str | None = None
    updated_acceptance: list[str] | None = None
    subtasks: list[dict] | None = None       # decompose payload (or via harness.decompose())
    concreteness: float | None = None         # grounding score; drives the gate
```

`phase0_assess` gains one arm — `elif v.verdict == "decompose": _decompose_card(...)`. The four existing arms are byte-identical. The fail-open default and the needs_decision confirm are untouched. **The twin gate (`score==5 AND CI green AND coverage`) is not modified at all** — decomposition sits strictly upstream and produces only planning cards.

---

## 5. Guardrails (concrete values)

- **`max_subtasks_per_card = 8`** (`Caps`): `decompose()` output truncated; wanting >8 → `needs_decision`.
- **`max_decompose_depth = 2`** (`Caps`): children carry `meta.autopilot.decomp_depth`; at the ceiling the arm queues `needs_decision`, never splits again — no infinite trees.
- **`Caps.new_tasks_per_run = 10`** (existing) is the hard per-run ceiling on **total** children across all decompositions — one bad run can't flood the ~450-card board.
- **Idempotency**: parent `meta.autopilot.decomposed` skip (symmetric with obsolete); children carry `parent:<id>` + a content-hash fingerprint; deterministic child ids via `stable_qid(parent_id+title)`. Re-runs are no-ops; re-editing a decomposed parent is punted to human re-triage (no clean auto-re-split semantics — a known limit, not a silent bug).
- **Human-review gate (default on)**: children are `autopilot-untriaged` → excluded by the existing `is_untriaged()` check; a `DecisionItem` summarizing the split (parent → child ids) is queued to the digest. Only an explicit release (or `decompose_autobuild=true`) lets them build.
- **Confidence-gated entry**: reached only on a confident model `decompose` **or** a concreteness-gate downgrade of a repo card. Un-groundable/inconclusive still fails OPEN to the twin gate.
- **Scope gate**: runs only on the unscoped board-wide path or explicit `--triage-only`. A `--task`/`--tasks`/`--tag` run never restructures the board.
- **Grounding is read-only**: `git grep`/`ls-files`/`ls` only — no writes, no sandbox, no network; pinned HEAD sha; refuses on a dirty/unexpected tree → text-only fallback.
- **Empty/failed decompose → `needs_decision`**, never a silent drop.
- **Never merges code**: decomposition only writes coord task files; every child still crosses the untouched twin gate.
- **`dry_run` honored**: no `create_task`/`mark_decomposed` writes on a dry run.

---

## 6. Affordability on the ~450-card board

- **Tier 1 assess cost is unchanged**: one cheap `light=True` single-turn call per unblocked card, exactly as today. Only unblocked cards are assessed.
- **Grounding is free**: host-side `git ls-files`/`grep`, per-repo index built once per run keyed on HEAD sha, `codebase_context` cached by `(repo_head_sha, card_content_hash)`. Steady-state re-runs touch only the daily delta (fingerprint cache).
- **Model spend scales with vagueness, not board size**: the concreteness heuristic pre-classifies for free; only build-eligible repo cards pay the one quorum call, only decompose-routed cards pay the one extra `decompose()` call. The bulk of the 450 (obviously concrete or obviously stale) still costs a single light assess.
- **Child creation is local JSON writes**, hard-capped at `new_tasks_per_run=10` per run, all inside the existing `CapLedger` token/dollar ceiling.
- **`--triage-only` + scope knobs** let a run sweep one repo/tag at a time if a full sweep is ever a concern.

---

## 7. Phased implementation plan (smallest shippable first)

**Phase A — grounding + latent-bug fixes (ships value with zero new verdict).**
- Fix `deepdive_spawn` to build a `Task(...)` object (it currently calls `create_task` with kwargs — a live bug).
- Add `Board.mark_decomposed()` (symmetric with `close_task_obsolete`).
- Add `grounding.ground_card()` and wire it into `phase0_assess` to fill `codebase_context`; rewrite the `assess` instruction to use it.
- Result: `stale`/`obsolete`/`valid` all get sharper immediately, with no behavioral risk beyond better inputs. Independently shippable, low risk. **This is the smallest increment that moves the needle.**

**Phase B — the `decompose` verdict (core).**
- Extend `Verdict` (`subtasks`, `concreteness`); grow `_ASSESS_VERDICTS` to 5; add the concreteness gate + generalized quorum on the valid→build edge; guard `obsolete` with existence+intent+confirm.
- Add `harness.decompose()` (reuse `_run`), `StubHarness.decompose() -> []`, and the Protocol method.
- Add the `_decompose_card` arm in `phase0_assess`: untriaged children, parked parent, all guardrails (caps, depth, idempotency). `decompose_autobuild=false`.

**Phase C — operability.**
- `run_once(..., triage_only=True)` early return + `skos autopilot triage` CLI re-export for a scheduled dry sweep.
- `DecisionItem` digest summary of each split; an aging sweep that flags long-orphaned `autopilot-untriaged` children so decompositions no one releases don't become permanent board clutter.

**Phase D — future/optional.**
- Read-only repo-**mounted** Tier 2 grounding (a sandbox with the worktree mounted read-only, egress off) for the ambiguous bucket, to judge *correctness* not just *existence* — the real fix for existence≠correctness. Deliberately deferred: bigger change, and the cheap host-side grounding already kills the observed failure.

---

## 8. Honest risks, and why the twin gate keeps it safe

- **Existence ≠ correctness** (the core weakness inherited from grounding): a stubbed `OpsExecutor` resolves as "exists". Mitigation: `obsolete` requires existence **and** intent **and** confirm and is reversible; ambiguous existence defaults to `stale`/`decompose`, never close. Phase D's mounted read pass is the real fix.
- **Over-decomposition of greenfield**: a legitimately new-file card resolves nothing and looks vague. Mitigation: the `net_new` intent flag treats "create X" as concrete-by-intent; the untriaged human gate catches the rest.
- **Grounding lies on a dirty tree**: mitigated by pinning HEAD sha and refusing on an unexpected branch → text-only fallback.
- **Model-dependent decompose boundary**: the same seam that hedges valid-vs-needs_decision now also owns valid-vs-decompose. But note the consequence is now *weaker*: a bad split creates **planning cards**, not a bad merge.
- **Orphaned untriaged clutter**: Phase C's aging sweep + digest summary.
- **Latency**: a vague card now takes ~2 cycles (decompose, then build children) instead of failing fast at round 1 — an acceptable, flagged behavior change, strictly better than today's empty-diff escalation.

**Why the net safety change is zero:** decomposition only ever *creates planning cards*. It never touches git, worktrees, or PRs. Every child still crosses the completely-unmodified twin gate (`score==5 AND CI green AND coverage`) before a single line merges. The worst case of a bad triage is board clutter a human reviews — strictly weaker than the merge the gate already guards. We are adding a capability (create cards) that is safer than the one the existing gate already contains, while removing the specific failure mode (vague card → doomed build → empty-diff escalation) that motivated the work.