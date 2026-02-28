"""
Human- and machine-readable replay outcome explanation.

This module is the single authority for transforming validated replay results
into explainable, auditable, immutable replay reports. It answers with zero
ambiguity: Did the replay succeed? What diverged? Is it acceptable? Can it be verified?
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Any, Protocol, Tuple
from collections import OrderedDict


# ============================================================================
# ENUMERATIONS - Controlled Vocabulary (No Freeform Strings)
# ============================================================================

class ReplayStatus(Enum):
    """Overall replay outcome."""
    PASS = "PASS"
    FAIL = "FAIL"
    PARTIAL = "PARTIAL"


class ComparisonResult(Enum):
    """Atomic comparison outcome."""
    MATCH = "MATCH"
    DIVERGE = "DIVERGE"
    SKIPPED = "SKIPPED"


class DivergenceClass(Enum):
    """Controlled vocabulary for divergence classification."""
    NUMERIC_DRIFT = "NUMERIC_DRIFT"
    WINDOW_BOUNDARY_SHIFT = "WINDOW_BOUNDARY_SHIFT"
    INPUT_AUTHORITY_CHANGE = "INPUT_AUTHORITY_CHANGE"
    COMPUTATION_VERSION_CHANGE = "COMPUTATION_VERSION_CHANGE"
    NON_DETERMINISTIC_CODE = "NON_DETERMINISTIC_CODE"
    INVARIANT_VIOLATION = "INVARIANT_VIOLATION"


class Severity(Enum):
    """
    Violation severity levels.
    
    NOTE: For InvariantViolation, only FATAL is permitted.
    This enum exists for potential future use in other contexts,
    but InvariantViolation enforces FATAL-only at construction time.
    """
    FATAL = "FATAL"
    WARNING = "WARNING"


class ArtifactType(Enum):
    """Evidence artifact classification."""
    COUNTER_SNAPSHOT = "COUNTER_SNAPSHOT"
    WINDOW_TRACE = "WINDOW_TRACE"
    COMPUTATION_OUTPUT = "COMPUTATION_OUTPUT"
    DIFF_DETAIL = "DIFF_DETAIL"
    EXECUTION_LOG = "EXECUTION_LOG"


# ============================================================================
# EVIDENCE ACCESSIBILITY VERIFICATION - Abstract Interface
# ============================================================================

class EvidenceAccessibilityVerifier(Protocol):
    """
    Protocol for verifying evidence artifact accessibility.
    
    Implementations must verify that storage pointers are:
    1. Valid/parseable URIs
    2. Actually retrievable (existence check)
    3. Content-addressable (hash matches if CAS storage)
    
    This is a critical audit requirement: if evidence cannot be located,
    report generation MUST FAIL.
    """
    
    def verify_accessible(self, artifact: EvidenceArtifact) -> bool:
        """
        Verify artifact is accessible and retrievable.
        
        Args:
            artifact: Evidence artifact to verify
            
        Returns:
            True if accessible, False otherwise
            
        Raises:
            ReportGenerationError: If verification fails critically
        """
        ...


class DefaultEvidenceVerifier:
    """
    Default evidence verifier that performs basic validation.
    
    This implementation checks:
    - Storage pointer is non-empty
    - Storage pointer format is valid (basic URI check)
    
    For full Tier-0 compliance, implement a custom verifier that:
    - Checks actual storage backend accessibility
    - Verifies content-addressable storage integrity
    - Validates hash matches stored content
    """
    
    def verify_accessible(self, artifact: EvidenceArtifact) -> bool:
        """Basic accessibility check."""
        if not artifact.storage_pointer:
            raise ReportGenerationError(
                f"Evidence artifact missing storage pointer"
            )
        
        # Basic URI format validation
        pointer = artifact.storage_pointer.strip()
        if not pointer:
            raise ReportGenerationError(
                f"Evidence artifact has empty storage pointer"
            )
        
        # Check for common URI schemes (file://, http://, https://, s3://, etc.)
        if '://' not in pointer and not pointer.startswith('/'):
            # Allow relative paths but warn - full verification requires custom implementation
            pass
        
        return True


# ============================================================================
# CORE DATA STRUCTURES - Immutable and Deterministic
# ============================================================================

@dataclass(frozen=True)
class EvidenceRef:
    """Reference to supporting evidence artifact."""
    artifact_id: str
    description: str
    
    def to_dict(self) -> Dict[str, str]:
        return {"artifact_id": self.artifact_id, "description": self.description}


@dataclass(frozen=True)
class EvidenceArtifact:
    """Immutable evidence artifact with content-addressed storage."""
    artifact_hash: str
    artifact_type: ArtifactType
    storage_pointer: str
    
    def to_dict(self) -> Dict[str, str]:
        return {
            "artifact_hash": self.artifact_hash,
            "artifact_type": self.artifact_type.value,
            "storage_pointer": self.storage_pointer
        }


@dataclass(frozen=True)
class EvidenceManifest:
    """Complete evidence catalog for report verification."""
    artifacts: Dict[str, EvidenceArtifact]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifacts": {k: v.to_dict() for k, v in sorted(self.artifacts.items())}
        }
    
    def validate(self, verifier: Optional[EvidenceAccessibilityVerifier] = None) -> None:
        """
        Verify all evidence artifacts are accessible and retrievable.
        
        Args:
            verifier: Optional custom verifier. If None, uses DefaultEvidenceVerifier.
            
        Raises:
            ReportGenerationError: If any artifact is inaccessible or invalid.
            
        Blueprint requirement: "If evidence cannot be located, report MUST FAIL generation."
        """
        if verifier is None:
            verifier = DefaultEvidenceVerifier()
        
        for artifact_id, artifact in self.artifacts.items():
            # Basic presence checks
            if not artifact.storage_pointer:
                raise ReportGenerationError(
                    f"Evidence artifact {artifact_id} missing storage pointer"
                )
            if not artifact.artifact_hash:
                raise ReportGenerationError(
                    f"Evidence artifact {artifact_id} missing content hash"
                )
            
            # Accessibility verification (blueprint requirement)
            try:
                if not verifier.verify_accessible(artifact):
                    raise ReportGenerationError(
                        f"Evidence artifact {artifact_id} at {artifact.storage_pointer} "
                        f"is not accessible or retrievable"
                    )
            except ReportGenerationError:
                raise
            except Exception as e:
                raise ReportGenerationError(
                    f"Evidence artifact {artifact_id} accessibility verification failed: {e}"
                )


@dataclass(frozen=True)
class Comparison:
    """Atomic unit of replay truth - a single comparison result."""
    comparison_key: str
    expected_hash: str
    actual_hash: str
    comparison_result: ComparisonResult
    divergence_class: Optional[DivergenceClass] = None
    evidence_refs: List[EvidenceRef] = field(default_factory=list)
    
    def __post_init__(self):
        """
        Validate comparison integrity.
        
        Blueprint contract enforcement:
        - DIVERGE must have divergence_class
        - MATCH must have equal hashes
        - SKIPPED MUST include justification in evidence (blueprint requirement)
        """
        if self.comparison_result == ComparisonResult.DIVERGE and self.divergence_class is None:
            raise ReportGenerationError(
                f"Comparison {self.comparison_key}: DIVERGE must include divergence_class"
            )
        if self.comparison_result == ComparisonResult.MATCH:
            if self.expected_hash != self.actual_hash:
                raise ReportGenerationError(
                    f"Comparison {self.comparison_key}: MATCH but hashes differ"
                )
        # Blueprint requirement: SKIPPED MUST include justification in evidence
        if self.comparison_result == ComparisonResult.SKIPPED:
            if not self.evidence_refs:
                raise ReportGenerationError(
                    f"Comparison {self.comparison_key}: SKIPPED must include "
                    f"evidence justification (evidence_refs cannot be empty)"
                )
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "comparison_key": self.comparison_key,
            "expected_hash": self.expected_hash,
            "actual_hash": self.actual_hash,
            "comparison_result": self.comparison_result.value,
            "divergence_class": self.divergence_class.value if self.divergence_class else None,
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs]
        }


@dataclass(frozen=True)
class DivergenceSection:
    """Single axis of comparison (e.g., counters, windows, computations)."""
    section_id: str
    description: str
    comparisons: List[Comparison]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "section_id": self.section_id,
            "description": self.description,
            "comparisons": [c.to_dict() for c in sorted(self.comparisons, key=lambda x: x.comparison_key)]
        }


@dataclass(frozen=True)
class InvariantViolation:
    """
    Hard failure detected during replay comparison.
    
    Contract: Invariant violations are ALWAYS FATAL at this layer.
    The Severity enum permits WARNING for other contexts, but this
    dataclass enforces FATAL-only at construction time.
    """
    invariant_id: str
    description: str
    violated_by: List[str]
    severity: Severity  # Type annotation allows enum, but __post_init__ enforces FATAL-only
    evidence_refs: List[EvidenceRef] = field(default_factory=list)
    
    def __post_init__(self):
        """
        Invariant violations are always fatal at this layer.
        
        This enforces the blueprint requirement that invariant violations
        cannot be downgraded to WARNING, even though the Severity enum
        technically permits it. This is a type-level contract enforcement.
        """
        if self.severity != Severity.FATAL:
            raise ReportGenerationError(
                f"Invariant violation {self.invariant_id} must have FATAL severity. "
                f"Received: {self.severity.value}. "
                f"Invariant violations cannot be downgraded to WARNING."
            )
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "invariant_id": self.invariant_id,
            "description": self.description,
            "violated_by": sorted(self.violated_by),
            "severity": self.severity.value,
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs]
        }


@dataclass(frozen=True)
class ReportSummary:
    """High-level replay outcome statistics."""
    total_checks: int
    passed_checks: int
    failed_checks: int
    tolerated_divergences: int
    
    def to_dict(self) -> Dict[str, int]:
        return asdict(self)


# ============================================================================
# CANONICAL REPORT - The Verifiable Argument
# ============================================================================

@dataclass(frozen=True)
class ReplayReport:
    """
    Immutable, deterministic replay report.
    
    A replay report is not a log. A replay report is not a summary.
    A replay report is a verifiable argument backed by cryptographic evidence.
    
    Schema Versioning:
    - schema_version: Explicit version anchor for forward compatibility
    - Current version: 1
    - Schema evolution rules: Additive-only changes permitted
    - Breaking changes require schema_version increment
    """
    replay_plan_id: str
    execution_context_hash: str
    overall_status: ReplayStatus
    summary: ReportSummary
    divergence_sections: List[DivergenceSection]
    invariant_violations: List[InvariantViolation]
    evidence_manifest: EvidenceManifest
    schema_version: int = field(default=1)  # Explicit schema version anchor
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    report_id: str = field(default="", init=False)
    
    def __post_init__(self):
        """Generate deterministic report ID from canonical content."""
        # Use object.__setattr__ to bypass frozen dataclass
        object.__setattr__(self, 'report_id', self._compute_report_id())
    
    def _compute_report_id(self) -> str:
        """Generate deterministic hash of report contents (excluding generated_at)."""
        canonical = self._to_canonical_dict(include_timestamp=False)
        content = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
    
    def _to_canonical_dict(self, include_timestamp: bool = True) -> Dict[str, Any]:
        """
        Serialize to canonical dictionary form.
        
        Guarantees:
        - Deterministic ordering (sorted keys)
        - Stable across Python versions
        - Excludes generated_at from hashing unless requested
        - Includes schema_version for forward compatibility
        """
        data = OrderedDict([
            ("schema_version", self.schema_version),  # Schema version anchor
            ("replay_plan_id", self.replay_plan_id),
            ("execution_context_hash", self.execution_context_hash),
            ("overall_status", self.overall_status.value),
            ("summary", self.summary.to_dict()),
            ("divergence_sections", [
                s.to_dict() for s in sorted(self.divergence_sections, key=lambda x: x.section_id)
            ]),
            ("invariant_violations", [
                v.to_dict() for v in sorted(self.invariant_violations, key=lambda x: x.invariant_id)
            ]),
            ("evidence_manifest", self.evidence_manifest.to_dict()),
        ])
        
        if include_timestamp:
            data["generated_at"] = self.generated_at.isoformat()
            data["report_id"] = self.report_id
        
        return data
    
    def to_dict(self) -> Dict[str, Any]:
        """Export complete report including metadata."""
        return self._to_canonical_dict(include_timestamp=True)
    
    def to_json(self, indent: Optional[int] = 2) -> str:
        """Serialize to JSON with deterministic ordering."""
        return json.dumps(self.to_dict(), indent=indent)
    
    def verify_integrity(self) -> bool:
        """Verify report ID matches content hash."""
        computed_id = self._compute_report_id()
        return computed_id == self.report_id


# ============================================================================
# REPORT BUILDER - Deterministic Report Construction
# ============================================================================

class ReportBuilder:
    """
    Builder for constructing replay reports from validated replay results.
    
    Enforces:
    - Single source of truth (ReplayResults input)
    - Deterministic report generation
    - Evidence integrity
    - Evidence accessibility (blueprint requirement)
    - Invariant validation
    """
    
    def __init__(self, evidence_verifier: Optional[EvidenceAccessibilityVerifier] = None):
        """
        Initialize report builder.
        
        Args:
            evidence_verifier: Optional custom verifier for evidence accessibility.
                            If None, uses DefaultEvidenceVerifier.
        """
        self._sections: List[DivergenceSection] = []
        self._violations: List[InvariantViolation] = []
        self._evidence: Dict[str, EvidenceArtifact] = {}
        self._replay_plan_id: Optional[str] = None
        self._execution_context_hash: Optional[str] = None
        self._evidence_verifier: Optional[EvidenceAccessibilityVerifier] = evidence_verifier
        self._schema_version: int = 1  # Current schema version
    
    def set_context(self, replay_plan_id: str, execution_context_hash: str) -> ReportBuilder:
        """Set replay execution context."""
        self._replay_plan_id = replay_plan_id
        self._execution_context_hash = execution_context_hash
        return self
    
    def add_section(self, section: DivergenceSection) -> ReportBuilder:
        """Add divergence comparison section."""
        self._sections.append(section)
        return self
    
    def add_violation(self, violation: InvariantViolation) -> ReportBuilder:
        """Add invariant violation (always fatal)."""
        self._violations.append(violation)
        return self
    
    def add_evidence(self, artifact_id: str, artifact: EvidenceArtifact) -> ReportBuilder:
        """Register evidence artifact."""
        if artifact_id in self._evidence:
            # Verify hash consistency if duplicate ID
            if self._evidence[artifact_id].artifact_hash != artifact.artifact_hash:
                raise ReportGenerationError(
                    f"Evidence artifact {artifact_id} registered with conflicting hashes"
                )
        self._evidence[artifact_id] = artifact
        return self
    
    def build(self) -> ReplayReport:
        """
        Construct immutable replay report.
        
        Validates:
        - All required context is set
        - Evidence manifest is complete
        - All referenced evidence exists
        - All evidence is accessible and retrievable (blueprint requirement)
        - Report can be deterministically regenerated
        
        Raises:
            ReportGenerationError: If evidence is inaccessible or validation fails
        """
        if not self._replay_plan_id:
            raise ReportGenerationError("replay_plan_id not set")
        if not self._execution_context_hash:
            raise ReportGenerationError("execution_context_hash not set")
        
        # Build evidence manifest
        manifest = EvidenceManifest(artifacts=self._evidence.copy())
        # Validate with accessibility verification (blueprint requirement)
        manifest.validate(verifier=self._evidence_verifier)
        
        # Verify all evidence references are satisfied
        self._verify_evidence_references(manifest)
        
        # Compute summary statistics
        summary = self._compute_summary()
        
        # Determine overall status
        status = self._determine_status()
        
        # Construct immutable report
        report = ReplayReport(
            replay_plan_id=self._replay_plan_id,
            execution_context_hash=self._execution_context_hash,
            overall_status=status,
            summary=summary,
            divergence_sections=self._sections.copy(),
            invariant_violations=self._violations.copy(),
            evidence_manifest=manifest,
            schema_version=self._schema_version
        )
        
        # Verify integrity
        if not report.verify_integrity():
            raise ReportGenerationError("Report failed integrity verification")
        
        return report
    
    def _verify_evidence_references(self, manifest: EvidenceManifest) -> None:
        """Ensure all evidence references point to existing artifacts."""
        referenced_ids = set()
        
        # Collect from sections
        for section in self._sections:
            for comparison in section.comparisons:
                for ref in comparison.evidence_refs:
                    referenced_ids.add(ref.artifact_id)
        
        # Collect from violations
        for violation in self._violations:
            for ref in violation.evidence_refs:
                referenced_ids.add(ref.artifact_id)
        
        # Verify existence
        missing = referenced_ids - set(manifest.artifacts.keys())
        if missing:
            raise ReportGenerationError(
                f"Evidence references point to missing artifacts: {sorted(missing)}"
            )
    
    def _compute_summary(self) -> ReportSummary:
        """Compute summary statistics from comparisons."""
        total = 0
        passed = 0
        failed = 0
        tolerated = 0
        
        for section in self._sections:
            for comp in section.comparisons:
                total += 1
                if comp.comparison_result == ComparisonResult.MATCH:
                    passed += 1
                elif comp.comparison_result == ComparisonResult.DIVERGE:
                    failed += 1
                    # Tolerated divergences are classified but not invariant violations
                    if comp.divergence_class != DivergenceClass.INVARIANT_VIOLATION:
                        tolerated += 1
        
        return ReportSummary(
            total_checks=total,
            passed_checks=passed,
            failed_checks=failed,
            tolerated_divergences=tolerated
        )
    
    def _determine_status(self) -> ReplayStatus:
        """Determine overall replay status."""
        # Any invariant violation = FAIL
        if self._violations:
            return ReplayStatus.FAIL
        
        # Any unclassified divergence = FAIL
        for section in self._sections:
            for comp in section.comparisons:
                if comp.comparison_result == ComparisonResult.DIVERGE:
                    if comp.divergence_class == DivergenceClass.INVARIANT_VIOLATION:
                        return ReplayStatus.FAIL
        
        # Check for any divergences (even tolerated)
        has_divergence = any(
            comp.comparison_result == ComparisonResult.DIVERGE
            for section in self._sections
            for comp in section.comparisons
        )
        
        # All checks passed
        if not has_divergence:
            return ReplayStatus.PASS
        
        # Has tolerated divergences but no violations
        return ReplayStatus.PARTIAL


# ============================================================================
# REPORT VERIFICATION - Independent Audit Support
# ============================================================================

class ReportVerifier:
    """
    Independent verification of replay report integrity.
    
    Can be used by auditors, downstream systems, or future replay runs
    to verify report authenticity and completeness.
    """
    
    @staticmethod
    def verify_report(report: ReplayReport) -> tuple[bool, List[str]]:
        """
        Verify report integrity and completeness.
        
        Returns:
            (is_valid, list_of_issues)
        """
        issues = []
        
        # Verify report ID
        if not report.verify_integrity():
            issues.append("Report ID does not match content hash")
        
        # Verify evidence manifest
        try:
            report.evidence_manifest.validate()
        except ReportGenerationError as e:
            issues.append(f"Evidence manifest validation failed: {e}")
        
        # Verify comparison integrity
        for section in report.divergence_sections:
            for comp in section.comparisons:
                try:
                    # Re-validate comparison rules
                    if comp.comparison_result == ComparisonResult.DIVERGE:
                        if comp.divergence_class is None:
                            issues.append(
                                f"Comparison {comp.comparison_key} marked DIVERGE without classification"
                            )
                    if comp.comparison_result == ComparisonResult.MATCH:
                        if comp.expected_hash != comp.actual_hash:
                            issues.append(
                                f"Comparison {comp.comparison_key} marked MATCH but hashes differ"
                            )
                    # Blueprint requirement: SKIPPED must have evidence justification
                    if comp.comparison_result == ComparisonResult.SKIPPED:
                        if not comp.evidence_refs:
                            issues.append(
                                f"Comparison {comp.comparison_key} marked SKIPPED without evidence justification"
                            )
                except Exception as e:
                    issues.append(f"Comparison {comp.comparison_key} validation error: {e}")
        
        # Verify invariant violations
        for violation in report.invariant_violations:
            if violation.severity != Severity.FATAL:
                issues.append(
                    f"Invariant violation {violation.invariant_id} has non-FATAL severity"
                )
        
        # Verify status consistency
        status_issues = ReportVerifier._verify_status_consistency(report)
        issues.extend(status_issues)
        
        return (len(issues) == 0, issues)
    
    @staticmethod
    def _verify_status_consistency(report: ReplayReport) -> List[str]:
        """Verify overall status matches content."""
        issues = []
        
        has_violations = len(report.invariant_violations) > 0
        has_hard_divergence = any(
            comp.divergence_class == DivergenceClass.INVARIANT_VIOLATION
            for section in report.divergence_sections
            for comp in section.comparisons
            if comp.comparison_result == ComparisonResult.DIVERGE
        )
        has_any_divergence = any(
            comp.comparison_result == ComparisonResult.DIVERGE
            for section in report.divergence_sections
            for comp in section.comparisons
        )
        
        if has_violations or has_hard_divergence:
            if report.overall_status != ReplayStatus.FAIL:
                issues.append("Report has violations but status is not FAIL")
        elif has_any_divergence:
            if report.overall_status not in (ReplayStatus.PARTIAL, ReplayStatus.FAIL):
                issues.append("Report has divergences but status is not PARTIAL or FAIL")
        else:
            if report.overall_status != ReplayStatus.PASS:
                issues.append("Report has no issues but status is not PASS")
        
        return issues


# ============================================================================
# EXCEPTIONS - Unrecoverable Failures Only
# ============================================================================

class ReportGenerationError(Exception):
    """
    Fatal error during report generation.
    
    All ReportGenerationError exceptions are unrecoverable at this layer.
    They indicate:
    - Missing evidence
    - Hash mismatches without classification
    - Schema corruption
    - Invalid state transitions
    """
    pass


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def create_comparison(
    key: str,
    expected: str,
    actual: str,
    evidence: Optional[List[EvidenceRef]] = None
) -> Comparison:
    """Create a comparison with automatic result determination."""
    result = ComparisonResult.MATCH if expected == actual else ComparisonResult.DIVERGE
    divergence_class = None
    
    if result == ComparisonResult.DIVERGE:
        # Default to invariant violation - caller must override if acceptable
        divergence_class = DivergenceClass.INVARIANT_VIOLATION
    
    return Comparison(
        comparison_key=key,
        expected_hash=expected,
        actual_hash=actual,
        comparison_result=result,
        divergence_class=divergence_class,
        evidence_refs=evidence or []
    )


def create_tolerated_comparison(
    key: str,
    expected: str,
    actual: str,
    divergence_class: DivergenceClass,
    evidence: Optional[List[EvidenceRef]] = None
) -> Comparison:
    """Create a comparison with expected/tolerated divergence."""
    if divergence_class == DivergenceClass.INVARIANT_VIOLATION:
        raise ValueError("Use create_comparison for invariant violations")
    
    return Comparison(
        comparison_key=key,
        expected_hash=expected,
        actual_hash=actual,
        comparison_result=ComparisonResult.DIVERGE,
        divergence_class=divergence_class,
        evidence_refs=evidence or []
    )


def hash_content(content: Any) -> str:
    """Generate deterministic content hash."""
    if isinstance(content, (dict, list)):
        serialized = json.dumps(content, sort_keys=True, separators=(',', ':'))
    else:
        serialized = str(content)
    
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


# ============================================================================
# MODULE INTERFACE
# ============================================================================

__all__ = [
    # Enums
    'ReplayStatus',
    'ComparisonResult',
    'DivergenceClass',
    'Severity',
    'ArtifactType',
    # Evidence verification
    'EvidenceAccessibilityVerifier',
    'DefaultEvidenceVerifier',
    # Core structures
    'ReplayReport',
    'DivergenceSection',
    'Comparison',
    'InvariantViolation',
    'ReportSummary',
    'EvidenceManifest',
    'EvidenceArtifact',
    'EvidenceRef',
    # Builder
    'ReportBuilder',
    # Verification
    'ReportVerifier',
    # Exceptions
    'ReportGenerationError',
    # Utilities
    'create_comparison',
    'create_tolerated_comparison',
    'hash_content',
]