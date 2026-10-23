"""Windows client contracts, plus portable simulations on POSIX runners."""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from git_deploy.config import load_config
from git_deploy.deployer import freeze_uploads
from git_deploy.errors import DeployError, PlanError
from git_deploy.git import GitRepository
from git_deploy.manifest import StateStore, TargetState
from git_deploy.planner import create_plan, UploadOperation
from git_deploy.transports import openssh_sftp as native
from tests.conftest import write_config
from tests.test_openssh_sftp import native_target


def test_native_windows_uses_pinned_endpoint_without_controlmaster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows uses native executables, UTF-8 batches and fresh alias checks."""

    monkeypatch.setattr(native, "IS_WINDOWS", True)
    monkeypatch.setattr(native.shutil, "which", lambda name: str(tmp_path / f"{name}.exe"))
    calls = []
    host = "192.0.2.10"

    def run(command, **kwargs):
        """Capture native process arguments and return a configurable alias."""

        calls.append((command, kwargs))
        output = f"hostname {host}\nuser deploy\nport 22\n" if "-G" in command else ""
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(native.subprocess, "run", run)
    pool = native.SSHConnectionPool()
    master = pool.acquire(native_target(tmp_path))
    assert pool.acquire(native_target(tmp_path)) is master
    assert master.control_path is None and master.directory is None
    master.run_batch(('put "C:/构建 files/a.js" "/srv/app/a.js"',), operation="upload")
    master.run_capture("printf file")
    master.run_command("true", PurePosixPath("/srv/app"), 3)
    invocations = [(command, kwargs) for command, kwargs in calls if "-G" not in command]
    assert len(invocations) == 4
    for command, kwargs in invocations:
        assert "BatchMode=yes" in command
        assert "HostName=192.0.2.10" in command and "User=deploy" in command
        assert not any("Control" in argument or argument == "-MNf" for argument in command)
        if "input" in kwargs:
            assert kwargs["encoding"] == "utf-8"
            assert "C:/构建 files" in kwargs["input"]
            assert command[command.index("-S") + 1] == master.ssh
    host = "192.0.2.99"
    before = len(invocations)
    with pytest.raises(DeployError, match="stale target"):
        master.run_batch(('rm "/srv/app/a.js"',), operation="delete")
    pool.close_all()
    assert len([command for command, _ in calls if "-G" not in command]) == before


def test_native_windows_auth_failure_does_not_mark_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed Windows authentication never leaves a reusable connection."""

    monkeypatch.setattr(native, "IS_WINDOWS", True)
    monkeypatch.setattr(native.shutil, "which", lambda name: str(tmp_path / f"{name}.exe"))

    def run(command, **kwargs):
        """Resolve the expected endpoint, then refuse authentication."""

        if "-G" in command:
            return subprocess.CompletedProcess(command, 0, "hostname 192.0.2.10\nuser deploy\nport 22", "")
        return subprocess.CompletedProcess(command, 255, "", "Permission denied")

    monkeypatch.setattr(native.subprocess, "run", run)
    master = native.OpenSSHMaster(native_target(tmp_path))
    with pytest.raises(DeployError, match="Windows requires a key or SSH Agent"):
        master.connect()
    assert not master.connected


def test_freeze_remote_names_do_not_become_windows_local_paths(git_project: Path) -> None:
    """Device names, drives, deep paths and case-distinct names retain exact bytes."""

    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    plan = create_plan(config, config.target(None), repository, None, full=False)
    source = next(item for item in plan.operations if isinstance(item, UploadOperation))
    names = ("CON", "con.txt", "C:/remote.js", "A.js", "a.js", "目录/文件.js", "deep/" * 60 + "x")
    plan = replace(plan, operations=tuple(replace(source, remote_path=name) for name in names))
    staging = git_project / "staging"
    frozen = freeze_uploads(plan, repository, staging)
    assert len(set(frozen.values())) == len(names)
    for path in frozen.values():
        assert path.parent == staging
        assert path.read_bytes() == b"print('v1')\n"


def test_state_replace_is_portable_and_preserves_existing_state_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows and POSIX can replace State; failed publish keeps the prior bytes."""

    store = StateStore(tmp_path / ".git")
    first = TargetState(1, "prod", "sftp:host", "a" * 40, 1, {})
    store.save(first)
    second = replace(first, deployed_at=2)
    store.save(second)
    assert store.load("prod") == second

    def fail(*args):
        """Simulate Windows denying replacement of a busy destination."""

        raise PermissionError("destination busy")

    monkeypatch.setattr("git_deploy.manifest.os.replace", fail)
    from git_deploy.errors import StateError
    with pytest.raises(StateError, match="destination busy"):
        store.save(first)
    assert store.load("prod") == second
    assert not list(store.base.glob("*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction requires native Windows")
def test_hybrid_rejects_windows_directory_junction(tmp_path: Path) -> None:
    """A junction cannot pull unrelated files into a Hybrid mirror."""

    from git_deploy.config import OutputConfig
    from git_deploy.hybrid import scan_hybrid_output

    root = tmp_path / "output"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"private")
    junction = root / "escape"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True)
    try:
        with pytest.raises(PlanError, match="symlinks"):
            scan_hybrid_output(OutputConfig(root, PurePosixPath("."), name="web", mode="hybrid"))
    finally:
        junction.rmdir()
