import subprocess

from skharness.autocode import doctor, health


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def test_check_ok_property():
    assert doctor.Check("x", "ok", "d").ok is True
    assert doctor.Check("x", "warn", "d").ok is False
    assert doctor.Check("x", "fail", "d").ok is False


def test_proxy_image_ok_when_module_imports():
    c = doctor.check_sandbox_proxy_image(run=lambda *a, **k: _proc(0))
    assert c.status == "ok"


def test_proxy_image_fail_when_module_missing():
    c = doctor.check_sandbox_proxy_image(
        run=lambda *a, **k: _proc(1, stderr="ModuleNotFoundError: skharness"))
    assert c.status == "fail" and "rebuild" in c.fix


def test_proxy_image_warn_when_no_docker():
    def _no_docker(*a, **k):
        raise FileNotFoundError("docker")
    c = doctor.check_sandbox_proxy_image(run=_no_docker)
    assert c.status == "warn"


def test_proxy_image_warn_on_timeout():
    def _slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=60)
    c = doctor.check_sandbox_proxy_image(run=_slow)
    assert c.status == "warn"


def test_auth_fail_when_no_token(tmp_path, monkeypatch):
    from skharness.autocode.adapters import claude_code as cc
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(cc, "_CRED_PATH", str(tmp_path / "none.json"))
    assert doctor.check_auth().status == "fail"


def test_auth_ok_with_env_token(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-LONGLIVED")
    c = doctor.check_auth()
    assert c.status == "ok" and "long-lived" in c.detail


def test_auth_warn_on_expired_credential(tmp_path, monkeypatch):
    from skharness.autocode.adapters import claude_code as cc
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    cred = tmp_path / ".credentials.json"
    cred.write_text('{"claudeAiOauth": {"accessToken": "sk-x", "expiresAt": 1}}')
    monkeypatch.setattr(cc, "_CRED_PATH", str(cred))
    assert doctor.check_auth().status == "warn"


def test_decline_signal_warns_when_high(tmp_path, monkeypatch):
    monkeypatch.setenv("SKHARNESS_HEALTH_PATH", str(tmp_path / "h.jsonl"))
    for _ in range(6):
        health.record("run_inconclusive")
    for _ in range(4):
        health.record("run_ok")                     # 60% decline
    assert doctor.check_decline_signal().status == "warn"


def test_decline_signal_ok_when_low(tmp_path, monkeypatch):
    monkeypatch.setenv("SKHARNESS_HEALTH_PATH", str(tmp_path / "h.jsonl"))
    for _ in range(9):
        health.record("run_ok")
    health.record("run_inconclusive")               # 10% decline
    assert doctor.check_decline_signal().status == "ok"


def test_grader_pin_ok_when_the_gateway_names_the_pin_back():
    c = doctor.check_grader_pin("m-1", "http://gw:18780",
                                probe=lambda *a: (200, "m-1", ""))
    assert c.status == "ok"


def test_grader_pin_fails_when_the_gateway_refuses_the_id():
    """The `ornith-big` case: the gateway is up and the pinned model is gone."""
    c = doctor.check_grader_pin("ornith-big", "http://gw:18780",
                                probe=lambda *a: (404, "", "model not found"))
    assert c.status == "fail"
    assert "ornith-big" in c.detail and "404" in c.detail
    assert "GRADER_MODEL" in c.fix


def test_grader_pin_cannot_report_ok_when_it_could_not_reach_the_gateway():
    """The load-bearing negative control. A checker that says healthy when it
    observed nothing is the exact failure this check exists to remove, so an
    unreachable gateway must warn LOUDLY and can never come back ok."""
    c = doctor.check_grader_pin("m-1", "http://gw:18780",
                                probe=lambda *a: (None, "", "URLError: refused"))
    assert c.status == "warn" and c.ok is False
    assert "UNVERIFIED" in c.detail


def test_grader_pin_warns_when_a_different_model_answered():
    """skgateway resolves failover server side. A 200 from something else means
    the pin is not the grader of record, so the grade it stamps is a fiction."""
    c = doctor.check_grader_pin("m-1", "http://gw:18780",
                                probe=lambda *a: (200, "m-9", ""))
    assert c.status == "warn"
    assert "m-9" in c.detail


def test_grader_pin_defaults_to_the_orchestrator_pin(monkeypatch):
    from skharness.autocode import orchestrator as orch
    seen = {}

    def _probe(model, base_url, timeout):
        seen["model"], seen["base"] = model, base_url
        return 200, model, ""
    monkeypatch.setenv("SKCODE_GATEWAY_BASE", "http://elsewhere:18780")
    assert doctor.check_grader_pin(probe=_probe).status == "ok"
    assert seen == {"model": orch.GRADER_MODEL, "base": "http://elsewhere:18780"}


def test_preflight_runs_all_and_records(tmp_path, monkeypatch):
    monkeypatch.setenv("SKHARNESS_HEALTH_PATH", str(tmp_path / "h.jsonl"))
    # Point the grader probe at a closed port: preflight must not put real
    # traffic on the live fleet from a test run. It warns, which is correct.
    monkeypatch.setenv("SKCODE_GATEWAY_BASE", "http://127.0.0.1:1")
    results = doctor.preflight()
    assert {r.name for r in results} == {"shim-delegation", "auth", "proxy-image",
                                         "grader-pin", "decline-rate", "concurrency"}
    assert health.recent("preflight")               # verdict recorded


def test_format_report_shows_fix_only_for_problems():
    rs = [doctor.Check("a", "ok", "fine", fix="unused"),
          doctor.Check("b", "fail", "broken", fix="do the thing")]
    out = doctor.format_report(rs)
    assert "OK" in out and "FAIL" in out
    assert "do the thing" in out          # fix shown for the failure
    assert out.count("fix:") == 1         # not shown for the ok check
