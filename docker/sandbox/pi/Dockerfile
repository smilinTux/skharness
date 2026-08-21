# syntax=docker/dockerfile:1.7
# Build context is the repository root; targets are pi-core and pi-polyglot.
ARG WOLFI_IMAGE=cgr.dev/chainguard/wolfi-base:latest@sha256:bfcffaf1336b26a3fd33c8cb31a86a09324d2048420d7f49b983f323b0d33e8d
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.9.27@sha256:143b40f4ab56a780f43377604702107b5a35f83a4453daf1e4be691358718a6a
FROM ${UV_IMAGE} AS uv-tools
FROM ${WOLFI_IMAGE} AS pi-node-builder

RUN apk add --no-cache nodejs-24=24.19.0-r0 npm-12=12.0.2-r2

ARG PI_VERSION=0.84.2
WORKDIR /build/pi
COPY docker/sandbox/pi/package.json docker/sandbox/pi/package-lock.json ./
# npm ci verifies every transitive artifact against package-lock.json. npm itself
# remains in the builder, keeping its unrelated dependency tree out of runtime.
RUN test "$(node -p "require('./package-lock.json').packages['node_modules/@earendil-works/pi-coding-agent'].version")" = "${PI_VERSION}" \
    && npm ci --ignore-scripts --omit=dev

FROM ${WOLFI_IMAGE} AS pi-python-builder
RUN apk add --no-cache build-base=1-r9 python-3.13=3.13.15-r1 \
    python-3.13-dev=3.13.15-r1 py3.13-pip=26.2.1-r0
RUN python3 -m venv /opt/skharness/venv
ENV PATH="/opt/skharness/venv/bin:${PATH}" \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SKHARNESS=0.0.0
COPY pyproject.toml README.md /src/skharness/
COPY src /src/skharness/src
COPY docker/sandbox/pi/requirements.lock /opt/skharness/pi/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /opt/skharness/pi/requirements.lock \
    && pip install --no-cache-dir --no-deps /src/skharness

FROM pi-python-builder AS pi-python-runtime
RUN pip uninstall -y pip setuptools

FROM pi-python-builder AS pi-python-test-builder
COPY docker/sandbox/pi/test-requirements.lock /opt/skharness/pi/test-requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /opt/skharness/pi/test-requirements.lock \
    && pip uninstall -y pip setuptools

FROM ${WOLFI_IMAGE} AS pi-base
LABEL org.opencontainers.image.title="SKHarness Pi worker" \
      org.opencontainers.image.source="https://github.com/smilinTux/skharness" \
      org.opencontainers.image.description="Pi worker routed through SKGateway with scoped SK tools"

RUN apk add --no-cache ca-certificates=20260413-r1 curl=8.21.0-r2 \
    fd=10.4.2-r5 git=2.55.0-r4 jq=1.8.2-r1 nodejs-24=24.19.0-r0 \
    procps=4.0.7-r0 python-3.13=3.13.15-r1 ripgrep=15.2.0-r2 \
    tini=0.19.0-r25

COPY --from=pi-node-builder /build/pi/node_modules /opt/skharness/pi/node_modules
RUN install -d /usr/local/bin \
    && ln -s /opt/skharness/pi/node_modules/.bin/pi /usr/local/bin/pi
COPY --from=pi-python-runtime /opt/skharness/venv /opt/skharness/venv
ENV PATH="/opt/skharness/venv/bin:${PATH}" \
    PI_CODING_AGENT_DIR=/agent \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SKHARNESS=0.0.0

COPY docker/sandbox/pi/requirements.lock /opt/skharness/pi/requirements.lock

COPY docker/sandbox/pi/sk-bridge.ts /opt/skharness/pi/sk-bridge.ts
COPY docker/sandbox/pi/dependencies.lock.json /opt/skharness/pi/dependencies.lock.json

RUN adduser -D -u 10001 -h /home/sbx sbx \
    && install -d -o sbx -g sbx /agent /work /tmp/skharness

FROM pi-base AS pi-core
WORKDIR /work
USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["pi", "--version"]

FROM pi-base AS pi-polyglot
USER root
COPY --from=uv-tools /uv /uvx /usr/local/bin/
RUN apk add --no-cache go-1.27=1.27.0-r1 \
    npm-12=12.0.2-r2 openjdk-21-default-jvm=21.0.12.1-r0 \
    rust-1.97=1.97.1-r0
WORKDIR /work
USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["pi", "--version"]

# Coding-ticket target: polyglot toolchains plus the pinned Python test runner.
# Keep this separate so pi-core retains its smaller runtime-only attack surface.
FROM pi-polyglot AS pi-python-test
USER root
COPY --from=pi-python-test-builder /opt/skharness/venv /opt/skharness/venv
USER 10001:10001
CMD ["pi", "--version"]
