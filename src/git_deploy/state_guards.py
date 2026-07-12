"""Pre-flight guards for identity, object integrity, and open transactions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError, PolicyError
from .expected_state import ExpectedStateStore
from .object_store import ContentAddressedStore
from .target_identity import TargetIdentity
from .transaction import TransactionStore


@dataclass(frozen=True)
class GuardReport:
    """Outcome of running state guards.

    Attributes:
        ok: Whether deploy/plan mutation may proceed.
        reasons: Human-readable block reasons (no secrets).
    """

    ok: bool
    reasons: tuple[str, ...]


class StateGuards:
    """Evaluate fail-closed preconditions before remote mutation."""

    def __init__(
        self,
        target_root: Path,
        identity: TargetIdentity,
        *,
        expected_physical: str | None = None,
        expected_policy: str | None = None,
    ):
        """Bind stores for one physical target.

        Args:
            target_root: Target state root.
            identity: Resolved physical identity.
            expected_physical: Config physical fingerprint that must match state.
            expected_policy: Config policy fingerprint that must match state.
        """

        self.target_root = target_root
        self.identity = identity
        self.expected_physical = expected_physical or identity.physical_fingerprint
        self.expected_policy = expected_policy
        self.state_store = ExpectedStateStore(target_root, identity)
        self.cas = ContentAddressedStore(target_root)
        self.tx = TransactionStore(target_root)

    def check(self, *, allow_force: bool = False) -> GuardReport:
        """Run all guards; ``--force`` never bypasses them.

        Args:
            allow_force: Ignored for integrity/identity/transaction gates.

        Returns:
            Guard report.
        """

        del allow_force  # --force cannot bypass these gates.
        reasons: list[str] = []
        open_tx = self.tx.list_open()
        if open_tx:
            stages = ", ".join(f"{item.transaction_id}:{item.stage}" for item in open_tx)
            reasons.append(f"unfinished transaction(s) require recover: {stages}")

        try:
            loaded = self.state_store.load_current_state()
        except ConfigurationError as exc:
            reasons.append(f"current state integrity failure: {exc}")
            loaded = None
        if loaded is not None:
            pointer, state = loaded
            if pointer.target_id != self.identity.target_id:
                reasons.append(
                    "physical target identity mismatch between config and current state"
                )
            if state.physical_fingerprint != self.expected_physical:
                reasons.append(
                    "physical target identity mismatch between config and current state"
                )
            if (
                self.expected_policy is not None
                and state.policy_fingerprint != self.expected_policy
            ):
                reasons.append(
                    "managed-state policy mismatch; run explicit policy migration"
                )
            # Re-read state to rehash; read_state already validates.
            try:
                self.state_store.read_state(pointer.state_id)
            except ConfigurationError as exc:
                reasons.append(f"current state integrity failure: {exc}")

            # Every expected content ref must exist in CAS and rehash.
            for entry in state.files:
                if not entry.exists or not entry.content_sha256:
                    continue
                try:
                    self.cas.get(entry.content_sha256)
                except ConfigurationError as exc:
                    reasons.append(f"CAS object integrity failure: {exc}")

            # Non-empty source_tree_id always requires a durable Git store.
            # Missing store/markers/tree must fail-closed (never skip when absent).
            if state.source_tree_id:
                try:
                    from .git_store import PersistentGitStore

                    git_dir = self.target_root / "git"
                    if not git_dir.is_dir():
                        raise ConfigurationError(
                            "persistent git store missing under target root"
                        )
                    repo_marker = git_dir / "repository_path"
                    identity_path = git_dir / "repository_identity"
                    if not repo_marker.is_file() or not identity_path.is_file():
                        raise ConfigurationError(
                            "persistent git store repository markers missing"
                        )
                    repo = Path(repo_marker.read_text(encoding="utf-8").strip())
                    if not repo:
                        raise ConfigurationError("persistent git store repository path empty")
                    store = PersistentGitStore(self.target_root, repo)
                    store.require_tree(state.source_tree_id)
                except ConfigurationError as exc:
                    reasons.append(f"git object integrity failure: {exc}")

        return GuardReport(ok=not reasons, reasons=tuple(reasons))

    def require_clear(self, *, force: bool = False) -> None:
        """Raise when guards fail.

        Args:
            force: Ignored; cannot bypass.

        Returns:
            None.
        """

        report = self.check(allow_force=force)
        if not report.ok:
            raise PolicyError("; ".join(report.reasons))
