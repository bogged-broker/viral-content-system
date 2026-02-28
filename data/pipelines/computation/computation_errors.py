"""
/data/pipelines/computation/computation_errors.py

Fatal Computation Execution Violations — The Criminal Code

AUTHORITY: Defines the only errors allowed to terminate computation execution
PRINCIPLE: Fail loudly, specifically, and irreversibly
BEHAVIOR: No fallbacks, no retries, no partial success, no "best effort"

This file answers:
> "When computation execution correctness is violated, how do we fail loudly,
  specifically, and irreversibly?"

BLUEPRINT INTENT:
This file = "criminal code" for execution-truth violations only.

It defines the minimal, sealed authority for what constitutes a fatal
execution violation. Every error here represents a reality violation
during computation execution.

If these errors are vague:
- Replay debugging becomes impossible
- Correctness failures masquerade as infra issues
- Engineers start adding retries (system death)

CORE PRINCIPLE:
If a computation error is raised, execution MUST NOT continue.

ERROR DESIGN LAWS:
Every error MUST:
1. Represent a semantic correctness violation during execution
2. Be specific and non-overlapping
3. Contain enough context to debug replay
4. Be safe to serialize into audit trails
5. Never be caught silently downstream

ERROR CLASSIFICATION:
1. Registry Violations – computation not allowed to exist/execute
2. Execution Violations – purity or determinism breach during execution
3. Replay Violations – historical drift detected during replay

THESE ARE NOT "EXCEPTIONS" - THEY ARE DECLARATIONS THAT REALITY HAS BEEN VIOLATED.

FORBIDDEN:
- Severity hierarchies (all errors are fatal by definition)
- Definition-time errors (belong in spec validation)
- Schema enforcement errors (belong in contract layer)
- Temporal ordering errors (belong in window authority)
- Catch-all categories (weakens specificity)
- Utility functions (operational policy doesn't belong here)
"""

from __future__ import annotations

from typing import Any, Optional, Dict


# ============================================================================
# BASE ERROR
# ============================================================================

class ComputationError(RuntimeError):
    """
    Fatal, non-recoverable computation execution failure.
    
    Base class for all computation-level execution errors.
    
    Why RuntimeError:
    - Signals execution-time fatality
    - Never suggests caller fault
    - Not a validation nicety
    
    CATCHING RULES:
    These errors may be caught only at:
    - Pipeline boundary
    - Replay supervisor
    - Audit emission layer
    
    They must:
    - Be logged verbatim
    - Include computation hash
    - Include window identity
    - Terminate execution path
    """
    
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
    
    def to_dict(self) -> dict[str, Any]:
        """Serialize error for audit trails."""
        return {
            'error_type': self.__class__.__name__,
            'message': self.message,
        }


# ============================================================================
# REGISTRY VIOLATIONS
# ============================================================================

class UnknownComputationError(ComputationError):
    """
    Raised when a computation hash is not registered.
    
    This is not a data issue. This is an authority breach.
    A computation cannot execute without being registered.
    """
    
    def __init__(self, computation_hash: str):
        self.computation_hash = computation_hash
        super().__init__(f"Computation not registered: {computation_hash}")
    
    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d['computation_hash'] = self.computation_hash
        return d


class ComputationInactiveError(ComputationError):
    """
    Raised when computation exists but is not ACTIVE.
    
    Inactive means:
    - DEPRECATED (superseded by newer version)
    - REVOKED (security or correctness issue)
    - SUPERSEDED (replaced by different computation)
    
    Execution is forbidden.
    """
    
    def __init__(self, computation_hash: str, status: str):
        self.computation_hash = computation_hash
        self.status = status
        super().__init__(f"Computation {computation_hash} is inactive (status={status})")
    
    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d['computation_hash'] = self.computation_hash
        d['status'] = self.status
        return d


class ComputationVersionMismatchError(ComputationError):
    """
    Raised when computation version doesn't match registry.
    
    Used to enforce exact version matching during replay or when
    version drift would compromise correctness.
    """
    
    def __init__(
        self,
        computation_hash: str,
        expected_version: str,
        actual_version: str
    ):
        self.computation_hash = computation_hash
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Computation {computation_hash} version mismatch: "
            f"expected={expected_version}, actual={actual_version}"
        )
    
    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d['computation_hash'] = self.computation_hash
        d['expected_version'] = self.expected_version
        d['actual_version'] = self.actual_version
        return d


# ============================================================================
# EXECUTION VIOLATIONS
# ============================================================================

class InvariantViolationError(ComputationError):
    """
    Raised when global computation invariants are violated during execution.
    
    Examples:
    - Nondeterminism declared false but detected
    - Floating-point usage without rounding spec
    - Undeclared window dependency
    - Required property not satisfied
    
    These are semantic correctness violations that make the
    computation fundamentally invalid.
    """
    
    def __init__(self, invariant: str, details: str, computation_hash: Optional[str] = None):
        self.invariant = invariant
        self.details = details
        self.computation_hash = computation_hash
        
        hash_prefix = f"[{computation_hash}] " if computation_hash else ""
        super().__init__(f"{hash_prefix}Invariant violated: {invariant}. {details}")
    
    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d['invariant'] = self.invariant
        d['details'] = self.details
        if self.computation_hash:
            d['computation_hash'] = self.computation_hash
        return d


# Backward compatibility alias (deprecated)
ComputationInvariantViolation = InvariantViolationError


class InputBindingError(ComputationError):
    """
    Raised when inputs cannot be bound to the computation spec during execution.
    
    Includes:
    - Missing required inputs
    - Extra undeclared inputs
    - Type mismatches
    - Schema drift
    - Incompatible input shapes
    
    This is not recoverable. Computation requires exact input contracts.
    """
    
    def __init__(
        self,
        message: str,
        computation_hash: Optional[str] = None,
        input_name: Optional[str] = None
    ):
        self.computation_hash = computation_hash
        self.input_name = input_name
        
        prefix_parts = []
        if computation_hash:
            prefix_parts.append(f"comp={computation_hash}")
        if input_name:
            prefix_parts.append(f"input={input_name}")
        
        prefix = f"[{', '.join(prefix_parts)}] " if prefix_parts else ""
        super().__init__(f"{prefix}Input binding failed: {message}")
    
    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        if self.computation_hash:
            d['computation_hash'] = self.computation_hash
        if self.input_name:
            d['input_name'] = self.input_name
        return d


class NonDeterministicExecutionError(ComputationError):
    """
    Raised when repeated execution diverges.
    
    This error is existential. If this fires, the computation is
    invalid as a concept.
    
    Detected when:
    - Same inputs produce different outputs
    - Output fingerprint differs across runs
    - Execution order affects results
    
    Requires immediate investigation.
    """
    
    def __init__(
        self,
        computation_hash: str,
        execution_id_1: str,
        execution_id_2: str,
        divergence_details: Optional[str] = None
    ):
        self.computation_hash = computation_hash
        self.execution_id_1 = execution_id_1
        self.execution_id_2 = execution_id_2
        self.divergence_details = divergence_details
        
        details = f": {divergence_details}" if divergence_details else ""
        super().__init__(
            f"Non-deterministic execution detected for {computation_hash} "
            f"(executions {execution_id_1} vs {execution_id_2}){details}"
        )
    
    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d['computation_hash'] = self.computation_hash
        d['execution_id_1'] = self.execution_id_1
        d['execution_id_2'] = self.execution_id_2
        if self.divergence_details:
            d['divergence_details'] = self.divergence_details
        return d


class PureExecutionViolationError(ComputationError):
    """
    Raised when purity is violated during computation execution.
    
    Used for:
    - IO attempts (file, network, database)
    - Time access (datetime.now, time.time)
    - Environment reads (os.environ, sys.argv)
    - Global state mutation
    - Random number generation without seeded RNG
    
    Computations must be pure functions of their inputs.
    """
    
    def __init__(
        self,
        operation: str,
        computation_hash: Optional[str] = None,
        details: Optional[str] = None
    ):
        self.operation = operation
        self.computation_hash = computation_hash
        self.details = details
        
        hash_prefix = f"[{computation_hash}] " if computation_hash else ""
        detail_suffix = f": {details}" if details else ""
        super().__init__(
            f"{hash_prefix}Illegal side effect during computation: {operation}{detail_suffix}"
        )
    
    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d['operation'] = self.operation
        if self.computation_hash:
            d['computation_hash'] = self.computation_hash
        if self.details:
            d['details'] = self.details
        return d


class WindowMismatchError(ComputationError):
    """
    Raised when inputs belong to the wrong window during execution.
    
    Protects time correctness by ensuring all inputs align to the
    declared computation window.
    
    Examples:
    - Input from wrong time period
    - Window boundary violation
    """
    
    def __init__(
        self,
        expected_window: str,
        found_window: str,
        input_name: Optional[str] = None,
        computation_hash: Optional[str] = None
    ):
        self.expected_window = expected_window
        self.found_window = found_window
        self.input_name = input_name
        self.computation_hash = computation_hash
        
        prefix_parts = []
        if computation_hash:
            prefix_parts.append(f"comp={computation_hash}")
        if input_name:
            prefix_parts.append(f"input={input_name}")
        
        prefix = f"[{', '.join(prefix_parts)}] " if prefix_parts else ""
        super().__init__(
            f"{prefix}Window mismatch: expected={expected_window}, found={found_window}"
        )
    
    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d['expected_window'] = self.expected_window
        d['found_window'] = self.found_window
        if self.input_name:
            d['input_name'] = self.input_name
        if self.computation_hash:
            d['computation_hash'] = self.computation_hash
        return d


# ============================================================================
# REPLAY VIOLATIONS
# ============================================================================

class ReplayDriftError(ComputationError):
    """
    Raised when replay output fingerprint differs from historical truth.
    
    This error means:
    - Analytics are no longer trustworthy
    - System must halt or quarantine
    - Investigation is mandatory
    
    This is the worst error that can occur.
    
    Causes:
    - Non-deterministic computation
    - Input data corruption
    - Version skew
    - Platform differences
    - Bug in computation logic
    """
    
    def __init__(
        self,
        computation_hash: str,
        window_identity: str,
        expected_fingerprint: str,
        actual_fingerprint: str,
        execution_id: Optional[str] = None
    ):
        self.computation_hash = computation_hash
        self.window_identity = window_identity
        self.expected_fingerprint = expected_fingerprint
        self.actual_fingerprint = actual_fingerprint
        self.execution_id = execution_id
        
        exec_suffix = f" (exec={execution_id})" if execution_id else ""
        super().__init__(
            f"Replay drift for {computation_hash} in window {window_identity}{exec_suffix}: "
            f"expected_fp={expected_fingerprint}, actual_fp={actual_fingerprint}"
        )
    
    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d['computation_hash'] = self.computation_hash
        d['window_identity'] = self.window_identity
        d['expected_fingerprint'] = self.expected_fingerprint
        d['actual_fingerprint'] = self.actual_fingerprint
        if self.execution_id:
            d['execution_id'] = self.execution_id
        return d


class SpecFingerprintMismatchError(ComputationError):
    """
    Raised when registry record spec_fingerprint doesn't match expected value.
    
    This prevents silent semantic drift from registry mutations.
    Without this check, registry corruption could alter computation semantics
    while preserving hash references, breaking referential transparency.
    """
    
    def __init__(
        self,
        computation_hash: str,
        expected_fingerprint: str,
        actual_fingerprint: str
    ):
        self.computation_hash = computation_hash
        self.expected_fingerprint = expected_fingerprint
        self.actual_fingerprint = actual_fingerprint
        super().__init__(
            f"Spec fingerprint mismatch for {computation_hash}: "
            f"expected={expected_fingerprint}, actual={actual_fingerprint}"
        )
    
    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d['computation_hash'] = self.computation_hash
        d['expected_fingerprint'] = self.expected_fingerprint
        d['actual_fingerprint'] = self.actual_fingerprint
        return d


class ReplayContextMismatchError(ComputationError):
    """
    Raised when replay context doesn't match historical context.
    
    Used to enforce that replay uses identical:
    - Input schemas
    - Invariant versions
    - Computation versions
    - Window definitions
    
    Prevents "accidental" replays with different semantics.
    """
    
    def __init__(
        self,
        computation_hash: str,
        mismatch_field: str,
        expected_value: str,
        actual_value: str
    ):
        self.computation_hash = computation_hash
        self.mismatch_field = mismatch_field
        self.expected_value = expected_value
        self.actual_value = actual_value
        super().__init__(
            f"Replay context mismatch for {computation_hash}: "
            f"{mismatch_field} expected={expected_value}, actual={actual_value}"
        )
    
    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d['computation_hash'] = self.computation_hash
        d['mismatch_field'] = self.mismatch_field
        d['expected_value'] = self.expected_value
        d['actual_value'] = self.actual_value
        return d
