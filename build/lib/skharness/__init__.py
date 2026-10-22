"""skharness: sovereign orchestration harness. Spawn isolated agent sessions,
drive them from a phone over the tailnet, no Big-Tech broker. P0 = the CI-testable
session-manager core. See docs/superpowers/specs/2026-06-13-skharness-design.md.

The unified two-plane Harness contract (Fable Wave 1 "contract hoist") is the
public seam over both the autocode engine (task plane) and the skcode daemon
(session plane); see skharness/harness.py.
"""

from skharness.harness import (
    HARNESSES,
    Harness,
    HarnessCapabilities,
    build_harness,
    register_harness,
    warn_missing_capabilities,
)

__all__ = [
    "Harness",
    "HarnessCapabilities",
    "HARNESSES",
    "register_harness",
    "build_harness",
    "warn_missing_capabilities",
]
