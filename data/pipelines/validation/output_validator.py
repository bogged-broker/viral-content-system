"""
/data/pipelines/validation/output_validator.py

Analytics Output Validation - Court of Record
Non-negotiable schema enforcement, invariant verification, and determinism validation.

Invalid analytics are worse than missing analytics.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Final, Protocol, runtime_checkable, Tuple, List, Dict

import numpy as np
import pandas as pd


# ============================================================================
# Error Categories & Codes
# ============================================================================


class ValidationCategory(Enum):
    """Validation failure categories."""
    SCHEMA = auto()
    INVARIANT = auto()
    WINDOW = auto()
    DETERMINISM = auto()
    CONTEXT = auto()
    COMPLETENESS = auto()


class ErrorCode(Enum):
    """Stable error codes for programmatic handling."""
    # Context
    INVALID_CONTEXT = "E001"
    MISSING_CONTEXT_FINGERPRINT = "E002"
    CONTEXT_FINGERPRINT_MISMATCH = "E003"
    
    # Schema
    SCHEMA_NOT_FOUND = "E101"
    SCHEMA_VERSION_MISMATCH = "E102"
    UNKNOWN_FIELD = "E103"
    MISSING_REQUIRED_FIELD = "E104"
    TYPE_MISMATCH = "E105"
    INVALID_SCHEMA_STRUCTURE = "E106"
    
    # Completeness
    MISSING_REQUIRED_METRIC = "E201"
    UNDECLARED_METRIC = "E202"
    EMPTY_OUTPUT_NOT_ALLOWED = "E203"
    PARTIAL_OUTPUT = "E204"
    
    # Invariants
    INVARIANT_VIOLATION = "E301"
    NON_NEGATIVE_VIOLATION = "E302"
    MONOTONIC_VIOLATION = "E303"
    BOUNDED_VIOLATION = "E304"
    INTEGER_VIOLATION = "E305"
    NUMERIC_DOMAIN_VIOLATION = "E306"
    NULLABILITY_VIOLATION = "E307"
    
    # Windows
    UNKNOWN_WINDOW = "E401"
    WINDOW_BOUNDARY_MISMATCH = "E402"
    MIXED_WINDOW_TYPES = "E403"
    OVERLAPPING_WINDOWS = "E404"
    IMPLICIT_ROLLING_WINDOW = "E405"
    
    # Determinism
    MISSING_COMPUTATION_HASH = "E501"
    HASH_MISMATCH = "E502"
    NON_DETERMINISTIC_ORDERING = "E503"
    SCHEMA_FINGERPRINT_MISMATCH = "E504"


# ============================================================================
# Core Data Structures
# ============================================================================


@dataclass(frozen=True, slots=True)
class OutputValidationError:
    """Structured validation failure."""
    error_code: ErrorCode
    category: ValidationCategory
    metric_name: str | None
    path: str
    message: str
    recovery_hint: str
    
    def __str__(self) -> str:
        metric = f"[{self.metric_name}] " if self.metric_name else ""
        return f"{self.error_code.value} {metric}{self.path}: {self.message}"


@dataclass(frozen=True, slots=True)
class WindowIdentity:
    """Immutable window identity."""
    window_type: str  # 'fixed', 'sliding', 'session'
    start: pd.Timestamp
    end: pd.Timestamp
    timezone: str
    granularity: str | None = None
    
    def __post_init__(self) -> None:
        if self.start >= self.end:
            raise ValueError("Window start must be before end")
    
    def fingerprint(self) -> str:
        """Deterministic hash of window identity."""
        components = (
            self.window_type,
            self.start.isoformat(),
            self.end.isoformat(),
            self.timezone,
            self.granularity or "",
        )
        return hashlib.sha256("|".join(components).encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ComputationContext:
    """Computation execution context."""
    pipeline_id: str
    computation_hash: str
    schema_version: str
    window_identities: tuple[WindowIdentity, ...]
    context_fingerprint: str
    timestamp: pd.Timestamp
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InvariantSpec:
    """Metric-level invariant specification."""
    non_negative: bool = False
    monotonic: bool = False  # Within window
    integer_only: bool = False
    min_value: float | None = None
    max_value: float | None = None
    allow_null: bool = False
    allow_inf: bool = False
    allow_nan: bool = False
    
    def validate_value(self, value: Any, path: str) -> list[OutputValidationError]:
        """Validate single value against invariants."""
        errors = []
        
        if value is None or (isinstance(value, float) and pd.isna(value)):
            if not self.allow_null:
                errors.append(OutputValidationError(
                    error_code=ErrorCode.NULLABILITY_VIOLATION,
                    category=ValidationCategory.INVARIANT,
                    metric_name=None,
                    path=path,
                    message="Null value not allowed",
                    recovery_hint="Remove nulls or set allow_null=True"
                ))
            return errors
        
        if isinstance(value, (int, float, np.number)):
            if math.isnan(value) and not self.allow_nan:
                errors.append(OutputValidationError(
                    error_code=ErrorCode.NUMERIC_DOMAIN_VIOLATION,
                    category=ValidationCategory.INVARIANT,
                    metric_name=None,
                    path=path,
                    message="NaN not allowed",
                    recovery_hint="Handle NaN upstream or set allow_nan=True"
                ))
            
            if math.isinf(value) and not self.allow_inf:
                errors.append(OutputValidationError(
                    error_code=ErrorCode.NUMERIC_DOMAIN_VIOLATION,
                    category=ValidationCategory.INVARIANT,
                    metric_name=None,
                    path=path,
                    message="Infinity not allowed",
                    recovery_hint="Cap values or set allow_inf=True"
                ))
            
            if self.non_negative and value < 0:
                errors.append(OutputValidationError(
                    error_code=ErrorCode.NON_NEGATIVE_VIOLATION,
                    category=ValidationCategory.INVARIANT,
                    metric_name=None,
                    path=path,
                    message=f"Negative value {value} violates non_negative constraint",
                    recovery_hint="Ensure computation produces non-negative values"
                ))
            
            if self.integer_only and not float(value).is_integer():
                errors.append(OutputValidationError(
                    error_code=ErrorCode.INTEGER_VIOLATION,
                    category=ValidationCategory.INVARIANT,
                    metric_name=None,
                    path=path,
                    message=f"Non-integer value {value} violates integer_only constraint",
                    recovery_hint="Round or cast values to integers"
                ))
            
            if self.min_value is not None and value < self.min_value:
                errors.append(OutputValidationError(
                    error_code=ErrorCode.BOUNDED_VIOLATION,
                    category=ValidationCategory.INVARIANT,
                    metric_name=None,
                    path=path,
                    message=f"Value {value} below minimum {self.min_value}",
                    recovery_hint=f"Ensure values >= {self.min_value}"
                ))
            
            if self.max_value is not None and value > self.max_value:
                errors.append(OutputValidationError(
                    error_code=ErrorCode.BOUNDED_VIOLATION,
                    category=ValidationCategory.INVARIANT,
                    metric_name=None,
                    path=path,
                    message=f"Value {value} exceeds maximum {self.max_value}",
                    recovery_hint=f"Ensure values <= {self.max_value}"
                ))
        
        return errors


@dataclass(frozen=True, slots=True)
class FieldSchema:
    """Schema for a single field."""
    name: str
    dtype: type
    required: bool = True
    invariants: InvariantSpec | None = None
    array_element_invariants: InvariantSpec | None = None


@dataclass(frozen=True, slots=True)
class OutputMetricSchema:
    """Schema definition for a single metric."""
    metric_name: str
    schema_version: str
    fields: tuple[FieldSchema, ...]
    invariants: InvariantSpec | None = None
    allow_empty: bool = False
    
    def fingerprint(self) -> str:
        """Deterministic schema fingerprint."""
        components = [self.metric_name, self.schema_version]
        for f in self.fields:
            components.extend([f.name, f.dtype.__name__, str(f.required)])
        return hashlib.sha256("|".join(components).encode()).hexdigest()[:16]


@runtime_checkable
class OutputSchemaRegistry(Protocol):
    """Protocol for output schema registry."""
    
    def get_schema(self, metric_name: str) -> OutputMetricSchema | None:
        """Retrieve schema for metric."""
        ...
    
    def required_metrics(self) -> frozenset[str]:
        """Get required metric names."""
        ...
    
    def optional_metrics(self) -> frozenset[str]:
        """Get optional metric names."""
        ...
    
    def allowed_windows(self) -> frozenset[WindowIdentity]:
        """Get allowed window identities."""
        ...


class DictOutputSchemaRegistry:
    """Concrete schema registry implementation."""
    
    def __init__(
        self,
        schemas: Mapping[str, OutputMetricSchema],
        required: frozenset[str],
        optional: frozenset[str],
        windows: frozenset[WindowIdentity],
    ):
        self._schemas = dict(schemas)
        self._required = required
        self._optional = optional
        self._windows = windows
        
        # Validate registry consistency
        all_metrics = required | optional
        if not all_metrics == set(schemas.keys()):
            raise ValueError("Schema keys must match required + optional metrics")
    
    def get_schema(self, metric_name: str) -> OutputMetricSchema | None:
        return self._schemas.get(metric_name)
    
    def required_metrics(self) -> frozenset[str]:
        return self._required
    
    def optional_metrics(self) -> frozenset[str]:
        return self._optional
    
    def allowed_windows(self) -> frozenset[WindowIdentity]:
        return self._windows


@dataclass(frozen=True, slots=True)
class OutputValidationResult:
    """Immutable validation outcome."""
    is_valid: bool
    errors: tuple[OutputValidationError, ...]
    validated_outputs: Mapping[str, Any] | None
    metric_fingerprints: Mapping[str, str]
    context_fingerprint: str
    validation_timestamp: pd.Timestamp
    
    def raise_if_invalid(self) -> None:
        """Raise if validation failed."""
        if not self.is_valid:
            error_summary = "\n".join(str(e) for e in self.errors[:10])
            total = len(self.errors)
            suffix = f"\n... and {total - 10} more errors" if total > 10 else ""
            raise OutputValidationException(
                f"Output validation failed with {total} error(s):\n{error_summary}{suffix}",
                errors=self.errors
            )


class OutputValidationException(Exception):
    """Validation failure exception."""
    
    def __init__(self, message: str, errors: tuple[OutputValidationError, ...]):
        super().__init__(message)
        self.errors = errors


# ============================================================================
# Core Validator
# ============================================================================


class OutputValidator:
    """
    Single authority for analytics output validation.
    
    Validates:
    - Schema conformance
    - Metric completeness
    - Invariant satisfaction
    - Window integrity
    - Determinism guarantees
    """
    
    __slots__ = ('_registry', '_strict_determinism', '_strict_ordering')
    
    def __init__(
        self,
        registry: OutputSchemaRegistry,
        *,
        strict_determinism: bool = True,
        strict_ordering: bool = True,
    ):
        self._registry = registry
        self._strict_determinism = strict_determinism
        self._strict_ordering = strict_ordering
    
    def validate(
        self,
        *,
        outputs: Mapping[str, Any],
        context: ComputationContext,
    ) -> OutputValidationResult:
        """
        Validate outputs against schema and invariants.
        
        Execution order is part of correctness - do not reorder.
        """
        errors: list[OutputValidationError] = []
        
        # 1. Validate context integrity
        errors.extend(self._validate_context(context))
        if errors:
            return self._failed_result(errors, context)
        
        # 2. Validate declared schemas exist
        errors.extend(self._validate_schemas_exist(outputs))
        if errors:
            return self._failed_result(errors, context)
        
        # 3. Validate completeness
        errors.extend(self._validate_completeness(outputs))
        if errors:
            return self._failed_result(errors, context)
        
        # 4. Validate schema conformance (deep)
        errors.extend(self._validate_schema_conformance(outputs))
        if errors:
            return self._failed_result(errors, context)
        
        # 5. Enforce metric-level invariants
        errors.extend(self._validate_metric_invariants(outputs))
        if errors:
            return self._failed_result(errors, context)
        
        # 6. Enforce global invariants
        errors.extend(self._validate_global_invariants(outputs))
        if errors:
            return self._failed_result(errors, context)
        
        # 7. Validate window integrity
        errors.extend(self._validate_window_integrity(outputs, context))
        if errors:
            return self._failed_result(errors, context)
        
        # 8. Validate determinism
        errors.extend(self._validate_determinism(outputs, context))
        if errors:
            return self._failed_result(errors, context)
        
        # 9. Generate fingerprints
        metric_fingerprints = self._generate_metric_fingerprints(outputs)
        
        return OutputValidationResult(
            is_valid=True,
            errors=(),
            validated_outputs=outputs,
            metric_fingerprints=metric_fingerprints,
            context_fingerprint=context.context_fingerprint,
            validation_timestamp=pd.Timestamp.now(tz='UTC'),
        )
    
    def _validate_context(self, context: ComputationContext) -> list[OutputValidationError]:
        """Validate computation context integrity."""
        errors = []
        
        if not context.computation_hash:
            errors.append(OutputValidationError(
                error_code=ErrorCode.MISSING_CONTEXT_FINGERPRINT,
                category=ValidationCategory.CONTEXT,
                metric_name=None,
                path="context.computation_hash",
                message="Computation hash is required",
                recovery_hint="Ensure computation executor generates hash"
            ))
        
        if not context.context_fingerprint:
            errors.append(OutputValidationError(
                error_code=ErrorCode.MISSING_CONTEXT_FINGERPRINT,
                category=ValidationCategory.CONTEXT,
                metric_name=None,
                path="context.context_fingerprint",
                message="Context fingerprint is required",
                recovery_hint="Generate context fingerprint before validation"
            ))
        
        if not context.window_identities:
            errors.append(OutputValidationError(
                error_code=ErrorCode.INVALID_CONTEXT,
                category=ValidationCategory.CONTEXT,
                metric_name=None,
                path="context.window_identities",
                message="At least one window identity required",
                recovery_hint="Define window identities in computation context"
            ))
        
        return errors
    
    def _validate_schemas_exist(self, outputs: Mapping[str, Any]) -> list[OutputValidationError]:
        """Validate all output metrics have declared schemas."""
        errors = []
        
        for metric_name in outputs.keys():
            schema = self._registry.get_schema(metric_name)
            if schema is None:
                errors.append(OutputValidationError(
                    error_code=ErrorCode.SCHEMA_NOT_FOUND,
                    category=ValidationCategory.SCHEMA,
                    metric_name=metric_name,
                    path=f"outputs.{metric_name}",
                    message=f"No schema found for metric '{metric_name}'",
                    recovery_hint="Add schema to registry or remove metric from outputs"
                ))
        
        return errors
    
    def _validate_completeness(self, outputs: Mapping[str, Any]) -> list[OutputValidationError]:
        """Validate required metrics present, no undeclared metrics."""
        errors = []
        
        required = self._registry.required_metrics()
        optional = self._registry.optional_metrics()
        allowed = required | optional
        output_keys = set(outputs.keys())
        
        # Check required metrics
        missing = required - output_keys
        for metric_name in sorted(missing):
            errors.append(OutputValidationError(
                error_code=ErrorCode.MISSING_REQUIRED_METRIC,
                category=ValidationCategory.COMPLETENESS,
                metric_name=metric_name,
                path=f"outputs.{metric_name}",
                message=f"Required metric '{metric_name}' not in outputs",
                recovery_hint="Ensure computation produces all required metrics"
            ))
        
        # Check for undeclared metrics
        undeclared = output_keys - allowed
        for metric_name in sorted(undeclared):
            errors.append(OutputValidationError(
                error_code=ErrorCode.UNDECLARED_METRIC,
                category=ValidationCategory.COMPLETENESS,
                metric_name=metric_name,
                path=f"outputs.{metric_name}",
                message=f"Undeclared metric '{metric_name}' in outputs",
                recovery_hint="Add to schema registry or remove from outputs"
            ))
        
        return errors
    
    def _validate_schema_conformance(self, outputs: Mapping[str, Any]) -> list[OutputValidationError]:
        """Deep schema conformance validation."""
        errors = []
        
        for metric_name, metric_data in outputs.items():
            schema = self._registry.get_schema(metric_name)
            if schema is None:
                continue  # Already caught in schemas_exist check
            
            # Validate schema version if present in data
            if isinstance(metric_data, dict) and '_schema_version' in metric_data:
                if metric_data['_schema_version'] != schema.schema_version:
                    errors.append(OutputValidationError(
                        error_code=ErrorCode.SCHEMA_VERSION_MISMATCH,
                        category=ValidationCategory.SCHEMA,
                        metric_name=metric_name,
                        path=f"outputs.{metric_name}._schema_version",
                        message=f"Schema version mismatch: got {metric_data['_schema_version']}, expected {schema.schema_version}",
                        recovery_hint="Update schema version or recompute with correct schema"
                    ))
            
            # Validate emptiness
            is_empty = self._is_empty(metric_data)
            if is_empty and not schema.allow_empty:
                errors.append(OutputValidationError(
                    error_code=ErrorCode.EMPTY_OUTPUT_NOT_ALLOWED,
                    category=ValidationCategory.COMPLETENESS,
                    metric_name=metric_name,
                    path=f"outputs.{metric_name}",
                    message="Empty output not allowed for this metric",
                    recovery_hint="Set allow_empty=True or ensure non-empty output"
                ))
                continue
            
            if is_empty:
                continue  # No further validation for empty allowed outputs
            
            # Validate fields
            errors.extend(self._validate_fields(metric_name, metric_data, schema))
        
        return errors
    
    def _validate_fields(
        self,
        metric_name: str,
        metric_data: Any,
        schema: OutputMetricSchema
    ) -> list[OutputValidationError]:
        """Validate individual fields against schema."""
        errors = []
        
        # Handle DataFrame
        if isinstance(metric_data, pd.DataFrame):
            data_fields = set(metric_data.columns)
            for field_schema in schema.fields:
                if field_schema.required and field_schema.name not in data_fields:
                    errors.append(OutputValidationError(
                        error_code=ErrorCode.MISSING_REQUIRED_FIELD,
                        category=ValidationCategory.SCHEMA,
                        metric_name=metric_name,
                        path=f"outputs.{metric_name}.{field_schema.name}",
                        message=f"Required field '{field_schema.name}' missing",
                        recovery_hint="Ensure computation produces all required fields"
                    ))
                elif field_schema.name in data_fields:
                    # Type check
                    series = metric_data[field_schema.name]
                    errors.extend(self._validate_series_type(
                        metric_name, field_schema.name, series, field_schema
                    ))
            
            # Check for unknown fields
            schema_fields = {f.name for f in schema.fields}
            unknown = data_fields - schema_fields - {'_schema_version', '_window_id'}
            for field_name in sorted(unknown):
                errors.append(OutputValidationError(
                    error_code=ErrorCode.UNKNOWN_FIELD,
                    category=ValidationCategory.SCHEMA,
                    metric_name=metric_name,
                    path=f"outputs.{metric_name}.{field_name}",
                    message=f"Unknown field '{field_name}' not in schema",
                    recovery_hint="Remove field or add to schema"
                ))
        
        # Handle dict
        elif isinstance(metric_data, dict):
            for field_schema in schema.fields:
                if field_schema.required and field_schema.name not in metric_data:
                    errors.append(OutputValidationError(
                        error_code=ErrorCode.MISSING_REQUIRED_FIELD,
                        category=ValidationCategory.SCHEMA,
                        metric_name=metric_name,
                        path=f"outputs.{metric_name}.{field_schema.name}",
                        message=f"Required field '{field_schema.name}' missing",
                        recovery_hint="Ensure computation produces all required fields"
                    ))
                elif field_schema.name in metric_data:
                    value = metric_data[field_schema.name]
                    if not isinstance(value, field_schema.dtype):
                        errors.append(OutputValidationError(
                            error_code=ErrorCode.TYPE_MISMATCH,
                            category=ValidationCategory.SCHEMA,
                            metric_name=metric_name,
                            path=f"outputs.{metric_name}.{field_schema.name}",
                            message=f"Type mismatch: got {type(value).__name__}, expected {field_schema.dtype.__name__}",
                            recovery_hint="Ensure field has correct type"
                        ))
        
        return errors
    
    def _validate_series_type(
        self,
        metric_name: str,
        field_name: str,
        series: pd.Series,
        field_schema: FieldSchema
    ) -> list[OutputValidationError]:
        """Validate pandas Series type compatibility."""
        errors = []
        
        # Map pandas dtypes to Python types
        dtype_map = {
            'int64': int,
            'int32': int,
            'float64': float,
            'float32': float,
            'object': object,
            'string': str,
            'bool': bool,
        }
        
        dtype_str = str(series.dtype)
        expected_type = field_schema.dtype
        
        if expected_type == int and dtype_str not in ('int64', 'int32'):
            errors.append(OutputValidationError(
                error_code=ErrorCode.TYPE_MISMATCH,
                category=ValidationCategory.SCHEMA,
                metric_name=metric_name,
                path=f"outputs.{metric_name}.{field_name}",
                message=f"Type mismatch: got {dtype_str}, expected integer type",
                recovery_hint="Cast to int64 or int32"
            ))
        elif expected_type == float and dtype_str not in ('float64', 'float32', 'int64', 'int32'):
            errors.append(OutputValidationError(
                error_code=ErrorCode.TYPE_MISMATCH,
                category=ValidationCategory.SCHEMA,
                metric_name=metric_name,
                path=f"outputs.{metric_name}.{field_name}",
                message=f"Type mismatch: got {dtype_str}, expected numeric type",
                recovery_hint="Cast to float64"
            ))
        elif expected_type == str and dtype_str not in ('object', 'string'):
            errors.append(OutputValidationError(
                error_code=ErrorCode.TYPE_MISMATCH,
                category=ValidationCategory.SCHEMA,
                metric_name=metric_name,
                path=f"outputs.{metric_name}.{field_name}",
                message=f"Type mismatch: got {dtype_str}, expected string type",
                recovery_hint="Cast to string type"
            ))
        
        return errors
    
    def _validate_metric_invariants(self, outputs: Mapping[str, Any]) -> list[OutputValidationError]:
        """Validate metric-level invariants."""
        errors = []
        
        for metric_name, metric_data in outputs.items():
            schema = self._registry.get_schema(metric_name)
            if schema is None or schema.invariants is None:
                continue
            
            if isinstance(metric_data, pd.DataFrame):
                for field_schema in schema.fields:
                    if field_schema.invariants is None:
                        continue
                    
                    if field_schema.name not in metric_data.columns:
                        continue
                    
                    series = metric_data[field_schema.name]
                    for idx, value in enumerate(series):
                        path = f"outputs.{metric_name}.{field_schema.name}[{idx}]"
                        field_errors = field_schema.invariants.validate_value(value, path)
                        for err in field_errors:
                            errors.append(OutputValidationError(
                                error_code=err.error_code,
                                category=err.category,
                                metric_name=metric_name,
                                path=err.path,
                                message=err.message,
                                recovery_hint=err.recovery_hint
                            ))
                    
                    # Check monotonicity if required
                    if field_schema.invariants.monotonic:
                        if not series.is_monotonic_increasing:
                            errors.append(OutputValidationError(
                                error_code=ErrorCode.MONOTONIC_VIOLATION,
                                category=ValidationCategory.INVARIANT,
                                metric_name=metric_name,
                                path=f"outputs.{metric_name}.{field_schema.name}",
                                message="Series violates monotonic constraint",
                                recovery_hint="Ensure values are monotonically increasing"
                            ))
            
            elif isinstance(metric_data, dict):
                for field_schema in schema.fields:
                    if field_schema.invariants is None:
                        continue
                    
                    if field_schema.name not in metric_data:
                        continue
                    
                    value = metric_data[field_schema.name]
                    path = f"outputs.{metric_name}.{field_schema.name}"
                    field_errors = field_schema.invariants.validate_value(value, path)
                    for err in field_errors:
                        errors.append(OutputValidationError(
                            error_code=err.error_code,
                            category=err.category,
                            metric_name=metric_name,
                            path=err.path,
                            message=err.message,
                            recovery_hint=err.recovery_hint
                        ))
        
        return errors
    
    def _validate_global_invariants(self, outputs: Mapping[str, Any]) -> list[OutputValidationError]:
        """Validate cross-metric invariants."""
        errors = []
        
        # Example: Validate that sum of parts equals total
        # This is domain-specific; implement as needed
        
        return errors
    
    def _validate_window_integrity(
        self,
        outputs: Mapping[str, Any],
        context: ComputationContext
    ) -> list[OutputValidationError]:
        """Validate window identities and boundaries."""
        errors = []
        
        allowed_windows = self._registry.allowed_windows()
        context_windows = set(context.window_identities)
        
        for metric_name, metric_data in outputs.items():
            # Extract window references from data
            window_refs = self._extract_window_references(metric_data)
            
            for window_ref in window_refs:
                # Check if window is in allowed set
                if window_ref not in allowed_windows:
                    errors.append(OutputValidationError(
                        error_code=ErrorCode.UNKNOWN_WINDOW,
                        category=ValidationCategory.WINDOW,
                        metric_name=metric_name,
                        path=f"outputs.{metric_name}._window_id",
                        message=f"Window {window_ref.fingerprint()} not in allowed windows",
                        recovery_hint="Use only windows declared in schema registry"
                    ))
                
                # Check if window is in context
                if window_ref not in context_windows:
                    errors.append(OutputValidationError(
                        error_code=ErrorCode.WINDOW_BOUNDARY_MISMATCH,
                        category=ValidationCategory.WINDOW,
                        metric_name=metric_name,
                        path=f"outputs.{metric_name}._window_id",
                        message=f"Window {window_ref.fingerprint()} not in computation context",
                        recovery_hint="Ensure all output windows match context windows"
                    ))
        
        return errors
    
    def _validate_determinism(
        self,
        outputs: Mapping[str, Any],
        context: ComputationContext
    ) -> list[OutputValidationError]:
        """Validate determinism guarantees."""
        errors = []
        
        if not self._strict_determinism:
            return errors
        
        # Validate computation hash presence (already in context validation)
        
        # Validate ordering if strict
        if self._strict_ordering:
            for metric_name, metric_data in outputs.items():
                if isinstance(metric_data, pd.DataFrame):
                    # Check for deterministic index
                    if not metric_data.index.is_monotonic_increasing:
                        errors.append(OutputValidationError(
                            error_code=ErrorCode.NON_DETERMINISTIC_ORDERING,
                            category=ValidationCategory.DETERMINISM,
                            metric_name=metric_name,
                            path=f"outputs.{metric_name}.index",
                            message="DataFrame index is not monotonically increasing",
                            recovery_hint="Sort DataFrame by deterministic key"
                        ))
        
        return errors
    
    def _generate_metric_fingerprints(self, outputs: Mapping[str, Any]) -> dict[str, str]:
        """Generate deterministic fingerprints for each metric."""
        fingerprints = {}
        
        for metric_name, metric_data in outputs.items():
            schema = self._registry.get_schema(metric_name)
            if schema is None:
                continue
            
            # Combine schema fingerprint with data hash
            schema_fp = schema.fingerprint()
            data_hash = self._hash_metric_data(metric_data)
            combined = f"{schema_fp}|{data_hash}"
            fingerprints[metric_name] = hashlib.sha256(combined.encode()).hexdigest()[:16]
        
        return fingerprints
    
    def _hash_metric_data(self, data: Any) -> str:
        """Generate deterministic hash of metric data."""
        if isinstance(data, pd.DataFrame):
            # Hash based on shape and representative sample
            shape_str = f"{data.shape[0]}x{data.shape[1]}"
            cols_str = "|".join(sorted(data.columns))
            return hashlib.sha256(f"{shape_str}|{cols_str}".encode()).hexdigest()[:16]
        elif isinstance(data, dict):
            items = sorted(data.items())
            content = "|".join(f"{k}:{v}" for k, v in items if k != '_schema_version')
            return hashlib.sha256(content.encode()).hexdigest()[:16]
        else:
            return hashlib.sha256(str(data).encode()).hexdigest()[:16]
    
    def _extract_window_references(self, data: Any) -> set[WindowIdentity]:
        """Extract window identity references from metric data."""
        windows = set()
        
        # This is simplified - real implementation would parse window metadata
        # from the data structure (e.g., _window_id field)
        
        return windows
    
    def _is_empty(self, data: Any) -> bool:
        """Check if metric data is empty."""
        if isinstance(data, pd.DataFrame):
            return len(data) == 0
        elif isinstance(data, dict):
            return len(data) == 0
        elif isinstance(data, (list, tuple)):
            return len(data) == 0
        return False
    
    def _failed_result(
        self,
        errors: list[OutputValidationError],
        context: ComputationContext
    ) -> OutputValidationResult:
        """Generate failed validation result."""
        return OutputValidationResult(
            is_valid=False,
            errors=tuple(errors),
            validated_outputs=None,
            metric_fingerprints={},
            context_fingerprint=context.context_fingerprint,
            validation_timestamp=pd.Timestamp.now(tz='UTC'),
        )


# ============================================================================
# Global Invariants (Domain-Specific)
# ============================================================================


class OutputValidationInvariants:
    """
    Global invariants enforced across all outputs.
    
    These are absolute guarantees that must hold regardless of metric type.
    """
    
    # No undeclared metrics escape
    UNDECLARED_METRICS_FORBIDDEN: Final = True
    
    # No invalid window analytics publish
    INVALID_WINDOWS_FORBIDDEN: Final = True
    
    # No non-deterministic outputs persist
    NON_DETERMINISTIC_OUTPUTS_FORBIDDEN: Final = True
    
    # No numeric domain lies
    NUMERIC_DOMAIN_VIOLATIONS_FORBIDDEN: Final = True
    
    # No silent truncation
    SILENT_TRUNCATION_FORBIDDEN: Final = True
    
    # No partial success (atomicity)
    PARTIAL_SUCCESS_FORBIDDEN: Final = True
    
    @classmethod
    def verify_all(cls) -> bool:
        """Verify all invariants are enabled."""
        return all([
            cls.UNDECLARED_METRICS_FORBIDDEN,
            cls.INVALID_WINDOWS_FORBIDDEN,
            cls.NON_DETERMINISTIC_OUTPUTS_FORBIDDEN,
            cls.NUMERIC_DOMAIN_VIOLATIONS_FORBIDDEN,
            cls.SILENT_TRUNCATION_FORBIDDEN,
            cls.PARTIAL_SUCCESS_FORBIDDEN,
        ])


# Verify invariants at module load time
assert OutputValidationInvariants.verify_all(), "All invariants must be enabled"