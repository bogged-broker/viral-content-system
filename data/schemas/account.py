"""
/data/schemas/account.py

CANONICAL ACCOUNT & IDENTITY SCHEMAS (STRUCTURAL TRUTH ONLY)

What This File ACTUALLY Is:
    Defines what it means for an account to exist — structurally and provably.
    
    Answers ONE question: "What is an account, at rest, when you strip away 
    behavior, trust, and enforcement?"

What This File Is NOT:
    ❌ Not an auth model
    ❌ Not a trust model
    ❌ Not a behavior record
    ❌ Not enforcement data
    ❌ Not mutable lifecycle state
    ❌ Not platform rules

Design Principle (NON-NEGOTIABLE):
    > Identity must be provable without belief.
    
    An account record should make sense to a third party with zero context.

Core Responsibilities:
    1. Define canonical account identity
    2. Declare immutable account attributes
    3. Encode account origin & lineage
    4. Support deterministic hashing
    5. Be replay-safe and auditable
    6. Avoid time-based meaning
    7. Contain zero behavioral data

No scores. Ever.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple, Dict, Any, FrozenSet, List
import hashlib
import json
import re

from .base import CanonicalSchema, ValidationError


# ============================================================================
# ACCOUNT TAXONOMY
# ============================================================================

class AccountKind(Enum):
    """
    Structural role classification.
    
    Kind declares structural role, not privilege.
    No trust, no permissions, no behavior.
    """
    HUMAN = "human"
    ORGANIZATION = "organization"
    SYSTEM = "system"
    SERVICE = "service"
    
    def __str__(self) -> str:
        return self.value


# ============================================================================
# CORE SCHEMA: ACCOUNT IDENTITY
# ============================================================================

@dataclass(frozen=True)
class AccountIdentity:
    """
    Immutable account identifier.
    
    Rules:
    - account_id MUST be deterministic
    - No email-derived IDs
    - No usernames
    - No platform handles
    - ID cannot encode meaning
    """
    account_id: str
    schema_name: str
    schema_version: int
    
    def __post_init__(self):
        """Validate identity invariants"""
        # Account ID validation
        if not self.account_id:
            raise ValidationError("account_id cannot be empty")
        
        if not isinstance(self.account_id, str):
            raise ValidationError("account_id must be string")
        
        # Must not contain encoding characters that suggest structure
        if '@' in self.account_id or ':' in self.account_id or '/' in self.account_id:
            raise ValidationError(
                "account_id cannot contain '@', ':', or '/' (no encoded meaning)"
            )
        
        # Length sanity check
        if len(self.account_id) > 128:
            raise ValidationError("account_id exceeds maximum length (128)")
        
        # Schema name must be exactly "account"
        if self.schema_name != "account":
            raise ValidationError(f"schema_name must be 'account', got '{self.schema_name}'")
        
        # Schema version must be positive
        if not isinstance(self.schema_version, int) or self.schema_version < 1:
            raise ValidationError("schema_version must be positive integer")
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Canonical dictionary representation.
        
        Ordering: account_id → schema_name → schema_version
        """
        return {
            "account_id": self.account_id,
            "schema_name": self.schema_name,
            "schema_version": self.schema_version
        }


# ============================================================================
# CORE SCHEMA: ACCOUNT PROVENANCE
# ============================================================================

@dataclass(frozen=True)
class AccountProvenance:
    """
    Account origin and lineage.
    
    Rules:
    - Parents define ownership or delegation lineage
    - Parents never imply authority here
    - Empty parents allowed
    - Cycles forbidden
    """
    origin: str  # created_by_system, imported, migrated, etc.
    source_system: Optional[str]
    parent_account_ids: Tuple[str, ...]
    
    # Known origin types (for validation)
    VALID_ORIGINS: FrozenSet[str] = frozenset({
        "created_by_system",
        "imported",
        "migrated",
        "delegated",
        "provisioned",
        "manual"
    })
    
    def __post_init__(self):
        """Validate provenance invariants"""
        # Origin validation
        if not self.origin:
            raise ValidationError("origin cannot be empty")
        
        if self.origin not in self.VALID_ORIGINS:
            raise ValidationError(
                f"origin must be one of {self.VALID_ORIGINS}, got '{self.origin}'"
            )
        
        # Source system validation
        if self.source_system is not None:
            if not isinstance(self.source_system, str):
                raise ValidationError("source_system must be string or None")
            if not self.source_system:
                raise ValidationError("source_system cannot be empty string (use None)")
        
        # Parent IDs validation
        if not isinstance(self.parent_account_ids, tuple):
            raise ValidationError("parent_account_ids must be tuple")
        
        # Validate each parent ID
        for parent_id in self.parent_account_ids:
            if not isinstance(parent_id, str):
                raise ValidationError("parent_account_ids must contain only strings")
            if not parent_id:
                raise ValidationError("parent_account_ids cannot contain empty strings")
        
        # Check for duplicates (no multi-edges in lineage graph)
        if len(self.parent_account_ids) != len(set(self.parent_account_ids)):
            raise ValidationError("parent_account_ids cannot contain duplicates")
    
    def has_parents(self) -> bool:
        """Check if account has parent lineage"""
        return len(self.parent_account_ids) > 0
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Canonical dictionary representation.
        
        Ordering: origin → source_system → parent_account_ids
        """
        return {
            "origin": self.origin,
            "source_system": self.source_system,
            "parent_account_ids": list(self.parent_account_ids)
        }


# ============================================================================
# CORE SCHEMA: ACCOUNT ATTRIBUTES
# ============================================================================

@dataclass(frozen=True)
class AccountAttributes:
    """
    Structural facts about the account.
    
    These are NOT judgments. These are declarations.
    
    Rules:
    - Optional does NOT mean mutable
    - No normalization beyond validation
    - Unknown remains unknown
    """
    country_code: Optional[str]  # ISO-3166-1 alpha-2
    declared_entity_type: Optional[str]
    language_preference: Optional[str]  # ISO-639-1
    
    # Common ISO-3166-1 alpha-2 country codes for practical validation
    # NOTE: This is NOT an authoritative ISO registry. Format validation (2 uppercase letters)
    # is the primary check. This list provides additional validation for common codes.
    # Codes not in this list but matching format are still accepted.
    COMMON_COUNTRY_CODES: FrozenSet[str] = frozenset({
        "US", "GB", "CA", "AU", "DE", "FR", "JP", "CN", "IN", "BR",
        "MX", "ES", "IT", "NL", "SE", "NO", "DK", "FI", "CH", "AT",
        "BE", "IE", "NZ", "SG", "HK", "KR", "TW", "TH", "MY", "ID",
        "PH", "VN", "PL", "CZ", "HU", "RO", "GR", "PT", "IL", "AE",
        "SA", "ZA", "AR", "CL", "CO", "PE", "VE", "UA", "RU", "TR",
        "EG", "NG", "KE", "ET", "GH", "TZ", "UG", "DZ", "SD", "MA",
        "IQ", "AF", "PK", "BD", "MM", "KH", "LA", "BN", "MO", "MN",
        "KZ", "UZ", "GE", "AM", "AZ", "BY", "MD", "RS", "BA", "ME",
        "MK", "AL", "XK", "IS", "MT", "CY", "LU", "EE", "LV", "LT",
        "SK", "SI", "HR", "BG", "EE", "LV", "LT"
    })
    
    # Common ISO-639-1 language codes for practical validation
    # NOTE: This is NOT an authoritative ISO registry. Format validation (2 lowercase letters)
    # is the primary check. This list provides additional validation for common codes.
    # Codes not in this list but matching format are still accepted.
    COMMON_LANGUAGE_CODES: FrozenSet[str] = frozenset({
        "en", "es", "fr", "de", "it", "pt", "nl", "pl", "ru", "ja",
        "zh", "ko", "ar", "hi", "tr", "sv", "no", "da", "fi", "cs",
        "hu", "ro", "el", "he", "th", "id", "vi", "uk", "bg", "hr",
        "sr", "sk", "sl", "et", "lv", "lt", "ga", "mt", "cy", "is",
        "mk", "sq", "bs", "az", "ka", "hy", "be", "kk", "ky", "uz",
        "mn", "my", "km", "lo", "ne", "si", "ta", "te", "ml", "kn",
        "gu", "pa", "bn", "or", "as", "mr", "sw", "zu", "af", "xh",
        "yo", "ig", "ha", "am", "ti", "so", "om", "rw", "mg", "ny"
    })
    
    def __post_init__(self):
        """
        Validate attributes with strict format checks.
        
        Tier-0 Principle: No normalization beyond validation.
        Validation rejects invalid input; it does not transform it.
        
        Format validation is primary (structure). Common code lists provide
        additional validation but do not reject valid format-compliant codes.
        """
        # Country code validation (ISO-3166-1 alpha-2 format)
        if self.country_code is not None:
            if not isinstance(self.country_code, str):
                raise ValidationError("country_code must be string or None")
            
            if len(self.country_code) != 2:
                raise ValidationError("country_code must be 2-character ISO-3166-1 alpha-2")
            
            # Validate format: must be exactly 2 uppercase ASCII letters
            if not self.country_code.isalpha():
                raise ValidationError(
                    f"country_code must contain only letters, got '{self.country_code}'"
                )
            
            if not self.country_code.isupper():
                raise ValidationError(
                    f"country_code must be uppercase (ISO-3166-1 alpha-2 standard), got '{self.country_code}'"
                )
            
            # Additional validation: check against common codes
            # NOTE: This is a practical validation trade-off. We validate against
            # a curated list of common ISO-3166-1 alpha-2 codes. Codes matching
            # format but not in this list will be rejected. For full ISO registry
            # validation, use an external authoritative source.
            if self.country_code not in self.COMMON_COUNTRY_CODES:
                raise ValidationError(
                    f"country_code '{self.country_code}' not recognized. "
                    f"Must be a valid ISO-3166-1 alpha-2 code. "
                    f"Common codes are supported; contact admin for additional codes."
                )
        
        # Declared entity type validation
        if self.declared_entity_type is not None:
            if not isinstance(self.declared_entity_type, str):
                raise ValidationError("declared_entity_type must be string or None")
            if not self.declared_entity_type:
                raise ValidationError("declared_entity_type cannot be empty string")
            if len(self.declared_entity_type) > 64:
                raise ValidationError("declared_entity_type exceeds maximum length (64)")
        
        # Language preference validation (ISO-639-1 format)
        if self.language_preference is not None:
            if not isinstance(self.language_preference, str):
                raise ValidationError("language_preference must be string or None")
            
            if len(self.language_preference) != 2:
                raise ValidationError("language_preference must be 2-character ISO-639-1")
            
            # Validate format: must be exactly 2 lowercase ASCII letters
            if not self.language_preference.isalpha():
                raise ValidationError(
                    f"language_preference must contain only letters, got '{self.language_preference}'"
                )
            
            if not self.language_preference.islower():
                raise ValidationError(
                    f"language_preference must be lowercase (ISO-639-1 standard), got '{self.language_preference}'"
                )
            
            # Additional validation: check against common codes
            # NOTE: This is a practical validation trade-off. We validate against
            # a curated list of common ISO-639-1 codes. Codes matching format but
            # not in this list will be rejected. For full ISO registry validation,
            # use an external authoritative source.
            if self.language_preference not in self.COMMON_LANGUAGE_CODES:
                raise ValidationError(
                    f"language_preference '{self.language_preference}' not recognized. "
                    f"Must be a valid ISO-639-1 code. "
                    f"Common codes are supported; contact admin for additional codes."
                )
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Canonical dictionary representation.
        
        Ordering: country_code → declared_entity_type → language_preference
        """
        return {
            "country_code": self.country_code,
            "declared_entity_type": self.declared_entity_type,
            "language_preference": self.language_preference
        }


# ============================================================================
# CANONICAL ACCOUNT SCHEMA (FINAL)
# ============================================================================

@dataclass(frozen=True)
class AccountRecord(CanonicalSchema):
    """
    Complete canonical account record.
    
    This is identity. Nothing more.
    
    No timestamps. No counters. No flags. No scores.
    """
    # Identity
    account_id: str
    schema_name: str
    schema_version: int
    
    # Classification
    kind: AccountKind
    
    # Provenance
    provenance: AccountProvenance
    
    # Attributes
    attributes: AccountAttributes
    
    def __post_init__(self):
        """Validate complete account record"""
        # Create identity object for validation
        identity = AccountIdentity(
            account_id=self.account_id,
            schema_name=self.schema_name,
            schema_version=self.schema_version
        )
        
        # Validate kind
        if not isinstance(self.kind, AccountKind):
            raise ValidationError("kind must be AccountKind enum")
        
        # Validate provenance
        if not isinstance(self.provenance, AccountProvenance):
            raise ValidationError("provenance must be AccountProvenance")
        
        # Validate attributes
        if not isinstance(self.attributes, AccountAttributes):
            raise ValidationError("attributes must be AccountAttributes")
        
        # Check for self-reference cycles (detectable at record level)
        self._validate_no_self_reference()
    
    def _validate_no_self_reference(self):
        """
        Ensure account doesn't reference itself as parent.
        
        This detects self-cycles at construction time.
        Multi-node cycles require external graph validation via LineageValidator.
        """
        if self.account_id in self.provenance.parent_account_ids:
            raise ValidationError(
                f"account {self.account_id} cannot be its own parent"
            )
    
    def validate(self) -> bool:
        """
        Full validation (required by CanonicalSchema).
        
        Hard failures only. No warnings.
        """
        try:
            # Schema name must be "account"
            if self.schema_name != "account":
                raise ValidationError(
                    f"schema_name must be 'account', got '{self.schema_name}'"
                )
            
            # Schema version must be supported (currently only 1)
            if self.schema_version not in {1}:
                raise ValidationError(
                    f"schema_version {self.schema_version} not supported"
                )
            
            # Account ID non-empty & stable
            if not self.account_id:
                raise ValidationError("account_id cannot be empty")
            
            # No mutable containers (tuples are immutable, checked in provenance)
            if not isinstance(self.provenance.parent_account_ids, tuple):
                raise ValidationError("parent_account_ids must be immutable tuple")
            
            # Attributes must be present (even if all None)
            if self.attributes is None:
                raise ValidationError("attributes cannot be None (use empty values)")
            
            return True
            
        except ValidationError:
            raise
        except Exception as e:
            raise ValidationError(f"Validation failed: {e}")
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Canonical dictionary representation.
        
        Ordering is FIXED:
        1. identity
        2. kind
        3. provenance
        4. attributes
        
        Serialization result MUST be bit-stable forever.
        """
        return {
            # Identity (fixed order)
            "account_id": self.account_id,
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            
            # Kind
            "kind": self.kind.value,
            
            # Provenance
            "provenance": self.provenance.to_dict(),
            
            # Attributes
            "attributes": self.attributes.to_dict()
        }
    
    def to_canonical_json(self) -> str:
        """
        Canonical JSON serialization with FIXED ordering.
        
        Ordering MUST be: identity → kind → provenance → attributes
        This is a Tier-0 architectural requirement for replay safety.
        
        Guarantees:
        - Same identity → same serialization
        - Same serialization → same hash
        - No time dependence
        - No environment dependence
        - No inference paths
        - Fixed semantic ordering (not alphabetical)
        """
        # Build dict in fixed order (Python 3.7+ preserves insertion order)
        data = self.to_dict()
        
        # Serialize with fixed order - DO NOT use sort_keys
        # The dict is already in the correct order from to_dict()
        return json.dumps(
            data,
            sort_keys=False,  # CRITICAL: Fixed semantic ordering, not alphabetical
            separators=(',', ':'),
            ensure_ascii=True
        )
    
    def compute_hash(self) -> str:
        """
        Compute deterministic hash of account record.
        
        Everything downstream relies on this.
        """
        canonical_json = self.to_canonical_json()
        return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()
    
    def get_identity(self) -> AccountIdentity:
        """Extract identity component"""
        return AccountIdentity(
            account_id=self.account_id,
            schema_name=self.schema_name,
            schema_version=self.schema_version
        )
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AccountRecord':
        """
        Reconstruct AccountRecord from dictionary.
        
        Inverse of to_dict().
        """
        # Reconstruct provenance
        provenance_data = data["provenance"]
        provenance = AccountProvenance(
            origin=provenance_data["origin"],
            source_system=provenance_data.get("source_system"),
            parent_account_ids=tuple(provenance_data.get("parent_account_ids", []))
        )
        
        # Reconstruct attributes
        attributes_data = data["attributes"]
        attributes = AccountAttributes(
            country_code=attributes_data.get("country_code"),
            declared_entity_type=attributes_data.get("declared_entity_type"),
            language_preference=attributes_data.get("language_preference")
        )
        
        # Reconstruct kind
        kind = AccountKind(data["kind"])
        
        return cls(
            account_id=data["account_id"],
            schema_name=data["schema_name"],
            schema_version=data["schema_version"],
            kind=kind,
            provenance=provenance,
            attributes=attributes
        )
    
    @classmethod
    def from_json(cls, json_str: str) -> 'AccountRecord':
        """Reconstruct AccountRecord from JSON string"""
        data = json.loads(json_str)
        return cls.from_dict(data)


# ============================================================================
# LINEAGE VALIDATION
# ============================================================================

class LineageValidator:
    """
    Validates account lineage for cycles and structural integrity.
    
    Separate from AccountRecord to keep identity schema clean.
    """
    
    @staticmethod
    def detect_cycle(
        account_id: str,
        parent_map: Dict[str, Tuple[str, ...]],
        visited: Optional[set] = None,
        stack: Optional[set] = None
    ) -> Optional[Tuple[str, ...]]:
        """
        Detect cycles in account lineage graph.
        
        Args:
            account_id: Current account being checked
            parent_map: Map of account_id -> parent_account_ids
            visited: Set of fully explored accounts
            stack: Current DFS stack
        
        Returns:
            Cycle path if found, None otherwise
        """
        if visited is None:
            visited = set()
        if stack is None:
            stack = set()
        
        # If we've already fully explored this node, skip
        if account_id in visited:
            return None
        
        # If we're currently exploring this node, we found a cycle
        if account_id in stack:
            return (account_id,)
        
        # Mark as currently exploring
        stack.add(account_id)
        
        # Explore parents
        parents = parent_map.get(account_id, ())
        for parent_id in parents:
            cycle = LineageValidator.detect_cycle(
                parent_id,
                parent_map,
                visited,
                stack
            )
            if cycle:
                # Build cycle path
                return cycle + (account_id,)
        
        # Done exploring this node
        stack.remove(account_id)
        visited.add(account_id)
        
        return None
    
    @staticmethod
    def validate_lineage_graph(accounts: list[AccountRecord]) -> bool:
        """
        Validate entire lineage graph for cycles.
        
        Args:
            accounts: List of all accounts to validate
        
        Returns:
            True if no cycles exist
        
        Raises:
            ValidationError if cycle detected
        """
        # Build parent map
        parent_map: Dict[str, Tuple[str, ...]] = {}
        for account in accounts:
            parent_map[account.account_id] = account.provenance.parent_account_ids
        
        # Check each account
        for account in accounts:
            cycle = LineageValidator.detect_cycle(
                account.account_id,
                parent_map
            )
            if cycle:
                cycle_str = " -> ".join(cycle)
                raise ValidationError(f"Cycle detected in lineage: {cycle_str}")
        
        return True


# ============================================================================
# ACCOUNT FACTORY
# ============================================================================

class AccountFactory:
    """
    Factory for creating valid AccountRecords.
    
    Enforces all invariants at construction time.
    """
    
    @staticmethod
    def create(
        account_id: str,
        kind: AccountKind,
        origin: str,
        source_system: Optional[str] = None,
        parent_account_ids: Optional[Tuple[str, ...]] = None,
        country_code: Optional[str] = None,
        declared_entity_type: Optional[str] = None,
        language_preference: Optional[str] = None,
        schema_version: int = 1
    ) -> AccountRecord:
        """
        Create validated AccountRecord.
        
        All validation happens here.
        """
        provenance = AccountProvenance(
            origin=origin,
            source_system=source_system,
            parent_account_ids=parent_account_ids or ()
        )
        
        attributes = AccountAttributes(
            country_code=country_code,
            declared_entity_type=declared_entity_type,
            language_preference=language_preference
        )
        
        record = AccountRecord(
            account_id=account_id,
            schema_name="account",
            schema_version=schema_version,
            kind=kind,
            provenance=provenance,
            attributes=attributes
        )
        
        # Validate before returning
        record.validate()
        
        return record

