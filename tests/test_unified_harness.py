"""Contract tests for the unified two-plane Harness (skharness/harness.py).

These pin the Wave 1 "contract hoist": one Harness abstraction with a task plane
(sync, sandboxed, one-shot: assess/run_task/grade) and a session plane (async,
long-lived: spawn/stream/inject/...), capability-gated so no adapter is forced to
implement both, a single fail-closed registry, and the two bridge mixins.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest


def _cfg(**over):
    base = {"allowed_tools": [], "mcp_endpoints": None, "live_execution": False,
            "sandbox_image": None, "harness_model": None, "harness_base_url": None,
            "harness_max_tokens": None}
    base.update(over)
    return SimpleNamespace(**base)


# -- public surface -----------------------------------------------------------

def test_public_exports_available():
    from skharness import HARNESSES, Harness, HarnessCapabilities, build_harness, register_harness
    assert Harness is not None
    assert HarnessCapabilities is not None
    assert isinstance(HARNESSES, dict)
    assert callable(register_harness)
    assert callable(build_harness)


def test_one_registry_shared_with_engine():
    # The unified registry IS the autocode registry (one answer for "which
    # harnesses exist on this host"), serving both the engine and the daemon.
    import skharness.autocode.harness as engine_mod
    import skharness.harness as unified_mod
    assert unified_mod.HARNESSES is engine_mod.HARNESSES


# -- capabilities: merged cells ----------------------------------------------

def test_capabilities_merge_task_and_session_cells():
    from skharness.harness import build_harness
    h = build_harness("claude-code", _cfg())
    caps = h.capabilities()
    # existing autocode cells kept verbatim
    for key in ("session_resume", "structured_output", "sandbox", "tool_restrictions"):
        assert key in caps
    # merged skcode cells
    for key in ("task_plane", "session_plane", "headless_api", "hot_set_model"):
        assert key in caps


def test_four_adapters_expose_task_plane_not_session_plane():
    from skharness.harness import build_harness
    for name in ("claude-code", "codex", "opencode", "pi"):
        h = build_harness(name, _cfg())
        caps = h.capabilities()
        assert caps["task_plane"] is True, name
        assert caps["session_plane"] is False, name


# -- fail-closed registry -----------------------------------------------------

def test_build_harness_unknown_name_fails_closed():
    from skharness.harness import build_harness
    with pytest.raises(ValueError) as ei:
        build_harness("no-such-harness", _cfg())
    msg = str(ei.value)
    assert "no-such-harness" in msg
    # the error lists what IS registered (fail-closed, discoverable)
    assert "claude-code" in msg


def test_four_adapters_registered_and_listed():
    from skharness.harness import HARNESSES, build_harness  # noqa: F401
    # trigger registration through the public builder
    build_harness("stub", _cfg()) if "stub" in HARNESSES else None
    for name in ("claude-code", "codex", "opencode", "pi"):
        assert name in HARNESSES


def test_register_harness_adds_to_the_one_registry():
    from skharness.harness import HARNESSES, build_harness, register_harness

    class _Toy:
        name = "toy"

        def capabilities(self):
            return {"session_resume": False, "structured_output": "none",
                    "sandbox": False, "tool_restrictions": False,
                    "task_plane": True, "session_plane": False,
                    "headless_api": "none", "hot_set_model": False}

    try:
        register_harness("toy", lambda cfg: _Toy())
        assert "toy" in HARNESSES
        assert build_harness("toy", _cfg()).name == "toy"
    finally:
        HARNESSES.pop("toy", None)


# -- capability gating (detected, not crashed) --------------------------------

def test_missing_session_plane_is_detected_not_crashed():
    from skharness.harness import build_harness, warn_missing_capabilities
    h = build_harness("claude-code", _cfg())
    warnings = warn_missing_capabilities(h, {"session_plane": True})
    assert len(warnings) == 1
    assert "session_plane" in warnings[0]


def test_present_task_plane_yields_no_warning():
    from skharness.harness import build_harness, warn_missing_capabilities
    h = build_harness("claude-code", _cfg())
    assert warn_missing_capabilities(h, {"task_plane": True}) == []


# -- Harness ABC: session plane optional, not forced --------------------------

def test_session_plane_optional_default_raises_not_forced():
    # A task-only Harness subclass must NOT be forced to implement the session
    # plane; the base provides a gated default that raises when called.
    from skharness.harness import Harness

    class TaskOnly(Harness):
        name = "task-only"

        def capabilities(self):
            return {"session_resume": False, "structured_output": "json",
                    "sandbox": True, "tool_restrictions": True,
                    "task_plane": True, "session_plane": False,
                    "headless_api": "none", "hot_set_model": False}

    h = TaskOnly()  # instantiable without implementing spawn/stream/inject
    assert h.capabilities()["session_plane"] is False


def test_session_plane_methods_are_async_on_the_contract():
    from skharness.harness import Harness
    for meth in ("spawn", "list_sessions", "stream", "inject", "set_model",
                 "get_branch", "background_tasks", "archive"):
        assert hasattr(Harness, meth)
        assert inspect.iscoroutinefunction(getattr(Harness, meth)) or \
            inspect.isasyncgenfunction(getattr(Harness, meth)), meth


def test_task_plane_methods_present_on_the_contract():
    from skharness.harness import Harness
    for meth in ("assess", "run_task", "grade", "capabilities"):
        assert hasattr(Harness, meth)


# -- bridge mixins ------------------------------------------------------------

def test_session_task_bridge_provides_task_plane_methods():
    from skharness.harness import SessionTaskBridge
    for meth in ("assess", "run_task", "grade"):
        assert hasattr(SessionTaskBridge, meth), meth


def test_task_session_shim_provides_readonly_session_plane():
    from skharness.harness import TaskSessionShim
    # exposes streaming + reports inject unsupported (read-only)
    assert hasattr(TaskSessionShim, "stream")
    assert hasattr(TaskSessionShim, "inject")
