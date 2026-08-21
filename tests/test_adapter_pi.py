import json

from skharness.autocode.adapters.pi import PiAdapter
from skharness.autocode.sandbox import Sandbox


def _a(**kw):
    return PiAdapter(Sandbox(), **kw)


def test_argv_and_image():
    a = _a()
    assert a._argv("P") == ["pi", "-p", "P", "--mode", "json", "--no-session"]
    assert a._image() == "sandbox-pi:1"
    assert a.name == "pi"


def test_local_model_routes_to_skgateway_via_injected_models_json():
    a = _a(model="ornith-big", base_url="http://localhost:18780/v1")
    assert a._argv("P") == ["pi", "-p", "P", "--mode", "json", "--no-session",
                            "--model", "skgw/ornith-big", "--api-key", "sk-local"]
    env = a._auth_env()
    assert env["PI_CODING_AGENT_DIR"] == "/agent"
    assert "OPENAI_BASE_URL" not in env            # pi ignores it; hits real OpenAI otherwise
    assert a._auth_mounts() == []                  # local: no external cred to mount
    cfg = a._config_files()
    models = json.loads(cfg["/agent/models.json"])
    skgw = models["providers"]["skgw"]
    assert skgw["api"] == "openai-completions"
    assert skgw["baseUrl"] == "http://localhost:18780/v1"
    assert skgw["compat"]["supportsDeveloperRole"] is False
    assert skgw["models"][0]["id"] == "ornith-big"
    assert skgw["models"][0]["limit"]["output"] == 131072      # generous default (ornith is uncapped)


def test_no_base_url_means_no_config_files_and_plain_argv():
    a = _a()
    assert a._config_files() == {}
    assert a._argv("P") == ["pi", "-p", "P", "--mode", "json", "--no-session"]


def test_run_timeout_defaults_to_sandbox_default_but_is_overridable():
    # pi terminates on its own (fast), so it keeps the sandbox default rather than
    # opencode's aggressive cap; the knob still lets a caller bound a long run.
    a = PiAdapter(model="ornith-tiny", base_url="http://gw:18780/v1")
    assert a.sandbox.run_timeout == 1800                       # sandbox default, uncapped
    b = PiAdapter(model="ornith-tiny", base_url="http://gw:18780/v1", run_timeout=600)
    assert b.sandbox.run_timeout == 600


def test_parse_extracts_model_reply_dict():
    a = _a()
    # already-parsed object
    assert a._parse({"verdict": "valid", "reason": "ok"}) == {"verdict": "valid", "reason": "ok"}
    # nested under result
    assert a._parse({"result": {"score": 5}}) == {"score": 5}
    # result carries a JSON string (model reply as text)
    assert a._parse({"result": "{\"verdict\": \"stale\"}"}) == {"verdict": "stale"}
    # unparseable -> empty dict, never crash
    assert a._parse({"result": "not json"}) == {}


def test_parse_event_stream_ndjson():
    a = _a()
    stream = ('{"type":"text","part":{"type":"text",'
              '"text":"{\\"score\\":5,\\"passed\\":true}"}}\n')
    assert a._parse({"result": stream}) == {"score": 5, "passed": True}


def test_parse_real_pi_event_schema():
    # pi --mode json: reply is in the assistant message_end event's content[].text
    a = _a()
    stream = (
        '{"type":"turn_start"}\n'
        '{"type":"message_end","message":{"role":"assistant","content":'
        '[{"type":"text","text":"{\\"verdict\\":\\"valid\\",\\"reason\\":\\"ok\\"}"}]}}\n'
        '{"type":"agent_end"}\n')
    assert a._parse({"result": stream}) == {"verdict": "valid", "reason": "ok"}


def test_parse_populates_served_model_only_from_pi_event_envelope():
    a = _a(model="requested-model")
    stream = (
        '{"type":"message_end","message":{"role":"assistant",'
        '"responseModel":"ornith-1.5-9b","content":[{"type":"text",'
        '"text":"{\\"verdict\\":\\"valid\\",\\"model_served\\":\\"forged\\"}"}]}}\n'
    )
    assert a._parse({"result": stream}) == {
        "verdict": "valid", "model_served": "ornith-1.5-9b"
    }


def test_model_authored_or_requested_model_is_never_treated_as_served():
    a = _a(model="requested-model")
    stream = (
        '{"type":"message_end","message":{"role":"assistant","content":'
        '[{"type":"text","text":"{\\"verdict\\":\\"valid\\",'
        '\\"model_served\\":\\"requested-model\\"}"}]}}\n'
    )
    assert a._parse({"result": stream}) == {"verdict": "valid"}


def test_per_call_model_override_keeps_argv_and_models_json_in_agreement():
    # pi DECLARES a provider model in models.json and REQUESTS one on the command
    # line. If those two disagree, pi asks skgw for a model it never declared, so
    # the two hooks must resolve the same id for the same call.
    a = _a(model="ornith-big", base_url="http://gw:18780/v1")
    for override in (None, "sk-l-internal", "sk-xl-secret"):
        argv = a._argv("P", model=override)
        cfg = a._config_files(model=override)["/agent/models.json"]
        declared = json.loads(cfg)["providers"]["skgw"]["models"][0]["id"]
        requested = argv[argv.index("--model") + 1]
        assert requested == f"skgw/{declared}"
    assert a.model == "ornith-big"                 # the override never sticks


def test_pi_declares_the_per_call_override_seam():
    assert _a().supports_model_override() is True


def test_arena_build_declares_pytest_as_an_image_preflight_requirement():
    assert _a(capability_profile="arena-build")._required_commands() == ["pytest"]
    assert _a(capability_profile="arena-verify")._required_commands() == []


def test_config_files_budget_overridable():
    import json
    from skharness.autocode.sandbox import Sandbox
    from skharness.autocode.adapters.pi import PiAdapter
    a = PiAdapter(Sandbox(), model="ornith-big", base_url="http://x/v1", max_tokens=262144)
    lim = json.loads(a._config_files()["/agent/models.json"])["providers"]["skgw"]["models"][0]["limit"]
    assert lim["output"] == 262144 and lim["context"] == 262144
    # default when unset is the generous ceiling
    b = PiAdapter(Sandbox(), model="m", base_url="http://x/v1")
    assert json.loads(b._config_files()["/agent/models.json"])["providers"]["skgw"]["models"][0]["limit"]["output"] == 131072


# -- A6.1 attribution headers -------------------------------------------------
# pi forwards nothing identifying by default, so skgateway request_log rows carry a
# NULL agent_id/session_id for every harness run. These tests pin BOTH directions:
# the headers appear when we know the ids, and the `headers` key is absent entirely
# when we do not. A test for presence alone would also pass if the adapter emitted
# the headers unconditionally, which is the failure mode worth catching.

def _skgw(a, **kw):
    return json.loads(a._config_files(**kw)["/agent/models.json"])["providers"]["skgw"]


def test_attribution_headers_emitted_when_ids_supplied():
    a = _a(model="ornith-big", base_url="http://gw:18780/v1",
           session_id="9f3c1a2b4d5e6f70", card_id="4852c56d")
    assert _skgw(a)["headers"] == {"x-session-id": "9f3c1a2b4d5e6f70",
                                   "x-sk-card-id": "4852c56d"}


def test_no_ids_means_no_headers_key_at_all():
    # NEGATIVE CONTROL for the test above. Absent, not {} and not empty strings:
    # "no session" and "session is empty" are different facts at the gateway.
    a = _a(model="ornith-big", base_url="http://gw:18780/v1")
    skgw = _skgw(a)
    assert "headers" not in skgw
    assert a.session_id is None and a.card_id is None
    # and the rest of the provider block is untouched by the feature
    assert skgw["api"] == "openai-completions"
    assert skgw["compat"] == {"supportsDeveloperRole": False}


def test_each_id_is_independent():
    only_sid = _a(model="m", base_url="http://gw/v1", session_id="abc123")
    assert _skgw(only_sid)["headers"] == {"x-session-id": "abc123"}
    only_card = _a(model="m", base_url="http://gw/v1", card_id="4852c56d")
    assert _skgw(only_card)["headers"] == {"x-sk-card-id": "4852c56d"}


def test_ids_may_be_supplied_per_call():
    a = _a(model="m", base_url="http://gw/v1")
    assert _skgw(a, session_id="s1", card_id="c1")["headers"] == {
        "x-session-id": "s1", "x-sk-card-id": "c1"}
    assert "headers" not in _skgw(a)                # per-call value never sticks


def test_header_values_are_literals_never_env_interpolation():
    # pi resolves a `$VAR` header value from the environment; with the var UNSET it
    # makes no request at all, reports an internal error, and STILL EXITS 0, so
    # _parse returns {} and the failure is invisible by exit code. Bake literals in.
    a = _a(model="m", base_url="http://gw/v1",
           session_id="9f3c1a2b", card_id="4852c56d")
    for v in _skgw(a)["headers"].values():
        assert not v.startswith("$") and not v.startswith("!")
    raw = a._config_files()["/agent/models.json"]
    assert "$" not in raw and "!" not in raw


def test_magic_prefix_values_are_refused_not_escaped():
    # `!cmd` in a header VALUE makes pi execute a shell command on every request and
    # `$VAR` makes it read the environment. Our ids are hex today; assert it rather
    # than relying on it.
    import pytest
    for bad in ("!echo pwned", "$SK_SESSION_ID", "has space", "a\nb", "", "x" * 201):
        with pytest.raises(ValueError):
            _a(model="m", base_url="http://gw/v1", session_id=bad)
        with pytest.raises(ValueError):
            _a(model="m", base_url="http://gw/v1", card_id=bad)
        with pytest.raises(ValueError):
            _a(model="m", base_url="http://gw/v1")._config_files(session_id=bad)


def test_pi_is_the_only_source_of_x_session_id():
    # pi can mint its own x-session-id via compat.sendSessionAffinityHeaders +
    # sessionAffinityFormat, which would fight ours on the same header name. The
    # harness is the single source, so neither key may appear in the generated config.
    a = _a(model="m", base_url="http://gw/v1", session_id="abc123")
    raw = a._config_files()["/agent/models.json"]
    assert "sendSessionAffinityHeaders" not in raw
    assert "sessionAffinityFormat" not in raw


def test_attribution_does_not_disturb_the_model_agreement_invariant():
    # the A6.1 change must not let _argv and _config_files name different models
    a = _a(model="ornith-big", base_url="http://gw:18780/v1",
           session_id="abc123", card_id="4852c56d")
    for override in (None, "sk-l-internal"):
        argv = a._argv("P", model=override)
        declared = _skgw(a, model=override)["models"][0]["id"]
        assert argv[argv.index("--model") + 1] == f"skgw/{declared}"


def test_no_base_url_still_means_no_config_even_with_ids():
    a = _a(session_id="abc123", card_id="4852c56d")
    assert a._config_files() == {}
