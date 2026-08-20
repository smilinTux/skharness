# syntax=docker/dockerfile:1.7
# Build context is the repository root; targets are pi-core and pi-polyglot.
ARG NODE_IMAGE=node:24-trixie-slim@sha256:0711b541c1c33a8a530ac4f0d391baa9a15b3d804695b1b24a47daa5fb60e74d
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.9.27@sha256:143b40f4ab56a780f43377604702107b5a35f83a4453daf1e4be691358718a6a
FROM ${UV_IMAGE} AS uv-tools
FROM ${NODE_IMAGE} AS pi-node-builder

ARG PI_VERSION=0.84.2
WORKDIR /build/pi
COPY docker/sandbox/pi/package.json docker/sandbox/pi/package-lock.json ./
# npm ci verifies every transitive artifact against package-lock.json. npm itself
# remains in the builder, keeping its unrelated dependency tree out of runtime.
RUN test "$(node -p "require('./package-lock.json').packages['node_modules/@earendil-works/pi-coding-agent'].version")" = "${PI_VERSION}" \
    && npm ci --ignore-scripts --omit=dev

FROM ${NODE_IMAGE} AS pi-python-builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential=12.12 \
       python3=3.13.5-1 python3-dev=3.13.5-1 python3-pip=25.1.1+dfsg-1 \
       python3-venv=3.13.5-1 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/skharness/venv
ENV PATH="/opt/skharness/venv/bin:${PATH}" \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SKHARNESS=0.0.0
COPY pyproject.toml README.md /src/skharness/
COPY src /src/skharness/src
COPY docker/sandbox/pi/requirements.lock /opt/skharness/pi/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /opt/skharness/pi/requirements.lock \
    && pip install --no-cache-dir --no-deps /src/skharness \
    && pip uninstall -y pip setuptools

FROM ${NODE_IMAGE} AS pi-base
LABEL org.opencontainers.image.title="SKHarness Pi worker" \
      org.opencontainers.image.source="https://github.com/smilinTux/skharness" \
      org.opencontainers.image.description="Pi worker routed through SKGateway with scoped SK tools"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates=20250419 \
       curl=8.14.1-2+deb13u4 fd-find=10.2.0-1+b5 git=1:2.47.3-0+deb13u1 \
       jq=1.7.1-6+deb13u3 procps=2:4.0.4-9 python3=3.13.5-1 \
       ripgrep=14.1.1-1+b4 tini=0.19.0-3+b7 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY --from=pi-node-builder /build/pi/node_modules /opt/skharness/pi/node_modules
RUN ln -s /opt/skharness/pi/node_modules/.bin/pi /usr/local/bin/pi \
    && rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm \
       /usr/local/bin/npx /usr/local/bin/corepack
COPY --from=pi-python-builder /opt/skharness/venv /opt/skharness/venv
ENV PATH="/opt/skharness/venv/bin:${PATH}" \
    PI_CODING_AGENT_DIR=/agent \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SKHARNESS=0.0.0

COPY docker/sandbox/pi/requirements.lock /opt/skharness/pi/requirements.lock

COPY docker/sandbox/pi/sk-bridge.ts /opt/skharness/pi/sk-bridge.ts
COPY docker/sandbox/pi/dependencies.lock.json /opt/skharness/pi/dependencies.lock.json

RUN useradd -u 10001 -m -d /home/sbx sbx \
    && install -d -o sbx -g sbx /agent /work /tmp/skharness

FROM pi-base AS pi-core
WORKDIR /work
USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["pi", "--version"]

FROM pi-base AS pi-polyglot
USER root
COPY --from=pi-node-builder /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY --from=uv-tools /uv /uvx /usr/local/bin/
RUN apt-get update \
    && apt-get install -y --no-install-recommends cargo=1.85.0+dfsg3-1 \
       golang-go=2:1.24~2 openjdk-21-jdk-headless=21.0.12+8-1~deb13u1 \
       pkg-config=1.8.1-4 rustc=1.85.0+dfsg3-1 \
    && ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
WORKDIR /work
USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["pi", "--version"]
