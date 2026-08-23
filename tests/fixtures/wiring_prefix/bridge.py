"""The agentrun bridge -- the ONLY caller of the recorder (instance 1).

This is a live entry, but NOT the gated orchestrator path. Because run_once
never reaches record_run, the recorder is unreachable from the path the
inventory declares as its live entry point.
"""
from __future__ import annotations

from . import autopilot_cost


def execute_dispatch(context: dict) -> None:
    autopilot_cost.record_run(card_id=context.get("ref", ""), tokens=0)
