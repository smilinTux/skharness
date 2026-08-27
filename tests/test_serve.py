import json
import os

import pytest

from skharness.harnesses.pi import PiHarness
from skharness.securefs import SecureDir
from skharness.serve import (
    DEFAULT_PORT,
    build_arena_status_service,
    build_audit_log,
    build_default_verifier,
    build_digest_provider,
    build_dispatch_targets,
    build_host_harness,
    full_profile_allowed,
    resolve_bind,
    skcode_state_dir,
)


def test_full_profile_allowed_defaults_to_the_enrolled_operator(monkeypatch):
    monkeypatch.delenv("SKCODE_FULL_PROFILE_SUBJECTS", raising=False)
    assert full_profile_allowed("lumina@chef.skworld.io") is True
    assert full_profile_allowed("mallory@evil.io") is False


def test_full_profile_allowed_honors_env_allowlist(monkeypatch):
    monkeypatch.setenv("SKCODE_FULL_PROFILE_SUBJECTS", "a@x.io, b@y.io")
    assert full_profile_allowed("a@x.io") is True
    assert full_profile_allowed("b@y.io") is True
    # a subject not on the explicit list (even the former default) is denied full
    assert full_profile_allowed("lumina@chef.skworld.io") is False


def test_full_profile_allowed_rejects_blank_subject(monkeypatch):
    monkeypatch.delenv("SKCODE_FULL_PROFILE_SUBJECTS", raising=False)
    assert full_profile_allowed("") is False
    assert full_profile_allowed(None) is False  # type: ignore[arg-type]


def test_default_port_is_9394():
    # 9390 belongs to the skcomms broker; hostd takes 9394 (spec R0.4).
    assert DEFAULT_PORT == 9394


def test_resolve_bind_accepts_a_concrete_ip():
    assert resolve_bind("100.64.0.1") == "100.64.0.1"


@pytest.mark.parametrize("bad", ["0.0.0.0", "::", "", None])
def test_resolve_bind_refuses_wildcard(bad):
    with pytest.raises(SystemExit):
        resolve_bind(bad)


def test_default_verifier_fails_closed():
    v = build_default_verifier()
    assert v("anything") is False


def test_build_audit_log_appends_jsonl_under_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCODE_STATE_DIR", str(tmp_path))
    audit = build_audit_log()
    audit("hello dispatch")
    log = tmp_path / "audit.log"
    assert log.exists()
    line = json.loads(log.read_text().splitlines()[0])
    assert line["record"] == "hello dispatch"


def test_build_audit_log_rejects_ancestor_symlink_without_escape(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "state-link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("SKCODE_STATE_DIR", str(tmp_path / "state-link" / "state"))

    with pytest.raises(OSError):
        build_audit_log()

    assert not (outside / "state").exists()
    assert not (outside / "audit.log").exists()


def test_build_audit_log_parent_swap_stays_on_anchored_inode(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("SKCODE_STATE_DIR", str(state))
    audit = build_audit_log()
    held = tmp_path / "held-state"
    state.rename(held)
    outside = tmp_path / "outside"
    outside.mkdir()
    state.symlink_to(outside, target_is_directory=True)

    audit("anchored")

    assert (held / "audit.log").is_file()
    assert not (outside / "audit.log").exists()


def test_build_audit_log_rejects_hardlinked_final_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCODE_STATE_DIR", str(tmp_path))
    audit = build_audit_log()
    audit("first")
    (tmp_path / "audit-link").hardlink_to(tmp_path / "audit.log")

    with pytest.raises(OSError):
        audit("must fail")

    assert "must fail" not in (tmp_path / "audit.log").read_text()


def test_audit_hardlink_at_former_verify_write_boundary_gets_no_new_record(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCODE_STATE_DIR", str(tmp_path))
    audit = build_audit_log()
    audit("first")
    outside = tmp_path / "outside-alias.log"
    original_verify = SecureDir._verify_file
    attacked = False

    def verify_then_link(fd, *, mode):
        nonlocal attacked
        original_verify(fd, mode=mode)
        if not attacked:
            os.link(tmp_path / "audit.log", outside)
            attacked = True

    with monkeypatch.context() as patch:
        patch.setattr(SecureDir, "_verify_file", staticmethod(verify_then_link))
        with pytest.raises(OSError):
            audit("mandatory-second")

    assert "mandatory-second" not in outside.read_text()
    assert "mandatory-second" not in (tmp_path / "audit.log").read_text()
    outside.unlink()
    # The deterministic same-inode backup left by the failed attempt is safely
    # removed on retry; no record is duplicated and ordering is preserved.
    audit("mandatory-second")
    lines = [
        json.loads(line)["record"] for line in (tmp_path / "audit.log").read_text().splitlines()
    ]
    assert lines == ["first", "mandatory-second"]


def test_audit_hardlink_after_last_old_inode_check_gets_no_new_record(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCODE_STATE_DIR", str(tmp_path))
    audit = build_audit_log()
    audit("first")
    outside = tmp_path / "outside-after-check.log"
    original_same = SecureDir._same_published_file
    attacked = False

    def check_then_link(self, name, fd, **kwargs):
        nonlocal attacked
        original_same(self, name, fd, **kwargs)
        if not attacked:
            os.link(tmp_path / "audit.log", outside)
            attacked = True

    monkeypatch.setattr(SecureDir, "_same_published_file", check_then_link)
    audit("mandatory-second")

    assert "mandatory-second" not in outside.read_text()
    assert "mandatory-second" in (tmp_path / "audit.log").read_text()


def test_build_dispatch_targets_reflects_the_allowlist(tmp_path, monkeypatch):
    a = tmp_path / "skharness"
    a.mkdir()
    monkeypatch.setenv("SKCODE_DISPATCH_REPOS", str(a))
    targets = build_dispatch_targets()()
    assert targets["repos"] == [str(a.resolve())]


def test_build_dispatch_targets_empty_when_no_allowlist(monkeypatch):
    monkeypatch.delenv("SKCODE_DISPATCH_REPOS", raising=False)
    assert build_dispatch_targets()()["repos"] == []


def test_build_host_harness_wires_pi_to_host_local_governed_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCODE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SKCODE_PI_GATEWAY_BASE", "http://gateway.test/v1")
    monkeypatch.setenv("SKCODE_DISPATCH_REPOS", str(tmp_path / "allowed"))

    harness = build_host_harness(host_id="chiap03", harness_name="pi")

    assert isinstance(harness, PiHarness)
    assert harness.host == "chiap03"
    assert harness.dispatch_repos == [str((tmp_path / "allowed").resolve())]
    assert harness.worktree_root == tmp_path / "state" / "worktrees"
    assert harness.config_root == tmp_path / "state" / "pi-config"
    assert harness.reservation_root == tmp_path / "state" / "sid-reservations"


def test_build_host_harness_rejects_unknown_remote_metadata_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCODE_STATE_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="unsupported host-local harness"):
        build_host_harness(host_id="chiap03", harness_name="ssh-pi")


def test_skcode_state_dir_honors_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCODE_STATE_DIR", str(tmp_path))
    assert skcode_state_dir() == tmp_path


def test_build_digest_provider_reads_the_real_default_path(tmp_path, monkeypatch):
    fake = tmp_path / "digest.json"
    fake.write_text('{"date": "2026-08-14"}', encoding="utf-8")
    monkeypatch.setenv("SKCODE_WATCHDOG_DIGEST_PATH", str(fake))
    provider = build_digest_provider()
    assert provider() == b'{"date": "2026-08-14"}'


def test_build_digest_provider_none_when_nothing_published(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCODE_WATCHDOG_DIGEST_PATH", str(tmp_path / "does-not-exist.json"))
    provider = build_digest_provider()
    assert provider() is None


def test_arena_disabled_reports_unconfigured_dependencies_but_stays_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCODE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("SKHARNESS_ARENA_ENABLED", raising=False)
    monkeypatch.delenv("SKHARNESS_ARENA_SKGATEWAY_HEALTH_URL", raising=False)
    monkeypatch.delenv("SKHARNESS_ARENA_VERIFIER_HEALTH_URL", raising=False)
    status = build_arena_status_service().readiness()
    assert status["ready"] is True
    assert status["dependencies"]["store"]["state"] == "ok"
    assert status["dependencies"]["skgateway"] == {
        "state": "unknown",
        "required": False,
        "detail": "SKHARNESS_ARENA_SKGATEWAY_HEALTH_URL not configured",
    }


def test_arena_enabled_never_calls_missing_dependency_configuration_healthy(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCODE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SKHARNESS_ARENA_ENABLED", "true")
    monkeypatch.delenv("SKHARNESS_ARENA_SKGATEWAY_HEALTH_URL", raising=False)
    monkeypatch.delenv("SKHARNESS_ARENA_VERIFIER_HEALTH_URL", raising=False)
    status = build_arena_status_service().readiness()
    assert status["ready"] is False
    assert status["dependencies"]["skgateway"]["state"] == "unknown"
    assert status["dependencies"]["verifier"]["state"] == "unknown"
