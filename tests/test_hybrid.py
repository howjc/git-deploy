"""Hybrid Output ownership, adoption, recovery, and safe convergence tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath
import shutil

import pytest

import git_deploy.cli as cli
from git_deploy.config import ConfigError, load_config
from git_deploy.deployer import execute_plan
from git_deploy.errors import DeployError, PlanError, StaleRemotePlanError
from git_deploy.doctor import run_doctor
from git_deploy.git import GitRepository
from git_deploy.hybrid import (
    MAX_REMOTE_RECORD_BYTES,
    HybridOwnership,
    parse_ownership,
    read_ownership,
    scan_hybrid_output,
    serialize_ownership,
)
from git_deploy.manifest import StateStore
from git_deploy.planner import (
    HybridAdoption,
    HybridDirectoryDelete,
    HybridRootFileDelete,
    create_plan,
    render_plan,
)
from git_deploy.prepared import execute_prepared, prepare_project, prepare_remote_plan
from git_deploy.transports.base import ProgressCallback, RemotePathType, Transport
from git_deploy.workspace import (
    execute_workspace,
    load_workspace,
    prepare_workspace,
    render_workspace_plan,
)

from .conftest import commit_all, write_config
from .test_workspace import _create_repository, _write_workspace


class MemoryHybridTransport(Transport):
    """Model a symlink-aware SFTP tree and count every remote mutation."""

    def __init__(self) -> None:
        """Create an existing empty target root."""

        self.files: dict[str, bytes] = {}
        self.directories: set[str] = {""}
        self.symlinks: set[str] = set()
        self.connects = 0
        self.mutations = 0
        self.commands: list[str] = []
        self.fail_command = False
        self.fail_stage_publish_once = False
        self.fail_stage_upload = False
        self.fail_ownership_write_once = False
        self.fail_cleanup_once = False
        self.interrupt_stage_publish_once = False

    def connect(self) -> None:
        """Record one synthetic authentication."""

        self.connects += 1

    def ensure_root(self) -> None:
        """Keep the already-present synthetic target root."""

    def root_exists(self) -> bool:
        """Report an existing target root."""

        return True

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        callback: ProgressCallback,
        *,
        executable: bool = False,
    ) -> None:
        """Store exact frozen bytes after creating parent directories."""

        path = self._normalize(remote_path)
        if self.fail_stage_upload and path.startswith(".git-deploy/stage/"):
            raise OSError("synthetic stage upload failure")
        self.make_directory(self._parent(path))
        data = local_path.read_bytes()
        self.files[path] = data
        self.directories.discard(path)
        self.mutations += 1
        callback(len(data), len(data))

    def delete(self, remote_path: str) -> None:
        """Delete one regular file idempotently."""

        self.files.pop(self._normalize(remote_path), None)
        self.mutations += 1

    def close(self) -> None:
        """Release no in-memory resources."""

    def run_command(
        self,
        command: str,
        *,
        cwd: PurePosixPath,
        timeout: float | None,
    ) -> None:
        """Record reviewed commands and optionally fail them."""

        self.commands.append(command)
        if self.fail_command:
            raise DeployError("synthetic command failure")

    def lstat(self, remote_path: str) -> RemotePathType:
        """Classify one exact in-memory node without following symlinks."""

        path = self._normalize(remote_path)
        if path in self.symlinks:
            return RemotePathType.SYMLINK
        if path in self.files:
            return RemotePathType.FILE
        if path in self.directories:
            return RemotePathType.DIRECTORY
        return RemotePathType.MISSING

    def read_file(self, remote_path: str, *, max_bytes: int) -> bytes:
        """Read one bounded regular in-memory file."""

        path = self._normalize(remote_path)
        if self.lstat(path) is not RemotePathType.FILE:
            raise DeployError(f"not a regular file: {path}")
        data = self.files[path]
        if len(data) > max_bytes:
            raise DeployError("remote metadata file exceeds limit")
        return data

    def write_file_atomic(self, remote_path: str, data: bytes) -> None:
        """Publish metadata bytes and count one mutation."""

        path = self._normalize(remote_path)
        if self.fail_ownership_write_once and path.startswith(".git-deploy/hybrid/"):
            self.fail_ownership_write_once = False
            raise OSError("synthetic ownership write failure")
        self.make_directory(self._parent(path))
        self.files[path] = data
        self.mutations += 1

    def list_directory(self, remote_path: str) -> tuple[str, ...]:
        """Return stable direct child names."""

        parent = self._normalize(remote_path)
        if self.lstat(parent) is RemotePathType.MISSING:
            return ()
        if self.lstat(parent) is not RemotePathType.DIRECTORY:
            raise DeployError(f"not a directory: {parent}")
        nodes = set(self.files) | self.directories | self.symlinks
        return tuple(
            sorted(
                PurePosixPath(path).name
                for path in nodes
                if path and self._parent(path) == parent
            )
        )

    def make_directory(self, remote_path: str, *, mode: int = 0o755) -> None:
        """Create a directory and all parents idempotently."""

        path = self._normalize(remote_path)
        current = PurePosixPath(".")
        for component in PurePosixPath(path).parts:
            current /= component
            value = "" if current.as_posix() == "." else current.as_posix()
            if value in self.files or value in self.symlinks:
                raise DeployError(f"cannot create directory over node: {value}")
            if value not in self.directories:
                self.directories.add(value)
                self.mutations += 1

    def rename_path(self, source: str, destination: str) -> None:
        """Rename a complete file or directory subtree."""

        source_path = self._normalize(source)
        destination_path = self._normalize(destination)
        if self.lstat(destination_path) is not RemotePathType.MISSING:
            raise DeployError(f"destination exists: {destination_path}")
        if (
            self.fail_stage_publish_once
            and ".git-deploy/stage/" in source_path
            and destination_path == "assets"
        ):
            self.fail_stage_publish_once = False
            raise DeployError("synthetic stage publish failure")
        if (
            self.interrupt_stage_publish_once
            and ".git-deploy/stage/" in source_path
            and destination_path == "assets"
        ):
            self.interrupt_stage_publish_once = False
            raise KeyboardInterrupt
        kind = self.lstat(source_path)
        if kind is RemotePathType.MISSING:
            raise DeployError(f"source is missing: {source_path}")
        self.make_directory(self._parent(destination_path))
        if kind is RemotePathType.FILE:
            self.files[destination_path] = self.files.pop(source_path)
        elif kind is RemotePathType.SYMLINK:
            self.symlinks.remove(source_path)
            self.symlinks.add(destination_path)
        else:
            directory_nodes = sorted(
                (item for item in self.directories if item == source_path or item.startswith(source_path + "/")),
                key=len,
            )
            file_nodes = sorted(
                item for item in self.files if item.startswith(source_path + "/")
            )
            symlink_nodes = sorted(
                item for item in self.symlinks if item.startswith(source_path + "/")
            )
            for item in directory_nodes:
                self.directories.remove(item)
                suffix = item[len(source_path) :].lstrip("/")
                self.directories.add(
                    destination_path if not suffix else f"{destination_path}/{suffix}"
                )
            for item in file_nodes:
                suffix = item[len(source_path) :].lstrip("/")
                self.files[f"{destination_path}/{suffix}"] = self.files.pop(item)
            for item in symlink_nodes:
                suffix = item[len(source_path) :].lstrip("/")
                self.symlinks.remove(item)
                self.symlinks.add(f"{destination_path}/{suffix}")
        self.mutations += 1

    def remove_tree(self, remote_path: str) -> None:
        """Remove one exact node and descendants without following symlinks."""

        path = self._normalize(remote_path)
        if self.fail_cleanup_once and ".git-deploy/backup/" in path:
            self.fail_cleanup_once = False
            raise OSError("synthetic cleanup failure")
        changed = False
        for collection in (self.files, self.symlinks, self.directories):
            for item in tuple(collection):
                if item == path or item.startswith(path + "/"):
                    if isinstance(collection, dict):
                        collection.pop(item, None)
                    else:
                        collection.discard(item)
                    changed = True
        if changed:
            self.mutations += 1

    def add_file(self, path: str, data: bytes) -> None:
        """Seed one out-of-band remote file for a test."""

        normalized = self._normalize(path)
        self.make_directory(self._parent(normalized))
        self.files[normalized] = data
        self.mutations = 0

    @staticmethod
    def _normalize(path: str) -> str:
        """Normalize a safe relative path while representing root as empty."""

        value = PurePosixPath(path).as_posix().strip("/")
        return "" if value == "." else value

    @staticmethod
    def _parent(path: str) -> str:
        """Return the normalized direct parent of one normalized path."""

        value = PurePosixPath(path).parent.as_posix()
        return "" if value == "." else value


def _hybrid_project(root: Path, *, commands: tuple[str, ...] = ()) -> Path:
    """Create an ignored Local Aggregation Root and Hybrid configuration."""

    info_exclude = root / ".git/info/exclude"
    info_exclude.write_text(
        info_exclude.read_text(encoding="utf-8") + ".deploy/\n",
        encoding="utf-8",
    )
    aggregation = root / ".deploy/frontend-root"
    (aggregation / "assets").mkdir(parents=True, exist_ok=True)
    (aggregation / "index.html").write_text("current index\n", encoding="utf-8")
    (aggregation / "assets/app.js").write_text("current app\n", encoding="utf-8")
    command_text = ", ".join(f'"{item}"' for item in commands)
    command_config = f"after_deploy = [{command_text}]\n" if commands else ""
    return write_config(
        root,
        f'''
project_id = "github.com/acme/hybrid-app"
default_target = "dev"

[source]
include = ["**"]

[build]
steps = []

[[outputs]]
name = "frontend-root"
local = ".deploy/frontend-root"
remote = "."
mode = "hybrid"

[targets.dev]
protocol = "sftp"
host = "example.invalid"
username = "deploy"
remote_root = "/srv/app"
{command_config}
''',
    )


def _enable_workspace_hybrid(root: Path, name: str) -> None:
    """Add one ignored Hybrid Output to a workspace test repository."""

    info_exclude = root / ".git/info/exclude"
    info_exclude.write_text(
        info_exclude.read_text(encoding="utf-8") + ".deploy/\n",
        encoding="utf-8",
    )
    hybrid = root / ".deploy/frontend-root/assets"
    hybrid.mkdir(parents=True)
    (hybrid / "app.js").write_text(name, encoding="utf-8")
    config = root / "deploy.toml"
    text = config.read_text(encoding="utf-8")
    text = f'project_id = "github.com/acme/{name}"\n' + text
    block = '''
[[outputs]]
name = "frontend-root"
local = ".deploy/frontend-root"
remote = "."
mode = "hybrid"

'''
    config.write_text(text.replace("[targets.dev]", block + "[targets.dev]"), encoding="utf-8")


def test_hybrid_config_normalizes_project_id_and_preserves_incremental_compatibility(
    git_project: Path,
) -> None:
    """Hybrid fields are strict while an old output remains Incremental."""

    path = _hybrid_project(git_project)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'project_id = "github.com/acme/hybrid-app"',
            'project_id = "https://user:token@GitHub.com/acme/hybrid-app.git"',
        ),
        encoding="utf-8",
    )
    config = load_config(path)

    assert config.project_id == "github.com/acme/hybrid-app"
    assert config.outputs[0].name == "frontend-root"
    assert config.outputs[0].mode == "hybrid"
    assert "token" not in repr(config)

    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'project_id = "https://user:token@GitHub.com/acme/hybrid-app.git"',
            'project_id = "user:private-token@github.com/acme/hybrid-app"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert "private-token" not in str(error.value)

    old_path = write_config(git_project)
    old_config = load_config(old_path)
    assert old_config.outputs[0].mode == "incremental"
    assert old_config.outputs[0].name is None


def test_hybrid_project_id_defaults_to_origin_and_is_required_when_unavailable(
    git_project: Path,
) -> None:
    """A credential-free Git Origin may supply identity; absence fails closed."""

    config_path = _hybrid_project(git_project)
    text = config_path.read_text(encoding="utf-8").replace(
        'project_id = "github.com/acme/hybrid-app"\n', ""
    )
    config_path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="requires project_id"):
        load_config(config_path)

    import subprocess

    subprocess.run(
        ["git", "remote", "add", "origin", "git@GitHub.com:acme/from-origin.git"],
        cwd=git_project,
        check=True,
    )
    assert load_config(config_path).project_id == "github.com/acme/from-origin"


def test_hybrid_rejects_ftp_multiple_mappings_and_protected_or_overlapping_paths(
    git_project: Path,
) -> None:
    """Config and local planning enforce the single SFTP ownership boundary."""

    config_path = _hybrid_project(git_project)
    ftp_text = config_path.read_text(encoding="utf-8").replace(
        'protocol = "sftp"\nhost = "example.invalid"\nusername = "deploy"',
        'protocol = "ftp"\nhost = "example.invalid"\nusername = "deploy"\npassword_env = "FTP_PASS"',
    )
    config_path.write_text(ftp_text, encoding="utf-8")
    with pytest.raises(ConfigError, match="SFTP-only"):
        load_config(config_path)

    config_path = _hybrid_project(git_project)
    duplicate = '''

[[outputs]]
name = "other"
local = ".deploy/frontend-root"
remote = "."
mode = "hybrid"
'''
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("[targets.dev]", duplicate + "\n[targets.dev]"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="at most one hybrid"):
        load_config(config_path)

    config_path = _hybrid_project(git_project)
    aggregation = git_project / ".deploy/frontend-root"
    (aggregation / ".git-deploy").mkdir()
    with pytest.raises(PlanError, match="unsafe direct child"):
        prepare_project("project", config_path, None, full=False, skip_build=True)
    (aggregation / ".git-deploy").rmdir()

    (aggregation / "app.py").write_text("collision\n", encoding="utf-8")
    with pytest.raises(PlanError, match="source/hybrid ownership conflict"):
        prepare_project("project", config_path, None, full=False, skip_build=True)


def test_hybrid_ignore_warning_and_clean_mode_rejection(
    git_project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unignored aggregation root warns normally and blocks clean mode."""

    config_path = _hybrid_project(git_project)
    info_exclude = git_project / ".git/info/exclude"
    info_exclude.write_text(
        info_exclude.read_text(encoding="utf-8").replace(".deploy/\n", ""),
        encoding="utf-8",
    )
    prepared = prepare_project("project", config_path, None, full=False, skip_build=True)
    assert "add '.deploy/'" in capsys.readouterr().out
    prepared.close()

    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'include = ["**"]',
            'include = ["**"]\nrequire_clean_worktree = true',
        ),
        encoding="utf-8",
    )
    with pytest.raises(PlanError, match="add '.deploy/'"):
        prepare_project("project", config_path, None, full=False, skip_build=True)


@pytest.mark.parametrize(
    "replacement, message",
    [
        ('remote = "public"', "remote must be '.'"),
        ('remote = "."\ndelete_removed = true', "delete_removed"),
        ('name = "frontend-root"\n', "name is required"),
    ],
)
def test_hybrid_config_rejects_unsafe_variants(
    git_project: Path,
    replacement: str,
    message: str,
) -> None:
    """Hybrid config fails closed for unsafe ownership semantics."""

    path = _hybrid_project(git_project)
    text = path.read_text(encoding="utf-8")
    if replacement.startswith("name"):
        text = text.replace('name = "frontend-root"\n', "")
    else:
        text = text.replace('remote = "."', replacement)
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_hybrid_scanner_tracks_empty_directories_and_rejects_symlinks(
    git_project: Path,
) -> None:
    """Local scan is stable, hashes files, retains empties, and never follows links."""

    config = load_config(_hybrid_project(git_project))
    empty = git_project / ".deploy/frontend-root/fonts"
    empty.mkdir()
    manifest = scan_hybrid_output(config.outputs[0])

    assert manifest.root_file_names == ("index.html",)
    assert manifest.directory_names == ("assets", "fonts")
    assert manifest.directories[0].file_count == 1
    assert manifest.directories[1].file_count == 0

    (git_project / ".deploy/frontend-root/escape").symlink_to(git_project / "app.py")
    with pytest.raises(PlanError, match="does not support symlinks"):
        scan_hybrid_output(config.outputs[0])


def test_hybrid_rejects_project_root_aliases_and_reserved_direct_children(
    git_project: Path,
) -> None:
    """Core config and scanner both reject project/repository control roots."""

    config_path = _hybrid_project(git_project)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'local = ".deploy/frontend-root"', 'local = "."'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must not be the project root"):
        load_config(config_path)

    alias = git_project / "project-root-alias"
    alias.symlink_to(git_project, target_is_directory=True)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'local = "."', 'local = "project-root-alias"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must not be the project root"):
        load_config(config_path)

    config_path = _hybrid_project(git_project)
    output = load_config(config_path).outputs[0]
    for name in (".git", ".deploy", ".git-deploy"):
        child = output.local / name
        child.mkdir()
        with pytest.raises(PlanError, match="unsafe direct child"):
            scan_hybrid_output(output)
        child.rmdir()


@pytest.mark.parametrize("name", [" leading", "trailing ", "tab\tname", "thin\u2009space"])
def test_hybrid_rejects_unstable_whitespace_components(
    git_project: Path,
    name: str,
) -> None:
    """Local and remote ownership names share one stable SFTP boundary."""

    config = load_config(_hybrid_project(git_project))
    (config.outputs[0].local / name).write_text("unsafe", encoding="utf-8")
    with pytest.raises(PlanError, match="unsafe direct child"):
        scan_hybrid_output(config.outputs[0])

    record = HybridOwnership(
        1,
        "github.com/acme/app",
        "frontend-root",
        ".",
        (),
        (name,),
        "abc123",
        1,
    )
    with pytest.raises(DeployError, match="root_files is invalid"):
        parse_ownership(
            serialize_ownership(record),
            project_id="github.com/acme/app",
            mapping="frontend-root",
            remote=".",
        )


def test_hybrid_mirror_preserves_nested_empty_directories(git_project: Path) -> None:
    """Nested empty directories are explicit manifest entries and Stage output."""

    config_path = _hybrid_project(git_project)
    nested = git_project / ".deploy/frontend-root/assets/empty/nested"
    nested.mkdir(parents=True)
    config = load_config(config_path)
    manifest = scan_hybrid_output(config.outputs[0])
    assets = next(item for item in manifest.directories if item.name == "assets")
    assert assets.directories == ("empty", "empty/nested")

    remote = MemoryHybridTransport()
    prepared = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(prepared, allow_recovery=False, transport_factory=lambda target: remote)
    execute_prepared(prepared)
    assert remote.lstat("assets/empty/nested") is RemotePathType.DIRECTORY


def test_remote_plan_reads_ownership_without_any_remote_mutation(git_project: Path) -> None:
    """Remote Plan completes Adoption/Delete facts but remains strictly read-only."""

    config_path = _hybrid_project(git_project)
    remote = MemoryHybridTransport()
    prepared = prepare_project("project", config_path, None, full=False, skip_build=True)

    prepare_remote_plan(
        prepared,
        allow_recovery=False,
        transport_factory=lambda target: remote,
    )

    assert prepared.plan.hybrid is not None
    assert prepared.plan.hybrid.remote_complete
    assert remote.connects == 1
    assert remote.mutations == 0
    assert "OWNERSHIP UPDATE" in render_plan(prepared.plan)
    prepared.close()


@pytest.mark.parametrize("new_type", [RemotePathType.FILE, RemotePathType.DIRECTORY])
def test_freshness_gate_rejects_missing_path_created_after_remote_plan(
    git_project: Path,
    new_type: RemotePathType,
) -> None:
    """A confirmation-window path cannot bypass explicit Adoption."""

    config_path = _hybrid_project(git_project)
    remote = MemoryHybridTransport()
    prepared = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(prepared, allow_recovery=False, transport_factory=lambda target: remote)
    if new_type is RemotePathType.FILE:
        remote.files["index.html"] = b"unknown user content"
    else:
        remote.directories.add("index.html")
        remote.files["index.html/important"] = b"unknown user content"
    remote.mutations = 0

    with pytest.raises(StaleRemotePlanError, match="path type changed"):
        execute_prepared(prepared)

    assert remote.mutations == 0
    assert (
        remote.files.get("index.html") == b"unknown user content"
        or remote.files.get("index.html/important") == b"unknown user content"
    )


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        ("index.html", RemotePathType.DIRECTORY),
        ("assets", RemotePathType.FILE),
    ],
)
def test_freshness_gate_rejects_owned_file_directory_type_changes(
    git_project: Path,
    path: str,
    replacement: RemotePathType,
) -> None:
    """Owned paths must retain the exact type approved in the Remote Plan."""

    config_path = _hybrid_project(git_project)
    remote = MemoryHybridTransport()
    first = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(first, allow_recovery=False, transport_factory=lambda target: remote)
    execute_prepared(first)
    planned = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(planned, allow_recovery=False, transport_factory=lambda target: remote)

    for item in tuple(remote.files):
        if item == path or item.startswith(path + "/"):
            remote.files.pop(item)
    for item in tuple(remote.directories):
        if item == path or item.startswith(path + "/"):
            remote.directories.discard(item)
    if replacement is RemotePathType.FILE:
        remote.files[path] = b"out-of-band replacement"
    else:
        remote.directories.add(path)
        remote.files[f"{path}/out-of-band"] = b"replacement"
    remote.mutations = 0

    with pytest.raises(StaleRemotePlanError, match="path type changed"):
        execute_prepared(planned)
    assert remote.mutations == 0


def test_freshness_gate_rejects_ownership_change_before_any_source_write(
    git_project: Path,
) -> None:
    """Ownership drift blocks ordinary Source operations before their first write."""

    config_path = _hybrid_project(git_project)
    remote = MemoryHybridTransport()
    first = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(first, allow_recovery=False, transport_factory=lambda target: remote)
    execute_prepared(first)
    old_source = remote.files["app.py"]
    (git_project / "app.py").write_text("print('new source')\n", encoding="utf-8")
    commit_all(git_project, "change source")
    planned = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(planned, allow_recovery=False, transport_factory=lambda target: remote)
    ownership_path = ".git-deploy/hybrid/frontend-root.json"
    ownership = read_ownership(
        remote,
        project_id="github.com/acme/hybrid-app",
        mapping="frontend-root",
        remote=".",
    )
    assert ownership is not None
    remote.files[ownership_path] = serialize_ownership(
        replace(ownership, updated_at=ownership.updated_at + 1)
    )
    remote.mutations = 0

    with pytest.raises(StaleRemotePlanError, match="ownership changed"):
        execute_prepared(planned)

    assert remote.mutations == 0
    assert remote.files["app.py"] == old_source


def test_hybrid_upload_bytes_remain_frozen_across_remote_preflight(
    git_project: Path,
) -> None:
    """Confirmation-window aggregation changes cannot alter reviewed upload bytes."""

    config_path = _hybrid_project(git_project)
    remote = MemoryHybridTransport()
    prepared = prepare_project("project", config_path, None, full=False, skip_build=True)
    (git_project / ".deploy/frontend-root/assets/app.js").write_text(
        "changed after local plan\n", encoding="utf-8"
    )
    prepare_remote_plan(prepared, allow_recovery=True, transport_factory=lambda target: remote)
    execute_prepared(prepared)

    assert remote.files["assets/app.js"] == b"current app\n"


def test_first_adoption_requires_explicit_full_and_only_adopts_current_names(
    git_project: Path,
) -> None:
    """Existing same-name paths require --full; unrelated remote content is ignored."""

    config_path = _hybrid_project(git_project)
    remote = MemoryHybridTransport()
    remote.add_file("assets/legacy.js", b"legacy")
    remote.add_file("manual-backup/snapshot", b"unknown")
    prepared = prepare_project("project", config_path, None, full=False, skip_build=True)

    with pytest.raises(PlanError, match="rerun with --full"):
        prepare_remote_plan(
            prepared,
            allow_recovery=True,
            transport_factory=lambda target: remote,
        )
    prepared.close()

    adopted = prepare_project("project", config_path, None, full=True, skip_build=True)
    prepare_remote_plan(
        adopted,
        allow_recovery=True,
        transport_factory=lambda target: remote,
    )
    assert adopted.plan.adoption_count == 1
    assert any(
        isinstance(item, HybridAdoption) and item.path == "assets"
        for item in adopted.plan.hybrid.operations  # type: ignore[union-attr]
    )
    assert "manual-backup" not in render_plan(adopted.plan)
    adopted.close()


def test_hybrid_deploy_preserves_unknown_content_and_state_loss_still_deletes_owned(
    git_project: Path,
) -> None:
    """Remote Ownership, not local State, drives safe file/directory deletion."""

    config_path = _hybrid_project(git_project)
    remote = MemoryHybridTransport()
    remote.add_file("index.php", b"backend")
    remote.add_file(".env", b"secret")
    remote.add_file("manual-backup/snapshot", b"unknown")
    first = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(first, allow_recovery=True, transport_factory=lambda target: remote)
    execute_prepared(first)

    ownership = read_ownership(
        remote,
        project_id="github.com/acme/hybrid-app",
        mapping="frontend-root",
        remote=".",
    )
    assert ownership is not None
    assert ownership.directories == ("assets",)
    assert ownership.root_files == ("index.html",)
    assert remote.files["index.php"] == b"backend"
    assert remote.files[".env"] == b"secret"
    assert remote.files["manual-backup/snapshot"] == b"unknown"

    state_store = StateStore(GitRepository(git_project).common_dir())
    state_store.path_for("dev").unlink()
    remote.add_file("assets/old.js", b"remote orphan")
    (git_project / ".deploy/frontend-root/index.html").unlink()
    (git_project / ".deploy/frontend-root/assets").rename(
        git_project / ".deploy/frontend-root/new-assets"
    )
    (git_project / ".deploy/frontend-root/index10.css").write_text(
        "new css\n", encoding="utf-8"
    )
    second = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(second, allow_recovery=True, transport_factory=lambda target: remote)
    assert any(
        isinstance(item, HybridRootFileDelete) and item.path == "index.html"
        for item in second.plan.hybrid.operations  # type: ignore[union-attr]
    )
    assert any(
        isinstance(item, HybridDirectoryDelete) and item.name == "assets"
        for item in second.plan.hybrid.operations  # type: ignore[union-attr]
    )
    execute_prepared(second)

    assert "index.html" not in remote.files
    assert not any(path == "assets" or path.startswith("assets/") for path in remote.directories)
    assert remote.files["new-assets/app.js"] == b"current app\n"
    assert remote.files["index10.css"] == b"new css\n"
    assert remote.files["index.php"] == b"backend"
    assert remote.files[".env"] == b"secret"
    assert remote.files["manual-backup/snapshot"] == b"unknown"
    assert state_store.load("dev") is not None


def test_swap_failure_keeps_state_and_next_preflight_restores_then_converges(
    git_project: Path,
) -> None:
    """A failed directory publish leaves Recovery that restores old content on rerun."""

    config_path = _hybrid_project(git_project)
    remote = MemoryHybridTransport()
    first = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(first, allow_recovery=True, transport_factory=lambda target: remote)
    execute_prepared(first)
    store = StateStore(GitRepository(git_project).common_dir())
    old_state = store.load("dev")
    old_bytes = remote.files["assets/app.js"]

    (git_project / ".deploy/frontend-root/assets/app.js").write_text(
        "next app\n", encoding="utf-8"
    )
    remote.fail_stage_publish_once = True
    failed = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(failed, allow_recovery=True, transport_factory=lambda target: remote)
    with pytest.raises(DeployError, match="stage publish failure"):
        execute_prepared(failed)

    assert store.load("dev") == old_state
    assert any(path.startswith(".git-deploy/recovery/") for path in remote.files)
    read_only = prepare_project("project", config_path, None, full=False, skip_build=True)
    mutations = remote.mutations
    prepare_remote_plan(
        read_only,
        allow_recovery=False,
        transport_factory=lambda target: remote,
    )
    assert "RECOVER [swapping]" in render_plan(read_only.plan)
    assert remote.mutations == mutations
    read_only.close()

    recovery = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(recovery, allow_recovery=False, transport_factory=lambda target: remote)
    execute_prepared(recovery, recover_only=True)
    assert remote.files["assets/app.js"] == old_bytes
    assert not any(path.startswith(".git-deploy/recovery/") for path in remote.files)

    resumed = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(resumed, allow_recovery=False, transport_factory=lambda target: remote)
    execute_prepared(resumed)
    assert remote.files["assets/app.js"] == b"next app\n"
    assert not any(path.startswith(".git-deploy/recovery/") for path in remote.files)


def test_stage_failure_keeps_online_paths_and_recovery_rerun_converges(
    git_project: Path,
) -> None:
    """A failed Stage upload never replaces online content or advances ownership/state."""

    config_path = _hybrid_project(git_project)
    remote = MemoryHybridTransport()
    remote.add_file("index.php", b"backend")
    remote.fail_stage_upload = True
    failed = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(failed, allow_recovery=True, transport_factory=lambda target: remote)
    with pytest.raises(DeployError, match="stage upload failure"):
        execute_prepared(failed)
    store = StateStore(GitRepository(git_project).common_dir())

    assert store.load("dev") is None
    assert remote.files["index.php"] == b"backend"
    assert "index.html" not in remote.files
    assert read_ownership(
        remote,
        project_id="github.com/acme/hybrid-app",
        mapping="frontend-root",
        remote=".",
    ) is None

    remote.fail_stage_upload = False
    recovery = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(recovery, allow_recovery=False, transport_factory=lambda target: remote)
    execute_prepared(recovery, recover_only=True)
    resumed = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(resumed, allow_recovery=False, transport_factory=lambda target: remote)
    execute_prepared(resumed)
    assert remote.files["assets/app.js"] == b"current app\n"
    assert remote.files["index.php"] == b"backend"


def test_ownership_write_failure_restores_backup_before_next_plan(
    git_project: Path,
) -> None:
    """A swap without Ownership commit is rolled back from Backup on rerun."""

    config_path = _hybrid_project(git_project)
    remote = MemoryHybridTransport()
    first = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(first, allow_recovery=True, transport_factory=lambda target: remote)
    execute_prepared(first)
    store = StateStore(GitRepository(git_project).common_dir())
    old_state = store.load("dev")
    old_bytes = remote.files["assets/app.js"]
    (git_project / ".deploy/frontend-root/assets/app.js").write_text(
        "ownership next\n", encoding="utf-8"
    )
    remote.fail_ownership_write_once = True
    failed = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(failed, allow_recovery=True, transport_factory=lambda target: remote)
    with pytest.raises(DeployError, match="ownership write failure"):
        execute_prepared(failed)

    assert store.load("dev") == old_state
    recovery = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(recovery, allow_recovery=False, transport_factory=lambda target: remote)
    execute_prepared(recovery, recover_only=True)
    assert remote.files["assets/app.js"] == old_bytes
    resumed = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(resumed, allow_recovery=False, transport_factory=lambda target: remote)
    execute_prepared(resumed)
    assert remote.files["assets/app.js"] == b"ownership next\n"


def test_ctrl_c_leaves_recovery_and_next_run_restores_safely(git_project: Path) -> None:
    """BaseException paths retain a durable SWAPPING record instead of losing Backup."""

    config_path = _hybrid_project(git_project)
    remote = MemoryHybridTransport()
    first = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(first, allow_recovery=True, transport_factory=lambda target: remote)
    execute_prepared(first)
    old = remote.files["assets/app.js"]
    (git_project / ".deploy/frontend-root/assets/app.js").write_text(
        "interrupt next\n", encoding="utf-8"
    )
    remote.interrupt_stage_publish_once = True
    interrupted = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(
        interrupted,
        allow_recovery=True,
        transport_factory=lambda target: remote,
    )
    with pytest.raises(KeyboardInterrupt):
        execute_prepared(interrupted)
    assert any(path.startswith(".git-deploy/recovery/") for path in remote.files)

    recovery = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(recovery, allow_recovery=False, transport_factory=lambda target: remote)
    execute_prepared(recovery, recover_only=True)
    assert remote.files["assets/app.js"] == old
    resumed = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(resumed, allow_recovery=False, transport_factory=lambda target: remote)
    execute_prepared(resumed)
    assert remote.files["assets/app.js"] == b"interrupt next\n"


def test_state_save_and_cleanup_failures_preserve_remote_facts_for_rerun(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Post-Ownership failures keep Recovery, repeat safely, and never roll facts back."""

    config_path = _hybrid_project(git_project, commands=("reload-app",))
    remote = MemoryHybridTransport()
    failed = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(failed, allow_recovery=True, transport_factory=lambda target: remote)

    def fail_state(state) -> None:  # noqa: ANN001
        """Simulate a local atomic State failure after commands."""

        raise OSError("synthetic state save failure")

    monkeypatch.setattr(failed.state_store, "save", fail_state)
    with pytest.raises(DeployError, match="state save failure"):
        execute_prepared(failed)
    assert remote.commands == ["reload-app"]
    assert read_ownership(
        remote,
        project_id="github.com/acme/hybrid-app",
        mapping="frontend-root",
        remote=".",
    ) is not None
    assert any(path.startswith(".git-deploy/recovery/") for path in remote.files)

    resumed = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(resumed, allow_recovery=False, transport_factory=lambda target: remote)
    remote.fail_cleanup_once = True
    execute_prepared(resumed, recover_only=True)
    assert remote.commands == ["reload-app"]
    assert "cleanup is pending" in capsys.readouterr().err
    assert any(path.startswith(".git-deploy/recovery/") for path in remote.files)

    cleanup = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(cleanup, allow_recovery=False, transport_factory=lambda target: remote)
    execute_prepared(cleanup, recover_only=True)
    assert remote.commands == ["reload-app"]
    assert not any(path.startswith(".git-deploy/recovery/") for path in remote.files)


def test_command_failure_commits_ownership_not_local_state_and_rerun_repeats_command(
    git_project: Path,
) -> None:
    """Hybrid preserves the v1.3 at-least-once command and delayed-State boundary."""

    config_path = _hybrid_project(git_project, commands=("reload-app",))
    remote = MemoryHybridTransport()
    remote.fail_command = True
    failed = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(failed, allow_recovery=True, transport_factory=lambda target: remote)
    with pytest.raises(DeployError, match="synthetic command failure"):
        execute_prepared(failed)

    store = StateStore(GitRepository(git_project).common_dir())
    assert store.load("dev") is None
    assert read_ownership(
        remote,
        project_id="github.com/acme/hybrid-app",
        mapping="frontend-root",
        remote=".",
    ) is not None
    assert remote.commands == ["reload-app"]

    remote.fail_command = False
    resumed = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(resumed, allow_recovery=False, transport_factory=lambda target: remote)
    assert "RESUME COMMANDS" in render_plan(resumed.plan)
    execute_prepared(resumed, recover_only=True)
    assert remote.commands == ["reload-app", "reload-app"]
    assert store.load("dev") is not None


def test_recovery_rejects_after_deploy_configuration_drift(git_project: Path) -> None:
    """A committed recovery cannot silently execute commands from a newer config."""

    config_path = _hybrid_project(git_project, commands=("reload-app",))
    remote = MemoryHybridTransport()
    remote.fail_command = True
    failed = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(failed, allow_recovery=False, transport_factory=lambda target: remote)
    with pytest.raises(DeployError, match="synthetic command failure"):
        execute_prepared(failed)

    changed_path = _hybrid_project(git_project, commands=("restart-other",))
    changed = prepare_project(
        "project", changed_path, None, full=False, skip_build=True
    )
    mutations = remote.mutations
    with pytest.raises(PlanError, match="commands or timeout changed"):
        prepare_remote_plan(
            changed,
            allow_recovery=False,
            transport_factory=lambda target: remote,
        )

    assert remote.commands == ["reload-app"]
    assert remote.mutations == mutations


@pytest.mark.parametrize("owned_kind", ["root-file", "directory"])
def test_delete_only_command_failure_is_resumed_by_explicit_recovery(
    git_project: Path,
    owned_kind: str,
) -> None:
    """Deleting the last owned item cannot lose a failed after-deploy command."""

    config_path = _hybrid_project(git_project, commands=("reload-app",))
    root = git_project / ".deploy/frontend-root"
    if owned_kind == "root-file":
        shutil.rmtree(root / "assets")
    else:
        (root / "index.html").unlink()
    remote = MemoryHybridTransport()
    first = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(first, allow_recovery=False, transport_factory=lambda target: remote)
    execute_prepared(first)

    if owned_kind == "root-file":
        (root / "index.html").unlink()
    else:
        shutil.rmtree(root / "assets")
    remote.fail_command = True
    failed = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(failed, allow_recovery=False, transport_factory=lambda target: remote)
    with pytest.raises(DeployError, match="synthetic command failure"):
        execute_prepared(failed)
    assert remote.commands == ["reload-app", "reload-app"]

    remote.fail_command = False
    recovery = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(recovery, allow_recovery=False, transport_factory=lambda target: remote)
    assert "RESUME COMMANDS" in render_plan(recovery.plan)
    execute_prepared(recovery, recover_only=True)

    assert remote.commands == ["reload-app", "reload-app", "reload-app"]
    assert not any(path.startswith(".git-deploy/recovery/") for path in remote.files)
    assert StateStore(GitRepository(git_project).common_dir()).load("dev") is not None


def test_ownership_only_command_failure_remains_pending(git_project: Path) -> None:
    """Recovery records the interrupted commit even when local HEAD has advanced."""

    config_path = _hybrid_project(git_project, commands=("reload-app",))
    shutil.rmtree(git_project / ".deploy/frontend-root/assets")
    remote = MemoryHybridTransport()
    first = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(first, allow_recovery=False, transport_factory=lambda target: remote)
    execute_prepared(first)
    (git_project / "app.py").write_text("print('ownership only')\n", encoding="utf-8")
    commit_all(git_project, "ownership-only source")
    remote.fail_command = True
    failed = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(failed, allow_recovery=False, transport_factory=lambda target: remote)
    with pytest.raises(DeployError, match="synthetic command failure"):
        execute_prepared(failed)
    interrupted_commit = GitRepository(git_project).head()
    (git_project / "app.py").write_text("print('later local head')\n", encoding="utf-8")
    commit_all(git_project, "later local source")
    assert GitRepository(git_project).head() != interrupted_commit

    remote.fail_command = False
    recovery = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(recovery, allow_recovery=False, transport_factory=lambda target: remote)
    execute_prepared(recovery, recover_only=True)
    assert remote.commands == ["reload-app", "reload-app", "reload-app"]
    state = StateStore(GitRepository(git_project).common_dir()).load("dev")
    assert state is not None
    assert state.last_commit == interrupted_commit


def test_recovery_missing_required_backup_fails_closed_and_doctor_reports_manual(
    git_project: Path,
) -> None:
    """Missing old-path Backup preserves every recovery artifact for inspection."""

    config_path = _hybrid_project(git_project)
    remote = MemoryHybridTransport()
    first = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(first, allow_recovery=False, transport_factory=lambda target: remote)
    execute_prepared(first)
    (git_project / ".deploy/frontend-root/assets/app.js").write_text(
        "next\n", encoding="utf-8"
    )
    remote.fail_stage_publish_once = True
    failed = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(failed, allow_recovery=False, transport_factory=lambda target: remote)
    with pytest.raises(DeployError, match="stage publish failure"):
        execute_prepared(failed)
    backup = next(path for path in remote.directories if ".git-deploy/backup/" in path and path.endswith("/assets"))
    for collection in (remote.files, remote.directories):
        for path in tuple(collection):
            if path == backup or path.startswith(backup + "/"):
                if isinstance(collection, dict):
                    collection.pop(path)
                else:
                    collection.discard(path)
    remote.mutations = 0

    recovery = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(recovery, allow_recovery=False, transport_factory=lambda target: remote)
    with pytest.raises(DeployError, match="manual inspection required"):
        execute_prepared(recovery, recover_only=True)
    assert remote.mutations == 0
    assert any(path.startswith(".git-deploy/recovery/") for path in remote.files)
    assert any(path.startswith(".git-deploy/stage/") for path in remote.directories)

    config = load_config(config_path)
    results = run_doctor(
        config,
        replace(config.target(None), ssh_resolved=True),
        GitRepository(git_project),
        StateStore(GitRepository(git_project).common_dir()),
        transport_factory=lambda target: remote,
        pre_resolved_target=replace(config.target(None), ssh_resolved=True),
    )
    recovery_result = next(item for item in results if item.name == "hybrid recovery")
    assert not recovery_result.ok
    assert "manual inspection required" in recovery_result.detail


def test_hybrid_owned_path_can_change_between_file_and_directory(git_project: Path) -> None:
    """Type changes use the same Backup boundary without transferring ownership."""

    config_path = _hybrid_project(git_project)
    switch = git_project / ".deploy/frontend-root/switch"
    switch.write_text("file v1", encoding="utf-8")
    remote = MemoryHybridTransport()
    first = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(first, allow_recovery=True, transport_factory=lambda target: remote)
    execute_prepared(first)
    assert remote.lstat("switch") is RemotePathType.FILE

    switch.unlink()
    switch.mkdir()
    (switch / "child.js").write_text("directory v2", encoding="utf-8")
    second = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(second, allow_recovery=True, transport_factory=lambda target: remote)
    execute_prepared(second)
    assert remote.lstat("switch") is RemotePathType.DIRECTORY
    assert remote.files["switch/child.js"] == b"directory v2"

    shutil.rmtree(switch)
    switch.write_text("file v3", encoding="utf-8")
    third = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(third, allow_recovery=True, transport_factory=lambda target: remote)
    execute_prepared(third)
    assert remote.lstat("switch") is RemotePathType.FILE
    assert remote.files["switch"] == b"file v3"


def test_hybrid_noop_skips_unchanged_root_file_but_any_directory_mirrors_each_run(
    git_project: Path,
) -> None:
    """Root-only Hybrid may no-op; an empty Mirror Directory intentionally runs commands."""

    config_path = _hybrid_project(git_project, commands=("reload-app",))
    shutil.rmtree(git_project / ".deploy/frontend-root/assets")
    remote = MemoryHybridTransport()
    first = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(first, allow_recovery=True, transport_factory=lambda target: remote)
    execute_prepared(first)
    assert remote.commands == ["reload-app"]

    noop = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(noop, allow_recovery=True, transport_factory=lambda target: remote)
    assert not noop.plan.has_remote_work
    execute_prepared(noop)
    assert remote.commands == ["reload-app"]

    (git_project / ".deploy/frontend-root/fonts").mkdir()
    mirror = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(mirror, allow_recovery=True, transport_factory=lambda target: remote)
    assert mirror.plan.has_remote_work
    execute_prepared(mirror)
    assert remote.lstat("fonts") is RemotePathType.DIRECTORY
    assert remote.commands == ["reload-app", "reload-app"]


def test_direct_execute_plan_api_does_not_skip_hybrid_only_work(
    git_project: Path,
) -> None:
    """The lower-level execution API freezes and deploys a Hybrid-only plan."""

    config = load_config(_hybrid_project(git_project))
    repository = GitRepository(git_project)
    store = StateStore(repository.common_dir())
    plan = create_plan(config, config.target(None), repository, None, full=False)
    remote = MemoryHybridTransport()

    execute_plan(
        plan,
        config,
        repository,
        store,
        transport_factory=lambda target: remote,
    )

    assert remote.files["assets/app.js"] == b"current app\n"
    assert store.load("dev") is not None


def test_remote_ownership_rejects_symlink_corruption_identity_and_oversize() -> None:
    """Untrusted ownership bytes and path types fail closed."""

    record = HybridOwnership(
        1,
        "github.com/acme/app",
        "frontend-root",
        ".",
        ("assets",),
        ("index.html",),
        "abc123",
        1,
    )
    from git_deploy.hybrid import serialize_ownership

    data = serialize_ownership(record)
    assert parse_ownership(
        data,
        project_id="github.com/acme/app",
        mapping="frontend-root",
        remote=".",
    ) == record
    with pytest.raises(DeployError, match="project_id mismatch"):
        parse_ownership(
            data,
            project_id="github.com/other/app",
            mapping="frontend-root",
            remote=".",
        )
    with pytest.raises(DeployError, match="size limit"):
        parse_ownership(
            b"x" * (MAX_REMOTE_RECORD_BYTES + 1),
            project_id="github.com/acme/app",
            mapping="frontend-root",
            remote=".",
        )
    remote = MemoryHybridTransport()
    remote.make_directory(".git-deploy/hybrid")
    remote.symlinks.add(".git-deploy/hybrid/frontend-root.json")
    with pytest.raises(DeployError, match="regular file"):
        read_ownership(
            remote,
            project_id="github.com/acme/app",
            mapping="frontend-root",
            remote=".",
        )


def test_cli_remote_plan_is_read_only_and_dry_run_never_connects(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The two planning modes preserve their distinct zero-write contracts."""

    config_path = _hybrid_project(git_project)
    remote = MemoryHybridTransport()
    monkeypatch.setattr("git_deploy.prepared.create_transport", lambda target, pool=None: remote)

    assert cli.main(["--config", str(config_path), "--skip-build", "--dry-run"]) == 0
    assert remote.connects == 0
    assert "not read" in capsys.readouterr().out

    assert cli.main(["--config", str(config_path), "--skip-build", "--remote-plan"]) == 0
    assert remote.connects == 1
    assert remote.mutations == 0
    assert "no upload, delete, command" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        cli.main(
            [
                "--config",
                str(config_path),
                "--skip-build",
                "--dry-run",
                "--remote-plan",
            ]
        )


def test_cli_adoption_requires_yes_when_input_is_noninteractive(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Even explicit --full Adoption still passes through the normal confirmation gate."""

    config_path = _hybrid_project(git_project)
    remote = MemoryHybridTransport()
    remote.add_file("assets/legacy.js", b"legacy")
    monkeypatch.setattr("git_deploy.prepared.create_transport", lambda target, pool=None: remote)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    assert cli.main(["--config", str(config_path), "--skip-build", "--full"]) == 2
    assert "requires --yes" in capsys.readouterr().err
    assert remote.files["assets/legacy.js"] == b"legacy"

    assert (
        cli.main(["--config", str(config_path), "--skip-build", "--full", "--yes"])
        == 0
    )
    assert remote.files["assets/app.js"] == b"current app\n"


def test_pending_recovery_remote_plan_and_cancel_are_strictly_read_only(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Recovery appears in plans, and declining its confirmation writes nothing."""

    config_path = _hybrid_project(git_project)
    remote = MemoryHybridTransport()
    failed = prepare_project("project", config_path, None, full=False, skip_build=True)
    prepare_remote_plan(failed, allow_recovery=False, transport_factory=lambda target: remote)
    remote.fail_stage_upload = True
    with pytest.raises(DeployError, match="stage upload failure"):
        execute_prepared(failed)
    remote.fail_stage_upload = False
    monkeypatch.setattr("git_deploy.prepared.create_transport", lambda target, pool=None: remote)

    baseline = remote.mutations
    assert cli.main(["--config", str(config_path), "--skip-build", "--remote-plan"]) == 0
    assert "RECOVER [prepared]" in capsys.readouterr().out
    assert remote.mutations == baseline

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert cli.main(["--config", str(config_path), "--skip-build", "--recover"]) == 2
    assert "deployment cancelled" in capsys.readouterr().err
    assert remote.mutations == baseline
    assert any(path.startswith(".git-deploy/recovery/") for path in remote.files)


def test_workspace_hybrid_root_gate_precedes_build_and_combined_plan_shows_remote_facts(
    tmp_path: Path,
) -> None:
    """Same-root Hybrid fails before Build; disjoint roots produce one full combined plan."""

    first = _create_repository(
        tmp_path,
        "api",
        remote_root="/srv/shared",
        build_steps=("printf built > build-marker",),
    )
    second = _create_repository(tmp_path, "web", remote_root="/srv/shared")
    _enable_workspace_hybrid(first, "api")
    _enable_workspace_hybrid(second, "web")
    workspace = load_workspace(_write_workspace(tmp_path, (("api", first), ("web", second))))

    with pytest.raises(ConfigError, match="overlapping remote roots"):
        prepare_workspace(workspace, None, full=False, skip_build=False)
    assert not (first / "build-marker").exists()

    second_config = second / "deploy.toml"
    second_config.write_text(
        second_config.read_text(encoding="utf-8").replace(
            'remote_root = "/srv/shared"', 'remote_root = "/srv/web"'
        ),
        encoding="utf-8",
    )
    _, prepared = prepare_workspace(workspace, None, full=False, skip_build=True)
    remotes = {item.plan.target.remote_root.as_posix(): MemoryHybridTransport() for item in prepared}
    for item in prepared:
        prepare_remote_plan(
            item,
            allow_recovery=True,
            transport_factory=lambda target: remotes[target.remote_root.as_posix()],
        )
    rendered = render_workspace_plan("dev", prepared)
    assert rendered.count("HYBRID Mapping") == 2
    assert rendered.count("OWNERSHIP UPDATE") == 2
    assert "frozen byte(s)" in rendered
    assert all(remote.mutations == 0 for remote in remotes.values())
    for item in prepared:
        item.close()


def test_workspace_hybrid_partial_failure_stops_then_rerun_converges(
    tmp_path: Path,
) -> None:
    """A commits, B remains recoverable, C stays untouched, and a rerun converges."""

    repositories = tuple(
        _create_repository(tmp_path, name, remote_root=f"/srv/{name}")
        for name in ("api", "web", "admin")
    )
    for repository, name in zip(repositories, ("api", "web", "admin"), strict=True):
        _enable_workspace_hybrid(repository, name)
    workspace = load_workspace(
        _write_workspace(
            tmp_path,
            tuple(
                (name, repository)
                for name, repository in zip(
                    ("api", "web", "admin"), repositories, strict=True
                )
            ),
        )
    )
    remotes = {
        f"/srv/{name}": MemoryHybridTransport()
        for name in ("api", "web", "admin")
    }

    _, first = prepare_workspace(workspace, None, full=False, skip_build=True)
    for item in first:
        prepare_remote_plan(
            item,
            allow_recovery=True,
            transport_factory=lambda target: remotes[target.remote_root.as_posix()],
        )
    remotes["/srv/web"].fail_stage_publish_once = True
    with pytest.raises(DeployError, match="stage publish failure"):
        execute_workspace(first)

    assert StateStore(GitRepository(repositories[0]).common_dir()).load("dev") is not None
    assert StateStore(GitRepository(repositories[1]).common_dir()).load("dev") is None
    assert StateStore(GitRepository(repositories[2]).common_dir()).load("dev") is None
    assert remotes["/srv/api"].files["assets/app.js"] == b"api"
    assert any(
        path.startswith(".git-deploy/recovery/")
        for path in remotes["/srv/web"].files
    )
    assert remotes["/srv/admin"].mutations == 0

    _, recovery = prepare_workspace(workspace, None, full=False, skip_build=True)
    for item in recovery:
        prepare_remote_plan(
            item,
            allow_recovery=False,
            transport_factory=lambda target: remotes[target.remote_root.as_posix()],
        )
    assert execute_workspace(recovery, recover_only=True) == ("web",)

    _, resumed = prepare_workspace(workspace, None, full=False, skip_build=True)
    for item in resumed:
        prepare_remote_plan(
            item,
            allow_recovery=False,
            transport_factory=lambda target: remotes[target.remote_root.as_posix()],
        )
    assert execute_workspace(resumed) == ("api", "web", "admin")
    assert remotes["/srv/web"].files["assets/app.js"] == b"web"
    assert remotes["/srv/admin"].files["assets/app.js"] == b"admin"
    assert all(
        StateStore(GitRepository(repository).common_dir()).load("dev") is not None
        for repository in repositories
    )


def test_workspace_freshness_gate_checks_later_repositories_before_any_write(
    tmp_path: Path,
) -> None:
    """A stale later repository prevents all earlier workspace mutations."""

    first = _create_repository(tmp_path, "api", remote_root="/srv/api")
    second = _create_repository(tmp_path, "web", remote_root="/srv/web")
    _enable_workspace_hybrid(first, "api")
    _enable_workspace_hybrid(second, "web")
    workspace = load_workspace(_write_workspace(tmp_path, (("api", first), ("web", second))))
    _, prepared = prepare_workspace(workspace, None, full=False, skip_build=True)
    remotes = {"/srv/api": MemoryHybridTransport(), "/srv/web": MemoryHybridTransport()}
    for item in prepared:
        prepare_remote_plan(
            item,
            allow_recovery=False,
            transport_factory=lambda target: remotes[target.remote_root.as_posix()],
        )
    remotes["/srv/web"].directories.add("assets")
    remotes["/srv/web"].files["assets/out-of-band"] = b"unknown"
    remotes["/srv/web"].mutations = 0

    with pytest.raises(StaleRemotePlanError, match="path type changed"):
        execute_workspace(prepared)

    assert remotes["/srv/api"].mutations == 0
    assert remotes["/srv/web"].mutations == 0
    assert StateStore(GitRepository(first).common_dir()).load("dev") is None
    assert StateStore(GitRepository(second).common_dir()).load("dev") is None


def test_hybrid_doctor_reports_local_remote_recovery_and_adoption_read_only(
    git_project: Path,
) -> None:
    """Doctor exposes every Hybrid diagnostic without changing the remote."""

    config = load_config(_hybrid_project(git_project))
    target = replace(config.target(None), ssh_resolved=True)
    repository = GitRepository(git_project)
    store = StateStore(repository.common_dir())
    remote = MemoryHybridTransport()
    remote.add_file("assets/legacy.js", b"legacy")
    results = run_doctor(
        config,
        target,
        repository,
        store,
        transport_factory=lambda selected: remote,
        pre_resolved_target=target,
    )
    by_name = {item.name: item for item in results}

    assert by_name["hybrid project id"].ok
    assert by_name["hybrid git ignore"].ok
    assert by_name["hybrid local root"].ok
    assert by_name["hybrid internal directory"].ok
    assert by_name["hybrid recovery"].ok
    assert by_name["hybrid ownership"].ok
    assert not by_name["hybrid adoption"].ok
    assert "--full required: assets" in by_name["hybrid adoption"].detail
    assert remote.mutations == 0
