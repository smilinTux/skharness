"""skcode-hostd runner. Binds a Tailscale IP ONLY (never 0.0.0.0), port 9390.

The port 9390 default has a KNOWN deploy-time conflict: skcomms broker_server may
already hold 0.0.0.0:9390 on this host. Resolve it before deploy (move the
broker, or pass --port). See the plan's Global Constraints.
"""
from __future__ import annotations

import argparse

from skharness.auth import Verifier
from skharness.daemon import build_daemon_app
from skharness.harnesses.claude_code import ClaudeCodeHarness

DEFAULT_PORT = 9390

_WILDCARD = {"0.0.0.0", "::"}


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


def main(argv: list[str] | None = None) -> None:
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
        verify_caller=build_default_verifier(),
        host_id=args.host_id,
    )
    uvicorn.run(app, host=host, port=args.port)
