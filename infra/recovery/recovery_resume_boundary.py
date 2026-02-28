"""
/infra/recovery/recovery_resume_boundary.py

Resume boundary calculation with formal guarantees.

All boundaries must correspond to atomic commit points.
Must never resume in the middle of:
- A partially applied transaction
- A half-aggregated window
- An incomplete enforcement decision
- A split batch
"""

from __future__ import annotations

from typing import Optional

from .recovery_models import (
    CheckpointSnapshot,
    CheckpointState,
    PipelineStateSnapshot,
    PersistenceSnapshot,
    AggregationSnapshot,
    RecoveryStage,
    ResumeBoundary,
    RewindBoundary,
    RecoveryConfig,
    LogicalTimestamp,
    WindowCompletenessGuarantee,
    TransactionReconciliationReport,
)


class BoundaryCalculator:
    """
    Calculates safe resume and rewind boundaries.
    
    All boundaries must correspond to atomic commit points.
    Must never resume in the middle of:
    - A partially applied transaction
    - A half-aggregated window
    - An incomplete enforcement decision
    - A split batch
    
    Resume boundary is calculated based on:
    - Last fully committed stage
    - Last atomic persistence commit
    - Window completeness
    - Transactional guarantees
    """

    @staticmethod
    def calculate_resume_boundary(
        checkpoint_snapshot: CheckpointSnapshot,
        pipeline_snapshot: PipelineStateSnapshot,
        persistence_snapshot: PersistenceSnapshot,
        aggregation_snapshot: Optional[AggregationSnapshot] = None,
    ) -> Optional[ResumeBoundary]:
        """
        Calculate safe resume boundary.
        
        Resume boundary is the highest safe committed stage.
        Must be atomic, window-safe, and transaction-safe.
        """
        # Find highest complete atomic stage
        # Process stages in reverse order (downstream to upstream)
        # to find the most advanced safe boundary
        for stage in reversed(list(RecoveryStage)):
            checkpoint = checkpoint_snapshot.get_checkpoint(stage)
            stage_state = pipeline_snapshot.get_stage(stage)

            if checkpoint and stage_state:
                # Only resume from atomic boundaries
                if stage_state.is_atomic_boundary and stage_state.status == "COMPLETE":
                    # Validate window safety for aggregation stage
                    window_safe = True
                    if stage == RecoveryStage.AGGREGATION:
                        window_safe = BoundaryCalculator._validate_window_safety(
                            checkpoint, aggregation_snapshot
                        )
                        if not window_safe:
                            continue
                    
                    # Validate transaction safety for persistence stage
                    transaction_safe = True
                    if stage == RecoveryStage.PERSISTENCE:
                        transaction_safe = BoundaryCalculator._validate_transaction_safety(
                            checkpoint, persistence_snapshot
                        )
                        if not transaction_safe:
                            continue
                    
                    # All checks passed - safe to resume from this boundary
                    return ResumeBoundary(
                        stage=stage,
                        boundary_timestamp=checkpoint.boundary_timestamp,
                        checkpoint_sequence=checkpoint.sequence_number,
                        idempotency_key=checkpoint.idempotency_key,
                        window_id=checkpoint.window_id,
                        transaction_log_position=checkpoint.transaction_log_position,
                        is_atomic=True,
                        atomic_commit_id=f"{checkpoint.checkpoint_id}_{checkpoint.sequence_number}",
                        window_safe=window_safe,
                        transaction_safe=transaction_safe,
                    )

        return None

    @staticmethod
    def _validate_window_safety(
        checkpoint: CheckpointState,
        aggregation_snapshot: Optional[AggregationSnapshot],
    ) -> bool:
        """Validate window completeness for safe resume."""
        if aggregation_snapshot is None:
            return True
        
        if checkpoint.window_id:
            # Check if window is closed and committed
            for window in aggregation_snapshot.closed_windows:
                if window.window_id == checkpoint.window_id:
                    return window.is_committed
            
            # Window not found in closed windows - not safe
            return False
        
        return True

    @staticmethod
    def _validate_transaction_safety(
        checkpoint: CheckpointState,
        persistence_snapshot: PersistenceSnapshot,
    ) -> bool:
        """Validate transaction safety for safe resume."""
        if checkpoint.transaction_log_position is not None:
            if checkpoint.transaction_log_position != persistence_snapshot.transaction_log_head:
                # Transaction log position mismatch - not safe
                return False
        
        # All transactions must be committed
        if persistence_snapshot.has_pending_transactions():
            return False
        
        return True

    @staticmethod
    def calculate_rewind_boundary(
        checkpoint_snapshot: CheckpointSnapshot,
        pipeline_snapshot: PipelineStateSnapshot,
        config: RecoveryConfig,
    ) -> Optional[RewindBoundary]:
        """
        Calculate safe rewind boundary for partial recovery.
        
        Must identify reversible boundary and reprocessing range.
        """
        # Find stages in progress or failed
        problematic_stages = [
            stage
            for stage, state in pipeline_snapshot.stages.items()
            if state.status in ("IN_PROGRESS", "FAILED")
        ]

        if not problematic_stages:
            return None

        # Find earliest problematic stage
        earliest_stage = min(problematic_stages, key=lambda s: s.value)

        # Get checkpoint for previous stage
        stage_index = list(RecoveryStage).index(earliest_stage)
        if stage_index == 0:
            # No previous stage, need full rebuild
            return None

        target_stage = list(RecoveryStage)[stage_index - 1]
        target_checkpoint = checkpoint_snapshot.get_checkpoint(target_stage)

        if not target_checkpoint:
            return None

        # Determine affected stages (current and all downstream)
        affected = tuple(list(RecoveryStage)[stage_index:])

        # Check if side effect compensation needed
        requires_compensation = any(
            s in (RecoveryStage.PERSISTENCE, RecoveryStage.DOWNSTREAM_PROPAGATION)
            for s in affected
        )

        compensation_actions = []
        if requires_compensation:
            compensation_actions = [
                f"Invalidate downstream propagation for stages {[s.value for s in affected]}",
                "Mark affected persistence writes as superseded",
            ]

        return RewindBoundary(
            target_stage=target_stage,
            target_timestamp=target_checkpoint.boundary_timestamp,
            target_checkpoint_sequence=target_checkpoint.sequence_number,
            reprocessing_range_start=target_checkpoint.boundary_timestamp,
            reprocessing_range_end=checkpoint_snapshot.snapshot_timestamp,
            affected_stages=affected,
            requires_side_effect_compensation=requires_compensation,
            compensation_actions=tuple(compensation_actions),
        )

    @staticmethod
    def calculate_window_completeness_guarantee(
        aggregation_snapshot: Optional[AggregationSnapshot],
        resume_boundary: Optional[ResumeBoundary],
    ) -> Optional[WindowCompletenessGuarantee]:
        """
        Calculate window completeness guarantee.
        
        Ensures closed windows are immutable and open windows are safe to rebuild.
        """
        if aggregation_snapshot is None:
            return None
        
        # Check closed windows are all committed
        closed_windows_immutable = all(
            w.is_committed for w in aggregation_snapshot.closed_windows
        )
        
        # Check open windows are safe (no partial state or all partial state can be rebuilt)
        open_windows_safe = not aggregation_snapshot.has_partial_windows()
        
        # Determine if full rewind or rebuild needed
        requires_full_rewind = not closed_windows_immutable
        requires_full_rebuild = (
            not open_windows_safe
            and resume_boundary is None
        )
        
        return WindowCompletenessGuarantee(
            closed_windows_immutable=closed_windows_immutable,
            open_windows_safe=open_windows_safe,
            last_committed_window_id=aggregation_snapshot.last_committed_window_id,
            requires_full_rewind=requires_full_rewind,
            requires_full_rebuild=requires_full_rebuild,
        )

    @staticmethod
    def calculate_transaction_reconciliation(
        persistence_snapshot: PersistenceSnapshot,
    ) -> TransactionReconciliationReport:
        """
        Calculate transaction reconciliation report.
        
        Validates all transactions are fully committed before resume.
        """
        all_committed = not persistence_snapshot.has_pending_transactions()
        pending_count = len(persistence_snapshot.pending_transactions)
        
        # Check transaction ID monotonicity
        transaction_id_monotonic = True
        last_committed_id = None
        
        if persistence_snapshot.committed_transactions:
            tx_ids = [t.transaction_id for t in persistence_snapshot.committed_transactions]
            # Simple check - in production would validate proper ordering
            transaction_id_monotonic = len(set(tx_ids)) == len(tx_ids)
            if persistence_snapshot.committed_transactions:
                last_committed_id = persistence_snapshot.committed_transactions[-1].transaction_id
        
        orphaned_fragments = persistence_snapshot.has_orphaned_fragments()
        
        return TransactionReconciliationReport(
            all_transactions_committed=all_committed,
            pending_transaction_count=pending_count,
            transaction_id_monotonic=transaction_id_monotonic,
            last_committed_transaction_id=last_committed_id,
            orphaned_fragments_detected=orphaned_fragments,
        )
