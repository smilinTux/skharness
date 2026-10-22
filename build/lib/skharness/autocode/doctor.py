"""Harness self-check: PROACTIVE self-healing.

Tonight's live-run failures each cost hours because nothing surfaced them until a
run silently escalated: skos.autopilot still pointed at pre-extraction code on a
node; a sandbox image predated the current module path so the egress proxy
crashed; the OAuth token had expired. Each is a fast, deterministic check. This
module runs them BEFORE a run wastes a coding round, reports a structured verdict,
and auto-heals the safe ones (a stale token warning, telemetry decline signal).

Design rules mirror health.py: pure stdlib, best-effort, and a check NEVER raises
into the run -- a failed check yields a `fail`/`warn` verdict, not an exception.
The subprocess/docker checks take an injectable runner so they stay unit-testable.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from . import health


@dataclass
class Check:
    name: str
    status: str            # "ok" | "warn" | "fail"
    detail: str
    fix: str = ""          # one-line remediation hint when not ok

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def check_shim_delegation() -> Check:
    """The bug that cost the most tonight: `skos autopilot run` used a node's
    OWN pre-extraction autopilot code because the skos->skharness shims were
    never deployed there, so none of the shared fixes ran. Verify the delegation
    by object identity."""
    try:
        import skharness.autocode.orchestrator as h
        import skos.autopilot.orchestrator as s
    except Exception as exc:                       # noqa: BLE001
        return Check("shim-delegation", "warn", f"could not import both paths: {exc}")
    if s.run_once is h.run_once and s.phase0_assess is h.phase0_assess:
        return Check("shim-delegation", "ok",
                     "skos.autopilot delegates to skharness.autocode")
    return Check("shim-delegation", "fail",
                 "skos.autopilot does NOT delegate to skharness (stale/un-shimmed "
                 "code on this node) -- every `skos autopilot run` here ignores the "
                 "shared engine and its fixes",
                 fix="rsync the skos.autopilot shim package to this node "
                     "(~/clawd/skos/src/skos/autopilot/)")


def check_auth() -> Check:
    """No usable token means every sandbox call 401s and the run escalates at
    phase 0. An expired fallback token is a warn (provision a long-lived one)."""
    from .adapters.claude_code import _CRED_PATH, _EXPIRY_SKEW_SEC, _oauth_token
    import json
    import os
    from pathlib import Path
    if not _oauth_token():
        return Check("auth", "fail", "no CLAUDE_CODE_OAUTH_TOKEN and no usable "
                     "credential; sandbox calls will 401",
                     fix="claude setup-token, then set CLAUDE_CODE_OAUTH_TOKEN")
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return Check("auth", "ok", "long-lived CLAUDE_CODE_OAUTH_TOKEN is set")
    try:
        oauth = json.loads(Path(_CRED_PATH).expanduser().read_text()).get("claudeAiOauth", {})
        exp = oauth.get("expiresAt")
        if isinstance(exp, (int, float)) and exp / 1000.0 <= time.time() + _EXPIRY_SKEW_SEC:
            return Check("auth", "warn", "host access token is expired/near-expiry; "
                         "a headless run cannot refresh it",
                         fix="claude setup-token -> CLAUDE_CODE_OAUTH_TOKEN for cron runs")
    except (OSError, ValueError):
        pass
    return Check("auth", "ok", "host access token resolves and is valid")


def check_sandbox_proxy_image(image: str = "sandbox-proxy:1", *, run=subprocess.run) -> Check:
    """The egress proxy image bundles the proxy module at a fixed path; if it was
    built before the module moved (skos.autopilot -> skharness.autocode) the
    sidecar crashes and the sandbox loses egress. Verify the module imports in
    the image."""
    try:
        proc = run(["docker", "run", "--rm", image, "python", "-c",
                    "import skharness.autocode.sandbox_proxy"],
                   capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return Check("proxy-image", "warn", "docker not on PATH; cannot verify image")
    except subprocess.TimeoutExpired:
        return Check("proxy-image", "warn", f"{image} check timed out")
    if proc.returncode == 0:
        return Check("proxy-image", "ok", f"{image} has the current proxy module")
    return Check("proxy-image", "fail",
                 f"{image} cannot import skharness.autocode.sandbox_proxy "
                 f"(stale image -> sandbox has no egress): {proc.stderr.strip()[:120]}",
                 fix="rebuild: docker/sandbox/build.sh proxy")


def check_decline_signal(threshold: float = 0.5) -> Check:
    """A learning signal: if the recent run-decline rate is high the CLI is being
    flaky (rate limits, hedging). Not a hard failure -- the harness already
    self-tunes its retries on it -- but worth surfacing."""
    rate = health.rate("run_inconclusive", over=("run_inconclusive", "run_ok"))
    if rate >= threshold:
        return Check("decline-rate", "warn",
                     f"recent CLI decline rate is high ({rate:.0%}); runs will be slow",
                     fix="usually transient (rate limit); the retry budget auto-raises")
    return Check("decline-rate", "ok", f"CLI decline rate nominal ({rate:.0%})")


def _probe_grader(model: str, base_url: str, timeout: float):
    """Ask the gateway to serve `model` and report (status, served_id, error).

    A one-token completion, NOT a `/v1/models` lookup. On this fleet the catalog
    is not evidence of serving: it has advertised ids that 404 and hidden ids
    that answer, so a catalog hit cannot distinguish a live pin from a dead one.
    A response can.

    `status` is None when nothing answered at all, which is the "cannot tell"
    case and is deliberately distinct from any HTTP status.
    """
    import json
    import os
    import urllib.error
    import urllib.request
    url = base_url.rstrip("/") + "/v1/chat/completions"
    body = json.dumps({"model": model, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "ping"}]}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "content-type": "application/json",
        "authorization": "Bearer " + os.environ.get("SKCODE_GATEWAY_TOKEN", "sk-local")})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads((resp.read() or b"{}").decode() or "{}")
            return resp.status, str(payload.get("model") or ""), ""
    except urllib.error.HTTPError as exc:
        return exc.code, "", str(exc.reason or "")
    except Exception as exc:                       # noqa: BLE001 -- never raises into a run
        return None, "", f"{type(exc).__name__}: {exc}"


def check_grader_pin(model: str | None = None, base_url: str | None = None,
                     *, probe=_probe_grader, timeout: float = 20.0) -> Check:
    """Does the PINNED grader model still exist on the gateway we would ask?

    A config pinning a dead model id is indistinguishable from one pinning a live
    model until something asks. That is how `ornith-big` sat in
    `orchestrator.GRADER_MODEL` for weeks after `ornith-aeon.service` was retired
    off chiap08: nothing in this repo ever asked. This check asks.

    "Cannot tell" is NOT one of the good answers:

    * ``ok``   -- the gateway answered and named the pinned model back.
    * ``fail`` -- the gateway is up and refused the id. The pin is dead, and
      every grade this harness stamps names a model that does not exist.
    * ``warn`` -- either the gateway could not be reached (the pin is UNVERIFIED,
      which is not the same as confirmed live), or it answered under a DIFFERENT
      id, meaning skgateway failed over and the pin is not the grader of record.

    An unreachable gateway can never make this check ``ok``. That is the whole
    design constraint: a checker that reports healthy when it could not observe
    anything is the failure being removed, not a check on it.
    """
    import os
    from .orchestrator import GRADER_MODEL
    model = (model or GRADER_MODEL).strip()
    base_url = base_url or os.environ.get("SKCODE_GATEWAY_BASE", "http://localhost:18780")
    reverify = (f"re-verify by hand: curl -s -o /dev/null -w '%{{http_code}}' "
                f"-X POST {base_url}/v1/chat/completions -H 'content-type: application/json' "
                f"-d '{{\"model\":\"{model}\",\"max_tokens\":1,"
                f"\"messages\":[{{\"role\":\"user\",\"content\":\"ping\"}}]}}'")
    status, served, err = probe(model, base_url, timeout)
    if status is None:
        return Check("grader-pin", "warn",
                     f"could not reach {base_url}, so the pinned grader {model!r} is "
                     f"UNVERIFIED -- not confirmed live ({err[:100]})",
                     fix=f"run this where skgateway is reachable. {reverify}")
    if status != 200:
        return Check("grader-pin", "fail",
                     f"the gateway refused the pinned grader {model!r} (HTTP {status}"
                     + (f": {err[:80]}" if err else "")
                     + "); it does not exist, so no card can be graded and every "
                       "grade already stamped with it names nothing",
                     fix="repoint orchestrator.GRADER_MODEL at a model the fleet "
                         "serves, and record the date you verified it beside the pin")
    if served and served != model:
        return Check("grader-pin", "warn",
                     f"asked for {model!r} and {served!r} answered: skgateway failed "
                     "over, so the pin is not the grader of record and a grade "
                     "stamped with it is unattributable",
                     fix="pin the id that actually answers, or fix the routing. "
                         + reverify)
    return Check("grader-pin", "ok",
                 f"{base_url} serves the pinned grader {model} under its own id")


#: The checks a preflight runs, cheapest/most-diagnostic first.
def check_concurrency() -> Check:
    """Report the resource-scaled build concurrency for THIS host (informational):
    how many builds a full run will execute at once, and the room above/below it."""
    try:
        from .autoscale import describe
        from .config import Config
        cfg = Config.load()
        cap = int(getattr(cfg.caps, "max_concurrent", 3) or 3)
        mode = getattr(cfg.caps, "concurrency", "recommended")
        return Check("concurrency", "ok", describe(mode, cap))
    except Exception as exc:                      # informational -> warn, never fail
        return Check("concurrency", "warn", f"could not resolve concurrency: {exc}")


CHECKS = (check_shim_delegation, check_auth, check_sandbox_proxy_image,
          check_grader_pin, check_decline_signal, check_concurrency)


def preflight() -> list[Check]:
    """Run every self-check and record the verdict to health telemetry. Returns
    the checks; the caller decides whether a `fail` should block (run_once treats
    it as advisory + logged, never a hard stop, so the doctor can never wedge a
    run on its own bug)."""
    results = [c() for c in CHECKS]
    worst = "ok"
    for r in results:
        if r.status == "fail":
            worst = "fail"
        elif r.status == "warn" and worst != "fail":
            worst = "warn"
    health.record("preflight", worst=worst,
                  failed=[r.name for r in results if r.status == "fail"])
    return results


def format_report(results: list[Check]) -> str:
    icon = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    lines = []
    for r in results:
        lines.append(f"[{icon.get(r.status, '?')}] {r.name}: {r.detail}")
        if r.fix and not r.ok:
            lines.append(f"        fix: {r.fix}")
    return "\n".join(lines)
