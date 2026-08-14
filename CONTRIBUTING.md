# Contributing to skharness

`skharness` is the SKWorld **execution plane**: it holds an RCE-capable daemon and the
gate that decides whether machine-written code may merge. A careless change here does not
break a feature, it removes a leash. Read [SOP.md](./SOP.md) before your first PR, and
[SECURITY.md](./SECURITY.md) before touching anything under "security-sensitive paths"
below.

By contributing you agree your work is licensed under **GPL-3.0-or-later**
([LICENSE](./LICENSE)) and that you follow the [Code of Conduct](./CODE_OF_CONDUCT.md).

## Getting set up

```bash
git clone https://github.com/smilinTux/skharness && cd skharness
python -m pip install -e ".[dev]"
python -m pytest tests/ -q
ruff check src/ tests/
```

**Clone with full history and tags.** The version is derived by `setuptools-scm` from the
newest `v*.*.*` tag; a shallow or tagless checkout yields a placeholder version and can
break resolution against pinned siblings.

Some tests reach optional siblings (`skos`, `capauth`, `skcapstone`, `skcoord`). They
self-skip locally when the sibling is absent. That is correct on a dev box and is a
failure in CI, see "the anti-skip gate" below.

## Branch, commit, PR

- **Never commit to `main` and never push a tag.** Pushing a `v*` tag triggers a PyPI
  publish. Releases are cut automatically by `publish.yml` on a push to `main`.
- Branch from `main`: `feat/...`, `fix/...`, `docs/...`, `ci/...`.
- Conventional-commit style subjects (`feat(daemon): ...`, `fix(autocode): ...`).
- Open a PR against `main` and leave it for review. All four CI jobs must be green.
- If your PR touches `src/**` or `pyproject.toml`, **update `CHANGELOG.md` in the same
  PR**. The docs-check gate enforces this.

## What a good PR contains

1. **A test that fails without the change.** This repo's whole premise is that gates must
   be verified in both directions.
2. **A negative control for anything gate-shaped.** A gate that passes everything is
   worth no more than one that never ran. If you add or modify a check, show in the PR
   body that you broke the underlying fact and confirmed the check went red.
3. **Docs updated alongside the code.** If you change a port, a route, a scope, an env
   var, a unit path, or an entry point, update [SOP.md](./SOP.md) and its
   `docs-evidence` block in the same PR. The tier-3 docs gate executes those checks; a
   drifted SOP is the failure mode this repo cares most about.
4. **No skipped tests presented as passing.** If a test cannot run, say so explicitly.

## Style

- `ruff check src/ tests/`, line length 99, rules `E,F,I,N,W`.
- **Do not refactor `src/skharness/autocode/**`.** It is a byte-identical copy of the
  skos autopilot engine (Phase A of the extraction). `pyproject.toml` scopes off `I001`
  and `N818` there for exactly that reason. Behaviour changes are fine; cosmetic import
  reordering is not, because it destroys the ability to diff the two trees.
- **No em dashes or en dashes** in code, comments, docs, commit messages, or PR bodies.
  Use commas, parentheses, a colon, or a new sentence. Regular hyphens are fine.
- Prefer a comment that explains *why*, especially on any fail-closed path. Several
  fail-closed branches in this repo look like dead code until you know the incident
  behind them.

## Testing rules that are load-bearing

### The anti-skip gate

`tests/test_autopilot_failure_memory_roundtrip.py` spans `skcoord` and `skharness`.
CI captures its output and **fails the job if it contains `skipped`**. Do not "fix" a red
here by adding a skip marker: install the sibling, or fix the contract.

### Sibling install order

CI installs `skos`, `capauth`, `skcapstone`, then **`skcoord` LAST with `--upgrade`**.
`skcapstone` depends on `skcoord>=0.1.0` and drags in the released PyPI wheel, which lags
`main`. Reordering those lines silently turns the job into a test of a stale release.

### Route coverage

`tests/test_route_coverage.py` enumerates the **live** FastAPI route table and asserts
every route is declared in `PUBLIC_ROUTES` or `ROUTE_SCOPES` in `daemon.py`. If you add a
route, declare it. An undeclared route fails the build, by design.

### The systemd unit guardrails

`tests/test_systemd_unit.py` pins the shipped unit's safety defaults: module invocation,
port 9394, an env placeholder rather than a hardcoded host, no wildcard, and the standard
hardening directives. Changing any of those is a deliberate security decision, not a
tidy-up.

## Security-sensitive paths

Changes to these are **security changes**. Expect review to be slow and adversarial, and
say plainly in the PR body what authority the change adds or removes.

| Path | Why |
|---|---|
| `src/skharness/serve.py` | Bind guard, verifier selection, PDP wiring, audit sink. |
| `src/skharness/daemon.py` | Routes and the scope map. |
| `src/skharness/auth.py` | The bearer gate and scope carrier. |
| `src/skharness/harnesses/claude_code.py` | The dispatch allowlist and spawn guard. |
| `src/skharness/autocode/protected.py` | The carve-out detector, which protects itself. |
| `src/skharness/autocode/engineering.py` | The automerge choke point. |
| `systemd/*` | The deployed posture. |

Three rules in this area are not negotiable:

- **Never widen a fail-closed default.** `resolve_bind()` refusing a wildcard, an empty
  `SKCODE_DISPATCH_REPOS` meaning deny-all, `_FAIL_CLOSED` protecting `**` on any
  manifest load failure: each of those exists because the alternative is a silent
  exposure. Do not add a convenience fallback.
- **Never add `skos` or `skharness` to a dispatch allowlist**, in code, in an example, or
  in a test fixture that could be copied. They are the self-modification hazard.
- **Never move a policy decision into this repo.** capauth is the PDP; this daemon is the
  PEP. If a decision needs new policy, it belongs in capauth's rule table.

## Reporting a vulnerability

Do not open a public issue. Use GitHub private vulnerability reporting for this repo
(`Security > Report a vulnerability`). See [SECURITY.md](./SECURITY.md) for scope and
response expectations.
