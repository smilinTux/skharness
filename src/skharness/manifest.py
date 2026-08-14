"""skcode's SKWorld module manifest (spec 4.2).

skcode is a first-class SKWorld subapp: it declares ONE capauth-signed
skworld.module.json with two facets. The UI facet lets the shell mount skcode's
Code pane; the operator facet lets Atlas watch and steer skcode-hostd.

This module builds the manifest as a pure dict from the serving origin, so the
served URLs are origin-relative (they resolve against wherever the host actually
answers, avoiding host/port drift). The daemon serves it unauthenticated at
/.well-known/skworld-module.json (public discovery metadata, no secrets).

The operator block mirrors operator_seat/skcode_adapter.py in skcapstone. The two
live in separate repos, so the shared schema in sk-standards is the source of
truth; keep these two in sync when either changes.
"""

from __future__ import annotations

#: The manifest schema version (sk-standards manifest schema v1.1, +operator block).
SCHEMA_VERSION = "1.1"
#: The audience skcode tokens are minted for.
AUDIENCE = "skcode"


def skcode_module_manifest(base_url: str) -> dict:
    """Build skcode's skworld.module.json for a given serving origin.

    Args:
        base_url: The origin the host answers on (e.g. the request base URL,
            "http://100.x.x.x:9394/"). URLs in the manifest are built relative
            to this so they never hardcode a host or port.

    Returns:
        The manifest dict (UI facet + operator facet).
    """
    base = base_url.rstrip("/")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": "skcode",
        "name": "Code",
        # UI facet: starts Grade B (the existing read-only web client at /app),
        # promotes to Grade A (a Flutter skcode_client package) at R4 by flipping
        # grade + adding entry.flutter_package, never a contract change.
        # Grade A: packages/skcode_client in skworld-app is the real native
        # module and the shell registry mounts it at /code. entry.url is GONE as
        # of C-10's deletion half: the legacy web client and the /code/legacy
        # route were removed from the app once C-16 (repo-less direct session)
        # and C-17 (attach-mode TUI chrome filter) closed the two parity gaps
        # that had been holding it open. Advertising a url the shell no longer
        # routes would point clients at a surface that does not exist.
        # The client itself still ships at /app for direct browser use; this is
        # about what the manifest tells the shell to mount.
        "grade": "A",
        "entry": {"flutter_package": "skcode_client"},
        "nav": {"icon": "terminal", "order": 30, "label": "Code"},
        "deeplinkPrefix": "skworld://skcode/",
        "auth": {
            "audience": AUDIENCE,
            "scopes": ["skcode.stream", "skcode.inject", "skcode.dispatch"],
        },
        "memory": {"opt_in": False},
        "health": f"{base}/api/v1/hosts/self",
        # Operator facet: what Atlas's skcode adapter observes and may act on.
        "operator": {
            "contractVersion": 1,
            "cli": "skcode-hostd operator",
            "repos": ["skharness"],
            "conditions": [
                "HostdReady",
                "SessionsHealthy",
                "RegistryConsistent",
                "AuthEnforced",
            ],
            "proposedStandardActions": ["restart-hostd", "archive-stale-session"],
        },
    }


__all__ = ["skcode_module_manifest", "SCHEMA_VERSION", "AUDIENCE"]
