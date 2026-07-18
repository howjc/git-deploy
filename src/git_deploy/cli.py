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
from git_deploy.planner import DeploymentPlan, render_plan, render_recovery_plan
from git_deploy.prepared import (
    execute_prepared,
    execute_prepared_recovery,
    prepare_project,
    prepare_recovery,
    prepare_remote_plan,
)
from git_deploy.transports.openssh_sftp import SSHConnectionPool
from git_deploy.workspace import (
    WorkspaceConfig,
    execute_workspace,
    execute_workspace_recovery,
    load_workspace,
    prepare_workspace,
    prepare_workspace_recovery,
    render_workspace_plan,
    render_workspace_recovery_plan,
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
    planning = parser.add_mutually_exclusive_group()
    planning.add_argument(
        "--dry-run",
        action="store_true",
        help="build and print the local plan without connecting",
    )
    planning.add_argument(
        "--remote-plan",
        action="store_true",
        help="read remote ownership and print the full plan without writing",
    )
    planning.add_argument(
        "--recover",
        action="store_true",
        help="review and explicitly execute one pending Hybrid Recovery",
    )
    parser.add_argument("--skip-build", action="store_true", help="skip configured build steps")
    parser.add_argument(
        "--full",
        action="store_true",
        help="upload all managed content, rebuild state, and allow reviewed Hybrid adoption",
    )
    parser.add_argument("--yes", action="store_true", help="deploy without interactive confirmation")
    parser.add_argument("--config", type=Path, help="path to one project's deploy.toml")
    parser.add_argument("--workspace", type=Path, help="path to deploy.workspace.toml")
    parser.add_argument("--verbose", action="store_true", help="show detailed upload progress")
    parser.add_argument(
        "--create-root",
        action="store_true",
        help="allow doctor to create a missing remote root",
    )
    parser.add_argument(
        "--probe-ftp-hybrid",
        action="store_true",
        help=(
            "allow doctor to create/remove FTP Hybrid probe files and replace the "
            "local capability profile"
        ),
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
        return _doctor_workspace(workspace, args.doctor_target, args)
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
    if args.probe_ftp_hybrid:
        raise ConfigError("--probe-ftp-hybrid is only valid with doctor")
    if args.recover and args.full:
        raise ConfigError("--recover does not accept --full")
    if args.recover:
        prepared_recovery = prepare_recovery(
            config.project_root.name,
            config,
            requested_target,
        )
        if prepared_recovery is None:
            raise ConfigError("no pending Hybrid Recovery exists for this target")
        try:
            print(render_recovery_plan(prepared_recovery.plan))
            if not args.yes:
                _confirm_recovery(
                    prepared_recovery.plan.target.name,
                    1,
                    len(prepared_recovery.plan.target.after_deploy)
                    if prepared_recovery.plan.outcome.commands_pending
                    else 0,
                    1,
                )
            execute_prepared_recovery(prepared_recovery, verbose=args.verbose)
            print("Hybrid Recovery step finished; rerun --remote-plan to verify its state.")
            return 0
        finally:
            prepared_recovery.close()

    prepared = prepare_project(
        config.project_root.name,
        config.path,
        requested_target,
        full=args.full,
        skip_build=args.skip_build,
    )
    try:
        if not args.dry_run and (
            args.remote_plan or prepared.plan.hybrid is not None
        ):
            prepare_remote_plan(prepared)
        print(render_plan(prepared.plan))
        if args.dry_run:
            print("Dry-run complete: no remote connection and no state change.")
            return 0
        if args.remote_plan:
            print(
                "Remote plan complete: no upload, delete, command, remote manifest, "
                "or local state write."
            )
            return 0
        has_recovery = bool(
            prepared.plan.hybrid and prepared.plan.hybrid.recovery_records
        )
        if has_recovery:
            raise ConfigError(
                "pending Hybrid Recovery requires a separate reviewed --recover run"
            )
        if prepared.plan.has_remote_work and not args.yes:
            _confirm(prepared.plan)
        execute_prepared(
            prepared,
            verbose=args.verbose,
        )
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
    if args.probe_ftp_hybrid:
        raise ConfigError("--probe-ftp-hybrid is only valid with doctor")
    if args.recover and args.full:
        raise ConfigError("--recover does not accept --full")
    if args.recover:
        pool = SSHConnectionPool()
        prepared_recovery = ()
        try:
            target, prepared_recovery = prepare_workspace_recovery(
                workspace,
                requested_target,
                connection_pool=pool,
            )
            if not prepared_recovery:
                raise ConfigError("no pending Hybrid Recovery exists in this workspace")
            print(render_workspace_recovery_plan(target, prepared_recovery))
            command_count = sum(
                len(item.plan.target.after_deploy)
                for item in prepared_recovery
                if item.plan.outcome.commands_pending
            )
            if not args.yes:
                _confirm_recovery(
                    target,
                    len(prepared_recovery),
                    command_count,
                    len(prepared_recovery),
                )
            execute_workspace_recovery(prepared_recovery, verbose=args.verbose)
            print(
                "Workspace Hybrid Recovery step finished; rerun --remote-plan to verify."
            )
            return 0
        finally:
            for item in prepared_recovery:
                item.close()
            pool.close_all()

    target, prepared = prepare_workspace(
        workspace,
        requested_target,
        full=args.full,
        skip_build=args.skip_build,
    )
    pool = SSHConnectionPool()
    try:
        if not args.dry_run and (
            args.remote_plan
            or any(item.plan.hybrid is not None for item in prepared)
        ):
            for item in prepared:
                if args.remote_plan or item.plan.hybrid is not None:
                    prepare_remote_plan(
                        item,
                        connection_pool=pool,
                    )
        print(render_workspace_plan(target, prepared))
        if args.dry_run:
            print("Workspace dry-run complete: no remote connection and no state change.")
            return 0
        if args.remote_plan:
            print(
                "Workspace remote plan complete: no remote or local state mutation."
            )
            return 0
        recovery_count = sum(
            len(item.plan.hybrid.recovery_records)
            for item in prepared
            if item.plan.hybrid is not None
        )
        if recovery_count:
            raise ConfigError(
                "pending Hybrid Recovery requires a separate reviewed --recover run"
            )
        operation_count = sum(item.plan.operation_count for item in prepared)
        adoption_count = sum(item.plan.adoption_count for item in prepared)
        command_count = sum(
            len(item.plan.target.after_deploy) for item in prepared if item.plan.has_remote_work
        )
        if operation_count and not args.yes:
            _confirm_workspace(
                target,
                operation_count,
                adoption_count,
                command_count,
                len(prepared),
            )
        completed = execute_workspace(
            prepared,
            verbose=args.verbose,
            connection_pool=pool,
        )
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
        pool.close_all()


def _doctor(
    config: Config,
    requested_target: str | None,
    args: argparse.Namespace,
    repository: GitRepository,
    state_store: StateStore,
) -> int:
    """Render focused diagnostics and return failure when any check fails."""

    if args.probe_ftp_hybrid:
        target = config.target(requested_target)
        has_hybrid = any(output.mode == "hybrid" for output in config.outputs)
        if target.protocol != "ftp" or not has_hybrid:
            raise ConfigError("--probe-ftp-hybrid requires an FTP target with one Hybrid output")
        print("This probe creates and removes temporary files under .git-deploy/ftp-probe.")
        if not args.yes:
            _confirm_ftp_probe(target.name)

    results = run_doctor(
        config,
        config.target(requested_target),
        repository,
        state_store,
        create_root=args.create_root,
        probe_ftp_hybrid=args.probe_ftp_hybrid,
    )
    _print_doctor_results(results)
    return 0 if all(item.ok for item in results) else 1


def _doctor_workspace(
    workspace: WorkspaceConfig,
    requested_target: str | None,
    args: argparse.Namespace,
) -> int:
    """Render per-project workspace diagnostics and aggregate their status."""

    target = workspace.target(requested_target)
    if args.probe_ftp_hybrid:
        print("This probe creates and removes temporary files under .git-deploy/ftp-probe.")
        if not args.yes:
            _confirm_ftp_probe(target)
    if args.probe_ftp_hybrid:
        grouped = run_workspace_doctor(
            workspace,
            requested_target,
            create_root=args.create_root,
            probe_ftp_hybrid=True,
        )
    else:
        grouped = run_workspace_doctor(
            workspace,
            requested_target,
            create_root=args.create_root,
        )
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
    commands = len(plan.target.after_deploy) if plan.has_remote_work else 0
    answer = input(
        f"Deploy {plan.operation_count} operation(s), {plan.adoption_count} adoption(s), "
        f"and {commands} after-deploy command(s) to {plan.target.name}? [y/N] "
    )
    if answer.strip().lower() not in {"y", "yes"}:
        raise ConfigError("deployment cancelled")


def _confirm_workspace(
    target: str,
    operations: int,
    adoptions: int,
    commands: int,
    repositories: int,
) -> None:
    """Require exactly one confirmation for all prepared workspace writes."""

    if not sys.stdin.isatty():
        raise ConfigError("deployment requires --yes when stdin is not interactive")
    answer = input(
        f"Deploy {operations} operation(s), {adoptions} adoption(s), and "
        f"{commands} after-deploy command(s) "
        f"across {repositories} repositories to {target}? [y/N] "
    )
    if answer.strip().lower() not in {"y", "yes"}:
        raise ConfigError("deployment cancelled")


def _confirm_recovery(
    target: str,
    actions: int,
    commands: int,
    repositories: int,
) -> None:
    """Require explicit confirmation for only rendered Recovery actions.

    Args:
        target: Logical target name.
        actions: Number of Recovery records to execute.
        commands: Number of pending reviewed commands.
        repositories: Number of affected projects.

    Returns:
        ``None`` only after an affirmative interactive answer.
    """

    if not sys.stdin.isatty():
        raise ConfigError("recovery requires --yes when stdin is not interactive")
    answer = input(
        f"Execute {actions} recovery action(s) and {commands} pending command(s) "
        f"across {repositories} project(s) for {target}? [y/N] "
    )
    if answer.strip().lower() not in {"y", "yes"}:
        raise ConfigError("recovery cancelled")


def _confirm_ftp_probe(target: str) -> None:
    """Require confirmation before Doctor writes protected FTP probe files."""

    if not sys.stdin.isatty():
        raise ConfigError("FTP Hybrid capability probe requires --yes when stdin is not interactive")
    answer = input(f"Probe FTP Hybrid capabilities for {target}? [y/N] ")
    if answer.strip().lower() not in {"y", "yes"}:
        raise ConfigError("FTP Hybrid capability probe cancelled")


def _validate_build_args(args: argparse.Namespace, *, allow_target: bool) -> None:
    """Reject deploy-only options accidentally passed to the build command."""

    if args.doctor_target is not None and not allow_target:
        raise ConfigError("build does not accept a target")
    if (
        args.dry_run
        or args.remote_plan
        or args.recover
        or args.skip_build
        or args.full
        or args.yes
        or args.create_root
        or args.probe_ftp_hybrid
    ):
        raise ConfigError("build does not accept deploy-only flags")


def _validate_doctor_args(args: argparse.Namespace) -> None:
    """Reject deploy-only options accidentally passed to doctor."""

    if (
        args.dry_run
        or args.remote_plan
        or args.recover
        or args.skip_build
        or args.full
        or (args.yes and not args.probe_ftp_hybrid)
    ):
        raise ConfigError("doctor does not accept deploy-only flags")


def _validate_init_args(args: argparse.Namespace) -> None:
    """Reject flags that would imply build, remote, or mutation behavior for init."""

    if args.doctor_target is not None:
        raise ConfigError("init does not accept a target")
    if (
        args.dry_run
        or args.remote_plan
        or args.recover
        or args.skip_build
        or args.full
        or args.yes
        or args.verbose
        or args.create_root
        or args.probe_ftp_hybrid
    ):
        raise ConfigError("init does not accept deploy or doctor flags")


if __name__ == "__main__":
    raise SystemExit(main())
