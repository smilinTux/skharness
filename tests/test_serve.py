import pytest

from skharness.serve import DEFAULT_PORT, build_default_verifier, resolve_bind


def test_default_port_is_9394():
    # 9390 belongs to the skcomms broker; hostd takes 9394 (spec R0.4).
    assert DEFAULT_PORT == 9394


def test_resolve_bind_accepts_a_concrete_ip():
    assert resolve_bind("100.108.59.57") == "100.108.59.57"


@pytest.mark.parametrize("bad", ["0.0.0.0", "::", "", None])
def test_resolve_bind_refuses_wildcard(bad):
    with pytest.raises(SystemExit):
        resolve_bind(bad)


def test_default_verifier_fails_closed():
    v = build_default_verifier()
    assert v("anything") is False
