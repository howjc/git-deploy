"""Shared v1-lite test helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def git_project(tmp_path: Path) -> Path:
    """Create a minimal Git project with one committed source file."""

    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Tests")
    info_exclude = root / ".git/info/exclude"
    info_exclude.write_text("deploy.toml\ndist/\n", encoding="utf-8")
    (root / "app.py").write_text("print('v1')\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-qm", "initial")
    return root


def commit_all(root: Path, message: str = "change") -> str:
    """Commit every current test-project change and return full HEAD."""

    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD").strip()


def write_config(root: Path, body: str | None = None) -> Path:
    """Write a minimal SFTP v1-lite configuration and return its path."""

    content = body or """
default_target = "dev"

[source]
include = ["**"]

[build]
steps = []

[[outputs]]
local = "dist"
remote = "public/dist"
delete_removed = true

[targets.dev]
protocol = "sftp"
host = "example.invalid"
username = "deploy"
remote_root = "/srv/app"
strict_host_key_checking = true

[deploy]
retries = 2
retry_delay = 0
"""
    path = root / "deploy.toml"
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path


def _git(root: Path, *arguments: str) -> str:
    """Run one test Git command and return text stdout."""

    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout
