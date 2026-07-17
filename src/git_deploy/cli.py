"""Flat CLI for independent projects and thin multi-repository workspaces."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Literal

from git_deploy import __version__
from git_deploy.builder import run_build
from git_deploy.config import Config, discover_config, load_config
from git_deploy.doctor import DoctorResult, run_doctor
from git_deploy.errors import ConfigError, GitDeployError, PlanError
from git_deploy.git import GitRepository
from git_deploy.initializer import initialize_project
from git_deploy.manifest import StateStore
from git_deploy.planner import DeploymentPlan, render_plan
from git_deploy.prepared import execute_prepared, prepare_project
from git_deploy.workspace import (
    WorkspaceConfig,
    execute_workspace,
    load_workspace,
    prepare_workspace,
    render_workspace_plan,
    run_workspace_build,
    run_workspace_doctor,
)

InputMode = Literal["project", "workspace"]


def build_parser() -> argparse.ArgumentParser:
    """Build the intentionally flat v1-lite argument parser."""

    parser = argparse.ArgumentParser(
        prog="git-deploy",
        description="Git-aware local build and FTP/SFTP file synchronization",
    )
    parser.add_argument("action", nargs="?", help="target name, 'build', 'doctor', or 'init'")
    parser.add_argument("doctor_target", nargs="?", help="optional target for doctor/workspace build")
    parser.add_argument("--dry-run", action="store_true", help="build and print the plan without connecting")
    parser.add_argument("--skip-build", action="store_true", help="skip configured build steps")
    parser.add_argument("--full", action="store_true", help="upload all managed local content and rebuild state")
    parser.add_argument("--yes", action="store_true", help="deploy without interactive confirmation")
    parser.add_argument("--config", type=Path, help="path to one project's deploy.toml")
    parser.add_argument("--workspace", type=Path, help="path to deploy.workspace.toml")
    parser.add_argument("--verbose", action="store_true", help="show detailed upload progress")
    parser.add_argument(
        "--create-root",
        action="store_true",
        help="allow doctor to create a missing remote root",
    )
    parser.add_argument("--version", action="version", version=f"git-deploy {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return a stable user-facing exit code.

    Args:
        argv: Optional arguments without the executable name.

    Returns:
        Zero on success or the categorized v1-lite failure code.
    """

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.action == "init":
            return _initialize(args)
        mode, input_path = _select_input(args.config, args.workspace)
        if mode == "workspace":
            workspace = load_workspace(input_path)
            return _run_workspace(workspace, args, parser)
        config = load_config(input_path)
        return _run_project(config, args, parser)
    except GitDeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130


def _initialize(args: argparse.Namespace) -> int:
    """Create a single-project scaffold without ambiguous workspace discovery."""

    _validate_init_args(args)
    if args.workspace is not None:
        raise ConfigError("init does not accept --workspace")
    local_workspace = Path.cwd() / "deploy.workspace.toml"
    if args.config is None and local_workspace.is_file():
        raise ConfigError("init in a workspace root requires an explicit repository --config path")
    destination = args.config if args.config is not None else None
    created = initialize_project(Path.cwd(), destination)
    print(f"Created {created}; edit target settings, then run git-deploy --dry-run.")
    return 0


def _select_input(config: Path | None, workspace: Path | None) -> tuple[InputMode, Path]:
    """Select project/workspace mode without silently resolving ambiguity.

    Args:
        config: Explicit single-project configuration, if supplied.
        workspace: Explicit thin-workspace configuration, if supplied.

    Returns:
        The selected mode and absolute configuration path.
    """

    if config is not None and workspace is not None:
        raise ConfigError("--config and --workspace are mutually exclusive")
    if config is not None:
        return "project", discover_config(config)
    if workspace is not None:
        return "workspace", workspace.expanduser().resolve()
    local_config = Path.cwd() / "deploy.toml"
    local_workspace = Path.cwd() / "deploy.workspace.toml"
    if local_config.is_file() and local_workspace.is_file():
        raise ConfigError(
            "both deploy.toml and deploy.workspace.toml exist; select --config or --workspace"
        )
    if local_workspace.is_file():
        return "workspace", local_workspace.resolve()
    return "project", discover_config(None)


def _run_project(
    config: Config,
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    """Dispatch one independent project command."""

    if args.action == "build":
        _validate_build_args(args, allow_target=False)
        run_build(config.build, config.project_root)
        print("Build completed successfully.")
        return 0
    repository = GitRepository(config.project_root)
    if args.action == "doctor":
        _validate_doctor_args(args)
        try:
            doctor_git_dir = repository.common_dir()
        except PlanError:
            doctor_git_dir = config.project_root / ".git"
        return _doctor(config, args.doctor_target, args, repository, StateStore(doctor_git_dir))
    if args.doctor_target is not None:
        parser.error("deployment accepts at most one TARGET positional argument")
    return _deploy_project(config, args.action, args)


def _run_workspace(
    workspace: WorkspaceConfig,
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    """Dispatch one thin-workspace command."""

    if args.action == "build":
        _validate_build_args(args, allow_target=True)
        run_workspace_build(workspace, args.doctor_target)
        print("Workspace build completed successfully.")
        return 0
    if args.action == "doctor":
        _validate_doctor_args(args)
        return _doctor_workspace(workspace, args.doctor_target, create_root=args.create_root)
    if args.doctor_target is not None:
        parser.error("deployment accepts at most one TARGET positional argument")
    return _deploy_workspace(workspace, args.action, args)


def _deploy_project(
    config: Config,
    requested_target: str | None,
    args: argparse.Namespace,
) -> int:
    """Prepare frozen bytes, confirm, and deploy one independent project.

    Args:
        config: Loaded independent project configuration.
        requested_target: Explicit/default target name.
        args: Validated flat CLI options.

    Returns:
        Zero after dry-run or successful deployment.
    """

    if args.create_root:
        raise ConfigError("--create-root is only valid with doctor")

    prepared = prepare_project(
        config.project_root.name,
        config.path,
        requested_target,
        full=args.full,
        skip_build=args.skip_build,
    )
    try:
        print(render_plan(prepared.plan))
        if args.dry_run:
            print("Dry-run complete: no remote connection and no state change.")
            return 0
        if prepared.plan.operations and not args.yes:
            _confirm(prepared.plan)
        execute_prepared(prepared, verbose=args.verbose)
        print(
            f"Deployment completed: {prepared.plan.upload_count} upload(s), "
            f"{prepared.plan.delete_count} delete(s)."
        )
        return 0
    finally:
        prepared.close()


def _deploy_workspace(
    workspace: WorkspaceConfig,
    requested_target: str | None,
    args: argparse.Namespace,
) -> int:
    """Prepare every project, confirm once, and deploy in listed order.

    Args:
        workspace: Loaded thin-workspace configuration.
        requested_target: Shared explicit/default target name.
        args: Validated flat CLI options.

    Returns:
        Zero after workspace dry-run or successful sequential deployment.
    """

    if args.create_root:
        raise ConfigError("--create-root is only valid with doctor")

    target, prepared = prepare_workspace(
        workspace,
        requested_target,
        full=args.full,
        skip_build=args.skip_build,
    )
    try:
        print(render_workspace_plan(target, prepared))
        if args.dry_run:
            print("Workspace dry-run complete: no remote connection and no state change.")
            return 0
        operation_count = sum(len(item.plan.operations) for item in prepared)
        command_count = sum(
            len(item.plan.target.after_deploy) for item in prepared if item.plan.operations
        )
        if operation_count and not args.yes:
            _confirm_workspace(target, operation_count, command_count, len(prepared))
        completed = execute_workspace(prepared, verbose=args.verbose)
        uploads = sum(item.plan.upload_count for item in prepared)
        deletes = sum(item.plan.delete_count for item in prepared)
        print(
            f"Workspace deployment completed ({', '.join(completed)}): "
            f"{uploads} upload(s), {deletes} delete(s)."
        )
        return 0
    finally:
        for item in prepared:
            item.close()


def _doctor(
    config: Config,
    requested_target: str | None,
    args: argparse.Namespace,
    repository: GitRepository,
    state_store: StateStore,
) -> int:
    """Render focused diagnostics and return failure when any check fails."""

    results = run_doctor(
        config,
        config.target(requested_target),
        repository,
        state_store,
        create_root=args.create_root,
    )
    _print_doctor_results(results)
    return 0 if all(item.ok for item in results) else 1


def _doctor_workspace(
    workspace: WorkspaceConfig,
    requested_target: str | None,
    *,
    create_root: bool,
) -> int:
    """Render per-project workspace diagnostics and aggregate their status."""

    grouped = run_workspace_doctor(workspace, requested_target, create_root=create_root)
    for name, results in grouped:
        print(f"[{name}]")
        _print_doctor_results(results)
    return 0 if all(result.ok for _, results in grouped for result in results) else 1


def _print_doctor_results(results: tuple[DoctorResult, ...]) -> None:
    """Print one stable group of named doctor results."""

    for result in results:
        marker = "OK" if result.ok else "FAIL"
        print(f"[{marker}] {result.name}: {result.detail}")


def _confirm(plan: DeploymentPlan) -> None:
    """Require an explicit interactive confirmation before one project's writes."""

    if not sys.stdin.isatty():
        raise ConfigError("deployment requires --yes when stdin is not interactive")
    commands = len(plan.target.after_deploy) if plan.operations else 0
    answer = input(
        f"Deploy {len(plan.operations)} file operation(s) and {commands} after-deploy "
        f"command(s) to {plan.target.name}? [y/N] "
    )
    if answer.strip().lower() not in {"y", "yes"}:
        raise ConfigError("deployment cancelled")


def _confirm_workspace(
    target: str,
    operations: int,
    commands: int,
    repositories: int,
) -> None:
    """Require exactly one confirmation for all prepared workspace writes."""

    if not sys.stdin.isatty():
        raise ConfigError("deployment requires --yes when stdin is not interactive")
    answer = input(
        f"Deploy {operations} file operation(s) and {commands} after-deploy command(s) "
        f"across {repositories} repositories to {target}? [y/N] "
    )
    if answer.strip().lower() not in {"y", "yes"}:
        raise ConfigError("deployment cancelled")


def _validate_build_args(args: argparse.Namespace, *, allow_target: bool) -> None:
    """Reject deploy-only options accidentally passed to the build command."""

    if args.doctor_target is not None and not allow_target:
        raise ConfigError("build does not accept a target")
    if args.dry_run or args.skip_build or args.full or args.yes or args.create_root:
        raise ConfigError("build does not accept deploy-only flags")


def _validate_doctor_args(args: argparse.Namespace) -> None:
    """Reject deploy-only options accidentally passed to doctor."""

    if args.dry_run or args.skip_build or args.full or args.yes:
        raise ConfigError("doctor does not accept deploy-only flags")


def _validate_init_args(args: argparse.Namespace) -> None:
    """Reject flags that would imply build, remote, or mutation behavior for init."""

    if args.doctor_target is not None:
        raise ConfigError("init does not accept a target")
    if args.dry_run or args.skip_build or args.full or args.yes or args.verbose or args.create_root:
        raise ConfigError("init does not accept deploy or doctor flags")


if __name__ == "__main__":
    raise SystemExit(main())
