"""
/data/pipelines/computation/computation_spec.py

Declarative Computation Contracts (Math Without Ambiguity)

AUTHORITY: Formal contract layer for computation
PRINCIPLE: A computation that cannot be fully described cannot be trusted
BEHAVIOR: Defines truth, not execution - zero math, zero state, pure contract

This file answers:
> "What does this computation promise to do — and under what explicit constraints?"

If this file is vague:
- The executor becomes interpretive
- Results are non-reproducible
- That is an unrecoverable breach

DESIGN PRINCIPLE (CRITICAL):
Every computation MUST be describable in:
- Inputs
- Outputs
- Preconditions
- Guarantees
- Versioned identity material

If any of those are implicit → reject.

CONCEPTUAL MODEL:
A computation spec is a legal contract, not a suggestion.

ComputationSpec
 ├── computation_name
 ├── version
 ├── description
 ├── inputs
 ├── outputs
 ├── parameters
 ├── invariants
 ├── guarantees
 ├── determinism_claims
 └── identity_material

If it's not in the spec, it doesn't exist.

HARD RULE:
If a parameter changes computation behavior, it participates in identity.

SERIALIZATION RULES (ABSOLUTE):
ComputationSpec MUST be:
- Canonically serializable
- Order-stable
- Language-independent
- Byte-identical across processes

FORBIDDEN:
❌ Implicit parameters
❌ Undeclared defaults
❌ Executor-dependent behavior
❌ Schema inference
❌ Dynamic typing
❌ Optional invariants
❌ Environment-sensitive logic

If behavior isn't declared, it's illegal.

Execution answers "how." Specs answer "what is allowed to be true."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Set, Tuple, Union, TYPE_CHECKING
from enum import Enum, auto
from abc import ABC, abstractmethod
import json

if TYPE_CHECKING:
    pass  # Forward references handled via string annotations


# ============================================================================
# TYPE SYSTEM (CLOSED, FIXED)
# ============================================================================

class ParameterType(Enum):
    """
    Fixed, closed type system for parameters.
    
    Dynamic types are forbidden.
    """
    
    STRING = auto()
    INTEGER = auto()
    FLOAT = auto()
    BOOLEAN = auto()
    ENUM = auto()
    ARRAY = auto()
    OBJECT = auto()


class SchemaFieldType(Enum):
    """Fixed type system for schema fields."""
    
    STRING = auto()
    INTEGER = auto()
    FLOAT = auto()
    BOOLEAN = auto()
    TIMESTAMP = auto()
    ARRAY = auto()
    OBJECT = auto()
    NULL = auto()


# ============================================================================
# DETERMINISM DECLARATION
# ============================================================================

class DeterminismLevel(Enum):
    """
    Formal claim of reproducibility guarantees.
    
    If determinism cannot be stated → computation forbidden.
    """
    
    BIT_IDENTICAL = auto()        # Exact bit-for-bit reproduction
    EPSILON_BOUNDED = auto()      # Deterministic within declared epsilon
    IEEE754_CONSTRAINED = auto()  # Deterministic given IEEE-754 constraints
    NON_DETERMINISTIC = auto()    # Explicitly non-deterministic


@dataclass(frozen=True)
class DeterminismDeclaration:
    """
    Explicit declaration of determinism guarantees.
    
    REQUIRED for every computation.
    """
    
    level: DeterminismLevel
    uses_floating_point: bool
    numerical_tolerance: Optional[float] = None
    randomness_sources: Optional[FrozenSet[str]] = None
    replay_guarantee: str = ""
    
    def __post_init__(self):
        if self.level == DeterminismLevel.EPSILON_BOUNDED:
            if self.numerical_tolerance is None:
                raise ValueError("EPSILON_BOUNDED requires explicit numerical_tolerance")
            if self.numerical_tolerance <= 0:
                raise ValueError("EPSILON_BOUNDED numerical_tolerance must be > 0")
        
        if self.level == DeterminismLevel.NON_DETERMINISTIC and not self.randomness_sources:
            raise ValueError("NON_DETERMINISTIC requires explicit randomness_sources")
        
        if not self.replay_guarantee:
            raise ValueError("replay_guarantee cannot be empty")
    
    def to_dict(self) -> dict[str, Any]:
        return {
            'level': self.level.name,
            'uses_floating_point': self.uses_floating_point,
            'numerical_tolerance': self.numerical_tolerance,
            'randomness_sources': list(self.randomness_sources) if self.randomness_sources else None,
            'replay_guarantee': self.replay_guarantee,
        }


# ============================================================================
# PARAMETER SPECIFICATION
# ============================================================================

@dataclass(frozen=True)
class ParameterConstraint:
    """Constraints on parameter values."""
    
    min_value: Optional[Union[int, float]] = None
    max_value: Optional[Union[int, float]] = None
    allowed_values: Optional[FrozenSet[Any]] = None
    pattern: Optional[str] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    
    def validate(self, value: Any, param_name: str) -> None:
        """Validate value against constraints."""
        if self.min_value is not None and value < self.min_value:
            raise ValueError(f"Parameter '{param_name}' value {value} below minimum {self.min_value}")
        
        if self.max_value is not None and value > self.max_value:
            raise ValueError(f"Parameter '{param_name}' value {value} above maximum {self.max_value}")
        
        if self.allowed_values is not None and value not in self.allowed_values:
            raise ValueError(f"Parameter '{param_name}' value {value} not in allowed set: {self.allowed_values}")
        
        if self.min_length is not None and len(value) < self.min_length:
            raise ValueError(f"Parameter '{param_name}' length {len(value)} below minimum {self.min_length}")
        
        if self.max_length is not None and len(value) > self.max_length:
            raise ValueError(f"Parameter '{param_name}' length {len(value)} above maximum {self.max_length}")
    
    def to_dict(self) -> dict[str, Any]:
        return {
            'min_value': self.min_value,
            'max_value': self.max_value,
            'allowed_values': list(self.allowed_values) if self.allowed_values else None,
            'pattern': self.pattern,
            'min_length': self.min_length,
            'max_length': self.max_length,
        }


@dataclass(frozen=True)
class ComputationParameter:
    """
    Fully declared computation parameter.
    
    HARD RULE:
    If a parameter changes computation behavior, it participates in identity.
    
    NO MAGIC. Every parameter MUST define:
    - name
    - type
    - required
    - default (explicit or explicitly None)
    - constraints
    - participates_in_identity
    """
    
    name: str
    param_type: ParameterType
    required: bool
    default_value: Optional[Any] = None
    constraints: Optional[ParameterConstraint] = None
    participates_in_identity: bool = True
    description: str = ""
    
    def __post_init__(self):
        if not self.name:
            raise ValueError("Parameter name cannot be empty")
        
        if self.required and self.default_value is not None:
            raise ValueError(f"Parameter '{self.name}' cannot be both required and have default")
        
        if not self.required and self.default_value is None:
            raise ValueError(f"Parameter '{self.name}' must have default if not required")
        
        # Validate default type matches declared ParameterType
        if self.default_value is not None:
            self._validate_default_type()
        
        # Validate default against constraints
        if self.default_value is not None and self.constraints is not None:
            self.constraints.validate(self.default_value, self.name)
    
    def _validate_default_type(self) -> None:
        """Validate that default_value type matches declared param_type."""
        expected_type_map = {
            ParameterType.STRING: str,
            ParameterType.INTEGER: int,
            ParameterType.FLOAT: float,
            ParameterType.BOOLEAN: bool,
            ParameterType.ENUM: (str, int),  # Enums can be string or int
            ParameterType.ARRAY: list,
            ParameterType.OBJECT: dict,
        }
        
        expected_type = expected_type_map.get(self.param_type)
        if expected_type is None:
            return  # Unknown type, skip validation
        
        if not isinstance(self.default_value, expected_type):
            raise TypeError(
                f"Parameter '{self.name}': default_value type {type(self.default_value).__name__} "
                f"does not match declared ParameterType.{self.param_type.name} "
                f"(expected {expected_type.__name__ if not isinstance(expected_type, tuple) else [t.__name__ for t in expected_type]})"
            )
    
    def validate_value(self, value: Any) -> None:
        """Validate a value against this parameter's constraints."""
        if self.constraints is not None:
            self.constraints.validate(value, self.name)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'param_type': self.param_type.name,
            'required': self.required,
            'default_value': self.default_value,
            'constraints': self.constraints.to_dict() if self.constraints else None,
            'participates_in_identity': self.participates_in_identity,
            'description': self.description,
        }


# ============================================================================
# SCHEMA SPECIFICATION
# ============================================================================

@dataclass(frozen=True)
class SchemaField:
    """
    Single field in a schema.
    
    Explicit, versioned, deterministically serializable.
    No optional implicit defaults.
    """
    
    name: str
    field_type: SchemaFieldType
    required: bool
    nullable: bool = False
    array_element_type: Optional[SchemaFieldType] = None
    nested_schema: Optional[SchemaDescriptor] = None
    description: str = ""
    
    def __post_init__(self):
        if not self.name:
            raise ValueError("Schema field name cannot be empty")
        
        if self.field_type == SchemaFieldType.ARRAY and self.array_element_type is None:
            raise ValueError(f"Field '{self.name}': ARRAY type requires array_element_type")
        
        if self.field_type == SchemaFieldType.OBJECT and self.nested_schema is None:
            raise ValueError(f"Field '{self.name}': OBJECT type requires nested_schema")
    
    def to_dict(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'field_type': self.field_type.name,
            'required': self.required,
            'nullable': self.nullable,
            'array_element_type': self.array_element_type.name if self.array_element_type else None,
            'nested_schema': self.nested_schema.to_dict() if self.nested_schema else None,
            'description': self.description,
        }


@dataclass(frozen=True)
class SchemaDescriptor:
    """
    Explicit schema definition.
    
    MUST be:
    - Explicit
    - Versioned
    - Deterministically serializable
    - Free of optional implicit defaults
    
    Dynamic schemas are forbidden.
    """
    
    schema_name: str
    version: str
    fields: Tuple[SchemaField, ...]
    description: str = ""
    
    def __post_init__(self):
        if not self.schema_name:
            raise ValueError("Schema name cannot be empty")
        
        if not self.version:
            raise ValueError("Schema version cannot be empty")
        
        if not self.fields:
            raise ValueError("Schema must have at least one field")
        
        # Verify no duplicate field names
        field_names = [f.name for f in self.fields]
        if len(field_names) != len(set(field_names)):
            raise ValueError(f"Schema '{self.schema_name}' has duplicate field names")
    
    @property
    def required_fields(self) -> FrozenSet[str]:
        return frozenset(f.name for f in self.fields if f.required)
    
    @property
    def optional_fields(self) -> FrozenSet[str]:
        return frozenset(f.name for f in self.fields if not f.required)
    
    @property
    def all_fields(self) -> FrozenSet[str]:
        return frozenset(f.name for f in self.fields)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            'schema_name': self.schema_name,
            'version': self.version,
            'fields': [f.to_dict() for f in self.fields],
            'description': self.description,
        }


# ============================================================================
# WINDOW DESCRIPTOR
# ============================================================================

@dataclass(frozen=True)
class WindowDescriptor:
    """
    Declarative window dependency.
    
    RULES:
    - By window identity only
    - Never by window logic
    - Never by dynamic reference
    
    This prevents window re-interpretation.
    """
    
    window_identity: str
    window_version: str
    description: str = ""
    
    def __post_init__(self):
        if not self.window_identity:
            raise ValueError("Window identity cannot be empty")
        
        if not self.window_version:
            raise ValueError("Window version cannot be empty")
    
    def to_dict(self) -> dict[str, Any]:
        return {
            'window_identity': self.window_identity,
            'window_version': self.window_version,
            'description': self.description,
        }


# ============================================================================
# INVARIANTS (CONTRACTUAL LAW)
# ============================================================================

class InvariantType(Enum):
    """Classification of invariant assertions."""
    
    PRECONDITION = auto()   # Must hold before execution
    POSTCONDITION = auto()  # Must hold after execution
    ALWAYS = auto()         # Must always hold


@dataclass(frozen=True)
class DeterministicPredicate:
    """
    Deterministic, executable predicate for invariant checking.
    
    RULES:
    - Must be pure (no side effects)
    - Must be deterministic (same inputs → same output)
    - Must be serializable (for cross-language reproducibility)
    - Must not depend on external state
    
    This is a placeholder for a formal DSL or callable that can be
    deterministically serialized and executed across environments.
    """
    
    predicate_id: str  # Unique identifier for the predicate
    predicate_dsl: str  # Deterministic DSL expression (e.g., JSONPath, SQL-like)
    expected_result: Any  # Expected result type/constraint
    
    def __post_init__(self):
        if not self.predicate_id:
            raise ValueError("DeterministicPredicate predicate_id cannot be empty")
        
        if not self.predicate_dsl:
            raise ValueError("DeterministicPredicate predicate_dsl cannot be empty")
    
    def to_dict(self) -> dict[str, Any]:
        return {
            'predicate_id': self.predicate_id,
            'predicate_dsl': self.predicate_dsl,
            'expected_result': self.expected_result,
        }


@dataclass(frozen=True)
class Invariant:
    """
    Executable assertion - not a comment.
    
    RULES:
    - Must be checkable
    - Must be deterministic
    - Must not depend on external state
    
    Violation → hard failure
    
    Examples:
    - "Input counts must be non-negative"
    - "Output ratio must be in [0, 1]"
    - "Zero inputs produce zero output"
    """
    
    name: str
    invariant_type: InvariantType
    predicate: DeterministicPredicate
    description: str = ""
    
    def __post_init__(self):
        if not self.name:
            raise ValueError("Invariant name cannot be empty")
    
    def to_dict(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'invariant_type': self.invariant_type.name,
            'predicate': self.predicate.to_dict(),
            'description': self.description,
        }


# ============================================================================
# COMPUTATION TYPE
# ============================================================================

class ComputationType(Enum):
    """Classification of computation semantics."""
    
    AGGREGATION = auto()     # Combines multiple inputs
    TRANSFORMATION = auto()  # 1:1 mapping
    FILTER = auto()          # Subset selection
    JOIN = auto()            # Combining aligned datasets
    WINDOW = auto()          # Time-windowed operation
    STATEFUL = auto()        # Requires historical state


# ============================================================================
# COMPUTATION SPEC (CANONICAL)
# ============================================================================

@dataclass(frozen=True)
class ComputationSpec:
    """
    Canonical computation specification.
    
    A computation spec is a legal contract, not a suggestion.
    
    REQUIRED FIELDS:
    - computation_name: Human-meaningful, stable identifier
    - version: Explicit version (semantic or monotonic)
    - description: Plain-English, precise, non-marketing explanation
    - computation_type: Classification of computation semantics
    - input_schema: Explicit input structure and types
    - output_schema: Explicit output structure and types
    - parameters: Fully declared tunables (no hidden constants)
    - required_windows: Declared dependency on windows (by identity only)
    - preconditions: Conditions that must hold before execution
    - postconditions: Conditions that must hold after execution
    - determinism: Formal claim of reproducibility guarantees
    - identity_fields: Fields contributing to identity hash
    
    SERIALIZATION:
    - Canonically serializable
    - Order-stable
    - Language-independent
    - Byte-identical across processes
    
    FAILURE POLICY:
    Reject spec construction if:
    - Required fields missing
    - Schemas underspecified
    - Parameters untyped
    - Identity fields incomplete
    - Determinism claim missing
    - Non-serializable content present
    
    Silent acceptance is not allowed.
    """
    
    computation_name: str
    version: str
    description: str
    computation_type: ComputationType
    input_schema: SchemaDescriptor
    output_schema: SchemaDescriptor
    parameters: Tuple[ComputationParameter, ...]
    invariants: Tuple[Invariant, ...]
    
    required_windows: Tuple[WindowDescriptor, ...] = field(default_factory=tuple)
    preconditions: Tuple[Invariant, ...] = field(default_factory=tuple)
    postconditions: Tuple[Invariant, ...] = field(default_factory=tuple)
    determinism: Optional[DeterminismDeclaration] = None
    identity_fields: FrozenSet[str] = field(default_factory=frozenset)
    
    def __post_init__(self):
        """
        Validate spec construction.
        
        FAIL FAST on any violation.
        """
        # Required fields validation
        if not self.computation_name:
            raise ValueError("computation_name cannot be empty")
        
        if not self.version:
            raise ValueError("version cannot be empty")
        
        if not self.description:
            raise ValueError("description cannot be empty")
        
        if not isinstance(self.input_schema, SchemaDescriptor):
            raise TypeError("input_schema must be SchemaDescriptor")
        
        if not isinstance(self.output_schema, SchemaDescriptor):
            raise TypeError("output_schema must be SchemaDescriptor")
        
        if not isinstance(self.parameters, tuple):
            raise TypeError("parameters must be Tuple[ComputationParameter, ...]")
        
        if not isinstance(self.invariants, tuple):
            raise TypeError("invariants must be Tuple[Invariant, ...]")
        
        # Determinism validation
        if self.determinism is None:
            raise ValueError("determinism declaration is required")
        
        # Identity fields validation - MUST be explicitly declared
        if not self.identity_fields:
            raise ValueError(
                "identity_fields must be explicitly declared. "
                "Auto-population is forbidden - contracts must never infer identity."
            )
        
        # Validate identity fields exist
        spec_fields = set(vars(self).keys())
        for id_field in self.identity_fields:
            if id_field not in spec_fields:
                raise ValueError(f"Identity field '{id_field}' not in spec")
    
    def to_canonical_dict(self) -> dict[str, Any]:
        """
        Convert to canonical dictionary for hashing.
        
        RULES:
        - Include only identity_fields
        - Deterministic ordering
        - Language-independent
        
        This enables:
        - Registry hashing
        - Replay validation
        - Cross-environment equality
        """
        canonical = {}
        
        for field_name in sorted(self.identity_fields):
            value = getattr(self, field_name)
            
            if field_name == 'computation_type':
                canonical[field_name] = value.name
            elif field_name == 'determinism':
                canonical[field_name] = value.to_dict() if value else None
            elif isinstance(value, SchemaDescriptor):
                canonical[field_name] = value.to_dict()
            elif isinstance(value, tuple):
                canonical[field_name] = [
                    item.to_dict() if hasattr(item, 'to_dict') else item
                    for item in value
                ]
            elif isinstance(value, frozenset):
                canonical[field_name] = sorted(list(value))
            else:
                canonical[field_name] = value
        
        return canonical
    
    def to_dict(self) -> dict[str, Any]:
        """
        Full serialization including non-identity fields.
        """
        return {
            'computation_name': self.computation_name,
            'version': self.version,
            'description': self.description,
            'computation_type': self.computation_type.name,
            'input_schema': self.input_schema.to_dict(),
            'output_schema': self.output_schema.to_dict(),
            'parameters': [p.to_dict() for p in self.parameters],
            'invariants': [inv.to_dict() for inv in self.invariants],
            'required_windows': [w.to_dict() for w in self.required_windows],
            'preconditions': [inv.to_dict() for inv in self.preconditions],
            'postconditions': [inv.to_dict() for inv in self.postconditions],
            'determinism': self.determinism.to_dict() if self.determinism else None,
            'identity_fields': sorted(list(self.identity_fields)),
        }
    
    @property
    def requires_window(self) -> bool:
        """Check if computation requires window dependency."""
        return bool(self.required_windows)
    
    @property
    def is_deterministic(self) -> bool:
        """Check if computation claims determinism."""
        return (
            self.determinism is not None
            and self.determinism.level != DeterminismLevel.NON_DETERMINISTIC
        )
    
    @property
    def identity_material(self) -> dict[str, Any]:
        """
        Get identity material for hashing.
        
        Derived exclusively from:
        - Spec fields
        - Ordered, canonical serialization
        - Explicit inclusion list (identity_fields)
        
        Never include:
        - Comments
        - Formatting
        - Executor implementation
        - Environment data
        """
        return self.to_canonical_dict()

