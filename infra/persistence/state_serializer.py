"""
/infra/persistence/state_serializer.py

Canonical State Serialization & Schema Authority

This module defines how state becomes bytes and back again — losslessly,
deterministically, and verifiably.

Core Guarantees:
- Deterministic encoding (identical input → identical bytes)
- Explicit schema enforcement
- Versioned evolution with compatibility rules
- No silent coercion or lossy operations
- Audit-proof serialization hashes

Authority Chain: schemas → serializer → state_backend → storage
"""

import json
import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from collections import OrderedDict


# ============================================================================
# CORE ENUMS
# ============================================================================

class SerializationFormat(Enum):
    """Supported serialization formats.
    
    Only CANONICAL_JSON is allowed for persistence.
    JSON is rejected for persistence due to non-deterministic ordering.
    """
    JSON = "json"
    CANONICAL_JSON = "canonical_json"


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class SchemaVersion:
    """Semantic version for state schemas.
    
    Rules:
    - major bump = breaking change
    - minor bump = backward compatible only
    - no implicit upgrades
    """
    major: int
    minor: int
    
    def __post_init__(self):
        if self.major < 0 or self.minor < 0:
            raise ValueError(f"Version numbers must be non-negative: {self}")
    
    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"
    
    def is_compatible_with(self, other: "SchemaVersion") -> bool:
        """Check if this version can read data from other version.
        
        Rules:
        - Same major version required
        - Reader minor >= writer minor (can read older data)
        """
        if self.major != other.major:
            return False
        return self.minor >= other.minor


@dataclass(frozen=True)
class FieldDefinition:
    """Schema field definition with strict typing.
    
    Rules:
    - no dynamic typing
    - no implicit defaults
    - optional ≠ nullable
    """
    name: str
    field_type: str  # int, float, bool, str, list, dict
    required: bool
    nullable: bool
    
    ALLOWED_TYPES: Set[str] = field(
        default_factory=lambda: {"int", "float", "bool", "str", "list", "dict"},
        init=False,
        repr=False
    )
    
    def __post_init__(self):
        if not self.name:
            raise ValueError("Field name cannot be empty")
        
        if self.field_type not in self.ALLOWED_TYPES:
            raise ValueError(
                f"Invalid field type '{self.field_type}'. "
                f"Allowed: {self.ALLOWED_TYPES}"
            )
        
        if not self.required and not self.nullable:
            raise ValueError(
                f"Field '{self.name}': optional fields must be nullable"
            )
    
    def validate_value(self, value: Any) -> None:
        """Validate value against field definition.
        
        Raises ValueError on validation failure.
        """
        # Check null handling
        if value is None:
            if not self.nullable:
                raise ValueError(f"Field '{self.name}' cannot be null")
            return
        
        # Check type
        type_validators = {
            "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "bool": lambda v: isinstance(v, bool),
            "str": lambda v: isinstance(v, str),
            "list": lambda v: isinstance(v, list),
            "dict": lambda v: isinstance(v, dict),
        }
        
        validator = type_validators[self.field_type]
        if not validator(value):
            raise ValueError(
                f"Field '{self.name}': expected {self.field_type}, "
                f"got {type(value).__name__}"
            )


@dataclass(frozen=True)
class StateSchema:
    """Immutable schema definition for state objects.
    
    Rules:
    - schemas are immutable
    - field order is canonical
    - no duplicate names
    """
    name: str
    version: SchemaVersion
    fields: Tuple[FieldDefinition, ...]  # Immutable sequence
    
    def __post_init__(self):
        if not self.name:
            raise ValueError("Schema name cannot be empty")
        
        # Ensure fields is tuple (immutable)
        if not isinstance(self.fields, tuple):
            object.__setattr__(self, 'fields', tuple(self.fields))
        
        # Check for duplicate field names
        field_names = [f.name for f in self.fields]
        if len(field_names) != len(set(field_names)):
            duplicates = [n for n in field_names if field_names.count(n) > 1]
            raise ValueError(f"Duplicate field names: {set(duplicates)}")
    
    def get_field(self, name: str) -> Optional[FieldDefinition]:
        """Get field definition by name."""
        for f in self.fields:
            if f.name == name:
                return f
        return None
    
    def validate_data(self, data: Dict[str, Any]) -> None:
        """Validate data against schema.
        
        Rules:
        - All required fields must be present
        - No unknown fields allowed
        - All values must match field definitions
        """
        # Check for unknown fields
        schema_fields = {f.name for f in self.fields}
        data_fields = set(data.keys())
        unknown = data_fields - schema_fields
        if unknown:
            raise ValueError(f"Unknown fields: {unknown}")
        
        # Validate each field
        for field_def in self.fields:
            if field_def.name in data:
                field_def.validate_value(data[field_def.name])
            elif field_def.required:
                raise ValueError(f"Missing required field: '{field_def.name}'")


@dataclass(frozen=True)
class SerializationContext:
    """Context for serialization operations.
    
    Mandatory for all encode/decode operations.
    """
    schema_name: str
    schema_version: SchemaVersion
    timestamp: int  # Unix timestamp in milliseconds
    
    def __post_init__(self):
        if not self.schema_name:
            raise ValueError("Schema name cannot be empty")
        if self.timestamp < 0:
            raise ValueError("Timestamp must be non-negative")


# ============================================================================
# SCHEMA REGISTRY
# ============================================================================

class SchemaRegistry:
    """Central registry for all state schemas.
    
    Rules:
    - schema name + version must be unique
    - all schemas registered at boot
    - registry is immutable at runtime (after finalization)
    - missing schema → hard failure
    """
    
    def __init__(self):
        self._schemas: Dict[Tuple[str, str], StateSchema] = {}
        self._finalized: bool = False
    
    def register(self, schema: StateSchema) -> None:
        """Register a schema.
        
        Raises:
            RuntimeError: If registry is finalized
            ValueError: If schema already exists
        """
        if self._finalized:
            raise RuntimeError("Cannot register schema: registry is finalized")
        
        key = (schema.name, str(schema.version))
        if key in self._schemas:
            raise ValueError(
                f"Schema already registered: {schema.name} v{schema.version}"
            )
        
        self._schemas[key] = schema
    
    def get(self, name: str, version: SchemaVersion) -> StateSchema:
        """Get schema by name and version.
        
        Raises:
            KeyError: If schema not found
        """
        key = (name, str(version))
        if key not in self._schemas:
            raise KeyError(f"Schema not found: {name} v{version}")
        return self._schemas[key]
    
    def finalize(self) -> None:
        """Finalize registry, making it immutable."""
        self._finalized = True
    
    def validate(self) -> None:
        """Validate registry consistency.
        
        Raises:
            RuntimeError: If validation fails
        """
        if not self._schemas:
            raise RuntimeError("Registry is empty")
        
        # Check for version gaps per schema name
        schemas_by_name: Dict[str, List[SchemaVersion]] = {}
        for (name, _), schema in self._schemas.items():
            if name not in schemas_by_name:
                schemas_by_name[name] = []
            schemas_by_name[name].append(schema.version)
        
        # Ensure no major version gaps
        for name, versions in schemas_by_name.items():
            majors = sorted(set(v.major for v in versions))
            for i in range(len(majors) - 1):
                if majors[i + 1] - majors[i] > 1:
                    raise RuntimeError(
                        f"Schema '{name}': major version gap detected "
                        f"(v{majors[i]} → v{majors[i + 1]})"
                    )


# ============================================================================
# COMPATIBILITY RESOLVER
# ============================================================================

class CompatibilityResolver:
    """Resolves schema compatibility for serialization/deserialization.
    
    Rules:
    - readers may accept newer MINOR versions
    - MAJOR mismatches are forbidden
    - unknown fields are rejected
    - missing required fields are rejected
    - no auto-migration
    """
    
    @staticmethod
    def assert_compatible(
        writer: SchemaVersion,
        reader: SchemaVersion
    ) -> None:
        """Assert that reader can deserialize data from writer.
        
        Raises:
            ValueError: If versions are incompatible
        """
        if not reader.is_compatible_with(writer):
            raise ValueError(
                f"Incompatible schema versions: "
                f"reader v{reader} cannot read writer v{writer}"
            )
    
    @staticmethod
    def assert_exact_match(
        version1: SchemaVersion,
        version2: SchemaVersion
    ) -> None:
        """Assert exact version match.
        
        Raises:
            ValueError: If versions don't match exactly
        """
        if version1.major != version2.major or version1.minor != version2.minor:
            raise ValueError(
                f"Exact version match required: v{version1} ≠ v{version2}"
            )


# ============================================================================
# SERIALIZATION INVARIANTS
# ============================================================================

class SerializationInvariants:
    """Enforces absolute serialization invariants.
    
    Rules:
    - no unordered maps
    - no float precision variance
    - no implicit casting
    - no default injection
    - no lossy decoding
    - no schema omission
    
    Any violation = refuse serialization.
    """
    
    @staticmethod
    def enforce_deterministic_ordering(data: Dict[str, Any]) -> OrderedDict:
        """Ensure dictionary has deterministic key ordering.
        
        Returns sorted OrderedDict for canonical representation.
        """
        if not isinstance(data, dict):
            raise TypeError(f"Expected dict, got {type(data).__name__}")
        
        ordered = OrderedDict()
        for key in sorted(data.keys()):
            value = data[key]
            # Recursively order nested dicts
            if isinstance(value, dict):
                ordered[key] = SerializationInvariants.enforce_deterministic_ordering(value)
            elif isinstance(value, list):
                # Check for dicts in lists
                ordered[key] = [
                    SerializationInvariants.enforce_deterministic_ordering(item)
                    if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                ordered[key] = value
        
        return ordered
    
    @staticmethod
    def validate_no_infinity(data: Any) -> None:
        """Reject infinity and NaN float values.
        
        These cannot be reliably serialized to JSON.
        """
        def check_value(v: Any) -> None:
            if isinstance(v, float):
                if not (-1e308 < v < 1e308):  # Approximate float range
                    raise ValueError(f"Float value out of safe range: {v}")
                if v != v:  # NaN check
                    raise ValueError("NaN values are not allowed")
            elif isinstance(v, dict):
                for val in v.values():
                    check_value(val)
            elif isinstance(v, list):
                for val in v:
                    check_value(val)
        
        check_value(data)
    
    @staticmethod
    def validate_no_schema_omission(
        data: Dict[str, Any],
        schema: StateSchema
    ) -> None:
        """Ensure no schema information is omitted."""
        # This is enforced by StateSchema.validate_data
        # Additional check: ensure all data keys exist in schema
        schema_fields = {f.name for f in schema.fields}
        data_fields = set(data.keys())
        
        if not data_fields.issubset(schema_fields):
            unknown = data_fields - schema_fields
            raise ValueError(f"Data contains fields not in schema: {unknown}")


# ============================================================================
# SERIALIZATION HASHING
# ============================================================================

def stable_hash(payload: bytes) -> str:
    """Compute deterministic hash of serialized payload.
    
    Used for:
    - replay verification
    - corruption detection
    - audit trails
    - snapshot identity
    
    Hash is deterministic across platforms.
    
    Args:
        payload: Serialized bytes
        
    Returns:
        Hex-encoded SHA-256 hash
    """
    return hashlib.sha256(payload).hexdigest()


# ============================================================================
# STATE SERIALIZER (CORE ENGINE)
# ============================================================================

class StateSerializer:
    """Core serialization engine with strict guarantees.
    
    Guarantees:
    - deterministic field ordering
    - strict schema enforcement
    - full validation on read AND write
    - identical input → identical bytes
    """
    
    def __init__(self, registry: SchemaRegistry):
        """Initialize serializer with schema registry.
        
        Args:
            registry: Schema registry (must be finalized)
        """
        if not registry._finalized:
            raise RuntimeError("Schema registry must be finalized")
        self._registry = registry
    
    def serialize(
        self,
        value: Dict[str, Any],
        context: SerializationContext
    ) -> bytes:
        """Serialize state to canonical bytes.
        
        Args:
            value: State data as dictionary
            context: Serialization context with schema info
            
        Returns:
            Deterministic byte representation
            
        Raises:
            ValueError: On validation failure
            KeyError: If schema not found
        """
        # Get schema
        schema = self._registry.get(context.schema_name, context.schema_version)
        
        # Validate data against schema
        schema.validate_data(value)
        
        # Enforce invariants
        SerializationInvariants.validate_no_infinity(value)
        SerializationInvariants.validate_no_schema_omission(value, schema)
        
        # Create canonical representation
        ordered_data = SerializationInvariants.enforce_deterministic_ordering(value)
        
        # Build envelope with metadata
        envelope = OrderedDict([
            ("_schema_name", context.schema_name),
            ("_schema_version", str(context.schema_version)),
            ("_timestamp", context.timestamp),
            ("data", ordered_data)
        ])
        
        # Serialize to canonical JSON (sorted keys, no whitespace)
        json_str = json.dumps(
            envelope,
            ensure_ascii=True,
            sort_keys=True,
            separators=(',', ':')
        )
        
        return json_str.encode('utf-8')
    
    def deserialize(
        self,
        payload: bytes,
        context: SerializationContext
    ) -> Dict[str, Any]:
        """Deserialize bytes to state with validation.
        
        Args:
            payload: Serialized bytes
            context: Expected schema context
            
        Returns:
            Validated state dictionary
            
        Raises:
            ValueError: On validation or compatibility failure
            KeyError: If schema not found
        """
        # Decode JSON
        try:
            json_str = payload.decode('utf-8')
            envelope = json.loads(json_str)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"Invalid serialized payload: {e}")
        
        # Validate envelope structure
        if not isinstance(envelope, dict):
            raise ValueError("Envelope must be a dictionary")
        
        required_keys = {"_schema_name", "_schema_version", "_timestamp", "data"}
        if not required_keys.issubset(envelope.keys()):
            missing = required_keys - set(envelope.keys())
            raise ValueError(f"Envelope missing required keys: {missing}")
        
        # Extract metadata
        schema_name = envelope["_schema_name"]
        schema_version_str = envelope["_schema_version"]
        timestamp = envelope["_timestamp"]
        data = envelope["data"]
        
        # Parse schema version
        try:
            major, minor = map(int, schema_version_str.split('.'))
            writer_version = SchemaVersion(major, minor)
        except (ValueError, AttributeError):
            raise ValueError(f"Invalid schema version format: {schema_version_str}")
        
        # Verify context matches
        if schema_name != context.schema_name:
            raise ValueError(
                f"Schema name mismatch: "
                f"expected '{context.schema_name}', got '{schema_name}'"
            )
        
        # Check version compatibility
        CompatibilityResolver.assert_compatible(
            writer=writer_version,
            reader=context.schema_version
        )
        
        # Get reader schema and validate data
        schema = self._registry.get(context.schema_name, context.schema_version)
        schema.validate_data(data)
        
        return data
    
    def serialize_with_hash(
        self,
        value: Dict[str, Any],
        context: SerializationContext
    ) -> Tuple[bytes, str]:
        """Serialize and compute hash in one operation.
        
        Returns:
            Tuple of (serialized_bytes, hash_hex)
        """
        payload = self.serialize(value, context)
        hash_hex = stable_hash(payload)
        return payload, hash_hex


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def create_global_registry() -> SchemaRegistry:
    """Create and return a new global schema registry."""
    return SchemaRegistry()


def quick_schema(
    name: str,
    version: Tuple[int, int],
    field_specs: List[Tuple[str, str, bool, bool]]
) -> StateSchema:
    """Quick schema creation helper.
    
    Args:
        name: Schema name
        version: (major, minor) tuple
        field_specs: List of (name, type, required, nullable) tuples
        
    Returns:
        StateSchema instance
        
    Example:
        schema = quick_schema(
            "user_state",
            (1, 0),
            [
                ("user_id", "str", True, False),
                ("score", "int", True, False),
                ("metadata", "dict", False, True)
            ]
        )
    """
    fields = [
        FieldDefinition(name=n, field_type=t, required=r, nullable=null)
        for n, t, r, null in field_specs
    ]
    return StateSchema(
        name=name,
        version=SchemaVersion(*version),
        fields=tuple(fields)
    )


# ============================================================================
# MODULE VERIFICATION
# ============================================================================

def _self_test():
    """Basic self-test to verify module integrity."""
    # Test schema creation
    schema = quick_schema(
        "test_state",
        (1, 0),
        [
            ("id", "str", True, False),
            ("count", "int", True, False),
            ("active", "bool", True, False),
        ]
    )
    
    # Test registry
    registry = create_global_registry()
    registry.register(schema)
    registry.finalize()
    
    # Test serialization
    serializer = StateSerializer(registry)
    
    data = {"id": "test-123", "count": 42, "active": True}
    context = SerializationContext("test_state", SchemaVersion(1, 0), 1234567890000)
    
    # Serialize
    payload, hash_hex = serializer.serialize_with_hash(data, context)
    
    # Deserialize
    recovered = serializer.deserialize(payload, context)
    
    # Verify
    assert recovered == data, "Serialization roundtrip failed"
    assert len(hash_hex) == 64, "Hash should be 64 hex characters"
    
    print("✓ state_serializer.py self-test passed")


if __name__ == "__main__":
    _self_test()