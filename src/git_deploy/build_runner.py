"""Restricted host build command execution."""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import PolicyError
from .models import BuildConfig


@dataclass(frozen=True)
class BuildCommandResult:
    """Redacted result of one completed build command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float


@dataclass(frozen=True)
class BuildResult:
    """Successful ordered build command results."""

    runner: str
    commands: tuple[BuildCommandResult, ...]


class BuildExecutionError(PolicyError):
    """Structured, secret-redacted build failure."""

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        command_index: int | None = None,
        returncode: int | None = None,
    ):
        """Record a stable build phase and optional process details."""

        super().__init__(message)
        self.phase = phase
        self.command_index = command_index
        self.returncode = returncode


class HostBuildRunner:
    """Execute configured argv directly with an allowlisted environment."""

    permission_warning = (
        "host build runs selected repository code with the current user's "
        "filesystem and network permissions"
    )

    def __init__(self, *, source_environment: Mapping[str, str] | None = None):
        """Bind the source environment used only for explicitly allowed names."""

        self.source_environment = dict(source_environment or os.environ)

    def run(
        self,
        worktree: Path,
        config: BuildConfig,
        *,
        injected_environment: Mapping[str, str] | None = None,
    ) -> BuildResult:
        """Execute host commands without a shell and terminate process groups on failure.

        Args:
            worktree: Exact isolated source tree.
            config: Validated host build configuration.
            injected_environment: Optional provider-resolved values for allowlisted names.

        Returns:
            Redacted command results.
        """

        if config.runner != "host":
            raise BuildExecutionError(
                f"host runner cannot execute runner={config.runner!r}", phase="configuration"
            )
        cwd = (worktree / config.cwd).resolve()
        try:
            cwd.relative_to(worktree.resolve())
        except ValueError as exc:
            raise BuildExecutionError("build cwd escapes isolated worktree", phase="configuration") from exc
        if not cwd.is_dir():
            raise BuildExecutionError(f"build cwd does not exist: {config.cwd}", phase="configuration")

        isolated_home = Path(
            tempfile.mkdtemp(prefix=".git-deploy-build-home-", dir=worktree.parent)
        )
        environment, sensitive_values = self._environment(
            config,
            injected_environment=injected_environment,
            isolated_home=isolated_home,
        )
        deadline = time.monotonic() + config.timeout
        results: list[BuildCommandResult] = []
        for index, argv in enumerate(config.commands):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _cleanup_isolated_home(isolated_home)
                raise BuildExecutionError(
                    f"build timed out after {config.timeout}s",
                    phase="timeout",
                    command_index=index,
                )
            started = time.monotonic()
            try:
                process = subprocess.Popen(
                    list(argv),
                    cwd=cwd,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    start_new_session=True,
                )
            except OSError as exc:
                safe = _redact(str(exc), sensitive_values)
                _cleanup_isolated_home(isolated_home)
                raise BuildExecutionError(
                    f"cannot start build command {index + 1}: {safe}",
                    phase="start",
                    command_index=index,
                ) from exc
            try:
                stdout_bytes, stderr_bytes = process.communicate(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                self._terminate_group(process)
                stdout_bytes, stderr_bytes = process.communicate()
                detail = _redact(
                    stderr_bytes.decode("utf-8", errors="replace"), sensitive_values
                )
                _cleanup_isolated_home(isolated_home)
                raise BuildExecutionError(
                    f"build command {index + 1} timed out after {config.timeout}s: {detail}",
                    phase="timeout",
                    command_index=index,
                    returncode=process.returncode,
                ) from exc
            except KeyboardInterrupt:
                self._terminate_group(process)
                process.communicate()
                _cleanup_isolated_home(isolated_home)
                raise

            stdout = _redact(
                stdout_bytes.decode("utf-8", errors="replace"), sensitive_values
            )
            stderr = _redact(
                stderr_bytes.decode("utf-8", errors="replace"), sensitive_values
            )
            duration = time.monotonic() - started
            result = BuildCommandResult(
                argv=argv,
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration,
            )
            if process.returncode != 0:
                _cleanup_isolated_home(isolated_home)
                raise BuildExecutionError(
                    f"build command {index + 1} exited {process.returncode}: {stderr}",
                    phase="nonzero",
                    command_index=index,
                    returncode=process.returncode,
                )
            results.append(result)
        _cleanup_isolated_home(isolated_home)
        return BuildResult(runner="host", commands=tuple(results))

    def _environment(
        self,
        config: BuildConfig,
        *,
        injected_environment: Mapping[str, str] | None,
        isolated_home: Path,
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        """Build the minimal child environment and sensitive redaction set."""

        environment: dict[str, str] = {
            "PATH": self.source_environment.get("PATH", os.defpath),
            "LANG": self.source_environment.get("LANG", "C.UTF-8"),
            "HOME": str(isolated_home),
        }
        injected = dict(injected_environment or {})
        for name in config.env_allowlist:
            if name in injected:
                environment[name] = injected[name]
            elif name in self.source_environment:
                environment[name] = self.source_environment[name]
        # Provider authentication is never inherited by the actual build.
        for name in tuple(environment):
            if name.startswith("OP_"):
                environment.pop(name, None)
        sensitive = tuple(
            sorted(
                {value for name, value in environment.items() if name in config.env_allowlist and value},
                key=len,
                reverse=True,
            )
        )
        return environment, sensitive

    @staticmethod
    def _terminate_group(process: subprocess.Popen[bytes]) -> None:
        """Terminate then kill the entire build process group idempotently."""

        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=1.0)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=1.0)


def _redact(text: str, sensitive_values: tuple[str, ...]) -> str:
    """Replace every known sensitive environment value in captured text."""

    redacted = text
    for value in sensitive_values:
        redacted = redacted.replace(value, "***")
    return redacted


def _cleanup_isolated_home(path: Path) -> None:
    """Remove the runner-owned HOME without following a replaced symlink."""

    try:
        if path.is_symlink():
            path.unlink()
        else:
            shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise BuildExecutionError(
            f"failed to clean isolated build HOME: {exc}", phase="cleanup"
        ) from exc
