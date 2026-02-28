"""
failure_policy.py - Production-Grade Retry, Abort, Degrade, Containment Logic

This file defines what the system does when it is told "NO."
Controlled, strategic response to constraint.

PRODUCTION-GRADE FEATURES:
- Thread-safe concurrent operations
- Comprehensive metrics and observability
- Performance optimizations (caching, lazy evaluation)
- Advanced pattern detection
- Adaptive policy tuning
- Health monitoring
- State persistence
- Cost tracking
- Integration with priority_router and factory_scheduler
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import (, Tuple, List, Dict
    Dict, Optional, Callable, List, Tuple, Set, Any, Union,
    DefaultDict, Deque, FrozenSet, Protocol, runtime_checkable
)
from collections import defaultdict, deque
from functools import lru_cache, wraps
from threading import Lock, RLock, Thread, Event
from contextlib import contextmanager
from datetime import datetime, timedelta
from time import time, perf_counter
import time as time_module
import random
import logging
import json
import hashlib
import pickle
from pathlib import Path
import weakref
from abc import ABC, abstractmethod

# ============================================================================
# TYPE DEFINITIONS & PROTOCOLS
# ============================================================================

@runtime_checkable
class MetricsCollector(Protocol):
    """Protocol for metrics collection systems"""
    def record_counter(self, name: str, value: float = 1.0, tags: Dict[str, str] = None) -> None: ...
    def record_gauge(self, name: str, value: float, tags: Dict[str, str] = None) -> None: ...
    def record_histogram(self, name: str, value: float, tags: Dict[str, str] = None) -> None: ...
    def record_timing(self, name: str, duration_ms: float, tags: Dict[str, str] = None) -> None: ...

@runtime_checkable
class AlertHandler(Protocol):
    """Protocol for alert handlers"""
    def send_alert(self, severity: str, message: str, context: Dict[str, Any]) -> None: ...

# ============================================================================
# ENUMS - Strict Type System
# ============================================================================

class FailureType(Enum):
    """Why the failure occurred - no generic failures allowed"""
    RESOURCE_EXHAUSTED = "resource_exhausted"
    RATE_LIMIT = "rate_limit"
    TRUST_DEPLETION = "trust_depletion"
    PLATFORM_RISK = "platform_risk"
    GLOBAL_SAFETY = "global_safety"
    INVARIANT_VIOLATION = "invariant_violation"
    
    @classmethod
    def from_string(cls, value: str) -> Optional['FailureType']:
        """Safely convert string to FailureType"""
        try:
            return cls(value)
        except ValueError:
            return None
    
    def severity_score(self) -> int:
        """Return severity score (0-100, higher = more severe)"""
        scores = {
            FailureType.RESOURCE_EXHAUSTED: 30,
            FailureType.RATE_LIMIT: 40,
            FailureType.PLATFORM_RISK: 70,
            FailureType.TRUST_DEPLETION: 90,
            FailureType.INVARIANT_VIOLATION: 95,
            FailureType.GLOBAL_SAFETY: 100,
        }
        return scores.get(self, 50)


class ResponseAction(Enum):
    """What to do about the failure - explicit actions only"""
    RETRY = "retry"
    BACKOFF = "backoff"
    DEFER = "defer"
    DEGRADE = "degrade"
    ABORT = "abort"
    LOCKDOWN = "lockdown"
    
    def is_terminal(self) -> bool:
        """Check if action is terminal (no further retries)"""
        return self in {ResponseAction.ABORT, ResponseAction.LOCKDOWN}
    
    def allows_retry(self) -> bool:
        """Check if action allows retry"""
        return self in {ResponseAction.RETRY, ResponseAction.BACKOFF}


class GovernorDecision(Enum):
    """Decision from ResourceGovernor"""
    APPROVED = "approved"
    THROTTLED = "throttled"
    DEFERRED = "deferred"
    DENIED = "denied"
    
    def requires_failure_policy(self) -> bool:
        """Check if this decision requires failure policy handling"""
        return self in {GovernorDecision.THROTTLED, GovernorDecision.DEFERRED, GovernorDecision.DENIED}


class ResourceType(Enum):
    """Resource types from governor"""
    API_CALLS = "api_calls"
    POSTS = "posts"
    STORAGE = "storage"
    COMPUTE = "compute"
    TRUST = "trust"
    NETWORK = "network"
    MEMORY = "memory"
    CPU = "cpu"
    
    def cost_multiplier(self) -> float:
        """Return cost multiplier for this resource type"""
        multipliers = {
            ResourceType.API_CALLS: 1.0,
            ResourceType.POSTS: 2.0,
            ResourceType.COMPUTE: 3.0,
            ResourceType.STORAGE: 0.5,
            ResourceType.TRUST: 10.0,
            ResourceType.NETWORK: 1.5,
            ResourceType.MEMORY: 0.8,
            ResourceType.CPU: 1.2,
        }
        return multipliers.get(self, 1.0)


class ResourceScope(Enum):
    """Scope of resource allocation"""
    GLOBAL = "global"
    PLATFORM = "platform"
    WORKFLOW = "workflow"
    USER = "user"
    FACTORY = "factory"
    ACCOUNT = "account"
    
    def hierarchy_level(self) -> int:
        """Return hierarchy level (0 = most global)"""
        levels = {
            ResourceScope.GLOBAL: 0,
            ResourceScope.PLATFORM: 1,
            ResourceScope.FACTORY: 2,
            ResourceScope.WORKFLOW: 3,
            ResourceScope.USER: 4,
            ResourceScope.ACCOUNT: 5,
        }
        return levels.get(self, 99)


# ============================================================================
# DATA STRUCTURES - Immutable Contexts
# ============================================================================

@dataclass(frozen=True)
class FailureContext:
    """
    Immutable context describing a failure - inputs to policy engine.
    
    Thread-safe: Immutable dataclass ensures no race conditions.
    Deterministic: All fields are deterministic for replay.
    """
    decision: GovernorDecision
    failure_type: FailureType
    resource_type: ResourceType
    scope: ResourceScope
    scope_id: str
    requester: str
    attempt: int
    timestamp: float
    
    # Optional context fields
    error_message: Optional[str] = None
    error_code: Optional[str] = None
    cost_estimate: Optional[float] = None
    priority: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def key(self) -> str:
        """Unique key for tracking this failure context"""
        return f"{self.scope.value}:{self.scope_id}:{self.resource_type.value}"
    
    def composite_key(self) -> str:
        """Composite key including requester for fine-grained tracking"""
        return f"{self.key()}:{self.requester}"
    
    def age_seconds(self) -> float:
        """Get age of this context in seconds"""
        return time() - self.timestamp
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "decision": self.decision.value,
            "failure_type": self.failure_type.value,
            "resource_type": self.resource_type.value,
            "scope": self.scope.value,
            "scope_id": self.scope_id,
            "requester": self.requester,
            "attempt": self.attempt,
            "timestamp": self.timestamp,
            "error_message": self.error_message,
            "error_code": self.error_code,
            "cost_estimate": self.cost_estimate,
            "priority": self.priority,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'FailureContext':
        """Create from dictionary"""
        return cls(
            decision=GovernorDecision(data["decision"]),
            failure_type=FailureType(data["failure_type"]),
            resource_type=ResourceType(data["resource_type"]),
            scope=ResourceScope(data["scope"]),
            scope_id=data["scope_id"],
            requester=data["requester"],
            attempt=data["attempt"],
            timestamp=data["timestamp"],
            error_message=data.get("error_message"),
            error_code=data.get("error_code"),
            cost_estimate=data.get("cost_estimate"),
            priority=data.get("priority"),
            metadata=data.get("metadata", {}),
        )


@dataclass
class FailureDecision:
    """
    Output decision - what to do about the failure.
    
    Mutable for metadata updates but core fields are set once.
    """
    action: ResponseAction
    delay_seconds: Optional[float] = None
    degrade_level: Optional[int] = None
    abort_reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # Performance tracking
    decision_time_ms: Optional[float] = None
    policy_version: Optional[str] = None
    
    # Cost tracking
    estimated_cost: Optional[float] = None
    cost_reason: Optional[str] = None
    
    def should_retry(self) -> bool:
        """Check if decision allows retry"""
        return self.action.allows_retry()
    
    def should_abort(self) -> bool:
        """Check if decision is terminal"""
        return self.action.is_terminal()
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "action": self.action.value,
            "delay_seconds": self.delay_seconds,
            "degrade_level": self.degrade_level,
            "abort_reason": self.abort_reason,
            "metadata": self.metadata,
            "decision_time_ms": self.decision_time_ms,
            "policy_version": self.policy_version,
            "estimated_cost": self.estimated_cost,
            "cost_reason": self.cost_reason,
        }


# ============================================================================
# METRICS & OBSERVABILITY
# ============================================================================

class FailureMetrics:
    """
    Comprehensive metrics collection for failure policy engine.
    Thread-safe with efficient aggregation.
    """
    
    def __init__(self, collector: Optional[MetricsCollector] = None):
        self.collector = collector
        self._lock = Lock()
        
        # Counter metrics
        self._counters: DefaultDict[str, float] = defaultdict(float)
        self._counter_history: DefaultDict[str, Deque[Tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=1000)
        )
        
        # Gauge metrics
        self._gauges: Dict[str, float] = {}
        self._gauge_history: DefaultDict[str, Deque[Tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=1000)
        )
        
        # Histogram metrics
        self._histograms: DefaultDict[str, List[float]] = defaultdict(list)
        
        # Timing metrics
        self._timings: DefaultDict[str, List[float]] = defaultdict(list)
        
        # Failure type distribution
        self._failure_type_counts: DefaultDict[FailureType, int] = defaultdict(int)
        self._action_type_counts: DefaultDict[ResponseAction, int] = defaultdict(int)
        
        # Resource type distribution
        self._resource_type_counts: DefaultDict[ResourceType, int] = defaultdict(int)
        
        # Scope distribution
        self._scope_counts: DefaultDict[ResourceScope, int] = defaultdict(int)
        
        # Cost tracking
        self._total_cost: float = 0.0
        self._cost_by_type: DefaultDict[ResourceType, float] = defaultdict(float)
        self._cost_by_action: DefaultDict[ResponseAction, float] = defaultdict(float)
        
        # Performance metrics
        self._decision_times: Deque[float] = deque(maxlen=10000)
        self._total_decisions: int = 0
        
        # Pattern detection metrics
        self._retry_storms_detected: int = 0
        self._denial_loops_detected: int = 0
        self._runaway_degradations: int = 0
        
        self.logger = logging.getLogger(f"{__name__}.Metrics")
    
    def record_decision(
        self,
        context: FailureContext,
        decision: FailureDecision,
        decision_time_ms: float
    ) -> None:
        """Record a failure decision with full context"""
        with self._lock:
            now = time()
            
            # Increment counters
            self._counters["decisions_total"] += 1
            self._counters[f"decisions_{decision.action.value}"] += 1
            self._counters[f"failures_{context.failure_type.value}"] += 1
            self._counters[f"resources_{context.resource_type.value}"] += 1
            self._counters[f"scopes_{context.scope.value}"] += 1
            
            # Update distributions
            self._failure_type_counts[context.failure_type] += 1
            self._action_type_counts[decision.action] += 1
            self._resource_type_counts[context.resource_type] += 1
            self._scope_counts[context.scope] += 1
            
            # Record timing
            self._decision_times.append(decision_time_ms)
            self._timings["decision_time_ms"].append(decision_time_ms)
            self._total_decisions += 1
            
            # Record cost if available
            if decision.estimated_cost:
                self._total_cost += decision.estimated_cost
                self._cost_by_type[context.resource_type] += decision.estimated_cost
                self._cost_by_action[decision.action] += decision.estimated_cost
            
            # Update history
            self._counter_history["decisions_total"].append((now, self._counters["decisions_total"]))
            
            # Forward to external collector if available
            if self.collector:
                try:
                    tags = {
                        "failure_type": context.failure_type.value,
                        "resource_type": context.resource_type.value,
                        "scope": context.scope.value,
                        "action": decision.action.value,
                    }
                    self.collector.record_counter("failure_policy.decisions", 1.0, tags)
                    self.collector.record_timing("failure_policy.decision_time", decision_time_ms, tags)
                    if decision.estimated_cost:
                        self.collector.record_gauge("failure_policy.cost", decision.estimated_cost, tags)
                except Exception as e:
                    self.logger.warning(f"Failed to forward metrics: {e}")
    
    def record_retry_storm(self, scope_key: str) -> None:
        """Record detection of retry storm"""
        with self._lock:
            self._retry_storms_detected += 1
            self._counters["retry_storms_detected"] += 1
            if self.collector:
                try:
                    self.collector.record_counter("failure_policy.retry_storms", 1.0, {"scope": scope_key})
                except Exception:
                    pass
    
    def record_denial_loop(self, scope_key: str) -> None:
        """Record detection of denial loop"""
        with self._lock:
            self._denial_loops_detected += 1
            self._counters["denial_loops_detected"] += 1
            if self.collector:
                try:
                    self.collector.record_counter("failure_policy.denial_loops", 1.0, {"scope": scope_key})
                except Exception:
                    pass
    
    def record_runaway_degradation(self, scope_key: str) -> None:
        """Record detection of runaway degradation"""
        with self._lock:
            self._runaway_degradations += 1
            self._counters["runaway_degradations_detected"] += 1
            if self.collector:
                try:
                    self.collector.record_counter("failure_policy.runaway_degradations", 1.0, {"scope": scope_key})
                except Exception:
                    pass
    
    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive statistics"""
        with self._lock:
            decision_times = list(self._decision_times)
            avg_decision_time = sum(decision_times) / len(decision_times) if decision_times else 0.0
            p50_decision_time = sorted(decision_times)[len(decision_times) // 2] if decision_times else 0.0
            p95_decision_time = sorted(decision_times)[int(len(decision_times) * 0.95)] if decision_times else 0.0
            p99_decision_time = sorted(decision_times)[int(len(decision_times) * 0.99)] if decision_times else 0.0
            
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "failure_type_distribution": {
                    ft.value: count for ft, count in self._failure_type_counts.items()
                },
                "action_type_distribution": {
                    action.value: count for action, count in self._action_type_counts.items()
                },
                "resource_type_distribution": {
                    rt.value: count for rt, count in self._resource_type_counts.items()
                },
                "scope_distribution": {
                    scope.value: count for scope, count in self._scope_counts.items()
                },
                "cost": {
                    "total": self._total_cost,
                    "by_type": {rt.value: cost for rt, cost in self._cost_by_type.items()},
                    "by_action": {action.value: cost for action, cost in self._cost_by_action.items()},
                },
                "performance": {
                    "total_decisions": self._total_decisions,
                    "avg_decision_time_ms": avg_decision_time,
                    "p50_decision_time_ms": p50_decision_time,
                    "p95_decision_time_ms": p95_decision_time,
                    "p99_decision_time_ms": p99_decision_time,
                },
                "patterns": {
                    "retry_storms": self._retry_storms_detected,
                    "denial_loops": self._denial_loops_detected,
                    "runaway_degradations": self._runaway_degradations,
                },
            }
    
    def reset(self) -> None:
        """Reset all metrics (for testing)"""
        with self._lock:
            self._counters.clear()
            self._counter_history.clear()
            self._gauges.clear()
            self._gauge_history.clear()
            self._histograms.clear()
            self._timings.clear()
            self._failure_type_counts.clear()
            self._action_type_counts.clear()
            self._resource_type_counts.clear()
            self._scope_counts.clear()
            self._total_cost = 0.0
            self._cost_by_type.clear()
            self._cost_by_action.clear()
            self._decision_times.clear()
            self._total_decisions = 0
            self._retry_storms_detected = 0
            self._denial_loops_detected = 0
            self._runaway_degradations = 0


# ============================================================================
# RETRY CONTROLLER - Anti-Meltdown System (Enhanced)
# ============================================================================

class RetryController:
    """
    Manages retry logic with exponential backoff.
    Prevents retry storms and ensures deterministic behavior.
    
    Thread-safe with efficient lookups.
    """
    
    def __init__(
        self,
        max_retries: int = 5,
        base_delay: float = 1.0,
        max_delay: float = 300.0,
        backoff_multiplier: float = 2.0,
        jitter_enabled: bool = True,
        jitter_seed: Optional[int] = None,
        cleanup_interval_seconds: float = 3600.0,  # Cleanup old entries every hour
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_multiplier = backoff_multiplier
        self.jitter_enabled = jitter_enabled
        self._rng = random.Random(jitter_seed) if jitter_enabled else None
        self.cleanup_interval = cleanup_interval_seconds
        
        # Thread-safe tracking
        self._lock = RLock()
        self._retry_counts: Dict[str, int] = {}
        self._last_retry: Dict[str, float] = {}
        self._retry_history: DefaultDict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=100))
        self._last_cleanup: float = time()
        
        # Performance optimization: cache for common keys
        self._cache: Dict[str, Tuple[bool, float]] = {}
        self._cache_ttl: float = 60.0  # Cache for 60 seconds
        self._cache_timestamps: Dict[str, float] = {}
        
        self.logger = logging.getLogger(f"{__name__}.RetryController")
    
    def _cleanup_old_entries(self) -> None:
        """Cleanup old retry tracking entries"""
        now = time()
        if now - self._last_cleanup < self.cleanup_interval:
            return
        
        with self._lock:
            cutoff_time = now - (self.cleanup_interval * 2)
            keys_to_remove = [
                key for key, last_time in self._last_retry.items()
                if last_time < cutoff_time
            ]
            for key in keys_to_remove:
                self._retry_counts.pop(key, None)
                self._last_retry.pop(key, None)
                self._retry_history.pop(key, None)
                self._cache.pop(key, None)
                self._cache_timestamps.pop(key, None)
            
            self._last_cleanup = now
    
    def can_retry(self, context: FailureContext) -> bool:
        """Determine if retry is allowed based on attempt count"""
        key = context.key()
        now = time()
        
        # Check cache first
        if key in self._cache:
            cache_time = self._cache_timestamps.get(key, 0)
            if now - cache_time < self._cache_ttl:
                can_retry, _ = self._cache[key]
                return can_retry
        
        with self._lock:
            self._cleanup_old_entries()
            current_count = self._retry_counts.get(key, 0)
            can_retry = current_count < self.max_retries
            
            # Update cache
            delay = self._calculate_delay_internal(context)
            self._cache[key] = (can_retry, delay)
            self._cache_timestamps[key] = now
            
            return can_retry
    
    def _calculate_delay_internal(self, context: FailureContext) -> float:
        """Internal delay calculation (without locking)"""
        attempt = context.attempt
        
        # Exponential backoff
        delay = self.base_delay * (self.backoff_multiplier ** attempt)
        delay = min(delay, self.max_delay)
        
        # Optional deterministic jitter (0.5x to 1.5x)
        if self.jitter_enabled and self._rng:
            # Use context data for deterministic seed
            seed_value = hash((context.key(), attempt)) & 0xFFFFFFFF
            self._rng.seed(seed_value)
            jitter = self._rng.uniform(0.5, 1.5)
            delay *= jitter
        
        return delay
    
    def calculate_delay(self, context: FailureContext) -> float:
        """
        Calculate retry delay with exponential backoff.
        Deterministic given same inputs.
        """
        key = context.key()
        now = time()
        
        # Check cache first
        if key in self._cache:
            cache_time = self._cache_timestamps.get(key, 0)
            if now - cache_time < self._cache_ttl:
                _, delay = self._cache[key]
                return delay
        
        delay = self._calculate_delay_internal(context)
        
        # Update cache
        with self._lock:
            self._cache[key] = (True, delay)
            self._cache_timestamps[key] = now
        
        return delay
    
    def record_retry(self, context: FailureContext) -> None:
        """Record a retry attempt"""
        key = context.key()
        now = context.timestamp
        
        with self._lock:
            self._retry_counts[key] = self._retry_counts.get(key, 0) + 1
            self._last_retry[key] = now
            self._retry_history[key].append(now)
            
            # Invalidate cache
            self._cache.pop(key, None)
            self._cache_timestamps.pop(key, None)
        
        self.logger.info(
            f"Retry recorded: {key} - attempt {self._retry_counts[key]}/{self.max_retries}"
        )
    
    def reset_retry_count(self, scope_key: str) -> None:
        """Reset retry count after successful operation"""
        with self._lock:
            self._retry_counts.pop(scope_key, None)
            self._last_retry.pop(scope_key, None)
            self._retry_history.pop(scope_key, None)
            self._cache.pop(scope_key, None)
            self._cache_timestamps.pop(scope_key, None)
    
    def get_retry_count(self, scope_key: str) -> int:
        """Get current retry count for a scope"""
        with self._lock:
            return self._retry_counts.get(scope_key, 0)
    
    def get_retry_rate(self, scope_key: str, window_seconds: float = 60.0) -> float:
        """Get retry rate (retries per second) for a scope"""
        with self._lock:
            history = self._retry_history.get(scope_key, deque())
            if not history:
                return 0.0
            
            now = time()
            recent_retries = sum(1 for ts in history if now - ts < window_seconds)
            return recent_retries / window_seconds if window_seconds > 0 else 0.0
    
    def get_stats(self) -> Dict[str, Any]:
        """Get retry controller statistics"""
        with self._lock:
            return {
                "max_retries": self.max_retries,
                "base_delay": self.base_delay,
                "max_delay": self.max_delay,
                "backoff_multiplier": self.backoff_multiplier,
                "jitter_enabled": self.jitter_enabled,
                "active_retry_tracks": len(self._retry_counts),
                "cache_size": len(self._cache),
            }


# ============================================================================
# DEGRADATION CONTROLLER - Graceful Fallback (Enhanced)
# ============================================================================

class DegradationController:
    """
    Manages graceful degradation strategies.
    Reduces ambition, never increases risk.
    
    Thread-safe with adaptive level management.
    """
    
    # Degradation levels: 0 = normal, higher = more degraded
    DEGRADATION_STRATEGIES: Dict[ResourceType, List[str]] = {
        ResourceType.API_CALLS: [
            "normal",
            "reduce_frequency",
            "batch_requests",
            "cache_aggressive",
            "minimal_only"
        ],
        ResourceType.POSTS: [
            "normal",
            "reduce_posting_frequency",
            "drop_experimental_content",
            "single_platform_only",
            "critical_posts_only"
        ],
        ResourceType.COMPUTE: [
            "normal",
            "switch_to_cheaper_model",
            "reduce_resolution",
            "lower_fps",
            "text_only"
        ],
        ResourceType.TRUST: [
            "normal",
            "conservative_content",
            "manual_review_required",
            "read_only_mode"
        ],
        ResourceType.STORAGE: [
            "normal",
            "reduce_retention",
            "compress_aggressive",
            "archive_old",
            "minimal_storage"
        ],
        ResourceType.NETWORK: [
            "normal",
            "reduce_bandwidth",
            "batch_transfers",
            "cache_local",
            "offline_mode"
        ],
    }
    
    def __init__(self, auto_recovery_enabled: bool = True, recovery_window_seconds: float = 300.0):
        self.auto_recovery_enabled = auto_recovery_enabled
        self.recovery_window = recovery_window_seconds
        
        self._lock = RLock()
        self._current_levels: Dict[str, int] = {}
        self._level_history: DefaultDict[str, Deque[Tuple[float, int]]] = defaultdict(
            lambda: deque(maxlen=100)
        )
        self._last_success: Dict[str, float] = {}
        self._recovery_timers: Dict[str, float] = {}
        
        self.logger = logging.getLogger(f"{__name__}.DegradationController")
    
    def get_degradation_level(self, context: FailureContext) -> int:
        """Determine appropriate degradation level"""
        key = context.key()
        
        with self._lock:
            current = self._current_levels.get(key, 0)
            max_level = len(self.DEGRADATION_STRATEGIES.get(context.resource_type, ["normal"])) - 1
            
            # Check for auto-recovery
            if self.auto_recovery_enabled and key in self._last_success:
                time_since_success = time() - self._last_success[key]
                if time_since_success > self.recovery_window:
                    # Gradually reduce degradation
                    current = max(0, current - 1)
            
            # Increase degradation based on failure type severity
            severity_increase = {
                FailureType.GLOBAL_SAFETY: 3,
                FailureType.TRUST_DEPLETION: 2,
                FailureType.PLATFORM_RISK: 2,
                FailureType.INVARIANT_VIOLATION: 2,
                FailureType.RATE_LIMIT: 1,
                FailureType.RESOURCE_EXHAUSTED: 1,
            }
            
            increase = severity_increase.get(context.failure_type, 1)
            new_level = min(current + increase, max_level)
            
            return new_level
    
    def apply_degradation(self, context: FailureContext, level: int) -> Dict[str, Any]:
        """Apply degradation and return strategy metadata"""
        key = context.key()
        now = time()
        
        with self._lock:
            old_level = self._current_levels.get(key, 0)
            self._current_levels[key] = level
            self._level_history[key].append((now, level))
            
            # Set recovery timer
            if level > 0:
                self._recovery_timers[key] = now + self.recovery_window
        
        strategies = self.DEGRADATION_STRATEGIES.get(context.resource_type, ["normal"])
        strategy = strategies[min(level, len(strategies) - 1)]
        
        if level != old_level:
            self.logger.warning(
                f"Degradation changed: {key} -> level {old_level} -> {level} ({strategy})"
            )
        
        return {
            "strategy": strategy,
            "level": level,
            "max_level": len(strategies) - 1,
            "previous_level": old_level,
            "recovery_available": self.auto_recovery_enabled,
        }
    
    def record_success(self, scope_key: str) -> None:
        """Record successful operation - may trigger recovery"""
        with self._lock:
            self._last_success[scope_key] = time()
            if scope_key in self._recovery_timers:
                del self._recovery_timers[scope_key]
    
    def reset_degradation(self, scope_key: str) -> None:
        """Reset to normal operation"""
        with self._lock:
            old_level = self._current_levels.pop(scope_key, 0)
            self._last_success.pop(scope_key, None)
            self._recovery_timers.pop(scope_key, None)
            if old_level > 0:
                self.logger.info(f"Degradation reset: {scope_key} -> level 0 (normal)")
    
    def get_current_level(self, scope_key: str) -> int:
        """Get current degradation level"""
        with self._lock:
            return self._current_levels.get(scope_key, 0)
    
    def get_degradation_history(self, scope_key: str) -> List[Tuple[float, int]]:
        """Get degradation level history for a scope"""
        with self._lock:
            return list(self._level_history.get(scope_key, deque()))
    
    def get_stats(self) -> Dict[str, Any]:
        """Get degradation controller statistics"""
        with self._lock:
            active_degradations = sum(1 for level in self._current_levels.values() if level > 0)
            return {
                "auto_recovery_enabled": self.auto_recovery_enabled,
                "recovery_window_seconds": self.recovery_window,
                "active_degradations": active_degradations,
                "total_tracked_scopes": len(self._current_levels),
                "degradation_levels": {
                    scope: level for scope, level in self._current_levels.items()
                },
            }


# ============================================================================
# CIRCUIT BREAKER - System Protection (Enhanced)
# ============================================================================

class CircuitBreaker:
    """
    Prevents cascading failures by temporarily blocking operations.
    Tracks failure patterns and opens circuit when threshold exceeded.
    
    Thread-safe with efficient state management.
    """
    
    class State(Enum):
        CLOSED = "closed"  # Normal operation
        OPEN = "open"      # Blocking all requests
        HALF_OPEN = "half_open"  # Testing recovery
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        success_threshold: int = 2,
        error_rate_threshold: float = 0.5,  # 50% error rate triggers open
        min_requests_for_error_rate: int = 10,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold
        self.error_rate_threshold = error_rate_threshold
        self.min_requests_for_error_rate = min_requests_for_error_rate
        
        self._lock = RLock()
        self._states: Dict[str, CircuitBreaker.State] = {}
        self._failure_counts: Dict[str, int] = {}
        self._success_counts: Dict[str, int] = {}
        self._open_times: Dict[str, float] = {}
        self._failure_history: DefaultDict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=100))
        self._request_history: DefaultDict[str, Deque[Tuple[float, bool]]] = defaultdict(
            lambda: deque(maxlen=100)
        )
        
        # Performance optimization: cache state checks
        self._state_cache: Dict[str, Tuple[CircuitBreaker.State, float]] = {}
        self._cache_ttl: float = 1.0  # Cache for 1 second
        
        self.logger = logging.getLogger(f"{__name__}.CircuitBreaker")
    
    def _get_state(self, key: str) -> CircuitBreaker.State:
        """Get current state for a key (with caching)"""
        now = time()
        
        # Check cache
        if key in self._state_cache:
            state, cache_time = self._state_cache[key]
            if now - cache_time < self._cache_ttl:
                return state
        
        with self._lock:
            state = self._states.get(key, CircuitBreaker.State.CLOSED)
            self._state_cache[key] = (state, now)
            return state
    
    def should_allow(self, context: FailureContext) -> bool:
        """Check if operation should be allowed"""
        key = context.key()
        state = self._get_state(key)
        
        if state == CircuitBreaker.State.CLOSED:
            return True
        elif state == CircuitBreaker.State.OPEN:
            # Check if recovery timeout has passed
            with self._lock:
                open_time = self._open_times.get(key, 0)
                if context.timestamp - open_time >= self.recovery_timeout:
                    self._states[key] = CircuitBreaker.State.HALF_OPEN
                    self._success_counts[key] = 0
                    self._state_cache[key] = (CircuitBreaker.State.HALF_OPEN, context.timestamp)
                    self.logger.info(f"Circuit {key} entering HALF_OPEN state")
                    return True
            return False
        else:  # HALF_OPEN
            return True
    
    def record_failure(self, context: FailureContext) -> None:
        """Record a failure and potentially open circuit"""
        key = context.key()
        now = context.timestamp
        
        with self._lock:
            state = self._states.get(key, CircuitBreaker.State.CLOSED)
            
            self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
            self._failure_history[key].append(now)
            self._request_history[key].append((now, False))
            
            # Invalidate cache
            self._state_cache.pop(key, None)
            
            if state == CircuitBreaker.State.HALF_OPEN:
                # Failure during recovery - reopen circuit
                self._states[key] = CircuitBreaker.State.OPEN
                self._open_times[key] = now
                self._failure_counts[key] = 0
                self.logger.error(f"Circuit {key} reopened after failure in HALF_OPEN")
            elif state == CircuitBreaker.State.CLOSED:
                # Check if we should open based on failure count
                if self._failure_counts[key] >= self.failure_threshold:
                    self._states[key] = CircuitBreaker.State.OPEN
                    self._open_times[key] = now
                    self._failure_counts[key] = 0
                    self.logger.error(
                        f"Circuit {key} OPENED after {self.failure_threshold} failures"
                    )
                # Also check error rate
                elif self._should_open_on_error_rate(key):
                    self._states[key] = CircuitBreaker.State.OPEN
                    self._open_times[key] = now
                    self._failure_counts[key] = 0
                    self.logger.error(f"Circuit {key} OPENED due to high error rate")
    
    def _should_open_on_error_rate(self, key: str) -> bool:
        """Check if circuit should open based on error rate"""
        history = self._request_history.get(key, deque())
        if len(history) < self.min_requests_for_error_rate:
            return False
        
        # Calculate error rate from recent requests
        now = time()
        window_start = now - 60.0  # Last 60 seconds
        recent_requests = [success for ts, success in history if ts >= window_start]
        
        if len(recent_requests) < self.min_requests_for_error_rate:
            return False
        
        error_rate = 1.0 - (sum(recent_requests) / len(recent_requests))
        return error_rate >= self.error_rate_threshold
    
    def record_success(self, scope_key: str) -> None:
        """Record a success and potentially close circuit"""
        with self._lock:
            state = self._states.get(scope_key, CircuitBreaker.State.CLOSED)
            self._request_history[scope_key].append((time(), True))
            
            # Invalidate cache
            self._state_cache.pop(scope_key, None)
            
            if state == CircuitBreaker.State.HALF_OPEN:
                self._success_counts[scope_key] = self._success_counts.get(scope_key, 0) + 1
                if self._success_counts[scope_key] >= self.success_threshold:
                    self._states[scope_key] = CircuitBreaker.State.CLOSED
                    self._failure_counts[scope_key] = 0
                    self.logger.info(f"Circuit {scope_key} CLOSED after successful recovery")
            elif state == CircuitBreaker.State.CLOSED:
                # Reset failure count on success
                self._failure_counts[scope_key] = 0
    
    def is_open(self, scope_key: str) -> bool:
        """Check if circuit is open"""
        return self._get_state(scope_key) == CircuitBreaker.State.OPEN
    
    def detect_correlated_failures(self) -> List[str]:
        """Detect patterns of correlated failures across circuits"""
        now = time()
        recent_window = 60.0  # Last 60 seconds
        
        with self._lock:
            correlated = []
            for key, history in self._failure_history.items():
                recent_failures = [ts for ts in history if now - ts < recent_window]
                if len(recent_failures) >= 3:
                    correlated.append(key)
        
        return correlated
    
    def get_circuit_stats(self, scope_key: str) -> Dict[str, Any]:
        """Get statistics for a specific circuit"""
        with self._lock:
            state = self._states.get(scope_key, CircuitBreaker.State.CLOSED)
            failure_count = self._failure_counts.get(scope_key, 0)
            success_count = self._success_counts.get(scope_key, 0)
            open_time = self._open_times.get(scope_key, 0)
            
            history = self._request_history.get(scope_key, deque())
            total_requests = len(history)
            recent_requests = [success for _, success in list(history)[-10:]]
            recent_success_rate = sum(recent_requests) / len(recent_requests) if recent_requests else 0.0
            
            return {
                "state": state.value,
                "failure_count": failure_count,
                "success_count": success_count,
                "open_time": open_time,
                "total_requests": total_requests,
                "recent_success_rate": recent_success_rate,
                "is_open": state == CircuitBreaker.State.OPEN,
            }
    
    def get_stats(self) -> Dict[str, Any]:
        """Get circuit breaker statistics"""
        with self._lock:
            open_circuits = sum(
                1 for state in self._states.values()
                if state == CircuitBreaker.State.OPEN
            )
            half_open_circuits = sum(
                1 for state in self._states.values()
                if state == CircuitBreaker.State.HALF_OPEN
            )
            
            return {
                "failure_threshold": self.failure_threshold,
                "recovery_timeout": self.recovery_timeout,
                "success_threshold": self.success_threshold,
                "total_circuits": len(self._states),
                "open_circuits": open_circuits,
                "half_open_circuits": half_open_circuits,
                "closed_circuits": len(self._states) - open_circuits - half_open_circuits,
            }


# ============================================================================
# CONTAINMENT POLICY - Catastrophe Mode (Enhanced)
# ============================================================================

class ContainmentPolicy:
    """
    Nuclear safety valve for catastrophic scenarios.
    System-wide protection when everything else fails.
    
    Thread-safe with audit logging.
    """
    
    class ContainmentLevel(Enum):
        NORMAL = 0
        ELEVATED = 1
        HIGH = 2
        CRITICAL = 3
        LOCKDOWN = 4
        
        def is_lockdown(self) -> bool:
            """Check if level is lockdown"""
            return self == ContainmentPolicy.ContainmentLevel.LOCKDOWN
    
    def __init__(self, audit_log_enabled: bool = True):
        self.audit_log_enabled = audit_log_enabled
        
        self._lock = RLock()
        self._level = ContainmentPolicy.ContainmentLevel.NORMAL
        self._lockdown_time: Optional[float] = None
        self._trigger_reason: Optional[str] = None
        self._snapshot: Dict[str, Any] = {}
        self._activation_history: Deque[Dict[str, Any]] = deque(maxlen=100)
        self._manual_override: bool = False
        self._override_reason: Optional[str] = None
        self._override_time: Optional[float] = None
        
        self.logger = logging.getLogger(f"{__name__}.ContainmentPolicy")
    
    def should_activate(self, context: FailureContext) -> bool:
        """Determine if containment should be activated"""
        if self._manual_override:
            return False
        
        # Trust collapse
        if context.failure_type == FailureType.TRUST_DEPLETION and context.scope == ResourceScope.GLOBAL:
            return True
        
        # Platform enforcement
        if context.failure_type == FailureType.PLATFORM_RISK and context.attempt > 3:
            return True
        
        # Invariant violation
        if context.failure_type == FailureType.INVARIANT_VIOLATION:
            return True
        
        # Global safety
        if context.failure_type == FailureType.GLOBAL_SAFETY:
            return True
        
        return False
    
    def activate(self, context: FailureContext, severity: ContainmentLevel) -> None:
        """Activate containment at specified level"""
        with self._lock:
            old_level = self._level
            self._level = severity
            self._lockdown_time = context.timestamp
            self._trigger_reason = f"{context.failure_type.value} - {context.scope_id}"
            
            # Take snapshot
            self._snapshot = {
                "context": context.to_dict(),
                "timestamp": context.timestamp,
                "severity": severity.value,
                "reason": self._trigger_reason,
                "previous_level": old_level.value,
            }
            
            # Record in history
            self._activation_history.append({
                "timestamp": context.timestamp,
                "severity": severity.value,
                "reason": self._trigger_reason,
                "context": context.to_dict(),
            })
            
            # Audit log
            if self.audit_log_enabled:
                self.logger.critical(
                    f"CONTAINMENT ACTIVATED: Level {severity.name} (was {old_level.name}) - {self._trigger_reason}",
                    extra={"containment_snapshot": self._snapshot}
                )
    
    def get_containment_action(self) -> ResponseAction:
        """Get appropriate action based on containment level"""
        with self._lock:
            if self._level == ContainmentPolicy.ContainmentLevel.LOCKDOWN:
                return ResponseAction.LOCKDOWN
            elif self._level == ContainmentPolicy.ContainmentLevel.CRITICAL:
                return ResponseAction.ABORT
            elif self._level == ContainmentPolicy.ContainmentLevel.HIGH:
                return ResponseAction.DEGRADE
            else:
                return ResponseAction.DEFER
    
    def is_active(self) -> bool:
        """Check if containment is currently active"""
        with self._lock:
            return self._level != ContainmentPolicy.ContainmentLevel.NORMAL
    
    def get_level(self) -> ContainmentLevel:
        """Get current containment level"""
        with self._lock:
            return self._level
    
    def get_recovery_delay(self) -> float:
        """Get required delay before recovery attempt"""
        delays = {
            ContainmentPolicy.ContainmentLevel.ELEVATED: 300.0,    # 5 min
            ContainmentPolicy.ContainmentLevel.HIGH: 3600.0,       # 1 hour
            ContainmentPolicy.ContainmentLevel.CRITICAL: 14400.0,  # 4 hours
            ContainmentPolicy.ContainmentLevel.LOCKDOWN: 86400.0   # 24 hours
        }
        with self._lock:
            return delays.get(self._level, 0.0)
    
    def reset(self, reason: Optional[str] = None) -> None:
        """Reset containment to normal"""
        with self._lock:
            old_level = self._level
            self._level = ContainmentPolicy.ContainmentLevel.NORMAL
            self._lockdown_time = None
            self._trigger_reason = None
            self._manual_override = False
            self._override_reason = None
            self._override_time = None
        
        self.logger.info(
            f"Containment reset to NORMAL (was {old_level.name})" + (f" - {reason}" if reason else "")
        )
    
    def set_manual_override(self, override: bool, reason: str) -> None:
        """Manually override containment (for emergency operations)"""
        with self._lock:
            self._manual_override = override
            self._override_reason = reason
            self._override_time = time() if override else None
        
        if override:
            self.logger.warning(f"MANUAL OVERRIDE ENABLED: {reason}")
        else:
            self.logger.info(f"MANUAL OVERRIDE DISABLED: {reason}")
    
    def get_snapshot(self) -> Dict[str, Any]:
        """Get current containment snapshot"""
        with self._lock:
            return {
                "level": self._level.value,
                "lockdown_time": self._lockdown_time,
                "trigger_reason": self._trigger_reason,
                "snapshot": self._snapshot.copy(),
                "manual_override": self._manual_override,
                "override_reason": self._override_reason,
                "override_time": self._override_time,
            }
    
    def get_activation_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent activation history"""
        with self._lock:
            return list(self._activation_history)[-limit:]


# ============================================================================
# POLICY REGISTRY - Explicit Rules (Enhanced)
# ============================================================================

class PolicyRegistry:
    """
    Code-declared policies for deterministic failure handling.
    No config magic - all rules are explicit.
    
    Enhanced with adaptive policies and cost awareness.
    """
    
    POLICY_VERSION = "2.0.0"
    
    @staticmethod
    @lru_cache(maxsize=1000)
    def _get_policy_cached(
        failure_type: str,
        resource_type: str,
        attempt: int,
        cost_estimate: Optional[float],
    ) -> Tuple[str, Optional[float], Optional[int], Optional[str]]:
        """Cached policy lookup for performance"""
        ft = FailureType(failure_type)
        rt = ResourceType(resource_type)
        
        # TRUST DEPLETION - Most severe
        if ft == FailureType.TRUST_DEPLETION:
            return ("lockdown", 86400.0, None, "Trust recovery window required")
        
        # GLOBAL SAFETY - Immediate halt
        if ft == FailureType.GLOBAL_SAFETY:
            return ("lockdown", None, None, "Global safety constraint violated")
        
        # INVARIANT VIOLATION - Critical error
        if ft == FailureType.INVARIANT_VIOLATION:
            return ("abort", None, None, "System invariant violated - manual intervention required")
        
        # PLATFORM RISK - Degrade or defer
        if ft == FailureType.PLATFORM_RISK:
            if attempt < 3:
                return ("defer", 3600.0, None, None)
            else:
                return ("degrade", None, 2, None)
        
        # RATE LIMIT - Backoff
        if ft == FailureType.RATE_LIMIT:
            delay = 60.0 * (2 ** min(attempt, 5))  # Exponential
            return ("backoff", delay, None, None)
        
        # RESOURCE EXHAUSTED - Retry with degradation
        if ft == FailureType.RESOURCE_EXHAUSTED:
            if attempt < 5:
                return ("retry", 30.0 * attempt, None, None)
            else:
                return ("degrade", None, 1, None)
        
        # Default: defer
        return ("defer", 60.0, None, None)
    
    @classmethod
    def get_policy(cls, context: FailureContext) -> FailureDecision:
        """
        Get policy decision for given context.
        Deterministic: same input -> same output.
        """
        # Use cached lookup
        action_str, delay, degrade_level, abort_reason = cls._get_policy_cached(
            context.failure_type.value,
            context.resource_type.value,
            context.attempt,
            context.cost_estimate,
        )
        
        action = ResponseAction(action_str)
        
        # Calculate cost if available
        estimated_cost = None
        cost_reason = None
        if context.cost_estimate:
            cost_multiplier = context.resource_type.cost_multiplier()
            estimated_cost = context.cost_estimate * cost_multiplier
            cost_reason = f"Base cost {context.cost_estimate} * multiplier {cost_multiplier}"
        
        return FailureDecision(
            action=action,
            delay_seconds=delay,
            degrade_level=degrade_level,
            abort_reason=abort_reason,
            policy_version=cls.POLICY_VERSION,
            estimated_cost=estimated_cost,
            cost_reason=cost_reason,
        )


# ============================================================================
# FAILURE WATCHDOG - Detection & Escalation (Enhanced)
# ============================================================================

class FailureWatchdog:
    """
    Monitors for pathological failure patterns.
    Escalates to safety systems when needed.
    
    Enhanced with advanced pattern detection and alerting.
    """
    
    def __init__(
        self,
        window_seconds: float = 300.0,
        alert_handlers: Optional[List[AlertHandler]] = None,
        enable_advanced_detection: bool = True,
    ):
        self.window_seconds = window_seconds
        self.alert_handlers = alert_handlers or []
        self.enable_advanced_detection = enable_advanced_detection
        
        self._lock = RLock()
        self._events: DefaultDict[str, Deque[Dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=1000)
        )
        self._alerts_sent: Set[str] = set()
        self._alert_cooldowns: Dict[str, float] = {}
        self._alert_cooldown_seconds: float = 300.0  # 5 minutes between same alerts
        
        # Advanced pattern detection
        self._failure_sequences: DefaultDict[str, Deque[FailureType]] = defaultdict(
            lambda: deque(maxlen=20)
        )
        self._correlation_matrix: DefaultDict[Tuple[FailureType, FailureType], int] = defaultdict(int)
        
        self.logger = logging.getLogger(f"{__name__}.FailureWatchdog")
    
    def record_event(self, context: FailureContext, decision: FailureDecision) -> None:
        """Record a failure event"""
        key = context.key()
        now = context.timestamp
        
        with self._lock:
            self._events[key].append({
                "timestamp": now,
                "failure_type": context.failure_type,
                "action": decision.action,
                "attempt": context.attempt,
                "resource_type": context.resource_type,
                "scope": context.scope,
            })
            
            # Track failure sequences for pattern detection
            if self.enable_advanced_detection:
                self._failure_sequences[key].append(context.failure_type)
                if len(self._failure_sequences[key]) >= 2:
                    prev_type = self._failure_sequences[key][-2]
                    curr_type = self._failure_sequences[key][-1]
                    self._correlation_matrix[(prev_type, curr_type)] += 1
    
    def detect_retry_storm(self, scope_key: str) -> bool:
        """Detect excessive retries in short time"""
        with self._lock:
            events = self._events.get(scope_key, deque())
            if len(events) < 10:
                return False
            
            now = time()
            recent = [e for e in events if now - e["timestamp"] < 60.0]
            retry_count = sum(1 for e in recent if e["action"].allows_retry())
            
            return retry_count >= 10
    
    def detect_denial_loop(self, scope_key: str) -> bool:
        """Detect repeated denials without progress"""
        with self._lock:
            events = self._events.get(scope_key, deque())
            if len(events) < 5:
                return False
            
            # Check last N events
            last_5 = list(events)[-5:]
            all_denials = all(
                e["action"].is_terminal()
                for e in last_5
            )
            
            return all_denials
    
    def detect_runaway_degradation(self, scope_key: str) -> bool:
        """Detect cascading degradation"""
        with self._lock:
            events = self._events.get(scope_key, deque())
            if len(events) < 3:
                return False
            
            last_3 = list(events)[-3:]
            all_degrade = all(e["action"] == ResponseAction.DEGRADE for e in last_3)
            
            return all_degrade
    
    def detect_escalating_failures(self, scope_key: str) -> bool:
        """Detect escalating failure severity"""
        if not self.enable_advanced_detection:
            return False
        
        with self._lock:
            sequence = list(self._failure_sequences.get(scope_key, deque()))
            if len(sequence) < 3:
                return False
            
            # Check if severity is increasing
            severities = [ft.severity_score() for ft in sequence[-3:]]
            return severities[0] < severities[1] < severities[2]
    
    def detect_correlated_failures(self, scope_keys: List[str]) -> List[Tuple[str, str]]:
        """Detect correlated failures across multiple scopes"""
        if not self.enable_advanced_detection:
            return []
        
        with self._lock:
            correlated = []
            now = time()
            window = 10.0  # 10 second window
            
            for i, key1 in enumerate(scope_keys):
                events1 = self._events.get(key1, deque())
                recent1 = [e for e in events1 if now - e["timestamp"] < window]
                
                for key2 in scope_keys[i+1:]:
                    events2 = self._events.get(key2, deque())
                    recent2 = [e for e in events2 if now - e["timestamp"] < window]
                    
                    # Check if failures occurred within window
                    if recent1 and recent2:
                        time_diff = abs(recent1[-1]["timestamp"] - recent2[-1]["timestamp"])
                        if time_diff < window:
                            correlated.append((key1, key2))
            
            return correlated
    
    def check_and_alert(self, scope_key: str) -> Optional[str]:
        """Check for issues and return alert if found"""
        alerts = []
        
        if self.detect_retry_storm(scope_key):
            alerts.append(("RETRY_STORM", f"RETRY STORM detected: {scope_key}"))
        
        if self.detect_denial_loop(scope_key):
            alerts.append(("DENIAL_LOOP", f"DENIAL LOOP detected: {scope_key}"))
        
        if self.detect_runaway_degradation(scope_key):
            alerts.append(("RUNAWAY_DEGRADATION", f"RUNAWAY DEGRADATION detected: {scope_key}"))
        
        if self.detect_escalating_failures(scope_key):
            alerts.append(("ESCALATING_FAILURES", f"ESCALATING FAILURES detected: {scope_key}"))
        
        # Send alerts with cooldown
        for alert_type, alert_message in alerts:
            alert_key = f"{alert_type}:{scope_key}"
            
            with self._lock:
                # Check cooldown
                last_alert = self._alert_cooldowns.get(alert_key, 0)
                if time() - last_alert < self._alert_cooldown_seconds:
                    continue
                
                if alert_key not in self._alerts_sent:
                    self._alerts_sent.add(alert_key)
                    self._alert_cooldowns[alert_key] = time()
                    
                    self.logger.error(alert_message)
                    
                    # Send to alert handlers
                    for handler in self.alert_handlers:
                        try:
                            handler.send_alert(
                                severity="error",
                                message=alert_message,
                                context={"scope_key": scope_key, "alert_type": alert_type}
                            )
                        except Exception as e:
                            self.logger.warning(f"Failed to send alert via handler: {e}")
                    
                    return alert_message
        
        return None
    
    def get_pattern_stats(self) -> Dict[str, Any]:
        """Get pattern detection statistics"""
        with self._lock:
            return {
                "total_scopes_tracked": len(self._events),
                "total_events": sum(len(events) for events in self._events.values()),
                "alerts_sent": len(self._alerts_sent),
                "correlation_matrix_size": len(self._correlation_matrix),
                "top_correlations": sorted(
                    self._correlation_matrix.items(),
                    key=lambda x: x[1],
                    reverse=True
                )[:10],
            }


# ============================================================================
# HEALTH MONITOR
# ============================================================================

class HealthMonitor:
    """
    Monitors health of failure policy system.
    Tracks system performance and detects anomalies.
    """
    
    def __init__(self, check_interval_seconds: float = 60.0):
        self.check_interval = check_interval_seconds
        self._lock = Lock()
        self._health_status: Dict[str, Any] = {
            "status": "healthy",
            "last_check": time(),
            "checks_performed": 0,
            "issues_detected": [],
        }
        self._metrics_history: Deque[Dict[str, Any]] = deque(maxlen=100)
        
        self.logger = logging.getLogger(f"{__name__}.HealthMonitor")
    
    def check_health(
        self,
        metrics: FailureMetrics,
        retry_controller: RetryController,
        circuit_breaker: CircuitBreaker,
        containment_policy: ContainmentPolicy,
    ) -> Dict[str, Any]:
        """Perform health check"""
        with self._lock:
            now = time()
            issues = []
            
            # Check metrics
            stats = metrics.get_stats()
            decision_times = stats.get("performance", {}).get("p99_decision_time_ms", 0)
            if decision_times > 1000.0:  # 1 second
                issues.append("High decision latency detected")
            
            # Check circuit breakers
            cb_stats = circuit_breaker.get_stats()
            open_circuits = cb_stats.get("open_circuits", 0)
            if open_circuits > 10:
                issues.append(f"Too many open circuits: {open_circuits}")
            
            # Check containment
            if containment_policy.is_active():
                issues.append(f"Containment active at level {containment_policy.get_level().name}")
            
            # Determine overall status
            status = "degraded" if issues else "healthy"
            if containment_policy.get_level() == ContainmentPolicy.ContainmentLevel.LOCKDOWN:
                status = "critical"
            
            self._health_status = {
                "status": status,
                "last_check": now,
                "checks_performed": self._health_status["checks_performed"] + 1,
                "issues_detected": issues,
                "metrics": stats,
                "circuit_breaker_stats": cb_stats,
                "containment_level": containment_policy.get_level().value,
            }
            
            self._metrics_history.append(self._health_status.copy())
            
            return self._health_status
    
    def get_health_status(self) -> Dict[str, Any]:
        """Get current health status"""
        with self._lock:
            return self._health_status.copy()
    
    def get_health_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get health check history"""
        with self._lock:
            return list(self._metrics_history)[-limit:]


# ============================================================================
# STATE PERSISTENCE
# ============================================================================

class StatePersistence:
    """
    Handles persistence of failure policy state.
    Allows recovery and audit trails.
    """
    
    def __init__(self, persistence_path: Optional[Path] = None):
        self.persistence_path = persistence_path or Path("/tmp/failure_policy_state")
        self.persistence_path.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        
        self.logger = logging.getLogger(f"{__name__}.StatePersistence")
    
    def save_state(
        self,
        retry_controller: RetryController,
        degradation_controller: DegradationController,
        circuit_breaker: CircuitBreaker,
        containment_policy: ContainmentPolicy,
        suffix: str = "",
    ) -> bool:
        """Save current state to disk"""
        try:
            with self._lock:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"failure_policy_state_{timestamp}{suffix}.json"
                filepath = self.persistence_path / filename
                
                state = {
                    "timestamp": time(),
                    "retry_controller": retry_controller.get_stats(),
                    "degradation_controller": degradation_controller.get_stats(),
                    "circuit_breaker": circuit_breaker.get_stats(),
                    "containment_policy": containment_policy.get_snapshot(),
                }
                
                with open(filepath, 'w') as f:
                    json.dump(state, f, indent=2, default=str)
                
                self.logger.info(f"State saved to {filepath}")
                return True
        except Exception as e:
            self.logger.error(f"Failed to save state: {e}")
            return False
    
    def load_state(self) -> Optional[Dict[str, Any]]:
        """Load most recent state from disk"""
        try:
            with self._lock:
                state_files = sorted(
                    self.persistence_path.glob("failure_policy_state_*.json"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True
                )
                
                if not state_files:
                    return None
                
                with open(state_files[0], 'r') as f:
                    state = json.load(f)
                
                self.logger.info(f"State loaded from {state_files[0]}")
                return state
        except Exception as e:
            self.logger.error(f"Failed to load state: {e}")
            return None


# ============================================================================
# FAILURE POLICY ENGINE - Core Decision System (Enhanced)
# ============================================================================

class FailurePolicyEngine:
    """
    Core policy engine that orchestrates all failure handling.
    Deterministic, rules-based decision making.
    
    Production-grade with:
    - Thread-safe operations
    - Comprehensive metrics
    - Performance optimizations
    - Health monitoring
    - State persistence
    """
    
    def __init__(
        self,
        metrics_collector: Optional[MetricsCollector] = None,
        alert_handlers: Optional[List[AlertHandler]] = None,
        enable_persistence: bool = False,
        persistence_path: Optional[Path] = None,
        health_check_interval: float = 60.0,
    ):
        # Core components
        self.retry_controller = RetryController()
        self.degradation_controller = DegradationController()
        self.circuit_breaker = CircuitBreaker()
        self.containment_policy = ContainmentPolicy()
        self.watchdog = FailureWatchdog(alert_handlers=alert_handlers)
        
        # Metrics and observability
        self.metrics = FailureMetrics(collector=metrics_collector)
        self.health_monitor = HealthMonitor(check_interval_seconds=health_check_interval)
        
        # State persistence
        self.enable_persistence = enable_persistence
        self.state_persistence = StatePersistence(persistence_path) if enable_persistence else None
        
        # Thread safety
        self._lock = RLock()
        self._decision_cache: Dict[str, Tuple[FailureDecision, float]] = {}
        self._cache_ttl: float = 5.0  # Cache decisions for 5 seconds
        
        # Performance tracking
        self._total_decisions: int = 0
        self._decision_times: Deque[float] = deque(maxlen=10000)
        
        # Background tasks
        self._health_check_thread: Optional[Thread] = None
        self._health_check_event = Event()
        self._running = False
        
        self.logger = logging.getLogger(__name__)
        
        # Start background health checks
        if health_check_interval > 0:
            self._start_health_checks()
    
    def _start_health_checks(self) -> None:
        """Start background health check thread"""
        def health_check_loop():
            while not self._health_check_event.wait(self.health_monitor.check_interval):
                try:
                    self.health_monitor.check_health(
                        self.metrics,
                        self.retry_controller,
                        self.circuit_breaker,
                        self.containment_policy,
                    )
                except Exception as e:
                    self.logger.error(f"Health check failed: {e}")
        
        self._health_check_thread = Thread(target=health_check_loop, daemon=True)
        self._health_check_thread.start()
        self._running = True
    
    def decide(self, context: FailureContext) -> FailureDecision:
        """
        Main decision function - determines how to handle failure.
        DETERMINISTIC: same context -> same decision.
        
        Thread-safe and optimized with caching.
        """
        start_time = perf_counter()
        key = context.key()
        
        # Check cache first (for identical contexts within TTL)
        cache_key = self._make_cache_key(context)
        now = time()
        
        with self._lock:
            if cache_key in self._decision_cache:
                cached_decision, cache_time = self._decision_cache[cache_key]
                if now - cache_time < self._cache_ttl:
                    # Update metadata with cache hit
                    cached_decision.metadata["cache_hit"] = True
                    return cached_decision
        
        # Main decision logic
        try:
            # 1. Check containment first
            if self.containment_policy.is_active():
                decision = FailureDecision(
                    action=self.containment_policy.get_containment_action(),
                    abort_reason="System in containment mode",
                    delay_seconds=self.containment_policy.get_recovery_delay(),
                    policy_version=PolicyRegistry.POLICY_VERSION,
                )
            elif self.containment_policy.should_activate(context):
                severity = self._determine_severity(context)
                self.containment_policy.activate(context, severity)
                decision = FailureDecision(
                    action=ResponseAction.LOCKDOWN,
                    abort_reason=f"Containment activated: {context.failure_type.value}",
                    policy_version=PolicyRegistry.POLICY_VERSION,
                )
            # 2. Check circuit breaker
            elif not self.circuit_breaker.should_allow(context):
                decision = FailureDecision(
                    action=ResponseAction.ABORT,
                    abort_reason="Circuit breaker open",
                    delay_seconds=self.circuit_breaker.recovery_timeout,
                    policy_version=PolicyRegistry.POLICY_VERSION,
                )
            else:
                # 3. Get base policy from registry
                base_decision = PolicyRegistry.get_policy(context)
                
                # 4. Apply retry logic if applicable
                if base_decision.action == ResponseAction.RETRY:
                    if not self.retry_controller.can_retry(context):
                        # Out of retries - degrade instead
                        base_decision = FailureDecision(
                            action=ResponseAction.DEGRADE,
                            degrade_level=self.degradation_controller.get_degradation_level(context),
                            policy_version=PolicyRegistry.POLICY_VERSION,
                        )
                    else:
                        delay = self.retry_controller.calculate_delay(context)
                        base_decision.delay_seconds = delay
                        self.retry_controller.record_retry(context)
                
                # 5. Apply degradation if needed
                if base_decision.action == ResponseAction.DEGRADE:
                    level = base_decision.degrade_level or self.degradation_controller.get_degradation_level(context)
                    metadata = self.degradation_controller.apply_degradation(context, level)
                    base_decision.metadata.update(metadata)
                
                decision = base_decision
            
            # 6. Record failure for circuit breaker
            self.circuit_breaker.record_failure(context)
            
            # 7. Record for watchdog
            self.watchdog.record_event(context, decision)
            
            # 8. Check for pathological patterns
            alert = self.watchdog.check_and_alert(key)
            if alert:
                decision.metadata["watchdog_alert"] = alert
            
            # Calculate decision time
            decision_time_ms = (perf_counter() - start_time) * 1000.0
            decision.decision_time_ms = decision_time_ms
            
            # Record metrics
            self.metrics.record_decision(context, decision, decision_time_ms)
            
            # Update performance tracking
            with self._lock:
                self._total_decisions += 1
                self._decision_times.append(decision_time_ms)
                self._decision_cache[cache_key] = (decision, now)
                
                # Cleanup old cache entries
                if len(self._decision_cache) > 10000:
                    # Remove oldest 20%
                    sorted_items = sorted(
                        self._decision_cache.items(),
                        key=lambda x: x[1][1]
                    )
                    for old_key, _ in sorted_items[:2000]:
                        self._decision_cache.pop(old_key, None)
            
            self.logger.info(
                f"Failure decision: {key} -> {decision.action.value} "
                f"(attempt {context.attempt}, {decision_time_ms:.2f}ms)"
            )
            
            # Persist state if enabled
            if self.enable_persistence and self.state_persistence and self._total_decisions % 100 == 0:
                try:
                    self.state_persistence.save_state(
                        self.retry_controller,
                        self.degradation_controller,
                        self.circuit_breaker,
                        self.containment_policy,
                    )
                except Exception as e:
                    self.logger.warning(f"Failed to persist state: {e}")
            
            return decision
            
        except Exception as e:
            self.logger.error(f"Error in decide(): {e}", exc_info=True)
            # Fallback to safe default
            return FailureDecision(
                action=ResponseAction.DEFER,
                delay_seconds=60.0,
                abort_reason=f"Policy engine error: {str(e)}",
                policy_version=PolicyRegistry.POLICY_VERSION,
            )
    
    def _make_cache_key(self, context: FailureContext) -> str:
        """Create cache key from context"""
        key_parts = [
            context.failure_type.value,
            context.resource_type.value,
            context.scope.value,
            context.scope_id,
            str(context.attempt),
        ]
        return ":".join(key_parts)
    
    def _determine_severity(self, context: FailureContext) -> ContainmentPolicy.ContainmentLevel:
        """Determine containment severity level"""
        if context.failure_type == FailureType.GLOBAL_SAFETY:
            return ContainmentPolicy.ContainmentLevel.LOCKDOWN
        elif context.failure_type == FailureType.TRUST_DEPLETION:
            return ContainmentPolicy.ContainmentLevel.CRITICAL
        elif context.failure_type == FailureType.INVARIANT_VIOLATION:
            return ContainmentPolicy.ContainmentLevel.HIGH
        else:
            return ContainmentPolicy.ContainmentLevel.ELEVATED
    
    def record_success(self, scope_key: str) -> None:
        """Record successful operation - resets counters"""
        self.retry_controller.reset_retry_count(scope_key)
        self.circuit_breaker.record_success(scope_key)
        self.degradation_controller.record_success(scope_key)
    
    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive system status"""
        return {
            "containment": {
                "active": self.containment_policy.is_active(),
                "level": self.containment_policy.get_level().value,
            },
            "circuit_breaker": self.circuit_breaker.get_stats(),
            "retry_controller": self.retry_controller.get_stats(),
            "degradation_controller": self.degradation_controller.get_stats(),
            "watchdog": self.watchdog.get_pattern_stats(),
            "metrics": self.metrics.get_stats(),
            "health": self.health_monitor.get_health_status(),
            "performance": {
                "total_decisions": self._total_decisions,
                "avg_decision_time_ms": (
                    sum(self._decision_times) / len(self._decision_times)
                    if self._decision_times else 0.0
                ),
                "cache_size": len(self._decision_cache),
            },
        }
    
    def shutdown(self) -> None:
        """Gracefully shutdown the engine"""
        self._health_check_event.set()
        if self._health_check_thread:
            self._health_check_thread.join(timeout=5.0)
        
        if self.enable_persistence and self.state_persistence:
            self.state_persistence.save_state(
                self.retry_controller,
                self.degradation_controller,
                self.circuit_breaker,
                self.containment_policy,
                suffix="_shutdown",
            )
        
        self._running = False
        self.logger.info("Failure policy engine shut down")


# ============================================================================
# INTEGRATION HELPERS
# ============================================================================

def create_failure_context_from_governor_decision(
    decision: GovernorDecision,
    failure_type: FailureType,
    resource_type: ResourceType,
    scope: ResourceScope,
    scope_id: str,
    requester: str,
    attempt: int = 1,
    **kwargs
) -> FailureContext:
    """
    Helper to create FailureContext from governor decision.
    Used by priority_router.py and factory_scheduler.py.
    """
    return FailureContext(
        decision=decision,
        failure_type=failure_type,
        resource_type=resource_type,
        scope=scope,
        scope_id=scope_id,
        requester=requester,
        attempt=attempt,
        timestamp=time(),
        **kwargs
    )


def create_failure_context_from_resource_pressure(
    resource_pressure: Any,  # ResourcePressure from factory_scheduler
    resource_type: ResourceType,
    scope: ResourceScope,
    scope_id: str,
    requester: str,
    attempt: int = 1,
) -> Optional[FailureContext]:
    """
    Helper to create FailureContext from ResourcePressure.
    Used by factory_scheduler.py integration.
    """
    # Determine if pressure indicates a failure
    if hasattr(resource_pressure, 'is_under_pressure'):
        if resource_pressure.is_under_pressure():
            # Map pressure to failure type
            if hasattr(resource_pressure, 'api_quota_remaining'):
                if resource_pressure.api_quota_remaining < 0.1:
                    failure_type = FailureType.RESOURCE_EXHAUSTED
                else:
                    failure_type = FailureType.RATE_LIMIT
            else:
                failure_type = FailureType.RESOURCE_EXHAUSTED
            
            return FailureContext(
                decision=GovernorDecision.DENIED,
                failure_type=failure_type,
                resource_type=resource_type,
                scope=scope,
                scope_id=scope_id,
                requester=requester,
                attempt=attempt,
                timestamp=time(),
            )
    
    return None


# ============================================================================
# EXAMPLE USAGE & TESTING
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Initialize engine
    engine = FailurePolicyEngine(
        enable_persistence=True,
        health_check_interval=30.0,
    )
    
    # Example 1: Rate limit failure
    context1 = FailureContext(
        decision=GovernorDecision.DENIED,
        failure_type=FailureType.RATE_LIMIT,
        resource_type=ResourceType.API_CALLS,
        scope=ResourceScope.WORKFLOW,
        scope_id="workflow_123",
        requester="content_generator",
        attempt=1,
        timestamp=time()
    )
    
    decision1 = engine.decide(context1)
    print(f"Decision 1: {decision1.action.value} - delay: {decision1.delay_seconds}s")
    
    # Example 2: Trust depletion (severe)
    context2 = FailureContext(
        decision=GovernorDecision.DENIED,
        failure_type=FailureType.TRUST_DEPLETION,
        resource_type=ResourceType.TRUST,
        scope=ResourceScope.GLOBAL,
        scope_id="global",
        requester="posting_workflow",
        attempt=1,
        timestamp=time()
    )
    
    decision2 = engine.decide(context2)
    print(f"Decision 2: {decision2.action.value} - reason: {decision2.abort_reason}")
    
    # Example 3: Multiple retry attempts
    for attempt in range(1, 6):
        context3 = FailureContext(
            decision=GovernorDecision.DENIED,
            failure_type=FailureType.RESOURCE_EXHAUSTED,
            resource_type=ResourceType.COMPUTE,
            scope=ResourceScope.WORKFLOW,
            scope_id="workflow_456",
            requester="batch_processor",
            attempt=attempt,
            timestamp=time()
        )
        decision3 = engine.decide(context3)
        print(f"Attempt {attempt}: {decision3.action.value}")
    
    # Check system status
    status = engine.get_status()
    print(f"\nSystem Status: {json.dumps(status, indent=2, default=str)}")
    
    # Shutdown
    engine.shutdown()
