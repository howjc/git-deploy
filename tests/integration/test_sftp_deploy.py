"""Real OpenSSH/SFTP deployment, drift, rollback, and permission integration."""

from __future__ import annotations

import hashlib
import secrets
import socket
import subprocess
import time
from pathlib import Path

import pytest

from git_deploy.errors import GitDeployError, RemoteDriftError
from git_deploy.expected_state import ExpectedStateStore, build_expected_state
from git_deploy.git_store import PersistentGitStore
from git_deploy.gitrepo import GitRepository
from git_deploy.models import PlannedFile, ProjectConfig
from git_deploy.state_executor import StateDeploymentExecutor
from git_deploy.state_planner import SourceDiffPlan
from git_deploy.state_rollback import StateRollbackService
from git_deploy.target_identity import policy_fingerprint_for_project, resolve_target_identity
from git_deploy.transport import SftpTransport


def _docker(*args: str) -> str:
    """Run one Docker fixture command and return stripped stdout."""

    return subprocess.run(
        ["docker", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture(scope="module")
def sftp_server(tmp_path_factory: pytest.TempPathFactory):
    """Build and start an isolated OpenSSH server with a pinned dynamic host key."""

    root = Path(__file__).parent / "fixtures" / "sftp"
    tag = f"git-deploy-sftp-test:{secrets.token_hex(6)}"
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
            "exec",
            container,
            "cat",
            "/etc/ssh/ssh_host_ed25519_key.pub",
        ).split()
        known_hosts = tmp_path_factory.mktemp("sftp-host-key") / "known_hosts"
        known_hosts.write_text(
            f"[127.0.0.1]:{port} {public_key[0]} {public_key[1]}\n",
            encoding="utf-8",
        )
        yield container, {
            "protocol": "sftp",
            "host": "127.0.0.1",
            "port": port,
            "username": "deploy",
            "password": "test-only-password",
            "known_hosts_file": str(known_hosts),
            "strict_host_key_checking": True,
            "use_ssh_agent": False,
            "owner": "deploy",
            "group": "www-data",
            "file_mode": "0640",
            "executable_mode": "0750",
            "directory_mode": "0750",
        }
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container],
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["docker", "image", "rm", "-f", tag],
            check=False,
            capture_output=True,
        )


def _git(repository: Path, *args: str) -> str:
    """Run one Git fixture command and return stripped stdout."""

    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _file(
    path: str,
    before: bytes | None,
    after: bytes | None,
    *,
    executable: bool = False,
) -> PlannedFile:
    """Build one exact real-SFTP file mutation."""

    action = "delete" if after is None else "upload"
    return PlannedFile(
        action=action,
        path=path,
        remote_path=f"/srv/application/{path}",
        source_path=path if after is not None else None,
        expected_before_sha256=(
            hashlib.sha256(before).hexdigest() if before is not None else None
        ),
        target_sha256=(
            hashlib.sha256(after).hexdigest() if after is not None else None
        ),
        target_size=len(after or b""),
        executable=executable,
        expected_before_executable=(executable if before is not None else None),
    )


def _plan(before_tree: str, after_tree: str, transition: str) -> SourceDiffPlan:
    """Build a minimal stateful source plan for one real transaction."""

    return SourceDiffPlan(
        before_tree_id=before_tree,
        after_tree_id=after_tree,
        files=(),
        excluded=(),
        introduced_transition_ids=(transition,),
        applied_transition_ids=(transition,),
        revision_specs=(transition,),
    )


def test_real_sftp_incremental_drift_latest_rollback_and_permissions(
    tmp_path: Path,
    sftp_server,
) -> None:
    """Exercise real atomic SFTP writes, modes, drift, rollback, and denied publish."""

    container, server = sftp_server
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "sftp@example.invalid")
    _git(repository, "config", "user.name", "SFTP")
    script_v1 = b"#!/bin/sh\necho one\n"
    removed = b"remove me\n"
    (repository / "script.sh").write_bytes(script_v1)
    (repository / "remove.txt").write_bytes(removed)
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "first")
    commit1 = _git(repository, "rev-parse", "HEAD")
    tree1 = _git(repository, "rev-parse", "HEAD^{tree}")
    script_v2 = b"#!/bin/sh\necho two\n"
    added = b"added\n"
    (repository / "script.sh").write_bytes(script_v2)
    (repository / "remove.txt").unlink()
    (repository / "add.txt").write_bytes(added)
    _git(repository, "add", "-A")
    _git(repository, "commit", "-qm", "second")
    commit2 = _git(repository, "rev-parse", "HEAD")
    tree2 = _git(repository, "rev-parse", "HEAD^{tree}")

    project = ProjectConfig(
        name="application",
        repository=repository,
        remote_root="/srv/application",
        local_state_dir=tmp_path / "state",
    )
    identity = resolve_target_identity(server, project)
    target_root = tmp_path / "state" / "targets" / identity.target_id
    git_store = PersistentGitStore(target_root, repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    empty_tree = GitRepository(repository).empty_tree()
    store = ExpectedStateStore(target_root, identity)
    store.cas_advance(
        expected_generation=None,
        state=build_expected_state(
            generation=1,
            parent_state_id=None,
            source_tree_id=empty_tree,
            applied_transition_ids=(),
            physical_fingerprint=identity.physical_fingerprint,
            policy_fingerprint=policy_fingerprint_for_project(project),
        ),
    )

    contents = {
        "script.sh": script_v1,
        "remove.txt": removed,
        "add.txt": added,
    }
    transport = SftpTransport(server)
    try:
        executor = StateDeploymentExecutor(
            project,
            identity,
            target_root,
            transport=transport,
            content_provider=lambda path: contents[path],
        )
        first = executor.deploy(
            _plan(empty_tree, tree1, commit1),
            [
                _file("script.sh", None, script_v1, executable=True),
                _file("remove.txt", None, removed),
            ],
        )
        if first["status"] != "succeeded":
            pytest.fail(str(first))
        assert transport.read_file("/srv/application/script.sh") == script_v1
        code, mode, _error = transport.execute(
            "stat -c '%U:%G %a' /srv/application/script.sh"
        )
        assert code == 0 and mode.strip() == "deploy:www-data 750"

        contents["script.sh"] = script_v2
        second = executor.deploy(
            _plan(tree1, tree2, commit2),
            [
                _file("script.sh", script_v1, script_v2, executable=True),
                _file("remove.txt", removed, None),
                _file("add.txt", None, added),
            ],
        )
        if second["status"] != "succeeded":
            pytest.fail(str(second))
        assert transport.read_file("/srv/application/script.sh") == script_v2
        assert transport.read_file("/srv/application/remove.txt") is None
        assert transport.read_file("/srv/application/add.txt") == added

        transport.execute("printf tampered > /srv/application/script.sh")
        with pytest.raises(RemoteDriftError):
            executor.deploy(
                _plan(tree2, tree2, "drift-check"),
                [_file("script.sh", script_v2, b"three", executable=True)],
            )
        transport.execute("printf '#!/bin/sh\\necho two\\n' > /srv/application/script.sh")

        rolled = StateRollbackService(
            project,
            identity,
            target_root,
            transport=transport,
        ).rollback_latest()
        assert rolled.status == "succeeded"
        assert transport.read_file("/srv/application/script.sh") == script_v1
        assert transport.read_file("/srv/application/remove.txt") == removed
        assert transport.read_file("/srv/application/add.txt") is None
        assert store.read_current().generation == 4  # type: ignore[union-attr]
        code, mode, _error = transport.execute(
            "stat -c '%U:%G %a' /srv/application/script.sh"
        )
        assert code == 0 and mode.strip() == "deploy:www-data 750"

        _docker("exec", container, "chmod", "0555", "/srv/application")
        with pytest.raises(GitDeployError):
            transport.replace_file("/srv/application/denied/new.txt", b"blocked")
        assert transport.read_file("/srv/application/denied/new.txt") is None
    finally:
        _docker("exec", container, "chmod", "0775", "/srv/application")
        transport.close()
