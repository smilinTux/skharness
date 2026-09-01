"""Lane 1 session-plane pool controller (Epic 6, coord card d2fbe65b).

A versioned, tested replacement for the "pi-three-lane-pool-controller"
inline session: a spawn / scale / drain / destroy verb set for a human
operator's manual pane cluster, built ON TOP of the existing guarded spawn
path (:meth:`skharness.harnesses.claude_code.ClaudeCodeHarness.spawn`), never
beside it. This module never re-checks the profile / repo-allowlist / branch
format / name-charset guards, and never persists a transcript or stops a
window itself: it delegates every actual tmux/git/worktree action to the
``Harness`` it wraps (``spawn`` / ``archive``), so those four fail-closed
guards and the transcript-first teardown discipline live in exactly one
place.

Lane boundary (AUTONOMY_ARCHITECTURE.md section 4.1, ADR-0001): this is lane
1, the manual pane cluster. The driver is a human, not an orchestrator; there
is no Ralph loop, no autoscale ceiling logic, no automerge. Concretely, this
module:

* never claims, moves, or completes a coord card (:meth:`poll_board` is
  READ-ONLY: it surfaces candidate cards for a human to act on, exactly the
  read half of the inline controller's ``skcapstone coord kanban --json``
  loop, and stops there);
* never touches ``autocode/run_record.py``, the twin gate, or
  ``automerge_repos`` / ``live_execution``;
* never drains or destroys a pane it did not itself spawn (mirrors the
  harness's own ``_spawned_sids`` scoping for inject/deny), so one
  controller instance can never reach another session sharing the same tmux
  server.

Every verb that changes pool state emits exactly one activity event through
the shared :class:`~skharness.activity.ActivityJournal`. ``authority`` is
hard-pinned to ``"observation"`` inside :meth:`ActivityJournal.publish`
itself (activity.py), not by any convention followed here, so a
prompt-injected worker pane's text can never escalate through this stream:
the controller reads the board and the harness's own session list, never a
worker's output.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from skharness.activity import ActivityContext, ActivityJournal, ActivityKind
from skharness.harness import (
    Harness,
    HarnessSession,
    SessionDescriptor,
    SpawnOwnershipError,
    SpawnRejected,
)

#: A board-query runner: argv -> stdout str. Injectable so tests never touch a
#: real coord CLI, mirroring the tmux/git ``Runner`` convention in
#: harnesses/claude_code.py (argv list, never ``shell=True``).
BoardRunner = Callable[[list[str]], str]

#: Sensible default: the same command the inline controller ran every poll.
_DEFAULT_BOARD_ARGV = ["skcapstone", "coord", "kanban", "--json"]


@dataclass
class PoolMember:
    """One pane this controller spawned and is tracking, grouped by ``lane``.

    ``lane`` is an operator-chosen grouping key (e.g. a repo name or a coord
    epic tag), not a tmux/harness concept: it is how :meth:`PoolController.scale`
    and :meth:`PoolController.destroy` know which panes belong together
    without ever looking outside panes this controller itself spawned.
    """

    sid: str
    lane: str
    repo: str
    branch: str
    session: HarnessSession
    spawned_at: float
    drained: bool = False
    drain_result: dict[str, Any] = field(default_factory=dict)


class PoolController:
    """spawn / scale / drain / destroy over one session-plane ``Harness``.

    Tracks only the panes it spawned itself (``self._pool``), keyed by sid and
    grouped by lane. Every verb that touches tmux/git delegates to
    ``self.harness`` (``spawn`` / ``archive``); this class owns none of that
    machinery, only the bookkeeping and the activity emission around it.
    """

    def __init__(
        self,
        harness: Harness,
        *,
        activity: ActivityJournal | None = None,
        board_runner: BoardRunner | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.harness = harness
        self.activity = activity
        self._board_runner = board_runner
        self._clock = clock
        self._pool: dict[str, PoolMember] = {}

    # ---- activity -------------------------------------------------------

    def _emit(
        self,
        sid: str,
        kind: ActivityKind,
        summary: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Publish one observation event. Never raises: a missing/misconfigured
        journal, or a sid that cannot form a valid ActivityContext, degrades to
        a silent no-op rather than failing a spawn/drain/destroy call that
        otherwise succeeded. ``authority`` is fixed to "observation" by
        :meth:`ActivityJournal.publish` itself; nothing here can change that.
        """
        if self.activity is None:
            return
        try:
            context = ActivityContext(session_id=sid, source="lane1-pool")
        except ValueError:
            return
        try:
            self.activity.publish(context, kind, summary=summary, data=data or {})
        except (ValueError, TypeError, OSError):
            return

    # ---- verbs ------------------------------------------------------------

    async def spawn(
        self,
        *,
        lane: str,
        repo: str,
        branch: str,
        prompt: str,
        model: str = "",
        quality: str = "sandbox",
        mode: str = "direct",
    ) -> PoolMember:
        """Spawn ONE pane in ``lane`` through the harness's guarded spawn path.

        Any :class:`~skharness.harness.SpawnRejected` the harness raises (bad
        profile, repo not on the allowlist, bad branch, bad name charset)
        propagates UNCHANGED: the guard that decides lives in the harness, not
        here, so a refused spawn is never tracked and never emits an activity
        event. Nothing is touched before the harness's own guards pass.
        """
        desc = SessionDescriptor(
            repo=repo,
            branch=branch,
            model=model,
            quality=quality,
            mode=mode,
        )
        # The controller's owned SID set is part of the harness reservation,
        # before launch. A harness that cannot honor this boundary is refused by
        # the base contract without actuating anything.
        session = await self.harness.spawn_reserved(
            desc, prompt=prompt, excluded_sids=frozenset(self._pool)
        )
        if session.sid in self._pool:
            # Contract-violating collaborator: contain only the newly returned
            # unambiguous resource identity. Never archive by the ambiguous SID.
            # Snapshot the original so an unresolved receipt proves which member
            # remains actionable and unchanged in this controller.
            original = self._pool[session.sid]
            original_state = asdict(original)
            try:
                teardown = await self.harness.teardown_owned(session.resource_id)
            except Exception as exc:  # noqa: BLE001 - preserve ownership on collaborator failure
                teardown = {
                    "resource_id": session.resource_id,
                    "teardown_succeeded": False,
                    "reason": f"exact-resource teardown raised: {exc}",
                }
            if not teardown.get("teardown_succeeded"):
                teardown_reason = str(
                    teardown.get("teardown_reason") or teardown.get("reason") or "unknown failure"
                )
                receipt = {
                    "sid": session.sid,
                    "resource_id": session.resource_id,
                    "launched": True,
                    "tracked": False,
                    "setup_succeeded": True,
                    "teardown_succeeded": False,
                    "teardown_reason": teardown_reason,
                    "original_member_preserved": self._pool.get(session.sid) is original,
                    "original_member": original_state,
                }
                raise SpawnOwnershipError(
                    f"duplicate pool member session id {session.sid!r}; "
                    f"unresolved exact-resource teardown: {teardown_reason}",
                    receipt=receipt,
                )
            raise SpawnRejected(
                f"duplicate pool member session id {session.sid!r}; contained teardown: "
                f"{teardown.get('reason', '')}"
            )
        member = PoolMember(
            sid=session.sid,
            lane=lane,
            repo=repo,
            branch=branch,
            session=session,
            spawned_at=self._clock(),
        )
        self._pool[session.sid] = member
        self._emit(
            session.sid,
            ActivityKind.STATUS,
            f"lane1 pool spawned {session.sid!r} in lane {lane!r}",
            {"lane": lane, "repo": repo, "branch": branch, "quality": quality, "mode": mode},
        )
        return member

    async def scale(
        self,
        *,
        lane: str,
        repo: str,
        branch: str,
        prompt: str,
        target: int,
        model: str = "",
        quality: str = "sandbox",
        mode: str = "direct",
    ) -> list[PoolMember]:
        """Converge the ACTIVE (undrained) pane count in ``lane`` to ``target``.

        Scale-down drains this lane's OWN oldest panes first (FIFO); scale-up
        spawns new ones with the given repo/branch/prompt. Never looks at, or
        touches, any pane outside ``lane``, or any pane this controller did
        not spawn. Returns the lane's active members after converging.
        """
        if target < 0:
            raise ValueError("scale target must be >= 0")
        active = self._active_members(lane)
        if len(active) > target:
            surplus = sorted(active, key=lambda m: m.spawned_at)[: len(active) - target]
            for member in surplus:
                await self.drain(member.sid)
        elif len(active) < target:
            for _ in range(target - len(active)):
                await self.spawn(
                    lane=lane,
                    repo=repo,
                    branch=branch,
                    prompt=prompt,
                    model=model,
                    quality=quality,
                    mode=mode,
                )
        return self._active_members(lane)

    async def drain(self, sid: str) -> dict:
        """Stop ONE pane, transcript-first, via the harness's own ``archive``.

        Scoped to panes THIS controller spawned: an unknown sid is a clean
        no-op (mirrors the harness's own ``archive``/``_spawned_sids``
        no-op shape) rather than reaching for an arbitrary tmux window. The
        actual persist-then-stop ordering is entirely the harness's; this
        method only records the pool-side bookkeeping and an activity event.
        """
        if sid not in self._pool:
            return {
                "sid": sid,
                "archived": False,
                "reason": (
                    "not a pool-tracked session (drain is scoped to panes this controller spawned)"
                ),
            }
        result = await self.harness.archive(sid)
        member = self._pool[sid]
        member.drain_result = result
        if result.get("archived") or "no live session" in str(result.get("reason", "")):
            # Either genuinely archived now, or already gone (idempotent no-op
            # from a prior drain/external stop): either way this controller
            # should not try to drain it again.
            member.drained = True
        self._emit(
            sid,
            ActivityKind.STATUS if result.get("archived") else ActivityKind.DISPOSITION,
            f"lane1 pool drained {sid!r}: archived={result.get('archived')}",
            result,
        )
        return result

    async def destroy(self, lane: str | None = None) -> list[dict]:
        """Drain every pane this controller tracks (optionally one lane only).

        Never a bare kill: each pane goes through :meth:`drain` ->
        ``harness.archive`` (persist transcript, then stop the window).
        Already-drained panes are skipped, so calling ``destroy`` twice is
        safe.
        """
        targets = [
            m for m in self._pool.values() if not m.drained and (lane is None or m.lane == lane)
        ]
        results = []
        for member in targets:
            results.append(await self.drain(member.sid))
        return results

    # ---- board polling (READ-ONLY; a human decides what to spawn) ---------

    def poll_board(
        self,
        *,
        statuses: Iterable[str] = ("doing",),
        labels: frozenset[str] = frozenset(),
        argv: list[str] | None = None,
    ) -> list[dict]:
        """Read-only coord board query surfacing candidate cards.

        Mirrors the read half of the inline controller's
        ``skcapstone coord kanban --json`` poll loop and stops there: this
        controller never claims, moves, or completes a card. Lane 1 is a
        human's tool (AUTONOMY_ARCHITECTURE.md section 4.1); this only shows
        the human what looks eligible, it never acts on it.

        Fails soft: no configured runner, a nonzero/garbled board query, or an
        unexpected JSON shape all return ``[]`` rather than raising, so a
        board-polling hiccup never blocks a spawn/scale/drain/destroy call.
        """
        if self._board_runner is None:
            return []
        wanted_statuses = set(statuses)
        try:
            raw = self._board_runner(argv or list(_DEFAULT_BOARD_ARGV))
            board = json.loads(raw)
        except (ValueError, TypeError, OSError):
            return []
        if not isinstance(board, dict):
            return []
        out: list[dict] = []
        for lane_cols in board.values():
            if not isinstance(lane_cols, dict):
                continue
            for items in lane_cols.values():
                if not isinstance(items, list):
                    continue
                for card in items:
                    if not isinstance(card, dict):
                        continue
                    if wanted_statuses and card.get("status") not in wanted_statuses:
                        continue
                    card_labels = card.get("labels")
                    if card_labels is None:
                        card_labels = card.get("tags") or []
                    if labels and not labels.intersection(card_labels):
                        continue
                    out.append(card)
        return out

    # ---- introspection ------------------------------------------------------

    def _active_members(self, lane: str) -> list[PoolMember]:
        return [m for m in self._pool.values() if m.lane == lane and not m.drained]

    def members(self, lane: str | None = None) -> list[PoolMember]:
        """All tracked panes (optionally scoped to one lane), spawned or drained."""
        return [m for m in self._pool.values() if lane is None or m.lane == lane]
