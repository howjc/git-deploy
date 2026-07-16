"""Git HEAD, no-rename diff, and exact blob export tests."""

from __future__ import annotations

from pathlib import Path

from git_deploy.git import GitRepository
from tests.conftest import commit_all


def test_rename_is_delete_plus_add(git_project: Path) -> None:
    """Rename detection stays disabled so old paths cannot remain remotely."""

    repository = GitRepository(git_project)
    old = repository.head()
    (git_project / "app.py").rename(git_project / "main.py")
    new = commit_all(git_project, "rename")

    assert [(item.status, item.path) for item in repository.diff(old, new)] == [
        ("D", "app.py"),
        ("A", "main.py"),
    ]


def test_duplicate_content_is_plain_add(git_project: Path) -> None:
    """A copied blob is not inferred as a copy operation or baseline shortcut."""

    repository = GitRepository(git_project)
    old = repository.head()
    (git_project / "copy.py").write_bytes((git_project / "app.py").read_bytes())
    new = commit_all(git_project, "copy")

    assert [(item.status, item.path) for item in repository.diff(old, new)] == [("A", "copy.py")]


def test_export_uses_committed_head_not_dirty_worktree(git_project: Path, tmp_path: Path) -> None:
    """Uncommitted source bytes never leak into a deployment upload."""

    repository = GitRepository(git_project)
    (git_project / "app.py").write_text("print('dirty')\n", encoding="utf-8")
    destination = tmp_path / "staged.py"

    repository.export_file(repository.head(), "app.py", destination)

    assert destination.read_text(encoding="utf-8") == "print('v1')\n"
    assert repository.is_dirty()
