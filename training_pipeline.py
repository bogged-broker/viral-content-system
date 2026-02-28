"""
/models/evaluation/training_pipeline.py

Training Authorization, Gating & Safety Orchestration Layer

This file answers ONE question:
"Is it safe, valid, and strategically correct to train or update this model 
right now — and if so, how aggressively?"

HARD RULE: FAIL means zero training. No override. Ever.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple
import json
import hashlib
import logging

logger = logging.getLogger(__name__)


class TrainingDecision(Enum):
    """Training authorization outcomes"""
    ALLOW = "ALLOW"
    THROTTLE = "THROTTLE"
    SHADOW = "SHADOW"
    BLOCK = "BLOCK"
    ROLLBACK = "ROLLBACK"


class TrainingMode(Enum):
    """Permitted training modes"""
    FINE_TUNE = "fine_tune"
    ADAPTER = "adapter"
    HEAD_ONLY = "head_only"
    FROZEN = "frozen"


class RiskLevel(Enum):
    """Training risk assessment levels"""
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ValidationInputs:
    """Contract from validation_pipeline.py"""
    validity_status: str  # PASS / WARN / FAIL
    confidence: float
    drift_magnitude: float
    drift_detected: bool
    leakage_flags: List[str]
    stability_score: float
    timestamp: datetime


@dataclass
class EarlySignalInputs:
    """Contract from early_signal_detector.py"""
    early_lift: float
    false_positive_rate: float
    decay_detected: bool
    decay_signature: Optional[str]


@dataclass
class LongTailInputs:
    """Contract from long_tail_tracker.py"""
    tail_survival_rate: float
    late_engagement_integrity: float
    evergreen_stability: float
    tail_degradation_detected: bool


@dataclass
class TrainingMetadata:
    """Historical training state"""
    model_id: str
    last_training_timestamp: Optional[datetime]
    last_checkpoint_hash: str
    training_frequency_days: float
    prior_update_magnitude: float
    consecutive_failures: int


@dataclass
class TrainingAuthorization:
    """Output manifest - MUST be obeyed by training code"""
    model_id: str
    decision: TrainingDecision
    allowed_modes: List[TrainingMode]
    max_learning_rate: float
    max_steps: int
    cooldown_hours: float
    confidence: float
    reasons: List[str]
    expires_at: datetime
    risk_level: RiskLevel
    version_hash: str
    
    def to_dict(self) -> Dict:
        """Serializable output"""
        return {
            "model_id": self.model_id,
            "decision": self.decision.value,
            "allowed_modes": [m.value for m in self.allowed_modes],
            "max_learning_rate": self.max_learning_rate,
            "max_steps": self.max_steps,
            "cooldown_hours": self.cooldown_hours,
            "confidence": self.confidence,
            "reasons": self.reasons,
            "expires_at": self.expires_at.isoformat(),
            "risk_level": self.risk_level.value,
            "version_hash": self.version_hash
        }


class EligibilityResolver:
    """Determines which models are eligible for training"""
    
    def __init__(self, config: Dict):
        self.core_predictor_cooldown_days = config.get("core_predictor_cooldown_days", 7)
        self.long_tail_cooldown_days = config.get("long_tail_cooldown_days", 14)
        self.adapter_cooldown_days = config.get("adapter_cooldown_days", 1)
        
    def resolve(
        self, 
        model_id: str, 
        metadata: TrainingMetadata,
        is_core_predictor: bool,
        is_long_tail: bool
    ) -> Tuple[bool, List[str], Set[TrainingMode]]:
        """
        Returns: (is_eligible, reasons, allowed_modes)
        """
        reasons = []
        allowed_modes = set()
        
        # Check cooldown periods
        if metadata.last_training_timestamp:
            hours_since_train = (
                datetime.utcnow() - metadata.last_training_timestamp
            ).total_seconds() / 3600
            
            required_cooldown = self._get_required_cooldown(
                is_core_predictor, is_long_tail
            )
            
            if hours_since_train < required_cooldown * 24:
                reasons.append(
                    f"Cooldown active: {hours_since_train:.1f}h / {required_cooldown*24}h required"
                )
                return False, reasons, allowed_modes
        
        # Determine allowed training modes based on model type
        if is_core_predictor:
            # Core predictors update conservatively
            allowed_modes.add(TrainingMode.HEAD_ONLY)
            allowed_modes.add(TrainingMode.ADAPTER)
            reasons.append("Core predictor: conservative update modes only")
        elif is_long_tail:
            # Long-tail models are even more conservative
            allowed_modes.add(TrainingMode.HEAD_ONLY)
            reasons.append("Long-tail model: head-only updates preferred")
        else:
            # Standard models can fine-tune
            allowed_modes.add(TrainingMode.FINE_TUNE)
            allowed_modes.add(TrainingMode.ADAPTER)
            allowed_modes.add(TrainingMode.HEAD_ONLY)
            reasons.append("Standard model: full training modes available")
        
        # Check consecutive failures
        if metadata.consecutive_failures >= 3:
            reasons.append(f"Blocked: {metadata.consecutive_failures} consecutive failures")
            return False, reasons, set()
        
        return True, reasons, allowed_modes
    
    def _get_required_cooldown(self, is_core: bool, is_tail: bool) -> float:
        """Get cooldown in days"""
        if is_tail:
            return self.long_tail_cooldown_days
        elif is_core:
            return self.core_predictor_cooldown_days
        else:
            return self.adapter_cooldown_days


class RiskAssessor:
    """Combines signals to produce training risk score"""
    
    def __init__(self, config: Dict):
        self.drift_risk_threshold = config.get("drift_risk_threshold", 0.3)
        self.stability_floor = config.get("stability_floor", 0.7)
        self.confidence_floor = config.get("confidence_floor", 0.6)
        
    def assess(
        self,
        validation: ValidationInputs,
        early_signal: EarlySignalInputs,
        long_tail: LongTailInputs
    ) -> Tuple[RiskLevel, float, List[str]]:
        """
        Returns: (risk_level, risk_score, reasons)
        Risk score: 0.0 (safe) to 1.0 (critical)
        """
        risk_score = 0.0
        reasons = []
        
        # CRITICAL: Validation failure is non-negotiable
        if validation.validity_status == "FAIL":
            return RiskLevel.CRITICAL, 1.0, ["Validation FAILED - training prohibited"]
        
        # Drift risk
        if validation.drift_detected:
            drift_risk = min(validation.drift_magnitude, 1.0) * 0.4
            risk_score += drift_risk
            reasons.append(f"Drift detected: magnitude {validation.drift_magnitude:.3f}")
        
        # Confidence degradation
        if validation.confidence < self.confidence_floor:
            conf_risk = (self.confidence_floor - validation.confidence) * 0.3
            risk_score += conf_risk
            reasons.append(f"Low confidence: {validation.confidence:.3f}")
        
        # Stability concerns
        if validation.stability_score < self.stability_floor:
            stability_risk = (self.stability_floor - validation.stability_score) * 0.25
            risk_score += stability_risk
            reasons.append(f"Stability degraded: {validation.stability_score:.3f}")
        
        # Early signal decay
        if early_signal.decay_detected:
            risk_score += 0.2
            reasons.append(f"Early lift decay: {early_signal.decay_signature}")
        
        # Long-tail degradation
        if long_tail.tail_degradation_detected:
            risk_score += 0.3
            reasons.append(
                f"Long-tail degradation: survival={long_tail.tail_survival_rate:.3f}"
            )
        
        # Data leakage flags
        if validation.leakage_flags:
            risk_score += 0.4
            reasons.append(f"Leakage detected: {', '.join(validation.leakage_flags)}")
        
        # Determine risk level
        risk_score = min(risk_score, 1.0)
        if risk_score >= 0.8:
            risk_level = RiskLevel.CRITICAL
        elif risk_score >= 0.5:
            risk_level = RiskLevel.HIGH
        elif risk_score >= 0.25:
            risk_level = RiskLevel.MODERATE
        else:
            risk_level = RiskLevel.LOW
        
        return risk_level, risk_score, reasons


class DriftRiskInterpreter:
    """Drift-aware training logic"""
    
    def interpret(
        self,
        drift_magnitude: float,
        drift_detected: bool
    ) -> Tuple[bool, str, Dict]:
        """
        Returns: (allow_incremental, strategy, params)
        
        CRITICAL: Small, frequent updates under drift cause silent collapse
        """
        if not drift_detected:
            return True, "incremental", {}
        
        if drift_magnitude < 0.2:
            # Small drift - cautious incremental allowed
            return True, "cautious_incremental", {
                "lr_multiplier": 0.5,
                "max_steps_multiplier": 0.7
            }
        elif drift_magnitude < 0.5:
            # Moderate drift - staged retraining only
            return False, "staged_retrain", {
                "requires_canary": True,
                "confidence_interval_multiplier": 1.5
            }
        else:
            # High drift - full retraining with extended validation
            return False, "full_retrain", {
                "requires_shadow": True,
                "extended_validation_days": 7,
                "confidence_interval_multiplier": 2.0
            }


class TrainingBudgetAllocator:
    """Controls training aggressiveness"""
    
    def __init__(self, config: Dict):
        self.base_lr = config.get("base_learning_rate", 1e-4)
        self.base_steps = config.get("base_max_steps", 10000)
        self.conservative_lr = config.get("conservative_learning_rate", 1e-5)
        self.conservative_steps = config.get("conservative_max_steps", 1000)
        
    def allocate(
        self,
        risk_level: RiskLevel,
        risk_score: float,
        drift_params: Dict,
        allowed_modes: Set[TrainingMode]
    ) -> Tuple[float, int]:
        """
        Returns: (max_lr, max_steps)
        """
        # Start with base budget
        max_lr = self.base_lr
        max_steps = self.base_steps
        
        # Apply risk-based throttling
        if risk_level == RiskLevel.CRITICAL:
            # Severe throttling
            max_lr = self.conservative_lr * 0.1
            max_steps = self.conservative_steps // 10
        elif risk_level == RiskLevel.HIGH:
            max_lr = self.conservative_lr * 0.5
            max_steps = self.conservative_steps
        elif risk_level == RiskLevel.MODERATE:
            max_lr = self.base_lr * 0.7
            max_steps = int(self.base_steps * 0.7)
        
        # Apply drift-aware adjustments
        if "lr_multiplier" in drift_params:
            max_lr *= drift_params["lr_multiplier"]
        if "max_steps_multiplier" in drift_params:
            max_steps = int(max_steps * drift_params["max_steps_multiplier"])
        
        # Conservative modes get reduced budgets
        if TrainingMode.HEAD_ONLY in allowed_modes and len(allowed_modes) == 1:
            max_lr *= 0.8
            max_steps = int(max_steps * 0.6)
        
        return max_lr, max_steps


class CooldownEnforcer:
    """Enforces minimum time between updates and backoff"""
    
    def __init__(self, config: Dict):
        self.base_cooldown_hours = config.get("base_cooldown_hours", 24)
        self.backoff_multiplier = config.get("backoff_multiplier", 2.0)
        self.max_cooldown_hours = config.get("max_cooldown_hours", 336)  # 2 weeks
        
    def compute_cooldown(
        self,
        metadata: TrainingMetadata,
        risk_level: RiskLevel
    ) -> float:
        """Returns cooldown in hours"""
        cooldown = self.base_cooldown_hours
        
        # Exponential backoff after failures
        if metadata.consecutive_failures > 0:
            cooldown *= (self.backoff_multiplier ** metadata.consecutive_failures)
        
        # Risk-based extension
        if risk_level == RiskLevel.HIGH:
            cooldown *= 2
        elif risk_level == RiskLevel.CRITICAL:
            cooldown *= 4
        
        return min(cooldown, self.max_cooldown_hours)


class RolloutController:
    """Manages canary vs shadow vs full rollout"""
    
    def determine_rollout(
        self,
        risk_level: RiskLevel,
        drift_params: Dict,
        validation_confidence: float
    ) -> Tuple[TrainingDecision, List[str]]:
        """
        Returns: (decision, requirements)
        """
        reasons = []
        
        # Shadow training required for high-risk scenarios
        if drift_params.get("requires_shadow", False):
            reasons.append("Drift magnitude requires shadow training")
            return TrainingDecision.SHADOW, reasons
        
        # Canary for moderate risk
        if drift_params.get("requires_canary", False) or risk_level == RiskLevel.HIGH:
            reasons.append("Risk level requires canary rollout")
            return TrainingDecision.THROTTLE, reasons
        
        # Low confidence = shadow first
        if validation_confidence < 0.7:
            reasons.append(f"Low confidence ({validation_confidence:.3f}) requires shadow")
            return TrainingDecision.SHADOW, reasons
        
        # Default to allow with monitoring
        reasons.append("Risk acceptable for full training")
        return TrainingDecision.ALLOW, reasons


class RLUpdateGate:
    """Gates RL policy updates with strict criteria"""
    
    def __init__(self, config: Dict):
        self.max_reward_correlation = config.get("max_reward_correlation", 0.3)
        self.min_calibration_score = config.get("min_calibration_score", 0.8)
        
    def gate_rl_update(
        self,
        validation: ValidationInputs,
        long_tail: LongTailInputs,
        reward_correlation: float,
        calibration_score: float
    ) -> Tuple[bool, List[str]]:
        """
        Returns: (allow_rl_update, reasons)
        
        RL policies may update ONLY if:
        - predictor calibration holds
        - reward correlation < threshold
        - long-tail metrics improve
        """
        reasons = []
        
        # Check calibration
        if calibration_score < self.min_calibration_score:
            reasons.append(
                f"Calibration too low: {calibration_score:.3f} < {self.min_calibration_score}"
            )
            return False, reasons
        
        # Check reward correlation
        if reward_correlation > self.max_reward_correlation:
            reasons.append(
                f"Reward correlation too high: {reward_correlation:.3f}"
            )
            return False, reasons
        
        # Check long-tail health
        if long_tail.tail_degradation_detected:
            reasons.append("Long-tail metrics degrading - RL freeze")
            return False, reasons
        
        # Validation must pass
        if validation.validity_status == "FAIL":
            reasons.append("Validation failed - RL freeze")
            return False, reasons
        
        reasons.append("RL update criteria satisfied")
        return True, reasons


class RollbackController:
    """Handles automatic rollback on post-training failures"""
    
    def should_rollback(
        self,
        post_training_validation_status: str,
        post_training_metrics: Dict
    ) -> Tuple[bool, List[str]]:
        """
        Returns: (should_rollback, reasons)
        """
        reasons = []
        
        if post_training_validation_status == "FAIL":
            reasons.append("Post-training validation FAILED")
            return True, reasons
        
        # Check for catastrophic metric degradation
        if post_training_metrics.get("tail_survival_drop", 0) > 0.2:
            reasons.append("Catastrophic long-tail degradation detected")
            return True, reasons
        
        if post_training_metrics.get("stability_drop", 0) > 0.3:
            reasons.append("Severe stability degradation")
            return True, reasons
        
        return False, reasons


class DecisionEmitter:
    """Emits auditable training authorization manifests"""
    
    def emit(
        self,
        model_id: str,
        decision: TrainingDecision,
        allowed_modes: Set[TrainingMode],
        max_lr: float,
        max_steps: int,
        cooldown_hours: float,
        confidence: float,
        reasons: List[str],
        risk_level: RiskLevel,
        inputs_hash: str
    ) -> TrainingAuthorization:
        """
        Creates deterministic, auditable authorization
        """
        # Generate version hash for reproducibility
        version_data = {
            "model_id": model_id,
            "decision": decision.value,
            "inputs_hash": inputs_hash,
            "timestamp": datetime.utcnow().isoformat()
        }
        version_hash = hashlib.sha256(
            json.dumps(version_data, sort_keys=True).encode()
        ).hexdigest()[:16]
        
        # Authorization expires after cooldown period
        expires_at = datetime.utcnow() + timedelta(hours=cooldown_hours)
        
        return TrainingAuthorization(
            model_id=model_id,
            decision=decision,
            allowed_modes=list(allowed_modes),
            max_learning_rate=max_lr,
            max_steps=max_steps,
            cooldown_hours=cooldown_hours,
            confidence=confidence,
            reasons=reasons,
            expires_at=expires_at,
            risk_level=risk_level,
            version_hash=version_hash
        )


class TrainingPipeline:
    """
    Training Authorization, Gating & Safety Orchestration Layer
    
    This is the last line of defense before training.
    Training NEVER proceeds without this pipeline's approval.
    """
    
    def __init__(self, config: Dict):
        self.config = config
        
        # Initialize all components
        self.eligibility_resolver = EligibilityResolver(config)
        self.risk_assessor = RiskAssessor(config)
        self.drift_interpreter = DriftRiskInterpreter()
        self.budget_allocator = TrainingBudgetAllocator(config)
        self.cooldown_enforcer = CooldownEnforcer(config)
        self.rollout_controller = RolloutController()
        self.rl_gate = RLUpdateGate(config)
        self.rollback_controller = RollbackController()
        self.decision_emitter = DecisionEmitter()
        
    def authorize_training(
        self,
        model_id: str,
        validation: ValidationInputs,
        early_signal: EarlySignalInputs,
        long_tail: LongTailInputs,
        metadata: TrainingMetadata,
        is_core_predictor: bool = False,
        is_long_tail_model: bool = False,
        is_rl_policy: bool = False,
        reward_correlation: float = 0.0,
        calibration_score: float = 1.0
    ) -> TrainingAuthorization:
        """
        Main authorization pipeline
        
        Returns training authorization manifest that MUST be obeyed
        """
        all_reasons = []
        
        # HARD RULE: FAIL means zero training
        if validation.validity_status == "FAIL":
            logger.critical(f"Training BLOCKED for {model_id}: validation FAILED")
            return self.decision_emitter.emit(
                model_id=model_id,
                decision=TrainingDecision.BLOCK,
                allowed_modes=set(),
                max_lr=0.0,
                max_steps=0,
                cooldown_hours=self.cooldown_enforcer.max_cooldown_hours,
                confidence=0.0,
                reasons=["Validation FAILED - training prohibited"],
                risk_level=RiskLevel.CRITICAL,
                inputs_hash=self._compute_inputs_hash(validation, early_signal, long_tail)
            )
        
        # Step 1: Check eligibility
        is_eligible, eligibility_reasons, allowed_modes = self.eligibility_resolver.resolve(
            model_id=model_id,
            metadata=metadata,
            is_core_predictor=is_core_predictor,
            is_long_tail=is_long_tail_model
        )
        all_reasons.extend(eligibility_reasons)
        
        if not is_eligible:
            logger.info(f"Training BLOCKED for {model_id}: not eligible")
            return self.decision_emitter.emit(
                model_id=model_id,
                decision=TrainingDecision.BLOCK,
                allowed_modes=set(),
                max_lr=0.0,
                max_steps=0,
                cooldown_hours=self.cooldown_enforcer.compute_cooldown(metadata, RiskLevel.LOW),
                confidence=validation.confidence,
                reasons=all_reasons,
                risk_level=RiskLevel.LOW,
                inputs_hash=self._compute_inputs_hash(validation, early_signal, long_tail)
            )
        
        # Step 2: Assess risk
        risk_level, risk_score, risk_reasons = self.risk_assessor.assess(
            validation=validation,
            early_signal=early_signal,
            long_tail=long_tail
        )
        all_reasons.extend(risk_reasons)
        
        # Step 3: RL-specific gating
        if is_rl_policy:
            rl_allowed, rl_reasons = self.rl_gate.gate_rl_update(
                validation=validation,
                long_tail=long_tail,
                reward_correlation=reward_correlation,
                calibration_score=calibration_score
            )
            all_reasons.extend(rl_reasons)
            
            if not rl_allowed:
                logger.warning(f"RL update BLOCKED for {model_id}")
                return self.decision_emitter.emit(
                    model_id=model_id,
                    decision=TrainingDecision.BLOCK,
                    allowed_modes=set(),
                    max_lr=0.0,
                    max_steps=0,
                    cooldown_hours=48.0,  # RL freeze for 48h
                    confidence=validation.confidence,
                    reasons=all_reasons,
                    risk_level=risk_level,
                    inputs_hash=self._compute_inputs_hash(validation, early_signal, long_tail)
                )
        
        # Step 4: Interpret drift
        allow_incremental, strategy, drift_params = self.drift_interpreter.interpret(
            drift_magnitude=validation.drift_magnitude,
            drift_detected=validation.drift_detected
        )
        all_reasons.append(f"Training strategy: {strategy}")
        
        # Step 5: Allocate training budget
        max_lr, max_steps = self.budget_allocator.allocate(
            risk_level=risk_level,
            risk_score=risk_score,
            drift_params=drift_params,
            allowed_modes=allowed_modes
        )
        
        # Step 6: Compute cooldown
        cooldown_hours = self.cooldown_enforcer.compute_cooldown(
            metadata=metadata,
            risk_level=risk_level
        )
        
        # Step 7: Determine rollout strategy
        decision, rollout_reasons = self.rollout_controller.determine_rollout(
            risk_level=risk_level,
            drift_params=drift_params,
            validation_confidence=validation.confidence
        )
        all_reasons.extend(rollout_reasons)
        
        # Emit final authorization
        logger.info(
            f"Training authorized for {model_id}: {decision.value} "
            f"(risk={risk_level.value}, lr={max_lr:.2e}, steps={max_steps})"
        )
        
        return self.decision_emitter.emit(
            model_id=model_id,
            decision=decision,
            allowed_modes=allowed_modes,
            max_lr=max_lr,
            max_steps=max_steps,
            cooldown_hours=cooldown_hours,
            confidence=validation.confidence,
            reasons=all_reasons,
            risk_level=risk_level,
            inputs_hash=self._compute_inputs_hash(validation, early_signal, long_tail)
        )
    
    def _compute_inputs_hash(
        self,
        validation: ValidationInputs,
        early_signal: EarlySignalInputs,
        long_tail: LongTailInputs
    ) -> str:
        """Deterministic hash of all inputs for auditability"""
        inputs_data = {
            "validation_status": validation.validity_status,
            "confidence": validation.confidence,
            "drift_magnitude": validation.drift_magnitude,
            "early_lift": early_signal.early_lift,
            "tail_survival": long_tail.tail_survival_rate
        }
        return hashlib.sha256(
            json.dumps(inputs_data, sort_keys=True).encode()
        ).hexdigest()[:16]


# Example usage and integration point
if __name__ == "__main__":
    # Configuration
    config = {
        "base_learning_rate": 1e-4,
        "base_max_steps": 10000,
        "core_predictor_cooldown_days": 7,
        "drift_risk_threshold": 0.3,
        "max_reward_correlation": 0.3
    }
    
    pipeline = TrainingPipeline(config)
    
    # Example inputs (normally from validation/tracking systems)
    validation = ValidationInputs(
        validity_status="PASS",
        confidence=0.85,
        drift_magnitude=0.15,
        drift_detected=True,
        leakage_flags=[],
        stability_score=0.9,
        timestamp=datetime.utcnow()
    )
    
    early_signal = EarlySignalInputs(
        early_lift=0.05,
        false_positive_rate=0.02,
        decay_detected=False,
        decay_signature=None
    )
    
    long_tail = LongTailInputs(
        tail_survival_rate=0.75,
        late_engagement_integrity=0.8,
        evergreen_stability=0.85,
        tail_degradation_detected=False
    )
    
    metadata = TrainingMetadata(
        model_id="predictor_v2",
        last_training_timestamp=datetime.utcnow() - timedelta(days=8),
        last_checkpoint_hash="abc123",
        training_frequency_days=7.0,
        prior_update_magnitude=0.1,
        consecutive_failures=0
    )
    
    # Get training authorization
    auth = pipeline.authorize_training(
        model_id="predictor_v2",
        validation=validation,
        early_signal=early_signal,
        long_tail=long_tail,
        metadata=metadata,
        is_core_predictor=True
    )
    
    print(json.dumps(auth.to_dict(), indent=2))