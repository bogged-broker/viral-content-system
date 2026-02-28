"""
Hard Failure Definitions for Aggregation (Never Silent).

This module defines the only allowed failure modes for the aggregation layer.

It answers one question: "When aggregation cannot proceed without violating
correctness or irreversibility, how does it fail — explicitly, permanently,
and audibly?"

AUTHORITY: Aggregation errors are fatal, explicit, and unrecoverable by default.
"""

from dataclasses import dataclass
from typing import Optional, Dict, Any


@dataclass(frozen=True)
class ErrorContext:
    """
    Immutable context for error reporting.
    
    RULES:
    - Never contains mutable state
    - Serializable for auditing
    - Contains only facts, no interpretations
    """
    aggregation_id: Optional[str] = None
    context_fingerprint: Optional[str] = None
    window_id: Optional[str] = None
    counter_namespace: Optional[str] = None
    reducer_name: Optional[str] = None
    sequence_id: Optional[int] = None
    additional_context: Optional[Dict[str, Any]] = None


class AggregationError(Exception):
    """
    Base class for all aggregation errors.
    
    PROPERTIES (REQUIRED):
    - error_code: Stable string identifier
    - message: Human-readable, invariant-oriented description
    - error_context: Structured context for auditing
    - non_recoverable: Always True (constant)
    
    RULES:
    - Must be raised, never returned
    - Must never be caught inside aggregation
    - Must never be downgraded to warnings
    
    Every aggregation error must correspond to a violated invariant.
    """
    
    error_code: str = "AGG_ERROR"
    non_recoverable: bool = True
    
    def __init__(
        self,
        message: str,
        error_context: Optional[ErrorContext] = None,
    ):
        self.message = message
        self.error_context = error_context or ErrorContext()
        super().__init__(self._format_message())
    
    def _format_message(self) -> str:
        """Format error message with context."""
        parts = [f"[{self.error_code}] {self.message}"]
        
        if self.error_context.aggregation_id:
            parts.append(f"aggregation_id={self.error_context.aggregation_id}")
        if self.error_context.context_fingerprint:
            parts.append(f"context_fingerprint={self.error_context.context_fingerprint}")
        if self.error_context.window_id:
            parts.append(f"window_id={self.error_context.window_id}")
        if self.error_context.counter_namespace:
            parts.append(f"counter_namespace={self.error_context.counter_namespace}")
        if self.error_context.reducer_name:
            parts.append(f"reducer_name={self.error_context.reducer_name}")
        if self.error_context.sequence_id is not None:
            parts.append(f"sequence_id={self.error_context.sequence_id}")
        
        return " | ".join(parts)
    
    def is_recoverable(self) -> bool:
        """Check if error is recoverable (always False)."""
        return False


class AggregationInvariantViolation(AggregationError):
    """
    Raised when a rule in aggregation_invariants.py is violated.
    
    EXAMPLES:
    - Non-monotonic counter update
    - Duplicate aggregation finalization
    - Reducer used outside allowlist
    - Window overlap violation
    
    MEANING: Aggregation correctness is compromised.
    
    This is the most severe error class.
    """
    
    error_code: str = "AGG_INVARIANT_VIOLATION"
    
    def __init__(
        self,
        invariant_name: str,
        violation_details: str,
        error_context: Optional[ErrorContext] = None,
    ):
        self.invariant_name = invariant_name
        self.violation_details = violation_details
        
        message = f"Invariant '{invariant_name}' violated: {violation_details}"
        super().__init__(message, error_context)


class AggregationContextMismatch(AggregationError):
    """
    Raised when aggregation inputs do not exactly match the frozen AggregationContext.
    
    EXAMPLES:
    - Context hash mismatch
    - Counter namespace mismatch
    - Unexpected window definition
    - Aggregation version mismatch
    
    MEANING: Aggregation is about to write results under false authority.
    """
    
    error_code: str = "AGG_CONTEXT_MISMATCH"
    
    def __init__(
        self,
        expected: str,
        actual: str,
        mismatch_field: str,
        error_context: Optional[ErrorContext] = None,
    ):
        self.expected = expected
        self.actual = actual
        self.mismatch_field = mismatch_field
        
        message = (
            f"Context mismatch on field '{mismatch_field}': "
            f"expected={expected}, actual={actual}"
        )
        super().__init__(message, error_context)


class AggregationReplayViolation(AggregationError):
    """
    Raised when replay safety is violated.
    
    EXAMPLES:
    - Attempted re-application of irreversible update
    - Duplicate aggregation sequence ID
    - Non-idempotent reducer usage
    - Window re-entry without explicit replay flag
    
    MEANING: History would fork if allowed.
    """
    
    error_code: str = "AGG_REPLAY_VIOLATION"
    
    def __init__(
        self,
        violation_type: str,
        violation_details: str,
        error_context: Optional[ErrorContext] = None,
    ):
        self.violation_type = violation_type
        self.violation_details = violation_details
        
        message = f"Replay safety violation ({violation_type}): {violation_details}"
        super().__init__(message, error_context)


class AggregationOrderingViolation(AggregationError):
    """
    Raised when event ordering assumptions are broken.
    
    EXAMPLES:
    - Out-of-order window emission
    - Late event past hard cutoff
    - Cross-window mutation attempt
    
    MEANING: Determinism is no longer provable.
    """
    
    error_code: str = "AGG_ORDERING_VIOLATION"
    
    def __init__(
        self,
        ordering_constraint: str,
        violation_details: str,
        error_context: Optional[ErrorContext] = None,
    ):
        self.ordering_constraint = ordering_constraint
        self.violation_details = violation_details
        
        message = (
            f"Ordering constraint '{ordering_constraint}' violated: {violation_details}"
        )
        super().__init__(message, error_context)


class AggregationReducerFailure(AggregationError):
    """
    Raised when a reducer fails in a way aggregation cannot sanitize.
    
    EXAMPLES:
    - ReductionError bubbling from reducers.py
    - Empty reduction where forbidden
    - Type inconsistency across values
    
    MEANING: Math correctness failed, not data quality.
    """
    
    error_code: str = "AGG_REDUCER_FAILURE"
    
    def __init__(
        self,
        reducer_name: str,
        failure_reason: str,
        error_context: Optional[ErrorContext] = None,
    ):
        self.reducer_name = reducer_name
        self.failure_reason = failure_reason
        
        message = f"Reducer '{reducer_name}' failed: {failure_reason}"
        super().__init__(message, error_context)


class AggregationFinalizationError(AggregationError):
    """
    Raised when finalization fails after partial progress.
    
    EXAMPLES:
    - Partial counter write
    - Commit boundary breach
    - Post-finalization mutation attempt
    
    MEANING: Aggregation state may be inconsistent and must halt.
    """
    
    error_code: str = "AGG_FINALIZATION_ERROR"
    
    def __init__(
        self,
        finalization_phase: str,
        failure_details: str,
        error_context: Optional[ErrorContext] = None,
    ):
        self.finalization_phase = finalization_phase
        self.failure_details = failure_details
        
        message = (
            f"Finalization failed during '{finalization_phase}': {failure_details}"
        )
        super().__init__(message, error_context)


class AggregationWindowViolation(AggregationError):
    """
    Raised when window semantics are violated during aggregation.
    
    EXAMPLES:
    - Window identity mismatch
    - Window boundary violation
    - Window overlap in tumbling context
    - Window mutation after closure
    
    MEANING: Window integrity compromised.
    """
    
    error_code: str = "AGG_WINDOW_VIOLATION"
    
    def __init__(
        self,
        window_constraint: str,
        violation_details: str,
        error_context: Optional[ErrorContext] = None,
    ):
        self.window_constraint = window_constraint
        self.violation_details = violation_details
        
        message = f"Window constraint '{window_constraint}' violated: {violation_details}"
        super().__init__(message, error_context)


class AggregationCounterViolation(AggregationError):
    """
    Raised when counter semantics are violated.
    
    EXAMPLES:
    - Counter namespace collision
    - Non-monotonic counter operation
    - Counter overflow
    - Counter accessed without proper context
    
    MEANING: Counter integrity compromised.
    """
    
    error_code: str = "AGG_COUNTER_VIOLATION"
    
    def __init__(
        self,
        counter_constraint: str,
        violation_details: str,
        error_context: Optional[ErrorContext] = None,
    ):
        self.counter_constraint = counter_constraint
        self.violation_details = violation_details
        
        message = f"Counter constraint '{counter_constraint}' violated: {violation_details}"
        super().__init__(message, error_context)


class AggregationVersionMismatch(AggregationError):
    """
    Raised when aggregation version constraints are violated.
    
    EXAMPLES:
    - Schema version incompatibility
    - Reducer version mismatch
    - Context version conflict
    
    MEANING: Version isolation compromised.
    """
    
    error_code: str = "AGG_VERSION_MISMATCH"
    
    def __init__(
        self,
        component: str,
        expected_version: str,
        actual_version: str,
        error_context: Optional[ErrorContext] = None,
    ):
        self.component = component
        self.expected_version = expected_version
        self.actual_version = actual_version
        
        message = (
            f"Version mismatch for '{component}': "
            f"expected={expected_version}, actual={actual_version}"
        )
        super().__init__(message, error_context)


class AggregationStateCorruption(AggregationError):
    """
    Raised when aggregation state is detected to be corrupted.
    
    EXAMPLES:
    - State hash mismatch
    - Checkpoint integrity failure
    - State deserialization failure
    - Unexpected null state
    
    MEANING: State cannot be trusted, aggregation must halt.
    """
    
    error_code: str = "AGG_STATE_CORRUPTION"
    
    def __init__(
        self,
        corruption_type: str,
        corruption_details: str,
        error_context: Optional[ErrorContext] = None,
    ):
        self.corruption_type = corruption_type
        self.corruption_details = corruption_details
        
        message = f"State corruption detected ({corruption_type}): {corruption_details}"
        super().__init__(message, error_context)


def create_error_context(
    aggregation_id: Optional[str] = None,
    context_fingerprint: Optional[str] = None,
    window_id: Optional[str] = None,
    counter_namespace: Optional[str] = None,
    reducer_name: Optional[str] = None,
    sequence_id: Optional[int] = None,
    **additional_context: Any,
) -> ErrorContext:
    """
    Create error context for aggregation errors.
    
    Args:
        aggregation_id: Unique aggregation identifier
        context_fingerprint: Context hash for mismatch detection
        window_id: Window identifier
        counter_namespace: Counter namespace
        reducer_name: Reducer name
        sequence_id: Sequence identifier
        **additional_context: Additional context fields
    
    Returns:
        Immutable ErrorContext
    """
    return ErrorContext(
        aggregation_id=aggregation_id,
        context_fingerprint=context_fingerprint,
        window_id=window_id,
        counter_namespace=counter_namespace,
        reducer_name=reducer_name,
        sequence_id=sequence_id,
        additional_context=additional_context if additional_context else None,
    )


AGGREGATION_ERROR_CODES = {
    "AGG_ERROR": AggregationError,
    "AGG_INVARIANT_VIOLATION": AggregationInvariantViolation,
    "AGG_CONTEXT_MISMATCH": AggregationContextMismatch,
    "AGG_REPLAY_VIOLATION": AggregationReplayViolation,
    "AGG_ORDERING_VIOLATION": AggregationOrderingViolation,
    "AGG_REDUCER_FAILURE": AggregationReducerFailure,
    "AGG_FINALIZATION_ERROR": AggregationFinalizationError,
    "AGG_WINDOW_VIOLATION": AggregationWindowViolation,
    "AGG_COUNTER_VIOLATION": AggregationCounterViolation,
    "AGG_VERSION_MISMATCH": AggregationVersionMismatch,
    "AGG_STATE_CORRUPTION": AggregationStateCorruption,
}


def is_aggregation_error(exception: Exception) -> bool:
    """
    Check if exception is an aggregation error.
    
    Args:
        exception: Exception to check
    
    Returns:
        True if exception is AggregationError subclass
    """
    return isinstance(exception, AggregationError)


def get_error_code(exception: Exception) -> Optional[str]:
    """
    Extract error code from exception.
    
    Args:
        exception: Exception to extract from
    
    Returns:
        Error code if aggregation error, None otherwise
    """
    if isinstance(exception, AggregationError):
        return exception.error_code
    return None