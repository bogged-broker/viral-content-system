"""
niche_router.py — Dynamic Niche Routing Engine

Determines where content, budget, and attention flow to maximize
guaranteed baseline virality (5M+) and enable 30M–300M+ clusters.

Location: /factories/niche_router.py
"""

from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
import numpy as np
import time
import hashlib
import json
from collections import defaultdict, deque


class RoutingAction(Enum):
    """Explicit routing actions for niche allocation."""
    SCALE_UP = "scale_up"
    MAINTAIN = "maintain"
    THROTTLE = "throttle"
    PAUSE = "pause"
    COOL_DOWN = "cool_down"  # New defensive state
    HARD_KILL = "hard_kill"   # New emergency stop


@dataclass
class NicheScore:
    """Complete scoring breakdown for a niche."""
    niche: str
    momentum: float
    saturation: float
    efficiency: float
    ceiling: float
    composite: float
    action: RoutingAction
    baseline_clearance_rate: float = 0.0
    dominance_score: float = 0.0
    time_in_top_position: int = 0
    concentration_violation: bool = False
    normalized_share: float = 0.0  # Share of total composite score
    budget_share: float = 0.0     # Final budget allocation after normalization
    decision_trace: Dict[str, Any] = field(default_factory=dict)  # NEW: Decision introspection
    raw_scores: Dict[str, float] = field(default_factory=dict)  # NEW: Raw component scores


class RoutingInvariantViolation(Exception):
    """Raised when a routing invariant is violated."""
    pass


@dataclass
class ExecutionStep:
    """Single execution step in the routing pipeline."""
    step_id: str
    step_name: str
    step_type: str  # 'validation', 'scoring', 'normalization', 'allocation'
    status: str  # 'pending', 'in_progress', 'completed', 'failed'
    start_time: float = 0.0
    end_time: float = 0.0
    duration_ms: float = 0.0
    input_data: Dict[str, Any] = field(default_factory=dict)
    output_data: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    metrics: Dict[str, float] = field(default_factory=dict)


@dataclass
class ExecutionPipeline:
    """Complete execution pipeline for routing decisions."""
    pipeline_id: str
    start_time: float
    end_time: float = 0.0
    total_duration_ms: float = 0.0
    status: str = "pending"  # 'pending', 'running', 'completed', 'failed'
    steps: List[ExecutionStep] = field(default_factory=list)
    final_output: Optional[Dict[str, Any]] = None
    error_summary: Dict[str, Any] = field(default_factory=dict)
    
    def add_step(self, step: ExecutionStep):
        """Add a step to the pipeline."""
        self.steps.append(step)
    
    def get_step_by_type(self, step_type: str) -> Optional[ExecutionStep]:
        """Get the first step of a specific type."""
        for step in self.steps:
            if step.step_type == step_type:
                return step
        return None
    
    def get_failed_steps(self) -> List[ExecutionStep]:
        """Get all failed steps."""
        return [step for step in self.steps if step.status == 'failed']
    
    def get_completion_rate(self) -> float:
        """Get pipeline completion rate."""
        if not self.steps:
            return 0.0
        completed = len([step for step in self.steps if step.status == 'completed'])
        return completed / len(self.steps)


@dataclass
class WorkflowStage:
    """Workflow stage for execution orchestration."""
    stage_id: str
    stage_name: str
    stage_type: str  # 'pre_routing', 'routing', 'post_routing', 'validation'
    dependencies: List[str] = field(default_factory=list)
    status: str = "pending"
    start_time: float = 0.0
    end_time: float = 0.0
    duration_ms: float = 0.0
    result: Any = None
    error: str = ""


@dataclass
class RoutingWorkflow:
    """Complete workflow for routing execution."""
    workflow_id: str
    pipeline: ExecutionPipeline
    stages: List[WorkflowStage] = field(default_factory=list)
    current_stage: str = ""
    status: str = "pending"
    start_time: float = 0.0
    end_time: float = 0.0
    total_duration_ms: float = 0.0
    
    def add_stage(self, stage: WorkflowStage):
        """Add a stage to the workflow."""
        self.stages.append(stage)
    
    def get_stage_by_id(self, stage_id: str) -> Optional[WorkflowStage]:
        """Get stage by ID."""
        for stage in self.stages:
            if stage.stage_id == stage_id:
                return stage
        return None
    
    def get_ready_stages(self) -> List[WorkflowStage]:
        """Get stages that are ready to execute (dependencies satisfied)."""
        ready_stages = []
        completed_stages = {s.stage_id for s in self.stages if s.status == 'completed'}
        
        for stage in self.stages:
            if stage.status == 'pending':
                dependencies_satisfied = all(dep in completed_stages for dep in stage.dependencies)
                if dependencies_satisfied:
                    ready_stages.append(stage)
        
        return ready_stages


@dataclass
class RoutingOutput:
    """Enhanced authoritative routing decision output contract."""
    snapshot_hash: str
    timestamp: float
    router_version: str
    niches: List[Dict[str, Any]]
    global_invariants_checked: List[str]
    hard_stops_triggered: bool
    state_deltas: Dict[str, Dict[str, Any]]
    summary: Dict[str, Any]
    
    # Enhanced output contract fields
    execution_metadata: Dict[str, Any] = field(default_factory=dict)
    budget_allocation: Dict[str, float] = field(default_factory=dict)
    risk_assessment: Dict[str, Any] = field(default_factory=dict)
    performance_forecast: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'snapshot_hash': self.snapshot_hash,
            'timestamp': self.timestamp,
            'router_version': self.router_version,
            'niches': self.niches,
            'global_invariants_checked': self.global_invariants_checked,
            'hard_stops_triggered': self.hard_stops_triggered,
            'state_deltas': self.state_deltas,
            'summary': self.summary,
            'execution_metadata': self.execution_metadata,
            'budget_allocation': self.budget_allocation,
            'risk_assessment': self.risk_assessment,
            'performance_forecast': self.performance_forecast
        }
    
    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=2)
    
    def validate_contract(self) -> List[str]:
        """Validate output contract completeness."""
        violations = []
        
        # Check required fields
        required_fields = ['snapshot_hash', 'timestamp', 'router_version', 'niches']
        for field in required_fields:
            if not getattr(self, field, None):
                violations.append(f"Missing required field: {field}")
        
        # Check niches structure
        if self.niches:
            for i, niche in enumerate(self.niches):
                if not isinstance(niche, dict):
                    violations.append(f"Niche {i} is not a dictionary")
                    continue
                
                required_niche_fields = ['niche', 'action', 'adjusted_score']
                for field in required_niche_fields:
                    if field not in niche:
                        violations.append(f"Niche {i} missing field: {field}")
        
        return violations


class NicheRouter:
    """
    Dynamically routes content, resources, and generation priority
    across niches based on real-time performance and future upside.
    
    Core Responsibility:
        Answer "Which niche should receive resources RIGHT NOW 
        to maximize guaranteed baseline virality?"
    
    Hard Constraints Enforced:
        1. No scaling without momentum (minimum momentum threshold)
        2. No budget without baseline (baseline clearance rate gating)
        3. No niche dominates forever (time-based decay and dominance caps)
        4. Concentration > diversification (explicit concentration limits)
    
    V2 Invariants & Assertions:
        This router is engineered to be non-breakable with strict invariants.
    """
    
    def __init__(
        self,
        niche_configs: Dict[str, Dict],
        factory_metrics: Dict[str, Dict],
        trend_signals: Dict[str, Dict],
        global_config: Dict[str, Any],
        niche_state: Dict[str, Dict] = None  # NEW: State memory for anti-dominance
    ):
        """
        Args:
            niche_configs: Loaded configs from /config/factories/*
            factory_metrics: Aggregated metrics per niche
            trend_signals: Real-time trend data across platforms
            global_config: Global routing thresholds and weights
            niche_state: State memory for anti-dominance and rotation
        """
        # INV-0: Router Must Be Deterministic per Snapshot
        self._input_snapshot_hash = self._compute_input_snapshot_hash(
            niche_configs, factory_metrics, trend_signals, global_config
        )
        
        self.niche_configs = niche_configs
        self.factory_metrics = factory_metrics
        self.trend_signals = trend_signals
        self.global_config = global_config
        
        # NEW: State memory for anti-dominance and rotation
        self.niche_state = niche_state or {}
        
        # Initialize state tracking for all niches (deterministic order)
        for niche in sorted(niche_configs.keys()):
            if niche not in self.niche_state:
                self.niche_state[niche] = {
                    'dominance_history': deque(maxlen=30),  # 30-day dominance tracking
                    'recent_scaling_exposure': deque(maxlen=7),  # 7-day scaling exposure
                    'cooldown_timer': 0,  # Days until can scale again
                    'last_scale_action': None,  # Last action taken
                    'consecutive_dominant_days': 0,  # Days in top position
                    'total_scaling_days': 0,  # Total days scaled this month
                    'performance_decay_rate': 0.0,  # Recent performance trend
                    'market_entry_date': 0,  # When niche entered system
                    'last_baseline_clearance': 0.0,  # Last baseline clearance rate
                    'momentum_inflection_points': deque(maxlen=10),  # Recent momentum changes
                    'saturation_kill_switch_active': False,  # Emergency saturation stop
                    'forced_rotation_cooldown': 0  # Days until can compete again
                }
        
        # Routing weights from global config
        self.momentum_weight = global_config.get('momentum_weight', 0.35)
        self.saturation_weight = global_config.get('saturation_weight', 0.25)
        self.marginal_efficiency_weight = global_config.get('marginal_efficiency_weight', 0.25)  # NEW: Replace efficiency_weight
        self.ceiling_weight = global_config.get('ceiling_weight', 0.15)
        self.dominance_decay_weight = global_config.get('dominance_decay_weight', 0.20)
        
        # Action thresholds
        self.scale_threshold = global_config.get('scale_threshold', 0.75)
        self.maintain_threshold = global_config.get('maintain_threshold', 0.50)
        self.throttle_threshold = global_config.get('throttle_threshold', 0.30)
        
        # Hard constraint thresholds
        self.min_momentum_for_scale = global_config.get('min_momentum_for_scale', 0.6)
        self.min_baseline_clearance = global_config.get('min_baseline_clearance', 0.4)
        self.max_dominance_share = global_config.get('max_dominance_share', 0.4)
        self.max_top_position_days = global_config.get('max_top_position_days', 14)
        self.max_concentration_ratio = global_config.get('max_concentration_ratio', 0.6)
        
        # Hard saturation enforcement thresholds
        self.saturation_hard_cap = global_config.get('saturation_hard_cap', 0.8)
        self.saturation_kill_threshold = global_config.get('saturation_kill_threshold', 0.9)
        self.saturation_cool_down_threshold = global_config.get('saturation_cool_down_threshold', 0.7)
        self.saturation_rate_penalty_threshold = global_config.get('saturation_rate_penalty_threshold', 0.1)
        self.max_cool_down_days = global_config.get('max_cool_down_days', 7)
        
        # Tracking for hard constraints
        self.niche_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=30))
        self.dominance_tracking: Dict[str, Dict] = defaultdict(dict)
        self.saturation_tracking: Dict[str, Dict] = defaultdict(dict)
        self.cool_down_tracking: Dict[str, int] = defaultdict(int)
        self.current_timestamp = int(time.time())
        self.last_dominance_check = self.current_timestamp
        
        # V2: Momentum State Tracking System
        self.momentum_state_tracking: Dict[str, Dict] = defaultdict(dict)
        for niche in sorted(niche_configs.keys()):
            self.momentum_state_tracking[niche] = {
                'descriptive_momentum': 0.0,  # Current performance level
                'predictive_momentum': 0.0,  # Future acceleration potential
                'momentum_velocity': 0.0,    # Rate of change
                'momentum_acceleration': 0.0,  # Acceleration rate
                'inflection_score': 0.0,       # Trend change detection
                'ignition_potential': 0.0,     # Early scaling opportunity
                'momentum_decay_rate': 0.0,    # Performance decline rate
                'last_inflection_timestamp': 0,  # When last trend change occurred
                'consecutive_accelerating_periods': 0,  # Streak of positive acceleration
                'peak_momentum_score': 0.0,   # Historical peak momentum
                'momentum_volatility': 0.0     # Momentum stability measure
            }
        
        # INV-1: Initialize routing consistency tracking
        self._routing_cache = {}
        self._invariant_violations = []
        
        # BASELINE SAFETY INVARIANTS - Critical System Guards
        self._enforce_baseline_safety_invariants()
    
    def _compute_input_snapshot_hash(self, niche_configs: Dict, factory_metrics: Dict, 
                                   trend_signals: Dict, global_config: Dict) -> str:
        """
        INV-0: Compute deterministic hash of input snapshot.
        
        Given identical inputs, routing decisions MUST be identical.
        """
        # Create deterministic snapshot
        snapshot = {
            'niche_configs': dict(sorted(niche_configs.items())),
            'factory_metrics': dict(sorted(factory_metrics.items())),
            'trend_signals': dict(sorted(trend_signals.items())),
            'global_config': dict(sorted(global_config.items()))
        }
        
        # Convert to JSON string with sorted keys
        snapshot_json = json.dumps(snapshot, sort_keys=True, default=str)
        
        # Compute SHA-256 hash
        return hashlib.sha256(snapshot_json.encode()).hexdigest()
    
    def _enforce_baseline_safety_invariants(self):
        """
        BASELINE SAFETY INVARIANTS (MOST IMPORTANT)
        
        These invariants protect against catastrophic routing failures.
        Any violation raises RoutingInvariantViolation immediately.
        """
        
        # INV-B1: No Empty Niches
        assert len(self.niche_configs) > 0, "INV-B1 VIOLATED: No niches configured"
        assert len(self.factory_metrics) > 0, "INV-B1 VIOLATED: No factory metrics provided"
        
        # INV-B2: Niche Config Consistency (deterministic order)
        for niche in sorted(self.niche_configs.keys()):
            assert niche in self.factory_metrics, f"INV-B2 VIOLATED: Niche {niche} missing from factory_metrics"
            assert isinstance(self.factory_metrics[niche], dict), f"INV-B2 VIOLATED: Invalid metrics for niche {niche}"
        
        # INV-B3: Threshold Sanity
        assert 0.0 <= self.min_baseline_clearance <= 1.0, "INV-B3 VIOLATED: Invalid baseline clearance threshold"
        assert 0.0 <= self.min_momentum_for_scale <= 1.0, "INV-B3 VIOLATED: Invalid momentum threshold"
        assert self.scale_threshold > self.maintain_threshold > self.throttle_threshold, "INV-B3 VIOLATED: Invalid threshold ordering"
        
        # INV-B4: Weight Normalization
        total_weight = (self.momentum_weight + self.saturation_weight + 
                       self.marginal_efficiency_weight + self.ceiling_weight)
        assert abs(total_weight - 1.0) < 0.01, f"INV-B4 VIOLATED: Weights sum to {total_weight}, expected 1.0"
        
        # INV-B5: Hard Constraint Bounds
        assert 0.0 <= self.max_dominance_share <= 1.0, "INV-B5 VIOLATED: Invalid dominance share bound"
        assert 0.0 <= self.max_concentration_ratio <= 1.0, "INV-B5 VIOLATED: Invalid concentration ratio bound"
        assert self.max_top_position_days > 0, "INV-B5 VIOLATED: Invalid top position days"
    
    def _check_deterministic_invariant(self, routing_output: Dict) -> bool:
        """
        INV-0: Router Must Be Deterministic per Snapshot
        
        Given identical inputs, routing decisions MUST be identical.
        """
        current_hash = self._compute_input_snapshot_hash(
            self.niche_configs, self.factory_metrics, self.trend_signals, self.global_config
        )
        
        # Check if we've seen this input snapshot before
        if current_hash in self._routing_cache:
            previous_output = self._routing_cache[current_hash]
            
            # Compare routing outputs
            if routing_output != previous_output:
                violation_msg = f"INV-0 VIOLATED: Non-deterministic routing for same input snapshot"
                self._invariant_violations.append({
                    'invariant': 'INV-0',
                    'timestamp': time.time(),  # Changed from 0 to time.time()
                    'message': violation_msg,
                    'input_hash': current_hash
                })
                raise RoutingInvariantViolation(violation_msg)
            
            return True
        
        # Cache this routing decision
        self._routing_cache[current_hash] = routing_output
        return False
    
    def _check_baseline_safety_invariants(self, niche_scores: List[NicheScore]):
        """
        BASELINE SAFETY INVARIANTS - Runtime checks on routing output.
        
        These invariants ensure the routing system cannot produce dangerous outputs.
        """
        
        # INV-B6: No Negative Scores
        for score in niche_scores:
            assert score.composite >= 0.0, f"INV-B6 VIOLATED: Negative composite score for {score.niche}"
            assert 0.0 <= score.momentum <= 1.0, f"INV-B6 VIOLATED: Invalid momentum range for {score.niche}"
            assert 0.0 <= score.saturation <= 1.0, f"INV-B6 VIOLATED: Invalid saturation range for {score.niche}"
            assert 0.0 <= score.efficiency <= 1.0, f"INV-B6 VIOLATED: Invalid efficiency range for {score.niche}"
            assert 0.0 <= score.ceiling <= 1.0, f"INV-B6 VIOLATED: Invalid ceiling range for {score.niche}"
        
        # INV-B7: Action Distribution Sanity
        actions = [s.action for s in niche_scores]
        scale_up_count = actions.count(RoutingAction.SCALE_UP)
        pause_count = actions.count(RoutingAction.PAUSE)
        
        # Should not scale up more than 50% of niches
        assert scale_up_count <= len(niche_scores) * 0.5, f"INV-B7 VIOLATED: Too many SCALE_UP actions ({scale_up_count})"
        
        # Should not pause more than 95% of niches (allow for poor baseline performance)
        # assert pause_count <= len(niche_scores) * 0.95, f"INV-B7 VIOLATED: Too many PAUSE actions ({pause_count})"
        
        # INV-B8: Baseline Gate Enforcement
        for score in niche_scores:
            if score.action == RoutingAction.SCALE_UP:
                # Any SCALE_UP must pass baseline gate
                assert self.passes_baseline_gate(score.niche), f"INV-B8 VIOLATED: SCALE_UP without baseline gate for {score.niche}"
                assert score.baseline_clearance_rate >= self.min_baseline_clearance, f"INV-B8 VIOLATED: SCALE_UP with insufficient baseline for {score.niche}"
                
                # INV-1: No Scaling Without Baseline Clearance (HARD GATE)
                # A niche that fails baseline clearance MUST NEVER receive SCALE_UP
                assert score.action != RoutingAction.SCALE_UP or self.passes_baseline_gate(score.niche), f"INV-1 VIOLATED: Niche {score.niche} failed baseline gate but received SCALE_UP - THIS IS HOW FACTORIES BLEED MONEY"
        
        # INV-B11: Baseline Regression Forces Downgrade
        for score in niche_scores:
            # Check if any niche is regressing but not downgraded
            baseline_clear_rate_delta, is_regressing = self.detect_baseline_regression(score.niche)
            
            if is_regressing:
                # INV-2: BASELINE REGRESSION FORCES DOWNGRADE
                # If baseline clearance is falling, scaling must be reversed
                assert score.action in [RoutingAction.THROTTLE, RoutingAction.PAUSE], f"INV-2 VIOLATED: Niche {score.niche} baseline regressing ({baseline_clear_rate_delta:.3f}) but not downgraded - PROTECTING FROM DECLINING PERFORMANCE"
        
        # INV-B9: Saturation Kill-Switch Enforcement
        for score in niche_scores:
            if self.should_force_throttle(score.niche):
                assert score.action in [RoutingAction.THROTTLE, RoutingAction.PAUSE], f"INV-B9 VIOLATED: Saturation kill-switch bypassed for {score.niche}"
        
        # INV-B10: No Dominance Monopoly
        total_composite = sum(s.composite for s in niche_scores)
        if total_composite > 0:
            for score in niche_scores:
                dominance_ratio = score.composite / total_composite
                assert dominance_ratio <= 0.6, f"INV-B10 VIOLATED: Niche {score.niche} dominates with {dominance_ratio:.2%} share"
    
    def _log_invariant_violation(self, invariant: str, message: str, niche: str = None):
        """
        Log invariant violations for orchestration monitoring.
        """
        violation = {
            'invariant': invariant,
            'timestamp': time.time(),
            'message': message,
            'niche': niche,
            'severity': 'CRITICAL'
        }
        
        self._invariant_violations.append(violation)
        
        # In production, this would surface to orchestration/monitoring
        print(f"CRITICAL INVARIANT VIOLATION: {invariant} - {message}")
        
        # Could also send to monitoring system
        # monitoring_system.alert(violation)
    
    def get_invariant_status(self) -> Dict:
        """
        Get current invariant status for monitoring.
        """
        return {
            'total_violations': len(self._invariant_violations),
            'recent_violations': [v for v in self._invariant_violations if time.time() - v['timestamp'] < 3600],
            'input_snapshot_hash': self._input_snapshot_hash,
            'routing_cache_size': len(self._routing_cache),
            'last_check': time.time()
        }
        
    def compute_niche_momentum(self, niche: str) -> float:
        """
        V2: Use new predictive momentum system - No longer routing based on raw averages.
        
        This method now delegates to compute_predictive_momentum which implements
        the V2 split momentum system with ignition detection and forward-looking analysis.
        
        Returns:
            Predictive momentum score (0-1, higher = stronger future momentum)
        """
        return self.compute_predictive_momentum(niche)
    
    def detect_momentum_ignition(self, niche: str) -> float:
        """
        V2: Early acceleration detection - Binary energy detection, not strength.
        
        This ONLY detects early acceleration using short-window deltas (1-24h).
        Focuses on velocity change, not absolute performance levels.
        
        Returns:
            Ignition score (0-1, higher = stronger early acceleration detected)
        """
        metrics = self.factory_metrics.get(niche, {})
        trends = self.trend_signals.get(niche, {})
        
        # Short-window deltas (1-24h focus)
        # Engagement velocity change (rate of change, not absolute)
        eng_velocity_1h = trends.get('engagement_velocity_1h', 0.0)
        eng_velocity_6h = trends.get('engagement_velocity_6h', 0.0)
        eng_velocity_24h = trends.get('engagement_velocity_24h', 0.0)
        
        # Calculate velocity changes (deltas)
        eng_velocity_change_1h = eng_velocity_1h - eng_velocity_6h
        eng_velocity_change_6h = eng_velocity_6h - eng_velocity_24h
        
        # Acceleration detection (change in velocity)
        engagement_acceleration = eng_velocity_change_1h - eng_velocity_change_6h
        
        # Baseline-clear rate change (not absolute %)
        baseline_rate_1h = trends.get('baseline_clearance_rate_1h', 0.0)
        baseline_rate_6h = trends.get('baseline_clearance_rate_6h', 0.0)
        baseline_rate_24h = trends.get('baseline_clearance_rate_24h', 0.0)
        
        # Baseline velocity changes
        baseline_velocity_change_1h = baseline_rate_1h - baseline_rate_6h
        baseline_velocity_change_6h = baseline_rate_6h - baseline_rate_24h
        
        # Baseline acceleration
        baseline_acceleration = baseline_velocity_change_1h - baseline_velocity_change_6h
        
        # Binary energy detection - focus on direction and magnitude of change
        engagement_ignition = 0.0
        baseline_ignition = 0.0
        
        # Engagement ignition detection
        if engagement_acceleration > 0:
            # Positive acceleration = ignition signal
            engagement_ignition = min(engagement_acceleration / 100.0, 1.0)  # Normalize to 0-1
        
        # Baseline ignition detection  
        if baseline_acceleration > 0:
            # Positive baseline acceleration = ignition signal
            baseline_ignition = min(baseline_acceleration * 10.0, 1.0)  # Amplify small changes
        
        # Cross-platform ignition detection
        platform_ignition = 0.0
        platforms = ['tiktok', 'youtube', 'instagram', 'twitter']
        accelerating_platforms = 0
        
        for platform in platforms:
            platform_velocity_1h = trends.get(f'{platform}_velocity_1h', 0.0)
            platform_velocity_6h = trends.get(f'{platform}_velocity_6h', 0.0)
            platform_acceleration = platform_velocity_1h - platform_velocity_6h
            
            if platform_acceleration > 0:
                accelerating_platforms += 1
                platform_ignition += min(platform_acceleration / 50.0, 0.25)  # Max 0.25 per platform
        
        # Market ignition signals (external validation)
        search_acceleration = trends.get('search_trend_acceleration_1h', 0.0)
        social_acceleration = trends.get('social_mentions_acceleration_1h', 0.0)
        market_ignition = min((search_acceleration + social_acceleration) / 2.0, 1.0)
        
        # Composite ignition score (binary energy detection)
        ignition_score = (
            0.35 * engagement_ignition +      # Primary ignition signal
            0.30 * baseline_ignition +       # Baseline clearance ignition
            0.20 * platform_ignition +       # Cross-platform validation
            0.15 * market_ignition           # External market validation
        )
        
        # Binary threshold - this is energy detection, not strength
        # Values below 0.3 are considered no ignition
        if ignition_score < 0.3:
            ignition_score = 0.0
        
        return min(max(ignition_score, 0.0), 1.0)
    
    def compute_predictive_momentum(self, niche: str) -> float:
        """
        V2: Replace old momentum logic - This becomes the only momentum used in routing.
        
        No longer routing based on raw averages - uses predictive signals only.
        Combines ignition detection with trend analysis for forward-looking momentum.
        
        Returns:
            Predictive momentum score (0-1, higher = stronger future momentum)
        """
        metrics = self.factory_metrics.get(niche, {})
        trends = self.trend_signals.get(niche, {})
        state = self.momentum_state_tracking[niche]
        
        # Get ignition signal (early acceleration detection)
        ignition_score = self.detect_momentum_ignition(niche)
        
        # Trend trajectory analysis (forward-looking, not historical averages)
        # Short-term trend (1-6h)
        eng_velocity_1h = trends.get('engagement_velocity_1h', 0.0)
        eng_velocity_6h = trends.get('engagement_velocity_6h', 0.0)
        short_term_trend = (eng_velocity_1h - eng_velocity_6h) / max(abs(eng_velocity_6h), 1)
        
        # Medium-term trend (6-24h)
        eng_velocity_24h = trends.get('engagement_velocity_24h', 0.0)
        medium_term_trend = (eng_velocity_6h - eng_velocity_24h) / max(abs(eng_velocity_24h), 1)
        
        # Trend acceleration (is trend accelerating?)
        trend_acceleration = short_term_trend - medium_term_trend
        
        # Baseline trajectory (predictive, not historical)
        baseline_rate_1h = trends.get('baseline_clearance_rate_1h', 0.0)
        baseline_rate_6h = trends.get('baseline_clearance_rate_6h', 0.0)
        baseline_rate_24h = trends.get('baseline_clearance_rate_24h', 0.0)
        
        baseline_trajectory = (baseline_rate_1h - baseline_rate_24h) / max(abs(baseline_rate_24h), 0.01)
        baseline_trend_acceleration = (baseline_rate_1h - baseline_rate_6h) - (baseline_rate_6h - baseline_rate_24h)
        
        # Cross-platform momentum sync (predictive coordination)
        platforms = ['tiktok', 'youtube', 'instagram', 'twitter']
        platform_momentums = []
        platform_trends = []
        
        for platform in platforms:
            platform_velocity = trends.get(f'{platform}_velocity_1h', 0.0)
            platform_velocity_6h = trends.get(f'{platform}_velocity_6h', 0.0)
            platform_trend = (platform_velocity - platform_velocity_6h) / max(abs(platform_velocity_6h), 1)
            
            platform_momentums.append(platform_velocity)
            platform_trends.append(platform_trend)
        
        # Platform synchronization (how many platforms accelerating together)
        accelerating_platforms = sum(1 for trend in platform_trends if trend > 0)
        platform_sync_score = accelerating_platforms / len(platforms)
        
        # Average platform momentum (normalized)
        avg_platform_momentum = sum(platform_momentums) / len(platform_momentums)
        normalized_platform_momentum = min(avg_platform_momentum / 1000.0, 1.0)
        
        # Market leading indicators (predictive, not descriptive)
        search_trend_momentum = trends.get('search_trend_momentum_1h', 0.0)
        social_mentions_momentum = trends.get('social_mentions_momentum_1h', 0.0)
        competitor_activity = trends.get('competitor_activity_surge_1h', 0.0)
        
        market_momentum = (search_trend_momentum + social_mentions_momentum + competitor_activity) / 3
        normalized_market_momentum = min(market_momentum / 100.0, 1.0)
        
        # V2: Update state tracking
        state['momentum_velocity'] = short_term_trend
        state['momentum_acceleration'] = trend_acceleration
        state['predictive_momentum'] = 0.0  # Will be updated at end
        
        # Predictive momentum composition (forward-looking only)
        # Primary predictive signals (60% weight) - Future potential
        primary_predictive = (
            0.30 * ignition_score +                    # Early acceleration detection
            0.25 * min(max(trend_acceleration, 0), 1.0) +  # Trend acceleration
            0.20 * min(max(baseline_trend_acceleration, 0), 1.0) +  # Baseline trend acceleration
            0.15 * platform_sync_score +               # Cross-platform coordination
            0.10 * min(max(baseline_trajectory, 0), 1.0)   # Baseline trajectory
        )
        
        # Secondary predictive signals (25% weight) - Confirmation
        secondary_predictive = (
            0.40 * normalized_platform_momentum +      # Platform momentum
            0.35 * normalized_market_momentum +         # Market validation
            0.25 * min(max(short_term_trend, 0), 1.0)  # Short-term trend strength
        )
        
        # Tertiary predictive signals (15% weight) - Context
        tertiary_predictive = (
            0.50 * trends.get('trend_alignment_momentum', 0.5) +  # Trend alignment
            0.30 * trends.get('long_tail_growth_momentum', 0.5) +  # Long-tail growth
            0.20 * trends.get('content_freshness_momentum', 0.5)   # Content freshness
        )
        
        # Weighted predictive momentum (no historical averages)
        predictive_momentum = (
            0.60 * primary_predictive +      # Future potential (highest priority)
            0.25 * secondary_predictive +    # Market/platform confirmation
            0.15 * tertiary_predictive       # Supporting context
        )
        
        # V2: Store in state
        state['predictive_momentum'] = predictive_momentum
        
        # Apply non-linear scaling for decision sensitivity
        if predictive_momentum < 0.2:
            # Very low momentum: compress to reduce noise
            predictive_momentum = predictive_momentum * 0.8
        elif predictive_momentum < 0.5:
            # Low-mid momentum: enhance for decision-making
            predictive_momentum = 0.16 + (predictive_momentum - 0.2) * 1.2
        elif predictive_momentum < 0.8:
            # Mid-high momentum: normal scaling
            predictive_momentum = 0.52 + (predictive_momentum - 0.5) * 0.93
        else:
            # High momentum: prevent over-amplification
            predictive_momentum = 0.80 + (predictive_momentum - 0.8) * 0.67
        
        return min(max(predictive_momentum, 0.0), 1.0)
    
    def _detect_momentum_inflection(self, current: float, prior: float, older: float, 
                                   trend_7d: float, trend_30d: float) -> float:
        """
        Detect momentum inflection points (trend changes).
        
        Args:
            current: Current momentum value
            prior: Prior momentum value
            older: Older momentum value
            trend_7d: 7-day trend
            trend_30d: 30-day trend
            
        Returns:
            Inflection score (0-1, higher = stronger inflection)
        """
        # Calculate acceleration change
        if prior != older:
            acceleration_change_ratio = (current - prior) / (prior - older)
        else:
            acceleration_change_ratio = 0.0
        
        # Detect sign change in acceleration
        current_acceleration = current - prior
        prior_acceleration = prior - older
        
        # Inflection occurs when acceleration changes sign or magnitude significantly
        sign_change = (current_acceleration * prior_acceleration) < 0
        magnitude_change = abs(acceleration_change_ratio) > 0.5
        
        # Trend consistency check
        trend_consistency = abs(trend_7d - trend_30d) < 0.2
        
        # Combined inflection score
        inflection_score = 0.0
        if sign_change:
            inflection_score += 0.4  # Strong inflection signal
        if magnitude_change:
            inflection_score += 0.3  # Significant magnitude change
        if trend_consistency:
            inflection_score += 0.2  # Consistent trend change
        if abs(acceleration_change_ratio) > 1.0:
            inflection_score += 0.1  # Very strong acceleration change
        
        return min(inflection_score, 1.0)
    
    def compute_saturation_score(self, niche: str) -> float:
        """
        Advanced niche fatigue detection with multi-dimensional signals.
        
        Enhanced Signals:
            - CTR velocity (rate of decline, not just current vs prior)
            - Retention decay acceleration (second-order change)
            - Content efficiency decay (views per content trend)
            - Engagement quality degradation (comment/like ratios)
            - Market fatigue indicators (search trend decline)
            - RL reward velocity (rate of change, not just current)
            - Cross-platform saturation signals
            - Competitive density increase
        
        Returns:
            Float saturation score (0–1), higher = more saturated
        """
        metrics = self.factory_metrics.get(niche, {})
        trends = self.trend_signals.get(niche, {})
        
        # Enhanced CTR Analysis - Velocity and Acceleration
        ctr_current = metrics.get('ctr_7d', 0.05)
        ctr_prior = metrics.get('ctr_14d_7d', 0.05)
        ctr_baseline = metrics.get('ctr_30d_baseline', 0.06)
        
        # CTR decline velocity (rate of change)
        ctr_decline_velocity = max(0, (ctr_prior - ctr_current) / max(ctr_prior, 0.01))
        
        # CTR acceleration (how fast decline is speeding up)
        ctr_older = metrics.get('ctr_30d_21d', ctr_baseline)
        ctr_prior_velocity = max(0, (ctr_older - ctr_prior) / max(ctr_older, 0.01))
        ctr_acceleration = max(0, ctr_decline_velocity - ctr_prior_velocity)
        
        # Distance from baseline CTR
        ctr_baseline_gap = max(0, (ctr_baseline - ctr_current) / ctr_baseline)
        
        # Enhanced Retention Analysis
        retention_current = metrics.get('retention_7d', 0.5)
        retention_prior = metrics.get('retention_14d_7d', 0.5)
        retention_baseline = metrics.get('retention_30d_baseline', 0.6)
        
        # Retention decay velocity and acceleration
        retention_decline_velocity = max(0, (retention_prior - retention_current) / max(retention_prior, 0.01))
        retention_baseline_gap = max(0, (retention_baseline - retention_current) / retention_baseline)
        
        # Content Efficiency Analysis - Views per Content
        content_volume_current = metrics.get('content_volume_7d', 100)
        content_volume_prior = metrics.get('content_volume_14d_7d', 90)
        views_current = metrics.get('views_7d', 1_000_000)
        views_prior = metrics.get('views_14d_7d', 950_000)
        
        # Efficiency metrics
        efficiency_current = views_current / max(content_volume_current, 1)
        efficiency_prior = views_prior / max(content_volume_prior, 1)
        efficiency_decline = max(0, (efficiency_prior - efficiency_current) / max(efficiency_prior, 1))
        
        # Content volume growth vs views growth (original signal, enhanced)
        volume_growth = content_volume_current / max(content_volume_prior, 1)
        views_growth = views_current / max(views_prior, 1)
        divergence = max(0, volume_growth - views_growth) / max(volume_growth, 0.01)
        
        # Engagement Quality Analysis
        likes_current = metrics.get('likes_7d', 50_000)
        comments_current = metrics.get('comments_7d', 5_000)
        shares_current = metrics.get('shares_7d', 2_000)
        
        # Engagement ratios (quality indicators)
        like_ratio = likes_current / max(views_current, 1)
        comment_ratio = comments_current / max(views_current, 1)
        share_ratio = shares_current / max(views_current, 1)
        
        # Historical engagement quality for comparison
        like_ratio_prior = metrics.get('like_ratio_14d_7d', like_ratio)
        comment_ratio_prior = metrics.get('comment_ratio_14d_7d', comment_ratio)
        share_ratio_prior = metrics.get('share_ratio_14d_7d', share_ratio)
        
        # Engagement quality decay
        engagement_quality_decline = (
            max(0, (like_ratio_prior - like_ratio) / max(like_ratio_prior, 0.0001)) +
            max(0, (comment_ratio_prior - comment_ratio) / max(comment_ratio_prior, 0.000001)) +
            max(0, (share_ratio_prior - share_ratio) / max(share_ratio_prior, 0.000001))
        ) / 3
        
        # Market Fatigue Indicators
        search_trend_current = trends.get('search_trend_score', 0.5)
        search_trend_prior = trends.get('search_trend_score_prior', 0.6)
        search_trend_decline = max(0, (search_trend_prior - search_trend_current) / max(search_trend_prior, 0.01))
        
        # Social media trend velocity
        social_trend_velocity = trends.get('social_trend_velocity', 0.0)
        social_trend_decline = max(0, -social_trend_velocity)  # Negative velocity = decline
        
        # RL Reward Analysis (enhanced)
        reward_current = metrics.get('rl_reward_current', 0.1)
        reward_prior = metrics.get('rl_reward_prior', 0.15)
        reward_baseline = metrics.get('rl_reward_baseline', 0.2)
        
        # Reward velocity and decay
        reward_velocity = (reward_current - reward_prior) / max(reward_prior, 0.01)
        reward_decay = max(0, -reward_velocity)  # Negative velocity = decay
        reward_baseline_gap = max(0, (reward_baseline - reward_current) / reward_baseline)
        
        # Cross-Platform Portability - Can this niche succeed across platforms?
        cross_platform_score = trends.get('cross_platform_score', 0.5)
        cross_platform_trend = trends.get('cross_platform_trend', 0.0)
        adjusted_cross_platform = cross_platform_score * (1.0 + cross_platform_trend)
        
        # Advanced Competitive Intelligence Analysis
        competitor_count_current = trends.get('competitor_count', 10)
        competitor_count_prior = trends.get('competitor_count_prior', 8)
        competitor_growth_rate = (competitor_count_current - competitor_count_prior) / max(competitor_count_prior, 1)
        
        # Competitive Strength Analysis - Not just count, but quality
        avg_competitor_performance = trends.get('avg_competitor_performance', 0.5)  # 0-1 scale
        top_competitor_strength = trends.get('top_competitor_strength', 0.7)  # How strong is #1
        
        # Competitive density penalty with strength weighting
        base_competitor_penalty = 1.0 / max(1.0 + (competitor_count_current / 20.0), 0.1)
        strength_penalty = 1.0 - (avg_competitor_performance * 0.3)  # Stronger competitors = lower ceiling
        top_competitor_penalty = 1.0 - (top_competitor_strength * 0.2)  # Dominant #1 = lower ceiling
        
        # Growth penalty with acceleration detection
        growth_competitor_penalty = 1.0 / max(1.0 + (competitor_growth_rate * 2.0), 0.5)
        
        # Market Entry Difficulty - How hard is it to enter this niche?
        market_entry_barrier = trends.get('market_entry_barrier', 0.5)  # 0-1 scale
        entry_penalty = 1.0 - (market_entry_barrier * 0.15)  # High barriers = lower ceiling
        
        # Total competitive penalty with advanced factors
        competitive_penalty = (
            base_competitor_penalty * 
            growth_competitor_penalty * 
            strength_penalty * 
            top_competitor_penalty * 
            entry_penalty
        )
        
        # Advanced Composite Saturation Score
        # Weight components based on predictive power and importance
        
        # Primary fatigue signals (highest weight)
        primary_fatigue = (
            0.25 * ctr_decline_velocity +          # CTR decline rate
            0.20 * retention_decline_velocity +     # Retention decline rate  
            0.15 * efficiency_decline +            # Content efficiency decay
            0.15 * engagement_quality_decline +    # Engagement quality degradation
            0.15 * divergence +                    # Content/views divergence
            0.10 * reward_decay                    # RL reward decay
        )
        
        # Secondary fatigue signals (medium weight)
        secondary_fatigue = (
            0.30 * ctr_baseline_gap +              # Distance from historical CTR
            0.25 * retention_baseline_gap +        # Distance from baseline retention
            0.20 * search_trend_decline +          # Market search decline
            0.15 * social_trend_decline +          # Social media trend decline
            0.10 * reward_baseline_gap             # Distance from reward baseline
        )
        
        # Tertiary fatigue signals (lower weight, early warning)
        tertiary_fatigue = (
            0.40 * ctr_acceleration +              # CTR decline acceleration (early warning)
            0.30 * adjusted_cross_platform +      # Cross-platform fatigue
            0.20 * competitor_growth_rate +        # Increased competition
            0.10 * 0.02                              # Platform performance divergence
        )
        
        # Weighted composite with dynamic adjustment
        saturation = (
            0.60 * primary_fatigue +      # Core fatigue signals
            0.30 * secondary_fatigue +    # Supporting indicators
            0.10 * tertiary_fatigue       # Early warning signals
        )
        
        # Apply saturation caps and non-linear scaling for better sensitivity
        # Enhanced sensitivity in mid-range (0.3-0.7) where decisions matter most
        if saturation < 0.3:
            # Low saturation: compress slightly to reduce noise
            saturation = saturation * 0.8
        elif saturation < 0.7:
            # Mid-range: enhance sensitivity for decision-making
            saturation = 0.24 + (saturation - 0.3) * 1.2
        else:
            # High saturation: ensure strong signal
            saturation = 0.72 + (saturation - 0.7) * 0.93
        
        return min(max(saturation, 0.0), 1.0)
    
    def compute_saturation_rate_of_increase(self, niche: str) -> float:
        """
        Calculate how fast saturation is increasing (rate-of-change).
        
        Args:
            niche: Niche name
            
        Returns:
            Float saturation rate of increase (0–1), higher = faster increase
        """
        history = self.saturation_tracking.get(niche, {})
        current_saturation = self.compute_saturation_score(niche)
        
        # Get previous saturation values
        prior_saturation = history.get('saturation_prior', current_saturation)
        older_saturation = history.get('saturation_older', prior_saturation)
        
        # Calculate rate of increase
        if prior_saturation > 0:
            rate_of_increase = (current_saturation - prior_saturation) / prior_saturation
        else:
            rate_of_increase = 0.0
            
        # Calculate acceleration of saturation increase
        prior_rate = (prior_saturation - older_saturation) / max(older_saturation, 0.01)
        acceleration = rate_of_increase - prior_rate
        
        # Return combined rate and acceleration signal
        return min(max(rate_of_increase + max(0, acceleration), 0.0), 1.0)
    
    def check_saturation_kill_conditions(self, niche: str, saturation: float, 
                                       saturation_rate: float) -> Tuple[bool, RoutingAction]:
        """
        Check if niche triggers hard saturation kill-switches.
        
        Args:
            niche: Niche name
            saturation: Current saturation score
            saturation_rate: Rate of saturation increase
            
        Returns:
            Tuple of (should_kill, action)
        """
        # Hard Kill Condition 1: Saturation exceeds kill threshold
        if saturation >= self.saturation_kill_threshold:
            return True, RoutingAction.HARD_KILL
        
        # Hard Kill Condition 2: Rapid saturation acceleration
        if saturation_rate > self.saturation_rate_penalty_threshold * 3:  # 3x penalty threshold
            return True, RoutingAction.HARD_KILL
        
        # Hard Kill Condition 3: Saturation hard cap exceeded
        if saturation >= self.saturation_hard_cap:
            return True, RoutingAction.HARD_KILL
        
        return False, None
    
    def check_cool_down_conditions(self, niche: str, saturation: float, 
                                 saturation_rate: float) -> Tuple[bool, int]:
        """
        Check if niche should enter cool-down state.
        
        Args:
            niche: Niche name
            saturation: Current saturation score
            saturation_rate: Rate of saturation increase
            
        Returns:
            Tuple of (should_cool_down, cool_down_days)
        """
        # Cool-down Condition 1: High saturation with rapid increase
        if saturation >= self.saturation_cool_down_threshold and \
           saturation_rate > self.saturation_rate_penalty_threshold:
            cool_down_days = min(int(saturation_rate * 10), self.max_cool_down_days)
            return True, cool_down_days
        
        # Cool-down Condition 2: Sustained high saturation
        if saturation >= self.saturation_hard_cap * 0.9:  # 90% of hard cap
            return True, self.max_cool_down_days // 2  # Half max cool-down
        
        return False, 0
    
    def apply_saturation_enforcement(self, niche: str, score: float, momentum: float, 
                                   efficiency: float, saturation: float) -> Tuple[float, RoutingAction]:
        """
        Apply hard saturation enforcement with kill-switches and penalties.
        
        Args:
            niche: Niche name
            score: Raw composite score
            momentum: Momentum score
            efficiency: Efficiency score
            saturation: Saturation score
            
        Returns:
            Tuple of (adjusted_score, final_action)
        """
        # Calculate saturation rate of increase
        saturation_rate = self.compute_saturation_rate_of_increase(niche)
        
        # Update saturation tracking
        self.saturation_tracking[niche] = {
            'saturation_current': saturation,
            'saturation_prior': self.saturation_tracking.get(niche, {}).get('saturation_current', saturation),
            'saturation_older': self.saturation_tracking.get(niche, {}).get('saturation_prior', saturation),
            'rate_of_increase': saturation_rate,
            'last_updated': self.current_timestamp
        }
        
        # Check cool-down status
        cool_down_days_remaining = self.cool_down_tracking.get(niche, 0)
        if cool_down_days_remaining > 0:
            # Still in cool-down - force COOL_DOWN action
            self.cool_down_tracking[niche] = cool_down_days_remaining - 1
            return 0.0, RoutingAction.COOL_DOWN
        
        # Check hard kill conditions
        should_kill, kill_action = self.check_saturation_kill_conditions(niche, saturation, saturation_rate)
        if should_kill:
            return 0.0, kill_action
        
        # Check cool-down conditions
        should_cool_down, cool_down_days = self.check_cool_down_conditions(niche, saturation, saturation_rate)
        if should_cool_down:
            self.cool_down_tracking[niche] = cool_down_days
            return 0.0, RoutingAction.COOL_DOWN
        
        # Apply saturation rate penalties
        if saturation_rate > self.saturation_rate_penalty_threshold:
            # Penalty proportional to rate excess
            penalty_factor = 1.0 - ((saturation_rate - self.saturation_rate_penalty_threshold) * 2)
            penalty_factor = max(penalty_factor, 0.1)  # Minimum 10% of original score
            score *= penalty_factor
        
        # Apply saturation hard cap penalties
        if saturation >= self.saturation_hard_cap:
            # Force score below maintain threshold
            score = min(score, self.throttle_threshold - 0.01)
        
        # Prevent scaling if saturation is too high, regardless of momentum/efficiency
        if saturation >= self.saturation_cool_down_threshold and score >= self.scale_threshold:
            score = self.maintain_threshold - 0.01  # Force to maintain/throttle
        
        # Determine final action based on saturation-adjusted score
        if score >= self.scale_threshold:
            action = RoutingAction.SCALE_UP
        elif score >= self.maintain_threshold:
            action = RoutingAction.MAINTAIN
        elif score >= self.throttle_threshold:
            action = RoutingAction.THROTTLE
        else:
            action = RoutingAction.PAUSE
        
        return score, action
    
    def apply_hard_constraints(self, niche: str, score: float, momentum: float, 
                             all_scores: List[NicheScore]) -> Tuple[float, RoutingAction]:
        """
        Apply all hard constraints with enhanced baseline gates and enforcement.
        
        Args:
            niche: Niche name
            score: Raw composite score
            momentum: Momentum score
            all_scores: All niche scores for comparison
            
        Returns:
            Tuple of (adjusted_score, final_action)
        """
        baseline_clearance = self.get_baseline_clearance_rate(niche)
        dominance = self.compute_dominance_score(niche, all_scores)
        time_on_top = self.get_time_in_top_position(niche)
        concentration_violation = self.check_concentration_violation(niche, all_scores)
        
        # Calculate saturation for enforcement
        saturation = self.compute_saturation_score(niche)
        marginal_efficiency = self.compute_marginal_efficiency(niche)  # NEW: Use marginal efficiency
        
        # Apply hard saturation enforcement FIRST (most critical)
        score, saturation_action = self.apply_saturation_enforcement(niche, score, momentum, marginal_efficiency, saturation)
        
        # If saturation enforcement already decided, return that action
        if saturation_action in [RoutingAction.HARD_KILL, RoutingAction.COOL_DOWN]:
            return score, saturation_action
        
        # SATURATION KILL-SWITCH - BINARY ENFORCEMENT BEFORE COMPOSITE SCORING
        if self.should_force_throttle(niche):
            return score, RoutingAction.THROTTLE  # Forced throttling, no exceptions
        
        # HARD BASELINE GATE - NON-NEGOTIABLE ENFORCEMENT
        if not self.passes_baseline_gate(niche):
            return score, RoutingAction.PAUSE  # NO EXCEPTIONS
        
        # Hard Constraint 1: No scaling without momentum
        if momentum < self.min_momentum_for_scale and score >= self.scale_threshold:
            score = self.maintain_threshold - 0.01  # Force to maintain/throttle
        
        # Hard Constraint 3: No niche dominates forever (ENHANCED WITH ROTATION LOGIC)
        # Apply comprehensive anti-dominance mechanisms
        dominance_penalty = self.apply_anti_dominance_logic(niche, score, dominance, time_on_top, all_scores)
        score *= dominance_penalty
        
        # DOMINANCE DECAY / ROTATION LOGIC - CRITICAL MISSING COMPONENT
        score, dominance_action = self._apply_dominance_decay_rotation(niche, score, dominance, time_on_top)
        
        # If dominance decay already decided, return that action
        if dominance_action in [RoutingAction.HARD_KILL, RoutingAction.PAUSE]:
            return score, dominance_action
        
        # Hard Constraint 4: Concentration > diversification
        if concentration_violation:
            # Reduce score to prevent over-concentration
            score *= 0.6
            if score >= self.scale_threshold:
                score = self.maintain_threshold - 0.01
        
        # Additional Saturation Constraint: Prevent over-exploitation
        # This catches cases where momentum/efficiency are high but saturation is rising
        if saturation >= self.saturation_cool_down_threshold * 0.8:  # 80% of cool-down threshold
            # Apply additional penalty for high saturation even if other metrics are good
            saturation_penalty = 1.0 - (saturation - self.saturation_cool_down_threshold * 0.8)
            saturation_penalty = max(saturation_penalty, 0.3)  # Minimum 30%
            score *= saturation_penalty
            
            # Force lower action if still trying to scale
            if score >= self.scale_threshold:
                score = self.maintain_threshold - 0.01
        
        # Determine final action based on constrained score
        # FINAL BASELINE GATE CHECK - NON-NEGOTIABLE
        if not self.passes_baseline_gate(niche):
            return score, RoutingAction.PAUSE  # NO EXCEPTIONS
            
        # DOMINANCE DECAY ENFORCEMENT - Force rotation
        dominance_decay = self.compute_dominance_decay(niche)
        
        # Apply dominance decay penalty directly to score (HARD subtraction)
        score -= dominance_decay
        
        # Force action downgrade if dominance decay crosses threshold
        if dominance_decay > 0.5:  # 50% decay threshold
            if score >= self.scale_threshold:
                # Force downgrade from SCALE_UP
                if dominance_decay > 0.7:
                    score = self.throttle_threshold - 0.01  # Force to THROTTLE
                    return score, RoutingAction.THROTTLE
                else:
                    score = self.maintain_threshold - 0.01  # Force to MAINTAIN
                    return score, RoutingAction.MAINTAIN
            elif score >= self.maintain_threshold:
                # Force downgrade from MAINTAIN to THROTTLE
                score = self.throttle_threshold - 0.01
                return score, RoutingAction.THROTTLE
            
        if score >= self.scale_threshold:
            action = RoutingAction.SCALE_UP
        elif score >= self.maintain_threshold:
            action = RoutingAction.MAINTAIN
        elif score >= self.throttle_threshold:
            action = RoutingAction.THROTTLE
        else:
            action = RoutingAction.PAUSE
        
        return score, action
    
    def _apply_hard_baseline_gates(self, niche: str, score: float, baseline_clearance: float,
                                   momentum: float, efficiency: float) -> Tuple[float, RoutingAction]:
        """
        Apply hard baseline gates with multiple enforcement layers.
        
        Args:
            niche: Niche name
            score: Current composite score
            baseline_clearance: Baseline clearance rate (0-1)
            momentum: Current momentum score
            efficiency: Current efficiency score
            
        Returns:
            Tuple of (adjusted_score, action)
        """
        # GATE 1: Absolute Minimum Baseline (Hard Stop)
        if baseline_clearance < self.min_baseline_clearance * 0.5:  # 20% baseline = hard stop
            # No exceptions - immediate hard kill regardless of other metrics
            return 0.0, RoutingAction.HARD_KILL
        
        # GATE 2: Minimum Baseline for Any Action
        if baseline_clearance < self.min_baseline_clearance:  # 40% baseline = no action
            # Force to pause regardless of momentum/efficiency
            return self.throttle_threshold - 0.01, RoutingAction.PAUSE
        
        # GATE 3: Scaling Baseline Gate (Higher Threshold for Scaling)
        scaling_baseline_threshold = self.min_baseline_clearance * 1.25  # 50% baseline for scaling
        if baseline_clearance < scaling_baseline_threshold and score >= self.scale_threshold:
            # Good baseline but not good enough for scaling
            score = self.maintain_threshold - 0.01
            return score, RoutingAction.MAINTAIN
        
        # GATE 4: Performance-Adjusted Baseline Gates
        # Allow lower baseline if momentum and efficiency are exceptional
        if baseline_clearance < self.min_baseline_clearance * 0.75:  # 30% baseline
            if momentum > 0.8 and efficiency > 0.8:
                # Exceptional performance - allow maintain but not scale
                if score >= self.scale_threshold:
                    score = self.maintain_threshold - 0.01
                return score, RoutingAction.MAINTAIN
            else:
                # Not exceptional enough - force pause
                return self.throttle_threshold - 0.01, RoutingAction.PAUSE
        
        # GATE 5: Trend-Based Baseline Adjustment
        baseline_trend = self._calculate_baseline_trend(niche)
        if baseline_trend < -0.2:  # Declining baseline trend
            # Apply stricter baseline requirements for declining trends
            adjusted_threshold = self.min_baseline_clearance * 1.1  # 44% baseline required
            if baseline_clearance < adjusted_threshold and score >= self.maintain_threshold:
                score = self.throttle_threshold - 0.01
                return score, RoutingAction.THROTTLE
        
        return score, None  # No baseline gate action taken
    
    def _apply_dominance_decay_rotation(self, niche: str, score: float, dominance: float,
                                       time_on_top: int) -> Tuple[float, RoutingAction]:
        """
        Apply dominance decay and rotation logic with progressive enforcement.
        
        Args:
            niche: Niche name
            score: Current composite score
            dominance: Dominance score (0-1)
            time_on_top: Days in #1 position
            
        Returns:
            Tuple of (adjusted_score, action)
        """
        # ROTATION LOGIC 1: Time-Based Decay
        if time_on_top > self.max_top_position_days:
            # Exceeded maximum time on top - force rotation
            decay_factor = 0.5  # 50% reduction
            score *= decay_factor
            
            # Force action reduction
            if score >= self.maintain_threshold:
                score = self.throttle_threshold - 0.01
                return score, RoutingAction.THROTTLE
        
        # ROTATION LOGIC 2: Progressive Time Decay
        elif time_on_top > self.max_top_position_days * 0.7:  # 70% of max time
            # Progressive decay as we approach limit
            decay_progress = (time_on_top - (self.max_top_position_days * 0.7)) / (self.max_top_position_days * 0.3)
            decay_factor = 1.0 - (decay_progress * 0.3)  # Up to 30% reduction
            score *= decay_factor
        
        # ROTATION LOGIC 3: Dominance-Based Decay
        if dominance > self.max_dominance_share:
            # Exceeded dominance share - force rotation
            excess_dominance = dominance - self.max_dominance_share
            dominance_decay_factor = 1.0 - (excess_dominance * 2.0)  # 2x penalty for excess
            dominance_decay_factor = max(dominance_decay_factor, 0.3)  # Minimum 30%
            score *= dominance_decay_factor
            
            # Force action reduction for high dominance
            if dominance > self.max_dominance_share * 1.5:  # 60%+ dominance
                score = self.throttle_threshold - 0.01
                return score, RoutingAction.THROTTLE
            elif score >= self.scale_threshold:
                score = self.maintain_threshold - 0.01
                return score, RoutingAction.MAINTAIN
        
        # ROTATION LOGIC 4: Combined Time + Dominance Enforcement
        if time_on_top > self.max_top_position_days * 0.5 and dominance > self.max_dominance_share * 0.8:
            # Combined violation - stronger enforcement
            combined_decay_factor = 0.7  # 30% reduction
            score *= combined_decay_factor
            
            if score >= self.scale_threshold:
                score = self.maintain_threshold - 0.01
                return score, RoutingAction.MAINTAIN
        
        return score, None  # No rotation action taken
    
    def apply_anti_dominance_logic(self, niche: str, score: float, dominance: float, 
                                 time_on_top: int, all_scores: List[NicheScore]) -> float:
        """
        Apply goal-oriented anti-dominance logic optimized for 5M+ baseline and 30M-300M+ targets.
        
        This is critical for 300M+ systems to ensure:
        - 5M+ baseline views consistently achieved and exceeded
        - 30M-300M+ targets are easily repeatable
        - No niche can stay #1 forever (but success is aggressively rewarded)
        - Fresh oxygen for new opportunities (when they can deliver results)
        - Long-tail expansion (focused on high-potential niches)
        - Compounding discovery (intelligent rotation toward goals)
        
        GOAL-ORIENTED ENHANCEMENTS:
        - Baseline Achievement Priority: Heavily favor niches hitting 5M+ baseline
        - Scale-Ready Optimization: Prioritize niches with 30M-300M+ potential
        - Performance-Driven Adaptation: Adjust based on actual goal achievement
        - Aggressive Success Preservation: Reward excellence that drives goals
        - Intelligent Rotation: Rotate only when it improves goal achievement
        
        Args:
            niche: Niche name
            score: Current composite score
            dominance: Dominance score (0-1)
            time_on_top: Days in #1 position
            all_scores: All niche scores for comparison
            
        Returns:
            Dominance penalty factor (0-1, lower = stronger penalty)
        """
        # GOAL-ORIENTED ANALYSIS
        baseline_performance = self._analyze_baseline_performance(niche)
        scale_potential = self._analyze_scale_potential(niche, all_scores)
        goal_achievement_rate = self._calculate_goal_achievement_rate(niche, all_scores)
        
        # MARKET CONTEXT ANALYSIS (enhanced for goals)
        market_health = self._analyze_market_health(all_scores)
        relative_performance = self._calculate_relative_performance(niche, score, all_scores)
        market_volatility = self._calculate_market_volatility(all_scores)
        
        # BASE ANTI-DOMINANCE FACTORS
        time_weighted_penalty = self.calculate_time_weighted_penalty(time_on_top)
        dominance_decay_penalty = self.calculate_dominance_decay_penalty(dominance)
        exposure_penalty = self.calculate_exposure_penalty(niche, all_scores)
        rotation_pressure = self.calculate_rotation_pressure(niche, score, all_scores)
        fresh_oxygen_bonus = self.calculate_fresh_oxygen_bonus(niche, all_scores)
        long_tail_bonus = self.calculate_long_tail_expansion_bonus(niche, all_scores)
        
        # GOAL-DRIVEN ADJUSTMENTS
        goal_adjusted_penalty = self._apply_goal_oriented_adjustments(
            time_weighted_penalty, dominance_decay_penalty, baseline_performance,
            scale_potential, goal_achievement_rate
        )
        
        # AGGRESSIVE SUCCESS PRESERVATION
        success_preservation_factor = self._calculate_aggressive_success_preservation(
            niche, score, dominance, all_scores, baseline_performance, scale_potential
        )
        
        # GOAL-OPTIMIZED COMPOSITE PENALTY
        base_penalty = (
            0.20 * goal_adjusted_penalty +        # Goal-driven time/dominance
            0.15 * exposure_penalty +              # Exposure balancing
            0.10 * rotation_pressure +              # Rotation pressure
            0.05 * (1.0 - fresh_oxygen_bonus) +   # Fresh oxygen (inverse)
            0.05 * (1.0 - long_tail_bonus) +      # Long-tail expansion (inverse)
            0.45 * success_preservation_factor      # ENHANCED: Aggressive success preservation
        )
        
        # GOAL-ORIENTED ADAPTIVE SCALING
        if baseline_performance > 0.8 and scale_potential > 0.7:
            # Strong baseline achiever + high scale potential = minimal penalties
            base_penalty = base_penalty * 1.3  # Reduce penalties by 30%
        elif baseline_performance > 0.6 and goal_achievement_rate > 0.8:
            # Good baseline performance = moderate penalty reduction
            base_penalty = base_penalty * 1.15  # Reduce penalties by 15%
        elif baseline_performance < 0.3 and scale_potential < 0.4:
            # Poor baseline performance + low scale potential = stronger penalties
            base_penalty = base_penalty * 0.7  # Increase penalties by 30%
        elif market_health > 0.8 and baseline_performance > 0.7:
            # Strong market + good baseline = allow more dominance for goal achievement
            if dominance < 0.6:  # But cap dominance
                base_penalty = min(base_penalty * 1.2, 1.0)  # Reduce penalties further
        
        return max(base_penalty, 0.02)  # Minimum 2% penalty (was 5%)
    
    def _analyze_baseline_performance(self, niche: str) -> float:
        """
        Analyze how well niche is achieving 5M+ baseline targets.
        
        Args:
            niche: Niche name
            
        Returns:
            Baseline performance score (0-1, higher = better baseline achievement)
        """
        metrics = self.factory_metrics.get(niche, {})
        
        # Baseline achievement metrics
        total_videos = metrics.get('total_videos', 1)
        baseline_clears = metrics.get('baseline_clears', 0)
        baseline_clearance_rate = baseline_clears / max(total_videos, 1)
        
        # Recent baseline performance (last 7 days)
        recent_videos_7d = metrics.get('videos_7d', 0)
        recent_baseline_clears_7d = metrics.get('baseline_clears_7d', 0)
        recent_baseline_rate = recent_baseline_clears_7d / max(recent_videos_7d, 1)
        
        # Baseline trend (improving or declining)
        baseline_trend = self._calculate_baseline_trend(niche)
        
        # 5M+ baseline achievement scoring
        baseline_achievement = (
            0.40 * min(baseline_clearance_rate * 2.5, 1.0) +  # Scale to 5M+ target (40% = 1M baseline)
            0.30 * min(recent_baseline_rate * 2.5, 1.0) +     # Recent performance
            0.20 * min(baseline_trend + 1.0, 2.0) / 2.0 +   # Trend (0-2 scaled to 0-1)
            0.10 * min(total_videos / 1000.0, 1.0)           # Volume (1000 videos = good baseline volume)
        )
        
        return min(max(baseline_achievement, 0.0), 1.0)
    
    def _analyze_scale_potential(self, niche: str, all_scores: List[NicheScore]) -> float:
        """
        Analyze niche's potential for 30M-300M+ scale targets.
        
        Args:
            niche: Niche name
            all_scores: All niche scores for comparison
            
        Returns:
            Scale potential score (0-1, higher = better scale potential)
        """
        niche_score_obj = next((s for s in all_scores if s.niche == niche), None)
        if not niche_score_obj:
            return 0.5
        
        metrics = self.factory_metrics.get(niche, {})
        trends = self.trend_signals.get(niche, {})
        
        # Scale potential indicators
        high_momentum = niche_score_obj.momentum > 0.6
        high_efficiency = niche_score_obj.efficiency > 0.6
        high_ceiling = niche_score_obj.ceiling > 0.7
        
        # Recent growth indicators
        recent_views_7d = metrics.get('views_7d', 0)
        recent_views_30d = metrics.get('views_30d', 0)
        growth_rate_7d = recent_views_7d / max(recent_views_30d / 4.3, 1)  # 30d -> 7d conversion
        
        # Market expansion indicators
        trend_breadth = trends.get('breadth_score', 0.5)
        cross_platform_score = trends.get('cross_platform_score', 0.5)
        market_growth = trends.get('market_growth_rate', 0.0)
        
        # Scale potential composite
        scale_potential = (
            0.25 * (1.0 if high_momentum else 0.3) +     # Momentum requirement
            0.25 * (1.0 if high_efficiency else 0.3) +     # Efficiency requirement
            0.20 * (1.0 if high_ceiling else 0.4) +        # Ceiling requirement
            0.15 * min(growth_rate_7d / 2.0, 1.0) +        # Growth rate (2x = good)
            0.10 * trend_breadth +                           # Market breadth
            0.05 * cross_platform_score                      # Cross-platform potential
        )
        
        return min(max(scale_potential, 0.0), 1.0)
    
    def _calculate_goal_achievement_rate(self, niche: str, all_scores: List[NicheScore]) -> float:
        """
        Calculate how well niche is achieving its goals (5M+ baseline, 30M-300M+ scale).
        
        Args:
            niche: Niche name
            all_scores: All niche scores for comparison
            
        Returns:
            Goal achievement rate (0-1, higher = better goal achievement)
        """
        baseline_perf = self._analyze_baseline_performance(niche)
        scale_potential = self._analyze_scale_potential(niche, all_scores)
        
        # Weighted goal achievement (baseline is primary, scale is secondary)
        goal_achievement = (
            0.60 * baseline_perf +     # 5M+ baseline achievement (primary goal)
            0.40 * scale_potential      # 30M-300M+ scale potential (secondary goal)
        )
        
        return min(max(goal_achievement, 0.0), 1.0)
    
    def _calculate_baseline_trend(self, niche: str) -> float:
        """
        Calculate baseline performance trend (-1 to +1).
        
        Args:
            niche: Niche name
            
        Returns:
            Baseline trend score (-1 to +1, positive = improving)
        """
        metrics = self.factory_metrics.get(niche, {})
        
        # Get baseline data for trend calculation
        baseline_7d = metrics.get('baseline_clears_7d', 0)
        baseline_14d = metrics.get('baseline_clears_14d_7d', 0)
        baseline_30d = metrics.get('baseline_clears_30d_7d', 0)
        
        videos_7d = metrics.get('videos_7d', 1)
        videos_14d = metrics.get('videos_14d_7d', 1)
        videos_30d = metrics.get('videos_30d', 1)
        
        # Calculate rates
        rate_7d = baseline_7d / max(videos_7d, 1)
        rate_14d = baseline_14d / max(videos_14d, 1)
        rate_30d = baseline_30d / max(videos_30d, 1)
        
        # Calculate trend (recent vs older)
        if rate_14d > 0 and rate_30d > 0:
            short_term_trend = (rate_7d - rate_14d) / rate_14d
            long_term_trend = (rate_14d - rate_30d) / rate_30d
            combined_trend = (short_term_trend + long_term_trend) / 2
        else:
            combined_trend = 0.0
        
        return min(max(combined_trend, -1.0), 1.0)
    
    def _apply_goal_oriented_adjustments(self, time_penalty: float, dominance_penalty: float,
                                           baseline_performance: float, scale_potential: float,
                                           goal_achievement_rate: float) -> float:
        """
        Apply goal-oriented adjustments to base penalties.
        
        Args:
            time_penalty: Base time-weighted penalty
            dominance_penalty: Base dominance penalty
            baseline_performance: Baseline achievement score
            scale_potential: Scale potential score
            goal_achievement_rate: Overall goal achievement rate
            
        Returns:
            Goal-adjusted penalty factor
        """
        base_penalty = (time_penalty + dominance_penalty) / 2
        
        # Baseline performance adjustment (primary goal)
        if baseline_performance > 0.8:
            # Excellent baseline achievement = reduce penalties
            baseline_adjustment = 1.0 - (0.3 * (baseline_performance - 0.8))
        elif baseline_performance < 0.4:
            # Poor baseline achievement = increase penalties
            baseline_adjustment = 1.0 + (0.4 * (0.4 - baseline_performance))
        else:
            baseline_adjustment = 1.0
        
        # Scale potential adjustment (secondary goal)
        if scale_potential > 0.7:
            # High scale potential = reduce penalties
            scale_adjustment = 1.0 - (0.2 * (scale_potential - 0.7))
        elif scale_potential < 0.3:
            # Low scale potential = increase penalties
            scale_adjustment = 1.0 + (0.3 * (0.3 - scale_potential))
        else:
            scale_adjustment = 1.0
        
        # Overall goal achievement adjustment
        if goal_achievement_rate > 0.8:
            # Strong goal achievement = significant penalty reduction
            goal_adjustment = 1.0 - (0.25 * (goal_achievement_rate - 0.8))
        elif goal_achievement_rate < 0.3:
            # Poor goal achievement = increase penalties
            goal_adjustment = 1.0 + (0.3 * (0.3 - goal_achievement_rate))
        else:
            goal_adjustment = 1.0
        
        # Combined goal-oriented adjustment
        adjusted_penalty = base_penalty * baseline_adjustment * scale_adjustment * goal_adjustment
        
        return min(max(adjusted_penalty, 0.0), 1.0)
    
    def _calculate_aggressive_success_preservation(self, niche: str, score: float, dominance: float,
                                                  all_scores: List[NicheScore], baseline_performance: float,
                                                  scale_potential: float) -> float:
        """
        Calculate aggressive success preservation factor focused on goal achievement.
        
        Args:
            niche: Niche name
            score: Current composite score
            dominance: Dominance score
            all_scores: All niche scores for comparison
            baseline_performance: Baseline achievement score
            scale_potential: Scale potential score
            
        Returns:
            Aggressive success preservation factor (0-1, higher = more preservation)
        """
        # Get niche's performance metrics
        niche_score_obj = next((s for s in all_scores if s.niche == niche), None)
        if not niche_score_obj:
            return 0.5
        
        # Goal-oriented excellence indicators
        excellent_baseline = baseline_performance > 0.8
        high_scale_potential = scale_potential > 0.7
        excellent_momentum = niche_score_obj.momentum > 0.7
        excellent_efficiency = niche_score_obj.efficiency > 0.7
        excellent_ceiling = niche_score_obj.ceiling > 0.8
        
        # Goal achievement leadership
        goal_achievement_rate = self._calculate_goal_achievement_rate(niche, all_scores)
        is_goal_leader = goal_achievement_rate > 0.8
        reasonable_dominance = dominance < 0.5  # Below 50% market share
        
        # AGGRESSIVE success preservation logic
        if excellent_baseline and high_scale_potential:
            # Excellent goal achiever = maximum preservation
            if reasonable_dominance and excellent_momentum:
                return 0.95  # 95% preservation (only 5% penalty)
            elif reasonable_dominance:
                return 0.90  # 90% preservation (10% penalty)
            else:
                return 0.80  # 80% preservation (20% penalty for too much dominance)
        
        elif is_goal_leader and high_scale_potential:
            # Strong goal achiever with scale potential
            return 0.85  # 85% preservation (15% penalty)
        
        elif excellent_baseline and excellent_momentum:
            # Good baseline achiever with momentum
            return 0.80  # 80% preservation (20% penalty)
        
        elif dominance > 0.6:
            # Too dominant regardless of performance
            return 0.3  # Only 30% preservation (70% penalty)
        
        elif baseline_performance > 0.6:
            # Good baseline achiever
            return 0.7  # 70% preservation (30% penalty)
        
        else:
            # Poor baseline performer = encourage rotation
            return 0.4  # 40% preservation (60% penalty)
    
    def _analyze_market_health(self, all_scores: List[NicheScore]) -> float:
        """
        Analyze overall market health based on all niche performance.
        
        Args:
            all_scores: All niche scores for analysis
            
        Returns:
            Market health score (0-1, higher = healthier market)
        """
        if not all_scores:
            return 0.5
        
        # Calculate market-wide performance metrics
        avg_momentum = np.mean([s.momentum for s in all_scores])
        avg_efficiency = np.mean([s.efficiency for s in all_scores])
        avg_ceiling = np.mean([s.ceiling for s in all_scores])
        
        # Calculate performance distribution (how many niches are performing well)
        high_performers = sum(1 for s in all_scores if s.composite > 0.6)
        performer_ratio = high_performers / len(all_scores)
        
        # Market health composite
        market_health = (
            0.40 * avg_momentum +      # Overall momentum
            0.30 * avg_efficiency +     # Overall efficiency  
            0.20 * avg_ceiling +        # Overall potential
            0.10 * performer_ratio     # Distribution of success
        )
        
        return min(max(market_health, 0.0), 1.0)
    
    def _calculate_relative_performance(self, niche: str, score: float, 
                                       all_scores: List[NicheScore]) -> float:
        """
        Calculate how well this niche performs relative to the market.
        
        Args:
            niche: Niche name
            score: Current composite score
            all_scores: All niche scores for comparison
            
        Returns:
            Relative performance score (0-1, higher = better relative performance)
        """
        if not all_scores:
            return 0.5
        
        # Get market statistics
        scores = [s.composite for s in all_scores]
        market_avg = np.mean(scores)
        market_std = np.std(scores)
        market_median = np.median(scores)
        
        # Calculate relative position
        if market_std > 0:
            z_score = (score - market_avg) / market_std
            relative_performance = 0.5 + (z_score * 0.15)  # Convert to 0-1 scale
        else:
            # No variation in market
            relative_performance = 0.5
        
        # Bonus for being above median
        if score > market_median:
            relative_performance += 0.1
        
        return min(max(relative_performance, 0.0), 1.0)
    
    def _calculate_market_volatility(self, all_scores: List[NicheScore]) -> float:
        """
        Calculate market volatility to adjust anti-dominance sensitivity.
        
        Args:
            all_scores: All niche scores for analysis
            
        Returns:
            Market volatility score (0-1, higher = more volatile)
        """
        if not all_scores:
            return 0.5
        
        scores = [s.composite for s in all_scores]
        
        # Calculate volatility metrics
        score_std = np.std(scores)
        score_range = max(scores) - min(scores)
        coefficient_of_variation = score_std / max(np.mean(scores), 0.01)
        
        # Momentum volatility
        momenta = [s.momentum for s in all_scores]
        momentum_std = np.std(momenta)
        
        # Composite volatility
        volatility = (
            0.40 * min(coefficient_of_variation, 1.0) +  # Score variation
            0.30 * min(score_std, 1.0) +                # Score spread
            0.30 * min(momentum_std, 1.0)                # Momentum variation
        )
        
        return min(volatility, 1.0)
    
    def _apply_market_context_adjustments(self, time_penalty: float, dominance_penalty: float,
                                         market_health: float, relative_performance: float,
                                         market_volatility: float) -> float:
        """
        Apply market-aware adjustments to base penalties.
        
        Args:
            time_penalty: Base time-weighted penalty
            dominance_penalty: Base dominance penalty
            market_health: Overall market health
            relative_performance: Niche's relative performance
            market_volatility: Market volatility level
            
        Returns:
            Market-adjusted penalty factor
        """
        base_penalty = (time_penalty + dominance_penalty) / 2
        
        # Market health adjustment
        if market_health > 0.7:
            # Strong market = reduce penalties for good performers
            health_adjustment = 1.0 + (0.2 * (market_health - 0.7))
        elif market_health < 0.3:
            # Weak market = increase penalties
            health_adjustment = 1.0 - (0.3 * (0.3 - market_health))
        else:
            health_adjustment = 1.0
        
        # Relative performance adjustment
        if relative_performance > 0.8:
            # Strong performer = reduce penalties
            performance_adjustment = 1.0 + (0.15 * (relative_performance - 0.8))
        elif relative_performance < 0.3:
            # Poor performer = increase penalties
            performance_adjustment = 1.0 - (0.2 * (0.3 - relative_performance))
        else:
            performance_adjustment = 1.0
        
        # Volatility adjustment (high volatility = more conservative penalties)
        if market_volatility > 0.6:
            volatility_adjustment = 0.9  # Reduce penalties in volatile markets
        elif market_volatility < 0.3:
            volatility_adjustment = 1.1  # Increase penalties in stable markets
        else:
            volatility_adjustment = 1.0
        
        # Combined adjustment
        adjusted_penalty = base_penalty * health_adjustment * performance_adjustment * volatility_adjustment
        
        return min(max(adjusted_penalty, 0.0), 1.0)
    
    def _calculate_success_preservation_factor(self, niche: str, score: float, dominance: float,
                                              all_scores: List[NicheScore], market_health: float) -> float:
        """
        Calculate factor to preserve genuine success while preventing dominance.
        
        Args:
            niche: Niche name
            score: Current composite score
            dominance: Dominance score
            all_scores: All niche scores for comparison
            market_health: Overall market health
            
        Returns:
            Success preservation factor (0-1, higher = more success preservation)
        """
        # Get niche's performance metrics
        niche_score_obj = next((s for s in all_scores if s.niche == niche), None)
        if not niche_score_obj:
            return 0.5
        
        # Excellence indicators
        excellent_momentum = niche_score_obj.momentum > 0.7
        excellent_efficiency = niche_score_obj.efficiency > 0.7
        excellent_ceiling = niche_score_obj.ceiling > 0.8
        
        # Market leadership (but not monopoly)
        is_top_performer = score > np.mean([s.composite for s in all_scores]) + np.std([s.composite for s in all_scores])
        reasonable_dominance = dominance < 0.4  # Below 40% market share
        
        # Success preservation logic
        if excellent_momentum and excellent_efficiency and excellent_ceiling:
            # Truly excellent performer = preserve success
            if reasonable_dominance and market_health > 0.5:
                return 0.9  # 90% preservation (only 10% penalty)
            elif market_health > 0.7:
                return 0.85  # 85% preservation in strong markets
            else:
                return 0.75  # 75% preservation in weaker markets
        
        elif is_top_performer and reasonable_dominance:
            # Good performer with reasonable market share
            return 0.8  # 80% preservation
        
        elif dominance > 0.5:
            # Too dominant regardless of performance
            return 0.4  # Only 40% preservation (60% penalty)
        
        else:
            # Average or below-average performer
            return 0.6  # 60% preservation (40% penalty)
    
    def calculate_time_weighted_penalty(self, time_on_top: int) -> float:
        """
        Calculate time-weighted penalty with exponential decay.
        
        Args:
            time_on_top: Days in #1 position
            
        Returns:
            Penalty factor (0-1, lower = stronger penalty)
        """
        if time_on_top <= 3:
            return 1.0  # No penalty for first 3 days
        
        # Exponential decay penalty
        # Day 4: 0.95, Day 7: 0.85, Day 14: 0.70, Day 21: 0.55, Day 28: 0.40
        decay_rate = 0.05  # 5% penalty per additional day
        max_penalty = 0.4   # Maximum 60% reduction
        
        penalty = 1.0 - (min((time_on_top - 3) * decay_rate, max_penalty))
        return max(penalty, 0.4)  # Minimum 40% of original score
    
    def calculate_dominance_decay_penalty(self, dominance: float) -> float:
        """
        Calculate dominance decay penalty based on resource share.
        
        Args:
            dominance: Dominance score (0-1)
            
        Returns:
            Penalty factor (0-1, lower = stronger penalty)
        """
        if dominance <= 0.2:
            return 1.0  # No penalty for low dominance
        
        # Progressive penalty for high dominance
        # 20% dominance: no penalty
        # 30% dominance: 0.95 penalty
        # 40% dominance: 0.85 penalty  
        # 50% dominance: 0.70 penalty
        # 60%+ dominance: 0.50 penalty
        
        if dominance <= 0.3:
            penalty = 1.0
        elif dominance <= 0.4:
            penalty = 0.95 - (dominance - 0.3) * 1.0  # Linear from 0.95 to 0.85
        elif dominance <= 0.5:
            penalty = 0.85 - (dominance - 0.4) * 1.5  # Linear from 0.85 to 0.70
        elif dominance <= 0.6:
            penalty = 0.70 - (dominance - 0.5) * 2.0  # Linear from 0.70 to 0.50
        else:
            penalty = 0.50  # Maximum penalty for 60%+ dominance
        
        return max(penalty, 0.5)  # Minimum 50% of original score
    
    def calculate_exposure_penalty(self, niche: str, all_scores: List[NicheScore]) -> float:
        """
        Calculate exposure penalty based on recent ranking frequency.
        
        Args:
            niche: Niche name
            all_scores: All niche scores for comparison
            
        Returns:
            Penalty factor (0-1, lower = stronger penalty)
        """
        # Get recent ranking history for this niche
        history = self.niche_history.get(niche, deque(maxlen=30))
        
        if len(history) < 7:  # Not enough history
            return 1.0
        
        # Calculate how often this niche appears in top 3
        recent_rankings = list(history)[-7:]  # Last 7 rankings
        top_3_count = sum(1 for record in recent_rankings if record.get('rank', 999) <= 3)
        
        # Penalty based on over-exposure
        # 0-1 times in top 3: no penalty
        # 2-3 times in top 3: 0.95 penalty
        # 4-5 times in top 3: 0.85 penalty
        # 6-7 times in top 3: 0.70 penalty
        
        if top_3_count <= 1:
            return 1.0
        elif top_3_count <= 3:
            return 0.95
        elif top_3_count <= 5:
            return 0.85
        else:
            return 0.70
    
    def calculate_rotation_pressure(self, niche: str, score: float, all_scores: List[NicheScore]) -> float:
        """
        Calculate rotation pressure for plateaued high performers.
        
        Args:
            niche: Niche name
            score: Current composite score
            all_scores: All niche scores for comparison
            
        Returns:
            Penalty factor (0-1, lower = stronger penalty)
        """
        # Get score history to detect plateauing
        history = self.niche_history.get(niche, deque(maxlen=30))
        
        if len(history) < 14:  # Not enough history for plateau detection
            return 1.0
        
        # Calculate score trend over last 14 days
        recent_scores = [record.get('score', 0) for record in list(history)[-14:]]
        
        if len(recent_scores) < 14:
            return 1.0
        
        # Calculate plateau detection metrics
        score_variance = np.var(recent_scores) if len(recent_scores) > 1 else 0
        score_trend = (recent_scores[-1] - recent_scores[0]) / max(recent_scores[0], 0.01)
        
        # High score + low variance + flat trend = plateau
        current_score_rank = next((i for i, s in enumerate(all_scores) if s.niche == niche), 999)
        
        if current_score_rank <= 2 and score > 0.6 and score_variance < 0.01 and abs(score_trend) < 0.05:
            # This niche is plateauing in top positions - apply rotation pressure
            return 0.80  # 20% penalty
        
        return 1.0
    
    def calculate_fresh_oxygen_bonus(self, niche: str, all_scores: List[NicheScore]) -> float:
        """
        Calculate fresh oxygen bonus for under-exposed promising niches.
        
        Args:
            niche: Niche name
            all_scores: All niche scores for comparison
            
        Returns:
            Bonus factor (1.0-1.2, higher = more bonus)
        """
        # Check if niche has good potential but low exposure
        niche_score = next((s for s in all_scores if s.niche == niche), None)
        if not niche_score:
            return 1.0
        
        # Good potential indicators
        good_momentum = niche_score.momentum > 0.5
        good_efficiency = niche_score.efficiency > 0.5
        good_ceiling = niche_score.ceiling > 0.6
        
        # Low exposure indicators
        history = self.niche_history.get(niche, deque(maxlen=30))
        recent_rankings = list(history)[-7:] if history else []
        avg_recent_rank = np.mean([record.get('rank', 999) for record in recent_rankings]) if recent_rankings else 999
        
        # Bonus for good potential with low exposure
        if good_momentum and good_efficiency and good_ceiling and avg_recent_rank > 5:
            return 1.15  # 15% bonus for fresh oxygen
        
        return 1.0
    
    def calculate_long_tail_expansion_bonus(self, niche: str, all_scores: List[NicheScore]) -> float:
        """
        Calculate long-tail expansion bonus for smaller niches with growth potential.
        
        Args:
            niche: Niche name
            all_scores: All niche scores for comparison
            
        Returns:
            Bonus factor (1.0-1.2, higher = more bonus)
        """
        # Check if niche has good potential but is smaller
        niche_score = next((s for s in all_scores if s.niche == niche), None)
        if not niche_score:
            return 1.0
        
        # Growth potential indicators
        good_momentum = niche_score.momentum > 0.4
        good_efficiency = niche_score.efficiency > 0.4
        improving_trend = niche_score.momentum > 0.3  # Some positive momentum
        
        # Identify smaller niches (bottom 50% by current score)
        sorted_scores = sorted(all_scores, key=lambda x: x.composite)
        mid_point = len(sorted_scores) // 2
        
        # Check if this is a smaller niche with growth potential
        niche_rank = next((i for i, s in enumerate(sorted_scores) if s.niche == niche), 999)
        
        if niche_rank > mid_point and good_momentum and improving_trend:
            return 1.10  # 10% bonus for long-tail expansion
        elif niche_rank > mid_point * 0.7 and good_momentum:
            return 1.05  # 5% bonus for mid-range niches with momentum
        
        return 1.0
    
    def get_time_in_top_position(self, niche: str) -> int:
        """
        Get how many consecutive days this niche has been in top position.
        
        Args:
            niche: Niche name
            
        Returns:
            Number of days in top position
        """
        history = self.niche_history.get(niche, deque(maxlen=30))
        if not history:
            return 0
        
        # Count consecutive days at #1 from most recent
        days_on_top = 0
        for record in reversed(history):
            # Handle both float values (dominance scores) and dict records
            if isinstance(record, dict):
                if record.get('rank') == 1:
                    days_on_top += 1
                else:
                    break
            elif isinstance(record, (float, int)):
                # If it's a dominance score, check if it's the highest (top position)
                if record > 0.8:  # High dominance score indicates top position
                    days_on_top += 1
                else:
                    break
            else:
                break
        
        return days_on_top
    
    def check_concentration_violation(self, niche: str, all_scores: List[NicheScore]) -> bool:
        """
        Check if allocating to this niche would violate concentration limits.
        
        Args:
            niche: Niche name
            all_scores: All niche scores for comparison
            
        Returns:
            True if concentration would be exceeded
        """
        # Get top 3 niches by score
        top_3 = sorted(all_scores, key=lambda x: x.composite, reverse=True)[:3]
        
        # Check if this niche is in top 3
        if not any(s.niche == niche for s in top_3):
            return False
        
        # Calculate concentration ratio (top 3 share)
        total_score = sum(s.composite for s in all_scores)
        top_3_score = sum(s.composite for s in top_3)
        concentration_ratio = top_3_score / max(total_score, 0.01)
        
        return concentration_ratio > self.max_concentration_ratio
    
    def compute_marginal_efficiency(self, niche: str) -> float:
        """
        V2: True marginal efficiency - Recent window, incremental analysis, opportunity cost.
        
        DANGEROUS: Old method used total lifetime views/cost (historical virals justify scaling)
        SAFE: This method uses ONLY recent performance and incremental analysis.
        
        Rules:
        - Only use last N videos (recent window)
        - Use incremental views vs incremental cost  
        - Compare against other niches (opportunity cost)
        - Past virals must NOT justify future scaling
        
        Args:
            niche: Niche name
            
        Returns:
            Marginal efficiency score (0-1, higher = better marginal ROI)
        """
        metrics = self.factory_metrics.get(niche, {})
        
        # RULE 1: Only use last N videos (recent window)
        recent_window_videos = 50  # Last 50 videos maximum
        recent_videos = min(metrics.get('videos_7d', 0), recent_window_videos)
        
        if recent_videos == 0:
            return 0.0  # No recent data
        
        # RULE 2: Use incremental views vs incremental cost
        # Get recent incremental data (not lifetime averages)
        recent_views = metrics.get('views_7d', 0)
        prior_views = metrics.get('views_14d_7d', recent_views)  # Prior period for comparison
        
        recent_cost = (
            metrics.get('compute_cost_current', 25.0) +
            metrics.get('generation_cost_current', 10.0) +
            metrics.get('posting_cost_current', 5.0)
        )
        
        prior_cost = (
            metrics.get('compute_cost_prior', 20.0) +
            metrics.get('generation_cost_prior', 8.0) +
            metrics.get('posting_cost_prior', 4.0)
        )
        
        # Incremental analysis (recent vs prior)
        incremental_views = recent_views - prior_views
        incremental_cost = recent_cost - prior_cost
        
        # Marginal ROI (incremental views per incremental cost)
        if incremental_cost > 0:
            marginal_roi = incremental_views / incremental_cost
        else:
            # If cost didn't increase, use recent performance
            marginal_roi = recent_views / max(recent_cost, 1.0)
        
        # RULE 3: Compare against other niches (opportunity cost)
        opportunity_cost_score = self._calculate_opportunity_cost(niche, marginal_roi)
        
        # RULE 4: Past virals must NOT justify future scaling
        # Apply historical viral discount
        historical_viral_videos = metrics.get('historical_viral_videos', 0)
        recent_viral_videos = metrics.get('viral_videos_7d', 0)
        
        # If most virals are historical, discount the efficiency
        if historical_viral_videos > 0:
            viral_recency_ratio = recent_viral_videos / historical_viral_videos
            historical_discount = min(viral_recency_ratio, 1.0)  # Max discount to 100%
        else:
            historical_discount = 1.0  # No historical virals, no discount
        
        # Apply historical discount to marginal ROI
        discounted_marginal_roi = marginal_roi * historical_discount
        
        # Normalize to 0-1 scale (100 views per dollar = excellent marginal efficiency)
        normalized_marginal_roi = min(discounted_marginal_roi / 100.0, 1.0)
        
        # Combine with opportunity cost
        final_marginal_efficiency = normalized_marginal_roi * opportunity_cost_score
        
        return min(max(final_marginal_efficiency, 0.0), 1.0)
    
    def _calculate_opportunity_cost(self, niche: str, marginal_roi: float) -> float:
        """
        Calculate opportunity cost score by comparing to other niches.
        
        Args:
            niche: Current niche
            marginal_roi: Current niche's marginal ROI
            
        Returns:
            Opportunity cost multiplier (0-2, higher = better than alternatives)
        """
        all_marginal_rois = []
        
        for other_niche in self.niche_configs.keys():
            if other_niche == niche:
                continue
                
            other_metrics = self.factory_metrics.get(other_niche, {})
            
            # Calculate other niche's marginal ROI using same logic
            other_recent_views = other_metrics.get('views_7d', 500_000)
            other_prior_views = other_metrics.get('views_14d_7d', other_recent_views)  # Prior period for comparison
            
            other_recent_cost = (
                other_metrics.get('compute_cost_current', 20.0) +
                other_metrics.get('generation_cost_current', 8.0) +
                other_metrics.get('posting_cost_current', 4.0)
            )
            
            other_prior_cost = (
                other_metrics.get('compute_cost_prior', 20.0) +
                other_metrics.get('generation_cost_prior', 8.0) +
                other_metrics.get('posting_cost_prior', 4.0)
            )
            
            other_incremental_views = other_recent_views - other_prior_views
            other_incremental_cost = other_recent_cost - other_prior_cost
            
            if other_incremental_cost > 0:
                other_marginal_roi = other_incremental_views / other_incremental_cost
            else:
                other_marginal_roi = other_recent_views / max(other_recent_cost, 1.0)
            
            all_marginal_rois.append(other_marginal_roi)
        
        if not all_marginal_rois:
            return 1.0
        
        # Calculate opportunity cost score
        avg_alternative_marginal_roi = sum(all_marginal_rois) / len(all_marginal_rois)
        
        if avg_alternative_marginal_roi > 0:
            opportunity_cost_ratio = marginal_roi / avg_alternative_marginal_roi
            # Cap at 2x better than average
            return min(opportunity_cost_ratio, 2.0)
        else:
            return 1.0

def check_concentration_violation(self, niche: str, all_scores: List[NicheScore]) -> bool:
    """
    Check if allocating to this niche would violate concentration limits.
    
    Args:
        niche: Niche name
        all_scores: All niche scores for comparison
        
    Returns:
        True if concentration would be exceeded
    """
    # Get top 3 niches by score
    top_3 = sorted(all_scores, key=lambda x: x.composite, reverse=True)[:3]
    
    # Check if this niche is in top 3
    if not any(s.niche == niche for s in top_3):
        return False
    
    # Calculate concentration ratio (top 3 share)
    total_score = sum(s.composite for s in all_scores)
    top_3_score = sum(s.composite for s in top_3)
    concentration_ratio = top_3_score / max(total_score, 0.01)
    
    return concentration_ratio > self.max_concentration_ratio

def compute_marginal_efficiency(self, niche: str) -> float:
    """
    V2: True marginal efficiency - Recent window, incremental analysis, opportunity cost.
    
    DANGEROUS: Old method used total lifetime views/cost (historical virals justify scaling)
    SAFE: This method uses ONLY recent performance and incremental analysis.
    
    Rules:
    - Only use last N videos (recent window)
    - Use incremental views vs incremental cost  
    - Compare against other niches (opportunity cost)
    - Past virals must NOT justify future scaling
    
    Args:
        niche: Niche name
        
    Returns:
        Marginal efficiency score (0-1, higher = better marginal ROI)
    """
    metrics = self.factory_metrics.get(niche, {})
    
    # RULE 1: Only use last N videos (recent window)
    recent_window_videos = 50  # Last 50 videos maximum
    recent_videos = min(metrics.get('videos_7d', 0), recent_window_videos)
    
    if recent_videos == 0:
        return 0.0  # No recent data
    
    # RULE 2: Use incremental views vs incremental cost
    # Get recent incremental data (not lifetime averages)
    recent_views = metrics.get('views_7d', 0)
    prior_views = metrics.get('views_14d_7d', recent_views)  # Prior period for comparison
    
    recent_cost = (
        metrics.get('compute_cost_current', 25.0) +
        metrics.get('generation_cost_current', 10.0) +
        metrics.get('posting_cost_current', 5.0)
    )
    
    prior_cost = (
        metrics.get('compute_cost_prior', 20.0) +
        metrics.get('generation_cost_prior', 8.0) +
        metrics.get('posting_cost_prior', 4.0)
    )
    
    # Incremental analysis (recent vs prior)
    incremental_views = recent_views - prior_views
    incremental_cost = recent_cost - prior_cost
    
    # Marginal ROI (incremental views per incremental cost)
    if incremental_cost > 0:
        marginal_roi = incremental_views / incremental_cost
    else:
        # If cost didn't increase, use recent performance
        marginal_roi = recent_views / max(recent_cost, 1.0)
    
    # RULE 3: Compare against other niches (opportunity cost)
    opportunity_cost_score = self._calculate_opportunity_cost(niche, marginal_roi)
    
    # RULE 4: Past virals must NOT justify future scaling
    # Apply historical viral discount
    historical_viral_videos = metrics.get('historical_viral_videos', 0)
    recent_viral_videos = metrics.get('viral_videos_7d', 0)
    
    # If most virals are historical, discount the efficiency
    if historical_viral_videos > 0:
        viral_recency_ratio = recent_viral_videos / historical_viral_videos
        historical_discount = min(viral_recency_ratio, 1.0)  # Max discount to 100%
    else:
        historical_discount = 1.0  # No historical virals, no discount
    
    # Apply historical discount to marginal ROI
    discounted_marginal_roi = marginal_roi * historical_discount
    
    # Normalize to 0-1 scale (100 views per dollar = excellent marginal efficiency)
    normalized_marginal_roi = min(discounted_marginal_roi / 100.0, 1.0)
    
    # Combine with opportunity cost
    final_marginal_efficiency = normalized_marginal_roi * opportunity_cost_score
    
    return min(max(final_marginal_efficiency, 0.0), 1.0)

def _calculate_opportunity_cost(self, niche: str, marginal_roi: float) -> float:
    """
    Calculate opportunity cost score by comparing to other niches.
    
    Args:
        niche: Current niche
        marginal_roi: Current niche's marginal ROI
        
    Returns:
        Opportunity cost multiplier (0-2, higher = better than alternatives)
    """
    all_marginal_rois = []
    
    for other_niche in self.niche_configs.keys():
        if other_niche == niche:
            continue
            
        other_metrics = self.factory_metrics.get(other_niche, {})
        
        # Calculate other niche's marginal ROI using same logic
        other_recent_views = other_metrics.get('views_7d', 500_000)
        other_prior_views = other_metrics.get('views_14d_7d', other_recent_views)  # Prior period for comparison
        
        other_recent_cost = (
            other_metrics.get('compute_cost_current', 20.0) +
            other_metrics.get('generation_cost_current', 8.0) +
            other_metrics.get('posting_cost_current', 4.0)
        )
        
        other_prior_cost = (
            other_metrics.get('compute_cost_prior', 20.0) +
            other_metrics.get('generation_cost_prior', 8.0) +
            other_metrics.get('posting_cost_prior', 4.0)
        )
        
        other_incremental_views = other_recent_views - other_prior_views
        other_incremental_cost = other_recent_cost - other_prior_cost
        
        if other_incremental_cost > 0:
            other_marginal_roi = other_incremental_views / other_incremental_cost
        else:
            other_marginal_roi = other_recent_views / max(other_recent_cost, 1.0)
        
        all_marginal_rois.append(other_marginal_roi)
    
    if not all_marginal_rois:
        return 1.0
    
    # Calculate opportunity cost score
    avg_alternative_marginal_roi = sum(all_marginal_rois) / len(all_marginal_rois)
    
    if avg_alternative_marginal_roi > 0:
        opportunity_cost_ratio = marginal_roi / avg_alternative_marginal_roi
        # Cap at 2x better than average
        return min(opportunity_cost_ratio, 2.0)
    else:
        return 1.0

def compute_capital_efficiency(self, niche: str) -> float:
    """
    Views produced per unit of:
        - compute
        - generation cost
        - posting bandwidth
    
    Blueprint Requirement: EXPLICIT views/dollar calculation
    Returns: Absolute efficiency score (0-1, higher = better)
    """
    metrics = self.factory_metrics.get(niche, {})
    
    # ABSOLUTE EFFICIENCY CALCULATION (Blueprint Requirement)
    # Total costs for absolute efficiency calculation
    compute_cost = metrics.get('compute_cost_current', 25.0)
    generation_cost = metrics.get('generation_cost_current', 10.0)
    bandwidth_cost = metrics.get('bandwidth_cost_current', 5.0)
    
    total_absolute_cost = compute_cost + generation_cost + bandwidth_cost
    
    # Views per absolute dollar (Blueprint Core Metric)
    views_current = metrics.get('views_7d', 1_000_000)
    absolute_efficiency = views_current / max(total_absolute_cost, 1.0)  # Views per dollar
    
    # Normalize to reasonable scale (1000 views per dollar = excellent)
    normalized_absolute_efficiency = min(absolute_efficiency / 1000.0, 1.0)
    
    return normalized_absolute_efficiency

def estimate_niche_ceiling(self, niche: str) -> float:
    """
    Estimates maximum upside potential of a niche.

    Blueprint Requirement: EXPLICIT single scalar ceiling score (0-1)
    Returns: Estimated max reachable views per video (normalized)
    """
    # Get probabilistic ceiling estimates from existing implementation
    ceiling_dict = self._estimate_niche_ceiling_detailed(niche)

    # Return P90 ceiling as the primary ceiling score (optimistic but realistic)
    # Normalized to 0-1 scale where 1.0 = 300M+ potential
    ceiling_score = ceiling_dict.get('p90_ceiling', 0.0)

    return ceiling_score

def _estimate_niche_ceiling_detailed(self, niche: str) -> Dict[str, float]:
    metrics = self.factory_metrics.get(niche, {})
    trends = self.trend_signals.get(niche, {})

    # Historical ceiling analysis with temporal decay
    historical_max = metrics.get('max_views_ever', 5_000_000)
    max_age_days = metrics.get('max_video_age_days', 180)  # How old is the historical max?

    # Temporal decay - older achievements less predictive
    temporal_decay_factor = max(0.1, 1.0 - (max_age_days / 365.0))  # Decay over 1 year
    decayed_historical_max = historical_max * temporal_decay_factor

    # Platform ceiling volatility - platforms have changing limits
    platform_ceiling_tiktok = trends.get('tiktok_ceiling_current', 100_000_000)
    platform_ceiling_youtube = trends.get('youtube_ceiling_current', 200_000_000)
    platform_ceiling_instagram = trends.get('instagram_ceiling_current', 50_000_000)
    platform_ceiling_twitter = trends.get('twitter_ceiling_current', 30_000_000)

    # Platform ceiling trends (are platforms getting more or less restrictive?)
    platform_trend_tiktok = trends.get('tiktok_ceiling_trend', 0.0)  # Positive = expanding
    platform_trend_youtube = trends.get('youtube_ceiling_trend', 0.0)
    platform_trend_instagram = trends.get('instagram_ceiling_trend', 0.0)
    platform_trend_twitter = trends.get('twitter_ceiling_trend', 0.0)

    # Adjusted platform ceilings with trends
    adjusted_tiktok = platform_ceiling_tiktok * (1.0 + platform_trend_tiktok)
    adjusted_youtube = platform_ceiling_youtube * (1.0 + platform_trend_youtube)
    adjusted_instagram = platform_ceiling_instagram * (1.0 + platform_trend_instagram)
    adjusted_twitter = platform_ceiling_twitter * (1.0 + platform_trend_twitter)

    # Maximum platform ceiling (best case scenario)
    max_platform_ceiling = max(adjusted_tiktok, adjusted_youtube, adjusted_instagram, adjusted_twitter)

    # Advanced Genre Fatigue Modeling - Audience Burnout Intelligence
    genre_fatigue_score = trends.get('genre_fatigue_score', 0.3)
    genre_fatigue_trend = trends.get('genre_fatigue_trend', 0.01)  # Positive = increasing fatigue

    # NEW: Audience Burnout Velocity Analysis
    fatigue_velocity = trends.get('genre_fatigue_velocity', 0.0)  # Rate of fatigue increase
    fatigue_acceleration = trends.get('genre_fatigue_acceleration', 0.0)  # Acceleration of burnout

    # NEW: Cross-Platform Fatigue Synchronization
    platform_fatigue_sync = trends.get('platform_fatigue_synchronization', 0.5)  # How synchronized is fatigue across platforms?
    fatigue_sync_penalty = 1.0 - (platform_fatigue_sync * 0.1)  # Low sync = diverse fatigue patterns

    # Fatigue penalty with advanced factors
    base_fatigue_penalty = 1.0 - (genre_fatigue_score * 0.5)  # Max 50% reduction
    fatigue_trend_penalty = max(0, 1.0 - (genre_fatigue_trend * 2.0))  # Trending fatigue reduces ceiling
    fatigue_velocity_penalty = max(0, 1.0 - (fatigue_velocity * 3.0))  # Rapid fatigue increase
    fatigue_acceleration_penalty = max(0, 1.0 - (fatigue_acceleration * 2.0))  # Accelerating fatigue

    # Total fatigue penalty with intelligence
    fatigue_penalty = (
        base_fatigue_penalty * 
        fatigue_trend_penalty * 
        fatigue_velocity_penalty * 
        fatigue_acceleration_penalty * 
        fatigue_sync_penalty
    )

    # Advanced Market Saturation Intelligence - Predictive Market Analysis
    market_saturation = trends.get('market_saturation_score', 0.4)
    market_saturation_trend = trends.get('market_saturation_trend', 0.02)

    # NEW: Market Saturation Velocity and Acceleration
    saturation_velocity = trends.get('market_saturation_velocity', 0.0)  # Rate of saturation increase
    saturation_acceleration = trends.get('market_saturation_acceleration', 0.0)  # Acceleration of saturation

    # NEW: Market Maturity Analysis - Where is this niche in its lifecycle?
    market_maturity = trends.get('market_maturity_score', 0.5)  # 0-1 scale (emerging to mature)
    market_momentum = trends.get('market_momentum', 0.0)  # Is market growing or declining?

    # NEW: Consumer Behavior Analysis - How are users engaging?
    consumer_engagement_trend = trends.get('consumer_engagement_trend', 0.0)  # Rising or falling engagement
    content_consumption_velocity = trends.get('content_consumption_velocity', 0.0)  # How fast is content being consumed

    # Trend breadth analysis - How wide is the appeal?
    trend_breadth = trends.get('breadth_score', 0.5)
    trend_breadth_trend = trends.get('breadth_trend', 0.0)  # Is appeal expanding or contracting?
    adjusted_breadth = trend_breadth * (1.0 + trend_breadth_trend)

    # Cross-Platform Portability - Can this niche succeed across platforms?
    cross_platform_score = trends.get('cross_platform_score', 0.5)
    cross_platform_trend = trends.get('cross_platform_trend', 0.0)
    adjusted_cross_platform = cross_platform_score * (1.0 + cross_platform_trend)

    # Diversification bonus - Multi-platform resilience
    diversification_bonus = trends.get('diversification_score', 0.5) * 0.1  # Multi-platform resilience bonus

    # Saturation penalty with predictive intelligence
    base_saturation_penalty = 1.0 - (market_saturation * 0.3)  # Max 30% reduction
    saturation_trend_penalty = max(0, 1.0 - (market_saturation_trend * 3.0))  # Trending saturation reduces ceiling
    saturation_velocity_penalty = max(0, 1.0 - (saturation_velocity * 4.0))  # Rapid saturation increase
    saturation_acceleration_penalty = max(0, 1.0 - (saturation_acceleration * 2.0))  # Accelerating saturation

    # Market maturity adjustments
    if market_maturity > 0.8:  # Mature market
        maturity_penalty = 0.85  # Mature markets have lower ceilings
    elif market_maturity < 0.3:  # Emerging market
        maturity_penalty = 1.15  # Emerging markets have higher potential
    else:  # Growth market
        maturity_penalty = 1.0  # Growth markets are neutral

    # Consumer behavior adjustments
    engagement_adjustment = 1.0 + (consumer_engagement_trend * 0.05)  # Rising engagement helps
    consumption_adjustment = 1.0 + (content_consumption_velocity * 0.03)  # Higher consumption helps

    # Market momentum adjustments
    momentum_adjustment = 1.0 + (market_momentum * 0.1)  # Growing markets get bonus

    # Total saturation penalty with intelligence
    saturation_penalty = (
        base_saturation_penalty * 
        saturation_trend_penalty * 
        saturation_velocity_penalty * 
        saturation_acceleration_penalty * 
        maturity_penalty *
        momentum_adjustment *
        engagement_adjustment *
        consumption_adjustment
    )

    # Content Lifecycle Ceiling - Age-based content performance
    avg_content_age = metrics.get('avg_content_age_days', 30)
    content_lifecycle_factor = max(0.3, 1.0 - (avg_content_age / 180.0))  # Decay over 6 months

    # Advanced Competitive Intelligence Analysis - Not just count, but quality
    competitor_count_current = trends.get('competitor_count', 10)
    competitor_count_prior = trends.get('competitor_count_prior', 8)
    competitor_growth_rate = (competitor_count_current - competitor_count_prior) / max(competitor_count_prior, 1)

    # Competitive Strength Analysis - Not just count, but quality
    avg_competitor_performance = trends.get('avg_competitor_performance', 0.5)  # 0-1 scale
    top_competitor_strength = trends.get('top_competitor_strength', 0.7)  # How strong is #1

    # Competitive density penalty with strength weighting
    base_competitor_penalty = 1.0 / max(1.0 + (competitor_count_current / 20.0), 0.1)
    strength_penalty = 1.0 - (avg_competitor_performance * 0.3)  # Stronger competitors = lower ceiling
    top_competitor_penalty = 1.0 - (top_competitor_strength * 0.2)  # Dominant #1 = lower ceiling

    # Growth penalty with acceleration detection
    growth_competitor_penalty = 1.0 / max(1.0 + (competitor_growth_rate * 2.0), 0.5)

    # Market Entry Difficulty - How hard is it to enter this niche?
    market_entry_barrier = trends.get('market_entry_barrier', 0.5)  # 0-1 scale
    entry_penalty = 1.0 - (market_entry_barrier * 0.15)  # High barriers = lower ceiling

    # Total competitive penalty with advanced factors
    competitive_penalty = (
        base_competitor_penalty * 
        growth_competitor_penalty * 
        strength_penalty * 
        top_competitor_penalty *
        entry_penalty
    )

    # Advanced Composite Ceiling Calculation
    # Weight components for realistic 30M-300M targeting

    # Primary ceiling factors (highest weight - core potential)
    primary_ceiling = (
        0.35 * min(decayed_historical_max / 300_000_000.0, 1.0) +  # Temporal-adjusted historical max
        0.25 * min(max_platform_ceiling / 300_000_000.0, 1.0) +   # Best platform ceiling
        0.20 * adjusted_breadth +                               # Trend breadth (market size)
        0.15 * adjusted_cross_platform +                         # Cross-platform potential
        0.05 * diversification_bonus                              # Multi-platform resilience
    )

    # Secondary ceiling factors (medium weight - market constraints)
    secondary_ceiling = (
        0.40 * competitive_penalty +                           # Competition density effects
        0.30 * fatigue_penalty +                               # Genre fatigue effects
        0.20 * saturation_penalty +                             # Market saturation constraints
        0.10 * content_lifecycle_factor                         # Content age effects
    )

    # Tertiary ceiling factors (lower weight - risk factors)
    tertiary_ceiling = (
        0.50 * trends.get('regulatory_risk_score', 0.8) +          # Regulatory constraints
        0.30 * trends.get('technology_disruption_risk', 0.7) +   # Technology disruption risk
        0.20 * trends.get('market_volatility_score', 0.6)        # Market volatility
    )

    # Weighted composite with defensive bias
    defensive_ceiling = (
        0.50 * primary_ceiling +      # Core potential (highest priority)
        0.35 * secondary_ceiling +    # Market constraints (critical for realism)
        0.15 * tertiary_ceiling       # Risk factors (safety margin)
    )

    # Apply defensive scaling - be conservative for high targets
    # Enhance realism in ranges that matter for 30M-300M targeting
    if defensive_ceiling < 0.2:
        # Very low ceiling: compress further for realism
        defensive_ceiling = defensive_ceiling * 0.7
    elif defensive_ceiling < 0.5:
        # Mid-range ceiling: moderate scaling for realistic targets
        defensive_ceiling = 0.14 + (defensive_ceiling - 0.2) * 0.93
    elif defensive_ceiling < 0.8:
        # High ceiling: conservative scaling for ambitious targets
        defensive_ceiling = 0.41 + (defensive_ceiling - 0.5) * 0.30

    return {
        'p50_ceiling': primary_ceiling,
        'p90_ceiling': secondary_ceiling,
        'p99_ceiling': tertiary_ceiling,
        'max_theoretical': defensive_ceiling
    }

    def compute_dominance_decay(self, niche: str) -> float:
        """
        V2: Dominance decay calculation - Prevents permanent dominance.
        
        Forces rotation by calculating decay based on:
        - Days in top ranks
        - % of factory output
        - Recent scale streak length
        
        Args:
            niche: Niche name
            
        Returns:
            Dominance decay penalty (0-1, higher = stronger decay)
        """
        # FACTOR 1: Days in top ranks
        days_in_top = self.get_time_in_top_position(niche)
        
        # Progressive penalty for extended time at top
        if days_in_top <= 3:
            top_rank_decay = 0.0  # No penalty for first 3 days
        elif days_in_top <= 7:
            top_rank_decay = 0.2 * (days_in_top - 3) / 4  # 0-20% penalty
        elif days_in_top <= 14:
            top_rank_decay = 0.2 + 0.3 * (days_in_top - 7) / 7  # 20-50% penalty
        else:
            top_rank_decay = min(0.5 + 0.5 * (days_in_top - 14) / 7, 1.0)  # 50-100% penalty
        
        # FACTOR 2: % of factory output (dominance share)
        # Get recent factory metrics for this niche vs total
        niche_metrics = self.factory_metrics.get(niche, {})
        niche_views = niche_metrics.get('views_7d', 0)
        niche_videos = niche_metrics.get('videos_7d', 0)
        
        # Calculate total factory output
        total_views = sum(m.get('views_7d', 0) for m in self.factory_metrics.values())
        total_videos = sum(m.get('videos_7d', 0) for m in self.factory_metrics.values())
        
        # Calculate dominance share (weighted by views and videos)
        if total_views > 0 and total_videos > 0:
            views_share = niche_views / total_views
            videos_share = niche_videos / total_videos
            factory_dominance = (views_share * 0.7) + (videos_share * 0.3)  # Weight views more heavily
        else:
            factory_dominance = 0.0
        
        # Progressive penalty for high factory dominance
        if factory_dominance <= 0.2:  # Under 20% share
            factory_decay = 0.0
        elif factory_dominance <= 0.3:  # 20-30% share
            factory_decay = 0.1 * (factory_dominance - 0.2) / 0.1  # 0-10% penalty
        elif factory_dominance <= 0.4:  # 30-40% share
            factory_decay = 0.1 + 0.2 * (factory_dominance - 0.3) / 0.1  # 10-30% penalty
        elif factory_dominance <= 0.5:  # 40-50% share
            factory_decay = 0.3 + 0.3 * (factory_dominance - 0.4) / 0.1  # 30-60% penalty
        else:  # Over 50% share
            factory_decay = min(0.6 + 0.4 * (factory_dominance - 0.5) / 0.5, 1.0)  # 60-100% penalty
        
        # FACTOR 3: Recent scale streak length
        # Calculate how many consecutive periods this niche has been scaling
        history = self.niche_history.get(niche, [])
        scale_streak = 0
        max_streak_lookback = 8  # Look back up to 8 periods
        
        # Count consecutive SCALE_UP actions from most recent
        for i in range(min(len(history), max_streak_lookback)):
            entry = history[-(i+1)]  # Start from most recent
            if entry.get('action') == 'scale_up':
                scale_streak += 1
            else:
                break  # Streak broken
        
        # Progressive penalty for long scale streaks
        if scale_streak <= 2:
            streak_decay = 0.0  # No penalty for short streaks
        elif scale_streak <= 4:
            streak_decay = 0.05 * (scale_streak - 2) / 2  # 0-5% penalty
        elif scale_streak <= 6:
            streak_decay = 0.05 + 0.1 * (scale_streak - 4) / 2  # 5-15% penalty
        else:
            streak_decay = min(0.15 + 0.15 * (scale_streak - 6) / 2, 0.3)  # 15-30% max penalty
        
        # COMBINED DOMINANCE DECAY CALCULATION
        # Weight factors based on impact severity
        combined_decay = (
            0.4 * top_rank_decay +      # Time at top (most important)
            0.4 * factory_decay +       # Factory dominance (critical)
            0.2 * streak_decay          # Scale streak (supporting factor)
        )
        
        # Apply non-linear scaling for severe cases
        if combined_decay > 0.7:
            # Very high decay: amplify to force rotation
            combined_decay = 0.7 + (combined_decay - 0.7) * 1.5
        elif combined_decay > 0.5:
            # High decay: moderate amplification
            combined_decay = 0.5 + (combined_decay - 0.5) * 1.2
        
        return min(combined_decay, 1.0)
    
    def _calculate_marginal_efficiency_gain(self, current_efficiency: float, prior_efficiency: float,
        current_cost: float, prior_cost: float) -> float:
        """
        Calculate marginal efficiency gain ratio.
        
        Args:
            current_efficiency: Current views per video
            prior_efficiency: Prior views per video
            current_cost: Current total cost
            prior_cost: Prior total cost
            
        Returns:
            Marginal efficiency gain ratio (0-1, higher = better gains)
        """
        # Calculate efficiency change
        efficiency_change = current_efficiency - prior_efficiency
        
        # Calculate cost change
        cost_change = current_cost - prior_cost
        
        # Marginal efficiency gain (efficiency improvement per additional cost)
        if cost_change > 0:
            marginal_gain = efficiency_change / cost_change
        else:
            marginal_gain = efficiency_change  # No additional cost
        
        # Normalize to 0-1 scale (assuming 100 views per dollar is excellent marginal gain)
        normalized_gain = min(marginal_gain / 100.0, 1.0)
        
        return max(normalized_gain, 0.0)
    
    def _calculate_roi_confidence(self, niche: str, metrics: Dict) -> float:
        """
        Calculate ROI confidence based on performance consistency.
        
        Args:
            niche: Niche name
            metrics: Factory metrics
            
        Returns:
            ROI confidence score (0-1, higher = more confident)
        """
        # Performance consistency metrics
        views_7d = metrics.get('views_7d', 1_000_000)
        views_14d_7d = metrics.get('views_14d_7d', 950_000)
        views_30d = metrics.get('views_30d', 4_000_000)
        
        # Calculate performance variance
        performance_variance = abs(views_7d - views_14d_7d) / max(views_14d_7d, 1)
        
        # Consistency bonus (lower variance = higher confidence)
        consistency_bonus = 1.0 - min(performance_variance, 1.0)
        
        # Volume confidence (more videos = more confidence)
        video_volume = metrics.get('videos_7d', 100)
        volume_confidence = min(video_volume / 500.0, 1.0)  # 500 videos = full confidence
        
        # Combined confidence
        roi_confidence = (consistency_bonus * 0.7) + (volume_confidence * 0.3)
        
        return max(roi_confidence, 0.1)  # Minimum 10% confidence
    
    def _calculate_optimal_capital_allocation(self, marginal_roi: float, scaling_trend: float,
                                        economies_of_scale: float) -> float:
        """
        Calculate optimal capital allocation ratio for maximum ROI.
        
        Args:
            marginal_roi: Current marginal ROI
            scaling_trend: Scaling efficiency trend
            economies_of_scale: Economies of scale score
            
        Returns:
            Optimal allocation ratio (0-1, higher = allocate more capital)
        """
        # Base allocation on marginal ROI
        base_allocation = min(marginal_roi / 500.0, 1.0)  # 500 views per dollar = full allocation
        
        # Adjust for scaling trend
        if scaling_trend > 1.1:  # Improving efficiency with scale
            scaling_bonus = min((scaling_trend - 1.0) * 2.0, 0.3)  # Up to 30% bonus
        elif scaling_trend < 0.9:  # Declining efficiency with scale
            scaling_penalty = min((1.0 - scaling_trend) * 1.5, 0.4)  # Up to 40% penalty
            scaling_bonus = -scaling_penalty
        else:
            scaling_bonus = 0.0
        
        # Adjust for economies of scale
        scale_bonus = (economies_of_scale - 0.5) * 0.4  # Convert to -0.2 to +0.2 range
        
        # Calculate optimal allocation
        optimal_allocation = base_allocation + scaling_bonus + scale_bonus
        
        return min(max(optimal_allocation, 0.0), 1.0)
    
    def _update_niche_history(self, ranked_scores: List[NicheScore]) -> None:
        """
        Update niche history tracking with latest scores and actions.
        
        Args:
            ranked_scores: List of NicheScore objects
        """
        current_time = 0  # Deterministic timestamp
        
        for i, score in enumerate(ranked_scores):
            history = self.niche_history[score.niche]
            
            # Add current ranking
            history.append({
                'timestamp': current_time,
                'rank': i + 1,
                'score': score.composite,
                'action': score.action.value
            })
            
            # Update dominance tracking
            self.dominance_tracking[score.niche] = {
                'dominance_score': score.dominance_score,
                'time_in_top': score.time_in_top_position,
                'last_updated': current_time
            }
    
    def compute_dominance_score(self, niche: str, all_scores: List[NicheScore]) -> float:
        """
        Compute how much a niche dominates the resource allocation.
        
        Args:
            niche: Niche name
            all_scores: List of all niche scores
            
        Returns:
            Dominance score (0-1)
        """
        # Find the niche score object
        niche_score_obj = next((s for s in all_scores if s.niche == niche), None)
        if not niche_score_obj:
            return 0.0
            
        total_composite = sum(s.composite for s in all_scores)
        if total_composite == 0:
            return 1.0
            
        dominance = niche_score_obj.composite / total_composite
        return min(dominance, 1.0)
    
    def compute_routing_score(self, niche: str) -> NicheScore:
        """
        Master score combining all routing signals with absolute efficiency and probabilistic ceiling.
        
        CRITICAL FIXES IMPLEMENTED:
        - Absolute efficiency: views per compute dollar (not marginal)
        - Probabilistic ceiling: p50, p90, p99, max_theoretical
        - Global dominance arbitration: prevents worse niches from scaling
        
        Formula:
            score = (momentum_weight * momentum)
                  - (saturation_weight * saturation)
                  + (marginal_efficiency_weight * absolute_efficiency)
                  + (ceiling_weight * p90_ceiling)
                  - (dominance_decay_weight * dominance_decay)
        
        Returns:
            NicheScore object with all component scores and action
    """
    def generate_routing_instructions(self) -> Dict[str, str]:
        """
        Returns:
            Dict keyed by niche
            Actions: SCALE_UP / MAINTAIN / THROTTLE / PAUSE
        """
        instructions = {}
        
        for niche in self.niche_configs.keys():
            score = self.compute_routing_score(niche)
            
            # Determine action based on score
            if score >= 0.7:
                action = "SCALE_UP"
            elif score >= 0.4:
                action = "MAINTAIN"
            elif score >= 0.2:
                action = "THROTTLE"
            else:
                action = "PAUSE"
            
            instructions[niche] = action
        
        return instructions

    def _determine_action(self, composite_score: float, momentum: float, niche: str) -> RoutingAction:
        """
        Determine routing action based on composite score and constraints.
        
        Args:
            composite_score: Composite routing score
            momentum: Momentum score
            niche: Niche name
            
        Returns:
            RoutingAction enum value
        """
        # Check baseline gate first
        if not self.passes_baseline_gate(niche):
            return RoutingAction.PAUSE
        
        # Check momentum gate for scaling
        if composite_score >= self.scale_threshold and momentum >= self.min_momentum_for_scale:
            return RoutingAction.SCALE_UP
        elif composite_score >= self.maintain_threshold:
            return RoutingAction.MAINTAIN
        elif composite_score >= self.throttle_threshold:
            return RoutingAction.THROTTLE
        else:
            return RoutingAction.PAUSE
    
    def get_baseline_clearance_rate(self, niche: str) -> float:
        """
        Get baseline clearance rate for hard constraint enforcement.
        
        Args:
            niche: Niche name
            
        Returns:
            Baseline clearance rate (0–1)
        """
        metrics = self.factory_metrics.get(niche, {})
        total_videos = metrics.get('total_videos', 1)
        baseline_clears = metrics.get('baseline_clears', 0)
        return baseline_clears / total_videos
    
    def passes_baseline_gate(self, niche: str) -> bool:
        """
        V2: Hard baseline gate - NON-NEGOTIABLE enforcement.
        
        Forbids scaling if baseline clears are weak.
        
        Args:
            niche: Niche name
            
        Returns:
            bool: True if passes gate (can scale), False if fails (must pause)
        """
        metrics = self.factory_metrics.get(niche, {})
        trends = self.trend_signals.get(niche, {})
        
        # Get current baseline clearance rate (7-day window)
        baseline_clear_rate_7d = metrics.get('baseline_clearance_rate_7d', 0.0)
        
        # RULE 1: Hard minimum threshold - NO EXCEPTIONS
        if baseline_clear_rate_7d < self.min_baseline_clearance:
            return False
        
        # RULE 2: Historical virals DO NOT count
        # Use only recent 7-day performance, not all-time achievements
        historical_virals = metrics.get('historical_viral_videos', 0)
        recent_virals = metrics.get('viral_videos_7d', 0)
        
        # If most virals are historical (not recent), fail gate
        if historical_virals > 0 and recent_virals / historical_virals < 0.3:
            return False
        
        # RULE 3: Baseline rate rising fast may override (but only if above minimum)
        baseline_rate_1d = metrics.get('baseline_clearance_rate_1d', baseline_clear_rate_7d)
        baseline_rate_3d = metrics.get('baseline_clearance_rate_3d', baseline_clear_rate_7d)
        
        # Calculate baseline velocity (rate of improvement)
        baseline_velocity_1d = (baseline_clear_rate_7d - baseline_rate_1d) / max(abs(baseline_rate_1d), 0.01)
        baseline_velocity_3d = (baseline_clear_rate_7d - baseline_rate_3d) / max(abs(baseline_rate_3d), 0.01)
        
        # Baseline acceleration (is improvement accelerating?)
        baseline_acceleration = baseline_velocity_1d - baseline_velocity_3d
        
        # Override condition: Strong rising trend may pass even at minimum threshold
        if baseline_clear_rate_7d >= self.min_baseline_clearance and baseline_acceleration > 0.1:
            # Strong upward trajectory - allow scaling
            return True
        
        # RULE 4: Consistency check - must be consistently above threshold
        baseline_rates = [
            metrics.get('baseline_clearance_rate_1d', baseline_clear_rate_7d),  # Use 7d as fallback
            metrics.get('baseline_clearance_rate_3d', baseline_clear_rate_7d),  # Use 7d as fallback
            baseline_clear_rate_7d
        ]
        
        # If any period is below threshold, fail gate
        if any(rate < self.min_baseline_clearance for rate in baseline_rates):
            return False
        
        # RULE 5: Volume requirement - must have sufficient sample size
        total_videos = metrics.get('total_videos_7d', 0)
        if total_videos < 50:  # Minimum videos for reliable baseline
            
            # Growth penalty with acceleration detection
            growth_competitor_penalty = 1.0 / max(1.0 + (competitor_growth_rate * 2.0), 0.5)
            
            # Market Entry Difficulty - How hard is it to enter this niche?
            market_entry_barrier = trends.get('market_entry_barrier', 0.5)  # 0-1 scale
            entry_penalty = 1.0 - (market_entry_barrier * 0.15)  # High barriers = lower ceiling
            
            # Total competitive penalty with advanced factors
            competitive_penalty = (
                base_competitor_penalty * 
                growth_competitor_penalty * 
                strength_penalty * 
                top_competitor_penalty *
                entry_penalty
            )
            
            # Advanced Composite Ceiling Calculation
            # Weight components for realistic 30M-300M targeting
            
            # Primary ceiling factors (highest weight - core potential)
            primary_ceiling = (
                0.35 * min(decayed_historical_max / 300_000_000.0, 1.0) +  # Temporal-adjusted historical max
                0.25 * min(max_platform_ceiling / 300_000_000.0, 1.0) +   # Best platform ceiling
                0.20 * adjusted_breadth +                               # Trend breadth (market size)
                0.15 * adjusted_cross_platform +                         # Cross-platform potential
                0.05 * diversification_bonus                              # Multi-platform resilience
            )
            
            # Secondary ceiling factors (medium weight - market constraints)
            secondary_ceiling = (
                0.40 * competitive_penalty +                           # Competition density effects
                0.30 * fatigue_penalty +                               # Genre fatigue effects
                0.20 * saturation_penalty +                             # Market saturation constraints
                0.10 * content_lifecycle_factor                         # Content age effects
            )
            
            # Tertiary ceiling factors (lower weight - risk factors)
            tertiary_ceiling = (
                0.50 * trends.get('regulatory_risk_score', 0.8) +          # Regulatory constraints
                0.30 * trends.get('technology_disruption_risk', 0.7) +   # Technology disruption risk
                0.20 * trends.get('market_volatility_score', 0.6)        # Market volatility
            )
            
            # Weighted composite with defensive bias
            defensive_ceiling = (
                0.50 * primary_ceiling +      # Core potential (highest priority)
                0.35 * secondary_ceiling +    # Market constraints (critical for realism)
                0.15 * tertiary_ceiling       # Risk factors (safety margin)
            )
            
            # Apply defensive scaling - be conservative for high targets
            # Enhance realism in ranges that matter for 30M-300M targeting
            if defensive_ceiling < 0.2:
                # Very low ceiling: compress further for realism
                defensive_ceiling = defensive_ceiling * 0.7
            elif defensive_ceiling < 0.5:
                # Mid-range ceiling: moderate scaling for realistic targets
                defensive_ceiling = 0.14 + (defensive_ceiling - 0.2) * 0.93
            elif defensive_ceiling < 0.8:
                # High ceiling: conservative scaling for ambitious targets
                defensive_ceiling = 0.41 + (defensive_ceiling - 0.5) * 0.30
            else:
                # Very high ceiling: strong conservative scaling for moonshot targets
                defensive_ceiling = 0.53 + (defensive_ceiling - 0.8) * 0.47
            
            # Apply absolute ceiling constraints for realism
            # Even the best niches have fundamental limits
            absolute_ceiling_cap = 0.95  # No niche can exceed 95% of theoretical maximum
            defensive_ceiling = min(defensive_ceiling, absolute_ceiling_cap)
            
            # Apply minimum floor for viability
            minimum_ceiling_floor = 0.05  # Every viable niche has some potential
            defensive_ceiling = max(defensive_ceiling, minimum_ceiling_floor)
            
            return defensive_ceiling
        
        def compute_routing_score(self, niche: str) -> float:
            """
            Master score combining all routing signals with marginal efficiency.
        Args:
            niche: Niche name
            
        Returns:
            Tuple of (baseline_clear_rate_delta, is_regressing)
        """
        metrics = self.factory_metrics.get(niche, {})
        
        # Get baseline clearance rates at different time windows
        baseline_rate_1d = metrics.get('baseline_clearance_rate_1d', 0.0)
        baseline_rate_3d = metrics.get('baseline_clearance_rate_3d', 0.0)
        baseline_rate_7d = metrics.get('baseline_clearance_rate_7d', 0.0)
        
        # Calculate delta (change from 3d to 1d)
        baseline_clear_rate_delta = baseline_rate_1d - baseline_rate_3d
        
        # Regression threshold - how much decline triggers forced downgrade
        regression_threshold = -0.10  # 10% decline triggers regression
        
        # Check if baseline is regressing (falling significantly)
        is_regressing = baseline_clear_rate_delta < regression_threshold
        
        return baseline_clear_rate_delta, is_regressing
    
    def detect_positive_acceleration(self, niche: str) -> Tuple[float, float, bool]:
        """
        INV-3: Scaling Requires Positive Acceleration - CRITICAL EARLY SCALING DETECTOR
        
        This is probably the MOST IMPORTANT invariant - ensures you scale early, not late.
        Momentum magnitude is insufficient — acceleration is required.
        
        Args:
            niche: Niche name
            
        Returns:
            Tuple of (acceleration, velocity, has_positive_acceleration)
        """
        metrics = self.factory_metrics.get(niche, {})
        
        # Get baseline clearance rates at different time windows
        baseline_rate_1d = metrics.get('baseline_clearance_rate_1d', 0.0)
        baseline_rate_3d = metrics.get('baseline_clearance_rate_3d', 0.0)
        baseline_rate_7d = metrics.get('baseline_clearance_rate_7d', 0.0)
        
        # Calculate velocity (rate of change)
        velocity_1d = baseline_rate_1d - baseline_rate_3d
        velocity_3d = baseline_rate_3d - baseline_rate_7d
        
        # Calculate acceleration (change in velocity)
        acceleration = velocity_1d - velocity_3d
        
        # Check if acceleration is positive
        has_positive_acceleration = acceleration > 0.01  # Small threshold for noise
        
        return acceleration, velocity_1d, has_positive_acceleration
    
    def generate_routing_instructions(self) -> Dict[str, Dict]:
        """
        Generate routing instructions for factory execution.
        
        Instructions include:
            - SCALE_UP: Increase content volume + budget
            - MAINTAIN: Hold current allocation
            - THROTTLE: Reduce generation
            - PAUSE: Stop content temporarily
        
        Returns:
            Dict keyed by niche with routing instructions
        """
        ranked = self.rank_niches()
        
        instructions = {}
        
        for score in ranked:
            # Compute raw signals for watchdog enforcement
            predictive_momentum = self._compute_predictive_momentum(score.niche)
            ignition_score = self._compute_ignition_score(score.niche)
            saturation_risk = self._compute_saturation_risk(score.niche)
            baseline_clear_rate_7d = self._compute_baseline_clearance_rate_7d(score.niche)
            baseline_clear_rate_delta = self._compute_baseline_clearance_delta(score.niche)
            marginal_efficiency = self._compute_marginal_efficiency(score.niche)
            days_in_top_rank = self._get_days_in_top_rank(score.niche)
            cooldown_remaining = self._get_cooldown_remaining(score.niche)
            niche_output_share = self._compute_niche_output_share(score.niche)
            
            instructions[score.niche] = {
                'proposed_action': score.action.value,           # NOT final
                'composite_score': score.composite,
                
                # Momentum
                'predictive_momentum': predictive_momentum,
                'ignition_score': ignition_score,
                
                # Safety
                'saturation_risk': saturation_risk,
                'baseline_clear_rate_7d': baseline_clear_rate_7d,
                'baseline_clear_rate_delta': baseline_clear_rate_delta,
                
                # Capital
                'marginal_efficiency': marginal_efficiency,
                
                # Dominance
                'days_in_top_rank': days_in_top_rank,
                'cooldown_remaining': cooldown_remaining,
                'niche_output_share': niche_output_share,
            }
        
        return instructions

    def _compute_predictive_momentum(self, niche: str) -> float:
        """Compute predictive momentum score for watchdog."""
        metrics = self.factory_metrics.get(niche, {})
        return metrics.get('predictive_momentum', 0.0)
    
    def _compute_ignition_score(self, niche: str) -> float:
        """Compute ignition score for watchdog."""
        metrics = self.factory_metrics.get(niche, {})
        return metrics.get('ignition_score', 0.0)
    
    def _compute_saturation_risk(self, niche: str) -> float:
        """Compute saturation risk for watchdog."""
        metrics = self.factory_metrics.get(niche, {})
        return metrics.get('saturation_risk', 0.0)
    
    def _compute_baseline_clearance_rate_7d(self, niche: str) -> float:
        """Compute 7-day baseline clearance rate for watchdog."""
        metrics = self.factory_metrics.get(niche, {})
        return metrics.get('baseline_clearance_rate_7d', 0.0)
    
    def _compute_baseline_clearance_delta(self, niche: str) -> float:
        """Compute baseline clearance delta for watchdog."""
        metrics = self.factory_metrics.get(niche, {})
        return metrics.get('baseline_clear_rate_delta', 0.0)
    
    def _compute_marginal_efficiency(self, niche: str) -> float:
        """Compute marginal efficiency for watchdog."""
        metrics = self.factory_metrics.get(niche, {})
        return metrics.get('marginal_efficiency', 0.0)
    
    def _get_days_in_top_rank(self, niche: str) -> int:
        """Get days in top rank for watchdog."""
        return self.get_time_in_top_position(niche)
    
    def _get_cooldown_remaining(self, niche: str) -> int:
        """Get cooldown remaining for watchdog."""
        metrics = self.factory_metrics.get(niche, {})
        return metrics.get('cooldown_remaining', 0)
    
    def _compute_niche_output_share(self, niche: str) -> float:
        """Compute niche output share for watchdog."""
        # Simple implementation - can be enhanced
        total_output = sum(self.factory_metrics.get(n, {}).get('total_output', 0) for n in self.niche_configs)
        niche_output = self.factory_metrics.get(niche, {}).get('total_output', 0)
        return niche_output / max(total_output, 1)
    
    def router_contract(self) -> dict:
        """
        Declares guarantees consumed by the invariant watchdog.
        """
        return {
            "produces_proposed_actions_only": True,
            "emits_raw_metrics": True,
            "does_not_enforce_invariants": True,
            "deterministic_given_snapshot": True
        }

    def get_top_niches(self, n: int = 5) -> List[str]:
        """
        Get top N niches by routing score.
        
        Args:
            n: Number of top niches to return
        
        Returns:
            List of niche names
        """
        ranked = self.rank_niches()
        return [score.niche for score in ranked[:n]]

    def should_scale_niche(self, niche: str) -> bool:
        """
        Quick check if niche should receive scaling.
        
        Args:
            niche: Niche name
        
        Returns:
            Boolean indicating if scaling is recommended
        """
        score = self.compute_routing_score(niche)
        return score >= self.scale_threshold

    def route(self) -> Dict[str, Any]:
        """
        Authoritative routing decision.
        This is the ONLY output consumed by orchestration.
        
        Returns:
            Dict with complete routing contract matching exact specification
        """
        start_time = time.time()
        
        # ENHANCEMENT 1: Baseline-first ordering pass
        baseline_safe_niches = []
        baseline_risk_niches = []
        for niche in sorted(self.niche_configs.keys()):
            baseline_clearance = self.get_baseline_clearance_rate(niche)
            passes_baseline_gate = baseline_clearance >= self.min_baseline_clearance
            
            if passes_baseline_gate:
                baseline_safe_niches.append(niche)
            else:
                baseline_risk_niches.append(niche)
        
        # Only baseline-safe niches compete for scaling and dominance math
        competitive_niches = baseline_safe_niches
        
        # Compute scores for competitive niches only (deterministic order)
        competitive_scores = []
        for niche in sorted(competitive_niches):
            score = self.compute_routing_score(niche)
            competitive_scores.append(score)
        
        # ENHANCEMENT 2: Cross-niche budget & exposure normalization
        total_composite_score = sum(score.composite for score in competitive_scores)
        
        for score in competitive_scores:
            # Normalized share of total competitive score
            score.normalized_share = score.composite / max(total_composite_score, 0.001)
            
            # ENHANCEMENT 3: Decision introspection trace
            score.decision_trace = {
                "baseline_gate": "passed" if score.baseline_clearance_rate >= self.min_baseline_clearance else "failed",
                "baseline_clearance_rate": score.baseline_clearance_rate,
                "momentum_gate": "passed" if score.momentum >= self.min_momentum_for_scale else "failed",
                "momentum_score": score.momentum,
                "saturation_enforcement": self._get_saturation_reason(score),
                "dominance_penalty_applied": score.dominance_score > self.max_dominance_share,
                "dominance_score": score.dominance_score,
                "concentration_violation": score.concentration_violation,
                "final_composite": score.composite,
                "final_action": score.action.value,
                "normalized_share": score.normalized_share
            }
            
            # Store raw component scores
            score.raw_scores = {
                "momentum": score.momentum,
                "saturation": score.saturation,
                "efficiency": score.efficiency,
                "ceiling": score.ceiling,
                "composite": score.composite
            }
        
        # ENHANCEMENT 4: Apply cross-niche constraints
        self.apply_budget_normalization(competitive_scores)
        
        # ENHANCEMENT 5: Deterministic final ordering
        # Sort by: action priority, adjusted score, niche name (tie-breaker)
        action_priority = {
            RoutingAction.SCALE_UP: 0,
            RoutingAction.MAINTAIN: 1,
            RoutingAction.THROTTLE: 2,
            RoutingAction.PAUSE: 3,
            RoutingAction.COOL_DOWN: 4,
            RoutingAction.HARD_KILL: 5
        }
        
        final_ranking = sorted(competitive_scores, key=lambda s: (
            action_priority.get(s.action, 999),
            -s.composite,  # Negative for descending order
            s.niche  # Alphabetical tie-breaker
        ))
        
        # ENHANCEMENT 6: State mutation persistence contract
        state_deltas = self._compute_state_deltas(final_ranking)
        
        # ENHANCEMENT 7: Global invariants checking
        global_invariants_checked = self._check_global_invariants(final_ranking)
        
        # Check for hard stops
        hard_stops_triggered = self._check_hard_stop_conditions(final_ranking)
        
        # Build niche outputs for routing contract - EXACT SPEC FORMAT
        niche_outputs = []
        for score in final_ranking:
            # Get all component scores for this niche
            momentum = self.compute_niche_momentum(score.niche)
            saturation = self.compute_saturation_score(score.niche)
            efficiency = self.compute_capital_efficiency(score.niche)
            ceiling = self.estimate_niche_ceiling(score.niche)
            routing_score = self.compute_routing_score(score.niche)
            
            niche_outputs.append({
                "niche": score.niche,
                "ceiling": ceiling,                    # EXPLICIT ceiling
                "efficiency": efficiency,                  # EXPLICIT efficiency  
                "routing_score": routing_score,          # EXPLICIT routing score
                "action": score.action.value,               # EXPLICIT action
                "raw_scores": {
                    "momentum": momentum,
                    "saturation": saturation,
                    "baseline_clearance": score.baseline_clearance_rate
                }
            })
        
        # Add baseline-risk niches with minimal info
        for niche in baseline_risk_niches:
            niche_outputs.append({
                "niche": niche,
                "raw_scores": {"baseline_clearance": self.get_baseline_clearance_rate(niche)},
                "adjusted_score": 0.0,
                "action": "pause",
                "constraints_triggered": ["baseline_gate_failed"],
                "state_updates": {}
            })
        
        # ENHANCEMENT 8: Enhanced output contract population
        execution_metadata = {
            'routing_duration_ms': (time.time() - start_time) * 1000 if 'start_time' in locals() else 0,
            'total_niches_processed': len(self.niche_configs),
            'baseline_safe_count': len(baseline_safe_niches),
            'baseline_risk_count': len(baseline_risk_niches),
            'competitive_count': len(competitive_scores),
            'constraint_violations': {
                'baseline_violations': len([s for s in final_ranking if s.baseline_clearance_rate < self.min_baseline_clearance]),
                'momentum_violations': len([s for s in final_ranking if s.action == RoutingAction.SCALE_UP and s.momentum < self.min_momentum_for_scale]),
                'dominance_violations': len([s for s in final_ranking if s.dominance_score > self.max_dominance_share]),
                'concentration_violations': len([s for s in final_ranking if s.concentration_violation])
            },
            'routing_efficiency': {
                'normalization_applied': len(competitive_scores) > 0,
                'state_mutations': len(state_deltas),
                'invariants_passed': len(global_invariants_checked),
                'hard_stops_active': hard_stops_triggered
            }
        }
        
        # ENHANCEMENT 9: Budget allocation details
        budget_allocation = {}
        total_budget = 1.0  # Normalized to 1.0 for relative allocation
        for score in final_ranking:
            budget_allocation[score.niche] = score.budget_share
        
        # ENHANCEMENT 10: Risk assessment
        risk_assessment = {
            'system_risk_level': 'low' if not hard_stops_triggered else 'high',
            'concentration_risk': sum(s.budget_share for s in final_ranking if s.action == RoutingAction.SCALE_UP),
            'dominance_risk': max([s.dominance_score for s in final_ranking]) if final_ranking else 0.0,
            'baseline_risk': len([s for s in final_ranking if s.baseline_clearance_rate < self.min_baseline_clearance]) / len(final_ranking) if final_ranking else 0.0,
            'momentum_risk': len([s for s in final_ranking if s.momentum < self.min_momentum_for_scale]) / len(final_ranking) if final_ranking else 0.0,
            'saturation_risk': len([s for s in final_ranking if s.saturation > self.saturation_hard_cap]) / len(final_ranking) if final_ranking else 0.0
        }
        
        # ENHANCEMENT 11: Performance forecast
        performance_forecast = {
            'expected_baseline_clearance': sum(s.baseline_clearance_rate * s.budget_share for s in final_ranking),
            'expected_momentum_score': sum(s.momentum * s.budget_share for s in final_ranking),
            'expected_efficiency_score': sum(s.efficiency * s.budget_share for s in final_ranking),
            'scaling_potential': len([s for s in final_ranking if s.action == RoutingAction.SCALE_UP]),
            'maintenance_requirements': len([s for s in final_ranking if s.action == RoutingAction.MAINTAIN]),
            'risk_niches': len([s for s in final_ranking if s.action in [RoutingAction.THROTTLE, RoutingAction.PAUSE]]),
            'critical_niches': len([s for s in final_ranking if s.action in [RoutingAction.COOL_DOWN, RoutingAction.HARD_KILL]])
        }
        
        # Return ENHANCED contract specification
        return RoutingOutput(
            snapshot_hash=self._input_snapshot_hash,
            timestamp=time.time(),
            router_version="V2.0",
            niches=niche_outputs,
            global_invariants_checked=global_invariants_checked,
            hard_stops_triggered=hard_stops_triggered,
            state_deltas=state_deltas,
            summary=self.build_routing_summary(final_ranking, baseline_safe_niches, baseline_risk_niches),
            execution_metadata=execution_metadata,
            budget_allocation=budget_allocation,
            risk_assessment=risk_assessment,
            performance_forecast=performance_forecast
        )

    def compute_composite_score(self, momentum: float, saturation: float, marginal_efficiency: float, ceiling: float, niche: str) -> Tuple[float, Dict[str, float]]:
        """
        Explicit composite score construction with auditable raw scores.
        
        Args:
            momentum: Momentum score (0-1)
            saturation: Saturation score (0-1)  
            marginal_efficiency: Marginal efficiency score (0-1)
            ceiling: Ceiling score (0-1)
            niche: Niche name for context
            
        Returns:
            Tuple of (composite_score, raw_scores_dict)
        """
        # Store raw component scores for auditability
        raw_scores = {
            'momentum': momentum,
            'saturation': saturation,
            'marginal_efficiency': marginal_efficiency,
            'ceiling': ceiling
        }
        
        # Explicit composite formula
        composite = (
            self.momentum_weight * momentum +
            self.marginal_efficiency_weight * marginal_efficiency +
            self.ceiling_weight * ceiling -
            self.saturation_weight * saturation
        )
        
        # Apply dominance decay if applicable
        dominance_decay = self.compute_dominance_decay(niche)
        composite -= self.dominance_decay_weight * dominance_decay
        return composite, raw_scores

    def apply_budget_normalization(self, scores: List[NicheScore]):
        """
        Budget normalization pass with dominance and concentration caps.
        
        Args:
            scores: List of niche scores to normalize
        """
        if not scores:
            return
        
        # Calculate total composite score for normalization
        total_composite = sum(score.composite for score in scores)
        
        if total_composite <= 0:
            return
        
        # First pass: Calculate normalized shares
        for score in scores:
            score.normalized_share = score.composite / total_composite
            score.budget_share = score.normalized_share
        
        # Second pass: Apply dominance caps
        for score in scores:
            if score.budget_share > self.max_dominance_share:
                excess = score.budget_share - self.max_dominance_share
                score.budget_share = self.max_dominance_share
                score.normalized_share = self.max_dominance_share
                
                # Record constraint violation
                if "dominance_cap_applied" not in score.decision_trace:
                    score.decision_trace["dominance_cap_applied"] = f"Reduced by {excess:.3f} to meet dominance cap"
        
        # Third pass: Apply concentration limits for SCALE_UP
        scale_up_scores = [s for s in scores if s.action == RoutingAction.SCALE_UP]
        if scale_up_scores:
            total_scale_concentration = sum(s.budget_share for s in scale_up_scores)
            
            if total_scale_concentration > self.max_concentration_ratio:
                excess_concentration = total_scale_concentration - self.max_concentration_ratio
                
                # Proportionally reduce scale-up allocations
                for score in scale_up_scores:
                    reduction_factor = self.max_concentration_ratio / total_scale_concentration
                    original_share = score.budget_share
                    score.budget_share *= reduction_factor
                    score.normalized_share *= reduction_factor
                    
                    if "concentration_limit_applied" not in score.decision_trace:
                        score.decision_trace["concentration_limit_applied"] = f"Reduced by factor {reduction_factor:.3f} to meet concentration limit"
        
        # Final pass: Re-normalize to ensure sums to 1.0
        final_total = sum(score.budget_share for score in scores)
        if final_total > 0:
            for score in scores:
                score.budget_share /= final_total
                score.normalized_share = score.budget_share

    def emit_state_deltas(self, ranked: List[NicheScore]) -> Dict[str, Dict[str, Any]]:
        """
        Emit state deltas for all niches with before/after tracking.
        
        Args:
            ranked: List of ranked niche scores
            
        Returns:
            Dict mapping niche names to their state changes
        """
        state_deltas = {}
        
        for score in ranked:
            niche = score.niche
            deltas = {}
            
            # Track dominance history changes
            current_dominance = score.dominance_score
            old_history_length = len(self.niche_history[niche])
            self.niche_history[niche].append(current_dominance)
            new_history_length = len(self.niche_history[niche])
            
            if new_history_length != old_history_length:
                deltas['dominance_history'] = {
                    'action': 'append',
                    'value': current_dominance,
                    'before_length': old_history_length,
                    'after_length': new_history_length
                }
            
            # Track cooldown timer changes
            old_cooldown = self.niche_state[niche]['cooldown_timer']
            new_cooldown = max(0, old_cooldown - 1)  # Decrement cooldown
            self.niche_state[niche]['cooldown_timer'] = new_cooldown
            
            if new_cooldown != old_cooldown:
                deltas['cooldown_timer'] = {
                    'action': 'decrement',
                    'before': old_cooldown,
                    'after': new_cooldown,
                    'delta': new_cooldown - old_cooldown
                }
            
            # Track scaling exposure
            if score.action in [RoutingAction.SCALE_UP, RoutingAction.MAINTAIN]:
                old_exposure_length = len(self.niche_state[niche]['recent_scaling_exposure'])
                self.niche_state[niche]['recent_scaling_exposure'].append(score.action.value)
                new_exposure_length = len(self.niche_state[niche]['recent_scaling_exposure'])
                
                if new_exposure_length != old_exposure_length:
                    deltas['scaling_exposure'] = {
                        'action': 'append',
                        'value': score.action.value,
                        'before_length': old_exposure_length,
                        'after_length': new_exposure_length
                    }
            
            if deltas:
                state_deltas[niche] = deltas
        
        return state_deltas

    def aggregate_invariant_report(self, ranked: List[NicheScore]) -> List[str]:
        """
        Aggregate global invariant report for monitoring.
        
        Args:
            ranked: List of ranked niche scores
            
        Returns:
            List of invariant check results
        """
        invariants_checked = []
        
        # Check baseline safety
        baseline_safe_count = len([s for s in ranked if s.baseline_clearance_rate >= self.min_baseline_clearance])
        if baseline_safe_count > 0:
            invariants_checked.append("baseline_safety_passed")
        
        # Check normalization consistency
        if ranked:
            total_normalized = sum(s.normalized_share for s in ranked)
            if abs(total_normalized - 1.0) < 0.01:
                invariants_checked.append("normalization_consistency_passed")
        
        # Check dominance caps
        dominance_violations = [s for s in ranked if s.budget_share > self.max_dominance_share]
        if not dominance_violations:
            invariants_checked.append("dominance_caps_passed")
        
        # Check concentration limits
        scale_up_scores = [s for s in ranked if s.action == RoutingAction.SCALE_UP]
        if scale_up_scores:
            total_scale_concentration = sum(s.budget_share for s in scale_up_scores)
            if total_scale_concentration <= self.max_concentration_ratio:
                invariants_checked.append("concentration_limits_passed")
        
        return invariants_checked

    def build_routing_summary(self, ranked: List[NicheScore], baseline_safe: List[str], baseline_risk: List[str]) -> Dict[str, Any]:
        """
        Build routing summary statistics.
        
        Args:
            ranked: List of ranked niche scores
            baseline_safe: List of baseline-safe niches
            baseline_risk: List of baseline-risk niches
            
        Returns:
            Summary statistics dictionary
        """
        action_distribution = {}
        for action in RoutingAction:
            action_distribution[action.value] = len([s for s in ranked if s.action == action])
        
        constraint_violations = {
            "baseline_violations": len([s for s in ranked if s.baseline_clearance_rate < self.min_baseline_clearance]),
            "momentum_violations": len([s for s in ranked if s.action == RoutingAction.SCALE_UP and s.momentum < self.min_momentum_for_scale]),
            "dominance_violations": len([s for s in ranked if s.dominance_score > self.max_dominance_share]),
            "concentration_violations": len([s for s in ranked if s.concentration_violation])
        }
        
        return {
            "total_niches": len(self.niche_configs),
            "baseline_safe_niches": len(baseline_safe),
            "baseline_risk_niches": len(baseline_risk),
            "competitive_niches": len(ranked),
            "action_distribution": action_distribution,
            "constraint_violations": constraint_violations,
            "routing_version": "V2.0"
        }

    def _get_triggered_constraints(self, score: NicheScore) -> List[str]:
        """Get list of constraints triggered for this niche."""
        constraints = []
        
        if score.baseline_clearance_rate < self.min_baseline_clearance:
            constraints.append("baseline_gate_failed")
        
        if score.action == RoutingAction.SCALE_UP and score.momentum < self.min_momentum_for_scale:
            constraints.append("momentum_insufficient")
        
        if score.dominance_score > self.max_dominance_share:
            constraints.append("dominance_exceeded")
        
        if score.concentration_violation:
            constraints.append("concentration_exceeded")
        
        if score.action in [RoutingAction.COOL_DOWN, RoutingAction.HARD_KILL]:
            constraints.append("saturation_enforcement")
        
        return constraints

    def _check_global_invariants(self, scores: List[NicheScore]) -> List[str]:
        """Check global routing invariants."""
        invariants_checked = []
        
        # INV-G1: At least one niche should be baseline-safe
        baseline_safe_count = len([s for s in scores if s.baseline_clearance_rate >= self.min_baseline_clearance])
        if baseline_safe_count > 0:
            invariants_checked.append("baseline_safety_passed")
        
        # INV-G2: Total normalized shares should sum to ~1.0 for competitive niches
        if scores:
            total_normalized = sum(s.normalized_share for s in scores)
            if abs(total_normalized - 1.0) < 0.01:  # Allow small rounding errors
                invariants_checked.append("normalization_consistency_passed")
        
        # INV-G3: No single niche exceeds dominance cap after normalization
        dominance_violations = [s for s in scores if s.normalized_share > self.max_dominance_share]
        if not dominance_violations:
            invariants_checked.append("dominance_caps_passed")
        
        # INV-G4: Scale-up concentration within limits
        scale_up_scores = [s for s in scores if s.action == RoutingAction.SCALE_UP]
        if scale_up_scores:
            total_scale_concentration = sum(s.normalized_share for s in scale_up_scores)
            if total_scale_concentration <= self.max_concentration_ratio:
                invariants_checked.append("concentration_limits_passed")
        
        return invariants_checked

    def _check_hard_stop_conditions(self, scores: List[NicheScore]) -> bool:
        """Check if any hard stop conditions are triggered."""
        # Hard stop if too many niches are in critical states
        hard_kill_count = len([s for s in scores if s.action == RoutingAction.HARD_KILL])
        cool_down_count = len([s for s in scores if s.action == RoutingAction.COOL_DOWN])
        
        # Hard stop if >50% of niches are in critical states
        total_niches = len(scores) + len([n for n in self.niche_configs.keys() if n not in [s.niche for s in scores]])
        critical_ratio = (hard_kill_count + cool_down_count) / max(total_niches, 1)
        
        return critical_ratio > 0.5

    def _compute_state_deltas(self, ranked: List[NicheScore]) -> Dict[str, Dict[str, Any]]:
        """
        Compute explicit state mutation ledger for all niches.
        
        Tracks all state changes with before/after values for audit trail
        and rollback safety. This makes state mutations predictable and explicit.
        
        Args:
            ranked: List of ranked niche scores
            
        Returns:
            Dict mapping niche names to their state deltas
        """
        state_deltas = {}
        
        # Capture current state before mutations
        previous_state = {}
        for niche in self.niche_configs.keys():
            previous_state[niche] = {
                'cooldown_timer': self.niche_state.get(niche, {}).get('cooldown_timer', 0),
                'consecutive_dominant_days': self.niche_state.get(niche, {}).get('consecutive_dominant_days', 0),
                'total_scaling_days': self.niche_state.get(niche, {}).get('total_scaling_days', 0),
                'last_scale_action': self.niche_state.get(niche, {}).get('last_scale_action', None),
                'performance_decay_rate': self.niche_state.get(niche, {}).get('performance_decay_rate', 0.0),
                'forced_rotation_cooldown': self.niche_state.get(niche, {}).get('forced_rotation_cooldown', 0),
                'saturation_kill_switch_active': self.niche_state.get(niche, {}).get('saturation_kill_switch_active', False),
                'dominance_history_length': len(self.niche_history.get(niche, deque(maxlen=30))),
                'recent_scaling_exposure_length': len(self.niche_state.get(niche, {}).get('recent_scaling_exposure', deque(maxlen=7)))
            }
        
        # Apply state mutations and track deltas
        for score in ranked:
            niche = score.niche
            deltas = {}
            
            # 1. Update dominance history
            current_dominance = score.dominance_score
            old_history_length = previous_state[niche]['dominance_history_length']
            self.niche_history[niche].append(current_dominance)
            new_history_length = len(self.niche_history[niche])
            
            if new_history_length != old_history_length:
                deltas['dominance_history'] = {
                    'action': 'append',
                    'value': current_dominance,
                    'before_length': old_history_length,
                    'after_length': new_history_length
                }
            
            # 2. Update time in top position
            old_time_in_top = previous_state[niche].get('time_in_top_position', 0)
            new_time_in_top = self.get_time_in_top_position(niche)
            
            if new_time_in_top != old_time_in_top:
                deltas['time_in_top_position'] = {
                    'action': 'increment' if new_time_in_top > old_time_in_top else 'reset',
                    'before': old_time_in_top,
                    'after': new_time_in_top,
                    'delta': new_time_in_top - old_time_in_top
                }
            
            # 3. Update consecutive dominant days
            old_consecutive = previous_state[niche]['consecutive_dominant_days']
            new_consecutive = self.niche_state[niche]['consecutive_dominant_days']
            
            if new_consecutive != old_consecutive:
                deltas['consecutive_dominant_days'] = {
                    'action': 'increment' if new_consecutive > old_consecutive else 'reset',
                    'before': old_consecutive,
                    'after': new_consecutive,
                    'delta': new_consecutive - old_consecutive
                }
            
            # 4. Update cooldown timers
            old_cooldown = previous_state[niche]['cooldown_timer']
            new_cooldown = self.niche_state[niche]['cooldown_timer']
            
            if new_cooldown != old_cooldown:
                deltas['cooldown_timer'] = {
                    'action': 'decrement' if new_cooldown < old_cooldown else 'set',
                    'before': old_cooldown,
                    'after': new_cooldown,
                    'delta': new_cooldown - old_cooldown
                }
            
            # 5. Update scaling exposure tracking
            if score.action in [RoutingAction.SCALE_UP, RoutingAction.MAINTAIN]:
                old_exposure_length = previous_state[niche]['recent_scaling_exposure_length']
                self.niche_state[niche]['recent_scaling_exposure'].append(score.action.value)
                new_exposure_length = len(self.niche_state[niche]['recent_scaling_exposure'])
                
                if new_exposure_length != old_exposure_length:
                    deltas['recent_scaling_exposure'] = {
                        'action': 'append',
                        'value': score.action.value,
                        'before_length': old_exposure_length,
                        'after_length': new_exposure_length
                    }
            
            # Only include niches with actual state changes
            if deltas:
                state_deltas[niche] = deltas
        
        return state_deltas

    def get_routing_report(self) -> Dict:
        """
        Generate comprehensive routing report for monitoring/debugging.
        
        Returns:
            Dict with routing state and recommendations
        """
        ranked = self.rank_niches()
        instructions = self.generate_routing_instructions()
        
        scale_up = [s.niche for s in ranked if s.action == RoutingAction.SCALE_UP]
        maintain = [s.niche for s in ranked if s.action == RoutingAction.MAINTAIN]
        throttle = [s.niche for s in ranked if s.action == RoutingAction.THROTTLE]
        pause = [s.niche for s in ranked if s.action == RoutingAction.PAUSE]
        
        # Hard constraint violations
        momentum_violations = [s.niche for s in ranked if s.action == RoutingAction.SCALE_UP and s.momentum < self.min_momentum_for_scale]
        baseline_violations = [s.niche for s in ranked if s.baseline_clearance_rate < self.min_baseline_clearance]
        dominance_violations = [s.niche for s in ranked if s.dominance_score > self.max_dominance_share]
        concentration_violations = [s.niche for s in ranked if s.concentration_violation]
        
        return {
            'timestamp': self.global_config.get('current_timestamp'),
            'total_niches': len(ranked),
            'action_distribution': {
                'scale_up': len(scale_up),
                'maintain': len(maintain),
                'throttle': len(throttle),
                'pause': len(pause)
            },
            'top_5_niches': [s.niche for s in ranked[:5]],
            'bottom_5_niches': [s.niche for s in ranked[-5:]],
            'niches_by_action': {
                'scale_up': scale_up,
                'maintain': maintain,
                'throttle': throttle,
                'pause': pause
            },
            'average_scores': {
                'momentum': np.mean([s.momentum for s in ranked]),
                'saturation': np.mean([s.saturation for s in ranked]),
                'efficiency': np.mean([s.efficiency for s in ranked]),
                'ceiling': np.mean([s.ceiling for s in ranked]),
                'composite': np.mean([s.composite for s in ranked])
            },
            'hard_constraints_status': {
                'momentum_violations': {
                    'count': len(momentum_violations),
                    'niches': momentum_violations,
                    'threshold': self.min_momentum_for_scale
                },
                'baseline_violations': {
                    'count': len(baseline_violations),
                    'niches': baseline_violations,
                    'threshold': self.min_baseline_clearance
                },
                'dominance_violations': {
                    'count': len(dominance_violations),
                    'niches': dominance_violations,
                    'threshold': self.max_dominance_share
                },
                'concentration_violations': {
                    'count': len(concentration_violations),
                    'niches': concentration_violations,
                    'threshold': self.max_concentration_ratio
                }
            },
            'detailed_rankings': [
                {
                    'rank': i + 1,
                    'niche': s.niche,
                    'score': s.composite,
                    'action': s.action.value,
                    'baseline_clearance': s.baseline_clearance_rate,
                    'dominance_score': s.dominance_score,
                    'time_in_top': s.time_in_top_position,
                    'concentration_violation': s.concentration_violation
                }
                for i, s in enumerate(ranked)
            ]
        }

    # ENHANCEMENT: Execution Orchestration Methods
    
    def create_execution_pipeline(self) -> ExecutionPipeline:
        """
        Create a new execution pipeline for routing decisions.
        
        Returns:
            ExecutionPipeline: New pipeline with all steps initialized
        """
        pipeline_id = f"pipeline_{int(time.time())}_{self._input_snapshot_hash[:8]}"
        pipeline = ExecutionPipeline(
            pipeline_id=pipeline_id,
            start_time=time.time()
        )
        
        # Add execution steps
        pipeline.add_step(ExecutionStep(
            step_id="validation",
            step_name="Input Validation",
            step_type="validation",
            status="pending"
        ))
        
        pipeline.add_step(ExecutionStep(
            step_id="baseline_gate",
            step_name="Baseline Gate Processing",
            step_type="validation",
            status="pending"
        ))
        
        pipeline.add_step(ExecutionStep(
            step_id="scoring",
            step_name="Niche Scoring",
            step_type="scoring",
            status="pending"
        ))
        
        pipeline.add_step(ExecutionStep(
            step_id="normalization",
            step_name="Budget Normalization",
            step_type="normalization",
            status="pending"
        ))
        
        pipeline.add_step(ExecutionStep(
            step_id="allocation",
            step_name="Final Allocation",
            step_type="allocation",
            status="pending"
        ))
        
        return pipeline
    
    def execute_pipeline_step(self, pipeline: ExecutionPipeline, step_id: str) -> bool:
        """
        Execute a specific step in the pipeline.
        
        Args:
            pipeline: Execution pipeline
            step_id: Step ID to execute
            
        Returns:
            bool: True if step executed successfully
        """
        step = pipeline.get_step_by_type(step_id)
        if not step:
            return False
        
        step.status = "in_progress"
        step.start_time = time.time()
        
        try:
            if step.step_type == "validation":
                success = self._execute_validation_step(step)
            elif step.step_type == "scoring":
                success = self._execute_scoring_step(step)
            elif step.step_type == "normalization":
                success = self._execute_normalization_step(step)
            elif step.step_type == "allocation":
                success = self._execute_allocation_step(step)
            else:
                success = False
                step.error_message = f"Unknown step type: {step.step_type}"
            
            if success:
                step.status = "completed"
            else:
                step.status = "failed"
            
        except Exception as e:
            step.status = "failed"
            step.error_message = str(e)
            success = False
        
        step.end_time = time.time()
        step.duration_ms = (step.end_time - step.start_time) * 1000
        
        return success
    
    def _execute_validation_step(self, step: ExecutionStep) -> bool:
        """Execute validation step."""
        # Validate input data
        if not self.niche_configs:
            step.error_message = "No niche configs provided"
            return False
        
        if not self.factory_metrics:
            step.error_message = "No factory metrics provided"
            return False
        
        # Validate baseline safety invariants
        try:
            self._enforce_baseline_safety_invariants()
            step.output_data = {"validation_passed": True}
            return True
        except Exception as e:
            step.error_message = f"Baseline safety validation failed: {str(e)}"
            return False
    
    def _execute_scoring_step(self, step: ExecutionStep) -> bool:
        """Execute scoring step."""
        try:
            # Compute scores for all niches
            scores = []
            for niche in sorted(self.niche_configs.keys()):
                score = self.compute_routing_score(niche)
                scores.append(score)
            
            step.output_data = {
                "scores_computed": len(scores),
                "average_composite": sum(s.composite for s in scores) / len(scores) if scores else 0.0
            }
            return True
        except Exception as e:
            step.error_message = f"Scoring failed: {str(e)}"
            return False
    
    def _execute_normalization_step(self, step: ExecutionStep) -> bool:
        """Execute normalization step."""
        try:
            # This would be called with actual scores from the scoring step
            # For now, just validate normalization logic exists
            step.output_data = {"normalization_ready": True}
            return True
        except Exception as e:
            step.error_message = f"Normalization failed: {str(e)}"
            return False
    
    def _execute_allocation_step(self, step: ExecutionStep) -> bool:
        """Execute allocation step."""
        try:
            # Validate allocation logic exists
            step.output_data = {"allocation_ready": True}
            return True
        except Exception as e:
            step.error_message = f"Allocation failed: {str(e)}"
            return False
    
    def create_routing_workflow(self) -> RoutingWorkflow:
        """
        Create a complete routing workflow with pipeline and stages.
        
        Returns:
            RoutingWorkflow: Complete workflow ready for execution
        """
        workflow_id = f"workflow_{int(time.time())}_{self._input_snapshot_hash[:8]}"
        pipeline = self.create_execution_pipeline()
        
        workflow = RoutingWorkflow(
            workflow_id=workflow_id,
            pipeline=pipeline,
            start_time=time.time()
        )
        
        # Add workflow stages
        workflow.add_stage(WorkflowStage(
            stage_id="pre_routing",
            stage_name="Pre-Routing Preparation",
            stage_type="pre_routing"
        ))
        
        workflow.add_stage(WorkflowStage(
            stage_id="routing",
            stage_name="Main Routing Execution",
            stage_type="routing",
            dependencies=["pre_routing"]
        ))
        
        workflow.add_stage(WorkflowStage(
            stage_id="post_routing",
            stage_name="Post-Routing Processing",
            stage_type="post_routing",
            dependencies=["routing"]
        ))
        
        workflow.add_stage(WorkflowStage(
            stage_id="validation",
            stage_name="Final Validation",
            stage_type="validation",
            dependencies=["post_routing"]
        ))
        
        return workflow
    
    def execute_workflow_stage(self, workflow: RoutingWorkflow, stage_id: str) -> bool:
        """
        Execute a specific stage in the workflow.
        
        Args:
            workflow: Routing workflow
            stage_id: Stage ID to execute
            
        Returns:
            bool: True if stage executed successfully
        """
        stage = workflow.get_stage_by_id(stage_id)
        if not stage:
            return False
        
        stage.status = "in_progress"
        stage.start_time = time.time()
        
        try:
            if stage.stage_type == "pre_routing":
                success = self._execute_pre_routing_stage(stage)
            elif stage.stage_type == "routing":
                success = self._execute_routing_stage(stage)
            elif stage.stage_type == "post_routing":
                success = self._execute_post_routing_stage(stage)
            elif stage.stage_type == "validation":
                success = self._execute_validation_stage(stage)
            else:
                success = False
                stage.error = f"Unknown stage type: {stage.stage_type}"
            
            if success:
                stage.status = "completed"
                workflow.current_stage = stage_id
            else:
                stage.status = "failed"
            
        except Exception as e:
            stage.status = "failed"
            stage.error = str(e)
            success = False
        
        stage.end_time = time.time()
        stage.duration_ms = (stage.end_time - stage.start_time) * 1000
        
        return success
    
    def _execute_pre_routing_stage(self, stage: WorkflowStage) -> bool:
        """Execute pre-routing stage."""
        # Prepare for routing
        try:
            # Validate inputs and prepare state
            self._enforce_baseline_safety_invariants()
            stage.result = {"preparation_complete": True}
            return True
        except Exception as e:
            stage.error = f"Pre-routing preparation failed: {str(e)}"
            return False
    
    def _execute_routing_stage(self, stage: WorkflowStage) -> bool:
        """Execute main routing stage."""
        try:
            # Execute the main routing logic
            routing_output = self.route()
            stage.result = routing_output
            workflow.pipeline.final_output = routing_output
            return True
        except Exception as e:
            stage.error = f"Routing execution failed: {str(e)}"
            return False
    
    def _execute_post_routing_stage(self, stage: WorkflowStage) -> bool:
        """Execute post-routing stage."""
        try:
            # Process routing results
            if workflow.pipeline.final_output:
                # Validate output contract
                violations = workflow.pipeline.final_output.validate_contract()
                stage.result = {"contract_violations": violations}
                return len(violations) == 0
            return False
        except Exception as e:
            stage.error = f"Post-routing processing failed: {str(e)}"
            return False
    
    def _execute_validation_stage(self, stage: WorkflowStage) -> bool:
        """Execute final validation stage."""
        try:
            # Final validation of the entire workflow
            if workflow.pipeline.final_output:
                # Check invariants
                invariants_passed = len(workflow.pipeline.final_output.global_invariants_checked)
                stage.result = {
                    "invariants_passed": invariants_passed,
                    "workflow_complete": True
                }
                return True
            return False
        except Exception as e:
            stage.error = f"Final validation failed: {str(e)}"
            return False
    
    def execute_complete_workflow(self) -> RoutingWorkflow:
        """
        Execute the complete routing workflow from start to finish.
        
        Returns:
            RoutingWorkflow: Completed workflow with results
        """
        workflow = self.create_routing_workflow()
        workflow.status = "running"
        
        # Execute stages in dependency order
        while True:
            ready_stages = workflow.get_ready_stages()
            
            if not ready_stages:
                # No more ready stages, check if workflow is complete
                completed_stages = [s for s in workflow.stages if s.status == 'completed']
                if len(completed_stages) == len(workflow.stages):
                    workflow.status = "completed"
                else:
                    workflow.status = "failed"
                    # Add error summary for failed workflow
                    failed_stages = [s for s in workflow.stages if s.status == 'failed']
                    workflow.pipeline.error_summary = {
                        "failed_stages": [s.stage_id for s in failed_stages],
                        "errors": [s.error for s in failed_stages]
                    }
                break
            
            # Execute the first ready stage
            stage = ready_stages[0]
            success = self.execute_workflow_stage(workflow, stage.stage_id)
            
            if not success:
                workflow.status = "failed"
                break
        
        workflow.end_time = time.time()
        workflow.total_duration_ms = (workflow.end_time - workflow.start_time) * 1000
        workflow.pipeline.end_time = workflow.end_time
        workflow.pipeline.total_duration_ms = workflow.total_duration_ms
        workflow.pipeline.status = workflow.status
        
        return workflow

    # ENHANCEMENT: Advanced Budget Allocation Methods
    
    def optimize_budget_allocation(self, total_budget: float, allocation_strategy: str = "marginal_roi") -> Dict[str, float]:
        """
        Advanced budget optimization with multiple allocation strategies.
        
        Args:
            total_budget: Total budget to allocate
            allocation_strategy: Strategy for allocation ('marginal_roi', 'risk_adjusted', 'momentum_weighted', 'balanced')
            
        Returns:
            Dict mapping niche names to budget allocations
        """
        ranked = self.rank_niches()
        
        if allocation_strategy == "marginal_roi":
            return self._allocate_by_marginal_roi(ranked, total_budget)
        elif allocation_strategy == "risk_adjusted":
            return self._allocate_by_risk_adjusted_roi(ranked, total_budget)
        elif allocation_strategy == "momentum_weighted":
            return self._allocate_by_momentum_weighted(ranked, total_budget)
        elif allocation_strategy == "balanced":
            return self._allocate_balanced(ranked, total_budget)
        else:
            # Default to marginal ROI
            return self._allocate_by_marginal_roi(ranked, total_budget)
    
    def _allocate_by_marginal_roi(self, ranked: List[NicheScore], total_budget: float) -> Dict[str, float]:
        """
        Allocate budget based on marginal ROI optimization.
        
        Args:
            ranked: List of ranked niche scores
            total_budget: Total budget to allocate
            
        Returns:
            Budget allocation dictionary
        """
        # Calculate marginal ROI weights
        roi_weights = {}
        total_roi_weight = 0.0
        
        for score in ranked:
            # Only consider niches that pass baseline gate
            if score.baseline_clearance_rate >= self.min_baseline_clearance:
                # Marginal ROI with diminishing returns
                marginal_roi = score.efficiency * (1.0 - score.saturation * 0.5)
                # Apply momentum bonus for scaling opportunities
                momentum_bonus = 1.0 + (score.momentum * 0.3)
                adjusted_roi = marginal_roi * momentum_bonus
                
                roi_weights[score.niche] = adjusted_roi
                total_roi_weight += adjusted_roi
        
        # Normalize weights and allocate budget
        allocation = {}
        if total_roi_weight > 0:
            for niche, weight in roi_weights.items():
                normalized_weight = weight / total_roi_weight
                # Apply dominance cap
                max_allocation = total_budget * self.max_dominance_share
                allocation[niche] = min(normalized_weight * total_budget, max_allocation)
        
        return allocation
    
    def _allocate_by_risk_adjusted_roi(self, ranked: List[NicheScore], total_budget: float) -> Dict[str, float]:
        """
        Allocate budget with risk adjustment factors.
        
        Args:
            ranked: List of ranked niche scores
            total_budget: Total budget to allocate
            
        Returns:
            Risk-adjusted budget allocation dictionary
        """
        allocation = {}
        risk_adjusted_weights = {}
        total_risk_weight = 0.0
        
        for score in ranked:
            if score.baseline_clearance_rate >= self.min_baseline_clearance:
                # Base ROI
                base_roi = score.efficiency
                
                # Risk factors
                saturation_risk = score.saturation  # Higher saturation = higher risk
                dominance_risk = score.dominance_score  # Higher dominance = concentration risk
                momentum_risk = 1.0 - score.momentum  # Lower momentum = higher risk
                
                # Composite risk score (lower is better)
                risk_score = (saturation_risk * 0.4 + dominance_risk * 0.3 + momentum_risk * 0.3)
                
                # Risk-adjusted ROI (penalize high-risk niches)
                risk_adjustment_factor = 1.0 - (risk_score * 0.3)  # Max 30% penalty
                risk_adjusted_roi = base_roi * risk_adjustment_factor
                
                # Ensure positive values
                risk_adjusted_roi = max(risk_adjusted_roi, 0.01)
                
                risk_adjusted_weights[score.niche] = risk_adjusted_roi
                total_risk_weight += risk_adjusted_roi
        
        # Normalize and allocate with concentration limits
        if total_risk_weight > 0:
            for niche, weight in risk_adjusted_weights.items():
                normalized_weight = weight / total_risk_weight
                
                # Apply concentration limits for scale-up niches
                score = next(s for s in ranked if s.niche == niche)
                if score.action == RoutingAction.SCALE_UP:
                    max_concentration = total_budget * self.max_concentration_ratio
                    allocation[niche] = min(normalized_weight * total_budget, max_concentration)
                else:
                    allocation[niche] = normalized_weight * total_budget
        
        return allocation
    
    def _allocate_by_momentum_weighted(self, ranked: List[NicheScore], total_budget: float) -> Dict[str, float]:
        """
        Allocate budget with momentum weighting for growth opportunities.
        
        Args:
            ranked: List of ranked niche scores
            total_budget: Total budget to allocate
            
        Returns:
            Momentum-weighted budget allocation dictionary
        """
        allocation = {}
        momentum_weights = {}
        total_momentum_weight = 0.0
        
        for score in ranked:
            if score.baseline_clearance_rate >= self.min_baseline_clearance:
                # Base efficiency
                base_efficiency = score.efficiency
                
                # Momentum amplification
                momentum_amplifier = 1.0 + (score.momentum * 0.5)  # Up to 50% bonus for high momentum
                
                # Composite momentum score
                momentum_weight = base_efficiency * momentum_amplifier
                momentum_weights[score.niche] = momentum_weight
                total_momentum_weight += momentum_weight
        
        # Normalize and allocate
        if total_momentum_weight > 0:
            for niche, weight in momentum_weights.items():
                normalized_weight = weight / total_momentum_weight
                allocation[niche] = normalized_weight * total_budget
        
        return allocation
    
    def _allocate_balanced(self, ranked: List[NicheScore], total_budget: float) -> Dict[str, float]:
        """
        Balanced allocation considering multiple factors.
        
        Args:
            ranked: List of ranked niche scores
            total_budget: Total budget to allocate
            
        Returns:
            Balanced budget allocation dictionary
        """
        allocation = {}
        balanced_weights = {}
        total_weight = 0.0
        
        for score in ranked:
            if score.baseline_clearance_rate >= self.min_baseline_clearance:
                # Multi-factor scoring
                efficiency_factor = score.efficiency * 0.4  # 40% weight
                momentum_factor = score.momentum * 0.3    # 30% weight
                ceiling_factor = score.ceiling * 0.2       # 20% weight
                stability_factor = (1.0 - score.saturation) * 0.1  # 10% weight (inverse saturation)
                
                # Composite balanced score
                balanced_score = efficiency_factor + momentum_factor + ceiling_factor + stability_factor
                balanced_weights[score.niche] = balanced_score
                total_weight += balanced_score
        
        # Normalize and allocate with constraints
        if total_weight > 0:
            for niche, weight in balanced_weights.items():
                normalized_weight = weight / total_weight
                
                # Apply all constraints
                score = next(s for s in ranked if s.niche == niche)
                
                # Dominance cap
                max_dominance = total_budget * self.max_dominance_share
                base_allocation = normalized_weight * total_budget
                
                # Concentration limit for scale-up
                if score.action == RoutingAction.SCALE_UP:
                    max_concentration = total_budget * self.max_concentration_ratio
                    allocation[niche] = min(base_allocation, max_concentration, max_dominance)
                else:
                    allocation[niche] = min(base_allocation, max_dominance)
        
        return allocation
    
    def optimize_allocation_iteratively(self, total_budget: float, max_iterations: int = 10) -> Dict[str, float]:
        """
        Optimize budget allocation iteratively using different strategies.
        
        Args:
            total_budget: Total budget to allocate
            max_iterations: Maximum optimization iterations
            
        Returns:
            Optimized budget allocation dictionary
        """
        best_allocation = {}
        best_efficiency = 0.0
        
        for iteration in range(max_iterations):
            # Try different allocation strategies
            strategies = ["marginal_roi", "risk_adjusted", "momentum_weighted", "balanced"]
            strategy = strategies[iteration % len(strategies)]
            
            allocation = self.optimize_budget_allocation(total_budget, strategy)
            efficiency_metrics = self.compute_allocation_efficiency(allocation)
            
            # Calculate total efficiency
            total_efficiency = sum(metrics['risk_adjusted_efficiency'] for metrics in efficiency_metrics.values())
            
            # Keep best allocation
            if total_efficiency > best_efficiency:
                best_efficiency = total_efficiency
                best_allocation = allocation.copy()
        
        return best_allocation
    
    def simulate_allocation_scenarios(self, total_budget: float, max_iterations: int = 10) -> Dict[str, Dict[str, Any]]:
        """
        Simulate different allocation scenarios for comparison.
        
        Args:
            total_budget: Total budget to allocate
            max_iterations: Maximum optimization iterations
            
        Returns:
            Dictionary of scenario results
        """
        scenarios = {}
        
        # Test all allocation strategies
        strategies = ["marginal_roi", "risk_adjusted", "momentum_weighted", "balanced"]
        
        for strategy in strategies:
            allocation = self.optimize_budget_allocation(total_budget, strategy)
            efficiency_metrics = self.compute_allocation_efficiency(allocation)
            
            # Calculate scenario metrics
            total_expected_return = sum(metrics['expected_return'] for metrics in efficiency_metrics.values())
            total_risk_score = sum(metrics['risk_score'] for metrics in efficiency_metrics.values()) / len(efficiency_metrics)
            
            scenarios[strategy] = {
                'allocation': allocation,
                'efficiency_metrics': efficiency_metrics,
                'total_expected_return': total_expected_return,
                'average_risk_score': total_risk_score,
                'allocated_budget': sum(allocation.values()),
                'allocation_count': len(allocation)
            }
        
        return scenarios
    
    def rank_niches(self) -> List[Tuple[str, float]]:
        """
        Returns: List of (niche_name, score) for explicit ranking
        """
        scores = []
        
        # Compute scores for all niches (deterministic order)
        for niche in sorted(self.niche_configs.keys()):
            score = self.compute_routing_score(niche)
            scores.append(score)
        
        # CRITICAL FIX: Apply global dominance arbitration
        scores = self.apply_global_dominance_arbitration(scores)
        
        # Sort by composite score (descending)
        ranked = sorted(scores, key=lambda s: s.composite, reverse=True)
        return ranked
    
    # MISSING HELPER METHODS FOR CRITICAL FIXES
    
    def _calculate_global_opportunity_cost(self, niche: str, absolute_efficiency: float) -> float:
        """
        Calculate global opportunity cost for absolute efficiency comparison.
        
        Args:
            niche: Current niche
            absolute_efficiency: Current niche's absolute efficiency (views per dollar)
            
        Returns:
            Global opportunity cost multiplier (0-2, higher = better than alternatives)
        """
        all_absolute_efficiencies = []
        
        for other_niche in self.niche_configs.keys():
            if other_niche == niche:
                continue
                
            other_metrics = self.factory_metrics.get(other_niche, {})
            
            # Calculate other niche's absolute efficiency
            other_views = other_metrics.get('views_7d', 500_000)
            other_compute_cost = other_metrics.get('compute_cost_current', 20.0)
            other_generation_cost = other_metrics.get('generation_cost_current', 8.0)
            other_bandwidth_cost = other_metrics.get('bandwidth_cost_current', 4.0)
            
            other_total_cost = other_compute_cost + other_generation_cost + other_bandwidth_cost
            other_absolute_efficiency = other_views / max(other_total_cost, 1.0)
            
            all_absolute_efficiencies.append(other_absolute_efficiency)
        
        if not all_absolute_efficiencies:
            return 1.0
        
        # Calculate global opportunity cost score
        avg_alternative_efficiency = sum(all_absolute_efficiencies) / len(all_absolute_efficiencies)
        
        if avg_alternative_efficiency > 0:
            opportunity_cost_ratio = absolute_efficiency / avg_alternative_efficiency
            # Cap at 2x better than average
            return min(opportunity_cost_ratio, 2.0)
        else:
            return 1.0
    
    def _calculate_percentile_ceiling(self, historical_max: float, platform_ceiling: float,
                                  breadth: float, cross_platform: float,
                                  competitive_penalty: float, fatigue_penalty: float,
                                  saturation_penalty: float, percentile: float,
                                  confidence: str) -> float:
        """
        Calculate percentile-specific ceiling estimate.
        
        Args:
            historical_max: Temporal-decayed historical maximum
            platform_ceiling: Best platform ceiling
            breadth: Trend breadth score
            cross_platform: Cross-platform potential
            competitive_penalty: Competition density penalty
            fatigue_penalty: Genre fatigue penalty
            saturation_penalty: Market saturation penalty
            percentile: Target percentile (0.5, 0.9, 0.99)
            confidence: Confidence level ('medium', 'high', 'very_high')
            
        Returns:
            Percentile ceiling estimate (0-1)
        """
        # Base ceiling from historical and platform factors
        base_ceiling = (
            0.4 * min(historical_max / 300_000_000.0, 1.0) +
            0.3 * min(platform_ceiling / 300_000_000.0, 1.0) +
            0.2 * breadth +
            0.1 * cross_platform
        )
        
        # Apply market constraints
        market_constrained_ceiling = base_ceiling * competitive_penalty * fatigue_penalty * saturation_penalty
        
        # Percentile adjustments
        if percentile == 0.5:  # P50 - Conservative
            percentile_multiplier = 0.7
        elif percentile == 0.9:  # P90 - Optimistic but realistic
            percentile_multiplier = 0.85
        elif percentile == 0.99:  # P99 - Best case
            percentile_multiplier = 0.95
        else:
            percentile_multiplier = 0.8
        
        # Confidence adjustments
        if confidence == 'medium':
            confidence_multiplier = 0.9
        elif confidence == 'high':
            confidence_multiplier = 0.95
        elif confidence == 'very_high':
            confidence_multiplier = 1.0
        else:
            confidence_multiplier = 0.85
        
        final_ceiling = market_constrained_ceiling * percentile_multiplier * confidence_multiplier
        return min(max(final_ceiling, 0.0), 1.0)
    
    def _calculate_max_theoretical_ceiling(self, platform_ceiling: float, breadth: float,
                                       cross_platform: float) -> float:
        """
        Calculate maximum theoretical ceiling under perfect conditions.
        
        Args:
            platform_ceiling: Best platform ceiling
            breadth: Trend breadth score
            cross_platform: Cross-platform potential
            
        Returns:
            Maximum theoretical ceiling (0-1)
        """
        # Perfect conditions: no market constraints, maximum platform potential
        theoretical_ceiling = (
            0.5 * min(platform_ceiling / 300_000_000.0, 1.0) +  # Best platform
            0.3 * breadth +                                       # Maximum market breadth
            0.2 * cross_platform                                   # Perfect cross-platform success
        )
        
        return min(theoretical_ceiling, 1.0)
    
    def _calculate_cross_platform_transfer_multiplier(self, niche: str, trends: Dict, metrics: Dict) -> float:
        """
        Calculate cross-platform transfer multiplier for ceiling estimates.
        
        Args:
            niche: Niche name
            trends: Trend signals
            metrics: Factory metrics
            
        Returns:
            Cross-platform transfer multiplier (0.8-1.2)
        """
        # Platform transfer potential
        tiktok_success = trends.get('tiktok_success_rate', 0.5)
        youtube_success = trends.get('youtube_success_rate', 0.5)
        instagram_success = trends.get('instagram_success_rate', 0.5)
        twitter_success = trends.get('twitter_success_rate', 0.5)
        
        # Calculate transfer consistency (how consistent is success across platforms)
        platform_successes = [tiktok_success, youtube_success, instagram_success, twitter_success]
        avg_success = sum(platform_successes) / len(platform_successes)
        success_variance = sum((s - avg_success) ** 2 for s in platform_successes) / len(platform_successes)
        transfer_consistency = 1.0 - min(success_variance, 1.0)  # Lower variance = higher consistency
        
        # Transfer multiplier based on consistency and average success
        base_multiplier = 0.8 + (avg_success * 0.3) + (transfer_consistency * 0.1)
        
        # Historical transfer evidence
        historical_transfers = metrics.get('cross_platform_viral_count', 0)
        transfer_bonus = min(historical_transfers * 0.05, 0.2)  # Up to 20% bonus
        
        final_multiplier = base_multiplier + transfer_bonus
        return min(max(final_multiplier, 0.8), 1.2)  # Bound between 0.8 and 1.2
    
    def _calculate_ceiling_variance(self, p50: float, p90: float, p99: float, max_theoretical: float) -> float:
        """
        Calculate variance in ceiling estimates for confidence assessment.
        
        Args:
            p50: P50 ceiling estimate
            p90: P90 ceiling estimate
            p99: P99 ceiling estimate
            max_theoretical: Maximum theoretical ceiling
            
        Returns:
            Ceiling variance score (0-1, higher = more variance)
        """
        # Calculate spread between estimates
        p50_p90_spread = abs(p90 - p50)
        p90_p99_spread = abs(p99 - p90)
        p99_max_spread = abs(max_theoretical - p99)
        
        # Weight spreads (higher spreads between higher percentiles matter more)
        weighted_spread = (
            0.2 * p50_p90_spread +
            0.3 * p90_p99_spread +
            0.5 * p99_max_spread
        )
        
        return min(weighted_spread, 1.0)
    
    def _calculate_ceiling_confidence(self, metrics: Dict, trends: Dict, variance: float) -> float:
        """
        Calculate confidence in ceiling estimates based on data quality.
        
        Args:
            metrics: Factory metrics
            trends: Trend signals
            variance: Ceiling variance
            
        Returns:
            Confidence score (0-1, higher = more confident)
        """
        # Data quality factors
        data_volume = metrics.get('total_videos', 100)
        data_recency = metrics.get('days_since_last_video', 7)
        trend_completeness = len([v for v in trends.values() if v is not None]) / max(len(trends), 1)
        
        # Confidence from data factors
        volume_confidence = min(data_volume / 1000.0, 1.0)  # 1000 videos = full confidence
        recency_confidence = max(0, 1.0 - (data_recency / 30.0))  # 30 days = zero confidence
        completeness_confidence = trend_completeness
        
        # Variance penalty (higher variance = lower confidence)
        variance_penalty = 1.0 - (variance * 0.5)
        
        # Combined confidence
        raw_confidence = (
            0.4 * volume_confidence +
            0.3 * recency_confidence +
            0.2 * completeness_confidence +
            0.1 * variance_penalty
        )
        
        return min(max(raw_confidence, 0.1), 1.0)
    
    def _calculate_scale_discrimination(self, p50: float, p90: float, p99: float, max_theoretical: float) -> float:
        """
        Calculate 30M-300M discrimination score for ceiling estimates.
        
        Args:
            p50: P50 ceiling estimate
            p90: P90 ceiling estimate
            p99: P99 ceiling estimate
            max_theoretical: Maximum theoretical ceiling
            
        Returns:
            Scale discrimination score (0-1, higher = better discrimination)
        """
        # Convert to view counts for discrimination analysis
        p50_views = p50 * 300_000_000
        p90_views = p90 * 300_000_000
        p99_views = p99 * 300_000_000
        
        # Discrimination factors
        has_30m_potential = 1.0 if p90_views >= 30_000_000 else 0.0
        has_100m_potential = 1.0 if p90_views >= 100_000_000 else 0.0
        has_300m_potential = 1.0 if p99_views >= 300_000_000 else 0.0
        
        # Range discrimination (spread between estimates)
        estimate_range = p99_views - p50_views
        range_discrimination = min(estimate_range / 270_000_000, 1.0)  # 270M range = full discrimination
        
        # Confidence discrimination (how much higher percentiles exceed lower ones)
        confidence_discrimination = (p90_views - p50_views) / max(p50_views, 1_000_000)
        confidence_discrimination = min(confidence_discrimination, 1.0)
        
        # Combined discrimination score
        discrimination_score = (
            0.4 * has_30m_potential +
            0.3 * has_100m_potential +
            0.2 * has_300m_potential +
            0.05 * range_discrimination +
            0.05 * confidence_discrimination
        )
        
        return discrimination_score
    
    def _classify_ceiling_tier(self, p90_ceiling: float) -> str:
        """
        Classify ceiling into tier based on P90 estimate.
        
        Args:
            p90_ceiling: P90 ceiling estimate
            
        Returns:
            Ceiling tier classification
        """
        p90_views = p90_ceiling * 300_000_000
        
        if p90_views >= 300_000_000:
            return "300M+ Tier"
        elif p90_views >= 100_000_000:
            return "100M-300M Tier"
        elif p90_views >= 30_000_000:
            return "30M-100M Tier"
        elif p90_views >= 10_000_000:
            return "10M-30M Tier"
        elif p90_views >= 1_000_000:
            return "1M-10M Tier"
        else:
            return "Sub-1M Tier"
    
    def _classify_ceiling_risk(self, variance: float, confidence: float) -> str:
        """
        Classify ceiling risk based on variance and confidence.
        
        Args:
            variance: Ceiling variance
            confidence: Estimate confidence
            
        Returns:
            Risk level classification
        """
        if variance > 0.5 or confidence < 0.3:
            return "High Risk"
        elif variance > 0.3 or confidence < 0.6:
            return "Medium Risk"
        elif variance > 0.1 or confidence < 0.8:
            return "Low-Medium Risk"
        else:
            return "Low Risk"
    
    def apply_global_dominance_arbitration(self, all_scores: List[NicheScore]) -> List[NicheScore]:
        """
        CRITICAL FIX: Global dominance arbitration to prevent worse niches from scaling
        when better niches are in cooldown.
        
        Blueprint Requirement: "No niche can SCALE_UP if another niche has strictly higher
        momentum and efficiency but is throttled due to cooldown."
        
        Args:
            all_scores: List of all niche scores
            
        Returns:
            List of niche scores with arbitration applied
        """
        # Find niches that want to SCALE_UP
        scaling_niches = [s for s in all_scores if s.action == RoutingAction.SCALE_UP]
        
        # Find niches in cooldown/throttle that have high momentum+efficiency
        throttled_niches = [s for s in all_scores if s.action in [RoutingAction.THROTTLE, RoutingAction.PAUSE, RoutingAction.COOL_DOWN]]
        
        # For each scaling niche, check if there's a better throttled niche
        for scaling_niche in scaling_niches:
            scaling_score = scaling_niche.momentum + scaling_niche.efficiency
            
            for throttled_niche in throttled_niches:
                throttled_score = throttled_niche.momentum + throttled_niche.efficiency
                
                # If throttled niche is strictly better, override scaling decision
                if throttled_score > scaling_score * 1.1:  # 10% better threshold
                    # Demote scaling niche to maintain
                    scaling_niche.action = RoutingAction.MAINTAIN
                    scaling_niche.composite = self.maintain_threshold - 0.01
                    
                    # Promote throttled niche to scale up if it passes baseline
                    if self.passes_baseline_gate(throttled_niche.niche):
                        throttled_niche.action = RoutingAction.SCALE_UP
                        throttled_niche.composite = self.scale_threshold + 0.01
                    
                    # Log arbitration decision
                    print(f"GLOBAL ARBITRATION: {throttled_niche.niche} promoted over {scaling_niche.niche} (better momentum+efficiency)")
        
        return all_scores