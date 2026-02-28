"""
/data/validation/audit_log_model.py

Validation Audit Serialization Model

---

1️⃣ Non-Negotiable Responsibility

This file defines:

- The canonical audit record format for validation execution
- Deterministic, immutable event structure
- Stable serialization model
- Hash-verifiable audit entries
- Replay-verifiable execution fingerprints

It must guarantee:

- Byte-stable output
- Immutable record objects
- No dynamic timestamps (unless explicitly passed)
- No implicit environment leakage
- No mutation after creation

This is compliance-layer material.

---

2️⃣ Architectural Boundary

This file does NOT:

- Execute validation
- Contain validation rules
- Interpret policy
- Perform I/O

It defines structured audit events — pure data.

---

3️⃣ Core Audit Object Model

There are typically three layers:

1. Execution metadata
2. Validation bundle reference
3. Policy interpretation result

---

This is not about logging. This is about audit-grade serialization contracts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional, Mapping, Any, Tuple, List, Dict

from .error_model import ValidationErrorBundle
from .policy_profiles import ValidationProfile


__all__ = [
    "ValidationAuditEvent",
    "SignedValidationAuditEvent",
    "AUDIT_SCHEMA_VERSION",
    "build_audit_event",
    "audit_event_to_dict",
    "audit_event_to_json",
    "compute_audit_hash",
    "verify_audit_integrity",
    "verify_replay_equivalence",
    "compute_policy_decision_hash",
    "compute_full_audit_hash",
]


# ============================================================================
# Audit Schema Versioning
# ============================================================================

AUDIT_SCHEMA_VERSION: int = 1
"""
Audit schema version number.

When the structure of ValidationAuditEvent changes, this version must increment.
This enables:
- Replay engines to use correct schema version
- Migration tools to handle version transitions
- Long-term compatibility tracking
"""


# ============================================================================
# Validation Audit Event (Core Model)
# ============================================================================

@dataclass(frozen=True)
class ValidationAuditEvent:
    """
    Immutable audit record for validation execution.
    
    This is the canonical, hash-verifiable record of a validation execution.
    It binds together:
    - Input object identity
    - Validation bundle (via hash reference)
    - Policy decision
    - Schema and policy versions
    - Execution context
    
    Attributes:
        object_id: Identifies validated entity (content_id, record_id, etc.)
        schema_version: Schema version used for validation (from bundle)
        profile: ValidationProfile that was applied
        bundle_hash: SHA-256 hash of ValidationErrorBundle (from bundle.deterministic_hash)
        decision_reject: Final policy decision (True = reject, False = accept)
        policy_version: Policy version that produced this decision
        execution_id: Externally provided deterministic execution identifier
        input_hash: SHA-256 hash of input object snapshot (for replay verification)
        rule_execution_fingerprint: Optional hash of ordered rule identifiers executed
        timestamp_epoch_ms: Optional timestamp (must be externally supplied, never generated)
        
    Design:
        - frozen=True ensures immutability
        - All fields are deterministic (no runtime generation)
        - Hash-based references prevent data bloat
        - Execution ID must be externally provided (never generated internally)
        - Timestamp is optional and excluded from deterministic hash
    """
    
    object_id: str
    schema_version: int
    profile: ValidationProfile
    bundle_hash: str
    decision_reject: bool
    policy_version: int
    execution_id: str
    input_hash: str
    rule_execution_fingerprint: Optional[str] = None
    timestamp_epoch_ms: Optional[int] = None
    
    def __post_init__(self) -> None:
        """
        Validate audit event structure (Tier-0).
        
        Enforces:
        - Non-empty required fields
        - Valid hash format (64 hex characters for SHA-256)
        - Positive schema/policy versions
        - Valid execution ID format
        """
        if not self.object_id:
            raise ValueError("object_id cannot be empty")
        if not self.execution_id:
            raise ValueError("execution_id cannot be empty")
        if self.schema_version < 1:
            raise ValueError(f"schema_version must be >= 1, got {self.schema_version}")
        if self.policy_version < 1:
            raise ValueError(f"policy_version must be >= 1, got {self.policy_version}")
        
        # Validate hash format (SHA-256 = 64 hex characters)
        if len(self.bundle_hash) != 64:
            raise ValueError(
                f"bundle_hash must be 64 hex characters (SHA-256), got {len(self.bundle_hash)}"
            )
        if not all(c in '0123456789abcdef' for c in self.bundle_hash.lower()):
            raise ValueError("bundle_hash must be hexadecimal")
        
        if len(self.input_hash) != 64:
            raise ValueError(
                f"input_hash must be 64 hex characters (SHA-256), got {len(self.input_hash)}"
            )
        if not all(c in '0123456789abcdef' for c in self.input_hash.lower()):
            raise ValueError("input_hash must be hexadecimal")
        
        # Validate optional fingerprint if provided
        if self.rule_execution_fingerprint is not None:
            if len(self.rule_execution_fingerprint) != 64:
                raise ValueError(
                    f"rule_execution_fingerprint must be 64 hex characters (SHA-256), "
                    f"got {len(self.rule_execution_fingerprint)}"
                )
            if not all(c in '0123456789abcdef' for c in self.rule_execution_fingerprint.lower()):
                raise ValueError("rule_execution_fingerprint must be hexadecimal")
        
        # Validate timestamp if provided (must be positive)
        if self.timestamp_epoch_ms is not None and self.timestamp_epoch_ms < 0:
            raise ValueError(f"timestamp_epoch_ms must be >= 0, got {self.timestamp_epoch_ms}")
    
    def __repr__(self) -> str:
        return (
            f"ValidationAuditEvent("
            f"object_id={self.object_id!r}, "
            f"schema_version={self.schema_version}, "
            f"profile={self.profile.value}, "
            f"decision_reject={self.decision_reject}, "
            f"policy_version={self.policy_version}, "
            f"execution_id={self.execution_id[:16]}..., "
            f"bundle_hash={self.bundle_hash[:16]}..., "
            f"input_hash={self.input_hash[:16]}...)"
        )


# ============================================================================
# Signed Audit Event (Advanced)
# ============================================================================

@dataclass(frozen=True)
class SignedValidationAuditEvent:
    """
    Audit event with cryptographic integrity hash.
    
    This wraps ValidationAuditEvent with a computed audit hash that can be
    verified independently. This enables:
    - Tamper detection
    - Integrity verification without full event reconstruction
    - Cryptographic compliance signing (future-proofing)
    
    Attributes:
        event: The ValidationAuditEvent being signed
        audit_hash: SHA-256 hash of canonical JSON representation
        signature: Optional cryptographic signature (future-proofing)
        signing_key_id: Optional key identifier for signature (future-proofing)
        
    Design:
        - audit_hash is computed from canonical JSON (deterministic)
        - signature and signing_key_id are optional for future cryptographic signing
        - Hash excludes timestamp for deterministic replay verification
    """
    
    event: ValidationAuditEvent
    audit_hash: str
    signature: Optional[str] = None
    signing_key_id: Optional[str] = None
    
    def __post_init__(self) -> None:
        """Validate signed event structure."""
        if len(self.audit_hash) != 64:
            raise ValueError(
                f"audit_hash must be 64 hex characters (SHA-256), got {len(self.audit_hash)}"
            )
        if not all(c in '0123456789abcdef' for c in self.audit_hash.lower()):
            raise ValueError("audit_hash must be hexadecimal")
        
        # Verify audit hash matches computed value
        computed_hash = compute_audit_hash(self.event)
        if self.audit_hash != computed_hash:
            raise ValueError(
                f"audit_hash mismatch: provided {self.audit_hash[:16]}..., "
                f"computed {computed_hash[:16]}..."
            )


# ============================================================================
# Deterministic Event Construction
# ============================================================================

def build_audit_event(
    object_id: str,
    bundle: ValidationErrorBundle,
    profile: ValidationProfile,
    decision_reject: bool,
    policy_version: int,
    execution_id: str,
    input_hash: str,
    rule_execution_fingerprint: Optional[str] = None,
    timestamp_epoch_ms: Optional[int] = None,
) -> ValidationAuditEvent:
    """
    Build ValidationAuditEvent from validation execution results.
    
    This is the canonical way to construct an audit event. All inputs must
    already be deterministic. No randomness. No timestamps generated internally.
    
    Args:
        object_id: Identifies validated entity
        bundle: ValidationErrorBundle (hash will be extracted)
        profile: ValidationProfile that was applied
        decision_reject: Final policy decision
        policy_version: Policy version used
        execution_id: Externally provided deterministic execution identifier
        input_hash: SHA-256 hash of input object snapshot
        rule_execution_fingerprint: Optional hash of ordered rule identifiers
        timestamp_epoch_ms: Optional timestamp (must be externally supplied)
        
    Returns:
        ValidationAuditEvent with all fields populated
        
    Note:
        - bundle.deterministic_hash is used (must be valid)
        - bundle.schema_version is used
        - execution_id must be externally provided (never generated here)
        - timestamp_epoch_ms must be externally provided (never generated here)
    """
    return ValidationAuditEvent(
        object_id=object_id,
        schema_version=bundle.schema_version,
        profile=profile,
        bundle_hash=bundle.deterministic_hash,
        decision_reject=decision_reject,
        policy_version=policy_version,
        execution_id=execution_id,
        input_hash=input_hash,
        rule_execution_fingerprint=rule_execution_fingerprint,
        timestamp_epoch_ms=timestamp_epoch_ms,
    )


# ============================================================================
# Deterministic Serialization (Canonical Ordering)
# ============================================================================

def audit_event_to_dict(event: ValidationAuditEvent) -> dict[str, Any]:
    """
    Convert audit event to canonical dictionary representation.
    
    This produces a machine-parseable dictionary with stable key ordering.
    All fields are explicit and deterministic. Keys are sorted for stability.
    
    Args:
        event: ValidationAuditEvent to convert
        
    Returns:
        Dictionary with sorted keys and deterministic structure
        
    Note:
        - Keys are in canonical order (alphabetical)
        - Profile is serialized as enum value (string)
        - Optional fields are included only if present
        - Timestamp is included but excluded from deterministic hash
    """
    result: dict[str, Any] = {
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "bundle_hash": event.bundle_hash,
        "decision_reject": event.decision_reject,
        "execution_id": event.execution_id,
        "input_hash": event.input_hash,
        "object_id": event.object_id,
        "policy_version": event.policy_version,
        "profile": event.profile.value,
        "schema_version": event.schema_version,
    }
    
    # Add optional fields if present
    if event.rule_execution_fingerprint is not None:
        result["rule_execution_fingerprint"] = event.rule_execution_fingerprint
    
    if event.timestamp_epoch_ms is not None:
        result["timestamp_epoch_ms"] = event.timestamp_epoch_ms
    
    return result


def audit_event_to_json(event: ValidationAuditEvent) -> str:
    """
    Convert audit event to canonical JSON string.
    
    This produces byte-identical JSON for identical events. Uses:
    - Sorted keys (alphabetical order)
    - Compact separators (no extra whitespace)
    - ASCII-only encoding (ensure_ascii=True)
    
    Args:
        event: ValidationAuditEvent to serialize
        
    Returns:
        Canonical JSON string (byte-identical for identical events)
        
    Note:
        - Stable key ordering via sort_keys=True
        - Stable formatting via separators=(",", ":")
        - Timestamp is included in JSON but excluded from deterministic hash
    """
    return json.dumps(
        audit_event_to_dict(event),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


# ============================================================================
# Audit Integrity Hash (Deterministic)
# ============================================================================

def compute_audit_hash(event: ValidationAuditEvent) -> str:
    """
    Compute deterministic SHA-256 hash of audit event.
    
    The hash is computed from canonical JSON representation, but excludes
    timestamp_epoch_ms to enable deterministic replay verification.
    
    This enables:
    - Tamper detection
    - Integrity verification
    - Replay equivalence proof
    
    Args:
        event: ValidationAuditEvent to hash
        
    Returns:
        SHA-256 hex digest (64 characters)
        
    Note:
        - Hash excludes timestamp for deterministic replay
        - Hash includes all other fields (bundle_hash, decision, etc.)
        - Same event (without timestamp) produces same hash
    """
    # Create hash-safe representation (exclude timestamp)
    hash_dict = {
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "bundle_hash": event.bundle_hash,
        "decision_reject": event.decision_reject,
        "execution_id": event.execution_id,
        "input_hash": event.input_hash,
        "object_id": event.object_id,
        "policy_version": event.policy_version,
        "profile": event.profile.value,
        "schema_version": event.schema_version,
    }
    
    # Add optional fingerprint if present
    if event.rule_execution_fingerprint is not None:
        hash_dict["rule_execution_fingerprint"] = event.rule_execution_fingerprint
    
    # Timestamp is explicitly excluded from hash for deterministic replay
    
    # Canonical JSON with sorted keys
    canonical_json = json.dumps(
        hash_dict,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def verify_audit_integrity(signed_event: SignedValidationAuditEvent) -> bool:
    """
    Verify audit event integrity by recomputing hash.
    
    Args:
        signed_event: SignedValidationAuditEvent to verify
        
    Returns:
        True if hash matches computed value, False otherwise
    """
    computed_hash = compute_audit_hash(signed_event.event)
    return signed_event.audit_hash == computed_hash


# ============================================================================
# Replay Verification Model
# ============================================================================

def verify_replay_equivalence(
    original_event: ValidationAuditEvent,
    replayed_bundle_hash: str,
    replayed_decision_reject: bool,
    replayed_audit_hash: Optional[str] = None,
) -> tuple[bool, list[str]]:
    """
    Verify that replay produces equivalent audit results.
    
    To verify replay:
    1. Recompute input_hash (must match)
    2. Re-run validation (produces new bundle_hash)
    3. Apply same profile + policy version (produces new decision)
    4. Recompute audit JSON (produces new audit_hash)
    5. Compare bundle_hash, decision, and audit_hash
    
    Args:
        original_event: Original ValidationAuditEvent from first execution
        replayed_bundle_hash: Bundle hash from replay execution
        replayed_decision_reject: Decision from replay execution
        replayed_audit_hash: Optional audit hash from replay (if available)
        
    Returns:
        Tuple of (is_equivalent, list_of_mismatch_descriptions)
        - is_equivalent: True if all checks pass
        - list_of_mismatch_descriptions: List of mismatch details if any
        
    Note:
        - bundle_hash must match (same validation output)
        - decision_reject must match (same policy interpretation)
        - audit_hash must match if provided (same audit record)
        - input_hash is assumed to match (caller responsibility)
    """
    mismatches: list[str] = []
    
    # Check bundle hash (validation output must be identical)
    if original_event.bundle_hash != replayed_bundle_hash:
        mismatches.append(
            f"Bundle hash mismatch: original={original_event.bundle_hash[:16]}..., "
            f"replayed={replayed_bundle_hash[:16]}..."
        )
    
    # Check decision (policy interpretation must be identical)
    if original_event.decision_reject != replayed_decision_reject:
        mismatches.append(
            f"Decision mismatch: original={original_event.decision_reject}, "
            f"replayed={replayed_decision_reject}"
        )
    
    # Check audit hash if provided (full audit record must be identical)
    if replayed_audit_hash is not None:
        original_audit_hash = compute_audit_hash(original_event)
        if original_audit_hash != replayed_audit_hash:
            mismatches.append(
                f"Audit hash mismatch: original={original_audit_hash[:16]}..., "
                f"replayed={replayed_audit_hash[:16]}..."
            )
    
    return (len(mismatches) == 0, mismatches)


# ============================================================================
# Advanced: Dual-Hash Model (Research-Grade)
# ============================================================================

def compute_policy_decision_hash(
    profile: ValidationProfile,
    policy_version: int,
    bundle_hash: str,
    decision_reject: bool,
) -> str:
    """
    Compute deterministic hash of policy decision.
    
    This provides a separate hash for the decision itself, independent of
    the full audit record. This enables:
    - Decision-only verification
    - Policy drift detection
    - Decision comparison across profiles
    
    Args:
        profile: ValidationProfile used
        policy_version: Policy version used
        bundle_hash: Bundle hash that was evaluated
        decision_reject: Final decision
        
    Returns:
        SHA-256 hex digest of decision (64 characters)
    """
    decision_repr = (
        f"profile={profile.value},"
        f"policy_version={policy_version},"
        f"bundle_hash={bundle_hash},"
        f"decision_reject={decision_reject}"
    )
    return hashlib.sha256(decision_repr.encode("utf-8")).hexdigest()


def compute_full_audit_hash(event: ValidationAuditEvent) -> str:
    """
    Compute full audit hash including all fields (including timestamp if present).
    
    This is different from compute_audit_hash() which excludes timestamp.
    Use this for complete audit record integrity, not for replay verification.
    
    Args:
        event: ValidationAuditEvent to hash
        
    Returns:
        SHA-256 hex digest of full audit record (64 characters)
    """
    # Use full JSON representation (includes timestamp)
    canonical_json = audit_event_to_json(event)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


# ============================================================================
# Anti-Patterns (Documentation)
# ============================================================================

# ❌ Logging raw validation bundle JSON inside event
# ❌ Embedding entire object snapshot (store hash only)
# ❌ Generating UUID inside file
# ❌ Using datetime.now() (must be externally supplied)
# ❌ Using non-sorted dict context
# ❌ Allowing mutation of audit record
# ❌ Including timestamp in deterministic hash (breaks replay)
# ❌ Generating execution_id internally (must be externally provided)

# This layer must behave like cryptographic material:
# - Immutable
# - Deterministic
# - Hash-verifiable
# - Replay-safe


# ============================================================================
# Governance Use Cases
# ============================================================================

# With this model you can:
#
# 1. Prove no post-hoc rule weakening
#    - Compare bundle_hash across time
#    - Detect silent rule removal via rule_execution_fingerprint
#
# 2. Audit policy drift
#    - Compare decision_hash across policy versions
#    - Detect silent policy broadening
#
# 3. Track schema evolution impacts
#    - Compare bundle_hash across schema versions
#    - Track validation behavior changes
#
# 4. Compare validation behavior across deployments
#    - Same input_hash → same bundle_hash (if deterministic)
#    - Same bundle_hash + profile → same decision
#
# 5. Detect silent determinism violations
#    - Replay verification detects non-deterministic behavior
#
# 6. Implement compliance export
#    - Full audit trail with cryptographic integrity
#    - Tamper-verifiable records
#    - Replay-safe verification
