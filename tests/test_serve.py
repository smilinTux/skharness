import json

import pytest

from skharness.serve import (
    DEFAULT_PORT,
    build_audit_log,
    build_default_verifier,
    build_dispatch_targets,
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


def test_build_dispatch_targets_reflects_the_allowlist(tmp_path, monkeypatch):
    a = tmp_path / "skharness"
    a.mkdir()
    monkeypatch.setenv("SKCODE_DISPATCH_REPOS", str(a))
    targets = build_dispatch_targets()()
    assert targets["repos"] == [str(a.resolve())]


def test_build_dispatch_targets_empty_when_no_allowlist(monkeypatch):
    monkeypatch.delenv("SKCODE_DISPATCH_REPOS", raising=False)
    assert build_dispatch_targets()()["repos"] == []


def test_skcode_state_dir_honors_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCODE_STATE_DIR", str(tmp_path))
    assert skcode_state_dir() == tmp_path
