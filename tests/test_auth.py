import pytest
from fastapi import HTTPException

from skharness.auth import AuthContext, check_token, require_bearer


def _v(token):
    return token == "good"


def test_require_bearer_accepts_good_token_and_returns_it():
    assert require_bearer("Bearer good", _v) == "good"


def test_require_bearer_missing_header_is_401():
    with pytest.raises(HTTPException) as ei:
        require_bearer(None, _v)
    assert ei.value.status_code == 401


def test_require_bearer_empty_token_is_401_before_verifier():
    # A verifier that accepts everything must still not see an empty token.
    with pytest.raises(HTTPException) as ei:
        require_bearer("Bearer ", lambda token: True)
    assert ei.value.status_code == 401


def test_require_bearer_non_bearer_scheme_is_401():
    with pytest.raises(HTTPException) as ei:
        require_bearer("Basic xyz", _v)
    assert ei.value.status_code == 401


def test_require_bearer_rejected_token_is_403():
    with pytest.raises(HTTPException) as ei:
        require_bearer("Bearer bad", _v)
    assert ei.value.status_code == 403


def test_check_token_fail_closed_on_empty():
    assert check_token("", lambda token: True) is False
    assert check_token(None, lambda token: True) is False
    assert check_token("   ", lambda token: True) is False


def test_check_token_true_only_when_verifier_accepts():
    assert check_token("good", _v) is True
    assert check_token("bad", _v) is False


# ---- required_scope gate: read/write scope split -----------------------------

def _scoped(*scopes):
    ctx = AuthContext(scopes=frozenset(scopes))
    return lambda token: ctx


def test_authcontext_has_scope_and_wildcard():
    ctx = AuthContext(scopes=frozenset({"skcode.stream"}))
    assert ctx.has_scope("skcode.stream") is True
    assert ctx.has_scope("skcode.inject") is False
    assert AuthContext(scopes=frozenset({"*"})).has_scope("anything") is True


def test_require_bearer_grants_when_scope_present():
    v = _scoped("skcode.stream", "skcode.inject")
    assert require_bearer("Bearer t", v, "skcode.stream") == "t"
    assert require_bearer("Bearer t", v, "skcode.inject") == "t"


def test_require_bearer_403_insufficient_scope():
    v = _scoped("skcode.stream")   # read only
    with pytest.raises(HTTPException) as ei:
        require_bearer("Bearer t", v, "skcode.inject")
    assert ei.value.status_code == 403


def test_require_bearer_bare_true_grants_every_scope():
    # A bool verifier (test fake / allow) carries no scopes: True == full authority.
    assert require_bearer("Bearer t", lambda token: True, "skcode.inject") == "t"


def test_require_bearer_deny_all_wins_over_required_scope():
    # Deny-all returns False: 403 regardless of the scope asked for.
    with pytest.raises(HTTPException) as ei:
        require_bearer("Bearer t", lambda token: False, "skcode.stream")
    assert ei.value.status_code == 403


def test_require_bearer_truthy_scope_opaque_fails_closed_on_scoped_route():
    # A truthy result that is NOT a scope carrier and NOT bare True is denied on a
    # scoped route (fail closed), but still allowed when no scope is required.
    def v(token):
        return "yes"
    assert require_bearer("Bearer t", v) == "t"          # no scope required -> ok
    with pytest.raises(HTTPException) as ei:
        require_bearer("Bearer t", v, "skcode.stream")
    assert ei.value.status_code == 403


def test_check_token_scope_split():
    v = _scoped("skcode.stream")
    assert check_token("t", v, "skcode.stream") is True
    assert check_token("t", v, "skcode.inject") is False
    assert check_token("t", lambda token: True, "skcode.inject") is True
    assert check_token("t", lambda token: False, "skcode.stream") is False
