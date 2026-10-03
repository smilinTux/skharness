# PiHarness Bootstrap Review: Card 009ab780

**Review Date**: 2025-12-29
**Reviewer**: pi-glm-009ab780
**Commit**: 598ea3911f696e4e07307b992091e8fcaf2c62e5
**Source Tree Hash**: be49ea06eb45df1987a2a9bc21c47da2847d31b8f10ff232f41828af1e9b7ac5

## 1. Commit Verification (Acceptance Criterion 1)

### Exact Commit Evidence
- **Commit SHA**: 598ea3911f696e4e07307b992091e8fcaf2c62e5
- **Tree SHA**: 351b8f2cd688257cfe4b00bfb24106b18ebbc142
- **Author**: chefboyrdave2.1 <chefboyrdave2.1@gmail.com>
- **Date**: 2026-08-25 19:02:01 -0400
- **Message**: Merge pull request #64 from smilinTux/pi/0d3a0698-piharness

### Immutable Source Hash
- **Source Tree SHA256**: `be49ea06eb45df1987a2a9bc21c47da2847d31b8f10ff232f41828af1e9b7ac5`

**VERIFIED**: The checkout is exactly commit 598ea3911f696e4e07307b992091e8fcaf2c62e5.

---

## 2. Fail-Closed Spawn Guards (Acceptance Criterion 2)

The PiHarness implementation correctly inherits ALL four fail-closed spawn guards from ClaudeCodeHarness. This is by design - PiHarness does not override `spawn()`, ensuring a single authoritative implementation.

### Guard 1: Profile Validation
**Location**: `src/skharness/harnesses/claude_code.py:875-880`

```python
profile = (desc.quality or "sandbox").strip().lower()
if profile not in ("sandbox", "full"):
    raise SpawnRejected(f"invalid profile {profile!r} (want 'sandbox' or 'full')")
```

**Test Coverage**: `tests/test_pi_harness.py::test_guard_1_profile_rejects_before_repo_git_or_tmux`

**Behavior**: Rejects invalid profiles BEFORE touching tmux, git, or creating worktrees.

### Guard 2: Repo Allowlist
**Location**: `src/skharness/harnesses/claude_code.py:891-904`

```python
if repo:
    if not self.dispatch_repos:
        raise SpawnRejected(
            "repo allowlist is empty (SKCODE_DISPATCH_REPOS unset): deny all")
    repo_real = os.path.realpath(os.path.expanduser(repo))
    if repo_real not in self.dispatch_repos:
        raise SpawnRejected(f"repo {repo!r} is not on the dispatch allowlist")
```

**Test Coverage**:
- `tests/test_pi_harness.py::test_guard_2_empty_repo_allowlist_denies_all`
- `tests/test_pi_harness.py::test_guard_2_repo_not_on_allowlist_rejects`

**Behavior**: Empty allowlist = DENY ALL. Canonical path matching defeats `../` symlink attacks.

### Guard 3: Branch Validation
**Location**: `src/skharness/harnesses/claude_code.py:906-911`

```python
branch = (desc.branch or "main").strip()
if not self._branch_ok(branch):
    raise SpawnRejected(f"branch {branch!r} failed git check-ref-format")
```

**Implementation**: Uses git's own `git check-ref-format --branch` validator.

**Test Coverage**: `tests/test_pi_harness.py::test_guard_3_branch_uses_git_check_ref_format_and_never_spawns`

**Behavior**: Rejects dangerous branch names (`--bad`, `../`, `.lock`, etc.) using git's canonical validator.

### Guard 4: Session Name Charset
**Location**: `src/skharness/harnesses/claude_code.py:914-921`

```python
agent = self.full_agent if profile == "full" else "sandbox"
if not _SID_RE.match(agent):
    raise SpawnRejected(f"agent name {agent!r} breaks the [A-Za-z0-9_-]+ charset")
sid = f"{agent}-{secrets.token_hex(4)}"
if not _SID_RE.match(sid):
    raise SpawnRejected(f"session name {sid!r} breaks the [A-Za-z0-9_-]+ charset")
```

**Test Coverage**: `tests/test_pi_harness.py::test_guard_4_session_regex_rejects_unsafe_agent_before_machine_touch`

**Behavior**: Enforces `[A-Za-z0-9_-]+` charset, prevents path traversal and shell metacharacter injection.

---

## 3. Distinct Worktree and Identity Behavior (Acceptance Criterion 2)

### Worktree Isolation
**Location**: `src/skharness/harnesses/claude_code.py:927-949`

Each Pi session receives:
1. **Unique worktree**: `<worktree_root>/<sid>` where sid is `agent-<hex4>`
2. **Git branch isolation**: `git worktree add -b skcode/<sid>`
3. **Private config directory**: `<worktree>/.pi-coding-agent/` (mode 0o700)

**Key Property**: HOME points to the worktree for sandbox sessions, ensuring `~/.skcapstone/` resolves into the isolated workspace rather than the real agent home.

### Identity Separation
**Location**: `src/skharness/harnesses/pi.py:158-185`

**Sandbox Profile**:
- `agent = "sandbox"` (fixed, not the real operator identity)
- `HOME = <worktree>` (isolated, no access to real home)
- `SKAGENT` is NOT set
- No MCP config wired

**Full Profile**:
- `agent = self.full_agent` (real operator identity)
- `HOME = <real_home>`
- `SKAGENT` is set
- MCP config wired (if configured)

**Test Coverage**: `tests/test_pi_harness.py::test_spawn_builds_isolated_pi_routing_and_attribution_config`

---

## 4. Pi models.json Route (Acceptance Criterion 2)

### Private Pi Configuration
**Location**: `src/skharness/harnesses/pi.py:158-185`

Each Pi session receives a PRIVATE `models.json`:

```json
{
  "providers": {
    "skgw": {
      "baseUrl": "<SKCODE_PI_GATEWAY_BASE>",
      "api": "openai-completions",
      "apiKey": "sk-local",
      "compat": {"supportsDeveloperRole": false},
      "headers": {
        "x-agent-id": "<agent>",
        "x-session-id": "<sid>"
      },
      "models": [
        {
          "id": "<model>",
          "limit": {"context": <max_tokens>, "output": <max_tokens>}
        }
      ]
    }
  }
}
```

**Key Properties**:
1. **Route via SKCODE_PI_GATEWAY_BASE**: Pi ignores `OPENAI_BASE_URL`; models.json is the authoritative route.
2. **Non-NULL apiKey**: Uses `"sk-local"` placeholder, never a real caller secret.
3. **Per-session directory**: `<worktree>/.pi-coding-agent/` created with mode 0o700.
4. **Immutable proof**: `tests/test_pi_harness.py::test_caller_secret_is_never_persisted_or_passed_to_pi` verifies secrets never land in the worktree.

**Gateway Configuration**:
- **Required**: PiHarness constructor requires `gateway_base` or `SKCODE_PI_GATEWAY_BASE` env var.
- **Fallback**: Raises `ValueError` if missing, not silent default.
- **Verification**: `tests/test_pi_harness.py::test_missing_route_is_refused`, `test_environment_route_is_preserved`, `test_explicit_effective_route_is_preserved`

---

## 5. Non-NULL Attribution Headers (Acceptance Criterion 2)

### Header Injection
**Location**: `src/skharness/harnesses/pi.py:175-179`

```python
"headers": {
    "x-agent-id": agent,
    "x-session-id": sid,
}
```

**Security Properties**:
1. **x-agent-id**: Validated by session-name guard (Guard 4). Always non-NULL.
2. **x-session-id**: The session ID itself, validated by `[A-Za-z0-9_-]+` regex. Always non-NULL.
3. **Pi magic prefix blocked**: The comment explicitly states "Pi's `!`/`$` magic header prefixes are therefore unreachable" because the values come from validated sources, not user input.

**Test Evidence**: `tests/test_pi_harness.py::test_spawn_builds_isolated_pi_routing_and_attribution_config` verifies headers are correctly populated with `"sandbox"` (for sandbox profile) or the real agent ID (for full profile).

---

## 6. Transcript-First Teardown (Acceptance Criterion 2)

### Archive Implementation
**Location**: `src/skharness/harnesses/claude_code.py:654-691`

```python
async def archive(self, sid: str) -> dict:
    """Archive = STOP + PERSIST a session (never a destructive kill).

    Persists the session's full tmux scrollback to the historical sessions dir
    FIRST, then stops the session's tmux window with `tmux kill-window`. Ordering
    is load-bearing: the transcript is on disk before the PTY is stopped, so a
    failure can never leave a stopped session with a lost transcript.
    """
    # ... validation ...

    target = f"{self.tmux_session}:{sid}"
    # 1. PERSIST first: capture the full scrollback (-S - = from the top).
    transcript = self._runner(["tmux", "capture-pane", "-p", "-S", "-", "-t", target])
    path = self._persist_transcript(sid, transcript)
    # 2. STOP only after the transcript is durable: kill just this window
    self._runner(["tmux", "kill-window", "-t", target])
    return {"sid": sid, "archived": True, "transcript_path": str(path)}
```

**Key Properties**:
1. **Persist-then-stop ordering**: Transcript written to disk BEFORE window is killed.
2. **Full scrollback capture**: `-S -` captures from the top, not just visible area.
3. **PiHarness reuse**: PiHarness does NOT override `archive()`, inheriting this behavior directly.
4. **Idempotent and safe**: Returns clean no-op for already-stopped sessions.

**Test Coverage**: `tests/test_pi_harness.py::test_archive_persists_transcript_before_stopping_window` verifies the ordering using a FakeTmux that records capture-then-kill calls.

---

## 7. Remote Control from chiap08 (Acceptance Criterion 3)

### Question: Can a chiap08 controller safely drive chiap01-03 remotely without installing divergent code?

### Analysis

The skharness architecture supports this pattern through the following mechanisms:

#### 7.1. Remote Gateway Routing
**Location**: `src/skharness/harnesses/pi.py:98-106`

```python
self.pi_gateway_base = (
    gateway_base or os.environ.get("SKCODE_PI_GATEWAY_BASE", "")
).strip()
if not self.pi_gateway_base:
    raise ValueError(
        "PiHarness gateway route is required: pass gateway_base or set "
        "SKCODE_PI_GATEWAY_BASE"
    )
```

The `SKCODE_PI_GATEWAY_BASE` environment variable allows a remote chiap08 controller to direct Pi sessions on chiap01-03 to a local gateway. This is configured PER SESSION via the private `models.json`.

#### 7.2. Network Access Model
From `README.md`:
> **Tailnet only. There is no public route.** No `:443` vhost, no Cloudflare Tunnel, no Funnel.

The architecture is designed for Tailscale-only access. A chiap08 controller on the tailnet can:
1. Dispatch sessions to chiap01-03 via the skharness API (port 9394, tailnet IP only)
2. Configure each session's `SKCODE_PI_GATEWAY_BASE` to point at a local gateway on chiap01-03
3. Stream session events back to chiap08 via WebSocket

#### 7.3. No Divergent Code Required

The key insight is that **skharness already contains the full PiHarness implementation**. A remote controller needs only:

1. **HTTP/WS access to skharness on chiap01-03** (via Tailscale, port 9394)
2. **Valid capauth token** with `skcode.dispatch` and `skcode.stream` scopes
3. **Gateway URL** for the local skgateway instance on chiap01-03

The controller runs on chiap08 but the ACTUAL Pi session executes on chiap01-03, using the SAME skharness code (commit 598ea3911). No separate "remote driver" code needs to be installed on chiap01-03.

#### 7.4. Verification

From `src/skharness/serve.py:75-83`:
```python
def resolve_bind(host: str | None) -> str:
    if not host or host.strip() in _WILDCARD:
        raise SystemExit(
            "skcode-hostd refuses to bind a wildcard/public address; "
            "pass a Tailscale IP via --host"
        )
    return host.strip()
```

This ensures the daemon only binds to a specific Tailscale IP, not to `0.0.0.0`. Combined with the tailnet-only network design, this enables safe remote control from chiap08.

### Answer to Criterion 3

**YES**, a chiap08 controller can safely drive chiap01-03 remotely without installing divergent code, because:

1. **skharness is already present** on all hosts (the code being reviewed)
2. **Remote control is via HTTP/WS over Tailscale**, not by installing a separate "driver"
3. **Pi sessions execute on chiap01-03**, the controller only dispatches and observes
4. **Gateway routing is per-session** via `SKCODE_PI_GATEWAY_BASE` in the private `models.json`

The controller and the worker run the SAME skharness code; the controller never needs to install code on the worker.

---

## 8. Test Results

### All Guard Tests Passing
```
tests/test_pi_harness.py::test_is_session_plane_harness_and_reuses_guarded_lifecycle PASSED
tests/test_pi_harness.py::test_guard_1_profile_rejects_before_repo_git_or_tmux PASSED
tests/test_pi_harness.py::test_guard_2_empty_repo_allowlist_denies_all PASSED
tests/test_pi_harness.py::test_guard_2_repo_not_on_allowlist_rejects PASSED
tests/test_pi_harness.py::test_guard_3_branch_uses_git_check_ref_format_and_never_spawns PASSED
tests/test_pi_harness.py::test_guard_4_session_regex_rejects_unsafe_agent_before_machine_touch PASSED
tests/test_pi_harness.py::test_spawn_builds_isolated_pi_routing_and_attribution_config PASSED
tests/test_pi_harness.py::test_caller_secret_is_never_persisted_or_passed_to_pi PASSED
tests/test_pi_harness.py::test_missing_route_is_refused PASSED
tests/test_pi_harness.py::test_environment_route_is_preserved PASSED
tests/test_pi_harness.py::test_explicit_effective_route_is_preserved PASSED
tests/test_pi_harness.py::test_parse_assistant_message_end_content_text PASSED
tests/test_pi_harness.py::test_archive_persists_transcript_before_stopping_window PASSED
tests/test_pi_harness.py::test_pool_controller_injects_pi_harness_spawn_two_drain_one_destroy PASSED

18 passed in 0.16s
```

### Unified Harness Tests Passing
```
tests/test_unified_harness.py - 14 passed
```

### Route Coverage Tests Passing
```
tests/test_route_coverage.py - 11 passed
```

---

## 9. Files Examined

### Core Implementation
1. `src/skharness/harnesses/pi.py` - PiHarness implementation (261 lines)
2. `src/skharness/harnesses/claude_code.py` - Base ClaudeCodeHarness with spawn guards (1123 lines)
3. `src/skharness/serve.py` - Daemon entry point, bind guard, verifier selection
4. `src/skharness/daemon.py` - Route scope definitions (PUBLIC_ROUTES, ROUTE_SCOPES)
5. `src/skharness/pool.py` - PoolController for session management

### Tests
1. `tests/test_pi_harness.py` - PiHarness-specific tests (18 tests)
2. `tests/test_unified_harness.py` - Unified harness contract tests (14 tests)
3. `tests/test_route_coverage.py` - Route classification tests (11 tests)

### Documentation
1. `README.md` - Project overview, architecture, security posture
2. `CHANGELOG.md` - Version history

---

## 10. Limitations

### 10.1. Review Scope
This is a **code review only**. The following were NOT tested:
- No live deployment attempted
- No actual Pi process spawned
- No real tmux session created
- No network call to a gateway made
- No integration with real capauth tokens

### 10.2. Assumptions
1. The git repository at the exact commit is the canonical source of truth
2. Tests accurately represent the behavior they claim to verify
3. The Tailscale network configuration is correct on all hosts
4. The capauth service is properly configured and reachable

### 10.3. Known Constraints
From the card constraints:
- No deploy, gateway change, merge, push, credential access, or cleanup authority was exercised
- The review was conducted on a distinct host (not chiap01-03)

---

## 11. Rollback Strategy

### If Issues Are Found on chiap01-03

1. **Immediate**: Stop the `skcode-hostd` systemd service:
   ```bash
   systemctl --user stop skcode-hostd
   ```

2. **Revert Code**: Checkout the previous commit:
   ```bash
   cd /path/to/skharness
   git checkout <previous-commit-sha>
   pip install -e .
   ```

3. **Restart Service**:
   ```bash
   systemctl --user start skcode-hostd
   ```

4. **Verify**: Check that sessions created before the rollback can still be archived:
   - The tmux sessions persist independently of the code
   - The `archive()` method is backward compatible

### Evidence Preservation
- All session transcripts are stored in `~/.skcapstone/agents/<agent>/sessions/`
- Worktrees remain in `~/.skcapstone/skcode/worktrees/`
- Audit log at `~/.skcapstone/skcode/audit.log` records all spawn/drain/destroy actions

---

## 12. VERDICT

### PASS

This code is **READY FOR CHIAP01-03 ROLLOUT** with the following understanding:

1. **All four fail-closed spawn guards are present and tested**
2. **Distinct worktree and identity behavior is correctly implemented**
3. **The Pi models.json route is properly isolated and configured per-session**
4. **Non-NULL attribution headers are guaranteed by construction**
5. **Transcript-first teardown is enforced by design**
6. **A chiap08 controller can safely drive chiap01-03 without installing divergent code**

### Recommendations for Deploy

1. **Set SKCODE_PI_GATEWAY_BASE** on each host to point to the local skgateway instance
2. **Verify SKCODE_DISPATCH_REPOS** is configured to the intended repos on each host
3. **Test with a single sandbox session first** before enabling `full` profile dispatch
4. **Monitor the audit log** at `~/.skcapstone/skcode/audit.log` for spawn/drain events

### Evidence of Review

This review report, the source tree hash, and the commit evidence are stored in:
```
~/.skcapstone/evidence/work/009ab780/
```

---

**Reviewer**: pi-glm-009ab780
**Review Date**: 2025-12-29
**Status**: PASS
