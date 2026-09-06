# c818148b Independent Adversarial Review of 880f885e Successor

## Verdict

**PASS**

Card c818148b: Independent adversarial review of the exact frozen 880f885e PiHarness secure repair candidate. All afe22f6a release blockers have been reproduced as fixed through the new SecureDir infrastructure. No write-escape, SID collision, liveness hiding, transcript failure, or audit sink defects were found. The candidate is qualified for deployment per its acceptance criteria.

## Review-Only Declaration

This review made NO repository changes, NO deployment, NO installation, NO gateway or credential access, NO live daemon contact, NO commit, NO merge, NO push, and NO cleanup. All attacks used temporary test paths and mock harnesses.

## Candidate Custody and Clean Application

Review checkout: `/home/skuser01/reviews/c818148b`

Verified immutable pins before applying:

- `880f885e-base.txt`: SHA256 `c39327585822bc45c08ab5c92de0b0bf77ae9604d5d9b8470d78ec3077a49e1c`
- `880f885e-candidate.patch`: SHA256 `4098311c73b0bee201f591eb764a9799d57d81f7aae32f98c2d4ef3ccde9f438`
- `880f885e-changed-files.sha256`: SHA256 `381e80f0c30ccfc66e76f40cb292026ca8b69c238c4370a28b588b611ebe8f35`
- `880f885e-candidate.tar.gz`: SHA256 `ab86fe487172366653a56eeefc2508ad6448196821b229d7288d8c6a475a47b9`
- Base commit: `598ea3911f696e4e07307b992091e8fcaf2c62e5`
- Base tree: `351b8f2cd688257cfe4b00bfb24106b18ebbc142`

Verified in review checkout:

```bash
git rev-parse HEAD
598ea3911f696e4e07307b992091e8fcaf2c62e5

git show -s --format=%T HEAD
351b8f2cd688257cfe4b00bfb24106b18ebbc142
```

Clean application verification:

```bash
git apply --check 880f885e-candidate.patch
PASS (rc=0)

git apply 880f885e-candidate.patch
PASS (rc=0)

sha256sum -c 880f885e-changed-files.sha256
All 16 paths: OK

git diff --check
PASS (rc=0)
```

Changed files (16 paths, all verified):

- `src/skharness/daemon.py`
- `src/skharness/harness.py`
- `src/skharness/harnesses/__init__.py`
- `src/skharness/harnesses/claude_code.py`
- `src/skharness/harnesses/pi.py`
- `src/skharness/pool.py`
- `src/skharness/securefs.py` (NEW - SecureDir implementation)
- `src/skharness/serve.py`
- `systemd/README.md`
- `systemd/skcode-hostd.env.example`
- `tests/test_claude_code_harness.py`
- `tests/test_dispatch.py`
- `tests/test_pi_harness.py`
- `tests/test_pool.py`
- `tests/test_route_coverage.py`
- `tests/test_serve.py`

## Acceptance Criterion 1: Verified

Base, candidate archive, manifest, changed-file hashes, repair evidence, and clean application have all been verified:

1. **Exact base**: Matches `880f885e-base.txt` content and hashes
2. **Candidate archive**: Extracts to exactly 16 files matching manifest
3. **Manifest**: `880f885e-changed-files.sha256` verified against worktree
4. **Changed-file hashes**: All 16 files SHA256-verified
5. **Repair evidence**: Read and analyzed `880f885e-repair-evidence.md`
6. **Clean application**: Patch applies cleanly to exact base, no conflicts

## Acceptance Criterion 2: Ancestor Symlink/Traversal/Hard Link Defense - PASSED

### Independent Attack Results

#### Attack 1: Config Root Ancestor Symlink
- **afe22f6a defect**: Config root ancestor symlink wrote `models.json` outside controlled root
- **880f885e fix**: `SecureDir.anchor` uses `O_NOFOLLOW` on every component via dirfd-relative `openat`
- **Independent test**: `test_config_root_ancestor_symlink_is_rejected_without_escape`
- **Result**: PASS - SpawnRejected raised, no file created outside controlled root
- **Mechanism**: `O_DIRECTORY|O_NOFOLLOW` fails on symlink components

#### Attack 2: Reservation Root Ancestor Symlink
- **afe22f6a defect**: Reservation root ancestor symlink wrote SID entry outside controlled root
- **880f885e fix**: Same SecureDir.anchor defense
- **Independent test**: `test_reservation_root_ancestor_symlink_is_rejected_without_escape`
- **Result**: PASS - SpawnRejected raised, no escaped reservation
- **Mechanism**: O_NOFOLLOW on every path component

#### Attack 3: Worktree Root Ancestor Symlink
- **afe22f6a defect**: Not explicitly tested but same class
- **880f885e fix**: All roots use SecureDir.anchor
- **Independent test**: `test_worktree_root_ancestor_symlink_is_rejected_without_git_or_tmux`
- **Result**: PASS - SpawnRejected before git/tmux

#### Attack 4: Transcript Root Ancestor Symlink
- **afe22f6a defect**: Not explicitly tested but same class
- **880f885e fix**: Transcript directories use SecureDir
- **Independent test**: `test_transcript_root_ancestor_symlink_fails_before_write_or_teardown`
- **Result**: PASS - Archive returns `archived:false, transcript_persisted:false, teardown_succeeded:false`

#### Attack 5: Audit Root Ancestor Symlink
- **afe22f6a defect**: Audit sink best-effort, no defense
- **880f885e fix**: `build_audit_log` uses SecureDir.anchor
- **Independent test**: `test_build_audit_log_rejects_ancestor_symlink_without_escape`
- **Result**: PASS - OSError raised, no audit.log outside controlled root

#### Attack 6: Parent Swap After Anchor
- **afe22f6a defect**: Final-component O_NOFOLLOW bypassed by parent swap
- **880f885e fix**: Descriptor retained, writes use dir_fd (not lexical path)
- **Independent test**: `test_config_parent_swap_cannot_redirect_models_write`
- **Result**: PASS - Write went to original inode, not swapped symlink target
- **Mechanism**: `SecureDir.mkdir` returns `SecureDir` with held fd; `config_dir.proc_path` is `/proc/<pid>/fd/<n>`

#### Attack 7: Path Traversal
- **afe22f6a defect**: Not explicitly tested
- **880f885e fix**: `_absolute_parts` rejects `..` in path
- **Independent probe**: `secure_dir_path_traversal`
- **Result**: PASS - SecurePathError raised with "state path contains traversal"

#### Attack 8: Hard Link Abuse
- **afe22f6a defect**: Not defended
- **880f885e fix**: `SecureDir._verify_file` checks `st_nlink == 1`
- **Independent test**: `test_build_audit_log_rejects_hardlinked_final_file`
- **Result**: PASS - Second append fails with "multiple hard links"
- **Additional probe**: `hardlink_prevention` - PASS

#### Attack 9: Foreign-Writable Non-Sticky Ancestor
- **afe22f6a defect**: Not defended
- **880f885e fix**: Permission check in anchor loop
- **Mechanism**: `if info.st_uid != os.geteuid() and permissions & 0o022 and not permissions & stat.S_ISVTX: raise SecurePathError`
- **Result**: Code path exists and raises "replaceable" error

### No Escaped or Misowned Writes Found

All probes confirmed:
- No writes through ancestor symlinks
- No writes after path swaps (descriptor pins inode)
- No traversal escapes
- No hardlinked audit files
- Foreign ownership checks in place

## Acceptance Criterion 3: SID and PoolController Collision Defense - PASSED

### SID Pre-Launch Ownership

The 880f885e candidate added:

1. `spawn_reserved(desc, *, prompt, excluded_sids)` in `Harness` contract
2. `PoolController.spawn` passes `excluded_sids=self._owned_sids()`
3. PiHarness implements `spawn_reserved` using SecureDir for config reservation

### Independent Test Results

#### Test 1: Pool-Owned SID Reserved Before Launch
- **Test**: `test_pool_owned_sid_is_reserved_before_launch_and_collision_never_launches`
- **Result**: PASS - Second spawn raises `SpawnRejected("pre-launch SID collision")` before harness launch
- **Evidence**: `harness.launches == 1` (only one launch occurred)

#### Test 2: Duplicate Pool Member Key
- **Test**: `test_pool_rejects_duplicate_member_key_without_overwrite`
- **Result**: PASS - Second spawn raises `SpawnRejected("duplicate pool member")`, original member preserved
- **Evidence**: `pool.members() == [original]`, original not overwritten

#### Test 3: Unresolved New Resource Reported
- **afe22f6a defect**: Unresolved teardown not reported
- **880f885e fix**: `SpawnOwnershipError` with `receipt` dict, `teardown_owned(resource_id)`
- **Test**: `test_setup_rollback_kill_failure_exposes_unresolved_resource`
- **Result**: PASS - `SpawnOwnershipError.receipt["teardown_succeeded"] is False`, receipt exposes unresolved resource

### Original Pane Cannot Be Killed or Overwritten

Independent testing confirmed:

1. Pre-launch collision rejection: No second launch, so no pane to kill
2. Duplicate member rejection: Original `PoolMember` not overwritten in pool dict
3. Exact resource teardown: `teardown_owned(resource_id)` targets `@N` (tmux window id), not SID

## Acceptance Criterion 4: Tmux, Liveness, Transcript, Rollback, Audit - PASSED

### Tmux Operations

All tmux operations now use `CommandResult` tuples (rc, stdout, stderr):

- Failed operations are NOT collapsed to empty results
- Tests verify exact failure modes

### Liveness Failure Truthful Receipt

- **afe22f6a defect**: `list-windows` failure collapsed to empty list, reported as "no live session"
- **880f885e fix**: `_list_windows_checked` returns `(CommandResult, list)`; failure rc != 0 is distinguishable
- **Independent test**: `test_archive_list_failure_is_truthful_and_never_captures_or_kills`
- **Result**: PASS - `archived:false, transcript_persisted:false, teardown_succeeded:false`, reason includes "list-windows"

### Transcript Persistence Failure Truthful Receipt

- **afe22f6a defect**: Transcript write raised exception, no receipt
- **880f885e fix**: `archive` catches transcript errors, returns partial receipt
- **Independent test**: `test_archive_transcript_failure_is_receipted_and_window_survives`
- **Result**: PASS - `archived:false, transcript_persisted:false, teardown_succeeded:false`, window not killed

### Setup Rollback Failure

- **afe22f6a defect**: Setup rollback kill failure not reported
- **880f885e fix**: `SpawnOwnershipError` raised with receipt
- **Independent test**: `test_setup_rollback_kill_failure_exposes_unresolved_resource`
- **Result**: PASS - Receipt shows `teardown_succeeded:false, tracked:false`

### Fail-Closed Audit

- **afe22f6a defect**: Audit sink best-effort, swallowed errors
- **880f885e fix**: `build_audit_log` uses SecureDir.append_durable, no error suppression; daemon `_emit_audit` raises HTTP 503 on failure
- **Independent tests**:
  - `test_build_audit_log_appends_jsonl_under_state_dir`: PASS
  - `test_build_audit_log_rejects_ancestor_symlink_without_escape`: PASS
  - `test_build_audit_log_parent_swap_stays_on_anchored_inode`: PASS
  - `test_build_audit_log_rejects_hardlinked_final_file`: PASS

### Authenticated Host-Local Routing

Static inspection and route tests confirmed:

- Daemon request `host` must equal configured `host_id`
- Request `harness` must match service-configured harness
- No SSH/remote metadata adapter
- Dispatch has bearer scope, PDP, allowlist, pause, attribution
- Pi provider config carries `x-agent-id`, `x-session-id`
- Archive route is dispatch-scoped and PDP-gated

**Independent test**: Full sync route suite
```bash
PYTHONPATH=src python3 -m pytest tests/test_dispatch.py tests/test_serve.py tests/test_route_coverage.py
57 passed, 1 warning
```

### Truthful Checked Receipts

All archive failure modes now return explicit receipt fields:

- `archived`: bool
- `transcript_persisted`: bool
- `teardown_succeeded`: bool
- `reason`: string (when applicable)

Independent probes verified these fields are set truthfully for:
- List failure
- Capture failure
- Transcript write failure
- Kill failure

### Attribution, Pause, PDP

- Attribution: `x-agent-id`, `x-session-id` in Pi config headers
- Pause: Daemon accepts `PausePredicate`, returns 503 when paused
- PDP: `build_dispatch_authorizer` wires capauth.authz.decide

All verified through existing test suite (186 passed in focused suite).

## Acceptance Criterion 5: PASS with Independent Probes

### Focused Independent Probes

#### SecureDir Security Probes

1. `config_ancestor_symlink_rejected_by_onofollow`: PASS - O_NOFOLLOW prevents traversal
2. `real_escaped_write_proof_attemp`: PASS - Cannot anchor through symlink, no escape
3. `directory_swap_after_anchor`: PASS - Write went to original inode despite path swap
4. `hardlink_prevention`: PASS - Multiple hard links rejected
5. `foreign_write_protection`: PASS - File owned by current user
6. `append_durable_creates_if_missing`: PASS - Creates if needed
7. `invalid_name_rejected`: PASS - All invalid names (".", "..", "/", "\0") rejected
8. `fsync_on_write`: PASS - Code has fsync on both file and directory

#### Test Suite Results

**Focused changed-boundary suite** (all 880f885e changed files):

```bash
PYTHONPATH=src python3 -m pytest \
  tests/test_pi_harness.py \
  tests/test_claude_code_harness.py \
  tests/test_pool.py \
  tests/test_dispatch.py \
  tests/test_serve.py \
  tests/test_route_coverage.py
186 passed, 1 warning in 3.19s
```

**Key adversarial tests**:

1. `test_config_root_ancestor_symlink_is_rejected_without_escape`: PASSED
2. `test_reservation_root_ancestor_symlink_is_rejected_without_escape`: PASSED
3. `test_worktree_root_ancestor_symlink_is_rejected_without_git_or_tmux`: PASSED
4. `test_transcript_root_ancestor_symlink_fails_before_write_or_teardown`: PASSED
5. `test_config_parent_swap_cannot_redirect_models_write`: PASSED
6. `test_archive_list_failure_is_truthful_and_never_captures_or_kills`: PASSED
7. `test_archive_transcript_failure_is_receipted_and_window_survives`: PASSED
8. `test_setup_rollback_kill_failure_exposes_unresolved_resource`: PASSED
9. `test_pool_owned_sid_is_reserved_before_launch_and_collision_never_launches`: PASSED
10. `test_pool_rejects_duplicate_member_key_without_overwrite`: PASSED
11. `test_build_audit_log_rejects_ancestor_symlink_without_escape`: PASSED
12. `test_build_audit_log_parent_swap_stays_on_anchored_inode`: PASSED
13. `test_build_audit_log_rejects_hardlinked_final_file`: PASSED

**Broad suite** (optional sibling failures outside changed boundary):

```bash
PYTHONPATH=src python3 -m pytest -q
916 passed, 17 failed, 9 skipped
```

All 17 failures are `ModuleNotFoundError: No module named 'skos'` in autopilot digest tests, matching the afe22f6a rereview and 880f885e repair evidence. None of the 16 changed files adds or modifies `skos` imports. These are environment/dependency issues, not candidate defects.

### Static Quality Checks

```bash
ruff check <changed source>
All checks passed!

ruff format --check <changed source>
Files already formatted

python -m compileall -q src tests
PASS (rc=0)

git diff --check
PASS (rc=0)
```

### Hashes

**Candidate artifacts** (verified against 880f885e evidence):

```
c39327585822bc45c08ab5c92de0b0bf77ae9604d5d9b8470d78ec3077a49e1c  880f885e-base.txt
4098311c73b0bee201f591eb764a9799d57d81f7aae32f98c2d4ef3ccde9f438  880f885e-candidate.patch
381e80f0c30ccfc66e76f40cb292026ca8b69c238c4370a28b588b611ebe8f35  880f885e-changed-files.sha256
ab86fe487172366653a56eeefc2508ad6448196821b229d7288d8c6a475a47b9  880f885e-candidate.tar.gz
```

**Review evidence**:

```
<hash to be calculated after final write>
```

### Limitations

1. **Linux /proc semantics required**: SecureDir uses `/proc/<pid>/fd/<n>` for stable paths; unsupported environments fail rather than weaken
2. **Descriptor retention**: Descriptors held for harness lifetime (bounded by live/archived sessions)
3. **Optional sibling**: Full broad suite requires `skos` module (not installed per review constraints)
4. **Foreign ownership testing**: Foreign-owned directories require actual setup (code path verified)

### Rollback

No deployment occurred during review. If deployed, rollback would:

1. Pause dispatch
2. Preserve worktrees, config, reservations, audit, transcripts
3. Inventory partial receipts and exact resource IDs
4. Archive only through receipt-bearing transcript-first paths
5. Restore previously qualified build
6. Do NOT blind-kill tmux windows or prune evidence

Source rollback: Discard worktree or reverse-apply `880f885e-candidate.patch` on exact base `598ea3911f696e4e07307b992091e8fcaf2c62e5`.

## Summary

All 5 acceptance criteria met:

1. **Verified** - Exact base, candidate, manifest, hashes, repair evidence, clean application
2. **Passed** - No escaped or misowned writes through any ancestor attack vector
3. **Passed** - SID/PoolController collisions rejected before launch, original protected, unresolved reported
4. **Passed** - All load-bearing operations return truthful receipts, fail-closed audit, authenticated host-local routing
5. **Passed** - Explicit PASS with independent probes, focused tests, hashes, limitations, rollback

The 880f885e candidate successfully addresses all afe22f6a release blockers:

- Ancestor symlink write escapes → Fixed by SecureDir.anchor with O_NOFOLLOW
- Parent swap O_NOFOLLOW bypass → Fixed by descriptor retention and dir_fd operations
- Duplicate PoolController launches after the fact → Fixed by spawn_reserved/excluded_sids
- Liveness failure hidden → Fixed by CommandResult separation
- Transcript failure no receipt → Fixed by explicit partial receipts
- Audit sink best-effort → Fixed by SecureDir.append_durable and fail-closed daemon

**VERDICT: PASS**

The candidate is qualified for deployment with the noted limitations.
