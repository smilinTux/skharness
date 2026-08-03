"""Capauth bearer gate, shared by the skharness gateway and skcode-hostd.

`verify_caller` is the auth seam: a real capauth verifier in production, a fake
in tests. Fail closed on missing or empty tokens BEFORE the verifier runs.

The verifier result carries scope, so routes can split read from write on the
SAME valid token (R2.4 scope split). A verifier returns:

* a falsy value (``False``/``None``) to DENY (fail closed);
* the bare bool ``True`` to ALLOW with FULL authority (the deny-all-vs-allow
  bool verifiers and test fakes carry no scopes, so True grants every route);
* an :class:`AuthContext` (or any object exposing ``has_scope``) to ALLOW only
  the scopes it carries, which is how the real capauth verifier lets the daemon
  grant read-only view (``skcode.stream``) without arming write (``skcode.inject``).

The deny-all default (serve.build_default_verifier) returns ``False`` and so
denies every route regardless of the scope a route asks for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from fastapi import HTTPException

Verifier = Callable[[str], Any]


@dataclass(frozen=True)
class AuthContext:
    """A verified caller's granted scopes.

    Truthy (a frozen object), so it passes the allow gate; ``has_scope`` drives
    the read/write scope split. Honours the ``*`` wildcard, matching capauth's
    own ``TokenPayload.has_scope``.
    """

    scopes: frozenset[str]

    def has_scope(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes


def _grants_scope(result: Any, required_scope: Optional[str]) -> bool:
    """Does a truthy verifier result satisfy ``required_scope``?

    Called ONLY after ``result`` is known truthy. When no scope is required the
    caller is already authorized. A bare-bool ``True`` is treated as full
    authority (the bool/fake verifiers carry no scopes and must keep granting
    every route). A scope carrier is asked via ``has_scope``. Anything else
    truthy-but-scope-opaque fails CLOSED on a scoped route.
    """
    if required_scope is None:
        return True
    if result is True:
        return True
    has = getattr(result, "has_scope", None)
    if callable(has):
        return bool(has(required_scope))
    return False


def require_bearer(
    authorization: str | None,
    verify_caller: Verifier,
    required_scope: str | None = None,
) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization[len("Bearer "):].strip()
    if not token:
        raise HTTPException(401, "missing bearer token")
    result = verify_caller(token)
    if not result:
        raise HTTPException(403, "unauthorized")
    if not _grants_scope(result, required_scope):
        raise HTTPException(403, "insufficient scope")
    return token


def check_token(
    token: str | None,
    verify_caller: Verifier,
    required_scope: str | None = None,
) -> bool:
    if not token or not token.strip():
        return False
    result = verify_caller(token.strip())
    if not result:
        return False
    return _grants_scope(result, required_scope)
