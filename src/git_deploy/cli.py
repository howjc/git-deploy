"""Command-line interface for revision selection, deployment, and rollback."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from . import __version__
from .config import discover_config, load_config
from .errors import ConfigurationError, GitDeployError, PolicyError
from .executor import DeploymentExecutor, RemoteCheck
from .gitrepo import GitDeploymentPlanner
from .models import AppConfig, DeploymentManifest, DeploymentPlan, ProjectConfig
from .progress import TerminalProgress
from .state import DeploymentStore


@dataclass(frozen=True)
class ProjectPlan:
    """Pair one project configuration with its planner and immutable plan."""

    project: ProjectConfig
    planner: GitDeploymentPlanner
    plan: DeploymentPlan


def build_parser() -> argparse.ArgumentParser:
    """Build the complete command parser.

    Returns:
        Configured top-level argument parser.
    """

    parser = argparse.ArgumentParser(
        prog="git-deploy",
        description="Deploy exact tracked-file changes selected from Git revisions.",
    )
    parser.add_argument("--config", help="deployment TOML path")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="build a local-only deployment preview")
    _add_plan_arguments(plan, include_dry_run=False)

    deploy = subparsers.add_parser("deploy", help="deploy selected commits and ranges")
    _add_plan_arguments(deploy, include_dry_run=True)
    deploy.add_argument("--yes", action="store_true", help="skip the mutation confirmation")

    history = subparsers.add_parser("history", help="show local deployment records")
    history.add_argument("target", help="project name or all")
    history.add_argument("--limit", type=int, default=20, help="records shown per project")

    verify = subparsers.add_parser("verify", help="compare remote files with a deployment record")
    verify.add_argument("target", help="project name or all")
    _add_deployment_selector(verify)

    rollback = subparsers.add_parser("rollback", help="restore an exact pre-deployment snapshot")
    rollback.add_argument("target", help="project name or all")
    _add_deployment_selector(rollback)
    rollback.add_argument("--dry-run", action="store_true", help="preview without remote writes")
    rollback.add_argument(
        "--check-remote",
        action="store_true",
        help="with --dry-run, connect read-only and verify rollback eligibility",
    )
    rollback.add_argument("--force", action="store_true", help="allow remote hash drift")
    rollback.add_argument("--yes", action="store_true", help="skip the mutation confirmation")
    return parser


def _add_plan_arguments(parser: argparse.ArgumentParser, include_dry_run: bool) -> None:
    """Add shared revision-selection planning arguments.

    Args:
        parser: Subcommand parser receiving the options.
        include_dry_run: Add the explicit deploy ``--dry-run`` flag when true.
    """

    parser.add_argument("targets", nargs="+", help="project names or all")
    parser.add_argument(
        "--revisions",
        nargs="+",
        required=True,
        metavar="COMMIT_OR_FROM..TO",
        help="single commits or continuous ranges; separate multiple selectors with spaces",
    )
    parser.add_argument(
        "--check-remote",
        action="store_true",
        help="connect read-only and verify the inferred Git baseline",
    )
    parser.add_argument("--force", action="store_true", help="allow remote hash drift")
    if include_dry_run:
        parser.add_argument("--dry-run", action="store_true", help="preview without remote writes")


def _add_deployment_selector(parser: argparse.ArgumentParser) -> None:
    """Add mutually exclusive deployment record selectors.

    Args:
        parser: Subcommand parser receiving the selector.
    """

    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--deployment", help="exact deployment ID or unique prefix")
    selector.add_argument("--latest", action="store_true", help="latest successful deployment")


def run(argv: Sequence[str] | None = None) -> int:
    """Execute one CLI invocation and convert domain errors to exit codes.

    Args:
        argv: Optional arguments excluding the executable name.

    Returns:
        Process exit code.
    """

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config_path = discover_config(args.config)
        config = load_config(config_path)
        if args.command in {"plan", "deploy"}:
            return _run_plan_or_deploy(config, args)
        if args.command == "history":
            return _run_history(config, args)
        if args.command == "verify":
            return _run_verify(config, args)
        if args.command == "rollback":
            return _run_rollback(config, args)
        raise ConfigurationError(f"unsupported command: {args.command}")
    except GitDeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130


def main() -> None:
    """Run the console entry point and terminate with its exit code."""

    raise SystemExit(run())


def _run_plan_or_deploy(config: AppConfig, args: argparse.Namespace) -> int:
    """Build plans, optionally inspect remote state, and deploy them.

    Args:
        config: Loaded application configuration.
        args: Parsed plan or deploy arguments.

    Returns:
        Process exit code.
    """

    projects = _select_projects(config, args.targets)
    planned: list[ProjectPlan] = []
    for project in projects:
        planner = GitDeploymentPlanner(project)
        planned.append(
            ProjectPlan(project, planner, planner.build_revisions(args.revisions))
        )

    print(f"Config: {config.path}")
    for item in planned:
        _print_plan(item.plan)
        _print_working_tree_warning(item)

    is_plan = args.command == "plan"
    is_dry_run = is_plan or args.dry_run
    if args.check_remote and not is_dry_run:
        raise PolicyError("--check-remote is only valid with plan or deploy --dry-run")
    if is_dry_run:
        if args.check_remote:
            for item in planned:
                if not item.plan.files:
                    continue
                with _progress_executor(config, item.project) as executor:
                    checks = executor.check_plan(item.plan, force=args.force)
                _print_checks(item.project.name, checks)
        else:
            print("Remote: not connected (local-only dry run)")
        return 0

    actionable = [item for item in planned if item.plan.files]
    if not actionable:
        print("No selected tracked-file changes; nothing deployed.")
        return 0
    _confirm(args.yes, f"Deploy {len(actionable)} project(s)?")
    for item in actionable:
        with _progress_executor(config, item.project) as executor:
            manifest = executor.deploy(item.plan, item.planner, force=args.force)
        print(f"[{item.project.name}] deployed: {manifest.deployment_id}")
    return 0


def _run_history(config: AppConfig, args: argparse.Namespace) -> int:
    """Print local deployment history without opening a remote connection.

    Args:
        config: Loaded application configuration.
        args: Parsed history arguments.

    Returns:
        Process exit code.
    """

    if args.limit < 1:
        raise ConfigurationError("--limit must be at least 1")
    for project in _select_projects(config, [args.target]):
        manifests = DeploymentStore(project).list_manifests()[: args.limit]
        print(f"[{project.name}] {len(manifests)} deployment record(s)")
        for manifest in manifests:
            selection = " ".join(manifest.revision_specs) or (
                f"{manifest.from_commit[:12]}..{manifest.to_commit[:12]}"
            )
            print(
                f"  {manifest.deployment_id}  {manifest.status:<18}  "
                f"{selection}  "
                f"{len(manifest.snapshots)} file(s)"
            )
    return 0


def _run_verify(config: AppConfig, args: argparse.Namespace) -> int:
    """Verify selected deployment records against remote files.

    Args:
        config: Loaded application configuration.
        args: Parsed verify arguments.

    Returns:
        Process exit code.
    """

    projects = _select_projects(config, [args.target])
    for project in projects:
        manifest = _select_manifest(project, args.deployment, args.latest, len(projects) > 1)
        with _progress_executor(config, project) as executor:
            checks = executor.verify(manifest)
        _print_checks(project.name, checks)
    return 0


def _run_rollback(config: AppConfig, args: argparse.Namespace) -> int:
    """Preview, verify, or execute deployment rollback.

    Args:
        config: Loaded application configuration.
        args: Parsed rollback arguments.

    Returns:
        Process exit code.
    """

    projects = _select_projects(config, [args.target])
    selected = [
        (
            project,
            _select_manifest(project, args.deployment, args.latest, len(projects) > 1),
        )
        for project in projects
    ]
    for project, manifest in selected:
        _print_rollback(project.name, manifest)

    if args.check_remote and not args.dry_run:
        raise PolicyError("--check-remote requires --dry-run")
    if args.dry_run:
        if args.check_remote:
            for project, manifest in selected:
                with _progress_executor(config, project) as executor:
                    checks = executor.check_rollback(manifest, force=args.force)
                _print_checks(project.name, checks)
        else:
            print("Remote: not connected (local-only dry run)")
        return 0

    _confirm(args.yes, f"Rollback {len(selected)} project(s)?")
    for project, manifest in selected:
        with _progress_executor(config, project) as executor:
            result = executor.rollback(manifest, force=args.force)
        print(f"[{project.name}] rolled back: {result.deployment_id}")
    return 0


@contextmanager
def _progress_executor(
    config: AppConfig,
    project: ProjectConfig,
) -> Iterator[DeploymentExecutor]:
    """Create an executor whose progress line is always finalized.

    Args:
        config: Loaded server configuration.
        project: Project being checked, deployed, verified, or rolled back.

    Yields:
        Progress-enabled deployment executor.
    """

    renderer = TerminalProgress(project.name)
    try:
        yield DeploymentExecutor(
            project,
            dict(config.server.values),
            progress_callback=renderer.update,
        )
    finally:
        renderer.finish()


def _select_projects(config: AppConfig, targets: Sequence[str]) -> list[ProjectConfig]:
    """Expand ``all`` or validate an ordered project selection.

    Args:
        config: Loaded application configuration.
        targets: Positional project names.

    Returns:
        Ordered unique project configurations.
    """

    if "all" in targets:
        if len(targets) != 1:
            raise ConfigurationError("all cannot be combined with explicit project names")
        return list(config.projects.values())
    selected: list[ProjectConfig] = []
    seen: set[str] = set()
    for name in targets:
        if name in seen:
            continue
        try:
            selected.append(config.projects[name])
        except KeyError as exc:
            available = ", ".join(config.projects)
            raise ConfigurationError(f"unknown project {name!r}; available: {available}") from exc
        seen.add(name)
    return selected


def _select_manifest(
    project: ProjectConfig,
    deployment_id: str | None,
    latest: bool,
    multiple_projects: bool,
) -> DeploymentManifest:
    """Load one project deployment record from CLI selectors.

    Args:
        project: Project owning the local history.
        deployment_id: Explicit identifier or prefix.
        latest: Select the latest successful record.
        multiple_projects: Whether ``all`` expanded to multiple projects.

    Returns:
        Selected deployment manifest.
    """

    store = DeploymentStore(project)
    if latest:
        return store.latest_successful()
    if multiple_projects:
        raise ConfigurationError("--deployment cannot be combined with all; use --latest")
    assert deployment_id is not None
    return store.load(deployment_id)


def _confirm(assume_yes: bool, prompt: str) -> None:
    """Require explicit confirmation before remote mutation.

    Args:
        assume_yes: Skip prompting when ``--yes`` was supplied.
        prompt: Operation summary shown to an interactive user.
    """

    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise PolicyError("remote mutation requires --yes in a non-interactive session")
    answer = input(f"{prompt} Type 'yes' to continue: ").strip().lower()
    if answer != "yes":
        raise PolicyError("operation cancelled")


def _print_plan(plan: DeploymentPlan) -> None:
    """Print one deterministic deployment plan.

    Args:
        plan: Plan to render.
    """

    selection = " ".join(plan.revision_specs) or f"{plan.from_commit}..{plan.to_commit}"
    print(f"[{plan.project}] revisions: {selection}")
    print(f"  baseline {plan.from_commit[:12]} -> target {plan.to_commit[:12]}")
    if not plan.files:
        print("  no selected tracked-file changes")
    for operation in plan.files:
        if operation.action == "upload":
            print(f"  UPLOAD {operation.path} ({operation.target_size} bytes)")
        else:
            print(f"  DELETE {operation.path}")
    if plan.excluded:
        print(f"  excluded changes: {len(plan.excluded)}")


def _print_working_tree_warning(item: ProjectPlan) -> None:
    """Warn when uncommitted paths are absent from the commit-based plan.

    Args:
        item: Project, planner, and plan being displayed.
    """

    changes = item.planner.repository.working_tree_changes()
    if not changes:
        return
    print(
        f"[{item.project.name}] WARNING: {len(changes)} uncommitted working-tree "
        "change(s) are ignored; deployment reads commits only."
    )
    planned_actions = {operation.path: operation.action.upper() for operation in item.plan.files}
    overlaps: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for change in changes:
        candidates = [change.path]
        if change.old_path:
            candidates.append(change.old_path)
        for path in candidates:
            action = planned_actions.get(path)
            if action and path not in seen:
                overlaps.append((change.status, path, action))
                seen.add(path)
    for status, path, action in overlaps[:8]:
        print(f"  WORKTREE {status} {path} (commit plan: {action})")
    if len(overlaps) > 8:
        print(f"  ... and {len(overlaps) - 8} more overlapping path(s)")


def _print_checks(project: str, checks: tuple[RemoteCheck, ...]) -> None:
    """Print concise remote hash verification results.

    Args:
        project: Configured project name.
        checks: Remote comparison results.
    """

    matched = sum(check.matches for check in checks)
    print(f"[{project}] remote checks: {matched}/{len(checks)} matched")
    for check in checks:
        if not check.matches:
            expected = check.expected_sha256[:12] if check.expected_sha256 else "absent"
            actual = check.actual_sha256[:12] if check.actual_sha256 else "absent"
            print(f"  DRIFT {check.path}: expected {expected}, actual {actual}")


def _print_rollback(project: str, manifest: DeploymentManifest) -> None:
    """Print files that an exact snapshot rollback will restore or remove.

    Args:
        project: Configured project name.
        manifest: Selected deployment record.
    """

    print(f"[{project}] rollback {manifest.deployment_id}")
    for snapshot in manifest.snapshots:
        action = "RESTORE" if snapshot.before_exists else "DELETE"
        print(f"  {action} {snapshot.path}")
