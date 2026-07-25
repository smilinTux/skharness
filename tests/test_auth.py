import pytest
from fastapi import HTTPException

from skharness.auth import check_token, require_bearer


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
