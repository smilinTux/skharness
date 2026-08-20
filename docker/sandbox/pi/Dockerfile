# syntax=docker/dockerfile:1.7
# Build context is the repository root; targets are pi-core and pi-polyglot.
FROM node:24-bookworm@sha256:934240a162082fd8b8a2f90cd5114446443f1eba1c5378f6687167ca405e6584 AS pi-base

ARG PI_VERSION=0.84.2
LABEL org.opencontainers.image.title="SKHarness Pi worker" \
      org.opencontainers.image.source="https://github.com/smilinTux/skharness" \
      org.opencontainers.image.description="Pi worker routed through SKGateway with scoped SK tools"

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential=12.9 \
       ca-certificates=20250419~deb12u1 curl=7.88.1-10+deb12u15 fd-find=8.6.0-3 \
       git=1:2.39.5-0+deb12u3 jq=1.6-2.1+deb12u2 procps=2:4.0.2-3 \
       python3=3.11.2-1+b1 python3-pip=23.0.1+dfsg-1 python3-venv=3.11.2-1+b1 \
       ripgrep=13.0.0-4+b2 tini=0.19.0-1+b3 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Exact package version. Registry integrity and the observed base manifests are
# recorded in dependencies.lock.json and checked by repository tests.
RUN npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}"

RUN python3 -m venv /opt/skharness/venv
ENV PATH="/opt/skharness/venv/bin:${PATH}" \
    PI_CODING_AGENT_DIR=/agent \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SKHARNESS=0.0.0

COPY pyproject.toml README.md /src/skharness/
COPY src /src/skharness/src
COPY docker/sandbox/pi/requirements.lock /opt/skharness/pi/requirements.lock
COPY docker/sandbox/pi/test-requirements.lock /opt/skharness/pi/test-requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /opt/skharness/pi/requirements.lock \
    && pip install --no-cache-dir --no-deps /src/skharness \
    && pip install --no-cache-dir --require-hashes -r /opt/skharness/pi/test-requirements.lock

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
RUN apt-get update \
    && apt-get install -y --no-install-recommends cargo=0.66.0+ds1-1 \
       golang-go=2:1.19~1 openjdk-17-jdk-headless=17.0.20+8-1~deb12u1 \
       pkg-config=1.8.1-1 rustc=1.63.0+dfsg1-2 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
WORKDIR /work
USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["pi", "--version"]
