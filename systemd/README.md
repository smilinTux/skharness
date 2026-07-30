# skcode-hostd systemd deploy tooling

Tooling to run `skcode-hostd` as a managed **systemd user unit**. It ships safe:
**tailnet-only bind**, **deny-all verifier by default**, and it does **NOT
auto-start**. Installing only stages the unit; the operator decides when to
enable and start it.

## What the unit does

`skcode-hostd.service` runs the P0 read-only remote-control daemon from
`src/skharness/serve.py`:

```
%h/.skenv/bin/python -m skharness --host ${SKCODE_HOSTD_TAILSCALE_IP} --port 9394 --host-id ${SKCODE_HOSTD_HOST_ID}
```

It owns one harness (the claude-code tmux adapter) and exposes exactly the three
capauth-gated read routes plus the static client. There is **no write surface**.

## Two safety defaults (do not weaken)

1. **Tailnet-only bind.** `--host` is sourced from `${SKCODE_HOSTD_TAILSCALE_IP}`
   in the env file. `serve.py`'s `resolve_bind()` **refuses** a wildcard/public
   address (`0.0.0.0`, `::`, or blank), so a misconfigured or empty value fails
   the unit closed instead of exposing a public port. Always point it at a real
   Tailscale IP (e.g. `100.86.156.5`), never a public/wildcard value.

2. **Deny-all verifier by default.** The unit deliberately does **not** set
   `SKCODE_REAL_VERIFIER`. `serve.py`'s `select_verifier()` then runs the P0
   deny-all placeholder, so no caller is accepted and the RCE surface stays
   gated. This is the intended P0 posture.

### Opting into the real capauth verifier

Only after the pairing/verifier is provisioned (spec 7.6), uncomment in your
live env file:

```
SKCODE_REAL_VERIFIER=1
```

Then `systemctl --user restart skcode-hostd`. Enabling it before the verifier is
provisioned would swap deny-all for a verifier with nothing to verify against.

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
# leave SKCODE_REAL_VERIFIER commented out unless the verifier is provisioned
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

Even with `--enable --start`, the tailnet-only bind and deny-all defaults are
unchanged: those flags only toggle systemd enablement, never the daemon's
security posture.

## Files

| File | Purpose |
|---|---|
| `skcode-hostd.service` | the user unit (tailnet-only, deny-all, `Restart=on-failure`, standard hardening) |
| `skcode-hostd.env.example` | tunables template (`SKCODE_HOSTD_TAILSCALE_IP`, `SKCODE_HOSTD_HOST_ID`, commented `SKCODE_REAL_VERIFIER`) |
| `install.sh` | idempotent installer; `--dry-run` / `--diff` / `--enable` / `--start`; never auto-starts |
