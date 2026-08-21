# Pi release package-provenance qualification — v0.3.37

Incident `cd4bf7fb` found that the otherwise signed and scanned `v0.3.36`
Pi image installed `skharness==0.0.0`. Release `v0.3.37` replaces that image
with one provenance identity shared by the source tag, Git commit, installed
Python distribution, baked provenance record, and OCI labels.

## Immutable identity

- Source tag: `v0.3.37`
- Source commit: `bbc50926b2afb91cebbfb85b9a98cae9fd575774`
- Registry reference:
  `ghcr.io/smilintux/skharness-pi-python-test@sha256:e7268563898230b39ca512d3614a9263c19bde79d9e1193d1c595d971aec1dfa`
- `.41` local image ID:
  `sha256:1d87013264652cc825359f4430f9d539fe1edd40122bbef3994feefecd9be05d`

## Registry release gates

GitHub Actions run
[`32525548341`](https://github.com/smilinTux/skharness/actions/runs/32525548341)
completed successfully on 2026-08-21. The
[`pi-python-test` publish job](https://github.com/smilinTux/skharness/actions/runs/32525548341/job/96906712445):

- verified that `v0.3.37` peels to the workflow commit;
- supplied release mode, version `0.3.37`, tag `v0.3.37`, and the full commit
  to the Docker build;
- built and installed `skharness-0.3.37`;
- emitted BuildKit maximal provenance and an SBOM;
- ran the version-aware preflight against the immutable digest;
- keyless-signed the digest and verified the expected workflow identity and
  GitHub OIDC issuer.

The independent
[`pi-python-test` vulnerability job](https://github.com/smilinTux/skharness/actions/runs/32525548341/job/96907379238)
passed the High/Critical gate after finding 2 vulnerability matches across
558 packages under the repository's reviewed OpenVEX policy.

## `.41` confined qualification

Host `cbrd21@192.168.0.41` pulled the exact digest and Docker reported the
same digest in `RepoDigests`. Image labels reported:

```text
version=0.3.37
ref.name=v0.3.37
revision=bbc50926b2afb91cebbfb85b9a98cae9fd575774
build-mode=release
```

The digest-pinned image passed its preflight with a read-only root filesystem,
no network, UID/GID `10001:10001`, all capabilities dropped,
`no-new-privileges`, PID limit 128, and a `noexec,nosuid,nodev` temporary
filesystem:

```text
skharness-pi-python-test qualified skharness=0.3.37 pytest=9.0.2
pytest-asyncio=1.4.0 pytest-mock=3.15.1 ruff=0.16.4
skcapstone=0.15.22 skcoord=0.1.16
```

The baked record and installed metadata independently reported:

```json
{"build_mode":"release","version":"0.3.37","tag":"v0.3.37","revision":"bbc50926b2afb91cebbfb85b9a98cae9fd575774"}
```

```text
0.3.37
```

A negative preflight requesting `0.3.38` exited nonzero and reported both the
baked-record mismatch and installed-distribution mismatch. This proves the
runtime join fails closed rather than merely printing provenance.

## Disposition

This evidence satisfies incident `cd4bf7fb`. The `v0.3.36` image remains
historical incident evidence and must not be reused. New Pi trials should pin
the `v0.3.37` digest above or a later independently qualified digest.
