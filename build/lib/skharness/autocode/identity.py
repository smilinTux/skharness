"""Who is writing this record: agent, session, node (card A2.1).

Every session on the control node writes to the coordination board as the
agent name ``lumina``. Measured on 2026-08-16: the overlay log held 2,027
events across only three distinct writer values, so four concurrent sessions
were permanently indistinguishable after the fact. This module mints the
identity that fixes that going forward. It is deliberately additive: nothing
here writes a session id onto a historical record, and nothing here walks an
existing log.

Precedence for the agent name is fixed by ``~/.claude/CLAUDE.md``:

    SKAGENT > SKCAPSTONE_AGENT > SKMEMORY_AGENT > "lumina"

The precedence alone is not enough, and this is the part that is easy to get
wrong. Those three variables genuinely disagree in production. On the ``.41``
node a single ``skcomms.service`` unit sets::

    SKAGENT=jarvis
    SKMEMORY_AGENT=lumina
    SKCHAT_IDENTITY=capauth:opus@skworld.io

so a bare ``agent="jarvis"`` on a run record silently discards the fact that
two other names were also on the table. Unit NAMES are no better as a tiebreak
(``skchat-daemon`` runs as opus while ``skchat-daemon-jarvis`` runs as jarvis),
so there is no out-of-band way to recover the answer later. The resolver
therefore records WHICH variable it read, as a machine-readable field on the
result (``agent_var``), not as a log line. Without that, the run record
inherits exactly the ambiguity this card exists to remove.

``session_id`` is minted once per process and stable for that process's whole
lifetime, so every record a run emits carries one id. ``SK_SESSION_ID`` is
honoured when set so a resumed or re-execed run keeps the identity it already
published; ``session_id_var`` says which of the two happened.
"""
from __future__ import annotations

import os
import socket
import threading
import uuid
from typing import NamedTuple

#: Agent-name env vars in precedence order. Fixed by ``~/.claude/CLAUDE.md``;
#: do not reorder without changing that document first.
AGENT_ENV_VARS: tuple[str, ...] = ("SKAGENT", "SKCAPSTONE_AGENT", "SKMEMORY_AGENT")

#: Agent name when none of ``AGENT_ENV_VARS`` is set to a non-empty value.
DEFAULT_AGENT = "lumina"

#: ``agent_var`` value meaning "no env var supplied a name, DEFAULT_AGENT used".
AGENT_VAR_DEFAULT = "default"

#: Env var that pins a session id across a re-exec / resume.
SESSION_ID_ENV_VAR = "SK_SESSION_ID"

#: ``session_id_var`` value meaning "freshly minted uuid4, no env var involved".
SESSION_ID_VAR_MINTED = "minted"


class Identity(NamedTuple):
    """Who/where a run record came from.

    A NamedTuple so callers can unpack it positionally and still read fields by
    name. ``agent_var`` and ``session_id_var`` are the provenance fields: they
    are what makes a stored record self-describing rather than merely
    plausible.
    """

    agent: str
    session_id: str
    node: str
    #: Which env var the agent name came from: one of ``AGENT_ENV_VARS``, or
    #: ``AGENT_VAR_DEFAULT`` when the fallback was used.
    agent_var: str
    #: ``SESSION_ID_ENV_VAR`` when inherited, ``SESSION_ID_VAR_MINTED`` when
    #: this process minted it.
    session_id_var: str

    @property
    def triple(self) -> tuple[str, str, str]:
        """``(agent, session_id, node)``, the identity minus its provenance."""
        return (self.agent, self.session_id, self.node)

    def to_dict(self) -> dict:
        """JSON-ready mapping, for embedding in a run record or descriptor."""
        return dict(self._asdict())


_lock = threading.Lock()
_cached: Identity | None = None


def _resolve_agent() -> tuple[str, str]:
    """``(agent, agent_var)`` by the documented precedence.

    A variable set to an empty or whitespace-only value counts as unset: a
    systemd unit with a bare ``Environment=SKAGENT=`` must fall through to the
    next name rather than pin the agent to "".
    """
    for var in AGENT_ENV_VARS:
        value = (os.environ.get(var) or "").strip()
        if value:
            return value, var
    return DEFAULT_AGENT, AGENT_VAR_DEFAULT


def _resolve_session_id() -> tuple[str, str]:
    """``(session_id, session_id_var)``: inherited if pinned, else fresh."""
    pinned = (os.environ.get(SESSION_ID_ENV_VAR) or "").strip()
    if pinned:
        return pinned, SESSION_ID_ENV_VAR
    return uuid.uuid4().hex, SESSION_ID_VAR_MINTED


def resolve_identity() -> Identity:
    """The calling process's identity, memoized for the process's lifetime.

    Memoized rather than recomputed so two records written seconds apart cannot
    disagree, and so a later env mutation (a library that sets ``SKAGENT``,
    say) cannot retroactively split one run across two names. Call
    :func:`reset_identity_cache` to force a re-read; that exists for tests and
    should not be used to model a real identity change mid-run.
    """
    global _cached
    cached = _cached
    if cached is not None:
        return cached
    with _lock:
        if _cached is None:
            agent, agent_var = _resolve_agent()
            session_id, session_id_var = _resolve_session_id()
            _cached = Identity(
                agent=agent,
                session_id=session_id,
                node=socket.gethostname(),
                agent_var=agent_var,
                session_id_var=session_id_var,
            )
        return _cached


def reset_identity_cache() -> None:
    """Drop the memoized identity so the next resolve re-reads the env."""
    global _cached
    with _lock:
        _cached = None


__all__ = [
    "AGENT_ENV_VARS",
    "AGENT_VAR_DEFAULT",
    "DEFAULT_AGENT",
    "Identity",
    "SESSION_ID_ENV_VAR",
    "SESSION_ID_VAR_MINTED",
    "reset_identity_cache",
    "resolve_identity",
]
