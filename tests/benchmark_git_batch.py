"""Manual 10k-file benchmark for the FTP Hybrid Git content contract."""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

from git_deploy.git import GitRepository

FILE_COUNT = 10_000


def _git(root: Path, *arguments: str) -> None:
    """Run one repository setup command and require success.

    Args:
        root: Temporary benchmark repository.
        arguments: Git subcommand and arguments.

    Returns:
        ``None`` after the command succeeds.
    """

    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def main() -> int:
    """Create 10k blobs and report streaming batch-contract throughput.

    Returns:
        Zero after every blob receives a SHA256 and exact size.
    """

    temporary_base = Path.cwd() / "tmp"
    temporary_base.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="git-contract-benchmark-",
        dir=temporary_base,
    ) as directory:
        root = Path(directory)
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "benchmark@example.invalid")
        _git(root, "config", "user.name", "Benchmark")
        _git(root, "config", "gc.auto", "0")
        source = root / "files"
        source.mkdir()
        for index in range(FILE_COUNT):
            (source / f"file-{index:05d}.txt").write_text(
                f"content-{index}\n",
                encoding="utf-8",
            )
        _git(root, "add", "files")
        _git(root, "commit", "-qm", "benchmark fixtures")
        repository = GitRepository(root)
        entries = repository.list_head_entries()
        started = time.perf_counter()
        manifests = repository.blob_manifests(entries)
        elapsed = time.perf_counter() - started
        if len(manifests) != FILE_COUNT:
            raise RuntimeError(
                f"expected {FILE_COUNT} manifests, received {len(manifests)}"
            )
        print(
            f"{FILE_COUNT} files: {elapsed:.3f}s ({FILE_COUNT / elapsed:.0f} files/s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
