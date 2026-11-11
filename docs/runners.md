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
  -e PULLWRIGHT_GITHUB_TOKEN -e ANTHROPIC_API_KEY -e PULLWRIGHT_MAX_COST_USD -e PULLWRIGHT_MAX_ROUNDS \
  <image> timeout 90m pullwright-work owner/repo#n
```

- **Non-root, resource-limited, no host mounts,** and removed when it exits.
- **Secrets are passed by name** (`-e NAME`, with the value in the docker client's environment), so they don't appear on a command line or in process listings.
- **The GitHub token** is an installation token for that one repository that expires within an hour. The clone's saved remote has the token removed, and `git push` uses it once by URL.
- **Agents can't push or call `gh`.** The pipeline pushes the branch and opens the PR itself. The repository's own deny list still applies.
- **Network access** is on, because dependency installs need it. To restrict egress, run the container on a Docker network that allows only your package mirrors, the Anthropic API and GitHub.

### Build the image

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

Then set `PULLWRIGHT_RUNNER_IMAGE` to your image. Services a test suite needs (Postgres, Redis) can be started by the repository's `setup` command in `.pullwright/checks.json`, or provided as sidecar containers on the same Docker network.

### Where the control plane runs

The control plane needs access to a Docker daemon: run it on the host, or in a container with the Docker socket mounted. Mounting the socket gives that container control of the host's Docker, so keep the control plane on a machine dedicated to Pullwright.

## The local runner

`PULLWRIGHT_RUNNER=local` runs `python -m pullwright.runner.job` as a subprocess. The agents' commands run on the control plane's machine with its user's rights. Use it only to work on Pullwright itself.

## EC2 (planned)

The same interface (`runners/base.py`: `start(job) -> handle` with `lines()`, `wait()` and `cancel()`) backed by an EC2 instance per issue:
- launched from a launch template whose AMI holds the runner image;
- user-data runs `pullwright-work`;
- spot by default, terminated when the job ends;
- the event stream delivered by a callback to the control plane instead of stdout.

Issue tracking: see the [roadmap](roadmap.md).
