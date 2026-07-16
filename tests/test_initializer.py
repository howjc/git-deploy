"""Non-secret single-repository configuration initialization tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from git_deploy.errors import ConfigError
from git_deploy.initializer import initialize_project


def test_init_suggests_detected_toolchains_without_server_or_secret(git_project: Path) -> None:
    """Init suggests local commands but leaves all target fields commented for editing."""

    (git_project / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (git_project / "composer.lock").write_text("{}\n", encoding="utf-8")

    path = initialize_project(git_project)
    content = path.read_text(encoding="utf-8")

    assert "pnpm install --frozen-lockfile" in content
    assert "composer install --no-dev" in content
    assert '# ssh_host_alias = "project-dev"' in content
    assert "password =" not in content
    assert "password_env" in content
    assert "[targets.dev]" not in [line for line in content.splitlines() if not line.startswith("#")]


def test_init_refuses_overwrite_and_non_git(tmp_path: Path, git_project: Path) -> None:
    """Init neither overwrites configuration nor scaffolds outside a Git worktree."""

    initialize_project(git_project)
    with pytest.raises(ConfigError, match="overwrite"):
        initialize_project(git_project)
    with pytest.raises(Exception, match="Git"):
        initialize_project(tmp_path / "not-git")
