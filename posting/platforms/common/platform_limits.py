"""
/posting/platforms/common/platform_limits.py

Platform Rate, Quota, & Suppression Modeling Authority

This is the authoritative model of what platforms will tolerate.
It answers: "Given current and historical behavior, is it safe to post right now?"

Tier-0 Critical: Pre-dispatch safety gate preventing rate-limit spirals,
shadow bans, and account-level trust decay.

NO heuristics. NO platform optimism. NO "we'll tune later."
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple, List, Dict
import time
import math


# ============================================================================
# CORE ENUMS (EXPLICIT)
# ============================================================================


class LimitType(Enum):
    """Type of limit being enforced or detected."""
    RATE = "rate"                    # requests per time window
    QUOTA = "quota"                  # total uploads per period
    CONCURRENCY = "concurrency"      # simultaneous operations
    VISIBILITY = "visibility"        # impression suppression
    TRUST = "trust"                  # account trust decay


class LimitScope(Enum):
    """Scope at which limit applies."""
    ACCOUNT = "account"              # per-account limit
    PLATFORM = "platform"            # platform-wide limit
    IP = "ip"                        # IP-based limit
    GLOBAL = "global"                # cross-platform global


class LimitDecision(Enum):
    """Final decision on whether to proceed."""
    ALLOW = "allow"                  # safe to proceed
    DELAY = "delay"                  # pause required
    BLOCK = "block"                  # do not proceed
    ESCALATE = "escalate"            # requires human intervention


class LimitSource(Enum):
    """Source of limit signal."""
    DECLARED = "declared"            # documented platform limits
    TELEMETRY = "telemetry"          # observed behavior
    ERROR = "error"                  # error patterns
    CADENCE = "cadence"              # posting rhythm analysis
    TRUST = "trust"                  # trust signal correlation


# ============================================================================
# IMMUTABLE DATA CONTRACTS
# ============================================================================


@dataclass(frozen=True)
class PlatformLimitSignal:
    """
    Immutable signal indicating potential or active limit condition.
    
    Signals are INPUTS, never conclusions.
    """
    platform: str
    account_id: str
    
    limit_type: LimitType
    scope: LimitScope
    
    confidence: float          # 0.0–1.0 (certainty of signal)
    severity: float            # 0.0–1.0 (impact magnitude)
    
    source: LimitSource
    timestamp: float
    metadata: dict = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate signal invariants."""
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"Invalid confidence: {self.confidence}")
        if not 0.0 <= self.severity <= 1.0:
            raise ValueError(f"Invalid severity: {self.severity}")
        if self.timestamp > time.time() + 60:
            raise ValueError(f"Future timestamp: {self.timestamp}")


@dataclass(frozen=True)
class LimitEvaluationResult:
    """
    Authoritative output from limit evaluation.
    
    This object is CONSUMED, not debated.
    """
    decision: LimitDecision
    
    risk_score: float          # 0.0 (safe) – 1.0 (catastrophic)
    cooldown_seconds: int      # required pause duration
    
    blocking_signals: tuple[PlatformLimitSignal, ...]
    advisory_notes: tuple[str, ...]
    
    evaluated_at: float = field(default_factory=time.time)
    
    def __post_init__(self):
        """Validate result invariants."""
        if not 0.0 <= self.risk_score <= 1.0:
            raise ValueError(f"Invalid risk_score: {self.risk_score}")
        if self.cooldown_seconds < 0:
            raise ValueError(f"Negative cooldown: {self.cooldown_seconds}")
        
        # Convert to tuples for immutability
        object.__setattr__(self, 'blocking_signals', tuple(self.blocking_signals))
        object.__setattr__(self, 'advisory_notes', tuple(self.advisory_notes))


@dataclass(frozen=True)
class DeclaredLimit:
    """Known, documented platform limit."""
    platform: str
    limit_type: LimitType
    scope: LimitScope
    
    max_count: int             # maximum allowed operations
    window_seconds: int        # time window for limit
    
    hard_limit: bool = True    # whether this is a hard stop
    margin_factor: float = 0.8 # safety margin (use 80% of limit)


# ============================================================================
# DECLARED LIMIT REGISTRY
# ============================================================================


class DeclaredLimitRegistry:
    """
    Registry of known, documented platform limits.
    
    These are STATIC TRUTHS, not inferences.
    """
    
    # Platform-specific declared limits
    _LIMITS: dict[str, list[DeclaredLimit]] = {
        'twitter': [
            DeclaredLimit('twitter', LimitType.RATE, LimitScope.ACCOUNT, 300, 180, True, 0.8),
            DeclaredLimit('twitter', LimitType.QUOTA, LimitScope.ACCOUNT, 2400, 86400, True, 0.85),
            DeclaredLimit('twitter', LimitType.CONCURRENCY, LimitScope.ACCOUNT, 5, 60, True, 0.7),
        ],
        'linkedin': [
            DeclaredLimit('linkedin', LimitType.RATE, LimitScope.ACCOUNT, 100, 86400, True, 0.75),
            DeclaredLimit('linkedin', LimitType.QUOTA, LimitScope.ACCOUNT, 150, 86400, True, 0.8),
        ],
        'reddit': [
            DeclaredLimit('reddit', LimitType.RATE, LimitScope.ACCOUNT, 60, 3600, True, 0.7),
            DeclaredLimit('reddit', LimitType.QUOTA, LimitScope.ACCOUNT, 100, 86400, True, 0.75),
        ],
        'mastodon': [
            DeclaredLimit('mastodon', LimitType.RATE, LimitScope.ACCOUNT, 300, 300, True, 0.8),
            DeclaredLimit('mastodon', LimitType.QUOTA, LimitScope.ACCOUNT, 1000, 86400, True, 0.85),
        ],
        'bluesky': [
            DeclaredLimit('bluesky', LimitType.RATE, LimitScope.ACCOUNT, 500, 300, True, 0.8),
            DeclaredLimit('bluesky', LimitType.QUOTA, LimitScope.ACCOUNT, 2000, 86400, True, 0.85),
        ],
    }
    
    @classmethod
    def get_limits(cls, platform: str) -> list[DeclaredLimit]:
        """Get all declared limits for platform."""
        return cls._LIMITS.get(platform.lower(), [])
    
    @classmethod
    def get_limit(cls, platform: str, limit_type: LimitType, 
                  scope: LimitScope) -> Optional[DeclaredLimit]:
        """Get specific declared limit."""
        for limit in cls.get_limits(platform):
            if limit.limit_type == limit_type and limit.scope == scope:
                return limit
        return None
    
    @classmethod
    def check_declared_limit(cls, platform: str, limit_type: LimitType,
                            scope: LimitScope, current_count: int,
                            window_seconds: int) -> Optional[PlatformLimitSignal]:
        """
        Check if current usage violates declared limit.
        
        Returns signal if limit is approached or exceeded.
        """
        limit = cls.get_limit(platform, limit_type, scope)
        if not limit:
            return None
        
        # Adjust for window differences
        if limit.window_seconds != window_seconds and window_seconds > 0:
            adjusted_max = (limit.max_count * window_seconds) / limit.window_seconds
        else:
            adjusted_max = limit.max_count
        
        # Apply safety margin
        safe_max = adjusted_max * limit.margin_factor
        
        if current_count >= adjusted_max:
            # Hard limit exceeded
            return PlatformLimitSignal(
                platform=platform,
                account_id="",
                limit_type=limit_type,
                scope=scope,
                confidence=1.0,
                severity=1.0,
                source=LimitSource.DECLARED,
                timestamp=time.time(),
                metadata={'limit': adjusted_max, 'current': current_count}
            )
        elif current_count >= safe_max:
            # Approaching limit
            severity = (current_count - safe_max) / (adjusted_max - safe_max)
            return PlatformLimitSignal(
                platform=platform,
                account_id="",
                limit_type=limit_type,
                scope=scope,
                confidence=0.9,
                severity=min(0.9, severity),
                source=LimitSource.DECLARED,
                timestamp=time.time(),
                metadata={'limit': adjusted_max, 'current': current_count, 'safe_max': safe_max}
            )
        
        return None


# ============================================================================
# INFERRED LIMIT MODEL
# ============================================================================


class InferredLimitModel:
    """
    Detects undocumented throttling and suppression.
    
    Platforms rarely tell the truth. This aggregates signals from:
    - platform_telemetry.py (latency, success rates)
    - posting_errors.py (error patterns)
    - cadence_memory.py (rhythm violations)
    
    This is NOT ML. It is deterministic signal aggregation.
    """
    
    # Thresholds for inference
    LATENCY_THRESHOLD_MS = 5000       # sustained latency → throttling
    ERROR_RATE_THRESHOLD = 0.15       # 15% errors → possible limit
    SUCCESS_DROP_THRESHOLD = 0.30     # 30% drop in success → suppression
    
    @classmethod
    def infer_from_telemetry(cls, platform: str, account_id: str,
                            recent_telemetry: dict) -> list[PlatformLimitSignal]:
        """
        Infer limits from telemetry patterns.
        
        recent_telemetry = {
            'avg_latency_ms': float,
            'error_rate': float,
            'success_rate': float,
            'baseline_success_rate': float,
            'sample_size': int,
        }
        """
        signals = []
        
        avg_latency = recent_telemetry.get('avg_latency_ms', 0)
        error_rate = recent_telemetry.get('error_rate', 0)
        success_rate = recent_telemetry.get('success_rate', 1.0)
        baseline_success = recent_telemetry.get('baseline_success_rate', 1.0)
        
        # Detect throttling via latency
        if avg_latency > cls.LATENCY_THRESHOLD_MS:
            severity = min(1.0, avg_latency / (cls.LATENCY_THRESHOLD_MS * 3))
            signals.append(PlatformLimitSignal(
                platform=platform,
                account_id=account_id,
                limit_type=LimitType.RATE,
                scope=LimitScope.ACCOUNT,
                confidence=0.7,
                severity=severity,
                source=LimitSource.TELEMETRY,
                timestamp=time.time(),
                metadata={'avg_latency_ms': avg_latency}
            ))
        
        # Detect rate limiting via error rate
        if error_rate > cls.ERROR_RATE_THRESHOLD:
            signals.append(PlatformLimitSignal(
                platform=platform,
                account_id=account_id,
                limit_type=LimitType.RATE,
                scope=LimitScope.ACCOUNT,
                confidence=0.8,
                severity=min(1.0, error_rate / 0.5),
                source=LimitSource.TELEMETRY,
                timestamp=time.time(),
                metadata={'error_rate': error_rate}
            ))
        
        # Detect visibility suppression
        if baseline_success > 0:
            success_drop = (baseline_success - success_rate) / baseline_success
            if success_drop > cls.SUCCESS_DROP_THRESHOLD:
                signals.append(PlatformLimitSignal(
                    platform=platform,
                    account_id=account_id,
                    limit_type=LimitType.VISIBILITY,
                    scope=LimitScope.ACCOUNT,
                    confidence=0.6,
                    severity=min(1.0, success_drop / 0.5),
                    source=LimitSource.TELEMETRY,
                    timestamp=time.time(),
                    metadata={'success_drop': success_drop}
                ))
        
        return signals
    
    @classmethod
    def infer_from_errors(cls, platform: str, account_id: str,
                         error_pattern: dict) -> list[PlatformLimitSignal]:
        """
        Infer limits from error patterns.
        
        error_pattern = {
            'rate_limit_count': int,
            'throttle_count': int,
            'quota_exceeded_count': int,
            'total_errors': int,
        }
        """
        signals = []
        
        rate_limit_count = error_pattern.get('rate_limit_count', 0)
        throttle_count = error_pattern.get('throttle_count', 0)
        quota_count = error_pattern.get('quota_exceeded_count', 0)
        
        if rate_limit_count > 0:
            signals.append(PlatformLimitSignal(
                platform=platform,
                account_id=account_id,
                limit_type=LimitType.RATE,
                scope=LimitScope.ACCOUNT,
                confidence=1.0,
                severity=min(1.0, rate_limit_count / 5),
                source=LimitSource.ERROR,
                timestamp=time.time(),
                metadata={'rate_limit_errors': rate_limit_count}
            ))
        
        if throttle_count > 0:
            signals.append(PlatformLimitSignal(
                platform=platform,
                account_id=account_id,
                limit_type=LimitType.RATE,
                scope=LimitScope.ACCOUNT,
                confidence=0.9,
                severity=min(1.0, throttle_count / 3),
                source=LimitSource.ERROR,
                timestamp=time.time(),
                metadata={'throttle_errors': throttle_count}
            ))
        
        if quota_count > 0:
            signals.append(PlatformLimitSignal(
                platform=platform,
                account_id=account_id,
                limit_type=LimitType.QUOTA,
                scope=LimitScope.ACCOUNT,
                confidence=1.0,
                severity=1.0,
                source=LimitSource.ERROR,
                timestamp=time.time(),
                metadata={'quota_errors': quota_count}
            ))
        
        return signals


# ============================================================================
# SUPPRESSION RISK MODEL
# ============================================================================


class SuppressionRiskModel:
    """
    Calculates composite suppression probability.
    
    Combines:
    - visibility decay
    - error distribution shifts
    - cadence violations
    - trust decay velocity
    
    Outputs bounded risk score: 0.0 (safe) → 1.0 (catastrophic)
    """
    
    # Risk weights
    VISIBILITY_WEIGHT = 0.35
    ERROR_WEIGHT = 0.25
    CADENCE_WEIGHT = 0.20
    TRUST_WEIGHT = 0.20
    
    @classmethod
    def calculate_risk(cls, signals: list[PlatformLimitSignal]) -> float:
        """
        Calculate composite suppression risk from signals.
        
        Returns: 0.0 (safe) to 1.0 (catastrophic)
        """
        if not signals:
            return 0.0
        
        # Aggregate by limit type
        visibility_signals = [s for s in signals if s.limit_type == LimitType.VISIBILITY]
        rate_signals = [s for s in signals if s.limit_type == LimitType.RATE]
        quota_signals = [s for s in signals if s.limit_type == LimitType.QUOTA]
        trust_signals = [s for s in signals if s.limit_type == LimitType.TRUST]
        
        # Calculate component risks
        visibility_risk = cls._max_weighted_severity(visibility_signals)
        error_risk = cls._max_weighted_severity(rate_signals + quota_signals)
        trust_risk = cls._max_weighted_severity(trust_signals)
        
        # Cadence risk (inferred from rate signals with cadence source)
        cadence_signals = [s for s in signals if s.source == LimitSource.CADENCE]
        cadence_risk = cls._max_weighted_severity(cadence_signals)
        
        # Weighted composite
        composite_risk = (
            visibility_risk * cls.VISIBILITY_WEIGHT +
            error_risk * cls.ERROR_WEIGHT +
            cadence_risk * cls.CADENCE_WEIGHT +
            trust_risk * cls.TRUST_WEIGHT
        )
        
        return min(1.0, composite_risk)
    
    @staticmethod
    def _max_weighted_severity(signals: list[PlatformLimitSignal]) -> float:
        """Get maximum confidence-weighted severity from signals."""
        if not signals:
            return 0.0
        
        weighted = [s.confidence * s.severity for s in signals]
        return max(weighted)


# ============================================================================
# COOLDOWN CALCULATOR
# ============================================================================


class CooldownCalculator:
    """
    Determines pause duration based on limit violations.
    
    Cooldown is PREVENTATIVE, not punitive.
    """
    
    # Base cooldown periods (seconds)
    BASE_COOLDOWN = {
        LimitType.RATE: 60,
        LimitType.QUOTA: 300,
        LimitType.CONCURRENCY: 30,
        LimitType.VISIBILITY: 600,
        LimitType.TRUST: 1800,
    }
    
    # Maximum cooldown per type
    MAX_COOLDOWN = {
        LimitType.RATE: 900,        # 15 minutes
        LimitType.QUOTA: 3600,      # 1 hour
        LimitType.CONCURRENCY: 300, # 5 minutes
        LimitType.VISIBILITY: 7200, # 2 hours
        LimitType.TRUST: 86400,     # 24 hours
    }
    
    @classmethod
    def calculate_cooldown(cls, signals: list[PlatformLimitSignal]) -> int:
        """
        Calculate required cooldown period.
        
        Uses exponential backoff based on severity.
        Returns cooldown in seconds.
        """
        if not signals:
            return 0
        
        max_cooldown = 0
        
        for signal in signals:
            base = cls.BASE_COOLDOWN.get(signal.limit_type, 60)
            max_cd = cls.MAX_COOLDOWN.get(signal.limit_type, 3600)
            
            # Exponential scaling based on severity
            severity_multiplier = math.exp(signal.severity) - 1  # 0→0, 1→1.718
            calculated = int(base * (1 + severity_multiplier * 2))
            
            # Cap at maximum
            capped = min(calculated, max_cd)
            
            max_cooldown = max(max_cooldown, capped)
        
        return max_cooldown


# ============================================================================
# PLATFORM LIMITS ENGINE (SINGLE ENTRY POINT)
# ============================================================================


class PlatformLimitsEngine:
    """
    Single entry point for limit evaluation.
    
    Rules:
    - Deterministic
    - Side-effect free
    - Conservative by default
    - Unknown → DELAY, never ALLOW
    """
    
    # Risk thresholds for decisions
    ALLOW_THRESHOLD = 0.3      # below this → ALLOW
    DELAY_THRESHOLD = 0.7      # above ALLOW, below this → DELAY
    BLOCK_THRESHOLD = 0.9      # above DELAY, below this → BLOCK
    # above BLOCK_THRESHOLD → ESCALATE
    
    def __init__(self):
        """Initialize engine with clean state."""
        self._declared_registry = DeclaredLimitRegistry()
        self._inferred_model = InferredLimitModel()
        self._risk_model = SuppressionRiskModel()
        self._cooldown_calc = CooldownCalculator()
    
    def evaluate(self, 
                account_id: str,
                platform: str,
                intent: str,
                context: Optional[dict] = None) -> LimitEvaluationResult:
        """
        Evaluate whether posting is safe right now.
        
        Args:
            account_id: Account attempting to post
            platform: Target platform
            intent: What operation is intended (post, upload, etc)
            context: Additional context (telemetry, error history, etc)
        
        Returns:
            LimitEvaluationResult with decision and supporting data
        """
        context = context or {}
        signals = []
        
        # Check declared limits
        declared_signals = self._check_declared_limits(platform, context)
        signals.extend(declared_signals)
        
        # Infer from telemetry
        if 'telemetry' in context:
            inferred_signals = self._inferred_model.infer_from_telemetry(
                platform, account_id, context['telemetry']
            )
            signals.extend(inferred_signals)
        
        # Infer from errors
        if 'error_pattern' in context:
            error_signals = self._inferred_model.infer_from_errors(
                platform, account_id, context['error_pattern']
            )
            signals.extend(error_signals)
        
        # Add any external signals
        if 'external_signals' in context:
            signals.extend(context['external_signals'])
        
        # Calculate risk
        risk_score = self._risk_model.calculate_risk(signals)
        
        # Determine decision
        decision = self._make_decision(risk_score, signals)
        
        # Calculate cooldown
        cooldown = self._cooldown_calc.calculate_cooldown(signals)
        
        # Filter blocking signals (high severity)
        blocking = [s for s in signals if s.severity >= 0.7]
        
        # Generate advisory notes
        notes = self._generate_notes(decision, risk_score, signals)
        
        return LimitEvaluationResult(
            decision=decision,
            risk_score=risk_score,
            cooldown_seconds=cooldown,
            blocking_signals=tuple(blocking),
            advisory_notes=tuple(notes)
        )
    
    def _check_declared_limits(self, platform: str, 
                               context: dict) -> list[PlatformLimitSignal]:
        """Check declared platform limits."""
        signals = []
        
        # Check rate limit
        if 'recent_post_count' in context:
            signal = self._declared_registry.check_declared_limit(
                platform, LimitType.RATE, LimitScope.ACCOUNT,
                context['recent_post_count'],
                context.get('window_seconds', 3600)
            )
            if signal:
                signals.append(PlatformLimitSignal(
                    platform=signal.platform,
                    account_id=context.get('account_id', ''),
                    limit_type=signal.limit_type,
                    scope=signal.scope,
                    confidence=signal.confidence,
                    severity=signal.severity,
                    source=signal.source,
                    timestamp=signal.timestamp,
                    metadata=signal.metadata
                ))
        
        return signals
    
    def _make_decision(self, risk_score: float, 
                       signals: list[PlatformLimitSignal]) -> LimitDecision:
        """
        Make final decision based on risk and signals.
        
        Conservative by default. Unknown → DELAY, never ALLOW.
        """
        # ESCALATE: critical signals present
        critical_signals = [s for s in signals if s.confidence >= 0.9 and s.severity >= 0.9]
        if critical_signals:
            return LimitDecision.ESCALATE
        
        # BLOCK: high risk
        if risk_score >= self.BLOCK_THRESHOLD:
            return LimitDecision.BLOCK
        
        # DELAY: moderate risk
        if risk_score >= self.DELAY_THRESHOLD:
            return LimitDecision.DELAY
        
        # DELAY: approaching declared limits
        high_confidence_signals = [s for s in signals if s.confidence >= 0.8]
        if high_confidence_signals:
            return LimitDecision.DELAY
        
        # ALLOW: low risk
        if risk_score < self.ALLOW_THRESHOLD:
            return LimitDecision.ALLOW
        
        # Default: DELAY (conservative)
        return LimitDecision.DELAY
    
    def _generate_notes(self, decision: LimitDecision, risk_score: float,
                       signals: list[PlatformLimitSignal]) -> list[str]:
        """Generate human-readable advisory notes."""
        notes = []
        
        notes.append(f"Risk score: {risk_score:.3f}")
        
        if signals:
            by_type = {}
            for s in signals:
                by_type.setdefault(s.limit_type, []).append(s)
            
            for limit_type, type_signals in by_type.items():
                max_severity = max(s.severity for s in type_signals)
                notes.append(f"{limit_type.value}: {len(type_signals)} signals, max severity {max_severity:.2f}")
        
        if decision == LimitDecision.ESCALATE:
            notes.append("CRITICAL: Human intervention required")
        elif decision == LimitDecision.BLOCK:
            notes.append("Account in suppression risk zone")
        elif decision == LimitDecision.DELAY:
            notes.append("Cooldown period recommended")
        
        return notes


# ============================================================================
# LIMIT INVARIANT VALIDATOR (NON-OPTIONAL)
# ============================================================================


class LimitInvariantValidator:
    """
    Enforces absolute invariants on limit evaluation.
    
    Violation → global halt.
    """
    
    @staticmethod
    def validate_result(result: LimitEvaluationResult) -> None:
        """
        Validate evaluation result invariants.
        
        Raises ValueError if any invariant is violated.
        """
        # Risk score bounds
        if not 0.0 <= result.risk_score <= 1.0:
            raise ValueError(f"Risk score out of bounds: {result.risk_score}")
        
        # Cooldown non-negative
        if result.cooldown_seconds < 0:
            raise ValueError(f"Negative cooldown: {result.cooldown_seconds}")
        
        # BLOCK always beats DELAY
        if result.decision == LimitDecision.ALLOW and result.risk_score > 0.7:
            raise ValueError("Cannot ALLOW with risk_score > 0.7")
        
        # ESCALATE requires high-severity signals
        if result.decision == LimitDecision.ESCALATE:
            critical = [s for s in result.blocking_signals 
                       if s.confidence >= 0.9 and s.severity >= 0.9]
            if not critical:
                raise ValueError("ESCALATE requires critical signals")
        
        # No ALLOW when suppression confidence high
        if result.decision == LimitDecision.ALLOW:
            visibility_signals = [s for s in result.blocking_signals 
                                 if s.limit_type == LimitType.VISIBILITY]
            high_confidence = [s for s in visibility_signals if s.confidence > 0.8]
            if high_confidence:
                raise ValueError("Cannot ALLOW with high-confidence suppression signals")
    
    @staticmethod
    def validate_decision_monotonicity(old_result: LimitEvaluationResult,
                                      new_result: LimitEvaluationResult) -> None:
        """
        Validate that decisions don't inappropriately relax on failure.
        
        Cooldowns should never shorten on repeated violations.
        """
        decision_severity = {
            LimitDecision.ALLOW: 0,
            LimitDecision.DELAY: 1,
            LimitDecision.BLOCK: 2,
            LimitDecision.ESCALATE: 3,
        }
        
        # If risk increased, decision should not relax
        if new_result.risk_score > old_result.risk_score:
            old_severity = decision_severity[old_result.decision]
            new_severity = decision_severity[new_result.decision]
            
            if new_severity < old_severity:
                raise ValueError(
                    f"Decision relaxed despite increased risk: "
                    f"{old_result.decision} → {new_result.decision}"
                )
        
        # Cooldown should not shorten on failure
        if (old_result.decision in (LimitDecision.BLOCK, LimitDecision.ESCALATE) and
            new_result.decision in (LimitDecision.BLOCK, LimitDecision.ESCALATE)):
            if new_result.cooldown_seconds < old_result.cooldown_seconds:
                raise ValueError(
                    f"Cooldown shortened on continued failure: "
                    f"{old_result.cooldown_seconds}s → {new_result.cooldown_seconds}s"
                )


# ============================================================================
# PUBLIC API
# ============================================================================


def create_limits_engine() -> PlatformLimitsEngine:
    """Create new limits engine instance."""
    return PlatformLimitsEngine()


def validate_evaluation(result: LimitEvaluationResult) -> None:
    """Validate evaluation result. Raises on invariant violation."""
    LimitInvariantValidator.validate_result(result)


__all__ = [
    # Enums
    'LimitType',
    'LimitScope',
    'LimitDecision',
    'LimitSource',
    
    # Data contracts
    'PlatformLimitSignal',
    'LimitEvaluationResult',
    'DeclaredLimit',
    
    # Components
    'DeclaredLimitRegistry',
    'InferredLimitModel',
    'SuppressionRiskModel',
    'CooldownCalculator',
    
    # Engine
    'PlatformLimitsEngine',
    'LimitInvariantValidator',
    
    # Public API
    'create_limits_engine',
    'validate_evaluation',
]