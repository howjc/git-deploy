"""Focused doctor check tests using a mock transport contract."""

from __future__ import annotations

from pathlib import Path

from git_deploy.config import load_config
from git_deploy.doctor import run_doctor
from git_deploy.git import GitRepository
from git_deploy.manifest import StateStore
from tests.conftest import write_config
from tests.test_deployer import FakeTransport


class MissingRootTransport(FakeTransport):
    """Report a missing root and record any explicit creation."""

    def __init__(self) -> None:
        """Initialize the root as absent."""

        super().__init__()
        self.created = 0

    def root_exists(self) -> bool:
        """Return whether ensure_root has been explicitly called."""

        return bool(self.created)

    def ensure_root(self) -> None:
        """Record explicit root creation."""

        self.created += 1


def test_doctor_checks_remote_and_local_contract(git_project: Path) -> None:
    """A valid first-deployment project reports every lite diagnostic as healthy."""

    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    transport = FakeTransport()

    results = run_doctor(
        config,
        config.target(None),
        repository,
        StateStore(repository.git_dir()),
        transport_factory=lambda target: transport,
    )

    assert all(result.ok for result in results)
    assert {result.name for result in results} == {
        "config",
        "git",
        "build commands",
        "outputs",
        "state",
        "SSH backend",
        "target",
    }
    assert transport.connects == 1
    assert transport.closed == 1


def test_doctor_is_read_only_unless_create_root_is_explicit(git_project: Path) -> None:
    """Missing roots fail the default check and are created only on explicit request."""

    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    store = StateStore(repository.common_dir())
    transport = MissingRootTransport()

    readonly = run_doctor(
        config,
        config.target(None),
        repository,
        store,
        transport_factory=lambda target: transport,
    )
    assert not next(item for item in readonly if item.name == "target").ok
    assert transport.created == 0

    created = run_doctor(
        config,
        config.target(None),
        repository,
        store,
        create_root=True,
        transport_factory=lambda target: transport,
    )
    assert next(item for item in created if item.name == "target").ok
    assert transport.created == 1
