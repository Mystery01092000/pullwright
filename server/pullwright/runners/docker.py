"""One throwaway container per job, driven through the docker CLI (no extra Python dependency).

Isolation: non-root user, CPU / memory / PID limits, no host mounts, removed when it exits. Secrets reach the
container as `-e NAME` with the value in the docker client's environment, so they never appear on a command line
or in `docker inspect` output of the client process.

Warm caches: one named Docker volume (PULLWRIGHT_RUNNER_CACHE, default `pullwright-cache`; empty turns it off) is
mounted at /home/runner/.cache, where the image keeps the npm, uv, pip and Go caches. Later jobs reuse earlier
downloads instead of fetching every dependency again. The volume is shared by every repository's jobs. npm, uv and Go
verify what they read from it against lockfile hashes, but a job could still plant files in the cache directories.
Give each trust boundary its own volume, or turn the cache off, when the repositories don't trust each other.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass

from .base import Job

DEFAULT_IMAGE = "ghcr.io/mystery01092000/pullwright-runner:latest"


class DockerHandle:
    def __init__(self, name: str, proc: subprocess.Popen[str], docker: str) -> None:
        self.name, self.proc, self.docker = name, proc, docker

    def lines(self) -> Iterator[str]:
        assert self.proc.stdout is not None
        yield from self.proc.stdout

    def wait(self) -> int:
        return self.proc.wait()

    def cancel(self) -> None:
        subprocess.run([self.docker, "rm", "-f", self.name], capture_output=True)
        self.proc.wait()


@dataclass
class DockerRunner:
    image: str = os.environ.get("PULLWRIGHT_RUNNER_IMAGE", DEFAULT_IMAGE)
    cpus: str = "2"
    memory: str = "6g"
    pids: int = 2048
    network: str = "bridge"  # dependency installs need the network; restrict with a custom network if required
    docker: str = "docker"
    timeout_minutes: int = 90
    cache_volume: str = os.environ.get("PULLWRIGHT_RUNNER_CACHE", "pullwright-cache")

    def argv(self, job: Job) -> list[str]:
        names = ["PULLWRIGHT_GITHUB_TOKEN", *sorted(job.env)]
        argv = [self.docker, "run", "--rm", "--name", job.name, "--init",
                "--user", "1000:1000", "--cpus", self.cpus, "--memory", self.memory, "--pids-limit", str(self.pids),
                "--network", self.network, "--security-opt", "no-new-privileges",
                "--stop-timeout", "30"]
        if self.cache_volume:
            argv += ["-v", f"{self.cache_volume}:/home/runner/.cache"]
        for n in names:
            argv += ["-e", n]
        if "host.docker.internal" in job.env.get("PULLWRIGHT_LLM_PROXY", ""):
            argv += ["--add-host", "host.docker.internal:host-gateway"]  # Linux needs it; Docker Desktop accepts it
        return [*argv, self.image, "timeout", f"{self.timeout_minutes}m", "pullwright-work", *job.args]

    def start(self, job: Job) -> DockerHandle:
        env = {**os.environ, **job.env, "PULLWRIGHT_GITHUB_TOKEN": job.github_token}
        proc = subprocess.Popen(self.argv(job), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", env=env)
        return DockerHandle(job.name, proc, self.docker)
