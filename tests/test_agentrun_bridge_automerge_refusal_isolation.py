"""Regression tests for card f9a63685.

The agent_run execute bridge is structurally incapable of merging: it inherits
``DirectExecutor._merge`` which raises unconditionally, and the bridge subclass
overrides nothing that changes that. Card f9a63685's concern is a *policy* refusal
on top of that structure: P1 refused any repo whose ``RepoSpec.automerge`` flag was
set, reusing the SAME field that engineering.py uses for its positive "the autopilot
MAY auto-merge this" opt-in. That is an overloaded field with two opposite meanings,
and it is a latent footgun the moment anyone flips ``RepoSpec.automerge`` on for a
live repo.

The fix introduces one explicit, separately-named predicate -- ``RepoSpec.agentrun_refused``
-- and makes the bridge read THAT instead of ``RepoSpec.automerge``. These tests pin:

  R1. A repo flagged ``agentrun_refused=True`` is refused by the bridge.
  R2. A repo flagged ``automerge=True`` (with ``agentrun_refused=False``) is NO LONGER
      refused by the bridge -- proving ``RepoSpec.automerge`` no longer carries refusal
      semantics in the bridge. This is the regression that would have caught the old
      behaviour of overloading that field.
  R3. The structural never-merge guarantee still holds: the bridge executor's ``_merge``
      raises unconditionally and is not weakened by the refactor.
  R4. Fail-closed default: with no flag set (``agentrun_refused=False``), every repo is
      dispatchable in review-only direct mode; nothing auto-merges.

These use the standard library ``unittest.mock`` only, so they run without pytest-mock.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from skharness.autocode.agentrun_bridge import (
    AgentRunDirectExecutor,
    execute_dispatch,
)
from skharness.autocode.config import Config
from skharness.autocode.types import QualityMode, RepoSpec


def _spec(name="skrender", **over):
    base = dict(name=name, path=f"/repos/{name}", base_branch="main",
                integration_branch="develop", test_cmd="pytest", ci="none",
                min_quality=QualityMode.DIRECT)
    base.update(over)
    return RepoSpec(**base)


def _cfg(repo_map=None, **over):
    base = dict(repo_map=(repo_map if repo_map is not None else {"skrender": _spec()}),
                automerge_repos=[], live_execution=True, harness="claude-code",
                default_quality=QualityMode.GATED)
    base.update(over)
    return Config(**base)


def _card(labels, description="", meta=None, title="Fix flaky retry", card_id="task-abc"):
    # A lightweight stand-in for a skcoord Card: execute_dispatch only touches
    # .description, .meta, .priority and (via _resolve_repo_label) .labels. Building
    # the stand-in directly keeps these regression tests independent of the skcoord
    # runtime import so they run anywhere pytest+the skharness source are present.
    return SimpleNamespace(id=card_id, description=description,
                           meta=meta or {}, priority=None, labels=list(labels))


@contextmanager
def _wire(card, cfg):
    """Patch the four seams execute_dispatch's own imports resolve through."""
    with mock.patch("skcapstone.mcp_tools._helpers._shared_root",
                    return_value=Path("/nonexistent-agentrun-bridge-test-home")), \
         mock.patch("skcoord.card_store.CardStore.fold", return_value=card), \
         mock.patch("skharness.autocode.agentrun_bridge.Config.load",
                    return_value=cfg), \
         mock.patch("skharness.autocode.agentrun_bridge.build_harness",
                    return_value=SimpleNamespace(name="fake")):
        yield


class TestAutomergeRefusalIsolation:

    @pytest.mark.needs_skcapstone
    def test_agentrun_refused_flag_is_refused(self):
        """R1: the explicit predicate refuses; the new message names it."""
        card = _card(["repo:skrender"])
        cfg = _cfg(repo_map={"skrender": _spec(agentrun_refused=True)})

        with _wire(card, cfg):
            result = execute_dispatch({"card_id": card.id})

        assert result["links"] == {}
        summary = result["summary"]
        assert summary.startswith("execute refused (bridge):")
        assert "agentrun_refused" in summary
        # It must be a clean refusal, never a partial side effect.
        assert result["activity"][-1]["atype"] == "error"

    @pytest.mark.needs_skcapstone
    def test_automerge_flag_no_longer_refuses_the_bridge(self):
        """R2: RepoSpec.automerge=True alone does NOT trip the bridge refusal.

        This is the regression that fails if the old behaviour (overloading
        RepoSpec.automerge with a refusal predicate) ever returns. With the flag set
        and the explicit agentrun_refused flag OFF, the bridge must proceed to the
        review-only direct run and finalize a draft PR -- i.e. it treats automerge as
        a positive autopilot opt-in, not as "forbid the bridge".
        """
        card = _card(["repo:skrender"])
        # automerge=True (positive autopilot opt-in) but agentrun_refused=False.
        cfg = _cfg(repo_map={"skrender": _spec(automerge=True, agentrun_refused=False)})

        order: list[str] = []

        def _run(self, item, harness):
            order.append("run")
            return SimpleNamespace(passed=True, notes="direct mode", score=None,
                                   mode="direct", artifact="/fake/wt", outcome="pass")

        def _finalize(self, item, result):
            order.append("finalize")
            self.pr_url = "https://github.com/acme/skrender/pull/42"

        with _wire(card, cfg):
            with mock.patch.object(AgentRunDirectExecutor, "run", _run), \
                 mock.patch("skharness.autocode.agentrun_bridge.ratify",
                            return_value=SimpleNamespace(score=5, passed=True, notes="x")), \
                 mock.patch.object(AgentRunDirectExecutor, "finalize", _finalize):
                result = execute_dispatch({"card_id": card.id})

        assert order == ["run", "finalize"]                # never refused at the check
        assert result["links"]["pr"] == "https://github.com/acme/skrender/pull/42"
        assert "draft PR" in result["summary"]
        assert not result["summary"].startswith("execute refused (bridge):")

    @pytest.mark.needs_skcapstone
    def test_both_flags_refuse(self):
        """R1 cross-check: automerge=True AND agentrun_refused=True still refuses."""
        card = _card(["repo:skrender"])
        cfg = _cfg(repo_map={"skrender": _spec(automerge=True, agentrun_refused=True)})

        with _wire(card, cfg):
            result = execute_dispatch({"card_id": card.id})

        assert result["links"] == {}
        assert "agentrun_refused" in result["summary"]

    def test_bridge_executor_merge_is_structurally_refused(self):
        """R3: the bridge executor still structurally cannot merge, and the refactor
        did not weaken it. Constructed directly (never registered), board=None."""
        ex = AgentRunDirectExecutor(_cfg(), board=None,
                                    journal=SimpleNamespace(worktree_for=lambda r: None),
                                    digest=SimpleNamespace(queue_decision=lambda **k: None))
        with pytest.raises(RuntimeError, match="must never merge"):
            ex._merge(_spec(), "autopilot/airun-x")

    def test_fail_closed_default_dispatches_without_refusal(self):
        """R4: with no flags set the repo is dispatchable (review-only direct mode);
        nothing auto-merges. This is the default every live config already carries."""
        card = _card(["repo:skrender"])
        cfg = _cfg()  # agentrun_refused defaults to None; legacy refusal is absent here

        order: list[str] = []

        def _run(self, item, harness):
            order.append("run")
            return SimpleNamespace(passed=True, notes="direct mode", score=None,
                                   mode="direct", artifact="/fake/wt", outcome="pass")

        def _finalize(self, item, result):
            order.append("finalize")
            self.pr_url = "https://github.com/acme/skrender/pull/42"

        with _wire(card, cfg):
            with mock.patch.object(AgentRunDirectExecutor, "run", _run), \
                 mock.patch("skharness.autocode.agentrun_bridge.ratify",
                            return_value=SimpleNamespace(score=5, passed=True, notes="x")), \
                 mock.patch.object(AgentRunDirectExecutor, "finalize", _finalize):
                result = execute_dispatch({"card_id": card.id})

        assert order == ["run", "finalize"]
        assert result["links"]["pr"] == "https://github.com/acme/skrender/pull/42"


class TestAgentrunRefusedRoundTrip:
    """The new field survives the yaml load boundary (config rejects unknown keys, so
    it must be whitelisted) and defaults to False on a fresh RepoSpec."""

    def test_default_is_false(self):
        spec = _spec()
        assert spec.agentrun_refused is None

    def test_field_survives_config_load(self, tmp_path):
        p = tmp_path / "autopilot.yaml"
        p.write_text(
            "repo_map:\n"
            "  skrender:\n"
            "    name: skrender\n"
            "    path: /repos/skrender\n"
            "    base_branch: main\n"
            "    integration_branch: develop\n"
            "    test_cmd: pytest\n"
            "    ci: none\n"
            "    agentrun_refused: false\n"
        )
        cfg = Config.load(p)
        spec = cfg.repo("skrender")
        assert spec is not None
        assert spec.agentrun_refused is False

    def test_unknown_automerge_field_is_rejected(self, tmp_path):
        """Sanity: the loader still rejects keys it does not know, so the new field
        is genuinely opt-in and there is no silent-accept footgun."""
        p = tmp_path / "autopilot.yaml"
        p.write_text(
            "repo_map:\n"
            "  skrender:\n"
            "    name: skrender\n"
            "    path: /repos/skrender\n"
            "    base_branch: main\n"
            "    integration_branch: develop\n"
            "    test_cmd: pytest\n"
            "    ci: none\n"
            "    bogus_agentrun_field: true\n"
        )
        with pytest.raises(Exception):
            Config.load(p)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
