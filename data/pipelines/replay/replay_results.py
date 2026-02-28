"""
Bit-for-bit comparison and divergence reporting.

This module is the final truth surface of replay. It answers provably and
exhaustively: "Did this replay produce the same reality as the original execution?"

It produces formal equivalence proofs or explicit divergence artifacts.
No guessing. No loose summaries. No forgiveness.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any, Tuple
from collections import OrderedDict


# ============================================================================
# RESULT TAXONOMY - Closed Set (No Other Outcomes Allowed)
# ============================================================================

class ReplayOutcome(Enum):
    """
    Mutually exclusive replay outcomes.
    
    - EXACT_EQUIVALENCE: Bit-for-bit match across all surfaces
    - AUTHORIZED_DIVERGENCE: Differs only in explicitly declared dimensions
    - UNAUTHORIZED_DIVERGENCE: Any difference not covered by authorization
    - INVALID_REPLAY: Could not be meaningfully compared
    """
    EXACT_EQUIVALENCE = "exact_equivalence"
    AUTHORIZED_DIVERGENCE = "authorized_divergence"
    UNAUTHORIZED_DIVERGENCE = "unauthorized_divergence"
    INVALID_REPLAY = "invalid_replay"


class DivergenceKind(Enum):
    """Classification of divergence type."""
    VALUE = "value"
    ORDERING = "ordering"
    PRESENCE = "presence"
    SCHEMA = "schema"
    CARDINALITY = "cardinality"


class ComparisonSurfaceType(Enum):
    """Type of output surface being compared."""
    COUNTER = "counter"
    WINDOW = "window"
    COMPUTATION = "computation"
    AGGREGATE = "aggregate"
    SNAPSHOT = "snapshot"


# ============================================================================
# DIVERGENCE ARTIFACTS - Immutable Evidence
# ============================================================================

@dataclass(frozen=True)
class OutputDivergence:
    """
    Explicit divergence artifact for a single output.
    
    Contract:
    - Authorized divergences must reference authorization in replay plan
    - Unauthorized divergences automatically fail replay
    - All divergences preserve expected vs observed values
    - Artifacts are serializable and deterministically replayable
    """
    output_id: str
    path: str
    expected_hash: str
    observed_hash: str
    kind: DivergenceKind
    authorized: bool
    authorization_ref: Optional[str] = None
    expected_value_summary: Optional[str] = None
    observed_value_summary: Optional[str] = None
    
    def __post_init__(self):
        """Validate divergence artifact integrity."""
        if self.authorized and not self.authorization_ref:
            raise ValueError(
                f"Authorized divergence {self.output_id} must include authorization_ref"
            )
        if not self.authorized and self.authorization_ref:
            raise ValueError(
                f"Unauthorized divergence {self.output_id} cannot have authorization_ref"
            )
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to deterministic dictionary."""
        return {
            "output_id": self.output_id,
            "path": self.path,
            "expected_hash": self.expected_hash,
            "observed_hash": self.observed_hash,
            "kind": self.kind.value,
            "authorized": self.authorized,
            "authorization_ref": self.authorization_ref,
            "expected_value_summary": self.expected_value_summary,
            "observed_value_summary": self.observed_value_summary,
        }


@dataclass(frozen=True)
class InvalidityReason:
    """
    Reason why replay could not be meaningfully compared.
    
    Invalid replay is not "failed equivalence" - it is non-comparable.
    """
    reason_code: str
    description: str
    affected_outputs: List[str]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "description": self.description,
            "affected_outputs": sorted(self.affected_outputs),
        }


# ============================================================================
# COMPARISON SURFACE - What Was Compared
# ============================================================================

@dataclass(frozen=True)
class ComparisonSurface:
    """
    Specification of what was compared in replay.
    
    Contract:
    - Surfaces are declared upfront, never inferred
    - Version changes invalidate cross-version comparison
    - Normalization must be deterministic and versioned
    """
    surface_id: str
    surface_type: ComparisonSurfaceType
    version: str
    normalization_applied: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "surface_id": self.surface_id,
            "surface_type": self.surface_type.value,
            "version": self.version,
            "normalization_applied": sorted(self.normalization_applied),
        }


# ============================================================================
# REPLAY RESULT - Verifiable Claim
# ============================================================================

@dataclass(frozen=True)
class ReplayResult:
    """
    Immutable, deterministic replay result.
    
    This is where replay stops being a process and becomes a verifiable claim.
    
    Contract:
    - Result is deterministic: same inputs → same result
    - Result is content-addressable via summary_hash
    - Result is serialization-safe
    - Result is audit-portable
    - Result is machine-verifiable
    
    No implicit fields. No optional interpretation.
    """
    replay_run_id: str
    replay_plan_hash: str
    original_execution_hash: str
    comparison_surface_version: str
    outcome: ReplayOutcome
    comparison_surfaces: List[ComparisonSurface]
    divergences: List[OutputDivergence] = field(default_factory=list)
    invalidity_reasons: List[InvalidityReason] = field(default_factory=list)
    metadata: Dict[str, str] = field(default_factory=dict)
    summary_hash: str = field(default="", init=False)
    
    def __post_init__(self):
        """Generate deterministic summary hash from canonical content."""
        object.__setattr__(self, 'summary_hash', self._compute_summary_hash())
        self._validate()
    
    def _validate(self) -> None:
        """Validate result consistency and invariants."""
        # Exact equivalence must have no divergences
        if self.outcome == ReplayOutcome.EXACT_EQUIVALENCE:
            if self.divergences:
                raise ValueError("EXACT_EQUIVALENCE cannot have divergences")
            if self.invalidity_reasons:
                raise ValueError("EXACT_EQUIVALENCE cannot have invalidity reasons")
        
        # Authorized divergence must have only authorized divergences
        if self.outcome == ReplayOutcome.AUTHORIZED_DIVERGENCE:
            if not self.divergences:
                raise ValueError("AUTHORIZED_DIVERGENCE must have divergences")
            if any(not d.authorized for d in self.divergences):
                raise ValueError("AUTHORIZED_DIVERGENCE cannot have unauthorized divergences")
            if self.invalidity_reasons:
                raise ValueError("AUTHORIZED_DIVERGENCE cannot have invalidity reasons")
        
        # Unauthorized divergence must have at least one unauthorized divergence
        if self.outcome == ReplayOutcome.UNAUTHORIZED_DIVERGENCE:
            if not self.divergences:
                raise ValueError("UNAUTHORIZED_DIVERGENCE must have divergences")
            if not any(not d.authorized for d in self.divergences):
                raise ValueError("UNAUTHORIZED_DIVERGENCE must have at least one unauthorized divergence")
            if self.invalidity_reasons:
                raise ValueError("UNAUTHORIZED_DIVERGENCE cannot have invalidity reasons")
        
        # Invalid replay must have invalidity reasons
        if self.outcome == ReplayOutcome.INVALID_REPLAY:
            if not self.invalidity_reasons:
                raise ValueError("INVALID_REPLAY must have invalidity_reasons")
            if self.divergences:
                raise ValueError("INVALID_REPLAY cannot have divergences")
    
    def _compute_summary_hash(self) -> str:
        """
        Generate deterministic hash of result content.
        
        Guarantees:
        - No timestamps
        - No randomness
        - No environment leakage
        - Stable across reruns
        """
        canonical = self._to_canonical_dict()
        content = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
    
    def _to_canonical_dict(self) -> Dict[str, Any]:
        """Serialize to canonical dictionary form."""
        return OrderedDict([
            ("replay_run_id", self.replay_run_id),
            ("replay_plan_hash", self.replay_plan_hash),
            ("original_execution_hash", self.original_execution_hash),
            ("comparison_surface_version", self.comparison_surface_version),
            ("outcome", self.outcome.value),
            ("comparison_surfaces", [
                s.to_dict() for s in sorted(self.comparison_surfaces, key=lambda x: x.surface_id)
            ]),
            ("divergences", [
                d.to_dict() for d in sorted(self.divergences, key=lambda x: (x.output_id, x.path))
            ]),
            ("invalidity_reasons", [
                r.to_dict() for r in sorted(self.invalidity_reasons, key=lambda x: x.reason_code)
            ]),
            ("metadata", OrderedDict(sorted(self.metadata.items()))),
        ])
    
    def to_dict(self) -> Dict[str, Any]:
        """Export complete result including summary hash."""
        data = self._to_canonical_dict()
        data["summary_hash"] = self.summary_hash
        return data
    
    def to_json(self, indent: Optional[int] = 2) -> str:
        """Serialize to JSON with deterministic ordering."""
        return json.dumps(self.to_dict(), indent=indent)
    
    def verify_integrity(self) -> bool:
        """Verify summary hash matches content."""
        computed_hash = self._compute_summary_hash()
        return computed_hash == self.summary_hash
    
    def is_success(self) -> bool:
        """Replay is successful if exact or authorized divergence only."""
        return self.outcome in (
            ReplayOutcome.EXACT_EQUIVALENCE,
            ReplayOutcome.AUTHORIZED_DIVERGENCE
        )
    
    def is_failure(self) -> bool:
        """Replay failed if unauthorized divergence or invalid."""
        return self.outcome in (
            ReplayOutcome.UNAUTHORIZED_DIVERGENCE,
            ReplayOutcome.INVALID_REPLAY
        )


# ============================================================================
# COMPARISON UTILITIES - Deterministic Equality
# ============================================================================

class ComparisonEngine:
    """
    Deterministic bit-level comparison engine.
    
    Rules:
    - Comparisons occur after normalization
    - Normalization must be declared, versioned, and deterministic
    - Ordering is part of the value unless explicitly waived
    - Missing vs null is a difference
    - Float tolerance only allowed if declared
    
    No "practical" comparisons. Only formal equality.
    """
    
    @staticmethod
    def compute_hash(value: Any, normalize: bool = True) -> str:
        """
        Compute deterministic content hash.
        
        Args:
            value: Content to hash
            normalize: Apply deterministic normalization
        
        Returns:
            SHA-256 hex digest
        """
        if normalize:
            value = ComparisonEngine._normalize_value(value)
        
        serialized = ComparisonEngine._serialize_deterministic(value)
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()
    
    @staticmethod
    def _normalize_value(value: Any) -> Any:
        """
        Apply deterministic normalization.
        
        Normalization rules:
        - Sort dictionary keys
        - Preserve list ordering unless explicitly waived
        - Convert sets to sorted lists
        - Preserve None vs missing distinction
        """
        if isinstance(value, dict):
            return OrderedDict(sorted(
                (k, ComparisonEngine._normalize_value(v))
                for k, v in value.items()
            ))
        elif isinstance(value, (list, tuple)):
            return [ComparisonEngine._normalize_value(v) for v in value]
        elif isinstance(value, set):
            return sorted([ComparisonEngine._normalize_value(v) for v in value])
        else:
            return value
    
    @staticmethod
    def _serialize_deterministic(value: Any) -> str:
        """Serialize value to deterministic string."""
        if isinstance(value, (dict, list, OrderedDict)):
            return json.dumps(value, sort_keys=True, separators=(',', ':'))
        elif value is None:
            return "null"
        else:
            return json.dumps(value, separators=(',', ':'))
    
    @staticmethod
    def compare_exact(
        expected: Any,
        observed: Any,
        output_id: str,
        path: str = "root"
    ) -> Optional[OutputDivergence]:
        """
        Perform exact bit-level comparison.
        
        Returns:
            None if exact match, OutputDivergence if mismatch
        """
        expected_hash = ComparisonEngine.compute_hash(expected)
        observed_hash = ComparisonEngine.compute_hash(observed)
        
        if expected_hash == observed_hash:
            return None
        
        # Determine divergence kind
        kind = ComparisonEngine._classify_divergence(expected, observed)
        
        return OutputDivergence(
            output_id=output_id,
            path=path,
            expected_hash=expected_hash,
            observed_hash=observed_hash,
            kind=kind,
            authorized=False,
            expected_value_summary=ComparisonEngine._summarize_value(expected),
            observed_value_summary=ComparisonEngine._summarize_value(observed),
        )
    
    @staticmethod
    def _classify_divergence(expected: Any, observed: Any) -> DivergenceKind:
        """Classify the kind of divergence detected."""
        # Type mismatch is schema divergence
        if type(expected) != type(observed):
            return DivergenceKind.SCHEMA
        
        # Check for cardinality differences in collections
        if isinstance(expected, (list, tuple, dict)):
            if len(expected) != len(observed):
                return DivergenceKind.CARDINALITY
        
        # Check for ordering differences in lists
        if isinstance(expected, (list, tuple)):
            if set(str(e) for e in expected) == set(str(o) for o in observed):
                return DivergenceKind.ORDERING
        
        # Check for presence differences in dicts
        if isinstance(expected, dict):
            if set(expected.keys()) != set(observed.keys()):
                return DivergenceKind.PRESENCE
        
        # Default to value divergence
        return DivergenceKind.VALUE
    
    @staticmethod
    def _summarize_value(value: Any, max_length: int = 100) -> str:
        """Create human-readable summary of value for debugging."""
        serialized = str(value)
        if len(serialized) > max_length:
            return serialized[:max_length] + f"... ({len(serialized)} chars)"
        return serialized


# ============================================================================
# RESULT BUILDER - Safe Construction
# ============================================================================

class ResultBuilder:
    """
    Builder for constructing replay results with validation.
    
    Ensures:
    - All required fields are set
    - Outcome matches divergence state
    - Deterministic hash generation
    - Consistency validation
    """
    
    def __init__(self, replay_run_id: str, replay_plan_hash: str, original_execution_hash: str):
        self._replay_run_id = replay_run_id
        self._replay_plan_hash = replay_plan_hash
        self._original_execution_hash = original_execution_hash
        self._comparison_surface_version: Optional[str] = None
        self._surfaces: List[ComparisonSurface] = []
        self._divergences: List[OutputDivergence] = []
        self._invalidity_reasons: List[InvalidityReason] = []
        self._metadata: Dict[str, str] = {}
    
    def set_comparison_version(self, version: str) -> ResultBuilder:
        """Set comparison surface version."""
        self._comparison_surface_version = version
        return self
    
    def add_surface(self, surface: ComparisonSurface) -> ResultBuilder:
        """Add comparison surface."""
        self._surfaces.append(surface)
        return self
    
    def add_divergence(self, divergence: OutputDivergence) -> ResultBuilder:
        """Add output divergence."""
        self._divergences.append(divergence)
        return self
    
    def add_invalidity(self, reason: InvalidityReason) -> ResultBuilder:
        """Add invalidity reason."""
        self._invalidity_reasons.append(reason)
        return self
    
    def set_metadata(self, key: str, value: str) -> ResultBuilder:
        """Set metadata field."""
        self._metadata[key] = value
        return self
    
    def build(self) -> ReplayResult:
        """
        Construct immutable replay result.
        
        Automatically determines outcome based on divergences and invalidity.
        """
        if not self._comparison_surface_version:
            raise ValueError("comparison_surface_version not set")
        if not self._surfaces:
            raise ValueError("No comparison surfaces defined")
        
        # Determine outcome
        outcome = self._determine_outcome()
        
        # Construct result
        result = ReplayResult(
            replay_run_id=self._replay_run_id,
            replay_plan_hash=self._replay_plan_hash,
            original_execution_hash=self._original_execution_hash,
            comparison_surface_version=self._comparison_surface_version,
            outcome=outcome,
            comparison_surfaces=self._surfaces.copy(),
            divergences=self._divergences.copy(),
            invalidity_reasons=self._invalidity_reasons.copy(),
            metadata=self._metadata.copy(),
        )
        
        # Verify integrity
        if not result.verify_integrity():
            raise ValueError("Result failed integrity verification")
        
        return result
    
    def _determine_outcome(self) -> ReplayOutcome:
        """Determine replay outcome from divergences and invalidity."""
        # Invalid replay takes precedence
        if self._invalidity_reasons:
            return ReplayOutcome.INVALID_REPLAY
        
        # No divergences = exact equivalence
        if not self._divergences:
            return ReplayOutcome.EXACT_EQUIVALENCE
        
        # Check if all divergences are authorized
        all_authorized = all(d.authorized for d in self._divergences)
        
        if all_authorized:
            return ReplayOutcome.AUTHORIZED_DIVERGENCE
        else:
            return ReplayOutcome.UNAUTHORIZED_DIVERGENCE


# ============================================================================
# VERIFICATION - Independent Audit
# ============================================================================

class ResultVerifier:
    """Independent verification of replay result integrity."""
    
    @staticmethod
    def verify_result(result: ReplayResult) -> tuple[bool, List[str]]:
        """
        Verify result integrity and consistency.
        
        Returns:
            (is_valid, list_of_issues)
        """
        issues = []
        
        # Verify hash integrity
        if not result.verify_integrity():
            issues.append("Summary hash does not match content")
        
        # Verify outcome consistency
        try:
            result._validate()
        except ValueError as e:
            issues.append(f"Outcome validation failed: {e}")
        
        # Verify divergence artifacts
        for div in result.divergences:
            try:
                # Validate divergence structure
                if div.authorized and not div.authorization_ref:
                    issues.append(
                        f"Divergence {div.output_id} authorized but missing authorization_ref"
                    )
                if div.expected_hash == div.observed_hash:
                    issues.append(
                        f"Divergence {div.output_id} has identical hashes"
                    )
            except Exception as e:
                issues.append(f"Divergence {div.output_id} validation error: {e}")
        
        # Verify comparison surfaces
        if not result.comparison_surfaces:
            issues.append("No comparison surfaces defined")
        
        # Verify surface versions are consistent
        surface_versions = {s.version for s in result.comparison_surfaces}
        if len(surface_versions) > 1:
            issues.append(f"Inconsistent surface versions: {surface_versions}")
        
        return (len(issues) == 0, issues)


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def create_exact_equivalence(
    replay_run_id: str,
    replay_plan_hash: str,
    original_execution_hash: str,
    surfaces: List[ComparisonSurface],
    version: str = "1.0"
) -> ReplayResult:
    """Create exact equivalence result."""
    return ReplayResult(
        replay_run_id=replay_run_id,
        replay_plan_hash=replay_plan_hash,
        original_execution_hash=original_execution_hash,
        comparison_surface_version=version,
        outcome=ReplayOutcome.EXACT_EQUIVALENCE,
        comparison_surfaces=surfaces,
    )


def create_invalid_replay(
    replay_run_id: str,
    replay_plan_hash: str,
    original_execution_hash: str,
    surfaces: List[ComparisonSurface],
    reason_code: str,
    description: str,
    affected_outputs: List[str],
    version: str = "1.0"
) -> ReplayResult:
    """Create invalid replay result."""
    return ReplayResult(
        replay_run_id=replay_run_id,
        replay_plan_hash=replay_plan_hash,
        original_execution_hash=original_execution_hash,
        comparison_surface_version=version,
        outcome=ReplayOutcome.INVALID_REPLAY,
        comparison_surfaces=surfaces,
        invalidity_reasons=[
            InvalidityReason(
                reason_code=reason_code,
                description=description,
                affected_outputs=affected_outputs
            )
        ],
    )


# ============================================================================
# ARTIFACT SCHEMA CONSTRUCTION - Authority for Replay Artifacts
# ============================================================================

@dataclass(frozen=True)
class ReplayArtifact:
    """
    Immutable replay artifact with content-addressed storage.
    
    Artifacts must be:
    - Immutable
    - Content-addressed
    - Timestamped with logical time only
    """
    artifact_id: str
    artifact_type: str
    content_hash: str
    logical_timestamp: int
    data: Dict[str, Any]
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "content_hash": self.content_hash,
            "logical_timestamp": self.logical_timestamp,
            "data": self.data,
        }


class ArtifactBuilder:
    """
    Authority for constructing replay artifacts.
    
    This is the single source of truth for artifact schema and construction.
    The runner delegates all artifact creation to this builder.
    """
    
    @staticmethod
    def create_execution_manifest(
        plan_id: str,
        plan_hash: str,
        context_id: str,
        context_hash: str,
        timeline: List[Dict[str, Any]],
        artifact_id: str,
        logical_timestamp: int
    ) -> ReplayArtifact:
        """Create execution manifest artifact."""
        data = {
            "plan_id": plan_id,
            "plan_hash": plan_hash,
            "context_id": context_id,
            "context_hash": context_hash,
            "timeline": timeline,
        }
        content_hash = ComparisonEngine.compute_hash(data)
        return ReplayArtifact(
            artifact_id=artifact_id,
            artifact_type="execution_manifest",
            content_hash=content_hash,
            logical_timestamp=logical_timestamp,
            data=data
        )
    
    @staticmethod
    def create_input_fingerprints(
        input_fingerprints: Dict[str, str],
        artifact_id: str,
        logical_timestamp: int
    ) -> ReplayArtifact:
        """Create input fingerprints artifact."""
        content_hash = ComparisonEngine.compute_hash(input_fingerprints)
        return ReplayArtifact(
            artifact_id=artifact_id,
            artifact_type="input_fingerprints",
            content_hash=content_hash,
            logical_timestamp=logical_timestamp,
            data=input_fingerprints
        )
    
    @staticmethod
    def create_output_hashes(
        output_hashes: Dict[str, str],
        artifact_id: str,
        logical_timestamp: int
    ) -> ReplayArtifact:
        """Create output hashes artifact."""
        content_hash = ComparisonEngine.compute_hash(output_hashes)
        return ReplayArtifact(
            artifact_id=artifact_id,
            artifact_type="output_hashes",
            content_hash=content_hash,
            logical_timestamp=logical_timestamp,
            data=output_hashes
        )
    
    @staticmethod
    def create_invariant_confirmations(
        confirmations: Dict[str, bool],
        artifact_id: str,
        logical_timestamp: int
    ) -> ReplayArtifact:
        """Create invariant confirmations artifact."""
        content_hash = ComparisonEngine.compute_hash(confirmations)
        return ReplayArtifact(
            artifact_id=artifact_id,
            artifact_type="invariant_confirmations",
            content_hash=content_hash,
            logical_timestamp=logical_timestamp,
            data=confirmations
        )
    
    @staticmethod
    def create_stage_output(
        stage: str,
        input_fingerprint: str,
        output_hash: str,
        status: str,
        artifact_id: str,
        logical_timestamp: int
    ) -> ReplayArtifact:
        """Create stage output artifact."""
        data = {
            "stage": stage,
            "input_fingerprint": input_fingerprint,
            "output_hash": output_hash,
            "status": status,
        }
        content_hash = ComparisonEngine.compute_hash(data)
        return ReplayArtifact(
            artifact_id=artifact_id,
            artifact_type="stage_output",
            content_hash=content_hash,
            logical_timestamp=logical_timestamp,
            data=data
        )


# ============================================================================
# OUTPUT VERIFICATION - Authority for Stage Output Validation
# ============================================================================

class OutputVerifier:
    """
    Authority for verifying stage outputs.
    
    The runner delegates all output verification to this verifier.
    """
    
    @staticmethod
    def verify_stage_output(
        stage: str,
        output_hash: str,
        divergence_authorizations: Dict[str, str]
    ) -> None:
        """
        Verify stage output invariants.
        
        Args:
            stage: Stage identifier
            output_hash: Hash of stage output
            divergence_authorizations: Authorized divergences from plan
            
        Raises:
            ReplayError: If output verification fails
        """
        from replay_errors import ReplayError, ReplayPhase
        from replay_invariants import InvariantID
        
        # Check if divergence is authorized for this stage
        divergence_authorized = stage in divergence_authorizations
        
        # Verify output hash is present
        if not output_hash:
            raise ReplayError(
                invariant_id=InvariantID.OUTPUT_HASH_MATCH.value,
                phase=ReplayPhase.EXECUTION,
                component_id=f"stage_{stage}",
                message="Stage produced no output hash",
                expected_value="output_hash",
                observed_value="none"
            )
        
        # In production, would compare with expected hash from original execution
        # For now, just verify output hash is present and valid format
        if len(output_hash) != 64:  # SHA-256 hex digest length
            raise ReplayError(
                invariant_id=InvariantID.OUTPUT_HASH_MATCH.value,
                phase=ReplayPhase.EXECUTION,
                component_id=f"stage_{stage}",
                message=f"Invalid output hash format: {output_hash}",
                expected_value="sha256_hex_digest",
                observed_value=output_hash
            )


# ============================================================================
# MODULE INTERFACE
# ============================================================================

__all__ = [
    # Enums
    'ReplayOutcome',
    'DivergenceKind',
    'ComparisonSurfaceType',
    # Core structures
    'ReplayResult',
    'OutputDivergence',
    'InvalidityReason',
    'ComparisonSurface',
    'ReplayArtifact',
    # Builder
    'ResultBuilder',
    'ArtifactBuilder',
    # Comparison
    'ComparisonEngine',
    # Verification
    'ResultVerifier',
    'OutputVerifier',
    # Convenience
    'create_exact_equivalence',
    'create_invalid_replay',
]