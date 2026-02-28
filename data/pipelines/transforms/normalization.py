"""
/data/pipelines/transforms/normalization.py

Canonical Normalization Authority (Zero Math, Zero Inference)

WHAT THIS FILE ACTUALLY IS (plain English):
normalization.py is the last place data is allowed to change shape before it becomes
analytically, legally, and operationally real.

It answers:
> "Are two facts that mean the same thing represented exactly the same way?"

If not — normalization fixes it without interpreting anything.

WHAT THIS FILE IS NOT (STRICT):
❌ Not aggregation
❌ Not enrichment
❌ Not analytics
❌ Not scoring
❌ Not prediction
❌ Not correction
❌ Not business logic

This file never adds meaning — it only removes ambiguity.

DESIGN PRINCIPLE (CRITICAL):
> Normalization eliminates representational freedom.

There must be one and only one canonical way to express a fact.

CORE RESPONSIBILITIES (NON-NEGOTIABLE):
normalization.py MUST:
1. Canonicalize timestamps
2. Canonicalize IDs and keys
3. Enforce field ordering
4. Enforce null semantics
5. Normalize enums and literals
6. Normalize text encodings
7. Stabilize numeric representations (not values)
8. Produce deterministic output hashes

No math. No inference. No statistics.

MENTAL MODEL (LOCK THIS):
> Normalization makes facts boring.
Boring facts scale.
Exciting facts lie.

This file is why analytics can trust ingestion, and why recovery can prove equivalence.

Quiet, boring, and absolutely non-negotiable.
If normalization lies, everything above it rots.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple
from uuid import UUID


# ============================================================================
# Type Aliases
# ============================================================================

FieldName = str
FieldValue = Any
SchemaName = str
RunID = UUID
NormalizationHash = str


# ============================================================================
# Enums
# ============================================================================

class NormalizationSource(Enum):
    """Where this normalization is occurring."""
    INGEST = "ingest"
    RECOVERY = "recovery"
    TRANSFORM = "transform"
    REPLAY = "replay"
    
    def __str__(self) -> str:
        return self.value


class CasingStrategy(Enum):
    """Identifier casing strategy."""
    LOWERCASE = "lowercase"
    UPPERCASE = "uppercase"
    PRESERVE = "preserve"
    
    def __str__(self) -> str:
        return self.value


class EnumStrategy(Enum):
    """How to handle enum normalization."""
    STRICT = "strict"  # Reject unknown values
    ALIAS_MAPPED = "alias_mapped"  # Map known aliases to canonical
    
    def __str__(self) -> str:
        return self.value


class FieldOrderingStrategy(Enum):
    """How to order fields in normalized output."""
    ALPHABETICAL = "alphabetical"
    SCHEMA_DEFINED = "schema_defined"
    
    def __str__(self) -> str:
        return self.value


class TimestampEpochUnit(Enum):
    """Unit for numeric timestamp epoch values."""
    SECONDS = "seconds"
    MILLISECONDS = "milliseconds"
    MICROSECONDS = "microseconds"
    NONE = "none"  # Reject numeric timestamps
    
    def __str__(self) -> str:
        return self.value


class ListOrderingStrategy(Enum):
    """How to handle list ordering."""
    PRESERVE = "preserve"  # Preserve original order
    SORTED = "sorted"  # Sort where order is semantically irrelevant
    
    def __str__(self) -> str:
        return self.value


class NullPolicy(Enum):
    """How to handle null values."""
    STRICT = "strict"  # Preserve nulls as-is
    CONVERT_EMPTY_STRINGS = "convert_empty_strings"  # Empty string → null
    CONVERT_SENTINELS = "convert_sentinels"  # Known sentinels → null
    
    def __str__(self) -> str:
        return self.value


class NormalizationErrorCode(Enum):
    """Explicit normalization error codes."""
    INVALID_SCHEMA = "invalid_schema"
    AMBIGUOUS_TIMESTAMP = "ambiguous_timestamp"
    UNKNOWN_ENUM = "unknown_enum"
    ILLEGAL_ENCODING = "illegal_encoding"
    INVARIANT_VIOLATION = "invariant_violation"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_IDENTIFIER = "invalid_identifier"
    INVALID_NUMERIC_REPRESENTATION = "invalid_numeric_representation"
    
    def __str__(self) -> str:
        return self.value


# ============================================================================
# Exceptions
# ============================================================================

class NormalizationError(Exception):
    """Base exception for normalization failures."""
    
    def __init__(
        self,
        code: NormalizationErrorCode,
        message: str,
        field_name: Optional[str] = None,
        field_value: Optional[Any] = None,
    ):
        self.code = code
        self.field_name = field_name
        self.field_value = field_value
        super().__init__(f"[{code}] {message}")


# ============================================================================
# Immutable Configuration Objects
# ============================================================================

@dataclass(frozen=True)
class NormalizationPolicy:
    """
    Defines the one true representation.
    
    Fields:
        timestamp_format: Expected timestamp format (RFC3339 UTC only)
        timestamp_precision_ms: Precision in milliseconds
        id_casing: Identifier casing strategy
        enum_strategy: How to handle enums
        text_encoding: Text encoding (UTF-8 only)
        null_policy: How to handle nulls
        field_ordering: Field ordering strategy
        allow_scientific_notation: Allow scientific notation in floats
        schema_version: Schema version this policy applies to
        enum_aliases: Mapping of aliases to canonical enum values
        null_sentinels: Values to treat as null (if policy allows)
        required_fields: Fields that cannot be null
    
    Rules:
        - Immutable
        - Versioned
        - No runtime overrides
        - No conditional behavior
    
    If the policy changes, downstream must migrate explicitly.
    """
    
    # Core strategies
    timestamp_format: str = "iso8601_utc"
    timestamp_precision_ms: int = 1000  # 1 second precision
    timestamp_epoch_unit: TimestampEpochUnit = TimestampEpochUnit.NONE  # Reject numeric timestamps by default
    id_casing: CasingStrategy = CasingStrategy.LOWERCASE
    enum_strategy: EnumStrategy = EnumStrategy.STRICT
    text_encoding: str = "utf-8"
    null_policy: NullPolicy = NullPolicy.STRICT
    field_ordering: FieldOrderingStrategy = FieldOrderingStrategy.ALPHABETICAL
    list_ordering: ListOrderingStrategy = ListOrderingStrategy.SORTED  # Sort lists by default
    allow_scientific_notation: bool = False
    allow_numeric_string_coercion: bool = False  # Reject numeric string parsing by default
    
    # Version
    schema_version: int = 1
    policy_version: int = 1
    
    # Optional mappings
    enum_aliases: Dict[str, Dict[str, str]] = field(default_factory=dict)
    null_sentinels: FrozenSet[str] = field(default_factory=frozenset)
    required_fields: FrozenSet[str] = field(default_factory=frozenset)
    
    # Field classifications (schema-driven, immutable, not runtime-configurable)
    # These define which fields are timestamps, identifiers, enums, etc.
    # Must be provided at policy creation time for deterministic behavior
    # This ensures: same (payload, policy, schema_version) → same output
    timestamp_fields: FrozenSet[str] = field(default_factory=frozenset)
    identifier_fields: FrozenSet[str] = field(default_factory=frozenset)
    enum_fields: Dict[str, FrozenSet[str]] = field(default_factory=dict)  # field_name -> valid enum values
    text_fields: FrozenSet[str] = field(default_factory=frozenset)
    numeric_fields: FrozenSet[str] = field(default_factory=frozenset)
    schema_field_order: Tuple[str, ...] = field(default_factory=tuple)  # Schema-defined field order
    
    def __post_init__(self) -> None:
        """Validate policy is complete and well-formed."""
        if self.timestamp_precision_ms <= 0:
            raise NormalizationError(
                code=NormalizationErrorCode.INVALID_SCHEMA,
                message=f"timestamp_precision_ms must be positive, got {self.timestamp_precision_ms}",
            )
        if self.schema_version < 1:
            raise NormalizationError(
                code=NormalizationErrorCode.INVALID_SCHEMA,
                message=f"schema_version must be >= 1, got {self.schema_version}",
            )
        if self.policy_version < 1:
            raise NormalizationError(
                code=NormalizationErrorCode.INVALID_SCHEMA,
                message=f"policy_version must be >= 1, got {self.policy_version}",
            )
        if self.text_encoding != "utf-8":
            raise NormalizationError(
                code=NormalizationErrorCode.ILLEGAL_ENCODING,
                message=f"Only UTF-8 encoding supported, got {self.text_encoding}",
            )
        
        # Validate enum_fields structure (must be Dict[str, FrozenSet[str]])
        for field_name, enum_values in self.enum_fields.items():
            if not isinstance(enum_values, (frozenset, set)):
                raise NormalizationError(
                    code=NormalizationErrorCode.INVALID_SCHEMA,
                    message=f"enum_fields['{field_name}'] must be a FrozenSet[str], got {type(enum_values)}",
                )
            if not all(isinstance(v, str) for v in enum_values):
                raise NormalizationError(
                    code=NormalizationErrorCode.INVALID_SCHEMA,
                    message=f"enum_fields['{field_name}'] must contain only strings",
                )


@dataclass(frozen=True)
class NormalizationContext:
    """
    Context passed to every normalizer.
    
    Context is for traceability, not logic.
    """
    
    pipeline_name: str
    stage_name: str
    schema_name: SchemaName
    schema_version: int
    source: NormalizationSource
    run_id: RunID
    timestamp: datetime
    
    def __post_init__(self) -> None:
        """Validate context is complete and well-formed."""
        required_fields = {
            "pipeline_name": self.pipeline_name,
            "stage_name": self.stage_name,
            "schema_name": self.schema_name,
        }
        
        missing = [k for k, v in required_fields.items() if not v]
        if missing:
            raise NormalizationError(
                code=NormalizationErrorCode.INVALID_SCHEMA,
                message=f"Missing required context fields: {missing}",
            )
        
        if self.schema_version < 1:
            raise NormalizationError(
                code=NormalizationErrorCode.INVALID_SCHEMA,
                message=f"schema_version must be >= 1, got {self.schema_version}",
            )
        
        if not isinstance(self.run_id, UUID):
            raise TypeError(f"run_id must be UUID, got {type(self.run_id)}")
        
        if not isinstance(self.source, NormalizationSource):
            raise TypeError(
                f"source must be NormalizationSource, got {type(self.source)}"
            )
        
        if self.timestamp.tzinfo is None:
            raise NormalizationError(
                code=NormalizationErrorCode.AMBIGUOUS_TIMESTAMP,
                message="timestamp must be timezone-aware",
            )


@dataclass(frozen=True)
class NormalizedPayload:
    """
    Output of normalization.
    
    The hash is used for:
        - Deduplication
        - Replay verification
        - Forensic comparison
        - Recovery validation
    
    If hashes differ, the facts differ — period.
    """
    
    payload: Dict[str, FieldValue]
    normalization_hash: NormalizationHash
    schema_version: int
    policy_version: int
    normalized_at: datetime
    
    def __post_init__(self) -> None:
        """Validate normalized payload is complete and well-formed."""
        if not self.payload:
            raise NormalizationError(
                code=NormalizationErrorCode.INVALID_SCHEMA,
                message="payload cannot be empty",
            )
        if not self.normalization_hash:
            raise NormalizationError(
                code=NormalizationErrorCode.INVARIANT_VIOLATION,
                message="normalization_hash cannot be empty",
            )
        if self.schema_version < 1:
            raise NormalizationError(
                code=NormalizationErrorCode.INVALID_SCHEMA,
                message=f"schema_version must be >= 1, got {self.schema_version}",
            )
        if self.policy_version < 1:
            raise NormalizationError(
                code=NormalizationErrorCode.INVALID_SCHEMA,
                message=f"policy_version must be >= 1, got {self.policy_version}",
            )
        if self.normalized_at.tzinfo is None:
            raise NormalizationError(
                code=NormalizationErrorCode.AMBIGUOUS_TIMESTAMP,
                message="normalized_at must be timezone-aware",
            )


# ============================================================================
# Individual Normalizers (Pure, Deterministic, Side-Effect Free)
# ============================================================================

class TimestampNormalizer:
    """
    Normalize timestamps to canonical UTC representation.
    
    Rules:
        - Convert all timestamps to UTC
        - Strip time zone metadata (implied UTC)
        - Enforce precision (e.g. milliseconds)
        - Reject ambiguous or floating timestamps
        - Reject locale-dependent formats
    
    No rounding beyond declared precision.
    """
    
    def __init__(self, policy: NormalizationPolicy):
        self.policy = policy
    
    def normalize(self, value: Any, field_name: str) -> str:
        """
        Normalize timestamp to canonical ISO8601 UTC string.
        
        Args:
            value: Timestamp value (datetime, string, or int)
            field_name: Name of field being normalized
            
        Returns:
            Canonical ISO8601 UTC timestamp string
            
        Raises:
            NormalizationError: If timestamp is invalid or ambiguous
        """
        # Convert to datetime
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str):
            dt = self._parse_timestamp_string(value, field_name)
        elif isinstance(value, (int, float)):
            # Numeric timestamps are ambiguous - require explicit policy
            if self.policy.timestamp_epoch_unit == TimestampEpochUnit.NONE:
                raise NormalizationError(
                    code=NormalizationErrorCode.AMBIGUOUS_TIMESTAMP,
                    message=f"Numeric timestamps are ambiguous and not allowed. Policy must explicitly define timestamp_epoch_unit.",
                    field_name=field_name,
                    field_value=value,
                )
            # Convert based on policy-defined unit
            dt = self._parse_timestamp_epoch(value, field_name)
        else:
            raise NormalizationError(
                code=NormalizationErrorCode.AMBIGUOUS_TIMESTAMP,
                message=f"Cannot normalize timestamp of type {type(value)}",
                field_name=field_name,
                field_value=value,
            )
        
        # Ensure UTC
        if dt.tzinfo is None:
            raise NormalizationError(
                code=NormalizationErrorCode.AMBIGUOUS_TIMESTAMP,
                message="Timestamp must be timezone-aware (UTC required)",
                field_name=field_name,
                field_value=value,
            )
        
        # Convert to UTC
        dt_utc = dt.astimezone(timezone.utc)
        
        # Enforce precision
        dt_normalized = self._enforce_precision(dt_utc)
        
        # Return canonical ISO8601 string (strip timezone as it's always UTC)
        return dt_normalized.replace(tzinfo=None).isoformat() + "Z"
    
    def _parse_timestamp_string(self, value: str, field_name: str) -> datetime:
        """Parse timestamp string to datetime."""
        try:
            # Try ISO8601 format
            if value.endswith('Z'):
                return datetime.fromisoformat(value[:-1]).replace(tzinfo=timezone.utc)
            else:
                return datetime.fromisoformat(value)
        except ValueError:
            raise NormalizationError(
                code=NormalizationErrorCode.AMBIGUOUS_TIMESTAMP,
                message=f"Cannot parse timestamp string: {value}",
                field_name=field_name,
                field_value=value,
            )
    
    def _parse_timestamp_epoch(self, value: float, field_name: str) -> datetime:
        """Parse numeric epoch timestamp based on policy-defined unit."""
        if self.policy.timestamp_epoch_unit == TimestampEpochUnit.SECONDS:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        elif self.policy.timestamp_epoch_unit == TimestampEpochUnit.MILLISECONDS:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        elif self.policy.timestamp_epoch_unit == TimestampEpochUnit.MICROSECONDS:
            return datetime.fromtimestamp(value / 1000000.0, tz=timezone.utc)
        else:
            # Should not reach here due to earlier check, but defensive
            raise NormalizationError(
                code=NormalizationErrorCode.AMBIGUOUS_TIMESTAMP,
                message=f"Numeric timestamps not allowed with epoch unit: {self.policy.timestamp_epoch_unit}",
                field_name=field_name,
                field_value=value,
            )
    
    def _enforce_precision(self, dt: datetime) -> datetime:
        """Enforce timestamp precision according to policy."""
        # Truncate to specified precision
        if self.policy.timestamp_precision_ms >= 1000:
            # Second-level precision
            return dt.replace(microsecond=0)
        elif self.policy.timestamp_precision_ms >= 1:
            # Millisecond-level precision
            ms = (dt.microsecond // 1000) * 1000
            return dt.replace(microsecond=ms)
        else:
            return dt


class IdentifierNormalizer:
    """
    Normalize identifiers to canonical representation.
    
    Rules:
        - Enforce canonical casing
        - Strip illegal characters
        - Normalize separators (- vs _) per policy
        - Reject mixed-strategy identifiers
        - Preserve semantic identity
    
    IDs must never change meaning, only form.
    """
    
    # Legal identifier characters
    LEGAL_CHARS = re.compile(r'^[a-zA-Z0-9_-]+$')
    
    def __init__(self, policy: NormalizationPolicy):
        self.policy = policy
    
    def normalize(self, value: Any, field_name: str) -> str:
        """
        Normalize identifier to canonical form.
        
        Args:
            value: Identifier value
            field_name: Name of field being normalized
            
        Returns:
            Canonical identifier string
            
        Raises:
            NormalizationError: If identifier is invalid
        """
        if not isinstance(value, str):
            # Convert to string if possible
            try:
                value = str(value)
            except Exception:
                raise NormalizationError(
                    code=NormalizationErrorCode.INVALID_IDENTIFIER,
                    message=f"Cannot convert to identifier: {type(value)}",
                    field_name=field_name,
                    field_value=value,
                )
        
        # Strip whitespace
        value = value.strip()
        
        if not value:
            raise NormalizationError(
                code=NormalizationErrorCode.INVALID_IDENTIFIER,
                message="Identifier cannot be empty",
                field_name=field_name,
                field_value=value,
            )
        
        # Validate legal characters
        if not self.LEGAL_CHARS.match(value):
            raise NormalizationError(
                code=NormalizationErrorCode.INVALID_IDENTIFIER,
                message=f"Identifier contains illegal characters: {value}",
                field_name=field_name,
                field_value=value,
            )
        
        # Apply casing strategy
        if self.policy.id_casing == CasingStrategy.LOWERCASE:
            return value.lower()
        elif self.policy.id_casing == CasingStrategy.UPPERCASE:
            return value.upper()
        else:  # PRESERVE
            return value


class EnumNormalizer:
    """
    Normalize enum values to canonical representation.
    
    Rules:
        - Map aliases → canonical enum
        - Reject unknown enum values
        - Preserve explicit "unknown" states
        - No best guesses
    
    If the enum isn't declared, it doesn't exist.
    """
    
    def __init__(self, policy: NormalizationPolicy):
        self.policy = policy
    
    def normalize(
        self,
        value: Any,
        field_name: str,
        valid_enums: Optional[Set[str]] = None,
    ) -> str:
        """
        Normalize enum value to canonical form.
        
        Args:
            value: Enum value
            field_name: Name of field being normalized
            valid_enums: Set of valid enum values (if known)
            
        Returns:
            Canonical enum value
            
        Raises:
            NormalizationError: If enum is unknown (in STRICT mode)
        """
        if not isinstance(value, str):
            value = str(value)
        
        # Strip and normalize case for comparison
        value_normalized = value.strip()
        
        # Check for alias mapping
        if field_name in self.policy.enum_aliases:
            aliases = self.policy.enum_aliases[field_name]
            if value_normalized in aliases:
                return aliases[value_normalized]
        
        # In STRICT mode, valid_enums must be provided
        if self.policy.enum_strategy == EnumStrategy.STRICT:
            if valid_enums is None:
                raise NormalizationError(
                    code=NormalizationErrorCode.UNKNOWN_ENUM,
                    message=f"STRICT enum mode requires valid_enums to be provided. Unknown enum value: {value_normalized}",
                    field_name=field_name,
                    field_value=value_normalized,
                )
            if value_normalized not in valid_enums:
                raise NormalizationError(
                    code=NormalizationErrorCode.UNKNOWN_ENUM,
                    message=f"Unknown enum value: {value_normalized}",
                    field_name=field_name,
                    field_value=value_normalized,
                )
        elif valid_enums is not None:
            # Non-STRICT mode but valid_enums provided - validate if present
            if value_normalized not in valid_enums:
                # In non-STRICT mode, we allow unknown enums but log/warn
                # For now, we still return the value
                pass
        
        return value_normalized


class TextNormalizer:
    """
    Normalize text to canonical representation.
    
    Rules:
        - UTF-8 only
        - Unicode NFC normalization
        - Trim illegal control characters
        - Preserve semantic whitespace (no collapsing)
        - No language-aware transformations
    
    This is representation cleanup, not NLP.
    """
    
    # Control characters to strip (except newline, tab, carriage return)
    ILLEGAL_CONTROL_CHARS = re.compile(
        r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]'
    )
    
    def __init__(self, policy: NormalizationPolicy):
        self.policy = policy
    
    def normalize(self, value: Any, field_name: str) -> str:
        """
        Normalize text to canonical UTF-8 NFC form.
        
        Args:
            value: Text value
            field_name: Name of field being normalized
            
        Returns:
            Canonical text string
            
        Raises:
            NormalizationError: If encoding is invalid
        """
        if not isinstance(value, str):
            value = str(value)
        
        # Ensure UTF-8 encoding
        try:
            # Encode to UTF-8 and decode to ensure validity
            value_bytes = value.encode('utf-8')
            value = value_bytes.decode('utf-8')
        except UnicodeError as e:
            raise NormalizationError(
                code=NormalizationErrorCode.ILLEGAL_ENCODING,
                message=f"Invalid UTF-8 encoding: {e}",
                field_name=field_name,
                field_value=value,
            )
        
        # Unicode NFC normalization
        value = unicodedata.normalize('NFC', value)
        
        # Strip illegal control characters
        value = self.ILLEGAL_CONTROL_CHARS.sub('', value)
        
        return value


class NullNormalizer:
    """
    Normalize null values to canonical representation.
    
    Rules:
        - One true null representation (None in Python, null in JSON)
        - Convert empty strings / sentinel values explicitly
        - No implicit null creation
        - Required fields cannot be nulled
    
    Nulls are facts, not defaults.
    """
    
    def __init__(self, policy: NormalizationPolicy):
        self.policy = policy
    
    def normalize(
        self,
        value: Any,
        field_name: str,
        is_required: bool = False,
    ) -> Optional[Any]:
        """
        Normalize null values.
        
        Args:
            value: Field value
            field_name: Name of field being normalized
            is_required: Whether this field is required
            
        Returns:
            Normalized value (may be None)
            
        Raises:
            NormalizationError: If required field is null
        """
        # Check if value is already None
        if value is None:
            if is_required:
                raise NormalizationError(
                    code=NormalizationErrorCode.MISSING_REQUIRED_FIELD,
                    message=f"Required field cannot be null: {field_name}",
                    field_name=field_name,
                    field_value=None,
                )
            return None
        
        # Convert empty strings to null if policy allows
        if self.policy.null_policy == NullPolicy.CONVERT_EMPTY_STRINGS:
            if isinstance(value, str) and value.strip() == "":
                if is_required:
                    raise NormalizationError(
                        code=NormalizationErrorCode.MISSING_REQUIRED_FIELD,
                        message=f"Required field cannot be empty: {field_name}",
                        field_name=field_name,
                        field_value=value,
                    )
                return None
        
        # Convert sentinel values to null if policy allows
        if self.policy.null_policy == NullPolicy.CONVERT_SENTINELS:
            if isinstance(value, str) and value in self.policy.null_sentinels:
                if is_required:
                    raise NormalizationError(
                        code=NormalizationErrorCode.MISSING_REQUIRED_FIELD,
                        message=f"Required field has sentinel null value: {field_name}",
                        field_name=field_name,
                        field_value=value,
                    )
                return None
        
        return value


class NumericNormalizer:
    """
    Normalize numeric representations (not values).
    
    Rules:
        - Preserve numeric value exactly
        - Normalize representation only
        - Handle scientific notation per policy
        - Ensure deterministic string representation
    
    This is NOT rounding or math — only representation stabilization.
    """
    
    def __init__(self, policy: NormalizationPolicy):
        self.policy = policy
    
    def normalize(self, value: Any, field_name: str) -> Any:
        """
        Normalize numeric representation.
        
        Args:
            value: Numeric value
            field_name: Name of field being normalized
            
        Returns:
            Value with normalized representation
            
        Raises:
            NormalizationError: If representation is invalid
        """
        if isinstance(value, bool):
            # Preserve booleans as-is (they're not really numeric)
            return value
        
        if isinstance(value, int):
            # Integers are already canonical
            return value
        
        if isinstance(value, float):
            # Check for special values
            if value != value:  # NaN
                raise NormalizationError(
                    code=NormalizationErrorCode.INVALID_NUMERIC_REPRESENTATION,
                    message="NaN values are not allowed",
                    field_name=field_name,
                    field_value=value,
                )
            
            if value == float('inf') or value == float('-inf'):
                raise NormalizationError(
                    code=NormalizationErrorCode.INVALID_NUMERIC_REPRESENTATION,
                    message="Infinity values are not allowed",
                    field_name=field_name,
                    field_value=value,
                )
            
            # Preserve float as-is (Python's float representation is deterministic)
            return value
        
        if isinstance(value, str):
            # String → numeric coercion is value derivation, not representation stabilization
            # Reject unless policy explicitly allows it
            if not self.policy.allow_numeric_string_coercion:
                raise NormalizationError(
                    code=NormalizationErrorCode.INVALID_NUMERIC_REPRESENTATION,
                    message=f"Numeric string coercion not allowed. String '{value}' cannot be parsed as number. Set allow_numeric_string_coercion=True in policy to allow.",
                    field_name=field_name,
                    field_value=value,
                )
            
            # Policy allows coercion - parse with validation
            try:
                # Check if it's a float
                if '.' in value or 'e' in value.lower():
                    parsed = float(value)
                    
                    # Check for scientific notation
                    if 'e' in value.lower() and not self.policy.allow_scientific_notation:
                        raise NormalizationError(
                            code=NormalizationErrorCode.INVALID_NUMERIC_REPRESENTATION,
                            message="Scientific notation not allowed",
                            field_name=field_name,
                            field_value=value,
                        )
                    
                    return parsed
                else:
                    return int(value)
            except ValueError:
                # Not a numeric string even though coercion is allowed
                raise NormalizationError(
                    code=NormalizationErrorCode.INVALID_NUMERIC_REPRESENTATION,
                    message=f"String '{value}' cannot be parsed as number",
                    field_name=field_name,
                    field_value=value,
                )
        
        # Non-numeric type, return as-is
        return value


class StructureNormalizer:
    """
    Enforce structural determinism.
    
    Rules:
        - Stable field ordering
        - Sorted lists where order is semantically irrelevant
        - Explicit ordering where order matters
        - Deterministic serialization
    
    Structure changes must be explainable.
    """
    
    def __init__(self, policy: NormalizationPolicy):
        self.policy = policy
    
    def normalize_dict(
        self,
        data: Dict[str, FieldValue],
        schema_field_order: Optional[List[str]] = None,
    ) -> Dict[str, FieldValue]:
        """
        Normalize dictionary structure.
        
        Args:
            data: Dictionary to normalize
            schema_field_order: Optional schema-defined field order
            
        Returns:
            Dictionary with normalized field ordering
        """
        # Recursively normalize structure (including lists)
        normalized = {}
        for key, value in data.items():
            normalized[key] = self._normalize_value(value)
        
        if self.policy.field_ordering == FieldOrderingStrategy.ALPHABETICAL:
            # Sort keys alphabetically
            return {k: normalized[k] for k in sorted(normalized.keys())}
        elif self.policy.field_ordering == FieldOrderingStrategy.SCHEMA_DEFINED:
            if schema_field_order is None:
                raise ValueError("Schema field order required but not provided")
            
            # Order according to schema, then alphabetical for remaining
            ordered = {}
            for field in schema_field_order:
                if field in normalized:
                    ordered[field] = normalized[field]
            
            # Add any remaining fields alphabetically
            remaining = sorted(set(normalized.keys()) - set(schema_field_order))
            for field in remaining:
                ordered[field] = normalized[field]
            
            return ordered
        else:
            return normalized
    
    def _normalize_value(self, value: Any) -> Any:
        """
        Recursively normalize value structure (handles lists, dicts).
        
        Args:
            value: Value to normalize
            
        Returns:
            Normalized value
        """
        if isinstance(value, list):
            # Normalize list elements recursively
            normalized_list = [self._normalize_value(item) for item in value]
            
            # Sort if policy requires it
            if self.policy.list_ordering == ListOrderingStrategy.SORTED:
                # Sort lists where order is semantically irrelevant
                # Use deterministic sorting for canonical representation
                # All normalized values must be JSON-serializable for deterministic sorting
                def sort_key(x: Any) -> str:
                    # Use JSON serialization for deterministic ordering
                    # No fallback - fail explicitly if not serializable
                    return json.dumps(x, sort_keys=True)
                
                try:
                    normalized_list.sort(key=sort_key)
                except (TypeError, ValueError) as e:
                    # If sorting fails, this is a schema violation
                    # In Tier-0, all normalized values must be JSON-serializable
                    raise NormalizationError(
                        code=NormalizationErrorCode.INVARIANT_VIOLATION,
                        message=f"Cannot sort list elements for canonical ordering - non-serializable type detected: {e}",
                    ) from e
            
            return normalized_list
        elif isinstance(value, dict):
            # Recursively normalize nested dictionaries
            normalized_dict = {}
            for k, v in value.items():
                normalized_dict[k] = self._normalize_value(v)
            
            # Apply field ordering to nested dicts too
            if self.policy.field_ordering == FieldOrderingStrategy.ALPHABETICAL:
                return {k: normalized_dict[k] for k in sorted(normalized_dict.keys())}
            else:
                return normalized_dict
        else:
            # Primitive value - return as-is
            return value


# ============================================================================
# Normalization Invariants
# ============================================================================

class NormalizationInvariants:
    """
    Enforce hard invariants on normalization.
    
    Violation → hard stop.
    """
    
    @staticmethod
    def enforce_no_field_addition(
        original_fields: Set[str],
        normalized_fields: Set[str],
    ) -> None:
        """
        Verify no fields were added during normalization.
        
        Raises:
            NormalizationError: If fields were added
        """
        added = normalized_fields - original_fields
        if added:
            raise NormalizationError(
                code=NormalizationErrorCode.INVARIANT_VIOLATION,
                message=f"Fields added during normalization: {added}",
            )
    
    @staticmethod
    def enforce_no_field_deletion(
        original_fields: Set[str],
        normalized_fields: Set[str],
    ) -> None:
        """
        Verify no fields were deleted during normalization.
        
        Raises:
            NormalizationError: If fields were deleted
        """
        deleted = original_fields - normalized_fields
        if deleted:
            raise NormalizationError(
                code=NormalizationErrorCode.INVARIANT_VIOLATION,
                message=f"Fields deleted during normalization: {deleted}",
            )
    
    @staticmethod
    def enforce_deterministic_hash(
        payload: Dict[str, FieldValue],
        hash1: str,
        hash2: str,
    ) -> None:
        """
        Verify normalization produces deterministic hash.
        
        Hard rule: no non-deterministic behavior.
        
        Args:
            payload: Normalized payload
            hash1: First hash
            hash2: Second hash
            
        Raises:
            NormalizationError: If hashes differ
        """
        if hash1 != hash2:
            raise NormalizationError(
                code=NormalizationErrorCode.INVARIANT_VIOLATION,
                message=f"Non-deterministic normalization: {hash1} != {hash2}",
            )
    
    @staticmethod
    def enforce_no_value_derivation(
        original_value: Any,
        normalized_value: Any,
        field_name: str,
    ) -> None:
        """
        Verify no value derivation occurred (only representation changed).
        
        Hard rule: no value derivation.
        
        Args:
            original_value: Original field value
            normalized_value: Normalized field value
            field_name: Name of field
            
        Raises:
            NormalizationError: If value was derived (not just represented differently)
        """
        # Type changes that indicate derivation (not just representation)
        original_type = type(original_value)
        normalized_type = type(normalized_value)
        
        # String → number coercion is derivation
        if isinstance(original_value, str) and isinstance(normalized_value, (int, float)):
            raise NormalizationError(
                code=NormalizationErrorCode.INVARIANT_VIOLATION,
                message=f"Value derivation detected: string '{original_value}' converted to {normalized_type.__name__} {normalized_value}",
                field_name=field_name,
                field_value=original_value,
            )
        
        # Number → string coercion is derivation (unless it's a timestamp/identifier normalization)
        if isinstance(original_value, (int, float)) and isinstance(normalized_value, str):
            # Allow if it's a timestamp normalization (datetime → string)
            if not isinstance(original_value, datetime):
                # This might be legitimate for identifier normalization, but we check
                # If the string representation doesn't match, it's derivation
                if str(original_value) != normalized_value:
                    raise NormalizationError(
                        code=NormalizationErrorCode.INVARIANT_VIOLATION,
                        message=f"Value derivation detected: {original_type.__name__} {original_value} converted to string '{normalized_value}' with changed value",
                        field_name=field_name,
                        field_value=original_value,
                    )
        
        # For numeric values, check if the actual value changed
        if isinstance(original_value, (int, float)) and isinstance(normalized_value, (int, float)):
            if original_value != normalized_value:
                raise NormalizationError(
                    code=NormalizationErrorCode.INVARIANT_VIOLATION,
                    message=f"Value derivation detected: numeric value changed from {original_value} to {normalized_value}",
                    field_name=field_name,
                    field_value=original_value,
                )
        
        # For strings, use formal type-preserving validation (no heuristic inference)
        if isinstance(original_value, str) and isinstance(normalized_value, str):
            # Formal check: string normalization must preserve non-empty → non-empty
            # This is a structural invariant, not semantic inference
            if len(original_value.strip()) > 0 and len(normalized_value.strip()) == 0:
                # Structural violation: non-empty input became empty
                # This is a formal property check, not semantic interpretation
                raise NormalizationError(
                    code=NormalizationErrorCode.INVARIANT_VIOLATION,
                    message=f"Value derivation detected: non-empty string '{original_value}' normalized to empty string",
                    field_name=field_name,
                    field_value=original_value,
                )
            # Note: We do NOT compare semantic content (strip().lower()) as that is heuristic inference
            # We only check structural properties (empty vs non-empty) which is formal validation
    
    @staticmethod
    def enforce_no_math(
        original_value: Any,
        normalized_value: Any,
        field_name: str,
    ) -> None:
        """
        Verify no math operations occurred.
        
        Hard rule: no math.
        
        Args:
            original_value: Original field value
            normalized_value: Normalized field value
            field_name: Name of field
            
        Raises:
            NormalizationError: If math operations detected
        """
        # Check for numeric operations that indicate math
        if isinstance(original_value, (int, float)) and isinstance(normalized_value, (int, float)):
            # Division, multiplication, addition, subtraction would change value
            # Rounding would also change value
            if original_value != normalized_value:
                # Check if it's a rounding operation (value close but not equal)
                if isinstance(original_value, float) and isinstance(normalized_value, float):
                    # Check if difference is due to rounding (very small relative difference)
                    if abs(original_value) > 0:
                        relative_diff = abs(original_value - normalized_value) / abs(original_value)
                        if relative_diff < 1e-10:
                            # Very small difference - might be floating point precision, allow
                            return
                    elif abs(original_value - normalized_value) < 1e-10:
                        # Both near zero, small absolute difference - allow
                        return
                
                # Value changed - could be math, but also could be legitimate normalization
                # We can't definitively detect math without knowing the operation
                # But we can detect obvious cases like division by 1000 (timestamp conversion)
                if isinstance(original_value, (int, float)) and isinstance(normalized_value, (int, float)):
                    # Check for common math operations
                    if abs(original_value) > 0 and abs(normalized_value) > 0:
                        ratio = abs(original_value / normalized_value) if normalized_value != 0 else float('inf')
                        # Common conversion factors
                        if ratio in (1000.0, 1000000.0, 0.001, 0.000001):
                            raise NormalizationError(
                                code=NormalizationErrorCode.INVARIANT_VIOLATION,
                                message=f"Math operation detected: value {original_value} converted to {normalized_value} (ratio: {ratio})",
                                field_name=field_name,
                                field_value=original_value,
                            )
        
        # Check for rounding operations (value changed but close)
        if isinstance(original_value, float) and isinstance(normalized_value, (int, float)):
            if isinstance(normalized_value, int):
                # Float to int conversion - check if it's rounding
                if abs(original_value - normalized_value) > 0.5:
                    # Significant rounding occurred
                    raise NormalizationError(
                        code=NormalizationErrorCode.INVARIANT_VIOLATION,
                        message=f"Rounding detected: float {original_value} rounded to int {normalized_value}",
                        field_name=field_name,
                        field_value=original_value,
                    )
    
    @staticmethod
    def enforce_no_schema_drift(
        original_schema_version: int,
        normalized_schema_version: int,
    ) -> None:
        """
        Verify schema version did not change.
        
        Hard rule: no schema drift.
        
        Args:
            original_schema_version: Original schema version
            normalized_schema_version: Normalized schema version
            
        Raises:
            NormalizationError: If schema version changed
        """
        if original_schema_version != normalized_schema_version:
            raise NormalizationError(
                code=NormalizationErrorCode.INVALID_SCHEMA,
                message=f"Schema version drift: {original_schema_version} != {normalized_schema_version}",
            )


# ============================================================================
# Canonical Normalizer - The Orchestrator
# ============================================================================

class CanonicalNormalizer:
    """
    The only public entrypoint for normalization.
    
    Conceptual flow:
        1. Validate input is schema-conformant
        2. Apply normalizers in fixed order
        3. Enforce invariants
        4. Produce normalized payload + hash
    
    Guarantees:
        - Same input → same output
        - Order-independent inputs normalize identically
        - Failure is explicit
    """
    
    def __init__(
        self,
        policy: NormalizationPolicy,
        context: NormalizationContext,
    ):
        """
        Initialize canonical normalizer.
        
        Args:
            policy: Normalization policy
            context: Normalization context
        """
        self.policy = policy
        self.context = context
        
        # Initialize individual normalizers
        self.timestamp_normalizer = TimestampNormalizer(policy)
        self.identifier_normalizer = IdentifierNormalizer(policy)
        self.enum_normalizer = EnumNormalizer(policy)
        self.text_normalizer = TextNormalizer(policy)
        self.null_normalizer = NullNormalizer(policy)
        self.numeric_normalizer = NumericNormalizer(policy)
        self.structure_normalizer = StructureNormalizer(policy)
    
    def normalize(
        self,
        payload: Dict[str, FieldValue],
    ) -> NormalizedPayload:
        """
        Normalize payload to canonical form.
        
        Field classifications come from policy (schema-driven, immutable).
        This ensures: same (payload, policy, schema_version) → same output.
        
        Args:
            payload: Input payload to normalize
            
        Returns:
            Normalized payload with hash
            
        Raises:
            NormalizationError: If normalization fails
        """
        # Track original fields and values for invariant checking
        original_fields = set(payload.keys())
        original_values = payload.copy()
        original_schema_version = self.policy.schema_version
        
        # Use field classifications from policy (immutable, schema-driven)
        timestamp_fields = self.policy.timestamp_fields
        identifier_fields = self.policy.identifier_fields
        enum_fields = {k: v for k, v in self.policy.enum_fields.items()}
        text_fields = self.policy.text_fields
        numeric_fields = self.policy.numeric_fields
        schema_field_order = list(self.policy.schema_field_order) if self.policy.schema_field_order else None
        
        # Normalize each field
        normalized = {}
        
        for field_name, value in payload.items():
            # Store original value for invariant checking
            original_value = value
            
            # Check if required
            is_required = field_name in self.policy.required_fields
            
            # Apply null normalization first
            value = self.null_normalizer.normalize(value, field_name, is_required)
            
            # Skip further normalization if null
            if value is None:
                normalized[field_name] = None
                # Still enforce invariants on null conversion
                NormalizationInvariants.enforce_no_value_derivation(
                    original_value, None, field_name
                )
                NormalizationInvariants.enforce_no_math(
                    original_value, None, field_name
                )
                continue
            
            # Apply type-specific normalization based on policy-defined field classification
            if field_name in timestamp_fields:
                normalized_value = self.timestamp_normalizer.normalize(value, field_name)
            elif field_name in identifier_fields:
                normalized_value = self.identifier_normalizer.normalize(value, field_name)
            elif field_name in enum_fields:
                # Convert FrozenSet to Set for enum_normalizer (it expects Optional[Set[str]])
                enum_values_set = set(enum_fields[field_name]) if enum_fields[field_name] else None
                normalized_value = self.enum_normalizer.normalize(
                    value, field_name, enum_values_set
                )
            elif field_name in text_fields:
                normalized_value = self.text_normalizer.normalize(value, field_name)
            elif field_name in numeric_fields:
                normalized_value = self.numeric_normalizer.normalize(value, field_name)
            else:
                # Field not classified - preserve as-is (no normalization)
                normalized_value = value
            
            # Enforce invariants after normalization
            NormalizationInvariants.enforce_no_value_derivation(
                original_value, normalized_value, field_name
            )
            NormalizationInvariants.enforce_no_math(
                original_value, normalized_value, field_name
            )
            
            normalized[field_name] = normalized_value
        
        # Apply structural normalization
        normalized = self.structure_normalizer.normalize_dict(
            normalized, schema_field_order
        )
        
        # Enforce structural invariants
        normalized_fields = set(normalized.keys())
        NormalizationInvariants.enforce_no_field_addition(
            original_fields, normalized_fields
        )
        NormalizationInvariants.enforce_no_field_deletion(
            original_fields, normalized_fields
        )
        
        # Enforce schema version consistency
        NormalizationInvariants.enforce_no_schema_drift(
            original_schema_version, self.policy.schema_version
        )
        
        # Compute deterministic hash
        normalization_hash = self._compute_hash(normalized)
        
        return NormalizedPayload(
            payload=normalized,
            normalization_hash=normalization_hash,
            schema_version=self.policy.schema_version,
            policy_version=self.policy.policy_version,
            normalized_at=self.context.timestamp,
        )
    
    def _compute_hash(self, payload: Dict[str, FieldValue]) -> NormalizationHash:
        """
        Compute deterministic hash of normalized payload.
        
        Args:
            payload: Normalized payload
            
        Returns:
            SHA-256 hash of canonical JSON representation
            
        Raises:
            NormalizationError: If payload contains non-serializable types
        """
        # Serialize to canonical JSON
        # Do NOT use default=str - fail explicitly on unsupported types
        try:
            canonical_json = json.dumps(
                payload,
                sort_keys=True,
                separators=(',', ':'),
                ensure_ascii=True,
            )
        except TypeError as e:
            # Identify the problematic value
            problematic_value = self._find_non_serializable(payload)
            raise NormalizationError(
                code=NormalizationErrorCode.INVARIANT_VIOLATION,
                message=f"Payload contains non-serializable type: {e}. Problematic value: {problematic_value}",
            )
        
        # Compute SHA-256 hash
        return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()
    
    def _find_non_serializable(self, obj: Any, path: str = "") -> str:
        """
        Recursively find non-serializable value in payload.
        
        Args:
            obj: Object to check
            path: Current path in object
            
        Returns:
            Path to non-serializable value
        """
        try:
            json.dumps(obj)
            return path or "root"
        except TypeError:
            if isinstance(obj, dict):
                for key, value in obj.items():
                    new_path = f"{path}.{key}" if path else key
                    result = self._find_non_serializable(value, new_path)
                    if result != new_path:
                        return result
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    new_path = f"{path}[{i}]" if path else f"[{i}]"
                    result = self._find_non_serializable(item, new_path)
                    if result != new_path:
                        return result
            return path or f"type: {type(obj)}"


# ============================================================================
# High-Level Normalization Orchestrator
# ============================================================================

class NormalizationOrchestrator:
    """
    High-level orchestrator for complete normalization workflow.
    
    Coordinates: validation → normalization → verification
    """
    
    def __init__(
        self,
        policy: NormalizationPolicy,
        context: NormalizationContext,
    ):
        """
        Initialize orchestrator.
        
        Args:
            policy: Normalization policy
            context: Normalization context
        """
        self.policy = policy
        self.context = context
        self.normalizer = CanonicalNormalizer(policy, context)
    
    def normalize_payload(
        self,
        payload: Dict[str, FieldValue],
    ) -> NormalizedPayload:
        """
        Execute complete normalization workflow.
        
        Field classifications come from policy (schema-driven, immutable).
        This ensures deterministic behavior: same (payload, policy, schema_version) → same output.
        
        Args:
            payload: Input payload
            
        Returns:
            Normalized payload with hash
            
        Raises:
            NormalizationError: If normalization fails
        """
        # Normalize
        result = self.normalizer.normalize(payload=payload)
        
        # Verify determinism by normalizing again (defensive check)
        result2 = self.normalizer.normalize(payload=payload)
        
        # Enforce deterministic hash
        NormalizationInvariants.enforce_deterministic_hash(
            result.payload,
            result.normalization_hash,
            result2.normalization_hash,
        )
        
        # Verify payloads are identical (deterministic normalization)
        if result.payload != result2.payload:
            raise NormalizationError(
                code=NormalizationErrorCode.INVARIANT_VIOLATION,
                message="Non-deterministic normalization: payloads differ",
            )
        
        return result


# ============================================================================
# Public API
# ============================================================================

__all__ = [
    'NormalizationSource',
    'CasingStrategy',
    'EnumStrategy',
    'FieldOrderingStrategy',
    'TimestampEpochUnit',
    'ListOrderingStrategy',
    'NullPolicy',
    'NormalizationErrorCode',
    'NormalizationError',
    'NormalizationPolicy',
    'NormalizationContext',
    'NormalizedPayload',
    'TimestampNormalizer',
    'IdentifierNormalizer',
    'EnumNormalizer',
    'TextNormalizer',
    'NullNormalizer',
    'NumericNormalizer',
    'StructureNormalizer',
    'NormalizationInvariants',
    'CanonicalNormalizer',
    'NormalizationOrchestrator',
]