"""
rate_limiter.py - Deterministic Rate Enforcement Authority

Built for:
- Velocity control (how fast)
- Deterministic windows (fixed or sliding)
- Replay-safe execution
- Zero forgiveness on limits
- Audit-complete decisions
- Watchdog-enforced boundaries

NO GUESSING. NO SMOOTHING. NO ML. NO FORGIVENESS.

What this file ACTUALLY is:
"Are you allowed to perform this action at this speed right now?"

It enforces velocity, not volume, not priority, not degradation.

Authority chain: quota → rate → backpressure → execution
Rate limits are evaluated after quota, before backpressure.
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple, Callable, Any
from datetime import datetime, timezone
from abc import ABC, abstractmethod
import threading
import time
from contextlib import contextmanager
from collections import defaultdict
import hashlib


# ============================================================================
# CORE ENUMS (STRICT)
# ============================================================================

class RateScope(Enum):
    """
    Scopes do not overlap implicitly.
    Each scope is independently rate-limited.
    """
    GLOBAL = "global"
    ACCOUNT = "account"
    WORKFLOW = "workflow"
    JOB = "job"
    ENDPOINT = "endpoint"
    RESOURCE = "resource"


class RateDecision(Enum):
    """
    No throttling here.
    Only permission.
    Binary finality.
    """
    ALLOW = "allow"
    DENY = "deny"


class WindowType(Enum):
    """
    Window calculation strategy.
    Declared, never inferred.
    """
    FIXED = "fixed"        # Reset at window boundary
    SLIDING = "sliding"    # Rolling time window


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class RateLimitKey:
    """
    Fully qualified identifier for a rate limit instance.
    Keys must be fully specified — no wildcards.
    """
    scope: RateScope
    scope_id: Optional[str]  # None only for GLOBAL scope
    action_type: str
    
    def __post_init__(self):
        if self.scope == RateScope.GLOBAL and self.scope_id is not None:
            raise ValueError("GLOBAL scope must have scope_id=None")
        if self.scope != RateScope.GLOBAL and not self.scope_id:
            raise ValueError(f"Scope {self.scope.value} requires non-null scope_id")
        if not self.action_type:
            raise ValueError("action_type cannot be empty")
    
    def to_string(self) -> str:
        """Convert to canonical string representation."""
        scope_id = self.scope_id or "global"
        return f"{self.scope.value}:{scope_id}:{self.action_type}"
    
    def to_hash(self) -> str:
        """Generate deterministic hash for storage key."""
        return hashlib.sha256(self.to_string().encode()).hexdigest()[:16]
    
    @classmethod
    def from_string(cls, key_str: str) -> 'RateLimitKey':
        """Parse from canonical string representation."""
        parts = key_str.split(':', 2)
        if len(parts) != 3:
            raise ValueError(f"Invalid rate limit key string: {key_str}")
        
        scope = RateScope(parts[0])
        scope_id = None if parts[1] == "global" else parts[1]
        action_type = parts[2]
        
        return cls(scope=scope, scope_id=scope_id, action_type=action_type)


@dataclass
class RateLimitState:
    """
    State tracking for rate limit window.
    State is monotonic inside a window.
    """
    window_start: int  # Unix timestamp in seconds
    event_count: int
    window_type: WindowType
    events_timestamps: List[int] = field(default_factory=list)  # For sliding window
    version: int = 1  # For optimistic locking
    
    def __post_init__(self):
        if self.window_start < 0:
            raise ValueError(f"window_start cannot be negative: {self.window_start}")
        if self.event_count < 0:
            raise ValueError(f"event_count cannot be negative: {self.event_count}")
        if self.version < 1:
            raise ValueError(f"version must be >= 1: {self.version}")
        
        # For sliding window, timestamps must be sorted
        if self.window_type == WindowType.SLIDING and self.events_timestamps:
            if self.events_timestamps != sorted(self.events_timestamps):
                raise ValueError("events_timestamps must be sorted for sliding window")
    
    def increment(self, now: int) -> 'RateLimitState':
        """
        Create new state with incremented count.
        Returns new instance - original is immutable for sliding window.
        """
        if self.window_type == WindowType.FIXED:
            return RateLimitState(
                window_start=self.window_start,
                event_count=self.event_count + 1,
                window_type=self.window_type,
                events_timestamps=[],
                version=self.version + 1
            )
        else:  # SLIDING
            new_timestamps = self.events_timestamps + [now]
            return RateLimitState(
                window_start=self.window_start,
                event_count=len(new_timestamps),
                window_type=self.window_type,
                events_timestamps=new_timestamps,
                version=self.version + 1
            )
    
    def reset_window(self, now: int) -> 'RateLimitState':
        """Create new state for a fresh window."""
        return RateLimitState(
            window_start=now,
            event_count=0,
            window_type=self.window_type,
            events_timestamps=[],
            version=1
        )
    
    def prune_old_events(self, window_seconds: int, now: int) -> 'RateLimitState':
        """
        For sliding window: remove events outside the window.
        Returns new state with pruned events.
        """
        if self.window_type != WindowType.SLIDING:
            return self
        
        cutoff = now - window_seconds
        new_timestamps = [ts for ts in self.events_timestamps if ts > cutoff]
        
        return RateLimitState(
            window_start=self.window_start,
            event_count=len(new_timestamps),
            window_type=self.window_type,
            events_timestamps=new_timestamps,
            version=self.version + 1
        )
    
    def to_dict(self) -> Dict:
        """Serialize to dictionary."""
        return {
            'window_start': self.window_start,
            'event_count': self.event_count,
            'window_type': self.window_type.value,
            'events_timestamps': self.events_timestamps,
            'version': self.version
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'RateLimitState':
        """Deserialize from dictionary."""
        return cls(
            window_start=data['window_start'],
            event_count=data['event_count'],
            window_type=WindowType(data['window_type']),
            events_timestamps=data.get('events_timestamps', []),
            version=data.get('version', 1)
        )


@dataclass(frozen=True)
class RateLimit:
    """
    Immutable rate limit definition.
    Limits are declared, never inferred.
    """
    limit_id: str
    max_events: int
    window_seconds: int
    scope: RateScope
    window_type: WindowType = WindowType.FIXED
    action_types: List[str] = field(default_factory=list)  # Empty = applies to all
    description: str = ""
    
    def __post_init__(self):
        if self.max_events <= 0:
            raise ValueError(f"max_events must be > 0: {self.max_events}")
        if self.window_seconds <= 0:
            raise ValueError(f"window_seconds must be > 0: {self.window_seconds}")
        if not self.limit_id:
            raise ValueError("limit_id cannot be empty")
    
    def applies_to(self, key: RateLimitKey) -> bool:
        """Check if this limit applies to the given key."""
        if self.scope != key.scope:
            return False
        
        # If action_types is empty, applies to all actions
        if not self.action_types:
            return True
        
        return key.action_type in self.action_types
    
    def get_window_start(self, now: int) -> int:
        """
        Calculate window start time for the current moment.
        For fixed window: align to window boundary.
        For sliding window: use current time.
        """
        if self.window_type == WindowType.FIXED:
            # Align to window boundary
            return (now // self.window_seconds) * self.window_seconds
        else:  # SLIDING
            # Sliding window doesn't have a fixed start
            return now - self.window_seconds
    
    def should_reset_window(self, state: RateLimitState, now: int) -> bool:
        """
        Determine if window should be reset.
        Only relevant for fixed windows.
        """
        if self.window_type == WindowType.SLIDING:
            return False  # Sliding windows don't reset
        
        current_window_start = self.get_window_start(now)
        return state.window_start < current_window_start


@dataclass(frozen=True)
class RatePolicy:
    """
    Collection of rate limits.
    Policies are versioned and immutable.
    """
    policy_version: str
    limits: List[RateLimit]
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
    
    def get_limits_for_key(self, key: RateLimitKey) -> List[RateLimit]:
        """Get all limits applicable to a rate limit key."""
        return [limit for limit in self.limits if limit.applies_to(key)]
    
    def is_active(self, now: datetime) -> bool:
        """Check if policy is currently active."""
        if now < self.effective_from:
            return False
        if self.effective_until and now >= self.effective_until:
            return False
        return True


@dataclass
class RateEvaluationResult:
    """Result of rate limit evaluation."""
    decision: RateDecision
    rate_key: RateLimitKey
    current_count: int
    limit_value: int
    window_seconds: int
    remaining: int
    limit_id: str
    would_exceed: bool
    window_start: int
    window_end: int
    retry_after_seconds: Optional[int]
    reason: str
    timestamp: int
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for audit."""
        return {
            'decision': self.decision.value,
            'rate_key': self.rate_key.to_string(),
            'current_count': self.current_count,
            'limit_value': self.limit_value,
            'window_seconds': self.window_seconds,
            'remaining': self.remaining,
            'limit_id': self.limit_id,
            'would_exceed': self.would_exceed,
            'window_start': self.window_start,
            'window_end': self.window_end,
            'retry_after_seconds': self.retry_after_seconds,
            'reason': self.reason,
            'timestamp': self.timestamp
        }


# ============================================================================
# STATE BACKEND INTERFACE
# ============================================================================

class RateStateBackend(ABC):
    """
    Abstract interface for rate limit state persistence.
    Implementations must provide atomicity guarantees.
    """
    
    @abstractmethod
    def get_state(self, key: RateLimitKey) -> Optional[RateLimitState]:
        """Retrieve current state for a rate limit key."""
        pass
    
    @abstractmethod
    def set_state(self, key: RateLimitKey, state: RateLimitState) -> bool:
        """
        Atomically set state for a rate limit key.
        Returns True on success, False on failure.
        """
        pass
    
    @abstractmethod
    def compare_and_swap(
        self,
        key: RateLimitKey,
        expected_version: int,
        new_state: RateLimitState
    ) -> bool:
        """
        Atomically update state if version matches.
        Returns True on success, False on version mismatch.
        """
        pass
    
    @abstractmethod
    def batch_get_state(self, keys: List[RateLimitKey]) -> Dict[RateLimitKey, Optional[RateLimitState]]:
        """Retrieve state for multiple keys efficiently."""
        pass


class InMemoryRateStateBackend(RateStateBackend):
    """
    In-memory implementation for testing and single-node deployments.
    NOT suitable for distributed systems.
    """
    
    def __init__(self):
        self._storage: Dict[str, RateLimitState] = {}
        self._lock = threading.Lock()
    
    def get_state(self, key: RateLimitKey) -> Optional[RateLimitState]:
        with self._lock:
            return self._storage.get(key.to_hash())
    
    def set_state(self, key: RateLimitKey, state: RateLimitState) -> bool:
        with self._lock:
            self._storage[key.to_hash()] = state
            return True
    
    def compare_and_swap(
        self,
        key: RateLimitKey,
        expected_version: int,
        new_state: RateLimitState
    ) -> bool:
        with self._lock:
            current = self._storage.get(key.to_hash())
            if current is None and expected_version == 0:
                # First write
                self._storage[key.to_hash()] = new_state
                return True
            if current and current.version == expected_version:
                self._storage[key.to_hash()] = new_state
                return True
            return False
    
    def batch_get_state(self, keys: List[RateLimitKey]) -> Dict[RateLimitKey, Optional[RateLimitState]]:
        with self._lock:
            return {key: self._storage.get(key.to_hash()) for key in keys}


# ============================================================================
# RATE EVALUATOR (DECISION LOGIC)
# ============================================================================

class RateEvaluator:
    """
    Deterministic rate limit evaluation engine.
    
    Guarantees:
    - Deterministic window math
    - Strict integer arithmetic
    - Exact boundary handling
    - Fail-closed on missing state
    
    Same inputs → same decision. Always.
    """
    
    def __init__(self, policy: RatePolicy):
        self.policy = policy
        self._eval_lock = threading.Lock()
    
    def evaluate(
        self,
        key: RateLimitKey,
        state: Optional[RateLimitState],
        now: int
    ) -> RateEvaluationResult:
        """
        Evaluate rate limit for a single request.
        
        Returns evaluation result with decision and details.
        """
        with self._eval_lock:
            return self._evaluate_internal(key, state, now)
    
    def _evaluate_internal(
        self,
        key: RateLimitKey,
        state: Optional[RateLimitState],
        now: int
    ) -> RateEvaluationResult:
        """Internal evaluation logic."""
        
        # Get applicable limits
        limits = self.policy.get_limits_for_key(key)
        
        if not limits:
            # No limits defined - fail closed
            return RateEvaluationResult(
                decision=RateDecision.DENY,
                rate_key=key,
                current_count=0,
                limit_value=0,
                window_seconds=0,
                remaining=0,
                limit_id="none",
                would_exceed=True,
                window_start=now,
                window_end=now,
                retry_after_seconds=None,
                reason="No rate limit defined for this key",
                timestamp=now
            )
        
        # Evaluate against all applicable limits
        # ANY limit exceeded = DENY
        results = []
        for limit in limits:
            result = self._evaluate_against_limit(key, state, limit, now)
            results.append(result)
            if result.decision == RateDecision.DENY:
                return result  # Return first denial
        
        # All limits passed - return most restrictive result
        return min(results, key=lambda r: r.remaining)
    
    def _evaluate_against_limit(
        self,
        key: RateLimitKey,
        state: Optional[RateLimitState],
        limit: RateLimit,
        now: int
    ) -> RateEvaluationResult:
        """Evaluate against a specific limit."""
        
        # Calculate window boundaries
        window_start = limit.get_window_start(now)
        window_end = window_start + limit.window_seconds
        
        # Determine current count
        if state is None:
            # No previous state - first request
            current_count = 0
        elif limit.window_type == WindowType.FIXED:
            # Fixed window - check if we need to reset
            if limit.should_reset_window(state, now):
                current_count = 0
            else:
                current_count = state.event_count
        else:  # SLIDING
            # Sliding window - count events within window
            cutoff = now - limit.window_seconds
            current_count = sum(1 for ts in state.events_timestamps if ts > cutoff)
        
        # Calculate projected count (after this request)
        projected_count = current_count + 1
        
        # Check against limit
        would_exceed = projected_count > limit.max_events
        remaining = max(0, limit.max_events - current_count)
        
        # Calculate retry_after for sliding window
        retry_after = None
        if would_exceed and limit.window_type == WindowType.SLIDING and state:
            # Find oldest event in window
            cutoff = now - limit.window_seconds
            events_in_window = [ts for ts in state.events_timestamps if ts > cutoff]
            if events_in_window:
                oldest_event = min(events_in_window)
                # Retry after oldest event expires from window
                retry_after = (oldest_event + limit.window_seconds) - now
        elif would_exceed and limit.window_type == WindowType.FIXED:
            # For fixed window, retry after window resets
            retry_after = window_end - now
        
        # Determine decision
        if would_exceed:
            decision = RateDecision.DENY
            reason = f"Rate limit exceeded: {projected_count} > {limit.max_events} in {limit.window_seconds}s ({limit.limit_id})"
        else:
            decision = RateDecision.ALLOW
            reason = f"Rate limit ok: {projected_count} <= {limit.max_events} in {limit.window_seconds}s ({limit.limit_id})"
        
        return RateEvaluationResult(
            decision=decision,
            rate_key=key,
            current_count=current_count,
            limit_value=limit.max_events,
            window_seconds=limit.window_seconds,
            remaining=remaining,
            limit_id=limit.limit_id,
            would_exceed=would_exceed,
            window_start=window_start,
            window_end=window_end,
            retry_after_seconds=retry_after,
            reason=reason,
            timestamp=now
        )


# ============================================================================
# RATE LIMITER (PUBLIC AUTHORITY)
# ============================================================================

class RateLimiter:
    """
    Public authority for rate limit enforcement.
    
    Responsibilities:
    - Load correct policy
    - Fetch state from persistence
    - Invoke evaluator
    - Update state atomically
    - Emit audit event
    
    No side effects beyond state + logs.
    """
    
    def __init__(
        self,
        policy: RatePolicy,
        state_backend: RateStateBackend,
        audit_callback: Optional[Callable[[RateEvaluationResult], None]] = None,
        watchdog: Optional['WatchdogInterface'] = None,
        clock: Optional[Callable[[], int]] = None
    ):
        self.policy = policy
        self.state_backend = state_backend
        self.audit_callback = audit_callback
        self.watchdog = watchdog
        self.clock = clock or self._default_clock
        self.evaluator = RateEvaluator(policy)
        
        # Statistics
        self._stats = defaultdict(int)
        self._stats_lock = threading.Lock()
        
        # Validate policy
        RateLimitInvariants.validate_policy(policy)
    
    @staticmethod
    def _default_clock() -> int:
        """Default clock using current UTC time."""
        return int(time.time())
    
    def check(self, key: RateLimitKey, now: Optional[int] = None) -> RateEvaluationResult:
        """
        Check rate limit and update state if allowed.
        
        Returns evaluation result.
        Raises RateLimitExceeded if denied.
        """
        if now is None:
            now = self.clock()
        
        # Validate inputs
        RateLimitInvariants.validate_rate_key(key)
        
        # Check watchdog freeze
        if self.watchdog and self.watchdog.is_frozen():
            raise RateLimitFrozen("System frozen by watchdog")
        
        # Get current state
        state = self.state_backend.get_state(key)
        
        # Apply watchdog overrides
        if self.watchdog and self.watchdog.should_deny(key):
            denied_result = RateEvaluationResult(
                decision=RateDecision.DENY,
                rate_key=key,
                current_count=state.event_count if state else 0,
                limit_value=0,
                window_seconds=0,
                remaining=0,
                limit_id="watchdog-override",
                would_exceed=True,
                window_start=now,
                window_end=now,
                retry_after_seconds=None,
                reason="Denied by watchdog override",
                timestamp=now
            )
            self._audit(denied_result)
            raise RateLimitExceeded(
                "Rate limit denied by watchdog",
                result=denied_result
            )
        
        # Evaluate rate limit
        result = self.evaluator.evaluate(key, state, now)
        
        # Record statistics
        self._record_evaluation(result.decision)
        
        # Audit (always, regardless of decision)
        self._audit(result)
        
        # If denied, raise exception
        if result.decision == RateDecision.DENY:
            raise RateLimitExceeded(
                f"Rate limit exceeded: {result.reason}",
                result=result
            )
        
        # If allowed, persist state update
        success = self._persist_increment(key, state, now)
        if not success:
            # Persistence failed - convert to DENY
            failed_result = RateEvaluationResult(
                decision=RateDecision.DENY,
                rate_key=result.rate_key,
                current_count=result.current_count,
                limit_value=result.limit_value,
                window_seconds=result.window_seconds,
                remaining=result.remaining,
                limit_id=result.limit_id,
                would_exceed=True,
                window_start=result.window_start,
                window_end=result.window_end,
                retry_after_seconds=result.retry_after_seconds,
                reason="Rate limit state persistence failed - fail closed",
                timestamp=now
            )
            self._audit(failed_result)
            raise RateLimitExceeded(
                "Rate limit state persistence failed",
                result=failed_result
            )
        
        return result
    
    def check_only(self, key: RateLimitKey, now: Optional[int] = None) -> RateEvaluationResult:
        """
        Check rate limit without updating state.
        Useful for pre-flight checks.
        """
        if now is None:
            now = self.clock()
        
        RateLimitInvariants.validate_rate_key(key)
        
        state = self.state_backend.get_state(key)
        result = self.evaluator.evaluate(key, state, now)
        self._record_evaluation(result.decision)
        
        return result
    
    def get_current_state(self, key: RateLimitKey) -> Optional[RateLimitState]:
        """Get current state for a rate limit key."""
        RateLimitInvariants.validate_rate_key(key)
        return self.state_backend.get_state(key)
    
    def reset_state(
        self,
        key: RateLimitKey,
        admin_authority: str,
        reason: str,
        now: Optional[int] = None
    ) -> bool:
        """
        Manually reset rate limit state (admin operation).
        Requires explicit authority and reason.
        """
        if now is None:
            now = self.clock()
        
        RateLimitInvariants.validate_rate_key(key)
        
        if not admin_authority:
            raise ValueError("Admin authority required for manual reset")
        if not reason:
            raise ValueError("Reason required for manual reset")
        
        # Determine window type from policy
        limits = self.policy.get_limits_for_key(key)
        if not limits:
            return False
        
        window_type = limits[0].window_type
        
        # Create reset state
        reset_state = RateLimitState(
            window_start=now,
            event_count=0,
            window_type=window_type,
            events_timestamps=[],
            version=1
        )
        
        # Persist reset
        success = self.state_backend.set_state(key, reset_state)
        
        if success:
            # Audit reset
            reset_result = RateEvaluationResult(
                decision=RateDecision.ALLOW,
                rate_key=key,
                current_count=0,
                limit_value=0,
                window_seconds=0,
                remaining=0,
                limit_id="manual-reset",
                would_exceed=False,
                window_start=now,
                window_end=now,
                retry_after_seconds=None,
                reason=f"Manual reset by {admin_authority}: {reason}",
                timestamp=now
            )
            self._audit(reset_result)
        
        return success
    
    def _persist_increment(
        self,
        key: RateLimitKey,
        current_state: Optional[RateLimitState],
        now: int,
        max_retries: int = 3
    ) -> bool:
        """
        Atomically persist state increment using compare-and-swap.
        Returns True on success, False on failure.
        """
        # Get applicable limits to determine window type
        limits = self.policy.get_limits_for_key(key)
        if not limits:
            return False
        
        limit = limits[0]  # Use first limit for state management
        
        for attempt in range(max_retries):
            if current_state is None:
                # First request - create new state
                new_state = RateLimitState(
                    window_start=limit.get_window_start(now),
                    event_count=1,
                    window_type=limit.window_type,
                    events_timestamps=[now] if limit.window_type == WindowType.SLIDING else [],
                    version=1
                )
                success = self.state_backend.compare_and_swap(key, 0, new_state)
            else:
                # Check if window should reset (fixed window only)
                if limit.should_reset_window(current_state, now):
                    new_state = RateLimitState(
                        window_start=limit.get_window_start(now),
                        event_count=1,
                        window_type=limit.window_type,
                        events_timestamps=[now] if limit.window_type == WindowType.SLIDING else [],
                        version=1
                    )
                    success = self.state_backend.compare_and_swap(
                        key,
                        current_state.version,
                        new_state
                    )
                else:
                    # Increment existing state
                    if limit.window_type == WindowType.SLIDING:
                        # Prune old events first
                        pruned_state = current_state.prune_old_events(limit.window_seconds, now)
                        new_state = pruned_state.increment(now)
                    else:
                        new_state = current_state.increment(now)
                    
                    success = self.state_backend.compare_and_swap(
                        key,
                        current_state.version,
                        new_state
                    )
            
            if success:
                return True
            
            # CAS failed - retry with fresh state
            current_state = self.state_backend.get_state(key)
            
            if attempt < max_retries - 1:
                # Small exponential backoff
                time.sleep(0.001 * (2 ** attempt))
        
        # All retries failed
        return False
    
    def _audit(self, result: RateEvaluationResult) -> None:
        """Emit audit event."""
        if self.audit_callback:
            try:
                self.audit_callback(result)
            except Exception:
                # Never let audit failure block rate limiting
                pass
    
    def _record_evaluation(self, decision: RateDecision) -> None:
        """Record evaluation statistics."""
        with self._stats_lock:
            self._stats[decision] += 1
            self._stats['total'] += 1
    
    def get_statistics(self) -> Dict[str, int]:
        """Get rate limiter statistics."""
        with self._stats_lock:
            return dict(self._stats)
    
    @contextmanager
    def limit(self, key: RateLimitKey, now: Optional[int] = None):
        """
        Context manager for rate limiting.
        
        Usage:
            with rate_limiter.limit(key):
                # Execute rate-limited operation
                pass
        """
        result = self.check(key, now)
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
    
    RateLimiter MUST:
    - Respect global freeze
    - Honor emergency bypass rules
    - Escalate sustained denials
    - Never self-disable
    
    Watchdog can override only to deny, never to allow.
    """
    
    def __init__(self):
        self._frozen = False
        self._deny_overrides: Set[str] = set()  # Rate limit key patterns to always deny
        self._lock = threading.Lock()
        self._denial_counts: Dict[str, int] = defaultdict(int)
    
    def freeze(self) -> None:
        """Freeze all rate limit operations."""
        with self._lock:
            self._frozen = True
    
    def unfreeze(self) -> None:
        """Unfreeze rate limit operations."""
        with self._lock:
            self._frozen = False
    
    def is_frozen(self) -> bool:
        """Check if rate limit operations are frozen."""
        with self._lock:
            return self._frozen
    
    def add_deny_override(self, key_pattern: str) -> None:
        """Add a pattern for rate limit keys to always deny."""
        with self._lock:
            self._deny_overrides.add(key_pattern)
    
    def remove_deny_override(self, key_pattern: str) -> None:
        """Remove a deny override pattern."""
        with self._lock:
            self._deny_overrides.discard(key_pattern)
    
    def should_deny(self, key: RateLimitKey) -> bool:
        """Check if this key should be denied by watchdog override."""
        with self._lock:
            key_str = key.to_string()
            for pattern in self._deny_overrides:
                if pattern in key_str:
                    return True
            return False
    
    def record_denial(self, key: RateLimitKey) -> None:
        """Record a rate limit denial for escalation tracking."""
        with self._lock:
            self._denial_counts[key.to_string()] += 1
    
    def get_denial_count(self, key: RateLimitKey) -> int:
        """Get denial count for a key."""
        with self._lock:
            return self._denial_counts.get(key.to_string(), 0)


# ============================================================================
# RATE LIMIT INVARIANTS (ABSOLUTE)
# ============================================================================

class RateLimitInvariants:
    """
    MUST enforce:
    - max_events > 0
    - window_seconds > 0
    - no overlapping limits for same key
    - no negative counters
    - no time travel across windows
    - no implicit bursts
    
    Violation → hard stop.
    """
    
    @staticmethod
    def validate_policy(policy: RatePolicy) -> None:
        """Validate policy invariants."""
        if not policy.policy_version:
            raise InvariantViolation("Policy must have version")
        
        if not policy.limits:
            raise InvariantViolation("Policy must have at least one limit")
        
        # Validate all limits
        for limit in policy.limits:
            RateLimitInvariants.validate_limit(limit)
        
        # Check for duplicate limit IDs
        limit_ids = [limit.limit_id for limit in policy.limits]
        if len(limit_ids) != len(set(limit_ids)):
            raise InvariantViolation("Duplicate limit IDs in policy")
        
        # Check for overlapping limits (same scope + same action types)
        for i, limit1 in enumerate(policy.limits):
            for limit2 in policy.limits[i+1:]:
                if limit1.scope == limit2.scope:
                    # Check if action types overlap
                    if not limit1.action_types and not limit2.action_types:
                        raise InvariantViolation(
                            f"Overlapping limits: {limit1.limit_id} and {limit2.limit_id} "
                            "both apply to all action types"
                        )
                    if not limit1.action_types or not limit2.action_types:
                        # One applies to all, one is specific - could overlap
                        continue
                    
                    overlap = set(limit1.action_types) & set(limit2.action_types)
                    if overlap:
                        raise InvariantViolation(
                            f"Overlapping limits: {limit1.limit_id} and {limit2.limit_id} "
                            f"both apply to actions: {overlap}"
                        )
    
    @staticmethod
    def validate_limit(limit: RateLimit) -> None:
        """Validate limit invariants."""
        if limit.max_events <= 0:
            raise InvariantViolation(
                f"Limit max_events must be > 0: {limit.limit_id} = {limit.max_events}"
            )
        
        if limit.window_seconds <= 0:
            raise InvariantViolation(
                f"Limit window_seconds must be > 0: {limit.limit_id} = {limit.window_seconds}"
            )
        
        if not limit.limit_id:
            raise InvariantViolation("Limit must have non-empty ID")
    
    @staticmethod
    def validate_rate_key(key: RateLimitKey) -> None:
        """Validate rate limit key invariants."""
        if key.scope == RateScope.GLOBAL and key.scope_id is not None:
            raise InvariantViolation("GLOBAL scope must have scope_id=None")
        
        if key.scope != RateScope.GLOBAL and not key.scope_id:
            raise InvariantViolation(f"Scope {key.scope.value} requires non-null scope_id")
        
        if not key.action_type:
            raise InvariantViolation("action_type cannot be empty")
    
    @staticmethod
    def validate_state(state: RateLimitState) -> None:
        """Validate state invariants."""
        if state.window_start < 0:
            raise InvariantViolation(
                f"State window_start cannot be negative: {state.window_start}"
            )
        
        if state.event_count < 0:
            raise InvariantViolation(
                f"State event_count cannot be negative: {state.event_count}"
            )
        
        if state.version < 1:
            raise InvariantViolation(f"State version must be >= 1: {state.version}")
        
        # For sliding window, validate timestamps
        if state.window_type == WindowType.SLIDING:
            if state.events_timestamps:
                # Must be sorted
                if state.events_timestamps != sorted(state.events_timestamps):
                    raise InvariantViolation("Sliding window timestamps must be sorted")
                
                # Count must match
                if len(state.events_timestamps) != state.event_count:
                    raise InvariantViolation(
                        f"Sliding window event_count mismatch: "
                        f"{state.event_count} != {len(state.events_timestamps)}"
                    )
    
    @staticmethod
    def validate_no_time_travel(old_state: Optional[RateLimitState], new_state: RateLimitState) -> None:
        """Validate that window boundaries don't go backwards."""
        if old_state is None:
            return  # First state
        
        # For fixed window, window_start can only stay same or move forward
        if old_state.window_type == WindowType.FIXED:
            if new_state.window_start < old_state.window_start:
                raise InvariantViolation(
                    f"Time travel detected: window_start went backwards "
                    f"{old_state.window_start} -> {new_state.window_start}"
                )


# ============================================================================
# EXCEPTIONS
# ============================================================================

class RateLimitException(Exception):
    """Base exception for rate limiting system."""
    pass


class RateLimitExceeded(RateLimitException):
    """Raised when rate limit is exceeded."""
    
    def __init__(self, message: str, result: RateEvaluationResult):
        super().__init__(message)
        self.result = result


class RateLimitFrozen(RateLimitException):
    """Raised when rate limit operations are frozen by watchdog."""
    pass


class InvariantViolation(RateLimitException):
    """Raised when a rate limit invariant is violated."""
    pass


# ============================================================================
# RATE POLICY FACTORY
# ============================================================================

class RatePolicyFactory:
    """Factory for creating standard rate limit policies."""
    
    @staticmethod
    def create_default_policy(now: Optional[datetime] = None) -> RatePolicy:
        """Create default production rate limit policy."""
        if now is None:
            now = datetime.now(timezone.utc)
        
        return RatePolicy(
            policy_version="default-v1.0.0",
            effective_from=now,
            effective_until=None,
            limits=[
                # Global limits
                RateLimit(
                    limit_id="global-api-per-second",
                    max_events=10000,
                    window_seconds=1,
                    scope=RateScope.GLOBAL,
                    window_type=WindowType.FIXED,
                    description="Global API calls per second"
                ),
                RateLimit(
                    limit_id="global-api-per-minute",
                    max_events=500000,
                    window_seconds=60,
                    scope=RateScope.GLOBAL,
                    window_type=WindowType.SLIDING,
                    description="Global API calls per minute (sliding)"
                ),
                
                # Account limits
                RateLimit(
                    limit_id="account-api-per-second",
                    max_events=100,
                    window_seconds=1,
                    scope=RateScope.ACCOUNT,
                    window_type=WindowType.FIXED,
                    description="Account API calls per second"
                ),
                RateLimit(
                    limit_id="account-api-per-minute",
                    max_events=5000,
                    window_seconds=60,
                    scope=RateScope.ACCOUNT,
                    window_type=WindowType.SLIDING,
                    description="Account API calls per minute (sliding)"
                ),
                RateLimit(
                    limit_id="account-post-per-minute",
                    max_events=100,
                    window_seconds=60,
                    scope=RateScope.ACCOUNT,
                    window_type=WindowType.FIXED,
                    action_types=["post", "create_content"],
                    description="Account content posts per minute"
                ),
                
                # Workflow limits
                RateLimit(
                    limit_id="workflow-execute-per-second",
                    max_events=10,
                    window_seconds=1,
                    scope=RateScope.WORKFLOW,
                    window_type=WindowType.FIXED,
                    action_types=["execute"],
                    description="Workflow executions per second"
                ),
                RateLimit(
                    limit_id="workflow-execute-per-hour",
                    max_events=1000,
                    window_seconds=3600,
                    scope=RateScope.WORKFLOW,
                    window_type=WindowType.SLIDING,
                    action_types=["execute"],
                    description="Workflow executions per hour (sliding)"
                ),
                
                # Job limits
                RateLimit(
                    limit_id="job-task-per-second",
                    max_events=50,
                    window_seconds=1,
                    scope=RateScope.JOB,
                    window_type=WindowType.FIXED,
                    description="Job tasks per second"
                ),
            ]
        )
    
    @staticmethod
    def create_free_tier_policy(now: Optional[datetime] = None) -> RatePolicy:
        """Create free tier rate limit policy with tighter limits."""
        if now is None:
            now = datetime.now(timezone.utc)
        
        return RatePolicy(
            policy_version="free-tier-v1.0.0",
            effective_from=now,
            effective_until=None,
            limits=[
                RateLimit(
                    limit_id="free-api-per-second",
                    max_events=10,
                    window_seconds=1,
                    scope=RateScope.ACCOUNT,
                    window_type=WindowType.FIXED,
                    description="Free tier API calls per second"
                ),
                RateLimit(
                    limit_id="free-api-per-minute",
                    max_events=500,
                    window_seconds=60,
                    scope=RateScope.ACCOUNT,
                    window_type=WindowType.SLIDING,
                    description="Free tier API calls per minute"
                ),
                RateLimit(
                    limit_id="free-post-per-hour",
                    max_events=100,
                    window_seconds=3600,
                    scope=RateScope.ACCOUNT,
                    window_type=WindowType.FIXED,
                    action_types=["post", "create_content"],
                    description="Free tier posts per hour"
                ),
            ]
        )
    
    @staticmethod
    def create_enterprise_policy(now: Optional[datetime] = None) -> RatePolicy:
        """Create enterprise rate limit policy with higher limits."""
        if now is None:
            now = datetime.now(timezone.utc)
        
        return RatePolicy(
            policy_version="enterprise-v1.0.0",
            effective_from=now,
            effective_until=None,
            limits=[
                RateLimit(
                    limit_id="enterprise-api-per-second",
                    max_events=1000,
                    window_seconds=1,
                    scope=RateScope.ACCOUNT,
                    window_type=WindowType.FIXED,
                    description="Enterprise API calls per second"
                ),
                RateLimit(
                    limit_id="enterprise-api-per-minute",
                    max_events=50000,
                    window_seconds=60,
                    scope=RateScope.ACCOUNT,
                    window_type=WindowType.SLIDING,
                    description="Enterprise API calls per minute"
                ),
                RateLimit(
                    limit_id="enterprise-workflow-per-second",
                    max_events=100,
                    window_seconds=1,
                    scope=RateScope.WORKFLOW,
                    window_type=WindowType.FIXED,
                    description="Enterprise workflow executions per second"
                ),
            ]
        )


# ============================================================================
# EXAMPLE USAGE & INTEGRATION
# ============================================================================

def example_usage():
    """Example of how to use the rate limiting system."""
    
    # Create state backend
    state_backend = InMemoryRateStateBackend()
    
    # Create policy
    policy = RatePolicyFactory.create_default_policy()
    
    # Create audit callback
    def audit_logger(result: RateEvaluationResult):
        print(f"[RATE LIMIT AUDIT] {result.decision.value}: {result.reason}")
        print(f"  Key: {result.rate_key.to_string()}")
        print(f"  Count: {result.current_count}/{result.limit_value} (remaining: {result.remaining})")
        if result.retry_after_seconds:
            print(f"  Retry after: {result.retry_after_seconds}s")
    
    # Create watchdog
    watchdog = WatchdogInterface()
    
    # Create rate limiter
    rate_limiter = RateLimiter(
        policy=policy,
        state_backend=state_backend,
        audit_callback=audit_logger,
        watchdog=watchdog
    )
    
    # Example 1: Check rate limit for account API call
    account_key = RateLimitKey(
        scope=RateScope.ACCOUNT,
        scope_id="acc-12345",
        action_type="api_call"
    )
    
    # Make several requests
    for i in range(5):
        try:
            result = rate_limiter.check(account_key)
            print(f"\n✓ Request {i+1} allowed. Remaining: {result.remaining}")
            time.sleep(0.1)
        except RateLimitExceeded as e:
            print(f"\n✗ Request {i+1} denied: {e}")
            print(f"  Retry after: {e.result.retry_after_seconds}s")
    
    # Example 2: Use context manager
    workflow_key = RateLimitKey(
        scope=RateScope.WORKFLOW,
        scope_id="wf-67890",
        action_type="execute"
    )
    
    try:
        with rate_limiter.limit(workflow_key):
            print("\n✓ Workflow execution allowed, performing work...")
            # Do work here
    except RateLimitExceeded as e:
        print(f"\n✗ Workflow execution denied: {e}")
    
    # Example 3: Pre-flight check without consuming
    check_result = rate_limiter.check_only(account_key)
    if check_result.decision == RateDecision.ALLOW:
        print(f"\n✓ Pre-flight check passed. Remaining: {check_result.remaining}")
    else:
        print(f"\n✗ Pre-flight check failed: {check_result.reason}")
    
    # Example 4: Get current state
    state = rate_limiter.get_current_state(account_key)
    if state:
        print(f"\nCurrent state for {account_key.to_string()}:")
        print(f"  Window start: {state.window_start}")
        print(f"  Event count: {state.event_count}")
        print(f"  Version: {state.version}")
    
    # Example 5: Admin reset
    try:
        rate_limiter.reset_state(
            account_key,
            admin_authority="admin@example.com",
            reason="Customer support request #12345"
        )
        print(f"\n✓ Rate limit reset for {account_key.to_string()}")
    except Exception as e:
        print(f"\n✗ Reset failed: {e}")
    
    # Example 6: Watchdog freeze
    watchdog.freeze()
    print("\n⚠ System frozen by watchdog")
    
    try:
        rate_limiter.check(account_key)
    except RateLimitFrozen:
        print("✗ Operation blocked - system frozen")
    
    watchdog.unfreeze()
    print("✓ System unfrozen")
    
    # Example 7: Watchdog deny override
    watchdog.add_deny_override("acc-12345")
    print("\n⚠ Added deny override for acc-12345")
    
    try:
        rate_limiter.check(account_key)
    except RateLimitExceeded as e:
        print(f"✗ Denied by watchdog override: {e}")
    
    watchdog.remove_deny_override("acc-12345")
    print("✓ Removed deny override")
    
    # Get statistics
    stats = rate_limiter.get_statistics()
    print(f"\nRate Limiter Statistics:")
    print(f"  Total evaluations: {stats.get('total', 0)}")
    print(f"  Allowed: {stats.get(RateDecision.ALLOW, 0)}")
    print(f"  Denied: {stats.get(RateDecision.DENY, 0)}")


if __name__ == "__main__":
    example_usage()