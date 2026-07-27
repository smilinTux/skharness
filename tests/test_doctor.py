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


def test_preflight_runs_all_and_records(tmp_path, monkeypatch):
    monkeypatch.setenv("SKHARNESS_HEALTH_PATH", str(tmp_path / "h.jsonl"))
    results = doctor.preflight()
    assert {r.name for r in results} == {"shim-delegation", "auth",
                                         "proxy-image", "decline-rate", "concurrency"}
    assert health.recent("preflight")               # verdict recorded


def test_format_report_shows_fix_only_for_problems():
    rs = [doctor.Check("a", "ok", "fine", fix="unused"),
          doctor.Check("b", "fail", "broken", fix="do the thing")]
    out = doctor.format_report(rs)
    assert "OK" in out and "FAIL" in out
    assert "do the thing" in out          # fix shown for the failure
    assert out.count("fix:") == 1         # not shown for the ok check
