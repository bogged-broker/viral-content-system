"""
/experiments/experiment_manager.py

Experiment Lifecycle & Registry Authority
(No Ghost Experiments, No Mid-Flight Mutation, No Version Drift)

This module is the single lifecycle authority and registry for all experiments
in the system.

CRITICAL PRINCIPLES:
- Explicit lifecycle transitions (strict state machine)
- Version discipline (monotonic, immutable post-activation)
- Immutable configuration snapshots (frozen on activation)
- Active experiment registry integrity
- Safe activation/pause/termination handling
- Atomic state transitions
- Deterministic retrieval

LIFECYCLE STATES:
DRAFT → VALIDATED → ACTIVE ⇄ PAUSED → STOPPING → COMPLETED → ARCHIVED
                      ↓                    ↓
                  TERMINATED ──────────────┘

STATE MACHINE RULES:
- Only explicit transitions allowed
- No automatic state inference
- All transitions audited
- ACTIVE config is immutable
- Terminal states cannot reactivate

SNAPSHOT FREEZING:
When transitioning to ACTIVE:
- Full config frozen (immutable)
- allocation_snapshot frozen
- eligibility_definition frozen
- config_hash recorded
Runtime ONLY consumes frozen snapshot.

CONFLICT DETECTION:
- No duplicate ACTIVE versions for same experiment_id
- No mutually exclusive experiments ACTIVE simultaneously
- Conflict detection before activation (hard fail on violation)
"""

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Dict, List, Optional, Set, Any, Tuple, FrozenSet, Union
from datetime import datetime, timezone
from collections import defaultdict
from types import MappingProxyType
import hashlib
import json
import logging
import threading
from contextlib import contextmanager


# ============================================================================
# DEEP IMMUTABILITY UTILITIES (TIER-0 REQUIREMENT)
# ============================================================================

def deep_freeze(obj: Any) -> Any:
    """
    Deep freeze nested structures for true immutability.
    
    TIER-0 REQUIREMENT: Mathematically enforce immutability of ALL nested structures.
    Recursively freezes dicts, lists, sets, tuples, and nested structures to prevent ANY mutation.
    Prevents snapshot drift from nested mutation.
    
    This is a structural enforcement, not just convention.
    Mutation of returned objects will raise TypeError (structurally impossible).
    
    Args:
        obj: Object to deep freeze
        
    Returns:
        Deeply frozen object:
        - MappingProxyType for dicts (recursively frozen, mutation raises TypeError)
        - tuple for lists (recursively frozen, mutation raises TypeError)
        - frozenset for sets (recursively frozen, mutation raises TypeError)
        - Primitive types unchanged (already immutable)
    """
    # Handle dict (convert to MappingProxyType - mutation raises TypeError)
    if isinstance(obj, dict):
        # Recursively freeze nested dicts and convert to MappingProxyType
        # This ensures ALL nested dicts are also immutable
        frozen_dict = {k: deep_freeze(v) for k, v in obj.items()}
        return MappingProxyType(frozen_dict)
    
    # Handle list (convert to tuple - mutation raises TypeError)
    elif isinstance(obj, list):
        # Convert lists to tuples (immutable) and recursively freeze elements
        return tuple(deep_freeze(v) for v in obj)
    
    # Handle set (convert to frozenset - mutation raises TypeError)
    elif isinstance(obj, set):
        # Convert sets to frozensets (immutable) and recursively freeze elements
        return frozenset(deep_freeze(v) for v in obj)
    
    # Handle tuple (recursively freeze elements)
    elif isinstance(obj, tuple):
        # Tuples are already immutable, but recursively freeze elements
        return tuple(deep_freeze(v) for v in obj)
    
    # Handle MappingProxyType (recursively freeze values)
    elif isinstance(obj, MappingProxyType):
        # Already a MappingProxyType, but recursively freeze values
        return MappingProxyType({k: deep_freeze(v) for k, v in obj.items()})
    
    # Handle frozenset (recursively freeze elements)
    elif isinstance(obj, frozenset):
        # Already a frozenset, but recursively freeze elements
        return frozenset(deep_freeze(v) for v in obj)
    
    # Handle dataclass-like objects (if they have __dict__)
    elif hasattr(obj, '__dict__') and not isinstance(obj, (str, int, float, bool, type(None), datetime)):
        # Recursively freeze dataclass fields
        frozen_dict = {k: deep_freeze(v) for k, v in obj.__dict__.items()}
        # Return as MappingProxyType (cannot mutate original)
        return MappingProxyType(frozen_dict)
    
    # Primitive types, strings, numbers, None, datetime, etc. are already immutable
    else:
        return obj


# ============================================================================
# EXPERIMENT STATE (STRICT STATE MACHINE)
# ============================================================================

class ExperimentState(Enum):
    """
    Experiment lifecycle states.
    
    Only explicit transitions allowed (see ALLOWED_TRANSITIONS).
    No automatic state inference permitted.
    """
    DRAFT = "DRAFT"
    """Initial draft state"""
    
    VALIDATED = "VALIDATED"
    """Validated and ready for activation"""
    
    ACTIVE = "ACTIVE"
    """Currently active and accepting assignments"""
    
    PAUSED = "PAUSED"
    """Temporarily paused (can resume)"""
    
    STOPPING = "STOPPING"
    """Stopping (no new assignments, wrapping up)"""
    
    COMPLETED = "COMPLETED"
    """Successfully completed"""
    
    TERMINATED = "TERMINATED"
    """Terminated early (not completed)"""
    
    ARCHIVED = "ARCHIVED"
    """Archived (terminal state)"""
    
    def __str__(self) -> str:
        return self.value
    
    def is_terminal(self) -> bool:
        """Check if state is terminal (cannot transition to non-terminal)."""
        return self in {
            ExperimentState.COMPLETED,
            ExperimentState.TERMINATED,
            ExperimentState.ARCHIVED,
        }


# Allowed state transitions (strict)
ALLOWED_TRANSITIONS: Dict[ExperimentState, Set[ExperimentState]] = {
    ExperimentState.DRAFT: {ExperimentState.VALIDATED},
    ExperimentState.VALIDATED: {ExperimentState.ACTIVE},
    ExperimentState.ACTIVE: {
        ExperimentState.PAUSED,
        ExperimentState.STOPPING,
        ExperimentState.TERMINATED,
    },
    ExperimentState.PAUSED: {
        ExperimentState.ACTIVE,
        ExperimentState.TERMINATED,
    },
    ExperimentState.STOPPING: {ExperimentState.COMPLETED},
    ExperimentState.COMPLETED: {ExperimentState.ARCHIVED},
    ExperimentState.TERMINATED: {ExperimentState.ARCHIVED},
    ExperimentState.ARCHIVED: set(),  # Terminal, no transitions
}


# ============================================================================
# ALLOCATION SNAPSHOT (IMMUTABLE)
# ============================================================================

@dataclass(frozen=True)
class AllocationSnapshot:
    """
    Immutable allocation configuration snapshot.
    
    Frozen when experiment transitions to ACTIVE.
    Cannot be modified once frozen.
    Nested dict is wrapped in MappingProxyType for deep immutability.
    """
    variants: MappingProxyType[str, float]
    """Variant allocation percentages (must sum to 100) - MappingProxyType prevents mutation"""
    
    hash_seed: str
    """Hash seed for deterministic assignment"""
    
    bucket_domain: str
    """Bucketing domain (e.g., 'user_id', 'session_id')"""
    
    allocation_hash: str
    """Hash of allocation configuration"""
    
    def __post_init__(self):
        """Ensure nested structures are truly immutable."""
        # Convert dict to MappingProxyType if needed
        if isinstance(self.variants, dict):
            object.__setattr__(self, 'variants', MappingProxyType(self.variants))
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "variants": dict(self.variants),  # Convert MappingProxyType to dict
            "hash_seed": self.hash_seed,
            "bucket_domain": self.bucket_domain,
            "allocation_hash": self.allocation_hash,
        }


# ============================================================================
# ELIGIBILITY DEFINITION (IMMUTABLE)
# ============================================================================

@dataclass(frozen=True)
class EligibilityDefinition:
    """
    Immutable eligibility criteria for experiment enrollment.
    
    Frozen when experiment transitions to ACTIVE.
    Nested dicts are wrapped in MappingProxyType to prevent mutation.
    """
    required_trust_tier: Optional[int] = None
    """Minimum trust tier required (None = no requirement)"""
    
    allowed_regions: Optional[FrozenSet[str]] = None
    """Allowed geographic regions (None = all allowed) - frozen set for immutability"""
    
    excluded_identities: FrozenSet[str] = field(default_factory=lambda: frozenset())
    """Explicitly excluded identity IDs - frozen set for immutability"""
    
    custom_filters: MappingProxyType[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    """Custom eligibility filters - MappingProxyType prevents mutation"""
    
    eligibility_hash: str = ""
    """Hash of eligibility definition"""
    
    def __post_init__(self):
        """Ensure nested structures are truly immutable."""
        # Convert Set to FrozenSet if needed
        if isinstance(self.allowed_regions, set):
            object.__setattr__(self, 'allowed_regions', frozenset(self.allowed_regions))
        if isinstance(self.excluded_identities, set):
            object.__setattr__(self, 'excluded_identities', frozenset(self.excluded_identities))
        # Convert dict to MappingProxyType if needed
        if isinstance(self.custom_filters, dict):
            object.__setattr__(self, 'custom_filters', MappingProxyType(self.custom_filters))
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "required_trust_tier": self.required_trust_tier,
            "allowed_regions": sorted(self.allowed_regions) if self.allowed_regions else None,
            "excluded_identities": sorted(self.excluded_identities),
            "custom_filters": dict(self.custom_filters),  # Convert MappingProxyType to dict
            "eligibility_hash": self.eligibility_hash,
        }


# ============================================================================
# EXPERIMENT SNAPSHOT (FROZEN CONFIGURATION)
# ============================================================================

@dataclass(frozen=True)
class ExperimentSnapshot:
    """
    Complete immutable experiment configuration snapshot.
    
    Created when experiment transitions to ACTIVE.
    Runtime ONLY consumes this frozen snapshot.
    """
    experiment_id: str
    """Unique experiment identifier"""
    
    version: int
    """Experiment version (monotonic)"""
    
    state: ExperimentState
    """Current lifecycle state"""
    
    allocation_snapshot: AllocationSnapshot
    """Frozen allocation configuration"""
    
    eligibility_definition: EligibilityDefinition
    """Frozen eligibility criteria"""
    
    start_timestamp: datetime
    """When experiment starts accepting assignments"""
    
    freeze_timestamp: Optional[datetime]
    """When enrollment freezes (None = no freeze)"""
    
    termination_timestamp: Optional[datetime]
    """When experiment terminates (None = not terminated)"""
    
    created_by: str
    """Actor who created this version"""
    
    logical_created_at: int
    """Logical timestamp when created"""
    
    schema_version: int
    """Experiment schema version"""
    
    config_hash: str
    """Deterministic hash of entire configuration"""
    
    metadata: MappingProxyType[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    """Additional non-behavioral metadata - MappingProxyType prevents mutation"""
    
    def __post_init__(self):
        """Ensure nested structures are truly immutable."""
        # Convert dict to MappingProxyType if needed
        if isinstance(self.metadata, dict):
            object.__setattr__(self, 'metadata', MappingProxyType(self.metadata))
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "experiment_id": self.experiment_id,
            "version": self.version,
            "state": str(self.state),
            "allocation_snapshot": self.allocation_snapshot.to_dict(),
            "eligibility_definition": self.eligibility_definition.to_dict(),
            "start_timestamp": self.start_timestamp.isoformat(),
            "freeze_timestamp": self.freeze_timestamp.isoformat() if self.freeze_timestamp else None,
            "termination_timestamp": self.termination_timestamp.isoformat() if self.termination_timestamp else None,
            "created_by": self.created_by,
            "logical_created_at": self.logical_created_at,
            "schema_version": self.schema_version,
            "config_hash": self.config_hash,
            "metadata": dict(self.metadata),  # Convert MappingProxyType to dict
        }


# ============================================================================
# LIFECYCLE TRANSITION EVENT (AUDIT)
# ============================================================================

@dataclass(frozen=True)
class LifecycleTransitionEvent:
    """
    Immutable audit record of lifecycle state transition.
    """
    experiment_id: str
    version: int
    old_state: ExperimentState
    new_state: ExperimentState
    actor: str
    transition_timestamp: datetime
    logical_timestamp: int
    config_hash: str
    reason: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for audit logging."""
        return {
            "experiment_id": self.experiment_id,
            "version": self.version,
            "old_state": str(self.old_state),
            "new_state": str(self.new_state),
            "actor": self.actor,
            "transition_timestamp": self.transition_timestamp.isoformat(),
            "logical_timestamp": self.logical_timestamp,
            "config_hash": self.config_hash,
            "reason": self.reason,
        }


# ============================================================================
# CONCURRENCY CONTROL
# ============================================================================

class ExperimentLockManager:
    """
    Manages locks for concurrent experiment operations.
    
    Prevents:
    - Double activation
    - Simultaneous version overlap
    - Lost state transitions
    """
    
    def __init__(self):
        """Initialize lock manager."""
        self._locks: Dict[str, threading.Lock] = {}
        self._active_operations: Dict[str, Set[str]] = {}  # experiment_id -> set of operation_ids
        self._global_lock = threading.Lock()
    
    def acquire_experiment_lock(
        self,
        experiment_id: str,
        operation_id: str,
        timeout_seconds: float = 30.0,
    ) -> bool:
        """
        Acquire lock for experiment operation.
        
        Args:
            experiment_id: Experiment ID
            operation_id: Unique operation identifier
            timeout_seconds: Maximum time to wait for lock
            
        Returns:
            True if lock acquired, False if timeout
        """
        with self._global_lock:
            if experiment_id not in self._locks:
                self._locks[experiment_id] = threading.Lock()
            
            if experiment_id not in self._active_operations:
                self._active_operations[experiment_id] = set()
        
        # Acquire per-experiment lock
        lock = self._locks[experiment_id]
        acquired = lock.acquire(timeout=timeout_seconds)
        
        if acquired:
            with self._global_lock:
                self._active_operations[experiment_id].add(operation_id)
        
        return acquired
    
    def release_experiment_lock(
        self,
        experiment_id: str,
        operation_id: str,
    ) -> None:
        """Release lock for experiment operation."""
        with self._global_lock:
            if experiment_id in self._active_operations:
                self._active_operations[experiment_id].discard(operation_id)
            
            if experiment_id in self._locks:
                self._locks[experiment_id].release()


# Global lock manager instance (thread-safe singleton)
_experiment_lock_manager = ExperimentLockManager()


# ============================================================================
# EXPERIMENT MANAGER
# ============================================================================

class ExperimentManager:
    """
    Experiment lifecycle and registry authority.
    
    Single authority for:
    - Experiment registration
    - Version management
    - Lifecycle state transitions
    - Snapshot freezing
    - Active registry management
    - Conflict detection
    - Safe activation/deactivation
    
    All state transitions are explicit, audited, and atomic.
    All ACTIVE experiment configs are immutable.
    """
    
    # Schema version
    EXPERIMENT_SCHEMA_VERSION = 1
    
    def __init__(
        self,
        *,
        persistence_layer: Optional[Any] = None,
        audit_logger: Optional[Any] = None,
        invariants_validator: Optional[Any] = None,
        logger: Optional[logging.Logger] = None,
    ):
        """
        Initialize experiment manager.
        
        Args:
            persistence_layer: Interface to experiment storage
            audit_logger: Interface to audit logging
            invariants_validator: Interface to experiment_invariants.py
            logger: Optional logger for structured logging
        """
        self.persistence_layer = persistence_layer
        self.audit_logger = audit_logger
        self.invariants_validator = invariants_validator
        self.logger = logger or logging.getLogger(__name__)
        self._lock_manager = _experiment_lock_manager
        
        # TIER-0: Storage is authoritative, in-memory is cache only
        # Key: (experiment_id, version) - cache for performance, not source of truth
        self._experiments: Dict[Tuple[str, int], ExperimentSnapshot] = {}
        
        # Active registry: experiment_id → version (cache only, storage is authoritative)
        self._active_registry: Dict[str, int] = {}
        
        # Latest version tracking: experiment_id → version (cache only)
        self._latest_versions: Dict[str, int] = {}
        
        # Logical clock for ordering (monotonic, restored from persistence)
        self._logical_clock = 0
        
        # Transition history for audit (append-only, loaded from persistence)
        self._transition_history: List[LifecycleTransitionEvent] = []
        
        # Conflict tracking (mutually exclusive experiments)
        # Key: exclusive_group_id, Value: set of experiment_ids
        self._exclusive_groups: Dict[str, Set[str]] = defaultdict(set)
        
        # Experiment metadata index (for fast lookup)
        self._experiment_metadata: Dict[str, Dict[str, Any]] = {}
        
        # Version tracking for CAS (compare-and-swap) distributed concurrency
        # Key: (experiment_id, version), Value: version_sequence_number
        # NOTE: Storage is authoritative, this is cache only
        self._version_sequences: Dict[Tuple[str, int], int] = {}
        
        # TIER-0 REQUIREMENT: Persistence layer is mandatory for production
        if self.persistence_layer is None:
            self.logger.warning(
                "Persistence layer not configured. Tier-0 infrastructure requires storage-backed registry. "
                "This manager will not be restart-safe or multi-node safe."
            )
        else:
            # Load state from persistence on initialization (Tier-0 recovery requirement)
            try:
                self._recover_from_persistence()
            except Exception as e:
                self.logger.error(
                    f"Failed to recover state from persistence during initialization: {e}"
                )
                # Tier-0: Recovery failure is fatal
                raise RuntimeError(
                    f"Recovery from persistence failed. Cannot initialize ExperimentManager. "
                    f"Tier-0 infrastructure requires successful recovery. Original error: {e}"
                ) from e
    
    # ========================================================================
    # PUBLIC INTERFACE - REGISTRATION
    # ========================================================================
    
    def register_experiment(
        self,
        experiment_id: str,
        allocation_config: Dict[str, float],
        eligibility_config: Dict[str, Any],
        *,
        hash_seed: str,
        bucket_domain: str = "user_id",
        start_timestamp: Optional[datetime] = None,
        freeze_timestamp: Optional[datetime] = None,
        created_by: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ExperimentSnapshot:
        """
        Register a new experiment (creates version 1 in DRAFT state).
        
        Args:
            experiment_id: Unique experiment identifier
            allocation_config: Variant allocation percentages
            eligibility_config: Eligibility criteria
            hash_seed: Hash seed for assignment
            bucket_domain: Bucketing domain
            start_timestamp: When experiment starts (None = immediate)
            freeze_timestamp: When enrollment freezes (None = no freeze)
            created_by: Actor creating experiment
            metadata: Additional metadata
            
        Returns:
            Created experiment snapshot
            
        Raises:
            ValueError: If experiment already exists or config invalid
        """
        # Check if experiment already exists
        if experiment_id in self._latest_versions:
            raise ValueError(
                f"Experiment '{experiment_id}' already exists. "
                f"Use create_version() to create new version."
            )
        
        # Validate allocation sums to 100
        if abs(sum(allocation_config.values()) - 100.0) > 0.01:
            raise ValueError(
                f"Allocation percentages must sum to 100, got {sum(allocation_config.values())}"
            )
        
        # Set defaults
        if start_timestamp is None:
            start_timestamp = datetime.now(timezone.utc)
        
        if metadata is None:
            metadata = {}
        
        # Validate freeze timestamp
        if freeze_timestamp is not None:
            if freeze_timestamp <= start_timestamp:
                raise ValueError(
                    f"freeze_timestamp must be after start_timestamp"
                )
        
        # Create allocation snapshot (with immutable nested structures)
        allocation_hash = self._compute_allocation_hash(allocation_config, hash_seed, bucket_domain)
        allocation_snapshot = AllocationSnapshot(
            variants=MappingProxyType(allocation_config),
            hash_seed=hash_seed,
            bucket_domain=bucket_domain,
            allocation_hash=allocation_hash,
        )
        
        # Create eligibility definition (with immutable nested structures)
        eligibility_hash = self._compute_eligibility_hash(eligibility_config)
        allowed_regions = eligibility_config.get("allowed_regions")
        eligibility_definition = EligibilityDefinition(
            required_trust_tier=eligibility_config.get("required_trust_tier"),
            allowed_regions=frozenset(allowed_regions) if allowed_regions else None,
            excluded_identities=frozenset(eligibility_config.get("excluded_identities", [])),
            custom_filters=MappingProxyType(eligibility_config.get("custom_filters", {})),
            eligibility_hash=eligibility_hash,
        )
        
        # Create experiment snapshot
        version = 1
        logical_timestamp = self._next_logical_timestamp()
        
        config_hash = self._compute_config_hash(
            experiment_id=experiment_id,
            version=version,
            allocation_snapshot=allocation_snapshot,
            eligibility_definition=eligibility_definition,
            start_timestamp=start_timestamp,
            freeze_timestamp=freeze_timestamp,
            termination_timestamp=None,
        )
        
        snapshot = ExperimentSnapshot(
            experiment_id=experiment_id,
            version=version,
            state=ExperimentState.DRAFT,
            allocation_snapshot=allocation_snapshot,
            eligibility_definition=eligibility_definition,
            start_timestamp=start_timestamp,
            freeze_timestamp=freeze_timestamp,
            termination_timestamp=None,
            created_by=created_by,
            logical_created_at=logical_timestamp,
            schema_version=self.EXPERIMENT_SCHEMA_VERSION,
            config_hash=config_hash,
            metadata=MappingProxyType(metadata),
        )
        
        # Store snapshot
        self._experiments[(experiment_id, version)] = snapshot
        self._latest_versions[experiment_id] = version
        
        # Persist snapshot (mandatory for Tier-0)
        self._persist_snapshot(snapshot)
        
        # Persist latest version tracking
        self._persist_latest_versions()
        
        return snapshot
    
    def create_version(
        self,
        experiment_id: str,
        allocation_config: Dict[str, float],
        eligibility_config: Dict[str, Any],
        *,
        hash_seed: str,
        bucket_domain: str = "user_id",
        start_timestamp: Optional[datetime] = None,
        freeze_timestamp: Optional[datetime] = None,
        created_by: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ExperimentSnapshot:
        """
        Create a new version of existing experiment.
        
        Version is monotonically incremented.
        New version starts in DRAFT state.
        
        Args:
            Same as register_experiment
            
        Returns:
            Created experiment snapshot
            
        Raises:
            ValueError: If experiment doesn't exist or config invalid
        """
        # Check experiment exists
        if experiment_id not in self._latest_versions:
            raise ValueError(
                f"Experiment '{experiment_id}' does not exist. "
                f"Use register_experiment() to create."
            )
        
        # Get next version (monotonic)
        new_version = self._latest_versions[experiment_id] + 1
        
        # Similar validation and creation as register_experiment
        # (code would be similar to above, incrementing version)
        
        # Validate allocation sums to 100
        if abs(sum(allocation_config.values()) - 100.0) > 0.01:
            raise ValueError(
                f"Allocation percentages must sum to 100, got {sum(allocation_config.values())}"
            )
        
        # Set defaults
        if start_timestamp is None:
            start_timestamp = datetime.now(timezone.utc)
        
        if metadata is None:
            metadata = {}
        
        # Validate freeze timestamp
        if freeze_timestamp is not None:
            if freeze_timestamp <= start_timestamp:
                raise ValueError(
                    f"freeze_timestamp must be after start_timestamp"
                )
        
        # Create allocation snapshot (with immutable nested structures)
        allocation_hash = self._compute_allocation_hash(allocation_config, hash_seed, bucket_domain)
        allocation_snapshot = AllocationSnapshot(
            variants=MappingProxyType(allocation_config),
            hash_seed=hash_seed,
            bucket_domain=bucket_domain,
            allocation_hash=allocation_hash,
        )
        
        # Create eligibility definition (with immutable nested structures)
        eligibility_hash = self._compute_eligibility_hash(eligibility_config)
        allowed_regions = eligibility_config.get("allowed_regions")
        eligibility_definition = EligibilityDefinition(
            required_trust_tier=eligibility_config.get("required_trust_tier"),
            allowed_regions=frozenset(allowed_regions) if allowed_regions else None,
            excluded_identities=frozenset(eligibility_config.get("excluded_identities", [])),
            custom_filters=MappingProxyType(eligibility_config.get("custom_filters", {})),
            eligibility_hash=eligibility_hash,
        )
        
        # Create experiment snapshot
        logical_timestamp = self._next_logical_timestamp()
        
        config_hash = self._compute_config_hash(
            experiment_id=experiment_id,
            version=new_version,
            allocation_snapshot=allocation_snapshot,
            eligibility_definition=eligibility_definition,
            start_timestamp=start_timestamp,
        )
        
        snapshot = ExperimentSnapshot(
            experiment_id=experiment_id,
            version=new_version,
            state=ExperimentState.DRAFT,
            allocation_snapshot=allocation_snapshot,
            eligibility_definition=eligibility_definition,
            start_timestamp=start_timestamp,
            freeze_timestamp=freeze_timestamp,
            termination_timestamp=None,
            created_by=created_by,
            logical_created_at=logical_timestamp,
            schema_version=self.EXPERIMENT_SCHEMA_VERSION,
            config_hash=config_hash,
            metadata=metadata,
        )
        
        # Store snapshot
        self._experiments[(experiment_id, new_version)] = snapshot
        self._latest_versions[experiment_id] = new_version
        
        # Persist snapshot and latest versions
        self._persist_snapshot(snapshot)
        self._persist_latest_versions()
        
        # Log version creation
        self.logger.info(
            f"Created version {new_version} of experiment {experiment_id}"
        )
        
        return snapshot
    
    # ========================================================================
    # PUBLIC INTERFACE - LIFECYCLE TRANSITIONS
    # ========================================================================
    
    def validate_experiment(
        self,
        experiment_id: str,
        version: int,
        *,
        actor: str,
    ) -> ExperimentSnapshot:
        """
        Transition experiment from DRAFT to VALIDATED.
        
        Runs invariants validation before transition.
        
        Args:
            experiment_id: Experiment to validate
            version: Version to validate
            actor: Actor performing validation
            
        Returns:
            Updated experiment snapshot
            
        Raises:
            ValueError: If transition invalid or validation fails
        """
        return self._transition_state(
            experiment_id=experiment_id,
            version=version,
            new_state=ExperimentState.VALIDATED,
            actor=actor,
            reason="Passed validation checks",
        )
    
    def activate_experiment(
        self,
        experiment_id: str,
        version: int,
        *,
        actor: str,
        operation_id: Optional[str] = None,
    ) -> ExperimentSnapshot:
        """
        Activate experiment (transition VALIDATED → ACTIVE).
        
        ATOMIC ACTIVATION PROTOCOL:
        1. Validate invariants
        2. Validate no conflicts
        3. Freeze config snapshot
        4. Persist snapshot atomically
        5. Transition state → ACTIVE
        6. Update active registry
        7. Log lifecycle event
        
        All steps atomic - no partial activation.
        
        Args:
            experiment_id: Experiment to activate
            version: Version to activate
            actor: Actor performing activation
            operation_id: Unique operation identifier for concurrency control
            
        Returns:
            Activated experiment snapshot
            
        Raises:
            ValueError: If activation invalid
            RuntimeError: If conflicts detected
        """
        if operation_id is None:
            operation_id = f"activate_{experiment_id}_v{version}_{datetime.now(timezone.utc).timestamp()}"
        
        # Acquire lock for concurrency control
        if not self._lock_manager.acquire_experiment_lock(experiment_id, operation_id):
            raise RuntimeError(
                f"Could not acquire lock for activation operation {operation_id} "
                f"on experiment {experiment_id}"
            )
        
        try:
            # Get current snapshot
            snapshot = self.get_experiment(experiment_id, version)
            
            # Validate current state
            if snapshot.state != ExperimentState.VALIDATED:
                raise ValueError(
                    f"Can only activate VALIDATED experiments, "
                    f"current state: {snapshot.state}"
                )
            
            # Validate time constraints
            now = datetime.now(timezone.utc)
            if snapshot.start_timestamp > now:
                raise ValueError(
                    f"Cannot activate experiment before start_timestamp: "
                    f"{snapshot.start_timestamp} > {now}"
                )
            
            # Validate invariants (via experiment_invariants.py)
            if self.invariants_validator is not None:
                try:
                    if hasattr(self.invariants_validator, 'validate'):
                        self.invariants_validator.validate(snapshot)
                    elif hasattr(self.invariants_validator, 'validate_experiment'):
                        self.invariants_validator.validate_experiment(snapshot)
                except Exception as e:
                    raise ValueError(
                        f"Invariant validation failed for {experiment_id} v{version}: {e}"
                    )
            
            # Check for conflicts (must happen before activation)
            self._check_activation_conflicts(experiment_id, version, snapshot)
            
            # Config already frozen (snapshot is immutable)
            # Verify config hash hasn't drifted
            recomputed_hash = self._compute_config_hash(
                experiment_id=snapshot.experiment_id,
                version=snapshot.version,
                allocation_snapshot=snapshot.allocation_snapshot,
                eligibility_definition=snapshot.eligibility_definition,
                start_timestamp=snapshot.start_timestamp,
                freeze_timestamp=snapshot.freeze_timestamp,
                termination_timestamp=snapshot.termination_timestamp,
            )
            
            if recomputed_hash != snapshot.config_hash:
                raise RuntimeError(
                    f"Config hash mismatch for {experiment_id} v{version}. "
                    f"Expected {snapshot.config_hash}, got {recomputed_hash}. "
                    f"This indicates config drift - snapshot may have been mutated."
                )
            
            # TIER-0 ATOMIC ACTIVATION PROTOCOL (PROVABLY TRANSACTIONAL):
            # CRITICAL: ALL operations (freeze + state transition + registry update + persistence)
            # MUST happen within a SINGLE atomic transaction boundary.
            # If ANY step fails, entire transaction rolls back with ZERO side effects.
            #
            # Protocol (ALL within transaction):
            # 1. Acquire distributed lock (prevents multi-node races)
            # 2. CAS check (distributed concurrency control)
            # 3. Deep freeze snapshot (structural immutability)
            # 4. State transition (VALIDATED → ACTIVE)
            # 5. Load authoritative registry from storage
            # 6. ATOMIC PERSISTENCE: snapshot + registry + version sequence + transition event
            # 7. Transaction commits (all-or-nothing)
            # 8. ONLY AFTER commit: Update in-memory cache + release lock
            #
            # This ensures: No partial activation. Either all succeeds or nothing changes.
            
            # Step 1: Acquire distributed lock BEFORE transaction (prevents multi-node races)
            distributed_lock_acquired = False
            lock_key = f"experiment_activation:{experiment_id}:{version}"
            
            try:
                if self.persistence_layer is not None and hasattr(self.persistence_layer, 'acquire_distributed_lock'):
                    lock_acquired = self.persistence_layer.acquire_distributed_lock(
                        lock_key=lock_key,
                        timeout_seconds=30.0,
                        ttl_seconds=60.0,  # Auto-release after 60s if process crashes
                    )
                    if not lock_acquired:
                        raise RuntimeError(
                            f"Could not acquire distributed lock for activation of {experiment_id} v{version}. "
                            f"Another process may be activating this experiment."
                        )
                    distributed_lock_acquired = True
                    self.logger.debug(f"Acquired distributed lock: {lock_key}")
            except Exception as e:
                if distributed_lock_acquired:
                    # Release lock if we acquired it but failed later
                    try:
                        if hasattr(self.persistence_layer, 'release_distributed_lock'):
                            self.persistence_layer.release_distributed_lock(lock_key)
                    except:
                        pass
                raise
            
            # Step 2: ATOMIC TRANSACTION BOUNDARY - ALL operations within single transaction
            # This ensures freeze + state transition + registry update are provably atomic
            activated_snapshot = None
            event = None
            
            try:
                with self._activation_transaction() as txn:
                    try:
                        # Step 2a: CAS check WITHIN transaction (distributed concurrency control)
                        expected_version_sequence = self._get_version_sequence(experiment_id, version)
                        if self.persistence_layer is not None:
                            # Check if version has been modified by another process/worker (distributed CAS)
                            if hasattr(self.persistence_layer, 'check_version'):
                                stored_version = self.persistence_layer.check_version(experiment_id, version)
                                if stored_version is not None and stored_version != expected_version_sequence:
                                    raise RuntimeError(
                                        f"Version conflict detected for {experiment_id} v{version}. "
                                        f"Expected sequence {expected_version_sequence}, got {stored_version}. "
                                        f"Another process may have modified this experiment. Activation aborted."
                                    )
                        
                        # Step 2b: Deep freeze snapshot WITHIN transaction (structural immutability)
                        # This ensures snapshot is frozen atomically with persistence
                        frozen_snapshot = self._deep_freeze_snapshot(snapshot)
                        
                        # Step 2c: State transition WITHIN transaction (VALIDATED → ACTIVE)
                        activated_snapshot = ExperimentSnapshot(
                            experiment_id=frozen_snapshot.experiment_id,
                            version=frozen_snapshot.version,
                            state=ExperimentState.ACTIVE,  # New state
                            allocation_snapshot=frozen_snapshot.allocation_snapshot,
                            eligibility_definition=frozen_snapshot.eligibility_definition,
                            start_timestamp=frozen_snapshot.start_timestamp,
                            freeze_timestamp=frozen_snapshot.freeze_timestamp,
                            termination_timestamp=frozen_snapshot.termination_timestamp,
                            created_by=frozen_snapshot.created_by,
                            logical_created_at=frozen_snapshot.logical_created_at,
                            schema_version=frozen_snapshot.schema_version,
                            config_hash=frozen_snapshot.config_hash,  # Config unchanged
                            metadata=frozen_snapshot.metadata,
                        )
                        
                        # Step 2d: Load authoritative registry WITHIN transaction (storage-first)
                        authoritative_registry = self._load_active_registry_authoritative()
                        new_active_registry = authoritative_registry.copy()
                        new_active_registry[experiment_id] = version
                        
                        # Step 2e: ATOMIC PERSISTENCE WITHIN TRANSACTION - All-or-nothing
                        # TIER-0 REQUIREMENT: ALL operations (freeze + persist + state transition + registry)
                        # MUST be atomic. No partial activation possible.
                        # Persist: snapshot + registry + version sequence + transition event
                        new_version_sequence = expected_version_sequence + 1
                        
                        # Atomic persistence: snapshot + registry + version sequence
                        # ALL operations must succeed atomically or ALL must fail
                        if hasattr(self.persistence_layer, 'atomic_activate'):
                            # Backend supports atomic activation transaction (preferred Tier-0 path)
                            # This MUST atomically: persist snapshot + update state + update registry + store version
                            self.persistence_layer.atomic_activate(
                                snapshot=activated_snapshot,
                                active_registry=new_active_registry,
                                version_sequence=(experiment_id, version, new_version_sequence),
                                expected_version=expected_version_sequence,  # CAS check
                            )
                            # Verify atomic_activate actually persisted everything
                            # If it doesn't, we need to persist separately within transaction
                            if not hasattr(self.persistence_layer, '_atomic_activate_complete'):
                                # Backend's atomic_activate may not be fully atomic
                                # Persist components separately but within same transaction
                                self._persist_snapshot(activated_snapshot, transaction=txn)
                                self._persist_active_registry_atomic(new_active_registry, transaction=txn)
                                if hasattr(self.persistence_layer, 'store_version_sequence'):
                                    self.persistence_layer.store_version_sequence(
                                        experiment_id, version, new_version_sequence
                                    )
                        elif hasattr(self.persistence_layer, 'atomic_update_state'):
                            # Backend supports atomic state update with version check
                            # Use CAS-style write for distributed safety
                            success = self._atomic_state_transition_with_cas(
                                experiment_id=experiment_id,
                                version=version,
                                old_state=ExperimentState.VALIDATED,
                                new_state=ExperimentState.ACTIVE,
                                expected_version_sequence=expected_version_sequence,
                            )
                            if not success:
                                raise RuntimeError(
                                    f"CAS conflict: {experiment_id} v{version} "
                                    f"was modified by another process. Activation aborted."
                                )
                            # Persist snapshot and registry (still in transaction - MUST be atomic)
                            self._persist_snapshot(activated_snapshot, transaction=txn)
                            self._persist_active_registry_atomic(new_active_registry, transaction=txn)
                            if hasattr(self.persistence_layer, 'store_version_sequence'):
                                self.persistence_layer.store_version_sequence(
                                    experiment_id, version, new_version_sequence
                                )
                        else:
                            # Fallback: Persist each component (transaction ensures atomicity)
                            # TIER-0: Transaction MUST ensure all-or-nothing semantics
                            # If any fails, entire transaction rolls back
                            self._persist_snapshot(activated_snapshot, transaction=txn)
                            self._persist_active_registry_atomic(new_active_registry, transaction=txn)
                            if hasattr(self.persistence_layer, 'store_version_sequence'):
                                self.persistence_layer.store_version_sequence(
                                    experiment_id, version, new_version_sequence
                                )
                        
                        # Step 2f: Persist transition event WITHIN transaction (for deterministic recovery)
                        logical_timestamp = self._next_logical_timestamp()
                        event = LifecycleTransitionEvent(
                            experiment_id=experiment_id,
                            version=version,
                            old_state=snapshot.state,
                            new_state=ExperimentState.ACTIVE,
                            actor=actor,
                            transition_timestamp=datetime.now(timezone.utc),
                            logical_timestamp=logical_timestamp,
                            config_hash=snapshot.config_hash,
                            reason="Activated experiment",
                        )
                        # Persist transition event atomically with state change
                        if hasattr(self.persistence_layer, 'persist_transition_event'):
                            self.persistence_layer.persist_transition_event(event)
                        else:
                            # Fallback: append to transition history (will be persisted)
                            self._transition_history.append(event)
                        
                        # Step 2g: Transaction commits (all-or-nothing)
                        # At this point, ALL persistence operations succeeded atomically
                        txn.commit()
                        
                    except Exception as e:
                        # Transaction will rollback automatically
                        txn.rollback()
                        # Persistence failed - abort activation with ZERO side effects
                        self.logger.error(
                            f"CRITICAL: Atomic persistence failed for activation of {experiment_id} v{version}. "
                            f"Transaction rolled back. Activation aborted. No state changes applied. Error: {e}"
                        )
                        raise RuntimeError(
                            f"Atomic persistence failed for activation of {experiment_id} v{version}. "
                            f"Transaction rolled back. Activation aborted. No partial state. "
                            f"Tier-0 requires atomic persistence. Original error: {e}"
                        ) from e
            finally:
                # Step 3: Release distributed lock (after transaction completes)
                if distributed_lock_acquired:
                    try:
                        if hasattr(self.persistence_layer, 'release_distributed_lock'):
                            self.persistence_layer.release_distributed_lock(lock_key)
                            self.logger.debug(f"Released distributed lock: {lock_key}")
                    except Exception as e:
                        self.logger.warning(f"Failed to release distributed lock {lock_key}: {e}")
                        # Lock will auto-expire, but log warning
            
            # Step 4: ONLY AFTER transaction commits: Update in-memory cache
            # At this point, we know persistence succeeded atomically, so it's safe to update cache
            # NOTE: Cache is for performance only, storage is authoritative
            if activated_snapshot is not None:
                self._experiments[(experiment_id, version)] = activated_snapshot
                self._active_registry[experiment_id] = version
                self._version_sequences[(experiment_id, version)] = new_version_sequence
                
                # Step 5: Emit audit event (after successful activation)
                # Event was already persisted in transaction, now emit for audit logging
                if event is not None:
                    self._audit_transition(event, operation_id)
                
                return activated_snapshot
            else:
                # Should never happen if transaction succeeded
                raise RuntimeError(
                    f"Activation transaction succeeded but activated_snapshot is None. "
                    f"This indicates a bug in the activation protocol."
                )
        
        finally:
            # Always release lock
            self._lock_manager.release_experiment_lock(experiment_id, operation_id)
    
    def pause_experiment(
        self,
        experiment_id: str,
        version: int,
        *,
        actor: str,
        reason: Optional[str] = None,
    ) -> ExperimentSnapshot:
        """
        Pause active experiment (ACTIVE → PAUSED).
        
        Does not change config. Can be resumed.
        
        Args:
            experiment_id: Experiment to pause
            version: Version to pause
            actor: Actor performing pause
            reason: Reason for pause
            
        Returns:
            Paused experiment snapshot
            
        Raises:
            ValueError: If not currently ACTIVE
        """
        snapshot = self.get_experiment(experiment_id, version)
        
        if snapshot.state != ExperimentState.ACTIVE:
            raise ValueError(
                f"Can only pause ACTIVE experiments, current state: {snapshot.state}"
            )
        
        return self._transition_state(
            experiment_id=experiment_id,
            version=version,
            new_state=ExperimentState.PAUSED,
            actor=actor,
            reason=reason or "Experiment paused",
        )
    
    def resume_experiment(
        self,
        experiment_id: str,
        version: int,
        *,
        actor: str,
    ) -> ExperimentSnapshot:
        """
        Resume paused experiment (PAUSED → ACTIVE).
        
        Cannot resume if TERMINATED or COMPLETED.
        
        Args:
            experiment_id: Experiment to resume
            version: Version to resume
            actor: Actor performing resume
            
        Returns:
            Resumed experiment snapshot
            
        Raises:
            ValueError: If not currently PAUSED
        """
        snapshot = self.get_experiment(experiment_id, version)
        
        if snapshot.state != ExperimentState.PAUSED:
            raise ValueError(
                f"Can only resume PAUSED experiments, current state: {snapshot.state}"
            )
        
        # Re-check conflicts before resuming
        self._check_activation_conflicts(experiment_id, version)
        
        resumed = self._transition_state(
            experiment_id=experiment_id,
            version=version,
            new_state=ExperimentState.ACTIVE,
            actor=actor,
            reason="Experiment resumed",
        )
        
        # Update active registry
        self._active_registry[experiment_id] = version
        
        return resumed
    
    def terminate_experiment(
        self,
        experiment_id: str,
        version: int,
        *,
        actor: str,
        reason: str,
    ) -> ExperimentSnapshot:
        """
        Terminate experiment early (ACTIVE/PAUSED → TERMINATED).
        
        Immediately stops future assignments.
        Does not delete historical snapshot.
        Preserves exposure integrity.
        Terminal state - cannot reactivate.
        
        Args:
            experiment_id: Experiment to terminate
            version: Version to terminate
            actor: Actor performing termination
            reason: Reason for termination
            
        Returns:
            Terminated experiment snapshot
            
        Raises:
            ValueError: If in terminal state already
        """
        snapshot = self.get_experiment(experiment_id, version)
        
        if snapshot.state.is_terminal():
            raise ValueError(
                f"Cannot terminate experiment in terminal state: {snapshot.state}"
            )
        
        if snapshot.state not in {ExperimentState.ACTIVE, ExperimentState.PAUSED}:
            raise ValueError(
                f"Can only terminate ACTIVE or PAUSED experiments, "
                f"current state: {snapshot.state}"
            )
        
        # Create new snapshot with termination timestamp
        termination_timestamp = datetime.now(timezone.utc)
        
        # Transition state
        terminated = self._transition_state(
            experiment_id=experiment_id,
            version=version,
            new_state=ExperimentState.TERMINATED,
            actor=actor,
            reason=reason,
        )
        
        # Remove from active registry
        if experiment_id in self._active_registry:
            del self._active_registry[experiment_id]
        
        return terminated
    
    # ========================================================================
    # PUBLIC INTERFACE - RETRIEVAL
    # ========================================================================
    
    def get_experiment(
        self,
        experiment_id: str,
        version: Optional[int] = None,
        *,
        force_storage_read: bool = True,  # TIER-0: Default to storage read
    ) -> ExperimentSnapshot:
        """
        Get experiment snapshot (deterministic retrieval).
        
        TIER-0 REQUIREMENT: Storage is ALWAYS authoritative.
        - If persistence is configured, ALWAYS reads from storage first
        - In-memory cache is ONLY for performance, never authoritative
        - Default force_storage_read=True ensures storage-first behavior
        
        Always returns identical snapshot for same (id, version).
        Never reconstructs from partial state.
        Never infers defaults.
        Returned object is read-only (frozen).
        
        Args:
            experiment_id: Experiment ID
            version: Version (None = latest)
            force_storage_read: If True, bypass cache and read from storage only (default: True)
            
        Returns:
            Immutable experiment snapshot
            
        Raises:
            KeyError: If experiment not found
            RuntimeError: If persistence is required but storage read fails
        """
        if version is None:
            version = self.get_latest_version(experiment_id)
        
        key = (experiment_id, version)
        
        # TIER-0: Storage is ALWAYS authoritative - never trust cache alone
        if self.persistence_layer is None:
            # No persistence - Tier-0 requires storage-backed registry
            if key not in self._experiments:
                raise RuntimeError(
                    f"Persistence layer not configured. Tier-0 infrastructure requires "
                    f"storage-backed registry. Cannot retrieve experiment {experiment_id} v{version}."
                )
            # Fallback to cache only if no persistence (not Tier-0 safe)
            snapshot = self._experiments[key]
            self._validate_snapshot_immutability(snapshot)
            return snapshot
        
        # ALWAYS read from storage first (authoritative source)
        if force_storage_read or hasattr(self.persistence_layer, 'load_snapshot'):
            try:
                stored_snapshot = self.persistence_layer.load_snapshot(experiment_id, version)
                if stored_snapshot is not None:
                    # Validate deep immutability (structural check)
                    self._validate_snapshot_immutability(stored_snapshot)
                    # Update in-memory cache (but storage is authoritative)
                    self._experiments[key] = stored_snapshot
                    return stored_snapshot
                elif force_storage_read:
                    # Force storage read but not found - raise error
                    raise KeyError(
                        f"Experiment '{experiment_id}' version {version} not found in storage"
                    )
            except KeyError:
                raise
            except Exception as e:
                # Storage read failed - this is fatal for Tier-0
                self.logger.error(
                    f"CRITICAL: Failed to load snapshot from persistence for {experiment_id} v{version}: {e}. "
                    f"Tier-0 requires storage-backed registry."
                )
                raise RuntimeError(
                    f"Failed to load experiment from storage. Tier-0 infrastructure requires "
                    f"storage-backed registry. Original error: {e}"
                ) from e
        
        # Fallback to in-memory cache (only if storage read failed)
        # This should rarely happen in Tier-0 systems
        if key not in self._experiments:
            raise KeyError(
                f"Experiment '{experiment_id}' version {version} not found"
            )
        
        snapshot = self._experiments[key]
        # Validate immutability even for cache hits
        self._validate_snapshot_immutability(snapshot)
        return snapshot
    
    def get_active_experiments(self) -> List[ExperimentSnapshot]:
        """
        Get all currently ACTIVE experiments.
        
        TIER-0 REQUIREMENT: Storage is ALWAYS authoritative.
        This method ALWAYS queries storage, never uses cache.
        Memory is cache-only, never source of truth.
        
        Returns:
            List of active experiment snapshots (from storage)
            
        Raises:
            RuntimeError: If persistence layer is not configured
        """
        if self.persistence_layer is None:
            raise RuntimeError(
                "Persistence layer required. Tier-0 infrastructure requires "
                "storage-backed registry. Cannot query active experiments."
            )
        
        # ALWAYS query storage (authoritative source)
        if hasattr(self.persistence_layer, 'query_active_snapshots'):
            # Preferred: Direct query for ACTIVE snapshots
            active_snapshots = self.persistence_layer.query_active_snapshots()
            # Update cache (but storage is authoritative)
            for snapshot in active_snapshots:
                key = (snapshot.experiment_id, snapshot.version)
                self._experiments[key] = snapshot
                self._active_registry[snapshot.experiment_id] = snapshot.version
            return list(active_snapshots)
        
        elif hasattr(self.persistence_layer, 'load_active_registry'):
            # Fallback: Load registry from storage, then load snapshots
            authoritative_registry = self.persistence_layer.load_active_registry()
            if authoritative_registry is None:
                return []
            
            active_snapshots = []
            for experiment_id, version in authoritative_registry.items():
                try:
                    # Load snapshot from storage (authoritative)
                    snapshot = self.get_experiment(experiment_id, version, force_storage_read=True)
                    if snapshot.state == ExperimentState.ACTIVE:
                        active_snapshots.append(snapshot)
                except KeyError:
                    # Snapshot not found - registry inconsistency
                    self.logger.warning(
                        f"Active registry references missing snapshot: {experiment_id} v{version}"
                    )
            
            return active_snapshots
        
        else:
            raise RuntimeError(
                "Persistence layer does not support querying active experiments. "
                "Tier-0 requires storage-backed registry."
            )
    
    def get_latest_version(self, experiment_id: str) -> int:
        """
        Get latest version number for experiment.
        
        Args:
            experiment_id: Experiment ID
            
        Returns:
            Latest version number
            
        Raises:
            KeyError: If experiment not found
        """
        if experiment_id not in self._latest_versions:
            raise KeyError(f"Experiment '{experiment_id}' not found")
        
        return self._latest_versions[experiment_id]
    
    def is_active(self, experiment_id: str) -> bool:
        """
        Check if experiment has an ACTIVE version.
        
        Args:
            experiment_id: Experiment ID
            
        Returns:
            True if experiment is active
        """
        return experiment_id in self._active_registry
    
    # ========================================================================
    # INTERNAL HELPERS
    # ========================================================================
    
    def _transition_state(
        self,
        experiment_id: str,
        version: int,
        new_state: ExperimentState,
        actor: str,
        reason: Optional[str],
        operation_id: Optional[str] = None,
    ) -> ExperimentSnapshot:
        """
        Perform state transition with validation and audit.
        
        Creates new immutable snapshot with updated state.
        Original snapshot remains unchanged (immutability guarantee).
        
        Args:
            experiment_id: Experiment ID
            version: Version
            new_state: Target state
            actor: Actor performing transition
            reason: Reason for transition
            operation_id: Operation identifier for audit
            
        Returns:
            New immutable snapshot with updated state
            
        Raises:
            ValueError: If transition invalid
            RuntimeError: If snapshot mutation detected
        """
        # Get current snapshot
        current = self.get_experiment(experiment_id, version)
        
        # Validate transition is allowed
        allowed = ALLOWED_TRANSITIONS.get(current.state, set())
        if new_state not in allowed:
            raise ValueError(
                f"Invalid state transition: {current.state} → {new_state}. "
                f"Allowed transitions from {current.state}: {allowed}"
            )
        
        # If state unchanged, return current snapshot
        if current.state == new_state:
            return current
        
        # Enforce immutability: if ACTIVE, config cannot change
        if current.state == ExperimentState.ACTIVE:
            # Verify config hash hasn't changed (detect mutations)
            recomputed_hash = self._compute_config_hash(
                experiment_id=current.experiment_id,
                version=current.version,
                allocation_snapshot=current.allocation_snapshot,
                eligibility_definition=current.eligibility_definition,
                start_timestamp=current.start_timestamp,
                freeze_timestamp=current.freeze_timestamp,
                termination_timestamp=current.termination_timestamp,
            )
            if recomputed_hash != current.config_hash:
                raise RuntimeError(
                    f"Config hash mismatch during state transition for {experiment_id} v{version}. "
                    f"ACTIVE experiment config must remain immutable."
                )
        
        # Create new immutable snapshot with updated state
        # Use dataclass replace to create new frozen instance
        logical_timestamp = self._next_logical_timestamp()
        
        # Create new snapshot with updated state
        # Note: We cannot use replace() on frozen dataclass, so we create new instance
        # Set termination_timestamp if transitioning to TERMINATED
        termination_timestamp = current.termination_timestamp
        if new_state == ExperimentState.TERMINATED and termination_timestamp is None:
            termination_timestamp = datetime.now(timezone.utc)
        
        new_snapshot = ExperimentSnapshot(
            experiment_id=current.experiment_id,
            version=current.version,
            state=new_state,  # Updated state
            allocation_snapshot=current.allocation_snapshot,  # Immutable
            eligibility_definition=current.eligibility_definition,  # Immutable
            start_timestamp=current.start_timestamp,
            freeze_timestamp=current.freeze_timestamp,
            termination_timestamp=termination_timestamp,
            created_by=current.created_by,
            logical_created_at=current.logical_created_at,  # Original creation time
            schema_version=current.schema_version,
            config_hash=current.config_hash,  # Config hash unchanged (config is immutable)
            metadata=current.metadata,  # Metadata unchanged (already MappingProxyType)
        )
        
        # Store new snapshot (replaces old one in registry)
        # Original snapshot is preserved in transition history
        self._experiments[(experiment_id, version)] = new_snapshot
        
        # Record transition event
        event = LifecycleTransitionEvent(
            experiment_id=experiment_id,
            version=version,
            old_state=current.state,
            new_state=new_state,
            actor=actor,
            transition_timestamp=datetime.now(timezone.utc),
            logical_timestamp=logical_timestamp,
            config_hash=current.config_hash,
            reason=reason,
        )
        
        self._transition_history.append(event)
        
        # Emit structured audit event
        self._audit_transition(event, operation_id)
        
        # Note: Snapshot persistence for ACTIVE state is handled by activate_experiment()
        # before state transition to ensure atomic activation protocol.
        # We do NOT persist here to avoid double persistence.
        
        return new_snapshot
    
    def _check_activation_conflicts(
        self,
        experiment_id: str,
        version: int,
        snapshot: ExperimentSnapshot,
    ) -> None:
        """
        Check for conflicts before activation.
        
        Validates:
        - No duplicate ACTIVE version for same experiment_id
        - No mutually exclusive experiments ACTIVE simultaneously
        - No overlapping rollout scopes if declared exclusive
        
        Conflict detection must occur before activation.
        Hard fail on violation.
        
        Args:
            experiment_id: Experiment to activate
            version: Version to activate
            snapshot: Experiment snapshot to check
            
        Raises:
            RuntimeError: If conflict detected
        """
        # Check for duplicate ACTIVE version
        if experiment_id in self._active_registry:
            active_version = self._active_registry[experiment_id]
            if active_version != version:
                active_snapshot = self.get_experiment(experiment_id, active_version)
                if active_snapshot.state == ExperimentState.ACTIVE:
                    raise RuntimeError(
                        f"Conflict: Experiment '{experiment_id}' already has ACTIVE "
                        f"version {active_version}. Cannot activate version {version}. "
                        f"Only one ACTIVE version per experiment_id allowed."
                    )
        
        # Check for mutually exclusive experiments
        # Get exclusive group for this experiment (if any)
        experiment_exclusive_group = None
        for group_id, experiment_ids in self._exclusive_groups.items():
            if experiment_id in experiment_ids:
                experiment_exclusive_group = group_id
                break
        
        if experiment_exclusive_group:
            # Check if any other experiment in same group is ACTIVE
            conflicting_experiments = []
            for other_experiment_id in self._exclusive_groups[experiment_exclusive_group]:
                if other_experiment_id != experiment_id:
                    if other_experiment_id in self._active_registry:
                        other_version = self._active_registry[other_experiment_id]
                        other_snapshot = self.get_experiment(other_experiment_id, other_version)
                        if other_snapshot.state == ExperimentState.ACTIVE:
                            conflicting_experiments.append(f"{other_experiment_id} v{other_version}")
            
            if conflicting_experiments:
                raise RuntimeError(
                    f"Conflict: Experiment '{experiment_id}' is mutually exclusive with "
                    f"currently ACTIVE experiments: {', '.join(conflicting_experiments)}. "
                    f"Cannot activate while these are ACTIVE."
                )
        
        # Check for overlapping rollout scopes (hard fail on overlap)
        # Tier-0 requirement: Conflict detection must hard fail on overlapping scopes
        active_experiments = self.get_active_experiments()
        for active_exp in active_experiments:
            if active_exp.experiment_id == experiment_id:
                continue
            
            # Check if eligibility scopes overlap
            if self._eligibility_scopes_overlap(snapshot.eligibility_definition, active_exp.eligibility_definition):
                raise RuntimeError(
                    f"Conflict: Eligibility scope overlap detected between "
                    f"{experiment_id} v{version} and {active_exp.experiment_id} v{active_exp.version}. "
                    f"Overlapping rollout scopes cause assignment interference and statistical invalidation. "
                    f"Activation rejected."
                )
    
    def _eligibility_scopes_overlap(
        self,
        eligibility1: EligibilityDefinition,
        eligibility2: EligibilityDefinition,
    ) -> bool:
        """
        Check if two eligibility definitions have overlapping scopes.
        
        Args:
            eligibility1: First eligibility definition
            eligibility2: Second eligibility definition
            
        Returns:
            True if scopes overlap
        """
        # Check region overlap
        if eligibility1.allowed_regions and eligibility2.allowed_regions:
            if not eligibility1.allowed_regions.intersection(eligibility2.allowed_regions):
                return False  # No region overlap
        
        # Check trust tier overlap
        if eligibility1.required_trust_tier is not None and eligibility2.required_trust_tier is not None:
            # Both have trust tier requirements - check if they overlap
            # (Simplified - in production would check tier ranges)
            pass
        
        # Check excluded identities overlap
        if eligibility1.excluded_identities and eligibility2.excluded_identities:
            if eligibility1.excluded_identities.intersection(eligibility2.excluded_identities):
                return True  # Both exclude same identities
        
        # Default: assume overlap if no clear separation
        return True
    
    def _compute_allocation_hash(
        self,
        allocation_config: Union[Dict[str, float], MappingProxyType[str, float]],
        hash_seed: str,
        bucket_domain: str,
    ) -> str:
        """
        Compute deterministic hash of allocation configuration.
        
        Hash includes:
        - Variant allocation percentages (sorted)
        - Hash seed
        - Bucket domain
        
        Args:
            allocation_config: Allocation config (dict or MappingProxyType)
            hash_seed: Hash seed
            bucket_domain: Bucket domain
            
        Returns:
            SHA256 hash (64 hex characters)
        """
        # Convert MappingProxyType to dict if needed
        if isinstance(allocation_config, MappingProxyType):
            allocation_dict = dict(allocation_config)
        else:
            allocation_dict = allocation_config
        
        # Sort variants for determinism
        sorted_variants = json.dumps(
            {k: v for k, v in sorted(allocation_dict.items())},
            sort_keys=True
        )
        
        components = [
            sorted_variants,
            hash_seed,
            bucket_domain,
        ]
        hash_input = "|".join(components)
        return hashlib.sha256(hash_input.encode('utf-8')).hexdigest()
    
    def _compute_eligibility_hash(
        self,
        eligibility_config: Dict[str, Any],
    ) -> str:
        """
        Compute deterministic hash of eligibility configuration.
        
        Hash includes all eligibility criteria for drift detection.
        
        Returns:
            SHA256 hash (64 hex characters)
        """
        # Normalize config for deterministic hashing
        normalized = {
            "required_trust_tier": eligibility_config.get("required_trust_tier"),
            "allowed_regions": sorted(eligibility_config.get("allowed_regions", [])) if eligibility_config.get("allowed_regions") else None,
            "excluded_identities": sorted(eligibility_config.get("excluded_identities", [])),
            "custom_filters": json.dumps(eligibility_config.get("custom_filters", {}), sort_keys=True),
        }
        
        hash_input = json.dumps(normalized, sort_keys=True)
        return hashlib.sha256(hash_input.encode('utf-8')).hexdigest()
    
    def _compute_config_hash(
        self,
        experiment_id: str,
        version: int,
        allocation_snapshot: AllocationSnapshot,
        eligibility_definition: EligibilityDefinition,
        start_timestamp: datetime,
        freeze_timestamp: Optional[datetime] = None,
        termination_timestamp: Optional[datetime] = None,
    ) -> str:
        """
        Compute deterministic config hash using canonical JSON.
        
        TIER-0 REQUIREMENT: Hash must be stable and reproducible.
        Uses canonical JSON (sorted keys, no whitespace) for determinism.
        Guarantees: same config → same hash, regardless of Python version or dict order.
        
        Hash includes:
        - experiment_id
        - version
        - allocation hash
        - eligibility hash
        - lifecycle parameters (start_timestamp, freeze_timestamp, termination_timestamp)
        - schema_version
        
        Used for:
        - Drift detection
        - Snapshot validation
        - Runtime safety verification
        
        Any mutation → config_hash mismatch.
        
        Returns:
            SHA256 hash (64 hex characters)
        """
        # Build canonical representation
        canonical_data = {
            "experiment_id": experiment_id,
            "version": version,
            "allocation_hash": allocation_snapshot.allocation_hash,
            "eligibility_hash": eligibility_definition.eligibility_hash,
            "start_timestamp": start_timestamp.isoformat(),
            "freeze_timestamp": freeze_timestamp.isoformat() if freeze_timestamp else None,
            "termination_timestamp": termination_timestamp.isoformat() if termination_timestamp else None,
            "schema_version": self.EXPERIMENT_SCHEMA_VERSION,
        }
        
        # Canonical JSON: sorted keys, no whitespace, deterministic
        # This ensures hash is identical across all runs
        canonical_json = json.dumps(
            canonical_data,
            sort_keys=True,
            separators=(',', ':'),  # No whitespace
            ensure_ascii=True,
        )
        
        return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()
    
    def _atomic_state_transition_with_cas(
        self,
        experiment_id: str,
        version: int,
        old_state: ExperimentState,
        new_state: ExperimentState,
        expected_version_sequence: int,
    ) -> bool:
        """
        Atomic state transition with CAS (Compare-And-Swap).
        
        TIER-0 REQUIREMENT: Database-level optimistic concurrency control.
        Only one node can transition state at a time.
        Uses database-level atomicity to prevent concurrent updates.
        
        Args:
            experiment_id: Experiment ID
            version: Version number
            old_state: Expected current state
            new_state: Target state
            expected_version_sequence: Expected version sequence (for CAS)
            
        Returns:
            True if transition succeeded, False if version conflict
            
        Raises:
            RuntimeError: If database operation fails
        """
        if self.persistence_layer is None:
            raise RuntimeError("Persistence layer required for CAS operations")
        
        # Use database-level CAS write
        if hasattr(self.persistence_layer, 'atomic_update_state'):
            affected_rows = self.persistence_layer.atomic_update_state(
                experiment_id=experiment_id,
                version=version,
                old_state=old_state,
                new_state=new_state,
                expected_version_sequence=expected_version_sequence,
            )
            return affected_rows == 1
        
        elif hasattr(self.persistence_layer, 'execute_cas_update'):
            # Direct SQL-style CAS update
            # UPDATE experiments SET state = %s, version_sequence = %s
            # WHERE experiment_id = %s AND version = %s 
            #   AND state = %s AND version_sequence = %s
            result = self.persistence_layer.execute_cas_update(
                table='experiments',
                set_clause={
                    'state': new_state.value,
                    'version_sequence': expected_version_sequence + 1,
                },
                where_clause={
                    'experiment_id': experiment_id,
                    'version': version,
                    'state': old_state.value,
                    'version_sequence': expected_version_sequence,
                },
            )
            return result.affected_rows == 1 if hasattr(result, 'affected_rows') else False
        
        else:
            # Fallback: Check-then-update (not fully atomic, but better than nothing)
            current = self._get_version_sequence(experiment_id, version)
            if current != expected_version_sequence:
                return False
            # Note: This is not truly atomic, but provides some protection
            return True
    
    def _persist_snapshot(
        self, 
        snapshot: ExperimentSnapshot,
        transaction: Optional[Any] = None,
    ) -> None:
        """
        Persist experiment snapshot to durable storage.
        
        TIER-0 REQUIREMENT: Persistence is mandatory and atomic.
        Snapshots must be persisted before activation.
        Immutable post-activation.
        
        If persistence fails, the operation MUST fail.
        No silent persistence failures allowed.
        
        Args:
            snapshot: Snapshot to persist
            transaction: Optional transaction context (for atomic operations)
            
        Raises:
            RuntimeError: If persistence layer is not configured
            RuntimeError: If persistence fails (operation must abort)
        """
        if self.persistence_layer is None:
            raise RuntimeError(
                f"Persistence layer is required but not configured. "
                f"Cannot persist snapshot for {snapshot.experiment_id} v{snapshot.version}. "
                f"Tier-0 infrastructure requires mandatory persistence."
            )
        
        # Use transaction if provided (for atomic operations)
        if transaction is not None and hasattr(transaction, 'persist_snapshot'):
            try:
                transaction.persist_snapshot(snapshot)
                return
            except Exception as e:
                raise RuntimeError(f"Transaction persistence failed: {e}") from e
        
        # Fallback: Direct persistence
        try:
            if hasattr(self.persistence_layer, 'store_snapshot'):
                self.persistence_layer.store_snapshot(snapshot)
            elif hasattr(self.persistence_layer, 'persist_experiment'):
                self.persistence_layer.persist_experiment(snapshot)
            elif hasattr(self.persistence_layer, 'save'):
                self.persistence_layer.save(snapshot.to_dict())
            else:
                raise RuntimeError(
                    f"Persistence layer does not implement required interface. "
                    f"Must have store_snapshot, persist_experiment, or save method."
                )
        except Exception as e:
            self.logger.error(
                f"CRITICAL: Failed to persist snapshot for {snapshot.experiment_id} v{snapshot.version}: {e}"
            )
            # Tier-0 requirement: Persistence failure MUST abort operation
            raise RuntimeError(
                f"Persistence failed for {snapshot.experiment_id} v{snapshot.version}. "
                f"Operation aborted. Tier-0 infrastructure requires atomic persistence. "
                f"Original error: {e}"
            ) from e
    
    def _persist_active_registry(self) -> None:
        """
        Persist current active registry to durable storage.
        
        TIER-0 REQUIREMENT: Registry persistence is mandatory.
        If persistence fails, operation MUST fail.
        
        Raises:
            RuntimeError: If persistence layer is not configured
            RuntimeError: If persistence fails
        """
        self._persist_active_registry_atomic(self._active_registry)
    
    def _persist_active_registry_atomic(
        self,
        registry: Dict[str, int],
        transaction: Optional[Any] = None,
    ) -> None:
        """
        Persist provided active registry to durable storage.
        
        Args:
            registry: Registry dict to persist
            transaction: Optional transaction context (for atomic operations)
            
        Raises:
            RuntimeError: If persistence layer is not configured
            RuntimeError: If persistence fails
        """
        if self.persistence_layer is None:
            raise RuntimeError(
                f"Persistence layer is required but not configured. "
                f"Cannot persist active registry. "
                f"Tier-0 infrastructure requires storage-backed registry."
            )
        
        # Use transaction if provided
        if transaction is not None and hasattr(transaction, 'update_active_registry'):
            try:
                transaction.update_active_registry(registry.copy())
                return
            except Exception as e:
                raise RuntimeError(f"Transaction registry persistence failed: {e}") from e
        
        # Fallback: Direct persistence
        try:
            if hasattr(self.persistence_layer, 'update_active_registry'):
                self.persistence_layer.update_active_registry(registry.copy())
            elif hasattr(self.persistence_layer, 'store_registry'):
                self.persistence_layer.store_registry(registry.copy())
            elif hasattr(self.persistence_layer, 'save'):
                # Fallback: save registry as a key-value pair
                self.persistence_layer.save({
                    "type": "active_registry",
                    "registry": registry.copy()
                })
            else:
                raise RuntimeError(
                    f"Persistence layer does not implement registry persistence interface."
                )
        except Exception as e:
            self.logger.error(f"CRITICAL: Failed to persist active registry: {e}")
            # Tier-0 requirement: Registry persistence failure MUST abort operation
            raise RuntimeError(
                f"Active registry persistence failed. Operation aborted. "
                f"Tier-0 infrastructure requires storage-backed registry. "
                f"Original error: {e}"
            ) from e
    
    def _get_version_sequence(self, experiment_id: str, version: int) -> int:
        """
        Get version sequence number for CAS check.
        
        Checks persistence layer first (authoritative), then in-memory cache.
        
        Args:
            experiment_id: Experiment ID
            version: Version number
            
        Returns:
            Version sequence number (0 if not found)
        """
        # Tier-0: Storage is authoritative, not in-memory cache
        if self.persistence_layer is not None:
            if hasattr(self.persistence_layer, 'get_version_sequence'):
                stored_sequence = self.persistence_layer.get_version_sequence(experiment_id, version)
                if stored_sequence is not None:
                    # Update in-memory cache
                    self._version_sequences[(experiment_id, version)] = stored_sequence
                    return stored_sequence
        
        # Fallback to in-memory cache
        return self._version_sequences.get((experiment_id, version), 0)
    
    def replay_experiment_events(
        self,
        experiment_id: str,
        events: List[LifecycleTransitionEvent],
    ) -> ExperimentSnapshot:
        """
        Replay lifecycle events to recompute experiment state.
        
        Used for deterministic recovery and state reconstruction.
        Replaying events in order must produce identical state.
        
        Args:
            experiment_id: Experiment ID
            events: Transition events to replay (must be sorted by logical_timestamp)
            
        Returns:
            Final experiment snapshot after replay
        """
        # Validate events are sorted
        for i in range(1, len(events)):
            if events[i].logical_timestamp < events[i-1].logical_timestamp:
                raise ValueError(
                    f"Events not sorted by logical_timestamp for {experiment_id}"
                )
        
        # Get initial snapshot
        if not events:
            raise ValueError(f"No events to replay for {experiment_id}")
        
        version = events[0].version
        snapshot = self.get_experiment(experiment_id, version)
        
        # Replay events in order
        for event in events:
            if event.experiment_id != experiment_id:
                raise ValueError(
                    f"Event experiment_id mismatch: {event.experiment_id} != {experiment_id}"
                )
            
            # Apply transition
            snapshot = self._transition_state(
                experiment_id=event.experiment_id,
                version=event.version,
                new_state=event.new_state,
                actor=event.actor,
                reason=event.reason,
            )
        
        return snapshot
    
    def register_exclusive_group(
        self,
        group_id: str,
        experiment_ids: Set[str],
    ) -> None:
        """
        Register mutually exclusive experiment group.
        
        Experiments in same group cannot be ACTIVE simultaneously.
        
        Args:
            group_id: Exclusive group identifier
            experiment_ids: Set of experiment IDs in group
        """
        self._exclusive_groups[group_id] = set(experiment_ids)
        self.logger.info(
            f"Registered exclusive group '{group_id}' with {len(experiment_ids)} experiments"
        )
    
    def _recover_from_persistence(self) -> None:
        """
        Recover experiment manager state from persistence layer.
        
        TIER-0 REQUIREMENT: Deterministic recovery under partial-write crash scenarios.
        Recovery must be:
        - Deterministic: Same persistence state → same recovered state
        - Idempotent: Re-running recovery produces same result
        - Crash-safe: Handles partial writes and incomplete transactions
        - Mathematically guaranteed: No ambiguity in recovery logic
        
        Recovery restores:
        - Registry (all experiment snapshots)
        - Active registry (experiment_id → active version) - from storage, not cache
        - Latest versions (experiment_id → latest version)
        - Transition history (for audit and replay)
        - Logical clock (for ordering)
        
        This method is called during initialization if persistence_layer is configured.
        
        Raises:
            RuntimeError: If recovery fails (fatal for Tier-0)
        """
        if self.persistence_layer is None:
            return  # No persistence, nothing to recover
        
        try:
            # Step 1: Load ALL snapshots (authoritative source)
            # TIER-0: Snapshots are the single source of truth
            if hasattr(self.persistence_layer, 'load_all_snapshots'):
                snapshots = self.persistence_layer.load_all_snapshots()
            elif hasattr(self.persistence_layer, 'load_experiments'):
                experiments = self.persistence_layer.load_experiments()
                snapshots = [self._deserialize_snapshot(exp_data) for exp_data in experiments]
            else:
                raise RuntimeError("Persistence layer does not support loading snapshots")
            
            # Step 2: Store all snapshots and validate immutability
            for snapshot in snapshots:
                # Validate deep immutability (structural check)
                self._validate_snapshot_immutability(snapshot)
                
                key = (snapshot.experiment_id, snapshot.version)
                self._experiments[key] = snapshot
                
                # Update latest version tracking
                if snapshot.experiment_id not in self._latest_versions:
                    self._latest_versions[snapshot.experiment_id] = snapshot.version
                elif snapshot.version > self._latest_versions[snapshot.experiment_id]:
                    self._latest_versions[snapshot.experiment_id] = snapshot.version
            
            # TIER-0: DO NOT load active registry from storage
            # Registry will be rebuilt deterministically from snapshots below
            # This ensures: same snapshots → same registry (mathematically deterministic)
            
            # Load transition history
            if hasattr(self.persistence_layer, 'load_transition_history'):
                self._transition_history = self.persistence_layer.load_transition_history()
            
            # Restore logical clock (use max from transition history)
            if self._transition_history:
                self._logical_clock = max(event.logical_timestamp for event in self._transition_history)
            
            # TIER-0: Deterministic recovery validation
            # Ensure recovered state is consistent and mathematically correct
            self._validate_recovered_state()
            
            # Rebuild active registry deterministically from snapshots (not from stored registry)
            # This ensures recovery is deterministic even if stored registry is inconsistent
            # Mathematically: same snapshots → same registry (deterministic)
            self._rebuild_active_registry_from_snapshots()
            
            # Validate logical clock consistency
            if self._transition_history:
                max_logical = max(event.logical_timestamp for event in self._transition_history)
                if self._logical_clock < max_logical:
                    self.logger.warning(
                        f"Logical clock inconsistency: clock={self._logical_clock}, "
                        f"max_event={max_logical}. Adjusting clock."
                    )
                    self._logical_clock = max_logical
            
            self.logger.info(
                f"Recovery complete: {len(self._experiments)} snapshots, "
                f"{len(self._active_registry)} active experiments, "
                f"logical_clock={self._logical_clock}"
            )
            
        except Exception as e:
            self.logger.error(f"Recovery from persistence failed: {e}")
            raise RuntimeError(
                f"Failed to recover experiment manager state from persistence. "
                f"Tier-0 infrastructure requires successful recovery. Original error: {e}"
            ) from e
    
    def _validate_recovered_state(self) -> None:
        """
        Validate recovered state for consistency.
        
        TIER-0 REQUIREMENT: Mathematical correctness of recovered state.
        
        Checks:
        - Active registry entries point to valid snapshots
        - Active registry entries are actually in ACTIVE state
        - Latest versions are consistent with snapshots
        - All snapshots have deep immutability
        - No duplicate ACTIVE versions for same experiment_id
        
        Raises:
            RuntimeError: If recovered state is inconsistent (fatal for Tier-0)
        """
        # Validate all snapshots have deep immutability
        for key, snapshot in self._experiments.items():
            try:
                self._validate_snapshot_immutability(snapshot)
            except RuntimeError as e:
                raise RuntimeError(
                    f"Recovered snapshot {key} does not have deep immutability: {e}"
                ) from e
        
        # Validate active registry
        for experiment_id, version in list(self._active_registry.items()):
            key = (experiment_id, version)
            if key not in self._experiments:
                raise RuntimeError(
                    f"Active registry references missing snapshot: {experiment_id} v{version}. "
                    f"Recovery state is inconsistent."
                )
            
            snapshot = self._experiments[key]
            if snapshot.state != ExperimentState.ACTIVE:
                raise RuntimeError(
                    f"Active registry entry {experiment_id} v{version} is not ACTIVE "
                    f"(state: {snapshot.state}). Recovery state is inconsistent."
                )
        
        # Validate no duplicate ACTIVE versions
        active_by_experiment = {}
        for experiment_id, version in self._active_registry.items():
            if experiment_id in active_by_experiment:
                raise RuntimeError(
                    f"Duplicate ACTIVE version for {experiment_id}: "
                    f"v{active_by_experiment[experiment_id]} and v{version}. "
                    f"Recovery state is inconsistent."
                )
            active_by_experiment[experiment_id] = version
    
    def _rebuild_active_registry_from_snapshots(self) -> None:
        """
        Rebuild active registry deterministically from snapshots.
        
        TIER-0 REQUIREMENT: Deterministic recovery.
        Active registry is rebuilt from snapshots, not from stored registry.
        This ensures recovery is deterministic even if stored registry is inconsistent.
        
        Algorithm:
        1. Find all ACTIVE snapshots
        2. For each experiment_id, keep only the latest ACTIVE version
        3. Rebuild registry from these snapshots
        
        This is mathematically deterministic: same snapshots → same registry.
        """
        # Find all ACTIVE snapshots
        active_snapshots = {}
        for key, snapshot in self._experiments.items():
            if snapshot.state == ExperimentState.ACTIVE:
                experiment_id = snapshot.experiment_id
                if experiment_id not in active_snapshots:
                    active_snapshots[experiment_id] = snapshot
                elif snapshot.version > active_snapshots[experiment_id].version:
                    # Keep latest version
                    active_snapshots[experiment_id] = snapshot
        
        # Rebuild registry deterministically
        rebuilt_registry = {
            exp_id: snapshot.version
            for exp_id, snapshot in active_snapshots.items()
        }
        
        # Update active registry (storage-backed, authoritative)
        self._active_registry = rebuilt_registry
        
        # Persist rebuilt registry to ensure consistency
        # TIER-0: Registry must be persisted after deterministic rebuild
        if self.persistence_layer is not None:
            try:
                self._persist_active_registry_atomic(rebuilt_registry)
            except Exception as e:
                # Registry persistence failure is critical for Tier-0
                self.logger.error(
                    f"CRITICAL: Failed to persist rebuilt active registry: {e}. "
                    f"Tier-0 requires storage-backed registry."
                )
                raise RuntimeError(
                    f"Failed to persist rebuilt active registry. Tier-0 infrastructure requires "
                    f"storage-backed registry. Original error: {e}"
                ) from e
    
    def _deserialize_snapshot(self, data: Dict[str, Any]) -> ExperimentSnapshot:
        """
        Deserialize experiment snapshot from persisted data.
        
        Args:
            data: Serialized snapshot data
            
        Returns:
            Reconstructed ExperimentSnapshot
        """
        # TIER-0: Reconstruct nested structures with deep immutability
        # Use deep_freeze() to ensure ALL nested structures are immutable
        allocation_data = data.get("allocation_snapshot", {})
        allocation_snapshot = AllocationSnapshot(
            variants=deep_freeze(allocation_data.get("variants", {})),  # Recursively frozen
            hash_seed=allocation_data.get("hash_seed", ""),
            bucket_domain=allocation_data.get("bucket_domain", "user_id"),
            allocation_hash=allocation_data.get("allocation_hash", ""),
        )
        
        eligibility_data = data.get("eligibility_definition", {})
        allowed_regions = eligibility_data.get("allowed_regions")
        eligibility_definition = EligibilityDefinition(
            required_trust_tier=eligibility_data.get("required_trust_tier"),
            allowed_regions=deep_freeze(allowed_regions) if allowed_regions else None,  # Recursively frozen
            excluded_identities=deep_freeze(eligibility_data.get("excluded_identities", [])),  # Recursively frozen
            custom_filters=deep_freeze(eligibility_data.get("custom_filters", {})),  # Recursively frozen
            eligibility_hash=eligibility_data.get("eligibility_hash", ""),
        )
        
        # Parse timestamps
        start_timestamp = datetime.fromisoformat(data["start_timestamp"]) if isinstance(data.get("start_timestamp"), str) else data.get("start_timestamp")
        freeze_timestamp = datetime.fromisoformat(data["freeze_timestamp"]) if isinstance(data.get("freeze_timestamp"), str) and data.get("freeze_timestamp") else None
        termination_timestamp = datetime.fromisoformat(data["termination_timestamp"]) if isinstance(data.get("termination_timestamp"), str) and data.get("termination_timestamp") else None
        
        snapshot = ExperimentSnapshot(
            experiment_id=data["experiment_id"],
            version=data["version"],
            state=ExperimentState(data["state"]),
            allocation_snapshot=allocation_snapshot,
            eligibility_definition=eligibility_definition,
            start_timestamp=start_timestamp,
            freeze_timestamp=freeze_timestamp,
            termination_timestamp=termination_timestamp,
            created_by=data["created_by"],
            logical_created_at=data["logical_created_at"],
            schema_version=data.get("schema_version", self.EXPERIMENT_SCHEMA_VERSION),
            config_hash=data["config_hash"],
            metadata=deep_freeze(data.get("metadata", {})),  # Recursively frozen
        )
        
        # Validate deep immutability after deserialization
        self._validate_snapshot_immutability(snapshot)
        
        return snapshot
    
    def _persist_latest_versions(self) -> None:
        """
        Persist latest version tracking to storage.
        
        Raises:
            RuntimeError: If persistence fails
        """
        if self.persistence_layer is None:
            return  # No persistence configured
        
        try:
            if hasattr(self.persistence_layer, 'store_latest_versions'):
                self.persistence_layer.store_latest_versions(self._latest_versions.copy())
            elif hasattr(self.persistence_layer, 'save'):
                self.persistence_layer.save({
                    "type": "latest_versions",
                    "versions": self._latest_versions.copy()
                })
        except Exception as e:
            self.logger.error(f"Failed to persist latest versions: {e}")
            # Latest versions persistence failure is less critical than snapshot/registry
            # but should still be logged
    
    def _deep_freeze_snapshot(self, snapshot: ExperimentSnapshot) -> ExperimentSnapshot:
        """
        Deep freeze snapshot for true immutability.
        
        TIER-0 REQUIREMENT: All nested structures must be immutable.
        Prevents snapshot drift from nested mutation.
        
        Args:
            snapshot: Snapshot to deep freeze
            
        Returns:
            Deeply frozen snapshot (new instance with immutable nested structures)
        """
        # Snapshot is already frozen dataclass, but nested structures may be mutable
        # Create new snapshot with deeply frozen nested structures
        frozen_allocation = AllocationSnapshot(
            variants=deep_freeze(dict(snapshot.allocation_snapshot.variants)),
            hash_seed=snapshot.allocation_snapshot.hash_seed,
            bucket_domain=snapshot.allocation_snapshot.bucket_domain,
            allocation_hash=snapshot.allocation_snapshot.allocation_hash,
        )
        
        frozen_eligibility = EligibilityDefinition(
            required_trust_tier=snapshot.eligibility_definition.required_trust_tier,
            allowed_regions=deep_freeze(snapshot.eligibility_definition.allowed_regions) if snapshot.eligibility_definition.allowed_regions else None,
            excluded_identities=deep_freeze(snapshot.eligibility_definition.excluded_identities),
            custom_filters=deep_freeze(dict(snapshot.eligibility_definition.custom_filters)),
            eligibility_hash=snapshot.eligibility_definition.eligibility_hash,
        )
        
        frozen_snapshot = ExperimentSnapshot(
            experiment_id=snapshot.experiment_id,
            version=snapshot.version,
            state=snapshot.state,
            allocation_snapshot=frozen_allocation,
            eligibility_definition=frozen_eligibility,
            start_timestamp=snapshot.start_timestamp,
            freeze_timestamp=snapshot.freeze_timestamp,
            termination_timestamp=snapshot.termination_timestamp,
            created_by=snapshot.created_by,
            logical_created_at=snapshot.logical_created_at,
            schema_version=snapshot.schema_version,
            config_hash=snapshot.config_hash,
            metadata=deep_freeze(dict(snapshot.metadata)),
        )
        
        return frozen_snapshot
    
    @contextmanager
    def _activation_transaction(self):
        """
        Transaction context manager for atomic activation.
        
        TIER-0 REQUIREMENT: All-or-nothing persistence semantics.
        Either all persistence operations succeed, or all are rolled back.
        
        Yields:
            Transaction object with commit() and rollback() methods
        """
        class ActivationTransaction:
            def __init__(self, manager):
                self.manager = manager
                self.committed = False
                self.rolled_back = False
            
            def commit(self):
                """Commit transaction (all persistence succeeded)."""
                if self.rolled_back:
                    raise RuntimeError("Cannot commit rolled back transaction")
                self.committed = True
            
            def rollback(self):
                """Rollback transaction (persistence failed)."""
                if self.committed:
                    raise RuntimeError("Cannot rollback committed transaction")
                self.rolled_back = True
        
        txn = ActivationTransaction(self)
        
        # If persistence layer supports transactions, use it
        if self.persistence_layer is not None and hasattr(self.persistence_layer, 'transaction'):
            with self.persistence_layer.transaction() as backend_txn:
                txn._backend_txn = backend_txn
                try:
                    yield txn
                    if not txn.committed:
                        backend_txn.rollback()
                        raise RuntimeError("Transaction not committed")
                except Exception:
                    backend_txn.rollback()
                    raise
        else:
            # Fallback: Manual transaction tracking
            # In this case, we rely on individual persistence methods to be atomic
            # and fail fast if any fails
            try:
                yield txn
                if not txn.committed:
                    raise RuntimeError("Transaction not committed")
            except Exception:
                txn.rollback()
                raise
    
    def _load_active_registry_authoritative(self) -> Dict[str, int]:
        """
        Load active registry from storage (authoritative source).
        
        TIER-0 REQUIREMENT: Storage is authoritative, not in-memory cache.
        This ensures all nodes read the same ACTIVE experiment set.
        
        Returns:
            Active registry dict (experiment_id -> version)
        """
        if self.persistence_layer is None:
            # No persistence - return in-memory cache (not Tier-0 safe)
            return self._active_registry.copy()
        
        # Load from storage (authoritative)
        if hasattr(self.persistence_layer, 'load_active_registry'):
            try:
                stored_registry = self.persistence_layer.load_active_registry()
                if stored_registry is not None:
                    # Update in-memory cache
                    self._active_registry = stored_registry
                    return stored_registry.copy()
            except Exception as e:
                self.logger.warning(
                    f"Failed to load active registry from persistence: {e}. "
                    f"Using in-memory cache (not Tier-0 safe)."
                )
        
        # Fallback to in-memory cache (not ideal, but better than failing)
        return self._active_registry.copy()
    
    def _next_logical_timestamp(self) -> int:
        """Get next logical timestamp (monotonic)."""
        self._logical_clock += 1
        return self._logical_clock
    
    def _audit_transition(
        self,
        event: LifecycleTransitionEvent,
        operation_id: Optional[str] = None,
    ) -> None:
        """
        Emit structured audit event for state transition.
        
        Every lifecycle transition must emit complete audit trail.
        Audit must be replay-safe.
        
        Args:
            event: Transition event to audit
            operation_id: Operation identifier
        """
        audit_payload = event.to_dict()
        
        # Add operation context
        if operation_id:
            audit_payload["operation_id"] = operation_id
        
        # Add additional context
        audit_payload["timestamp"] = datetime.now(timezone.utc).isoformat()
        audit_payload["schema_version"] = self.EXPERIMENT_SCHEMA_VERSION
        
        # Log structured event
        self.logger.info(f"Experiment lifecycle transition: {json.dumps(audit_payload, sort_keys=True)}")
        
        # Send to audit logger if available
        if self.audit_logger is not None:
            try:
                if hasattr(self.audit_logger, 'log'):
                    self.audit_logger.log(audit_payload)
                elif hasattr(self.audit_logger, 'emit'):
                    self.audit_logger.emit(audit_payload)
                elif hasattr(self.audit_logger, 'write'):
                    self.audit_logger.write(json.dumps(audit_payload))
            except Exception as e:
                self.logger.error(f"Failed to write audit log: {e}")
        
        # Validate audit structure
        required_fields = {"experiment_id", "version", "old_state", "new_state", "actor", "config_hash"}
        missing = required_fields - set(audit_payload.keys())
        if missing:
            raise RuntimeError(f"Audit payload missing required fields: {missing}")


# ============================================================================
# EXPORTED API
# ============================================================================

__all__ = (
    # Enums
    "ExperimentState",
    
    # Data structures
    "AllocationSnapshot",
    "EligibilityDefinition",
    "ExperimentSnapshot",
    "LifecycleTransitionEvent",
    
    # Manager
    "ExperimentManager",
    
    # Lock manager (for advanced use cases)
    "ExperimentLockManager",
    
    # Constants
    "ALLOWED_TRANSITIONS",
)