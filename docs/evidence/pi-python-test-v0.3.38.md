# Pi release package-provenance qualification — v0.3.38

Release `v0.3.38` binds source commit
`2e8e4d89aac1967fb297c0558b311998a9bc1e9a` to the immutable image
`ghcr.io/smilintux/skharness-pi-python-test@sha256:8e991c893e7553522369a35d10b78ae2e831eb62b9f127ba53a7dabd045e2c7d`.
The machine-readable release contract is
[`pi-python-test-v0.3.38.release.json`](./pi-python-test-v0.3.38.release.json).

GitHub Actions run
[`32532127259`](https://github.com/smilinTux/skharness/actions/runs/32532127259)
completed successfully on 2026-08-21. Its
[`pi-python-test` publish job](https://github.com/smilinTux/skharness/actions/runs/32532127259/job/96926059810)
built `skharness==0.3.38`, published the exact manifest digest above, emitted
BuildKit provenance and an SBOM, passed the digest-local Python-test capability
probe, keyless-signed the digest, and verified the tag-scoped workflow identity and
GitHub OIDC issuer. The independent
[`pi-python-test` vulnerability job](https://github.com/smilinTux/skharness/actions/runs/32532127259/job/96926773625)
passed the fail-build High/Critical Grype 0.116.1 gate with the reviewed OpenVEX
policy.

The S/M/L qualifier accepts only this equality-pinned image reference. Before live
work it verifies that the local Docker object exposes that exact `RepoDigest` and
OCI labels `version=0.3.38`, `ref.name=v0.3.38`,
`revision=2e8e4d89aac1967fb297c0558b311998a9bc1e9a`, and `build-mode=release`.
It also requires this checked-in JSON contract to match its frozen release record;
a generic digest-shaped reference or another signed release is not substitutable.
