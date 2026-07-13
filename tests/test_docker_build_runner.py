"""Restricted Docker command, image, and lifecycle tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_deploy.build_runner import BuildExecutionError
from git_deploy.docker_runner import DockerBuildRunner, DockerCli
from git_deploy.models import BuildConfig, DockerBuildConfig


class FakeProcess:
    """Controllable Docker run process."""

    def __init__(self, behavior: str, cli: "FakeDockerCli"):
        """Store behavior and parent fake CLI."""

        self.behavior = behavior
        self.cli = cli
        self.returncode: int | None = None
        self.calls = 0

    def communicate(
        self,
        input: bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        """Return success/nonzero output or model a stuck container."""

        del input, timeout
        self.calls += 1
        if self.behavior == "interrupt" and self.calls == 1:
            raise KeyboardInterrupt()
        if self.behavior in {"timeout", "interrupt"} and not self.cli.killed:
            raise subprocess.TimeoutExpired("docker run", 1)
        if self.behavior == "nonzero":
            self.returncode = 9
            return b"", b"failed"
        self.returncode = 0 if self.returncode is None else self.returncode
        return b"ok", b""

    def poll(self) -> int | None:
        """Return current fake status."""

        return self.returncode

    def terminate(self) -> None:
        """Mark the local client terminated."""

        self.returncode = -15

    def kill(self) -> None:
        """Mark the local client killed."""

        self.returncode = -9


class FakeDockerCli(DockerCli):
    """Record all Docker argv and provide deterministic image/lifecycle responses."""

    def __init__(
        self,
        *,
        image_present: bool = True,
        behavior: str = "success",
        remove_fails: bool = False,
    ):
        """Configure fake image and run behavior."""

        self.calls: list[list[str]] = []
        self.image_present = image_present
        self.behavior = behavior
        self.remove_fails = remove_fails
        self.pulled = False
        self.killed = False
        self.process: FakeProcess | None = None

    def run(self, args, *, environment):
        """Record control commands and return a completed result."""

        del environment
        argv = list(args)
        self.calls.append(argv)
        if argv[:2] == ["image", "inspect"]:
            present = self.image_present or self.pulled
            return subprocess.CompletedProcess(argv, 0 if present else 1, b"sha256:immutable\n" if present else b"", b"")
        if argv[0] == "pull":
            self.pulled = True
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if argv[0] == "kill":
            self.killed = True
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if argv[0] == "rm" and self.remove_fails:
            return subprocess.CompletedProcess(argv, 1, b"", b"remove failed")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def start(self, args, *, environment):
        """Record run argv/environment and return a fake process."""

        self.calls.append(list(args))
        self.run_environment = dict(environment)
        self.process = FakeProcess(self.behavior, self)
        return self.process


def _config(
    *,
    pull_policy: str = "never",
    network: str = "none",
    env: tuple[str, ...] = (),
) -> BuildConfig:
    """Return one Docker build configuration."""

    return BuildConfig(
        runner="docker",
        commands=(("tool", "build"),),
        env_allowlist=env,
        docker=DockerBuildConfig(
            image="tool:latest",
            platform="linux/amd64",
            network=network,
            pull_policy=pull_policy,
        ),
    )


def test_image_resolution_never_and_missing_pull_policy() -> None:
    """Image lookup resolves immutable ID; only missing policy may pull."""

    missing = FakeDockerCli(image_present=False)
    with pytest.raises(BuildExecutionError, match="pull_policy=never"):
        DockerBuildRunner(cli=missing).resolve_image(_config())
    assert not any(call[0] == "pull" for call in missing.calls)

    pull = FakeDockerCli(image_present=False)
    identity = DockerBuildRunner(cli=pull).resolve_image(_config(pull_policy="missing"))
    assert identity.image_id == "sha256:immutable"
    assert [call[0] for call in pull.calls] == ["image", "pull", "image"]


def test_command_has_single_mount_uid_policy_and_no_escape_paths(tmp_path: Path) -> None:
    """Docker run argv contains only fixed worktree/security/env-name controls."""

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    fake = FakeDockerCli()
    runner = DockerBuildRunner(
        cli=fake,
        source_environment={"PATH": "/bin", "TOKEN": "secret", "SSH_AUTH_SOCK": "/ssh"},
    )
    runner.run(worktree, _config(network="bridge", env=("TOKEN",)))
    run_argv = next(call for call in fake.calls if call[0] == "run")
    assert run_argv.count("--mount") == 1
    mount = run_argv[run_argv.index("--mount") + 1]
    assert f"src={worktree.resolve()},dst=/workspace" in mount
    assert "--user" in run_argv
    assert "--security-opt" in run_argv and "no-new-privileges" in run_argv
    assert "--cap-drop" in run_argv and "ALL" in run_argv
    assert run_argv[run_argv.index("--network") + 1] == "bridge"
    assert run_argv[run_argv.index("--platform") + 1] == "linux/amd64"
    assert "sha256:immutable" in run_argv
    assert "secret" not in run_argv
    assert "/ssh" not in repr(run_argv)
    assert all("docker.sock" not in item for item in run_argv)


@pytest.mark.parametrize("behavior", ["success", "nonzero"])
def test_lifecycle_success_or_nonzero_always_removes(
    tmp_path: Path, behavior: str
) -> None:
    """Success and non-zero paths never leave their named container."""

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    fake = FakeDockerCli(behavior=behavior)
    runner = DockerBuildRunner(cli=fake)
    if behavior == "nonzero":
        with pytest.raises(BuildExecutionError) as raised:
            runner.run(worktree, _config())
        assert raised.value.phase == "nonzero"
    else:
        assert runner.run(worktree, _config()).runner == "docker"
    assert any(call[0] == "rm" for call in fake.calls)


def test_timeout_stop_kill_remove_order(tmp_path: Path) -> None:
    """A stuck run follows stop→kill→remove without host fallback."""

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    fake = FakeDockerCli(behavior="timeout")
    with pytest.raises(BuildExecutionError, match="timed out"):
        DockerBuildRunner(cli=fake).run(worktree, _config())
    controls = [call[0] for call in fake.calls if call[0] in {"stop", "kill", "rm"}]
    assert controls == ["stop", "kill", "rm"]


def test_interrupt_stop_kill_remove_order(tmp_path: Path) -> None:
    """Ctrl-C coordinates container cleanup before propagating cancellation."""

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    fake = FakeDockerCli(behavior="interrupt")
    with pytest.raises(KeyboardInterrupt):
        DockerBuildRunner(cli=fake).run(worktree, _config())
    controls = [call[0] for call in fake.calls if call[0] in {"stop", "kill", "rm"}]
    assert controls == ["stop", "kill", "rm"]


def test_ownership_env_and_cleanup_failure_gate(tmp_path: Path) -> None:
    """UID/GID are fixed; cleanup failure exposes only a safe container name."""

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    fake = FakeDockerCli(remove_fails=True)
    runner = DockerBuildRunner(cli=fake, source_environment={"PATH": "/bin", "TOKEN": "sentinel"})
    with pytest.raises(BuildExecutionError) as raised:
        runner.run(worktree, _config(env=("TOKEN",)))
    assert raised.value.phase == "cleanup"
    assert "sentinel" not in str(raised.value)
    run_argv = next(call for call in fake.calls if call[0] == "run")
    uid_gid = run_argv[run_argv.index("--user") + 1]
    assert uid_gid == f"{__import__('os').getuid()}:{__import__('os').getgid()}"
    assert "TOKEN" in run_argv
    assert "sentinel" not in run_argv
