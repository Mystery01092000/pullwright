# Pullwright runner: one throwaway container per issue.
# Build from the repository root:  docker build -f runner/Dockerfile -t pullwright-runner .
# Toolchains: Python 3.13 + uv, Node 22 + npm/pnpm/yarn, git, a C toolchain. For other stacks (Go, Rust, Java),
# extend this image:  FROM pullwright-runner  then add what your repositories need (docs/runners.md).
FROM node:22-bookworm-slim AS node

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates build-essential procps \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g corepack@latest && corepack enable

COPY server /opt/pullwright
RUN uv pip install --system --no-cache /opt/pullwright

RUN useradd --uid 1000 --create-home runner
USER runner
WORKDIR /home/runner
ENV UV_CACHE_DIR=/home/runner/.cache/uv PYTHONUNBUFFERED=1
ENTRYPOINT []
CMD ["pullwright-work", "--help"]
