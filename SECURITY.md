# Security Policy

`skharness` executes agent-authored code and owns the **autocode twin gate**, the
predicate that decides whether machine-written work may merge. Its own posture
therefore matters as much as the controls it enforces.

**Maturity-tier:** operational. **Canonical-home:** this file.

## Reporting a vulnerability

Report privately. Do **not** open a public issue for a security bug.

- **Preferred:** GitHub private vulnerability reporting for this repo
  (`Security ▸ Report a vulnerability`).
- **Alternate:** a PGP-encrypted report to the SKWorld security contact via
  CapAuth identity, or the smilinTux / SKWorld maintainers through the SKCapstone
  coordination channel.

Include the affected version, a reproduction, and the impact observed. Expect
acknowledgement within a few days, then a coordinated fix and disclosure
timeline.

## Threat model (summary)

| Asset | Threat | Control |
|---|---|---|
| The twin gate | a change that weakens the merge predicate | `tests/test_autocode_gate_conformance.py` drives the real loop and pins all four arms; the constitutional carve-out sends any diff touching guardrail paths to human review regardless of grade |
| Merge authority | ungated work reaching a protected branch | `DirectExecutor._merge` refuses structurally; `EngineeringExecutor.finalize` refuses a non-gated result before any commit |
| Build isolation | one build affecting another | per-card worktree, sandbox, and PR branch; only the shared git refs and the agent file are lock-serialised |
| Credentials | a secret committed to history | `secret-scan` gate, below |
| Card data | a write clobbering sibling keys | all card mutation goes through `_write_task_raw`, single-writer, pinned to one node |

## Secret handling

**No secret belongs in this repo.** Runtime credentials live in the operator's
environment or `~/.config/...` files outside the tree; the KeePass vault
(`skvault`, master password PGP-sealed) is the source of truth.

### The `secret-scan` gate

`.github/workflows/secret-scan.yml` runs the **gitleaks binary** on every push
and pull request, over the **full history**, and fails the build on a finding.

Two decisions worth knowing, both learned the hard way:

- **The binary, not `gitleaks-action`.** The action requires a paid licence for
  organization-owned repos and exits with `missing gitleaks license` *before
  scanning a single byte*. A sibling repo carried that gate for months: red on
  `main`, therefore ignored, and scanning nothing. gitleaks itself is MIT and
  free. A permanently red check is worse than no check, because people learn to
  route around it.
- **Verify a gate in both directions.** A gate that passes everything is worth
  no more than one that never ran. Before trusting this one, confirm a planted
  secret *fails* it. Note that `AKIAIOSFODNN7EXAMPLE` is AWS's documentation key
  and is allowlisted by gitleaks default rules, so it is a misleading canary.

### If a secret does land

1. **Rotate first.** The credential is compromised from the moment it is pushed.
   Revoking before the replacement is live breaks every consumer mid-swap, so:
   issue the new credential, prove it works, swap every consumer, prove again,
   *then* revoke the old one.
2. **Verify with a call that actually authenticates.** Many list endpoints ignore
   the `Authorization` header and return `200` for a garbage key, which makes a
   dead credential look live and a successful revocation look failed.
3. **Do not allowlist it.** `.gitleaks.toml` is for documented placeholders and
   fixtures only. A real finding is rotated and, if warranted, purged.
4. Purging history is separate cleanup and is **not** a substitute for rotation:
   GitHub keeps unreachable objects addressable by SHA until Support purges
   them, so a force-push alone does not remove the value.

## Dependency posture

- Runtime dependencies are declared in `pyproject.toml` and pinned by floor.
- Optional siblings (`skos`, `capauth`, `skcapstone`, `skcoord`) are **not**
  declared dependencies. Tests that need them guard with
  `pytest.importorskip`, so their absence skips rather than breaking collection.
- CI installs siblings from **git `main`**, not PyPI: their released wheels lag
  `main` by an arbitrary amount, and testing a contract against a stale wheel
  proves nothing about the merged code.
- `build` runs `twine check` on every PR.

## What this repo does NOT claim

- The sandbox is isolation for *accidents*, not a hostile-code boundary. Treat
  agent-authored code as untrusted input to review, not as contained.
- Auto-merge is gated on external CI plus a security-check verdict, and holds on
  any red, any security flag, or any timeout. It is not a proof of correctness.
