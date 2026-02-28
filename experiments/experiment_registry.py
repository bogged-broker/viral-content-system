"""
/experiments/experiment_registry.py

PRODUCTION-GRADE EXPERIMENT REGISTRY SYSTEM
Single authoritative source of truth for all experiments.

FULL MAXIMUM SPEC (240k-LOC SYSTEM, NO BS)

This file enforces:
- Global uniqueness (ID + version composite key)
- Immutability of registered experiments
- Collision prevention (variables, traffic, platforms, hypotheses, rollout)
- State transition safety (one-way, terminal states enforced)
- Auditability (legally auditable, immutable logs)
- Runtime integrity validation
- Spec drift detection
- Version tracking and history

NO EXPERIMENT RUNS WITHOUT REGISTRY APPROVAL.

This file is a governor, not a helper.
Nothing experimental touches production unless this registry says yes.
"""

import hashlib
import json
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Callable, Set, Dict, List, Tuple
from functools import lru_cache

from experiment_spec import (
    ExperimentSpec,
    ExperimentSpecValidator,
    AssignmentUnit,
    RiskLevel,
)


# ============================================================================
# ENUMS (STRICT STATE MACHINE)
# ============================================================================

class ExperimentState(Enum):
    """
    Experiment lifecycle states.
    
    TRANSITIONS ARE ONE-WAY (see StateTransitionGuard).
    """
    DRAFT = "draft"
    REGISTERED = "registered"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABORTED = "aborted"
    BANNED = "banned"


class ExperimentVisibility(Enum):
    """Controls who can see the experiment."""
    INTERNAL = "internal"
    TEAM = "team"
    PUBLIC = "public"


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class ExperimentRecord:
    """
    Immutable record of a registered experiment.
    
    Once created, this NEVER changes.
    State transitions create new records with same spec but new state.
    """
    spec: ExperimentSpec
    state: ExperimentState
    registered_at: datetime
    
    # Integrity
    hash_signature: str  # cryptographic hash of spec
    version_hash: str  # hash of (experiment_id, version) for uniqueness
    
    # Conflict detection
    compatibility_tags: frozenset[str] = field(default_factory=frozenset)
    
    # Lifecycle tracking
    activated_at: Optional[datetime] = None
    paused_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    aborted_at: Optional[datetime] = None
    
    # Convenience
    active: bool = False
    
    # Metadata
    visibility: ExperimentVisibility = ExperimentVisibility.INTERNAL
    notes: str = ""


@dataclass(frozen=True)
class AuditLogEntry:
    """
    Immutable audit trail entry.
    
    LEGALLY AUDITABLE.
    """
    timestamp: datetime
    experiment_id: str
    action: str
    actor: str
    old_state: Optional[ExperimentState]
    new_state: ExperimentState
    hash_signature: str
    reason: str


@dataclass(frozen=True)
class StateTransition:
    """Represents a valid state transition."""
    from_state: ExperimentState
    to_state: ExperimentState
    requires_validation: bool = False


# ============================================================================
# VALIDATORS
# ============================================================================

class CollisionValidator:
    """
    Detects experiment collisions.
    
    SILENT COLLISIONS = INVALID SYSTEM.
    
    Validates:
    - Variable mutations
    - Traffic isolation
    - Platform conflicts
    - Hypothesis incompatibility
    - Rollout stage conflicts
    """
    
    def validate_no_collisions(
        self,
        new_spec: ExperimentSpec,
        active_records: list[ExperimentRecord],
    ) -> None:
        """
        Raises ValueError if any collision detected.
        """
        for record in active_records:
            self._check_variable_collision(new_spec, record.spec)
            self._check_traffic_collision(new_spec, record.spec)
            self._check_platform_collision(new_spec, record.spec)
            self._check_hypothesis_collision(new_spec, record.spec)
            self._check_rollout_collision(new_spec, record.spec)
    
    def _check_variable_collision(
        self,
        spec1: ExperimentSpec,
        spec2: ExperimentSpec,
    ) -> None:
        """Check if experiments mutate the same variables in same location."""
        # Build (variable_name, location) tuples for both specs
        vars1 = {
            (vc.variable_name, vc.location) 
            for vc in spec1.variable_changes
        }
        vars2 = {
            (vc.variable_name, vc.location)
            for vc in spec2.variable_changes
        }
        
        overlap = vars1 & vars2
        if overlap:
            raise ValueError(
                f"Variable collision detected: {overlap} "
                f"between {spec1.experiment_id} v{spec1.version} and "
                f"{spec2.experiment_id} v{spec2.version}"
            )
    
    def _check_traffic_collision(
        self,
        spec1: ExperimentSpec,
        spec2: ExperimentSpec,
    ) -> None:
        """Check if experiments use same traffic slice."""
        # Same assignment unit + overlapping allocation = collision risk
        if spec1.traffic.assignment_unit == spec2.traffic.assignment_unit:
            # Check if isolation hashes differ
            if spec1.traffic.isolation_hash == spec2.traffic.isolation_hash:
                raise ValueError(
                    f"Traffic collision: {spec1.experiment_id} v{spec1.version} and "
                    f"{spec2.experiment_id} v{spec2.version} "
                    f"use identical isolation hash '{spec1.traffic.isolation_hash}'"
                )
            
            # Check for overlapping allocation ranges
            # If both experiments use same assignment unit, ensure non-overlapping hash ranges
            total_allocation = (
                spec1.traffic.allocation_fraction + 
                spec2.traffic.allocation_fraction
            )
            if total_allocation > 1.0:
                raise ValueError(
                    f"Traffic allocation overflow: {spec1.experiment_id} and "
                    f"{spec2.experiment_id} together allocate {total_allocation:.2%}"
                )
    
    def _check_platform_collision(
        self,
        spec1: ExperimentSpec,
        spec2: ExperimentSpec,
    ) -> None:
        """Check if platform-sensitive experiments conflict."""
        if (spec1.risk_profile.platform_sensitive and 
            spec2.risk_profile.platform_sensitive):
            # Both platform-sensitive = requires manual review
            raise ValueError(
                f"Platform collision: Both {spec1.experiment_id} v{spec1.version} and "
                f"{spec2.experiment_id} v{spec2.version} are platform-sensitive. "
                "Manual review required."
            )
    
    def _check_hypothesis_collision(
        self,
        spec1: ExperimentSpec,
        spec2: ExperimentSpec,
    ) -> None:
        """Check for incompatible hypotheses (same variables, opposite directions)."""
        # If experiments target same variables with opposite expected directions,
        # they may be testing contradictory hypotheses
        vars1 = {vc.variable_name for vc in spec1.variable_changes}
        vars2 = {vc.variable_name for vc in spec2.variable_changes}
        
        if vars1 & vars2:  # Shared variables
            h1_dir = spec1.hypothesis.expected_direction
            h2_dir = spec2.hypothesis.expected_direction
            
            # Incompatible if opposite directions on shared variables
            if (h1_dir.value == "increase" and h2_dir.value == "decrease") or \
               (h1_dir.value == "decrease" and h2_dir.value == "increase"):
                raise ValueError(
                    f"Hypothesis collision: {spec1.experiment_id} expects "
                    f"{h1_dir.value}, {spec2.experiment_id} expects {h2_dir.value} "
                    f"on shared variables {vars1 & vars2}"
                )
    
    def _check_rollout_collision(
        self,
        spec1: ExperimentSpec,
        spec2: ExperimentSpec,
    ) -> None:
        """Check for competing rollout stages on same traffic unit."""
        # If both experiments are in rollout and use same assignment unit,
        # ensure they don't have overlapping stage windows
        if (spec1.traffic.assignment_unit == spec2.traffic.assignment_unit and
            spec1.rollout.stages and spec2.rollout.stages):
            # Check if max allocation overlaps
            max_stage1 = max(spec1.rollout.stages) * spec1.traffic.allocation_fraction
            max_stage2 = max(spec2.rollout.stages) * spec2.traffic.allocation_fraction
            
            if max_stage1 + max_stage2 > 1.0:
                raise ValueError(
                    f"Rollout collision: {spec1.experiment_id} and "
                    f"{spec2.experiment_id} max rollout stages exceed 100% "
                    f"({max_stage1:.2%} + {max_stage2:.2%})"
                )


class ConcurrencyValidator:
    """
    Validates safe concurrent experiment execution.
    """
    
    def validate_concurrency(
        self,
        new_spec: ExperimentSpec,
        active_records: list[ExperimentRecord],
    ) -> None:
        """
        Ensures new experiment can safely run alongside active ones.
        """
        # Check total traffic allocation
        total_allocation = sum(
            r.spec.traffic.allocation_fraction 
            for r in active_records
        )
        total_allocation += new_spec.traffic.allocation_fraction
        
        if total_allocation > 1.0:
            raise ValueError(
                f"Total traffic allocation exceeds 100%: {total_allocation:.2%}"
            )
        
        # High-risk experiments cannot run concurrently
        high_risk_active = any(
            r.spec.risk_profile.risk_level.value == "high"
            for r in active_records
        )
        
        if high_risk_active and new_spec.risk_profile.risk_level.value == "high":
            raise ValueError(
                "Cannot run multiple high-risk experiments concurrently"
            )


class StateTransitionGuard:
    """
    Enforces valid state transitions.
    
    ONE-WAY ONLY.
    """
    
    # Valid transitions
    ALLOWED_TRANSITIONS = {
        (ExperimentState.DRAFT, ExperimentState.REGISTERED),
        (ExperimentState.REGISTERED, ExperimentState.RUNNING),
        (ExperimentState.RUNNING, ExperimentState.PAUSED),
        (ExperimentState.RUNNING, ExperimentState.COMPLETED),
        (ExperimentState.RUNNING, ExperimentState.ABORTED),
        (ExperimentState.PAUSED, ExperimentState.RUNNING),
        (ExperimentState.PAUSED, ExperimentState.ABORTED),
        (ExperimentState.ABORTED, ExperimentState.BANNED),
    }
    
    def validate_transition(
        self,
        from_state: ExperimentState,
        to_state: ExperimentState,
    ) -> None:
        """
        Raises ValueError if transition is invalid.
        """
        if (from_state, to_state) not in self.ALLOWED_TRANSITIONS:
            raise ValueError(
                f"Invalid state transition: {from_state.value} → {to_state.value}"
            )
        
        # BANNED is terminal
        if from_state == ExperimentState.BANNED:
            raise ValueError("Cannot transition from BANNED state")
        
        # COMPLETED is terminal
        if from_state == ExperimentState.COMPLETED:
            raise ValueError("Cannot transition from COMPLETED state")


# ============================================================================
# AUDIT LOG
# ============================================================================

class AuditLogWriter:
    """
    Writes immutable audit trail.
    
    LEGALLY AUDITABLE.
    """
    
    def __init__(self):
        self._log: list[AuditLogEntry] = []
        self._lock = threading.Lock()
    
    def write(
        self,
        experiment_id: str,
        action: str,
        actor: str,
        old_state: Optional[ExperimentState],
        new_state: ExperimentState,
        hash_signature: str,
        reason: str = "",
    ) -> None:
        """Write audit entry."""
        with self._lock:
            entry = AuditLogEntry(
                timestamp=datetime.utcnow(),
                experiment_id=experiment_id,
                action=action,
                actor=actor,
                old_state=old_state,
                new_state=new_state,
                hash_signature=hash_signature,
                reason=reason,
            )
            self._log.append(entry)
    
    def get_history(self, experiment_id: str) -> list[AuditLogEntry]:
        """Get audit history for experiment."""
        with self._lock:
            return [
                entry for entry in self._log
                if entry.experiment_id == experiment_id
            ]
    
    def get_full_log(self) -> list[AuditLogEntry]:
        """Get complete audit log."""
        with self._lock:
            return list(self._log)


# ============================================================================
# REGISTRY LOCK
# ============================================================================

class RegistryLock:
    """
    Thread-safe locking for registry operations.
    """
    
    def __init__(self):
        self._lock = threading.RLock()
    
    def __enter__(self):
        self._lock.acquire()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self._lock.release()


# ============================================================================
# EXPERIMENT REGISTRY (CORE ENGINE)
# ============================================================================

class ExperimentRegistry:
    """
    Single authoritative source of truth for all experiments.
    
    SINGLETON ENFORCED.
    Thread-safe.
    Immutable after registration.
    
    Scales to thousands of concurrent experiments with O(1) lookups.
    """
    
    _instance: Optional['ExperimentRegistry'] = None
    _initialized: bool = False
    _init_lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized') and self._initialized:
            return
        
        with type(self)._init_lock:
            if hasattr(self, '_initialized') and self._initialized:
                return
            
            # Core storage: composite key (experiment_id, version) -> record
            self._records: dict[str, ExperimentRecord] = {}
            self._version_index: dict[str, dict[str, ExperimentRecord]] = defaultdict(dict)  # id -> {version -> record}
            self._latest_versions: dict[str, str] = {}  # experiment_id -> latest version
            
            # Performance indexes
            self._active_index: set[str] = set()  # experiment_id of active experiments
            self._state_index: dict[ExperimentState, set[str]] = defaultdict(set)  # state -> {experiment_id}
            self._tag_index: dict[str, set[str]] = defaultdict(set)  # tag -> {experiment_id}
            self._owner_index: dict[str, set[str]] = defaultdict(set)  # owner -> {experiment_id}
            
            # Validators
            self._spec_validator = ExperimentSpecValidator()
            self._collision_validator = CollisionValidator()
            self._concurrency_validator = ConcurrencyValidator()
            self._state_guard = StateTransitionGuard()
            
            # Audit
            self._audit = AuditLogWriter()
            
            # Lock (RLock for nested calls)
            self._lock = threading.RLock()
            
            # Watchdog
            self._watchdog = None  # Will be set lazily
            
            self._initialized = True
    
    def _get_composite_key(self, experiment_id: str, version: str) -> str:
        """Generate composite key for (experiment_id, version)."""
        return f"{experiment_id}::{version}"
    
    def _get_version_hash(self, experiment_id: str, version: str) -> str:
        """Generate hash for (experiment_id, version) uniqueness."""
        key = f"{experiment_id}::{version}"
        return hashlib.sha256(key.encode()).hexdigest()
    
    # ========================================================================
    # REGISTRATION
    # ========================================================================
    
    def register_experiment(
        self,
        spec: ExperimentSpec,
        actor: str = "system",
        allow_re_register: bool = False,
    ) -> ExperimentRecord:
        """
        Register a new experiment.
        
        HARD-FAIL IF:
        - Duplicate (experiment_id, version) composite key
        - Spec validation fails
        - Hash mismatch on re-registration
        - Unsafe risk profile
        - Non-reversible
        - Conflicts with active experiments
        
        Returns: ExperimentRecord (frozen)
        """
        with self._lock:
            # Validate spec
            self._spec_validator.validate(spec)
            
            # Check for unsafe risk profile
            if spec.risk_profile.irreversible:
                raise ValueError(
                    f"Experiment {spec.experiment_id} v{spec.version}: "
                    "Irreversible experiments are FORBIDDEN"
                )
            
            # Generate composite key and check uniqueness
            composite_key = self._get_composite_key(spec.experiment_id, spec.version)
            existing_record = self._records.get(composite_key)
            
            if existing_record is not None:
                if not allow_re_register:
                    raise ValueError(
                        f"Experiment {spec.experiment_id} v{spec.version} "
                        "already registered. Use allow_re_register=True to override."
                    )
                
                # Re-registration: check hash matches
                new_hash = self._compute_hash(spec)
                if new_hash != existing_record.hash_signature:
                    raise ValueError(
                        f"Hash mismatch on re-registration: "
                        f"Experiment {spec.experiment_id} v{spec.version} "
                        f"expected hash {existing_record.hash_signature}, "
                        f"got {new_hash}. Spec cannot be modified after registration."
                    )
                
                # Hash matches, return existing record
                return existing_record
            
            # Check if any version of this experiment_id exists
            if spec.experiment_id in self._version_index:
                # Check for version conflicts with active experiments
                for version, existing in self._version_index[spec.experiment_id].items():
                    if existing.state in (ExperimentState.RUNNING, ExperimentState.PAUSED):
                        raise ValueError(
                            f"Cannot register new version {spec.version} while "
                            f"version {version} is active (state: {existing.state.value})"
                        )
            
            # Compute hashes
            hash_sig = self._compute_hash(spec)
            version_hash = self._get_version_hash(spec.experiment_id, spec.version)
            
            # Check for collisions with active experiments
            active_records = self.get_active_experiments()
            if active_records:
                self._collision_validator.validate_no_collisions(spec, active_records)
                self._concurrency_validator.validate_concurrency(spec, active_records)
            
            # Generate compatibility tags
            compatibility_tags = self._generate_compatibility_tags(spec)
            
            # Create record
            record = ExperimentRecord(
                spec=spec,
                state=ExperimentState.REGISTERED,
                registered_at=datetime.utcnow(),
                hash_signature=hash_sig,
                version_hash=version_hash,
                compatibility_tags=compatibility_tags,
                active=False,
            )
            
            # Store in all indexes
            self._records[composite_key] = record
            self._version_index[spec.experiment_id][spec.version] = record
            
            # Update latest version if this is newer
            if (spec.experiment_id not in self._latest_versions or
                self._is_version_newer(spec.version, self._latest_versions[spec.experiment_id])):
                self._latest_versions[spec.experiment_id] = spec.version
            
            # Update state index
            self._state_index[ExperimentState.REGISTERED].add(spec.experiment_id)
            
            # Update tag index
            for tag in compatibility_tags:
                self._tag_index[tag].add(spec.experiment_id)
            
            # Update owner index
            self._owner_index[spec.owner].add(spec.experiment_id)
            
            # Audit
            self._audit.write(
                experiment_id=spec.experiment_id,
                action="REGISTER",
                actor=actor,
                old_state=None,
                new_state=ExperimentState.REGISTERED,
                hash_signature=hash_sig,
                reason=f"Registered v{spec.version}: {spec.description}",
            )
            
            return record
    
    def _is_version_newer(self, v1: str, v2: str) -> bool:
        """Compare semantic versions. Returns True if v1 > v2."""
        try:
            parts1 = [int(x) for x in v1.split('.')]
            parts2 = [int(x) for x in v2.split('.')]
            return parts1 > parts2
        except (ValueError, AttributeError):
            # Fallback: string comparison
            return v1 > v2
    
    # ========================================================================
    # STATE TRANSITIONS
    # ========================================================================
    
    def activate_experiment(
        self,
        experiment_id: str,
        version: Optional[str] = None,
        actor: str = "system",
    ) -> ExperimentRecord:
        """
        Activate experiment (REGISTERED → RUNNING or PAUSED → RUNNING).
        
        HARD-FAIL IF:
        - Not registered
        - Invalid state transition
        - Conflicts with active experiments
        - Traffic isolation violated
        
        If version not specified, uses latest version.
        """
        with self._lock:
            # Get record (latest version if not specified)
            if version is None:
                version = self._latest_versions.get(experiment_id)
                if version is None:
                    raise ValueError(
                        f"Experiment {experiment_id} not found. "
                        "Specify version or register first."
                    )
            
            record = self._get_record(experiment_id, version)
            
            # Validate transition (can activate from REGISTERED or PAUSED)
            self._state_guard.validate_transition(
                record.state,
                ExperimentState.RUNNING,
            )
            
            # Check collisions with OTHER active experiments
            active_records = [
                r for r in self.get_active_experiments()
                if r.spec.experiment_id != experiment_id
            ]
            
            if active_records:
                self._collision_validator.validate_no_collisions(
                    record.spec,
                    active_records,
                )
                self._concurrency_validator.validate_concurrency(
                    record.spec,
                    active_records,
                )
            
            # Create new record with updated state
            now = datetime.utcnow()
            new_record = ExperimentRecord(
                spec=record.spec,
                state=ExperimentState.RUNNING,
                registered_at=record.registered_at,
                hash_signature=record.hash_signature,
                version_hash=record.version_hash,
                compatibility_tags=record.compatibility_tags,
                activated_at=now,
                paused_at=None if record.state == ExperimentState.REGISTERED else record.paused_at,
                active=True,
            )
            
            # Update indexes
            composite_key = self._get_composite_key(experiment_id, version)
            self._records[composite_key] = new_record
            self._version_index[experiment_id][version] = new_record
            
            # Update state index
            old_state = record.state
            self._state_index[old_state].discard(experiment_id)
            self._state_index[ExperimentState.RUNNING].add(experiment_id)
            
            # Update active index
            self._active_index.add(experiment_id)
            
            # Audit
            self._audit.write(
                experiment_id=experiment_id,
                action="ACTIVATE",
                actor=actor,
                old_state=record.state,
                new_state=ExperimentState.RUNNING,
                hash_signature=record.hash_signature,
                reason=f"Experiment activated (v{version})",
            )
            
            return new_record
    
    def resume_experiment(
        self,
        experiment_id: str,
        version: Optional[str] = None,
        actor: str = "system",
    ) -> ExperimentRecord:
        """
        Resume paused experiment (PAUSED → RUNNING).
        
        Alias for activate_experiment with state validation.
        """
        return self.activate_experiment(experiment_id, version, actor)
    
    def pause_experiment(
        self,
        experiment_id: str,
        version: Optional[str] = None,
        actor: str = "system",
        reason: str = "",
    ) -> ExperimentRecord:
        """
        Pause running experiment.
        
        Preserves state for potential resume.
        """
        with self._lock:
            # Get record (latest active version if not specified)
            if version is None:
                version = self._get_active_version(experiment_id)
            
            record = self._get_record(experiment_id, version)
            
            # Validate transition
            self._state_guard.validate_transition(
                record.state,
                ExperimentState.PAUSED,
            )
            
            # Create new record
            now = datetime.utcnow()
            new_record = ExperimentRecord(
                spec=record.spec,
                state=ExperimentState.PAUSED,
                registered_at=record.registered_at,
                hash_signature=record.hash_signature,
                version_hash=record.version_hash,
                compatibility_tags=record.compatibility_tags,
                activated_at=record.activated_at,
                paused_at=now,
                active=False,
            )
            
            # Update indexes
            composite_key = self._get_composite_key(experiment_id, version)
            self._records[composite_key] = new_record
            self._version_index[experiment_id][version] = new_record
            
            # Update state index
            self._state_index[ExperimentState.RUNNING].discard(experiment_id)
            self._state_index[ExperimentState.PAUSED].add(experiment_id)
            
            # Update active index
            self._active_index.discard(experiment_id)
            
            # Audit
            self._audit.write(
                experiment_id=experiment_id,
                action="PAUSE",
                actor=actor,
                old_state=record.state,
                new_state=ExperimentState.PAUSED,
                hash_signature=record.hash_signature,
                reason=reason or f"Experiment paused (v{version})",
            )
            
            return new_record
    
    def abort_experiment(
        self,
        experiment_id: str,
        version: Optional[str] = None,
        actor: str = "system",
        reason: str = "",
    ) -> ExperimentRecord:
        """
        Abort experiment (triggers rollback).
        
        TERMINAL STATE.
        """
        with self._lock:
            # Get record (latest active version if not specified)
            if version is None:
                version = self._get_active_version(experiment_id)
            
            record = self._get_record(experiment_id, version)
            
            # Validate transition
            self._state_guard.validate_transition(
                record.state,
                ExperimentState.ABORTED,
            )
            
            # Create new record
            now = datetime.utcnow()
            new_record = ExperimentRecord(
                spec=record.spec,
                state=ExperimentState.ABORTED,
                registered_at=record.registered_at,
                hash_signature=record.hash_signature,
                version_hash=record.version_hash,
                compatibility_tags=record.compatibility_tags,
                activated_at=record.activated_at,
                paused_at=record.paused_at,
                aborted_at=now,
                active=False,
            )
            
            # Update indexes
            composite_key = self._get_composite_key(experiment_id, version)
            self._records[composite_key] = new_record
            self._version_index[experiment_id][version] = new_record
            
            # Update state index
            old_state = record.state
            self._state_index[old_state].discard(experiment_id)
            self._state_index[ExperimentState.ABORTED].add(experiment_id)
            
            # Update active index
            self._active_index.discard(experiment_id)
            
            # Audit
            self._audit.write(
                experiment_id=experiment_id,
                action="ABORT",
                actor=actor,
                old_state=record.state,
                new_state=ExperimentState.ABORTED,
                hash_signature=record.hash_signature,
                reason=reason or f"Experiment aborted (v{version})",
            )
            
            return new_record
    
    def complete_experiment(
        self,
        experiment_id: str,
        version: Optional[str] = None,
        actor: str = "system",
        reason: str = "",
    ) -> ExperimentRecord:
        """
        Mark experiment as completed.
        
        IMMUTABLE FOREVER.
        """
        with self._lock:
            # Get record (latest active version if not specified)
            if version is None:
                version = self._get_active_version(experiment_id)
            
            record = self._get_record(experiment_id, version)
            
            # Validate transition
            self._state_guard.validate_transition(
                record.state,
                ExperimentState.COMPLETED,
            )
            
            # Create new record
            now = datetime.utcnow()
            new_record = ExperimentRecord(
                spec=record.spec,
                state=ExperimentState.COMPLETED,
                registered_at=record.registered_at,
                hash_signature=record.hash_signature,
                version_hash=record.version_hash,
                compatibility_tags=record.compatibility_tags,
                activated_at=record.activated_at,
                paused_at=record.paused_at,
                completed_at=now,
                active=False,
            )
            
            # Update indexes
            composite_key = self._get_composite_key(experiment_id, version)
            self._records[composite_key] = new_record
            self._version_index[experiment_id][version] = new_record
            
            # Update state index
            old_state = record.state
            self._state_index[old_state].discard(experiment_id)
            self._state_index[ExperimentState.COMPLETED].add(experiment_id)
            
            # Update active index
            self._active_index.discard(experiment_id)
            
            # Audit
            self._audit.write(
                experiment_id=experiment_id,
                action="COMPLETE",
                actor=actor,
                old_state=record.state,
                new_state=ExperimentState.COMPLETED,
                hash_signature=record.hash_signature,
                reason=reason or f"Experiment completed (v{version})",
            )
            
            return new_record
    
    def ban_experiment(
        self,
        experiment_id: str,
        version: Optional[str] = None,
        actor: str = "system",
        reason: str = "",
    ) -> ExperimentRecord:
        """
        Ban experiment (permanent block).
        
        NO RESURRECTION.
        """
        with self._lock:
            # Get record (latest version if not specified)
            if version is None:
                version = self._latest_versions.get(experiment_id)
                if version is None:
                    raise ValueError(f"Experiment {experiment_id} not found")
            
            record = self._get_record(experiment_id, version)
            
            # Can only ban from ABORTED
            self._state_guard.validate_transition(
                record.state,
                ExperimentState.BANNED,
            )
            
            # Create new record
            new_record = ExperimentRecord(
                spec=record.spec,
                state=ExperimentState.BANNED,
                registered_at=record.registered_at,
                hash_signature=record.hash_signature,
                version_hash=record.version_hash,
                compatibility_tags=record.compatibility_tags,
                activated_at=record.activated_at,
                paused_at=record.paused_at,
                aborted_at=record.aborted_at,
                active=False,
            )
            
            # Update indexes
            composite_key = self._get_composite_key(experiment_id, version)
            self._records[composite_key] = new_record
            self._version_index[experiment_id][version] = new_record
            
            # Update state index
            self._state_index[ExperimentState.ABORTED].discard(experiment_id)
            self._state_index[ExperimentState.BANNED].add(experiment_id)
            
            # Audit
            self._audit.write(
                experiment_id=experiment_id,
                action="BAN",
                actor=actor,
                old_state=record.state,
                new_state=ExperimentState.BANNED,
                hash_signature=record.hash_signature,
                reason=reason or f"Experiment banned (v{version})",
            )
            
            return new_record
    
    # ========================================================================
    # QUERIES (READ-ONLY)
    # ========================================================================
    
    def get_active_experiments(self) -> list[ExperimentRecord]:
        """
        Returns read-only view of active experiments (RUNNING state only).
        
        Optimized: O(n) where n = number of active experiments.
        Uses active_index for fast lookup.
        """
        with self._lock:
            records = []
            for eid in self._active_index:
                version = self._latest_versions.get(eid)
                if version:
                    composite_key = self._get_composite_key(eid, version)
                    record = self._records.get(composite_key)
                    if record and record.state == ExperimentState.RUNNING:
                        records.append(record)
            return records
    
    def get_experiment(
        self, 
        experiment_id: str, 
        version: Optional[str] = None
    ) -> Optional[ExperimentRecord]:
        """
        Get experiment by ID and version.
        
        If version not specified, returns latest version.
        """
        with self._lock:
            if version is None:
                version = self._latest_versions.get(experiment_id)
                if version is None:
                    return None
            
            composite_key = self._get_composite_key(experiment_id, version)
            return self._records.get(composite_key)
    
    def get_experiment_latest(self, experiment_id: str) -> Optional[ExperimentRecord]:
        """Get latest version of experiment."""
        with self._lock:
            version = self._latest_versions.get(experiment_id)
            if version is None:
                return None
            composite_key = self._get_composite_key(experiment_id, version)
            return self._records.get(composite_key)
    
    def get_experiment_versions(self, experiment_id: str) -> dict[str, ExperimentRecord]:
        """Get all versions of an experiment."""
        with self._lock:
            return dict(self._version_index.get(experiment_id, {}))
    
    def get_all_experiments(self) -> list[ExperimentRecord]:
        """Get all registered experiments (all versions)."""
        with self._lock:
            return list(self._records.values())
    
    def get_experiments_by_state(
        self,
        state: ExperimentState,
    ) -> list[ExperimentRecord]:
        """Get experiments in specific state (latest version only)."""
        with self._lock:
            experiment_ids = self._state_index.get(state, set())
            records = []
            for eid in experiment_ids:
                version = self._latest_versions.get(eid)
                if version:
                    composite_key = self._get_composite_key(eid, version)
                    record = self._records.get(composite_key)
                    if record and record.state == state:
                        records.append(record)
            return records
    
    def get_experiments_by_owner(self, owner: str) -> list[ExperimentRecord]:
        """Get all experiments by owner."""
        with self._lock:
            experiment_ids = self._owner_index.get(owner, set())
            records = []
            for eid in experiment_ids:
                version = self._latest_versions.get(eid)
                if version:
                    composite_key = self._get_composite_key(eid, version)
                    record = self._records.get(composite_key)
                    if record:
                        records.append(record)
            return records
    
    def get_experiments_by_tag(self, tag: str) -> list[ExperimentRecord]:
        """Get experiments matching compatibility tag."""
        with self._lock:
            experiment_ids = self._tag_index.get(tag, set())
            records = []
            for eid in experiment_ids:
                version = self._latest_versions.get(eid)
                if version:
                    composite_key = self._get_composite_key(eid, version)
                    record = self._records.get(composite_key)
                    if record and tag in record.compatibility_tags:
                        records.append(record)
            return records
    
    def get_experiments_by_date_range(
        self,
        start_date: datetime,
        end_date: datetime,
    ) -> list[ExperimentRecord]:
        """Get experiments registered within date range."""
        with self._lock:
            return [
                record for record in self._records.values()
                if start_date <= record.registered_at <= end_date
            ]
    
    def get_running_experiment_ids(self) -> set[str]:
        """Get set of experiment IDs currently running (for fast lookups)."""
        with self._lock:
            return set(self._active_index)
    
    def assert_no_conflicts(self, spec: ExperimentSpec) -> None:
        """
        Check if spec would conflict with active experiments.
        
        Raises ValueError if conflict detected.
        """
        with self._lock:
            active = self.get_active_experiments()
            self._collision_validator.validate_no_collisions(spec, active)
            self._concurrency_validator.validate_concurrency(spec, active)
    
    # ========================================================================
    # AUDIT
    # ========================================================================
    
    def get_audit_history(
        self,
        experiment_id: str,
    ) -> list[AuditLogEntry]:
        """Get audit history for experiment (all versions)."""
        return self._audit.get_history(experiment_id)
    
    def get_full_audit_log(self) -> list[AuditLogEntry]:
        """Get complete audit log (immutable copy)."""
        return self._audit.get_full_log()
    
    def get_audit_log_by_action(self, action: str) -> list[AuditLogEntry]:
        """Get audit entries by action type."""
        with self._lock:
            return [
                entry for entry in self._audit.get_full_log()
                if entry.action == action
            ]
    
    # ========================================================================
    # STATISTICS & METRICS
    # ========================================================================
    
    def get_registry_stats(self) -> dict:
        """Get registry statistics."""
        with self._lock:
            total_experiments = len(self._records)
            total_experiment_ids = len(self._version_index)
            
            state_counts = {
                state.value: len(ids) 
                for state, ids in self._state_index.items()
            }
            
            active_count = len(self._active_index)
            
            # Version distribution
            version_counts = {
                eid: len(versions) 
                for eid, versions in self._version_index.items()
            }
            
            avg_versions_per_experiment = (
                sum(version_counts.values()) / len(version_counts)
                if version_counts else 0
            )
            
            return {
                "total_experiments": total_experiments,
                "total_experiment_ids": total_experiment_ids,
                "active_experiments": active_count,
                "state_distribution": state_counts,
                "avg_versions_per_experiment": avg_versions_per_experiment,
                "max_versions": max(version_counts.values()) if version_counts else 0,
            }
    
    def is_experiment_active(self, experiment_id: str) -> bool:
        """Fast check if experiment is active (O(1))."""
        with self._lock:
            return experiment_id in self._active_index
    
    def get_experiment_count_by_owner(self) -> dict[str, int]:
        """Get count of experiments per owner."""
        with self._lock:
            return {
                owner: len(ids) 
                for owner, ids in self._owner_index.items()
            }
    
    # ========================================================================
    # INTERNAL HELPERS
    # ========================================================================
    
    def _get_record(
        self, 
        experiment_id: str, 
        version: Optional[str] = None
    ) -> ExperimentRecord:
        """Get record or raise."""
        if version is None:
            version = self._latest_versions.get(experiment_id)
            if version is None:
                raise ValueError(f"Experiment {experiment_id} not found")
        
        composite_key = self._get_composite_key(experiment_id, version)
        record = self._records.get(composite_key)
        if record is None:
            raise ValueError(
                f"Experiment {experiment_id} v{version} not found"
            )
        return record
    
    def _get_active_version(self, experiment_id: str) -> str:
        """Get active version of experiment or raise."""
        if experiment_id not in self._active_index:
            raise ValueError(
                f"Experiment {experiment_id} is not active. "
                "Specify version explicitly."
            )
        
        # Find active version
        versions = self._version_index.get(experiment_id, {})
        for version, record in versions.items():
            if record.state == ExperimentState.RUNNING:
                return version
        
        raise ValueError(f"No active version found for {experiment_id}")
    
    def _compute_hash(self, spec: ExperimentSpec) -> str:
        """
        Compute cryptographic hash of spec.
        
        Includes ALL fields to detect any modification.
        """
        # Serialize spec to JSON (comprehensive)
        spec_dict = {
            "experiment_id": spec.experiment_id,
            "version": spec.version,
            "owner": spec.owner,
            "description": spec.description,
            "created_at": spec.created_at.isoformat() if spec.created_at else None,
            # Hypothesis
            "hypothesis": {
                "statement": spec.hypothesis.statement,
                "expected_direction": spec.hypothesis.expected_direction.value,
                "minimum_effect_size": spec.hypothesis.minimum_effect_size,
                "causal_mechanism": spec.hypothesis.causal_mechanism,
            },
            # Variable changes (all fields)
            "variable_changes": [
                {
                    "variable_name": vc.variable_name,
                    "location": vc.location,
                    "baseline_value": str(vc.baseline_value),
                    "variant_value": str(vc.variant_value),
                    "mutation_type": vc.mutation_type.value,
                    "bounded": vc.bounded,
                }
                for vc in spec.variable_changes
            ],
            # Control
            "control": {
                "control_id": spec.control.control_id,
                "definition": spec.control.definition,
            },
            # Traffic
            "traffic": {
                "allocation_fraction": spec.traffic.allocation_fraction,
                "assignment_unit": spec.traffic.assignment_unit.value,
                "isolation_hash": spec.traffic.isolation_hash,
                "min_sample_size": spec.traffic.min_sample_size,
                "max_duration_hours": spec.traffic.max_duration_hours,
            },
            # Rollout
            "rollout": {
                "stages": spec.rollout.stages,
                "advance_conditions": spec.rollout.advance_conditions,
                "rollback_conditions": spec.rollout.rollback_conditions,
            },
            # Metrics
            "success_metrics": [
                {
                    "metric_name": m.metric_name,
                    "source": m.source,
                    "window_hours": m.window_hours,
                    "aggregation": m.aggregation.value,
                    "direction": m.direction.value,
                }
                for m in spec.success_metrics
            ],
            "guardrail_metrics": [
                {
                    "metric_name": g.metric_name,
                    "max_regression": g.max_regression,
                    "action_on_violation": g.action_on_violation.value,
                }
                for g in spec.guardrail_metrics
            ],
            # Risk profile
            "risk_profile": {
                "risk_level": spec.risk_profile.risk_level.value,
                "max_exposure_fraction": spec.risk_profile.max_exposure_fraction,
                "platform_sensitive": spec.risk_profile.platform_sensitive,
            },
            # Reversibility
            "reversibility": {
                "reversible": spec.reversibility.reversible,
                "rollback_path": spec.reversibility.rollback_path,
                "max_rollback_time_seconds": spec.reversibility.max_rollback_time_seconds,
            },
        }
        
        json_str = json.dumps(spec_dict, sort_keys=True, default=str)
        return hashlib.sha256(json_str.encode()).hexdigest()
    
    def _generate_compatibility_tags(
        self,
        spec: ExperimentSpec,
    ) -> frozenset[str]:
        """Generate tags for conflict detection."""
        tags = set()
        
        # Variable tags (variable + location)
        for vc in spec.variable_changes:
            tags.add(f"var:{vc.variable_name}")
            tags.add(f"loc:{vc.location}")
            tags.add(f"var_loc:{vc.variable_name}:{vc.location}")
        
        # Traffic tags
        tags.add(f"traffic:{spec.traffic.assignment_unit.value}")
        tags.add(f"hash:{spec.traffic.isolation_hash[:8]}")  # First 8 chars of hash
        
        # Risk tags
        tags.add(f"risk:{spec.risk_profile.risk_level.value}")
        if spec.risk_profile.platform_sensitive:
            tags.add("platform_sensitive")
        
        # Owner tag
        tags.add(f"owner:{spec.owner}")
        
        return frozenset(tags)


# ============================================================================
# REGISTRY WATCHDOG
# ============================================================================

class RegistryWatchdog:
    """
    Continuously validates registry integrity.
    
    MISMATCH → KILL-SWITCH ELIGIBLE EVENT.
    
    Validates:
    - State consistency
    - Hash integrity
    - Spec drift detection
    - Index consistency
    - Runtime vs registry mismatch (if runtime_state_provider provided)
    """
    
    def __init__(
        self, 
        registry: ExperimentRegistry,
        runtime_state_provider: Optional[Callable[[], Set[str]]] = None,
    ):
        self.registry = registry
        self.runtime_state_provider = runtime_state_provider  # Returns set of experiment_ids running in runtime
    
    def check_integrity(self) -> list[str]:
        """
        Check registry integrity comprehensively.
        
        Returns list of violations (empty if clean).
        """
        violations = []
        
        with self.registry._lock:
            # 1. Check: active experiments have RUNNING state
            active = self.registry.get_active_experiments()
            for record in active:
                if record.state != ExperimentState.RUNNING:
                    violations.append(
                        f"CRITICAL: Active experiment {record.spec.experiment_id} v{record.spec.version} "
                        f"has non-RUNNING state: {record.state.value}"
                    )
                if not record.active:
                    violations.append(
                        f"CRITICAL: Experiment {record.spec.experiment_id} v{record.spec.version} "
                        "marked RUNNING but active=False"
                    )
            
            # 2. Check: no hash mismatches (spec drift detection)
            all_records = self.registry.get_all_experiments()
            for record in all_records:
                computed_hash = self.registry._compute_hash(record.spec)
                if computed_hash != record.hash_signature:
                    violations.append(
                        f"CRITICAL: Hash mismatch for {record.spec.experiment_id} v{record.spec.version}: "
                        f"expected {record.hash_signature}, got {computed_hash}. "
                        "SPEC DRIFT DETECTED - experiment has been modified after registration."
                    )
            
            # 3. Check: index consistency
            violations.extend(self._check_index_consistency())
            
            # 4. Check: version consistency
            violations.extend(self._check_version_consistency())
            
            # 5. Check: state transition consistency
            violations.extend(self._check_state_transitions())
        
        # 6. Check: runtime vs registry mismatch (if provider available)
        if self.runtime_state_provider:
            violations.extend(self._check_runtime_mismatch())
        
        return violations
    
    def _check_index_consistency(self) -> list[str]:
        """Check that indexes are consistent with records."""
        violations = []
        
        # Check active index
        active_index_ids = set(self.registry._active_index)
        running_state_ids = set(self.registry._state_index.get(ExperimentState.RUNNING, set()))
        
        if active_index_ids != running_state_ids:
            violations.append(
                f"Index inconsistency: active_index has {active_index_ids}, "
                f"but state_index[RUNNING] has {running_state_ids}"
            )
        
        # Check that all records in indexes actually exist
        for eid in self.registry._active_index:
            version = self.registry._latest_versions.get(eid)
            if version:
                composite_key = self.registry._get_composite_key(eid, version)
                if composite_key not in self.registry._records:
                    violations.append(
                        f"Index inconsistency: {eid} in active_index but not in records"
                    )
        
        return violations
    
    def _check_version_consistency(self) -> list[str]:
        """Check that version tracking is consistent."""
        violations = []
        
        # Check that latest_versions matches version_index
        for eid, latest_version in self.registry._latest_versions.items():
            if eid not in self.registry._version_index:
                violations.append(
                    f"Version inconsistency: {eid} in latest_versions but not in version_index"
                )
            elif latest_version not in self.registry._version_index[eid]:
                violations.append(
                    f"Version inconsistency: {eid} latest version {latest_version} not in version_index"
                )
        
        # Check that all records have corresponding version entries
        for composite_key, record in self.registry._records.items():
            eid, version = composite_key.split("::", 1)
            if eid not in self.registry._version_index:
                violations.append(
                    f"Version inconsistency: {composite_key} exists but {eid} not in version_index"
                )
            elif version not in self.registry._version_index[eid]:
                violations.append(
                    f"Version inconsistency: {composite_key} exists but version not tracked"
                )
        
        return violations
    
    def _check_state_transitions(self) -> list[str]:
        """Check that state transitions are valid."""
        violations = []
        
        # Get audit log and validate transitions
        audit_log = self.registry.get_full_audit_log()
        state_guard = StateTransitionGuard()
        
        for i in range(1, len(audit_log)):
            prev_entry = audit_log[i-1]
            curr_entry = audit_log[i]
            
            if prev_entry.experiment_id != curr_entry.experiment_id:
                continue  # Different experiments
            
            try:
                state_guard.validate_transition(
                    prev_entry.new_state,
                    curr_entry.new_state,
                )
            except ValueError as e:
                violations.append(
                    f"Invalid state transition in audit log: "
                    f"{prev_entry.experiment_id} {prev_entry.new_state.value} → "
                    f"{curr_entry.new_state.value}: {e}"
                )
        
        return violations
    
    def _check_runtime_mismatch(self) -> list[str]:
        """
        Check for experiments running in runtime but not in registry (or vice versa).
        
        This requires a runtime_state_provider that returns set of experiment_ids.
        """
        violations = []
        
        try:
            runtime_running = self.runtime_state_provider()
        except Exception as e:
            violations.append(f"Failed to get runtime state: {e}")
            return violations
        
        registry_running = {
            record.spec.experiment_id 
            for record in self.registry.get_active_experiments()
        }
        
        # Unknown experiments running in runtime
        unknown = runtime_running - registry_running
        if unknown:
            violations.append(
                f"CRITICAL: Unknown experiments running in runtime (not in registry): {unknown}"
            )
        
        # Experiments in registry but not in runtime
        missing = registry_running - runtime_running
        if missing:
            violations.append(
                f"WARNING: Experiments in registry but not running in runtime: {missing}"
            )
        
        return violations
    
    def check_spec_drift(self, experiment_id: str) -> bool:
        """
        Check if experiment spec has drifted from registered version.
        
        Returns True if drift detected.
        """
        record = self.registry.get_experiment_latest(experiment_id)
        if record is None:
            return False
        
        computed_hash = self.registry._compute_hash(record.spec)
        return computed_hash != record.hash_signature


# ============================================================================
# EXPORT
# ============================================================================

__all__ = [
    # Enums
    "ExperimentState",
    "ExperimentVisibility",
    
    # Core
    "ExperimentRecord",
    "AuditLogEntry",
    
    # Registry
    "ExperimentRegistry",
    
    # Validators
    "CollisionValidator",
    "ConcurrencyValidator",
    "StateTransitionGuard",
    
    # Watchdog
    "RegistryWatchdog",
]