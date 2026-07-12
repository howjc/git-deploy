"""Command-line interface for revision selection, deployment, and rollback."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .config import discover_config, load_config, select_remote
from .errors import ConfigurationError, GitDeployError, PolicyError
from .executor import DeploymentExecutor, RemoteCheck
from .gitrepo import GitDeploymentPlanner
from .models import AppConfig, DeploymentManifest, DeploymentPlan, ProjectConfig, ServerConfig
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
    _add_remote_argument(history)

    verify = subparsers.add_parser("verify", help="compare remote files with a deployment record")
    verify.add_argument("target", help="project name or all")
    _add_deployment_selector(verify)
    _add_remote_argument(verify)

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
    _add_remote_argument(rollback)

    state = subparsers.add_parser("state", help="inspect, verify, bootstrap, and recover target state")
    state_sub = state.add_subparsers(dest="state_command", required=True)

    inspect = state_sub.add_parser("inspect", help="show local expected-state summary (no remote)")
    inspect.add_argument("target", help="project name")
    _add_remote_argument(inspect)

    state_verify = state_sub.add_parser("verify", help="verify local or remote expected state")
    state_verify.add_argument("target", help="project name")
    state_verify.add_argument(
        "--check-remote",
        dest="state_remote_check",
        action="store_true",
        help="read-only remote path check against current snapshot (zero writes)",
    )
    _add_remote_argument(state_verify)

    bootstrap = state_sub.add_parser("bootstrap", help="create generation-1 current state")
    bootstrap.add_argument("target", help="project name")
    bootstrap.add_argument("--revision", help="known Git revision baseline")
    bootstrap.add_argument("--empty", action="store_true", help="empty baseline after remote verify")
    bootstrap.add_argument("--dry-run", action="store_true", help="plan only; do not write")
    bootstrap.add_argument("--yes", action="store_true", help="confirm write")
    _add_remote_argument(bootstrap)

    recover = state_sub.add_parser("recover", help="show or execute transaction recovery decisions")
    recover.add_argument("target", help="project name")
    recover.add_argument("--execute", action="store_true", help="apply safe finalize/restore decisions")
    recover.add_argument("--yes", action="store_true", help="confirm recovery mutations")
    _add_remote_argument(recover)

    # Explicitly reject GC in v0.2.
    state_sub.add_parser("gc", help="not supported in v0.2 (objects are fully retained)")

    migrate = state_sub.add_parser("migrate", help="legacy/named-remote history migration")
    migrate.add_argument("target", help="project name")
    migrate.add_argument("--yes", action="store_true", help="publish staging to live targets")
    migrate.add_argument("--stage", action="store_true", help="build staging tree")
    _add_remote_argument(migrate)

    policy = state_sub.add_parser("policy-migrate", help="managed-state policy migration")
    policy.add_argument("target", help="project name")
    policy.add_argument("--execute", action="store_true", help="CAS-advance new policy state")
    policy.add_argument("--yes", action="store_true", help="confirm execute")
    _add_remote_argument(policy)

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
    _add_remote_argument(parser)
    if include_dry_run:
        parser.add_argument("--dry-run", action="store_true", help="preview without remote writes")


def _add_remote_argument(parser: argparse.ArgumentParser) -> None:
    """Add the named remote selector shared by every command.

    Args:
        parser: Subcommand parser receiving the selector.

    Returns:
        None.
    """

    parser.add_argument("--remote", help="named remote environment, for example dev or prod")


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
        if args.command == "state":
            return _run_state(config, args)
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

    With current state, uses state composer/planner (current tree + applied
    transitions as the sole before). Ordinary plan/dry-run stays local-only.
    Real deploy injects the real transport into ``StateDeploymentExecutor``.

    Args:
        config: Loaded application configuration.
        args: Parsed plan or deploy arguments.

    Returns:
        Process exit code.
    """

    from .config import resolve_project_target
    from .expected_state import ExpectedStateStore
    from .gitrepo import GitRepository
    from .remote_verify import open_cli_transport
    from .state_executor import StateDeploymentExecutor
    from .state_guards import StateGuards
    from .git_store import PersistentGitStore
    from .state_planner import SourceDiffPlan, StatePlanner
    from .target_identity import TargetIdentity, default_state_base, policy_fingerprint_for_project

    remote_name, server, available_projects = select_remote(config, args.remote)
    projects = _select_projects(available_projects, args.targets)
    is_plan = args.command == "plan"
    is_dry_run = is_plan or bool(getattr(args, "dry_run", False))
    if args.check_remote and not is_dry_run:
        raise PolicyError("--check-remote is only valid with plan or deploy --dry-run")

    print(f"Config: {config.path}")
    print(f"Remote: {remote_name}")

    # Split projects into stateful (has current) vs first-time legacy path.
    stateful_items: list[
        tuple[
            ProjectConfig,
            TargetIdentity,
            Path,
            SourceDiffPlan,
            dict[str, str] | None,
            PersistentGitStore | None,
        ]
    ] = []
    legacy_items: list[ProjectPlan] = []
    for project in projects:
        # Plan/dry-run may run with incomplete server tables (host deferred to deploy).
        host = str(server.values.get("host", "")).strip()
        if not host and is_dry_run:
            planner = GitDeploymentPlanner(project)
            plan = planner.build_revisions(args.revisions)
            item = ProjectPlan(project, planner, plan)
            _print_plan(plan)
            _print_working_tree_warning(item)
            print("  remote: unverified (local-only plan/dry-run)")
            legacy_items.append(item)
            continue

        identity = resolve_project_target(server, project, config=config)
        base = default_state_base(project.name, project.local_state_dir)
        target_root = identity.state_root(base)
        store = ExpectedStateStore(target_root, identity)
        loaded = store.load_current_state()
        print(f"[{project.name}] target_id={identity.target_id}")

        if loaded is None:
            # No current: keep v0.1.5 revision planning for first-time compatibility.
            # Real first deploy should prefer explicit bootstrap; legacy deploy still works.
            planner = GitDeploymentPlanner(project)
            plan = planner.build_revisions(args.revisions)
            item = ProjectPlan(project, planner, plan)
            _print_plan(plan)
            _print_working_tree_warning(item)
            if is_dry_run:
                print("  remote: unverified (local-only plan/dry-run; no current state)")
            legacy_items.append(item)
            continue

        _pointer, state = loaded
        policy = policy_fingerprint_for_project(project)
        guards = StateGuards(target_root, identity, expected_policy=policy)
        # Integrity/identity/tx gates always run on plan and deploy (force ignored).
        guards.require_clear(force=bool(getattr(args, "force", False)))

        # Plan/dry-run: no durable git_store under target_root (strict zero-write).
        # Real deploy: bind PersistentGitStore so synthetic trees are durable-published.
        git_store: PersistentGitStore | None
        alternate_dirs: list[str] = []
        if is_dry_run:
            git_store = None
            objects = target_root / "git" / "objects"
            if objects.is_dir():
                alternate_dirs.append(str(objects))
        else:
            git_store = PersistentGitStore(target_root, project.repository)
        planner = StatePlanner(
            project.repository,
            include=project.include,
            exclude=project.exclude,
            protected=project.protected,
            remote_root=project.remote_root,
            git_store=git_store,
            alternate_object_dirs=alternate_dirs or None,
        )
        if is_dry_run and alternate_dirs:
            # Seed read env for current synthetic trees without writing target_root.
            try:
                planner._object_env = PersistentGitStore(
                    target_root, project.repository
                ).object_environment()
            except Exception:
                pass
        source_plan = planner.plan_selectors(
            args.revisions,
            current_tree_id=state.source_tree_id,
            applied_transition_ids=state.applied_transition_ids,
            static_only=is_dry_run,
        )
        object_env = planner.object_env()
        print(
            f"  source tree {source_plan.before_tree_id[:12]} -> {source_plan.after_tree_id[:12]}"
        )
        print(f"  introduced transitions: {len(source_plan.introduced_transition_ids)}")
        if source_plan.static_noop:
            print("  static no-op (already applied transitions)")
        if not source_plan.files:
            print("  no selected managed-file changes")
        for operation in source_plan.files:
            if operation.action == "upload":
                print(f"  UPLOAD {operation.path} ({operation.target_size} bytes)")
            else:
                print(f"  DELETE {operation.path}")
        if is_dry_run:
            print("  remote: unverified (local-only plan/dry-run)")
            print("state: no state/CAS/journal/deployment/worktree written")
        # git_store may be None for dry-run; deploy path re-creates it.
        stateful_items.append(
            (project, identity, target_root, source_plan, object_env, git_store)  # type: ignore[arg-type]
        )

    if is_dry_run:
        if args.check_remote:
            for item in legacy_items:
                if not item.plan.files:
                    continue
                with _progress_executor(server, item.project) as executor:
                    checks = executor.check_plan(item.plan, force=args.force)
                _print_checks(item.project.name, checks)
            for project, identity, target_root, source_plan, _env, _gs in stateful_items:
                if not source_plan.files:
                    continue
                legacy = DeploymentPlan(
                    project=project.name,
                    repository=project.repository,
                    remote_root=project.remote_root,
                    from_commit=source_plan.before_tree_id,
                    to_commit=source_plan.after_tree_id,
                    files=tuple(source_plan.files),
                    excluded=tuple(source_plan.excluded),
                    revision_specs=tuple(args.revisions),
                )
                with _progress_executor(server, project) as executor:
                    checks = executor.check_plan(legacy, force=args.force)
                _print_checks(project.name, checks)
        else:
            print("Remote: not connected (local-only dry run)")
            print("state: no state/CAS/journal/deployment/worktree written")
        return 0

    actionable_legacy = [item for item in legacy_items if item.plan.files]
    actionable_state = [
        item
        for item in stateful_items
        if item[3].files or item[3].introduced_transition_ids
    ]
    if not actionable_legacy and not actionable_state:
        print("No selected tracked-file changes; nothing deployed.")
        return 0
    _confirm(
        args.yes,
        f"Deploy {len(actionable_legacy) + len(actionable_state)} project(s) "
        f"to remote {remote_name}?",
    )

    exit_code = 0
    for item in actionable_legacy:
        with _progress_executor(server, item.project) as executor:
            manifest = executor.deploy(item.plan, item.planner, force=args.force)
        print(f"[{item.project.name}] deployed: {manifest.deployment_id}")

    for project, identity, target_root, source_plan, object_env, git_store in actionable_state:
        repo = GitRepository(project.repository)

        def _content(
            path: str,
            *,
            _repo: GitRepository = repo,
            _tree: str = source_plan.after_tree_id,
            _env: dict[str, str] | None = object_env,
        ) -> bytes:
            blob = _repo.blob(_tree, path, env=_env)
            if blob is None:
                return b""
            return blob.data

        transport = open_cli_transport(dict(server.values))
        try:
            executor = StateDeploymentExecutor(
                project,
                identity,
                target_root,
                transport=transport,
                content_provider=_content,
            )
            result = executor.deploy(
                source_plan,
                list(source_plan.files),
                force=bool(getattr(args, "force", False)),
            )
        finally:
            close = getattr(transport, "close", None)
            if callable(close):
                close()
        status = result.get("status", "unknown")
        deployment_id = result.get("deployment_id", "")
        generation = result.get("generation")
        print(
            f"[{project.name}] {status}"
            + (f": {deployment_id}" if deployment_id else "")
            + (f" generation={generation}" if generation is not None else "")
        )
        if result.get("restored") or result.get("ok") is False or status == "restored":
            # Auto-restored deploy is still a failed deploy for CI/operators.
            error = result.get("error") or "deploy restored after failure"
            print(f"[{project.name}] deploy failed: {error}", file=sys.stderr)
            exit_code = 3
        elif status not in {"succeeded", "already deployed", "reconciled"}:
            exit_code = 3
    return exit_code


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
    remote_name, server, available_projects = select_remote(config, args.remote)
    print(f"Remote: {remote_name}")
    for project in _select_projects(available_projects, [args.target]):
        from .config import resolve_project_target
        from .target_identity import default_state_base

        identity = resolve_project_target(server, project)
        # Prefer target-scoped deployment store (stateful v0.2); fall back to
        # legacy project store so pre-state history remains visible.
        target_root = identity.state_root(default_state_base(project.name, project.local_state_dir))
        target_store = DeploymentStore(project, root=target_root)
        legacy_store = DeploymentStore(project)
        by_id: dict[str, DeploymentManifest] = {}
        for manifest in legacy_store.list_manifests():
            by_id[manifest.deployment_id] = manifest
        for manifest in target_store.list_manifests():
            by_id[manifest.deployment_id] = manifest
        manifests = sorted(
            by_id.values(),
            key=lambda item: item.deployment_id,
            reverse=True,
        )[: args.limit]
        print(f"[{project.name}] {len(manifests)} deployment record(s)")
        print(f"  target_id: {identity.target_id}")
        for manifest in manifests:
            selection = " ".join(manifest.revision_specs) or (
                f"{manifest.from_commit[:12]}..{manifest.to_commit[:12]}"
            )
            lineage = manifest.lineage_label()
            state_note = f"state: {lineage}"
            if lineage == "v1":
                state_note += (
                    f" gen={manifest.before_generation}->{manifest.after_generation}"
                    f" target={manifest.target_id or identity.target_id}"
                )
            print(
                f"  {manifest.deployment_id}  {manifest.status:<18}  "
                f"{selection}  "
                f"{len(manifest.snapshots)} file(s)  {state_note}"
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

    remote_name, server, available_projects = select_remote(config, args.remote)
    print(f"Remote: {remote_name}")
    projects = _select_projects(available_projects, [args.target])
    for project in projects:
        from .config import resolve_project_target
        from .target_identity import default_state_base

        identity = resolve_project_target(server, project)
        target_root = identity.state_root(default_state_base(project.name, project.local_state_dir))
        manifest = _select_manifest(
            project,
            args.deployment,
            args.latest,
            len(projects) > 1,
            target_root=target_root,
        )
        with _progress_executor(server, project) as executor:
            # Point legacy executor store at target-scoped root when present.
            if (target_root / "deployments").is_dir():
                executor.store = DeploymentStore(project, root=target_root)
            checks = executor.verify(manifest)
        _print_checks(project.name, checks)
    return 0


def _run_rollback(config: AppConfig, args: argparse.Namespace) -> int:
    """Preview, verify, or execute deployment rollback.

    With current state: non-latest is refused before transport factory/connect;
    latest uses ``StateRollbackService`` and advances an auditable generation.
    Without current: keep v0.1.5 legacy rollback compatibility.

    Args:
        config: Loaded application configuration.
        args: Parsed rollback arguments.

    Returns:
        Process exit code.
    """

    from .config import resolve_project_target
    from .expected_state import ExpectedStateStore
    from .remote_verify import open_cli_transport
    from .state_rollback import StateRollbackService
    from .target_identity import TargetIdentity, default_state_base

    remote_name, server, available_projects = select_remote(config, args.remote)
    print(f"Remote: {remote_name}")
    projects = _select_projects(available_projects, [args.target])

    stateful: list[
        tuple[
            ProjectConfig,
            TargetIdentity,
            Path,
            StateRollbackService,
            DeploymentManifest,
        ]
    ] = []
    legacy: list[tuple[ProjectConfig, DeploymentManifest]] = []
    for project in projects:
        identity = resolve_project_target(server, project, config=config)
        base = default_state_base(project.name, project.local_state_dir)
        target_root = identity.state_root(base)
        store = ExpectedStateStore(target_root, identity)
        loaded = store.load_current_state()
        if loaded is not None:
            service = StateRollbackService(project, identity, target_root)
            # Resolve deployment selection against target-scoped store.
            if args.latest or not args.deployment:
                manifest = service.deploy_store.latest_successful()
            else:
                if len(projects) > 1:
                    raise ConfigurationError(
                        "--deployment cannot be combined with all; use --latest"
                    )
                manifest = service.deploy_store.load(args.deployment)
            # Non-latest + after_state/current match + v1 before readability
            # all run before any transport factory/connect.
            service.assert_latest_only(manifest.deployment_id)
            service.assert_rollback_eligible(manifest)
            _print_rollback(project.name, manifest)
            stateful.append((project, identity, target_root, service, manifest))
        else:
            manifest = _select_manifest(
                project,
                args.deployment,
                args.latest,
                len(projects) > 1,
                target_root=target_root,
            )
            _print_rollback(project.name, manifest)
            legacy.append((project, manifest))

    if args.check_remote and not args.dry_run:
        raise PolicyError("--check-remote requires --dry-run")
    if args.dry_run:
        if args.check_remote:
            for project, manifest in legacy:
                with _progress_executor(server, project) as executor:
                    checks = executor.check_rollback(manifest, force=args.force)
                _print_checks(project.name, checks)
            if stateful:
                print(
                    "Remote: stateful rollback dry-run "
                    "(no connect; latest-only and eligibility gates checked)"
                )
        else:
            print("Remote: not connected (local-only dry run)")
        return 0

    _confirm(
        args.yes,
        f"Rollback {len(stateful) + len(legacy)} project(s) on remote {remote_name}?",
    )
    for project, identity, target_root, service, manifest in stateful:
        # Re-check eligibility after confirm (current may have moved); still
        # before factory so repeat/advanced stay factory=0.
        service.assert_rollback_eligible(manifest)
        transport = open_cli_transport(dict(server.values))
        try:
            service.transport = transport  # type: ignore[assignment]
            result = service.rollback_latest()
        finally:
            close = getattr(transport, "close", None)
            if callable(close):
                close()
        print(
            f"[{project.name}] rolled back (stateful): generation={result.generation} "
            f"paths={len(result.restored_paths)}"
        )
    for project, manifest in legacy:
        with _progress_executor(server, project) as executor:
            result = executor.rollback(manifest, force=args.force)
        print(f"[{project.name}] rolled back: {result.deployment_id}")
    return 0


def _run_state(config: AppConfig, args: argparse.Namespace) -> int:
    """Dispatch ``git-deploy state`` subcommands.

    Args:
        config: Loaded application configuration.
        args: Parsed state arguments.

    Returns:
        Process exit code.
    """

    command = args.state_command
    if command == "gc":
        raise ConfigurationError(
            "state gc is not supported in v0.2; all state/CAS/Git objects are retained"
        )

    remote_name, server, available_projects = select_remote(config, getattr(args, "remote", None))
    projects = _select_projects(available_projects, [args.target])
    if len(projects) != 1:
        raise ConfigurationError("state commands require exactly one project")
    project = projects[0]
    from .config import resolve_project_target
    from .target_identity import default_state_base

    identity = resolve_project_target(server, project)
    base = default_state_base(project.name, project.local_state_dir)
    target_root = identity.state_root(base)

    if command == "inspect":
        return _state_inspect(project, identity, target_root, remote_name)
    if command == "verify":
        remote_check = bool(getattr(args, "state_remote_check", False))
        return _state_verify(
            project,
            identity,
            target_root,
            remote_name,
            server,
            remote_check=remote_check,
        )
    if command == "bootstrap":
        return _state_bootstrap(project, identity, target_root, server, args)
    if command == "recover":
        return _state_recover(project, identity, target_root, server, args)
    if command == "migrate":
        return _state_migrate(project, config, server, remote_name, args)
    if command == "policy-migrate":
        return _state_policy_migrate(project, identity, target_root, server, args)
    raise ConfigurationError(f"unsupported state command: {command}")


def _state_inspect(project: ProjectConfig, identity, target_root, remote_name: str) -> int:
    """Print local state summary without remote calls or writes.

    Args:
        project: Project configuration.
        identity: Physical target identity.
        target_root: Target state root.
        remote_name: Selected remote alias.

    Returns:
        Exit code.
    """

    from .expected_state import ExpectedStateStore
    from .transaction import TransactionStore

    store = ExpectedStateStore(target_root, identity)
    tx = TransactionStore(target_root)
    print(f"Remote alias: {remote_name}")
    print(f"Physical target ID: {identity.target_id}")
    print("Policy fingerprint: (see current state)")
    loaded = store.load_current_state()
    if loaded is None:
        print("Generation: (none)")
        print("Current state: absent")
    else:
        pointer, state = loaded
        print(f"Generation: {pointer.generation}")
        print(f"State ID: {pointer.state_id}")
        print(f"Source tree: {state.source_tree_id}")
        print(f"Applied transitions: {len(state.applied_transition_ids)}")
        print(f"Policy fingerprint: {state.policy_fingerprint}")
        print(f"Physical fingerprint: {state.physical_fingerprint}")
        print(f"Files: {len(state.files)}")
    open_tx = tx.list_open()
    print(f"Open transactions: {len(open_tx)}")
    for journal in open_tx:
        print(f"  {journal.transaction_id} stage={journal.stage}")
    migration = target_root / "migration.json"
    print(f"Legacy migration record: {'present' if migration.is_file() else 'absent'}")
    print("Remote: not connected (state inspect is local-only)")
    return 0


def _state_verify(
    project: ProjectConfig,
    identity,
    target_root,
    remote_name: str,
    server: ServerConfig,
    *,
    remote_check: bool,
) -> int:
    """Verify local integrity, optionally read-only remote match.

    Args:
        project: Project configuration.
        identity: Physical identity.
        target_root: Target root.
        remote_name: Remote alias.
        server: Selected remote settings used when ``remote_check`` is set.
        remote_check: When true, compare remote paths read-only.

    Returns:
        Exit code.
    """

    from .expected_state import ExpectedStateStore
    from .object_store import ContentAddressedStore
    from .remote_verify import open_cli_transport, verify_remote_current

    store = ExpectedStateStore(target_root, identity)
    cas = ContentAddressedStore(target_root)
    loaded = store.load_current_state()
    print(f"Remote alias: {remote_name}")
    print(f"Target ID: {identity.target_id}")
    if loaded is None:
        print("state_verify_local: no current state")
        return 0
    pointer, state = loaded
    # Re-hash state (read_state already validates).
    store.read_state(pointer.state_id)
    print(f"state_verify_local: current generation {pointer.generation} ok")
    print(f"state_verify_local: physical fingerprint {state.physical_fingerprint[:12]}...")
    from .state_guards import StateGuards
    from .target_identity import policy_fingerprint_for_project

    # Unconditional integrity: CAS + Git store/tree (missing store fails closed).
    guards = StateGuards(
        target_root,
        identity,
        expected_policy=policy_fingerprint_for_project(project),
    )
    report = guards.check()
    if not report.ok:
        for reason in report.reasons:
            print(f"state_verify_local: FAIL {reason}", file=sys.stderr)
        return 3
    for entry in state.files:
        if not entry.exists or not entry.content_sha256:
            continue
        cas.get(entry.content_sha256)
    print("state_verify_local: local integrity checks passed")
    if not remote_check:
        print("Remote: not connected")
        return 0

    transport = open_cli_transport(dict(server.values))
    try:
        writes_before = getattr(transport, "write_calls", 0)
        report = verify_remote_current(state, project, transport)
        writes_after = getattr(transport, "write_calls", 0)
        print(f"state_verify_remote: write_calls={writes_after - writes_before}")
        print(f"state_verify_remote: read_calls={report.read_calls}")
        for item in report.results:
            print(
                f"  {item.status} {item.path}: "
                f"expected={item.expected_sha256} actual={item.actual_sha256}"
            )
        if not report.ok:
            return 3
    finally:
        close = getattr(transport, "close", None)
        if callable(close):
            close()
    return 0


def _state_bootstrap(
    project: ProjectConfig,
    identity,
    target_root,
    server: ServerConfig,
    args: argparse.Namespace,
) -> int:
    """Plan or execute generation-1 bootstrap with remote path verification.

    Args:
        project: Project configuration.
        identity: Physical identity.
        target_root: Target root.
        server: Remote settings for read-only verification.
        args: CLI args.

    Returns:
        Exit code.
    """

    from .remote_verify import open_cli_transport
    from .state_bootstrap import StateBootstrapService

    service = StateBootstrapService(project, identity, target_root)
    if args.empty and args.revision:
        raise ConfigurationError("use either --revision or --empty, not both")
    if not args.empty and not args.revision:
        raise ConfigurationError("bootstrap requires --revision or --empty")
    if args.empty:
        # Derive full managed destination set from source/artifact policy.
        plan = service.plan_empty(dry_run=args.dry_run, managed_paths=None)
    else:
        plan = service.plan_inferred(args.revision, dry_run=args.dry_run)
    print(f"bootstrap mode={plan.mode} generation={plan.generation} tree={plan.source_tree_id[:12]}")
    print(f"applied transitions: {len(plan.applied_transition_ids)}")
    print(f"managed paths to verify: {len(plan.managed_paths)}")
    if args.dry_run or not args.yes:
        print("bootstrap dry-run/local plan only; no current written")
        return 0
    # Prepare Git store before remote verify; final tree precommit runs under the
    # same target lock immediately before CAS (no post-current require_tree).
    from .git_store import PersistentGitStore

    try:
        git_store = PersistentGitStore(target_root, project.repository)
        git_store.ensure_layout()
        git_store._publish_repository_identity()
        git_store.require_tree(plan.source_tree_id)
    except GitDeployError:
        raise
    except Exception as exc:
        raise ConfigurationError(
            f"bootstrap refused: cannot establish durable git store for source tree: {exc}"
        ) from exc

    def _precommit_git_tree(bootstrap_plan: object) -> None:
        """Final Git tree validation under bootstrap lock, immediately before CAS.

        Args:
            bootstrap_plan: Bootstrap plan whose source_tree_id must remain readable.

        Returns:
            None.
        """

        tree_id = getattr(bootstrap_plan, "source_tree_id", plan.source_tree_id)
        try:
            git_store.require_tree(str(tree_id))
        except GitDeployError:
            raise
        except Exception as exc:
            raise ConfigurationError(
                f"bootstrap refused: final precommit tree validation failed: {exc}"
            ) from exc

    transport = open_cli_transport(dict(server.values))
    try:
        write_counter = [getattr(transport, "write_calls", 0)]
        try:
            state = service.execute(
                plan,
                yes=True,
                transport=transport,
                write_counter=write_counter,
                precommit_validator=_precommit_git_tree,
            )
        except GitDeployError:
            raise
        except Exception as exc:
            raise ConfigurationError(
                f"bootstrap refused: cannot publish generation-1 current: {exc}"
            ) from exc
    finally:
        close = getattr(transport, "close", None)
        if callable(close):
            close()
    # Success path: current is durable; no further integrity step that can fail
    # without undoing the visible generation-1 current.
    print(f"bootstrap wrote generation {state.generation} state {state.state_id()}")
    return 0


def _state_recover(
    project: ProjectConfig,
    identity,
    target_root,
    server: ServerConfig,
    args: argparse.Namespace,
) -> int:
    """Show or execute recovery decisions without leaking secrets.

    Execute path opens transport, classifies remote against journal
    before/after/current, and passes real restore/finalize callbacks.

    Args:
        project: Project configuration.
        identity: Physical identity.
        target_root: Target root.
        server: Remote server settings.
        args: CLI args.

    Returns:
        Exit code.
    """

    from .expected_state import ExpectedStateStore
    from .remote_verify import open_cli_transport, remote_path_for
    from .state import DeploymentStore
    from .state_executor import StateDeploymentExecutor
    from .transaction_recovery import TransactionRecoveryService

    service = TransactionRecoveryService(target_root, identity)
    open_tx = service.tx.list_open()
    if not open_tx:
        print("state_recover: no open transactions")
        return 0

    state_store = ExpectedStateStore(target_root, identity)
    deploy_store = DeploymentStore(project, root=target_root)

    def _classify(journal, transport) -> dict[str, bool]:
        """Classify remote vs journal before/after/current using durable evidence.

        Args:
            journal: Open transaction journal.
            transport: Connected transport.

        Returns:
            Match flags for recovery decide.
        """

        flags = {
            "remote_matches_current": False,
            "remote_matches_target": False,
            "remote_third": False,
        }
        before_state = None
        after_state = None
        if journal.before_state_id:
            try:
                before_state = state_store.read_state(journal.before_state_id)
            except Exception:
                before_state = None
        if journal.after_state_id:
            try:
                after_state = state_store.read_state(journal.after_state_id)
            except Exception:
                after_state = None
        loaded_current = state_store.load_current_state()
        current_state = loaded_current[1] if loaded_current is not None else None
        # Prefer structured backup_entries paths; fall back to after/before files.
        entries = list(journal.meta.get("backup_entries") or [])
        paths: list[tuple[str, str]] = []
        if entries:
            for entry in entries:
                paths.append((str(entry["path"]), str(entry["remote_path"])))
        elif after_state is not None:
            for file_entry in after_state.files:
                paths.append((file_entry.path, remote_path_for(project, file_entry.path)))
        elif before_state is not None:
            for file_entry in before_state.files:
                paths.append((file_entry.path, remote_path_for(project, file_entry.path)))
        if not paths:
            flags["remote_third"] = True
            return flags

        match_before = True
        match_after = True
        match_current = True
        for rel, remote in paths:
            actual = transport.read_file(remote)
            import hashlib

            actual_hash = hashlib.sha256(actual).hexdigest() if actual is not None else None

            def _expected_hash(state_obj, relative: str) -> str | None:
                if state_obj is None:
                    return None
                for item in state_obj.files:
                    if item.path == relative:
                        return item.content_sha256 if item.exists else None
                return None

            before_hash = _expected_hash(before_state, rel)
            after_hash = _expected_hash(after_state, rel)
            current_hash = _expected_hash(current_state, rel)
            # Also accept journal backup before_exists mapping when states missing.
            if before_state is None and entries:
                for entry in entries:
                    if str(entry.get("path")) == rel:
                        if entry.get("before_exists") and entry.get("backup_file"):
                            try:
                                data = deploy_store.read_backup(
                                    str(journal.deployment_id), str(entry["backup_file"])
                                )
                                before_hash = hashlib.sha256(data).hexdigest()
                            except Exception:
                                before_hash = None
                        else:
                            before_hash = None
            if actual_hash != before_hash:
                match_before = False
            if actual_hash != after_hash:
                match_after = False
            if actual_hash != current_hash:
                match_current = False
        if match_before or match_current:
            flags["remote_matches_current"] = True
        if match_after:
            flags["remote_matches_target"] = True
        if not match_before and not match_after and not match_current:
            flags["remote_third"] = True
        return flags

    for journal in open_tx:
        if not args.execute:
            decision = service.decide_for_journal(journal)
            print(
                f"transaction {journal.transaction_id}: stage={journal.stage} "
                f"decision={decision.decision} reason={decision.reason}"
            )
            if decision.decision == "manual":
                print(
                    "  manual_recovery_required: inspect journal and backups; no secrets printed"
                )
            continue

        if not args.yes:
            raise PolicyError("state recover --execute requires --yes")

        if journal.stage == "prepared" and journal.meta.get("kind") in {
            "bootstrap",
            "state_only",
        }:
            # These transaction kinds have no remote mutation at prepared.
            # Their recovery is decided entirely from durable current/staged state;
            # do not make local publication recovery depend on remote availability.
            decision = service.decide_for_journal(journal)
            print(
                f"transaction {journal.transaction_id}: stage={journal.stage} "
                f"decision={decision.decision} reason={decision.reason}"
            )
            service.execute(decision, journal)
            continue

        transport = open_cli_transport(dict(server.values))
        try:
            flags = _classify(journal, transport)
            decision = service.decide_for_journal(journal, **flags)
            print(
                f"transaction {journal.transaction_id}: stage={journal.stage} "
                f"decision={decision.decision} reason={decision.reason}"
            )
            if decision.decision == "manual":
                print(
                    "  manual_recovery_required: inspect journal and backups; no secrets printed"
                )
                service.execute(decision, journal)
                continue

            executor = StateDeploymentExecutor(
                project,
                identity,
                target_root,
                transport=transport,  # type: ignore[arg-type]
            )

            def restore_callback(j, *, _executor=executor):
                _executor.restore_from_backups(j)

            def finalize_callback(j, *, _executor=executor, _store=state_store):
                # Re-check identity/generation/after then complete idempotently.
                if j.after_state_id:
                    after = _store.read_state(j.after_state_id)
                    if after.physical_fingerprint != identity.physical_fingerprint:
                        raise PolicyError("finalize refused: identity mismatch")
                    current = _store.read_current()
                    expected_gen = j.before_generation
                    if current is not None and expected_gen is not None:
                        if current.generation not in {expected_gen, j.after_generation}:
                            raise PolicyError("finalize refused: generation race")
                    if current is None or (
                        j.after_generation is not None
                        and current.generation != j.after_generation
                    ):
                        _store.cas_advance(
                            expected_generation=j.before_generation,
                            state=after,
                        )

            updated = service.execute(
                decision,
                journal,
                restore_callback=restore_callback,
                finalize_callback=finalize_callback,
            )
            print(f"  executed -> stage={updated.stage}")
        finally:
            close = getattr(transport, "close", None)
            if callable(close):
                close()
    return 0


def _state_migrate(project: ProjectConfig, config: AppConfig, server, remote_name: str, args) -> int:
    """Plan/stage/publish legacy history migration.

    Args:
        project: Project configuration.
        config: App config.
        server: Selected server (unused for multi-alias plan uses all remotes).
        remote_name: Selected remote.
        args: CLI args.

    Returns:
        Exit code.
    """

    from .config import resolve_project_target
    from .state_migration import StateMigrationService
    from .target_identity import default_state_base

    base = default_state_base(project.name, project.local_state_dir)
    # Map all configured remotes for this project.
    alias_map = {}
    for name in config.remotes:
        _, remote_server, projects = select_remote(config, name)
        resolved = projects[project.name]
        alias_map[name] = resolve_project_target(remote_server, resolved)
    if "default" not in alias_map and len(config.remotes) == 1:
        only = next(iter(config.remotes))
        alias_map["default"] = alias_map[only]
    svc = StateMigrationService(base)
    plan = svc.plan(alias_map)
    print(f"migration plan items={len(plan.items)} blocked={plan.blocked}")
    for reason in plan.reasons:
        print(f"  block: {reason}")
    if plan.blocked:
        return 2
    if args.stage or args.yes:
        staging = base / ".migration-staging"
        svc.stage(plan, staging)
        print(f"staging ready: {staging}")
        if args.yes:
            svc.publish(staging, yes=True)
            print("migration published; legacy evidence retained")
    return 0


def _state_policy_migrate(
    project: ProjectConfig,
    identity,
    target_root,
    server: ServerConfig,
    args,
) -> int:
    """Plan or execute managed policy migration.

    Args:
        project: Project configuration.
        identity: Physical identity.
        target_root: Target root.
        server: Selected remote server for read-only verify.
        args: CLI args.

    Returns:
        Exit code.
    """

    from .remote_verify import open_cli_transport
    from .state_planner import StatePlanner
    from .state_policy_migration import PolicyMigrationService
    from .target_identity import policy_fingerprint_for_project

    from .git_store import PersistentGitStore

    svc = PolicyMigrationService(target_root, identity)
    new_policy = policy_fingerprint_for_project(project)
    # Old paths: current expected-state managed table.
    # New paths + full file entries: recompute under the *new* policy from
    # the durable current source tree (not a copy of old state.files).
    loaded = svc.store.load_current_state()
    old_paths: tuple[str, ...] = ()
    new_paths: tuple[str, ...] = ()
    new_entries: tuple = ()
    if loaded is not None:
        _pointer, state = loaded
        old_paths = tuple(sorted({entry.path for entry in state.files}))
        git_store = PersistentGitStore(target_root, project.repository)
        planner = StatePlanner(
            project.repository,
            include=project.include,
            exclude=project.exclude,
            protected=project.protected,
            remote_root=project.remote_root,
            git_store=git_store,
        )
        new_entries = planner.file_entries_for_tree(state.source_tree_id)
        new_paths = tuple(sorted({entry.path for entry in new_entries}))
    plan = svc.plan(
        new_policy=new_policy,
        old_managed_paths=old_paths,
        new_managed_paths=new_paths,
        new_file_entries=new_entries,
    )
    print(f"policy plan old={plan.old_policy[:12]} new={plan.new_policy[:12]}")
    print(f"readonly verify paths: {len(plan.readonly_verify_paths)}")
    if not args.execute:
        print("normal deploy remains blocked on policy mismatch until execute")
        return 0
    transport = open_cli_transport(dict(server.values))
    try:
        write_counter = [getattr(transport, "write_calls", 0)]
        state_id = svc.execute(
            plan,
            yes=args.yes,
            transport=transport,
            project=project,
            remote_write_counter=write_counter,
        )
    finally:
        close = getattr(transport, "close", None)
        if callable(close):
            close()
    print(f"policy migrated to state {state_id}")
    return 0


@contextmanager
def _progress_executor(
    server: ServerConfig,
    project: ProjectConfig,
) -> Iterator[DeploymentExecutor]:
    """Create an executor whose progress line is always finalized.

    Args:
        server: Selected remote connection configuration.
        project: Project being checked, deployed, verified, or rolled back.

    Yields:
        Progress-enabled deployment executor.
    """

    renderer = TerminalProgress(project.name)
    try:
        yield DeploymentExecutor(
            project,
            dict(server.values),
            progress_callback=renderer.update,
        )
    finally:
        renderer.finish()


def _select_projects(
    projects: dict[str, ProjectConfig],
    targets: Sequence[str],
) -> list[ProjectConfig]:
    """Expand ``all`` or validate an ordered project selection.

    Args:
        projects: Projects resolved for the selected remote.
        targets: Positional project names.

    Returns:
        Ordered unique project configurations.
    """

    if "all" in targets:
        if len(targets) != 1:
            raise ConfigurationError("all cannot be combined with explicit project names")
        return list(projects.values())
    selected: list[ProjectConfig] = []
    seen: set[str] = set()
    for name in targets:
        if name in seen:
            continue
        try:
            selected.append(projects[name])
        except KeyError as exc:
            available = ", ".join(projects)
            raise ConfigurationError(f"unknown project {name!r}; available: {available}") from exc
        seen.add(name)
    return selected


def _select_manifest(
    project: ProjectConfig,
    deployment_id: str | None,
    latest: bool,
    multiple_projects: bool,
    *,
    target_root: Path | None = None,
) -> DeploymentManifest:
    """Load one project deployment record from CLI selectors.

    Prefers the physical target-scoped deployment store (v0.2 stateful path),
    then falls back to the legacy project-level store.

    Args:
        project: Project owning the local history.
        deployment_id: Explicit identifier or prefix.
        latest: Select the latest successful record.
        multiple_projects: Whether ``all`` expanded to multiple projects.
        target_root: Optional physical target root for stateful manifests.

    Returns:
        Selected deployment manifest.
    """

    stores: list[DeploymentStore] = []
    if target_root is not None:
        stores.append(DeploymentStore(project, root=target_root))
    stores.append(DeploymentStore(project))
    if latest:
        last_error: Exception | None = None
        for store in stores:
            try:
                return store.latest_successful()
            except ConfigurationError as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise ConfigurationError(f"no successful deployment found for project {project.name}")
    if multiple_projects:
        raise ConfigurationError("--deployment cannot be combined with all; use --latest")
    assert deployment_id is not None
    last_error = None
    for store in stores:
        try:
            return store.load(deployment_id)
        except ConfigurationError as exc:
            last_error = exc
            continue
    assert last_error is not None
    raise last_error


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
