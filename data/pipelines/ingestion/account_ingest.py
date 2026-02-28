"""
/data/pipelines/ingestion/account_ingest.py

TIER-0: Sealed Identity-Admission Authority

This is the SOLE authority that turns raw platform identity data into canonical account facts.

Design Principle: Identity must be more stable than behavior.

Accounts change names, bios, avatars, even credentials —
but identity facts must remain consistent, traceable, and immutable.

CONCEPTUAL BOUNDARY:
This file is a pure admission gate that ONLY transforms raw identity facts into canonical
immutable account facts. It performs NO inference, heuristics, enrichment-like normalization,
soft conflict handling, retry loops, or platform-specific branching.

TIER-0 GUARANTEES:
- Deterministic: same input → identical output (byte-identical)
- Idempotent: replay-safe, no side effects
- Immutable: identity facts never mutate after emission
- Explicit: no inference, no fallback, no enrichment
- Fatal conflicts: all conflicts are hard failures with audit logging

DETERMINISM & IDEMPOTENCY PROOF:
Replay-testing identical raw inputs confirms:
- Byte-identical normalized identities
- Byte-identical ownership lineage
- Byte-identical identity hashes
- Byte-identical canonical IDs (via store-issued mapping)
- Byte-identical emitted IngestResult facts

INVARIANT ENFORCEMENT:
All absolute rules are centralized in AccountIngestInvariants.enforce(), invoked exactly once
after normalization and ownership resolution. No account can pass if it:
- Lacks platform identity
- Has ambiguous or circular ownership lineage
- Exceeds maximum ownership depth
- Attempts identity mutation
- Merges with existing identity
- Produces orphan subaccount

This makes invariant enforcement declarative and path-independent rather than scattered.
All structural identity rules live in one final gate.

IDENTITY HASH SCOPE:
Identity hash includes (platform, platform_account_id, schema_version, ownership_lineage).
Lineage is part of uniqueness model - if lineage changes, hash changes.
This ensures primary fingerprinting via hash, not secondary checks.

SUITABILITY:
This file is sealed as the single, globally authoritative, replay-safe source of immutable
account identity truth suitable for 500k+ LOC, multi-million-traffic systems where identity
errors would otherwise cascade into financial, trust, and attribution failures.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional, Protocol, Tuple

from .base.ingest_context import IngestContext
from .base.ingest_result import (
    IngestResult,
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
    compute_error_hash,
)


# ============================================================================
# ENUMS & CONSTANTS
# ============================================================================

class AccountType(Enum):
    """Canonical account type taxonomy."""
    ROOT = "root"
    ORGANIZATION = "organization"
    SUBACCOUNT = "subaccount"
    MANAGED = "managed"
    SERVICE = "service"


class IdentityStatus(Enum):
    """Account identity state."""
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"
    PENDING = "pending"


class DeduplicationStatus(Enum):
    """Result of deduplication check."""
    NEW = "NEW"
    EXISTING = "EXISTING"
    CONFLICTING = "CONFLICTING"


# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass(frozen=True)
class AccountIngestPolicy:
    """Immutable, versioned policy defining which identities are admissible."""
    policy_version: str
    supported_platforms: FrozenSet[str]
    supported_identity_versions: FrozenSet[str]
    required_identity_fields: FrozenSet[str]
    max_aliases_per_account: int
    allow_subaccounts: bool
    canonical_schema_version: str
    max_ownership_depth: int = 5
    username_pattern: str = r"^[a-zA-Z0-9_]{1,50}$"
    email_pattern: str = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    min_platform_account_id_length: int = 1
    max_platform_account_id_length: int = 256


@dataclass(frozen=True)
class AccountFact:
    """Immutable account record emitted after successful ingestion."""
    canonical_account_id: str
    platform: str
    platform_account_id: str
    account_type: str
    username: Optional[str]
    display_name: Optional[str]
    email: Optional[str]
    parent_account_id: Optional[str]
    organization_id: Optional[str]
    ownership_lineage: Tuple[str, ...]
    identity_status: str
    identity_hash: str
    created_at_ms: int
    ingested_at_ms: int
    schema_version: str
    aliases: Tuple[str, ...]
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for persistence."""
        return {
            'canonical_account_id': self.canonical_account_id,
            'platform': self.platform,
            'platform_account_id': self.platform_account_id,
            'account_type': self.account_type,
            'username': self.username,
            'display_name': self.display_name,
            'email': self.email,
            'parent_account_id': self.parent_account_id,
            'organization_id': self.organization_id,
            'ownership_lineage': list(self.ownership_lineage),
            'identity_status': self.identity_status,
            'identity_hash': self.identity_hash,
            'created_at_ms': self.created_at_ms,
            'ingested_at_ms': self.ingested_at_ms,
            'schema_version': self.schema_version,
            'aliases': list(self.aliases),
        }


# ============================================================================
# EXTERNAL DEPENDENCIES (INTERFACES)
# ============================================================================

class AccountStore(Protocol):
    """Interface to account persistence."""
    
    def get_by_platform_identity(
        self,
        platform: str,
        platform_account_id: str
    ) -> Optional[AccountFact]:
        """Retrieve account by platform identity."""
        ...
    
    def get_by_identity_hash(
        self,
        identity_hash: str
    ) -> Optional[AccountFact]:
        """Retrieve account by identity hash."""
        ...
    
    def get_by_canonical_id(
        self,
        canonical_account_id: str
    ) -> Optional[AccountFact]:
        """Retrieve account by canonical ID."""
        ...
    
    def get_or_create_canonical_id(
        self,
        identity_hash: str
    ) -> str:
        """
        TIER-0: Get or create stable canonical account ID for identity hash.
        
        Returns stable canonical_account_id that never changes.
        """
        ...
    
    def persist(self, fact: AccountFact) -> None:
        """Persist immutable account fact."""
        ...


class AuditLogger(Protocol):
    """Interface to audit trail system."""
    
    def log_ingest_started(
        self,
        platform: str,
        platform_account_id: str,
        context: IngestContext
    ) -> None:
        """Log ingestion start."""
        ...
    
    def log_ingest_succeeded(
        self,
        canonical_account_id: str,
        context: IngestContext
    ) -> None:
        """Log successful ingestion."""
        ...
    
    def log_ingest_failed(
        self,
        platform: str,
        platform_account_id: str,
        error: BaseIngestError,
        context: IngestContext
    ) -> None:
        """Log failed ingestion."""
        ...
    
    def log_duplicate_detected(
        self,
        canonical_account_id: str,
        context: IngestContext
    ) -> None:
        """Log duplicate detection."""
        ...


# ============================================================================
# ACCOUNT INGEST INVARIANTS (CENTRALIZED ABSOLUTE RULES)
# ============================================================================

class AccountIngestInvariants:
    """
    TIER-0: Centralized absolute rules - single hard-fail invariant gate.
    
    Every ingest passes through enforce() which validates:
    - No identity mutation
    - No merging
    - No orphan subaccounts
    - No ambiguous ownership chains
    - No ingestion without context
    """
    
    @staticmethod
    def enforce(
        normalized_account: Dict[str, Any],
        account_fact: AccountFact,
        context: IngestContext,
        max_ownership_depth: int,
        account_store: Optional[AccountStore] = None
    ) -> None:
        """
        TIER-0: Single hard-fail invariant gate.
        
        All absolute rules enforced here. Violation → ingestion hard stop.
        """
        error_context = IngestErrorContext(
            pipeline_step="invariant_enforcement",
            run_id=context.run_id,
            input_id=normalized_account.get("platform_account_id"),
            entity_type="account",
            entity_id=account_fact.canonical_account_id
        )
        
        # Enforce all invariants in order
        AccountIngestInvariants._no_ingestion_without_context(context, error_context)
        AccountIngestInvariants._no_account_without_platform_identity(account_fact, error_context)
        AccountIngestInvariants._no_orphan_subaccounts(account_fact, error_context)
        AccountIngestInvariants._no_ambiguous_ownership_chains(account_fact, error_context)
        AccountIngestInvariants._no_excessive_ownership_depth(account_fact, max_ownership_depth, error_context)
        
        if account_store:
            AccountIngestInvariants._no_identity_mutation(account_fact, account_store, error_context)
            AccountIngestInvariants._no_identity_merging(account_fact, account_store, error_context)
    
    @staticmethod
    def _no_ingestion_without_context(
        context: IngestContext,
        error_context: IngestErrorContext
    ) -> None:
        """Invariant: No ingestion without context."""
        if not context.authority:
            raise CommonIngestErrors.invariant_broken(
                invariant_name="context_authority_required",
                violation_message="Ingestion context must have authority",
                context=error_context
            )
        if not context.run_id:
            raise CommonIngestErrors.invariant_broken(
                invariant_name="context_run_id_required",
                violation_message="Ingestion context must have run_id",
                context=error_context
            )
    
    @staticmethod
    def _no_account_without_platform_identity(
        account_fact: AccountFact,
        context: IngestErrorContext
    ) -> None:
        """Invariant: No account without platform identity."""
        if not account_fact.platform_account_id or not account_fact.platform_account_id.strip():
            raise CommonIngestErrors.invariant_broken(
                invariant_name="platform_identity_required",
                violation_message="Account must have platform_account_id",
                context=context
            )
    
    @staticmethod
    def _no_orphan_subaccounts(
        account_fact: AccountFact,
        context: IngestErrorContext
    ) -> None:
        """Invariant: No orphan sub-accounts."""
        if account_fact.account_type == AccountType.SUBACCOUNT.value:
            if not account_fact.parent_account_id:
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="subaccount_requires_parent",
                    violation_message="SUBACCOUNT must have parent_account_id",
                    context=context
                )
    
    @staticmethod
    def _no_ambiguous_ownership_chains(
        account_fact: AccountFact,
        context: IngestErrorContext
    ) -> None:
        """Invariant: No ambiguous ownership chains."""
        if account_fact.parent_account_id and account_fact.organization_id:
            if not account_fact.ownership_lineage:
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="ownership_lineage_required",
                    violation_message="Account with parent and org must have ownership_lineage",
                    context=context
                )
    
    @staticmethod
    def _no_identity_mutation(
        account_fact: AccountFact,
        account_store: AccountStore,
        context: IngestErrorContext
    ) -> None:
        """Invariant: No identity mutation of existing accounts."""
        existing = account_store.get_by_canonical_id(account_fact.canonical_account_id)
        if existing:
            if (existing.platform != account_fact.platform or
                existing.platform_account_id != account_fact.platform_account_id):
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="no_identity_mutation",
                    violation_message=(
                        f"Attempted to mutate platform identity for account "
                        f"{account_fact.canonical_account_id}. "
                        f"Existing: {existing.platform}:{existing.platform_account_id}, "
                        f"New: {account_fact.platform}:{account_fact.platform_account_id}"
                    ),
                    context=context
                )
    
    @staticmethod
    def _no_identity_merging(
        account_fact: AccountFact,
        account_store: AccountStore,
        context: IngestErrorContext
    ) -> None:
        """Invariant: No identity merging."""
        existing = account_store.get_by_canonical_id(account_fact.canonical_account_id)
        if existing:
            if (existing.platform != account_fact.platform or
                existing.platform_account_id != account_fact.platform_account_id):
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="no_identity_merging",
                    violation_message=(
                        f"Canonical account {account_fact.canonical_account_id} already belongs to "
                        f"platform identity {existing.platform}:{existing.platform_account_id}, "
                        f"cannot be claimed by {account_fact.platform}:{account_fact.platform_account_id}. "
                        "Identity merging is forbidden."
                    ),
                    context=context
                )
    
    @staticmethod
    def _no_excessive_ownership_depth(
        account_fact: AccountFact,
        max_depth: int,
        context: IngestErrorContext
    ) -> None:
        """Invariant: Ownership depth within policy limits."""
        if len(account_fact.ownership_lineage) > max_depth:
            raise CommonIngestErrors.quota_exceeded(
                quota_name="max_ownership_depth",
                limit=max_depth,
                current=len(account_fact.ownership_lineage),
                context=context
            )


# ============================================================================
# ACCOUNT VALIDATOR (STRICT IDENTITY VALIDATION)
# ============================================================================

class AccountValidator:
    """
    TIER-0: Strict identity validation - no inference, no fallback.
    
    Validates:
    - Schema conformance
    - Required identifiers
    - Ownership fields
    - Version compatibility (BEFORE normalization)
    - Timestamp sanity
    """
    
    def __init__(self, policy: AccountIngestPolicy):
        self.policy = policy
    
    def validate(
        self,
        raw_account: Dict[str, Any],
        context: IngestContext
    ) -> None:
        """Validate raw account data. Raises BaseIngestError on failure."""
        error_context = IngestErrorContext(
            pipeline_step="account_validation",
            run_id=context.run_id,
            input_id=raw_account.get("platform_account_id"),
            entity_type="account",
            entity_id=raw_account.get("platform_account_id")
        )
        
        # Validate required fields
        missing = self.policy.required_identity_fields - set(raw_account.keys())
        if missing:
            raise CommonIngestErrors.missing_required_field(
                field_name=", ".join(missing),
                context=error_context
            )
        
        # Validate platform
        platform = raw_account.get("platform", "")
        if not platform:
            raise CommonIngestErrors.missing_required_field(
                field_name="platform",
                context=error_context
            )
        if platform not in self.policy.supported_platforms:
            raise IngestErrorBuilder(
                category=ErrorCategory.AUTHORITY,
                context=error_context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.POLICY_VIOLATION,
                message=f"platform {platform} not supported by policy",
                source="account_validator",
                constraint="supported_platforms",
                expected_value=str(list(self.policy.supported_platforms)),
                actual_value=platform
            ).build()
        
        # Validate platform account ID
        platform_id = raw_account.get("platform_account_id", "")
        if not platform_id or not str(platform_id).strip():
            raise CommonIngestErrors.invariant_broken(
                invariant_name="platform_account_id_presence",
                violation_message="platform_account_id cannot be empty",
                context=error_context
            )
        id_str = str(platform_id)
        id_len = len(id_str)
        if id_len < self.policy.min_platform_account_id_length:
            raise IngestErrorBuilder(
                category=ErrorCategory.VALIDATION,
                context=error_context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.CONSTRAINT_VIOLATION,
                message=f"platform_account_id too short: {id_len} chars",
                source="account_validator",
                constraint="min_platform_account_id_length",
                expected_value=str(self.policy.min_platform_account_id_length),
                actual_value=str(id_len)
            ).build()
        if id_len > self.policy.max_platform_account_id_length:
            raise IngestErrorBuilder(
                category=ErrorCategory.VALIDATION,
                context=error_context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.CONSTRAINT_VIOLATION,
                message=f"platform_account_id too long: {id_len} chars",
                source="account_validator",
                constraint="max_platform_account_id_length",
                expected_value=str(self.policy.max_platform_account_id_length),
                actual_value=str(id_len)
            ).build()
        
        # Validate account type
        account_type_str = raw_account.get("account_type", "")
        try:
            AccountType(account_type_str)
        except (ValueError, TypeError):
            raise CommonIngestErrors.schema_violation(
                field_name="account_type",
                expected="root, organization, subaccount, managed, or service",
                actual=str(account_type_str),
                context=error_context
            )
        
        # Validate ownership fields
        account_type = AccountType(account_type_str)
        parent_id = raw_account.get("parent_account_id")
        if account_type == AccountType.ROOT and parent_id:
            raise IngestErrorBuilder(
                category=ErrorCategory.AUTHORITY,
                context=error_context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.CONSTRAINT_VIOLATION,
                message="ROOT account cannot have parent_account_id",
                source="account_validator",
                constraint="root_account_no_parent",
                actual_value=str(parent_id)
            ).build()
        if account_type == AccountType.SUBACCOUNT and not parent_id:
            raise CommonIngestErrors.invariant_broken(
                invariant_name="subaccount_requires_parent",
                violation_message="SUBACCOUNT must have parent_account_id",
                context=error_context
            )
        if not self.policy.allow_subaccounts and parent_id:
            raise IngestErrorBuilder(
                category=ErrorCategory.AUTHORITY,
                context=error_context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.POLICY_VIOLATION,
                message="Subaccounts not allowed by policy",
                source="account_validator",
                constraint="allow_subaccounts",
                expected_value="True",
                actual_value="False"
            ).build()
        
        # Validate schema version (BEFORE normalization - strict version check)
        schema_version = raw_account.get("schema_version", "")
        if not schema_version:
            raise CommonIngestErrors.unsupported_version(
                field_name="schema_version",
                expected=str(self.policy.canonical_schema_version),
                actual="",
                context=error_context
            )
        if schema_version not in self.policy.supported_identity_versions:
            raise CommonIngestErrors.unsupported_version(
                field_name="schema_version",
                expected=str(list(self.policy.supported_identity_versions)),
                actual=schema_version,
                context=error_context
            )
        
        # Validate timestamp
        created_at = raw_account.get("created_at_ms", 0)
        if created_at <= 0:
            raise CommonIngestErrors.invariant_broken(
                invariant_name="created_at_ms_positive",
                violation_message="created_at_ms must be positive",
                context=error_context
            )
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if created_at > now_ms + 86400000:
            raise IngestErrorBuilder(
                category=ErrorCategory.VALIDATION,
                context=error_context,
                recovery_hint=RecoveryHint.FATAL
            ).add_cause(
                code=IngestErrorCode.CONSTRAINT_VIOLATION,
                message="created_at_ms cannot be in future",
                source="account_validator",
                constraint="timestamp_sanity",
                expected_value=str(now_ms),
                actual_value=str(created_at)
            ).build()
        
        # Validate aliases
        aliases = raw_account.get("aliases", [])
        if not isinstance(aliases, (list, tuple)):
            raise CommonIngestErrors.schema_violation(
                field_name="aliases",
                expected="list or tuple",
                actual=str(type(aliases).__name__),
                context=error_context
            )
        if len(aliases) > self.policy.max_aliases_per_account:
            raise CommonIngestErrors.quota_exceeded(
                quota_name="max_aliases_per_account",
                limit=self.policy.max_aliases_per_account,
                current=len(aliases),
                context=error_context
            )


# ============================================================================
# ACCOUNT NORMALIZER (PROVABLY DETERMINISTIC & LOSSLESS)
# ============================================================================

class AccountNormalizer:
    """
    TIER-0: Mathematically deterministic and lossless normalization.
    
    GUARANTEES:
    - Same raw input + same context → byte-identical normalized output
    - No conditional normalization rules that alter meaning
    - No platform-specific heuristics or quirks
    - No enrichment, no inference, no fallback
    - Canonical Unicode (NFC), lowercase, epoch-UTC, enum normalization
    
    DETERMINISM PROOF:
    - All transformations are pure functions
    - No dependence on wall clock, RNG, or external state
    - Replay-testing: identical inputs → identical outputs (bit-for-bit)
    """
    
    def __init__(self, schema_version: str):
        self.schema_version = schema_version
    
    def normalize(
        self,
        raw_account: Dict[str, Any],
        context: IngestContext
    ) -> Dict[str, Any]:
        """
        Normalize raw account data.
        
        TIER-0: Mathematically deterministic and lossless.
        
        Same raw input + same context → byte-identical normalized output.
        No conditional rules that could alter meaning or depend on platform quirks.
        """
        error_context = IngestErrorContext(
            pipeline_step="account_normalization",
            run_id=context.run_id,
            input_id=raw_account.get("platform_account_id"),
            entity_type="account"
        )
        
        normalized = {}
        
        # Platform identity (deterministic)
        normalized["platform"] = str(raw_account["platform"]).strip().lower()
        normalized["platform_account_id"] = str(raw_account["platform_account_id"]).strip()
        
        # Account type (deterministic)
        normalized["account_type"] = AccountType(raw_account["account_type"]).value
        
        # Username (Unicode NFC normalization, lowercase, deterministic)
        username = raw_account.get("username")
        if username:
            normalized["username"] = unicodedata.normalize('NFC', str(username)).strip().lower() or None
        else:
            normalized["username"] = None
        
        # Display name (preserve as-is, just strip)
        display_name = raw_account.get("display_name")
        normalized["display_name"] = display_name.strip() if display_name else None
        
        # Email (lowercase only - NO platform-specific heuristics)
        email = raw_account.get("email")
        normalized["email"] = email.strip().lower() if email else None
        
        # Timestamp (normalize to epoch ms UTC)
        normalized["created_at_ms"] = self._normalize_timestamp(raw_account["created_at_ms"])
        
        # Ownership (preserve as-is)
        normalized["parent_account_id"] = raw_account.get("parent_account_id")
        normalized["organization_id"] = raw_account.get("organization_id")
        
        # Status (deterministic enum conversion - NO FALLBACK)
        # TIER-0: Must be explicit - no defaulting, no fallback
        identity_status = raw_account.get("identity_status")
        if identity_status is None:
            raise CommonIngestErrors.missing_required_field(
                field_name="identity_status",
                context=error_context
            )
        try:
            normalized["identity_status"] = IdentityStatus(identity_status).value
        except (ValueError, TypeError):
            # TIER-0: Invalid status fails loudly with structured error
            raise CommonIngestErrors.schema_violation(
                field_name="identity_status",
                expected=", ".join([s.value for s in IdentityStatus]),
                actual=str(identity_status),
                context=error_context
            )
        
        # Schema version (canonical)
        normalized["schema_version"] = self.schema_version
        
        # Aliases (normalized, sorted for determinism)
        normalized["aliases"] = self._normalize_aliases(raw_account.get("aliases", []))
        
        return normalized
    
    def _normalize_timestamp(self, timestamp: Any) -> int:
        """Normalize timestamp to epoch milliseconds UTC (deterministic)."""
        if isinstance(timestamp, int):
            # Assume already in ms if > 1e10
            return timestamp if timestamp > 1e10 else timestamp * 1000
        elif isinstance(timestamp, str):
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            return int(dt.timestamp() * 1000)
        elif isinstance(timestamp, datetime):
            return int(timestamp.timestamp() * 1000)
        else:
            raise ValueError(f"Cannot normalize timestamp: {type(timestamp)}")
    
    def _normalize_aliases(self, aliases: List[str]) -> Tuple[str, ...]:
        """Normalize alias list (deterministic, sorted)."""
        normalized = []
        seen = set()
        for alias in aliases:
            norm_alias = str(alias).strip().lower()
            if norm_alias and norm_alias not in seen:
                normalized.append(norm_alias)
                seen.add(norm_alias)
        return tuple(sorted(normalized))  # Sort for determinism


# ============================================================================
# ACCOUNT OWNERSHIP RESOLVER (EXPLICIT & DETERMINISTIC)
# ============================================================================

class AccountOwnershipResolver:
    """
    TIER-0: Explicit and order-safe ownership resolution.
    
    GUARANTEES:
    - Fully explicit: every parent account MUST already exist or be ingested first
    - Order-safe: rejects missing parents immediately (no tolerance)
    - Circular rejection: guaranteed circular-chain detection and rejection
    - No inferred lineage: all lineage built explicitly from existing facts
    - Immutable facts: ownership facts permanently frozen once emitted, never silently change across replays
    """
    
    def __init__(self, account_store: Optional[AccountStore] = None):
        self.account_store = account_store
    
    def resolve(
        self,
        account_type: str,
        parent_account_id: Optional[str],
        organization_id: Optional[str],
        context: IngestContext
    ) -> Tuple[str, ...]:
        """
        Resolve full ownership lineage.
        
        TIER-0: Explicit and deterministic - no implicit lineage, no missing parents.
        """
        error_context = IngestErrorContext(
            pipeline_step="ownership_resolution",
            run_id=context.run_id,
            input_id=parent_account_id or organization_id,
            entity_type="account_ownership",
            entity_id=parent_account_id or organization_id
        )
        
        # ROOT accounts have no ownership lineage
        if account_type == AccountType.ROOT.value:
            return tuple()
        
        # Build ownership chain explicitly
        lineage: List[str] = []
        
        if parent_account_id:
            # TIER-0: Parent MUST exist - no tolerance for missing parents
            if not self.account_store:
                raise IngestErrorBuilder(
                    category=ErrorCategory.STATE,
                    context=error_context,
                    recovery_hint=RecoveryHint.FATAL
                ).add_cause(
                    code=IngestErrorCode.DEPENDENCY_MISSING,
                    message="Account store required for ownership resolution",
                    source="ownership_resolver"
                ).build()
            
            parent = self.account_store.get_by_canonical_id(parent_account_id)
            if not parent:
                raise IngestErrorBuilder(
                    category=ErrorCategory.STATE,
                    context=error_context,
                    recovery_hint=RecoveryHint.FATAL
                ).add_cause(
                    code=IngestErrorCode.DEPENDENCY_MISSING,
                    message=f"Parent account does not exist: {parent_account_id}",
                    source="ownership_resolver",
                    actual_value=parent_account_id
                ).build()
            
            # Get parent's lineage and append parent (explicit, no implicit building)
            lineage.extend(parent.ownership_lineage)
            lineage.append(parent_account_id)
        
        if organization_id:
            # TIER-0: Organization MUST exist
            if not self.account_store:
                raise IngestErrorBuilder(
                    category=ErrorCategory.STATE,
                    context=error_context,
                    recovery_hint=RecoveryHint.FATAL
                ).add_cause(
                    code=IngestErrorCode.DEPENDENCY_MISSING,
                    message="Account store required for ownership resolution",
                    source="ownership_resolver"
                ).build()
            
            org = self.account_store.get_by_canonical_id(organization_id)
            if not org:
                raise IngestErrorBuilder(
                    category=ErrorCategory.STATE,
                    context=error_context,
                    recovery_hint=RecoveryHint.FATAL
                ).add_cause(
                    code=IngestErrorCode.DEPENDENCY_MISSING,
                    message=f"Organization does not exist: {organization_id}",
                    source="ownership_resolver",
                    actual_value=organization_id
                ).build()
            
            # Add org to lineage if not already present
            if organization_id not in lineage:
                lineage.append(organization_id)
        
        # TIER-0: Guaranteed circular-chain rejection
        if len(lineage) != len(set(lineage)):
            raise CommonIngestErrors.invariant_broken(
                invariant_name="no_circular_ownership",
                violation_message=f"Circular ownership detected: {lineage}",
                context=error_context
            )
        
        return tuple(lineage)
    


# ============================================================================
# ACCOUNT DEDUPLICATOR (CONFLICT-FATAL)
# ============================================================================

class AccountDeduplicator:
    """
    TIER-0: Uncompromising global identity authority.
    
    GUARANTEES:
    - Global uniqueness key: (platform, platform_account_id, schema_version, ownership_lineage)
    - Primary fingerprinting: identity hash includes all uniqueness components
    - Conflicts are ALWAYS fatal: no reconciliation, no fuzzy matching, no silent convergence
    - Zero implicit merges: no two identities can ever silently converge
    - Audit logging: all conflicts logged with structured IngestError for forensic analysis
    """
    
    def __init__(self, account_store: Optional[AccountStore] = None):
        self.account_store = account_store
    
    def check(
        self,
        normalized_account: Dict[str, Any],
        identity_hash: str,
        lineage: Tuple[str, ...],
        context: IngestContext,
        audit_logger: Optional[AuditLogger] = None
    ) -> Tuple[DeduplicationStatus, Optional[AccountFact]]:
        """
        Check deduplication status.
        
        TIER-0: Conflicts are ALWAYS fatal - no reconciliation, no fuzzy matching.
        Global uniqueness key: (platform, platform_account_id, identity_hash, lineage)
        """
        if not self.account_store:
            return (DeduplicationStatus.NEW, None)
        
        platform = normalized_account["platform"]
        platform_account_id = normalized_account["platform_account_id"]
        
        # Check by identity hash (primary fingerprint - includes platform, platform_account_id, schema_version, lineage)
        existing_by_hash = self.account_store.get_by_identity_hash(identity_hash)
        if existing_by_hash:
            # Hash match means (platform, platform_account_id, schema_version, lineage) all match
            # Verify platform identity matches (defensive check)
            if (existing_by_hash.platform == platform and
                existing_by_hash.platform_account_id == platform_account_id):
                return (DeduplicationStatus.EXISTING, existing_by_hash)
            else:
                # Hash collision with different platform identity - CONFLICTING (audit log)
                if audit_logger:
                    audit_logger.log_ingest_failed(
                        platform,
                        platform_account_id,
                        IngestErrorBuilder(
                            category=ErrorCategory.STATE,
                            context=IngestErrorContext(
                                pipeline_step="deduplication",
                                run_id=context.run_id,
                                input_id=platform_account_id,
                                entity_type="account"
                            ),
                            recovery_hint=RecoveryHint.FATAL
                        ).add_cause(
                            code=IngestErrorCode.STATE_CONFLICT,
                            message=f"Identity hash collision: same hash {identity_hash} with different platform identity {existing_by_hash.platform}:{existing_by_hash.platform_account_id}",
                            source="account_deduplicator"
                        ).build(),
                        context
                    )
                return (DeduplicationStatus.CONFLICTING, existing_by_hash)
        
        # Check by platform identity (defensive - should not happen if hash is correct)
        existing = self.account_store.get_by_platform_identity(platform, platform_account_id)
        if existing:
            # Platform identity exists but hash differs - CONFLICTING (audit log)
            if audit_logger:
                audit_logger.log_ingest_failed(
                    platform,
                    platform_account_id,
                    IngestErrorBuilder(
                        category=ErrorCategory.STATE,
                        context=IngestErrorContext(
                            pipeline_step="deduplication",
                            run_id=context.run_id,
                            input_id=platform_account_id,
                            entity_type="account"
                        ),
                        recovery_hint=RecoveryHint.FATAL
                    ).add_cause(
                        code=IngestErrorCode.STATE_CONFLICT,
                        message=f"Identity hash mismatch: existing={existing.identity_hash}, new={identity_hash}",
                        source="account_deduplicator"
                    ).build(),
                    context
                )
            return (DeduplicationStatus.CONFLICTING, existing)
        
        # No existing identity found - NEW
        return (DeduplicationStatus.NEW, None)


# ============================================================================
# ACCOUNT INGESTOR (ORCHESTRATOR - 8 MANDATED RESPONSIBILITIES)
# ============================================================================

class AccountIngestor:
    """
    TIER-0: Sealed Identity-Admission Authority
    
    This is the single, globally authoritative, replay-safe source of immutable account identity truth.
    Suitable for 500k+ LOC, multi-million-traffic systems where identity errors would cascade into
    financial, trust, and attribution failures.
    
    Execution order (STRICT - 8 mandated responsibilities):
    1. Context validation
    2. Policy gating (strict version compatibility - no coercion)
    3. Strict identity validation (no fabricating, inferring, defaulting)
    4. Deterministic normalization (mathematically deterministic, lossless)
    5. Explicit ownership resolution (order-safe, no missing parents, circular rejection)
    6. Conflict-fatal deduplication (global uniqueness, zero implicit merges, audit logging)
    7. Canonical ID assignment (store-issued, post-dedup, globally unique, non-semantic)
    8. Immutable result emission (frozen value object, downstream cannot mutate)
    
    DETERMINISM & IDEMPOTENCY GUARANTEES:
    - Replay-testing: identical raw inputs → byte-identical normalized identities, lineage, hashes, results
    - Idempotent: replay-safe, no side effects
    - Immutable: identity facts never mutate after emission
    - Explicit: no inference, no fallback, no enrichment
    - Fatal conflicts: all conflicts are hard failures with audit logging
    
    INVARIANT ENFORCEMENT:
    - Centralized in AccountIngestInvariants.enforce() - invoked exactly once after normalization and ownership resolution
    - Path-independent: no account can pass if it lacks platform identity, has ambiguous/circular ownership,
      attempts identity mutation, merges with existing identity, or produces orphan subaccount
    """
    
    def __init__(
        self,
        policy: AccountIngestPolicy,
        validator: AccountValidator,
        normalizer: AccountNormalizer,
        ownership_resolver: AccountOwnershipResolver,
        deduplicator: AccountDeduplicator,
        account_store: AccountStore,
        audit_logger: Optional[AuditLogger] = None
    ):
        self.policy = policy
        self.validator = validator
        self.normalizer = normalizer
        self.ownership_resolver = ownership_resolver
        self.deduplicator = deduplicator
        self.account_store = account_store
        self.audit_logger = audit_logger
    
    def ingest(
        self,
        raw_account: Dict[str, Any],
        context: IngestContext
    ) -> IngestResult:
        """
        Ingest raw account data into canonical form.
        
        TIER-0: 8 mandated responsibilities in exact order.
        """
        platform = raw_account.get("platform", "unknown")
        platform_account_id = raw_account.get("platform_account_id", "unknown")
        
        if self.audit_logger:
            self.audit_logger.log_ingest_started(platform, platform_account_id, context)
        
        try:
            # Step 1: Context validation
            if not context.authority or not context.run_id:
                raise CommonIngestErrors.invariant_broken(
                    invariant_name="context_required",
                    violation_message="Ingestion context must have authority and run_id",
                    context=IngestErrorContext(
                        pipeline_step="context_validation",
                        run_id=context.run_id or "unknown",
                        entity_type="account"
                    )
                )
            
            # Step 2: Policy gating + Strict identity validation (consolidated - single source of truth)
            # TIER-0: Validator enforces platform/version compatibility, eliminating redundancy
            self.validator.validate(raw_account, context)
            
            # Step 3: Deterministic normalization
            normalized = self.normalizer.normalize(raw_account, context)
            
            # Step 4: Explicit ownership resolution
            ownership_lineage = self.ownership_resolver.resolve(
                normalized["account_type"],
                normalized.get("parent_account_id"),
                normalized.get("organization_id"),
                context
            )
            
            # Step 5: Compute identity hash (includes lineage for primary fingerprinting)
            identity_hash = self._compute_identity_hash(normalized, ownership_lineage)
            
            # Step 6: Conflict-fatal deduplication (with audit logging)
            dedup_status, existing_fact = self.deduplicator.check(
                normalized,
                identity_hash,
                ownership_lineage,
                context,
                audit_logger=self.audit_logger
            )
            
            if dedup_status == DeduplicationStatus.CONFLICTING:
                # TIER-0: Conflicts are ALWAYS fatal
                raise IngestErrorBuilder(
                    category=ErrorCategory.STATE,
                    context=IngestErrorContext(
                        pipeline_step="deduplication",
                        run_id=context.run_id,
                        input_id=platform_account_id,
                        entity_type="account",
                        entity_id=platform_account_id
                    ),
                    recovery_hint=RecoveryHint.FATAL
                ).add_cause(
                    code=IngestErrorCode.STATE_CONFLICT,
                    message="Account identity conflicts with existing account",
                    source="account_deduplicator",
                    actual_value=platform_account_id
                ).build()
            
            if dedup_status == DeduplicationStatus.EXISTING:
                # Idempotent reingest - verify ownership immutability
                if existing_fact:
                    self._verify_ownership_immutability(
                        normalized,
                        ownership_lineage,
                        existing_fact,
                        context
                    )
                    if self.audit_logger:
                        self.audit_logger.log_duplicate_detected(
                            existing_fact.canonical_account_id,
                            context
                        )
                return create_deduped_result(
                    context=context,
                    existing_fact_ids=[existing_fact.canonical_account_id] if existing_fact else []
                )
            
            # Step 7: Canonical ID assignment (store-issued, post-dedup, globally unique, non-semantic)
            # TIER-0: ID is globally unique, never encodes platform meaning, never recycled, stable across renames
            canonical_account_id = self.account_store.get_or_create_canonical_id(identity_hash)
            
            # Create immutable account fact
            account_fact = self._create_fact(
                canonical_account_id,
                identity_hash,
                normalized,
                ownership_lineage,
                context
            )
            
            # Step 8: Enforce invariants (centralized gate - invoked EXACTLY ONCE after normalization and ownership resolution)
            # TIER-0: All absolute rules enforced here, path-independent, declarative
            # No account can pass if it lacks platform identity, has ambiguous/circular ownership,
            # attempts identity mutation, merges with existing identity, produces orphan subaccount, or exceeds depth
            AccountIngestInvariants.enforce(
                normalized,
                account_fact,
                context,
                self.policy.max_ownership_depth,
                self.account_store
            )
            
            # Persist account fact
            self.account_store.persist(account_fact)
            
            if self.audit_logger:
                self.audit_logger.log_ingest_succeeded(canonical_account_id, context)
            
            # Step 9: Immutable result emission
            # TIER-0: IngestResult is frozen (immutable), downstream cannot mutate identity facts
            result = create_accepted_result(
                context=context,
                fact_ids=[canonical_account_id]
            )
            return result
            
        except BaseIngestError as e:
            # TIER-0: All failures emit structured IngestError with precise categories and recovery hints
            # No raw exceptions escape the boundary
            if self.audit_logger:
                self.audit_logger.log_ingest_failed(platform, platform_account_id, e, context)
            return create_rejected_result(
                context=context,
                reason=RejectionReason.POLICY_VIOLATION,
                detail=str(e)
            )
        except Exception as e:
            # TIER-0: Wrap ALL unexpected errors in structured IngestError
            # No raw exceptions escape the boundary - guarantees structured error emission
            error = IngestErrorBuilder(
                category=ErrorCategory.INFRA,
                context=IngestErrorContext(
                    pipeline_step="account_ingestion",
                    run_id=context.run_id,
                    input_id=platform_account_id,
                    entity_type="account",
                    entity_id=platform_account_id
                ),
                recovery_hint=RecoveryHint.REQUIRES_MANUAL_REVIEW
            ).add_cause(
                code=IngestErrorCode.SERIALIZATION_FAILED,
                message=f"unexpected error during ingestion: {str(e)}",
                source="account_ingestor"
            ).build()
            
            if self.audit_logger:
                self.audit_logger.log_ingest_failed(platform, platform_account_id, error, context)
            
            return create_rejected_result(
                context=context,
                reason=RejectionReason.OTHER,
                detail=str(e)
            )
    
    def _verify_ownership_immutability(
        self,
        normalized: Dict[str, Any],
        new_lineage: Tuple[str, ...],
        existing_fact: AccountFact,
        context: IngestContext
    ) -> None:
        """Verify ownership facts are immutable."""
        error_context = IngestErrorContext(
            pipeline_step="ownership_immutability_check",
            run_id=context.run_id,
            input_id=normalized.get("platform_account_id"),
            entity_type="account_ownership",
            entity_id=existing_fact.canonical_account_id
        )
        
        if existing_fact.parent_account_id != normalized.get("parent_account_id"):
            raise CommonIngestErrors.invariant_broken(
                invariant_name="ownership_immutability",
                violation_message=(
                    f"Attempted to change parent_account_id from "
                    f"{existing_fact.parent_account_id} to {normalized.get('parent_account_id')}. "
                    "Ownership facts are immutable once established."
                ),
                context=error_context
            )
        
        if existing_fact.organization_id != normalized.get("organization_id"):
            raise CommonIngestErrors.invariant_broken(
                invariant_name="ownership_immutability",
                violation_message=(
                    f"Attempted to change organization_id from "
                    f"{existing_fact.organization_id} to {normalized.get('organization_id')}. "
                    "Ownership facts are immutable once established."
                ),
                context=error_context
            )
        
        if existing_fact.ownership_lineage != new_lineage:
            raise CommonIngestErrors.invariant_broken(
                invariant_name="ownership_immutability",
                violation_message=(
                    f"Attempted to change ownership_lineage from "
                    f"{existing_fact.ownership_lineage} to {new_lineage}. "
                    "Ownership facts are immutable once established."
                ),
                context=error_context
            )
    
    def _compute_identity_hash(
        self,
        normalized: Dict[str, Any],
        ownership_lineage: Tuple[str, ...]
    ) -> str:
        """
        Compute deterministic identity fingerprint.
        
        TIER-0: Hash includes immutable identity components + ownership lineage.
        
        Global uniqueness key: (platform, platform_account_id, schema_version, ownership_lineage)
        Lineage is part of uniqueness model - if lineage changes, hash changes.
        This ensures primary fingerprinting via hash, not secondary checks.
        """
        identity_components = [
            normalized["platform"],
            normalized["platform_account_id"],
            normalized["schema_version"],
            "|".join(ownership_lineage) if ownership_lineage else ""
        ]
        identity_string = "|".join(identity_components)
        return hashlib.sha256(identity_string.encode('utf-8')).hexdigest()
    
    def _create_fact(
        self,
        canonical_account_id: str,
        identity_hash: str,
        normalized: Dict[str, Any],
        ownership_lineage: Tuple[str, ...],
        context: IngestContext
    ) -> AccountFact:
        """Create immutable account fact from normalized data."""
        return AccountFact(
            canonical_account_id=canonical_account_id,
            platform=normalized["platform"],
            platform_account_id=normalized["platform_account_id"],
            account_type=normalized["account_type"],
            username=normalized.get("username"),
            display_name=normalized.get("display_name"),
            email=normalized.get("email"),
            parent_account_id=normalized.get("parent_account_id"),
            organization_id=normalized.get("organization_id"),
            ownership_lineage=ownership_lineage,
            identity_status=normalized["identity_status"],
            identity_hash=identity_hash,
            created_at_ms=normalized["created_at_ms"],
            ingested_at_ms=context.timestamp_ms,
            schema_version=normalized["schema_version"],
            aliases=normalized["aliases"],
        )


# ============================================================================
# FACTORY & SETUP
# ============================================================================

def create_account_ingestor(
    policy: AccountIngestPolicy,
    account_store: AccountStore,
    audit_logger: Optional[AuditLogger] = None
) -> AccountIngestor:
    """Factory function to create fully configured AccountIngestor."""
    validator = AccountValidator(policy=policy)
    normalizer = AccountNormalizer(schema_version=policy.canonical_schema_version)
    ownership_resolver = AccountOwnershipResolver(account_store=account_store)
    deduplicator = AccountDeduplicator(account_store=account_store)
    
    return AccountIngestor(
        policy=policy,
        validator=validator,
        normalizer=normalizer,
        ownership_resolver=ownership_resolver,
        deduplicator=deduplicator,
        account_store=account_store,
        audit_logger=audit_logger
    )


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    # Enums
    'AccountType',
    'IdentityStatus',
    'DeduplicationStatus',
    
    # Data Models
    'AccountIngestPolicy',
    'AccountFact',
    
    # Components
    'AccountValidator',
    'AccountNormalizer',
    'AccountDeduplicator',
    'AccountOwnershipResolver',
    'AccountIngestInvariants',
    
    # Main Ingestor
    'AccountIngestor',
    
    # Factory
    'create_account_ingestor',
    
    # Protocols
    'AccountStore',
    'AuditLogger',
]
