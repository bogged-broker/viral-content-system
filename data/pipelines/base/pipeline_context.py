"""
/data/pipelines/base/pipeline_context.py

Immutable Pipeline Execution Context

This module defines the non-negotiable execution environment for every pipeline run.
It answers: "Under what exact conditions was this pipeline executed?"

Design Principle:
    No context → no trust.
    If you can't name the run, you can't trust the numbers.

Authority Level: SPINE OBJECT
All fields are mandatory, immutable, and deterministically derived.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional, Tuple, Union


# ============================================================================
# CORE ENUMS
# ============================================================================


class TriggerType(Enum):
    """Enumeration of pipeline trigger sources."""
    
    SCHEDULER = "scheduler"      # Scheduled execution
    REPLAY = "replay"            # Replay of historical run
    RECOVERY = "recovery"        # Recovery from failure
    MANUAL = "manual"            # Manual trigger
    BACKFILL = "backfill"        # Backfill operation
    TEST = "test"                # Test execution


class ExecutionMode(Enum):
    """Enumeration of execution modes."""
    
    PRODUCTION = "production"
    STAGING = "staging"
    DEVELOPMENT = "development"
    REPLAY = "replay"
    VALIDATION = "validation"


class TimeMode(Enum):
    """Enumeration of time handling modes."""
    
    EVENT_TIME = "event_time"          # Use event timestamps
    DECLARED_WINDOW = "declared_window"  # Use declared time windows
    FIXED_WINDOW = "fixed_window"      # Fixed time boundaries


# ============================================================================
# EXCEPTIONS
# ============================================================================


class PipelineContextError(Exception):
    """Base exception for pipeline context errors."""
    pass


class InvalidContextError(PipelineContextError):
    """Raised when context validation fails."""
    pass


class ImmutabilityViolation(PipelineContextError):
    """Raised when attempting to modify immutable context."""
    pass


class DeterminismViolation(PipelineContextError):
    """Raised when determinism guarantees are violated."""
    pass


# ============================================================================
# VERSION TYPES
# ============================================================================


@dataclass(frozen=True)
class SchemaVersion:
    """Immutable schema version identifier.
    
    Represents a specific version of a data schema.
    """
    
    name: str
    version: int
    hash: str = field(default="")
    
    def __post_init__(self):
        """Validate schema version."""
        if not self.name:
            raise InvalidContextError("Schema name cannot be empty")
        if self.version < 0:
            raise InvalidContextError(f"Schema version must be non-negative: {self.version}")
        
        # Generate hash if not provided
        if not self.hash:
            computed_hash = self._compute_hash()
            # Use object.__setattr__ to bypass frozen dataclass
            object.__setattr__(self, 'hash', computed_hash)
    
    def _compute_hash(self) -> str:
        """Compute deterministic hash of schema version."""
        data = f"{self.name}::{self.version}"
        return hashlib.sha256(data.encode('utf-8')).hexdigest()[:16]
    
    def to_tuple(self) -> Tuple[str, int]:
        """Convert to tuple representation."""
        return (self.name, self.version)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "name": self.name,
            "version": self.version,
            "hash": self.hash,
        }
    
    @classmethod
    def from_tuple(cls, t: Tuple[str, int]) -> SchemaVersion:
        """Create from tuple."""
        return cls(name=t[0], version=t[1])
    
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> SchemaVersion:
        """Create from dictionary."""
        return cls(
            name=d["name"],
            version=d["version"],
            hash=d.get("hash", ""),
        )
    
    def __str__(self) -> str:
        return f"{self.name}@v{self.version}"
    
    def __repr__(self) -> str:
        return f"SchemaVersion(name={self.name!r}, version={self.version}, hash={self.hash!r})"


@dataclass(frozen=True)
class ComputationVersion:
    """Immutable computation/algorithm version identifier.
    
    Represents a specific version of a computation or transformation.
    """
    
    name: str
    version: str
    checksum: str = field(default="")
    
    def __post_init__(self):
        """Validate computation version."""
        if not self.name:
            raise InvalidContextError("Computation name cannot be empty")
        if not self.version:
            raise InvalidContextError("Computation version cannot be empty")
        
        # Generate checksum if not provided
        if not self.checksum:
            computed_checksum = self._compute_checksum()
            object.__setattr__(self, 'checksum', computed_checksum)
    
    def _compute_checksum(self) -> str:
        """Compute deterministic checksum."""
        data = f"{self.name}::{self.version}"
        return hashlib.sha256(data.encode('utf-8')).hexdigest()[:16]
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "name": self.name,
            "version": self.version,
            "checksum": self.checksum,
        }
    
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> ComputationVersion:
        """Create from dictionary."""
        return cls(
            name=d["name"],
            version=d["version"],
            checksum=d.get("checksum", ""),
        )
    
    def __str__(self) -> str:
        return f"{self.name}@{self.version}"
    
    def __repr__(self) -> str:
        return f"ComputationVersion(name={self.name!r}, version={self.version!r}, checksum={self.checksum!r})"


# ============================================================================
# RUN ID GENERATOR
# ============================================================================


class RunIdGenerator:
    """Deterministic run ID generator.
    
    Generates run IDs based on pipeline configuration and inputs.
    Same inputs → same run_id (critical for replay).
    """
    
    ALGORITHM_VERSION = "v1"
    
    @classmethod
    def generate(
        cls,
        pipeline_name: str,
        pipeline_version: str,
        input_schema_versions: Tuple[SchemaVersion, ...],
        computation_versions: Tuple[ComputationVersion, ...],
        execution_timestamp: int,
    ) -> str:
        """Generate deterministic run ID.
        
        Args:
            pipeline_name: Name of the pipeline
            pipeline_version: Version of the pipeline
            input_schema_versions: Sorted tuple of input schema versions
            computation_versions: Sorted tuple of computation versions
            execution_timestamp: Monotonic execution timestamp
            
        Returns:
            Deterministic run ID string
            
        Note:
            run_id is derived ONLY from pipeline identity, schema versions,
            computation versions, and execution timestamp. Trigger type is
            NOT included to ensure same inputs → same run_id regardless of
            how the run was triggered (scheduler, replay, recovery, etc.).
        """
        # Build canonical representation
        components = [
            f"name={pipeline_name}",
            f"version={pipeline_version}",
            f"timestamp={execution_timestamp}",
        ]
        
        # Add sorted input schemas
        schema_strs = [f"{s.name}@v{s.version}" for s in sorted(input_schema_versions, key=lambda s: (s.name, s.version))]
        components.append(f"schemas=[{','.join(schema_strs)}]")
        
        # Add sorted computations
        comp_strs = [f"{c.name}@{c.version}" for c in sorted(computation_versions, key=lambda c: (c.name, c.version))]
        components.append(f"computations=[{','.join(comp_strs)}]")
        
        # Join and hash
        canonical = "::".join(components)
        hash_digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        
        # Format: run_{timestamp}_{hash_prefix}
        run_id = f"run_{execution_timestamp}_{hash_digest[:16]}"
        
        return run_id
    
    @classmethod
    def verify(
        cls,
        run_id: str,
        pipeline_name: str,
        pipeline_version: str,
        input_schema_versions: Tuple[SchemaVersion, ...],
        computation_versions: Tuple[ComputationVersion, ...],
        execution_timestamp: int,
    ) -> bool:
        """Verify a run ID matches the given parameters.
        
        Args:
            run_id: Run ID to verify
            Other args: Same as generate()
            
        Returns:
            True if run ID is valid for given parameters
        """
        expected = cls.generate(
            pipeline_name,
            pipeline_version,
            input_schema_versions,
            computation_versions,
            execution_timestamp,
        )
        return run_id == expected


# ============================================================================
# PIPELINE CONTEXT
# ============================================================================


@dataclass(frozen=True)
class PipelineContext:
    """Immutable pipeline execution context.
    
    Defines the complete, non-negotiable execution environment for a pipeline run.
    
    Rules (HARD):
    - Context is immutable
    - No defaults allowed
    - No environment reads
    - No wall-clock calls
    - All versions are mandatory
    - Same inputs → same run_id
    
    Forbidden:
    - Environment variables
    - System time
    - Random seeds
    - Mutable collections
    """
    
    # Core identification
    pipeline_name: str
    pipeline_version: str
    run_id: str
    
    # Trigger information
    triggered_by: TriggerType
    execution_mode: ExecutionMode
    
    # Schema versions (sorted, immutable)
    input_schema_versions: Tuple[SchemaVersion, ...]
    output_schema_version: SchemaVersion
    
    # Computation versions (sorted, immutable)
    computation_versions: Tuple[ComputationVersion, ...]
    
    # Timing (monotonic, deterministic)
    execution_timestamp: int
    
    # Optional metadata (immutable)
    metadata: FrozenSet[Tuple[str, str]]
    
    # Configuration (immutable)
    hash_algorithm: str
    reproducibility_mode: str
    time_mode: TimeMode
    
    # Provenance tracking
    provenance_artifacts: FrozenSet[str]
    
    # Parent run (for replay/recovery)
    parent_run_id: Optional[str]
    
    def __post_init__(self):
        """Validate context after initialization."""
        self._validate_pipeline_identity()
        self._validate_schemas()
        self._validate_computations()
        self._validate_timing()
        self._validate_run_id()
        self._validate_configuration()
    
    def _validate_pipeline_identity(self) -> None:
        """Validate pipeline identification fields."""
        if not self.pipeline_name:
            raise InvalidContextError("pipeline_name cannot be empty")
        
        if not self.pipeline_version:
            raise InvalidContextError("pipeline_version cannot be empty")
        
        # Version should follow semantic versioning pattern
        if not self._is_valid_semver(self.pipeline_version):
            raise InvalidContextError(
                f"pipeline_version must be semantic version (e.g., '1.0.0'): {self.pipeline_version}"
            )
        
        if not isinstance(self.triggered_by, TriggerType):
            raise InvalidContextError(f"triggered_by must be TriggerType: {type(self.triggered_by)}")
        
        if not isinstance(self.execution_mode, ExecutionMode):
            raise InvalidContextError(f"execution_mode must be ExecutionMode: {type(self.execution_mode)}")
    
    def _validate_schemas(self) -> None:
        """Validate schema versions."""
        if not self.input_schema_versions:
            raise InvalidContextError("input_schema_versions cannot be empty")
        
        if not isinstance(self.input_schema_versions, tuple):
            raise InvalidContextError("input_schema_versions must be tuple (immutable)")
        
        # Verify all elements are SchemaVersion
        for i, schema in enumerate(self.input_schema_versions):
            if not isinstance(schema, SchemaVersion):
                raise InvalidContextError(
                    f"input_schema_versions[{i}] must be SchemaVersion: {type(schema)}"
                )
        
        # Verify schemas are sorted (for determinism)
        sorted_schemas = tuple(sorted(self.input_schema_versions, key=lambda s: (s.name, s.version)))
        if self.input_schema_versions != sorted_schemas:
            raise InvalidContextError(
                "input_schema_versions must be sorted by (name, version) for determinism"
            )
        
        # Validate output schema
        if not isinstance(self.output_schema_version, SchemaVersion):
            raise InvalidContextError(
                f"output_schema_version must be SchemaVersion: {type(self.output_schema_version)}"
            )
    
    def _validate_computations(self) -> None:
        """Validate computation versions."""
        if not self.computation_versions:
            raise InvalidContextError("computation_versions cannot be empty")
        
        if not isinstance(self.computation_versions, tuple):
            raise InvalidContextError("computation_versions must be tuple (immutable)")
        
        # Verify all elements are ComputationVersion
        for i, comp in enumerate(self.computation_versions):
            if not isinstance(comp, ComputationVersion):
                raise InvalidContextError(
                    f"computation_versions[{i}] must be ComputationVersion: {type(comp)}"
                )
        
        # Verify computations are sorted (for determinism)
        sorted_comps = tuple(sorted(self.computation_versions, key=lambda c: (c.name, c.version)))
        if self.computation_versions != sorted_comps:
            raise InvalidContextError(
                "computation_versions must be sorted by (name, version) for determinism"
            )
    
    def _validate_timing(self) -> None:
        """Validate timing fields.
        
        Note:
            This validation does NOT use wall-clock time to maintain
            determinism and replay safety. Timestamp validation is limited
            to structural checks (type and positivity).
        """
        if not isinstance(self.execution_timestamp, int):
            raise InvalidContextError(
                f"execution_timestamp must be int: {type(self.execution_timestamp)}"
            )
        
        if self.execution_timestamp <= 0:
            raise InvalidContextError(
                f"execution_timestamp must be positive: {self.execution_timestamp}"
            )
    
    def _validate_run_id(self) -> None:
        """Validate run ID matches expected format and content."""
        if not self.run_id:
            raise InvalidContextError("run_id cannot be empty")
        
        # Verify run ID format: run_{timestamp}_{hash}
        if not self.run_id.startswith("run_"):
            raise InvalidContextError(f"run_id must start with 'run_': {self.run_id}")
        
        parts = self.run_id.split("_")
        if len(parts) != 3:
            raise InvalidContextError(
                f"run_id must have format 'run_{{timestamp}}_{{hash}}': {self.run_id}"
            )
        
        # Verify run ID is deterministic
        expected_run_id = RunIdGenerator.generate(
            self.pipeline_name,
            self.pipeline_version,
            self.input_schema_versions,
            self.computation_versions,
            self.execution_timestamp,
        )
        
        if self.run_id != expected_run_id:
            raise DeterminismViolation(
                f"run_id does not match deterministic generation.\n"
                f"Expected: {expected_run_id}\n"
                f"Got: {self.run_id}"
            )
    
    def _validate_configuration(self) -> None:
        """Validate configuration fields."""
        # Validate hash algorithm
        approved_algorithms = {"sha256", "sha512", "blake2b"}
        if self.hash_algorithm not in approved_algorithms:
            raise InvalidContextError(
                f"hash_algorithm must be one of {approved_algorithms}: {self.hash_algorithm}"
            )
        
        # Validate reproducibility mode
        approved_modes = {"bit_for_bit", "semantic"}
        if self.reproducibility_mode not in approved_modes:
            raise InvalidContextError(
                f"reproducibility_mode must be one of {approved_modes}: {self.reproducibility_mode}"
            )
        
        # Validate time mode
        if not isinstance(self.time_mode, TimeMode):
            raise InvalidContextError(f"time_mode must be TimeMode: {type(self.time_mode)}")
        
        # Validate provenance artifacts
        if not isinstance(self.provenance_artifacts, frozenset):
            raise InvalidContextError("provenance_artifacts must be frozenset (immutable)")
        
        required_artifacts = {"lineage_graph", "schema_manifest", "execution_plan"}
        if not required_artifacts.issubset(self.provenance_artifacts):
            missing = required_artifacts - self.provenance_artifacts
            raise InvalidContextError(
                f"provenance_artifacts missing required artifacts: {missing}"
            )
        
        # Validate parent run ID format if present
        if self.parent_run_id is not None:
            if not self.parent_run_id.startswith("run_"):
                raise InvalidContextError(
                    f"parent_run_id must start with 'run_': {self.parent_run_id}"
                )
    
    def _is_valid_semver(self, version: str) -> bool:
        """Check if version string is valid semantic version."""
        import re
        pattern = r'^\d+\.\d+\.\d+(?:-[a-zA-Z0-9-]+)?(?:\+[a-zA-Z0-9-]+)?$'
        return bool(re.match(pattern, version))
    
    # ========================================================================
    # PUBLIC API
    # ========================================================================
    
    def get_context_hash(self) -> str:
        """Get deterministic hash of entire context.
        
        Returns:
            SHA256 hash of context
        """
        canonical = self._to_canonical_string()
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    
    def _to_canonical_string(self) -> str:
        """Convert context to canonical string representation."""
        parts = [
            f"pipeline_name={self.pipeline_name}",
            f"pipeline_version={self.pipeline_version}",
            f"run_id={self.run_id}",
            f"triggered_by={self.triggered_by.value}",
            f"execution_mode={self.execution_mode.value}",
            f"execution_timestamp={self.execution_timestamp}",
        ]
        
        # Add input schemas
        schema_strs = [str(s) for s in self.input_schema_versions]
        parts.append(f"input_schemas=[{','.join(schema_strs)}]")
        
        # Add output schema
        parts.append(f"output_schema={self.output_schema_version}")
        
        # Add computations
        comp_strs = [str(c) for c in self.computation_versions]
        parts.append(f"computations=[{','.join(comp_strs)}]")
        
        # Add configuration
        parts.append(f"hash_algorithm={self.hash_algorithm}")
        parts.append(f"reproducibility_mode={self.reproducibility_mode}")
        parts.append(f"time_mode={self.time_mode.value}")
        
        return "::".join(parts)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert context to dictionary.
        
        Returns:
            Dictionary representation of context
        """
        return {
            "pipeline_name": self.pipeline_name,
            "pipeline_version": self.pipeline_version,
            "run_id": self.run_id,
            "triggered_by": self.triggered_by.value,
            "execution_mode": self.execution_mode.value,
            "input_schema_versions": [s.to_dict() for s in self.input_schema_versions],
            "output_schema_version": self.output_schema_version.to_dict(),
            "computation_versions": [c.to_dict() for c in self.computation_versions],
            "execution_timestamp": self.execution_timestamp,
            "metadata": dict(self.metadata),
            "hash_algorithm": self.hash_algorithm,
            "reproducibility_mode": self.reproducibility_mode,
            "time_mode": self.time_mode.value,
            "provenance_artifacts": list(self.provenance_artifacts),
            "parent_run_id": self.parent_run_id,
            "context_hash": self.get_context_hash(),
        }
    
    def to_json(self) -> str:
        """Convert context to JSON string.
        
        Returns:
            JSON representation of context
        """
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)
    
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> PipelineContext:
        """Create context from dictionary.
        
        Args:
            d: Dictionary representation
            
        Returns:
            PipelineContext instance
        """
        return cls(
            pipeline_name=d["pipeline_name"],
            pipeline_version=d["pipeline_version"],
            run_id=d["run_id"],
            triggered_by=TriggerType(d["triggered_by"]),
            execution_mode=ExecutionMode(d["execution_mode"]),
            input_schema_versions=tuple(
                SchemaVersion.from_dict(s) for s in d["input_schema_versions"]
            ),
            output_schema_version=SchemaVersion.from_dict(d["output_schema_version"]),
            computation_versions=tuple(
                ComputationVersion.from_dict(c) for c in d["computation_versions"]
            ),
            execution_timestamp=d["execution_timestamp"],
            metadata=frozenset(d.get("metadata", {}).items()) if d.get("metadata") else frozenset(),
            hash_algorithm=d["hash_algorithm"],
            reproducibility_mode=d["reproducibility_mode"],
            time_mode=TimeMode(d["time_mode"]),
            provenance_artifacts=frozenset(d["provenance_artifacts"]),
            parent_run_id=d.get("parent_run_id"),
        )
    
    @classmethod
    def from_json(cls, json_str: str) -> PipelineContext:
        """Create context from JSON string.
        
        Args:
            json_str: JSON representation
            
        Returns:
            PipelineContext instance
        """
        d = json.loads(json_str)
        return cls.from_dict(d)
    
    def is_replay(self) -> bool:
        """Check if this is a replay execution."""
        return self.triggered_by == TriggerType.REPLAY
    
    def is_recovery(self) -> bool:
        """Check if this is a recovery execution."""
        return self.triggered_by == TriggerType.RECOVERY
    
    def is_production(self) -> bool:
        """Check if this is a production execution."""
        return self.execution_mode == ExecutionMode.PRODUCTION
    
    def get_metadata(self, key: str) -> Optional[str]:
        """Get metadata value by key.
        
        Args:
            key: Metadata key
            
        Returns:
            Metadata value or None
        """
        for k, v in self.metadata:
            if k == key:
                return v
        return None
    
    def with_metadata(self, key: str, value: str) -> PipelineContext:
        """Create new context with additional metadata.
        
        Args:
            key: Metadata key
            value: Metadata value
            
        Returns:
            New PipelineContext with added metadata
        """
        new_metadata = frozenset(list(self.metadata) + [(key, value)])
        
        return PipelineContext(
            pipeline_name=self.pipeline_name,
            pipeline_version=self.pipeline_version,
            run_id=self.run_id,
            triggered_by=self.triggered_by,
            execution_mode=self.execution_mode,
            input_schema_versions=self.input_schema_versions,
            output_schema_version=self.output_schema_version,
            computation_versions=self.computation_versions,
            execution_timestamp=self.execution_timestamp,
            metadata=new_metadata,
            hash_algorithm=self.hash_algorithm,
            reproducibility_mode=self.reproducibility_mode,
            time_mode=self.time_mode,
            provenance_artifacts=self.provenance_artifacts,
            parent_run_id=self.parent_run_id,
        )
    
    def __str__(self) -> str:
        """String representation."""
        return (
            f"PipelineContext("
            f"name={self.pipeline_name}, "
            f"version={self.pipeline_version}, "
            f"run_id={self.run_id}, "
            f"trigger={self.triggered_by.value})"
        )
    
    def __repr__(self) -> str:
        """Detailed representation."""
        return (
            f"PipelineContext(\n"
            f"  pipeline_name={self.pipeline_name!r},\n"
            f"  pipeline_version={self.pipeline_version!r},\n"
            f"  run_id={self.run_id!r},\n"
            f"  triggered_by={self.triggered_by},\n"
            f"  execution_mode={self.execution_mode},\n"
            f"  input_schemas={len(self.input_schema_versions)},\n"
            f"  computations={len(self.computation_versions)},\n"
            f"  timestamp={self.execution_timestamp}\n"
            f")"
        )


# ============================================================================
# CONTEXT BUILDER
# ============================================================================


class PipelineContextBuilder:
    """Builder for creating PipelineContext instances.
    
    Provides a fluent interface for constructing contexts with validation.
    """
    
    def __init__(self):
        self._pipeline_name: Optional[str] = None
        self._pipeline_version: Optional[str] = None
        self._triggered_by: Optional[TriggerType] = None
        self._execution_mode: ExecutionMode = ExecutionMode.PRODUCTION
        self._input_schemas: List[SchemaVersion] = []
        self._output_schema: Optional[SchemaVersion] = None
        self._computations: List[ComputationVersion] = []
        self._execution_timestamp: Optional[int] = None
        self._metadata: Dict[str, str] = {}
        self._hash_algorithm: str = "sha256"
        self._reproducibility_mode: str = "bit_for_bit"
        self._time_mode: TimeMode = TimeMode.EVENT_TIME
        self._provenance_artifacts: Set[str] = {
            "lineage_graph", "schema_manifest", "execution_plan"
        }
        self._parent_run_id: Optional[str] = None
    
    def pipeline(self, name: str, version: str) -> PipelineContextBuilder:
        """Set pipeline name and version."""
        self._pipeline_name = name
        self._pipeline_version = version
        return self
    
    def triggered_by(self, trigger: TriggerType) -> PipelineContextBuilder:
        """Set trigger type."""
        self._triggered_by = trigger
        return self
    
    def execution_mode(self, mode: ExecutionMode) -> PipelineContextBuilder:
        """Set execution mode."""
        self._execution_mode = mode
        return self
    
    def add_input_schema(self, name: str, version: int) -> PipelineContextBuilder:
        """Add input schema version."""
        self._input_schemas.append(SchemaVersion(name=name, version=version))
        return self
    
    def output_schema(self, name: str, version: int) -> PipelineContextBuilder:
        """Set output schema version."""
        self._output_schema = SchemaVersion(name=name, version=version)
        return self
    
    def add_computation(self, name: str, version: str) -> PipelineContextBuilder:
        """Add computation version."""
        self._computations.append(ComputationVersion(name=name, version=version))
        return self
    
    def timestamp(self, ts: int) -> PipelineContextBuilder:
        """Set execution timestamp."""
        self._execution_timestamp = ts
        return self
    
    
    def add_metadata(self, key: str, value: str) -> PipelineContextBuilder:
        """Add metadata."""
        self._metadata[key] = value
        return self
    
    def hash_algorithm(self, algorithm: str) -> PipelineContextBuilder:
        """Set hash algorithm."""
        self._hash_algorithm = algorithm
        return self
    
    def reproducibility_mode(self, mode: str) -> PipelineContextBuilder:
        """Set reproducibility mode."""
        self._reproducibility_mode = mode
        return self
    
    def time_mode(self, mode: TimeMode) -> PipelineContextBuilder:
        """Set time mode."""
        self._time_mode = mode
        return self
    
    def parent_run(self, run_id: str) -> PipelineContextBuilder:
        """Set parent run ID."""
        self._parent_run_id = run_id
        return self
    
    def build(self) -> PipelineContext:
        """Build and validate the context.
        
        Returns:
            Validated PipelineContext
            
        Raises:
            InvalidContextError: If required fields are missing
        """
        # Validate required fields
        if not self._pipeline_name:
            raise InvalidContextError("pipeline_name is required")
        if not self._pipeline_version:
            raise InvalidContextError("pipeline_version is required")
        if not self._triggered_by:
            raise InvalidContextError("triggered_by is required")
        if not self._input_schemas:
            raise InvalidContextError("At least one input schema is required")
        if not self._output_schema:
            raise InvalidContextError("output_schema is required")
        if not self._computations:
            raise InvalidContextError("At least one computation is required")
        if self._execution_timestamp is None:
            raise InvalidContextError("execution_timestamp is required")
        
        # Sort for determinism
        sorted_schemas = tuple(sorted(self._input_schemas, key=lambda s: (s.name, s.version)))
        sorted_comps = tuple(sorted(self._computations, key=lambda c: (c.name, c.version)))
        
        # Generate run ID
        run_id = RunIdGenerator.generate(
            pipeline_name=self._pipeline_name,
            pipeline_version=self._pipeline_version,
            input_schema_versions=sorted_schemas,
            computation_versions=sorted_comps,
            execution_timestamp=self._execution_timestamp,
        )
        
        # Build context
        return PipelineContext(
            pipeline_name=self._pipeline_name,
            pipeline_version=self._pipeline_version,
            run_id=run_id,
            triggered_by=self._triggered_by,
            execution_mode=self._execution_mode,
            input_schema_versions=sorted_schemas,
            output_schema_version=self._output_schema,
            computation_versions=sorted_comps,
            execution_timestamp=self._execution_timestamp,
            metadata=frozenset(self._metadata.items()),
            hash_algorithm=self._hash_algorithm,
            reproducibility_mode=self._reproducibility_mode,
            time_mode=self._time_mode,
            provenance_artifacts=frozenset(self._provenance_artifacts),
            parent_run_id=self._parent_run_id,
        )


# ============================================================================
# EXPORTS
# ============================================================================


__all__ = [
    # Enums
    "TriggerType",
    "ExecutionMode",
    "TimeMode",
    # Exceptions
    "PipelineContextError",
    "InvalidContextError",
    "ImmutabilityViolation",
    "DeterminismViolation",
    # Version types
    "SchemaVersion",
    "ComputationVersion",
    # Core classes
    "PipelineContext",
    "PipelineContextBuilder",
    "RunIdGenerator",
]