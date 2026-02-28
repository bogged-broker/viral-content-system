"""
/data/validation/validators.py

Validation Orchestrator Specification
(Deterministic Rule Execution Engine)

---

1️⃣ Purpose

This file:

- Executes validation rules.
- Composes rule groups.
- Applies context constraints.
- Enforces strict vs permissive semantics.
- Enforces fail-fast logic.
- Sorts violations deterministically.
- Produces ValidationResult.

It is the only public execution entrypoint of the validation subsystem.

Every other file defines rules.

Only this file may:

- Call rule evaluate functions.
- Apply severity logic.
- Aggregate violations.
- Generate validation fingerprints.

Nothing else may assemble ValidationResult.

---

2️⃣ Position in System Architecture

Validation execution sits strictly before:

- lineage record creation
- mutation proposal emission
- migration plan execution
- registry update
- compatibility update
- governance lock mutation
- snapshot sealing

This file ensures:

No artifact
No mutation
No governance action
No migration plan

Touches state unless it passes this orchestrator.

---

3️⃣ Deterministic Execution Contract

Given:

    input = x
    context = c
    rule_set = R

The output must satisfy:

    run_validation(x, c, R) == run_validation(x, c, R)

Across:

- Nodes
- Machines
- Replays
- Time

Rule execution order must be deterministic.

Violation ordering must be deterministic.

Fingerprint must be deterministic.

---

4️⃣ Core Responsibilities

The orchestrator must:

1. Select applicable rules.
2. Execute rules deterministically.
3. Collect violations.
4. Apply fail-fast semantics.
5. Enforce strict_mode severity behavior.
6. Sort violations.
7. Compute result fingerprint.
8. Produce immutable ValidationResult.

It must NOT:

- Modify input.
- Handle IO.
- Mutate registry.
- Log to disk.
- Reference global config.
- Dynamically discover rules at runtime (unless rule registry fingerprinted).

---

5️⃣ Rule Source

Rules may come from:

- field_rules.py
- semantic_rules.py
- invariants.py
- compatibility_guards.py
- deterministic_checks.py

But this file must not embed rule logic.

Instead, it receives rule lists from those modules.

Each must expose deterministic ordered lists.

---

6️⃣ Deterministic Rule Ordering

Rule execution order must be stable.

Rule sets must be sorted by:

    rule.rule_id

Even if imported in arbitrary order.

No reliance on module definition order.

No reliance on dict ordering.

---

7️⃣ Orchestration Function

Canonical entrypoint: run_validation()

This is the constitutional path.

---

8️⃣ Strict Mode Enforcement

Strict mode must alter behavior of WARNING level.

Strict mode never downgrades ERROR. Only upgrades WARNING.

---

9️⃣ Validation Fingerprint

Fingerprint must be deterministic.

Fingerprint properties:

- Identical across nodes.
- Independent of runtime order.
- Independent of rule execution timing.
- Depends only on sorted violation content.

This enables:

- Cross-node rejection equivalence.
- Replay proof.
- Audit verification.

---

🔟 Full-Stack Entry Function

To avoid partial execution misuse, provide unified entry:

    validate_artifact(input_data, context)

This function becomes the canonical entrypoint.

No file outside /validation/ may assemble rule sets manually.

---

1️⃣1️⃣ Domain-Specific Entry Points

Optionally:

- validate_mutation(...)
- validate_migration_plan(...)
- validate_registry(...)
- validate_governance_operation(...)

Each composes rule sets relevant to that scope.

They must still call run_validation.

---

1️⃣2️⃣ Determinism Constraints

This file must:

- Not depend on runtime environment.
- Not use datetime.
- Not use random.
- Not introspect global registry state unless fingerprint included in context.
- Not depend on external services.

Everything must derive from:

- input_data
- context
- rules

Nothing else.

---

1️⃣3️⃣ Replay Compatibility

During event replay:

- Same input payload.
- Same validation context fingerprint.
- Same rule set version.

Must produce identical ValidationResult.

If not:

Your lineage replay is broken.

This file guarantees replay integrity.

---

1️⃣4️⃣ Failure Isolation

Even if rule execution raises unexpected exception, the orchestrator must not corrupt output.

Rule failures must not silently pass.

System failure is distinct from validation failure.

---

1️⃣5️⃣ Security Guarantees

By centralizing execution, you prevent:

- Rule skipping.
- Partial validation.
- Non-deterministic rule composition.
- Mixed severity interpretation.
- Inconsistent fail-fast logic.
- Cross-node disagreement on violation ordering.

This orchestrator ensures:

Validation is a single authoritative gate.

---

1️⃣6️⃣ Testing Requirements

- Repeated identical input test.
- Random rule ordering input test (should normalize).
- Strict vs non-strict comparison test.
- Fail-fast vs full-report equivalence test.
- Large rule count scaling test.
- Cross-node equivalence test.
- Fingerprint reproducibility test.

If any test produces variation → deterministic violation.

---

1️⃣7️⃣ Absolute Definition

/data/validation/validators.py is:

> The deterministic execution engine that applies validation rules under a formal contract,
> enforces severity and mode semantics, guarantees violation ordering stability, and produces
> replay-stable validation results before any system state transition may occur.

Contracts define legitimacy. This file enforces it.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, List, Optional, Tuple

from .validation_contract import (
    ValidationContext,
    ValidationResult,
    ValidationRule,
    ValidationViolation,
    SeverityLevel,
    ValidationError,
)


__all__ = [
    "run_validation",
    "determine_passed",
    "compute_validation_fingerprint",
    "sort_violations",
    "validate_artifact",
    "validate_mutation",
    "validate_migration_plan",
    "validate_registry",
    "validate_governance_operation",
    "SystemValidationFailure",
]


# ============================================================================
# Exceptions
# ============================================================================


class SystemValidationFailure(ValidationError):
    """
    Exception raised when validation orchestrator encounters a system error.
    
    This is distinct from validation failure (which returns ValidationResult).
    This indicates the orchestrator itself failed, not the input data.
    """
    
    def __init__(self, rule_id: str, message: str = ""):
        super().__init__(
            f"System validation failure in rule {rule_id}: {message}",
            rule_id=rule_id,
        )
        self.rule_id = rule_id


# ============================================================================
# Violation Sorting
# ============================================================================


def sort_violations(violations: Iterable[ValidationViolation]) -> Tuple[ValidationViolation, ...]:
    """
    Sort violations deterministically.
    
    Sorting order:
    1. rule_id (ascending)
    2. field_path (ascending, None treated as empty string)
    3. message (ascending)
    
    This ensures stable ordering across nodes and replays.
    
    Args:
        violations: Iterable of validation violations
        
    Returns:
        Sorted tuple of violations
    """
    return tuple(sorted(violations))


# ============================================================================
# Pass/Fail Determination
# ============================================================================


def determine_passed(
    violations: Iterable[ValidationViolation],
    context: ValidationContext,
) -> bool:
    """
    Determine if validation passed based on violations and context.
    
    Validation fails if:
    - Any CRITICAL violation exists
    - Any ERROR violation exists
    - Any WARNING violation exists AND strict_mode is True
    
    Validation passes if:
    - No violations exist, OR
    - Only INFO violations exist, OR
    - Only WARNING violations exist AND strict_mode is False
    
    Args:
        violations: Iterable of validation violations
        context: Validation context with strict_mode flag
        
    Returns:
        True if validation passed, False otherwise
    """
    for violation in violations:
        # CRITICAL always fails
        if violation.severity == SeverityLevel.CRITICAL:
            return False
        
        # ERROR always fails
        if violation.severity == SeverityLevel.ERROR:
            return False
        
        # WARNING fails only in strict mode
        if (
            violation.severity == SeverityLevel.WARNING
            and context.strict_mode
        ):
            return False
    
    # No blocking violations found
    return True


# ============================================================================
# Validation Fingerprint
# ============================================================================


def compute_validation_fingerprint(
    violations: Iterable[ValidationViolation],
) -> str:
    """
    Compute deterministic fingerprint from violations.
    
    Fingerprint is computed from sorted violation content:
    - rule_id
    - message
    - field_path (or empty string)
    
    This ensures:
    - Identical violations produce identical fingerprints
    - Order-independent (violations are sorted first)
    - Cross-node equivalence
    - Replay stability
    
    Args:
        violations: Iterable of validation violations
        
    Returns:
        SHA-256 hex digest of canonical violation representation
    """
    # Sort violations deterministically
    sorted_violations = sort_violations(violations)
    
    # Create canonical material string
    material_parts = []
    for violation in sorted_violations:
        material_parts.append(
            violation.rule_id + violation.message + (violation.field_path or "")
        )
    
    # Join and hash
    material = "".join(material_parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ============================================================================
# Core Orchestration Function
# ============================================================================


def run_validation(
    input_data: Any,
    context: ValidationContext,
    rules: Iterable[ValidationRule],
) -> ValidationResult:
    """
    Canonical validation orchestration function.
    
    This is the only function that may:
    - Execute rule.evaluate()
    - Aggregate violations
    - Apply severity logic
    - Generate validation fingerprints
    
    Execution guarantees:
    - Deterministic rule ordering (sorted by rule_id)
    - Deterministic violation ordering
    - Fail-fast behavior when configured
    - Strict mode enforcement
    - Immutable result
    
    Args:
        input_data: Data to validate (must not be modified)
        context: Validation context (must not be modified)
        rules: Iterable of validation rules (will be sorted by rule_id)
        
    Returns:
        ValidationResult with sorted violations and fingerprint
        
    Raises:
        SystemValidationFailure: If rule execution raises unexpected exception
    """
    violations: List[ValidationViolation] = []
    
    # Sort rules deterministically by rule_id
    # This ensures stable execution order across nodes and replays
    ordered_rules = sorted(rules, key=lambda r: r.rule_id)
    
    # Execute rules in deterministic order
    for rule in ordered_rules:
        # Check if rule applies to this context
        if not rule.should_apply(context):
            continue
        
        # Evaluate rule
        try:
            violation = rule.evaluate(input_data, context)
            
            if violation is None:
                # Rule passed, continue to next rule
                continue
            
            # Rule failed, record violation
            violations.append(violation)
            
            # Fail-fast behavior
            # CRITICAL always stops execution immediately
            # When fail_fast is True, also stop on ERROR
            if violation.severity == SeverityLevel.CRITICAL or (
                context.fail_fast and violation.severity == SeverityLevel.ERROR
            ):
                break
        
        except Exception as e:
            # Rule execution raised unexpected exception
            # This is a system failure, not a validation failure
            raise SystemValidationFailure(
                rule_id=rule.rule_id,
                message=f"Rule evaluation raised exception: {e}",
            ) from e
    
    # Sort violations deterministically
    violations = sort_violations(violations)
    
    # Determine if validation passed
    passed = determine_passed(violations, context)
    
    # Compute validation fingerprint before constructing result
    fingerprint = compute_validation_fingerprint(violations)
    
    # Create immutable result with fingerprint set immediately
    # Since validation_fingerprint has init=False, we must set it via object.__setattr__
    # This is done immediately after construction to avoid post-init mutation semantics
    result = ValidationResult(
        passed=passed,
        violations=violations,
    )
    # Set fingerprint immediately to use our deterministic computation
    # This ensures consistency and avoids relying on __post_init__ computation
    object.__setattr__(result, "validation_fingerprint", fingerprint)
    
    return result


# ============================================================================
# Full-Stack Entry Functions
# ============================================================================


def validate_artifact(
    input_data: Any,
    context: ValidationContext,
) -> ValidationResult:
    """
    Full-stack validation entrypoint for artifacts.
    
    This function:
    - Imports all rule sets
    - Composes them into a single rule list
    - Executes validation via run_validation()
    
    This is the canonical entrypoint for artifact validation.
    
    No file outside /validation/ may assemble rule sets manually.
    
    Args:
        input_data: Artifact data to validate
        context: Validation context
        
    Returns:
        ValidationResult with all violations
        
    Raises:
        SystemValidationFailure: If rule execution fails
        ImportError: If rule modules cannot be imported
    """
    # Import rule sets from rule definition modules
    # These modules must expose deterministic ordered lists
    # Hard-fail on missing modules to ensure deterministic validation semantics
    try:
        from .field_rules import FIELD_RULES
    except ImportError as e:
        raise SystemValidationFailure(
            rule_id="field_rules",
            message=f"Rule module missing: {e}",
        ) from e
    
    try:
        from .semantic_rules import SEMANTIC_RULES
    except ImportError as e:
        raise SystemValidationFailure(
            rule_id="semantic_rules",
            message=f"Rule module missing: {e}",
        ) from e
    
    try:
        from .invariants import INVARIANT_RULES
    except ImportError as e:
        raise SystemValidationFailure(
            rule_id="invariants",
            message=f"Rule module missing: {e}",
        ) from e
    
    try:
        from .compatibility_guards import COMPATIBILITY_RULES
    except ImportError as e:
        raise SystemValidationFailure(
            rule_id="compatibility_guards",
            message=f"Rule module missing: {e}",
        ) from e
    
    try:
        from .deterministic_checks import DETERMINISM_RULES
    except ImportError as e:
        raise SystemValidationFailure(
            rule_id="deterministic_checks",
            message=f"Rule module missing: {e}",
        ) from e
    
    # Compose all rule sets
    all_rules = list(FIELD_RULES) + list(SEMANTIC_RULES) + list(INVARIANT_RULES) + list(COMPATIBILITY_RULES) + list(DETERMINISM_RULES)
    
    # Execute validation
    return run_validation(input_data, context, all_rules)


# ============================================================================
# Domain-Specific Entry Points
# ============================================================================


def validate_mutation(
    input_data: Any,
    context: ValidationContext,
) -> ValidationResult:
    """
    Validate mutation payload.
    
    Composes rule sets relevant to mutation validation.
    Must still call run_validation().
    
    Args:
        input_data: Mutation payload to validate
        context: Validation context
        
    Returns:
        ValidationResult with mutation-specific violations
    """
    # Import mutation-specific rule sets
    # Hard-fail on missing modules to ensure deterministic validation semantics
    try:
        from .field_rules import FIELD_RULES
    except ImportError as e:
        raise SystemValidationFailure(
            rule_id="field_rules",
            message=f"Rule module missing: {e}",
        ) from e
    
    try:
        from .semantic_rules import SEMANTIC_RULES
    except ImportError as e:
        raise SystemValidationFailure(
            rule_id="semantic_rules",
            message=f"Rule module missing: {e}",
        ) from e
    
    try:
        from .invariants import INVARIANT_RULES
    except ImportError as e:
        raise SystemValidationFailure(
            rule_id="invariants",
            message=f"Rule module missing: {e}",
        ) from e
    
    # Compose mutation-relevant rules
    mutation_rules = list(FIELD_RULES) + list(SEMANTIC_RULES) + list(INVARIANT_RULES)
    
    return run_validation(input_data, context, mutation_rules)


def validate_migration_plan(
    input_data: Any,
    context: ValidationContext,
) -> ValidationResult:
    """
    Validate migration plan.
    
    Composes rule sets relevant to migration plan validation.
    Must still call run_validation().
    
    Args:
        input_data: Migration plan to validate
        context: Validation context
        
    Returns:
        ValidationResult with migration-specific violations
    """
    # Import migration-specific rule sets
    # Hard-fail on missing modules to ensure deterministic validation semantics
    try:
        from .semantic_rules import SEMANTIC_RULES
    except ImportError as e:
        raise SystemValidationFailure(
            rule_id="semantic_rules",
            message=f"Rule module missing: {e}",
        ) from e
    
    try:
        from .compatibility_guards import COMPATIBILITY_RULES
    except ImportError as e:
        raise SystemValidationFailure(
            rule_id="compatibility_guards",
            message=f"Rule module missing: {e}",
        ) from e
    
    try:
        from .deterministic_checks import DETERMINISM_RULES
    except ImportError as e:
        raise SystemValidationFailure(
            rule_id="deterministic_checks",
            message=f"Rule module missing: {e}",
        ) from e
    
    # Compose migration-relevant rules
    migration_rules = list(SEMANTIC_RULES) + list(COMPATIBILITY_RULES) + list(DETERMINISM_RULES)
    
    return run_validation(input_data, context, migration_rules)


def validate_registry(
    input_data: Any,
    context: ValidationContext,
) -> ValidationResult:
    """
    Validate version registry.
    
    Composes rule sets relevant to registry validation.
    Must still call run_validation().
    
    Args:
        input_data: Registry data to validate
        context: Validation context
        
    Returns:
        ValidationResult with registry-specific violations
    """
    # Import registry-specific rule sets
    # Hard-fail on missing modules to ensure deterministic validation semantics
    try:
        from .field_rules import FIELD_RULES
    except ImportError as e:
        raise SystemValidationFailure(
            rule_id="field_rules",
            message=f"Rule module missing: {e}",
        ) from e
    
    try:
        from .compatibility_guards import COMPATIBILITY_RULES
    except ImportError as e:
        raise SystemValidationFailure(
            rule_id="compatibility_guards",
            message=f"Rule module missing: {e}",
        ) from e
    
    try:
        from .deterministic_checks import DETERMINISM_RULES
    except ImportError as e:
        raise SystemValidationFailure(
            rule_id="deterministic_checks",
            message=f"Rule module missing: {e}",
        ) from e
    
    # Compose registry-relevant rules
    registry_rules = list(FIELD_RULES) + list(COMPATIBILITY_RULES) + list(DETERMINISM_RULES)
    
    return run_validation(input_data, context, registry_rules)


def validate_governance_operation(
    input_data: Any,
    context: ValidationContext,
) -> ValidationResult:
    """
    Validate governance operation.
    
    Composes rule sets relevant to governance validation.
    Must still call run_validation().
    
    Args:
        input_data: Governance operation to validate
        context: Validation context
        
    Returns:
        ValidationResult with governance-specific violations
    """
    # Import governance-specific rule sets
    # Hard-fail on missing modules to ensure deterministic validation semantics
    try:
        from .invariants import INVARIANT_RULES
    except ImportError as e:
        raise SystemValidationFailure(
            rule_id="invariants",
            message=f"Rule module missing: {e}",
        ) from e
    
    try:
        from .deterministic_checks import DETERMINISM_RULES
    except ImportError as e:
        raise SystemValidationFailure(
            rule_id="deterministic_checks",
            message=f"Rule module missing: {e}",
        ) from e
    
    # Compose governance-relevant rules
    governance_rules = list(INVARIANT_RULES) + list(DETERMINISM_RULES)
    
    return run_validation(input_data, context, governance_rules)
