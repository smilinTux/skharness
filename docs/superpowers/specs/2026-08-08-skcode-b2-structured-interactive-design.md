# Design: skcode B2 — structured interactive sessions (via `--resume`)

Date: 2026-08-08
Author: Lumina (Opus 4.8 harness session)
Repo: `~/clawd/skcapstone-repos/skharness` (branch `master`)
Status: approved decisions, pre-implementation

## Context

skcode has two session modes on the `ClaudeCodeHarness`:

- **direct** (one-shot): `claude -p --dangerously-skip-permissions --output-format
  stream-json --verbose --model <m> <prompt>`. Task 3-B1 (shipped, `b683e35`) made
  these emit STRUCTURED events: `tmux pipe-pane` copies the JSONL to
  `<worktree>/.skcode/stream.jsonl`; `stream()` detects the `.skcode` dir and tails
  it through `parse_stream_json_line` into typed SessionEvents.
- **interactive** (stays open for follow-ups): today it launches the `claude` TUI
  (no `-p`, no bypass, a seeded `~/.claude.json` skips onboarding), and `inject()`
  sends raw text via `tmux send-keys`. Output is SCREEN-SCRAPED from the TUI via
  `capture-pane` + the client's `isChromeLine` chrome filter.

B2's goal: give interactive sessions the SAME structured output + real tool
rendering as direct, instead of screen-scraping the TUI.

## Why the obvious path does not work (verified 2026-08-08)

Structured output requires headless stream-json mode, which has NO TUI. Two facts
were established empirically before this design:

1. `claude -p --input-format stream-json --output-format stream-json --verbose`
   stays alive across turns when fed newline-delimited JSON user frames
   (`{"type":"user","message":{"role":"user","content":"..."}}`) on **piped**
   stdin, answering each in turn. So a persistent headless process is possible.
2. BUT the same command with a **tty** stdin (a tmux pane's pty) exits immediately
   with `Error: Input must be provided either through stdin or as a prompt argument
   when using --print`. So `tmux send-keys` into the pane CANNOT drive it; headless
   mode needs a non-tty stdin (a pipe/FIFO).

Therefore B2 cannot keep the send-keys inject mechanism. Two viable mechanisms
were considered.

## Approaches considered

**Option A — persistent process + FIFO.** One long-lived headless `claude
--input-format stream-json` per session, stdin from a FIFO opened O_RDWR (via a
fixed, injection-safe `sh -c 'exec 3<>"$F"; exec "$@" <&3'` shim so it never sees
EOF). `inject()` writes a JSON user frame to the FIFO. Warm process (fast
follow-ups). Cost: real stdin plumbing, a shell shim added to the RCE surface, a
long-lived process to babysit.

**Option B — stateless `--resume` turns (CHOSEN).** Each turn is a one-shot
`claude -p --resume <session_id> --output-format stream-json`, reusing the direct
path plus `--resume`. Verified 2026-08-08: turn 1 emits a `session_id` in its
`system/init` event; a second one-shot with `--resume <session_id>` in the same
`HOME`=worktree recalls prior context (asked it to remember 42, resume answered
42), cloud-free via the gateway, exit 0. No FIFO, no stdin shim, no long-lived
process; reuses B1 wholesale. Cost: each follow-up re-inits `claude` (~2s context
reload), fine for phone-driven steering.

Decision: **Option B.** Far less plumbing, nothing new on the RCE surface, cleaner
failure model, maximal reuse of the tested direct path.

## Design (Option B)

Both modes become headless stream-json. The mode difference collapses to: **direct
is one-shot; interactive is resumable via inject.**

### Turn 1 (spawn)
Identical to a direct spawn (headless stream-json, `--dangerously-skip-permissions`,
pipe-pane → `.skcode/stream.jsonl`). The only mode-specific behavior is that an
interactive session is recorded as resumable (already tracked: `_spawned_sids`).

### session_id
Not stored as new harness state. `inject()` reads it from the FIRST `system/init`
line of `<worktree>/.skcode/stream.jsonl` (always present, written by turn 1). This
survives a daemon restart (the file persists) and needs no spawn-time bookkeeping.

### inject (follow-up)
`inject(sid, text)` keeps its guards UNCHANGED: sid charset, CR-6.2 C2
`_inject_target_allowed` (daemon-spawned sids only), live-window check. The delivery
changes:

1. Read `session_id` from the session's `stream.jsonl` init event. If absent (no
   completed turn 1 yet), return a clean `injected: False` no-op (never touch tmux).
2. `tmux respawn-pane -k -t <window> -- env -i <same env> claude -p --resume
   <session_id> --output-format stream-json --verbose --dangerously-skip-permissions
   --model <m> <text>`. Argv-only, prompt as a distinct DATA element (never shell).
3. Re-attach `tmux pipe-pane -o -t <window> "cat >> '<stream.jsonl>'"` so the new
   turn's events APPEND to the same log (respawn replaces the process; the pane and
   its pipe target persist, but pipe-pane is re-issued to be safe/idempotent).

`stream()` is unchanged: it is already tailing `stream.jsonl` by byte offset, so
appended turns flow through as new typed events with no client change.

### Permission posture (approved)
Headless mode has no answerable TUI permission prompt, so interactive uses
`--dangerously-skip-permissions`, same as direct. This is consistent with direct and
stays inside CR-6.2 containment (sandbox: no creds, no real HOME; full: trusted
operator identity). It trades away interactive's former per-action gate; accepted.
inject remains scoped to daemon-spawned sids (CR-6.2 C2).

### Removed (now-dead interactive TUI path)
- The `mode == "interactive"` TUI branch in `_claude_argv` (the no-`-p` launch).
- `_write_interactive_seed` and its `~/.claude.json` seeding (headless `-p` skips
  onboarding; a direct session already runs with a fresh HOME and no seed).
- The raw-text `tmux send-keys` body of `inject` (replaced by respawn `--resume`).

### Unchanged / reused as-is
`_build_env` (incl. gateway wiring + `CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT`),
`pipe-pane` capture, the `.skcode` dir structured-vs-scrape signal,
`parse_stream_json_line`, `_stream_structured`, and the client's per-type rendering
(`renderStructured`). Direct mode is untouched.

## Error handling
- inject before turn 1 completes (no `session_id` yet) → clean no-op, no tmux.
- A dead/absent window → existing no-op path.
- `--resume` on an unknown/expired session id → claude errors into the JSONL as a
  `result` with `is_error`; `stream()` surfaces it as a `turn failed` STATUS. No
  crash, fail soft (same discipline as the parser).
- pipe-pane re-attach is idempotent (`-o` overwrites the pane's pipe).

## Testing (all against the fake tmux runner + tmp files, no real tmux/claude)
1. `_claude_argv` no longer has an interactive TUI branch: interactive turn-1 argv
   == direct argv (headless stream-json flags present, `-p`, bypass).
2. spawn(interactive) creates `.skcode`, attaches pipe-pane, writes NO `.claude.json`
   seed.
3. A helper reads `session_id` from a `stream.jsonl` init line (unit test on the
   reader).
4. inject(interactive) with a session_id present → issues `respawn-pane` carrying
   `--resume <id>` and the message as a distinct argv element, then re-attaches
   pipe-pane. Prompt is never shell-interpolated.
5. inject before any init line → clean `injected: False` no-op, never touches tmux.
6. inject still refuses a non-daemon-spawned sid (CR-6.2 C2 regression) and an
   invalid sid.
7. `stream()` structured path already covered by B1 tests (append-tail) — add a case
   that a second appended turn's events are yielded.
8. Full suite green; the pre-existing SIEM/autopilot notes do not apply here.

## Deploy
`systemctl --user restart skcode-hostd.service` (editable install). Smoke: dispatch
an interactive gateway-model session, confirm turn 1 streams structured events, then
inject a follow-up and confirm the resumed turn appends structured events (incl. a
tool_use round-trip now that the gateway tool-call fix `9e23c62` is live).

## Out of scope
- Anthropic-frontend Option A hardening (separate).
- Any change to direct mode.
- Restoring a headless per-action permission gate (`--permission-prompt-tool`);
  explicitly declined in favor of matching direct.
