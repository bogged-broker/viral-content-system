"""
/data/pipelines/ingestion/content_ingest.py

Content Metadata → Canonical Fact Authority

This is the SOLE authority that converts raw platform-level content metadata into canonical, immutable content facts.

It answers:

> "Is this piece of content real, valid, owned, version-safe, and admissible into the system's truth layer?"

Nothing becomes "content" until this file says yes.

Design Principle: Content facts must be true even if the platform lies, retries, or reorders.

A single piece of content may be ingested many times —
it must become one fact, or be rejected deterministically.
"""

from __future__ import annotations

import copy
import hashlib
import json
import unicodedata
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

class ContentType(Enum):
    """Canonical content types."""
    VIDEO = "video"
    IMAGE = "image"
    TEXT = "text"
    AUDIO = "audio"
    LINK = "link"


class DeduplicationStatus(Enum):
    """Result of deduplication check."""
    NEW = "NEW"
    KNOWN = "KNOWN"
    CONFLICTING = "CONFLICTING"


# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass(frozen=True)
class ContentIngestPolicy:
    """
    Immutable, versioned policy defining what content is allowed.
    
    Policy is immutable. Changes require migration. No dynamic overrides.
    """
    policy_version: str
    supported_platforms: FrozenSet[str]
    supported_versions: FrozenSet[str]
    required_fields: FrozenSet[str]
    max_metadata_size_bytes: int
    canonical_schema_version: str
    max_future_drift_seconds: int = 300  # 5 minutes
    
    def __post_init__(self):
        """Validate policy immutability constraints."""
        if not self.policy_version:
            raise ValueError("policy_version must be set")
        if not self.supported_platforms:
            raise ValueError("supported_platforms cannot be empty")
        if not self.supported_versions:
            raise ValueError("supported_versions cannot be empty")
        if not self.required_fields:
            raise ValueError("required_fields cannot be empty")
        if self.max_metadata_size_bytes <= 0:
            raise ValueError("max_metadata_size_bytes must be positive")
        if not self.canonical_schema_version:
            raise ValueError("canonical_schema_version must be set")


@dataclass(frozen=True)
class ContentFact:
    """
    Immutable content record emitted after successful ingestion.
    
    Once written, treated as historical truth.
    """
    content_id: str
    platform: str
    platform_content_id: str
    content_type: str
    account_id: str
    normalized_metadata: Dict[str, Any]
    canonical_hash: str
    schema_version: str
    created_at_epoch_ms: int
    updated_at_epoch_ms: Optional[int]
    ingested_at_ms: int
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for persistence."""
        return {
            'content_id': self.content_id,
            'platform': self.platform,
            'platform_content_id': self.platform_content_id,
            'content_type': self.content_type,
            'account_id': self.account_id,
            'normalized_metadata': self.normalized_metadata,
            'canonical_hash': self.canonical_hash,
            'schema_version': self.schema_version,
            'created_at_epoch_ms': self.created_at_epoch_ms,
            'updated_at_epoch_ms': self.updated_at_epoch_ms,
            'ingested_at_ms': self.ingested_at_ms,
        }


# ============================================================================
# EXTERNAL DEPENDENCIES (INTERFACES)
# ============================================================================

class ContentStore(Protocol):
    """
    Interface to content fact persistence.
    
    Tier-0 requirement: Atomic deduplication via compare-and-set ONLY.
    At 5M+ traffic, non-atomic dedup check → assign ID → persist causes race conditions.
    
    CRITICAL: persist_atomic() is the ONLY allowed persistence method for Tier-0 compliance.
    No fallback to non-atomic persist() is permitted.
    """
    
    def get_by_content_id(self, content_id: str) -> Optional[ContentFact]:
        """Retrieve content by canonical content_id."""
        ...
    
    def get_by_platform_id(
        self,
        platform: str,
        platform_content_id: str,
        account_id: str
    ) -> Optional[ContentFact]:
        """Retrieve content by platform identifiers."""
        ...
    
    def get_by_canonical_hash(self, canonical_hash: str) -> Optional[ContentFact]:
        """Retrieve content by canonical hash."""
        ...
    
    def persist_atomic(
        self,
        fact: ContentFact,
        expected_content_id: Optional[str] = None
    ) -> Tuple[bool, Optional[ContentFact]]:
        """
        Atomically persist content fact with compare-and-set semantics.
        
        This is the ONLY persistence method allowed for Tier-0 compliance.
        If the store cannot guarantee atomicity → ingestion must reject, not downgrade.
        
        Args:
            fact: Content fact to persist
            expected_content_id: If provided, only persist if content_id doesn't exist
        
        Returns:
            Tuple of (success: bool, existing_fact_if_conflict: Optional[ContentFact])
            - success=True: Fact was persisted successfully
            - success=False, existing=None: Atomic operation failed (store error)
            - success=False, existing=ContentFact: Conflict detected (same ID, different content)
        
        Tier-0 requirement: This method provides atomic deduplication.
        Implementation MUST use database-level transactions or compare-and-set.
        MUST guarantee exactly-once semantics under concurrency.
        
        Raises:
            BaseIngestError: If store cannot guarantee atomicity (must reject, not fallback)
        """
        ...


class AuditLogger(Protocol):
    """Interface to append-only audit trail."""
    
    def log_ingest_started(
        self,
        platform: str,
        platform_content_id: str,
        context: IngestContext
    ) -> None:
        """Log ingestion start."""
        ...
    
    def log_ingest_succeeded(
        self,
        content_id: str,
        context: IngestContext
    ) -> None:
        """Log successful ingestion."""
        ...
    
    def log_ingest_failed(
        self,
        platform: str,
        platform_content_id: str,
        error: BaseIngestError,
        context: IngestContext
    ) -> None:
        """Log failed ingestion."""
        ...
    
    def log_duplicate_detected(
        self,
        content_id: str,
        context: IngestContext
    ) -> None:
        """Log duplicate detection."""
        ...


# ============================================================================
# CONTENT VALIDATOR (GATEKEEPER)
# ============================================================================

class ContentValidator:
    """
    Responsible for hard validation only.
    
    Tier-0 requirement: All policy enforcement occurs in ONE deterministic evaluation pass
    before normalization begins. No late policy branching allowed.
    
    Must validate:
    - schema conformance
    - required field presence (across full canonical schema projection)
    - field type correctness
    - allowed enum values
    - metadata size limits
    - timestamp sanity (no future drift)
    
    Must NOT:
    - auto-fill missing fields
    - guess defaults
    - coerce invalid types
    - delegate schema authority to input structure
    
    Failures → structured IngestError.
    """
    
    def __init__(self, policy: ContentIngestPolicy):
        self.policy = policy
    
    def validate(
        self,
        raw_content: Dict[str, Any],
        context: IngestContext
    ) -> None:
        """
        Validate raw content data through single deterministic policy evaluation pass.
        
        Tier-0 requirement: All policy enforcement in ONE pass before normalization.
        Schema authority is external (policy.required_fields), not delegated to input structure.
        
        Raises BaseIngestError on any validation failure.
        """
        error_context = IngestErrorContext(
            pipeline_step="content_validation",
            run_id=context.run_id,
            input_id=raw_content.get("platform_content_id"),
            entity_type="content",
            entity_id=raw_content.get("platform_content_id")
        )
        
        platform_content_id = raw_content.get("platform_content_id", "unknown")
        
        # Step 1: Project raw content to canonical schema view
        # This flattens nested structure so required fields can be checked at top-level
        canonical_view = self._project_to_canonical_schema(raw_content, error_context)
        
        # Step 2: Validate platform (policy enforcement)
        self._validate_platform(canonical_view, error_context, platform_content_id)
        
        # Step 3: Validate schema version (policy enforcement)
        self._validate_schema_version(canonical_view, error_context, platform_content_id)
        
        # Step 4: Validate required fields across canonical schema (policy enforcement)
        self._validate_required_fields(canonical_view, error_context, platform_content_id)
        
        # Step 5: Validate field types
        self._validate_field_types(canonical_view, error_context, platform_content_id)
        
        # Step 6: Validate metadata size (policy enforcement)
        self._validate_metadata_size(canonical_view, error_context, platform_content_id)
        
        # Step 7: Validate timestamp sanity (policy enforcement)
        self._validate_timestamp_sanity(canonical_view, error_context, platform_content_id)
        
        # Step 8: Validate content type (policy enforcement)
        self._validate_content_type(canonical_view, error_context, platform_content_id)
        
        # Step 9: Validate account_id (policy enforcement)
        self._validate_account_id(canonical_view, error_context, platform_content_id)
    
    def _project_to_canonical_schema(
        self,
        raw_content: Dict[str, Any],
        error_context: IngestErrorContext
    ) -> Dict[str, Any]:
        """
        Project raw content to canonical schema view.
        
        Tier-0 requirement: Schema authority is external. Required fields must be checked
        at top-level of canonical projection, not nested in metadata.
        
        This function flattens the structure so that fields can be checked uniformly
        regardless of whether they appear at top-level or nested in metadata.
        
        CRITICAL: Metadata shadowing (same key at both top-level and metadata) is REJECTED.
        At scale, ambiguous flattening precedence must be explicitly declared as invariant.
        """
        canonical = {}
        metadata = raw_content.get("metadata", {})
        
        if not isinstance(metadata, dict):
            # Will be caught by field type validation, but be explicit here
            return canonical
        
        # Copy top-level fields
        top_level_keys = ['platform', 'platform_content_id', 'content_type', 'account_id', 'schema_version']
        for key in top_level_keys:
            if key in raw_content:
                canonical[key] = raw_content[key]
        
        # Flatten metadata fields to top-level for uniform validation
        # CRITICAL INVARIANT: Reject metadata shadowing (same key at both levels)
        shadowed_keys = []
        for key, value in metadata.items():
            if key in canonical:
                # Same key exists at both top-level and metadata → REJECT
                # This is an ambiguous flattening precedence violation
                shadowed_keys.append(key)
            else:
                canonical[key] = value
        
        # Reject metadata shadowing explicitly
        if shadowed_keys:
            raise CommonIngestErrors.invariant_broken(
                invariant_name="no_metadata_shadowing",
                violation_message=f"Metadata shadowing detected: keys {shadowed_keys} exist at both top-level and metadata. Ambiguous flattening precedence is forbidden.",
                context=error_context
            )
        
        # Preserve metadata as nested structure for normalization
        canonical['_metadata'] = metadata
        
        return canonical
    
    def _validate_platform(
        self,
        canonical_view: Dict[str, Any],
        context: IngestErrorContext,
        platform_content_id: str
    ) -> None:
        """Ensure platform is supported."""
        platform = canonical_view.get("platform", "")
        
        if not platform:
            raise CommonIngestErrors.missing_required_field(
                field_name="platform",
                context=context
            )
        
        platform_lower = platform.strip().lower()
        
        if platform_lower not in self.policy.supported_platforms:
            raise IngestErrorBuilder(
                category=ErrorCategory.AUTHORITY,
                context=context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.POLICY_VIOLATION,
                message=f"platform {platform} not supported by policy",
                source="content_validator",
                constraint="supported_platforms",
                expected_value=str(list(self.policy.supported_platforms)),
                actual_value=platform
            ).build()
    
    def _validate_schema_version(
        self,
        canonical_view: Dict[str, Any],
        context: IngestErrorContext,
        platform_content_id: str
    ) -> None:
        """Verify schema version compatibility."""
        schema_version = canonical_view.get("schema_version", "")
        
        if not schema_version:
            raise CommonIngestErrors.unsupported_version(
                field_name="schema_version",
                expected=str(self.policy.canonical_schema_version),
                actual="",
                context=context
            )
        
        if schema_version not in self.policy.supported_versions:
            raise CommonIngestErrors.unsupported_version(
                field_name="schema_version",
                expected=str(list(self.policy.supported_versions)),
                actual=schema_version,
                context=context
            )
    
    def _validate_required_fields(
        self,
        canonical_view: Dict[str, Any],
        context: IngestErrorContext,
        platform_content_id: str
    ) -> None:
        """
        Ensure all required fields are present in canonical schema projection.
        
        Tier-0 requirement: Required fields checked at top-level of canonical projection.
        Schema authority is external (policy.required_fields), not delegated to input structure.
        """
        missing_fields = []
        
        for field in self.policy.required_fields:
            # Check canonical view (already flattened from raw_content)
            if field not in canonical_view or canonical_view[field] is None:
                missing_fields.append(field)
        
        if missing_fields:
            raise CommonIngestErrors.missing_required_field(
                field_name=", ".join(missing_fields),
                context=context
            )
    
    def _validate_field_types(
        self,
        canonical_view: Dict[str, Any],
        context: IngestErrorContext,
        platform_content_id: str
    ) -> None:
        """Validate field type correctness."""
        platform_content_id_value = canonical_view.get("platform_content_id", "")
        
        if not isinstance(platform_content_id_value, str) or not platform_content_id_value.strip():
            raise CommonIngestErrors.invariant_broken(
                invariant_name="platform_content_id_type",
                violation_message="platform_content_id must be non-empty string",
                context=context
            )
        
        metadata = canonical_view.get("_metadata", {})
        if not isinstance(metadata, dict):
            raise CommonIngestErrors.schema_violation(
                field_name="metadata",
                expected="dict",
                actual=str(type(metadata).__name__),
                context=context
            )
    
    def _validate_metadata_size(
        self,
        canonical_view: Dict[str, Any],
        context: IngestErrorContext,
        platform_content_id: str
    ) -> None:
        """Ensure metadata doesn't exceed size limits."""
        metadata = canonical_view.get("_metadata", {})
        metadata_json = json.dumps(metadata, sort_keys=True, separators=(',', ':'))
        metadata_size = len(metadata_json.encode('utf-8'))
        
        if metadata_size > self.policy.max_metadata_size_bytes:
            raise CommonIngestErrors.quota_exceeded(
                quota_name="max_metadata_size_bytes",
                limit=self.policy.max_metadata_size_bytes,
                current=metadata_size,
                context=context
            )
    
    def _validate_timestamp_sanity(
        self,
        canonical_view: Dict[str, Any],
        context: IngestErrorContext,
        platform_content_id: str
    ) -> None:
        """
        Ensure timestamps are sane (no excessive future drift).
        
        Tier-0 requirement: If uncertain → reject. No silent validation skips.
        Spec explicitly forbids silent acceptance of malformed timestamps.
        """
        # Check canonical view (flattened) first, then fall back to metadata
        created_at = canonical_view.get("created_at")
        if created_at is None:
            metadata = canonical_view.get("_metadata", {})
            created_at = metadata.get("created_at") if isinstance(metadata, dict) else None
        
        if created_at:
            try:
                if isinstance(created_at, str):
                    created_dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                elif isinstance(created_at, (int, float)):
                    # Assume milliseconds if > 1e10, seconds otherwise
                    if created_at > 1e10:
                        created_dt = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc)
                    else:
                        created_dt = datetime.fromtimestamp(created_at, tz=timezone.utc)
                elif isinstance(created_at, datetime):
                    created_dt = created_at
                else:
                    # Reject unclear timestamp types - no silent skip
                    raise IngestErrorBuilder(
                        category=ErrorCategory.VALIDATION,
                        context=context,
                        recovery_hint=RecoveryHint.FATAL
                    ).add_cause(
                        code=IngestErrorCode.INVALID_FORMAT,
                        message=f"created_at has unsupported type: {type(created_at).__name__}",
                        source="content_validator",
                        field_name="created_at",
                        expected_value="str, int, float, or datetime",
                        actual_value=type(created_at).__name__
                    ).build()
                
                now = datetime.now(timezone.utc)
                if created_dt > now:
                    future_drift_seconds = (created_dt - now).total_seconds()
                    
                    if future_drift_seconds > self.policy.max_future_drift_seconds:
                        raise IngestErrorBuilder(
                            category=ErrorCategory.VALIDATION,
                            context=context,
                            recovery_hint=RecoveryHint.FATAL
                        ).add_cause(
                            code=IngestErrorCode.CONSTRAINT_VIOLATION,
                            message=f"created_at is too far in the future ({future_drift_seconds}s drift)",
                            source="content_validator",
                            constraint="max_future_drift_seconds",
                            expected_value=str(self.policy.max_future_drift_seconds),
                            actual_value=str(future_drift_seconds)
                        ).build()
            except (ValueError, TypeError) as e:
                # Reject malformed timestamps - no silent skip
                raise IngestErrorBuilder(
                    category=ErrorCategory.VALIDATION,
                    context=context,
                    recovery_hint=RecoveryHint.FATAL
                ).add_cause(
                    code=IngestErrorCode.INVALID_FORMAT,
                    message=f"created_at timestamp is malformed: {str(e)}",
                    source="content_validator",
                    field_name="created_at",
                    expected_value="valid ISO format string, epoch seconds, or epoch milliseconds",
                    actual_value=str(created_at)
                ).build()
    
    def _validate_content_type(
        self,
        canonical_view: Dict[str, Any],
        context: IngestErrorContext,
        platform_content_id: str
    ) -> None:
        """Validate content type is recognized."""
        content_type = canonical_view.get("content_type", "")
        
        if not content_type:
            raise CommonIngestErrors.missing_required_field(
                field_name="content_type",
                context=context
            )
        
        try:
            ContentType(content_type.lower())
        except (ValueError, TypeError):
            raise CommonIngestErrors.schema_violation(
                field_name="content_type",
                expected="video, image, text, audio, or link",
                actual=content_type,
                context=context
            )
    
    def _validate_account_id(
        self,
        canonical_view: Dict[str, Any],
        context: IngestErrorContext,
        platform_content_id: str
    ) -> None:
        """
        Validate account_id is present and non-empty.
        
        Account ID is part of content identity and required for ownership.
        """
        account_id = canonical_view.get("account_id", "")
        
        if not account_id or not account_id.strip():
            raise CommonIngestErrors.missing_required_field(
                field_name="account_id",
                context=context
            )


# ============================================================================
# CONTENT NORMALIZER (CANONICALIZER)
# ============================================================================

class ContentNormalizer:
    """
    Responsible for lossless normalization.
    
    Examples:
    - platform IDs → canonical strings
    - timestamps → epoch ms (UTC)
    - text fields → normalized Unicode
    - language codes → ISO canonical form
    
    Rules:
    - No information loss
    - No enrichment
    - Deterministic transforms only
    
    Same input → same normalized output.
    """
    
    def __init__(self, schema_version: str):
        self.schema_version = schema_version
    
    def normalize(self, raw_content: Dict[str, Any], context: IngestContext) -> Dict[str, Any]:
        """
        Normalize raw content to canonical form.
        
        Tier-0 requirement: Normalization must be a provably lossless and total function
        over validated inputs. No None, no silent fallback branches, no uncertainty.
        
        validated_input → deterministic total function → canonical_output
        
        If normalization can fail, either:
        - validator is incomplete (Tier-0 violation)
        - normalization is nondeterministic (Tier-0 violation)
        
        Args:
            raw_content: Raw content to normalize (must be pre-validated)
            context: Ingest context for error reporting
        
        Returns:
            Normalized content dictionary (fully deterministic)
        
        Raises:
            BaseIngestError: If normalization fails (must reject, never silently fail)
        """
        error_context = IngestErrorContext(
            pipeline_step="content_normalization",
            run_id=context.run_id,
            input_id=raw_content.get("platform_content_id"),
            entity_type="content",
            entity_id=raw_content.get("platform_content_id")
        )
        
        normalized = {}
        
        # Platform ID normalization (must not fail - validated input)
        platform = raw_content.get("platform", "")
        if not platform:
            raise CommonIngestErrors.missing_required_field(
                field_name="platform",
                context=error_context
            )
        normalized['platform'] = self._normalize_platform(platform)
        
        # Platform content ID normalization (must not fail - validated input)
        platform_content_id = raw_content.get("platform_content_id", "")
        if not platform_content_id:
            raise CommonIngestErrors.missing_required_field(
                field_name="platform_content_id",
                context=error_context
            )
        normalized['platform_content_id'] = self._normalize_platform_id(platform_content_id)
        
        # Content type normalization (must not fail - validated input)
        content_type = raw_content.get("content_type", "")
        if not content_type:
            raise CommonIngestErrors.missing_required_field(
                field_name="content_type",
                context=error_context
            )
        normalized['content_type'] = self._normalize_content_type(content_type)
        
        # Timestamp normalization (UTC epoch milliseconds)
        # Must not silently return None for validated inputs
        metadata = raw_content.get("metadata", {})
        if not isinstance(metadata, dict):
            raise CommonIngestErrors.schema_violation(
                field_name="metadata",
                expected="dict",
                actual=str(type(metadata).__name__),
                context=error_context
            )
        
        created_at = metadata.get("created_at")
        normalized['created_at_epoch_ms'] = self._normalize_timestamp(
            created_at,
            error_context,
            "created_at"
        )
        
        updated_at = metadata.get("updated_at")
        normalized['updated_at_epoch_ms'] = self._normalize_timestamp(
            updated_at,
            error_context,
            "updated_at"
        )
        
        # Metadata normalization (must be lossless and deterministic)
        normalized['metadata'] = self._normalize_metadata(metadata)
        
        # Account ID (required for content ownership - must not fail)
        account_id = raw_content.get("account_id", "")
        if not account_id:
            raise CommonIngestErrors.missing_required_field(
                field_name="account_id",
                context=error_context
            )
        normalized['account_id'] = account_id.strip()
        
        # Schema version (immutable from policy)
        normalized['schema_version'] = self.schema_version
        
        return normalized
    
    def _normalize_platform(self, platform: str) -> str:
        """Normalize platform to canonical lowercase form."""
        return platform.strip().lower()
    
    def _normalize_platform_id(self, platform_id: str) -> str:
        """Normalize platform content ID to stable form."""
        return platform_id.strip()
    
    def _normalize_content_type(self, content_type: str) -> str:
        """Normalize content type to canonical lowercase form."""
        return content_type.strip().lower()
    
    def _normalize_timestamp(
        self,
        timestamp: Any,
        error_context: IngestErrorContext,
        field_name: str
    ) -> Optional[int]:
        """
        Convert timestamp to UTC epoch milliseconds.
        
        Tier-0 requirement: If uncertain → reject. No silent None returns.
        Validator already guards malformed timestamps, but normalizer must be
        self-consistent and not rely on validator correctness.
        
        Args:
            timestamp: Timestamp value to normalize
            error_context: Context for error reporting
            field_name: Name of the timestamp field (for error messages)
        
        Returns:
            UTC epoch milliseconds, or None if timestamp is None (for optional fields)
        
        Raises:
            BaseIngestError: If timestamp is malformed or unsupported type
        """
        if timestamp is None:
            return None
        
        try:
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
                # Reject unclear timestamp types - no silent skip
                raise IngestErrorBuilder(
                    category=ErrorCategory.VALIDATION,
                    context=error_context,
                    recovery_hint=RecoveryHint.FATAL
                ).add_cause(
                    code=IngestErrorCode.INVALID_FORMAT,
                    message=f"{field_name} has unsupported type: {type(timestamp).__name__}",
                    source="content_normalizer",
                    field_name=field_name,
                    expected_value="str, int, float, or datetime",
                    actual_value=type(timestamp).__name__
                ).build()
            
            # Ensure UTC
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            elif dt.tzinfo != timezone.utc:
                dt = dt.astimezone(timezone.utc)
            
            return int(dt.timestamp() * 1000)
        except BaseIngestError:
            # Re-raise IngestErrors as-is
            raise
        except (ValueError, TypeError, OSError) as e:
            # Reject malformed timestamps - no silent None
            # This should not happen if validator is correct, but normalizer
            # must be self-consistent per Tier-0 requirements
            raise IngestErrorBuilder(
                category=ErrorCategory.VALIDATION,
                context=error_context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.INVALID_FORMAT,
                message=f"{field_name} timestamp normalization failed: {str(e)}",
                source="content_normalizer",
                field_name=field_name,
                expected_value="valid ISO format string, epoch seconds, or epoch milliseconds",
                actual_value=str(timestamp)
            ).build() from e
    
    def _normalize_metadata(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize metadata fields without interpretation.
        
        Lossless, deterministic transformation.
        """
        normalized = {}
        
        for key in sorted(metadata.keys()):
            value = metadata[key]
            
            # Normalize nested dictionaries recursively
            if isinstance(value, dict):
                normalized[key] = self._normalize_metadata(value)
            # Normalize text fields to NFC Unicode
            elif isinstance(value, str):
                normalized[key] = self._normalize_text(value)
            # Normalize lists deterministically where appropriate
            elif isinstance(value, list):
                normalized[key] = self._normalize_list(value, key)
            else:
                normalized[key] = value
        
        return normalized
    
    def _normalize_text(self, text: str) -> str:
        """Normalize text to canonical Unicode NFC form."""
        return unicodedata.normalize('NFC', text)
    
    def _normalize_list(self, lst: List[Any], key: str) -> List[Any]:
        """
        Normalize lists appropriately.
        
        Tier-0 requirement: Schema-driven normalization, not heuristic normalization.
        
        KNOWN LIMITATION: Currently preserves order for all lists to maintain determinism.
        This is a Tier-0 compliance gap that must be addressed:
        - Schema registry must define which fields are sets vs ordered lists
        - List semantics affect hashing identity
        - At scale, this introduces semantic drift risk between platforms
        
        This limitation is documented and must be resolved before production deployment
        at 5M+ traffic scale. Until schema registry is available, list normalization
        is deterministic but not schema-authoritative.
        """
        # Preserve order for deterministic normalization
        # CRITICAL: This is a known Tier-0 compliance gap.
        # Schema-driven normalization via explicit schema registry is required.
        # Current behavior: All lists preserve order (deterministic but not schema-authoritative)
        return [
            self._normalize_text(x) if isinstance(x, str) else x
            for x in lst
        ]


# ============================================================================
# CONTENT DEDUPLICATOR (IDENTITY AUTHORITY)
# ============================================================================

class ContentDeduplicator:
    """
    Hard authority boundary for content identity.
    
    Tier-0 requirement: This is the SINGLE identity authority. No merge logic, no soft equivalence.
    
    Deduplication keys (all required):
    - platform: str
    - platform_content_id: str
    - account_id: str
    - canonical_hash: str (post-normalization)
    
    Guarantees:
    - Duplicate (same identity + same hash) → REPLAY_SAFE (return existing)
    - Conflict (same identity + different hash) → FATAL (reject)
    - New (no existing match) → admitted
    
    Rules:
    - duplicates NEVER overwrite
    - conflicts are FATAL (no merge, no soft equivalence)
    - idempotency is mandatory
    - no ambiguity resolution allowed
    
    Tier-0 requirement: ContentStore is mandatory. Identity enforcement cannot be conditional.
    """
    
    def __init__(self, content_store: ContentStore):
        if content_store is None:
            raise ValueError("ContentStore is mandatory for ContentDeduplicator - idempotency is mandatory")
        self.content_store = content_store
    
    def check(
        self,
        normalized_content: Dict[str, Any],
        canonical_hash: str,
        context: IngestContext
    ) -> Tuple[DeduplicationStatus, Optional[ContentFact]]:
        """
        Check deduplication status of normalized content.
        
        Tier-0 requirement: This is the hard authority boundary. All identity checks
        must go through this method. No bypass paths allowed.
        
        Returns:
            Tuple of (status, existing_fact_if_known)
            - (NEW, None): Content is new and can be admitted
            - (KNOWN, ContentFact): Content is duplicate (same identity + same hash) → REPLAY_SAFE
            - (CONFLICTING, ContentFact): Content conflicts (same identity + different hash) → FATAL
        """
        platform = normalized_content['platform']
        platform_content_id = normalized_content['platform_content_id']
        account_id = normalized_content.get('account_id', '')
        
        # Validate all deduplication keys are present
        if not platform or not platform_content_id or not account_id:
            error_context = IngestErrorContext(
                pipeline_step="deduplication",
                run_id=context.run_id,
                input_id=platform_content_id,
                entity_type="content",
                entity_id=platform_content_id
            )
            raise CommonIngestErrors.invariant_broken(
                invariant_name="deduplication_keys_required",
                violation_message="All deduplication keys (platform, platform_content_id, account_id) must be present",
                context=error_context
            )
        
        # Check by platform identifiers (primary identity check)
        existing_by_platform = self.content_store.get_by_platform_id(
            platform,
            platform_content_id,
            account_id
        )
        
        if existing_by_platform:
            return self._compare_with_existing(canonical_hash, existing_by_platform)
        
        # Check by canonical hash (catch resubmissions with different platform IDs)
        existing_by_hash = self.content_store.get_by_canonical_hash(canonical_hash)
        
        if existing_by_hash:
            return self._compare_with_existing(canonical_hash, existing_by_hash)
        
        return (DeduplicationStatus.NEW, None)
    
    def _compare_with_existing(
        self,
        canonical_hash: str,
        existing: ContentFact
    ) -> Tuple[DeduplicationStatus, ContentFact]:
        """
        Compare canonical hash with existing content.
        
        Tier-0 requirement: No merge logic, no soft equivalence, no ambiguity resolution.
        Exact hash match → KNOWN (replay-safe). Hash mismatch → CONFLICTING (fatal).
        
        Returns:
            - (KNOWN, ContentFact): Identical content (same hash) → REPLAY_SAFE
            - (CONFLICTING, ContentFact): Different content (different hash) → FATAL
        """
        if existing.canonical_hash == canonical_hash:
            return (DeduplicationStatus.KNOWN, existing)
        
        # Hash mismatch with same identity → FATAL conflict
        # No merge, no soft equivalence, no ambiguity resolution
        return (DeduplicationStatus.CONFLICTING, existing)


# ============================================================================
# CONTENT INGEST INVARIANTS (ABSOLUTE)
# ============================================================================

class ContentIngestInvariants:
    """
    Enforces absolute invariants for content ingestion.
    
    Violation → ingestion hard stop.
    """
    
    @staticmethod
    def enforce(
        normalized_content: Dict[str, Any],
        content_fact: ContentFact,
        context: IngestContext,
        deduplication_executed: bool = False
    ) -> None:
        """
        Enforce all invariants.
        
        Args:
            normalized_content: Normalized content dictionary
            content_fact: Content fact to validate
            context: Ingest context
            deduplication_executed: Flag indicating deduplication was actually executed
        
        Raises BaseIngestError on any violation.
        """
        error_context = IngestErrorContext(
            pipeline_step="invariant_enforcement",
            run_id=context.run_id,
            input_id=normalized_content.get("platform_content_id"),
            entity_type="content",
            entity_id=content_fact.content_id
        )
        
        ContentIngestInvariants._no_content_without_stable_id(
            content_fact,
            error_context
        )
        ContentIngestInvariants._no_partial_ingestion(
            content_fact,
            error_context
        )
        ContentIngestInvariants._no_ingestion_without_full_context(
            context,
            error_context
        )
        ContentIngestInvariants._no_silent_deduplication(
            normalized_content,
            content_fact,
            error_context,
            deduplication_executed=deduplication_executed
        )
        ContentIngestInvariants._no_mutation_of_existing_facts(
            content_fact,
            error_context
        )
        ContentIngestInvariants._no_platform_leakage(
            normalized_content,
            content_fact,
            error_context
        )
    
    @staticmethod
    def _no_content_without_stable_id(
        content_fact: ContentFact,
        context: IngestErrorContext
    ) -> None:
        """Invariant: Every content must have a stable platform ID."""
        if not content_fact.platform_content_id or not content_fact.platform_content_id.strip():
            raise CommonIngestErrors.invariant_broken(
                invariant_name="platform_content_id_required",
                violation_message="Content must have stable platform_content_id",
                context=context
            )
    
    @staticmethod
    def _no_partial_ingestion(
        content_fact: ContentFact,
        context: IngestErrorContext
    ) -> None:
        """Invariant: Content must have all required fields."""
        if not content_fact.normalized_metadata:
            raise CommonIngestErrors.invariant_broken(
                invariant_name="metadata_required",
                violation_message="Content must have metadata",
                context=context
            )
    
    @staticmethod
    def _no_ingestion_without_full_context(
        context: IngestContext,
        error_context: IngestErrorContext
    ) -> None:
        """Invariant: Every ingestion must have full execution context."""
        if not context.run_id or not context.run_id.strip():
            raise CommonIngestErrors.invariant_broken(
                invariant_name="context_required",
                violation_message="Ingestion must have run_id",
                context=error_context
            )
    
    @staticmethod
    def _no_silent_deduplication(
        normalized_content: Dict[str, Any],
        content_fact: ContentFact,
        error_context: IngestErrorContext,
        deduplication_executed: bool = False
    ) -> None:
        """
        Invariant: Deduplication must be explicit, never silent.
        
        Executable guard: Verify that deduplication was ACTUALLY EXECUTED before persistence.
        This prevents future refactors from accidentally bypassing deduplicator.
        
        Args:
            deduplication_executed: Flag indicating deduplication was actually executed
        """
        # Executable guard: Verify deduplication was actually executed
        if not deduplication_executed:
            raise CommonIngestErrors.invariant_broken(
                invariant_name="deduplication_must_execute",
                violation_message="Deduplication must be explicitly executed before persistence. Bypassing deduplicator is forbidden.",
                context=error_context
            )
        
        # Executable guard: Verify deduplication keys are present
        platform = normalized_content.get('platform')
        platform_content_id = normalized_content.get('platform_content_id')
        account_id = normalized_content.get('account_id')
        
        if not platform or not platform_content_id or not account_id:
            raise CommonIngestErrors.invariant_broken(
                invariant_name="deduplication_keys_required",
                violation_message="Deduplication keys must be present before persistence",
                context=error_context
            )
        
        # Executable guard: Verify canonical hash is present
        if not content_fact.canonical_hash:
            raise CommonIngestErrors.invariant_broken(
                invariant_name="canonical_hash_required",
                violation_message="Canonical hash must be present (deduplication requires it)",
                context=error_context
            )
    
    @staticmethod
    def _no_mutation_of_existing_facts(
        content_fact: ContentFact,
        error_context: IngestErrorContext
    ) -> None:
        """
        Invariant: Existing facts are immutable.
        
        Executable guard: Verify fact is frozen and deeply immutable.
        Blueprint: "Once admitted, content is read-only forever."
        
        This checks that nested structures are frozen to prevent post-persistence mutation.
        """
        # ContentFact is a frozen dataclass, but verify deep immutability
        if not isinstance(content_fact.normalized_metadata, dict):
            raise CommonIngestErrors.invariant_broken(
                invariant_name="metadata_immutability",
                violation_message="Metadata must be immutable dict",
                context=error_context
            )
        
        # Verify nested structures are frozen (lists should be tuples, dicts should be immutable)
        # This is a best-effort check - true immutability requires types.MappingProxyType
        # but we verify structure is frozen at creation time
        ContentIngestInvariants._verify_deep_immutability(
            content_fact.normalized_metadata,
            error_context,
            path="normalized_metadata"
        )
    
    @staticmethod
    def _verify_deep_immutability(
        obj: Any,
        error_context: IngestErrorContext,
        path: str = "root"
    ) -> None:
        """
        Recursively verify deep immutability of nested structures.
        
        Checks that lists are tuples (frozen) and dicts are properly structured.
        """
        if isinstance(obj, dict):
            for key, value in obj.items():
                ContentIngestInvariants._verify_deep_immutability(
                    value,
                    error_context,
                    path=f"{path}.{key}"
                )
        elif isinstance(obj, list):
            # Lists should have been converted to tuples during deep freeze
            # If we see a list here, it means deep freeze didn't work correctly
            raise CommonIngestErrors.invariant_broken(
                invariant_name="deep_immutability",
                violation_message=f"Nested list at {path} must be frozen (tuple). Deep immutability violation.",
                context=error_context
            )
        elif isinstance(obj, tuple):
            # Tuples are immutable, verify contents
            for i, item in enumerate(obj):
                ContentIngestInvariants._verify_deep_immutability(
                    item,
                    error_context,
                    path=f"{path}[{i}]"
                )
    
    @staticmethod
    def _no_platform_leakage(
        normalized_content: Dict[str, Any],
        content_fact: ContentFact,
        error_context: IngestErrorContext
    ) -> None:
        """
        Invariant: No platform-specific assumptions leak into canonical form.
        
        Executable guard: Verify canonical form is platform-agnostic.
        """
        # Verify platform is normalized (lowercase, no platform-specific encoding)
        platform = content_fact.platform
        if platform != platform.lower():
            raise CommonIngestErrors.invariant_broken(
                invariant_name="platform_normalization",
                violation_message="Platform must be normalized (lowercase)",
                context=error_context
            )


# ============================================================================
# CONTENT INGESTOR (ORCHESTRATOR)
# ============================================================================

class ContentIngestor:
    """
    Primary entrypoint for content metadata ingestion.
    
    Converts raw platform metadata into canonical, immutable content facts.
    
    Tier-0 requirement: Idempotency guarantee at ingestor boundary.
    
    Guaranteed: ingest(X) == ingest(X) == ingest(X) across:
    - retries
    - reordered delivery
    - concurrent workers
    - cross-region replication
    
    This is achieved by:
    - canonical hash strictly post-normalization
    - dedup executed before persistence
    - canonical ID deterministic (pure function)
    - persistence atomic (exactly-once)
    - no ordering dependence anywhere
    
    Execution order (STRICT - NO REORDERING):
    1. Validate ingest context
    2. Apply ingest policy (single deterministic pass)
    3. Validate raw input (against canonical schema projection)
    4. Normalize metadata (lossless, total function)
    5. Compute canonical hash (post-normalization)
    6. Detect duplicates (hard authority boundary)
    7. Assign canonical content_id (pure function)
    8. Create immutable content fact
    9. Enforce invariants (executable guards)
    10. Persist atomically (exactly-once, no fallback)
    
    No reordering. No shortcuts. No defensive convenience code.
    """
    
    def __init__(
        self,
        policy: ContentIngestPolicy,
        validator: ContentValidator,
        normalizer: ContentNormalizer,
        deduplicator: ContentDeduplicator,
        content_store: ContentStore,
        audit_logger: Optional[AuditLogger] = None
    ):
        self.policy = policy
        self.validator = validator
        self.normalizer = normalizer
        self.deduplicator = deduplicator
        self.content_store = content_store
        self.audit_logger = audit_logger
    
    def ingest(
        self,
        raw_content: Dict[str, Any],
        context: IngestContext
    ) -> IngestResult:
        """
        Ingest raw content metadata through complete validation pipeline.
        
        Args:
            raw_content: Untrusted platform metadata
            context: Execution identity
        
        Returns:
            IngestResult - either admission or deterministic rejection
        """
        platform = raw_content.get("platform", "unknown")
        platform_content_id = raw_content.get("platform_content_id", "unknown")
        
        # Log start (no fallback - audit logger is optional but if provided must work)
        if self.audit_logger is not None:
            self.audit_logger.log_ingest_started(platform, platform_content_id, context)
        
        try:
            # Step 1: Validate ingest context
            self._validate_context(context)
            
            # Step 2: Apply ingest policy (single deterministic pass in validator)
            # All policy enforcement occurs in validator.validate() before normalization.
            # This includes: platform support, schema version, required fields, field types,
            # metadata size limits, timestamp sanity, content type, account_id.
            # No late policy branching allowed.
            self.validator.validate(raw_content, context)
            
            # Step 4: Normalize metadata
            normalized = self.normalizer.normalize(raw_content, context)
            
            # Step 5: Compute canonical hash (needed for deduplication)
            canonical_hash = self._compute_canonical_hash(normalized)
            
            # Step 6: Detect duplicates (hard authority boundary)
            dedup_status, existing_content = self.deduplicator.check(
                normalized,
                canonical_hash,
                context
            )
            deduplication_executed = True  # Track that deduplication was actually executed
            
            if dedup_status == DeduplicationStatus.CONFLICTING:
                raise IngestErrorBuilder(
                    category=ErrorCategory.STATE,
                    context=IngestErrorContext(
                        pipeline_step="deduplication",
                        run_id=context.run_id,
                        input_id=platform_content_id,
                        entity_type="content",
                        entity_id=platform_content_id
                    ),
                    recovery_hint=RecoveryHint.FATAL
                ).add_cause(
                    code=IngestErrorCode.STATE_CONFLICT,
                    message="Content conflicts with existing content - same identity, different hash",
                    source="content_deduplicator",
                    actual_value=platform_content_id
                ).build()
            
            if dedup_status == DeduplicationStatus.KNOWN:
                # Safe idempotent case: return existing content (REPLAY_SAFE)
                # This proves idempotency: ingest(X) == ingest(X) returns same result
                if self.audit_logger is not None and existing_content is not None:
                    self.audit_logger.log_duplicate_detected(
                        existing_content.content_id,
                        context
                    )
                
                return create_deduped_result(
                    context=context,
                    existing_fact_ids=[existing_content.content_id] if existing_content else []
                )
            
            # Step 7: Assign canonical content_id
            content_id = self._assign_content_id(normalized, canonical_hash)
            
            # Step 8: Create immutable content fact
            content_fact = self._create_fact(
                content_id,
                canonical_hash,
                normalized,
                context
            )
            
            # Step 9: Enforce invariants (with deduplication execution proof)
            ContentIngestInvariants.enforce(
                normalized,
                content_fact,
                context,
                deduplication_executed=deduplication_executed
            )
            
            # Step 10: Persist immutable fact atomically
            # Tier-0 requirement: Atomic persistence is MANDATORY. No fallback, no best-effort.
            # If store cannot guarantee atomicity → ingestion must reject, not downgrade.
            # 
            # Exactly-once atomic admission at storage boundary:
            # persist_if_absent(content_id, canonical_hash, fact) -> PersistResult
            #
            # This is the ONLY persistence path. No non-atomic fallback allowed.
            try:
                success, existing = self.content_store.persist_atomic(content_fact, content_id)
            except AttributeError:
                # Store doesn't implement persist_atomic → fatal error
                raise IngestErrorBuilder(
                    category=ErrorCategory.INFRA,
                    context=IngestErrorContext(
                        pipeline_step="persistence",
                        run_id=context.run_id,
                        input_id=platform_content_id,
                        entity_type="content",
                        entity_id=content_id
                    ),
                    recovery_hint=RecoveryHint.FATAL
                ).add_cause(
                    code=IngestErrorCode.INFRA_ERROR,
                    message="ContentStore must implement persist_atomic for Tier-0 compliance. No fallback allowed.",
                    source="content_ingestor",
                    expected_value="ContentStore with persist_atomic method",
                    actual_value="ContentStore without atomic persistence"
                ).build()
            if not success and existing:
                # Race condition: another process created the same content_id
                # This should be extremely rare with full 256-bit hash, but handle it
                if existing.canonical_hash == canonical_hash:
                    # Same content, return existing (idempotency proof)
                    if self.audit_logger is not None:
                        self.audit_logger.log_duplicate_detected(
                            existing.content_id,
                            context
                        )
                    return create_deduped_result(
                        context=context,
                        existing_fact_ids=[existing.content_id]
                    )
                else:
                    # Hash collision - this should never happen with SHA-256
                    # but if it does, it's a fatal error
                    raise IngestErrorBuilder(
                        category=ErrorCategory.INFRA,
                        context=IngestErrorContext(
                            pipeline_step="persistence",
                            run_id=context.run_id,
                            input_id=platform_content_id,
                            entity_type="content",
                            entity_id=content_id
                        ),
                        recovery_hint=RecoveryHint.FATAL
                    ).add_cause(
                        code=IngestErrorCode.HASH_COLLISION,
                        message=f"Hash collision detected: content_id {content_id} exists with different hash",
                        source="content_store",
                        expected_value=canonical_hash,
                        actual_value=existing.canonical_hash
                    ).build()
            
            # Log success (no fallback)
            if self.audit_logger is not None:
                self.audit_logger.log_ingest_succeeded(content_id, context)
            
            return create_accepted_result(
                context=context,
                fact_ids=[content_id]
            )
            
        except BaseIngestError as e:
            # Log failure (no fallback)
            if self.audit_logger is not None:
                self.audit_logger.log_ingest_failed(platform, platform_content_id, e, context)
            
            # Map error category to proper RejectionReason
            # Tier-0 requirement: Preserve semantic error taxonomy (INPUT/VALIDATION/AUTHORITY)
            rejection_reason = self._map_error_to_rejection_reason(e)
            
            # Convert to rejection result
            return create_rejected_result(
                context=context,
                reason=rejection_reason,
                detail=str(e)
            )
        except Exception as e:
            # Wrap unexpected errors
            error = IngestErrorBuilder(
                category=ErrorCategory.INFRA,
                context=IngestErrorContext(
                    pipeline_step="content_ingestion",
                    run_id=context.run_id,
                    input_id=platform_content_id,
                    entity_type="content",
                    entity_id=platform_content_id
                ),
                recovery_hint=RecoveryHint.REQUIRES_MANUAL_REVIEW
            ).add_cause(
                code=IngestErrorCode.SERIALIZATION_FAILED,
                message=f"unexpected error during ingestion: {str(e)}",
                source="content_ingestor"
            ).build()
            
            if self.audit_logger is not None:
                self.audit_logger.log_ingest_failed(platform, platform_content_id, error, context)
            
            return create_rejected_result(
                context=context,
                reason=RejectionReason.OTHER,
                detail=str(e)
            )
    
    def _validate_context(self, context: IngestContext) -> None:
        """
        Validate ingestion context.
        
        Tier-0 requirement: Strict context enforcement at ingestion boundary.
        Relying solely on external __post_init__ is unsafe in Tier-0 boundaries.
        """
        error_context = IngestErrorContext(
            pipeline_step="context_validation",
            run_id=context.run_id,
            input_id=None,
            entity_type="content",
            entity_id=None
        )
        
        # Validate run_id is present and non-empty
        if not context.run_id or not context.run_id.strip():
            raise CommonIngestErrors.missing_required_field(
                field_name="context.run_id",
                context=error_context
            )
        
        # Validate pipeline_version is present and non-empty
        if not context.pipeline_version or not context.pipeline_version.strip():
            raise CommonIngestErrors.missing_required_field(
                field_name="context.pipeline_version",
                context=error_context
            )
        
        # Validate timestamp is positive
        if context.timestamp_ms <= 0:
            raise IngestErrorBuilder(
                category=ErrorCategory.VALIDATION,
                context=error_context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.CONSTRAINT_VIOLATION,
                message=f"context.timestamp_ms must be positive, got {context.timestamp_ms}",
                source="content_ingestor",
                constraint="timestamp_ms_positive",
                expected_value="positive integer",
                actual_value=str(context.timestamp_ms)
            ).build()
        
        # Validate context_hash is present and correct length (SHA-256 = 64 hex chars)
        if not context.context_hash or len(context.context_hash) != 64:
            raise IngestErrorBuilder(
                category=ErrorCategory.VALIDATION,
                context=error_context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.INVALID_FORMAT,
                message=f"context.context_hash must be 64 characters (SHA-256), got {len(context.context_hash) if context.context_hash else 0}",
                source="content_ingestor",
                constraint="context_hash_format",
                expected_value="64-character hex string",
                actual_value=f"{len(context.context_hash) if context.context_hash else 0} characters"
            ).build()
    
    def _compute_canonical_hash(self, normalized: Dict[str, Any]) -> str:
        """
        Compute deterministic hash of normalized content.
        
        Same normalized content → same hash.
        
        Tier-0 requirement: Include schema_version to prevent cross-schema conflicts.
        """
        # Include all identifying fields in hash, including schema_version
        hash_input = {
            'platform': normalized['platform'],
            'platform_content_id': normalized['platform_content_id'],
            'account_id': normalized.get('account_id', ''),
            'content_type': normalized['content_type'],
            'metadata': normalized['metadata'],
            'schema_version': normalized.get('schema_version', '')  # Prevent cross-schema conflicts
        }
        
        canonical_json = json.dumps(hash_input, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()
    
    def _assign_content_id(self, normalized: Dict[str, Any], canonical_hash: str) -> str:
        """
        Assign globally unique, deterministic content ID.
        
        Tier-0 requirement: This is a PURE FUNCTION. Must be:
        - deterministic: same input → same output
        - side-effect free: no storage, no time, no runtime context
        - based only on normalized canonical content
        - independent of storage, time, or execution order
        
        Content IDs MUST:
        - be globally unique
        - be deterministic if input is identical
        - never encode platform assumptions
        - never be reused
        - never include entropy (timestamps, UUID4, runtime context)
        
        Formula: content_id = f(normalized_metadata, schema_version)
        Nothing else. Ever.
        
        Once assigned, never changes — even after recovery.
        
        Tier-0 requirement: Use full 256-bit hash to prevent birthday-bound collisions
        at 5M+ traffic scale. Truncation is unsafe for canonical identity authority.
        """
        # Pure function: content_id = f(canonical_hash)
        # No entropy, no timestamps, no UUIDs, no runtime context
        # Use full 256-bit hash (64 hex chars) for collision resistance
        # At 5M+ traffic, 64-bit truncation has non-theoretical collision risk
        return f"content_{canonical_hash}"
    
    def _create_fact(
        self,
        content_id: str,
        canonical_hash: str,
        normalized: Dict[str, Any],
        context: IngestContext
    ) -> ContentFact:
        """
        Create immutable content fact from normalized data.
        
        Tier-0 requirement: Deep-freeze metadata to prevent mutability leaks.
        Blueprint: "Once admitted, content is read-only forever."
        
        Deep immutability is enforced by recursively freezing all nested structures.
        """
        ingested_at_ms = context.timestamp_ms
        
        # Deep freeze metadata to prevent mutability leaks
        # ContentFact is frozen, but nested dicts/lists can still be mutated
        # Deep freeze ensures immutability invariant is preserved at all levels
        frozen_metadata = self._deep_freeze(normalized['metadata'])
        
        return ContentFact(
            content_id=content_id,
            platform=normalized['platform'],
            platform_content_id=normalized['platform_content_id'],
            content_type=normalized['content_type'],
            account_id=normalized.get('account_id', ''),
            normalized_metadata=frozen_metadata,
            canonical_hash=canonical_hash,
            schema_version=normalized['schema_version'],
            created_at_epoch_ms=normalized['created_at_epoch_ms'] or ingested_at_ms,
            updated_at_epoch_ms=normalized.get('updated_at_epoch_ms'),
            ingested_at_ms=ingested_at_ms
        )
    
    def _deep_freeze(self, obj: Any) -> Any:
        """
        Recursively freeze nested structures to enforce deep immutability.
        
        Tier-0 requirement: Once admitted, content is read-only forever.
        This prevents accidental mutation post-persistence.
        
        Converts:
        - Lists → Tuples (immutable)
        - Dicts → Frozen dicts (nested structures recursively frozen)
        - Primitives → Unchanged (already immutable)
        """
        if isinstance(obj, dict):
            # Freeze dict by recursively freezing all values
            frozen_dict = {}
            for key, value in obj.items():
                frozen_dict[key] = self._deep_freeze(value)
            # Return as dict but nested structures are frozen
            # In production, consider using types.MappingProxyType for true immutability
            # For now, we rely on frozen dataclass + deep freeze + documentation
            return frozen_dict
        elif isinstance(obj, list):
            # Freeze list by converting to tuple (immutable)
            return tuple(self._deep_freeze(item) for item in obj)
        elif isinstance(obj, (str, int, float, bool, type(None))):
            # Primitives are already immutable
            return obj
        else:
            # For other types, return as-is (will be caught by validation if problematic)
            return obj
    
    def _map_error_to_rejection_reason(self, error: BaseIngestError) -> RejectionReason:
        """
        Map IngestError category to appropriate RejectionReason.
        
        Tier-0 requirement: Preserve semantic error taxonomy.
        All failures should not collapse into POLICY_VIOLATION.
        """
        # Map by error category to preserve taxonomy
        if error.category == ErrorCategory.INPUT:
            # Input-level issues
            if error.primary_error_code == IngestErrorCode.DUPLICATE_INPUT:
                return RejectionReason.DUPLICATE
            elif error.primary_error_code == IngestErrorCode.OUT_OF_ORDER_INPUT:
                return RejectionReason.OUT_OF_ORDER
            elif error.primary_error_code == IngestErrorCode.MALFORMED_INPUT:
                return RejectionReason.SCHEMA_INVALID
            else:
                return RejectionReason.SCHEMA_INVALID
        
        elif error.category == ErrorCategory.VALIDATION:
            # Validation failures
            if error.primary_error_code == IngestErrorCode.SCHEMA_VIOLATION:
                return RejectionReason.SCHEMA_INVALID
            elif error.primary_error_code == IngestErrorCode.MISSING_REQUIRED_FIELD:
                return RejectionReason.MISSING_REQUIRED_FIELD
            elif error.primary_error_code == IngestErrorCode.INVALID_FORMAT:
                return RejectionReason.SCHEMA_INVALID
            elif error.primary_error_code == IngestErrorCode.UNSUPPORTED_VERSION:
                return RejectionReason.UNSUPPORTED_VERSION
            elif error.primary_error_code == IngestErrorCode.INVARIANT_BROKEN:
                return RejectionReason.INVARIANT_VIOLATION
            else:
                return RejectionReason.SCHEMA_INVALID
        
        elif error.category == ErrorCategory.AUTHORITY:
            # Authority/permission issues
            if error.primary_error_code == IngestErrorCode.QUOTA_EXCEEDED:
                return RejectionReason.QUOTA_EXCEEDED
            elif error.primary_error_code == IngestErrorCode.POLICY_VIOLATION:
                return RejectionReason.POLICY_VIOLATION
            elif error.primary_error_code == IngestErrorCode.AUTHORITY_INVALID:
                return RejectionReason.AUTHORITY_MISMATCH
            else:
                return RejectionReason.POLICY_VIOLATION
        
        elif error.category == ErrorCategory.STATE:
            # State conflicts
            if error.primary_error_code == IngestErrorCode.STATE_CONFLICT:
                return RejectionReason.STATE_CONFLICT
            elif error.primary_error_code == IngestErrorCode.DUPLICATE_INPUT:
                return RejectionReason.DUPLICATE
            else:
                return RejectionReason.STATE_CONFLICT
        
        elif error.category == ErrorCategory.INFRA:
            # Infrastructure failures
            return RejectionReason.OTHER
        
        else:
            # Default fallback (should be rare)
            return RejectionReason.OTHER


# ============================================================================
# FACTORY & SETUP
# ============================================================================

def create_content_ingestor(
    policy: ContentIngestPolicy,
    content_store: ContentStore,
    audit_logger: Optional[AuditLogger] = None
) -> ContentIngestor:
    """
    Factory function to create fully configured ContentIngestor.
    
    This is the recommended way to instantiate the ingestion pipeline.
    """
    validator = ContentValidator(policy=policy)
    
    normalizer = ContentNormalizer(
        schema_version=policy.canonical_schema_version
    )
    
    deduplicator = ContentDeduplicator(
        content_store=content_store
    )
    
    return ContentIngestor(
        policy=policy,
        validator=validator,
        normalizer=normalizer,
        deduplicator=deduplicator,
        content_store=content_store,
        audit_logger=audit_logger
    )


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    # Enums
    'ContentType',
    'DeduplicationStatus',
    
    # Data Models
    'ContentIngestPolicy',
    'ContentFact',
    
    # Components
    'ContentValidator',
    'ContentNormalizer',
    'ContentDeduplicator',
    'ContentIngestInvariants',
    
    # Main Ingestor
    'ContentIngestor',
    
    # Factory
    'create_content_ingestor',
    
    # Protocols
    'ContentStore',
    'AuditLogger',
]
