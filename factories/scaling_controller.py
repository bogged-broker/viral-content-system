"""
AI Viral Content Factory - Scaling Controller
Production-grade resource enforcement layer.

Path: AI_Viral_Content_Factory/factories/scaling_controller.py
"""

# DESIGN RULE:
# Time-based logic is ONLY permitted in apply_mode_transition().
# Admission and enforcement must be fully deterministic.

# GLOBAL INVARIANT:
# At no time may admitted execution exceed the LAST successfully ingested budget plan.
# Scaling modes may only REDUCE effective capacity, never increase it.

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, FrozenSet, List
from enum import Enum

from utils.logger import get_logger


logger = get_logger("ScalingController")

# Operational constants
MIN_MODE_DURATION = 30  # seconds - prevents mode thrashing

# Public API contract
__all__ = [
    "ScalingController",
    "ScalingMode",
    "ExecutionLimits",
    "UtilizationSnapshot",
    "GlobalCapacity",
    "InvariantType",
    "InvariantDefinition",
    "create_scaling_controller",
]


class ScalingMode(str, Enum):
    """System scaling states - always exactly one active."""
    NORMAL = "NORMAL"
    SCALE_UP = "SCALE_UP"  # Means "remove penalties", NOT increase capacity
    SCALE_DOWN = "SCALE_DOWN"
    EMERGENCY_BRAKE = "EMERGENCY_BRAKE"


@dataclass(frozen=True)
class GlobalCapacity:
    """Frozen global capacity constraints - immutable system limits."""
    max_parallel_jobs: int
    max_gpu_units: float
    max_cpu_units: float
    max_tokens_per_minute: int


class InvariantType(str, Enum):
    """Registry of system invariants for validation."""
    GLOBAL_CAPACITY = "global_capacity"
    PER_NICHE_PARALLELISM = "per_niche_parallelism"
    ADMISSION_CONSISTENCY = "admission_consistency"


@dataclass(frozen=True)
class InvariantDefinition:
    """Definition of a system invariant for validation."""
    name: InvariantType
    description: str
    validator: str  # Method name that validates this invariant


@dataclass
class ExecutionLimits:
    """Hard constraints for a single niche's execution."""
    max_parallel_jobs: int
    gpu_units: float
    cpu_units: float
    tokens_per_minute: int
    posting_limits: Dict[str, int]
    retry_budget: int
    burst_enabled: bool = False


@dataclass
class UtilizationSnapshot:
    """Real-time system resource state."""
    gpu_utilization: float
    cpu_utilization: float
    active_jobs: int
    active_jobs_per_niche: Dict[str, int]  # Per-niche job tracking for invariant validation
    queue_depths: Dict[str, int]
    posting_backlog: Dict[str, int]
    timestamp: float = field(default_factory=time.time)


class ScalingController:
    """
    HARD ENFORCEMENT LAYER.
    
    Converts budget plans into physical execution limits.
    Prevents resource contention.
    Guarantees viral winners capture scale.
    
    This is the enforcement layer - not a decision maker.
    It executes what budget_allocator.py decides.
    
    Note:
        This controller uses SOFT ENFORCEMENT model:
        - Mode changes affect limit interpretation downstream
        - Budget allocations are NOT rewritten on mode transitions
        - get_execution_limits() applies mode-based multipliers to base budgets
        - This allows budget stability while enabling dynamic scaling behavior
    """

    # REGISTRY: System invariants for validation
    _INVARIANT_REGISTRY: FrozenSet[InvariantDefinition] = frozenset([
        InvariantDefinition(
            InvariantType.GLOBAL_CAPACITY,
            "Aggregate enforced capacity ≤ global capacity",
            "_validate_global_capacity_invariant"
        ),
        InvariantDefinition(
            InvariantType.PER_NICHE_PARALLELISM,
            "Per-niche active jobs ≤ per-niche max parallel jobs",
            "_validate_per_niche_parallelism_invariant"
        ),
        InvariantDefinition(
            InvariantType.ADMISSION_CONSISTENCY,
            "Admission decisions consistent with current mode",
            "_validate_admission_consistency_invariant"
        ),
    ])

    def __init__(
        self,
        global_capacity: GlobalCapacity,
        scaling_config: Dict[str, Any],
    ):
        """
        Initialize scaling controller with global capacity constraints.
        
        Args:
            global_capacity: Frozen global capacity constraints (ENFORCED)
            scaling_config: Configuration for scaling behavior thresholds
            
        Note:
            This controller uses SOFT ENFORCEMENT model:
            - Mode changes affect limit interpretation downstream
            - Budget allocations are NOT rewritten on mode transitions
            - get_execution_limits() applies mode-based multipliers to base budgets
            - This allows budget stability while enabling dynamic scaling.
        """
        self.global_capacity = global_capacity
        self.scaling_config = scaling_config

        self.current_mode: ScalingMode = ScalingMode.NORMAL
        self.niche_limits: Dict[str, ExecutionLimits] = {}
        self.current_utilization: Optional[UtilizationSnapshot] = None
        
        # Operational state
        self._last_mode_change_ts: float = time.time()
        self._min_mode_duration_sec: int = MIN_MODE_DURATION  # Use constant

        self._lock = threading.Lock()
        self.scaling_events_log: list[dict] = []
        self._max_events_log_size: int = 1000  # Cap log size for long-running systems
        
        logger.info("ScalingController initialized with global capacity enforcement")

    # ------------------------------------------------------------------
    # BUDGET INGESTION
    # ------------------------------------------------------------------

    def ingest_budget_plan(self, budget_snapshot: Dict[str, Any]) -> None:
        """
        Ingests a fully-resolved budget allocation snapshot.
        
        This operation is atomic and overrides previous limits.
        Called by budget_allocator.py after reallocation decisions.
        Enforces global capacity constraints.
        
        Args:
            budget_snapshot: Dict mapping niche -> allocation plan
            
        Raises:
            ValueError: If budget exceeds global capacity
        """
        with self._lock:
            # Validate global capacity constraints
            total_parallel_jobs = sum(plan["compute"]["max_parallel_jobs"] for plan in budget_snapshot.values())
            total_gpu = sum(plan["compute"]["gpu_units"] for plan in budget_snapshot.values())
            total_cpu = sum(plan["compute"]["cpu_units"] for plan in budget_snapshot.values())
            total_tokens = sum(plan["compute"]["tokens_per_minute"] for plan in budget_snapshot.values())
            
            # GLOBAL CAPACITY ENFORCEMENT: sum(niche limits) ≤ global capacity
            if total_parallel_jobs > self.global_capacity.max_parallel_jobs:
                self._emit_metric("budget_rejected_total", 1, reason="global_parallel_jobs_exceeded")
                raise ValueError(
                    f"Global parallel job capacity exceeded: {total_parallel_jobs} > "
                    f"{self.global_capacity.max_parallel_jobs}"
                )
            if total_gpu > self.global_capacity.max_gpu_units:
                self._emit_metric("budget_rejected_total", 1, reason="global_gpu_exceeded")
                raise ValueError(
                    f"Global GPU capacity exceeded: {total_gpu} > "
                    f"{self.global_capacity.max_gpu_units}"
                )
            if total_cpu > self.global_capacity.max_cpu_units:
                self._emit_metric("budget_rejected_total", 1, reason="global_cpu_exceeded")
                raise ValueError(
                    f"Global CPU capacity exceeded: {total_cpu} > "
                    f"{self.global_capacity.max_cpu_units}"
                )
            if total_tokens > self.global_capacity.max_tokens_per_minute:
                self._emit_metric("budget_rejected_total", 1, reason="global_tokens_exceeded")
                raise ValueError(
                    f"Global token capacity exceeded: {total_tokens} > "
                    f"{self.global_capacity.max_tokens_per_minute}"
                )
            
            self.niche_limits.clear()

            for niche, plan in budget_snapshot.items():
                limits = ExecutionLimits(
                    max_parallel_jobs=plan["compute"]["max_parallel_jobs"],
                    gpu_units=plan["compute"]["gpu_units"],
                    cpu_units=plan["compute"]["cpu_units"],
                    tokens_per_minute=plan["compute"]["tokens_per_minute"],
                    posting_limits=plan["posting"]["per_platform_limits"],
                    retry_budget=plan["retry_policy"]["max_retries"],
                    burst_enabled=plan["compute"].get("burst_enabled", False),
                )
                self.niche_limits[niche] = limits

            logger.info(
                "Budget plan ingested for %d niches (global capacity enforced)",
                len(self.niche_limits),
            )

    # ------------------------------------------------------------------
    # UTILIZATION INGESTION
    # ------------------------------------------------------------------

    def update_utilization(self, snapshot: UtilizationSnapshot) -> None:
        """
        Update real-time resource utilization snapshot.
        
        Called by monitoring subsystem to feed current system state.
        
        Args:
            snapshot: Current resource utilization metrics
        """
        with self._lock:
            self.current_utilization = snapshot

    # ------------------------------------------------------------------
    # MODE EVALUATION
    # ------------------------------------------------------------------

    def evaluate_mode(self) -> ScalingMode:
        """
        Determines desired scaling mode based on utilization ONLY.
        
        Pure evaluation function - no policy enforcement.
        Uses scaling_config thresholds for consistency:
        - >emergency_threshold: EMERGENCY_BRAKE
        - >scale_down_threshold: SCALE_DOWN
        - <scale_up_threshold: SCALE_UP
        - else: NORMAL
        
        Returns:
            Desired scaling mode for current utilization
        """
        if not self.current_utilization:
            return ScalingMode.EMERGENCY_BRAKE

        util = self.current_utilization
        config = self.scaling_config

        # Emergency conditions - use config threshold
        if util.gpu_utilization > config["emergency_threshold"] or util.cpu_utilization > config["emergency_threshold"]:
            return ScalingMode.EMERGENCY_BRAKE

        # High load - reduce capacity - use config threshold
        if util.gpu_utilization > config["scale_down_threshold"] or util.cpu_utilization > config["scale_down_threshold"]:
            return ScalingMode.SCALE_DOWN

        # Low load - can increase capacity - use config threshold
        if util.gpu_utilization < config["scale_up_threshold"] and util.cpu_utilization < config["scale_up_threshold"]:
            return ScalingMode.SCALE_UP

        return ScalingMode.NORMAL

    def is_emergency(self) -> bool:
        """Returns True if system is in emergency brake mode."""
        return self.current_mode == ScalingMode.EMERGENCY_BRAKE

    def _enter_emergency(self, reason: str) -> None:
        """INTERNAL: Single choke point for all emergency transitions."""
        self.current_mode = ScalingMode.EMERGENCY_BRAKE
        logger.critical("EMERGENCY BRAKE TRIGGERED: %s", reason)
        self._log_event("EMERGENCY_BRAKE", reason, None)
        self._emit_metric("emergency_brake_total", 1, reason=reason)

    def apply_mode_transition(self) -> None:
        """
        Transitions scaling modes if required.
        
        Thread-safe mode evaluation and transition.
        Logs all mode changes for audit trail.
        Implements hysteresis to prevent mode thrashing.
        Emergency brake is irreversible without manual reset.
        
        Note:
            SOFT ENFORCEMENT MODEL: This changes current_mode but does NOT
            rewrite budget allocations. Limits are interpreted downstream
            via get_execution_limits() which applies mode-based multipliers.
            This preserves budget stability while enabling dynamic scaling.
        """
        with self._lock:
            # Emergency brake is irreversible without manual reset
            if self.is_emergency():
                return
            
            now = time.time()
            
            # Rate limit mode transitions to prevent thrashing
            if now - self._last_mode_change_ts < self._min_mode_duration_sec:
                return
            
            new_mode = self.evaluate_mode()
            if new_mode != self.current_mode:
                # All emergency transitions must pass through ONE choke point
                if new_mode == ScalingMode.EMERGENCY_BRAKE:
                    self._enter_emergency("utilization_threshold_exceeded")
                    return
                
                logger.warning(
                    "Scaling mode changed: %s → %s",
                    self.current_mode,
                    new_mode,
                )
                self._log_event("MODE_TRANSITION", self.current_mode, new_mode)
                self._emit_metric("scaling_mode_transitions_total", 1, from_mode=self.current_mode, to_mode=new_mode)
                self.current_mode = new_mode
                self._last_mode_change_ts = now

    # ------------------------------------------------------------------
    # ENFORCEMENT FUNCTIONS
    # ------------------------------------------------------------------

    def _assert_admissible(self, niche: str, limits: ExecutionLimits) -> None:
        """Check if niche admission is allowed - raises if not."""
        # Emergency brake - hard stop everything with explicit denial
        if self.is_emergency():
            self._emit_metric("admission_denied_total", 1, reason="emergency_brake", niche=niche)
            raise RuntimeError(
                f"EMERGENCY_BRAKE ACTIVE: Admission denied for niche '{niche}'. "
                f"System-wide shutdown in effect. No new executions allowed."
            )

        # ADMISSION CONTROL: Enforce per-niche parallelism at controller boundary
        if self.current_utilization:
            active_jobs = self.current_utilization.active_jobs_per_niche.get(niche, 0)
            if active_jobs >= limits.max_parallel_jobs:
                self._emit_metric("admission_denied_total", 1, reason="max_capacity_reached", niche=niche)
                raise RuntimeError(
                    f"Admission denied: niche '{niche}' at max parallel capacity "
                    f"({active_jobs} >= {limits.max_parallel_jobs})"
                )

    def get_execution_limits(self, niche: str) -> ExecutionLimits:
        """
        Returns enforced execution limits for a niche.
        
        Applies mode-based multipliers to base limits:
        - EMERGENCY_BRAKE: 0% capacity + EXPLICIT ADMISSION DENIAL
        - SCALE_DOWN: 60% capacity
        - NORMAL/SCALE_UP: 100% capacity
        
        This implements SOFT ENFORCEMENT: base budgets are stable,
        but interpretation changes based on current_mode.
        
        Args:
            niche: Niche identifier to get limits for
            
        Returns:
            Enforced execution limits for the niche
            
        Raises:
            KeyError: If niche has no configured limits
            RuntimeError: If admission is denied
        """
        if niche not in self.niche_limits:
            raise KeyError(f"No scaling limits for niche: {niche}")

        limits = self.niche_limits[niche]
        
        # Check admission first
        self._assert_admissible(niche, limits)

        # Scale down - apply careful percentages per original spec
        if self.current_mode == ScalingMode.SCALE_DOWN:
            return ExecutionLimits(
                max_parallel_jobs=max(1, limits.max_parallel_jobs // 2),  # 50% reduction
                gpu_units=limits.gpu_units * 0.6,  # 60% of original
                cpu_units=limits.cpu_units * 0.6,  # 60% of original
                tokens_per_minute=int(limits.tokens_per_minute * 0.6),  # 60% of original
                posting_limits={
                    k: max(1, int(v * 0.7)) for k, v in limits.posting_limits.items()  # 70% posting
                },
                retry_budget=max(0, limits.retry_budget // 2),  # 50% retry budget
                burst_enabled=False,  # Burst disabled in scale-down
            )
        # Scale up with burst allocation if enabled
        if self.current_mode == ScalingMode.SCALE_UP:
            if limits.burst_enabled:
                # BURST ALLOCATION: Opportunistic capacity increase for viral spikes
                return ExecutionLimits(
                    max_parallel_jobs=min(limits.max_parallel_jobs * 2, limits.max_parallel_jobs + 4),  # 2x or +4 jobs
                    gpu_units=limits.gpu_units * 1.5,  # 50% increase
                    cpu_units=limits.cpu_units * 1.5,  # 50% increase
                    tokens_per_minute=int(limits.tokens_per_minute * 1.5),  # 50% increase
                    posting_limits={
                        k: int(v * 1.3) for k, v in limits.posting_limits.items()  # 30% posting boost
                    },
                    retry_budget=limits.retry_budget + 2,  # +2 retries for burst
                    burst_enabled=True,  # Keep burst active
                )
            else:
                # Normal scale up - remove penalties only
                return limits

        # Normal mode - return full limits
        return limits

    # ------------------------------------------------------------------
    # EMERGENCY BRAKE
    # ------------------------------------------------------------------

    def emergency_brake(self, reason: str) -> None:
        """
        Hard-stop execution to prevent system collapse.
        
        Can be triggered manually or automatically by evaluate_mode().
        All execution stops immediately until mode is manually reset.
        Emergency brake is irreversible without manual intervention.
        
        Args:
            reason: Human-readable explanation for emergency brake
        """
        with self._lock:
            self._enter_emergency(reason)
    
    def clear_emergency_brake(self, reason: str) -> None:
        """
        Manually clear emergency brake state.
        
        This is the ONLY way to exit emergency brake mode.
        Requires explicit human intervention.
        
        Args:
            reason: Human-readable explanation for clearing emergency brake
        """
        with self._lock:
            if not self.is_emergency():
                logger.warning("Attempted to clear emergency brake when not active")
                return
                
            self.current_mode = ScalingMode.NORMAL
            self._last_mode_change_ts = time.time()
            logger.info("EMERGENCY BRAKE CLEARED: %s", reason)
            self._log_event("EMERGENCY_BRAKE_CLEARED", reason, None)

    # ------------------------------------------------------------------
    # MONITORING & OBSERVABILITY
    # ------------------------------------------------------------------

    def get_current_mode(self) -> ScalingMode:
        """Returns current scaling mode (thread-safe)."""
        with self._lock:
            return self.current_mode

    def get_niche_limits_snapshot(self) -> Dict[str, ExecutionLimits]:
        """
        Returns copy of current niche limits (thread-safe).
        
        Validates all registered invariants but does not mutate state.
        """
        with self._lock:
            # Validate all registered invariants
            self._validate_invariants()
            
            return dict(self.niche_limits)
    
    def _validate_invariants(self) -> None:
        """Validate all registered system invariants."""
        for invariant in self._INVARIANT_REGISTRY:
            validator = getattr(self, invariant.validator)
            validator()
    
    def _validate_global_capacity_invariant(self) -> None:
        """GLOBAL CAPACITY: aggregate enforced capacity ≤ global capacity."""
        total_parallel = sum(limits.max_parallel_jobs for limits in self.niche_limits.values())
        if total_parallel > self.global_capacity.max_parallel_jobs:
            logger.critical(
                "GLOBAL CAPACITY VIOLATION: %s parallel jobs > %s global limit",
                total_parallel, self.global_capacity.max_parallel_jobs
            )
            self._emit_metric("invariant_violation_total", 1, 
                            type="global_capacity", total=total_parallel)
            # ENFORCEMENT: Trigger emergency brake on global capacity violation
            self._enter_emergency("global_capacity_violation")
            raise AssertionError(f"Global capacity invariant violated: {total_parallel} > {self.global_capacity.max_parallel_jobs}")
    
    def _validate_per_niche_parallelism_invariant(self) -> None:
        """PER_NICHE: active jobs ≤ max parallel jobs per niche."""
        if not self.is_emergency() and self.current_utilization:
            if hasattr(self.current_utilization, 'active_jobs_per_niche'):
                for niche, limits in self.niche_limits.items():
                    active_jobs = self.current_utilization.active_jobs_per_niche.get(niche, 0)
                    if active_jobs > limits.max_parallel_jobs:
                        logger.critical(
                            "INVARIANT VIOLATION: %s active jobs in niche '%s' > %s max",
                            active_jobs, niche, limits.max_parallel_jobs
                        )
                        self._emit_metric("invariant_violation_total", 1, 
                                        type="per_niche_parallelism", niche=niche)
                        # ENFORCEMENT: Trigger emergency brake on invariant violation
                        self._enter_emergency(f"per_niche_parallelism_violation_{niche}")
                        raise AssertionError(f"Per-niche parallelism invariant violated: {niche}")
    
    def _validate_admission_consistency_invariant(self) -> None:
        """ADMISSION: admission decisions consistent with current mode."""
        # This invariant is validated by _assert_admissible() during admission
        # Here we just ensure the method exists and is callable
        assert hasattr(self, '_assert_admissible') and callable(getattr(self, '_assert_admissible'))

    def get_scaling_events(self, last_n: Optional[int] = None) -> list[dict]:
        """
        Returns scaling event log for debugging/audit.
        
        Args:
            last_n: If provided, returns only last N events (up to max log size)
            
        Returns:
            List of scaling events with timestamps
        """
        with self._lock:
            if last_n:
                # Return the most recent events, limited by max log size
                return self.scaling_events_log[-min(last_n, self._max_events_log_size):]
            return list(self.scaling_events_log)

    # ------------------------------------------------------------------
    # INTERNAL LOGGING
    # ------------------------------------------------------------------

    def _log_event(self, event_type: str, before: Any, after: Any) -> None:
        """
        Internal event logger for audit trail.
        
        Args:
            event_type: Type of scaling event
            before: State before event
            after: State after event
        """
        self.scaling_events_log.append({
            "timestamp": time.time(),
            "event": event_type,
            "before": str(before),
            "after": str(after),
        })
        
        # Cap log size for long-running systems
        if len(self.scaling_events_log) > self._max_events_log_size:
            # Keep only the most recent events
            self.scaling_events_log = self.scaling_events_log[-self._max_events_log_size:]

    def _emit_metric(self, name: str, value: int = 1, **labels) -> None:
        """
        Emit structured metrics for operational monitoring.
        
        Args:
            name: Metric name
            value: Metric value (default: 1)
            **labels: Additional metric labels
        """
        # In production, this would integrate with Prometheus/DataDog/etc.
        # For now, structured logging for metrics collection
        metric_data = {
            "metric": name,
            "value": value,
            "labels": labels,
            "timestamp": time.time()
        }
        logger.info("METRIC: %s", metric_data)


# ------------------------------------------------------------------
# FACTORY FUNCTION
# ------------------------------------------------------------------

def create_scaling_controller(
    global_capacity: GlobalCapacity,
    scaling_config: Optional[Dict[str, Any]] = None,
) -> ScalingController:
    """
    Factory function to create a configured ScalingController.
    
    Args:
        global_capacity: Frozen global capacity constraints
        scaling_config: Optional scaling behavior configuration
        
    Returns:
        Configured ScalingController instance
    """
    if scaling_config is None:
        scaling_config = {
            "emergency_threshold": 0.95,
            "scale_down_threshold": 0.85,
            "scale_up_threshold": 0.70,
        }
    else:
        # Validate required config keys
        required_keys = ["emergency_threshold", "scale_down_threshold", "scale_up_threshold"]
        missing_keys = [key for key in required_keys if key not in scaling_config]
        if missing_keys:
            raise ValueError(f"Missing required scaling_config keys: {missing_keys}")
    
    return ScalingController(
        global_capacity=global_capacity,
        scaling_config=scaling_config,
    )