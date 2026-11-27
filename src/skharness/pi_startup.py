"""Bounded startup profiles for interactive and SKFleet Pi sessions.

This module is intentionally pure: callers build the prompt and MCP surface,
then pass them to Pi. It never reads project-wide instruction files. Worker
context is card scoped and direct MCP access is opt-in and allowlisted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

# These gates are included in both profiles and must not be removed by callers.
SAFETY_INVARIANTS = (
    "Protect legal matter data and secrets; never disclose credentials.",
    "Honor CapAuth authorization and repository safety gates.",
    "Do not deploy, enable live execution, mutate protected configuration, or automerge.",
)


@dataclass(frozen=True)
class StartupProfile:
    name: str
    instruction_bundle: str
    mcp_mode: str
    direct_tools: tuple[str, ...]
    load_project_instructions: bool

    @property
    def mcp_config(self) -> dict[str, object]:
        """Serializable MCP policy consumed by Pi adapters."""
        return {"mode": self.mcp_mode, "direct_tools": list(self.direct_tools)}


def _tools(values: Iterable[str]) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(str(v).strip() for v in values if str(v).strip()))
    if any("*" in value for value in result):
        raise ValueError("direct MCP tools must be explicitly allowlisted")
    return result


def interactive_profile(*, safety_instructions: str = "", direct_tools: Iterable[str] = ()) -> StartupProfile:
    """Convenient interactive context with on-demand, not eagerly registered, MCP."""
    text = safety_instructions.strip()
    bundle = "\n".join((*SAFETY_INVARIANTS, text)) if text else "\n".join(SAFETY_INVARIANTS)
    return StartupProfile("interactive", bundle, "proxy", _tools(direct_tools), True)


def worker_profile(*, card_id: str, card_title: str, card_brief: str = "", direct_tools: Iterable[str] = ()) -> StartupProfile:
    """Compact card-scoped worker context; broad AGENTS/CLAUDE files are excluded."""
    if not card_id or not card_title:
        raise ValueError("worker startup requires card_id and card_title")
    brief = card_brief.strip() or "No additional brief supplied."
    bundle = "\n".join((f"Card: {card_id}", f"Task: {card_title}", f"Brief: {brief}", *SAFETY_INVARIANTS))
    return StartupProfile("worker", bundle, "proxy", _tools(direct_tools), False)


def coordination_brief(*, card_id: str, state: str, next_action: str) -> str:
    """Produce a concise card-specific startup coordination check."""
    if not card_id or not state or not next_action:
        raise ValueError("card_id, state, and next_action are required")
    return f"card:{card_id} state:{state} next:{next_action}"


def startup_environment(profile: StartupProfile) -> Mapping[str, str]:
    """Environment contract for adapters, avoiding implicit broad context loading."""
    return {
        "PI_STARTUP_PROFILE": profile.name,
        "PI_MCP_MODE": profile.mcp_mode,
        "PI_DIRECT_MCP_TOOLS": ",".join(profile.direct_tools),
        "PI_LOAD_PROJECT_INSTRUCTIONS": "1" if profile.load_project_instructions else "0",
    }
