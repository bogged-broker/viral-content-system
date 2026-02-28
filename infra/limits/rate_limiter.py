"""
/infra/limits/rate_limiter.py

Deterministic Rate Enforcement Authority

This module answers exactly one question:

> "Are you allowed to perform this action at this speed right now?"

It enforces velocity, not volume, not priority, not degradation.

No guessing. No smoothing. No ML. No forgiveness.

Design Principle: Rate limiting protects time. Quotas protect totals. Backpressure protects truth.

Never mix their responsibilities.

TIER-0 ENHANCEMENTS (Production-Grade Hardening):
==================================================

1. ATOMIC COUNTER UPDATES (CRITICAL)
   - Compare-and-swap (CAS) operations via StateBackend.compare_and_swap()
   - Version-based optimistic locking in RateLimitState
   - Retry logic with fresh state reads on CAS failures
   - Prevents race conditions under high concurrency (5M+ baseline traffic)

2. LIMIT-ID SCOPED STATE KEYS
   - State keys include limit_id: (scope, scope_id, action_type, limit_id)
   - Prevents cross-limit interference when multiple limits apply
   - Each limit maintains independent window state
   - Eliminates window duration conflicts

3. FAIL-CLOSED ON PERSISTENCE FAILURE
   - ALLOW + failed write → DENY (not silent bypass)
   - Escalates persistence failures to watchdog
   - Audit logs include persistence_failure escalation marker
   - Prevents replay window inflation

4. SUSTAINED DENIAL TRACKING
   - Tracks denial counts per key
   - Escalates sustained denials via watchdog.should_escalate_sustained_denials()
   - Resets denial count on successful ALLOW
   - Audit logs include escalation markers

5. RUNTIME INVARIANT ENFORCEMENT
   - Invariants enforced during evaluation and persistence:
     * No overlapping limits
     * No negative counters
     * No implicit bursts
     * No time travel
     * Monotonicity
   - Violations raise exceptions (hard stop)

6. POLICY VERSION IN AUDIT LOGS
   - All audit events include policy_version
   - Enables forensic traceability
   - Required for Tier-0 compliance

7. REMOVED REDUNDANT PARAMETERS
   - RateEvaluator.evaluate() no longer accepts policy parameter
   - Policy stored in evaluator instance (reduces ambiguity)

8. WATCHDOG ESCALATION INTEGRATION
   - Persistence failures escalated via escalate_persistence_failure()
   - Sustained denials escalated via should_escalate_sustained_denials()
   - All escalation events marked in audit logs
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Protocol, Tuple


# ============================================================================
# CORE ENUMS (STRICT)
# ============================================================================

class RateScope(Enum):
    """
    Scopes do not overlap implicitly.
    
    TIER-0 NOTE: GLOBAL scope applies to all keys implicitly.
    This is acceptable but should be explicitly documented in invariants.
    Each limit with GLOBAL scope is evaluated separately with its own state.
    """
    GLOBAL = "global"
    ACCOUNT = "account"
    WORKFLOW = "workflow"
    JOB = "job"


class RateDecision(Enum):
    """
    No throttling here. Only permission.
    """
    ALLOW = "allow"
    DENY = "deny"


# ============================================================================
# CORE DATA STRUCTURES (IMMUTABLE)
# ============================================================================

@dataclass(frozen=True)
class RateLimit:
    """
    Limits are declared, never inferred.
    """
    max_events: int
    window_seconds: int
    scope: RateScope
    limit_id: str
    
    def __post_init__(self):
        """Validate rate limit at construction."""
        if self.max_events <= 0:
            raise ValueError("RateLimit max_events must be positive")
        
        if self.window_seconds <= 0:
            raise ValueError("RateLimit window_seconds must be positive")
        
        if not self.limit_id or not self.limit_id.strip():
            raise ValueError("RateLimit limit_id cannot be empty")
    
    def to_dict(self) -> Dict[str, any]:
        """Convert to dictionary for serialization."""
        return {
            "max_events": self.max_events,
            "window_seconds": self.window_seconds,
            "scope": self.scope.value,
            "limit_id": self.limit_id,
        }
    
    @staticmethod
    def from_dict(data: Dict[str, any]) -> RateLimit:
        """Reconstruct from dictionary."""
        return RateLimit(
            max_events=data["max_events"],
            window_seconds=data["window_seconds"],
            scope=RateScope(data["scope"]),
            limit_id=data["limit_id"],
        )


@dataclass(frozen=True)
class RateLimitKey:
    """
    Keys must be fully specified — no wildcards.
    
    TIER-0: Keys are scoped by (scope, scope_id, action_type, limit_id).
    This prevents cross-limit interference when multiple limits apply.
    """
    scope: RateScope
    scope_id: Optional[str]
    action_type: str
    limit_id: Optional[str] = None  # TIER-0: Include limit_id in key for isolation
    
    def __post_init__(self):
        """Validate rate limit key at construction."""
        if not self.action_type or not self.action_type.strip():
            raise ValueError("RateLimitKey action_type cannot be empty")
    
    def to_dict(self) -> Dict[str, any]:
        """Convert to dictionary for serialization."""
        return {
            "scope": self.scope.value,
            "scope_id": self.scope_id,
            "action_type": self.action_type,
            "limit_id": self.limit_id,
        }
    
    @staticmethod
    def from_dict(data: Dict[str, any]) -> RateLimitKey:
        """Reconstruct from dictionary."""
        return RateLimitKey(
            scope=RateScope(data["scope"]),
            scope_id=data.get("scope_id"),
            action_type=data["action_type"],
            limit_id=data.get("limit_id"),
        )
    
    def __hash__(self) -> int:
        """Make RateLimitKey hashable."""
        return hash((self.scope, self.scope_id, self.action_type, self.limit_id))
    
    def __eq__(self, other: object) -> bool:
        """RateLimitKey equality."""
        if not isinstance(other, RateLimitKey):
            return False
        return (
            self.scope == other.scope
            and self.scope_id == other.scope_id
            and self.action_type == other.action_type
            and self.limit_id == other.limit_id
        )
    
    def with_limit_id(self, limit_id: str) -> 'RateLimitKey':
        """
        Create a new key with limit_id for state isolation.
        
        TIER-0: Each limit gets its own state key to prevent window conflicts.
        """
        return RateLimitKey(
            scope=self.scope,
            scope_id=self.scope_id,
            action_type=self.action_type,
            limit_id=limit_id
        )


@dataclass
class RateLimitState:
    """
    State is monotonic inside a window.
    
    TIER-0: Includes version for optimistic locking (CAS).
    Version is monotonic across window resets to prevent ABA race conditions.
    """
    window_start: int  # Epoch seconds
    event_count: int
    version: int = 0  # Monotonic version for CAS (never resets, increments on each write)
    
    def __post_init__(self):
        """Validate rate limit state at construction."""
        if self.window_start < 0:
            raise ValueError("RateLimitState window_start cannot be negative")
        
        if self.event_count < 0:
            raise ValueError("RateLimitState event_count cannot be negative")
        
        if self.version < 0:
            raise ValueError("RateLimitState version cannot be negative")
    
    def to_dict(self) -> Dict[str, any]:
        """Convert to dictionary for serialization."""
        return {
            "window_start": self.window_start,
            "event_count": self.event_count,
            "version": self.version,
        }
    
    @staticmethod
    def from_dict(data: Dict[str, any]) -> RateLimitState:
        """Reconstruct from dictionary."""
        return RateLimitState(
            window_start=data["window_start"],
            event_count=data["event_count"],
            version=data.get("version", 0),  # Backward compatible
        )


@dataclass(frozen=True)
class RatePolicy:
    """
    Declarative rate policy.
    
    Rules:
    - versioned
    - immutable
    - loaded from config_registry
    - never mutated at runtime
    """
    limits: Tuple[RateLimit, ...]
    policy_version: str
    
    def __post_init__(self):
        """Validate rate policy at construction."""
        if not self.limits:
            raise ValueError("RatePolicy must have at least one limit")
        
        if not self.policy_version or not self.policy_version.strip():
            raise ValueError("RatePolicy policy_version cannot be empty")
        
        # Validate all limits
        for limit in self.limits:
            if not isinstance(limit, RateLimit):
                raise ValueError("RatePolicy limits must be RateLimit instances")
    
    def to_dict(self) -> Dict[str, any]:
        """Convert to dictionary for serialization."""
        return {
            "limits": [limit.to_dict() for limit in self.limits],
            "policy_version": self.policy_version,
        }
    
    @staticmethod
    def from_dict(data: Dict[str, any]) -> RatePolicy:
        """Reconstruct from dictionary."""
        return RatePolicy(
            limits=tuple(RateLimit.from_dict(l) for l in data["limits"]),
            policy_version=data["policy_version"],
        )


# ============================================================================
# EXTERNAL DEPENDENCIES (INTERFACES)
# ============================================================================

class StateBackend(Protocol):
    """
    Interface for rate limit state persistence.
    
    TIER-0: Must support atomic compare-and-swap operations.
    """
    
    def get_state(self, key: RateLimitKey) -> Optional[RateLimitState]:
        """Retrieve rate limit state by key."""
        ...
    
    def set_state(self, key: RateLimitKey, state: RateLimitState) -> bool:
        """
        Persist rate limit state atomically.
        
        WARNING: This method does NOT provide concurrency protection.
        Use compare_and_swap() for Tier-0 atomicity guarantees.
        
        Returns:
            True if successful, False otherwise
        """
        ...
    
    def compare_and_swap(
        self,
        key: RateLimitKey,
        expected_version: int,
        new_state: RateLimitState
    ) -> bool:
        """
        Atomically update state if version matches (CAS operation).
        
        TIER-0 REQUIREMENT: Prevents race conditions under concurrency.
        
        Args:
            key: Rate limit key
            expected_version: Expected current version (must match)
            new_state: New state to write (version will be incremented)
        
        Returns:
            True if version matched and update succeeded, False if version mismatch
        
        Raises:
            RuntimeError: If backend does not support CAS
        """
        ...
    
    def compare_and_swap_batch(
        self,
        updates: Dict[RateLimitKey, Tuple[int, RateLimitState]]
    ) -> bool:
        """
        Atomically update multiple states if all versions match (batch CAS).
        
        TIER-0 REQUIREMENT: Multi-limit atomicity - all-or-nothing.
        
        Args:
            updates: Dict of key -> (expected_version, new_state)
        
        Returns:
            True if all versions matched and updates succeeded, False if any mismatch
        
        Raises:
            RuntimeError: If backend does not support batch CAS
        """
        ...
    
    def get_all_states(self) -> Dict[RateLimitKey, RateLimitState]:
        """Retrieve all rate limit states (for recovery)."""
        ...


class ConfigRegistry(Protocol):
    """Interface for rate policy configuration."""
    
    def get_active_policy(self) -> Optional[RatePolicy]:
        """Get active rate policy."""
        ...


class AuditLogger(Protocol):
    """Interface for rate limit audit events."""
    
    def log_rate_decision(
        self,
        key: RateLimitKey,
        decision: RateDecision,
        limit: Optional[RateLimit],
        state: Optional[RateLimitState],
        now: int,
        policy_version: Optional[str] = None,
        escalation_marker: Optional[str] = None
    ) -> None:
        """
        Log rate limit decision event.
        
        TIER-0: Includes policy_version and escalation_marker for forensics.
        
        Args:
            key: Rate limit key
            decision: Decision (ALLOW/DENY)
            limit: Applicable limit (if any)
            state: Current state (if any)
            now: Timestamp
            policy_version: Policy version for audit trail
            escalation_marker: Marker if escalation occurred
        """
        ...


class Watchdog(Protocol):
    """
    Interface for watchdog authority.
    
    TIER-0: Must support sustained denial escalation.
    """
    
    def is_global_freeze_active(self) -> bool:
        """Check if global freeze is active."""
        ...
    
    def get_emergency_bypass_rules(self) -> List[RateLimitKey]:
        """Get list of keys that must be denied (emergency bypass)."""
        ...
    
    def should_escalate_sustained_denials(
        self,
        key: RateLimitKey,
        denial_count: int
    ) -> bool:
        """Check if sustained denials should be escalated."""
        ...
    
    def escalate_persistence_failure(
        self,
        key: RateLimitKey,
        reason: str
    ) -> None:
        """
        Escalate persistence failure to watchdog.
        
        TIER-0: Persistence failures are critical and must be escalated.
        
        Args:
            key: Rate limit key that failed
            reason: Reason for failure
        """
        ...


class Clock(Protocol):
    """Interface for monotonic timestamps."""
    
    def now_seconds(self) -> int:
        """Get current time in epoch seconds."""
        ...


# ============================================================================
# RATE EVALUATOR (DECISION LOGIC)
# ============================================================================

class RateEvaluator:
    """
    Decision logic for rate limit evaluation.
    
    Evaluation guarantees:
    - deterministic window math
    - strict integer arithmetic
    - exact boundary handling
    - fail-closed on missing state
    
    Same inputs → same decision.
    """
    
    def __init__(self, policy: RatePolicy):
        self.policy = policy
    
    def evaluate(
        self,
        key: RateLimitKey,
        now: int,
        state: Optional[RateLimitState]
    ) -> Tuple[RateDecision, Optional[RateLimit], Optional[RateLimitState], str]:
        """
        Evaluate rate limit request.
        
        TIER-0: Removed redundant policy parameter (stored in evaluator).
        
        Args:
            key: Rate limit key (scope, scope_id, action_type)
            now: Current timestamp (epoch seconds)
            state: Current rate limit state (None if not found)
        
        Returns:
            Tuple of (decision, limit, state, reason)
        """
        # Find applicable limits
        applicable_limits = self._get_applicable_limits(key)
        
        if not applicable_limits:
            # No limits configured = deny (fail closed)
            return (
                RateDecision.DENY,
                None,
                None,
                f"No rate limits configured for {key.scope.value}:{key.action_type}"
            )
        
        # TIER-0: Runtime invariant enforcement
        RateLimitInvariants.validate_no_overlapping_limits(applicable_limits, key)
        
        # Check each applicable limit
        for limit in applicable_limits:
            # Get or create state for this limit
            current_state = self._get_or_reset_state(key, limit, state, now)
            
            if current_state is None:
                # Fail closed if state cannot be determined
                return (
                    RateDecision.DENY,
                    limit,
                    None,
                    f"Unable to determine state for {key.scope.value}:{key.scope_id}"
                )
            
            # TIER-0: Runtime invariant enforcement
            RateLimitInvariants.validate_no_negative_counters(current_state.event_count)
            RateLimitInvariants.validate_no_implicit_bursts(current_state, limit)
            
            # Check if request would exceed limit
            if current_state.event_count >= limit.max_events:
                return (
                    RateDecision.DENY,
                    limit,
                    current_state,
                    f"Rate limit exceeded: {current_state.event_count}/{limit.max_events} "
                    f"in {limit.window_seconds}s window"
                )
        
        # All limits passed
        return (
            RateDecision.ALLOW,
            applicable_limits[0] if applicable_limits else None,
            self._get_or_reset_state(key, applicable_limits[0], state, now) if applicable_limits else None,
            "Within all rate limits"
        )
    
    def get_applicable_limits(
        self,
        key: RateLimitKey
    ) -> List[RateLimit]:
        """
        Get all limits applicable to this key.
        
        TIER-0: Public method for clean layering (no private method access).
        """
        applicable = []
        
        for limit in self.policy.limits:
            # Global scope applies to everything
            if limit.scope == RateScope.GLOBAL:
                applicable.append(limit)
            # Specific scope must match
            elif limit.scope == key.scope:
                applicable.append(limit)
        
        return applicable
    
    def evaluate_single_limit(
        self,
        key: RateLimitKey,
        limit: RateLimit,
        now: int,
        state: Optional[RateLimitState]
    ) -> Tuple[RateDecision, Optional[RateLimitState], str]:
        """
        Evaluate a single rate limit.
        
        TIER-0: Used for limit_id-scoped evaluation.
        
        Args:
            key: Rate limit key
            limit: Single limit to evaluate
            now: Current timestamp
            state: Current state for this limit
        
        Returns:
            Tuple of (decision, state, reason)
        """
        # Get or create state for this limit
        current_state = self._get_or_reset_state(key, limit, state, now)
        
        if current_state is None:
            # Fail closed if state cannot be determined
            return (
                RateDecision.DENY,
                None,
                f"Unable to determine state for {key.scope.value}:{key.scope_id}"
            )
        
        # TIER-0: Runtime invariant enforcement
        RateLimitInvariants.validate_no_negative_counters(current_state.event_count)
        RateLimitInvariants.validate_no_implicit_bursts(current_state, limit)
        
        # Check if request would exceed limit
        if current_state.event_count >= limit.max_events:
            return (
                RateDecision.DENY,
                current_state,
                f"Rate limit exceeded: {current_state.event_count}/{limit.max_events} "
                f"in {limit.window_seconds}s window"
            )
        
        # Limit passed
        return (
            RateDecision.ALLOW,
            current_state,
            f"Within limit {limit.limit_id}"
        )
    
    def _get_or_reset_state(
        self,
        key: RateLimitKey,
        limit: RateLimit,
        state: Optional[RateLimitState],
        now: int
    ) -> Optional[RateLimitState]:
        """Get state, resetting if window expired."""
        if state is None:
            # No existing state - create new window
            new_state = RateLimitState(
                window_start=now,
                event_count=0,
                version=0
            )
            # TIER-0: Runtime invariant enforcement
            RateLimitInvariants.validate_no_negative_counters(new_state.event_count)
            return new_state
        
        # TIER-0: Runtime invariant enforcement
        RateLimitInvariants.validate_no_time_travel(state.window_start, now)
        
        # Check if window expired
        window_end = state.window_start + limit.window_seconds
        
        if now >= window_end:
            # Window expired - reset to new window
            # TIER-0: Version remains monotonic (increment, don't reset)
            # Prevents ABA race conditions under heavy concurrency
            new_version = (state.version + 1) if state else 0
            new_state = RateLimitState(
                window_start=now,
                event_count=0,
                version=new_version  # Monotonic - never reset to 0
            )
            # TIER-0: Runtime invariant enforcement
            RateLimitInvariants.validate_no_negative_counters(new_state.event_count)
            return new_state
        
        # Window still active - return existing state
        return state


# ============================================================================
# RATE LIMITER (PUBLIC AUTHORITY)
# ============================================================================

class RateLimiter:
    """
    Public authority for rate limit enforcement.
    
    Responsibilities:
    - load correct policy
    - fetch state from persistence
    - invoke evaluator
    - update state atomically (CAS)
    - emit audit event
    - track sustained denials
    - escalate failures
    
    TIER-0: Atomic updates, fail-closed, sustained denial tracking.
    No side effects beyond state + logs.
    """
    
    def __init__(
        self,
        config_registry: ConfigRegistry,
        state_backend: StateBackend,
        audit_logger: Optional[AuditLogger] = None,
        watchdog: Optional[Watchdog] = None,
        clock: Optional[Clock] = None
    ):
        self.config_registry = config_registry
        self.state_backend = state_backend
        self.audit_logger = audit_logger
        self.watchdog = watchdog
        self.clock = clock or _DefaultClock()
        self._evaluator: Optional[RateEvaluator] = None
        self._active_policy: Optional[RatePolicy] = None
        
        # TIER-0: Sustained denial tracking
        # TIER-0 REQUIREMENT: For multi-instance deployments, denial tracking must be
        # external/durable. In-memory tracking is node-local and breaks determinism.
        # 
        # Options:
        # 1. Use external durable storage (state backend, Redis, etc.)
        # 2. Use distributed watchdog that handles durability
        # 3. Accept node-local tracking for single-instance deployments only
        #
        # Current implementation: In-memory (node-local)
        # For production multi-instance: Must be replaced with durable backend
        self._denial_counts: Dict[RateLimitKey, int] = {}
        self._denial_lock = None  # Will be initialized if threading is available
        try:
            import threading
            self._denial_lock = threading.Lock()
        except ImportError:
            pass  # Single-threaded environment
    
    def load_policy(self) -> None:
        """Load active rate policy from config registry."""
        policy = self.config_registry.get_active_policy()
        
        if policy is None:
            raise RuntimeError("No active rate policy found in config registry")
        
        self._active_policy = policy
        self._evaluator = RateEvaluator(policy=policy)
    
    def check(
        self,
        key: RateLimitKey,
        now: Optional[int] = None
    ) -> Tuple[RateDecision, str]:
        """
        Check if rate limit allows the action.
        
        TIER-0: Uses CAS for atomicity, fail-closed on persistence failure,
        tracks sustained denials, includes policy_version in audit.
        
        Args:
            key: Rate limit key (scope, scope_id, action_type)
            now: Current timestamp (epoch seconds), None to use clock
        
        Returns:
            Tuple of (decision, reason)
        
        Raises:
            RuntimeError: If policy not loaded
        """
        if self._evaluator is None:
            raise RuntimeError("Rate policy not loaded - call load_policy() first")
        
        if now is None:
            now = self.clock.now_seconds()
        
        # Check watchdog first (highest authority)
        if self.watchdog:
            if self.watchdog.is_global_freeze_active():
                decision = RateDecision.DENY
                reason = "Global freeze active"
                
                self._record_denial(key)
                
                if self.audit_logger:
                    self.audit_logger.log_rate_decision(
                        key=key,
                        decision=decision,
                        limit=None,
                        state=None,
                        now=now,
                        policy_version=self._active_policy.policy_version if self._active_policy else None,
                        escalation_marker=None
                    )
                
                return (decision, reason)
            
            bypass_rules = self.watchdog.get_emergency_bypass_rules()
            if key in bypass_rules:
                decision = RateDecision.DENY
                reason = "Emergency bypass rule active"
                
                self._record_denial(key)
                
                if self.audit_logger:
                    self.audit_logger.log_rate_decision(
                        key=key,
                        decision=decision,
                        limit=None,
                        state=None,
                        now=now,
                        policy_version=self._active_policy.policy_version if self._active_policy else None,
                        escalation_marker=None
                    )
                
                return (decision, reason)
        
        # Get applicable limits for this key
        applicable_limits = self._evaluator.get_applicable_limits(key)
        
        if not applicable_limits:
            # No limits configured = deny (fail closed)
            decision = RateDecision.DENY
            reason = f"No rate limits configured for {key.scope.value}:{key.action_type}"
            
            self._record_denial(key)
            
            if self.audit_logger:
                self.audit_logger.log_rate_decision(
                    key=key,
                    decision=decision,
                    limit=None,
                    state=None,
                    now=now,
                    policy_version=self._active_policy.policy_version if self._active_policy else None,
                    escalation_marker=None
                )
            
            return (decision, reason)
        
        # TIER-0: Check each limit with limit_id-scoped keys
        # All limits must pass for ALLOW decision
        evaluated_states: Dict[str, RateLimitState] = {}  # limit_id -> state
        
        for limit in applicable_limits:
            # Create limit_id-scoped key for state isolation
            limit_key = key.with_limit_id(limit.limit_id)
            
            # Get current state for this specific limit
            state = self.state_backend.get_state(limit_key)
            
            # Evaluate this single limit
            decision, evaluated_state, reason = self._evaluator.evaluate_single_limit(
                key=key,
                limit=limit,
                now=now,
                state=state
            )
            
            # If denied, record and check for escalation
            if decision == RateDecision.DENY:
                self._record_denial(key)
                denial_count = self._get_denial_count(key)
                
                # Check if escalation needed
                escalation_marker = None
                if self.watchdog and self.watchdog.should_escalate_sustained_denials(key, denial_count):
                    escalation_marker = f"sustained_denials_{denial_count}"
                
                # Audit log with policy version and escalation
                if self.audit_logger:
                    self.audit_logger.log_rate_decision(
                        key=key,
                        decision=decision,
                        limit=limit,
                        state=evaluated_state,
                        now=now,
                        policy_version=self._active_policy.policy_version if self._active_policy else None,
                        escalation_marker=escalation_marker
                    )
                
                return (decision, reason)
            
            # Store evaluated state for later update
            if evaluated_state:
                evaluated_states[limit.limit_id] = evaluated_state
        
        # All limits passed - update all states atomically
        # TIER-0: Multi-limit atomicity with batch CAS and compensation rollback
        
        # Prepare batch updates
        batch_updates: Dict[RateLimitKey, Tuple[int, RateLimitState]] = {}
        limit_key_map: Dict[RateLimitKey, RateLimit] = {}  # For rollback
        limit_id_to_key: Dict[str, RateLimitKey] = {}  # For state lookup
        
        for limit in applicable_limits:
            limit_key = key.with_limit_id(limit.limit_id)
            evaluated_state = evaluated_states.get(limit.limit_id)
            
            if evaluated_state:
                new_state = RateLimitState(
                    window_start=evaluated_state.window_start,
                    event_count=evaluated_state.event_count + 1,
                    version=evaluated_state.version + 1  # Increment version for CAS
                )
                
                # TIER-0: Runtime invariant enforcement
                RateLimitInvariants.validate_monotonicity(
                    evaluated_state.event_count,
                    new_state.event_count
                )
                RateLimitInvariants.validate_no_negative_counters(new_state.event_count)
                RateLimitInvariants.validate_no_implicit_bursts(new_state, limit)
                
                batch_updates[limit_key] = (evaluated_state.version, new_state)
                limit_key_map[limit_key] = limit
                limit_id_to_key[limit.limit_id] = limit_key
        
        # TIER-0: Hard fail if CAS unavailable (no silent degradation)
        if not hasattr(self.state_backend, 'compare_and_swap'):
            raise RuntimeError(
                "TIER-0 REQUIREMENT: StateBackend must support compare_and_swap(). "
                "Non-atomic fallback is not permitted."
            )
        
        # TIER-0: Batch CAS is MANDATORY for multi-limit scenarios
        # Compensation rollback is not mathematically equivalent to atomic batch CAS
        # and can erase legitimate increments under heavy concurrency.
        if len(batch_updates) > 1:
            if not hasattr(self.state_backend, 'compare_and_swap_batch'):
                raise RuntimeError(
                    f"TIER-0 REQUIREMENT: Multiple limits ({len(batch_updates)}) require "
                    "batch CAS support. StateBackend must implement compare_and_swap_batch(). "
                    "Compensation rollback is not equivalent to atomic batch operations."
                )
            
            # Attempt batch CAS (mandatory for multi-limit)
            try:
                batch_success = self.state_backend.compare_and_swap_batch(batch_updates)
                if not batch_success:
                    # Batch CAS failed (version mismatch) - fail closed
                    decision = RateDecision.DENY
                    reason = f"Batch CAS failed: version mismatch for multi-limit update"
                    
                    if self.watchdog and hasattr(self.watchdog, 'escalate_persistence_failure'):
                        self.watchdog.escalate_persistence_failure(
                            key=key,
                            reason=reason
                        )
                    
                    if self.audit_logger:
                        self.audit_logger.log_rate_decision(
                            key=key,
                            decision=decision,
                            limit=applicable_limits[0] if applicable_limits else None,
                            state=None,
                            now=now,
                            policy_version=self._active_policy.policy_version if self._active_policy else None,
                            escalation_marker="batch_cas_failure"
                        )
                    
                    return (decision, reason)
                
                # Batch CAS succeeded - all updates atomic
                self._reset_denial_count(key)
            except Exception as e:
                # Batch CAS exception - fail closed
                decision = RateDecision.DENY
                reason = f"Batch CAS exception: {e}"
                
                if self.watchdog and hasattr(self.watchdog, 'escalate_persistence_failure'):
                    self.watchdog.escalate_persistence_failure(
                        key=key,
                        reason=reason
                    )
                
                if self.audit_logger:
                    self.audit_logger.log_rate_decision(
                        key=key,
                        decision=decision,
                        limit=applicable_limits[0] if applicable_limits else None,
                        state=None,
                        now=now,
                        policy_version=self._active_policy.policy_version if self._active_policy else None,
                        escalation_marker="batch_cas_exception"
                    )
                
                return (decision, reason)
        elif len(batch_updates) == 1:
            # Single limit - use individual CAS (no rollback needed)
            limit_key, (expected_version, new_state) = next(iter(batch_updates.items()))
            limit = limit_key_map.get(limit_key)
            
            max_retries = 3
            success = False
            current_state = evaluated_states.get(limit.limit_id if limit else "")
            
            for attempt in range(max_retries):
                try:
                    success = self.state_backend.compare_and_swap(
                        key=limit_key,
                        expected_version=expected_version,
                        new_state=new_state
                    )
                    
                    if success:
                        break
                    
                    # CAS failed - read fresh state and retry
                    if attempt < max_retries - 1:
                        fresh_state = self.state_backend.get_state(limit_key)
                        if fresh_state:
                            current_state = fresh_state
                            expected_version = fresh_state.version
                            new_state = RateLimitState(
                                window_start=fresh_state.window_start,
                                event_count=fresh_state.event_count + 1,
                                version=fresh_state.version + 1
                            )
                except Exception as e:
                    # Backend error - fail closed
                    if self.watchdog and hasattr(self.watchdog, 'escalate_persistence_failure'):
                        self.watchdog.escalate_persistence_failure(
                            key=limit_key,
                            reason=f"CAS operation failed: {e}"
                        )
                    
                    decision = RateDecision.DENY
                    reason = f"Persistence failure: unable to atomically update state for {limit.limit_id if limit else 'unknown'}"
                    
                    if self.audit_logger:
                        self.audit_logger.log_rate_decision(
                            key=key,
                            decision=decision,
                            limit=limit,
                            state=current_state,
                            now=now,
                            policy_version=self._active_policy.policy_version if self._active_policy else None,
                            escalation_marker="persistence_failure"
                        )
                    
                    return (decision, reason)
            
            if not success:
                decision = RateDecision.DENY
                reason = f"Persistence failure: unable to atomically update state for {limit.limit_id if limit else 'unknown'}"
                
                if self.watchdog and hasattr(self.watchdog, 'escalate_persistence_failure'):
                    self.watchdog.escalate_persistence_failure(
                        key=limit_key,
                        reason=reason
                    )
                
                if self.audit_logger:
                    self.audit_logger.log_rate_decision(
                        key=key,
                        decision=decision,
                        limit=limit,
                        state=current_state,
                        now=now,
                        policy_version=self._active_policy.policy_version if self._active_policy else None,
                        escalation_marker="persistence_failure"
                    )
                
                return (decision, reason)
            
            # Single limit update succeeded
            self._reset_denial_count(key)
        else:
            # No updates needed (should not happen, but handle gracefully)
            self._reset_denial_count(key)
        
        # ========================================================================
        # LEGACY FALLBACK CODE (DISABLED - KEPT FOR REFERENCE)
        # ========================================================================
        # 
        # TIER-0 NOTE: The following compensation rollback code has been disabled
        # because compensation rollback is not mathematically equivalent to atomic
        # batch CAS and can erase legitimate increments under heavy concurrency.
        #
        # However, this code is preserved for:
        # 1. Reference and understanding of the evolution
        # 2. Potential future use cases where compensation might be acceptable
        # 3. Documentation of why batch CAS is mandatory
        #
        # To re-enable (NOT RECOMMENDED for Tier-0):
        # - Change the `if False:` below to `if True:`
        # - Understand the concurrency risks
        # - Accept that it's not truly atomic
        #
        # ========================================================================
        
        if False:  # DISABLED - Batch CAS is mandatory for multi-limit scenarios
            # Legacy: Individual CAS with compensation rollback (NOT Tier-0 safe)
            # This path is disabled because:
            # 1. Compensation rollback can erase legitimate increments from other threads
            # 2. Not mathematically equivalent to atomic batch CAS
            # 3. Introduces correctness drift under heavy concurrency
            
            successful_updates: List[Tuple[RateLimitKey, RateLimitState]] = []
            
            try:
                for limit_key, (expected_version, new_state) in batch_updates.items():
                    # Get original state before update (for rollback)
                    limit_id = limit_key.limit_id
                    if not limit_id:
                        # Fallback: find limit_id from reverse mapping
                        for lid, lkey in limit_id_to_key.items():
                            if lkey == limit_key:
                                limit_id = lid
                                break
                    
                    original_state = evaluated_states.get(limit_id) if limit_id else None
                    if not original_state:
                        continue
                    
                    # Atomic CAS operation with retry logic
                    max_retries = 3
                    success = False
                    current_state = original_state
                    
                    for attempt in range(max_retries):
                        try:
                            success = self.state_backend.compare_and_swap(
                                key=limit_key,
                                expected_version=expected_version,
                                new_state=new_state
                            )
                            
                            if success:
                                # Store original state for potential rollback
                                successful_updates.append((limit_key, original_state))
                                break
                            
                            # CAS failed - read fresh state and retry
                            if attempt < max_retries - 1:
                                fresh_state = self.state_backend.get_state(limit_key)
                                if fresh_state:
                                    current_state = fresh_state
                                    expected_version = fresh_state.version
                                    new_state = RateLimitState(
                                        window_start=fresh_state.window_start,
                                        event_count=fresh_state.event_count + 1,
                                        version=fresh_state.version + 1
                                    )
                        except Exception as e:
                            # Backend error - rollback and fail closed
                            self._rollback_updates(successful_updates)
                            
                            if self.watchdog and hasattr(self.watchdog, 'escalate_persistence_failure'):
                                self.watchdog.escalate_persistence_failure(
                                    key=limit_key,
                                    reason=f"CAS operation failed: {e}"
                                )
                            
                            decision = RateDecision.DENY
                            limit = limit_key_map.get(limit_key)
                            limit_id_str = limit.limit_id if limit else "unknown"
                            reason = f"Persistence failure: unable to atomically update state for {limit_id_str}"
                            
                            if self.audit_logger:
                                self.audit_logger.log_rate_decision(
                                    key=key,
                                    decision=decision,
                                    limit=limit_key_map.get(limit_key),
                                    state=current_state,
                                    now=now,
                                    policy_version=self._active_policy.policy_version if self._active_policy else None,
                                    escalation_marker="persistence_failure"
                                )
                            
                            return (decision, reason)
                    
                    # Fail-closed on persistence failure with rollback
                    if not success:
                        # Rollback all successful updates
                        self._rollback_updates(successful_updates)
                        
                        decision = RateDecision.DENY
                        limit = limit_key_map.get(limit_key)
                        limit_id_str = limit.limit_id if limit else "unknown"
                        reason = f"Persistence failure: unable to atomically update state for {limit_id_str}"
                        
                        if self.watchdog and hasattr(self.watchdog, 'escalate_persistence_failure'):
                            self.watchdog.escalate_persistence_failure(
                                key=limit_key,
                                reason=reason
                            )
                        
                        if self.audit_logger:
                            self.audit_logger.log_rate_decision(
                                key=key,
                                decision=decision,
                                limit=limit_key_map.get(limit_key),
                                state=current_state,
                                now=now,
                                policy_version=self._active_policy.policy_version if self._active_policy else None,
                                escalation_marker="persistence_failure"
                            )
                        
                        return (decision, reason)
                
                # All updates succeeded (with compensation rollback risk)
                self._reset_denial_count(key)
            except Exception as e:
                # Unexpected error - rollback and fail closed
                self._rollback_updates(successful_updates)
                
                if self.watchdog and hasattr(self.watchdog, 'escalate_persistence_failure'):
                    self.watchdog.escalate_persistence_failure(
                        key=key,
                        reason=f"Unexpected error during multi-limit update: {e}"
                    )
                
                decision = RateDecision.DENY
                reason = f"Unexpected error during state update: {e}"
                
                if self.audit_logger:
                    self.audit_logger.log_rate_decision(
                        key=key,
                        decision=decision,
                        limit=None,
                        state=None,
                        now=now,
                        policy_version=self._active_policy.policy_version if self._active_policy else None,
                        escalation_marker="persistence_failure"
                    )
                
                return (decision, reason)
        
        # All limits passed and all states updated successfully
        decision = RateDecision.ALLOW
        reason = "Within all rate limits"
        
        # Audit log success
        if self.audit_logger:
            self.audit_logger.log_rate_decision(
                key=key,
                decision=decision,
                limit=applicable_limits[0] if applicable_limits else None,
                state=None,
                now=now,
                policy_version=self._active_policy.policy_version if self._active_policy else None,
                escalation_marker=None
            )
        
        return (decision, reason)
    
    def _record_denial(self, key: RateLimitKey) -> None:
        """Record a denial for sustained denial tracking."""
        if self._denial_lock:
            with self._denial_lock:
                self._denial_counts[key] = self._denial_counts.get(key, 0) + 1
        else:
            self._denial_counts[key] = self._denial_counts.get(key, 0) + 1
    
    def _get_denial_count(self, key: RateLimitKey) -> int:
        """Get denial count for key."""
        if self._denial_lock:
            with self._denial_lock:
                return self._denial_counts.get(key, 0)
        return self._denial_counts.get(key, 0)
    
    def _reset_denial_count(self, key: RateLimitKey) -> None:
        """Reset denial count on successful allow."""
        if self._denial_lock:
            with self._denial_lock:
                self._denial_counts.pop(key, None)
        else:
            self._denial_counts.pop(key, None)
    
    def _rollback_updates(
        self,
        successful_updates: List[Tuple[RateLimitKey, RateLimitState]]
    ) -> None:
        """
        Rollback successful updates (compensation for partial failure).
        
        TIER-0 WARNING: This method is deprecated. Compensation rollback is not
        mathematically equivalent to atomic batch CAS and can erase legitimate
        increments under heavy concurrency. Batch CAS is now mandatory for
        multi-limit scenarios.
        
        This method is kept for reference but should not be used in production.
        
        Args:
            successful_updates: List of (key, original_state) to rollback
        """
        # TIER-0: Log rollback attempts for forensics (even if they fail)
        rollback_failures: List[Tuple[RateLimitKey, str]] = []
        
        for limit_key, original_state in reversed(successful_updates):
            try:
                # Get current state to determine version
                current_state = self.state_backend.get_state(limit_key)
                if current_state and hasattr(self.state_backend, 'compare_and_swap'):
                    # Rollback by decrementing event_count (compensation)
                    # Use current version for CAS
                    rollback_state = RateLimitState(
                        window_start=current_state.window_start,
                        event_count=max(0, current_state.event_count - 1),  # Decrement, but not below 0
                        version=current_state.version + 1
                    )
                    # Attempt CAS rollback (may fail if state changed, but that's acceptable)
                    rollback_success = self.state_backend.compare_and_swap(
                        key=limit_key,
                        expected_version=current_state.version,
                        new_state=rollback_state
                    )
                    
                    if not rollback_success:
                        rollback_failures.append((
                            limit_key,
                            f"Rollback CAS failed: version mismatch (expected {current_state.version})"
                        ))
            except Exception as e:
                # Rollback failure - log for forensics
                rollback_failures.append((
                    limit_key,
                    f"Rollback exception: {e}"
                ))
        
        # TIER-0: Log rollback failures for forensic analysis
        # Even though rollback is best-effort, failures must be visible
        if rollback_failures and self.audit_logger:
            # Log rollback failures (if audit logger supports it)
            # This provides forensic traceability even if rollback doesn't block
            for limit_key, failure_reason in rollback_failures:
                # Note: This is a best-effort log - audit logger may not support
                # rollback-specific events, but we attempt to log for forensics
                pass  # Could add rollback-specific logging if audit logger supports it
    
    def get_state(
        self,
        key: RateLimitKey,
        limit_id: Optional[str] = None
    ) -> Optional[RateLimitState]:
        """
        Get current rate limit state.
        
        TIER-0: For multi-limit scenarios, specify limit_id to get state for specific limit.
        If limit_id is None, returns state for first applicable limit (backward compatible).
        
        Args:
            key: Rate limit key
            limit_id: Optional limit_id for limit-scoped state retrieval
        
        Returns:
            RateLimitState or None if not found
        """
        if limit_id:
            limit_key = key.with_limit_id(limit_id)
            return self.state_backend.get_state(limit_key)
        return self.state_backend.get_state(key)


# ============================================================================
# DEFAULT CLOCK IMPLEMENTATION
# ============================================================================

class _DefaultClock:
    """Default clock implementation using system time."""
    
    def now_seconds(self) -> int:
        """Get current time in epoch seconds."""
        import time
        return int(time.time())


# ============================================================================
# RATE LIMIT INVARIANTS (ABSOLUTE)
# ============================================================================

class RateLimitInvariants:
    """
    Absolute invariants enforced throughout rate limiting system.
    
    Violation → hard stop.
    """
    
    @staticmethod
    def validate_max_events_positive(max_events: int) -> None:
        """Invariant: max_events > 0."""
        if max_events <= 0:
            raise ValueError(f"Rate limit max_events must be positive, got {max_events}")
    
    @staticmethod
    def validate_window_seconds_positive(window_seconds: int) -> None:
        """Invariant: window_seconds > 0."""
        if window_seconds <= 0:
            raise ValueError(f"Rate limit window_seconds must be positive, got {window_seconds}")
    
    @staticmethod
    def validate_no_overlapping_limits(
        limits: List[RateLimit],
        key: RateLimitKey
    ) -> None:
        """Invariant: no overlapping limits for same key."""
        applicable = [
            limit for limit in limits
            if limit.scope == RateScope.GLOBAL or limit.scope == key.scope
        ]
        
        # Check for duplicate limit_ids
        limit_ids = [limit.limit_id for limit in applicable]
        if len(limit_ids) != len(set(limit_ids)):
            raise ValueError("Duplicate limit_id found for same key")
    
    @staticmethod
    def validate_no_negative_counters(event_count: int) -> None:
        """Invariant: no negative counters."""
        if event_count < 0:
            raise ValueError(f"Rate limit event_count cannot be negative, got {event_count}")
    
    @staticmethod
    def validate_no_time_travel(
        old_window_start: int,
        new_window_start: int
    ) -> None:
        """Invariant: no time travel across windows."""
        if new_window_start < old_window_start:
            raise ValueError(
                f"Window start time travel detected: {old_window_start} -> {new_window_start}"
            )
    
    @staticmethod
    def validate_no_implicit_bursts(
        state: RateLimitState,
        limit: RateLimit
    ) -> None:
        """Invariant: no implicit bursts."""
        if state.event_count > limit.max_events:
            raise ValueError(
                f"Event count {state.event_count} exceeds limit {limit.max_events} "
                f"(implicit burst detected)"
            )
    
    @staticmethod
    def validate_monotonicity(old_count: int, new_count: int) -> None:
        """Invariant: event_count only increases."""
        if new_count < old_count:
            raise ValueError(
                f"Event count decreased: {old_count} -> {new_count} "
                f"(MONOTONICITY VIOLATION)"
            )


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    # Enums
    'RateScope',
    'RateDecision',
    
    # Data Structures
    'RateLimit',
    'RateLimitKey',
    'RateLimitState',
    'RatePolicy',
    
    # Core Classes
    'RateEvaluator',
    'RateLimiter',
    'RateLimitInvariants',
    
    # Interfaces
    'StateBackend',
    'ConfigRegistry',
    'AuditLogger',
    'Watchdog',
    'Clock',
]
