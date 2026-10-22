"""The morning numbered digest and its stable-within-a-day manifest.

Numbers are assigned over the unanswered ``source="autopilot"`` decision items in
the GTD store, ordered by priority then created_at, and pinned in
``autopilot-digest.json`` so a reply-by-number resolves the same item all day
(spec section 9). Sending the DM is Phase F; this module only builds and persists.

Binding (spec `2026-08-13-unified-consent-plane-arch.md` section 3.2, card
`78409fc0`): the displayed number ``n`` is presentation only. Every item also
carries a stable ``qid`` and a ``content_hash`` over exactly what was shown for
that item (qid + prompt + options), and the manifest as a whole carries a
``generation`` hash over the full ordered (n, qid, content_hash) sequence. A
rebuild that changes composition or order produces a new ``generation``,
mirroring Terraform Cloud's stale-plan behaviour: the whole digest is what was
"shown", not each line item independently, so any drift voids the lot and
forces a fresh digest rather than resolving `n` against whatever now happens
to sit at that position. See `resolver.answer`.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_GTD_FILES = ["inbox.json", "next-actions.json", "projects.json",
              "waiting-for.json", "someday-maybe.json", "archive.json"]

# Default lifetime of a queued decision before it is treated as expired
# (spec section 3.2: "mandatory expiry with an explicit escalation state").
DEFAULT_DECISION_TTL_HOURS = 24.0


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def content_hash(qid: str | None, prompt: str | None, options) -> str:
    """Hash of exactly what a single decision item shows a human.

    Deliberately excludes ``n``: the ordinal is presentation only (criterion 1)
    and must not participate in the binding, only the substance the human is
    actually asked to approve.
    """
    return hashlib.sha256(
        _canonical({"qid": qid, "prompt": prompt, "options": options}).encode()
    ).hexdigest()[:24]


def generation_hash(manifest_items: list[dict]) -> str:
    """Hash over the full ordered presentation: (n, qid, content_hash) per item.

    This is the Terraform-style "plan identity": any change to composition or
    order between build and answer changes this hash, and `resolver.answer`
    refuses rather than silently reinterpreting a stale `n`.
    """
    shape = [{"n": it.get("n"), "qid": it.get("qid"),
             "content_hash": it.get("content_hash")} for it in manifest_items]
    return hashlib.sha256(_canonical(shape).encode()).hexdigest()[:24]


def _load_store_items() -> list[dict]:
    from skos.gtd_ingest import gtd_dir
    items: list[dict] = []
    for fname in _GTD_FILES:
        p = gtd_dir() / fname
        if p.exists():
            try:
                items += json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
    return items


def build_manifest(items: list[dict] | None = None, *, digest_date: str,
                   sent_at: str | None = None) -> dict:
    """Build the digest manifest over unanswered autopilot decision items."""
    if items is None:
        items = _load_store_items()
    unanswered = [it for it in items
                  if it.get("source") == "autopilot"
                  and not (it.get("decision") or {}).get("answered")
                  # expired is an explicit terminal state (criterion 4): once
                  # resolver.answer records it, it must stop recirculating
                  # through future digests, not silently reappear forever.
                  and (it.get("decision") or {}).get("status") != "expired"]
    ordered = sorted(unanswered,
                     key=lambda it: (PRIORITY_RANK.get(it.get("priority") or "medium", 2),
                                     it.get("created_at") or ""))
    manifest_items = []
    for n, it in enumerate(ordered, 1):
        dec = it.get("decision") or {}
        qid, prompt, options = dec.get("qid"), dec.get("prompt"), dec.get("options")
        # Mandatory expiry (criterion 4): carry the decision's own expires_at
        # through if it has one; backfill from created_at + the default TTL for
        # older items captured before this field existed, so every manifest
        # entry always has a concrete expiry -- never an implicit "forever".
        expires_at = dec.get("expires_at") or _fallback_expiry(it.get("created_at"))
        manifest_items.append({
            "n": n, "qid": qid, "id": it.get("id"),
            "source_ref": it.get("source_ref"), "prompt": prompt, "options": options,
            "content_hash": content_hash(qid, prompt, options),
            "expires_at": expires_at, "answered": False})
    return {"digest_date": digest_date, "sent_at": sent_at,
            "generation": generation_hash(manifest_items), "items": manifest_items}


def _fallback_expiry(created_at: str | None) -> str:
    """Backfill expiry for decisions captured before `expires_at` existed."""
    base = None
    if created_at:
        try:
            base = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            base = None
    base = base or datetime.now(timezone.utc)
    return (base + timedelta(hours=DEFAULT_DECISION_TTL_HOURS)).isoformat()


def build_digest_text(manifest: dict) -> str:
    """Render the reply-by-number DM body."""
    lines = ["Morning decisions (reply with the number):"]
    for it in manifest["items"]:
        opts = it.get("options") or {}
        optstr = "/".join(opts.keys()) if isinstance(opts, dict) else ""
        suffix = f"  [{optstr}]" if optstr else ""
        lines.append(f"{it['n']}. {it['prompt']}{suffix}")
    lines.append('Reply "1 yes" or just "1".')
    return "\n".join(lines)


def write_manifest(manifest: dict) -> Path:
    """Persist the manifest to gtd_dir()/autopilot-digest.json."""
    from skos.gtd_ingest import gtd_dir
    p = gtd_dir() / "autopilot-digest.json"
    p.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str),
                 encoding="utf-8")
    return p


def rebuild_manifest() -> dict:
    """Rebuild today's manifest over unanswered autopilot items and persist it."""
    from datetime import datetime, timezone
    m = build_manifest(digest_date=datetime.now(timezone.utc).date().isoformat())
    write_manifest(m)
    return m


def _send_alert(text: str, chat: str) -> None:
    """Deliver a Telegram DM through the sovereign alert primitive.

    Shells the installed `sk-alert` shim (chat override via -c), which loads the
    bot token from Hermes .env and posts to the Bot API. Kept as a subprocess so
    the alert path stays the one audited primitive the fleet already uses.
    """
    subprocess.run(["sk-alert", "-c", str(chat), text], check=True)


def send_digest(config, *, dry_run: bool = False) -> dict:
    """Build the numbered digest and DM it (spec section 9.2 / Phase 3 Report).

    In dry-run the DM is suppressed entirely (spec section 14, genuinely
    read-only) except for one opt-in one-line summary when
    ``config.dry_run_summary`` is set.
    """
    manifest = rebuild_manifest()
    n_items = len(manifest.get("items", []))

    if dry_run:
        if not getattr(config, "dry_run_summary", False):
            return {"sent": False, "reason": "dry-run", "items": n_items}
        summary = (f"Autopilot dry-run: {n_items} decision(s) would be queued "
                   f"(preview only, nothing written).")
        _send_alert(summary, config.digest_chat)
        return {"sent": True, "mode": "dry-run-summary", "items": n_items}

    _send_alert(build_digest_text(manifest), config.digest_chat)
    return {"sent": True, "mode": "live", "items": n_items}


def queue_decision(prompt: str, options: dict, action_ref: str | None,
                   priority: str = "high", qid: str | None = None,
                   ttl_hours: float = DEFAULT_DECISION_TTL_HOURS) -> str | None:
    """Write one decision to GTD via the gtd_ingest port (source='autopilot').
    capture() returns None on a duplicate (source, source_ref); fall back to
    upsert() so the resolver's source_ref always resolves (spec section 9.1).
    The single decision-write path, called by both the orchestrator and the
    engineering executor's finalize.

    `qid` is the stable id (criterion 1): derived from action_ref+prompt when
    not given, so it survives every manifest rebuild even though the digest's
    ``n`` does not. `ttl_hours` sets the mandatory expiry (criterion 4)."""
    qid = qid or hashlib.sha256(f"{action_ref}|{prompt}".encode()).hexdigest()[:12]
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
    from skos import gtd_ingest
    c = gtd_ingest.GtdCapture(
        text=prompt, source="autopilot", source_ref=f"autopilot:{qid}",
        status="waiting", context="@decide", priority=priority or "high",
        meta={"decision": {"qid": qid, "prompt": prompt, "options": options,
                           "answered": False, "answer": None, "action_ref": action_ref,
                           "expires_at": expires_at, "status": "pending"}})
    gid = gtd_ingest.capture(c)
    if gid is None:
        gid, _ = gtd_ingest.upsert(c)
    return gid
