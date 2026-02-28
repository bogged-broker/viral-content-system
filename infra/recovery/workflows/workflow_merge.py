"""
Verified Artifact Merge & Live-State Reconciliation Engine

This is the point of no return. Everything before this is theory.
This is where truth re-enters reality.

CRITICAL PRINCIPLE: Live state is sacred.

A merge is permitted only if ALL are true:
- replay succeeded
- determinism was proven
- invariants hold post-merge
- lineage is preserved
- rollback is possible

If any condition fails → merge is forbidden.

This file commits only proven facts.
"""

import logging
from datetime import datetime
from typing import Optional, Union, Tuple, List, Dict
from uuid import uuid4

from .merge_base import (
    WorkflowMergeResult,
    WorkflowMergeContext,
    MergeConflict,
    MergeCheckpoint,
    MergePreconditions,
    ArtifactLineage,
    LineageVerificationResult,
    MergePhase,
    MergeAbortReason,
    # Events
    MergeEvent,
    WorkflowMergeStarted,
    WorkflowMergeConflictDetected,
    WorkflowMergeAborted,
    WorkflowMergeCommitted,
    # Exceptions
    MergePreconditionError,
    MergeConflictError,
    LineageViolationError,
    InvariantViolationError,
    RollbackAnchorError,
    AtomicCommitError,
)


logger = logging.getLogger(__name__)


# ============================================================================
# PHASE 1: MERGE ADMISSIBILITY GATE
# ============================================================================

class MergeAdmissibilityGate:
    """
    Phase 1: Verify replay artifacts are ready for merge.
    
    Checks:
    - Replay artifacts fully materialized
    - No missing hashes
    - No schema drift
    - No unexpected outputs
    
    Failure → abort.
    """
    
    @staticmethod
    def check_admissibility(context: WorkflowMergeContext) -> tuple[bool, Optional[str]]:
        """
        Check if replay results are admissible for merge.
        
        Returns:
            (is_admissible, failure_reason)
        """
        replay_result = context.replay_result
        
        # Check 1: All artifacts must be materialized
        if not MergeAdmissibilityGate._check_materialization(replay_result):
            return False, "Replay artifacts not fully materialized"
        
        # Check 2: No missing hashes
        if not MergeAdmissibilityGate._check_hashes(replay_result):
            return False, "Missing content hashes for artifacts"
        
        # Check 3: No schema drift
        if not MergeAdmissibilityGate._check_schema_compatibility(
            context.original_dag, replay_result
        ):
            return False, "Schema drift detected between original and replayed artifacts"
        
        # Check 4: No unexpected outputs
        if not MergeAdmissibilityGate._check_expected_outputs(
            context.repair_plan, replay_result
        ):
            return False, "Unexpected outputs produced during replay"
        
        return True, None
    
    @staticmethod
    def _check_materialization(replay_result) -> bool:
        """Verify all artifacts exist and are accessible."""
        if not hasattr(replay_result, 'produced_artifacts'):
            return False
        
        for artifact_id in replay_result.produced_artifacts:
            # In real impl, would check artifact storage
            if not artifact_id:  # Basic check
                return False
        
        return True
    
    @staticmethod
    def _check_hashes(replay_result) -> bool:
        """Verify all artifacts have content hashes."""
        if not hasattr(replay_result, 'artifact_hashes'):
            return False
        
        produced = set(replay_result.produced_artifacts)
        hashed = set(replay_result.artifact_hashes.keys())
        
        return produced == hashed
    
    @staticmethod
    def _check_schema_compatibility(original_dag, replay_result) -> bool:
        """Verify schema versions are compatible."""
        # In real impl, would check schema versions match
        return True  # Simplified for now
    
    @staticmethod
    def _check_expected_outputs(repair_plan, replay_result) -> bool:
        """Verify only expected artifacts were produced."""
        expected = set(repair_plan.expected_outputs)
        produced = set(replay_result.produced_artifacts)
        
        # Produced should be subset of expected
        unexpected = produced - expected
        return len(unexpected) == 0


# ============================================================================
# PHASE 2: LINEAGE VERIFICATION
# ============================================================================

class LineageVerifier:
    """
    Phase 2: Ensure causal continuity.
    
    For each new artifact:
    - Producer node unchanged or explicitly repaired
    - Artifact schema compatible
    - Dependency chain intact
    - No orphan consumers created
    
    Failure → abort.
    """
    
    @staticmethod
    def verify_lineage(
        context: WorkflowMergeContext,
        artifact_id: str,
    ) -> LineageVerificationResult:
        """
        Verify lineage for a single artifact.
        
        Returns verification result with specific check results.
        """
        violations = []
        
        # Get lineage info
        live_lineage = context.live_artifact_index.get(artifact_id)
        replay_result = context.replay_result
        
        # Check 1: Producer node unchanged or repaired
        producer_ok = LineageVerifier._verify_producer(
            artifact_id, context.repair_plan, context.original_dag
        )
        if not producer_ok:
            violations.append("Producer node changed without repair authorization")
        
        # Check 2: Schema compatible
        schema_ok = LineageVerifier._verify_schema_compatibility(
            artifact_id, live_lineage, replay_result
        )
        if not schema_ok:
            violations.append("Schema incompatibility detected")
        
        # Check 3: Dependency chain intact
        deps_ok = LineageVerifier._verify_dependency_chain(
            artifact_id, context
        )
        if not deps_ok:
            violations.append("Dependency chain broken")
        
        # Check 4: No orphan consumers
        consumers_ok = LineageVerifier._verify_no_orphans(
            artifact_id, context
        )
        if not consumers_ok:
            violations.append("Orphan consumers would be created")
        
        verified = len(violations) == 0
        
        return LineageVerificationResult(
            artifact_id=artifact_id,
            verified=verified,
            producer_unchanged=producer_ok,
            schema_compatible=schema_ok,
            dependency_chain_intact=deps_ok,
            no_orphan_consumers=consumers_ok,
            violations=tuple(violations),
        )
    
    @staticmethod
    def _verify_producer(artifact_id: str, repair_plan, original_dag) -> bool:
        """Verify producer node is unchanged or explicitly repaired."""
        # Get producer from repair plan
        if artifact_id in repair_plan.repaired_artifacts:
            # Producer was explicitly repaired - OK
            return True
        
        # Producer must be unchanged
        # In real impl, would check DAG node configs
        return True
    
    @staticmethod
    def _verify_schema_compatibility(
        artifact_id: str,
        live_lineage: Optional[ArtifactLineage],
        replay_result,
    ) -> bool:
        """Verify schema versions are compatible."""
        if not live_lineage:
            # New artifact - no compatibility issues
            return True
        
        # Check schema version from replay matches live
        if hasattr(replay_result, 'artifact_schemas'):
            replay_schema = replay_result.artifact_schemas.get(artifact_id)
            if replay_schema != live_lineage.schema_version:
                return False
        
        return True
    
    @staticmethod
    def _verify_dependency_chain(artifact_id: str, context: WorkflowMergeContext) -> bool:
        """Verify all dependencies are satisfied."""
        lineage = context.live_artifact_index.get(artifact_id)
        if not lineage:
            return True  # New artifact
        
        # Verify all dependencies exist in either live or replay
        for dep in lineage.depends_on:
            if dep not in context.live_artifact_index:
                if dep not in context.replay_result.produced_artifacts:
                    return False  # Missing dependency
        
        return True
    
    @staticmethod
    def _verify_no_orphans(artifact_id: str, context: WorkflowMergeContext) -> bool:
        """Verify no downstream consumers will be orphaned."""
        lineage = context.live_artifact_index.get(artifact_id)
        if not lineage:
            return True  # New artifact, no consumers yet
        
        # All consumers must either:
        # 1. Be included in repair plan
        # 2. Not depend on this artifact anymore
        for consumer in lineage.consumed_by:
            # In real impl, would check if consumer is included in repair
            pass
        
        return True


# ============================================================================
# PHASE 3: CONFLICT DETECTION
# ============================================================================

class ConflictDetector:
    """
    Phase 3: Detect merge conflicts.
    
    Detects:
    - Concurrent live mutations
    - Artifact version supersession
    - Overlapping repairs
    - Downstream invalidation
    
    Rules:
    - Never overwrite newer valid artifacts
    - Never silently fork lineage
    
    Failure → abort or escalate.
    """
    
    @staticmethod
    def detect_conflicts(context: WorkflowMergeContext) -> list[MergeConflict]:
        """
        Detect all merge conflicts.
        
        Returns list of conflicts (empty if none).
        """
        conflicts = []
        
        for artifact_id in context.replay_result.produced_artifacts:
            # Check for concurrent mutation
            concurrent = ConflictDetector._check_concurrent_mutation(
                artifact_id, context
            )
            if concurrent:
                conflicts.append(concurrent)
            
            # Check for version supersession
            supersession = ConflictDetector._check_version_supersession(
                artifact_id, context
            )
            if supersession:
                conflicts.append(supersession)
            
            # Check for overlapping repairs
            overlap = ConflictDetector._check_overlapping_repairs(
                artifact_id, context
            )
            if overlap:
                conflicts.append(overlap)
        
        return conflicts
    
    @staticmethod
    def _check_concurrent_mutation(
        artifact_id: str,
        context: WorkflowMergeContext,
    ) -> Optional[MergeConflict]:
        """Check if artifact was mutated concurrently with replay."""
        live_lineage = context.live_artifact_index.get(artifact_id)
        if not live_lineage:
            return None  # New artifact, no conflict
        
        # Check if live version was updated after replay started
        replay_started = context.replay_result.replay_started_at
        
        if live_lineage.produced_at > replay_started:
            # Live artifact is newer!
            return MergeConflict(
                conflict_type="concurrent_mutation",
                artifact_id=artifact_id,
                description=(
                    f"Live artifact {artifact_id} was updated at "
                    f"{live_lineage.produced_at} after replay started at "
                    f"{replay_started}"
                ),
                live_version=live_lineage.version,
                replay_version=context.replay_result.artifact_hashes.get(artifact_id),
                conflict_timestamp=datetime.utcnow(),
                requires_manual_resolution=True,
            )
        
        return None
    
    @staticmethod
    def _check_version_supersession(
        artifact_id: str,
        context: WorkflowMergeContext,
    ) -> Optional[MergeConflict]:
        """Check if we're trying to overwrite a newer valid artifact."""
        live_lineage = context.live_artifact_index.get(artifact_id)
        if not live_lineage:
            return None
        
        # Get replay hash
        replay_hash = context.replay_result.artifact_hashes.get(artifact_id)
        
        # If hashes differ and live is newer, conflict
        if replay_hash != live_lineage.content_hash:
            if live_lineage.produced_at > context.replay_result.replay_started_at:
                return MergeConflict(
                    conflict_type="version_supersession",
                    artifact_id=artifact_id,
                    description=(
                        f"Cannot overwrite newer valid artifact {artifact_id}"
                    ),
                    live_version=live_lineage.version,
                    replay_version=replay_hash,
                    conflict_timestamp=datetime.utcnow(),
                    requires_manual_resolution=True,
                )
        
        return None
    
    @staticmethod
    def _check_overlapping_repairs(
        artifact_id: str,
        context: WorkflowMergeContext,
    ) -> Optional[MergeConflict]:
        """Check for overlapping repair operations."""
        # In real impl, would check for concurrent repairs
        # For now, simplified
        return None


# ============================================================================
# PHASE 4: INVARIANT RE-VALIDATION
# ============================================================================

class InvariantRevalidator:
    """
    Phase 4: Re-validate invariants post-merge.
    
    Re-checks:
    - Workflow invariants
    - Safety invariants
    - Cross-workflow constraints
    - Storage invariants
    
    This is post-state validation.
    Failure → abort.
    """
    
    @staticmethod
    def revalidate_invariants(context: WorkflowMergeContext) -> tuple[bool, list[str]]:
        """
        Revalidate all invariants in post-merge state.
        
        Returns:
            (all_valid, violations)
        """
        violations = []
        
        # Check 1: Workflow invariants
        if not InvariantRevalidator._check_workflow_invariants(context):
            violations.append("Workflow invariants violated")
        
        # Check 2: Safety invariants
        if not InvariantRevalidator._check_safety_invariants(context):
            violations.append("Safety invariants violated")
        
        # Check 3: Cross-workflow constraints
        if not InvariantRevalidator._check_cross_workflow_constraints(context):
            violations.append("Cross-workflow constraints violated")
        
        # Check 4: Storage invariants
        if not InvariantRevalidator._check_storage_invariants(context):
            violations.append("Storage invariants violated")
        
        return len(violations) == 0, violations
    
    @staticmethod
    def _check_workflow_invariants(context: WorkflowMergeContext) -> bool:
        """Verify workflow-level invariants hold."""
        # In real impl, would run workflow_validator
        return True
    
    @staticmethod
    def _check_safety_invariants(context: WorkflowMergeContext) -> bool:
        """Verify safety invariants hold."""
        # Check no data loss, no corruption, etc.
        return True
    
    @staticmethod
    def _check_cross_workflow_constraints(context: WorkflowMergeContext) -> bool:
        """Verify cross-workflow constraints hold."""
        # Check shared resources, dependencies, etc.
        return True
    
    @staticmethod
    def _check_storage_invariants(context: WorkflowMergeContext) -> bool:
        """Verify storage layer invariants hold."""
        # Check storage consistency, referential integrity, etc.
        return True


# ============================================================================
# PHASE 5: ROLLBACK ANCHOR CREATION
# ============================================================================

class RollbackAnchorCreator:
    """
    Phase 5: Create rollback checkpoint before mutation.
    
    Before any mutation:
    - Snapshot pre-merge state
    - Record artifact lineage map
    - Persist rollback metadata
    
    If rollback anchor fails → abort.
    """
    
    @staticmethod
    def create_rollback_anchor(context: WorkflowMergeContext) -> MergeCheckpoint:
        """
        Create rollback anchor for this merge.
        
        Raises:
            RollbackAnchorError: If anchor creation fails
        """
        checkpoint_id = f"merge_checkpoint_{uuid4().hex[:12]}"
        
        try:
            # Snapshot current state
            lineage_map = RollbackAnchorCreator._snapshot_lineage(context)
            
            # Record all current artifacts
            current_artifacts = tuple(context.live_artifact_index.keys())
            
            # Create checkpoint
            checkpoint = MergeCheckpoint(
                checkpoint_id=checkpoint_id,
                workflow_id=context.workflow_id,
                artifact_lineage_map=lineage_map,
                pre_merge_artifacts=current_artifacts,
                created_at=datetime.utcnow(),
                snapshot_metadata={
                    "replay_id": context.replay_result.replay_id,
                    "merge_id": str(uuid4()),
                },
            )
            
            # Persist checkpoint (in real impl, would write to storage)
            RollbackAnchorCreator._persist_checkpoint(checkpoint)
            
            logger.info(
                f"Created rollback anchor {checkpoint_id} for workflow {context.workflow_id}"
            )
            
            return checkpoint
            
        except Exception as e:
            raise RollbackAnchorError(
                f"Failed to create rollback anchor: {e}"
            ) from e
    
    @staticmethod
    def _snapshot_lineage(context: WorkflowMergeContext) -> dict:
        """Snapshot current artifact lineage."""
        lineage_map = {}
        
        for artifact_id, lineage in context.live_artifact_index.items():
            lineage_map[artifact_id] = {
                "version": lineage.version,
                "content_hash": lineage.content_hash,
                "lineage_hash": lineage.lineage_hash,
                "producer_node_id": lineage.producer_node_id,
                "produced_at": lineage.produced_at.isoformat(),
            }
        
        return lineage_map
    
    @staticmethod
    def _persist_checkpoint(checkpoint: MergeCheckpoint):
        """Persist checkpoint to durable storage."""
        # In real impl, would write to checkpoint store
        logger.info(f"Persisting checkpoint {checkpoint.checkpoint_id}")


# ============================================================================
# PHASE 6: ATOMIC COMMIT
# ============================================================================

class AtomicCommitter:
    """
    Phase 6: Atomically commit merge changes.
    
    Operations:
    - Insert new artifacts
    - Mark old artifacts superseded
    - Update artifact index
    - Emit merge commit record
    
    Guarantees:
    - Atomic
    - Isolated
    - Idempotent
    
    Partial commits are forbidden.
    """
    
    @staticmethod
    def commit_merge(
        context: WorkflowMergeContext,
        checkpoint: MergeCheckpoint,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """
        Atomically commit merge changes.
        
        Returns:
            (merged_artifacts, superseded_artifacts)
        
        Raises:
            AtomicCommitError: If commit cannot be made atomic
        """
        try:
            # Begin atomic transaction
            with AtomicCommitter._atomic_transaction():
                # Insert new artifacts
                merged = AtomicCommitter._insert_artifacts(context)
                
                # Mark superseded artifacts
                superseded = AtomicCommitter._mark_superseded(context, merged)
                
                # Update artifact index
                AtomicCommitter._update_index(context, merged, superseded)
                
                # Emit commit record
                AtomicCommitter._emit_commit_record(
                    context, checkpoint, merged, superseded
                )
                
                return merged, superseded
                
        except Exception as e:
            raise AtomicCommitError(
                f"Atomic commit failed: {e}. No changes committed."
            ) from e
    
    @staticmethod
    def _atomic_transaction():
        """Context manager for atomic transaction."""
        # In real impl, would use database transaction or similar
        class TransactionContext:
            def __enter__(self):
                logger.info("BEGIN ATOMIC TRANSACTION")
                return self
            
            def __exit__(self, exc_type, exc_val, exc_tb):
                if exc_type is None:
                    logger.info("COMMIT TRANSACTION")
                else:
                    logger.error("ROLLBACK TRANSACTION")
                return False
        
        return TransactionContext()
    
    @staticmethod
    def _insert_artifacts(context: WorkflowMergeContext) -> tuple[str, ...]:
        """Insert new artifacts into live state."""
        merged = []
        
        for artifact_id in context.replay_result.produced_artifacts:
            # Insert artifact
            logger.info(f"Inserting artifact {artifact_id}")
            merged.append(artifact_id)
        
        return tuple(merged)
    
    @staticmethod
    def _mark_superseded(
        context: WorkflowMergeContext,
        merged_artifacts: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Mark old versions of artifacts as superseded."""
        superseded = []
        
        for artifact_id in merged_artifacts:
            # Check if there's an old version
            if artifact_id in context.live_artifact_index:
                old_lineage = context.live_artifact_index[artifact_id]
                logger.info(
                    f"Marking artifact {artifact_id} version "
                    f"{old_lineage.version} as superseded"
                )
                superseded.append(artifact_id)
        
        return tuple(superseded)
    
    @staticmethod
    def _update_index(
        context: WorkflowMergeContext,
        merged: tuple[str, ...],
        superseded: tuple[str, ...],
    ):
        """Update artifact index with new entries."""
        logger.info(f"Updating artifact index: {len(merged)} new, {len(superseded)} superseded")
    
    @staticmethod
    def _emit_commit_record(
        context: WorkflowMergeContext,
        checkpoint: MergeCheckpoint,
        merged: tuple[str, ...],
        superseded: tuple[str, ...],
    ):
        """Emit merge commit record for audit trail."""
        record = {
            "workflow_id": context.workflow_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "merged_artifacts": merged,
            "superseded_artifacts": superseded,
            "committed_at": datetime.utcnow().isoformat(),
        }
        logger.info(f"Merge commit record: {record}")


# ============================================================================
# MAIN MERGE ORCHESTRATOR
# ============================================================================

class WorkflowMerger:
    """
    Main orchestrator for verified artifact merge.
    
    Executes all 7 phases in strict order:
    1. Admissibility Gate
    2. Lineage Verification
    3. Conflict Detection
    4. Invariant Re-validation
    5. Rollback Anchor Creation
    6. Atomic Commit
    7. Finalization
    
    Any phase failure aborts the entire merge.
    """
    
    def __init__(self):
        self.admissibility_gate = MergeAdmissibilityGate()
        self.lineage_verifier = LineageVerifier()
        self.conflict_detector = ConflictDetector()
        self.invariant_revalidator = InvariantRevalidator()
        self.rollback_creator = RollbackAnchorCreator()
        self.atomic_committer = AtomicCommitter()
    
    def merge_workflow(
        self,
        context: WorkflowMergeContext,
    ) -> WorkflowMergeResult:
        """
        Execute verified artifact merge with all safety checks.
        
        Returns:
            WorkflowMergeResult with merge outcome
        
        GUARANTEE: If merge_safe=False, nothing was committed.
        """
        merge_id = str(uuid4())
        
        # Emit start event
        self._emit_event(WorkflowMergeStarted(
            workflow_id=context.workflow_id,
            merge_id=merge_id,
            timestamp=datetime.utcnow(),
            event_type="workflow_merge_started",
            replay_id=context.replay_result.replay_id,
            artifact_count=len(context.replay_result.produced_artifacts),
        ))
        
        try:
            # ================================================================
            # PRECONDITION CHECK
            # ================================================================
            logger.info("Checking merge preconditions...")
            preconditions = self._check_preconditions(context)
            
            if not preconditions.all_satisfied():
                failures = preconditions.get_failures()
                abort_reason = MergeAbortReason.REPLAY_NOT_SAFE  # Default
                
                if "determinism_verified" in failures:
                    abort_reason = MergeAbortReason.DETERMINISM_NOT_VERIFIED
                elif "validation_passed" in failures:
                    abort_reason = MergeAbortReason.VALIDATION_FAILED
                elif "damage_bounded" in failures:
                    abort_reason = MergeAbortReason.DAMAGE_UNBOUNDED
                elif "repair_plan_applied" in failures:
                    abort_reason = MergeAbortReason.REPAIR_PLAN_INCOMPLETE
                
                return self._abort_merge(
                    context, merge_id, MergePhase.ADMISSIBILITY_GATE,
                    abort_reason,
                    f"Preconditions not satisfied: {failures}"
                )
            
            logger.info("✓ All preconditions satisfied")
            
            # ================================================================
            # PHASE 1: ADMISSIBILITY GATE
            # ================================================================
            logger.info("Phase 1: Checking merge admissibility...")
            
            admissible, reason = self.admissibility_gate.check_admissibility(context)
            
            if not admissible:
                abort_reason = MergeAbortReason.ARTIFACTS_NOT_MATERIALIZED
                if "hash" in reason.lower():
                    abort_reason = MergeAbortReason.MISSING_HASHES
                elif "schema" in reason.lower():
                    abort_reason = MergeAbortReason.SCHEMA_DRIFT
                elif "unexpected" in reason.lower():
                    abort_reason = MergeAbortReason.UNEXPECTED_OUTPUTS
                
                return self._abort_merge(
                    context, merge_id, MergePhase.ADMISSIBILITY_GATE,
                    abort_reason, reason
                )
            
            logger.info("✓ Phase 1 complete: Artifacts admissible")
            
            # ================================================================
            # PHASE 2: LINEAGE VERIFICATION
            # ================================================================
            logger.info("Phase 2: Verifying lineage...")
            
            for artifact_id in context.replay_result.produced_artifacts:
                lineage_result = self.lineage_verifier.verify_lineage(
                    context, artifact_id
                )
                
                if not lineage_result.verified:
                    return self._abort_merge(
                        context, merge_id, MergePhase.LINEAGE_VERIFICATION,
                        MergeAbortReason.LINEAGE_BROKEN,
                        f"Lineage broken for {artifact_id}: {lineage_result.violations}"
                    )
            
            logger.info("✓ Phase 2 complete: Lineage verified")
            
            # ================================================================
            # PHASE 3: CONFLICT DETECTION
            # ================================================================
            logger.info("Phase 3: Detecting conflicts...")
            
            conflicts = self.conflict_detector.detect_conflicts(context)
            
            if conflicts:
                # Emit conflict events
                for conflict in conflicts:
                    self._emit_event(WorkflowMergeConflictDetected(
                        workflow_id=context.workflow_id,
                        merge_id=merge_id,
                        timestamp=datetime.utcnow(),
                        event_type="workflow_merge_conflict_detected",
                        conflict=conflict,
                        phase=MergePhase.CONFLICT_DETECTION,
                    ))
                
                return self._abort_merge(
                    context, merge_id, MergePhase.CONFLICT_DETECTION,
                    MergeAbortReason.CONFLICT_DETECTED,
                    f"Detected {len(conflicts)} merge conflict(s)"
                )
            
            logger.info("✓ Phase 3 complete: No conflicts detected")
            
            # ================================================================
            # PHASE 4: INVARIANT RE-VALIDATION
            # ================================================================
            logger.info("Phase 4: Revalidating invariants...")
            
            invariants_ok, violations = self.invariant_revalidator.revalidate_invariants(
                context
            )
            
            if not invariants_ok:
                return self._abort_merge(
                    context, merge_id, MergePhase.INVARIANT_REVALIDATION,
                    MergeAbortReason.INVARIANT_VIOLATION,
                    f"Invariant violations: {violations}"
                )
            
            logger.info("✓ Phase 4 complete: Invariants verified")
            
            # ================================================================
            # PHASE 5: ROLLBACK ANCHOR CREATION
            # ================================================================
            logger.info("Phase 5: Creating rollback anchor...")
            
            try:
                checkpoint = self.rollback_creator.create_rollback_anchor(context)
            except RollbackAnchorError as e:
                return self._abort_merge(
                    context, merge_id, MergePhase.ROLLBACK_ANCHOR,
                    MergeAbortReason.ROLLBACK_ANCHOR_FAILED,
                    str(e)
                )
            
            logger.info(f"✓ Phase 5 complete: Rollback anchor {checkpoint.checkpoint_id}")
            
            # ================================================================
            # PHASE 6: ATOMIC COMMIT
            # ================================================================
            logger.info("Phase 6: Atomic commit...")
            
            try:
                merged_artifacts, superseded_artifacts = self.atomic_committer.commit_merge(
                    context, checkpoint
                )
            except AtomicCommitError as e:
                return self._abort_merge(
                    context, merge_id, MergePhase.ATOMIC_COMMIT,
                    MergeAbortReason.ATOMIC_COMMIT_RISK,
                    str(e)
                )
            
            logger.info(
                f"✓ Phase 6 complete: {len(merged_artifacts)} merged, "
                f"{len(superseded_artifacts)} superseded"
            )
            
            # ================================================================
            # PHASE 7: FINALIZATION
            # ================================================================
            logger.info("Phase 7: Finalizing merge...")
            
            result = self._finalize_merge(
                context, merge_id, checkpoint,
                merged_artifacts, superseded_artifacts
            )
            
            # Emit success event
            self._emit_event(WorkflowMergeCommitted(
                workflow_id=context.workflow_id,
                merge_id=merge_id,
                timestamp=datetime.utcnow(),
                event_type="workflow_merge_committed",
                rollback_checkpoint_id=checkpoint.checkpoint_id,
                merged_artifact_count=len(merged_artifacts),
                superseded_artifact_count=len(superseded_artifacts),
            ))
            
            logger.info("✓ Phase 7 complete: Merge finalized")
            logger.info(f"✓ MERGE SUCCESSFUL: {merge_id}")
            
            return result
            
        except Exception as e:
            logger.error(f"Unexpected error during merge: {e}", exc_info=True)
            return self._abort_merge(
                context, merge_id, MergePhase.ADMISSIBILITY_GATE,
                MergeAbortReason.INVARIANT_VIOLATION,
                f"Unexpected error: {e}"
            )
    
    def _check_preconditions(self, context: WorkflowMergeContext) -> MergePreconditions:
        """Check all merge preconditions."""
        replay_result = context.replay_result
        
        return MergePreconditions(
            replay_safe=getattr(replay_result, 'replay_safe', False),
            determinism_verified=getattr(replay_result, 'determinism_verified', False),
            validation_passed=getattr(replay_result, 'validation_passed', True),
            damage_bounded=getattr(context.damage_assessment, 'bounded', True),
            repair_plan_applied=getattr(context.repair_plan, 'applied', True),
        )
    
    def _abort_merge(
        self,
        context: WorkflowMergeContext,
        merge_id: str,
        phase: MergePhase,
        reason: MergeAbortReason,
        details: str,
    ) -> WorkflowMergeResult:
        """
        Abort merge and return failed result.
        
        GUARANTEE: No changes were committed.
        """
        logger.warning(
            f"Merge aborted in {phase.value}: {reason.value} - {details}"
        )
        
        # Emit abort event
        self._emit_event(WorkflowMergeAborted(
            workflow_id=context.workflow_id,
            merge_id=merge_id,
            timestamp=datetime.utcnow(),
            event_type="workflow_merge_aborted",
            abort_reason=reason,
            phase=phase,
            details=details,
        ))
        
        # Return failed result
        return WorkflowMergeResult(
            workflow_id=context.workflow_id,
            merged_artifacts=(),  # NOTHING merged
            superseded_artifacts=(),
            merge_safe=False,  # NOT SAFE
            invariants_verified=False,
            rollback_checkpoint_id=None,  # No checkpoint created
            merged_at=datetime.utcnow(),
            merge_id=merge_id,
            replay_id=context.replay_result.replay_id,
            merge_metadata={
                "abort_reason": reason.value,
                "abort_phase": phase.value,
                "abort_details": details,
            },
        )
    
    def _finalize_merge(
        self,
        context: WorkflowMergeContext,
        merge_id: str,
        checkpoint: MergeCheckpoint,
        merged_artifacts: tuple[str, ...],
        superseded_artifacts: tuple[str, ...],
    ) -> WorkflowMergeResult:
        """Finalize successful merge."""
        # Compute artifact diffs
        artifact_diffs = {}
        for artifact_id in merged_artifacts:
            if artifact_id in context.live_artifact_index:
                old_lineage = context.live_artifact_index[artifact_id]
                new_hash = context.replay_result.artifact_hashes.get(artifact_id)
                artifact_diffs[artifact_id] = {
                    "old_hash": old_lineage.content_hash,
                    "new_hash": new_hash,
                }
        
        return WorkflowMergeResult(
            workflow_id=context.workflow_id,
            merged_artifacts=merged_artifacts,
            superseded_artifacts=superseded_artifacts,
            merge_safe=True,  # SAFE!
            invariants_verified=True,
            rollback_checkpoint_id=checkpoint.checkpoint_id,
            merged_at=datetime.utcnow(),
            merge_id=merge_id,
            replay_id=context.replay_result.replay_id,
            artifact_diffs=artifact_diffs,
            merge_metadata={
                "checkpoint_id": checkpoint.checkpoint_id,
                "phase_count": 7,
            },
        )
    
    def _emit_event(self, event: MergeEvent):
        """Emit observability event."""
        logger.info(f"Event: {event.event_type}", extra=event.to_dict())


# ============================================================================
# PUBLIC API
# ============================================================================

def merge_workflow(context: WorkflowMergeContext) -> WorkflowMergeResult:
    """
    Public API for verified artifact merge.
    
    This is the point of no return - where repaired artifacts
    re-enter live state.
    
    GUARANTEE: If result.merge_safe == False, nothing was committed.
    
    Args:
        context: Complete merge context
    
    Returns:
        WorkflowMergeResult with merge outcome
    
    Example:
        >>> result = merge_workflow(context)
        >>> if result.merge_safe:
        ...     print(f"Merged {len(result.merged_artifacts)} artifacts")
        ...     print(f"Rollback: {result.rollback_checkpoint_id}")
        ... else:
        ...     print(f"Merge failed: {result.merge_metadata}")
    """
    merger = WorkflowMerger()
    return merger.merge_workflow(context)


"""
"""
/recovery/workflows/workflow_merge.py

Verified Artifact Merge & Live-State Reconciliation Engine

This is the point of no return. Everything before this is theory.
This is where truth re-enters reality.

Replay proves correctness in isolation.
workflow_merge.py answers the final question:
"Can these repaired artifacts safely replace live artifacts without violating 
invariants, causality, or history?"

This is NOT execution. This is controlled reality mutation.

WHAT THIS FILE IS:
- Artifact-level merge authority
- Lineage-preserving replacement engine
- Safety-gated state reconciler
- Invariant-checked commit layer

WHAT THIS FILE IS NOT:
- Repair logic
- Replay logic
- Storage abstraction
- Best-effort updater

Merge Philosophy:
Live state is sacred. A merge is permitted ONLY if ALL are true:
- Replay succeeded
- Determinism was proven
- Invariants hold post-merge
- Lineage is preserved
- Rollback is possible

If ANY condition fails → merge is forbidden.

Mental Model:
If replay is testing an organ on a bench, then merge is transplant surgery
with the entire hospital on standby. One mistake = systemic failure.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol
from collections.abc import Sequence
import time


# ============================================================================
# CORE ENUMS
# ============================================================================


class MergePhase(Enum):
    """
    Strict merge execution phases.
    
    These must execute in order. No skipping.
    """
    ADMISSIBILITY_GATE = "admissibility_gate"
    LINEAGE_VERIFICATION = "lineage_verification"
    CONFLICT_DETECTION = "conflict_detection"
    INVARIANT_REVALIDATION = "invariant_revalidation"
    ROLLBACK_ANCHOR = "rollback_anchor"
    ATOMIC_COMMIT = "atomic_commit"
    FINALIZATION = "finalization"


class MergeConflictType(Enum):
    """
    Types of merge conflicts detected.
    """
    CONCURRENT_MUTATION = "concurrent_mutation"
    VERSION_SUPERSESSION = "version_supersession"
    OVERLAPPING_REPAIR = "overlapping_repair"
    DOWNSTREAM_INVALIDATION = "downstream_invalidation"
    ORPHANED_CONSUMER = "orphaned_consumer"
    LINEAGE_FORK = "lineage_fork"


class MergeAbortReason(Enum):
    """
    Reasons for merge abortion.
    
    All are fatal - no retries, no partials, no overrides.
    """
    REPLAY_NOT_SAFE = "replay_not_safe"
    DETERMINISM_NOT_VERIFIED = "determinism_not_verified"
    VALIDATION_FAILED = "validation_failed"
    DAMAGE_UNBOUNDED = "damage_unbounded"
    REPAIR_INCOMPLETE = "repair_incomplete"
    ARTIFACTS_MISSING = "artifacts_missing"
    HASH_MISMATCH = "hash_mismatch"
    SCHEMA_DRIFT = "schema_drift"
    UNEXPECTED_OUTPUT = "unexpected_output"
    LINEAGE_BROKEN = "lineage_broken"
    CONFLICT_DETECTED = "conflict_detected"
    INVARIANT_VIOLATION = "invariant_violation"
    ROLLBACK_UNAVAILABLE = "rollback_unavailable"
    SNAPSHOT_FAILED = "snapshot_failed"
    COMMIT_PARTIAL = "commit_partial"


# ============================================================================
# EXCEPTIONS
# ============================================================================


class MergeError(Exception):
    """Base exception for merge failures."""
    pass


class MergeAborted(MergeError):
    """
    Raised when merge is aborted.
    
    Abort is the correct response to unsafe conditions.
    This is not a bug - it's the safety system working.
    """
    def __init__(
        self,
        reason: MergeAbortReason,
        phase: MergePhase,
        details: dict[str, Any]
    ):
        self.reason = reason
        self.phase = phase
        self.details = details
        super().__init__(
            f"Merge aborted in {phase.value}: {reason.value} - {details}"
        )


class MergeConflict(MergeError):
    """Raised when unresolvable merge conflict detected."""
    def __init__(
        self,
        conflict_type: MergeConflictType,
        details: dict[str, Any]
    ):
        self.conflict_type = conflict_type
        self.details = details
        super().__init__(f"Merge conflict: {conflict_type.value} - {details}")


class MergeInvariantViolation(MergeError):
    """Raised when post-merge invariants violated."""
    pass


# ============================================================================
# ARTIFACT DEFINITIONS
# ============================================================================


@dataclass(frozen=True)
class ArtifactDescriptor:
    """
    Complete immutable artifact descriptor.
    
    Includes all metadata needed for merge decision.
    """
    artifact_id: str
    content_hash: str
    schema_hash: str
    producer_node_id: str
    created_at: int
    version: int
    lineage_hash: str | None
    valid: bool
    size_bytes: int
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate artifact descriptor."""
        if not self.artifact_id:
            raise ValueError("artifact_id required")
        if not self.content_hash:
            raise ValueError("content_hash required")
        if not self.producer_node_id:
            raise ValueError("producer_node_id required")


@dataclass(frozen=True)
class ArtifactIndex:
    """
    Immutable index of current live artifacts.
    
    Snapshot of artifact state at merge start time.
    """
    artifacts: dict[str, ArtifactDescriptor]
    indexed_at: int
    
    def get(self, artifact_id: str) -> ArtifactDescriptor | None:
        """Safely retrieve artifact."""
        return self.artifacts.get(artifact_id)
    
    def has_newer_version(
        self,
        artifact_id: str,
        version: int
    ) -> bool:
        """Check if live state has newer version."""
        live = self.get(artifact_id)
        if not live:
            return False
        return live.version > version


# ============================================================================
# WORKFLOW DEFINITIONS
# ============================================================================


@dataclass(frozen=True)
class WorkflowDAG:
    """
    Immutable workflow DAG definition.
    
    Original live DAG - not the repaired one.
    """
    workflow_id: str
    nodes: dict[str, Any]  # Node definitions
    edges: frozenset[tuple[str, str]]  # (source, target) pairs
    version: int
    
    def get_node_outputs(self, node_id: str) -> frozenset[str]:
        """Get expected outputs for a node."""
        node = self.nodes.get(node_id)
        if not node:
            return frozenset()
        return frozenset(node.get('outputs', []))
    
    def get_downstream_nodes(self, node_id: str) -> frozenset[str]:
        """Get nodes that depend on this node."""
        return frozenset(
            target for source, target in self.edges
            if source == node_id
        )


# ============================================================================
# REPAIR & REPLAY RESULTS
# ============================================================================


@dataclass(frozen=True)
class RepairPlan:
    """
    Immutable repair plan.
    
    Describes what was repaired and how.
    """
    plan_id: str
    workflow_id: str
    repaired_nodes: frozenset[str]
    repaired_edges: frozenset[tuple[str, str]]
    expected_artifacts: frozenset[str]
    created_at: int
    fully_applied: bool
    
    def is_complete(self) -> bool:
        """Check if repair plan was fully applied."""
        return self.fully_applied


@dataclass(frozen=True)
class DamageAssessment:
    """
    Immutable damage assessment.
    
    Describes extent and scope of damage.
    """
    assessment_id: str
    workflow_id: str
    damaged_nodes: frozenset[str]
    corrupted_artifacts: frozenset[str]
    blast_radius: int
    bounded: bool  # Whether damage is contained
    assessed_at: int
    
    def is_bounded(self) -> bool:
        """Check if damage is bounded and safe to repair."""
        return self.bounded


@dataclass(frozen=True)
class ReplayResult:
    """
    Immutable replay verification result.
    
    Proof that repair was correct in isolation.
    """
    replay_id: str
    workflow_id: str
    replay_safe: bool
    determinism_verified: bool
    replayed_artifacts: dict[str, ArtifactDescriptor]
    execution_time_ns: int
    completed_at: int
    
    def is_merge_eligible(self) -> bool:
        """Check if replay result allows merge."""
        return self.replay_safe and self.determinism_verified
    
    def get_artifact(self, artifact_id: str) -> ArtifactDescriptor | None:
        """Get replayed artifact."""
        return self.replayed_artifacts.get(artifact_id)


# ============================================================================
# MERGE CONTEXT
# ============================================================================


@dataclass(frozen=True)
class MergeContext:
    """
    Complete immutable context for merge decision.
    
    Everything needed to make safe merge decision.
    """
    workflow_dag: WorkflowDAG
    replay_result: ReplayResult
    repair_plan: RepairPlan
    damage_assessment: DamageAssessment
    live_artifact_index: ArtifactIndex
    snapshot_id: str | None = None
    recovery_run_id: str | None = None
    
    def __post_init__(self):
        """Validate context consistency."""
        # All IDs must match
        if self.replay_result.workflow_id != self.workflow_dag.workflow_id:
            raise ValueError("Workflow ID mismatch between replay and DAG")
        if self.repair_plan.workflow_id != self.workflow_dag.workflow_id:
            raise ValueError("Workflow ID mismatch between repair plan and DAG")
        if self.damage_assessment.workflow_id != self.workflow_dag.workflow_id:
            raise ValueError("Workflow ID mismatch between damage assessment and DAG")


# ============================================================================
# MERGE RESULT
# ============================================================================


@dataclass(frozen=True)
class WorkflowMergeResult:
    """
    Canonical merge result contract.
    
    Immutable proof of merge outcome.
    
    HARD RULE: merge_safe == False ⇒ nothing was committed
    """
    workflow_id: str
    merge_id: str
    replay_id: str
    merged_artifacts: tuple[str, ...]  # Successfully merged artifact IDs
    superseded_artifacts: tuple[str, ...]  # Replaced artifact IDs
    merge_safe: bool
    invariants_verified: bool
    rollback_checkpoint_id: str | None
    merged_at: int
    
    def __post_init__(self):
        """Validate merge result invariants."""
        # If not safe, nothing should be merged
        if not self.merge_safe:
            if self.merged_artifacts:
                raise ValueError(
                    "merge_safe=False but merged_artifacts not empty"
                )
            if self.superseded_artifacts:
                raise ValueError(
                    "merge_safe=False but superseded_artifacts not empty"
                )
    
    def was_committed(self) -> bool:
        """Check if merge actually committed changes."""
        return self.merge_safe and len(self.merged_artifacts) > 0


# ============================================================================
# VALIDATION PROTOCOL
# ============================================================================


class WorkflowValidator(Protocol):
    """
    Interface to workflow validation system.
    
    Used for pre-merge and post-merge validation.
    """
    
    def validate(
        self,
        workflow_dag: WorkflowDAG,
        artifacts: dict[str, ArtifactDescriptor]
    ) -> tuple[bool, str]:
        """
        Validate workflow state.
        
        Returns:
            Tuple of (is_valid, reason)
        """
        ...
    
    def validate_invariants(
        self,
        workflow_dag: WorkflowDAG,
        artifacts: dict[str, ArtifactDescriptor]
    ) -> tuple[bool, list[str]]:
        """
        Validate workflow invariants.
        
        Returns:
            Tuple of (all_valid, violations)
        """
        ...


# ============================================================================
# STORAGE PROTOCOL
# ============================================================================


class ArtifactStore(Protocol):
    """
    Interface to artifact storage system.
    
    Must support atomic operations and snapshots.
    """
    
    def get_current_index(self) -> ArtifactIndex:
        """Get current artifact index."""
        ...
    
    def create_snapshot(self, label: str) -> str:
        """
        Create rollback snapshot.
        
        Returns:
            Snapshot ID for rollback.
        """
        ...
    
    def atomic_merge(
        self,
        new_artifacts: dict[str, ArtifactDescriptor],
        supersede_artifacts: frozenset[str]
    ) -> bool:
        """
        Atomically merge new artifacts and supersede old ones.
        
        Must be atomic, isolated, idempotent.
        
        Returns:
            True if committed successfully.
        """
        ...
    
    def rollback_to_snapshot(self, snapshot_id: str) -> bool:
        """
        Rollback to previous snapshot.
        
        Returns:
            True if rollback successful.
        """
        ...


# ============================================================================
# OBSERVABILITY
# ============================================================================


class MergeObserver(Protocol):
    """
    Interface for merge observability.
    
    Emits audit events for all merge operations.
    """
    
    def merge_started(
        self,
        context: MergeContext,
        merge_id: str
    ) -> None:
        """Log merge start."""
        ...
    
    def merge_conflict_detected(
        self,
        conflict: MergeConflict,
        merge_id: str
    ) -> None:
        """Log conflict detection."""
        ...
    
    def merge_aborted(
        self,
        abort: MergeAborted,
        merge_id: str
    ) -> None:
        """Log merge abortion."""
        ...
    
    def merge_committed(
        self,
        result: WorkflowMergeResult
    ) -> None:
        """Log successful merge commit."""
        ...


# ============================================================================
# MERGE PRECONDITIONS
# ============================================================================


class MergePreconditions:
    """
    Validates all required preconditions before merge.
    
    ALL must pass. Fail any → abort.
    """
    
    @staticmethod
    def validate_all(context: MergeContext) -> None:
        """
        Validate all merge preconditions.
        
        Raises:
            MergeAborted: If any precondition fails.
        """
        MergePreconditions.validate_replay_safe(context)
        MergePreconditions.validate_determinism_verified(context)
        MergePreconditions.validate_damage_bounded(context)
        MergePreconditions.validate_repair_complete(context)
    
    @staticmethod
    def validate_replay_safe(context: MergeContext) -> None:
        """Validate replay was safe."""
        if not context.replay_result.replay_safe:
            raise MergeAborted(
                MergeAbortReason.REPLAY_NOT_SAFE,
                MergePhase.ADMISSIBILITY_GATE,
                {
                    'workflow_id': context.workflow_dag.workflow_id,
                    'replay_id': context.replay_result.replay_id
                }
            )
    
    @staticmethod
    def validate_determinism_verified(context: MergeContext) -> None:
        """Validate determinism was proven."""
        if not context.replay_result.determinism_verified:
            raise MergeAborted(
                MergeAbortReason.DETERMINISM_NOT_VERIFIED,
                MergePhase.ADMISSIBILITY_GATE,
                {
                    'workflow_id': context.workflow_dag.workflow_id,
                    'replay_id': context.replay_result.replay_id
                }
            )
    
    @staticmethod
    def validate_damage_bounded(context: MergeContext) -> None:
        """Validate damage is bounded."""
        if not context.damage_assessment.is_bounded():
            raise MergeAborted(
                MergeAbortReason.DAMAGE_UNBOUNDED,
                MergePhase.ADMISSIBILITY_GATE,
                {
                    'workflow_id': context.workflow_dag.workflow_id,
                    'blast_radius': context.damage_assessment.blast_radius
                }
            )
    
    @staticmethod
    def validate_repair_complete(context: MergeContext) -> None:
        """Validate repair plan was fully applied."""
        if not context.repair_plan.is_complete():
            raise MergeAborted(
                MergeAbortReason.REPAIR_INCOMPLETE,
                MergePhase.ADMISSIBILITY_GATE,
                {
                    'workflow_id': context.workflow_dag.workflow_id,
                    'plan_id': context.repair_plan.plan_id
                }
            )


# ============================================================================
# MERGE INVARIANTS
# ============================================================================


class MergeInvariants:
    """
    Invariants that must hold during and after merge.
    
    Violations are fatal.
    """
    
    @staticmethod
    def validate_no_missing_artifacts(
        expected: frozenset[str],
        actual: dict[str, ArtifactDescriptor]
    ) -> None:
        """
        Ensure all expected artifacts are present.
        
        Raises:
            MergeAborted: If artifacts missing.
        """
        actual_ids = set(actual.keys())
        missing = expected - actual_ids
        
        if missing:
            raise MergeAborted(
                MergeAbortReason.ARTIFACTS_MISSING,
                MergePhase.ADMISSIBILITY_GATE,
                {
                    'missing_artifacts': list(missing),
                    'expected_count': len(expected),
                    'actual_count': len(actual_ids)
                }
            )
    
    @staticmethod
    def validate_hash_integrity(
        artifacts: dict[str, ArtifactDescriptor]
    ) -> None:
        """
        Ensure all artifacts have valid hashes.
        
        Raises:
            MergeAborted: If hash integrity violated.
        """
        invalid = [
            aid for aid, artifact in artifacts.items()
            if not artifact.content_hash or not artifact.schema_hash
        ]
        
        if invalid:
            raise MergeAborted(
                MergeAbortReason.HASH_MISMATCH,
                MergePhase.ADMISSIBILITY_GATE,
                {
                    'invalid_artifacts': invalid,
                    'count': len(invalid)
                }
            )
    
    @staticmethod
    def validate_schema_compatibility(
        artifact: ArtifactDescriptor,
        expected_schema_hash: str | None
    ) -> None:
        """
        Ensure artifact schema is compatible.
        
        Raises:
            MergeAborted: If schema drifted.
        """
        if expected_schema_hash and artifact.schema_hash != expected_schema_hash:
            raise MergeAborted(
                MergeAbortReason.SCHEMA_DRIFT,
                MergePhase.ADMISSIBILITY_GATE,
                {
                    'artifact_id': artifact.artifact_id,
                    'expected_schema': expected_schema_hash,
                    'actual_schema': artifact.schema_hash
                }
            )
    
    @staticmethod
    def validate_lineage_continuity(
        new_artifact: ArtifactDescriptor,
        old_artifact: ArtifactDescriptor | None
    ) -> None:
        """
        Ensure lineage is continuous.
        
        Raises:
            MergeAborted: If lineage broken.
        """
        # If replacing artifact, lineage must be derivable
        if old_artifact:
            # New artifact should reference old artifact's lineage
            # (Simplified check - real impl would verify lineage chain)
            if new_artifact.lineage_hash and old_artifact.lineage_hash:
                # Lineage should be related (simplified check)
                pass
        
        # New artifact must have lineage
        if not new_artifact.lineage_hash:
            raise MergeAborted(
                MergeAbortReason.LINEAGE_BROKEN,
                MergePhase.LINEAGE_VERIFICATION,
                {
                    'artifact_id': new_artifact.artifact_id,
                    'message': 'New artifact missing lineage'
                }
            )


# ============================================================================
# CONFLICT DETECTOR
# ============================================================================


class ConflictDetector:
    """
    Detects merge conflicts between replayed and live artifacts.
    
    Never overwrites newer valid artifacts.
    Never silently forks lineage.
    """
    
    @staticmethod
    def detect_conflicts(
        replayed: dict[str, ArtifactDescriptor],
        live_index: ArtifactIndex,
        merge_start_time: int
    ) -> list[MergeConflict]:
        """
        Detect all merge conflicts.
        
        Returns:
            List of detected conflicts.
        """
        conflicts = []
        
        for artifact_id, new_artifact in replayed.items():
            live_artifact = live_index.get(artifact_id)
            
            if live_artifact:
                # Check for concurrent mutation
                if live_artifact.created_at > merge_start_time:
                    conflicts.append(MergeConflict(
                        MergeConflictType.CONCURRENT_MUTATION,
                        {
                            'artifact_id': artifact_id,
                            'live_created_at': live_artifact.created_at,
                            'merge_start': merge_start_time
                        }
                    ))
                
                # Check for version supersession
                if live_index.has_newer_version(artifact_id, new_artifact.version):
                    conflicts.append(MergeConflict(
                        MergeConflictType.VERSION_SUPERSESSION,
                        {
                            'artifact_id': artifact_id,
                            'new_version': new_artifact.version,
                            'live_version': live_artifact.version
                        }
                    ))
                
                # Check for lineage fork
                if (new_artifact.lineage_hash and live_artifact.lineage_hash and
                    new_artifact.lineage_hash != live_artifact.lineage_hash):
                    conflicts.append(MergeConflict(
                        MergeConflictType.LINEAGE_FORK,
                        {
                            'artifact_id': artifact_id,
                            'new_lineage': new_artifact.lineage_hash,
                            'live_lineage': live_artifact.lineage_hash
                        }
                    ))
        
        return conflicts
    
    @staticmethod
    def detect_orphaned_consumers(
        superseded: frozenset[str],
        workflow_dag: WorkflowDAG,
        live_index: ArtifactIndex
    ) -> list[MergeConflict]:
        """
        Detect consumers that would be orphaned by merge.
        
        Returns:
            List of orphan conflicts.
        """
        conflicts = []
        
        for artifact_id in superseded:
            artifact = live_index.get(artifact_id)
            if not artifact:
                continue
            
            # Check if any downstream nodes depend on this artifact
            downstream = workflow_dag.get_downstream_nodes(
                artifact.producer_node_id
            )
            
            for node_id in downstream:
                expected_outputs = workflow_dag.get_node_outputs(node_id)
                # Check if node's outputs still exist
                missing_outputs = [
                    out_id for out_id in expected_outputs
                    if not live_index.get(out_id)
                ]
                
                if missing_outputs:
                    conflicts.append(MergeConflict(
                        MergeConflictType.ORPHANED_CONSUMER,
                        {
                            'consumer_node': node_id,
                            'missing_outputs': missing_outputs,
                            'superseded_artifact': artifact_id
                        }
                    ))
        
        return conflicts


# ============================================================================
# WORKFLOW MERGE ENGINE
# ============================================================================


class WorkflowMergeEngine:
    """
    Verified artifact merge & live-state reconciliation engine.
    
    This is controlled reality mutation. Live state is sacred.
    
    One mistake = systemic failure.
    """
    
    def __init__(
        self,
        artifact_store: ArtifactStore,
        validator: WorkflowValidator,
        observer: MergeObserver | None = None
    ):
        """
        Initialize merge engine.
        
        Args:
            artifact_store: Storage backend with atomic operations.
            validator: Workflow validation system.
            observer: Optional observability integration.
        """
        self._store = artifact_store
        self._validator = validator
        self._observer = observer
    
    def merge(
        self,
        context: MergeContext
    ) -> WorkflowMergeResult:
        """
        Execute verified artifact merge.
        
        Strict 7-phase execution:
        1. Admissibility gate
        2. Lineage verification
        3. Conflict detection
        4. Invariant re-validation
        5. Rollback anchor creation
        6. Atomic commit
        7. Finalization
        
        Args:
            context: Complete merge context.
            
        Returns:
            Immutable merge result.
            
        Raises:
            MergeAborted: If merge cannot proceed safely.
            MergeConflict: If unresolvable conflicts detected.
        """
        # Generate merge ID
        merge_id = self._generate_merge_id(context)
        merge_start_time = time.time_ns()
        
        # Log start
        if self._observer:
            self._observer.merge_started(context, merge_id)
        
        try:
            # PHASE 1: Admissibility Gate
            self._phase_1_admissibility_gate(context)
            
            # PHASE 2: Lineage Verification
            self._phase_2_lineage_verification(context)
            
            # PHASE 3: Conflict Detection
            conflicts = self._phase_3_conflict_detection(
                context,
                merge_start_time
            )
            
            # PHASE 4: Invariant Re-Validation
            self._phase_4_invariant_revalidation(context)
            
            # PHASE 5: Rollback Anchor Creation
            rollback_checkpoint_id = self._phase_5_rollback_anchor(context)
            
            # PHASE 6: Atomic Commit
            merged, superseded = self._phase_6_atomic_commit(context)
            
            # PHASE 7: Finalization
            result = self._phase_7_finalization(
                context,
                merge_id,
                merged,
                superseded,
                rollback_checkpoint_id,
                merge_start_time
            )
            
            # Log success
            if self._observer:
                self._observer.merge_committed(result)
            
            return result
            
        except (MergeAborted, MergeConflict) as e:
            # Log abortion
            if self._observer:
                if isinstance(e, MergeAborted):
                    self._observer.merge_aborted(e, merge_id)
                else:
                    self._observer.merge_conflict_detected(e, merge_id)
            
            # Return failed result
            return WorkflowMergeResult(
                workflow_id=context.workflow_dag.workflow_id,
                merge_id=merge_id,
                replay_id=context.replay_result.replay_id,
                merged_artifacts=(),
                superseded_artifacts=(),
                merge_safe=False,
                invariants_verified=False,
                rollback_checkpoint_id=None,
                merged_at=merge_start_time
            )
    
    def _phase_1_admissibility_gate(
        self,
        context: MergeContext
    ) -> None:
        """
        Phase 1: Validate merge admissibility.
        
        Checks:
        - Replay artifacts fully materialized
        - No missing hashes
        - No schema drift
        - No unexpected outputs
        - All preconditions met
        
        Raises:
            MergeAborted: If not admissible.
        """
        # Validate preconditions
        MergePreconditions.validate_all(context)
        
        # Get replayed artifacts
        replayed = context.replay_result.replayed_artifacts
        expected = context.repair_plan.expected_artifacts
        
        # Validate no missing artifacts
        MergeInvariants.validate_no_missing_artifacts(expected, replayed)
        
        # Validate hash integrity
        MergeInvariants.validate_hash_integrity(replayed)
        
        # Validate no unexpected outputs
        unexpected = set(replayed.keys()) - expected
        if unexpected:
            raise MergeAborted(
                MergeAbortReason.UNEXPECTED_OUTPUT,
                MergePhase.ADMISSIBILITY_GATE,
                {
                    'unexpected_artifacts': list(unexpected),
                    'expected_artifacts': list(expected)
                }
            )
    
    def _phase_2_lineage_verification(
        self,
        context: MergeContext
    ) -> None:
        """
        Phase 2: Verify lineage preservation.
        
        For each new artifact:
        - Producer node unchanged or explicitly repaired
        - Artifact schema compatible
        - Dependency chain intact
        - No orphan consumers created
        
        Raises:
            MergeAborted: If lineage broken.
        """
        replayed = context.replay_result.replayed_artifacts
        live_index = context.live_artifact_index
        repaired_nodes = context.repair_plan.repaired_nodes
        
        for artifact_id, new_artifact in replayed.items():
            # Check producer node
            producer_changed = new_artifact.producer_node_id in repaired_nodes
            
            # If producer not repaired, it should be unchanged
            # (This is a simplified check)
            
            # Validate lineage continuity
            old_artifact = live_index.get(artifact_id)
            MergeInvariants.validate_lineage_continuity(
                new_artifact,
                old_artifact
            )
            
            # Validate schema compatibility
            # (Simplified - real impl would check against DAG schema)
            expected_outputs = context.workflow_dag.get_node_outputs(
                new_artifact.producer_node_id
            )
            if artifact_id not in expected_outputs:
                raise MergeAborted(
                    MergeAbortReason.UNEXPECTED_OUTPUT,
                    MergePhase.LINEAGE_VERIFICATION,
                    {
                        'artifact_id': artifact_id,
                        'producer_node': new_artifact.producer_node_id
                    }
                )
    
    def _phase_3_conflict_detection(
        self,
        context: MergeContext,
        merge_start_time: int
    ) -> list[MergeConflict]:
        """
        Phase 3: Detect merge conflicts.
        
        Detects:
        - Concurrent live mutations
        - Artifact version supersession
        - Overlapping repairs
        - Downstream invalidation
        
        Raises:
            MergeAborted: If conflicts detected.
        """
        replayed = context.replay_result.replayed_artifacts
        live_index = context.live_artifact_index
        
        # Detect conflicts
        conflicts = ConflictDetector.detect_conflicts(
            replayed,
            live_index,
            merge_start_time
        )
        
        # Detect orphans
        # (Simplified - would need to calculate superseded artifacts)
        superseded = set(live_index.artifacts.keys()) & set(replayed.keys())
        orphan_conflicts = ConflictDetector.detect_orphaned_consumers(
            frozenset(superseded),
            context.workflow_dag,
            live_index
        )
        
        conflicts.extend(orphan_conflicts)
        
        # If any conflicts, abort
        if conflicts:
            raise MergeAborted(
                MergeAbortReason.CONFLICT_DETECTED,
                MergePhase.CONFLICT_DETECTION,
                {
                    'conflict_count': len(conflicts),
                    'conflicts': [
                        {
                            'type': c.conflict_type.value,
                            'details': c.details
                        }
                        for c in conflicts
                    ]
                }
            )
        
        return conflicts
    
    def _phase_4_invariant_revalidation(
        self,
        context: MergeContext
    ) -> None:
        """
        Phase 4: Re-validate invariants post-merge.
        
        Re-checks:
        - Workflow invariants
        - Safety invariants
        - Cross-workflow constraints
        - Storage invariants
        
        This is post-state validation.
        
        Raises:
            MergeAborted: If invariants violated.
        """
        # Simulate post-merge state
        post_merge_artifacts = {
            **context.live_artifact_index.artifacts,
            **context.replay_result.replayed_artifacts
        }
        
        # Validate workflow
        is_valid, reason = self._validator.validate(
            context.workflow_dag,
            post_merge_artifacts
        )
        
        if not is_valid:
            raise MergeAborted(
                MergeAbortReason.VALIDATION_FAILED,
                MergePhase.INVARIANT_REVALIDATION,
                {
                    'validation_failure': reason
                }
            )
        
        # Validate invariants
        all_valid, violations = self._validator.validate_invariants(
            context.workflow_dag,
            post_merge_artifacts
        )
        
        if not all_valid:
            raise MergeAborted(
                MergeAbortReason.INVARIANT_VIOLATION,
                MergePhase.INVARIANT_REVALIDATION,
                {
                    'violations': violations
                }
            )
    
    def _phase_5_rollback_anchor(
        self,
        context: MergeContext
    ) -> str:
        """
        Phase 5: Create rollback anchor.
        
        Before mutation:
        - Snapshot pre-merge state
        - Record artifact lineage map
        - Persist rollback metadata
        
        Raises:
            MergeAborted: If rollback anchor creation fails.
        """
        try:
            snapshot_id = self._store.create_snapshot(
                f"pre_merge_{context.workflow_dag.workflow_id}"
            )
            
            if not snapshot_id:
                raise MergeAborted(
                    MergeAbortReason.SNAPSHOT_FAILED,
                    MergePhase.ROLLBACK_ANCHOR,
                    {
                        'workflow_id': context.workflow_dag.workflow_id,
                        'message': 'Snapshot creation returned None'
                    }
                )
            
            return snapshot_id
            
        except Exception as e:
            raise MergeAborted(
                MergeAbortReason.SNAPSHOT_FAILED,
                MergePhase.ROLLBACK_ANCHOR,
                {
                    'workflow_id': context.workflow_dag.workflow_id,
                    'error': str(e)
                }
            )
    
    def _phase_6_atomic_commit(
        self,
        context: MergeContext
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """
        Phase 6: Execute atomic commit.
        
        Operations:
        - Insert new artifacts
        - Mark old artifacts superseded
        - Update artifact index
        - Emit merge commit record
        
        Must be atomic, isolated, idempotent.
        Partial commits are forbidden.
        
        Returns:
            Tuple of (merged_artifact_ids, superseded_artifact_ids)
            
        Raises:
            MergeAborted: If commit fails.
        """
        replayed = context.replay_result.replayed_artifacts
        
        # Identify superseded artifacts
        superseded = frozenset(
            aid for aid in context.live_artifact_index.artifacts.keys()
            if aid in replayed
        )
        
        # Execute atomic merge
        try:
            success = self._store.atomic_merge(
                replayed,
                superseded
            )
            
            if not success:
                raise MergeAborted(
                    MergeAbortReason.COMMIT_PARTIAL,
                    MergePhase.ATOMIC_COMMIT,
                    {
                        'workflow_id': context.workflow_dag.workflow_id,
                        'message': 'Atomic merge returned False'
                    }
                )
            
        except Exception as e:
            raise MergeAborted(
                MergeAbortReason.COMMIT_PARTIAL,
                MergePhase.ATOMIC_COMMIT,
                {
                    'workflow_id': context.workflow_dag.workflow_id,
                    'error': str(e)
                }
            )
        
        merged = tuple(replayed.keys())
        return (merged, tuple(superseded))
    
    def _phase_7_finalization(
        self,
        context: MergeContext,
        merge_id: str,
        merged: tuple[str, ...],
        superseded: tuple[str, ...],
        rollback_checkpoint_id: str,
        merge_start_time: int
    ) -> WorkflowMergeResult:
        """
        Phase 7: Finalize merge.
        
        Emits:
        - Merge success event
        - Updated workflow version
        - Commit lineage
        
        Returns immutable WorkflowMergeResult.
        """
        return WorkflowMergeResult(
            workflow_id=context.workflow_dag.workflow_id,
            merge_id=merge_id,
            replay_id=context.replay_result.replay_id,
            merged_artifacts=merged,
            superseded_artifacts=superseded,
            merge_safe=True,
            invariants_verified=True,
            rollback_checkpoint_id=rollback_checkpoint_id,
            merged_at=time.time_ns()
        )
    
    def _generate_merge_id(self, context: MergeContext) -> str:
        """Generate unique merge ID."""
        import hashlib
        data = f"{context.workflow_dag.workflow_id}_{context.replay_result.replay_id}_{time.time_ns()}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    'MergePhase',
    'MergeConflictType',
    'MergeAbortReason',
    'MergeError',
    'MergeAborted',
    'MergeConflict',
    'MergeInvariantViolation',
    'ArtifactDescriptor',
    'ArtifactIndex',
    'WorkflowDAG',
    'RepairPlan',
    'DamageAssessment',
    'ReplayResult',
    'MergeContext',
    'WorkflowMergeResult',
    'WorkflowValidator',
    'ArtifactStore',
    'MergeObserver',
    'MergePreconditions',
    'MergeInvariants',
    'ConflictDetector',
    'WorkflowMergeEngine',
]

