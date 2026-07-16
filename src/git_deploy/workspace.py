"""Thin multi-repository orchestration with independent project state and locks."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from git_deploy.builder import run_build
from git_deploy.config import Config, load_config
from git_deploy.deployer import TransportFactory
from git_deploy.doctor import DoctorResult, run_doctor
from git_deploy.errors import ConfigError, PlanError
from git_deploy.git import GitRepository
from git_deploy.manifest import StateStore
from git_deploy.planner import UploadOperation
from git_deploy.prepared import PreparedDeployment, execute_prepared, prepare_project
from git_deploy.transports import create_transport
from git_deploy.transports.openssh_sftp import SSHConnectionPool


@dataclass(frozen=True, slots=True)
class WorkspaceRepository:
    """Name and locate one independent repository in deployment order."""

    name: str
    path: Path
    config_path: Path


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    """Represent the intentionally thin workspace configuration."""

    path: Path
    root: Path
    default_target: str | None
    repositories: tuple[WorkspaceRepository, ...]

    def target(self, requested: str | None) -> str:
        """Return one target name shared by every repository."""

        target = requested or self.default_target
        if not target:
            raise ConfigError("workspace target is required because default_target is not set")
        return target


def load_workspace(path: Path) -> WorkspaceConfig:
    """Load a workspace containing only repository name/path/order."""

    path = path.expanduser().resolve()
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"workspace file not found: {path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read workspace {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("workspace root must be a table")
    unknown = sorted(set(raw) - {"default_target", "repositories"})
    if unknown:
        raise ConfigError(f"unknown workspace field(s): {', '.join(unknown)}")
    default = raw.get("default_target")
    if default is not None and (not isinstance(default, str) or not default.strip()):
        raise ConfigError("workspace default_target must be a non-empty string")
    entries = raw.get("repositories")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("workspace requires at least one [[repositories]] entry")
    root = path.parent.resolve()
    repositories: list[WorkspaceRepository] = []
    names: set[str] = set()
    paths: set[Path] = set()
    for index, value in enumerate(entries):
        if not isinstance(value, dict):
            raise ConfigError(f"repositories[{index}] must be a table")
        extra = sorted(set(value) - {"name", "path"})
        if extra:
            raise ConfigError(
                f"unknown repositories[{index}] field(s): {', '.join(extra)}"
            )
        name = _required_string(value.get("name"), f"repositories[{index}].name")
        path_text = _required_string(value.get("path"), f"repositories[{index}].path")
        if name in names:
            raise ConfigError(f"duplicate workspace repository name: {name}")
        relative = Path(path_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise ConfigError(f"repositories[{index}].path must stay inside the workspace")
        repository_path = (root / relative).resolve()
        if not repository_path.is_relative_to(root):
            raise ConfigError(f"repositories[{index}].path resolves outside the workspace")
        if repository_path in paths:
            raise ConfigError(f"duplicate workspace repository path: {repository_path}")
        if not repository_path.is_dir():
            raise ConfigError(f"workspace repository directory not found: {repository_path}")
        config_path = repository_path / "deploy.toml"
        if not config_path.is_file():
            raise ConfigError(f"repository {name!r} is missing deploy.toml: {config_path}")
        names.add(name)
        paths.add(repository_path)
        repositories.append(WorkspaceRepository(name, repository_path, config_path))
    return WorkspaceConfig(path, root, default.strip() if isinstance(default, str) else None, tuple(repositories))


def prepare_workspace(
    workspace: WorkspaceConfig,
    requested_target: str | None,
    *,
    full: bool,
    skip_build: bool,
) -> tuple[str, tuple[PreparedDeployment, ...]]:
    """Prepare every repository before any remote connection can occur."""

    target = workspace.target(requested_target)
    prepared: list[PreparedDeployment] = []
    try:
        # Load and validate the shared target name for every repository before
        # the first expensive build or lock acquisition.
        for repository in workspace.repositories:
            load_config(repository.config_path).target(target)
        for repository in workspace.repositories:
            print(f"Preparing {repository.name}...")
            prepared.append(
                prepare_project(
                    repository.name,
                    repository.config_path,
                    target,
                    full=full,
                    skip_build=skip_build,
                )
            )
        return target, tuple(prepared)
    except BaseException:
        for item in reversed(prepared):
            item.close()
        raise


def render_workspace_plan(target: str, prepared: tuple[PreparedDeployment, ...]) -> str:
    """Render every repository operation and one deterministic total summary."""

    lines = [f"Workspace Target: {target}"]
    uploads = 0
    deletes = 0
    for item in prepared:
        lines.append("")
        lines.append(f"[{item.name}]")
        if not item.plan.operations:
            lines.append("  No changes")
        else:
            for operation in item.plan.operations:
                action = "UPLOAD" if isinstance(operation, UploadOperation) else "DELETE"
                lines.append(f"  {action:6} [{operation.origin}] {operation.remote_path}")
        lines.append(
            f"  Summary: {item.plan.upload_count} upload(s), {item.plan.delete_count} delete(s)"
        )
        uploads += item.plan.upload_count
        deletes += item.plan.delete_count
    lines.extend(("", f"Total: {uploads} upload(s), {deletes} delete(s)"))
    return "\n".join(lines)


def execute_workspace(
    prepared: tuple[PreparedDeployment, ...],
    *,
    verbose: bool = False,
    transport_factory: TransportFactory | None = None,
) -> tuple[str, ...]:
    """Deploy repositories sequentially, sharing Native OpenSSH connections."""

    pool = SSHConnectionPool()
    completed: list[str] = []
    try:
        for item in prepared:
            print(f"Deploying {item.name}...")
            execute_prepared(
                item,
                verbose=verbose,
                transport_factory=transport_factory,
                connection_pool=pool,
            )
            completed.append(item.name)
        return tuple(completed)
    finally:
        for item in prepared:
            item.close()
        pool.close_all()


def run_workspace_build(workspace: WorkspaceConfig, requested_target: str | None) -> None:
    """Validate all repository targets, then run every build sequentially."""

    target = workspace.target(requested_target)
    configs = tuple(load_config(item.config_path) for item in workspace.repositories)
    for config in configs:
        config.target(target)
    for repository, config in zip(workspace.repositories, configs, strict=True):
        print(f"Building {repository.name}...")
        run_build(config.build, config.project_root)


def run_workspace_doctor(
    workspace: WorkspaceConfig,
    requested_target: str | None,
    *,
    create_root: bool,
) -> tuple[tuple[str, tuple[DoctorResult, ...]], ...]:
    """Run per-repository diagnostics with one shared Native OpenSSH pool."""

    target_name = workspace.target(requested_target)
    loaded: list[tuple[WorkspaceRepository, Config, GitRepository, StateStore]] = []
    for item in workspace.repositories:
        config = load_config(item.config_path)
        config.target(target_name)
        repository = GitRepository(config.project_root)
        try:
            common = repository.common_dir()
        except PlanError:
            common = config.project_root / ".git"
        loaded.append((item, config, repository, StateStore(common)))
    pool = SSHConnectionPool()
    results: list[tuple[str, tuple[DoctorResult, ...]]] = []
    try:
        for item, config, repository, store in loaded:
            target = config.target(target_name)
            checks = run_doctor(
                config,
                target,
                repository,
                store,
                create_root=create_root,
                transport_factory=lambda selected: create_transport(selected, pool),
            )
            results.append((item.name, checks))
        return tuple(results)
    finally:
        pool.close_all()


def _required_string(value: Any, name: str) -> str:
    """Require a non-empty workspace string."""

    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()
