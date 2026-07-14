"""Configuration and target selection service shared by application adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from git_deploy.config import (
    load_config,
    resolve_project_target,
    select_remote,
)
from git_deploy.errors import ConfigurationError
from git_deploy.models import AppConfig, ProjectConfig, ServerConfig
from git_deploy.target_identity import TargetIdentity, policy_fingerprint_for_project

from .policy import EnvironmentRisk


@dataclass(frozen=True, slots=True)
class ProjectSelection:
    """Secret-safe resolved project and physical target summary.

    Args:
        remote_alias: Explicit selected remote alias.
        project: Explicit selected project key.
        endpoint: Canonical protocol/host/port summary without credentials.
        remote_root: Canonical deployment root.
        target_id: Stable physical target identifier.
        physical_fingerprint: Canonical endpoint/project/root fingerprint.
        policy_fingerprint: Managed-state policy fingerprint.
        environment_risk: Explicit configured environment classification.
        uses_secret: Whether the effective build injects configured secrets.
    """

    remote_alias: str
    project: str
    endpoint: str
    remote_root: str
    target_id: str
    physical_fingerprint: str
    policy_fingerprint: str
    environment_risk: EnvironmentRisk
    uses_secret: bool


@dataclass(frozen=True, slots=True)
class SelectionState:
    """Adapter selection state that invalidates review data on target changes."""

    remote_alias: str | None = None
    project: ProjectSelection | None = None
    plan_token: str | None = None
    confirmed: bool = False


class ApplicationConfigService:
    """Resolve configuration without connecting to a remote or writing state."""

    def __init__(self, config: AppConfig):
        """Bind a validated application configuration.

        Args:
            config: Already parsed project configuration.
        """

        if not isinstance(config, AppConfig):
            raise TypeError("config must be an AppConfig")
        self._config = config

    @classmethod
    def from_path(cls, path: Path) -> ApplicationConfigService:
        """Load a TOML file and create its selection service.

        Args:
            path: Existing deployment configuration path.

        Returns:
            Selection service bound to the parsed configuration.
        """

        return cls(load_config(path))

    @property
    def config_path(self) -> Path:
        """Return the resolved configuration source path."""

        return self._config.path

    def available_remotes(self) -> tuple[str, ...]:
        """Return configured remote aliases in declaration order."""

        return tuple(self._config.remotes)

    def available_projects(self, remote: str | None) -> tuple[str, ...]:
        """Return project keys available for one selected remote.

        Args:
            remote: Explicit alias or None for the configured safe default.

        Returns:
            Project keys in declaration order.
        """

        _alias, _server, projects = select_remote(self._config, remote)
        return tuple(projects)

    def resolve_project(
        self,
        remote: str | None,
        project_name: str,
    ) -> ProjectSelection:
        """Resolve a secret-safe project/target/risk summary.

        Args:
            remote: Explicit alias or None for the configured safe default.
            project_name: Exact configured project key.

        Returns:
            Immutable project selection suitable for CLI or TUI rendering.
        """

        alias, server, project, identity = self._resolve_domain_project(
            remote,
            project_name,
        )
        payload = identity.payload
        risk = _environment_risk(server, alias)
        build = project.build
        return ProjectSelection(
            remote_alias=alias,
            project=project.name,
            endpoint=f"{payload.protocol}://{payload.host}:{payload.port}",
            remote_root=payload.remote_root,
            target_id=identity.target_id,
            physical_fingerprint=identity.physical_fingerprint,
            policy_fingerprint=policy_fingerprint_for_project(project),
            environment_risk=risk,
            uses_secret=bool(build is not None and build.onepassword is not None),
        )

    def _resolve_domain_project(
        self,
        remote: str | None,
        project_name: str,
    ) -> tuple[str, ServerConfig, ProjectConfig, TargetIdentity]:
        """Resolve domain configuration for sibling application services.

        Args:
            remote: Explicit alias or None for the configured safe default.
            project_name: Exact configured project key.

        Returns:
            Alias, connection config, effective project config, and identity.

        Notes:
            ``ServerConfig`` can contain credential variable names and must not be
            exposed directly by CLI/TUI renderers; use ``resolve_project`` there.
        """

        alias, server, projects = select_remote(self._config, remote)
        project = _select_project(projects, project_name)
        identity = resolve_project_target(server, project, config=self._config)
        return alias, server, project, identity

    def switch_remote(
        self,
        state: SelectionState,
        remote: str | None,
    ) -> SelectionState:
        """Select a remote and invalidate old project/review state.

        Args:
            state: Current adapter selection state.
            remote: Explicit alias or None for the configured safe default.

        Returns:
            Fresh remote-only state with project, token, and confirmation reset.
        """

        if not isinstance(state, SelectionState):
            raise TypeError("state must be a SelectionState")
        alias, _server, _projects = select_remote(self._config, remote)
        return SelectionState(remote_alias=alias)

    def select_project(
        self,
        state: SelectionState,
        project_name: str,
    ) -> SelectionState:
        """Select a project and clear any prior plan confirmation.

        Args:
            state: State containing a selected remote.
            project_name: Exact configured project key.

        Returns:
            State containing the resolved target with no inherited review data.
        """

        if not isinstance(state, SelectionState):
            raise TypeError("state must be a SelectionState")
        if state.remote_alias is None:
            raise ConfigurationError("select a remote before selecting a project")
        selection = self.resolve_project(state.remote_alias, project_name)
        return SelectionState(remote_alias=state.remote_alias, project=selection)

    def record_confirmation(
        self,
        state: SelectionState,
        *,
        plan_token: str,
    ) -> SelectionState:
        """Attach reviewed plan state until the target selection changes.

        Args:
            state: State containing a resolved project target.
            plan_token: Opaque reviewed operation plan token.

        Returns:
            Confirmed state bound to the current selection.
        """

        if state.project is None:
            raise ConfigurationError("select a project before confirming a plan")
        if not isinstance(plan_token, str) or not plan_token.strip():
            raise ValueError("plan_token must be a non-empty string")
        return SelectionState(
            remote_alias=state.remote_alias,
            project=state.project,
            plan_token=plan_token,
            confirmed=True,
        )


def _select_project(
    projects: dict[str, ProjectConfig],
    project_name: str,
) -> ProjectConfig:
    """Return one exact project or raise a stable configuration error."""

    try:
        return projects[project_name]
    except KeyError as exc:
        available = ", ".join(projects)
        raise ConfigurationError(
            f"unknown project {project_name!r}; available: {available}"
        ) from exc


def _environment_risk(server: ServerConfig, alias: str) -> EnvironmentRisk:
    """Parse an explicit risk field without inspecting alias spelling."""

    value = str(server.values.get("risk", EnvironmentRisk.STANDARD.value)).strip().lower()
    try:
        return EnvironmentRisk(value)
    except ValueError as exc:
        choices = ", ".join(item.value for item in EnvironmentRisk)
        raise ConfigurationError(
            f"remotes.{alias}.risk must be one of: {choices}"
        ) from exc
