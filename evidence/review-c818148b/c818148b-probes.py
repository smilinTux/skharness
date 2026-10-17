"""Independent evidence-only adversarial probes for c818148b.

No candidate source is modified. All filesystem state lives under TemporaryDirectory;
all tmux/process/network actions use in-process fakes.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from fastapi.testclient import TestClient

from skharness.auth import AuthContext
from skharness.daemon import build_daemon_app
from skharness.harness import (
    FakeHarness,
    HarnessSession,
    SessionDescriptor,
    SpawnOwnershipError,
    SpawnRejected,
)
from skharness.harnesses.claude_code import ClaudeCodeHarness, CommandResult
from skharness.pool import PoolController
from skharness.securefs import SecureDir, SecurePathError


def section(name: str) -> None:
    print(f"\n=== {name} ===")


def filesystem_probes() -> None:
    section("filesystem anchoring and concurrent hard-link probes")
    with tempfile.TemporaryDirectory(prefix="c818148b-fs-") as raw:
        tmp = Path(raw)
        outside = tmp / "outside"
        outside.mkdir()

        # Every ancestor symlink must be rejected, not followed.
        (tmp / "link").symlink_to(outside, target_is_directory=True)
        try:
            SecureDir.anchor(tmp / "link" / "state")
        except OSError as exc:
            print("ANCESTOR_SYMLINK_REJECTED", type(exc).__name__, exc.errno)
        else:
            print("ANCESTOR_SYMLINK_REJECTED False")

        # Once anchored, replacement of the lexical root must not redirect writes.
        stable = tmp / "stable"
        stable.mkdir()
        anchored = SecureDir.anchor(stable)
        displaced = tmp / "displaced"
        stable.rename(displaced)
        stable.symlink_to(outside, target_is_directory=True)
        fd = anchored.write_exclusive("held.txt", b"held-inode")
        os.close(fd)
        print("POST_ANCHOR_SWAP_HELD_INODE", (displaced / "held.txt").read_text())
        print("POST_ANCHOR_SWAP_OUTSIDE_EXISTS", (outside / "held.txt").exists())
        anchored.close()

        # Concurrent hard-link creation after _verify_file's st_nlink==1 check but
        # before write. This attacks every write_exclusive user (models, transcript,
        # capture state) at the exact load-bearing TOCTOU boundary.
        root_path = tmp / "hardlink-root"
        root_path.mkdir()
        root = SecureDir.anchor(root_path)
        escaped = outside / "models-copy.json"
        original_verify = SecureDir._verify_file
        attacked = False

        def verify_then_link(fd: int, *, mode: int) -> None:
            nonlocal attacked
            original_verify(fd, mode=mode)
            if not attacked:
                os.link(root_path / "models.json", escaped)
                attacked = True

        SecureDir._verify_file = staticmethod(verify_then_link)
        try:
            fd = root.write_exclusive("models.json", b'{"route":"private"}')
            os.close(fd)
        finally:
            SecureDir._verify_file = staticmethod(original_verify)
        print("CONCURRENT_HARDLINK_WRITE_ESCAPED", escaped.read_text())
        print("CONCURRENT_HARDLINK_NLINK", os.stat(root_path / "models.json").st_nlink)
        root.close()

        # Same race against the audit append path: validate nlink=1, then add an
        # outside hard link before append. The outside alias receives the audit.
        audit_path = tmp / "audit-root"
        audit_path.mkdir()
        audit = SecureDir.anchor(audit_path)
        audit.append_durable("audit.log", b"first\n")
        audit_escape = outside / "audit-copy.log"
        attacked = False

        def audit_verify_then_link(fd: int, *, mode: int) -> None:
            nonlocal attacked
            original_verify(fd, mode=mode)
            if not attacked:
                os.link(audit_path / "audit.log", audit_escape)
                attacked = True

        SecureDir._verify_file = staticmethod(audit_verify_then_link)
        try:
            audit.append_durable("audit.log", b"mandatory-second\n")
        finally:
            SecureDir._verify_file = staticmethod(original_verify)
        print("CONCURRENT_AUDIT_HARDLINK_ESCAPED", repr(audit_escape.read_text()))
        print("CONCURRENT_AUDIT_NLINK", os.stat(audit_path / "audit.log").st_nlink)
        audit.close()


class DuplicateHarness(FakeHarness):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.teardown_targets: list[str] = []

    async def spawn_reserved(self, desc, *, prompt, excluded_sids):
        del prompt, excluded_sids
        self.calls += 1
        return HarnessSession(
            sid="sandbox-shared",
            descriptor=desc,
            status="running",
            resource_id="@original" if self.calls == 1 else "@new",
        )

    async def teardown_owned(self, resource_id):
        self.teardown_targets.append(resource_id)
        return {
            "resource_id": resource_id,
            "teardown_succeeded": False,
            "reason": "forced kill failure",
        }


async def pool_probe() -> None:
    section("PoolController post-receipt collision and unresolved containment")
    harness = DuplicateHarness()
    pool = PoolController(harness)
    original = await pool.spawn(lane="one", repo="", branch="", prompt="one")
    try:
        await pool.spawn(lane="two", repo="", branch="", prompt="two")
    except Exception as exc:
        print("POOL_EXCEPTION_TYPE", type(exc).__name__)
        print("POOL_EXCEPTION_TEXT", str(exc))
        print("POOL_EXCEPTION_HAS_RECEIPT", hasattr(exc, "receipt"))
        print("POOL_EXCEPTION_DICT", getattr(exc, "__dict__", {}))
    print("POOL_TEARDOWN_TARGETS", harness.teardown_targets)
    print("POOL_ORIGINAL_PRESERVED", pool.members()[0] is original, original.session.resource_id)


@dataclass
class Obligation:
    kind: str = "audit"
    data: dict = field(default_factory=dict)


@dataclass
class Decision:
    allow: bool = True
    reason: str = "allow"
    obligations: list = field(default_factory=lambda: [Obligation(data={"decision": "allow"})])


class UnresolvedSpawnHarness(FakeHarness):
    def __init__(self) -> None:
        super().__init__()
        self.spawn_calls = 0

    async def spawn(self, desc, *, prompt):
        del desc, prompt
        self.spawn_calls += 1
        raise SpawnOwnershipError(
            "tmux setup failed",
            receipt={
                "sid": "sandbox-race",
                "resource_id": "@77",
                "launched": True,
                "tracked": False,
                "setup_succeeded": False,
                "teardown_succeeded": False,
                "teardown_reason": "forced rollback kill failure",
            },
        )


def daemon_receipt_probe() -> None:
    section("daemon handling of unresolved SpawnOwnershipError")
    harness = UnresolvedSpawnHarness()
    verifier = lambda token: AuthContext(
        scopes=frozenset({"skcode.dispatch"}), subject="reviewer@chiap03"
    )
    authorizer = lambda subject, resource, context: Decision()
    audits: list[str] = []
    app = build_daemon_app(
        harness=harness,
        verify_caller=verifier,
        authorize_dispatch=authorizer,
        audit_log=audits.append,
        host_id="chiap03",
    )
    body = {
        "harness": "fake",
        "host": "chiap03",
        "repo": "",
        "branch": "",
        "profile": "sandbox",
        "prompt": "local fake only",
    }
    response = TestClient(app).post(
        "/api/v1/dispatch", json=body, headers={"authorization": "Bearer local-fake"}
    )
    print("DAEMON_STATUS", response.status_code)
    print("DAEMON_BODY", json.dumps(response.json(), sort_keys=True))
    print("DAEMON_EXPOSES_RESOURCE_ID", "@77" in response.text)
    print("DAEMON_EXPOSES_TEARDOWN_FAILURE", "teardown_succeeded" in response.text)
    print("DAEMON_SPAWN_CALLS", harness.spawn_calls)


class ArchiveRunner:
    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.targets: list[tuple[str, str]] = []

    def __call__(self, argv: list[str]) -> CommandResult:
        if "list-windows" in argv:
            return CommandResult(tuple(argv), 0, f"{self.sid}\t123\n", "")
        if "capture-pane" in argv:
            self.targets.append(("capture", argv[argv.index("-t") + 1]))
            return CommandResult(tuple(argv), 0, "transcript", "")
        if "kill-window" in argv:
            self.targets.append(("kill", argv[argv.index("-t") + 1]))
            return CommandResult(tuple(argv), 0, "", "")
        return CommandResult(tuple(argv), 0, "", "")


async def archive_identity_probe() -> None:
    section("archive exact-resource ownership")
    with tempfile.TemporaryDirectory(prefix="c818148b-archive-") as raw:
        tmp = Path(raw)
        sid = "sandbox-collision"
        runner = ArchiveRunner(sid)
        harness = ClaudeCodeHarness(
            runner=runner,
            dispatch_repos=[],
            sessions_root=tmp / "sessions",
            worktree_root=tmp / "worktrees",
            reservation_root=tmp / "reservations",
        )
        # Simulate the exact identity recorded by a successful spawn. A colliding
        # tmux name now exists; archive must target @new to prove ownership.
        harness._spawned_sids.add(sid)
        harness._resource_ids[sid] = "@new"
        result = await harness.archive(sid)
        print("ARCHIVE_RECEIPT", json.dumps(result, sort_keys=True))
        print("ARCHIVE_TMUX_TARGETS", runner.targets)
        print("ARCHIVE_USED_EXACT_RESOURCE", all(target == "@new" for _, target in runner.targets))


def rollback_identity_probe() -> None:
    section("setup rollback exact-resource ownership")
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> CommandResult:
        calls.append(argv)
        return CommandResult(tuple(argv), 1, "", "forced kill failure")

    harness = ClaudeCodeHarness(runner=runner, dispatch_repos=[])
    failure = CommandResult(("tmux", "pipe-pane"), 1, "", "forced setup failure")
    try:
        harness._rollback_created_window("sandbox-shared", "tmux pipe-pane", failure)
    except SpawnOwnershipError as exc:
        print("ROLLBACK_RECEIPT", json.dumps(exc.receipt, sort_keys=True))
    targets = [argv[argv.index("-t") + 1] for argv in calls if "-t" in argv]
    print("ROLLBACK_TMUX_TARGETS", targets)
    print("ROLLBACK_TARGET_IS_AMBIGUOUS_SID", targets == ["skchat-agents:sandbox-shared"])


async def main() -> None:
    filesystem_probes()
    await pool_probe()
    daemon_receipt_probe()
    await archive_identity_probe()
    rollback_identity_probe()


if __name__ == "__main__":
    asyncio.run(main())
