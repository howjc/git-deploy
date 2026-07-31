"""Source/output merge, safety, and incremental planner tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess

import pytest

from git_deploy.config import load_config
from git_deploy.errors import PlanError
from git_deploy.git import GitRepository
from git_deploy.manifest import ManifestEntry, TargetState
from git_deploy.planner import (
    DeleteOperation,
    UploadOperation,
    create_plan,
    deployment_contract_hash,
    render_plan,
)
from tests.conftest import commit_all, write_config


def test_first_plan_uploads_all_managed_source_and_outputs(git_project: Path) -> None:
    """Missing state automatically enters full mode without remote deletion."""

    dist = git_project / "dist"
    dist.mkdir()
    (dist / "asset.js").write_text("one", encoding="utf-8")
    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)

    plan = create_plan(config, config.target(None), repository, None, full=False)

    assert plan.full
    assert [(type(item), item.remote_path) for item in plan.operations] == [
        (UploadOperation, "app.py"),
        (UploadOperation, "public/dist/asset.js"),
    ]


def test_plan_freezes_and_renders_reviewed_after_deploy_commands(git_project: Path) -> None:
    """The confirmation surface includes commands frozen into the resolved target."""

    config = load_config(
        write_config(
            git_project,
            """
[targets.dev]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
after_deploy = ["restart-app", "check-app"]
""",
        )
    )

    plan = create_plan(config, config.target(None), GitRepository(git_project), None, full=False)
    rendered = render_plan(plan)

    assert plan.target.after_deploy == ("restart-app", "check-app")
    assert "AFTER  restart-app" in rendered
    assert "AFTER  check-app" in rendered
    assert "2 after-deploy command(s)" in rendered


def test_incremental_source_and_output_changes(git_project: Path) -> None:
    """Git deletion and changed/new/removed output hashes merge deterministically."""

    dist = git_project / "dist"
    dist.mkdir()
    (dist / "new.js").write_text("new", encoding="utf-8")
    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    old = repository.head()
    (git_project / "app.py").unlink()
    commit_all(git_project, "delete source")
    state = TargetState(
        1,
        "dev",
        config.target(None).fingerprint,
        old,
        1,
        {
            "public/dist/new.js": ManifestEntry("0" * 64, 3),
            "public/dist/old.js": ManifestEntry("1" * 64, 3),
            "remote-unknown.txt": ManifestEntry("2" * 64, 3),
        },
    )

    plan = create_plan(config, config.target(None), repository, state, full=False)

    assert {(type(item), item.remote_path) for item in plan.operations} == {
        (DeleteOperation, "app.py"),
        (UploadOperation, "public/dist/new.js"),
        (DeleteOperation, "public/dist/old.js"),
    }
    assert all(item.remote_path != "remote-unknown.txt" for item in plan.operations)


def test_unchanged_output_is_not_uploaded(git_project: Path) -> None:
    """Equal SHA256 and size records suppress redundant output transfer."""

    dist = git_project / "dist"
    dist.mkdir()
    (dist / "asset.js").write_text("same", encoding="utf-8")
    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    full = create_plan(config, config.target(None), repository, None, full=False)
    state = TargetState(
        1,
        "dev",
        config.target(None).fingerprint,
        repository.head(),
        1,
        full.output_manifest,
    )

    incremental = create_plan(config, config.target(None), repository, state, full=False)

    assert incremental.operations == ()


def test_modified_source_is_uploaded(git_project: Path) -> None:
    """A committed Git modification produces one exact source upload."""

    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    old = repository.head()
    (git_project / "app.py").write_text("print('v2')\n", encoding="utf-8")
    commit_all(git_project, "modify source")
    state = TargetState(1, "dev", config.target(None).fingerprint, old, 1, {})

    plan = create_plan(config, config.target(None), repository, state, full=False)

    assert [(type(item), item.remote_path) for item in plan.operations] == [
        (UploadOperation, "app.py")
    ]


def test_existing_empty_output_root_can_delete_owned_manifest_files(git_project: Path) -> None:
    """An existing empty directory is an explicit current set, unlike a missing root."""

    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    state = TargetState(
        1,
        "dev",
        config.target(None).fingerprint,
        repository.head(),
        1,
        {"public/dist/old.js": ManifestEntry("1" * 64, 3)},
    )

    plan = create_plan(config, config.target(None), repository, state, full=False)

    assert [(type(item), item.remote_path) for item in plan.operations] == [
        (DeleteOperation, "public/dist/old.js")
    ]


def test_protected_output_destination_fails_closed(git_project: Path) -> None:
    """Final protection also applies to output mappings, not only Git source."""

    (git_project / "dist").mkdir()
    (git_project / "dist/secret").write_text("secret", encoding="utf-8")
    config = load_config(
        write_config(
            git_project,
            """
[[outputs]]
local = "dist"
remote = ".env"

[targets.dev]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
""",
        )
    )

    with pytest.raises(PlanError, match="protected"):
        create_plan(config, config.target(None), GitRepository(git_project), None, full=False)


def test_target_change_requires_explicit_full(git_project: Path) -> None:
    """An existing state cannot silently bind to a different physical target."""

    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    state = TargetState(1, "dev", "sftp:other", repository.head(), 1, {})

    with pytest.raises(PlanError, match="identity changed"):
        create_plan(config, config.target(None), repository, state, full=False)


def test_source_output_remote_collision_is_rejected(git_project: Path) -> None:
    """Two ownership domains cannot publish different bytes to the same remote path."""

    output = git_project / "generated"
    output.mkdir()
    (output / "app.py").write_text("generated", encoding="utf-8")
    config = load_config(
        write_config(
            git_project,
            """
[[outputs]]
local = "generated"
remote = "."

[targets.dev]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
""",
        )
    )

    with pytest.raises(PlanError, match="ownership conflict"):
        create_plan(config, config.target(None), GitRepository(git_project), None, full=False)


def test_complete_ownership_conflict_is_rejected_when_source_is_unchanged(
    git_project: Path,
) -> None:
    """Full ownership validation does not depend on a source operation this run."""

    generated = git_project / "generated"
    generated.mkdir()
    (generated / "app.py").write_text("generated", encoding="utf-8")
    config = load_config(
        write_config(
            git_project,
            """
[[outputs]]
local = "generated"
remote = "."

[targets.dev]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
""",
        )
    )
    repository = GitRepository(git_project)
    state = TargetState(
        1,
        "dev",
        config.target(None).fingerprint,
        repository.head(),
        1,
        {"app.py": ManifestEntry("0" * 64, 9)},
    )

    with pytest.raises(PlanError, match="ownership conflict"):
        create_plan(config, config.target(None), repository, state, full=False)


def test_ssh_alias_is_resolved_and_frozen_into_plan(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewed plans bind the effective SSH endpoint, not a movable alias string."""

    config = load_config(
        write_config(
            git_project,
            """
[targets.dev]
protocol = "sftp"
ssh_host_alias = "project-dev"
remote_root = "/srv/app"
""",
        )
    )
    endpoint = {"host": "192.0.2.10"}
    calls = 0
    real_run = subprocess.run

    def resolve(*args, **kwargs) -> subprocess.CompletedProcess[str]:  # noqa: ANN002, ANN003
        """Return a movable alias while recording resolution count."""

        if args and args[0] and args[0][0] != "ssh":
            return real_run(*args, **kwargs)
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=f"hostname {endpoint['host']}\nuser deploy\nport 2222\n",
            stderr="",
        )

    monkeypatch.setattr("git_deploy.config.subprocess.run", resolve)
    repository = GitRepository(git_project)
    plan = create_plan(config, config.target(None), repository, None, full=False)
    endpoint["host"] = "192.0.2.99"

    assert plan.target.ssh_resolved
    assert plan.target.host == "192.0.2.10"
    assert plan.target_fingerprint == "sftp:deploy@192.0.2.10:2222:/srv/app"
    assert plan.target.fingerprint == plan.target_fingerprint
    assert calls == 1

    state = TargetState(1, "dev", plan.target_fingerprint, repository.head(), 1, {})
    with pytest.raises(PlanError, match="identity changed"):
        create_plan(config, config.target(None), repository, state, full=False)


def test_executable_source_is_preserved_for_sftp_and_rejected_for_ftp(
    git_project: Path,
) -> None:
    """Executable Git mode is explicit and FTP cannot silently drop it."""

    script = git_project / "deploy.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    commit_all(git_project, "add executable")
    repository = GitRepository(git_project)
    sftp_config = load_config(write_config(git_project))

    plan = create_plan(sftp_config, sftp_config.target(None), repository, None, full=False)
    operation = next(item for item in plan.operations if item.remote_path == "deploy.sh")
    assert isinstance(operation, UploadOperation)
    assert operation.executable

    ftp_config = load_config(
        write_config(
            git_project,
            """
[targets.dev]
protocol = "ftp"
host = "ftp.example.invalid"
username = "deploy"
password_env = "FTP_PASSWORD"
remote_root = "/public_html"
""",
        )
    )
    with pytest.raises(PlanError, match="executable mode"):
        create_plan(ftp_config, ftp_config.target(None), repository, None, full=False)


def test_ftp_hybrid_plan_contract_binds_content_policy_full_and_previous_state(
    git_project: Path,
) -> None:
    """Stable resume identity covers exact bytes and every drift-sensitive policy."""

    dist = git_project / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("hybrid", encoding="utf-8")
    config = load_config(
        write_config(
            git_project,
            """
project_id = "github.com/acme/project"

[source]
include = ["**"]

[[outputs]]
name = "frontend-root"
local = "dist"
remote = "."
mode = "hybrid"

[targets.dev]
protocol = "ftp"
host = "ftp.example.invalid"
username = "deploy"
password_env = "FTP_PASSWORD"
remote_root = "/public_html"
""",
        )
    )
    repository = GitRepository(git_project)
    plan = create_plan(config, config.target(None), repository, None, full=False)
    source = next(item for item in plan.operations if item.remote_path == "app.py")

    assert isinstance(source, UploadOperation)
    assert source.sha256 is not None
    assert source.size == len(b"print('v1')\n")
    assert plan.non_hybrid_plan_hash == deployment_contract_hash(plan, config)
    assert len(plan.non_hybrid_plan_hash) == 64
    assert len(plan.previous_state_hash) == 64
    assert replace(plan, full=not plan.full).non_hybrid_plan_hash == plan.non_hybrid_plan_hash
    assert (
        deployment_contract_hash(replace(plan, full=not plan.full), config)
        != plan.non_hybrid_plan_hash
    )


def test_non_ftp_hybrid_plans_skip_source_content_contract(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary SFTP planning performs no extra source content hashing."""

    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)

    def forbidden(entries: object) -> object:
        """Fail if a non-FTP-Hybrid plan requests batch content identities."""

        del entries
        raise AssertionError("source content contract must be conditional")

    monkeypatch.setattr(repository, "blob_manifests", forbidden)
    plan = create_plan(config, config.target(None), repository, None, full=False)
    source = next(item for item in plan.operations if item.remote_path == "app.py")

    assert isinstance(source, UploadOperation)
    assert source.sha256 is None
    assert source.size == len(b"print('v1')\n")
    assert plan.non_hybrid_plan_hash == ""


def _ftp_hybrid_project(git_project: Path) -> tuple[object, GitRepository]:
    """Materialize a small FTP Hybrid aggregation root for planner tests.

    Args:
        git_project: Temporary Git worktree fixture root.

    Returns:
        Loaded config and repository with ``assets`` mirror files present.
    """

    root = git_project / ".deploy" / "frontend-root"
    (root / "assets" / "nested").mkdir(parents=True)
    (root / "index.html").write_text("home", encoding="utf-8")
    (root / "assets" / "app.js").write_text("app-v1", encoding="utf-8")
    (root / "assets" / "nested" / "chunk.js").write_text("chunk-v1", encoding="utf-8")
    config = load_config(
        write_config(
            git_project,
            """
project_id = "github.com/acme/project"

[source]
include = ["app.py"]

[[outputs]]
name = "frontend-root"
local = ".deploy/frontend-root"
remote = "."
mode = "hybrid"

[targets.dev]
protocol = "ftp"
host = "ftp.example.invalid"
username = "deploy"
password_env = "FTP_PASSWORD"
remote_root = "/public_html"
""",
            create_outputs=False,
        )
    )
    return config, GitRepository(git_project)


def test_hybrid_output_manifest_includes_mirror_file_hashes(git_project: Path) -> None:
    """Local State records nested Mirror file identity for FTP incremental uploads."""

    config, repository = _ftp_hybrid_project(git_project)
    plan = create_plan(config, config.target(None), repository, None, full=False)

    assert "index.html" in plan.output_manifest
    assert "assets/app.js" in plan.output_manifest
    assert "assets/nested/chunk.js" in plan.output_manifest
    assert plan.output_manifest["assets/app.js"].size == len(b"app-v1")
    assert plan.output_manifest["assets/nested/chunk.js"].size == len(b"chunk-v1")
    assert plan.hybrid is not None
    assert plan.hybrid.previous_outputs == {}


def test_ftp_hybrid_mirror_plan_skips_unchanged_uploads_and_republishes_gaps(
    git_project: Path,
    tmp_path: Path,
) -> None:
    """Mirror uploads follow Local State hash + remote presence, not full trees."""

    from git_deploy.config import resolve_target_for_plan
    from git_deploy.ftp_hybrid import (
        FTP_CAPABILITY_SCHEMA,
        FTPHybridCapabilities,
        save_capability_profile,
    )
    from git_deploy.hybrid import make_ownership, serialize_ownership
    from git_deploy.manifest import new_state
    from git_deploy.planner import complete_remote_plan
    from git_deploy.transports.base import RemotePathType
    from git_deploy.transports.ftp import FTPRemoteEntry, FTPTransport

    config, repository = _ftp_hybrid_project(git_project)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    target = resolve_target_for_plan(config.target(None), runtime_dir=runtime)
    banner = "a" * 64
    save_capability_profile(
        runtime,
        FTPHybridCapabilities(
            FTP_CAPABILITY_SCHEMA,
            target.fingerprint,
            banner,
            True,
            True,
            True,
            True,
            True,
            True,
            True,
            100,
            True,
            True,
            True,
        ),
    )

    first = create_plan(config, target, repository, None, full=False, resolved_target=target)
    assert first.hybrid is not None
    ownership = make_ownership(
        first.hybrid.local,
        config.project_id or "",
        first.head,
        now=10,
    )
    ownership_bytes = serialize_ownership(ownership)
    state = new_state(
        first.target.name,
        first.target_fingerprint,
        first.head,
        dict(first.output_manifest),
    )
    aggregation = git_project / ".deploy" / "frontend-root"

    class PlanFTPTransport(FTPTransport):
        """In-memory FTP tree sufficient for FTP Hybrid remote planning."""

        def __init__(self) -> None:
            """Seed ownership and the previously published mirror tree."""

            super().__init__(target)
            self._file_bytes = {
                ".git-deploy/hybrid/frontend-root.json": ownership_bytes,
                "index.html": b"home",
                "assets/app.js": b"app-v1",
                "assets/nested/chunk.js": b"chunk-v1",
                "app.py": b"print('v1')\n",
            }
            self._directories = {
                "",
                ".git-deploy",
                ".git-deploy/hybrid",
                "assets",
                "assets/nested",
            }

        def connect(self) -> None:
            """Mark the adapter connected without a real socket."""

            self.ftp = self  # type: ignore[assignment]

        def close(self) -> None:
            """Drop the synthetic session handle."""

            self.ftp = None

        def enable_utf8(self) -> None:
            """Accept UTF-8 as required for Hybrid planning."""

            self._require_utf8 = True

        def server_banner_hash(self) -> str:
            """Return the capability-profile banner identity."""

            return banner

        def features(self) -> frozenset[str]:
            """Advertise MLSD and UTF8 for the capability gate."""

            return frozenset({"MLSD", "UTF8"})

        def list_root_names(self) -> tuple[str, ...]:
            """Expose only direct children of the synthetic root."""

            names = sorted(
                {
                    path.split("/", 1)[0]
                    for path in (*self._file_bytes, *self._directories)
                    if path and "/" not in path
                }
            )
            self._root_names = tuple(names)
            self._root_types = {
                name: (
                    RemotePathType.DIRECTORY
                    if name in self._directories
                    else RemotePathType.FILE
                )
                for name in names
            }
            return self._root_names

        def list_directory_typed(
            self,
            remote_path: str,
            *,
            allow_case_collisions: bool = False,
        ) -> tuple[FTPRemoteEntry, ...]:
            """Return one level of typed children under ``remote_path``."""

            del allow_case_collisions
            prefix = "" if remote_path in {"", "."} else remote_path.rstrip("/") + "/"
            children: dict[str, RemotePathType] = {}
            for directory in self._directories:
                if not directory.startswith(prefix):
                    continue
                rest = directory[len(prefix) :]
                if rest and "/" not in rest:
                    children[rest] = RemotePathType.DIRECTORY
            for file_path in self._file_bytes:
                if not file_path.startswith(prefix):
                    continue
                rest = file_path[len(prefix) :]
                if rest and "/" not in rest:
                    children[rest] = RemotePathType.FILE
            return tuple(
                FTPRemoteEntry(name, kind, None, None)
                for name, kind in sorted(children.items())
            )

        def read_file(
            self,
            remote_path: str,
            *,
            max_bytes: int,
            allow_case_collisions: bool = False,
        ) -> bytes:
            """Return configured file bytes with the hard bound enforced."""

            del allow_case_collisions
            data = self._file_bytes[remote_path]
            if len(data) > max_bytes:
                raise AssertionError(f"test fixture exceeds max_bytes for {remote_path}")
            return data

        def lstat(
            self,
            remote_path: str,
            *,
            allow_case_collisions: bool = False,
        ) -> RemotePathType:
            """Classify synthetic paths without consulting a real FTP server."""

            del allow_case_collisions
            path = remote_path.strip("/")
            if path in {"", "."}:
                return RemotePathType.DIRECTORY
            if path in self._directories:
                return RemotePathType.DIRECTORY
            if path in self._file_bytes:
                return RemotePathType.FILE
            return RemotePathType.MISSING

    transport = PlanFTPTransport()
    transport.connect()

    # Unchanged Local State + remote still present → no Mirror/Root Hybrid uploads.
    noop = create_plan(
        config,
        target,
        repository,
        state,
        full=False,
        resolved_target=target,
    )
    noop_remote = complete_remote_plan(noop, config, transport)
    assert noop_remote.hybrid is not None and noop_remote.hybrid.ftp is not None
    assert noop_remote.hybrid.ftp.uploads == ()

    # Content change forces only that Mirror path.
    (aggregation / "assets" / "app.js").write_text("app-v2", encoding="utf-8")
    changed = create_plan(
        config,
        target,
        repository,
        state,
        full=False,
        resolved_target=target,
    )
    changed_remote = complete_remote_plan(changed, config, transport)
    assert changed_remote.hybrid is not None and changed_remote.hybrid.ftp is not None
    assert [item.path for item in changed_remote.hybrid.ftp.uploads] == ["assets/app.js"]

    # Restore content; remote gap still republishes even when Local State matches.
    (aggregation / "assets" / "app.js").write_text("app-v1", encoding="utf-8")
    del transport._file_bytes["assets/nested/chunk.js"]
    gap = create_plan(
        config,
        target,
        repository,
        state,
        full=False,
        resolved_target=target,
    )
    gap_remote = complete_remote_plan(gap, config, transport)
    assert gap_remote.hybrid is not None and gap_remote.hybrid.ftp is not None
    assert [item.path for item in gap_remote.hybrid.ftp.uploads] == [
        "assets/nested/chunk.js"
    ]

    # --full forces every current Mirror and Root file.
    transport._file_bytes["assets/nested/chunk.js"] = b"chunk-v1"
    full = create_plan(
        config,
        target,
        repository,
        state,
        full=True,
        resolved_target=target,
    )
    full_remote = complete_remote_plan(full, config, transport)
    assert full_remote.hybrid is not None and full_remote.hybrid.ftp is not None
    assert full_remote.hybrid.ftp.incremental_mirror is False
    assert sorted(item.path for item in full_remote.hybrid.ftp.uploads) == [
        "assets/app.js",
        "assets/nested/chunk.js",
        "index.html",
    ]
    from git_deploy.planner import render_hybrid_plan

    full_lines = "\n".join(render_hybrid_plan(full_remote.hybrid))
    assert "FTP MIRROR MODE: STRONG" in full_lines
    assert "STAGE-VERIFIED RENAME-TRUSTED" in full_lines
    assert noop_remote.hybrid is not None and noop_remote.hybrid.ftp is not None
    assert noop_remote.hybrid.ftp.incremental_mirror is True
    noop_lines = "\n".join(render_hybrid_plan(noop_remote.hybrid))
    assert "LOCAL-STATE INCREMENTAL" in noop_lines
    assert "REMOTE CONTENT HASH: NOT VERIFIED" in noop_lines


def test_files_published_plan_skips_uploads_and_fail_closed_on_missing(
    git_project: Path,
    tmp_path: Path,
) -> None:
    """FILES_PUBLISHED Plan matches executor: no Hybrid uploads; missing files abort."""

    from git_deploy.config import resolve_target_for_plan
    from git_deploy.ftp_hybrid import (
        FTP_CAPABILITY_SCHEMA,
        FTP_PENDING_SCHEMA,
        FTPHybridCapabilities,
        FTPHybridPending,
        FTPPendingPhase,
        pending_local_manifest_hash,
        pending_path,
        save_capability_profile,
        serialize_pending,
    )
    from git_deploy.hybrid import make_ownership, ownership_hash, serialize_ownership
    from git_deploy.manifest import new_state
    from git_deploy.planner import complete_remote_plan, render_hybrid_plan
    from git_deploy.transports.base import RemotePathType
    from git_deploy.transports.ftp import FTPRemoteEntry, FTPTransport

    config, repository = _ftp_hybrid_project(git_project)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    target = resolve_target_for_plan(config.target(None), runtime_dir=runtime)
    banner = "b" * 64
    save_capability_profile(
        runtime,
        FTPHybridCapabilities(
            FTP_CAPABILITY_SCHEMA,
            target.fingerprint,
            banner,
            True,
            True,
            True,
            True,
            True,
            True,
            True,
            100,
            True,
            True,
            True,
        ),
    )
    first = create_plan(config, target, repository, None, full=False, resolved_target=target)
    assert first.hybrid is not None
    # Remote Ownership uses created_at=10; Pending next Ownership uses created_at=11
    # (planner rebuilds next Ownership with pending.created_at as ``now``).
    ownership = make_ownership(
        first.hybrid.local,
        config.project_id or "",
        first.head,
        now=10,
    )
    next_ownership = make_ownership(
        first.hybrid.local,
        config.project_id or "",
        first.head,
        now=11,
    )
    ownership_bytes = serialize_ownership(ownership)
    state = new_state(
        first.target.name,
        first.target_fingerprint,
        first.head,
        dict(first.output_manifest),
    )
    pending = FTPHybridPending(
        FTP_PENDING_SCHEMA,
        config.project_id or "",
        "frontend-root",
        ".",
        target.fingerprint,
        "deployment-files-published",
        FTPPendingPhase.FILES_PUBLISHED,
        ownership_hash(ownership),
        ownership_hash(next_ownership),
        pending_local_manifest_hash(first.hybrid.local, first.output_manifest),
        first.head,
        state,
        11,
        first.non_hybrid_plan_hash,
        first.previous_state_hash,
    )
    pending_bytes = serialize_pending(pending)

    class FilesPublishedTransport(FTPTransport):
        """In-memory FTP tree with a FILES_PUBLISHED pending marker."""

        def __init__(self, *, drop_index: bool = False) -> None:
            """Seed published tree; optionally omit a current root file."""

            super().__init__(target)
            self._file_bytes = {
                ".git-deploy/hybrid/frontend-root.json": ownership_bytes,
                pending_path("frontend-root"): pending_bytes,
                "index.html": b"home",
                "assets/app.js": b"app-v1",
                "assets/nested/chunk.js": b"chunk-v1",
                "app.py": b"print('v1')\n",
            }
            if drop_index:
                del self._file_bytes["index.html"]
            self._directories = {
                "",
                ".git-deploy",
                ".git-deploy/hybrid",
                ".git-deploy/ftp-hybrid",
                ".git-deploy/ftp-hybrid/pending",
                "assets",
                "assets/nested",
            }

        def connect(self) -> None:
            """Mark the adapter connected without a real socket."""

            self.ftp = self  # type: ignore[assignment]

        def close(self) -> None:
            """Drop the synthetic session handle."""

            self.ftp = None

        def enable_utf8(self) -> None:
            """Accept UTF-8 as required for Hybrid planning."""

            self._require_utf8 = True

        def server_banner_hash(self) -> str:
            """Return the capability-profile banner identity."""

            return banner

        def features(self) -> frozenset[str]:
            """Advertise MLSD and UTF8 for the capability gate."""

            return frozenset({"MLSD", "UTF8"})

        def list_root_names(self) -> tuple[str, ...]:
            """Expose only direct children of the synthetic root."""

            names = sorted(
                {
                    path.split("/", 1)[0]
                    for path in (*self._file_bytes, *self._directories)
                    if path and "/" not in path
                }
            )
            self._root_names = tuple(names)
            self._root_types = {
                name: (
                    RemotePathType.DIRECTORY
                    if name in self._directories
                    else RemotePathType.FILE
                )
                for name in names
            }
            return self._root_names

        def list_directory_typed(
            self,
            remote_path: str,
            *,
            allow_case_collisions: bool = False,
        ) -> tuple[FTPRemoteEntry, ...]:
            """Return one level of typed children under ``remote_path``."""

            del allow_case_collisions
            prefix = "" if remote_path in {"", "."} else remote_path.rstrip("/") + "/"
            children: dict[str, RemotePathType] = {}
            for directory in self._directories:
                if not directory.startswith(prefix):
                    continue
                rest = directory[len(prefix) :]
                if rest and "/" not in rest:
                    children[rest] = RemotePathType.DIRECTORY
            for file_path in self._file_bytes:
                if not file_path.startswith(prefix):
                    continue
                rest = file_path[len(prefix) :]
                if rest and "/" not in rest:
                    children[rest] = RemotePathType.FILE
            return tuple(
                FTPRemoteEntry(name, kind, None, None)
                for name, kind in sorted(children.items())
            )

        def read_file(
            self,
            remote_path: str,
            *,
            max_bytes: int,
            allow_case_collisions: bool = False,
        ) -> bytes:
            """Return configured file bytes with the hard bound enforced."""

            del allow_case_collisions
            data = self._file_bytes[remote_path]
            if len(data) > max_bytes:
                raise AssertionError(f"test fixture exceeds max_bytes for {remote_path}")
            return data

        def lstat(
            self,
            remote_path: str,
            *,
            allow_case_collisions: bool = False,
        ) -> RemotePathType:
            """Classify synthetic paths without consulting a real FTP server."""

            del allow_case_collisions
            path = remote_path.strip("/")
            if path in {"", "."}:
                return RemotePathType.DIRECTORY
            if path in self._directories:
                return RemotePathType.DIRECTORY
            if path in self._file_bytes:
                return RemotePathType.FILE
            return RemotePathType.MISSING

    transport = FilesPublishedTransport()
    transport.connect()
    remote = complete_remote_plan(first, config, transport)
    assert remote.hybrid is not None and remote.hybrid.ftp is not None
    assert remote.hybrid.ftp.resume_phase is FTPPendingPhase.FILES_PUBLISHED
    assert remote.hybrid.ftp.uploads == ()
    assert remote.hybrid.ftp.create_directories == ()
    rendered = "\n".join(render_hybrid_plan(remote.hybrid))
    assert "UPLOAD" not in rendered or remote.hybrid.ftp.uploads == ()

    missing = FilesPublishedTransport(drop_index=True)
    missing.connect()
    with pytest.raises(PlanError, match="cannot verify published file"):
        complete_remote_plan(first, config, missing)


def test_files_published_resume_fail_closed_on_missing_empty_directories(
    git_project: Path,
    tmp_path: Path,
) -> None:
    """Published resume phases require top-level and nested empty directories."""

    from git_deploy.config import resolve_target_for_plan
    from git_deploy.ftp_hybrid import (
        FTP_CAPABILITY_SCHEMA,
        FTP_PENDING_SCHEMA,
        FTPHybridCapabilities,
        FTPHybridPending,
        FTPPendingPhase,
        pending_local_manifest_hash,
        pending_path,
        save_capability_profile,
        serialize_pending,
    )
    from git_deploy.hybrid import make_ownership, ownership_hash, serialize_ownership
    from git_deploy.manifest import new_state
    from git_deploy.planner import complete_remote_plan
    from git_deploy.transports.base import RemotePathType
    from git_deploy.transports.ftp import FTPRemoteEntry, FTPTransport

    root = git_project / ".deploy" / "frontend-root"
    (root / "assets" / "nested").mkdir(parents=True)
    (root / "assets" / "empty-nested").mkdir(parents=True)
    (root / "keep-empty").mkdir(parents=True)
    (root / "index.html").write_text("home", encoding="utf-8")
    (root / "assets" / "app.js").write_text("app-v1", encoding="utf-8")
    (root / "assets" / "nested" / "chunk.js").write_text("chunk-v1", encoding="utf-8")
    config = load_config(
        write_config(
            git_project,
            """
project_id = "github.com/acme/project"

[source]
include = ["app.py"]

[[outputs]]
name = "frontend-root"
local = ".deploy/frontend-root"
remote = "."
mode = "hybrid"

[targets.dev]
protocol = "ftp"
host = "ftp.example.invalid"
username = "deploy"
password_env = "FTP_PASSWORD"
remote_root = "/public_html"
""",
            create_outputs=False,
        )
    )
    repository = GitRepository(git_project)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    target = resolve_target_for_plan(config.target(None), runtime_dir=runtime)
    banner = "c" * 64
    save_capability_profile(
        runtime,
        FTPHybridCapabilities(
            FTP_CAPABILITY_SCHEMA,
            target.fingerprint,
            banner,
            True,
            True,
            True,
            True,
            True,
            True,
            True,
            100,
            True,
            True,
            True,
        ),
    )
    first = create_plan(config, target, repository, None, full=False, resolved_target=target)
    assert first.hybrid is not None
    assert "keep-empty" in first.hybrid.local.directory_names
    assets = next(item for item in first.hybrid.local.directories if item.name == "assets")
    assert "empty-nested" in assets.directories

    ownership = make_ownership(
        first.hybrid.local,
        config.project_id or "",
        first.head,
        now=10,
    )
    next_ownership = make_ownership(
        first.hybrid.local,
        config.project_id or "",
        first.head,
        now=11,
    )
    ownership_bytes = serialize_ownership(ownership)
    state = new_state(
        first.target.name,
        first.target_fingerprint,
        first.head,
        dict(first.output_manifest),
    )

    def make_pending(phase: FTPPendingPhase) -> FTPHybridPending:
        """Build a schema-2 pending marker for one published-phase resume."""

        return FTPHybridPending(
            FTP_PENDING_SCHEMA,
            config.project_id or "",
            "frontend-root",
            ".",
            target.fingerprint,
            f"deployment-{phase.value.lower()}",
            phase,
            ownership_hash(ownership),
            ownership_hash(next_ownership),
            pending_local_manifest_hash(first.hybrid.local, first.output_manifest),
            first.head,
            state,
            11,
            first.non_hybrid_plan_hash,
            first.previous_state_hash,
        )

    class EmptyDirResumeTransport(FTPTransport):
        """In-memory tree with optional dropped/mis-typed empty directories."""

        def __init__(
            self,
            *,
            drop_top: bool = False,
            drop_nested: bool = False,
            top_as_file: bool = False,
            phase: FTPPendingPhase = FTPPendingPhase.FILES_PUBLISHED,
        ) -> None:
            """Seed a published tree; optionally omit or mistype empty dirs."""

            super().__init__(target)
            pending_bytes = serialize_pending(make_pending(phase))
            self._file_bytes = {
                ".git-deploy/hybrid/frontend-root.json": ownership_bytes,
                pending_path("frontend-root"): pending_bytes,
                "index.html": b"home",
                "assets/app.js": b"app-v1",
                "assets/nested/chunk.js": b"chunk-v1",
                "app.py": b"print('v1')\n",
            }
            self._directories = {
                "",
                ".git-deploy",
                ".git-deploy/hybrid",
                ".git-deploy/ftp-hybrid",
                ".git-deploy/ftp-hybrid/pending",
                "assets",
                "assets/nested",
                "assets/empty-nested",
                "keep-empty",
            }
            if drop_top:
                self._directories.discard("keep-empty")
            if drop_nested:
                self._directories.discard("assets/empty-nested")
            if top_as_file:
                self._directories.discard("keep-empty")
                self._file_bytes["keep-empty"] = b"not-a-directory"

        def connect(self) -> None:
            """Mark the adapter connected without a real socket."""

            self.ftp = self  # type: ignore[assignment]

        def close(self) -> None:
            """Drop the synthetic session handle."""

            self.ftp = None

        def enable_utf8(self) -> None:
            """Accept UTF-8 as required for Hybrid planning."""

            self._require_utf8 = True

        def server_banner_hash(self) -> str:
            """Return the capability-profile banner identity."""

            return banner

        def features(self) -> frozenset[str]:
            """Advertise MLSD and UTF8 for the capability gate."""

            return frozenset({"MLSD", "UTF8"})

        def list_root_names(self) -> tuple[str, ...]:
            """Expose only direct children of the synthetic root."""

            names = sorted(
                {
                    path.split("/", 1)[0]
                    for path in (*self._file_bytes, *self._directories)
                    if path and "/" not in path
                }
            )
            self._root_names = tuple(names)
            self._root_types = {
                name: (
                    RemotePathType.DIRECTORY
                    if name in self._directories
                    else RemotePathType.FILE
                )
                for name in names
            }
            return self._root_names

        def list_directory_typed(
            self,
            remote_path: str,
            *,
            allow_case_collisions: bool = False,
        ) -> tuple[FTPRemoteEntry, ...]:
            """Return one level of typed children under ``remote_path``."""

            del allow_case_collisions
            prefix = "" if remote_path in {"", "."} else remote_path.rstrip("/") + "/"
            children: dict[str, RemotePathType] = {}
            for directory in self._directories:
                if not directory.startswith(prefix):
                    continue
                rest = directory[len(prefix) :]
                if rest and "/" not in rest:
                    children[rest] = RemotePathType.DIRECTORY
            for file_path in self._file_bytes:
                if not file_path.startswith(prefix):
                    continue
                rest = file_path[len(prefix) :]
                if rest and "/" not in rest:
                    children[rest] = RemotePathType.FILE
            return tuple(
                FTPRemoteEntry(name, kind, None, None)
                for name, kind in sorted(children.items())
            )

        def read_file(
            self,
            remote_path: str,
            *,
            max_bytes: int,
            allow_case_collisions: bool = False,
        ) -> bytes:
            """Return configured file bytes with the hard bound enforced."""

            del allow_case_collisions
            data = self._file_bytes[remote_path]
            if len(data) > max_bytes:
                raise AssertionError(f"test fixture exceeds max_bytes for {remote_path}")
            return data

        def lstat(
            self,
            remote_path: str,
            *,
            allow_case_collisions: bool = False,
        ) -> RemotePathType:
            """Classify synthetic paths without consulting a real FTP server."""

            del allow_case_collisions
            path = remote_path.strip("/")
            if path in {"", "."}:
                return RemotePathType.DIRECTORY
            if path in self._directories:
                return RemotePathType.DIRECTORY
            if path in self._file_bytes:
                return RemotePathType.FILE
            return RemotePathType.MISSING

    ok = EmptyDirResumeTransport()
    ok.connect()
    remote = complete_remote_plan(first, config, ok)
    assert remote.hybrid is not None and remote.hybrid.ftp is not None
    assert remote.hybrid.ftp.resume_phase is FTPPendingPhase.FILES_PUBLISHED
    assert remote.hybrid.ftp.uploads == ()

    missing_top = EmptyDirResumeTransport(drop_top=True)
    missing_top.connect()
    with pytest.raises(PlanError, match="cannot verify published directory: keep-empty"):
        complete_remote_plan(first, config, missing_top)

    missing_nested = EmptyDirResumeTransport(drop_nested=True)
    missing_nested.connect()
    with pytest.raises(
        PlanError,
        match="cannot verify published mirror directories: assets/empty-nested",
    ):
        complete_remote_plan(first, config, missing_nested)

    type_drift = EmptyDirResumeTransport(top_as_file=True)
    type_drift.connect()
    with pytest.raises(PlanError, match="type mismatch for directory 'keep-empty'|cannot verify published directory"):
        complete_remote_plan(first, config, type_drift)

    pruned_ok = EmptyDirResumeTransport(phase=FTPPendingPhase.PRUNED)
    pruned_ok.connect()
    pruned_remote = complete_remote_plan(first, config, pruned_ok)
    assert pruned_remote.hybrid is not None and pruned_remote.hybrid.ftp is not None
    assert pruned_remote.hybrid.ftp.resume_phase is FTPPendingPhase.PRUNED

    pruned_missing_nested = EmptyDirResumeTransport(
        drop_nested=True,
        phase=FTPPendingPhase.PRUNED,
    )
    pruned_missing_nested.connect()
    with pytest.raises(
        PlanError,
        match="cannot verify published mirror directories: assets/empty-nested",
    ):
        complete_remote_plan(first, config, pruned_missing_nested)


def test_ftp_strong_mirror_republishes_all_current_files(
    git_project: Path,
    tmp_path: Path,
) -> None:
    """deploy.ftp_incremental_mirror=false uploads every Hybrid file like strong Mirror."""

    from git_deploy.config import resolve_target_for_plan
    from git_deploy.ftp_hybrid import (
        FTP_CAPABILITY_SCHEMA,
        FTPHybridCapabilities,
        save_capability_profile,
    )
    from git_deploy.hybrid import make_ownership, serialize_ownership
    from git_deploy.manifest import new_state
    from git_deploy.planner import complete_remote_plan
    from git_deploy.transports.base import RemotePathType
    from git_deploy.transports.ftp import FTPRemoteEntry, FTPTransport

    root = git_project / ".deploy" / "frontend-root"
    (root / "assets" / "nested").mkdir(parents=True)
    (root / "index.html").write_text("home", encoding="utf-8")
    (root / "assets" / "app.js").write_text("app-v1", encoding="utf-8")
    (root / "assets" / "nested" / "chunk.js").write_text("chunk-v1", encoding="utf-8")
    config = load_config(
        write_config(
            git_project,
            """
project_id = "github.com/acme/project"

[source]
include = ["app.py"]

[[outputs]]
name = "frontend-root"
local = ".deploy/frontend-root"
remote = "."
mode = "hybrid"

[targets.dev]
protocol = "ftp"
host = "ftp.example.invalid"
username = "deploy"
password_env = "FTP_PASSWORD"
remote_root = "/public_html"

[deploy]
ftp_incremental_mirror = false
""",
            create_outputs=False,
        )
    )
    repository = GitRepository(git_project)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    target = resolve_target_for_plan(config.target(None), runtime_dir=runtime)
    banner = "b" * 64
    save_capability_profile(
        runtime,
        FTPHybridCapabilities(
            FTP_CAPABILITY_SCHEMA,
            target.fingerprint,
            banner,
            True,
            True,
            True,
            True,
            True,
            True,
            True,
            100,
            True,
            True,
            True,
        ),
    )
    first = create_plan(config, target, repository, None, full=False, resolved_target=target)
    assert first.hybrid is not None
    ownership = make_ownership(
        first.hybrid.local,
        config.project_id or "",
        first.head,
        now=10,
    )
    state = new_state(
        first.target.name,
        first.target_fingerprint,
        first.head,
        dict(first.output_manifest),
    )

    class StrongPlanFTP(FTPTransport):
        """Remote tree already matches Local State content identity."""

        def __init__(self) -> None:
            super().__init__(target)
            self._file_bytes = {
                ".git-deploy/hybrid/frontend-root.json": serialize_ownership(ownership),
                "index.html": b"home",
                "assets/app.js": b"app-v1",
                "assets/nested/chunk.js": b"chunk-v1",
                "app.py": b"print('v1')\n",
            }
            self._directories = {
                "",
                ".git-deploy",
                ".git-deploy/hybrid",
                "assets",
                "assets/nested",
            }

        def connect(self) -> None:
            self.ftp = self  # type: ignore[assignment]

        def close(self) -> None:
            self.ftp = None

        def enable_utf8(self) -> None:
            self._require_utf8 = True

        def server_banner_hash(self) -> str:
            return banner

        def features(self) -> frozenset[str]:
            return frozenset({"MLSD", "UTF8"})

        def list_root_names(self) -> tuple[str, ...]:
            names = sorted(
                {
                    path.split("/", 1)[0]
                    for path in (*self._file_bytes, *self._directories)
                    if path and "/" not in path
                }
            )
            return tuple(names)

        def list_directory_typed(
            self,
            remote_path: str,
            *,
            allow_case_collisions: bool = False,
        ) -> tuple[FTPRemoteEntry, ...]:
            del allow_case_collisions
            prefix = "" if remote_path in {"", "."} else remote_path.rstrip("/") + "/"
            children: dict[str, RemotePathType] = {}
            for directory in self._directories:
                if not directory.startswith(prefix):
                    continue
                rest = directory[len(prefix) :]
                if rest and "/" not in rest:
                    children[rest] = RemotePathType.DIRECTORY
            for file_path in self._file_bytes:
                if not file_path.startswith(prefix):
                    continue
                rest = file_path[len(prefix) :]
                if rest and "/" not in rest:
                    children[rest] = RemotePathType.FILE
            return tuple(
                FTPRemoteEntry(name, kind, None, None)
                for name, kind in sorted(children.items())
            )

        def read_file(
            self,
            remote_path: str,
            *,
            max_bytes: int,
            allow_case_collisions: bool = False,
        ) -> bytes:
            del allow_case_collisions
            data = self._file_bytes[remote_path]
            if len(data) > max_bytes:
                raise AssertionError(f"test fixture exceeds max_bytes for {remote_path}")
            return data

        def lstat(
            self,
            remote_path: str,
            *,
            allow_case_collisions: bool = False,
        ) -> RemotePathType:
            del allow_case_collisions
            path = remote_path.strip("/")
            if path in {"", "."}:
                return RemotePathType.DIRECTORY
            if path in self._directories:
                return RemotePathType.DIRECTORY
            if path in self._file_bytes:
                return RemotePathType.FILE
            return RemotePathType.MISSING

    transport = StrongPlanFTP()
    transport.connect()
    plan = create_plan(
        config,
        target,
        repository,
        state,
        full=False,
        resolved_target=target,
    )
    remote = complete_remote_plan(plan, config, transport)
    assert remote.hybrid is not None and remote.hybrid.ftp is not None
    assert remote.hybrid.ftp.incremental_mirror is False
    assert sorted(item.path for item in remote.hybrid.ftp.uploads) == [
        "assets/app.js",
        "assets/nested/chunk.js",
        "index.html",
    ]


@pytest.mark.parametrize(
    "protocol,output_mode",
    (("ftp", "incremental"), ("sftp", "hybrid")),
)
def test_source_contract_requires_both_ftp_and_hybrid(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    output_mode: str,
) -> None:
    """FTP Incremental and SFTP Hybrid both avoid the resume-only source hash."""

    dist = git_project / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("output", encoding="utf-8")
    password = 'password_env = "FTP_PASSWORD"' if protocol == "ftp" else ""
    strict = "strict_host_key_checking = true" if protocol == "sftp" else ""
    project_id = 'project_id = "github.com/acme/project"' if output_mode == "hybrid" else ""
    name = 'name = "frontend-root"' if output_mode == "hybrid" else ""
    remote = "." if output_mode == "hybrid" else "public/dist"
    config = load_config(
        write_config(
            git_project,
            f"""
{project_id}

[source]
include = ["**"]

[[outputs]]
{name}
local = "dist"
remote = "{remote}"
mode = "{output_mode}"

[targets.dev]
protocol = "{protocol}"
host = "example.invalid"
username = "deploy"
{password}
remote_root = "/srv/app"
{strict}
""",
        )
    )
    repository = GitRepository(git_project)

    def forbidden(entries: object) -> object:
        """Fail if either half of the required protocol/mode pair is absent."""

        del entries
        raise AssertionError("source content contract must require FTP Hybrid")

    monkeypatch.setattr(repository, "blob_manifests", forbidden)
    plan = create_plan(config, config.target(None), repository, None, full=False)

    assert plan.non_hybrid_plan_hash == ""


def test_ftp_hybrid_rejects_cross_owner_casefold_root_before_connection(
    git_project: Path,
) -> None:
    """Source and Hybrid direct roots share one NFC-plus-casefold namespace."""

    source = git_project / "Assets/app.js"
    source.parent.mkdir()
    source.write_text("source", encoding="utf-8")
    commit_all(git_project, "add source root")
    dist = git_project / "dist"
    dist.mkdir()
    (dist / "assets").write_text("hybrid", encoding="utf-8")
    config = load_config(
        write_config(
            git_project,
            """
project_id = "github.com/acme/project"

[source]
include = ["**"]

[[outputs]]
name = "frontend-root"
local = "dist"
remote = "."
mode = "hybrid"

[targets.dev]
protocol = "ftp"
host = "ftp.example.invalid"
username = "deploy"
password_env = "FTP_PASSWORD"
remote_root = "/public_html"
""",
        )
    )

    with pytest.raises(PlanError, match="root namespace.*Assets.*assets"):
        create_plan(config, config.target(None), GitRepository(git_project), None, full=False)
