"""SKJoule accounting for autocode builds.

Measures the *work* an autocode build does (joules minted only when the twin
gate passes = verified value) against its *real cost* (claude-code token spend,
priced by the UsageTracker), so the harness accrues a per-agent P&L and runs can
be steered toward efficiency over time.

Every skcapstone import here is LAZY and OPTIONAL: on a bare harness (no
skcapstone sibling) each entry point is a graceful no-op that returns an empty,
``recorded=False`` :class:`Economics`. Nothing in this module may raise into the
build path -- a broken wallet must never fail a correct build.

The single knob is :data:`DEFAULT_JOULE_PER_USD`: how many joules one USD of real
token spend costs the wallet. It is calibrated so an efficient ``task_complete``
build (base 25 J x priority x quality) nets positive against a typical sub-dollar
build, and drains the wallet toward zero when a build burns tokens without
shipping. Override per run via ``settle(..., joule_per_usd=...)``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

log = logging.getLogger("skharness.autocode.joules")

# Joules charged per USD of real token spend. See module docstring for calibration.
DEFAULT_JOULE_PER_USD = 50.0

_PRIORITIES = ("critical", "high", "medium", "low")


def _priority_bucket(card_priority: str | None) -> str:
    """Map a coord card priority onto an XPBridge priority bucket."""
    p = (card_priority or "medium").strip().lower()
    return p if p in _PRIORITIES else "medium"


def _quality_bucket(score: int | None) -> str:
    """Map a 0-5 grade score onto an XPBridge quality bucket.

    Only a twin-gate PASS (score 5) reaches settle(), so this normally returns
    "excellent"; the lower buckets keep the helper honest if it is reused for a
    partial credit path later.
    """
    s = int(score or 0)
    if s >= 5:
        return "excellent"
    if s == 4:
        return "good"
    if s >= 2:
        return "acceptable"
    return "needs_improvement"


@dataclass
class BuildUsage:
    """LLM usage accumulated across a build's harness rounds.

    The claude-code adapter adds one of these per model call; settle() prices the
    total. ``cost_usd`` is claude-code's own ``total_cost_usd`` when present (the
    authoritative figure); tokens drive the UsageTracker's independent estimate.
    """

    model: str = "claude-code"
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0

    def add(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        turns: int = 0,
        model: str | None = None,
    ) -> None:
        self.input_tokens += int(input_tokens or 0)
        self.output_tokens += int(output_tokens or 0)
        self.cost_usd += float(cost_usd or 0.0)
        self.turns += int(turns or 0)
        if model:
            self.model = model

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_claude_json(cls, raw: dict) -> "BuildUsage":
        """Build a BuildUsage from a claude-code ``--output-format json`` result.

        Tolerant of missing fields (older CLIs / stub replies): unknown -> 0.
        """
        u = (raw or {}).get("usage") or {}
        return cls(
            model=str((raw or {}).get("model") or "claude-code"),
            input_tokens=int(u.get("input_tokens", 0) or 0)
            + int(u.get("cache_read_input_tokens", 0) or 0)
            + int(u.get("cache_creation_input_tokens", 0) or 0),
            output_tokens=int(u.get("output_tokens", 0) or 0),
            cost_usd=float((raw or {}).get("total_cost_usd", 0.0) or 0.0),
            turns=int((raw or {}).get("num_turns", 0) or 0),
        )


@dataclass
class Economics:
    """The settled P&L of one build. ``recorded`` is False when skjoule is absent
    or the settle failed (the build is unaffected either way)."""

    agent: str
    task_ref: str
    minted: int = 0
    cost_usd: float = 0.0
    spent_joules: int = 0          # intended cost in joules (drives net/P&L)
    spent_joules_actual: int = 0   # actually debited (capped at balance floor)
    net_joules: int = 0            # minted - spent_joules (may be negative)
    joules_per_usd: float = 0.0    # efficiency metric: value per real dollar
    tokens: int = 0
    balance_after: int | None = None
    recorded: bool = False

    def as_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        if not self.recorded:
            return f"joules: not recorded ({self.task_ref})"
        return (
            f"joules: +{self.minted} -{self.spent_joules} "
            f"= net {self.net_joules:+d} "
            f"(${self.cost_usd:.4f}, {self.joules_per_usd:.1f} J/$)"
        )


def settle(
    agent: str,
    task_ref: str,
    *,
    priority: str | None,
    score: int | None,
    usage: BuildUsage,
    commit_sha: str = "",
    joule_per_usd: float = DEFAULT_JOULE_PER_USD,
    home=None,
) -> Economics:
    """Settle a PASSED build's economics: record real token cost, mint value for
    the verified work, spend the USD-equivalent joules, and return the P&L.

    MUST only be called on a twin-gate pass (verified work). Never raises: on any
    failure (skjoule absent, wallet error) it returns a ``recorded=False``
    Economics and logs, leaving the build path untouched.
    """
    econ = Economics(
        agent=agent,
        task_ref=task_ref,
        cost_usd=round(usage.cost_usd, 6),
        tokens=usage.tokens,
    )
    try:
        from skcapstone.skjoule import JouleWallet, XPBridge
        from skcapstone.usage import UsageTracker
    except Exception as exc:  # skcapstone sibling not installed -> no-op
        log.debug("skjoule unavailable; skipping build economics (%s)", exc)
        return econ

    try:
        bridge = XPBridge()
        wallet = JouleWallet(agent, home=home)

        # 1. Record the REAL token cost (authoritative USD via per-model pricing).
        if usage.tokens > 0:
            try:
                UsageTracker(home=_usage_home(agent, home)).record_usage(
                    usage.model, usage.input_tokens, usage.output_tokens
                )
            except Exception as exc:  # cost telemetry is best-effort
                log.debug("usage record failed for %s: %s", agent, exc)

        # 2. Mint joules for the verified work (twin-gate pass).
        minted = bridge.calculate_joules(
            "task_complete",
            priority=_priority_bucket(priority),
            quality=_quality_bucket(score),
        )
        proof = XPBridge.compute_proof_hash(commit_sha or task_ref)
        if minted > 0:
            wallet.mint(
                minted,
                description=f"autocode task_complete {task_ref}",
                proof_hash=proof,
            )

        # 3. Spend the USD-equivalent joules (real cost). The framework floors the
        #    balance at 0, so cap the debit; the intended cost still drives net.
        spent = int(round(usage.cost_usd * joule_per_usd))
        actual = min(spent, wallet.balance) if spent > 0 else 0
        if actual > 0:
            wallet.spend(
                actual,
                description=f"autocode llm-cost {task_ref} (${usage.cost_usd:.4f})",
                proof_hash=proof,
            )

        econ.minted = minted
        econ.spent_joules = spent
        econ.spent_joules_actual = actual
        econ.net_joules = minted - spent
        econ.joules_per_usd = (minted / usage.cost_usd) if usage.cost_usd > 0 else 0.0
        econ.balance_after = wallet.balance
        econ.recorded = True
    except Exception as exc:  # never fail the build over accounting
        log.warning("build economics failed for %s (build unaffected): %s", task_ref, exc)
    return econ


def _usage_home(agent: str, home=None):
    """Resolve the UsageTracker home for *agent* (agent-scoped when available)."""
    from pathlib import Path

    if home is not None:
        return Path(home)
    try:
        from skcapstone.mcp_tools._helpers import _shared_root

        root = Path(_shared_root())
        agent_home = root / "agents" / agent
        return agent_home if agent_home.exists() else root
    except Exception:
        return Path.home() / ".skcapstone"
