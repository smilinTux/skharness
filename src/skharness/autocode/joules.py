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
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("skharness.autocode.joules")

# Joules charged per USD of real token spend. See module docstring for calibration.
DEFAULT_JOULE_PER_USD = 50.0

#: Env var that redirects the joule wallet, and the usage ledger that rides with
#: it, at an alternate skcapstone root. It exists for the same reason
#: ``SKAI_COST_DIR`` does: settle() is a WRITE to real economic state, and a test
#: suite must never be that write. tests/conftest.py sets it for every test, so
#: isolation is the default rather than something each test file opts into.
WALLET_HOME_ENV = "SKHARNESS_WALLET_HOME"

_PRIORITIES = ("critical", "high", "medium", "low")


class ProductionWalletInTestError(RuntimeError):
    """A test run resolved the joule wallet to a production skcapstone root.

    Deliberately loud, and deliberately raised OUTSIDE settle()'s catch-all, for
    which see the comment at the top of settle(). The failure this replaces was
    silent: the suite minted well formed joules into the operator's live ledger
    for weeks, and a balance that is two thirds pytest output looks exactly like
    a balance that is real.
    """


# --------------------------------------------------------------------------- #
# Wallet home resolution and the production guard                              #
# --------------------------------------------------------------------------- #

def _skjoule_available() -> bool:
    """True when the optional skcapstone sibling that owns the wallet is here."""
    import importlib.util

    return importlib.util.find_spec("skcapstone") is not None


def _default_wallet_root() -> Path:
    """The root JouleWallet picks on its own, read LIVE rather than at import.

    skjoule binds ``SHARED_ROOT`` into its own namespace with a ``from . import``
    at import time, so this reads the attribute off the module on every call.
    That is the value the writer will actually use, and reading it late is what
    lets a test stand up a decoy root and prove the guard sees whatever the
    writer sees, without the demonstration minting into the real ledger.
    """
    try:
        from skcapstone import skjoule

        return Path(skjoule.SHARED_ROOT).expanduser()
    except Exception:
        return Path.home() / ".skcapstone"


def _production_roots() -> set[Path]:
    """Roots holding real, operator-owned economic state."""
    roots: set[Path] = set()
    for cand in (_default_wallet_root(), Path.home() / ".skcapstone"):
        try:
            roots.add(Path(cand).expanduser().resolve())
        except Exception:
            continue
    return roots


def _in_test_run() -> bool:
    """True while pytest is driving this process."""
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


def wallet_home(home=None) -> Path | None:
    """Resolve the skcapstone root the wallet should be opened against.

    Precedence: an explicit ``home`` argument, then :data:`WALLET_HOME_ENV`, then
    None, which leaves skjoule to resolve its own default. The explicit argument
    wins so a caller that already isolated itself keeps its choice, mirroring how
    the per-file ``SKAI_COST_DIR`` fixtures still win over the conftest default.
    """
    if home is not None:
        return Path(home)
    override = os.environ.get(WALLET_HOME_ENV)
    if override:
        return Path(override).expanduser()
    return None


def assert_not_production_wallet_in_test(home=None) -> None:
    """Fail loudly if a test run is about to open a production wallet.

    Shaped after skgateway's ``assertNotProductionCacheInTest``, and here for the
    same reason that one exists. This fleet has already shipped a store where the
    READER honoured an env override and the WRITER ignored it, so every check
    passed while production was being overwritten. The lesson is that asserting
    on the resolver is not enough, so this checks the path the writer will
    actually use: ``wallet_home()`` when something overrides it, and skjoule's
    own live default when nothing does.

    A no-op outside a test run. Production must never be refused a settlement.
    """
    if not _in_test_run():
        return
    resolved = wallet_home(home)
    target = resolved if resolved is not None else _default_wallet_root()
    try:
        target = Path(target).expanduser().resolve()
    except Exception:
        return
    if target in _production_roots():
        raise ProductionWalletInTestError(
            f"refusing to open a PRODUCTION joule wallet from a test run: {target}. "
            f"settle() mints and spends real joules, so a suite that reaches this "
            f"path writes fabricated economic history into the operator's ledger. "
            f"Set {WALLET_HOME_ENV} to a throwaway directory (tests/conftest.py "
            f"does this for every test by default), or pass home=tmp_path."
        )


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

    MUST only be called on a twin-gate pass (verified work). Never raises in
    production: on any failure (skjoule absent, wallet error) it returns a
    ``recorded=False`` Economics and logs, leaving the build path untouched. The
    single exception is :class:`ProductionWalletInTestError`, which can only fire
    inside a test run.

    ``home`` resolves through :func:`wallet_home`, so ``SKHARNESS_WALLET_HOME``
    redirects both the wallet and the usage ledger. Callers that already pass an
    explicit home keep it.
    """
    # Resolve the root and run the guard BEFORE the try below. Everything after
    # that point is deliberately swallowed so accounting can never fail a correct
    # build, and a guard the swallow ate would be no guard at all. This is also
    # why the guard raises its own exception type rather than returning a flag:
    # settle()'s callers wrap it in their own try/except too, and a flag would be
    # dropped on the floor by every one of them.
    home = wallet_home(home)
    assert_not_production_wallet_in_test(home)

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
    """Resolve the UsageTracker home for *agent* (agent-scoped when available).

    Routed through :func:`wallet_home` so the env override covers the usage
    ledger too. The wallet was the loud half of this leak, but UsageTracker
    writes ``{home}/usage/tokens-{date}.json`` under the same real agent home,
    so isolating only the wallet would have left the suite still editing
    production cost telemetry.
    """
    home = wallet_home(home)
    if home is not None:
        return Path(home)
    try:
        from skcapstone.mcp_tools._helpers import _shared_root

        root = Path(_shared_root())
        agent_home = root / "agents" / agent
        return agent_home if agent_home.exists() else root
    except Exception:
        return Path.home() / ".skcapstone"
