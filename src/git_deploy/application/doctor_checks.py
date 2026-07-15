"""Concrete zero-write checks used by the standard doctor command."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path

from git_deploy.expected_state import ExpectedStateStore
from git_deploy.models import DeploymentManifest
from git_deploy.object_store import ContentAddressedStore
from git_deploy.remote_verify import open_cli_transport
from git_deploy.state import DeploymentStore
from git_deploy.target_identity import default_state_base, policy_fingerprint_for_project
from git_deploy.transaction import TransactionStore

from .config_service import ApplicationConfigService
from .doctor_service import (
    DoctorCheckCategory,
    DoctorCheckResult,
    DoctorCheckStatus,
    DoctorRequest,
)
from .models import SideEffectLevel
from .policy import EnvironmentRisk


class ProjectDoctorChecks:
    """Read configuration and Git metadata without changing local or remote state."""

    def __init__(self, config: ApplicationConfigService):
        """Bind the parsed configuration shared by all concrete checks."""

        if not isinstance(config, ApplicationConfigService):
            raise TypeError("config must be an ApplicationConfigService")
        self.config = config

    def configuration(self, _request: DoctorRequest) -> DoctorCheckResult:
        """Check the selected TOML path and report secret-bearing field names only."""

        path = self.config.config_path
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        sensitive = tuple(sorted(_plaintext_sensitive_fields(raw)))
        tracked = _git_tracked(path)
        status = DoctorCheckStatus.WARN if sensitive or tracked else DoctorCheckStatus.PASS
        reasons: list[str] = []
        if sensitive:
            reasons.append("plaintext-sensitive fields are present")
        if tracked:
            reasons.append("configuration is tracked by Git")
        summary = "; ".join(reasons) if reasons else "configuration is readable and valid"
        return DoctorCheckResult.create(
            check_id="local.configuration",
            category=DoctorCheckCategory.LOCAL,
            status=status,
            summary=summary,
            side_effect=SideEffectLevel.LOCAL_READ,
            context={
                "config_path": str(path),
                "git_tracked": tracked,
                "sensitive_field_names": sensitive,
            },
            suggested_action=(
                "replace plaintext secret fields with environment or provider references"
                if sensitive
                else None
            ),
        )

    def repository(self, request: DoctorRequest) -> DoctorCheckResult:
        """Check Git availability, repositories, HEAD, shallow state, and dirty hints."""

        if shutil.which("git") is None:
            return DoctorCheckResult.create(
                check_id="local.git",
                category=DoctorCheckCategory.LOCAL,
                status=DoctorCheckStatus.FAIL,
                summary="Git executable is not available",
                side_effect=SideEffectLevel.LOCAL_READ,
                suggested_action="install Git and rerun doctor",
            )
        projects = self._projects(request)
        failures: list[str] = []
        warnings: list[str] = []
        heads: list[tuple[str, str]] = []
        for name in projects:
            _alias, _server, project, _identity = self.config._resolve_domain_project(
                request.remote,
                name,
            )
            repository = project.repository
            if not repository.is_dir():
                failures.append(f"{name}: repository path does not exist")
                continue
            head = _git(repository, "rev-parse", "--verify", "HEAD^{commit}")
            if head.returncode != 0:
                failures.append(f"{name}: repository or HEAD is invalid")
                continue
            heads.append((name, head.stdout.strip()))
            shallow = _git(repository, "rev-parse", "--is-shallow-repository")
            if shallow.returncode == 0 and shallow.stdout.strip() == "true":
                warnings.append(f"{name}: shallow history may hide current ancestors")
            dirty = _git(repository, "status", "--porcelain", "--untracked-files=no")
            if dirty.returncode == 0 and dirty.stdout.strip():
                warnings.append(f"{name}: tracked working tree changes are ignored by deploy")
        status = DoctorCheckStatus.PASS
        if failures:
            status = DoctorCheckStatus.FAIL
        elif warnings:
            status = DoctorCheckStatus.WARN
        summary = "Git repositories and HEAD are readable"
        if failures:
            summary = "; ".join(failures)
        elif warnings:
            summary = "; ".join(warnings)
        return DoctorCheckResult.create(
            check_id="local.git",
            category=DoctorCheckCategory.LOCAL,
            status=status,
            summary=summary,
            side_effect=SideEffectLevel.LOCAL_READ,
            context={"heads": heads, "projects": projects},
            suggested_action=(
                "fix repository paths or fetch the required Git history, then rerun doctor"
                if failures
                else None
            ),
        )

    def selection(self, request: DoctorRequest) -> DoctorCheckResult:
        """Resolve projects, remote alias, and stable physical target identities."""

        projects = self._projects(request)
        selections = tuple(
            self.config.resolve_project(request.remote, project) for project in projects
        )
        aliases = tuple(dict.fromkeys(item.remote_alias for item in selections))
        production_default = (
            any(
                item.environment_risk is EnvironmentRisk.PRODUCTION
                for item in selections
            )
            and request.remote is None
        )
        return DoctorCheckResult.create(
            check_id="local.selection",
            category=DoctorCheckCategory.LOCAL,
            status=(
                DoctorCheckStatus.WARN
                if production_default
                else DoctorCheckStatus.PASS
            ),
            summary=(
                "default remote resolves to a high-risk environment"
                if production_default
                else "project, remote, and physical target selection is stable"
            ),
            side_effect=SideEffectLevel.LOCAL_READ,
            context={
                "remote_aliases": aliases,
                "projects": projects,
                "target_ids": tuple(item.target_id for item in selections),
            },
            suggested_action=(
                "pass --remote explicitly for high-risk environments"
                if production_default
                else None
            ),
        )

    def current_state(self, request: DoctorRequest) -> DoctorCheckResult:
        """Validate current pointer/state, CAS bytes, identity, policy, and Git tree."""

        failures: list[str] = []
        missing: list[str] = []
        generations: list[tuple[str, int]] = []
        for name, project, identity, target_root in self._targets(request):
            store = ExpectedStateStore(target_root, identity)
            try:
                loaded = store.load_current_state()
            except Exception as exc:
                failures.append(f"{name}: {exc}")
                continue
            if loaded is None:
                missing.append(name)
                continue
            pointer, state = loaded
            if pointer.target_id != identity.target_id:
                failures.append(f"{name}: current target identity mismatch")
            if pointer.generation != state.generation or state.generation < 1:
                failures.append(f"{name}: current generation is inconsistent")
            if state.physical_fingerprint != identity.physical_fingerprint:
                failures.append(f"{name}: current physical fingerprint mismatch")
            if state.policy_fingerprint != policy_fingerprint_for_project(project):
                failures.append(f"{name}: current managed policy fingerprint mismatch")
            cas = ContentAddressedStore(target_root)
            for entry in state.files:
                if not entry.exists or entry.content_sha256 is None:
                    continue
                try:
                    cas.get(entry.content_sha256)
                except Exception as exc:
                    failures.append(f"{name}: {entry.path}: {exc}")
            if not _tree_readable(project.repository, target_root, state.source_tree_id):
                failures.append(f"{name}: current source tree object is unreadable")
            generations.append((name, state.generation))
        status = DoctorCheckStatus.PASS
        if failures or missing:
            status = DoctorCheckStatus.FAIL
        summary = "current state, CAS, and Git tree are internally consistent"
        if failures:
            summary = "; ".join(failures)
        elif missing:
            summary = f"trusted current state is missing for: {', '.join(missing)}"
        return DoctorCheckResult.create(
            check_id="state.current",
            category=DoctorCheckCategory.STATE,
            status=status,
            summary=summary,
            side_effect=SideEffectLevel.LOCAL_READ,
            context={"generations": generations, "missing_projects": missing},
            suggested_action=(
                "run state inspect, then bootstrap a verified commit or empty target"
                if failures or missing
                else None
            ),
        )

    def manifests(self, request: DoctorRequest) -> DoctorCheckResult:
        """Parse every manifest directory and verify required backup bytes and hashes."""

        failures: list[str] = []
        counts: list[tuple[str, int]] = []
        rollback_event_counts: list[tuple[str, int]] = []
        for name, project, _identity, target_root in self._targets(request):
            deployment_root = target_root / "deployments"
            count = 0
            rollback_events = 0
            for directory in sorted(deployment_root.glob("*")):
                if not directory.is_dir():
                    continue
                if directory.name.startswith("rb-"):
                    # P1-04: rollback recovery evidence (pre-rollback backup
                    # bytes captured by StateRollbackService), not a deployment
                    # record — it has no manifest.json by design.
                    rollback_events += 1
                    continue
                count += 1
                manifest_path = directory / "manifest.json"
                try:
                    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("manifest root is not an object")
                    manifest = DeploymentManifest.from_dict(payload)
                    if manifest.deployment_id != directory.name:
                        raise ValueError("deployment ID does not match its directory")
                    if manifest.status not in {
                        "succeeded",
                        "failed",
                        "rollback_in_progress",
                        "rollback_failed",
                        "rolled_back",
                        "restored",
                    }:
                        raise ValueError(f"unknown manifest status {manifest.status!r}")
                    store = DeploymentStore(project, root=target_root)
                    for snapshot in manifest.snapshots:
                        if not snapshot.before_exists:
                            continue
                        if not snapshot.backup_file or not snapshot.before_sha256:
                            raise ValueError(
                                f"{snapshot.path}: before backup metadata is incomplete"
                            )
                        body = store.read_backup(
                            manifest.deployment_id,
                            snapshot.backup_file,
                        )
                        if hashlib.sha256(body).hexdigest() != snapshot.before_sha256:
                            raise ValueError(f"{snapshot.path}: backup hash mismatch")
                except Exception as exc:
                    failures.append(f"{name}/{directory.name}: {exc}")
            counts.append((name, count))
            rollback_event_counts.append((name, rollback_events))
        return DoctorCheckResult.create(
            check_id="state.manifests",
            category=DoctorCheckCategory.STATE,
            status=(DoctorCheckStatus.FAIL if failures else DoctorCheckStatus.PASS),
            summary=(
                "; ".join(failures)
                if failures
                else "all deployment manifests and referenced backups are readable"
            ),
            side_effect=SideEffectLevel.LOCAL_READ,
            context={
                "deployment_counts": counts,
                "corrupt_count": len(failures),
                "rollback_event_counts": rollback_event_counts,
            },
            suggested_action=(
                "inspect the listed manifest paths and restore them from a trusted backup"
                if failures
                else None
            ),
        )

    def transactions(self, request: DoctorRequest) -> DoctorCheckResult:
        """Report every open or corrupt journal without attempting recovery."""

        corrupt: list[str] = []
        open_items: list[tuple[str, str, str]] = []
        for name, _project, _identity, target_root in self._targets(request):
            store = TransactionStore(target_root)
            for path in sorted((target_root / "transactions").glob("*.json")):
                try:
                    journal = store.load(path.stem)
                    if journal.stage != "recovered":
                        open_items.append((name, journal.transaction_id, journal.stage))
                        for reference in journal.backup_refs:
                            candidate = (target_root / reference).resolve()
                            if target_root.resolve() not in candidate.parents:
                                raise ValueError("journal contains an unsafe backup reference")
                            if not candidate.is_file():
                                raise ValueError(
                                    f"recovery object is missing: {reference}"
                                )
                except Exception as exc:
                    corrupt.append(f"{name}/{path.name}: {exc}")
        status = DoctorCheckStatus.PASS
        if corrupt:
            status = DoctorCheckStatus.FAIL
        elif open_items:
            status = DoctorCheckStatus.WARN
        summary = "no unfinished transaction requires recovery"
        if corrupt:
            summary = "; ".join(corrupt)
        elif open_items:
            summary = "unfinished transactions require recovery review"
        return DoctorCheckResult.create(
            check_id="state.transactions",
            category=DoctorCheckCategory.STATE,
            status=status,
            summary=summary,
            side_effect=SideEffectLevel.LOCAL_READ,
            context={"open_transactions": open_items, "corrupt_count": len(corrupt)},
            suggested_action=(
                f"run git-deploy state recover {request.target} without --execute"
                if corrupt or open_items
                else None
            ),
        )

    def remote_access(self, request: DoctorRequest) -> DoctorCheckResult:
        """Open selected transports and perform only a read-only root listing."""

        failures: list[str] = []
        observations: list[tuple[str, int]] = []
        for name, project, _identity, _target_root in self._targets(request):
            _alias, server, _project, _resolved_identity = (
                self.config._resolve_domain_project(request.remote, name)
            )
            transport = None
            try:
                transport = open_cli_transport(dict(server.values))
                writes_before = getattr(transport, "write_calls", 0)
                list_files = getattr(transport, "list_files", None)
                if not callable(list_files):
                    raise TypeError("transport does not expose read-only root listing")
                paths = tuple(list_files(project.remote_root))
                writes_after = getattr(transport, "write_calls", 0)
                if writes_after != writes_before:
                    raise RuntimeError("doctor remote check performed transport writes")
                observations.append((name, len(paths)))
            except Exception as exc:
                failures.append(f"{name}: {exc}")
            finally:
                close = getattr(transport, "close", None)
                if callable(close):
                    close()
        return DoctorCheckResult.create(
            check_id="remote.access",
            category=DoctorCheckCategory.REMOTE,
            status=(DoctorCheckStatus.FAIL if failures else DoctorCheckStatus.PASS),
            summary=(
                "; ".join(failures)
                if failures
                else "remote roots are reachable through read-only listings"
            ),
            side_effect=SideEffectLevel.REMOTE_READ,
            context={"listed_path_counts": observations},
            suggested_action=(
                "check SSH alias, host key, credentials, timeout, and remote root"
                if failures
                else None
            ),
        )
    def _projects(self, request: DoctorRequest) -> tuple[str, ...]:
        """Resolve one exact project or the complete selected remote project set."""

        available = self.config.available_projects(request.remote)
        if request.target == "all":
            return available
        if request.target not in available:
            choices = ", ".join(available)
            raise ValueError(f"unknown project {request.target!r}; available: {choices}")
        return (request.target,)

    def _targets(self, request: DoctorRequest):
        """Yield selected domain project, identity, and zero-write target root tuples."""

        for name in self._projects(request):
            _alias, _server, project, identity = self.config._resolve_domain_project(
                request.remote,
                name,
            )
            target_root = identity.state_root(
                default_state_base(project.name, project.local_state_dir)
            )
            yield name, project, identity, target_root


def standard_doctor_service(config: ApplicationConfigService):
    """Return a DoctorService configured with the standard local checks."""

    from .doctor_service import DoctorService

    checks = ProjectDoctorChecks(config)
    return DoctorService(
        config,
        local_checks=(checks.configuration, checks.selection, checks.repository),
        state_checks=(checks.current_state, checks.manifests, checks.transactions),
        remote_checks=(checks.remote_access,),
    )


def _git(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one read-only Git command and retain failures for aggregation."""

    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _git_tracked(path: Path) -> bool:
    """Return whether a configuration path belongs to and is tracked by Git."""

    top = _git(path.parent, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return False
    root = Path(top.stdout.strip())
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    tracked = _git(root, "ls-files", "--error-unmatch", "--", str(relative))
    return tracked.returncode == 0


def _plaintext_sensitive_fields(
    value: object,
    *,
    prefix: str = "",
) -> set[str]:
    """Return sensitive TOML field paths without retaining their values."""

    found: set[str] = set()
    if not isinstance(value, Mapping):
        return found
    for raw_key, child in value.items():
        key = str(raw_key)
        path = f"{prefix}.{key}" if prefix else key
        normalized = key.lower()
        if normalized in {"password", "passphrase", "private_key", "token", "secret"}:
            found.add(path)
        found.update(_plaintext_sensitive_fields(child, prefix=path))
    return found


def _tree_readable(repository: Path, target_root: Path, tree_id: str) -> bool:
    """Check a current Git tree using existing durable objects without creating paths."""

    environment = os.environ.copy()
    durable_objects = target_root / "git" / "objects"
    if durable_objects.is_dir():
        main = _git(repository, "rev-parse", "--git-path", "objects")
        if main.returncode != 0:
            return False
        main_path = Path(main.stdout.strip())
        if not main_path.is_absolute():
            main_path = (repository / main_path).resolve()
        environment["GIT_OBJECT_DIRECTORY"] = str(durable_objects)
        environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(main_path)
    result = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "-e", f"{tree_id}^{{tree}}"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.returncode == 0
