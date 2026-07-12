"""SSH alias and exact agent-key selection tests."""

from __future__ import annotations

import base64
import hashlib
import subprocess
import sys
import types
from pathlib import Path

import pytest

from git_deploy.errors import ConfigurationError
from git_deploy.transport import _agent_key_for_public_file, resolve_server


def test_resolve_server_uses_effective_openssh_alias_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read host, user, port, identities, and known-hosts from ``ssh -G`` output."""

    config = tmp_path / "config"
    config.write_text("Host production\n", encoding="utf-8")

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return representative effective OpenSSH configuration.

        Args:
            args: Subprocess positional arguments, unused.
            kwargs: Subprocess keyword arguments, unused.

        Returns:
            Successful text subprocess result.
        """

        del args, kwargs
        return subprocess.CompletedProcess(
            ["ssh"],
            0,
            stdout=(
                "hostname 192.0.2.10\n"
                "user deploy\n"
                "port 2222\n"
                "identityfile ~/.ssh/production.pub\n"
                "userknownhostsfile ~/.ssh/known_hosts\n"
                "proxyjump none\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    resolved = resolve_server(
        {
            "protocol": "sftp",
            "ssh_host_alias": "production",
            "ssh_config_file": str(config),
        }
    )

    assert resolved["host"] == "192.0.2.10"
    assert resolved["username"] == "deploy"
    assert resolved["port"] == 2222
    assert resolved["key_file"] == ["~/.ssh/production.pub"]
    assert resolved["known_hosts_file"] == "~/.ssh/known_hosts"


def test_resolve_server_refuses_unsupported_proxy_jump(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed instead of silently bypassing an SSH jump host."""

    config = tmp_path / "config"
    config.write_text("Host production\n", encoding="utf-8")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            ["ssh"],
            0,
            stdout="hostname 192.0.2.10\nproxyjump bastion\n",
            stderr="",
        ),
    )

    with pytest.raises(ConfigurationError, match="unsupported proxyjump"):
        resolve_server(
            {
                "protocol": "sftp",
                "ssh_host_alias": "production",
                "ssh_config_file": str(config),
            }
        )


def test_public_identity_selects_only_matching_agent_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Match a public IdentityFile fingerprint to exactly one agent key."""

    public_blob = b"ssh-ed25519-test-public-blob"
    public_file = tmp_path / "production.pub"
    public_file.write_text(
        "ssh-ed25519 " + base64.b64encode(public_blob).decode("ascii") + " test\n",
        encoding="utf-8",
    )

    class FakeKey:
        """Expose a Paramiko-compatible legacy fingerprint."""

        def __init__(self, fingerprint: bytes):
            """Store the key fingerprint.

            Args:
                fingerprint: MD5 identity digest.
            """

            self.fingerprint = fingerprint

        def get_fingerprint(self) -> bytes:
            """Return the configured identity digest.

            Returns:
                Fingerprint bytes.
            """

            return self.fingerprint

    wrong = FakeKey(b"x" * 16)
    matching = FakeKey(hashlib.md5(public_blob).digest())  # noqa: S324 - identity comparison.

    class FakeAgent:
        """Provide deterministic keys and retain close state."""

        def __init__(self) -> None:
            """Initialize an open fake agent."""

            self.closed = False

        def get_keys(self) -> tuple[FakeKey, FakeKey]:
            """Return one wrong and one matching identity.

            Returns:
                Ordered fake agent keys.
            """

            return wrong, matching

        def close(self) -> None:
            """Record agent closure."""

            self.closed = True

    fake_module = types.SimpleNamespace(Agent=FakeAgent)
    monkeypatch.setitem(sys.modules, "paramiko", fake_module)

    key, agent = _agent_key_for_public_file(public_file)

    assert key is matching
    assert agent is not None
    assert not agent.closed


@pytest.mark.parametrize("protocol", ["sftp", "ftp", "ftps"])
def test_fake_transport_state_and_rollback_semantics_match(
    tmp_path: Path, protocol: str
) -> None:
    """All protocol facades share identical transaction/state/latest-rollback behavior."""

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import PlannedFile, ProjectConfig
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport, StateDeploymentExecutor
    from git_deploy.state_planner import SourceDiffPlan
    from git_deploy.state_rollback import StateRollbackService
    from git_deploy.target_identity import policy_fingerprint_for_project, resolve_target_identity

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "app.txt").write_bytes(b"old")
    subprocess.run(["git", "-C", str(repo), "add", "app.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "old"], check=True)
    tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    project = ProjectConfig("demo", repo, "/srv")
    identity = resolve_target_identity({"protocol": protocol, "host": "fake"}, project)
    root = tmp_path / "target"
    git_store = PersistentGitStore(root, repo)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    old_hash = hashlib.sha256(b"old").hexdigest()
    new_hash = hashlib.sha256(b"new").hexdigest()
    ContentAddressedStore(root).put(b"old")
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=tree,
        applied_transition_ids=("t0",),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(FileEntry("app.txt", "source", old_hash),),
    )
    store = ExpectedStateStore(root, identity)
    store.cas_advance(expected_generation=None, state=state)
    remote = InMemoryTransport()
    remote.files["/srv/app.txt"] = FakeRemotePath(b"old")
    operation = PlannedFile(
        "upload", "app.txt", "/srv/app.txt", "app.txt", old_hash, new_hash, 3
    )
    plan = SourceDiffPlan(tree, tree, (operation,), (), ("t1",), ("t0", "t1"))
    executor = StateDeploymentExecutor(
        project,
        identity,
        root,
        transport=remote,
        content_provider=lambda _path: b"new",
    )
    result = executor.deploy(plan, (operation,))
    assert result["status"] == "succeeded"
    assert remote.files["/srv/app.txt"].data == b"new"
    rolled = StateRollbackService(project, identity, root, transport=remote).rollback_latest()
    assert rolled.status == "succeeded"
    assert remote.files["/srv/app.txt"].data == b"old"
    current = store.load_current_state()
    assert current is not None and current[1].generation == 3
