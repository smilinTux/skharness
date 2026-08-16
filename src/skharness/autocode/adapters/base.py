"""BaseCliAdapter: the shared harness seam. Holds a Sandbox and composes the
assess/run_task/grade prompts (framing untrusted text as data), building a
LaunchSpec the sandbox runs. Concrete adapters supply only what varies."""
from __future__ import annotations

import json
import os
import subprocess

from skharness.harness import Harness

from .. import health
from ..buckets import dispatch_model_of, validate_bucket
from ..claude_code import frame
from ..sandbox import LaunchSpec
from ..types import (AssessBrief, GateResult, GradeBrief, HarnessResult,
                     TaskBrief, Verdict)

#: The verdicts assess may legitimately return; anything else is "inconclusive".
_ASSESS_VERDICTS = ("valid", "stale", "obsolete", "needs_decision", "decompose")


class ModelOverrideUnsupported(RuntimeError):
    """A per-call model override was requested of an adapter that cannot honour it.

    Raised instead of dropping the override: dropping it would run the call on the
    statically configured model, discarding the requested routing (and with it the
    card's sensitivity ceiling) with no visible signal at all."""


def _record_assess_inconclusive(brief: AssessBrief, out: dict) -> None:
    """Telemetry: the CLI gave no parseable verdict for an assess (declined or
    errored through all retries). Fed to the learning layer so the assess decline
    rate is observable and the retry budget can adapt (see BaseCliAdapter._run)."""
    health.record("assess_inconclusive", task=getattr(brief, "task_id", None),
                  had_error=bool(isinstance(out, dict) and out.get("is_error")))


def extract_json(text) -> dict | None:
    """Best-effort: pull a JSON object out of a model text reply, tolerating
    surrounding prose or a ```json fence. Claude Code returns the model reply as a
    string in `result`; a strict-JSON instruction usually yields clean JSON but may
    be fenced or prefixed. Returns None when no JSON object is found."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s[3:]
        if s[:4].lower() == "json":
            s = s[4:]
        end = s.rfind("```")
        if end != -1:
            s = s[:end]
        s = s.strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    start = s.find("{")               # fall back to the first balanced {...}
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except (json.JSONDecodeError, TypeError):
                    return None
    return None


def _event_text(ev: dict) -> str | None:
    """Pull assistant text out of one event, tolerating the two real shapes:
    opencode `--format json`: {"type":"text","part":{"type":"text","text":...}}
    pi `--mode json`: {"type":"message_end","message":{"role":"assistant",
                        "content":[{"type":"text","text":...}]}}
    """
    part = ev.get("part")
    if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
        return part["text"]
    if ev.get("type") == "text" and isinstance(ev.get("text"), str):
        return ev["text"]
    msg = ev.get("message")                       # pi: assistant message with content list
    if isinstance(msg, dict) and msg.get("role") == "assistant":
        chunks = [c.get("text") for c in (msg.get("content") or [])
                  if isinstance(c, dict) and c.get("type") == "text" and c.get("text")]
        if chunks:
            return "".join(chunks)
    return None


def parse_event_stream(body: str) -> dict:
    """Extract the model's JSON reply from a newline-delimited JSON event stream
    (opencode `--format json`, pi `--mode json`). Takes the FIRST event that carries
    assistant text decoding to a JSON object, not the last: opencode's first
    assistant text chunk is the model's direct reply, and it then agentic-loops
    with further (non-JSON, rambling) chunks that must not clobber the real answer.
    pi emits its reply once, in the assistant message_end event, so first-valid-JSON
    is unchanged for pi. Returns {} when no decodable reply is present. Grounded in
    captured opencode + pi samples."""
    for line in (body or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(ev, dict):
            continue
        text = _event_text(ev)
        if not text:
            continue
        obj = extract_json(text)
        if obj is not None:
            return obj
    return {}


class BaseCliAdapter(Harness):
    """Task-plane adapter over the shared Docker sandbox. Subclasses the unified
    Harness contract: it implements the task plane (assess/run_task/grade below)
    and leaves the session plane at Harness's gated default. Concrete adapters
    declare merged capabilities (task_plane=True, session_plane=False)."""
    name = "base"

    def __init__(self, sandbox, egress_hosts=None, live_execution: bool = False):
        self.sandbox = sandbox
        self.egress_hosts = list(egress_hosts or [])
        self.live_execution = live_execution

    # -- hooks each concrete adapter provides --
    # `model` is the per-call model override (widening the existing `light` seam:
    # same shape, threaded down the same path). It is passed ONLY when non-None AND
    # the adapter declares supports_model_override(), so an adapter that has not
    # implemented it keeps its narrow (prompt, light) / () signatures untouched.
    def _argv(self, prompt: str, light: bool = False,
              model: str | None = None) -> list[str]: raise NotImplementedError
    def _image(self) -> str: raise NotImplementedError
    def _auth_mounts(self) -> list: raise NotImplementedError
    def _auth_env(self) -> dict: raise NotImplementedError
    def _config_files(self, model: str | None = None) -> dict: return {}

    def supports_model_override(self) -> bool:
        """True only when this adapter honours a per-call model id in BOTH _argv and
        _config_files. Default False, and _run_raw REFUSES a per-call override on a
        False adapter rather than dropping it. Silently ignoring the override is the
        dangerous outcome: the call would run on the statically configured model with
        the card's sensitivity ceiling quietly discarded, which is exactly the class
        of failure this seam exists to prevent. Fail closed and loudly instead."""
        return False
    def _stdin_for(self, prompt: str) -> str | None: return None
    def _parse(self, raw: dict) -> dict: raise NotImplementedError
    def capabilities(self): raise NotImplementedError

    # -- egress host derivation --
    def _remote_host(self, repo):
        if repo is None:
            return None
        try:
            r = subprocess.run(["git", "-C", repo.path, "remote", "get-url", "origin"],
                               capture_output=True, text=True)
        except OSError:
            return None
        url = (r.stdout or "").strip()
        if not url:
            return None
        if url.startswith("git@"):
            return url.split("@", 1)[1].split(":", 1)[0]
        if "://" in url:
            return url.split("://", 1)[1].split("@")[-1].split("/", 1)[0].split(":", 1)[0]
        return None

    def _ci_host(self, repo):
        if repo is None:
            return None
        if str(getattr(repo, "ci", "none")).startswith("github"):
            return "api.github.com"
        return None

    # -- shared spawn helpers --
    def _run_raw(self, instruction: str, data: str, *, worktree: str, repo,
                 light: bool = False, model: str | None = None) -> dict:
        prompt = frame(instruction, data)
        image = getattr(repo, "sandbox_image", None) or self._image()
        # One kwargs dict feeds BOTH hooks, so _argv and _config_files can never be
        # handed different model ids. They must agree: _config_files DECLARES the
        # model to the CLI and _argv REQUESTS it, so a disagreement means the CLI
        # asks for a model it never declared.
        mkw: dict = {}
        if model is not None:
            if not self.supports_model_override():
                raise ModelOverrideUnsupported(
                    f"{self.name} adapter cannot honour a per-call model override "
                    f"({model!r}); refusing rather than silently running the static "
                    "model without the requested routing.")
            validate_bucket(model)          # never emit an unvalidated bucket id
            mkw["model"] = model
        spec = LaunchSpec(name=self.name, argv=self._argv(prompt, light=light, **mkw),
                          image=image,
                          worktree=worktree, auth_mounts=self._auth_mounts(),
                          auth_env=self._auth_env(), egress_hosts=self.egress_hosts,
                          config_files=self._config_files(**mkw),
                          stdin=self._stdin_for(prompt))
        return self.sandbox.spawn(spec, repo_remote_host=self._remote_host(repo),
                                  ci_host=self._ci_host(repo))

    # A judgment call (assess/grade) must not turn a transient hiccup into a
    # wrong answer. The sandboxed CLI intermittently returns a hard API error
    # (rate limit, socket) or a prose-only reply with no extractable JSON; either
    # yields an empty parse that the callers default to needs_decision / no-score.
    # Retry a bounded number of times before giving up, so one bad roll does not
    # silently escalate a valid task.
    _RUN_ATTEMPTS = 3
    #: Ceiling the adaptive budget will not exceed (cost/latency guard).
    _RUN_ATTEMPTS_MAX = 6

    def _run_attempts(self) -> int:
        """Self-tuning retry budget: the base attempts, raised toward the ceiling
        when the recent decline rate is high, so the harness spends more retries
        exactly when the CLI is being flaky and no more when it is healthy. This
        is the learning loop reading its own health telemetry."""
        base = self._RUN_ATTEMPTS
        try:
            decline = health.rate("run_inconclusive",
                                  over=("run_inconclusive", "run_ok"))
        except Exception:              # noqa: BLE001 - telemetry never gates the run
            return base
        if decline >= 0.5:
            return self._RUN_ATTEMPTS_MAX
        if decline >= 0.25:
            return min(self._RUN_ATTEMPTS_MAX, base + 2)
        return base

    def _run(self, instruction: str, data: str, *, worktree: str, repo,
             light: bool = False, model: str | None = None) -> dict:
        parsed: dict = {}
        attempts = self._run_attempts()
        for i in range(attempts):
            raw = self._run_raw(instruction, data, worktree=worktree, repo=repo,
                                light=light, model=model)
            if not (isinstance(raw, dict) and raw.get("is_error")):
                parsed = self._parse(raw)
                if parsed:                      # non-empty parse == usable answer
                    if i:
                        health.record("run_retry_recovered", attempt=i + 1)
                    health.record("run_ok")
                    return parsed
            # hard API error, or empty/unparseable reply: try again
        health.record("run_inconclusive", attempts=attempts,
                      name=getattr(self, "name", "?"))
        return parsed

    # -- the three seam methods (prompts copied verbatim from ClaudeCodeAdapter) --
    def assess(self, brief: AssessBrief) -> Verdict:
        instruction = (
            "Assess whether a coord task is still valid work, judging ONLY from its "
            "title, description, and acceptance criteria. You do NOT have repo access "
            "in this step; the implementer is given the repo downstream, so do NOT "
            "return needs_decision merely because you cannot see the repo or its "
            "files. verdict=valid if it is coherent, actionable work; stale if the "
            "description is outdated but you can rewrite it (give updated_description "
            "and updated_acceptance); obsolete if clearly no longer needed; "
            "decompose if it is coherent and wanted but too COARSE to implement as one "
            "diff (it names several distinct artifacts, or the acceptance is "
            "skeleton/scaffold/framework/epic-shaped); needs_decision ONLY if the task "
            "itself is ambiguous or self-contradictory. When REPO FACTS are provided, "
            "treat them as authoritative: prefer decompose over valid when the "
            "acceptance names nothing that resolves in the facts and is not clearly "
            "greenfield. Reply strictly as JSON: {\"verdict\":\"valid|stale|obsolete|"
            "needs_decision|decompose\",\"reason\":\"...\"}.")
        data = json.dumps({"task_id": brief.task_id, "title": brief.title,
                           "description": brief.description,
                           "acceptance": brief.acceptance, "tags": brief.tags,
                           "codebase_context": brief.codebase_context})
        out = self._run(instruction, data, worktree=os.getcwd(), repo=None, light=True)
        verdict = out.get("verdict")
        if verdict not in _ASSESS_VERDICTS:
            # Inconclusive assess: even after _run's retries the CLI declined or
            # returned no parseable verdict. assess is a cheap PRE-filter; the twin
            # gate (score==5 AND CI green AND coverage) is the real safety net and
            # never merges bad code. So fail OPEN toward the gate -- proceed as
            # valid rather than block genuine work at a flaky pre-check. A task that
            # is actually bad still gets caught downstream (the agent produces junk,
            # the gate refuses, and it escalates then). Self-healing principle:
            # never let a non-answer at a cheap check strand work a strong check
            # protects. The inconclusive count is recorded so a learning layer can
            # watch the assess decline rate (see AssessHealth).
            _record_assess_inconclusive(brief, out)
            return Verdict(verdict="valid",
                           reason="assess inconclusive after retries; proceeding to "
                                  "the twin gate (fail-open)")
        if verdict == "needs_decision":
            # A single needs_decision may be a flaky HEDGE, not a real ambiguity:
            # the same well-formed task grades `valid` on most calls and
            # needs_decision on some (the newer CLI is cautious about the framed
            # prompt). Escalating a valid task to a human on one bad roll is the
            # failure we are killing. So CONFIRM with a second opinion; escalate
            # only when the confirmation ALSO says needs_decision. If it does not,
            # treat the task as valid and let the twin gate be the real arbiter --
            # same fail-open-toward-the-gate principle, applied to a wobbly verdict
            # rather than a missing one.
            confirm = self._run(instruction, data, worktree=os.getcwd(), repo=None, light=True)
            if confirm.get("verdict") != "needs_decision":
                health.record("assess_needs_decision_unconfirmed",
                              task=getattr(brief, "task_id", None),
                              confirm=confirm.get("verdict"))
                return Verdict(verdict="valid",
                               reason="needs_decision not confirmed on a second "
                                      "opinion; proceeding to the twin gate")
            health.record("assess_needs_decision_confirmed",
                          task=getattr(brief, "task_id", None))
        health.record("assess_ok", verdict=verdict, task=getattr(brief, "task_id", None))
        return Verdict(verdict=verdict,
                       reason=out.get("reason", ""),
                       updated_description=out.get("updated_description"),
                       updated_acceptance=out.get("updated_acceptance"))

    def decompose(self, brief: AssessBrief) -> list[dict]:
        """Split a too-coarse card into 2-8 independently buildable subtasks. Each
        subtask's acceptance must NAME real files/functions from the REPO FACTS in
        codebase_context. Cheap single-turn call reusing _run. Returns a list of
        {title, description, acceptance} dicts (empty on an inconclusive reply --
        the caller then queues a human decision, never a silent drop)."""
        instruction = (
            "This coord task is coherent and wanted but too coarse to build as one "
            "diff. Using the REPO FACTS in codebase_context as ground truth, split it "
            "into 2 to 8 INDEPENDENTLY buildable subtasks. Each subtask must be a small "
            "single-diff unit whose acceptance NAMES concrete files/functions (prefer "
            "ones from the facts). Do NOT restate the parent; produce real sub-steps. "
            "Reply strictly as JSON: {\"subtasks\":[{\"title\":\"...\",\"description\":"
            "\"...\",\"acceptance\":[\"...\"]}, ...]}.")
        data = json.dumps({"task_id": brief.task_id, "title": brief.title,
                           "description": brief.description,
                           "acceptance": brief.acceptance, "tags": brief.tags,
                           "codebase_context": brief.codebase_context})
        out = self._run(instruction, data, worktree=os.getcwd(), repo=None, light=True)
        subs = out.get("subtasks") if isinstance(out, dict) else None
        if not isinstance(subs, list):
            health.record("decompose_inconclusive", task=getattr(brief, "task_id", None))
            return []
        clean: list[dict] = []
        for s in subs:
            if isinstance(s, dict) and s.get("title"):
                acc = s.get("acceptance")
                clean.append({"title": str(s["title"]),
                              "description": str(s.get("description", "")),
                              "acceptance": acc if isinstance(acc, list) else (
                                  [str(acc)] if acc else [])})
        health.record("decompose_ok", n=len(clean), task=getattr(brief, "task_id", None))
        return clean

    def run_task(self, brief: TaskBrief) -> HarnessResult:
        instruction = (
            "FIRST check whether the task's acceptance criteria are ALREADY fully "
            "satisfied by existing code in the current worktree (read the files the "
            "acceptance names). If they ARE already satisfied, make NO changes and "
            "stop -- do not re-implement working code. Otherwise implement the task "
            "in the current git worktree, test-driven (failing test first), matching "
            "the repo's conventions.")
        data = json.dumps({"task_id": brief.task_id, "title": brief.title,
                           "description": brief.description,
                           "acceptance": brief.acceptance,
                           "prior_feedback": brief.prior_feedback, "round": brief.round})
        # A dispatcher may pin this ONE call to a graded bucket (see buckets.py);
        # absent that, dispatch_model_of is None and this is the pre-existing call.
        raw = self._run_raw(instruction, data, worktree=brief.worktree, repo=brief.repo,
                            model=dispatch_model_of(brief))
        usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
        return HarnessResult(
            ok=(not bool(raw.get("is_error"))) and int(raw.get("exit_code", 0) or 0) == 0,
            artifact=brief.worktree,
            tokens=int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)),
            cost_usd=float(raw.get("total_cost_usd", 0.0) or 0.0),
            raw=raw)

    def grade(self, brief: GradeBrief) -> GateResult:
        instruction = (
            "You are an independent grader. Score the diff 1-5 against the "
            "acceptance criteria and CI status. A 5 requires: every acceptance "
            "criterion met, tests present and passing, CI green. Reply strictly as "
            "JSON: {\"score\":N,\"passed\":bool,\"notes\":\"...\"}. ONLY when the "
            "score is 5 and the work is genuinely complete, include the exact token "
            "<promise>COMPLETE</promise> inside notes; never include it otherwise.")
        data = json.dumps({"task_id": brief.task_id, "diff": brief.diff,
                           "acceptance": brief.acceptance, "ci_status": brief.ci_status,
                           "diff_coverage": brief.diff_coverage})
        # The grader reads the DIFF, which carries the card's content, so it sits in
        # the same sensitivity zone as the build and takes the same bucket.
        out = self._run(instruction, data, worktree=brief.worktree, repo=brief.repo,
                        light=True, model=dispatch_model_of(brief))
        return GateResult(score=out.get("score"), passed=bool(out.get("passed")),
                          notes=out.get("notes", ""), artifact=out.get("artifact"))
