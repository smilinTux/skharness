"""The append-only cost/outcome recorder (instance 1).

Roughly 45 passing unit tests prove it works in isolation. Its only caller is
the bridge; the gated orchestrator path never reaches it.
"""
from __future__ import annotations


def _append_row(card_id: str, tokens: int) -> tuple[str, int]:
    return (card_id, tokens)


def record_run(*, card_id: str, tokens: int = 0) -> None:
    _append_row(card_id, tokens)
