# The Pullwright runner plus Go, for repositories with a Go backend (docs/runners.md: "extend this image").
#   docker build -t pullwright-runner:go runner/go      (after building pullwright-runner:dev)
# Runners run as uid 1000 (HOME=/home/runner), so every Go path lives under that home, not /root. The module and
# build caches sit in /home/runner/.cache, which the Docker runner shares between jobs (PULLWRIGHT_RUNNER_CACHE).
ARG BASE=pullwright-runner:dev
FROM golang:1.25-bookworm AS go
FROM ${BASE}
COPY --from=go /usr/local/go /usr/local/go
ENV GOPATH=/home/runner/go \
    GOMODCACHE=/home/runner/.cache/go-mod \
    GOCACHE=/home/runner/.cache/go-build \
    GOFLAGS=-buildvcs=false \
    GOTOOLCHAIN=auto \
    PATH=/usr/local/go/bin:/home/runner/go/bin:$PATH
RUN mkdir -p /home/runner/go /home/runner/.cache/go-mod /home/runner/.cache/go-build && go version
