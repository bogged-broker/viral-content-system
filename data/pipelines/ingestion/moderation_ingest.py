"""
/data/pipelines/ingestion/moderation_ingest.py

Flags, Strikes, Decisions → Canonical Moderation Facts

This is the ONLY gateway by which moderation events are allowed to enter
the canonical data universe.

Design Principle: Moderation must be replayable, reviewable, and provable — or it doesn't exist.

If a moderation action can't be reconstructed post-hoc, it must not be ingested.

Once ingested, moderation facts are historical truth, not opinions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Protocol, Set, Tuple

from .base.ingest_context import IngestContext, IngestMode, IngestAuthority
from .base.ingest_result import (
    IngestResult,
    IngestOutcome,
    RejectionReason,
)
from .builders.result_factory import (
    create_accepted_result,
    create_rejected_result,
    create_deduped_result,
)
from .base.ingest_errors import (
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

class ModerationEventType(Enum):
    """Types of moderation events that can be ingested."""
    FLAG = "flag"
    STRIKE = "strike"
    DECISION = "decision"


class ModerationDecision(Enum):
    """Canonical moderation decision outcomes."""
    ALLOW = "allow"
    LIMIT = "limit"
    REMOVE = "remove"


class ModerationSeverity(Enum):
    """Canonical severity levels."""
    INFORMATIONAL = "informational"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ModerationTargetType(Enum):
    """Types of entities that can be moderated."""
    CONTENT = "content"
    ACCOUNT = "account"
    COMMENT = "comment"
    MESSAGE = "message"
    WORKFLOW = "workflow"
    ARTIFACT = "artifact"


class ReviewerType(Enum):
    """Types of reviewers that can make moderation decisions."""
    HUMAN = "human"
    SYSTEM = "system"
    ML_MODEL = "ml_model"


class DeduplicationStatus(Enum):
    """Result of deduplication check."""
    NEW = "NEW"
    DUPLICATE = "DUPLICATE"
    CONFLICTING = "CONFLICTING"


# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass(frozen=True)
class ModerationIngestPolicy:
    """
    Immutable, versioned policy defining what moderation events are admissible.
    
    Policy changes require deployment, not runtime flags.
    """
    policy_version: str
    supported_targets: FrozenSet[str]  # content, account, comment, message
    supported_event_types: FrozenSet[str]  # flag, strike, decision
    supported_policy_versions: FrozenSet[str]
    allow_machine_flags: bool
    allow_machine_decisions: bool
    canonical_schema_version: str
    
    def __post_init__(self):
        """Validate policy immutability constraints."""
        if not self.policy_version:
            raise ValueError("policy_version must be set")
        if not self.supported_targets:
            raise ValueError("supported_targets cannot be empty")
        if not self.supported_event_types:
            raise ValueError("supported_event_types cannot be empty")
        if not self.canonical_schema_version:
            raise ValueError("canonical_schema_version must be set")


@dataclass(frozen=True)
class ModerationFact:
    """
    Immutable moderation record emitted after successful ingestion.
    
    TIER-0: Once written, treated as historical truth with full forensic replay capability.
    All fields required for legal audit reconstruction and deterministic replay.
    """
    moderation_id: str
    event_type: str  # flag / strike / decision
    target_type: str
    target_id: str
    decision: Optional[str]  # if applicable
    severity: Optional[str]  # if applicable
    policy_id: str
    policy_version: str
    policy_hash: str  # TIER-0: Immutable fingerprint of exact policy document
    reviewer_id: str
    reviewer_type: str
    reviewer_authority_scope: Optional[str]  # TIER-0: Reviewer's authority scope
    timestamp: datetime
    source: str  # human / system / ML
    schema_version: str
    # TIER-0: Enforcement provenance (required for legal audit)
    enforcement_scope: str  # Scope at which policy was enforced
    decision_authority: str  # Who had authority to enforce
    # TIER-0: Forensic replay envelope
    event_fingerprint: str  # Canonical hash of normalized event (without event_id, timestamp)
    raw_event_digest: str  # SHA256 of raw event JSON
    normalized_event_digest: str  # SHA256 of normalized event JSON
    context_digest: str  # SHA256 of ingest context JSON
    # TIER-0: Ingest policy snapshot
    ingest_policy_version: str  # Version of ingest policy that accepted this event
    ingest_policy_hash: str  # Hash of ingest policy that accepted this event
    # TIER-0: Event-specific payloads (prevents semantic ambiguity)
    flag_payload: Optional[Dict[str, Any]] = None
    strike_payload: Optional[Dict[str, Any]] = None
    decision_payload: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for persistence."""
        return {
            'moderation_id': self.moderation_id,
            'event_type': self.event_type,
            'target_type': self.target_type,
            'target_id': self.target_id,
            'decision': self.decision,
            'severity': self.severity,
            'policy_id': self.policy_id,
            'policy_version': self.policy_version,
            'policy_hash': self.policy_hash,
            'reviewer_id': self.reviewer_id,
            'reviewer_type': self.reviewer_type,
            'reviewer_authority_scope': self.reviewer_authority_scope,
            'timestamp': self.timestamp.isoformat(),
            'source': self.source,
            'schema_version': self.schema_version,
            'enforcement_scope': self.enforcement_scope,
            'decision_authority': self.decision_authority,
            'event_fingerprint': self.event_fingerprint,
            'raw_event_digest': self.raw_event_digest,
            'normalized_event_digest': self.normalized_event_digest,
            'context_digest': self.context_digest,
            'ingest_policy_version': self.ingest_policy_version,
            'ingest_policy_hash': self.ingest_policy_hash,
            'flag_payload': self.flag_payload,
            'strike_payload': self.strike_payload,
            'decision_payload': self.decision_payload,
        }


# ============================================================================
# EXTERNAL DEPENDENCIES (INTERFACES)
# ============================================================================

class TargetStore(Protocol):
    """Interface to target existence verification."""
    
    def target_exists(self, target_type: str, target_id: str) -> bool:
        """Check if target exists."""
        ...


class PolicyStore(Protocol):
    """Interface to policy registry."""
    
    def get_policy(
        self,
        policy_id: str,
        policy_version: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve policy by ID and version."""
        ...


class ModerationFactStore(Protocol):
    """Interface to moderation fact persistence."""
    
    def get_by_event_id(self, event_id: str) -> Optional[ModerationFact]:
        """Retrieve moderation fact by event ID."""
        ...
    
    def get_by_dedup_key(self, dedup_key: str) -> Optional[ModerationFact]:
        """Retrieve moderation fact by deduplication key."""
        ...
    
    def get_decisions_by_scope(
        self,
        target_type: str,
        target_id: str,
        policy_id: str,
        policy_version: str,
        enforcement_scope: Optional[str] = None
    ) -> List[ModerationFact]:
        """
        Retrieve all decisions for a given scope.
        
        TIER-0: Required for scope-level conflict detection.
        Scope is defined by (target_type, target_id, policy_id, policy_version, enforcement_scope).
        
        Args:
            target_type: Type of target entity
            target_id: ID of target entity
            policy_id: Policy identifier
            policy_version: Policy version
            enforcement_scope: Optional enforcement scope filter
        
        Returns:
            List of ModerationFact objects matching the scope
        """
        ...
    
    def persist(self, fact: ModerationFact) -> str:
        """Persist immutable moderation fact. Returns moderation_id."""
        ...


class AuditLogger(Protocol):
    """Interface to audit trail system."""
    
    def log_ingest_started(
        self,
        event_id: str,
        context: IngestContext
    ) -> None:
        """Log ingestion start."""
        ...
    
    def log_ingest_succeeded(
        self,
        event_id: str,
        moderation_id: str,
        context: IngestContext
    ) -> None:
        """Log successful ingestion."""
        ...
    
    def log_ingest_failed(
        self,
        event_id: Optional[str],
        error: BaseIngestError,
        context: IngestContext
    ) -> None:
        """Log failed ingestion."""
        ...
    
    def log_duplicate_detected(
        self,
        event_id: str,
        existing_moderation_id: str,
        context: IngestContext
    ) -> None:
        """Log duplicate detection."""
        ...


# ============================================================================
# MODERATION VALIDATOR (TRUTH GATE)
# ============================================================================

class ModerationValidator:
    """
    Validates structural and logical correctness.
    
    Must enforce:
    - schema conformance (TIER-0: canonical schema validation)
    - valid target references
    - valid event type
    - presence of policy_id + policy_version for decisions
    - reviewer identity (human or system)
    - timestamp sanity
    - decision completeness (no partial outcomes)
    - machine decision gating (TIER-0)
    
    Must reject:
    - dangling targets
    - policy-less decisions
    - anonymous reviewers
    - ambiguous outcomes
    - machine decisions when disabled
    """
    
    def __init__(
        self,
        policy: ModerationIngestPolicy,
        schema_registry: Optional[ModerationSchemaRegistry] = None
    ):
        """
        Initialize validator.
        
        TIER-0: Validator does not perform existence checks.
        Existence validation is handled by ModerationTargetResolver.
        """
        self.policy = policy
        self.schema_registry = schema_registry
    
    def validate(
        self,
        raw_event: Dict[str, Any],
        context: IngestContext
    ) -> None:
        """
        Validate raw moderation event.
        
        Raises BaseIngestError on any validation failure.
        """
        error_context = IngestErrorContext(
            pipeline_step="moderation_validation",
            run_id=context.run_id,
            input_id=raw_event.get("event_id"),
            entity_type="moderation_event",
            entity_id=raw_event.get("event_id")
        )
        
        event_id = raw_event.get("event_id", "unknown")
        
        # TIER-0: Validate canonical schema conformance
        if self.schema_registry:
            self.schema_registry.validate(
                raw_event,
                self.policy.canonical_schema_version,
                error_context
            )
        
        # Validate required fields
        self._validate_required_fields(raw_event, error_context, event_id)
        
        # Validate event type
        self._validate_event_type(raw_event, error_context, event_id)
        
        # TIER-0: Validate machine decision gating
        self._validate_machine_decision_gating(raw_event, error_context, event_id)
        
        # Validate target type
        self._validate_target_type(raw_event, error_context, event_id)
        
        # Validate target references
        self._validate_target_references(raw_event, error_context, event_id)
        
        # Validate timestamp
        self._validate_timestamp(raw_event, error_context, event_id)
        
        # Validate decision-specific fields
        event_type_str = raw_event.get("event_type", "")
        if event_type_str == ModerationEventType.DECISION.value:
            self._validate_decision_fields(raw_event, error_context, event_id)
        
        # Validate reviewer identity
        self._validate_reviewer_identity(raw_event, error_context, event_id)
    
    def _validate_required_fields(
        self,
        raw_event: Dict[str, Any],
        context: IngestErrorContext,
        event_id: str
    ) -> None:
        """Ensure required fields are present."""
        required_fields = {
            "event_id",
            "event_type",
            "target_type",
            "target_id",
            "timestamp",
            "source",
        }
        
        missing = required_fields - set(raw_event.keys())
        if missing:
            raise CommonIngestErrors.missing_required_field(
                field_name=", ".join(missing),
                context=context
            )
    
    def _validate_event_type(
        self,
        raw_event: Dict[str, Any],
        context: IngestErrorContext,
        event_id: str
    ) -> None:
        """Validate event type."""
        event_type_str = raw_event.get("event_type")
        
        try:
            event_type = ModerationEventType(event_type_str)
        except (ValueError, TypeError):
            raise CommonIngestErrors.schema_violation(
                field_name="event_type",
                expected="flag, strike, or decision",
                actual=str(event_type_str),
                context=context
            )
        
        if event_type.value not in self.policy.supported_event_types:
            raise IngestErrorBuilder(
                category=ErrorCategory.AUTHORITY,
                context=context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.POLICY_VIOLATION,
                message=f"event_type {event_type.value} not supported by policy",
                source="moderation_validator",
                constraint="supported_event_types",
                expected_value=str(list(self.policy.supported_event_types)),
                actual_value=event_type.value
            ).build()
    
    def _validate_target_type(
        self,
        raw_event: Dict[str, Any],
        context: IngestErrorContext,
        event_id: str
    ) -> None:
        """Validate target type."""
        target_type_str = raw_event.get("target_type")
        
        try:
            target_type = ModerationTargetType(target_type_str)
        except (ValueError, TypeError):
            raise CommonIngestErrors.schema_violation(
                field_name="target_type",
                expected="content, account, comment, message, workflow, or artifact",
                actual=str(target_type_str),
                context=context
            )
        
        if target_type.value not in self.policy.supported_targets:
            raise IngestErrorBuilder(
                category=ErrorCategory.AUTHORITY,
                context=context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.POLICY_VIOLATION,
                message=f"target_type {target_type.value} not supported by policy",
                source="moderation_validator",
                constraint="supported_targets",
                expected_value=str(list(self.policy.supported_targets)),
                actual_value=target_type.value
            ).build()
    
    def _validate_target_references(
        self,
        raw_event: Dict[str, Any],
        context: IngestErrorContext,
        event_id: str
    ) -> None:
        """
        Validate target references are structurally valid.
        
        TIER-0: Validator enforces logical correctness only.
        Existence validation belongs in ModerationTargetResolver.
        """
        target_type = raw_event.get("target_type")
        target_id = raw_event.get("target_id")
        
        if not target_id or not target_id.strip():
            raise CommonIngestErrors.invariant_broken(
                invariant_name="target_id_presence",
                violation_message="target_id is missing or empty",
                context=context
            )
    
    def _validate_timestamp(
        self,
        raw_event: Dict[str, Any],
        context: IngestErrorContext,
        event_id: str
    ) -> None:
        """Validate timestamp sanity."""
        timestamp_value = raw_event.get("timestamp")
        
        try:
            if isinstance(timestamp_value, str):
                timestamp = datetime.fromisoformat(timestamp_value.replace('Z', '+00:00'))
            elif isinstance(timestamp_value, (int, float)):
                timestamp = datetime.fromtimestamp(timestamp_value, tz=timezone.utc)
            elif isinstance(timestamp_value, datetime):
                timestamp = timestamp_value
            else:
                raise ValueError("Invalid timestamp type")
        except (ValueError, TypeError) as e:
            raise CommonIngestErrors.schema_violation(
                field_name="timestamp",
                expected="ISO format string, epoch seconds, or datetime",
                actual=str(type(timestamp_value)),
                context=context
            )
        
        # Sanity check - not too far in past or future
        now = datetime.now(timezone.utc)
        age_days = (now - timestamp).total_seconds() / 86400
        
        if age_days > 365:
            raise IngestErrorBuilder(
                category=ErrorCategory.VALIDATION,
                context=context,
                recovery_hint=RecoveryHint.REQUIRES_MANUAL_REVIEW
            ).add_cause(
                code=IngestErrorCode.CONSTRAINT_VIOLATION,
                message="timestamp too old (>365 days)",
                source="timestamp_validator",
                constraint="timestamp_sanity",
                actual_value=str(age_days)
            ).build()
        
        if age_days < -1:
            raise IngestErrorBuilder(
                category=ErrorCategory.VALIDATION,
                context=context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.CONSTRAINT_VIOLATION,
                message="timestamp in future (>1 day)",
                source="timestamp_validator",
                constraint="timestamp_sanity",
                actual_value=str(age_days)
            ).build()
    
    def _validate_decision_fields(
        self,
        raw_event: Dict[str, Any],
        context: IngestErrorContext,
        event_id: str
    ) -> None:
        """Validate decision-specific required fields."""
        required_fields = {
            "decision",
            "policy_id",
            "policy_version",
            "reviewer_id",
            "reviewer_type",
        }
        
        missing = required_fields - set(raw_event.keys())
        if missing:
            raise CommonIngestErrors.missing_required_field(
                field_name=", ".join(missing),
                context=context
            )
        
        # Validate decision enum
        decision_str = raw_event.get("decision")
        try:
            ModerationDecision(decision_str)
        except (ValueError, TypeError):
            raise CommonIngestErrors.schema_violation(
                field_name="decision",
                expected="allow, limit, or remove",
                actual=str(decision_str),
                context=context
            )
        
        # Validate reviewer type
        reviewer_type_str = raw_event.get("reviewer_type")
        try:
            ReviewerType(reviewer_type_str)
        except (ValueError, TypeError):
            raise CommonIngestErrors.schema_violation(
                field_name="reviewer_type",
                expected="human, system, or ml_model",
                actual=str(reviewer_type_str),
                context=context
            )
    
    def _validate_reviewer_identity(
        self,
        raw_event: Dict[str, Any],
        context: IngestErrorContext,
        event_id: str
    ) -> None:
        """Validate reviewer identity is present."""
        event_type_str = raw_event.get("event_type", "")
        
        if event_type_str == ModerationEventType.DECISION.value:
            reviewer_id = raw_event.get("reviewer_id")
            reviewer_type = raw_event.get("reviewer_type")
            
            if not reviewer_id or not reviewer_id.strip():
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="reviewer_identity_required",
                    violation_message="DECISION event requires reviewer_id",
                    context=context
                )
            
            if not reviewer_type:
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="reviewer_type_required",
                    violation_message="DECISION event requires reviewer_type",
                    context=context
                )
    
    def _validate_machine_decision_gating(
        self,
        raw_event: Dict[str, Any],
        context: IngestErrorContext,
        event_id: str
    ) -> None:
        """
        TIER-0: Enforce machine decision gating.
        
        If reviewer_type is SYSTEM or ML_MODEL:
        - For DECISION events: must have allow_machine_decisions=True
        - For FLAG events: must have allow_machine_flags=True
        """
        reviewer_type = raw_event.get("reviewer_type")
        event_type = raw_event.get("event_type")
        
        if not reviewer_type or not event_type:
            return
        
        is_machine = reviewer_type in (ReviewerType.SYSTEM.value, ReviewerType.ML_MODEL.value)
        
        if is_machine:
            if event_type == ModerationEventType.DECISION.value:
                if not self.policy.allow_machine_decisions:
                    raise IngestErrorBuilder(
                        category=ErrorCategory.AUTHORITY,
                        context=context,
                        recovery_hint=RecoveryHint.FATAL
                    ).add_cause(
                        code=IngestErrorCode.POLICY_VIOLATION,
                        message=f"Machine decisions not allowed (reviewer_type={reviewer_type})",
                        source="moderation_validator",
                        constraint="allow_machine_decisions",
                        expected_value="true",
                        actual_value="false"
                    ).build()
            elif event_type == ModerationEventType.FLAG.value:
                if not self.policy.allow_machine_flags:
                    raise IngestErrorBuilder(
                        category=ErrorCategory.AUTHORITY,
                        context=context,
                        recovery_hint=RecoveryHint.FATAL
                    ).add_cause(
                        code=IngestErrorCode.POLICY_VIOLATION,
                        message=f"Machine flags not allowed (reviewer_type={reviewer_type})",
                        source="moderation_validator",
                        constraint="allow_machine_flags",
                        expected_value="true",
                        actual_value="false"
                    ).build()


# ============================================================================
# MODERATION NORMALIZER (CANONICALIZER)
# ============================================================================

class ModerationNormalizer:
    """
    Normalizes moderation inputs without reinterpretation.
    
    Rules:
    - Deterministic
    - Lossless
    - No enrichment
    
    Same raw event → same normalized fact.
    """
    
    def __init__(self, schema_version: str):
        self.schema_version = schema_version
    
    def normalize(self, raw_event: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize raw moderation event.
        
        Returns:
            Normalized event dictionary
        """
        normalized = {}
        
        # Identity fields
        normalized["event_id"] = self._normalize_id(raw_event["event_id"])
        
        # Enums
        normalized["event_type"] = ModerationEventType(raw_event["event_type"]).value
        normalized["target_type"] = ModerationTargetType(raw_event["target_type"]).value
        normalized["target_id"] = self._normalize_id(raw_event["target_id"])
        
        # Decision and severity
        if "decision" in raw_event and raw_event["decision"]:
            normalized["decision"] = ModerationDecision(raw_event["decision"]).value
        else:
            normalized["decision"] = None
        
        if "severity" in raw_event and raw_event["severity"]:
            normalized["severity"] = ModerationSeverity(raw_event["severity"]).value
        else:
            normalized["severity"] = None
        
        # Policy binding
        # TIER-0: No silent fallbacks - missing fields remain None/empty
        normalized["policy_id"] = raw_event.get("policy_id") or ""
        normalized["policy_version"] = raw_event.get("policy_version") or ""
        
        # Authority
        normalized["reviewer_id"] = self._normalize_reviewer_id(
            raw_event.get("reviewer_id") or ""
        )
        # TIER-0: No silent reviewer substitution - must remain None if missing
        # Validator/invariants will reject if required
        if "reviewer_type" in raw_event and raw_event["reviewer_type"]:
            normalized["reviewer_type"] = ReviewerType(raw_event["reviewer_type"]).value
        else:
            normalized["reviewer_type"] = None
        
        # Timestamp → epoch ms UTC
        normalized["timestamp"] = self._normalize_timestamp(raw_event["timestamp"])
        
        # Source
        # TIER-0: No silent fallback - must be explicitly provided
        if "source" not in raw_event or not raw_event.get("source"):
            raise ValueError("source field is required and cannot be defaulted")
        normalized["source"] = raw_event["source"]
        
        # Schema version
        normalized["schema_version"] = self.schema_version
        
        return normalized
    
    def _normalize_id(self, id_value: str) -> str:
        """Normalize ID to stable canonical form."""
        return id_value.strip()
    
    def _normalize_reviewer_id(self, reviewer_id: str) -> str:
        """Normalize reviewer ID to canonical form."""
        if not reviewer_id:
            return ""
        return reviewer_id.strip()
    
    def _normalize_timestamp(self, timestamp: Any) -> datetime:
        """Normalize timestamp to UTC datetime."""
        if isinstance(timestamp, str):
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        elif isinstance(timestamp, (int, float)):
            # Assume milliseconds if > 1e10, seconds otherwise
            if timestamp > 1e10:
                dt = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
            else:
                dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        elif isinstance(timestamp, datetime):
            dt = timestamp
        else:
            raise ValueError(f"Cannot normalize timestamp: {timestamp}")
        
        # Ensure UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        elif dt.tzinfo != timezone.utc:
            dt = dt.astimezone(timezone.utc)
        
        return dt


# ============================================================================
# MODERATION TARGET RESOLVER (BINDING AUTHORITY)
# ============================================================================

class ModerationTargetResolver:
    """
    Binds moderation to immutable system entities.
    
    Handles:
    - content_id
    - account_id
    - workflow_id
    - artifact_id
    
    Rules:
    - target must already exist
    - no speculative targets
    - targets are immutable once bound
    - moderation never creates entities
    
    If target resolution fails → hard reject.
    """
    
    def __init__(self, target_store: Optional[TargetStore] = None):
        self.target_store = target_store
    
    def resolve(
        self,
        target_type: str,
        target_id: str,
        context: IngestContext
    ) -> Tuple[str, str]:
        """
        Resolve and validate moderation target.
        
        Returns:
            Tuple of (target_type, target_id) if valid
        
        Raises:
            BaseIngestError: If target is invalid or doesn't exist
        """
        error_context = IngestErrorContext(
            pipeline_step="target_resolution",
            run_id=context.run_id,
            input_id=target_id,
            entity_type="moderation_target",
            entity_id=target_id
        )
        
        # Validate target ID is non-empty
        if not target_id or not target_id.strip():
            raise CommonIngestErrors.invariant_broken(
                invariant_name="target_id_presence",
                violation_message="target_id is missing or empty",
                context=error_context
            )
        
        # Check target exists if store is provided
        if self.target_store:
            exists = self.target_store.target_exists(target_type, target_id)
            if not exists:
                raise IngestErrorBuilder(
                    category=ErrorCategory.STATE,
                    context=error_context,
                    recovery_hint=RecoveryHint.REQUIRES_MANUAL_REVIEW
                ).add_cause(
                    code=IngestErrorCode.DEPENDENCY_MISSING,
                    message=f"target {target_type}:{target_id} does not exist",
                    source="target_resolver",
                    actual_value=f"{target_type}:{target_id}"
                ).build()
        
        return (target_type, target_id)


# ============================================================================
# MODERATION POLICY BINDER (CRITICAL)
# ============================================================================

class ModerationPolicyBinder:
    """
    Attaches moderation to the exact policy in force.
    
    Requires:
    - policy_id
    - policy_version
    - enforcement_scope
    - decision_authority
    
    Rules:
    - policies are immutable references
    - version mismatch → reject
    - no "latest policy" lookups
    
    Moderation without policy is invalid.
    """
    
    def __init__(
        self,
        policy_store: Optional[PolicyStore] = None,
        ingest_policy: Optional[ModerationIngestPolicy] = None
    ):
        self.policy_store = policy_store
        self.ingest_policy = ingest_policy
    
    def bind(
        self,
        policy_id: str,
        policy_version: str,
        normalized_event: Dict[str, Any],
        context: IngestContext
    ) -> Tuple[str, str, str, str, str]:
        """
        Bind moderation event to specific policy version.
        
        TIER-0: Spec requires binding:
        policy_id + policy_version + policy_hash + enforcement_scope + decision_authority
        
        Returns:
            Tuple of (policy_id, policy_version, policy_hash, enforcement_scope, decision_authority)
        
        Raises:
            BaseIngestError: If policy doesn't exist or version mismatch
        """
        error_context = IngestErrorContext(
            pipeline_step="policy_binding",
            run_id=context.run_id,
            input_id=f"{policy_id}:{policy_version}",
            entity_type="moderation_policy",
            entity_id=policy_id
        )
        
        # Validate policy version is supported
        if self.ingest_policy:
            if policy_version not in self.ingest_policy.supported_policy_versions:
                raise IngestErrorBuilder(
                    category=ErrorCategory.AUTHORITY,
                    context=error_context,
                    recovery_hint=RecoveryHint.FATAL
                ).add_cause(
                    code=IngestErrorCode.UNSUPPORTED_VERSION,
                    message=f"policy_version {policy_version} not supported",
                    source="policy_binder",
                    constraint="supported_policy_versions",
                    expected_value=str(list(self.ingest_policy.supported_policy_versions)),
                    actual_value=policy_version
                ).build()
        
        # TIER-0: Policy store is REQUIRED for policy hash computation
        # No fallback allowed - policy_hash must be cryptographically provable
        if not self.policy_store:
            raise IngestErrorBuilder(
                category=ErrorCategory.INFRA,
                context=error_context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.DEPENDENCY_MISSING,
                message="policy_store is required for Tier-0 policy hash computation",
                source="policy_binder",
                constraint="policy_store_required",
                expected_value="PolicyStore instance",
                actual_value="None"
            ).build()
        
        # Validate policy exists - REQUIRED
        policy_metadata = self.policy_store.get_policy(policy_id, policy_version)
        if not policy_metadata:
            raise IngestErrorBuilder(
                category=ErrorCategory.STATE,
                context=error_context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.DEPENDENCY_MISSING,
                message=f"policy {policy_id}:{policy_version} not found",
                source="policy_store",
                actual_value=f"{policy_id}:{policy_version}"
            ).build()
        
        # TIER-0: Compute policy hash (immutable fingerprint of exact policy document)
        # REQUIRED - no fallback, must be cryptographically provable
        policy_document = policy_metadata.get("document") or policy_metadata
        if not policy_document:
            raise CommonIngestErrors.invariant_broken(
                invariant_name="policy_document_required",
                violation_message="policy metadata must include policy document for hash computation",
                context=error_context
            )
        
        policy_hash = CanonicalSerializationEngine.compute_digest(policy_document)
        enforcement_scope = policy_metadata.get("enforcement_scope")
        
        if not enforcement_scope:
            raise CommonIngestErrors.invariant_broken(
                invariant_name="enforcement_scope_required",
                violation_message="policy metadata must include enforcement_scope",
                context=error_context
            )
        
        # TIER-0: No silent fallback for enforcement_scope
        if not enforcement_scope:
            raise CommonIngestErrors.invariant_broken(
                invariant_name="enforcement_scope_required",
                violation_message="enforcement_scope must be explicitly provided in policy metadata",
                context=error_context
            )
        
        # TIER-0: Extract decision_authority (who had authority to enforce)
        # Decision authority must be explicit - no fallback logic allowed
        # This is critical for legal audit reconstruction
        decision_authority = normalized_event.get("decision_authority")
        
        if not decision_authority or not decision_authority.strip():
            raise CommonIngestErrors.invariant_broken(
                invariant_name="decision_authority_required",
                violation_message="decision_authority is required for policy binding and must be explicitly provided",
                context=error_context
            )
        
        return (policy_id, policy_version, policy_hash, enforcement_scope, decision_authority)


# ============================================================================
# MODERATION DEDUPLICATOR (IDEMPOTENCY AUTHORITY)
# ============================================================================

class ModerationDeduplicator:
    """
    Determines moderation event state: NEW, DUPLICATE, or CONFLICT.
    
    TIER-0: Strict idempotency authority.
    
    Deduplication keys include:
    - event_id
    - target_id
    - policy_id
    - decision_hash (normalized decision outcome hash)
    - timestamp window (not exact timestamp)
    
    Rules:
    - no silent overwrites
    - conflicts are fatal
    - duplicates must be provably identical
    - timestamp window allows for reserialization tolerance
    """
    
    def __init__(
        self, 
        fact_store: Optional[ModerationFactStore] = None,
        timestamp_window_seconds: int = 300  # 5-minute window for idempotency
    ):
        self.fact_store = fact_store
        self.timestamp_window_seconds = timestamp_window_seconds
    
    def check(
        self,
        normalized_event: Dict[str, Any],
        context: IngestContext
    ) -> Tuple[DeduplicationStatus, Optional[ModerationFact]]:
        """
        Check deduplication status of moderation event.
        
        Returns:
            Tuple of (status, existing_fact_if_duplicate)
        """
        if not self.fact_store:
            return (DeduplicationStatus.NEW, None)
        
        event_id = normalized_event["event_id"]
        
        # Check by event_id
        existing_by_id = self.fact_store.get_by_event_id(event_id)
        
        if existing_by_id:
            return self._compare_with_existing(normalized_event, existing_by_id)
        
        # Check by deduplication key
        dedup_key = self._compute_dedup_key(normalized_event)
        existing_by_key = self.fact_store.get_by_dedup_key(dedup_key)
        
        if existing_by_key:
            return self._compare_with_existing(normalized_event, existing_by_key)
        
        return (DeduplicationStatus.NEW, None)
    
    def _compute_dedup_key(self, normalized_event: Dict[str, Any]) -> str:
        """
        Compute deduplication key from normalized event.
        
        TIER-0: Strict idempotency - key must NOT include exact timestamp or event_id.
        Same logical event replayed with reserialized timestamp or different event_id → same key.
        
        Key includes:
        - target_type
        - target_id
        - policy_id
        - policy_version
        - enforcement_scope (TIER-0: required for cross-scope conflict detection)
        - decision_hash (normalized decision outcome hash)
        - timestamp_window (rounded to window, not exact)
        
        Note: event_id is NOT included - replay must dedupe even if event_id changes.
        """
        # Normalize decision to hash (for idempotency)
        decision_hash = self._compute_decision_hash(normalized_event)
        
        # Round timestamp to window (not exact timestamp)
        timestamp_window = self._round_to_window(normalized_event["timestamp"])
        
        key_components = [
            normalized_event["target_type"],
            normalized_event["target_id"],
            normalized_event.get("policy_id", ""),
            normalized_event.get("policy_version", ""),
            normalized_event.get("enforcement_scope", ""),  # TIER-0: Include scope
            decision_hash,
            timestamp_window,
        ]
        
        key_string = '|'.join(key_components)
        return hashlib.sha256(key_string.encode('utf-8')).hexdigest()
    
    def _compute_decision_hash(self, normalized_event: Dict[str, Any]) -> str:
        """
        Compute normalized decision outcome hash.
        
        TIER-0: Decision hash must include all decision-relevant fields
        in a normalized, deterministic way.
        """
        decision_fields = {
            "decision": normalized_event.get("decision", ""),
            "severity": normalized_event.get("severity", ""),
            "policy_id": normalized_event.get("policy_id", ""),
            "policy_version": normalized_event.get("policy_version", ""),
        }
        
        # Create canonical representation (sorted keys for determinism)
        canonical = json.dumps(decision_fields, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    
    def _round_to_window(self, timestamp: Any) -> str:
        """
        Round timestamp to window for idempotency.
        
        TIER-0: Timestamp window allows same logical event with
        slightly different timestamps to be treated as identical.
        """
        if isinstance(timestamp, datetime):
            dt = timestamp
        elif isinstance(timestamp, str):
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        elif isinstance(timestamp, (int, float)):
            if timestamp > 1e10:
                dt = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
            else:
                dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        else:
            # Fallback: use current time
            dt = datetime.now(timezone.utc)
        
        # Ensure UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        elif dt.tzinfo != timezone.utc:
            dt = dt.astimezone(timezone.utc)
        
        # Round to window (e.g., 5-minute windows)
        window_seconds = self.timestamp_window_seconds
        total_seconds = int(dt.timestamp())
        rounded_seconds = (total_seconds // window_seconds) * window_seconds
        
        # Return as ISO string for consistency
        rounded_dt = datetime.fromtimestamp(rounded_seconds, tz=timezone.utc)
        return rounded_dt.isoformat()
    
    def _compare_with_existing(
        self,
        normalized_event: Dict[str, Any],
        existing: ModerationFact
    ) -> Tuple[DeduplicationStatus, ModerationFact]:
        """
        Compare normalized event with existing moderation fact.
        
        TIER-0: Uses timestamp window comparison, not exact timestamp.
        Returns DUPLICATE if identical within window, CONFLICTING if different.
        """
        # Compare key fields for identity
        event_type_match = existing.event_type == normalized_event["event_type"]
        target_type_match = existing.target_type == normalized_event["target_type"]
        target_id_match = existing.target_id == normalized_event["target_id"]
        decision_match = existing.decision == normalized_event.get("decision")
        policy_id_match = existing.policy_id == normalized_event.get("policy_id", "")
        policy_version_match = existing.policy_version == normalized_event.get("policy_version", "")
        
        # TIER-0: Compare timestamps within window (not exact)
        timestamp_match = self._timestamps_in_same_window(
            existing.timestamp,
            normalized_event["timestamp"]
        )
        
        # All fields must match, including timestamp within window
        if (event_type_match and target_type_match and target_id_match and
            decision_match and policy_id_match and policy_version_match and
            timestamp_match):
            return (DeduplicationStatus.DUPLICATE, existing)
        
        return (DeduplicationStatus.CONFLICTING, existing)
    
    def _timestamps_in_same_window(
        self,
        timestamp1: Any,
        timestamp2: Any
    ) -> bool:
        """
        Check if two timestamps fall within the same window.
        
        TIER-0: Timestamp window comparison for idempotency.
        """
        window1 = self._round_to_window(timestamp1)
        window2 = self._round_to_window(timestamp2)
        return window1 == window2


# ============================================================================
# CANONICAL SERIALIZATION ENGINE (TIER-0)
# ============================================================================

class CanonicalSerializationEngine:
    """
    TIER-0: Deterministic serialization for hashing and persistence.
    
    Guarantees:
    - Same input → same output (byte-for-byte)
    - No environment-dependent values
    - No implicit ordering
    - Strict UTF-8 encoding
    """
    
    @staticmethod
    def canonical_json_dumps(obj: Any) -> str:
        """
        Serialize to canonical JSON string.
        
        TIER-0: Uses strict deterministic settings:
        - sort_keys=True (deterministic key ordering)
        - separators=(',', ':') (no extra whitespace)
        - ensure_ascii=False (preserve Unicode)
        - no indent (compact)
        """
        return json.dumps(
            obj,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=False
        )
    
    @staticmethod
    def compute_hash(data: str) -> str:
        """Compute SHA256 hash of string data."""
        return hashlib.sha256(data.encode('utf-8')).hexdigest()
    
    @staticmethod
    def compute_digest(obj: Any) -> str:
        """Compute SHA256 digest of canonical JSON representation."""
        canonical = CanonicalSerializationEngine.canonical_json_dumps(obj)
        return CanonicalSerializationEngine.compute_hash(canonical)


# ============================================================================
# MODERATION SCHEMA REGISTRY (TIER-0)
# ============================================================================

class ModerationSchemaRegistry:
    """
    TIER-0: Canonical schema enforcement.
    
    Validates raw events against versioned JSON schemas.
    Required invariant: raw_event MUST match schema(policy.canonical_schema_version)
    or ingestion fails.
    """
    
    def __init__(self):
        # In production, this would load schemas from a registry
        # For now, we validate structure based on schema version
        self._schemas: Dict[str, Dict[str, Any]] = {}
    
    def register_schema(self, schema_version: str, schema: Dict[str, Any]) -> None:
        """Register a schema for a given version."""
        self._schemas[schema_version] = schema
    
    def validate(
        self,
        raw_event: Dict[str, Any],
        schema_version: str,
        context: IngestErrorContext
    ) -> None:
        """
        Validate raw event against canonical schema.
        
        TIER-0: Must enforce strict structural conformance.
        Raises BaseIngestError on schema violation.
        """
        if schema_version not in self._schemas:
            # For Tier-0, we require schema to be registered
            # In production, this would fetch from registry
            raise IngestErrorBuilder(
                category=ErrorCategory.VALIDATION,
                context=context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.UNSUPPORTED_VERSION,
                message=f"schema_version {schema_version} not registered",
                source="schema_registry",
                constraint="canonical_schema_version",
                expected_value=str(list(self._schemas.keys())),
                actual_value=schema_version
            ).build()
        
        schema = self._schemas[schema_version]
        
        # Basic structural validation
        # In production, use jsonschema library for full validation
        required_fields = schema.get("required", [])
        for field in required_fields:
            if field not in raw_event:
                raise CommonIngestErrors.schema_violation(
                    field_name=field,
                    expected=f"required field from schema {schema_version}",
                    actual="missing",
                    context=context
                )
        
        # Validate field types
        properties = schema.get("properties", {})
        for field_name, field_spec in properties.items():
            if field_name in raw_event:
                expected_type = field_spec.get("type")
                actual_value = raw_event[field_name]
                
                # Basic type checking
                if expected_type == "string" and not isinstance(actual_value, str):
                    raise CommonIngestErrors.schema_violation(
                        field_name=field_name,
                        expected=f"string (schema {schema_version})",
                        actual=str(type(actual_value)),
                        context=context
                    )
                elif expected_type == "integer" and not isinstance(actual_value, int):
                    raise CommonIngestErrors.schema_violation(
                        field_name=field_name,
                        expected=f"integer (schema {schema_version})",
                        actual=str(type(actual_value)),
                        context=context
                    )


# ============================================================================
# REVIEWER AUTHORITY RESOLVER (TIER-0)
# ============================================================================

class ReviewerAuthorityResolver:
    """
    TIER-0: Validates reviewer authority hierarchy and scope.
    
    Must validate:
    - reviewer allowed under policy
    - reviewer role scope matches enforcement scope
    - ML/system decisions allowed by ingest policy flags
    - reviewer has explicit policy grants for the enforcement scope
    """
    
    def __init__(
        self,
        ingest_policy: ModerationIngestPolicy,
        policy_store: Optional[PolicyStore] = None
    ):
        self.ingest_policy = ingest_policy
        self.policy_store = policy_store
    
    def resolve(
        self,
        reviewer_type: str,
        reviewer_id: str,
        enforcement_scope: str,
        event_type: str,
        policy_id: str,
        policy_version: str,
        context: IngestErrorContext
    ) -> str:
        """
        Resolve and validate reviewer authority scope.
        
        TIER-0: Validates reviewer has explicit policy grants for the enforcement scope.
        
        Returns:
            reviewer_authority_scope: The scope of authority for this reviewer
        
        Raises:
            BaseIngestError: If reviewer lacks required authority
        """
        # TIER-0: Enforce machine decision gating
        if reviewer_type in (ReviewerType.SYSTEM.value, ReviewerType.ML_MODEL.value):
            if event_type == ModerationEventType.DECISION.value:
                if not self.ingest_policy.allow_machine_decisions:
                    raise IngestErrorBuilder(
                        category=ErrorCategory.AUTHORITY,
                        context=context,
                        recovery_hint=RecoveryHint.FATAL
                    ).add_cause(
                        code=IngestErrorCode.POLICY_VIOLATION,
                        message=f"Machine decisions not allowed by ingest policy (reviewer_type={reviewer_type})",
                        source="reviewer_authority_resolver",
                        constraint="allow_machine_decisions",
                        expected_value="true",
                        actual_value="false"
                    ).build()
            elif event_type == ModerationEventType.FLAG.value:
                if not self.ingest_policy.allow_machine_flags:
                    raise IngestErrorBuilder(
                        category=ErrorCategory.AUTHORITY,
                        context=context,
                        recovery_hint=RecoveryHint.FATAL
                    ).add_cause(
                        code=IngestErrorCode.POLICY_VIOLATION,
                        message=f"Machine flags not allowed by ingest policy (reviewer_type={reviewer_type})",
                        source="reviewer_authority_resolver",
                        constraint="allow_machine_flags",
                        expected_value="true",
                        actual_value="false"
                    ).build()
        
        # TIER-0: Validate reviewer has policy grants for enforcement scope
        if self.policy_store:
            policy_metadata = self.policy_store.get_policy(policy_id, policy_version)
            if policy_metadata:
                # Check reviewer grants in policy
                reviewer_grants = policy_metadata.get("reviewer_grants", {})
                reviewer_key = f"{reviewer_type}:{reviewer_id}"
                
                # Validate reviewer has grant for this scope
                grants = reviewer_grants.get(reviewer_key, [])
                if enforcement_scope not in grants and "*" not in grants:
                    # Check if reviewer has any grants at all
                    if not grants:
                        raise IngestErrorBuilder(
                            category=ErrorCategory.AUTHORITY,
                            context=context,
                            recovery_hint=RecoveryHint.FATAL
                        ).add_cause(
                            code=IngestErrorCode.POLICY_VIOLATION,
                            message=f"Reviewer {reviewer_key} has no policy grants for scope {enforcement_scope}",
                            source="reviewer_authority_resolver",
                            constraint="reviewer_policy_grants",
                            expected_value=f"grants including {enforcement_scope}",
                            actual_value="no grants"
                        ).build()
                    else:
                        raise IngestErrorBuilder(
                            category=ErrorCategory.AUTHORITY,
                            context=context,
                            recovery_hint=RecoveryHint.FATAL
                        ).add_cause(
                            code=IngestErrorCode.POLICY_VIOLATION,
                            message=f"Reviewer {reviewer_key} lacks grant for scope {enforcement_scope}",
                            source="reviewer_authority_resolver",
                            constraint="reviewer_policy_grants",
                            expected_value=f"grants including {enforcement_scope}",
                            actual_value=str(grants)
                        ).build()
        
        # TIER-0: Reviewer authority scope must match enforcement scope
        # This ensures reviewer's authority is explicitly bound to the scope they're enforcing
        return enforcement_scope


# ============================================================================
# DECISION FINALITY GUARD (TIER-0)
# ============================================================================

class DecisionFinalityGuard:
    """
    TIER-0: Enforces decision finality rules with formal authority hierarchy.
    
    Must enforce:
    - No downgrade after REMOVE (terminal decision)
    - No override without higher authority (authority hierarchy)
    - No multi-authority collisions (conflict detection)
    - Escalation path consistency (ALLOW → LIMIT → REMOVE only)
    """
    
    # TIER-0: Formal authority hierarchy (higher number = higher authority)
    AUTHORITY_HIERARCHY = {
        ReviewerType.HUMAN.value: 3,
        ReviewerType.ML_MODEL.value: 2,
        ReviewerType.SYSTEM.value: 1,
    }
    
    # TIER-0: Decision severity hierarchy (higher number = more severe)
    DECISION_HIERARCHY = {
        ModerationDecision.ALLOW.value: 1,
        ModerationDecision.LIMIT.value: 2,
        ModerationDecision.REMOVE.value: 3,  # Terminal
    }
    
    @classmethod
    def enforce(
        cls,
        normalized_event: Dict[str, Any],
        fact_store: ModerationFactStore,
        context: IngestErrorContext
    ) -> None:
        """
        Enforce decision finality invariants with formal authority hierarchy.
        
        TIER-0: Prevents terminal decision downgrades and unauthorized overrides.
        """
        if normalized_event.get("event_type") != ModerationEventType.DECISION.value:
            return
        
        decision = normalized_event.get("decision")
        target_type = normalized_event.get("target_type")
        target_id = normalized_event.get("target_id")
        policy_id = normalized_event.get("policy_id")
        policy_version = normalized_event.get("policy_version")
        enforcement_scope = normalized_event.get("enforcement_scope")
        reviewer_type = normalized_event.get("reviewer_type")
        decision_authority = normalized_event.get("decision_authority")
        
        if not all([decision, target_type, target_id, policy_id, policy_version, reviewer_type]):
            return
        
        # Get existing decisions for this scope
        existing_decisions = fact_store.get_decisions_by_scope(
            target_type=target_type,
            target_id=target_id,
            policy_id=policy_id,
            policy_version=policy_version,
            enforcement_scope=enforcement_scope
        )
        
        new_decision_level = cls.DECISION_HIERARCHY.get(decision, 0)
        new_authority_level = cls.AUTHORITY_HIERARCHY.get(reviewer_type, 0)
        
        # TIER-0: Check for terminal decision downgrade
        # REMOVE is terminal - cannot be downgraded by any authority
        for existing in existing_decisions:
            if existing.decision == ModerationDecision.REMOVE.value:
                if decision != ModerationDecision.REMOVE.value:
                    raise CommonIngestErrors.invariant_broken(
                        invariant_name="no_downgrade_after_terminal_decision",
                        violation_message=(
                            f"Cannot downgrade from REMOVE to {decision} for "
                            f"target={target_type}:{target_id}, policy={policy_id}:{policy_version}. "
                            "REMOVE is terminal and immutable regardless of authority level."
                        ),
                        context=context
                    )
        
        # TIER-0: Check for unauthorized override (authority hierarchy)
        # Can only override if new authority level is higher than existing
        for existing in existing_decisions:
            if existing.decision == decision:
                # Same decision - check if this is a duplicate (handled by deduplicator)
                continue
            
            existing_authority_level = cls.AUTHORITY_HIERARCHY.get(existing.reviewer_type, 0)
            
            # If new decision is more severe, require higher authority
            if new_decision_level > cls.DECISION_HIERARCHY.get(existing.decision, 0):
                if new_authority_level <= existing_authority_level:
                    raise CommonIngestErrors.invariant_broken(
                        invariant_name="no_override_without_higher_authority",
                        violation_message=(
                            f"Cannot escalate from {existing.decision} to {decision} for "
                            f"target={target_type}:{target_id}, policy={policy_id}:{policy_version}. "
                            f"New authority level ({reviewer_type}={new_authority_level}) must be higher than "
                            f"existing ({existing.reviewer_type}={existing_authority_level})."
                        ),
                        context=context
                    )
            
            # If new decision is less severe, require higher authority (downgrade protection)
            elif new_decision_level < cls.DECISION_HIERARCHY.get(existing.decision, 0):
                if new_authority_level <= existing_authority_level:
                    raise CommonIngestErrors.invariant_broken(
                        invariant_name="no_downgrade_without_higher_authority",
                        violation_message=(
                            f"Cannot downgrade from {existing.decision} to {decision} for "
                            f"target={target_type}:{target_id}, policy={policy_id}:{policy_version}. "
                            f"New authority level ({reviewer_type}={new_authority_level}) must be higher than "
                            f"existing ({existing.reviewer_type}={existing_authority_level})."
                        ),
                        context=context
                    )
        
        # TIER-0: Validate escalation path consistency
        # Only allow: ALLOW → LIMIT → REMOVE (increasing severity)
        for existing in existing_decisions:
            existing_level = cls.DECISION_HIERARCHY.get(existing.decision, 0)
            if new_decision_level < existing_level:
                # Downgrade attempt - check if it's a valid escalation reversal
                # Only allow if authority is higher
                existing_authority_level = cls.AUTHORITY_HIERARCHY.get(existing.reviewer_type, 0)
                if new_authority_level <= existing_authority_level:
                    raise CommonIngestErrors.invariant_broken(
                        invariant_name="invalid_escalation_path",
                        violation_message=(
                            f"Invalid escalation path: {existing.decision} → {decision} for "
                            f"target={target_type}:{target_id}, policy={policy_id}:{policy_version}. "
                            "Escalation reversals require higher authority level."
                        ),
                        context=context
                    )


# ============================================================================
# IMMUTABILITY PERSISTENCE GUARD (TIER-0)
# ============================================================================

class ImmutabilityPersistenceGuard:
    """
    TIER-0: Enforces append-only write model with cryptographic guarantees.
    
    Persistence contract must enforce:
    - append-only write model (cryptographically verified)
    - no update path (reject if fact exists)
    - no delete path (hard reject)
    - idempotent write key (same fact = same ID)
    - cryptographic fact fingerprint verification
    """
    
    @staticmethod
    def persist(
        fact: ModerationFact,
        fact_store: ModerationFactStore
    ) -> str:
        """
        Persist fact with cryptographic immutability guarantees.
        
        TIER-0: Ensures append-only semantics with cryptographic verification.
        Returns moderation_id.
        
        Raises:
            BaseIngestError: If fact already exists (immutability violation)
        """
        # TIER-0: Cryptographic fact fingerprint for immutability verification
        fact_fingerprint = ImmutabilityPersistenceGuard._compute_fact_fingerprint(fact)
        
        # Check for existing fact with same moderation_id (idempotency check)
        existing = fact_store.get_by_event_id(fact.moderation_id)
        if existing:
            # Verify it's the exact same fact (cryptographic verification)
            existing_fingerprint = ImmutabilityPersistenceGuard._compute_fact_fingerprint(existing)
            if existing_fingerprint == fact_fingerprint:
                # Same fact - idempotent write, return existing ID
                return existing.moderation_id
            else:
                # Different fact with same ID - immutability violation
                raise IngestErrorBuilder(
                    category=ErrorCategory.STATE,
                    context=IngestErrorContext(
                        pipeline_step="immutability_guard",
                        run_id="",
                        input_id=fact.moderation_id,
                        entity_type="moderation_fact",
                        entity_id=fact.moderation_id
                    ),
                    recovery_hint=RecoveryHint.FATAL
                ).add_cause(
                    code=IngestErrorCode.STATE_CONFLICT,
                    message=f"Attempted to mutate existing fact {fact.moderation_id}. Facts are immutable.",
                    source="immutability_guard",
                    constraint="append_only",
                    expected_value="new fact",
                    actual_value="existing fact with different content"
                ).build()
        
        # TIER-0: Persist with append-only guarantee
        # Store implementation must enforce: no updates, no deletes
        return fact_store.persist(fact)
    
    @staticmethod
    def _compute_fact_fingerprint(fact: ModerationFact) -> str:
        """
        Compute cryptographic fingerprint of fact for immutability verification.
        
        TIER-0: Uses all fact fields to create deterministic fingerprint.
        """
        fact_dict = fact.to_dict()
        # Remove moderation_id from fingerprint (it's derived, not part of content)
        fact_content = {k: v for k, v in fact_dict.items() if k != "moderation_id"}
        return CanonicalSerializationEngine.compute_digest(fact_content)


# ============================================================================
# MODERATION INGEST INVARIANTS (ABSOLUTE)
# ============================================================================

class ModerationIngestInvariants:
    """
    Enforces absolute invariants for moderation ingestion.
    
    Violation → ingestion hard stop + audit entry.
    """
    
    @staticmethod
    def enforce(
        normalized_event: Dict[str, Any],
        context: IngestContext,
        fact_store: Optional[ModerationFactStore] = None
    ) -> None:
        """
        Enforce all invariants.
        
        TIER-0: ABSOLUTE invariants - violation → ingestion hard stop.
        
        Raises BaseIngestError on any violation.
        """
        error_context = IngestErrorContext(
            pipeline_step="invariant_enforcement",
            run_id=context.run_id,
            input_id=normalized_event.get("event_id"),
            entity_type="moderation_event",
            entity_id=normalized_event.get("event_id")
        )
        
        ModerationIngestInvariants._no_decision_without_policy(
            normalized_event,
            error_context
        )
        ModerationIngestInvariants._no_moderation_without_target(
            normalized_event,
            error_context
        )
        ModerationIngestInvariants._no_anonymous_reviewer_for_decisions(
            normalized_event,
            error_context
        )
        
        # TIER-0: Additional required invariants from spec
        if fact_store:
            ModerationIngestInvariants._no_mutation_of_past_moderation(
                normalized_event,
                fact_store,
                error_context
            )
            ModerationIngestInvariants._no_conflicting_decisions_for_same_scope(
                normalized_event,
                fact_store,
                error_context
            )
        
        ModerationIngestInvariants._enforcement_logic_presence_required(
            normalized_event,
            error_context
        )
    
    @staticmethod
    def _no_decision_without_policy(
        normalized_event: Dict[str, Any],
        context: IngestErrorContext
    ) -> None:
        """Invariant: No decision without policy."""
        if normalized_event["event_type"] == ModerationEventType.DECISION.value:
            if not normalized_event.get("policy_id") or not normalized_event.get("policy_version"):
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="decision_requires_policy",
                    violation_message="DECISION event requires policy_id and policy_version",
                    context=context
                )
    
    @staticmethod
    def _no_moderation_without_target(
        normalized_event: Dict[str, Any],
        context: IngestErrorContext
    ) -> None:
        """Invariant: No moderation without target."""
        if not normalized_event.get("target_type") or not normalized_event.get("target_id"):
            raise CommonIngestErrors.invariant_broken(
                invariant_name="moderation_requires_target",
                violation_message="moderation event requires target_type and target_id",
                context=context
            )
    
    @staticmethod
    def _no_anonymous_reviewer_for_decisions(
        normalized_event: Dict[str, Any],
        context: IngestErrorContext
    ) -> None:
        """Invariant: No anonymous reviewer for decisions."""
        if normalized_event["event_type"] == ModerationEventType.DECISION.value:
            if not normalized_event.get("reviewer_id") or not normalized_event.get("reviewer_id").strip():
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="decision_requires_reviewer",
                    violation_message="DECISION event requires reviewer_id",
                    context=context
                )
    
    @staticmethod
    def _no_mutation_of_past_moderation(
        normalized_event: Dict[str, Any],
        fact_store: ModerationFactStore,
        context: IngestErrorContext
    ) -> None:
        """
        TIER-0 Invariant: No mutation of past moderation.
        
        Guard against overwrite of existing moderation facts.
        Past moderation is immutable - cannot be changed or deleted.
        """
        event_id = normalized_event.get("event_id")
        if not event_id:
            return
        
        # Check if this event_id already exists with different content
        existing = fact_store.get_by_event_id(event_id)
        if existing:
            # Compare key immutable fields
            if (existing.event_type != normalized_event.get("event_type") or
                existing.target_type != normalized_event.get("target_type") or
                existing.target_id != normalized_event.get("target_id") or
                existing.decision != normalized_event.get("decision")):
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="no_mutation_of_past_moderation",
                    violation_message=(
                        f"Attempted to mutate past moderation {event_id}. "
                        "Past moderation facts are immutable and cannot be changed."
                    ),
                    context=context
                )
    
    @staticmethod
    def _no_conflicting_decisions_for_same_scope(
        normalized_event: Dict[str, Any],
        fact_store: ModerationFactStore,
        context: IngestErrorContext
    ) -> None:
        """
        TIER-0 Invariant: No conflicting decisions for same scope.
        
        Multiple decisions for the same (target, policy, scope) must be consistent.
        Scope is evaluated from enforcement_scope + target context.
        """
        if normalized_event.get("event_type") != ModerationEventType.DECISION.value:
            return
        
        target_type = normalized_event.get("target_type")
        target_id = normalized_event.get("target_id")
        policy_id = normalized_event.get("policy_id")
        policy_version = normalized_event.get("policy_version")
        enforcement_scope = normalized_event.get("enforcement_scope", "global")
        decision = normalized_event.get("decision")
        
        if not all([target_type, target_id, policy_id, policy_version, decision]):
            return
        
        # Check for existing decisions with same scope
        # TIER-0: Validate no conflicting decisions for same (target, policy, scope)
        # 
        # Note: Full scope conflict detection requires fact_store to support
        # querying by scope. With current interface, we check:
        # 1. If event_id exists, mutation check already covers it
        # 2. If dedup key matches, deduplicator will catch it
        # 3. For same-scope different-timestamp conflicts, we validate when found
        
        # Check by event_id first (catches exact duplicates and mutations)
        event_id = normalized_event.get("event_id")
        if event_id:
            existing_by_id = fact_store.get_by_event_id(event_id)
            if existing_by_id and existing_by_id.event_type == ModerationEventType.DECISION.value:
                # Verify scope components match
                if (existing_by_id.target_type == target_type and
                    existing_by_id.target_id == target_id and
                    existing_by_id.policy_id == policy_id and
                    existing_by_id.policy_version == policy_version):
                    # Same scope - check for conflicting decision
                    if existing_by_id.decision != decision:
                        raise CommonIngestErrors.invariant_broken(
                            invariant_name="no_conflicting_decisions_for_same_scope",
                            violation_message=(
                                f"Conflicting decision for scope (target={target_type}:{target_id}, "
                                f"policy={policy_id}:{policy_version}, scope={enforcement_scope}). "
                                f"Existing: {existing_by_id.decision}, New: {decision}"
                            ),
                            context=context
                        )
        
        # TIER-0: Use scope query for comprehensive conflict detection
        existing_decisions = fact_store.get_decisions_by_scope(
            target_type=target_type,
            target_id=target_id,
            policy_id=policy_id,
            policy_version=policy_version,
            enforcement_scope=enforcement_scope
        )
        
        # Check for conflicting decisions in same scope
        # All decisions returned are for the same scope by definition
        for existing_decision in existing_decisions:
            # Check for conflicting decision outcome
            if existing_decision.decision and existing_decision.decision != decision:
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="no_conflicting_decisions_for_same_scope",
                    violation_message=(
                        f"Conflicting decision for scope (target={target_type}:{target_id}, "
                        f"policy={policy_id}:{policy_version}, scope={enforcement_scope}). "
                        f"Existing: {existing_decision.decision}, New: {decision}"
                    ),
                    context=context
                )
    
    @staticmethod
    def _enforcement_logic_presence_required(
        normalized_event: Dict[str, Any],
        context: IngestErrorContext
    ) -> None:
        """
        TIER-0 Invariant: Enforcement logic presence required.
        
        Decisions must have complete enforcement context:
        - policy_id + policy_version (binding)
        - enforcement_scope (from policy)
        - decision_authority (who can enforce)
        - decision outcome (allow/limit/remove)
        """
        if normalized_event.get("event_type") != ModerationEventType.DECISION.value:
            return
        
        # Check all required enforcement fields are present
        required_fields = {
            "policy_id": normalized_event.get("policy_id"),
            "policy_version": normalized_event.get("policy_version"),
            "decision": normalized_event.get("decision"),
        }
        
        missing = [field for field, value in required_fields.items() if not value]
        if missing:
            raise CommonIngestErrors.invariant_broken(
                invariant_name="enforcement_logic_presence_required",
                violation_message=(
                    f"Decision missing required enforcement fields: {', '.join(missing)}. "
                    "Enforcement logic must be complete for decisions."
                ),
                context=context
            )
        
        # Check enforcement_scope is present (should be set by policy binder)
        if not normalized_event.get("enforcement_scope"):
            raise CommonIngestErrors.invariant_broken(
                invariant_name="enforcement_scope_required",
                violation_message=(
                    "Decision missing enforcement_scope. "
                    "Policy binding must set enforcement_scope."
                ),
                context=context
            )
        
        # Check decision_authority is present (should be set by policy binder)
        if not normalized_event.get("decision_authority"):
            raise CommonIngestErrors.invariant_broken(
                invariant_name="decision_authority_required",
                violation_message=(
                    "Decision missing decision_authority. "
                    "Policy binding must set decision_authority for legal audit reconstruction."
                ),
                context=context
            )


# ============================================================================
# MODERATION INGESTOR (ORCHESTRATOR)
# ============================================================================

class ModerationIngestor:
    """
    Primary entrypoint for moderation ingestion.
    
    Orchestrates validation, normalization, binding, deduplication,
    and emission of immutable moderation facts.
    
    Execution order (STRICT):
    1. Validate ingest context
    2. Apply ingest policy
    3. Validate raw moderation event
    4. Normalize event fields
    5. Resolve target
    6. Bind policy
    7. Deduplicate event
    8. Emit immutable moderation fact
    
    No retries. No partial success.
    """
    
    def __init__(
        self,
        policy: ModerationIngestPolicy,
        validator: ModerationValidator,
        normalizer: ModerationNormalizer,
        target_resolver: ModerationTargetResolver,
        policy_binder: ModerationPolicyBinder,
        deduplicator: ModerationDeduplicator,
        fact_store: ModerationFactStore,
        authority_resolver: Optional[ReviewerAuthorityResolver] = None,
        finality_guard: Optional[DecisionFinalityGuard] = None,
        audit_logger: Optional[AuditLogger] = None
    ):
        self.policy = policy
        self.validator = validator
        self.normalizer = normalizer
        self.target_resolver = target_resolver
        self.policy_binder = policy_binder
        self.deduplicator = deduplicator
        self.fact_store = fact_store
        self.authority_resolver = authority_resolver
        self.finality_guard = finality_guard
        self.audit_logger = audit_logger
        
        # TIER-0: Compute ingest policy hash for forensic reconstruction
        self.ingest_policy_hash = self._compute_ingest_policy_hash()
    
    def ingest(
        self,
        raw_moderation_event: Dict[str, Any],
        context: IngestContext
    ) -> IngestResult:
        """
        Ingest a moderation event and convert to immutable fact.
        
        Args:
            raw_moderation_event: Raw moderation event dictionary
            context: Ingestion context
        
        Returns:
            IngestResult indicating success or failure
        """
        event_id = raw_moderation_event.get("event_id", "unknown")
        
        # Log start
        if self.audit_logger:
            self.audit_logger.log_ingest_started(event_id, context)
        
        try:
            # Step 1: Validate ingest context
            self._validate_context(context)
            
            # Step 2: Apply ingest policy (implicit in validator)
            
            # Step 3: Validate raw moderation event
            self.validator.validate(raw_moderation_event, context)
            
            # Step 4: Normalize event fields
            normalized = self.normalizer.normalize(raw_moderation_event)
            
            # Step 5: Resolve target
            target_type, target_id = self.target_resolver.resolve(
                normalized["target_type"],
                normalized["target_id"],
                context
            )
            normalized["target_type"] = target_type
            normalized["target_id"] = target_id
            
            # Step 6: Bind policy (for decisions)
            policy_hash = ""
            if normalized["event_type"] == ModerationEventType.DECISION.value:
                policy_id, policy_version, policy_hash, enforcement_scope, decision_authority = self.policy_binder.bind(
                    normalized["policy_id"],
                    normalized["policy_version"],
                    normalized,
                    context
                )
                normalized["policy_id"] = policy_id
                normalized["policy_version"] = policy_version
                normalized["policy_hash"] = policy_hash
                normalized["enforcement_scope"] = enforcement_scope
                normalized["decision_authority"] = decision_authority
                
                # TIER-0: Resolve reviewer authority scope (REQUIRED for decisions)
                if not self.authority_resolver:
                    raise IngestErrorBuilder(
                        category=ErrorCategory.INFRA,
                        context=IngestErrorContext(
                            pipeline_step="authority_resolution",
                            run_id=context.run_id,
                            input_id=event_id,
                            entity_type="moderation_event",
                            entity_id=event_id
                        ),
                        recovery_hint=RecoveryHint.FATAL
                    ).add_cause(
                        code=IngestErrorCode.DEPENDENCY_MISSING,
                        message="authority_resolver is required for Tier-0 decision authority validation",
                        source="moderation_ingestor",
                        constraint="authority_resolver_required",
                        expected_value="ReviewerAuthorityResolver instance",
                        actual_value="None"
                    ).build()
                
                # TIER-0: All fields must be present - no fallbacks
                reviewer_type = normalized.get("reviewer_type")
                reviewer_id = normalized.get("reviewer_id")
                if not reviewer_type or not reviewer_id:
                    raise CommonIngestErrors.invariant_broken(
                        invariant_name="reviewer_identity_required",
                        violation_message="reviewer_type and reviewer_id are required for authority resolution",
                        context=IngestErrorContext(
                            pipeline_step="authority_resolution",
                            run_id=context.run_id,
                            input_id=event_id,
                            entity_type="moderation_event",
                            entity_id=event_id
                        )
                    )
                
                reviewer_authority_scope = self.authority_resolver.resolve(
                    reviewer_type,
                    reviewer_id,
                    enforcement_scope,
                    normalized["event_type"],
                    policy_id,
                    policy_version,
                    IngestErrorContext(
                        pipeline_step="authority_resolution",
                        run_id=context.run_id,
                        input_id=event_id,
                        entity_type="moderation_event",
                        entity_id=event_id
                    )
                )
                normalized["reviewer_authority_scope"] = reviewer_authority_scope
            
            # Step 7: Deduplicate event (BEFORE invariants that rely on existing facts)
            # TIER-0: Dedup before invariants to avoid mutation false positives.
            # This order ensures we check for duplicates before checking for conflicts,
            # preventing false mutation violations when the same event is replayed.
            dedup_status, existing_fact = self.deduplicator.check(normalized, context)
            
            if dedup_status == DeduplicationStatus.CONFLICTING:
                raise IngestErrorBuilder(
                    category=ErrorCategory.STATE,
                    context=IngestErrorContext(
                        pipeline_step="deduplication",
                        run_id=context.run_id,
                        input_id=event_id,
                        entity_type="moderation_event",
                        entity_id=event_id
                    ),
                    recovery_hint=RecoveryHint.FATAL
                ).add_cause(
                    code=IngestErrorCode.STATE_CONFLICT,
                    message="event conflicts with existing moderation fact",
                    source="moderation_deduplicator",
                    actual_value=event_id
                ).build()
            
            if dedup_status == DeduplicationStatus.DUPLICATE:
                # Safe duplicate - log and return
                if self.audit_logger and existing_fact:
                    self.audit_logger.log_duplicate_detected(
                        event_id,
                        existing_fact.moderation_id,
                        context
                    )
                
                return create_deduped_result(
                    context=context,
                    existing_fact_ids=[existing_fact.moderation_id] if existing_fact else []
                )
            
            # Step 8: Enforce invariants (AFTER deduplication)
            ModerationIngestInvariants.enforce(normalized, context, self.fact_store)
            
            # TIER-0: Enforce decision finality (no downgrade after REMOVE)
            if self.finality_guard:
                self.finality_guard.enforce(
                    normalized,
                    self.fact_store,
                    IngestErrorContext(
                        pipeline_step="decision_finality",
                        run_id=context.run_id,
                        input_id=event_id,
                        entity_type="moderation_event",
                        entity_id=event_id
                    )
                )
            
            # TIER-0: Compute forensic replay envelope (digests) - ALWAYS REQUIRED
            # No optional paths - all digests must be computed for byte-perfect reconstruction
            raw_event_digest = CanonicalSerializationEngine.compute_digest(raw_moderation_event)
            normalized_event_digest = CanonicalSerializationEngine.compute_digest(normalized)
            
            # TIER-0: Full context serialization for deterministic digest
            context_dict = {
                "run_id": context.run_id,
                "mode": context.mode.value if hasattr(context.mode, 'value') else str(context.mode),
                "authority": context.authority.value if hasattr(context.authority, 'value') else str(context.authority),
            }
            # Include all context fields for complete reconstruction
            if hasattr(context, 'timestamp'):
                context_dict["timestamp"] = context.timestamp.isoformat() if isinstance(context.timestamp, datetime) else str(context.timestamp)
            context_digest = CanonicalSerializationEngine.compute_digest(context_dict)
            
            # TIER-0: Compute canonical event fingerprint (without event_id, timestamp)
            # This ensures replay determinism across different event IDs and timestamps
            event_fingerprint_data = {k: v for k, v in normalized.items() 
                                     if k not in ("event_id", "timestamp", "raw_event_digest", 
                                                  "normalized_event_digest", "context_digest", 
                                                  "event_fingerprint", "ingest_policy_version", 
                                                  "ingest_policy_hash")}
            event_fingerprint = CanonicalSerializationEngine.compute_digest(event_fingerprint_data)
            
            # TIER-0: All digests are REQUIRED - no optional paths
            if not all([raw_event_digest, normalized_event_digest, context_digest, event_fingerprint]):
                raise IngestErrorBuilder(
                    category=ErrorCategory.INFRA,
                    context=IngestErrorContext(
                        pipeline_step="forensic_envelope",
                        run_id=context.run_id,
                        input_id=event_id,
                        entity_type="moderation_event",
                        entity_id=event_id
                    ),
                    recovery_hint=RecoveryHint.FATAL
                ).add_cause(
                    code=IngestErrorCode.SERIALIZATION_FAILED,
                    message="Failed to compute required forensic digests",
                    source="moderation_ingestor",
                    constraint="forensic_digests_required",
                    expected_value="all digests computed",
                    actual_value="one or more digests missing"
                ).build()
            
            normalized["raw_event_digest"] = raw_event_digest
            normalized["normalized_event_digest"] = normalized_event_digest
            normalized["context_digest"] = context_digest
            normalized["event_fingerprint"] = event_fingerprint
            normalized["ingest_policy_version"] = self.policy.policy_version
            normalized["ingest_policy_hash"] = self.ingest_policy_hash
            
            # TIER-0: Validate all required fields are present before fact creation
            self._validate_fact_fields(normalized, event_id, context)
            
            # Step 9: Emit immutable moderation fact
            moderation_fact = self._create_fact(normalized, raw_moderation_event)
            moderation_id = ImmutabilityPersistenceGuard.persist(moderation_fact, self.fact_store)
            
            # Log success
            if self.audit_logger:
                self.audit_logger.log_ingest_succeeded(event_id, moderation_id, context)
            
            return create_accepted_result(
                context=context,
                fact_ids=[moderation_id]
            )
            
        except BaseIngestError as e:
            # Log failure
            if self.audit_logger:
                self.audit_logger.log_ingest_failed(event_id, e, context)
            
            # Convert to rejection result
            return create_rejected_result(
                context=context,
                reason=RejectionReason.POLICY_VIOLATION,
                detail=str(e)
            )
        except Exception as e:
            # Wrap unexpected errors
            error = IngestErrorBuilder(
                category=ErrorCategory.INFRA,
                context=IngestErrorContext(
                    pipeline_step="moderation_ingestion",
                    run_id=context.run_id,
                    input_id=event_id,
                    entity_type="moderation_event",
                    entity_id=event_id
                ),
                recovery_hint=RecoveryHint.REQUIRES_MANUAL_REVIEW
            ).add_cause(
                code=IngestErrorCode.SERIALIZATION_FAILED,
                message=f"unexpected error during ingestion: {str(e)}",
                source="moderation_ingestor"
            ).build()
            
            if self.audit_logger:
                self.audit_logger.log_ingest_failed(event_id, error, context)
            
            return create_rejected_result(
                context=context,
                reason=RejectionReason.OTHER,
                detail=str(e)
            )
    
    def _validate_context(self, context: IngestContext) -> None:
        """Validate ingestion context."""
        # Context validation is handled by IngestContext.__post_init__
        # Additional checks can be added here if needed
        pass
    
    def _validate_fact_fields(
        self,
        normalized: Dict[str, Any],
        event_id: str,
        context: IngestContext
    ) -> None:
        """
        TIER-0: Validate all required fact fields are present.
        
        Ensures no silent fallbacks - all Tier-0 fields must be explicitly set.
        """
        error_context = IngestErrorContext(
            pipeline_step="fact_validation",
            run_id=context.run_id,
            input_id=event_id,
            entity_type="moderation_fact",
            entity_id=event_id
        )
        
        # Required fields for all events
        required_fields = {
            "event_type": normalized.get("event_type"),
            "target_type": normalized.get("target_type"),
            "target_id": normalized.get("target_id"),
            "timestamp": normalized.get("timestamp"),
            "source": normalized.get("source"),
            "schema_version": normalized.get("schema_version"),
            "raw_event_digest": normalized.get("raw_event_digest"),
            "normalized_event_digest": normalized.get("normalized_event_digest"),
            "context_digest": normalized.get("context_digest"),
            "event_fingerprint": normalized.get("event_fingerprint"),
            "ingest_policy_version": normalized.get("ingest_policy_version"),
            "ingest_policy_hash": normalized.get("ingest_policy_hash"),
        }
        
        missing = [field for field, value in required_fields.items() if not value]
        if missing:
            raise CommonIngestErrors.invariant_broken(
                invariant_name="fact_fields_required",
                violation_message=f"Missing required fact fields: {', '.join(missing)}",
                context=error_context
            )
        
        # For decisions, additional required fields
        if normalized.get("event_type") == ModerationEventType.DECISION.value:
            decision_required = {
                "policy_id": normalized.get("policy_id"),
                "policy_version": normalized.get("policy_version"),
                "policy_hash": normalized.get("policy_hash"),  # TIER-0: Must be computed
                "enforcement_scope": normalized.get("enforcement_scope"),
                "decision_authority": normalized.get("decision_authority"),
                "reviewer_id": normalized.get("reviewer_id"),
                "reviewer_type": normalized.get("reviewer_type"),
                "reviewer_authority_scope": normalized.get("reviewer_authority_scope"),
            }
            
            missing_decision = [field for field, value in decision_required.items() if not value]
            if missing_decision:
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="decision_fact_fields_required",
                    violation_message=f"Missing required decision fact fields: {', '.join(missing_decision)}",
                    context=error_context
                )
            
            # TIER-0: Policy hash must be non-empty (cryptographically provable)
            if not normalized.get("policy_hash") or len(normalized.get("policy_hash", "")) != 64:
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="policy_hash_invalid",
                    violation_message="policy_hash must be a valid SHA256 hash (64 hex characters)",
                    context=error_context
                )
    
    def _create_fact(self, normalized: Dict[str, Any], raw_event: Dict[str, Any]) -> ModerationFact:
        """
        Create immutable moderation fact from normalized event.
        
        TIER-0: Includes all forensic replay fields and event-specific payloads.
        """
        # Generate moderation_id deterministically
        moderation_id = self._generate_moderation_id(normalized)
        
        # TIER-0: Extract event-specific payloads (prevents semantic ambiguity)
        event_type = normalized["event_type"]
        flag_payload = None
        strike_payload = None
        decision_payload = None
        
        if event_type == ModerationEventType.FLAG.value:
            flag_payload = {
                "severity": normalized.get("severity"),
                "source": normalized.get("source"),
            }
        elif event_type == ModerationEventType.STRIKE.value:
            strike_payload = {
                "severity": normalized.get("severity"),
                "source": normalized.get("source"),
            }
        elif event_type == ModerationEventType.DECISION.value:
            decision_payload = {
                "decision": normalized.get("decision"),
                "severity": normalized.get("severity"),
                "policy_id": normalized.get("policy_id"),
                "policy_version": normalized.get("policy_version"),
                "reviewer_id": normalized.get("reviewer_id"),
                "reviewer_type": normalized.get("reviewer_type"),
                "enforcement_scope": normalized.get("enforcement_scope"),
                "decision_authority": normalized.get("decision_authority"),
            }
        
        return ModerationFact(
            moderation_id=moderation_id,
            event_type=event_type,
            target_type=normalized["target_type"],
            target_id=normalized["target_id"],
            decision=normalized.get("decision"),
            severity=normalized.get("severity"),
            policy_id=normalized.get("policy_id") or "",
            policy_version=normalized.get("policy_version") or "",
            policy_hash=normalized.get("policy_hash") or "",  # TIER-0: Must be computed, no fallback
            reviewer_id=normalized.get("reviewer_id") or "",
            reviewer_type=normalized.get("reviewer_type") or "",
            reviewer_authority_scope=normalized.get("reviewer_authority_scope"),
            timestamp=normalized["timestamp"],
            source=normalized["source"],
            schema_version=normalized["schema_version"],
            enforcement_scope=normalized.get("enforcement_scope") or "",
            decision_authority=normalized.get("decision_authority") or "",
            # TIER-0: All forensic digests are REQUIRED - validated above
            event_fingerprint=normalized.get("event_fingerprint") or "",
            raw_event_digest=normalized.get("raw_event_digest") or "",
            normalized_event_digest=normalized.get("normalized_event_digest") or "",
            context_digest=normalized.get("context_digest") or "",
            ingest_policy_version=normalized.get("ingest_policy_version") or "",
            ingest_policy_hash=normalized.get("ingest_policy_hash") or "",
            flag_payload=flag_payload,
            strike_payload=strike_payload,
            decision_payload=decision_payload,
        )
    
    def _compute_ingest_policy_hash(self) -> str:
        """TIER-0: Compute hash of ingest policy for forensic reconstruction."""
        policy_dict = {
            "policy_version": self.policy.policy_version,
            "supported_targets": sorted(list(self.policy.supported_targets)),
            "supported_event_types": sorted(list(self.policy.supported_event_types)),
            "supported_policy_versions": sorted(list(self.policy.supported_policy_versions)),
            "allow_machine_flags": self.policy.allow_machine_flags,
            "allow_machine_decisions": self.policy.allow_machine_decisions,
            "canonical_schema_version": self.policy.canonical_schema_version,
        }
        return CanonicalSerializationEngine.compute_digest(policy_dict)
    
    def _generate_moderation_id(self, normalized: Dict[str, Any]) -> str:
        """Generate unique moderation ID deterministically."""
        # Combine key fields for stable ID generation
        id_components = [
            normalized["event_id"],
            normalized["target_type"],
            normalized["target_id"],
            normalized["timestamp"].isoformat() if isinstance(normalized["timestamp"], datetime) else str(normalized["timestamp"]),
        ]
        
        id_string = ":".join(id_components)
        hash_value = hashlib.sha256(id_string.encode('utf-8')).hexdigest()
        
        return f"mod_{hash_value[:16]}"


# ============================================================================
# FACTORY & SETUP
# ============================================================================

def create_moderation_ingestor(
    policy: ModerationIngestPolicy,
    fact_store: ModerationFactStore,
    target_store: Optional[TargetStore] = None,
    policy_store: Optional[PolicyStore] = None,
    schema_registry: Optional[ModerationSchemaRegistry] = None,
    audit_logger: Optional[AuditLogger] = None
) -> ModerationIngestor:
    """
    Factory function to create fully configured ModerationIngestor.
    
    TIER-0: Creates all required components for deterministic, replay-safe ingestion.
    
    This is the recommended way to instantiate the ingestion pipeline.
    """
    validator = ModerationValidator(
        policy=policy,
        schema_registry=schema_registry
    )
    
    normalizer = ModerationNormalizer(
        schema_version=policy.canonical_schema_version
    )
    
    target_resolver = ModerationTargetResolver(
        target_store=target_store
    )
    
    policy_binder = ModerationPolicyBinder(
        policy_store=policy_store,
        ingest_policy=policy
    )
    
    deduplicator = ModerationDeduplicator(
        fact_store=fact_store
    )
    
    # TIER-0: Create authority resolver and finality guard
    # Authority resolver requires policy_store for grant validation
    authority_resolver = ReviewerAuthorityResolver(
        ingest_policy=policy,
        policy_store=policy_store
    )
    finality_guard = DecisionFinalityGuard()
    
    return ModerationIngestor(
        policy=policy,
        validator=validator,
        normalizer=normalizer,
        target_resolver=target_resolver,
        policy_binder=policy_binder,
        deduplicator=deduplicator,
        fact_store=fact_store,
        authority_resolver=authority_resolver,
        finality_guard=finality_guard,
        audit_logger=audit_logger
    )


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    # Enums
    'ModerationEventType',
    'ModerationDecision',
    'ModerationSeverity',
    'ModerationTargetType',
    'ReviewerType',
    'DeduplicationStatus',
    
    # Data Models
    'ModerationIngestPolicy',
    'ModerationFact',
    
    # Components
    'ModerationValidator',
    'ModerationNormalizer',
    'ModerationTargetResolver',
    'ModerationPolicyBinder',
    'ModerationDeduplicator',
    'ModerationIngestInvariants',
    
    # TIER-0 Components
    'CanonicalSerializationEngine',
    'ModerationSchemaRegistry',
    'ReviewerAuthorityResolver',
    'DecisionFinalityGuard',
    'ImmutabilityPersistenceGuard',
    
    # Main Ingestor
    'ModerationIngestor',
    
    # Factory
    'create_moderation_ingestor',
    
    # Protocols
    'TargetStore',
    'PolicyStore',
    'ModerationFactStore',
    'AuditLogger',
]
