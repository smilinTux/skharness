"""skcode-hostd runner. Binds a Tailscale IP ONLY (never 0.0.0.0), port 9394.

Port 9390 is owned by the skcomms broker_server (its honest, documented default).
skcode-hostd therefore takes 9394 as its ratified default (SKWorld platform spec
R0.4) so the two never collide on a shared host. Pass --port to override.
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

from skharness.auth import Verifier
from skharness.daemon import build_daemon_app
from skharness.harnesses.claude_code import ClaudeCodeHarness

DEFAULT_PORT = 9394

_WILDCARD = {"0.0.0.0", "::"}

# The audience a wire token must be scoped to for skcode-hostd (spec R4.2).
SKCODE_AUDIENCE = "skcode"

# Env flag that opts INTO the real capauth verifier. Unset/off keeps the P0
# deny-all placeholder, so the RCE surface stays gated by default (R2.4).
REAL_VERIFIER_ENV = "SKCODE_REAL_VERIFIER"
_TRUTHY = {"1", "true", "yes", "on"}


def resolve_bind(host: str | None) -> str:
    if not host or host.strip() in _WILDCARD:
        raise SystemExit(
            "skcode-hostd refuses to bind a wildcard/public address; "
            "pass a Tailscale IP via --host"
        )
    return host.strip()


def build_default_verifier() -> Verifier:
    # P0 placeholder: fail closed. Real capauth verification is wired with the
    # pairing work (spec 7.6); a read-only MVP must never accept a token blindly.
    def _deny_all(token: str) -> bool:
        return False

    return _deny_all


def build_capauth_verifier(home: Path | None = None) -> Verifier:
    """A real capauth verifier for skcode-hostd (R2.4).

    Accepts a caller ONLY when the bearer is a valid capauth SKCODE-audience
    token: the wire form is base64url of ``export_token(...)``, so the verifier
    base64url-decodes it, ``import_token``s the JSON, then requires
    ``verify_audience_token(t, "skcode")`` (signature + time validity + audience
    match). It is capauth-only and self-contained.

    It fails CLOSED on any parse/verify error: a non-base64 string, non-token
    JSON, an expired/garbage/unsigned token, a wrong-audience token, or an
    unscoped (legacy audience=None) token all return False. ``home`` selects the
    capauth keyring home; ``None`` uses capauth's default (~/.skcapstone).
    """
    # Import inside the factory so the module has no hard capauth import at load
    # time (the deny-all default path stays capauth-free).
    from capauth import import_token, verify_audience_token

    def _verify(token: str) -> bool:
        try:
            token = (token or "").strip()
            if not token:
                return False
            # base64url decode, tolerating missing '=' padding.
            padded = token + "=" * (-len(token) % 4)
            token_json = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            signed = import_token(token_json)
            return bool(verify_audience_token(signed, SKCODE_AUDIENCE, home=home))
        except Exception:
            # Fail closed on ANY error: bad base64, bad JSON, bad token, keyring
            # miss, etc. Never let a caller through on an exception.
            return False

    return _verify


def select_verifier() -> Verifier:
    """Pick the verifier the daemon runs with.

    Default (``SKCODE_REAL_VERIFIER`` unset or not truthy): the P0 deny-all
    placeholder, byte-identical to prior behavior, so the RCE surface stays
    gated. Only when the flag is explicitly ON is the real capauth verifier
    constructed. This is the ONLY thing the flag changes; routes, the bind
    guard, and the gate wiring are untouched.
    """
    flag = os.environ.get(REAL_VERIFIER_ENV, "").strip().lower()
    if flag in _TRUTHY:
        return build_capauth_verifier()
    return build_default_verifier()


def main(argv: list[str] | None = None) -> int | None:
    """``skcode-hostd`` entry point.

    ``skcode-hostd operator ...`` routes to the operator-facet CLI (spec 4.2, the
    seam Atlas's skcode adapter drives). Anything else is the daemon runner, whose
    ``--host`` contract is unchanged (backwards compatible with
    ``python -m skharness --host <ip>``).
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "operator":
        from skharness.operator_cli import main as operator_main

        return operator_main(args[1:])
    _serve(args)
    return 0


def _serve(argv: list[str]) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(prog="skcode-hostd")
    parser.add_argument("--host", required=True, help="Tailscale IP to bind")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host-id", default=".158", help="node id for hosts/self")
    args = parser.parse_args(argv)

    host = resolve_bind(args.host)
    harness = ClaudeCodeHarness(host=args.host_id)
    app = build_daemon_app(
        harness=harness,
        verify_caller=select_verifier(),
        host_id=args.host_id,
    )
    uvicorn.run(app, host=host, port=args.port)
