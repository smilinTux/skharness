# Changelog

All notable changes to `skharness` are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/spec/v2.0.0.html). The git tag IS the
version (setuptools-scm); a release is cut by pushing a `v*` tag, or by
dispatching `publish.yml` on `main`, which cuts the next patch tag itself.

## [Unreleased]

### Fixed
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
- **Graded dispatch: a card's grade now selects the model.** `model_class` and
  `sensitivity` map onto a skgateway bucket id (`sk-<class>-<sensitivity>`),
  replacing the single static `autocode.config.harness_model`. Bucket ids are
  validated against the gateway's exact grammar BEFORE being sent, because a
  typo is not a loud error at the gateway: an id that fails the bucket regex is
  still caught as `sk-*` and falls through to the difficulty classifier,
  returning 200 from an arbitrary model with no sensitivity ceiling enforced. A
  single typo would otherwise discard every sovereignty guarantee.
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
