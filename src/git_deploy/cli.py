"""Minimal v1-lite command line: deploy by default, build, and doctor."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from git_deploy import __version__
from git_deploy.builder import run_build
from git_deploy.config import Config, discover_config, load_config
from git_deploy.deployer import execute_plan
from git_deploy.doctor import run_doctor
from git_deploy.errors import ConfigError, GitDeployError, PlanError, StateError
from git_deploy.git import GitRepository
from git_deploy.manifest import StateStore
from git_deploy.planner import DeploymentPlan, create_plan, render_plan
from git_deploy.config import resolve_target_for_plan


def build_parser() -> argparse.ArgumentParser:
    """Build the intentionally flat v1-lite argument parser."""

    parser = argparse.ArgumentParser(
        prog="git-deploy",
        description="Git-aware local build and FTP/SFTP file synchronization",
    )
    parser.add_argument("action", nargs="?", help="target name, 'build', or 'doctor'")
    parser.add_argument("doctor_target", nargs="?", help="optional target for doctor")
    parser.add_argument("--dry-run", action="store_true", help="build and print the plan without connecting")
    parser.add_argument("--skip-build", action="store_true", help="skip configured build steps")
    parser.add_argument("--full", action="store_true", help="upload all managed local content and rebuild state")
    parser.add_argument("--yes", action="store_true", help="deploy without interactive confirmation")
    parser.add_argument("--config", type=Path, help="path to deploy.toml")
    parser.add_argument("--verbose", action="store_true", help="show detailed upload progress")
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
        config = load_config(discover_config(args.config))
        if args.action == "build":
            _validate_build_args(args)
            run_build(config.build, config.project_root)
            print("Build completed successfully.")
            return 0
        repository = GitRepository(config.project_root)
        if args.action == "doctor":
            _validate_doctor_args(args)
            try:
                doctor_git_dir = repository.git_dir()
            except PlanError:
                doctor_git_dir = config.project_root / ".git"
            state_store = StateStore(doctor_git_dir)
            return _doctor(config, args.doctor_target, repository, state_store)
        if args.doctor_target is not None:
            parser.error("deployment accepts at most one TARGET positional argument")
        state_store = StateStore(repository.git_dir())
        return _deploy(config, args.action, args, repository, state_store)
    except GitDeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130


def _deploy(
    config: Config,
    requested_target: str | None,
    args: argparse.Namespace,
    repository: GitRepository,
    state_store: StateStore,
) -> int:
    """Run build, local planning, optional confirmation, and deployment."""

    target = config.target(requested_target)
    repository.validate()
    before_build_status = repository.status_porcelain()
    if before_build_status and config.source.require_clean_worktree:
        raise PlanError("worktree has uncommitted changes and source.require_clean_worktree is true")
    if before_build_status:
        print("WARNING: uncommitted changes are not included; committed HEAD content will be deployed.")
    try:
        state = state_store.load(target.name)
    except StateError:
        if not args.full:
            raise
        print("WARNING: existing state is unreadable; --full will rebuild it after success.")
        state = None
    resolved_target = resolve_target_for_plan(target)
    target_fingerprint = resolved_target.fingerprint
    if state is not None and state.target_fingerprint != target_fingerprint and not args.full:
        raise PlanError("target identity changed since the last success; review it and rerun with --full")
    if not args.skip_build:
        run_build(config.build, config.project_root)
        after_build_status = repository.status_porcelain()
        if after_build_status and config.source.require_clean_worktree:
            raise PlanError(
                "build left uncommitted changes and source.require_clean_worktree is true"
            )
        if after_build_status != before_build_status:
            print(
                "WARNING: build changed the worktree; these changes are not included in source deployment."
            )
    plan = create_plan(
        config,
        target,
        repository,
        state,
        full=args.full,
        resolved_target=resolved_target,
    )
    print(render_plan(plan))
    if args.dry_run:
        print("Dry-run complete: no remote connection and no state change.")
        return 0
    if plan.operations and not args.yes:
        _confirm(plan)
    execute_plan(plan, config, repository, state_store, verbose=args.verbose)
    print(f"Deployment completed: {plan.upload_count} upload(s), {plan.delete_count} delete(s).")
    return 0


def _doctor(
    config: Config,
    requested_target: str | None,
    repository: GitRepository,
    state_store: StateStore,
) -> int:
    """Render focused diagnostics and return failure when any check fails."""

    target = config.target(requested_target)
    results = run_doctor(config, target, repository, state_store)
    for result in results:
        marker = "OK" if result.ok else "FAIL"
        print(f"[{marker}] {result.name}: {result.detail}")
    return 0 if all(item.ok for item in results) else 1


def _confirm(plan: DeploymentPlan) -> None:
    """Require an explicit interactive confirmation before remote writes."""

    if not sys.stdin.isatty():
        raise ConfigError("deployment requires --yes when stdin is not interactive")
    answer = input(f"Deploy {len(plan.operations)} operation(s) to {plan.target.name}? [y/N] ")
    if answer.strip().lower() not in {"y", "yes"}:
        raise ConfigError("deployment cancelled")


def _validate_build_args(args: argparse.Namespace) -> None:
    """Reject deploy-only options accidentally passed to the build command."""

    if args.doctor_target is not None:
        raise ConfigError("build does not accept a target")
    if args.dry_run or args.skip_build or args.full or args.yes:
        raise ConfigError("build does not accept deploy-only flags")


def _validate_doctor_args(args: argparse.Namespace) -> None:
    """Reject deploy-only options accidentally passed to doctor."""

    if args.dry_run or args.skip_build or args.full or args.yes:
        raise ConfigError("doctor does not accept deploy-only flags")


if __name__ == "__main__":
    raise SystemExit(main())
