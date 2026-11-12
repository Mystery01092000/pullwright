# Pullwright runner: one throwaway container per issue.
# Build from the repository root:  docker build -f runner/Dockerfile -t pullwright-runner .
# Toolchains: Python 3.13 + uv, Node 22 + npm/pnpm/yarn, git, a C toolchain. For other stacks (Go, Rust, Java),
# extend this image:  FROM pullwright-runner  then add what your repositories need (docs/runners.md).
# Coding agents: Pullwright's own, plus Claude Code and goose (PULLWRIGHT_CODER, docs/coders.md).
FROM node:22-bookworm-slim AS node

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
ARG CLAUDE_CODE_VERSION=2.1.322
ARG GOOSE_VERSION=v1.59.0
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates build-essential procps curl bzip2 libxcb1 libdbus-1-3 \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g corepack@latest && corepack enable

# External coding agents, pinned
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && curl -fsSL "https://github.com/aaif-goose/goose/releases/download/${GOOSE_VERSION}/goose-x86_64-unknown-linux-gnu.tar.bz2" \
    | tar -xj -C /usr/local/bin ./goose \
 && chmod 755 /usr/local/bin/goose && claude --version && goose --version

COPY server /opt/pullwright
RUN uv pip install --system --no-cache /opt/pullwright

RUN useradd --uid 1000 --create-home runner \
 && mkdir -p /home/runner/.cache/uv /home/runner/.cache/npm /home/runner/.cache/pip \
 && chown -R runner:runner /home/runner/.cache
USER runner
WORKDIR /home/runner
# Package caches live in /home/runner/.cache. The Docker runner mounts a shared volume there
# (PULLWRIGHT_RUNNER_CACHE), so later jobs reuse what earlier jobs downloaded.
ENV UV_CACHE_DIR=/home/runner/.cache/uv npm_config_cache=/home/runner/.cache/npm PIP_CACHE_DIR=/home/runner/.cache/pip \
    PYTHONUNBUFFERED=1
ENTRYPOINT []
CMD ["pullwright-work", "--help"]
