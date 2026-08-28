# a930e3c4 independent re-review

Verdict: PASS

Reviewer identity: `pi-codex-chiap03-a930e3c4`
Review host: `chiap03`
Review date: 2026-08-28
Candidate repository: `smilinTux/skharness`
Candidate branch: `fix/d13d6eb2-bootstrap-disclosure`
Candidate pull request: https://github.com/smilinTux/skharness/pull/75
Exact fetched commit reviewed: `d30deb7d7685cad09a4c88efad09e3acdbef72f2`
Candidate implementation commit: `46c8cb67fa4f83e82f4b46d989b67e4b1662238f`
Candidate review-report pull request: https://github.com/smilinTux/skharness/pull/86

## Identity and independence

The preparer recorded on card d13d6eb2 is `pi-codex-d13d6eb2`. The prior reviewer recorded on card c7d721da and in PR 84 is `pi-glm-c7d721da`. This reviewer is `pi-codex-chiap03-a930e3c4`, which differs exactly from both identities.

## Fetch and byte identity

I fetched `origin/main` and `origin/fix/d13d6eb2-bootstrap-disclosure` from `https://github.com/smilinTux/skharness.git` into a detached, card-specific worktree under `SKFLEET_WORKSPACE`. Both the fetched remote-tracking branch and the detached worktree resolved to `d30deb7d7685cad09a4c88efad09e3acdbef72f2`. GitHub PR metadata also reported that exact head OID. The candidate worktree remained clean after examination and testing.

## d13d6eb2 acceptance criteria

### AC1: no pre-final same-uid reference, with mechanism stated

Result: PASS.

Examined:

* `src/skharness/securefs.py`, especially `_private_proc_fds`, `_new_unlinked_file`, `_publish_exclusive`, `write_exclusive`, and `append_durable`.
* `src/skharness/harnesses/pi.py:181-236`, where private `models.json` configuration is written through `SecureDir.write_exclusive`.
* `src/skharness/serve.py:224-238`, where mandatory audit records are written through `SecureDir.append_durable`.
* `CHANGELOG.md`, which records the mechanism and affected data.

Mechanism found: a process-wide reentrant lock surrounds Linux `prctl(PR_SET_DUMPABLE, 0)` before creation of each sensitive anonymous `O_TMPFILE` inode. Protection remains active while mode `0600` is established by the create call, bytes are written, the inode is fsynced, link count and ownership are validated, and the completed inode is published. The prior dumpable state is restored only after publication. Configuration is never mutated after publication. Audit append uses copy-on-write, so a link to an old published inode cannot receive new mandatory records.

### AC2: concurrent same-uid regression and mutation sensitivity

Result: PASS.

Examined:

* `tests/test_pi_harness.py:27-47`, worker `_same_uid_link_worker`.
* `tests/test_pi_harness.py:503-535`, parameterized regression `test_same_uid_process_cannot_link_anonymous_inode_before_first_write` for both `models.json` configuration and `audit.log` mandatory audit bytes.
* Durable mutation output `~/.skcapstone/evidence/work/d13d6eb2/regression-reopened.txt`, sha256 `41c3fbcf50a6e7737b93387587c6a06cf2b245437d82f79b3e12133695a82140`.

The test starts a distinct same-uid process, pauses it immediately before the first sensitive write, and attempts the exact `/proc/<pid>/fd/<fd>` hard link from the peer. It requires `PermissionError`, verifies that no stolen link exists, then verifies correct final content. Independent execution on the exact fetched commit passed both parameter variants. The recorded mutation run removed the protection and both variants failed with `DID NOT RAISE PermissionError`, proving the test loses when the disclosure window is reopened.

Independent command and result:

```text
python -m pytest -q tests/test_pi_harness.py::test_same_uid_process_cannot_link_anonymous_inode_before_first_write --maxfail=1
2 passed in 0.17s
```

### AC3: configuration and mandatory audit unreadable throughout writes

Result: PASS.

Examined the complete write sequences in `SecureDir.write_exclusive` and `SecureDir.append_durable`, their callers in `pi.py` and `serve.py`, and these boundary tests:

* `test_same_uid_process_cannot_link_anonymous_inode_before_first_write`, for both data classes at the pre-first-write boundary.
* `test_models_bytes_are_unlinkable_at_former_verification_write_boundary`.
* `test_audit_hardlink_at_former_verify_write_boundary_gets_no_new_record`.
* `test_audit_hardlink_after_last_old_inode_check_gets_no_new_record`.

Independent execution of the latter three tests passed:

```text
3 passed in 0.35s
```

Before publication, peers cannot traverse the writer's procfs descriptors while dumpability is zero and there is no pathname to the anonymous inode. At publication, the bytes are complete, fsynced, mode `0600`, and validated. For subsequent audit records, copy-on-write means an outside link can observe only an immutable old inode and never the new mandatory bytes. These checks cover both data classes named by finding report sha256 `54079bfe9e04053c770786f88de7078b091ef99d0c84e7de365289cac548cdd2`.

### AC4: independent re-review uses repaired candidate

Result: PASS.

Examined card d13d6eb2 links, card c7d721da identity and PR 84, the current card description, and PR 75 history. The unrepaired candidate was not used. This first re-review fetched and tested PR 75 at repaired head `d30deb7d7685cad09a4c88efad09e3acdbef72f2`, which contains implementation commit `46c8cb67fa4f83e82f4b46d989b67e4b1662238f`. This report supplies the explicit independent verdict that the prior lifecycle record lacked. It does not infer resolution from lifecycle state and does not alter review card e87a34d6.

### AC5: no deployment or rollout

Result: PASS.

No deployment, rollout, restart, live gateway or configuration mutation, merge, automerge, provider call, or candidate repair occurred. PR 75 remains open. The only repository output from this card is this independent review report on a separate review branch and pull request, as required by the publication rail. No credential material was read, copied, or disclosed.

## Additional checks

GitHub reported these PR 75 checks as passing at review time:

* `docs / docs-check`
* `gitleaks`
* `GitGuardian Security Checks`

The candidate diff and implementation were read directly. Prior review text was used only to identify evidence to independently confirm, not as a substitute for fetching, code examination, or test execution.

## Explicit verdict

PASS

All five acceptance criteria of d13d6eb2 pass against exact fetched commit `d30deb7d7685cad09a4c88efad09e3acdbef72f2`. This is a review-only result. It authorizes no merge, deployment, rollout, or live mutation.
