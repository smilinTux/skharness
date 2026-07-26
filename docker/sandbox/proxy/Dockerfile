# Sovereign egress allowlist proxy sidecar for the autopilot sandbox.
# Runs `python -m skharness.autocode.sandbox_proxy <port> <hosts...>` (invoked by
# Sandbox.spawn). Only the single stdlib-only proxy module is present; a minimal
# package tree makes the -m import resolve without pulling the skharness deps.
FROM python:3.12-slim

RUN mkdir -p /app/skharness/autocode \
    && touch /app/skharness/__init__.py /app/skharness/autocode/__init__.py
COPY src/skharness/autocode/sandbox_proxy.py /app/skharness/autocode/sandbox_proxy.py
ENV PYTHONPATH=/app
WORKDIR /app
# Command is supplied by Sandbox.spawn:
#   python -m skharness.autocode.sandbox_proxy <port> <allow-host> <allow-host> ...
