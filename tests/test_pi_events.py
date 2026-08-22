from skharness.autocode.pi_events import scan_pi_events, served_model_evidence


def test_pi_session_envelope_is_valid_and_does_not_invalidate_terminal_assessment():
    raw = b'\n'.join(
        [
            b'{"type":"session","version":3,"id":"session-1","cwd":"/work"}',
            b'{"type":"agent_start"}',
            b'{"type":"message_end","message":{"role":"assistant","content":[],"responseModel":"ornith-1.5-9b"}}',
            b'{"type":"agent_settled"}',
        ]
    )
    scan = scan_pi_events(raw)
    assert scan.incomplete is False
    assert len(scan.events) == 4
    assert served_model_evidence(scan) == ("ornith-1.5-9b", None)
