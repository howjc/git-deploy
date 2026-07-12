"""Transaction recovery decision table and crash-stage handlers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .errors import ConfigurationError, PolicyError
from .expected_state import ExpectedStateStore
from .target_identity import TargetIdentity
from .transaction import TransactionJournal, TransactionStore

Decision = Literal["finalize", "restore", "reconcile", "manual", "noop"]


@dataclass(frozen=True)
class RecoveryDecision:
    """One recovery decision with rationale.

    Attributes:
        decision: Action to take.
        reason: Human-readable reason without secrets.
        transaction_id: Related journal id.
    """

    decision: Decision
    reason: str
    transaction_id: str


@dataclass(frozen=True)
class RecoveryContext:
    """Inputs to the recovery decision table.

    Attributes:
        stage: Journal stage.
        current_generation: Local current generation or ``None``.
        journal_before_generation: Journal before generation.
        journal_after_generation: Journal after generation.
        remote_matches_current: Remote equals before/current bytes.
        remote_matches_target: Remote equals after/target bytes.
        remote_matches_before: Alias of remote_matches_current.
        remote_third: Remote matches neither current nor target.
    """

    stage: str
    current_generation: int | None
    journal_before_generation: int | None
    journal_after_generation: int | None
    remote_matches_current: bool = False
    remote_matches_target: bool = False
    remote_matches_before: bool = False
    remote_third: bool = False


class TransactionRecoveryService:
    """Decide and optionally execute recovery for unfinished transactions."""

    def __init__(self, target_root: Path, identity: TargetIdentity):
        """Bind target stores.

        Args:
            target_root: Target state root.
            identity: Physical identity.
        """

        self.target_root = target_root
        self.identity = identity
        self.tx = TransactionStore(target_root)
        self.states = ExpectedStateStore(target_root, identity)

    def decide(self, ctx: RecoveryContext, transaction_id: str) -> RecoveryDecision:
        """Apply the north-star decision table.

        Args:
            ctx: Observed stage/generation/remote combination.
            transaction_id: Journal id.

        Returns:
            Recovery decision (unknown combos → manual, zero mutation implied).
        """

        stage = ctx.stage
        if stage == "prepared":
            return RecoveryDecision(
                "restore",
                "prepared crash: keep before generation, no remote writes, clean staging",
                transaction_id,
            )
        if stage == "remote_mutating":
            if ctx.remote_matches_before or ctx.remote_matches_current:
                return RecoveryDecision(
                    "restore",
                    "remote_mutating with before-like remote: restore before and recover",
                    transaction_id,
                )
            if ctx.remote_matches_target:
                return RecoveryDecision(
                    "manual",
                    "remote_mutating but remote already at target: manual confirmation required",
                    transaction_id,
                )
            return RecoveryDecision(
                "restore",
                "remote_mutating partial upload/delete: restore from durable backups",
                transaction_id,
            )
        if stage == "remote_verified":
            if ctx.remote_third:
                return RecoveryDecision(
                    "manual",
                    "remote_verified with third remote content: do not overwrite",
                    transaction_id,
                )
            if ctx.remote_matches_target:
                return RecoveryDecision(
                    "finalize",
                    "remote_verified and remote matches target: finalize generation CAS",
                    transaction_id,
                )
            return RecoveryDecision(
                "manual",
                "remote_verified but remote does not match target: manual recovery",
                transaction_id,
            )
        if stage == "state_committed":
            return RecoveryDecision(
                "finalize",
                "state_committed: idempotently complete manifest/journal terminal state",
                transaction_id,
            )
        if stage in {"reconciled", "recovered"}:
            return RecoveryDecision("noop", f"stage {stage} needs no recovery", transaction_id)
        if stage == "manual_recovery_required":
            return RecoveryDecision(
                "manual",
                "journal already requires manual recovery",
                transaction_id,
            )
        return RecoveryDecision(
            "manual",
            f"unknown stage/context combination defaults to manual: {stage}",
            transaction_id,
        )

    def decide_for_journal(
        self,
        journal: TransactionJournal,
        *,
        remote_matches_current: bool = False,
        remote_matches_target: bool = False,
        remote_third: bool = False,
    ) -> RecoveryDecision:
        """Build context from journal + remote flags and decide.

        Args:
            journal: Open journal.
            remote_matches_current: Remote equals current/before.
            remote_matches_target: Remote equals target/after.
            remote_third: Remote equals neither.

        Returns:
            Recovery decision.
        """

        current = self.states.read_current()
        ctx = RecoveryContext(
            stage=journal.stage,
            current_generation=current.generation if current else None,
            journal_before_generation=journal.before_generation,
            journal_after_generation=journal.after_generation,
            remote_matches_current=remote_matches_current,
            remote_matches_before=remote_matches_current,
            remote_matches_target=remote_matches_target,
            remote_third=remote_third,
        )
        return self.decide(ctx, journal.transaction_id)

    def execute(
        self,
        decision: RecoveryDecision,
        journal: TransactionJournal,
        *,
        finalize_callback: Any | None = None,
        restore_callback: Any | None = None,
    ) -> TransactionJournal:
        """Execute a non-manual decision with zero silent generation advance.

        Args:
            decision: Decision from ``decide``.
            journal: Journal to update.
            finalize_callback: Optional callable to CAS-advance after-state.
            restore_callback: Optional callable to restore remote before bytes.

        Returns:
            Updated journal.
        """

        if decision.decision == "manual":
            if journal.stage != "manual_recovery_required":
                return self.tx.advance(journal, "manual_recovery_required", error=decision.reason)
            return journal
        if decision.decision == "noop":
            return journal
        if decision.decision == "restore":
            # prepared: no remote mutation — do not call restore_callback (zero remote I/O).
            # state-only journals may need CAS finalize when after_state_id is already written.
            if journal.stage == "prepared":
                if journal.meta.get("kind") == "bootstrap":
                    current = self.states.read_current()
                    if current is None:
                        # CAS never became visible: safely abort the zero-remote-write
                        # bootstrap while retaining its immutable staged state.
                        return self.tx.advance(journal, "recovered", error=None)
                    if (
                        journal.after_state_id
                        and current.state_id == journal.after_state_id
                        and current.generation == journal.after_generation
                    ):
                        # CAS became visible before the publisher/terminal step
                        # reported failure. Close the durable evidence idempotently.
                        return self.tx.advance(journal, "recovered", error=None)
                    return self.tx.advance(
                        journal,
                        "manual_recovery_required",
                        error=(
                            "bootstrap recovery found current that does not match "
                            "the staged after-state"
                        ),
                    )
                if (
                    journal.meta.get("kind") == "state_only"
                    and journal.after_state_id
                ):
                    current = self.states.read_current()
                    if (
                        current is None
                        or (
                            journal.before_generation is not None
                            and current.generation == journal.before_generation
                        )
                    ):
                        after = self.states.read_state(journal.after_state_id)
                        self.states.cas_advance(
                            expected_generation=journal.before_generation,
                            state=after,
                        )
                return self.tx.advance(journal, "recovered", error=None)
            # remote_mutating: require a real restore callback that rewrites before bytes.
            if restore_callback is None:
                return self.tx.advance(
                    journal,
                    "manual_recovery_required",
                    error="restore_callback required for proven recovery",
                )
            restore_callback(journal)
            # Never advance current on restore path.
            if journal.stage not in {"recovered", "manual_recovery_required"}:
                if journal.stage == "remote_mutating":
                    return self.tx.advance(journal, "recovered", error=None)
                return self.tx.advance(journal, "recovered")
            return journal
        if decision.decision == "finalize":
            # Fail-closed: remote_verified finalize must re-check via callback.
            if finalize_callback is None and journal.stage in {
                "remote_verified",
                "state_committed",
            }:
                return self.tx.advance(
                    journal,
                    "manual_recovery_required",
                    error="finalize_callback required for proven recovery",
                )
            if finalize_callback is not None:
                finalize_callback(journal)
            if journal.stage == "remote_verified":
                journal = self.tx.advance(journal, "state_committed")
            if journal.stage == "state_committed":
                journal = self.tx.advance(journal, "recovered")
            return journal
        if decision.decision == "reconcile":
            return self.tx.advance(journal, "recovered")
        raise ConfigurationError(f"unsupported recovery decision: {decision.decision}")

    def recover_open(
        self,
        *,
        remote_matches_current: bool = False,
        remote_matches_target: bool = False,
        remote_third: bool = False,
        finalize_callback: Any | None = None,
        restore_callback: Any | None = None,
    ) -> list[tuple[RecoveryDecision, TransactionJournal]]:
        """Recover all open journals.

        Args:
            remote_matches_current: Remote flag.
            remote_matches_target: Remote flag.
            remote_third: Remote flag.
            finalize_callback: Optional finalize hook.
            restore_callback: Optional restore hook.

        Returns:
            List of decision/journal pairs after execution attempts.
        """

        results: list[tuple[RecoveryDecision, TransactionJournal]] = []
        for journal in self.tx.list_open():
            decision = self.decide_for_journal(
                journal,
                remote_matches_current=remote_matches_current,
                remote_matches_target=remote_matches_target,
                remote_third=remote_third,
            )
            if decision.decision == "manual":
                updated = self.execute(decision, journal)
                results.append((decision, updated))
                continue
            updated = self.execute(
                decision,
                journal,
                finalize_callback=finalize_callback,
                restore_callback=restore_callback,
            )
            results.append((decision, updated))
        return results

    def require_no_manual_block(self) -> None:
        """Raise when any journal requires manual recovery.

        Returns:
            None.
        """

        for journal in self.tx.list_open():
            if journal.stage == "manual_recovery_required":
                raise PolicyError(
                    f"manual_recovery_required for transaction {journal.transaction_id}; deploy blocked"
                )
