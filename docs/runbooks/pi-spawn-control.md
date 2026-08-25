# Pi spawn pause and drain contract

All supported Pi worker creation uses one state file and one final launch
boundary. Direct `pi`, `tmux`, or temporary launcher execution is unsupported.

## Supported entrypoints

- SKHarness autocode and arena Pi runs pass through `Sandbox.spawn`, which
  reserves the worker before any Docker network, proxy, or worker process is
  created.
- `scripts/pi-cockpit.sh` runs every tmux creation and every worker command
  through `skharness-pi-launch`.
- Rolling pool controllers, wave launchers, repair launchers, and retry
  launchers must invoke `skharness-pi-launch`. Temporary scripts do not receive
  an exception.
- Qualification scripts that directly create provider test containers are not
  worker launchers. Their separately approved qualification gates still apply.

The state path is deployment configuration. It must not contain a host alias,
operator name, credential, provider route, or temporary script path. Launchers
receive it through `SKHARNESS_PI_SPAWN_STATE` and receive their attributable
identity through `SKHARNESS_PI_SPAWN_ACTOR`.

## Bootstrap

Create the state once before enabling the guarded candidate:

```bash
skharness-pi-spawn-control --state "$SKHARNESS_PI_SPAWN_STATE" \
  bootstrap --actor install-controller
```

Bootstrap is atomic and idempotent. A missing, unreadable, malformed, or
unsupported state denies new Pi creation. There is no implicit open fallback.

## Reserve a window

Pause rejects new workers immediately. Drain also reports the exact registered
workers that are allowed to finish naturally.

```bash
skharness-pi-spawn-control --state "$SKHARNESS_PI_SPAWN_STATE" drain \
  --owner window-controller --reason canary-window --scope pi:all \
  --ttl-seconds 2100 --expected-fence 0

skharness-pi-spawn-control --state "$SKHARNESS_PI_SPAWN_STATE" status
```

The status is quiescent only when its sorted `workers` list is empty. An expired
pause remains fail closed. Expiry never silently grants spawn authority.

## Renew and resume

Only the exact owner and current fence can renew or resume. Repeating the exact
same mutation is idempotent. A changed request with a stale fence is rejected.

```bash
skharness-pi-spawn-control --state "$SKHARNESS_PI_SPAWN_STATE" renew \
  --owner window-controller --fence 1 --ttl-seconds 2100

skharness-pi-spawn-control --state "$SKHARNESS_PI_SPAWN_STATE" resume \
  --owner window-controller --fence 2
```

Resume is the operational rollback. It changes only the spawn decision. It
does not send input or signals, terminate a process, kill a pane, mutate a card,
or rewrite worker settings. Source rollback selects the exact parent commit or
reverts only the candidate commit. The unused state file can remain as inert
evidence after the parent is restored.

## Worker launcher

```bash
skharness-pi-launch --state "$SKHARNESS_PI_SPAWN_STATE" \
  --worker-id card-1234-attempt-1 --actor rolling-pool --scope pi:all \
  --kind process -- pi --model approved-model
```

The launcher atomically reserves an exact worker identity before calling the
command and removes only that reservation after the command exits. Pause and
drain never alter existing worker processes or tmux panes.
