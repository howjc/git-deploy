"""Local-only build orchestration over exact tree, runner, collector, and cache."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from .artifacts import ArtifactCollector
from .build_cache import (
    BuildCache,
    BuildCacheEntry,
    build_fingerprint,
    docker_runner_identity,
)
from .build_runner import HostBuildRunner
from .errors import PolicyError
from .models import ProjectConfig
from .worktree import WorktreeManager


@dataclass(frozen=True)
class BuildOutcome:
    """Local build result ready for artifact planning, with no remote side effects."""

    entry: BuildCacheEntry
    cache_hit: bool
    fingerprint: str
    warning: str


class BuildService:
    """Build/cache artifacts from a real or persistent synthetic source tree."""

    def __init__(self, project: ProjectConfig, target_root: Path):
        """Bind one resolved project and physical target state root."""

        self.project = project
        self.target_root = target_root.resolve()
        self.cache = BuildCache(self.target_root)

    def execute(
        self,
        source_tree_id: str,
        *,
        object_env: dict[str, str] | None = None,
    ) -> BuildOutcome:
        """Materialize, fingerprint, run, collect, and cache without remote access."""

        build = self.project.build
        if build is None:
            raise PolicyError(f"project {self.project.name} has no build configuration")
        manager = WorktreeManager(
            self.project.repository,
            self.target_root / "build" / "worktrees",
        )
        with manager.materialize(source_tree_id, object_env=object_env) as worktree:
            tools = _tool_identities(build.commands)
            locks = _lock_digests(worktree.path)
            runner_identity: dict[str, object]
            docker_runner = None
            docker_image = None
            if build.runner == "docker":
                from .docker_runner import DockerBuildRunner

                docker_runner = DockerBuildRunner()
                docker_image = docker_runner.resolve_image(build)
                runner_identity = docker_runner_identity(build, docker_image.image_id)
            elif build.runner == "host":
                runner_identity = {"backend": "host"}
            else:
                raise PolicyError(f"unsupported build runner: {build.runner}")
            fingerprint = build_fingerprint(
                source_tree_id=source_tree_id,
                build=build,
                artifacts=self.project.artifacts,
                tool_versions=tools,
                lock_digests=locks,
                runner_identity=runner_identity,
            )
            lookup = self.cache.lookup(
                fingerprint,
                secrets_enabled=build.onepassword is not None,
            )
            if lookup.hit and lookup.entry is not None:
                warning = (
                    DockerBuildRunner.daemon_warning
                    if build.runner == "docker" and docker_runner is not None
                    else HostBuildRunner.permission_warning
                )
                return BuildOutcome(
                    lookup.entry,
                    True,
                    fingerprint,
                    warning,
                )
            if build.runner == "host" and build.onepassword is not None:
                from .onepassword_runner import OnePasswordHostRunner

                OnePasswordHostRunner().run(worktree.path, build)
            elif build.runner == "host":
                HostBuildRunner().run(worktree.path, build)
            elif docker_runner is not None:
                if build.onepassword is not None:
                    from .docker_runner import DockerBuildRunner, DockerCli
                    from .onepassword_runner import OnePasswordDockerCli

                    wrapped = OnePasswordDockerCli(DockerCli(), build.onepassword)
                    docker_runner = DockerBuildRunner(cli=wrapped)
                docker_runner.run(worktree.path, build, image=docker_image)
            manifest = ArtifactCollector().collect(worktree.path, self.project.artifacts)
            entry = self.cache.store(
                fingerprint,
                source_tree_id,
                manifest,
                secrets_enabled=build.onepassword is not None,
            )
            if entry is None:  # pragma: no cover - store returns an imminent entry
                raise PolicyError("build cache failed to stage artifact manifest")
            warning = (
                docker_runner.daemon_warning
                if build.runner == "docker" and docker_runner is not None
                else HostBuildRunner.permission_warning
            )
            return BuildOutcome(
                entry,
                False,
                fingerprint,
                warning,
            )


def _tool_identities(commands: tuple[tuple[str, ...], ...]) -> dict[str, str]:
    """Identify command executables by resolved path and stable stat metadata."""

    identities: dict[str, str] = {}
    for command in commands:
        executable = command[0]
        resolved = shutil.which(executable) if not Path(executable).is_absolute() else executable
        if resolved is None:
            identities[executable] = "missing"
            continue
        path = Path(resolved).resolve()
        try:
            info = path.stat()
            identities[executable] = f"{path}:{info.st_size}:{info.st_mtime_ns}"
        except OSError:
            identities[executable] = str(path)
    return identities


def _lock_digests(worktree: Path) -> dict[str, str]:
    """Hash known reproducibility lockfiles present in the exact worktree."""

    locks: dict[str, str] = {}
    for name in ("composer.lock", "package-lock.json", "npm-shrinkwrap.json", "go.sum"):
        path = worktree / name
        if path.is_file() and not path.is_symlink():
            locks[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return locks
