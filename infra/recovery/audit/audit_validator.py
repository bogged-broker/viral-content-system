"""
/infra/recovery/audit/audit_validator.py

Post-Hoc Integrity Verification & Forensic Proof Engine

WHAT THIS FILE ACTUALLY IS:
    Given only stored audit data, independently prove history is:
    - Real
    - Ordered
    - Complete
    - Untampered
    
    Assumes nothing about runtime state.
    If production is gone, RAM wiped, machines seized — this file speaks truth.

WHAT THIS FILE IS NOT:
    ❌ Not part of live execution
    ❌ Not a repair tool
    ❌ Not a logger
    ❌ Not a chain mutator
    ❌ Not a reconciliation engine
    ❌ Not a monitoring system
    
    This file never changes history. It only judges it.

DESIGN PRINCIPLE:
    > Validation must be possible without trust.
    
    Treats all inputs as hostile until proven otherwise.

FAILURE SEMANTICS:
    - Any critical finding → FAILED
    - Missing backend segments → INDETERMINATE
    - Hash mismatch → FAILED
    - Fork detected → FAILED
    - Replay divergence → FAILED
    
    Validator never hides bad news.

INVOCATION CONTEXT:
    - Recovery
    - Replay
    - Forensic audit
    - Governance
    - Legal discovery
    - Regulator inspection
    
    Never in hot paths.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, Iterator, Any, Tuple, List, Dict
from collections import defaultdict


# ============================================================================
# VALIDATOR VERSION (IMMUTABLE)
# ============================================================================

VALIDATOR_VERSION = "1.0.0"


# ============================================================================
# CORE ENUMS
# ============================================================================

class ValidationStatus(Enum):
    """
    Validation outcome.
    
    No "mostly ok". Uncertainty is explicit.
    """
    PASSED = "passed"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"
    
    def __str__(self) -> str:
        return self.value


class ValidationIssue(Enum):
    """
    Enumeration of all possible validation failures.
    
    Each issue must point to concrete evidence.
    """
    # Hash integrity
    HASH_MISMATCH = "hash_mismatch"
    HASH_ALGORITHM_MISMATCH = "hash_algorithm_mismatch"
    
    # Event presence
    MISSING_EVENT = "missing_event"
    DUPLICATE_EVENT = "duplicate_event"
    ORPHAN_EVENT = "orphan_event"
    
    # Chain structure
    FORK_DETECTED = "fork_detected"
    MISSING_GENESIS = "missing_genesis"
    BROKEN_PARENT_LINK = "broken_parent_link"
    
    # Temporal integrity
    TIMESTAMP_REGRESSION = "timestamp_regression"
    TIMESTAMP_OUT_OF_BOUNDS = "timestamp_out_of_bounds"
    
    # Sequence integrity
    HEIGHT_GAP = "height_gap"
    HEIGHT_DUPLICATE = "height_duplicate"
    HEIGHT_REGRESSION = "height_regression"
    NON_ZERO_GENESIS_HEIGHT = "non_zero_genesis_height"
    
    # Replay compatibility
    REPLAY_DIVERGENCE = "replay_divergence"
    MISSING_STATE_REFERENCE = "missing_state_reference"
    DANGLING_DEPENDENCY = "dangling_dependency"
    
    # Ordering
    ORDERING_VIOLATION = "ordering_violation"
    DETERMINISM_VIOLATION = "determinism_violation"
    
    # Backend integrity
    BACKEND_CORRUPTION = "backend_corruption"
    BACKEND_INACCESSIBLE = "backend_inaccessible"
    PARTIAL_WRITE_DETECTED = "partial_write_detected"
    
    def __str__(self) -> str:
        return self.value


class Severity(Enum):
    """Issue severity levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
    
    def __str__(self) -> str:
        return self.value
    
    def __lt__(self, other: Severity) -> bool:
        order = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
        return order.index(self) < order.index(other)


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class ValidationFinding:
    """
    Immutable evidence-based validation finding.
    
    Findings are immutable and cite concrete evidence.
    Court-safe representation of validation failures.
    """
    issue: ValidationIssue
    severity: Severity
    event_hash: str | None
    details: str
    height: int | None = None
    timestamp: int | None = None
    expected_value: str | None = None
    actual_value: str | None = None
    
    def __post_init__(self) -> None:
        """Enforce evidence requirements."""
        if not self.details:
            raise ValueError("Finding must include details")
        if self.severity == Severity.CRITICAL and not self.event_hash and self.height is None:
            # Critical findings must be locatable
            pass  # Some critical findings may be structural
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to serializable dictionary."""
        return {
            "issue": str(self.issue),
            "severity": str(self.severity),
            "event_hash": self.event_hash,
            "details": self.details,
            "height": self.height,
            "timestamp": self.timestamp,
            "expected_value": self.expected_value,
            "actual_value": self.actual_value,
        }
    
    def reproduction_steps(self) -> str:
        """Generate deterministic reproduction instructions."""
        steps = [f"Issue: {self.issue}"]
        if self.height is not None:
            steps.append(f"Locate event at height: {self.height}")
        if self.event_hash:
            steps.append(f"Verify event hash: {self.event_hash}")
        if self.expected_value and self.actual_value:
            steps.append(f"Expected: {self.expected_value}")
            steps.append(f"Actual: {self.actual_value}")
        steps.append(f"Details: {self.details}")
        return " | ".join(steps)


@dataclass(frozen=True)
class ValidationReport:
    """
    Immutable validation report.
    
    Court-safe, reproducible validation outcome.
    """
    status: ValidationStatus
    findings: tuple[ValidationFinding, ...]
    total_events: int
    validated_events: int
    started_at: int
    completed_at: int
    validator_version: str
    hash_algorithm: str
    backend_type: str
    
    # Statistics
    critical_findings: int = field(init=False)
    high_findings: int = field(init=False)
    medium_findings: int = field(init=False)
    low_findings: int = field(init=False)
    
    def __post_init__(self) -> None:
        """Compute statistics."""
        critical = sum(1 for f in self.findings if f.severity == Severity.CRITICAL)
        high = sum(1 for f in self.findings if f.severity == Severity.HIGH)
        medium = sum(1 for f in self.findings if f.severity == Severity.MEDIUM)
        low = sum(1 for f in self.findings if f.severity == Severity.LOW)
        
        object.__setattr__(self, "critical_findings", critical)
        object.__setattr__(self, "high_findings", high)
        object.__setattr__(self, "medium_findings", medium)
        object.__setattr__(self, "low_findings", low)
    
    @property
    def duration_ms(self) -> int:
        """Validation duration in milliseconds."""
        return self.completed_at - self.started_at
    
    @property
    def is_valid(self) -> bool:
        """Whether chain passed validation."""
        return self.status == ValidationStatus.PASSED
    
    @property
    def has_critical_issues(self) -> bool:
        """Whether any critical issues were found."""
        return self.critical_findings > 0
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to serializable dictionary."""
        return {
            "status": str(self.status),
            "findings": [f.to_dict() for f in self.findings],
            "total_events": self.total_events,
            "validated_events": self.validated_events,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "validator_version": self.validator_version,
            "hash_algorithm": self.hash_algorithm,
            "backend_type": self.backend_type,
            "summary": {
                "critical": self.critical_findings,
                "high": self.high_findings,
                "medium": self.medium_findings,
                "low": self.low_findings,
            }
        }
    
    def generate_executive_summary(self) -> str:
        """Generate human-readable executive summary."""
        lines = [
            "=" * 80,
            "FORENSIC AUDIT VALIDATION REPORT",
            "=" * 80,
            f"Status: {self.status.value.upper()}",
            f"Validator Version: {self.validator_version}",
            f"Hash Algorithm: {self.hash_algorithm}",
            f"Backend: {self.backend_type}",
            f"Total Events: {self.total_events}",
            f"Validated Events: {self.validated_events}",
            f"Duration: {self.duration_ms}ms",
            "",
            "FINDINGS SUMMARY:",
            f"  Critical: {self.critical_findings}",
            f"  High:     {self.high_findings}",
            f"  Medium:   {self.medium_findings}",
            f"  Low:      {self.low_findings}",
        ]
        
        if self.findings:
            lines.extend(["", "DETAILED FINDINGS:", "-" * 80])
            for i, finding in enumerate(self.findings, 1):
                lines.append(f"{i}. [{finding.severity.value.upper()}] {finding.issue.value}")
                lines.append(f"   {finding.details}")
                if finding.height is not None:
                    lines.append(f"   Height: {finding.height}")
                if finding.event_hash:
                    lines.append(f"   Event: {finding.event_hash[:16]}...")
                lines.append("")
        
        lines.append("=" * 80)
        return "\n".join(lines)


# ============================================================================
# AUDIT RECORD PROTOCOL
# ============================================================================

class AuditRecord(Protocol):
    """Protocol for audit record interface."""
    event_hash: str
    height: int
    parent_hash: str | None
    timestamp: int
    event_type: str
    state_id: str | None
    payload: dict[str, Any]


class AuditBackend(Protocol):
    """Protocol for read-only audit backend."""
    
    def load_all_events(self) -> Iterator[AuditRecord]:
        """Load all events from storage."""
        ...
    
    def get_event_by_hash(self, event_hash: str) -> AuditRecord | None:
        """Retrieve specific event by hash."""
        ...
    
    def get_backend_type(self) -> str:
        """Return backend implementation name."""
        ...


# ============================================================================
# AUDIT VALIDATOR (THE FORENSIC ENGINE)
# ============================================================================

class AuditValidator:
    """
    Post-hoc integrity verification engine.
    
    RULES:
        - Backend must be read-only
        - Hash algorithm must match chain
        - No overrides
        - No mutation hooks
        - Zero side effects
    
    FAILURE SEMANTICS:
        Validator never hides bad news.
        Uncertainty is explicit.
    """
    
    def __init__(
        self,
        backend: AuditBackend,
        hash_algo: str = "sha256",
        strict_mode: bool = True
    ):
        """
        Initialize forensic validator.
        
        Args:
            backend: Read-only audit storage backend
            hash_algo: Hash algorithm name (must match chain)
            strict_mode: Whether to enforce strictest validation
        """
        self._backend = backend
        self._hash_algo = hash_algo.lower()
        self._strict_mode = strict_mode
        self._findings: list[ValidationFinding] = []
        
        # Validate hash algorithm
        if self._hash_algo not in hashlib.algorithms_available:
            raise ValueError(f"Unsupported hash algorithm: {hash_algo}")
    
    # ========================================================================
    # TOP-LEVEL VALIDATION ORCHESTRATION
    # ========================================================================
    
    def validate_chain(self) -> ValidationReport:
        """
        Top-level validation orchestration.
        
        MANDATED STEPS:
            1. Load all audit records
            2. Sort deterministically
            3. Validate hashes
            4. Validate linkage
            5. Validate ordering
            6. Validate completeness
            7. Validate replay compatibility
            8. Aggregate findings
            9. Return report
        
        No early exits unless backend is unreadable.
        """
        started_at = self._get_monotonic_time_ms()
        self._findings = []
        
        try:
            # Step 1 & 2: Load and sort events
            events = self._load_and_sort_events()
            total_events = len(events)
            
            if total_events == 0:
                return self._create_report(
                    status=ValidationStatus.INDETERMINATE,
                    total_events=0,
                    validated_events=0,
                    started_at=started_at
                )
            
            # Step 3: Validate hashes
            self._validate_hashes(events)
            
            # Step 4: Validate linkage
            self._validate_linkage(events)
            
            # Step 5: Validate ordering
            self._validate_ordering(events)
            
            # Step 6: Validate completeness
            self._validate_completeness(events)
            
            # Step 7: Validate replay compatibility
            self._validate_replay_compatibility(events)
            
            # Step 8 & 9: Aggregate and return
            status = self._determine_status()
            validated_events = self._count_validated_events(events)
            
            return self._create_report(
                status=status,
                total_events=total_events,
                validated_events=validated_events,
                started_at=started_at
            )
            
        except Exception as e:
            # Backend failure or catastrophic error
            self._add_finding(
                issue=ValidationIssue.BACKEND_INACCESSIBLE,
                severity=Severity.CRITICAL,
                details=f"Backend failure during validation: {str(e)}",
                event_hash=None
            )
            return self._create_report(
                status=ValidationStatus.INDETERMINATE,
                total_events=0,
                validated_events=0,
                started_at=started_at
            )
    
    # ========================================================================
    # HASH VALIDATION
    # ========================================================================
    
    def _validate_hashes(self, events: list[AuditRecord]) -> None:
        """
        Validate cryptographic integrity of all events.
        
        MUST:
            - Recompute hash for every record
            - Compare to stored hash
            - Record mismatches explicitly
            - Never "fix" hashes
        """
        for event in events:
            computed_hash = self._compute_event_hash(event)
            
            if computed_hash != event.event_hash:
                self._add_finding(
                    issue=ValidationIssue.HASH_MISMATCH,
                    severity=Severity.CRITICAL,
                    details=(
                        f"Computed hash does not match stored hash. "
                        f"Event may be corrupted or tampered."
                    ),
                    event_hash=event.event_hash,
                    height=event.height,
                    expected_value=event.event_hash,
                    actual_value=computed_hash
                )
    
    def _compute_event_hash(self, event: AuditRecord) -> str:
        """
        Recompute event hash using canonical algorithm.
        
        Must match the hash computation in audit_chain.py.
        """
        hasher = hashlib.new(self._hash_algo)
        
        # Canonical serialization (MUST match chain implementation)
        hasher.update(str(event.height).encode())
        hasher.update(b"|")
        hasher.update((event.parent_hash or "").encode())
        hasher.update(b"|")
        hasher.update(str(event.timestamp).encode())
        hasher.update(b"|")
        hasher.update(event.event_type.encode())
        hasher.update(b"|")
        hasher.update((event.state_id or "").encode())
        hasher.update(b"|")
        hasher.update(str(sorted(event.payload.items())).encode())
        
        return hasher.hexdigest()
    
    # ========================================================================
    # LINKAGE VALIDATION
    # ========================================================================
    
    def _validate_linkage(self, events: list[AuditRecord]) -> None:
        """
        Validate parent-child chain linkage.
        
        ENFORCES:
            - Genesis has no parent
            - All non-genesis events reference valid parent
            - No orphans
            - No forks
        """
        if not events:
            return
        
        # Build hash lookup
        hash_to_event = {e.event_hash: e for e in events}
        
        for event in events:
            # Genesis validation
            if event.height == 0:
                if event.parent_hash is not None:
                    self._add_finding(
                        issue=ValidationIssue.BROKEN_PARENT_LINK,
                        severity=Severity.CRITICAL,
                        details="Genesis event must have null parent_hash",
                        event_hash=event.event_hash,
                        height=event.height,
                        expected_value="null",
                        actual_value=event.parent_hash
                    )
            else:
                # Non-genesis must have parent
                if event.parent_hash is None:
                    self._add_finding(
                        issue=ValidationIssue.ORPHAN_EVENT,
                        severity=Severity.CRITICAL,
                        details=f"Non-genesis event at height {event.height} has no parent",
                        event_hash=event.event_hash,
                        height=event.height
                    )
                elif event.parent_hash not in hash_to_event:
                    self._add_finding(
                        issue=ValidationIssue.BROKEN_PARENT_LINK,
                        severity=Severity.CRITICAL,
                        details=f"Parent hash {event.parent_hash[:16]}... not found in chain",
                        event_hash=event.event_hash,
                        height=event.height,
                        expected_value="valid_parent_hash",
                        actual_value=event.parent_hash
                    )
                else:
                    # Validate parent height
                    parent = hash_to_event[event.parent_hash]
                    expected_parent_height = event.height - 1
                    
                    if parent.height != expected_parent_height:
                        self._add_finding(
                            issue=ValidationIssue.BROKEN_PARENT_LINK,
                            severity=Severity.CRITICAL,
                            details=(
                                f"Parent height mismatch. Event at {event.height} "
                                f"references parent at {parent.height}, expected {expected_parent_height}"
                            ),
                            event_hash=event.event_hash,
                            height=event.height,
                            expected_value=str(expected_parent_height),
                            actual_value=str(parent.height)
                        )
        
        # Detect forks (multiple children for same parent)
        self._detect_forks(events)
    
    def _detect_forks(self, events: list[AuditRecord]) -> None:
        """Detect chain forks."""
        parent_to_children: dict[str, list[AuditRecord]] = defaultdict(list)
        
        for event in events:
            if event.parent_hash:
                parent_to_children[event.parent_hash].append(event)
        
        for parent_hash, children in parent_to_children.items():
            if len(children) > 1:
                child_hashes = [c.event_hash[:16] for c in children]
                self._add_finding(
                    issue=ValidationIssue.FORK_DETECTED,
                    severity=Severity.CRITICAL,
                    details=(
                        f"Fork detected: parent {parent_hash[:16]}... has "
                        f"{len(children)} children: {child_hashes}"
                    ),
                    event_hash=parent_hash
                )
    
    # ========================================================================
    # ORDERING VALIDATION
    # ========================================================================
    
    def _validate_ordering(self, events: list[AuditRecord]) -> None:
        """
        Validate temporal and sequential ordering.
        
        ENFORCES:
            - Strictly increasing height
            - Parent-child adjacency
            - Deterministic ordering for same timestamp
            - No reordering tolerance
        """
        for i, event in enumerate(events):
            # Height must match position
            if event.height != i:
                self._add_finding(
                    issue=ValidationIssue.ORDERING_VIOLATION,
                    severity=Severity.CRITICAL,
                    details=f"Event height {event.height} does not match position {i}",
                    event_hash=event.event_hash,
                    height=event.height,
                    expected_value=str(i),
                    actual_value=str(event.height)
                )
            
            # Timestamp validation
            if i > 0:
                prev_event = events[i - 1]
                
                # Timestamp must not regress
                if event.timestamp < prev_event.timestamp:
                    self._add_finding(
                        issue=ValidationIssue.TIMESTAMP_REGRESSION,
                        severity=Severity.HIGH,
                        details=(
                            f"Timestamp regression detected: "
                            f"{prev_event.timestamp} -> {event.timestamp}"
                        ),
                        event_hash=event.event_hash,
                        height=event.height,
                        expected_value=f">= {prev_event.timestamp}",
                        actual_value=str(event.timestamp)
                    )
                
                # For same timestamp, enforce deterministic ordering
                if event.timestamp == prev_event.timestamp:
                    if event.event_hash < prev_event.event_hash:
                        self._add_finding(
                            issue=ValidationIssue.DETERMINISM_VIOLATION,
                            severity=Severity.MEDIUM,
                            details=(
                                "Events with same timestamp not in deterministic order. "
                                "Must be sorted by event_hash as tiebreaker."
                            ),
                            event_hash=event.event_hash,
                            height=event.height
                        )
    
    # ========================================================================
    # COMPLETENESS VALIDATION
    # ========================================================================
    
    def _validate_completeness(self, events: list[AuditRecord]) -> None:
        """
        Validate chain completeness.
        
        DETECTS:
            - Missing heights
            - Duplicate heights
            - Skipped genesis
            - Truncated tail
            - Partial writes
        """
        if not events:
            return
        
        # Check genesis
        if events[0].height != 0:
            self._add_finding(
                issue=ValidationIssue.MISSING_GENESIS,
                severity=Severity.CRITICAL,
                details=f"Chain does not start at height 0 (starts at {events[0].height})",
                event_hash=events[0].event_hash,
                height=events[0].height
            )
        
        # Check for gaps and duplicates
        heights = [e.height for e in events]
        height_counts: dict[int, int] = defaultdict(int)
        
        for height in heights:
            height_counts[height] += 1
        
        # Detect duplicates
        for height, count in height_counts.items():
            if count > 1:
                self._add_finding(
                    issue=ValidationIssue.HEIGHT_DUPLICATE,
                    severity=Severity.CRITICAL,
                    details=f"Height {height} appears {count} times in chain",
                    event_hash=None,
                    height=height
                )
        
        # Detect gaps
        expected_heights = set(range(len(events)))
        actual_heights = set(heights)
        missing_heights = expected_heights - actual_heights
        
        for missing_height in sorted(missing_heights):
            self._add_finding(
                issue=ValidationIssue.HEIGHT_GAP,
                severity=Severity.CRITICAL,
                details=f"Missing event at height {missing_height}",
                event_hash=None,
                height=missing_height
            )
    
    # ========================================================================
    # REPLAY COMPATIBILITY VALIDATION
    # ========================================================================
    
    def _validate_replay_compatibility(self, events: list[AuditRecord]) -> None:
        """
        Validate replay compatibility.
        
        ENSURES:
            - All referenced state IDs exist
            - No dangling dependencies
            - Replay context can be reconstructed
            - No ambiguous inputs
        
        Bridges audit → replay.
        """
        seen_state_ids: set[str] = set()
        
        for event in events:
            # Track state IDs
            if event.state_id:
                if event.state_id in seen_state_ids:
                    # State ID reuse (potential issue)
                    if self._strict_mode:
                        self._add_finding(
                            issue=ValidationIssue.REPLAY_DIVERGENCE,
                            severity=Severity.MEDIUM,
                            details=f"State ID {event.state_id} used multiple times",
                            event_hash=event.event_hash,
                            height=event.height
                        )
                else:
                    seen_state_ids.add(event.state_id)
            
            # Check for dangling references in payload
            self._check_payload_references(event, seen_state_ids)
    
    def _check_payload_references(
        self,
        event: AuditRecord,
        seen_state_ids: set[str]
    ) -> None:
        """Check payload for dangling state references."""
        payload = event.payload
        
        # Look for common reference patterns
        reference_keys = ["state_id", "parent_state_id", "previous_state", "source_state"]
        
        for key in reference_keys:
            if key in payload and payload[key]:
                ref_state_id = payload[key]
                if isinstance(ref_state_id, str) and ref_state_id not in seen_state_ids:
                    self._add_finding(
                        issue=ValidationIssue.DANGLING_DEPENDENCY,
                        severity=Severity.HIGH,
                        details=(
                            f"Event references state_id '{ref_state_id}' "
                            f"via payload key '{key}' but state not found"
                        ),
                        event_hash=event.event_hash,
                        height=event.height
                    )
    
    # ========================================================================
    # HELPER METHODS
    # ========================================================================
    
    def _load_and_sort_events(self) -> list[AuditRecord]:
        """
        Load all events and sort deterministically.
        
        Sort order:
            1. Height (ascending)
            2. Timestamp (ascending)
            3. Event hash (ascending, for determinism)
        """
        try:
            events = list(self._backend.load_all_events())
        except Exception as e:
            self._add_finding(
                issue=ValidationIssue.BACKEND_CORRUPTION,
                severity=Severity.CRITICAL,
                details=f"Failed to load events from backend: {str(e)}",
                event_hash=None
            )
            return []
        
        # Deterministic sort
        events.sort(key=lambda e: (e.height, e.timestamp, e.event_hash))
        
        return events
    
    def _add_finding(
        self,
        issue: ValidationIssue,
        severity: Severity,
        details: str,
        event_hash: str | None,
        height: int | None = None,
        timestamp: int | None = None,
        expected_value: str | None = None,
        actual_value: str | None = None
    ) -> None:
        """Add validation finding with evidence."""
        finding = ValidationFinding(
            issue=issue,
            severity=severity,
            event_hash=event_hash,
            details=details,
            height=height,
            timestamp=timestamp,
            expected_value=expected_value,
            actual_value=actual_value
        )
        self._findings.append(finding)
    
    def _determine_status(self) -> ValidationStatus:
        """
        Determine validation status from findings.
        
        FAILURE SEMANTICS:
            - Any critical finding → FAILED
            - Backend issues → INDETERMINATE
            - No issues → PASSED
        """
        if not self._findings:
            return ValidationStatus.PASSED
        
        has_critical = any(f.severity == Severity.CRITICAL for f in self._findings)
        has_backend_issue = any(
            f.issue in {
                ValidationIssue.BACKEND_CORRUPTION,
                ValidationIssue.BACKEND_INACCESSIBLE
            }
            for f in self._findings
        )
        
        if has_backend_issue:
            return ValidationStatus.INDETERMINATE
        if has_critical:
            return ValidationStatus.FAILED
        
        # Has non-critical issues
        return ValidationStatus.FAILED if self._strict_mode else ValidationStatus.PASSED
    
    def _count_validated_events(self, events: list[AuditRecord]) -> int:
        """Count events that passed validation."""
        failed_hashes = {
            f.event_hash for f in self._findings
            if f.severity == Severity.CRITICAL and f.event_hash
        }
        return sum(1 for e in events if e.event_hash not in failed_hashes)
    
    def _create_report(
        self,
        status: ValidationStatus,
        total_events: int,
        validated_events: int,
        started_at: int
    ) -> ValidationReport:
        """Create immutable validation report."""
        completed_at = self._get_monotonic_time_ms()
        
        return ValidationReport(
            status=status,
            findings=tuple(self._findings),
            total_events=total_events,
            validated_events=validated_events,
            started_at=started_at,
            completed_at=completed_at,
            validator_version=VALIDATOR_VERSION,
            hash_algorithm=self._hash_algo,
            backend_type=self._backend.get_backend_type()
        )
    
    def _get_monotonic_time_ms(self) -> int:
        """Get monotonic time in milliseconds."""
        return int(time.monotonic() * 1000)


# ============================================================================
# VALIDATION INVARIANTS (ABSOLUTE)
# ============================================================================

class AuditValidationInvariants:
    """
    Invariant enforcement for validation system.
    
    MUST ENFORCE:
        - Validation does not mutate state
        - Findings are immutable
        - Reports are reproducible
        - Identical inputs → identical reports
        - No reliance on live clocks
        - No hidden heuristics
    
    Violation = validator compromised.
    """
    
    @staticmethod
    def verify_immutability(report: ValidationReport) -> bool:
        """Verify report immutability."""
        try:
            # Attempt to modify (should fail)
            report.status = ValidationStatus.FAILED  # type: ignore
            return False  # Should not reach here
        except (AttributeError, Exception):
            return True  # Correctly immutable
    
    @staticmethod
    def verify_reproducibility(
        validator1: AuditValidator,
        validator2: AuditValidator,
        backend: AuditBackend
    ) -> bool:
        """
        Verify identical inputs produce identical reports.
        
        Critical for forensic validity.
        """
        report1 = validator1.validate_chain()
        report2 = validator2.validate_chain()
        
        # Compare critical fields
        return (
            report1.status == report2.status and
            len(report1.findings) == len(report2.findings) and
            report1.total_events == report2.total_events and
            report1.validated_events == report2.validated_events
        )
    
    @staticmethod
    def verify_no_mutations(
        backend: AuditBackend,
        validator: AuditValidator
    ) -> bool:
        """
        Verify validation does not mutate backend.
        
        Captures state before/after validation.
        """
        # Snapshot before
        events_before = list(backend.load_all_events())
        hashes_before = {e.event_hash for e in events_before}
        
        # Run validation
        validator.validate_chain()
        
        # Snapshot after
        events_after = list(backend.load_all_events())
        hashes_after = {e.event_hash for e in events_after}
        
        return hashes_before == hashes_after
    
    @staticmethod
    def verify_evidence_completeness(report: ValidationReport) -> bool:
        """Verify all findings have sufficient evidence."""
        for finding in report.findings:
            if finding.severity == Severity.CRITICAL:
                # Critical findings must be locatable
                if finding.event_hash is None and finding.height is None:
                    # Some structural findings may not have specific events
                    if finding.issue not in {
                        ValidationIssue.MISSING_GENESIS,
                        ValidationIssue.BACKEND_CORRUPTION,
                        ValidationIssue.BACKEND_INACCESSIBLE
                    }:
                        return False
            
            # All findings must have details
            if not finding.details:
                return False
        
        return True


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def validate_audit_chain(
    backend: AuditBackend,
    hash_algo: str = "sha256",
    strict_mode: bool = True
) -> ValidationReport:
    """
    Convenience function for chain validation.
    
    Args:
        backend: Audit storage backend
        hash_algo: Hash algorithm to use
        strict_mode: Whether to enforce strict validation
    
    Returns:
        Immutable validation report
    """
    validator = AuditValidator(backend, hash_algo, strict_mode)
    return validator.validate_chain()


def validate_and_print_report(
    backend: AuditBackend,
    hash_algo: str = "sha256",
    strict_mode: bool = True
) -> ValidationReport:
    """
    Validate chain and print human-readable report.
    
    Args:
        backend: Audit storage backend
        hash_algo: Hash algorithm to use
        strict_mode: Whether to enforce strict validation
    
    Returns:
        Validation report (also printed to stdout)
    """
    report = validate_audit_chain(backend, hash_algo, strict_mode)
    print(report.generate_executive_summary())
    return report


# ============================================================================
# MODULE METADATA
# ============================================================================

__all__ = [
    # Enums
    "ValidationStatus",
    "ValidationIssue",
    "Severity",
    
    # Data structures
    "ValidationFinding",
    "ValidationReport",
    
    # Validator
    "AuditValidator",
    
    # Invariants
    "AuditValidationInvariants",
    
    # Convenience
    "validate_audit_chain",
    "validate_and_print_report",
    
    # Protocols
    "AuditRecord",
    "AuditBackend",
]