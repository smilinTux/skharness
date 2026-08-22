"""Pi experiment execution composed with durable arena lifecycle state.

The production supervisor reuses :class:`Sandbox` launch hardening while replacing
its blocking ``subprocess.run`` boundary with a cancellable ``Popen``. Stdout and
stderr stream directly to attempt files, so timeout, OOM, controller crash, or host
restart cannot erase partial evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Protocol

from skharness.activity import (
    ActivityContext,
    ActivityJournal,
    ActivityKind,
    sanitize_activity_text,
)
from skharness.autocode.pi_events import (
    PiEventScan,
    assistant_message_events,
    scan_pi_events,
    served_model_evidence,
    valid_pi_event_envelope,
)
from skharness.autocode.sandbox import AuthMount, InspectionScope, LaunchSpec, Sandbox
from skharness.autocode.sandbox_lifecycle import SandboxOwnership

from .controller import ArenaController
from .models import ExperimentState
from .scheduler import Admission, AttemptRequest
from .swarm import ScoutFinding
from .trajectory import DEFAULT_PHASE_BUDGETS, CardSize, PhaseBudget, compact_pi_events

if TYPE_CHECKING:
    from skharness.autocode.adapters.pi import PiAdapter


@dataclass(frozen=True)
class RunOutcome:
    successful: bool
    classification: str
    exit_code: int | None
    stdout_digest: str
    stderr_digest: str
    partial: bool = False
    metrics: dict[str, object] | None = None
    disposition: str | None = None
    scout_assessment: str | None = None
    scout_findings: tuple[dict[str, object], ...] = ()


class TaskDisposition(str, Enum):
    """Task-level state, deliberately separate from process termination."""

    COMPLETED = "completed"
    BLOCKED = "blocked"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"


class AttemptSupervisor(Protocol):
    def run(self, spec: LaunchSpec, attempt_dir: Path, timeout_s: float) -> tuple[int, str]: ...
    def cancel(self) -> None: ...


class DockerSupervisorError(RuntimeError):
    """A bounded Docker control-plane operation failed closed."""


class _PiActivityTailer:
    """Publish bounded Pi envelopes while stdout is still being written."""

    def __init__(
        self,
        journal: ActivityJournal,
        context: ActivityContext,
        path: Path,
    ) -> None:
        self.journal = journal
        self.context = context
        self.path = path
        self.stop_event = threading.Event()
        self.errors = 0
        self._offset = 0
        self._buffer = b""
        self._thread = threading.Thread(
            target=self._run,
            name="arena-pi-activity-tail",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop_and_drain(self) -> None:
        self.stop_event.set()
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            self.errors += 1

    def _publish(
        self,
        kind: ActivityKind,
        summary: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.journal.publish(self.context, kind, summary=summary, data=data)
        except Exception:  # noqa: BLE001 - observability cannot change worker outcome
            self.errors += 1

    @staticmethod
    def _assistant_text(message: dict[str, Any]) -> str:
        content = message.get("content")
        if not isinstance(content, list):
            return ""
        chunks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    chunks.append(text)
        return sanitize_activity_text("\n".join(chunks))

    def _event(self, event: dict[str, Any]) -> None:
        event_type = str(event["type"])
        if event_type == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                text = self._assistant_text(message)
                self._publish(
                    ActivityKind.ASSISTANT_TEXT,
                    text or "assistant message completed",
                    data={
                        "stop_reason": message.get("stopReason"),
                        "response_model": message.get("responseModel"),
                    },
                )
                usage = message.get("usage")
                if isinstance(usage, dict):
                    cost = usage.get("cost")
                    self._publish(
                        ActivityKind.BUDGET,
                        "provider usage observed",
                        data={
                            "total_tokens": usage.get("totalTokens"),
                            "cost": cost.get("total") if isinstance(cost, dict) else None,
                        },
                    )
            return
        if event_type == "tool_execution_start":
            tool = event.get("toolName")
            self._publish(
                ActivityKind.TOOL_CALL,
                f"{tool or 'unknown'} started",
                data={"tool": tool},
            )
            return
        if event_type == "tool_execution_end":
            tool = event.get("toolName")
            self._publish(
                ActivityKind.TOOL_RESULT,
                f"{tool or 'unknown'} finished",
                data={"tool": tool, "is_error": bool(event.get("isError", False))},
            )
            if (
                isinstance(tool, str)
                and tool.lower() in {"edit", "write", "apply_patch"}
                and not bool(event.get("isError", False))
            ):
                self._publish(
                    ActivityKind.FILE_CHANGE,
                    "worktree edit completed",
                    data={"tool": tool},
                )
            return
        kind = (
            ActivityKind.PHASE
            if event_type.startswith(("turn_", "compaction_", "auto_retry_"))
            else ActivityKind.STATUS
        )
        self._publish(kind, event_type.replace("_", " "), data={"event": event_type})

    def _read_available(self, *, final: bool = False) -> None:
        if self.path.exists():
            try:
                with self.path.open("rb") as stream:
                    stream.seek(self._offset)
                    chunk = stream.read()
            except OSError:
                self.errors += 1
                return
            self._offset += len(chunk)
            self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                event = None
            if not valid_pi_event_envelope(event):
                self._publish(
                    ActivityKind.ERROR,
                    "Pi emitted an invalid structured event",
                    data={"stream_integrity": "incomplete"},
                )
                continue
            self._event(event)
        if final and self._buffer.strip():
            self._publish(
                ActivityKind.ERROR,
                "Pi event stream ended with an incomplete record",
                data={"stream_integrity": "incomplete"},
            )
            self._buffer = b""

    def _run(self) -> None:
        while not self.stop_event.wait(0.05):
            self._read_available()
        self._read_available(final=True)


class SandboxProcessSupervisor:
    """Cancellable real Docker supervisor; never a FakeSpawner adaptation."""

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        repo_remote_host: str | None = None,
        ci_host: str | None = None,
        shutdown_grace_s: float = 5.0,
        docker_timeout_s: float = 3.0,
        preflight_timeout_s: float = 30.0,
        startup_timeout_s: float = 9.0,
        active_run_ids: Callable[[], Iterable[str]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not sandbox.live_execution:
            raise ValueError("production arena supervisor requires Sandbox(live_execution=True)")
        self.sandbox = sandbox
        self.repo_remote_host = repo_remote_host
        self.ci_host = ci_host
        if shutdown_grace_s <= 0:
            raise ValueError("shutdown_grace_s must be positive")
        if docker_timeout_s <= 0:
            raise ValueError("docker_timeout_s must be positive")
        if preflight_timeout_s <= 0:
            raise ValueError("preflight_timeout_s must be positive")
        if startup_timeout_s <= 0:
            raise ValueError("startup_timeout_s must be positive")
        self.shutdown_grace_s = shutdown_grace_s
        self.docker_timeout_s = docker_timeout_s
        self.preflight_timeout_s = preflight_timeout_s
        self.startup_timeout_s = startup_timeout_s
        self.active_run_ids = active_run_ids or (lambda: ())
        self.monotonic = monotonic
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._container_name: str | None = None
        self._cancel_requested = threading.Event()

    @property
    def cancel_bound_s(self) -> float:
        """Maximum blocking wait inside :meth:`cancel`, excluding scheduler drain."""
        return self.docker_timeout_s + self.shutdown_grace_s

    def _raise_if_cancelled(self) -> None:
        if self._cancel_requested.is_set():
            raise DockerSupervisorError("sandbox launch cancelled by controller")

    def _run_checked(self, argv: list[str], *, deadline: float) -> None:
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise DockerSupervisorError("Docker startup deadline expired (fail closed)")
        try:
            subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=True,
                timeout=min(self.docker_timeout_s, remaining),
            )
        except subprocess.TimeoutExpired as exc:
            raise DockerSupervisorError(
                f"Docker startup timed out after at most {self.docker_timeout_s:g}s"
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "Docker command failed").strip()
            raise DockerSupervisorError(f"Docker startup failed: {detail[:240]}") from exc
        self._raise_if_cancelled()

    def _cleanup_resources(
        self, *, docker: str, worker: str, proxy: str, network: str
    ) -> None:
        """Attempt every exact resource removal before reporting bounded errors."""
        errors: list[str] = []
        commands = (
            ("worker", [docker, "rm", "-f", worker]),
            ("proxy", [docker, "rm", "-f", proxy]),
            ("network", [docker, "network", "rm", network]),
        )
        for role, argv in commands:
            try:
                result = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=self.docker_timeout_s,
                )
            except subprocess.TimeoutExpired:
                errors.append(
                    f"{role}: Docker removal timed out after {self.docker_timeout_s:g}s"
                )
                continue
            except Exception as exc:  # noqa: BLE001 - attempt all cleanup paths first
                errors.append(f"{role}: {type(exc).__name__}: {exc}"[:240])
                continue
            if result.returncode == 0:
                continue
            detail = (result.stderr or result.stdout or "docker removal failed").strip()
            normalized = detail.casefold()
            absent_marker = "no such network" if role == "network" else "no such container"
            if absent_marker in normalized:
                continue
            errors.append(f"{role}: exit {result.returncode}: {detail}"[:240])
        if errors:
            raise DockerSupervisorError(
                "sandbox cleanup failed after all attempts:\n- " + "\n- ".join(errors)
            )

    def run(self, spec: LaunchSpec, attempt_dir: Path, timeout_s: float) -> tuple[int, str]:
        self._raise_if_cancelled()
        reconciliation = self.sandbox.maybe_reconcile_orphans(
            active_run_ids=self.active_run_ids(),
            active_lease_ids_authoritative=True,
        )
        if reconciliation.get("outcome") != "ok":
            raise DockerSupervisorError(
                "sandbox orphan reconciliation did not prove a clean admission: "
                f"{reconciliation.get('outcome', 'unknown')}"
            )
        self._raise_if_cancelled()
        token = secrets.token_hex(6)
        ownership = (
            SandboxOwnership(spec.sandbox_run_id, authority="lease")
            if spec.sandbox_run_id is not None
            else SandboxOwnership.create()
        )
        network = f"arena-net-{token}"
        proxy_alias = "sbxproxy"
        proxy_name = f"arena-proxy-{token}"
        container_name = f"arena-pi-{token}"
        allow = [
            item for item in (self.repo_remote_host, self.ci_host, *spec.egress_hosts) if item
        ]
        config_dir: str | None = None
        config_mounts: list[AuthMount] = []
        attempt_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._container_name = container_name
        try:
            preflight_deadline = self.monotonic() + self.preflight_timeout_s
            self.sandbox._ensure_capable(
                spec,
                deadline=preflight_deadline,
                cancelled=self._cancel_requested.is_set,
                ownership=ownership,
                container_name=container_name,
            )
            self._raise_if_cancelled()
            startup_deadline = self.monotonic() + self.startup_timeout_s
            if spec.config_files:
                config_dir = tempfile.mkdtemp(prefix="arena-pi-config-")
                for index, (destination, content) in enumerate(spec.config_files.items()):
                    source = Path(config_dir) / f"config-{index}"
                    source.write_text(content, encoding="utf-8")
                    config_mounts.append(AuthMount(str(source), destination, ro=True))
            self._run_checked(
                self.sandbox._network_create_argv(network, ownership),
                deadline=startup_deadline,
            )
            self._run_checked(
                self.sandbox._proxy_run_argv(
                    name=proxy_name,
                    network=network,
                    alias=proxy_alias,
                    allow=allow,
                    ownership=ownership,
                ),
                deadline=startup_deadline,
            )
            self._run_checked(
                [self.sandbox.docker, "network", "connect", "bridge", proxy_name],
                deadline=startup_deadline,
            )
            self._raise_if_cancelled()
            argv = self.sandbox._docker_run_argv(
                spec,
                network,
                proxy_alias,
                container_name=container_name,
                extra_mounts=config_mounts,
                ownership=ownership,
            )
            # Sandbox.spawn normally uses --rm because it needs no post-exit
            # classification. The arena must inspect State.OOMKilled after exit,
            # so retain the stopped container until this supervisor's finally.
            argv.remove("--rm")
            with (
                (attempt_dir / "stdout.log").open("wb") as stdout,
                (attempt_dir / "stderr.log").open("wb") as stderr,
            ):
                process = subprocess.Popen(
                    argv,
                    cwd=spec.worktree,
                    stdin=subprocess.PIPE if spec.stdin is not None else subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
                with self._lock:
                    self._process = process
                    self._container_name = container_name
                inspection_denial = threading.Event()
                inspection_detail: dict[str, object] = {}
                inspection_observation: dict[str, object] = {
                    "root": spec.inspection_scope.root if spec.inspection_scope else None,
                    "max_calls": spec.inspection_scope.max_calls if spec.inspection_scope else None,
                    "observed_calls": 0,
                    "remaining_calls": spec.inspection_scope.max_calls if spec.inspection_scope else None,
                    "denial_reason": None,
                    "stream_status": "running",
                }
                monitor = None
                if spec.inspection_scope is not None:
                    monitor = threading.Thread(
                        target=self._monitor_inspection,
                        args=(attempt_dir / "stdout.log", spec.inspection_scope,
                              process, inspection_denial, inspection_detail,
                              inspection_observation),
                        daemon=True,
                        name="arena-inspection-scope",
                    )
                    monitor.start()
                if spec.stdin is not None and process.stdin is not None:
                    process.stdin.write(spec.stdin.encode())
                    process.stdin.close()
                classification = "exit"
                try:
                    exit_code = process.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    classification = "timeout"
                    try:
                        self.cancel()
                    except DockerSupervisorError as exc:
                        raise DockerSupervisorError(
                            f"worker timeout cancellation failed: {exc}"
                        ) from exc
                    exit_code = process.wait(timeout=self.shutdown_grace_s)
                if monitor is not None:
                    monitor.join(timeout=1)
                    inspection_observation["stream_status"] = (
                        "denied" if inspection_denial.is_set() else "complete"
                    )
                    (attempt_dir / "inspection-observation.json").write_text(
                        json.dumps(inspection_observation, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                if inspection_denial.is_set():
                    classification = "inspection_denied"
                    (attempt_dir / "inspection-denial.json").write_text(
                        json.dumps(inspection_detail, sort_keys=True) + "\n", encoding="utf-8"
                    )
            if classification not in {"timeout", "inspection_denied"}:
                self._raise_if_cancelled()
            if classification == "exit":
                # Docker reserves 125 for a client/daemon launch failure: the
                # workload was never invoked and a container may not exist to
                # inspect.  Attempting OOM inspection in that case masks the
                # primary stderr and return code as a supervisor failure.
                if exit_code == 125:
                    classification = "docker_launch_error"
                elif self._oom_killed(container_name):
                    classification = "oom"
            return exit_code, classification
        finally:
            with self._lock:
                self._process = None
                self._container_name = None
            try:
                self._cleanup_resources(
                    docker=self.sandbox.docker,
                    worker=container_name,
                    proxy=proxy_name,
                    network=network,
                )
            finally:
                if config_dir:
                    shutil.rmtree(config_dir, ignore_errors=True)

    def _monitor_inspection(
        self,
        path: Path,
        scope: InspectionScope,
        process: subprocess.Popen,
        denied: threading.Event,
        detail: dict[str, object],
        observation: dict[str, object] | None = None,
    ) -> None:
        """Tail Pi tool-start envelopes and terminate out-of-scope discovery."""
        offset = 0
        calls = 0
        while process.poll() is None or (path.exists() and path.stat().st_size > offset):
            if not path.exists():
                time.sleep(0.02)
                continue
            with path.open("rb") as stream:
                stream.seek(offset)
                lines = stream.readlines()
                offset = stream.tell()
            for raw in lines:
                violation, inspected = inspect_pi_tool_event(raw, scope)
                calls += inspected
                if observation is not None:
                    observation.update({
                        "observed_calls": calls,
                        "remaining_calls": max(0, scope.max_calls - calls),
                    })
                if violation is None and calls <= scope.max_calls:
                    continue
                reason = violation or "inspection_call_budget_exceeded"
                if observation is not None:
                    observation["denial_reason"] = reason
                detail.update(
                    {
                        "type": "inspection_denial",
                        "reason": reason,
                        "root": scope.root,
                        "observed_calls": calls,
                        "max_calls": scope.max_calls,
                    }
                )
                denied.set()
                try:
                    self.cancel()
                except DockerSupervisorError as exc:
                    detail["cancellation_error"] = str(exc)[:240]
                return
            time.sleep(0.02)


    def _oom_killed(self, container_name: str) -> bool:
        argv = [
            self.sandbox.docker,
            "inspect",
            "--format",
            "{{.State.OOMKilled}}",
            container_name,
        ]
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.docker_timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise DockerSupervisorError(
                f"Docker OOM inspection timed out after {self.docker_timeout_s:g}s"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "Docker inspect failed").strip()
            raise DockerSupervisorError(f"Docker OOM inspection failed: {detail[:240]}")
        observed = result.stdout.strip().lower()
        if observed not in {"true", "false"}:
            raise DockerSupervisorError("Docker OOM inspection returned an invalid state")
        return observed == "true"

    def cancel(self) -> None:
        """Stop the worker within ``docker_timeout_s + shutdown_grace_s`` seconds."""
        self._cancel_requested.set()
        with self._lock:
            process = self._process
            container = self._container_name
        errors: list[str] = []
        if container:
            try:
                result = subprocess.run(
                    [self.sandbox.docker, "rm", "-f", container],
                    capture_output=True,
                    text=True,
                    timeout=self.docker_timeout_s,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "Docker removal failed").strip()
                    if "no such container" not in detail.casefold():
                        errors.append(f"worker: exit {result.returncode}: {detail}"[:240])
            except subprocess.TimeoutExpired:
                errors.append(
                    f"worker: Docker cancellation timed out after {self.docker_timeout_s:g}s"
                )
            except Exception as exc:  # noqa: BLE001 - process signalling must still run
                errors.append(f"worker: {type(exc).__name__}: {exc}"[:240])
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception as exc:  # noqa: BLE001 - report after bounded fallback
                errors.append(f"worker signal: {type(exc).__name__}: {exc}"[:240])
            else:
                try:
                    process.wait(timeout=self.shutdown_grace_s)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except Exception as exc:  # noqa: BLE001 - bounded reporting
                        errors.append(f"worker kill: {type(exc).__name__}: {exc}"[:240])
                except Exception as exc:  # noqa: BLE001 - bounded reporting
                    errors.append(f"worker wait: {type(exc).__name__}: {exc}"[:240])
        if errors:
            raise DockerSupervisorError(
                "sandbox cancellation failed after bounded attempts:\n- "
                + "\n- ".join(errors)
            )


def inspect_pi_tool_event(raw: bytes, scope: InspectionScope) -> tuple[str | None, int]:
    """Return a stable denial reason and discovery-call count for one Pi envelope."""
    try:
        event = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, 0
    if not isinstance(event, dict) or event.get("type") != "tool_execution_start":
        return None, 0
    tool = event.get("toolName")
    args = event.get("args")
    if not isinstance(args, dict):
        return (
            ("malformed_inspection_arguments", 1)
            if tool in {"find", "grep", "ls"}
            else (None, 0)
        )
    direct_discovery = tool in {"find", "grep", "rg", "ls"}
    if direct_discovery:
        command = " ".join(str(value) for value in args.values())
    elif tool == "bash" and isinstance(args.get("command"), str):
        command = args["command"]
    else:
        return None, 0
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return "malformed_inspection_command", 1
    separators = {";", "&&", "||", "|", "&"}
    commands: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in separators:
            if current:
                commands.append(current)
                current = []
        else:
            current.append(token)
    if current:
        commands.append(current)
    if direct_discovery:
        commands = [[str(tool), *tokens]]
    discovery_commands = [
        argv for argv in commands if argv and os.path.basename(argv[0]) in {"find", "grep", "rg", "ls"}
    ]
    discovery = len(discovery_commands)
    if not discovery:
        return None, 0
    root = scope.root.rstrip("/")
    relevant_commands = discovery_commands + [
        argv for argv in commands if argv and os.path.basename(argv[0]) == "cd"
    ]
    def path_operands(argv: list[str]) -> list[str]:
        """Extract filesystem operands, excluding grep patterns/regex syntax."""
        name = os.path.basename(argv[0])
        args = argv[1:]
        if name == "find":
            return [next((token for token in args if not token.startswith("-")), ".")]
        if name in {"grep", "rg"}:
            # grep's first non-option operand is the pattern; paths follow it.
            # Options with separate values are skipped, and -- makes the split
            # explicit. This prevents regexes such as ``/foo|bar`` being treated
            # as paths while retaining fail-closed validation of real operands.
            option_values = {"-e", "--regexp", "-f", "--file", "-m", "--max-count"}
            positional: list[str] = []
            index = 0
            while index < len(args):
                token = args[index]
                if token == "--":
                    positional.extend(args[index + 1:])
                    break
                if token in option_values:
                    index += 2
                    continue
                if token.startswith("-"):
                    index += 1
                    continue
                positional.append(token)
                index += 1
            return positional[1:] if positional else []
        return [token for token in args if token != "--" and not token.startswith("-")]

    for argv in relevant_commands:
        if direct_discovery and isinstance(args, dict):
            # Structured Pi tools identify path fields; never reinterpret a
            # pattern/regex field as a filesystem operand.
            direct_paths = [args[key] for key in ("path", "paths", "directory")
                            if isinstance(args.get(key), str)]
            tokens_to_validate = direct_paths
        else:
            tokens_to_validate = path_operands(argv) if argv and os.path.basename(argv[0]) in {
            "find", "grep", "rg", "ls"
            } else argv[1:]
        for token in tokens_to_validate:
            if token == ".." or token.startswith("../"):
                return "inspection_parent_escape", discovery
            if token.startswith("/") and token != root and not token.startswith(root + "/"):
                return "inspection_path_outside_worktree", discovery
    # Bash starts in /work. Relative discovery remains scoped unless an explicit
    # cd escapes it; those cd arguments are validated above. Inspecting only the
    # discovery command's argv prevents sed/awk regexes elsewhere in a compound
    # command from being mistaken for filesystem paths.
    if tool == "bash" and not discovery_commands:
        return None, 0
    return None, discovery


def pi_launch_spec(
    adapter: PiAdapter,
    *,
    prompt: str,
    worktree: str,
    model: str | None = None,
    card_size: CardSize | None = None,
    phase_budget: PhaseBudget | None = None,
) -> LaunchSpec:
    """Build the same pinned Pi argv/config/profile contract used by PiAdapter."""
    if phase_budget is not None and card_size is None:
        raise ValueError("an explicit phase budget requires card_size")
    if card_size is not None:
        budget = phase_budget or DEFAULT_PHASE_BUDGETS[card_size]
        prompt = f"{budget.prompt_contract()}\n\n{prompt}"
    return LaunchSpec(
        name="pi",
        argv=adapter._argv(prompt, model=model),
        image=adapter._image(),
        worktree=worktree,
        auth_mounts=adapter._auth_mounts(),
        auth_env=adapter._auth_env(),
        egress_hosts=list(adapter.egress_hosts),
        config_files=adapter._config_files(model=model),
        stdin=adapter._stdin_for(prompt),
        required_commands=adapter._required_commands(),
        required_checks=adapter._required_checks(),
        inspection_scope=(
            InspectionScope(max_calls={CardSize.SMALL: 24, CardSize.MEDIUM: 48,
                                       CardSize.LARGE: 80}.get(card_size, 24))
            if adapter.capability_profile in {"arena-build", "arena-verify"}
            else None
        ),
    )


class PiExperimentRunner:
    def __init__(
        self,
        controller: ArenaController,
        supervisor: AttemptSupervisor,
        artifact_root: str | Path,
        *,
        activity_journal: ActivityJournal | None = None,
    ) -> None:
        self.controller = controller
        self.supervisor = supervisor
        self.artifact_root = Path(artifact_root)
        self.activity_journal = activity_journal
        self.artifact_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_activity_id(value: object, prefix: str) -> str:
        candidate = str(value)
        try:
            ActivityContext(session_id=candidate)
        except ValueError:
            return f"{prefix}-{hashlib.sha256(candidate.encode()).hexdigest()}"
        return candidate

    def _default_activity_context(self, request: AttemptRequest) -> ActivityContext:
        return ActivityContext(
            session_id=self._safe_activity_id(self.controller.session_id, "session"),
            run_id=self._safe_activity_id(request.experiment_id, "run"),
            agent_id=self._safe_activity_id(self.controller.actor, "agent"),
            role="worker",
            phase="run",
            source="arena",
            trajectory_id=self._safe_activity_id(self.controller.session_id, "trajectory"),
            attempt_id=self._safe_activity_id(request.attempt_id, "attempt"),
        )

    def _publish_activity(
        self,
        context: ActivityContext,
        kind: ActivityKind,
        summary: str,
        *,
        data: dict[str, Any] | None = None,
        artifact_refs: tuple[str, ...] = (),
    ) -> int:
        if self.activity_journal is None:
            return 0
        try:
            self.activity_journal.publish(
                context,
                kind,
                summary=summary,
                data=data,
                artifact_refs=artifact_refs,
            )
        except Exception:  # noqa: BLE001 - activity never decides worker state
            return 1
        return 0

    def _attempt_dir(self, experiment_id: str, attempt: int) -> Path:
        # Experiment identities are external structured data, never path syntax.
        safe = hashlib.sha256(experiment_id.encode()).hexdigest()
        return self.artifact_root / safe / str(attempt)

    def execute(
        self,
        request: AttemptRequest,
        spec: LaunchSpec,
        *,
        attempt: int = 1,
        timeout_s: float = 1800,
        card_size: CardSize = CardSize.MEDIUM,
        requested_model: str | None = None,
        phase_budget: PhaseBudget | None = None,
        activity_context: ActivityContext | None = None,
    ) -> RunOutcome | Admission:
        context = activity_context or self._default_activity_context(request)
        activity_errors = 0
        admission = self.controller.admit(request, attempt_number=attempt)
        if not admission.admitted or admission.duplicate:
            activity_errors += self._publish_activity(
                context,
                ActivityKind.DISPOSITION,
                "worker admission refused",
                data={
                    "admitted": admission.admitted,
                    "duplicate": admission.duplicate,
                    "reason": admission.reason.value if admission.reason else None,
                },
            )
            return admission
        if not context.lease_id and admission.lease is not None:
            context = replace(context, lease_id=admission.lease.lease_id)
        activity_errors += self._publish_activity(
            context,
            ActivityKind.STATUS,
            "worker admitted",
            data={"attempt": attempt, "lease_id": admission.lease.lease_id},
        )
        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()

        def _heartbeat() -> None:
            # Heartbeat at one third of the configured TTL. A fixed lower bound
            # can exceed short test/edge TTLs and allow the lease to expire before
            # its first renewal. The scheduler already requires a positive TTL;
            # cap only the *upper* interval so long production TTLs still receive
            # periodic liveness evidence.
            interval = min(10.0, self.controller.scheduler.lease_ttl_s / 3)
            while not heartbeat_stop.wait(interval):
                if not self.controller.heartbeat(request.experiment_id, attempt):
                    lease_lost.set()
                    self.supervisor.cancel()
                    return

        # Admission itself starts the TTL. Begin renewal before durable run-file
        # and RUNNING-event fsyncs, which can legitimately exceed a very short TTL
        # on a busy or slow disk.
        heartbeat = threading.Thread(target=_heartbeat, name="arena-lease-heartbeat", daemon=True)
        heartbeat.start()
        try:
            resources = request.resources
            spec = replace(
                spec,
                cpu_limit=resources.cpu if resources.cpu > 0 else None,
                memory_gb_limit=resources.ram_gb if resources.ram_gb > 0 else None,
                sandbox_run_id=admission.lease.lease_id,
            )
            directory = self._attempt_dir(request.experiment_id, attempt)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "run.json").write_text(
                json.dumps(
                    {
                        "experiment_id": request.experiment_id,
                        "attempt": attempt,
                        "lease_id": admission.lease.lease_id,
                        "state": "admitted",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            self.controller.running(request.experiment_id, attempt)
            activity_errors += self._publish_activity(
                context,
                ActivityKind.PHASE,
                "Pi worker running",
                data={"attempt": attempt},
            )
        except Exception:
            heartbeat_stop.set()
            heartbeat.join(timeout=1)
            self.controller.cancel(
                request.experiment_id,
                attempt,
                stop=self.supervisor.cancel,
                payload={"reason": "preparation_failed"},
            )
            raise
        budget = phase_budget or DEFAULT_PHASE_BUDGETS[card_size]
        timeout_s = min(timeout_s, budget.total_s)
        started_at = time.monotonic()
        tailer = (
            _PiActivityTailer(self.activity_journal, context, directory / "stdout.log")
            if self.activity_journal is not None
            else None
        )
        if tailer is not None:
            tailer.start()
        try:
            exit_code, classification = self.supervisor.run(spec, directory, timeout_s)
        except DockerSupervisorError as exc:
            classification, exit_code = "docker_supervisor_error", None
            with (directory / "stderr.log").open("ab") as stream:
                stream.write(f"\n{type(exc).__name__}: {exc}\n".encode())
        except Exception as exc:
            classification, exit_code = "supervisor_error", None
            with (directory / "stderr.log").open("ab") as stream:
                stream.write(f"\n{type(exc).__name__}: {exc}\n".encode())
        finally:
            if tailer is not None:
                tailer.stop_and_drain()
                activity_errors += tailer.errors
            heartbeat_stop.set()
            heartbeat.join(timeout=1)
        if lease_lost.is_set():
            classification = "lease_lost"
        duration_s = max(0.0, time.monotonic() - started_at)
        stdout_path = directory / "stdout.log"
        event_scan = self._event_scan(stdout_path)
        stdout_digest = self._capture(stdout_path, compact_events=True)
        stderr_digest = self._capture(directory / "stderr.log")
        terminal_error = self._pi_terminal_error(stdout_path)
        negative_disposition = self._pi_negative_disposition(stdout_path)
        scout_assessment, scout_findings = self._pi_scout_terminal(stdout_path)
        inspection_denial = self._inspection_denial(directory / "inspection-denial.json")
        inspection_observation = self._inspection_observation(
            directory / "inspection-observation.json"
        )
        served_model, served_model_reason = served_model_evidence(event_scan)
        time_to_first_edit_s = self._time_to_first_edit(stdout_path)
        event_usage = self._usage_metrics(stdout_path)
        requested_model = requested_model or self._requested_model(spec)
        terminal_event_observed = bool(assistant_message_events(event_scan)) or any(
            event.get("type") in {"agent_end", "agent_settled"}
            for event in event_scan.events
        )
        timeout_phase = None
        if classification == "timeout":
            timeout_phase = "inspect" if time_to_first_edit_s is None else "test"
        metrics: dict[str, object] = {
            "duration_s": round(duration_s, 3),
            "time_to_first_edit_s": time_to_first_edit_s,
            "timeout_phase": timeout_phase,
            "requested_model": requested_model,
            "served_model": served_model,
            "served_model_reason": (
                served_model_reason.value if served_model_reason is not None else None
            ),
            "pi_event_stream_complete": not event_scan.incomplete,
            "pi_terminal_event_observed": terminal_event_observed,
            "activity_publication_errors": activity_errors,
            "inspection_calls": inspection_observation.get("observed_calls"),
            "inspection_calls_remaining": inspection_observation.get("remaining_calls"),
            "inspection_denial_reason": inspection_observation.get("denial_reason"),
            "inspection_stream_status": inspection_observation.get("stream_status"),
            "card_size": card_size.value,
            "phase_budget_s": {
                "assess": budget.assess_s,
                "inspect": budget.inspect_s,
                "build": budget.build_s,
                "test": budget.test_s,
            },
            **event_usage,
        }
        if exit_code == 0 and classification == "exit" and event_scan.incomplete:
            # Best-effort reply parsing must never make a malformed, truncated,
            # unknown, or plain-text Pi stream trustworthy. In particular, an
            # earlier actionable scout message cannot authorize a downstream phase.
            classification = "pi_event_stream_incomplete"
        elif exit_code == 0 and classification == "exit" and not event_scan.events:
            classification = "pi_event_stream_missing"
        elif exit_code == 0 and classification == "exit" and not terminal_event_observed:
            classification = "pi_terminal_event_missing"
        elif exit_code == 0 and classification == "exit" and terminal_error is not None:
            # Pi can report a provider/parser failure in its structured event
            # stream and still exit zero. A zero shell status alone is not success.
            classification = "pi_terminal_error"
        if exit_code == 0 and classification == "exit" and negative_disposition is not None:
            classification = negative_disposition.value
        if exit_code not in (None, 0) and classification == "exit":
            stderr_path = directory / "stderr.log"
            stderr = (
                stderr_path.read_text(encoding="utf-8", errors="replace").lower()
                if stderr_path.exists()
                else ""
            )
            gateway_markers = (
                "connection refused",
                "gateway unavailable",
                "failed to connect",
                "connection reset",
            )
            if any(marker in stderr for marker in gateway_markers):
                classification = "gateway_outage"
        successful = exit_code == 0 and classification == "exit"
        cancelled = (
            self.controller.state(request.experiment_id, attempt) is ExperimentState.CANCELLED
        )
        if cancelled:
            successful = False
            classification = "cancelled"
        activity_errors += self._publish_activity(
            context,
            ActivityKind.DISPOSITION if successful else ActivityKind.ERROR,
            "Pi worker completed" if successful else f"Pi worker failed: {classification}",
            data={
                "classification": classification,
                "exit_code": exit_code,
                "successful": successful,
                "inspection_calls": metrics["inspection_calls"],
                "inspection_calls_remaining": metrics["inspection_calls_remaining"],
                "inspection_denial_reason": metrics["inspection_denial_reason"],
                "inspection_stream_status": metrics["inspection_stream_status"],
            },
            artifact_refs=(stdout_digest, stderr_digest),
        )
        metrics["activity_publication_errors"] = activity_errors
        payload = {
            "classification": classification,
            "reason": classification if not successful else None,
            "exit_code": exit_code,
            "stdout_digest": stdout_digest,
            "stderr_digest": stderr_digest,
            "partial": not successful,
            "metrics": metrics,
            # Retain controller-owned lineage in the durable attempt record even
            # after the bounded live activity window has trimmed older events.
            "activity_context": asdict(context),
        }
        if terminal_error is not None:
            payload["terminal_error"] = terminal_error
        if negative_disposition is not None:
            payload["disposition"] = negative_disposition.value
        if scout_assessment is not None:
            payload["scout_assessment"] = scout_assessment
            payload["scout_findings"] = scout_findings
        if inspection_denial is not None:
            payload["inspection_denial"] = inspection_denial
        if not cancelled:
            self.controller.finish_run(
                request.experiment_id, attempt, successful=successful, payload=payload
            )
        (directory / "run.json").write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )
        return RunOutcome(
            successful,
            classification,
            exit_code,
            stdout_digest,
            stderr_digest,
            partial=not successful,
            metrics=metrics,
            disposition=negative_disposition.value if negative_disposition is not None else None,
            scout_assessment=scout_assessment,
            scout_findings=scout_findings,
        )

    @staticmethod
    def _event_scan(path: Path) -> PiEventScan:
        if not path.exists():
            return PiEventScan((), False)
        return scan_pi_events(path.read_bytes())

    @classmethod
    def _events(cls, path: Path):
        yield from cls._event_scan(path).events

    @classmethod
    def _served_model(cls, path: Path) -> str | None:
        """Compatibility view returning only complete, agreeing model evidence."""

        return served_model_evidence(cls._event_scan(path))[0]

    @staticmethod
    def _requested_model(spec: LaunchSpec) -> str | None:
        """Read the generated provider config, never model-authored output."""
        raw = spec.config_files.get("/agent/models.json")
        if not raw:
            return None
        try:
            providers = json.loads(raw).get("providers", {})
        except (json.JSONDecodeError, AttributeError, TypeError):
            return None
        for provider in providers.values() if isinstance(providers, dict) else ():
            models = provider.get("models") if isinstance(provider, dict) else None
            if isinstance(models, list) and models and isinstance(models[0], dict):
                model = models[0].get("id")
                if isinstance(model, str) and model.strip():
                    return model.strip()
        return None

    @staticmethod
    def _inspection_denial(path: Path) -> dict[str, object] | None:
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8")[:4096])
        except (json.JSONDecodeError, OSError):
            return {"type": "inspection_denial", "reason": "invalid_denial_evidence"}
        return value if isinstance(value, dict) else None

    @staticmethod
    def _inspection_observation(path: Path) -> dict[str, object]:
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8")[:4096])
        except (json.JSONDecodeError, OSError):
            return {}
        return value if isinstance(value, dict) else {}

    @classmethod
    def _time_to_first_edit(cls, path: Path) -> float | None:
        """Read Pi's relative event time for its first mutating tool call, if supplied."""
        mutators = ("edit", "write", "apply_patch", "create_file")
        for event in cls._events(path):
            encoded = json.dumps(event, sort_keys=True).lower()
            if not any(f'"{name}"' in encoded for name in mutators):
                continue
            for key in ("elapsed_s", "elapsed", "time_s"):
                value = event.get(key)
                if isinstance(value, (int, float)) and value >= 0:
                    return round(float(value), 3)
            return None
        return None

    @classmethod
    def _usage_metrics(cls, path: Path) -> dict[str, int | float]:
        tool_calls = 0
        tokens = 0
        cost = 0.0
        for event in cls._events(path):
            if event.get("type") == "tool_execution_start":
                tool_calls += 1
            if event.get("type") != "message_end":
                continue
            message = event.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(usage, dict):
                continue
            total = usage.get("totalTokens")
            if isinstance(total, int) and total >= 0:
                tokens += total
            costs = usage.get("cost")
            total_cost = costs.get("total") if isinstance(costs, dict) else None
            if isinstance(total_cost, (int, float)) and total_cost >= 0:
                cost += float(total_cost)
        return {"tool_calls": tool_calls, "tokens": tokens, "cost": round(cost, 8)}

    @staticmethod
    def _pi_terminal_error(path: Path) -> str | None:
        """Return Pi's last structured terminal error, bounded for event metadata."""
        if not path.exists():
            return None
        error = None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(event, dict) or event.get("type") != "message_end":
                continue
            message = event.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            if message.get("stopReason") != "error":
                continue
            detail = message.get("errorMessage")
            error = detail.strip() if isinstance(detail, str) and detail.strip() else "pi error"
        return error[:2000] if error is not None else None

    @staticmethod
    def _pi_negative_disposition(path: Path) -> TaskDisposition | None:
        """Accept model text only as a one-way, fail-safe negative signal.

        A worker can never claim completion through this seam. An explicit blocked,
        needs-input, or failed status can only reduce trust, and must still be
        independently verified before board mutation.
        """
        if not path.exists():
            return None
        statuses = {
            "blocked": TaskDisposition.BLOCKED,
            "needs_input": TaskDisposition.NEEDS_INPUT,
            "needs input": TaskDisposition.NEEDS_INPUT,
            "failed": TaskDisposition.FAILED,
        }
        for raw in reversed(path.read_bytes().splitlines()):
            try:
                envelope = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            message = envelope.get("message") if isinstance(envelope, dict) else None
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            blocks = content if isinstance(content, list) else []
            text = "\n".join(
                block.get("text", "")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            ).casefold()
            for label, disposition in statuses.items():
                if f"status: {label}" in text or f"status: **{label}" in text:
                    return disposition
            return None
        return None

    @staticmethod
    def _pi_scout_terminal(
        path: Path,
    ) -> tuple[str | None, tuple[dict[str, object], ...]]:
        """Parse a strict final scout assessment and path-scoped finding evidence.

        This is deliberately not a completion parser. It recognizes one narrow
        controller-requested terminal form. Actionable/no-action assessments are
        accepted only with concrete repository-relative findings retained in the
        raw trajectory; generic claims and placeholder text fail closed.
        """

        final_text: str | None = None
        for event in PiExperimentRunner._events(path) or ():
            if event.get("type") != "message_end":
                continue
            message = event.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            blocks = content if isinstance(content, list) else []
            if not blocks or any(
                not isinstance(block, dict)
                or block.get("type") != "text"
                or not isinstance(block.get("text"), str)
                for block in blocks
            ):
                final_text = None
                continue
            final_text = "\n".join(str(block["text"]) for block in blocks)
        if (
            final_text is None
            or not final_text
            or final_text != final_text.strip()
            or "\r" in final_text
        ):
            return None, ()
        lines = final_text.split("\n")
        # Pi commonly explains that it is about to emit the controller
        # disposition, then places the exact contract block on the following
        # lines.  Treat that prose as untrusted commentary: only a single
        # canonical block, anchored at the end of the message, is parsed.
        headings = [
            index
            for index, line in enumerate(lines)
            if re.fullmatch(
                r"SCOUT_ASSESSMENT: (ACTIONABLE|NO_ACTION|BLOCKED|NEEDS_INPUT)",
                line,
            )
        ]
        if len(headings) != 1:
            return None, ()
        lines = lines[headings[0] :]
        assessment_match = re.fullmatch(
            r"SCOUT_ASSESSMENT: (ACTIONABLE|NO_ACTION|BLOCKED|NEEDS_INPUT)",
            lines[0],
        )
        if assessment_match is None:
            return None, ()
        assessment = assessment_match.group(1).lower()
        if assessment in {"blocked", "needs_input"}:
            return (assessment, ()) if len(lines) == 1 else (None, ())
        if len(lines) < 2:
            return None, ()

        findings: list[dict[str, object]] = []
        for line in lines[1:]:
            match = re.fullmatch(
                r"SCOUT_FINDING: "
                r"([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)"
                r"(?::([1-9][0-9]*))? - (.{12,500})",
                line,
            )
            if match is None:
                return None, ()
            raw_path = match.group(1)
            raw_detail = match.group(3)
            if raw_path != raw_path.strip() or raw_detail != raw_detail.strip():
                return None, ()
            try:
                finding = ScoutFinding.create(
                    path=raw_path,
                    line=int(match.group(2)) if match.group(2) is not None else None,
                    detail=raw_detail,
                )
            except (TypeError, ValueError):
                # Worker text is untrusted observation data. A syntactically
                # plausible line that fails the typed contract is invalid output,
                # never an exception that escapes controller terminalization.
                return None, ()
            findings.append(finding.model_dump(mode="json"))
        if not findings:
            return None, ()
        unique = {str(item["digest"]): item for item in findings}
        return assessment, tuple(unique[key] for key in sorted(unique))

    def cancel(self, experiment_id: str, attempt: int = 1) -> None:
        self.controller.cancel(
            experiment_id,
            attempt,
            stop=self.supervisor.cancel,
            payload={"reason": "operator_cancelled"},
        )

    def recover_incomplete(self) -> list[str]:
        """On restart, terminalize admitted/running attempts and retain partial logs."""
        recovered = []
        latest: dict[tuple[str, int], ExperimentState] = {}
        for event in self.controller.store.read_all_events():
            latest[(event.experiment_id, event.attempt)] = event.to_state
        for (experiment_id, attempt), state in sorted(latest.items()):
            if state not in {ExperimentState.ADMITTED, ExperimentState.RUNNING}:
                continue
            directory = self._attempt_dir(experiment_id, attempt)
            payload = {
                "reason": "controller_restart",
                "classification": "restart_recovery",
                "stdout_digest": self._capture(directory / "stdout.log"),
                "stderr_digest": self._capture(directory / "stderr.log"),
                "partial": True,
            }
            self.controller._append(
                experiment_id, attempt, ExperimentState.FAILED, payload=payload
            )
            recovered.append(experiment_id)
        return recovered

    def _capture(self, path: Path, *, compact_events: bool = False) -> str:
        content = path.read_bytes() if path.exists() else b""
        if compact_events:
            content = compact_pi_events(content)
        return self.controller.store.put_artifact(content)


def build_production_pi_runner(
    controller: ArenaController,
    *,
    artifact_root: str | Path,
    docker: str = "docker",
    activity_journal: ActivityJournal | None = None,
) -> PiExperimentRunner:
    """Production composition is always real Sandbox+Docker, never FakeSpawner."""
    activity_journal = activity_journal or ActivityJournal()
    sandbox = Sandbox(live_execution=True, docker=docker)

    def active_run_ids() -> Iterable[str]:
        return (
            str(record["lease_id"])
            for record in controller.scheduler.lease_records()
        )

    supervisor = SandboxProcessSupervisor(sandbox, active_run_ids=active_run_ids)
    return PiExperimentRunner(
        controller,
        supervisor,
        artifact_root,
        activity_journal=activity_journal,
    )
