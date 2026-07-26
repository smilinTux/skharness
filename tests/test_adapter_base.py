from skharness.autocode.adapters.base import BaseCliAdapter
from skharness.autocode.sandbox import Sandbox, LaunchSpec, AuthMount
from skharness.autocode.types import AssessBrief, RepoSpec, TaskBrief


class _Fake(BaseCliAdapter):
    name = "fake"
    def _argv(self, prompt): return ["fake", prompt]
    def _image(self): return "sandbox-fake:1"
    def _auth_mounts(self): return [AuthMount("/h/.cred", "/c/.cred")]
    def _auth_env(self): return {"BASE_URL": "http://gw.local"}
    def _parse(self, raw): return raw.get("result", raw)
    def capabilities(self): return {"session_resume": False, "structured_output": "json",
                                    "sandbox": True, "tool_restrictions": True}


def test_assess_builds_spec_and_delegates_to_sandbox(monkeypatch):
    seen = {}
    sb = Sandbox(live_execution=True)
    monkeypatch.setattr(sb, "spawn",
        lambda spec, **kw: seen.setdefault("spec", spec) and {"result": {"verdict": "valid", "reason": "ok"}})
    a = _Fake(sb, egress_hosts=["gw.local"])
    v = a.assess(AssessBrief(task_id="t1", title="t", description="d", acceptance=[],
                             tags=[], repo=None, codebase_context=""))
    assert v.verdict == "valid"
    spec = seen["spec"]
    assert isinstance(spec, LaunchSpec) and spec.image == "sandbox-fake:1"
    assert spec.argv[0] == "fake" and spec.auth_env["BASE_URL"] == "http://gw.local"
    assert spec.egress_hosts == ["gw.local"]


def _repo(**kw):
    base = dict(name="r", path="/tmp/r", base_branch="main", integration_branch="int",
                test_cmd="pytest", ci="none")
    base.update(kw)
    return RepoSpec(**base)


def _task_brief(repo):
    return TaskBrief(task_id="t1", repo=repo, worktree="/tmp/wt", title="t",
                     description="d", acceptance=[], prior_feedback=None, round=0)


def test_run_task_crash_is_not_ok(monkeypatch):
    sb = Sandbox(live_execution=True)
    monkeypatch.setattr(sb, "spawn",
        lambda spec, **kw: {"result": "boom", "exit_code": 1, "is_error": True})
    a = _Fake(sb, egress_hosts=[])
    result = a.run_task(_task_brief(_repo()))
    assert result.ok is False


def test_run_task_clean_json_exit_zero_is_ok(monkeypatch):
    sb = Sandbox(live_execution=True)
    monkeypatch.setattr(sb, "spawn", lambda spec, **kw: {"result": {"x": 1}})
    a = _Fake(sb, egress_hosts=[])
    result = a.run_task(_task_brief(_repo()))
    assert result.ok is True


def test_run_raw_uses_per_repo_sandbox_image_override(monkeypatch):
    seen = {}
    sb = Sandbox(live_execution=True)
    monkeypatch.setattr(sb, "spawn",
        lambda spec, **kw: seen.setdefault("spec", spec) and {"result": {}})
    a = _Fake(sb, egress_hosts=[])
    a._run_raw("instr", "data", worktree="/tmp/wt", repo=_repo(sandbox_image="repo-img:9"))
    assert seen["spec"].image == "repo-img:9"


def test_run_raw_falls_back_to_adapter_image_when_repo_image_is_none(monkeypatch):
    seen = {}
    sb = Sandbox(live_execution=True)
    monkeypatch.setattr(sb, "spawn",
        lambda spec, **kw: seen.setdefault("spec", spec) and {"result": {}})
    a = _Fake(sb, egress_hosts=[])
    a._run_raw("instr", "data", worktree="/tmp/wt", repo=_repo(sandbox_image=None))
    assert seen["spec"].image == "sandbox-fake:1"


def test_config_files_hook_defaults_to_empty():
    a = _Fake(Sandbox(), egress_hosts=[])
    assert a._config_files() == {}


def test_stdin_for_hook_defaults_to_none():
    a = _Fake(Sandbox(), egress_hosts=[])
    assert a._stdin_for("prompt") is None


def test_run_raw_sets_stdin_from_hook_override(monkeypatch):
    class _FakeStdin(_Fake):
        def _stdin_for(self, prompt):
            return prompt

    seen = {}
    sb = Sandbox(live_execution=True)
    monkeypatch.setattr(sb, "spawn",
        lambda spec, **kw: seen.setdefault("spec", spec) and {"result": {}})
    a = _FakeStdin(sb, egress_hosts=[])
    a._run_raw("instr", "data", worktree="/tmp/wt", repo=_repo(sandbox_image=None))
    framed_prompt = seen["spec"].argv[1]           # _Fake._argv returns ["fake", prompt]
    assert seen["spec"].stdin == framed_prompt


def test_run_raw_passes_config_files_to_launch_spec(monkeypatch):
    class _FakeCfg(_Fake):
        def _config_files(self):
            return {"/agent/x.json": "hi"}

    seen = {}
    sb = Sandbox(live_execution=True)
    monkeypatch.setattr(sb, "spawn",
        lambda spec, **kw: seen.setdefault("spec", spec) and {"result": {}})
    a = _FakeCfg(sb, egress_hosts=[])
    a._run_raw("instr", "data", worktree="/tmp/wt", repo=_repo(sandbox_image=None))
    assert seen["spec"].config_files == {"/agent/x.json": "hi"}


def test_extract_json_tolerates_fences_and_prose():
    from skharness.autocode.adapters.base import extract_json
    assert extract_json('{"score": 5}') == {"score": 5}
    assert extract_json('```json\n{"a":1}\n```') == {"a": 1}
    assert extract_json('answer: {"verdict":"valid"} done') == {"verdict": "valid"}
    assert extract_json('no json') is None
    assert extract_json(None) is None


def test_claude_parse_extracts_from_result_string():
    from skharness.autocode.adapters.claude_code import ClaudeCodeAdapter
    from skharness.autocode.sandbox import Sandbox
    a = ClaudeCodeAdapter(["Read"], sandbox=Sandbox())
    raw = {"type": "result", "result": '{"score":5,"passed":true,"notes":"ok"}', "is_error": False}
    assert a._parse(raw) == {"score": 5, "passed": True, "notes": "ok"}


def test_run_retries_past_api_error_and_empty_then_returns_usable(monkeypatch):
    """_run must not let a transient hard error or an empty/unparseable reply
    become the answer: it retries and returns the first usable (non-empty) parse."""
    sb = Sandbox(live_execution=True)
    seq = [
        {"is_error": True, "result": "API Error: 401 token expired"},   # hard error -> retry
        {"result": {}},                                                 # empty parse -> retry
        {"result": {"verdict": "valid", "reason": "ok"}},               # usable -> return
    ]
    calls = {"n": 0}
    def fake_spawn(spec, **kw):
        r = seq[calls["n"]]; calls["n"] += 1; return r
    monkeypatch.setattr(sb, "spawn", fake_spawn)
    a = _Fake(sb, egress_hosts=[])
    out = a._run("instr", "data", worktree="/tmp/wt", repo=None)
    assert out == {"verdict": "valid", "reason": "ok"}
    assert calls["n"] == 3                                              # exhausted the two bad rolls


def test_run_gives_up_after_bounded_attempts(monkeypatch):
    """When every attempt hard-errors, _run stops after _RUN_ATTEMPTS and returns
    the last (empty) parse rather than looping forever."""
    sb = Sandbox(live_execution=True)
    calls = {"n": 0}
    def fake_spawn(spec, **kw):
        calls["n"] += 1; return {"is_error": True, "result": "still down"}
    monkeypatch.setattr(sb, "spawn", fake_spawn)
    a = _Fake(sb, egress_hosts=[])
    out = a._run("instr", "data", worktree="/tmp/wt", repo=None)
    assert out == {}
    assert calls["n"] == BaseCliAdapter._RUN_ATTEMPTS


def _brief():
    return AssessBrief(task_id="t", title="T", description="d", acceptance=[],
                       tags=[], repo="r", codebase_context="")


def test_assess_fails_open_to_valid_on_inconclusive(tmp_path, monkeypatch):
    """A cheap pre-filter must not strand work the strong twin gate protects: an
    inconclusive assess (no parseable verdict after retries) proceeds as valid."""
    monkeypatch.setenv("SKHARNESS_HEALTH_PATH", str(tmp_path / "h.jsonl"))
    a = _Fake(Sandbox(), egress_hosts=[])
    monkeypatch.setattr(a, "_run", lambda *args, **kw: {})     # inconclusive
    v = a.assess(_brief())
    assert v.verdict == "valid" and "fail-open" in v.reason
    from skharness.autocode import health
    assert health.recent("assess_inconclusive")               # telemetry recorded


def test_assess_honors_explicit_needs_decision(tmp_path, monkeypatch):
    """An EXPLICIT model needs_decision is still honored (only the non-answer
    fails open)."""
    monkeypatch.setenv("SKHARNESS_HEALTH_PATH", str(tmp_path / "h.jsonl"))
    a = _Fake(Sandbox(), egress_hosts=[])
    monkeypatch.setattr(a, "_run",
                        lambda *args, **kw: {"verdict": "needs_decision", "reason": "contradictory"})
    v = a.assess(_brief())
    assert v.verdict == "needs_decision" and v.reason == "contradictory"


def test_run_attempts_adapt_to_decline_rate(tmp_path, monkeypatch):
    """Self-tuning: the retry budget climbs toward the ceiling when the recent
    decline rate is high, and sits at base when the CLI is healthy."""
    monkeypatch.setenv("SKHARNESS_HEALTH_PATH", str(tmp_path / "h.jsonl"))
    from skharness.autocode import health
    a = _Fake(Sandbox(), egress_hosts=[])
    assert a._run_attempts() == a._RUN_ATTEMPTS                # no data -> base
    for _ in range(8):
        health.record("run_inconclusive")
    for _ in range(2):
        health.record("run_ok")                               # 80% decline
    assert a._run_attempts() == a._RUN_ATTEMPTS_MAX
