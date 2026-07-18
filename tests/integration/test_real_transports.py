"""Real local FTP and containerized OpenSSH/SFTP contract tests."""

from __future__ import annotations

import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from git_deploy.config import TargetConfig, load_config, resolve_target_for_plan
from git_deploy.deployer import execute_plan
from git_deploy.doctor import run_doctor
from git_deploy.errors import DeployError, PlanError, StaleRemotePlanError, StateError
from git_deploy.ftp_hybrid import (
    probe_ftp_hybrid_capabilities,
    save_capability_profile,
)
from git_deploy.git import GitRepository
from git_deploy.manifest import StateStore
from git_deploy.planner import create_plan
from git_deploy.prepared import (
    execute_prepared,
    execute_prepared_recovery,
    prepare_project,
    prepare_recovery,
    prepare_remote_plan,
    validate_prepared_freshness,
)
from git_deploy.transports.ftp import FTPTransport
from git_deploy.transports.openssh_sftp import OpenSSHSFTPTransport
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


def test_real_ftp_hybrid_capability_probe_supports_active_mode(
    tmp_path: Path,
    ftp_server,
) -> None:
    """The explicit binary/MLSD/rename probe also works with FTP active mode."""

    root, port = ftp_server
    (root / "active-root").mkdir()
    sibling = root / "active-root/.git-deploy/ftp-probe/older-probe"
    sibling.mkdir(parents=True)
    (sibling / "marker.bin").write_bytes(b"preserve")
    target = TargetConfig(
        "active",
        "ftp",
        "127.0.0.1",
        "deploy",
        PurePosixPath("/active-root"),
        port,
        password_env="TEST_FTP_PASSWORD",
        passive=False,
        runtime_dir=tmp_path,
    )
    transport = FTPTransport(target)
    try:
        transport.connect()
        profile = probe_ftp_hybrid_capabilities(transport, target, now=50)
        assert profile.rename_cross_directory
        assert profile.rename_replace_file
        assert profile.mlsd
    finally:
        transport.close()
    assert (sibling / "marker.bin").read_bytes() == b"preserve"
    assert len(tuple(sibling.parent.iterdir())) == 1


def test_real_ftp_hybrid_probe_adoption_mirror_state_loss_and_cleanup(
    git_project: Path,
    ftp_server,
) -> None:
    """pyftpdlib proves the FTP In-place Hybrid end-to-end safety contract."""

    root, port = ftp_server
    _create_hybrid_local_view(git_project)
    config_path = write_config(
        git_project,
        f'''
project_id = "github.com/acme/ftp-hybrid"

[[outputs]]
name = "frontend-root"
local = ".deploy/frontend-root"
remote = "."
mode = "hybrid"

[targets.dev]
protocol = "ftp"
host = "127.0.0.1"
port = {port}
username = "deploy"
password_env = "TEST_FTP_PASSWORD"
remote_root = "/public_html/ftp-hybrid"
''',
    )
    remote_root = root / "public_html/ftp-hybrid"
    (remote_root / "assets").mkdir(parents=True)
    (remote_root / "assets/legacy.js").write_text("legacy", encoding="utf-8")
    (remote_root / "index.php").write_text("backend", encoding="utf-8")
    (remote_root / ".env").write_text("secret", encoding="utf-8")
    (remote_root / "uploads").mkdir()
    (remote_root / "uploads/user.dat").write_text("user", encoding="utf-8")

    config = load_config(config_path)
    repository = GitRepository(git_project)
    store = StateStore(repository.common_dir())
    target = resolve_target_for_plan(config.target(None), runtime_dir=store.base)
    missing_profile = prepare_project(
        "ftp-hybrid", config_path, None, full=True, skip_build=True
    )
    try:
        with pytest.raises(PlanError, match="Capability Profile is missing"):
            prepare_remote_plan(missing_profile)
    finally:
        missing_profile.close()
    probe_transport = FTPTransport(target)
    try:
        probe_transport.connect()
        assert probe_transport.root_exists()
        profile = probe_ftp_hybrid_capabilities(probe_transport, target, now=100)
        save_capability_profile(store.base, profile)
    finally:
        probe_transport.close()
    assert not (remote_root / ".git-deploy/ftp-probe").exists()

    blocked = prepare_project("ftp-hybrid", config_path, None, full=False, skip_build=True)
    try:
        with pytest.raises(PlanError, match="--full to adopt"):
            prepare_remote_plan(blocked)
    finally:
        blocked.close()

    first = prepare_project("ftp-hybrid", config_path, None, full=True, skip_build=True)
    try:
        prepare_remote_plan(first)
        execute_prepared(first)
    finally:
        first.close()
    assert (remote_root / "assets/app.js").read_text(encoding="utf-8") == "hybrid app\n"
    assert not (remote_root / "assets/legacy.js").exists()
    assert (remote_root / "assets/empty/nested").is_dir()
    assert (remote_root / "index.php").read_text(encoding="utf-8") == "backend"
    assert (remote_root / ".env").read_text(encoding="utf-8") == "secret"
    assert (remote_root / "uploads/user.dat").read_text(encoding="utf-8") == "user"
    assert not (remote_root / ".git-deploy/ftp-hybrid/pending/frontend-root.json").exists()

    stale = prepare_project("ftp-hybrid", config_path, None, full=False, skip_build=True)
    try:
        prepare_remote_plan(stale)
        (remote_root / "assets/external.js").write_text("external", encoding="utf-8")
        with pytest.raises(StaleRemotePlanError, match="managed tree changed"):
            validate_prepared_freshness(stale)
        assert not (remote_root / ".git-deploy/ftp-hybrid/pending/frontend-root.json").exists()
        assert (remote_root / "assets/external.js").read_text(encoding="utf-8") == "external"
    finally:
        stale.close()
    (remote_root / "assets/external.js").unlink()

    local_app = git_project / ".deploy/frontend-root/assets/app.js"
    local_app.unlink()
    local_app.mkdir()
    nested_type_change = prepare_project(
        "ftp-hybrid", config_path, None, full=True, skip_build=True
    )
    try:
        with pytest.raises(PlanError, match="cannot safely change a Mirror path"):
            prepare_remote_plan(nested_type_change)
    finally:
        nested_type_change.close()
    local_app.rmdir()
    local_app.write_text("hybrid app\n", encoding="utf-8")

    (git_project / ".deploy/frontend-root/old-assets/old.js").unlink()
    (git_project / ".deploy/frontend-root/old-assets").rmdir()
    (git_project / ".deploy/frontend-root/old-assets").write_text("new type", encoding="utf-8")
    type_change = prepare_project("ftp-hybrid", config_path, None, full=True, skip_build=True)
    try:
        with pytest.raises(PlanError, match="cannot safely change an owned direct path"):
            prepare_remote_plan(type_change)
    finally:
        type_change.close()
    (git_project / ".deploy/frontend-root/old-assets").unlink()
    (git_project / ".deploy/frontend-root/assets/app.js").unlink()
    (git_project / ".deploy/frontend-root/assets/new.js").write_text("new\n", encoding="utf-8")
    store.path_for("dev").unlink()
    second = prepare_project("ftp-hybrid", config_path, None, full=False, skip_build=True)
    try:
        prepare_remote_plan(second)
        execute_prepared(second)
    finally:
        second.close()
    assert not (remote_root / "old-assets").exists()
    assert not (remote_root / "assets/app.js").exists()
    assert (remote_root / "assets/new.js").read_text(encoding="utf-8") == "new\n"
    assert (remote_root / "index.php").read_text(encoding="utf-8") == "backend"


def test_real_ftp_hybrid_forward_resume_at_publish_prune_state_and_cleanup(
    git_project: Path,
    ftp_server,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every durable FTP phase naturally resumes without Adoption or current-State drift."""

    root, port = ftp_server
    _create_hybrid_local_view(git_project)
    config_path = write_config(
        git_project,
        f'''
project_id = "github.com/acme/ftp-resume"

[[outputs]]
name = "frontend-root"
local = ".deploy/frontend-root"
remote = "."
mode = "hybrid"

[targets.dev]
protocol = "ftp"
host = "127.0.0.1"
port = {port}
username = "deploy"
password_env = "TEST_FTP_PASSWORD"
remote_root = "/public_html/ftp-resume"

[deploy]
retries = 3
retry_delay = 0
''',
    )
    remote_root = root / "public_html/ftp-resume"
    remote_root.mkdir(parents=True)
    config = load_config(config_path)
    repository = GitRepository(git_project)
    store = StateStore(repository.common_dir())
    target = resolve_target_for_plan(config.target(None), runtime_dir=store.base)
    probe_transport = FTPTransport(target)
    try:
        probe_transport.connect()
        save_capability_profile(
            store.base,
            probe_ftp_hybrid_capabilities(probe_transport, target, now=200),
        )
    finally:
        probe_transport.close()

    initial = prepare_project("ftp-resume", config_path, None, full=False, skip_build=True)
    try:
        prepare_remote_plan(initial)
        execute_prepared(initial)
    finally:
        initial.close()

    assets = git_project / ".deploy/frontend-root/assets"
    (assets / "prepared-write.js").write_text("prepared\n", encoding="utf-8")
    prepared_failure = prepare_project(
        "ftp-resume", config_path, None, full=False, skip_build=True
    )
    prepare_remote_plan(prepared_failure)
    assert isinstance(prepared_failure.transport, FTPTransport)
    original_rename = prepared_failure.transport.rename_replace

    def fail_initial_pending(source: str, destination: str) -> None:
        """Fail the first PREPARED marker publish after its Stage exists."""

        if destination == ".git-deploy/ftp-hybrid/pending/frontend-root.json":
            raise DeployError("injected initial Pending write failure")
        original_rename(source, destination)

    cast(Any, prepared_failure.transport).rename_replace = fail_initial_pending
    with pytest.raises(DeployError, match="initial Pending"):
        execute_prepared(prepared_failure)
    assert not (remote_root / ".git-deploy/ftp-hybrid/pending/frontend-root.json").exists()
    assert not (remote_root / ".git-deploy/ftp-hybrid/stage").exists()

    resumed = prepare_project("ftp-resume", config_path, None, full=False, skip_build=True)
    try:
        prepare_remote_plan(resumed)
        execute_prepared(resumed)
    finally:
        resumed.close()

    orphan = remote_root / ".git-deploy/ftp-hybrid/stage/orphan-stage"
    orphan.mkdir(parents=True)
    (orphan / "left.bin").write_bytes(b"left")
    (assets / "orphan-safe.js").write_text("orphan-safe\n", encoding="utf-8")
    with_orphan = prepare_project(
        "ftp-resume", config_path, None, full=False, skip_build=True
    )
    try:
        prepare_remote_plan(with_orphan)
        execute_prepared(with_orphan)
    finally:
        with_orphan.close()
    assert (orphan / "left.bin").read_bytes() == b"left"
    assert not (remote_root / ".git-deploy/ftp-hybrid/pending/frontend-root.json").exists()
    doctor = run_doctor(config, config.target(None), repository, store)
    orphan_result = next(item for item in doctor if item.name == "FTP Hybrid Orphan Stage")
    assert not orphan_result.ok
    assert "orphan-stage" in orphan_result.detail
    assert "entries=1" in orphan_result.detail

    (assets / "reset.js").write_text("reset\n", encoding="utf-8")
    reset = prepare_project("ftp-resume", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(reset)
    assert isinstance(reset.transport, FTPTransport)
    original_upload = reset.transport.upload
    reset_calls = 0

    def reset_once(local_path, remote_path, callback, *, executable=False) -> None:  # noqa: ANN001
        """Simulate one dropped connection, then allow the retry to converge."""

        nonlocal reset_calls
        if remote_path.endswith("/files/assets/reset.js"):
            reset_calls += 1
            if reset_calls == 1:
                raise DeployError("injected connection reset")
        original_upload(local_path, remote_path, callback, executable=executable)

    cast(Any, reset.transport).upload = reset_once
    execute_prepared(reset)
    assert reset_calls == 2
    assert (remote_root / "assets/reset.js").read_text(encoding="utf-8") == "reset\n"

    (assets / "interrupt.js").write_text("interrupt\n", encoding="utf-8")
    interrupted = prepare_project("ftp-resume", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(interrupted)
    assert isinstance(interrupted.transport, FTPTransport)
    original_upload = interrupted.transport.upload

    def interrupt_stage(local_path, remote_path, callback, *, executable=False) -> None:  # noqa: ANN001
        """Interrupt only one business Stage upload after PREPARED is durable."""

        if remote_path.endswith("/files/assets/interrupt.js"):
            raise KeyboardInterrupt
        original_upload(local_path, remote_path, callback, executable=executable)

    cast(Any, interrupted.transport).upload = interrupt_stage
    with pytest.raises(KeyboardInterrupt):
        execute_prepared(interrupted)
    pending_file = remote_root / ".git-deploy/ftp-hybrid/pending/frontend-root.json"
    assert '"phase":"PREPARED"' in pending_file.read_text(encoding="utf-8")

    # PREPARED is bound to both the prior State and non-Hybrid policy. Each
    # mismatch must fail during read-only planning and preserve all remote bytes.
    remote_snapshot = {
        path.relative_to(remote_root).as_posix(): path.read_bytes()
        for path in remote_root.rglob("*")
        if path.is_file()
    }
    state_path = store.path_for("dev")
    saved_state = state_path.read_bytes()
    state_path.unlink()
    missing_state = prepare_project(
        "ftp-resume", config_path, None, full=False, skip_build=True
    )
    try:
        with pytest.raises(PlanError, match="Pending non-Hybrid plan|previous State"):
            prepare_remote_plan(missing_state)
    finally:
        missing_state.close()
        state_path.write_bytes(saved_state)
    assert remote_snapshot == {
        path.relative_to(remote_root).as_posix(): path.read_bytes()
        for path in remote_root.rglob("*")
        if path.is_file()
    }

    original_config = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        original_config + '\n[source]\ninclude = ["**"]\nexclude = ["docs/**"]\n',
        encoding="utf-8",
    )
    drifted = prepare_project(
        "ftp-resume", config_path, None, full=False, skip_build=True
    )
    try:
        with pytest.raises(PlanError, match="Pending non-Hybrid plan"):
            prepare_remote_plan(drifted)
    finally:
        drifted.close()
        config_path.write_text(original_config, encoding="utf-8")
    assert remote_snapshot == {
        path.relative_to(remote_root).as_posix(): path.read_bytes()
        for path in remote_root.rglob("*")
        if path.is_file()
    }

    resumed = prepare_project("ftp-resume", config_path, None, full=False, skip_build=True)
    try:
        prepare_remote_plan(resumed)
        execute_prepared(resumed)
    finally:
        resumed.close()

    (assets / "verify.js").write_text("verify\n", encoding="utf-8")
    verify_failure = prepare_project("ftp-resume", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(verify_failure)
    assert isinstance(verify_failure.transport, FTPTransport)
    original_read = verify_failure.transport.read_file

    def corrupt_stage_read(path: str, *, max_bytes: int) -> bytes:
        """Return corrupt bytes only for the selected staged business file."""

        if path.endswith("/files/assets/verify.js"):
            return b"corrupt"
        return original_read(path, max_bytes=max_bytes)

    cast(Any, verify_failure.transport).read_file = corrupt_stage_read
    with pytest.raises(DeployError, match="verify.js failed"):
        execute_prepared(verify_failure)
    assert '"phase":"PREPARED"' in pending_file.read_text(encoding="utf-8")
    resumed = prepare_project("ftp-resume", config_path, None, full=False, skip_build=True)
    try:
        prepare_remote_plan(resumed)
        execute_prepared(resumed)
    finally:
        resumed.close()

    (assets / "publish.js").write_text("publish\n", encoding="utf-8")
    publish_failure = prepare_project(
        "ftp-resume", config_path, None, full=False, skip_build=True
    )
    prepare_remote_plan(publish_failure)
    assert isinstance(publish_failure.transport, FTPTransport)
    original_rename = publish_failure.transport.rename_replace

    def fail_business_publish(source: str, destination: str) -> None:
        """Fail only the final business rename after Pending and Stage writes."""

        if destination == "assets/publish.js":
            raise DeployError("injected publish failure")
        original_rename(source, destination)

    cast(Any, publish_failure.transport).rename_replace = fail_business_publish
    with pytest.raises(DeployError, match="publish.js failed"):
        execute_prepared(publish_failure)
    assert '"phase":"PREPARED"' in pending_file.read_text(encoding="utf-8")

    (assets / "changed-during-resume.js").write_text("changed\n", encoding="utf-8")
    changed = prepare_project("ftp-resume", config_path, None, full=False, skip_build=True)
    try:
        with pytest.raises(PlanError, match="Pending Manifest"):
            prepare_remote_plan(changed)
    finally:
        changed.close()
    (assets / "changed-during-resume.js").unlink()

    resumed = prepare_project("ftp-resume", config_path, None, full=False, skip_build=True)
    try:
        prepare_remote_plan(resumed)
        execute_prepared(resumed)
    finally:
        resumed.close()
    assert (remote_root / "assets/publish.js").read_text(encoding="utf-8") == "publish\n"

    (assets / "app.js").unlink()
    (assets / "prune.js").write_text("prune\n", encoding="utf-8")
    prune_failure = prepare_project("ftp-resume", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(prune_failure)
    assert isinstance(prune_failure.transport, FTPTransport)
    original_delete = prune_failure.transport.delete_typed

    def fail_orphan_delete(path: str) -> None:
        """Fail only one owned orphan after every current file is published."""

        if path == "assets/app.js":
            raise DeployError("injected prune failure")
        original_delete(path)

    cast(Any, prune_failure.transport).delete_typed = fail_orphan_delete
    with pytest.raises(DeployError, match="app.js failed"):
        execute_prepared(prune_failure)
    assert '"phase":"FILES_PUBLISHED"' in pending_file.read_text(encoding="utf-8")
    assert (remote_root / "assets/prune.js").exists()

    resumed = prepare_project("ftp-resume", config_path, None, full=False, skip_build=True)
    try:
        prepare_remote_plan(resumed)
        assert isinstance(resumed.transport, FTPTransport)

        def forbid_regular_replay(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
            """FILES_PUBLISHED recovery must not upload ordinary Source again."""

            raise AssertionError("FILES_PUBLISHED replayed an upload")

        cast(Any, resumed.transport).upload = forbid_regular_replay
        execute_prepared(resumed)
    finally:
        resumed.close()
    assert not (remote_root / "assets/app.js").exists()

    (assets / "empty/nested").rmdir()
    rmd_failure = prepare_project("ftp-resume", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(rmd_failure)
    assert isinstance(rmd_failure.transport, FTPTransport)
    original_rmd = rmd_failure.transport.remove_directory

    def fail_orphan_rmd(path: str) -> None:
        """Fail one orphan RMD after file publication and deletion complete."""

        if path == "assets/empty/nested":
            raise DeployError("injected RMD failure")
        original_rmd(path)

    cast(Any, rmd_failure.transport).remove_directory = fail_orphan_rmd
    with pytest.raises(DeployError, match="assets/empty/nested failed"):
        execute_prepared(rmd_failure)
    assert '"phase":"FILES_PUBLISHED"' in pending_file.read_text(encoding="utf-8")
    resumed = prepare_project("ftp-resume", config_path, None, full=False, skip_build=True)
    try:
        prepare_remote_plan(resumed)
        execute_prepared(resumed)
    finally:
        resumed.close()
    assert not (remote_root / "assets/empty/nested").exists()

    (assets / "ownership.js").write_text("ownership\n", encoding="utf-8")
    ownership_failure = prepare_project(
        "ftp-resume", config_path, None, full=False, skip_build=True
    )
    prepare_remote_plan(ownership_failure)
    assert isinstance(ownership_failure.transport, FTPTransport)
    original_rename = ownership_failure.transport.rename_replace

    def fail_ownership_publish(source: str, destination: str) -> None:
        """Fail the metadata rename only after current files and prune succeed."""

        if destination == ".git-deploy/hybrid/frontend-root.json":
            raise DeployError("injected Ownership failure")
        original_rename(source, destination)

    cast(Any, ownership_failure.transport).rename_replace = fail_ownership_publish
    with pytest.raises(DeployError, match="Ownership failure"):
        execute_prepared(ownership_failure)
    assert '"phase":"PRUNED"' in pending_file.read_text(encoding="utf-8")
    pruned_review = prepare_project(
        "ftp-resume", config_path, None, full=False, skip_build=True
    )
    prepare_remote_plan(pruned_review)
    (remote_root / "assets/pruned-external.js").write_text("external", encoding="utf-8")
    with pytest.raises(StaleRemotePlanError, match="managed tree changed"):
        execute_prepared(pruned_review)
    assert '"phase":"PRUNED"' in pending_file.read_text(encoding="utf-8")
    assert (remote_root / "assets/pruned-external.js").exists()
    (remote_root / "assets/pruned-external.js").unlink()
    resumed = prepare_project("ftp-resume", config_path, None, full=False, skip_build=True)
    try:
        prepare_remote_plan(resumed)
        execute_prepared(resumed)
    finally:
        resumed.close()

    (assets / "state.js").write_text("state\n", encoding="utf-8")
    state_failure = prepare_project("ftp-resume", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(state_failure)
    def fail_state(state) -> None:  # noqa: ANN001
        """Inject failure only after Ownership has committed."""

        raise StateError("injected state failure")

    cast(Any, state_failure.state_store).save = fail_state
    with pytest.raises(DeployError, match="injected state"):
        execute_prepared(state_failure)
    assert '"phase":"OWNERSHIP_COMMITTED"' in pending_file.read_text(encoding="utf-8")

    (git_project / "after-ownership.txt").write_text("new head\n", encoding="utf-8")
    commit_all(git_project, "continue after FTP Ownership commit")
    shutil.rmtree(git_project / ".deploy")
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write('\n[build]\nsteps = ["definitely-missing-build-command"]\n')
    broken_config = load_config(config_path)
    store.path_for("dev").write_text("not-json", encoding="utf-8")
    with pytest.raises(PlanError, match="--recover"):
        prepare_project(
            "ftp-resume",
            config_path,
            None,
            full=False,
            skip_build=False,
            check_post_commit_pending=True,
        )
    cleanup_failure = prepare_recovery("ftp-resume", broken_config, None)
    assert cleanup_failure is not None
    assert isinstance(cleanup_failure.transport, FTPTransport)
    original_remove_tree = cleanup_failure.transport.remove_tree

    def fail_stage_cleanup(path: str) -> None:
        """Leave only protected internal remnants after State recovery succeeds."""

        if path.startswith(".git-deploy/ftp-hybrid/stage/"):
            raise DeployError("injected cleanup failure")
        original_remove_tree(path)

    cast(Any, cleanup_failure.transport).remove_tree = fail_stage_cleanup
    execute_prepared_recovery(cleanup_failure)
    assert '"phase":"STATE_COMPLETE"' in pending_file.read_text(encoding="utf-8")
    assert "cleanup is pending" in capsys.readouterr().err
    frozen_commit = store.load("dev")
    assert frozen_commit is not None
    assert frozen_commit.last_commit != GitRepository(git_project).head()

    (git_project / "after-state.txt").write_text("newer head\n", encoding="utf-8")
    commit_all(git_project, "continue after FTP State commit")
    store.path_for("dev").unlink()
    cleaned = prepare_recovery("ftp-resume", broken_config, None)
    assert cleaned is not None
    try:
        execute_prepared_recovery(cleaned)
    finally:
        cleaned.close()
    assert not pending_file.exists()
    restored = store.load("dev")
    assert restored is not None
    assert restored.last_commit == frozen_commit.last_commit
    assert (remote_root / "assets/state.js").read_text(encoding="utf-8") == "state\n"


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


def _create_hybrid_local_view(root: Path) -> None:
    """Create one ignored aggregation root with files, mirrors, and an empty directory."""

    info_exclude = root / ".git/info/exclude"
    info_exclude.write_text(
        info_exclude.read_text(encoding="utf-8") + ".deploy/\n",
        encoding="utf-8",
    )
    hybrid = root / ".deploy/frontend-root"
    (hybrid / "assets").mkdir(parents=True)
    (hybrid / "assets/empty/nested").mkdir(parents=True)
    (hybrid / "old-assets").mkdir()
    (hybrid / "fonts").mkdir()
    (hybrid / "index.html").write_text("hybrid index\n", encoding="utf-8")
    (hybrid / "assets/app.js").write_text("hybrid app\n", encoding="utf-8")
    (hybrid / "old-assets/old.js").write_text("old bundle\n", encoding="utf-8")


def _deploy_hybrid(
    root: Path,
    *,
    full: bool = False,
    recover: bool = False,
) -> None:
    """Run the local-freeze and explicit deploy or Recovery pipeline."""

    if recover:
        prepared_recovery = prepare_recovery(
            "hybrid",
            load_config(root / "deploy.toml"),
            None,
        )
        assert prepared_recovery is not None
        execute_prepared_recovery(prepared_recovery)
        return
    prepared = prepare_project(
        "hybrid", root / "deploy.toml", None, full=full, skip_build=True
    )
    try:
        prepare_remote_plan(prepared)
        execute_prepared(prepared)
    finally:
        prepared.close()


def test_real_paramiko_hybrid_preserves_unknown_and_recovers_ownership_without_state(
    git_project: Path,
    sftp_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paramiko Hybrid mirrors directories and deletes only remotely owned direct children."""

    container, port, known_hosts = sftp_server
    monkeypatch.setenv("TEST_SFTP_PASSWORD", "test-only-password")
    _create_hybrid_local_view(git_project)
    write_config(
        git_project,
        f'''
project_id = "github.com/acme/paramiko-hybrid"

[[outputs]]
name = "frontend-root"
local = ".deploy/frontend-root"
remote = "."
mode = "hybrid"

[targets.dev]
protocol = "sftp"
host = "127.0.0.1"
port = {port}
username = "deploy"
password_env = "TEST_SFTP_PASSWORD"
known_hosts_file = "{known_hosts}"
use_ssh_agent = false
remote_root = "/srv/application/hybrid-paramiko"
''',
    )
    _docker(
        "exec",
        container,
        "sh",
        "-c",
        "mkdir -p /srv/application/hybrid-paramiko/manual-backup && "
        "mkdir -p /srv/application/hybrid-paramiko/assets && "
        "printf legacy > /srv/application/hybrid-paramiko/assets/legacy.js && "
        "printf backend > /srv/application/hybrid-paramiko/index.php && "
        "printf secret > /srv/application/hybrid-paramiko/.env && "
        "printf unknown > /srv/application/hybrid-paramiko/manual-backup/item && "
        "chown -R deploy:deploy /srv/application/hybrid-paramiko",
    )

    blocked = prepare_project(
        "hybrid", git_project / "deploy.toml", None, full=False, skip_build=True
    )
    try:
        with pytest.raises(PlanError, match="--full to adopt"):
            prepare_remote_plan(blocked)
    finally:
        blocked.close()
    assert _docker(
        "exec", container, "cat", "/srv/application/hybrid-paramiko/assets/legacy.js"
    ) == "legacy"

    _deploy_hybrid(git_project, full=True)
    assert _docker(
        "exec", container, "cat", "/srv/application/hybrid-paramiko/assets/app.js"
    ) == "hybrid app"
    assert _docker(
        "exec",
        container,
        "test",
        "!",
        "-e",
        "/srv/application/hybrid-paramiko/assets/legacy.js",
    ) == ""
    assert _docker(
        "exec", container, "test", "-d", "/srv/application/hybrid-paramiko/fonts"
    ) == ""
    assert _docker(
        "exec",
        container,
        "test",
        "-d",
        "/srv/application/hybrid-paramiko/assets/empty/nested",
    ) == ""
    assert _docker(
        "exec",
        container,
        "stat",
        "-c",
        "%a",
        "/srv/application/hybrid-paramiko/.git-deploy",
    ) == "700"

    race = git_project / ".deploy/frontend-root/race.txt"
    race.write_text("planned", encoding="utf-8")
    stale = prepare_project(
        "hybrid", git_project / "deploy.toml", None, full=False, skip_build=True
    )
    try:
        prepare_remote_plan(stale)
        _docker(
            "exec",
            container,
            "sh",
            "-c",
            "printf important > /srv/application/hybrid-paramiko/race.txt && "
            "chown deploy:deploy /srv/application/hybrid-paramiko/race.txt",
        )
        with pytest.raises(StaleRemotePlanError, match="path type changed"):
            execute_prepared(stale)
    finally:
        stale.close()
    assert _docker(
        "exec", container, "cat", "/srv/application/hybrid-paramiko/race.txt"
    ) == "important"
    race.unlink()

    stage_race = git_project / ".deploy/frontend-root/stage-race.txt"
    stage_race.write_text("planned during stage", encoding="utf-8")
    staged = prepare_project(
        "hybrid", git_project / "deploy.toml", None, full=False, skip_build=True
    )
    try:
        prepare_remote_plan(staged)
        assert staged.transport is not None
        original_write = staged.transport.write_file_atomic
        raced_during_stage = False

        def create_after_staged_record(remote_path: str, data: bytes) -> None:
            """Create a real same-name file after Stage and before the second gate."""

            nonlocal raced_during_stage
            original_write(remote_path, data)
            if (
                not raced_during_stage
                and remote_path.startswith(".git-deploy/recovery/")
                and b'"phase": "STAGED"' in data
            ):
                raced_during_stage = True
                _docker(
                    "exec",
                    container,
                    "sh",
                    "-c",
                    "printf stage-important > "
                    "/srv/application/hybrid-paramiko/stage-race.txt && "
                    "chown deploy:deploy "
                    "/srv/application/hybrid-paramiko/stage-race.txt",
                )

        monkeypatch.setattr(
            staged.transport,
            "write_file_atomic",
            create_after_staged_record,
        )
        with pytest.raises(StaleRemotePlanError, match="path type changed"):
            execute_prepared(staged)
    finally:
        staged.close()
    assert _docker(
        "exec",
        container,
        "cat",
        "/srv/application/hybrid-paramiko/stage-race.txt",
    ) == "stage-important"
    stage_race.unlink()

    store = StateStore(GitRepository(git_project).common_dir())
    store.path_for("dev").unlink()
    _docker(
        "exec",
        container,
        "sh",
        "-c",
        "printf orphan > /srv/application/hybrid-paramiko/assets/orphan.js",
    )
    hybrid = git_project / ".deploy/frontend-root"
    shutil.rmtree(hybrid / "old-assets")
    (hybrid / "index.html").unlink()
    (hybrid / "index10.css").write_text("new css\n", encoding="utf-8")
    interrupted = prepare_project(
        "hybrid", git_project / "deploy.toml", None, full=False, skip_build=True
    )
    try:
        prepare_remote_plan(interrupted)
        assert interrupted.transport is not None
        original_rename = interrupted.transport.rename_path
        failed = False

        def fail_first_stage_publish(source: str, destination: str) -> None:
            """Interrupt the first real Stage publish after Backup is durable."""

            nonlocal failed
            if not failed and ".git-deploy/stage/" in source and destination == "assets":
                failed = True
                raise DeployError("synthetic real-transport swap interruption")
            original_rename(source, destination)

        monkeypatch.setattr(interrupted.transport, "rename_path", fail_first_stage_publish)
        with pytest.raises(DeployError, match="swap interruption"):
            execute_prepared(interrupted)
    finally:
        interrupted.close()
    assert _docker(
        "exec",
        container,
        "sh",
        "-c",
        "test -n \"$(find /srv/application/hybrid-paramiko/.git-deploy/recovery "
        "-type f -print -quit)\"",
    ) == ""

    _deploy_hybrid(git_project, recover=True)
    _deploy_hybrid(git_project)

    for unknown, content in (("index.php", "backend"), (".env", "secret"), ("manual-backup/item", "unknown")):
        assert _docker(
            "exec", container, "cat", f"/srv/application/hybrid-paramiko/{unknown}"
        ) == content
    assert _docker(
        "exec", container, "test", "!", "-e", "/srv/application/hybrid-paramiko/assets/orphan.js"
    ) == ""
    assert _docker(
        "exec", container, "test", "!", "-e", "/srv/application/hybrid-paramiko/old-assets"
    ) == ""
    assert _docker(
        "exec", container, "test", "!", "-e", "/srv/application/hybrid-paramiko/index.html"
    ) == ""
    assert _docker(
        "exec", container, "cat", "/srv/application/hybrid-paramiko/index10.css"
    ) == "new css"
    assert _docker(
        "exec", container, "cat", "/srv/application/hybrid-paramiko/race.txt"
    ) == "important"
    assert _docker(
        "exec", container, "cat", "/srv/application/hybrid-paramiko/stage-race.txt"
    ) == "stage-important"
    assert _docker(
        "exec",
        container,
        "sh",
        "-c",
        "test -z \"$(find /srv/application/hybrid-paramiko/.git-deploy/stage "
        "/srv/application/hybrid-paramiko/.git-deploy/backup "
        "/srv/application/hybrid-paramiko/.git-deploy/recovery "
        "-mindepth 1 -print -quit)\"",
    ) == ""


def test_real_native_openssh_hybrid_stage_swap_and_manifest_read(
    git_project: Path,
    sftp_server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native OpenSSH performs Hybrid Stage/Swap and rereads ownership through one master."""

    container, port, known_hosts = sftp_server
    key = _install_native_test_key(container, tmp_path)
    ssh_config = tmp_path / "hybrid_ssh_config"
    ssh_config.write_text(
        f'''
Host fixture-hybrid
    HostName 127.0.0.1
    Port {port}
    User deploy
    IdentityFile {key}
    IdentitiesOnly yes
    UserKnownHostsFile {known_hosts}
    StrictHostKeyChecking yes
'''.strip()
        + "\n",
        encoding="utf-8",
    )
    _create_hybrid_local_view(git_project)
    write_config(
        git_project,
        f'''
project_id = "github.com/acme/native-hybrid"

[[outputs]]
name = "frontend-root"
local = ".deploy/frontend-root"
remote = "."
mode = "hybrid"

[targets.dev]
protocol = "sftp"
ssh_host_alias = "fixture-hybrid"
ssh_config_file = "{ssh_config}"
remote_root = "/srv/application/hybrid-native"
''',
    )

    _deploy_hybrid(git_project)
    assert _docker(
        "exec", container, "cat", "/srv/application/hybrid-native/assets/app.js"
    ) == "hybrid app"
    assert _docker(
        "exec",
        container,
        "cat",
        "/srv/application/hybrid-native/.git-deploy/hybrid/frontend-root.json",
    ).startswith("{")
    assert _docker(
        "exec",
        container,
        "stat",
        "-c",
        "%a",
        "/srv/application/hybrid-native/.git-deploy",
    ) == "700"
    race = git_project / ".deploy/frontend-root/native-race"
    race.write_text("planned", encoding="utf-8")
    stale = prepare_project(
        "hybrid", git_project / "deploy.toml", None, full=False, skip_build=True
    )
    try:
        prepare_remote_plan(stale)
        _docker(
            "exec",
            container,
            "sh",
            "-c",
            "mkdir /srv/application/hybrid-native/native-race && "
            "printf important > /srv/application/hybrid-native/native-race/item && "
            "chown -R deploy:deploy /srv/application/hybrid-native/native-race",
        )
        with pytest.raises(StaleRemotePlanError, match="path type changed"):
            execute_prepared(stale)
    finally:
        stale.close()
    assert _docker(
        "exec",
        container,
        "cat",
        "/srv/application/hybrid-native/native-race/item",
    ) == "important"
    race.unlink()

    last_moment = git_project / ".deploy/frontend-root/native-last-moment.txt"
    last_moment.write_text("planned", encoding="utf-8")
    ownership_path = (
        "/srv/application/hybrid-native/.git-deploy/hybrid/frontend-root.json"
    )
    old_ownership = _docker("exec", container, "cat", ownership_path)
    state_store = StateStore(GitRepository(git_project).common_dir())
    old_state = state_store.path_for("dev").read_bytes()
    raced = prepare_project(
        "hybrid", git_project / "deploy.toml", None, full=False, skip_build=True
    )
    try:
        prepare_remote_plan(raced)
        assert isinstance(raced.transport, OpenSSHSFTPTransport)
        master = raced.transport.master
        assert master is not None
        original_batch = master.run_batch
        injected = False

        def create_before_legacy_rename(
            commands: tuple[str, ...],
            *,
            operation: str,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            """Create the real destination after lstat but before legacy rename."""

            nonlocal injected
            command = commands[0]
            if (
                not injected
                and command.startswith("rename -l ")
                and ".git-deploy/stage/" in command
                and command.endswith(
                    ' "/srv/application/hybrid-native/native-last-moment.txt"'
                )
            ):
                injected = True
                _docker(
                    "exec",
                    container,
                    "sh",
                    "-c",
                    "printf last-moment-important > "
                    "/srv/application/hybrid-native/native-last-moment.txt && "
                    "chown deploy:deploy "
                    "/srv/application/hybrid-native/native-last-moment.txt",
                )
            return original_batch(commands, operation=operation, check=check)

        monkeypatch.setattr(master, "run_batch", create_before_legacy_rename)
        with pytest.raises(
            StaleRemotePlanError,
            match="appeared during no-overwrite Hybrid publish",
        ):
            execute_prepared(raced)
        assert injected
    finally:
        raced.close()
    assert _docker(
        "exec",
        container,
        "cat",
        "/srv/application/hybrid-native/native-last-moment.txt",
    ) == "last-moment-important"
    assert _docker("exec", container, "cat", ownership_path) == old_ownership
    assert state_store.path_for("dev").read_bytes() == old_state
    assert _docker(
        "exec",
        container,
        "sh",
        "-c",
        "test -n \"$(find /srv/application/hybrid-native/.git-deploy/stage "
        "-mindepth 1 -print -quit)\" && "
        "test -n \"$(find /srv/application/hybrid-native/.git-deploy/recovery "
        "-mindepth 1 -print -quit)\"",
    ) == ""

    _deploy_hybrid(git_project, recover=True)
    assert _docker(
        "exec",
        container,
        "cat",
        "/srv/application/hybrid-native/native-last-moment.txt",
    ) == "last-moment-important"
    assert _docker("exec", container, "cat", ownership_path) == old_ownership
    assert state_store.path_for("dev").read_bytes() == old_state
    assert _docker(
        "exec",
        container,
        "sh",
        "-c",
        "test -z \"$(find /srv/application/hybrid-native/.git-deploy/stage "
        "/srv/application/hybrid-native/.git-deploy/backup "
        "/srv/application/hybrid-native/.git-deploy/recovery "
        "-mindepth 1 -print -quit)\"",
    ) == ""
    last_moment.unlink()

    (git_project / ".deploy/frontend-root/assets/app.js").write_text(
        "native next\n", encoding="utf-8"
    )
    _deploy_hybrid(git_project)
    assert _docker(
        "exec", container, "cat", "/srv/application/hybrid-native/assets/app.js"
    ) == "native next"
    assert _docker(
        "exec",
        container,
        "cat",
        "/srv/application/hybrid-native/native-race/item",
    ) == "important"
    assert _docker(
        "exec",
        container,
        "cat",
        "/srv/application/hybrid-native/native-last-moment.txt",
    ) == "last-moment-important"
    assert _docker(
        "exec",
        container,
        "sh",
        "-c",
        "test -z \"$(find /srv/application/hybrid-native/.git-deploy/stage "
        "/srv/application/hybrid-native/.git-deploy/backup "
        "/srv/application/hybrid-native/.git-deploy/recovery "
        "-mindepth 1 -print -quit)\"",
    ) == ""
