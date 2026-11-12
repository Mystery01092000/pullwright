# Pullwright control plane (`pullwright-serve`): web UI + API + Slack/Teams + dispatcher (+ LLM proxy).
# Build from the repository root:  docker build -f deploy/server.Dockerfile -t pullwright-server .
# It starts runner containers through the Docker CLI, so it needs the host's Docker socket (see docker-compose.yml).
FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM docker:27-cli AS docker

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
COPY server /opt/pullwright
RUN uv pip install --system --no-cache /opt/pullwright
COPY --from=web /web/dist /opt/pullwright-web
ENV PULLWRIGHT_WEB_DIST=/opt/pullwright-web \
    PULLWRIGHT_DB=/data/pullwright.db \
    PULLWRIGHT_WORKDIR=/data/work \
    PULLWRIGHT_HOST=0.0.0.0 \
    PULLWRIGHT_PORT=8765 \
    PYTHONUNBUFFERED=1
WORKDIR /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8765/api/runs', timeout=4)" || exit 1
CMD ["pullwright-serve"]
