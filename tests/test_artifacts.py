"""Artifact collector safety and manifest tests."""

from __future__ import annotations

import hashlib
import os
import socket
from pathlib import Path

import pytest

from git_deploy.artifacts import ArtifactCollector
from git_deploy.errors import PolicyError
from git_deploy.models import ArtifactConfig


def test_collector_file_tree_hash_size_and_mode(tmp_path: Path) -> None:
    """File/tree mappings produce deterministic hash, size, mode, and owner fields."""

    worktree = tmp_path / "tree"
    (worktree / "dist/assets").mkdir(parents=True)
    (worktree / "dist/index.html").write_bytes(b"index")
    executable = worktree / "dist/assets/app"
    executable.write_bytes(b"binary")
    executable.chmod(0o755)
    (worktree / "server").write_bytes(b"server")

    manifest = ArtifactCollector().collect(
        worktree,
        (
            ArtifactConfig(source="dist", destination="public", kind="tree"),
            ArtifactConfig(source="server", destination="bin/server", kind="file"),
        ),
    )
    assert [item.destination for item in manifest.files] == [
        "bin/server",
        "public/assets/app",
        "public/index.html",
    ]
    by_destination = {item.destination: item for item in manifest.files}
    assert by_destination["public/index.html"].sha256 == hashlib.sha256(b"index").hexdigest()
    assert by_destination["public/index.html"].size == 5
    assert by_destination["public/assets/app"].executable is True
    assert by_destination["bin/server"].owner == "artifact:bin/server"


@pytest.mark.parametrize("source", ["/absolute", "../outside", "a/../../outside", "a//b"])
def test_collector_rejects_absolute_or_traversal_source(tmp_path: Path, source: str) -> None:
    """Defense in depth rejects unsafe mappings even if models were built manually."""

    with pytest.raises(PolicyError, match="relative|traversal|empty"):
        ArtifactCollector().collect(
            tmp_path,
            (ArtifactConfig(source=source, destination="out", kind="tree"),),
        )


def test_collector_rejects_symlink_and_submodule_metadata(tmp_path: Path) -> None:
    """Tree walks never follow links or collect nested Git worktree/submodule data."""

    worktree = tmp_path / "tree"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("secret")
    (worktree / "linked").parent.mkdir(parents=True)
    (worktree / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PolicyError, match="symlink"):
        ArtifactCollector().collect(
            worktree,
            (ArtifactConfig(source="linked", destination="out", kind="tree"),),
        )

    submodule = worktree / "submodule"
    submodule.mkdir()
    (submodule / ".git").write_text("gitdir: elsewhere")
    with pytest.raises(PolicyError, match="submodule|worktree"):
        ArtifactCollector().collect(
            worktree,
            (ArtifactConfig(source="submodule", destination="out", kind="tree"),),
        )


def test_collector_rejects_fifo_and_socket(tmp_path: Path) -> None:
    """FIFO/socket nodes are rejected before any attempt to read their bytes."""

    worktree = tmp_path / "tree"
    output = worktree / "output"
    output.mkdir(parents=True)
    fifo = output / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(PolicyError, match="non-regular"):
        ArtifactCollector().collect(
            worktree,
            (ArtifactConfig(source="output", destination="out", kind="tree"),),
        )
    fifo.unlink()

    socket_path = output / "socket"
    server = socket.socket(socket.AF_UNIX)
    try:
        server.bind(str(socket_path))
        with pytest.raises(PolicyError, match="non-regular"):
            ArtifactCollector().collect(
                worktree,
                (ArtifactConfig(source="output", destination="out", kind="tree"),),
            )
    finally:
        server.close()
