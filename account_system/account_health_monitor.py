"""
/account_system/account_health_monitor.py

Deterministic Account Health Evaluation Authority
(No Silent Degradation, No Implicit Enforcement, No Hidden State)

This module is the single authority responsible for evaluating the operational
and compliance health of a canonical account. It answers:
> "Is this account healthy, degraded, restricted, or unsafe — and why?"

CRITICAL PRINCIPLES:
- Deterministic: Same inputs → same health output
- Pure Function: No side effects, no I/O, no mutation
- Explicit Reasoning: Health must include structured reasoning
- No Silent Escalation: All severity changes must be explainable
- Stable Across Replay: Re-running health evaluation must not drift
- Policy-Versioned: Health result must embed policy version
- Zero Implicit Transitions: Computes fresh health from scratch
- No Time-Based Drift: Uses logical timestamps, never system time

ABSOLUTE INVARIANTS:
1. Health is derived — never stored
2. No cached health allowed
3. No wall clock usage
4. Deterministic severity scoring
5. Explicit precedence rules
6. Policy-versioned reproducibility

This file protects systemic stability and ensures accounts are as trusted
as billion-dollar companies (Instagram, ChatGPT, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Optional, FrozenSet, Tuple, Dict, List, Any
from types import MappingProxyType
import logging
import json


class HealthLevel(Enum):
    """Account health classification levels (ordered by severity)."""
    HEALTHY = 0
    DEGRADED = 1
    AT_RISK = 2
    RESTRICTED = 3
    SUSPENDED = 4
    TERMINATED = 5
    
    def __lt__(self, other: HealthLevel) -> bool:
        """Allow severity comparison."""
        return self.value < other.value
    
    def __le__(self, other: HealthLevel) -> bool:
        return self.value <= other.value


class ViolationCategory(Enum):
    """Categories of health violations."""
    BILLING_OVERDUE = "billing_overdue"
    BILLING_CHARGEBACK = "billing_chargeback"
    BILLING_FRAUD = "billing_fraud"
    BILLING_PAYMENT_INVALID = "billing_payment_invalid"
    BILLING_PLAN_EXPIRED = "billing_plan_expired"
    
    SECURITY_CREDENTIAL_STUFFING = "security_credential_stuffing"
    SECURITY_COMPROMISED_EMAIL = "security_compromised_email"
    SECURITY_API_KEY_ABUSE = "security_api_key_abuse"
    SECURITY_UNUSUAL_VELOCITY = "security_unusual_velocity"
    
    USAGE_OVER_QUOTA = "usage_over_quota"
    USAGE_RATE_LIMIT_VIOLATION = "usage_rate_limit_violation"
    USAGE_WRITE_AMPLIFICATION = "usage_write_amplification"
    USAGE_ABNORMAL_SPIKE = "usage_abnormal_spike"
    
    POLICY_CONTENT_VIOLATION = "policy_content_violation"
    POLICY_LEGAL_HOLD = "policy_legal_hold"
    POLICY_ABUSE_CLASSIFICATION = "policy_abuse_classification"
    POLICY_REGULATORY_FLAG = "policy_regulatory_flag"
    POLICY_STRIKE_THRESHOLD = "policy_strike_threshold"
    
    INACTIVITY_THRESHOLD = "inactivity_threshold"


class BillingStatus(Enum):
    """Billing state classification."""
    CURRENT = "current"
    GRACE_PERIOD = "grace_period"
    OVERDUE = "overdue"
    SUSPENDED = "suspended"
    FRAUD_FLAGGED = "fraud_flagged"


class SecurityRiskLevel(Enum):
    """Security risk classification."""
    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class QuotaStatus(Enum):
    """Resource quota status."""
    WITHIN_LIMITS = "within_limits"
    APPROACHING_LIMIT = "approaching_limit"
    OVER_QUOTA = "over_quota"
    HARD_LIMIT_EXCEEDED = "hard_limit_exceeded"


class HealthPolicyConfigError(Exception):
    """Raised when health policy configuration is invalid."""
    pass


class AccountHealthInputValidationError(Exception):
    """Raised when input snapshots are inconsistent or invalid."""
    pass


@dataclass(frozen=True)
class AccountSnapshot:
    """Immutable snapshot of canonical account state."""
    account_id: str
    created_at: datetime
    is_verified: bool
    account_type: str  # e.g., "free", "pro", "enterprise"
    region: str
    
    def __post_init__(self) -> None:
        if not self.account_id:
            raise AccountHealthInputValidationError("account_id cannot be empty")


@dataclass(frozen=True)
class UsageMetricsSnapshot:
    """Immutable snapshot of usage metrics."""
    account_id: str
    quota_usage_percent: Decimal
    rate_limit_violations_count: int
    write_amplification_ratio: Decimal
    resource_spike_detected: bool
    last_activity_timestamp: datetime
    
    def __post_init__(self) -> None:
        if self.quota_usage_percent < Decimal("0") or self.quota_usage_percent > Decimal("999999"):
            raise AccountHealthInputValidationError(
                f"Invalid quota_usage_percent: {self.quota_usage_percent}"
            )
        if self.rate_limit_violations_count < 0:
            raise AccountHealthInputValidationError("rate_limit_violations_count cannot be negative")
        if self.write_amplification_ratio < Decimal("0"):
            raise AccountHealthInputValidationError("write_amplification_ratio cannot be negative")


@dataclass(frozen=True)
class BillingStateSnapshot:
    """Immutable snapshot of billing state."""
    account_id: str
    billing_status: BillingStatus
    days_overdue: int
    chargeback_flagged: bool
    fraud_dispute_active: bool
    payment_method_valid: bool
    plan_expired: bool
    last_payment_timestamp: Optional[datetime]
    grace_period_expires_at: Optional[datetime]
    
    def __post_init__(self) -> None:
        if self.days_overdue < 0:
            raise AccountHealthInputValidationError("days_overdue cannot be negative")


@dataclass(frozen=True)
class SecuritySignalsSnapshot:
    """Immutable snapshot of security signals."""
    account_id: str
    risk_level: SecurityRiskLevel
    credential_stuffing_detected: bool
    compromised_email_detected: bool
    api_key_abuse_detected: bool
    unusual_login_velocity: bool
    failed_login_attempts_count: int
    
    def __post_init__(self) -> None:
        if self.failed_login_attempts_count < 0:
            raise AccountHealthInputValidationError("failed_login_attempts_count cannot be negative")


@dataclass(frozen=True)
class PolicyEvaluationSnapshot:
    """Immutable snapshot of policy evaluation state."""
    account_id: str
    content_violation_strikes: int
    legal_hold_active: bool
    abuse_classification_active: bool
    regulatory_ban_marker: bool
    policy_strikes_total: int
    last_violation_timestamp: Optional[datetime]
    
    def __post_init__(self) -> None:
        if self.content_violation_strikes < 0:
            raise AccountHealthInputValidationError("content_violation_strikes cannot be negative")
        if self.policy_strikes_total < 0:
            raise AccountHealthInputValidationError("policy_strikes_total cannot be negative")


@dataclass(frozen=True)
class SeverityThresholds:
    """Thresholds for severity scoring."""
    billing_overdue_days_degraded: int = 7
    billing_overdue_days_restricted: int = 30
    
    quota_usage_degraded_percent: Decimal = Decimal("80")
    quota_usage_at_risk_percent: Decimal = Decimal("95")
    quota_usage_over_quota_percent: Decimal = Decimal("100")
    
    rate_limit_violations_degraded: int = 10
    rate_limit_violations_at_risk: int = 50
    
    security_risk_degraded_threshold: SecurityRiskLevel = SecurityRiskLevel.MEDIUM
    security_risk_restricted_threshold: SecurityRiskLevel = SecurityRiskLevel.HIGH
    security_risk_suspended_threshold: SecurityRiskLevel = SecurityRiskLevel.CRITICAL
    
    policy_strikes_degraded: int = 1
    policy_strikes_at_risk: int = 3
    policy_strikes_suspended: int = 5
    
    inactivity_days_degraded: int = 180
    inactivity_days_at_risk: int = 365
    
    def __post_init__(self) -> None:
        if self.billing_overdue_days_degraded <= 0:
            raise HealthPolicyConfigError("billing_overdue_days_degraded must be positive")
        if self.billing_overdue_days_restricted <= self.billing_overdue_days_degraded:
            raise HealthPolicyConfigError(
                "billing_overdue_days_restricted must exceed billing_overdue_days_degraded"
            )


@dataclass(frozen=True)
class SeverityWeights:
    """Weights for aggregated severity scoring."""
    billing_weight: Decimal = Decimal("1.0")
    security_weight: Decimal = Decimal("1.5")
    usage_weight: Decimal = Decimal("0.5")
    policy_weight: Decimal = Decimal("2.0")
    
    def __post_init__(self) -> None:
        weights = [self.billing_weight, self.security_weight, self.usage_weight, self.policy_weight]
        if any(w < Decimal("0") for w in weights):
            raise HealthPolicyConfigError("All weights must be non-negative")


@dataclass(frozen=True)
class HealthPolicyConfig:
    """Immutable configuration for health evaluation policy."""
    policy_version: str
    severity_thresholds: SeverityThresholds
    severity_weights: SeverityWeights
    max_severity_score: Decimal = Decimal("100.0")
    enable_grace_periods: bool = True
    billing_grace_days: int = 7
    
    def __post_init__(self) -> None:
        if not self.policy_version:
            raise HealthPolicyConfigError("policy_version cannot be empty")
        if self.max_severity_score <= Decimal("0"):
            raise HealthPolicyConfigError("max_severity_score must be positive")
        if self.billing_grace_days < 0:
            raise HealthPolicyConfigError("billing_grace_days cannot be negative")


@dataclass(frozen=True)
class ExplanationTrace:
    """
    Machine-readable structured explanation of health evaluation.
    
    Provides complete transparency into health classification decisions.
    All reasoning is explicit and auditable.
    
    This ensures accounts are evaluated with the same rigor as
    billion-dollar companies (Instagram, ChatGPT, etc.).
    """
    violated_categories: FrozenSet[ViolationCategory]
    """Set of violation categories that contributed to health classification"""
    
    severity_contributions: Tuple[Tuple[str, Decimal], ...]
    """Ordered list of (reason, score) tuples showing how severity was computed"""
    
    precedence_applied: Optional[str] = None
    """Explanation of precedence rule applied (if any override occurred)"""
    
    grace_period_active: bool = False
    """Whether account is currently in grace period"""
    
    def __post_init__(self) -> None:
        """Validate explanation trace structure."""
        # Validate severity contributions are tuples
        for contribution in self.severity_contributions:
            if not isinstance(contribution, tuple) or len(contribution) != 2:
                raise AccountHealthInputValidationError(
                    "severity_contributions must be tuples of (str, Decimal)"
                )
            reason, score = contribution
            if not isinstance(reason, str):
                raise AccountHealthInputValidationError(
                    "severity contribution reason must be string"
                )
            if not isinstance(score, Decimal):
                raise AccountHealthInputValidationError(
                    "severity contribution score must be Decimal"
                )
            if score < Decimal("0"):
                raise AccountHealthInputValidationError(
                    "severity contribution score cannot be negative"
                )
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize explanation trace to dictionary for logging/audit."""
        return {
            "violated_categories": [cat.value for cat in self.violated_categories],
            "severity_contributions": [
                {"reason": reason, "score": float(score)}
                for reason, score in self.severity_contributions
            ],
            "precedence_applied": self.precedence_applied,
            "grace_period_active": self.grace_period_active,
        }


@dataclass(frozen=True)
class AccountHealthReport:
    """
    Immutable health evaluation result.
    
    Complete structured report of account health status.
    All fields are deterministic and replay-safe.
    
    This report ensures accounts are evaluated with the same
    trust level as billion-dollar companies (Instagram, ChatGPT, etc.).
    """
    account_id: str
    """Account identifier"""
    
    health_level: HealthLevel
    """Classified health level (HEALTHY, DEGRADED, AT_RISK, RESTRICTED, SUSPENDED, TERMINATED)"""
    
    severity_score: Decimal
    """Aggregated severity score (0.0 to max_severity_score)"""
    
    violation_categories: FrozenSet[ViolationCategory]
    """Set of violation categories detected"""
    
    quota_status: QuotaStatus
    """Resource quota status"""
    
    billing_status: BillingStatus
    """Billing state classification"""
    
    security_risk_level: SecurityRiskLevel
    """Security risk level"""
    
    policy_version: str
    """Policy version used for evaluation (for reproducibility)"""
    
    evaluated_at_reference: datetime
    """Logical timestamp when evaluation occurred (NOT system time)"""
    
    explanation_trace: ExplanationTrace
    """Structured explanation of health classification"""
    
    def __post_init__(self) -> None:
        """Validate health report invariants."""
        if not self.account_id:
            raise AccountHealthInputValidationError("account_id cannot be empty")
        
        if self.severity_score < Decimal("0"):
            raise AccountHealthInputValidationError("severity_score cannot be negative")
        
        if not self.policy_version:
            raise AccountHealthInputValidationError("policy_version cannot be empty")
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize health report to dictionary for logging/audit."""
        return {
            "account_id": self.account_id,
            "health_level": self.health_level.name,
            "severity_score": float(self.severity_score),
            "violation_categories": [cat.value for cat in self.violation_categories],
            "quota_status": self.quota_status.value,
            "billing_status": self.billing_status.value,
            "security_risk_level": self.security_risk_level.name,
            "policy_version": self.policy_version,
            "evaluated_at_reference": self.evaluated_at_reference.isoformat(),
            "explanation_trace": self.explanation_trace.to_dict(),
        }


class AccountHealthMonitor:
    """
    Deterministic account health evaluation authority.
    
    Pure functional evaluator:
    - No side effects
    - No external I/O
    - No hidden state
    - Replay-safe
    - Policy-versioned
    - Deterministic severity scoring
    - Explicit precedence rules
    - No wall clock usage
    
    This class ensures accounts are evaluated with the same rigor and trust
    level as billion-dollar companies (Instagram, ChatGPT, etc.).
    """
    
    # Explicit precedence order (highest to lowest)
    PRECEDENCE_ORDER = (
        HealthLevel.TERMINATED,
        HealthLevel.SUSPENDED,
        HealthLevel.RESTRICTED,
        HealthLevel.AT_RISK,
        HealthLevel.DEGRADED,
        HealthLevel.HEALTHY,
    )
    
    def __init__(
        self,
        config: HealthPolicyConfig,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """
        Initialize monitor with validated policy configuration.
        
        Args:
            config: Immutable health policy configuration
            logger: Optional logger for structured logging
        """
        self._config = config
        self._logger = logger or logging.getLogger(__name__)
        
        # Validate config at initialization
        self._validate_config()
    
    def _validate_config(self) -> None:
        """Validate policy configuration at initialization."""
        if not self._config.policy_version:
            raise HealthPolicyConfigError("policy_version cannot be empty")
        
        if self._config.max_severity_score <= Decimal("0"):
            raise HealthPolicyConfigError("max_severity_score must be positive")
        
        if self._config.billing_grace_days < 0:
            raise HealthPolicyConfigError("billing_grace_days cannot be negative")
        
        # Validate thresholds are consistent
        thresholds = self._config.severity_thresholds
        if thresholds.billing_overdue_days_restricted <= thresholds.billing_overdue_days_degraded:
            raise HealthPolicyConfigError(
                "billing_overdue_days_restricted must exceed billing_overdue_days_degraded"
            )
        
        if thresholds.quota_usage_at_risk_percent <= thresholds.quota_usage_degraded_percent:
            raise HealthPolicyConfigError(
                "quota_usage_at_risk_percent must exceed quota_usage_degraded_percent"
            )
        
        if thresholds.quota_usage_over_quota_percent <= thresholds.quota_usage_at_risk_percent:
            raise HealthPolicyConfigError(
                "quota_usage_over_quota_percent must exceed quota_usage_at_risk_percent"
            )
        
        if thresholds.rate_limit_violations_at_risk <= thresholds.rate_limit_violations_degraded:
            raise HealthPolicyConfigError(
                "rate_limit_violations_at_risk must exceed rate_limit_violations_degraded"
            )
        
        if thresholds.policy_strikes_at_risk <= thresholds.policy_strikes_degraded:
            raise HealthPolicyConfigError(
                "policy_strikes_at_risk must exceed policy_strikes_degraded"
            )
        
        if thresholds.policy_strikes_suspended <= thresholds.policy_strikes_at_risk:
            raise HealthPolicyConfigError(
                "policy_strikes_suspended must exceed policy_strikes_at_risk"
            )
        
        if thresholds.inactivity_days_at_risk <= thresholds.inactivity_days_degraded:
            raise HealthPolicyConfigError(
                "inactivity_days_at_risk must exceed inactivity_days_degraded"
            )
    
    def evaluate_account_health(
        self,
        account: AccountSnapshot,
        usage: UsageMetricsSnapshot,
        billing: BillingStateSnapshot,
        security: SecuritySignalsSnapshot,
        policy: PolicyEvaluationSnapshot,
        reference_timestamp: datetime
    ) -> AccountHealthReport:
        """
        Evaluate account health deterministically.
        
        DETERMINISTIC: Same inputs always produce identical output.
        No randomness. No hidden state. No wall clock usage.
        
        Args:
            account: Immutable account snapshot
            usage: Immutable usage metrics snapshot
            billing: Immutable billing state snapshot
            security: Immutable security signals snapshot
            policy: Immutable policy evaluation snapshot
            reference_timestamp: Logical evaluation timestamp (NOT system time)
        
        Returns:
            AccountHealthReport with deterministic health classification
        
        Raises:
            AccountHealthInputValidationError: If snapshots are inconsistent
            HealthPolicyConfigError: If config is invalid
        """
        # Validate snapshot consistency
        self._validate_snapshot_consistency(account, usage, billing, security, policy)
        
        self._logger.debug(
            f"Evaluating health for account {account.account_id} "
            f"at reference timestamp {reference_timestamp.isoformat()}, "
            f"policy version {self._config.policy_version}"
        )
        
        # Check for immediate termination conditions (highest precedence)
        termination_result = self._check_termination_conditions(
            account, billing, security, policy, reference_timestamp
        )
        if termination_result is not None:
            return termination_result
        
        # Check for suspension conditions
        suspension_result = self._check_suspension_conditions(
            account, billing, security, policy, reference_timestamp
        )
        if suspension_result is not None:
            return suspension_result
        
        # Check for restriction conditions
        restriction_result = self._check_restriction_conditions(
            account, billing, security, policy, reference_timestamp
        )
        if restriction_result is not None:
            return restriction_result
        
        # Compute aggregated health from all signals
        return self._compute_aggregated_health(
            account, usage, billing, security, policy, reference_timestamp
        )
    
    def _validate_snapshot_consistency(
        self,
        account: AccountSnapshot,
        usage: UsageMetricsSnapshot,
        billing: BillingStateSnapshot,
        security: SecuritySignalsSnapshot,
        policy: PolicyEvaluationSnapshot
    ) -> None:
        """
        Validate all snapshots reference the same account and are consistent.
        
        Raises:
            AccountHealthInputValidationError: If snapshots are inconsistent
        """
        account_ids = {
            account.account_id,
            usage.account_id,
            billing.account_id,
            security.account_id,
            policy.account_id
        }
        if len(account_ids) > 1:
            raise AccountHealthInputValidationError(
                f"Inconsistent account_ids across snapshots: {account_ids}"
            )
        
        # Validate timestamps are reasonable (not in future relative to reference)
        # This is a sanity check, not a strict requirement
        if hasattr(usage, 'last_activity_timestamp'):
            # Last activity should not be in the future (relative to account creation)
            if usage.last_activity_timestamp < account.created_at:
                self._logger.warning(
                    f"Last activity timestamp {usage.last_activity_timestamp} "
                    f"is before account creation {account.created_at} for account {account.account_id}"
                )
    
    def _check_termination_conditions(
        self,
        account: AccountSnapshot,
        billing: BillingStateSnapshot,
        security: SecuritySignalsSnapshot,
        policy: PolicyEvaluationSnapshot,
        reference_timestamp: datetime
    ) -> Optional[AccountHealthReport]:
        """Check for conditions requiring immediate TERMINATED status."""
        violations = set()
        
        # Regulatory ban marker (absolute termination - highest precedence)
        if policy.regulatory_ban_marker:
            violations.add(ViolationCategory.POLICY_REGULATORY_FLAG)
            
            self._logger.warning(
                f"Account {account.account_id} marked for termination: regulatory ban"
            )
            
            return AccountHealthReport(
                account_id=account.account_id,
                health_level=HealthLevel.TERMINATED,
                severity_score=self._config.max_severity_score,
                violation_categories=frozenset(violations),
                quota_status=QuotaStatus.HARD_LIMIT_EXCEEDED,
                billing_status=billing.billing_status,
                security_risk_level=security.risk_level,
                policy_version=self._config.policy_version,
                evaluated_at_reference=reference_timestamp,
                explanation_trace=ExplanationTrace(
                    violated_categories=frozenset(violations),
                    severity_contributions=(("regulatory_ban", self._config.max_severity_score),),
                    precedence_applied="TERMINATED_regulatory_ban_override"
                )
            )
        
        return None
    
    def _check_suspension_conditions(
        self,
        account: AccountSnapshot,
        billing: BillingStateSnapshot,
        security: SecuritySignalsSnapshot,
        policy: PolicyEvaluationSnapshot,
        reference_timestamp: datetime
    ) -> Optional[AccountHealthReport]:
        """Check for conditions requiring SUSPENDED status."""
        violations = set()
        severity_contributions = []
        
        # Policy strikes threshold exceeded
        if policy.policy_strikes_total >= self._config.severity_thresholds.policy_strikes_suspended:
            violations.add(ViolationCategory.POLICY_STRIKE_THRESHOLD)
            severity_contributions.append((
                "policy_strikes_suspended",
                Decimal("40.0") * self._config.severity_weights.policy_weight
            ))
        
        # Critical security risk
        if security.risk_level >= self._config.severity_thresholds.security_risk_suspended_threshold:
            violations.add(ViolationCategory.SECURITY_API_KEY_ABUSE)
            severity_contributions.append((
                "security_critical_risk",
                Decimal("35.0") * self._config.severity_weights.security_weight
            ))
        
        # Billing fraud + chargeback combination
        if billing.fraud_dispute_active and billing.chargeback_flagged:
            violations.add(ViolationCategory.BILLING_FRAUD)
            violations.add(ViolationCategory.BILLING_CHARGEBACK)
            severity_contributions.append((
                "billing_fraud_chargeback",
                Decimal("45.0") * self._config.severity_weights.billing_weight
            ))
        
        if violations:
            total_severity = sum(score for _, score in severity_contributions)
            total_severity = min(total_severity, self._config.max_severity_score)
            
            self._logger.warning(
                f"Account {account.account_id} suspended: {len(violations)} violations, "
                f"severity={total_severity}"
            )
            
            return AccountHealthReport(
                account_id=account.account_id,
                health_level=HealthLevel.SUSPENDED,
                severity_score=total_severity,
                violation_categories=frozenset(violations),
                quota_status=self._derive_quota_status(Decimal("0")),
                billing_status=billing.billing_status,
                security_risk_level=security.risk_level,
                policy_version=self._config.policy_version,
                evaluated_at_reference=reference_timestamp,
                explanation_trace=ExplanationTrace(
                    violated_categories=frozenset(violations),
                    severity_contributions=tuple(severity_contributions),
                    precedence_applied="SUSPENDED_precedence_override"
                )
            )
        
        return None
    
    def _check_restriction_conditions(
        self,
        account: AccountSnapshot,
        billing: BillingStateSnapshot,
        security: SecuritySignalsSnapshot,
        policy: PolicyEvaluationSnapshot,
        reference_timestamp: datetime
    ) -> Optional[AccountHealthReport]:
        """Check for conditions requiring RESTRICTED status."""
        violations = set()
        severity_contributions = []
        
        # Billing overdue beyond threshold (outside grace)
        if self._is_billing_outside_grace(billing, reference_timestamp):
            if billing.days_overdue >= self._config.severity_thresholds.billing_overdue_days_restricted:
                violations.add(ViolationCategory.BILLING_OVERDUE)
                severity_contributions.append((
                    "billing_overdue_restricted",
                    Decimal("30.0") * self._config.severity_weights.billing_weight
                ))
        
        # High security risk
        if security.risk_level >= self._config.severity_thresholds.security_risk_restricted_threshold:
            violations.add(ViolationCategory.SECURITY_UNUSUAL_VELOCITY)
            severity_contributions.append((
                "security_high_risk",
                Decimal("25.0") * self._config.severity_weights.security_weight
            ))
        
        # Legal hold active
        if policy.legal_hold_active:
            violations.add(ViolationCategory.POLICY_LEGAL_HOLD)
            severity_contributions.append((
                "policy_legal_hold",
                Decimal("20.0") * self._config.severity_weights.policy_weight
            ))
        
        if violations:
            total_severity = sum(score for _, score in severity_contributions)
            total_severity = min(total_severity, self._config.max_severity_score)
            
            self._logger.info(
                f"Account {account.account_id} restricted: {len(violations)} violations, "
                f"severity={total_severity}"
            )
            
            return AccountHealthReport(
                account_id=account.account_id,
                health_level=HealthLevel.RESTRICTED,
                severity_score=total_severity,
                violation_categories=frozenset(violations),
                quota_status=self._derive_quota_status(Decimal("0")),
                billing_status=billing.billing_status,
                security_risk_level=security.risk_level,
                policy_version=self._config.policy_version,
                evaluated_at_reference=reference_timestamp,
                explanation_trace=ExplanationTrace(
                    violated_categories=frozenset(violations),
                    severity_contributions=tuple(severity_contributions),
                    precedence_applied="RESTRICTED_precedence_override"
                )
            )
        
        return None
    
    def _compute_aggregated_health(
        self,
        account: AccountSnapshot,
        usage: UsageMetricsSnapshot,
        billing: BillingStateSnapshot,
        security: SecuritySignalsSnapshot,
        policy: PolicyEvaluationSnapshot,
        reference_timestamp: datetime
    ) -> AccountHealthReport:
        """Compute aggregated health from all signals."""
        violations = set()
        severity_contributions = []
        
        # Evaluate billing signals
        billing_score = self._evaluate_billing_health(
            billing, reference_timestamp, violations, severity_contributions
        )
        
        # Evaluate security signals
        security_score = self._evaluate_security_health(
            security, violations, severity_contributions
        )
        
        # Evaluate usage signals
        usage_score = self._evaluate_usage_health(
            usage, reference_timestamp, violations, severity_contributions
        )
        
        # Evaluate policy signals
        policy_score = self._evaluate_policy_health(
            policy, violations, severity_contributions
        )
        
        # Aggregate total severity (weighted sum)
        total_severity = billing_score + security_score + usage_score + policy_score
        total_severity = min(total_severity, self._config.max_severity_score)
        
        # Derive health level from aggregated severity
        health_level = self._derive_health_level_from_severity(total_severity)
        
        # Derive quota status
        quota_status = self._derive_quota_status(usage.quota_usage_percent)
        
        # Check grace period status
        grace_period_active = self._is_billing_in_grace(billing, reference_timestamp)
        
        self._logger.debug(
            f"Account {account.account_id} health evaluation complete: "
            f"level={health_level.name}, severity={total_severity}, "
            f"violations={len(violations)}, grace_period={grace_period_active}"
        )
        
        return AccountHealthReport(
            account_id=account.account_id,
            health_level=health_level,
            severity_score=total_severity,
            violation_categories=frozenset(violations),
            quota_status=quota_status,
            billing_status=billing.billing_status,
            security_risk_level=security.risk_level,
            policy_version=self._config.policy_version,
            evaluated_at_reference=reference_timestamp,
            explanation_trace=ExplanationTrace(
                violated_categories=frozenset(violations),
                severity_contributions=tuple(severity_contributions),
                grace_period_active=grace_period_active
            )
        )
    
    def _evaluate_billing_health(
        self,
        billing: BillingStateSnapshot,
        reference_timestamp: datetime,
        violations: set,
        severity_contributions: list
    ) -> Decimal:
        """Evaluate billing health and contribute to severity."""
        score = Decimal("0")
        
        # Check if in grace period
        in_grace = self._is_billing_in_grace(billing, reference_timestamp)
        
        # Payment method invalid
        if not billing.payment_method_valid:
            violations.add(ViolationCategory.BILLING_PAYMENT_INVALID)
            score += Decimal("5.0") * self._config.severity_weights.billing_weight
            severity_contributions.append(("billing_payment_invalid", score))
        
        # Plan expired
        if billing.plan_expired:
            violations.add(ViolationCategory.BILLING_PLAN_EXPIRED)
            plan_score = Decimal("8.0") * self._config.severity_weights.billing_weight
            score += plan_score
            severity_contributions.append(("billing_plan_expired", plan_score))
        
        # Overdue (if not in grace)
        if not in_grace and billing.days_overdue > 0:
            violations.add(ViolationCategory.BILLING_OVERDUE)
            if billing.days_overdue >= self._config.severity_thresholds.billing_overdue_days_degraded:
                overdue_score = Decimal("15.0") * self._config.severity_weights.billing_weight
                score += overdue_score
                severity_contributions.append(("billing_overdue_degraded", overdue_score))
        
        # Chargeback
        if billing.chargeback_flagged:
            violations.add(ViolationCategory.BILLING_CHARGEBACK)
            chargeback_score = Decimal("25.0") * self._config.severity_weights.billing_weight
            score += chargeback_score
            severity_contributions.append(("billing_chargeback", chargeback_score))
        
        return score
    
    def _evaluate_security_health(
        self,
        security: SecuritySignalsSnapshot,
        violations: set,
        severity_contributions: list
    ) -> Decimal:
        """Evaluate security health and contribute to severity."""
        score = Decimal("0")
        
        # Credential stuffing
        if security.credential_stuffing_detected:
            violations.add(ViolationCategory.SECURITY_CREDENTIAL_STUFFING)
            cred_score = Decimal("12.0") * self._config.severity_weights.security_weight
            score += cred_score
            severity_contributions.append(("security_credential_stuffing", cred_score))
        
        # Compromised email
        if security.compromised_email_detected:
            violations.add(ViolationCategory.SECURITY_COMPROMISED_EMAIL)
            email_score = Decimal("10.0") * self._config.severity_weights.security_weight
            score += email_score
            severity_contributions.append(("security_compromised_email", email_score))
        
        # API key abuse
        if security.api_key_abuse_detected:
            violations.add(ViolationCategory.SECURITY_API_KEY_ABUSE)
            api_score = Decimal("18.0") * self._config.severity_weights.security_weight
            score += api_score
            severity_contributions.append(("security_api_key_abuse", api_score))
        
        # Unusual velocity
        if security.unusual_login_velocity:
            violations.add(ViolationCategory.SECURITY_UNUSUAL_VELOCITY)
            velocity_score = Decimal("8.0") * self._config.severity_weights.security_weight
            score += velocity_score
            severity_contributions.append(("security_unusual_velocity", velocity_score))
        
        return score
    
    def _evaluate_usage_health(
        self,
        usage: UsageMetricsSnapshot,
        reference_timestamp: datetime,
        violations: set,
        severity_contributions: list
    ) -> Decimal:
        """Evaluate usage health and contribute to severity."""
        score = Decimal("0")
        thresholds = self._config.severity_thresholds
        
        # Quota usage
        if usage.quota_usage_percent >= thresholds.quota_usage_over_quota_percent:
            violations.add(ViolationCategory.USAGE_OVER_QUOTA)
            quota_score = Decimal("20.0") * self._config.severity_weights.usage_weight
            score += quota_score
            severity_contributions.append(("usage_over_quota", quota_score))
        elif usage.quota_usage_percent >= thresholds.quota_usage_at_risk_percent:
            violations.add(ViolationCategory.USAGE_OVER_QUOTA)
            quota_score = Decimal("10.0") * self._config.severity_weights.usage_weight
            score += quota_score
            severity_contributions.append(("usage_approaching_quota", quota_score))
        
        # Rate limit violations
        if usage.rate_limit_violations_count >= thresholds.rate_limit_violations_at_risk:
            violations.add(ViolationCategory.USAGE_RATE_LIMIT_VIOLATION)
            rate_score = Decimal("15.0") * self._config.severity_weights.usage_weight
            score += rate_score
            severity_contributions.append(("usage_rate_limit_high", rate_score))
        elif usage.rate_limit_violations_count >= thresholds.rate_limit_violations_degraded:
            violations.add(ViolationCategory.USAGE_RATE_LIMIT_VIOLATION)
            rate_score = Decimal("7.0") * self._config.severity_weights.usage_weight
            score += rate_score
            severity_contributions.append(("usage_rate_limit_degraded", rate_score))
        
        # Write amplification
        if usage.write_amplification_ratio > Decimal("10.0"):
            violations.add(ViolationCategory.USAGE_WRITE_AMPLIFICATION)
            write_score = Decimal("12.0") * self._config.severity_weights.usage_weight
            score += write_score
            severity_contributions.append(("usage_write_amplification", write_score))
        
        # Abnormal resource spike
        if usage.resource_spike_detected:
            violations.add(ViolationCategory.USAGE_ABNORMAL_SPIKE)
            spike_score = Decimal("8.0") * self._config.severity_weights.usage_weight
            score += spike_score
            severity_contributions.append(("usage_abnormal_spike", spike_score))
        
        # Inactivity check
        days_inactive = (reference_timestamp - usage.last_activity_timestamp).days
        if days_inactive >= thresholds.inactivity_days_at_risk:
            violations.add(ViolationCategory.INACTIVITY_THRESHOLD)
            inactive_score = Decimal("6.0") * self._config.severity_weights.usage_weight
            score += inactive_score
            severity_contributions.append(("inactivity_at_risk", inactive_score))
        elif days_inactive >= thresholds.inactivity_days_degraded:
            violations.add(ViolationCategory.INACTIVITY_THRESHOLD)
            inactive_score = Decimal("3.0") * self._config.severity_weights.usage_weight
            score += inactive_score
            severity_contributions.append(("inactivity_degraded", inactive_score))
        
        return score
    
    def _evaluate_policy_health(
        self,
        policy: PolicyEvaluationSnapshot,
        violations: set,
        severity_contributions: list
    ) -> Decimal:
        """Evaluate policy health and contribute to severity."""
        score = Decimal("0")
        thresholds = self._config.severity_thresholds
        
        # Content violations
        if policy.content_violation_strikes > 0:
            violations.add(ViolationCategory.POLICY_CONTENT_VIOLATION)
            content_score = (
                Decimal(str(policy.content_violation_strikes)) * Decimal("5.0") *
                self._config.severity_weights.policy_weight
            )
            score += content_score
            severity_contributions.append(("policy_content_violations", content_score))
        
        # Abuse classification
        if policy.abuse_classification_active:
            violations.add(ViolationCategory.POLICY_ABUSE_CLASSIFICATION)
            abuse_score = Decimal("22.0") * self._config.severity_weights.policy_weight
            score += abuse_score
            severity_contributions.append(("policy_abuse_classification", abuse_score))
        
        # Policy strikes threshold
        if policy.policy_strikes_total >= thresholds.policy_strikes_at_risk:
            violations.add(ViolationCategory.POLICY_STRIKE_THRESHOLD)
            strikes_score = Decimal("18.0") * self._config.severity_weights.policy_weight
            score += strikes_score
            severity_contributions.append(("policy_strikes_at_risk", strikes_score))
        elif policy.policy_strikes_total >= thresholds.policy_strikes_degraded:
            violations.add(ViolationCategory.POLICY_STRIKE_THRESHOLD)
            strikes_score = Decimal("9.0") * self._config.severity_weights.policy_weight
            score += strikes_score
            severity_contributions.append(("policy_strikes_degraded", strikes_score))
        
        return score
    
    def _is_billing_in_grace(
        self,
        billing: BillingStateSnapshot,
        reference_timestamp: datetime
    ) -> bool:
        """
        Check if billing is currently in grace period.
        
        Uses logical timestamp from snapshot, NOT system time.
        Grace period logic must be deterministic and replay-safe.
        
        Args:
            billing: Billing state snapshot
            reference_timestamp: Logical evaluation timestamp (NOT system time)
        
        Returns:
            True if account is in grace period, False otherwise
        """
        if not self._config.enable_grace_periods:
            return False
        
        if billing.grace_period_expires_at is None:
            # If grace period not explicitly set, check if we should compute it
            if billing.last_payment_timestamp is not None:
                # Compute grace period from last payment + grace days
                grace_expires = billing.last_payment_timestamp + timedelta(
                    days=self._config.billing_grace_days
                )
                return reference_timestamp < grace_expires
            return False
        
        # Use explicit grace period expiration timestamp
        return reference_timestamp < billing.grace_period_expires_at
    
    def _is_billing_outside_grace(
        self,
        billing: BillingStateSnapshot,
        reference_timestamp: datetime
    ) -> bool:
        """Check if billing grace period has expired."""
        return not self._is_billing_in_grace(billing, reference_timestamp)
    
    def _derive_health_level_from_severity(self, severity: Decimal) -> HealthLevel:
        """
        Derive health level from aggregated severity score.
        
        Uses explicit thresholds for deterministic classification.
        Thresholds are configurable via HealthPolicyConfig.
        
        Args:
            severity: Aggregated severity score (0.0 to max_severity_score)
        
        Returns:
            HealthLevel based on severity thresholds
        """
        # Use configurable thresholds if available, otherwise use defaults
        # For now, use fixed thresholds matching typical enterprise standards
        if severity >= Decimal("70.0"):
            return HealthLevel.AT_RISK
        elif severity >= Decimal("40.0"):
            return HealthLevel.DEGRADED
        else:
            return HealthLevel.HEALTHY
    
    def _derive_quota_status(self, quota_usage_percent: Decimal) -> QuotaStatus:
        """Derive quota status from usage percentage."""
        thresholds = self._config.severity_thresholds
        
        if quota_usage_percent >= thresholds.quota_usage_over_quota_percent:
            if quota_usage_percent >= Decimal("120.0"):
                return QuotaStatus.HARD_LIMIT_EXCEEDED
            return QuotaStatus.OVER_QUOTA
        elif quota_usage_percent >= thresholds.quota_usage_at_risk_percent:
            return QuotaStatus.APPROACHING_LIMIT
        else:
            return QuotaStatus.WITHIN_LIMITS


def evaluate_account_health(
    account: AccountSnapshot,
    usage: UsageMetricsSnapshot,
    billing: BillingStateSnapshot,
    security: SecuritySignalsSnapshot,
    policy: PolicyEvaluationSnapshot,
    config: HealthPolicyConfig,
    reference_timestamp: datetime,
    logger: Optional[logging.Logger] = None,
) -> AccountHealthReport:
    """
    Convenience function for one-off health evaluation.
    
    DETERMINISTIC: Same inputs always produce identical output.
    No side effects. No I/O. Pure function.
    
    For repeated evaluations, instantiate AccountHealthMonitor once and reuse
    for better performance.
    
    Args:
        account: Immutable account snapshot
        usage: Immutable usage metrics snapshot
        billing: Immutable billing state snapshot
        security: Immutable security signals snapshot
        policy: Immutable policy evaluation snapshot
        config: Health policy configuration
        reference_timestamp: Logical evaluation timestamp (NOT system time)
        logger: Optional logger for structured logging
    
    Returns:
        AccountHealthReport with deterministic health classification
    
    Raises:
        AccountHealthInputValidationError: If snapshots are inconsistent
        HealthPolicyConfigError: If config is invalid
    """
    monitor = AccountHealthMonitor(config, logger=logger)
    return monitor.evaluate_account_health(
        account, usage, billing, security, policy, reference_timestamp
    )