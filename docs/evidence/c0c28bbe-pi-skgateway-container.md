# Pi container to SKGateway qualification

Card `c0c28bbe` requires one joined proof that real Pi uses the canonical
provider configuration, reaches only its assigned OpenAI-compatible gateway,
preserves request attribution, observes the provider-owned served model, and
returns assistant output.

## Immutable runtime

- Image:
  `ghcr.io/smilintux/skharness-pi-python-test@sha256:e7268563898230b39ca512d3614a9263c19bde79d9e1193d1c595d971aec1dfa`
- Release: `v0.3.37`
- Installed package: `skharness==0.3.37`
- Image revision: `bbc50926b2afb91cebbfb85b9a98cae9fd575774`
- Test: `tests/test_pi_container_gateway_it.py`

The test refuses a mutable image tag. Both the Python-standard-library mock
gateway and the Pi worker run from the same supplied digest on a unique Docker
`--internal` network. Both containers use a read-only root filesystem, all
capabilities dropped, `no-new-privileges`, PID limits, and bounded `noexec`
temporary filesystems.

## Joined observations

The real `PiAdapter` generated `providers.skgw` configuration with the gateway
base URL, `openai-completions` API mode, API key, compatibility object, and
model catalog. Real Pi was invoked as `--model skgw/reference` and the mock
gateway observed:

```text
path=/v1/chat/completions
model=reference
x-session-id=c0c28bbe-container-it
x-sk-card-id=c0c28bbe
```

The gateway returned request ID `confined-mock-request-1`, backend
`confined-mock`, and served model `served-reference`. Pi's provider-owned
`responseModel` was `served-reference`; its assistant message parsed as
`{"qualified": true}`.

From that same worker container, a raw TCP connection to `1.1.1.1:443` failed.
This is a joined routing-and-confinement proof: a host-only Pi test, an
independent generic sandbox test, or host networking cannot satisfy it.

## Validation and cleanup

The focused provider, bucket, host-mock, and container integration suite
reported `50 passed`. Ruff and `git diff --check` passed. The test's `finally`
path independently attempts removal of both named containers and the unique
network, accumulates any cleanup errors, and reports them only after every
removal was attempted. Post-run inspection found no `skh-pi-gateway-*`,
`skh-pi-worker-*`, or `skh-pi-gw-*` resources.

This evidence satisfies `c0c28bbe`. It does not claim a live role route or
production served-model persistence; card `d3c6377a` owns those separate
runtime and RunRecord gates.
