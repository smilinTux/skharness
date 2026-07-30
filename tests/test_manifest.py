"""skcode's SKWorld module manifest: shape, origin-relative URLs, operator facet."""

from skharness.manifest import AUDIENCE, SCHEMA_VERSION, skcode_module_manifest


def test_manifest_ui_facet_shape():
    m = skcode_module_manifest("http://100.108.59.57:9394/")
    assert m["schemaVersion"] == SCHEMA_VERSION
    assert m["id"] == "skcode"
    assert m["grade"] == "B"
    assert m["nav"] == {"icon": "terminal", "order": 30, "label": "Code"}
    assert m["deeplinkPrefix"] == "skworld://skcode/"
    assert m["memory"] == {"opt_in": False}


def test_urls_are_origin_relative_and_not_double_slashed():
    m = skcode_module_manifest("http://host:9394/")
    assert m["entry"]["url"] == "http://host:9394/app"
    assert m["health"] == "http://host:9394/api/v1/hosts/self"
    # A base without a trailing slash yields the same (no missing/extra slash).
    m2 = skcode_module_manifest("http://host:9394")
    assert m2["entry"]["url"] == "http://host:9394/app"


def test_auth_facet_declares_audience_and_scopes():
    m = skcode_module_manifest("http://host/")
    assert m["auth"]["audience"] == AUDIENCE == "skcode"
    assert m["auth"]["scopes"] == ["skcode.stream", "skcode.inject", "skcode.dispatch"]


def test_operator_facet_matches_the_skcode_adapter_contract():
    op = skcode_module_manifest("http://host/")["operator"]
    assert op["contractVersion"] == 1
    assert op["cli"] == "skcode-hostd operator"
    assert op["repos"] == ["skharness"]
    # Mirrors operator_seat/skcode_adapter.py CONDITIONS and its standard actions.
    assert op["conditions"] == [
        "HostdReady",
        "SessionsHealthy",
        "RegistryConsistent",
        "AuthEnforced",
    ]
    assert op["proposedStandardActions"] == ["restart-hostd", "archive-stale-session"]
