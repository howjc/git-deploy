"""Focused doctor check tests using a mock transport contract."""

from __future__ import annotations

from pathlib import Path

from git_deploy.config import load_config
from git_deploy.doctor import run_doctor
from git_deploy.git import GitRepository
from git_deploy.manifest import StateStore
from tests.conftest import write_config
from tests.test_deployer import FakeTransport


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
    assert {result.name for result in results} == {"config", "git", "build commands", "outputs", "state", "target"}
    assert transport.connects == 1
    assert transport.closed == 1
