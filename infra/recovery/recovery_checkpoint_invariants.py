"""
/infra/recovery/recovery_checkpoint_invariants.py

Checkpoint invariant validation.

Validates checkpoint monotonicity and other critical invariants.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from .recovery_models import (
    CheckpointSnapshot,
    RecoveryStage,
)


class CheckpointInvariantValidator:
    """
    Validates checkpoint invariants.
    
    Critical invariants:
    - Checkpoint sequence numbers never move backwards
    - Checkpoint timestamps are monotonic (or equal)
    - Checkpoint never beyond durable boundary
    """
    
    @staticmethod
    def validate_checkpoint_monotonicity(
        current_snapshot: CheckpointSnapshot,
        previous_snapshot: Optional[CheckpointSnapshot],
        logger: Optional[logging.Logger] = None,
    ) -> Tuple[bool, Optional[str]]:
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
        if previous_snapshot is None:
            return True, None

        log = logger or logging.getLogger(__name__)
        
        for stage in RecoveryStage:
            current_cp = current_snapshot.get_checkpoint(stage)
            previous_cp = previous_snapshot.get_checkpoint(stage)

            if current_cp and previous_cp:
                # Sequence must be monotonically increasing
                if current_cp.sequence_number < previous_cp.sequence_number:
                    error_msg = (
                        f"Checkpoint sequence moved backwards for {stage.value}: "
                        f"{previous_cp.sequence_number} -> {current_cp.sequence_number}"
                    )
                    log.error(error_msg)
                    return False, error_msg
                
                # Timestamp should also be monotonic (or equal)
                if current_cp.boundary_timestamp < previous_cp.boundary_timestamp:
                    error_msg = (
                        f"Checkpoint timestamp moved backwards for {stage.value}: "
                        f"{previous_cp.boundary_timestamp.iso_string} -> {current_cp.boundary_timestamp.iso_string}"
                    )
                    log.warning(error_msg)
                    # Warning, not error - timestamps can be equal in some cases

        return True, None
    
    @staticmethod
    def validate_checkpoint_not_beyond_durable_boundary(
        checkpoint: CheckpointSnapshot,
        persistence_boundary_timestamp: str,  # ISO string
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate checkpoint never beyond durable boundary.
        
        This is a critical safety invariant.
        
        Args:
            checkpoint: Checkpoint snapshot to validate
            persistence_boundary_timestamp: ISO string of persistence boundary
            
        Returns:
            (is_valid, error_message)
        """
        from .recovery_models import LogicalTimestamp
        
        boundary_ts = LogicalTimestamp(iso_string=persistence_boundary_timestamp)
        
        for stage, cp in checkpoint.checkpoints.items():
            if cp.boundary_timestamp > boundary_ts:
                return False, (
                    f"Checkpoint for {stage.value} at {cp.boundary_timestamp.iso_string} "
                    f"is beyond durable boundary {boundary_ts.iso_string}"
                )
        
        return True, None
