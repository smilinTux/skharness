"""Fail-closed capability profiles for the Pi-to-SK tool bridge.

This module is deliberately transport-neutral.  The Pi extension sends one operation
and a JSON object to ``skharness-pi-bridge``; this policy layer authorizes the operation
before a configured backend sees it.  It never evaluates a shell string and it never
turns an arbitrary MCP method name into authority.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Mapping


class BridgeDeniedError(PermissionError):
    """A profile did not explicitly grant the requested operation."""


_TOKEN = re.compile(r"\A[A-Za-z0-9._:@/+=-]{1,200}\Z")


@dataclass(frozen=True)
class PiCapabilityProfile:
    name: str
    builtin_tools: tuple[str, ...]
    sk_operations: frozenset[str]

    @property
    def pi_tools(self) -> tuple[str, ...]:
        return (*self.builtin_tools, *(op.replace(".", "_") for op in sorted(self.sk_operations)))


PI_PROFILES: Mapping[str, PiCapabilityProfile] = {
    "arena-build": PiCapabilityProfile(
        "arena-build",
        ("read", "edit", "write", "bash", "grep", "find", "ls"),
        frozenset({"capstone.card.read", "arena.experiment.search",
                   "arena.experiment.reproduce", "arena.experiment.mutate",
                   "arena.negative.search", "arena.progress.append", "arena.result.append",
                   "memory.recall", "memory.scratch.append"}),
    ),
    "arena-verify": PiCapabilityProfile(
        "arena-verify", ("read", "bash", "grep", "find", "ls"),
        frozenset({"capstone.card.read", "arena.verdict.append"}),
    ),
    "project-full": PiCapabilityProfile(
        "project-full",
        ("read", "edit", "write", "bash", "grep", "find", "ls"),
        frozenset({"capstone.card.read", "capstone.card.claim", "capstone.progress.append",
                   "arena.experiment.search", "arena.experiment.reproduce",
                   "arena.experiment.mutate", "arena.negative.search",
                   "arena.result.append", "memory.recall", "memory.scratch.append",
                   "memory.proposal.append"}),
    ),
    # Empty by construction: operator authority must arrive as an explicit,
    # CapAuth-approved operation set at launch, never as a baked image default.
    "operator": PiCapabilityProfile("operator", (), frozenset()),
}


class ScopedPiBridge:
    def __init__(self, profile: str, backend: Callable[[str, dict], dict]):
        try:
            self.profile = PI_PROFILES[profile]
        except KeyError as exc:
            raise BridgeDeniedError(f"unknown Pi capability profile: {profile!r}") from exc
        self.backend = backend

    def invoke(self, operation: str, payload: object) -> dict:
        if operation not in self.profile.sk_operations:
            raise BridgeDeniedError(
                f"operation {operation!r} is not granted by profile {self.profile.name!r}")
        if not _TOKEN.fullmatch(operation):
            raise BridgeDeniedError("operation is not a safe canonical token")
        if not isinstance(payload, dict):
            raise BridgeDeniedError("bridge payload must be a JSON object")
        result = self.backend(operation, payload)
        if not isinstance(result, dict):
            raise RuntimeError("SK bridge backend returned a non-object response")
        return result


def _exec_backend(operation: str, payload: dict) -> dict:
    """Call a runtime-mounted backend executable without a shell.

    The image intentionally contains no credentials or sovereign agent home.  An
    operator/controller must mount a backend executable and scoped credentials.
    """
    executable = os.environ.get("SKHARNESS_SK_BRIDGE_BACKEND")
    if not executable or not os.path.isabs(executable):
        raise RuntimeError("SKHARNESS_SK_BRIDGE_BACKEND must name an absolute mounted executable")
    proc = subprocess.run(
        [executable, operation], input=json.dumps(payload), text=True,
        capture_output=True, timeout=30, check=False,
    )
    if proc.returncode:
        raise RuntimeError(f"SK bridge backend failed with exit {proc.returncode}: {proc.stderr[:500]}")
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("SK bridge backend returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("SK bridge backend returned a non-object response")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Invoke one profile-scoped SK operation")
    parser.add_argument("--profile", required=True, choices=tuple(PI_PROFILES))
    parser.add_argument("--operation", required=True)
    parser.add_argument("--payload", required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.payload)
        result = ScopedPiBridge(args.profile, _exec_backend).invoke(args.operation, payload)
    except (BridgeDeniedError, RuntimeError, json.JSONDecodeError) as exc:
        parser.exit(2, f"skharness-pi-bridge: {exc}\n")
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0
