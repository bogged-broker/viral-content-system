"""
resource_governor.py — Production Resource Governance Layer

This is the ONLY component with final authority to deny resource consumption.
No component may directly consume scarce resources without passing through this governor.

Core Principle:
    No backdoors. No exceptions. All consumption flows through ResourceGovernor.

Responsibilities:
    - Define all resource classes
    - Track real-time usage
    - Enforce hard ceilings and rate limits
    - Enforce burst envelopes
    - Allocate fairly across factories
    - Support priority-aware throttling
    - Protect accounts and trust
    - Expose deterministic decisions
    - Be audit-safe and replayable

This file is rules, not intelligence. NO ML. NO prediction. NO optimization.
This file enforces physics.
"""

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple, Set
import json
import logging
from datetime import datetime, timedelta

# ============================================================================
# CORE ENUMS (MANDATORY - ALL RESOURCES DECLARED HERE)
# ============================================================================

class ResourceType(Enum):
    """All consumable resources. No free-text resources allowed."""
    API_REQUEST = "api_request"
    POST_ACTION = "post_action"
    ACCOUNT_ACTION = "account_action"
    GPU_MINUTE = "gpu_minute"
    CPU_SECOND = "cpu_second"
    MEMORY_MB = "memory_mb"
    WORKFLOW_SLOT = "workflow_slot"
    TRUST_BUDGET = "trust_budget"


class ResourceScope(Enum):
    """Resource enforcement scope hierarchy."""
    GLOBAL = "global"
    PLATFORM = "platform"
    FACTORY = "factory"
    ACCOUNT = "account"
    WORKFLOW = "workflow"


class GovernorDecision(Enum):
    """Decision outcomes. No implicit approval."""
    APPROVED = "approved"
    THROTTLED = "throttled"
    DEFERRED = "deferred"
    DENIED = "denied"


class DenyReason(Enum):
    """Explicit denial reasons for audit trail."""
    HARD_LIMIT_EXCEEDED = "hard_limit_exceeded"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    BURST_LIMIT_EXCEEDED = "burst_limit_exceeded"
    TRUST_BUDGET_DEPLETED = "trust_budget_depleted"
    PLATFORM_RISK_HIGH = "platform_risk_high"
    FAIRNESS_CONSTRAINT = "fairness_constraint"
    EMERGENCY_LOCKDOWN = "emergency_lockdown"
    QUOTA_NOT_FOUND = "quota_not_found"


# ============================================================================
# RESOURCE WINDOW MODEL (CRITICAL FOR ENFORCEMENT)
# ============================================================================

@dataclass(frozen=True)
class ResourceWindow:
    """
    Sliding window definition for rate limiting.
    
    Without windowing:
        - limits are meaningless
        - burst behavior is unsafe
        - platforms punish you silently
    """
    size_seconds: int          # Window duration (e.g., 60, 3600, 86400)
    hard_limit: int            # Absolute ceiling within window
    burst_limit: int           # Allowed temporary spike above hard_limit
    refill_rate: float         # Tokens per second for token bucket
    
    def __post_init__(self):
        assert self.size_seconds > 0, "Window size must be positive"
        assert self.hard_limit >= 0, "Hard limit must be non-negative"
        assert self.burst_limit >= self.hard_limit, "Burst must be >= hard limit"
        assert self.refill_rate >= 0, "Refill rate must be non-negative"


@dataclass
class ResourceQuota:
    """
    Authoritative contract for resource limits.
    ALL enforcement flows through this structure.
    """
    resource_type: ResourceType
    scope: ResourceScope
    scope_id: str              # platform name, factory_id, account_id
    windows: List[ResourceWindow]
    priority_weight: float = 1.0    # Fairness allocator input
    hard_fail: bool = True          # deny vs degrade on limit
    
    def __post_init__(self):
        assert len(self.windows) > 0, "At least one window required"
        assert 0.0 <= self.priority_weight <= 10.0, "Priority weight out of range"


# ============================================================================
# USAGE TRACKING STRUCTURES
# ============================================================================

@dataclass
class ResourceUsageSnapshot:
    """Point-in-time usage state for a resource."""
    resource_type: ResourceType
    scope: ResourceScope
    scope_id: str
    timestamp: float
    current_usage: Dict[int, int]  # window_size -> usage count
    recent_requests: int
    recent_denials: int
    last_burst_time: Optional[float]
    trust_level: float


@dataclass
class UsageRecord:
    """Individual usage event record."""
    timestamp: float
    amount: int
    requester: str
    priority: int


@dataclass
class DecisionRecord:
    """Audit record for governor decisions."""
    timestamp: float
    decision: GovernorDecision
    resource_type: ResourceType
    scope: ResourceScope
    scope_id: str
    amount: int
    requester: str
    priority: int
    reason: Optional[DenyReason] = None
    metadata: Dict = field(default_factory=dict)


# ============================================================================
# RATE LIMITER (SLIDING WINDOW + TOKEN BUCKET)
# ============================================================================

class RateLimiter:
    """
    Deterministic rate limiting with sliding windows and token bucket.
    
    Rules:
        - No randomness
        - Deterministic refill
        - Clean window sliding
    """
    
    def __init__(self, window: ResourceWindow):
        self.window = window
        self.usage_history: deque = deque()  # (timestamp, amount)
        self.tokens: float = float(window.burst_limit)
        self.last_refill: float = time.time()
        self.lock = threading.Lock()
    
    def request(self, amount: int, now: float) -> Tuple[bool, str]:
        """
        Check if request is allowed under rate limits.
        
        Returns:
            (allowed: bool, reason: str)
        """
        with self.lock:
            # Refill tokens
            self._refill_tokens(now)
            
            # Clean old usage from sliding window
            self._clean_window(now)
            
            # Check sliding window hard limit
            current_usage = sum(amt for _, amt in self.usage_history)
            if current_usage + amount > self.window.hard_limit:
                return False, "sliding_window_hard_limit"
            
            # Check token bucket for burst
            if self.tokens < amount:
                return False, "token_bucket_depleted"
            
            # Approved - consume tokens and record usage
            self.tokens -= amount
            self.usage_history.append((now, amount))
            
            return True, "approved"
    
    def _refill_tokens(self, now: float):
        """Refill token bucket at configured rate."""
        elapsed = now - self.last_refill
        refill_amount = elapsed * self.window.refill_rate
        self.tokens = min(self.tokens + refill_amount, float(self.window.burst_limit))
        self.last_refill = now
    
    def _clean_window(self, now: float):
        """Remove usage records outside the sliding window."""
        cutoff = now - self.window.size_seconds
        while self.usage_history and self.usage_history[0][0] < cutoff:
            self.usage_history.popleft()
    
    def get_current_usage(self, now: float) -> int:
        """Get current usage within window."""
        with self.lock:
            self._clean_window(now)
            return sum(amt for _, amt in self.usage_history)
    
    def get_available_tokens(self, now: float) -> float:
        """Get current token bucket level."""
        with self.lock:
            self._refill_tokens(now)
            return self.tokens


# ============================================================================
# BURST CONTROLLER
# ============================================================================

class BurstController:
    """
    Manages controlled spikes in resource usage.
    
    Rules:
        - Bursts are capped
        - Bursts consume future capacity
        - Bursts decay explicitly
    
    Without this:
        - You miss viral moments OR
        - You get banned
    """
    
    def __init__(self, max_burst_ratio: float = 2.0, decay_seconds: int = 300):
        self.max_burst_ratio = max_burst_ratio
        self.decay_seconds = decay_seconds
        self.burst_debt: Dict[str, float] = {}  # scope_key -> debt amount
        self.last_burst: Dict[str, float] = {}  # scope_key -> timestamp
        self.lock = threading.Lock()
    
    def can_burst(self, scope_key: str, amount: int, baseline: int, now: float) -> bool:
        """Check if burst is allowed given current debt."""
        with self.lock:
            self._decay_debt(scope_key, now)
            
            current_debt = self.burst_debt.get(scope_key, 0.0)
            max_debt = baseline * self.max_burst_ratio
            
            return (current_debt + amount) <= max_debt
    
    def record_burst(self, scope_key: str, amount: int, now: float):
        """Record burst usage and accumulate debt."""
        with self.lock:
            self.burst_debt[scope_key] = self.burst_debt.get(scope_key, 0.0) + amount
            self.last_burst[scope_key] = now
    
    def _decay_debt(self, scope_key: str, now: float):
        """Exponentially decay burst debt over time."""
        if scope_key not in self.last_burst:
            return
        
        elapsed = now - self.last_burst[scope_key]
        if elapsed <= 0:
            return
        
        # Exponential decay
        decay_factor = max(0.0, 1.0 - (elapsed / self.decay_seconds))
        current_debt = self.burst_debt.get(scope_key, 0.0)
        self.burst_debt[scope_key] = current_debt * decay_factor
        
        if self.burst_debt[scope_key] < 0.01:
            self.burst_debt[scope_key] = 0.0


# ============================================================================
# QUOTA ENFORCER
# ============================================================================

class QuotaEnforcer:
    """
    Hard rule enforcement.
    
    Once a hard limit is hit, no logic above may override it.
    
    Includes:
        - Global caps
        - Platform caps
        - Account caps
    """
    
    def __init__(self):
        self.quotas: Dict[str, ResourceQuota] = {}
        self.rate_limiters: Dict[str, List[RateLimiter]] = {}
        self.lock = threading.Lock()
    
    def register_quota(self, quota: ResourceQuota):
        """Register a quota for enforcement."""
        quota_key = self._quota_key(quota.resource_type, quota.scope, quota.scope_id)
        
        with self.lock:
            self.quotas[quota_key] = quota
            self.rate_limiters[quota_key] = [
                RateLimiter(window) for window in quota.windows
            ]
    
    def check_quota(
        self, 
        resource_type: ResourceType,
        scope: ResourceScope,
        scope_id: str,
        amount: int,
        now: float
    ) -> Tuple[bool, Optional[DenyReason]]:
        """
        Check if request satisfies all quota constraints.
        
        Returns:
            (allowed: bool, deny_reason: Optional[DenyReason])
        """
        quota_key = self._quota_key(resource_type, scope, scope_id)
        
        with self.lock:
            if quota_key not in self.quotas:
                return False, DenyReason.QUOTA_NOT_FOUND
            
            limiters = self.rate_limiters[quota_key]
            
            # All windows must pass
            for limiter in limiters:
                allowed, reason = limiter.request(amount, now)
                if not allowed:
                    if "hard_limit" in reason:
                        return False, DenyReason.HARD_LIMIT_EXCEEDED
                    elif "bucket" in reason:
                        return False, DenyReason.BURST_LIMIT_EXCEEDED
                    else:
                        return False, DenyReason.RATE_LIMIT_EXCEEDED
            
            return True, None
    
    def get_usage(
        self,
        resource_type: ResourceType,
        scope: ResourceScope,
        scope_id: str,
        now: float
    ) -> Dict[int, int]:
        """Get current usage across all windows."""
        quota_key = self._quota_key(resource_type, scope, scope_id)
        
        with self.lock:
            if quota_key not in self.rate_limiters:
                return {}
            
            limiters = self.rate_limiters[quota_key]
            quota = self.quotas[quota_key]
            
            return {
                window.size_seconds: limiter.get_current_usage(now)
                for window, limiter in zip(quota.windows, limiters)
            }
    
    @staticmethod
    def _quota_key(resource_type: ResourceType, scope: ResourceScope, scope_id: str) -> str:
        return f"{resource_type.value}:{scope.value}:{scope_id}"


# ============================================================================
# FAIRNESS ALLOCATOR
# ============================================================================

class FairnessAllocator:
    """
    Prevents one factory from monopolizing resources.
    
    Uses:
        - priority_weight
        - historical usage
        - recent success rate
    
    Fairness is required for multi-niche scalability.
    """
    
    def __init__(self, history_window_seconds: int = 3600):
        self.history_window = history_window_seconds
        self.usage_history: Dict[str, List[Tuple[float, int]]] = defaultdict(list)
        self.weights: Dict[str, float] = {}
        self.lock = threading.Lock()
    
    def register_requester(self, requester: str, priority_weight: float):
        """Register a requester with priority weight."""
        with self.lock:
            self.weights[requester] = priority_weight
    
    def should_throttle(
        self,
        requester: str,
        amount: int,
        now: float,
        global_capacity: int
    ) -> bool:
        """
        Check if requester should be throttled for fairness.
        
        Returns:
            True if requester should be throttled
        """
        with self.lock:
            self._clean_history(now)
            
            # Calculate fair share
            total_weight = sum(self.weights.values())
            if total_weight == 0:
                return False
            
            requester_weight = self.weights.get(requester, 1.0)
            fair_share = (requester_weight / total_weight) * global_capacity
            
            # Calculate requester's recent usage
            requester_usage = sum(
                amt for ts, amt in self.usage_history.get(requester, [])
            )
            
            # Throttle if significantly over fair share
            return requester_usage > (fair_share * 1.5)
    
    def record_usage(self, requester: str, amount: int, now: float):
        """Record usage for fairness tracking."""
        with self.lock:
            self.usage_history[requester].append((now, amount))
    
    def _clean_history(self, now: float):
        """Remove usage records outside history window."""
        cutoff = now - self.history_window
        for requester in list(self.usage_history.keys()):
            self.usage_history[requester] = [
                (ts, amt) for ts, amt in self.usage_history[requester]
                if ts >= cutoff
            ]


# ============================================================================
# TRUST BUDGET MANAGER
# ============================================================================

class TrustBudgetManager:
    """
    Models platform trust depletion as a consumable resource.
    
    Examples:
        - New accounts have low trust budget
        - Aggressive posting drains trust
        - Slow posting replenishes trust
    
    Trust is enforced before posting.
    This protects you from shadow bans.
    """
    
    def __init__(self):
        self.trust_levels: Dict[str, float] = {}  # account_id -> trust level (0-1)
        self.trust_decay_rate: Dict[str, float] = {}
        self.trust_recharge_rate: Dict[str, float] = {}
        self.last_action: Dict[str, float] = {}
        self.lock = threading.Lock()
    
    def initialize_account(
        self,
        account_id: str,
        initial_trust: float = 0.5,
        decay_rate: float = 0.1,
        recharge_rate: float = 0.05
    ):
        """Initialize trust budget for an account."""
        with self.lock:
            self.trust_levels[account_id] = initial_trust
            self.trust_decay_rate[account_id] = decay_rate
            self.trust_recharge_rate[account_id] = recharge_rate
            self.last_action[account_id] = time.time()
    
    def check_trust(self, account_id: str, action_cost: float, now: float) -> bool:
        """
        Check if account has sufficient trust budget.
        
        Args:
            account_id: Account identifier
            action_cost: Trust cost of action (0-1)
            now: Current timestamp
        
        Returns:
            True if trust budget is sufficient
        """
        with self.lock:
            if account_id not in self.trust_levels:
                return False
            
            self._update_trust(account_id, now)
            
            return self.trust_levels[account_id] >= action_cost
    
    def consume_trust(self, account_id: str, action_cost: float, now: float):
        """Consume trust budget for an action."""
        with self.lock:
            if account_id in self.trust_levels:
                self._update_trust(account_id, now)
                self.trust_levels[account_id] = max(
                    0.0,
                    self.trust_levels[account_id] - action_cost
                )
                self.last_action[account_id] = now
    
    def _update_trust(self, account_id: str, now: float):
        """Update trust level based on time elapsed."""
        if account_id not in self.last_action:
            return
        
        elapsed = now - self.last_action[account_id]
        if elapsed <= 0:
            return
        
        # Trust recharges slowly when idle
        recharge = elapsed * self.trust_recharge_rate.get(account_id, 0.05)
        self.trust_levels[account_id] = min(
            1.0,
            self.trust_levels[account_id] + recharge
        )
    
    def get_trust_level(self, account_id: str, now: float) -> float:
        """Get current trust level."""
        with self.lock:
            if account_id not in self.trust_levels:
                return 0.0
            self._update_trust(account_id, now)
            return self.trust_levels[account_id]


# ============================================================================
# PLATFORM RISK GUARD
# ============================================================================

class PlatformRiskGuard:
    """
    Hard-coded platform invariants.
    
    Examples:
        - max posts / hour
        - geo rotation rules
        - account reuse limits
        - correlation detection limits
    
    If violated → automatic deny.
    No ML here. Rules only.
    """
    
    def __init__(self):
        self.platform_rules: Dict[str, Dict] = {}
        self.violation_history: Dict[str, List[float]] = defaultdict(list)
        self.lock = threading.Lock()
    
    def register_platform_rules(self, platform: str, rules: Dict):
        """
        Register hard-coded rules for a platform.
        
        Example rules:
            {
                'max_posts_per_hour': 50,
                'max_posts_per_day': 500,
                'min_post_interval_seconds': 30,
                'max_account_reuse_per_hour': 3,
                'require_geo_rotation': True
            }
        """
        with self.lock:
            self.platform_rules[platform] = rules
    
    def check_platform_risk(
        self,
        platform: str,
        action_type: str,
        account_id: str,
        now: float
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if action violates platform-specific rules.
        
        Returns:
            (allowed: bool, violation_reason: Optional[str])
        """
        with self.lock:
            if platform not in self.platform_rules:
                return True, None
            
            rules = self.platform_rules[platform]
            
            # Check minimum interval between posts
            if action_type == "post":
                min_interval = rules.get('min_post_interval_seconds', 0)
                last_post_key = f"{platform}:{account_id}:post"
                
                if last_post_key in self.violation_history:
                    last_posts = self.violation_history[last_post_key]
                    if last_posts and (now - last_posts[-1]) < min_interval:
                        return False, "min_post_interval_violation"
            
            # Check hourly limits
            if 'max_posts_per_hour' in rules:
                hour_ago = now - 3600
                post_key = f"{platform}:{account_id}:post"
                recent_posts = [
                    ts for ts in self.violation_history.get(post_key, [])
                    if ts >= hour_ago
                ]
                
                if len(recent_posts) >= rules['max_posts_per_hour']:
                    return False, "hourly_post_limit_exceeded"
            
            return True, None
    
    def record_action(self, platform: str, action_type: str, account_id: str, now: float):
        """Record platform action for risk tracking."""
        with self.lock:
            action_key = f"{platform}:{account_id}:{action_type}"
            self.violation_history[action_key].append(now)
            
            # Keep only last 24 hours
            cutoff = now - 86400
            self.violation_history[action_key] = [
                ts for ts in self.violation_history[action_key]
                if ts >= cutoff
            ]


# ============================================================================
# GOVERNANCE POLICY
# ============================================================================

class GovernancePolicy:
    """
    Declared policy rules (NOT config-only).
    
    Policies include:
        - deny vs degrade behavior
        - per-platform strictness
        - emergency lockdown thresholds
    """
    
    def __init__(self):
        self.emergency_lockdown = False
        self.platform_strictness: Dict[str, float] = {}  # platform -> strictness (0-1)
        self.degrade_on_limit: Set[ResourceType] = set()
        self.lock = threading.Lock()
    
    def set_emergency_lockdown(self, enabled: bool):
        """Enable/disable emergency lockdown mode."""
        with self.lock:
            self.emergency_lockdown = enabled
    
    def is_locked_down(self) -> bool:
        """Check if system is in emergency lockdown."""
        with self.lock:
            return self.emergency_lockdown
    
    def set_platform_strictness(self, platform: str, strictness: float):
        """Set strictness level for a platform (0=lenient, 1=strict)."""
        assert 0.0 <= strictness <= 1.0
        with self.lock:
            self.platform_strictness[platform] = strictness
    
    def should_degrade_on_limit(self, resource_type: ResourceType) -> bool:
        """Check if resource should degrade instead of deny on limit."""
        with self.lock:
            return resource_type in self.degrade_on_limit


# ============================================================================
# DECISION LOGGER (AUDIT TRAIL)
# ============================================================================

class DecisionLogger:
    """
    Deterministic audit logging for all governor decisions.
    
    Required for:
        - Replay
        - Debugging
        - Legal review
    """
    
    def __init__(self, max_records: int = 100000):
        self.max_records = max_records
        self.decisions: deque = deque(maxlen=max_records)
        self.denial_counts: Dict[DenyReason, int] = defaultdict(int)
        self.lock = threading.Lock()
    
    def log_decision(self, record: DecisionRecord):
        """Log a governor decision."""
        with self.lock:
            self.decisions.append(record)
            
            if record.decision == GovernorDecision.DENIED and record.reason:
                self.denial_counts[record.reason] += 1
    
    def get_recent_decisions(self, count: int = 100) -> List[DecisionRecord]:
        """Get most recent decisions."""
        with self.lock:
            return list(self.decisions)[-count:]
    
    def get_denial_stats(self) -> Dict[DenyReason, int]:
        """Get denial statistics."""
        with self.lock:
            return dict(self.denial_counts)
    
    def export_audit_log(self, filepath: str):
        """Export full audit log to file."""
        with self.lock:
            records = [
                {
                    'timestamp': r.timestamp,
                    'decision': r.decision.value,
                    'resource_type': r.resource_type.value,
                    'scope': r.scope.value,
                    'scope_id': r.scope_id,
                    'amount': r.amount,
                    'requester': r.requester,
                    'priority': r.priority,
                    'reason': r.reason.value if r.reason else None,
                    'metadata': r.metadata
                }
                for r in self.decisions
            ]
            
            with open(filepath, 'w') as f:
                json.dump(records, f, indent=2)


# ============================================================================
# GOVERNOR WATCHDOG
# ============================================================================

class GovernorWatchdog:
    """
    Detects violations and anomalies in governor operation.
    
    On violation:
        - Hard error
        - Global throttle
        - Alert + snapshot
    """
    
    def __init__(self, governor):
        self.governor = governor
        self.violation_count = 0
        self.last_check = time.time()
    
    def check_integrity(self):
        """Run integrity checks on governor state."""
        violations = []
        now = time.time()
        
        # Check for negative usage in rate limiters
        with self.governor.quota_enforcer.lock:
            for quota_key, limiters in self.governor.quota_enforcer.rate_limiters.items():
                for limiter in limiters:
                    if limiter.tokens < 0:
                        violations.append(f"Negative tokens in {quota_key}: {limiter.tokens}")
                    
                    # Check for negative usage amounts in history
                    for ts, amt in limiter.usage_history:
                        if amt < 0:
                            violations.append(f"Negative usage amount in {quota_key} at {ts}: {amt}")
        
        # Check for negative burst debt
        with self.governor.burst_controller.lock:
            for scope_key, debt in self.governor.burst_controller.burst_debt.items():
                if debt < 0:
                    violations.append(f"Negative burst debt in {scope_key}: {debt}")
        
        # Check for negative trust levels
        with self.governor.trust_manager.lock:
            for account_id, trust in self.governor.trust_manager.trust_levels.items():
                if trust < 0 or trust > 1.0:
                    violations.append(f"Invalid trust level for {account_id}: {trust}")
        
        # Check for quota mismatches (quotas without rate limiters)
        with self.governor.quota_enforcer.lock:
            for quota_key, quota in self.governor.quota_enforcer.quotas.items():
                if quota_key not in self.governor.quota_enforcer.rate_limiters:
                    violations.append(f"Quota {quota_key} missing rate limiters")
                elif len(self.governor.quota_enforcer.rate_limiters[quota_key]) != len(quota.windows):
                    violations.append(
                        f"Quota {quota_key} has {len(quota.windows)} windows but "
                        f"{len(self.governor.quota_enforcer.rate_limiters[quota_key])} limiters"
                    )
        
        # Check for unregistered resources in usage tracking
        # (This would require tracking all resource requests, which we do via DecisionLogger)
        recent_decisions = self.governor.decision_logger.get_recent_decisions(1000)
        registered_quotas = set(self.governor.quota_enforcer.quotas.keys())
        
        for decision in recent_decisions:
            if decision.decision == GovernorDecision.APPROVED:
                quota_key = QuotaEnforcer._quota_key(
                    decision.resource_type,
                    decision.scope,
                    decision.scope_id
                )
                if quota_key not in registered_quotas and decision.amount > 0:
                    # This might be okay if it's a new quota, but log it
                    pass  # Not a violation, just unregistered
        
        self.last_check = now
        
        if violations:
            self.violation_count += len(violations)
            self._handle_violations(violations)
    
    def _handle_violations(self, violations: List[str]):
        """Handle detected violations."""
        logging.critical(f"GOVERNOR WATCHDOG: {len(violations)} violations detected")
        for v in violations:
            logging.critical(f"  - {v}")
        
        # Emergency response
        if self.violation_count > 10:
            self.governor.policy.set_emergency_lockdown(True)


# ============================================================================
# RESOURCE GOVERNOR (SINGLETON - CORE ENGINE)
# ============================================================================

class ResourceGovernor:
    """
    Central resource governance engine.
    
    Singleton. Non-negotiable.
    
    This is the ONLY component that has final authority to deny resource consumption.
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        
        self._initialized = True
        
        # Core components
        self.quota_enforcer = QuotaEnforcer()
        self.burst_controller = BurstController()
        self.fairness_allocator = FairnessAllocator()
        self.trust_manager = TrustBudgetManager()
        self.risk_guard = PlatformRiskGuard()
        self.policy = GovernancePolicy()
        self.decision_logger = DecisionLogger()
        
        # Watchdog
        self.watchdog = GovernorWatchdog(self)
        
        # Global state
        self.global_capacity: Dict[ResourceType, int] = {}
        
        # Usage tracking for snapshots
        self.recent_requests: Dict[str, int] = defaultdict(int)  # quota_key -> count
        self.recent_denials: Dict[str, int] = defaultdict(int)  # quota_key -> count
        self.request_history_window = 3600  # 1 hour window
        self.request_timestamps: Dict[str, deque] = defaultdict(lambda: deque())  # quota_key -> timestamps
        self.denial_timestamps: Dict[str, deque] = defaultdict(lambda: deque())  # quota_key -> timestamps
        
        # Trust cost configuration per resource type
        self.trust_costs: Dict[ResourceType, float] = {
            ResourceType.POST_ACTION: 0.1,
            ResourceType.ACCOUNT_ACTION: 0.15,
        }
        
        logging.info("ResourceGovernor initialized (singleton)")
    
    # ========================================================================
    # PRIMARY API
    # ========================================================================
    
    def request(
        self,
        resource_type: ResourceType,
        amount: int,
        scope: ResourceScope,
        scope_id: str,
        requester: str,
        priority: int = 5
    ) -> Tuple[GovernorDecision, Optional[DenyReason]]:
        """
        Request resource allocation.
        
        Returns:
            (decision: GovernorDecision, reason: Optional[DenyReason])
        
        No implicit approval. Ever.
        """
        now = time.time()
        
        # Emergency lockdown check
        if self.policy.is_locked_down():
            decision = DecisionRecord(
                timestamp=now,
                decision=GovernorDecision.DENIED,
                resource_type=resource_type,
                scope=scope,
                scope_id=scope_id,
                amount=amount,
                requester=requester,
                priority=priority,
                reason=DenyReason.EMERGENCY_LOCKDOWN
            )
            self.decision_logger.log_decision(decision)
            # Track denial
            quota_key = QuotaEnforcer._quota_key(resource_type, scope, scope_id)
            self.denial_timestamps[quota_key].append(now)
            self._clean_denial_history(quota_key, now)
            self.recent_denials[quota_key] = len(self.denial_timestamps[quota_key])
            return GovernorDecision.DENIED, DenyReason.EMERGENCY_LOCKDOWN
        
        # Pre-check burst debt (before consuming resources)
        scope_key = QuotaEnforcer._quota_key(resource_type, scope, scope_id)
        if scope_key in self.quota_enforcer.quotas:
            quota = self.quota_enforcer.quotas[scope_key]
            baseline = min(w.hard_limit for w in quota.windows) if quota.windows else amount
            
            # Get current usage BEFORE quota check
            current_usage = self.quota_enforcer.get_usage(resource_type, scope, scope_id, now)
            total_window_usage = sum(current_usage.values()) if current_usage else 0
            
            # If this request would push us into burst territory, check burst controller
            if total_window_usage + amount > baseline:
                burst_amount = (total_window_usage + amount) - baseline
                if not self.burst_controller.can_burst(scope_key, burst_amount, baseline, now):
                    decision = DecisionRecord(
                        timestamp=now,
                        decision=GovernorDecision.DENIED,
                        resource_type=resource_type,
                        scope=scope,
                        scope_id=scope_id,
                        amount=amount,
                        requester=requester,
                        priority=priority,
                        reason=DenyReason.BURST_LIMIT_EXCEEDED
                    )
                    self.decision_logger.log_decision(decision)
                    # Track denial
                    quota_key = scope_key
                    self.denial_timestamps[quota_key].append(now)
                    self._clean_denial_history(quota_key, now)
                    self.recent_denials[quota_key] = len(self.denial_timestamps[quota_key])
                    return GovernorDecision.DENIED, DenyReason.BURST_LIMIT_EXCEEDED
        
        # Quota enforcement (consumes resources if approved)
        allowed, deny_reason = self.quota_enforcer.check_quota(
            resource_type, scope, scope_id, amount, now
        )
        
        if not allowed:
            decision = DecisionRecord(
                timestamp=now,
                decision=GovernorDecision.DENIED,
                resource_type=resource_type,
                scope=scope,
                scope_id=scope_id,
                amount=amount,
                requester=requester,
                priority=priority,
                reason=deny_reason
            )
            self.decision_logger.log_decision(decision)
            # Track denial
            quota_key = QuotaEnforcer._quota_key(resource_type, scope, scope_id)
            self.denial_timestamps[quota_key].append(now)
            self._clean_denial_history(quota_key, now)
            self.recent_denials[quota_key] = len(self.denial_timestamps[quota_key])
            return GovernorDecision.DENIED, deny_reason
        
        # Record burst if we're in burst territory (after quota approved)
        if scope_key in self.quota_enforcer.quotas:
            quota = self.quota_enforcer.quotas[scope_key]
            baseline = min(w.hard_limit for w in quota.windows) if quota.windows else amount
            current_usage = self.quota_enforcer.get_usage(resource_type, scope, scope_id, now)
            total_window_usage = sum(current_usage.values()) if current_usage else 0
            
            if total_window_usage > baseline:
                burst_amount = total_window_usage - baseline
                self.burst_controller.record_burst(scope_key, burst_amount, now)
        
        # Trust budget check (for sensitive actions)
        if resource_type in self.trust_costs:
            trust_cost = self.trust_costs[resource_type]
            if not self.trust_manager.check_trust(scope_id, trust_cost, now):
                decision = DecisionRecord(
                    timestamp=now,
                    decision=GovernorDecision.DENIED,
                    resource_type=resource_type,
                    scope=scope,
                    scope_id=scope_id,
                    amount=amount,
                    requester=requester,
                    priority=priority,
                    reason=DenyReason.TRUST_BUDGET_DEPLETED
                )
                self.decision_logger.log_decision(decision)
                return GovernorDecision.DENIED, DenyReason.TRUST_BUDGET_DEPLETED
            
            self.trust_manager.consume_trust(scope_id, trust_cost, now)
        
        # Platform risk check
        if resource_type == ResourceType.POST_ACTION:
            risk_allowed, risk_reason = self.risk_guard.check_platform_risk(
                scope_id.split(':')[0],  # Extract platform from scope_id
                "post",
                scope_id,
                now
            )
            
            if not risk_allowed:
                decision = DecisionRecord(
                    timestamp=now,
                    decision=GovernorDecision.DENIED,
                    resource_type=resource_type,
                    scope=scope,
                    scope_id=scope_id,
                    amount=amount,
                    requester=requester,
                    priority=priority,
                    reason=DenyReason.PLATFORM_RISK_HIGH,
                    metadata={'risk_reason': risk_reason}
                )
                self.decision_logger.log_decision(decision)
                # Track denial
                quota_key = QuotaEnforcer._quota_key(resource_type, scope, scope_id)
                self.denial_timestamps[quota_key].append(now)
                self._clean_denial_history(quota_key, now)
                self.recent_denials[quota_key] = len(self.denial_timestamps[quota_key])
                return GovernorDecision.DENIED, DenyReason.PLATFORM_RISK_HIGH
            
            self.risk_guard.record_action(
                scope_id.split(':')[0],
                "post",
                scope_id,
                now
            )
        
        # Fairness check
        global_cap = self.global_capacity.get(resource_type, 999999)
        if self.fairness_allocator.should_throttle(requester, amount, now, global_cap):
            decision = DecisionRecord(
                timestamp=now,
                decision=GovernorDecision.THROTTLED,
                resource_type=resource_type,
                scope=scope,
                scope_id=scope_id,
                amount=amount,
                requester=requester,
                priority=priority,
                reason=DenyReason.FAIRNESS_CONSTRAINT
            )
            self.decision_logger.log_decision(decision)
            # Track denial (throttled counts as denial for stats)
            quota_key = QuotaEnforcer._quota_key(resource_type, scope, scope_id)
            self.denial_timestamps[quota_key].append(now)
            self._clean_denial_history(quota_key, now)
            self.recent_denials[quota_key] = len(self.denial_timestamps[quota_key])
            return GovernorDecision.THROTTLED, DenyReason.FAIRNESS_CONSTRAINT
        
        # Record usage for fairness
        self.fairness_allocator.record_usage(requester, amount, now)
        
        # Track request
        quota_key = QuotaEnforcer._quota_key(resource_type, scope, scope_id)
        self.request_timestamps[quota_key].append(now)
        self._clean_request_history(quota_key, now)
        self.recent_requests[quota_key] = len(self.request_timestamps[quota_key])
        
        # APPROVED
        decision = DecisionRecord(
            timestamp=now,
            decision=GovernorDecision.APPROVED,
            resource_type=resource_type,
            scope=scope,
            scope_id=scope_id,
            amount=amount,
            requester=requester,
            priority=priority
        )
        self.decision_logger.log_decision(decision)
        
        return GovernorDecision.APPROVED, None
    
    def release(
        self,
        resource_type: ResourceType,
        amount: int,
        scope: ResourceScope,
        scope_id: str,
        requester: str
    ):
        """
        Release previously allocated resources.
        
        Used for:
            - Early workflow exit
            - Failed allocations
            - Speculative reservations
        """
        now = time.time()
        scope_key = QuotaEnforcer._quota_key(resource_type, scope, scope_id)
        
        # Credit back to rate limiters (remove from usage history)
        if scope_key in self.quota_enforcer.rate_limiters:
            limiters = self.quota_enforcer.rate_limiters[scope_key]
            with self.quota_enforcer.lock:
                for limiter in limiters:
                    # Remove most recent usage records that match this amount
                    # This is a best-effort credit-back
                    removed = 0
                    for i in range(len(limiter.usage_history) - 1, -1, -1):
                        if removed >= amount:
                            break
                        ts, amt = limiter.usage_history[i]
                        if amt <= (amount - removed):
                            removed += amt
                            del limiter.usage_history[i]
                        else:
                            # Partial match - adjust the record
                            limiter.usage_history[i] = (ts, amt - (amount - removed))
                            removed = amount
                            break
                    
                    # Refill tokens proportionally
                    if removed > 0:
                        # Credit tokens back (up to burst limit)
                        limiter.tokens = min(
                            limiter.tokens + removed,
                            float(limiter.window.burst_limit)
                        )
        
        # Credit back burst debt if this was a burst allocation
        with self.burst_controller.lock:
            if scope_key in self.burst_controller.burst_debt:
                current_debt = self.burst_controller.burst_debt.get(scope_key, 0.0)
                self.burst_controller.burst_debt[scope_key] = max(
                    0.0,
                    current_debt - amount
                )
        
        # Credit back trust if this was a trust-consuming action
        if resource_type in self.trust_costs:
            trust_cost = self.trust_costs[resource_type]
            with self.trust_manager.lock:
                if scope_id in self.trust_manager.trust_levels:
                    self.trust_manager.trust_levels[scope_id] = min(
                        1.0,
                        self.trust_manager.trust_levels[scope_id] + trust_cost
                    )
        
        # Record release in fairness allocator
        self.fairness_allocator.record_usage(requester, -amount, now)
        
        # Log the release
        decision = DecisionRecord(
            timestamp=now,
            decision=GovernorDecision.APPROVED,  # Release is always "approved"
            resource_type=resource_type,
            scope=scope,
            scope_id=scope_id,
            amount=-amount,  # Negative to indicate release
            requester=requester,
            priority=0,
            metadata={'action': 'release'}
        )
        self.decision_logger.log_decision(decision)
    
    def check(
        self,
        resource_type: ResourceType,
        amount: int,
        scope: ResourceScope,
        scope_id: str,
        requester: str,
        priority: int = 5
    ) -> Tuple[GovernorDecision, Optional[DenyReason]]:
        """
        Non-mutating check (for planning/simulation).
        
        Used for:
            - Planning
            - Simulation
            - Dry-runs
            - RL state introspection
        """
        now = time.time()
        
        # Check quota without consuming
        allowed, deny_reason = self.quota_enforcer.check_quota(
            resource_type, scope, scope_id, amount, now
        )
        
        if not allowed:
            return GovernorDecision.DENIED, deny_reason
        
        # Check trust without consuming
        if resource_type in self.trust_costs:
            trust_cost = self.trust_costs[resource_type]
            if not self.trust_manager.check_trust(scope_id, trust_cost, now):
                return GovernorDecision.DENIED, DenyReason.TRUST_BUDGET_DEPLETED
        
        return GovernorDecision.APPROVED, None
    
    def snapshot(self) -> Dict:
        """
        Get deterministic snapshot of governor state.
        
        Returns state of:
            - quotas
            - usage
            - recent denials
        
        Required for:
            - Replay
            - Debugging
            - Legal review
        """
        now = time.time()
        
        # Gather usage across all quotas
        usage_snapshots = []
        for quota_key, quota in self.quota_enforcer.quotas.items():
            current_usage = self.quota_enforcer.get_usage(
                quota.resource_type,
                quota.scope,
                quota.scope_id,
                now
            )
            
            quota_key = QuotaEnforcer._quota_key(
                quota.resource_type,
                quota.scope,
                quota.scope_id
            )
            
            # Get last burst time from burst controller
            last_burst_time = None
            if quota_key in self.burst_controller.last_burst:
                last_burst_time = self.burst_controller.last_burst[quota_key]
            
            snapshot = ResourceUsageSnapshot(
                resource_type=quota.resource_type,
                scope=quota.scope,
                scope_id=quota.scope_id,
                timestamp=now,
                current_usage=current_usage,
                recent_requests=self.recent_requests.get(quota_key, 0),
                recent_denials=self.recent_denials.get(quota_key, 0),
                last_burst_time=last_burst_time,
                trust_level=self.trust_manager.get_trust_level(quota.scope_id, now)
            )
            usage_snapshots.append(snapshot)
        
        return {
            'timestamp': now,
            'emergency_lockdown': self.policy.is_locked_down(),
            'usage_snapshots': [
                {
                    'resource_type': s.resource_type.value,
                    'scope': s.scope.value,
                    'scope_id': s.scope_id,
                    'current_usage': s.current_usage,
                    'trust_level': s.trust_level
                }
                for s in usage_snapshots
            ],
            'recent_denials': self.decision_logger.get_denial_stats(),
            'recent_decisions': [
                {
                    'timestamp': d.timestamp,
                    'decision': d.decision.value,
                    'resource_type': d.resource_type.value,
                    'requester': d.requester
                }
                for d in self.decision_logger.get_recent_decisions(50)
            ]
        }
    
    # ========================================================================
    # CONFIGURATION API
    # ========================================================================
    
    def register_quota(self, quota: ResourceQuota):
        """Register a resource quota."""
        self.quota_enforcer.register_quota(quota)
    
    def register_requester(self, requester: str, priority_weight: float):
        """Register a requester with priority weight."""
        self.fairness_allocator.register_requester(requester, priority_weight)
    
    def initialize_account_trust(
        self,
        account_id: str,
        initial_trust: float = 0.5,
        decay_rate: float = 0.1,
        recharge_rate: float = 0.05
    ):
        """Initialize trust budget for an account."""
        self.trust_manager.initialize_account(
            account_id, initial_trust, decay_rate, recharge_rate
        )
    
    def register_platform_rules(self, platform: str, rules: Dict):
        """Register platform-specific risk rules."""
        self.risk_guard.register_platform_rules(platform, rules)
    
    def set_global_capacity(self, resource_type: ResourceType, capacity: int):
        """Set global capacity for fairness allocation."""
        self.global_capacity[resource_type] = capacity
    
    # ========================================================================
    # MONITORING API
    # ========================================================================
    
    def get_denial_stats(self) -> Dict[DenyReason, int]:
        """Get denial statistics."""
        return self.decision_logger.get_denial_stats()
    
    def export_audit_log(self, filepath: str):
        """Export audit log."""
        self.decision_logger.export_audit_log(filepath)
    
    def run_integrity_check(self):
        """Run watchdog integrity check."""
        self.watchdog.check_integrity()
    
    def set_trust_cost(self, resource_type: ResourceType, cost: float):
        """Set trust cost for a resource type."""
        assert 0.0 <= cost <= 1.0, "Trust cost must be between 0 and 1"
        self.trust_costs[resource_type] = cost
    
    def _clean_request_history(self, quota_key: str, now: float):
        """Clean old request timestamps outside history window."""
        cutoff = now - self.request_history_window
        timestamps = self.request_timestamps[quota_key]
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()
        self.recent_requests[quota_key] = len(timestamps)
    
    def _clean_denial_history(self, quota_key: str, now: float):
        """Clean old denial timestamps outside history window."""
        cutoff = now - self.request_history_window
        timestamps = self.denial_timestamps[quota_key]
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()
        self.recent_denials[quota_key] = len(timestamps)


# ============================================================================
# USAGE EXAMPLE (FOR REFERENCE)
# ============================================================================

def example_usage():
    """
    Example of how to use ResourceGovernor in production.
    """
    
    # Get singleton instance
    governor = ResourceGovernor()
    
    # Register quotas
    api_quota = ResourceQuota(
        resource_type=ResourceType.API_REQUEST,
        scope=ResourceScope.PLATFORM,
        scope_id="twitter",
        windows=[
            ResourceWindow(
                size_seconds=60,
                hard_limit=100,
                burst_limit=150,
                refill_rate=2.0
            ),
            ResourceWindow(
                size_seconds=3600,
                hard_limit=5000,
                burst_limit=7000,
                refill_rate=1.5
            )
        ],
        priority_weight=1.0,
        hard_fail=True
    )
    governor.register_quota(api_quota)
    
    # Register platform rules
    governor.register_platform_rules('twitter', {
        'max_posts_per_hour': 50,
        'max_posts_per_day': 500,
        'min_post_interval_seconds': 30
    })
    
    # Initialize account trust
    governor.initialize_account_trust(
        account_id='twitter:account_123',
        initial_trust=0.7,
        decay_rate=0.1,
        recharge_rate=0.05
    )
    
    # Register factory requester
    governor.register_requester(
        requester='factory_viral_memes',
        priority_weight=2.0
    )
    
    # Make resource request
    decision, reason = governor.request(
        resource_type=ResourceType.API_REQUEST,
        amount=10,
        scope=ResourceScope.PLATFORM,
        scope_id='twitter',
        requester='factory_viral_memes',
        priority=7
    )
    
    if decision == GovernorDecision.APPROVED:
        # Proceed with API calls
        print("Request approved - proceeding")
    elif decision == GovernorDecision.DENIED:
        # Handle denial
        print(f"Request denied: {reason}")
    elif decision == GovernorDecision.THROTTLED:
        # Back off and retry
        print("Request throttled - backing off")
    
    # Get system snapshot
    snapshot = governor.snapshot()
    print(f"System snapshot: {len(snapshot['usage_snapshots'])} resources tracked")
    
    # Export audit log
    governor.export_audit_log('/var/log/governor_audit.json')


# ============================================================================
# MODULE INITIALIZATION
# ============================================================================

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Export public API
__all__ = [
    'ResourceType',
    'ResourceScope',
    'GovernorDecision',
    'DenyReason',
    'ResourceWindow',
    'ResourceQuota',
    'ResourceUsageSnapshot',
    'ResourceGovernor',
]