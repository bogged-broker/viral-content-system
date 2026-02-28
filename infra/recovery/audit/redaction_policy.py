"""
/infra/recovery/audit/redaction_policy.py

Centralized Redaction Policy Authority

PURPOSE:
    Separates policy definition from policy execution.
    Policy authority is distinct from redaction implementation.

PRINCIPLE:
    Policy changes are auditable events.
    Policy is versioned, immutable, and centrally managed.

AUTHORITY SEPARATION:
    - Policy definition: This file
    - Policy execution: audit_redactor.py
    - Policy validation: audit_redactor.py (conflict detection)
"""

from __future__ import annotations

# Import from audit_redactor (avoid circular import by importing at function level)
# This maintains authority separation while avoiding import cycles


def create_default_policy() -> "AuditRedactionPolicy":
    """
    Create a reasonable default redaction policy.

    This is a STARTING POINT — production deployments should
    customize based on their specific compliance requirements.

    POLICY AUTHORITY:
        This function is the single source of truth for default policy.
        Changes here are policy changes and must be audited.
        
    TIER-0: Explicit reversible/irreversible contract for regulatory compliance.
    """
    # Import here to avoid circular dependency
    from .audit_redactor import (
        AuditRedactionPolicy,
        RedactionRule,
        RedactionLevel,
        RedactionReason,
    )
    
    rules = (
        # PII - Privacy protection (reversible for GDPR compliance)
        RedactionRule(
            field_path="payload.ip_address",
            max_disclosure_level=RedactionLevel.INTERNAL,
            replacement="[IP_REDACTED]",
            reason=RedactionReason.PRIVACY,
            is_reversible=True,  # Can be reversed for data subject requests
        ),
        RedactionRule(
            field_path="payload.email",
            max_disclosure_level=RedactionLevel.INTERNAL,
            replacement="[EMAIL_REDACTED]",
            reason=RedactionReason.PRIVACY,
            is_reversible=True,  # Can be reversed for data subject requests
        ),
        RedactionRule(
            field_path="payload.user_id",
            max_disclosure_level=RedactionLevel.EXTERNAL,
            replacement="[USER_REDACTED]",
            reason=RedactionReason.PRIVACY,
            is_reversible=True,  # Can be reversed for data subject requests
        ),
        # Security - Credentials & secrets (IRREVERSIBLE for security)
        RedactionRule(
            field_path="payload.session_token",
            max_disclosure_level=RedactionLevel.NONE,
            replacement="[TOKEN_REDACTED]",
            reason=RedactionReason.SECURITY,
            is_reversible=False,  # Security tokens must never be recoverable
        ),
        RedactionRule(
            field_path="payload.api_key",
            max_disclosure_level=RedactionLevel.NONE,
            replacement="[KEY_REDACTED]",
            reason=RedactionReason.SECURITY,
            is_reversible=False,  # API keys must never be recoverable
        ),
        RedactionRule(
            field_path="payload.password_hash",
            max_disclosure_level=RedactionLevel.NONE,
            replacement="[HASH_REDACTED]",
            reason=RedactionReason.SECURITY,
            is_reversible=False,  # Password hashes must never be recoverable
        ),
        RedactionRule(
            field_path="payload.internal_hostname",
            max_disclosure_level=RedactionLevel.INTERNAL,
            replacement="[HOST_REDACTED]",
            reason=RedactionReason.SECURITY,
            is_reversible=True,  # Hostnames can be reversed for internal ops
        ),
        # Trade secrets - Internal algorithms & configs (IRREVERSIBLE)
        RedactionRule(
            field_path="payload.algorithm_params",
            max_disclosure_level=RedactionLevel.EXTERNAL,
            replacement="[PARAMS_REDACTED]",
            reason=RedactionReason.TRADE_SECRET,
            is_reversible=False,  # Trade secrets must never be recoverable
        ),
        RedactionRule(
            field_path="payload.model_weights",
            max_disclosure_level=RedactionLevel.INTERNAL,
            replacement=None,  # Remove entirely
            reason=RedactionReason.TRADE_SECRET,
            is_reversible=False,  # Model weights must never be recoverable
        ),
        # Metadata - Context-specific
        RedactionRule(
            field_path="metadata.internal_notes",
            max_disclosure_level=RedactionLevel.INTERNAL,
            replacement=None,
            reason=RedactionReason.LEGAL,
            is_reversible=True,  # Legal notes may need to be recovered
        ),
        RedactionRule(
            field_path="metadata.debug_info",
            max_disclosure_level=RedactionLevel.EXTERNAL,
            replacement=None,
            reason=RedactionReason.SECURITY,
            is_reversible=True,  # Debug info can be reversed for troubleshooting
        ),
    )

    return AuditRedactionPolicy(rules=rules, policy_version="1.0.0")
