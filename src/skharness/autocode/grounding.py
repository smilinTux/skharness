"""grounding.py — host-side, model-free repo grounding for the assess seam.

The assess step judges a card from title/description/acceptance ONLY and, when a
card is vague, historically fails OPEN (rubber-stamps it "valid") and sends it to
a build that cannot converge. This module fills the always-empty
``AssessBrief.codebase_context`` with cheap FACTS read directly from the checked-out
repo on the host: which named artifacts already exist, which acceptance anchors
resolve, and a concreteness score. No sandbox, no model call, no network — just
``git ls-files`` / ``git grep`` against a pinned HEAD.

Safety: grounding NEVER lies against a working tree that disagrees with the base
branch. If the tree is dirty or on an unexpected branch, ``ground_card`` refuses
(returns an empty context with ``concreteness=None``) and the caller falls back to
the text-only assess.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache

# CamelCase symbols (OpsExecutor), snake/path tokens (ops_executor, src/a/b.py),
# and dotted module paths. These are the "anchors" a card references.
_CAMEL = re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")
_PATHISH = re.compile(r"\b([\w/]+\.\w{1,4})\b")
_SNAKE = re.compile(r"\b([a-z][a-z0-9]+(?:_[a-z0-9]+)+)\b")
_NET_NEW = re.compile(r"\b(create|add|new|implement|introduce|scaffold)\b", re.I)


@dataclass
class Grounding:
    """The result of grounding a card against a repo."""

    context: str = ""                       # the codebase_context facts string
    concreteness: float | None = None       # resolved/referenced anchors (None = ungrounded)
    net_new: bool = False                    # acceptance is create-shaped + nothing resolves
    resolved: list[str] = field(default_factory=list)
    referenced: list[str] = field(default_factory=list)
    grounded: bool = False                   # False => caller falls back to text-only assess


def extract_anchors(*texts: str) -> list[str]:
    """Pull candidate code anchors (symbols / paths) out of card text. Deduped,
    order-preserving, bounded so a huge card can't blow the probe budget."""
    seen: dict[str, None] = {}
    for t in texts:
        if not t:
            continue
        for rx in (_PATHISH, _CAMEL, _SNAKE):
            for m in rx.findall(t):
                if len(m) >= 3 and m not in seen:
                    seen[m] = None
    return list(seen)[:24]


@lru_cache(maxsize=64)
def _repo_index(path: str, head: str) -> tuple[str, ...]:
    """All tracked files at a pinned HEAD, cached per (repo, head sha) so the whole
    board is grounded against one ``git ls-files`` per repo per run."""
    out = subprocess.run(["git", "-C", path, "ls-files"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return tuple()
    return tuple(out.stdout.splitlines())


def _head_sha(path: str) -> str | None:
    r = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _tree_clean_on(path: str, base_branch: str | None) -> bool:
    """True when the worktree is clean and (if base_branch given) on that branch.
    Grounding refuses otherwise so it never reports facts a dirty tree contradicts."""
    dirty = subprocess.run(["git", "-C", path, "status", "--porcelain"],
                           capture_output=True, text=True)
    if dirty.returncode != 0 or dirty.stdout.strip():
        return False
    if base_branch:
        cur = subprocess.run(["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True)
        if cur.returncode == 0 and cur.stdout.strip() not in (base_branch, "HEAD"):
            return False
    return True


def _resolve(anchor: str, files: tuple[str, ...], path: str) -> str | None:
    """Return a matching tracked path for an anchor, else None.
    Path-ish anchors match by suffix; symbol anchors match by content grep."""
    if "/" in anchor or "." in anchor:
        for f in files:
            if f.endswith(anchor) or f.endswith(anchor.replace(".", "/") + ".py"):
                return f
        return None
    # symbol: grep tracked files for a definition-ish occurrence
    r = subprocess.run(["git", "-C", path, "grep", "-l", "-F", anchor],
                       capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.splitlines()[0]
    return None


def ground_card(brief, repo_path: str | None, base_branch: str | None = None) -> Grounding:
    """Ground a card against its repo. ``brief`` has .title/.description/.acceptance
    (a list or str). ``repo_path`` is the checked-out repo (None => no repo tag =>
    ungrounded). Pure w.r.t. the process: only reads git, never writes."""
    if not repo_path:
        return Grounding(grounded=False)
    head = _head_sha(repo_path)
    if not head or not _tree_clean_on(repo_path, base_branch):
        return Grounding(grounded=False)   # refuse on missing/dirty/unexpected tree

    acc = brief.acceptance
    acc_text = " ".join(acc) if isinstance(acc, (list, tuple)) else (acc or "")
    anchors = extract_anchors(brief.title or "", acc_text, brief.description or "")
    files = _repo_index(repo_path, head)

    resolved: list[str] = []
    facts: list[str] = []
    for a in anchors:
        hit = _resolve(a, files, repo_path)
        if hit:
            resolved.append(a)
            facts.append(f"{a}: EXISTS ({hit})")
        else:
            facts.append(f"{a}: ABSENT")

    referenced = anchors
    concreteness = (len(resolved) / len(referenced)) if referenced else None
    net_new = bool(_NET_NEW.search(acc_text) or _NET_NEW.search(brief.title or "")) \
        and not resolved
    if not facts:
        facts.append("no code anchors named in the card")
    context = "REPO FACTS (host git, read-only):\n" + "\n".join(facts[:24])
    return Grounding(context=context, concreteness=concreteness, net_new=net_new,
                     resolved=resolved, referenced=referenced, grounded=True)
