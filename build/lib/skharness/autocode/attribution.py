"""Join a unit of work to the gateway row that describes it (card c7aea2e0, A6.3).

Three things merged within an hour of each other and together they made this
possible for the first time:

  * A6.1 (`adapters/pi.py`) puts `x-session-id` and `x-sk-card-id` on the wire,
    as a provider-level `headers` map in the models.json we already generate.
  * A2.1 (`identity.py`) mints the session id those headers carry.
  * skgateway now returns `x-sk-req-id`, `x-sk-backend` and `x-sk-model-served`,
    and its `request_log.backend` UPDATE was repaired.

So the caller can name itself and the gateway records the name. This module is
the other end: given a req id, it reads what the gateway actually stored and
says whether the row and the run are demonstrably the same event.

WHY THE STORE AND NOT THE RESPONSE HEADERS
------------------------------------------
`Sandbox.spawn` returns only the sandboxed CLI's stdout. No HTTP response header
survives that boundary, so for the sandboxed harness path the response headers
from the gateway change are unreachable by construction. What the harness does
have is the ids it CHOSE, which is enough: it queries the store by those ids.
The response headers remain the right route for a non-sandboxed HTTP caller, and
`SentIds.req_id` is where such a caller passes `x-sk-req-id` in.

WHAT THIS CAN AND CANNOT PROVE, MEASURED RATHER THAN ASSUMED
------------------------------------------------------------
CAN: that a session id we sent reached the gateway and landed on the row; that
the card id did too (`energy_log.card_id`); that the row is reachable by req id;
and that the backend which served the call is recoverable.

CANNOT, and the module is built so neither can be faked:

  * `request_log.agent_id` is NULL on all 8,136 rows and has never once been
    populated, by two different mechanisms across its history. Nothing here
    reads it. An agent name on a run record comes from `identity.py`, which
    knows it firsthand, never from this join.
  * No table holds a SERVED model. `token_usage.model` never once disagrees with
    `request_log.model` across all 1,445 joined rows, because both are the
    REQUESTED id. `model_served` is therefore fixed at None with a written
    reason, and is never derived from the agreeing columns. The served model
    does exist, in pi's stdout as `responseModel` (card 04970a6e); it does not
    exist here, and a join that quietly returned the requested id would make the
    substitution question permanently unaskable.

THE ONE RULE THAT MAKES ANY OF IT EVIDENCE
------------------------------------------
"nothing was sent" and "what was sent came back" must be different words. A
headerless call must produce a row with a NULL session id AND a verdict that
says ABSENT_AS_SENT, not MATCH. If a default ever gets stamped onto such a row,
`verify_join` returns INVENTED and refuses. A join that succeeds when nothing
was sent is not a join, it is a coincidence with good manners.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, NamedTuple

#: The gateway's metrics store. The live gateway owns and writes it; every
#: handle this module opens is read-only (see :func:`open_store`).
DEFAULT_METRICS_DB = "~/clawd/skcapstone-repos/skgateway/data/metrics.db"

#: Override for the store path, so a test or a second node can point elsewhere
#: without editing code.
METRICS_DB_ENV = "SKHARNESS_GATEWAY_METRICS_DB"

#: The headers A6.1 puts on the wire. Named here so a caller assembling a
#: request and a caller verifying the row cannot drift apart on the spelling.
SESSION_HEADER = "x-session-id"
CARD_HEADER = "x-sk-card-id"

#: Response headers the gateway now returns. Unreachable through
#: ``Sandbox.spawn`` (stdout only), useful to a direct HTTP caller.
REQ_ID_HEADER = "x-sk-req-id"
BACKEND_HEADER = "x-sk-backend"
MODEL_SERVED_HEADER = "x-sk-model-served"

MODEL_SERVED_UNOBSERVED = (
    "no table in the gateway metrics store has ever held a served model: "
    "request_log.model and token_usage.model are both the REQUESTED id and "
    "never disagree. The served model is observable in pi's stdout as "
    "responseModel (card 04970a6e), not from this path.")

# Per-axis verdict words. Distinct constants rather than booleans because the
# whole point of the control is that "absent" and "matched" must not collapse.
MATCH = "match"                   # we sent an id and the store holds the same id
ABSENT_AS_SENT = "absent-as-sent"  # we sent none and the store holds none: correct
MISSING = "missing"               # we sent one and the store holds none: LOST
MISMATCH = "mismatch"             # we sent one and the store holds a different one
INVENTED = "invented"             # we sent none and the store holds one anyway

RECOVERED = "recovered"           # exactly one backend, agreed by every source
UNRECOVERABLE = "unrecoverable"   # no source names one: a gap, not a defect
CONFLICT = "conflict"             # sources disagree: the store does not know

UNOBSERVED = "unobserved-in-store"   # the only value model_served ever takes


class GatewayStoreUnavailable(RuntimeError):
    """The metrics store could not be opened. Raised rather than returning an
    empty result, because "no rows" and "no database" are different facts and a
    verifier that cannot tell them apart passes vacuously."""


class SentIds(NamedTuple):
    """What the caller PUT ON THE WIRE, as opposed to what it found afterwards.

    All three fields default to None, and None is meaningful: a caller that sent
    no session id passes ``SentIds()`` and gets the control semantics. It is
    never correct to fill these in from the row being verified, which would make
    the check compare the store against itself.

    ``req_id`` is the gateway's own id: available to a direct HTTP caller from
    the ``x-sk-req-id`` response header, and to a store-side caller from
    :func:`find_req_ids_for_session`.
    """

    session_id: str | None = None
    card_id: str | None = None
    req_id: str | None = None


class BackendAttempt(NamedTuple):
    """One row of the per-request failover chain, from ``energy_log``.

    A single request can touch several backends. Real pair, req 185ab8359ac8:
    ``reg:ornith / measured_gpu / 2.98 J`` then ``nvidia / imputed_cloud /
    825.36 J``. Reducing that to a scalar loses both the fact of the failover
    and 99.6% of the energy, so the chain is kept whole.
    """

    backend: str | None
    basis: str | None
    joules: float | None
    node: str | None
    card_id: str | None


@dataclass(frozen=True)
class GatewayRows:
    """The raw rows the store holds for ONE req id. No interpretation."""

    req_id: str
    request_log: Mapping | None = None
    token_usage: tuple[Mapping, ...] = ()
    cost_log: tuple[Mapping, ...] = ()
    energy_log: tuple[Mapping, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping) -> "GatewayRows":
        """Build from the committed-fixture shape (also the shape
        :func:`fetch_rows` produces)."""
        def rows(key) -> tuple:
            value = data.get(key) or ()
            return (value,) if isinstance(value, Mapping) else tuple(value)

        request = data.get("request_log")
        req_id = data.get("req_id") or (request or {}).get("id") or ""
        return cls(req_id=str(req_id), request_log=request,
                   token_usage=rows("token_usage"), cost_log=rows("cost_log"),
                   energy_log=rows("energy_log"))


@dataclass(frozen=True)
class AttributionJoin:
    """What the store knows about one request, with every absence preserved.

    Every Optional field here is Optional on purpose. None means "the store does
    not hold this", and no field is ever backfilled from a neighbouring one.
    """

    req_id: str
    found: bool
    session_id: str | None = None
    card_id: str | None = None
    model_requested: str | None = None
    #: Always None. See MODEL_SERVED_UNOBSERVED; kept as a field rather than
    #: omitted so a consumer reading the join sees the absence explicitly.
    model_served: None = None
    model_served_reason: str = MODEL_SERVED_UNOBSERVED
    backend_served: str | None = None
    #: Which tables agreed on ``backend_served``, sorted. Empty when there is
    #: none to report.
    backend_sources: tuple[str, ...] = ()
    #: ``{table: backend}`` when the per-request tables disagree, else None.
    backend_conflict: dict[str, str] | None = None
    backend_attempts: tuple[BackendAttempt, ...] = ()
    status_code: int | None = None
    started_at: int | None = None
    total_ms: int | None = None
    joules_total: float | None = None

    @property
    def attributed(self) -> bool:
        """True when the store can name who this request belonged to."""
        return self.session_id is not None


@dataclass(frozen=True)
class JoinVerdict:
    """Whether the run and the row are the same event, axis by axis.

    ``ok`` means the two ends are CONSISTENT, not that everything is known. An
    unrecoverable backend leaves ``ok`` True (the store simply has a gap) while
    the ``backend`` axis says so plainly. A conflicting backend, an invented
    session id, a lost header or a req id naming a different row all make it
    False, because each of those is the store and the caller contradicting each
    other rather than merely being incomplete.
    """

    req_id: str
    ok: bool
    attributed: bool
    session_id: str
    card_id: str
    backend: str
    model_served: str = UNOBSERVED
    problems: tuple[str, ...] = ()
    join: AttributionJoin | None = field(default=None, repr=False)

    def summary(self) -> str:
        state = "ok" if self.ok else "INCONSISTENT"
        who = "attributed" if self.attributed else "anonymous"
        return (f"req {self.req_id}: {state}, {who} "
                f"(session={self.session_id}, card={self.card_id}, "
                f"backend={self.backend}, model_served={self.model_served})"
                + (f" problems={list(self.problems)}" if self.problems else ""))


# --------------------------------------------------------------- pure join

def _first(rows: Iterable[Mapping], key: str):
    for row in rows:
        value = row.get(key)
        if value is not None:
            return value
    return None


def _served_backend(rows: GatewayRows):
    """``(backend, sources, conflict)`` from the tables written once per request.

    ``request_log``, ``token_usage`` and ``cost_log`` hold at most one row per
    completed request, so a backend named in any of them is the backend that
    SERVED the call. ``energy_log`` is deliberately excluded: it holds one row
    per ATTEMPT, and on a failover it names a backend that did NOT serve the
    call. It is returned separately, whole, as ``backend_attempts``.

    Precedence is deliberately NOT used. Precedence would turn a disagreement
    into a confident answer, and a disagreement means the store does not know
    what served the call. Agreement across whichever tables have a value is the
    only thing that yields a backend.
    """
    candidates: dict[str, str] = {}
    if rows.request_log and rows.request_log.get("backend") is not None:
        candidates["request_log"] = rows.request_log["backend"]
    for table in ("token_usage", "cost_log"):
        value = _first(getattr(rows, table), "backend")
        if value is not None:
            candidates[table] = value

    if not candidates:
        return None, (), None
    distinct = set(candidates.values())
    if len(distinct) == 1:
        return distinct.pop(), tuple(sorted(candidates)), None
    return None, (), dict(sorted(candidates.items()))


def join_rows(rows: GatewayRows) -> AttributionJoin:
    """Interpret the raw rows. Pure: no I/O, no clock, no environment.

    Kept pure so the whole join is testable against committed rows in CI, where
    no gateway exists. The reachability of a live gateway must never be what
    decides whether this logic is exercised.
    """
    request = rows.request_log
    attempts = tuple(
        BackendAttempt(backend=row.get("backend"), basis=row.get("basis"),
                       joules=row.get("joules"), node=row.get("node"),
                       card_id=row.get("card_id"))
        for row in rows.energy_log)
    measured = [a.joules for a in attempts if a.joules is not None]

    if request is None:
        return AttributionJoin(req_id=rows.req_id, found=False,
                               backend_attempts=attempts,
                               joules_total=sum(measured) if measured else None)

    backend, sources, conflict = _served_backend(rows)
    # The card id rides on energy_log, which is where the gateway puts
    # x-sk-card-id. request_log has no column for it.
    card_id = _first(rows.energy_log, "card_id")
    # session_id is on request_log, and mirrored onto the usage rows. Read the
    # request row first: it is the row the req id IS, so it is the one the join
    # is about. Fall back to the mirrors only when the request row has none, so
    # a partially written record is still readable.
    session_id = request.get("session_id")
    if session_id is None:
        session_id = _first(tuple(rows.token_usage) + tuple(rows.cost_log),
                            "session_id")

    return AttributionJoin(
        req_id=rows.req_id or request.get("id", ""),
        found=True,
        session_id=session_id,
        card_id=card_id,
        model_requested=request.get("model"),
        model_served=None,                      # never derived. See the reason.
        backend_served=backend,
        backend_sources=sources,
        backend_conflict=conflict,
        backend_attempts=attempts,
        status_code=request.get("status_code"),
        started_at=request.get("started_at"),
        total_ms=request.get("total_ms"),
        joules_total=sum(measured) if measured else None,
    )


def _compare(name: str, sent, stored) -> tuple[str, list[str]]:
    """One attribution axis, four outcomes, each separately nameable."""
    if sent is None and stored is None:
        return ABSENT_AS_SENT, []
    if sent is None and stored is not None:
        return INVENTED, [
            f"{name} was invented: nothing was sent, yet the gateway row holds "
            f"{stored!r}. A row that attributes an anonymous call to a default "
            f"is worse than an unattributed one, because it looks correct."]
    if stored is None:
        return MISSING, [
            f"{name} was lost in flight: {sent!r} was sent and the gateway row "
            f"holds NULL. The header did not survive the path to the gateway."]
    if sent != stored:
        return MISMATCH, [
            f"{name} does not match: sent {sent!r}, row holds {stored!r}. "
            f"These are not the same event."]
    return MATCH, []


def verify_join(sent: SentIds, join: AttributionJoin) -> JoinVerdict:
    """Are the run and the row the same event, given what the run actually sent?

    ``sent`` must describe the wire, not the row. Passing the row's own values
    back in produces a tautology that always passes.
    """
    problems: list[str] = []

    if not join.found:
        return JoinVerdict(
            req_id=join.req_id or (sent.req_id or ""), ok=False, attributed=False,
            session_id=MISSING if sent.session_id else ABSENT_AS_SENT,
            card_id=MISSING if sent.card_id else ABSENT_AS_SENT,
            backend=UNRECOVERABLE,
            problems=(f"no request_log row for req_id "
                      f"{join.req_id or sent.req_id!r}: the gateway has no "
                      f"record of this request, so there is nothing to join.",),
            join=join)

    if sent.req_id is not None and sent.req_id != join.req_id:
        problems.append(
            f"req_id names a different row: the call reported {sent.req_id!r} "
            f"and this join was built from {join.req_id!r}.")

    session_axis, session_problems = _compare(
        "session_id", sent.session_id, join.session_id)
    card_axis, card_problems = _compare("card_id", sent.card_id, join.card_id)
    problems += session_problems + card_problems

    if join.backend_conflict is not None:
        backend_axis = CONFLICT
        problems.append(
            f"the per-request tables disagree on the served backend: "
            f"{join.backend_conflict}. The store does not know what served "
            f"this call, and picking one by precedence would invent an answer.")
    elif join.backend_served is not None:
        backend_axis = RECOVERED
    else:
        # An honest gap: no source names a backend. Not a contradiction, so it
        # does not fail the verdict, but it is visible on its own axis.
        backend_axis = UNRECOVERABLE

    return JoinVerdict(
        req_id=join.req_id,
        ok=not problems,
        # An invented id attributes nothing: the run is anonymous whatever the
        # row claims. Only a genuine MATCH earns "attributed".
        attributed=session_axis == MATCH,
        session_id=session_axis,
        card_id=card_axis,
        backend=backend_axis,
        model_served=UNOBSERVED,
        problems=tuple(problems),
        join=join,
    )


# ------------------------------------------------------------------- reading

def metrics_db_path() -> Path:
    """The store to read: ``METRICS_DB_ENV`` if set, else the fleet default."""
    override = (os.environ.get(METRICS_DB_ENV) or "").strip()
    return Path(override).expanduser() if override else Path(
        DEFAULT_METRICS_DB).expanduser()


@contextmanager
def open_store(db_path: Path | str | None = None):
    """A READ-ONLY connection to the metrics store.

    ``mode=ro`` rather than a convention of only running SELECTs: the live
    gateway owns this file and writes to it continuously, so the handle must be
    incapable of writing, not merely uninterested in it. A test asserts that a
    DELETE through this handle raises.
    """
    path = Path(db_path).expanduser() if db_path is not None else metrics_db_path()
    if not path.exists():
        raise GatewayStoreUnavailable(
            f"gateway metrics store not found at {path}. Set {METRICS_DB_ENV} "
            f"to point at it, or accept that this run cannot be joined.")
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise GatewayStoreUnavailable(
            f"cannot open gateway metrics store {path} read-only: {exc}") from exc
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


def fetch_rows(req_id: str, db_path: Path | str | None = None) -> GatewayRows:
    """Every row the store holds for ``req_id``, across all four tables."""
    with open_store(db_path) as con:
        request = con.execute(
            "SELECT * FROM request_log WHERE id = ?", (req_id,)).fetchone()

        def many(table: str) -> tuple[Mapping, ...]:
            return tuple(dict(r) for r in con.execute(
                f"SELECT * FROM {table} WHERE req_id = ? ORDER BY id", (req_id,)))

        return GatewayRows(
            req_id=req_id,
            request_log=dict(request) if request is not None else None,
            token_usage=many("token_usage"),
            cost_log=many("cost_log"),
            energy_log=many("energy_log"))


def find_req_ids_for_session(session_id: str, db_path: Path | str | None = None,
                             since_ms: int | None = None) -> tuple[str, ...]:
    """Req ids that carry ``session_id``, oldest first.

    The route a SANDBOXED caller takes: it cannot read a response header, but it
    chose the session id, so it can find its own rows by it. ``since_ms`` bounds
    the search to requests started at or after a timestamp, which matters when a
    session id is reused across a long run.
    """
    sql = "SELECT id FROM request_log WHERE session_id = ?"
    params: list = [session_id]
    if since_ms is not None:
        sql += " AND started_at >= ?"
        params.append(int(since_ms))
    with open_store(db_path) as con:
        return tuple(row[0] for row in con.execute(
            sql + " ORDER BY started_at", params))


def join_by_req_id(sent: SentIds, req_id: str | None = None,
                   db_path: Path | str | None = None) -> JoinVerdict:
    """Read the store and verify in one call. ``req_id`` defaults to
    ``sent.req_id``; raises when neither is available, rather than guessing."""
    target = req_id or sent.req_id
    if not target:
        raise ValueError(
            "no req_id to join on: pass one, or read x-sk-req-id from the "
            "response, or look the run up with find_req_ids_for_session().")
    return verify_join(sent, join_rows(fetch_rows(target, db_path=db_path)))


__all__ = [
    "ABSENT_AS_SENT", "AttributionJoin", "BACKEND_HEADER", "BackendAttempt",
    "CARD_HEADER", "CONFLICT", "DEFAULT_METRICS_DB", "GatewayRows",
    "GatewayStoreUnavailable", "INVENTED", "JoinVerdict", "MATCH",
    "METRICS_DB_ENV", "MISMATCH", "MISSING", "MODEL_SERVED_HEADER",
    "MODEL_SERVED_UNOBSERVED", "RECOVERED", "REQ_ID_HEADER", "SESSION_HEADER",
    "SentIds", "UNOBSERVED", "UNRECOVERABLE", "fetch_rows",
    "find_req_ids_for_session", "join_by_req_id", "join_rows",
    "metrics_db_path", "open_store", "verify_join",
]
