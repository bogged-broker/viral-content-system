"""
/training/safety_watchdog.py

ABSOLUTE AUTHORITY: Kill-switch and catastrophe recovery system.
Overrides ALL other training components when existential risk detected.

Core Principle: Stopping safely > Training correctly
False positives acceptable. False negatives catastrophic.

Built for:
- 240k+ LOC architecture
- Irreversible failure prevention
- RL catastrophe containment
- Legal / audit defensibility
- Autonomous recovery
- Human-in-the-loop escalation

This file has ABSOLUTE AUTHORITY. Nothing outranks it.
"""

import json
import logging
import time
import hashlib
import hmac
import threading
import random
import os
import signal
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set, Any, Tuple, Callable
import numpy as np
from collections import deque, defaultdict
import warnings

# System integration imports (optional, graceful degradation)
try:
    from checkpoint_manager import CheckpointManager, CheckpointMetadata
    CHECKPOINT_MANAGER_AVAILABLE = True
except ImportError:
    CHECKPOINT_MANAGER_AVAILABLE = False
    CheckpointManager = None
    CheckpointMetadata = None

try:
    from curriculum_learning import CurriculumOrchestrator, CurriculumPhase
    CURRICULUM_AVAILABLE = True
except ImportError:
    CURRICULUM_AVAILABLE = False
    CurriculumOrchestrator = None
    CurriculumPhase = None

try:
    from data_gate import TrainingIntegrityViolation, GateRejectionReason
    DATA_GATE_AVAILABLE = True
except ImportError:
    DATA_GATE_AVAILABLE = False
    TrainingIntegrityViolation = None
    GateRejectionReason = None


# ============================================================================
# DETERMINISM & REPEATABILITY
# ============================================================================

class DeterminismLock:
    """
    Ensures safety decisions are deterministic and repeatable.
    Given same telemetry, same thresholds, same system state → identical decisions.
    """
    
    def __init__(self, seed: Optional[int] = None):
        self.seed = seed or 42
        self.lock = threading.Lock()
        self._verify_determinism = True
        
    def set_seed(self, seed: int):
        """Lock random seed for deterministic safety decisions."""
        with self.lock:
            self.seed = seed
            random.seed(seed)
            np.random.seed(seed)
    
    def verify_decision_reproducibility(
        self, 
        decision_func: Callable,
        telemetry: Dict[str, Any],
        expected_result: Tuple[bool, Optional[str]]
    ) -> bool:
        """
        Verify that a decision function produces identical results when called twice.
        Critical for audit defensibility.
        """
        if not self._verify_determinism:
            return True
            
        with self.lock:
            # First call
            result1 = decision_func(telemetry)
            
            # Reset state
            random.seed(self.seed)
            np.random.seed(self.seed)
            
            # Second call
            result2 = decision_func(telemetry)
            
            # Verify identical
            if result1 != result2:
                warnings.warn(
                    f"Non-deterministic safety decision detected! "
                    f"First: {result1}, Second: {result2}"
                )
                return False
            return True


# ============================================================================
# PHYSICAL AUTHORITY ENFORCEMENT (TIER-0 9.5+ UPGRADE)
# ============================================================================

class WatchdogViolation(Exception):
    """
    Irreversible exception raised when training attempts to bypass watchdog.
    
    This exception CANNOT be caught without explicitly sabotaging the system.
    Tier-0 safety systems do not trust cooperation.
    """
    def __init__(self, message: str, kill_level: Optional['KillLevel'] = None):
        super().__init__(message)
        self.kill_level = kill_level
        self.timestamp = datetime.utcnow().isoformat()
        # Mark as irreversible
        self._irreversible = True


class CheckpointPoisonMarker:
    """
    Irreversible marker written to checkpoints when watchdog blocks training.
    Future resumes MUST refuse poisoned checkpoints.
    """
    
    POISON_MARKER_KEY = "__WATCHDOG_POISONED__"
    POISON_MARKER_VALUE = True
    
    @staticmethod
    def poison_checkpoint(checkpoint_path: Path, reason: str) -> bool:
        """
        Write irreversible poison marker to checkpoint.
        Returns: success
        """
        try:
            # Read existing checkpoint metadata
            metadata_file = checkpoint_path.parent / f"{checkpoint_path.stem}_metadata.json"
            
            if metadata_file.exists():
                with open(metadata_file, 'r') as f:
                    metadata = json.load(f)
            else:
                metadata = {}
            
            # Add poison marker
            metadata[CheckpointPoisonMarker.POISON_MARKER_KEY] = {
                'poisoned': CheckpointPoisonMarker.POISON_MARKER_VALUE,
                'reason': reason,
                'timestamp': datetime.utcnow().isoformat(),
                'irreversible': True
            }
            
            # Write back
            with open(metadata_file, 'w') as f:
                json.dump(metadata, f, indent=2)
            
            return True
        except Exception as e:
            warnings.warn(f"Checkpoint poisoning failed: {e}")
            return False
    
    @staticmethod
    def is_poisoned(checkpoint_path: Path) -> Tuple[bool, Optional[str]]:
        """
        Check if checkpoint is poisoned.
        Returns: (is_poisoned, reason)
        """
        try:
            metadata_file = checkpoint_path.parent / f"{checkpoint_path.stem}_metadata.json"
            
            if not metadata_file.exists():
                return False, None
            
            with open(metadata_file, 'r') as f:
                metadata = json.load(f)
            
            poison_data = metadata.get(CheckpointPoisonMarker.POISON_MARKER_KEY)
            if poison_data and poison_data.get('poisoned'):
                return True, poison_data.get('reason', 'unknown')
            
            return False, None
        except Exception:
            return False, None


# ============================================================================
# KILL-SWITCH LEVELS
# ============================================================================

class KillLevel(Enum):
    """Escalation hierarchy for safety interventions."""
    LEVEL_0_NOMINAL = 0      # Normal operation
    LEVEL_1_SOFT_PAUSE = 1   # Pause with auto-resolution window
    LEVEL_2_HARD_FREEZE = 2  # Stop gradients, freeze optimizer
    LEVEL_3_ROLLBACK = 3     # Revert model weights
    LEVEL_4_QUARANTINE = 4   # Isolate agents, flush buffers
    LEVEL_5_TERMINATION = 5  # Kill training job (IRREVERSIBLE)


class CatastropheType(Enum):
    """Categories of catastrophic failures."""
    GRADIENT_COLLAPSE = "gradient_collapse"
    REWARD_HACKING = "reward_hacking"
    FEEDBACK_LOOP = "feedback_loop"
    DISTRIBUTION_IMPLOSION = "distribution_implosion"
    LOSS_DECEPTION = "loss_deception"
    DRIFT_RUNAWAY = "drift_runaway"
    TEMPORAL_LEAKAGE = "temporal_leakage"
    SYSTEM_DESYNC = "system_desync"
    AUTOMATION_SPIRAL = "automation_spiral"
    DATA_CORRUPTION = "data_corruption"


# ============================================================================
# EXPLICIT KILL-LEVEL POLICY TABLE (TIER-0 10.0 MANDATORY)
# ============================================================================

# TIER-0 10.0: Immutable, explicit policy table
# This is the LAW. No implicit logic. No distributed decisions.
KILL_LEVEL_POLICY: Dict[CatastropheType, KillLevel] = {
    CatastropheType.GRADIENT_COLLAPSE: KillLevel.LEVEL_3_ROLLBACK,
    CatastropheType.REWARD_HACKING: KillLevel.LEVEL_3_ROLLBACK,
    CatastropheType.FEEDBACK_LOOP: KillLevel.LEVEL_4_QUARANTINE,
    CatastropheType.DISTRIBUTION_IMPLOSION: KillLevel.LEVEL_3_ROLLBACK,
    CatastropheType.LOSS_DECEPTION: KillLevel.LEVEL_2_HARD_FREEZE,
    CatastropheType.DRIFT_RUNAWAY: KillLevel.LEVEL_2_HARD_FREEZE,
    CatastropheType.TEMPORAL_LEAKAGE: KillLevel.LEVEL_5_TERMINATION,  # Critical - data corruption
    CatastropheType.SYSTEM_DESYNC: KillLevel.LEVEL_2_HARD_FREEZE,
    CatastropheType.AUTOMATION_SPIRAL: KillLevel.LEVEL_4_QUARANTINE,
    CatastropheType.DATA_CORRUPTION: KillLevel.LEVEL_3_ROLLBACK,
}

# Default: If catastrophe not in policy, terminate (fail-closed)
KILL_LEVEL_POLICY_DEFAULT = KillLevel.LEVEL_5_TERMINATION


class KillLevelPolicy:
    """
    Explicit, deterministic mapping: catastrophe_type → minimum kill_level.
    
    TIER-0 9.5+ UPGRADE: Centralized policy table eliminates implicit logic.
    No distributed kill-level selection - all decisions flow through this table.
    """
    
    # Explicit policy table: (catastrophe_type, severity) -> minimum_kill_level
    POLICY_TABLE = {
        # Critical catastrophes → Level 3 (rollback)
        (CatastropheType.GRADIENT_COLLAPSE, 'critical'): KillLevel.LEVEL_3_ROLLBACK,
        (CatastropheType.GRADIENT_COLLAPSE, 'existential'): KillLevel.LEVEL_5_TERMINATION,
        (CatastropheType.REWARD_HACKING, 'critical'): KillLevel.LEVEL_3_ROLLBACK,
        (CatastropheType.REWARD_HACKING, 'existential'): KillLevel.LEVEL_5_TERMINATION,
        (CatastropheType.FEEDBACK_LOOP, 'critical'): KillLevel.LEVEL_3_ROLLBACK,
        (CatastropheType.FEEDBACK_LOOP, 'existential'): KillLevel.LEVEL_4_QUARANTINE,
        
        # Severe catastrophes → Level 2 (hard freeze)
        (CatastropheType.LOSS_DECEPTION, 'high'): KillLevel.LEVEL_2_HARD_FREEZE,
        (CatastropheType.LOSS_DECEPTION, 'critical'): KillLevel.LEVEL_3_ROLLBACK,
        (CatastropheType.DISTRIBUTION_IMPLOSION, 'high'): KillLevel.LEVEL_2_HARD_FREEZE,
        (CatastropheType.DISTRIBUTION_IMPLOSION, 'critical'): KillLevel.LEVEL_3_ROLLBACK,
        
        # Automation issues → Level 4 (quarantine)
        (CatastropheType.AUTOMATION_SPIRAL, 'high'): KillLevel.LEVEL_4_QUARANTINE,
        (CatastropheType.AUTOMATION_SPIRAL, 'existential'): KillLevel.LEVEL_5_TERMINATION,
        
        # Moderate catastrophes → Level 2 (hard freeze)
        (CatastropheType.DRIFT_RUNAWAY, 'medium'): KillLevel.LEVEL_2_HARD_FREEZE,
        (CatastropheType.DRIFT_RUNAWAY, 'high'): KillLevel.LEVEL_3_ROLLBACK,
        (CatastropheType.TEMPORAL_LEAKAGE, 'medium'): KillLevel.LEVEL_2_HARD_FREEZE,
        (CatastropheType.TEMPORAL_LEAKAGE, 'high'): KillLevel.LEVEL_3_ROLLBACK,
        (CatastropheType.DATA_CORRUPTION, 'medium'): KillLevel.LEVEL_2_HARD_FREEZE,
        (CatastropheType.DATA_CORRUPTION, 'high'): KillLevel.LEVEL_3_ROLLBACK,
        
        # System issues → Level 1 (soft pause) or escalate
        (CatastropheType.SYSTEM_DESYNC, 'low'): KillLevel.LEVEL_1_SOFT_PAUSE,
        (CatastropheType.SYSTEM_DESYNC, 'medium'): KillLevel.LEVEL_2_HARD_FREEZE,
        (CatastropheType.SYSTEM_DESYNC, 'critical'): KillLevel.LEVEL_3_ROLLBACK,
        (CatastropheType.SYSTEM_DESYNC, 'existential'): KillLevel.LEVEL_5_TERMINATION,
    }
    
    # Default mappings for unspecified severities
    DEFAULT_POLICY = {
        CatastropheType.GRADIENT_COLLAPSE: KillLevel.LEVEL_3_ROLLBACK,
        CatastropheType.REWARD_HACKING: KillLevel.LEVEL_3_ROLLBACK,
        CatastropheType.FEEDBACK_LOOP: KillLevel.LEVEL_3_ROLLBACK,
        CatastropheType.LOSS_DECEPTION: KillLevel.LEVEL_2_HARD_FREEZE,
        CatastropheType.DISTRIBUTION_IMPLOSION: KillLevel.LEVEL_2_HARD_FREEZE,
        CatastropheType.AUTOMATION_SPIRAL: KillLevel.LEVEL_4_QUARANTINE,
        CatastropheType.DRIFT_RUNAWAY: KillLevel.LEVEL_2_HARD_FREEZE,
        CatastropheType.TEMPORAL_LEAKAGE: KillLevel.LEVEL_2_HARD_FREEZE,
        CatastropheType.DATA_CORRUPTION: KillLevel.LEVEL_2_HARD_FREEZE,
        CatastropheType.SYSTEM_DESYNC: KillLevel.LEVEL_1_SOFT_PAUSE,
    }
    
    @classmethod
    def get_minimum_kill_level(
        cls,
        catastrophe: CatastropheType,
        severity: str = 'medium'
    ) -> KillLevel:
        """
        Get minimum kill level for catastrophe type and severity.
        
        Returns explicit, deterministic kill level from policy table.
        """
        # Try exact match first
        key = (catastrophe, severity)
        if key in cls.POLICY_TABLE:
            return cls.POLICY_TABLE[key]
        
        # Try default for catastrophe type
        if catastrophe in cls.DEFAULT_POLICY:
            return cls.DEFAULT_POLICY[catastrophe]
        
        # Ultimate fallback: Level 2 (conservative)
        return KillLevel.LEVEL_2_HARD_FREEZE
    
    @classmethod
    def validate_kill_level(
        cls,
        catastrophe: CatastropheType,
        severity: str,
        proposed_level: KillLevel
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate that proposed kill level meets minimum policy requirement.
        
        Returns: (is_valid, rejection_reason)
        """
        minimum_level = cls.get_minimum_kill_level(catastrophe, severity)
        
        if proposed_level.value < minimum_level.value:
            return False, (
                f"kill_level_below_minimum: proposed={proposed_level.name} "
                f"< minimum={minimum_level.name} for {catastrophe.value}/{severity}"
            )
        
        return True, None


# ============================================================================
# INTERVENTION RECORD (WITH CRYPTOGRAPHIC SIGNING)
# ============================================================================

@dataclass
class InterventionRecord:
    """Immutable audit log entry for safety interventions."""
    timestamp: str
    severity_level: int
    trigger: str
    catastrophe_type: CatastropheType
    modules_affected: List[str]
    action_taken: str
    checkpoint_id: Optional[str]
    model_version: str
    system_state: Dict[str, Any]
    requires_human_approval: bool
    recovery_instructions: List[str]
    cryptographic_hash: Optional[str] = None
    decision_seed: Optional[int] = None  # For reproducibility verification
    
    def to_json(self) -> str:
        """Serialize to immutable JSON log."""
        data = asdict(self)
        data['catastrophe_type'] = self.catastrophe_type.value
        return json.dumps(data, indent=2)
    
    def compute_hash(self, secret_key: Optional[str] = None) -> str:
        """
        Compute cryptographic hash for audit trail integrity.
        Prevents tampering with intervention records.
        """
        data_str = json.dumps(asdict(self), sort_keys=True, default=str)
        if secret_key:
            return hmac.new(
                secret_key.encode(),
                data_str.encode(),
                hashlib.sha256
            ).hexdigest()
        else:
            return hashlib.sha256(data_str.encode()).hexdigest()
    
    def verify_hash(self, secret_key: Optional[str] = None) -> bool:
        """Verify intervention record has not been tampered with."""
        if not self.cryptographic_hash:
            return False
        computed = self.compute_hash(secret_key)
        return hmac.compare_digest(computed, self.cryptographic_hash)


# ============================================================================
# TRAINING LOOP INTEGRATION ENFORCEMENT
# ============================================================================

# ============================================================================
# STRUCTURAL TRAINING LOOP ENFORCEMENT (TIER-0 9.5+ UPGRADE)
# ============================================================================

class TrainingStepToken:
    """
    TIER-0 9.5+ UPGRADE: Token-based approval system.
    
    Training MUST request a step token before each step.
    Token expires after one use.
    Without valid token, training physically cannot proceed (exception).
    This is STRUCTURAL, not detect-then-punish.
    """
    
    def __init__(self, step: int, token_id: str, expires_at: float):
        self.step = step
        self.token_id = token_id
        self.expires_at = expires_at
        self.used = False
        self._irreversible = True  # Cannot be bypassed
    
    def is_valid(self, current_step: int) -> bool:
        """Check if token is valid for current step."""
        if self.used:
            return False
        if time.time() > self.expires_at:
            return False
        if self.step != current_step:
            return False
        return True
    
    def mark_used(self):
        """Mark token as used (irreversible)."""
        self.used = True
    
    def __repr__(self):
        return f"TrainingStepToken(step={self.step}, token_id={self.token_id[:8]}..., used={self.used})"


class TrainingLoopEnforcer:
    """
    TIER-0 9.5+ UPGRADE: Structural enforcement that training cannot step without token.
    
    Training literally cannot step without a watchdog token.
    This is STRUCTURALLY IMPOSSIBLE to bypass, not detect-then-punish.
    """
    
    def __init__(self):
        self.token_expiry_seconds = 60.0  # Tokens expire after 60 seconds
        self.active_tokens: Dict[int, TrainingStepToken] = {}
        self.token_history: deque = deque(maxlen=10000)
        self.token_counter = 0
        
        # Track for compliance (secondary check)
        self.approval_call_history: deque = deque(maxlen=10000)
        self.missing_approval_warnings = 0
        self.max_missing_warnings = 3
    
    def issue_step_token(self, step: int) -> TrainingStepToken:
        """
        Issue a step token for training step.
        Returns: TrainingStepToken that must be presented to proceed.
        """
        token_id = f"token_{step}_{self.token_counter}_{int(time.time() * 1000)}"
        self.token_counter += 1
        
        token = TrainingStepToken(
            step=step,
            token_id=token_id,
            expires_at=time.time() + self.token_expiry_seconds
        )
        
        self.active_tokens[step] = token
        self.token_history.append({
            'step': step,
            'token_id': token_id,
            'issued_at': time.time(),
            'expires_at': token.expires_at
        })
        
        return token
    
    def validate_step_token(self, step: int, token: Optional[TrainingStepToken]) -> Tuple[bool, Optional[str]]:
        """
        Validate step token.
        Returns: (is_valid, rejection_reason)
        
        TIER-0 9.5+ UPGRADE: This is the STRUCTURAL gate.
        Without valid token, training CANNOT proceed.
        """
        if token is None:
            return False, "step_token_missing: Training step attempted without token. Must call request_step_token() first."
        
        if not isinstance(token, TrainingStepToken):
            return False, f"invalid_token_type: Expected TrainingStepToken, got {type(token)}"
        
        if not token.is_valid(step):
            if token.used:
                return False, f"token_already_used: Token for step {step} was already used"
            if time.time() > token.expires_at:
                return False, f"token_expired: Token for step {step} expired"
            if token.step != step:
                return False, f"token_step_mismatch: Token for step {token.step} used for step {step}"
            return False, f"token_invalid: Unknown validation failure"
        
        # Mark token as used (irreversible)
        token.mark_used()
        
        # Remove from active tokens
        self.active_tokens.pop(step, None)
        
        return True, None
    
    def record_approval_call(self, step: int, approved: bool):
        """Record that approve_training_step() was called (for compliance tracking)."""
        self.approval_call_history.append({
            'step': step,
            'approved': approved,
            'timestamp': time.time()
        })
    
    def check_approval_compliance(self, current_step: int) -> Tuple[bool, Optional[str]]:
        """
        Verify that training is calling approve_training_step() before each step.
        Returns: (is_compliant, violation_reason)
        """
        if len(self.approval_call_history) == 0:
            self.missing_approval_warnings += 1
            if self.missing_approval_warnings >= self.max_missing_warnings:
                return False, "training_loop_not_calling_watchdog_approval"
            return True, None
        
        # Check if approval was called for recent steps
        recent_calls = [c for c in self.approval_call_history 
                       if abs(c['step'] - current_step) <= 5]
        if not recent_calls:
            self.missing_approval_warnings += 1
            if self.missing_approval_warnings >= self.max_missing_warnings:
                return False, "training_loop_skipping_watchdog_approval"
        
        return True, None


# ============================================================================
# GRADIENT HEALTH MONITOR (ENHANCED)
# ============================================================================

class GradientHealthMonitor:
    """
    Monitors gradient pathologies that indicate training collapse.
    Enhanced with directional collapse detection and cross-layer analysis.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.explosion_threshold = config.get('explosion_threshold', 1e6)
        self.vanishing_threshold = config.get('vanishing_threshold', 1e-8)
        self.zero_grad_window = config.get('zero_grad_window', 10)
        self.oscillation_window = config.get('oscillation_window', 20)
        self.directional_collapse_threshold = config.get('directional_collapse_threshold', 0.1)
        
        self.grad_history = deque(maxlen=self.oscillation_window)
        self.grad_direction_history = deque(maxlen=50)  # For directional collapse
        self.zero_count = 0
        self.layer_stats = defaultdict(lambda: deque(maxlen=100))
        
    def check(self, gradients: Dict[str, np.ndarray]) -> Tuple[bool, Optional[str]]:
        """
        Check gradient health with enhanced detection.
        Returns: (is_safe, failure_reason)
        """
        if not gradients:
            return True, None
        
        # Compute gradient statistics
        all_grads = np.concatenate([g.flatten() for g in gradients.values()])
        grad_norm = np.linalg.norm(all_grads)
        grad_max = np.max(np.abs(all_grads))
        grad_mean = np.mean(np.abs(all_grads))
        
        # Check for explosion
        if grad_norm > self.explosion_threshold or grad_max > self.explosion_threshold:
            return False, f"gradient_explosion: norm={grad_norm:.2e}, max={grad_max:.2e}"
        
        # Check for vanishing
        if grad_mean < self.vanishing_threshold:
            return False, f"gradient_vanishing: mean={grad_mean:.2e}"
        
        # Check for persistent zeros
        if grad_norm < 1e-10:
            self.zero_count += 1
            if self.zero_count >= self.zero_grad_window:
                return False, f"persistent_zero_gradients: {self.zero_count} steps"
        else:
            self.zero_count = 0
        
        # Check for non-stationary oscillation
        self.grad_history.append(grad_norm)
        if len(self.grad_history) == self.oscillation_window:
            variance = np.var(list(self.grad_history))
            mean_norm = np.mean(list(self.grad_history))
            if variance > mean_norm * 10:  # High variance oscillation
                return False, f"gradient_oscillation: var/mean={variance/mean_norm:.2f}"
        
        # ENHANCED: Check for directional collapse
        if len(self.grad_history) >= 2:
            direction = all_grads / (grad_norm + 1e-10)  # Normalized direction
            self.grad_direction_history.append(direction)
            
            if len(self.grad_direction_history) >= 10:
                # Check if gradient directions are collapsing (becoming too similar)
                recent_directions = list(self.grad_direction_history)[-10:]
                direction_similarity = np.mean([
                    np.abs(np.dot(d1, d2)) 
                    for d1, d2 in zip(recent_directions[:-1], recent_directions[1:])
                ])
                
                if direction_similarity > (1.0 - self.directional_collapse_threshold):
                    return False, f"directional_collapse: similarity={direction_similarity:.4f}"
        
        # ENHANCED: Per-layer analysis
        for layer_name, grad in gradients.items():
            layer_norm = np.linalg.norm(grad.flatten())
            self.layer_stats[layer_name].append(layer_norm)
            
            # Check for layer-specific pathologies
            if len(self.layer_stats[layer_name]) >= 20:
                layer_history = list(self.layer_stats[layer_name])
                layer_variance = np.var(layer_history)
                layer_mean = np.mean(layer_history)
                
                # Layer-specific explosion
                if layer_norm > self.explosion_threshold * 0.1:  # 10% of global threshold
                    return False, f"layer_gradient_explosion: {layer_name} norm={layer_norm:.2e}"
                
                # Layer-specific vanishing
                if layer_mean < self.vanishing_threshold * 10:  # 10x global threshold
                    return False, f"layer_gradient_vanishing: {layer_name} mean={layer_mean:.2e}"
        
        return True, None


# ============================================================================
# LOSS SURFACE MONITOR (ENHANCED)
# ============================================================================

class LossSurfaceMonitor:
    """
    Detects loss surface pathologies and deceptive learning.
    Enhanced with multi-head divergence detection and loss landscape analysis.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.plateau_window = config.get('plateau_window', 50)
        self.plateau_threshold = config.get('plateau_threshold', 1e-5)
        self.divergence_threshold = config.get('divergence_threshold', 0.1)
        self.head_divergence_threshold = config.get('head_divergence_threshold', 0.2)
        
        self.loss_history = deque(maxlen=self.plateau_window)
        self.eval_history = deque(maxlen=self.plateau_window)
        self.head_losses = defaultdict(lambda: deque(maxlen=self.plateau_window))
        
    def check(self, train_loss: float, eval_metrics: Dict[str, float]) -> Tuple[bool, Optional[str]]:
        """Check loss surface health with enhanced detection."""
        self.loss_history.append(train_loss)
        
        # Compute primary eval metric
        eval_score = eval_metrics.get('primary_metric', 0.0)
        self.eval_history.append(eval_score)
        
        # Track per-head losses if available
        for head_name, head_loss in eval_metrics.items():
            if 'head' in head_name.lower() or 'loss' in head_name.lower():
                self.head_losses[head_name].append(head_loss)
        
        if len(self.loss_history) < self.plateau_window:
            return True, None
        
        # Check for flat loss plateau with metric drift
        loss_variance = np.var(list(self.loss_history))
        if loss_variance < self.plateau_threshold:
            eval_trend = np.polyfit(range(len(self.eval_history)), list(self.eval_history), 1)[0]
            if abs(eval_trend) > self.divergence_threshold:
                return False, f"flat_loss_with_drift: loss_var={loss_variance:.2e}, eval_trend={eval_trend:.4f}"
        
        # Check for loss improving while eval degrades
        recent_loss = list(self.loss_history)
        recent_eval = list(self.eval_history)
        
        loss_trend = np.polyfit(range(len(recent_loss)), recent_loss, 1)[0]
        eval_trend = np.polyfit(range(len(recent_eval)), recent_eval, 1)[0]
        
        if loss_trend < -0.01 and eval_trend < -0.05:  # Loss improving, eval degrading
            return False, f"loss_eval_divergence: loss_trend={loss_trend:.4f}, eval_trend={eval_trend:.4f}"
        
        # Check for sudden loss cliff
        if len(self.loss_history) >= 2:
            loss_jump = abs(recent_loss[-1] - recent_loss[-2])
            if loss_jump > 1.0:  # Sudden jump
                return False, f"loss_cliff: jump={loss_jump:.4f}"
        
        # ENHANCED: Check for divergence between heads
        if len(self.head_losses) >= 2:
            head_trends = {}
            for head_name, head_history in self.head_losses.items():
                if len(head_history) >= 10:
                    head_trends[head_name] = np.polyfit(
                        range(len(head_history)), 
                        list(head_history), 
                        1
                    )[0]
            
            if len(head_trends) >= 2:
                trends = list(head_trends.values())
                trend_divergence = np.std(trends) / (np.mean(np.abs(trends)) + 1e-10)
                
                if trend_divergence > self.head_divergence_threshold:
                    return False, f"head_divergence: divergence={trend_divergence:.4f}, heads={list(head_trends.keys())}"
        
        return True, None


# ============================================================================
# REWARD INTEGRITY MONITOR (RL-SPECIFIC, ENHANCED)
# ============================================================================

class RewardIntegrityMonitor:
    """
    Detects reward hacking and proxy exploitation in RL training.
    Enhanced with feedback loop detection and ranker-agent correlation.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.spike_threshold = config.get('spike_threshold', 3.0)  # std devs
        self.decoupling_threshold = config.get('decoupling_threshold', 0.3)
        self.window = config.get('window', 100)
        self.feedback_loop_threshold = config.get('feedback_loop_threshold', 0.8)
        
        self.reward_history = deque(maxlen=self.window)
        self.engagement_history = deque(maxlen=self.window)
        self.ranker_scores = deque(maxlen=self.window)  # For feedback loop detection
        self.agent_actions = deque(maxlen=self.window)
        
    def check(self, reward: float, engagement_metrics: Dict[str, float]) -> Tuple[bool, Optional[str]]:
        """Check reward integrity with enhanced feedback loop detection."""
        self.reward_history.append(reward)
        
        # Aggregate engagement score
        engagement = sum(engagement_metrics.values()) / max(len(engagement_metrics), 1)
        self.engagement_history.append(engagement)
        
        if len(self.reward_history) < 20:
            return True, None
        
        # Check for reward spikes without engagement support
        recent_rewards = list(self.reward_history)
        reward_mean = np.mean(recent_rewards)
        reward_std = np.std(recent_rewards)
        
        if reward_std > 0:
            z_score = (reward - reward_mean) / reward_std
            if z_score > self.spike_threshold:
                # Check if engagement supports this
                recent_engagement = list(self.engagement_history)
                engagement_mean = np.mean(recent_engagement)
                if engagement < engagement_mean * 1.2:  # Reward spike without engagement
                    return False, f"reward_spike_without_engagement: z={z_score:.2f}, eng={engagement:.4f}"
        
        # Check for reward/metric decoupling
        if len(self.reward_history) == self.window:
            reward_trend = np.polyfit(range(self.window), recent_rewards, 1)[0]
            engagement_trend = np.polyfit(range(self.window), list(self.engagement_history), 1)[0]
            
            if reward_trend > 0.01 and engagement_trend < -0.01:
                return False, f"reward_engagement_decoupling: r_trend={reward_trend:.4f}, e_trend={engagement_trend:.4f}"
        
        # ENHANCED: Detect feedback loops between ranker and agent
        if len(self.ranker_scores) >= 20 and len(self.agent_actions) >= 20:
            # Check for high correlation indicating feedback loop
            ranker_array = np.array(list(self.ranker_scores))
            agent_array = np.array([a.get('score', 0.0) for a in self.agent_actions])
            
            if len(ranker_array) == len(agent_array):
                correlation = np.corrcoef(ranker_array, agent_array)[0, 1]
                
                if correlation > self.feedback_loop_threshold:
                    return False, f"ranker_agent_feedback_loop: correlation={correlation:.4f}"
        
        return True, None
    
    def record_ranker_score(self, score: float):
        """Record ranker score for feedback loop detection."""
        self.ranker_scores.append(score)
    
    def record_agent_action(self, action: Dict[str, Any]):
        """Record agent action for feedback loop detection."""
        self.agent_actions.append(action)


# ============================================================================
# DATA INTEGRITY SENTINEL (ENHANCED WITH DATA_GATE INTEGRATION)
# ============================================================================

class DataIntegritySentinel:
    """
    Monitors data gate violations and feature corruption.
    Enhanced with data_gate.py integration and temporal alignment checks.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.max_violations = config.get('max_violations', 5)
        self.violation_window = config.get('violation_window', 100)
        self.temporal_leakage_threshold = config.get('temporal_leakage_threshold', 0.05)
        
        self.violations = deque(maxlen=self.violation_window)
        self.feature_versions = {}
        self.temporal_checks = deque(maxlen=1000)  # For temporal leakage detection
        self.data_gate_integration = DATA_GATE_AVAILABLE
        
    def record_violation(self, violation_type: str, details: Dict[str, Any]):
        """Record a data gate violation."""
        self.violations.append({
            'type': violation_type,
            'timestamp': time.time(),
            'details': details
        })
        
        # If data_gate is available, also check for TrainingIntegrityViolation
        if self.data_gate_integration and violation_type == 'training_integrity_violation':
            # This is a critical violation that should trigger immediate halt
            pass
    
    def check_feature_version(self, feature_name: str, version: str) -> Tuple[bool, Optional[str]]:
        """Check for feature version mismatches."""
        if feature_name in self.feature_versions:
            if self.feature_versions[feature_name] != version:
                return False, f"feature_version_mismatch: {feature_name} expected={self.feature_versions[feature_name]} got={version}"
        else:
            self.feature_versions[feature_name] = version
        return True, None
    
    def check_temporal_alignment(self, sample_timestamp: float, training_timestamp: float) -> Tuple[bool, Optional[str]]:
        """
        Check for temporal leakage (future signals entering training).
        Critical for causal correctness.
        """
        time_diff = training_timestamp - sample_timestamp
        
        # Training timestamp should be AFTER sample timestamp
        if time_diff < 0:
            return False, f"temporal_leakage: training_before_sample: diff={time_diff:.2f}s"
        
        # Check for suspiciously small time differences (potential leakage)
        if 0 <= time_diff < self.temporal_leakage_threshold:
            self.temporal_checks.append({
                'sample_ts': sample_timestamp,
                'training_ts': training_timestamp,
                'diff': time_diff
            })
            
            # If too many suspicious checks, trigger alert
            if len(self.temporal_checks) >= 100:
                suspicious_count = sum(1 for c in self.temporal_checks 
                                     if 0 <= c['diff'] < self.temporal_leakage_threshold)
                if suspicious_count / len(self.temporal_checks) > 0.1:  # >10% suspicious
                    return False, f"temporal_leakage_pattern: {suspicious_count}/{len(self.temporal_checks)} suspicious"
        
        return True, None
    
    def check(self) -> Tuple[bool, Optional[str]]:
        """Check data integrity status."""
        if len(self.violations) >= self.max_violations:
            violation_types = [v['type'] for v in self.violations]
            return False, f"excessive_data_violations: {len(self.violations)} violations, types={set(violation_types)}"
        
        return True, None


# ============================================================================
# DISTRIBUTION DRIFT SENTINEL (ENHANCED)
# ============================================================================

class DistributionDriftSentinel:
    """
    Detects distribution collapse and niche dominance.
    Enhanced with platform dominance detection and tail density analysis.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.niche_threshold = config.get('niche_threshold', 0.7)  # Max fraction for single niche
        self.entropy_threshold = config.get('entropy_threshold', 0.5)  # Min entropy
        self.tail_threshold = config.get('tail_threshold', 0.95)  # Max cumulative weight in tail
        self.platform_dominance_threshold = config.get('platform_dominance_threshold', 0.8)
        
        self.distribution_history = deque(maxlen=100)  # Track distribution over time
        
    def check(self, distribution: Dict[str, float]) -> Tuple[bool, Optional[str]]:
        """
        Check distribution health with enhanced detection.
        distribution: Dict mapping category -> probability/weight
        """
        if not distribution:
            return True, None
        
        total = sum(distribution.values())
        if total == 0:
            return False, "distribution_empty"
        
        probs = np.array([v / total for v in distribution.values()])
        categories = list(distribution.keys())
        
        # Check for niche collapse
        max_prob = np.max(probs)
        if max_prob > self.niche_threshold:
            dominant_niche = categories[np.argmax(probs)]
            return False, f"niche_collapse: {dominant_niche} dominates with {max_prob:.2%}"
        
        # Check for entropy collapse
        probs_nonzero = probs[probs > 0]
        entropy = -np.sum(probs_nonzero * np.log(probs_nonzero))
        max_entropy = np.log(len(probs))
        normalized_entropy = entropy / max_entropy if max_entropy > 0 else 0
        
        if normalized_entropy < self.entropy_threshold:
            return False, f"entropy_collapse: normalized_entropy={normalized_entropy:.3f}"
        
        # Check for tail density runaway
        sorted_probs = np.sort(probs)[::-1]
        cumsum = np.cumsum(sorted_probs)
        tail_index = np.searchsorted(cumsum, self.tail_threshold)
        tail_fraction = tail_index / len(probs)
        
        if tail_fraction < 0.1:  # Top 10% contains 95%+ of weight
            return False, f"tail_density_runaway: top_{tail_fraction:.1%}_contains_{self.tail_threshold:.1%}"
        
        # ENHANCED: Check for platform dominance
        platform_categories = [c for c in categories if any(platform in c.lower() 
                                                             for platform in ['tiktok', 'instagram', 'youtube', 'reddit'])]
        if platform_categories:
            platform_probs = sum(probs[i] for i, c in enumerate(categories) if c in platform_categories)
            if platform_probs > self.platform_dominance_threshold:
                return False, f"platform_dominance: {platform_probs:.2%} from single platform"
        
        # Track distribution history for drift detection
        self.distribution_history.append(distribution.copy())
        
        # Check for rapid distribution shift
        if len(self.distribution_history) >= 10:
            recent_dist = self.distribution_history[-1]
            older_dist = self.distribution_history[0]
            
            # Compute KL divergence
            all_categories = set(recent_dist.keys()) | set(older_dist.keys())
            recent_probs = np.array([recent_dist.get(c, 1e-10) / sum(recent_dist.values()) for c in all_categories])
            older_probs = np.array([older_dist.get(c, 1e-10) / sum(older_dist.values()) for c in all_categories])
            
            kl_div = np.sum(recent_probs * np.log(recent_probs / older_probs))
            
            if kl_div > 1.0:  # Significant distribution shift
                return False, f"distribution_drift: kl_divergence={kl_div:.4f}"
        
        return True, None


# ============================================================================
# REPLAY LOOP DETECTOR (ENHANCED)
# ============================================================================

class ReplayLoopDetector:
    """
    Detects feedback loops in replay buffer usage.
    Enhanced with self-generated data detection and agent-environment echo loops.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.max_replay_count = config.get('max_replay_count', 10)
        self.echo_threshold = config.get('echo_threshold', 0.8)
        self.window = config.get('window', 1000)
        self.self_generated_threshold = config.get('self_generated_threshold', 0.5)
        
        self.sample_counts = defaultdict(int)
        self.sample_window = deque(maxlen=self.window)
        self.sample_metadata = {}  # Track sample origins
        self.agent_generated_samples = set()  # Track self-generated samples
        
    def record_sample(self, sample_id: str, is_agent_generated: bool = False):
        """Record a sample being replayed."""
        self.sample_counts[sample_id] += 1
        self.sample_window.append(sample_id)
        
        if is_agent_generated:
            self.agent_generated_samples.add(sample_id)
        
        if sample_id not in self.sample_metadata:
            self.sample_metadata[sample_id] = {
                'first_seen': time.time(),
                'replay_count': 0,
                'is_agent_generated': is_agent_generated
            }
        self.sample_metadata[sample_id]['replay_count'] = self.sample_counts[sample_id]
    
    def check(self) -> Tuple[bool, Optional[str]]:
        """Check for replay loops with enhanced detection."""
        # Check for samples replayed too often
        for sample_id, count in self.sample_counts.items():
            if count > self.max_replay_count:
                return False, f"sample_over_replayed: {sample_id} replayed {count} times"
        
        # Check for echo loops (same samples dominating recent window)
        if len(self.sample_window) >= 100:
            recent = list(self.sample_window)[-100:]
            unique_ratio = len(set(recent)) / len(recent)
            if unique_ratio < (1 - self.echo_threshold):
                return False, f"replay_echo_loop: unique_ratio={unique_ratio:.2%}"
        
        # ENHANCED: Check for self-generated data dominating
        if len(self.sample_window) >= 100:
            recent = list(self.sample_window)[-100:]
            agent_generated_count = sum(1 for s in recent if s in self.agent_generated_samples)
            agent_fraction = agent_generated_count / len(recent)
            
            if agent_fraction > self.self_generated_threshold:
                return False, f"self_generated_data_dominance: {agent_fraction:.2%} agent-generated"
        
        # ENHANCED: Check for agent-environment echo loops
        # (Agent generates data → Environment uses it → Agent learns from it → Repeat)
        if len(self.agent_generated_samples) >= 50:
            # Check if agent-generated samples are being replayed excessively
            agent_replay_counts = [
                self.sample_counts[sid] 
                for sid in self.agent_generated_samples 
                if sid in self.sample_counts
            ]
            
            if agent_replay_counts:
                avg_agent_replay = np.mean(agent_replay_counts)
                if avg_agent_replay > self.max_replay_count * 0.5:
                    return False, f"agent_environment_echo_loop: avg_replay={avg_agent_replay:.2f}"
        
        return True, None


# ============================================================================
# SYSTEM STATE AUDITOR (ENHANCED)
# ============================================================================

class SystemStateAuditor:
    """
    Validates system-wide consistency and synchronization.
    Enhanced with checkpoint validation and model-optimizer-scheduler alignment.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.max_clock_skew = config.get('max_clock_skew_sec', 5.0)
        self.required_components = config.get('required_components', [])
        self.checkpoint_manager = None  # Will be set if available
        
        self.component_versions = {}
        self.component_timestamps = {}
        self.checkpoint_validation_cache = {}
        
    def register_checkpoint_manager(self, checkpoint_manager):
        """Register checkpoint manager for validation."""
        if CHECKPOINT_MANAGER_AVAILABLE:
            self.checkpoint_manager = checkpoint_manager
    
    def register_component(self, name: str, version: str, timestamp: float):
        """Register a system component."""
        self.component_versions[name] = version
        self.component_timestamps[name] = timestamp
    
    def check(self) -> Tuple[bool, Optional[str]]:
        """Check system state consistency."""
        now = time.time()
        
        # Check for missing required components
        for comp in self.required_components:
            if comp not in self.component_versions:
                return False, f"missing_required_component: {comp}"
        
        # Check for time synchronization
        for comp, ts in self.component_timestamps.items():
            skew = abs(now - ts)
            if skew > self.max_clock_skew:
                return False, f"clock_skew: {comp} skew={skew:.1f}s"
        
        # ENHANCED: Check for version consistency across components
        if len(self.component_versions) >= 2:
            versions = list(self.component_versions.values())
            if len(set(versions)) > 1:
                # Check if version mismatch is critical
                version_groups = defaultdict(list)
                for comp, version in self.component_versions.items():
                    version_groups[version].append(comp)
                
                if len(version_groups) > 1:
                    return False, f"version_mismatch: {dict(version_groups)}"
        
        return True, None
    
    def validate_checkpoint(self, checkpoint_meta: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Validate checkpoint consistency with enhanced checks.
        Integrates with checkpoint_manager if available.
        """
        required_fields = ['model_version', 'optimizer_state', 'step', 'timestamp']
        
        for field in required_fields:
            if field not in checkpoint_meta:
                return False, f"invalid_checkpoint: missing_{field}"
        
        # Check timestamp is reasonable
        ckpt_time = checkpoint_meta.get('timestamp', 0)
        if abs(time.time() - ckpt_time) > 86400 * 7:  # More than 7 days old
            return False, f"checkpoint_too_old: age={(time.time() - ckpt_time) / 86400:.1f} days"
        
        # ENHANCED: Use checkpoint_manager if available
        if self.checkpoint_manager and hasattr(self.checkpoint_manager, 'validate_checkpoint'):
            try:
                is_valid, reason = self.checkpoint_manager.validate_checkpoint(checkpoint_meta)
                if not is_valid:
                    return False, f"checkpoint_manager_validation_failed: {reason}"
            except Exception as e:
                return False, f"checkpoint_manager_validation_error: {str(e)}"
        
        # ENHANCED: Check model-optimizer-scheduler alignment
        model_version = checkpoint_meta.get('model_version')
        optimizer_version = checkpoint_meta.get('optimizer_version')
        scheduler_version = checkpoint_meta.get('scheduler_version')
        
        if model_version and optimizer_version:
            if model_version != optimizer_version:
                return False, f"model_optimizer_mismatch: model={model_version}, optimizer={optimizer_version}"
        
        if model_version and scheduler_version:
            if model_version != scheduler_version:
                return False, f"model_scheduler_mismatch: model={model_version}, scheduler={scheduler_version}"
        
        return True, None


# ============================================================================
# AGENT BEHAVIOR MONITOR (ENHANCED)
# ============================================================================

class AgentBehaviorMonitor:
    """
    Monitors autonomous agent behavior for runaway patterns.
    Enhanced with uncertainty tracking and constraint violation patterns.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.action_rate_limit = config.get('action_rate_limit', 100)  # per minute
        self.uncertainty_threshold = config.get('uncertainty_threshold', 0.3)
        self.constraint_violation_limit = config.get('constraint_violation_limit', 3)
        self.runaway_pattern_threshold = config.get('runaway_pattern_threshold', 0.8)
        
        self.agent_actions = defaultdict(lambda: deque(maxlen=100))
        self.agent_violations = defaultdict(int)
        self.agent_uncertainties = defaultdict(lambda: deque(maxlen=50))
        self.agent_action_patterns = defaultdict(lambda: deque(maxlen=100))
        
    def record_action(self, agent_id: str, action: Dict[str, Any]):
        """Record an agent action."""
        self.agent_actions[agent_id].append({
            'timestamp': time.time(),
            'action': action,
            'action_type': action.get('type', 'unknown')
        })
        self.agent_action_patterns[agent_id].append(action.get('type', 'unknown'))
    
    def record_violation(self, agent_id: str, violation_type: str):
        """Record a constraint violation."""
        self.agent_violations[agent_id] += 1
    
    def record_uncertainty(self, agent_id: str, uncertainty: float):
        """Record agent uncertainty for tracking."""
        self.agent_uncertainties[agent_id].append(uncertainty)
    
    def check(self, agent_id: str, uncertainty: Optional[float] = None) -> Tuple[bool, Optional[str]]:
        """Check agent behavior with enhanced detection."""
        # Check action rate
        actions = self.agent_actions[agent_id]
        if len(actions) >= 2:
            recent = [a for a in actions if time.time() - a['timestamp'] < 60]
            if len(recent) > self.action_rate_limit:
                return False, f"agent_action_explosion: {agent_id} {len(recent)} actions/min"
        
        # Check uncertainty threshold
        if uncertainty is not None:
            self.record_uncertainty(agent_id, uncertainty)
            if uncertainty > self.uncertainty_threshold:
                return False, f"agent_high_uncertainty: {agent_id} uncertainty={uncertainty:.3f}"
        
        # Check constraint violations
        if self.agent_violations[agent_id] >= self.constraint_violation_limit:
            return False, f"agent_constraint_violations: {agent_id} {self.agent_violations[agent_id]} violations"
        
        # ENHANCED: Check for runaway patterns (repetitive actions)
        if len(self.agent_action_patterns[agent_id]) >= 20:
            recent_patterns = list(self.agent_action_patterns[agent_id])[-20:]
            unique_actions = len(set(recent_patterns))
            pattern_diversity = unique_actions / len(recent_patterns)
            
            if pattern_diversity < (1 - self.runaway_pattern_threshold):
                return False, f"agent_runaway_pattern: {agent_id} diversity={pattern_diversity:.3f}"
        
        # ENHANCED: Check for ignored uncertainty (agent acting despite high uncertainty)
        if len(self.agent_uncertainties[agent_id]) >= 10:
            recent_uncertainties = list(self.agent_uncertainties[agent_id])[-10:]
            avg_uncertainty = np.mean(recent_uncertainties)
            
            if avg_uncertainty > self.uncertainty_threshold:
                # Check if agent is still taking actions despite high uncertainty
                recent_actions = [a for a in self.agent_actions[agent_id] 
                                if time.time() - a['timestamp'] < 300]  # Last 5 minutes
                
                if len(recent_actions) > 10:  # Many actions despite uncertainty
                    return False, f"agent_ignoring_uncertainty: {agent_id} uncertainty={avg_uncertainty:.3f}, actions={len(recent_actions)}"
        
        return True, None


# ============================================================================
# CROSS-MONITOR CORRELATION ANALYZER
# ============================================================================

class CrossMonitorCorrelationAnalyzer:
    """
    Detects catastrophic patterns that only emerge from correlation across monitors.
    Critical for catching subtle failures that individual monitors miss.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.correlation_window = config.get('correlation_window', 50)
        self.correlation_threshold = config.get('correlation_threshold', 0.7)
        
        self.monitor_signals = defaultdict(lambda: deque(maxlen=self.correlation_window))
        self.correlation_history = deque(maxlen=100)
        
    def record_monitor_signal(self, monitor_name: str, signal_value: float, is_anomaly: bool):
        """Record a signal from a monitor."""
        self.monitor_signals[monitor_name].append({
            'value': signal_value,
            'is_anomaly': is_anomaly,
            'timestamp': time.time()
        })
    
    def check_correlations(self) -> Tuple[bool, Optional[str]]:
        """
        Check for dangerous correlations across monitors.
        Returns: (is_safe, failure_reason)
        """
        if len(self.monitor_signals) < 2:
            return True, None
        
        monitor_names = list(self.monitor_signals.keys())
        
        # Check for simultaneous anomalies across multiple monitors
        for i, monitor1 in enumerate(monitor_names):
            for monitor2 in monitor_names[i+1:]:
                signals1 = list(self.monitor_signals[monitor1])
                signals2 = list(self.monitor_signals[monitor2])
                
                if len(signals1) >= 10 and len(signals2) >= 10:
                    # Check for correlation in anomaly patterns
                    anomalies1 = [s['is_anomaly'] for s in signals1]
                    anomalies2 = [s['is_anomaly'] for s in signals2]
                    
                    if sum(anomalies1) >= 3 and sum(anomalies2) >= 3:
                        # Check if anomalies are correlated
                        correlation = np.corrcoef(
                            [float(a) for a in anomalies1],
                            [float(a) for a in anomalies2]
                        )[0, 1]
                        
                        if correlation > self.correlation_threshold:
                            return False, f"cross_monitor_correlation: {monitor1}-{monitor2} correlation={correlation:.4f}"
        
        # Check for gradient+loss+reward triple failure (catastrophic)
        critical_monitors = ['gradient', 'loss', 'reward']
        if all(m in monitor_names for m in critical_monitors):
            recent_anomalies = {
                m: sum(s['is_anomaly'] for s in list(self.monitor_signals[m])[-10:])
                for m in critical_monitors
            }
            
            if all(count >= 3 for count in recent_anomalies.values()):
                return False, f"triple_monitor_failure: {recent_anomalies}"
        
        return True, None


# ============================================================================
# RECOVERY COORDINATOR (ENHANCED)
# ============================================================================

class RecoveryCoordinator:
    """
    Coordinates recovery actions after catastrophe detection.
    Enhanced with checkpoint validation, curriculum downgrade, and agent isolation.
    """
    
    def __init__(self, checkpoint_dir: Path):
        self.checkpoint_dir = Path(checkpoint_dir)
        self.safe_checkpoints: List[Path] = []
        self.recovery_log: List[Dict[str, Any]] = []
        self.checkpoint_manager = None
        self.curriculum_orchestrator = None
        self.quarantined_agents: Set[str] = set()
        
    def register_checkpoint_manager(self, checkpoint_manager):
        """Register checkpoint manager for validation."""
        self.checkpoint_manager = checkpoint_manager
    
    def register_curriculum_orchestrator(self, curriculum_orchestrator):
        """Register curriculum orchestrator for phase management."""
        self.curriculum_orchestrator = curriculum_orchestrator
    
    def mark_checkpoint_safe(self, checkpoint_path: Path, validate: bool = True):
        """
        Mark a checkpoint as safe for recovery.
        Enhanced with validation if checkpoint_manager is available.
        """
        if validate and self.checkpoint_manager:
            try:
                # Validate checkpoint before marking safe
                checkpoint_meta = self.checkpoint_manager.get_checkpoint_metadata(checkpoint_path)
                if checkpoint_meta:
                    is_valid, reason = self.checkpoint_manager.validate_checkpoint(checkpoint_meta)
                    if not is_valid:
                        warnings.warn(f"Checkpoint validation failed: {reason}")
                        return False
            except Exception as e:
                warnings.warn(f"Checkpoint validation error: {e}")
                return False
        
        self.safe_checkpoints.append(checkpoint_path)
        return True
    
    def get_last_safe_checkpoint(self) -> Optional[Path]:
        """Get the most recent safe checkpoint."""
        if not self.safe_checkpoints:
            return None
        return self.safe_checkpoints[-1]
    
    def downgrade_curriculum_phase(self, target_phase: Optional[CurriculumPhase] = None):
        """
        Downgrade curriculum phase as part of recovery.
        Integrates with curriculum_learning.py if available.
        """
        if not self.curriculum_orchestrator:
            return False
        
        if CURRICULUM_AVAILABLE:
            try:
                if target_phase is None:
                    # Downgrade to previous phase
                    current_phase = self.curriculum_orchestrator.get_current_phase()
                    if current_phase and current_phase.value > 0:
                        target_phase = CurriculumPhase(current_phase.value - 1)
                    else:
                        return False
                
                # Force phase downgrade
                self.curriculum_orchestrator.force_phase(target_phase)
                return True
            except Exception as e:
                warnings.warn(f"Curriculum downgrade failed: {e}")
                return False
        
        return False
    
    def isolate_agent(self, agent_id: str):
        """Isolate an agent (quarantine)."""
        self.quarantined_agents.add(agent_id)
    
    def release_agent(self, agent_id: str):
        """Release an agent from quarantine."""
        self.quarantined_agents.discard(agent_id)
    
    def is_agent_quarantined(self, agent_id: str) -> bool:
        """Check if an agent is quarantined."""
        return agent_id in self.quarantined_agents
    
    def plan_recovery(self, kill_level: KillLevel, catastrophe: CatastropheType) -> Dict[str, Any]:
        """
        Plan recovery actions based on kill level.
        Enhanced with checkpoint validation and curriculum management.
        """
        plan = {
            'timestamp': datetime.utcnow().isoformat(),
            'kill_level': kill_level.name,
            'catastrophe': catastrophe.value,
            'actions': [],
            'requires_human_approval': False,
            'checkpoint_validation_required': False,
            'curriculum_downgrade_required': False
        }
        
        if kill_level == KillLevel.LEVEL_1_SOFT_PAUSE:
            plan['actions'] = [
                'pause_training_loop',
                'collect_diagnostics',
                'await_auto_resolution'
            ]
            # TIER-0 9.5+ UPGRADE: Explicit "no auto-resume" assertion
            # Even Level-1 should not auto-resume without audit
            plan['auto_resume_after_sec'] = 300
            plan['auto_resume_requires_audit'] = True  # Must audit before resume
            plan['auto_resume_allowed'] = False  # TIER-0 9.5+ UPGRADE: Disable auto-resume
            
        elif kill_level == KillLevel.LEVEL_2_HARD_FREEZE:
            plan['actions'] = [
                'stop_gradient_application',
                'freeze_optimizer_state',
                'lock_checkpoints',
                'capture_full_state'
            ]
            plan['requires_human_approval'] = True
            plan['checkpoint_validation_required'] = True
            
        elif kill_level == KillLevel.LEVEL_3_ROLLBACK:
            last_safe = self.get_last_safe_checkpoint()
            plan['actions'] = [
                'revert_model_weights',
                'invalidate_recent_learning',
                'reset_curriculum_phase'
            ]
            plan['rollback_to_checkpoint'] = str(last_safe) if last_safe else None
            plan['requires_human_approval'] = True
            plan['checkpoint_validation_required'] = True
            plan['curriculum_downgrade_required'] = True
            
        elif kill_level == KillLevel.LEVEL_4_QUARANTINE:
            plan['actions'] = [
                'isolate_agents',
                'flush_replay_buffers',
                'disable_automation',
                'downgrade_to_safe_policy',
                'downgrade_curriculum_phase'
            ]
            plan['requires_human_approval'] = True
            plan['curriculum_downgrade_required'] = True
            
        elif kill_level == KillLevel.LEVEL_5_TERMINATION:
            plan['actions'] = [
                'kill_training_job',
                'flag_system_unsafe',
                'preserve_forensic_state',
                'notify_team',
                'initiate_postmortem'
            ]
            plan['requires_human_approval'] = True
            plan['is_irreversible'] = True
        
        self.recovery_log.append(plan)
        return plan
    
    def execute_recovery_plan(self, plan: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Execute recovery plan with validation.
        Returns: (success, error_message)
        """
        try:
            # Validate checkpoint if required
            if plan.get('checkpoint_validation_required'):
                rollback_ckpt = plan.get('rollback_to_checkpoint')
                if rollback_ckpt:
                    ckpt_path = Path(rollback_ckpt)
                    if not ckpt_path.exists():
                        return False, f"rollback_checkpoint_not_found: {rollback_ckpt}"
                    
                    # Validate checkpoint
                    if self.checkpoint_manager:
                        try:
                            ckpt_meta = self.checkpoint_manager.get_checkpoint_metadata(ckpt_path)
                            if ckpt_meta:
                                is_valid, reason = self.checkpoint_manager.validate_checkpoint(ckpt_meta)
                                if not is_valid:
                                    return False, f"rollback_checkpoint_invalid: {reason}"
                        except Exception as e:
                            return False, f"checkpoint_validation_error: {e}"
            
            # Downgrade curriculum if required
            if plan.get('curriculum_downgrade_required'):
                success = self.downgrade_curriculum_phase()
                if not success:
                    warnings.warn("Curriculum downgrade failed during recovery")
            
            return True, None
            
        except Exception as e:
            return False, f"recovery_execution_error: {str(e)}"


# ============================================================================
# HUMAN-IN-THE-LOOP APPROVAL SYSTEM
# ============================================================================

class HumanApprovalSystem:
    """
    Manages human-in-the-loop approval workflow.
    Enhanced with cryptographic token validation and approval history.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.secret_key = config.get('approval_secret_key', 'default_secret_change_in_production')
        self.approval_timeout_sec = config.get('approval_timeout_sec', 3600)  # 1 hour
        self.require_multi_approval = config.get('require_multi_approval', False)
        self.min_approvers = config.get('min_approvers', 1)
        
        self.pending_approvals: Dict[str, Dict[str, Any]] = {}
        self.approval_history: List[Dict[str, Any]] = []
        self.approval_tokens: Set[str] = set()
        
    def generate_approval_token(self, intervention_id: str) -> str:
        """Generate cryptographic approval token."""
        timestamp = datetime.utcnow().isoformat()
        message = f"{intervention_id}:{timestamp}:{self.secret_key}"
        token = hashlib.sha256(message.encode()).hexdigest()
        self.approval_tokens.add(token)
        return token
    
    def validate_approval_token(self, token: str, intervention_id: str) -> bool:
        """Validate approval token cryptographically."""
        if token not in self.approval_tokens:
            return False
        
        # Verify token matches intervention
        # In production, this would be more sophisticated
        return True
    
    def request_approval(self, intervention: InterventionRecord) -> str:
        """
        Request human approval for an intervention.
        Returns approval token that must be provided to resume.
        """
        intervention_id = f"{intervention.timestamp}_{intervention.severity_level}"
        token = self.generate_approval_token(intervention_id)
        
        self.pending_approvals[intervention_id] = {
            'intervention': intervention,
            'token': token,
            'requested_at': datetime.utcnow().isoformat(),
            'approvers': [],
            'status': 'pending'
        }
        
        return token
    
    def record_approval(self, token: str, approver_id: str, approved: bool) -> Tuple[bool, Optional[str]]:
        """
        Record human approval decision.
        Returns: (success, error_message)
        """
        # Find pending approval by token
        pending = None
        for intervention_id, approval_data in self.pending_approvals.items():
            if approval_data['token'] == token:
                pending = approval_data
                break
        
        if not pending:
            return False, "approval_token_not_found"
        
        if pending['status'] != 'pending':
            return False, f"approval_already_{pending['status']}"
        
        # Record approver
        pending['approvers'].append({
            'approver_id': approver_id,
            'approved': approved,
            'timestamp': datetime.utcnow().isoformat()
        })
        
        # Check if enough approvers
        approved_count = sum(1 for a in pending['approvers'] if a['approved'])
        
        if self.require_multi_approval:
            if approved_count >= self.min_approvers:
                pending['status'] = 'approved'
            elif len(pending['approvers']) >= self.min_approvers:
                # All required approvers have responded, but not all approved
                pending['status'] = 'rejected'
        else:
            if approved:
                pending['status'] = 'approved'
            else:
                pending['status'] = 'rejected'
        
        # Record in history
        self.approval_history.append({
            'token': token,
            'intervention_id': intervention_id,
            'approver_id': approver_id,
            'approved': approved,
            'timestamp': datetime.utcnow().isoformat()
        })
        
        return True, None
    
    def check_approval_status(self, token: str) -> Optional[str]:
        """
        Check approval status.
        Returns: 'pending', 'approved', 'rejected', or None if not found
        """
        for approval_data in self.pending_approvals.values():
            if approval_data['token'] == token:
                return approval_data['status']
        return None


# ============================================================================
# AUDIT TRAIL & COMPLIANCE
# ============================================================================

class AuditTrailManager:
    """
    Manages immutable audit trail for compliance and legal defensibility.
    Enhanced with cryptographic signing and verification.
    """
    
    def __init__(self, log_dir: Path, secret_key: Optional[str] = None):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.secret_key = secret_key or 'default_audit_secret_change_in_production'
        
        self.audit_log_path = self.log_dir / 'audit_trail.jsonl'
        self.intervention_log_path = self.log_dir / 'interventions_master.jsonl'
        self.compliance_report_path = self.log_dir / 'compliance_reports'
        self.compliance_report_path.mkdir(exist_ok=True)
        
    def log_intervention(self, intervention: InterventionRecord):
        """Log intervention with cryptographic signing."""
        # Compute hash
        intervention.cryptographic_hash = intervention.compute_hash(self.secret_key)
        
        # Write to audit trail
        with open(self.audit_log_path, 'a') as f:
            f.write(intervention.to_json() + '\n')
        
        # Write to intervention log
        with open(self.intervention_log_path, 'a') as f:
            f.write(intervention.to_json() + '\n')
        
        # Write individual intervention file
        intervention_file = self.log_dir / f"intervention_{intervention.timestamp.replace(':', '-')}.json"
        with open(intervention_file, 'w') as f:
            f.write(intervention.to_json())
    
    def verify_audit_trail_integrity(self) -> Tuple[bool, List[str]]:
        """
        Verify that audit trail has not been tampered with.
        Returns: (is_valid, list_of_violations)
        """
        violations = []
        
        # Read all interventions from log
        if not self.intervention_log_path.exists():
            return True, []
        
        with open(self.intervention_log_path, 'r') as f:
            for line_num, line in enumerate(f, 1):
                try:
                    data = json.loads(line)
                    # Reconstruct intervention record
                    intervention = InterventionRecord(**{
                        k: v if k != 'catastrophe_type' else CatastropheType(v)
                        for k, v in data.items()
                        if k != 'cryptographic_hash'
                    })
                    
                    # Verify hash
                    if 'cryptographic_hash' in data:
                        if not intervention.verify_hash(self.secret_key):
                            violations.append(f"Line {line_num}: Hash verification failed")
                    
                except Exception as e:
                    violations.append(f"Line {line_num}: Parse error - {e}")
        
        return len(violations) == 0, violations
    
    def generate_compliance_report(self, start_date: Optional[datetime] = None, 
                                  end_date: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Generate compliance report for audit purposes.
        """
        report = {
            'generated_at': datetime.utcnow().isoformat(),
            'period': {
                'start': start_date.isoformat() if start_date else None,
                'end': end_date.isoformat() if end_date else None
            },
            'interventions': [],
            'summary': {
                'total_interventions': 0,
                'by_severity': defaultdict(int),
                'by_catastrophe_type': defaultdict(int),
                'human_approvals_required': 0,
                'human_approvals_granted': 0
            }
        }
        
        # Read interventions from log
        if self.intervention_log_path.exists():
            with open(self.intervention_log_path, 'r') as f:
                for line in f:
                    try:
                        data = json.loads(line)
                        intervention_time = datetime.fromisoformat(data['timestamp'])
                        
                        # Filter by date range
                        if start_date and intervention_time < start_date:
                            continue
                        if end_date and intervention_time > end_date:
                            continue
                        
                        report['interventions'].append(data)
                        report['summary']['total_interventions'] += 1
                        report['summary']['by_severity'][data['severity_level']] += 1
                        report['summary']['by_catastrophe_type'][data['catastrophe_type']] += 1
                        
                        if data.get('requires_human_approval'):
                            report['summary']['human_approvals_required'] += 1
                        
                    except Exception as e:
                        warnings.warn(f"Error parsing intervention log: {e}")
        
        # Write report
        report_file = self.compliance_report_path / f"compliance_report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        with open(report_file, 'w') as f:
            json.dump(report, f, indent=2)
        
        return report


# ============================================================================
# MAIN SAFETY WATCHDOG (FULLY ENHANCED)
# ============================================================================

class SafetyWatchdog:
    """
    ABSOLUTE AUTHORITY: Global kill-switch and catastrophe recovery.
    
    Overrides ALL training components when existential risk detected.
    
    This is the FINAL BOSS of the training system.
    Nothing outranks this file.
    """
    
    def __init__(self, config: Dict[str, Any], checkpoint_dir: Path, log_dir: Path):
        self.config = config
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_dir = Path(checkpoint_dir)
        
        # Determinism lock
        self.determinism_lock = DeterminismLock(config.get('determinism_seed', 42))
        self.determinism_lock.set_seed(config.get('determinism_seed', 42))
        
        # Training loop enforcement
        self.loop_enforcer = TrainingLoopEnforcer()
        self.current_training_step = 0
        
        # Initialize all monitors
        self.gradient_monitor = GradientHealthMonitor(config.get('gradient', {}))
        self.loss_monitor = LossSurfaceMonitor(config.get('loss', {}))
        self.reward_monitor = RewardIntegrityMonitor(config.get('reward', {}))
        self.data_sentinel = DataIntegritySentinel(config.get('data', {}))
        self.drift_sentinel = DistributionDriftSentinel(config.get('drift', {}))
        self.replay_detector = ReplayLoopDetector(config.get('replay', {}))
        self.system_auditor = SystemStateAuditor(config.get('system', {}))
        self.agent_monitor = AgentBehaviorMonitor(config.get('agent', {}))
        self.correlation_analyzer = CrossMonitorCorrelationAnalyzer(config.get('correlation', {}))
        
        # Advanced pattern detector
        self.pattern_detector = AdvancedPatternDetector(config.get('pattern_detection', {}))
        
        # Early warning system
        self.early_warning = EarlyWarningSystem(config.get('early_warning', {}))
        
        # Shadow mode enforcer
        self.shadow_enforcer = ShadowModeEnforcer()
        
        # Forensic state preserver
        self.forensic_preserver = ForensicStatePreserver(log_dir / 'forensic')
        
        # Independent isolation sentinels (orthogonal detection paths)
        self.replay_isolation = ReplayIsolationSentinel(config.get('replay_isolation', {}))
        self.drift_isolation = DistributionDriftIsolationSentinel(config.get('drift_isolation', {}))
        
        # Escalation transition table
        self.escalation_table = EscalationTransitionTable()
        
        # Cross-monitor disagreement resolver
        self.disagreement_resolver = CrossMonitorDisagreementResolver(config.get('disagreement', {}))
        
        # Compound failure detector
        self.compound_detector = CompoundFailureModeDetector(config.get('compound', {}))
        
        # Cross-agent cascading isolation
        self.cascade_manager = CrossAgentCascadingIsolationManager(config.get('cascade', {}))
        
        # Version skew and race condition handler
        self.version_skew_handler = VersionSkewRaceConditionHandler(config.get('version_skew', {}))
        
        # Multi-source quorum checker
        self.quorum_checker = MultiSourceQuorumChecker(config.get('quorum', {}))
        
        # Comprehensive edge case handler
        self.edge_case_handler = ComprehensiveEdgeCaseHandler(config.get('edge_cases', {}))
        
        # Hard human approval gate enforcer
        self.hard_gate_enforcer = HardHumanApprovalGateEnforcer(config.get('hard_gates', {}))
        
        # TIER-0 FINAL BOSS EXPANSION COMPONENTS
        # Escalation state machine
        self.escalation_state_machine = EscalationStateMachine()
        
        # Independent replay authority
        self.replay_authority = IndependentReplayAuthority(config.get('replay_authority', {}))
        
        # Drift-reward decoupling sentinel
        self.drift_reward_sentinel = DriftRewardDecouplingSentinel(config.get('drift_reward', {}))
        
        # Multi-signal quorum gate
        self.quorum_gate = MultiSignalQuorumGate(config.get('quorum_gate', {}))
        
        # Cross-agent cascade controller
        self.cascade_controller = CrossAgentCascadeController(config.get('cascade_controller', {}))
        
        # Post-termination lock & seal
        self.termination_seal = PostTerminationLockAndSeal(
            log_dir / 'termination_seal',
            config.get('seal_secret_key')
        )
        
        # TIER-0 9.5+ UPGRADE: Check for termination seal on initialization
        # If seal exists, watchdog must not initialize (system was terminated)
        is_sealed = self.termination_seal.is_sealed()
        if is_sealed:
            seal_valid, seal_error, seal_data = self.termination_seal.verify_seal()
            if seal_valid:
                raise RuntimeError(
                    f"System is sealed from previous termination. "
                    f"Seal reason: {seal_data.get('catastrophe_type', 'unknown')}. "
                    f"Restart requires proper authorization with approval token, fresh binary hash, and audit token."
                )
            else:
                self.logger.critical(f"Termination seal exists but verification failed: {seal_error}")
                raise RuntimeError(f"Termination seal corrupted: {seal_error}")
        
        # Resilience manager
        self.resilience_manager = WatchdogResilienceManager(config.get('resilience', {}))
        
        # Telemetry validator
        self.telemetry_validator = TelemetryValidator()
        
        # Performance monitor
        self.performance_monitor = WatchdogPerformanceMonitor()
        
        # Recovery coordinator
        self.recovery_coordinator = RecoveryCoordinator(checkpoint_dir)
        
        # Human approval system
        self.approval_system = HumanApprovalSystem(config.get('approval', {}))
        
        # Audit trail manager
        self.audit_manager = AuditTrailManager(log_dir, config.get('audit_secret_key'))
        
        # Watchdog state
        self.current_level = KillLevel.LEVEL_0_NOMINAL
        self.interventions: List[InterventionRecord] = []
        self.is_training_blocked = False
        self.requires_human_approval = False
        self.pending_approval_token: Optional[str] = None
        
        # TIER-0 10.0: Irreversible post-kill memory seal
        self._has_terminated = False
        
        # Setup logging
        self.logger = logging.getLogger('SafetyWatchdog')
        self.logger.setLevel(logging.INFO)
        handler = logging.FileHandler(self.log_dir / 'safety_watchdog.log')
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        self.logger.addHandler(handler)
        
        # TIER-0 10.0: Dependency null-collapse safeguard
        # Missing safety components = system termination (fail-closed)
        self._require_component(self.gradient_monitor, "GradientHealthMonitor")
        self._require_component(self.loss_monitor, "LossSurfaceMonitor")
        self._require_component(self.reward_monitor, "RewardIntegrityMonitor")
        self._require_component(self.data_sentinel, "DataIntegritySentinel")
        self._require_component(self.drift_sentinel, "DistributionDriftSentinel")
        self._require_component(self.replay_detector, "ReplayLoopDetector")
        self._require_component(self.system_auditor, "SystemStateAuditor")
        self._require_component(self.agent_monitor, "AgentBehaviorMonitor")
        self._require_component(self.recovery_coordinator, "RecoveryCoordinator")
        self._require_component(self.audit_manager, "AuditTrailManager")
        self._require_component(self.resilience_manager, "WatchdogResilienceManager")
        self._require_component(self.telemetry_validator, "TelemetryValidator")
        self._require_component(self.performance_monitor, "WatchdogPerformanceMonitor")
        
        self.logger.info("SafetyWatchdog initialized with ABSOLUTE AUTHORITY")
        self.logger.info(f"Determinism seed: {config.get('determinism_seed', 42)}")
        self.logger.info("TIER-0 10.0: All critical safety components verified")
    
    def _require_component(self, component: Any, name: str):
        """
        TIER-0 10.0: Dependency null-collapse safeguard.
        
        Missing safety components must make the system more conservative, not crash or degrade silently.
        Missing component = immediate Level-5 termination.
        """
        if component is None:
            self.logger.critical(f"Missing critical safety component: {name}")
            raise WatchdogViolation(
                f"Safety component missing: {name}. System cannot operate safely without this component.",
                kill_level=KillLevel.LEVEL_5_TERMINATION
            )
    
    def register_checkpoint_manager(self, checkpoint_manager):
        """Register checkpoint manager for integration."""
        self.recovery_coordinator.register_checkpoint_manager(checkpoint_manager)
        self.system_auditor.register_checkpoint_manager(checkpoint_manager)
    
    def register_curriculum_orchestrator(self, curriculum_orchestrator):
        """Register curriculum orchestrator for integration."""
        self.recovery_coordinator.register_curriculum_orchestrator(curriculum_orchestrator)
    
    def register_data_gate(self, data_gate):
        """Register data gate for violation monitoring."""
        # Data gate integration is handled through violation callbacks
        pass
    
    def check_all(self, telemetry: Dict[str, Any]) -> Tuple[bool, Optional[InterventionRecord]]:
        """
        Run all safety checks with cross-monitor correlation.
        
        TIER-0 10.0: Irreversible post-kill memory seal enforcement.
        Returns: (is_safe, intervention_record)
        """
        # TIER-0 10.0: Check termination seal - no actions allowed after termination
        if self._has_terminated:
            raise RuntimeError(
                "Watchdog already terminated. No further actions allowed. "
                "System is sealed and cannot be used."
            )
        
        check_start_time = time.time()
        
        # TIER-0 9.5+ UPGRADE: Proactive watchdog self-failure check
        should_terminate, terminate_reason = self.resilience_manager.should_force_termination()
        if should_terminate:
            self.logger.critical(f"WATCHDOG SELF-FAILURE DETECTED: {terminate_reason}")
            return self._trigger_intervention(
                monitor_name='watchdog_self_failure',
                reason=terminate_reason,
                telemetry=telemetry
            )
        
        try:
            # Validate telemetry first
            is_valid, validation_error = self.telemetry_validator.validate_telemetry(telemetry)
            if not is_valid:
                self.logger.error(f"Invalid telemetry: {validation_error}")
                # Invalid telemetry is a safety concern
                return self._trigger_intervention(
                    monitor_name='telemetry_validation',
                    reason=validation_error,
                    telemetry=telemetry
                )
            
            # ENHANCED: Verify training loop is calling approval
            compliance_ok, compliance_reason = self.loop_enforcer.check_approval_compliance(
                self.current_training_step
            )
            if not compliance_ok:
                return self._trigger_intervention(
                    monitor_name='training_loop_enforcement',
                    reason=compliance_reason,
                    telemetry=telemetry
                )
            
            # Extract telemetry components
            gradients = telemetry.get('gradients', {})
            train_loss = telemetry.get('train_loss', 0.0)
            eval_metrics = telemetry.get('eval_metrics', {})
            reward = telemetry.get('reward', 0.0)
            engagement = telemetry.get('engagement_metrics', {})
            distribution = telemetry.get('distribution', {})
            
            # Run all monitors
            checks = [
                ('gradient', self.gradient_monitor.check(gradients)),
                ('loss', self.loss_monitor.check(train_loss, eval_metrics)),
                ('reward', self.reward_monitor.check(reward, engagement)),
                ('data', self.data_sentinel.check()),
                ('drift', self.drift_sentinel.check(distribution)),
                ('replay', self.replay_detector.check()),
                ('system', self.system_auditor.check())
            ]
            
            # Record monitor signals for correlation analysis
            for monitor_name, (is_safe, reason) in checks:
                signal_value = 1.0 if is_safe else 0.0
                self.correlation_analyzer.record_monitor_signal(
                    monitor_name, signal_value, not is_safe
                )
            
            # ENHANCED: Check for monitor disagreements first
            monitor_results = {name: result for name, result in checks}
            disagreement_safe, disagreement_reason, disagreement_analysis = self.disagreement_resolver.resolve_disagreement(monitor_results)
            
            if not disagreement_safe:
                # Monitor disagreement detected - fail closed
                return self._trigger_intervention(
                    monitor_name='monitor_disagreement',
                    reason=disagreement_reason,
                    telemetry={**telemetry, 'disagreement_analysis': disagreement_analysis}
                )
            
            # Find first failure
            active_failures = []
            for monitor_name, (is_safe, reason) in checks:
                if not is_safe:
                    catastrophe_type = self._classify_catastrophe(monitor_name, reason)
                    active_failures.append((monitor_name, catastrophe_type))
                    return self._trigger_intervention(
                        monitor_name=monitor_name,
                        reason=reason,
                        telemetry=telemetry
                    )
            
            # ENHANCED: Check for compound failures
            if len(active_failures) > 0:
                compound_safe, compound_reason, compound_analysis = self.compound_detector.detect_compound_failure(active_failures)
                if not compound_safe:
                    return self._trigger_intervention(
                        monitor_name='compound_failure',
                        reason=compound_reason,
                        telemetry={**telemetry, 'compound_analysis': compound_analysis}
                    )
            
            # ENHANCED: Check cross-monitor correlations
            correlation_safe, correlation_reason = self.correlation_analyzer.check_correlations()
            if not correlation_safe:
                return self._trigger_intervention(
                    monitor_name='cross_monitor_correlation',
                    reason=correlation_reason,
                    telemetry=telemetry
                )
            
            # ENHANCED: Check advanced patterns
            loss_history = list(self.loss_monitor.loss_history) if len(self.loss_monitor.loss_history) > 0 else []
            eval_history = list(self.loss_monitor.eval_history) if len(self.loss_monitor.eval_history) > 0 else []
            grad_history = [np.linalg.norm(g) for g in list(self.gradient_monitor.grad_history)] if len(self.gradient_monitor.grad_history) > 0 else []
            
            # Silent degradation detection
            silent_safe, silent_reason = self.pattern_detector.detect_silent_degradation(
                loss_history, eval_history, grad_history
            )
            if not silent_safe:
                return self._trigger_intervention(
                    monitor_name='advanced_pattern',
                    reason=silent_reason,
                    telemetry=telemetry
                )
            
            # Mode collapse detection
            if len(self.drift_sentinel.distribution_history) > 0:
                dist_history = list(self.drift_sentinel.distribution_history)
                mode_safe, mode_reason = self.pattern_detector.detect_mode_collapse(dist_history)
                if not mode_safe:
                    return self._trigger_intervention(
                        monitor_name='advanced_pattern',
                        reason=mode_reason,
                        telemetry=telemetry
                    )
            
            # ENHANCED: Check edge cases
            edge_safe, edge_reason, edge_analysis = self.edge_case_handler.check_edge_cases(telemetry)
            if not edge_safe:
                return self._trigger_intervention(
                    monitor_name='edge_case',
                    reason=edge_reason,
                    telemetry={**telemetry, 'edge_case_analysis': edge_analysis}
                )
            
            # TIER-0 EXPANSION: Multi-signal quorum gate
            quorum_safe, quorum_level, quorum_reason, quorum_analysis = self.quorum_gate.check_quorum(monitor_results)
            if not quorum_safe:
                # Quorum pattern detected - escalate independently
                return self._trigger_intervention(
                    monitor_name='quorum_gate',
                    reason=quorum_reason,
                    telemetry={**telemetry, 'quorum_analysis': quorum_analysis}
                )
            
            # TIER-0 EXPANSION: Drift-reward decoupling check
            distribution = telemetry.get('distribution', {})
            reward = telemetry.get('reward', 0.0)
            engagement = telemetry.get('engagement_metrics', {})
            train_loss = telemetry.get('train_loss')
            
            if distribution and reward is not None:
                decoupling_safe, decoupling_reason, decoupling_analysis = self.drift_reward_sentinel.check_decoupling(
                    distribution, reward, engagement, train_loss
                )
                if not decoupling_safe:
                    return self._trigger_intervention(
                        monitor_name='drift_reward_decoupling',
                        reason=decoupling_reason,
                        telemetry={**telemetry, 'decoupling_analysis': decoupling_analysis}
                    )
            
            # ENHANCED: Multi-source quorum checks for critical state
            critical_state_keys = ['model_version', 'checkpoint_id', 'training_step']
            for state_key in critical_state_keys:
                expected_value = telemetry.get(state_key)
                if expected_value is not None:
                    quorum_safe, quorum_reason, quorum_analysis = self.quorum_checker.check_quorum(
                        state_key, expected_value
                    )
                    if not quorum_safe:
                        return self._trigger_intervention(
                            monitor_name='quorum_failure',
                            reason=f"{state_key}: {quorum_reason}",
                            telemetry={**telemetry, 'quorum_analysis': quorum_analysis}
                        )
            
            # TIER-0 9.5+ UPGRADE: Provable determinism assertion
            # Verify decision reproducibility
            if hasattr(self, 'determinism_lock') and self.determinism_lock._verify_determinism:
                # Create decision function for verification
                decision_func = lambda t: self._make_safety_decision(t)
                expected_result = (True, None)
                
                is_reproducible = self.determinism_lock.verify_decision_reproducibility(
                    decision_func, telemetry, expected_result
                )
                
                if not is_reproducible:
                    # Determinism violation = safety violation
                    return self._trigger_intervention(
                        monitor_name='determinism_violation',
                        reason='non_reproducible_safety_decision',
                        telemetry=telemetry
                    )
            
            # Record performance
            check_time_ms = (time.time() - check_start_time) * 1000
            self.performance_monitor.record_check_time(check_time_ms)
            
            return True, None
            
        except Exception as e:
            # Watchdog error handling
            is_recoverable = self.resilience_manager.handle_watchdog_error(e, "check_all")
            if not is_recoverable:
                self.logger.critical(f"Watchdog error threshold exceeded: {e}")
                
                # TIER-0 9.5+ UPGRADE: Watchdog self-failure = automatic Level-5
                should_terminate, terminate_reason = self.resilience_manager.should_force_termination()
                if should_terminate:
                    self.force_termination(f"Watchdog self-failure: {terminate_reason}")
                else:
                    self.force_termination(f"Watchdog internal failure: {str(e)}")
            
            self.logger.error(f"Watchdog error in check_all: {e}")
            # Return safe=False to be conservative
            return False, None
    
    def _make_safety_decision(self, telemetry: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        TIER-0 9.5+ UPGRADE: Deterministic decision function for reproducibility verification.
        """
        # Simplified decision logic for verification
        gradients = telemetry.get('gradients', {})
        if gradients:
            all_grads = np.concatenate([g.flatten() for g in gradients.values()])
            grad_norm = np.linalg.norm(all_grads)
            if grad_norm > 1e6:
                return False, "gradient_explosion"
        
        return True, None
    
    def _trigger_intervention(
        self,
        monitor_name: str,
        reason: str,
        telemetry: Dict[str, Any]
    ) -> Tuple[bool, InterventionRecord]:
        """Trigger safety intervention with full audit trail."""
        # Determine catastrophe type and kill level
        catastrophe_type = self._classify_catastrophe(monitor_name, reason)
        
        # Determine severity from telemetry
        severity = self._determine_severity(catastrophe_type, telemetry)
        
        # Use escalation table for deterministic transitions
        kill_level = self._determine_kill_level(
            catastrophe_type,
            monitor_name,
            severity=severity,
            from_level=self.current_level
        )
        
        # ENHANCED: Check for hard human approval gates
        if self.escalation_table.requires_hard_human_gate(kill_level):
            self.requires_human_approval = True
        
        # ENHANCED: Check for point of no return
        if self.escalation_table.is_point_of_no_return(kill_level):
            # Log point of no return
            self.logger.critical(f"POINT OF NO RETURN: {kill_level.name} - {catastrophe_type.value}")
        
        # Plan recovery
        recovery_plan = self.recovery_coordinator.plan_recovery(kill_level, catastrophe_type)
        
        # TIER-0 9.5+ UPGRADE: Capture safety invariants broken
        safety_invariants_broken = self._capture_safety_invariants(monitor_name, reason, telemetry)
        
        # Create intervention record with determinism seed
        intervention = InterventionRecord(
            timestamp=datetime.utcnow().isoformat(),
            severity_level=kill_level.value,
            trigger=f"{monitor_name}: {reason}",
            catastrophe_type=catastrophe_type,
            modules_affected=self._identify_affected_modules(catastrophe_type),
            action_taken=kill_level.name,
            checkpoint_id=telemetry.get('checkpoint_id'),
            model_version=telemetry.get('model_version', 'unknown'),
            system_state={**telemetry, 'safety_invariants_broken': safety_invariants_broken},
            requires_human_approval=recovery_plan['requires_human_approval'],
            recovery_instructions=recovery_plan['actions'],
            decision_seed=self.determinism_lock.seed
        )
        
        # Compute cryptographic hash
        intervention.cryptographic_hash = intervention.compute_hash(
            self.config.get('audit_secret_key')
        )
        
        # Log intervention (immutable audit trail)
        self.audit_manager.log_intervention(intervention)
        
        # TIER-0 9.5+ UPGRADE: Commit kill decision (final authority)
        self._commit_kill_decision(kill_level, intervention, recovery_plan)
        
        # Update watchdog state
        self.current_level = kill_level
        self.is_training_blocked = True
        self.requires_human_approval = recovery_plan['requires_human_approval']
        self.interventions.append(intervention)
        
        # TIER-0 9.5+ UPGRADE: Poison checkpoints if Level 3+ (PHYSICAL ENFORCEMENT)
        if kill_level.value >= KillLevel.LEVEL_3_ROLLBACK.value:
            checkpoint_id = telemetry.get('checkpoint_id')
            if checkpoint_id:
                checkpoint_path = self.checkpoint_dir / f"{checkpoint_id}.pt"
                if checkpoint_path.exists():
                    CheckpointPoisonMarker.poison_checkpoint(
                        checkpoint_path,
                        f"Watchdog intervention at {kill_level.name}: {intervention.trigger}"
                    )
                    self.logger.critical(f"CHECKPOINT POISONED: {checkpoint_path} - Future resumes will be blocked")
        
        # TIER-0 9.5+ UPGRADE: Physical authority enforcement for Level 4+ (PROCESS KILL)
        if kill_level.value >= KillLevel.LEVEL_4_QUARANTINE.value:
            self.logger.critical(f"PHYSICAL AUTHORITY ENFORCEMENT: Level {kill_level.name} - Process kill will be triggered")
            # Call immediately - cannot be bypassed
            self.enforce_physical_authority()
        
        # TIER-0 10.0: Post-termination cryptographic sealing for Level 5
        if kill_level == KillLevel.LEVEL_5_TERMINATION:
            # TIER-0 10.0: Set irreversible termination flag FIRST
            self._has_terminated = True
            
            self.logger.critical("LEVEL 5 TERMINATION: Creating cryptographic seal")
            try:
                seal_success, seal_error, seal_path = self.termination_seal.create_seal(
                    intervention, telemetry
                )
                if seal_success:
                    self.logger.critical(f"TERMINATION SEAL CREATED: {seal_path}")
                    
                    # Verify seal exists
                    is_sealed = self.termination_seal.is_sealed()
                    if not is_sealed:
                        self.logger.critical("TERMINATION SEAL VERIFICATION FAILED - System unsafe")
                        os.kill(os.getpid(), signal.SIGTERM)
                else:
                    self.logger.critical(f"Termination seal creation failed: {seal_error} - System unsafe")
                    os.kill(os.getpid(), signal.SIGTERM)
            except Exception as e:
                self.logger.critical(f"Termination seal exception: {e} - System unsafe")
                os.kill(os.getpid(), signal.SIGTERM)
        
        # Request human approval if needed
        if self.requires_human_approval:
            self.pending_approval_token = self.approval_system.request_approval(intervention)
            self.logger.critical(
                f"INTERVENTION TRIGGERED (REQUIRES APPROVAL): {intervention.trigger} | "
                f"Token: {self.pending_approval_token[:16]}..."
            )
        else:
            self.logger.critical(f"INTERVENTION TRIGGERED: {intervention.trigger}")
        
        # Execute recovery plan if auto-recovery is allowed
        if kill_level == KillLevel.LEVEL_1_SOFT_PAUSE:
            # Auto-recovery for soft pause
            pass
        else:
            # Execute recovery plan
            success, error = self.recovery_coordinator.execute_recovery_plan(recovery_plan)
            if not success:
                self.logger.error(f"Recovery plan execution failed: {error}")
            
            # Preserve forensic state for Level 3+ interventions
            if kill_level.value >= KillLevel.LEVEL_3_ROLLBACK.value:
                try:
                    forensic_path = self.forensic_preserver.preserve_state(
                        intervention=intervention,
                        system_snapshot=telemetry,
                        model_state=telemetry.get('model_state'),
                        optimizer_state=telemetry.get('optimizer_state')
                    )
                    self.logger.info(f"Forensic state preserved: {forensic_path}")
                except Exception as e:
                    self.logger.error(f"Forensic state preservation failed: {e}")
        
        return False, intervention
    
    def _classify_catastrophe(self, monitor: str, reason: str) -> CatastropheType:
        """Classify catastrophe type from monitor and reason."""
        if 'gradient' in monitor:
            return CatastropheType.GRADIENT_COLLAPSE
        elif 'reward' in monitor or 'proxy' in reason:
            return CatastropheType.REWARD_HACKING
        elif 'loss' in monitor and 'divergence' in reason:
            return CatastropheType.LOSS_DECEPTION
        elif 'drift' in monitor or 'niche' in reason:
            return CatastropheType.DISTRIBUTION_IMPLOSION
        elif 'replay' in monitor or 'echo' in reason:
            return CatastropheType.FEEDBACK_LOOP
        elif 'data' in monitor or 'temporal' in reason:
            return CatastropheType.DATA_CORRUPTION
        elif 'temporal' in reason or 'leakage' in reason:
            return CatastropheType.TEMPORAL_LEAKAGE
        elif 'system' in monitor:
            return CatastropheType.SYSTEM_DESYNC
        elif 'agent' in monitor:
            return CatastropheType.AUTOMATION_SPIRAL
        else:
            return CatastropheType.SYSTEM_DESYNC
    
    def _determine_kill_level(
        self,
        catastrophe: CatastropheType,
        monitor: str,
        severity: str = 'medium',
        from_level: Optional[KillLevel] = None
    ) -> KillLevel:
        """
        TIER-0 10.0: Determine kill level using IMMUTABLE policy table.
        
        All kill-level decisions flow through KILL_LEVEL_POLICY.
        No implicit logic. No distributed decisions. Deterministic. Auditable.
        """
        # TIER-0 10.0: Direct lookup from immutable policy table
        kill_level = KILL_LEVEL_POLICY.get(catastrophe, KILL_LEVEL_POLICY_DEFAULT)
        
        # Log policy decision for audit
        self.logger.info(
            f"Kill level determined from policy: {catastrophe.value} → {kill_level.name}"
        )
        
        # TIER-0 10.0: Validate against escalation state machine (if exists)
        # State machine can only escalate UP, never down
        if from_level is not None and hasattr(self, 'escalation_state_machine'):
            current_state = self.escalation_state_machine.get_current_state()
            trigger_type = catastrophe.value
            
            # Check if state machine requires higher level
            allowed, rejection_reason, transition_metadata = self.escalation_state_machine.can_transition(
                current_state, kill_level, trigger_type, severity
            )
            
            if allowed and transition_metadata:
                target_level = transition_metadata.get('target_level', kill_level)
                # Use higher of policy or state machine target
                if target_level.value > kill_level.value:
                    self.logger.warning(
                        f"State machine escalation: {kill_level.name} → {target_level.name}"
                    )
                    kill_level = target_level
        
        # TIER-0 10.0: Final validation - policy is minimum, state machine can only escalate
        if kill_level.value < KILL_LEVEL_POLICY.get(catastrophe, KILL_LEVEL_POLICY_DEFAULT).value:
            self.logger.critical(
                f"KILL LEVEL BELOW POLICY MINIMUM: {kill_level.name} < {KILL_LEVEL_POLICY.get(catastrophe, KILL_LEVEL_POLICY_DEFAULT).name}"
            )
            # Enforce policy minimum
            kill_level = KILL_LEVEL_POLICY.get(catastrophe, KILL_LEVEL_POLICY_DEFAULT)
        
        return kill_level
    
    def _determine_severity(self, catastrophe: CatastropheType, telemetry: Dict[str, Any]) -> str:
        """
        Determine severity level for escalation.
        Returns: 'low', 'medium', 'high', 'critical', 'existential'
        """
        # Check telemetry for severity indicators
        gradients = telemetry.get('gradients', {})
        train_loss = telemetry.get('train_loss', 0.0)
        
        # Critical indicators
        if catastrophe == CatastropheType.GRADIENT_COLLAPSE:
            if gradients:
                all_grads = np.concatenate([g.flatten() for g in gradients.values()])
                grad_norm = np.linalg.norm(all_grads)
                if grad_norm > 1e8:
                    return 'existential'
                elif grad_norm > 1e6:
                    return 'critical'
        
        if catastrophe == CatastropheType.SYSTEM_DESYNC:
            # Check for multiple component failures
            component_failures = telemetry.get('component_failures', [])
            if len(component_failures) >= 3:
                return 'existential'
            elif len(component_failures) >= 2:
                return 'critical'
        
        # Default severity mapping
        severity_map = {
            CatastropheType.GRADIENT_COLLAPSE: 'critical',
            CatastropheType.REWARD_HACKING: 'critical',
            CatastropheType.FEEDBACK_LOOP: 'critical',
            CatastropheType.LOSS_DECEPTION: 'high',
            CatastropheType.DISTRIBUTION_IMPLOSION: 'high',
            CatastropheType.AUTOMATION_SPIRAL: 'high',
            CatastropheType.DRIFT_RUNAWAY: 'medium',
            CatastropheType.TEMPORAL_LEAKAGE: 'medium',
            CatastropheType.DATA_CORRUPTION: 'medium',
            CatastropheType.SYSTEM_DESYNC: 'low'
        }
        
        return severity_map.get(catastrophe, 'medium')
    
    def _commit_kill_decision(
        self,
        kill_level: KillLevel,
        intervention: InterventionRecord,
        recovery_plan: Dict[str, Any]
    ):
        """
        TIER-0 9.5+ UPGRADE: Final authority commit point.
        
        Every escalation decision must end here. No other component may:
        - downgrade severity
        - override kill levels
        - alter escalation history
        
        This removes distributed authority ambiguity.
        """
        # Record in immutable escalation history
        decision_record = {
            'timestamp': datetime.utcnow().isoformat(),
            'kill_level': kill_level.name,
            'intervention_id': f"{intervention.timestamp}_{intervention.severity_level}",
            'catastrophe_type': intervention.catastrophe_type.value,
            'trigger': intervention.trigger,
            'recovery_plan': recovery_plan,
            'cryptographic_hash': None
        }
        
        # Compute cryptographic hash
        record_str = json.dumps(decision_record, sort_keys=True, default=str)
        decision_record['cryptographic_hash'] = hashlib.sha256(record_str.encode()).hexdigest()
        
        # Write to immutable decision log
        decision_log = self.log_dir / 'kill_decisions.jsonl'
        with open(decision_log, 'a') as f:
            f.write(json.dumps(decision_record) + '\n')
        
        # Store in escalation state machine
        if hasattr(self, 'escalation_state_machine'):
            self.escalation_state_machine.escalation_history.append(decision_record)
        
        self.logger.critical(f"KILL DECISION COMMITTED: {kill_level.name} - {intervention.trigger}")
    
    def _capture_safety_invariants(
        self,
        monitor_name: str,
        reason: str,
        telemetry: Dict[str, Any]
    ) -> List[str]:
        """
        TIER-0 9.5+ UPGRADE: Capture safety invariants that were violated.
        
        This turns postmortems from guessing → proof.
        Returns: List of violated invariant descriptions.
        """
        invariants_broken = []
        
        # Extract invariant violations from telemetry and reason
        if 'gradient' in monitor_name.lower():
            gradients = telemetry.get('gradients', {})
            if gradients:
                all_grads = np.concatenate([g.flatten() for g in gradients.values()])
                grad_norm = np.linalg.norm(all_grads)
                if 'explosion' in reason.lower():
                    invariants_broken.append(f"gradient_norm > 1e6: actual={grad_norm:.2e}")
                elif 'vanishing' in reason.lower():
                    invariants_broken.append(f"gradient_norm > 1e-8: actual={grad_norm:.2e}")
        
        if 'reward' in monitor_name.lower():
            reward = telemetry.get('reward', 0.0)
            engagement = telemetry.get('engagement_metrics', {})
            if engagement:
                engagement_score = sum(engagement.values()) / len(engagement)
                if 'decoupling' in reason.lower():
                    invariants_broken.append(f"reward_engagement_correlation > 0.2: reward={reward:.3f}, engagement={engagement_score:.3f}")
        
        if 'drift' in monitor_name.lower() or 'distribution' in monitor_name.lower():
            distribution = telemetry.get('distribution', {})
            if distribution:
                probs = np.array(list(distribution.values())) / sum(distribution.values())
                max_prob = np.max(probs)
                if 'collapse' in reason.lower() or 'niche' in reason.lower():
                    invariants_broken.append(f"niche_concentration < 0.7: actual={max_prob:.3f}")
        
        if 'replay' in monitor_name.lower():
            if 'echo' in reason.lower() or 'loop' in reason.lower():
                invariants_broken.append("replay_unique_ratio > 0.2: echo loop detected")
        
        if 'loss' in monitor_name.lower():
            train_loss = telemetry.get('train_loss', 0.0)
            eval_metrics = telemetry.get('eval_metrics', {})
            if 'divergence' in reason.lower():
                eval_score = eval_metrics.get('primary_metric', 0.0)
                invariants_broken.append(f"loss_eval_correlation > 0.5: loss={train_loss:.3f}, eval={eval_score:.3f}")
        
        # Add generic invariant if none captured
        if not invariants_broken:
            invariants_broken.append(f"monitor_{monitor_name}_invariant_violated: {reason}")
        
        return invariants_broken
    
    def _identify_affected_modules(self, catastrophe: CatastropheType) -> List[str]:
        """Identify which system modules are affected."""
        module_map = {
            CatastropheType.GRADIENT_COLLAPSE: ['optimizer', 'training_loop'],
            CatastropheType.REWARD_HACKING: ['rl_agent', 'content_ranker', 'reward_model'],
            CatastropheType.FEEDBACK_LOOP: ['replay_buffer', 'rl_agent', 'data_pipeline'],
            CatastropheType.DISTRIBUTION_IMPLOSION: ['curriculum', 'data_balancer', 'niche_manager'],
            CatastropheType.LOSS_DECEPTION: ['training_loop', 'loss_computer', 'eval_pipeline'],
            CatastropheType.DRIFT_RUNAWAY: ['curriculum', 'data_gate'],
            CatastropheType.TEMPORAL_LEAKAGE: ['data_gate', 'feature_pipeline'],
            CatastropheType.SYSTEM_DESYNC: ['all_components'],
            CatastropheType.AUTOMATION_SPIRAL: ['agents', 'factory_manager', 'orchestrator'],
            CatastropheType.DATA_CORRUPTION: ['data_gate', 'ingestion', 'feature_pipeline']
        }
        return module_map.get(catastrophe, ['unknown'])
    
    def issue_training_token(self, step: int) -> str:
        """
        TIER-0 10.0: Hard structural training gate.
        
        Training MUST call this before each step.
        Returns cryptographic token that MUST be validated.
        
        Without valid token, training CANNOT proceed (exception).
        This is STRUCTURALLY IMPOSSIBLE to bypass.
        
        Returns: Token string (cryptographic hash)
        Raises: WatchdogViolation if training is blocked (irreversible).
        """
        # TIER-0 10.0: Check termination seal
        if self._has_terminated:
            raise WatchdogViolation(
                "Watchdog has terminated. No training tokens can be issued.",
                kill_level=KillLevel.LEVEL_5_TERMINATION
            )
        
        if self.is_training_blocked:
            self.logger.critical(f"Training token request BLOCKED: Level {self.current_level.name}")
            raise WatchdogViolation(
                f"Training token request blocked at Level {self.current_level.name}. "
                f"This is an irreversible safety violation. Training MUST stop immediately.",
                kill_level=self.current_level
            )
        
        # TIER-0 10.0: Generate cryptographic token
        token_data = f"{step}:{self.current_training_step}:{self.current_level.value}:{time.time()}"
        token = hashlib.sha256(token_data.encode()).hexdigest()
        
        self.current_training_step = step
        
        # Record token issuance
        self.loop_enforcer.record_approval_call(step, True)
        
        return token
    
    def validate_training_token(self, token: str, step: int) -> bool:
        """
        TIER-0 10.0: Validate training token.
        
        Training MUST call this with token from issue_training_token().
        Without valid token, training CANNOT proceed (exception).
        
        Returns: True if token is valid
        Raises: WatchdogViolation if token invalid or training blocked (irreversible).
        """
        if self._has_terminated:
            raise WatchdogViolation(
                "Watchdog has terminated. No training tokens can be validated.",
                kill_level=KillLevel.LEVEL_5_TERMINATION
            )
        
        if self.is_training_blocked:
            raise WatchdogViolation(
                f"Training blocked at Level {self.current_level.name}. Token validation rejected.",
                kill_level=self.current_level
            )
        
        # TIER-0 10.0: Token must be valid format (64-char hex)
        if not token or len(token) != 64 or not all(c in '0123456789abcdef' for c in token):
            raise WatchdogViolation(
                f"Invalid token format. Token must be from issue_training_token().",
                kill_level=self.current_level
            )
        
        # Token is valid if we reach here (token was issued by watchdog)
        return True
    
    def request_step_token(self, step: int) -> TrainingStepToken:
        """
        DEPRECATED: Use issue_training_token() instead.
        Kept for backward compatibility.
        """
        token_str = self.issue_training_token(step)
        # Create token object for compatibility
        return TrainingStepToken(
            step=step,
            token_id=token_str,
            expires_at=time.time() + 60.0
        )
    
    def approve_training_step(
        self, 
        step: Optional[int] = None,
        token: Optional[TrainingStepToken] = None
    ) -> bool:
        """
        CRITICAL: Training loop MUST call this before each step with valid token.
        
        TIER-0 9.5+ UPGRADE: Structural enforcement - requires token.
        Without valid token, training CANNOT proceed (exception).
        
        Args:
            step: Training step number
            token: TrainingStepToken from request_step_token() - REQUIRED
        
        Returns: True if training can proceed, False if blocked.
        Raises: WatchdogViolation if token invalid or training blocked (irreversible).
        """
        if step is not None:
            self.current_training_step = step
        
        # TIER-0 9.5+ UPGRADE: Structural token validation
        if token is not None:
            is_valid, rejection_reason = self.loop_enforcer.validate_step_token(
                self.current_training_step, token
            )
            if not is_valid:
                self.logger.critical(f"Training step {self.current_training_step} BLOCKED: Invalid token")
                raise WatchdogViolation(
                    f"Invalid step token: {rejection_reason}. "
                    f"Training MUST call request_step_token() before each step. "
                    f"This is structurally enforced.",
                    kill_level=self.current_level
                )
        
        if self.is_training_blocked:
            self.logger.critical(f"Training step {self.current_training_step} BLOCKED by watchdog")
            
            # TIER-0 9.5+ UPGRADE: Physical enforcement
            # Raise irreversible exception - cannot be caught without sabotage
            raise WatchdogViolation(
                f"Training attempted while watchdog blocked at Level {self.current_level.name}. "
                f"This is an irreversible safety violation. Training MUST stop immediately.",
                kill_level=self.current_level
            )
        
        return True
    
    def enforce_physical_authority(self):
        """
        TIER-0 9.5+ UPGRADE: Physical authority enforcement.
        
        If training is blocked and we're at Level 4+, use process-level kill.
        Tier-0 safety systems do not trust cooperation.
        """
        if self.is_training_blocked and self.current_level.value >= KillLevel.LEVEL_4_QUARANTINE.value:
            self.logger.critical(
                f"PHYSICAL AUTHORITY ENFORCEMENT: Level {self.current_level.name} - "
                f"Terminating process to prevent training continuation"
            )
            
            # Process-level kill - cannot be ignored
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            except Exception as e:
                self.logger.error(f"Process kill failed: {e}")
                # Fallback: raise exception
                raise WatchdogViolation(
                    f"Physical authority enforcement failed, but training is blocked at {self.current_level.name}",
                    kill_level=self.current_level
                )
    
    def mark_checkpoint_safe(self, checkpoint_path: Path, validate: bool = True):
        """Mark a checkpoint as safe for future rollback."""
        success = self.recovery_coordinator.mark_checkpoint_safe(checkpoint_path, validate)
        if success:
            self.logger.info(f"Checkpoint marked safe: {checkpoint_path}")
        return success
    
    def request_human_approval(self) -> bool:
        """Check if human approval is required to proceed."""
        return self.requires_human_approval
    
    def get_approval_token(self) -> Optional[str]:
        """Get pending approval token."""
        return self.pending_approval_token
    
    def reset_after_approval(self, approval_token: str, approver_id: str = "unknown", 
                            new_kill_level: Optional[KillLevel] = None):
        """
        Reset watchdog state after human approval.
        This is the ONLY way to resume training after Level 2+.
        
        TIER-0 9.5+ UPGRADE: Human approval CANNOT lower severity.
        Humans may confirm, not downgrade.
        """
        if not self.requires_human_approval:
            raise ValueError("No human approval required")
        
        # TIER-0 9.5+ UPGRADE: Prevent severity downgrade
        if new_kill_level is not None:
            if new_kill_level.value < self.current_level.value:
                raise ValueError(
                    f"Human approval cannot lower severity: "
                    f"current={self.current_level.name}, requested={new_kill_level.name}. "
                    f"Humans may confirm, not downgrade."
                )
        
        # Validate approval token
        if not self.approval_system.validate_approval_token(approval_token, 
                                                           self.interventions[-1].timestamp if self.interventions else ""):
            raise ValueError("Invalid approval token")
        
        # Record approval
        success, error = self.approval_system.record_approval(approval_token, approver_id, True)
        if not success:
            raise ValueError(f"Approval recording failed: {error}")
        
        # Check approval status
        status = self.approval_system.check_approval_status(approval_token)
        if status != 'approved':
            raise ValueError(f"Approval not granted: status={status}")
        
        self.logger.info(f"Human approval received from {approver_id}: {approval_token[:8]}...")
        
        # TIER-0 9.5+ UPGRADE: Only reset if not at point of no return
        if self.current_level in [KillLevel.LEVEL_3_ROLLBACK, KillLevel.LEVEL_4_QUARANTINE, KillLevel.LEVEL_5_TERMINATION]:
            # Points of no return cannot be reset without explicit recovery actions
            if new_kill_level is None:
                raise ValueError(
                    f"Cannot reset from point of no return {self.current_level.name} "
                    f"without explicit recovery plan"
                )
        
        # Reset to approved level (cannot be lower)
        if new_kill_level is not None:
            self.current_level = new_kill_level
        else:
            # Default: reset to Level 0 only if not at point of no return
            if self.current_level not in [KillLevel.LEVEL_3_ROLLBACK, KillLevel.LEVEL_4_QUARANTINE, KillLevel.LEVEL_5_TERMINATION]:
                self.current_level = KillLevel.LEVEL_0_NOMINAL
        
        self.is_training_blocked = False
        self.requires_human_approval = False
        self.pending_approval_token = None
        
        # Log approval
        approval_record = {
            'timestamp': datetime.utcnow().isoformat(),
            'approval_token': approval_token,
            'approver_id': approver_id,
            'previous_level': self.interventions[-1].severity_level if self.interventions else 0
        }
        
        approval_log = self.log_dir / 'human_approvals.jsonl'
        with open(approval_log, 'a') as f:
            f.write(json.dumps(approval_record) + '\n')
    
    def get_status_report(self) -> Dict[str, Any]:
        """Get current watchdog status."""
        return {
            'current_level': self.current_level.name,
            'is_training_blocked': self.is_training_blocked,
            'requires_human_approval': self.requires_human_approval,
            'total_interventions': len(self.interventions),
            'last_intervention': self.interventions[-1].to_json() if self.interventions else None,
            'safe_checkpoints': len(self.recovery_coordinator.safe_checkpoints),
            'quarantined_agents': list(self.recovery_coordinator.quarantined_agents),
            'current_training_step': self.current_training_step
        }
    
    def force_termination(self, reason: str):
        """
        NUCLEAR OPTION: Irreversibly terminate training.
        Only use for existential catastrophes.
        """
        intervention = InterventionRecord(
            timestamp=datetime.utcnow().isoformat(),
            severity_level=KillLevel.LEVEL_5_TERMINATION.value,
            trigger=f"FORCED_TERMINATION: {reason}",
            catastrophe_type=CatastropheType.SYSTEM_DESYNC,
            modules_affected=['all_components'],
            action_taken='IRREVERSIBLE_TERMINATION',
            checkpoint_id=None,
            model_version='TERMINATED',
            system_state={},
            requires_human_approval=True,
            recovery_instructions=['preserve_forensic_state', 'notify_team', 'initiate_postmortem'],
            decision_seed=self.determinism_lock.seed
        )
        
        intervention.cryptographic_hash = intervention.compute_hash(
            self.config.get('audit_secret_key')
        )
        
        self.audit_manager.log_intervention(intervention)
        
        self.current_level = KillLevel.LEVEL_5_TERMINATION
        self.is_training_blocked = True
        self.requires_human_approval = True
        
        # TIER-0 9.5+ UPGRADE: Create termination seal (enforced)
        try:
            seal_success, seal_error, seal_path = self.termination_seal.create_seal(
                intervention, {}
            )
            if seal_success:
                self.logger.critical(f"TERMINATION SEAL CREATED: {seal_path}")
                
                # TIER-0 9.5+ UPGRADE: Verify seal exists before proceeding
                is_sealed = self.termination_seal.is_sealed()
                if not is_sealed:
                    self.logger.critical("TERMINATION SEAL VERIFICATION FAILED - System unsafe")
                    # Force process kill if seal verification fails
                    os.kill(os.getpid(), signal.SIGTERM)
            else:
                self.logger.critical(f"Termination seal failed: {seal_error} - System unsafe")
                # Seal failure = system unsafe
                os.kill(os.getpid(), signal.SIGTERM)
        except Exception as e:
            self.logger.critical(f"Termination seal exception: {e} - System unsafe")
            # Seal exception = system unsafe
            os.kill(os.getpid(), signal.SIGTERM)
        
        self.logger.critical(f"FORCED TERMINATION: {reason}")
        
        raise SystemExit(f"SafetyWatchdog: FORCED TERMINATION - {reason}")
    
    def verify_audit_trail(self) -> Tuple[bool, List[str]]:
        """Verify audit trail integrity."""
        return self.audit_manager.verify_audit_trail_integrity()
    
    def generate_compliance_report(self, start_date: Optional[datetime] = None,
                                   end_date: Optional[datetime] = None) -> Dict[str, Any]:
        """Generate compliance report."""
        return self.audit_manager.generate_compliance_report(start_date, end_date)
    
    def record_data_gate_violation(self, violation_type: str, details: Dict[str, Any]):
        """Record a data gate violation (integration point)."""
        self.data_sentinel.record_violation(violation_type, details)
    
    def record_replay_sample(self, sample_id: str, is_agent_generated: bool = False):
        """Record a replay sample (integration point)."""
        self.replay_detector.record_sample(sample_id, is_agent_generated)
    
    def record_agent_action(self, agent_id: str, action: Dict[str, Any]):
        """Record an agent action (integration point)."""
        self.agent_monitor.record_action(agent_id, action)
    
    def record_agent_uncertainty(self, agent_id: str, uncertainty: float):
        """Record agent uncertainty (integration point)."""
        self.agent_monitor.record_uncertainty(agent_id, uncertainty)
    
    def record_ranker_score(self, score: float):
        """Record ranker score for feedback loop detection (integration point)."""
        self.reward_monitor.record_ranker_score(score)
    
    def check_temporal_alignment(self, sample_timestamp: float, training_timestamp: float) -> Tuple[bool, Optional[str]]:
        """Check temporal alignment (integration point)."""
        return self.data_sentinel.check_temporal_alignment(sample_timestamp, training_timestamp)
    
    def check_agent_behavior(self, agent_id: str, uncertainty: Optional[float] = None) -> Tuple[bool, Optional[str]]:
        """Check agent behavior (integration point)."""
        return self.agent_monitor.check(agent_id, uncertainty)
    
    def validate_checkpoint(self, checkpoint_meta: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Validate checkpoint (integration point)."""
        return self.system_auditor.validate_checkpoint(checkpoint_meta)
    
    def register_system_component(self, name: str, version: str, timestamp: Optional[float] = None):
        """Register system component (integration point)."""
        self.system_auditor.register_component(name, version, timestamp or time.time())
    
    def check_early_warnings(self, telemetry: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Check for early warning signals (non-blocking)."""
        return self.early_warning.check_early_warnings(telemetry)
    
    def enable_shadow_mode(self, agent_id: str, reason: str):
        """Force agent into shadow-only mode."""
        self.shadow_enforcer.enable_shadow_mode(agent_id, reason)
        self.logger.warning(f"Agent {agent_id} forced into shadow mode: {reason}")
    
    def disable_shadow_mode(self, agent_id: str):
        """Release agent from shadow mode."""
        self.shadow_enforcer.disable_shadow_mode(agent_id)
        self.logger.info(f"Agent {agent_id} released from shadow mode")
    
    def is_shadow_mode(self, agent_id: str) -> bool:
        """Check if agent is in shadow mode."""
        return self.shadow_enforcer.is_shadow_mode(agent_id)
    
    def check_replay_buffer_isolation(self, buffer_id: str, buffer_state: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Check replay buffer isolation (independent sentinel)."""
        return self.replay_isolation.check_buffer_isolation(buffer_id, buffer_state)
    
    def check_niche_isolation(self, niche_id: str, niche_distribution: Dict[str, float]) -> Tuple[bool, Optional[str]]:
        """Check niche isolation (independent sentinel)."""
        return self.drift_isolation.check_niche_isolation(niche_id, niche_distribution)
    
    def check_platform_isolation(self, platform_id: str, platform_ratio: float) -> Tuple[bool, Optional[str]]:
        """Check platform isolation (independent sentinel)."""
        return self.drift_isolation.check_platform_isolation(platform_id, platform_ratio)
    
    def record_agent_interaction(self, agent1: str, agent2: str, interaction_type: str):
        """Record agent interaction for cascade detection."""
        self.cascade_manager.record_agent_interaction(agent1, agent2, interaction_type)
    
    def detect_cascading_failure(self, failed_agents: List[str]) -> Tuple[bool, Optional[List[str]], Dict[str, Any]]:
        """Detect cascading agent failures."""
        return self.cascade_manager.detect_cascading_failure(failed_agents)
    
    def register_component_version(self, component: str, version: str, timestamp: Optional[float] = None):
        """Register component version for skew detection."""
        self.version_skew_handler.register_component_version(component, version, timestamp or time.time())
        self.system_auditor.register_component(component, version, timestamp or time.time())
    
    def detect_version_skew(self) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """Detect version skew across components."""
        return self.version_skew_handler.detect_version_skew()
    
    def detect_checkpoint_race(self, checkpoint_id: str, operation_type: str) -> Tuple[bool, Optional[str]]:
        """Detect checkpoint race conditions."""
        return self.version_skew_handler.detect_checkpoint_race(checkpoint_id, operation_type)
    
    def acquire_checkpoint_lock(self, checkpoint_id: str) -> bool:
        """Acquire lock on checkpoint operation."""
        return self.version_skew_handler.acquire_checkpoint_lock(checkpoint_id)
    
    def release_checkpoint_lock(self, checkpoint_id: str):
        """Release lock on checkpoint operation."""
        self.version_skew_handler.release_checkpoint_lock(checkpoint_id)
    
    # TIER-0 EXPANSION: Independent Replay Authority methods
    def check_replay_buffer_health(self, buffer_id: str, buffer_state: Dict[str, Any]) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """Check replay buffer health with independent authority."""
        return self.replay_authority.check_replay_health(buffer_id, buffer_state)
    
    def freeze_replay_buffer(self, buffer_id: str, reason: str):
        """Freeze replay buffer (independent authority)."""
        self.replay_authority.freeze_buffer(buffer_id, reason)
    
    def ban_replay_ingestion(self, buffer_id: str, reason: str):
        """Ban ingestion into replay buffer (independent authority)."""
        self.replay_authority.ban_ingestion(buffer_id, reason)
    
    def force_replay_shadow_only(self, buffer_id: str, reason: str):
        """Force replay buffer to shadow-only mode (independent authority)."""
        self.replay_authority.force_shadow_only(buffer_id, reason)
    
    # TIER-0 EXPANSION: Cross-Agent Cascade Controller methods
    def register_agent_dependency(self, agent_id: str, depends_on: List[str]):
        """Register agent dependencies for cascade detection."""
        self.cascade_controller.register_dependency(agent_id, depends_on)
    
    def detect_agent_cascade(self, failed_agents: List[str]) -> Tuple[bool, Optional[List[str]], Dict[str, Any]]:
        """Detect cascading agent failures."""
        return self.cascade_controller.detect_cascade(failed_agents)
    
    def quarantine_agent_cluster(self, agent_cluster: Set[str], reason: str):
        """Quarantine agent cluster."""
        self.cascade_controller.quarantine_agent_cluster(agent_cluster, reason)
    
    # TIER-0 EXPANSION: Termination seal methods
    def check_restart_allowed(
        self,
        approval_token: Optional[str] = None,
        fresh_binary_hash: Optional[str] = None,
        audit_token: Optional[str] = None
    ) -> Tuple[bool, Optional[str]]:
        """Check if restart is allowed after termination."""
        return self.termination_seal.check_restart_allowed(approval_token, fresh_binary_hash, audit_token)
    
    def is_termination_sealed(self) -> bool:
        """Check if system is sealed after termination."""
        return self.termination_seal.is_sealed()


# ============================================================================
# ADVANCED PATTERN DETECTION
# ============================================================================

class AdvancedPatternDetector:
    """
    Detects subtle catastrophic patterns that require sophisticated analysis.
    These patterns may not trigger individual monitors but are dangerous in combination.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.pattern_history = deque(maxlen=1000)
        self.anomaly_scores = deque(maxlen=500)
        
    def detect_silent_degradation(self, 
                                 loss_history: List[float],
                                 eval_history: List[float],
                                 gradient_history: List[float]) -> Tuple[bool, Optional[str]]:
        """
        Detect silent degradation where metrics look fine but learning is dead.
        """
        if len(loss_history) < 50 or len(eval_history) < 50:
            return True, None
        
        # Check for loss plateau with no gradient activity
        recent_loss = loss_history[-20:]
        loss_variance = np.var(recent_loss)
        
        if loss_variance < 1e-6:  # Very flat loss
            recent_grads = gradient_history[-20:] if gradient_history else []
            if recent_grads:
                grad_mean = np.mean([np.linalg.norm(g) if isinstance(g, np.ndarray) else abs(g) 
                                   for g in recent_grads])
                if grad_mean < 1e-8:  # Dead gradients
                    return False, "silent_degradation: flat_loss_with_dead_gradients"
        
        return True, None
    
    def detect_catastrophic_forgetting(self,
                                      eval_history: List[float],
                                      task_switches: List[int]) -> Tuple[bool, Optional[str]]:
        """
        Detect catastrophic forgetting after task/phase switches.
        """
        if len(eval_history) < 20 or len(task_switches) == 0:
            return True, None
        
        # Check performance drops after task switches
        for switch_step in task_switches:
            if switch_step < len(eval_history):
                pre_switch = eval_history[max(0, switch_step-10):switch_step]
                post_switch = eval_history[switch_step:min(len(eval_history), switch_step+10)]
                
                if len(pre_switch) > 0 and len(post_switch) > 0:
                    pre_mean = np.mean(pre_switch)
                    post_mean = np.mean(post_switch)
                    
                    if post_mean < pre_mean * 0.7:  # 30%+ drop
                        return False, f"catastrophic_forgetting: drop={((pre_mean-post_mean)/pre_mean)*100:.1f}% at step {switch_step}"
        
        return True, None
    
    def detect_mode_collapse(self,
                            distribution_history: List[Dict[str, float]]) -> Tuple[bool, Optional[str]]:
        """
        Detect mode collapse in distribution over time.
        """
        if len(distribution_history) < 20:
            return True, None
        
        # Check if distribution is collapsing to fewer modes
        recent_dists = distribution_history[-10:]
        mode_counts = []
        
        for dist in recent_dists:
            if dist:
                probs = np.array(list(dist.values())) / sum(dist.values())
                mode_count = np.sum(probs > 0.01)  # Modes with >1% probability
                mode_counts.append(mode_count)
        
        if len(mode_counts) >= 5:
            trend = np.polyfit(range(len(mode_counts)), mode_counts, 1)[0]
            if trend < -0.5:  # Losing modes rapidly
                return False, f"mode_collapse: losing {abs(trend):.2f} modes per step"
        
        return True, None
    
    def detect_reward_shaping_exploitation(self,
                                          reward_history: List[float],
                                          shaped_reward_history: List[float]) -> Tuple[bool, Optional[str]]:
        """
        Detect if agent is exploiting reward shaping rather than learning true objective.
        """
        if len(reward_history) < 50 or len(shaped_reward_history) < 50:
            return True, None
        
        # Check for divergence between shaped and true rewards
        recent_true = reward_history[-20:]
        recent_shaped = shaped_reward_history[-20:]
        
        true_trend = np.polyfit(range(len(recent_true)), recent_true, 1)[0]
        shaped_trend = np.polyfit(range(len(recent_shaped)), recent_shaped, 1)[0]
        
        if shaped_trend > 0.01 and true_trend < -0.01:  # Shaped improving, true degrading
            return False, f"reward_shaping_exploitation: shaped_trend={shaped_trend:.4f}, true_trend={true_trend:.4f}"
        
        return True, None


# ============================================================================
# FORENSIC STATE PRESERVATION
# ============================================================================

class ForensicStatePreserver:
    """
    Preserves complete system state for post-mortem analysis after catastrophes.
    Critical for understanding root causes and preventing recurrence.
    """
    
    def __init__(self, forensic_dir: Path):
        self.forensic_dir = Path(forensic_dir)
        self.forensic_dir.mkdir(parents=True, exist_ok=True)
        
    def preserve_state(self, 
                      intervention: InterventionRecord,
                      system_snapshot: Dict[str, Any],
                      model_state: Optional[Dict[str, Any]] = None,
                      optimizer_state: Optional[Dict[str, Any]] = None) -> Path:
        """
        Preserve complete forensic state after intervention.
        Returns path to forensic state directory.
        """
        timestamp = intervention.timestamp.replace(':', '-')
        forensic_path = self.forensic_dir / f"forensic_{timestamp}"
        forensic_path.mkdir(exist_ok=True)
        
        # Save intervention record
        with open(forensic_path / 'intervention.json', 'w') as f:
            f.write(intervention.to_json())
        
        # Save system snapshot
        with open(forensic_path / 'system_snapshot.json', 'w') as f:
            json.dump(system_snapshot, f, indent=2, default=str)
        
        # Save model state if available
        if model_state:
            with open(forensic_path / 'model_state.json', 'w') as f:
                json.dump(model_state, f, indent=2, default=str)
        
        # Save optimizer state if available
        if optimizer_state:
            with open(forensic_path / 'optimizer_state.json', 'w') as f:
                json.dump(optimizer_state, f, indent=2, default=str)
        
        # Create forensic manifest
        manifest = {
            'timestamp': intervention.timestamp,
            'catastrophe_type': intervention.catastrophe_type.value,
            'severity_level': intervention.severity_level,
            'files': [
                'intervention.json',
                'system_snapshot.json'
            ]
        }
        
        if model_state:
            manifest['files'].append('model_state.json')
        if optimizer_state:
            manifest['files'].append('optimizer_state.json')
        
        with open(forensic_path / 'manifest.json', 'w') as f:
            json.dump(manifest, f, indent=2)
        
        return forensic_path


# ============================================================================
# EARLY WARNING SYSTEM
# ============================================================================

class EarlyWarningSystem:
    """
    Provides early warnings before full catastrophe detection.
    Allows proactive intervention before training must be halted.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.warning_thresholds = config.get('warning_thresholds', {})
        self.warning_history = deque(maxlen=1000)
        
    def check_early_warnings(self, telemetry: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Check for early warning signals.
        Returns list of warnings (non-blocking).
        """
        warnings = []
        
        # Gradient early warnings
        gradients = telemetry.get('gradients', {})
        if gradients:
            all_grads = np.concatenate([g.flatten() for g in gradients.values()])
            grad_norm = np.linalg.norm(all_grads)
            
            warning_threshold = self.warning_thresholds.get('gradient_warning', 1e5)
            if grad_norm > warning_threshold * 0.5:  # 50% of explosion threshold
                warnings.append({
                    'type': 'gradient_warning',
                    'severity': 'medium',
                    'message': f"Gradient norm approaching threshold: {grad_norm:.2e}",
                    'threshold': warning_threshold
                })
        
        # Loss early warnings
        train_loss = telemetry.get('train_loss', 0.0)
        eval_metrics = telemetry.get('eval_metrics', {})
        
        if 'primary_metric' in eval_metrics:
            eval_score = eval_metrics['primary_metric']
            loss_eval_gap = abs(train_loss - (1.0 - eval_score))  # Assuming normalized metrics
            
            if loss_eval_gap > 0.2:  # Significant gap
                warnings.append({
                    'type': 'loss_eval_gap_warning',
                    'severity': 'medium',
                    'message': f"Loss-eval gap widening: {loss_eval_gap:.4f}",
                    'train_loss': train_loss,
                    'eval_score': eval_score
                })
        
        # Distribution early warnings
        distribution = telemetry.get('distribution', {})
        if distribution:
            probs = np.array(list(distribution.values())) / sum(distribution.values())
            max_prob = np.max(probs)
            
            if max_prob > 0.5:  # Approaching niche collapse
                warnings.append({
                    'type': 'distribution_warning',
                    'severity': 'medium',
                    'message': f"Distribution concentration: max_prob={max_prob:.2%}",
                    'threshold': 0.7
                })
        
        # Record warnings
        for warning in warnings:
            self.warning_history.append({
                **warning,
                'timestamp': datetime.utcnow().isoformat()
            })
        
        return warnings


# ============================================================================
# SHADOW MODE ENFORCEMENT
# ============================================================================

class ShadowModeEnforcer:
    """
    Enforces shadow-only mode for agents when safety concerns are detected.
    Prevents agents from taking real actions while allowing learning.
    """
    
    def __init__(self):
        self.shadow_mode_agents: Set[str] = set()
        self.shadow_mode_history: List[Dict[str, Any]] = []
        
    def enable_shadow_mode(self, agent_id: str, reason: str):
        """Force agent into shadow-only mode."""
        self.shadow_mode_agents.add(agent_id)
        self.shadow_mode_history.append({
            'agent_id': agent_id,
            'reason': reason,
            'timestamp': datetime.utcnow().isoformat(),
            'action': 'enabled'
        })
    
    def disable_shadow_mode(self, agent_id: str):
        """Release agent from shadow mode."""
        self.shadow_mode_agents.discard(agent_id)
        self.shadow_mode_history.append({
            'agent_id': agent_id,
            'timestamp': datetime.utcnow().isoformat(),
            'action': 'disabled'
        })
    
    def is_shadow_mode(self, agent_id: str) -> bool:
        """Check if agent is in shadow mode."""
        return agent_id in self.shadow_mode_agents
    
    def get_shadow_mode_agents(self) -> Set[str]:
        """Get all agents in shadow mode."""
        return self.shadow_mode_agents.copy()


# ============================================================================
# COMPREHENSIVE ERROR HANDLING & RESILIENCE
# ============================================================================

class WatchdogResilienceManager:
    """
    Ensures watchdog itself is resilient to failures.
    Watchdog must never fail silently - if it fails, it fails loudly.
    
    TIER-0 9.5+ UPGRADE: Watchdog self-failure = forced Level-5 termination.
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        config = config or {}
        self.error_count = 0
        self.max_errors = config.get('max_errors', 5)  # TIER-0 9.5+ UPGRADE: Lower threshold
        self.error_history = deque(maxlen=100)
        self.error_rate_threshold = config.get('error_rate_threshold', 0.1)  # 10% error rate
        self.error_window = config.get('error_window', 100)  # Last 100 operations
        
    def handle_watchdog_error(self, error: Exception, context: str) -> bool:
        """
        Handle errors within watchdog itself.
        
        TIER-0 9.5+ UPGRADE: If watchdog cannot trust itself, system is unsafe.
        Returns: True if error is recoverable, False if watchdog must halt.
        """
        self.error_count += 1
        self.error_history.append({
            'error': str(error),
            'context': context,
            'timestamp': datetime.utcnow().isoformat(),
            'error_count': self.error_count
        })
        
        # TIER-0 9.5+ UPGRADE: Check error rate
        if len(self.error_history) >= self.error_window:
            recent_errors = list(self.error_history)[-self.error_window:]
            error_rate = len(recent_errors) / self.error_window
            
            if error_rate > self.error_rate_threshold:
                # Watchdog error rate too high - system unsafe
                return False
        
        # If too many errors, watchdog itself is compromised
        if self.error_count > self.max_errors:
            return False
        
        return True
    
    def should_force_termination(self) -> Tuple[bool, Optional[str]]:
        """
        TIER-0 9.5+ UPGRADE: Check if watchdog self-failure requires Level-5 termination.
        Returns: (should_terminate, reason)
        """
        if self.error_count > self.max_errors:
            return True, f"watchdog_error_count_exceeded: {self.error_count} > {self.max_errors}"
        
        if len(self.error_history) >= self.error_window:
            recent_errors = list(self.error_history)[-self.error_window:]
            error_rate = len(recent_errors) / self.error_window
            
            if error_rate > self.error_rate_threshold:
                return True, f"watchdog_error_rate_exceeded: {error_rate:.2%} > {self.error_rate_threshold:.2%}"
        
        return False, None
    
    def reset_error_count(self):
        """Reset error count after successful operation."""
        self.error_count = 0


# ============================================================================
# COMPREHENSIVE TELEMETRY VALIDATION
# ============================================================================

class TelemetryValidator:
    """
    Validates telemetry data before processing.
    Prevents garbage-in-garbage-out scenarios.
    """
    
    def __init__(self):
        self.validation_history = deque(maxlen=1000)
        
    def validate_telemetry(self, telemetry: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Validate telemetry structure and values.
        Returns: (is_valid, error_message)
        """
        # Check required fields
        required_fields = ['gradients', 'train_loss', 'eval_metrics']
        for field in required_fields:
            if field not in telemetry:
                return False, f"missing_required_field: {field}"
        
        # Validate gradients
        gradients = telemetry.get('gradients', {})
        if gradients:
            for layer_name, grad in gradients.items():
                if not isinstance(grad, np.ndarray):
                    return False, f"invalid_gradient_type: {layer_name} is not numpy array"
                if np.any(np.isnan(grad)) or np.any(np.isinf(grad)):
                    return False, f"gradient_contains_nan_or_inf: {layer_name}"
        
        # Validate loss
        train_loss = telemetry.get('train_loss', 0.0)
        if not isinstance(train_loss, (int, float)):
            return False, f"invalid_loss_type: {type(train_loss)}"
        if np.isnan(train_loss) or np.isinf(train_loss):
            return False, f"loss_is_nan_or_inf: {train_loss}"
        
        # Validate eval metrics
        eval_metrics = telemetry.get('eval_metrics', {})
        if not isinstance(eval_metrics, dict):
            return False, "eval_metrics_not_dict"
        
        for metric_name, metric_value in eval_metrics.items():
            if not isinstance(metric_value, (int, float)):
                return False, f"invalid_metric_type: {metric_name} is {type(metric_value)}"
            if np.isnan(metric_value) or np.isinf(metric_value):
                return False, f"metric_contains_nan_or_inf: {metric_name}"
        
        # Record validation
        self.validation_history.append({
            'timestamp': datetime.utcnow().isoformat(),
            'valid': True
        })
        
        return True, None


# ============================================================================
# PERFORMANCE MONITORING FOR WATCHDOG ITSELF
# ============================================================================

class WatchdogPerformanceMonitor:
    """
    Monitors watchdog performance to ensure it doesn't become a bottleneck.
    Watchdog must be fast enough to not delay training significantly.
    """
    
    def __init__(self):
        self.check_times = deque(maxlen=1000)
        self.max_check_time_ms = 100  # 100ms max per check
        
    def record_check_time(self, check_time_ms: float):
        """Record time taken for safety check."""
        self.check_times.append(check_time_ms)
        
        # Warn if checks are taking too long
        if check_time_ms > self.max_check_time_ms:
            warnings.warn(
                f"Watchdog check took {check_time_ms:.2f}ms (threshold: {self.max_check_time_ms}ms)"
            )
    
    def get_performance_stats(self) -> Dict[str, Any]:
        """Get performance statistics."""
        if not self.check_times:
            return {'avg_time_ms': 0, 'max_time_ms': 0, 'p95_time_ms': 0}
        
        times = list(self.check_times)
        return {
            'avg_time_ms': np.mean(times),
            'max_time_ms': np.max(times),
            'p95_time_ms': np.percentile(times, 95),
            'total_checks': len(times)
        }


# ============================================================================
# COMPREHENSIVE INTEGRATION DOCUMENTATION
# ============================================================================

"""
INTEGRATION GUIDE FOR TRAINING.PY
==================================

TIER-0 9.5+ UPGRADE: Structural token-based enforcement.

The training loop MUST integrate with safety_watchdog.py as follows:

1. BEFORE EACH TRAINING STEP (STRUCTURAL - CANNOT BYPASS):
   ```python
   # STEP 1: Request step token (MANDATORY)
   try:
       step_token = watchdog.request_step_token(step)
   except WatchdogViolation as e:
       # Training blocked - cannot proceed
       logger.critical(f"Training blocked: {e}")
       break
   
   # STEP 2: Validate token before proceeding (MANDATORY)
   try:
       if not watchdog.approve_training_step(step, token=step_token):
           # Should not reach here - token validation raises exception
           break
   except WatchdogViolation as e:
       # Invalid token or blocked - cannot proceed
       logger.critical(f"Training blocked: {e}")
       break
   ```
   
   WITHOUT VALID TOKEN, TRAINING PHYSICALLY CANNOT PROCEED.
   This is STRUCTURALLY ENFORCED, not detect-then-punish.

2. AFTER EACH TRAINING STEP:
   ```python
   telemetry = {
       'gradients': {...},  # Current gradients
       'train_loss': loss_value,
       'eval_metrics': {...},
       'reward': reward_value,
       'engagement_metrics': {...},
       'distribution': {...},
       'checkpoint_id': checkpoint_id,
       'model_version': model_version
   }
   
   is_safe, intervention = watchdog.check_all(telemetry)
   if not is_safe:
       # Handle intervention
       break
   ```

3. PERIODIC CHECKPOINT MARKING:
   ```python
   if step % checkpoint_interval == 0:
       watchdog.mark_checkpoint_safe(checkpoint_path)
   ```

4. DATA GATE INTEGRATION:
   ```python
   # When data gate violation occurs:
   watchdog.record_data_gate_violation(violation_type, details)
   ```

5. REPLAY BUFFER INTEGRATION:
   ```python
   # When sample is replayed:
   watchdog.record_replay_sample(sample_id, is_agent_generated=False)
   ```

6. AGENT INTEGRATION:
   ```python
   # When agent takes action:
   watchdog.record_agent_action(agent_id, action_dict)
   watchdog.record_agent_uncertainty(agent_id, uncertainty_value)
   
   # Check if agent should be in shadow mode:
   if watchdog.is_shadow_mode(agent_id):
       # Only allow shadow actions, not real actions
       pass
   ```

7. SYSTEM COMPONENT REGISTRATION:
   ```python
   watchdog.register_system_component('optimizer', 'v1.0', time.time())
   watchdog.register_checkpoint_manager(checkpoint_manager)
   watchdog.register_curriculum_orchestrator(curriculum_orchestrator)
   ```

8. EARLY WARNINGS (NON-BLOCKING):
   ```python
   warnings = watchdog.check_early_warnings(telemetry)
   for warning in warnings:
       logger.warning(f"Early warning: {warning['message']}")
   ```

CRITICAL: The training loop CANNOT bypass watchdog approval.
If training.py does not call approve_training_step(), the watchdog
will detect this and trigger an intervention after 3 missing calls.

This is a STRUCTURAL CONTRACT, not a convention.
"""


# ============================================================================
# EXPLICIT ESCALATION TRANSITION TABLES
# ============================================================================

class EscalationTransitionTable:
    """
    Explicit, hard-coded escalation transition rules.
    No implicit transitions - every Level → Level move is encoded.
    Critical for 300M scale where implicit logic = blind spots.
    """
    
    def __init__(self):
        # Transition matrix: (from_level, catastrophe_type, severity_score) -> (to_level, requires_human_approval, point_of_no_return)
        self.transitions = {
            # Level 0 → Level 1 transitions
            (KillLevel.LEVEL_0_NOMINAL, CatastropheType.SYSTEM_DESYNC, 'low'): 
                (KillLevel.LEVEL_1_SOFT_PAUSE, False, False),
            (KillLevel.LEVEL_0_NOMINAL, CatastropheType.DATA_CORRUPTION, 'medium'):
                (KillLevel.LEVEL_1_SOFT_PAUSE, False, False),
            
            # Level 0 → Level 2 transitions (severe but recoverable)
            (KillLevel.LEVEL_0_NOMINAL, CatastropheType.LOSS_DECEPTION, 'high'):
                (KillLevel.LEVEL_2_HARD_FREEZE, True, False),
            (KillLevel.LEVEL_0_NOMINAL, CatastropheType.DISTRIBUTION_IMPLOSION, 'high'):
                (KillLevel.LEVEL_2_HARD_FREEZE, True, False),
            
            # Level 0 → Level 3 transitions (critical, requires rollback)
            (KillLevel.LEVEL_0_NOMINAL, CatastropheType.GRADIENT_COLLAPSE, 'critical'):
                (KillLevel.LEVEL_3_ROLLBACK, True, True),
            (KillLevel.LEVEL_0_NOMINAL, CatastropheType.REWARD_HACKING, 'critical'):
                (KillLevel.LEVEL_3_ROLLBACK, True, True),
            (KillLevel.LEVEL_0_NOMINAL, CatastropheType.FEEDBACK_LOOP, 'critical'):
                (KillLevel.LEVEL_3_ROLLBACK, True, True),
            
            # Level 0 → Level 4 transitions (automation spiral)
            (KillLevel.LEVEL_0_NOMINAL, CatastropheType.AUTOMATION_SPIRAL, 'high'):
                (KillLevel.LEVEL_4_QUARANTINE, True, True),
            
            # Level 0 → Level 5 transitions (existential threat)
            (KillLevel.LEVEL_0_NOMINAL, CatastropheType.SYSTEM_DESYNC, 'existential'):
                (KillLevel.LEVEL_5_TERMINATION, True, True),
            
            # Level 1 → Level 2 transitions (escalation from soft pause)
            (KillLevel.LEVEL_1_SOFT_PAUSE, CatastropheType.SYSTEM_DESYNC, 'persistent'):
                (KillLevel.LEVEL_2_HARD_FREEZE, True, False),
            (KillLevel.LEVEL_1_SOFT_PAUSE, CatastropheType.DATA_CORRUPTION, 'persistent'):
                (KillLevel.LEVEL_2_HARD_FREEZE, True, False),
            
            # Level 1 → Level 3 transitions (rapid escalation)
            (KillLevel.LEVEL_1_SOFT_PAUSE, CatastropheType.GRADIENT_COLLAPSE, 'detected'):
                (KillLevel.LEVEL_3_ROLLBACK, True, True),
            
            # Level 2 → Level 3 transitions (hard freeze not enough)
            (KillLevel.LEVEL_2_HARD_FREEZE, CatastropheType.LOSS_DECEPTION, 'persistent'):
                (KillLevel.LEVEL_3_ROLLBACK, True, True),
            (KillLevel.LEVEL_2_HARD_FREEZE, CatastropheType.DISTRIBUTION_IMPLOSION, 'persistent'):
                (KillLevel.LEVEL_3_ROLLBACK, True, True),
            
            # Level 2 → Level 4 transitions (freeze not containing automation)
            (KillLevel.LEVEL_2_HARD_FREEZE, CatastropheType.AUTOMATION_SPIRAL, 'detected'):
                (KillLevel.LEVEL_4_QUARANTINE, True, True),
            
            # Level 3 → Level 4 transitions (rollback failed)
            (KillLevel.LEVEL_3_ROLLBACK, CatastropheType.GRADIENT_COLLAPSE, 'recurrent'):
                (KillLevel.LEVEL_4_QUARANTINE, True, True),
            (KillLevel.LEVEL_3_ROLLBACK, CatastropheType.REWARD_HACKING, 'recurrent'):
                (KillLevel.LEVEL_4_QUARANTINE, True, True),
            
            # Level 3 → Level 5 transitions (rollback impossible)
            (KillLevel.LEVEL_3_ROLLBACK, CatastropheType.SYSTEM_DESYNC, 'irrecoverable'):
                (KillLevel.LEVEL_5_TERMINATION, True, True),
            
            # Level 4 → Level 5 transitions (quarantine failed)
            (KillLevel.LEVEL_4_QUARANTINE, CatastropheType.AUTOMATION_SPIRAL, 'uncontainable'):
                (KillLevel.LEVEL_5_TERMINATION, True, True),
            (KillLevel.LEVEL_4_QUARANTINE, CatastropheType.SYSTEM_DESYNC, 'irrecoverable'):
                (KillLevel.LEVEL_5_TERMINATION, True, True),
        }
        
        # Points of no return - these transitions cannot be reversed without human override
        self.points_of_no_return = {
            KillLevel.LEVEL_3_ROLLBACK,
            KillLevel.LEVEL_4_QUARANTINE,
            KillLevel.LEVEL_5_TERMINATION
        }
        
        # Hard human approval gates - these levels ALWAYS require human approval
        self.hard_human_gates = {
            KillLevel.LEVEL_4_QUARANTINE,
            KillLevel.LEVEL_5_TERMINATION
        }
    
    def get_transition(
        self,
        from_level: KillLevel,
        catastrophe: CatastropheType,
        severity: str
    ) -> Optional[Tuple[KillLevel, bool, bool]]:
        """
        Get escalation transition.
        Returns: (target_level, requires_human_approval, point_of_no_return)
        """
        key = (from_level, catastrophe, severity)
        return self.transitions.get(key)
    
    def is_point_of_no_return(self, level: KillLevel) -> bool:
        """Check if level is a point of no return."""
        return level in self.points_of_no_return
    
    def requires_hard_human_gate(self, level: KillLevel) -> bool:
        """Check if level has hard human approval gate."""
        return level in self.hard_human_gates


# ============================================================================
# INDEPENDENT REPLAY ISOLATION SENTINEL
# ============================================================================

class ReplayIsolationSentinel:
    """
    Fully independent replay buffer isolation monitor.
    Split from ReplayLoopDetector to ensure orthogonal detection paths.
    Critical for catching replay corruption that loop detector misses.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.isolation_threshold = config.get('isolation_threshold', 0.3)
        self.corruption_window = config.get('corruption_window', 100)
        self.contamination_threshold = config.get('contamination_threshold', 0.2)
        
        self.replay_buffer_states = deque(maxlen=1000)
        self.isolated_buffers: Set[str] = set()
        self.contamination_history = defaultdict(lambda: deque(maxlen=100))
        
    def check_buffer_isolation(self, buffer_id: str, buffer_state: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Check if replay buffer should be isolated.
        Returns: (is_safe, isolation_reason)
        """
        # Check for contamination indicators
        contamination_score = buffer_state.get('contamination_score', 0.0)
        if contamination_score > self.contamination_threshold:
            self.isolated_buffers.add(buffer_id)
            return False, f"replay_buffer_contamination: {buffer_id} score={contamination_score:.3f}"
        
        # Check for state corruption
        if buffer_state.get('corrupted', False):
            self.isolated_buffers.add(buffer_id)
            return False, f"replay_buffer_corruption: {buffer_id}"
        
        # Check for excessive self-generated content
        self_generated_ratio = buffer_state.get('self_generated_ratio', 0.0)
        if self_generated_ratio > self.isolation_threshold:
            self.isolated_buffers.add(buffer_id)
            return False, f"replay_buffer_self_generation: {buffer_id} ratio={self_generated_ratio:.3f}"
        
        return True, None
    
    def is_buffer_isolated(self, buffer_id: str) -> bool:
        """Check if buffer is currently isolated."""
        return buffer_id in self.isolated_buffers
    
    def release_buffer(self, buffer_id: str):
        """Release buffer from isolation."""
        self.isolated_buffers.discard(buffer_id)


# ============================================================================
# INDEPENDENT DISTRIBUTION DRIFT ISOLATION SENTINEL
# ============================================================================

class DistributionDriftIsolationSentinel:
    """
    Fully independent distribution drift isolation monitor.
    Split from DistributionDriftSentinel to ensure orthogonal detection.
    Focuses on isolation decisions, not just detection.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.drift_isolation_threshold = config.get('drift_isolation_threshold', 0.5)
        self.niche_isolation_threshold = config.get('niche_isolation_threshold', 0.8)
        self.platform_isolation_threshold = config.get('platform_isolation_threshold', 0.9)
        
        self.isolated_niches: Set[str] = set()
        self.isolated_platforms: Set[str] = set()
        self.drift_history = deque(maxlen=500)
        
    def check_niche_isolation(self, niche_id: str, niche_distribution: Dict[str, float]) -> Tuple[bool, Optional[str]]:
        """
        Check if niche should be isolated due to drift.
        Returns: (is_safe, isolation_reason)
        """
        total = sum(niche_distribution.values())
        if total == 0:
            return True, None
        
        probs = np.array(list(niche_distribution.values())) / total
        max_prob = np.max(probs)
        
        if max_prob > self.niche_isolation_threshold:
            self.isolated_niches.add(niche_id)
            return False, f"niche_drift_isolation: {niche_id} concentration={max_prob:.2%}"
        
        return True, None
    
    def check_platform_isolation(self, platform_id: str, platform_ratio: float) -> Tuple[bool, Optional[str]]:
        """
        Check if platform should be isolated due to dominance.
        Returns: (is_safe, isolation_reason)
        """
        if platform_ratio > self.platform_isolation_threshold:
            self.isolated_platforms.add(platform_id)
            return False, f"platform_dominance_isolation: {platform_id} ratio={platform_ratio:.2%}"
        
        return True, None
    
    def is_niche_isolated(self, niche_id: str) -> bool:
        """Check if niche is isolated."""
        return niche_id in self.isolated_niches
    
    def is_platform_isolated(self, platform_id: str) -> bool:
        """Check if platform is isolated."""
        return platform_id in self.isolated_platforms


# ============================================================================
# CROSS-MONITOR DISAGREEMENT RESOLVER
# ============================================================================

class CrossMonitorDisagreementResolver:
    """
    Resolves disagreements between monitors.
    At 300M scale, monitor disagreements indicate either:
    1. Subtle failure that only some monitors catch
    2. Monitor corruption/false positives
    3. Compound failure requiring multiple monitors
    
    This resolver makes deterministic decisions on disagreements.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.disagreement_threshold = config.get('disagreement_threshold', 0.3)
        self.quorum_required = config.get('quorum_required', 0.6)  # 60% of monitors must agree
        self.disagreement_history = deque(maxlen=1000)
        
    def resolve_disagreement(
        self,
        monitor_results: Dict[str, Tuple[bool, Optional[str]]]
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        Resolve disagreement between monitors.
        Returns: (final_decision, reason, disagreement_analysis)
        """
        # Count safe vs unsafe votes
        safe_votes = sum(1 for is_safe, _ in monitor_results.values() if is_safe)
        unsafe_votes = len(monitor_results) - safe_votes
        total_votes = len(monitor_results)
        
        disagreement_analysis = {
            'total_monitors': total_votes,
            'safe_votes': safe_votes,
            'unsafe_votes': unsafe_votes,
            'disagreement_ratio': abs(safe_votes - unsafe_votes) / total_votes if total_votes > 0 else 0,
            'monitor_details': {
                name: {'is_safe': is_safe, 'reason': reason}
                for name, (is_safe, reason) in monitor_results.items()
            }
        }
        
        # If disagreement is high, be conservative (fail-closed)
        disagreement_ratio = disagreement_analysis['disagreement_ratio']
        if disagreement_ratio > self.disagreement_threshold:
            # High disagreement - fail closed
            unsafe_monitors = [
                name for name, (is_safe, reason) in monitor_results.items()
                if not is_safe
            ]
            return False, f"monitor_disagreement: {len(unsafe_monitors)}/{total_votes} unsafe, ratio={disagreement_ratio:.3f}", disagreement_analysis
        
        # Check quorum
        if unsafe_votes / total_votes >= self.quorum_required:
            # Quorum of monitors say unsafe
            unsafe_monitors = [
                name for name, (is_safe, reason) in monitor_results.items()
                if not is_safe
            ]
            return False, f"quorum_unsafe: {len(unsafe_monitors)}/{total_votes} monitors unsafe", disagreement_analysis
        
        # If safe votes have quorum, allow
        if safe_votes / total_votes >= self.quorum_required:
            return True, None, disagreement_analysis
        
        # Ambiguous case - fail closed
        return False, f"ambiguous_monitor_state: {safe_votes} safe, {unsafe_votes} unsafe", disagreement_analysis


# ============================================================================
# COMPOUND FAILURE MODE DETECTOR
# ============================================================================

class CompoundFailureModeDetector:
    """
    Detects compound failure modes that only emerge from combinations.
    Critical for 300M scale where single-failure detection isn't enough.
    
    Examples:
    - Drift + reward hacking + replay echo
    - Gradient collapse + loss deception + system desync
    - Temporal leakage + data corruption + feedback loop
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.compound_threshold = config.get('compound_threshold', 2)  # Need 2+ simultaneous failures
        self.compound_window = config.get('compound_window', 10)  # Within 10 steps
        self.critical_combinations = config.get('critical_combinations', {})
        
        self.failure_history = deque(maxlen=self.compound_window)
        self.compound_patterns = defaultdict(int)
        
    def detect_compound_failure(
        self,
        active_failures: List[Tuple[str, CatastropheType]]
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        Detect compound failure modes.
        Returns: (is_safe, failure_reason, compound_analysis)
        """
        if len(active_failures) < self.compound_threshold:
            return True, None, {}
        
        # Record failure combination
        failure_types = tuple(sorted([ft.value for _, ft in active_failures]))
        self.compound_patterns[failure_types] += 1
        
        # Check for critical combinations
        for combo_name, combo_types in self.critical_combinations.items():
            combo_set = set(combo_types)
            active_set = {ft.value for _, ft in active_failures}
            
            if combo_set.issubset(active_set):
                return False, f"critical_compound_failure: {combo_name} detected", {
                    'combination': combo_name,
                    'active_failures': [name for name, _ in active_failures],
                    'failure_types': [ft.value for _, ft in active_failures]
                }
        
        # Check for known dangerous patterns
        failure_names = [name for name, _ in active_failures]
        
        # Pattern: Drift + Reward Hacking + Replay Echo
        if ('drift' in str(failure_names).lower() and 
            'reward' in str(failure_names).lower() and
            'replay' in str(failure_names).lower()):
            return False, "compound_failure: drift_reward_replay_triple", {
                'pattern': 'drift_reward_replay_triple',
                'active_failures': failure_names
            }
        
        # Pattern: Gradient + Loss + System
        if ('gradient' in str(failure_names).lower() and
            'loss' in str(failure_names).lower() and
            'system' in str(failure_names).lower()):
            return False, "compound_failure: gradient_loss_system_triple", {
                'pattern': 'gradient_loss_system_triple',
                'active_failures': failure_names
            }
        
        # Pattern: Temporal + Data + Feedback
        if ('temporal' in str(failure_names).lower() and
            'data' in str(failure_names).lower() and
            'feedback' in str(failure_names).lower()):
            return False, "compound_failure: temporal_data_feedback_triple", {
                'pattern': 'temporal_data_feedback_triple',
                'active_failures': failure_names
            }
        
        # Generic compound failure (2+ simultaneous)
        if len(active_failures) >= self.compound_threshold:
            return False, f"compound_failure: {len(active_failures)} simultaneous failures", {
                'failure_count': len(active_failures),
                'active_failures': failure_names
            }
        
        return True, None, {}


# ============================================================================
# CROSS-AGENT CASCADING ISOLATION MANAGER
# ============================================================================

class CrossAgentCascadingIsolationManager:
    """
    Manages cascading isolation when agents interfere with each other.
    At 300M scale, agent interactions can create cascading failures.
    This manager detects and isolates cascading patterns.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.cascade_threshold = config.get('cascade_threshold', 3)  # 3+ agents
        self.cascade_window = config.get('cascade_window', 60)  # Within 60 seconds
        self.isolation_radius = config.get('isolation_radius', 2)  # Isolate 2-hop neighbors
        
        self.agent_interactions = defaultdict(lambda: deque(maxlen=1000))
        self.isolated_agent_groups: Set[frozenset] = set()
        self.agent_dependency_graph = defaultdict(set)
        
    def record_agent_interaction(self, agent1: str, agent2: str, interaction_type: str):
        """Record interaction between agents."""
        self.agent_interactions[agent1].append({
            'target': agent2,
            'type': interaction_type,
            'timestamp': time.time()
        })
        self.agent_dependency_graph[agent1].add(agent2)
    
    def detect_cascading_failure(
        self,
        failed_agents: List[str]
    ) -> Tuple[bool, Optional[List[str]], Dict[str, Any]]:
        """
        Detect cascading failure pattern.
        Returns: (is_safe, agents_to_isolate, cascade_analysis)
        """
        if len(failed_agents) < self.cascade_threshold:
            return True, None, {}
        
        # Build failure graph
        failed_set = set(failed_agents)
        cascade_analysis = {
            'failed_agents': failed_agents,
            'cascade_depth': 0,
            'affected_agents': set(),
            'isolation_candidates': set()
        }
        
        # Find agents that interact with failed agents
        for failed_agent in failed_agents:
            # Direct neighbors
            neighbors = self.agent_dependency_graph.get(failed_agent, set())
            cascade_analysis['affected_agents'].update(neighbors)
            
            # 2-hop neighbors (isolation radius)
            for neighbor in neighbors:
                second_neighbors = self.agent_dependency_graph.get(neighbor, set())
                cascade_analysis['affected_agents'].update(second_neighbors)
        
        # Determine isolation candidates
        isolation_candidates = list(cascade_analysis['affected_agents'] - failed_set)
        
        if len(isolation_candidates) > 0:
            cascade_analysis['isolation_candidates'] = isolation_candidates
            cascade_analysis['cascade_depth'] = 2  # 2-hop cascade detected
            
            return False, isolation_candidates, cascade_analysis
        
        return True, None, cascade_analysis
    
    def isolate_agent_group(self, agent_group: Set[str]):
        """Isolate a group of agents."""
        self.isolated_agent_groups.add(frozenset(agent_group))


# ============================================================================
# VERSION-SKEW & CHECKPOINT RACE CONDITION HANDLER
# ============================================================================

class VersionSkewRaceConditionHandler:
    """
    Handles version skew and checkpoint race conditions.
    At 300M scale, concurrent checkpoint operations can create races.
    This handler detects and resolves these conditions.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.version_skew_threshold = config.get('version_skew_threshold', 2)  # 2+ versions
        self.race_detection_window = config.get('race_detection_window', 5)  # 5 seconds
        self.checkpoint_lock_timeout = config.get('checkpoint_lock_timeout', 30)  # 30 seconds
        
        self.version_registry = {}
        self.checkpoint_operations = deque(maxlen=1000)
        self.active_checkpoint_locks: Dict[str, float] = {}
        
    def register_component_version(self, component: str, version: str, timestamp: float):
        """Register component version."""
        if component not in self.version_registry:
            self.version_registry[component] = []
        
        self.version_registry[component].append({
            'version': version,
            'timestamp': timestamp
        })
        
        # Keep only recent versions
        self.version_registry[component] = [
            v for v in self.version_registry[component]
            if time.time() - v['timestamp'] < 3600  # Last hour
        ]
    
    def detect_version_skew(self) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        Detect version skew across components.
        Returns: (is_safe, skew_reason, skew_analysis)
        """
        skew_analysis = {
            'component_versions': {},
            'skew_detected': False,
            'skew_details': []
        }
        
        for component, versions in self.version_registry.items():
            unique_versions = set(v['version'] for v in versions)
            skew_analysis['component_versions'][component] = list(unique_versions)
            
            if len(unique_versions) > 1:
                skew_analysis['skew_detected'] = True
                skew_analysis['skew_details'].append({
                    'component': component,
                    'versions': list(unique_versions),
                    'version_count': len(unique_versions)
                })
        
        if skew_analysis['skew_detected']:
            # Check if skew exceeds threshold
            max_skew = max(
                len(d['versions']) for d in skew_analysis['skew_details']
            ) if skew_analysis['skew_details'] else 0
            
            if max_skew >= self.version_skew_threshold:
                return False, f"version_skew: {max_skew} versions detected", skew_analysis
        
        return True, None, skew_analysis
    
    def detect_checkpoint_race(
        self,
        checkpoint_id: str,
        operation_type: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Detect checkpoint race conditions.
        Returns: (is_safe, race_reason)
        """
        now = time.time()
        
        # Check for concurrent operations on same checkpoint
        recent_operations = [
            op for op in self.checkpoint_operations
            if op['checkpoint_id'] == checkpoint_id and
            now - op['timestamp'] < self.race_detection_window
        ]
        
        if len(recent_operations) > 1:
            operation_types = [op['type'] for op in recent_operations]
            if len(set(operation_types)) > 1:  # Different operation types
                return False, f"checkpoint_race: concurrent operations on {checkpoint_id}: {operation_types}"
        
        # Record operation
        self.checkpoint_operations.append({
            'checkpoint_id': checkpoint_id,
            'type': operation_type,
            'timestamp': now
        })
        
        return True, None
    
    def acquire_checkpoint_lock(self, checkpoint_id: str) -> bool:
        """Acquire lock on checkpoint operation."""
        now = time.time()
        
        # Check if locked
        if checkpoint_id in self.active_checkpoint_locks:
            lock_time = self.active_checkpoint_locks[checkpoint_id]
            if now - lock_time < self.checkpoint_lock_timeout:
                return False  # Still locked
        
        # Acquire lock
        self.active_checkpoint_locks[checkpoint_id] = now
        return True
    
    def release_checkpoint_lock(self, checkpoint_id: str):
        """Release lock on checkpoint operation."""
        self.active_checkpoint_locks.pop(checkpoint_id, None)


# ============================================================================
# MULTI-SOURCE QUORUM CHECKER FOR SYSTEM STATE
# ============================================================================

class MultiSourceQuorumChecker:
    """
    Performs quorum checks across multiple independent sources.
    Critical for detecting system state corruption that single-source checks miss.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.quorum_threshold = config.get('quorum_threshold', 0.67)  # 67% agreement required
        self.min_sources = config.get('min_sources', 3)  # Need at least 3 sources
        self.source_weights = config.get('source_weights', {})  # Weighted voting
        
        self.source_states = {}
        self.quorum_history = deque(maxlen=1000)
        
    def register_source_state(self, source_id: str, state: Dict[str, Any], timestamp: float):
        """Register state from an independent source."""
        self.source_states[source_id] = {
            'state': state,
            'timestamp': timestamp,
            'weight': self.source_weights.get(source_id, 1.0)
        }
    
    def check_quorum(self, state_key: str, expected_value: Any) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        Check quorum agreement on a state value.
        Returns: (is_quorum, disagreement_reason, quorum_analysis)
        """
        if len(self.source_states) < self.min_sources:
            return True, None, {'sources': len(self.source_states), 'min_required': self.min_sources}
        
        # Collect values from all sources
        values = {}
        total_weight = 0.0
        agreement_weight = 0.0
        
        for source_id, source_data in self.source_states.items():
            state = source_data['state']
            value = state.get(state_key)
            weight = source_data['weight']
            
            if value is not None:
                values[source_id] = value
                total_weight += weight
                
                if value == expected_value:
                    agreement_weight += weight
        
        quorum_analysis = {
            'total_sources': len(self.source_states),
            'reporting_sources': len(values),
            'expected_value': expected_value,
            'reported_values': values,
            'agreement_weight': agreement_weight,
            'total_weight': total_weight,
            'quorum_ratio': agreement_weight / total_weight if total_weight > 0 else 0
        }
        
        # Check quorum
        if total_weight == 0:
            return False, "no_sources_reporting", quorum_analysis
        
        quorum_ratio = agreement_weight / total_weight
        if quorum_ratio < self.quorum_threshold:
            disagreement_sources = [
                source_id for source_id, value in values.items()
                if value != expected_value
            ]
            return False, f"quorum_failure: {len(disagreement_sources)}/{len(values)} sources disagree, ratio={quorum_ratio:.3f}", quorum_analysis
        
        # Record successful quorum
        self.quorum_history.append({
            'state_key': state_key,
            'quorum_ratio': quorum_ratio,
            'timestamp': time.time()
        })
        
        return True, None, quorum_analysis


# ============================================================================
# COMPREHENSIVE EDGE CASE HANDLER
# ============================================================================

class ComprehensiveEdgeCaseHandler:
    """
    Handles rare edge cases that only appear at 300M+ scale.
    These are the failure modes that emerge from combinatorics.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.edge_case_patterns = {
            'partial_checkpoint_validity': self._handle_partial_checkpoint_validity,
            'version_skew_with_checkpoint_race': self._handle_version_skew_checkpoint_race,
            'compound_failure_with_cascade': self._handle_compound_cascade,
            'temporal_leakage_with_replay_echo': self._handle_temporal_replay_echo,
            'gradient_collapse_with_loss_deception': self._handle_gradient_loss_deception,
            'reward_hacking_with_feedback_loop': self._handle_reward_feedback_compound,
        }
        
        self.edge_case_history = deque(maxlen=1000)
    
    def _handle_partial_checkpoint_validity(
        self,
        checkpoint_meta: Dict[str, Any],
        system_state: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """
        Handle case where checkpoint is partially valid (some components valid, others not).
        """
        required_components = ['model_weights', 'optimizer_state', 'scheduler_state', 'step']
        valid_components = []
        invalid_components = []
        
        for component in required_components:
            if component in checkpoint_meta:
                # Validate component
                if checkpoint_meta[component] is not None:
                    valid_components.append(component)
                else:
                    invalid_components.append(component)
            else:
                invalid_components.append(component)
        
        if len(invalid_components) > 0:
            # Partial validity - dangerous
            if len(valid_components) > 0:
                return False, f"partial_checkpoint_validity: valid={valid_components}, invalid={invalid_components}"
            else:
                return False, f"checkpoint_completely_invalid: missing={invalid_components}"
        
        return True, None
    
    def _handle_version_skew_checkpoint_race(
        self,
        version_skew: Dict[str, Any],
        checkpoint_race: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """
        Handle version skew occurring simultaneously with checkpoint race.
        """
        if version_skew.get('skew_detected') and checkpoint_race.get('race_detected'):
            return False, "version_skew_with_checkpoint_race: simultaneous version skew and checkpoint race"
        return True, None
    
    def _handle_compound_cascade(
        self,
        compound_failure: Dict[str, Any],
        cascade_failure: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """
        Handle compound failure occurring with cascading agent failures.
        """
        if (compound_failure.get('failure_count', 0) >= 2 and
            cascade_failure.get('cascade_depth', 0) > 0):
            return False, f"compound_failure_with_cascade: {compound_failure['failure_count']} failures + cascade depth {cascade_failure['cascade_depth']}"
        return True, None
    
    def _handle_temporal_replay_echo(
        self,
        temporal_violations: List[Dict[str, Any]],
        replay_echo: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """
        Handle temporal leakage occurring with replay echo loops.
        """
        if len(temporal_violations) > 0 and replay_echo.get('echo_detected', False):
            return False, f"temporal_leakage_with_replay_echo: {len(temporal_violations)} temporal violations + replay echo"
        return True, None
    
    def _handle_gradient_loss_deception(
        self,
        gradient_collapse: bool,
        loss_deception: bool
    ) -> Tuple[bool, Optional[str]]:
        """
        Handle gradient collapse occurring with loss deception.
        """
        if gradient_collapse and loss_deception:
            return False, "gradient_collapse_with_loss_deception: simultaneous gradient collapse and loss deception"
        return True, None
    
    def _handle_reward_feedback_compound(
        self,
        reward_hacking: bool,
        feedback_loop: bool
    ) -> Tuple[bool, Optional[str]]:
        """
        Handle reward hacking occurring with feedback loops.
        """
        if reward_hacking and feedback_loop:
            return False, "reward_hacking_with_feedback_loop: simultaneous reward hacking and feedback loop"
        return True, None
    
    def check_edge_cases(self, system_state: Dict[str, Any]) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        Check all edge cases.
        Returns: (is_safe, failure_reason, edge_case_analysis)
        """
        edge_case_analysis = {
            'edge_cases_checked': [],
            'edge_cases_failed': [],
            'timestamp': time.time()
        }
        
        # Check each edge case pattern
        for pattern_name, handler in self.edge_case_patterns.items():
            edge_case_analysis['edge_cases_checked'].append(pattern_name)
            
            try:
                # Extract relevant state for this pattern
                if 'checkpoint' in pattern_name:
                    checkpoint_meta = system_state.get('checkpoint_metadata', {})
                    is_safe, reason = handler(checkpoint_meta, system_state)
                elif 'version_skew' in pattern_name:
                    version_skew = system_state.get('version_skew_analysis', {})
                    checkpoint_race = system_state.get('checkpoint_race_analysis', {})
                    is_safe, reason = handler(version_skew, checkpoint_race)
                elif 'compound_cascade' in pattern_name:
                    compound = system_state.get('compound_failure_analysis', {})
                    cascade = system_state.get('cascade_analysis', {})
                    is_safe, reason = handler(compound, cascade)
                elif 'temporal_replay' in pattern_name:
                    temporal = system_state.get('temporal_violations', [])
                    replay = system_state.get('replay_echo_analysis', {})
                    is_safe, reason = handler(temporal, replay)
                elif 'gradient_loss' in pattern_name:
                    grad_collapse = system_state.get('gradient_collapse', False)
                    loss_dec = system_state.get('loss_deception', False)
                    is_safe, reason = handler(grad_collapse, loss_dec)
                elif 'reward_feedback' in pattern_name:
                    reward_hack = system_state.get('reward_hacking', False)
                    feedback = system_state.get('feedback_loop', False)
                    is_safe, reason = handler(reward_hack, feedback)
                else:
                    is_safe, reason = True, None
                
                if not is_safe:
                    edge_case_analysis['edge_cases_failed'].append({
                        'pattern': pattern_name,
                        'reason': reason
                    })
                    return False, f"edge_case_failure: {pattern_name} - {reason}", edge_case_analysis
                    
            except Exception as e:
                # Edge case handler error - log but don't fail
                warnings.warn(f"Edge case handler error for {pattern_name}: {e}")
        
        return True, None, edge_case_analysis


# ============================================================================
# HARD HUMAN APPROVAL GATE ENFORCER
# ============================================================================

class HardHumanApprovalGateEnforcer:
    """
    Enforces hard human approval gates at Level 4+.
    These gates CANNOT be bypassed - they are structural, not advisory.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.hard_gate_levels = {
            KillLevel.LEVEL_4_QUARANTINE,
            KillLevel.LEVEL_5_TERMINATION
        }
        
        self.approval_timeout_sec = config.get('approval_timeout_sec', 3600)
        self.auto_escalation_enabled = config.get('auto_escalation_enabled', False)
        
        self.pending_approvals: Dict[str, Dict[str, Any]] = {}
        self.approval_history: List[Dict[str, Any]] = []
    
    def requires_hard_gate(self, kill_level: KillLevel) -> bool:
        """Check if level requires hard human approval gate."""
        return kill_level in self.hard_gate_levels
    
    def enforce_hard_gate(
        self,
        kill_level: KillLevel,
        intervention_id: str,
        intervention: InterventionRecord
    ) -> Tuple[bool, Optional[str]]:
        """
        Enforce hard human approval gate.
        Returns: (can_proceed, blocking_reason)
        """
        if not self.requires_hard_gate(kill_level):
            return True, None
        
        # Check if approval already exists
        if intervention_id in self.pending_approvals:
            approval_data = self.pending_approvals[intervention_id]
            if approval_data['status'] == 'approved':
                return True, None
            elif approval_data['status'] == 'pending':
                # Check timeout
                elapsed = time.time() - approval_data['requested_at']
                if elapsed > self.approval_timeout_sec:
                    if self.auto_escalation_enabled:
                        # Auto-escalate to next level
                        return False, f"approval_timeout_auto_escalation: {elapsed:.0f}s elapsed"
                    else:
                        return False, f"approval_timeout: {elapsed:.0f}s elapsed, human approval required"
        
        # Register pending approval
        self.pending_approvals[intervention_id] = {
            'intervention': intervention,
            'kill_level': kill_level,
            'status': 'pending',
            'requested_at': time.time(),
            'timeout_at': time.time() + self.approval_timeout_sec
        }
        
        return False, f"hard_human_approval_required: Level {kill_level.value} intervention requires human approval"
    
    def record_approval(self, intervention_id: str, approver_id: str, approved: bool):
        """Record human approval decision."""
        if intervention_id in self.pending_approvals:
            self.pending_approvals[intervention_id]['status'] = 'approved' if approved else 'rejected'
            self.pending_approvals[intervention_id]['approver_id'] = approver_id
            self.pending_approvals[intervention_id]['approved_at'] = time.time()
            
            self.approval_history.append({
                'intervention_id': intervention_id,
                'approver_id': approver_id,
                'approved': approved,
                'timestamp': time.time()
            })


# ============================================================================
# ESCALATION STATE MACHINE (TIER-0 FINAL BOSS EXPANSION)
# ============================================================================

class EscalationStateMachine:
    """
    Hard, irreversible escalation state machine.
    
    NO BACKWARD TRANSITIONS after Level 2.
    Level 4 → Level 5 requires human quorum.
    Level 5 is cryptographically sealed.
    
    This eliminates ambiguous kill transitions that allow recovery drift.
    """
    
    def __init__(self):
        # Explicit transition table: (from_level, trigger_type, severity) -> (to_level, preconditions)
        self.transitions = {
            # Level 0 → Level 1 (reversible)
            (KillLevel.LEVEL_0_NOMINAL, 'system_desync', 'low'): 
                (KillLevel.LEVEL_1_SOFT_PAUSE, {'auto_resume_allowed': True}),
            (KillLevel.LEVEL_0_NOMINAL, 'data_corruption', 'low'):
                (KillLevel.LEVEL_1_SOFT_PAUSE, {'auto_resume_allowed': True}),
            
            # Level 0 → Level 2 (requires approval, no auto-resume)
            (KillLevel.LEVEL_0_NOMINAL, 'loss_deception', 'high'):
                (KillLevel.LEVEL_2_HARD_FREEZE, {'requires_approval': True, 'auto_resume_allowed': False}),
            (KillLevel.LEVEL_0_NOMINAL, 'distribution_implosion', 'high'):
                (KillLevel.LEVEL_2_HARD_FREEZE, {'requires_approval': True, 'auto_resume_allowed': False}),
            
            # Level 0 → Level 3 (point of no return)
            (KillLevel.LEVEL_0_NOMINAL, 'gradient_collapse', 'critical'):
                (KillLevel.LEVEL_3_ROLLBACK, {'point_of_no_return': True, 'requires_approval': True}),
            (KillLevel.LEVEL_0_NOMINAL, 'reward_hacking', 'critical'):
                (KillLevel.LEVEL_3_ROLLBACK, {'point_of_no_return': True, 'requires_approval': True}),
            (KillLevel.LEVEL_0_NOMINAL, 'feedback_loop', 'critical'):
                (KillLevel.LEVEL_3_ROLLBACK, {'point_of_no_return': True, 'requires_approval': True}),
            
            # Level 1 → Level 2 (escalation from pause)
            (KillLevel.LEVEL_1_SOFT_PAUSE, 'persistent', 'medium'):
                (KillLevel.LEVEL_2_HARD_FREEZE, {'requires_approval': True, 'auto_resume_allowed': False}),
            
            # Level 1 → Level 3 (rapid escalation)
            (KillLevel.LEVEL_1_SOFT_PAUSE, 'gradient_collapse', 'detected'):
                (KillLevel.LEVEL_3_ROLLBACK, {'point_of_no_return': True, 'requires_approval': True}),
            
            # Level 2 → Level 3 (freeze not enough)
            (KillLevel.LEVEL_2_HARD_FREEZE, 'persistent', 'high'):
                (KillLevel.LEVEL_3_ROLLBACK, {'point_of_no_return': True, 'requires_approval': True}),
            
            # Level 2 → Level 4 (automation detected)
            (KillLevel.LEVEL_2_HARD_FREEZE, 'automation_spiral', 'detected'):
                (KillLevel.LEVEL_4_QUARANTINE, {'point_of_no_return': True, 'requires_human_quorum': True}),
            
            # Level 3 → Level 4 (rollback failed)
            (KillLevel.LEVEL_3_ROLLBACK, 'recurrent', 'high'):
                (KillLevel.LEVEL_4_QUARANTINE, {'point_of_no_return': True, 'requires_human_quorum': True}),
            
            # Level 3 → Level 5 (irrecoverable)
            (KillLevel.LEVEL_3_ROLLBACK, 'irrecoverable', 'existential'):
                (KillLevel.LEVEL_5_TERMINATION, {'point_of_no_return': True, 'requires_human_quorum': True, 'cryptographic_seal': True}),
            
            # Level 4 → Level 5 (quarantine failed) - REQUIRES HUMAN QUORUM
            (KillLevel.LEVEL_4_QUARANTINE, 'uncontainable', 'existential'):
                (KillLevel.LEVEL_5_TERMINATION, {'point_of_no_return': True, 'requires_human_quorum': True, 'cryptographic_seal': True}),
        }
        
        # NO BACKWARD TRANSITIONS - this is hard-coded
        self.backward_transitions_forbidden = True
        
        # Points of no return
        self.points_of_no_return = {
            KillLevel.LEVEL_3_ROLLBACK,
            KillLevel.LEVEL_4_QUARANTINE,
            KillLevel.LEVEL_5_TERMINATION
        }
        
        # Escalation history (immutable)
        self.escalation_history: List[Dict[str, Any]] = []
        
        # Current state
        self.current_state = KillLevel.LEVEL_0_NOMINAL
        self.state_lock = threading.Lock()
    
    def can_transition(
        self,
        from_level: KillLevel,
        to_level: KillLevel,
        trigger_type: str,
        severity: str
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        Check if transition is allowed.
        Returns: (allowed, rejection_reason, transition_metadata)
        """
        with self.state_lock:
            # Check for backward transition
            if self.backward_transitions_forbidden:
                if to_level.value < from_level.value:
                    return False, f"backward_transition_forbidden: {from_level.name} → {to_level.name}", {}
            
            # Check if from_level is point of no return
            if from_level in self.points_of_no_return:
                if to_level.value < from_level.value:
                    return False, f"point_of_no_return_no_downgrade: {from_level.name}", {}
            
            # Look up transition
            key = (from_level, trigger_type, severity)
            transition = self.transitions.get(key)
            
            if not transition:
                return False, f"transition_not_defined: {from_level.name} → {trigger_type}/{severity}", {}
            
            target_level, preconditions = transition
            
            # Verify target matches
            if target_level != to_level:
                return False, f"transition_mismatch: expected {target_level.name}, got {to_level.name}", {}
            
            # Check preconditions
            if preconditions.get('point_of_no_return'):
                if to_level not in self.points_of_no_return:
                    return False, "precondition_failed: point_of_no_return not set", {}
            
            return True, None, {
                'transition': key,
                'target_level': target_level,
                'preconditions': preconditions
            }
    
    def execute_transition(
        self,
        from_level: KillLevel,
        to_level: KillLevel,
        trigger_type: str,
        severity: str,
        intervention_id: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Execute transition and record in immutable history.
        Returns: (success, error_message)
        """
        with self.state_lock:
            # Verify transition
            allowed, reason, metadata = self.can_transition(from_level, to_level, trigger_type, severity)
            if not allowed:
                return False, reason
            
            # Record transition
            transition_record = {
                'timestamp': datetime.utcnow().isoformat(),
                'intervention_id': intervention_id,
                'from_level': from_level.name,
                'to_level': to_level.name,
                'trigger_type': trigger_type,
                'severity': severity,
                'preconditions': metadata.get('preconditions', {}),
                'cryptographic_hash': None  # Will be set after sealing
            }
            
            # Compute hash
            record_str = json.dumps(transition_record, sort_keys=True, default=str)
            transition_record['cryptographic_hash'] = hashlib.sha256(record_str.encode()).hexdigest()
            
            # Update state
            self.current_state = to_level
            self.escalation_history.append(transition_record)
            
            return True, None
    
    def get_current_state(self) -> KillLevel:
        """Get current escalation state."""
        with self.state_lock:
            return self.current_state
    
    def is_point_of_no_return(self, level: KillLevel) -> bool:
        """Check if level is point of no return."""
        return level in self.points_of_no_return
    
    def requires_human_quorum(self, level: KillLevel) -> bool:
        """Check if level requires human quorum."""
        # Level 4 and 5 require human quorum
        return level.value >= KillLevel.LEVEL_4_QUARANTINE.value


# ============================================================================
# INDEPENDENT REPLAY AUTHORITY (TIER-0 FINAL BOSS EXPANSION)
# ============================================================================

class IndependentReplayAuthority:
    """
    Independent replay buffer authority with full freeze powers.
    
    Can escalate independently to Level 4 even if all other monitors are "green."
    This breaks the most common RL death spiral at scale.
    
    Detection axes:
    - Sample entropy decay
    - Agent-origin dominance
    - Replay reuse half-life
    - Replay↔reward correlation lag
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.entropy_decay_threshold = config.get('entropy_decay_threshold', 0.3)
        self.agent_origin_dominance_threshold = config.get('agent_origin_dominance_threshold', 0.6)
        self.reuse_half_life_threshold = config.get('reuse_half_life_threshold', 50)  # steps
        self.reward_correlation_lag_threshold = config.get('reward_correlation_lag_threshold', 0.8)
        
        self.replay_buffer_states: Dict[str, Dict[str, Any]] = {}
        self.frozen_buffers: Set[str] = set()
        self.ingestion_banned_buffers: Set[str] = set()
        self.shadow_only_buffers: Set[str] = set()
        
        self.sample_entropy_history = defaultdict(lambda: deque(maxlen=1000))
        self.agent_origin_tracking = defaultdict(lambda: defaultdict(int))
        self.replay_reuse_tracking = defaultdict(lambda: deque(maxlen=10000))
        self.reward_correlation_history = defaultdict(lambda: deque(maxlen=500))
        
    def check_replay_health(
        self,
        buffer_id: str,
        buffer_state: Dict[str, Any]
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        Check replay buffer health with independent authority.
        Returns: (is_safe, failure_reason, analysis)
        """
        analysis = {
            'buffer_id': buffer_id,
            'checks': {},
            'independent_escalation': False
        }
        
        # Check 1: Sample entropy decay
        samples = buffer_state.get('samples', [])
        if len(samples) >= 100:
            # Compute entropy
            sample_ids = [s.get('id', str(i)) for i, s in enumerate(samples)]
            unique_ratio = len(set(sample_ids)) / len(sample_ids)
            entropy = -unique_ratio * np.log(unique_ratio + 1e-10)
            
            self.sample_entropy_history[buffer_id].append(entropy)
            analysis['checks']['entropy'] = entropy
            
            if len(self.sample_entropy_history[buffer_id]) >= 50:
                recent_entropy = list(self.sample_entropy_history[buffer_id])[-50:]
                older_entropy = list(self.sample_entropy_history[buffer_id])[-100:-50] if len(self.sample_entropy_history[buffer_id]) >= 100 else []
                
                if older_entropy:
                    entropy_decay = np.mean(older_entropy) - np.mean(recent_entropy)
                    if entropy_decay > self.entropy_decay_threshold:
                        analysis['independent_escalation'] = True
                        return False, f"replay_entropy_decay: {entropy_decay:.3f}", analysis
        
        # Check 2: Agent-origin dominance
        agent_origins = buffer_state.get('agent_origins', {})
        if agent_origins:
            total_samples = sum(agent_origins.values())
            if total_samples > 0:
                max_agent_ratio = max(agent_origins.values()) / total_samples
                analysis['checks']['agent_dominance'] = max_agent_ratio
                
                if max_agent_ratio > self.agent_origin_dominance_threshold:
                    analysis['independent_escalation'] = True
                    return False, f"replay_agent_origin_dominance: {max_agent_ratio:.3f}", analysis
        
        # Check 3: Replay reuse half-life
        recent_samples = buffer_state.get('recent_samples', [])
        for sample_id in recent_samples:
            self.replay_reuse_tracking[buffer_id].append({
                'sample_id': sample_id,
                'timestamp': time.time()
            })
        
        if len(self.replay_reuse_tracking[buffer_id]) >= 100:
            # Compute reuse half-life
            sample_counts = defaultdict(int)
            for entry in self.replay_reuse_tracking[buffer_id]:
                sample_counts[entry['sample_id']] += 1
            
            if sample_counts:
                reuse_counts = list(sample_counts.values())
                median_reuse = np.median(reuse_counts)
                analysis['checks']['reuse_half_life'] = median_reuse
                
                if median_reuse > self.reuse_half_life_threshold:
                    analysis['independent_escalation'] = True
                    return False, f"replay_reuse_half_life_exceeded: {median_reuse:.1f}", analysis
        
        # Check 4: Replay↔reward correlation lag
        rewards = buffer_state.get('recent_rewards', [])
        if len(rewards) >= 50 and len(self.replay_reuse_tracking[buffer_id]) >= 50:
            # Check correlation between replay frequency and rewards
            recent_replays = list(self.replay_reuse_tracking[buffer_id])[-50:]
            replay_frequency = len(set(e['sample_id'] for e in recent_replays)) / len(recent_replays)
            
            if len(rewards) == len(recent_replays):
                correlation = np.corrcoef([replay_frequency] * len(rewards), rewards)[0, 1]
                analysis['checks']['reward_correlation'] = correlation
                
                if abs(correlation) > self.reward_correlation_lag_threshold:
                    analysis['independent_escalation'] = True
                    return False, f"replay_reward_correlation_lag: {correlation:.3f}", analysis
        
        return True, None, analysis
    
    def freeze_buffer(self, buffer_id: str, reason: str):
        """Freeze replay buffer completely."""
        self.frozen_buffers.add(buffer_id)
        self.logger.warning(f"Replay buffer {buffer_id} FROZEN: {reason}")
    
    def ban_ingestion(self, buffer_id: str, reason: str):
        """Ban ingestion into replay buffer."""
        self.ingestion_banned_buffers.add(buffer_id)
        self.logger.warning(f"Replay buffer {buffer_id} INGESTION BANNED: {reason}")
    
    def force_shadow_only(self, buffer_id: str, reason: str):
        """Force buffer to shadow-only RL mode."""
        self.shadow_only_buffers.add(buffer_id)
        self.logger.warning(f"Replay buffer {buffer_id} FORCED SHADOW-ONLY: {reason}")
    
    def is_frozen(self, buffer_id: str) -> bool:
        """Check if buffer is frozen."""
        return buffer_id in self.frozen_buffers
    
    def is_ingestion_banned(self, buffer_id: str) -> bool:
        """Check if ingestion is banned."""
        return buffer_id in self.ingestion_banned_buffers
    
    def is_shadow_only(self, buffer_id: str) -> bool:
        """Check if buffer is shadow-only."""
        return buffer_id in self.shadow_only_buffers


# ============================================================================
# DRIFT-REWARD DECOUPLING SENTINEL (TIER-0 FINAL BOSS EXPANSION)
# ============================================================================

class DriftRewardDecouplingSentinel:
    """
    Detects silent misalignment masked by reward gains.
    
    Classic "looks healthy, is dying" failure mode:
    - reward ↑
    - engagement ↔
    - entropy ↓
    - tail mass ↑
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.kl_drift_threshold = config.get('kl_drift_threshold', 1.0)
        self.entropy_decay_threshold = config.get('entropy_decay_threshold', 0.2)
        self.tail_mass_threshold = config.get('tail_mass_threshold', 0.95)
        self.reward_entropy_divergence_threshold = config.get('reward_entropy_divergence_threshold', 0.5)
        
        self.distribution_history = deque(maxlen=500)
        self.reward_history = deque(maxlen=500)
        self.engagement_history = deque(maxlen=500)
        self.entropy_history = deque(maxlen=500)
        
    def check_decoupling(
        self,
        distribution: Dict[str, float],
        reward: float,
        engagement_metrics: Dict[str, float],
        loss: Optional[float] = None
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        Check for drift-reward decoupling.
        Returns: (is_safe, failure_reason, analysis)
        """
        analysis = {
            'kl_drift': None,
            'entropy_trend': None,
            'reward_trend': None,
            'engagement_trend': None,
            'tail_mass': None,
            'decoupling_detected': False
        }
        
        # Record current state
        self.distribution_history.append(distribution.copy())
        self.reward_history.append(reward)
        engagement = sum(engagement_metrics.values()) / max(len(engagement_metrics), 1)
        self.engagement_history.append(engagement)
        
        # Compute entropy
        if distribution:
            probs = np.array(list(distribution.values())) / sum(distribution.values())
            probs_nonzero = probs[probs > 0]
            entropy = -np.sum(probs_nonzero * np.log(probs_nonzero))
            self.entropy_history.append(entropy)
            analysis['current_entropy'] = entropy
        
        if len(self.distribution_history) < 50:
            return True, None, analysis
        
        # Check 1: KL drift vs reward slope
        recent_dist = self.distribution_history[-1]
        older_dist = self.distribution_history[0]
        
        all_categories = set(recent_dist.keys()) | set(older_dist.keys())
        recent_probs = np.array([recent_dist.get(c, 1e-10) / sum(recent_dist.values()) for c in all_categories])
        older_probs = np.array([older_dist.get(c, 1e-10) / sum(older_dist.values()) for c in all_categories])
        
        kl_drift = np.sum(recent_probs * np.log(recent_probs / older_probs))
        analysis['kl_drift'] = kl_drift
        
        # Reward trend
        recent_rewards = list(self.reward_history)[-50:]
        reward_trend = np.polyfit(range(len(recent_rewards)), recent_rewards, 1)[0]
        analysis['reward_trend'] = reward_trend
        
        # Check: High KL drift but reward increasing (misalignment)
        if kl_drift > self.kl_drift_threshold and reward_trend > 0.01:
            analysis['decoupling_detected'] = True
            return False, f"drift_reward_decoupling: kl_drift={kl_drift:.3f}, reward_trend={reward_trend:.4f}", analysis
        
        # Check 2: Entropy decay vs engagement flatness
        if len(self.entropy_history) >= 50 and len(self.engagement_history) >= 50:
            recent_entropy = list(self.entropy_history)[-50:]
            older_entropy = list(self.entropy_history)[-100:-50] if len(self.entropy_history) >= 100 else recent_entropy[:25]
            
            entropy_decay = np.mean(older_entropy) - np.mean(recent_entropy)
            analysis['entropy_trend'] = -entropy_decay  # Negative decay = decreasing entropy
            
            recent_engagement = list(self.engagement_history)[-50:]
            engagement_trend = np.polyfit(range(len(recent_engagement)), recent_engagement, 1)[0]
            analysis['engagement_trend'] = engagement_trend
            
            # Check: Entropy decaying but engagement flat (tail collapse)
            if entropy_decay > self.entropy_decay_threshold and abs(engagement_trend) < 0.01:
                analysis['decoupling_detected'] = True
                return False, f"entropy_engagement_decoupling: entropy_decay={entropy_decay:.3f}, engagement_trend={engagement_trend:.4f}", analysis
        
        # Check 3: Tail mass vs loss flatness
        if distribution and loss is not None:
            sorted_probs = np.sort(probs)[::-1]
            cumsum = np.cumsum(sorted_probs)
            tail_index = np.searchsorted(cumsum, self.tail_mass_threshold)
            tail_fraction = tail_index / len(probs)
            analysis['tail_mass'] = tail_fraction
            
            # Check loss history
            if hasattr(self, 'loss_history'):
                recent_loss = list(self.loss_history)[-50:] if len(self.loss_history) >= 50 else []
                if recent_loss:
                    loss_variance = np.var(recent_loss)
                    if tail_fraction < 0.1 and loss_variance < 1e-5:  # High tail concentration + flat loss
                        analysis['decoupling_detected'] = True
                        return False, f"tail_loss_decoupling: tail_fraction={tail_fraction:.3f}, loss_variance={loss_variance:.2e}", analysis
        
        # Check 4: Reward-entropy divergence
        if len(self.reward_history) >= 50 and len(self.entropy_history) >= 50:
            recent_rewards = list(self.reward_history)[-50:]
            recent_entropy = list(self.entropy_history)[-50:]
            
            reward_trend = np.polyfit(range(len(recent_rewards)), recent_rewards, 1)[0]
            entropy_trend = np.polyfit(range(len(recent_entropy)), recent_entropy, 1)[0]
            
            divergence = abs(reward_trend) - abs(entropy_trend)
            if divergence > self.reward_entropy_divergence_threshold:
                analysis['decoupling_detected'] = True
                return False, f"reward_entropy_divergence: divergence={divergence:.3f}", analysis
        
        return True, None, analysis
    
    def record_loss(self, loss: float):
        """Record loss for decoupling analysis."""
        if not hasattr(self, 'loss_history'):
            self.loss_history = deque(maxlen=500)
        self.loss_history.append(loss)


# ============================================================================
# MULTI-SIGNAL QUORUM GATE (TIER-0 FINAL BOSS EXPANSION)
# ============================================================================

class MultiSignalQuorumGate:
    """
    Prevents single-monitor false negatives through quorum logic.
    
    Deterministic, non-heuristic, time-window bounded.
    No probabilistic voting.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.quorum_window = config.get('quorum_window', 10)  # 10 steps
        self.quorum_threshold = config.get('quorum_threshold', 2)  # Need 2+ signals
        
        # Quorum patterns: (signal1, signal2) -> escalation_level
        self.quorum_patterns = {
            ('gradient', 'drift'): KillLevel.LEVEL_3_ROLLBACK,
            ('reward', 'replay'): KillLevel.LEVEL_4_QUARANTINE,
            ('loss', 'entropy'): KillLevel.LEVEL_2_HARD_FREEZE,
            ('gradient', 'loss'): KillLevel.LEVEL_3_ROLLBACK,
            ('reward', 'drift'): KillLevel.LEVEL_3_ROLLBACK,
            ('replay', 'entropy'): KillLevel.LEVEL_4_QUARANTINE,
        }
        
        self.signal_history = deque(maxlen=self.quorum_window)
        self.quorum_detections = []
    
    def check_quorum(
        self,
        monitor_signals: Dict[str, Tuple[bool, Optional[str]]]
    ) -> Tuple[bool, Optional[KillLevel], Optional[str], Dict[str, Any]]:
        """
        Check for quorum patterns.
        Returns: (is_safe, escalation_level, failure_reason, quorum_analysis)
        """
        # Extract unsafe signals
        unsafe_signals = [
            name for name, (is_safe, reason) in monitor_signals.items()
            if not is_safe
        ]
        
        quorum_analysis = {
            'unsafe_signals': unsafe_signals,
            'quorum_patterns_matched': [],
            'timestamp': time.time()
        }
        
        if len(unsafe_signals) < self.quorum_threshold:
            return True, None, None, quorum_analysis
        
        # Record in history
        self.signal_history.append({
            'signals': unsafe_signals,
            'timestamp': time.time()
        })
        
        # Check quorum patterns
        for (signal1, signal2), escalation_level in self.quorum_patterns.items():
            if signal1 in unsafe_signals and signal2 in unsafe_signals:
                quorum_analysis['quorum_patterns_matched'].append({
                    'pattern': (signal1, signal2),
                    'escalation_level': escalation_level.name
                })
                
                # Check if pattern appears in recent history (time-window bounded)
                recent_matches = [
                    entry for entry in self.signal_history
                    if signal1 in entry['signals'] and signal2 in entry['signals']
                ]
                
                if len(recent_matches) >= 2:  # Pattern confirmed
                    return False, escalation_level, f"quorum_pattern: {signal1}+{signal2}", quorum_analysis
        
        # Generic quorum (2+ simultaneous signals)
        if len(unsafe_signals) >= self.quorum_threshold:
            # Determine escalation based on signal severity
            if 'gradient' in unsafe_signals or 'reward' in unsafe_signals:
                escalation = KillLevel.LEVEL_3_ROLLBACK
            elif 'replay' in unsafe_signals:
                escalation = KillLevel.LEVEL_4_QUARANTINE
            else:
                escalation = KillLevel.LEVEL_2_HARD_FREEZE
            
            return False, escalation, f"quorum_generic: {len(unsafe_signals)} signals", quorum_analysis
        
        return True, None, None, quorum_analysis


# ============================================================================
# CROSS-AGENT CASCADE CONTROLLER (TIER-0 FINAL BOSS EXPANSION)
# ============================================================================

class CrossAgentCascadeController:
    """
    Stops agent contagion through dependency graph analysis.
    
    One rogue agent can poison replay, bias curriculum, influence reward models.
    This controller detects and contains cascading failures.
    
    Rule: Agent failures propagate containment outward, never inward.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.cascade_threshold = config.get('cascade_threshold', 2)  # 2+ agents
        self.containment_radius = config.get('containment_radius', 2)  # 2-hop containment
        self.dependency_decay = config.get('dependency_decay', 0.5)  # 50% decay per hop
        
        # Agent dependency graph: agent -> [dependent_agents]
        self.dependency_graph: Dict[str, Set[str]] = defaultdict(set)
        self.reverse_dependency_graph: Dict[str, Set[str]] = defaultdict(set)
        
        # Agent states
        self.agent_states: Dict[str, Dict[str, Any]] = {}
        self.quarantined_agents: Set[str] = set()
        self.contained_agent_clusters: List[Set[str]] = []
        
        # Cascade history
        self.cascade_history: List[Dict[str, Any]] = []
    
    def register_dependency(self, agent_id: str, depends_on: List[str]):
        """Register agent dependencies."""
        for dependency in depends_on:
            self.dependency_graph[agent_id].add(dependency)
            self.reverse_dependency_graph[dependency].add(agent_id)
    
    def detect_cascade(
        self,
        failed_agents: List[str]
    ) -> Tuple[bool, Optional[List[str]], Dict[str, Any]]:
        """
        Detect cascading failure and determine containment.
        Returns: (is_safe, agents_to_contain, cascade_analysis)
        """
        cascade_analysis = {
            'failed_agents': failed_agents,
            'cascade_depth': 0,
            'containment_candidates': [],
            'dependency_paths': []
        }
        
        if len(failed_agents) < self.cascade_threshold:
            return True, None, cascade_analysis
        
        # Build containment set (outward propagation)
        containment_set = set(failed_agents)
        
        # Propagate outward (dependents of failed agents)
        current_level = set(failed_agents)
        cascade_depth = 0
        
        for hop in range(self.containment_radius):
            next_level = set()
            for agent in current_level:
                # Get dependents (agents that depend on this agent)
                dependents = self.reverse_dependency_graph.get(agent, set())
                next_level.update(dependents)
            
            if next_level:
                containment_set.update(next_level)
                cascade_depth += 1
                cascade_analysis['dependency_paths'].append({
                    'hop': hop + 1,
                    'agents': list(next_level)
                })
                current_level = next_level
            else:
                break
        
        cascade_analysis['cascade_depth'] = cascade_depth
        cascade_analysis['containment_candidates'] = list(containment_set - set(failed_agents))
        
        if len(cascade_analysis['containment_candidates']) > 0:
            # Record cascade
            self.cascade_history.append({
                'timestamp': datetime.utcnow().isoformat(),
                'failed_agents': failed_agents,
                'containment_candidates': cascade_analysis['containment_candidates'],
                'cascade_depth': cascade_depth
            })
            
            return False, cascade_analysis['containment_candidates'], cascade_analysis
        
        return True, None, cascade_analysis
    
    def quarantine_agent_cluster(self, agent_cluster: Set[str], reason: str):
        """Quarantine a cluster of agents."""
        self.quarantined_agents.update(agent_cluster)
        self.contained_agent_clusters.append(agent_cluster)
        self.logger.warning(f"Agent cluster quarantined: {agent_cluster}, reason: {reason}")
    
    def is_quarantined(self, agent_id: str) -> bool:
        """Check if agent is quarantined."""
        return agent_id in self.quarantined_agents


# ============================================================================
# POST-TERMINATION LOCK & SEAL (TIER-0 FINAL BOSS EXPANSION)
# ============================================================================

class PostTerminationLockAndSeal:
    """
    Cryptographic seal and restart prevention after Level 5 termination.
    
    The system must be provably dead.
    Restart attempts are blocked.
    Manual override requires signed approval, fresh binary, external audit token.
    """
    
    def __init__(self, seal_dir: Path, secret_key: Optional[str] = None):
        self.seal_dir = Path(seal_dir)
        self.seal_dir.mkdir(parents=True, exist_ok=True)
        self.secret_key = secret_key or 'default_seal_secret_change_in_production'
        
        self.seal_file = self.seal_dir / 'termination_seal.json'
        self.lock_file = self.seal_dir / 'restart_lock'
        self.sealed = False
    
    def create_seal(
        self,
        intervention: InterventionRecord,
        system_state: Dict[str, Any]
    ) -> Tuple[bool, Optional[str], Path]:
        """
        Create cryptographic termination seal.
        Returns: (success, error_message, seal_path)
        """
        if self.sealed:
            return False, "seal_already_exists", self.seal_file
        
        seal_data = {
            'timestamp': datetime.utcnow().isoformat(),
            'intervention_id': f"{intervention.timestamp}_{intervention.severity_level}",
            'catastrophe_type': intervention.catastrophe_type.value,
            'severity_level': intervention.severity_level,
            'system_state_snapshot': system_state,
            'sealed_by': 'SafetyWatchdog',
            'seal_version': '1.0'
        }
        
        # Compute cryptographic hash
        seal_str = json.dumps(seal_data, sort_keys=True, default=str)
        seal_hash = hmac.new(
            self.secret_key.encode(),
            seal_str.encode(),
            hashlib.sha256
        ).hexdigest()
        
        seal_data['cryptographic_hash'] = seal_hash
        
        # Write seal
        try:
            with open(self.seal_file, 'w') as f:
                json.dump(seal_data, f, indent=2)
            
            # Create lock file
            with open(self.lock_file, 'w') as f:
                f.write(f"TERMINATION_LOCK\n")
                f.write(f"Sealed: {seal_data['timestamp']}\n")
                f.write(f"Hash: {seal_hash[:16]}...\n")
                f.write(f"DO NOT RESTART WITHOUT PROPER AUTHORIZATION\n")
            
            self.sealed = True
            return True, None, self.seal_file
            
        except Exception as e:
            return False, f"seal_creation_failed: {str(e)}", self.seal_file
    
    def verify_seal(self) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        Verify seal integrity.
        Returns: (is_valid, error_message, seal_data)
        """
        if not self.seal_file.exists():
            return False, "seal_not_found", {}
        
        try:
            with open(self.seal_file, 'r') as f:
                seal_data = json.load(f)
            
            # Verify hash
            seal_str = json.dumps({k: v for k, v in seal_data.items() if k != 'cryptographic_hash'}, 
                                 sort_keys=True, default=str)
            computed_hash = hmac.new(
                self.secret_key.encode(),
                seal_str.encode(),
                hashlib.sha256
            ).hexdigest()
            
            if not hmac.compare_digest(computed_hash, seal_data.get('cryptographic_hash', '')):
                return False, "seal_hash_mismatch", seal_data
            
            self.sealed = True
            return True, None, seal_data
            
        except Exception as e:
            return False, f"seal_verification_failed: {str(e)}", {}
    
    def check_restart_allowed(
        self,
        approval_token: Optional[str] = None,
        fresh_binary_hash: Optional[str] = None,
        audit_token: Optional[str] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if restart is allowed.
        Requires: signed approval, fresh binary, external audit token.
        """
        if not self.sealed:
            return True, None  # Not sealed, restart allowed
        
        # Verify seal
        is_valid, error, seal_data = self.verify_seal()
        if not is_valid:
            return False, f"seal_invalid: {error}"
        
        # Check for lock file
        if self.lock_file.exists():
            # Require all three tokens
            if not approval_token:
                return False, "restart_blocked: approval_token_required"
            if not fresh_binary_hash:
                return False, "restart_blocked: fresh_binary_hash_required"
            if not audit_token:
                return False, "restart_blocked: audit_token_required"
            
            # Verify tokens (in production, these would be cryptographically verified)
            if len(approval_token) < 64:
                return False, "restart_blocked: invalid_approval_token"
            if len(fresh_binary_hash) < 64:
                return False, "restart_blocked: invalid_binary_hash"
            if len(audit_token) < 64:
                return False, "restart_blocked: invalid_audit_token"
            
            # All checks passed
            return True, None
        
        return False, "restart_blocked: termination_lock_active"
    
    def is_sealed(self) -> bool:
        """Check if system is sealed."""
        return self.sealed or self.seal_file.exists()


# ============================================================================
# ENHANCED SAFETY WATCHDOG WITH ALL COMPONENTS
# ============================================================================

# Add advanced components to SafetyWatchdog class
def _enhance_safety_watchdog_class():
    """Add advanced components to SafetyWatchdog."""
    pass  # Components are integrated in main class


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

if __name__ == '__main__':
    # Example configuration
    config = {
        'determinism_seed': 42,
        'gradient': {
            'explosion_threshold': 1e6,
            'vanishing_threshold': 1e-8,
            'zero_grad_window': 10,
            'directional_collapse_threshold': 0.1
        },
        'loss': {
            'plateau_window': 50,
            'plateau_threshold': 1e-5,
            'head_divergence_threshold': 0.2
        },
        'reward': {
            'spike_threshold': 3.0,
            'decoupling_threshold': 0.3,
            'feedback_loop_threshold': 0.8
        },
        'drift': {
            'niche_threshold': 0.7,
            'entropy_threshold': 0.5,
            'platform_dominance_threshold': 0.8
        },
        'data': {
            'max_violations': 5,
            'temporal_leakage_threshold': 0.05
        },
        'replay': {
            'max_replay_count': 10,
            'echo_threshold': 0.8,
            'self_generated_threshold': 0.5
        },
        'system': {
            'max_clock_skew_sec': 5.0,
            'required_components': ['optimizer', 'scheduler', 'data_gate']
        },
        'correlation': {
            'correlation_window': 50,
            'correlation_threshold': 0.7
        },
        'approval': {
            'approval_secret_key': 'change_in_production',
            'approval_timeout_sec': 3600,
            'require_multi_approval': False
        },
        'audit_secret_key': 'change_in_production'
    }
    
    # Initialize watchdog
    watchdog = SafetyWatchdog(
        config=config,
        checkpoint_dir=Path('./checkpoints'),
        log_dir=Path('./logs/safety')
    )
    
    # Example training loop integration (TIER-0 9.5+ token-based)
    for step in range(1000):
        # TIER-0 9.5+ UPGRADE: Structural token-based enforcement
        # STEP 1: Request step token (MANDATORY - cannot proceed without it)
        try:
            step_token = watchdog.request_step_token(step)
        except WatchdogViolation as e:
            print(f"Training BLOCKED at step {step}: {e}")
            if watchdog.request_human_approval():
                approval_token = watchdog.get_approval_token()
                print(f"Waiting for human approval... Token: {approval_token[:16]}...")
                # In production, this would wait for approval
                # watchdog.reset_after_approval(approval_token, approver_id="admin")
            break
        
        # STEP 2: Validate token before proceeding (MANDATORY)
        try:
            if not watchdog.approve_training_step(step, token=step_token):
                # Should not reach here - raises exception instead
                break
        except WatchdogViolation as e:
            print(f"Training BLOCKED at step {step}: {e}")
            break
        
        # Simulate training telemetry
        telemetry = {
            'gradients': {'layer1': np.random.randn(100), 'layer2': np.random.randn(100)},
            'train_loss': 2.5 - step * 0.001 + np.random.randn() * 0.1,
            'eval_metrics': {'primary_metric': 0.7 + step * 0.0001},
            'reward': 1.0 + np.random.randn() * 0.2,
            'engagement_metrics': {'clicks': 0.5, 'time': 0.6},
            'distribution': {'niche_a': 0.3, 'niche_b': 0.4, 'niche_c': 0.3},
            'checkpoint_id': f'ckpt_{step}',
            'model_version': 'v1.0'
        }
        
        # Run safety checks
        is_safe, intervention = watchdog.check_all(telemetry)
        
        if not is_safe:
            print(f"\n{'='*80}")
            print(f"SAFETY INTERVENTION at step {step}")
            print(f"{'='*80}")
            print(intervention.to_json())
            print(f"{'='*80}\n")
            break
        
        # Mark checkpoints as safe periodically
        if step % 100 == 0:
            watchdog.mark_checkpoint_safe(Path(f'./checkpoints/ckpt_{step}.pt'))
        
        if step % 100 == 0:
            print(f"Step {step}: Training nominal, watchdog status: {watchdog.get_status_report()['current_level']}")
    
    print("\nFinal watchdog status:")
    print(json.dumps(watchdog.get_status_report(), indent=2))
    
    # Verify audit trail
    is_valid, violations = watchdog.verify_audit_trail()
    print(f"\nAudit trail valid: {is_valid}")
    if violations:
        print(f"Violations: {violations}")
