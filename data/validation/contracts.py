"""
/data/validation/contracts.py

Deterministic Validation Contract Specification
(Pre-State Legitimacy Boundary)

---

1️⃣ Purpose

This file defines:

- What validation is.
- How validation results are represented.
- How violations are structured.
- What contextual inputs validation depends on.
- How determinism is guaranteed.
- How failure semantics work.
- How results are fingerprinted.

Every other file in /data/validation/ depends on this contract.

Nothing may return raw booleans. Nothing may throw business exceptions. Nothing may emit ad-hoc dictionaries.

If validation happens, it must produce a ValidationResult.

---

2️⃣ Core Principle

Validation must be:

- Deterministic
- Side-effect free
- Serializable
- Replay-safe
- Version-aware
- Context-aware
- Stable in output ordering

Given:

    input = x
    context = c

It must hold:

    Validate(x, c) == Validate(x, c)

Across:

- Machines
- Nodes
- Replays
- Time

There is no randomness permitted.

---

3️⃣ Formal Validation Function

We define:

    Validate : (Input, ValidationContext) → ValidationResult

Required Properties

Determinism:
    If:
        Validate(x, c) = r
        Validate(x, c) = r'
    Then:
        r == r'

Purity:
    - No mutation of input.
    - No external IO.
    - No registry writes.
    - No logging side-effects inside rule execution.

Completeness:
    All applicable rules must run unless fail-fast is enabled explicitly.

---

4️⃣ ValidationContext

Validation is never isolated.

It depends on declared structural context.

Why fingerprints?

Because validation depends on:

- Current registry definitions
- Compatibility guarantees
- Active invariants

Fingerprints make validation traceable and replayable.

Without them, replay cannot prove equivalence.

---

5️⃣ SeverityLevel

We standardize severity classification.

Enforcement Semantics:

- INFO → always non-blocking
- WARNING → blocking only in strict_mode
- ERROR → validation fails
- CRITICAL → immediate rejection (fail-fast always)

Severity behavior must not be contextually reinterpreted elsewhere.

---

6️⃣ ValidationViolation

Each rule evaluation must produce structured violations.

Deterministic Hash Requirement:

deterministic_hash must be:

    H(rule_id + message + field_path)

No timestamps.
No UUIDs.
No runtime entropy.

This enables:

- Cross-node equivalence detection
- Audit proof
- Replay comparison

---

7️⃣ ValidationResult

This is the only allowable output type.

Fingerprint Construction:

Fingerprint must be:

    H(
      sorted(
        violation.rule_id +
        violation.message +
        violation.field_path
      )
    )

Sorted by:

1. rule_id
2. field_path (None treated as "")

Stable ordering is mandatory.

---

8️⃣ Deterministic Ordering Rule

Before producing a ValidationResult:

Violations must be sorted:

    def sort_violations(violations):
        return sorted(
            violations,
            key=lambda v: (v.rule_id, v.field_path or "")
        )

Violation ordering must not depend on:

- Rule registration order
- Python dictionary iteration
- Runtime execution sequence
- Parallelism

Stable ordering ensures:

- Replay integrity.

---

9️⃣ Fail-Fast vs Aggregated Mode

Controlled explicitly by:

    ValidationContext.fail_fast

Behavior:

If fail_fast = True:
    - Stop on first ERROR or CRITICAL.
    - Always stop on CRITICAL.

If fail_fast = False:
    - Collect all violations.

Fail-fast behavior must not change violation formatting.

---

🔟 Rule Execution Contract

All rule files must obey:

    class ValidationRule:
        rule_id: str
        severity: SeverityLevel

        def evaluate(self, input, context) -> Optional[ValidationViolation]:
            ...

Rules must:

- Return None if passing.
- Return ValidationViolation if failing.
- Never throw business exceptions.
- Never mutate input.
- Never consult global state.
- Use only provided context.

---

1️⃣1️⃣ Scope Definition (Optional Extension Ready)

Rules may declare logical scope:

- artifact
- mutation_payload
- migration_plan
- registry
- compatibility_matrix
- snapshot
- governance_operation

Scope filtering must be done by orchestrator — not by rule side-effects.

---

1️⃣2️⃣ Validation Invariants

Validation itself must satisfy:

- No nondeterministic randomness
- No wall-clock calls
- No network calls
- No file reads
- No global writes
- No environment-dependent branching
- No implicit version upgrades

Validation must be replayable purely from:

    (input, context)

---

1️⃣3️⃣ Rejection Semantics

If passed == False:

The following MUST NOT occur:

- Lineage record creation
- Mutation proposal broadcast
- Governance lock mutation
- Snapshot sealing
- Append log write

Validation failure is pre-transition rejection.

---

1️⃣4️⃣ Integration Guarantees

This contract must be executed before:

- Artifact canonicalization
- Migration plan execution
- Registry update
- Compatibility update
- Distributed mutation proposal
- Governance mutation
- Snapshot sealing
- Rollback initiation

It is impossible to enter state transition domain without passing validation.

Formally:

If:

    T(S, μ)

is a state transition.

Then it becomes:

    T(S, μ) defined iff Validate(μ, context).passed

Validation constrains the domain of state transitions.

---

1️⃣5️⃣ Security Role

This layer prevents:

- Malformed schema injection
- Invalid version declaration
- Structural downgrade disguised as upgrade
- Invariant bypass attempts
- Migration without plan approval
- Cross-version contamination
- Governance misuse before structure compliance

It reduces attack surface before consensus.

---

1️⃣6️⃣ Testing Requirements

For production-grade integrity:

- Deterministic repeated validation test
- Strict vs permissive difference test
- Rule ordering stability test
- Fingerprint reproducibility test
- Large rule-set scaling test
- Nested conditional rule test
- Fail-fast equivalence test
- Cross-node comparison test

Any nondeterminism here invalidates replay safety.

---

1️⃣7️⃣ Absolute Definition

/data/validation/contracts.py is:

> The deterministic, side-effect-free pre-state enforcement contract that defines how
> structural and semantic legitimacy is measured before any mutation, artifact, or
> governance action may interact with the lineage system.

It is not a helper file. It is a constitutional document.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional, Tuple


__all__ = [
    "SeverityLevel",
    "ValidationContext",
    "ValidationViolation",
    "ValidationResult",
    "ValidationRule",
    "sort_violations",
    "compute_violation_hash",
    "compute_result_fingerprint",
]


# ============================================================================
# SeverityLevel
# ============================================================================

class SeverityLevel(str, Enum):
    """
    Standardized severity classification.
    
    Enforcement Semantics:
    - INFO → always non-blocking
    - WARNING → blocking only in strict_mode
    - ERROR → validation fails
    - CRITICAL → immediate rejection (fail-fast always)
    
    Severity behavior must not be contextually reinterpreted elsewhere.
    """
    
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"
    
    def is_blocking(self, strict_mode: bool = False) -> bool:
        """
        Determine if this severity level blocks validation.
        
        Args:
            strict_mode: If True, WARNING also blocks
            
        Returns:
            True if this severity should cause validation failure
        """
        if self == SeverityLevel.CRITICAL:
            return True
        if self == SeverityLevel.ERROR:
            return True
        if self == SeverityLevel.WARNING and strict_mode:
            return True
        return False


# ============================================================================
# ValidationContext
# ============================================================================

@dataclass(frozen=True)
class ValidationContext:
    """
    Validation context ensuring version & governance awareness.
    
    Validation is never isolated. It depends on declared structural context.
    
    Fingerprints make validation traceable and replayable.
    Without them, replay cannot prove equivalence.
    """
    
    artifact_type: Optional[str]  # Optional: some governance flows validate without declared artifact identity
    artifact_version: Optional[str]  # Optional: some governance flows validate without declared artifact identity
    
    registry_fingerprint: str
    compatibility_fingerprint: str
    invariants_fingerprint: str
    
    strict_mode: bool
    fail_fast: bool
    
    source: str  # ingestion | migration | consensus | recovery | governance
    
    def __post_init__(self) -> None:
        """Validate required fields."""
        if not self.registry_fingerprint:
            raise ValueError("registry_fingerprint is required")
        if not self.compatibility_fingerprint:
            raise ValueError("compatibility_fingerprint is required")
        if not self.invariants_fingerprint:
            raise ValueError("invariants_fingerprint is required")
        if not self.source:
            raise ValueError("source is required")
        if self.fail_fast not in (True, False):
            raise ValueError("fail_fast must be explicitly set (True or False)")


# ============================================================================
# ValidationViolation
# ============================================================================

@dataclass(frozen=True)
class ValidationViolation:
    """
    Individual validation violation with deterministic hash.
    
    Each rule evaluation must produce structured violations.
    
    Deterministic Hash Requirement:
    deterministic_hash must be:
        H(rule_id + message + field_path)
    
    No timestamps. No UUIDs. No runtime entropy.
    
    This enables:
    - Cross-node equivalence detection
    - Audit proof
    - Replay comparison
    """
    
    rule_id: str
    message: str
    severity: SeverityLevel
    field_path: Optional[str] = None
    deterministic_hash: str = field(default="", init=False)
    
    def __post_init__(self) -> None:
        """Compute deterministic hash if not provided."""
        if not self.deterministic_hash:
            # H(rule_id + message + field_path)
            # Normalize unicode for cross-node equivalence (NFC normalization)
            rule_id_norm = unicodedata.normalize('NFC', self.rule_id)
            message_norm = unicodedata.normalize('NFC', self.message)
            field_path_norm = unicodedata.normalize('NFC', self.field_path or "")
            
            parts = [
                rule_id_norm,
                message_norm,
                field_path_norm,
            ]
            canonical = json.dumps(
                parts,
                sort_keys=False,  # Order matters here
                separators=(",", ":"),
                ensure_ascii=True
            )
            hash_value = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            object.__setattr__(self, "deterministic_hash", hash_value)
    
    def __lt__(self, other: ValidationViolation) -> bool:
        """Comparison for deterministic sorting."""
        if not isinstance(other, ValidationViolation):
            return NotImplemented
        
        # Sort by rule_id, then field_path
        if self.rule_id != other.rule_id:
            return self.rule_id < other.rule_id
        return (self.field_path or "") < (other.field_path or "")
    
    def __hash__(self) -> int:
        """Hash based on deterministic fields."""
        return hash((self.rule_id, self.message, self.field_path, self.severity))
    
    def __eq__(self, other: object) -> bool:
        """Equality based on deterministic fields."""
        if not isinstance(other, ValidationViolation):
            return NotImplemented
        return (
            self.rule_id == other.rule_id
            and self.message == other.message
            and self.field_path == other.field_path
            and self.severity == other.severity
        )


# ============================================================================
# ValidationResult
# ============================================================================

@dataclass(frozen=True)
class ValidationResult:
    """
    Complete validation result with deterministic fingerprint.
    
    This is the only allowable output type.
    
    Fingerprint Construction:
    Fingerprint must be:
        H(
          sorted(
            violation.rule_id +
            violation.message +
            violation.field_path
          )
        )
    
    Sorted by:
    1. rule_id
    2. field_path (None treated as "")
    
    Stable ordering is mandatory.
    """
    
    passed: bool
    violations: tuple[ValidationViolation, ...]  # Immutable tuple for determinism
    validation_fingerprint: str = field(default="", init=False)
    
    def __post_init__(self) -> None:
        """Compute validation fingerprint from sorted violations."""
        if not self.validation_fingerprint:
            # Handle empty violations case
            if not self.violations:
                # Empty result fingerprint: H([])
                parts: List[str] = []
                canonical = json.dumps(
                    parts,
                    sort_keys=False,
                    separators=(",", ":"),
                    ensure_ascii=True
                )
                fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                object.__setattr__(self, "validation_fingerprint", fingerprint)
                return
            
            # Sort violations deterministically
            sorted_violations = sorted(self.violations)
            
            # Create canonical representation
            # H(sorted(violation.rule_id + violation.message + violation.field_path))
            # Spec-pure: fingerprint does NOT include passed flag
            violation_parts = []
            for v in sorted_violations:
                # Normalize unicode for cross-node equivalence
                rule_id_norm = unicodedata.normalize('NFC', v.rule_id)
                message_norm = unicodedata.normalize('NFC', v.message)
                field_path_norm = unicodedata.normalize('NFC', v.field_path or "")
                violation_parts.append(
                    f"{rule_id_norm}:{message_norm}:{field_path_norm}"
                )
            
            # Fingerprint is ONLY sorted violations (spec-pure)
            canonical = json.dumps(
                violation_parts,
                sort_keys=False,  # Already sorted
                separators=(",", ":"),
                ensure_ascii=True
            )
            fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            object.__setattr__(self, "validation_fingerprint", fingerprint)
    
    @classmethod
    def success(cls) -> ValidationResult:
        """Create a successful validation result."""
        return cls(passed=True, violations=())
    
    @classmethod
    def failure(cls, violations: List[ValidationViolation]) -> ValidationResult:
        """Create a failed validation result."""
        return cls(passed=False, violations=tuple(violations))
    
    def has_blocking_violations(self, strict_mode: bool = False) -> bool:
        """Check if result contains blocking violations."""
        return any(
            v.severity.is_blocking(strict_mode)
            for v in self.violations
        )


# ============================================================================
# ValidationRule (Base Contract)
# ============================================================================

class ValidationRule(ABC):
    """
    Base class for validation rules.
    
    All rule files must obey this contract.
    
    Rules must:
    - Return None if passing.
    - Return ValidationViolation if failing.
    - Never throw business exceptions.
    - Never mutate input.
    - Never consult global state.
    - Use only provided context.
    """
    
    @property
    @abstractmethod
    def rule_id(self) -> str:
        """Unique identifier for this rule."""
        pass
    
    @property
    @abstractmethod
    def severity(self) -> SeverityLevel:
        """Default severity level for violations from this rule."""
        pass
    
    @abstractmethod
    def evaluate(
        self,
        input_data: Any,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """
        Evaluate rule against input.
        
        Args:
            input_data: Data to validate
            context: Validation context
            
        Returns:
            ValidationViolation if rule fails, None if passes
            
        Rules must:
        - Return None if passing
        - Return ValidationViolation if failing
        - Never throw business exceptions
        - Never mutate input
        - Never consult global state
        - Use only provided context
        """
        pass


# ============================================================================
# Deterministic Ordering Function
# ============================================================================

def sort_violations(violations: List[ValidationViolation]) -> List[ValidationViolation]:
    """
    Sort violations deterministically.
    
    Before producing a ValidationResult:
    Violations must be sorted by:
    1. rule_id
    2. field_path (None treated as "")
    
    Violation ordering must not depend on:
    - Rule registration order
    - Python dictionary iteration
    - Runtime execution sequence
    - Parallelism
    
    Stable ordering ensures replay integrity.
    
    Args:
        violations: List of violations to sort
        
    Returns:
        Sorted list of violations
    """
    return sorted(
        violations,
        key=lambda v: (v.rule_id, v.field_path or "")
    )


# ============================================================================
# Hash Computation Functions
# ============================================================================

def compute_violation_hash(violation: ValidationViolation) -> str:
    """
    Compute deterministic hash of violation.
    
    H(rule_id + message + field_path)
    
    Unicode normalization (NFC) ensures cross-node equivalence.
    
    Args:
        violation: Validation violation
        
    Returns:
        SHA-256 hex digest
    """
    # Normalize unicode for cross-node equivalence
    rule_id_norm = unicodedata.normalize('NFC', violation.rule_id)
    message_norm = unicodedata.normalize('NFC', violation.message)
    field_path_norm = unicodedata.normalize('NFC', violation.field_path or "")
    
    parts = [
        rule_id_norm,
        message_norm,
        field_path_norm,
    ]
    canonical = json.dumps(
        parts,
        sort_keys=False,
        separators=(",", ":"),
        ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_result_fingerprint(result: ValidationResult) -> str:
    """
    Compute deterministic fingerprint of validation result.
    
    H(
      sorted(
        violation.rule_id +
        violation.message +
        violation.field_path
      )
    )
    
    Spec-pure: fingerprint does NOT include passed flag.
    This ensures cross-node equivalence regardless of strict_mode differences.
    
    Unicode normalization (NFC) ensures cross-node equivalence.
    
    Args:
        result: Validation result
        
    Returns:
        SHA-256 hex digest
    """
    # Handle empty violations case
    if not result.violations:
        # Empty result fingerprint: H([])
        parts: List[str] = []
        canonical = json.dumps(
            parts,
            sort_keys=False,
            separators=(",", ":"),
            ensure_ascii=True
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    
    # Sort violations deterministically
    sorted_violations = sorted(result.violations)
    
    # Create canonical representation
    # Spec-pure: ONLY sorted violations, no passed flag
    violation_parts = []
    for v in sorted_violations:
        # Normalize unicode for cross-node equivalence
        rule_id_norm = unicodedata.normalize('NFC', v.rule_id)
        message_norm = unicodedata.normalize('NFC', v.message)
        field_path_norm = unicodedata.normalize('NFC', v.field_path or "")
        violation_parts.append(
            f"{rule_id_norm}:{message_norm}:{field_path_norm}"
        )
    
    # Fingerprint is ONLY sorted violations (spec-pure)
    canonical = json.dumps(
        violation_parts,
        sort_keys=False,  # Already sorted
        separators=(",", ":"),
        ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
