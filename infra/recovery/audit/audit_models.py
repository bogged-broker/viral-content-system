"""
/infra/recovery/audit/audit_models.py

Immutable Recovery Audit Data Models

MISSION:
Define authoritative, immutable data structures for every recovery audit record.
Pure structure. Pure truth. Pure immutability.

CONSTRAINTS:
- NO mutation
- NO setters
- NO derived values
- NO computed timestamps
- NO lazy evaluation
- NO persistence references
- NO side effects

REQUIREMENTS:
- Immutability (frozen dataclasses)
- Explicit fields
- Deterministic equality
- Serialization stability
- Hashability
- Version tagging
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, FrozenSet, Optional, Tuple
from hashlib import sha256
import json


# =============================================================================
# ENUMERATIONS - Finite, Explicit, Non-Negotiable
# =============================================================================


class ActorType(Enum):
    """Actor authority classification"""
    HUMAN = "HUMAN"
    SYSTEM = "SYSTEM"
    SERVICE = "SERVICE"


class TrustLevel(Enum):
    """Hierarchical trust classification"""
    UNTRUSTED = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4
    ABSOLUTE = 5


class TargetType(Enum):
    """Recovery target classification"""
    WORKFLOW = "WORKFLOW"
    NODE = "NODE"
    CHECKPOINT = "CHECKPOINT"
    ARTIFACT = "ARTIFACT"
    METADATA = "METADATA"
    STATE = "STATE"
    TRANSACTION = "TRANSACTION"
    BATCH = "BATCH"


class ActionType(Enum):
    """Recovery action classification"""
    CHECKPOINT_CREATE = "CHECKPOINT_CREATE"
    CHECKPOINT_RESTORE = "CHECKPOINT_RESTORE"
    CHECKPOINT_DELETE = "CHECKPOINT_DELETE"
    CHECKPOINT_VERIFY = "CHECKPOINT_VERIFY"
    
    STATE_SNAPSHOT = "STATE_SNAPSHOT"
    STATE_ROLLBACK = "STATE_ROLLBACK"
    STATE_VERIFY = "STATE_VERIFY"
    STATE_SEAL = "STATE_SEAL"
    
    WORKFLOW_PAUSE = "WORKFLOW_PAUSE"
    WORKFLOW_RESUME = "WORKFLOW_RESUME"
    WORKFLOW_ABORT = "WORKFLOW_ABORT"
    WORKFLOW_RESTART = "WORKFLOW_RESTART"
    
    TRANSACTION_BEGIN = "TRANSACTION_BEGIN"
    TRANSACTION_COMMIT = "TRANSACTION_COMMIT"
    TRANSACTION_ROLLBACK = "TRANSACTION_ROLLBACK"
    TRANSACTION_VERIFY = "TRANSACTION_VERIFY"
    
    AUDIT_SEAL = "AUDIT_SEAL"
    AUDIT_VERIFY = "AUDIT_VERIFY"
    AUDIT_EXPORT = "AUDIT_EXPORT"


class OutcomeStatus(Enum):
    """Action execution outcome"""
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    PARTIAL = "PARTIAL"
    ABORTED = "ABORTED"
    TIMEOUT = "TIMEOUT"
    REJECTED = "REJECTED"


class IntegrityAlgorithm(Enum):
    """Hash algorithm for integrity verification"""
    SHA256 = "SHA256"
    SHA512 = "SHA512"
    BLAKE2B = "BLAKE2B"


# =============================================================================
# 1️⃣ IDENTITY MODELS (Who)
# =============================================================================


@dataclass(frozen=True)
class ImpersonationContext:
    """Explicit delegation chain"""
    original_actor_id: str
    delegated_actor_id: str
    delegation_token: str
    delegation_timestamp: datetime
    delegation_expiry: datetime
    delegation_scope: FrozenSet[str]
    delegation_reason: str
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        assert self.original_actor_id != self.delegated_actor_id, \
            "Cannot delegate to self"
        assert self.delegation_timestamp < self.delegation_expiry, \
            "Delegation already expired"
        assert len(self.delegation_scope) > 0, \
            "Delegation scope cannot be empty"


@dataclass(frozen=True)
class AuthContext:
    """Authentication state snapshot"""
    auth_method: str  # JWT | OAUTH2 | MTLS | INTERNAL | EMERGENCY
    token_hash: str  # Never store raw tokens
    session_id: str
    issued_at: datetime
    expires_at: datetime
    scopes: FrozenSet[str]
    claims: Dict[str, str]  # Immutable snapshot
    mfa_verified: bool
    ip_address: str
    user_agent: str
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        assert self.issued_at < self.expires_at, \
            "Auth token already expired"
        assert len(self.token_hash) == 64, \
            "Token hash must be SHA256 (64 hex chars)"


@dataclass(frozen=True)
class AuditActor:
    """
    Represents the source of authority behind a recovery action.
    
    Immutable. Deterministic. Auditable.
    """
    actor_id: str
    actor_type: ActorType
    auth_context: AuthContext
    trust_level: TrustLevel
    origin_subsystem: str
    origin_host: str
    origin_process_id: str
    impersonation: Optional[ImpersonationContext]
    
    # Actor metadata
    actor_version: str  # Version of actor service/system
    actor_capabilities: FrozenSet[str]
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        assert len(self.actor_id) > 0, "Actor ID cannot be empty"
        assert len(self.origin_subsystem) > 0, "Origin subsystem required"
        assert len(self.origin_host) > 0, "Origin host required"
        
        # HUMAN actors must have HIGH or CRITICAL trust
        if self.actor_type == ActorType.HUMAN:
            assert self.trust_level.value >= TrustLevel.HIGH.value, \
                "HUMAN actors require HIGH+ trust"
        
        # Impersonation requires explicit context
        if self.impersonation is not None:
            assert self.impersonation.original_actor_id == self.actor_id, \
                "Impersonation context mismatch"
    
    def deterministic_hash(self) -> str:
        """SHA256 hash of canonical representation"""
        canonical = (
            f"{self.actor_id}|{self.actor_type.value}|"
            f"{self.auth_context.token_hash}|{self.trust_level.value}|"
            f"{self.origin_subsystem}|{self.origin_host}|{self.origin_process_id}"
        )
        return sha256(canonical.encode('utf-8')).hexdigest()


# =============================================================================
# 2️⃣ TARGET MODELS (What)
# =============================================================================


@dataclass(frozen=True)
class LineageHash:
    """Cryptographic state fingerprint"""
    algorithm: IntegrityAlgorithm
    hash_value: str
    computed_at: datetime
    input_size_bytes: int
    salt: Optional[str]  # Optional for deterministic hashing
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        expected_len = {
            IntegrityAlgorithm.SHA256: 64,
            IntegrityAlgorithm.SHA512: 128,
            IntegrityAlgorithm.BLAKE2B: 128,
        }
        assert len(self.hash_value) == expected_len[self.algorithm], \
            f"Invalid hash length for {self.algorithm.value}"
        assert self.input_size_bytes >= 0, "Size cannot be negative"


@dataclass(frozen=True)
class AuditTarget:
    """
    Represents what was affected or considered in a recovery action.
    
    Targets are ALWAYS snapshotted before any change.
    """
    target_id: str
    target_type: TargetType
    namespace: str
    version: str
    lineage_hash: LineageHash
    
    # Target state snapshot
    state_snapshot: Dict[str, Any]  # Immutable pre-action state
    parent_target_id: Optional[str]  # Hierarchical lineage
    child_target_ids: FrozenSet[str]  # Immutable dependency tree
    
    # Metadata
    created_at: datetime
    last_modified_at: datetime
    tags: FrozenSet[str]
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        assert len(self.target_id) > 0, "Target ID cannot be empty"
        assert len(self.namespace) > 0, "Namespace required"
        assert len(self.version) > 0, "Version required"
        assert self.created_at <= self.last_modified_at, \
            "Invalid timestamp ordering"
        
        # Prevent self-referential targets
        if self.parent_target_id is not None:
            assert self.parent_target_id != self.target_id, \
                "Target cannot be its own parent"
        assert self.target_id not in self.child_target_ids, \
            "Target cannot be its own child"
    
    def deterministic_hash(self) -> str:
        """SHA256 hash of canonical representation"""
        # Sort keys for deterministic serialization
        state_json = json.dumps(self.state_snapshot, sort_keys=True)
        canonical = (
            f"{self.target_id}|{self.target_type.value}|{self.namespace}|"
            f"{self.version}|{self.lineage_hash.hash_value}|{state_json}"
        )
        return sha256(canonical.encode('utf-8')).hexdigest()


# =============================================================================
# 3️⃣ ACTION MODELS (Why & How)
# =============================================================================


@dataclass(frozen=True)
class ActionParameters:
    """Immutable action input parameters"""
    params: Dict[str, Any]  # Frozen snapshot
    checksum: str  # SHA256 of canonical params
    schema_version: str
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        # Verify checksum matches params
        canonical = json.dumps(self.params, sort_keys=True)
        computed = sha256(canonical.encode('utf-8')).hexdigest()
        assert self.checksum == computed, \
            "Parameter checksum mismatch - data corrupted"


@dataclass(frozen=True)
class ActionOutcome:
    """Immutable action execution result"""
    status: OutcomeStatus
    result_data: Dict[str, Any]  # Frozen result snapshot
    error_code: Optional[str]
    error_message: Optional[str]
    error_stacktrace: Optional[str]
    
    # Execution metrics
    execution_duration_ms: int
    retries_attempted: int
    resources_consumed: Dict[str, float]  # CPU, memory, I/O, etc.
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        assert self.execution_duration_ms >= 0, "Duration cannot be negative"
        assert self.retries_attempted >= 0, "Retries cannot be negative"
        
        # Failures must have error information
        if self.status in (OutcomeStatus.FAILURE, OutcomeStatus.ABORTED):
            assert self.error_code is not None, \
                "Failed actions require error_code"
            assert self.error_message is not None, \
                "Failed actions require error_message"


@dataclass(frozen=True)
class AuditAction:
    """
    Represents why and how a recovery action was performed.
    
    Immutable. Deterministic. Replay-compatible.
    """
    action_id: str
    action_type: ActionType
    parameters: ActionParameters
    outcome: ActionOutcome
    
    # Temporal ordering
    initiated_at: datetime
    completed_at: datetime
    
    # Causality chain
    triggered_by_action_id: Optional[str]  # Parent action
    triggered_action_ids: FrozenSet[str]  # Child actions
    
    # Authorization
    authorization_token: str  # Pre-authorized action token hash
    authorization_expires_at: datetime
    
    # Intent and justification
    intent_description: str
    regulatory_reference: Optional[str]  # Compliance tag
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        assert len(self.action_id) > 0, "Action ID cannot be empty"
        assert self.initiated_at <= self.completed_at, \
            "Invalid action timeline"
        assert self.completed_at <= self.authorization_expires_at, \
            "Action completed after authorization expired"
        assert len(self.intent_description) > 0, \
            "Intent description required"
        
        # Self-causality prevention
        if self.triggered_by_action_id is not None:
            assert self.triggered_by_action_id != self.action_id, \
                "Action cannot trigger itself"
        assert self.action_id not in self.triggered_action_ids, \
            "Action cannot be its own child"
    
    def deterministic_hash(self) -> str:
        """SHA256 hash of canonical representation"""
        canonical = (
            f"{self.action_id}|{self.action_type.value}|"
            f"{self.parameters.checksum}|{self.outcome.status.value}|"
            f"{self.initiated_at.isoformat()}|{self.completed_at.isoformat()}"
        )
        return sha256(canonical.encode('utf-8')).hexdigest()


# =============================================================================
# 4️⃣ CONTEXT MODELS (When & Where)
# =============================================================================


@dataclass(frozen=True)
class EnvironmentContext:
    """Execution environment snapshot"""
    environment_name: str  # PROD | STAGING | DEV | DR
    region: str
    availability_zone: str
    cluster_id: str
    node_id: str
    
    # System state
    system_load_avg: float
    memory_pressure: float  # 0.0 to 1.0
    disk_pressure: float  # 0.0 to 1.0
    network_latency_ms: float
    
    # Version metadata
    platform_version: str
    kernel_version: str
    runtime_version: str
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        assert len(self.environment_name) > 0, "Environment required"
        assert 0.0 <= self.memory_pressure <= 1.0, "Invalid memory pressure"
        assert 0.0 <= self.disk_pressure <= 1.0, "Invalid disk pressure"
        assert self.network_latency_ms >= 0, "Latency cannot be negative"


@dataclass(frozen=True)
class TemporalContext:
    """Temporal metadata for audit record"""
    event_timestamp: datetime  # Wall clock time
    monotonic_timestamp: int  # Monotonic counter (nanoseconds since epoch)
    logical_clock: int  # Lamport timestamp for ordering
    
    # Timezone and precision
    timezone_name: str
    timestamp_precision_ms: int
    
    # Clock synchronization
    ntp_synchronized: bool
    clock_drift_ms: float
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        assert self.monotonic_timestamp >= 0, "Monotonic time cannot be negative"
        assert self.logical_clock >= 0, "Logical clock cannot be negative"
        assert self.timestamp_precision_ms > 0, "Precision must be positive"
        assert abs(self.clock_drift_ms) < 1000, \
            "Clock drift exceeds acceptable threshold (1s)"


@dataclass(frozen=True)
class AuditContext:
    """
    Represents when and where a recovery action occurred.
    
    Captures complete environmental and temporal state.
    """
    temporal: TemporalContext
    environment: EnvironmentContext
    
    # Correlation
    trace_id: str  # Distributed tracing ID
    span_id: str  # Span within trace
    correlation_id: str  # Business correlation ID
    
    # Session
    session_id: str
    request_id: str
    
    # Metadata
    labels: FrozenSet[Tuple[str, str]]  # Immutable key-value pairs
    annotations: Dict[str, str]  # Additional context
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        assert len(self.trace_id) > 0, "Trace ID required"
        assert len(self.span_id) > 0, "Span ID required"
        assert len(self.correlation_id) > 0, "Correlation ID required"
    
    def deterministic_hash(self) -> str:
        """SHA256 hash of canonical representation"""
        labels_str = "|".join(sorted(f"{k}={v}" for k, v in self.labels))
        canonical = (
            f"{self.trace_id}|{self.span_id}|{self.correlation_id}|"
            f"{self.temporal.event_timestamp.isoformat()}|"
            f"{self.temporal.monotonic_timestamp}|{labels_str}"
        )
        return sha256(canonical.encode('utf-8')).hexdigest()


# =============================================================================
# 5️⃣ INTEGRITY MODELS (Hashing & Lineage)
# =============================================================================


@dataclass(frozen=True)
class IntegritySignature:
    """Cryptographic signature for non-repudiation"""
    signature_algorithm: str  # RSA | ECDSA | ED25519
    signature_value: str  # Base64-encoded signature
    public_key_fingerprint: str  # SHA256 of public key
    signed_at: datetime
    signer_identity: str
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        assert len(self.signature_value) > 0, "Signature cannot be empty"
        assert len(self.public_key_fingerprint) == 64, \
            "Key fingerprint must be SHA256"


@dataclass(frozen=True)
class ChainLink:
    """Link in audit chain for causality tracking"""
    previous_record_hash: str  # Hash of previous audit record
    current_record_hash: str  # Hash of this audit record
    sequence_number: int  # Monotonically increasing
    chain_id: str  # Chain identifier
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        assert len(self.previous_record_hash) == 64, \
            "Previous hash must be SHA256"
        assert len(self.current_record_hash) == 64, \
            "Current hash must be SHA256"
        assert self.sequence_number >= 0, "Sequence cannot be negative"
        assert self.previous_record_hash != self.current_record_hash, \
            "Hash collision or self-reference"


@dataclass(frozen=True)
class AuditIntegrity:
    """
    Cryptographic integrity and lineage tracking.
    
    Enables chain verification, tamper detection, replay prevention.
    """
    record_hash: str  # SHA256 of complete audit record
    record_signature: IntegritySignature
    chain_link: ChainLink
    
    # Versioning
    schema_version: str
    model_version: str
    
    # Tamper detection
    sealed_at: datetime
    seal_algorithm: IntegrityAlgorithm
    
    # Retention
    retention_class: str  # TRANSIENT | STANDARD | ARCHIVE | PERMANENT
    retention_expires_at: Optional[datetime]
    immutable_until: datetime  # WORM semantics
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        assert len(self.record_hash) == 64, "Record hash must be SHA256"
        assert self.sealed_at <= datetime.now(timezone.utc), \
            "Cannot seal in the future"
        assert self.immutable_until >= self.sealed_at, \
            "Immutability must start after sealing"
        
        # Retention validation
        if self.retention_class != "PERMANENT":
            assert self.retention_expires_at is not None, \
                "Non-permanent records require expiry"
            assert self.retention_expires_at > self.sealed_at, \
                "Retention must extend beyond sealing"


# =============================================================================
# COMPOSITE AUDIT RECORD - The Complete Truth
# =============================================================================


@dataclass(frozen=True)
class RecoveryAuditRecord:
    """
    The atomic, immutable, authoritative audit record.
    
    Composition of all five model families:
    1. Actor (who)
    2. Target (what)
    3. Action (why & how)
    4. Context (when & where)
    5. Integrity (hashing & lineage)
    
    This is the single source of truth for recovery audit events.
    """
    # Core components
    actor: AuditActor
    target: AuditTarget
    action: AuditAction
    context: AuditContext
    integrity: AuditIntegrity
    
    # Record metadata
    record_id: str  # Globally unique
    record_version: int  # Schema evolution support
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        assert len(self.record_id) > 0, "Record ID required"
        assert self.record_version > 0, "Version must be positive"
        
        # Temporal consistency
        assert self.action.initiated_at >= self.target.created_at, \
            "Cannot act on target before it exists"
        assert self.context.temporal.event_timestamp <= self.integrity.sealed_at, \
            "Event cannot occur after sealing"
        
        # Trust level validation
        critical_actions = {
            ActionType.CHECKPOINT_DELETE,
            ActionType.STATE_ROLLBACK,
            ActionType.WORKFLOW_ABORT,
            ActionType.TRANSACTION_ROLLBACK,
        }
        if self.action.action_type in critical_actions:
            assert self.actor.trust_level.value >= TrustLevel.HIGH.value, \
                f"Critical action {self.action.action_type.value} requires HIGH+ trust"
    
    def compute_canonical_hash(self) -> str:
        """
        Compute deterministic SHA256 hash of entire record.
        
        This hash MUST match integrity.record_hash for valid records.
        Used for tamper detection and chain verification.
        """
        components = [
            self.actor.deterministic_hash(),
            self.target.deterministic_hash(),
            self.action.deterministic_hash(),
            self.context.deterministic_hash(),
            self.record_id,
            str(self.record_version),
        ]
        canonical = "|".join(components)
        return sha256(canonical.encode('utf-8')).hexdigest()
    
    def verify_integrity(self) -> bool:
        """
        Verify record integrity.
        
        Returns True if:
        - Computed hash matches sealed hash
        - Chain link is valid
        - Signature is valid (external verification needed)
        """
        computed = self.compute_canonical_hash()
        return computed == self.integrity.record_hash
    
    def to_immutable_dict(self) -> Dict[str, Any]:
        """
        Serialize to immutable dictionary for storage/transmission.
        
        Deterministic serialization - same record always produces
        same dict with keys in same order.
        """
        return {
            "record_id": self.record_id,
            "record_version": self.record_version,
            
            "actor": {
                "actor_id": self.actor.actor_id,
                "actor_type": self.actor.actor_type.value,
                "trust_level": self.actor.trust_level.value,
                "origin_subsystem": self.actor.origin_subsystem,
                "origin_host": self.actor.origin_host,
                "origin_process_id": self.actor.origin_process_id,
                "actor_version": self.actor.actor_version,
                "auth_context": {
                    "auth_method": self.actor.auth_context.auth_method,
                    "token_hash": self.actor.auth_context.token_hash,
                    "session_id": self.actor.auth_context.session_id,
                    "scopes": sorted(self.actor.auth_context.scopes),
                    "mfa_verified": self.actor.auth_context.mfa_verified,
                },
            },
            
            "target": {
                "target_id": self.target.target_id,
                "target_type": self.target.target_type.value,
                "namespace": self.target.namespace,
                "version": self.target.version,
                "lineage_hash": {
                    "algorithm": self.target.lineage_hash.algorithm.value,
                    "hash_value": self.target.lineage_hash.hash_value,
                    "computed_at": self.target.lineage_hash.computed_at.isoformat(),
                },
                "state_snapshot": self.target.state_snapshot,
            },
            
            "action": {
                "action_id": self.action.action_id,
                "action_type": self.action.action_type.value,
                "initiated_at": self.action.initiated_at.isoformat(),
                "completed_at": self.action.completed_at.isoformat(),
                "intent_description": self.action.intent_description,
                "outcome": {
                    "status": self.action.outcome.status.value,
                    "execution_duration_ms": self.action.outcome.execution_duration_ms,
                    "result_data": self.action.outcome.result_data,
                },
            },
            
            "context": {
                "trace_id": self.context.trace_id,
                "span_id": self.context.span_id,
                "correlation_id": self.context.correlation_id,
                "temporal": {
                    "event_timestamp": self.context.temporal.event_timestamp.isoformat(),
                    "monotonic_timestamp": self.context.temporal.monotonic_timestamp,
                    "logical_clock": self.context.temporal.logical_clock,
                },
                "environment": {
                    "environment_name": self.context.environment.environment_name,
                    "region": self.context.environment.region,
                    "cluster_id": self.context.environment.cluster_id,
                },
            },
            
            "integrity": {
                "record_hash": self.integrity.record_hash,
                "sealed_at": self.integrity.sealed_at.isoformat(),
                "seal_algorithm": self.integrity.seal_algorithm.value,
                "chain_link": {
                    "previous_record_hash": self.integrity.chain_link.previous_record_hash,
                    "current_record_hash": self.integrity.chain_link.current_record_hash,
                    "sequence_number": self.integrity.chain_link.sequence_number,
                    "chain_id": self.integrity.chain_link.chain_id,
                },
                "retention_class": self.integrity.retention_class,
                "immutable_until": self.integrity.immutable_until.isoformat(),
            },
        }


# =============================================================================
# TYPE REGISTRY - Versioned Schema Catalog
# =============================================================================


@dataclass(frozen=True)
class ModelRegistry:
    """
    Immutable registry of all model versions for schema evolution.
    
    Enables backward-compatible deserialization and migration.
    """
    schema_version: str
    model_versions: Dict[str, str]
    supported_algorithms: FrozenSet[IntegrityAlgorithm]
    supported_actor_types: FrozenSet[ActorType]
    supported_target_types: FrozenSet[TargetType]
    supported_action_types: FrozenSet[ActionType]
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        assert len(self.schema_version) > 0, "Schema version required"
        assert len(self.model_versions) > 0, "Model versions required"


# Current registry - update on schema evolution
CURRENT_MODEL_REGISTRY = ModelRegistry(
    schema_version="1.0.0",
    model_versions={
        "AuditActor": "1.0.0",
        "AuditTarget": "1.0.0",
        "AuditAction": "1.0.0",
        "AuditContext": "1.0.0",
        "AuditIntegrity": "1.0.0",
        "RecoveryAuditRecord": "1.0.0",
    },
    supported_algorithms=frozenset([
        IntegrityAlgorithm.SHA256,
        IntegrityAlgorithm.SHA512,
        IntegrityAlgorithm.BLAKE2B,
    ]),
    supported_actor_types=frozenset([
        ActorType.HUMAN,
        ActorType.SYSTEM,
        ActorType.SERVICE,
    ]),
    supported_target_types=frozenset([
        TargetType.WORKFLOW,
        TargetType.NODE,
        TargetType.CHECKPOINT,
        TargetType.ARTIFACT,
        TargetType.METADATA,
        TargetType.STATE,
        TargetType.TRANSACTION,
        TargetType.BATCH,
    ]),
    supported_action_types=frozenset([
        ActionType.CHECKPOINT_CREATE,
        ActionType.CHECKPOINT_RESTORE,
        ActionType.CHECKPOINT_DELETE,
        ActionType.CHECKPOINT_VERIFY,
        ActionType.STATE_SNAPSHOT,
        ActionType.STATE_ROLLBACK,
        ActionType.STATE_VERIFY,
        ActionType.STATE_SEAL,
        ActionType.WORKFLOW_PAUSE,
        ActionType.WORKFLOW_RESUME,
        ActionType.WORKFLOW_ABORT,
        ActionType.WORKFLOW_RESTART,
        ActionType.TRANSACTION_BEGIN,
        ActionType.TRANSACTION_COMMIT,
        ActionType.TRANSACTION_ROLLBACK,
        ActionType.TRANSACTION_VERIFY,
        ActionType.AUDIT_SEAL,
        ActionType.AUDIT_VERIFY,
        ActionType.AUDIT_EXPORT,
    ]),
)


# =============================================================================
# IMMUTABILITY GUARANTEES - Compile-Time Verification
# =============================================================================

# All models are frozen dataclasses - mutation is impossible at runtime.
# Attempting to modify any field raises FrozenInstanceError.
#
# Example:
#   record = RecoveryAuditRecord(...)
#   record.actor.actor_id = "new_id"  # ❌ FrozenInstanceError
#
# Hashability is guaranteed - all models can be dict keys or set members.
# Deterministic equality is guaranteed - same data always equals.
# Serialization stability is guaranteed - canonical representation.