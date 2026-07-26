"""Capauth bearer gate, shared by the skharness gateway and skcode-hostd.

`verify_caller` is the auth seam: a real capauth verifier in production, a fake
in tests. Fail closed on missing or empty tokens BEFORE the verifier runs.
"""

from __future__ import annotations

from typing import Callable

from fastapi import HTTPException

Verifier = Callable[[str], bool]


def require_bearer(authorization: str | None, verify_caller: Verifier) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization[len("Bearer "):].strip()
    if not token:
        raise HTTPException(401, "missing bearer token")
    if not verify_caller(token):
        raise HTTPException(403, "unauthorized")
    return token


def check_token(token: str | None, verify_caller: Verifier) -> bool:
    if not token or not token.strip():
        return False
    return bool(verify_caller(token.strip()))
