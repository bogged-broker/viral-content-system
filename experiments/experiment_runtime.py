"""
/experiments/experiment_runtime.py

Deterministic Experiment Execution & Exposure Authority
(No Assignment Drift, No Implicit Mutation, No Hidden State)

This module is the single authority responsible for executing experiments at runtime.

CRITICAL PRINCIPLES:
- Deterministic assignment (same identity + config → same variant)
- Stable bucketing (hash-based, reproducible)
- Exposure integrity (idempotent, replay-safe)
- Isolation between experiments (no conflicts)
- Policy enforcement at decision time
- No assignment drift
- No implicit mutation
- No hidden state

ABSOLUTE INVARIANTS:
1. Same identity + config → same variant
2. No assignment without ACTIVE state
3. No double exposure emission
4. No silent fallback to control
5. No cross-experiment interference
6. No runtime mutation of config
7. No implicit randomization
8. No eligibility drift mid-flight
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Optional, Any, Dict, List, Tuple, Set
from enum import Enum
from datetime import datetime, timezone
from types import MappingProxyType


# ============================================================================
# EXPERIMENT STATE DEFINITIONS
# ============================================================================

class ExperimentState(Enum):
    """Experiment lifecycle states"""
    DRAFT = "draft"  # Not executable
    ACTIVE = "active"  # Eligible for assignment
    PAUSED = "paused"  # No new assignments
    STOPPING = "stopping"  # Freeze exposures
    COMPLETED = "completed"  # No new exposures
    TERMINATED = "terminated"  # Inactive


class EligibilityStatus(Enum):
    """Assignment eligibility outcomes"""
    ELIGIBLE = "eligible"
    EXCLUDED_BY_PREDICATE = "excluded_by_predicate"
    EXCLUDED_BY_STATE = "excluded_by_state"
    EXCLUDED_BY_TIMING = "excluded_by_timing"
    BLOCKED_BY_ISOLATION = "blocked_by_isolation"
    OVERRIDDEN = "overridden"


# ============================================================================
# IMMUTABLE CONTEXT STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class IdentityContext:
    """Immutable identity context for assignment"""
    canonical_identity_id: str  # Primary stable identity
    identity_type: str  # e.g., "user_id", "device_id", "session_id"
    unified_id: Optional[str] = None  # Cross-device unified ID
    
    def __post_init__(self):
        if not self.canonical_identity_id:
            raise ValueError("canonical_identity_id cannot be empty")
        if not self.identity_type:
            raise ValueError("identity_type cannot be empty")


@dataclass(frozen=True)
class RequestContext:
    """Immutable request context for eligibility evaluation"""
    timestamp: int  # Unix timestamp (seconds)
    attributes: dict[str, Any] = field(default_factory=dict)
    
    def get_fingerprint(self) -> str:
        """Generate deterministic fingerprint of request context"""
        canonical = json.dumps({
            'timestamp': self.timestamp,
            'attributes': dict(sorted(self.attributes.items()))
        }, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]


@dataclass(frozen=True)
class RuntimeFlags:
    """Runtime behavior flags"""
    allow_overrides: bool = False
    enable_idempotency_check: bool = True
    enable_isolation_check: bool = True


# ============================================================================
# EXPERIMENT CONFIGURATION SNAPSHOT
# ============================================================================

@dataclass(frozen=True)
class AllocationRange:
    """Bucket range allocation (must match variant_generator.py)"""
    start_bucket: int
    end_bucket: int  # Inclusive
    variant_id: str
    
    def contains(self, bucket: int) -> bool:
        return self.start_bucket <= bucket <= self.end_bucket


@dataclass(frozen=True)
class EligibilityPredicate:
    """Deterministic eligibility predicate"""
    predicate_type: str  # e.g., "attribute_match", "time_window"
    parameters: dict[str, Any]
    
    def evaluate(self, identity: IdentityContext, context: RequestContext) -> bool:
        """
        Evaluate predicate deterministically
        
        Must be:
        - Deterministic
        - Side-effect free
        - Identity-consistent
        - Time-bound stable
        """
        if self.predicate_type == "always_true":
            return True
        
        elif self.predicate_type == "attribute_match":
            # Match request context attribute
            attr_name = self.parameters.get("attribute")
            expected_value = self.parameters.get("value")
            actual_value = context.attributes.get(attr_name)
            return actual_value == expected_value
        
        elif self.predicate_type == "time_window":
            # Within time window
            start_time = self.parameters.get("start_timestamp", 0)
            end_time = self.parameters.get("end_timestamp", float('inf'))
            return start_time <= context.timestamp <= end_time
        
        elif self.predicate_type == "identity_type_match":
            # Match identity type
            expected_type = self.parameters.get("identity_type")
            return identity.identity_type == expected_type
        
        else:
            # Unknown predicate type - fail safe (exclude)
            return False


@dataclass(frozen=True)
class OverrideRule:
    """Explicit override rule"""
    identity_id: str
    variant_id: str
    reason: str
    created_at: int


@dataclass(frozen=True)
class ExperimentConfigSnapshot:
    """Immutable experiment configuration snapshot"""
    experiment_id: str
    version: int
    state: ExperimentState
    hash_seed: str  # Deterministic seed for bucketing
    bucket_domain_size: int
    allocation_ranges: tuple[AllocationRange, ...]
    variant_metadata: dict[str, dict[str, Any]]  # variant_id -> metadata
    eligibility_predicate: EligibilityPredicate
    start_timestamp: int
    freeze_timestamp: Optional[int]  # No new enrollments after this
    config_hash: str  # Hash of entire config
    override_rules: tuple[OverrideRule, ...] = field(default_factory=tuple)
    isolation_groups: tuple[str, ...] = field(default_factory=tuple)  # Mutually exclusive experiments
    
    def __post_init__(self):
        if not self.experiment_id:
            raise ValueError("experiment_id cannot be empty")
        if self.version < 1:
            raise ValueError(f"version must be >= 1, got {self.version}")
        if self.bucket_domain_size < 100:
            raise ValueError(f"bucket_domain_size must be >= 100, got {self.bucket_domain_size}")
        if not self.allocation_ranges:
            raise ValueError("allocation_ranges cannot be empty")


# ============================================================================
# ASSIGNMENT RESULT
# ============================================================================

@dataclass(frozen=True)
class AssignmentResult:
    """Immutable assignment result"""
    experiment_id: str
    variant_id: Optional[str]  # None if excluded
    eligibility_status: EligibilityStatus
    exclusion_reason: Optional[str]
    assignment_bucket: Optional[int]  # None if excluded
    exposure_emitted: bool
    config_version: int
    assignment_hash: str  # Hash of assignment for verification
    logical_timestamp: int
    override_applied: bool = False
    
    def is_assigned(self) -> bool:
        """Check if user was assigned to a variant"""
        return self.variant_id is not None and self.eligibility_status == EligibilityStatus.ELIGIBLE


# ============================================================================
# EXPOSURE EVENT
# ============================================================================

@dataclass(frozen=True)
class ExposureEvent:
    """Immutable exposure event for logging"""
    experiment_id: str
    variant_id: str
    identity_id: str
    assignment_bucket: int
    assignment_seed_version: str
    config_version: int
    logical_timestamp: int
    request_context_fingerprint: str
    idempotency_key: str  # For deduplication
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            'experiment_id': self.experiment_id,
            'variant_id': self.variant_id,
            'identity_id': self.identity_id,
            'assignment_bucket': self.assignment_bucket,
            'assignment_seed_version': self.assignment_seed_version,
            'config_version': self.config_version,
            'logical_timestamp': self.logical_timestamp,
            'request_context_fingerprint': self.request_context_fingerprint,
            'idempotency_key': self.idempotency_key,
        }


# ============================================================================
# RUNTIME EXCEPTIONS
# ============================================================================

class ExperimentRuntimeError(Exception):
    """Base exception for runtime errors"""
    pass


class InvalidConfigVersionError(ExperimentRuntimeError):
    """Config version mismatch or invalid"""
    pass


class InvalidExperimentStateError(ExperimentRuntimeError):
    """Experiment not in executable state"""
    pass


class InvalidAllocationRangeError(ExperimentRuntimeError):
    """Allocation range invalid or malformed"""
    pass


class IdentityContextMissingError(ExperimentRuntimeError):
    """Required identity context missing"""
    pass


# ============================================================================
# IDEMPOTENCY MANAGER (INTERFACE)
# ============================================================================

class IdempotencyManager:
    """
    Interface for idempotency tracking
    
    Implementations must provide atomic write guarantees to prevent
    double exposure emission.
    """
    
    def check_and_record_exposure(
        self,
        idempotency_key: str,
        exposure_event: ExposureEvent
    ) -> bool:
        """
        Check if exposure already emitted and record if not
        
        Returns:
            True if this is first exposure (should emit)
            False if already emitted (skip)
        
        Must be atomic and thread-safe.
        """
        raise NotImplementedError("Subclass must implement")


class InMemoryIdempotencyManager(IdempotencyManager):
    """
    In-memory idempotency manager for testing.
    
    WARNING: Not production-safe (lost on restart).
    Use persistent storage in production with atomic write guarantees.
    """
    
    def __init__(self):
        self._emitted_keys: Set[str] = set()
        self._lock = threading.Lock()  # Thread-safe
    
    def check_and_record_exposure(
        self,
        idempotency_key: str,
        exposure_event: ExposureEvent
    ) -> bool:
        """
        Thread-safe check and record with atomic operation.
        
        Args:
            idempotency_key: Unique key for deduplication
            exposure_event: Exposure event to record
            
        Returns:
            True if first exposure (should emit), False if duplicate
        """
        with self._lock:
            if idempotency_key in self._emitted_keys:
                return False  # Already emitted
            self._emitted_keys.add(idempotency_key)
            return True  # First emission


# ============================================================================
# ISOLATION MANAGER (INTERFACE)
# ============================================================================

class IsolationManager:
    """
    Interface for cross-experiment isolation tracking
    
    Implementations must track active exposures to detect conflicts.
    """
    
    def check_isolation_conflict(
        self,
        identity_id: str,
        experiment_id: str,
        isolation_groups: tuple[str, ...]
    ) -> Optional[str]:
        """
        Check if identity has conflicting experiment exposure
        
        Returns:
            None if no conflict
            Conflicting experiment_id if conflict detected
        """
        raise NotImplementedError("Subclass must implement")
    
    def record_exposure(
        self,
        identity_id: str,
        experiment_id: str,
        isolation_groups: tuple[str, ...]
    ) -> None:
        """Record exposure for isolation tracking"""
        raise NotImplementedError("Subclass must implement")


class InMemoryIsolationManager(IsolationManager):
    """
    In-memory isolation manager for testing.
    
    WARNING: Not production-safe (lost on restart).
    Use persistent storage in production.
    """
    
    def __init__(self):
        # identity_id -> set of (experiment_id, isolation_groups)
        self._exposures: Dict[str, Set[Tuple[str, Tuple[str, ...]]]] = {}
        self._lock = threading.Lock()  # Thread-safe
    
    def check_isolation_conflict(
        self,
        identity_id: str,
        experiment_id: str,
        isolation_groups: Tuple[str, ...]
    ) -> Optional[str]:
        """
        Check for isolation conflicts (thread-safe).
        
        Args:
            identity_id: Identity to check
            experiment_id: Experiment attempting assignment
            isolation_groups: Isolation groups for this experiment
            
        Returns:
            None if no conflict, conflicting experiment_id if conflict detected
        """
        with self._lock:
            if identity_id not in self._exposures:
                return None
            
            for exposed_exp_id, exposed_groups in self._exposures[identity_id]:
                # Check if any isolation group overlaps
                if isolation_groups and exposed_groups:
                    common_groups = set(isolation_groups) & set(exposed_groups)
                    if common_groups and exposed_exp_id != experiment_id:
                        return exposed_exp_id
            
            return None
    
    def record_exposure(
        self,
        identity_id: str,
        experiment_id: str,
        isolation_groups: Tuple[str, ...]
    ) -> None:
        """
        Record exposure for isolation tracking (thread-safe).
        
        Args:
            identity_id: Identity exposed
            experiment_id: Experiment assigned
            isolation_groups: Isolation groups for this experiment
        """
        with self._lock:
            if identity_id not in self._exposures:
                self._exposures[identity_id] = set()
            self._exposures[identity_id].add((experiment_id, isolation_groups))


# ============================================================================
# CORE RUNTIME FUNCTIONS
# ============================================================================

def _compute_assignment_bucket(
    identity: IdentityContext,
    experiment_snapshot: ExperimentConfigSnapshot
) -> int:
    """
    Compute deterministic bucket for identity.
    
    Uses hash-based bucketing with experiment seed for determinism.
    
    Guarantees:
    - Same identity + same config → same bucket (always)
    - Uniform distribution across bucket domain
    - No randomness
    - Reproducible across restarts, deployments, replays
    - Collision-resistant (SHA256)
    
    Args:
        identity: Identity context
        experiment_snapshot: Experiment configuration snapshot
        
    Returns:
        Bucket number in range [0, bucket_domain_size)
        
    Raises:
        InvalidAllocationRangeError: If bucket calculation fails
    """
    # Validate inputs
    if not identity.canonical_identity_id:
        raise IdentityContextMissingError("canonical_identity_id cannot be empty")
    
    if not experiment_snapshot.hash_seed:
        raise InvalidConfigVersionError("hash_seed cannot be empty")
    
    if experiment_snapshot.bucket_domain_size <= 0:
        raise InvalidAllocationRangeError(
            f"bucket_domain_size must be > 0, got {experiment_snapshot.bucket_domain_size}"
        )
    
    # Build deterministic hash input
    # Order matters for determinism: experiment_id:hash_seed:identity_id
    hash_input = (
        f"{experiment_snapshot.experiment_id}:"
        f"{experiment_snapshot.hash_seed}:"
        f"{identity.canonical_identity_id}"
    )
    
    # Hash with SHA256 for collision resistance
    hash_bytes = hashlib.sha256(hash_input.encode('utf-8')).digest()
    
    # Convert first 8 bytes to integer (64-bit unsigned)
    hash_int = int.from_bytes(hash_bytes[:8], byteorder='big', signed=False)
    
    # Modulo to bucket domain (ensures uniform distribution)
    bucket = hash_int % experiment_snapshot.bucket_domain_size
    
    # Validate bucket is in valid range
    if bucket < 0 or bucket >= experiment_snapshot.bucket_domain_size:
        raise InvalidAllocationRangeError(
            f"Computed bucket {bucket} out of range [0, {experiment_snapshot.bucket_domain_size})"
        )
    
    return bucket


def _map_bucket_to_variant(
    bucket: int,
    allocation_ranges: Tuple[AllocationRange, ...]
) -> Optional[str]:
    """
    Map bucket to variant ID using allocation ranges.
    
    Allocation boundaries must be explicit (no floating arithmetic ambiguity).
    Runtime must use allocation_ranges exactly as defined.
    Never recompute boundaries.
    Never infer allocation order.
    
    Args:
        bucket: Bucket number to map
        allocation_ranges: Ordered allocation ranges (from variant_generator)
        
    Returns:
        Variant ID if bucket is covered, None if not covered
        
    Raises:
        InvalidAllocationRangeError: If bucket not covered (should never happen with valid config)
    """
    if bucket < 0:
        raise InvalidAllocationRangeError(f"Bucket cannot be negative: {bucket}")
    
    # Linear search through ranges (ranges are typically small, O(n) is acceptable)
    # In production with many variants, could use binary search for O(log n)
    for alloc_range in allocation_ranges:
        if alloc_range.contains(bucket):
            return alloc_range.variant_id
    
    # Bucket not covered - this should never happen with valid config
    # but we fail explicitly rather than silently
    raise InvalidAllocationRangeError(
        f"Bucket {bucket} not covered by any allocation range. "
        f"Ranges: {[(r.start_bucket, r.end_bucket) for r in allocation_ranges]}"
    )


def _check_override(
    identity: IdentityContext,
    override_rules: tuple[OverrideRule, ...],
    allow_overrides: bool
) -> Optional[str]:
    """
    Check if identity has override rule
    
    Returns variant_id if override exists and allowed, None otherwise
    """
    if not allow_overrides or not override_rules:
        return None
    
    for rule in override_rules:
        if rule.identity_id == identity.canonical_identity_id:
            return rule.variant_id
    
    return None


def _generate_idempotency_key(
    experiment_id: str,
    identity_id: str,
    config_version: int
) -> str:
    """
    Generate deterministic idempotency key
    
    Key format: {experiment_id}:{identity_id}:{config_version}
    """
    key_input = f"{experiment_id}:{identity_id}:{config_version}"
    return hashlib.sha256(key_input.encode('utf-8')).hexdigest()


def _generate_assignment_hash(
    experiment_id: str,
    variant_id: Optional[str],
    bucket: Optional[int],
    config_version: int
) -> str:
    """
    Generate deterministic assignment hash for verification
    """
    hash_input = json.dumps({
        'experiment_id': experiment_id,
        'variant_id': variant_id,
        'bucket': bucket,
        'config_version': config_version
    }, sort_keys=True, separators=(',', ':'))
    
    return hashlib.sha256(hash_input.encode('utf-8')).hexdigest()[:16]


def evaluate_assignment(
    experiment_snapshot: ExperimentConfigSnapshot,
    identity: IdentityContext,
    request_context: RequestContext,
    runtime_flags: RuntimeFlags,
    idempotency_manager: Optional[IdempotencyManager] = None,
    isolation_manager: Optional[IsolationManager] = None,
    exposure_logger: Optional[Any] = None,
    logger: Optional[logging.Logger] = None,
) -> AssignmentResult:
    """
    Evaluate experiment assignment for identity.
    
    This is the main entry point for deterministic variant assignment.
    
    Canonical Assignment Flow:
    1. Check experiment state (ACTIVE only)
    2. Evaluate eligibility predicates
    3. Compute deterministic bucket
    4. Map bucket → variant
    5. Apply override rules (if allowed)
    6. Validate isolation constraints
    7. Emit exposure event (idempotent)
    8. Return variant decision
    
    All inputs must be immutable.
    No live config fetches allowed.
    No runtime mutation of config.
    
    Args:
        experiment_snapshot: Immutable experiment configuration
        identity: Immutable identity context
        request_context: Immutable request context
        runtime_flags: Runtime behavior flags
        idempotency_manager: Optional manager for exposure deduplication
        isolation_manager: Optional manager for cross-experiment isolation
        exposure_logger: Optional logger for exposure events
        logger: Optional logger for structured logging
    
    Returns:
        AssignmentResult with variant decision and metadata
    
    Raises:
        InvalidExperimentStateError: If experiment not in valid state
        IdentityContextMissingError: If required identity context missing
        InvalidAllocationRangeError: If allocation ranges invalid
        InvalidConfigVersionError: If config version invalid
    
    Guarantees:
    - Same identity + config → same variant (deterministic)
    - No assignment without ACTIVE state
    - No double exposure emission (with idempotency manager)
    - No silent fallback to control
    - Reproducible across replays
    - Constant time per experiment (O(1) bucketing, O(n) range lookup where n is small)
    """
    if logger is None:
        logger = logging.getLogger(__name__)
    
    logical_timestamp = request_context.timestamp
    
    # Validate inputs
    if not experiment_snapshot.experiment_id:
        raise InvalidConfigVersionError("experiment_snapshot.experiment_id cannot be empty")
    
    if experiment_snapshot.version < 1:
        raise InvalidConfigVersionError(
            f"experiment_snapshot.version must be >= 1, got {experiment_snapshot.version}"
        )
    
    if not identity.canonical_identity_id:
        raise IdentityContextMissingError("identity.canonical_identity_id cannot be empty")
    
    logger.debug(
        f"Evaluating assignment for experiment {experiment_snapshot.experiment_id} v{experiment_snapshot.version}, "
        f"identity: {identity.canonical_identity_id}"
    )
    
    # Step 1: Check experiment state
    if experiment_snapshot.state != ExperimentState.ACTIVE:
        return AssignmentResult(
            experiment_id=experiment_snapshot.experiment_id,
            variant_id=None,
            eligibility_status=EligibilityStatus.EXCLUDED_BY_STATE,
            exclusion_reason=f"Experiment state is {experiment_snapshot.state.value}, not ACTIVE",
            assignment_bucket=None,
            exposure_emitted=False,
            config_version=experiment_snapshot.version,
            assignment_hash="",
            logical_timestamp=logical_timestamp
        )
    
    # Step 2: Check timing window (freeze timestamp)
    if experiment_snapshot.freeze_timestamp is not None:
        if logical_timestamp > experiment_snapshot.freeze_timestamp:
            return AssignmentResult(
                experiment_id=experiment_snapshot.experiment_id,
                variant_id=None,
                eligibility_status=EligibilityStatus.EXCLUDED_BY_TIMING,
                exclusion_reason=f"After freeze timestamp {experiment_snapshot.freeze_timestamp}",
                assignment_bucket=None,
                exposure_emitted=False,
                config_version=experiment_snapshot.version,
                assignment_hash="",
                logical_timestamp=logical_timestamp
            )
    
    # Step 3: Evaluate eligibility predicates
    is_eligible = experiment_snapshot.eligibility_predicate.evaluate(identity, request_context)
    
    if not is_eligible:
        return AssignmentResult(
            experiment_id=experiment_snapshot.experiment_id,
            variant_id=None,
            eligibility_status=EligibilityStatus.EXCLUDED_BY_PREDICATE,
            exclusion_reason="Failed eligibility predicate evaluation",
            assignment_bucket=None,
            exposure_emitted=False,
            config_version=experiment_snapshot.version,
            assignment_hash="",
            logical_timestamp=logical_timestamp
        )
    
    # Step 4: Compute deterministic bucket
    assignment_bucket = _compute_assignment_bucket(identity, experiment_snapshot)
    
    # Step 5: Map bucket to variant
    variant_id = _map_bucket_to_variant(assignment_bucket, experiment_snapshot.allocation_ranges)
    
    if variant_id is None:
        raise InvalidAllocationRangeError(
            f"Bucket {assignment_bucket} not covered by allocation ranges for experiment {experiment_snapshot.experiment_id}"
        )
    
    # Step 6: Check for override (if allowed)
    override_applied = False
    override_variant_id = _check_override(identity, experiment_snapshot.override_rules, runtime_flags.allow_overrides)
    
    if override_variant_id is not None:
        variant_id = override_variant_id
        override_applied = True
    
    # Step 7: Check isolation constraints
    if runtime_flags.enable_isolation_check and isolation_manager is not None:
        conflict_exp_id = isolation_manager.check_isolation_conflict(
            identity.canonical_identity_id,
            experiment_snapshot.experiment_id,
            experiment_snapshot.isolation_groups
        )
        
        if conflict_exp_id is not None:
            return AssignmentResult(
                experiment_id=experiment_snapshot.experiment_id,
                variant_id=None,
                eligibility_status=EligibilityStatus.BLOCKED_BY_ISOLATION,
                exclusion_reason=f"Conflicts with experiment {conflict_exp_id}",
                assignment_bucket=assignment_bucket,
                exposure_emitted=False,
                config_version=experiment_snapshot.version,
                assignment_hash="",
                logical_timestamp=logical_timestamp
            )
    
    # Step 8: Generate assignment hash
    assignment_hash = _generate_assignment_hash(
        experiment_snapshot.experiment_id,
        variant_id,
        assignment_bucket,
        experiment_snapshot.version
    )
    
    # Step 9: Emit exposure event (with idempotency check)
    exposure_emitted = False
    
    # Generate idempotency key (deterministic)
    idempotency_key = _generate_idempotency_key(
        experiment_snapshot.experiment_id,
        identity.canonical_identity_id,
        experiment_snapshot.version
    )
    
    # Create exposure event
    exposure_event = ExposureEvent(
        experiment_id=experiment_snapshot.experiment_id,
        variant_id=variant_id,
        identity_id=identity.canonical_identity_id,
        assignment_bucket=assignment_bucket,
        assignment_seed_version=experiment_snapshot.hash_seed,
        config_version=experiment_snapshot.version,
        logical_timestamp=logical_timestamp,
        request_context_fingerprint=request_context.get_fingerprint(),
        idempotency_key=idempotency_key
    )
    
    # Check idempotency and emit (atomic operation)
    if runtime_flags.enable_idempotency_check and idempotency_manager is not None:
        # Check and record (atomic) - prevents double emission
        should_emit = idempotency_manager.check_and_record_exposure(idempotency_key, exposure_event)
        
        if should_emit:
            exposure_emitted = True
            # Emit to logging pipeline
            _emit_exposure_event(exposure_event, exposure_logger, logger)
        else:
            logger.debug(
                f"Exposure already emitted for experiment {experiment_snapshot.experiment_id}, "
                f"identity {identity.canonical_identity_id}, key: {idempotency_key[:16]}..."
            )
    else:
        # No idempotency check - always emit (not recommended for production)
        exposure_emitted = True
        _emit_exposure_event(exposure_event, exposure_logger, logger)
    
    # Step 10: Record isolation (if exposure emitted)
    if exposure_emitted and isolation_manager is not None:
        isolation_manager.record_exposure(
            identity.canonical_identity_id,
            experiment_snapshot.experiment_id,
            experiment_snapshot.isolation_groups
        )
    
    # Step 11: Return assignment result
    result = AssignmentResult(
        experiment_id=experiment_snapshot.experiment_id,
        variant_id=variant_id,
        eligibility_status=EligibilityStatus.ELIGIBLE if not override_applied else EligibilityStatus.OVERRIDDEN,
        exclusion_reason=None,
        assignment_bucket=assignment_bucket,
        exposure_emitted=exposure_emitted,
        config_version=experiment_snapshot.version,
        assignment_hash=assignment_hash,
        logical_timestamp=logical_timestamp,
        override_applied=override_applied
    )
    
    logger.info(
        f"Assignment result for experiment {experiment_snapshot.experiment_id} v{experiment_snapshot.version}: "
        f"variant={variant_id}, bucket={assignment_bucket}, eligible={result.is_assigned()}, "
        f"exposure_emitted={exposure_emitted}"
    )
    
    return result


def _emit_exposure_event(
    exposure_event: ExposureEvent,
    exposure_logger: Optional[Any],
    logger: logging.Logger,
) -> None:
    """
    Emit exposure event to logging pipeline.
    
    Exposure must be:
    - Emitted exactly once per identity per experiment
    - Idempotent
    - Replay-safe
    - Deterministically reproducible
    
    Args:
        exposure_event: Exposure event to emit
        exposure_logger: Optional logger interface
        logger: Standard logger for fallback
    """
    # Log structured exposure event
    exposure_dict = exposure_event.to_dict()
    logger.info(f"Exposure event: {json.dumps(exposure_dict, sort_keys=True)}")
    
    # Emit to exposure logger if available
    if exposure_logger is not None:
        try:
            if hasattr(exposure_logger, 'log_exposure'):
                exposure_logger.log_exposure(exposure_event)
            elif hasattr(exposure_logger, 'emit'):
                exposure_logger.emit(exposure_dict)
            elif hasattr(exposure_logger, 'write'):
                exposure_logger.write(json.dumps(exposure_dict))
        except Exception as e:
            logger.error(f"Failed to emit exposure event: {e}")


def replay_assignment(
    experiment_snapshot: ExperimentConfigSnapshot,
    identity: IdentityContext,
    request_context: RequestContext,
    runtime_flags: RuntimeFlags,
    expected_variant_id: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> Tuple[AssignmentResult, bool]:
    """
    Replay assignment to verify deterministic behavior.
    
    Used for:
    - Deterministic replay verification
    - Audit trail validation
    - Debugging assignment issues
    
    Args:
        experiment_snapshot: Experiment configuration snapshot
        identity: Identity context
        request_context: Request context
        runtime_flags: Runtime flags
        expected_variant_id: Expected variant ID (for verification)
        logger: Optional logger
        
    Returns:
        Tuple of (AssignmentResult, matches_expected)
        
    Raises:
        Same exceptions as evaluate_assignment
    """
    if logger is None:
        logger = logging.getLogger(__name__)
    
    # Evaluate assignment (deterministic)
    result = evaluate_assignment(
        experiment_snapshot=experiment_snapshot,
        identity=identity,
        request_context=request_context,
        runtime_flags=runtime_flags,
        idempotency_manager=None,  # Disable idempotency for replay
        isolation_manager=None,  # Disable isolation for replay
        exposure_logger=None,  # Don't emit exposure during replay
        logger=logger,
    )
    
    # Verify against expected result if provided
    matches_expected = True
    if expected_variant_id is not None:
        matches_expected = (result.variant_id == expected_variant_id)
        if not matches_expected:
            logger.warning(
                f"Replay mismatch: expected variant {expected_variant_id}, "
                f"got {result.variant_id} for experiment {experiment_snapshot.experiment_id}"
            )
    
    return result, matches_expected


# Export public API
__all__ = [
    # Enums
    'ExperimentState',
    'EligibilityStatus',
    
    # Context structures
    'IdentityContext',
    'RequestContext',
    'RuntimeFlags',
    
    # Configuration
    'AllocationRange',
    'EligibilityPredicate',
    'OverrideRule',
    'ExperimentConfigSnapshot',
    
    # Results
    'AssignmentResult',
    'ExposureEvent',
    
    # Exceptions
    'ExperimentRuntimeError',
    'InvalidConfigVersionError',
    'InvalidExperimentStateError',
    'InvalidAllocationRangeError',
    'IdentityContextMissingError',
    
    # Managers
    'IdempotencyManager',
    'InMemoryIdempotencyManager',
    'IsolationManager',
    'InMemoryIsolationManager',
    
    # Core functions
    'evaluate_assignment',
    'replay_assignment',
]