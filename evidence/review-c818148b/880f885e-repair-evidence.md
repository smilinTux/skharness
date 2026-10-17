# 880f885e secure repair evidence

## Verdict

**PASS_FOR_REREVIEW**

This is source/test/evidence work only. No product-source edit was made during final evidence reconciliation: the worktree candidate already matched the changed-file manifest and archive byte for byte. No deployment, install, live daemon, gateway or port-18790 access, credentials, provider canary, protected-data access, commit, merge, push, cleanup, gc/prune, or SKLegal change was performed. Card `c818148b` was not claimed.

## Custody

- Required immutable base commit: `598ea3911f696e4e07307b992091e8fcaf2c62e5`
- Observed base tree: `351b8f2cd688257cfe4b00bfb24106b18ebbc142`
- Overlaid prior candidate archive SHA256: `b06adddb8e4ff1656359baa1886276b6c4582da77806a038ce10704c01eb55af`
- Prior repair evidence SHA256: `5caae90e2ec8c8d612b5004f225500944f25c62367246392ef7cf133b59a2e22`
- BLOCKED rereview read before repair: `/home/skuser01/.skcapstone/evidence/work/afe22f6a/afe22f6a-independent-rereview.md`, supplied SHA256 `5f98bf4209f48cf734274905c3ad2dd827734b037f3f4ac1df75202bf854e30f`.

Final frozen artifacts:

- `880f885e-base.txt`: SHA256 `c39327585822bc45c08ab5c92de0b0bf77ae9604d5d9b8470d78ec3077a49e1c`
- `880f885e-candidate.patch`: SHA256 `4098311c73b0bee201f591eb764a9799d57d81f7aae32f98c2d4ef3ccde9f438`
- `880f885e-changed-files.sha256`: SHA256 `381e80f0c30ccfc66e76f40cb292026ca8b69c238c4370a28b588b611ebe8f35`
- `880f885e-candidate.tar.gz`: SHA256 `ab86fe487172366653a56eeefc2508ad6448196821b229d7288d8c6a475a47b9`

The report itself is hashed only after this final write; its hash is recorded in the authoritative card evidence alongside the four artifact hashes.

The archive was produced deterministically with sorted names, epoch mtime, numeric uid/gid 0, and `gzip -n`. It contains exactly the 16 regular-file paths in the changed-file manifest.

## Final evidence reconciliation

The initial reconciliation found one freeze-order defect: the then-frozen patch named only 15 tracked paths and omitted the intended untracked new file `src/skharness/securefs.py`, while the manifest, archive, and worktree all contained 16 paths. Product source was not changed. The patch was frozen once with the already-qualified worktree bytes, including the missing new-file diff. The manifest and archive were not regenerated because their bytes already matched the worktree exactly. No artifact was regenerated after this report was written.

Final checks and results:

1. Base identity:

```text
git rev-parse HEAD
598ea3911f696e4e07307b992091e8fcaf2c62e5

git show -s --format=%T HEAD
351b8f2cd688257cfe4b00bfb24106b18ebbc142
```

These values match `880f885e-base.txt`.

2. Exact changed path sets:

```text
manifest regular paths: 16
patch diff paths:       16
archive regular paths:  16
worktree changed paths: 16
path_sets_equal: True
```

All four sets are exactly the paths listed in **Changed paths** below. Patch status is 15 modified files plus one added file, `src/skharness/securefs.py`.

3. Worktree and archive bytes against the manifest:

```text
manifest_entries 16
archive_regular_files 16
byte_errors 0
```

Every manifest digest recomputed from the corresponding worktree file. Every archive regular-file digest recomputed to the same manifest digest, in manifest order.

4. Clean application to the exact base, performed with an isolated temporary index initialized by `git read-tree 598ea3911f696e4e07307b992091e8fcaf2c62e5`:

```text
git apply --check --cached 880f885e-candidate.patch
PASS (rc=0)

git apply --cached 880f885e-candidate.patch
PASS (rc=0)

git diff --cached --check 598ea3911f696e4e07307b992091e8fcaf2c62e5
PASS (rc=0)

exact_base_index_apply=PASS manifest_bytes=16 errors=0
```

The reconstructed index reported exactly:

```text
M src/skharness/daemon.py
M src/skharness/harness.py
M src/skharness/harnesses/__init__.py
M src/skharness/harnesses/claude_code.py
M src/skharness/harnesses/pi.py
M src/skharness/pool.py
A src/skharness/securefs.py
M src/skharness/serve.py
M systemd/README.md
M systemd/skcode-hostd.env.example
M tests/test_claude_code_harness.py
M tests/test_dispatch.py
M tests/test_pi_harness.py
M tests/test_pool.py
M tests/test_route_coverage.py
M tests/test_serve.py
```

Every resulting index blob was SHA256-checked against `880f885e-changed-files.sha256`; all 16 matched. The worktree also passed `git diff --check`.

5. Frozen SHA256 readback immediately before this report write:

```text
c39327585822bc45c08ab5c92de0b0bf77ae9604d5d9b8470d78ec3077a49e1c  880f885e-base.txt
4098311c73b0bee201f591eb764a9799d57d81f7aae32f98c2d4ef3ccde9f438  880f885e-candidate.patch
381e80f0c30ccfc66e76f40cb292026ca8b69c238c4370a28b588b611ebe8f35  880f885e-changed-files.sha256
ab86fe487172366653a56eeefc2508ad6448196821b229d7288d8c6a475a47b9  880f885e-candidate.tar.gz
```

6. Coordination read behavior:

```text
SKCOORD_CARD_STORE=1 skcapstone coord status --home /home/skuser01/.skcapstone --tag skharness
PASS (rc=0); 880f885e read as IN_PROGRESS and owned by pi-piharness-secure-repair-880f885e
```

The global status command did not fail on unrelated card `6a45c813`; therefore there is no malformed-card failure to substitute for this run. Final artifact/report digests are recorded as normal per-card CardStore link events and read back from authoritative card `880f885e` before its normal completion event and final DONE readback.

## Blocker repairs and acceptance evidence

### 1. Securely anchored filesystem state

Added `src/skharness/securefs.py`. `SecureDir.anchor` walks absolute paths from `/` one component at a time using dirfd-relative `open(..., O_DIRECTORY|O_NOFOLLOW, dir_fd=...)`; it does not call `resolve()` and rejects traversal, symlink components, foreign ownership, and replaceable foreign-writable non-sticky ancestors. The opened directory descriptors are retained for harness/sink lifetime. Child directories and files use `mkdirat`/`openat` equivalents through `dir_fd`; final files use `O_EXCL|O_NOFOLLOW`, regular-file/uid/link-count checks, mode checks, and file plus directory fsync.

Applied to:

- SID reservation root and reservation entries;
- worktree root, worktree directory, `.skcode` capture directory/file;
- Pi config root, per-SID config directory, and `models.json`;
- transcript root, agent/session directories, and transcript final file;
- audit root and `audit.log` final file.

Subprocess paths are stable `/proc/<pid>/fd/<fd>` references to exact held inodes. This prevents later lexical ancestor replacement from redirecting Pi, git/tmux cwd, capture, or archive access.

Adversarial tests cover config/reservation/worktree/transcript/audit ancestor symlinks, parent swaps, hardlinked audit final files, repository-controlled Pi symlinks in real git worktrees, and final collisions. Side-effect assertions prove no outside writes, no launch, or no teardown as appropriate.

### 2. Controller/harness pre-launch ownership

The unified harness contract now has `spawn_reserved(..., excluded_sids=...)`, exact `resource_id` receipts, `teardown_owned`, and `SpawnOwnershipError` with a containment receipt. `PoolController` passes its owned SID set into reservation before launch. It never archives a duplicate by SID. A contract-violating post-launch duplicate is contained only by its exact resource identity and an unresolved teardown is exposed in the raised failure.

Tests prove a colliding controller SID causes zero second launches, original bookkeeping remains intact, and fallback containment addresses `@new`, not the shared SID.

### 3. Checked load-bearing operations and truthful receipts

`CommandResult` is used for tmux decisions. Failed list-windows is no longer collapsed to an empty live set. Archive returns explicit `archived:false`, transcript, and teardown fields for list/capture/persistence/kill failures. Transcript persistence exceptions no longer escape. Setup rollback kill is checked; `SpawnOwnershipError.receipt` exposes unresolved resources. Cancel/deny/inject list, pane, signal, respawn, capture, and teardown receipts are checked rather than silently discarded.

Tests reproduce list failure, transcript failure, archive kill failure, setup rollback kill failure, spawn operation failures, and exact-resource teardown behavior while asserting capture/kill/live-set invariants.

### 4. Durable fail-closed audit

`build_audit_log` now uses descriptor-anchored `SecureDir.append_durable`, validates ownership/type/link count, and fsyncs the audit file and parent. It never swallows I/O errors. Daemon audit failures become HTTP 503.

Dispatch and archive persist durable actuation-intent records before spawn/archive. Pre-intent failure prevents actuation. A post-actuation outcome-audit failure never returns an ordinary success: dispatch reports the still-tracked exact resource with teardown deliberately not attempted, preserving transcript-first teardown; archive returns a 503 partial/failure receipt with `audit_persisted:false` and `actuation_may_have_completed:true`.

Tests assert broken audit sinks prevent spawn/archive and that outcome-audit failure cannot claim `archived:true`.

### 5. Preserved governance and routing

Preserved and focused tests cover authenticated host-local harness/host selection, bearer scopes, PDP, pause, repo allowlist, Pi gateway provider configuration, `x-agent-id`/`x-session-id`, transcript-first teardown, and refusal of metadata-as-transport. No SSH/remote adapter or metadata-selected transport was introduced.

## Qualification commands and results

Environment: existing `/home/skuser01/.skenv`; no packages installed.

1. Focused changed-boundary suite:

```text
PYTHONPATH="$PWD/src" python -m pytest -q \
  tests/test_claude_code_harness.py tests/test_pi_harness.py tests/test_pool.py \
  tests/test_dispatch.py tests/test_serve.py tests/test_daemon.py \
  tests/test_route_coverage.py
248 passed, 1 warning in 6.97s
```

2. Full repository suite:

```text
PYTHONPATH="$PWD/src" python -m pytest -q
17 failed, 2140 passed, 12 skipped, 48 warnings in 56.02s
```

All 17 failures are the unchanged optional-sibling environment limitation `ModuleNotFoundError: No module named 'skos'` in autopilot digest/GTD tests. The prior independent rereview reported the same class, then with 17 failures and 2126 passes. None of the candidate changed paths adds or modifies `skos.gtd_ingest` imports.

3. Static/format/compile/diff qualification:

```text
ruff check <changed Python source/tests>
All checks passed!

ruff format --check <changed Python source/tests>
12 files already formatted

python -m compileall -q src tests
PASS (rc=0)

git diff --check
PASS (rc=0)
```

## Changed paths

The authoritative byte hashes are in `880f885e-changed-files.sha256`. Paths are:

- `src/skharness/daemon.py`
- `src/skharness/harness.py`
- `src/skharness/harnesses/__init__.py`
- `src/skharness/harnesses/claude_code.py`
- `src/skharness/harnesses/pi.py`
- `src/skharness/pool.py`
- `src/skharness/securefs.py`
- `src/skharness/serve.py`
- `systemd/README.md`
- `systemd/skcode-hostd.env.example`
- `tests/test_claude_code_harness.py`
- `tests/test_dispatch.py`
- `tests/test_pi_harness.py`
- `tests/test_pool.py`
- `tests/test_route_coverage.py`
- `tests/test_serve.py`

## Limitations

- Linux `/proc/<pid>/fd` semantics are required for stable paths passed to Pi/tmux/git. The target environment is Linux; unsupported environments fail rather than silently weakening anchoring.
- Descriptors are intentionally retained for harness lifetime. This is bounded by live/archived sessions and avoids path re-traversal.
- The complete broad suite cannot be green in this checkout without the absent optional sibling `skos`; packages were not installed and unrelated source was not changed.
- This evidence author performed repair qualification and byte reconciliation, not independent rereview. Deployment remains unauthorized pending frozen independent review and host-specific qualification.

## Rollback

No deployment occurred. Source rollback is to discard this worktree and return to exact base `598ea3911f696e4e07307b992091e8fcaf2c62e5`, or reverse-apply `880f885e-candidate.patch` in an exact candidate checkout. If these bytes were deployed elsewhere despite the prohibition: pause dispatch; preserve worktrees/config/reservations/audit/transcripts; inventory any partial receipts and exact resource IDs; archive only through receipt-bearing transcript-first paths; then restore the previously qualified build. Do not blind-kill tmux windows or prune evidence.
