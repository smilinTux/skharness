# Card 7fe0fb6b: signed `pi-python-test` release qualification

Release `v0.3.36` published the `pi-python-test` target from SKHarness commit
`3bfe5b4a82d25cbf02d1174fb5f0fd6ec530bead` on 2026-08-21.

## Immutable identity

- Registry reference:
  `ghcr.io/smilintux/skharness-pi-python-test@sha256:f8fbc2f8733aae10ebaf3c8f268c9d4e004ccf34db2e5f0458452548b3f3acd3`
- `.41` local image ID after digest pull:
  `sha256:f006dc937ef7a78dd83eafb2549c51913cbab693b81fdf701dec4664675103e5`
- Runtime user: `10001:10001`
- Workflow run:
  `https://github.com/smilinTux/skharness/actions/runs/32522425659`
- Publish/sign/verify job:
  `https://github.com/smilinTux/skharness/actions/runs/32522425659/job/96897293157`
- Vulnerability job:
  `https://github.com/smilinTux/skharness/actions/runs/32522425659/job/96898060251`

## Release gates

The tag workflow built and pushed the image by digest with BuildKit SBOM and maximal
provenance attestations. It then pulled the exact digest and ran the immutable Python
preflight under a read-only root filesystem, UID `10001`, and noexec/nosuid/nodev
temporary filesystem.

The preflight reported:

```text
skharness-pi-python-test qualified skharness=0.0.0 pytest=9.0.2
pytest-asyncio=1.4.0 pytest-mock=3.15.1 ruff=0.16.4
skcapstone=0.15.22 skcoord=0.1.16
```

Cosign then keyless-signed the immutable digest. Verification bound the signature to
the repository's `pi-image.yml` workflow identity on tag `v0.3.36` and the GitHub Actions
OIDC issuer; claims, transparency-log inclusion, and certificate trust all verified.

The independent vulnerability job pulled the same tag, observed image content digest
`sha256:f006dc937ef7a78dd83eafb2549c51913cbab693b81fdf701dec4664675103e5`,
and ran Grype `0.116.1` with the checked-in OpenVEX document and `--fail-on high`.
It found two matches across 558 packages and exited successfully, meaning no
unaccounted High/Critical finding crossed the release gate.

## `.41` qualification

Node `.41` pulled the immutable registry digest—not a mutable local tag—and ran:

```text
read-only rootfs; network none; UID 10001:10001; cap-drop ALL;
no-new-privileges; pids-limit 128; noexec/nosuid/nodev tmpfs
```

The same preflight passed and Docker reported the exact registry digest in
`RepoDigests`. This closes the publication/signature/scan/digest-pull acceptance gate.
It does not claim that every future release digest is qualified; the workflow must
re-run these checks for every tag.
