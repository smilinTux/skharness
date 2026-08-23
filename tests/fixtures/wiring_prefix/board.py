"""Board.record_success and the success-memory read path (instance 2).

25 passing tests, ZERO production callers. Nothing on any live path invokes it.
"""
from __future__ import annotations


class Board:
    def _store(self, card_id: str, mem: dict) -> None:
        self._memory = (card_id, mem)

    def record_success(self, card_id: str, mem: dict) -> None:
        self._store(card_id, mem)

    def read_success_memory(self, card_id: str) -> dict | None:
        return getattr(self, "_memory", None)
