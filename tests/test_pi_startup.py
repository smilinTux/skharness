from skharness.pi_startup import coordination_brief, interactive_profile, startup_environment, worker_profile


def test_interactive_keeps_safety_and_proxy_without_direct_registration():
    profile = interactive_profile(safety_instructions="Repository safety")
    assert profile.name == "interactive"
    assert profile.mcp_config == {"mode": "proxy", "direct_tools": []}
    assert "legal matter" in profile.instruction_bundle
    assert profile.load_project_instructions is True
    assert startup_environment(profile)["PI_LOAD_PROJECT_INSTRUCTIONS"] == "1"


def test_worker_is_card_scoped_and_explicit_tools_only():
    profile = worker_profile(card_id="abc123", card_title="Build", card_brief="focused", direct_tools=["coord.read"])
    assert profile.mcp_config == {"mode": "proxy", "direct_tools": ["coord.read"]}
    assert "Card: abc123" in profile.instruction_bundle
    assert "AGENTS.md" not in profile.instruction_bundle
    assert profile.load_project_instructions is False
    assert startup_environment(profile)["PI_DIRECT_MCP_TOOLS"] == "coord.read"


def test_worker_rejects_wildcard_and_coordination_is_compact():
    try:
        worker_profile(card_id="abc", card_title="Build", direct_tools=["*"])
    except ValueError:
        pass
    else:
        raise AssertionError("wildcard direct MCP access must be rejected")
    assert coordination_brief(card_id="abc", state="doing", next_action="test") == "card:abc state:doing next:test"
