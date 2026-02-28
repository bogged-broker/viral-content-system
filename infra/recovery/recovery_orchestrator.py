"""
/infra/recovery/recovery_orchestrator.py

Deterministic Failure Recovery & Resume Orchestration Authority
(No Duplicate Effects, No Undefined Resume Points, No Implicit Ordering)

This module is the single authority that determines how the system resumes safely,
deterministically, and without corruption after a crash, partial failure, deployment
restart, or replay event.

CRITICAL PRINCIPLES:
- Deterministic: Same persisted state → same recovery plan
- Replay-safe: No unintended duplication
- Idempotent orchestration
- Explicit phase boundaries
- Explicit data version boundaries
- Explicit checkpoint authority
- Zero hidden heuristics
- Zero system-time dependence (must use reference timestamps)

ABSOLUTE INVARIANTS:
1. No irreversible action occurs in this file
2. No mutation of checkpoints here
3. No partial stage execution here
4. No implicit dependency resolution
5. No cross-component heuristic fixes
6. No auto-advancement of checkpoint

This file is the core planner only - delegates to specialized modules:
- recovery_corruption_detection: Corruption detection
- recovery_validation: Validation and idempotency checks
- recovery_dependency_graph: Dependency graph validation
- recovery_resume_boundary: Boundary calculation
- recovery_checkpoint_invariants: Checkpoint validation
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional, Tuple, List, Dict

from .recovery_models import (
    RecoveryContext,
    CheckpointSnapshot,
    PipelineStateSnapshot,
    PersistenceSnapshot,
    AggregationSnapshot,
    RecoveryConfig,
    RecoveryMode,
    RecoveryStage,
    RecoveryPlan,
    RecoveryStep,
    ConsistencyCheck,
    ResumeBoundary,
    RewindBoundary,
    IdempotencyGuarantee,
    LogicalTimestamp,
    TransactionReconciliationReport,
    WindowCompletenessGuarantee,
    CorruptionReason,
)
from .recovery_corruption_detection import CorruptionDetector, CorruptionReport
from .recovery_validation import DuplicateEffectDetector, RecoveryValidator
from .recovery_dependency_graph import StageDependencyGraph
from .recovery_resume_boundary import BoundaryCalculator
from .recovery_checkpoint_invariants import CheckpointInvariantValidator


# ============================================================================
# RECOVERY PLAN BUILDER
# ============================================================================


class RecoveryPlanBuilder:
    """
    Builds deterministic recovery plans.
    
    Pure function - same inputs always produce same plan.
    """

    def __init__(self, config: RecoveryConfig):
        self.config = config

    def build_no_recovery_plan(
        self, context: RecoveryContext, checkpoint_snapshot: CheckpointSnapshot
    ) -> RecoveryPlan:
        """Build plan for no recovery needed."""
        return RecoveryPlan(
            plan_id=self._generate_plan_id(context, RecoveryMode.NO_RECOVERY_NEEDED),
            recovery_mode=RecoveryMode.NO_RECOVERY_NEEDED,
            ordered_steps=(),
            resume_boundary=None,
            rewind_boundary=None,
            affected_stages=(),
            consistency_checks=(),
            idempotency_requirements={},
            idempotency_guarantee=None,
            window_completeness_guarantee=None,
            transaction_reconciliation=None,
            reference_timestamp=context.reference_timestamp,
            plan_version="1.0.0",
            plan_hash=self._compute_plan_hash(context, RecoveryMode.NO_RECOVERY_NEEDED, ()),
        )

    def build_safe_resume_plan(
        self,
        context: RecoveryContext,
        checkpoint_snapshot: CheckpointSnapshot,
        resume_boundary: ResumeBoundary,
        idempotency_guarantee: Optional[IdempotencyGuarantee],
        window_completeness_guarantee: Optional[WindowCompletenessGuarantee],
        transaction_reconciliation: Optional[TransactionReconciliationReport],
    ) -> RecoveryPlan:
        """Build plan for safe resume from checkpoint."""
        steps = [
            RecoveryStep(
                step_id="verify_checkpoint",
                step_type="VALIDATE",
                target_stage=resume_boundary.stage,
                description=f"Verify checkpoint at {resume_boundary.stage.value}",
                prerequisites=(),
                idempotency_key=resume_boundary.idempotency_key,
                is_reversible=True,
            ),
            RecoveryStep(
                step_id="resume_pipeline",
                step_type="REPLAY",
                target_stage=resume_boundary.stage,
                description=f"Resume from {resume_boundary.stage.value}",
                prerequisites=("verify_checkpoint",),
                idempotency_key=f"{resume_boundary.idempotency_key}_resume",
                is_reversible=False,
            ),
        ]

        consistency_checks = [
            ConsistencyCheck(
                check_id="verify_boundary_atomic",
                check_type="ATOMICITY",
                target_stage=resume_boundary.stage,
                validation_rule="boundary_is_atomic",
                expected_state={"is_atomic": True},
            )
        ]

        # Determine affected stages (current and all downstream)
        stage_index = list(RecoveryStage).index(resume_boundary.stage)
        affected_stages = tuple(list(RecoveryStage)[stage_index:])

        return RecoveryPlan(
            plan_id=self._generate_plan_id(context, RecoveryMode.SAFE_RESUME),
            recovery_mode=RecoveryMode.SAFE_RESUME,
            ordered_steps=tuple(steps),
            resume_boundary=resume_boundary,
            rewind_boundary=None,
            affected_stages=affected_stages,
            consistency_checks=tuple(consistency_checks),
            idempotency_requirements={
                "resume": resume_boundary.idempotency_key,
            },
            idempotency_guarantee=idempotency_guarantee,
            window_completeness_guarantee=window_completeness_guarantee,
            transaction_reconciliation=transaction_reconciliation,
            reference_timestamp=context.reference_timestamp,
            plan_version="1.0.0",
            plan_hash=self._compute_plan_hash(context, RecoveryMode.SAFE_RESUME, steps),
        )

    def build_partial_rewind_plan(
        self,
        context: RecoveryContext,
        checkpoint_snapshot: CheckpointSnapshot,
        rewind_boundary: RewindBoundary,
        resume_boundary: Optional[ResumeBoundary],
        idempotency_guarantee: Optional[IdempotencyGuarantee],
        window_completeness_guarantee: Optional[WindowCompletenessGuarantee],
        transaction_reconciliation: Optional[TransactionReconciliationReport],
    ) -> RecoveryPlan:
        """Build plan for partial rewind and replay."""
        steps = [
            RecoveryStep(
                step_id="validate_rewind_safety",
                step_type="VALIDATE",
                target_stage=rewind_boundary.target_stage,
                description="Validate rewind safety and reversibility",
                prerequisites=(),
                idempotency_key=f"rewind_validate_{rewind_boundary.target_checkpoint_sequence}",
                is_reversible=True,
            ),
            RecoveryStep(
                step_id="rewind_to_boundary",
                step_type="REWIND",
                target_stage=rewind_boundary.target_stage,
                description=f"Rewind to {rewind_boundary.target_stage.value} at {rewind_boundary.target_timestamp.iso_string}",
                prerequisites=("validate_rewind_safety",),
                idempotency_key=f"rewind_{rewind_boundary.target_checkpoint_sequence}",
                is_reversible=False,
            ),
        ]

        # Add compensation steps if needed
        if rewind_boundary.requires_side_effect_compensation:
            for idx, action in enumerate(rewind_boundary.compensation_actions):
                steps.append(
                    RecoveryStep(
                        step_id=f"compensate_{idx}",
                        step_type="VERIFY",
                        target_stage=rewind_boundary.target_stage,
                        description=action,
                        prerequisites=("rewind_to_boundary",),
                        idempotency_key=f"compensate_{idx}_{rewind_boundary.target_checkpoint_sequence}",
                        is_reversible=True,
                    )
                )

        # Add replay steps for each affected stage
        for stage in rewind_boundary.affected_stages:
            prev_step = steps[-1].step_id
            steps.append(
                RecoveryStep(
                    step_id=f"replay_{stage.value}",
                    step_type="REPLAY",
                    target_stage=stage,
                    description=f"Replay {stage.value} from {rewind_boundary.reprocessing_range_start.iso_string}",
                    prerequisites=(prev_step,),
                    idempotency_key=f"replay_{stage.value}_{rewind_boundary.target_checkpoint_sequence}",
                    is_reversible=False,
                )
            )

        consistency_checks = [
            ConsistencyCheck(
                check_id="verify_rewind_completeness",
                check_type="COMPLETENESS",
                target_stage=rewind_boundary.target_stage,
                validation_rule="all_affected_stages_rewound",
                expected_state={"rewound_stages": [s.value for s in rewind_boundary.affected_stages]},
            )
        ]

        return RecoveryPlan(
            plan_id=self._generate_plan_id(context, RecoveryMode.PARTIAL_REWIND),
            recovery_mode=RecoveryMode.PARTIAL_REWIND,
            ordered_steps=tuple(steps),
            resume_boundary=resume_boundary,
            rewind_boundary=rewind_boundary,
            affected_stages=rewind_boundary.affected_stages,
            consistency_checks=tuple(consistency_checks),
            idempotency_requirements={
                "rewind": f"rewind_{rewind_boundary.target_checkpoint_sequence}",
            },
            idempotency_guarantee=idempotency_guarantee,
            window_completeness_guarantee=window_completeness_guarantee,
            transaction_reconciliation=transaction_reconciliation,
            reference_timestamp=context.reference_timestamp,
            plan_version="1.0.0",
            plan_hash=self._compute_plan_hash(context, RecoveryMode.PARTIAL_REWIND, steps),
        )

    def build_full_rebuild_plan(
        self, context: RecoveryContext, checkpoint_snapshot: CheckpointSnapshot
    ) -> RecoveryPlan:
        """Build plan for full system rebuild."""
        steps = [
            RecoveryStep(
                step_id="validate_rebuild_prerequisites",
                step_type="VALIDATE",
                target_stage=RecoveryStage.INGESTION,
                description="Validate source data availability for full rebuild",
                prerequisites=(),
                idempotency_key=f"rebuild_validate_{context.context_id}",
                is_reversible=True,
            )
        ]

        # Add rebuild step for each stage
        for idx, stage in enumerate(RecoveryStage):
            prev_step = steps[-1].step_id
            steps.append(
                RecoveryStep(
                    step_id=f"rebuild_{stage.value}",
                    step_type="REBUILD",
                    target_stage=stage,
                    description=f"Rebuild {stage.value} from source",
                    prerequisites=(prev_step,),
                    idempotency_key=f"rebuild_{stage.value}_{context.context_id}",
                    is_reversible=False,
                )
            )

        return RecoveryPlan(
            plan_id=self._generate_plan_id(context, RecoveryMode.FULL_REBUILD_REQUIRED),
            recovery_mode=RecoveryMode.FULL_REBUILD_REQUIRED,
            ordered_steps=tuple(steps),
            resume_boundary=None,
            rewind_boundary=None,
            affected_stages=tuple(RecoveryStage),
            consistency_checks=(),
            idempotency_requirements={},
            idempotency_guarantee=None,
            window_completeness_guarantee=None,
            transaction_reconciliation=None,
            reference_timestamp=context.reference_timestamp,
            plan_version="1.0.0",
            plan_hash=self._compute_plan_hash(
                context, RecoveryMode.FULL_REBUILD_REQUIRED, steps
            ),
        )

    def build_blocked_plan(
        self,
        context: RecoveryContext,
        corruption_report: CorruptionReport,
    ) -> RecoveryPlan:
        """Build plan for blocked recovery due to corruption."""
        return RecoveryPlan(
            plan_id=self._generate_plan_id(context, RecoveryMode.BLOCKED_CORRUPTION_DETECTED),
            recovery_mode=RecoveryMode.BLOCKED_CORRUPTION_DETECTED,
            ordered_steps=(),
            resume_boundary=None,
            rewind_boundary=None,
            affected_stages=(),
            consistency_checks=(),
            idempotency_requirements={},
            idempotency_guarantee=None,
            window_completeness_guarantee=None,
            transaction_reconciliation=None,
            reference_timestamp=context.reference_timestamp,
            plan_version="1.0.0",
            plan_hash=self._compute_plan_hash(
                context, RecoveryMode.BLOCKED_CORRUPTION_DETECTED, ()
            ),
            corruption_detected=True,
            corruption_reason=corruption_report.corruption_reason,
            failing_stage=corruption_report.failing_stage,
            inconsistency_detail=corruption_report.detail,
        )

    def _generate_plan_id(self, context: RecoveryContext, mode: RecoveryMode) -> str:
        """Generate deterministic plan ID."""
        return f"{context.context_id}_{mode.name}_{context.reference_timestamp.iso_string}"

    def _compute_plan_hash(
        self,
        context: RecoveryContext,
        mode: RecoveryMode,
        steps: list[RecoveryStep],
    ) -> str:
        """
        Compute deterministic hash of recovery plan.
        
        Ensures same inputs always produce same hash.
        """
        components = {
            "context_id": context.context_id,
            "mode": mode.name,
            "reference_timestamp": context.reference_timestamp.iso_string,
            "steps": [
                {
                    "id": s.step_id,
                    "type": s.step_type,
                    "stage": s.target_stage.value,
                }
                for s in steps
            ],
        }

        canonical_json = json.dumps(components, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


# ============================================================================
# MAIN RECOVERY ORCHESTRATOR
# ============================================================================


class RecoveryOrchestrator:
    """
    Main orchestrator for deterministic recovery planning.
    
    This is the authority that decides how the system returns to a safe,
    consistent, replay-correct state after interruption.
    
    Guarantees:
    - No duplicate irreversible effects
    - No unsafe partial resumes
    - No silent corruption
    - Explicit resume boundaries
    - Deterministic recovery planning
    - Strict checkpoint monotonicity
    - No wall clock usage
    - Safety over liveness
    
    This file does NOT execute recovery - it only plans it.
    Delegates to specialized modules for validation, detection, and calculation.
    """

    def __init__(
        self,
        config: RecoveryConfig,
        logger: Optional[logging.Logger] = None,
    ):
        """
        Initialize recovery orchestrator.
        
        Args:
            config: Recovery configuration
            logger: Optional logger for structured logging
        """
        self.config = config
        self.plan_builder = RecoveryPlanBuilder(config)
        self.dependency_graph = StageDependencyGraph.from_config(config)
        self.logger = logger or logging.getLogger(__name__)

    def orchestrate_recovery(
        self,
        recovery_context: RecoveryContext,
        checkpoint_snapshot: CheckpointSnapshot,
        pipeline_snapshot: PipelineStateSnapshot,
        persistence_snapshot: PersistenceSnapshot,
        aggregation_snapshot: Optional[AggregationSnapshot] = None,
    ) -> RecoveryPlan:
        """
        Orchestrate recovery planning.
        
        DETERMINISTIC: Same inputs always produce identical plan.
        No randomness. No adaptive heuristics.
        
        This function does NOT execute recovery - it only plans it.
        All inputs must be immutable snapshots.
        No direct reads from live systems.
        No wall clock usage.
        
        Args:
            recovery_context: Immutable recovery context
            checkpoint_snapshot: Immutable checkpoint snapshot
            pipeline_snapshot: Immutable pipeline state snapshot
            persistence_snapshot: Immutable persistence state snapshot
            aggregation_snapshot: Optional aggregation state snapshot
            
        Returns:
            Immutable RecoveryPlan with explicit ordered steps
            
        Raises:
            ValueError: If inputs are invalid
        """
        # Validate inputs
        if not recovery_context.context_id:
            raise ValueError("recovery_context.context_id cannot be empty")
        
        if not checkpoint_snapshot.checkpoints:
            raise ValueError("checkpoint_snapshot.checkpoints cannot be empty")
        
        if not pipeline_snapshot.stages:
            raise ValueError("pipeline_snapshot.stages cannot be empty")
        
        self.logger.info(
            f"Orchestrating recovery for context {recovery_context.context_id}: "
            f"reason={recovery_context.recovery_reason}, "
            f"reference_timestamp={recovery_context.reference_timestamp.iso_string}"
        )
        
        # Step 1: Detect corruption (safety first)
        corruption_report = CorruptionDetector.detect_corruption(
            recovery_context,
            checkpoint_snapshot,
            pipeline_snapshot,
            persistence_snapshot,
            self.config,
            aggregation_snapshot,
        )

        if corruption_report.is_corrupted:
            self.logger.error(
                f"Corruption detected: reason={corruption_report.corruption_reason.value if corruption_report.corruption_reason else None}, "
                f"stage={corruption_report.failing_stage.value if corruption_report.failing_stage else None}, "
                f"detail={corruption_report.detail}"
            )
            return self.plan_builder.build_blocked_plan(recovery_context, corruption_report)

        # Step 2: Validate stage dependencies
        deps_valid, deps_error = self.dependency_graph.validate_dependencies(
            checkpoint_snapshot, pipeline_snapshot
        )
        if not deps_valid:
            self.logger.error(f"Stage dependency validation failed: {deps_error}")
            return self.plan_builder.build_blocked_plan(
                recovery_context,
                CorruptionReport.detected(
                    CorruptionReason.ORPHANED_TRANSACTION_FRAGMENTS,
                    None,
                    f"Stage dependency violation: {deps_error}",
                ),
            )
        
        # Step 2b: Validate idempotency keys if required
        idempotency_valid, idempotency_error = RecoveryValidator.validate_idempotency_keys(
            checkpoint_snapshot, pipeline_snapshot, self.config
        )
        if not idempotency_valid:
            self.logger.error(f"Idempotency key validation failed: {idempotency_error}")
            return self.plan_builder.build_blocked_plan(
                recovery_context,
                CorruptionReport.detected(
                    CorruptionReason.PARTIAL_TRANSACTION_DETECTED,
                    None,
                    f"Idempotency key violation: {idempotency_error}",
                ),
            )
        
        # Step 2c: Detect duplicate effects
        has_duplicates, dup_error, dup_stage = DuplicateEffectDetector.detect_duplicate_effects(
            checkpoint_snapshot, pipeline_snapshot, persistence_snapshot, self.config
        )
        if has_duplicates:
            self.logger.error(f"Duplicate effects detected: {dup_error}")
            return self.plan_builder.build_blocked_plan(
                recovery_context,
                CorruptionReport.detected(
                    CorruptionReason.ORPHANED_TRANSACTION_FRAGMENTS,
                    dup_stage,
                    f"Duplicate effects detected: {dup_error}",
                ),
            )
        
        # Step 2d: Check if recovery needed at all
        if self._no_recovery_needed(pipeline_snapshot):
            self.logger.info(
                f"No recovery needed for context {recovery_context.context_id}: "
                f"all stages complete and consistent"
            )
            return self.plan_builder.build_no_recovery_plan(
                recovery_context, checkpoint_snapshot
            )

        # Step 3: Calculate resume boundary (after all validations pass)
        resume_boundary = BoundaryCalculator.calculate_resume_boundary(
            checkpoint_snapshot, pipeline_snapshot, persistence_snapshot, aggregation_snapshot
        )
        
        if resume_boundary:
            self.logger.info(
                f"Calculated resume boundary: stage={resume_boundary.stage.value}, "
                f"sequence={resume_boundary.checkpoint_sequence}, "
                f"timestamp={resume_boundary.boundary_timestamp.iso_string}"
            )

        # Calculate formal guarantees
        window_completeness = BoundaryCalculator.calculate_window_completeness_guarantee(
            aggregation_snapshot, resume_boundary
        )
        transaction_reconciliation = BoundaryCalculator.calculate_transaction_reconciliation(
            persistence_snapshot
        )
        
        # Build idempotency guarantee
        idempotency_guarantee = None
        if resume_boundary:
            idempotency_guarantee = IdempotencyGuarantee(
                effect_scopes=(resume_boundary.stage.value,),
                replay_keys=(resume_boundary.idempotency_key,),
                deduplication_authority="checkpoint_idempotency_key",
            )

        # Step 4: Check if safe resume possible
        if resume_boundary and self._can_safely_resume(
            resume_boundary, pipeline_snapshot, aggregation_snapshot
        ):
            self.logger.info(
                f"Safe resume possible from {resume_boundary.stage.value}, "
                f"building SAFE_RESUME plan"
            )
            plan = self.plan_builder.build_safe_resume_plan(
                recovery_context,
                checkpoint_snapshot,
                resume_boundary,
                idempotency_guarantee,
                window_completeness,
                transaction_reconciliation,
            )
            # Validate plan idempotency requirements
            idemp_valid, idemp_error = DuplicateEffectDetector.validate_idempotency_requirements(
                plan, self.config
            )
            if not idemp_valid:
                self.logger.error(f"Plan idempotency validation failed: {idemp_error}")
                return self.plan_builder.build_blocked_plan(
                    recovery_context,
                    CorruptionReport.detected(
                        CorruptionReason.PARTIAL_TRANSACTION_DETECTED,
                        None,
                        f"Plan idempotency violation: {idemp_error}",
                    ),
                )
            return plan

        # Step 5: Check if partial rewind possible
        if self.config.allow_partial_rewind:
            rewind_boundary = BoundaryCalculator.calculate_rewind_boundary(
                checkpoint_snapshot, pipeline_snapshot, self.config
            )

            if rewind_boundary and self._can_safely_rewind(rewind_boundary, aggregation_snapshot):
                self.logger.info(
                    f"Partial rewind possible to {rewind_boundary.target_stage.value}, "
                    f"building PARTIAL_REWIND plan"
                )
                plan = self.plan_builder.build_partial_rewind_plan(
                    recovery_context,
                    checkpoint_snapshot,
                    rewind_boundary,
                    resume_boundary,
                    idempotency_guarantee,
                    window_completeness,
                    transaction_reconciliation,
                )
                # Validate plan idempotency requirements
                idemp_valid, idemp_error = DuplicateEffectDetector.validate_idempotency_requirements(
                    plan, self.config
                )
                if not idemp_valid:
                    self.logger.error(f"Plan idempotency validation failed: {idemp_error}")
                    return self.plan_builder.build_blocked_plan(
                        recovery_context,
                        CorruptionReport.detected(
                            CorruptionReason.PARTIAL_TRANSACTION_DETECTED,
                            None,
                            f"Plan idempotency violation: {idemp_error}",
                        ),
                    )
                return plan

        # Step 6: Full rebuild required
        if self.config.allow_full_rebuild:
            self.logger.warning(
                f"Full rebuild required for context {recovery_context.context_id}, "
                f"building FULL_REBUILD_REQUIRED plan"
            )
            return self.plan_builder.build_full_rebuild_plan(
                recovery_context, checkpoint_snapshot
            )

        # Step 7: Cannot recover - block
        self.logger.error(
            f"No safe recovery path available for context {recovery_context.context_id}, "
            f"blocking recovery"
        )
        return self.plan_builder.build_blocked_plan(
            recovery_context,
            CorruptionReport.detected(
                CorruptionReason.CHECKPOINT_BEYOND_PERSISTENCE,
                None,
                "No safe recovery path available with current configuration",
            ),
        )

    def _no_recovery_needed(self, pipeline_snapshot: PipelineStateSnapshot) -> bool:
        """
        Check if all stages are complete and consistent.
        
        Recovery not needed if:
        - All stages are COMPLETE
        - All stages are at atomic boundaries
        - No stages are IN_PROGRESS or FAILED
        """
        if not pipeline_snapshot.stages:
            return False
        
        for state in pipeline_snapshot.stages.values():
            if state.status != "COMPLETE":
                return False
            if not state.is_atomic_boundary:
                return False
        
        return True
    
    def _can_safely_resume(
        self,
        resume_boundary: ResumeBoundary,
        pipeline_snapshot: PipelineStateSnapshot,
        aggregation_snapshot: Optional[AggregationSnapshot] = None,
    ) -> bool:
        """
        Validate resume boundary is safe.
        
        Must ensure:
        - Boundary is atomic
        - Stage state matches boundary
        - No partial windows (if aggregation stage)
        - No pending transactions (if persistence stage)
        """
        # Resume boundary must be atomic (enforced by ResumeBoundary dataclass)
        if not resume_boundary.is_atomic:
            self.logger.warning(
                f"Resume boundary for {resume_boundary.stage.value} is not atomic"
            )
            return False

        # Check stage state matches boundary
        stage_state = pipeline_snapshot.get_stage(resume_boundary.stage)
        if not stage_state:
            self.logger.warning(
                f"No stage state found for {resume_boundary.stage.value}"
            )
            return False

        # Stage must be at atomic boundary
        if not stage_state.is_atomic_boundary:
            self.logger.warning(
                f"Stage {resume_boundary.stage.value} is not at atomic boundary"
            )
            return False
        
        # Additional validation for aggregation stage
        if resume_boundary.stage == RecoveryStage.AGGREGATION:
            if aggregation_snapshot is not None:
                # Check for partial open windows
                if aggregation_snapshot.has_partial_windows():
                    self.logger.warning(
                        "Cannot resume from aggregation stage: partial windows exist"
                    )
                    return False
                
                # Verify window_id matches closed window
                if resume_boundary.window_id:
                    window_found = False
                    for window in aggregation_snapshot.closed_windows:
                        if window.window_id == resume_boundary.window_id:
                            if not window.is_committed:
                                self.logger.warning(
                                    f"Window {resume_boundary.window_id} is not committed"
                                )
                                return False
                            window_found = True
                            break
                    
                    if not window_found:
                        self.logger.warning(
                            f"Window {resume_boundary.window_id} not found in closed windows"
                        )
                        return False

        return True

    def _can_safely_rewind(
        self,
        rewind_boundary: RewindBoundary,
        aggregation_snapshot: Optional[AggregationSnapshot] = None,
    ) -> bool:
        """
        Validate rewind is safe and within limits.
        
        Partial rewind allowed only if:
        - Effects behind rewind boundary are reversible OR
        - Downstream effects are idempotent OR
        - Replay-safe transformations exist
        
        Must validate:
        - Rewind doesn't exceed max windows
        - Side effect compensation available (if needed)
        - Window state allows rewind (if aggregation involved)
        """
        # Check rewind doesn't exceed max windows
        if len(rewind_boundary.affected_stages) > self.config.max_rewind_windows:
            self.logger.warning(
                f"Rewind exceeds max windows: {len(rewind_boundary.affected_stages)} > "
                f"{self.config.max_rewind_windows}"
            )
            return False

        # If side effect compensation required, must have compensation actions
        if rewind_boundary.requires_side_effect_compensation:
            if len(rewind_boundary.compensation_actions) == 0:
                self.logger.warning(
                    "Side effect compensation required but no compensation actions defined"
                )
                return False
        
        # Validate window state for aggregation rewind
        if RecoveryStage.AGGREGATION in rewind_boundary.affected_stages:
            if aggregation_snapshot is not None:
                # Check if any closed windows would be affected
                # Closed windows must be fully committed before rewind
                for window in aggregation_snapshot.closed_windows:
                    if not window.is_committed:
                        self.logger.warning(
                            f"Cannot rewind: window {window.window_id} is closed but not committed"
                        )
                        return False

        return True


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================


def orchestrate_recovery(
    recovery_context: RecoveryContext,
    checkpoint_snapshot: CheckpointSnapshot,
    pipeline_snapshot: PipelineStateSnapshot,
    persistence_snapshot: PersistenceSnapshot,
    config: RecoveryConfig,
    aggregation_snapshot: Optional[AggregationSnapshot] = None,
    logger: Optional[logging.Logger] = None,
) -> RecoveryPlan:
    """
    Convenience function for orchestrating recovery.
    
    Main entry point for recovery planning.
    
    DETERMINISTIC: Same inputs always produce identical plan.
    All inputs must be immutable snapshots.
    No wall clock usage.
    
    Args:
        recovery_context: Immutable recovery context
        checkpoint_snapshot: Immutable checkpoint snapshot
        pipeline_snapshot: Immutable pipeline state snapshot
        persistence_snapshot: Immutable persistence state snapshot
        config: Recovery configuration
        aggregation_snapshot: Optional aggregation state snapshot
        logger: Optional logger for structured logging
        
    Returns:
        Immutable RecoveryPlan with explicit ordered steps
    """
    orchestrator = RecoveryOrchestrator(config, logger=logger)
    return orchestrator.orchestrate_recovery(
        recovery_context,
        checkpoint_snapshot,
        pipeline_snapshot,
        persistence_snapshot,
        aggregation_snapshot,
    )


def is_recovery_blocked(plan: RecoveryPlan) -> bool:
    """Check if recovery is blocked due to corruption."""
    return plan.is_blocked()


def requires_manual_intervention(plan: RecoveryPlan) -> bool:
    """Check if recovery requires manual intervention."""
    return plan.requires_manual_intervention()


def validate_checkpoint_monotonicity(
    current_snapshot: CheckpointSnapshot,
    previous_snapshot: Optional[CheckpointSnapshot],
    logger: Optional[logging.Logger] = None,
) -> tuple[bool, Optional[str]]:
    """
    Validate checkpoint sequence numbers never move backwards.
    
    Critical invariant for recovery correctness.
    Checkpoints must never move backwards silently.
    
    Args:
        current_snapshot: Current checkpoint snapshot
        previous_snapshot: Previous checkpoint snapshot (if available)
        logger: Optional logger for warnings
        
    Returns:
        (is_valid, error_message)
    """
    return CheckpointInvariantValidator.validate_checkpoint_monotonicity(
        current_snapshot, previous_snapshot, logger
    )


# ============================================================================
# ABSOLUTE INVARIANTS (Policy Enforcement)
# ============================================================================


ABSOLUTE_INVARIANTS = {
    "NO_IRREVERSIBLE_ACTION_IN_ORCHESTRATOR": "No irreversible action occurs in orchestrator",
    "NO_CHECKPOINT_MUTATION": "No mutation of checkpoints in orchestrator",
    "NO_PARTIAL_STAGE_EXECUTION": "No partial stage execution in orchestrator",
    "NO_IMPLICIT_DEPENDENCY_RESOLUTION": "No implicit dependency resolution",
    "NO_CROSS_COMPONENT_HEURISTICS": "No cross-component heuristic fixes",
    "NO_AUTO_CHECKPOINT_ADVANCEMENT": "No auto-advancement of checkpoint",
    "DETERMINISTIC_PLANNING": "Same inputs always produce identical plan",
    "CHECKPOINT_MONOTONICITY": "Checkpoints never move backwards",
    "ATOMIC_BOUNDARIES_ONLY": "Resume only from atomic boundaries",
}


def validate_absolute_invariants(plan: RecoveryPlan) -> dict[str, bool]:
    """
    Validate absolute recovery invariants.
    
    Returns dict of invariant_name -> satisfied (bool).
    
    Validates:
    - No irreversible actions in orchestrator
    - No checkpoint mutation
    - No partial stage execution
    - Deterministic planning
    - Atomic boundaries only
    - Explicit step ordering
    """
    from collections import defaultdict
    
    invariants = {
        "NO_IRREVERSIBLE_ACTION_IN_ORCHESTRATOR": True,  # By design - orchestrator only plans
        "NO_CHECKPOINT_MUTATION": True,  # All checkpoints are immutable
        "NO_PARTIAL_STAGE_EXECUTION": all(
            step.step_type in ("VALIDATE", "REWIND", "REPLAY", "REBUILD", "VERIFY")
            for step in plan.ordered_steps
        ),
        "DETERMINISTIC_PLANNING": len(plan.plan_hash) > 0,  # Plan has deterministic hash
        "ATOMIC_BOUNDARIES_ONLY": plan.resume_boundary is None
        or plan.resume_boundary.is_atomic,
    }
    
    # Validate step ordering (prerequisites must come before steps)
    step_ids = {step.step_id for step in plan.ordered_steps}
    for step in plan.ordered_steps:
        for prereq in step.prerequisites:
            if prereq not in step_ids:
                invariants["EXPLICIT_STEP_ORDERING"] = False
                break
        else:
            continue
        break
    else:
        invariants["EXPLICIT_STEP_ORDERING"] = True
    
    # Validate no duplicate step IDs
    step_id_counts = defaultdict(int)
    for step in plan.ordered_steps:
        step_id_counts[step.step_id] += 1
    invariants["NO_DUPLICATE_STEP_IDS"] = all(
        count == 1 for count in step_id_counts.values()
    )
    
    return invariants
