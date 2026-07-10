"""CLI safety and multi-project range tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_deploy.cli import run


def _git(repository: Path, *args: str) -> str:
    """Run Git in a temporary CLI test repository.

    Args:
        repository: Temporary Git working tree.
        args: Git arguments.

    Returns:
        Stripped stdout.
    """

    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _two_commit_repository(path: Path, filename: str) -> tuple[str, str]:
    """Create a repository with one modified file and return its commit range.

    Args:
        path: Repository directory to create.
        filename: Tracked file name.

    Returns:
        Source and target commit IDs.
    """

    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "tests@example.invalid")
    _git(path, "config", "user.name", "Tests")
    (path / filename).write_text("one\n", encoding="utf-8")
    _git(path, "add", filename)
    _git(path, "commit", "-m", "one")
    older = _git(path, "rev-parse", "HEAD")
    (path / filename).write_text("two\n", encoding="utf-8")
    _git(path, "commit", "-am", "two")
    return older, _git(path, "rev-parse", "HEAD")


def test_deploy_all_dry_run_is_local_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Expand all ranges without attempting the intentionally invalid server connection."""

    first = tmp_path / "first"
    second = tmp_path / "second"
    first_range = _two_commit_repository(first, "one.txt")
    second_range = _two_commit_repository(second, "two.txt")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "invalid.example"

[projects.first]
repository = "{first}"
remote_root = "/srv/first"

[projects.second]
repository = "{second}"
remote_root = "/srv/second"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    code = run(
        [
            "deploy",
            "all",
            "--range",
            f"first={first_range[0]}..{first_range[1]}",
            "--range",
            f"second={second_range[0]}..{second_range[1]}",
            "--dry-run",
        ]
    )

    assert code == 0
    assert not (tmp_path / "state").exists()


def test_multiple_projects_reject_common_range(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require explicit project-to-range mapping for an ``all`` deployment."""

    repository = tmp_path / "repo"
    older, newer = _two_commit_repository(repository, "file.txt")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"

[projects.first]
repository = "{repository}"
remote_root = "/srv/first"

[projects.second]
repository = "{repository}"
remote_root = "/srv/second"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert run(["plan", "all", "--from", older, "--to", newer]) == 4


def test_dry_run_warns_when_worktree_deletion_is_still_present_in_target_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Expose that an uncommitted deletion cannot affect a commit-range upload."""

    repository = tmp_path / "repo"
    older, newer = _two_commit_repository(repository, "file.txt")
    (repository / "file.txt").unlink()
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    code = run(
        [
            "deploy",
            "demo",
            "--from",
            older,
            "--to",
            newer,
            "--dry-run",
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert "UPLOAD file.txt" in output
    assert "uncommitted working-tree change(s) are ignored" in output
    assert "WORKTREE D file.txt (commit plan: UPLOAD)" in output
