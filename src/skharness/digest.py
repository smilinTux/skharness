"""skwatchdog digest reader (card C-14a): a read-only view over the published
digest artifact, spec docs/specs/2026-08-10-skwatchdog-architecture.md
section 9 + section 6.1.

The Code section's Digest tab (card C-9, skworld-app
packages/skcode_client/lib/src/skcode_digest.dart) fetches the latest
published digest artifact and renders it. Card C-14a's investigation found
that nothing served it: the artifact is written by skos
(skos.watchdog.publish.publish_digest) beside the Atlas brief, at
``~/.skcapstone/watchdog/digests/latest/digest.json`` (mode 0600,
owner-only), and no fleet service exposed that path over HTTP. This module
is the serving half. It reads the SAME file skos already writes and hands
it back byte-for-byte: it never reformats, never regenerates, and never
falls back to a fabricated digest.

hostd owns none of this data -- skos does -- exactly the same division of
labor as ``skharness.jobs`` has with the scheduler's cron ledger, so this
module deliberately does NOT import skos: it resolves the identical default
path directly and exposes its own dedicated override env
(``SKCODE_WATCHDOG_DIGEST_PATH``, mirroring the ``SKCODE_CRON_LEDGER_PATH``
convention ``jobs.py`` already uses), so tests never touch a real fleet path
and never need skos installed.

Fail-safe, always (mirrors ``jobs.py``): a missing ``digests`` directory, a
missing ``latest/digest.json``, and a permission error all read the same
way -- "nothing here to read yet" -- and degrade to ``None``, never an
exception. A ``digest.json`` that IS present but is not valid JSON is a
DIFFERENT fact ("a digest was published but is corrupt") and is
deliberately NOT collapsed into the same state: this module never parses
JSON at all, so it can never be the thing that "fixes" or fabricates a bad
digest -- it returns the file's raw bytes unexamined either way. The C-9
Dart client already distinguishes these on its own: a 404 raises
``SkcodeDigestNotFoundException`` ("no digest published yet"), while a 200
whose body fails to parse raises ``SkcodeDigestParseException`` ("digest
content could not be read"). Two different, honest facts. Never a
fabricated empty "quiet day" digest.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Dedicated override env for this reader (never the producer's own
#: SK_WATCHDOG_DIR / SKCAPSTONE_HOME precedence -- see module docstring).
#: Tests point this at a tmp fixture file so the suite never depends on live
#: fleet watchdog state.
_DIGEST_ENV = "SKCODE_WATCHDOG_DIGEST_PATH"


def default_digest_path() -> Path:
    """``~/.skcapstone/watchdog/digests/latest/digest.json`` (or
    ``$SKCODE_WATCHDOG_DIGEST_PATH`` when set).

    This is exactly the ``latest_json`` path
    ``skos.watchdog.publish.publish_digest`` writes (``digests_dir() /
    "latest" / "digest.json"``, where ``digests_dir()`` roots at
    ``skos.watchdog.cursor.watchdog_home() / "digests"``, defaulting to
    ``~/.skcapstone/watchdog``). Verified against the skos checkout, not
    guessed.
    """
    override = os.environ.get(_DIGEST_ENV)
    if override:
        return Path(override)
    return Path.home() / ".skcapstone" / "watchdog" / "digests" / "latest" / "digest.json"


def read_latest_digest(path: Path | None = None) -> bytes | None:
    """Read the published digest artifact's raw bytes, or ``None`` if there
    is nothing published.

    Byte-faithful on purpose (card C-14a: "do not change the digest JSON
    shape"): this never parses, reformats, or re-serializes the file, so
    whatever skos wrote is exactly what a caller gets back. A missing file,
    a missing parent directory, and a permission error are all the SAME
    fact from a reader's point of view (nothing is there to read yet) and
    degrade to ``None`` rather than raising; a present-but-malformed file is
    a DIFFERENT fact and is returned as-is (see module docstring).
    """
    target = path if path is not None else default_digest_path()
    try:
        return target.read_bytes()
    except OSError:
        return None


__all__ = ["default_digest_path", "read_latest_digest"]
