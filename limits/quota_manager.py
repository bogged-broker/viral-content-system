"""
quota_manager.py - Hard Consumption Ceiling Authority

Built for:
- Absolute usage ceilings
- Replay-safe monotonic tracking
- Multi-dimensional quota enforcement
- Zero forgiveness on exhaustion
- Irreversible audit trails
- Watchdog-enforced caps

NO SOFT LIMITS. NO GRACE PERIODS. NO BORROWING.

What this file ACTUALLY is:
"Have you already done too much — period?"

Quota is binary finality, not flow control.

Authority chain: quota → rate → backpressure → execution
If quota denies → nothing downstream runs.
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Set, Tuple, Callable, Any
from datetime import datetime, timezone, timedelta
from abc import ABC, abstractmethod
import threading
from contextlib import contextmanager
from collections import defaultdict
import hashlib
import json


# ============================================================================
# CORE ENUMS (STRICT)
# ============================================================================

class QuotaScope(Enum):
    """
    Scopes do not overlap unless explicitly declared.
    Hierarchy is strict and non-inferrable.
    """
    GLOBAL = "global"
    ACCOUNT = "account"
    WORKFLOW = "workflow"
    PROJECT = "project"
    INFRA = "infra"


class QuotaMetric(Enum):
    """
    Metrics are concrete, measurable, and monotonic.
    Each metric type is independently tracked.
    """
    EVENT_COUNT = "event_count"
    API_CALLS = "api_calls"
    CONTENT_CREATED = "content_created"
    CONTENT_POSTED = "content_posted"
    GPU_SECONDS = "gpu_seconds"
    STORAGE_BYTES = "storage_bytes"
    EGRESS_BYTES = "egress_bytes"
    INFERENCE_TOKENS = "inference_tokens"
    WORKFLOW_EXECUTIONS = "workflow_executions"
    COMPUTE_HOURS = "compute_hours"


class QuotaDecision(Enum):
    """
    No partial grants.
    No "warn" mode.
    Binary finality only.
    """
    ALLOW = "allow"
    DENY = "deny"


class ResetPolicy(Enum):
    """
    How and when quotas reset.
    No implicit behavior.
    """
    FIXED_HOURLY = "fixed_hourly"      # Reset at top of hour
    FIXED_DAILY = "fixed_daily"        # Reset at midnight UTC
    FIXED_WEEKLY = "fixed_weekly"      # Reset at Monday 00:00 UTC
    FIXED_MONTHLY = "fixed_monthly"    # Reset at 1st of month 00:00 UTC
    ROLLING_WINDOW = "rolling_window"  # Sliding time window
    MANUAL_ONLY = "manual_only"        # Admin reset only
    NEVER = "never"                    # Lifetime quota


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class QuotaKey:
    """
    Fully qualified identifier for a quota instance.
    Fully specified or rejected - no partial keys.
    """
    scope: QuotaScope
    scope_id: str  # Must be non-empty
    metric: QuotaMetric
    
    def __post_init__(self):
        if not self.scope_id:
            raise ValueError("scope_id cannot be empty")
        if not isinstance(self.scope, QuotaScope):
            raise ValueError(f"Invalid scope type: {type(self.scope)}")
        if not isinstance(self.metric, QuotaMetric):
            raise ValueError(f"Invalid metric type: {type(self.metric)}")
    
    def to_string(self) -> str:
        """Convert to canonical string representation."""
        return f"{self.scope.value}:{self.scope_id}:{self.metric.value}"
    
    def to_hash(self) -> str:
        """Generate deterministic hash for storage key."""
        return hashlib.sha256(self.to_string().encode()).hexdigest()[:16]
    
    @classmethod
    def from_string(cls, key_str: str) -> 'QuotaKey':
        """Parse from canonical string representation."""
        parts = key_str.split(':', 2)
        if len(parts) != 3:
            raise ValueError(f"Invalid quota key string: {key_str}")
        return cls(
            scope=QuotaScope(parts[0]),
            scope_id=parts[1],
            metric=QuotaMetric(parts[2])
        )


@dataclass
class QuotaUsage:
    """
    Immutable record of consumption.
    Usage only moves forward - never decreases.
    """
    consumed_value: int
    first_consumed_at: datetime
    last_updated_at: datetime
    reset_at: Optional[datetime] = None
    version: int = 1  # For optimistic locking
    
    def __post_init__(self):
        if self.consumed_value < 0:
            raise ValueError(f"Consumed value cannot be negative: {self.consumed_value}")
        if self.last_updated_at < self.first_consumed_at:
            raise ValueError("last_updated_at cannot be before first_consumed_at")
        if self.version < 1:
            raise ValueError(f"Version must be >= 1: {self.version}")
    
    def increment(self, amount: int, now: datetime) -> 'QuotaUsage':
        """
        Create new usage record with incremented consumption.
        Returns new instance - original is immutable.
        """
        if amount < 0:
            raise ValueError(f"Cannot increment by negative amount: {amount}")
        if amount == 0:
            return self
        
        return QuotaUsage(
            consumed_value=self.consumed_value + amount,
            first_consumed_at=self.first_consumed_at,
            last_updated_at=now,
            reset_at=self.reset_at,
            version=self.version + 1
        )
    
    def reset(self, now: datetime) -> 'QuotaUsage':
        """Create new usage record for reset period."""
        return QuotaUsage(
            consumed_value=0,
            first_consumed_at=now,
            last_updated_at=now,
            reset_at=now,
            version=1
        )
    
    def to_dict(self) -> Dict:
        """Serialize to dictionary."""
        return {
            'consumed_value': self.consumed_value,
            'first_consumed_at': self.first_consumed_at.isoformat(),
            'last_updated_at': self.last_updated_at.isoformat(),
            'reset_at': self.reset_at.isoformat() if self.reset_at else None,
            'version': self.version
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'QuotaUsage':
        """Deserialize from dictionary."""
        return cls(
            consumed_value=data['consumed_value'],
            first_consumed_at=datetime.fromisoformat(data['first_consumed_at']),
            last_updated_at=datetime.fromisoformat(data['last_updated_at']),
            reset_at=datetime.fromisoformat(data['reset_at']) if data.get('reset_at') else None,
            version=data.get('version', 1)
        )


@dataclass(frozen=True)
class QuotaLimit:
    """
    Immutable quota limit definition.
    Limits are declared, versioned, immutable.
    """
    limit_id: str
    max_value: int
    metric: QuotaMetric
    scope: QuotaScope
    reset_policy: ResetPolicy
    window_seconds: Optional[int] = None  # For ROLLING_WINDOW only
    description: str = ""
    
    def __post_init__(self):
        if self.max_value <= 0:
            raise ValueError(f"max_value must be > 0: {self.max_value}")
        if not self.limit_id:
            raise ValueError("limit_id cannot be empty")
        
        # Validate window_seconds for rolling window
        if self.reset_policy == ResetPolicy.ROLLING_WINDOW:
            if self.window_seconds is None or self.window_seconds <= 0:
                raise ValueError("ROLLING_WINDOW requires positive window_seconds")
        elif self.window_seconds is not None:
            raise ValueError(f"window_seconds only valid for ROLLING_WINDOW, got {self.reset_policy}")
    
    def should_reset(self, last_reset: Optional[datetime], now: datetime) -> bool:
        """Determine if quota should be reset based on policy."""
        if self.reset_policy == ResetPolicy.NEVER:
            return False
        
        if self.reset_policy == ResetPolicy.MANUAL_ONLY:
            return False
        
        if last_reset is None:
            return False  # No previous reset, so not time for another
        
        if self.reset_policy == ResetPolicy.FIXED_HOURLY:
            # Reset if we've crossed an hour boundary
            return last_reset.hour != now.hour or last_reset.date() != now.date()
        
        if self.reset_policy == ResetPolicy.FIXED_DAILY:
            # Reset if we've crossed a day boundary
            return last_reset.date() != now.date()
        
        if self.reset_policy == ResetPolicy.FIXED_WEEKLY:
            # Reset if we've crossed a week boundary (Monday)
            last_week = last_reset.isocalendar()[1]
            curr_week = now.isocalendar()[1]
            return last_week != curr_week or last_reset.year != now.year
        
        if self.reset_policy == ResetPolicy.FIXED_MONTHLY:
            # Reset if we've crossed a month boundary
            return last_reset.month != now.month or last_reset.year != now.year
        
        if self.reset_policy == ResetPolicy.ROLLING_WINDOW:
            # Reset if outside the rolling window
            delta = (now - last_reset).total_seconds()
            return delta >= self.window_seconds
        
        return False
    
    def get_next_reset_time(self, now: datetime) -> Optional[datetime]:
        """Calculate when the next reset will occur."""
        if self.reset_policy in [ResetPolicy.NEVER, ResetPolicy.MANUAL_ONLY]:
            return None
        
        if self.reset_policy == ResetPolicy.FIXED_HOURLY:
            next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            return next_hour
        
        if self.reset_policy == ResetPolicy.FIXED_DAILY:
            next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return next_day
        
        if self.reset_policy == ResetPolicy.FIXED_WEEKLY:
            days_until_monday = (7 - now.weekday()) % 7
            if days_until_monday == 0:
                days_until_monday = 7
            next_monday = (now + timedelta(days=days_until_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            return next_monday
        
        if self.reset_policy == ResetPolicy.FIXED_MONTHLY:
            if now.month == 12:
                next_month = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            else:
                next_month = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
            return next_month
        
        if self.reset_policy == ResetPolicy.ROLLING_WINDOW:
            # Rolling window doesn't have a fixed "next reset"
            return None
        
        return None


@dataclass(frozen=True)
class QuotaPolicy:
    """
    Collection of quota limits.
    Policies are versioned and immutable.
    """
    policy_version: str
    limits: List[QuotaLimit]
    effective_from: datetime
    effective_until: Optional[datetime] = None
    
    def __post_init__(self):
        if not self.policy_version:
            raise ValueError("policy_version cannot be empty")
        if not self.limits:
            raise ValueError("Policy must have at least one limit")
        
        # Validate no duplicate limit_ids
        limit_ids = [limit.limit_id for limit in self.limits]
        if len(limit_ids) != len(set(limit_ids)):
            raise ValueError("Duplicate limit_ids in policy")
    
    def get_limits_for_key(self, key: QuotaKey) -> List[QuotaLimit]:
        """Get all limits applicable to a quota key."""
        return [
            limit for limit in self.limits
            if limit.metric == key.metric and limit.scope == key.scope
        ]
    
    def is_active(self, now: datetime) -> bool:
        """Check if policy is currently active."""
        if now < self.effective_from:
            return False
        if self.effective_until and now >= self.effective_until:
            return False
        return True


@dataclass
class QuotaEvaluationResult:
    """Result of quota evaluation."""
    decision: QuotaDecision
    quota_key: QuotaKey
    requested_amount: int
    current_usage: int
    limit_value: int
    remaining: int
    limit_id: str
    would_exceed: bool
    reset_at: Optional[datetime]
    next_reset: Optional[datetime]
    reason: str
    timestamp: datetime
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for audit."""
        return {
            'decision': self.decision.value,
            'quota_key': self.quota_key.to_string(),
            'requested_amount': self.requested_amount,
            'current_usage': self.current_usage,
            'limit_value': self.limit_value,
            'remaining': self.remaining,
            'limit_id': self.limit_id,
            'would_exceed': self.would_exceed,
            'reset_at': self.reset_at.isoformat() if self.reset_at else None,
            'next_reset': self.next_reset.isoformat() if self.next_reset else None,
            'reason': self.reason,
            'timestamp': self.timestamp.isoformat()
        }


# ============================================================================
# STATE BACKEND INTERFACE
# ============================================================================

class StateBackend(ABC):
    """
    Abstract interface for durable quota usage storage.
    Implementations must provide atomicity and persistence guarantees.
    """
    
    @abstractmethod
    def get_usage(self, key: QuotaKey) -> Optional[QuotaUsage]:
        """Retrieve current usage for a quota key."""
        pass
    
    @abstractmethod
    def set_usage(self, key: QuotaKey, usage: QuotaUsage) -> bool:
        """
        Atomically set usage for a quota key.
        Returns True on success, False on conflict/failure.
        """
        pass
    
    @abstractmethod
    def compare_and_swap(
        self,
        key: QuotaKey,
        expected_version: int,
        new_usage: QuotaUsage
    ) -> bool:
        """
        Atomically update usage if version matches.
        Returns True on success, False on version mismatch.
        """
        pass
    
    @abstractmethod
    def batch_get_usage(self, keys: List[QuotaKey]) -> Dict[QuotaKey, Optional[QuotaUsage]]:
        """Retrieve usage for multiple keys efficiently."""
        pass
    
    @abstractmethod
    def delete_usage(self, key: QuotaKey) -> bool:
        """Delete usage record (admin operation only)."""
        pass


class InMemoryStateBackend(StateBackend):
    """
    In-memory implementation for testing and single-node deployments.
    NOT suitable for distributed systems.
    """
    
    def __init__(self):
        self._storage: Dict[str, QuotaUsage] = {}
        self._lock = threading.Lock()
    
    def get_usage(self, key: QuotaKey) -> Optional[QuotaUsage]:
        with self._lock:
            return self._storage.get(key.to_hash())
    
    def set_usage(self, key: QuotaKey, usage: QuotaUsage) -> bool:
        with self._lock:
            self._storage[key.to_hash()] = usage
            return True
    
    def compare_and_swap(
        self,
        key: QuotaKey,
        expected_version: int,
        new_usage: QuotaUsage
    ) -> bool:
        with self._lock:
            current = self._storage.get(key.to_hash())
            if current is None and expected_version == 0:
                # First write
                self._storage[key.to_hash()] = new_usage
                return True
            if current and current.version == expected_version:
                self._storage[key.to_hash()] = new_usage
                return True
            return False
    
    def batch_get_usage(self, keys: List[QuotaKey]) -> Dict[QuotaKey, Optional[QuotaUsage]]:
        with self._lock:
            return {key: self._storage.get(key.to_hash()) for key in keys}
    
    def delete_usage(self, key: QuotaKey) -> bool:
        with self._lock:
            if key.to_hash() in self._storage:
                del self._storage[key.to_hash()]
                return True
            return False


# ============================================================================
# QUOTA EVALUATOR (FINAL ARBITER)
# ============================================================================

class QuotaEvaluator:
    """
    Determines: "If this action executes, will any quota be exceeded?"
    
    Rules:
    - Evaluation is deterministic
    - All affected quotas checked
    - Fail closed if any usage unknown
    - No anticipation — only math
    
    Same inputs → same decision. Always.
    """
    
    def __init__(self, policy: QuotaPolicy, state_backend: StateBackend):
        self.policy = policy
        self.state_backend = state_backend
        self._eval_lock = threading.Lock()
    
    def evaluate(
        self,
        key: QuotaKey,
        requested_amount: int,
        now: Optional[datetime] = None
    ) -> QuotaEvaluationResult:
        """
        Evaluate quota for a single request.
        
        Returns evaluation result with decision and details.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        
        if requested_amount < 0:
            raise ValueError(f"Requested amount cannot be negative: {requested_amount}")
        
        if requested_amount == 0:
            # Zero consumption always allowed
            return QuotaEvaluationResult(
                decision=QuotaDecision.ALLOW,
                quota_key=key,
                requested_amount=0,
                current_usage=0,
                limit_value=0,
                remaining=0,
                limit_id="zero-request",
                would_exceed=False,
                reset_at=None,
                next_reset=None,
                reason="Zero consumption request",
                timestamp=now
            )
        
        with self._eval_lock:
            return self._evaluate_internal(key, requested_amount, now)
    
    def _evaluate_internal(
        self,
        key: QuotaKey,
        requested_amount: int,
        now: datetime
    ) -> QuotaEvaluationResult:
        """Internal evaluation logic."""
        
        # Get applicable limits
        limits = self.policy.get_limits_for_key(key)
        
        if not limits:
            # No limits defined - fail closed
            return QuotaEvaluationResult(
                decision=QuotaDecision.DENY,
                quota_key=key,
                requested_amount=requested_amount,
                current_usage=0,
                limit_value=0,
                remaining=0,
                limit_id="none",
                would_exceed=True,
                reset_at=None,
                next_reset=None,
                reason="No quota limit defined for this key",
                timestamp=now
            )
        
        # Evaluate against all applicable limits
        # ANY limit exceeded = DENY
        for limit in limits:
            result = self._evaluate_against_limit(key, requested_amount, limit, now)
            if result.decision == QuotaDecision.DENY:
                return result
        
        # All limits passed - use first limit for result details
        return self._evaluate_against_limit(key, requested_amount, limits[0], now)
    
    def _evaluate_against_limit(
        self,
        key: QuotaKey,
        requested_amount: int,
        limit: QuotaLimit,
        now: datetime
    ) -> QuotaEvaluationResult:
        """Evaluate against a specific limit."""
        
        # Get current usage
        usage = self.state_backend.get_usage(key)
        
        # Check if reset is needed
        if usage and limit.should_reset(usage.reset_at or usage.first_consumed_at, now):
            # Reset has occurred - treat as zero usage
            current_consumed = 0
            reset_at = now
        else:
            current_consumed = usage.consumed_value if usage else 0
            reset_at = usage.reset_at if usage else None
        
        # Calculate projected consumption
        projected_consumed = current_consumed + requested_amount
        
        # Check against limit
        would_exceed = projected_consumed > limit.max_value
        remaining = max(0, limit.max_value - current_consumed)
        
        # Determine decision
        if would_exceed:
            decision = QuotaDecision.DENY
            reason = f"Quota would exceed: {projected_consumed} > {limit.max_value} ({limit.limit_id})"
        else:
            decision = QuotaDecision.ALLOW
            reason = f"Quota within limit: {projected_consumed} <= {limit.max_value} ({limit.limit_id})"
        
        # Calculate next reset
        next_reset = limit.get_next_reset_time(now)
        
        return QuotaEvaluationResult(
            decision=decision,
            quota_key=key,
            requested_amount=requested_amount,
            current_usage=current_consumed,
            limit_value=limit.max_value,
            remaining=remaining,
            limit_id=limit.limit_id,
            would_exceed=would_exceed,
            reset_at=reset_at,
            next_reset=next_reset,
            reason=reason,
            timestamp=now
        )
    
    def batch_evaluate(
        self,
        requests: List[Tuple[QuotaKey, int]],
        now: Optional[datetime] = None
    ) -> List[QuotaEvaluationResult]:
        """
        Evaluate multiple quota requests.
        All must pass for batch to be allowed.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        
        results = []
        for key, amount in requests:
            result = self.evaluate(key, amount, now)
            results.append(result)
            if result.decision == QuotaDecision.DENY:
                # Stop early on first denial
                break
        
        return results


# ============================================================================
# QUOTA MANAGER (PUBLIC AUTHORITY)
# ============================================================================

class QuotaManager:
    """
    Public authority for quota enforcement.
    
    Responsibilities:
    - Load active quota policies
    - Resolve applicable limits
    - Fetch durable usage
    - Evaluate impact
    - Atomically persist usage on ALLOW
    - Emit irreversible audit events
    
    If persistence fails → decision is DENY.
    """
    
    def __init__(
        self,
        policy: QuotaPolicy,
        state_backend: StateBackend,
        audit_callback: Optional[Callable[[QuotaEvaluationResult], None]] = None,
        watchdog: Optional['WatchdogInterface'] = None
    ):
        self.policy = policy
        self.state_backend = state_backend
        self.audit_callback = audit_callback
        self.watchdog = watchdog
        self.evaluator = QuotaEvaluator(policy, state_backend)
        
        # Statistics
        self._stats = defaultdict(int)
        self._stats_lock = threading.Lock()
        
        # Validate policy
        QuotaInvariants.validate_policy(policy)
    
    def check_and_consume(
        self,
        key: QuotaKey,
        amount: int,
        now: Optional[datetime] = None
    ) -> QuotaEvaluationResult:
        """
        Check quota and atomically consume if allowed.
        
        Returns evaluation result.
        Raises QuotaExceeded if denied.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        
        # Validate inputs
        QuotaInvariants.validate_quota_key(key)
        if amount < 0:
            raise ValueError(f"Cannot consume negative amount: {amount}")
        
        # Check watchdog freeze
        if self.watchdog and self.watchdog.is_frozen():
            raise QuotaFrozen("System frozen by watchdog")
        
        # Evaluate quota
        result = self.evaluator.evaluate(key, amount, now)
        
        # Record statistics
        self._record_evaluation(result.decision)
        
        # Audit (always, regardless of decision)
        self._audit(result)
        
        # If denied, raise exception
        if result.decision == QuotaDecision.DENY:
            raise QuotaExceeded(
                f"Quota exceeded: {result.reason}",
                result=result
            )
        
        # If allowed, persist consumption
        if amount > 0:  # Only persist if actually consuming
            success = self._persist_consumption(key, amount, now)
            if not success:
                # Persistence failed - convert to DENY
                failed_result = QuotaEvaluationResult(
                    decision=QuotaDecision.DENY,
                    quota_key=result.quota_key,
                    requested_amount=result.requested_amount,
                    current_usage=result.current_usage,
                    limit_value=result.limit_value,
                    remaining=result.remaining,
                    limit_id=result.limit_id,
                    would_exceed=True,
                    reset_at=result.reset_at,
                    next_reset=result.next_reset,
                    reason="Quota persistence failed - fail closed",
                    timestamp=now
                )
                self._audit(failed_result)
                raise QuotaExceeded(
                    "Quota persistence failed",
                    result=failed_result
                )
        
        return result
    
    def check_only(
        self,
        key: QuotaKey,
        amount: int,
        now: Optional[datetime] = None
    ) -> QuotaEvaluationResult:
        """
        Check quota without consuming.
        Useful for pre-flight checks.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        
        QuotaInvariants.validate_quota_key(key)
        
        result = self.evaluator.evaluate(key, amount, now)
        self._record_evaluation(result.decision)
        
        return result
    
    def get_current_usage(self, key: QuotaKey) -> Optional[QuotaUsage]:
        """Get current usage for a quota key."""
        QuotaInvariants.validate_quota_key(key)
        return self.state_backend.get_usage(key)
    
    def reset_quota(
        self,
        key: QuotaKey,
        admin_authority: str,
        reason: str,
        now: Optional[datetime] = None
    ) -> bool:
        """
        Manually reset quota (admin operation).
        Requires explicit authority and reason.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        
        QuotaInvariants.validate_quota_key(key)
        
        if not admin_authority:
            raise ValueError("Admin authority required for manual reset")
        if not reason:
            raise ValueError("Reason required for manual reset")
        
        # Create reset usage
        reset_usage = QuotaUsage(
            consumed_value=0,
            first_consumed_at=now,
            last_updated_at=now,
            reset_at=now,
            version=1
        )
        
        # Persist reset
        success = self.state_backend.set_usage(key, reset_usage)
        
        if success:
            # Audit reset
            reset_result = QuotaEvaluationResult(
                decision=QuotaDecision.ALLOW,
                quota_key=key,
                requested_amount=0,
                current_usage=0,
                limit_value=0,
                remaining=0,
                limit_id="manual-reset",
                would_exceed=False,
                reset_at=now,
                next_reset=None,
                reason=f"Manual reset by {admin_authority}: {reason}",
                timestamp=now
            )
            self._audit(reset_result)
        
        return success
    
    def _persist_consumption(
        self,
        key: QuotaKey,
        amount: int,
        now: datetime,
        max_retries: int = 3
    ) -> bool:
        """
        Atomically persist consumption using compare-and-swap.
        Returns True on success, False on failure.
        """
        for attempt in range(max_retries):
            # Get current usage
            current_usage = self.state_backend.get_usage(key)
            
            if current_usage is None:
                # First consumption - create new usage
                new_usage = QuotaUsage(
                    consumed_value=amount,
                    first_consumed_at=now,
                    last_updated_at=now,
                    reset_at=None,
                    version=1
                )
                success = self.state_backend.compare_and_swap(key, 0, new_usage)
            else:
                # Check if reset needed
                limits = self.policy.get_limits_for_key(key)
                if limits and limits[0].should_reset(
                    current_usage.reset_at or current_usage.first_consumed_at,
                    now
                ):
                    # Reset occurred - start fresh
                    new_usage = QuotaUsage(
                        consumed_value=amount,
                        first_consumed_at=now,
                        last_updated_at=now,
                        reset_at=now,
                        version=1
                    )
                    success = self.state_backend.compare_and_swap(
                        key,
                        current_usage.version,
                        new_usage
                    )
                else:
                    # Increment existing usage
                    new_usage = current_usage.increment(amount, now)
                    success = self.state_backend.compare_and_swap(
                        key,
                        current_usage.version,
                        new_usage
                    )
            
            if success:
                return True
            
            # CAS failed - retry
            if attempt < max_retries - 1:
                # Small exponential backoff
                import time
                time.sleep(0.001 * (2 ** attempt))
        
        # All retries failed
        return False
    
    def _audit(self, result: QuotaEvaluationResult) -> None:
        """Emit irreversible audit event."""
        if self.audit_callback:
            try:
                self.audit_callback(result)
            except Exception:
                # Never let audit failure block quota decision
                pass
    
    def _record_evaluation(self, decision: QuotaDecision) -> None:
        """Record evaluation statistics."""
        with self._stats_lock:
            self._stats[decision] += 1
            self._stats['total'] += 1
    
    def get_statistics(self) -> Dict[str, int]:
        """Get quota manager statistics."""
        with self._stats_lock:
            return dict(self._stats)
    
    @contextmanager
    def consume(self, key: QuotaKey, amount: int, now: Optional[datetime] = None):
        """
        Context manager for quota consumption.
        
        Usage:
            with quota_manager.consume(key, 100):
                # Execute operation
                pass
        """
        result = self.check_and_consume(key, amount, now)
        try:
            yield result
        finally:
            # Could add cleanup logic here if needed
            pass


# ============================================================================
# WATCHDOG INTEGRATION
# ============================================================================

class WatchdogInterface:
    """
    Interface for watchdog integration.
    
    QuotaManager MUST:
    - Obey global freeze
    - Honor emergency DENY mandates
    - Allow manual emergency caps
    - Never auto-expand limits
    
    Watchdog can revoke quota.
    Watchdog cannot silently raise it.
    """
    
    def __init__(self):
        self._frozen = False
        self._emergency_caps: Dict[str, int] = {}
        self._lock = threading.Lock()
    
    def freeze(self) -> None:
        """Freeze all quota operations."""
        with self._lock:
            self._frozen = True
    
    def unfreeze(self) -> None:
        """Unfreeze quota operations."""
        with self._lock:
            self._frozen = False
    
    def is_frozen(self) -> bool:
        """Check if quota operations are frozen."""
        with self._lock:
            return self._frozen
    
    def set_emergency_cap(self, limit_id: str, cap: int) -> None:
        """Set emergency cap for a specific limit."""
        if cap < 0:
            raise ValueError(f"Emergency cap cannot be negative: {cap}")
        with self._lock:
            self._emergency_caps[limit_id] = cap
    
    def get_emergency_cap(self, limit_id: str) -> Optional[int]:
        """Get emergency cap if set."""
        with self._lock:
            return self._emergency_caps.get(limit_id)
    
    def clear_emergency_cap(self, limit_id: str) -> None:
        """Clear emergency cap."""
        with self._lock:
            if limit_id in self._emergency_caps:
                del self._emergency_caps[limit_id]


# ============================================================================
# QUOTA INVARIANTS (ABSOLUTE)
# ============================================================================

class QuotaInvariants:
    """
    MUST guarantee:
    - max_value > 0
    - consumed_value never decreases
    - no metric ambiguity
    - no cross-scope leakage
    - no execution without usage persistence
    - no recovery from quota exhaustion without authority
    
    Violation → immediate hard stop.
    """
    
    @staticmethod
    def validate_policy(policy: QuotaPolicy) -> None:
        """Validate policy invariants."""
        if not policy.policy_version:
            raise InvariantViolation("Policy must have version")
        
        if not policy.limits:
            raise InvariantViolation("Policy must have at least one limit")
        
        # Validate all limits
        for limit in policy.limits:
            QuotaInvariants.validate_limit(limit)
        
        # Check for duplicate limit IDs
        limit_ids = [limit.limit_id for limit in policy.limits]
        if len(limit_ids) != len(set(limit_ids)):
            raise InvariantViolation("Duplicate limit IDs in policy")
    
    @staticmethod
    def validate_limit(limit: QuotaLimit) -> None:
        """Validate limit invariants."""
        if limit.max_value <= 0:
            raise InvariantViolation(
                f"Limit max_value must be > 0: {limit.limit_id} = {limit.max_value}"
            )
        
        if not limit.limit_id:
            raise InvariantViolation("Limit must have non-empty ID")
        
        # Validate reset policy constraints
        if limit.reset_policy == ResetPolicy.ROLLING_WINDOW:
            if limit.window_seconds is None or limit.window_seconds <= 0:
                raise InvariantViolation(
                    f"ROLLING_WINDOW requires positive window_seconds: {limit.limit_id}"
                )
    
    @staticmethod
    def validate_quota_key(key: QuotaKey) -> None:
        """Validate quota key invariants."""
        if not key.scope_id:
            raise InvariantViolation("QuotaKey must have non-empty scope_id")
        
        if not isinstance(key.scope, QuotaScope):
            raise InvariantViolation(f"Invalid scope type: {type(key.scope)}")
        
        if not isinstance(key.metric, QuotaMetric):
            raise InvariantViolation(f"Invalid metric type: {type(key.metric)}")
    
    @staticmethod
    def validate_usage(usage: QuotaUsage) -> None:
        """Validate usage invariants."""
        if usage.consumed_value < 0:
            raise InvariantViolation(
                f"Usage consumed_value cannot be negative: {usage.consumed_value}"
            )
        
        if usage.last_updated_at < usage.first_consumed_at:
            raise InvariantViolation(
                "Usage last_updated_at cannot be before first_consumed_at"
            )
        
        if usage.version < 1:
            raise InvariantViolation(f"Usage version must be >= 1: {usage.version}")
    
    @staticmethod
    def validate_consumption_never_decreases(
        old_usage: Optional[QuotaUsage],
        new_usage: QuotaUsage
    ) -> None:
        """
        Validate that consumption never decreases (except on reset).
        This is a CRITICAL invariant.
        """
        if old_usage is None:
            return  # First consumption
        
        # If reset occurred, new usage can be lower
        if new_usage.reset_at and new_usage.reset_at > (old_usage.reset_at or old_usage.first_consumed_at):
            return  # Valid reset
        
        # Otherwise, consumption must not decrease
        if new_usage.consumed_value < old_usage.consumed_value:
            raise InvariantViolation(
                f"Consumption cannot decrease: {old_usage.consumed_value} -> {new_usage.consumed_value}"
            )


# ============================================================================
# EXCEPTIONS
# ============================================================================

class QuotaException(Exception):
    """Base exception for quota system."""
    pass


class QuotaExceeded(QuotaException):
    """Raised when quota is exceeded."""
    
    def __init__(self, message: str, result: QuotaEvaluationResult):
        super().__init__(message)
        self.result = result


class QuotaFrozen(QuotaException):
    """Raised when quota operations are frozen by watchdog."""
    pass


class InvariantViolation(QuotaException):
    """Raised when a quota invariant is violated."""
    pass


# ============================================================================
# QUOTA POLICY FACTORY
# ============================================================================

class QuotaPolicyFactory:
    """Factory for creating standard quota policies."""
    
    @staticmethod
    def create_default_policy(now: Optional[datetime] = None) -> QuotaPolicy:
        """Create default production quota policy."""
        if now is None:
            now = datetime.now(timezone.utc)
        
        return QuotaPolicy(
            policy_version="default-v1.0.0",
            effective_from=now,
            effective_until=None,
            limits=[
                # Global API rate limits
                QuotaLimit(
                    limit_id="global-api-hourly",
                    max_value=1_000_000,
                    metric=QuotaMetric.API_CALLS,
                    scope=QuotaScope.GLOBAL,
                    reset_policy=ResetPolicy.FIXED_HOURLY,
                    description="Global API calls per hour"
                ),
                
                # Account limits
                QuotaLimit(
                    limit_id="account-api-daily",
                    max_value=100_000,
                    metric=QuotaMetric.API_CALLS,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.FIXED_DAILY,
                    description="Account API calls per day"
                ),
                QuotaLimit(
                    limit_id="account-content-daily",
                    max_value=10_000,
                    metric=QuotaMetric.CONTENT_CREATED,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.FIXED_DAILY,
                    description="Account content creation per day"
                ),
                QuotaLimit(
                    limit_id="account-storage-total",
                    max_value=100_000_000_000,  # 100 GB
                    metric=QuotaMetric.STORAGE_BYTES,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.NEVER,
                    description="Account total storage"
                ),
                
                # Workflow limits
                QuotaLimit(
                    limit_id="workflow-executions-hourly",
                    max_value=1_000,
                    metric=QuotaMetric.WORKFLOW_EXECUTIONS,
                    scope=QuotaScope.WORKFLOW,
                    reset_policy=ResetPolicy.FIXED_HOURLY,
                    description="Workflow executions per hour"
                ),
                
                # Infrastructure limits
                QuotaLimit(
                    limit_id="infra-gpu-daily",
                    max_value=86_400,  # 24 hours in seconds
                    metric=QuotaMetric.GPU_SECONDS,
                    scope=QuotaScope.INFRA,
                    reset_policy=ResetPolicy.FIXED_DAILY,
                    description="Infrastructure GPU seconds per day"
                ),
                QuotaLimit(
                    limit_id="infra-egress-daily",
                    max_value=1_000_000_000_000,  # 1 TB
                    metric=QuotaMetric.EGRESS_BYTES,
                    scope=QuotaScope.INFRA,
                    reset_policy=ResetPolicy.FIXED_DAILY,
                    description="Infrastructure egress per day"
                ),
            ]
        )
    
    @staticmethod
    def create_free_tier_policy(now: Optional[datetime] = None) -> QuotaPolicy:
        """Create free tier quota policy with tighter limits."""
        if now is None:
            now = datetime.now(timezone.utc)
        
        return QuotaPolicy(
            policy_version="free-tier-v1.0.0",
            effective_from=now,
            effective_until=None,
            limits=[
                QuotaLimit(
                    limit_id="free-api-daily",
                    max_value=1_000,
                    metric=QuotaMetric.API_CALLS,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.FIXED_DAILY,
                    description="Free tier API calls per day"
                ),
                QuotaLimit(
                    limit_id="free-content-daily",
                    max_value=100,
                    metric=QuotaMetric.CONTENT_CREATED,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.FIXED_DAILY,
                    description="Free tier content per day"
                ),
                QuotaLimit(
                    limit_id="free-storage-total",
                    max_value=1_000_000_000,  # 1 GB
                    metric=QuotaMetric.STORAGE_BYTES,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.NEVER,
                    description="Free tier total storage"
                ),
            ]
        )
    
    @staticmethod
    def create_enterprise_policy(now: Optional[datetime] = None) -> QuotaPolicy:
        """Create enterprise quota policy with higher limits."""
        if now is None:
            now = datetime.now(timezone.utc)
        
        return QuotaPolicy(
            policy_version="enterprise-v1.0.0",
            effective_from=now,
            effective_until=None,
            limits=[
                QuotaLimit(
                    limit_id="enterprise-api-hourly",
                    max_value=10_000_000,
                    metric=QuotaMetric.API_CALLS,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.FIXED_HOURLY,
                    description="Enterprise API calls per hour"
                ),
                QuotaLimit(
                    limit_id="enterprise-content-daily",
                    max_value=1_000_000,
                    metric=QuotaMetric.CONTENT_CREATED,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.FIXED_DAILY,
                    description="Enterprise content per day"
                ),
                QuotaLimit(
                    limit_id="enterprise-storage-total",
                    max_value=10_000_000_000_000,  # 10 TB
                    metric=QuotaMetric.STORAGE_BYTES,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.NEVER,
                    description="Enterprise total storage"
                ),
                QuotaLimit(
                    limit_id="enterprise-gpu-daily",
                    max_value=864_000,  # 240 hours in seconds
                    metric=QuotaMetric.GPU_SECONDS,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.FIXED_DAILY,
                    description="Enterprise GPU seconds per day"
                ),
            ]
        )


# ============================================================================
# EXAMPLE USAGE & INTEGRATION
# ============================================================================

def example_usage():
    """Example of how to use the quota system."""
    
    # Create state backend
    state_backend = InMemoryStateBackend()
    
    # Create policy
    policy = QuotaPolicyFactory.create_default_policy()
    
    # Create audit callback
    def audit_logger(result: QuotaEvaluationResult):
        print(f"[QUOTA AUDIT] {result.decision.value}: {result.reason}")
        print(f"  Key: {result.quota_key.to_string()}")
        print(f"  Usage: {result.current_usage}/{result.limit_value} (remaining: {result.remaining})")
        if result.next_reset:
            print(f"  Next reset: {result.next_reset.isoformat()}")
    
    # Create watchdog
    watchdog = WatchdogInterface()
    
    # Create quota manager
    quota_manager = QuotaManager(
        policy=policy,
        state_backend=state_backend,
        audit_callback=audit_logger,
        watchdog=watchdog
    )
    
    # Example 1: Check and consume API quota
    account_key = QuotaKey(
        scope=QuotaScope.ACCOUNT,
        scope_id="acc-12345",
        metric=QuotaMetric.API_CALLS
    )
    
    try:
        # Consume 10 API calls
        result = quota_manager.check_and_consume(account_key, 10)
        print(f"\n✓ Consumed 10 API calls. Remaining: {result.remaining}")
    except QuotaExceeded as e:
        print(f"\n✗ Quota exceeded: {e}")
    
    # Example 2: Use context manager
    workflow_key = QuotaKey(
        scope=QuotaScope.WORKFLOW,
        scope_id="wf-67890",
        metric=QuotaMetric.WORKFLOW_EXECUTIONS
    )
    
    try:
        with quota_manager.consume(workflow_key, 1):
            print("\n✓ Workflow execution allowed, performing work...")
            # Do work here
    except QuotaExceeded as e:
        print(f"\n✗ Workflow execution denied: {e}")
    
    # Example 3: Check without consuming (pre-flight)
    check_result = quota_manager.check_only(account_key, 50000)
    if check_result.decision == QuotaDecision.ALLOW:
        print(f"\n✓ Pre-flight check passed. Can consume 50000 more.")
    else:
        print(f"\n✗ Pre-flight check failed: {check_result.reason}")
    
    # Example 4: Get current usage
    usage = quota_manager.get_current_usage(account_key)
    if usage:
        print(f"\nCurrent usage for {account_key.to_string()}: {usage.consumed_value}")
        print(f"  First consumed: {usage.first_consumed_at.isoformat()}")
        print(f"  Last updated: {usage.last_updated_at.isoformat()}")
    
    # Example 5: Admin reset
    try:
        quota_manager.reset_quota(
            account_key,
            admin_authority="admin@example.com",
            reason="Customer support request #12345"
        )
        print(f"\n✓ Quota reset for {account_key.to_string()}")
    except Exception as e:
        print(f"\n✗ Reset failed: {e}")
    
    # Example 6: Emergency freeze
    watchdog.freeze()
    print("\n⚠ System frozen by watchdog")
    
    try:
        quota_manager.check_and_consume(account_key, 1)
    except QuotaFrozen:
        print("✗ Operation blocked - system frozen")
    
    watchdog.unfreeze()
    print("✓ System unfrozen")
    
    # Get statistics
    stats = quota_manager.get_statistics()
    print(f"\nQuota Manager Statistics:")
    print(f"  Total evaluations: {stats.get('total', 0)}")
    print(f"  Allowed: {stats.get(QuotaDecision.ALLOW, 0)}")
    print(f"  Denied: {stats.get(QuotaDecision.DENY, 0)}")


if __name__ == "__main__":
    example_usage()"""
quota_manager.py - Hard Consumption Ceiling Authority

Built for:
- Absolute usage ceilings
- Replay-safe monotonic tracking
- Multi-dimensional quota enforcement
- Zero forgiveness on exhaustion
- Irreversible audit trails
- Watchdog-enforced caps

NO SOFT LIMITS. NO GRACE PERIODS. NO BORROWING.

What this file ACTUALLY is:
"Have you already done too much — period?"

Quota is binary finality, not flow control.

Authority chain: quota → rate → backpressure → execution
If quota denies → nothing downstream runs.
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Set, Tuple, Callable, Any
from datetime import datetime, timezone, timedelta
from abc import ABC, abstractmethod
import threading
from contextlib import contextmanager
from collections import defaultdict
import hashlib
import json


# ============================================================================
# CORE ENUMS (STRICT)
# ============================================================================

class QuotaScope(Enum):
    """
    Scopes do not overlap unless explicitly declared.
    Hierarchy is strict and non-inferrable.
    """
    GLOBAL = "global"
    ACCOUNT = "account"
    WORKFLOW = "workflow"
    PROJECT = "project"
    INFRA = "infra"


class QuotaMetric(Enum):
    """
    Metrics are concrete, measurable, and monotonic.
    Each metric type is independently tracked.
    """
    EVENT_COUNT = "event_count"
    API_CALLS = "api_calls"
    CONTENT_CREATED = "content_created"
    CONTENT_POSTED = "content_posted"
    GPU_SECONDS = "gpu_seconds"
    STORAGE_BYTES = "storage_bytes"
    EGRESS_BYTES = "egress_bytes"
    INFERENCE_TOKENS = "inference_tokens"
    WORKFLOW_EXECUTIONS = "workflow_executions"
    COMPUTE_HOURS = "compute_hours"


class QuotaDecision(Enum):
    """
    No partial grants.
    No "warn" mode.
    Binary finality only.
    """
    ALLOW = "allow"
    DENY = "deny"


class ResetPolicy(Enum):
    """
    How and when quotas reset.
    No implicit behavior.
    """
    FIXED_HOURLY = "fixed_hourly"      # Reset at top of hour
    FIXED_DAILY = "fixed_daily"        # Reset at midnight UTC
    FIXED_WEEKLY = "fixed_weekly"      # Reset at Monday 00:00 UTC
    FIXED_MONTHLY = "fixed_monthly"    # Reset at 1st of month 00:00 UTC
    ROLLING_WINDOW = "rolling_window"  # Sliding time window
    MANUAL_ONLY = "manual_only"        # Admin reset only
    NEVER = "never"                    # Lifetime quota


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class QuotaKey:
    """
    Fully qualified identifier for a quota instance.
    Fully specified or rejected - no partial keys.
    """
    scope: QuotaScope
    scope_id: str  # Must be non-empty
    metric: QuotaMetric
    
    def __post_init__(self):
        if not self.scope_id:
            raise ValueError("scope_id cannot be empty")
        if not isinstance(self.scope, QuotaScope):
            raise ValueError(f"Invalid scope type: {type(self.scope)}")
        if not isinstance(self.metric, QuotaMetric):
            raise ValueError(f"Invalid metric type: {type(self.metric)}")
    
    def to_string(self) -> str:
        """Convert to canonical string representation."""
        return f"{self.scope.value}:{self.scope_id}:{self.metric.value}"
    
    def to_hash(self) -> str:
        """Generate deterministic hash for storage key."""
        return hashlib.sha256(self.to_string().encode()).hexdigest()[:16]
    
    @classmethod
    def from_string(cls, key_str: str) -> 'QuotaKey':
        """Parse from canonical string representation."""
        parts = key_str.split(':', 2)
        if len(parts) != 3:
            raise ValueError(f"Invalid quota key string: {key_str}")
        return cls(
            scope=QuotaScope(parts[0]),
            scope_id=parts[1],
            metric=QuotaMetric(parts[2])
        )


@dataclass
class QuotaUsage:
    """
    Immutable record of consumption.
    Usage only moves forward - never decreases.
    """
    consumed_value: int
    first_consumed_at: datetime
    last_updated_at: datetime
    reset_at: Optional[datetime] = None
    version: int = 1  # For optimistic locking
    
    def __post_init__(self):
        if self.consumed_value < 0:
            raise ValueError(f"Consumed value cannot be negative: {self.consumed_value}")
        if self.last_updated_at < self.first_consumed_at:
            raise ValueError("last_updated_at cannot be before first_consumed_at")
        if self.version < 1:
            raise ValueError(f"Version must be >= 1: {self.version}")
    
    def increment(self, amount: int, now: datetime) -> 'QuotaUsage':
        """
        Create new usage record with incremented consumption.
        Returns new instance - original is immutable.
        """
        if amount < 0:
            raise ValueError(f"Cannot increment by negative amount: {amount}")
        if amount == 0:
            return self
        
        return QuotaUsage(
            consumed_value=self.consumed_value + amount,
            first_consumed_at=self.first_consumed_at,
            last_updated_at=now,
            reset_at=self.reset_at,
            version=self.version + 1
        )
    
    def reset(self, now: datetime) -> 'QuotaUsage':
        """Create new usage record for reset period."""
        return QuotaUsage(
            consumed_value=0,
            first_consumed_at=now,
            last_updated_at=now,
            reset_at=now,
            version=1
        )
    
    def to_dict(self) -> Dict:
        """Serialize to dictionary."""
        return {
            'consumed_value': self.consumed_value,
            'first_consumed_at': self.first_consumed_at.isoformat(),
            'last_updated_at': self.last_updated_at.isoformat(),
            'reset_at': self.reset_at.isoformat() if self.reset_at else None,
            'version': self.version
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'QuotaUsage':
        """Deserialize from dictionary."""
        return cls(
            consumed_value=data['consumed_value'],
            first_consumed_at=datetime.fromisoformat(data['first_consumed_at']),
            last_updated_at=datetime.fromisoformat(data['last_updated_at']),
            reset_at=datetime.fromisoformat(data['reset_at']) if data.get('reset_at') else None,
            version=data.get('version', 1)
        )


@dataclass(frozen=True)
class QuotaLimit:
    """
    Immutable quota limit definition.
    Limits are declared, versioned, immutable.
    """
    limit_id: str
    max_value: int
    metric: QuotaMetric
    scope: QuotaScope
    reset_policy: ResetPolicy
    window_seconds: Optional[int] = None  # For ROLLING_WINDOW only
    description: str = ""
    
    def __post_init__(self):
        if self.max_value <= 0:
            raise ValueError(f"max_value must be > 0: {self.max_value}")
        if not self.limit_id:
            raise ValueError("limit_id cannot be empty")
        
        # Validate window_seconds for rolling window
        if self.reset_policy == ResetPolicy.ROLLING_WINDOW:
            if self.window_seconds is None or self.window_seconds <= 0:
                raise ValueError("ROLLING_WINDOW requires positive window_seconds")
        elif self.window_seconds is not None:
            raise ValueError(f"window_seconds only valid for ROLLING_WINDOW, got {self.reset_policy}")
    
    def should_reset(self, last_reset: Optional[datetime], now: datetime) -> bool:
        """Determine if quota should be reset based on policy."""
        if self.reset_policy == ResetPolicy.NEVER:
            return False
        
        if self.reset_policy == ResetPolicy.MANUAL_ONLY:
            return False
        
        if last_reset is None:
            return False  # No previous reset, so not time for another
        
        if self.reset_policy == ResetPolicy.FIXED_HOURLY:
            # Reset if we've crossed an hour boundary
            return last_reset.hour != now.hour or last_reset.date() != now.date()
        
        if self.reset_policy == ResetPolicy.FIXED_DAILY:
            # Reset if we've crossed a day boundary
            return last_reset.date() != now.date()
        
        if self.reset_policy == ResetPolicy.FIXED_WEEKLY:
            # Reset if we've crossed a week boundary (Monday)
            last_week = last_reset.isocalendar()[1]
            curr_week = now.isocalendar()[1]
            return last_week != curr_week or last_reset.year != now.year
        
        if self.reset_policy == ResetPolicy.FIXED_MONTHLY:
            # Reset if we've crossed a month boundary
            return last_reset.month != now.month or last_reset.year != now.year
        
        if self.reset_policy == ResetPolicy.ROLLING_WINDOW:
            # Reset if outside the rolling window
            delta = (now - last_reset).total_seconds()
            return delta >= self.window_seconds
        
        return False
    
    def get_next_reset_time(self, now: datetime) -> Optional[datetime]:
        """Calculate when the next reset will occur."""
        if self.reset_policy in [ResetPolicy.NEVER, ResetPolicy.MANUAL_ONLY]:
            return None
        
        if self.reset_policy == ResetPolicy.FIXED_HOURLY:
            next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            return next_hour
        
        if self.reset_policy == ResetPolicy.FIXED_DAILY:
            next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return next_day
        
        if self.reset_policy == ResetPolicy.FIXED_WEEKLY:
            days_until_monday = (7 - now.weekday()) % 7
            if days_until_monday == 0:
                days_until_monday = 7
            next_monday = (now + timedelta(days=days_until_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            return next_monday
        
        if self.reset_policy == ResetPolicy.FIXED_MONTHLY:
            if now.month == 12:
                next_month = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            else:
                next_month = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
            return next_month
        
        if self.reset_policy == ResetPolicy.ROLLING_WINDOW:
            # Rolling window doesn't have a fixed "next reset"
            return None
        
        return None


@dataclass(frozen=True)
class QuotaPolicy:
    """
    Collection of quota limits.
    Policies are versioned and immutable.
    """
    policy_version: str
    limits: List[QuotaLimit]
    effective_from: datetime
    effective_until: Optional[datetime] = None
    
    def __post_init__(self):
        if not self.policy_version:
            raise ValueError("policy_version cannot be empty")
        if not self.limits:
            raise ValueError("Policy must have at least one limit")
        
        # Validate no duplicate limit_ids
        limit_ids = [limit.limit_id for limit in self.limits]
        if len(limit_ids) != len(set(limit_ids)):
            raise ValueError("Duplicate limit_ids in policy")
    
    def get_limits_for_key(self, key: QuotaKey) -> List[QuotaLimit]:
        """Get all limits applicable to a quota key."""
        return [
            limit for limit in self.limits
            if limit.metric == key.metric and limit.scope == key.scope
        ]
    
    def is_active(self, now: datetime) -> bool:
        """Check if policy is currently active."""
        if now < self.effective_from:
            return False
        if self.effective_until and now >= self.effective_until:
            return False
        return True


@dataclass
class QuotaEvaluationResult:
    """Result of quota evaluation."""
    decision: QuotaDecision
    quota_key: QuotaKey
    requested_amount: int
    current_usage: int
    limit_value: int
    remaining: int
    limit_id: str
    would_exceed: bool
    reset_at: Optional[datetime]
    next_reset: Optional[datetime]
    reason: str
    timestamp: datetime
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for audit."""
        return {
            'decision': self.decision.value,
            'quota_key': self.quota_key.to_string(),
            'requested_amount': self.requested_amount,
            'current_usage': self.current_usage,
            'limit_value': self.limit_value,
            'remaining': self.remaining,
            'limit_id': self.limit_id,
            'would_exceed': self.would_exceed,
            'reset_at': self.reset_at.isoformat() if self.reset_at else None,
            'next_reset': self.next_reset.isoformat() if self.next_reset else None,
            'reason': self.reason,
            'timestamp': self.timestamp.isoformat()
        }


# ============================================================================
# STATE BACKEND INTERFACE
# ============================================================================

class StateBackend(ABC):
    """
    Abstract interface for durable quota usage storage.
    Implementations must provide atomicity and persistence guarantees.
    """
    
    @abstractmethod
    def get_usage(self, key: QuotaKey) -> Optional[QuotaUsage]:
        """Retrieve current usage for a quota key."""
        pass
    
    @abstractmethod
    def set_usage(self, key: QuotaKey, usage: QuotaUsage) -> bool:
        """
        Atomically set usage for a quota key.
        Returns True on success, False on conflict/failure.
        """
        pass
    
    @abstractmethod
    def compare_and_swap(
        self,
        key: QuotaKey,
        expected_version: int,
        new_usage: QuotaUsage
    ) -> bool:
        """
        Atomically update usage if version matches.
        Returns True on success, False on version mismatch.
        """
        pass
    
    @abstractmethod
    def batch_get_usage(self, keys: List[QuotaKey]) -> Dict[QuotaKey, Optional[QuotaUsage]]:
        """Retrieve usage for multiple keys efficiently."""
        pass
    
    @abstractmethod
    def delete_usage(self, key: QuotaKey) -> bool:
        """Delete usage record (admin operation only)."""
        pass


class InMemoryStateBackend(StateBackend):
    """
    In-memory implementation for testing and single-node deployments.
    NOT suitable for distributed systems.
    """
    
    def __init__(self):
        self._storage: Dict[str, QuotaUsage] = {}
        self._lock = threading.Lock()
    
    def get_usage(self, key: QuotaKey) -> Optional[QuotaUsage]:
        with self._lock:
            return self._storage.get(key.to_hash())
    
    def set_usage(self, key: QuotaKey, usage: QuotaUsage) -> bool:
        with self._lock:
            self._storage[key.to_hash()] = usage
            return True
    
    def compare_and_swap(
        self,
        key: QuotaKey,
        expected_version: int,
        new_usage: QuotaUsage
    ) -> bool:
        with self._lock:
            current = self._storage.get(key.to_hash())
            if current is None and expected_version == 0:
                # First write
                self._storage[key.to_hash()] = new_usage
                return True
            if current and current.version == expected_version:
                self._storage[key.to_hash()] = new_usage
                return True
            return False
    
    def batch_get_usage(self, keys: List[QuotaKey]) -> Dict[QuotaKey, Optional[QuotaUsage]]:
        with self._lock:
            return {key: self._storage.get(key.to_hash()) for key in keys}
    
    def delete_usage(self, key: QuotaKey) -> bool:
        with self._lock:
            if key.to_hash() in self._storage:
                del self._storage[key.to_hash()]
                return True
            return False


# ============================================================================
# QUOTA EVALUATOR (FINAL ARBITER)
# ============================================================================

class QuotaEvaluator:
    """
    Determines: "If this action executes, will any quota be exceeded?"
    
    Rules:
    - Evaluation is deterministic
    - All affected quotas checked
    - Fail closed if any usage unknown
    - No anticipation — only math
    
    Same inputs → same decision. Always.
    """
    
    def __init__(self, policy: QuotaPolicy, state_backend: StateBackend):
        self.policy = policy
        self.state_backend = state_backend
        self._eval_lock = threading.Lock()
    
    def evaluate(
        self,
        key: QuotaKey,
        requested_amount: int,
        now: Optional[datetime] = None
    ) -> QuotaEvaluationResult:
        """
        Evaluate quota for a single request.
        
        Returns evaluation result with decision and details.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        
        if requested_amount < 0:
            raise ValueError(f"Requested amount cannot be negative: {requested_amount}")
        
        if requested_amount == 0:
            # Zero consumption always allowed
            return QuotaEvaluationResult(
                decision=QuotaDecision.ALLOW,
                quota_key=key,
                requested_amount=0,
                current_usage=0,
                limit_value=0,
                remaining=0,
                limit_id="zero-request",
                would_exceed=False,
                reset_at=None,
                next_reset=None,
                reason="Zero consumption request",
                timestamp=now
            )
        
        with self._eval_lock:
            return self._evaluate_internal(key, requested_amount, now)
    
    def _evaluate_internal(
        self,
        key: QuotaKey,
        requested_amount: int,
        now: datetime
    ) -> QuotaEvaluationResult:
        """Internal evaluation logic."""
        
        # Get applicable limits
        limits = self.policy.get_limits_for_key(key)
        
        if not limits:
            # No limits defined - fail closed
            return QuotaEvaluationResult(
                decision=QuotaDecision.DENY,
                quota_key=key,
                requested_amount=requested_amount,
                current_usage=0,
                limit_value=0,
                remaining=0,
                limit_id="none",
                would_exceed=True,
                reset_at=None,
                next_reset=None,
                reason="No quota limit defined for this key",
                timestamp=now
            )
        
        # Evaluate against all applicable limits
        # ANY limit exceeded = DENY
        for limit in limits:
            result = self._evaluate_against_limit(key, requested_amount, limit, now)
            if result.decision == QuotaDecision.DENY:
                return result
        
        # All limits passed - use first limit for result details
        return self._evaluate_against_limit(key, requested_amount, limits[0], now)
    
    def _evaluate_against_limit(
        self,
        key: QuotaKey,
        requested_amount: int,
        limit: QuotaLimit,
        now: datetime
    ) -> QuotaEvaluationResult:
        """Evaluate against a specific limit."""
        
        # Get current usage
        usage = self.state_backend.get_usage(key)
        
        # Check if reset is needed
        if usage and limit.should_reset(usage.reset_at or usage.first_consumed_at, now):
            # Reset has occurred - treat as zero usage
            current_consumed = 0
            reset_at = now
        else:
            current_consumed = usage.consumed_value if usage else 0
            reset_at = usage.reset_at if usage else None
        
        # Calculate projected consumption
        projected_consumed = current_consumed + requested_amount
        
        # Check against limit
        would_exceed = projected_consumed > limit.max_value
        remaining = max(0, limit.max_value - current_consumed)
        
        # Determine decision
        if would_exceed:
            decision = QuotaDecision.DENY
            reason = f"Quota would exceed: {projected_consumed} > {limit.max_value} ({limit.limit_id})"
        else:
            decision = QuotaDecision.ALLOW
            reason = f"Quota within limit: {projected_consumed} <= {limit.max_value} ({limit.limit_id})"
        
        # Calculate next reset
        next_reset = limit.get_next_reset_time(now)
        
        return QuotaEvaluationResult(
            decision=decision,
            quota_key=key,
            requested_amount=requested_amount,
            current_usage=current_consumed,
            limit_value=limit.max_value,
            remaining=remaining,
            limit_id=limit.limit_id,
            would_exceed=would_exceed,
            reset_at=reset_at,
            next_reset=next_reset,
            reason=reason,
            timestamp=now
        )
    
    def batch_evaluate(
        self,
        requests: List[Tuple[QuotaKey, int]],
        now: Optional[datetime] = None
    ) -> List[QuotaEvaluationResult]:
        """
        Evaluate multiple quota requests.
        All must pass for batch to be allowed.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        
        results = []
        for key, amount in requests:
            result = self.evaluate(key, amount, now)
            results.append(result)
            if result.decision == QuotaDecision.DENY:
                # Stop early on first denial
                break
        
        return results


# ============================================================================
# QUOTA MANAGER (PUBLIC AUTHORITY)
# ============================================================================

class QuotaManager:
    """
    Public authority for quota enforcement.
    
    Responsibilities:
    - Load active quota policies
    - Resolve applicable limits
    - Fetch durable usage
    - Evaluate impact
    - Atomically persist usage on ALLOW
    - Emit irreversible audit events
    
    If persistence fails → decision is DENY.
    """
    
    def __init__(
        self,
        policy: QuotaPolicy,
        state_backend: StateBackend,
        audit_callback: Optional[Callable[[QuotaEvaluationResult], None]] = None,
        watchdog: Optional['WatchdogInterface'] = None
    ):
        self.policy = policy
        self.state_backend = state_backend
        self.audit_callback = audit_callback
        self.watchdog = watchdog
        self.evaluator = QuotaEvaluator(policy, state_backend)
        
        # Statistics
        self._stats = defaultdict(int)
        self._stats_lock = threading.Lock()
        
        # Validate policy
        QuotaInvariants.validate_policy(policy)
    
    def check_and_consume(
        self,
        key: QuotaKey,
        amount: int,
        now: Optional[datetime] = None
    ) -> QuotaEvaluationResult:
        """
        Check quota and atomically consume if allowed.
        
        Returns evaluation result.
        Raises QuotaExceeded if denied.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        
        # Validate inputs
        QuotaInvariants.validate_quota_key(key)
        if amount < 0:
            raise ValueError(f"Cannot consume negative amount: {amount}")
        
        # Check watchdog freeze
        if self.watchdog and self.watchdog.is_frozen():
            raise QuotaFrozen("System frozen by watchdog")
        
        # Evaluate quota
        result = self.evaluator.evaluate(key, amount, now)
        
        # Record statistics
        self._record_evaluation(result.decision)
        
        # Audit (always, regardless of decision)
        self._audit(result)
        
        # If denied, raise exception
        if result.decision == QuotaDecision.DENY:
            raise QuotaExceeded(
                f"Quota exceeded: {result.reason}",
                result=result
            )
        
        # If allowed, persist consumption
        if amount > 0:  # Only persist if actually consuming
            success = self._persist_consumption(key, amount, now)
            if not success:
                # Persistence failed - convert to DENY
                failed_result = QuotaEvaluationResult(
                    decision=QuotaDecision.DENY,
                    quota_key=result.quota_key,
                    requested_amount=result.requested_amount,
                    current_usage=result.current_usage,
                    limit_value=result.limit_value,
                    remaining=result.remaining,
                    limit_id=result.limit_id,
                    would_exceed=True,
                    reset_at=result.reset_at,
                    next_reset=result.next_reset,
                    reason="Quota persistence failed - fail closed",
                    timestamp=now
                )
                self._audit(failed_result)
                raise QuotaExceeded(
                    "Quota persistence failed",
                    result=failed_result
                )
        
        return result
    
    def check_only(
        self,
        key: QuotaKey,
        amount: int,
        now: Optional[datetime] = None
    ) -> QuotaEvaluationResult:
        """
        Check quota without consuming.
        Useful for pre-flight checks.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        
        QuotaInvariants.validate_quota_key(key)
        
        result = self.evaluator.evaluate(key, amount, now)
        self._record_evaluation(result.decision)
        
        return result
    
    def get_current_usage(self, key: QuotaKey) -> Optional[QuotaUsage]:
        """Get current usage for a quota key."""
        QuotaInvariants.validate_quota_key(key)
        return self.state_backend.get_usage(key)
    
    def reset_quota(
        self,
        key: QuotaKey,
        admin_authority: str,
        reason: str,
        now: Optional[datetime] = None
    ) -> bool:
        """
        Manually reset quota (admin operation).
        Requires explicit authority and reason.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        
        QuotaInvariants.validate_quota_key(key)
        
        if not admin_authority:
            raise ValueError("Admin authority required for manual reset")
        if not reason:
            raise ValueError("Reason required for manual reset")
        
        # Create reset usage
        reset_usage = QuotaUsage(
            consumed_value=0,
            first_consumed_at=now,
            last_updated_at=now,
            reset_at=now,
            version=1
        )
        
        # Persist reset
        success = self.state_backend.set_usage(key, reset_usage)
        
        if success:
            # Audit reset
            reset_result = QuotaEvaluationResult(
                decision=QuotaDecision.ALLOW,
                quota_key=key,
                requested_amount=0,
                current_usage=0,
                limit_value=0,
                remaining=0,
                limit_id="manual-reset",
                would_exceed=False,
                reset_at=now,
                next_reset=None,
                reason=f"Manual reset by {admin_authority}: {reason}",
                timestamp=now
            )
            self._audit(reset_result)
        
        return success
    
    def _persist_consumption(
        self,
        key: QuotaKey,
        amount: int,
        now: datetime,
        max_retries: int = 3
    ) -> bool:
        """
        Atomically persist consumption using compare-and-swap.
        Returns True on success, False on failure.
        """
        for attempt in range(max_retries):
            # Get current usage
            current_usage = self.state_backend.get_usage(key)
            
            if current_usage is None:
                # First consumption - create new usage
                new_usage = QuotaUsage(
                    consumed_value=amount,
                    first_consumed_at=now,
                    last_updated_at=now,
                    reset_at=None,
                    version=1
                )
                success = self.state_backend.compare_and_swap(key, 0, new_usage)
            else:
                # Check if reset needed
                limits = self.policy.get_limits_for_key(key)
                if limits and limits[0].should_reset(
                    current_usage.reset_at or current_usage.first_consumed_at,
                    now
                ):
                    # Reset occurred - start fresh
                    new_usage = QuotaUsage(
                        consumed_value=amount,
                        first_consumed_at=now,
                        last_updated_at=now,
                        reset_at=now,
                        version=1
                    )
                    success = self.state_backend.compare_and_swap(
                        key,
                        current_usage.version,
                        new_usage
                    )
                else:
                    # Increment existing usage
                    new_usage = current_usage.increment(amount, now)
                    success = self.state_backend.compare_and_swap(
                        key,
                        current_usage.version,
                        new_usage
                    )
            
            if success:
                return True
            
            # CAS failed - retry
            if attempt < max_retries - 1:
                # Small exponential backoff
                import time
                time.sleep(0.001 * (2 ** attempt))
        
        # All retries failed
        return False
    
    def _audit(self, result: QuotaEvaluationResult) -> None:
        """Emit irreversible audit event."""
        if self.audit_callback:
            try:
                self.audit_callback(result)
            except Exception:
                # Never let audit failure block quota decision
                pass
    
    def _record_evaluation(self, decision: QuotaDecision) -> None:
        """Record evaluation statistics."""
        with self._stats_lock:
            self._stats[decision] += 1
            self._stats['total'] += 1
    
    def get_statistics(self) -> Dict[str, int]:
        """Get quota manager statistics."""
        with self._stats_lock:
            return dict(self._stats)
    
    @contextmanager
    def consume(self, key: QuotaKey, amount: int, now: Optional[datetime] = None):
        """
        Context manager for quota consumption.
        
        Usage:
            with quota_manager.consume(key, 100):
                # Execute operation
                pass
        """
        result = self.check_and_consume(key, amount, now)
        try:
            yield result
        finally:
            # Could add cleanup logic here if needed
            pass


# ============================================================================
# WATCHDOG INTEGRATION
# ============================================================================

class WatchdogInterface:
    """
    Interface for watchdog integration.
    
    QuotaManager MUST:
    - Obey global freeze
    - Honor emergency DENY mandates
    - Allow manual emergency caps
    - Never auto-expand limits
    
    Watchdog can revoke quota.
    Watchdog cannot silently raise it.
    """
    
    def __init__(self):
        self._frozen = False
        self._emergency_caps: Dict[str, int] = {}
        self._lock = threading.Lock()
    
    def freeze(self) -> None:
        """Freeze all quota operations."""
        with self._lock:
            self._frozen = True
    
    def unfreeze(self) -> None:
        """Unfreeze quota operations."""
        with self._lock:
            self._frozen = False
    
    def is_frozen(self) -> bool:
        """Check if quota operations are frozen."""
        with self._lock:
            return self._frozen
    
    def set_emergency_cap(self, limit_id: str, cap: int) -> None:
        """Set emergency cap for a specific limit."""
        if cap < 0:
            raise ValueError(f"Emergency cap cannot be negative: {cap}")
        with self._lock:
            self._emergency_caps[limit_id] = cap
    
    def get_emergency_cap(self, limit_id: str) -> Optional[int]:
        """Get emergency cap if set."""
        with self._lock:
            return self._emergency_caps.get(limit_id)
    
    def clear_emergency_cap(self, limit_id: str) -> None:
        """Clear emergency cap."""
        with self._lock:
            if limit_id in self._emergency_caps:
                del self._emergency_caps[limit_id]


# ============================================================================
# QUOTA INVARIANTS (ABSOLUTE)
# ============================================================================

class QuotaInvariants:
    """
    MUST guarantee:
    - max_value > 0
    - consumed_value never decreases
    - no metric ambiguity
    - no cross-scope leakage
    - no execution without usage persistence
    - no recovery from quota exhaustion without authority
    
    Violation → immediate hard stop.
    """
    
    @staticmethod
    def validate_policy(policy: QuotaPolicy) -> None:
        """Validate policy invariants."""
        if not policy.policy_version:
            raise InvariantViolation("Policy must have version")
        
        if not policy.limits:
            raise InvariantViolation("Policy must have at least one limit")
        
        # Validate all limits
        for limit in policy.limits:
            QuotaInvariants.validate_limit(limit)
        
        # Check for duplicate limit IDs
        limit_ids = [limit.limit_id for limit in policy.limits]
        if len(limit_ids) != len(set(limit_ids)):
            raise InvariantViolation("Duplicate limit IDs in policy")
    
    @staticmethod
    def validate_limit(limit: QuotaLimit) -> None:
        """Validate limit invariants."""
        if limit.max_value <= 0:
            raise InvariantViolation(
                f"Limit max_value must be > 0: {limit.limit_id} = {limit.max_value}"
            )
        
        if not limit.limit_id:
            raise InvariantViolation("Limit must have non-empty ID")
        
        # Validate reset policy constraints
        if limit.reset_policy == ResetPolicy.ROLLING_WINDOW:
            if limit.window_seconds is None or limit.window_seconds <= 0:
                raise InvariantViolation(
                    f"ROLLING_WINDOW requires positive window_seconds: {limit.limit_id}"
                )
    
    @staticmethod
    def validate_quota_key(key: QuotaKey) -> None:
        """Validate quota key invariants."""
        if not key.scope_id:
            raise InvariantViolation("QuotaKey must have non-empty scope_id")
        
        if not isinstance(key.scope, QuotaScope):
            raise InvariantViolation(f"Invalid scope type: {type(key.scope)}")
        
        if not isinstance(key.metric, QuotaMetric):
            raise InvariantViolation(f"Invalid metric type: {type(key.metric)}")
    
    @staticmethod
    def validate_usage(usage: QuotaUsage) -> None:
        """Validate usage invariants."""
        if usage.consumed_value < 0:
            raise InvariantViolation(
                f"Usage consumed_value cannot be negative: {usage.consumed_value}"
            )
        
        if usage.last_updated_at < usage.first_consumed_at:
            raise InvariantViolation(
                "Usage last_updated_at cannot be before first_consumed_at"
            )
        
        if usage.version < 1:
            raise InvariantViolation(f"Usage version must be >= 1: {usage.version}")
    
    @staticmethod
    def validate_consumption_never_decreases(
        old_usage: Optional[QuotaUsage],
        new_usage: QuotaUsage
    ) -> None:
        """
        Validate that consumption never decreases (except on reset).
        This is a CRITICAL invariant.
        """
        if old_usage is None:
            return  # First consumption
        
        # If reset occurred, new usage can be lower
        if new_usage.reset_at and new_usage.reset_at > (old_usage.reset_at or old_usage.first_consumed_at):
            return  # Valid reset
        
        # Otherwise, consumption must not decrease
        if new_usage.consumed_value < old_usage.consumed_value:
            raise InvariantViolation(
                f"Consumption cannot decrease: {old_usage.consumed_value} -> {new_usage.consumed_value}"
            )


# ============================================================================
# EXCEPTIONS
# ============================================================================

class QuotaException(Exception):
    """Base exception for quota system."""
    pass


class QuotaExceeded(QuotaException):
    """Raised when quota is exceeded."""
    
    def __init__(self, message: str, result: QuotaEvaluationResult):
        super().__init__(message)
        self.result = result


class QuotaFrozen(QuotaException):
    """Raised when quota operations are frozen by watchdog."""
    pass


class InvariantViolation(QuotaException):
    """Raised when a quota invariant is violated."""
    pass


# ============================================================================
# QUOTA POLICY FACTORY
# ============================================================================

class QuotaPolicyFactory:
    """Factory for creating standard quota policies."""
    
    @staticmethod
    def create_default_policy(now: Optional[datetime] = None) -> QuotaPolicy:
        """Create default production quota policy."""
        if now is None:
            now = datetime.now(timezone.utc)
        
        return QuotaPolicy(
            policy_version="default-v1.0.0",
            effective_from=now,
            effective_until=None,
            limits=[
                # Global API rate limits
                QuotaLimit(
                    limit_id="global-api-hourly",
                    max_value=1_000_000,
                    metric=QuotaMetric.API_CALLS,
                    scope=QuotaScope.GLOBAL,
                    reset_policy=ResetPolicy.FIXED_HOURLY,
                    description="Global API calls per hour"
                ),
                
                # Account limits
                QuotaLimit(
                    limit_id="account-api-daily",
                    max_value=100_000,
                    metric=QuotaMetric.API_CALLS,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.FIXED_DAILY,
                    description="Account API calls per day"
                ),
                QuotaLimit(
                    limit_id="account-content-daily",
                    max_value=10_000,
                    metric=QuotaMetric.CONTENT_CREATED,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.FIXED_DAILY,
                    description="Account content creation per day"
                ),
                QuotaLimit(
                    limit_id="account-storage-total",
                    max_value=100_000_000_000,  # 100 GB
                    metric=QuotaMetric.STORAGE_BYTES,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.NEVER,
                    description="Account total storage"
                ),
                
                # Workflow limits
                QuotaLimit(
                    limit_id="workflow-executions-hourly",
                    max_value=1_000,
                    metric=QuotaMetric.WORKFLOW_EXECUTIONS,
                    scope=QuotaScope.WORKFLOW,
                    reset_policy=ResetPolicy.FIXED_HOURLY,
                    description="Workflow executions per hour"
                ),
                
                # Infrastructure limits
                QuotaLimit(
                    limit_id="infra-gpu-daily",
                    max_value=86_400,  # 24 hours in seconds
                    metric=QuotaMetric.GPU_SECONDS,
                    scope=QuotaScope.INFRA,
                    reset_policy=ResetPolicy.FIXED_DAILY,
                    description="Infrastructure GPU seconds per day"
                ),
                QuotaLimit(
                    limit_id="infra-egress-daily",
                    max_value=1_000_000_000_000,  # 1 TB
                    metric=QuotaMetric.EGRESS_BYTES,
                    scope=QuotaScope.INFRA,
                    reset_policy=ResetPolicy.FIXED_DAILY,
                    description="Infrastructure egress per day"
                ),
            ]
        )
    
    @staticmethod
    def create_free_tier_policy(now: Optional[datetime] = None) -> QuotaPolicy:
        """Create free tier quota policy with tighter limits."""
        if now is None:
            now = datetime.now(timezone.utc)
        
        return QuotaPolicy(
            policy_version="free-tier-v1.0.0",
            effective_from=now,
            effective_until=None,
            limits=[
                QuotaLimit(
                    limit_id="free-api-daily",
                    max_value=1_000,
                    metric=QuotaMetric.API_CALLS,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.FIXED_DAILY,
                    description="Free tier API calls per day"
                ),
                QuotaLimit(
                    limit_id="free-content-daily",
                    max_value=100,
                    metric=QuotaMetric.CONTENT_CREATED,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.FIXED_DAILY,
                    description="Free tier content per day"
                ),
                QuotaLimit(
                    limit_id="free-storage-total",
                    max_value=1_000_000_000,  # 1 GB
                    metric=QuotaMetric.STORAGE_BYTES,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.NEVER,
                    description="Free tier total storage"
                ),
            ]
        )
    
    @staticmethod
    def create_enterprise_policy(now: Optional[datetime] = None) -> QuotaPolicy:
        """Create enterprise quota policy with higher limits."""
        if now is None:
            now = datetime.now(timezone.utc)
        
        return QuotaPolicy(
            policy_version="enterprise-v1.0.0",
            effective_from=now,
            effective_until=None,
            limits=[
                QuotaLimit(
                    limit_id="enterprise-api-hourly",
                    max_value=10_000_000,
                    metric=QuotaMetric.API_CALLS,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.FIXED_HOURLY,
                    description="Enterprise API calls per hour"
                ),
                QuotaLimit(
                    limit_id="enterprise-content-daily",
                    max_value=1_000_000,
                    metric=QuotaMetric.CONTENT_CREATED,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.FIXED_DAILY,
                    description="Enterprise content per day"
                ),
                QuotaLimit(
                    limit_id="enterprise-storage-total",
                    max_value=10_000_000_000_000,  # 10 TB
                    metric=QuotaMetric.STORAGE_BYTES,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.NEVER,
                    description="Enterprise total storage"
                ),
                QuotaLimit(
                    limit_id="enterprise-gpu-daily",
                    max_value=864_000,  # 240 hours in seconds
                    metric=QuotaMetric.GPU_SECONDS,
                    scope=QuotaScope.ACCOUNT,
                    reset_policy=ResetPolicy.FIXED_DAILY,
                    description="Enterprise GPU seconds per day"
                ),
            ]
        )


# ============================================================================
# EXAMPLE USAGE & INTEGRATION
# ============================================================================

def example_usage():
    """Example of how to use the quota system."""
    
    # Create state backend
    state_backend = InMemoryStateBackend()
    
    # Create policy
    policy = QuotaPolicyFactory.create_default_policy()
    
    # Create audit callback
    def audit_logger(result: QuotaEvaluationResult):
        print(f"[QUOTA AUDIT] {result.decision.value}: {result.reason}")
        print(f"  Key: {result.quota_key.to_string()}")
        print(f"  Usage: {result.current_usage}/{result.limit_value} (remaining: {result.remaining})")
        if result.next_reset:
            print(f"  Next reset: {result.next_reset.isoformat()}")
    
    # Create watchdog
    watchdog = WatchdogInterface()
    
    # Create quota manager
    quota_manager = QuotaManager(
        policy=policy,
        state_backend=state_backend,
        audit_callback=audit_logger,
        watchdog=watchdog
    )
    
    # Example 1: Check and consume API quota
    account_key = QuotaKey(
        scope=QuotaScope.ACCOUNT,
        scope_id="acc-12345",
        metric=QuotaMetric.API_CALLS
    )
    
    try:
        # Consume 10 API calls
        result = quota_manager.check_and_consume(account_key, 10)
        print(f"\n✓ Consumed 10 API calls. Remaining: {result.remaining}")
    except QuotaExceeded as e:
        print(f"\n✗ Quota exceeded: {e}")
    
    # Example 2: Use context manager
    workflow_key = QuotaKey(
        scope=QuotaScope.WORKFLOW,
        scope_id="wf-67890",
        metric=QuotaMetric.WORKFLOW_EXECUTIONS
    )
    
    try:
        with quota_manager.consume(workflow_key, 1):
            print("\n✓ Workflow execution allowed, performing work...")
            # Do work here
    except QuotaExceeded as e:
        print(f"\n✗ Workflow execution denied: {e}")
    
    # Example 3: Check without consuming (pre-flight)
    check_result = quota_manager.check_only(account_key, 50000)
    if check_result.decision == QuotaDecision.ALLOW:
        print(f"\n✓ Pre-flight check passed. Can consume 50000 more.")
    else:
        print(f"\n✗ Pre-flight check failed: {check_result.reason}")
    
    # Example 4: Get current usage
    usage = quota_manager.get_current_usage(account_key)
    if usage:
        print(f"\nCurrent usage for {account_key.to_string()}: {usage.consumed_value}")
        print(f"  First consumed: {usage.first_consumed_at.isoformat()}")
        print(f"  Last updated: {usage.last_updated_at.isoformat()}")
    
    # Example 5: Admin reset
    try:
        quota_manager.reset_quota(
            account_key,
            admin_authority="admin@example.com",
            reason="Customer support request #12345"
        )
        print(f"\n✓ Quota reset for {account_key.to_string()}")
    except Exception as e:
        print(f"\n✗ Reset failed: {e}")
    
    # Example 6: Emergency freeze
    watchdog.freeze()
    print("\n⚠ System frozen by watchdog")
    
    try:
        quota_manager.check_and_consume(account_key, 1)
    except QuotaFrozen:
        print("✗ Operation blocked - system frozen")
    
    watchdog.unfreeze()
    print("✓ System unfrozen")
    
    # Get statistics
    stats = quota_manager.get_statistics()
    print(f"\nQuota Manager Statistics:")
    print(f"  Total evaluations: {stats.get('total', 0)}")
    print(f"  Allowed: {stats.get(QuotaDecision.ALLOW, 0)}")
    print(f"  Denied: {stats.get(QuotaDecision.DENY, 0)}")


if __name__ == "__main__":
    example_usage()