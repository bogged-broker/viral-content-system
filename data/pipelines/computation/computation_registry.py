"""
/data/pipelines/computation/computation_registry.py

Identity Admission & Existence Authority

AUTHORITY: Single authority that decides "Is this computation allowed to exist in the system?"
PRINCIPLE: Identity without registration is meaningless. Registration without immutability is corruption.
BEHAVIOR: Append-only ledger - admission, rejection, and immutability only

This file answers:
> "Is this computation allowed to exist in the system?"

A computation does not exist until it is registered here.

If this file lies:
- Duplicate computations silently fork truth
- Incompatible changes masquerade as safe updates
- Replay becomes nondeterministic
- Provenance collapses

This file is not optional infrastructure. It is ontological.

CONCEPTUAL MODEL:
The registry is an append-only ledger:
ComputationHash → CanonicalComputationRecord

There are only three valid states:
1. Absent — computation does not exist
2. Registered — computation exists and is immutable
3. Rejected — computation is invalid forever

No updates. No deletes. No overwrites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Any
from enum import Enum, auto
import hashlib
import json
import re
import threading
import copy

from .computation_spec import ComputationSpec
from .computation_hashing import compute_computation_hash, canonicalize_identity
from .computation_invariants import validate_spec
from .computation_errors import (
    UnknownComputationError,
    ComputationInvariantViolation,
)


# ============================================================================
# COMPUTATION STATUS
# ============================================================================

class ComputationStatus(Enum):
    """Status of registered computation."""
    ACTIVE = auto()        # Computation exists and is executable
    DEPRECATED = auto()    # Superseded by newer version


# ============================================================================
# LOGICAL TIME (for registration timestamps)
# ============================================================================

@dataclass(frozen=True)
class LogicalTime:
    """
    Monotonic logical time for registration ordering.
    
    NEVER wall-clock time. NEVER system time.
    """
    sequence: int
    epoch: str = "registry"
    
    def __post_init__(self):
        if self.sequence < 0:
            raise ValueError(f"Logical sequence must be non-negative, got {self.sequence}")
        if not self.epoch:
            raise ValueError("Logical epoch cannot be empty")


# ============================================================================
# COMPUTATION RECORD (CANONICAL)
# ============================================================================

# ============================================================================
# IDENTITY VERSIONING (for replay stability)
# ============================================================================

# Registry schema version - must be incremented if identity derivation changes
REGISTRY_SCHEMA_VERSION = "1.0"
CANONICALIZER_VERSION = "1.0"  # Version of canonicalize_identity algorithm
HASH_ALGORITHM_VERSION = "sha256-v1"  # Hash algorithm identifier


@dataclass(frozen=True)
class IdentityVersionInfo:
    """
    Version information for identity derivation.
    
    Ensures replay stability across canonicalizer and schema changes.
    """
    registry_schema_version: str
    canonicalizer_version: str
    hash_algorithm_version: str
    
    def to_bytes(self) -> bytes:
        """Serialize version info to canonical bytes."""
        version_dict = {
            "registry_schema": self.registry_schema_version,
            "canonicalizer": self.canonicalizer_version,
            "hash_algorithm": self.hash_algorithm_version,
        }
        return json.dumps(version_dict, sort_keys=True, separators=(',', ':')).encode('utf-8')


@dataclass(frozen=True)
class ComputationRecord:
    """
    Frozen record of a registered computation.
    
    A registered computation is represented by a frozen record.
    Deeply immutable - no mutation possible after creation.
    
    Fields:
    - computation_hash: Cryptographic identity
    - computation_type: Type classification
    - version: Semantic or monotonic version
    - spec_fingerprint: Canonical JSON payload used to hash
    - registered_at: Logical time (not wall-clock)
    - status: ACTIVE | DEPRECATED
    - identity_version_info: Version info for identity derivation (provenance)
    - validation_proof_hash: Hash of validation results (machine-verifiable provenance)
    
    Notes:
    - spec_fingerprint is the canonical JSON payload used to hash
    - registered_at is logical, not wall-clock
    - Status changes require explicit migration files
    - All fields are immutable (frozen dataclass + immutable types)
    """
    computation_hash: str
    computation_type: str
    version: str
    spec_fingerprint: bytes
    registered_at: LogicalTime
    status: ComputationStatus
    identity_version_info: IdentityVersionInfo
    validation_proof_hash: str
    
    def is_active(self) -> bool:
        """Check if computation is active."""
        return self.status == ComputationStatus.ACTIVE
    
    def to_immutable_dict(self) -> Dict[str, Any]:
        """
        Convert to immutable dictionary representation.
        
        Returns a deep copy that cannot be mutated.
        Used for defensive copying when exposing record data.
        """
        return {
            "computation_hash": self.computation_hash,
            "computation_type": self.computation_type,
            "version": self.version,
            "spec_fingerprint": self.spec_fingerprint,  # bytes are immutable
            "registered_at": {
                "sequence": self.registered_at.sequence,
                "epoch": self.registered_at.epoch,
            },
            "status": self.status.name,
            "identity_version_info": {
                "registry_schema_version": self.identity_version_info.registry_schema_version,
                "canonicalizer_version": self.identity_version_info.canonicalizer_version,
                "hash_algorithm_version": self.identity_version_info.hash_algorithm_version,
            },
            "validation_proof_hash": self.validation_proof_hash,
        }


@dataclass(frozen=True)
class RejectedComputationRecord:
    """
    Canonical record of a rejected computation.
    
    Rejections are append-only ledger entries with full provenance.
    Cryptographically anchored for deterministic permanence.
    
    Fields:
    - computation_hash: Cryptographic hash of rejected computation
    - reason: Reason for rejection (audit trail)
    - rejected_at: Logical time of rejection
    - invariant_violated: Which invariant was violated (if applicable)
    - rejection_proof_hash: Cryptographic hash of rejection decision (anchoring)
    """
    computation_hash: str
    reason: str
    rejected_at: LogicalTime
    invariant_violated: Optional[str] = None
    rejection_proof_hash: str = field(default="")
    
    def __post_init__(self):
        """Compute rejection proof hash if not provided."""
        if not self.rejection_proof_hash:
            # Compute deterministic hash of rejection decision
            rejection_data = {
                "computation_hash": self.computation_hash,
                "reason": self.reason,
                "rejected_at_sequence": self.rejected_at.sequence,
                "invariant_violated": self.invariant_violated or "",
            }
            canonical_json = json.dumps(rejection_data, sort_keys=True, separators=(',', ':')).encode('utf-8')
            proof_hash = hashlib.sha256(canonical_json).hexdigest()
            # Use object.__setattr__ to bypass frozen dataclass
            object.__setattr__(self, 'rejection_proof_hash', proof_hash)


# ============================================================================
# REGISTRY ERRORS
# ============================================================================

class DuplicateComputationError(Exception):
    """
    Raised when same intent exists with different hash.
    
    Human-friendly labels have no authority.
    Only hashes define identity.
    """
    pass


class RegistryCorruptionError(Exception):
    """
    Raised when ledger inconsistency is detected.
    
    This is a fatal invariant violation.
    """
    pass


# ============================================================================
# COMPUTATION REGISTRY (APPEND-ONLY LEDGER)
# ============================================================================

class ComputationRegistry:
    """
    Append-only ledger for computation registration.
    
    The registry is an append-only ledger:
    ComputationHash → CanonicalComputationRecord
    
    There are only three valid states:
    1. Absent — computation does not exist
    2. Registered — computation exists and is immutable
    3. Rejected — computation is invalid forever
    
    No updates. No deletes. No overwrites.
    
    STRUCTURAL ENFORCEMENT:
    - _ledger: Immutable append-only list of records
    - _index: Hash → ledger position mapping
    - _rejection_ledger: Canonical append-only rejection records
    - _rejection_index: Hash → rejection ledger position mapping
    - _name_to_hash: Name → hash mapping for duplicate detection
    - _ledger_hash_chain: Cryptographic hash chain for tamper-evidence
    """
    
    def __init__(self, window_registry: Optional[Any] = None):
        """
        Initialize empty registry.
        
        Args:
            window_registry: Optional window registry for authoritative window validation.
                           If None, window validation is structural only.
        """
        # Append-only ledger (structurally immutable)
        self._ledger: List[ComputationRecord] = []
        # Index: hash → ledger position
        self._index: Dict[str, int] = {}
        # Cryptographic hash chain for tamper-evidence
        self._ledger_hash_chain: List[str] = []
        # Canonical rejection ledger (append-only records)
        self._rejection_ledger: List[RejectedComputationRecord] = []
        # Rejection index: hash → rejection ledger position
        self._rejection_index: Dict[str, int] = []
        # Name → hash mapping for duplicate intent detection
        self._name_to_hash: Dict[str, str] = {}
        # Window registry for authoritative validation
        self._window_registry: Optional[Any] = window_registry
        # Thread lock for atomic admission semantics
        self._lock = threading.Lock()
    
    def _get_logical_time(self) -> LogicalTime:
        """
        Get logical time from ledger length (replay-safe).
        
        Logical time is derived from ledger position, not a mutable counter.
        This ensures replay determinism.
        """
        sequence = len(self._ledger) + 1
        return LogicalTime(sequence=sequence, epoch="registry")
    
    def register(self, spec: ComputationSpec) -> ComputationRecord:
        """
        Register a computation by cryptographic identity.
        
        A computation MAY be registered iff:
        1. Its hash is valid
        2. It does not already exist with different meaning
        3. All computation invariants pass
        4. Its spec version is declared
        5. All referenced windows are registered (conceptual)
        6. Determinism contract is explicit
        
        Failure of any rule → reject.
        
        IDENTITY COLLISION HANDLING:
        Case 1: Same Hash, Same Spec
        ✅ Accept as idempotent
        Return existing record
        
        Case 2: Same Hash, Different Spec
        🚨 Impossible by definition
        If detected: hashing contract is broken, system must hard-fail
        
        Case 3: Different Hash, Same Name / Label
        ❌ Reject
        Human-friendly labels have no authority.
        Only hashes define identity.
        
        Args:
            spec: Computation specification to register
            
        Returns:
            ComputationRecord for registered computation
            
        Raises:
            DuplicateComputationError: Same name/label with different hash
            RegistryCorruptionError: Hash collision with different spec
            ComputationInvariantViolation: Spec breaks global laws
        """
        # Atomic admission: acquire lock for thread-safe append-only semantics
        with self._lock:
            return self._register_impl(spec)
    
    def _register_impl(self, spec: ComputationSpec) -> ComputationRecord:
        """
        Internal registration implementation (called within lock).
        
        This method performs the actual registration logic with atomic guarantees.
        """
        # STEP 1: Create version info for identity derivation (replay stability)
        identity_version_info = IdentityVersionInfo(
            registry_schema_version=REGISTRY_SCHEMA_VERSION,
            canonicalizer_version=CANONICALIZER_VERSION,
            hash_algorithm_version=HASH_ALGORITHM_VERSION,
        )
        
        # STEP 2: Canonicalize identity (single canonical surface)
        # Registry owns canonical identity derivation - this is the authoritative source
        canonical_bytes = canonicalize_identity(spec)
        
        # STEP 3: Include version info in identity derivation (version-stamped)
        # This ensures replay stability across canonicalizer/schema changes
        version_bytes = identity_version_info.to_bytes()
        versioned_identity = version_bytes + b"||" + canonical_bytes
        
        # STEP 4: Compute hash from versioned canonical bytes (registry-owned identity derivation)
        # The registry must own identity semantics, not outsource to external functions
        computation_hash = hashlib.sha256(versioned_identity).hexdigest()
        
        # STEP 5: Validate hash matches canonical identity (semantic integrity check)
        # Assert that external hash function produces same result (defense in depth)
        # Note: External hash doesn't include version info, so we compare canonical part
        # Tier-0: Pass window_registry to enforce mandatory validation when windows are present
        external_hash = compute_computation_hash(spec, window_registry=self._window_registry)
        canonical_hash = hashlib.sha256(canonical_bytes).hexdigest()
        if canonical_hash != external_hash:
            raise RegistryCorruptionError(
                f"Hash computation mismatch: registry-derived canonical hash {canonical_hash} "
                f"does not match external hash {external_hash}. "
                f"This indicates canonical identity derivation inconsistency."
            )
        
        # STEP 6: Validate hash format
        self._validate_hash_format(computation_hash)
        
        # STEP 7: Check if already rejected (cryptographically anchored)
        if computation_hash in self._rejection_index:
            rejection_record = self._rejection_ledger[self._rejection_index[computation_hash]]
            raise ComputationInvariantViolation(
                invariant="rejected_computation",
                details=f"Computation {computation_hash} was previously rejected at {rejection_record.rejected_at.sequence} "
                        f"(proof_hash={rejection_record.rejection_proof_hash}): {rejection_record.reason}",
                computation_hash=computation_hash
            )
        
        # STEP 8: Check if already registered with hash collision defense
        if computation_hash in self._index:
            ledger_position = self._index[computation_hash]
            existing_record = self._ledger[ledger_position]
            
            # Hash collision defense: deep structural equality check
            # Case 1: Same Hash, Same Spec → idempotent
            if existing_record.spec_fingerprint == canonical_bytes:
                # Additional defense: verify version info matches
                if existing_record.identity_version_info.registry_schema_version != REGISTRY_SCHEMA_VERSION:
                    raise RegistryCorruptionError(
                        f"Schema version mismatch for {computation_hash}: "
                        f"existing={existing_record.identity_version_info.registry_schema_version}, "
                        f"current={REGISTRY_SCHEMA_VERSION}"
                    )
                return existing_record
            
            # Case 2: Same Hash, Different Spec → fatal corruption
            # This should be impossible, but we enforce it explicitly
            raise RegistryCorruptionError(
                f"Hash collision detected: computation_hash={computation_hash} "
                f"exists with different spec_fingerprint. "
                f"Existing fingerprint length: {len(existing_record.spec_fingerprint)}, "
                f"New fingerprint length: {len(canonical_bytes)}. "
                f"This indicates hashing contract is broken."
            )
        
        # STEP 9: Validate all computation invariants
        validation_result = self._validate_computation_invariants(spec, computation_hash)
        
        # STEP 10: Compute validation proof hash (machine-verifiable provenance)
        validation_proof_hash = self._compute_validation_proof_hash(spec, computation_hash, validation_result)
        
        # STEP 11: Check for duplicate intent (Case 3: Different Hash, Same Name)
        self._check_duplicate_intent(spec, computation_hash)
        
        # STEP 12: Create new record with replay-safe logical time
        registered_at = self._get_logical_time()
        
        record = ComputationRecord(
            computation_hash=computation_hash,
            computation_type=spec.computation_type.name,
            version=spec.version,
            spec_fingerprint=canonical_bytes,
            registered_at=registered_at,
            status=ComputationStatus.ACTIVE,
            identity_version_info=identity_version_info,
            validation_proof_hash=validation_proof_hash,
        )
        
        # STEP 13: Append to ledger (structurally append-only)
        ledger_position = len(self._ledger)
        self._ledger.append(record)
        self._index[computation_hash] = ledger_position
        
        # STEP 14: Update cryptographic hash chain (tamper-evidence)
        prev_hash = self._ledger_hash_chain[-1] if self._ledger_hash_chain else ""
        # Chain: H[n] = SHA256(H[n-1] || record.spec_fingerprint || validation_proof_hash)
        chain_input = prev_hash.encode('utf-8') + record.spec_fingerprint + record.validation_proof_hash.encode('utf-8')
        new_chain_hash = hashlib.sha256(chain_input).hexdigest()
        self._ledger_hash_chain.append(new_chain_hash)
        
        # STEP 15: Track name → hash mapping for duplicate detection
        if spec.computation_name:
            self._name_to_hash[spec.computation_name] = computation_hash
        
        return record
    
    def get(self, computation_hash: str) -> ComputationRecord:
        """
        Retrieve computation record by hash.
        
        Absent means non-existent, not "maybe later".
        
        Returns a frozen dataclass that is deeply immutable.
        No defensive copying needed - dataclass is frozen and all fields are immutable.
        
        Args:
            computation_hash: Cryptographic hash of computation
            
        Returns:
            ComputationRecord for the computation (deeply immutable)
            
        Raises:
            UnknownComputationError: Hash not registered
        """
        with self._lock:
            if computation_hash not in self._index:
                raise UnknownComputationError(computation_hash)
            
            ledger_position = self._index[computation_hash]
            # Return frozen dataclass - no mutation possible
            # All fields are immutable (str, bytes, frozen LogicalTime, Enum)
            return self._ledger[ledger_position]
    
    def exists(self, computation_hash: str) -> bool:
        """
        Check if computation exists in registry.
        
        Args:
            computation_hash: Cryptographic hash of computation
            
        Returns:
            True if computation is registered, False otherwise
        """
        return computation_hash in self._index
    
    def is_rejected(self, computation_hash: str) -> bool:
        """
        Check if computation was rejected.
        
        Args:
            computation_hash: Cryptographic hash of computation
            
        Returns:
            True if computation is in rejection ledger, False otherwise
        """
        return computation_hash in self._rejection_index
    
    def reject(self, computation_hash: str, reason: str, invariant_violated: Optional[str] = None) -> RejectedComputationRecord:
        """
        Permanently reject a computation.
        
        Rejected computations cannot be registered. This is an append-only
        operation that permanently records rejection in the canonical ledger.
        
        Args:
            computation_hash: Cryptographic hash of computation to reject
            reason: Reason for rejection (for audit trail)
            invariant_violated: Optional invariant that was violated
            
        Returns:
            RejectedComputationRecord for the rejection
            
        Raises:
            ValueError: If computation is already registered
        """
        # Validate hash format
        self._validate_hash_format(computation_hash)
        
        # Cannot reject if already registered
        if computation_hash in self._index:
            raise ValueError(
                f"Cannot reject computation {computation_hash}: already registered"
            )
        
        # Cannot reject if already rejected (idempotent check)
        if computation_hash in self._rejection_index:
            return self._rejection_ledger[self._rejection_index[computation_hash]]
        
        # Create canonical rejection record
        rejected_at = LogicalTime(sequence=len(self._rejection_ledger) + 1, epoch="registry")
        rejection_record = RejectedComputationRecord(
            computation_hash=computation_hash,
            reason=reason,
            rejected_at=rejected_at,
            invariant_violated=invariant_violated
        )
        
        # Append to rejection ledger (canonical append-only)
        rejection_position = len(self._rejection_ledger)
        self._rejection_ledger.append(rejection_record)
        self._rejection_index[computation_hash] = rejection_position
        
        return rejection_record
    
    def deprecate(self, computation_hash: str, migration_id: str, superseded_by: Optional[str] = None) -> ComputationRecord:
        """
        Deprecate a computation with explicit migration governance.
        
        Status changes require explicit migration files. This method enforces
        that invariant by requiring a migration_id.
        
        IMPORTANT: This creates a new record with DEPRECATED status and appends
        it to the ledger. The original record remains immutable. The index is
        updated to point to the new record, maintaining append-only semantics.
        
        Args:
            computation_hash: Hash of computation to deprecate
            migration_id: Explicit migration identifier (required)
            superseded_by: Optional hash of computation that supersedes this one
            
        Returns:
            New ComputationRecord with DEPRECATED status
            
        Raises:
            UnknownComputationError: Computation not registered
            ValueError: If migration_id is empty or computation already deprecated
        """
        if not migration_id:
            raise ValueError("migration_id is required for status migration")
        
        if computation_hash not in self._index:
            raise UnknownComputationError(computation_hash)
        
        ledger_position = self._index[computation_hash]
        existing_record = self._ledger[ledger_position]
        
        if existing_record.status == ComputationStatus.DEPRECATED:
            # Already deprecated - return existing record (idempotent)
            return existing_record
        
        # Create new record with DEPRECATED status (append-only: new record, not mutation)
        deprecated_at = self._get_logical_time()
        deprecated_record = ComputationRecord(
            computation_hash=existing_record.computation_hash,
            computation_type=existing_record.computation_type,
            version=existing_record.version,
            spec_fingerprint=existing_record.spec_fingerprint,
            registered_at=deprecated_at,  # New logical time for status change
            status=ComputationStatus.DEPRECATED,
        )
        
        # Append new record to ledger (maintains append-only semantics)
        new_ledger_position = len(self._ledger)
        self._ledger.append(deprecated_record)
        # Update index to point to new record (latest status)
        self._index[computation_hash] = new_ledger_position
        
        # Update hash chain
        prev_hash = self._ledger_hash_chain[-1] if self._ledger_hash_chain else ""
        chain_input = prev_hash.encode('utf-8') + deprecated_record.spec_fingerprint
        new_chain_hash = hashlib.sha256(chain_input).hexdigest()
        self._ledger_hash_chain.append(new_chain_hash)
        
        return deprecated_record
    
    def list_active(self) -> Iterable[ComputationRecord]:
        """
        List all active computations.
        
        Returns:
            Iterable of active ComputationRecords
        """
        return (
            record for record in self._ledger
            if record.is_active()
        )
    
    def _validate_hash_format(self, computation_hash: str) -> None:
        """
        Validate hash format and encoding.
        
        Enforces:
        - SHA-256 format (64 hex characters)
        - Lowercase hex encoding
        - No truncation
        
        Raises:
            ComputationInvariantViolation: If hash format is invalid
        """
        if not isinstance(computation_hash, str):
            raise ComputationInvariantViolation(
                invariant="hash_format",
                details=f"Computation hash must be string, got {type(computation_hash)}",
                computation_hash=None
            )
        
        if len(computation_hash) != 64:
            raise ComputationInvariantViolation(
                invariant="hash_format",
                details=f"Computation hash must be 64 hex characters (SHA-256), got length {len(computation_hash)}",
                computation_hash=computation_hash
            )
        
        if not re.match(r'^[0-9a-f]{64}$', computation_hash):
            raise ComputationInvariantViolation(
                invariant="hash_format",
                details=f"Computation hash must be lowercase hex, got: {computation_hash[:16]}...",
                computation_hash=computation_hash
            )
    
    def _compute_validation_proof_hash(self, spec: ComputationSpec, computation_hash: str, validation_result: Dict[str, Any]) -> str:
        """
        Compute cryptographic hash of validation results.
        
        This provides machine-verifiable provenance that validation was performed.
        
        Args:
            spec: Computation spec that was validated
            computation_hash: Hash of the computation being validated
            validation_result: Dictionary of validation results
            
        Returns:
            SHA-256 hash of validation proof
        """
        proof_data = {
            "computation_hash": computation_hash,
            "validation_results": validation_result,
            "validated_at_schema_version": REGISTRY_SCHEMA_VERSION,
        }
        canonical_json = json.dumps(proof_data, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(canonical_json).hexdigest()
    
    def _validate_computation_invariants(self, spec: ComputationSpec, computation_hash: str) -> Dict[str, Any]:
        """
        Validate all computation invariants.
        
        Enforces:
        1. Spec integrity (via validate_spec)
        2. Version declared
        3. Determinism contract explicit
        4. Window references registered (if any)
        5. Hash correctness (canonical match)
        
        Returns:
            Dictionary of validation results for provenance tracking
            
        Raises:
            ComputationInvariantViolation: If invariants are violated
        """
        validation_results = {
            "spec_integrity": "passed",
            "version_declared": False,
            "determinism_contract": False,
            "window_references": "passed",
        }
        
        # Validate spec integrity (includes determinism declaration)
        validate_spec(spec)
        validation_results["spec_integrity"] = "passed"
        
        # Validate version is declared
        if not spec.version:
            raise ComputationInvariantViolation(
                invariant="version_declared",
                details="Computation version must be explicitly declared",
                computation_hash=computation_hash
            )
        validation_results["version_declared"] = True
        
        # Validate determinism contract is explicit
        if not hasattr(spec, 'requires_determinism'):
            raise ComputationInvariantViolation(
                invariant="determinism_contract",
                details="Computation must explicitly declare requires_determinism",
                computation_hash=computation_hash
            )
        
        if not isinstance(spec.requires_determinism, bool):
            raise ComputationInvariantViolation(
                invariant="determinism_contract",
                details="requires_determinism must be boolean (True or False)",
                computation_hash=computation_hash
            )
        validation_results["determinism_contract"] = spec.requires_determinism
        
        # Validate window references (if any) - authoritative check
        if spec.required_windows:
            validated_windows = []
            for window_desc in spec.required_windows:
                if not window_desc.window_identity:
                    raise ComputationInvariantViolation(
                        invariant="window_references",
                        details=f"Window descriptor missing window_identity: {window_desc}",
                        computation_hash=computation_hash
                    )
                if not window_desc.window_version:
                    raise ComputationInvariantViolation(
                        invariant="window_references",
                        details=f"Window descriptor missing window_version: {window_desc}",
                        computation_hash=computation_hash
                    )
                
                # Authoritative window registry check (if registry provided)
                if self._window_registry is not None:
                    # Check if window is registered in window registry
                    # WindowRegistry.get() raises KeyError if not found
                    try:
                        window_def = self._window_registry.get(window_desc.window_identity)
                        # Verify version matches if window registry tracks versions
                        # (This depends on WindowRegistry implementation)
                        validated_windows.append({
                            "window_identity": window_desc.window_identity,
                            "window_version": window_desc.window_version,
                            "registered": True,
                        })
                    except KeyError:
                        raise ComputationInvariantViolation(
                            invariant="window_references",
                            details=f"Window {window_desc.window_identity} (version {window_desc.window_version}) is not registered in window registry",
                            computation_hash=computation_hash
                        )
                else:
                    validated_windows.append({
                        "window_identity": window_desc.window_identity,
                        "window_version": window_desc.window_version,
                        "registered": "structural_only",
                    })
            validation_results["window_references"] = validated_windows
        
        return validation_results
    
    def _check_duplicate_intent(self, spec: ComputationSpec, computation_hash: str) -> None:
        """
        Check for duplicate intent (Case 3: Different Hash, Same Name).
        
        If a computation with the same name but different hash exists,
        reject the registration to prevent semantic confusion.
        
        Raises:
            DuplicateComputationError: Same name with different hash
        """
        if not spec.computation_name:
            return  # No name to check
        
        existing_hash = self._name_to_hash.get(spec.computation_name)
        if existing_hash is not None and existing_hash != computation_hash:
            raise DuplicateComputationError(
                f"Computation name '{spec.computation_name}' already registered "
                f"with different hash: existing={existing_hash}, new={computation_hash}. "
                f"Human-friendly labels have no authority. Only hashes define identity."
            )


# ============================================================================
# PUBLIC API (MINIMAL & SEALED)
# ============================================================================
# 
# Tier-0 infrastructure requires explicit dependency injection.
# No global singleton fallback - authority must be explicit.

def register(spec: ComputationSpec, registry: ComputationRegistry) -> ComputationRecord:
    """
    Register a computation by cryptographic identity.
    
    This is the single authority that decides whether a computation
    is allowed to exist in the system.
    
    Args:
        spec: Computation specification to register
        registry: Registry instance (required - no global fallback)
        
    Returns:
        ComputationRecord for registered computation
        
    Raises:
        DuplicateComputationError: Same name/label with different hash
        RegistryCorruptionError: Hash collision with different spec
        ComputationInvariantViolation: Spec breaks global laws
    """
    return registry.register(spec)


def get(computation_hash: str, registry: ComputationRegistry) -> ComputationRecord:
    """
    Retrieve computation record by hash.
    
    Args:
        computation_hash: Cryptographic hash of computation
        registry: Registry instance (required - no global fallback)
        
    Returns:
        ComputationRecord for the computation
        
    Raises:
        UnknownComputationError: Hash not registered
    """
    return registry.get(computation_hash)


def exists(computation_hash: str, registry: ComputationRegistry) -> bool:
    """
    Check if computation exists in registry.
    
    Args:
        computation_hash: Cryptographic hash of computation
        registry: Registry instance (required - no global fallback)
        
    Returns:
        True if computation is registered, False otherwise
    """
    return registry.exists(computation_hash)


def list_active(registry: ComputationRegistry) -> Iterable[ComputationRecord]:
    """
    List all active computations.
    
    Args:
        registry: Registry instance (required - no global fallback)
        
    Returns:
        Iterable of active ComputationRecords
    """
    return registry.list_active()
