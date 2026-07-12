"""Restricted host BuildRunner tests."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from git_deploy.build_runner import BuildExecutionError, HostBuildRunner
from git_deploy.models import BuildConfig


def _config(*commands: tuple[str, ...], timeout: int = 5) -> BuildConfig:
    """Build a minimal validated-equivalent host configuration."""

    return BuildConfig(runner="host", commands=commands, timeout=timeout)


def test_host_argv_cwd_and_success(tmp_path: Path) -> None:
    """Commands receive literal argv, run in configured cwd, and execute in order."""

    worktree = tmp_path / "tree"
    cwd = worktree / "frontend"
    cwd.mkdir(parents=True)
    config = BuildConfig(
        runner="host",
        commands=(
            (
                sys.executable,
                "-c",
                "import pathlib,sys; pathlib.Path('argv.txt').write_text(sys.argv[1])",
                "literal;not-shell-expanded",
            ),
            (sys.executable, "-c", "import pathlib; pathlib.Path('done').write_text('ok')"),
        ),
        cwd="frontend",
        timeout=5,
    )
    result = HostBuildRunner().run(worktree, config)
    assert result.runner == "host"
    assert len(result.commands) == 2
    assert (cwd / "argv.txt").read_text() == "literal;not-shell-expanded"
    assert (cwd / "done").read_text() == "ok"


def test_host_environment_allowlist_and_redaction(tmp_path: Path) -> None:
    """Only allowed names enter the child and their values are redacted from output."""

    worktree = tmp_path / "tree"
    worktree.mkdir()
    sentinel = "SECRET-SENTINEL-123"
    command = (
        sys.executable,
        "-c",
        "import os,sys; print(os.getenv('ALLOWED')); print(os.getenv('BLOCKED')); "
        "print(os.getenv('ALLOWED'), file=sys.stderr)",
    )
    config = BuildConfig(
        runner="host",
        commands=(command,),
        env_allowlist=("ALLOWED",),
    )
    runner = HostBuildRunner(
        source_environment={
            "PATH": os.environ.get("PATH", ""),
            "ALLOWED": sentinel,
            "BLOCKED": "must-not-enter",
        }
    )
    result = runner.run(worktree, config)
    captured = result.commands[0].stdout + result.commands[0].stderr
    assert sentinel not in captured
    assert "must-not-enter" not in captured
    assert "***" in captured
    assert "None" in captured


def test_host_nonzero_is_structured_and_redacted(tmp_path: Path) -> None:
    """Non-zero exits expose stable fields without leaking allowed values."""

    worktree = tmp_path / "tree"
    worktree.mkdir()
    sentinel = "FAIL-SECRET-456"
    config = BuildConfig(
        runner="host",
        commands=((sys.executable, "-c", "import os,sys; sys.stderr.write(os.environ['TOKEN']); sys.exit(7)"),),
        env_allowlist=("TOKEN",),
    )
    runner = HostBuildRunner(
        source_environment={"PATH": os.environ.get("PATH", ""), "TOKEN": sentinel}
    )
    with pytest.raises(BuildExecutionError) as raised:
        runner.run(worktree, config)
    assert raised.value.phase == "nonzero"
    assert raised.value.returncode == 7
    assert sentinel not in str(raised.value)
    assert "***" in str(raised.value)


def test_host_timeout_terminates_process_group(tmp_path: Path) -> None:
    """Timeout terminates the command and a child in the same process group."""

    worktree = tmp_path / "tree"
    worktree.mkdir()
    pid_file = worktree / "child.pid"
    code = (
        "import pathlib,subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "pathlib.Path('child.pid').write_text(str(p.pid)); time.sleep(60)"
    )
    with pytest.raises(BuildExecutionError, match="timed out") as raised:
        HostBuildRunner().run(
            worktree,
            _config((sys.executable, "-c", code), timeout=1),
        )
    assert raised.value.phase == "timeout"
    child_pid = int(pid_file.read_text())
    for _attempt in range(20):
        if not Path(f"/proc/{child_pid}").exists():
            break
        time.sleep(0.05)
    assert not Path(f"/proc/{child_pid}").exists()


def test_host_permission_warning_is_explicit() -> None:
    """Host execution summary states its filesystem/network trust boundary."""

    warning = HostBuildRunner.permission_warning
    assert "current user" in warning
    assert "filesystem" in warning
    assert "network" in warning
