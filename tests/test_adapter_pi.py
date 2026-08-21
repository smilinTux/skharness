import json

from skharness.autocode.adapters.pi import PiAdapter
from skharness.autocode.sandbox import Sandbox
from skharness.autocode.types import (
    HarnessProvenanceReason,
    RepoSpec,
    TaskBrief,
)


def _a(**kw):
    return PiAdapter(Sandbox(), **kw)


def _task_brief():
    repo = RepoSpec(
        name="r",
        path="/tmp/r",
        base_branch="main",
        integration_branch="int",
        test_cmd="pytest",
        ci="none",
    )
    return TaskBrief(
        task_id="t1",
        repo=repo,
        worktree="/tmp/wt",
        title="t",
        description="d",
        acceptance=[],
        prior_feedback=None,
        round=1,
    )


def test_argv_and_image():
    a = _a()
    assert a._argv("P") == ["pi", "-p", "P", "--mode", "json", "--no-session"]
    assert a._image() == "sandbox-pi:1"
    assert a.name == "pi"


def test_local_model_routes_to_skgateway_via_injected_models_json():
    a = _a(model="ornith-big", base_url="http://localhost:18780/v1")
    assert a._argv("P") == [
        "pi",
        "-p",
        "P",
        "--mode",
        "json",
        "--no-session",
        "--model",
        "skgw/ornith-big",
        "--api-key",
        "sk-local",
    ]
    env = a._auth_env()
    assert env["PI_CODING_AGENT_DIR"] == "/agent"
    assert "OPENAI_BASE_URL" not in env  # pi ignores it; hits real OpenAI otherwise
    assert a._auth_mounts() == []  # local: no external cred to mount
    cfg = a._config_files()
    models = json.loads(cfg["/agent/models.json"])
    skgw = models["providers"]["skgw"]
    assert {"baseUrl", "api", "apiKey", "compat", "models"} <= skgw.keys()
    assert skgw["api"] == "openai-completions"
    assert skgw["apiKey"] == "sk-local"
    assert skgw["baseUrl"] == "http://localhost:18780/v1"
    assert skgw["compat"]["supportsDeveloperRole"] is False
    assert skgw["models"][0]["id"] == "ornith-big"
    assert skgw["models"][0]["limit"]["output"] == 131072  # generous default (ornith is uncapped)


def test_no_base_url_means_no_config_files_and_plain_argv():
    a = _a()
    assert a._config_files() == {}
    assert a._argv("P") == ["pi", "-p", "P", "--mode", "json", "--no-session"]


def test_run_timeout_defaults_to_sandbox_default_but_is_overridable():
    # pi terminates on its own (fast), so it keeps the sandbox default rather than
    # opencode's aggressive cap; the knob still lets a caller bound a long run.
    a = PiAdapter(model="ornith-tiny", base_url="http://gw:18780/v1")
    assert a.sandbox.run_timeout == 1800  # sandbox default, uncapped
    b = PiAdapter(model="ornith-tiny", base_url="http://gw:18780/v1", run_timeout=600)
    assert b.sandbox.run_timeout == 600


def test_parse_extracts_model_reply_dict():
    a = _a()
    # already-parsed object
    assert a._parse({"verdict": "valid", "reason": "ok"}) == {"verdict": "valid", "reason": "ok"}
    # nested under result
    assert a._parse({"result": {"score": 5}}) == {"score": 5}
    # result carries a JSON string (model reply as text)
    assert a._parse({"result": '{"verdict": "stale"}'}) == {"verdict": "stale"}
    # unparseable -> empty dict, never crash
    assert a._parse({"result": "not json"}) == {}


def test_parse_event_stream_ndjson():
    a = _a()
    stream = '{"type":"text","part":{"type":"text","text":"{\\"score\\":5,\\"passed\\":true}"}}\n'
    assert a._parse({"result": stream}) == {"score": 5, "passed": True}


def test_parse_real_pi_event_schema():
    # pi --mode json: reply is in the assistant message_end event's content[].text
    a = _a()
    stream = (
        '{"type":"turn_start"}\n'
        '{"type":"message_end","message":{"role":"assistant","content":'
        '[{"type":"text","text":"{\\"verdict\\":\\"valid\\",\\"reason\\":\\"ok\\"}"}]}}\n'
        '{"type":"agent_end"}\n'
    )
    assert a._parse({"result": stream}) == {"verdict": "valid", "reason": "ok"}


def test_parse_populates_served_model_only_from_pi_event_envelope():
    a = _a(model="requested-model")
    stream = (
        '{"type":"message_end","message":{"role":"assistant",'
        '"responseModel":"ornith-1.5-9b","content":[{"type":"text",'
        '"text":"{\\"verdict\\":\\"valid\\",\\"model_served\\":\\"forged\\"}"}]}}\n'
    )
    assert a._parse({"result": stream}) == {"verdict": "valid", "model_served": "ornith-1.5-9b"}


def test_parse_uses_the_final_provider_owned_served_model():
    a = _a(model="requested-model")
    stream = (
        '{"type":"message_end","message":{"role":"assistant",'
        '"responseModel":"tool-turn-model","content":[]}}\n'
        '{"type":"message_end","message":{"role":"assistant",'
        '"responseModel":"final-model","content":[{"type":"text",'
        '"text":"{\\"verdict\\":\\"valid\\"}"}]}}\n'
    )
    assert a._parse({"result": stream}) == {"verdict": "valid", "model_served": "final-model"}


def test_parse_binds_reply_to_its_own_event_not_later_model_metadata():
    stream = (
        '{"type":"message_end","message":{"role":"assistant",'
        '"responseModel":"model-a","content":[{"type":"text",'
        '"text":"{\\"verdict\\":\\"valid\\"}"}]}}\n'
        '{"type":"message_end","message":{"role":"assistant",'
        '"responseModel":"model-b","content":[{"type":"text",'
        '"text":"{\\"verdict\\":\\"obsolete\\"}"}]}}\n'
    )
    assert _a()._parse({"result": stream}) == {"verdict": "valid", "model_served": "model-a"}


def test_model_authored_or_requested_model_is_never_treated_as_served():
    a = _a(model="requested-model")
    stream = (
        '{"type":"message_end","message":{"role":"assistant","content":'
        '[{"type":"text","text":"{\\"verdict\\":\\"valid\\",'
        '\\"model_served\\":\\"requested-model\\"}"}]}}\n'
    )
    assert a._parse({"result": stream}) == {"verdict": "valid"}


def test_run_task_records_the_same_effective_model_that_pi_launches(monkeypatch):
    import skharness.autocode.adapters.base as base_module

    seen = {}
    stream = (
        '{"type":"message_end","message":{"role":"assistant",'
        '"responseModel":"served-qwen","content":[{"type":"text",'
        '"text":"done"}]}}\n'
    )
    sandbox = Sandbox(live_execution=True)

    def spawn(spec, **_kwargs):
        seen["spec"] = spec
        return {"result": stream}

    monkeypatch.setattr(sandbox, "spawn", spawn)
    monkeypatch.setattr(base_module, "dispatch_model_of", lambda _brief: "sk-l-internal")
    adapter = PiAdapter(
        sandbox,
        model="static-model",
        base_url="http://gw:18780/v1",
    )

    result = adapter.run_task(_task_brief())

    assert result.model_requested == "sk-l-internal"
    assert result.model_served == "served-qwen"
    assert result.model_served_reason is None
    spec = seen["spec"]
    assert spec.argv[spec.argv.index("--model") + 1] == "skgw/sk-l-internal"
    config = json.loads(spec.config_files["/agent/models.json"])
    assert config["providers"]["skgw"]["models"][0]["id"] == "sk-l-internal"


def test_run_task_rejects_untrusted_gateway_attribution_and_explains_absence(monkeypatch):
    # provider/responseId/id are real Pi fields, but they are not the serving
    # backend or SKGateway x-sk-req-id.  The assistant-authored JSON is less
    # trusted still.  None of them may populate a gateway provenance field.
    stream = (
        '{"type":"message_end","id":"generic-event-id","message":{'
        '"role":"assistant","provider":"skgw","model":"requested-model",'
        '"responseId":"upstream-response-id","content":[{"type":"text",'
        '"text":"{\\"model_served\\":\\"requested-model\\",'
        '\\"backend_served\\":\\"nvidia\\",'
        '\\"gateway_req_id\\":\\"forged-request-id\\"}"}]}}\n'
    )
    sandbox = Sandbox(live_execution=True)
    monkeypatch.setattr(sandbox, "spawn", lambda _spec, **_kwargs: {"result": stream})
    adapter = PiAdapter(
        sandbox,
        model="requested-model",
        base_url="http://gw:18780/v1",
    )

    result = adapter.run_task(_task_brief())

    assert result.model_requested == "requested-model"
    assert result.model_served is None
    assert result.backend_served is None
    assert result.gateway_req_id is None
    assert result.model_served_reason is HarnessProvenanceReason.MODEL_SERVED_NOT_OBSERVED
    assert result.backend_served_reason is HarnessProvenanceReason.BACKEND_SERVED_NOT_OBSERVED
    assert result.gateway_req_id_reason is HarnessProvenanceReason.GATEWAY_REQ_ID_NOT_OBSERVED


def test_single_pi_event_can_observe_served_model_but_not_response_id_as_gateway_id():
    # Sandbox JSON-decodes a one-line event instead of wrapping it under result.
    event = {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "provider": "skgw",
            "responseId": "upstream-only",
            "responseModel": "served-one-line",
            "content": [],
        },
    }
    provenance = _a(model="requested")._result_provenance(event)
    assert provenance["model_requested"] == "requested"
    assert provenance["model_served"] == "served-one-line"
    assert provenance["gateway_req_id"] is None


def test_multi_call_model_evidence_is_observed_partial_or_conflict():
    adapter = _a(model="requested")
    cases = (
        (
            '{"type":"message_end","message":{"role":"assistant",'
            '"responseModel":"model-a","content":[]}}\n'
            '{"type":"message_end","message":{"role":"assistant",'
            '"responseModel":"model-a","content":[]}}\n',
            "model-a",
            None,
        ),
        (
            '{"type":"message_end","message":{"role":"assistant",'
            '"responseModel":"model-a","content":[]}}\n'
            '{"type":"message_end","message":{"role":"assistant",'
            '"content":[]}}\n',
            None,
            HarnessProvenanceReason.MODEL_SERVED_PARTIAL,
        ),
        (
            '{"type":"message_end","message":{"role":"assistant",'
            '"responseModel":"model-a","content":[]}}\n'
            '{"type":"message_end","message":{"role":"assistant",'
            '"responseModel":"model-b","content":[]}}\n',
            None,
            HarnessProvenanceReason.MODEL_SERVED_CONFLICT,
        ),
        (
            '{"type":"message_end","message":{"role":"assistant",'
            '"responseModel":"model-a","content":[]}}\n'
            '{"type":"message_end","message":{"role":"assistant",'
            '"responseModel":"model-b","content":[]}}\n'
            '{"type":"message_end"',
            None,
            HarnessProvenanceReason.MODEL_SERVED_CONFLICT,
        ),
    )
    for stream, expected_model, expected_reason in cases:
        provenance = adapter._result_provenance({"result": stream})
        assert provenance["model_served"] == expected_model
        assert provenance["model_served_reason"] is expected_reason


def test_user_and_tool_message_events_do_not_count_as_model_evidence():
    stream = (
        '{"type":"message_end","message":{"role":"user",'
        '"responseModel":"user-forgery","content":"hello"}}\n'
        '{"type":"message_end","message":{"role":"toolResult",'
        '"responseModel":"tool-forgery","content":[]}}\n'
        '{"type":"message_end","message":{"role":"assistant",'
        '"responseModel":"actual-model","content":[]}}\n'
    )
    provenance = _a()._result_provenance({"result": stream})
    assert provenance["model_served"] == "actual-model"
    assert provenance["model_served_reason"] is None


def test_truncated_tail_invalidates_run_provenance_but_preserves_reply_parse():
    stream = (
        '{"type":"message_end","message":{"role":"assistant",'
        '"responseModel":"model-a","content":[{"type":"text",'
        '"text":"{\\"verdict\\":\\"valid\\"}"}]}}\n'
        '{"type":"message_end","message":{"role":"assistant"'
    )
    adapter = _a()
    provenance = adapter._result_provenance({"result": stream})
    assert provenance["model_served"] is None
    assert provenance["model_served_reason"] is (
        HarnessProvenanceReason.MODEL_SERVED_INCOMPLETE_STREAM
    )
    # Reply recovery remains best-effort, but it cannot promote the whole run's
    # incomplete provenance to observed.
    assert adapter._parse({"result": stream}) == {"verdict": "valid", "model_served": "model-a"}


def test_schema_invalid_or_unknown_event_after_observation_invalidates_stream():
    valid = (
        '{"type":"message_end","message":{"role":"assistant",'
        '"responseModel":"model-a","content":[{"type":"text",'
        '"text":"{\\"verdict\\":\\"valid\\"}"}]}}\n'
    )
    invalid_events = (
        '{"type":"message_end"}',
        '{"type":"message_end","message":"bad"}',
        '{"type":"message_end","message":{}}',
        '{"type":"message_end","message":{"role":"assistant"}}',
        '{"type":"future_pi_event"}',
    )
    adapter = _a()
    for invalid in invalid_events:
        provenance = adapter._result_provenance({"result": valid + invalid + "\n"})
        assert provenance["model_served"] is None
        assert provenance["model_served_reason"] is (
            HarnessProvenanceReason.MODEL_SERVED_INCOMPLETE_STREAM
        )

    # Recovery may still decode assistant text inside an unknown envelope, but
    # generic parsing strips its provenance because the pinned scanner rejected it.
    unknown = json.dumps(
        {
            "type": "future_pi_event",
            "message": {
                "role": "assistant",
                "responseModel": "untrusted-future-model",
                "content": [{"type": "text", "text": '{"verdict":"valid"}'}],
            },
        }
    )
    assert adapter._parse({"result": unknown}) == {"verdict": "valid"}


def test_malformed_or_plain_stdout_is_never_model_provenance():
    adapter = _a(model="requested")
    for body in ("not-json\n", '{"verdict":"valid"}\n'):
        provenance = adapter._result_provenance({"result": body})
        assert provenance["model_served"] is None
        assert provenance["model_served_reason"] is (
            HarnessProvenanceReason.MODEL_SERVED_INCOMPLETE_STREAM
        )
    direct = adapter._result_provenance({"verdict": "valid"})
    assert direct["model_served"] is None
    assert direct["model_served_reason"] is (
        HarnessProvenanceReason.MODEL_SERVED_INCOMPLETE_STREAM
    )
    # The valid plain JSON reply remains parse-compatible, but authored route
    # claims are still stripped and contribute no provider observation.
    assert adapter._parse({"result": '{"verdict":"valid","model_served":"requested"}'}) == {
        "verdict": "valid"
    }


def test_blank_model_ids_fail_closed_before_argv_config_or_result_can_diverge():
    import pytest

    for blank in ("", " ", "\t\n"):
        with pytest.raises(ValueError, match="model id must not be blank"):
            _a(model=blank)
        adapter = _a(model="static-model", base_url="http://gw/v1")
        with pytest.raises(ValueError, match="model id must not be blank"):
            adapter._argv("P", model=blank)
        with pytest.raises(ValueError, match="model id must not be blank"):
            adapter._config_files(model=blank)
        with pytest.raises(ValueError, match="model id must not be blank"):
            _a()._config_files(model=blank)
        with pytest.raises(ValueError, match="model id must not be blank"):
            adapter._result_provenance({}, model=blank)


def test_padded_nonblank_model_id_is_normalized_once_for_every_representation():
    adapter = _a(model="  ornith-big  ", base_url="http://gw/v1")
    assert adapter.model == "ornith-big"
    argv = adapter._argv("P")
    assert argv[argv.index("--model") + 1] == "skgw/ornith-big"
    declared = json.loads(adapter._config_files()["/agent/models.json"])
    assert declared["providers"]["skgw"]["models"][0]["id"] == "ornith-big"
    assert adapter._result_provenance({})["model_requested"] == "ornith-big"


def test_parse_strips_assistant_provenance_from_every_raw_shape():
    claims = {
        "model_requested": "forged-request",
        "model_served": "forged-model",
        "backend_served": "forged-backend",
        "gateway_req_id": "forged-gateway-id",
        "model_served_reason": "forged-reason",
    }
    adapter = _a()
    assert adapter._parse({"verdict": "valid", **claims}) == {"verdict": "valid"}
    assert adapter._parse({"result": {"score": 5, **claims}}) == {"score": 5}
    assert adapter._parse({"result": claims}) == {}
    assert adapter._parse({"result": json.dumps({"verdict": "valid", **claims})}) == {
        "verdict": "valid"
    }

    event = {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "responseModel": "observed-model",
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "verdict": "valid",
                            **claims,
                        }
                    ),
                }
            ],
        },
    }
    assert adapter._parse({"result": json.dumps(event) + "\n"}) == {
        "verdict": "valid",
        "model_served": "observed-model",
    }


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
    assert a.model == "ornith-big"  # the override never sticks


def test_pi_declares_the_per_call_override_seam():
    assert _a().supports_model_override() is True


def test_arena_build_declares_pytest_as_an_image_preflight_requirement():
    assert _a(capability_profile="arena-build")._required_commands() == ["pytest"]
    assert _a(capability_profile="arena-build")._required_checks() == [
        ["/usr/local/bin/skharness-pi-python-test-preflight"]
    ]
    assert _a(capability_profile="arena-verify")._required_commands() == []
    assert _a(capability_profile="arena-verify")._required_checks() == []


def test_config_files_budget_overridable():
    import json
    from skharness.autocode.sandbox import Sandbox
    from skharness.autocode.adapters.pi import PiAdapter

    a = PiAdapter(Sandbox(), model="ornith-big", base_url="http://x/v1", max_tokens=262144)
    lim = json.loads(a._config_files()["/agent/models.json"])["providers"]["skgw"]["models"][0][
        "limit"
    ]
    assert lim["output"] == 262144 and lim["context"] == 262144
    # default when unset is the generous ceiling
    b = PiAdapter(Sandbox(), model="m", base_url="http://x/v1")
    assert (
        json.loads(b._config_files()["/agent/models.json"])["providers"]["skgw"]["models"][0][
            "limit"
        ]["output"]
        == 131072
    )


# -- A6.1 attribution headers -------------------------------------------------
# pi forwards nothing identifying by default, so skgateway request_log rows carry a
# NULL agent_id/session_id for every harness run. These tests pin BOTH directions:
# the headers appear when we know the ids, and the `headers` key is absent entirely
# when we do not. A test for presence alone would also pass if the adapter emitted
# the headers unconditionally, which is the failure mode worth catching.


def _skgw(a, **kw):
    return json.loads(a._config_files(**kw)["/agent/models.json"])["providers"]["skgw"]


def test_attribution_headers_emitted_when_ids_supplied():
    a = _a(
        model="ornith-big",
        base_url="http://gw:18780/v1",
        session_id="9f3c1a2b4d5e6f70",
        card_id="4852c56d",
    )
    assert _skgw(a)["headers"] == {"x-session-id": "9f3c1a2b4d5e6f70", "x-sk-card-id": "4852c56d"}


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
        "x-session-id": "s1",
        "x-sk-card-id": "c1",
    }
    assert "headers" not in _skgw(a)  # per-call value never sticks


def test_header_values_are_literals_never_env_interpolation():
    # pi resolves a `$VAR` header value from the environment; with the var UNSET it
    # makes no request at all, reports an internal error, and STILL EXITS 0, so
    # _parse returns {} and the failure is invisible by exit code. Bake literals in.
    a = _a(model="m", base_url="http://gw/v1", session_id="9f3c1a2b", card_id="4852c56d")
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
    a = _a(
        model="ornith-big", base_url="http://gw:18780/v1", session_id="abc123", card_id="4852c56d"
    )
    for override in (None, "sk-l-internal"):
        argv = a._argv("P", model=override)
        declared = _skgw(a, model=override)["models"][0]["id"]
        assert argv[argv.index("--model") + 1] == f"skgw/{declared}"


def test_no_base_url_still_means_no_config_even_with_ids():
    a = _a(session_id="abc123", card_id="4852c56d")
    assert a._config_files() == {}
