"""CLI safety and multi-project revision-selection tests."""

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
    _two_commit_repository(first, "one.txt")
    _two_commit_repository(second, "two.txt")
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
            "--revisions",
            "HEAD~1..HEAD",
            "--dry-run",
        ]
    )

    assert code == 0
    assert not (tmp_path / "state").exists()


def test_removed_from_and_to_options_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose the intentional CLI break instead of silently accepting old syntax."""

    repository = tmp_path / "repo"
    older, newer = _two_commit_repository(repository, "file.txt")
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

    with pytest.raises(SystemExit) as error:
        run(["plan", "demo", "--from", older, "--to", newer])

    assert error.value.code == 2


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
            "--revisions",
            f"{older}..{newer}",
            "--dry-run",
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert "UPLOAD file.txt" in output
    assert "uncommitted working-tree change(s) are ignored" in output
    assert "WORKTREE D file.txt (commit plan: UPLOAD)" in output


def test_cli_accepts_space_separated_non_contiguous_revisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pass multiple selectors through argparse into the composite planner."""

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "tests@example.invalid")
    _git(repository, "config", "user.name", "Tests")
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "base.txt")
    _git(repository, "commit", "-m", "base")
    (repository / "one.txt").write_text("one\n", encoding="utf-8")
    _git(repository, "add", "one.txt")
    _git(repository, "commit", "-m", "one")
    first = _git(repository, "rev-parse", "HEAD")
    (repository / "skipped.txt").write_text("skipped\n", encoding="utf-8")
    _git(repository, "add", "skipped.txt")
    _git(repository, "commit", "-m", "skipped")
    (repository / "three.txt").write_text("three\n", encoding="utf-8")
    _git(repository, "add", "three.txt")
    _git(repository, "commit", "-m", "three")
    third = _git(repository, "rev-parse", "HEAD")
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

    code = run(["plan", "demo", "--revisions", third, first])
    output = capsys.readouterr().out

    assert code == 0
    assert "UPLOAD one.txt" in output
    assert "UPLOAD three.txt" in output
    assert "skipped.txt" not in output


def test_cli_selects_a_named_remote_for_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Require and display the named remote used by a local deployment plan."""

    repository = tmp_path / "repo"
    older, newer = _two_commit_repository(repository, "file.txt")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[remotes.dev]
protocol = "sftp"
host = "dev.example.invalid"

[remotes.prod]
protocol = "sftp"
host = "prod.example.invalid"

[projects.demo]
repository = "{repository}"

[projects.demo.remotes.dev]
remote_root = "/srv/dev/demo"

[projects.demo.remotes.prod]
remote_root = "/srv/prod/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    missing_code = run(["plan", "demo", "--revisions", f"{older}..{newer}"])
    missing_output = capsys.readouterr()
    selected_code = run(
        ["plan", "demo", "--revisions", f"{older}..{newer}", "--remote", "dev"]
    )
    selected_output = capsys.readouterr().out

    assert missing_code == 4
    assert "--remote is required" in missing_output.err
    assert selected_code == 0
    assert "Remote: dev" in selected_output
    assert "UPLOAD file.txt" in selected_output
