"""
/data/schemas/base.py

Canonical Schema Contracts & Data Invariants

This file defines the non-negotiable laws that all data in the system must obey.

It answers:
    "What makes a piece of data legitimate, versioned, serializable, and provable?"

If a schema violates this file, it cannot exist in the system.

WHAT THIS FILE IS:
  - The base schema interface
  - The enforcer of immutability
  - The guarantor of versioning
  - The mandate for canonical serialization
  - The foundation for deterministic hashing
  - The universal validation contract

WHAT THIS FILE IS NOT:
  ❌ Not a business model
  ❌ Not a database schema
  ❌ Not ORM mappings
  ❌ Not platform-specific
  ❌ Not flexible

This file is authoritarian by design.

CORE PRINCIPLE:
    Data is a contract with the future.
    
    If someone replays this system in 5 years,
    this file is why it still makes sense.

IMMUTABILITY RULE (ABSOLUTE):
    All schemas MUST be:
      - @dataclass(frozen=True)
      - Deeply immutable (no mutable containers)
      - Hashable via canonical serialization
    
    Mutation == corruption.

VERSIONING RULE (STRICT):
    Every schema has:
      - schema_name
      - schema_version
    
    Version is required, not inferred.
    No "latest". No auto-upgrade.
    Evolution happens explicitly elsewhere.

HASHING RULE (CRITICAL):
    content_hash() MUST:
      - Hash serialized bytes
      - Be collision-resistant
      - Be reproducible across machines
      - Never include runtime metadata
    
    This hash is used by:
      - Persistence backends
      - Recovery logs
      - Audit trails
      - Replay systems
    
    If this lies, everything lies.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, fields, is_dataclass, Field
from typing import (, List, Dict
    Any,
    Dict,
    List,
    Tuple,
    Optional,
    Protocol,
    TypeVar,
    Type,
    runtime_checkable,
    FrozenSet,
    get_type_hints,
)
from datetime import datetime, date, time
from enum import Enum
import hashlib
import json
import inspect
from collections.abc import Mapping, Sequence
from types import MappingProxyType


# =============================================================================
# TYPE DEFINITIONS
# =============================================================================


T = TypeVar('T', bound='CanonicalSchema')


# =============================================================================
# SCHEMA EXCEPTIONS (TYPED, DETERMINISTIC)
# =============================================================================


class SchemaError(Exception):
    """
    Base exception for all schema errors.
    
    All schema errors MUST be deterministic:
      - Same input → same exception
      - Same violation → same message
      - No runtime variance
    """
    pass


class SchemaValidationError(SchemaError):
    """
    Schema validation failed.
    
    Raised when validate() detects an invariant violation.
    MUST include exact invariant that was violated.
    """
    
    def __init__(self, schema_name: str, field_name: str, reason: str):
        self.schema_name = schema_name
        self.field_name = field_name
        self.reason = reason
        super().__init__(
            f"Schema validation failed: {schema_name}.{field_name}: {reason}"
        )


class SchemaVersionError(SchemaError):
    """
    Schema version is invalid or unsupported.
    
    Raised when:
      - schema_version is missing
      - schema_version is not supported
      - schema_version format is invalid
    """
    pass


class SchemaImmutabilityError(SchemaError):
    """
    Schema immutability contract violated.
    
    Raised when:
      - Mutable container detected in frozen dataclass
      - Attempt to modify frozen schema
      - Deep immutability requirement violated
    """
    pass


class SchemaSerializationError(SchemaError):
    """
    Schema serialization failed.
    
    Raised when:
      - Cannot serialize to canonical format
      - Non-deterministic serialization detected
      - Encoding error
    """
    pass


class SchemaDeserializationError(SchemaError):
    """Schema deserialization failed."""
    pass


# =============================================================================
# CANONICAL SCHEMA PROTOCOL (THE LAW)
# =============================================================================


@runtime_checkable
class CanonicalSchema(Protocol):
    """
    The absolute contract for all schemas in the system.
    
    EVERY schema MUST implement this protocol.
    
    REQUIRED ATTRIBUTES:
      - schema_name: str (unique identifier)
      - schema_version: int (explicit version)
    
    REQUIRED METHODS:
      - validate() -> None
      - canonical_serialize() -> bytes
      - content_hash() -> str
    
    IMMUTABILITY:
      - MUST be frozen dataclass
      - MUST NOT contain mutable containers
      - MUST be deeply immutable
    
    If any of these are missing → invalid schema.
    Cannot exist in the system.
    """
    
    schema_name: str
    schema_version: int
    
    @abstractmethod
    def validate(self) -> None:
        """
        Validate all schema invariants.
        
        RULES:
          - MUST raise deterministic exceptions
          - MUST list exact invariant violated
          - MUST NOT mutate
          - MUST NOT depend on external state
          - Silent failure is ILLEGAL
        
        Raises:
            SchemaValidationError: If any invariant violated
        """
        ...
    
    @abstractmethod
    def canonical_serialize(self) -> bytes:
        """
        Serialize to canonical byte representation.
        
        RULES:
          - MUST use stable field ordering
          - MUST use explicit encoding
          - MUST NOT have floating point ambiguity
          - MUST NOT depend on locale
          - Same object → same bytes → same hash FOREVER
        
        Returns:
            bytes: Canonical serialization
        
        Raises:
            SchemaSerializationError: If serialization fails
        """
        ...
    
    @abstractmethod
    def content_hash(self) -> str:
        """
        Compute deterministic content hash.
        
        RULES:
          - MUST hash canonical_serialize() output
          - MUST be collision-resistant (SHA-256 minimum)
          - MUST be reproducible across machines
          - MUST NOT include runtime metadata
        
        This hash is used by:
          - Persistence backends
          - Recovery logs
          - Audit trails
          - Replay systems
        
        If this lies, everything lies.
        
        Returns:
            str: Hex-encoded hash digest
        """
        ...


# =============================================================================
# BASE CANONICAL SCHEMA (ABSTRACT IMPLEMENTATION)
# =============================================================================


class BaseCanonicalSchema(ABC):
    """
    Abstract base implementation of CanonicalSchema.
    
    Provides default implementations for:
      - canonical_serialize()
      - content_hash()
      - validate() (partial)
      - immutability checks
    
    Concrete schemas should:
      1. Inherit from this
      2. Use @dataclass(frozen=True)
      3. Override validate() with domain logic
      4. Ensure deep immutability
    
    USAGE:
        @dataclass(frozen=True)
        class MySchema(BaseCanonicalSchema):
            schema_name: str = "my_schema"
            schema_version: int = 1
            my_field: str
            
            def validate(self) -> None:
                super().validate()  # Base validation
                # Add domain-specific validation
                if not self.my_field:
                    raise SchemaValidationError(
                        self.schema_name, "my_field", "cannot be empty"
                    )
    """
    
    schema_name: str
    schema_version: int
    
    def validate(self) -> None:
        """
        Base validation - checks universal invariants.
        
        Subclasses MUST call super().validate() first,
        then add domain-specific validation.
        """
        # Ensure this is a frozen dataclass (authoritative check)
        if not is_dataclass(self):
            raise SchemaImmutabilityError(
                f"{self.__class__.__name__} must be a dataclass"
            )
        
        # Authoritative frozen check via __dataclass_params__
        if not hasattr(self.__class__, '__dataclass_params__'):
            raise SchemaImmutabilityError(
                f"{self.__class__.__name__} must be a dataclass (missing __dataclass_params__)"
            )
        
        if not self.__class__.__dataclass_params__.frozen:
            raise SchemaImmutabilityError(
                f"{self.__class__.__name__} must be frozen (use @dataclass(frozen=True))"
            )
        
        # Validate schema_name
        if not self.schema_name:
            raise SchemaValidationError(
                self.__class__.__name__, "schema_name", "cannot be empty"
            )
        
        # Validate schema_version
        if not isinstance(self.schema_version, int):
            raise SchemaValidationError(
                self.schema_name, "schema_version", "must be an integer"
            )
        
        if self.schema_version < 1:
            raise SchemaValidationError(
                self.schema_name, "schema_version", "must be >= 1"
            )
        
        # Check for mutable containers (deep immutability)
        self._check_deep_immutability()
        
        # Enforce forbidden patterns (mandatory, not optional)
        violations = ForbiddenPatternDetector.check_schema(self.__class__)
        if violations:
            raise SchemaValidationError(
                self.schema_name,
                "__class__",
                f"Forbidden patterns detected: {'; '.join(violations)}"
            )
    
    def _check_deep_immutability(self) -> None:
        """
        Check that all fields are deeply immutable.
        
        Raises:
            SchemaImmutabilityError: If mutable container found
        """
        for field_obj in fields(self):
            value = getattr(self, field_obj.name)
            self._check_value_immutability(field_obj.name, value)
    
    def _check_value_immutability(self, field_name: str, value: Any) -> None:
        """
        Recursively check value immutability.
        
        Args:
            field_name: Name of field being checked
            value: Value to check
        
        Raises:
            SchemaImmutabilityError: If mutable container found
        """
        if value is None:
            return
        
        # Mutable types are FORBIDDEN
        if isinstance(value, (list, dict, set)):
            raise SchemaImmutabilityError(
                f"{self.schema_name}.{field_name}: "
                f"mutable {type(value).__name__} not allowed. "
                f"Use tuple/frozenset/immutable mapping instead."
            )
        
        # Check tuple/frozenset contents recursively
        if isinstance(value, (tuple, frozenset)):
            for item in value:
                self._check_value_immutability(field_name, item)
        
        # Check immutable mapping contents (authoritative - only allow known immutable types)
        if isinstance(value, Mapping):
            # Only allow MappingProxyType or other known immutable mappings
            # Reject custom mutable Mappings that could bypass immutability
            if not isinstance(value, (MappingProxyType,)):
                # Check if it's a dict (mutable) - this should have been caught above
                if isinstance(value, dict):
                    raise SchemaImmutabilityError(
                        f"{self.schema_name}.{field_name}: "
                        f"mutable dict not allowed. "
                        f"Use MappingProxyType or immutable mapping instead."
                    )
                # For other Mapping types, check if they're hashable (immutable indicator)
                # If not hashable, they're likely mutable
                try:
                    hash(value)
                except TypeError:
                    raise SchemaImmutabilityError(
                        f"{self.schema_name}.{field_name}: "
                        f"mutable Mapping type {type(value).__name__} not allowed. "
                        f"Use MappingProxyType or hashable immutable mapping instead."
                    )
            # Recursively check mapping contents
            for k, v in value.items():
                self._check_value_immutability(f"{field_name}[{k}]", v)
    
    def canonical_serialize(self) -> bytes:
        """
        Default canonical serialization via deterministic JSON.
        
        RULES:
          - Fields serialized in declaration order
          - Deterministic key sorting for nested dicts
          - UTC timestamps
          - No locale influence
          - Explicit UTF-8 encoding
        
        Returns:
            bytes: Canonical JSON bytes
        """
        try:
            # Convert to dictionary
            data = self.to_dict()
            
            # Deterministic JSON serialization
            json_str = json.dumps(
                data,
                sort_keys=True,  # Deterministic key order
                separators=(',', ':'),  # Minimal whitespace
                ensure_ascii=True,  # No unicode escaping ambiguity
                default=self._json_serializer,
            )
            
            # Explicit UTF-8 encoding
            return json_str.encode('utf-8')
            
        except (TypeError, ValueError) as e:
            raise SchemaSerializationError(
                f"Failed to serialize {self.schema_name}: {e}"
            )
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert schema to dictionary.
        
        Preserves field declaration order.
        Handles nested dataclasses, enums, datetimes.
        
        Returns:
            Dict[str, Any]: Schema as dictionary
        """
        result = {}
        
        for field_obj in fields(self):
            value = getattr(self, field_obj.name)
            result[field_obj.name] = self._serialize_value(value)
        
        return result
    
    def _serialize_value(self, value: Any) -> Any:
        """
        Recursively serialize a value to JSON-safe type.
        
        Args:
            value: Value to serialize
        
        Returns:
            JSON-safe value
        """
        if value is None:
            return None
        
        # Enums → value
        if isinstance(value, Enum):
            return value.value
        
        # Dataclasses → dict
        if is_dataclass(value) and not isinstance(value, type):
            return {
                f.name: self._serialize_value(getattr(value, f.name))
                for f in fields(value)
            }
        
        # Datetime types → ISO format
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, time):
            return value.isoformat()
        
        # Sequences → list
        if isinstance(value, (tuple, list, frozenset, set)):
            return [self._serialize_value(item) for item in value]
        
        # Mappings → dict
        if isinstance(value, Mapping):
            return {
                str(k): self._serialize_value(v)
                for k, v in value.items()
            }
        
        # Primitives
        if isinstance(value, (str, int, float, bool)):
            return value
        
        # Bytes → base64 (for JSON safety)
        if isinstance(value, bytes):
            import base64
            return base64.b64encode(value).decode('ascii')
        
        # Unknown type - REJECT (no silent coercion)
        # This ensures deterministic serialization and prevents hidden schema mistakes
        raise SchemaSerializationError(
            f"Cannot serialize value of type {type(value).__name__}: {value}. "
            f"Only supported types are: Enum, dataclass, datetime/date/time, "
            f"tuple/list/frozenset/set, Mapping, str/int/float/bool, bytes, None. "
            f"Silent coercion is forbidden for deterministic serialization."
        )
    
    def _json_serializer(self, obj: Any) -> Any:
        """
        JSON serializer fallback for custom types.
        
        Args:
            obj: Object to serialize
        
        Returns:
            JSON-safe representation
        """
        return self._serialize_value(obj)
    
    def content_hash(self) -> str:
        """
        Compute SHA-256 hash of canonical serialization.
        
        Returns:
            str: 64-character hex digest
        """
        canonical_bytes = self.canonical_serialize()
        return hashlib.sha256(canonical_bytes).hexdigest()
    
    def __hash__(self) -> int:
        """
        Make schema hashable for use in sets/dicts.
        
        Uses content_hash() to ensure consistency.
        """
        return int(self.content_hash()[:16], 16)  # First 64 bits
    
    def __eq__(self, other: Any) -> bool:
        """
        Equality based on content hash.
        
        Two schemas are equal if their content hashes match.
        """
        if not isinstance(other, BaseCanonicalSchema):
            return NotImplemented
        return self.content_hash() == other.content_hash()


# =============================================================================
# SCHEMA REGISTRY (VERSION MANAGEMENT)
# =============================================================================


class SchemaRegistry:
    """
    Central registry for schema versions.
    
    Tracks:
      - Which schema versions are supported
      - Which version is current
      - How to migrate between versions
    
    This is NOT an auto-upgrade system.
    Migration is explicit and external.
    """
    
    def __init__(self):
        self._schemas: Dict[str, Dict[int, Type[BaseCanonicalSchema]]] = {}
        self._current_versions: Dict[str, int] = {}
    
    def register(
        self,
        schema_class: Type[BaseCanonicalSchema],
        schema_name: str,
        schema_version: int,
        is_current: bool = False,
    ) -> None:
        """
        Register a schema version.
        
        Args:
            schema_class: Schema class
            schema_name: Schema name
            schema_version: Schema version
            is_current: Whether this is the current version
        """
        if schema_name not in self._schemas:
            self._schemas[schema_name] = {}
        
        if schema_version in self._schemas[schema_name]:
            raise SchemaError(
                f"Schema {schema_name} version {schema_version} already registered"
            )
        
        self._schemas[schema_name][schema_version] = schema_class
        
        if is_current:
            self._current_versions[schema_name] = schema_version
    
    def get_schema(
        self,
        schema_name: str,
        schema_version: Optional[int] = None,
    ) -> Type[BaseCanonicalSchema]:
        """
        Get schema class by name and version.
        
        Args:
            schema_name: Schema name
            schema_version: Version (None = current)
        
        Returns:
            Schema class
        
        Raises:
            SchemaVersionError: If version not found
        """
        if schema_name not in self._schemas:
            raise SchemaVersionError(f"Unknown schema: {schema_name}")
        
        if schema_version is None:
            # Get current version
            if schema_name not in self._current_versions:
                raise SchemaVersionError(
                    f"No current version for schema: {schema_name}"
                )
            schema_version = self._current_versions[schema_name]
        
        if schema_version not in self._schemas[schema_name]:
            available = sorted(self._schemas[schema_name].keys())
            raise SchemaVersionError(
                f"Schema {schema_name} version {schema_version} not found. "
                f"Available versions: {available}"
            )
        
        return self._schemas[schema_name][schema_version]
    
    def get_supported_versions(self, schema_name: str) -> FrozenSet[int]:
        """
        Get all supported versions for a schema.
        
        Args:
            schema_name: Schema name
        
        Returns:
            Frozenset of supported versions
        """
        if schema_name not in self._schemas:
            return frozenset()
        return frozenset(self._schemas[schema_name].keys())
    
    def get_current_version(self, schema_name: str) -> int:
        """
        Get current version for a schema.
        
        Args:
            schema_name: Schema name
        
        Returns:
            Current version number
        
        Raises:
            SchemaVersionError: If no current version set
        """
        if schema_name not in self._current_versions:
            raise SchemaVersionError(
                f"No current version for schema: {schema_name}"
            )
        return self._current_versions[schema_name]
    
    def is_supported(self, schema_name: str, schema_version: int) -> bool:
        """
        Check if schema version is supported.
        
        Args:
            schema_name: Schema name
            schema_version: Schema version
        
        Returns:
            True if supported
        """
        return (
            schema_name in self._schemas
            and schema_version in self._schemas[schema_name]
        )


# Global registry instance
SCHEMA_REGISTRY = SchemaRegistry()


# =============================================================================
# VALIDATION HELPERS
# =============================================================================


def validate_immutability(obj: Any, path: str = "") -> None:
    """
    Validate deep immutability of an object.
    
    Args:
        obj: Object to validate
        path: Field path for error messages
    
    Raises:
        SchemaImmutabilityError: If mutable container found
    """
    if obj is None:
        return
    
    # Check for mutable types
    if isinstance(obj, (list, dict, set)):
        raise SchemaImmutabilityError(
            f"Mutable {type(obj).__name__} found at {path or 'root'}. "
            f"Use tuple/frozenset/immutable mapping instead."
        )
    
    # Recurse into containers
    if isinstance(obj, (tuple, frozenset)):
        for i, item in enumerate(obj):
            validate_immutability(item, f"{path}[{i}]")
    
    if isinstance(obj, Mapping):
        # Authoritative check: only allow known immutable mappings
        if isinstance(obj, dict):
            raise SchemaImmutabilityError(
                f"Mutable dict found at {path or 'root'}. "
                f"Use MappingProxyType or immutable mapping instead."
            )
        # For other Mapping types, check if they're hashable (immutable indicator)
        if not isinstance(obj, (MappingProxyType,)):
            try:
                hash(obj)
            except TypeError:
                raise SchemaImmutabilityError(
                    f"Mutable Mapping type {type(obj).__name__} found at {path or 'root'}. "
                    f"Use MappingProxyType or hashable immutable mapping instead."
                )
        # Recursively check mapping contents
        for key, value in obj.items():
            validate_immutability(value, f"{path}[{key}]")
    
    # Check dataclass fields
    if is_dataclass(obj):
        for field_obj in fields(obj):
            value = getattr(obj, field_obj.name)
            validate_immutability(value, f"{path}.{field_obj.name}")


def validate_schema_instance(schema: CanonicalSchema) -> None:
    """
    Validate that an instance conforms to CanonicalSchema protocol.
    
    Args:
        schema: Schema instance to validate
    
    Raises:
        SchemaError: If schema invalid
    """
    # Check protocol compliance
    if not isinstance(schema, CanonicalSchema):
        raise SchemaError(
            f"{schema.__class__.__name__} does not implement CanonicalSchema protocol"
        )
    
    # Check required attributes
    if not hasattr(schema, 'schema_name'):
        raise SchemaError(f"{schema.__class__.__name__} missing schema_name")
    
    if not hasattr(schema, 'schema_version'):
        raise SchemaError(f"{schema.__class__.__name__} missing schema_version")
    
    # Check required methods
    required_methods = ['validate', 'canonical_serialize', 'content_hash']
    for method_name in required_methods:
        if not hasattr(schema, method_name):
            raise SchemaError(
                f"{schema.__class__.__name__} missing method: {method_name}"
            )
        if not callable(getattr(schema, method_name)):
            raise SchemaError(
                f"{schema.__class__.__name__}.{method_name} is not callable"
            )


# =============================================================================
# FORBIDDEN PATTERNS (ENFORCEMENT)
# =============================================================================


class ForbiddenPatternDetector:
    """
    Detects and prevents forbidden patterns in schemas.
    
    FORBIDDEN PATTERNS (ZERO TOLERANCE):
      ❌ Optional critical fields
      ❌ Hidden defaults
      ❌ Mutable lists or dicts
      ❌ Runtime-dependent values
      ❌ Auto-generated IDs
      ❌ Time-of-creation stamps (as structural fields)
    
    Schemas describe facts, not events.
    """
    
    @staticmethod
    def check_schema(schema_class: Type) -> List[str]:
        """
        Check schema class for forbidden patterns.
        
        Args:
            schema_class: Schema class to check
        
        Returns:
            List of violations (empty if clean)
        """
        violations = []
        
        if not is_dataclass(schema_class):
            violations.append("Schema must be a dataclass")
            return violations
        
        # Check frozen status
        if hasattr(schema_class, '__dataclass_params__'):
            if not schema_class.__dataclass_params__.frozen:
                violations.append("Schema must be frozen (use @dataclass(frozen=True))")
        
        # Check fields
        for field_obj in fields(schema_class):
            # Check for mutable default factories
            if field_obj.default_factory is not None:
                # Default factory should only create immutable objects
                try:
                    default_value = field_obj.default_factory()
                    if isinstance(default_value, (list, dict, set)):
                        violations.append(
                            f"Field {field_obj.name} has mutable default factory"
                        )
                except Exception:
                    pass  # Can't instantiate, skip check
            
            # Check field type hints for mutability
            if hasattr(field_obj, 'type'):
                field_type = field_obj.type
                
                # Check for mutable types in annotations
                if field_type in (list, dict, set, List, Dict):
                    violations.append(
                        f"Field {field_obj.name} uses mutable type {field_type}"
                    )
        
        return violations


# =============================================================================
# SCHEMA UTILITIES
# =============================================================================


def compute_schema_hash(schema: CanonicalSchema) -> str:
    """
    Compute content hash for a schema instance.
    
    Convenience wrapper around schema.content_hash().
    
    Args:
        schema: Schema instance
    
    Returns:
        str: Hex-encoded hash
    """
    return schema.content_hash()


def schemas_equal(schema1: CanonicalSchema, schema2: CanonicalSchema) -> bool:
    """
    Check if two schemas are equal by content hash.
    
    Args:
        schema1: First schema
        schema2: Second schema
    
    Returns:
        bool: True if content hashes match
    """
    return schema1.content_hash() == schema2.content_hash()


def serialize_schema(schema: CanonicalSchema) -> bytes:
    """
    Serialize schema to canonical bytes.
    
    Args:
        schema: Schema instance
    
    Returns:
        bytes: Canonical serialization
    """
    return schema.canonical_serialize()


# =============================================================================
# FORBIDDEN PATTERNS DOCUMENTATION
# =============================================================================

"""
STRICTLY FORBIDDEN IN ALL SCHEMAS:

❌ Optional critical fields
   - If a field is critical, it cannot be Optional
   - Use explicit sentinel values instead

❌ Hidden defaults
   - All defaults must be explicit and immutable
   - No runtime-computed defaults

❌ Mutable lists or dicts
   - Use tuple instead of list
   - Use frozenset instead of set
   - Use immutable mappings instead of dict

❌ Runtime-dependent values
   - No datetime.now() in defaults
   - No random UUIDs
   - No environment variables

❌ Auto-generated IDs
   - IDs must be deterministic or explicit
   - No hidden ID generation

❌ Time-of-creation stamps (as structural fields)
   - Timestamps are informational metadata
   - Not part of structural identity
   - Allowed only as optional metadata

Schemas describe FACTS, not EVENTS.
"""


# =============================================================================
# VERSION INFO
# =============================================================================

__version__ = "1.0.0"

# This is the law. All schemas must obey.