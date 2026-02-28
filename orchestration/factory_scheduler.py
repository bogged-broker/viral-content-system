"""
/orchestration/factory_scheduler.py
Deterministic Execution Admission & Throughput Controller

This file is where work actually gets admitted to run — but only after 
orchestration, priority, dependency, and resource constraints agree.

NO SLOPPY ADMISSION CONTROL.
NO PRIORITY INVERSION.
NO SILENT BOTTLENECKS.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, List, Set, Tuple, Callable
from enum import Enum
from collections import defaultdict
import time
import logging

# ============================================================================
# ENUMS & CONSTANTS
# ============================================================================

class PriorityClass(Enum):
    """Execution priority tiers (from priority_router.py)"""
    CRITICAL = 5   # Platform window, viral breakout
    HIGH = 4       # Confirmed viral momentum
    NORMAL = 3     # Standard valuable content
    LOW = 2        # Experimental, low-confidence
    DEFERRED = 1   # Backfill, batch

class ExecutionPhase(Enum):
    """Content creation pipeline phases"""
    GENERATION = "generation"
    VALIDATION = "validation"
    POSTING = "posting"
    MONITORING = "monitoring"

class ExecutionLane(Enum):
    """Throughput lanes mapped from priority"""
    FAST = "fast"
    NORMAL = "normal"
    SLOW = "slow"
    PARKED = "parked"

# Lane mapping
PRIORITY_TO_LANE = {
    PriorityClass.CRITICAL: ExecutionLane.FAST,
    PriorityClass.HIGH: ExecutionLane.FAST,
    PriorityClass.NORMAL: ExecutionLane.NORMAL,
    PriorityClass.LOW: ExecutionLane.SLOW,
    PriorityClass.DEFERRED: ExecutionLane.PARKED,
}

# Default capacity limits per lane
LANE_CONCURRENCY_CAPS = {
    ExecutionLane.FAST: 50,
    ExecutionLane.NORMAL: 100,
    ExecutionLane.SLOW: 30,
    ExecutionLane.PARKED: 10,
}

# Priority class weights for reserved capacity (per factory)
PRIORITY_CLASS_WEIGHTS = {
    PriorityClass.CRITICAL: 0.40,
    PriorityClass.HIGH: 0.30,
    PriorityClass.NORMAL: 0.20,
    PriorityClass.LOW: 0.08,
    PriorityClass.DEFERRED: 0.02,
}

# Per-phase global caps
PHASE_CONCURRENCY_CAPS = {
    ExecutionPhase.GENERATION: 200,
    ExecutionPhase.VALIDATION: 150,
    ExecutionPhase.POSTING: 100,
    ExecutionPhase.MONITORING: 50,
}

# Burst elasticity multipliers (temporary capacity expansion)
BURST_MULTIPLIERS = {
    ExecutionLane.FAST: 1.5,
    ExecutionLane.NORMAL: 1.3,
    ExecutionLane.SLOW: 1.0,
    ExecutionLane.PARKED: 1.0,
}

BURST_DURATION_SECONDS = 300  # 5 minutes max burst
SLOT_EXPIRY_SECONDS = 600     # 10 minutes max slot hold

# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass
class ExecutionSlot:
    """
    Explicit capacity unit.
    
    Slots are:
    - finite
    - traceable
    - revocable
    """
    slot_id: str
    factory_id: str
    phase: ExecutionPhase
    priority_class: PriorityClass
    lane: ExecutionLane
    platform_id: Optional[str]
    
    allocated_at: float
    expires_at: float
    
    task_id: str
    resource_cost: Dict[str, float] = field(default_factory=dict)
    
    def is_expired(self, now: Optional[float] = None) -> bool:
        """Check if slot has expired"""
        current = now if now is not None else time.time()
        return current > self.expires_at
    
    def __hash__(self):
        return hash(self.slot_id)


@dataclass(frozen=True)
class AdmissionDecision:
    """
    Immutable admission result.
    
    No partial states.
    No hidden retries.
    """
    admitted: bool
    slot: Optional[ExecutionSlot]
    intent: Optional["ExecutionIntent"]
    
    reason: str
    retry_after_seconds: Optional[int]
    
    # Audit trail
    timestamp: float = field(default_factory=time.time)
    priority_class: Optional[PriorityClass] = None
    factory_id: Optional[str] = None
    phase: Optional[ExecutionPhase] = None


@dataclass(frozen=True)
class ExecutionIntent:
    """
    Immutable execution intent (does not execute work).
    """
    intent_id: str
    task_id: str
    factory_id: str
    phase: ExecutionPhase
    priority_class: PriorityClass
    lane: ExecutionLane
    platform_id: Optional[str]
    created_at: float


@dataclass
class RoutingDecision:
    """Input from priority_router.py (simplified for this module)"""
    task_id: str
    factory_id: str
    priority_class: PriorityClass
    phase: ExecutionPhase
    dependencies_ready: bool
    platform_id: Optional[str] = None
    estimated_resource_cost: Dict[str, float] = field(default_factory=dict)
    burst_signal: bool = False
    burst_reason: Optional[str] = None
    is_retry: bool = False


@dataclass(frozen=True)
class PlatformCaps:
    """Hard caps per platform"""
    max_concurrent: int
    max_per_phase: Dict[ExecutionPhase, int] = field(default_factory=dict)


@dataclass
class ResourcePressure:
    """Input from resource_governor.py"""
    cpu_utilization: float
    memory_utilization: float
    api_quota_remaining: float
    platform_rate_limit_pressure: float
    
    def is_under_pressure(self) -> bool:
        """High pressure blocks new admissions"""
        return (
            self.cpu_utilization > 0.85 or
            self.memory_utilization > 0.85 or
            self.api_quota_remaining < 0.2 or
            self.platform_rate_limit_pressure > 0.8
        )


@dataclass
class BurstState:
    """Burst elasticity state"""
    active: bool
    started_at: float
    expires_at: float
    lane: ExecutionLane
    reason: str
    
    def is_active(self) -> bool:
        """Check if burst is still valid"""
        return self.active and time.time() < self.expires_at


# ============================================================================
# SCHEDULER STATE
# ============================================================================

class SchedulerState:
    """
    Central state for admission control.
    
    Tracks:
    - Active slots per factory/phase/lane
    - Burst windows
    - Inversion guards
    """
    
    def __init__(self, time_fn: Callable[[], float]):
        # slot_id -> ExecutionSlot
        self.active_slots: Dict[str, ExecutionSlot] = {}
        
        # Indexing for fast lookups
        self.slots_by_factory: Dict[str, Set[str]] = defaultdict(set)
        self.slots_by_phase: Dict[ExecutionPhase, Set[str]] = defaultdict(set)
        self.slots_by_lane: Dict[ExecutionLane, Set[str]] = defaultdict(set)
        self.slots_by_priority: Dict[PriorityClass, Set[str]] = defaultdict(set)
        self.slots_by_factory_lane: Dict[Tuple[str, ExecutionLane], Set[str]] = defaultdict(set)
        self.slots_by_factory_priority: Dict[Tuple[str, PriorityClass], Set[str]] = defaultdict(set)
        self.slots_by_platform: Dict[str, Set[str]] = defaultdict(set)
        self.slots_by_platform_phase: Dict[Tuple[str, ExecutionPhase], Set[str]] = defaultdict(set)
        
        # Burst tracking
        self.active_bursts: Dict[ExecutionLane, BurstState] = {}
        
        # Inversion guard: track recent rejections to prevent LOW from blocking HIGH
        self.recent_rejections: List[Tuple[float, PriorityClass, str]] = []
        self.recent_attempts: List[Tuple[float, PriorityClass, bool, bool, str]] = []

        # Deterministic sequencing
        self.next_slot_seq = 1
        self.time_fn = time_fn
        
    def add_slot(self, slot: ExecutionSlot):
        """Register new slot"""
        self.active_slots[slot.slot_id] = slot
        self.slots_by_factory[slot.factory_id].add(slot.slot_id)
        self.slots_by_phase[slot.phase].add(slot.slot_id)
        self.slots_by_lane[slot.lane].add(slot.slot_id)
        self.slots_by_priority[slot.priority_class].add(slot.slot_id)
        self.slots_by_factory_lane[(slot.factory_id, slot.lane)].add(slot.slot_id)
        self.slots_by_factory_priority[(slot.factory_id, slot.priority_class)].add(slot.slot_id)
        if slot.platform_id:
            self.slots_by_platform[slot.platform_id].add(slot.slot_id)
            self.slots_by_platform_phase[(slot.platform_id, slot.phase)].add(slot.slot_id)
    
    def remove_slot(self, slot_id: str):
        """Remove slot from all indices"""
        if slot_id not in self.active_slots:
            return
        
        slot = self.active_slots[slot_id]
        del self.active_slots[slot_id]
        
        self.slots_by_factory[slot.factory_id].discard(slot_id)
        self.slots_by_phase[slot.phase].discard(slot_id)
        self.slots_by_lane[slot.lane].discard(slot_id)
        self.slots_by_priority[slot.priority_class].discard(slot_id)
        self.slots_by_factory_lane[(slot.factory_id, slot.lane)].discard(slot_id)
        self.slots_by_factory_priority[(slot.factory_id, slot.priority_class)].discard(slot_id)
        if slot.platform_id:
            self.slots_by_platform[slot.platform_id].discard(slot_id)
            self.slots_by_platform_phase[(slot.platform_id, slot.phase)].discard(slot_id)
    
    def cleanup_expired_slots(self):
        """Remove expired slots"""
        now = self.time_fn()
        expired = [
            sid for sid, slot in self.active_slots.items()
            if slot.is_expired(now)
        ]
        for sid in expired:
            self.remove_slot(sid)
    
    def get_lane_utilization(self, lane: ExecutionLane) -> Tuple[int, int]:
        """Returns (current, capacity) for lane"""
        current = len(self.slots_by_lane[lane])
        base_cap = LANE_CONCURRENCY_CAPS[lane]
        
        # Apply burst multiplier if active
        if lane in self.active_bursts and self.active_bursts[lane].is_active():
            capacity = int(base_cap * BURST_MULTIPLIERS[lane])
        else:
            capacity = base_cap
        
        return current, capacity
    
    def get_phase_utilization(self, phase: ExecutionPhase) -> Tuple[int, int]:
        """Returns (current, capacity) for phase"""
        current = len(self.slots_by_phase[phase])
        capacity = PHASE_CONCURRENCY_CAPS[phase]
        return current, capacity
    
    def get_factory_utilization(self, factory_id: str) -> int:
        """Returns current slot count for factory"""
        return len(self.slots_by_factory[factory_id])

    def get_factory_lane_utilization(self, factory_id: str, lane: ExecutionLane) -> int:
        """Returns current slot count for factory + lane"""
        return len(self.slots_by_factory_lane[(factory_id, lane)])

    def get_factory_priority_utilization(self, factory_id: str, priority: PriorityClass) -> int:
        """Returns current slot count for factory + priority"""
        return len(self.slots_by_factory_priority[(factory_id, priority)])

    def get_platform_utilization(self, platform_id: str) -> int:
        """Returns current slot count for platform"""
        return len(self.slots_by_platform[platform_id])

    def get_platform_phase_utilization(self, platform_id: str, phase: ExecutionPhase) -> int:
        """Returns current slot count for platform + phase"""
        return len(self.slots_by_platform_phase[(platform_id, phase)])


# ============================================================================
# THROUGHPUT CONTROLLER
# ============================================================================

class ThroughputController:
    """
    Controls:
    - max concurrent tasks per factory
    - max per phase
    - weighted capacity per priority class
    """
    
    def __init__(
        self,
        max_per_factory: int = 20,
        priority_weights: Optional[Dict[PriorityClass, float]] = None,
        platform_caps: Optional[Dict[str, PlatformCaps]] = None,
        max_lane_share_per_factory: float = 0.6
    ):
        self.max_per_factory = max_per_factory
        self.priority_weights = priority_weights or PRIORITY_CLASS_WEIGHTS
        self.platform_caps = platform_caps or {}
        self.max_lane_share_per_factory = max_lane_share_per_factory
    
    def check_factory_capacity(
        self,
        state: SchedulerState,
        factory_id: str
    ) -> Tuple[bool, Optional[str]]:
        """Check if factory has capacity"""
        current = state.get_factory_utilization(factory_id)
        
        if current >= self.max_per_factory:
            return False, f"factory {factory_id} at capacity ({current}/{self.max_per_factory})"
        
        return True, None

    def check_priority_gate(
        self,
        state: SchedulerState,
        factory_id: str,
        priority: PriorityClass
    ) -> Tuple[bool, Optional[str]]:
        """
        Ensure lower priorities do not consume reserved capacity
        needed for higher priorities.
        """
        total_cap = self.max_per_factory
        current_total = state.get_factory_utilization(factory_id)

        # Compute reserved capacity for higher priorities
        higher_priorities = [
            p for p in PriorityClass if p.value > priority.value
        ]
        reserved_for_higher = 0
        for p in higher_priorities:
            weight = self.priority_weights.get(p, 0.0)
            reserved_for_higher += max(1, int(round(total_cap * weight)))

        # If total usage reaches the non-reserved portion, block lower priority
        allowed_for_lower = max(0, total_cap - reserved_for_higher)
        if priority.value < PriorityClass.HIGH.value and current_total >= allowed_for_lower:
            return False, (
                f"priority gate: reserved capacity for higher priorities "
                f"(usage={current_total}, lower_cap={allowed_for_lower})"
            )

        return True, None

    def check_factory_lane_share(
        self,
        state: SchedulerState,
        factory_id: str,
        lane: ExecutionLane
    ) -> Tuple[bool, Optional[str]]:
        """Prevent a single factory from dominating a lane"""
        current_lane, lane_cap = state.get_lane_utilization(lane)
        if lane_cap == 0:
            return False, f"lane {lane.value} has zero capacity"

        factory_lane = state.get_factory_lane_utilization(factory_id, lane)
        max_share = max(1, int(round(lane_cap * self.max_lane_share_per_factory)))

        if factory_lane >= max_share and current_lane >= lane_cap:
            return False, (
                f"factory {factory_id} exceeded lane share "
                f"({factory_lane}/{max_share})"
            )

        return True, None
    
    def check_lane_capacity(
        self,
        state: SchedulerState,
        lane: ExecutionLane
    ) -> Tuple[bool, Optional[str]]:
        """Check if lane has capacity"""
        current, capacity = state.get_lane_utilization(lane)
        
        if current >= capacity:
            return False, f"lane {lane.value} at capacity ({current}/{capacity})"
        
        return True, None
    
    def check_phase_capacity(
        self,
        state: SchedulerState,
        phase: ExecutionPhase
    ) -> Tuple[bool, Optional[str]]:
        """Check if phase has capacity"""
        current, capacity = state.get_phase_utilization(phase)
        
        if current >= capacity:
            return False, f"phase {phase.value} at capacity ({current}/{capacity})"
        
        return True, None

    def check_platform_capacity(
        self,
        state: SchedulerState,
        platform_id: Optional[str],
        phase: ExecutionPhase
    ) -> Tuple[bool, Optional[str]]:
        """Check platform-specific hard caps"""
        if not platform_id or platform_id not in self.platform_caps:
            return True, None

        caps = self.platform_caps[platform_id]
        current_total = state.get_platform_utilization(platform_id)
        if current_total >= caps.max_concurrent:
            return False, (
                f"platform {platform_id} at capacity "
                f"({current_total}/{caps.max_concurrent})"
            )

        phase_cap = caps.max_per_phase.get(phase)
        if phase_cap is not None:
            current_phase = state.get_platform_phase_utilization(platform_id, phase)
            if current_phase >= phase_cap:
                return False, (
                    f"platform {platform_id} phase {phase.value} at capacity "
                    f"({current_phase}/{phase_cap})"
                )

        return True, None

    def find_preempt_candidate(
        self,
        state: SchedulerState,
        challenger_priority: PriorityClass,
        lane: ExecutionLane
    ) -> Optional[ExecutionSlot]:
        """Find a lower-priority slot in lane to preempt"""
        candidates = [
            state.active_slots[sid]
            for sid in state.slots_by_lane[lane]
        ]
        lower = [
            slot for slot in candidates
            if slot.priority_class.value < challenger_priority.value
        ]
        if not lower:
            return None

        # Preempt the lowest priority, oldest first for determinism
        lower.sort(key=lambda s: (s.priority_class.value, s.allocated_at, s.slot_id))
        return lower[0]


# ============================================================================
# PRIORITY LANE BALANCER
# ============================================================================

class PriorityLaneBalancer:
    """
    Maps priority to lanes.
    Enforces lane-specific policies.
    """
    
    def get_lane(self, priority: PriorityClass) -> ExecutionLane:
        """Map priority to execution lane"""
        return PRIORITY_TO_LANE[priority]
    
    def can_preempt(
        self,
        challenger: PriorityClass,
        incumbent: PriorityClass
    ) -> bool:
        """
        Determine if challenger can preempt incumbent.
        
        Rules:
        - CRITICAL can preempt anything below CRITICAL
        - HIGH can preempt NORMAL/LOW/DEFERRED
        - Others cannot preempt
        """
        if challenger == PriorityClass.CRITICAL:
            return incumbent != PriorityClass.CRITICAL
        
        if challenger == PriorityClass.HIGH:
            return incumbent.value < PriorityClass.HIGH.value
        
        return False


# ============================================================================
# BURST ELASTICITY MANAGER
# ============================================================================

class BurstElasticityManager:
    """
    Temporarily expands capacity when:
    - burst detected upstream
    - uncertainty is low
    - platform window is narrow
    - long-tail opportunity confirmed
    
    Expansion is:
    - bounded
    - time-limited
    - logged
    """
    
    def __init__(self, logger: logging.Logger, time_fn: Callable[[], float]):
        self.logger = logger
        self.time_fn = time_fn
    
    def activate_burst(
        self,
        state: SchedulerState,
        lane: ExecutionLane,
        reason: str
    ) -> bool:
        """Activate burst mode for lane"""
        now = self.time_fn()
        
        if lane in state.active_bursts:
            existing = state.active_bursts[lane]
            if existing.is_active():
                self.logger.info(f"Burst already active for {lane.value}")
                return False
        
        burst = BurstState(
            active=True,
            started_at=now,
            expires_at=now + BURST_DURATION_SECONDS,
            lane=lane,
            reason=reason
        )
        
        state.active_bursts[lane] = burst
        
        self.logger.warning(
            f"BURST ACTIVATED: lane={lane.value} reason={reason} "
            f"duration={BURST_DURATION_SECONDS}s"
        )
        
        return True
    
    def deactivate_burst(
        self,
        state: SchedulerState,
        lane: ExecutionLane
    ):
        """Deactivate burst mode"""
        if lane in state.active_bursts:
            state.active_bursts[lane].active = False
            self.logger.info(f"Burst deactivated for {lane.value}")
    
    def cleanup_expired_bursts(self, state: SchedulerState):
        """Remove expired bursts"""
        now = self.time_fn()
        for lane, burst in list(state.active_bursts.items()):
            if not burst.is_active():
                self.logger.info(f"Burst expired for {lane.value}")
                del state.active_bursts[lane]


# ============================================================================
# INVERSION GUARD
# ============================================================================

class InversionGuard:
    """
    Prevents:
    - LOW priority occupying scarce slots
    - NORMAL blocking HIGH under load
    - retries leapfrogging fresh candidates
    
    This is where most systems die.
    """
    
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.rejection_window_seconds = 60
        self.max_rejections_before_block = 3
    
    def check_inversion_risk(
        self,
        state: SchedulerState,
        priority: PriorityClass,
        lane: ExecutionLane,
        is_retry: bool
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if admitting this priority would cause inversion.
        
        If HIGH/CRITICAL tasks have been rejected recently due to capacity,
        block LOW/NORMAL admissions temporarily.
        """
        now = state.time_fn()
        
        # Clean old rejections
        state.recent_rejections = [
            (ts, pri, reason)
            for ts, pri, reason in state.recent_rejections
            if now - ts < self.rejection_window_seconds
        ]
        
        # Count recent high-priority rejections
        high_priority_rejections = [
            (ts, pri, reason)
            for ts, pri, reason in state.recent_rejections
            if pri.value >= PriorityClass.HIGH.value
        ]
        
        # If we've rejected HIGH/CRITICAL recently, block LOW/NORMAL
        if len(high_priority_rejections) >= self.max_rejections_before_block:
            if priority.value < PriorityClass.HIGH.value:
                return False, (
                    f"inversion guard: {len(high_priority_rejections)} high-priority "
                    f"rejections in last {self.rejection_window_seconds}s, blocking {priority.name}"
                )
        
        # Prevent retries from leapfrogging fresh candidates
        if is_retry:
            recent_fresh_rejections = [
                (ts, pri, is_retry_attempt, admitted, reason)
                for ts, pri, is_retry_attempt, admitted, reason in state.recent_attempts
                if not is_retry_attempt and not admitted and pri.value >= priority.value
            ]
            if recent_fresh_rejections:
                return False, (
                    "inversion guard: fresh candidates recently rejected, "
                    "blocking retry admission"
                )

        return True, None
    
    def record_rejection(
        self,
        state: SchedulerState,
        priority: PriorityClass,
        reason: str
    ):
        """Record rejection for inversion detection"""
        state.recent_rejections.append((state.time_fn(), priority, reason))

    def record_attempt(
        self,
        state: SchedulerState,
        priority: PriorityClass,
        is_retry: bool,
        admitted: bool,
        reason: str
    ):
        """Record admission attempt for inversion analysis"""
        state.recent_attempts.append(
            (state.time_fn(), priority, is_retry, admitted, reason)
        )


# ============================================================================
# SCHEDULER AUDIT LOG
# ============================================================================

class SchedulerAuditLog:
    """
    Every admission attempt logs:
    - task_id
    - factory_id
    - priority
    - admitted (bool)
    - lane
    - slot_id
    - reason
    
    No silent drops. Ever.
    """
    
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.decisions: List[Dict] = []
        self.max_history = 10000
    
    def log_decision(self, decision: AdmissionDecision, task_id: str):
        """Log admission decision"""
        entry = {
            "timestamp": decision.timestamp,
            "task_id": task_id,
            "factory_id": decision.factory_id,
            "priority": decision.priority_class.name if decision.priority_class else None,
            "phase": decision.phase.value if decision.phase else None,
            "admitted": decision.admitted,
            "lane": decision.intent.lane.value if decision.intent else None,
            "platform_id": decision.intent.platform_id if decision.intent else None,
            "slot_id": decision.slot.slot_id if decision.slot else None,
            "intent_id": decision.intent.intent_id if decision.intent else None,
            "reason": decision.reason,
            "retry_after": decision.retry_after_seconds,
        }
        
        self.decisions.append(entry)
        
        # Trim history
        if len(self.decisions) > self.max_history:
            self.decisions = self.decisions[-self.max_history:]
        
        # Log to standard logger
        level = logging.INFO if decision.admitted else logging.WARNING
        self.logger.log(
            level,
            f"ADMISSION: task={task_id} factory={decision.factory_id} "
            f"priority={decision.priority_class.name if decision.priority_class else 'N/A'} "
            f"admitted={decision.admitted} reason={decision.reason}"
        )
    
    def get_recent_decisions(self, limit: int = 100) -> List[Dict]:
        """Get recent decisions for debugging"""
        return self.decisions[-limit:]


# ============================================================================
# FACTORY SCHEDULER (CORE ENGINE)
# ============================================================================

class FactoryScheduler:
    """
    Core admission control engine.
    
    Inputs are already validated upstream.
    
    Admission proceeds in this exact order:
    1. PriorityClass gate
    2. Dependency readiness
    3. Factory concurrency limit
    4. Global resource pressure
    5. Platform hard caps
    6. Phase capacity
    7. Burst elasticity rules
    8. Lane capacity (with preemption)
    9. Factory lane share
    10. Inversion check
    11. Slot allocation
    
    Fail fast.
    Fail explicitly.
    """
    
    def __init__(
        self,
        max_per_factory: int = 20,
        enable_burst: bool = True,
        logger: Optional[logging.Logger] = None,
        time_fn: Callable[[], float] = time.time,
        platform_caps: Optional[Dict[str, PlatformCaps]] = None,
        priority_weights: Optional[Dict[PriorityClass, float]] = None,
        max_lane_share_per_factory: float = 0.6
    ):
        self.state = SchedulerState(time_fn=time_fn)
        self.throughput = ThroughputController(
            max_per_factory=max_per_factory,
            priority_weights=priority_weights,
            platform_caps=platform_caps,
            max_lane_share_per_factory=max_lane_share_per_factory
        )
        self.lane_balancer = PriorityLaneBalancer()
        
        self.logger = logger or logging.getLogger(__name__)
        
        self.burst_manager = BurstElasticityManager(self.logger, time_fn=time_fn)
        self.inversion_guard = InversionGuard(self.logger)
        self.audit_log = SchedulerAuditLog(self.logger)
        
        self.enable_burst = enable_burst
    
    def admit(
        self,
        routing_decision: RoutingDecision,
        resource_pressure: Optional[ResourcePressure] = None
    ) -> AdmissionDecision:
        """
        Main admission control entry point.
        
        Returns AdmissionDecision with slot if admitted.
        """
        # Cleanup before admission
        self.state.cleanup_expired_slots()
        if self.enable_burst:
            self.burst_manager.cleanup_expired_bursts(self.state)
        
        task_id = routing_decision.task_id
        factory_id = routing_decision.factory_id
        priority = routing_decision.priority_class
        phase = routing_decision.phase
        lane = self.lane_balancer.get_lane(priority)
        platform_id = routing_decision.platform_id
        
        # Step 1: Priority gate
        priority_ok, priority_reason = self.throughput.check_priority_gate(
            self.state, factory_id, priority
        )
        if not priority_ok:
            decision = AdmissionDecision(
                admitted=False,
                slot=None,
                intent=None,
                reason=priority_reason,
                retry_after_seconds=30,
                priority_class=priority,
                factory_id=factory_id,
                phase=phase
            )
            self.audit_log.log_decision(decision, task_id)
            self.inversion_guard.record_attempt(
                self.state, priority, routing_decision.is_retry, False, priority_reason
            )
            return decision

        # Step 2: Dependency readiness
        if not routing_decision.dependencies_ready:
            decision = AdmissionDecision(
                admitted=False,
                slot=None,
                intent=None,
                reason="dependencies not ready",
                retry_after_seconds=10,
                priority_class=priority,
                factory_id=factory_id,
                phase=phase
            )
            self.audit_log.log_decision(decision, task_id)
            self.inversion_guard.record_attempt(
                self.state, priority, routing_decision.is_retry, False, "dependencies"
            )
            return decision
        
        # Step 3: Factory capacity
        factory_ok, factory_reason = self.throughput.check_factory_capacity(
            self.state, factory_id
        )
        if not factory_ok:
            decision = AdmissionDecision(
                admitted=False,
                slot=None,
                intent=None,
                reason=factory_reason,
                retry_after_seconds=20,
                priority_class=priority,
                factory_id=factory_id,
                phase=phase
            )
            self.audit_log.log_decision(decision, task_id)
            self.inversion_guard.record_rejection(self.state, priority, "factory_capacity")
            self.inversion_guard.record_attempt(
                self.state, priority, routing_decision.is_retry, False, "factory_capacity"
            )
            return decision

        # Step 4: Global resource pressure check
        if resource_pressure and resource_pressure.is_under_pressure():
            # Only admit CRITICAL under pressure
            if priority != PriorityClass.CRITICAL:
                decision = AdmissionDecision(
                    admitted=False,
                    slot=None,
                    intent=None,
                    reason="resource pressure high, only CRITICAL admitted",
                    retry_after_seconds=30,
                    priority_class=priority,
                    factory_id=factory_id,
                    phase=phase
                )
                self.audit_log.log_decision(decision, task_id)
                self.inversion_guard.record_rejection(self.state, priority, "resource_pressure")
                self.inversion_guard.record_attempt(
                    self.state, priority, routing_decision.is_retry, False, "resource_pressure"
                )
                return decision
        
        # Step 5: Platform hard caps
        platform_ok, platform_reason = self.throughput.check_platform_capacity(
            self.state, platform_id, phase
        )
        if not platform_ok:
            decision = AdmissionDecision(
                admitted=False,
                slot=None,
                intent=None,
                reason=platform_reason,
                retry_after_seconds=20,
                priority_class=priority,
                factory_id=factory_id,
                phase=phase
            )
            self.audit_log.log_decision(decision, task_id)
            self.inversion_guard.record_rejection(self.state, priority, "platform_capacity")
            self.inversion_guard.record_attempt(
                self.state, priority, routing_decision.is_retry, False, "platform_capacity"
            )
            return decision

        # Step 6: Phase capacity
        phase_ok, phase_reason = self.throughput.check_phase_capacity(
            self.state, phase
        )
        if not phase_ok:
            decision = AdmissionDecision(
                admitted=False,
                slot=None,
                intent=None,
                reason=phase_reason,
                retry_after_seconds=15,
                priority_class=priority,
                factory_id=factory_id,
                phase=phase
            )
            self.audit_log.log_decision(decision, task_id)
            self.inversion_guard.record_rejection(self.state, priority, "phase_capacity")
            self.inversion_guard.record_attempt(
                self.state, priority, routing_decision.is_retry, False, "phase_capacity"
            )
            return decision
        
        # Step 7: Burst elasticity rules
        if routing_decision.burst_signal and self.enable_burst:
            reason = routing_decision.burst_reason or "upstream burst signal"
            self.burst_manager.activate_burst(self.state, lane, reason)

        # Step 8: Lane capacity
        lane_ok, lane_reason = self.throughput.check_lane_capacity(
            self.state, lane
        )
        if not lane_ok:
            # Attempt preemption for higher priority
            candidate = self.throughput.find_preempt_candidate(
                self.state, priority, lane
            )
            if candidate and self.lane_balancer.can_preempt(priority, candidate.priority_class):
                # Never preempt CRITICAL from another factory
                if not (
                    candidate.priority_class == PriorityClass.CRITICAL
                    and candidate.factory_id != factory_id
                ):
                    self.release_slot(candidate.slot_id)
                    lane_ok = True

            if not lane_ok:
                decision = AdmissionDecision(
                    admitted=False,
                    slot=None,
                    intent=None,
                    reason=lane_reason,
                    retry_after_seconds=10,
                    priority_class=priority,
                    factory_id=factory_id,
                    phase=phase
                )
                self.audit_log.log_decision(decision, task_id)
                self.inversion_guard.record_rejection(self.state, priority, "lane_capacity")
                self.inversion_guard.record_attempt(
                    self.state, priority, routing_decision.is_retry, False, "lane_capacity"
                )
                return decision

        # Step 9: Factory lane share
        lane_share_ok, lane_share_reason = self.throughput.check_factory_lane_share(
            self.state, factory_id, lane
        )
        if not lane_share_ok:
            decision = AdmissionDecision(
                admitted=False,
                slot=None,
                intent=None,
                reason=lane_share_reason,
                retry_after_seconds=10,
                priority_class=priority,
                factory_id=factory_id,
                phase=phase
            )
            self.audit_log.log_decision(decision, task_id)
            self.inversion_guard.record_rejection(self.state, priority, "lane_share")
            self.inversion_guard.record_attempt(
                self.state, priority, routing_decision.is_retry, False, "lane_share"
            )
            return decision
        
        # Step 10: Inversion guard
        inversion_ok, inversion_reason = self.inversion_guard.check_inversion_risk(
            self.state, priority, lane, routing_decision.is_retry
        )
        if not inversion_ok:
            decision = AdmissionDecision(
                admitted=False,
                slot=None,
                intent=None,
                reason=inversion_reason,
                retry_after_seconds=30,
                priority_class=priority,
                factory_id=factory_id,
                phase=phase
            )
            self.audit_log.log_decision(decision, task_id)
            self.inversion_guard.record_attempt(
                self.state, priority, routing_decision.is_retry, False, "inversion_guard"
            )
            return decision
        
        # Step 11: Allocate slot
        slot = self._allocate_slot(
            task_id=task_id,
            factory_id=factory_id,
            phase=phase,
            priority=priority,
            lane=lane,
            resource_cost=routing_decision.estimated_resource_cost,
            platform_id=platform_id
        )
        
        self.state.add_slot(slot)

        intent = ExecutionIntent(
            intent_id=slot.slot_id,
            task_id=task_id,
            factory_id=factory_id,
            phase=phase,
            priority_class=priority,
            lane=lane,
            platform_id=platform_id,
            created_at=slot.allocated_at
        )
        
        decision = AdmissionDecision(
            admitted=True,
            slot=slot,
            intent=intent,
            reason="capacity available",
            retry_after_seconds=None,
            priority_class=priority,
            factory_id=factory_id,
            phase=phase
        )
        
        self.audit_log.log_decision(decision, task_id)
        self.inversion_guard.record_attempt(
            self.state, priority, routing_decision.is_retry, True, "admitted"
        )
        
        return decision
    
    def _allocate_slot(
        self,
        task_id: str,
        factory_id: str,
        phase: ExecutionPhase,
        priority: PriorityClass,
        lane: ExecutionLane,
        resource_cost: Dict[str, float],
        platform_id: Optional[str]
    ) -> ExecutionSlot:
        """Allocate a new execution slot"""
        now = self.state.time_fn()
        slot_seq = self.state.next_slot_seq
        self.state.next_slot_seq += 1
        
        slot = ExecutionSlot(
            slot_id=f"slot_{slot_seq:08d}",
            factory_id=factory_id,
            phase=phase,
            priority_class=priority,
            lane=lane,
            platform_id=platform_id,
            allocated_at=now,
            expires_at=now + SLOT_EXPIRY_SECONDS,
            task_id=task_id,
            resource_cost=resource_cost
        )
        
        return slot
    
    def release_slot(self, slot_id: str):
        """Release an execution slot"""
        self.state.remove_slot(slot_id)
        self.logger.info(f"Released slot {slot_id}")
    
    def activate_burst_mode(self, lane: ExecutionLane, reason: str):
        """Manually activate burst mode"""
        if self.enable_burst:
            self.burst_manager.activate_burst(self.state, lane, reason)
    
    def get_stats(self) -> Dict:
        """Get current scheduler statistics"""
        stats = {
            "total_active_slots": len(self.state.active_slots),
            "by_lane": {},
            "by_phase": {},
            "by_priority": {},
            "active_bursts": [],
        }
        
        for lane in ExecutionLane:
            current, capacity = self.state.get_lane_utilization(lane)
            stats["by_lane"][lane.value] = {
                "current": current,
                "capacity": capacity,
                "utilization": current / capacity if capacity > 0 else 0
            }
        
        for phase in ExecutionPhase:
            current, capacity = self.state.get_phase_utilization(phase)
            stats["by_phase"][phase.value] = {
                "current": current,
                "capacity": capacity,
                "utilization": current / capacity if capacity > 0 else 0
            }
        
        for priority in PriorityClass:
            count = len(self.state.slots_by_priority[priority])
            stats["by_priority"][priority.name] = count
        
        for lane, burst in self.state.active_bursts.items():
            if burst.is_active():
                stats["active_bursts"].append({
                    "lane": lane.value,
                    "reason": burst.reason,
                    "expires_in": burst.expires_at - time.time()
                })
        
        return stats


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

if __name__ == "__main__":
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Create scheduler
    scheduler = FactoryScheduler(max_per_factory=20, enable_burst=True)
    
    # Simulate admissions
    decisions = []
    
    for i in range(100):
        # Vary priority
        if i % 10 == 0:
            priority = PriorityClass.CRITICAL
        elif i % 5 == 0:
            priority = PriorityClass.HIGH
        elif i % 3 == 0:
            priority = PriorityClass.NORMAL
        else:
            priority = PriorityClass.LOW
        
        routing = RoutingDecision(
            task_id=f"task_{i}",
            factory_id=f"factory_{i % 5}",
            priority_class=priority,
            phase=ExecutionPhase.GENERATION,
            dependencies_ready=True,
            estimated_resource_cost={"cpu": 0.1, "memory": 100}
        )
        
        pressure = ResourcePressure(
            cpu_utilization=0.5,
            memory_utilization=0.6,
            api_quota_remaining=0.8,
            platform_rate_limit_pressure=0.3
        )
        
        decision = scheduler.admit(routing, pressure)
        decisions.append(decision)
        
        if not decision.admitted:
            print(f"Task {i} REJECTED: {decision.reason}")
    
    # Print stats
    print("\n" + "="*60)
    print("SCHEDULER STATISTICS")
    print("="*60)
    stats = scheduler.get_stats()
    
    print(f"\nTotal active slots: {stats['total_active_slots']}")
    
    print("\nBy Lane:")
    for lane, data in stats['by_lane'].items():
        print(f"  {lane}: {data['current']}/{data['capacity']} ({data['utilization']:.1%})")
    
    print("\nBy Phase:")
    for phase, data in stats['by_phase'].items():
        print(f"  {phase}: {data['current']}/{data['capacity']} ({data['utilization']:.1%})")
    
    print("\nBy Priority:")
    for priority, count in stats['by_priority'].items():
        print(f"  {priority}: {count}")
    
    admission_rate = sum(1 for d in decisions if d.admitted) / len(decisions)
    print(f"\nOverall admission rate: {admission_rate:.1%}")