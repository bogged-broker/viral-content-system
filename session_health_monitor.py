"""
session_health_monitor.py - Core session health management and behavioral enforcement

Purpose:
    - Track per-session metrics and health scores
    - Manage session state transitions (WARMUP → HEALTHY → DEGRADED → COOLDOWN → RETIRED)
    - Enforce behavioral shape constraints and read-only contracts
    - Detect silent throttling and platform responses
    - Provide blast radius isolation and kill switch functionality
    - Enable compliance-aware logging and audit trails

Design Philosophy:
    This is NOT an evasion tool. It's a defensive infrastructure component that
    minimizes detection risk through behavioral realism, load discipline, and
    platform compliance. Sessions are retired BEFORE bans occur, not after.

Integration:
    - Plugs into tiktok_scraper.py, instagram_scraper.py, reddit_scraper.py
    - Connects to ingestion_pipeline.py for system-wide health monitoring
    - Provides metrics to anomaly detection and factory management systems
"""

from typing import Literal, Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import time
import json
import logging
import hashlib
import random
from pathlib import Path
from collections import defaultdict, deque
from enum import Enum

logger = logging.getLogger(__name__)


class SessionState(Enum):
    """Session health state machine states"""
    WARMUP = "WARMUP"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    COOLDOWN = "COOLDOWN"
    RETIRED = "RETIRED"


class RequestType(Enum):
    """Request type classification for behavioral shaping"""
    VIDEO_FETCH = "video_fetch"
    CREATOR_FETCH = "creator_fetch"
    TRENDING_FETCH = "trending_fetch"
    MISC_FETCH = "misc_fetch"


@dataclass
class HealthMetrics:
    """Per-session health metrics tracking"""
    session_id: str
    created_at: datetime
    last_request: Optional[datetime] = None
    
    # Success/Failure tracking
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    
    # Response quality metrics
    response_times: deque = field(default_factory=lambda: deque(maxlen=100))
    empty_payloads: int = 0
    truncated_payloads: int = 0
    
    # Behavioral metrics
    endpoint_diversity: Dict[str, int] = field(default_factory=dict)
    request_patterns: deque = field(default_factory=lambda: deque(maxlen=50))
    
    # Content quality metrics
    unique_content_count: int = 0
    duplicate_content_count: int = 0
    stale_content_count: int = 0
    
    # Compliance metrics
    read_only_violations: int = 0
    behavioral_violations: int = 0
    kill_switch_triggers: int = 0
    
    def get_success_ratio(self) -> float:
        """Calculate request success ratio"""
        if self.total_requests == 0:
            return 1.0
        return self.successful_requests / self.total_requests
    
    def get_avg_response_time(self) -> float:
        """Calculate average response time"""
        if not self.response_times:
            return 0.0
        return sum(self.response_times) / len(self.response_times)
    
    def get_endpoint_entropy(self) -> float:
        """Calculate endpoint diversity entropy"""
        if not self.endpoint_diversity:
            return 0.0
        
        total = sum(self.endpoint_diversity.values())
        if total == 0:
            return 0.0
        
        entropy = 0.0
        for count in self.endpoint_diversity.values():
            if count > 0:
                prob = count / total
                entropy -= prob * (prob and math.log2(prob) or 0)
        
        return entropy


@dataclass
class ComplianceLog:
    """Compliance-aware logging entry"""
    timestamp: datetime
    session_id: str
    request_type: RequestType
    endpoint: str
    reason_code: str
    success: bool
    response_summary: Dict[str, Any]
    compliance_checks: Dict[str, bool]
    session_state: SessionState


class SessionHealthMonitor:
    """
    Advanced session health monitoring and behavioral enforcement system.
    
    This class implements the 10 core anti-detection features:
    1. Session Health State Machine
    2. Behavioral Shape Enforcement  
    3. Read-Only Contract Enforcement
    4. Blast Radius Isolation
    5. Silent Throttling Detection
    6. Traffic Shape Budgeting
    7. Delayed Consistency Acceptance
    8. Account Age Awareness
    9. Kill Switches Everywhere
    10. Compliance-Aware Logging
    """
    
    def __init__(
        self,
        platform: str,
        config: Optional[Dict[str, Any]] = None,
        state_dir: Optional[Path] = None
    ):
        """
        Initialize session health monitor.
        
        Args:
            platform: Platform name (tiktok, instagram, reddit, etc.)
            config: Configuration dictionary with health thresholds
            state_dir: Directory for persistent state storage
        """
        self.platform = platform
        self.config = config or self._default_config()
        self.state_dir = state_dir or Path(f"/data/health_monitor/{platform}")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        
        # Session tracking
        self.session_metrics: Dict[str, HealthMetrics] = {}
        self.session_states: Dict[str, SessionState] = {}
        self.session_budgets: Dict[str, Dict[str, Any]] = {}
        
        # Global state
        self.global_kill_switch = False
        self.endpoint_kill_switches: Dict[str, bool] = {}
        self.compliance_logs: List[ComplianceLog] = []
        
        # Load persistent state
        self._load_state()
        
        logger.info(
            f"SessionHealthMonitor initialized for {platform} - "
            f"tracking {len(self.session_metrics)} sessions"
        )
    
    def _default_config(self) -> Dict[str, Any]:
        """Default configuration for health monitoring"""
        return {
            # Health thresholds
            "success_ratio_healthy": 0.9,
            "success_ratio_degraded": 0.7,
            "success_ratio_retire": 0.5,
            "latency_drift_factor": 2.0,
            "empty_payload_threshold": 0.3,
            "stagnation_window": 3600,
            
            # Behavioral constraints
            "max_identical_requests": 3,
            "min_entropy_gap": 2,
            "endpoint_mix_diversity": 0.6,
            "cadence_jitter_range": (0.8, 1.2),
            
            # Traffic budgets
            "video_fetch_ratio": 0.6,
            "creator_fetch_ratio": 0.2,
            "trending_fetch_ratio": 0.15,
            "misc_fetch_ratio": 0.05,
            "max_daily_requests": 1000,
            
            # Kill switches
            "auto_trigger_threshold": 0.3,
            "recovery_cooldown": 1800,
            
            # Session lifecycle
            "warmup_duration": 300,
            "cooldown_duration": 600,
            "retirement_threshold": 3  # Number of degradations before retirement
        }
    
    def register_session(self, session_id: str, account_age_days: int = 0) -> SessionState:
        """
        Register a new session for health monitoring.
        
        Args:
            session_id: Unique session identifier
            account_age_days: Age of the account in days (for warmup scaling)
            
        Returns:
            Initial session state
        """
        if session_id in self.session_metrics:
            logger.warning(f"Session {session_id} already registered")
            return self.session_states[session_id]
        
        # Create health metrics
        metrics = HealthMetrics(
            session_id=session_id,
            created_at=datetime.utcnow()
        )
        
        # Determine initial state based on account age
        if account_age_days < 7:
            initial_state = SessionState.WARMUP
        else:
            initial_state = SessionState.HEALTHY
        
        # Initialize daily budget
        self._initialize_budget(session_id)
        
        # Store session data
        self.session_metrics[session_id] = metrics
        self.session_states[session_id] = initial_state
        
        logger.info(
            f"Registered session {session_id[:8]}... in {initial_state.value} state "
            f"(account_age: {account_age_days} days)"
        )
        
        return initial_state
    
    def can_make_request(
        self,
        session_id: str,
        request_type: RequestType,
        endpoint: str,
        reason_code: str = "standard_request"
    ) -> Tuple[bool, str]:
        """
        Check if session can make a request with behavioral enforcement.
        
        Args:
            session_id: Session identifier
            request_type: Type of request being made
            endpoint: Target endpoint
            reason_code: Internal reason for the request
            
        Returns:
            Tuple of (can_request, denial_reason)
        """
        # Check global kill switch
        if self.global_kill_switch:
            return False, "global_kill_switch_active"
        
        # Check endpoint kill switch
        if self.endpoint_kill_switches.get(endpoint, False):
            return False, "endpoint_kill_switch_active"
        
        # Check if session exists
        if session_id not in self.session_metrics:
            return False, "session_not_registered"
        
        # Check session state
        session_state = self.session_states[session_id]
        if session_state == SessionState.RETIRED:
            return False, "session_retired"
        elif session_state == SessionState.COOLDOWN:
            return False, "session_in_cooldown"
        elif session_state == SessionState.WARMUP:
            # Limited endpoints during warmup
            if request_type in [RequestType.TRENDING_FETCH]:
                return False, "warmup_endpoint_restricted"
        
        # Check behavioral constraints
        behavioral_check = self._check_behavioral_constraints(
            session_id, request_type, endpoint
        )
        if not behavioral_check[0]:
            return behavioral_check
        
        # Check traffic budgets
        budget_check = self._check_traffic_budget(session_id, request_type)
        if not budget_check[0]:
            return budget_check
        
        # Check read-only compliance
        compliance_check = self._check_read_only_compliance(endpoint)
        if not compliance_check[0]:
            return compliance_check
        
        return True, "request_allowed"
    
    def record_request(
        self,
        session_id: str,
        request_type: RequestType,
        endpoint: str,
        reason_code: str,
        success: bool,
        response_time: float,
        response_summary: Dict[str, Any]
    ) -> None:
        """
        Record request outcome and update health metrics.
        
        Args:
            session_id: Session identifier
            request_type: Type of request made
            endpoint: Target endpoint
            reason_code: Internal reason for request
            success: Whether request succeeded
            response_time: Response time in seconds
            response_summary: Summary of response data
        """
        if session_id not in self.session_metrics:
            logger.warning(f"Recording request for unregistered session {session_id}")
            return
        
        metrics = self.session_metrics[session_id]
        current_time = datetime.utcnow()
        
        # Update basic metrics
        metrics.total_requests += 1
        metrics.last_request = current_time
        
        if success:
            metrics.successful_requests += 1
        else:
            metrics.failed_requests += 1
        
        # Update response time
        metrics.response_times.append(response_time)
        
        # Update endpoint diversity
        metrics.endpoint_diversity[endpoint] = metrics.endpoint_diversity.get(endpoint, 0) + 1
        
        # Update request pattern
        pattern_entry = {
            "timestamp": current_time.isoformat(),
            "endpoint": endpoint,
            "request_type": request_type.value,
            "success": success,
            "response_time": response_time
        }
        metrics.request_patterns.append(pattern_entry)
        
        # Analyze response quality
        self._analyze_response_quality(metrics, response_summary)
        
        # Update traffic budget
        self._update_budget_usage(session_id, request_type)
        
        # Create compliance log entry
        compliance_log = ComplianceLog(
            timestamp=current_time,
            session_id=session_id,
            request_type=request_type,
            endpoint=endpoint,
            reason_code=reason_code,
            success=success,
            response_summary=response_summary,
            compliance_checks=self._run_compliance_checks(endpoint, response_summary),
            session_state=self.session_states[session_id]
        )
        self.compliance_logs.append(compliance_log)
        
        # Evaluate session health and potentially transition state
        self._evaluate_session_health(session_id)
        
        # Save state periodically
        if metrics.total_requests % 10 == 0:
            self._save_state()
    
    def _check_behavioral_constraints(
        self,
        session_id: str,
        request_type: RequestType,
        endpoint: str
    ) -> Tuple[bool, str]:
        """Check behavioral shape constraints"""
        metrics = self.session_metrics[session_id]
        
        # Check for too many identical requests
        if len(metrics.request_patterns) >= self.config["max_identical_requests"]:
            recent_patterns = list(metrics.request_patterns)[-self.config["max_identical_requests"]:]
            identical_count = sum(
                1 for pattern in recent_patterns 
                if pattern["endpoint"] == endpoint and pattern["request_type"] == request_type.value
            )
            
            if identical_count >= self.config["max_identical_requests"]:
                return False, "too_many_identical_requests"
        
        # Check entropy gap (time between similar requests)
        if len(metrics.request_patterns) >= 2:
            last_pattern = metrics.request_patterns[-1]
            if (last_pattern["endpoint"] == endpoint and 
                last_pattern["request_type"] == request_type.value):
                
                last_time = datetime.fromisoformat(last_pattern["timestamp"])
                time_gap = (datetime.utcnow() - last_time).total_seconds()
                
                if time_gap < self.config["min_entropy_gap"]:
                    return False, "insufficient_entropy_gap"
        
        # Check endpoint diversity
        if len(metrics.endpoint_diversity) > 0:
            total_requests = sum(metrics.endpoint_diversity.values())
            max_endpoint_ratio = max(metrics.endpoint_diversity.values()) / total_requests
            
            if max_endpoint_ratio > (1 - self.config["endpoint_mix_diversity"]):
                return False, "insufficient_endpoint_diversity"
        
        return True, "behavioral_constraints_satisfied"
    
    def _check_traffic_budget(
        self,
        session_id: str,
        request_type: RequestType
    ) -> Tuple[bool, str]:
        """Check if request fits within traffic budget"""
        if session_id not in self.session_budgets:
            return False, "no_budget_initialized"
        
        budget = self.session_budgets[session_id]
        daily_usage = budget["daily_usage"]
        daily_limits = budget["daily_limits"]
        
        # Check daily total limit
        if daily_usage["total"] >= daily_limits["total"]:
            return False, "daily_total_limit_exceeded"
        
        # Check request type limits
        type_key = request_type.value
        if daily_usage[type_key] >= daily_limits[type_key]:
            return False, f"daily_{type_key}_limit_exceeded"
        
        return True, "within_traffic_budget"
    
    def _check_read_only_compliance(self, endpoint: str) -> Tuple[bool, str]:
        """Check if endpoint complies with read-only contract"""
        forbidden_endpoints = [
            "/like/", "/comment/", "/follow/", "/share/", "/update/",
            "/create/", "/delete/", "/edit/", "/post/"
        ]
        
        for forbidden in forbidden_endpoints:
            if forbidden in endpoint.lower():
                return False, "read_only_violation"
        
        return True, "read_only_compliant"
    
    def _analyze_response_quality(
        self,
        metrics: HealthMetrics,
        response_summary: Dict[str, Any]
    ) -> None:
        """Analyze response quality for health indicators"""
        # Check for empty payloads
        if not response_summary.get("data") or len(response_summary.get("data", [])) == 0:
            metrics.empty_payloads += 1
        
        # Check for truncated responses
        if response_summary.get("truncated", False):
            metrics.truncated_payloads += 1
        
        # Analyze content quality
        data = response_summary.get("data", [])
        if isinstance(data, list):
            content_ids = [item.get("id") for item in data if item.get("id")]
            unique_ids = set(content_ids)
            
            metrics.unique_content_count += len(unique_ids)
            metrics.duplicate_content_count += len(content_ids) - len(unique_ids)
            
            # Check for stale content (old timestamps)
            current_time = time.time()
            stale_threshold = 86400 * 7  # 7 days
            
            stale_count = sum(
                1 for item in data 
                if item.get("created_timestamp") and 
                current_time - item.get("created_timestamp") > stale_threshold
            )
            metrics.stale_content_count += stale_count
    
    def _run_compliance_checks(
        self,
        endpoint: str,
        response_summary: Dict[str, Any]
    ) -> Dict[str, bool]:
        """Run comprehensive compliance checks"""
        return {
            "read_only": self._check_read_only_compliance(endpoint)[0],
            "has_data": bool(response_summary.get("data")),
            "not_truncated": not response_summary.get("truncated", False),
            "valid_response": response_summary.get("status_code") in [200, 201],
            "content_fresh": self._check_content_freshness(response_summary)
        }
    
    def _check_content_freshness(self, response_summary: Dict[str, Any]) -> bool:
        """Check if content is sufficiently fresh"""
        data = response_summary.get("data", [])
        if not isinstance(data, list) or len(data) == 0:
            return True  # No content to check
        
        # Check if at least 50% of content is less than 7 days old
        current_time = time.time()
        fresh_threshold = 86400 * 7  # 7 days
        
        fresh_count = sum(
            1 for item in data 
            if item.get("created_timestamp") and 
            current_time - item.get("created_timestamp") < fresh_threshold
        )
        
        return fresh_count / len(data) >= 0.5
    
    def _evaluate_session_health(self, session_id: str) -> None:
        """Evaluate session health and transition state if necessary"""
        metrics = self.session_metrics[session_id]
        current_state = self.session_states[session_id]
        
        # Calculate health indicators
        success_ratio = metrics.get_success_ratio()
        avg_response_time = metrics.get_avg_response_time()
        empty_payload_ratio = metrics.empty_payloads / max(metrics.total_requests, 1)
        
        # Determine if state transition is needed
        new_state = current_state
        
        if current_state == SessionState.WARMUP:
            # Check if ready for healthy state
            if (metrics.total_requests >= 10 and 
                success_ratio >= self.config["success_ratio_healthy"] and
                time.time() - metrics.created_at.timestamp() >= self.config["warmup_duration"]):
                new_state = SessionState.HEALTHY
        
        elif current_state == SessionState.HEALTHY:
            # Check for degradation
            if (success_ratio < self.config["success_ratio_degraded"] or
                empty_payload_ratio > self.config["empty_payload_threshold"]):
                new_state = SessionState.DEGRADED
        
        elif current_state == SessionState.DEGRADED:
            # Check for recovery or further degradation
            if success_ratio >= self.config["success_ratio_healthy"]:
                new_state = SessionState.HEALTHY
            elif success_ratio < self.config["success_ratio_retire"]:
                new_state = SessionState.RETIRED
        
        # Transition state if needed
        if new_state != current_state:
            self._transition_session_state(session_id, current_state, new_state)
    
    def _transition_session_state(
        self,
        session_id: str,
        old_state: SessionState,
        new_state: SessionState
    ) -> None:
        """Transition session to new state with appropriate actions"""
        self.session_states[session_id] = new_state
        
        logger.warning(
            f"Session {session_id[:8]}... transitioned from {old_state.value} to {new_state.value}"
        )
        
        # Take state-specific actions
        if new_state == SessionState.COOLDOWN:
            # Schedule cooldown end
            cooldown_end = time.time() + self.config["cooldown_duration"]
            self.session_cooldown_end[session_id] = cooldown_end
            
        elif new_state == SessionState.RETIRED:
            # Clean up session data
            self._cleanup_retired_session(session_id)
        
        # Record state transition
        self._record_state_transition(session_id, old_state, new_state)
    
    def trigger_kill_switch(
        self,
        switch_type: Literal["global", "session", "endpoint"],
        target: str,
        reason: str
    ) -> None:
        """Trigger appropriate kill switch"""
        if switch_type == "global":
            self.global_kill_switch = True
            logger.critical(f"GLOBAL KILL SWITCH TRIGGERED: {reason}")
            
        elif switch_type == "session" and target in self.session_metrics:
            self.session_states[target] = SessionState.RETIRED
            logger.critical(f"SESSION KILL SWITCH triggered for {target[:8]}...: {reason}")
            
        elif switch_type == "endpoint":
            self.endpoint_kill_switches[target] = True
            logger.critical(f"ENDPOINT KILL SWITCH triggered for {target}: {reason}")
        
        # Record kill switch trigger
        self._record_kill_switch_trigger(switch_type, target, reason)
    
    def get_session_health_report(self, session_id: str) -> Dict[str, Any]:
        """Get comprehensive health report for a session"""
        if session_id not in self.session_metrics:
            return {"error": "session_not_found"}
        
        metrics = self.session_metrics[session_id]
        state = self.session_states[session_id]
        
        return {
            "session_id": session_id,
            "state": state.value,
            "created_at": metrics.created_at.isoformat(),
            "last_request": metrics.last_request.isoformat() if metrics.last_request else None,
            "metrics": {
                "total_requests": metrics.total_requests,
                "success_ratio": metrics.get_success_ratio(),
                "avg_response_time": metrics.get_avg_response_time(),
                "empty_payload_ratio": metrics.empty_payloads / max(metrics.total_requests, 1),
                "endpoint_entropy": metrics.get_endpoint_entropy(),
                "unique_content": metrics.unique_content_count,
                "duplicate_content": metrics.duplicate_content_count,
                "stale_content": metrics.stale_content_count
            },
            "compliance": {
                "read_only_violations": metrics.read_only_violations,
                "behavioral_violations": metrics.behavioral_violations,
                "kill_switch_triggers": metrics.kill_switch_triggers
            },
            "budget": self.session_budgets.get(session_id, {}),
            "health_score": self._calculate_health_score(session_id)
        }
    
    def get_system_health_overview(self) -> Dict[str, Any]:
        """Get system-wide health overview"""
        state_counts = defaultdict(int)
        total_requests = 0
        total_successes = 0
        
        for session_id, state in self.session_states.items():
            state_counts[state.value] += 1
            metrics = self.session_metrics[session_id]
            total_requests += metrics.total_requests
            total_successes += metrics.successful_requests
        
        return {
            "platform": self.platform,
            "total_sessions": len(self.session_metrics),
            "session_states": dict(state_counts),
            "global_kill_switch": self.global_kill_switch,
            "endpoint_kill_switches": len(self.endpoint_kill_switches),
            "system_success_ratio": total_successes / max(total_requests, 1),
            "compliance_logs": len(self.compliance_logs),
            "health_distribution": {
                state: count for state, count in state_counts.items()
            }
        }
    
    def _calculate_health_score(self, session_id: str) -> float:
        """Calculate overall health score (0-1) for session"""
        metrics = self.session_metrics[session_id]
        
        # Success ratio weight: 40%
        success_score = metrics.get_success_ratio()
        
        # Response time weight: 20% (lower is better)
        avg_time = metrics.get_avg_response_time()
        time_score = max(0, 1 - (avg_time / 5.0))  # 5s is bad
        
        # Content quality weight: 25%
        total_content = metrics.unique_content_count + metrics.duplicate_content_count
        content_score = metrics.unique_content_count / max(total_content, 1)
        
        # Compliance weight: 15%
        compliance_violations = metrics.read_only_violations + metrics.behavioral_violations
        compliance_score = max(0, 1 - (compliance_violations / max(metrics.total_requests, 1)))
        
        overall_score = (
            0.4 * success_score +
            0.2 * time_score +
            0.25 * content_score +
            0.15 * compliance_score
        )
        
        return round(overall_score, 3)
    
    def _initialize_budget(self, session_id: str) -> None:
        """Initialize daily traffic budget for session"""
        daily_limits = {
            "total": self.config["max_daily_requests"],
            "video_fetch": int(self.config["max_daily_requests"] * self.config["video_fetch_ratio"]),
            "creator_fetch": int(self.config["max_daily_requests"] * self.config["creator_fetch_ratio"]),
            "trending_fetch": int(self.config["max_daily_requests"] * self.config["trending_fetch_ratio"]),
            "misc_fetch": int(self.config["max_daily_requests"] * self.config["misc_fetch_ratio"])
        }
        
        self.session_budgets[session_id] = {
            "daily_limits": daily_limits,
            "daily_usage": {key: 0 for key in daily_limits.keys()},
            "last_reset": datetime.utcnow().date()
        }
    
    def _update_budget_usage(self, session_id: str, request_type: RequestType) -> None:
        """Update budget usage for request type"""
        if session_id not in self.session_budgets:
            return
        
        budget = self.session_budgets[session_id]
        today = datetime.utcnow().date()
        
        # Reset budget if it's a new day
        if budget["last_reset"] != today:
            self._initialize_budget(session_id)
            budget = self.session_budgets[session_id]
        
        # Update usage
        budget["daily_usage"]["total"] += 1
        budget["daily_usage"][request_type.value] += 1
    
    def _cleanup_retired_session(self, session_id: str) -> None:
        """Clean up data for retired session"""
        # Archive metrics before cleanup
        self._archive_session_data(session_id)
        
        # Remove from active tracking
        del self.session_metrics[session_id]
        del self.session_states[session_id]
        if session_id in self.session_budgets:
            del self.session_budgets[session_id]
    
    def _archive_session_data(self, session_id: str) -> None:
        """Archive session data for audit trail"""
        archive_file = self.state_dir / f"archived_sessions_{session_id}.json"
        
        archive_data = {
            "session_id": session_id,
            "archived_at": datetime.utcnow().isoformat(),
            "final_state": self.session_states[session_id].value,
            "metrics": self.session_metrics[session_id].__dict__,
            "budget": self.session_budgets.get(session_id, {})
        }
        
        try:
            with open(archive_file, 'w') as f:
                json.dump(archive_data, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Failed to archive session {session_id}: {e}")
    
    def _record_state_transition(
        self,
        session_id: str,
        old_state: SessionState,
        new_state: SessionState
    ) -> None:
        """Record state transition for audit"""
        transition = {
            "timestamp": datetime.utcnow().isoformat(),
            "session_id": session_id,
            "old_state": old_state.value,
            "new_state": new_state.value,
            "reason": "health_evaluation"
        }
        
        # Add to session metrics for tracking
        self.session_metrics[session_id].kill_switch_triggers += 1
    
    def _record_kill_switch_trigger(
        self,
        switch_type: str,
        target: str,
        reason: str
    ) -> None:
        """Record kill switch trigger for audit"""
        trigger = {
            "timestamp": datetime.utcnow().isoformat(),
            "switch_type": switch_type,
            "target": target,
            "reason": reason
        }
        
        logger.critical(f"KILL SWITCH TRIGGER: {trigger}")
    
    def _load_state(self) -> None:
        """Load persistent state from disk"""
        state_file = self.state_dir / "health_monitor_state.json"
        
        if state_file.exists():
            try:
                with open(state_file, 'r') as f:
                    state = json.load(f)
                
                # Restore session states and budgets
                for session_id, session_data in state.get("sessions", {}).items():
                    self.session_states[session_id] = SessionState(session_data["state"])
                    self.session_budgets[session_id] = session_data["budget"]
                
                # Restore kill switches
                self.global_kill_switch = state.get("global_kill_switch", False)
                self.endpoint_kill_switches = state.get("endpoint_kill_switches", {})
                
                logger.info(f"Loaded state for {len(self.session_states)} sessions")
                
            except Exception as e:
                logger.error(f"Failed to load state: {e}")
    
    def _save_state(self) -> None:
        """Save persistent state to disk"""
        state_file = self.state_dir / "health_monitor_state.json"
        
        try:
            state = {
                "platform": self.platform,
                "saved_at": datetime.utcnow().isoformat(),
                "sessions": {
                    session_id: {
                        "state": state.value,
                        "budget": budget
                    }
                    for session_id, (state, budget) in zip(
                        self.session_states.keys(),
                        [(self.session_states[sid], self.session_budgets.get(sid, {})) 
                         for sid in self.session_states.keys()]
                    )
                },
                "global_kill_switch": self.global_kill_switch,
                "endpoint_kill_switches": self.endpoint_kill_switches
            }
            
            with open(state_file, 'w') as f:
                json.dump(state, f, indent=2, default=str)
                
        except Exception as e:
            logger.error(f"Failed to save state: {e}")


# Import math for entropy calculation
import math
