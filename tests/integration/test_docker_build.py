"""Real local Docker daemon lifecycle, ownership, and secret-boundary gates."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from git_deploy.build_runner import BuildExecutionError
from git_deploy.docker_runner import DockerBuildRunner, DockerCli, DockerProcess
from git_deploy.models import BuildConfig, DockerBuildConfig, OnePasswordConfig
from git_deploy.onepassword_runner import OnePasswordDockerCli


@pytest.fixture(scope="module")
def docker_image(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Build a registry-free scratch image from a local static Go helper."""

    root = tmp_path_factory.mktemp("docker-helper")
    (root / "go.mod").write_text("module docker-helper\n\ngo 1.23\n", encoding="utf-8")
    (root / "main.go").write_text(
        "package main\n"
        "import (\"fmt\";\"os\";\"path/filepath\";\"time\")\n"
        "func main(){\n"
        " mode:=\"success\"; if len(os.Args)>1 { mode=os.Args[1] };\n"
        " switch mode {\n"
        " case \"success\": filepath.Walk(\"/workspace\",func(_ string,_ os.FileInfo,_ error)error{return nil}); "
        "os.MkdirAll(\"/workspace/dist\",0755); os.WriteFile(\"/workspace/dist/result.txt\",[]byte(\"docker-artifact\"),0755)\n"
        " case \"nonzero\": fmt.Fprintln(os.Stderr,\"failed\"); os.Exit(7)\n"
        " case \"sleep\": time.Sleep(60*time.Second)\n"
        " case \"secret\": fmt.Println(os.Getenv(\"SECRET\"))\n"
        " }\n} \n",
        encoding="utf-8",
    )
    subprocess.run(
        ["go", "build", "-o", "helper", "."],
        cwd=root,
        env={**os.environ, "CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "amd64"},
        check=True,
        capture_output=True,
    )
    (root / "Dockerfile").write_text(
        "FROM scratch\nCOPY helper /helper\n",
        encoding="utf-8",
    )
    tag = f"git-deploy-v02-integration:{uuid.uuid4().hex[:12]}"
    subprocess.run(
        ["docker", "build", "--network=none", "-t", tag, "."],
        cwd=root,
        check=True,
        capture_output=True,
    )
    yield tag
    subprocess.run(["docker", "image", "rm", "--force", tag], check=False, capture_output=True)


def _config(image: str, mode: str, *, timeout: int = 10) -> BuildConfig:
    """Return a locked-down Docker helper configuration."""

    return BuildConfig(
        runner="docker",
        commands=(("/helper", mode),),
        timeout=timeout,
        docker=DockerBuildConfig(image=image, network="none", pull_policy="never"),
    )


def _fixture_containers() -> set[str]:
    """Return all current git-deploy named Docker containers."""

    output = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=git-deploy-build-", "--format", "{{.Names}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {line for line in output.splitlines() if line}


def test_success_real_docker_artifact_hash_uid_and_cleanup(
    tmp_path: Path, docker_image: str
) -> None:
    """Real scratch container writes the expected executable artifact as host UID/GID."""

    before = _fixture_containers()
    worktree = tmp_path / "tree"
    worktree.mkdir()
    result = DockerBuildRunner().run(worktree, _config(docker_image, "success"))
    artifact = worktree / "dist/result.txt"
    info = artifact.stat()
    assert result.runner == "docker"
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == hashlib.sha256(
        b"docker-artifact"
    ).hexdigest()
    assert info.st_uid == os.getuid() and info.st_gid == os.getgid()
    assert info.st_mode & 0o111
    assert _fixture_containers() == before


def test_nonzero_real_docker_has_no_residue(tmp_path: Path, docker_image: str) -> None:
    """Real non-zero container returns a structured failure and is removed."""

    before = _fixture_containers()
    worktree = tmp_path / "tree"
    worktree.mkdir()
    with pytest.raises(BuildExecutionError) as raised:
        DockerBuildRunner().run(worktree, _config(docker_image, "nonzero"))
    assert raised.value.phase == "nonzero"
    assert raised.value.returncode == 7
    assert _fixture_containers() == before


def test_timeout_real_docker_stop_remove_has_no_residue(
    tmp_path: Path, docker_image: str
) -> None:
    """Real timeout stops and removes a sleeping scratch container."""

    before = _fixture_containers()
    worktree = tmp_path / "tree"
    worktree.mkdir()
    with pytest.raises(BuildExecutionError) as raised:
        DockerBuildRunner().run(worktree, _config(docker_image, "sleep", timeout=1))
    assert raised.value.phase == "timeout"
    assert _fixture_containers() == before


class InterruptOnceProcess:
    """Proxy a real docker process but raise Ctrl-C on its first wait."""

    def __init__(self, process: DockerProcess):
        """Store the real process."""

        self.process = process
        self.interrupted = False

    @property
    def returncode(self):
        """Expose underlying return code."""

        return self.process.returncode

    def communicate(self, input=None, timeout=None):
        """Raise once, then delegate cleanup waits."""

        if not self.interrupted:
            self.interrupted = True
            raise KeyboardInterrupt()
        return self.process.communicate(input=input, timeout=timeout)

    def poll(self):
        """Delegate status."""

        return self.process.poll()

    def terminate(self):
        """Delegate terminate."""

        self.process.terminate()

    def kill(self):
        """Delegate kill."""

        self.process.kill()


class InterruptOnceCli(DockerCli):
    """Start one real container process wrapped with a synthetic Ctrl-C."""

    def start(self, args, *, environment):
        """Wrap the real Docker run process."""

        return InterruptOnceProcess(super().start(args, environment=environment))


def test_interrupt_real_docker_cleanup_has_no_residue(
    tmp_path: Path, docker_image: str
) -> None:
    """Ctrl-C path coordinates cleanup against a real sleeping container."""

    before = _fixture_containers()
    worktree = tmp_path / "tree"
    worktree.mkdir()
    with pytest.raises(KeyboardInterrupt):
        DockerBuildRunner(cli=InterruptOnceCli()).run(
            worktree, _config(docker_image, "sleep")
        )
    assert _fixture_containers() == before


def test_cleanup_and_secret_boundary_live_metadata(
    tmp_path: Path, docker_image: str
) -> None:
    """Daemon can inspect live env; op output is masked and removed containers vanish."""

    name = f"git-deploy-secret-boundary-{uuid.uuid4().hex[:10]}"
    sentinel = "REAL-DOCKER-SECRET-SENTINEL"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--env",
            f"SECRET={sentinel}",
            docker_image,
            "/helper",
            "sleep",
        ],
        check=True,
        capture_output=True,
    )
    inspected = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Config.Env}}", name],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert sentinel in inspected
    subprocess.run(["docker", "rm", "--force", name], check=True, capture_output=True)
    assert subprocess.run(
        ["docker", "inspect", name], check=False, capture_output=True
    ).returncode != 0

    fake_op = tmp_path / "op"
    fake_op.write_text(
        f"#!{sys.executable}\n"
        "import os,subprocess,sys\n"
        "args=sys.argv[1:]; secret=os.environ['OP_FAKE_SECRET']; env=os.environ.copy()\n"
        "for k,v in list(env.items()):\n"
        "    if v.startswith('op://'): env[k]=secret\n"
        "r=subprocess.run(args[2:],env=env,capture_output=True)\n"
        "sys.stdout.buffer.write(r.stdout.replace(secret.encode(),b'***'))\n"
        "sys.stderr.buffer.write(r.stderr.replace(secret.encode(),b'***'))\n"
        "raise SystemExit(r.returncode)\n",
        encoding="utf-8",
    )
    fake_op.chmod(0o755)
    onepassword = OnePasswordConfig((("SECRET", "op://vault/item/secret"),))
    source = {
        "PATH": os.environ.get("PATH", ""),
        "OP_FAKE_SECRET": sentinel,
        "OP_SERVICE_ACCOUNT_TOKEN": "AUTH-SENTINEL",
    }
    cli = OnePasswordDockerCli(
        DockerCli(),
        onepassword,
        op_executable=str(fake_op),
        source_environment=source,
    )
    config = BuildConfig(
        runner="docker",
        commands=(("/helper", "secret"),),
        env_allowlist=("SECRET",),
        docker=DockerBuildConfig(docker_image, network="none", pull_policy="never"),
        onepassword=onepassword,
    )
    before = _fixture_containers()
    worktree = tmp_path / "tree"
    worktree.mkdir()
    result = DockerBuildRunner(cli=cli, source_environment=source).run(worktree, config)
    assert result.commands[0].stdout.strip() == "***"
    assert sentinel not in repr(result)
    assert "AUTH-SENTINEL" not in repr(result)
    assert _fixture_containers() == before
