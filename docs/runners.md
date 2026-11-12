# Runners

A runner is where an approved issue is implemented: a fresh, isolated environment that clones the repository, installs its dependencies, lets the agents work, runs the checks, and opens the pull request. Every backend runs the same command:

```
pullwright-work owner/repo#123
```

The job reads everything else from the issue and the environment. It writes `PWEVENT {json}` lines to stdout as it goes and finishes with one `PWRESULT {json}` line. The dispatcher follows that stream (so the trace shows up in the web UI) and posts the result to the chat thread.

| Backend | Isolation | Use |
|---|---|---|
| `docker` | a throwaway container per issue | production (v1) |
| `local` | none: a subprocess of the control plane | developing Pullwright |
| `ec2` | an instance per issue | planned ([roadmap](roadmap.md)) |

## The Docker runner

`PULLWRIGHT_RUNNER=docker` (the default) starts one container per issue:

```
docker run --rm --name pullwright-<owner>-<repo>-<n> --init \
  --user 1000:1000 --cpus 2 --memory 6g --pids-limit 2048 \
  --security-opt no-new-privileges --network bridge \
  -e PULLWRIGHT_GITHUB_TOKEN -e PULLWRIGHT_LLM_PROXY -e PULLWRIGHT_LLM_TOKEN -e PULLWRIGHT_MAX_COST_USD -e PULLWRIGHT_MAX_ROUNDS \
  <image> timeout 90m pullwright-work owner/repo#n
```

- **Non-root, resource-limited, no host mounts,** and removed when it exits.
- **Secrets are passed by name** (`-e NAME`, with the value in the docker client's environment), so they don't appear on a command line or in process listings.
- **No provider key with the [LLM proxy](providers.md#the-llm-proxy-no-provider-key-in-a-runner):** the runner gets a job token, and every model call goes through the control plane, which enforces the job's dollar cap. Without the proxy, the configured providers' keys are passed by name instead.
- **The GitHub token** is an installation token for that one repository that expires within an hour. The clone's saved remote has the token removed, and `git push` uses it once by URL.
- **Agents can't push or call `gh`.** The pipeline pushes the branch and opens the PR itself. The repository's own deny list still applies.
- **Network access** is on, because dependency installs need it. To restrict egress, run the container on a Docker network that allows only your package mirrors, GitHub and either the control plane (with the proxy) or your model providers (without it).

### Build the image

Released images are on GHCR (`ghcr.io/mystery01092000/pullwright-runner:<version>` and `:latest`, amd64 and arm64), published by `.github/workflows/release.yml` on every `v*` tag. To build your own:

```bash
docker build -f runner/Dockerfile -t ghcr.io/<you>/pullwright-runner:latest .
docker push ghcr.io/<you>/pullwright-runner:latest
```

The image has Python 3.13 with uv, Node 22 with npm and corepack (pnpm, yarn), git and a C toolchain, and runs as uid 1000.

### Add toolchains for your stack

Extend the image instead of changing it:

```dockerfile
FROM ghcr.io/<you>/pullwright-runner:latest
USER root
RUN apt-get update && apt-get install -y --no-install-recommends golang-go openjdk-21-jdk-headless \
 && rm -rf /var/lib/apt/lists/*
USER runner
```

Then set `PULLWRIGHT_RUNNER_IMAGE` to your image. `runner/go/Dockerfile` is a ready example that adds Go 1.25 (`docker build -t pullwright-runner:go runner/go`). Runners run as uid 1000, so an extension must put tool caches under `/home/runner` (for Go: `GOPATH`, `GOMODCACHE`, `GOCACHE`), not `/root`; otherwise the checks fail with "permission denied" even when the agents worked around it in their own shells.

A repository whose stack isn't in the image fails in setup or the first checks round. The runner can't install system toolchains itself, because agents run without root. Services a test suite needs (Postgres, Redis) can be started by the repository's `setup` command in `.pullwright/checks.json`, or provided as sidecar containers on the same Docker network.

### Warm caches

Each job is a fresh container, but downloads don't have to be. The Docker runner mounts one named volume (`PULLWRIGHT_RUNNER_CACHE`, default `pullwright-cache`) at `/home/runner/.cache`. The image points npm, uv, pip and Go's module and build caches there, so the second job on a repository installs from the cache instead of the network. An image that adds a toolchain should keep its cache under the same folder.

The volume is shared by all repositories' jobs. npm, uv and Go check what they read from it against lockfile and checksum hashes, but a job could still plant files in it. When the repositories don't trust each other, give each its own volume, or set `PULLWRIGHT_RUNNER_CACHE=` to turn the cache off.

### Tokens that outlive the job

A runner starts with a newly minted GitHub token for its repository. GitHub App tokens last an hour, and a long job can outlast it. So before pushing, the runner asks the control plane for a fresh one (`POST /llm/github-token` on the LLM proxy). That call needs the job token and a second per-job secret, and agents' commands see neither. Without the LLM proxy the runner keeps the token it started with, so keep jobs under an hour there.

### Where the control plane runs

The control plane needs access to a Docker daemon: run it on the host, or in a container with the Docker socket mounted (`deploy/docker-compose.yml` does this). Mounting the socket gives that container control of the host's Docker, so keep the control plane on a machine dedicated to Pullwright.

## The local runner

`PULLWRIGHT_RUNNER=local` runs `python -m pullwright.runner.job` as a subprocess. The agents' commands run on the control plane's machine with its user's rights. Use it only to work on Pullwright itself.

## EC2 (planned)

The same interface (`runners/base.py`: `start(job) -> handle` with `lines()`, `wait()` and `cancel()`) backed by an EC2 instance per issue:
- launched from a launch template whose AMI holds the runner image;
- user-data runs `pullwright-work`;
- spot by default, terminated when the job ends;
- the event stream delivered by a callback to the control plane instead of stdout.

Issue tracking: see the [roadmap](roadmap.md).
