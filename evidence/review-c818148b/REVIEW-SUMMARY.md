# c818148b Adversarial Review Summary

## Claim Status
- **Card ID**: c818148b
- **Claimed by**: pi-glm-c818148b
- **Claim time**: 2026-08-27T21:27:52+00:00
- **Host**: chiap03

## Dependency Verification
- **Dependency**: 880f885e (SKHARNESS-PI-BOOTSTRAP-01R3 repair)
- **Status**: Complete - agent `pi-piharness-secure-repair-880f885e` has 880f885e in `completed_tasks`
- **Evidence located at**: ~/.skcapstone/evidence/work/880f885e/
- **All artifact hashes verified**: PASS

## Candidate Artifacts Verified
All five 880f885e successor artifacts were independently verified:

1. **880f885e-base.txt**: `c39327585822bc45c08ab5c92de0b0bf77ae9604d5d9b8470d78ec3077a49e1c`
2. **880f885e-candidate.patch**: `4098311c73b0bee201f591eb764a9799d57d81f7aae32f98c2d4ef3ccde9f438`
3. **880f885e-changed-files.sha256**: `381e80f0c30ccfc66e76f40cb292026ca8b69c238c4370a28b588b611ebe8f35`
4. **880f885e-candidate.tar.gz**: `ab86fe487172366653a56eeefc2508ad6448196821b229d7288d8c6a475a47b9`
5. **880f885e-repair-evidence.md**: `08ff74688ac26ae413cb91112adcbe9994321445cd1b86eab50052691c7a5f91`

## Worktree Verification
- **Base commit**: `598ea3911f696e4e07307b992091e8fcaf2c62e5`
- **Base tree**: `351b8f2cd688257cfe4b00bfb24106b18ebbc142`
- **All 16 file hashes verified**: OK
- **Clean patch application**: PASS

## Test Results
- **Focused changed-boundary suite**: 248 passed, 1 warning
- **Full repository suite**: 2143 passed, 17 failed (skos module missing, outside boundary)
- **Static checks**: All passed
- **Format checks**: All passed
- **Compilation**: PASS
- **Diff cleanliness**: PASS

## Independent Adversarial Probes
- **Probe source**: `c818148b-probes.py` (SHA256: `f521314e179edd119320ba70891ad1d66d1c70c76e903e4e9e10dca2cf2062f1`)
- **Probe output**: `c818148b-probes.out` (SHA256: `8fa279a410a60cdeee07e7deed5d7361543c506122e43caf4fd4fca50364057b`)

## Verdict: BLOCKED

### Blocked Criteria
- **AC:1 (Custody/hashes/clean application)**: PASS
- **AC:2 (Ancestor attacks)**: BLOCKED - concurrent hard-link race discovered
- **AC:3 (SID/PoolController collisions)**: BLOCKED - exact resource reporting defects
- **AC:4 (Failure truthfulness)**: BLOCKED - ambiguous SID targeting in archive/rollback
- **AC:5 (Explicit PASS/BLOCKED)**: BLOCKED

### Four Release-Blocking Defects Found

1. **Concurrent hard-link write/append race** in `SecureDir.create_file()`/`open_file()`
   - Hard link created after `_verify_file()` but before write/append receives controller state or mandatory audit bytes
   - Fails the requirement to prove no escaped or misowned write

2. **Post-receipt PoolController collision containment loses exact resource_id**
   - Unresolved containment converted to plain `SpawnRejected`
   - No typed teardown receipt with `resource_id` exposed to caller

3. **Daemon discards `SpawnOwnershipError.receipt` at API boundary**
   - Unresolved spawned resources become HTTP 400 with no resource_id or teardown state
   - Not truthful unresolved containment or actionable ownership reporting

4. **Normal archive and setup rollback use ambiguous SID instead of exact @N identity**
   - Cannot prove they acted on the owned resource rather than a same-name original/replacement
   - Fails requirement that original pane cannot be killed during collision/replacement

## Evidence Published

### Durable Evidence Files (all in ~/.skcapstone/evidence/work/c818148b/)
1. `c818148b-independent-rereview.md` - Full review report
   - SHA256: `1dd0a3b562b8711012a306a1c40e85b1c4a94ad4266e8cbe880d3e7cd7848329`

2. `c818148b-verdict.json` - Structured verdict with all hashes
   - SHA256: `f9ef95b048c8d3c441ab72156a06359d1d8ae1e601d441c97fdc58e1927d0a4d`

3. `c818148b-probes.py` - Independent adversarial probe source
   - SHA256: `f521314e179edd119320ba70891ad1d66d1c70c76e903e4e9e10dca2cf2062f1`

4. `c818148b-probes.out` - Probe execution results
   - SHA256: `8fa279a410a60cdeee07e7deed5d7361543c506122e43caf4fd4fca50364057b`

### Coordination Evidence Links (recorded via skcapstone coord link)
- `evidence_base_sha256` → `c39327585822bc45c08ab5c92de0b0bf77ae9604d5d9b8470d78ec3077a49e1c`
- `evidence_patch_sha256` → `4098311c73b0bee201f591eb764a9799d57d81f7aae32f98c2d4ef3ccde9f438`
- `evidence_manifest_sha256` → `381e80f0c30ccfc66e76f40cb292026ca8b69c238c4370a28b588b611ebe8f35`
- `evidence_archive_sha256` → `ab86fe487172366653a56eeefc2508ad6448196821b229d7288d8c6a475a47b9`
- `evidence_report_sha256` → `1dd0a3b562b8711012a306a1c40e85b1c4a94ad4266e8cbe880d3e7cd7848329`
- `evidence_verdict` → `/home/skuser01/.skcapstone/evidence/work/c818148b/c818148b-verdict.json`
- `blocked_on` → `{"type":"card","referent":"ac:2"}`

### CardStore Event
- Verdict event written to `~/.skcapstone/cards/c818148b/events/pi-glm-c818148b@chiap03.jsonl`
- Event ID: `54870ce1-8716-4c30-8cff-a29b3e598f8c`
- Transition ID: `c818148b-verdict-001`

## Notifications Sent
- **To jarvis**: skmail delivered to `~/.skcapstone/coordination/skmail.d/jarvis@chiap03.jsonl`
- **To lumina**: skmail delivered to `~/.skcapstone/coordination/skmail.d/lumina@chiap03.jsonl`

## What Was NOT Done (per constraints)
- No repository change (review only)
- No deployment, install, or live daemon contact
- No gateway or port-18790 access
- No credential disclosure or protected data access
- No commit, merge, or push to origin/main
- No cleanup, gc, or prune operations
- No completion event (BLOCKED verdict leaves card open)

## Rollback Information
No deployment occurred. If these bytes were deployed elsewhere despite this block:
1. Pause dispatch
2. Preserve audit/config/reservations/worktree/transcripts
3. Inventory tmux windows by exact @N identity before any action
4. Do not archive or kill by ambiguous SID
5. Restore the previously qualified build
6. Do not clean or prune evidence

## Required Repairs Before Next Rereview
See full report for detailed requirements. Summary:
1. Obtain and retain exact tmux window identity atomically at creation
2. Preserve `SpawnOwnershipError.receipt` through PoolController and daemon
3. Return typed containment receipt when post-receipt PoolController cleanup fails
4. Define and enforce concurrent same-uid/hard-link threat boundary
5. Add tests where duplicate tmux names/replacements exist
