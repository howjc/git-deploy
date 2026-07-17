"""Real local FTP and containerized OpenSSH/SFTP contract tests."""

from __future__ import annotations

import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath

import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from git_deploy.config import TargetConfig, load_config, resolve_target_for_plan
from git_deploy.deployer import execute_plan
from git_deploy.git import GitRepository
from git_deploy.manifest import StateStore
from git_deploy.planner import create_plan
from git_deploy.transports.ftp import FTPTransport
from git_deploy.transports.sftp import SFTPTransport
from tests.conftest import commit_all, write_config


def _docker(*arguments: str) -> str:
    """Run one Docker fixture command and return stripped stdout."""

    return subprocess.run(
        ["docker", *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture(scope="module")
def sftp_server(tmp_path_factory: pytest.TempPathFactory):  # noqa: ANN201
    """Start isolated OpenSSH and return its container, port, and known-hosts file."""

    root = Path(__file__).parent / "fixtures/sftp"
    tag = f"git-deploy-v1-sftp:{secrets.token_hex(6)}"
    _docker("build", "-q", "-t", tag, str(root))
    container = _docker("run", "-d", "-P", tag)
    try:
        binding = _docker("port", container, "22/tcp").splitlines()[0]
        port = int(binding.rsplit(":", 1)[1])
        deadline = time.monotonic() + 15
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("SFTP fixture did not become ready") from None
                time.sleep(0.1)
        public_key = _docker(
            "exec", container, "cat", "/etc/ssh/ssh_host_ed25519_key.pub"
        ).split()
        known_hosts = tmp_path_factory.mktemp("sftp-known-hosts") / "known_hosts"
        known_hosts.write_text(
            f"[127.0.0.1]:{port} {public_key[0]} {public_key[1]}\n",
            encoding="utf-8",
        )
        yield container, port, known_hosts
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True)
        subprocess.run(["docker", "image", "rm", "-f", tag], check=False, capture_output=True)


@pytest.fixture
def ftp_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Start an authenticated writable local FTP server on a dynamic port."""

    root = tmp_path / "ftp-root"
    root.mkdir()
    authorizer = DummyAuthorizer()
    authorizer.add_user("deploy", "test-password", str(root), perm="elradfmwMT")
    handler = type("TestFTPHandler", (FTPHandler,), {"authorizer": authorizer})
    server = FTPServer(("127.0.0.1", 0), handler)
    port = server.socket.getsockname()[1]
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"timeout": 0.05, "blocking": True, "handle_exit": False},
        daemon=True,
    )
    thread.start()
    monkeypatch.setenv("TEST_FTP_PASSWORD", "test-password")
    try:
        yield root, port
    finally:
        server.close_all()
        thread.join(timeout=2)


def test_real_sftp_upload_replace_and_idempotent_delete(
    tmp_path: Path,
    sftp_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real SFTP verifies host identity, atomically replaces, and repeats deletion."""

    container, port, known_hosts = sftp_server
    monkeypatch.setenv("TEST_SFTP_PASSWORD", "test-only-password")
    target = TargetConfig(
        name="dev",
        protocol="sftp",
        host="127.0.0.1",
        username="deploy",
        remote_root=PurePosixPath("/srv/application"),
        port=port,
        port_explicit=True,
        password_env="TEST_SFTP_PASSWORD",
        known_hosts_file=known_hosts,
        use_ssh_agent=False,
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    script = tmp_path / "script.sh"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    script.write_bytes(b"#!/bin/sh\n")
    transport = SFTPTransport(target)
    try:
        transport.connect()
        transport.ensure_root()
        transport.upload(first, "nested/app.txt", lambda done, total: None)
        transport.upload(second, "nested/app.txt", lambda done, total: None)
        transport.upload(
            script,
            "nested/script.sh",
            lambda done, total: None,
            executable=True,
        )
        transport.run_command(
            "printf command-ok > command.txt",
            cwd=PurePosixPath("/srv/application"),
            timeout=5,
        )
        assert _docker(
            "exec", container, "cat", "/srv/application/nested/app.txt"
        ) == "two"
        assert _docker(
            "exec", container, "stat", "-c", "%a", "/srv/application/nested/script.sh"
        ) == "755"
        assert _docker(
            "exec", container, "cat", "/srv/application/command.txt"
        ) == "command-ok"
        transport.delete("nested/app.txt")
        transport.delete("nested/app.txt")
    finally:
        transport.close()


def test_real_ftp_upload_replace_and_idempotent_delete(
    tmp_path: Path,
    ftp_server,
) -> None:
    """Real FTP creates directories, transfers binary bytes, replaces, and deletes."""

    root, port = ftp_server
    target = TargetConfig(
        name="legacy",
        protocol="ftp",
        host="127.0.0.1",
        username="deploy",
        remote_root=PurePosixPath("/public_html"),
        port=port,
        password_env="TEST_FTP_PASSWORD",
    )
    local = tmp_path / "asset.bin"
    local.write_bytes(b"\x00first")
    transport = FTPTransport(target)
    try:
        transport.connect()
        transport.ensure_root()
        transport.upload(local, "assets/app.bin", lambda done, total: None)
        assert (root / "public_html/assets/app.bin").read_bytes() == b"\x00first"
        local.write_bytes(b"second")
        transport.upload(local, "assets/app.bin", lambda done, total: None)
        assert (root / "public_html/assets/app.bin").read_bytes() == b"second"
        transport.delete("assets/app.bin")
        transport.delete("assets/app.bin")
        assert not (root / "public_html/assets/app.bin").exists()
    finally:
        transport.close()


def _deploy_project(root: Path) -> int:
    """Execute one complete incremental deployment using the configured real transport.

    Args:
        root: Git project containing its target-specific ``deploy.toml``.

    Returns:
        Number of remote operations in the completed plan.
    """

    config = load_config(root / "deploy.toml")
    repository = GitRepository(root)
    store = StateStore(repository.common_dir())
    target = config.target(None)
    resolved_target = resolve_target_for_plan(target, runtime_dir=store.base)
    plan = create_plan(
        config,
        target,
        repository,
        store.load("dev"),
        full=False,
        resolved_target=resolved_target,
    )
    execute_plan(plan, config, repository, store)
    return len(plan.operations)


def test_complete_planner_deployer_state_cycle_over_real_sftp(
    git_project: Path,
    sftp_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The complete v1-lite pipeline performs first and incremental real SFTP syncs."""

    container, port, known_hosts = sftp_server
    monkeypatch.setenv("TEST_SFTP_PASSWORD", "test-only-password")
    write_config(
        git_project,
        f"""
[targets.dev]
protocol = "sftp"
host = "127.0.0.1"
port = {port}
username = "deploy"
password_env = "TEST_SFTP_PASSWORD"
known_hosts_file = "{known_hosts}"
use_ssh_agent = false
remote_root = "/srv/application/e2e"
after_deploy = ["echo paramiko-command-ok >> command.log"]
command_timeout = 5

[deploy]
retries = 2
retry_delay = 0
""",
    )

    assert _deploy_project(git_project) == 1
    assert _docker("exec", container, "cat", "/srv/application/e2e/app.py") == "print('v1')"
    assert _docker("exec", container, "wc", "-l", "/srv/application/e2e/command.log") == "1 /srv/application/e2e/command.log"

    (git_project / "app.py").write_text("print('v2')\n", encoding="utf-8")
    commit_all(git_project, "SFTP daily change")
    assert _deploy_project(git_project) == 1
    assert _docker("exec", container, "cat", "/srv/application/e2e/app.py") == "print('v2')"
    assert _docker("exec", container, "wc", "-l", "/srv/application/e2e/command.log") == "2 /srv/application/e2e/command.log"


def test_complete_planner_deployer_state_cycle_over_real_ftp(
    git_project: Path,
    ftp_server,
) -> None:
    """The complete v1-lite pipeline performs first and incremental real FTP syncs."""

    root, port = ftp_server
    write_config(
        git_project,
        f"""
[targets.dev]
protocol = "ftp"
host = "127.0.0.1"
port = {port}
username = "deploy"
password_env = "TEST_FTP_PASSWORD"
remote_root = "/e2e"

[deploy]
retries = 2
retry_delay = 0
""",
    )

    assert _deploy_project(git_project) == 1
    assert (root / "e2e/app.py").read_text(encoding="utf-8") == "print('v1')\n"

    (git_project / "app.py").write_text("print('v2')\n", encoding="utf-8")
    commit_all(git_project, "FTP daily change")
    assert _deploy_project(git_project) == 1
    assert (root / "e2e/app.py").read_text(encoding="utf-8") == "print('v2')\n"


def _install_native_test_key(container: str, root: Path) -> Path:
    """Generate a throwaway key and authorize it in the isolated OpenSSH fixture."""

    key = root / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
    )
    _docker("cp", str(key) + ".pub", f"{container}:/tmp/git-deploy-test-key.pub")
    _docker(
        "exec",
        container,
        "sh",
        "-c",
        "mkdir -p /home/deploy/.ssh && "
        "cat /tmp/git-deploy-test-key.pub >> /home/deploy/.ssh/authorized_keys && "
        "chown -R deploy:deploy /home/deploy/.ssh && "
        "chmod 700 /home/deploy/.ssh && chmod 600 /home/deploy/.ssh/authorized_keys",
    )
    return key


def test_complete_native_openssh_alias_and_proxyjump_cycle(
    git_project: Path,
    sftp_server,
    tmp_path: Path,
) -> None:
    """Native OpenSSH honors alias, key, non-default port, ControlMaster, and ProxyJump."""

    container, port, known_hosts = sftp_server
    key = _install_native_test_key(container, tmp_path)
    host_key = _docker(
        "exec", container, "cat", "/etc/ssh/ssh_host_ed25519_key.pub"
    ).split()
    with known_hosts.open("a", encoding="utf-8") as handle:
        handle.write(f"127.0.0.1 {host_key[0]} {host_key[1]}\n")
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text(
        f"""
Host fixture-gateway
    HostName 127.0.0.1
    Port {port}
    User deploy
    IdentityFile {key}
    IdentitiesOnly yes
    UserKnownHostsFile {known_hosts}
    StrictHostKeyChecking yes

Host fixture-proxy-target
    HostName 127.0.0.1
    Port 22
    User deploy
    IdentityFile {key}
    IdentitiesOnly yes
    UserKnownHostsFile {known_hosts}
    StrictHostKeyChecking yes
    ProxyJump fixture-gateway
""".strip()
        + "\n",
        encoding="utf-8",
    )
    write_config(
        git_project,
        f"""
[targets.dev]
protocol = "sftp"
ssh_host_alias = "fixture-proxy-target"
ssh_config_file = "{ssh_config}"
remote_root = "/srv/application/native-e2e"
after_deploy = ["echo native-command-ok >> command.log"]
command_timeout = 5

[deploy]
retries = 2
retry_delay = 0
""",
    )

    assert _deploy_project(git_project) == 1
    assert _docker(
        "exec", container, "cat", "/srv/application/native-e2e/app.py"
    ) == "print('v1')"
    assert _docker(
        "exec", container, "wc", "-l", "/srv/application/native-e2e/command.log"
    ) == "1 /srv/application/native-e2e/command.log"
    assert _deploy_project(git_project) == 0
    assert _docker(
        "exec", container, "wc", "-l", "/srv/application/native-e2e/command.log"
    ) == "1 /srv/application/native-e2e/command.log"
