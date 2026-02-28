"""
/data/pipelines/validation/input_validator.py

Schema + Structural Validation of Pipeline Inputs

AUTHORITY: Single source of truth for structural admissibility
PRINCIPLE: Invalid structure is worse than missing data
BEHAVIOR: Accept or reject - never repair

This file enforces:
- Schema conformance (deep, recursive)
- Structural completeness
- Type correctness (exact, no coercion)
- Version compatibility (explicit only)
- Execution context integrity
- Global invariants

NO mutations. NO partial success. NO silent failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, FrozenSet, Mapping, Optional, Tuple, Union, List, Dict
from collections.abc import Hashable
import hashlib
import json
from abc import ABC, abstractmethod


# ============================================================================
# IMMUTABLE STRUCTURES
# ============================================================================

class FrozenDict(Mapping[str, Any]):
    """Immutable dictionary wrapper for validated inputs with deep immutability."""
    
    __slots__ = ('_data', '_hash')
    
    def __init__(self, data: dict[str, Any]) -> None:
        # Deep freeze: recursively convert nested dicts to FrozenDict
        self._data = self._deep_freeze(data)
        self._hash: Optional[int] = None
    
    @staticmethod
    def _deep_freeze(data: Any) -> Any:
        """Recursively freeze nested structures for true immutability."""
        if isinstance(data, dict):
            # Recursively freeze nested dicts and wrap them in FrozenDict
            frozen_dict = {}
            for k, v in data.items():
                if isinstance(v, dict):
                    # Wrap nested dicts in FrozenDict for true immutability
                    frozen_dict[k] = FrozenDict(v)
                elif isinstance(v, list):
                    frozen_dict[k] = tuple(FrozenDict._deep_freeze(item) for item in v)
                else:
                    frozen_dict[k] = v
            return frozen_dict
        elif isinstance(data, list):
            return tuple(FrozenDict._deep_freeze(item) for item in data)
        else:
            return data
    
    def __getitem__(self, key: str) -> Any:
        value = self._data[key]
        # Ensure nested dicts are returned as FrozenDict instances
        if isinstance(value, dict) and not isinstance(value, FrozenDict):
            return FrozenDict(value)
        return value
    
    def __iter__(self):
        return iter(self._data)
    
    def __len__(self) -> int:
        return len(self._data)
    
    def __hash__(self) -> int:
        if self._hash is None:
            self._hash = hash(tuple(sorted(self._data.items())))
        return self._hash
    
    def __repr__(self) -> str:
        return f"FrozenDict({self._data!r})"


# ============================================================================
# ERROR TAXONOMY
# ============================================================================

class ValidationCategory(Enum):
    """Enumeration of validation failure categories."""
    SCHEMA = auto()
    STRUCTURE = auto()
    VERSION = auto()
    CONTEXT = auto()
    TYPE = auto()
    INVARIANT = auto()


class ValidationErrorCode(Enum):
    """Stable, enumerable error codes for deterministic failure reporting."""
    
    # Context errors
    CONTEXT_MISSING = "E001"
    CONTEXT_INVALID = "E002"
    CONTEXT_FINGERPRINT_MISMATCH = "E003"
    
    # Schema errors
    SCHEMA_NOT_DECLARED = "E101"
    SCHEMA_NOT_FOUND = "E102"
    SCHEMA_VERSION_MISSING = "E103"
    SCHEMA_VERSION_INCOMPATIBLE = "E104"
    
    # Structure errors
    REQUIRED_INPUT_MISSING = "E201"
    UNKNOWN_INPUT = "E202"
    REQUIRED_FIELD_MISSING = "E203"
    UNKNOWN_FIELD = "E204"
    EMPTY_OBJECT_FORBIDDEN = "E205"
    
    # Type errors
    TYPE_MISMATCH = "E301"
    ARRAY_ELEMENT_TYPE_MISMATCH = "E302"
    NESTED_VALIDATION_FAILED = "E303"
    
    # Invariant violations
    PARTIAL_VALIDATION = "E401"
    INPUT_MUTATION_DETECTED = "E402"
    AMBIGUOUS_INPUT = "E403"
    UNDERSPECIFIED_INPUT = "E404"


@dataclass(frozen=True)
class InputValidationError:
    """Structured, immutable representation of a validation failure."""
    
    error_code: ValidationErrorCode
    category: ValidationCategory
    input_name: str
    path: str
    message: str
    recovery_hint: str
    
    def __str__(self) -> str:
        return (
            f"[{self.error_code.value}] {self.category.name} error in '{self.input_name}' "
            f"at path '{self.path}': {self.message}\n"
            f"Recovery: {self.recovery_hint}"
        )


# ============================================================================
# SCHEMA DEFINITIONS
# ============================================================================

class SchemaType(Enum):
    """Primitive and composite schema types."""
    STRING = auto()
    INTEGER = auto()
    FLOAT = auto()
    BOOLEAN = auto()
    OBJECT = auto()
    ARRAY = auto()
    NULL = auto()
    ENUM = auto()


@dataclass(frozen=True)
class FieldDefinition:
    """Defines constraints for a single field."""
    
    name: str
    field_type: SchemaType
    required: bool
    nullable: bool = False
    enum_values: Optional[FrozenSet[Any]] = None
    nested_schema: Optional[SchemaDefinition] = None
    array_element_schema: Optional[FieldDefinition] = None
    allow_empty: bool = False
    
    def __post_init__(self):
        if self.field_type == SchemaType.ENUM and self.enum_values is None:
            raise ValueError(f"Field {self.name}: ENUM type requires enum_values")
        if self.field_type == SchemaType.OBJECT and self.nested_schema is None:
            raise ValueError(f"Field {self.name}: OBJECT type requires nested_schema")
        if self.field_type == SchemaType.ARRAY and self.array_element_schema is None:
            raise ValueError(f"Field {self.name}: ARRAY type requires array_element_schema")


@dataclass(frozen=True)
class SchemaDefinition:
    """Immutable schema definition for an input."""
    
    name: str
    version: str
    fields: Tuple[FieldDefinition, ...]
    allow_unknown_fields: bool = False
    allow_empty_object: bool = False
    
    def __post_init__(self):
        field_names = {f.name for f in self.fields}
        if len(field_names) != len(self.fields):
            raise ValueError(f"Schema {self.name}: Duplicate field names detected")
    
    @property
    def required_fields(self) -> FrozenSet[str]:
        return frozenset(f.name for f in self.fields if f.required)
    
    @property
    def optional_fields(self) -> FrozenSet[str]:
        return frozenset(f.name for f in self.fields if not f.required)
    
    @property
    def all_fields(self) -> FrozenSet[str]:
        return frozenset(f.name for f in self.fields)


# ============================================================================
# SCHEMA REGISTRY
# ============================================================================

@dataclass(frozen=True)
class InputSchemaRegistry:
    """
    Immutable registry defining allowed schemas.
    
    RULES:
    - Registry is immutable
    - Schemas are declared, never inferred
    - Missing schema = fatal error
    - Dynamic schema loading is forbidden
    """
    
    schemas: FrozenDict
    required_inputs: FrozenSet[str]
    optional_inputs: FrozenSet[str]
    version_compatibility_matrix: FrozenDict
    
    def __post_init__(self):
        all_inputs = self.required_inputs | self.optional_inputs
        if len(all_inputs) != len(self.required_inputs) + len(self.optional_inputs):
            raise ValueError("Input sets overlap between required and optional")
        
        for input_name in all_inputs:
            if input_name not in self.schemas:
                raise ValueError(f"Input '{input_name}' declared but schema not found")
    
    def get_schema(self, input_name: str) -> Optional[SchemaDefinition]:
        """Retrieve schema by input name."""
        return self.schemas.get(input_name)
    
    def is_version_compatible(self, schema_name: str, version: str) -> bool:
        """Check if version is compatible with schema."""
        compat_matrix = self.version_compatibility_matrix.get(schema_name)
        if compat_matrix is None:
            return False
        return version in compat_matrix
    
    @property
    def all_inputs(self) -> FrozenSet[str]:
        return self.required_inputs | self.optional_inputs


# ============================================================================
# PIPELINE CONTEXT
# ============================================================================

@dataclass(frozen=True)
class PipelineContext:
    """
    Execution context required for validation.
    
    Provides execution authority and environmental invariants.
    """
    
    execution_id: str
    pipeline_name: str
    environment: str
    timestamp: str
    required_inputs: FrozenSet[str]
    metadata: FrozenDict
    
    def fingerprint(self) -> str:
        """Generate deterministic fingerprint of context."""
        data = {
            'execution_id': self.execution_id,
            'pipeline_name': self.pipeline_name,
            'environment': self.environment,
            'timestamp': self.timestamp,
            'required_inputs': sorted(self.required_inputs)
        }
        return hashlib.sha256(
            json.dumps(data, sort_keys=True).encode('utf-8')
        ).hexdigest()


# ============================================================================
# VALIDATION RESULT
# ============================================================================

@dataclass(frozen=True)
class InputValidationResult:
    """
    Immutable validation outcome.
    
    If is_valid == True, inputs are safe to execute against.
    If is_valid == False, execution MUST NOT proceed.
    """
    
    is_valid: bool
    errors: Tuple[InputValidationError, ...]
    validated_inputs: Optional[FrozenDict]
    schema_versions: FrozenDict
    context_fingerprint: str
    validation_timestamp: str
    
    def __post_init__(self):
        if self.is_valid and len(self.errors) > 0:
            raise ValueError("INVARIANT VIOLATION: is_valid=True with errors present")
        if not self.is_valid and self.validated_inputs is not None:
            raise ValueError("INVARIANT VIOLATION: is_valid=False with validated_inputs present")


# ============================================================================
# TYPE VALIDATORS
# ============================================================================

class TypeValidator(ABC):
    """Abstract base for type-specific validation."""
    
    @abstractmethod
    def validate(
        self,
        value: Any,
        field_def: FieldDefinition,
        path: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate value against field definition.
        
        Returns: (is_valid, error_message)
        """
        pass


class StringValidator(TypeValidator):
    def validate(self, value: Any, field_def: FieldDefinition, path: str) -> Tuple[bool, Optional[str]]:
        if not isinstance(value, str):
            return False, f"Expected string, got {type(value).__name__}"
        return True, None


class IntegerValidator(TypeValidator):
    def validate(self, value: Any, field_def: FieldDefinition, path: str) -> Tuple[bool, Optional[str]]:
        if not isinstance(value, int) or isinstance(value, bool):
            return False, f"Expected integer, got {type(value).__name__}"
        return True, None


class FloatValidator(TypeValidator):
    def validate(self, value: Any, field_def: FieldDefinition, path: str) -> Tuple[bool, Optional[str]]:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False, f"Expected float, got {type(value).__name__}"
        return True, None


class BooleanValidator(TypeValidator):
    def validate(self, value: Any, field_def: FieldDefinition, path: str) -> Tuple[bool, Optional[str]]:
        if not isinstance(value, bool):
            return False, f"Expected boolean, got {type(value).__name__}"
        return True, None


class NullValidator(TypeValidator):
    def validate(self, value: Any, field_def: FieldDefinition, path: str) -> Tuple[bool, Optional[str]]:
        if value is not None:
            return False, f"Expected null, got {type(value).__name__}"
        return True, None


class EnumValidator(TypeValidator):
    def validate(self, value: Any, field_def: FieldDefinition, path: str) -> Tuple[bool, Optional[str]]:
        if field_def.enum_values is None:
            return False, "Enum field missing enum_values"
        if value not in field_def.enum_values:
            return False, f"Value '{value}' not in allowed enum values: {field_def.enum_values}"
        return True, None


# ============================================================================
# CORE VALIDATOR
# ============================================================================

class InputValidator:
    """
    Primary authority for structural validation of pipeline inputs.
    
    GUARANTEES:
    - No execution without full schema validation
    - No partial validation success
    - No silent acceptance of unknown fields
    - No mutation of input payloads
    - No context-free validation
    - No implicit defaults
    
    Violation of any guarantee → pipeline hard stop.
    """
    
    def __init__(self, registry: InputSchemaRegistry):
        self._registry = registry
        self._type_validators = {
            SchemaType.STRING: StringValidator(),
            SchemaType.INTEGER: IntegerValidator(),
            SchemaType.FLOAT: FloatValidator(),
            SchemaType.BOOLEAN: BooleanValidator(),
            SchemaType.NULL: NullValidator(),
            SchemaType.ENUM: EnumValidator(),
        }
    
    def validate(
        self,
        *,
        inputs: dict[str, Any],
        context: PipelineContext
    ) -> InputValidationResult:
        """
        Orchestrate structural validation of pipeline inputs.
        
        EXECUTION ORDER (STRICT):
        1. Validate execution context presence
        2. Validate schema declarations exist
        3. Validate input set completeness
        4. Validate per-input schema conformance
        5. Validate version compatibility
        6. Enforce global invariants
        7. Emit immutable validation result
        
        No step may be skipped or reordered.
        """
        errors: list[InputValidationError] = []
        schema_versions: dict[str, str] = {}
        
        # STEP 1: Validate execution context presence
        context_errors = self._validate_context(context)
        errors.extend(context_errors)
        if context_errors:
            return self._fail(errors, context)
        
        # STEP 2: Validate schema declarations exist
        schema_errors = self._validate_schema_declarations(inputs)
        errors.extend(schema_errors)
        if schema_errors:
            return self._fail(errors, context)
        
        # STEP 3: Validate input set completeness
        completeness_errors = self._validate_input_completeness(inputs, context)
        errors.extend(completeness_errors)
        if completeness_errors:
            return self._fail(errors, context)
        
        # STEP 4: Validate per-input schema conformance and extract versions from payloads
        for input_name, input_data in inputs.items():
            schema = self._registry.get_schema(input_name)
            if schema is None:
                errors.append(InputValidationError(
                    error_code=ValidationErrorCode.SCHEMA_NOT_FOUND,
                    category=ValidationCategory.SCHEMA,
                    input_name=input_name,
                    path=f"/{input_name}",
                    message=f"No schema definition found for input '{input_name}'",
                    recovery_hint="Register schema in InputSchemaRegistry before validation"
                ))
                continue
            
            # Extract version from payload (authoritative source)
            declared_version = None
            if isinstance(input_data, dict) and '_schema_version' in input_data:
                declared_version = input_data['_schema_version']
            
            if declared_version is None:
                errors.append(InputValidationError(
                    error_code=ValidationErrorCode.SCHEMA_VERSION_MISSING,
                    category=ValidationCategory.VERSION,
                    input_name=input_name,
                    path=f"/{input_name}/_schema_version",
                    message=f"Schema version declaration missing in input payload",
                    recovery_hint="Add '_schema_version' field to input payload"
                ))
            
            schema_errors = self._validate_against_schema(
                input_name=input_name,
                input_data=input_data,
                schema=schema,
                path=f"/{input_name}"
            )
            errors.extend(schema_errors)
            
            if not schema_errors and declared_version is not None:
                schema_versions[input_name] = declared_version
        
        if errors:
            return self._fail(errors, context)
        
        # STEP 5: Validate version compatibility
        version_errors = self._validate_version_compatibility(inputs, schema_versions)
        errors.extend(version_errors)
        if version_errors:
            return self._fail(errors, context)
        
        # STEP 6: Enforce global invariants
        invariant_errors = self._enforce_invariants(inputs, context)
        errors.extend(invariant_errors)
        if invariant_errors:
            return self._fail(errors, context)
        
        # STEP 7: Emit immutable validation result
        result = self._succeed(inputs, schema_versions, context)
        
        # STEP 8: Enforce all validation invariants (post-validation checks)
        InputValidationInvariants.enforce_all(inputs, result, context)
        
        return result
    
    def _validate_context(self, context: PipelineContext) -> list[InputValidationError]:
        """Validate execution context presence and integrity."""
        errors = []
        
        if context is None:
            errors.append(InputValidationError(
                error_code=ValidationErrorCode.CONTEXT_MISSING,
                category=ValidationCategory.CONTEXT,
                input_name="<context>",
                path="/",
                message="PipelineContext is None",
                recovery_hint="Provide valid PipelineContext to validator"
            ))
            return errors
        
        if not context.execution_id:
            errors.append(InputValidationError(
                error_code=ValidationErrorCode.CONTEXT_INVALID,
                category=ValidationCategory.CONTEXT,
                input_name="<context>",
                path="/execution_id",
                message="execution_id is empty or missing",
                recovery_hint="Ensure PipelineContext has valid execution_id"
            ))
        
        if not context.pipeline_name:
            errors.append(InputValidationError(
                error_code=ValidationErrorCode.CONTEXT_INVALID,
                category=ValidationCategory.CONTEXT,
                input_name="<context>",
                path="/pipeline_name",
                message="pipeline_name is empty or missing",
                recovery_hint="Ensure PipelineContext has valid pipeline_name"
            ))
        
        if not context.timestamp:
            errors.append(InputValidationError(
                error_code=ValidationErrorCode.CONTEXT_INVALID,
                category=ValidationCategory.CONTEXT,
                input_name="<context>",
                path="/timestamp",
                message="timestamp is empty or missing",
                recovery_hint="Ensure PipelineContext has valid timestamp"
            ))
        
        return errors
    
    def _validate_schema_declarations(self, inputs: dict[str, Any]) -> list[InputValidationError]:
        """Validate that all inputs have declared schemas."""
        errors = []
        
        for input_name in inputs.keys():
            if input_name not in self._registry.all_inputs:
                errors.append(InputValidationError(
                    error_code=ValidationErrorCode.SCHEMA_NOT_DECLARED,
                    category=ValidationCategory.SCHEMA,
                    input_name=input_name,
                    path=f"/{input_name}",
                    message=f"Input '{input_name}' not declared in registry",
                    recovery_hint="Add input to InputSchemaRegistry.required_inputs or .optional_inputs"
                ))
        
        return errors
    
    def _validate_input_completeness(
        self,
        inputs: dict[str, Any],
        context: PipelineContext
    ) -> list[InputValidationError]:
        """Validate that all required inputs are present."""
        errors = []
        
        provided_inputs = set(inputs.keys())
        # Registry is the authoritative source for required inputs
        required_inputs = self._registry.required_inputs
        
        missing_inputs = required_inputs - provided_inputs
        for input_name in missing_inputs:
            errors.append(InputValidationError(
                error_code=ValidationErrorCode.REQUIRED_INPUT_MISSING,
                category=ValidationCategory.STRUCTURE,
                input_name=input_name,
                path=f"/{input_name}",
                message=f"Required input '{input_name}' is missing",
                recovery_hint=f"Provide '{input_name}' in inputs dictionary"
            ))
        
        unknown_inputs = provided_inputs - self._registry.all_inputs
        for input_name in unknown_inputs:
            errors.append(InputValidationError(
                error_code=ValidationErrorCode.UNKNOWN_INPUT,
                category=ValidationCategory.STRUCTURE,
                input_name=input_name,
                path=f"/{input_name}",
                message=f"Unknown input '{input_name}' provided",
                recovery_hint="Remove unknown input or register it in schema registry"
            ))
        
        return errors
    
    def _validate_against_schema(
        self,
        input_name: str,
        input_data: Any,
        schema: SchemaDefinition,
        path: str
    ) -> list[InputValidationError]:
        """Validate input data against schema definition."""
        errors = []
        
        if not isinstance(input_data, dict):
            errors.append(InputValidationError(
                error_code=ValidationErrorCode.TYPE_MISMATCH,
                category=ValidationCategory.TYPE,
                input_name=input_name,
                path=path,
                message=f"Expected object/dict, got {type(input_data).__name__}",
                recovery_hint="Ensure input is a dictionary structure"
            ))
            return errors
        
        # Check for empty object
        if len(input_data) == 0 and not schema.allow_empty_object:
            errors.append(InputValidationError(
                error_code=ValidationErrorCode.EMPTY_OBJECT_FORBIDDEN,
                category=ValidationCategory.STRUCTURE,
                input_name=input_name,
                path=path,
                message="Empty object provided but not allowed by schema",
                recovery_hint="Provide required fields or enable allow_empty_object in schema"
            ))
            return errors
        
        # Validate required fields
        provided_fields = set(input_data.keys())
        missing_required = schema.required_fields - provided_fields
        
        for field_name in missing_required:
            errors.append(InputValidationError(
                error_code=ValidationErrorCode.REQUIRED_FIELD_MISSING,
                category=ValidationCategory.STRUCTURE,
                input_name=input_name,
                path=f"{path}/{field_name}",
                message=f"Required field '{field_name}' is missing",
                recovery_hint=f"Add field '{field_name}' to input object"
            ))
        
        # Validate unknown fields
        if not schema.allow_unknown_fields:
            unknown_fields = provided_fields - schema.all_fields
            for field_name in unknown_fields:
                errors.append(InputValidationError(
                    error_code=ValidationErrorCode.UNKNOWN_FIELD,
                    category=ValidationCategory.STRUCTURE,
                    input_name=input_name,
                    path=f"{path}/{field_name}",
                    message=f"Unknown field '{field_name}' not in schema",
                    recovery_hint="Remove field or enable allow_unknown_fields in schema"
                ))
        
        # Validate each field
        for field_def in schema.fields:
            if field_def.name not in input_data:
                continue
            
            field_value = input_data[field_def.name]
            field_path = f"{path}/{field_def.name}"
            
            field_errors = self._validate_field(
                input_name=input_name,
                field_value=field_value,
                field_def=field_def,
                path=field_path
            )
            errors.extend(field_errors)
        
        return errors
    
    def _validate_field(
        self,
        input_name: str,
        field_value: Any,
        field_def: FieldDefinition,
        path: str
    ) -> list[InputValidationError]:
        """Validate a single field against its definition."""
        errors = []
        
        # Handle nullable
        if field_value is None:
            if not field_def.nullable:
                errors.append(InputValidationError(
                    error_code=ValidationErrorCode.TYPE_MISMATCH,
                    category=ValidationCategory.TYPE,
                    input_name=input_name,
                    path=path,
                    message=f"Field is null but not nullable",
                    recovery_hint="Provide non-null value or mark field as nullable"
                ))
            return errors
        
        # Validate by type
        if field_def.field_type == SchemaType.OBJECT:
            if not isinstance(field_value, dict):
                errors.append(InputValidationError(
                    error_code=ValidationErrorCode.TYPE_MISMATCH,
                    category=ValidationCategory.TYPE,
                    input_name=input_name,
                    path=path,
                    message=f"Expected object, got {type(field_value).__name__}",
                    recovery_hint="Provide dictionary/object value"
                ))
            elif field_def.nested_schema is not None:
                nested_errors = self._validate_against_schema(
                    input_name=input_name,
                    input_data=field_value,
                    schema=field_def.nested_schema,
                    path=path
                )
                errors.extend(nested_errors)
        
        elif field_def.field_type == SchemaType.ARRAY:
            if not isinstance(field_value, list):
                errors.append(InputValidationError(
                    error_code=ValidationErrorCode.TYPE_MISMATCH,
                    category=ValidationCategory.TYPE,
                    input_name=input_name,
                    path=path,
                    message=f"Expected array, got {type(field_value).__name__}",
                    recovery_hint="Provide list/array value"
                ))
            elif field_def.array_element_schema is not None:
                for idx, element in enumerate(field_value):
                    element_errors = self._validate_field(
                        input_name=input_name,
                        field_value=element,
                        field_def=field_def.array_element_schema,
                        path=f"{path}[{idx}]"
                    )
                    errors.extend(element_errors)
        
        else:
            # Primitive type validation
            validator = self._type_validators.get(field_def.field_type)
            if validator is None:
                errors.append(InputValidationError(
                    error_code=ValidationErrorCode.TYPE_MISMATCH,
                    category=ValidationCategory.TYPE,
                    input_name=input_name,
                    path=path,
                    message=f"No validator for type {field_def.field_type}",
                    recovery_hint="Use supported schema types"
                ))
            else:
                is_valid, error_msg = validator.validate(field_value, field_def, path)
                if not is_valid:
                    errors.append(InputValidationError(
                        error_code=ValidationErrorCode.TYPE_MISMATCH,
                        category=ValidationCategory.TYPE,
                        input_name=input_name,
                        path=path,
                        message=error_msg or "Type validation failed",
                        recovery_hint="Provide value matching expected type"
                    ))
        
        return errors
    
    def _validate_version_compatibility(
        self,
        inputs: dict[str, Any],
        schema_versions: dict[str, str]
    ) -> list[InputValidationError]:
        """Validate version compatibility for all inputs."""
        errors = []
        
        for input_name, declared_version in schema_versions.items():
            schema = self._registry.get_schema(input_name)
            if schema is None:
                continue
            
            # Check compatibility using schema name (not input name) for proper version lineage
            if not self._registry.is_version_compatible(schema.name, declared_version):
                errors.append(InputValidationError(
                    error_code=ValidationErrorCode.SCHEMA_VERSION_INCOMPATIBLE,
                    category=ValidationCategory.VERSION,
                    input_name=input_name,
                    path=f"/{input_name}/_schema_version",
                    message=f"Declared schema version '{declared_version}' is not compatible with schema '{schema.name}'",
                    recovery_hint=f"Use a compatible version for schema '{schema.name}' (expected: {schema.version} or compatible versions)"
                ))
        
        return errors
    
    def _enforce_invariants(
        self,
        inputs: dict[str, Any],
        context: PipelineContext
    ) -> list[InputValidationError]:
        """Enforce global validation invariants."""
        errors = []
        
        # Invariant: Context fingerprint must be deterministic
        try:
            fingerprint = context.fingerprint()
            if not fingerprint or len(fingerprint) != 64:
                errors.append(InputValidationError(
                    error_code=ValidationErrorCode.CONTEXT_FINGERPRINT_MISMATCH,
                    category=ValidationCategory.INVARIANT,
                    input_name="<context>",
                    path="/fingerprint",
                    message="Context fingerprint invalid or non-deterministic",
                    recovery_hint="Ensure context generates valid SHA-256 fingerprint"
                ))
        except Exception as e:
            errors.append(InputValidationError(
                error_code=ValidationErrorCode.CONTEXT_FINGERPRINT_MISMATCH,
                category=ValidationCategory.INVARIANT,
                input_name="<context>",
                path="/fingerprint",
                message=f"Failed to generate context fingerprint: {str(e)}",
                recovery_hint="Fix context fingerprint generation logic"
            ))
        
        # Invariant: No ambiguous or underspecified inputs
        for input_name, input_data in inputs.items():
            if isinstance(input_data, dict):
                if not input_data:
                    schema = self._registry.get_schema(input_name)
                    if schema and not schema.allow_empty_object:
                        errors.append(InputValidationError(
                            error_code=ValidationErrorCode.UNDERSPECIFIED_INPUT,
                            category=ValidationCategory.INVARIANT,
                            input_name=input_name,
                            path=f"/{input_name}",
                            message="Input is empty and underspecified",
                            recovery_hint="Provide complete input data"
                        ))
        
        return errors
    
    def _fail(
        self,
        errors: list[InputValidationError],
        context: PipelineContext
    ) -> InputValidationResult:
        """Construct failed validation result."""
        # Use context timestamp for determinism (replay-safe)
        validation_timestamp = context.timestamp if context else ""
        
        return InputValidationResult(
            is_valid=False,
            errors=tuple(errors),
            validated_inputs=None,
            schema_versions=FrozenDict({}),
            context_fingerprint=context.fingerprint() if context else "",
            validation_timestamp=validation_timestamp
        )
    
    def _succeed(
        self,
        inputs: dict[str, Any],
        schema_versions: dict[str, str],
        context: PipelineContext
    ) -> InputValidationResult:
        """Construct successful validation result."""
        # Use context timestamp for determinism (replay-safe)
        validation_timestamp = context.timestamp
        
        return InputValidationResult(
            is_valid=True,
            errors=tuple(),
            validated_inputs=FrozenDict(inputs),
            schema_versions=FrozenDict(schema_versions),
            context_fingerprint=context.fingerprint(),
            validation_timestamp=validation_timestamp
        )


# ============================================================================
# INVARIANT ENFORCEMENT
# ============================================================================

class InputValidationInvariants:
    """
    Enforces validation invariants that MUST hold.
    
    INVARIANTS:
    - No execution without full schema validation
    - No partial validation success
    - No silent acceptance of unknown fields
    - No mutation of input payloads
    - No context-free validation
    - No implicit defaults
    
    Violation → pipeline hard stop
    """
    
    @staticmethod
    def enforce_no_mutation(
        original: dict[str, Any],
        after_validation: Optional[FrozenDict]
    ) -> None:
        """Verify inputs were not mutated during validation."""
        if after_validation is None:
            return
        
        original_hash = hash(tuple(sorted(original.items())))
        validated_hash = hash(after_validation)
        
        if original_hash != validated_hash:
            raise RuntimeError(
                "INVARIANT VIOLATION: Input mutation detected during validation. "
                "Validator must never mutate inputs."
            )
    
    @staticmethod
    def enforce_no_partial_success(result: InputValidationResult) -> None:
        """Verify no partial validation occurred."""
        if result.is_valid and len(result.errors) > 0:
            raise RuntimeError(
                "INVARIANT VIOLATION: Partial validation success detected. "
                "Result cannot be valid with errors present."
            )
        
        if not result.is_valid and result.validated_inputs is not None:
            raise RuntimeError(
                "INVARIANT VIOLATION: Invalid result contains validated inputs. "
                "Failed validation must not produce validated inputs."
            )
    
    @staticmethod
    def enforce_context_requirement(context: Optional[PipelineContext]) -> None:
        """Verify context is present."""
        
        if context is None:
            raise RuntimeError(
                "INVARIANT VIOLATION: Context-free validation attempted. "
                "Validation requires PipelineContext."
            )
    
    @staticmethod
    def enforce_all(
        original_inputs: dict[str, Any],
        result: InputValidationResult,
        context: Optional[PipelineContext]
    ) -> None:
        """Enforce all invariants."""
        InputValidationInvariants.enforce_context_requirement(context)
        InputValidationInvariants.enforce_no_partial_success(result)
        InputValidationInvariants.enforce_no_mutation(original_inputs, result.validated_inputs)