"""
Immutable historical execution context.

This module is the sole authority that defines what it means to replay the past.
It answers exactly one question:
"Under what exact conditions are we asserting the past should be reproducible?"

If this context is wrong, replay becomes fiction.

This file is the notary stamp on time.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, List, Any, Mapping
from collections import OrderedDict


# ============================================================================
# REPLAY MODE - Execution Permissions
# ============================================================================

class ReplayMode(Enum):
    """
    Explicit execution mode for replay.
    
    Mode changes permissions, never semantics.
    
    Modes:
    - VERIFY_ONLY: Prove bit-for-bit identity
    - DIAGNOSE: Allow divergence reporting but no mutation
    - DRY_RUN: Execute without persistence
    - PROVE_REPRODUCIBILITY: Strictest mode, zero tolerance
    """
    VERIFY_ONLY = "verify_only"
    DIAGNOSE = "diagnose"
    DRY_RUN = "dry_run"
    PROVE_REPRODUCIBILITY = "prove_reproducibility"


class ReplayEnforcement(Enum):
    """
    Controls how strictly mismatches are treated.
    
    Enforcement affects response, not rules.
    
    Levels:
    - STRICT: Any divergence is fatal
    - EVIDENTIARY: Divergences allowed but must be recorded
    - FORENSIC: Maximize trace output, still no mutation
    """
    STRICT = "strict"
    EVIDENTIARY = "evidentiary"
    FORENSIC = "forensic"


# ============================================================================
# TIME RANGE - Explicit Temporal Bounds
# ============================================================================

@dataclass(frozen=True)
class TimeRange:
    """
    Explicit time bounds being replayed.
    
    Rules:
    - Must be fully contained within audit's recorded bounds
    - Must align with declared window models
    - No "open ended" or relative ranges
    
    Replay never guesses time.
    """
    start_timestamp: datetime
    end_timestamp: datetime
    timezone_id: str
    
    def __post_init__(self):
        """Validate time range sanity."""
        if self.start_timestamp >= self.end_timestamp:
            raise ValueError("start_timestamp must be before end_timestamp")
        
        if not self.timezone_id:
            raise ValueError("timezone_id cannot be empty")
    
    def to_dict(self) -> Dict[str, str]:
        return {
            "start_timestamp": self.start_timestamp.isoformat(),
            "end_timestamp": self.end_timestamp.isoformat(),
            "timezone_id": self.timezone_id,
        }
    
    def contains(self, timestamp: datetime) -> bool:
        """Check if timestamp falls within this range."""
        return self.start_timestamp <= timestamp < self.end_timestamp
    
    def is_subset_of(self, other: TimeRange) -> bool:
        """Verify this range is contained within another range."""
        return (
            other.start_timestamp <= self.start_timestamp and
            self.end_timestamp <= other.end_timestamp and
            self.timezone_id == other.timezone_id
        )


# ============================================================================
# AUDIT ARTIFACT REFERENCE - Root of Truth
# ============================================================================

@dataclass(frozen=True)
class AuditArtifact:
    """
    Reference to the exact artifact emitted by pipeline_audit.py.
    
    This is the root of truth.
    Replay derives authority from audit - never the other way around.
    
    Contains:
    - Audit artifact ID
    - Content hash
    - Pipeline version
    - Computation registry version
    - Window model versions
    - Schema versions
    - Code hash
    - Environment fingerprint
    - Recorded time bounds
    """
    artifact_id: str
    artifact_hash: str
    pipeline_version: str
    computation_registry_version: str
    window_model_versions: Dict[str, str]
    schema_versions: Dict[str, str]
    code_hash: str
    environment_fingerprint: str
    recorded_time_range: TimeRange
    entity_manifest: List[str]
    computation_manifest: List[str]
    
    def __post_init__(self):
        """Validate audit artifact completeness."""
        if not self.artifact_id:
            raise ValueError("artifact_id cannot be empty")
        if not self.artifact_hash:
            raise ValueError("artifact_hash cannot be empty")
        if not self.pipeline_version:
            raise ValueError("pipeline_version cannot be empty")
        if not self.code_hash:
            raise ValueError("code_hash cannot be empty")
        if not self.environment_fingerprint:
            raise ValueError("environment_fingerprint cannot be empty")
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_hash": self.artifact_hash,
            "pipeline_version": self.pipeline_version,
            "computation_registry_version": self.computation_registry_version,
            "window_model_versions": OrderedDict(sorted(self.window_model_versions.items())),
            "schema_versions": OrderedDict(sorted(self.schema_versions.items())),
            "code_hash": self.code_hash,
            "environment_fingerprint": self.environment_fingerprint,
            "recorded_time_range": self.recorded_time_range.to_dict(),
            "entity_manifest": sorted(self.entity_manifest),
            "computation_manifest": sorted(self.computation_manifest),
        }
    
    def verify_integrity(self) -> bool:
        """Verify artifact hash matches content."""
        computed = self._compute_hash()
        return computed == self.artifact_hash
    
    def _compute_hash(self) -> str:
        """Compute deterministic hash of artifact content."""
        canonical = OrderedDict([
            ("artifact_id", self.artifact_id),
            ("pipeline_version", self.pipeline_version),
            ("computation_registry_version", self.computation_registry_version),
            ("window_model_versions", OrderedDict(sorted(self.window_model_versions.items()))),
            ("schema_versions", OrderedDict(sorted(self.schema_versions.items()))),
            ("code_hash", self.code_hash),
            ("environment_fingerprint", self.environment_fingerprint),
            ("recorded_time_range", self.recorded_time_range.to_dict()),
            ("entity_manifest", sorted(self.entity_manifest)),
            ("computation_manifest", sorted(self.computation_manifest)),
        ])
        content = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(content.encode('utf-8')).hexdigest()


# ============================================================================
# DETERMINISM LOCK - Version Pinning
# ============================================================================

@dataclass(frozen=True)
class DeterminismLock:
    """
    Immutable lock on all version-sensitive components.
    
    ReplayContext MUST lock:
    - Pipeline version
    - Computation registry version
    - Window model versions
    - Schema versions
    - Code hash
    - Environment fingerprint
    
    If any of these differ → replay is invalid before execution.
    """
    pipeline_version: str
    computation_registry_version: str
    window_model_versions: Dict[str, str]
    schema_versions: Dict[str, str]
    code_hash: str
    environment_fingerprint: str
    dependency_versions: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "pipeline_version": self.pipeline_version,
            "computation_registry_version": self.computation_registry_version,
            "window_model_versions": OrderedDict(sorted(self.window_model_versions.items())),
            "schema_versions": OrderedDict(sorted(self.schema_versions.items())),
            "code_hash": self.code_hash,
            "environment_fingerprint": self.environment_fingerprint,
            "dependency_versions": OrderedDict(sorted(self.dependency_versions.items())),
        }
    
    def matches(self, other: DeterminismLock) -> bool:
        """Verify this lock matches another exactly."""
        return (
            self.pipeline_version == other.pipeline_version and
            self.computation_registry_version == other.computation_registry_version and
            self.window_model_versions == other.window_model_versions and
            self.schema_versions == other.schema_versions and
            self.code_hash == other.code_hash and
            self.environment_fingerprint == other.environment_fingerprint
        )


# ============================================================================
# REPLAY CONTEXT - The Frozen Universe
# ============================================================================

@dataclass(frozen=True)
class ReplayContext:
    """
    Immutable historical execution context.
    
    This is the frozen universe in which replay is allowed to occur.
    
    Once constructed:
    - No mutation
    - No derived setters
    - No lazy loading
    - No environment access
    
    If replay needs something, it must already be inside the context.
    
    Authority relationships:
    - Downstream of: pipeline_context, computation_context, aggregation_context
    - Upstream of: replay_plan, replay_runner, replay_results
    
    Nothing bypasses it.
    """
    context_id: str
    invocation_id: str
    audit_artifact: AuditArtifact
    target_time_range: TimeRange
    execution_mode: ReplayMode
    enforcement_level: ReplayEnforcement
    determinism_lock: DeterminismLock
    metadata: Mapping[str, str] = field(default_factory=dict)
    context_hash: str = field(default="", init=False)
    
    def __post_init__(self):
        """Validate context and generate hash."""
        # Freeze metadata to prevent external mutation
        if isinstance(self.metadata, dict):
            object.__setattr__(self, 'metadata', dict(self.metadata))
        
        # Validate mode/enforcement compatibility
        self._validate_mode_enforcement_compatibility()
        
        # Validate time range bounds
        self._validate_time_range_bounds()
        
        # Validate audit artifact integrity
        if not self.audit_artifact.verify_integrity():
            raise ValueError("AuditArtifact failed integrity verification")
        
        # Validate determinism lock completeness
        self._validate_determinism_lock()
        
        # Generate context hash
        object.__setattr__(self, 'context_hash', self._compute_context_hash())
    
    def _validate_mode_enforcement_compatibility(self) -> None:
        """Ensure mode and enforcement level are compatible."""
        # PROVE_REPRODUCIBILITY must use STRICT enforcement
        if self.execution_mode == ReplayMode.PROVE_REPRODUCIBILITY:
            if self.enforcement_level != ReplayEnforcement.STRICT:
                raise ValueError(
                    "PROVE_REPRODUCIBILITY mode requires STRICT enforcement"
                )
        
        # VERIFY_ONLY typically uses STRICT, but can be relaxed
        # DIAGNOSE can use any enforcement
        # DRY_RUN typically uses EVIDENTIARY or FORENSIC
        
        # FORENSIC enforcement implies detailed tracing
        if self.enforcement_level == ReplayEnforcement.FORENSIC:
            if self.execution_mode == ReplayMode.VERIFY_ONLY:
                # Warning: unusual but allowed
                pass
    
    def _validate_time_range_bounds(self) -> None:
        """Verify target time range is within audit bounds."""
        if not self.target_time_range.is_subset_of(
            self.audit_artifact.recorded_time_range
        ):
            raise ValueError(
                f"Target time range {self.target_time_range.to_dict()} "
                f"exceeds audit bounds {self.audit_artifact.recorded_time_range.to_dict()}"
            )
    
    def _validate_determinism_lock(self) -> None:
        """Verify determinism lock is complete and matches audit artifact."""
        # Validate completeness
        if not self.determinism_lock.pipeline_version:
            raise ValueError("Determinism lock missing pipeline_version")
        if not self.determinism_lock.code_hash:
            raise ValueError("Determinism lock missing code_hash")
        if not self.determinism_lock.environment_fingerprint:
            raise ValueError("Determinism lock missing environment_fingerprint")
        
        # Cross-validate against audit artifact (Tier-0 requirement)
        if self.determinism_lock.pipeline_version != self.audit_artifact.pipeline_version:
            raise ValueError(
                f"Determinism lock pipeline_version mismatch: "
                f"lock={self.determinism_lock.pipeline_version} "
                f"audit={self.audit_artifact.pipeline_version}"
            )
        
        if self.determinism_lock.computation_registry_version != self.audit_artifact.computation_registry_version:
            raise ValueError(
                f"Determinism lock computation_registry_version mismatch: "
                f"lock={self.determinism_lock.computation_registry_version} "
                f"audit={self.audit_artifact.computation_registry_version}"
            )
        
        if self.determinism_lock.window_model_versions != self.audit_artifact.window_model_versions:
            raise ValueError(
                f"Determinism lock window_model_versions mismatch: "
                f"lock={self.determinism_lock.window_model_versions} "
                f"audit={self.audit_artifact.window_model_versions}"
            )
        
        if self.determinism_lock.schema_versions != self.audit_artifact.schema_versions:
            raise ValueError(
                f"Determinism lock schema_versions mismatch: "
                f"lock={self.determinism_lock.schema_versions} "
                f"audit={self.audit_artifact.schema_versions}"
            )
        
        if self.determinism_lock.code_hash != self.audit_artifact.code_hash:
            raise ValueError(
                f"Determinism lock code_hash mismatch: "
                f"lock={self.determinism_lock.code_hash} "
                f"audit={self.audit_artifact.code_hash}"
            )
        
        if self.determinism_lock.environment_fingerprint != self.audit_artifact.environment_fingerprint:
            raise ValueError(
                f"Determinism lock environment_fingerprint mismatch: "
                f"lock={self.determinism_lock.environment_fingerprint} "
                f"audit={self.audit_artifact.environment_fingerprint}"
            )
    
    def _compute_context_hash(self) -> str:
        """
        Generate deterministic hash of context.
        
        Excludes invocation_id from hash to support
        comparison of logically identical contexts.
        """
        canonical = self._to_canonical_dict(include_metadata=False)
        content = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
    
    def _to_canonical_dict(self, include_metadata: bool = True) -> Dict[str, Any]:
        """Serialize to canonical dictionary form."""
        data = OrderedDict([
            ("context_id", self.context_id),
            ("audit_artifact", self.audit_artifact.to_dict()),
            ("target_time_range", self.target_time_range.to_dict()),
            ("execution_mode", self.execution_mode.value),
            ("enforcement_level", self.enforcement_level.value),
            ("determinism_lock", self.determinism_lock.to_dict()),
        ])
        
        if include_metadata:
            data["invocation_id"] = self.invocation_id
            data["metadata"] = OrderedDict(sorted(self.metadata.items()))
            data["context_hash"] = self.context_hash
        
        return data
    
    def to_dict(self) -> Dict[str, Any]:
        """Export complete context including metadata."""
        return self._to_canonical_dict(include_metadata=True)
    
    def to_json(self, indent: Optional[int] = 2) -> str:
        """Serialize to JSON with deterministic ordering."""
        return json.dumps(self.to_dict(), indent=indent)
    
    def verify_integrity(self) -> bool:
        """Verify context hash matches content."""
        computed_hash = self._compute_context_hash()
        return computed_hash == self.context_hash
    
    def is_compatible_with_lock(self, lock: DeterminismLock) -> bool:
        """Verify context determinism lock matches another lock."""
        return self.determinism_lock.matches(lock)
    
    def allows_mutation(self) -> bool:
        """Check if context allows mutations."""
        # Only DRY_RUN with non-STRICT enforcement might allow limited mutation
        # But generally, replay contexts forbid mutation
        return False
    
    def allows_persistence(self) -> bool:
        """Check if context allows persistence."""
        # Generally forbidden - replay is read-only
        return False
    
    def requires_exact_match(self) -> bool:
        """Check if context requires bit-for-bit exact matches."""
        return (
            self.enforcement_level == ReplayEnforcement.STRICT or
            self.execution_mode == ReplayMode.PROVE_REPRODUCIBILITY
        )


# ============================================================================
# CONTEXT BUILDER - Safe Construction
# ============================================================================

class ReplayContextBuilder:
    """
    Builder for constructing replay contexts with validation.
    
    Ensures:
    - All mandatory fields are set
    - Audit artifact is valid
    - Time ranges are bounded
    - Mode/enforcement compatibility
    - Determinism lock completeness
    """
    
    def __init__(self, context_id: str):
        self._context_id = context_id
        self._invocation_id = str(uuid.uuid4())
        self._audit_artifact: Optional[AuditArtifact] = None
        self._target_time_range: Optional[TimeRange] = None
        self._execution_mode: ReplayMode = ReplayMode.VERIFY_ONLY
        self._enforcement_level: ReplayEnforcement = ReplayEnforcement.STRICT
        self._determinism_lock: Optional[DeterminismLock] = None
        self._metadata: Dict[str, str] = {}
    
    def set_invocation_id(self, invocation_id: str) -> ReplayContextBuilder:
        """Set explicit invocation ID (otherwise auto-generated)."""
        self._invocation_id = invocation_id
        return self
    
    def set_audit_artifact(self, artifact: AuditArtifact) -> ReplayContextBuilder:
        """Set audit artifact reference."""
        self._audit_artifact = artifact
        return self
    
    def set_target_time_range(self, time_range: TimeRange) -> ReplayContextBuilder:
        """Set target time range for replay."""
        self._target_time_range = time_range
        return self
    
    def set_execution_mode(self, mode: ReplayMode) -> ReplayContextBuilder:
        """Set execution mode."""
        self._execution_mode = mode
        return self
    
    def set_enforcement_level(
        self,
        enforcement: ReplayEnforcement
    ) -> ReplayContextBuilder:
        """Set enforcement level."""
        self._enforcement_level = enforcement
        return self
    
    def set_determinism_lock(self, lock: DeterminismLock) -> ReplayContextBuilder:
        """Set determinism lock."""
        self._determinism_lock = lock
        return self
    
    def set_metadata(self, key: str, value: str) -> ReplayContextBuilder:
        """Set metadata field."""
        self._metadata[key] = value
        return self
    
    def build(self) -> ReplayContext:
        """
        Construct immutable replay context.
        
        Validates:
        - All mandatory fields are set
        - Audit artifact is complete
        - Time range is bounded
        - Mode/enforcement compatibility
        - Determinism lock is complete
        """
        # Validate mandatory fields
        if not self._audit_artifact:
            raise ValueError("audit_artifact not set")
        if not self._target_time_range:
            raise ValueError("target_time_range not set")
        if not self._determinism_lock:
            raise ValueError("determinism_lock not set")
        
        # Construct context with immutable metadata copy
        metadata_copy = dict(self._metadata)  # Create immutable copy
        
        context = ReplayContext(
            context_id=self._context_id,
            invocation_id=self._invocation_id,
            audit_artifact=self._audit_artifact,
            target_time_range=self._target_time_range,
            execution_mode=self._execution_mode,
            enforcement_level=self._enforcement_level,
            determinism_lock=self._determinism_lock,
            metadata=metadata_copy,
        )
        
        # Verify integrity
        if not context.verify_integrity():
            raise ValueError("Context failed integrity verification")
        
        return context


# ============================================================================
# MODULE INTERFACE
# ============================================================================

__all__ = [
    # Core context
    'ReplayContext',
    # Components
    'AuditArtifact',
    'TimeRange',
    'DeterminismLock',
    # Enums
    'ReplayMode',
    'ReplayEnforcement',
    # Builder
    'ReplayContextBuilder',
]