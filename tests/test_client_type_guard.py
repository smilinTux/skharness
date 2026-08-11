"""Guard: the skcode web client (src/skharness/client/index.html) must express
every font size as a rem token on :root, never a raw px literal, so browser
zoom keeps working (rem scales with the root font size; px does not).

Card D-3 / spec 2026-08-11-skworld-density-and-type-scale.md section 7.2.

The two regexes below (the px font-size check and the negative lookbehind
that protects ``--font-size:`` custom properties, and the Tailwind-style
arbitrary-value check) are lifted from Buzz's ``scripts/check-px-text-core.mjs``
(https://github.com/block/buzz, Apache-2.0), adapted from JS/Node to Python
for a codebase that already converted completely (a ratchet baseline is not
needed here: the client is one file, and this test bans px font sizes
outright rather than allowing a shrinking list of pre-existing hits).
"""

import re
from pathlib import Path

# Resolved relative to this test file (not via `import skharness`): an editable
# install can point at a different checkout of the repo (e.g. a worktree runs
# tests against its own tree, but the venv's skharness may be pip-installed
# from a sibling checkout), and this guard must check THIS tree's client, not
# whichever one happens to be import-resolved.
CLIENT_HTML = Path(__file__).resolve().parent.parent / "src" / "skharness" / "client" / "index.html"

# (?<!-) excludes a match preceded by "-", so "--font-size: ...px" (a custom
# property declaration, not a font-size rule) does not trip the guard.
PX_FONT_SIZE_RE = re.compile(r"(?<!-)font-size:\s*\d+(\.\d+)?px")
ARBITRARY_TEXT_SIZE_RE = re.compile(r"text-\[\d")


def test_client_has_zero_px_font_sizes_or_arbitrary_text_sizes():
    assert CLIENT_HTML.is_file(), f"expected the skcode client at {CLIENT_HTML}"
    text = CLIENT_HTML.read_text(encoding="utf-8")

    px_matches = PX_FONT_SIZE_RE.findall(text)
    assert not px_matches, (
        "found raw px font-size literal(s) in client/index.html; use one of "
        "the --fs-* rem tokens declared on :root instead"
    )

    arbitrary_matches = ARBITRARY_TEXT_SIZE_RE.findall(text)
    assert not arbitrary_matches, (
        "found an arbitrary Tailwind-style text-[Npx] value in client/index.html; "
        "this client has no build step and no Tailwind, so this should never match, "
        "but the check stays as a guard against one sneaking in"
    )
