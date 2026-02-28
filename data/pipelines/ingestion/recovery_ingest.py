"""
recovery_ingest.py

Recovery-Visible State Re-Entry Authority

This is the ONLY gateway by which recovered, replayed, or repaired state
is allowed to re-enter the canonical data universe.

Design Principle: Recovered state is guilty until proven identical, justified, and bounded.

Nothing crosses this boundary on trust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Any, Tuple, Protocol
from datetime import datetime, timezone
import hashlib
import json

from ..ingestion.base.ingest_context import IngestContext, IngestMode, IngestAuthority
from ..ingestion.base.ingest_result import IngestResult, IngestOutcome, RejectionReason
from ..ingestion.builders.result_factory import create_accepted_result, create_rejected_result
from ..ingestion.base.ingest_errors import (
    IngestError as BaseIngestError,
    IngestErrorCode,
    ErrorCategory,
    RecoveryHint,
    IngestErrorContext,
    IngestErrorCause,
    IngestErrorBuilder,
    CommonIngestErrors,
)


# ============================================================================
# ENUMS & CONSTANTS
# ============================================================================

class RecoveryType(Enum):
    """Allowed recovery operation types."""
    REPLAY = "replay"
    ROLLBACK = "rollback"
    REPAIR = "repair"


class RecoverySource(Enum):
    """Allowed recovery source subsystems."""
    CHECKPOINT = "checkpoint"
    WORKFLOW_REPAIR = "workflow_repair"
    MANUAL_INTERVENTION = "manual_intervention"


class ScopeType(Enum):
    """Recovery scope boundaries."""
    GLOBAL = "global"
    WORKFLOW = "workflow"
    ACCOUNT = "account"
    ARTIFACT = "artifact"


class DeduplicationStatus(Enum):
    """Result of deduplication check."""
    NEW = "NEW"
    DUPLICATE = "DUPLICATE"
    CONFLICTING = "CONFLICTING"


# ============================================================================
# EXCEPTIONS
# ============================================================================

class RecoveryIngestError(Exception):
    """Base exception for recovery ingestion failures."""
    pass


# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass(frozen=True)
class RecoveryIngestPolicy:
    """
    Immutable, versioned policy defining what recovery outputs are allowed.
    
    Policy changes require deployment, not runtime flags.
    """
    policy_version: str
    allowed_recovery_types: Set[RecoveryType]
    allowed_sources: Set[RecoverySource]
    max_scope_size: int
    max_time_travel_ms: int
    require_checkpoint_validation: bool
    require_audit_package: bool
    canonical_schema_version: str
    
    def __post_init__(self):
        """Validate policy immutability constraints."""
        if not self.policy_version:
            raise ValueError("policy_version must be set")
        if not self.allowed_recovery_types:
            raise ValueError("allowed_recovery_types cannot be empty")
        if not self.allowed_sources:
            raise ValueError("allowed_sources cannot be empty")
        if self.max_scope_size <= 0:
            raise ValueError("max_scope_size must be positive")
        if self.max_time_travel_ms <= 0:
            raise ValueError("max_time_travel_ms must be positive")
        if not self.canonical_schema_version:
            raise ValueError("canonical_schema_version must be set")


@dataclass(frozen=True)
class RecoveryPayload:
    """
    Input recovery data requiring validation and admission.
    
    Immutable to prevent mutation during validation pipeline.
    """
    recovery_id: str
    recovery_type: RecoveryType
    source_subsystem: RecoverySource
    audit_package_ref: str
    checkpoint_id: str
    checkpoint_hash: str
    lineage_hash: str
    scope_type: ScopeType
    scope_id: str
    affected_entity_ids: Tuple[str, ...]
    schema_version: str
    timestamp: datetime
    payload_data: Dict[str, Any]
    parent_lineage_node: Optional[str] = None
    replay_context_hash: Optional[str] = None
    
    def __post_init__(self):
        """Validate payload at construction."""
        if not self.recovery_id or not self.recovery_id.strip():
            raise ValueError("recovery_id must be present and non-empty")
        if not self.checkpoint_id or not self.checkpoint_id.strip():
            raise ValueError("checkpoint_id must be present and non-empty")
        if not self.checkpoint_hash or not self.checkpoint_hash.strip():
            raise ValueError("checkpoint_hash must be present and non-empty")
        if not self.lineage_hash or not self.lineage_hash.strip():
            raise ValueError("lineage_hash must be present and non-empty")
        if not self.scope_id or not self.scope_id.strip():
            raise ValueError("scope_id must be present and non-empty")
        if not self.schema_version or not self.schema_version.strip():
            raise ValueError("schema_version must be present and non-empty")
        if not self.audit_package_ref or not self.audit_package_ref.strip():
            raise ValueError("audit_package_ref must be present and non-empty")
        if not self.affected_entity_ids:
            raise ValueError("affected_entity_ids cannot be empty")
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
    
    def compute_payload_hash(self) -> str:
        """Compute deterministic hash of payload data."""
        canonical_json = json.dumps(
            self.payload_data,
            sort_keys=True,
            separators=(',', ':')
        )
        return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class RecoveryFact:
    """
    Immutable recovery record emitted after successful ingestion.
    
    Once written, treated as historical truth.
    """
    recovery_id: str
    recovery_type: str
    source_subsystem: str
    checkpoint_id: str
    checkpoint_hash: str
    lineage_hash: str
    scope_type: str
    scope_id: str
    affected_entity_ids: Tuple[str, ...]
    schema_version: str
    timestamp: datetime
    audit_package_ref: str
    payload_hash: str
    ingestion_timestamp: datetime
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for persistence."""
        return {
            'recovery_id': self.recovery_id,
            'recovery_type': self.recovery_type,
            'source_subsystem': self.source_subsystem,
            'checkpoint_id': self.checkpoint_id,
            'checkpoint_hash': self.checkpoint_hash,
            'lineage_hash': self.lineage_hash,
            'scope_type': self.scope_type,
            'scope_id': self.scope_id,
            'affected_entity_ids': list(self.affected_entity_ids),
            'schema_version': self.schema_version,
            'timestamp': self.timestamp.isoformat(),
            'audit_package_ref': self.audit_package_ref,
            'payload_hash': self.payload_hash,
            'ingestion_timestamp': self.ingestion_timestamp.isoformat(),
        }


@dataclass
class CheckpointRecord:
    """Checkpoint metadata for lineage verification."""
    checkpoint_id: str
    checkpoint_hash: str
    validated: bool
    parent_checkpoint_id: Optional[str]
    lineage_sequence: int
    created_at: datetime


@dataclass
class LineageNode:
    """Node in the recovery lineage chain."""
    node_id: str
    checkpoint_id: str
    parent_node_id: Optional[str]
    replay_context_hash: str
    sequence_number: int
    is_fork: bool
    fork_audit_ref: Optional[str]


# ============================================================================
# EXTERNAL DEPENDENCIES (INTERFACES)
# ============================================================================

class CheckpointStore:
    """Interface to checkpoint persistence layer."""
    
    def get_checkpoint(self, checkpoint_id: str) -> Optional[CheckpointRecord]:
        """Retrieve checkpoint by ID."""
        raise NotImplementedError
    
    def validate_checkpoint_hash(self, checkpoint_id: str, expected_hash: str) -> bool:
        """Verify checkpoint hash matches expected value."""
        raise NotImplementedError


class LineageStore:
    """Interface to lineage tracking system."""
    
    def get_lineage_node(self, node_id: str) -> Optional[LineageNode]:
        """Retrieve lineage node by ID."""
        raise NotImplementedError
    
    def verify_lineage_chain(self, node_id: str, checkpoint_id: str) -> bool:
        """Verify complete lineage chain from node to checkpoint."""
        raise NotImplementedError


class AuditStore:
    """Interface to audit trail system."""
    
    def verify_audit_package(self, audit_ref: str) -> bool:
        """Verify audit package exists and is valid."""
        raise NotImplementedError


class SchemaRegistry:
    """Interface to schema versioning system."""
    
    def is_compatible(self, schema_version: str, canonical_version: str) -> bool:
        """Check if schema version is compatible with canonical version."""
        raise NotImplementedError


class RecoveryFactStore:
    """Interface to recovery fact persistence."""
    
    def get_by_recovery_id(self, recovery_id: str) -> Optional[RecoveryFact]:
        """Retrieve recovery fact by recovery ID."""
        raise NotImplementedError
    
    def get_by_dedup_key(self, dedup_key: str) -> Optional[RecoveryFact]:
        """Retrieve recovery fact by deduplication key."""
        raise NotImplementedError
    
    def persist(self, fact: RecoveryFact) -> None:
        """Persist immutable recovery fact."""
        raise NotImplementedError
    
    def persist_atomic(self, fact: RecoveryFact, dedup_key: str) -> RecoveryFact:
        """
        Atomically persist recovery fact with deduplication enforcement.
        
        This method ensures atomic compare-and-persist semantics:
        - If recovery_id already exists → returns existing fact
        - If dedup_key already exists → raises conflict error
        - Otherwise → persists and returns new fact
        
        Args:
            fact: Recovery fact to persist
            dedup_key: Deduplication key for conflict detection
            
        Returns:
            RecoveryFact - either the newly persisted fact or existing fact
            
        Raises:
            BaseIngestError: If conflicting recovery detected (same key, different content)
        """
        raise NotImplementedError


# ============================================================================
# RECOVERY VALIDATOR (HARD GATE)
# ============================================================================

class RecoveryValidator:
    """
    Validates recovery payload legitimacy.
    
    Rejects orphaned recovery outputs, un-audited state, and unverifiable lineage.
    """
    
    def __init__(
        self,
        policy: RecoveryIngestPolicy,
        audit_store: AuditStore,
        schema_registry: SchemaRegistry
    ):
        self.policy = policy
        self.audit_store = audit_store
        self.schema_registry = schema_registry
    
    def validate(self, payload: RecoveryPayload, ingest_context: IngestContext) -> None:
        """
        Validate recovery payload against all requirements.
        
        Raises BaseIngestError on any validation failure.
        """
        error_context = IngestErrorContext(
            pipeline_step="recovery_validation",
            run_id=ingest_context.run_id,
            input_id=payload.recovery_id,
            entity_type="recovery",
            entity_id=payload.recovery_id
        )
        
        self._validate_recovery_id(payload, error_context)
        self._validate_recovery_type(payload, error_context)
        self._validate_source_subsystem(payload, error_context)
        self._validate_audit_package(payload, error_context)
        self._validate_checkpoint_references(payload, error_context)
        self._validate_schema_compatibility(payload, error_context)
        self._validate_timestamp_sanity(payload, error_context)
        self._validate_scope_consistency(payload, error_context)
    
    def _validate_recovery_id(self, payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """Ensure recovery_id is present and valid."""
        if not payload.recovery_id or not payload.recovery_id.strip():
            raise CommonIngestErrors.invariant_broken(
                invariant_name="recovery_id_presence",
                violation_message="recovery_id is missing or empty",
                context=context
            )
    
    def _validate_recovery_type(self, payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """Ensure recovery_type is declared and allowed."""
        if payload.recovery_type not in self.policy.allowed_recovery_types:
            raise IngestErrorBuilder(
                category=ErrorCategory.AUTHORITY,
                context=context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.POLICY_VIOLATION,
                message=f"recovery_type {payload.recovery_type.value} not allowed by policy",
                source="recovery_validator",
                constraint="allowed_recovery_types",
                expected_value=str([rt.value for rt in self.policy.allowed_recovery_types]),
                actual_value=payload.recovery_type.value
            ).build()
    
    def _validate_source_subsystem(self, payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """Ensure source subsystem is identified and allowed."""
        if payload.source_subsystem not in self.policy.allowed_sources:
            raise IngestErrorBuilder(
                category=ErrorCategory.AUTHORITY,
                context=context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.POLICY_VIOLATION,
                message=f"source_subsystem {payload.source_subsystem.value} not allowed by policy",
                source="recovery_validator",
                constraint="allowed_sources",
                expected_value=str([src.value for src in self.policy.allowed_sources]),
                actual_value=payload.source_subsystem.value
            ).build()
    
    def _validate_audit_package(self, payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """Ensure audit package reference is present and valid."""
        if self.policy.require_audit_package:
            if not payload.audit_package_ref or not payload.audit_package_ref.strip():
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="audit_package_required",
                    violation_message="audit_package_ref is required but missing",
                    context=context
                )
            
            if not self.audit_store.verify_audit_package(payload.audit_package_ref):
                raise IngestErrorBuilder(
                    category=ErrorCategory.STATE,
                    context=context,
                    recovery_hint=RecoveryHint.REQUIRES_MANUAL_REVIEW
                ).add_cause(
                    code=IngestErrorCode.DEPENDENCY_MISSING,
                    message=f"audit_package_ref {payload.audit_package_ref} not found or invalid",
                    source="audit_store",
                    actual_value=payload.audit_package_ref
                ).build()
    
    def _validate_checkpoint_references(self, payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """Ensure checkpoint_id and checkpoint_hash are present."""
        # These are already validated in __post_init__, but we check again for explicit error context
        if not payload.checkpoint_id or not payload.checkpoint_id.strip():
            raise CommonIngestErrors.invariant_broken(
                invariant_name="checkpoint_id_presence",
                violation_message="checkpoint_id is missing or empty",
                context=context
            )
        
        if not payload.checkpoint_hash or not payload.checkpoint_hash.strip():
            raise CommonIngestErrors.invariant_broken(
                invariant_name="checkpoint_hash_presence",
                violation_message="checkpoint_hash is missing or empty",
                context=context
            )
    
    def _validate_schema_compatibility(self, payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """
        Verify schema version compatibility.
        
        Uses schema registry compatibility check as the single source of truth.
        The invariant layer enforces strict equality, but validator uses compatibility
        to allow forward-compatible schema versions.
        """
        if not self.schema_registry.is_compatible(
            payload.schema_version,
            self.policy.canonical_schema_version
        ):
            raise CommonIngestErrors.unsupported_version(
                field_name="schema_version",
                expected=str(self.policy.canonical_schema_version),
                actual=payload.schema_version,
                context=context
            )
    
    def _validate_timestamp_sanity(self, payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """Ensure timestamp is sane (no future-dated recovery)."""
        now = datetime.now(timezone.utc)
        
        if payload.timestamp > now:
            raise IngestErrorBuilder(
                category=ErrorCategory.VALIDATION,
                context=context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.CONSTRAINT_VIOLATION,
                message=f"recovery timestamp is in the future",
                source="timestamp_validator",
                constraint="timestamp_not_future",
                expected_value=now.isoformat(),
                actual_value=payload.timestamp.isoformat()
            ).build()
        
        # Check time travel bounds
        time_delta_ms = int((now - payload.timestamp).total_seconds() * 1000)
        if time_delta_ms > self.policy.max_time_travel_ms:
            raise IngestErrorBuilder(
                category=ErrorCategory.VALIDATION,
                context=context,
                recovery_hint=RecoveryHint.REQUIRES_MANUAL_REVIEW
            ).add_cause(
                code=IngestErrorCode.CONSTRAINT_VIOLATION,
                message=f"recovery timestamp exceeds max_time_travel_ms",
                source="timestamp_validator",
                constraint="max_time_travel_ms",
                expected_value=str(self.policy.max_time_travel_ms),
                actual_value=str(time_delta_ms)
            ).build()
    
    def _validate_scope_consistency(self, payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """Ensure no cross-scope contamination."""
        # These are already validated in __post_init__, but we check again for explicit error context
        if not payload.scope_id or not payload.scope_id.strip():
            raise CommonIngestErrors.invariant_broken(
                invariant_name="scope_id_presence",
                violation_message="scope_id is missing or empty",
                context=context
            )
        
        if not payload.affected_entity_ids:
            raise CommonIngestErrors.invariant_broken(
                invariant_name="affected_entity_ids_presence",
                violation_message="affected_entity_ids is empty",
                context=context
            )


# ============================================================================
# RECOVERY LINEAGE VERIFIER (NON-NEGOTIABLE)
# ============================================================================

class RecoveryLineageVerifier:
    """
    Verifies causal continuity of recovery lineage.
    
    Lineage is immutable. Partial lineage = reject. Forks must be explicit and audited.
    """
    
    def __init__(
        self,
        checkpoint_store: CheckpointStore,
        lineage_store: LineageStore,
        policy: RecoveryIngestPolicy
    ):
        self.checkpoint_store = checkpoint_store
        self.lineage_store = lineage_store
        self.policy = policy
    
    def verify(self, payload: RecoveryPayload, ingest_context: IngestContext) -> None:
        """
        Verify complete lineage integrity.
        
        Raises BaseIngestError if lineage is unclear or incomplete.
        """
        error_context = IngestErrorContext(
            pipeline_step="recovery_lineage_verification",
            run_id=ingest_context.run_id,
            input_id=payload.recovery_id,
            entity_type="recovery",
            entity_id=payload.recovery_id
        )
        
        self._verify_checkpoint_exists(payload, error_context)
        self._verify_checkpoint_validated(payload, error_context)
        self._verify_checkpoint_hash(payload, error_context)
        self._verify_replay_context(payload, error_context)
        self._verify_lineage_chain(payload, error_context)
        self._verify_no_branch_ambiguity(payload, error_context)
    
    def _verify_checkpoint_exists(self, payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """Ensure referenced checkpoint exists."""
        checkpoint = self.checkpoint_store.get_checkpoint(payload.checkpoint_id)
        
        if checkpoint is None:
            raise IngestErrorBuilder(
                category=ErrorCategory.STATE,
                context=context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.DEPENDENCY_MISSING,
                message=f"checkpoint {payload.checkpoint_id} does not exist",
                source="checkpoint_store",
                actual_value=payload.checkpoint_id
            ).build()
    
    def _verify_checkpoint_validated(self, payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """Ensure checkpoint has been validated."""
        if self.policy.require_checkpoint_validation:
            checkpoint = self.checkpoint_store.get_checkpoint(payload.checkpoint_id)
            
            if checkpoint and not checkpoint.validated:
                raise IngestErrorBuilder(
                    category=ErrorCategory.STATE,
                    context=context,
                    recovery_hint=RecoveryHint.REQUIRES_MANUAL_REVIEW
                ).add_cause(
                    code=IngestErrorCode.PRECONDITION_FAILED,
                    message=f"checkpoint {payload.checkpoint_id} is not validated",
                    source="checkpoint_validator",
                    actual_value=payload.checkpoint_id
                ).build()
    
    def _verify_checkpoint_hash(self, payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """Verify checkpoint hash matches expected value."""
        if not self.checkpoint_store.validate_checkpoint_hash(
            payload.checkpoint_id,
            payload.checkpoint_hash
        ):
            raise CommonIngestErrors.checksum_mismatch(
                entity_id=payload.checkpoint_id,
                expected_checksum=payload.checkpoint_hash,
                actual_checksum="<unknown>",
                context=context
            )
    
    def _verify_replay_context(self, payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """Verify replay context hash if provided."""
        if payload.replay_context_hash and payload.parent_lineage_node:
            parent_node = self.lineage_store.get_lineage_node(payload.parent_lineage_node)
            
            if parent_node is None:
                raise IngestErrorBuilder(
                    category=ErrorCategory.STATE,
                    context=context,
                    recovery_hint=RecoveryHint.FATAL
                ).add_cause(
                    code=IngestErrorCode.DEPENDENCY_MISSING,
                    message=f"parent_lineage_node {payload.parent_lineage_node} not found",
                    source="lineage_store",
                    actual_value=payload.parent_lineage_node
                ).build()
            
            if parent_node.replay_context_hash != payload.replay_context_hash:
                raise CommonIngestErrors.checksum_mismatch(
                    entity_id=payload.parent_lineage_node,
                    expected_checksum=parent_node.replay_context_hash,
                    actual_checksum=payload.replay_context_hash,
                    context=context
                )
    
    def _verify_lineage_chain(self, payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """
        Verify complete lineage chain with no skipped nodes and hash integrity.
        
        Performs two checks:
        1. Chain continuity via lineage_store.verify_lineage_chain()
        2. Lineage hash integrity - recomputes hash from chain and verifies match
        """
        if payload.parent_lineage_node:
            # Check chain continuity
            if not self.lineage_store.verify_lineage_chain(
                payload.parent_lineage_node,
                payload.checkpoint_id
            ):
                raise IngestErrorBuilder(
                    category=ErrorCategory.STATE,
                    context=context,
                    recovery_hint=RecoveryHint.FATAL
                ).add_cause(
                    code=IngestErrorCode.STATE_CONFLICT,
                    message="incomplete or broken lineage chain detected",
                    source="lineage_verifier",
                    actual_value=payload.parent_lineage_node
                ).build()
            
            # Verify lineage hash integrity
            # Reconstruct lineage chain and compute expected hash
            lineage_chain = self._reconstruct_lineage_chain(payload.parent_lineage_node)
            expected_lineage_hash = self._compute_lineage_hash(lineage_chain)
            
            if expected_lineage_hash != payload.lineage_hash:
                raise CommonIngestErrors.checksum_mismatch(
                    entity_id=payload.parent_lineage_node,
                    expected_checksum=expected_lineage_hash,
                    actual_checksum=payload.lineage_hash,
                    context=context
                )
    
    def _reconstruct_lineage_chain(self, node_id: str) -> List[LineageNode]:
        """Reconstruct lineage chain from node to root."""
        chain = []
        current_id = node_id
        
        while current_id:
            node = self.lineage_store.get_lineage_node(current_id)
            if node is None:
                break
            chain.append(node)
            current_id = node.parent_node_id
        
        return chain
    
    def _compute_lineage_hash(self, chain: List[LineageNode]) -> str:
        """
        Compute deterministic hash of lineage chain.
        
        Hash includes: node IDs, checkpoint IDs, sequence numbers, fork flags.
        This ensures lineage integrity cannot be tampered with.
        """
        hash_components = []
        for node in chain:
            hash_components.extend([
                node.node_id,
                node.checkpoint_id,
                str(node.sequence_number),
                str(node.is_fork),
                node.replay_context_hash
            ])
        
        hash_string = '|'.join(hash_components)
        return hashlib.sha256(hash_string.encode('utf-8')).hexdigest()
    
    def _verify_no_branch_ambiguity(self, payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """Ensure forks are explicit and audited."""
        if payload.parent_lineage_node:
            parent_node = self.lineage_store.get_lineage_node(payload.parent_lineage_node)
            
            if parent_node and parent_node.is_fork:
                if not parent_node.fork_audit_ref:
                    raise IngestErrorBuilder(
                        category=ErrorCategory.STATE,
                        context=context,
                        recovery_hint=RecoveryHint.REQUIRES_MANUAL_REVIEW
                    ).add_cause(
                        code=IngestErrorCode.STATE_CONFLICT,
                        message=f"fork detected but not audited at node {payload.parent_lineage_node}",
                        source="lineage_verifier",
                        actual_value=payload.parent_lineage_node
                    ).build()


# ============================================================================
# RECOVERY SCOPE GUARD (BLAST-RADIUS ENFORCER)
# ============================================================================

class RecoveryScopeGuard:
    """
    Ensures recovery doesn't exceed authority.
    
    A workflow repair cannot mutate global state.
    An account recovery cannot touch unrelated content.
    """
    
    def __init__(self, policy: RecoveryIngestPolicy):
        self.policy = policy
    
    def verify(self, payload: RecoveryPayload, ingest_context: IngestContext) -> None:
        """
        Verify recovery scope is authorized and bounded.
        
        Raises BaseIngestError on scope violations.
        """
        error_context = IngestErrorContext(
            pipeline_step="recovery_scope_guard",
            run_id=ingest_context.run_id,
            input_id=payload.recovery_id,
            entity_type="recovery",
            entity_id=payload.recovery_id
        )
        
        self._verify_scope_size(payload, error_context)
        self._verify_scope_authority(payload, error_context)
        self._verify_no_scope_escalation(payload, error_context)
    
    def _verify_scope_size(self, payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """Ensure affected entity count doesn't exceed limits."""
        entity_count = len(payload.affected_entity_ids)
        
        if entity_count > self.policy.max_scope_size:
            raise CommonIngestErrors.quota_exceeded(
                quota_name="max_scope_size",
                limit=self.policy.max_scope_size,
                current=entity_count,
                context=context
            )
    
    def _verify_scope_authority(self, payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """Verify recovery has authority over declared scope."""
        # Workflow-scoped recovery cannot affect global scope
        if payload.scope_type == ScopeType.WORKFLOW:
            if any(self._is_global_entity(eid) for eid in payload.affected_entity_ids):
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="workflow_scope_boundary",
                    violation_message="workflow-scoped recovery cannot affect global entities",
                    context=context
                )
        
        # Account-scoped recovery must be confined to account
        if payload.scope_type == ScopeType.ACCOUNT:
            if any(not self._belongs_to_account(eid, payload.scope_id) for eid in payload.affected_entity_ids):
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="account_scope_boundary",
                    violation_message="account-scoped recovery affects entities outside account",
                    context=context
                )
    
    def _verify_no_scope_escalation(self, payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """
        Prevent privilege escalation through recovery.
        
        Global scope recoveries require explicit manual intervention authorization.
        This is enforced at the invariant layer, but we verify consistency here.
        """
        # This check is redundant with invariant layer, but provides defense in depth
        # The invariant layer will catch violations, so this is primarily for audit
        if payload.scope_type == ScopeType.GLOBAL:
            if payload.source_subsystem != RecoverySource.MANUAL_INTERVENTION:
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="scope_escalation_prevention",
                    violation_message=(
                        f"global scope recovery requires manual intervention authorization, "
                        f"got source: {payload.source_subsystem.value}"
                    ),
                    context=context
                )
    
    def _is_global_entity(self, entity_id: str) -> bool:
        """Check if entity_id represents a global resource."""
        # Implementation would check entity naming conventions or registry
        return entity_id.startswith('global:')
    
    def _belongs_to_account(self, entity_id: str, account_id: str) -> bool:
        """Check if entity belongs to the specified account."""
        # Implementation would verify entity ownership
        return entity_id.startswith(f'account:{account_id}:')


# ============================================================================
# RECOVERY NORMALIZER (CANONICALIZER)
# ============================================================================

class RecoveryNormalizer:
    """
    Normalizes recovered data without reinterpretation.
    
    Rules: Lossless, Deterministic, No inference, No enrichment.
    Recovered data must exactly match canonical shapes.
    """
    
    def normalize(self, payload: RecoveryPayload) -> Dict[str, Any]:
        """
        Normalize payload data to canonical form.
        
        Returns normalized data dictionary.
        """
        normalized = {}
        
        # Canonical timestamp normalization (UTC, ISO format)
        normalized['timestamp'] = self._normalize_timestamp(payload.timestamp)
        
        # Stable ID normalization
        normalized['recovery_id'] = self._normalize_id(payload.recovery_id)
        normalized['checkpoint_id'] = self._normalize_id(payload.checkpoint_id)
        normalized['scope_id'] = self._normalize_id(payload.scope_id)
        
        # Deterministic ordering of entity IDs
        normalized['affected_entity_ids'] = self._normalize_entity_ids(
            payload.affected_entity_ids
        )
        
        # Schema normalization (enforce null-field rules)
        normalized['payload_data'] = self._normalize_schema(payload.payload_data)
        
        return normalized
    
    def _normalize_timestamp(self, ts: datetime) -> str:
        """Convert timestamp to canonical UTC ISO format."""
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).isoformat()
    
    def _normalize_id(self, id_value: str) -> str:
        """Normalize ID to stable canonical form."""
        return id_value.strip().lower()
    
    def _normalize_entity_ids(self, entity_ids: Tuple[str, ...]) -> List[str]:
        """Sort entity IDs deterministically."""
        return sorted([self._normalize_id(eid) for eid in entity_ids])
    
    def _normalize_schema(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize schema fields without interpretation.
        
        Enforces null-field rules and canonical field ordering.
        """
        # Sort keys for deterministic serialization
        normalized = {}
        for key in sorted(data.keys()):
            value = data[key]
            
            # Normalize nested dictionaries recursively
            if isinstance(value, dict):
                normalized[key] = self._normalize_schema(value)
            # Normalize lists deterministically where order doesn't matter
            elif isinstance(value, list) and key.endswith('_set'):
                normalized[key] = sorted(value)
            else:
                normalized[key] = value
        
        return normalized


# ============================================================================
# RECOVERY DEDUPLICATOR (IDEMPOTENCY)
# ============================================================================

class RecoveryDeduplicator:
    """
    Determines whether recovery output is NEW, DUPLICATE, or CONFLICTING.
    
    No silent overwrite of recovered state. Ever.
    """
    
    def __init__(self, fact_store: RecoveryFactStore):
        self.fact_store = fact_store
    
    def check(self, payload: RecoveryPayload, normalized_data: Optional[Dict[str, Any]] = None) -> DeduplicationStatus:
        """
        Check deduplication status of recovery payload.
        
        Args:
            payload: Recovery payload to check
            normalized_data: Optional pre-normalized data (for consistent hashing)
        
        Returns:
            DeduplicationStatus.NEW - proceed with ingestion
            DeduplicationStatus.DUPLICATE - safe no-op
            DeduplicationStatus.CONFLICTING - fatal error
        """
        # Check by recovery_id
        existing_by_id = self.fact_store.get_by_recovery_id(payload.recovery_id)
        
        if existing_by_id:
            return self._compare_with_existing(payload, existing_by_id, normalized_data)
        
        # Check by deduplication key (using normalized data if available)
        dedup_key = self._compute_dedup_key(payload, normalized_data)
        existing_by_key = self.fact_store.get_by_dedup_key(dedup_key)
        
        if existing_by_key:
            return self._compare_with_existing(payload, existing_by_key, normalized_data)
        
        return DeduplicationStatus.NEW
    
    def _compute_dedup_key(self, payload: RecoveryPayload, normalized_data: Optional[Dict[str, Any]] = None) -> str:
        """
        Compute deduplication key from payload attributes.
        
        Key includes: checkpoint_id, lineage_hash, scope, normalized_payload_hash
        
        Args:
            payload: Recovery payload
            normalized_data: Optional pre-normalized data (if available)
            
        Note: Uses normalized payload hash for deterministic deduplication.
        """
        # Use normalized payload hash if available, otherwise compute from raw
        if normalized_data and 'payload_data' in normalized_data:
            # Hash normalized payload data
            canonical_json = json.dumps(
                normalized_data['payload_data'],
                sort_keys=True,
                separators=(',', ':')
            )
            payload_hash = hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()
        else:
            # Fallback to raw payload hash (should not happen in normal flow)
            payload_hash = payload.compute_payload_hash()
        
        key_components = [
            payload.checkpoint_id,
            payload.lineage_hash,
            payload.scope_type.value,
            payload.scope_id,
            payload_hash
        ]
        
        key_string = '|'.join(key_components)
        return hashlib.sha256(key_string.encode('utf-8')).hexdigest()
    
    def _compare_with_existing(
        self,
        payload: RecoveryPayload,
        existing: RecoveryFact,
        normalized_data: Optional[Dict[str, Any]] = None
    ) -> DeduplicationStatus:
        """
        Compare payload with existing recovery fact.
        
        Args:
            payload: Recovery payload to compare
            existing: Existing recovery fact
            normalized_data: Optional pre-normalized data (for consistent hashing)
        
        Returns DUPLICATE if identical, CONFLICTING if different.
        """
        # Use normalized hash if available, otherwise fallback to raw
        if normalized_data and 'payload_data' in normalized_data:
            canonical_json = json.dumps(
                normalized_data['payload_data'],
                sort_keys=True,
                separators=(',', ':')
            )
            payload_hash = hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()
        else:
            payload_hash = payload.compute_payload_hash()
        
        # Check all critical fields for identity
        if (existing.recovery_id == payload.recovery_id and
            existing.checkpoint_id == payload.checkpoint_id and
            existing.lineage_hash == payload.lineage_hash and
            existing.scope_type == payload.scope_type.value and
            existing.scope_id == payload.scope_id and
            existing.payload_hash == payload_hash):
            return DeduplicationStatus.DUPLICATE
        
        return DeduplicationStatus.CONFLICTING


# ============================================================================
# RECOVERY INGEST INVARIANTS (ABSOLUTE)
# ============================================================================

class RecoveryIngestInvariants:
    """
    Enforces absolute invariants for recovery ingestion.
    
    Violation → ingestion hard stop + audit escalation.
    """
    
    @staticmethod
    def enforce(
        payload: RecoveryPayload,
        policy: RecoveryIngestPolicy,
        context: IngestErrorContext
    ) -> None:
        """
        Enforce all invariants.
        
        Raises BaseIngestError on any violation.
        """
        RecoveryIngestInvariants._no_recovery_without_checkpoint(payload, context)
        RecoveryIngestInvariants._no_recovery_without_audit(payload, policy, context)
        RecoveryIngestInvariants._no_schema_drift(payload, policy, context)
        RecoveryIngestInvariants._no_scope_escalation(payload, context)
        RecoveryIngestInvariants._no_future_state(payload, context)
        RecoveryIngestInvariants._no_partial_lineage(payload, context)
    
    @staticmethod
    def _no_recovery_without_checkpoint(
        payload: RecoveryPayload,
        context: IngestErrorContext
    ) -> None:
        """Invariant: Every recovery must have a checkpoint."""
        if not payload.checkpoint_id or not payload.checkpoint_hash:
            raise CommonIngestErrors.invariant_broken(
                invariant_name="checkpoint_required",
                violation_message="recovery without checkpoint",
                context=context
            )
    
    @staticmethod
    def _no_recovery_without_audit(
        payload: RecoveryPayload,
        policy: RecoveryIngestPolicy,
        context: IngestErrorContext
    ) -> None:
        """Invariant: Every recovery must have an audit trail."""
        if policy.require_audit_package and not payload.audit_package_ref:
            raise CommonIngestErrors.invariant_broken(
                invariant_name="audit_trail_required",
                violation_message="recovery without audit trail",
                context=context
            )
    
    @staticmethod
    def _no_schema_drift(
        payload: RecoveryPayload,
        policy: RecoveryIngestPolicy,
        context: IngestErrorContext
    ) -> None:
        """
        Invariant: Schema version must be compatible with canonical.
        
        Note: This invariant uses compatibility check rather than strict equality
        to allow forward-compatible schema versions. The validator layer performs
        the actual compatibility check via schema registry.
        
        For invariant purposes, we verify the schema version is present and valid.
        The validator's schema_registry.is_compatible() is the authoritative check.
        """
        # Schema compatibility is checked by validator via schema_registry
        # Invariant only ensures schema version is present (already validated in __post_init__)
        if not payload.schema_version or not payload.schema_version.strip():
            raise CommonIngestErrors.invariant_broken(
                invariant_name="schema_version_presence",
                violation_message="schema_version is missing or empty",
                context=context
            )
    
    @staticmethod
    def _no_scope_escalation(payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """
        Invariant: Recovery cannot escalate its own scope.
        
        Global scope recoveries require explicit manual intervention authorization.
        """
        # Global scope recoveries must be explicitly authorized via manual intervention
        if payload.scope_type == ScopeType.GLOBAL:
            if payload.source_subsystem != RecoverySource.MANUAL_INTERVENTION:
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="scope_escalation_prevention",
                    violation_message=(
                        f"global scope recovery requires manual intervention authorization, "
                        f"got source: {payload.source_subsystem.value}"
                    ),
                    context=context
                )
    
    @staticmethod
    def _no_future_state(payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """Invariant: No future-dated recovery state."""
        if payload.timestamp > datetime.now(timezone.utc):
            raise CommonIngestErrors.invariant_broken(
                invariant_name="timestamp_not_future",
                violation_message="future-dated recovery state",
                context=context
            )
    
    @staticmethod
    def _no_partial_lineage(payload: RecoveryPayload, context: IngestErrorContext) -> None:
        """Invariant: Lineage must be complete or absent."""
        has_lineage_hash = bool(payload.lineage_hash)
        has_parent = bool(payload.parent_lineage_node)
        has_context = bool(payload.replay_context_hash)
        
        # Either all lineage fields present or none
        if has_parent or has_context:
            if not (has_lineage_hash and has_parent):
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="lineage_completeness",
                    violation_message="partial lineage detected",
                    context=context
                )


# ============================================================================
# RECOVERY INGESTOR (ORCHESTRATOR)
# ============================================================================

class RecoveryIngestor:
    """
    Primary entrypoint for recovery state ingestion.
    
    Orchestrates validation, verification, normalization, deduplication,
    and emission of immutable recovery facts.
    
    Failure anywhere → zero state admitted.
    """
    
    def __init__(
        self,
        policy: RecoveryIngestPolicy,
        validator: RecoveryValidator,
        lineage_verifier: RecoveryLineageVerifier,
        scope_guard: RecoveryScopeGuard,
        normalizer: RecoveryNormalizer,
        deduplicator: RecoveryDeduplicator,
        fact_store: RecoveryFactStore
    ):
        self.policy = policy
        self.validator = validator
        self.lineage_verifier = lineage_verifier
        self.scope_guard = scope_guard
        self.normalizer = normalizer
        self.deduplicator = deduplicator
        self.fact_store = fact_store
    
    def ingest(self, payload: RecoveryPayload, ingest_context: IngestContext) -> RecoveryFact:
        """
        Ingest recovery payload through complete validation pipeline.
        
        Args:
            payload: Recovery payload to ingest
            ingest_context: Immutable ingestion execution context
            
        Returns:
            RecoveryFact - immutable record of admitted recovery
        
        Raises:
            BaseIngestError - on any validation, verification, or policy failure
        
        Conceptual flow:
            1. Build error context
            2. Enforce invariants
            3. Normalize data (before hashing)
            4. Validate payload
            5. Verify lineage
            6. Guard scope
            7. Deduplicate (atomic)
            8. Emit immutable recovery facts
        """
        # Build error context for all operations
        error_context = IngestErrorContext(
            pipeline_step="recovery_ingestion",
            run_id=ingest_context.run_id,
            input_id=payload.recovery_id,
            entity_type="recovery",
            entity_id=payload.recovery_id
        )
        
        try:
            # Step 1: Enforce invariants (with error context)
            RecoveryIngestInvariants.enforce(payload, self.policy, error_context)
            
            # Step 2: Normalize data BEFORE hashing (critical for deterministic deduplication)
            normalized_data = self.normalizer.normalize(payload)
            
            # Step 3: Validate payload (requires ingest_context)
            self.validator.validate(payload, ingest_context)
            
            # Step 4: Verify lineage integrity (requires ingest_context)
            self.lineage_verifier.verify(payload, ingest_context)
            
            # Step 5: Guard scope boundaries (requires ingest_context)
            self.scope_guard.verify(payload, ingest_context)
            
            # Step 6: Compute deduplication key from normalized data
            dedup_key = self.deduplicator._compute_dedup_key(payload, normalized_data)
            
            # Step 7: Create immutable recovery fact with normalized payload hash
            normalized_payload_hash = self._compute_normalized_hash(normalized_data['payload_data'])
            
            fact = RecoveryFact(
                recovery_id=payload.recovery_id,
                recovery_type=payload.recovery_type.value,
                source_subsystem=payload.source_subsystem.value,
                checkpoint_id=payload.checkpoint_id,
                checkpoint_hash=payload.checkpoint_hash,
                lineage_hash=payload.lineage_hash,
                scope_type=payload.scope_type.value,
                scope_id=payload.scope_id,
                affected_entity_ids=tuple(normalized_data['affected_entity_ids']),
                schema_version=payload.schema_version,
                timestamp=payload.timestamp,
                audit_package_ref=payload.audit_package_ref,
                payload_hash=normalized_payload_hash,
                ingestion_timestamp=datetime.now(timezone.utc)
            )
            
            # Step 8: Atomically persist with deduplication enforcement
            persisted_fact = self.fact_store.persist_atomic(fact, dedup_key)
            
            return persisted_fact
            
        except BaseIngestError:
            # Re-raise structured errors
            raise
        except Exception as e:
            # Wrap unexpected errors with proper error context
            raise IngestErrorBuilder(
                category=ErrorCategory.INFRA,
                context=error_context,
                recovery_hint=RecoveryHint.REQUIRES_MANUAL_REVIEW
            ).add_cause(
                code=IngestErrorCode.STORAGE_WRITE_FAILED,
                message=f"unexpected error during ingestion: {str(e)}",
                source="recovery_ingestor",
                actual_value=str(e)
            ).build() from e
    
    def _compute_normalized_hash(self, normalized_payload_data: Dict[str, Any]) -> str:
        """Compute hash of normalized payload data."""
        canonical_json = json.dumps(
            normalized_payload_data,
            sort_keys=True,
            separators=(',', ':')
        )
        return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()


# ============================================================================
# FACTORY & SETUP
# ============================================================================

def create_recovery_ingestor(
    policy: RecoveryIngestPolicy,
    checkpoint_store: CheckpointStore,
    lineage_store: LineageStore,
    audit_store: AuditStore,
    schema_registry: SchemaRegistry,
    fact_store: RecoveryFactStore
) -> RecoveryIngestor:
    """
    Factory function to create fully configured RecoveryIngestor.
    
    This is the recommended way to instantiate the ingestion pipeline.
    """
    validator = RecoveryValidator(
        policy=policy,
        audit_store=audit_store,
        schema_registry=schema_registry
    )
    
    lineage_verifier = RecoveryLineageVerifier(
        checkpoint_store=checkpoint_store,
        lineage_store=lineage_store,
        policy=policy
    )
    
    scope_guard = RecoveryScopeGuard(policy=policy)
    
    normalizer = RecoveryNormalizer()
    
    deduplicator = RecoveryDeduplicator(fact_store=fact_store)
    
    return RecoveryIngestor(
        policy=policy,
        validator=validator,
        lineage_verifier=lineage_verifier,
        scope_guard=scope_guard,
        normalizer=normalizer,
        deduplicator=deduplicator,
        fact_store=fact_store
    )

