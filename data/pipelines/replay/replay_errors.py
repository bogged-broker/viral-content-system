"""
Fatal replay violations only.

This module defines the closed set of fatal replay exception types.
These errors represent structural or constitutional violations of replay.
If any of these are raised, replay is irrevocably invalid.

This is the legal codebook of replay failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Any, Tuple, List, Dict


# ============================================================================
# ERROR CATEGORIES - Maps 1-to-1 with Replay Invariants
# ============================================================================

class ReplayPhase(Enum):
    """Phase of replay where error occurred."""
    PLANNING = "planning"
    INITIALIZATION = "initialization"
    EXECUTION = "execution"
    COMPARISON = "comparison"
    FINALIZATION = "finalization"


class InvariantCategory(Enum):
    """Category of invariant that was violated."""
    IDENTITY = "identity"
    ENVIRONMENT = "environment"
    SCOPE = "scope"
    EXECUTION = "execution"
    OUTPUT = "output"


# ============================================================================
# BASE ERROR - All Replay Errors Inherit From This
# ============================================================================

@dataclass(frozen=True)
class ReplayError(Exception):
    """
    Base class for all fatal replay errors.
    
    Contract:
    - Replay errors are always fatal
    - Never caught and ignored
    - Never downgraded or retried
    - If raised, replay stops
    
    All errors must be:
    - Immutable after creation
    - Serialization-safe
    - Deterministically comparable
    - Context-complete
    """
    invariant_id: str
    phase: ReplayPhase
    component_id: str
    message: str
    expected_value: Optional[str] = None
    observed_value: Optional[str] = None
    
    def __str__(self) -> str:
        """Deterministic string representation."""
        parts = [
            f"[{self.phase.value.upper()}]",
            f"Invariant '{self.invariant_id}' violated",
            f"in component '{self.component_id}':",
            self.message
        ]
        
        if self.expected_value is not None or self.observed_value is not None:
            parts.append(
                f"Expected: {self.expected_value or 'N/A'}, "
                f"Observed: {self.observed_value or 'N/A'}"
            )
        
        return " ".join(parts)
    
    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for audit logs."""
        return {
            "error_type": self.__class__.__name__,
            "invariant_id": self.invariant_id,
            "phase": self.phase.value,
            "component_id": self.component_id,
            "message": self.message,
            "expected_value": self.expected_value,
            "observed_value": self.observed_value,
        }
    
    def get_category(self) -> InvariantCategory:
        """Get invariant category for this error type."""
        raise NotImplementedError("Subclasses must implement get_category()")


# ============================================================================
# 1. IDENTITY ERRORS - Replay Identity Cannot Be Proven Identical
# ============================================================================

@dataclass(frozen=True)
class ReplayPlanHashMismatch(ReplayError):
    """
    ReplayPlan hash does not match expected value.
    
    If identity is ambiguous, replay must abort.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.IDENTITY


@dataclass(frozen=True)
class ComputationIdentityDivergence(ReplayError):
    """
    Computation node identity differs from original execution.
    
    Computation identity must be bit-identical for replay validity.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.IDENTITY


@dataclass(frozen=True)
class PipelineVersionMismatch(ReplayError):
    """
    Pipeline version differs between original and replay execution.
    
    Version drift invalidates replay assumptions.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.IDENTITY


@dataclass(frozen=True)
class LineageHashCorruption(ReplayError):
    """
    Lineage hash integrity check failed.
    
    Corrupted lineage means replay cannot establish provenance.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.IDENTITY


@dataclass(frozen=True)
class ExecutionContextHashMismatch(ReplayError):
    """
    Execution context hash differs from declared context.
    
    Context divergence invalidates replay claims.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.IDENTITY


# ============================================================================
# 2. ENVIRONMENT ERRORS - Unsealed Environment Detected
# ============================================================================

@dataclass(frozen=True)
class WallClockAccessDetected(ReplayError):
    """
    Code accessed wall-clock time during replay.
    
    Environment drift equals replay corruption.
    Wall-clock access breaks determinism.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.ENVIRONMENT


@dataclass(frozen=True)
class UndeclaredEnvironmentVariable(ReplayError):
    """
    Environment variable accessed that was not declared in replay plan.
    
    Undeclared variables invalidate environment sealing.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.ENVIRONMENT


@dataclass(frozen=True)
class RandomnessSourceNotFrozen(ReplayError):
    """
    Non-deterministic randomness source detected.
    
    All randomness must be seeded identically for replay.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.ENVIRONMENT


@dataclass(frozen=True)
class DependencyGraphMismatch(ReplayError):
    """
    Dependency graph differs from original execution.
    
    Dependency changes invalidate replay environment.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.ENVIRONMENT


@dataclass(frozen=True)
class ExternalIOAttempted(ReplayError):
    """
    Replay code attempted external I/O not declared in plan.
    
    Undeclared I/O breaks environment seal.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.ENVIRONMENT


@dataclass(frozen=True)
class ConcurrencyModelUndeclared(ReplayError):
    """Concurrency model not declared before execution."""
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.ENVIRONMENT


@dataclass(frozen=True)
class GlobalStateMutationDetected(ReplayError):
    """Mutable global state detected during replay."""
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.ENVIRONMENT


# ============================================================================
# 3. SCOPE ERRORS - Replay Scope Differs from Declared Scope
# ============================================================================

@dataclass(frozen=True)
class UndeclaredEntityEncountered(ReplayError):
    """
    Entity encountered that was not in declared replay scope.
    
    Scope mutation voids replay validity.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.SCOPE


@dataclass(frozen=True)
class LazyWindowExpansionAttempted(ReplayError):
    """
    Replay attempted to expand window scope beyond declared boundaries.
    
    Dynamic window expansion violates scope contract.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.SCOPE


@dataclass(frozen=True)
class RuntimeDiscoveryTriggered(ReplayError):
    """
    Code triggered runtime entity discovery during replay.
    
    All entities must be known upfront, no runtime discovery.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.SCOPE


@dataclass(frozen=True)
class ComputationNodeMissing(ReplayError):
    """
    Expected computation node is missing from replay execution.
    
    Scope must match exactly; missing nodes invalidate replay.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.SCOPE


@dataclass(frozen=True)
class ComputationNodeExtraneous(ReplayError):
    """
    Extra computation node found that was not in original execution.
    
    Additional nodes indicate scope divergence.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.SCOPE


@dataclass(frozen=True)
class EntityCountMismatch(ReplayError):
    """
    Number of entities differs from declared scope.
    
    Cardinality changes violate scope contract.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.SCOPE


@dataclass(frozen=True)
class PipelineStagesNotFixed(ReplayError):
    """Pipeline stages not fixed before execution."""
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.SCOPE


# ============================================================================
# 4. EXECUTION ERRORS - Replay Semantics Deviate
# ============================================================================

@dataclass(frozen=True)
class NonDeterministicOrderingDetected(ReplayError):
    """
    Execution produced non-deterministic ordering.
    
    If execution differs, replay is false.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.EXECUTION


@dataclass(frozen=True)
class NonCanonicalOrderingDetected(ReplayError):
    """Ordering is not canonical and stable."""
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.EXECUTION


@dataclass(frozen=True)
class ExecutionGraphMismatch(ReplayError):
    """
    Execution graph topology differs from original.
    
    Graph structure must be identical for replay validity.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.EXECUTION


@dataclass(frozen=True)
class DivergentFailurePoint(ReplayError):
    """
    Replay failed at different point than original execution.
    
    Failure point divergence indicates semantic difference.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.EXECUTION


@dataclass(frozen=True)
class SchedulerDependentBehavior(ReplayError):
    """
    Execution behavior depends on scheduler decisions.
    
    Scheduler dependency breaks determinism contract.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.EXECUTION


@dataclass(frozen=True)
class ExecutionPathDivergence(ReplayError):
    """
    Code execution path differs from original run.
    
    Path divergence indicates semantic difference.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.EXECUTION


@dataclass(frozen=True)
class StateTransitionMismatch(ReplayError):
    """
    State machine transition differs from original execution.
    
    State transitions must be deterministically identical.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.EXECUTION


# ============================================================================
# 5. OUTPUT ERRORS - Outputs Cannot Be Reconciled
# ============================================================================

@dataclass(frozen=True)
class OutputSchemaMismatch(ReplayError):
    """
    Output schema differs from expected schema.
    
    Unexplained output difference is failure.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.OUTPUT


@dataclass(frozen=True)
class OutputHashMismatch(ReplayError):
    """
    Output content hash differs without declared divergence authorization.
    
    Hash mismatch without authorization is fatal.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.OUTPUT


@dataclass(frozen=True)
class SilentNormalizationAttempted(ReplayError):
    """
    Code attempted output normalization not declared in plan.
    
    All normalization must be explicit and declared.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.OUTPUT


@dataclass(frozen=True)
class UnauthorizedCoercion(ReplayError):
    """
    Type coercion occurred without explicit authorization.
    
    Coercion without declaration indicates semantic drift.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.OUTPUT


@dataclass(frozen=True)
class OutputCardinalityMismatch(ReplayError):
    """
    Number of output records differs from original execution.
    
    Row count differences must be explained or are fatal.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.OUTPUT


@dataclass(frozen=True)
class OutputOrderingDivergence(ReplayError):
    """
    Output ordering differs from original execution.
    
    Ordering changes must be authorized or are fatal.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.OUTPUT


@dataclass(frozen=True)
class MissingOutputArtifact(ReplayError):
    """
    Expected output artifact not produced by replay.
    
    Missing outputs invalidate comparison.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.OUTPUT


@dataclass(frozen=True)
class ExtraneoousOutputArtifact(ReplayError):
    """
    Replay produced output artifact not in original execution.
    
    Extra outputs indicate divergent behavior.
    """
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.OUTPUT


@dataclass(frozen=True)
class DivergenceUndeclared(ReplayError):
    """Output divergence detected but not declared."""
    
    def get_category(self) -> InvariantCategory:
        return InvariantCategory.OUTPUT


# ============================================================================
# ERROR UTILITIES
# ============================================================================

def get_all_error_types() -> list[type[ReplayError]]:
    """
    Get all defined replay error types.
    
    Returns list of error classes for validation and testing.
    """
    return [
        # Identity errors
        ReplayPlanHashMismatch,
        ComputationIdentityDivergence,
        PipelineVersionMismatch,
        LineageHashCorruption,
        ExecutionContextHashMismatch,
        # Environment errors
        WallClockAccessDetected,
        UndeclaredEnvironmentVariable,
        RandomnessSourceNotFrozen,
        DependencyGraphMismatch,
        ExternalIOAttempted,
        ConcurrencyModelUndeclared,
        GlobalStateMutationDetected,
        # Scope errors
        UndeclaredEntityEncountered,
        LazyWindowExpansionAttempted,
        RuntimeDiscoveryTriggered,
        ComputationNodeMissing,
        ComputationNodeExtraneous,
        EntityCountMismatch,
        PipelineStagesNotFixed,
        # Execution errors
        NonDeterministicOrderingDetected,
        NonCanonicalOrderingDetected,
        ExecutionGraphMismatch,
        DivergentFailurePoint,
        SchedulerDependentBehavior,
        ExecutionPathDivergence,
        StateTransitionMismatch,
        # Output errors
        OutputSchemaMismatch,
        OutputHashMismatch,
        SilentNormalizationAttempted,
        UnauthorizedCoercion,
        OutputCardinalityMismatch,
        OutputOrderingDivergence,
        MissingOutputArtifact,
        ExtraneoousOutputArtifact,
        DivergenceUndeclared,
    ]


def get_errors_by_category(category: InvariantCategory) -> list[type[ReplayError]]:
    """Get all error types for a specific invariant category."""
    return [
        error_type for error_type in get_all_error_types()
        if error_type(
            invariant_id="",
            phase=ReplayPhase.EXECUTION,
            component_id="",
            message=""
        ).get_category() == category
    ]


def validate_error_coverage() -> tuple[bool, list[str]]:
    """
    Validate that all invariant categories have error coverage.
    
    Returns:
        (is_complete, list_of_missing_categories)
    """
    all_errors = get_all_error_types()
    covered_categories = {
        error_type(
            invariant_id="",
            phase=ReplayPhase.EXECUTION,
            component_id="",
            message=""
        ).get_category()
        for error_type in all_errors
    }
    
    missing = set(InvariantCategory) - covered_categories
    return (len(missing) == 0, [cat.value for cat in missing])


# ============================================================================
# MODULE INTERFACE
# ============================================================================

__all__ = [
    # Base
    'ReplayError',
    # Enums
    'ReplayPhase',
    'InvariantCategory',
    # Identity errors
    'ReplayPlanHashMismatch',
    'ComputationIdentityDivergence',
    'PipelineVersionMismatch',
    'LineageHashCorruption',
    'ExecutionContextHashMismatch',
    # Environment errors
    'WallClockAccessDetected',
    'UndeclaredEnvironmentVariable',
    'RandomnessSourceNotFrozen',
    'DependencyGraphMismatch',
    'ExternalIOAttempted',
    'ConcurrencyModelUndeclared',
    'GlobalStateMutationDetected',
    # Scope errors
    'UndeclaredEntityEncountered',
    'LazyWindowExpansionAttempted',
    'RuntimeDiscoveryTriggered',
    'ComputationNodeMissing',
    'ComputationNodeExtraneous',
    'EntityCountMismatch',
    'PipelineStagesNotFixed',
    # Execution errors
    'NonDeterministicOrderingDetected',
    'NonCanonicalOrderingDetected',
    'ExecutionGraphMismatch',
    'DivergentFailurePoint',
    'SchedulerDependentBehavior',
    'ExecutionPathDivergence',
    'StateTransitionMismatch',
    # Output errors
    'OutputSchemaMismatch',
    'OutputHashMismatch',
    'SilentNormalizationAttempted',
    'UnauthorizedCoercion',
    'OutputCardinalityMismatch',
    'OutputOrderingDivergence',
    'MissingOutputArtifact',
    'ExtraneoousOutputArtifact',
    # Utilities
    'get_all_error_types',
    'get_errors_by_category',
    'validate_error_coverage',
]