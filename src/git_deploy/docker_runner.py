"""Restricted Docker CLI build backend with coordinated lifecycle cleanup."""

from __future__ import annotations

import os
import secrets
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .build_runner import BuildCommandResult, BuildExecutionError, BuildResult
from .models import BuildConfig


@dataclass(frozen=True)
class DockerImageIdentity:
    """Configured image reference resolved to immutable local content identity."""

    reference: str
    image_id: str


class DockerProcess(Protocol):
    """Minimal running process surface used by real and fake Docker clients."""

    returncode: int | None

    def communicate(
        self,
        input: bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        """Wait for process output or raise ``TimeoutExpired``."""

    def poll(self) -> int | None:
        """Return process status."""

    def terminate(self) -> None:
        """Terminate the local Docker CLI process."""

    def kill(self) -> None:
        """Kill the local Docker CLI process."""


class DockerCli:
    """Subprocess adapter for deterministic Docker CLI calls."""

    def __init__(self, executable: str = "docker"):
        """Configure the Docker executable name/path."""

        self.executable = executable

    def run(
        self,
        args: Sequence[str],
        *,
        environment: Mapping[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        """Run a bounded Docker control command and capture output."""

        return subprocess.run(
            [self.executable, *args],
            check=False,
            capture_output=True,
            env=dict(environment),
            timeout=30,
        )

    def start(
        self,
        args: Sequence[str],
        *,
        environment: Mapping[str, str],
    ) -> DockerProcess:
        """Start a long-running Docker run command."""

        return subprocess.Popen(
            [self.executable, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment),
            start_new_session=True,
        )


class DockerBuildRunner:
    """Execute build argv in named, least-privilege, always-removed containers."""

    daemon_warning = (
        "Docker daemon administrators can inspect environment metadata of a live container"
    )
    _DOCKER_ENV_NAMES = (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    )

    def __init__(
        self,
        *,
        cli: DockerCli | None = None,
        source_environment: Mapping[str, str] | None = None,
    ):
        """Bind an injectable Docker CLI and separated client environment."""

        self.cli = cli or DockerCli()
        self.source_environment = dict(source_environment or os.environ)

    def resolve_image(self, config: BuildConfig) -> DockerImageIdentity:
        """Resolve/pull according to policy and require an immutable image ID."""

        docker = config.docker
        if config.runner != "docker" or docker is None:
            raise BuildExecutionError("Docker runner configuration is missing", phase="configuration")
        environment = self._client_environment(config, injected_environment=None)
        inspected = self.cli.run(
            ["image", "inspect", "--format", "{{.Id}}", docker.image],
            environment=environment,
        )
        if inspected.returncode != 0:
            if docker.pull_policy != "missing":
                raise BuildExecutionError(
                    f"Docker image is missing and pull_policy={docker.pull_policy}: {docker.image}",
                    phase="image",
                    returncode=inspected.returncode,
                )
            pulled = self.cli.run(["pull", docker.image], environment=environment)
            if pulled.returncode != 0:
                raise BuildExecutionError(
                    "Docker image pull failed",
                    phase="image",
                    returncode=pulled.returncode,
                )
            inspected = self.cli.run(
                ["image", "inspect", "--format", "{{.Id}}", docker.image],
                environment=environment,
            )
        image_id = inspected.stdout.decode("utf-8", errors="replace").strip()
        if inspected.returncode != 0 or not image_id.startswith("sha256:"):
            raise BuildExecutionError(
                "Docker inspect did not return an immutable sha256 image ID",
                phase="image",
                returncode=inspected.returncode,
            )
        return DockerImageIdentity(docker.image, image_id)

    def run(
        self,
        worktree: Path,
        config: BuildConfig,
        *,
        image: DockerImageIdentity | None = None,
        injected_environment: Mapping[str, str] | None = None,
    ) -> BuildResult:
        """Run all configured commands and remove every named container."""

        docker = config.docker
        if config.runner != "docker" or docker is None:
            raise BuildExecutionError("Docker runner configuration is missing", phase="configuration")
        identity = image or self.resolve_image(config)
        environment = self._client_environment(
            config, injected_environment=injected_environment
        )
        sensitive = tuple(
            sorted(
                {
                    environment[name]
                    for name in config.env_allowlist
                    if name in environment and environment[name]
                },
                key=len,
                reverse=True,
            )
        )
        deadline = time.monotonic() + config.timeout
        results: list[BuildCommandResult] = []
        for index, command in enumerate(config.commands):
            name = f"git-deploy-build-{secrets.token_hex(6)}-{index}"
            argv = self._run_argv(
                name,
                worktree.resolve(),
                config,
                identity,
                command,
            )
            started = time.monotonic()
            process = self.cli.start(argv, environment=environment)
            try:
                remaining = max(0.001, deadline - time.monotonic())
                stdout_bytes, stderr_bytes = process.communicate(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                self._stop_kill_remove(name, process, environment)
                raise BuildExecutionError(
                    f"Docker build command {index + 1} timed out",
                    phase="timeout",
                    command_index=index,
                    returncode=process.returncode,
                ) from exc
            except KeyboardInterrupt:
                self._stop_kill_remove(name, process, environment)
                raise
            cleanup_error = self._remove(name, environment)
            stdout = _redact(
                stdout_bytes.decode("utf-8", errors="replace"), sensitive
            )
            stderr = _redact(
                stderr_bytes.decode("utf-8", errors="replace"), sensitive
            )
            if cleanup_error is not None:
                raise cleanup_error
            returncode = int(process.returncode or 0)
            if returncode != 0:
                raise BuildExecutionError(
                    f"Docker build command {index + 1} exited {returncode}: {stderr}",
                    phase="nonzero",
                    command_index=index,
                    returncode=returncode,
                )
            results.append(
                BuildCommandResult(
                    argv=command,
                    returncode=returncode,
                    stdout=stdout,
                    stderr=stderr,
                    duration_seconds=time.monotonic() - started,
                )
            )
        return BuildResult("docker", tuple(results))

    def _run_argv(
        self,
        name: str,
        worktree: Path,
        config: BuildConfig,
        identity: DockerImageIdentity,
        command: tuple[str, ...],
    ) -> list[str]:
        """Compose the fixed Docker run policy and one literal build argv."""

        docker = config.docker
        if docker is None:
            raise BuildExecutionError("Docker config missing", phase="configuration")
        argv = [
            "run",
            "--name",
            name,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--workdir",
            f"/workspace/{config.cwd}" if config.cwd != "." else "/workspace",
            "--mount",
            f"type=bind,src={worktree},dst=/workspace",
            "--network",
            docker.network,
            "--platform",
            docker.platform,
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--env",
            "HOME=/tmp/git-deploy-home",
            "--env",
            "TMPDIR=/tmp",
        ]
        for name_value in config.env_allowlist:
            argv.extend(("--env", name_value))
        argv.extend((identity.image_id, *command))
        return argv

    def _client_environment(
        self,
        config: BuildConfig,
        *,
        injected_environment: Mapping[str, str] | None,
    ) -> dict[str, str]:
        """Separate Docker daemon settings from allowlisted container values."""

        environment: dict[str, str] = {
            "PATH": self.source_environment.get("PATH", os.defpath),
            "LANG": self.source_environment.get("LANG", "C.UTF-8"),
        }
        for name in self._DOCKER_ENV_NAMES:
            if name in self.source_environment:
                environment[name] = self.source_environment[name]
        injected = dict(injected_environment or {})
        for name in config.env_allowlist:
            if name in injected:
                environment[name] = injected[name]
            elif name in self.source_environment:
                environment[name] = self.source_environment[name]
        for name in tuple(environment):
            if name.startswith("OP_"):
                environment.pop(name, None)
        return environment

    def _stop_kill_remove(
        self,
        name: str,
        process: DockerProcess,
        environment: Mapping[str, str],
    ) -> None:
        """Coordinate stop→bounded wait→kill→remove for timeout/interrupt."""

        self.cli.run(["stop", "--time", "2", name], environment=environment)
        try:
            process.communicate(timeout=2.5)
        except subprocess.TimeoutExpired:
            self.cli.run(["kill", name], environment=environment)
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.communicate(timeout=1.0)
        error = self._remove(name, environment)
        if error is not None:
            raise error

    def _remove(
        self, name: str, environment: Mapping[str, str]
    ) -> BuildExecutionError | None:
        """Remove a named container and return a structured cleanup failure."""

        removed = self.cli.run(["rm", "--force", name], environment=environment)
        if removed.returncode == 0:
            return None
        return BuildExecutionError(
            f"failed to remove Docker build container {name}",
            phase="cleanup",
            returncode=removed.returncode,
        )


def _redact(text: str, sensitive_values: tuple[str, ...]) -> str:
    """Redact injected values from Docker-captured output."""

    result = text
    for value in sensitive_values:
        result = result.replace(value, "***")
    return result
