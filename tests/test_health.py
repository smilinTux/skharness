from skharness.autocode import health


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SKHARNESS_HEALTH_PATH", str(tmp_path / "health.jsonl"))


def test_record_and_recent_roundtrip(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    health.record("assess_ok", verdict="valid", task="t1")
    health.record("assess_inconclusive", task="t2")
    evs = health.recent()
    assert [e["kind"] for e in evs] == ["assess_ok", "assess_inconclusive"]
    assert evs[0]["verdict"] == "valid" and "ts" in evs[0]


def test_recent_filters_by_kind(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    for _ in range(3):
        health.record("run_ok")
    health.record("run_inconclusive", attempts=6)
    assert len(health.recent("run_ok")) == 3
    assert len(health.recent("run_inconclusive")) == 1


def test_rate_is_the_decline_signal(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    for _ in range(3):
        health.record("run_inconclusive")
    health.record("run_ok")
    r = health.rate("run_inconclusive", over=("run_inconclusive", "run_ok"))
    assert abs(r - 0.75) < 1e-9


def test_rate_zero_when_no_data(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert health.rate("run_inconclusive", over=("run_inconclusive", "run_ok")) == 0.0


def test_record_never_raises_on_unserialisable(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    health.record("weird", obj=object())          # not JSON-serialisable
    ev = health.recent("weird")[0]
    assert "obj" in ev                             # coerced to repr, not dropped


def test_record_is_silent_when_path_unwritable(tmp_path, monkeypatch):
    # point at a path whose parent cannot be created (a file used as a dir)
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    monkeypatch.setenv("SKHARNESS_HEALTH_PATH", str(blocker / "nope.jsonl"))
    health.record("x")                             # must not raise
    assert health.recent() == []
