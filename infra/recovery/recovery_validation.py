"""
/infra/recovery/recovery_validation.py

Validation logic for recovery orchestration.

Includes idempotency validation and duplicate effect detection.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional, Tuple, List, Dict

from .recovery_models import (
    CheckpointSnapshot,
    PipelineStateSnapshot,
    PersistenceSnapshot,
    RecoveryConfig,
    RecoveryStage,
    RecoveryPlan,
)


class DuplicateEffectDetector:
    """
    Detects and prevents duplicate effects during recovery.
    
    Prevents:
    - Double window aggregation
    - Re-applying billing charges
    - Duplicate identity routing
    - Re-triggering enforcement
    - Re-emitting external events
    
    Uses:
    - Idempotency keys
    - Deterministic window IDs
    - Transaction version checks
    - Checkpoint monotonicity
    """
    
    @staticmethod
    def detect_duplicate_effects(
        checkpoint_snapshot: CheckpointSnapshot,
        pipeline_snapshot: PipelineStateSnapshot,
        persistence_snapshot: PersistenceSnapshot,
        config: RecoveryConfig,
    ) -> Tuple[bool, Optional[str], Optional[RecoveryStage]]:
        """
        Detect potential duplicate effects.
        
        Returns:
            (has_duplicates, error_message, affected_stage)
        """
        # Check for duplicate idempotency keys across stages
        idempotency_keys_seen: dict[str, list[RecoveryStage]] = defaultdict(list)
        
        for stage, state in pipeline_snapshot.stages.items():
            for key in state.idempotency_keys:
                idempotency_keys_seen[key].append(stage)
        
        # Check for duplicate keys (same key used in multiple stages)
        for key, stages in idempotency_keys_seen.items():
            if len(stages) > 1:
                # Same idempotency key used in multiple stages
                # This could indicate duplicate processing
                return (
                    True,
                    (
                        f"Duplicate idempotency key '{key}' found across stages: "
                        f"{[s.value for s in stages]}"
                    ),
                    stages[0],
                )
        
        # Check checkpoint idempotency keys
        checkpoint_keys_seen: dict[str, list[RecoveryStage]] = defaultdict(list)
        for stage, checkpoint in checkpoint_snapshot.checkpoints.items():
            if checkpoint.idempotency_key:
                checkpoint_keys_seen[checkpoint.idempotency_key].append(stage)
        
        for key, stages in checkpoint_keys_seen.items():
            if len(stages) > 1:
                return (
                    True,
                    (
                        f"Duplicate checkpoint idempotency key '{key}' found across stages: "
                        f"{[s.value for s in stages]}"
                    ),
                    stages[0],
                )
        
        # Check for transaction log position conflicts
        # Multiple checkpoints with same transaction log position
        # indicates potential duplicate persistence
        if config.strict_transaction_validation:
            tx_positions: dict[Optional[int], list[RecoveryStage]] = defaultdict(list)
            for stage, checkpoint in checkpoint_snapshot.checkpoints.items():
                if checkpoint.transaction_log_position is not None:
                    tx_positions[checkpoint.transaction_log_position].append(stage)
            
            for pos, stages in tx_positions.items():
                if len(stages) > 1 and pos is not None:
                    # Multiple stages at same transaction position
                    # Could indicate duplicate persistence
                    return (
                        True,
                        (
                            f"Multiple stages at same transaction log position {pos}: "
                            f"{[s.value for s in stages]}"
                        ),
                        stages[0],
                    )
        
        return False, None, None
    
    @staticmethod
    def validate_idempotency_requirements(
        plan: RecoveryPlan,
        config: RecoveryConfig,
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate that recovery plan has required idempotency protection.
        
        Returns:
            (is_valid, error_message)
        """
        if not config.require_idempotency_keys:
            return True, None
        
        # Check that all steps have idempotency keys
        for step in plan.ordered_steps:
            if not step.idempotency_key:
                return False, f"Step {step.step_id} missing idempotency key"
        
        # Check that plan has idempotency requirements
        if not plan.idempotency_requirements:
            return False, "Recovery plan missing idempotency requirements"
        
        return True, None


class RecoveryValidator:
    """Validation logic for recovery orchestration."""
    
    @staticmethod
    def validate_idempotency_keys(
        checkpoint_snapshot: CheckpointSnapshot,
        pipeline_snapshot: PipelineStateSnapshot,
        config: RecoveryConfig,
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate idempotency keys are present if required.
        
        Returns:
            (is_valid, error_message)
        """
        if not config.require_idempotency_keys:
            return True, None
        
        for stage, state in pipeline_snapshot.stages.items():
            if state.status in ("IN_PROGRESS", "FAILED"):
                if not state.idempotency_keys:
                    return False, (
                        f"Stage {stage.value} is {state.status} but has no idempotency keys"
                    )
                
                # Check checkpoint also has idempotency key
                checkpoint = checkpoint_snapshot.get_checkpoint(stage)
                if checkpoint:
                    if not checkpoint.idempotency_key:
                        return False, (
                            f"Checkpoint for {stage.value} missing idempotency key"
                        )
        
        return True, None
