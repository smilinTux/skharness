from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "pi-cockpit.sh"


def test_cockpit_guards_every_tmux_and_worker_creation():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "SKHARNESS_PI_SPAWN_STATE is required" in source
    assert "SKHARNESS_PI_SPAWN_ACTOR is required" in source
    assert source.count('"${PI_LAUNCH[@]}"') == 2
    assert "skharness-pi-launch --state %q" in source
    assert "--kind process" in source
    assert "--kind tmux" in source
