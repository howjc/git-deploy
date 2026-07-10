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
