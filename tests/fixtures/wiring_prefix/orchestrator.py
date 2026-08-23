"""The gated autopilot orchestrator -- the live entry point (run_once).

Reproduces two of the eight pre-fix shapes:
  * instance 1 (record_run): the terminal-row path here NEVER calls the recorder;
    it was wired only from the bridge, so nothing on the gated path wrote a row.
  * instance 4 (CapLedger ceiling): GateResult declares no ``tokens`` field, so
    ``ledger.add(getattr(result, "tokens", 0))`` always adds zero. The cost cap
    caps nothing.
"""
from __future__ import annotations


class GateResult:
    # NOTE: no ``tokens`` attribute -- exactly the instance-4 shape.
    def __init__(self) -> None:
        self.passed = False


class CapLedger:
    def __init__(self, cap: int = 0) -> None:
        self.total = 0
        self.cap = cap

    def add(self, tokens: int = 0, usd: float = 0.0) -> None:
        self.total += tokens

    def exceeded(self) -> bool:
        return self.total > self.cap


def _select(board: object) -> list:
    return getattr(board, "items", [])


def _summarise(item: object, result: object) -> dict:
    return {"ref": getattr(item, "ref", "")}


def record_outcome_row(item: object, result: object = None) -> None:
    # The orchestrator's terminal-row path. In the pre-fix tree it did NOT reach
    # the recorder (instance 1): record_run was called only from the bridge.
    _summarise(item, result)


def run_once(board: object = None, harness: object = None,
             config: object = None) -> CapLedger:
    ledger = CapLedger(cap=1000)
    result = GateResult()
    # instance 4: always adds zero, so the ceiling never triggers.
    ledger.add(getattr(result, "tokens", 0))
    for item in _select(board):
        record_outcome_row(item, result=result)
    return ledger
