# Sandbox image for modelbench's coding harness: every check/command an agent runs executes in a
# throw-away container from this image (no network, read-only root, no capabilities, 1 GiB, 2 CPUs).
# Add the toolchains your projects need, then: sudo aibox sandbox-rebuild
FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash coreutils git nodejs npm ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
