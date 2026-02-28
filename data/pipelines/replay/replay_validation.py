"""
Replay context validation and convenience utilities.

This module provides independent validation of replay contexts and
convenience factory functions for common context creation patterns.

These utilities are separate from the core context definition to maintain
strict authority boundary discipline.
"""

from __future__ import annotations

from typing import List, Tuple
from replay_context import (
    ReplayContext,
    AuditArtifact,
    TimeRange,
    DeterminismLock,
    ReplayMode,
    ReplayEnforcement,
    ReplayContextBuilder,
)


# ============================================================================
# CONTEXT VALIDATOR - Independent Verification
# ============================================================================

class ReplayContextValidator:
    """
    Independent validation of replay context integrity.
    
    Checks:
    - Context hash integrity
    - Audit artifact completeness
    - Time range bounds
    - Mode/enforcement compatibility
    - Determinism lock completeness
    - Determinism lock cross-validation against audit artifact
    """
    
    @staticmethod
    def validate_context(context: ReplayContext) -> tuple[bool, List[str]]:
        """
        Validate context integrity and completeness.
        
        Returns:
            (is_valid, list_of_issues)
        """
        issues = []
        
        # Verify hash integrity
        if not context.verify_integrity():
            issues.append("Context hash does not match content")
        
        # Verify audit artifact
        if not context.audit_artifact.verify_integrity():
            issues.append("Audit artifact failed integrity verification")
        
        # Verify time range bounds
        if not context.target_time_range.is_subset_of(
            context.audit_artifact.recorded_time_range
        ):
            issues.append("Target time range exceeds audit bounds")
        
        # Verify mode/enforcement compatibility
        if context.execution_mode == ReplayMode.PROVE_REPRODUCIBILITY:
            if context.enforcement_level != ReplayEnforcement.STRICT:
                issues.append("PROVE_REPRODUCIBILITY requires STRICT enforcement")
        
        # Verify determinism lock completeness
        if not context.determinism_lock.pipeline_version:
            issues.append("Determinism lock missing pipeline_version")
        if not context.determinism_lock.code_hash:
            issues.append("Determinism lock missing code_hash")
        
        # Verify determinism lock matches audit artifact
        if context.determinism_lock.pipeline_version != context.audit_artifact.pipeline_version:
            issues.append(
                f"Determinism lock pipeline_version mismatch: "
                f"lock={context.determinism_lock.pipeline_version} "
                f"audit={context.audit_artifact.pipeline_version}"
            )
        
        if context.determinism_lock.computation_registry_version != context.audit_artifact.computation_registry_version:
            issues.append(
                f"Determinism lock computation_registry_version mismatch: "
                f"lock={context.determinism_lock.computation_registry_version} "
                f"audit={context.audit_artifact.computation_registry_version}"
            )
        
        if context.determinism_lock.window_model_versions != context.audit_artifact.window_model_versions:
            issues.append(
                f"Determinism lock window_model_versions mismatch: "
                f"lock={context.determinism_lock.window_model_versions} "
                f"audit={context.audit_artifact.window_model_versions}"
            )
        
        if context.determinism_lock.schema_versions != context.audit_artifact.schema_versions:
            issues.append(
                f"Determinism lock schema_versions mismatch: "
                f"lock={context.determinism_lock.schema_versions} "
                f"audit={context.audit_artifact.schema_versions}"
            )
        
        if context.determinism_lock.code_hash != context.audit_artifact.code_hash:
            issues.append(
                f"Determinism lock code_hash mismatch: "
                f"lock={context.determinism_lock.code_hash} "
                f"audit={context.audit_artifact.code_hash}"
            )
        
        if context.determinism_lock.environment_fingerprint != context.audit_artifact.environment_fingerprint:
            issues.append(
                f"Determinism lock environment_fingerprint mismatch: "
                f"lock={context.determinism_lock.environment_fingerprint} "
                f"audit={context.audit_artifact.environment_fingerprint}"
            )
        
        return (len(issues) == 0, issues)
    
    @staticmethod
    def validate_against_current_environment(
        context: ReplayContext,
        current_lock: DeterminismLock
    ) -> tuple[bool, List[str]]:
        """
        Validate that context is compatible with current environment.
        
        Returns:
            (is_compatible, list_of_incompatibilities)
        """
        issues = []
        
        if not context.is_compatible_with_lock(current_lock):
            # Find specific incompatibilities
            if context.determinism_lock.pipeline_version != current_lock.pipeline_version:
                issues.append(
                    f"Pipeline version mismatch: "
                    f"context={context.determinism_lock.pipeline_version} "
                    f"current={current_lock.pipeline_version}"
                )
            
            if context.determinism_lock.code_hash != current_lock.code_hash:
                issues.append(
                    f"Code hash mismatch: "
                    f"context={context.determinism_lock.code_hash} "
                    f"current={current_lock.code_hash}"
                )
            
            if context.determinism_lock.environment_fingerprint != current_lock.environment_fingerprint:
                issues.append(
                    f"Environment fingerprint mismatch: "
                    f"context={context.determinism_lock.environment_fingerprint} "
                    f"current={current_lock.environment_fingerprint}"
                )
        
        return (len(issues) == 0, issues)


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def create_strict_verification_context(
    context_id: str,
    audit_artifact: AuditArtifact,
    time_range: TimeRange,
    determinism_lock: DeterminismLock
) -> ReplayContext:
    """Create context for strict bit-for-bit verification."""
    return (
        ReplayContextBuilder(context_id)
        .set_audit_artifact(audit_artifact)
        .set_target_time_range(time_range)
        .set_execution_mode(ReplayMode.PROVE_REPRODUCIBILITY)
        .set_enforcement_level(ReplayEnforcement.STRICT)
        .set_determinism_lock(determinism_lock)
        .build()
    )


def create_diagnostic_context(
    context_id: str,
    audit_artifact: AuditArtifact,
    time_range: TimeRange,
    determinism_lock: DeterminismLock
) -> ReplayContext:
    """Create context for diagnostic divergence analysis."""
    return (
        ReplayContextBuilder(context_id)
        .set_audit_artifact(audit_artifact)
        .set_target_time_range(time_range)
        .set_execution_mode(ReplayMode.DIAGNOSE)
        .set_enforcement_level(ReplayEnforcement.EVIDENTIARY)
        .set_determinism_lock(determinism_lock)
        .build()
    )


__all__ = [
    'ReplayContextValidator',
    'create_strict_verification_context',
    'create_diagnostic_context',
]
