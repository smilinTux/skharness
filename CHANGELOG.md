# Changelog

All notable changes to `skharness` are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/spec/v2.0.0.html). The git tag IS the
version (setuptools-scm); a release is cut by pushing a `v*` tag, or by
dispatching `publish.yml` on `main`, which cuts the next patch tag itself.

## [Unreleased]

### Security
- **A card can no longer select the model that grades it** (card `0b7e3ac3`).
  The twin-gate grader took the build's bucket wholesale, so a card graded
  S/low routed its own quality gate to the weakest class in the fleet: grade
  the work easy, get an easy grader. The bucket's two legs are now split at the
  grader boundary. The SENSITIVITY leg is still inherited exactly (the grader
  reads the diff, so it must stay inside the build's trust zone, and a secret
  card still cannot get a looser grader); the CAPABILITY leg is a fixed class
  the card cannot influence, matching the pin the approved design already
  applies to the phase0 assessor. New module `autocode/grader_pin.py`.
- **A card `quality:` tag is now RAISE-ONLY.** `quality:direct` routed to
  `engineering-direct`, documented as NO grade, NO gate, so a card-authored tag
  could switch off the twin gate that judges it. A tag may now only strengthen
  review against the operator baseline (`config.default_quality` raised by
  `RepoSpec.min_quality`). Lowering quality is still fully available, but only
  through operator config that no card can write. A refused downgrade emits a
  `quality_downgrade_refused` health event rather than coercing silently.
- **`grader_model` is recorded on every outcome row.** Before this, a grade
  produced by a competent grader and one the card downgraded for itself wrote
  byte-identical ledger rows (both a well formed score 5); there was no
  observation separating them. The field sits alongside `model_requested`, never
  folded into it: on a graded card the two agree on sensitivity zone and differ
  on capability class, and two rows whose classes track each other is the defect
  returning.
### Fixed
- **S25: the grading-floor guard now composes across a multi-card branch, without
  being weakened** (card `3f6719e4`). `test_no_file_on_the_grading_floor_was_modified`
  measured `origin/main...HEAD` and called the result "what this card did". True
  for one card, false for a branch that composes nine: S12 touches no floor file,
  S14 legitimately touches `buckets.py`, and S12's guard reported S14's work as
  S12's violation. Every card was individually compliant and the composition was
  red, structurally, on every future integration pass regardless of merit.
  The rule is unchanged; only the measurement moved. A floor change is still a
  violation unless it appears in `tests/data/grading-floor-allowances.json`, a
  human-written list with a card and a written reason per entry. Each entry pins
  a git BLOB SHA, not a path, so the exception covers exactly the post-image that
  was read and dies the moment the file changes again. That list is itself on
  `protected._ALWAYS_PROTECTED`, so a diff that adds an allowance can never
  auto-merge; "reviewed, not automatic" is structural rather than conventional.
  Every failure to MEASURE is now a violation too: the old form called
  `pytest.skip` when `origin/main` was missing, which is a guard reporting success
  over an unmeasured floor. It now raises `FloorCheckError`, and a deleted floor
  file hashes to a sentinel no pin can match. Two negative controls mutate
  `model_class` derivation (in `bucket_id`, inside the ALLOWED file, and in
  `grading.model_class_for`, outside it) and prove the guard still fires.
  DISPOSITION RECORDED: S14's `buckets.py` change (import of
  `assert_routing_field` plus two lines in `attach_dispatch_model`) is accepted.
  It changes no grading semantics, only which attribute name the routing layer
  may write. Human sign-off goes to Chef with the branch and the allowance file
  says so in the entry.
- **S18: `record_success` is wired, so S9's symmetric memory is no longer
  dormant.** `Board.record_success`, the `successes[]` sibling key,
  `build_prior_success_feedback`, its own renderer and 25 passing tests all
  existed and NO production code path called any of it. A dormant
  success-memory module and an absent one produce identical behaviour, which is
  this epic's own failure mode applied to itself. `EngineeringExecutor.finalize`
  now records a success on the pass branch, before the attempt archive so a
  future change to `clear_attempts` cannot take it along; `DirectExecutor`
  records too, with the UNGATED status stated in the entry so a later round
  cannot read an unverified pass as a verified one; and `TaskBrief` carries
  `prior_success_feedback`, seeded once per build and, unlike `prior_feedback`,
  NOT overwritten round to round, because cross-run memory has no in-run
  equivalent for the live grade to replace. The salvage path still records
  nothing: it opens a CI-green human-review PR, but the grade never said 5, so
  nothing verified the approach that would be remembered. Every acceptance test
  drives `run()` and `finalize()`, never `record_success` directly.
  - **Outcome vocabulary, decided rather than inherited:** success outcomes DO
    validate against `types.GATE_OUTCOMES`. `record_attempt`'s looseness was
    reasonable before S1, when no closed vocabulary existed. It is not now:
    `GateResult.__post_init__` already refuses a value outside the five and the
    S4 outcome rows are keyed on the same set, so a success row carrying a
    sixth value could be joined against neither.
  - **Two dependencies, stated rather than assumed.** `skcoord`'s
    `Board.record_success` is on branch `feat/s9-success-memory` and is not on
    the installed package, so on a node running today's skcoord the write
    degrades to a `record_success_error` health event and the finalized PR is
    untouched (pinned by a test). And `adapters/base.py:440` builds the model's
    JSON payload field by field, so `prior_success_feedback` reaches the
    `TaskBrief` but not yet the prompt; adding it there is a one-line change in
    a file owned by a concurrent card.
- **S17: the self-modification floor had ONE call site and it was unreachable.**
  `changed_paths_are_protected` was called only from inside `if automerge:` in
  `engineering.py`, and `automerge_repos` is `[]` in all four live configs, so
  the carve-out never executed. This CHANGELOG, card 09573989 and the epic
  935d4b61 design spec all cited it as the protection against
  self-modification; that reasoning is correct, but the mechanism implementing
  it did not run. The floor is now evaluated on the path that actually runs:
  every `finalize`, before the diff is offered for merge by any route. The
  auto-merge hold is unchanged and now names the files it held on, and a PR-only
  review decision whose diff touches the floor says so in its prompt, because
  PR-only work still asks a human to merge a diff. Every evaluation writes a
  `carveout_evaluated` health event carrying the matched paths, which is the
  observation that did not exist: "the carve-out held" and "the carve-out never
  ran" used to produce identical silence. `protected.matched_protected_paths`
  returns the matches rather than a bare bool, so a hold can state its reason.
  The real control is still that auto-merge is off fleet-wide; the carve-out is
  the backstop for the day that flag flips, which is why it must not be
  reachable only through the flag it exists to survive. Note the manifest half
  remains UNSIGNED (`"signature": null`, no caller passes `verify`), so the only
  unforgeable half is the hard-coded `_ALWAYS_PROTECTED` tuple. That is exactly
  what this call reaches with or without a manifest, so the repair does not wait
  on the signing work. `tests/test_autocode_carveout_reachability.py` pins
  reachability under BOTH `automerge_repos: []` and an enabled repo, so it fails
  if auto-merge is ever turned on while the carve-out path is unreachable, and
  also fails if the evaluation is ever re-guarded behind the auto-merge branch.
- **S21: the coverage arm reported a CLAIM, not a measurement, and a planted
  `coverage.xml` passed the twin gate.** `_stage_work` deliberately resets
  `coverage.xml` out of the staged index so CI byproducts never pollute the
  diff, and `_diff` reads `git diff --cached`, so the diff the LLM grader sees
  is structurally incapable of containing a `coverage.xml`. `diff_coverage`
  separately parsed whatever `coverage.xml` was on disk, with no returncode
  check on `cov_cmd`. A worker could therefore write a fabricated report and
  satisfy the coverage arm while the grader could not observe the reason. The
  exclusion is correct and is unchanged (staging untracked files is what makes a
  legitimate TDD change gradeable at all), so the repair is in `diff_coverage`:
  any pre-existing report is deleted before `cov_cmd` runs, the returncode is
  checked (and a hang is bounded by the local-CI ceiling), the emitted report's
  mtime must postdate the run, and a diff whose non-test source lines are absent
  from the report yields `None` instead of the old perfect `1.0` that an `omit`
  rule produced. Every refusal now records a `coverage_unusable` health event
  naming the reason, which is the observation that separates "coverage measured
  and met" from "the instrument never ran". Negative control, in
  `tests/test_autocode_coverage_integrity.py`: a planted `coverage.xml` claiming
  100 percent, with a score-5 grade, a `COMPLETE` promise and green CI, is
  driven through `EngineeringExecutor.run` and must NOT pass.
- **S21: the coverage instrument's own configuration is now on the protected
  floor.** `.coveragerc`, `pyproject.toml`, `pytest.ini`, `setup.cfg`,
  `tox.ini` and `conftest.py` decide what `--cov` measures, so a diff adding an
  `omit` rule blinds the arm that grades it without touching CI. That is the
  rubric hazard one level down, and the floor is hard-coded for the same reason
  the rubric is. Accepted cost, recorded rather than discovered later:
  `pyproject.toml` and `conftest.py` change for unrelated reasons, so under
  auto-merge these globs will route ordinary work to human review. The floor
  never blocks work, it only refuses to merge it unattended, and auto-merge is
  off fleet-wide today.
- **S21: cross-attempt failure memory now declares itself unverified.**
  `why_failed` is model-derived text distilled from one failed run's grader
  notes and fed into the next run's round one, which is the mechanism the
  2026-08-16 red-team accepted as decisive against the exploration slice (card
  f81d8d2d). The channel is kept and its existing bounds are unchanged (3
  distinct entries, 600 chars, one distilled line each, dedup, journal pointer);
  what was missing was epistemic status, so the block now says it carries
  reports from runs that FAILED, to be checked rather than believed.

### Added
- **A worker-independent outcome label: shadow mutation testing over changed
  lines** (card `33c50540`, new module `autocode/mutation.py`). The twin gate's
  CI and coverage arms are satisfied by tests the WORKER ITSELF AUTHORED, so a
  passing score partly measures the worker's ability to write a passing test.
  The epic's refusal to close a learning loop rested on that, and named an
  outcome label the worker did not author as its own reopen condition. Nobody
  owned building one. This is it: the mutation operators live in the harness,
  fixed, so a worker cannot author the mutants and cannot weaken them the way a
  diff can add an `omit` rule to a coverage config. A surviving mutant is a
  changed line the worker's own tests did not notice.
  - THREE states (`survived_clean`, `mutants_survived`, `unobserved`), with a
    deliberate asymmetry: a survivor is decisive even on a partial run, while
    the absence of a survivor over a capped or timed-out run is `unobserved`,
    never a clean sweep. A sampled label reported as universal is the failure
    this epic exists to remove, so a sampled run structurally cannot report a
    universal verdict.
  - Cost is bounded and the bound is recorded: changed lines only (mirroring
    `diff_coverage`), `max_mutants`, a wall-clock budget and a per-mutant
    timeout. A per-mutant timeout counts as unjudged, never as a kill; a red
    baseline is `unobserved`, never a perfect kill rate.
  - It rides the EXISTING outcome row; no second store. The seven `mutation_*`
    columns are DERIVED inside `record_run` from the raw probe report, the same
    pattern `escalation_state` follows, so no caller can stamp a state that
    disagrees with its own counts.
  - SHADOW ONLY. It gates nothing and nothing routes on it;
    `tests/test_autocode_mutation.py` section 5 proves that with a
    whole-package static sweep, a byte-identical twin-gate verdict and dispatch
    decision across all three states, and a check that no module calls
    `probe()`. Promoting it to a gate would destroy the property that makes it
    worth having.
  - Every live row today reads `unobserved` / `not_run`: the one site holding a
    live worktree at grade time is on the protected floor. The module therefore
    ships its own entry point (`python -m skharness.autocode.mutation`) so real
    data can be produced now. New read helper
    `autopilot_cost.mutation_summary()`, which over the live 183-row ledger
    reports `observed_fraction: 0.0` and `survival_rate: null` rather than a
    reassuring zero.
  - MEASURED, not estimated. Run against this card's own diff: 102 mutable
    sites, capped at 20, 19 mutants executed in 537.9s (28.3s each, so a
    complete run would have been ~48 minutes for one card), **12 killed and 7
    SURVIVED**. Those 7 survived the 43-test suite this card itself authored, a
    suite that passes and satisfies both the CI and the coverage arm. The run
    was incomplete and the `mutants_survived` verdict still stands, which is
    the asymmetry working: a survivor found in a sample is a real survivor.

- **pi now sends `x-session-id` and `x-sk-card-id` to skgateway, so a harness run
  can be joined to the gateway row that describes it.** Measured first: pi's
  baseline request carries no identifying header at all (host, Accept, the
  OpenAI-JS `X-Stainless-*` block, `authorization`, and nothing else), which is
  why `request_log.agent_id` and `session_id` are null for every harness run. pi
  reads a provider-level `headers` map out of the same `models.json` the adapter
  already generates per call, so this is a config change rather than new
  plumbing. Values are baked in as LITERALS and never as `$VAR` interpolation:
  with the variable unset pi makes NO request at all, reports an internal error,
  and still exits 0, which `_parse` turns into `{}`, so an unset variable would
  be a total failure invisible by exit code. Values are validated against a
  conservative token charset and REFUSED rather than escaped, because a leading
  `!` in a pi header value executes a shell command on every request. When no ids
  are supplied the `headers` key is absent entirely, never an empty map: "no
  session" and "session is empty" are different facts. `compat`'s session
  affinity keys are deliberately never written, so the harness is the single
  source of `x-session-id`.
- **A per-process agent / session / node identity, so two concurrent sessions on
  one box are no longer indistinguishable.** Every session on this fleet writes
  to the coordination board as the same AGENT name, so the overlay log's 2,027
  events carry only three distinct writers and four concurrent sessions collapse
  into one. `autocode.identity.resolve_identity()` mints a uuid4 session id once
  per process (honouring `SK_SESSION_ID` so a resumed run keeps its identity) and
  resolves the agent through the documented `SKAGENT` > `SKCAPSTONE_AGENT` >
  `SKMEMORY_AGENT` > `lumina` precedence. It also records WHICH variable it read,
  as `agent_var` / `session_id_var`, because that ambiguity is real in
  production: one systemd unit on .41 sets `SKAGENT=jarvis`,
  `SKMEMORY_AGENT=lumina` and `SKCHAT_IDENTITY=capauth:opus@skworld.io`
  simultaneously, and recording only the resolved value destroys information that
  cannot be reconstructed. A variable set to an empty or whitespace value counts
  as unset and falls through, so a bare `Environment=SKAGENT=` cannot pin the
  agent to the empty string. `AutocodeSessionRegistry.register()` now defaults its
  sid and host from the same resolver, so the on-disk session descriptor and the
  run record cannot disagree. Nothing backfills: historical events never carried a
  session id and any value written onto them now would be a guess.
- **Graded dispatch: a card's grade now selects the model.** `model_class` and
  `sensitivity` map onto a skgateway bucket id (`sk-<class>-<sensitivity>`),
  intended to replace the single static `autocode.config.harness_model`.
  **Correction (2026-08-16, card S15):** that replacement has not happened yet.
  Only the `pi` adapter implements `supports_model_override()`; `claude_code`
  and `opencode` still raise `ModelOverrideUnsupported`, `codex` is an
  unimplemented stub, no card currently carries a grade, and the gateway's
  `buckets_enabled` is off. `config.harness_model` remains the only live model
  selector today. See `docs/superpowers/specs/2026-08-16-b3-b4-closure-decisions.md`.
  Bucket ids are validated against the gateway's exact grammar BEFORE being
  sent, because a typo is not a loud error at the gateway: an id that fails
  the bucket regex is still caught as `sk-*` and falls through to the
  difficulty classifier, returning 200 from an arbitrary model with no
  sensitivity ceiling enforced. A single typo would otherwise discard every
  sovereignty guarantee.
- **A per-call model override seam** in the adapters, threaded like the existing
  `light` flag. `ModelOverrideUnsupported` is raised rather than silently
  dropping an override an adapter cannot honour, since dropping it would run on
  the static model and discard the requested ceiling with no visible signal.
  In the pi adapter a single `_effective_model()` feeds both the CLI argv and
  the injected `models.json`, so the two cannot disagree about which model was
  requested.
- An ungraded card sends no override and behaves exactly as before. It never
  constructs a bucket id, so it cannot address a trust zone at all. A *corrupt*
  grade raises instead of degrading to None, because degrading would fall back
  to the static model and silently drop the requested ceiling.

### Added (earlier in this release)
- **Joule work grading wired into `phase0_assess`.** A card is graded on three
  independent axes (`size`, `risk`, `sensitivity`) and the grade is written to
  the card via `skcoord.Board.set_grade`, so it is assigned once and not
  recomputed per dispatch. Two runs of the same card route identically.
- **`sensitivity.py`, a deterministic sensitivity classifier.** No model call.
  Sensitivity is the one axis a model must not guess: a classifier that is 95%
  right here is a credential leak 5% of the time. The model may propose a value
  and the deterministic rules override it unconditionally, in both directions.
  `public` is unreachable from the rules by design, because no keyword match can
  support the claim "this payload could be posted publicly"; it requires an
  explicit human override.
- **`.gitguardian.yaml`** scoping the scanner off `sensitivity.py` only. That
  file contains credential-shaped regexes because it is a detector, so the check
  could never pass by any correct change. `gitleaks` still covers the path.

### Added (pre-existing)
- `secret-scan` CI gate running the **gitleaks binary** over the full history.
- **`LICENSE` (GPL-3.0-or-later)** and a matching `license` field plus OSI
  classifier in `pyproject.toml`. The project previously declared no license at
  all, in the repo or in package metadata.
- `SOP.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, completing the seven files
  `SK_REPO_DOC_STANDARD` requires. `SOP.md` carries a `docs-evidence` block of 10
  hermetic checks pinning the entry point, port 9394, the wildcard-bind refusal,
  the shipped `ExecStart`, the inject/dispatch routes, the three scope names, the
  deny-all dispatch default, the fail-closed carve-out manifest, the absence of a
  health route, and the CI anti-skip gate.
- `.github/workflows/docs-check.yml` (tiers 1 and 2 for now; tier 3 is a
  follow-up once the gate has run clean).

### Fixed
- **The sandbox egress proxy no longer sends two `Host` headers.** `_forward()`
  built its outbound headers as a comprehension over `self.headers.items()`,
  which preserves the CLIENT's casing, so a client's lowercase `host` survived
  and the following `headers["Host"] = ...` added a SECOND, distinct dict key.
  `_HOP_BY_HOP` does not list host, so nothing removed the first, and both went
  on the wire. RFC 7230 section 5.4 says a server MUST reject that as 400; only
  skgateway's tolerance kept it invisible. The outbound dict is now
  `_RequestHeaders`, whose field names compare case-insensitively, so any header
  the proxy overrides replaces the client's whatever case it arrived in. Fixing
  the class of bug rather than special-casing Host, because the same trap was
  waiting for the next override. Proven by capturing raw bytes at a socket
  origin: a `BaseHTTPRequestHandler` upstream folds duplicate headers away and
  cannot observe this defect at all.
- **The sandbox egress proxy refuses `https://` absolute-URI forwards instead of
  silently downgrading them to cleartext.** `_target_host()` accepted https
  paths, but `_forward()` only ever built an `http.client.HTTPConnection` on
  `parsed.port or 80` and the module originates no TLS, so such a request went
  out in the clear, `Authorization` header included, by default to port 80. Not
  reachable through today's plain-HTTP skgateway flow, and https normally
  arrives as CONNECT (the separate blind tunnel, which is correct and stays end
  to end), but it was a live trap for the next sandbox pointed at an https
  endpoint. The proxy is a confinement boundary, so it fails closed: 501 with an
  explanation pointing at CONNECT. The allowlist check still runs first, so a
  denied host keeps its 403 rather than degrading into 501.
- **An autopilot config key this loader does not understand now raises instead of
  being silently dropped.** Measured on `~/.skcapstone/config/autopilot-pi.yaml`
  (2026-08-16): the file set `new_tasks_per_run: 1` at the TOP level, but
  `Config.load` only ever read that name out of the `caps:` block, so the key was
  filtered away and the default of 10 applied. Nothing distinguished a misplaced
  key from a correct one at runtime, and the operator who wrote 1 believed the run
  was capped at 1. `Config.load` now raises `ConfigError` on an unknown key at all
  three filtered levels (top level, `caps:`, each `repo_map:` entry), naming the
  file and the block. This follows the same fail-closed discipline as
  `types.coerce_quality` (falls to GATED, never to a permissive mode) and
  `buckets.BucketError` (raises rather than returning None, because returning None
  would silently widen). Dropping a key silently widens a cap. The known-key sets
  are derived from the dataclass fields so the lint cannot drift from the parser.
- **The work-grade policy is now on autocode's hard-coded protected floor.**
  `_ALWAYS_PROTECTED` covered `protected.py`, `engineering.py`, the fleet store,
  `itil.py`, the manifest and the freeze file, but not `grading.py`,
  `sensitivity.py`, `buckets.py`, the vendored grade vocabulary or the golden
  set. Once a card's grade selects the model that executes it, the rubric IS
  routing policy, so an engine routed by that rubric could edit it and merge the
  change behind a twin gate whose CI arm it satisfies with tests it wrote
  itself. This is the self-modification hazard CR-6.2 already ruled on for
  skos/skharness, applied to the files that decide the routing. The floor is
  hard-coded rather than manifest-driven because the realistic failure is not
  that someone removes an entry, it is that nobody ever adds one. The golden-set
  glob is versioned (`golden-set-*.json`) so promoting it to v2 does not drop it
  off the floor at exactly the moment it stops being a consistency check and
  becomes ground truth.
- **`README.md` no longer claims the daemon has no write surface.** It asserted
  "There is NO write surface: no spawn, inject, kill, dispatch, rename, archive,
  or model switch" and cited `tests/test_daemon.py::test_no_write_surface` as
  proof that POST/DELETE returned 405 and `/inject` and `/dispatch` returned 404.
  That test no longer exists, and `daemon.py` has shipped `POST
  /sessions/{sid}/inject`, `/ratify`, `/deny`, `/cancel` and `POST /dispatch`
  for some time. A stale security claim on the fleet's sandboxed execution
  bridge is the most dangerous kind of stale doc, because it is trusted. The
  README now documents the real routes, the four fail-closed gates in front of
  them, and the `skcode.stream` / `skcode.inject` / `skcode.dispatch` scope
  split. The same false claim was propagated into
  `systemd/skcode-hostd.service`, `systemd/README.md`, and `systemd/install.sh`,
  and is corrected there too (comments and docs only; no unit directive changed).
- `README.md` no longer reports "Status: v0.1.0" (the version is derived by
  setuptools-scm from the git tag) or "Mirror: smilinTux (private)" (the repo is
  public as of 2026-08-13).
- `systemd/skcode-hostd.env.example` and `systemd/README.md` no longer say the
  daemon runs the deny-all placeholder by default. Since CR-3.2
  `serve.select_verifier()` runs the **real** capauth verifier by default and
  falls back to deny-all only when capauth cannot be imported or when
  `SKCODE_FORCE_DENY_ALL` is set.

### Documented (no behaviour change)
- **Human ratification is FORMALLY DEMOTED as a control** (card `33c50540`,
  `docs/specs/2026-08-16-s23-worker-independent-label.md` section 5). The
  gaming analysis leaned on it hardest; measured against the live GTD store on
  2026-08-16 it answers at zero. 0 of 38 open autopilot decisions answered, 20
  of 82 archived, and **every answer ever recorded predates 2026-07-26**: the
  control did not decline gradually, it stopped. The mechanism is the
  built-but-unwired class (card `bb536f68`) applied to a human process:
  `digest.py` builds the morning manifest daily and never sends it (`sent_at:
  null`, and its own docstring says sending "is Phase F"), and the manifest
  counts 62 already-archived items, so the reply-by-number index would not even
  match the live queue if it were delivered. It is therefore removed from the
  list of controls any analysis in this epic may lean on. Listing a control
  that does not operate is worse than listing none, because it lets an analysis
  discharge a risk against something that has answered nothing in three weeks.
  No notification system was built; this is a written demotion, deliberately.
- **`SKCODE_DISPATCH_REPOS` unset means DENY ALL**, and the shipped env template
  deliberately omits the key, so a fresh install can dispatch nothing. The
  template now carries a commented, annotated entry explaining this, plus
  `SKCODE_FORCE_DENY_ALL`.
- **`skos` and `skharness` must never appear on a dispatch allowlist**: the
  self-modification hazard. The enforcement today is the deployed env value
  alone, with no code-level exclusion list, so the rule is now written down in
  `SOP.md`, `README.md`, `CONTRIBUTING.md`, and the env template.
- The `autocode/protected.py` carve-out detector: `_FAIL_CLOSED` protecting `**`
  on any manifest load failure, and the hardcoded `_ALWAYS_PROTECTED` floor.
- `skcode-hostd operator observe` **fails safe**, reporting all conditions
  healthy when hostd is unreachable. It is not a liveness probe, and there is no
  `/health` route.

## [0.3.14] - 2026-08-14

### Added
- **Cross-run failure memory (read half + call sites).** Closes the loop opened
  by `skcoord` 0.1.5.

  `engineering.py` threads in-run feedback round to round, but that state died
  when `run()` returned, and `direct.py` hardcoded `prior_feedback=None`. A
  fresh run on a previously-failed card therefore walked in blind.

  - `autocode/failure_memory.py` (new). `build_prior_feedback(item.payload)`
    renders the card's memory as bounded context or `None`: dedup on
    `(outcome, why_failed)` keeping newest, last **3** distinct by `ts`, one
    consequence line each, block hard-capped at **600 chars** with the oldest
    dropped first. Tolerant reader, so a card with no `meta` returns `None`,
    byte-identical to the previous fresh-start behaviour.
    `distill_failure()` reduces grader notes to the failing test id and
    assertion; traceback bulk stays in the run journal.
  - `engineering.py` records at the **two real terminal returns** (the no-op
    double-empty bail and did-not-converge) and seeds round 1 from the card.
  - `direct.py` records `direct_fail` and seeds `prior_feedback` from the card.
  - `finalize()` clears the card on a pass and archives the entries to the run
    journal, so a flaky-CI false failure haunts a card at most until its next
    pass.
  - `journal.py` gains `RunHandle.archive_attempts()`, which deliberately does
    not touch `state` (the orchestrator owns that field and writes it after
    `finalize` returns).

  **Two sites deliberately do NOT record**, each pinned by a regression test:
  `escalate()`, which the orchestrator calls for *every* non-passed result (a
  write there double-counts every failure), and the salvage return, which has
  `passed=False` but opened a CI-green human-review PR: that is a success, and
  recording it would poison future context.

  Every write site is best-effort with `health.record` on failure. Failure
  memory is an optimization and must never be the reason a build dies; tests
  cover a board that raises and a board lacking the method entirely.

  Spec: `docs/specs/2026-08-14-skharness-failure-memory.md`.
  Tests: 40 new cases, including a cross-repo round trip against a real
  `Board` and run journal.

- **CI test gate (`ci.yml`).** Until this existed, every PR check on this repo
  was GitGuardian, a secret scanner: the suite existed and nothing ran it, in
  the repo that owns the autopilot twin gate.
  - `lint` (blocking; nine pre-existing violations fixed to make it so)
  - `test` (3.12, real siblings installed from git main, full suite)
  - `compat-3-10` (proves the declared `requires-python` floor still imports;
    the full suite cannot run there because sibling `skos` is >=3.12)
  - `build` (sdist/wheel + `twine check`)

  The `test` job **fails the build if the cross-repo round trip skips**. Those
  tests self-skip when the sibling is missing, which is right on a dev box and
  wrong in CI, where running them is the entire point.

### Fixed
- **`pypi-publish` was skipped on every tag push, so releases published
  nothing.** GitHub propagates a skip *through* a job: `tag` is skipped on the
  tag-push path, `build` survived via `always()`, but a bare `needs: build` on
  `pypi-publish` still evaluated to skipped because an ancestor was. The build
  was green, the run reported success, and nothing was uploaded. **v0.3.13 was
  lost to this** (the tag was later deleted; PyPI went 0.3.12 → 0.3.14).
- Two test modules hard-imported optional siblings (`skos`, `capauth`) at module
  level, so their absence was a **collection error that took the whole suite
  down** in any clean environment. Both now guard with `pytest.importorskip`.

## [0.3.13] - 2026-08-14 [YANKED]

Tagged but **never published**: see the `pypi-publish` fix above. The tag was
deleted so the git tags match PyPI again.

## [0.3.12] and earlier

Not retrofitted. See `git log` and the release tags.

[Unreleased]: https://github.com/smilinTux/skharness/compare/v0.3.14...HEAD
[0.3.14]: https://github.com/smilinTux/skharness/releases/tag/v0.3.14
