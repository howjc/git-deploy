"""Read-only expected-state inspection application service."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from git_deploy.errors import ConfigurationError
from git_deploy.expected_state import ExpectedStateStore
from git_deploy.remote_verify import RemotePathStatus, verify_remote_current
from git_deploy.state_guards import StateGuards
from git_deploy.target_identity import default_state_base
from git_deploy.transaction import TransactionStore

from .config_service import ApplicationConfigService, ProjectSelection
from .errors import ApplicationError, ErrorCategory
from .models import SideEffectLevel, StateAction, StateRequest
from .plan_token import StalePlanError


@dataclass(frozen=True, slots=True)
class OpenTransactionSummary:
    """Renderer-neutral open transaction summary."""

    transaction_id: str
    stage: str
    deployment_id: str | None
    before_generation: int | None
    after_generation: int | None
    error: str | None


@dataclass(frozen=True, slots=True)
class StateInspectResult:
    """Structured local current-state and transaction summary."""

    selection: ProjectSelection
    current_present: bool
    generation: int | None
    state_id: str | None
    source_tree_id: str | None
    applied_transition_count: int
    configured_policy_fingerprint: str
    state_policy_fingerprint: str | None
    state_physical_fingerprint: str | None
    file_count: int
    open_transactions: tuple[OpenTransactionSummary, ...]
    legacy_migration_present: bool


class StateVerifyMode(StrEnum):
    """Explicit local or remote-read current-state verification modes."""

    LOCAL = "local"
    REMOTE_READ = "remote_read"


@dataclass(frozen=True, slots=True)
class StateVerifyResult:
    """Structured current-state integrity and optional remote comparison."""

    selection: ProjectSelection
    mode: StateVerifyMode
    current_present: bool
    generation: int | None
    local_ok: bool
    local_reasons: tuple[str, ...]
    remote_paths: tuple[RemotePathStatus, ...]
    remote_read_calls: int
    remote_write_calls: int

    @property
    def ok(self) -> bool:
        """Return whether local integrity and every remote path pass."""

        return self.local_ok and all(
            item.status == "match" for item in self.remote_paths
        )


class StateVerifyTransport(Protocol):
    """Minimal read-only transport surface for current-state verification."""

    def read_file(self, remote_path: str) -> bytes | None:
        """Return remote bytes or None when absent."""


StateTransportFactory = Callable[[dict[str, object]], StateVerifyTransport]


class StateInspectService:
    """Inspect local state without creating directories or contacting a remote."""

    def __init__(
        self,
        config: ApplicationConfigService,
        *,
        transport_factory: StateTransportFactory | None = None,
    ):
        """Bind the shared application configuration service.

        Args:
            config: Validated application configuration selector.
        """

        if not isinstance(config, ApplicationConfigService):
            raise TypeError("config must be an ApplicationConfigService")
        self._config = config
        self._transport_factory = transport_factory

    def current_generation(
        self,
        remote: str | None,
        project_name: str,
    ) -> tuple[ProjectSelection, int | None]:
        """Observe target identity and current generation without mutation.

        Args:
            remote: Explicit alias or None for the configured safe default.
            project_name: Exact configured project key.

        Returns:
            Secret-safe selection and current generation, or None when absent.
        """

        _alias, _server, project, identity = self._config._resolve_domain_project(
            remote,
            project_name,
        )
        selection = self._config.resolve_project(remote, project_name)
        target_root = identity.state_root(
            default_state_base(project.name, project.local_state_dir)
        )
        try:
            current = ExpectedStateStore(target_root, identity).read_current()
        except ConfigurationError as exc:
            raise ApplicationError(
                code="state.corrupt",
                category=ErrorCategory.CONFIGURATION,
                message="persisted state is corrupt or unreadable",
                context={"target_id": selection.target_id, "detail": str(exc)},
            ) from exc
        return selection, current.generation if current is not None else None

    def inspect_selected(
        self,
        remote: str | None,
        project_name: str,
    ) -> StateInspectResult:
        """Inspect a CLI/TUI selection using the just-observed generation.

        Args:
            remote: Explicit alias or None for the configured safe default.
            project_name: Exact configured project key.

        Returns:
            Structured state inspection result.
        """

        selection, generation = self.current_generation(remote, project_name)
        return self.inspect(
            StateRequest(
                remote=selection.remote_alias,
                project=selection.project,
                side_effect=SideEffectLevel.LOCAL_READ,
                expected_target_id=selection.target_id,
                expected_physical_fingerprint=selection.physical_fingerprint,
                expected_generation=generation,
                action=StateAction.INSPECT,
            )
        )

    def verify_selected(
        self,
        remote: str | None,
        project_name: str,
        *,
        remote_check: bool,
    ) -> StateVerifyResult:
        """Verify a CLI/TUI selection using its just-observed generation.

        Args:
            remote: Explicit alias or None for the configured safe default.
            project_name: Exact configured project key.
            remote_check: Whether to perform a read-only remote comparison.

        Returns:
            Structured local/remote current-state verification result.
        """

        selection, generation = self.current_generation(remote, project_name)
        return self.verify(
            StateRequest(
                remote=selection.remote_alias,
                project=selection.project,
                side_effect=(
                    SideEffectLevel.REMOTE_READ
                    if remote_check
                    else SideEffectLevel.LOCAL_READ
                ),
                expected_target_id=selection.target_id,
                expected_physical_fingerprint=selection.physical_fingerprint,
                expected_generation=generation,
                action=StateAction.VERIFY,
                check_remote=remote_check,
            )
        )

    def verify(self, request: StateRequest) -> StateVerifyResult:
        """Verify local integrity and optionally compare current remote bytes.

        Args:
            request: State verify request with explicit side-effect mode.

        Returns:
            Structured verification result with transport counters.
        """

        if not isinstance(request, StateRequest):
            raise TypeError("request must be a StateRequest")
        if request.action is not StateAction.VERIFY:
            raise ValueError("state verify service requires a verify request")
        remote_check = request.side_effect is SideEffectLevel.REMOTE_READ
        if request.check_remote != remote_check:
            raise ValueError("state verify check_remote must match side-effect level")
        _alias, server, project, identity = self._config._resolve_domain_project(
            request.remote,
            request.project,
        )
        selection = self._config.resolve_project(request.remote, request.project)
        if (
            request.expected_target_id != selection.target_id
            or request.expected_physical_fingerprint
            != selection.physical_fingerprint
        ):
            raise StalePlanError("configured physical target changed before state verify")
        target_root = identity.state_root(
            default_state_base(project.name, project.local_state_dir)
        )
        store = ExpectedStateStore(target_root, identity)
        try:
            loaded = store.load_current_state()
        except ConfigurationError as exc:
            raise ApplicationError(
                code="state.corrupt",
                category=ErrorCategory.CONFIGURATION,
                message="persisted state is corrupt or unreadable",
                context={"target_id": selection.target_id, "detail": str(exc)},
            ) from exc
        generation = loaded[0].generation if loaded is not None else None
        if generation != request.expected_generation:
            raise StalePlanError(
                "current generation changed before state verify: "
                f"expected {request.expected_generation}, actual {generation}"
            )
        if loaded is None:
            return StateVerifyResult(
                selection=selection,
                mode=(StateVerifyMode.REMOTE_READ if remote_check else StateVerifyMode.LOCAL),
                current_present=False,
                generation=None,
                local_ok=True,
                local_reasons=(),
                remote_paths=(),
                remote_read_calls=0,
                remote_write_calls=0,
            )
        _pointer, state = loaded
        local = StateGuards(
            target_root,
            identity,
            expected_policy=selection.policy_fingerprint,
        ).check()
        if not remote_check or not local.ok:
            return StateVerifyResult(
                selection=selection,
                mode=StateVerifyMode.LOCAL,
                current_present=True,
                generation=generation,
                local_ok=local.ok,
                local_reasons=local.reasons,
                remote_paths=(),
                remote_read_calls=0,
                remote_write_calls=0,
            )

        transport = self._open_state_transport(dict(server.values))
        writes_before = int(getattr(transport, "write_calls", 0))
        reads_before = int(getattr(transport, "read_calls", 0))
        try:
            report = verify_remote_current(state, project, transport)
            writes_after = int(getattr(transport, "write_calls", writes_before))
            reads_after = int(getattr(transport, "read_calls", report.read_calls))
            writes = writes_after - writes_before
            if writes != 0:
                raise ApplicationError(
                    code="state.verify-write",
                    category=ErrorCategory.INTERNAL,
                    message="read-only state verify performed a remote write",
                )
            return StateVerifyResult(
                selection=selection,
                mode=StateVerifyMode.REMOTE_READ,
                current_present=True,
                generation=generation,
                local_ok=True,
                local_reasons=(),
                remote_paths=report.results,
                remote_read_calls=reads_after - reads_before,
                remote_write_calls=writes,
            )
        finally:
            close = getattr(transport, "close", None)
            if callable(close):
                close()

    def _open_state_transport(
        self,
        values: dict[str, object],
    ) -> StateVerifyTransport:
        """Open a transport only for explicit remote-read verification."""

        if self._transport_factory is not None:
            return self._transport_factory(values)
        from git_deploy.remote_verify import open_cli_transport

        return open_cli_transport(values)

    def inspect(self, request: StateRequest) -> StateInspectResult:
        """Read current state, identity/policy, and open transactions.

        Args:
            request: Local-read state inspect request.

        Returns:
            Structured state summary.

        Raises:
            ApplicationError: If persisted state is corrupt or unreadable.
            StalePlanError: If request identity/generation does not match.
        """

        if not isinstance(request, StateRequest):
            raise TypeError("request must be a StateRequest")
        if (
            request.action is not StateAction.INSPECT
            or request.side_effect is not SideEffectLevel.LOCAL_READ
        ):
            raise ValueError("state inspect service requires a local-read inspect request")
        _alias, _server, project, identity = self._config._resolve_domain_project(
            request.remote,
            request.project,
        )
        selection = self._config.resolve_project(request.remote, request.project)
        if (
            request.expected_target_id != selection.target_id
            or request.expected_physical_fingerprint
            != selection.physical_fingerprint
        ):
            raise StalePlanError("configured physical target changed before state inspect")
        target_root = identity.state_root(
            default_state_base(project.name, project.local_state_dir)
        )
        store = ExpectedStateStore(target_root, identity)
        transactions = TransactionStore(target_root)
        try:
            loaded = store.load_current_state()
            open_transactions = tuple(
                OpenTransactionSummary(
                    transaction_id=item.transaction_id,
                    stage=item.stage,
                    deployment_id=item.deployment_id,
                    before_generation=item.before_generation,
                    after_generation=item.after_generation,
                    error=item.error,
                )
                for item in transactions.list_open()
            )
        except ConfigurationError as exc:
            raise ApplicationError(
                code="state.corrupt",
                category=ErrorCategory.CONFIGURATION,
                message="persisted state is corrupt or unreadable",
                context={"target_id": selection.target_id, "detail": str(exc)},
            ) from exc

        generation = loaded[0].generation if loaded is not None else None
        if generation != request.expected_generation:
            raise StalePlanError(
                "current generation changed before state inspect: "
                f"expected {request.expected_generation}, actual {generation}"
            )
        if loaded is None:
            pointer = None
            state = None
        else:
            pointer, state = loaded
        return StateInspectResult(
            selection=selection,
            current_present=loaded is not None,
            generation=generation,
            state_id=pointer.state_id if pointer is not None else None,
            source_tree_id=state.source_tree_id if state is not None else None,
            applied_transition_count=(
                len(state.applied_transition_ids) if state is not None else 0
            ),
            configured_policy_fingerprint=selection.policy_fingerprint,
            state_policy_fingerprint=(
                state.policy_fingerprint if state is not None else None
            ),
            state_physical_fingerprint=(
                state.physical_fingerprint if state is not None else None
            ),
            file_count=len(state.files) if state is not None else 0,
            open_transactions=open_transactions,
            legacy_migration_present=(target_root / "migration.json").is_file(),
        )
