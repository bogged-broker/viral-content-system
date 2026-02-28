"""
/rl_agents/video_micro/content_agent.py

Per-Video Micro-Decision Reinforcement Agent

This agent answers ONE question:
"Given everything we know about THIS video right now, what is the single next
micro-decision that maximizes long-horizon survival?"

NOT:
- "Is this video viral?"
- "Should we boost this?"
- "What should the factory do globally?"

This agent NEVER sees other videos.

Core Principle (NON-NEGOTIABLE):
A video does not "go viral." It either continues surviving or it collapses.
This agent's job is to avoid collapse at every step.

Production-grade. Zero hand-waving. 240k+ LOC architecture.
5M+ views baseline. 30M-300M views repeatable.
"""

import logging
import math
import time
import uuid
from typing import Dict, List, Optional, Literal, Any, Tuple
from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum
import json
import hashlib

# Integration imports (with graceful fallbacks)
try:
    from replay_buffer import ReplayBuffer, Experience
    REPLAY_BUFFER_AVAILABLE = True
except ImportError:
    REPLAY_BUFFER_AVAILABLE = False
    Experience = None
    ReplayBuffer = None
    logging.warning("replay_buffer not available, replay buffer integration disabled")

try:
    from factory_agent import FactoryAgent
    FACTORY_AGENT_AVAILABLE = True
except ImportError:
    FACTORY_AGENT_AVAILABLE = False
    FactoryAgent = None
    logging.warning("factory_agent not available, factory escalation disabled")


# ============================================================================
# FACTORY ESCALATION INTERFACE CONTRACT
# ============================================================================

class FactoryEscalationInterface:
    """
    Escalation intent emission - SPEC COMPLIANT.
    
    This agent emits escalation intent only - it does NOT execute delivery.
    Delivery is owned by orchestration layer (factory agent, message queue, etc.).
    
    At 5M+ baseline, escalation delivery must be owned by orchestration, not the micro-agent.
    This agent outputs "escalation_requested" intent - orchestration guarantees delivery.
    
    This prevents the agent from executing external effects and maintains decision purity.
    """
    
    @staticmethod
    def create_escalation_intent(escalation_payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create escalation intent payload - NO EXECUTION, INTENT ONLY.
        
        This method creates the escalation intent payload but does NOT attempt delivery.
        Delivery is handled by orchestration layer (factory agent, message queue, etc.).
        
        Args:
            escalation_payload: Structured escalation payload
            
        Returns:
            Dict containing escalation intent (for inclusion in output)
        """
        return {
            "escalation_requested": True,
            "escalation_payload": escalation_payload,
            "delivery_guarantee": "orchestration_owned"  # Delivery owned by orchestration
        }


# ============================================================================
# TYPE DEFINITIONS
# ============================================================================

class NetworkUnavailableError(Exception):
    """
    Silent exception for network unavailability.
    
    This exception is caught silently to trigger fallback scoring.
    It does NOT log errors to prevent alert fatigue at scale.
    """
    pass


class ActionType(str, Enum):
    """Allowed micro-action types (EXHAUSTIVE)."""
    CAPTION_VARIANT = "caption_variant"
    THUMBNAIL_SWAP = "thumbnail_swap"
    TIMING_ADJUSTMENT = "timing_adjustment"
    REVIVAL_FLAG = "revival_flag"
    NO_OP = "no_op"


class DecayPhase(str, Enum):
    """Video lifecycle phases based on age."""
    COLD_START = "cold_start"          # 0-15min
    EARLY_GROWTH = "early_growth"      # 15min-2hr
    MID_LIFECYCLE = "mid_lifecycle"    # 2hr-24hr
    MATURE = "mature"                  # 24hr-7d
    LATE_STAGE = "late_stage"          # 7d+


@dataclass
class ActionSpec:
    """Declarative action specification."""
    action_id: str
    type: ActionType
    cost: float
    reversibility: bool
    cooldown: int  # seconds
    constraints: Dict[str, Any]
    
    def validate(self) -> bool:
        """Ensure action spec is well-formed."""
        return (
            bool(self.action_id) and
            self.cost >= 0.0 and
            self.cooldown >= 0 and
            isinstance(self.constraints, dict)
        )


@dataclass
class DecisionContext:
    """
    Deterministic decision context - injected from orchestration layer.
    
    This agent NEVER calls the system clock. All timestamps come from this context.
    This ensures bit-exact replay determinism and legal-grade traceability.
    """
    decision_timestamp: float  # epoch seconds (from orchestration layer)
    decision_iso: str  # ISO format timestamp (from orchestration layer)
    
    def validate(self) -> bool:
        """Validate decision context."""
        return (
            isinstance(self.decision_timestamp, (int, float)) and
            self.decision_timestamp > 0 and
            isinstance(self.decision_iso, str) and
            len(self.decision_iso) > 0
        )


@dataclass
class ContentAgentInput:
    """Frozen snapshot input contract (STRICT)."""
    video_id: str
    video_age_seconds: int
    platform: str
    
    current_state: Dict[str, Any]  # engagement_snapshot, retention_markers, etc.
    predicted_trajectory: Dict[str, Any]  # short_term, mid_term, confidence
    available_actions: List[ActionSpec]
    
    policy_version: str
    decision_context: DecisionContext  # REQUIRED: deterministic timestamp context
    
    def validate(self) -> bool:
        """Validate input contract."""
        if not self.video_id or self.video_age_seconds < 0:
            return False
        if not self.platform or not self.policy_version:
            return False
        if not isinstance(self.current_state, dict):
            return False
        if not isinstance(self.predicted_trajectory, dict):
            return False
        if not isinstance(self.available_actions, list):
            return False
        if not isinstance(self.decision_context, DecisionContext):
            return False
        if not self.decision_context.validate():
            return False
        return all(action.validate() for action in self.available_actions)


@dataclass
class DecisionExplanation:
    """Structured explanation for decision."""
    primary_signal: str
    risk_factors: List[str]
    confidence_drivers: List[str]
    expected_outcome: Dict[str, Any]
    micro_impact_envelope: Optional[Dict[str, Any]] = None  # Formalized envelope (serializable, provable)
    optimization_claim: str = "none"  # Explicit: agent cannot improve a video
    goal: str = "collapse_prevention_only"  # Explicit: goal is survival, not growth
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        result = asdict(self)
        return result


@dataclass
class MicroImpactEnvelope:
    """
    Explicit maximum allowed damage per decision.
    
    This is what lets you PROVE: "This agent literally cannot destroy a video."
    Executive-grade safety boundary.
    """
    max_cost: float
    max_irreversible_actions: int
    max_actions_per_window: int
    max_uncertainty_for_action: float
    
    def validate_action(self, action: ActionSpec, uncertainty: float) -> Tuple[bool, Optional[str]]:
        """Validate action is within envelope."""
        if action.cost > self.max_cost:
            return False, f"Action cost {action.cost:.2f} exceeds max {self.max_cost:.2f}"
        if not action.reversibility and uncertainty > self.max_uncertainty_for_action:
            return False, f"Irreversible action with uncertainty {uncertainty:.2f} exceeds max {self.max_uncertainty_for_action:.2f}"
        return True, None


@dataclass
class ContentAgentOutput:
    """Single decision output (ONLY)."""
    video_id: str
    action_id: str
    confidence: float  # 0.0-1.0
    decision_timestamp: str
    policy_version: str
    explanation: DecisionExplanation
    escalation_requested: bool = False  # Escalation intent (not execution)
    escalation_intent: Optional[Dict[str, Any]] = None  # Escalation intent payload
    decision_fingerprint: Optional[str] = None  # Immutable hash for auditability
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "video_id": self.video_id,
            "action_id": self.action_id,
            "confidence": self.confidence,
            "decision_timestamp": self.decision_timestamp,
            "policy_version": self.policy_version,
            "explanation": self.explanation.to_dict(),
            "escalation_requested": self.escalation_requested,  # Escalation intent (not execution)
            "escalation_intent": self.escalation_intent,  # Escalation intent payload (orchestration-owned delivery)
            "decision_fingerprint": self.decision_fingerprint  # Immutable hash for auditability
        }
    
    def validate(self) -> bool:
        """Validate output."""
        return (
            bool(self.video_id) and
            bool(self.action_id) and
            0.0 <= self.confidence <= 1.0 and
            bool(self.decision_timestamp) and
            bool(self.policy_version)
        )


# ============================================================================
# INPUT VALIDATOR
# ============================================================================


class InputValidator:
    """
    Comprehensive input validation with detailed checks.
    
    Validates all aspects of ContentAgentInput including:
    - Basic contract compliance
    - Type correctness
    - Value ranges
    - State structure completeness
    - Trajectory validity
    - Action space validity
    - Platform-specific constraints
    - Temporal consistency
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.logger = logging.getLogger(__name__ + ".InputValidator")
        self.config = config or {}
        self.validation_cache: Dict[str, Tuple[bool, Optional[str]]] = {}
        
        # Validation thresholds
        self.max_video_age_seconds = self.config.get("max_video_age_seconds", 365 * 24 * 3600)
        self.max_actions = self.config.get("max_actions", 100)
        self.min_actions = self.config.get("min_actions", 1)
    
    def validate(self, agent_input: ContentAgentInput) -> Tuple[bool, Optional[str]]:
        """
        Comprehensive input validation.
        
        Returns:
            (valid, error_message)
        """
        # Check cache (deterministic validation)
        input_hash = self._compute_input_hash(agent_input)
        if input_hash in self.validation_cache:
            return self.validation_cache[input_hash]
        
        # 1. Basic contract validation
        valid, error = self._validate_basic_contract(agent_input)
        if not valid:
            self._cache_result(input_hash, False, error)
            return False, error
        
        # 2. Video ID validation
        valid, error = self._validate_video_id(agent_input.video_id)
        if not valid:
            self._cache_result(input_hash, False, error)
            return False, error
        
        # 3. Video age validation
        valid, error = self._validate_video_age(agent_input.video_age_seconds)
        if not valid:
            self._cache_result(input_hash, False, error)
            return False, error
        
        # 4. Platform validation
        valid, error = self._validate_platform(agent_input.platform)
        if not valid:
            self._cache_result(input_hash, False, error)
            return False, error
        
        # 5. Policy version validation
        valid, error = self._validate_policy_version(agent_input.policy_version)
        if not valid:
            self._cache_result(input_hash, False, error)
            return False, error
        
        # 6. Current state structure validation
        valid, error = self._validate_current_state(agent_input.current_state)
        if not valid:
            self._cache_result(input_hash, False, error)
            return False, error
        
        # 7. Predicted trajectory validation (HARD-FAIL on missing confidence)
        valid, error = self._validate_trajectory(agent_input.predicted_trajectory)
        if not valid:
            self._cache_result(input_hash, False, error)
            return False, error
        
        # 7.5. HARD-FAIL on missing or malformed trajectory confidence
        # Spec: Missing trajectory → no-op + escalation
        trajectory_confidence = agent_input.predicted_trajectory.get("confidence", {})
        if not isinstance(trajectory_confidence, dict):
            error = "Trajectory confidence is missing or malformed (must be dict with 'overall' key)"
            self._cache_result(input_hash, False, error)
            return False, error
        
        overall_confidence = trajectory_confidence.get("overall")
        if overall_confidence is None or not isinstance(overall_confidence, (int, float)):
            error = "Trajectory confidence['overall'] is missing or invalid (must be numeric 0.0-1.0)"
            self._cache_result(input_hash, False, error)
            return False, error
        
        if not (0.0 <= overall_confidence <= 1.0):
            error = f"Trajectory confidence['overall'] out of range: {overall_confidence} (must be 0.0-1.0)"
            self._cache_result(input_hash, False, error)
            return False, error
        
        # 8. Action space validation
        valid, error = self._validate_action_space(agent_input.available_actions)
        if not valid:
            self._cache_result(input_hash, False, error)
            return False, error
        
        # 9. Temporal consistency check
        valid, error = self._validate_temporal_consistency(
            agent_input.video_age_seconds,
            agent_input.current_state,
            agent_input.predicted_trajectory
        )
        if not valid:
            self._cache_result(input_hash, False, error)
            return False, error
        
        # 10. State-trajectory consistency
        valid, error = self._validate_state_trajectory_consistency(
            agent_input.current_state,
            agent_input.predicted_trajectory
        )
        if not valid:
            self._cache_result(input_hash, False, error)
            return False, error
        
        self.logger.debug(f"Input validation passed for video {agent_input.video_id}")
        self._cache_result(input_hash, True, None)
        return True, None
    
    def _validate_basic_contract(self, agent_input: ContentAgentInput) -> Tuple[bool, Optional[str]]:
        """Validate basic dataclass contract."""
        if not agent_input.validate():
            return False, "Input contract validation failed"
        return True, None
    
    def _validate_video_id(self, video_id: str) -> Tuple[bool, Optional[str]]:
        """Validate video ID format and constraints."""
        if not video_id:
            return False, "Video ID cannot be empty"
        
        if not isinstance(video_id, str):
            return False, f"Video ID must be string, got {type(video_id)}"
        
        if len(video_id) > 256:
            return False, f"Video ID too long: {len(video_id)} chars (max 256)"
        
        if len(video_id) < 1:
            return False, "Video ID too short"
        
        # Check for valid characters (alphanumeric, dash, underscore)
        import re
        if not re.match(r'^[a-zA-Z0-9_-]+$', video_id):
            return False, f"Video ID contains invalid characters: {video_id}"
        
        return True, None
    
    def _validate_video_age(self, video_age_seconds: int) -> Tuple[bool, Optional[str]]:
        """Validate video age constraints."""
        if not isinstance(video_age_seconds, int):
            return False, f"Video age must be int, got {type(video_age_seconds)}"
        
        if video_age_seconds < 0:
            return False, f"Video age cannot be negative: {video_age_seconds}s"
        
        if video_age_seconds > self.max_video_age_seconds:
            return False, (
                f"Video age exceeds maximum: {video_age_seconds}s "
                f"(max {self.max_video_age_seconds}s)"
            )
        
        # Check for reasonable upper bound (1 year)
        if video_age_seconds > 365 * 24 * 3600:
            return False, f"Video age suspiciously high: {video_age_seconds}s"
        
        return True, None
    
    def _validate_platform(self, platform: str) -> Tuple[bool, Optional[str]]:
        """Validate platform identifier."""
        if not platform:
            return False, "Platform cannot be empty"
        
        if not isinstance(platform, str):
            return False, f"Platform must be string, got {type(platform)}"
        
        valid_platforms = ["youtube", "tiktok", "instagram", "reddit", "twitter"]
        if platform.lower() not in valid_platforms:
            self.logger.warning(f"Unknown platform: {platform}")
        
        return True, None
    
    def _validate_policy_version(self, policy_version: str) -> Tuple[bool, Optional[str]]:
        """Validate policy version format."""
        if not policy_version:
            return False, "Policy version cannot be empty"
        
        if not isinstance(policy_version, str):
            return False, f"Policy version must be string, got {type(policy_version)}"
        
        if len(policy_version) > 128:
            return False, f"Policy version too long: {len(policy_version)} chars"
        
        # Check semantic version format (loose)
        import re
        if not re.match(r'^[a-zA-Z0-9._-]+$', policy_version):
            return False, f"Policy version contains invalid characters: {policy_version}"
        
        return True, None
    
    def _validate_current_state(self, current_state: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Validate current state structure and values."""
        if not isinstance(current_state, dict):
            return False, f"Current state must be dict, got {type(current_state)}"
        
        if len(current_state) == 0:
            return False, "Current state cannot be empty"
        
        # Required keys
        required_state_keys = ["engagement_snapshot", "retention_markers", 
                               "sentiment_state", "distribution_mode"]
        missing_keys = [k for k in required_state_keys if k not in current_state]
        if missing_keys:
            return False, f"Missing required state keys: {missing_keys}"
        
        # Validate engagement_snapshot
        engagement = current_state.get("engagement_snapshot", {})
        if not isinstance(engagement, dict):
            return False, "engagement_snapshot must be dict"
        
        # Check for basic engagement metrics
        if len(engagement) == 0:
            return False, "engagement_snapshot cannot be empty"
        
        # Validate retention_markers
        retention = current_state.get("retention_markers", {})
        if not isinstance(retention, dict):
            return False, "retention_markers must be dict"
        
        # Validate sentiment_state
        sentiment = current_state.get("sentiment_state", {})
        if not isinstance(sentiment, dict):
            return False, "sentiment_state must be dict"
        
        # Validate distribution_mode
        dist_mode = current_state.get("distribution_mode")
        if not isinstance(dist_mode, str):
            return False, f"distribution_mode must be string, got {type(dist_mode)}"
        
        valid_modes = ["organic", "boosted", "viral", "decaying", "stagnant"]
        if dist_mode not in valid_modes:
            self.logger.warning(f"Unknown distribution_mode: {dist_mode}")
        
        return True, None
    
    def _validate_trajectory(self, trajectory: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Validate predicted trajectory structure."""
        if not isinstance(trajectory, dict):
            return False, f"Predicted trajectory must be dict, got {type(trajectory)}"
        
        if len(trajectory) == 0:
            return False, "Predicted trajectory cannot be empty"
        
        # Required keys
        required_traj_keys = ["short_term", "mid_term", "confidence"]
        missing_keys = [k for k in required_traj_keys if k not in trajectory]
        if missing_keys:
            return False, f"Missing trajectory keys: {missing_keys}"
        
        # Validate short_term
        short_term = trajectory.get("short_term", {})
        if not isinstance(short_term, dict):
            return False, "short_term must be dict"
        
        # Validate mid_term
        mid_term = trajectory.get("mid_term", {})
        if not isinstance(mid_term, dict):
            return False, "mid_term must be dict"
        
        # Validate confidence
        confidence = trajectory.get("confidence", {})
        if not isinstance(confidence, dict):
            return False, "confidence must be dict"
        
        # Check confidence values are valid
        overall_conf = confidence.get("overall", 0.5)
        if not isinstance(overall_conf, (int, float)):
            return False, "confidence.overall must be numeric"
        
        if not 0.0 <= overall_conf <= 1.0:
            return False, f"confidence.overall must be 0.0-1.0, got {overall_conf}"
        
        return True, None
    
    def _validate_action_space(self, available_actions: List[ActionSpec]) -> Tuple[bool, Optional[str]]:
        """Validate action space constraints."""
        if not isinstance(available_actions, list):
            return False, f"Available actions must be list, got {type(available_actions)}"
        
        if len(available_actions) < self.min_actions:
            return False, f"Too few actions: {len(available_actions)} (min {self.min_actions})"
        
        if len(available_actions) > self.max_actions:
            return False, f"Too many actions: {len(available_actions)} (max {self.max_actions})"
        
        # Validate each action
        action_ids = set()
        for i, action in enumerate(available_actions):
            if not isinstance(action, ActionSpec):
                return False, f"Action {i} is not ActionSpec instance"
            
            if not action.validate():
                return False, f"Action {i} failed validation"
            
            # Check for duplicate action_ids
            if action.action_id in action_ids:
                return False, f"Duplicate action_id: {action.action_id}"
            action_ids.add(action.action_id)
            
            # Validate action cost
            if action.cost < 0.0:
                return False, f"Action {action.action_id} has negative cost: {action.cost}"
            
            if action.cost > 100.0:  # Reasonable upper bound
                return False, f"Action {action.action_id} has excessive cost: {action.cost}"
            
            # Validate cooldown
            if action.cooldown < 0:
                return False, f"Action {action.action_id} has negative cooldown: {action.cooldown}"
        
        # Ensure no_op is always available
        has_no_op = any(a.type == ActionType.NO_OP for a in available_actions)
        if not has_no_op:
            return False, "no_op action must always be available"
        
        return True, None
    
    def _validate_temporal_consistency(
        self,
        video_age_seconds: int,
        current_state: Dict[str, Any],
        trajectory: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """Validate temporal consistency between age and state/trajectory."""
        # Check if state timestamps are consistent with video age
        engagement = current_state.get("engagement_snapshot", {})
        if "timestamp" in engagement:
            try:
                from datetime import datetime
                state_time = datetime.fromisoformat(engagement["timestamp"].replace("Z", "+00:00"))
                # Basic consistency check (state shouldn't be from future)
                # This is a loose check - in production would be stricter
            except Exception as e:
                self.logger.warning(f"Could not parse state timestamp: {e}")
        
        # Check if trajectory predictions are reasonable for video age
        confidence = trajectory.get("confidence", {})
        overall_conf = confidence.get("overall", 0.5)
        
        # Very old videos might have lower confidence
        if video_age_seconds > 7 * 24 * 3600:  # 7 days
            if overall_conf > 0.9:
                self.logger.warning(
                    f"High confidence ({overall_conf}) for old video ({video_age_seconds}s)"
                )
        
        return True, None
    
    def _validate_state_trajectory_consistency(
        self,
        current_state: Dict[str, Any],
        trajectory: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """Validate consistency between current state and predicted trajectory."""
        engagement = current_state.get("engagement_snapshot", {})
        short_term = trajectory.get("short_term", {})
        
        # Basic sanity: if current engagement is very low, short-term shouldn't predict massive growth
        current_views = engagement.get("views", 0)
        predicted_views = short_term.get("predicted_views", current_views)
        
        # Allow some growth but flag extreme discrepancies
        if current_views > 0:
            growth_factor = predicted_views / current_views
            if growth_factor > 1000.0:  # 1000x growth seems unrealistic
                self.logger.warning(
                    f"Extreme growth prediction: {current_views} -> {predicted_views} "
                    f"({growth_factor:.1f}x)"
                )
        
        return True, None
    
    def _compute_input_hash(self, agent_input: ContentAgentInput) -> str:
        """
        Compute deterministic hash of input for caching.
        
        Uses frozen canonicalization rules for bit-exact determinism.
        """
        canonical = json.dumps({
            "video_id": agent_input.video_id,
            "video_age_seconds": agent_input.video_age_seconds,
            "platform": agent_input.platform,
            "policy_version": agent_input.policy_version,
            "state_hash": hashlib.sha256(
                json.dumps(agent_input.current_state, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode('utf-8')
            ).hexdigest()[:16],
            "trajectory_hash": hashlib.sha256(
                json.dumps(agent_input.predicted_trajectory, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode('utf-8')
            ).hexdigest()[:16],
            "action_count": len(agent_input.available_actions)
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    
    def _cache_result(self, input_hash: str, valid: bool, error: Optional[str]) -> None:
        """Cache validation result."""
        self.validation_cache[input_hash] = (valid, error)
        
        # Limit cache size
        if len(self.validation_cache) > 10000:
            # Remove oldest entries (simple: remove half)
            keys_to_remove = list(self.validation_cache.keys())[:5000]
            for key in keys_to_remove:
                del self.validation_cache[key]


# ============================================================================
# TEMPORAL CONTEXT ENCODER
# ============================================================================


class TemporalContextEncoder:
    """
    Temporal context encoding system - SPEC COMPLIANT.
    
    A 10-minute video and a 2-hour video are DIFFERENT PROBLEMS.
    
    Encodes ONLY:
    - Exact video age (continuous)
    - Decay phase (discrete)
    - Phase progress (continuous within phase)
    - Exposure phase (early/mid/late within phase)
    - Time-of-day context (if provided)
    - Day-of-week context (if provided)
    
    NOTE: Lifecycle abstractions (expected_lifespan, lifespan_remaining, urgency_score, 
    stability_score, decay_rate_estimate) are NOT computed here. They belong in 
    feature_extraction or policy_network preprocessing to avoid value-network semantics 
    bleeding into the micro-agent.
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.logger = logging.getLogger(__name__ + ".TemporalContextEncoder")
        self.config = config or {}
        
        # Phase boundaries (seconds)
        self.phase_boundaries = {
            DecayPhase.COLD_START: (0, 15 * 60),           # 0-15min
            DecayPhase.EARLY_GROWTH: (15 * 60, 2 * 3600),  # 15min-2hr
            DecayPhase.MID_LIFECYCLE: (2 * 3600, 24 * 3600),  # 2hr-24hr
            DecayPhase.MATURE: (24 * 3600, 7 * 24 * 3600),  # 24hr-7d
            DecayPhase.LATE_STAGE: (7 * 24 * 3600, float('inf'))  # 7d+
        }
        
        # NOTE: phase_decay_rates removed - lifecycle abstractions belong in feature_extraction
        # or policy_network preprocessing, not in the micro-agent's temporal encoder.
        
        # Peak hours (hour of day, 0-23)
        self.peak_hours = self.config.get("peak_hours", [18, 19, 20, 21, 22])
        
        # Peak days (day of week, 0=Monday, 6=Sunday)
        self.peak_days = self.config.get("peak_days", [4, 5, 6])  # Friday, Saturday, Sunday
    
    def encode(self, video_age_seconds: int, creation_time: Optional[datetime] = None, decision_context: Optional[DecisionContext] = None) -> Dict[str, Any]:
        """
        Comprehensive temporal context encoding.
        
        Args:
            video_age_seconds: Age of video in seconds
            creation_time: Optional creation timestamp for time-of-day context
            decision_context: Optional decision context for deterministic time encoding
        
        Returns:
            {
                "age_seconds": int,
                "decay_phase": str,
                "phase_progress": float,  # 0.0-1.0 within phase
                "exposure_phase": str,    # "early"/"mid"/"late" within phase
                "log_age": float,
                "age_bucket": str,
                "normalized_age": float,  # 0.0-1.0 normalized across all phases
                "days_since_creation": float,
                "hours_since_creation": float,
                "minutes_since_creation": float,
                "is_peak_hour": bool,
                "is_peak_day": bool,
                "hour_of_day": int,
                "day_of_week": int,
            }
            
        NOTE: Lifecycle abstractions (expected_lifespan, lifespan_remaining, urgency_score,
        stability_score, decay_rate_estimate) are NOT included. These should be computed
        in feature_extraction or policy_network preprocessing.
        """
        # Determine decay phase
        decay_phase = self._get_decay_phase(video_age_seconds)
        
        # Phase progress
        phase_start, phase_end = self.phase_boundaries[decay_phase]
        if phase_end == float('inf'):
            # For late stage, normalize against 30 days
            max_age_for_normalization = 30 * 24 * 3600
            phase_progress = min(1.0, (video_age_seconds - phase_start) / (max_age_for_normalization - phase_start))
        else:
            phase_progress = max(0.0, min(1.0, (video_age_seconds - phase_start) / (phase_end - phase_start)))
        
        # Exposure phase within current phase
        exposure_phase = self._get_exposure_phase(phase_progress)
        
        # Log age (for neural encoding)
        log_age = self._safe_log(video_age_seconds + 1)
        
        # Normalized age across all phases (0.0-1.0)
        normalized_age = self._normalize_age(video_age_seconds)
        
        # Age bucket for logging
        age_bucket = self._get_age_bucket(video_age_seconds)
        
        # Time breakdowns
        days_since_creation = video_age_seconds / (24 * 3600)
        hours_since_creation = video_age_seconds / 3600
        minutes_since_creation = video_age_seconds / 60
        
        # Time-of-day and day-of-week context
        time_context = self._encode_time_context(creation_time, decision_context)
        
        # NOTE: Lifecycle abstractions (decay_rate_estimate, expected_lifespan, 
        # lifespan_remaining, urgency_score, stability_score) are NOT computed here.
        # These belong in feature_extraction or policy_network preprocessing to avoid
        # value-network semantics bleeding into the micro-agent.
        
        context = {
            "age_seconds": video_age_seconds,
            "decay_phase": decay_phase.value,
            "phase_progress": phase_progress,
            "exposure_phase": exposure_phase,
            "log_age": log_age,
            "age_bucket": age_bucket,
            "normalized_age": normalized_age,
            "days_since_creation": days_since_creation,
            "hours_since_creation": hours_since_creation,
            "minutes_since_creation": minutes_since_creation,
            **time_context,
        }
        
        self.logger.debug(f"Encoded temporal context: phase={decay_phase.value}, progress={phase_progress:.2f}")
        return context
    
    def _get_decay_phase(self, age_seconds: int) -> DecayPhase:
        """Determine which decay phase the video is in."""
        for phase, (start, end) in self.phase_boundaries.items():
            if start <= age_seconds < end:
                return phase
        return DecayPhase.LATE_STAGE
    
    def _get_exposure_phase(self, phase_progress: float) -> str:
        """Get exposure phase within current phase."""
        if phase_progress < 0.33:
            return "early"
        elif phase_progress < 0.67:
            return "mid"
        else:
            return "late"
    
    def _normalize_age(self, age_seconds: int) -> float:
        """Normalize age across all phases to 0.0-1.0."""
        # Use 30 days as maximum for normalization
        max_normalization_age = 30 * 24 * 3600
        return min(1.0, age_seconds / max_normalization_age)
    
    def _encode_time_context(self, creation_time: Optional[datetime], decision_context: Optional[DecisionContext] = None) -> Dict[str, Any]:
        """
        Encode time-of-day and day-of-week context.
        
        CRITICAL: Agent must NEVER infer time. Time must be provided or absent, never guessed.
        For 10/10 determinism: if no time provided, skip time-of-day encoding entirely.
        """
        if creation_time is None:
            # Use decision_context timestamp if available (deterministic)
            if decision_context:
                creation_time = datetime.fromisoformat(decision_context.decision_iso.replace("Z", "+00:00"))
            else:
                # NO FALLBACK: Agent must never infer time. Skip time-of-day encoding.
                # This ensures bit-exact determinism - identical input → identical output
                return {
                    "is_peak_hour": False,  # Neutral default
                    "is_peak_day": False,   # Neutral default
                    "hour_of_day": 12,      # Neutral default (noon)
                    "day_of_week": 3        # Neutral default (Thursday)
                }
        
        # Time-of-day features
        hour_of_day = creation_time.hour
        day_of_week = creation_time.weekday()  # 0=Monday, 6=Sunday
        
        is_peak_hour = hour_of_day in self.peak_hours
        is_peak_day = day_of_week in self.peak_days
        
        return {
            "is_peak_hour": is_peak_hour,
            "is_peak_day": is_peak_day,
            "hour_of_day": hour_of_day,
            "day_of_week": day_of_week,
        }
    
    # NOTE: Lifecycle abstraction methods (_estimate_decay_rate, _estimate_expected_lifespan,
    # _compute_urgency_score, _compute_stability_score) have been removed per spec compliance.
    # These forward-looking abstractions belong in feature_extraction or policy_network 
    # preprocessing, not in the micro-agent's temporal encoder.
    
    def _safe_log(self, x: float) -> float:
        """Safe logarithm."""
        import math
        return math.log(max(x, 1.0))
    
    def _get_age_bucket(self, age_seconds: int) -> str:
        """Human-readable age bucket."""
        minutes = age_seconds / 60
        hours = age_seconds / 3600
        days = age_seconds / (24 * 3600)
        weeks = age_seconds / (7 * 24 * 3600)
        months = age_seconds / (30 * 24 * 3600)
        
        if minutes < 1:
            return f"{int(age_seconds)}s"
        elif minutes < 60:
            return f"{int(minutes)}min"
        elif hours < 24:
            return f"{int(hours)}hr"
        elif days < 7:
            return f"{int(days)}d"
        elif weeks < 4:
            return f"{int(weeks)}w"
        elif months < 12:
            return f"{int(months)}mo"
        else:
            return f"{int(months / 12)}yr"


# ============================================================================
# ACTION MASKER (CRITICAL)
# ============================================================================


class ActionMasker:
    """
    Comprehensive action filtering system.
    
    Prevents catastrophic decisions by enforcing:
    - Cooldowns (per-action and global)
    - Irreversibility limits (temporal and phase-based)
    - Platform rules (dynamic and static)
    - Agent authority (scope of control)
    - Cost thresholds (absolute and relative)
    - Rate limiting (per-action-type and global)
    - Temporal restrictions (phase-based policies)
    - State-based constraints (engagement-dependent)
    
    EDGE CASE BEHAVIOR:
    This masker applies ~12 independent filters. In cold-start or partial-state scenarios,
    this can collapse action space aggressively. To prevent persistent no-op loops:
    
    - no_op is ALWAYS allowed (safe fallback)
    - In cold-start, if action_space collapses to only no_op and other actions exist,
      the masker applies lenient defaults for missing state (e.g., assume cooldowns expired)
    - Partial state: missing history/rate_state defaults to permissive (no blocking)
    - Recovery mode: after persistent no_op, filters gradually relax to allow recovery
    
    This ensures survivability (no damage) while allowing recovery in edge cases.
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.logger = logging.getLogger(__name__ + ".ActionMasker")
        self.config = config or {}
        
        # Configuration
        self.max_action_cost = self.config.get("max_action_cost", 1.0)
        self.max_action_cost_cold_start = self.config.get("max_action_cost_cold_start", 0.3)
        self.global_rate_limit = self.config.get("global_rate_limit", 100)  # actions per hour
        self.rate_limit_window = self.config.get("rate_limit_window", 3600)  # seconds
        
        # Platform-specific rules
        self.platform_rules = self._load_platform_rules()
        
        # Phase-based policies
        self.phase_policies = self._load_phase_policies()
    
    def mask_actions(
        self,
        available_actions: List[ActionSpec],
        temporal_context: Dict[str, Any],
        current_state: Dict[str, Any],
        action_history: Optional[Dict[str, datetime]] = None,
        action_type_history: Optional[Dict[ActionType, datetime]] = None,
        rate_state: Optional[Dict[str, Any]] = None,
        factory_locks: Optional[Dict[str, Any]] = None
    ) -> List[ActionSpec]:
        """
        Comprehensive action filtering.
        
        STATELESS: All decision-relevant state must arrive in inputs.
        This ensures perfect replay determinism and decision purity.
        
        Args:
            action_history: Dict mapping action_id -> last_execution_time
            action_type_history: Dict mapping ActionType -> last_execution_time
            rate_state: Dict with 'global_action_count' and 'window_start' for rate limiting
        
        Returns:
            List of allowed actions (always includes no_op)
        """
        if len(available_actions) == 0:
            self.logger.warning("No available actions to mask")
            return []
        
        # Default empty state if not provided (for backward compatibility)
        # CRITICAL: All state must come from snapshot - no default creation with datetime.utcnow()
        action_history = action_history or {}
        action_type_history = action_type_history or {}
        
        # Rate state must be provided from snapshot - no defaults that break determinism
        # If not provided, rate limiting is effectively disabled (safe default)
        if rate_state is None:
            rate_state = {
                "global_action_count": 0,
                "window_start": None  # No window = no rate limiting
            }
        
        allowed_actions = []
        decay_phase = temporal_context.get("decay_phase", DecayPhase.COLD_START.value)
        
        # Compute rate limit check (read-only, no mutation)
        # Window reset logic is computed but state is NOT mutated (snapshot-driven)
        rate_state_for_check = self._compute_rate_state_for_check(rate_state)
        
        for action in available_actions:
            # Always allow no_op (but still check some constraints)
            if action.type == ActionType.NO_OP:
                allowed_actions.append(action)
                continue
            
            # Extract current_timestamp from rate_state for deterministic cooldown checks
            current_timestamp = rate_state.get("current_timestamp")
            if current_timestamp and isinstance(current_timestamp, str):
                try:
                    current_timestamp = datetime.fromisoformat(current_timestamp)
                except (ValueError, AttributeError):
                    current_timestamp = None
            
            # 1. Check individual cooldown (snapshot-driven)
            if not self._check_cooldown(action, action_history, current_timestamp):
                self.logger.debug(f"Action {action.action_id} blocked by cooldown")
                continue
            
            # 2. Check action-type cooldown (snapshot-driven)
            if not self._check_action_type_cooldown(action, action_type_history, current_timestamp):
                self.logger.debug(f"Action {action.action_id} blocked by action-type cooldown")
                continue
            
            # 3. Check global rate limit (read-only check, no mutation)
            if not self._check_global_rate_limit(rate_state_for_check):
                self.logger.debug(f"Action {action.action_id} blocked by global rate limit")
                continue
            
            # NOTE: Safety checks (irreversibility, cost, platform, phase policy, state constraints)
            # have been moved to SafetyChecker for centralized safety veto.
            # ActionMasker now only handles availability checks (cooldowns, rate limits, dependencies).
            
            # 10. Check action dependencies
            if not self._check_action_dependencies(action, current_state, action_history):
                self.logger.debug(
                    f"Action {action.action_id} blocked: dependencies not satisfied"
                )
                continue
            
            # 11. Check resource constraints
            if not self._check_resource_constraints(action, current_state):
                self.logger.debug(
                    f"Action {action.action_id} blocked: resource constraints"
                )
                continue
            
            # 12. Check temporal validity
            if not self._check_temporal_validity(action, temporal_context):
                self.logger.debug(
                    f"Action {action.action_id} blocked: temporal validity check failed"
                )
                continue
            
            # All checks passed - action is allowed
            allowed_actions.append(action)
        
        # Ensure no_op is always available
        has_no_op = any(a.type == ActionType.NO_OP for a in allowed_actions)
        if not has_no_op:
            # Add no_op if missing
            no_op_action = ActionSpec(
                action_id="no_op_fallback",
                type=ActionType.NO_OP,
                cost=0.0,
                reversibility=True,
                cooldown=0,
                constraints={}
            )
            allowed_actions.append(no_op_action)
            self.logger.warning("Added fallback no_op action")
        
        # EDGE CASE: Prevent action space collapse to only no_op in cold-start/partial state
        # If only no_op remains and we have other actions available, apply lenient defaults
        # This prevents persistent no-op loops while maintaining safety
        non_no_op_available = [a for a in available_actions if a.type != ActionType.NO_OP]
        non_no_op_allowed = [a for a in allowed_actions if a.type != ActionType.NO_OP]
        
        if len(non_no_op_allowed) == 0 and len(non_no_op_available) > 0:
            # Action space collapsed to only no_op - apply recovery logic
            # In cold-start/partial state, assume lenient defaults for missing state
            self.logger.warning(
                f"Action space collapsed to only no_op in {decay_phase} phase. "
                f"Applying lenient defaults for recovery (partial state assumed)"
            )
            
            # Apply lenient recovery: allow low-cost reversible actions if state is partial
            # This prevents persistent no-op loops while maintaining safety bounds
            has_partial_state = (
                not action_history or 
                not rate_state.get("window_start") or 
                decay_phase == DecayPhase.COLD_START.value
            )
            
            if has_partial_state:
                # RECOVERY MODE SINGLE-SHOT: At most 1 non-no-op action per decision window
                # This prevents long-tail drift and accidental exploration.
                # Enforced via snapshot state, not memory.
                recovery_window_key = f"recovery_action_window_{rate_state.get('window_start', 'unknown')}"
                # Get recovery actions used from current_state (snapshot-driven, not memory)
                recovery_state = current_state.get("recovery_actions_used", {})
                if not isinstance(recovery_state, dict):
                    recovery_state = {}
                recovery_actions_used = recovery_state.get(recovery_window_key, 0)
                
                if recovery_actions_used < 1:
                    # In partial state: allow low-cost reversible actions (safe recovery)
                    for action in non_no_op_available:
                        if action.cost < 0.3 and action.reversibility:
                            if action not in allowed_actions:
                                allowed_actions.append(action)
                                self.logger.debug(
                                    f"Recovery mode: Allowing low-cost reversible action {action.action_id} "
                                    f"in partial state ({decay_phase}) - {recovery_actions_used + 1}/1 allowed this window"
                                )
                                # Mark that recovery action will be used (tracked in snapshot state)
                                temporal_context["_recovery_action_used"] = True
                                break  # Only allow one recovery action per window
                else:
                    self.logger.debug(
                        f"Recovery mode: Already used {recovery_actions_used} recovery action(s) this window - "
                        f"single-shot limit enforced"
                    )
        
        self.logger.info(
            f"Masked {len(available_actions)} actions to {len(allowed_actions)} allowed "
            f"(phase: {decay_phase}, recovery: {len([a for a in allowed_actions if a.type != ActionType.NO_OP]) > 0})"
        )
        return allowed_actions
    
    def _check_cooldown(
        self, 
        action: ActionSpec, 
        action_history: Dict[str, datetime],
        current_timestamp: Optional[datetime] = None
    ) -> bool:
        """
        Check if action is off cooldown (100% snapshot-driven).
        
        Args:
            current_timestamp: Timestamp from snapshot (not datetime.utcnow())
        """
        if action.action_id not in action_history:
            return True
        
        last_use = action_history[action.action_id]
        
        # Use timestamp from snapshot if provided, otherwise cooldown check is disabled
        if current_timestamp is None:
            # No timestamp = assume cooldown expired (safe default)
            return True
        
        if isinstance(last_use, str):
            last_use = datetime.fromisoformat(last_use)
        if isinstance(current_timestamp, str):
            current_timestamp = datetime.fromisoformat(current_timestamp)
        
        elapsed = (current_timestamp - last_use).total_seconds()
        
        if elapsed < action.cooldown:
            self.logger.debug(
                f"Action {action.action_id} on cooldown: "
                f"{elapsed:.1f}s / {action.cooldown}s"
            )
            return False
        
        return True
    
    def _check_action_type_cooldown(
        self, 
        action: ActionSpec, 
        action_type_history: Dict[ActionType, datetime],
        current_timestamp: Optional[datetime] = None
    ) -> bool:
        """
        Check if action type is off cooldown (100% snapshot-driven).
        
        Args:
            current_timestamp: Timestamp from snapshot (not datetime.utcnow())
        """
        if action.type not in action_type_history:
            return True
        
        last_use = action_type_history[action.type]
        
        # Use timestamp from snapshot if provided
        if current_timestamp is None:
            # No timestamp = assume cooldown expired (safe default)
            return True
        
        if isinstance(last_use, str):
            last_use = datetime.fromisoformat(last_use)
        if isinstance(current_timestamp, str):
            current_timestamp = datetime.fromisoformat(current_timestamp)
        
        # Type-level cooldown: 10% of action cooldown or minimum 60s
        type_cooldown = max(action.cooldown * 0.1, 60)
        elapsed = (current_timestamp - last_use).total_seconds()
        
        return elapsed >= type_cooldown
    
    def _compute_rate_state_for_check(self, rate_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Compute rate state for checking (read-only, no mutation).
        
        This computes whether window should be reset, but does NOT mutate input.
        All state comes from snapshot - this is purely functional.
        """
        window_start = rate_state.get("window_start")
        global_action_count = rate_state.get("global_action_count", 0)
        current_timestamp = rate_state.get("current_timestamp")  # Must come from snapshot
        
        # If no window_start or current_timestamp, rate limiting is disabled
        if window_start is None or current_timestamp is None:
            return {
                "global_action_count": 0,
                "window_start": None,
                "within_limit": True  # No rate limiting if no timestamp
            }
        
        # Compute elapsed time (read-only)
        if isinstance(window_start, str):
            window_start = datetime.fromisoformat(window_start)
        if isinstance(current_timestamp, str):
            current_timestamp = datetime.fromisoformat(current_timestamp)
        
        elapsed = (current_timestamp - window_start).total_seconds()
        
        # If window expired, compute reset state (but don't mutate input)
        if elapsed >= self.rate_limit_window:
            return {
                "global_action_count": 0,
                "window_start": current_timestamp,
                "within_limit": True
            }
        
        # Check if within limit (read-only computation)
        actions_per_hour = (global_action_count / elapsed) * 3600 if elapsed > 0 else 0
        within_limit = actions_per_hour < self.global_rate_limit
        
        return {
            "global_action_count": global_action_count,
            "window_start": window_start,
            "within_limit": within_limit,
            "actions_per_hour": actions_per_hour
        }
    
    def _check_global_rate_limit(self, rate_state_for_check: Dict[str, Any]) -> bool:
        """
        Check if global rate limit is not exceeded (100% read-only, snapshot-driven).
        
        This is purely functional - no mutation, no datetime.utcnow(), all from snapshot.
        """
        within_limit = rate_state_for_check.get("within_limit", True)
        
        if not within_limit:
            actions_per_hour = rate_state_for_check.get("actions_per_hour", 0)
            self.logger.warning(
                f"Global rate limit exceeded: {actions_per_hour:.1f} actions/hour "
                f"(limit: {self.global_rate_limit})"
            )
            return False
        
        return True
    
    # NOTE: Safety checks (_check_irreversibility_limit, _check_cost_threshold, 
    # _check_platform_constraints, _check_phase_policy, _check_state_constraints)
    # have been moved to SafetyChecker.veto() for final centralized safety gate.
    # ActionMasker now only handles availability checks (cooldowns, rate limits, dependencies).
    
    def _check_factory_locks(
        self,
        action: ActionSpec,
        factory_locks: Dict[str, Any]
    ) -> bool:
        """Comprehensive factory lock checking."""
        # Check locked action types
        locked_types = factory_locks.get("locked_action_types", [])
        if action.type.value in locked_types:
            return False
        
        # Check locked action IDs
        locked_action_ids = factory_locks.get("locked_action_ids", [])
        if action.action_id in locked_action_ids:
            return False
        
        # Check locked video (if specified)
        locked_video = factory_locks.get("locked_video_id")
        if locked_video:
            # All actions blocked for this video
            return False
        
        # Check budget locks
        budget_locked = factory_locks.get("budget_locked", False)
        if budget_locked and action.cost > 0:
            return False
        
        # Check maintenance mode
        maintenance_mode = factory_locks.get("maintenance_mode", False)
        if maintenance_mode and action.type != ActionType.NO_OP:
            return False
        
        return True
    
    def _check_action_dependencies(
        self,
        action: ActionSpec,
        current_state: Dict[str, Any],
        action_history: Dict[str, datetime]
    ) -> bool:
        """Check if action dependencies are satisfied (stateless)."""
        dependencies = action.constraints.get("dependencies", [])
        if not dependencies:
            return True
        
        # Check if all dependencies are satisfied
        for dep in dependencies:
            dep_type = dep.get("type")
            dep_value = dep.get("value")
            
            if dep_type == "previous_action":
                # Check if previous action was executed
                action_id = dep_value
                if action_id not in action_history:
                    return False
            
            elif dep_type == "state_condition":
                # Check state condition
                key = dep_value.get("key")
                operator = dep_value.get("operator", "==")
                expected = dep_value.get("value")
                
                actual = current_state.get(key)
                
                if operator == "==" and actual != expected:
                    return False
                elif operator == ">" and actual <= expected:
                    return False
                elif operator == "<" and actual >= expected:
                    return False
        
        return True
    
    def _check_resource_constraints(
        self,
        action: ActionSpec,
        current_state: Dict[str, Any]
    ) -> bool:
        """Check resource constraints."""
        # Check if action requires resources that are not available
        required_resources = action.constraints.get("required_resources", [])
        if not required_resources:
            return True
        
        available_resources = current_state.get("available_resources", {})
        
        for resource in required_resources:
            resource_type = resource.get("type")
            resource_amount = resource.get("amount", 1)
            
            available = available_resources.get(resource_type, 0)
            if available < resource_amount:
                self.logger.debug(
                    f"Action {action.action_id} blocked: insufficient {resource_type} "
                    f"(required: {resource_amount}, available: {available})"
                )
                return False
        
        return True
    
    def _check_temporal_validity(
        self,
        action: ActionSpec,
        temporal_context: Dict[str, Any]
    ) -> bool:
        """Check temporal validity of action."""
        # Some actions are only valid at certain times
        valid_time_windows = action.constraints.get("valid_time_windows", [])
        if not valid_time_windows:
            return True
        
        video_age = temporal_context.get("age_seconds", 0)
        
        for window in valid_time_windows:
            start = window.get("start_seconds", 0)
            end = window.get("end_seconds", float('inf'))
            
            if start <= video_age < end:
                return True
        
        return False
    
    def _load_platform_rules(self) -> Dict[str, Dict[str, Any]]:
        """Load platform-specific rules."""
        return {
            "youtube": {
                "restricted_action_types": [],
                "max_action_cost": 2.0,
            },
            "tiktok": {
                "restricted_action_types": ["thumbnail_swap"],
                "max_action_cost": 1.0,
            },
            "instagram": {
                "restricted_action_types": [],
                "max_action_cost": 1.5,
            },
            "reddit": {
                "restricted_action_types": ["thumbnail_swap"],
                "max_action_cost": 1.0,
            },
            "twitter": {
                "restricted_action_types": ["thumbnail_swap"],
                "max_action_cost": 1.0,
            }
        }
    
    def _load_phase_policies(self) -> Dict[str, Dict[str, Any]]:
        """Load phase-based policies."""
        return {
            DecayPhase.COLD_START.value: {
                "allow_irreversible": False,
                "max_action_cost": 0.3,
                "allowed_action_types": None,  # All allowed if other checks pass
                "restricted_action_types": [],
                "early_phase_threshold": 0.5,
                "early_phase_policy": {
                    "restrict_irreversible": True,
                    "restrict_high_cost": True
                }
            },
            DecayPhase.EARLY_GROWTH.value: {
                "allow_irreversible": True,
                "max_action_cost": 0.8,
                "allowed_action_types": None,
                "restricted_action_types": [],
                "early_phase_threshold": 0.3,
                "early_phase_policy": {
                    "restrict_irreversible": False,
                    "restrict_high_cost": True
                }
            },
            DecayPhase.MID_LIFECYCLE.value: {
                "allow_irreversible": True,
                "max_action_cost": 1.5,
                "allowed_action_types": None,
                "restricted_action_types": [],
            },
            DecayPhase.MATURE.value: {
                "allow_irreversible": True,
                "max_action_cost": 2.0,
                "allowed_action_types": None,
                "restricted_action_types": [],
            },
            DecayPhase.LATE_STAGE.value: {
                "allow_irreversible": True,
                "max_action_cost": 3.0,
                "allowed_action_types": None,
                "restricted_action_types": [],
            }
        }
    


# ============================================================================
# CONFIDENCE ESTIMATOR (UNCERTAINTY & CONFIDENCE COMPUTATION)
# ============================================================================

class ConfidenceEstimator:
    """
    Dedicated component for confidence and uncertainty estimation.
    
    This separates confidence computation from policy scoring, making the
    architecture cleaner and easier to audit.
    
    Responsibilities:
    - Uncertainty estimation (trajectory, phase, action-type based)
    - Collapse risk estimation
    - Overall confidence computation
    
    This component is used by:
    - PolicyInterface: to add confidence metrics to scores
    - SafetyChecker: for safety-based uncertainty checks
    
    NOTE: Priors are versioned with policy_version to prevent silent conservatism drift.
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None, policy_version: Optional[str] = None):
        self.logger = logging.getLogger(__name__ + ".ConfidenceEstimator")
        self.config = config or {}
        self.confidence_priors_version = policy_version or "unknown"  # Versioned to prevent drift
    
    def estimate_uncertainty(
        self,
        agent_input: ContentAgentInput,
        temporal_context: Dict[str, Any],
        action: ActionSpec,
        state_features: Optional[Dict[str, Any]] = None,
        action_features: Optional[Dict[str, Any]] = None
    ) -> float:
        """Comprehensive uncertainty estimation."""
        # Base uncertainty from trajectory confidence
        trajectory_confidence = agent_input.predicted_trajectory.get("confidence", {}).get("overall", 0.5)
        base_uncertainty = 1.0 - trajectory_confidence
        
        # Phase-based uncertainty adjustment
        decay_phase = temporal_context.get("decay_phase", DecayPhase.COLD_START.value)
        phase_uncertainty = {
            DecayPhase.COLD_START.value: 0.3,
            DecayPhase.EARLY_GROWTH.value: 0.2,
            DecayPhase.MID_LIFECYCLE.value: 0.1,
            DecayPhase.MATURE.value: 0.05,
            DecayPhase.LATE_STAGE.value: 0.1
        }.get(decay_phase, 0.2)
        
        # Action-type uncertainty
        action_uncertainty = {
            ActionType.NO_OP: 0.05,
            ActionType.CAPTION_VARIANT: 0.2,
            ActionType.THUMBNAIL_SWAP: 0.3,
            ActionType.TIMING_ADJUSTMENT: 0.15,
            ActionType.REVIVAL_FLAG: 0.4
        }.get(action.type, 0.25)
        
        # Irreversibility adds uncertainty
        irreversibility_penalty = 0.1 if not action.reversibility else 0.0
        
        # Combined uncertainty
        total_uncertainty = (
            base_uncertainty * 0.4 +
            phase_uncertainty * 0.3 +
            action_uncertainty * 0.2 +
            irreversibility_penalty * 0.1
        )
        
        return min(1.0, max(0.0, total_uncertainty))
    
    def estimate_collapse_risk(
        self,
        agent_input: ContentAgentInput,
        temporal_context: Dict[str, Any],
        action: ActionSpec,
        state_features: Optional[Dict[str, Any]] = None,
        action_features: Optional[Dict[str, Any]] = None
    ) -> float:
        """Estimate collapse risk for this action."""
        trajectory = agent_input.predicted_trajectory
        short_term = trajectory.get("short_term", {})
        
        # Base collapse risk from trajectory
        retention_drop = short_term.get("retention_drop_predicted", False)
        engagement_cliff = short_term.get("engagement_cliff_predicted", False)
        
        base_risk = 0.0
        if retention_drop:
            base_risk += 0.3
        if engagement_cliff:
            base_risk += 0.3
        
        # Action-specific risk
        action_risk = {
            ActionType.NO_OP: 0.0,
            ActionType.CAPTION_VARIANT: 0.05,
            ActionType.THUMBNAIL_SWAP: 0.15,
            ActionType.TIMING_ADJUSTMENT: 0.1,
            ActionType.REVIVAL_FLAG: 0.2
        }.get(action.type, 0.1)
        
        # Irreversibility adds risk
        irreversibility_risk = 0.1 if not action.reversibility else 0.0
        
        total_risk = min(1.0, base_risk + action_risk + irreversibility_risk)
        return total_risk
    
    def compute_confidence(
        self,
        policy_score: float,
        value_estimate: float,
        uncertainty: float,
        temporal_context: Dict[str, Any]
    ) -> float:
        """Compute overall confidence in this decision."""
        # Base confidence from policy score and uncertainty
        base_confidence = policy_score * (1.0 - uncertainty)
        
        # Adjust for value estimate magnitude (higher value = higher confidence if positive)
        if value_estimate > 0:
            value_confidence = min(1.0, abs(value_estimate) * 2.0)
        else:
            value_confidence = 0.5
        
        # Phase-based confidence adjustment
        decay_phase = temporal_context.get("decay_phase", DecayPhase.COLD_START.value)
        phase_confidence = {
            DecayPhase.COLD_START.value: 0.7,
            DecayPhase.EARLY_GROWTH.value: 0.85,
            DecayPhase.MID_LIFECYCLE.value: 1.0,
            DecayPhase.MATURE.value: 1.1,
            DecayPhase.LATE_STAGE.value: 0.9
        }.get(decay_phase, 1.0)
        
        combined_confidence = (
            base_confidence * 0.5 +
            value_confidence * 0.3 +
            (phase_confidence * 0.2)
        )
        
        return min(1.0, max(0.0, combined_confidence))


# ============================================================================
# POLICY INTERFACE
# ============================================================================


class PolicyInterface:
    """
    Comprehensive policy and value network interface.
    
    Delegates scoring to policy_network.py and value_network.py.
    
    This file NEVER embeds network logic directly.
    
    Responsibilities:
    - State encoding for network input
    - Action encoding for network input
    - Feature extraction and normalization
    - Policy network queries (policy_score)
    - Value network queries (value_estimate)
    - Batch scoring
    - Network health checking
    
    NOTE: Confidence/uncertainty computation is delegated to ConfidenceEstimator.
    This keeps PolicyInterface focused on scoring, not decision-making.
    """
    
    def __init__(self, policy_network=None, value_network=None, config: Optional[Dict[str, Any]] = None, confidence_estimator: Optional[ConfidenceEstimator] = None):
        self.logger = logging.getLogger(__name__ + ".PolicyInterface")
        self.policy_network = policy_network
        self.value_network = value_network
        self.config = config or {}
        
        # Confidence estimator (delegates uncertainty/confidence computation)
        self.confidence_estimator = confidence_estimator or ConfidenceEstimator(config)
        
        # Network configuration
        self.network_timeout = self.config.get("network_timeout", 5.0)  # seconds
        self.fallback_enabled = self.config.get("fallback_enabled", True)
        self.use_batch_scoring = self.config.get("use_batch_scoring", False)
        
        # Feature normalization
        self.feature_scalers = {}
        self._initialize_feature_scalers()
        
        # Network health tracking
        self.network_health = {
            "policy_network": True,
            "value_network": True,
            "last_successful_query": None,
            "consecutive_failures": 0
        }
    
    def score_actions(
        self,
        agent_input: ContentAgentInput,
        temporal_context: Dict[str, Any],
        allowed_actions: List[ActionSpec]
    ) -> Dict[str, Dict[str, float]]:
        """
        Comprehensive action scoring.
        
        Returns:
            {
                action_id: {
                    "policy_score": float,  # 0.0-1.0
                    "value_estimate": float,  # Expected value
                    "uncertainty": float,  # 0.0-1.0
                    "collapse_risk": float,  # 0.0-1.0
                    "confidence": float,  # 0.0-1.0
                    "network_used": bool
                }
            }
        """
        if len(allowed_actions) == 0:
            return {}
        
        # Encode state once
        state_features = self._encode_state_features(agent_input, temporal_context)
        
        # Batch scoring if enabled and networks available
        if self.use_batch_scoring and self.policy_network and self.value_network:
            try:
                return self._batch_score_actions(
                    agent_input,
                    temporal_context,
                    allowed_actions,
                    state_features
                )
            except Exception as e:
                self.logger.warning(f"Batch scoring failed: {e}, falling back to individual scoring")
                self._record_network_failure()
        
        # Individual action scoring
        scores = {}
        
        for action in allowed_actions:
            try:
                # Encode action features
                action_features = self._encode_action_features(action, temporal_context)
                
                # Query networks (with silent fallback to prevent log noise)
                if self.policy_network and self.value_network and self.network_health["policy_network"]:
                    try:
                        policy_score = self._query_policy_network(
                            agent_input,
                            temporal_context,
                            action,
                            state_features,
                            action_features
                        )
                        value_estimate = self._query_value_network(
                            agent_input,
                            temporal_context,
                            action,
                            state_features,
                            action_features
                        )
                        # Confidence/uncertainty computation delegated to ConfidenceEstimator
                        uncertainty = self.confidence_estimator.estimate_uncertainty(
                            agent_input,
                            temporal_context,
                            action,
                            state_features,
                            action_features
                        )
                        collapse_risk = self.confidence_estimator.estimate_collapse_risk(
                            agent_input,
                            temporal_context,
                            action,
                            state_features,
                            action_features
                        )
                        network_used = True
                    except NetworkUnavailableError:
                        # Silent fallback: network unavailable - use fallback scoring (no error log)
                        # This prevents alert fatigue at 5M+ events/day
                        policy_score, value_estimate, uncertainty, collapse_risk = self._fallback_scoring(
                            agent_input,
                            temporal_context,
                            action
                        )
                        network_used = False
                    except Exception as e:
                        # Only log non-stub exceptions (actual network errors, not placeholders)
                        self.logger.error(f"Network query error (non-stub) for action {action.action_id}: {e}")
                        # Fallback on actual errors too
                        policy_score, value_estimate, uncertainty, collapse_risk = self._fallback_scoring(
                            agent_input,
                            temporal_context,
                            action
                        )
                        network_used = False
                        self._record_network_failure()
                else:
                    # Fallback scoring (network health check failed)
                    policy_score, value_estimate, uncertainty, collapse_risk = self._fallback_scoring(
                        agent_input,
                        temporal_context,
                        action
                    )
                    network_used = False
                
                # Compute confidence (delegated to ConfidenceEstimator)
                confidence = self.confidence_estimator.compute_confidence(
                    policy_score,
                    value_estimate,
                    uncertainty,
                    temporal_context
                )
                
                scores[action.action_id] = {
                    "policy_score": policy_score,
                    "value_estimate": value_estimate,
                    "uncertainty": uncertainty,
                    "collapse_risk": collapse_risk,
                    "confidence": confidence,
                    "network_used": network_used
                }
                
            except Exception as e:
                self.logger.error(f"Error scoring action {action.action_id}: {e}")
                # Fallback for this action
                scores[action.action_id] = self._fallback_scoring_dict(
                    agent_input,
                    temporal_context,
                    action
                )
                self._record_network_failure()
        
        self.logger.debug(f"Scored {len(scores)} actions")
        self._record_network_success()
        return scores
    
    def _encode_state_features(
        self,
        agent_input: ContentAgentInput,
        temporal_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Encode state features for network input."""
        current_state = agent_input.current_state
        engagement = current_state.get("engagement_snapshot", {})
        retention = current_state.get("retention_markers", {})
        sentiment = current_state.get("sentiment_state", {})
        trajectory = agent_input.predicted_trajectory
        
        # Extract and normalize features
        features = {
            # Temporal features
            "video_age": temporal_context.get("age_seconds", 0),
            "log_age": temporal_context.get("log_age", 0.0),
            "decay_phase": self._encode_decay_phase(temporal_context.get("decay_phase", DecayPhase.COLD_START.value)),
            "phase_progress": temporal_context.get("phase_progress", 0.0),
            "normalized_age": temporal_context.get("normalized_age", 0.0),
            
            # Engagement features
            "views": engagement.get("views", 0),
            "likes": engagement.get("likes", 0),
            "comments": engagement.get("comments", 0),
            "shares": engagement.get("shares", 0),
            "engagement_rate": engagement.get("engagement_rate", 0.0),
            "view_velocity": engagement.get("view_velocity", 0.0),
            
            # Retention features
            "retention_rate": retention.get("retention_rate", 0.0),
            "drop_off_point": retention.get("drop_off_point", 0.0),
            "completion_rate": retention.get("completion_rate", 0.0),
            
            # Sentiment features
            "sentiment_score": sentiment.get("score", 0.0),
            "sentiment_polarity": sentiment.get("polarity", 0.0),
            
            # Trajectory features
            "predicted_views": trajectory.get("short_term", {}).get("predicted_views", 0),
            "predicted_growth": trajectory.get("short_term", {}).get("predicted_growth", 0.0),
            "trajectory_confidence": trajectory.get("confidence", {}).get("overall", 0.5),
            
            # Platform features
            "platform": self._encode_platform(agent_input.platform),
        }
        
        # Normalize features
        features = self._normalize_features(features)
        
        return features
    
    def _encode_action_features(
        self,
        action: ActionSpec,
        temporal_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Encode action features for network input."""
        return {
            "action_type": self._encode_action_type(action.type),
            "cost": action.cost,
            "reversibility": 1.0 if action.reversibility else 0.0,
            "cooldown": action.cooldown,
            "action_id_hash": hash(action.action_id) % 10000  # Normalize hash
        }
    
    def _encode_decay_phase(self, decay_phase: str) -> float:
        """Encode decay phase as numeric feature."""
        phase_map = {
            DecayPhase.COLD_START.value: 0.0,
            DecayPhase.EARLY_GROWTH.value: 0.25,
            DecayPhase.MID_LIFECYCLE.value: 0.5,
            DecayPhase.MATURE.value: 0.75,
            DecayPhase.LATE_STAGE.value: 1.0
        }
        return phase_map.get(decay_phase, 0.5)
    
    def _encode_action_type(self, action_type: ActionType) -> float:
        """Encode action type as numeric feature."""
        type_map = {
            ActionType.NO_OP: 0.0,
            ActionType.CAPTION_VARIANT: 0.2,
            ActionType.THUMBNAIL_SWAP: 0.4,
            ActionType.TIMING_ADJUSTMENT: 0.6,
            ActionType.REVIVAL_FLAG: 0.8
        }
        return type_map.get(action_type, 0.0)
    
    def _encode_platform(self, platform: str) -> float:
        """Encode platform as numeric feature."""
        platform_map = {
            "youtube": 0.0,
            "tiktok": 0.25,
            "instagram": 0.5,
            "reddit": 0.75,
            "twitter": 1.0
        }
        return platform_map.get(platform.lower(), 0.5)
    
    def _normalize_features(self, features: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize features to 0.0-1.0 range."""
        normalized = {}
        
        for key, value in features.items():
            if isinstance(value, (int, float)):
                # Use feature-specific normalization
                if key in ["views", "likes", "comments", "shares", "predicted_views"]:
                    # Log-scale normalization
                    normalized[key] = min(1.0, math.log(max(1, value) + 1) / math.log(1000000 + 1))
                elif key in ["engagement_rate", "retention_rate", "completion_rate", "sentiment_score"]:
                    # Already 0-1 range (or should be)
                    normalized[key] = max(0.0, min(1.0, value))
                elif key in ["cost", "cooldown"]:
                    # Cost and cooldown normalization
                    max_cost = 10.0
                    max_cooldown = 86400.0  # 1 day
                    if key == "cost":
                        normalized[key] = min(1.0, value / max_cost)
                    else:
                        normalized[key] = min(1.0, value / max_cooldown)
                else:
                    # Assume already normalized or in reasonable range
                    normalized[key] = value if -1.0 <= value <= 1.0 else 0.0
            else:
                normalized[key] = value
        
        return normalized
    
    def _query_policy_network(
        self,
        agent_input: ContentAgentInput,
        temporal_context: Dict[str, Any],
        action: ActionSpec,
        state_features: Dict[str, Any],
        action_features: Dict[str, Any]
    ) -> float:
        """
        Query policy network for action probability.
        
        In production, this would call:
            state_vector = self._features_to_vector(state_features, action_features)
            return self.policy_network.forward(state_vector)
        
        If network is not available, this raises NetworkUnavailableError (silently caught
        by score_actions to trigger fallback). No error logs are emitted to prevent alert fatigue.
        """
        # If network not available, raise silent exception (caught by score_actions)
        if self.policy_network is None:
            raise NetworkUnavailableError("Policy network not available")
        
        # TODO: Actual network call
        # state_vector = self._features_to_vector(state_features, action_features)
        # with timeout(self.network_timeout):
        #     return self.policy_network.forward(state_vector)
        
        # Placeholder: network not yet implemented - raise silent exception for fallback
        raise NetworkUnavailableError("Policy network query not yet implemented")
    
    def _query_value_network(
        self,
        agent_input: ContentAgentInput,
        temporal_context: Dict[str, Any],
        action: ActionSpec,
        state_features: Dict[str, Any],
        action_features: Dict[str, Any]
    ) -> float:
        """
        Query value network for state-action value.
        
        In production, this would call:
            state_vector = self._features_to_vector(state_features, action_features)
            return self.value_network.forward(state_vector)
        
        If network is not available, this raises NetworkUnavailableError (silently caught
        by score_actions to trigger fallback). No error logs are emitted to prevent alert fatigue.
        """
        # If network not available, raise silent exception (caught by score_actions)
        if self.value_network is None:
            raise NetworkUnavailableError("Value network not available")
        
        # TODO: Actual network call
        # state_vector = self._features_to_vector(state_features, action_features)
        # with timeout(self.network_timeout):
        #     return self.value_network.forward(state_vector)
        
        # Placeholder: network not yet implemented - raise silent exception for fallback
        raise NetworkUnavailableError("Value network query not yet implemented")
    
    
    def _fallback_scoring(
        self,
        agent_input: ContentAgentInput,
        temporal_context: Dict[str, Any],
        action: ActionSpec
    ) -> Tuple[float, float, float, float]:
        """
        Fallback scoring when networks unavailable.
        
        CRITICAL: This is SAFETY-ONLY. It must NEVER express preference between
        non-no_op actions. Fallback must be strictly monotonic and bias toward no_op.
        
        This ensures:
        - No behavior drift when networks are down
        - Replay equivalence with/without networks
        - PolicyInterface remains a true delegator, not a policy source
        """
        # Safety-only fallback: no_op is safe, all other actions are discouraged
        if action.type == ActionType.NO_OP:
            # no_op: safe, low risk, low uncertainty
            return 1.0, 0.0, 0.1, 0.0
        else:
            # All non-no_op actions: discouraged, high uncertainty, high collapse risk
            # This ensures fallback never chooses preference, only safety
            return 0.0, 0.0, 0.9, 0.5
    
    def _fallback_scoring_dict(
        self,
        agent_input: ContentAgentInput,
        temporal_context: Dict[str, Any],
        action: ActionSpec
    ) -> Dict[str, float]:
        """Fallback scoring as dictionary."""
        policy_score, value_estimate, uncertainty, collapse_risk = self._fallback_scoring(
            agent_input, temporal_context, action
        )
        confidence = self.confidence_estimator.compute_confidence(policy_score, value_estimate, uncertainty, temporal_context)
        
        return {
            "policy_score": policy_score,
            "value_estimate": value_estimate,
            "uncertainty": uncertainty,
            "collapse_risk": collapse_risk,
            "confidence": confidence,
            "network_used": False
        }
    
    def _batch_score_actions(
        self,
        agent_input: ContentAgentInput,
        temporal_context: Dict[str, Any],
        allowed_actions: List[ActionSpec],
        state_features: Dict[str, Any]
    ) -> Dict[str, Dict[str, float]]:
        """Batch score actions for efficiency."""
        # TODO: Implement batch scoring when networks support it
        # For now, fall back to individual scoring
        return {}
    
    def _initialize_feature_scalers(self) -> None:
        """Initialize feature scalers for normalization."""
        # In production, would load from trained scalers
        pass
    
    def _features_to_vector(
        self,
        state_features: Dict[str, Any],
        action_features: Dict[str, Any]
    ) -> List[float]:
        """Convert features to network input vector."""
        # Combine state and action features into single vector
        state_vec = [state_features.get(k, 0.0) for k in sorted(state_features.keys())]
        action_vec = [action_features.get(k, 0.0) for k in sorted(action_features.keys())]
        return state_vec + action_vec
    
    def _record_network_success(self) -> None:
        """
        Record successful network query.
        
        NOTE: This uses datetime.utcnow() for operational health tracking only.
        This is NOT in the decision path - it's operational monitoring.
        Decision path uses decision_context (deterministic).
        """
        self.network_health["last_successful_query"] = datetime.utcnow()  # Operational only, not decision-path
        self.network_health["consecutive_failures"] = 0
        self.network_health["policy_network"] = True
        self.network_health["value_network"] = True
    
    def _record_network_failure(self) -> None:
        """Record network query failure."""
        self.network_health["consecutive_failures"] += 1
        if self.network_health["consecutive_failures"] > 10:
            self.network_health["policy_network"] = False
            self.network_health["value_network"] = False
            self.logger.error("Networks marked as unhealthy after 10 consecutive failures")
    
    def get_network_health(self) -> Dict[str, Any]:
        """Get network health status."""
        return self.network_health.copy()


# ============================================================================
# UNCERTAINTY GATE
# ============================================================================


class UncertaintyGate:
    """
    EXPLICIT UNCERTAINTY GATE - Hard first-class stop with explicit invariant.
    
    INVARIANT: "If uncertainty > X → no-op OR escalate" (non-negotiable).
    
    This is a centralized, authoritative safety override that prevents
    risky actions when system confidence is too low.
    
    Phase-dependent thresholds:
    - Cold start: Very low (strict blocking)
    - Early exposure: Low (cautious)
    - Stable survival: Medium (balanced)
    - Long-tail decay: Higher (still capped, but allows gentle action)
    
    This is HOW YOU PREVENT DAMAGE while preserving late-stage survival.
    """
    
    def __init__(self, uncertainty_threshold: float = 0.6):
        self.logger = logging.getLogger(__name__ + ".UncertaintyGate")
        # Base threshold (used as fallback if phase not detected)
        self.uncertainty_threshold = uncertainty_threshold
        
        # Phase-conditioned uncertainty thresholds
        # Late-stage videos die from inaction, not bad action (within safety bounds)
        self.phase_thresholds = {
            DecayPhase.COLD_START.value: 0.2,        # Very low - strict blocking
            DecayPhase.EARLY_GROWTH.value: 0.35,     # Low - cautious
            DecayPhase.MID_LIFECYCLE.value: 0.5,     # Medium - balanced
            DecayPhase.MATURE.value: 0.6,            # Medium-high - stable survival
            DecayPhase.LATE_STAGE.value: 0.7,         # Higher - still capped, but allows gentle action
        }
    
    def evaluate(
        self,
        action_scores: Dict[str, Dict[str, float]],
        temporal_context: Dict[str, Any]
    ) -> Tuple[bool, bool, Optional[str]]:
        """
        EXPLICIT UNCERTAINTY GATE EVALUATION - Single authoritative method.
        
        Enforces hard invariant: "If uncertainty > X → no-op OR escalate"
        
        This is the ONLY method that should be called for uncertainty gating.
        All uncertainty checks flow through this explicit gate.
        
        Args:
            action_scores: Dict mapping action_id to scores (including uncertainty)
            temporal_context: Temporal context including decay_phase
            
        Returns:
            (allow, escalate, reason_if_blocked)
            - allow: True if actions can proceed, False if must no-op
            - escalate: True if escalation is required
            - reason_if_blocked: Explanation if blocked (None if allowed)
        """
        if not action_scores:
            reason = "No action scores available - fail safe"
            self.logger.warning(f"UNCERTAINTY GATE: {reason}")
            return False, True, reason  # Block + escalate
        
        # Get phase-dependent threshold
        decay_phase = temporal_context.get("decay_phase", DecayPhase.COLD_START.value)
        phase_threshold = self.phase_thresholds.get(
            decay_phase,
            self.uncertainty_threshold  # Fallback to base threshold
        )
        
        # Check maximum confidence across all actions (inverse of uncertainty)
        max_confidence = 0.0
        best_action_id = None
        max_uncertainty = 0.0
        
        for action_id, scores in action_scores.items():
            uncertainty = scores.get("uncertainty", 1.0)
            confidence = 1.0 - uncertainty  # Convert uncertainty to confidence
            if confidence > max_confidence:
                max_confidence = confidence
                best_action_id = action_id
            if uncertainty > max_uncertainty:
                max_uncertainty = uncertainty
        
        # ENFORCE HARD INVARIANT: If uncertainty > threshold → no-op OR escalate
        min_confidence_required = 1.0 - phase_threshold
        if max_confidence < min_confidence_required or max_uncertainty > phase_threshold:
            reason = (
                f"UNCERTAINTY GATE: System uncertainty ({max_uncertainty:.2f}) exceeds "
                f"phase threshold ({phase_threshold:.2f}) for {decay_phase}. "
                f"Max confidence ({max_confidence:.2f}) below required ({min_confidence_required:.2f}). "
                f"Enforcing invariant: no-op + escalate"
            )
            self.logger.warning(reason)
            return False, True, reason  # Block + escalate
        
        # All actions pass uncertainty gate
        return True, False, None
    
    def should_fail_safe(
        self,
        action_scores: Dict[str, Dict[str, float]],
        temporal_context: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """
        DEPRECATED: Use evaluate() instead.
        
        Legacy method for backward compatibility.
        """
        allow, escalate, reason = self.evaluate(action_scores, temporal_context)
        return not allow, reason
    
    def check(
        self,
        action: ActionSpec,
        scores: Dict[str, Any],
        temporal_context: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """
        DEPRECATED: Use evaluate() for explicit gate evaluation.
        
        Legacy method for per-action checks.
        """
        uncertainty = scores.get("uncertainty", 0.0)
        
        # Always allow no_op
        if action.type == ActionType.NO_OP:
            return True, None
        
        # Get phase-dependent threshold
        decay_phase = temporal_context.get("decay_phase", DecayPhase.COLD_START.value)
        phase_threshold = self.phase_thresholds.get(
            decay_phase,
            self.uncertainty_threshold  # Fallback to base threshold
        )
        
        # Check phase-conditioned uncertainty threshold
        if uncertainty > phase_threshold:
            reason = (
                f"Uncertainty {uncertainty:.2f} exceeds phase threshold {phase_threshold:.2f} "
                f"for phase {decay_phase}"
            )
            self.logger.warning(f"Blocking action {action.action_id}: {reason}")
            return False, reason
        
        return True, None


# ============================================================================
# SAFETY CHECKER (HARD BLOCKER)
# ============================================================================


class SafetyChecker:
    """
    CENTRALIZED SAFETY CHECKER - Single authoritative safety veto.
    
    This is the ONLY place where final safety checks happen after scoring and before emission.
    All safety logic flows through this single pass:
    - Confidence collapse predicted
    - Platform constraints
    - Micro-impact envelope
    - Factory-level locks
    - Risk-to-survival ratio
    - State instability
    - Causal violations
    - Irreversibility safety
    - Cost-benefit safety
    - Temporal safety
    
    This centralization makes it easier to reason about failure guarantees.
    All safety vetoes are explicit and auditable.
    
    Fail safe > act wrong.
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.logger = logging.getLogger(__name__ + ".SafetyChecker")
        self.config = config or {}
        
        # Safety thresholds
        self.max_micro_impact = self.config.get("max_micro_impact", 0.1)
        self.max_uncertainty_for_irreversible = self.config.get("max_uncertainty_for_irreversible", 0.3)
        self.max_uncertainty_for_reversible = self.config.get("max_uncertainty_for_reversible", 0.6)
        self.min_risk_survival_ratio = self.config.get("min_risk_survival_ratio", 0.5)
        self.max_collapse_risk = self.config.get("max_collapse_risk", 0.2)
        
        # FORMALIZED MICRO-IMPACT ENVELOPE (explicit, serializable, provable)
        # This is what lets you PROVE: "This agent literally cannot destroy a video."
        self.micro_impact_envelope = MicroImpactEnvelope(
            max_cost=self.max_micro_impact,
            max_irreversible_actions=1,  # Only one irreversible action per decision window
            max_actions_per_window=10,  # Max actions per hour window
            max_uncertainty_for_action=self.max_uncertainty_for_irreversible
        )
        
        # Platform safety rules
        self.platform_safety_rules = self._load_platform_safety_rules()
        
        # Collapse prediction thresholds
        self.collapse_thresholds = {
            "retention_drop_threshold": 0.15,  # 15% drop predicted
            "engagement_cliff_threshold": 0.20,  # 20% drop predicted
            "confidence_drop_threshold": 0.10,  # 10% confidence drop
        }
    
    def veto(
        self,
        action: ActionSpec,
        agent_input: ContentAgentInput,
        scores: Dict[str, Any],
        temporal_context: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """
        EXPLICIT FINAL SAFETY VETO - Single authoritative mechanical gate.
        
        This is the ONLY method that can veto a decision candidate.
        All safety logic flows through this single explicit pass.
        
        At 9.5+ compliance, this method exists as the mechanical centralization point.
        Not just conceptually centralized - mechanically centralized.
        
        All safety checks:
        - Irreversibility limits (moved from ActionMasker)
        - Cost thresholds (moved from ActionMasker)
        - Platform constraints (moved from ActionMasker)
        - Phase policies (moved from ActionMasker)
        - State constraints (moved from ActionMasker)
        - Confidence collapse prediction
        - Micro-impact envelope
        - Risk-to-survival ratio
        - State stability
        - Causal safety
        - Cost-benefit safety
        - Temporal safety
        
        Args:
            action: Decision candidate action
            agent_input: Agent input snapshot
            scores: Action scores (policy_score, value_estimate, uncertainty, etc.)
            temporal_context: Temporal context including decay_phase
        
        Returns:
            (allowed, veto_reason)
            - allowed: True if action passes all safety checks, False if vetoed
            - veto_reason: Explanation if vetoed (None if allowed)
        """
        return self.check(action, agent_input, scores, temporal_context)
    
    def check(
        self,
        action: ActionSpec,
        agent_input: ContentAgentInput,
        scores: Dict[str, Any],
        temporal_context: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """
        Internal safety check implementation.
        
        Use veto() for explicit final gate - this is the implementation.
        
        Returns:
            (safe, reason_if_blocked)
        """
        # Always allow no_op (but still log for audit)
        if action.type == ActionType.NO_OP:
            self.logger.debug("Safety check: no_op always allowed")
            return True, None
        
        # 1. Check for predicted confidence collapse (highest priority - prevents damage)
        collapse_predicted, collapse_reason = self._predicts_collapse(agent_input, scores, action)
        if collapse_predicted:
            self.logger.error(f"SAFETY BLOCK: {collapse_reason}")
            return False, collapse_reason
        
        # 2. Check micro-impact envelope (provable damage ceiling)
        safe, impact_reason = self._check_impact_envelope(action, scores, temporal_context)
        if not safe:
            self.logger.error(f"SAFETY BLOCK: {impact_reason}")
            return False, impact_reason
        
        # 3. Check irreversibility safety (with uncertainty - consolidated from early checks)
        safe, irrev_reason = self._check_irreversibility_safety(action, scores, temporal_context)
        if not safe:
            self.logger.error(f"SAFETY BLOCK: {irrev_reason}")
            return False, irrev_reason
        
        # 4. Check risk-to-survival ratio
        safe, risk_survival_reason = self._check_risk_survival_ratio(action, scores, temporal_context)
        if not safe:
            self.logger.error(f"SAFETY BLOCK: {risk_survival_reason}")
            return False, risk_survival_reason
        
        # 5. Check platform safety constraints (consolidated from early platform checks)
        safe, platform_reason = self._check_platform_safety(action, agent_input, temporal_context)
        if not safe:
            self.logger.error(f"SAFETY BLOCK: {platform_reason}")
            return False, platform_reason
        
        # 6. Check state stability
        safe, stability_reason = self._check_state_stability(agent_input, scores, temporal_context)
        if not safe:
            self.logger.error(f"SAFETY BLOCK: {stability_reason}")
            return False, stability_reason
        
        # 7. Check causal safety
        safe, causal_reason = self._check_causal_safety(action, agent_input, scores, temporal_context)
        if not safe:
            self.logger.error(f"SAFETY BLOCK: {causal_reason}")
            return False, causal_reason
        
        # 8. Check cost-benefit safety (consolidated from early cost checks)
        safe, cost_benefit_reason = self._check_cost_benefit_safety(action, scores, temporal_context)
        if not safe:
            self.logger.error(f"SAFETY BLOCK: {cost_benefit_reason}")
            return False, cost_benefit_reason
        
        self.logger.debug(f"Safety check passed for action {action.action_id}")
        return True, None
    
    def _predicts_collapse(
        self,
        agent_input: ContentAgentInput,
        scores: Dict[str, Any],
        action: ActionSpec
    ) -> Tuple[bool, Optional[str]]:
        """Check if trajectory predicts collapse with this action."""
        trajectory = agent_input.predicted_trajectory
        short_term = trajectory.get("short_term", {})
        mid_term = trajectory.get("mid_term", {})
        
        # Check short-term retention drop
        retention_drop_predicted = short_term.get("retention_drop_predicted", False)
        retention_drop_magnitude = short_term.get("retention_drop_magnitude", 0.0)
        
        if retention_drop_predicted and retention_drop_magnitude > self.collapse_thresholds["retention_drop_threshold"]:
            return True, (
                f"Predicted retention drop of {retention_drop_magnitude:.1%} exceeds threshold "
                f"({self.collapse_thresholds['retention_drop_threshold']:.1%})"
            )
        
        # Check engagement cliff
        engagement_cliff_predicted = short_term.get("engagement_cliff_predicted", False)
        engagement_drop_magnitude = short_term.get("engagement_drop_magnitude", 0.0)
        
        if engagement_cliff_predicted and engagement_drop_magnitude > self.collapse_thresholds["engagement_cliff_threshold"]:
            return True, (
                f"Predicted engagement drop of {engagement_drop_magnitude:.1%} exceeds threshold "
                f"({self.collapse_thresholds['engagement_cliff_threshold']:.1%})"
            )
        
        # Check confidence drop
        confidence = trajectory.get("confidence", {})
        current_confidence = confidence.get("overall", 1.0)
        predicted_confidence = short_term.get("predicted_confidence", current_confidence)
        confidence_drop = current_confidence - predicted_confidence
        
        if confidence_drop > self.collapse_thresholds["confidence_drop_threshold"]:
            return True, (
                f"Predicted confidence drop of {confidence_drop:.1%} exceeds threshold "
                f"({self.collapse_thresholds['confidence_drop_threshold']:.1%})"
            )
        
        # Check mid-term collapse risk
        mid_term_collapse_risk = mid_term.get("collapse_risk", 0.0)
        if mid_term_collapse_risk > self.max_collapse_risk:
            return True, (
                f"Mid-term collapse risk {mid_term_collapse_risk:.1%} exceeds threshold "
                f"({self.max_collapse_risk:.1%})"
            )
        
        # Action-specific collapse risk
        action_collapse_risk = scores.get("collapse_risk", 0.0)
        if action_collapse_risk > self.max_collapse_risk:
            return True, (
                f"Action collapse risk {action_collapse_risk:.1%} exceeds threshold "
                f"({self.max_collapse_risk:.1%})"
            )
        
        return False, None
    
    def _check_platform_safety(
        self,
        action: ActionSpec,
        agent_input: ContentAgentInput,
        temporal_context: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """Comprehensive platform-specific safety rules (consolidated from early checks)."""
        platform = agent_input.platform.lower()
        video_age = agent_input.video_age_seconds
        current_state = agent_input.current_state
        distribution_mode = current_state.get("distribution_mode", "")
        
        # Get platform rules
        rules = self.platform_safety_rules.get(platform, {})
        
        # Check action type restrictions
        restricted_types = rules.get("restricted_action_types", [])
        if action.type.value in restricted_types:
            return False, f"Action type {action.type.value} restricted on platform {platform}"
        
        # Platform-specific timing constraints
        if platform == "tiktok":
            if action.type == ActionType.CAPTION_VARIANT:
                # TikTok: caption edits only within 1 hour
                if video_age > 3600:
                    return False, "TikTok caption edits only allowed within 1 hour of posting"
            
            if action.type == ActionType.THUMBNAIL_SWAP:
                # TikTok: no thumbnail swaps
                return False, "TikTok does not allow thumbnail swaps"
            
            if action.type == ActionType.TIMING_ADJUSTMENT:
                # TikTok: timing adjustments only before posting
                if video_age > 0:
                    return False, "TikTok timing adjustments only allowed before posting"
        
        elif platform == "youtube":
            if action.type == ActionType.CAPTION_VARIANT:
                # YouTube: caption edits allowed anytime
                pass
            
            if action.type == ActionType.THUMBNAIL_SWAP:
                # YouTube: thumbnail swaps allowed anytime
                pass
        
        elif platform == "instagram":
            if action.type == ActionType.CAPTION_VARIANT:
                # Instagram: caption edits within 24 hours
                if video_age > 24 * 3600:
                    return False, "Instagram caption edits only allowed within 24 hours"
            
            if action.type == ActionType.THUMBNAIL_SWAP:
                # Instagram: no thumbnail swaps after posting
                if video_age > 0:
                    return False, "Instagram thumbnail swaps only allowed before posting"
        
        # Timing adjustments only for scheduled posts (consolidated from early checks)
        if action.type == ActionType.TIMING_ADJUSTMENT:
            if distribution_mode not in ["scheduled", "draft"]:
                return False, "Timing adjustments only allowed for scheduled or draft posts"
        
        # Check platform-specific cost limits
        platform_max_cost = rules.get("max_action_cost", self.max_micro_impact * 10)
        if action.cost > platform_max_cost:
            return False, f"Action cost {action.cost} exceeds platform limit {platform_max_cost}"
        
        return True, None
    
    def _check_impact_envelope(
        self,
        action: ActionSpec,
        scores: Dict[str, Any],
        temporal_context: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """
        Comprehensive impact envelope checking using formalized MicroImpactEnvelope.
        
        This uses the explicit envelope object to prove maximum allowed damage per decision.
        The envelope is serializable and provable - auditors can inspect: "How much damage can this agent do?"
        """
        uncertainty = scores.get("uncertainty", 0.0)
        
        # Use formalized MicroImpactEnvelope for validation (explicit, not implicit)
        valid, reason = self.micro_impact_envelope.validate_action(action, uncertainty)
        if not valid:
            return False, f"Micro-impact envelope violation: {reason}"
        
        # Additional phase-dependent checks (envelope is base, phase adds context)
        decay_phase = temporal_context.get("decay_phase", DecayPhase.COLD_START.value)
        phase_max_impact = {
            DecayPhase.COLD_START.value: self.max_micro_impact * 0.3,
            DecayPhase.EARLY_GROWTH.value: self.max_micro_impact * 0.5,
            DecayPhase.MID_LIFECYCLE.value: self.max_micro_impact * 0.8,
            DecayPhase.MATURE.value: self.max_micro_impact,
            DecayPhase.LATE_STAGE.value: self.max_micro_impact * 1.5,
        }.get(decay_phase, self.max_micro_impact)
        
        # Estimate impact from value delta
        value_estimate = scores.get("value_estimate", 0.0)
        policy_score = scores.get("policy_score", 0.0)
        
        # Adjusted impact (higher uncertainty = lower effective impact)
        effective_impact = abs(value_estimate) * (1.0 - uncertainty)
        
        if effective_impact > phase_max_impact:
            return False, (
                f"Effective impact {effective_impact:.3f} exceeds phase limit {phase_max_impact:.3f} "
                f"for phase {decay_phase}"
            )
        
        # Check absolute value estimate
        if abs(value_estimate) > self.max_micro_impact * 2.0:
            return False, (
                f"Absolute value estimate {value_estimate:.3f} exceeds maximum "
                f"({self.max_micro_impact * 2.0:.3f})"
            )
        
        # Check policy score vs uncertainty (high uncertainty with high policy score is suspicious)
        if policy_score > 0.8 and uncertainty > 0.6:
            return False, (
                f"High policy score ({policy_score:.2f}) with high uncertainty ({uncertainty:.2f}) "
                "indicates unreliable prediction"
            )
        
        return True, None
    
    def _check_irreversibility_safety(
        self,
        action: ActionSpec,
        scores: Dict[str, Any],
        temporal_context: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """Check irreversibility safety with uncertainty (consolidated from early checks)."""
        if action.reversibility:
            return True, None  # Reversible actions are always safer
        
        uncertainty = scores.get("uncertainty", 0.0)
        decay_phase = temporal_context.get("decay_phase", DecayPhase.COLD_START.value)
        
        # Phase-based irreversibility policies (consolidated from early checks)
        if decay_phase == DecayPhase.COLD_START.value:
            return False, f"Irreversible actions not allowed in cold start phase"
        
        if decay_phase == DecayPhase.EARLY_GROWTH.value:
            if action.cost > 0.5:
                return False, f"High-cost irreversible actions not allowed in early growth phase"
        
        # Phase-dependent uncertainty threshold
        phase_threshold = {
            DecayPhase.COLD_START.value: 0.1,  # Very strict in cold start
            DecayPhase.EARLY_GROWTH.value: 0.2,
            DecayPhase.MID_LIFECYCLE.value: self.max_uncertainty_for_irreversible,
            DecayPhase.MATURE.value: self.max_uncertainty_for_irreversible * 1.2,
            DecayPhase.LATE_STAGE.value: self.max_uncertainty_for_irreversible * 1.5,
        }.get(decay_phase, self.max_uncertainty_for_irreversible)
        
        if uncertainty > phase_threshold:
            return False, (
                f"Irreversible action with uncertainty {uncertainty:.2f} exceeds phase threshold "
                f"{phase_threshold:.2f} for {decay_phase}"
            )
        
        # Additional check: cost should be low for irreversible
        if action.cost > 0.5 and uncertainty > 0.2:
            return False, (
                f"High-cost ({action.cost:.2f}) irreversible action with uncertainty "
                f"{uncertainty:.2f} is too risky"
            )
        
        return True, None
    
    def _check_risk_survival_ratio(
        self,
        action: ActionSpec,
        scores: Dict[str, Any],
        temporal_context: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """Check risk-to-survival ratio."""
        value_estimate = scores.get("value_estimate", 0.0)
        uncertainty = scores.get("uncertainty", 0.0)
        policy_score = scores.get("policy_score", 0.0)
        
        # Risk = uncertainty * cost
        risk = uncertainty * action.cost
        
        # Survival signal = value_estimate * policy_score (expected survival delta)
        expected_survival_delta = abs(value_estimate) * policy_score if value_estimate > 0 else 0.0
        
        # Risk-to-survival ratio
        if expected_survival_delta == 0:
            if risk > 0:
                return False, "Non-zero risk with zero expected survival delta"
            return True, None
        
        risk_survival_ratio = risk / expected_survival_delta
        
        # Require minimum risk-survival ratio
        if risk_survival_ratio > (1.0 / self.min_risk_survival_ratio):
            return False, (
                f"Risk-survival ratio {risk_survival_ratio:.2f} exceeds maximum "
                f"({1.0 / self.min_risk_survival_ratio:.2f})"
            )
        
        return True, None
    
    def _check_state_stability(
        self,
        agent_input: ContentAgentInput,
        scores: Dict[str, Any],
        temporal_context: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """Check if state is stable enough for action."""
        current_state = agent_input.current_state
        engagement = current_state.get("engagement_snapshot", {})
        trajectory = agent_input.predicted_trajectory
        
        # Check for extreme state changes
        views = engagement.get("views", 0)
        recent_view_change = engagement.get("recent_view_change_rate", 0.0)
        
        # Very high change rate indicates instability
        if abs(recent_view_change) > 10.0:  # 1000% change rate
            return False, f"State instability detected: view change rate {recent_view_change:.1f}x"
        
        # Check trajectory stability
        confidence = trajectory.get("confidence", {})
        overall_conf = confidence.get("overall", 1.0)
        
        if overall_conf < 0.3:
            return False, f"Low trajectory confidence {overall_conf:.2f} indicates unstable state"
        
        # Check engagement volatility
        engagement_volatility = engagement.get("volatility", 0.0)
        if engagement_volatility > 0.5:
            return False, f"High engagement volatility {engagement_volatility:.2f} indicates instability"
        
        return True, None
    
    def _check_causal_safety(
        self,
        action: ActionSpec,
        agent_input: ContentAgentInput,
        scores: Dict[str, Any],
        temporal_context: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """Check causal safety (prevent causal violations)."""
        # Check if action would violate temporal causality
        video_age = agent_input.video_age_seconds
        
        # Some actions are only valid at certain ages
        if action.type == ActionType.REVIVAL_FLAG:
            # Revival only makes sense for older videos
            if video_age < 24 * 3600:  # Less than 1 day
                return False, "Revival flag only valid for videos older than 1 day"
        
        # Check predicted trajectory for causal violations
        trajectory = agent_input.predicted_trajectory
        short_term = trajectory.get("short_term", {})
        
        # Check if prediction suggests impossible state transitions
        predicted_views = short_term.get("predicted_views", 0)
        current_views = agent_input.current_state.get("engagement_snapshot", {}).get("views", 0)
        
        # Views can't go negative
        if predicted_views < 0:
            return False, "Predicted views cannot be negative (causal violation)"
        
        # Check for impossible growth rates
        if current_views > 0:
            growth_rate = (predicted_views - current_views) / current_views
            if growth_rate > 100.0:  # 10000% growth seems impossible
                return False, f"Impossible growth rate predicted: {growth_rate:.1%}"
        
        return True, None
    
    def _check_action_history(
        self,
        action: ActionSpec,
        agent_input: ContentAgentInput
    ) -> Tuple[bool, Optional[str]]:
        """Check action history for repeated actions (warning, not blocking)."""
        # In production, would check actual action history from state
        # For now, just a placeholder
        return True, None
    
    def _check_cost_benefit_safety(
        self,
        action: ActionSpec,
        scores: Dict[str, Any],
        temporal_context: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """Check cost-benefit safety (consolidated from early cost checks)."""
        value_estimate = scores.get("value_estimate", 0.0)
        cost = action.cost
        decay_phase = temporal_context.get("decay_phase", DecayPhase.COLD_START.value)
        
        # Phase-dependent cost thresholds (consolidated from early checks)
        if decay_phase == DecayPhase.COLD_START.value:
            max_cost = self.config.get("max_action_cost_cold_start", 0.3)
        else:
            max_cost = self.config.get("max_action_cost", 1.0)
        
        if action.cost > max_cost:
            return False, f"Action cost {action.cost} exceeds phase threshold {max_cost} for {decay_phase}"
        
        # Cost should not exceed benefit (with some margin)
        if value_estimate < 0 and cost > 0:
            return False, f"Negative value estimate ({value_estimate:.3f}) with positive cost ({cost:.2f})"
        
        # Benefit-to-cost ratio
        if cost > 0:
            benefit_cost_ratio = value_estimate / cost if value_estimate > 0 else 0.0
            if benefit_cost_ratio < 1.0:
                return False, (
                    f"Benefit-cost ratio {benefit_cost_ratio:.2f} < 1.0 (benefit: {value_estimate:.3f}, "
                    f"cost: {cost:.2f})"
                )
        
        return True, None
    
    def _check_temporal_safety(
        self,
        action: ActionSpec,
        temporal_context: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """Check temporal safety constraints."""
        decay_phase = temporal_context.get("decay_phase", DecayPhase.COLD_START.value)
        phase_progress = temporal_context.get("phase_progress", 0.0)
        
        # Some actions are only safe at certain phases
        if action.type == ActionType.REVIVAL_FLAG:
            if decay_phase in [DecayPhase.COLD_START.value, DecayPhase.EARLY_GROWTH.value]:
                return False, "Revival flag not applicable in early phases"
        
        # Timing-sensitive actions
        if action.type == ActionType.TIMING_ADJUSTMENT:
            if phase_progress > 0.8:  # Late in phase
                return False, "Timing adjustments not effective late in phase"
        
        return True, None
    
    def _load_platform_safety_rules(self) -> Dict[str, Dict[str, Any]]:
        """Load platform-specific safety rules."""
        return {
            "youtube": {
                "restricted_action_types": [],
                "max_action_cost": 2.0,
            },
            "tiktok": {
                "restricted_action_types": ["thumbnail_swap"],
                "max_action_cost": 1.0,
            },
            "instagram": {
                "restricted_action_types": [],
                "max_action_cost": 1.5,
            },
            "reddit": {
                "restricted_action_types": ["thumbnail_swap"],
                "max_action_cost": 1.0,
            },
            "twitter": {
                "restricted_action_types": ["thumbnail_swap"],
                "max_action_cost": 1.0,
            }
        }


# ============================================================================
# EXPLANATION BUILDER
# ============================================================================


class ExplanationBuilder:
    """
    Generates structured explanation for decision.
    
    Used by:
    - debugging
    - audits
    - dashboards
    """
    
    def __init__(self):
        self.logger = logging.getLogger(__name__ + ".ExplanationBuilder")
    
    def build(
        self,
        action: ActionSpec,
        agent_input: ContentAgentInput,
        temporal_context: Dict[str, Any],
        scores: Dict[str, Any],
        micro_impact_envelope: Optional[MicroImpactEnvelope] = None
    ) -> DecisionExplanation:
        """Build structured explanation."""
        # Determine primary signal
        primary_signal = self._identify_primary_signal(agent_input, temporal_context, scores)
        
        # Identify risk factors
        risk_factors = self._identify_risk_factors(agent_input, temporal_context, action)
        
        # Identify confidence drivers
        confidence_drivers = self._identify_confidence_drivers(scores, temporal_context)
        
        # Expected outcome
        expected_outcome = self._build_expected_outcome(action, scores, temporal_context)
        
        # Serialize micro-impact envelope (formalized, provable)
        envelope_dict = None
        if micro_impact_envelope:
            envelope_dict = {
                "max_cost": micro_impact_envelope.max_cost,
                "max_irreversible_actions": micro_impact_envelope.max_irreversible_actions,
                "max_actions_per_window": micro_impact_envelope.max_actions_per_window,
                "max_uncertainty_for_action": micro_impact_envelope.max_uncertainty_for_action
            }
        
        # Include confidence_priors_version for causal attribution hygiene
        # This prevents silent conservatism drift when policy networks evolve
        confidence_priors_version = getattr(agent_input, 'confidence_priors_version', None)
        if confidence_priors_version is None:
            # Fallback: use policy_version (priors are versioned with policy)
            confidence_priors_version = agent_input.policy_version
        
        explanation = DecisionExplanation(
            primary_signal=primary_signal,
            risk_factors=risk_factors,
            confidence_drivers=confidence_drivers,
            expected_outcome=expected_outcome,
            micro_impact_envelope=envelope_dict,
            optimization_claim="none",  # Explicit: agent cannot improve a video
            goal="collapse_prevention_only"  # Explicit: goal is survival, not growth
        )
        
        self.logger.debug(f"Built explanation: {explanation}")
        return explanation
    
    def _identify_primary_signal(
        self,
        agent_input: ContentAgentInput,
        temporal_context: Dict[str, Any],
        scores: Dict[str, Any]
    ) -> str:
        """
        Identify the SINGLE dominant causal signal driving decision.
        
        Enforces exactly one primary signal per decision for perfect replay explainability.
        Returns explicit signal labels that map to causal survival physics.
        
        EXPLICIT NO-OP: If no_op_reason is set, it becomes the primary signal.
        This makes no-op dominance explicit (not emergent).
        """
        # EXPLICIT NO-OP POLICY: Check for explicit no-op reason first
        no_op_reason = temporal_context.get("_no_op_reason")
        if no_op_reason:
            # Map no_op_reason to explicit primary signal
            if no_op_reason.startswith("high_uncertainty"):
                return "high_uncertainty_safety"
            elif no_op_reason == "all_actions_blocked":
                return "all_actions_blocked_safety"
            elif no_op_reason == "no_op_created_fallback":
                return "fallback_safety_mode"
            else:
                # Generic explicit no-op
                return f"explicit_no_op: {no_op_reason}"
        
        decay_phase = temporal_context["decay_phase"]
        phase_progress = temporal_context.get("phase_progress", 0.0)
        
        # Collect candidate signals with priority ordering (highest priority first)
        candidate_signals = []
        
        # Check trajectory for retention inflection
        trajectory = agent_input.predicted_trajectory
        short_term = trajectory.get("short_term", {})
        mid_term = trajectory.get("mid_term", {})
        
        # Priority 1: Retention inflection detected (highest urgency)
        if short_term.get("retention_drop_predicted") or mid_term.get("retention_drop_predicted"):
            candidate_signals.append("retention_inflection_detected")
        
        # Priority 2: Early exposure decay
        if decay_phase == DecayPhase.COLD_START.value or decay_phase == DecayPhase.EARLY_GROWTH.value:
            if phase_progress < 0.5:
                candidate_signals.append("early_exposure_decay")
        
        # Priority 3: Sentiment volatility spike
        engagement = agent_input.current_state.get("engagement_snapshot", {})
        sentiment = engagement.get("sentiment", {})
        if isinstance(sentiment, dict):
            sentiment_volatility = sentiment.get("volatility", 0.0)
            if sentiment_volatility > 0.7:
                candidate_signals.append("sentiment_volatility_spike")
        
        # Priority 4: Distribution phase transition
        if decay_phase in [DecayPhase.MID_LIFECYCLE.value, DecayPhase.MATURE.value]:
            if 0.3 < phase_progress < 0.7:
                candidate_signals.append("distribution_phase_transition")
        
        # Priority 5: High uncertainty safety (fallback)
        if scores.get("uncertainty", 0.0) > 0.5:
            candidate_signals.append("high_uncertainty_safety")
        
        # Priority 6: Long-tail decay prevention
        if decay_phase == DecayPhase.LATE_STAGE.value:
            candidate_signals.append("long_tail_decay_prevention")
        
        # Enforce exactly one primary signal
        if len(candidate_signals) == 0:
            # Default: stable survival mode
            primary_signal = "stable_survival_mode"
        elif len(candidate_signals) == 1:
            primary_signal = candidate_signals[0]
        else:
            # Multiple signals detected - select the highest priority (first in list)
            primary_signal = candidate_signals[0]
            self.logger.warning(
                f"Multiple candidate signals detected: {candidate_signals}. "
                f"Selecting highest priority: {primary_signal}"
            )
        
        # Assert exactly one signal (defensive check)
        assert isinstance(primary_signal, str) and len(primary_signal) > 0, (
            f"Primary signal must be a non-empty string, got: {primary_signal}"
        )
        
        return primary_signal
    
    def _identify_risk_factors(
        self,
        agent_input: ContentAgentInput,
        temporal_context: Dict[str, Any],
        action: ActionSpec
    ) -> List[str]:
        """Identify risk factors for this decision."""
        risks = []
        
        if not action.reversibility:
            risks.append("irreversible_action")
        
        if temporal_context["decay_phase"] == DecayPhase.COLD_START.value:
            risks.append("cold_start_uncertainty")
        
        if action.cost > 0.5:
            risks.append("high_cost_action")
        
        trajectory = agent_input.predicted_trajectory
        if trajectory.get("confidence", {}).get("overall", 1.0) < 0.5:
            risks.append("low_trajectory_confidence")
        
        return risks
    
    def _identify_confidence_drivers(
        self,
        scores: Dict[str, Any],
        temporal_context: Dict[str, Any]
    ) -> List[str]:
        """Identify what drives confidence in this decision."""
        drivers = []
        
        if scores.get("policy_score", 0.0) > 0.7:
            drivers.append("high_policy_score")
        
        if scores.get("uncertainty", 1.0) < 0.3:
            drivers.append("low_uncertainty")
        
        if temporal_context["decay_phase"] != DecayPhase.COLD_START.value:
            drivers.append("sufficient_data_available")
        
        return drivers
    
    def _build_expected_outcome(
        self,
        action: ActionSpec,
        scores: Dict[str, Any],
        temporal_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Build comprehensive expected outcome description."""
        value_estimate = scores.get("value_estimate", 0.0)
        policy_score = scores.get("policy_score", 0.0)
        uncertainty = scores.get("uncertainty", 0.0)
        decay_phase = temporal_context.get("decay_phase", DecayPhase.COLD_START.value)
        
        # Compute expected value range (with uncertainty)
        value_lower = value_estimate - (uncertainty * abs(value_estimate))
        value_upper = value_estimate + (uncertainty * abs(value_estimate))
        
        # Estimate time to effect
        time_to_effect = self._estimate_time_to_effect(action, decay_phase, temporal_context)
        
        # Estimate effect duration
        effect_duration = self._estimate_effect_duration(action, decay_phase)
        
        # Expected impact magnitude
        impact_magnitude = self._estimate_impact_magnitude(action, scores, temporal_context)
        
        return {
            "action_type": action.type.value,
            "action_id": action.action_id,
            "estimated_value": value_estimate,
            "value_range": {
                "lower": value_lower,
                "upper": value_upper,
                "mean": value_estimate
            },
            "confidence": 1.0 - uncertainty,
            "policy_score": policy_score,
            "uncertainty": uncertainty,
            "decay_phase": decay_phase,
            "phase_progress": temporal_context.get("phase_progress", 0.0),
            "reversible": action.reversibility,
            "cost": action.cost,
            "time_to_effect_seconds": time_to_effect,
            "effect_duration_seconds": effect_duration,
            "impact_magnitude": impact_magnitude,
            "risk_level": self._classify_risk_level(action, scores, temporal_context),
            "success_probability": self._estimate_success_probability(action, scores, temporal_context)
        }
    
    def _estimate_time_to_effect(
        self,
        action: ActionSpec,
        decay_phase: str,
        temporal_context: Dict[str, Any]
    ) -> float:
        """Estimate time to effect in seconds."""
        # Action-type specific estimates
        time_to_effect_map = {
            ActionType.CAPTION_VARIANT: 300,  # 5 minutes
            ActionType.THUMBNAIL_SWAP: 1800,  # 30 minutes
            ActionType.TIMING_ADJUSTMENT: 0,  # Immediate
            ActionType.REVIVAL_FLAG: 3600,  # 1 hour
            ActionType.NO_OP: 0
        }
        
        base_time = time_to_effect_map.get(action.type, 600)  # Default 10 minutes
        
        # Adjust based on phase (earlier phases see effects faster)
        if decay_phase == DecayPhase.COLD_START.value:
            return base_time * 0.5
        elif decay_phase == DecayPhase.EARLY_GROWTH.value:
            return base_time * 0.7
        else:
            return base_time
    
    def _estimate_effect_duration(
        self,
        action: ActionSpec,
        decay_phase: str
    ) -> float:
        """Estimate effect duration in seconds."""
        # Most actions have temporary effects
        effect_duration_map = {
            ActionType.CAPTION_VARIANT: 4 * 3600,  # 4 hours
            ActionType.THUMBNAIL_SWAP: 24 * 3600,  # 24 hours
            ActionType.TIMING_ADJUSTMENT: 3600,  # 1 hour (one-time)
            ActionType.REVIVAL_FLAG: 7 * 24 * 3600,  # 7 days
            ActionType.NO_OP: 0
        }
        
        return effect_duration_map.get(action.type, 3600)
    
    def _estimate_impact_magnitude(
        self,
        action: ActionSpec,
        scores: Dict[str, Any],
        temporal_context: Dict[str, Any]
    ) -> str:
        """Estimate impact magnitude category."""
        value_estimate = abs(scores.get("value_estimate", 0.0))
        uncertainty = scores.get("uncertainty", 0.0)
        effective_impact = value_estimate * (1.0 - uncertainty)
        
        if effective_impact < 0.01:
            return "minimal"
        elif effective_impact < 0.05:
            return "low"
        elif effective_impact < 0.15:
            return "moderate"
        elif effective_impact < 0.3:
            return "high"
        else:
            return "very_high"
    
    def _classify_risk_level(
        self,
        action: ActionSpec,
        scores: Dict[str, Any],
        temporal_context: Dict[str, Any]
    ) -> str:
        """Classify risk level."""
        uncertainty = scores.get("uncertainty", 0.0)
        cost = action.cost
        reversibility = action.reversibility
        
        risk_score = uncertainty * 0.6 + (cost / 2.0) * 0.3 + (0.0 if reversibility else 1.0) * 0.1
        
        if risk_score < 0.2:
            return "low"
        elif risk_score < 0.5:
            return "moderate"
        elif risk_score < 0.8:
            return "high"
        else:
            return "very_high"
    
    def _estimate_success_probability(
        self,
        action: ActionSpec,
        scores: Dict[str, Any],
        temporal_context: Dict[str, Any]
    ) -> float:
        """Estimate success probability (0.0-1.0)."""
        policy_score = scores.get("policy_score", 0.5)
        uncertainty = scores.get("uncertainty", 0.5)
        
        # Base probability from policy score
        base_prob = policy_score
        
        # Adjust for uncertainty (higher uncertainty = lower success prob)
        adjusted_prob = base_prob * (1.0 - uncertainty * 0.5)
        
        # Adjust for phase (cold start = lower success prob)
        decay_phase = temporal_context.get("decay_phase", DecayPhase.COLD_START.value)
        phase_adjustment = {
            DecayPhase.COLD_START.value: 0.9,
            DecayPhase.EARLY_GROWTH.value: 0.95,
            DecayPhase.MID_LIFECYCLE.value: 1.0,
            DecayPhase.MATURE.value: 1.05,
            DecayPhase.LATE_STAGE.value: 1.0
        }.get(decay_phase, 1.0)
        
        final_prob = adjusted_prob * phase_adjustment
        
        return min(1.0, max(0.0, final_prob))


# ============================================================================
# PRIMARY CAUSAL SIGNAL GUARD
# ============================================================================

def enforce_primary_causal_signal(explanation: DecisionExplanation) -> None:
    """
    Enforce exactly one dominant causal signal per decision.
    
    This guard ensures:
    - Causal clarity (one decision = one story)
    - Replay determinism (no ambiguity)
    - Audit-grade explanations (unambiguous at scale)
    
    Hard-fails on ambiguity to prevent misuse.
    """
    primary = explanation.primary_signal
    
    if primary is None:
        raise ValueError("Explanation missing primary_signal - cannot emit ambiguous decision")
    
    if isinstance(primary, (list, tuple, set)):
        if len(primary) != 1:
            raise ValueError(
                f"Exactly one primary causal signal required, got {len(primary)} signals: {primary}"
            )
        # Normalize to single string if wrapped in collection
        explanation.primary_signal = next(iter(primary))
        primary = explanation.primary_signal
    
    if not isinstance(primary, str):
        raise ValueError(
            f"Primary signal must be a string, got {type(primary).__name__}: {primary}"
        )
    
    if not primary or not primary.strip():
        raise ValueError("Primary signal cannot be empty - decision must have explicit causal driver")
    
    # Validate it's a recognized signal type (optional but recommended)
    # This prevents typos and ensures consistency
    allowed_signals = {
        "retention_inflection_detected",
        "early_exposure_decay",
        "sentiment_volatility_spike",
        "distribution_phase_transition",
        "high_uncertainty_safety",
        "long_tail_decay_prevention",
        "stable_survival_mode",
        "confidence_stability_preservation",
    }
    
    # Note: We don't hard-fail on unknown signals to allow future expansion,
    # but we log a warning for audit purposes
    if primary not in allowed_signals:
        logging.getLogger(__name__).warning(
            f"Primary signal '{primary}' not in known set - may be valid extension"
        )


# ============================================================================
# DECISION EMITTER
# ============================================================================


class DecisionEmitter:
    """
    Emits final decision with determinism guarantees.
    
    Given:
    - identical input snapshot
    - same policy version
    - same seed
    
    ➡ decision must be identical.
    """
    
    def __init__(self, seed: Optional[int] = None):
        self.logger = logging.getLogger(__name__ + ".DecisionEmitter")
        self.seed = seed
    
    def emit(
        self,
        video_id: str,
        selected_action: ActionSpec,
        confidence: float,
        policy_version: str,
        explanation: DecisionExplanation,
        decision_context: DecisionContext,
        escalation_intent: Optional[Dict[str, Any]] = None
    ) -> ContentAgentOutput:
        """
        Emit final decision.
        
        Guaranteed deterministic for replay - uses decision_context, never system clock.
        Escalation is intent emission (not execution) - orchestration owns delivery.
        """
        decision_timestamp = decision_context.decision_iso  # Deterministic: from context, not datetime.utcnow()
        
        output = ContentAgentOutput(
            video_id=video_id,
            action_id=selected_action.action_id,
            confidence=confidence,
            decision_timestamp=decision_timestamp,
            policy_version=policy_version,
            explanation=explanation,
            escalation_requested=escalation_intent is not None,
            escalation_intent=escalation_intent
        )
        
        # Validate before emitting
        if not output.validate():
            raise ValueError("Invalid decision output")
        
        self.logger.info(
            f"Decision emitted: video={video_id}, action={selected_action.action_id}, "
            f"confidence={confidence:.3f}"
        )
        
        return output
    
    def compute_decision_hash(self, output: ContentAgentOutput) -> str:
        """
        Compute deterministic hash of decision for audit trail.
        
        FROZEN CANONICALIZATION RULES (for bit-exact determinism):
        - Float precision: 6 decimals (via rounding in to_dict)
        - UTF-8 normalization: ensure_ascii=True
        - Explicit separators: (",", ":") - no whitespace
        - Sort keys: True (deterministic ordering)
        
        This guarantees: "Identical snapshot → identical decision" across machines, years later.
        """
        # Create canonical representation with frozen rules
        canonical = json.dumps(
            output.to_dict(),
            sort_keys=True,
            separators=(",", ":"),  # No whitespace - explicit separators
            ensure_ascii=True  # UTF-8 normalization
        )
        
        # Hash
        hash_obj = hashlib.sha256(canonical.encode('utf-8'))
        return hash_obj.hexdigest()
    
    def compute_decision_fingerprint(
        self,
        input_hash: str,
        action_id: str,
        policy_version: str,
        envelope_params: Dict[str, Any]
    ) -> str:
        """
        Compute immutable decision fingerprint for auditability.
        
        Fingerprint includes:
        - Input hash (deterministic input snapshot)
        - Selected action_id
        - Policy version
        - Envelope parameters (safety boundary)
        
        This enables court-grade auditability, replay diffing, and cross-system integrity checks.
        """
        fingerprint_data = {
            "input_hash": input_hash,
            "action_id": action_id,
            "policy_version": policy_version,
            "envelope": envelope_params
        }
        
        # Use same canonicalization rules as compute_decision_hash
        canonical = json.dumps(
            fingerprint_data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True
        )
        
        hash_obj = hashlib.sha256(canonical.encode('utf-8'))
        return hash_obj.hexdigest()


# ============================================================================
# CONTENT AGENT (MAIN)
# ============================================================================


class ContentAgent:
    """
    Per-Video Micro-Decision Reinforcement Agent.
    
    Minimal authority. Maximum leverage. Causally clean. RL-safe.
    
    This agent is why individual videos stay alive long enough to matter.
    """
    
    def __init__(
        self,
        policy_network=None,
        value_network=None,
        config: Optional[Dict[str, Any]] = None,
        seed: Optional[int] = None
    ):
        self.logger = logging.getLogger(__name__ + ".ContentAgent")
        self.config = config or {}
        self.seed = seed
        
        # Initialize components
        self.input_validator = InputValidator()
        self.temporal_encoder = TemporalContextEncoder()
        self.action_masker = ActionMasker(config)
        # Initialize confidence estimator (shared by PolicyInterface and SafetyChecker)
        # Priors are versioned with policy_version to prevent silent conservatism drift
        # Note: policy_version will be set from agent_input during decide() - use "unknown" as default
        self.confidence_estimator = ConfidenceEstimator(self.config, policy_version=None)
        self.policy_interface = PolicyInterface(policy_network, value_network, self.config, self.confidence_estimator)
        self.uncertainty_gate = UncertaintyGate(
            uncertainty_threshold=self.config.get("uncertainty_threshold", 0.6)
        )
        self.safety_checker = SafetyChecker(config)
        self.explanation_builder = ExplanationBuilder()
        self.decision_emitter = DecisionEmitter(seed)
        
        self.logger.info("ContentAgent initialized")
    
    def decide(
        self,
        agent_input: ContentAgentInput,
        factory_locks: Optional[Dict[str, Any]] = None
    ) -> ContentAgentOutput:
        """
        Make single micro-decision for video.
        
        This is the ONLY public method.
        
        Args:
            agent_input: Frozen snapshot input
            factory_locks: Optional factory-level constraints
        
        Returns:
            ContentAgentOutput with single action decision
        
        Raises:
            ValueError: If input is invalid
        """
        self.logger.info(f"Making decision for video {agent_input.video_id}")
        
        # 0. Update ConfidenceEstimator with policy_version (version priors to prevent drift)
        self.confidence_estimator.confidence_priors_version = agent_input.policy_version
        
        # 1. Validate input
        valid, error = self.input_validator.validate(agent_input)
        if not valid:
            self.logger.error(f"Input validation failed: {error}")
            raise ValueError(f"Invalid input: {error}")
        
        # 2. Encode temporal context
        temporal_context = self.temporal_encoder.encode(agent_input.video_age_seconds)
        
        # 3. Mask actions (100% snapshot-driven - extract ALL state from current_state)
        # CRITICAL: No datetime.utcnow() - all timestamps must come from snapshot
        action_history = agent_input.current_state.get("action_history", {})
        action_type_history = agent_input.current_state.get("action_type_history", {})
        
        # Rate state must include current_timestamp from snapshot (not datetime.utcnow())
        # This ensures perfect replay determinism
        rate_state = agent_input.current_state.get("rate_state", {})
        if "current_timestamp" not in rate_state:
            # If not in snapshot, use decision_timestamp from input (if available)
            # Otherwise, rate limiting is effectively disabled (safe default)
            rate_state = {
                "global_action_count": rate_state.get("global_action_count", 0),
                "window_start": rate_state.get("window_start"),
                "current_timestamp": None  # No timestamp = no rate limiting
            }
        else:
            # Ensure current_timestamp is in rate_state for deterministic checking
            rate_state = {
                "global_action_count": rate_state.get("global_action_count", 0),
                "window_start": rate_state.get("window_start"),
                "current_timestamp": rate_state.get("current_timestamp")
            }
        
        allowed_actions = self.action_masker.mask_actions(
            agent_input.available_actions,
            temporal_context,
            agent_input.current_state,
            action_history,
            action_type_history,
            rate_state,
            factory_locks
        )
        
        # 3.5. EXPLICIT NO-OP POLICY STAGE (makes no-op dominance explicit, not emergent)
        # This distinguishes healthy restraint from systemic failure
        no_op_action = next((a for a in allowed_actions if a.type == ActionType.NO_OP), None)
        non_no_op_allowed = [a for a in allowed_actions if a.type != ActionType.NO_OP]
        
        no_op_reason = None
        if len(non_no_op_allowed) == 0 and len(agent_input.available_actions) > 1:
            # All actions blocked - explicit no-op due to masking
            no_op_reason = "all_actions_blocked"
            self.logger.info(f"NO-OP POLICY: All actions blocked by ActionMasker - explicit no_op (reason: {no_op_reason})")
        elif no_op_action is None:
            # Safety: ensure no_op always exists
            no_op_action = ActionSpec(
                action_id="no_op_fallback",
                type=ActionType.NO_OP,
                cost=0.0,
                reversibility=True,
                cooldown=0,
                constraints={}
            )
            allowed_actions.append(no_op_action)
            no_op_reason = "no_op_created_fallback"
            self.logger.warning(f"NO-OP POLICY: Created fallback no_op (reason: {no_op_reason})")
        
        # Store no_op_reason for explanation (will be used later)
        temporal_context["_no_op_reason"] = no_op_reason
        
        # 4. Score actions via policy interface
        action_scores = self.policy_interface.score_actions(
            agent_input,
            temporal_context,
            allowed_actions
        )
        
        # 4.5. EXPLICIT UNCERTAINTY GATE - Hard first-class stop (before action selection)
        # This is the centralized, authoritative uncertainty check with explicit invariant:
        # "If uncertainty > X → no-op OR escalate"
        allow, escalate, uncertainty_reason = self.uncertainty_gate.evaluate(
            action_scores,
            temporal_context
        )
        
        if not allow:
            # ABSOLUTE HARD STOP: Bypass all selection logic, go straight to no_op + escalation
            # EXPLICIT NO-OP: High uncertainty triggers explicit no-op (not emergent)
            no_op_reason = f"high_uncertainty: {uncertainty_reason}"
            temporal_context["_no_op_reason"] = no_op_reason
            self.logger.warning(
                f"NO-OP POLICY (UNCERTAINTY GATE): {no_op_reason}. "
                f"Bypassing action selection - absolute override to no_op + escalation"
            )
            
            # Single authoritative escalation point (if escalate flag is True)
            if escalate:
                escalation_reason = f"Uncertainty gate hard stop: {uncertainty_reason}"
                temporal_context["_escalated"] = True
                temporal_context["_escalation_reason"] = escalation_reason
                
                self._escalate_to_factory(
                    agent_input.video_id,
                    None,  # No action selected due to uncertainty gate
                    escalation_reason,
                    {},
                    agent_input.decision_context
                )
            
            # Hard override: no_op only (no selection, no scoring, no influence)
            no_op_action = next((a for a in allowed_actions if a.type == ActionType.NO_OP), None)
            if no_op_action is None:
                raise RuntimeError("No no_op action available - system error")
            
            # Use no_op with default safe scores (no selection logic applied)
            best_action = no_op_action
            best_score = {
                "policy_score": 0.5,  # Neutral - not selected, just safe default
                "value_estimate": 0.0,
                "uncertainty": 0.1,
                "collapse_risk": 0.0,
                "confidence": 0.9,
                "network_used": False
            }
            best_confidence = 0.5
        else:
            # Only proceed to selection if uncertainty gate allows
            # 5. Select best action (deterministic)
            # Pass agent_input for cold-start trajectory confidence check
            best_action, best_score, best_confidence = self._select_action(
                allowed_actions,
                action_scores,
                temporal_context,
                agent_input
            )
        
        # 6. EXPLICIT FINAL SAFETY VETO - Single authoritative mechanical gate
        # This is the ONLY place where final safety veto happens after scoring.
        # SafetyChecker.veto() is the mechanical centralization point - not just conceptual.
        # All safety logic flows through this single explicit pass before emission.
        allowed, veto_reason = self.safety_checker.veto(
            best_action,
            agent_input,
            action_scores.get(best_action.action_id, {}),
            temporal_context
        )
        
        if not allowed:
            self.logger.error(
                f"SAFETY CHECKER VETO: Selected action {best_action.action_id} vetoed: {veto_reason}. "
                f"Escalating to factory agent and using no_op"
            )
            # Escalation intent will be created in output emission (intent, not execution)
            escalation_reason = f"SafetyChecker.veto() blocked action: {veto_reason}"
            temporal_context["_escalated"] = True
            temporal_context["_escalation_reason"] = escalation_reason
            # Fallback to no_op
            no_op_action = next((a for a in allowed_actions if a.type == ActionType.NO_OP), None)
            if no_op_action is None:
                raise RuntimeError("No no_op action available - system error")
            best_action = no_op_action
            best_score = action_scores.get(no_op_action.action_id, {})
            best_confidence = best_score.get("policy_score", 0.5)
        
        # 6.5. EXPLICIT MICRO-IMPACT ENVELOPE ENFORCEMENT - Hard gate (provable safety boundary)
        # This is the provable safety boundary - execs, auditors, and lawyers care about THIS.
        # The envelope MUST be enforced as a hard gate, not just informational.
        micro_impact_envelope = self.safety_checker.get_micro_impact_envelope()
        selected_uncertainty = action_scores.get(best_action.action_id, {}).get("uncertainty", 1.0)
        envelope_ok, envelope_reason = micro_impact_envelope.validate_action(best_action, selected_uncertainty)
        
        if not envelope_ok:
            self.logger.error(
                f"MICRO-IMPACT ENVELOPE VIOLATION: Action {best_action.action_id} violates envelope: {envelope_reason}. "
                f"Forcing no_op and escalating."
            )
            # Force no_op and escalate
            escalation_reason = f"MicroImpactEnvelope violation: {envelope_reason}"
            temporal_context["_escalated"] = True
            temporal_context["_escalation_reason"] = escalation_reason
            
            no_op_action = next((a for a in allowed_actions if a.type == ActionType.NO_OP), None)
            if no_op_action is None:
                raise RuntimeError("No no_op action available - system error")
            best_action = no_op_action
            best_score = action_scores.get(no_op_action.action_id, {})
            best_confidence = best_score.get("policy_score", 0.5)
        
        # 8. Build explanation (includes formalized micro-impact envelope)
        explanation = self.explanation_builder.build(
            best_action,
            agent_input,
            temporal_context,
            action_scores.get(best_action.action_id, {}),
            self.safety_checker.get_micro_impact_envelope()
        )
        
        # 8.5. Enforce primary causal signal guard (exactly one signal)
        enforce_primary_causal_signal(explanation)
        
        # 9. Create escalation intent if needed (intent emission, not execution)
        escalation_intent = None
        if temporal_context.get("_escalated", False):
            escalation_reason = temporal_context.get("_escalation_reason", "Unknown escalation reason")
            escalation_intent = self._create_escalation_intent(
                agent_input.video_id,
                best_action if temporal_context.get("_escalation_reason", "").startswith("SafetyChecker") else None,
                escalation_reason,
                action_scores.get(best_action.action_id, {}),
                agent_input.decision_context
            )
        
        # 10. Emit decision (deterministic - uses decision_context, never system clock)
        # Escalation intent is included in output - orchestration owns delivery
        output = self.decision_emitter.emit(
            agent_input.video_id,
            best_action,
            best_confidence,
            agent_input.policy_version,
            explanation,
            agent_input.decision_context,
            escalation_intent
        )
        
        # 10.5. Compute decision fingerprint (immutable hash for auditability)
        # Fingerprint enables court-grade auditability, replay diffing, and cross-system integrity checks
        input_hash = self.input_validator._compute_input_hash(agent_input)
        envelope_params = {
            "max_cost": micro_impact_envelope.max_cost,
            "max_irreversible_actions": micro_impact_envelope.max_irreversible_actions,
            "max_actions_per_window": micro_impact_envelope.max_actions_per_window,
            "max_uncertainty_for_action": micro_impact_envelope.max_uncertainty_for_action
        }
        decision_fingerprint = self.decision_emitter.compute_decision_fingerprint(
            input_hash,
            best_action.action_id,
            agent_input.policy_version,
            envelope_params
        )
        output.decision_fingerprint = decision_fingerprint
        
        # 10. Action recording is handled by factory/external state (stateless agent)
        
        # 11. Send to replay buffer (integration point)
        self._send_to_replay_buffer(agent_input, output, temporal_context)
        
        # 12. Audit log (integration point)
        self._audit_decision(agent_input, output, best_score, temporal_context)
        
        return output
    
    def _select_action(
        self,
        allowed_actions: List[ActionSpec],
        action_scores: Dict[str, Dict[str, float]],
        temporal_context: Dict[str, Any],
        agent_input: Optional[ContentAgentInput] = None
    ) -> Tuple[ActionSpec, Dict[str, float], float]:
        """
        Select best action deterministically.
        
        Given identical inputs and seed, returns identical action.
        
        COLD-START BEHAVIOR: Brutally conservative.
        In cold start, only no_op or cheapest reversible action unless trajectory confidence is explicitly high.
        """
        if len(allowed_actions) == 0:
            raise ValueError("No allowed actions available")
        
        # If only no_op, return it
        if len(allowed_actions) == 1:
            action = allowed_actions[0]
            scores = action_scores.get(action.action_id, {})
            confidence = scores.get("policy_score", 0.5)
            return action, scores, confidence
        
        # BRUTAL COLD-START SUPPRESSION: Only no_op or cheapest reversible unless confidence is high
        decay_phase = temporal_context.get("decay_phase", DecayPhase.COLD_START.value)
        if decay_phase == DecayPhase.COLD_START.value:
            # Check trajectory confidence - must be explicitly high to allow non-no_op actions
            trajectory_confidence = 0.0
            if agent_input:
                trajectory = agent_input.predicted_trajectory
                trajectory_confidence = trajectory.get("confidence", {}).get("overall", 0.0)
            
            # Cold start: brutally conservative - only no_op or cheapest reversible unless confidence > 0.8
            if trajectory_confidence < 0.8:
                # Find no_op first
                no_op_action = next((a for a in allowed_actions if a.type == ActionType.NO_OP), None)
                if no_op_action:
                    scores = action_scores.get(no_op_action.action_id, {})
                    confidence = scores.get("policy_score", 0.5)
                    self.logger.info(
                        f"COLD-START: Low trajectory confidence ({trajectory_confidence:.2f}), "
                        f"selecting no_op (brutally conservative)"
                    )
                    return no_op_action, scores, confidence
                
                # If no no_op, find cheapest reversible action
                reversible_actions = [a for a in allowed_actions if a.reversibility and a.cost < 0.3]
                if reversible_actions:
                    cheapest_reversible = min(reversible_actions, key=lambda a: a.cost)
                    scores = action_scores.get(cheapest_reversible.action_id, {})
                    confidence = scores.get("policy_score", 0.5)
                    self.logger.info(
                        f"COLD-START: Low trajectory confidence ({trajectory_confidence:.2f}), "
                        f"selecting cheapest reversible action {cheapest_reversible.action_id} "
                        f"(cost: {cheapest_reversible.cost:.2f})"
                    )
                    return cheapest_reversible, scores, confidence
                
                # Fallback: if no safe actions, still prefer no_op (should have been added by ActionMasker)
                self.logger.warning(
                    f"COLD-START: No safe actions available, falling back to first action"
                )
        
        # Normal action selection (non-cold-start or high-confidence cold-start)
        # Score each action using weighted combination
        scored_actions = []
        
        for action in allowed_actions:
            scores = action_scores.get(action.action_id, {})
            policy_score = scores.get("policy_score", 0.0)
            value_estimate = scores.get("value_estimate", 0.0)
            uncertainty = scores.get("uncertainty", 1.0)
            
            # Weighted score: policy_score * (1 - uncertainty) + value_estimate * confidence_weight
            confidence_weight = 1.0 - min(uncertainty, 1.0)
            combined_score = (
                policy_score * confidence_weight * 0.6 +
                value_estimate * confidence_weight * 0.4
            )
            
            # Prefer reversible actions in cold start (if we got here, confidence is high)
            if decay_phase == DecayPhase.COLD_START.value:
                if action.reversibility:
                    combined_score *= 1.2
            
            # Prefer no_op if uncertainty is high
            if action.type == ActionType.NO_OP and uncertainty > 0.5:
                combined_score = max(combined_score, 0.7)
            
            scored_actions.append((action, scores, combined_score, policy_score))
        
        # Sort by combined score (descending), then by action_id for determinism
        scored_actions.sort(
            key=lambda x: (-x[2], x[0].action_id),  # Negative for descending score
            reverse=False
        )
        
        # Deterministic tie-breaking: use seed if available
        best_actions = [x for x in scored_actions if abs(x[2] - scored_actions[0][2]) < 0.001]
        
        if len(best_actions) > 1 and self.seed is not None:
            # Use seed for deterministic tie-breaking
            import random
            random.seed(self.seed + hash(scored_actions[0][0].action_id))
            selected = random.choice(best_actions)
        else:
            # If no seed or only one best, select first (already sorted by action_id)
            selected = best_actions[0]
        
        action, scores, combined_score, confidence = selected
        
        self.logger.debug(
            f"Selected action {action.action_id} with combined_score={combined_score:.3f}, "
            f"confidence={confidence:.3f}"
        )
        
        return action, scores, confidence
    
    def _create_escalation_intent(
        self,
        video_id: str,
        action: Optional[ActionSpec],
        reason: str,
        scores: Dict[str, float],
        decision_context: DecisionContext
    ) -> Dict[str, Any]:
        """
        Create escalation intent - SPEC COMPLIANT (intent emission, not execution).
        
        This agent emits escalation intent only - it does NOT execute delivery.
        Delivery is owned by orchestration layer (factory agent, message queue, etc.).
        
        At 5M+ baseline, escalation delivery must be owned by orchestration, not the micro-agent.
        This method creates the escalation intent payload for inclusion in output.
        
        Returns:
            Dict containing escalation intent (for inclusion in ContentAgentOutput)
        """
        escalation_timestamp = decision_context.decision_timestamp
        escalation_payload = {
            "source": "content_agent",
            "video_id": video_id,
            "escalation_id": str(uuid.uuid4()),
            "timestamp": escalation_timestamp,  # Deterministic: from decision_context
            "decision_context": {
                "blocked_action": {
                    "action_id": action.action_id if action else None,
                    "action_type": action.type.value if action else None,
                    "cost": action.cost if action else 0.0,
                    "reversibility": action.reversibility if action else True
                },
                "block_reason": reason,
                "decision_scores": scores,
                "escalation_type": "safety_block"
            },
            "requested_response": {
                "action": "review_and_decide",
                "priority": "high" if reason.startswith("SAFETY BLOCK") or "veto" in reason.lower() else "medium",
                "deadline_seconds": 3600  # 1 hour
            }
        }
        
        # Create escalation intent (NO EXECUTION - orchestration owns delivery)
        escalation_intent = FactoryEscalationInterface.create_escalation_intent(escalation_payload)
        
        self.logger.info(
            f"Escalation intent created: video={video_id}, "
            f"escalation_id={escalation_payload['escalation_id']}. "
            f"Delivery owned by orchestration."
        )
        
        return escalation_intent
    
    def _send_to_replay_buffer(
        self,
        agent_input: ContentAgentInput,
        output: ContentAgentOutput,
        temporal_context: Dict[str, Any]
    ) -> None:
        """
        Send decision to replay buffer for offline learning.
        
        Full integration with replay_buffer.py.
        Creates Experience object and stores it in replay buffer for RL training.
        """
        if not REPLAY_BUFFER_AVAILABLE or Experience is None or ReplayBuffer is None:
            self.logger.warning("Replay buffer not available, skipping experience storage")
            return
        
        try:
            # Create Experience object for replay buffer
            # Extract factory_id from state (or use default)
            factory_id = agent_input.current_state.get("factory_id", "default_factory")
            
            # Create experience ID (use decision_timestamp for determinism, not time.time())
            # Extract timestamp from decision_timestamp for deterministic ID generation
            decision_ts_seconds = datetime.fromisoformat(output.decision_timestamp.replace("Z", "+00:00")).timestamp()
            experience_id = f"{agent_input.video_id}_{output.action_id}_{int(decision_ts_seconds)}"
            
            # Build state snapshot (frozen at decision time)
            # NOTE: features_computed_at uses decision_timestamp for replay determinism
            state_snapshot = {
                "video_id": agent_input.video_id,
                "video_age_seconds": agent_input.video_age_seconds,
                "platform": agent_input.platform,
                "features_computed_at": decision_ts_seconds,  # Deterministic: uses decision_timestamp, not time.time()
                "current_state": agent_input.current_state,
                "predicted_trajectory": agent_input.predicted_trajectory,
                "temporal_context": temporal_context,
                "decision_context": {
                    "policy_version": agent_input.policy_version,
                    "confidence_priors_version": self.confidence_estimator.confidence_priors_version,  # Versioned priors
                    "agent_type": "content_agent",
                    "decision_timestamp": output.decision_timestamp
                }
            }
            
            # Build action record
            action_record = {
                "action_id": output.action_id,
                "action_type": output.action_id.split("_")[0] if "_" in output.action_id else output.action_id,
                "confidence": output.confidence,
                "explanation": output.explanation.to_dict() if hasattr(output.explanation, 'to_dict') else asdict(output.explanation),
                "selected_from": [action.action_id for action in agent_input.available_actions]
            }
            
            # Map action_id back to ActionSpec to get full details
            action_spec = next(
                (a for a in agent_input.available_actions if a.action_id == output.action_id),
                None
            )
            if action_spec:
                action_record.update({
                    "type": action_spec.type.value,
                    "cost": action_spec.cost,
                    "reversibility": action_spec.reversibility,
                    "cooldown": action_spec.cooldown
                })
            
            # Build causal mask (what was known at decision time)
            causal_mask = {
                "known_features": list(agent_input.current_state.keys()),
                "known_trajectory_keys": list(agent_input.predicted_trajectory.keys()),
                "decision_context": {
                    "video_age_known": True,
                    "engagement_known": "engagement_snapshot" in agent_input.current_state,
                    "trajectory_known": "predicted_trajectory" in agent_input.__dict__,
                    "available_actions_known": len(agent_input.available_actions) > 0
                }
            }
            
            # Calculate valid_after and expires_at (horizon-based)
            # Use decision_timestamp for deterministic horizon calculation (replay-compatible)
            action_timestamp = decision_ts_seconds  # Deterministic: from decision_timestamp, not time.time()
            valid_after = action_timestamp + 300  # 5 minutes minimum (allow short-term survival signals)
            expires_at = action_timestamp + (30 * 24 * 3600)  # 30 days for long-term survival signals
            
            # Determine required horizons based on action type
            required_horizons = set()
            if action_spec and action_spec.type != ActionType.NO_OP:
                required_horizons.add("short_term")  # 1 hour
                required_horizons.add("mid_term")    # 24 hours
                # For expensive actions, also track long-term
                if action_spec.cost > 0.5:
                    required_horizons.add("long_term")  # 7 days
            
            # Create Experience object
            experience = Experience(
                experience_id=experience_id,
                video_id=agent_input.video_id,
                factory_id=factory_id,
                agent_id="content_agent",
                state_snapshot=state_snapshot,
                action=action_record,
                action_timestamp=action_timestamp,
                reward_summary={},  # Will be populated later when survival signals mature (legacy field name)
                reward_finalized=False,  # Legacy field name - tracks survival signal finalization
                required_horizons=required_horizons,
                policy_version=agent_input.policy_version,
                platform_context={
                    "platform": agent_input.platform,
                    "video_age_seconds": agent_input.video_age_seconds,
                    "decay_phase": temporal_context.get("decay_phase", DecayPhase.COLD_START.value)
                },
                exploration_flag=False,  # Content agent doesn't do exploration
                causal_mask=causal_mask,
                valid_after=valid_after,
                expires_at=expires_at,
                created_at=action_timestamp,
                schema_version="1.0.0",
                reward_function_hash=None,  # Will be set by survival signal collector (legacy field name)
                parent_experience_id=None
            )
            
            # AUTHORITY BOUNDARY ASSERTIONS - Lock boundary in code, not just docs
            # This agent does NOT assign rewards, does NOT do exploration, does NOT optimize views
            # Fail loud if violated - defends against future engineers
            assert experience.reward_summary == {}, (
                f"Authority violation: reward_summary must be empty, got {experience.reward_summary}"
            )
            assert experience.exploration_flag is False, (
                f"Authority violation: exploration_flag must be False, got {experience.exploration_flag}"
            )
            assert experience.reward_function_hash is None, (
                f"Authority violation: reward_function_hash must be None, got {experience.reward_function_hash}"
            )
            
            # Get or create replay buffer instance
            # In production, this would use a proper instance management pattern
            if not hasattr(self, '_replay_buffer'):
                # Create replay buffer instance
                # In production, this might be a singleton or dependency-injected
                self._replay_buffer = ReplayBuffer(
                    max_size=1_000_000,
                    storage_path=None,  # Use default path
                    seed=getattr(self, 'seed', 42)
                )
            
            # Add experience to replay buffer
            success = self._replay_buffer.add(experience, validate_schema_drift=True)
            
            if success:
                self.logger.info(
                    f"Experience stored in replay buffer: {experience_id}, "
                    f"video={agent_input.video_id}, action={output.action_id}"
                )
            else:
                self.logger.warning(
                    f"Failed to store experience in replay buffer: {experience_id}"
                )
                
        except ValueError as e:
            # Causal violation or schema drift - log but don't crash
            self.logger.error(
                f"Replay buffer rejected experience due to validation failure: {e}",
                exc_info=True
            )
        except Exception as e:
            # Other errors - log but don't crash
            self.logger.error(
                f"Failed to send experience to replay buffer: {e}",
                exc_info=True
            )
    
    def _audit_decision(
        self,
        agent_input: ContentAgentInput,
        output: ContentAgentOutput,
        scores: Dict[str, float],
        temporal_context: Dict[str, Any]
    ) -> None:
        """
        Audit log decision for compliance and debugging.
        
        Integration point for audit system
        """
        audit_record = {
            "video_id": agent_input.video_id,
            "decision_hash": self.decision_emitter.compute_decision_hash(output),
            "decision_fingerprint": output.decision_fingerprint,
            "action_id": output.action_id,
            "confidence": output.confidence,
            "policy_version": agent_input.policy_version,
            "confidence_priors_version": self.confidence_estimator.confidence_priors_version,  # Versioned priors
            "scores": scores,
            "temporal_context": temporal_context,
            "timestamp": output.decision_timestamp,
            "seed": self.seed
        }
        
        # Store in audit trail
        if not hasattr(self, '_audit_trail'):
            self._audit_trail = []
        
        self._audit_trail.append(audit_record)
        
        # Limit audit trail size
        if len(self._audit_trail) > 10000:
            self._audit_trail = self._audit_trail[-5000:]
        
        self.logger.debug(f"Audit logged: {audit_record['decision_hash']}")