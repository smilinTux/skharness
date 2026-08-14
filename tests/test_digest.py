"""skharness.digest: the read-only view over the published skwatchdog digest
artifact (card C-14a). Never touches the real fleet watchdog state -- every
test here writes its own tmp_path fixture file, per the card's explicit
instruction.
"""
from __future__ import annotations

import json
from pathlib import Path

from skharness.digest import default_digest_path, read_latest_digest


# ---- read_latest_digest: fail-safe reads ----------------------------------

def test_missing_file_returns_none_not_an_error(tmp_path):
    missing = tmp_path / "latest" / "digest.json"
    assert read_latest_digest(missing) is None


def test_missing_parent_directory_returns_none_not_an_error(tmp_path):
    # No "latest" directory has ever been created here -- the state before
    # the watchdog's first-ever publish.
    missing = tmp_path / "digests" / "latest" / "digest.json"
    assert read_latest_digest(missing) is None


def test_present_file_is_returned_byte_for_byte(tmp_path):
    path = tmp_path / "digest.json"
    payload = json.dumps({
        "date": "2026-08-14",
        "headline": "2 problems, 1 notable item, 3 quiet info events across 2 sources.",
        "problems": [{
            "ts": "2026-08-14T06:12:03Z", "source": "fleet", "kind": "ServiceCrashLoop",
            "object": "skchat-daemon@dot41", "severity": "problem",
            "summary": "skchat daemon on .41 restarted 4 times.",
            "link": {"uri": "skworld://skchat/ops/daemon", "http": "https://atlas.skworld.io/"},
            "ref": "fleet:dot41:2026-08-14T06:12:03Z:ServiceCrashLoop:skchat-daemon",
        }],
        "notable": [],
        "info_counts": {"scheduler": 3},
        "per_source": {},
    }, indent=2, ensure_ascii=False, sort_keys=True)
    path.write_text(payload, encoding="utf-8")

    raw = read_latest_digest(path)
    assert raw is not None
    # Byte-faithful: exactly what was on disk, not reparsed/reserialized.
    assert raw == payload.encode("utf-8")
    # And it is still valid JSON carrying the five keys C-9's Dart parser reads.
    decoded = json.loads(raw)
    assert set(["date", "headline", "problems", "notable", "info_counts"]) <= set(decoded)


def test_malformed_json_is_returned_as_is_not_hidden_or_fixed(tmp_path):
    # A corrupt artifact is a DIFFERENT fact than "nothing published yet"
    # (card C-14a's hard rule): this module never parses, so it can never be
    # the thing that "fixes" or fabricates a bad digest.
    path = tmp_path / "digest.json"
    path.write_text("{not valid json at all", encoding="utf-8")
    raw = read_latest_digest(path)
    assert raw == b"{not valid json at all"


def test_permission_error_degrades_to_none(tmp_path, monkeypatch):
    path = tmp_path / "digest.json"
    path.write_text("{}", encoding="utf-8")

    def _boom(self, *a, **kw):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", _boom)
    assert read_latest_digest(path) is None


# ---- default_digest_path: env override + default -----------------------

def test_default_digest_path_honors_env_override(monkeypatch, tmp_path):
    fake = tmp_path / "custom" / "digest.json"
    monkeypatch.setenv("SKCODE_WATCHDOG_DIGEST_PATH", str(fake))
    assert default_digest_path() == fake


def test_default_digest_path_falls_back_to_skcapstone_watchdog(monkeypatch):
    monkeypatch.delenv("SKCODE_WATCHDOG_DIGEST_PATH", raising=False)
    path = default_digest_path()
    assert str(path).endswith(
        str(Path(".skcapstone") / "watchdog" / "digests" / "latest" / "digest.json")
    )
