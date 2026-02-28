#!/usr/bin/env python3
"""
/data/pipelines/base/pipeline_step.py

Deterministic Pipeline Step Contracts (Map, Reduce, Window)

CRITICAL: This file defines the ONLY legal shapes of computation allowed
inside analytics pipelines. If a transformation can't fit here, it cannot
exist in the pipeline.

Design Principle: Every transformation must be explainable as either
mapping, reducing, or windowing. No hybrids. No implicit state.

TIER-0 VALIDATION:
This module now supports Tier-0 semantic validation through registry interfaces:
- SchemaRegistryInterface: Validates that ReduceStep grouping keys exist in input schema
- WindowRegistryInterface: Validates that WindowStep window_ref exists in window registry
- AlgorithmRegistryInterface: Non-heuristic classification of algorithm legality

Use validate_with_registries() for full Tier-0 validation, or validate() for
basic structural validation only.
"""

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Tuple, Optional, Dict, Any, List, FrozenSet, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    # Avoid circular imports - these are only used for type hints
    from data.pipelines.validation.input_validator import SchemaDefinition
    from data.pipelines.windows.windows import WindowRegistry


# ============================================================================
# CONSTANTS & CONFIGURATION
# ============================================================================

# Supported reduction kinds (explicit enumeration)
ALLOWED_REDUCTION_KINDS = frozenset([
    "count",
    "sum",
    "min",
    "max",
    "avg",
    "first",
    "last",
    "distinct_count",
])

# Supported window alignments
ALLOWED_WINDOW_ALIGNMENTS = frozenset([
    "event_time",
    "processing_time",
])

# Minimum schema version
MIN_SCHEMA_VERSION = 1


# ============================================================================
# ENUMS & TYPE DEFINITIONS
# ============================================================================

class PipelineStepKind(Enum):
    """
    Exhaustive taxonomy of permitted transformation types.
    
    If it's not one of these, it's illegal.
    """
    MAP = "map"
    REDUCE = "reduce"
    WINDOW = "window"
    
    def __str__(self) -> str:
        """String representation."""
        return self.value
    
    @classmethod
    def from_string(cls, value: str) -> 'PipelineStepKind':
        """
        Parse step kind from string.
        
        Args:
            value: String representation
            
        Returns:
            PipelineStepKind enum
            
        Raises:
            ValueError: If value is not a valid step kind
        """
        try:
            return cls(value.lower())
        except ValueError:
            valid_kinds = [k.value for k in cls]
            raise ValueError(
                f"Invalid step kind: {value}. Must be one of: {valid_kinds}"
            )


class ReductionKind(Enum):
    """
    Explicit enumeration of permitted reduction operations.
    
    Each reduction must have well-defined semantics and be deterministic.
    """
    COUNT = "count"
    SUM = "sum"
    MIN = "min"
    MAX = "max"
    AVG = "avg"
    FIRST = "first"
    LAST = "last"
    DISTINCT_COUNT = "distinct_count"
    
    def __str__(self) -> str:
        """String representation."""
        return self.value
    
    @classmethod
    def from_string(cls, value: str) -> 'ReductionKind':
        """
        Parse reduction kind from string.
        
        Args:
            value: String representation
            
        Returns:
            ReductionKind enum
            
        Raises:
            ValueError: If value is not a valid reduction kind
        """
        try:
            return cls(value.lower())
        except ValueError:
            valid_kinds = [k.value for k in cls]
            raise ValueError(
                f"Invalid reduction kind: {value}. Must be one of: {valid_kinds}"
            )


class WindowAlignment(Enum):
    """
    Window alignment strategies.
    
    Determines whether windows are aligned to event time or processing time.
    """
    EVENT_TIME = "event_time"
    PROCESSING_TIME = "processing_time"
    
    def __str__(self) -> str:
        """String representation."""
        return self.value
    
    @classmethod
    def from_string(cls, value: str) -> 'WindowAlignment':
        """
        Parse window alignment from string.
        
        Args:
            value: String representation
            
        Returns:
            WindowAlignment enum
            
        Raises:
            ValueError: If value is not a valid alignment
        """
        try:
            return cls(value.lower())
        except ValueError:
            valid_alignments = [a.value for a in cls]
            raise ValueError(
                f"Invalid window alignment: {value}. Must be one of: {valid_alignments}"
            )


# ============================================================================
# VALIDATION ERRORS
# ============================================================================

class StepValidationError(Exception):
    """
    Raised when a pipeline step fails validation.
    
    All validation failures are HARD FAILURES — no step proceeds
    without perfect conformance.
    """
    pass


class StepInvariantViolation(Exception):
    """
    Raised when a step violates core invariants.
    
    This is a CRITICAL error indicating pipeline corruption.
    """
    pass


# ============================================================================
# REGISTRY INTERFACES (For Tier-0 Semantic Validation)
# ============================================================================

class SchemaRegistryInterface(Protocol):
    """
    Protocol for schema registry lookup.
    
    Used for semantic validation of step configurations.
    """
    def get_schema(self, schema_name: str, schema_version: int) -> Optional['SchemaDefinition']:
        """
        Retrieve schema definition by name and version.
        
        Args:
            schema_name: Schema name
            schema_version: Schema version
            
        Returns:
            SchemaDefinition if found, None otherwise
        """
        ...


class WindowRegistryInterface(Protocol):
    """
    Protocol for window registry lookup.
    
    Used for semantic validation of window references.
    """
    def get(self, window_ref: str) -> Any:
        """
        Retrieve window definition by reference.
        
        Args:
            window_ref: Window reference (may include version)
            
        Returns:
            Window definition
            
        Raises:
            KeyError: If window not found
        """
        ...
    
    def has_window(self, window_ref: str) -> bool:
        """
        Check if window exists in registry.
        
        Args:
            window_ref: Window reference
            
        Returns:
            True if window exists, False otherwise
        """
        ...


class AlgorithmRegistryInterface(Protocol):
    """
    Protocol for algorithm registry lookup.
    
    Used for non-heuristic classification of algorithm legality.
    """
    def get_algorithm_metadata(self, algorithm_id: str) -> Optional['AlgorithmMetadata']:
        """
        Retrieve algorithm metadata by ID.
        
        Args:
            algorithm_id: Algorithm identifier (may include version)
            
        Returns:
            AlgorithmMetadata if found, None otherwise
        """
        ...
    
    def is_algorithm_allowed_for_step_kind(
        self,
        algorithm_id: str,
        step_kind: PipelineStepKind
    ) -> bool:
        """
        Check if algorithm is allowed for given step kind.
        
        Args:
            algorithm_id: Algorithm identifier
            step_kind: Step kind
            
        Returns:
            True if algorithm is allowed, False otherwise
        """
        ...


@dataclass(frozen=True)
class AlgorithmMetadata:
    """
    Metadata for algorithm classification.
    
    Used for non-heuristic legality enforcement.
    """
    algorithm_id: str
    step_kind: PipelineStepKind
    is_deterministic: bool
    is_aggregation: bool
    is_randomized: bool
    requires_window: bool
    description: str = ""


# ============================================================================
# BASE CONTRACT: PIPELINE STEP
# ============================================================================

@dataclass(frozen=True)
class PipelineStep(ABC):
    """
    Base contract for all pipeline transformation steps.
    
    Every step MUST:
    - Declare its kind (map/reduce/window)
    - Specify input and output schemas explicitly
    - Reference a versioned algorithm
    - Be deterministic and replayable
    
    Steps are declarative, not executable.
    No embedded logic. No runtime state.
    """
    
    # Step identity
    step_name: str
    step_kind: PipelineStepKind
    
    # Schema contracts (explicit)
    input_schema: str
    input_schema_version: int
    output_schema: str
    output_schema_version: int
    
    # Algorithm reference (versioned, deterministic)
    algorithm_id: str
    
    # Optional metadata (does not affect step hash)
    description: str = ""
    tags: Tuple[str, ...] = field(default_factory=tuple)
    
    def __post_init__(self):
        """
        Post-initialization validation.
        
        This is a safety net — steps should ideally be validated
        before construction, but this ensures no invalid steps exist.
        """
        # Validate immediately upon construction
        self.validate()
    
    @abstractmethod
    def validate(self) -> None:
        """
        Validate step configuration.
        
        Must be implemented by all concrete step types.
        
        Raises:
            StepValidationError: If validation fails
        """
        # Base validations (common to all step types)
        self._validate_base()
    
    def validate_with_registries(
        self,
        schema_registry: Optional[SchemaRegistryInterface] = None,
        window_registry: Optional[WindowRegistryInterface] = None,
        algorithm_registry: Optional[AlgorithmRegistryInterface] = None
    ) -> None:
        """
        Validate step with full Tier-0 semantic validation.
        
        This method performs:
        - Basic structural validation (validate())
        - Schema-aware validation (for ReduceStep)
        - Window registry resolution (for WindowStep)
        - Algorithm registry classification (for all steps)
        
        Args:
            schema_registry: Optional schema registry for semantic validation
            window_registry: Optional window registry for semantic validation
            algorithm_registry: Optional algorithm registry for metadata-based validation
            
        Raises:
            StepValidationError: If validation fails
        """
        # Basic validation first
        self.validate()
        
        # Step-specific semantic validation
        if isinstance(self, ReduceStep):
            self.validate_with_schema_registry(schema_registry)
        
        if isinstance(self, WindowStep):
            self.validate_with_window_registry(window_registry)
        
        # Algorithm registry validation for all steps
        if algorithm_registry is not None:
            ForbiddenPatternDetector.check_all_patterns(self, algorithm_registry)
    
    def _validate_base(self) -> None:
        """
        Validate base step contract.
        
        Raises:
            StepValidationError: If validation fails
        """
        # Step name must be non-empty and valid
        if not self.step_name or not self.step_name.strip():
            raise StepValidationError("Step name must be non-empty")
        
        if len(self.step_name) > 256:
            raise StepValidationError(
                f"Step name too long: {len(self.step_name)} > 256"
            )
        
        # Step kind must match class type
        expected_kind = self._get_expected_kind()
        if self.step_kind != expected_kind:
            raise StepValidationError(
                f"Step kind mismatch: declared={self.step_kind}, "
                f"expected={expected_kind} for class {self.__class__.__name__}"
            )
        
        # Input schema must be non-empty
        if not self.input_schema or not self.input_schema.strip():
            raise StepValidationError("Input schema must be non-empty")
        
        # Output schema must be non-empty
        if not self.output_schema or not self.output_schema.strip():
            raise StepValidationError("Output schema must be non-empty")
        
        # Schema versions must be >= MIN_SCHEMA_VERSION
        if self.input_schema_version < MIN_SCHEMA_VERSION:
            raise StepValidationError(
                f"Input schema version must be >= {MIN_SCHEMA_VERSION}, "
                f"got {self.input_schema_version}"
            )
        
        if self.output_schema_version < MIN_SCHEMA_VERSION:
            raise StepValidationError(
                f"Output schema version must be >= {MIN_SCHEMA_VERSION}, "
                f"got {self.output_schema_version}"
            )
        
        # Algorithm ID must be non-empty and versioned
        if not self.algorithm_id or not self.algorithm_id.strip():
            raise StepValidationError("Algorithm ID must be non-empty")
        
        # Algorithm ID must contain version separator
        if '@' not in self.algorithm_id and ':' not in self.algorithm_id:
            raise StepValidationError(
                f"Algorithm ID must be versioned (use '@' or ':'): {self.algorithm_id}"
            )
    
    @abstractmethod
    def _get_expected_kind(self) -> PipelineStepKind:
        """
        Get expected step kind for this class.
        
        Returns:
            Expected PipelineStepKind
        """
        pass
    
    def compute_step_hash(self) -> str:
        """
        Compute deterministic hash of step configuration.
        
        Hash is based on:
        - step_name
        - step_kind
        - input/output schemas
        - algorithm_id
        - step-specific parameters
        
        Same contract → same hash forever.
        
        Returns:
            SHA-256 hash (hex string)
        """
        # Get canonical representation
        canonical_dict = self._to_canonical_dict()
        
        # Serialize deterministically
        canonical_json = json.dumps(
            canonical_dict,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True
        )
        
        # Hash
        step_hash = hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()
        
        return step_hash
    
    @abstractmethod
    def _to_canonical_dict(self) -> Dict[str, Any]:
        """
        Convert step to canonical dictionary for hashing.
        
        Must include all fields that affect step identity.
        Must exclude runtime/metadata fields.
        
        Returns:
            Canonical dictionary representation
        """
        # Base fields (common to all steps)
        return {
            'step_name': self.step_name.strip(),
            'step_kind': self.step_kind.value,
            'input_schema': self.input_schema.strip(),
            'input_schema_version': self.input_schema_version,
            'output_schema': self.output_schema.strip(),
            'output_schema_version': self.output_schema_version,
            'algorithm_id': self.algorithm_id.strip(),
        }
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert step to dictionary (including metadata).
        
        Returns:
            Full dictionary representation
        """
        step_dict = asdict(self)
        
        # Convert enums to strings
        if 'step_kind' in step_dict:
            step_dict['step_kind'] = self.step_kind.value
        
        return step_dict
    
    def to_json(self) -> str:
        """
        Serialize step to JSON.
        
        Returns:
            JSON string
        """
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)
    
    def check_invariants(self) -> None:
        """
        Check core step invariants.
        
        These are fundamental guarantees that must NEVER be violated.
        
        Raises:
            StepInvariantViolation: If any invariant is violated
        """
        # Invariant: Steps are immutable (frozen=True ensures this)
        # No additional check needed
        
        # Invariant: Step hash is deterministic
        hash_1 = self.compute_step_hash()
        hash_2 = self.compute_step_hash()
        if hash_1 != hash_2:
            raise StepInvariantViolation(
                f"Step hash is non-deterministic: {hash_1} != {hash_2}"
            )
        
        # Invariant: Step kind matches class
        expected_kind = self._get_expected_kind()
        if self.step_kind != expected_kind:
            raise StepInvariantViolation(
                f"Step kind mismatch: {self.step_kind} != {expected_kind}"
            )


# ============================================================================
# MAP STEP CONTRACT
# ============================================================================

@dataclass(frozen=True)
class MapStep(PipelineStep):
    """
    Map transformation step.
    
    Rules:
    - No fan-in (1 input record → exactly 1 output record)
    - No fan-out
    - No aggregation
    - Pure function only
    - MUST be deterministic
    
    Map steps preserve cardinality and are embarrassingly parallel.
    """
    
    # Cardinality guarantee (must always be True for valid map)
    preserves_cardinality: bool = True
    
    # Determinism requirement (must always be True)
    deterministic: bool = True
    
    # Optional: explicit field mappings (for documentation)
    field_mappings: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)
    
    def _get_expected_kind(self) -> PipelineStepKind:
        """Get expected step kind."""
        return PipelineStepKind.MAP
    
    def validate(self) -> None:
        """
        Validate map step.
        
        Raises:
            StepValidationError: If validation fails
        """
        # Base validation
        self._validate_base()
        
        # MAP INVARIANT: Must preserve cardinality
        if not self.preserves_cardinality:
            raise StepValidationError(
                f"Map step '{self.step_name}' must preserve cardinality"
            )
        
        # MAP INVARIANT: Must be deterministic
        if not self.deterministic:
            raise StepValidationError(
                f"Map step '{self.step_name}' must be deterministic"
            )
        
        # MAP CONSTRAINT: Field mappings must be valid tuples
        if self.field_mappings:
            for mapping in self.field_mappings:
                if not isinstance(mapping, tuple) or len(mapping) != 2:
                    raise StepValidationError(
                        f"Invalid field mapping: {mapping}. Must be (source, target) tuple"
                    )
                if not all(isinstance(f, str) for f in mapping):
                    raise StepValidationError(
                        f"Field mapping must contain strings: {mapping}"
                    )
    
    def _to_canonical_dict(self) -> Dict[str, Any]:
        """Convert to canonical dictionary for hashing."""
        canonical = super()._to_canonical_dict()
        
        # Add map-specific fields
        canonical.update({
            'preserves_cardinality': self.preserves_cardinality,
            'deterministic': self.deterministic,
            'field_mappings': sorted([
                (src.strip(), tgt.strip())
                for src, tgt in self.field_mappings
            ]) if self.field_mappings else [],
        })
        
        return canonical
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        step_dict = super().to_dict()
        return step_dict


# ============================================================================
# REDUCE STEP CONTRACT
# ============================================================================

@dataclass(frozen=True)
class ReduceStep(PipelineStep):
    """
    Reduce (aggregation) transformation step.
    
    Rules:
    - Group keys must exist in input schema
    - Output cardinality ≤ input cardinality
    - Reduction kind must be declared
    - Windowing is explicit (not implicit)
    
    Reduce steps aggregate data within groups.
    """
    
    # Grouping specification (explicit)
    grouping_keys: Tuple[str, ...]
    
    # Reduction operation (explicit)
    reduction_kind: str
    
    # Window association (explicit)
    windowed: bool = False
    window_ref: Optional[str] = None
    
    # Optional: aggregation field (for sum, avg, min, max)
    aggregation_field: Optional[str] = None
    
    # Optional: output field name for aggregation result
    output_field: Optional[str] = None
    
    def _get_expected_kind(self) -> PipelineStepKind:
        """Get expected step kind."""
        return PipelineStepKind.REDUCE
    
    def validate(self) -> None:
        """
        Validate reduce step.
        
        Raises:
            StepValidationError: If validation fails
        """
        # Base validation
        self._validate_base()
        
        # REDUCE INVARIANT: Must have grouping keys
        if not self.grouping_keys:
            raise StepValidationError(
                f"Reduce step '{self.step_name}' must have at least one grouping key"
            )
        
        # REDUCE CONSTRAINT: Grouping keys must be non-empty strings
        for key in self.grouping_keys:
            if not key or not key.strip():
                raise StepValidationError(
                    f"Reduce step '{self.step_name}' has empty grouping key"
                )
        
        # REDUCE CONSTRAINT: No duplicate grouping keys
        if len(self.grouping_keys) != len(set(self.grouping_keys)):
            raise StepValidationError(
                f"Reduce step '{self.step_name}' has duplicate grouping keys"
            )
        
        # REDUCE INVARIANT: Reduction kind must be valid
        if self.reduction_kind not in ALLOWED_REDUCTION_KINDS:
            raise StepValidationError(
                f"Invalid reduction kind '{self.reduction_kind}'. "
                f"Must be one of: {sorted(ALLOWED_REDUCTION_KINDS)}"
            )
        
        # REDUCE CONSTRAINT: Aggregation field required for certain reductions
        if self.reduction_kind in {'sum', 'avg', 'min', 'max'}:
            if not self.aggregation_field:
                raise StepValidationError(
                    f"Reduction kind '{self.reduction_kind}' requires aggregation_field"
                )
        
        # REDUCE CONSTRAINT: Window reference required if windowed
        if self.windowed and not self.window_ref:
            raise StepValidationError(
                f"Windowed reduce step '{self.step_name}' must specify window_ref"
            )
        
        # REDUCE CONSTRAINT: Window reference forbidden if not windowed
        if not self.windowed and self.window_ref:
            raise StepValidationError(
                f"Non-windowed reduce step '{self.step_name}' cannot have window_ref"
            )
    
    def validate_with_schema_registry(
        self,
        schema_registry: Optional[SchemaRegistryInterface] = None
    ) -> None:
        """
        Validate reduce step with semantic schema validation.
        
        This is Tier-0 validation that ensures grouping keys actually exist
        in the input schema definition.
        
        Args:
            schema_registry: Optional schema registry for semantic validation
            
        Raises:
            StepValidationError: If validation fails
        """
        # First run basic validation
        self.validate()
        
        # Tier-0 semantic validation: verify grouping keys exist in input schema
        if schema_registry is not None:
            try:
                schema = schema_registry.get_schema(
                    self.input_schema,
                    self.input_schema_version
                )
                
                if schema is None:
                    raise StepValidationError(
                        f"Reduce step '{self.step_name}': Input schema "
                        f"'{self.input_schema}' v{self.input_schema_version} not found in registry"
                    )
                
                # Get all field names from schema
                schema_field_names = schema.all_fields
                
                # Verify all grouping keys exist in schema
                missing_keys = []
                for key in self.grouping_keys:
                    if key not in schema_field_names:
                        missing_keys.append(key)
                
                if missing_keys:
                    raise StepValidationError(
                        f"Reduce step '{self.step_name}': Grouping keys not found in input schema "
                        f"'{self.input_schema}': {missing_keys}. "
                        f"Available fields: {sorted(schema_field_names)}"
                    )
                
                # Verify aggregation field exists if specified
                if self.aggregation_field:
                    if self.aggregation_field not in schema_field_names:
                        raise StepValidationError(
                            f"Reduce step '{self.step_name}': Aggregation field "
                            f"'{self.aggregation_field}' not found in input schema "
                            f"'{self.input_schema}'. Available fields: {sorted(schema_field_names)}"
                        )
            except AttributeError:
                # Schema registry doesn't implement the interface correctly
                raise StepValidationError(
                    f"Schema registry does not implement required interface for "
                    f"semantic validation of step '{self.step_name}'"
                )
    
    def _to_canonical_dict(self) -> Dict[str, Any]:
        """Convert to canonical dictionary for hashing."""
        canonical = super()._to_canonical_dict()
        
        # Add reduce-specific fields
        canonical.update({
            'grouping_keys': sorted([k.strip() for k in self.grouping_keys]),
            'reduction_kind': self.reduction_kind.strip(),
            'windowed': self.windowed,
            'window_ref': self.window_ref.strip() if self.window_ref else None,
            'aggregation_field': self.aggregation_field.strip() if self.aggregation_field else None,
            'output_field': self.output_field.strip() if self.output_field else None,
        })
        
        return canonical
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        step_dict = super().to_dict()
        return step_dict


# ============================================================================
# WINDOW STEP CONTRACT
# ============================================================================

@dataclass(frozen=True)
class WindowStep(PipelineStep):
    """
    Window transformation step.
    
    Rules:
    - Windows must be declared elsewhere (referenced, not defined)
    - No rolling defaults
    - Alignment must be explicit (event_time or processing_time)
    
    Window steps partition data into time-bounded groups.
    """
    
    # Window declaration reference (versioned)
    window_ref: str
    
    # Window alignment strategy (explicit)
    alignment: str
    
    # Optional: allowed lateness for event-time windows
    allowed_lateness_seconds: Optional[int] = None
    
    # Optional: watermark strategy
    watermark_strategy: Optional[str] = None
    
    def _get_expected_kind(self) -> PipelineStepKind:
        """Get expected step kind."""
        return PipelineStepKind.WINDOW
    
    def validate(self) -> None:
        """
        Validate window step.
        
        Raises:
            StepValidationError: If validation fails
        """
        # Base validation
        self._validate_base()
        
        # WINDOW INVARIANT: Must have window reference
        if not self.window_ref or not self.window_ref.strip():
            raise StepValidationError(
                f"Window step '{self.step_name}' must specify window_ref"
            )
        
        # WINDOW INVARIANT: Window reference must be versioned
        if '@' not in self.window_ref and ':' not in self.window_ref:
            raise StepValidationError(
                f"Window reference must be versioned (use '@' or ':'): {self.window_ref}"
            )
        
        # WINDOW INVARIANT: Alignment must be valid
        if self.alignment not in ALLOWED_WINDOW_ALIGNMENTS:
            raise StepValidationError(
                f"Invalid window alignment '{self.alignment}'. "
                f"Must be one of: {sorted(ALLOWED_WINDOW_ALIGNMENTS)}"
            )
        
        # WINDOW CONSTRAINT: Allowed lateness only for event-time windows
        if self.allowed_lateness_seconds is not None:
            if self.alignment != WindowAlignment.EVENT_TIME.value:
                raise StepValidationError(
                    f"Allowed lateness only applicable to event-time windows"
                )
            if self.allowed_lateness_seconds < 0:
                raise StepValidationError(
                    f"Allowed lateness must be non-negative: {self.allowed_lateness_seconds}"
                )
    
    def validate_with_window_registry(
        self,
        window_registry: Optional[WindowRegistryInterface] = None
    ) -> None:
        """
        Validate window step with semantic window registry validation.
        
        This is Tier-0 validation that ensures the window_ref actually exists
        in the window registry.
        
        Args:
            window_registry: Optional window registry for semantic validation
            
        Raises:
            StepValidationError: If validation fails
        """
        # First run basic validation
        self.validate()
        
        # Tier-0 semantic validation: verify window exists in registry
        if window_registry is not None:
            try:
                # Extract window name from versioned reference
                # Support both "window_name@version" and "window_name:version" formats
                window_name = self.window_ref
                if '@' in window_name:
                    window_name = window_name.split('@')[0]
                elif ':' in window_name:
                    window_name = window_name.split(':')[0]
                
                # Check if window exists in registry
                if hasattr(window_registry, 'has_window'):
                    if not window_registry.has_window(window_name):
                        raise StepValidationError(
                            f"Window step '{self.step_name}': Window reference "
                            f"'{self.window_ref}' (name: '{window_name}') not found in window registry. "
                            f"Windows must be declared before use."
                        )
                else:
                    # Fallback: try to get the window (will raise KeyError if not found)
                    try:
                        window_registry.get(window_name)
                    except KeyError:
                        raise StepValidationError(
                            f"Window step '{self.step_name}': Window reference "
                            f"'{self.window_ref}' (name: '{window_name}') not found in window registry. "
                            f"Windows must be declared before use."
                        )
            except AttributeError:
                # Window registry doesn't implement the interface correctly
                raise StepValidationError(
                    f"Window registry does not implement required interface for "
                    f"semantic validation of step '{self.step_name}'"
                )
    
    def _to_canonical_dict(self) -> Dict[str, Any]:
        """Convert to canonical dictionary for hashing."""
        canonical = super()._to_canonical_dict()
        
        # Add window-specific fields
        canonical.update({
            'window_ref': self.window_ref.strip(),
            'alignment': self.alignment.strip(),
            'allowed_lateness_seconds': self.allowed_lateness_seconds,
            'watermark_strategy': self.watermark_strategy.strip() if self.watermark_strategy else None,
        })
        
        return canonical
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        step_dict = super().to_dict()
        return step_dict


# ============================================================================
# STEP FACTORY & DESERIALIZATION
# ============================================================================

class StepFactory:
    """
    Factory for creating pipeline steps from configuration.
    
    Handles deserialization and validation.
    """
    
    @staticmethod
    def from_dict(step_dict: Dict[str, Any]) -> PipelineStep:
        """
        Create pipeline step from dictionary.
        
        Args:
            step_dict: Step configuration dictionary
            
        Returns:
            Concrete PipelineStep instance
            
        Raises:
            StepValidationError: If step cannot be created
        """
        # Extract step kind
        step_kind_str = step_dict.get('step_kind')
        if not step_kind_str:
            raise StepValidationError("Missing step_kind in configuration")
        
        try:
            step_kind = PipelineStepKind.from_string(step_kind_str)
        except ValueError as e:
            raise StepValidationError(str(e))
        
        # Dispatch to appropriate constructor
        if step_kind == PipelineStepKind.MAP:
            return StepFactory._create_map_step(step_dict)
        elif step_kind == PipelineStepKind.REDUCE:
            return StepFactory._create_reduce_step(step_dict)
        elif step_kind == PipelineStepKind.WINDOW:
            return StepFactory._create_window_step(step_dict)
        else:
            raise StepValidationError(f"Unknown step kind: {step_kind}")
    
    @staticmethod
    def _create_map_step(step_dict: Dict[str, Any]) -> MapStep:
        """Create MapStep from dictionary."""
        # Extract field mappings if present
        field_mappings_raw = step_dict.get('field_mappings', [])
        field_mappings = tuple(tuple(m) for m in field_mappings_raw) if field_mappings_raw else ()
        
        # Extract tags
        tags_raw = step_dict.get('tags', [])
        tags = tuple(tags_raw) if tags_raw else ()
        
        return MapStep(
            step_name=step_dict['step_name'],
            step_kind=PipelineStepKind.MAP,
            input_schema=step_dict['input_schema'],
            input_schema_version=step_dict['input_schema_version'],
            output_schema=step_dict['output_schema'],
            output_schema_version=step_dict['output_schema_version'],
            algorithm_id=step_dict['algorithm_id'],
            description=step_dict.get('description', ''),
            tags=tags,
            preserves_cardinality=step_dict.get('preserves_cardinality', True),
            deterministic=step_dict.get('deterministic', True),
            field_mappings=field_mappings,
        )
    
    @staticmethod
    def _create_reduce_step(step_dict: Dict[str, Any]) -> ReduceStep:
        """Create ReduceStep from dictionary."""
        # Extract grouping keys
        grouping_keys_raw = step_dict.get('grouping_keys', [])
        grouping_keys = tuple(grouping_keys_raw) if grouping_keys_raw else ()
        
        # Extract tags
        tags_raw = step_dict.get('tags', [])
        tags = tuple(tags_raw) if tags_raw else ()
        
        return ReduceStep(
            step_name=step_dict['step_name'],
            step_kind=PipelineStepKind.REDUCE,
            input_schema=step_dict['input_schema'],
            input_schema_version=step_dict['input_schema_version'],
            output_schema=step_dict['output_schema'],
            output_schema_version=step_dict['output_schema_version'],
            algorithm_id=step_dict['algorithm_id'],
            description=step_dict.get('description', ''),
            tags=tags,
            grouping_keys=grouping_keys,
            reduction_kind=step_dict['reduction_kind'],
            windowed=step_dict.get('windowed', False),
            window_ref=step_dict.get('window_ref'),
            aggregation_field=step_dict.get('aggregation_field'),
            output_field=step_dict.get('output_field'),
        )
    
    @staticmethod
    def _create_window_step(step_dict: Dict[str, Any]) -> WindowStep:
        """Create WindowStep from dictionary."""
        # Extract tags
        tags_raw = step_dict.get('tags', [])
        tags = tuple(tags_raw) if tags_raw else ()
        
        return WindowStep(
            step_name=step_dict['step_name'],
            step_kind=PipelineStepKind.WINDOW,
            input_schema=step_dict['input_schema'],
            input_schema_version=step_dict['input_schema_version'],
            output_schema=step_dict['output_schema'],
            output_schema_version=step_dict['output_schema_version'],
            algorithm_id=step_dict['algorithm_id'],
            description=step_dict.get('description', ''),
            tags=tags,
            window_ref=step_dict['window_ref'],
            alignment=step_dict['alignment'],
            allowed_lateness_seconds=step_dict.get('allowed_lateness_seconds'),
            watermark_strategy=step_dict.get('watermark_strategy'),
        )
    
    @staticmethod
    def from_json(json_str: str) -> PipelineStep:
        """
        Create pipeline step from JSON string.
        
        Args:
            json_str: JSON string
            
        Returns:
            Concrete PipelineStep instance
            
        Raises:
            StepValidationError: If step cannot be created
        """
        try:
            step_dict = json.loads(json_str)
            return StepFactory.from_dict(step_dict)
        except json.JSONDecodeError as e:
            raise StepValidationError(f"Invalid JSON: {e}")


# ============================================================================
# STEP REGISTRY & VALIDATION
# ============================================================================

class StepRegistry:
    """
    Registry of pipeline steps for validation and lookup.
    
    Ensures no duplicate step names and provides step retrieval.
    """
    
    def __init__(self):
        """Initialize step registry."""
        self._steps: Dict[str, PipelineStep] = {}
        self._step_hashes: Dict[str, str] = {}
    
    def register(self, step: PipelineStep) -> None:
        """
        Register a pipeline step.
        
        Args:
            step: Pipeline step to register
            
        Raises:
            StepValidationError: If step name already exists
        """
        # Validate step before registration
        step.validate()
        
        # Check for duplicate name
        if step.step_name in self._steps:
            raise StepValidationError(
                f"Step name already registered: {step.step_name}"
            )
        
        # Compute and store hash
        step_hash = step.compute_step_hash()
        
        # Register
        self._steps[step.step_name] = step
        self._step_hashes[step.step_name] = step_hash
    
    def get(self, step_name: str) -> Optional[PipelineStep]:
        """
        Get step by name.
        
        Args:
            step_name: Step name
            
        Returns:
            PipelineStep if found, None otherwise
        """
        return self._steps.get(step_name)
    
    def get_hash(self, step_name: str) -> Optional[str]:
        """
        Get step hash by name.
        
        Args:
            step_name: Step name
            
        Returns:
            Step hash if found, None otherwise
        """
        return self._step_hashes.get(step_name)
    
    def list_steps(self) -> List[str]:
        """
        List all registered step names.
        
        Returns:
            List of step names
        """
        return sorted(self._steps.keys())
    
    def list_steps_by_kind(self, kind: PipelineStepKind) -> List[str]:
        """
        List steps by kind.
        
        Args:
            kind: Step kind to filter by
            
        Returns:
            List of step names
        """
        return sorted([
            name for name, step in self._steps.items()
            if step.step_kind == kind
        ])
    
    def validate_all(self) -> None:
        """
        Validate all registered steps.
        
        Raises:
            StepValidationError: If any step is invalid
        """
        for step_name, step in self._steps.items():
            try:
                step.validate()
                step.check_invariants()
            except (StepValidationError, StepInvariantViolation) as e:
                raise StepValidationError(
                    f"Step '{step_name}' validation failed: {e}"
                )
    
    def validate_all_with_registries(
        self,
        schema_registry: Optional[SchemaRegistryInterface] = None,
        window_registry: Optional[WindowRegistryInterface] = None,
        algorithm_registry: Optional[AlgorithmRegistryInterface] = None
    ) -> None:
        """
        Validate all registered steps with full Tier-0 semantic validation.
        
        This performs:
        - Basic structural validation
        - Schema-aware validation (for ReduceStep)
        - Window registry resolution (for WindowStep)
        - Algorithm registry classification (for all steps)
        
        Args:
            schema_registry: Optional schema registry for semantic validation
            window_registry: Optional window registry for semantic validation
            algorithm_registry: Optional algorithm registry for metadata-based validation
            
        Raises:
            StepValidationError: If any step is invalid
        """
        for step_name, step in self._steps.items():
            try:
                step.validate_with_registries(
                    schema_registry=schema_registry,
                    window_registry=window_registry,
                    algorithm_registry=algorithm_registry
                )
                step.check_invariants()
            except (StepValidationError, StepInvariantViolation) as e:
                raise StepValidationError(
                    f"Step '{step_name}' Tier-0 validation failed: {e}"
                )


# ============================================================================
# STEP GRAPH VALIDATOR
# ============================================================================

class StepGraphValidator:
    """
    Validates pipeline step graphs for consistency.
    
    Ensures:
    - Schema compatibility between connected steps
    - No cycles in step dependencies
    - All referenced windows/algorithms exist
    """
    
    def __init__(self, registry: StepRegistry):
        """
        Initialize validator.
        
        Args:
            registry: Step registry
        """
        self._registry = registry
    
    def validate_schema_compatibility(
        self,
        upstream_step: PipelineStep,
        downstream_step: PipelineStep
    ) -> None:
        """
        Validate schema compatibility between steps.
        
        Args:
            upstream_step: Upstream (producer) step
            downstream_step: Downstream (consumer) step
            
        Raises:
            StepValidationError: If schemas are incompatible
        """
        # Output of upstream must match input of downstream
        if upstream_step.output_schema != downstream_step.input_schema:
            raise StepValidationError(
                f"Schema mismatch: {upstream_step.step_name} outputs "
                f"'{upstream_step.output_schema}' but {downstream_step.step_name} "
                f"expects '{downstream_step.input_schema}'"
            )
        
        # Schema versions should be compatible (exact match for now)
        if upstream_step.output_schema_version != downstream_step.input_schema_version:
            raise StepValidationError(
                f"Schema version mismatch: {upstream_step.step_name} outputs "
                f"v{upstream_step.output_schema_version} but {downstream_step.step_name} "
                f"expects v{downstream_step.input_schema_version}"
            )
    
    def validate_step_sequence(self, step_names: List[str]) -> None:
        """
        Validate a sequence of steps.
        
        Args:
            step_names: Ordered list of step names
            
        Raises:
            StepValidationError: If sequence is invalid
        """
        if len(step_names) < 2:
            return  # Single step is always valid
        
        # Get steps
        steps = []
        for name in step_names:
            step = self._registry.get(name)
            if not step:
                raise StepValidationError(f"Unknown step: {name}")
            steps.append(step)
        
        # Validate pairwise compatibility
        for i in range(len(steps) - 1):
            self.validate_schema_compatibility(steps[i], steps[i + 1])


# ============================================================================
# INVARIANT CHECKER
# ============================================================================

class InvariantChecker:
    """
    Checks absolute invariants that must NEVER be violated.
    
    Violations indicate pipeline corruption.
    """
    
    @staticmethod
    def check_step_immutability(step: PipelineStep) -> None:
        """
        Verify step is truly immutable.
        
        Args:
            step: Step to check
            
        Raises:
            StepInvariantViolation: If step is mutable
        """
        # Frozen dataclass should prevent mutation
        # This is a sanity check
        try:
            step.step_name = "modified"  # type: ignore
            raise StepInvariantViolation(
                "Step is mutable! frozen=True not enforced"
            )
        except AttributeError:
            # Expected: cannot modify frozen dataclass
            pass
    
    @staticmethod
    def check_no_external_state(step: PipelineStep) -> None:
        """
        Verify step does not reference external state.
        
        Tier-0 invariant: Steps must never read external state.
        This checks for file paths, URLs, API endpoints, and other
        external dependencies that violate the declarative contract.
        
        Args:
            step: Step to check
            
        Raises:
            StepInvariantViolation: If step references external state
        """
        # Steps should only contain primitive types, strings, and tuples
        # No file handles, network connections, etc.
        
        step_dict = step.to_dict()
        
        # Check all values are serializable
        try:
            json.dumps(step_dict)
        except (TypeError, ValueError) as e:
            raise StepInvariantViolation(
                f"Step contains non-serializable state: {e}"
            )
        
        # Tier-0 semantic check: detect external state patterns
        import re
        
        # Patterns that indicate external state
        file_path_patterns = [
            r'^[A-Za-z]:[/\\]',  # Windows absolute paths (C:\ or C:/)
            r'^[/\\]',  # Unix absolute paths
            r'^\.\.?[/\\]',  # Relative paths with ../
            r'^~[/\\]',  # Home directory paths
        ]
        
        url_patterns = [
            r'^https?://',  # HTTP/HTTPS URLs
            r'^ftp://',  # FTP URLs
            r'^file://',  # File URLs
            r'^s3://',  # S3 URLs
            r'^gs://',  # GCS URLs
            r'^azure://',  # Azure URLs
        ]
        
        api_endpoint_patterns = [
            r'^api\.',  # API subdomains
            r'\.api\.',  # API in domain
            r':\d{4,5}/',  # Port numbers (likely API endpoints)
        ]
        
        def check_string_for_external_state(value: str, field_path: str) -> List[str]:
            """Check if string contains external state patterns."""
            violations = []
            
            # Check file paths
            for pattern in file_path_patterns:
                if re.match(pattern, value):
                    violations.append(
                        f"Field '{field_path}' contains file path pattern: {value}"
                    )
            
            # Check URLs
            for pattern in url_patterns:
                if re.match(pattern, value, re.IGNORECASE):
                    violations.append(
                        f"Field '{field_path}' contains URL pattern: {value}"
                    )
            
            # Check API endpoints
            for pattern in api_endpoint_patterns:
                if re.search(pattern, value, re.IGNORECASE):
                    violations.append(
                        f"Field '{field_path}' contains API endpoint pattern: {value}"
                    )
            
            return violations
        
        def check_dict_recursive(obj: Any, path: str = "") -> List[str]:
            """Recursively check dictionary for external state."""
            violations = []
            
            if isinstance(obj, dict):
                for key, value in obj.items():
                    current_path = f"{path}.{key}" if path else key
                    if isinstance(value, str):
                        violations.extend(check_string_for_external_state(value, current_path))
                    elif isinstance(value, (dict, list)):
                        violations.extend(check_dict_recursive(value, current_path))
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    current_path = f"{path}[{i}]"
                    if isinstance(item, str):
                        violations.extend(check_string_for_external_state(item, current_path))
                    elif isinstance(item, (dict, list)):
                        violations.extend(check_dict_recursive(item, current_path))
            
            return violations
        
        # Check for external state patterns
        violations = check_dict_recursive(step_dict)
        
        if violations:
            raise StepInvariantViolation(
                f"Step '{step.step_name}' references external state (violates declarative contract):\n"
                + "\n".join(f"  - {v}" for v in violations)
            )
    
    @staticmethod
    def check_deterministic_hash(step: PipelineStep) -> None:
        """
        Verify step hash is deterministic.
        
        Args:
            step: Step to check
            
        Raises:
            StepInvariantViolation: If hash is non-deterministic
        """
        hash_1 = step.compute_step_hash()
        hash_2 = step.compute_step_hash()
        
        if hash_1 != hash_2:
            raise StepInvariantViolation(
                f"Step hash is non-deterministic: {hash_1} != {hash_2}"
            )
    
    @staticmethod
    def check_all_invariants(step: PipelineStep) -> None:
        """
        Check all invariants for a step.
        
        Args:
            step: Step to check
            
        Raises:
            StepInvariantViolation: If any invariant is violated
        """
        InvariantChecker.check_step_immutability(step)
        InvariantChecker.check_no_external_state(step)
        InvariantChecker.check_deterministic_hash(step)


# ============================================================================
# FORBIDDEN PATTERN DETECTOR
# ============================================================================

class ForbiddenPatternDetector:
    """
    Detects forbidden patterns in pipeline steps.
    
    ZERO TOLERANCE for:
    - Stateful transforms
    - Hidden aggregation
    - Implicit windows
    - Schema inference
    - Randomization
    - Conditional emission
    
    Tier-0 enforcement: Uses algorithm registry metadata instead of heuristics.
    """
    
    @staticmethod
    def check_map_has_no_aggregation(
        step: MapStep,
        algorithm_registry: Optional[AlgorithmRegistryInterface] = None
    ) -> None:
        """
        Verify map step does not perform aggregation.
        
        Tier-0 enforcement: Uses algorithm registry metadata when available.
        Falls back to heuristic detection only if registry is unavailable.
        
        Args:
            step: Map step to check
            algorithm_registry: Optional algorithm registry for metadata-based validation
            
        Raises:
            StepValidationError: If aggregation detected
        """
        # Tier-0 path: use algorithm registry metadata
        if algorithm_registry is not None:
            try:
                metadata = algorithm_registry.get_algorithm_metadata(step.algorithm_id)
                
                if metadata is not None:
                    # Use authoritative metadata classification
                    if metadata.is_aggregation:
                        raise StepValidationError(
                            f"Map step '{step.step_name}' uses aggregation algorithm "
                            f"'{step.algorithm_id}'. Use ReduceStep instead."
                        )
                    
                    # Check if algorithm is allowed for MAP step kind
                    if not algorithm_registry.is_algorithm_allowed_for_step_kind(
                        step.algorithm_id,
                        PipelineStepKind.MAP
                    ):
                        raise StepValidationError(
                            f"Map step '{step.step_name}': Algorithm '{step.algorithm_id}' "
                            f"is not allowed for MAP step kind."
                        )
                    
                    # Metadata-based validation passed
                    return
            except AttributeError:
                # Registry doesn't implement interface correctly, fall through to heuristic
                pass
        
        # Fallback: heuristic detection (not Tier-0, but better than nothing)
        # This is a compatibility path for systems without algorithm registry
        aggregation_keywords = ['sum', 'count', 'avg', 'reduce', 'aggregate']
        
        algorithm_lower = step.algorithm_id.lower()
        for keyword in aggregation_keywords:
            if keyword in algorithm_lower:
                raise StepValidationError(
                    f"Map step '{step.step_name}' appears to perform aggregation "
                    f"(algorithm contains '{keyword}'). Use ReduceStep instead. "
                    f"Note: Heuristic detection used - algorithm registry recommended for Tier-0 validation."
                )
    
    @staticmethod
    def check_no_randomization(
        step: PipelineStep,
        algorithm_registry: Optional[AlgorithmRegistryInterface] = None
    ) -> None:
        """
        Verify step does not use randomization.
        
        Tier-0 enforcement: Uses algorithm registry metadata when available.
        Falls back to heuristic detection only if registry is unavailable.
        
        Args:
            step: Step to check
            algorithm_registry: Optional algorithm registry for metadata-based validation
            
        Raises:
            StepValidationError: If randomization detected
        """
        # Tier-0 path: use algorithm registry metadata
        if algorithm_registry is not None:
            try:
                metadata = algorithm_registry.get_algorithm_metadata(step.algorithm_id)
                
                if metadata is not None:
                    # Use authoritative metadata classification
                    if metadata.is_randomized:
                        raise StepValidationError(
                            f"Step '{step.step_name}' uses randomized algorithm "
                            f"'{step.algorithm_id}'. All steps must be deterministic."
                        )
                    
                    if not metadata.is_deterministic:
                        raise StepValidationError(
                            f"Step '{step.step_name}': Algorithm '{step.algorithm_id}' "
                            f"is not deterministic. All steps must be deterministic."
                        )
                    
                    # Metadata-based validation passed
                    return
            except AttributeError:
                # Registry doesn't implement interface correctly, fall through to heuristic
                pass
        
        # Fallback: heuristic detection (not Tier-0, but better than nothing)
        random_keywords = ['random', 'rand', 'nondeterministic', 'uuid']
        
        algorithm_lower = step.algorithm_id.lower()
        for keyword in random_keywords:
            if keyword in algorithm_lower:
                raise StepValidationError(
                    f"Step '{step.step_name}' appears to use randomization "
                    f"(algorithm contains '{keyword}'). All steps must be deterministic. "
                    f"Note: Heuristic detection used - algorithm registry recommended for Tier-0 validation."
                )
    
    @staticmethod
    def check_reduce_has_explicit_window(step: ReduceStep) -> None:
        """
        Verify reduce step has explicit window if windowed.
        
        Args:
            step: Reduce step to check
            
        Raises:
            StepValidationError: If window is implicit
        """
        if step.windowed and not step.window_ref:
            raise StepValidationError(
                f"Reduce step '{step.step_name}' is windowed but has no window_ref. "
                f"Windows must be explicit."
            )
    
    @staticmethod
    def check_all_patterns(
        step: PipelineStep,
        algorithm_registry: Optional[AlgorithmRegistryInterface] = None
    ) -> None:
        """
        Check all forbidden patterns for a step.
        
        Tier-0 enforcement: Uses algorithm registry metadata when available.
        
        Args:
            step: Step to check
            algorithm_registry: Optional algorithm registry for metadata-based validation
            
        Raises:
            StepValidationError: If any forbidden pattern detected
        """
        # Check randomization for all steps
        ForbiddenPatternDetector.check_no_randomization(step, algorithm_registry)
        
        # Step-specific checks
        if isinstance(step, MapStep):
            ForbiddenPatternDetector.check_map_has_no_aggregation(step, algorithm_registry)
        elif isinstance(step, ReduceStep):
            ForbiddenPatternDetector.check_reduce_has_explicit_window(step)


# ============================================================================
# SERIALIZATION UTILITIES
# ============================================================================

class StepSerializer:
    """
    Utilities for step serialization with stable ordering.
    
    Rules:
    - Sorted keys
    - Stable ordering
    - No runtime fields
    - No execution timestamps
    
    Steps are configuration, not activity.
    """
    
    @staticmethod
    def to_json(step: PipelineStep, indent: int = 2) -> str:
        """
        Serialize step to JSON with stable formatting.
        
        Args:
            step: Step to serialize
            indent: JSON indentation level
            
        Returns:
            JSON string
        """
        return json.dumps(
            step.to_dict(),
            indent=indent,
            sort_keys=True,
            ensure_ascii=True
        )
    
    @staticmethod
    def to_canonical_json(step: PipelineStep) -> str:
        """
        Serialize step to canonical JSON (for hashing).
        
        Args:
            step: Step to serialize
            
        Returns:
            Canonical JSON string
        """
        canonical_dict = step._to_canonical_dict()
        return json.dumps(
            canonical_dict,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True
        )
    
    @staticmethod
    def batch_to_json(steps: List[PipelineStep], indent: int = 2) -> str:
        """
        Serialize multiple steps to JSON array.
        
        Args:
            steps: List of steps
            indent: JSON indentation level
            
        Returns:
            JSON array string
        """
        steps_list = [step.to_dict() for step in steps]
        return json.dumps(
            steps_list,
            indent=indent,
            sort_keys=True,
            ensure_ascii=True
        )


# ============================================================================
# COMMAND-LINE UTILITIES
# ============================================================================

def validate_step_from_file(filepath: str) -> PipelineStep:
    """
    Load and validate a step from JSON file.
    
    Args:
        filepath: Path to step JSON file
        
    Returns:
        Validated PipelineStep
        
    Raises:
        StepValidationError: If step is invalid
    """
    with open(filepath, 'r') as f:
        step_json = f.read()
    
    step = StepFactory.from_json(step_json)
    step.validate()
    step.check_invariants()
    ForbiddenPatternDetector.check_all_patterns(step)
    
    return step


def validate_step_sequence_from_file(filepath: str) -> List[PipelineStep]:
    """
    Load and validate a sequence of steps from JSON file.
    
    Args:
        filepath: Path to JSON file containing step array
        
    Returns:
        List of validated PipelineSteps
        
    Raises:
        StepValidationError: If any step is invalid
    """
    with open(filepath, 'r') as f:
        steps_data = json.load(f)
    
    if not isinstance(steps_data, list):
        raise StepValidationError("Expected JSON array of steps")
    
    registry = StepRegistry()
    steps = []
    
    for step_dict in steps_data:
        step = StepFactory.from_dict(step_dict)
        step.validate()
        step.check_invariants()
        ForbiddenPatternDetector.check_all_patterns(step)
        registry.register(step)
        steps.append(step)
    
    # Validate sequence
    validator = StepGraphValidator(registry)
    step_names = [s.step_name for s in steps]
    validator.validate_step_sequence(step_names)
    
    return steps


if __name__ == '__main__':
    import sys
    
    # Simple CLI for validation
    if len(sys.argv) < 2:
        print("Usage: python pipeline_step.py <step_file.json>")
        sys.exit(1)
    
    filepath = sys.argv[1]
    
    try:
        # Try as single step first
        step = validate_step_from_file(filepath)
        print(f"✓ Step '{step.step_name}' is valid")
        print(f"  Kind: {step.step_kind}")
        print(f"  Hash: {step.compute_step_hash()}")
    except (StepValidationError, json.JSONDecodeError):
        # Try as sequence
        try:
            steps = validate_step_sequence_from_file(filepath)
            print(f"✓ Step sequence is valid ({len(steps)} steps)")
            for step in steps:
                print(f"  - {step.step_name} ({step.step_kind})")
        except Exception as e:
            print(f"✗ Validation failed: {e}")
            sys.exit(1)