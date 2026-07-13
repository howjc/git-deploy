"""Safe ``op run --`` wrapper for host build commands."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from .build_runner import (
    BuildCommandResult,
    BuildExecutionError,
    BuildResult,
    HostBuildRunner,
)
from .models import BuildConfig
from .docker_runner import DockerCli, DockerProcess
from .models import OnePasswordConfig


class OnePasswordHostRunner:
    """Run host build argv through op without exposing resolved values to Python."""

    def __init__(
        self,
        *,
        op_executable: str = "op",
        source_environment: Mapping[str, str] | None = None,
    ):
        """Bind the official CLI executable and minimal source environment."""

        self.op_executable = op_executable
        self.source_environment = dict(source_environment or os.environ)

    def run(self, worktree: Path, config: BuildConfig) -> BuildResult:
        """Execute fixed ``op run -- secret_exec -- argv`` command chains."""

        if config.runner != "host" or config.onepassword is None:
            raise BuildExecutionError(
                "1Password host runner requires host build.onepassword config",
                phase="configuration",
            )
        environment, redactions = self._op_environment(config)
        cwd = (worktree / config.cwd).resolve()
        try:
            cwd.relative_to(worktree.resolve())
        except ValueError as exc:
            raise BuildExecutionError("build cwd escapes worktree", phase="configuration") from exc
        deadline = time.monotonic() + config.timeout
        results: list[BuildCommandResult] = []
        for index, command in enumerate(config.commands):
            argv = [
                self.op_executable,
                "run",
                "--",
                sys.executable,
                "-m",
                "git_deploy.secret_exec",
                "--",
                *command,
            ]
            started = time.monotonic()
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    start_new_session=True,
                )
            except OSError as exc:
                raise BuildExecutionError(
                    f"cannot start 1Password CLI: {_redact(str(exc), redactions)}",
                    phase="op_start",
                    command_index=index,
                ) from exc
            try:
                stdout_bytes, stderr_bytes = process.communicate(
                    timeout=max(0.001, deadline - time.monotonic())
                )
            except subprocess.TimeoutExpired as exc:
                HostBuildRunner._terminate_group(process)
                process.communicate()
                raise BuildExecutionError(
                    f"1Password build command {index + 1} timed out",
                    phase="timeout",
                    command_index=index,
                    returncode=process.returncode,
                ) from exc
            except KeyboardInterrupt:
                HostBuildRunner._terminate_group(process)
                process.communicate()
                raise
            stdout = _redact(
                stdout_bytes.decode("utf-8", errors="replace"), redactions
            )
            stderr = _redact(
                stderr_bytes.decode("utf-8", errors="replace"), redactions
            )
            returncode = int(process.returncode or 0)
            if returncode != 0:
                raise BuildExecutionError(
                    f"1Password build command {index + 1} exited {returncode}: {stderr}",
                    phase="op_nonzero",
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
        return BuildResult("host", tuple(results))

    def _op_environment(self, config: BuildConfig) -> tuple[dict[str, str], tuple[str, ...]]:
        """Expose only references, allowed ordinary names, and existing OP auth to op."""

        onepassword = config.onepassword
        if onepassword is None:
            raise BuildExecutionError("1Password config missing", phase="configuration")
        references = onepassword.as_dict()
        environment: dict[str, str] = {
            "PATH": self.source_environment.get("PATH", os.defpath),
            "LANG": self.source_environment.get("LANG", "C.UTF-8"),
        }
        for name, value in self.source_environment.items():
            if name.startswith("OP_"):
                environment[name] = value
        for name in config.env_allowlist:
            if name in references:
                environment[name] = references[name]
            elif name in self.source_environment:
                environment[name] = self.source_environment[name]
        redactions = tuple(
            sorted(
                {
                    value
                    for name, value in environment.items()
                    if name.startswith("OP_") or name in config.env_allowlist
                },
                key=len,
                reverse=True,
            )
        )
        return environment, redactions


class OnePasswordDockerCli(DockerCli):
    """Wrap only ``docker run`` with op; image/control commands stay secret-free."""

    def __init__(
        self,
        base: DockerCli,
        onepassword: OnePasswordConfig,
        *,
        op_executable: str = "op",
        source_environment: Mapping[str, str] | None = None,
    ):
        """Bind a base Docker client, opaque refs, and existing OP authentication."""

        super().__init__(base.executable)
        self.base = base
        self.onepassword = onepassword
        self.op_executable = op_executable
        self.source_environment = dict(source_environment or os.environ)

    def run(self, args, *, environment):
        """Delegate inspect/pull/stop/kill/rm without exposing secret refs."""

        return self.base.run(args, environment=environment)

    def start(self, args, *, environment) -> DockerProcess:
        """Start ``op run -- secret_exec -- docker run ...`` with minimal env."""

        op_environment = dict(environment)
        for name, value in self.source_environment.items():
            if name.startswith("OP_"):
                op_environment[name] = value
        for name, reference in self.onepassword.env:
            op_environment[name] = reference
        argv = [
            self.op_executable,
            "run",
            "--",
            sys.executable,
            "-m",
            "git_deploy.secret_exec",
            "--",
            self.base.executable,
            *args,
        ]
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=op_environment,
            start_new_session=True,
        )


def _redact(text: str, values: tuple[str, ...]) -> str:
    """Redact known reference/auth/ordinary allowlist values from output."""

    result = text
    for value in values:
        if value:
            result = result.replace(value, "***")
    return result
