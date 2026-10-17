# c818148b independent security rereview

## Verdict

**BLOCKED**

The exact frozen `880f885e` successor must not be deployed. The repair fixes the previously demonstrated ancestor-symlink/parent-swap escapes and makes many failure receipts truthful, but independent adversarial probes found release-blocking ownership and containment defects:

1. normal archive/capture/kill and setup rollback still address tmux resources by caller-visible SID (`session:sid`), not by the exact `@N` resource identity that spawn records;
2. a failed post-receipt PoolController collision teardown is raised as plain `SpawnRejected`, losing the resource ID and typed containment receipt;
3. `SpawnOwnershipError.receipt` is discarded by the daemon's generic `except SpawnRejected`, so an unresolved launched resource becomes HTTP 400 with no resource ID or teardown state;
4. the claimed hard-link protection has a check/use race: a concurrent hard link created after `_verify_file()` but before write/append receives controller state or mandatory audit bytes.

Review only was performed on host `chiap03`. I did not repair candidate source, deploy, install, contact a gateway or port 18790, access credentials/protected data, run a provider canary, merge, push, clean, gc, prune, or modify SKLegal. All tmux, process, HTTP-routing, and audit failure attacks used local fakes; filesystem attacks used temporary directories. Card status was not used to infer this verdict.

## Frozen candidate custody and closure

Working directory:

```text
/home/skuser01/.skcapstone/worktrees/c818148b
```

Before application:

```text
$ git rev-parse HEAD
598ea3911f696e4e07307b992091e8fcaf2c62e5
$ git show -s --format=%T HEAD
351b8f2cd688257cfe4b00bfb24106b18ebbc142
$ git status --short --branch
## HEAD (no branch)
```

This exactly matches `880f885e-base.txt`:

```text
base_commit 598ea3911f696e4e07307b992091e8fcaf2c62e5
base_tree 351b8f2cd688257cfe4b00bfb24106b18ebbc142
```

I independently recomputed all supplied artifact hashes:

```text
c39327585822bc45c08ab5c92de0b0bf77ae9604d5d9b8470d78ec3077a49e1c  880f885e-base.txt
4098311c73b0bee201f591eb764a9799d57d81f7aae32f98c2d4ef3ccde9f438  880f885e-candidate.patch
381e80f0c30ccfc66e76f40cb292026ca8b69c238c4370a28b588b611ebe8f35  880f885e-changed-files.sha256
ab86fe487172366653a56eeefc2508ad6448196821b229d7288d8c6a475a47b9  880f885e-candidate.tar.gz
08ff74688ac26ae413cb91112adcbe9994321445cd1b86eab50052691c7a5f91  880f885e-repair-evidence.md
```

All match the review brief.

I reconstructed the candidate in an isolated temporary Git index initialized with the exact base:

```text
GIT_INDEX_FILE=<temporary> git read-tree 598ea3911f696e4e07307b992091e8fcaf2c62e5
git apply --check --cached 880f885e-candidate.patch       # rc=0
git apply --cached 880f885e-candidate.patch               # rc=0
git diff --cached --check 598ea3911f696e4e07307b992091e8fcaf2c62e5  # rc=0
```

Resulting index status was exactly 15 modified files plus one added file:

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

An independent byte-closure script computed:

```text
base_tree 351b8f2cd688257cfe4b00bfb24106b18ebbc142
manifest_count 16 patch_count 16 archive_regular_count 16
path_sets_equal True
archive_nonregular_nondir []
errors []
```

For every manifest path, both the reconstructed index blob and archive member SHA256 matched the manifest. The archive contains only directories and those 16 regular files; it contains no symlink, hard-link, device, FIFO, or extra regular member. The archive entries have numeric uid/gid 0 and epoch mtime.

Only after these checks I ran:

```text
git apply --check 880f885e-candidate.patch  # rc=0
git apply --binary 880f885e-candidate.patch # rc=0
sha256sum -c 880f885e-changed-files.sha256
# all 16 paths: OK
git diff --check                           # rc=0
```

No other source patch was applied and no candidate source was edited.

## Coordination/CardStore provenance

I explicitly checked both projections. No matching `880f885e` event was found in the legacy `coordination/card_events` projection. The authoritative per-card CardStore at:

```text
/home/skuser01/.skcapstone/cards/880f885e/
```

contains the structural core and seven folded events: one claim, **five evidence link events**, and one structural complete event. The five link values exactly match the base, patch, manifest, archive, and report hashes above. This split is reported as provenance only; neither the predecessor's completion/status nor any other status was used to infer this review verdict.

## Independent adversarial probes

Evidence-only probe source and captured output:

```text
/home/skuser01/.skcapstone/evidence/work/c818148b/c818148b-probes.py
SHA256 f521314e179edd119320ba70891ad1d66d1c70c76e903e4e9e10dca2cf2062f1

/home/skuser01/.skcapstone/evidence/work/c818148b/c818148b-probes.out
SHA256 8fa279a410a60cdeee07e7deed5d7361543c506122e43caf4fd4fca50364057b
```

Executed with the required interpreter and local candidate source:

```text
PYTHONPATH="$PWD/src" /home/skuser01/.skenv/bin/python \
  /home/skuser01/.skcapstone/evidence/work/c818148b/c818148b-probes.py
```

### 1. Ancestor symlinks and parent replacement

The generic `SecureDir.anchor()` component walk rejected an ancestor symlink with `SecurePathError`/`ENOTDIR`. After anchoring a root descriptor, I renamed its lexical directory and replaced that lexical name with a symlink to an outside directory. A subsequent descriptor-relative exclusive write went to the displaced held inode, and no outside file appeared:

```text
ANCESTOR_SYMLINK_REJECTED SecurePathError 20
POST_ANCHOR_SWAP_HELD_INODE held-inode
POST_ANCHOR_SWAP_OUTSIDE_EXISTS False
```

The candidate's focused suite additionally exercises config, reservation, worktree, transcript, and audit ancestors; config parent swaps; traversal; final collisions; pre-existing state; real Git worktrees containing absolute and relative repository-controlled Pi config symlinks; and pre-existing hard-linked audit files. Those tests pass. This independently confirms the prior `afe22f6a` ancestor-symlink and final-component `O_NOFOLLOW` parent-swap escapes are repaired for the tested threat schedule.

### 2. Concurrent hard-link check/use race — BLOCKER

`SecureDir.create_file()`/`open_file()` calls `_verify_file()` once, then returns the writable descriptor. `write_exclusive()` and `append_durable()` write only after that return. A same-uid concurrent actor with access to the state directory can create a hard link in this gap. No second link-count check is made before/during write, and the link cannot be undone by later fsync.

I deterministically inserted the hard link immediately after `_verify_file()` accepted `st_nlink == 1`, before the first write. The outside alias received the Pi-like private configuration bytes:

```text
CONCURRENT_HARDLINK_WRITE_ESCAPED {"route":"private"}
CONCURRENT_HARDLINK_NLINK 2
```

The same schedule against an existing audit file caused the outside hard link to receive the mandatory second audit record:

```text
CONCURRENT_AUDIT_HARDLINK_ESCAPED 'first\nmandatory-second\n'
CONCURRENT_AUDIT_NLINK 2
```

This disproves the card's unqualified requirement to withstand hard links and concurrent replacement. Pre-existing hard links are rejected, but concurrent post-check links are not.

### 3. SID reservation and pre-launch collisions

Candidate tests force reservation collisions and prove retries occur before git/tmux launch. `PoolController` passes its owned SID set into `spawn_reserved`, so a conforming harness refuses an owned-SID collision before a second launch. The targeted tests pass. The earlier `afe22f6a` defect in which PoolController itself always launched before checking its dict is repaired.

### 4. Post-receipt PoolController collision containment — BLOCKER

I forced a contract-violating harness to return the existing SID after launch with exact new resource `@new`, then forced `teardown_owned('@new')` to fail. The controller correctly targeted `@new`, preserved the original `@original` member, and did not overwrite bookkeeping. However, it converted the unresolved containment into plain `SpawnRejected`:

```text
POOL_EXCEPTION_TYPE SpawnRejected
POOL_EXCEPTION_TEXT duplicate pool member session id 'sandbox-shared'; UNRESOLVED teardown: forced kill failure
POOL_EXCEPTION_HAS_RECEIPT False
POOL_EXCEPTION_DICT {}
POOL_TEARDOWN_TARGETS ['@new']
POOL_ORIGINAL_PRESERVED True @original
```

The exception exposes neither `resource_id='@new'` nor a typed teardown receipt. A caller cannot reliably identify or later contain the unresolved launched resource. This fails the acceptance criterion requiring unresolved new ownership to be explicitly reported. The harness contract already defines `SpawnOwnershipError(receipt=...)`; PoolController does not use it here.

### 5. Daemon discards an unresolved spawn containment receipt — BLOCKER

I made a local fake harness raise `SpawnOwnershipError` with `launched:true`, `resource_id:'@77'`, and `teardown_succeeded:false`. Because `SpawnOwnershipError` subclasses `SpawnRejected`, dispatch's generic handler reduced it to an ordinary HTTP 400 string:

```text
DAEMON_STATUS 400
DAEMON_BODY {"detail": "spawn rejected: tmux setup failed"}
DAEMON_EXPOSES_RESOURCE_ID False
DAEMON_EXPOSES_TEARDOWN_FAILURE False
DAEMON_SPAWN_CALLS 1
```

The rejection was audited, but the load-bearing containment receipt was lost at the authenticated API boundary. A resource may remain live and untracked while the controller receives only an input-like 400. This is neither truthful unresolved containment nor actionable ownership reporting.

### 6. Normal archive ignores recorded exact resource ownership — BLOCKER

A successful spawn records tmux `window_id` in `self._resource_ids[sid]` and returns it as `HarnessSession.resource_id`. I seeded `sid -> '@new'`, exposed a colliding live SID through the fake tmux listing, and archived. Both capture and kill addressed the ambiguous name `skchat-agents:sandbox-collision`, not `@new`:

```text
ARCHIVE_RECEIPT {"archived": true, ... "teardown_succeeded": true, ...}
ARCHIVE_TMUX_TARGETS [('capture', 'skchat-agents:sandbox-collision'),
                      ('kill', 'skchat-agents:sandbox-collision')]
ARCHIVE_USED_EXACT_RESOURCE False
```

The candidate stores `_resource_ids`, but normal archive does not consume it. Therefore it cannot prove it captured/killed the owned resource rather than a same-name original/replacement. The receipt can claim `archived:true` for the wrong window.

### 7. Setup rollback also uses ambiguous SID — BLOCKER

For failures after `new-window` but before the later `display-message` identity receipt, `_rollback_created_window()` kills `session:sid`. Forced failure produced:

```text
ROLLBACK_RECEIPT {"launched": true, "setup_succeeded": false,
                  "teardown_succeeded": false, ...}
ROLLBACK_TMUX_TARGETS ['skchat-agents:sandbox-shared']
ROLLBACK_TARGET_IS_AMBIGUOUS_SID True
```

The receipt truthfully says rollback failed, which is an improvement. But it has no exact resource ID, and its attempted kill may select an existing same-name window. A safe creation path needs to obtain the new `@N` identity atomically from `new-window` (for example using tmux's print/format receipt) and use that identity for every subsequent setup and rollback operation.

### 8. Tmux/liveness/transcript/audit truthfulness that did pass

The focused suite and source inspection force and check:

- `list-windows` failure distinct from no-live-session;
- capture failure, transcript persistence/final collision, teardown failure;
- `new-session`, `new-window`, option, pipe, and liveness failures;
- failed setup rollback kill represented in `SpawnOwnershipError.receipt` at the harness boundary;
- cancel/deny/inject signal, pane, respawn, and teardown failures;
- pre-intent dispatch/archive audit failures preventing actuation;
- outcome-audit failure returning HTTP 503 and never ordinary `archived:true`;
- transcript-first archive ordering.

These repaired behaviors pass, but they do not cure the receipt loss and SID/exact-resource blockers above.

### 9. Governance, attribution, pause, PDP, and host-local routing

Targeted tests prove:

- no token is 401 and insufficient bearer scope is 403;
- PDP deny is 403 and audited; missing PDP/audit is 501 fail-closed;
- dispatch pause is checked first and returns 503 with zero spawn;
- request `host` must equal the service-configured host and request `harness` must equal the daemon's configured harness;
- no request metadata becomes SSH/remote transport;
- repo allowlist remains fail-closed;
- Pi `models.json` uses the configured governed provider and includes literal validated `x-agent-id` and `x-session-id` attribution headers;
- archive is dispatch-scoped and PDP/audit gated.

No live daemon, gateway, network listener, tmux server, Pi binary, or provider was used.

## Qualification results

### Focused changed boundary

The requested `/home/skuser01/.skenv/bin/python` exists but contains no installed `pytest`; `/home/skuser01/.skenv/bin/pytest` and `/home/skuser01/.skenv/bin/ruff` are absent. I installed nothing. To execute with the requested interpreter, I used already-present read-only pytest/plugin paths from the prior rereview environment:

```text
PYTHONPATH="$PWD/src:/home/skuser01/.skcapstone/venvs/howtowinincourt/lib/python3.12/site-packages:/tmp/afe22f6a-pytest-plugins" \
/home/skuser01/.skenv/bin/python -m pytest -q \
  tests/test_claude_code_harness.py tests/test_pi_harness.py tests/test_pool.py \
  tests/test_dispatch.py tests/test_serve.py tests/test_daemon.py \
  tests/test_route_coverage.py

248 passed, 1 warning in 4.41s
```

A separately selected adversarial/governance subset produced:

```text
53 passed, 1 warning in 0.72s
```

Subset list SHA256: `aa3791b652b5d40e8b4e148874dc09375090a82aa835b3f82744acf82a493b21`.
Captured result SHA256: `1d8aca646b2a8ba4fc100f73470a0204a2bed4986c8b9e6f25b60d8403f7f8f0`.

### Full repository suite

```text
PYTHONPATH="$PWD/src:/home/skuser01/.skcapstone/venvs/howtowinincourt/lib/python3.12/site-packages:/tmp/afe22f6a-pytest-plugins" \
/home/skuser01/.skenv/bin/python -m pytest -q

17 failed, 2143 passed, 12 skipped, 48 warnings in 43.23s
```

Captured output SHA256: `623c82fdf1033c9dd8608d4a18351059f376ce86c38f3053cdd2754b3f61ed16`.

All 17 failures are `ModuleNotFoundError: No module named 'skos'` in unchanged optional-sibling autopilot tests. This is the same environment limitation reported by the predecessor and is not the basis of this BLOCKED verdict.

### Static/format/compile/diff

`/home/skuser01/.skenv/bin/ruff` is absent, as required to be reported. As supplemental evidence only, an already-present `/tmp/56fa5431/ruff/bin/ruff` (`ruff 0.15.4`, binary SHA256 `7dbef975fe238e0a716cd7dc4b4ddd86790c2e3eaf1cb749f95547096a89aff0`) ran without installation:

```text
ruff check <changed Python source/tests>
All checks passed!
ruff format --check <changed Python source/tests>
14 files already formatted
/home/skuser01/.skenv/bin/python -m compileall -q src tests
rc=0
git diff --check
rc=0
```

## Acceptance conclusion

- **Custody/base/archive/manifest/patch closure:** PASS.
- **Ancestor symlink and parent-swap repair:** PASS for independently tested schedules.
- **Pre-existing nodes/traversal/final collisions:** PASS in focused tests.
- **Hard links and concurrent replacement:** **BLOCKED** by post-check hard-link write/append race.
- **Pre-launch SID/PoolController collision:** PASS for conforming harness.
- **Post-receipt exact ownership and unresolved containment:** **BLOCKED** by untyped PoolController error and daemon receipt loss.
- **No original pane can be captured/killed during collision/replacement:** **BLOCKED** because archive and rollback use ambiguous SID targets instead of recorded `@N` identity.
- **Tmux/liveness/transcript/audit failure truthfulness:** substantial repairs pass, but end-to-end unresolved spawn receipt truthfulness remains **BLOCKED**.
- **Attribution/pause/PDP/authenticated host-local routing:** PASS in local fake qualification.

Overall: **BLOCKED**.

## Required repair before another rereview

1. Obtain and retain the exact tmux window identity atomically at creation; use that identity for set-option, pipe, liveness, rollback, capture, archive kill, cancel, deny, and any destructive respawn where ownership is required. Do not treat `session:sid` as exact ownership.
2. Preserve `SpawnOwnershipError.receipt` through PoolController and the daemon/API. An unresolved launched resource must expose at least exact `resource_id`, `launched/tracked`, setup state, teardown state, and teardown reason; it must not become an ordinary input-like 400.
3. Return a typed containment receipt when post-receipt PoolController cleanup fails, including `@new`; preserve the original member and never address it by shared SID.
4. Define and enforce the concurrent same-uid/hard-link threat boundary. If concurrent writers are in scope as the card states, remove the check/use gap or redesign state ownership/isolation so an attacker cannot link a writable inode between validation and write/append. Add deterministic config/transcript/capture/audit regression tests.
5. Add tests where duplicate tmux names/replacements exist and assert every load-bearing command targets the exact owned `@N`, not merely that a kill/capture command occurred.

## Limitations

- No real tmux/Pi/provider/network operation was authorized or performed; command failures and identity collisions were exercised with typed local fakes.
- Foreign-uid ownership could not be created without privilege and was inspected/tested through existing ownership guards; the reproduced hard-link race uses the relevant same-uid concurrent actor.
- Linux `/proc/<pid>/fd` behavior is assumed by the candidate and was available on `chiap03`.
- The optional `skos` sibling is absent, preventing a wholly green broad suite. No package was installed and no unrelated source was changed.
- The review worktree intentionally remains at exact base plus exact candidate patch. Evidence-only probes are outside the repository worktree.

## Rollback and card handling

No deployment occurred. Review rollback is to discard this review worktree and return to exact base `598ea3911f696e4e07307b992091e8fcaf2c62e5`, or reverse-apply the exact candidate patch in a disposable review checkout.

If these bytes were deployed elsewhere despite this block: pause dispatch; preserve audit/config/reservation/worktree/transcript state and every partial receipt; inventory tmux windows by exact `@N` identity before any action; do not archive or kill by ambiguous SID; then restore the previously qualified build. Do not clean or prune evidence.

Because the verdict is **BLOCKED**, card `c818148b` must remain open. Evidence links are recorded without a completion event.
