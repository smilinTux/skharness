"""The ONE definition of sovereignty for the fleet.

Sovereignty is a claim about HARDWARE AND JURISDICTION. The discriminator is
the **backend that served the call** plus the **energy basis it reported**,
never the model name. ``ornith-1.0-9b`` served by ``nvidia`` is a violation;
the same weights served by ``reg:ornith`` are not. The weights are not the
variable.

Why the model id is unsound, measured rather than argued
--------------------------------------------------------
skgateway resolves failover server side, so a model id in a request is an
INTENT, not an outcome. In the live ledger
(``skgateway/data/metrics.db``, ``energy_log``) on 2026-08-17:

* ``ornith-big`` (the pinned "sovereign" grader in ``orchestrator.py``) has a
  row with ``backend=nvidia``, ``basis=imputed_cloud``. So does ``ornith-tiny``.
* 76 rows carry a model name containing one of the old shell allowlist tokens
  (``ornith qwen llama mxbai beellama``) while running on ``backend=nvidia``,
  ``basis=imputed_cloud``, e.g. ``meta/llama-3.3-70b-instruct`` and
  ``nvidia/llama-3.3-nemotron-super-49b-v1``.
* ``req_id 185ab8359ac8`` holds BOTH ``sk-default / reg:ornith / measured_gpu``
  and ``sk-default / nvidia / imputed_cloud``: one request, two attempts, two
  jurisdictions, one model id.

A name check returns True for every one of those. That is not a strict gate
that occasionally errs; it is a gate that cannot see the fact it exists to
check.

Evidence ranking
----------------
1. ``basis=measured_gpu`` WITH a named ``node``. Physical and unforgeable: a
   GPU we own reported joules. A cloud provider cannot produce that reading.
2. ``backend``. Config grounded and correct per winning attempt: skgateway
   names the backend that actually produced the bytes.
3. The model id. Demonstrably unsound, so it is not an input to this module at
   all. ``classify()`` takes no model parameter, by construction.

Three states, never two
-----------------------
``sovereign``   we observed our own hardware answering.
``violated``    we observed something else answering.
``unobserved``  we did not observe who answered.

``unobserved`` is NOT ``sovereign``. Collapsing the two is the bug this module
removes: it is exactly what makes a broken gate and a healthy gate look
identical. Callers that gate on sovereignty must treat ``unobserved`` as a
refusal (fail closed) while recording it distinctly, because "nobody looked"
and "a cloud answered" call for different fixes.

The denylist wins over everything
---------------------------------
The sovereign backend allowlist is extensible by an operator (new local
backends appear). The third-party denylist is NOT, and it is checked FIRST.
That ordering is deliberate: it means no configuration change can ever
relabel ``nvidia`` or ``anthropic`` as sovereign. It also catches contradictory
evidence that is really present in the ledger today, one row of
``backend=anthropic`` with ``basis=imputed_local``: an imputation bug does not
buy a third party a sovereignty certificate.

Pure stdlib, no deps, no network, no I/O. Both ends of the fleet import this
one module rather than each carrying a rule.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import NamedTuple

__all__ = [
    "SOVEREIGN", "VIOLATED", "UNOBSERVED", "STATES",
    "Verdict", "classify", "from_headers", "from_attributes",
    "sovereign_backends", "THIRD_PARTY_BACKENDS",
]

#: We observed hardware we own answering the call.
SOVEREIGN: str = "sovereign"
#: We observed something we do not own answering the call.
VIOLATED: str = "violated"
#: We did not observe who answered. Not a synonym for either of the above.
UNOBSERVED: str = "unobserved"

STATES: tuple[str, ...] = (SOVEREIGN, VIOLATED, UNOBSERVED)

#: Energy basis values skgateway writes. ``measured_gpu`` is the only PHYSICAL
#: one; the two imputed values are model outputs, so they are corroborating
#: evidence rather than proof and are only trusted alongside the backend.
BASIS_MEASURED_GPU: str = "measured_gpu"
BASIS_IMPUTED_LOCAL: str = "imputed_local"
BASIS_IMPUTED_CLOUD: str = "imputed_cloud"

#: Backend ids that are, by definition, somebody else's hardware in somebody
#: else's jurisdiction. Checked FIRST and NOT overridable: see the module
#: docstring. Matched on the full id and on the part before a ``:``, so
#: ``anthropic-direct`` and a future ``anthropic:foo`` both land here.
THIRD_PARTY_BACKENDS: frozenset[str] = frozenset({
    "anthropic", "anthropic-direct", "openai", "azure", "bedrock", "vertex",
    "google", "gemini", "nvidia", "openrouter", "opencode", "groq", "together",
    "fireworks", "deepinfra", "replicate", "perplexity", "mistral", "cohere",
    "deepseek", "xai", "moonshot", "cerebras", "sambanova", "hyperbolic",
})

#: Backend id prefixes that name hardware we own. ``reg:`` is skgateway's own
#: local model registry; the rest are the process/runtime names our nodes
#: report. Concrete prefixes rather than a denylist, because an unrecognised
#: backend must land in ``unobserved``, not in ``sovereign``.
_DEFAULT_SOVEREIGN_BACKEND_PREFIXES: tuple[str, ...] = (
    "reg:", "local", "ollama", "llamacpp", "llama.cpp", "vllm", "chiap",
    "sk-local", "sklocal", "beellama", "mxbai-arc", "qwen3-arc",
)

#: Operator extension point. Comma separated backend id prefixes ADDED to the
#: defaults above; it can never remove one and can never override
#: ``THIRD_PARTY_BACKENDS``, which is checked first.
SOVEREIGN_BACKENDS_ENV: str = "SK_SOVEREIGN_BACKEND_PREFIXES"


class Verdict(NamedTuple):
    """One sovereignty answer plus the evidence it was reached on.

    ``reason`` is written for a human reading a failed gate at 3am: it names
    the observed values, not a rule number.
    """

    state: str
    backend: str | None
    basis: str | None
    node: str | None
    reason: str

    @property
    def sovereign(self) -> bool:
        """True ONLY for ``sovereign``. ``unobserved`` is false here, on
        purpose: a gate that reads this property fails closed by default."""
        return self.state == SOVEREIGN

    def as_dict(self) -> dict:
        return {"state": self.state, "backend": self.backend,
                "basis": self.basis, "node": self.node, "reason": self.reason}


def sovereign_backends() -> tuple[str, ...]:
    """The sovereign backend prefixes in force, defaults plus any operator
    extension from ``SK_SOVEREIGN_BACKEND_PREFIXES``. Read at call time so a
    test or an operator can extend it without reimporting."""
    extra = os.environ.get(SOVEREIGN_BACKENDS_ENV, "")
    added = tuple(p.strip().lower() for p in extra.split(",") if p.strip())
    return _DEFAULT_SOVEREIGN_BACKEND_PREFIXES + added


def _clean(value: object) -> str | None:
    """A non-empty stripped string, or None. Anything that is not a str (a
    ``MagicMock``, an int, a sentinel) is ABSENT, never coerced: a repr is not
    an observation."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _is_third_party(backend: str) -> bool:
    return backend in THIRD_PARTY_BACKENDS or backend.split(":", 1)[0] in THIRD_PARTY_BACKENDS


def _is_sovereign_backend(backend: str) -> bool:
    return any(backend.startswith(p) for p in sovereign_backends())


def classify(backend: object, basis: object, node: object = None) -> Verdict:
    """Classify one serving observation. THE definition; there is no other.

    Deliberately takes NO model parameter. A model id cannot change this
    answer, because a model id is what was asked for and this function reports
    what happened. Adding one would reintroduce the exact defect this module
    exists to remove, so the signature refuses it rather than ignoring it.

    ``backend``  the backend that served the call (skgateway ``x-sk-backend``
                 / ``energy_log.backend``).
    ``basis``    the energy basis it reported (``x-sk-energy-basis`` /
                 ``energy_log.basis``).
    ``node``     the node that measured it, when one did (``x-sk-energy-node``
                 / ``energy_log.node``).

    Any missing observable yields ``unobserved``, never ``sovereign``.
    """
    b = _clean(backend)
    ba = _clean(basis)
    n = _clean(node)
    bl = b.lower() if b else None

    if bl is None:
        return Verdict(UNOBSERVED, None, ba, n,
                       "no serving backend was observed, so who answered is unknown")

    # Denylist FIRST and unconditionally. A third party stays a third party
    # whatever basis it reported and whatever an operator added to the
    # allowlist. The live ledger holds a backend=anthropic row with
    # basis=imputed_local; that is an imputation bug, not a jurisdiction change.
    if _is_third_party(bl):
        return Verdict(VIOLATED, b, ba, n,
                       f"served by {b!r}, a third-party backend"
                       + (f" (basis {ba!r})" if ba else ""))

    if not _is_sovereign_backend(bl):
        return Verdict(UNOBSERVED, b, ba, n,
                       f"backend {b!r} is not a known sovereign backend and is not a known "
                       f"third party, so its jurisdiction is unknown; add its prefix to "
                       f"{SOVEREIGN_BACKENDS_ENV} only if we own the hardware")

    if ba is None:
        return Verdict(UNOBSERVED, b, ba, n,
                       f"backend {b!r} is allowlisted but no energy basis was observed, "
                       f"so nothing corroborates it")

    if ba == BASIS_MEASURED_GPU:
        if n is None:
            # measured_gpu is only physical evidence when something names the
            # thing that did the measuring. A reading with no meter is a claim.
            return Verdict(UNOBSERVED, b, ba, n,
                           f"backend {b!r} reports {ba!r} but names no node, so there is no "
                           f"meter behind the measurement")
        return Verdict(SOVEREIGN, b, ba, n,
                       f"{b!r} on node {n!r} reported measured_gpu joules: physical evidence "
                       f"from hardware we own")

    if ba == BASIS_IMPUTED_LOCAL:
        return Verdict(SOVEREIGN, b, ba, n,
                       f"served by {b!r} with basis {ba!r}: config-grounded local backend, "
                       f"no physical measurement")

    # An allowlisted backend reporting a cloud basis is contradictory evidence.
    # Refusing is the only safe read: one of the two observations is wrong and
    # we cannot tell which, so we do not certify.
    return Verdict(VIOLATED, b, ba, n,
                   f"backend {b!r} is allowlisted but reported basis {ba!r}; the two "
                   f"observations contradict each other, so sovereignty is not certified")


def from_headers(headers: object) -> Verdict:
    """Classify from skgateway response headers, the observable a caller
    holding a live response already has.

    Reads ``x-sk-backend`` / ``x-sk-energy-basis`` / ``x-sk-energy-node``, all
    of which skgateway emits today for the SERVING attempt (verified live
    against ``localhost:18780``). Header lookup is case insensitive. A response
    that carries none of them classifies ``unobserved``, which is the honest
    answer for a gateway too old to report attribution.
    """
    lower: dict[str, object] = {}
    try:
        items = headers.items()          # type: ignore[union-attr]
    except AttributeError:
        return Verdict(UNOBSERVED, None, None, None,
                       "no headers to read, so who answered is unknown")
    for key, value in items:
        if isinstance(key, str):
            lower[key.lower()] = value
    return classify(lower.get("x-sk-backend"),
                    lower.get("x-sk-energy-basis"),
                    lower.get("x-sk-energy-node"))


def from_attributes(obj: object) -> Verdict:
    """Classify from an object that reports what it observed.

    The attribute names are ``backend_served`` (same field name PR #45 adds to
    the ledger row, so one word means one thing across the repo),
    ``energy_basis`` and ``energy_node``. An object that reports none of them
    classifies ``unobserved``: a harness with no attribution channel has not
    told us it ran sovereign, it has told us nothing.
    """
    return classify(getattr(obj, "backend_served", None),
                    getattr(obj, "energy_basis", None),
                    getattr(obj, "energy_node", None))


#: Exit codes for the CLI below. Distinct per state so a shell caller can tell
#: "a cloud answered" from "nobody looked" without parsing prose.
EXIT_CODES: dict[str, int] = {SOVEREIGN: 0, VIOLATED: 1, UNOBSERVED: 2}


def main(argv: list[str] | None = None) -> int:
    """``python3 -m skharness.autocode.sovereignty`` so a shell probe consumes
    THIS definition rather than mirroring it.

    A mirrored copy in another language is a second definition the moment
    either side is edited, and nothing would report the drift. Calling across
    the seam costs one subprocess and makes drift impossible.
    """
    ap = argparse.ArgumentParser(
        prog="python3 -m skharness.autocode.sovereignty",
        description="Classify one serving observation as sovereign / violated / unobserved.")
    ap.add_argument("--backend", default=None, help="x-sk-backend / energy_log.backend")
    ap.add_argument("--basis", default=None, help="x-sk-energy-basis / energy_log.basis")
    ap.add_argument("--node", default=None, help="x-sk-energy-node / energy_log.node")
    ap.add_argument("--json", action="store_true", help="emit the full verdict as JSON")
    args = ap.parse_args(argv)

    verdict = classify(args.backend, args.basis, args.node)
    if args.json:
        print(json.dumps(verdict.as_dict(), sort_keys=True))
    else:
        print(f"{verdict.state}\t{verdict.reason}")
    return EXIT_CODES[verdict.state]


if __name__ == "__main__":       # pragma: no cover - exercised via subprocess
    sys.exit(main())
