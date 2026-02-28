"""
budget_allocator.py - CAPITAL ALLOCATION ENGINE

THIS FILE ANSWERS ONE QUESTION ONLY:
Given constraints, who gets capital and who does not?

THIS FILE DOES NOT:
- Learn
- Remember  
- Predict
- Evaluate platforms
- Evaluate content
- Shape RL rewards
- Track overrides
- Perform analytics
- Perform diagnostics
- Perform governance
- Perform compliance

THIS FILE ONLY:
- Applies hard constraints
- Makes irreversible allocation decisions
- Emits executable budgets

SINGLE-WRITER AUTHORITY: Capital allocation only.
"""

import logging
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from enum import Enum
import math
import time
import uuid
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import yaml
from datetime import datetime
import threading
from collections import deque

logger = logging.getLogger(__name__)


class RoutingState(Enum):
    """Hard constraint routing states - NON-NEGOTIABLE"""
    SCALE_UP = "SCALE_UP"      # Push capital aggressively
    MAINTAIN = "MAINTAIN"      # Hold current allocation
    THROTTLE = "THROTTLE"      # Reduce allocation
    PAUSE = "PAUSE"            # ZERO BUDGET - TEMPORARY
    KILL = "KILL"              # ZERO BUDGET - PERMANENT


class QualityTier(Enum):
    """Content quality tiers with capital cost implications"""
    PROBE = "PROBE"           # $500-2K per video - Exploration
    STANDARD = "STANDARD"     # $2K-5K per video - Baseline
    PREMIUM = "PREMIUM"       # $5K-15K per video - High investment
    ELITE = "ELITE"           # $15K+ per video - Maximum investment



@dataclass
class Budget:
    """EXECUTION ARTIFACT - This is what systems obey
    
    ONLY contains executable fields. No analytics, no memory, no reasoning.
    Downstream systems consume this, they do not influence it.
    """
    # Generation allocation (ENFORCED)
    max_videos_per_day: int
    allowed_quality_tiers: List[QualityTier]
    max_tokens_per_video: int
    
    # Compute allocation (ENFORCED)
    compute_priority: float          # 0.0 (best-effort) to 1.0 (highest priority)
    max_compute_units: float
    inference_stack_access: bool
    
    # Posting allocation (ENFORCED)
    max_posts_per_day: int
    platform_caps: Dict[str, int]
    
    # Risk & retry allocation (ENFORCED)
    retry_policy: Dict[str, Any]
    kill_threshold: float
    
    # EXECUTION STATE ONLY
    niche: str
    routing_state: RoutingState
    scale_multiplier: float
    is_killed: bool = False
    execution_timestamp: float = field(default_factory=time.time)
    allocation_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # Unique ID for traceability
    correlation_id: str = ""  # Cross-system correlation ID for audit trails
    audit_timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class AllocationResult:
    """IMMUTABLE ALLOCATION RESULT - This is what downstream systems receive"""
    timestamp: float
    budgets: Dict[str, Budget]
    global_constraints_applied: bool
    total_compute_allocated: float
    total_tokens_allocated: int
    total_videos_allocated: int
    niches_killed: List[str]
    niches_starved: List[str]
    execution_time_ms: float
    allocation_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # Unique ID for traceability
    correlation_id: str = ""  # Cross-system correlation ID for audit trails
    audit_timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class CapitalAllocator:
    """
    CAPITAL ALLOCATION ENGINE - Single-writer authority.
    
    Answers ONE question: Given constraints, who gets capital and who does not?
    
    This file does NOT think, learn, predict, or evaluate.
    This file ONLY applies constraints and makes allocation decisions.
    """
    
    # HARD CONSTRAINTS (NON-NEGOTIABLE)
    BASELINE_5M_THRESHOLD = 5_000_000
    BASELINE_AGE_DAYS = 30
    TIME_BASED_KILL_DAYS = 45
    MAX_COST_PER_MILLION_VIEWS = 50.0
    MIN_VELOCITY_THRESHOLD = 100.0
    
    # ALLOCATION THRESHOLDS (SIMPLE GATES)
    MIN_CLEARANCE_RATE_TO_SCALE = 0.30
    AGGRESSIVE_CLEARANCE_THRESHOLD = 0.50
    ELITE_CLEARANCE_THRESHOLD = 0.80
    MAX_SCALE_MULTIPLIER = 5.0
    
    # PERFORMANCE SETTINGS
    MAX_CONCURRENT_ALLOCATIONS = 50  # For async processing
    ALLOCATION_TIMEOUT_SECONDS = 30  # Timeout for large allocations
    
    # ENTERPRISE-GRADE SETTINGS
    ANOMALY_DETECTION_WINDOW = 10  # Number of recent allocations to analyze
    ANOMALY_THRESHOLD_SCORE = 0.7  # Threshold for automatic anomaly alerts
    ASYNC_LOGGING_BATCH_SIZE = 100  # Batch size for async logging
    CORRELATION_ID_PREFIX = "ALLOC"  # Prefix for correlation IDs
    
    # INPUT SANITIZATION
    MIN_SCALE_MULTIPLIER = 0.0
    MAX_PLATFORM_PRESSURE = 10.0
    REQUIRED_METRICS = [
        "baseline_clearance_rate",
        "momentum_score", 
        "median_views",
        "niche_age_days",
        "cost_per_view",
        "velocity_score"
    ]
    
    # CONFIG-DRIVEN MULTIPLIERS (ELIMINATE MAGIC NUMBERS)
    MOMENTUM_BOOST_HIGH = 1.2
    MOMENTUM_PENALTY_LOW = 0.7
    EVERGREEN_BOOST_HIGH = 1.3
    EVERGREEN_PENALTY_LOW = 0.8
    SLOW_BURN_BOOST_HIGH = 1.2
    COST_PENALTY_HIGH = 0.5
    COST_PENALTY_MEDIUM = 0.8
    ROUTING_SCALE_UP_BOOST = 1.5
    ROUTING_THROTTLE_FACTOR = 0.7
    ROUTING_MINIMUM_SCALE = 0.1
    
    # GLOBAL CONSTRAINTS
    DEFAULT_MAX_DAILY_COMPUTE = 1000.0
    DEFAULT_MAX_DAILY_TOKENS = 10_000_000
    DEFAULT_MAX_PARALLEL_GENERATIONS = 50
    
    # BASE ALLOCATION
    BASE_VIDEOS_PER_DAY = 2
    BASE_TOKENS_PER_VIDEO = 25_000
    BASE_COMPUTE_UNITS = 1.0
    
    def __init__(
        self,
        routing_instructions: Dict[str, str],
        global_constraints: Optional[Dict[str, Any]] = None,
        config_file_path: Optional[str] = None
    ):
        """
        Initialize the production allocator.
        
        Args:
            routing_instructions: Hard routing states for each niche
            global_constraints: System-wide resource limits (overrides config)
            config_file_path: Path to YAML config file for global constraints
        """
        self.routing_instructions = routing_instructions
        
        # CONFIG-DRIVEN GLOBAL CONSTRAINTS
        self.global_config = self._load_global_config(config_file_path)
        
        # GLOBAL CONSTRAINTS (CONFIG-DRIVEN WITH FALLBACKS)
        constraints = global_constraints or {}
        self.max_daily_compute = constraints.get("max_daily_compute", self.global_config.get("max_daily_compute", self.DEFAULT_MAX_DAILY_COMPUTE))
        self.max_daily_tokens = constraints.get("max_daily_tokens", self.global_config.get("max_daily_tokens", self.DEFAULT_MAX_DAILY_TOKENS))
        self.max_parallel_generations = constraints.get("max_parallel_generations", self.global_config.get("max_parallel_generations", self.DEFAULT_MAX_PARALLEL_GENERATIONS))
        self.max_posts_per_platform = constraints.get("max_posts_per_platform", self.global_config.get("max_posts_per_platform", {
            "youtube": 50,
            "tiktok": 100,
            "instagram": 100,
            "twitter": 30
        }))
        self.risk_ceiling = constraints.get("risk_ceiling", self.global_config.get("risk_ceiling", 0.8))
        
        # EXECUTION STATE
        self.last_allocation_result: Optional[AllocationResult] = None
        
        # PERFORMANCE OPTIMIZATION
        self.thread_pool = ThreadPoolExecutor(max_workers=self.MAX_CONCURRENT_ALLOCATIONS)
        
        # ENTERPRISE-GRADE FEATURES
        self.correlation_id_counter = 0
        self.allocation_history: deque = deque(maxlen=self.ANOMALY_DETECTION_WINDOW)
        self.async_log_queue: List[Dict[str, Any]] = []
        self.log_lock = threading.Lock()
        self.anomaly_alerts: List[Dict[str, Any]] = []
        
        logger.info(f"CapitalAllocator initialized: {len(routing_instructions)} niches, config-driven constraints loaded")

    def _load_global_config(self, config_file_path: Optional[str]) -> Dict[str, Any]:
        """
        CONFIG-DRIVEN: Load global constraints from YAML config file.
        
        Args:
            config_file_path: Path to YAML config file
            
        Returns:
            Dict containing global constraints
        """
        default_config = {
            "max_daily_compute": self.DEFAULT_MAX_DAILY_COMPUTE,
            "max_daily_tokens": self.DEFAULT_MAX_DAILY_TOKENS,
            "max_parallel_generations": self.DEFAULT_MAX_PARALLEL_GENERATIONS,
            "max_posts_per_platform": {
                "youtube": 50,
                "tiktok": 100,
                "instagram": 100,
                "twitter": 30
            },
            "risk_ceiling": 0.8
        }
        
        if config_file_path is None:
            logger.info("No config file provided, using default global constraints")
            return default_config
        
        try:
            with open(config_file_path, 'r') as f:
                config = yaml.safe_load(f)
                logger.info(f"Loaded global config from {config_file_path}")
                return {**default_config, **config}
        except FileNotFoundError:
            logger.warning(f"Config file {config_file_path} not found, using defaults")
            return default_config
        except yaml.YAMLError as e:
            logger.error(f"Error parsing config file {config_file_path}: {e}, using defaults")
            return default_config
        except Exception as e:
                logger.error(f"Unexpected error loading config {config_file_path}: {e}, using defaults")
                return default_config

    def _apply_routing_signal_integration(self, niche: str, routing_state: RoutingState, scale_multiplier: float) -> float:
        """
        EXPLICIT ROUTING SIGNAL INTEGRATION - Map routing states to budget behavior (CONFIG-DRIVEN).
        
        Args:
            niche: Niche identifier
            routing_state: Hard routing constraint
            scale_multiplier: Calculated scale multiplier
            
        Returns:
            Adjusted scale multiplier based on routing state
        """
        if routing_state == RoutingState.SCALE_UP:
            # Push capital aggressively - boost scale multiplier (CONFIG-DRIVEN)
            return min(self.MAX_SCALE_MULTIPLIER, scale_multiplier * self.ROUTING_SCALE_UP_BOOST)
        elif routing_state == RoutingState.MAINTAIN:
            # Hold current allocation - preserve scale multiplier
            return scale_multiplier
        elif routing_state == RoutingState.THROTTLE:
            # Reduce allocation - throttle scale multiplier (CONFIG-DRIVEN)
            return max(self.ROUTING_MINIMUM_SCALE, scale_multiplier * self.ROUTING_THROTTLE_FACTOR)
        elif routing_state == RoutingState.PAUSE:
            # ZERO BUDGET - kill scale multiplier
            return 0.0
        else:
            # Unknown routing state - conservative approach (CONFIG-DRIVEN)
            logger.warning(f"Unknown routing state {routing_state} for niche {niche}, using conservative allocation")
            return max(self.ROUTING_MINIMUM_SCALE, scale_multiplier * self.ROUTING_THROTTLE_FACTOR)

    def execute_allocation(
        self,
        factory_metrics: Dict[str, Dict[str, Any]],
        long_tail_metrics: Dict[str, Dict[str, Any]],
        correlation_id: Optional[str] = None
    ) -> AllocationResult:
        """
        EXECUTE ALLOCATION - Apply constraints and make allocation decisions.
        
        CRUEL BY DESIGN: No optimism, no retries after failure, no soft degradation.
        ENTERPRISE-GRADE: Correlation tracking, input sanitization, anomaly detection.
        """
        start_time = time.time()
        
        # GENERATE CORRELATION ID FOR ENTERPRISE TRACEABILITY
        if correlation_id is None:
            self.correlation_id_counter += 1
            correlation_id = f"{self.CORRELATION_ID_PREFIX}-{self.correlation_id_counter:06d}"
        
        # INPUT SANITIZATION AND VALIDATION
        sanitized_factory_metrics = self._sanitize_and_validate_inputs(factory_metrics, correlation_id)
        sanitized_long_tail_metrics = self._sanitize_and_validate_inputs(long_tail_metrics, correlation_id)
        
        budgets: Dict[str, Budget] = {}
        niches_killed: List[str] = []
        niches_starved: List[str] = []
        
        total_compute = 0.0
        total_tokens = 0
        total_videos = 0
        
        # APPLY CONSTRAINTS TO EACH NICHE
        for niche in self.routing_instructions.keys():
            routing_state = RoutingState(self.routing_instructions[niche])
            metrics = sanitized_factory_metrics.get(niche, {})
            
            # HARD ROUTING ENFORCEMENT
            if routing_state == RoutingState.KILL:
                budget = self._create_killed_budget(niche, routing_state)
                budget.correlation_id = correlation_id
                budgets[niche] = budget
                niches_killed.append(niche)
                continue
            
            if routing_state == RoutingState.PAUSE:
                budget = self._create_paused_budget(niche, routing_state)
                budget.correlation_id = correlation_id
                budgets[niche] = budget
                niches_starved.append(niche)
                continue
            
            # IRREVERSIBLE KILL DECISIONS (NO OPTIMISM)
            if self._violates_5m_baseline(niche, metrics):
                budget = self._create_killed_budget(niche, routing_state, "5M baseline")
                budget.correlation_id = correlation_id
                budgets[niche] = budget
                niches_killed.append(niche)
                continue
            
            if self._should_kill_by_age(niche, metrics):
                budget = self._create_killed_budget(niche, routing_state, "Time-based death")
                budget.correlation_id = correlation_id
                budgets[niche] = budget
                niches_killed.append(niche)
                continue
            
            if self._should_kill_by_cost(niche, metrics):
                budget = self._create_killed_budget(niche, routing_state, "Cost violation")
                budget.correlation_id = correlation_id
                budgets[niche] = budget
                niches_killed.append(niche)
                continue
            
            if self._should_kill_by_velocity(niche, metrics):
                budget = self._create_killed_budget(niche, routing_state, "Velocity violation")
                budget.correlation_id = correlation_id
                budgets[niche] = budget
                niches_killed.append(niche)
                continue
            
            # NO LONG-TAIL OPTIMISM - Immediate kill on poor performance
            if self._should_kill_by_performance(niche, metrics):
                budget = self._create_killed_budget(niche, routing_state, "Poor performance")
                budget.correlation_id = correlation_id
                budgets[niche] = budget
                niches_killed.append(niche)
                continue
            
            # CORE DECISION: SCALE CALCULATION (VISUALLY DOMINANT)
            scale_multiplier = self._calculate_scale(niche, metrics)
            
            # EXPLICIT ROUTING SIGNAL INTEGRATION
            scale_multiplier = self._apply_routing_signal_integration(niche, routing_state, scale_multiplier)
            
            # VALIDATE SCALE MULTIPLIER (PREVENT OVER-ALLOCATION)
            scale_multiplier = self._validate_scale_multiplier(scale_multiplier, niche)
            
            # CORE DECISION: BUDGET GENERATION (VISUALLY DOMINANT)
            budget = self._generate_budget(niche, routing_state, scale_multiplier, metrics)
            budget.correlation_id = correlation_id
            
            budgets[niche] = budget
            total_compute += budget.max_compute_units
            total_tokens += budget.max_tokens_per_video * budget.max_videos_per_day
            total_videos += budget.max_videos_per_day
        
        # ENFORCE GLOBAL CONSTRAINTS
        budgets, total_compute, total_tokens, total_videos = self._enforce_global_constraints(
            budgets, total_compute, total_tokens, total_videos
        )
        
        # CREATE RESULT WITH CORRELATION ID
        execution_time = (time.time() - start_time) * 1000
        result = AllocationResult(
            timestamp=start_time,
            budgets=budgets,
            global_constraints_applied=True,
            total_compute_allocated=total_compute,
            total_tokens_allocated=total_tokens,
            total_videos_allocated=total_videos,
            niches_killed=niches_killed,
            niches_starved=niches_starved,
            execution_time_ms=execution_time,
            correlation_id=correlation_id
        )
        
        # ENTERPRISE-GRADE: ANOMALY DETECTION AND ASYNC LOGGING
        self._detect_anomalies_and_log(result, correlation_id)
        
        self.last_allocation_result = result
        return result

    async def execute_allocation_async(
        self,
        factory_metrics: Dict[str, Dict[str, Any]],
        long_tail_metrics: Dict[str, Dict[str, Any]]
    ) -> AllocationResult:
        """
        ASYNC ALLOCATION - For large-scale allocation (hundreds of niches).
        
        Reduces blocking time for large allocation runs.
        """
        loop = asyncio.get_event_loop()
        
        # Use thread pool for CPU-bound allocation logic
        with ThreadPoolExecutor(max_workers=self.MAX_CONCURRENT_ALLOCATIONS) as executor:
            future = loop.run_in_executor(
                self.execute_allocation, 
                factory_metrics, 
                long_tail_metrics
            )
            
            try:
                result = await asyncio.wait_for(future, timeout=self.ALLOCATION_TIMEOUT_SECONDS)
                return result
            except asyncio.TimeoutError:
                logger.error(f"Allocation timeout after {self.ALLOCATION_TIMEOUT_SECONDS} seconds")
                # Return minimal safe allocation on timeout
                return self._create_timeout_allocation()

    def _validate_scale_multiplier(self, scale_multiplier: float, niche: str) -> float:
        """Validate and cap scale multiplier to prevent over-allocation."""
        if scale_multiplier < 0:
            logger.warning(f"Negative scale multiplier for {niche}: {scale_multiplier}, setting to 0")
            return 0.0
        
        if scale_multiplier > self.MAX_SCALE_MULTIPLIER:
            logger.warning(f"Scale multiplier {scale_multiplier} exceeds max {self.MAX_SCALE_MULTIPLIER} for {niche}, capping")
            return self.MAX_SCALE_MULTIPLIER
        
        if math.isnan(scale_multiplier) or math.isinf(scale_multiplier):
            logger.warning(f"Invalid scale multiplier {scale_multiplier} for {niche}, setting to 1.0")
            return 1.0
        
        return scale_multiplier

    def _sanitize_and_validate_inputs(self, metrics: Dict[str, Dict[str, Any]], correlation_id: str) -> Dict[str, Dict[str, Any]]:
        """ENTERPRISE-GRADE: Sanitize and validate input metrics."""
        sanitized = {}
        
        for niche, niche_metrics in metrics.items():
            sanitized_niche = {}
            
            # Validate required metrics
            for required_metric in self.REQUIRED_METRICS:
                if required_metric not in niche_metrics:
                    logger.warning(f"[{correlation_id}] Missing required metric {required_metric} for niche {niche}")
                    sanitized_niche[required_metric] = 0.0  # Default value
                else:
                    value = niche_metrics[required_metric]
                    sanitized_niche[required_metric] = self._sanitize_metric_value(value, required_metric, niche, correlation_id)
            
            # Sanitize optional metrics
            for metric, value in niche_metrics.items():
                if metric not in self.REQUIRED_METRICS:
                    sanitized_niche[metric] = self._sanitize_metric_value(value, metric, niche, correlation_id)
            
            sanitized[niche] = sanitized_niche
        
        return sanitized

    def _sanitize_metric_value(self, value: Any, metric_name: str, niche: str, correlation_id: str) -> float:
        """ENTERPRISE-GRADE: Sanitize individual metric values."""
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            logger.warning(f"[{correlation_id}] Invalid metric value {value} for {metric_name} in {niche}, using 0.0")
            return 0.0
        
        # Handle NaN and infinity
        if math.isnan(numeric_value) or math.isinf(numeric_value):
            logger.warning(f"[{correlation_id}] NaN/Inf metric value {numeric_value} for {metric_name} in {niche}, using 0.0")
            return 0.0
        
        # Apply metric-specific bounds
        if "scale" in metric_name.lower() or "multiplier" in metric_name.lower():
            return max(self.MIN_SCALE_MULTIPLIER, min(numeric_value, self.MAX_SCALE_MULTIPLIER))
        elif "pressure" in metric_name.lower():
            return max(0.0, min(numeric_value, self.MAX_PLATFORM_PRESSURE))
        elif "rate" in metric_name.lower():
            return max(0.0, min(numeric_value, 1.0))
        elif "views" in metric_name.lower():
            return max(0.0, min(numeric_value, 1_000_000_000))  # 1B max views
        elif "cost" in metric_name.lower():
            return max(0.0, min(numeric_value, 1000.0))  # $1000 max cost
        else:
            return max(0.0, min(numeric_value, 1_000_000))  # General cap
    
    def _detect_anomalies_and_log(self, result: AllocationResult, correlation_id: str) -> None:
        """ENTERPRISE-GRADE: Detect anomalies and queue async logging."""
        # Add to allocation history for anomaly detection
        self.allocation_history.append({
            'timestamp': result.timestamp,
            'correlation_id': correlation_id,
            'total_compute': result.total_compute_allocated,
            'total_tokens': result.total_tokens_allocated,
            'total_videos': result.total_videos_allocated,
            'niches_killed': len(result.niches_killed),
            'niches_starved': len(result.niches_starved)
        })
        
        # Detect anomalies
        anomaly_score = self._calculate_anomaly_score(result)
        if anomaly_score > self.ANOMALY_THRESHOLD_SCORE:
            alert = {
                'timestamp': time.time(),
                'correlation_id': correlation_id,
                'anomaly_score': anomaly_score,
                'allocation_id': result.allocation_id,
                'alert_type': 'HIGH_ANOMALY_DETECTED',
                'details': {
                    'total_compute': result.total_compute_allocated,
                    'niches_killed': len(result.niches_killed),
                    'execution_time_ms': result.execution_time_ms
                }
            }
            self.anomaly_alerts.append(alert)
            logger.error(f"[{correlation_id}] ANOMALY ALERT: Score {anomaly_score:.3f} - {len(result.niches_killed)} niches killed")
        
        # Queue async logging
        log_entry = {
            'timestamp': time.time(),
            'correlation_id': correlation_id,
            'allocation_id': result.allocation_id,
            'event_type': 'ALLOCATION_COMPLETED',
            'total_compute': result.total_compute_allocated,
            'total_tokens': result.total_tokens_allocated,
            'total_videos': result.total_videos_allocated,
            'niches_killed': len(result.niches_killed),
            'niches_starved': len(result.niches_starved),
            'execution_time_ms': result.execution_time_ms
        }
        
        with self.log_lock:
            self.async_log_queue.append(log_entry)
            
            # Batch process async logs if queue is full
            if len(self.async_log_queue) >= self.ASYNC_LOGGING_BATCH_SIZE:
                self._process_async_log_batch()
    
    def _calculate_anomaly_score(self, result: AllocationResult) -> float:
        """ENTERPRISE-GRADE: Calculate anomaly detection score."""
        if len(self.allocation_history) < 3:
            return 0.0  # Not enough history
        
        # Get recent allocations
        recent_allocations = list(self.allocation_history)[-3:]
        
        # Calculate anomalies
        anomalies = 0.0
        total_checks = 0
        
        # Check compute anomalies
        compute_values = [a['total_compute'] for a in recent_allocations]
        if len(compute_values) >= 2:
            compute_change = abs(compute_values[-1] - compute_values[-2]) / max(1, compute_values[-2])
            if compute_change > 0.5:  # 50% change is anomalous
                anomalies += 1.0
            total_checks += 1.0
        
        # Check kill ratio anomalies
        kill_ratios = [a['niches_killed'] / max(1, len(self.routing_instructions)) for a in recent_allocations]
        if kill_ratios[-1] > 0.3:  # More than 30% killed is anomalous
            anomalies += 1.0
        total_checks += 1.0
        
        # Check execution time anomalies
        execution_times = [a.get('execution_time_ms', 0) for a in recent_allocations]
        if len(execution_times) >= 2:
            time_change = abs(execution_times[-1] - execution_times[-2]) / max(1, execution_times[-2])
            if time_change > 2.0:  # 200% time increase is anomalous
                anomalies += 1.0
            total_checks += 1.0
        
        return anomalies / max(1, total_checks)
    
    def _process_async_log_batch(self) -> None:
        """ENTERPRISE-GRADE: Process async logging batch."""
        if not self.async_log_queue:
            return
        
        # In production, this would send to external logging system
        # For now, we'll just log to the local logger
        batch_size = len(self.async_log_queue)
        logger.info(f"Processing async log batch of {batch_size} entries")
        
        # Clear the queue
        with self.log_lock:
            self.async_log_queue.clear()
    
    def get_anomaly_alerts(self, limit: int = 10) -> List[Dict[str, Any]]:
        """ENTERPRISE-GRADE: Get recent anomaly alerts."""
        return self.anomaly_alerts[-limit:] if self.anomaly_alerts else []
    
    def flush_async_logs(self) -> None:
        """ENTERPRISE-GRADE: Force flush async log queue."""
        self._process_async_log_batch()

    # ================================
    # CONSTRAINT APPLICATION (NO THINKING)
    # ================================
    # REVIEWERS FIND CORE DECISIONS IN UNDER 30 SECONDS

    def _violates_5m_baseline(self, niche: str, metrics: Dict[str, Any]) -> bool:
        """Apply 5M baseline constraint."""
        median_views = metrics.get("median_views", 0)
        niche_age_days = metrics.get("niche_age_days", 0)
        return median_views < self.BASELINE_5M_THRESHOLD and niche_age_days > self.BASELINE_AGE_DAYS

    def _should_kill_by_age(self, niche: str, metrics: Dict[str, Any]) -> bool:
        """Apply time-based death constraint."""
        median_views = metrics.get("median_views", 0)
        niche_age_days = metrics.get("niche_age_days", 0)
        return niche_age_days > self.TIME_BASED_KILL_DAYS and median_views < self.BASELINE_5M_THRESHOLD

    def _should_kill_by_cost(self, niche: str, metrics: Dict[str, Any]) -> bool:
        """Apply cost constraint."""
        cost_per_view = metrics.get("cost_per_view", 0.0)
        cost_per_million = cost_per_view * 1_000_000
        return cost_per_million > self.MAX_COST_PER_MILLION_VIEWS

    def _should_kill_by_velocity(self, niche: str, metrics: Dict[str, Any]) -> bool:
        """Apply velocity constraint."""
        velocity = metrics.get("velocity_score", 0.0)
        clearance_rate = metrics.get("baseline_clearance_rate", 0.0)
        return velocity < self.MIN_VELOCITY_THRESHOLD and clearance_rate < 0.2

    def _should_kill_by_performance(self, niche: str, metrics: Dict[str, Any]) -> bool:
        """CRUEL: Immediate kill on poor performance - NO OPTIMISM."""
        clearance_rate = metrics.get("baseline_clearance_rate", 0.0)
        momentum = metrics.get("momentum_score", 0.0)
        
        # Immediate death for poor performance - no second chances
        return clearance_rate < 0.15 and momentum < -0.3

    def _calculate_scale(self, niche: str, metrics: Dict[str, Any]) -> float:
        """
        CORE DECISION: SCALE CALCULATION (VISUALLY DOMINANT)
        
        EXACT SPEC FORMULA: scale_multiplier = f(clearance_rate) * g(momentum) * h(ceiling_estimate) * risk_adjustment
        """
        clearance_rate = metrics.get("baseline_clearance_rate", 0.0)
        momentum_score = metrics.get("momentum_score", 0.0)  # First & second derivative of views
        evergreen_probability = metrics.get("evergreen_probability", 0.0)  # Long-tail projection
        slow_burn_rate = metrics.get("slow_burn_rate", 0.0)  # Long-tail projection
        cost_per_view = metrics.get("cost_per_view", 0.0)
        
        # f(clearance_rate) - Base scaling function
        if clearance_rate < self.MIN_CLEARANCE_RATE_TO_SCALE:
            f_clearance = 0.0  # Kill
        elif clearance_rate < self.AGGRESSIVE_CLEARANCE_THRESHOLD:
            f_clearance = 0.5  # Coast
        elif clearance_rate < self.ELITE_CLEARANCE_THRESHOLD:
            f_clearance = 1.5  # Scale
        else:
            f_clearance = 3.0  # Push HARD
        
        # g(momentum) - Momentum derivative function (CONFIG-DRIVEN)
        if momentum_score > 0.5:
            g_momentum = self.MOMENTUM_BOOST_HIGH  # Positive momentum boost
        elif momentum_score < -0.5:
            g_momentum = self.MOMENTUM_PENALTY_LOW  # Negative momentum penalty
        else:
            g_momentum = 1.0  # Neutral momentum
        
        # h(ceiling_estimate) - Long-tail ceiling function (CONFIG-DRIVEN)
        if evergreen_probability > 0.8:
            h_ceiling = self.EVERGREEN_BOOST_HIGH  # High evergreen probability boost
        elif slow_burn_rate > 0.6:
            h_ceiling = self.SLOW_BURN_BOOST_HIGH  # High slow burn rate boost
        elif evergreen_probability < 0.2:
            h_ceiling = self.EVERGREEN_PENALTY_LOW  # Low evergreen probability penalty
        else:
            h_ceiling = 1.0  # Neutral ceiling
        
        # risk_adjustment - Cost-based risk adjustment (CONFIG-DRIVEN)
        cost_per_million = cost_per_view * 1_000_000
        if cost_per_million > self.MAX_COST_PER_MILLION_VIEWS:
            risk_adjustment = self.COST_PENALTY_HIGH  # High cost penalty
        elif cost_per_million > self.MAX_COST_PER_MILLION_VIEWS * 0.5:
            risk_adjustment = self.COST_PENALTY_MEDIUM  # Medium cost penalty
        else:
            risk_adjustment = 1.0  # Normal risk
        
        # EXACT SPEC FORMULA IMPLEMENTATION
        scale_multiplier = f_clearance * g_momentum * h_ceiling * risk_adjustment
        
        # Apply hard caps per spec
        return min(self.MAX_SCALE_MULTIPLIER, max(0.0, scale_multiplier))

    def _generate_budget(self, niche: str, routing_state: RoutingState, scale_multiplier: float, metrics: Dict[str, Any]) -> Budget:
        """
        CONSOLIDATED BUDGET GENERATION - Single, clear method for all budget creation.
        
        Args:
            niche: Niche identifier
            routing_state: Hard routing state
            scale_multiplier: Calculated and validated scale multiplier
            metrics: Sanitized performance metrics
            
        Returns:
            Executable budget with all allocation decisions applied
        """
        # BASE ALLOCATION
        scaled_videos = max(1, int(self.BASE_VIDEOS_PER_DAY * scale_multiplier))
        scaled_tokens = max(1000, int(self.BASE_TOKENS_PER_VIDEO * scale_multiplier))
        scaled_compute = self.BASE_COMPUTE_UNITS * scale_multiplier
        
        # 5M BASELINE ENFORCEMENT
        median_views = metrics.get("median_views", 0)
        if median_views < self.BASELINE_5M_THRESHOLD:
            scaled_videos = min(scaled_videos, 1)
            scaled_tokens = min(scaled_tokens, self.BASE_TOKENS_PER_VIDEO // 2)
            scaled_compute = min(scaled_compute, 0.5)
        
        # QUALITY TIERS (CONSOLIDATED LOGIC)
        if scale_multiplier >= 2.0:
            allowed_tiers = [QualityTier.STANDARD, QualityTier.PREMIUM]
        elif scale_multiplier >= 1.0:
            allowed_tiers = [QualityTier.PROBE, QualityTier.STANDARD]
        else:
            allowed_tiers = [QualityTier.PROBE]
        
        # COMPUTE PRIORITY (CONSOLIDATED LOGIC)
        compute_priority = min(1.0, scale_multiplier / 3.0)
        inference_access = scale_multiplier >= 1.5
        
        # POSTING ALLOCATION (CONSOLIDATED LOGIC)
        max_posts = max(1, scaled_videos // 2)
        platform_caps = self._calculate_platform_caps(niche, scale_multiplier, metrics)
        
        # RETRY POLICY (CONSOLIDATED LOGIC)
        if scale_multiplier >= 2.0:
            max_retries = 3
            required_delta = 0.10
        elif scale_multiplier >= 1.0:
            max_retries = 2
            required_delta = 0.15
        else:
            max_retries = 0  # NO RETRIES FOR POOR PERFORMERS
            required_delta = 0.25
        
        retry_policy = {
            "max_retries": max_retries,
            "required_delta": required_delta,
            "mutation_strength": 0.8
        }
        
        # KILL THRESHOLD (CONSOLIDATED LOGIC)
        kill_threshold = 0.8 if scale_multiplier >= 1.0 else 0.5
        
        return Budget(
            max_videos_per_day=scaled_videos,
            allowed_quality_tiers=allowed_tiers,
            max_tokens_per_video=scaled_tokens,
            compute_priority=compute_priority,
            max_compute_units=scaled_compute,
            inference_stack_access=inference_access,
            max_posts_per_day=max_posts,
            platform_caps=platform_caps,
            retry_policy=retry_policy,
            kill_threshold=kill_threshold,
            niche=niche,
            routing_state=routing_state,
            scale_multiplier=scale_multiplier
        )

    def _calculate_platform_caps(self, niche: str, scale_multiplier: float, metrics: Dict[str, Any]) -> Dict[str, int]:
        """
        CONSOLIDATED: Calculate platform-specific posting caps.
        
        Args:
            niche: Niche identifier
            scale_multiplier: Scale multiplier for allocation
            metrics: Performance metrics
            
        Returns:
            Platform-specific posting caps
        """
        platform_caps = {}
        
        # Apply global platform caps with scale-based adjustments
        for platform, base_cap in self.max_posts_per_platform.items():
            if scale_multiplier >= 2.0:
                platform_caps[platform] = int(base_cap * 1.2)  # 20% boost for high performers
            elif scale_multiplier >= 1.0:
                platform_caps[platform] = int(base_cap)
            else:
                platform_caps[platform] = int(base_cap * 0.5)  # 50% reduction for low performers
        
        return platform_caps

    # ================================
    # UTILITIES (MINIMAL)
    # ================================

    def _enforce_global_constraints(
        self,
        budgets: Dict[str, Budget],
        total_compute: float,
        total_tokens: int,
        total_videos: int
    ) -> Tuple[Dict[str, Budget], float, int, int]:
        """
        ENHANCED GLOBAL CONSTRAINTS - Enforce scarcity with priority rebalancing for fairness.
        
        Args:
            budgets: Current budget allocations
            total_compute: Total compute allocated
            total_tokens: Total tokens allocated
            total_videos: Total videos allocated
            
        Returns:
            Adjusted budgets and totals with global constraints enforced
        """
        # Check and enforce global compute constraint with priority rebalancing
        if total_compute > self.max_daily_compute:
            budgets, total_compute = self._rebalance_compute_constraints(budgets, total_compute)
        
        # Check and enforce global token constraint with priority rebalancing
        if total_tokens > self.max_daily_tokens:
            budgets, total_tokens = self._rebalance_token_constraints(budgets, total_tokens)
        
        return budgets, total_compute, total_tokens, total_videos

    def _rebalance_compute_constraints(self, budgets: Dict[str, Budget], total_compute: float) -> Tuple[Dict[str, Budget], float]:
        """
        REBALANCE: Fair compute constraint enforcement with priority preservation.
        
        Args:
            budgets: Current budget allocations
            total_compute: Total compute allocated
            
        Returns:
            Rebalanced budgets and compute totals
        """
        # Calculate reduction factor
        reduction_factor = self.max_daily_compute / total_compute
        
        # Sort budgets by priority (high priority gets preferential treatment)
        active_budgets = [(niche, budget) for niche, budget in budgets.items() if not budget.is_killed]
        active_budgets.sort(key=lambda x: x[1].compute_priority, reverse=True)
        
        # Apply progressive reduction based on priority
        remaining_compute = self.max_daily_compute
        rebalanced_budgets = {}
        
        for niche, budget in active_budgets:
            if remaining_compute <= 0:
                # No compute left, kill remaining budgets
                budget.max_compute_units = 0.0
                budget.compute_priority = 0.0
                budget.inference_stack_access = False
            else:
                # Allocate compute based on priority
                if budget.compute_priority >= 0.8:  # High priority - preserve more
                    allocated_compute = min(budget.max_compute_units, remaining_compute * 0.6)
                elif budget.compute_priority >= 0.5:  # Medium priority - moderate reduction
                    allocated_compute = min(budget.max_compute_units * reduction_factor, remaining_compute * 0.3)
                else:  # Low priority - aggressive reduction
                    allocated_compute = min(budget.max_compute_units * reduction_factor * 0.5, remaining_compute * 0.1)
                
                budget.max_compute_units = allocated_compute
                remaining_compute -= allocated_compute
                
                # Adjust inference access based on compute allocation
                budget.inference_stack_access = allocated_compute >= 1.0
            
            rebalanced_budgets[niche] = budget
        
        # Preserve killed budgets
        for niche, budget in budgets.items():
            if budget.is_killed:
                rebalanced_budgets[niche] = budget
        
        new_total_compute = sum(b.max_compute_units for b in rebalanced_budgets.values())
        logger.warning(f"Compute constraint rebalanced: {total_compute:.1f} → {new_total_compute:.1f} (priority-based)")
        
        return rebalanced_budgets, new_total_compute

    def _rebalance_token_constraints(self, budgets: Dict[str, Budget], total_tokens: int) -> Tuple[Dict[str, Budget], int]:
        """
        REBALANCE: Fair token constraint enforcement with priority preservation.
        
        Args:
            budgets: Current budget allocations
            total_tokens: Total tokens allocated
            
        Returns:
            Rebalanced budgets and token totals
        """
        # Calculate reduction factor
        reduction_factor = self.max_daily_tokens / total_tokens
        
        # Sort budgets by scale multiplier (higher scale gets preferential treatment)
        active_budgets = [(niche, budget) for niche, budget in budgets.items() if not budget.is_killed]
        active_budgets.sort(key=lambda x: x[1].scale_multiplier, reverse=True)
        
        # Apply progressive reduction based on scale
        remaining_tokens = self.max_daily_tokens
        rebalanced_budgets = {}
        
        for niche, budget in active_budgets:
            if remaining_tokens <= 0:
                # No tokens left, kill remaining budgets
                budget.max_tokens_per_video = 0
                budget.max_videos_per_day = 0
            else:
                # Allocate tokens based on scale multiplier
                if budget.scale_multiplier >= 2.0:  # High scale - preserve more
                    allocated_tokens = min(budget.max_tokens_per_video, int(remaining_tokens * 0.6 / budget.max_videos_per_day))
                elif budget.scale_multiplier >= 1.0:  # Medium scale - moderate reduction
                    allocated_tokens = min(int(budget.max_tokens_per_video * reduction_factor), int(remaining_tokens * 0.3 / budget.max_videos_per_day))
                else:  # Low scale - aggressive reduction
                    allocated_tokens = min(int(budget.max_tokens_per_video * reduction_factor * 0.5), int(remaining_tokens * 0.1 / budget.max_videos_per_day))
                
                budget.max_tokens_per_video = max(1000, allocated_tokens)  # Minimum 1000 tokens
                remaining_tokens -= allocated_tokens * budget.max_videos_per_day
            
            rebalanced_budgets[niche] = budget
        
        # Preserve killed budgets
        for niche, budget in budgets.items():
            if budget.is_killed:
                rebalanced_budgets[niche] = budget
        
        new_total_tokens = sum(b.max_tokens_per_video * b.max_videos_per_day for b in rebalanced_budgets.values())
        logger.warning(f"Token constraint rebalanced: {total_tokens:,} → {new_total_tokens:,} (scale-based)")
        
        return rebalanced_budgets, new_total_tokens

    def _create_killed_budget(self, niche: str, routing_state: RoutingState, reason: str = "Kill") -> Budget:
        """Create killed budget."""
        return Budget(
            max_videos_per_day=0,
            allowed_quality_tiers=[QualityTier.PROBE],
            max_tokens_per_video=0,
            compute_priority=0.0,
            max_compute_units=0.0,
            inference_stack_access=False,
            max_posts_per_day=0,
            platform_caps={},
            retry_policy={"max_retries": 0, "required_delta": 0.0, "mutation_strength": 0.0},
            kill_threshold=1.0,
            niche=niche,
            routing_state=routing_state,
            scale_multiplier=0.0,
            is_killed=True
        )

    def _create_paused_budget(self, niche: str, routing_state: RoutingState) -> Budget:
        """Create paused budget."""
        return Budget(
            max_videos_per_day=0,
            allowed_quality_tiers=[QualityTier.PROBE],
            max_tokens_per_video=0,
            compute_priority=0.1,
            max_compute_units=0.1,
            inference_stack_access=False,
            max_posts_per_day=0,
            platform_caps={},
            retry_policy={"max_retries": 0, "required_delta": 0.0, "mutation_strength": 0.0},
            kill_threshold=0.5,
            niche=niche,
            routing_state=routing_state,
            scale_multiplier=0.0
        )

    def get_allocation_summary(self) -> Dict[str, Any]:
        """Get allocation summary."""
        if not self.last_allocation_result:
            return {"error": "No allocation executed"}
        
        result = self.last_allocation_result
        active_budgets = [b for b in result.budgets.values() if not b.is_killed]
        
        return {
            "timestamp": result.timestamp,
            "total_niches": len(result.budgets),
            "active_niches": len(active_budgets),
            "killed_niches": len(result.niches_killed),
            "total_videos_allocated": result.total_videos_allocated,
            "total_compute_allocated": result.total_compute_allocated,
            "total_tokens_allocated": result.total_tokens_allocated,
            "execution_time_ms": result.execution_time_ms
        }
        last_compute = self.last_good_allocation.total_compute_allocated
        compute_diff = (current_compute - last_compute) / max(1, last_compute)
        
        current_kills = len(current_result.niches_killed)
        last_kills = len(self.last_good_allocation.niches_killed)
        kill_diff = current_kills - last_kills
        
        logger.info(
            f"SHADOW COMPARISON: compute_diff={compute_diff:+.2%}, "
            f"kill_diff={kill_diff:+d}, "
            f"current_niches={len(current_result.budgets)}"
        )
        
        # Log shadow decision
        decision = AllocationDecisionRecord(
            action="shadow_comparison",
            compute_diff=compute_diff,
            kill_diff=kill_diff,
            current_budgets=len(current_result.budgets),
            timestamp=time.time()
        )
        self.decision_records.append(decision)

    def _log_allocation_decision(self, niche: str, budget: Budget, metrics: Dict[str, Any]) -> None:
        """
        LOG ALLOCATION DECISION - Structured decision logging for accountability.
        """
        decision = AllocationDecisionRecord(
            niche=niche,
            routing_state=budget.routing_state.value,
            scale_multiplier=budget.scale_multiplier,
            kill=budget.is_killed,
            videos_allocated=budget.max_videos_per_day,
            compute_allocated=budget.max_compute_units,
            tokens_allocated=budget.max_tokens_per_video,
            reasons=budget.reasoning,
            inputs_snapshot={
                "clearance_rate": self._clamp_metric(metrics.get("baseline_clearance_rate", 0), "clearance_rate"),
                "momentum_score": self._clamp_metric(metrics.get("momentum_score", 0), "momentum_score"),
                "p95_views": self._clamp_metric(metrics.get("p95_views", 0), "views"),
                "cost_per_view": self._clamp_metric(metrics.get("cost_per_view", 0), "cost_per_view"),
                "median_views": self._clamp_metric(metrics.get("median_views", 0), "views"),
                "niche_age_days": metrics.get("niche_age_days", 0)
            },
            production_metrics={
                "memory_7d": budget.performance_memory.clearance_rate_7d,
                "memory_30d": budget.performance_memory.clearance_rate_30d,
                "platform_pressure": {p: pressure.pressure_score for p, pressure in budget.platform_pressure.items()},
                "content_overrides": len(budget.content_overrides),
                "retry_bid": budget.global_retry_bid,
                "baseline_5m_enforced": budget.baseline_5m_enforced
            }
        )
        
        self.decision_records.append(decision)
        
        # Log to file for auditability
        logger.debug(f"DECISION: {decision.to_json()}")

    def _should_kill_irreversibly(
        self,
        niche: str,
        metrics: Dict[str, Any],
        long_tail: Dict[str, Any]
    ) -> bool:
        """
        IRREVERSIBLE KILL CONDITIONS - These are non-negotiable.
        
        If any of these conditions are met, the niche is killed permanently.
        """
        # CONDITION 1: CPI rising while views flat
        cost_per_view = metrics.get("cost_per_view", 0.0)
        cost_per_million = cost_per_view * 1_000_000
        momentum = metrics.get("momentum_score", 0.0)
        
        if cost_per_million > self.MAX_COST_PER_MILLION_VIEWS and momentum < -0.3:
            logger.warning(f"KILL TRIGGER: {niche} - CPI rising while views flat")
            return True
        
        # CONDITION 2: Engagement collapse after retries
        engagement_rate = metrics.get("avg_engagement_rate", 0.0)
        retry_count = metrics.get("retry_count", 0)
        
        if engagement_rate < 0.01 and retry_count >= 3:
            logger.warning(f"KILL TRIGGER: {niche} - Engagement collapse after retries")
            return True
        
        # CONDITION 3: Velocity below minimum threshold
        velocity = metrics.get("velocity_score", 0.0)
        
        if velocity < self.MIN_VELOCITY_THRESHOLD:
            logger.warning(f"KILL TRIGGER: {niche} - Velocity below threshold")
            return True
        
        # CONDITION 4: Long-tail probability too low
        long_tail_prob = long_tail.get("long_tail_probability", 0.0)
        
        if long_tail_prob < 0.05:
            logger.warning(f"KILL TRIGGER: {niche} - Long-tail probability too low")
            return True
        
        return False

    def _compute_scale_pressure(self, niche: str, metrics: Dict[str, Any]) -> float:
        """
        COMPUTE SCALE PRESSURE - This determines how much capital to allocate.
        
        This is the heart of the allocation engine - it converts performance signals
        into allocation pressure.
        """
        # EXTRACT PERFORMANCE METRICS
        clearance_rate = metrics.get("baseline_clearance_rate", 0.0)
        momentum = metrics.get("momentum_score", 0.0)
        p95_views = metrics.get("p95_views", 0)
        cost_per_view = metrics.get("cost_per_view", 0.0)
        
        # PHASE-BASED REGIME SWITCHING
        if clearance_rate < self.MIN_CLEARANCE_RATE_TO_SCALE:
            # DEFENSIVE: Preserve capital
            phase_multiplier = 0.3
        elif clearance_rate < self.AGGRESSIVE_CLEARANCE_THRESHOLD:
            # NEUTRAL: Balanced approach
            phase_multiplier = 0.8 + (clearance_rate - self.MIN_CLEARANCE_RATE_TO_SCALE) * 0.5
        elif clearance_rate < self.ELITE_CLEARANCE_THRESHOLD:
            # GROWTH: Accelerated scaling
            phase_multiplier = 1.2 + (clearance_rate - self.AGGRESSIVE_CLEARANCE_THRESHOLD) * 2.0
        else:
            # COMPOUNDING: Winner takes capital
            phase_multiplier = 2.0 + (clearance_rate - self.ELITE_CLEARANCE_THRESHOLD) * 10.0
        
        # MOMENTUM COMPOUNDING (Super-linear for winners)
        if momentum > 0.8:
            momentum_factor = 1.0 + (momentum ** 2) * 2.0  # Exponential
        elif momentum > 0.5:
            momentum_factor = 1.0 + (momentum ** 1.5) * 1.0  # Super-linear
        else:
            momentum_factor = 1.0 + momentum * 0.5  # Linear
        
        # CEILING ESTIMATE
        ceiling_estimate = p95_views / 10_000_000  # Normalize to 10M units
        if ceiling_estimate > 10:  # 100M+ potential
            ceiling_factor = 2.0 + (ceiling_estimate - 10) * 0.1
        elif ceiling_estimate > 5:  # 50M+ potential
            ceiling_factor = 1.5 + (ceiling_estimate - 5) * 0.1
        else:
            ceiling_factor = 1.0
        
        # RISK CEILING
        current_risk = metrics.get("risk_score", 0.0)
        risk_ceiling = 0.4 if clearance_rate >= self.ELITE_CLEARANCE_THRESHOLD else 0.2
        if current_risk > risk_ceiling:
            risk_penalty = risk_ceiling / current_risk
        else:
            risk_penalty = 1.0
        
        # COMBINE AND CLAMP
        raw_multiplier = phase_multiplier * momentum_factor * ceiling_factor * risk_penalty
        final_multiplier = max(0.0, min(raw_multiplier, self.MAX_SCALE_MULTIPLIER))
        
        return final_multiplier

    def _compute_base_allocation(self, niche: str, routing_state: RoutingState) -> Dict[str, Any]:
        """
        COMPUTE BASE ALLOCATION - Minimum survival allocation.
        
        This ensures anti-starvation guarantees.
        """
        if routing_state == RoutingState.THROTTLE:
            # Throttled niches get reduced base allocation
            return {
                "max_videos_per_day": 1,
                "allowed_quality_tiers": [QualityTier.PROBE],
                "max_tokens_per_video": self.BASE_TOKENS_PER_VIDEO // 2,
                "compute_units": self.BASE_COMPUTE_UNITS * 0.5
            }
        
        # Standard base allocation for active niches
        return {
            "max_videos_per_day": self.BASE_VIDEOS_PER_DAY,
            "allowed_quality_tiers": [QualityTier.STANDARD],
            "max_tokens_per_video": self.BASE_TOKENS_PER_VIDEO,
            "compute_units": self.BASE_COMPUTE_UNITS
        }

    def _generate_budget(
        self,
        niche: str,
        routing_state: RoutingState,
        base_budget: Dict[str, Any],
        scale_multiplier: float,
        metrics: Dict[str, Any]
    ) -> Budget:
        """
        GENERATE BUDGET - Create the enforceable budget allocation.
        """
        clearance_rate = metrics.get("baseline_clearance_rate", 0.0)
        
        # GENERATION ALLOCATION
        base_videos = base_budget["max_videos_per_day"]
        base_tokens = base_budget["max_tokens_per_video"]
        
        scaled_videos = int(base_videos * scale_multiplier)
        scaled_tokens = int(base_tokens * scale_multiplier)
        
        # ELITE EXCEPTION HANDLING
        if clearance_rate >= self.ELITE_CLEARANCE_THRESHOLD:
            # Elite: Can have both volume AND quality
            if scale_multiplier > 3.0:
                allowed_tiers = [QualityTier.STANDARD, QualityTier.PREMIUM, QualityTier.ELITE]
                max_tokens = scaled_tokens * 1.5  # Quality bonus
                scaled_videos = int(scaled_videos * 0.8)  # Slight volume reduction
            else:
                allowed_tiers = [QualityTier.PREMIUM, QualityTier.ELITE]
                max_tokens = scaled_tokens * 1.2
                scaled_videos = int(scaled_videos * 0.9)
        else:
            # NON-ELITE: Volume OR quality, never both
            if scale_multiplier > 2.0:
                # High volume: Force to lower quality
                allowed_tiers = [QualityTier.PROBE, QualityTier.STANDARD]
                max_tokens = base_tokens  # No quality scaling
                scaled_videos = int(scaled_videos * 1.2)  # Volume bonus
            else:
                # Standard: Balanced approach
                allowed_tiers = [QualityTier.STANDARD]
                max_tokens = scaled_tokens
                # No volume scaling
        
        # COMPUTE ALLOCATION
        if clearance_rate >= self.ELITE_CLEARANCE_THRESHOLD:
            compute_priority = min(0.95, 0.8 + scale_multiplier * 0.1)
            inference_access = True
            latency_priority = 10  # Highest urgency
        elif clearance_rate >= self.AGGRESSIVE_CLEARANCE_THRESHOLD:
            compute_priority = min(0.8, 0.6 + scale_multiplier * 0.1)
            inference_access = True
            latency_priority = 7
        elif clearance_rate >= self.MIN_CLEARANCE_RATE_TO_SCALE:
            compute_priority = min(0.6, 0.4 + scale_multiplier * 0.1)
            inference_access = False
            latency_priority = 4
        else:
            compute_priority = max(0.1, 0.2 + scale_multiplier * 0.05)
            inference_access = False
            latency_priority = 1  # Best effort
        
        # POSTING ALLOCATION
        max_posts = min(10, scaled_videos)
        
        # RETRY POLICY
        if clearance_rate >= self.ELITE_CLEARANCE_THRESHOLD:
            max_retries = 5
            required_delta = 0.05  # 5% improvement required
            mutation_strength = 0.3
        elif clearance_rate >= self.AGGRESSIVE_CLEARANCE_THRESHOLD:
            max_retries = 3
            required_delta = 0.10  # 10% improvement required
            mutation_strength = 0.5
        elif clearance_rate >= self.MIN_CLEARANCE_RATE_TO_SCALE:
            max_retries = 2
            required_delta = 0.15  # 15% improvement required
            mutation_strength = 0.7
        else:
            max_retries = 1
            required_delta = 0.25  # 25% improvement required
            mutation_strength = 0.9
        
        # CREATE BUDGET OBJECT
        budget = Budget(
            # Generation allocation
            max_videos_per_day=max(0, scaled_videos),
            allowed_quality_tiers=allowed_tiers,
            max_tokens_per_video=max(1000, int(max_tokens)),
            
            # Compute allocation
            compute_priority=compute_priority,
            max_compute_units=self.BASE_COMPUTE_UNITS * scale_multiplier,
            inference_stack_access=inference_access,
            latency_priority=latency_priority,
            
            # Posting allocation
            max_posts_per_day=max_posts,
            platform_caps=self.max_posts_per_platform,
            posting_slots=["morning", "afternoon", "evening"],
            
            # Risk & retry allocation
            retry_policy={
                "max_retries": max_retries,
                "required_delta": required_delta,
                "mutation_strength": mutation_strength
            },
            kill_threshold=0.8 if scale_multiplier > 2.0 else 0.5,
            risk_tolerance=min(scale_multiplier / self.MAX_SCALE_MULTIPLIER, 1.0),
            
            # Economic metrics (FOR RL FEEDBACK)
            marginal_value_per_token=scale_multiplier / self.MAX_SCALE_MULTIPLIER,
            opportunity_cost_score=1.0 - (scale_multiplier / self.MAX_SCALE_MULTIPLIER),
            reward_signal_strength=scale_multiplier / self.MAX_SCALE_MULTIPLIER,
            expected_return_weight=scale_multiplier / self.MAX_SCALE_MULTIPLIER,
            risk_penalization_factor=1.0 - (scale_multiplier / self.MAX_SCALE_MULTIPLIER),
            
            # Metadata
            niche=niche,
            routing_state=routing_state,
            scale_multiplier=scale_multiplier,
            reasoning=[f"Scale: {scale_multiplier:.2f}x, Priority: {compute_priority:.2f}"]
        )
        
        return budget

    def _enforce_cost_discipline(
        self,
        niche: str,
        budget: Budget,
        metrics: Dict[str, Any]
    ) -> Budget:
        """
        ENFORCE COST DISCIPLINE - Apply cost constraints and penalties.
        """
        cost_per_view = metrics.get("cost_per_view", 0.0)
        cost_per_million = cost_per_view * 1_000_000
        
        # CPI CEILING ENFORCEMENT
        if cost_per_million > self.MAX_COST_PER_MILLION_VIEWS:
            penalty = self.MAX_COST_PER_MILLION_VIEWS / cost_per_million
            budget.max_videos_per_day = max(1, int(budget.max_videos_per_day * penalty))
            budget.max_tokens_per_video = max(1000, int(budget.max_tokens_per_video * penalty))
            budget.reasoning.append(f"CPI ceiling enforcement: {penalty:.2f}x penalty")
        
        return budget

    def _enforce_global_constraints(
        self,
        budgets: Dict[str, Budget],
        total_compute: float,
        total_tokens: int,
        total_videos: int
    ) -> Tuple[Dict[str, Budget], float, int, int]:
        """
        ENFORCE GLOBAL CONSTRAINTS - Atomic budget depletion.
        
        This ensures global scarcity is respected across all allocations.
        """
        constraints_applied = False
        
        # COMPUTE CONSTRAINT
        if total_compute > self.max_daily_compute:
            scale_factor = self.max_daily_compute / total_compute
            logger.warning(f"COMPUTE CONSTRAINT: Scaling down by {scale_factor:.2f}x")
            for budget in budgets.values():
                budget.max_compute_units *= scale_factor
                budget.compute_priority *= scale_factor
            total_compute = self.max_daily_compute
            constraints_applied = True
        
        # TOKEN CONSTRAINT
        if total_tokens > self.max_daily_tokens:
            scale_factor = self.max_daily_tokens / total_tokens
            logger.warning(f"TOKEN CONSTRAINT: Scaling down by {scale_factor:.2f}x")
            for budget in budgets.values():
                budget.max_tokens_per_video = int(budget.max_tokens_per_video * scale_factor)
            total_tokens = self.max_daily_tokens
            constraints_applied = True
        
        # VIDEO CONSTRAINT
        if total_videos > self.max_parallel_generations:
            scale_factor = self.max_parallel_generations / total_videos
            logger.warning(f"VIDEO CONSTRAINT: Scaling down by {scale_factor:.2f}x")
            for budget in budgets.values():
                budget.max_videos_per_day = int(budget.max_videos_per_day * scale_factor)
            total_videos = self.max_parallel_generations
            constraints_applied = True
        
        return budgets, total_compute, total_tokens, total_videos

    def _update_performance_memory(self, niche: str, metrics: Dict[str, Any]) -> PerformanceMemory:
        """
        UPDATE PERFORMANCE MEMORY - Time-based memory for production-grade decisions.
        
        Implements rolling averages, decay-weighted momentum, and multi-window clearance.
        """
        current_time = time.time()
        
        # Get or create performance memory
        if niche not in self.performance_memory:
            self.performance_memory[niche] = PerformanceMemory()
        
        memory = self.performance_memory[niche]
        
        # Update daily clearance rates
        current_clearance = metrics.get("baseline_clearance_rate", 0.0)
        memory.daily_clearance_rates.append(current_clearance)
        
        # Calculate rolling averages
        if len(memory.daily_clearance_rates) >= 7:
            memory.clearance_rate_7d = sum(list(memory.daily_clearance_rates)[-7:]) / 7
        if len(memory.daily_clearance_rates) >= 30:
            memory.clearance_rate_30d = sum(list(memory.daily_clearance_rates)[-30:]) / 30
        
        # Update momentum with decay
        current_momentum = metrics.get("momentum_score", 0.0)
        memory.momentum_decay = memory.momentum_decay * self.MOMENTUM_DECAY_RATE + current_momentum * (1 - self.MOMENTUM_DECAY_RATE)
        
        # Update weekly momentum
        memory.weekly_momentum.append(current_momentum)
        
        # Update platform-specific performance
        for platform in ["tiktok", "youtube", "instagram", "twitter"]:
            platform_clearance = metrics.get(f"{platform}_clearance_rate", 0.0)
            platform_velocity = metrics.get(f"{platform}_velocity_score", 0.0)
            
            if platform_clearance > 0:
                memory.platform_clearance_rates[platform] = platform_clearance
            if platform_velocity > 0:
                memory.platform_velocity_scores[platform] = platform_velocity
        
        memory.last_updated = current_time
        
        logger.debug(f"Updated performance memory for {niche}: 7d={memory.clearance_rate_7d:.3f}, 30d={memory.clearance_rate_30d:.3f}, momentum_decay={memory.momentum_decay:.3f}")
        
        return memory

    def _compute_platform_pressure(self, niche: str, metrics: Dict[str, Any]) -> Dict[str, PlatformPressure]:
        """
        COMPUTE PLATFORM PRESSURE - Platform-specific allocation pressure.
        
        TikTok ≠ YouTube ≠ Reels - each platform has different distribution ceilings.
        """
        platform_pressure = {}
        
        for platform, ceiling in self.PLATFORM_DISTRIBUTION_CEILINGS.items():
            # Get platform-specific metrics
            clearance_rate = metrics.get(f"{platform}_clearance_rate", 0.0)
            velocity_score = metrics.get(f"{platform}_velocity_score", 0.0)
            avg_views = metrics.get(f"{platform}_avg_views", 0)
            
            # Calculate pressure score (0-1)
            velocity_pressure = min(1.0, velocity_score / 1000.0)  # Normalize velocity
            clearance_pressure = clearance_rate  # Already 0-1
            ceiling_pressure = min(1.0, avg_views / ceiling)  # How close to ceiling
            
            # Combined pressure score
            pressure_score = (velocity_pressure * 0.4 + clearance_pressure * 0.4 + ceiling_pressure * 0.2)
            
            # Capital efficiency (views per dollar)
            cost_per_view = metrics.get(f"{platform}_cost_per_view", 0.0)
            capital_efficiency = 1.0 / max(cost_per_view, 0.001) if cost_per_view > 0 else 0.0
            capital_efficiency = min(1.0, capital_efficiency / 1000.0)  # Normalize
            
            platform_pressure[platform] = PlatformPressure(
                platform=platform,
                pressure_score=pressure_score,
                clearance_rate=clearance_rate,
                velocity_score=velocity_score,
                distribution_ceiling=ceiling,
                capital_efficiency=capital_efficiency
            )
        
        return platform_pressure

    def _evaluate_content_overrides(self, niche: str, metrics: Dict[str, Any]) -> List[ContentOverride]:
        """
        EVALUATE CONTENT OVERRIDES - Content-level escape hatches for elite performers.
        
        Sometimes a niche is mid but a single format is elite.
        """
        overrides = []
        current_time = time.time()
        
        # Check for elite content performers
        elite_content = metrics.get("elite_content_performers", [])
        
        for content_data in elite_content[:3]:  # Max 3 overrides per niche
            content_id = content_data.get("content_id", "")
            performance_score = content_data.get("performance_score", 0.0)
            platform = content_data.get("platform", "unknown")
            
            # Only override for truly elite content (top 1%)
            if performance_score >= 0.99 and self.content_override_budget > 0:
                # Calculate override budget (capped, rare, high-signal only)
                override_budget = min(
                    self.content_override_budget * 0.2,  # Max 20% of override budget per content
                    5.0  # Max 5x normal budget
                )
                
                override = ContentOverride(
                    content_id=content_id,
                    niche=niche,
                    override_budget=override_budget,
                    performance_score=performance_score,
                    platform=platform,
                    expires_at=current_time + 86400,  # 24-hour override
                    reason=f"Elite content performer: {performance_score:.3f}"
                )
                
                overrides.append(override)
                self.content_override_budget -= 1  # Use one override slot
                
                logger.info(f"CONTENT OVERRIDE: {niche} - {content_id} ({platform}) - {override_budget:.1f}x budget")
        
        return overrides

    def _bid_for_global_retry_budget(self, niche: str, metrics: Dict[str, Any], perf_memory: PerformanceMemory) -> float:
        """
        BID FOR GLOBAL RETRY BUDGET - Retries must compete globally.
        
        Retries are capital-intensive and should be allocated to highest performers.
        """
        # Calculate bid strength based on performance
        clearance_rate = metrics.get("baseline_clearance_rate", 0.0)
        momentum = perf_memory.momentum_decay
        velocity = metrics.get("velocity_score", 0.0)
        
        # Higher performers bid more aggressively
        base_bid = clearance_rate * 0.3 + momentum * 0.4 + min(1.0, velocity / 1000.0) * 0.3
        
        # Time-based memory bonus
        if perf_memory.clearance_rate_7d > 0.5:
            base_bid *= 1.2  # 20% bonus for consistent 7-day performance
        
        if perf_memory.clearance_rate_30d > 0.3:
            base_bid *= 1.1  # 10% bonus for consistent 30-day performance
        
        # Platform-specific bonus
        platform_bonus = 0.0
        for platform, pressure in self.platform_pressure.items():
            if platform in metrics and metrics.get(f"{platform}_clearance_rate", 0) > 0.7:
                platform_bonus = max(platform_bonus, pressure.capital_efficiency * 0.1)
        
        final_bid = min(1.0, base_bid + platform_bonus)
        
        return final_bid

    def _enforce_5m_baseline(self, niche: str, metrics: Dict[str, Any], perf_memory: PerformanceMemory) -> bool:
        """
        ENFORCE 5M BASELINE - Hard baseline enforcement for production-grade guarantee.
        
        if median_views < 5_000_000 and age > N: hard_cap_budget()
        """
        median_views = metrics.get("median_views", 0)
        avg_views = metrics.get("avg_views", 0)
        niche_age_days = metrics.get("niche_age_days", 0)
        
        # Use median for baseline, but check average too
        views_to_check = min(median_views, avg_views)
        
        # Hard baseline enforcement
        if views_to_check < self.BASELINE_5M_THRESHOLD and niche_age_days > self.BASELINE_AGE_DAYS:
            logger.warning(f"5M BASELINE VIOLATION: {niche} - views={views_to_check:,}, age={niche_age_days}d")
            return True
        
        return False

    def _compute_scale_pressure_production(self, niche: str, metrics: Dict[str, Any], perf_memory: PerformanceMemory) -> float:
        """
        COMPUTE SCALE PRESSURE PRODUCTION - Production-grade scale pressure with time-based memory.
        
        Uses rolling averages, decay-weighted momentum, and multi-window clearance.
        """
        # EXTRACT PERFORMANCE METRICS
        current_clearance = metrics.get("baseline_clearance_rate", 0.0)
        momentum = metrics.get("momentum_score", 0.0)
        p95_views = metrics.get("p95_views", 0)
        cost_per_view = metrics.get("cost_per_view", 0.0)
        
        # TIME-BASED MEMORY WEIGHTING (PRODUCTION-GRADE)
        # Use explicit weights for stability: 1d: 20%, 7d: 50%, 30d: 30%
        memory_weighted_clearance = (
            current_clearance * self.MEMORY_WEIGHT_1D +
            perf_memory.clearance_rate_7d * self.MEMORY_WEIGHT_7D +
            perf_memory.clearance_rate_30d * self.MEMORY_WEIGHT_30D
        )
        
        # DECAY-WEIGHTED MOMENTUM
        decay_weighted_momentum = perf_memory.momentum_decay * 0.6 + momentum * 0.4
        
        # MULTI-WINDOW CLEARANCE BONUS
        clearance_bonus = 1.0
        if perf_memory.clearance_rate_7d > 0.6 and perf_memory.clearance_rate_30d > 0.4:
            clearance_bonus = 1.3  # 30% bonus for consistent multi-window performance
        elif perf_memory.clearance_rate_7d > 0.5:
            clearance_bonus = 1.15  # 15% bonus for strong 7-day performance
        
        # PHASE-BASED REGIME SWITCHING (with memory)
        if memory_weighted_clearance < self.MIN_CLEARANCE_RATE_TO_SCALE:
            # DEFENSIVE: Preserve capital
            phase_multiplier = 0.3
        elif memory_weighted_clearance < self.AGGRESSIVE_CLEARANCE_THRESHOLD:
            # NEUTRAL: Balanced approach
            phase_multiplier = 0.8 + (memory_weighted_clearance - self.MIN_CLEARANCE_RATE_TO_SCALE) * 0.5
        elif memory_weighted_clearance < self.ELITE_CLEARANCE_THRESHOLD:
            # GROWTH: Accelerated scaling
            phase_multiplier = 1.2 + (memory_weighted_clearance - self.AGGRESSIVE_CLEARANCE_THRESHOLD) * 2.0
        else:
            # COMPOUNDING: Winner takes capital
            phase_multiplier = 2.0 + (memory_weighted_clearance - self.ELITE_CLEARANCE_THRESHOLD) * 10.0
        
        # MOMENTUM COMPOUNDING (with decay weighting)
        if decay_weighted_momentum > 0.8:
            momentum_factor = 1.0 + (decay_weighted_momentum ** 2) * 2.0  # Exponential
        elif decay_weighted_momentum > 0.5:
            momentum_factor = 1.0 + (decay_weighted_momentum ** 1.5) * 1.0  # Super-linear
        else:
            momentum_factor = 1.0 + decay_weighted_momentum * 0.5  # Linear
        
        # CEILING ESTIMATE
        ceiling_estimate = p95_views / 10_000_000  # Normalize to 10M units
        if ceiling_estimate > 10:  # 100M+ potential
            ceiling_factor = 2.0 + (ceiling_estimate - 10) * 0.1
        elif ceiling_estimate > 5:  # 50M+ potential
            ceiling_factor = 1.5 + (ceiling_estimate - 5) * 0.1
        else:
            ceiling_factor = 1.0
        
        # RISK CEILING
        current_risk = metrics.get("risk_score", 0.0)
        risk_ceiling = 0.4 if memory_weighted_clearance >= self.ELITE_CLEARANCE_THRESHOLD else 0.2
        if current_risk > risk_ceiling:
            risk_penalty = risk_ceiling / current_risk
        else:
            risk_penalty = 1.0
        
        # COMBINE WITH TIME-BASED MEMORY BONUS
        raw_multiplier = phase_multiplier * momentum_factor * ceiling_factor * risk_penalty * clearance_bonus
        final_multiplier = max(0.0, min(raw_multiplier, self.MAX_SCALE_MULTIPLIER))
        
        logger.debug(f"Scale pressure for {niche}: memory_clearance={memory_weighted_clearance:.3f}, decay_momentum={decay_weighted_momentum:.3f}, final={final_multiplier:.2f}x")
        
        return final_multiplier

    def _compute_platform_aware_scale(self, niche: str, metrics: Dict[str, Any]) -> float:
        """
        COMPUTE PLATFORM-AWARE SCALE - Enterprise requirement for platform-specific scarcity.
        
        YouTube winners ≠ TikTok winners. Capital must follow platform ROI.
        """
        platform_scales = {}
        
        for platform in ["tiktok", "youtube", "instagram", "twitter"]:
            # Get platform-specific metrics
            clearance = metrics.get(f"{platform}_clearance_rate", 0.0)
            velocity = metrics.get(f"{platform}_velocity_score", 0.0)
            views = metrics.get(f"{platform}_avg_views", 0.0)
            
            if clearance > 0 or velocity > 0 or views > 0:
                # Compute platform-specific scale pressure
                platform_pressure = clearance * 0.4 + min(1.0, velocity / 1000.0) * 0.4 + min(1.0, views / self.PLATFORM_DISTRIBUTION_CEILINGS[platform]) * 0.2
                platform_scales[platform] = platform_pressure
            else:
                platform_scales[platform] = 0.0
        
        # Return the maximum platform scale (follow the winner)
        if platform_scales:
            final_scale = max(platform_scales.values())
            logger.debug(f"Platform-aware scale for {niche}: {platform_scales}, final={final_scale:.3f}")
            return final_scale
        else:
            return 0.0

    def _generate_production_budget(
        self,
        niche: str,
        routing_state: RoutingState,
        base_budget: Dict[str, Any],
        scale_multiplier: float,
        metrics: Dict[str, Any],
        perf_memory: PerformanceMemory,
        platform_pressure: Dict[str, PlatformPressure],
        content_overrides: List[ContentOverride]
    ) -> Budget:
        """
        GENERATE PRODUCTION BUDGET - Production-grade budget with all improvements.
        """
        clearance_rate = metrics.get("baseline_clearance_rate", 0.0)
        
        # GENERATION ALLOCATION
        base_videos = base_budget["max_videos_per_day"]
        base_tokens = base_budget["max_tokens_per_video"]
        
        scaled_videos = int(base_videos * scale_multiplier)
        scaled_tokens = int(base_tokens * scale_multiplier)
        
        # PLATFORM-SPECIFIC ALLOCATION
        # Find best performing platform and allocate accordingly
        best_platform = None
        best_platform_score = 0.0
        for platform, pressure in platform_pressure.items():
            if pressure.pressure_score > best_platform_score:
                best_platform_score = pressure.pressure_score
                best_platform = platform
        
        # CONTENT-LEVEL OVERRIDES
        override_multiplier = 1.0
        if content_overrides:
            # Apply highest override budget
            override_multiplier = max(override.override_budget for override in content_overrides)
            logger.info(f"Applying content override for {niche}: {override_multiplier:.1f}x")
        
        # Apply override to allocation
        final_videos = int(scaled_videos * override_multiplier)
        final_tokens = int(scaled_tokens * override_multiplier)
        
        # ELITE EXCEPTION HANDLING (with platform awareness)
        if clearance_rate >= self.ELITE_CLEARANCE_THRESHOLD:
            # Elite: Can have both volume AND quality
            if scale_multiplier > 3.0:
                allowed_tiers = [QualityTier.STANDARD, QualityTier.PREMIUM, QualityTier.ELITE]
                max_tokens = final_tokens * 1.5  # Quality bonus
                final_videos = int(final_videos * 0.8)  # Slight volume reduction
            else:
                allowed_tiers = [QualityTier.PREMIUM, QualityTier.ELITE]
                max_tokens = final_tokens * 1.2
                final_videos = int(final_videos * 0.9)
        else:
            # NON-ELITE: Volume OR quality, never both
            if scale_multiplier > 2.0:
                # High volume: Force to lower quality
                allowed_tiers = [QualityTier.PROBE, QualityTier.STANDARD]
                max_tokens = base_tokens  # No quality scaling
                final_videos = int(final_videos * 1.2)  # Volume bonus
            else:
                # Standard: Balanced approach
                allowed_tiers = [QualityTier.STANDARD]
                max_tokens = final_tokens
                # No volume scaling
        
        # COMPUTE ALLOCATION (with platform pressure)
        if clearance_rate >= self.ELITE_CLEARANCE_THRESHOLD:
            compute_priority = min(0.95, 0.8 + scale_multiplier * 0.1)
            inference_access = True
            latency_priority = 10  # Highest urgency
        elif clearance_rate >= self.AGGRESSIVE_CLEARANCE_THRESHOLD:
            compute_priority = min(0.8, 0.6 + scale_multiplier * 0.1)
            inference_access = True
            latency_priority = 7
        elif clearance_rate >= self.MIN_CLEARANCE_RATE_TO_SCALE:
            compute_priority = min(0.6, 0.4 + scale_multiplier * 0.1)
            inference_access = False
            latency_priority = 4
        else:
            compute_priority = max(0.1, 0.2 + scale_multiplier * 0.05)
            inference_access = False
            latency_priority = 1  # Best effort
        
        # POSTING ALLOCATION (platform-specific)
        max_posts = min(10, final_videos)
        platform_caps = {}
        for platform, pressure in platform_pressure.items():
            # Allocate posting slots based on platform pressure
            if pressure.pressure_score > 0.7:
                platform_caps[platform] = self.max_posts_per_platform.get(platform, 10)
            elif pressure.pressure_score > 0.4:
                platform_caps[platform] = max(1, self.max_posts_per_platform.get(platform, 10) // 2)
            else:
                platform_caps[platform] = 1
        
        # GLOBAL RETRY BUDGET BIDDING
        global_retry_bid = self._bid_for_global_retry_budget(niche, metrics, perf_memory)
        
        # RETRY POLICY (with global budget consideration)
        if clearance_rate >= self.ELITE_CLEARANCE_THRESHOLD:
            max_retries = min(5, int(global_retry_bid * 5))
            required_delta = 0.05  # 5% improvement required
            mutation_strength = 0.3
        elif clearance_rate >= self.AGGRESSIVE_CLEARANCE_THRESHOLD:
            max_retries = min(3, int(global_retry_bid * 3))
            required_delta = 0.10  # 10% improvement required
            mutation_strength = 0.5
        elif clearance_rate >= self.MIN_CLEARANCE_RATE_TO_SCALE:
            max_retries = min(2, int(global_retry_bid * 2))
            required_delta = 0.15  # 15% improvement required
            mutation_strength = 0.7
        else:
            max_retries = 1 if global_retry_bid > 0.1 else 0
            required_delta = 0.25  # 25% improvement required
            mutation_strength = 0.9
        
        # 5M BASELINE ENFORCEMENT
        baseline_5m_enforced = self._enforce_5m_baseline(niche, metrics, perf_memory)
        if baseline_5m_enforced:
            # Hard cap budget for baseline violations
            final_videos = min(final_videos, 1)
            max_tokens = min(max_tokens, base_tokens)
            compute_priority = min(compute_priority, 0.2)
            max_retries = 0
            logger.warning(f"5M BASELINE ENFORCEMENT: {niche} - budget hard-capped")
        
        # CREATE PRODUCTION-GRADE BUDGET OBJECT
        budget = Budget(
            # Generation allocation
            max_videos_per_day=max(0, final_videos),
            allowed_quality_tiers=allowed_tiers,
            max_tokens_per_video=max(1000, int(max_tokens)),
            
            # Compute allocation
            compute_priority=compute_priority,
            max_compute_units=self.BASE_COMPUTE_UNITS * scale_multiplier * override_multiplier,
            inference_stack_access=inference_access,
            latency_priority=latency_priority,
            
            # Posting allocation
            max_posts_per_day=max_posts,
            platform_caps=platform_caps,
            posting_slots=["morning", "afternoon", "evening"],
            
            # Risk & retry allocation
            retry_policy={
                "max_retries": max_retries,
                "required_delta": required_delta,
                "mutation_strength": mutation_strength
            },
            kill_threshold=0.8 if scale_multiplier > 2.0 else 0.5,
            risk_tolerance=min(scale_multiplier / self.MAX_SCALE_MULTIPLIER, 1.0),
            
            # Economic metrics (FOR RL FEEDBACK)
            marginal_value_per_token=scale_multiplier / self.MAX_SCALE_MULTIPLIER,
            opportunity_cost_score=1.0 - (scale_multiplier / self.MAX_SCALE_MULTIPLIER),
            reward_signal_strength=scale_multiplier / self.MAX_SCALE_MULTIPLIER,
            expected_return_weight=scale_multiplier / self.MAX_SCALE_MULTIPLIER,
            risk_penalization_factor=1.0 - (scale_multiplier / self.MAX_SCALE_MULTIPLIER),
            
            # PRODUCTION-GRADE METRICS
            performance_memory=perf_memory,
            platform_pressure=platform_pressure,
            content_overrides=content_overrides,
            global_retry_bid=global_retry_bid,
            baseline_5m_enforced=baseline_5m_enforced,
            
            # Metadata
            niche=niche,
            routing_state=routing_state,
            scale_multiplier=scale_multiplier,
            reasoning=[
                f"Scale: {scale_multiplier:.2f}x, Priority: {compute_priority:.2f}",
                f"Memory: 7d={perf_memory.clearance_rate_7d:.3f}, 30d={perf_memory.clearance_rate_30d:.3f}",
                f"Platform: {best_platform} ({best_platform_score:.3f})" if best_platform else "No platform data",
                f"Overrides: {len(content_overrides)} active",
                f"5M Baseline: {'ENFORCED' if baseline_5m_enforced else 'OK'}"
            ]
        )
        
        return budget

    def _enforce_production_discipline(
        self,
        niche: str,
        budget: Budget,
        metrics: Dict[str, Any],
        perf_memory: PerformanceMemory
    ) -> Budget:
        """
        ENFORCE PRODUCTION DISCIPLINE - Apply all production-grade constraints.
        """
        cost_per_view = metrics.get("cost_per_view", 0.0)
        cost_per_million = cost_per_view * 1_000_000
        
        # CPI CEILING ENFORCEMENT
        if cost_per_million > self.MAX_COST_PER_MILLION_VIEWS:
            penalty = self.MAX_COST_PER_MILLION_VIEWS / cost_per_million
            budget.max_videos_per_day = max(1, int(budget.max_videos_per_day * penalty))
            budget.max_tokens_per_video = max(1000, int(budget.max_tokens_per_video * penalty))
            budget.reasoning.append(f"CPI ceiling enforcement: {penalty:.2f}x penalty")
        
        # TIME-BASED MEMORY DISCIPLINE
        # If 7-day performance is poor, reduce budget
        if perf_memory.clearance_rate_7d < 0.2 and perf_memory.clearance_rate_30d < 0.15:
            budget.max_videos_per_day = max(1, budget.max_videos_per_day // 2)
            budget.max_tokens_per_video = max(1000, budget.max_tokens_per_video // 2)
            budget.compute_priority *= 0.5
            budget.reasoning.append("Time-based memory discipline: Poor long-term performance")
        
        # PLATFORM-SPECIFIC DISCIPLINE
        # If no platform is performing well, reduce allocation
        platform_performing = any(
            pressure.pressure_score > 0.3 
            for pressure in budget.platform_pressure.values()
        )
        
        if not platform_performing:
            budget.max_posts_per_day = max(1, budget.max_posts_per_day // 2)
            budget.reasoning.append("Platform discipline: No strong platform performance")
        
        # GLOBAL RETRY BUDGET DISCIPLINE
        # Actually deduct from global retry budget based on bid
        if self.global_retry_budget > 0 and budget.global_retry_bid > 0:
            retry_cost = budget.global_retry_bid * budget.retry_policy["max_retries"]
            self.global_retry_budget = max(0, self.global_retry_budget - retry_cost)
            budget.reasoning.append(f"Global retry budget deducted: {retry_cost:.2f}")
        
        # HARD 5M BASELINE ENFORCEMENT (EXPLICIT)
        # Turn philosophy into code law
        median_views = metrics.get("median_views", 0)
        niche_age_days = metrics.get("niche_age_days", 0)
        
        if median_views < self.BASELINE_5M_THRESHOLD and niche_age_days > self.BASELINE_AGE_DAYS:
            budget.max_videos_per_day = min(budget.max_videos_per_day, 1)
            budget.allowed_quality_tiers = [QualityTier.PROBE]
            budget.max_tokens_per_video = min(budget.max_tokens_per_video, self.BASE_TOKENS_PER_VIDEO // 2)
            budget.compute_priority = min(budget.compute_priority, 0.2)
            budget.max_posts_per_day = 0
            budget.reasoning.append("HARD 5M BASELINE ENFORCEMENT: Below threshold")
            logger.warning(f"HARD 5M BASELINE: {niche} - views={median_views:,}, age={niche_age_days}d")
        
        return budget

    def _create_killed_budget(self, niche: str, routing_state: RoutingState, reason: str = "Routing kill") -> Budget:
        """Create a killed budget with zero allocations."""
        return Budget(
            max_videos_per_day=0,
            allowed_quality_tiers=[QualityTier.PROBE],
            max_tokens_per_video=0,
            compute_priority=0.0,
            max_compute_units=0.0,
            inference_stack_access=False,
            latency_priority=0,
            max_posts_per_day=0,
            platform_caps={},
            posting_slots=[],
            retry_policy={"max_retries": 0, "required_delta": 0.0, "mutation_strength": 0.0},
            kill_threshold=1.0,
            risk_tolerance=0.0,
            marginal_value_per_token=0.0,
            opportunity_cost_score=0.0,
            reward_signal_strength=0.0,
            expected_return_weight=0.0,
            risk_penalization_factor=1.0,
            
            # PRODUCTION-GRADE METRICS
            performance_memory=PerformanceMemory(),  # Empty memory for killed budgets
            platform_pressure={},  # Empty pressure for killed budgets
            content_overrides=[],  # No overrides for killed budgets
            global_retry_bid=0.0,  # No retry budget for killed budgets
            baseline_5m_enforced=False,  # Not applicable for killed budgets
            
            niche=niche,
            routing_state=routing_state,
            scale_multiplier=0.0,
            reasoning=[f"KILLED: {reason}"],
            is_killed=True
        )

    def _create_paused_budget(self, niche: str, routing_state: RoutingState) -> Budget:
        """Create a paused budget with minimal allocations."""
        return Budget(
            max_videos_per_day=0,
            allowed_quality_tiers=[QualityTier.PROBE],
            max_tokens_per_video=0,
            compute_priority=0.1,
            max_compute_units=0.1,
            inference_stack_access=False,
            latency_priority=1,
            max_posts_per_day=0,
            platform_caps={},
            posting_slots=[],
            retry_policy={"max_retries": 0, "required_delta": 0.0, "mutation_strength": 0.0},
            kill_threshold=0.5,
            risk_tolerance=0.1,
            marginal_value_per_token=0.0,
            opportunity_cost_score=1.0,
            reward_signal_strength=0.0,
            expected_return_weight=0.0,
            risk_penalization_factor=1.0,
            
            # PRODUCTION-GRADE METRICS
            performance_memory=PerformanceMemory(),  # Empty memory for paused budgets
            platform_pressure={},  # Empty pressure for paused budgets
            content_overrides=[],  # No overrides for paused budgets
            global_retry_bid=0.0,  # No retry budget for paused budgets
            baseline_5m_enforced=False,  # Not applicable for paused budgets
            
            niche=niche,
            routing_state=routing_state,
            scale_multiplier=0.0,
            reasoning=["PAUSED: Routing state PAUSE"]
        )

    def get_allocation_summary(self) -> Dict[str, Any]:
        """Get summary of last allocation result."""
        if not self.last_allocation_result:
            return {"error": "No allocation has been executed yet"}

    # ================================
    # PRODUCTION OPERATIONAL METHODS
    # ================================

    def trigger_manual_freeze(self, reason: str) -> None:
        """
        TRIGGER MANUAL FREEZE - On-call controlled freeze.
        
        This is how on-call engineers can immediately stop allocations.
        """
        self._trigger_freeze(f"MANUAL: {reason}")
        logger.info(f"Manual freeze triggered by on-call: {reason}")

    def release_manual_freeze(self, reason: str) -> None:
        """
        RELEASE MANUAL FREEZE - On-call controlled unfreeze.
        """
        self._release_freeze(f"MANUAL: {reason}")
        logger.info(f"Manual freeze released by on-call: {reason}")

    def get_decision_records(self, niche: Optional[str] = None, limit: int = 100) -> List[AllocationDecisionRecord]:
        """
        GET DECISION RECORDS - Queryable decision history for accountability.
        
        This is how execs ask "why did this die?" and finance audits spend.
        """
        records = self.decision_records
        
        if niche:
            records = [r for r in records if r.get("niche") == niche]
        
        # Return most recent first
        return sorted(records, key=lambda r: r.get("timestamp", 0), reverse=True)[:limit]

    def get_production_status(self) -> Dict[str, Any]:
        """
        GET PRODUCTION STATUS - Complete operational status for on-call.
        
        This is what on-call sees when investigating issues.
        """
        return {
            "allocation_frozen": self.allocation_frozen,
            "freeze_reason": self.freeze_reason,
            "freeze_duration": time.time() - self.freeze_start_time if self.freeze_start_time else 0,
            "dry_run": self.dry_run,
            "shadow_mode": self.shadow_mode,
            "metrics_anomaly_score": self.metrics_anomaly_score,
            "global_retry_budget": self.global_retry_budget,
            "content_override_budget": self.content_override_budget,
            "last_allocation_time": self.last_allocation_result.timestamp if self.last_allocation_result else None,
            "total_decision_records": len(self.decision_records),
            "niches_under_management": len(self.routing_instructions),
            "production_safety_constraints": {
                "max_kill_ratio": self.MAX_KILL_RATIO_PER_RUN,
                "max_anomaly_score": self.MAX_METRICS_ANOMALY_SCORE,
                "freeze_duration": self.ALLOCATION_FREEZE_DURATION
            }
        }

    def export_decision_audit(self, filepath: str) -> None:
        """
        EXPORT DECISION AUDIT - Persist decision records for compliance.
        
        This is how finance audits spend and regulators verify compliance.
        """
        try:
            with open(filepath, 'w') as f:
                for record in self.decision_records:
                    f.write(record.to_json() + "\n")
            logger.info(f"Decision audit exported to {filepath} ({len(self.decision_records)} records)")
        except Exception as e:
            logger.error(f"Failed to export decision audit: {e}")

    def set_dry_run_mode(self, enabled: bool) -> None:
        """
        SET DRY RUN MODE - Safe deployment without enforcement.
        
        This is mandatory for rollout safety.
        """
        self.dry_run = enabled
        logger.info(f"Dry run mode: {'ENABLED' if enabled else 'DISABLED'}")

    def set_shadow_mode(self, enabled: bool) -> None:
        """
        SET SHADOW MODE - Compare vs production without enforcement.
        
        This is mandatory for A/B testing new logic.
        """
        self.shadow_mode = enabled
        logger.info(f"Shadow mode: {'ENABLED' if enabled else 'DISABLED'}")
        
        result = self.last_allocation_result
        
        # Analyze allocation distribution
        active_budgets = [b for b in result.budgets.values() if not b.is_killed]
        killed_budgets = [b for b in result.budgets.values() if b.is_killed]
        
        # Compute statistics
        if active_budgets:
            avg_videos = sum(b.max_videos_per_day for b in active_budgets) / len(active_budgets)
            avg_compute = sum(b.max_compute_units for b in active_budgets) / len(active_budgets)
            avg_priority = sum(b.compute_priority for b in active_budgets) / len(active_budgets)
        else:
            avg_videos = avg_compute = avg_priority = 0
        
        return {
            "timestamp": result.timestamp,
            "total_niches": len(result.budgets),
            "active_niches": len(active_budgets),
            "killed_niches": len(killed_budgets),
            "total_resources": {
                "compute": result.total_compute_allocated,
                "tokens": result.total_tokens_allocated,
                "videos": result.total_videos_allocated
            },
            "averages": {
                "videos_per_niche": avg_videos,
                "compute_per_niche": avg_compute,
                "priority_per_niche": avg_priority
            },
            "execution_time_ms": result.execution_time_ms,
            "niches_killed": result.niches_killed,
            "niches_starved": result.niches_starved
        }


# BACKWARD COMPATIBILITY
BudgetAllocator = CapitalAllocator
