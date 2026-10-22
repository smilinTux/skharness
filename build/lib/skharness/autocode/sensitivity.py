"""Deterministic sensitivity classification for a coordination card (Joule Economy).

`sensitivity` is the DATA EXPOSURE axis of the work grade: what the payload would
reveal if it reached a provider, independent of blast radius. It resolves to a
provider trust-zone ceiling, so getting it wrong in the permissive direction is a
credential disclosure, not a quality regression.

That is why this module contains NO model call. A classifier that is 95 percent
right on this axis leaks a credential 5 percent of the time, and the 5 percent is
unattributable after the fact. Rules here are pure functions of the card text, so
two runs of the same card always agree and a human can re-derive the verdict from
the returned `reasons`.

Three design rules, in order of precedence:

1. An explicit human override at ``card["meta"]["sensitivity_override"]`` wins.
   It is validated against the canonical vocabulary; an unrecognised value raises
   ``SensitivityOverrideError`` rather than falling back, because a typo'd
   override that silently degrades to a rule verdict is a security control that
   reports success while doing nothing.
2. Otherwise the rules run and any credential-bearing or private-corpus signal
   makes the card ``secret``.
3. Otherwise the card is ``internal``, which the vocabulary names the default for
   fleet agent traffic.

``public`` is deliberately UNREACHABLE from the rules. Declaring a payload safe to
post publicly is a claim no keyword match can support, and the fail-closed rule
says an uncertain answer takes the stricter value. A card is only ``public`` when
a human writes the override.

Public API is pinned; other agents code against it:

    classify_sensitivity(card: dict) -> tuple[str, list[str]]
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .grading import SENSITIVITY_VALUES

#: Documented single location for a human override. One key, one place, so an
#: audit can grep for every card whose sensitivity was set by hand.
OVERRIDE_KEY = "sensitivity_override"

#: Card fields the classifier reads. Explicit rather than "everything in the
#: dict": a wholesale sweep would later pick up whatever a new writer adds to the
#: card (including this module's own grade output) and quietly change verdicts.
#:
#: These are the real free-text fields on a coord Task (title, description, tags,
#: acceptance_criteria, notes; measured over the 4804 cards on the live board).
#: `acceptance` and `repo` are also read because the autopilot WorkItem payload
#: normalizes acceptance_criteria onto `acceptance`, and other card sources carry
#: a plain `repo` string where coord uses a `repo:<name>` tag.
TEXT_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "notes",
    "acceptance_criteria",
    "acceptance",
    "tags",
    "repo",
)


class SensitivityOverrideError(ValueError):
    """An explicit ``meta.sensitivity_override`` that is not a vocabulary value.

    Raised rather than ignored: a silent fallback would let a typo turn a hand
    marked `secret` card back into rule-classified traffic with no signal.
    """


def _flatten(value: Any, depth: int = 0) -> Iterable[str]:
    """Yield every string reachable in a card field (lists and dicts included)."""
    if depth > 4:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _flatten(v, depth + 1)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            yield from _flatten(v, depth + 1)
    elif value is not None and not isinstance(value, bool):
        yield str(value)


def card_text(card: dict) -> str:
    """The corpus the rules match against, from TEXT_FIELDS only, original case.

    Case is preserved because the pasted-literal detectors are case SENSITIVE by
    design (a JWT starts `eyJ`, a Telegram bot token has `:AA`); the keyword rules
    lowercase it themselves.
    """
    parts: list[str] = []
    for field in TEXT_FIELDS:
        parts.extend(_flatten(card.get(field)))
    return "\n".join(parts)


# Repos whose working material IS credentials, keys, sealed stores, or a private
# corpus. Touching one at all is enough; the size of the diff is irrelevant,
# because reading the card and the repo to make a one-line change still puts key
# material in front of the model.
#
# Deliberately NOT here: the PQC library repos (sk_pqc, sk-pqc-py/rs/dart,
# skpqc-skworld-io). They implement primitives and ship test vectors; their cards
# are packaging and API work. They become secret when a card ALSO names key
# material, which the keyword rules below catch. Listing them as repos would mark
# a large, mostly-benign slice of the board secret for no gain.
SECRET_REPOS: frozenset[str] = frozenset({
    "capauth",
    "sksecurity",
    "skvault",
    "skingest",
    "skseal",
    "sksso",
    "sk_pgp",
    "sk-pgp",
    "skpgp",
})

# Tags are a far higher precision signal than a body match: an operator chose the
# tag, and a tag is an exact token rather than a word inside prose. Every entry
# here is a tag that actually appears on the live board.
SECRET_TAGS: frozenset[str] = frozenset({
    "secrets",
    "secrets-lifecycle",
    "ep-secrets-lifecycle",
    "secrets-purge",
    "tier0-secrets",
    "credentials",
    "key-custody",
    "root-key",
    "key-transparency",
    "ep-key-transparency",
    "metadata-privacy",
    "security-sensitive",
    "vault-refactor",
    "vault-file",
    "hashicorp-vault",
    "capauth",
    "skvault",
    "pgp",
    "sk-pgp",
    "prekey",
    "authentik",
    "dataplane-auth",
})

# High confidence signals. Each entry is (compiled pattern, human readable reason).
# Patterns are word-boundary anchored so ordinary prose ("keyword", "tokenizer",
# "authorship") does not trip them.
#
# The vocabulary was measured against the 4804 cards on the live board rather than
# guessed, and terms the board proved NOISY are deliberately absent: bare `health`
# / `medical` (247 cards, almost all "health probe"), `sovereign` (278, a branding
# word), `identity`, `trust`, `audit`, bare `sign`, and bare `auth` all bleed into
# ordinary ops and CI work. Marking those secret would put most of the board in the
# strictest bucket and make the axis meaningless.
_SECRET_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), reason)
    for pattern, reason in (
        # Named credential stores and identity systems in this fleet.
        (r"\bcapauth\b", "capauth, the fleet capability/identity authority"),
        (r"\bskvault\b", "skvault, the sealed KeePass credential store"),
        (r"\bskingest\b", "skingest, private corpus ingestion"),
        (r"\bsksecurity\b|\bskseal\b|\bsk[_-]?pgp\b", "a fleet key-handling component"),
        (r"\bopenbao\b|\bhashicorp\s+vault\b|\bexternal[\s-]?secrets\b|\beso\b",
         "an external secret store"),
        (r"\bauthentik\b|\bbunker\b|\bkms\b", "an identity/key management service"),
        (r"\bkeepass\b|\bkdbx\b", "a KeePass database"),
        (r"\bshamir\b|\bsecret\s+shar", "Shamir secret sharing material"),
        (r"\btotp\b|\botp\s+seed\b|\bunlock[\s-]?word\b", "a TOTP seed or unlock word"),
        (r"\bsecret\s+register\b|\bsecrets\s+plane\b", "the fleet secret register"),
        # Key material, by name and by artifact.
        (r"\bprivate\s+key(s)?\b|\bprivate_key(s)?\b", "private key material"),
        (r"\bsecret\s+key(s)?\b", "secret key material"),
        (r"\bsigning\s+key(s)?\b", "signing key material"),
        (r"\bapi[\s_-]?key(s)?\b|\bmodel\s+keys\b", "an API key"),
        (r"\bssh\s+key(s)?\b|~/\.ssh|\bid_(rsa|ed25519|ecdsa)\b|\bauthorized_keys\b",
         "SSH key material"),
        (r"\bgpg\b|\bpgp\b|\bkeyring\b|\bgnupg\b|\bpubring\b|\bsecring\b",
         "PGP/GPG key material"),
        (r"\brevocation\b|\brevoke(d)?\b|\bgen-revoke\b", "key revocation"),
        (r"\bsops\b|\bsealed[\s-]?secret|\bage\s+recipient\b|\bsops\s*\+\s*age\b",
         "encrypted-secret tooling"),
        (r"\bprekey(s)?\b|\bratchet\b|\bx25519\b|\bx3dh\b|\be2ee\b",
         "E2E key-agreement material"),
        (r"\bml-kem\b|\bml-dsa\b|\bkyber\b|\bdilithium\b|\bfalcon-\d",
         "post-quantum key material"),
        (r"\bdevice\s+key(s)?\b|\broot\s+key\b|\bagent\s+key(s)?\b|\bkek\b|\bdek\b",
         "root/device key material"),
        (r"\bhmac\b|\bargon2\b|\bbcrypt\b|\bscrypt\b|\bwireguard\b",
         "keyed cryptographic material"),
        (r"\bcertificate\s+private\b|\.pem\b|\.p12\b|\.pfx\b|\.jks\b|\bprivkey\b",
         "private certificate material"),
        # Generic credentials.
        (r"\bcredential(s)?\b", "credentials"),
        (r"\bpassword(s)?\b|\bpasswd\b|\bpassphrase(s)?\b|\bhtpasswd\b",
         "a password or passphrase"),
        (r"\bsecret(s)?\b", "material named 'secret'"),
        (r"\bbearer\b|\baccess\s+token\b|\brefresh\s+token\b|\bauth\s+token\b|\bjwt\b",
         "an auth token"),
        (r"\bclient[\s_-]?secret\b|\bservice\s+account\b", "an OAuth/service-account credential"),
        (r"\boauth\b|\bapp\s+password\b|\bpersonal\s+access\s+token\b|\bgithub\s+pat\b",
         "an OAuth, app-password, or personal access token"),
        (r"\bdotenv\b|\.env\b|\benvironment\s+file\b", "an env file, a credential carrier"),
        (r"\bnetrc\b|\.pgpass\b|\bdocker\s*config\.json\b", "a credential config file"),
        # Secret-scanning and history-scrub work: these cards quote what leaked.
        (r"\bgitleaks\b|\btrufflehog\b|\bdetect-secrets\b|\bfilter-repo\b",
         "secret-scanning or history-rewrite tooling, so the card may quote the secret"),
        # Private corpora and personal data.
        (r"\blegal[\s/-]*medical\b|\bmedical\s+record|\bphi\b|\bhipaa\b|\bprotected\s+health\b",
         "the private legal/medical corpus"),
        (r"\bprivate\s+corpus\b|\bunder\s+seal\b|\bsealed\s+(store|vault|corpus)\b",
         "a sealed or private corpus"),
        (r"\bpii\b|\bpersonally\s+identifiable\b|\bssn\b|\bsocial\s+security\s+number\b|\bdlp\b",
         "personally identifiable information"),
        (r"\bchef-only\b|\bskmem-pg\b|\brow[\s-]level\s+security\b|\brls\b",
         "the private memory store or its row-level access control"),
        (r"\bsoul\s+(blueprint|file|state|overlay|content)\b|\bfeb\s+file|\bmemory\s+corpus\b",
         "agent soul or memory content"),
    )
)

# Literal credential SHAPES pasted into a card. Memory of this fleet: card
# descriptions really do carry pasted key material, and a card that quotes a key
# is secret regardless of what the work is about.
_LITERAL_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), reason)
    for pattern, reason in (
        (r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----", "a pasted PEM private key block"),
        (r"(?i)-----BEGIN PGP PRIVATE KEY BLOCK-----", "a pasted PGP private key block"),
        (r"\bAKIA[0-9A-Z]{16}\b", "a pasted AWS access key id"),
        (r"\b(ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{20,}", "a pasted GitHub token"),
        (r"\bsk-(ant|proj|or)-[0-9A-Za-z_-]{16,}", "a pasted provider API key"),
        (r"\bxox[baprs]-[0-9A-Za-z-]{10,}", "a pasted Slack token"),
        (r"\bglpat-[0-9A-Za-z_-]{16,}", "a pasted GitLab token"),
        (r"\beyJ[0-9A-Za-z_-]{10,}\.[0-9A-Za-z_-]{10,}\.[0-9A-Za-z_-]{10,}", "a pasted JWT"),
        (r"\b\d{6,10}:AA[0-9A-Za-z_-]{30,}", "a pasted Telegram bot token"),
    )
)

# Signals that are secret-ADJACENT but genuinely ambiguous in prose. These do not
# prove key material is involved, so they are listed apart, but the fail-closed
# rule resolves them to `secret` with a reason that says the match was ambiguous.
# An operator who disagrees writes the override, which is then attributable.
_AMBIGUOUS_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), reason)
    for pattern, reason in (
        (r"\bkey\s+material\b|\bkeystore\b|\bkey\s+store\b", "a key store"),
        (r"\bvault\b", "a vault"),
        (r"\bseal(ed|ing)?\b|\bunseal\b|\bunlock(ing)?\s+(the\s+)?(store|vault|corpus|keyring)\b",
         "something sealed or being unsealed"),
        (r"\bleak(ed|s|ing)?\b|\bexfiltrat", "a leak, so the text may quote what leaked"),
        (r"\bscrub(bed|bing)?\b|\bredact(ed|ing|ion)?\b",
         "a scrub or redaction, so the text may quote what is being removed"),
        (r"\brotat(e|es|ed|ing|ion)\b.{0,40}\b(key|token|cert|identity|deployer)\b"
         r"|\b(key|token|cert|identity|deployer)\b.{0,40}\brotat(e|es|ed|ing|ion)\b",
         "rotating something key-shaped"),
        (r"\bsoul\b", "a soul, which is agent content on some cards and a public "
                      "registry API on others"),
        (r"\bsign(ing|ed)?\s+(request|payload|token|attestation|cert)\b",
         "a signing path, which commonly handles keys"),
    )
)


def _matches(text: str, rules: Iterable[tuple[re.Pattern[str], str]]) -> list[str]:
    return [reason for pattern, reason in rules if pattern.search(text)]


def _card_tags(card: dict) -> list[str]:
    return [t.strip().lower() for t in _flatten(card.get("tags")) if t.strip()]


def _repo_names(card: dict) -> list[str]:
    """Repo identifiers on the card.

    coord encodes the repo as a tag, and the board carries BOTH separators in the
    wild (``repo:capauth`` is the convention, ``repo-capauth`` appears as a
    one-off). Both are read, plus a plain ``repo`` field for card sources that
    have one.
    """
    names: list[str] = [r.strip().lower() for r in _flatten(card.get("repo"))]
    for tag in _card_tags(card):
        for prefix in ("repo:", "repo-"):
            if tag.startswith(prefix):
                names.append(tag[len(prefix):].strip())
                break
    return [n for n in names if n]


def _read_override(card: dict) -> str | None:
    """The validated human override, or None when the card carries none."""
    meta = card.get("meta")
    if not isinstance(meta, dict):
        return None
    if OVERRIDE_KEY not in meta:
        return None
    raw = meta[OVERRIDE_KEY]
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if value not in SENSITIVITY_VALUES:
        raise SensitivityOverrideError(
            f"meta.{OVERRIDE_KEY}={raw!r} is not a sensitivity value; "
            f"expected one of {list(SENSITIVITY_VALUES)}"
        )
    return value


def classify_sensitivity(card: dict) -> tuple[str, list[str]]:
    """Return (sensitivity, reasons). sensitivity is one of the canonical
    vocabulary values. reasons is human readable evidence, never empty.

    Deterministic and model-free. Precedence: a validated human override at
    ``meta.sensitivity_override``, then the secret rules (repo, keyword, pasted
    literal, ambiguous-and-therefore-strict), then ``internal``.

    Raises SensitivityOverrideError when an override is present but not a
    vocabulary value. Every other input, including a non-dict or an empty card,
    returns a value: an unreadable card cannot be shown to be safe, so it fails
    closed to ``secret``.
    """
    if not isinstance(card, dict):
        return "secret", [
            f"card is {type(card).__name__}, not a dict, so no rule could read it; "
            "failing closed to the strictest value"
        ]

    override = _read_override(card)
    if override is not None:
        return override, [
            f"explicit human override meta.{OVERRIDE_KEY}={override!r}; "
            "an override outranks the rules and is attributable to whoever set it"
        ]

    raw = card_text(card)
    text = raw.lower()
    if not text.strip():
        return "secret", [
            "card carries no readable text in "
            f"{', '.join(TEXT_FIELDS)}, so nothing rules out credential material; "
            "failing closed to the strictest value"
        ]

    reasons: list[str] = []

    for name in sorted({n for n in _repo_names(card) if n in SECRET_REPOS}):
        reasons.append(
            f"repo {name!r} is credential-bearing by nature, so any change to it "
            "puts key material or a sealed store in front of the model, however "
            "small the diff"
        )
    for tag in sorted({t for t in _card_tags(card) if t in SECRET_TAGS}):
        reasons.append(
            f"tag {tag!r} was applied by an operator and marks credential, key, or "
            "private-data work"
        )

    for reason in _matches(raw, _LITERAL_RULES):
        reasons.append(f"card text contains {reason}")
    for reason in _matches(text, _SECRET_RULES):
        reasons.append(f"card text names {reason}")

    if reasons:
        return "secret", reasons

    ambiguous = _matches(text, _AMBIGUOUS_RULES)
    if ambiguous:
        return "secret", [
            f"card text names {reason}, which is ambiguous on its own"
            for reason in ambiguous
        ] + [
            "no rule can confirm or rule out credential material here, and an "
            "uncertain sensitivity takes the stricter value, so this is secret; "
            f"set meta.{OVERRIDE_KEY} to relax it deliberately"
        ]

    return "internal", [
        "no credential, key-material, or private-corpus signal matched in "
        f"{', '.join(TEXT_FIELDS)}",
        "internal is the vocabulary default for fleet agent traffic; public is "
        f"never inferred and requires an explicit meta.{OVERRIDE_KEY}",
    ]
