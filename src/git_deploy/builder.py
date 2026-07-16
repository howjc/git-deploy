"""Run trusted local build commands before any remote connection is opened."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from git_deploy.config import BuildConfig
from git_deploy.errors import BuildError


def run_build(config: BuildConfig, project_root: Path) -> None:
    """Run configured shell steps serially with live inherited output.

    Args:
        config: Trusted build steps and aggregate timeout.
        project_root: Working directory used for every command.

    Returns:
        ``None`` after all steps complete successfully.
    """

    started = time.monotonic()
    for index, step in enumerate(config.steps, start=1):
        remaining = None
        if config.timeout is not None:
            remaining = config.timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise BuildError(f"build timed out before step {index}: {step}")
        print(f"[build {index}/{len(config.steps)}] {step}", flush=True)
        try:
            process = subprocess.Popen(
                step,
                cwd=project_root,
                shell=True,
                start_new_session=True,
            )
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            _terminate_process(process)
            raise BuildError(f"build timed out in step {index}: {step}") from exc
        except KeyboardInterrupt:
            _terminate_process(process)
            raise
        except OSError as exc:
            raise BuildError(f"cannot start build step {index}: {exc}") from exc
        if returncode != 0:
            raise BuildError(f"build step {index} failed with exit code {returncode}: {step}")


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    """Terminate a build's whole process group so shell children cannot leak."""

    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
