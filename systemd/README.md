# skcode-hostd systemd deploy tooling

Tooling to run `skcode-hostd` as a managed **systemd user unit**. It ships safe:
**tailnet-only bind**, **capauth-gated on every data route**, **dispatch
allowlist empty (deny all)**, and it does **NOT auto-start**. Installing only
stages the unit; the operator decides when to enable and start it.

## What the unit does

`skcode-hostd.service` runs the remote-control daemon from
`src/skharness/serve.py`:

```
%h/.venvs/skops/bin/python -m skharness --host ${SKCODE_HOSTD_TAILSCALE_IP} --port 9394 --host-id ${SKCODE_HOSTD_HOST_ID}
```

It owns one harness (the claude-code tmux adapter) and serves the capauth-gated
read routes, the static client, **and a write surface**: `POST
/api/v1/sessions/{sid}/inject`, `/ratify`, `/deny`, `/cancel`, and `POST
/api/v1/dispatch`, which spawns a new agent session (remote code execution).
Earlier revisions of this file claimed there was no write surface; that was
stale. The authoritative route and scope table is [../SOP.md](../SOP.md) section 7.

## Three safety defaults (do not weaken)

1. **Tailnet-only bind.** `--host` is sourced from `${SKCODE_HOSTD_TAILSCALE_IP}`
   in the env file. `serve.py`'s `resolve_bind()` **refuses** a wildcard/public
   address (`0.0.0.0`, `::`, or blank), so a misconfigured or empty value fails
   the unit closed instead of exposing a public port. Always point it at a real
   Tailscale IP (e.g. `100.64.0.2`), never a public/wildcard value.

2. **Dispatch allowlist empty = deny all.** Neither the unit nor the env template
   sets `SKCODE_DISPATCH_REPOS`, so the spawn guard refuses every repo. Add roots
   only on a node that is meant to dispatch, and **never add `skos` or
   `skharness`**: an agent dispatched into either could edit the very code that
   grades it.

3. **Fail-closed auth on every gated route.** No token, an invalid or expired
   token, a wrong-audience token, or an unreachable capauth all deny. Since
   CR-3.2 `select_verifier()` runs the **real** capauth verifier by default and
   falls back to deny-all only when capauth cannot be imported. Writes need the
   `skcode.inject` scope and dispatch needs `skcode.dispatch`, each additionally
   decided by the capauth PDP at a `VERIFIED` enrollment floor.

### Disarming without stopping the unit

Set the escape hatch in your live env file and restart:

```
SKCODE_FORCE_DENY_ALL=1
```

Every caller is then denied and nothing actuates. `SKCODE_REAL_VERIFIER=1` is
the legacy explicit opt-in to the real verifier; it is redundant now that real
is the default, and it is harmless to leave set.

## Install

The installer copies the unit into `~/.config/systemd/user`, stages the env
template into `~/.config/skcode-hostd/`, runs `daemon-reload`, and prints the
next-step commands. It never enables or starts the service on its own.

```sh
# preview only (nothing written)
./systemd/install.sh --dry-run
./systemd/install.sh --diff

# install the unit + env template + daemon-reload (no enable, no start)
./systemd/install.sh
```

Then provision the env file (the unit fails closed until this exists):

```sh
mkdir -p ~/.config/skcode-hostd
cp ~/.config/skcode-hostd/skcode-hostd.env.example ~/.config/skcode-hostd/skcode-hostd.env
$EDITOR ~/.config/skcode-hostd/skcode-hostd.env
# set SKCODE_HOSTD_TAILSCALE_IP=<this node's tailnet IP>
# set SKCODE_HOSTD_HOST_ID=<node id, e.g. .158>
# leave SKCODE_DISPATCH_REPOS unset unless this node should dispatch (empty = deny all)
```

## Enable / start (operator decision)

```sh
systemctl --user enable skcode-hostd
systemctl --user start skcode-hostd

systemctl --user status skcode-hostd
journalctl --user -u skcode-hostd -f
```

The installer can also do this for you (still never auto-run without the flags):

```sh
./systemd/install.sh --enable          # enable only, no start
./systemd/install.sh --enable --start  # enable + start
```

Even with `--enable --start`, the tailnet-only bind and the empty dispatch
allowlist are unchanged: those flags only toggle systemd enablement, never the
daemon's security posture.

## Files

| File | Purpose |
|---|---|
| `skcode-hostd.service` | the user unit (tailnet-only, capauth-gated, `Restart=on-failure`, standard hardening) |
| `skcode-hostd.env.example` | tunables template (`SKCODE_HOSTD_TAILSCALE_IP`, `SKCODE_HOSTD_HOST_ID`, plus commented `SKCODE_DISPATCH_REPOS`, `SKCODE_FORCE_DENY_ALL`, `SKCODE_REAL_VERIFIER`) |
| `install.sh` | idempotent installer; `--dry-run` / `--diff` / `--enable` / `--start`; never auto-starts |
