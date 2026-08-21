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
import secrets
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from skharness.autocode.sandbox import AuthMount, InspectionScope, LaunchSpec, Sandbox

from .controller import ArenaController
from .models import ExperimentState
from .scheduler import Admission, AttemptRequest
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


class TaskDisposition(str, Enum):
    """Task-level state, deliberately separate from process termination."""

    COMPLETED = "completed"
    BLOCKED = "blocked"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"


class AttemptSupervisor(Protocol):
    def run(self, spec: LaunchSpec, attempt_dir: Path, timeout_s: float) -> tuple[int, str]: ...
    def cancel(self) -> None: ...


class SandboxProcessSupervisor:
    """Cancellable real Docker supervisor; never a FakeSpawner adaptation."""

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        repo_remote_host: str | None = None,
        ci_host: str | None = None,
        shutdown_grace_s: float = 10.0,
    ) -> None:
        if not sandbox.live_execution:
            raise ValueError("production arena supervisor requires Sandbox(live_execution=True)")
        self.sandbox = sandbox
        self.repo_remote_host = repo_remote_host
        self.ci_host = ci_host
        if shutdown_grace_s <= 0:
            raise ValueError("shutdown_grace_s must be positive")
        self.shutdown_grace_s = shutdown_grace_s
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._container_name: str | None = None

    @staticmethod
    def _run_checked(argv: list[str]) -> None:
        subprocess.run(argv, capture_output=True, text=True, check=True)

    def run(self, spec: LaunchSpec, attempt_dir: Path, timeout_s: float) -> tuple[int, str]:
        self.sandbox._ensure_capable(spec)
        token = secrets.token_hex(6)
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
        try:
            if spec.config_files:
                config_dir = tempfile.mkdtemp(prefix="arena-pi-config-")
                for index, (destination, content) in enumerate(spec.config_files.items()):
                    source = Path(config_dir) / f"config-{index}"
                    source.write_text(content, encoding="utf-8")
                    config_mounts.append(AuthMount(str(source), destination, ro=True))
            self._run_checked([self.sandbox.docker, "network", "create", "--internal", network])
            self._run_checked(
                self.sandbox._proxy_run_argv(
                    name=proxy_name, network=network, alias=proxy_alias, allow=allow
                )
            )
            self._run_checked([self.sandbox.docker, "network", "connect", "bridge", proxy_name])
            argv = self.sandbox._docker_run_argv(
                spec,
                network,
                proxy_alias,
                container_name=container_name,
                extra_mounts=config_mounts,
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
                monitor = None
                if spec.inspection_scope is not None:
                    monitor = threading.Thread(
                        target=self._monitor_inspection,
                        args=(attempt_dir / "stdout.log", spec.inspection_scope,
                              process, inspection_denial, inspection_detail),
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
                    self.cancel()
                    exit_code = process.wait(timeout=10)
                if monitor is not None:
                    monitor.join(timeout=1)
                if inspection_denial.is_set():
                    classification = "inspection_denied"
                    (attempt_dir / "inspection-denial.json").write_text(
                        json.dumps(inspection_detail, sort_keys=True) + "\n", encoding="utf-8"
                    )
            if classification != "timeout" and self._oom_killed(container_name):
                classification = "oom"
            return exit_code, classification
        finally:
            with self._lock:
                self._process = None
                self._container_name = None
            subprocess.run(
                [self.sandbox.docker, "rm", "-f", container_name], capture_output=True, text=True
            )
            subprocess.run(
                [self.sandbox.docker, "rm", "-f", proxy_name], capture_output=True, text=True
            )
            subprocess.run(
                [self.sandbox.docker, "network", "rm", network], capture_output=True, text=True
            )
            if config_dir:
                shutil.rmtree(config_dir, ignore_errors=True)

    def _monitor_inspection(
        self,
        path: Path,
        scope: InspectionScope,
        process: subprocess.Popen,
        denied: threading.Event,
        detail: dict[str, object],
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
                if violation is None and calls <= scope.max_calls:
                    continue
                reason = violation or "inspection_call_budget_exceeded"
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
                self.cancel()
                return
            time.sleep(0.02)


    def _oom_killed(self, container_name: str) -> bool:
        result = subprocess.run(
            [self.sandbox.docker, "inspect", "--format", "{{.State.OOMKilled}}", container_name],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == "true"

    def cancel(self) -> None:
        with self._lock:
            process = self._process
            container = self._container_name
        if container:
            subprocess.run(
                [self.sandbox.docker, "rm", "-f", container], capture_output=True, text=True
            )
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                process.wait(timeout=self.shutdown_grace_s)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


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
    for argv in relevant_commands:
        for token in argv[1:]:
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
        self, controller: ArenaController, supervisor: AttemptSupervisor, artifact_root: str | Path
    ) -> None:
        self.controller = controller
        self.supervisor = supervisor
        self.artifact_root = Path(artifact_root)
        self.artifact_root.mkdir(parents=True, exist_ok=True)

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
    ) -> RunOutcome | Admission:
        admission = self.controller.admit(request, attempt_number=attempt)
        if not admission.admitted or admission.duplicate:
            return admission
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
        try:
            exit_code, classification = self.supervisor.run(spec, directory, timeout_s)
        except Exception as exc:
            classification, exit_code = "supervisor_error", None
            with (directory / "stderr.log").open("ab") as stream:
                stream.write(f"\n{type(exc).__name__}: {exc}\n".encode())
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1)
        if lease_lost.is_set():
            classification = "lease_lost"
        duration_s = max(0.0, time.monotonic() - started_at)
        stdout_path = directory / "stdout.log"
        stdout_digest = self._capture(stdout_path, compact_events=True)
        stderr_digest = self._capture(directory / "stderr.log")
        terminal_error = self._pi_terminal_error(stdout_path)
        negative_disposition = self._pi_negative_disposition(stdout_path)
        inspection_denial = self._inspection_denial(directory / "inspection-denial.json")
        served_model = self._served_model(stdout_path)
        time_to_first_edit_s = self._time_to_first_edit(stdout_path)
        requested_model = requested_model or self._requested_model(spec)
        timeout_phase = None
        if classification == "timeout":
            timeout_phase = "inspect" if time_to_first_edit_s is None else "test"
        metrics: dict[str, object] = {
            "duration_s": round(duration_s, 3),
            "time_to_first_edit_s": time_to_first_edit_s,
            "timeout_phase": timeout_phase,
            "requested_model": requested_model,
            "served_model": served_model,
            "card_size": card_size.value,
            "phase_budget_s": {
                "assess": budget.assess_s,
                "inspect": budget.inspect_s,
                "build": budget.build_s,
                "test": budget.test_s,
            },
        }
        if exit_code == 0 and classification == "exit" and terminal_error is not None:
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
        payload = {
            "classification": classification,
            "reason": classification if not successful else None,
            "exit_code": exit_code,
            "stdout_digest": stdout_digest,
            "stderr_digest": stderr_digest,
            "partial": not successful,
            "metrics": metrics,
        }
        if terminal_error is not None:
            payload["terminal_error"] = terminal_error
        if negative_disposition is not None:
            payload["disposition"] = negative_disposition.value
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
        )

    @staticmethod
    def _events(path: Path):
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(event, dict):
                yield event

    @classmethod
    def _served_model(cls, path: Path) -> str | None:
        served = None
        for event in cls._events(path):
            candidate = event.get("responseModel")
            message = event.get("message")
            if isinstance(message, dict):
                candidate = message.get("responseModel", candidate)
            if isinstance(candidate, str) and candidate.strip():
                served = candidate.strip()
        return served

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
) -> PiExperimentRunner:
    """Production composition is always real Sandbox+Docker, never FakeSpawner."""
    sandbox = Sandbox(live_execution=True, docker=docker)
    return PiExperimentRunner(controller, SandboxProcessSupervisor(sandbox), artifact_root)
