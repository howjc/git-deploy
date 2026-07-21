"""FTP Hybrid remote runtime bootstrap for Project and Workspace modes.

Bootstrap initializes Capability Profiles and optional missing remote roots.
It never runs Build, business upload, Adoption, Ownership, Pending, or
Deployment State writes.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from git_deploy.config import Config, TargetConfig, load_config, resolve_target_for_plan
from git_deploy.errors import ConfigError, DeployError, GitDeployError, PlanError
from git_deploy.ftp_hybrid import (
    CapabilityProfileStatus,
    inspect_capability_profile,
    probe_and_save_ftp_hybrid_capabilities,
    read_pending,
)
from git_deploy.git import GitRepository
from git_deploy.lock import TargetLock
from git_deploy.manifest import StateStore
from git_deploy.transports import create_transport
from git_deploy.transports.ftp import FTPTransport
from git_deploy.workspace import WorkspaceConfig, load_workspace


class BootstrapAction(str, Enum):
    """Planned action for one repository/target bootstrap item."""

    READY = "ready"
    PROBE = "probe"
    REPROBE = "reprobe"
    CREATE_ROOT_AND_PROBE = "create-root-and-probe"
    SKIP = "skip"
    FAIL_PRECHECK = "fail-precheck"


@dataclass(frozen=True, slots=True)
class BootstrapItem:
    """One repository/target row in the bootstrap plan or result set."""

    repository_name: str
    repository_root: Path
    config_path: Path
    target_name: str
    target: TargetConfig | None
    state_base: Path
    action: BootstrapAction
    reason: str
    endpoint: str = ""
    project_id: str | None = None
    hybrid_mapping: str | None = None


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """Outcome of executing one bootstrap item."""

    item: BootstrapItem
    success: bool
    profile_path: Path | None
    error: str | None
    detail: str = ""


def has_hybrid_output(config: Config) -> bool:
    """Return whether the project configures one Hybrid output.

    Args:
        config: Loaded project configuration.

    Returns:
        ``True`` when exactly one hybrid mapping is present (config enforces ≤1).
    """

    return any(output.mode == "hybrid" for output in config.outputs)


def hybrid_output_name(config: Config) -> str | None:
    """Return the Hybrid mapping name when configured.

    Args:
        config: Loaded project configuration.

    Returns:
        Hybrid output name, or ``None`` when the project has no Hybrid output.
    """

    for output in config.outputs:
        if output.mode == "hybrid":
            return output.name
    return None


def enumerate_project_bootstrap_candidates(
    config: Config,
    *,
    target_filter: frozenset[str] | None = None,
    repository_name: str | None = None,
) -> tuple[BootstrapItem, ...]:
    """Enumerate Project targets and classify non-eligible rows as SKIP.

    Args:
        config: Loaded independent project configuration.
        target_filter: Optional set of target names to keep; others become SKIP.
        repository_name: Display name; defaults to the project directory name.

    Returns:
        Stable-sorted candidates (eligible and skipped) for preflight.
    """

    name = repository_name or config.project_root.name
    repository = GitRepository(config.project_root)
    try:
        git_dir = repository.common_dir()
    except PlanError:
        git_dir = config.project_root / ".git"
    state_base = StateStore(git_dir).base
    hybrid = has_hybrid_output(config)
    mapping = hybrid_output_name(config)
    items: list[BootstrapItem] = []
    for target_name in sorted(config.targets):
        target = config.targets[target_name]
        endpoint = _endpoint_label(target)
        if target_filter is not None and target_name not in target_filter:
            items.append(
                BootstrapItem(
                    name,
                    config.project_root,
                    config.path,
                    target_name,
                    target,
                    state_base,
                    BootstrapAction.SKIP,
                    "filtered",
                    endpoint=endpoint,
                    project_id=config.project_id,
                    hybrid_mapping=mapping,
                )
            )
            continue
        if not hybrid:
            items.append(
                BootstrapItem(
                    name,
                    config.project_root,
                    config.path,
                    target_name,
                    target,
                    state_base,
                    BootstrapAction.SKIP,
                    "no hybrid output",
                    endpoint=endpoint,
                    project_id=config.project_id,
                    hybrid_mapping=mapping,
                )
            )
            continue
        if target.protocol != "ftp":
            items.append(
                BootstrapItem(
                    name,
                    config.project_root,
                    config.path,
                    target_name,
                    target,
                    state_base,
                    BootstrapAction.SKIP,
                    "sftp backend" if target.protocol == "sftp" else f"{target.protocol} backend",
                    endpoint=endpoint,
                    project_id=config.project_id,
                    hybrid_mapping=mapping,
                )
            )
            continue
        items.append(
            BootstrapItem(
                name,
                config.project_root,
                config.path,
                target_name,
                target,
                state_base,
                BootstrapAction.PROBE,
                "candidate",
                endpoint=endpoint,
                project_id=config.project_id,
                hybrid_mapping=mapping,
            )
        )
    return tuple(items)


def enumerate_workspace_bootstrap_candidates(
    workspace: WorkspaceConfig,
    *,
    target_filter: frozenset[str] | None = None,
) -> tuple[BootstrapItem, ...]:
    """Enumerate every workspace repository's targets for FTP Hybrid bootstrap.

    Args:
        workspace: Loaded thin workspace configuration.
        target_filter: Optional target-name filter applied per repository.

    Returns:
        Stable-sorted items across repository order then target name.
    """

    items: list[BootstrapItem] = []
    for repository in workspace.repositories:
        config = load_config(repository.config_path)
        items.extend(
            enumerate_project_bootstrap_candidates(
                config,
                target_filter=target_filter,
                repository_name=repository.name,
            )
        )
    return tuple(items)


def preflight_bootstrap_item(
    item: BootstrapItem,
    *,
    force: bool = False,
    create_root: bool = True,
    transport_factory=None,  # noqa: ANN001
) -> BootstrapItem:
    """Run a read-only preflight and resolve the concrete bootstrap action.

    Args:
        item: Candidate or already-skipped item.
        force: Force REPROBE even when the profile is valid.
        create_root: When False, missing roots become FAIL_PRECHECK.
        transport_factory: Injectable transport factory for tests.

    Returns:
        Updated item with READY/PROBE/REPROBE/CREATE_ROOT_AND_PROBE/FAIL/SKIP.
    """

    factory = transport_factory or create_transport
    if item.action is BootstrapAction.SKIP:
        return item
    if item.target is None:
        return replace(
            item,
            action=BootstrapAction.FAIL_PRECHECK,
            reason="missing target configuration",
        )
    target = item.target
    if not target.password_env:
        return replace(
            item,
            action=BootstrapAction.FAIL_PRECHECK,
            reason="password_env is not configured",
        )
    if os.environ.get(target.password_env) is None:
        return replace(
            item,
            action=BootstrapAction.FAIL_PRECHECK,
            reason=f"password environment variable is not set: {target.password_env}",
        )
    try:
        resolved = resolve_target_for_plan(target, runtime_dir=item.state_base)
    except Exception as exc:
        return replace(
            item,
            action=BootstrapAction.FAIL_PRECHECK,
            reason=str(exc),
            target=target,
        )
    transport = factory(resolved)
    if not isinstance(transport, FTPTransport):
        return replace(
            item,
            action=BootstrapAction.FAIL_PRECHECK,
            reason="FTP Hybrid bootstrap requires FTPTransport",
            target=resolved,
        )
    try:
        transport.connect()
        try:
            banner_hash = transport.server_banner_hash()
            root_exists = transport.root_exists()
            if not root_exists:
                if create_root:
                    return replace(
                        item,
                        action=BootstrapAction.CREATE_ROOT_AND_PROBE,
                        reason="remote root missing",
                        target=resolved,
                        endpoint=_endpoint_label(resolved),
                    )
                return replace(
                    item,
                    action=BootstrapAction.FAIL_PRECHECK,
                    reason="remote root missing; pass without --no-create-root to create it",
                    target=resolved,
                    endpoint=_endpoint_label(resolved),
                )
            status = inspect_capability_profile(
                item.state_base,
                resolved,
                server_banner_hash=banner_hash,
            )
            if force:
                return replace(
                    item,
                    action=BootstrapAction.REPROBE,
                    reason="forced reprobe",
                    target=resolved,
                    endpoint=_endpoint_label(resolved),
                )
            if status is CapabilityProfileStatus.VALID:
                pending_reason = _pending_block_reason(transport, item, resolved)
                if pending_reason is not None:
                    return replace(
                        item,
                        action=BootstrapAction.FAIL_PRECHECK,
                        reason=pending_reason,
                        target=resolved,
                        endpoint=_endpoint_label(resolved),
                    )
                return replace(
                    item,
                    action=BootstrapAction.READY,
                    reason="existing profile valid",
                    target=resolved,
                    endpoint=_endpoint_label(resolved),
                )
            action, reason = _status_to_action(status)
            return replace(
                item,
                action=action,
                reason=reason,
                target=resolved,
                endpoint=_endpoint_label(resolved),
            )
        finally:
            transport.close()
    except Exception as exc:
        return replace(
            item,
            action=BootstrapAction.FAIL_PRECHECK,
            reason=str(exc),
            target=resolved,
            endpoint=_endpoint_label(resolved),
        )


def plan_bootstrap_items(
    items: tuple[BootstrapItem, ...],
    *,
    force: bool = False,
    create_root: bool = True,
    transport_factory=None,  # noqa: ANN001
) -> tuple[BootstrapItem, ...]:
    """Preflight every item and return the final plan in stable order.

    Args:
        items: Enumerated candidates including SKIP rows.
        force: Force REPROBE for valid profiles.
        create_root: Allow CREATE_ROOT_AND_PROBE for missing roots.
        transport_factory: Injectable transport factory for tests.

    Returns:
        Preflighted items ready for rendering and confirmation.
    """

    factory = transport_factory or create_transport
    return tuple(
        preflight_bootstrap_item(
            item,
            force=force,
            create_root=create_root,
            transport_factory=factory,
        )
        for item in items
    )


def render_bootstrap_plan(items: tuple[BootstrapItem, ...]) -> str:
    """Render the unified FTP Hybrid bootstrap plan table.

    Args:
        items: Preflighted bootstrap items.

    Returns:
        Multi-line plan text suitable for stdout.
    """

    lines = [
        "FTP HYBRID BOOTSTRAP PLAN",
        "",
        f"{'REPOSITORY':<14} {'TARGET':<10} {'ENDPOINT':<36} ACTION",
    ]
    create_root = 0
    probe = 0
    ready = 0
    skipped = 0
    failed = 0
    for item in items:
        action_label = _action_label(item)
        endpoint = item.endpoint or item.reason
        if item.action is BootstrapAction.SKIP:
            endpoint = item.reason
            skipped += 1
        elif item.action is BootstrapAction.READY:
            ready += 1
        elif item.action is BootstrapAction.FAIL_PRECHECK:
            failed += 1
            endpoint = item.reason
        elif item.action is BootstrapAction.CREATE_ROOT_AND_PROBE:
            create_root += 1
            probe += 1
        elif item.action in {BootstrapAction.PROBE, BootstrapAction.REPROBE}:
            probe += 1
        lines.append(
            f"{item.repository_name:<14} {item.target_name:<10} {endpoint:<36} {action_label}"
        )
    lines.extend(
        [
            "",
            "Remote mutations:",
            f"  create root: {create_root}",
            f"  capability probe: {probe}",
            f"  existing valid profiles: {ready}",
            f"  skipped: {skipped}",
            f"  precheck failed: {failed}",
        ]
    )
    return "\n".join(lines)


def mutation_count(items: tuple[BootstrapItem, ...]) -> int:
    """Count items that will create a root and/or run a capability probe.

    Args:
        items: Preflighted bootstrap items.

    Returns:
        Number of mutating actions that require confirmation.
    """

    return sum(
        1
        for item in items
        if item.action
        in {
            BootstrapAction.PROBE,
            BootstrapAction.REPROBE,
            BootstrapAction.CREATE_ROOT_AND_PROBE,
        }
    )


def confirm_bootstrap(items: tuple[BootstrapItem, ...], *, yes: bool) -> None:
    """Require one interactive confirmation for the whole bootstrap batch.

    Args:
        items: Preflighted plan.
        yes: Skip confirmation when True.

    Raises:
        ConfigError: When confirmation is refused or Non-TTY lacks ``--yes``.
    """

    count = mutation_count(items)
    if count == 0:
        return
    if yes:
        return
    if not sys.stdin.isatty():
        raise ConfigError("bootstrap requires --yes when stdin is not interactive")
    answer = input(f"Proceed with {count} FTP Hybrid initialization action(s)? [y/N] ")
    if answer.strip().lower() not in {"y", "yes"}:
        raise ConfigError("bootstrap cancelled")


def execute_bootstrap_item(
    item: BootstrapItem,
    *,
    transport_factory=None,  # noqa: ANN001
) -> BootstrapResult:
    """Execute one planned bootstrap item with a local target lock.

    Args:
        item: Preflighted item.
        transport_factory: Injectable transport factory for tests.

    Returns:
        Per-item success/failure result; never raises for probe failures.
    """

    factory = transport_factory or create_transport
    if item.action is BootstrapAction.SKIP:
        return BootstrapResult(item, True, None, None, detail=item.reason)
    if item.action is BootstrapAction.READY:
        return BootstrapResult(item, True, None, None, detail=item.reason)
    if item.action is BootstrapAction.FAIL_PRECHECK:
        return BootstrapResult(item, False, None, item.reason, detail=item.reason)
    if item.target is None:
        return BootstrapResult(item, False, None, "missing target configuration")

    lock = TargetLock(item.state_base, item.target_name)
    try:
        lock.acquire()
    except PlanError as exc:
        return BootstrapResult(item, False, None, str(exc))

    transport = factory(item.target)
    try:
        if not isinstance(transport, FTPTransport):
            return BootstrapResult(item, False, None, "FTP Hybrid bootstrap requires FTPTransport")
        transport.connect()
        try:
            if item.action is BootstrapAction.CREATE_ROOT_AND_PROBE:
                if not transport.root_exists():
                    transport.ensure_root()
            pending_reason = _pending_block_reason(transport, item, item.target)
            if pending_reason is not None:
                return BootstrapResult(item, False, None, pending_reason)
            profile_path = probe_and_save_ftp_hybrid_capabilities(
                transport,
                item.target,
                item.state_base,
            )
            detail = (
                "root created; profile saved"
                if item.action is BootstrapAction.CREATE_ROOT_AND_PROBE
                else "profile saved"
            )
            return BootstrapResult(item, True, profile_path, None, detail=detail)
        finally:
            transport.close()
    except Exception as exc:
        return BootstrapResult(item, False, None, str(exc))
    finally:
        lock.release()


def execute_bootstrap(
    items: tuple[BootstrapItem, ...],
    *,
    transport_factory=None,  # noqa: ANN001
) -> tuple[BootstrapResult, ...]:
    """Execute planned items sequentially, continuing after individual failures.

    Args:
        items: Preflighted plan.
        transport_factory: Injectable transport factory for tests.

    Returns:
        One result per item in plan order.
    """

    factory = transport_factory or create_transport
    return tuple(execute_bootstrap_item(item, transport_factory=factory) for item in items)


def render_bootstrap_summary(results: tuple[BootstrapResult, ...]) -> str:
    """Render the final bootstrap summary and counts.

    Args:
        results: Execution results in plan order.

    Returns:
        Multi-line summary text.
    """

    lines = ["FTP HYBRID BOOTSTRAP SUMMARY", ""]
    ready = 0
    skipped = 0
    failed = 0
    for result in results:
        label = f"{result.item.repository_name}/{result.item.target_name}"
        if result.item.action is BootstrapAction.SKIP:
            status = "SKIP"
            detail = result.detail or result.item.reason
            skipped += 1
        elif not result.success:
            status = "FAIL"
            detail = result.error or "unknown error"
            failed += 1
        else:
            status = "READY"
            detail = result.detail or result.item.reason
            ready += 1
        lines.append(f"{status:<7} {label:<24} {detail}")
    lines.extend(
        [
            "",
            f"ready:   {ready}",
            f"skipped: {skipped}",
            f"failed:  {failed}",
        ]
    )
    return "\n".join(lines)


def bootstrap_exit_code(results: tuple[BootstrapResult, ...]) -> int:
    """Map bootstrap results to a process exit code.

    Args:
        results: Execution results.

    Returns:
        ``0`` when every non-SKIP item succeeded; non-zero otherwise.
    """

    if any(not result.success for result in results):
        return 1
    return 0


def run_bootstrap(
    *,
    config_path: Path | None = None,
    workspace_path: Path | None = None,
    target_filter: tuple[str, ...] = (),
    yes: bool = False,
    force: bool = False,
    create_root: bool = True,
    transport_factory=None,  # noqa: ANN001
    confirm=None,  # noqa: ANN001
    print_fn=print,  # noqa: ANN001
) -> int:
    """Run the full bootstrap workflow and return a process exit code.

    Args:
        config_path: Explicit project ``deploy.toml`` path.
        workspace_path: Explicit workspace TOML path.
        target_filter: Optional target names to include.
        yes: Skip interactive confirmation.
        force: Force REPROBE for valid profiles.
        create_root: Create missing configured remote roots when True.
        transport_factory: Injectable transport factory for tests.
        confirm: Confirmation callback (injectable for tests).
        print_fn: Output callback (injectable for tests).

    Returns:
        ``0`` on full success; non-zero when any item failed.
    """

    factory = transport_factory or create_transport
    confirm_fn = confirm or confirm_bootstrap
    filter_set = frozenset(target_filter) if target_filter else None
    if config_path is not None and workspace_path is not None:
        raise ConfigError("--config and --workspace are mutually exclusive")
    if workspace_path is not None:
        workspace = load_workspace(workspace_path)
        candidates = enumerate_workspace_bootstrap_candidates(
            workspace,
            target_filter=filter_set,
        )
    elif config_path is not None:
        config = load_config(config_path)
        candidates = enumerate_project_bootstrap_candidates(
            config,
            target_filter=filter_set,
        )
    else:
        local_config = Path.cwd() / "deploy.toml"
        local_workspace = Path.cwd() / "deploy.workspace.toml"
        if local_config.is_file() and local_workspace.is_file():
            raise ConfigError(
                "both deploy.toml and deploy.workspace.toml exist; select --config or --workspace"
            )
        if local_workspace.is_file():
            workspace = load_workspace(local_workspace.resolve())
            candidates = enumerate_workspace_bootstrap_candidates(
                workspace,
                target_filter=filter_set,
            )
        else:
            from git_deploy.config import discover_config

            config = load_config(discover_config(None))
            candidates = enumerate_project_bootstrap_candidates(
                config,
                target_filter=filter_set,
            )

    if not candidates:
        raise ConfigError("no targets found for bootstrap")

    plan = plan_bootstrap_items(
        candidates,
        force=force,
        create_root=create_root,
        transport_factory=factory,
    )
    print_fn(render_bootstrap_plan(plan))
    confirm_fn(plan, yes=yes)
    results = execute_bootstrap(plan, transport_factory=factory)
    print_fn("")
    print_fn(render_bootstrap_summary(results))
    return bootstrap_exit_code(results)


def _endpoint_label(target: TargetConfig) -> str:
    """Build a non-secret endpoint label for plan tables.

    Args:
        target: Target configuration.

    Returns:
        ``host:remote_root`` style label without credentials.
    """

    host = target.host or target.ssh_host_alias or "<unknown>"
    return f"{host}:{target.remote_root}"


def _action_label(item: BootstrapItem) -> str:
    """Return the display action for one plan row.

    Args:
        item: Preflighted item.

    Returns:
        Human-readable action string.
    """

    if item.action is BootstrapAction.CREATE_ROOT_AND_PROBE:
        return "CREATE ROOT + PROBE"
    if item.action is BootstrapAction.FAIL_PRECHECK:
        return f"FAIL ({item.reason})"
    if item.action is BootstrapAction.SKIP:
        return f"SKIP"
    return item.action.value.upper()


def _status_to_action(status: CapabilityProfileStatus) -> tuple[BootstrapAction, str]:
    """Map a profile inspection status to a planned action and reason.

    Args:
        status: Result of ``inspect_capability_profile``.

    Returns:
        Action and human-readable reason.
    """

    if status is CapabilityProfileStatus.MISSING:
        return BootstrapAction.PROBE, "profile missing"
    if status is CapabilityProfileStatus.OLD_SCHEMA:
        return BootstrapAction.REPROBE, "old capability schema"
    if status is CapabilityProfileStatus.CORRUPT:
        return BootstrapAction.REPROBE, "profile corrupt"
    if status is CapabilityProfileStatus.TARGET_DRIFT:
        return BootstrapAction.REPROBE, "target fingerprint changed"
    if status is CapabilityProfileStatus.BANNER_DRIFT:
        return BootstrapAction.REPROBE, "server banner changed"
    if status is CapabilityProfileStatus.INCOMPLETE:
        return BootstrapAction.REPROBE, "profile incomplete"
    return BootstrapAction.READY, "existing profile valid"


def _pending_block_reason(
    transport: FTPTransport,
    item: BootstrapItem,
    target: TargetConfig,
) -> str | None:
    """Return a fail-closed reason when an FTP Hybrid Pending marker exists.

    Args:
        transport: Connected FTP transport.
        item: Bootstrap item with optional project/mapping identity.
        target: Resolved target.

    Returns:
        Error string when Pending blocks bootstrap; otherwise ``None``.
    """

    if item.project_id is None or item.hybrid_mapping is None:
        return None
    try:
        pending = read_pending(
            transport,
            project_id=item.project_id,
            mapping=item.hybrid_mapping,
            remote=".",
            target=target,
        )
    except (PlanError, DeployError, GitDeployError):
        # Unreadable pending is treated as a hard block for safety.
        return "unreadable FTP Hybrid Pending; finish deploy/recover before bootstrap"
    except Exception:
        return "unreadable FTP Hybrid Pending; finish deploy/recover before bootstrap"
    if pending is not None:
        return (
            f"FTP Hybrid Pending phase {pending.phase.value}; "
            "finish deploy/recover before bootstrap"
        )
    return None
