"""Harness-agnostic Docker confinement: the single subprocess boundary for live
harness execution. Secrets are confined by absence (nothing secret is mounted);
egress is an internal network whose only route out is the allowlist proxy.

NOTE: a LaunchSpec can also carry `config_files` (container-path -> content) for
an adapter to inject a GENERATED config into the container (e.g. pi's
models.json routing to a local skgateway model). Sandbox.spawn writes those to
a per-run host temp dir and mounts them read-only; it does NOT open any new
egress. Reaching a local http service (skgateway) from inside the sandbox is a
separate networking concern; see adapters/pi.py for that follow-up note."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .claude_code import HarnessUnavailable

PROXY_PORT = 8080


@dataclass
class AuthMount:
    src: str
    dst: str
    ro: bool = True


@dataclass
class LaunchSpec:
    name: str
    argv: list[str]
    image: str
    worktree: str
    auth_mounts: list[AuthMount] = field(default_factory=list)
    auth_env: dict[str, str] = field(default_factory=dict)
    egress_hosts: list[str] = field(default_factory=list)
    config_files: dict[str, str] = field(default_factory=dict)
    stdin: str | None = None
    cpu_limit: float | None = None
    memory_gb_limit: float | None = None
    required_commands: list[str] = field(default_factory=list)
    # Trusted, image-local executable probes. Unlike required_commands these
    # validate behavior, not mere PATH presence. Each inner list is argv.
    required_checks: list[list[str]] = field(default_factory=list)


class Sandbox:
    def __init__(self, live_execution: bool = False, docker: str = "docker",
                 run_timeout: int = 1800) -> None:
        self.live_execution = live_execution
        self.docker = docker
        self.run_timeout = run_timeout

    @staticmethod
    def _linked_worktree_git_mount(worktree: str) -> AuthMount | None:
        """Resolve the minimum Git metadata mount needed by a linked worktree.

        Git stores a linked worktree's ``.git`` as a pointer to an administrative
        directory below the repository's common Git directory.  Mounting only the
        worktree leaves that absolute pointer dangling in the container.  Expose
        the common directory read-only at the same absolute path so Git can read
        HEAD, refs, objects, and config without granting the worker authority to
        mutate repository metadata.

        A normal checkout has a ``.git`` directory and needs no extra mount.  A
        malformed or non-standard linked-worktree relationship fails closed.
        """
        dotgit = Path(os.path.realpath(worktree)) / ".git"
        if dotgit.is_dir() or not dotgit.exists():
            return None
        if not dotgit.is_file():
            raise HarnessUnavailable("worktree .git metadata is not a file or directory")
        try:
            marker = dotgit.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise HarnessUnavailable(f"cannot read worktree .git metadata: {exc}") from exc
        prefix = "gitdir: "
        if not marker.startswith(prefix) or "\n" in marker:
            raise HarnessUnavailable("malformed linked-worktree .git pointer (fail closed)")
        raw_gitdir = marker[len(prefix):].strip()
        if not raw_gitdir:
            raise HarnessUnavailable("empty linked-worktree .git pointer (fail closed)")
        gitdir = Path(raw_gitdir)
        if not gitdir.is_absolute():
            gitdir = dotgit.parent / gitdir
        gitdir = gitdir.resolve(strict=False)
        if not gitdir.is_dir():
            raise HarnessUnavailable("linked-worktree Git administrative directory is missing")
        commondir_file = gitdir / "commondir"
        try:
            raw_common = commondir_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise HarnessUnavailable(
                f"linked-worktree Git common-dir metadata is unavailable: {exc}"
            ) from exc
        if not raw_common or "\n" in raw_common:
            raise HarnessUnavailable("malformed linked-worktree Git common-dir (fail closed)")
        common = Path(raw_common)
        if not common.is_absolute():
            common = gitdir / common
        common = common.resolve(strict=False)
        if not common.is_dir():
            raise HarnessUnavailable("linked-worktree Git common directory is missing")
        try:
            gitdir.relative_to(common)
        except ValueError as exc:
            raise HarnessUnavailable(
                "linked-worktree administrative directory escapes its Git common directory"
            ) from exc
        return AuthMount(str(common), str(common), ro=True)

    def _docker_run_argv(self, spec: LaunchSpec, network: str, proxy_alias: str,
                         container_name: str | None = None,
                         extra_mounts: list[AuthMount] | None = None) -> list[str]:
        wt = os.path.realpath(spec.worktree)
        all_mounts = list(spec.auth_mounts) + list(extra_mounts or [])
        git_mount = self._linked_worktree_git_mount(wt)
        argv = [self.docker, "run"]
        if container_name:
            argv += ["--name", container_name]
        if spec.stdin is not None:
            argv += ["-i"]                      # keep stdin open so the harness can read it
        argv += [
            "--rm", "--network", network,
            # run as the host uid:gid so the bind-mounted worktree is writable;
            # still non-root-privileged (caps dropped, no-new-privileges, read-only
            # rootfs, no docker socket), so confinement holds.
            "--user", f"{os.getuid()}:{os.getgid()}", "--workdir", "/work",
            "--read-only", "--tmpfs", "/tmp:mode=1777",
            "--tmpfs", "/home/sbx:mode=1777",       # writable HOME for an arbitrary uid
            "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
            "--pids-limit", "512",
            "--env", "HOME=/home/sbx",
            "--mount", f"type=bind,src={wt},dst=/work",
            "--env", f"HTTPS_PROXY=http://{proxy_alias}:{PROXY_PORT}",
            "--env", f"HTTP_PROXY=http://{proxy_alias}:{PROXY_PORT}",
        ]
        if git_mount is not None:
            argv += [
                "--mount",
                f"type=bind,src={git_mount.src},dst={git_mount.dst},readonly",
            ]
        if spec.cpu_limit is not None:
            if spec.cpu_limit <= 0:
                raise ValueError("cpu_limit must be positive when set")
            argv += ["--cpus", f"{spec.cpu_limit:g}"]
        if spec.memory_gb_limit is not None:
            if spec.memory_gb_limit <= 0:
                raise ValueError("memory_gb_limit must be positive when set")
            memory = f"{spec.memory_gb_limit:g}g"
            # Equal memory/swap values prohibit additional swap consumption.
            argv += ["--memory", memory, "--memory-swap", memory]
        # Each auth mount's parent dir is auto-created by docker as a root-owned,
        # non-writable dir; the harness (e.g. claude) needs to write siblings there
        # (session-env, cache). Mount a writable tmpfs at each such parent first so
        # the RO cred file binds inside a writable dir. Skip HOME/root, already tmpfs.
        # Injected config files (extra_mounts) get the same treatment: their parent
        # dir also needs to be a writable tmpfs before the RO file binds inside it.
        for parent in sorted({os.path.dirname(m.dst) for m in all_mounts}):
            if parent and parent not in ("/", "/home/sbx"):
                argv += ["--tmpfs", f"{parent}:mode=1777"]
        for m in all_mounts:
            src = os.path.realpath(os.path.expanduser(m.src))
            ro = ",readonly" if m.ro else ""
            argv += ["--mount", f"type=bind,src={src},dst={m.dst}{ro}"]
        for k, v in spec.auth_env.items():
            argv += ["--env", f"{k}={v}"]
        argv += [spec.image, *spec.argv]
        return argv

    def _proxy_run_argv(
        self, *, name: str, network: str, alias: str, allow: list[str]
    ) -> list[str]:
        """Build the equally confined egress-proxy sidecar command."""
        return [
            self.docker, "run", "-d", "--name", name,
            "--network", network, "--network-alias", alias,
            "--user", "65534:65534", "--read-only", "--tmpfs", "/tmp:mode=1777",
            "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
            "--pids-limit", "64", "--cpus", "0.5",
            "--memory", "128m", "--memory-swap", "128m",
            "sandbox-proxy:1", "python", "-m", "skharness.autocode.sandbox_proxy",
            str(PROXY_PORT), *allow,
        ]

    def _ensure_capable(self, spec: LaunchSpec) -> None:
        # Resolve linked-worktree metadata before image/network/container setup.
        # This both validates the relationship and guarantees a dangling .git
        # pointer cannot be admitted into a live run.
        self._linked_worktree_git_mount(spec.worktree)
        if not shutil.which(self.docker):
            raise HarnessUnavailable("docker not found on this node (fail closed)")
        r = subprocess.run([self.docker, "image", "inspect", spec.image],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise HarnessUnavailable(
                f"sandbox image {spec.image!r} not present; build it before live run (fail closed)")
        for command in spec.required_commands:
            probe = subprocess.run(
                [self.docker, "run", "--rm", "--network", "none", "--read-only",
                 "--entrypoint", "sh", spec.image, "-c", f"command -v {command}"],
                capture_output=True,
                text=True,
            )
            if probe.returncode != 0:
                raise HarnessUnavailable(
                    f"sandbox image {spec.image!r} lacks required command {command!r}; "
                    "select a project-qualified/test-capable image before admission"
                )
        for check in spec.required_checks:
            if not check or not all(isinstance(part, str) and part for part in check):
                raise HarnessUnavailable("sandbox image required check has invalid argv")
            probe = subprocess.run(
                [self.docker, "run", "--rm", "--network", "none", "--read-only",
                 "--entrypoint", check[0], spec.image, *check[1:]],
                capture_output=True,
                text=True,
            )
            if probe.returncode != 0:
                detail = (probe.stderr or probe.stdout or "no diagnostic output").strip()
                raise HarnessUnavailable(
                    f"sandbox image {spec.image!r} failed required executable check "
                    f"{check[0]!r}: {detail[:240]}"
                )

    def _wait_for_proxy(self, name: str, attempts: int = 50) -> None:
        """Do not launch a worker until its only permitted egress route listens."""
        probe = (
            "import socket; s=socket.create_connection(('127.0.0.1', "
            f"{PROXY_PORT}), 0.2); s.close()"
        )
        for _ in range(attempts):
            ready = subprocess.run(
                [self.docker, "exec", name, "python", "-c", probe],
                capture_output=True, text=True,
            )
            if ready.returncode == 0:
                return
            time.sleep(0.1)
        logs = subprocess.run(
            [self.docker, "logs", "--tail", "20", name],
            capture_output=True, text=True,
        )
        detail = (logs.stderr or logs.stdout or "proxy did not become ready").strip()
        raise HarnessUnavailable(f"sandbox egress proxy unavailable (fail closed): {detail[:240]}")

    def spawn(self, spec: LaunchSpec, *, repo_remote_host=None, ci_host=None) -> dict:
        if not self.live_execution:
            raise HarnessUnavailable(
                "live harness execution is disabled (posture C / config): set "
                "harness.live_execution=true only after the confinement proof passes.")
        self._ensure_capable(spec)
        allow = [h for h in ([repo_remote_host, ci_host] + list(spec.egress_hosts)) if h]
        token = secrets.token_hex(4)
        net = f"sbxnet-{token}"
        proxy_alias = "sbxproxy"
        proxy_name = f"sbxproxy-{token}"
        harness_name = f"sbxrun-{token}"
        cfg_dir = None
        try:
            cfg_mounts = []
            if spec.config_files:
                cfg_dir = tempfile.mkdtemp(prefix="sbxcfg-")
                for i, (dst, content) in enumerate(spec.config_files.items()):
                    host_path = os.path.join(cfg_dir, f"cfg{i}")
                    with open(host_path, "w") as fh:
                        fh.write(content)
                    cfg_mounts.append(AuthMount(src=host_path, dst=dst, ro=True))
            subprocess.run([self.docker, "network", "create", "--internal", net],
                           capture_output=True, text=True, check=True)
            # proxy sidecar: dual-homed (internal net + default bridge) so it is the
            # ONLY route out; started with the pinned allowlist; reached by alias.
            subprocess.run(
                self._proxy_run_argv(
                    name=proxy_name, network=net, alias=proxy_alias, allow=allow
                ),
                capture_output=True, text=True, check=True)
            subprocess.run([self.docker, "network", "connect", "bridge", proxy_name],
                           capture_output=True, text=True)          # give proxy outward egress
            self._wait_for_proxy(proxy_name)
            run_kwargs = {"capture_output": True, "text": True, "cwd": spec.worktree,
                          "timeout": self.run_timeout}
            if spec.stdin is not None:
                run_kwargs["input"] = spec.stdin
            try:
                proc = subprocess.run(
                    self._docker_run_argv(spec, net, proxy_alias, container_name=harness_name,
                                          extra_mounts=cfg_mounts),
                    **run_kwargs)
            except subprocess.TimeoutExpired as e:
                # Preserve whatever the harness streamed before the kill. An agentic
                # harness (opencode) emits its direct answer in the FIRST event, then
                # over-runs; discarding partial stdout here would throw that answer
                # away on every timeout. The adapter's _parse pulls the first valid
                # JSON reply out of the partial stream.
                partial = e.stdout if isinstance(e.stdout, str) else (
                    e.stdout.decode(errors="replace") if e.stdout else "")
                return {"result": partial, "is_error": True, "exit_code": 124,
                        "timeout": True}
            try:
                return json.loads(proc.stdout or "{}")
            except json.JSONDecodeError:
                return {"result": proc.stdout, "stderr": proc.stderr,
                        "exit_code": proc.returncode, "is_error": proc.returncode != 0}
        finally:
            subprocess.run([self.docker, "rm", "-f", harness_name], capture_output=True, text=True)
            subprocess.run([self.docker, "rm", "-f", proxy_name], capture_output=True, text=True)
            subprocess.run([self.docker, "network", "rm", net], capture_output=True, text=True)
            if cfg_dir:
                shutil.rmtree(cfg_dir, ignore_errors=True)
