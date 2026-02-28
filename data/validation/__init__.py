"""
/data/validation/

Canonical Validation Framework

This package provides the deterministic, side-effect-free pre-state enforcement
framework that governs all structural and semantic legitimacy checks before any
data, mutation, or governance action is allowed to interact with the lineage system.

Core Contract:
    /data/validation/validation_contract.py

All validators must depend on the validation contract. Nothing may bypass this layer.
"""

from .validation_contract import (
    SeverityLevel,
    ValidationScope,
    ValidationContext,
    ValidationRule,
    ValidationViolation,
    ValidationResult,
    ValidationEvaluator,
    ValidationError,
    ValidationContract,
    compute_violation_hash,
    compute_result_fingerprint,
)
from .validators import (
    run_validation,
    determine_passed,
    compute_validation_fingerprint,
    sort_violations,
    validate_artifact,
    validate_mutation,
    validate_migration_plan,
    validate_registry,
    validate_governance_operation,
    SystemValidationFailure,
)
from .ejection_reasons import (
    RejectionSeverity,
    RejectionCategory,
    RejectionDeterminismClass,
    EjectionReason,
    ALL_EJECTION_REASONS,
    validate_registry_integrity,
    get_reasons_by_category,
    get_reasons_by_severity,
    get_reason_by_code,
    sort_violations_by_reason,
)
from .policy_profiles import (
    ValidationProfile,
    ProfileConfig,
    PolicyDecision,
    VersionedProfile,
    STRICT_PROFILE,
    RECOVERY_PROFILE,
    AUDIT_PROFILE,
    MIGRATION_PROFILE,
    get_profile_config,
    get_profile_version,
    get_versioned_profile,
    evaluate_bundle,
    make_policy_decision,
    compute_decision_hash,
    PROFILE_REGISTRY,
    PROFILE_VERSIONS,
    get_severity_order,
    compare_severities,
)
from .audit_log_model import (
    ValidationAuditEvent,
    SignedValidationAuditEvent,
    AUDIT_SCHEMA_VERSION,
    build_audit_event,
    audit_event_to_dict,
    audit_event_to_json,
    compute_audit_hash,
    verify_audit_integrity,
    verify_replay_equivalence,
    compute_policy_decision_hash,
    compute_full_audit_hash,
)
from .rejection_reasons import (
    FixType,
    ExplainableReason,
    Remediation,
    EXPLAINABLE_REGISTRY,
    EXPLANATION_SCHEMA_VERSION,
    render_violation,
    render_violation_dict,
    render_violation_with_level,
    validate_explainable_registry,
    get_explainable_reason,
    compute_template_hash,
    validate_template_hashes,
)

__all__ = [
    # Contract types
    "SeverityLevel",
    "ValidationScope",
    "ValidationContext",
    "ValidationRule",
    "ValidationViolation",
    "ValidationResult",
    "ValidationEvaluator",
    "ValidationError",
    "ValidationContract",
    "compute_violation_hash",
    "compute_result_fingerprint",
    # Orchestrator functions (canonical entrypoints)
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
    # Ejection reasons taxonomy
    "RejectionSeverity",
    "RejectionCategory",
    "RejectionDeterminismClass",
    "EjectionReason",
    "ALL_EJECTION_REASONS",
    "validate_registry_integrity",
    "get_reasons_by_category",
    "get_reasons_by_severity",
    "get_reason_by_code",
    "sort_violations_by_reason",
    # Policy profiles
    "ValidationProfile",
    "ProfileConfig",
    "PolicyDecision",
    "VersionedProfile",
    "STRICT_PROFILE",
    "RECOVERY_PROFILE",
    "AUDIT_PROFILE",
    "MIGRATION_PROFILE",
    "get_profile_config",
    "get_profile_version",
    "get_versioned_profile",
    "evaluate_bundle",
    "make_policy_decision",
    "compute_decision_hash",
    "PROFILE_REGISTRY",
    "PROFILE_VERSIONS",
    "get_severity_order",
    "compare_severities",
    # Audit log model
    "ValidationAuditEvent",
    "SignedValidationAuditEvent",
    "AUDIT_SCHEMA_VERSION",
    "build_audit_event",
    "audit_event_to_dict",
    "audit_event_to_json",
    "compute_audit_hash",
    "verify_audit_integrity",
    "verify_replay_equivalence",
    "compute_policy_decision_hash",
    "compute_full_audit_hash",
    # Rejection reasons (explainable)
    "FixType",
    "ExplainableReason",
    "Remediation",
    "EXPLAINABLE_REGISTRY",
    "EXPLANATION_SCHEMA_VERSION",
    "render_violation",
    "render_violation_dict",
    "render_violation_with_level",
    "validate_explainable_registry",
    "get_explainable_reason",
    "compute_template_hash",
    "validate_template_hashes",
]
