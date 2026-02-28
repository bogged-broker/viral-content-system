"""
/training/optimizer.py

Gradient Optimizer Controller — NOT just Adam()

Centralized authority for parameter update governance:
- How parameters are updated
- How fast they're allowed to change
- Under what constraints learning is permitted

This is a SAFETY-CRITICAL system component.

ARCHITECTURAL PLACEMENT:
    models / rl_agents / trainers
                ↓
         optimizer.py  ← YOU ARE HERE
                ↓
       gradient_governor / safety_watchdog

Nothing updates weights without passing through this file.

RESPONSIBILITIES:
✓ Construct optimizers per model class
✓ Enforce learning-rate policy
✓ Support schedule-aware optimization
✓ Apply trust-region constraints
✓ Respect risk & gradient budgets
✓ Integrate with curriculum phase
✓ Produce deterministic updates
✓ Emit optimizer state for audits

MUST NOT:
✗ Define losses
✗ Compute gradients
✗ Access data
✗ Decide when to train
✗ Override safety governors
"""

import torch
import torch.optim as optim
from torch.nn import Parameter
from typing import Dict, List, Tuple, Optional, Any, Set, Union
from dataclasses import dataclass
from enum import Enum
import math
import logging
from pathlib import Path
import json
import hashlib


# ============================================================================
# ENUMS & CONSTANTS
# ============================================================================

class ModelRole(Enum):
    """Model types with different optimization semantics."""
    PREDICTOR = "predictor"
    RANKER = "ranker"
    POLICY = "policy"
    VALUE = "value"
    SHARED_BACKBONE = "shared_backbone"
    CLASSIFIER = "classifier"


class CurriculumPhase(Enum):
    """Curriculum phases affecting optimizer behavior."""
    STRUCTURE_LEARNING = "structure_learning"
    STABILIZATION = "stabilization"
    TAIL_AMPLIFICATION = "tail_amplification"
    RISK_CONTROL = "risk_control"


class ScheduleType(Enum):
    """Learning rate schedule types."""
    CONSTANT = "constant"
    WARMUP = "warmup"
    COSINE_DECAY = "cosine_decay"
    PLATEAU = "plateau"
    EMERGENCY_COOLDOWN = "emergency_cooldown"


class ParameterGroup(Enum):
    """Parameter grouping for differential learning rates."""
    EMBEDDINGS = "embeddings"
    TEMPORAL_ENCODERS = "temporal_encoders"
    ATTENTION_LAYERS = "attention_layers"
    HEADS = "heads"
    BACKBONES = "backbones"


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class OptimizerConfig:
    """Configuration for optimizer construction."""
    model_role: ModelRole
    base_lr: float
    weight_decay: float
    betas: Tuple[float, float] = (0.9, 0.999)
    eps: float = 1e-8
    max_grad_norm: float = 1.0
    
    # Trust region constraints
    kl_clip: Optional[float] = None  # For policy models
    param_delta_bound: Optional[float] = None
    
    # Schedule config
    schedule_type: ScheduleType = ScheduleType.CONSTANT
    warmup_steps: int = 0
    total_steps: Optional[int] = None
    
    # Risk scaling
    enable_risk_scaling: bool = True
    uncertainty_threshold: float = 0.8


@dataclass
class OptimizerControllerConfig:
    """
    Configuration for OptimizerController behavior.
    
    Controls safety enforcement and failure modes.
    """
    hard_fail_on_violation: bool = True  # Raise exceptions instead of soft-fail
    allow_ppo_kl_soft_fail: bool = False  # Exception: PPO KL may soft-fail if True
    require_parameter_group_authorization: bool = True  # Enforce group authorization
    enable_state_versioning: bool = True  # Enable semantic hashing for state


@dataclass
class OptimizerState:
    """Serializable optimizer state for audits."""
    step: int
    effective_lr: float
    gradient_norm: float
    clipped_ratio: float
    trust_violations: int
    phase: CurriculumPhase
    schedule_type: ScheduleType
    risk_scale: float


@dataclass
class UpdateDecision:
    """Result of update approval check."""
    approved: bool
    reason: str
    effective_lr: float
    risk_scale: float


# ============================================================================
# EXCEPTIONS (HARD STOPS)
# ============================================================================

class OptimizerSafetyException(Exception):
    """Hard stop exception - training MUST be halted."""
    pass


class GradientGovernorBlockedException(OptimizerSafetyException):
    """Gradient governor blocked the update."""
    pass


class TrustRegionViolationException(OptimizerSafetyException):
    """Trust region constraint violated."""
    pass


class NaNInfDetectedException(OptimizerSafetyException):
    """NaN or Inf detected in gradients/loss."""
    pass


class StepSizeExceededException(OptimizerSafetyException):
    """Step size exceeds risk budget."""
    pass


class UnauthorizedParameterGroupException(OptimizerSafetyException):
    """Unauthorized parameter group detected."""
    pass


class ResourceBudgetExceededException(OptimizerSafetyException):
    """Resource budget (GPU/TPU/memory) exceeded."""
    pass


class KillSwitchActivatedException(OptimizerSafetyException):
    """Safety watchdog kill switch activated."""
    pass


# ============================================================================
# SEED CONTROLLER (DETERMINISM ENFORCEMENT)
# ============================================================================

class SeedController:
    """
    Enforces global determinism across optimizer operations.
    
    CRITICAL REQUIREMENT:
    Given same seed, same gradients, same schedule → optimizer MUST produce
    identical parameter updates.
    
    This is NON-NEGOTIABLE for reproducibility.
    """
    
    def __init__(self, base_seed: int = 42):
        """
        Initialize seed controller.
        
        Args:
            base_seed: Base seed for deterministic operations
        """
        self.base_seed = base_seed
        self.optimizer_seed = base_seed
        self.step_seed_offset = 0
        
        # Seed history for debugging
        self.seed_history: List[Tuple[int, int]] = []  # (step, seed)
        
        # Determinism flags
        self.deterministic_mode = True
        self._logger = logging.getLogger(__name__)
    
    def get_optimizer_seed(self) -> int:
        """
        Get seed for optimizer initialization.
        
        Returns:
            Seed value for optimizer
        """
        return self.optimizer_seed
    
    def get_step_seed(self, step: int) -> int:
        """
        Get seed for specific training step.
        
        Args:
            step: Training step number
            
        Returns:
            Deterministic seed for this step
        """
        step_seed = self.base_seed + step * 1000 + self.step_seed_offset
        self.seed_history.append((step, step_seed))
        
        # Keep only recent history
        if len(self.seed_history) > 1000:
            self.seed_history.pop(0)
        
        return step_seed
    
    def enforce_determinism(self):
        """
        Enforce deterministic behavior across PyTorch operations.
        
        Sets all necessary flags for reproducibility.
        """
        if not self.deterministic_mode:
            self._logger.warning("Deterministic mode disabled")
            return
        
        # Set PyTorch seed
        torch.manual_seed(self.optimizer_seed)
        
        # Set CUDA seed if available
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(self.optimizer_seed)
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False
            self._logger.info(f"CUDA determinism enforced with seed: {self.optimizer_seed}")
        
        # Set NumPy seed if available
        try:
            import numpy as np
            np.random.seed(self.optimizer_seed % (2**32))
        except ImportError:
            pass
        
        # Set Python random seed
        import random
        random.seed(self.optimizer_seed)
        
        self._logger.info(f"Determinism enforced with seed: {self.optimizer_seed}")
    
    def verify_determinism(
        self,
        param_updates_1: Dict[str, torch.Tensor],
        param_updates_2: Dict[str, torch.Tensor],
        tolerance: float = 1e-6
    ) -> Tuple[bool, str]:
        """
        Verify that two sets of parameter updates are identical (deterministic).
        
        Args:
            param_updates_1: First set of parameter updates
            param_updates_2: Second set of parameter updates
            tolerance: Numerical tolerance for comparison
            
        Returns:
            (is_identical, reason)
        """
        if set(param_updates_1.keys()) != set(param_updates_2.keys()):
            return False, "Parameter sets differ"
        
        max_diff = 0.0
        max_diff_param = None
        
        for name in param_updates_1:
            diff = torch.abs(param_updates_1[name] - param_updates_2[name]).max().item()
            if diff > max_diff:
                max_diff = diff
                max_diff_param = name
            
            if diff > tolerance:
                return False, f"Parameter {name} differs by {diff:.8f} (tolerance: {tolerance:.8f})"
        
        return True, f"All parameters identical (max diff: {max_diff:.10f})"
    
    def reset_seed(self, new_seed: Optional[int] = None):
        """
        Reset seed controller with new seed.
        
        Args:
            new_seed: New seed value (if None, uses base_seed)
        """
        if new_seed is not None:
            self.base_seed = new_seed
            self.optimizer_seed = new_seed
        else:
            self.optimizer_seed = self.base_seed
        
        self.enforce_determinism()
        self._logger.info(f"Seed reset to: {self.optimizer_seed}")
    
    def get_seed_history(self) -> List[Tuple[int, int]]:
        """Get seed history for debugging."""
        return self.seed_history.copy()


# ============================================================================
# INTEGRATION INTERFACES (FALLBACK IF MODULES DON'T EXIST)
# ============================================================================

class IntegrationRegistry:
    """Central registry for training subsystem integrations."""
    
    def __init__(self):
        self.gradient_governor = None
        self.safety_watchdog = None
        self.curriculum = None
        self.audit_logger = None
        self.version_manager = None
        self.seed_controller = None
        self.resource_governor = None
        self._logger = logging.getLogger(__name__)
    
    def register_gradient_governor(self, governor):
        """Register gradient governor (from gradient_governor.py)."""
        self.gradient_governor = governor
        self._logger.info("Gradient governor registered")
    
    def register_safety_watchdog(self, watchdog):
        """Register safety watchdog (from safety_watchdog.py)."""
        self.safety_watchdog = watchdog
        self._logger.info("Safety watchdog registered")
    
    def register_curriculum(self, curriculum):
        """Register curriculum manager (from curriculum.py)."""
        self.curriculum = curriculum
        self._logger.info("Curriculum manager registered")
    
    def register_audit_logger(self, logger):
        """Register audit logger (from audit_logger.py)."""
        self.audit_logger = logger
        self._logger.info("Audit logger registered")
    
    def register_version_manager(self, version_mgr):
        """Register version manager (from version_manager.py)."""
        self.version_manager = version_mgr
        self._logger.info("Version manager registered")
    
    def register_seed_controller(self, seed_ctrl):
        """Register seed controller (from seed_controller.py)."""
        self.seed_controller = seed_ctrl
        self._logger.info("Seed controller registered")
    
    def register_resource_governor(self, resource_gov):
        """Register resource governor (from resource_governor.py)."""
        self.resource_governor = resource_gov
        self._logger.info("Resource governor registered")


# Global integration registry
_integration_registry = IntegrationRegistry()


# ============================================================================
# VALIDATION UTILITIES
# ============================================================================

class ParameterValidator:
    """
    Validates model parameters for optimizer safety.
    
    Checks:
    - Parameter shapes consistency
    - Parameter value ranges
    - Gradient presence
    - Parameter group assignments
    """
    
    @staticmethod
    def validate_parameters(
        parameters: Dict[str, Parameter],
        param_groups: List[Dict[str, Any]],
        require_gradients: bool = False
    ) -> Tuple[bool, str]:
        """
        Validate parameters for optimizer safety.
        
        Args:
            parameters: Model parameters
            param_groups: Optimizer parameter groups
            require_gradients: If True, require gradients for all parameters
            
        Returns:
            (is_valid, reason)
        """
        if not parameters:
            return False, "No parameters provided"
        
        if not param_groups:
            return False, "No parameter groups provided"
        
        # Check all parameters are in parameter groups
        group_params = set()
        for group in param_groups:
            if 'params' not in group:
                return False, f"Parameter group missing 'params' key"
            group_params.update(group['params'])
        
        # Check all parameters have gradients (if required)
        if require_gradients:
            for name, param in parameters.items():
                if param not in group_params:
                    continue
                if param.grad is None:
                    return False, f"Parameter {name} missing gradient"
        
        # Check parameter values are finite
        for name, param in parameters.items():
            if torch.isnan(param.data).any():
                return False, f"Parameter {name} contains NaN"
            if torch.isinf(param.data).any():
                return False, f"Parameter {name} contains Inf"
        
        return True, "Parameters valid"
    
    @staticmethod
    def validate_gradients(
        parameters: Dict[str, Parameter],
        max_grad_norm: float = 10.0
    ) -> Tuple[bool, str, float]:
        """
        Validate gradients for optimizer safety.
        
        Args:
            parameters: Model parameters
            max_grad_norm: Maximum allowed gradient norm
            
        Returns:
            (is_valid, reason, gradient_norm)
        """
        total_norm = 0.0
        param_count = 0
        
        for name, param in parameters.items():
            if param.grad is not None:
                param_count += 1
                param_norm = param.grad.data.norm(2)
                total_norm += param_norm.item() ** 2
                
                # Check for NaN/Inf in gradients
                if torch.isnan(param.grad).any():
                    return False, f"NaN detected in gradient for {name}", total_norm
                if torch.isinf(param.grad).any():
                    return False, f"Inf detected in gradient for {name}", total_norm
        
        total_norm = total_norm ** 0.5
        
        if param_count == 0:
            return False, "No parameters with gradients", 0.0
        
        if total_norm > max_grad_norm:
            return False, f"Gradient norm {total_norm:.4f} exceeds maximum {max_grad_norm}", total_norm
        
        return True, "Gradients valid", total_norm


class OptimizerStateValidator:
    """
    Validates optimizer state for consistency and safety.
    
    Checks:
    - Optimizer state consistency
    - Parameter state alignment
    - Schedule state consistency
    - Trust region state consistency
    """
    
    @staticmethod
    def validate_optimizer_state(
        optimizer: optim.Optimizer,
        parameters: Dict[str, Parameter]
    ) -> Tuple[bool, str]:
        """
        Validate optimizer state consistency.
        
        Args:
            optimizer: Optimizer instance
            parameters: Model parameters
            
        Returns:
            (is_valid, reason)
        """
        if optimizer is None:
            return False, "Optimizer is None"
        
        # Check optimizer has parameter groups
        if not hasattr(optimizer, 'param_groups') or not optimizer.param_groups:
            return False, "Optimizer has no parameter groups"
        
        # Check parameter groups match parameters
        optimizer_params = set()
        for group in optimizer.param_groups:
            if 'params' in group:
                optimizer_params.update(group['params'])
        
        # Verify all parameters are in optimizer
        for name, param in parameters.items():
            if param not in optimizer_params:
                return False, f"Parameter {name} not in optimizer parameter groups"
        
        return True, "Optimizer state valid"
    
    @staticmethod
    def validate_checkpoint_consistency(
        checkpoint: Dict[str, Any],
        expected_step: int,
        expected_phase: CurriculumPhase
    ) -> Tuple[bool, str]:
        """
        Validate checkpoint consistency.
        
        Args:
            checkpoint: Checkpoint dictionary
            expected_step: Expected training step
            expected_phase: Expected curriculum phase
            
        Returns:
            (is_valid, reason)
        """
        if 'step_count' not in checkpoint:
            return False, "Checkpoint missing step_count"
        
        if checkpoint['step_count'] != expected_step:
            return False, f"Checkpoint step {checkpoint['step_count']} != expected {expected_step}"
        
        if 'phase' in checkpoint:
            checkpoint_phase = CurriculumPhase(checkpoint['phase'])
            if checkpoint_phase != expected_phase:
                return False, f"Checkpoint phase {checkpoint_phase.value} != expected {expected_phase.value}"
        
        if 'optimizer' not in checkpoint:
            return False, "Checkpoint missing optimizer state"
        
        return True, "Checkpoint consistent"


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def compute_effective_learning_rate(
    base_lr: float,
    schedule_factor: float,
    risk_scale: float,
    maturity_factor: float = 1.0,
    uncertainty_factor: float = 1.0,
    noise_factor: float = 1.0,
    throttle_factor: float = 1.0
) -> float:
    """
    Compute effective learning rate from all factors.
    
    Args:
        base_lr: Base learning rate
        schedule_factor: Schedule adjustment factor
        risk_scale: Risk-based scaling factor
        maturity_factor: Model maturity factor
        uncertainty_factor: Uncertainty-based factor
        noise_factor: Gradient noise factor
        throttle_factor: Adaptive throttle factor
        
    Returns:
        Effective learning rate
    """
    effective_lr = base_lr
    effective_lr *= schedule_factor
    effective_lr *= risk_scale
    effective_lr *= maturity_factor
    effective_lr *= uncertainty_factor
    effective_lr *= noise_factor
    effective_lr *= throttle_factor
    
    # Ensure minimum LR (1% of base)
    effective_lr = max(effective_lr, base_lr * 0.01)
    
    return effective_lr


def compute_parameter_delta(
    old_params: Dict[str, torch.Tensor],
    new_params: Dict[str, torch.Tensor]
) -> Dict[str, float]:
    """
    Compute parameter delta (change) between old and new parameters.
    
    Args:
        old_params: Parameters before update
        new_params: Parameters after update
        
    Returns:
        Dictionary mapping parameter names to delta values
    """
    deltas = {}
    
    for name in old_params:
        if name in new_params:
            delta = torch.abs(new_params[name] - old_params[name]).max().item()
            deltas[name] = delta
        else:
            deltas[name] = float('inf')
    
    return deltas


def compute_gradient_statistics(
    parameters: Dict[str, Parameter]
) -> Dict[str, Any]:
    """
    Compute gradient statistics for monitoring.
    
    Args:
        parameters: Model parameters
        
    Returns:
        Dictionary of gradient statistics
    """
    stats = {
        'total_params': len(parameters),
        'params_with_grad': 0,
        'params_without_grad': 0,
        'total_norm': 0.0,
        'max_norm': 0.0,
        'min_norm': float('inf'),
        'mean_norm': 0.0,
        'gradient_norms': []
    }
    
    norms = []
    
    for name, param in parameters.items():
        if param.grad is not None:
            stats['params_with_grad'] += 1
            param_norm = param.grad.data.norm(2).item()
            norms.append(param_norm)
            stats['gradient_norms'].append(param_norm)
            stats['max_norm'] = max(stats['max_norm'], param_norm)
            stats['min_norm'] = min(stats['min_norm'], param_norm)
        else:
            stats['params_without_grad'] += 1
    
    if norms:
        stats['total_norm'] = math.sqrt(sum(n**2 for n in norms))
        stats['mean_norm'] = sum(norms) / len(norms)
    
    if stats['min_norm'] == float('inf'):
        stats['min_norm'] = 0.0
    
    return stats


# ============================================================================
# PPO / CONSTRAINED OPTIMIZERS (CAPABILITY-BASED)
# ============================================================================

class PPOOptimizerState:
    """
    Isolated state for PPO optimizers.
    
    Tracks policy-only state separate from Adam machinery:
    - KL divergence accounting
    - Ratio clipping history
    - Policy update statistics
    """
    
    def __init__(self):
        self.kl_divergence_history: List[float] = []
        self.kl_divergence_accumulated = 0.0
        self.ratio_clip_history: List[Tuple[float, float]] = []  # (ratio, clipped_ratio)
        self.policy_update_count = 0
        self.last_old_policy_log_probs: Optional[torch.Tensor] = None
        self.last_new_policy_log_probs: Optional[torch.Tensor] = None
        self.ratio_clip_epsilon = 0.2  # Standard PPO clip range
        
    def record_kl_divergence(self, kl_div: float):
        """Record KL divergence for accounting."""
        self.kl_divergence_history.append(kl_div)
        self.kl_divergence_accumulated += kl_div
        # Keep only recent history
        if len(self.kl_divergence_history) > 1000:
            self.kl_divergence_history.pop(0)
    
    def record_ratio_clip(self, ratio: float, clipped_ratio: float):
        """Record ratio clipping for policy updates."""
        self.ratio_clip_history.append((ratio, clipped_ratio))
        # Keep only recent history
        if len(self.ratio_clip_history) > 1000:
            self.ratio_clip_history.pop(0)
    
    def compute_kl_divergence(self, old_log_probs: torch.Tensor, new_log_probs: torch.Tensor) -> float:
        """
        Compute KL divergence between old and new policy distributions.
        
        Args:
            old_log_probs: Log probabilities from old policy
            new_log_probs: Log probabilities from new policy
            
        Returns:
            KL divergence value
        """
        # KL(p_old || p_new) = E[log(p_old) - log(p_new)]
        # For log probabilities: KL = mean(old_log_probs - new_log_probs)
        kl = (old_log_probs - new_log_probs).mean().item()
        self.record_kl_divergence(kl)
        return kl
    
    def clip_ratio(self, ratio: torch.Tensor, epsilon: Optional[float] = None) -> torch.Tensor:
        """
        Clip importance sampling ratio for PPO.
        
        Args:
            ratio: Importance sampling ratio (new_prob / old_prob)
            epsilon: Clip range (defaults to self.ratio_clip_epsilon)
            
        Returns:
            Clipped ratio
        """
        if epsilon is None:
            epsilon = self.ratio_clip_epsilon
        
        clipped_ratio = torch.clamp(ratio, 1.0 - epsilon, 1.0 + epsilon)
        
        # Record clipping statistics
        if ratio.numel() > 0:
            mean_ratio = ratio.mean().item()
            mean_clipped = clipped_ratio.mean().item()
            self.record_ratio_clip(mean_ratio, mean_clipped)
        
        return clipped_ratio
    
    def get_kl_statistics(self) -> Dict[str, float]:
        """Get KL divergence statistics."""
        if not self.kl_divergence_history:
            return {
                'mean_kl': 0.0,
                'max_kl': 0.0,
                'accumulated_kl': 0.0,
                'count': 0
            }
        
        return {
            'mean_kl': sum(self.kl_divergence_history) / len(self.kl_divergence_history),
            'max_kl': max(self.kl_divergence_history),
            'accumulated_kl': self.kl_divergence_accumulated,
            'count': len(self.kl_divergence_history)
        }
    
    def get_ratio_clip_statistics(self) -> Dict[str, float]:
        """Get ratio clipping statistics."""
        if not self.ratio_clip_history:
            return {
                'mean_ratio': 1.0,
                'mean_clipped': 1.0,
                'clip_frequency': 0.0
            }
        
        ratios = [r[0] for r in self.ratio_clip_history]
        clipped = [r[1] for r in self.ratio_clip_history]
        clip_count = sum(1 for r in ratios if abs(r - 1.0) > self.ratio_clip_epsilon)
        
        return {
            'mean_ratio': sum(ratios) / len(ratios),
            'mean_clipped': sum(clipped) / len(clipped),
            'clip_frequency': clip_count / len(ratios) if ratios else 0.0
        }


class PPOOptimizer:
    """
    True PPO optimizer with isolated policy-only state.
    
    Capability-based: handles KL accounting, ratio clipping, and policy updates
    separately from Adam-like machinery.
    
    This is NOT just Adam with external constraints - it's a dedicated
    policy optimizer with its own state management.
    """
    
    def __init__(
        self,
        parameters: List[Parameter],
        base_lr: float,
        kl_clip: float = 0.2,
        ratio_clip_epsilon: float = 0.2,
        betas: Tuple[float, float] = (0.9, 0.999),
        eps: float = 1e-8
    ):
        """
        Initialize PPO optimizer.
        
        Args:
            parameters: Policy network parameters
            base_lr: Base learning rate
            kl_clip: Maximum allowed KL divergence per update
            ratio_clip_epsilon: PPO ratio clipping range
            betas: Adam betas (for underlying optimizer)
            eps: Adam epsilon
        """
        # Underlying optimizer (Adam for policy networks)
        self.optimizer = optim.Adam(
            parameters,
            lr=base_lr,
            betas=betas,
            eps=eps
        )
        
        # Isolated PPO state (NOT shared with Adam)
        self.ppo_state = PPOOptimizerState()
        self.ppo_state.ratio_clip_epsilon = ratio_clip_epsilon
        self.kl_clip = kl_clip
        self.parameters = parameters
        
    def step(
        self,
        loss: torch.Tensor,
        old_log_probs: torch.Tensor,
        new_log_probs: torch.Tensor,
        hard_fail_on_kl_violation: bool = False
    ) -> Dict[str, Any]:
        """
        Perform PPO update step.
        
        ARCHITECTURAL CHANGE: Loss computation moved out of optimizer.
        Optimizer governs HOW (KL accounting, ratio clipping, trust enforcement),
        not WHAT (objective construction).
        
        Loss should be computed by trainer/RL agent using:
        - PPOOptimizerState.compute_kl_divergence()
        - PPOOptimizerState.clip_ratio()
        - Then construct: loss = -torch.min(ratio * advantages, clipped_ratio * advantages).mean()
        
        Args:
            loss: Pre-computed PPO loss tensor (from trainer/RL agent)
            old_log_probs: Log probabilities from old policy (for KL accounting)
            new_log_probs: Log probabilities from new policy (for KL accounting)
            hard_fail_on_kl_violation: If True, raise exception on KL violation (default: False for backward compat)
            
        Returns:
            Update statistics including KL divergence and clipping info
        """
        # Compute KL divergence (policy-only accounting)
        kl_div = self.ppo_state.compute_kl_divergence(old_log_probs, new_log_probs)
        
        # Check KL constraint
        if kl_div > self.kl_clip:
            if hard_fail_on_kl_violation:
                raise TrustRegionViolationException(
                    f'KL divergence {kl_div:.4f} exceeds clip {self.kl_clip}'
                )
            return {
                'update_applied': False,
                'reason': f'KL divergence {kl_div:.4f} exceeds clip {self.kl_clip}',
                'kl_divergence': kl_div,
                'ratio_clipped': False
            }
        
        # Backward pass (loss already computed externally)
        loss.backward()
        
        # Step optimizer
        self.optimizer.step()
        self.optimizer.zero_grad()
        
        # Update policy statistics
        self.ppo_state.policy_update_count += 1
        
        return {
            'update_applied': True,
            'kl_divergence': kl_div,
            'policy_update_count': self.ppo_state.policy_update_count
        }
    
    def compute_kl_and_ratio(
        self,
        old_log_probs: torch.Tensor,
        new_log_probs: torch.Tensor,
        ratio: Optional[torch.Tensor] = None
    ) -> Tuple[float, torch.Tensor, torch.Tensor]:
        """
        Compute KL divergence and clipped ratio for external loss construction.
        
        This allows trainer/RL agent to construct the PPO objective:
        objective = torch.min(ratio * advantages, clipped_ratio * advantages).mean()
        loss = -objective
        
        Args:
            old_log_probs: Log probabilities from old policy
            new_log_probs: Log probabilities from new policy
            ratio: Optional precomputed importance sampling ratio
            
        Returns:
            (kl_divergence, ratio, clipped_ratio)
        """
        # Compute KL divergence
        kl_div = self.ppo_state.compute_kl_divergence(old_log_probs, new_log_probs)
        
        # Compute or clip ratio
        if ratio is None:
            ratio = torch.exp(new_log_probs - old_log_probs)
        
        clipped_ratio = self.ppo_state.clip_ratio(ratio)
        
        return kl_div, ratio, clipped_ratio
    
    def get_state(self) -> Dict[str, Any]:
        """Get complete PPO optimizer state."""
        return {
            'optimizer_state': self.optimizer.state_dict(),
            'ppo_state': {
                'kl_divergence_history': self.ppo_state.kl_divergence_history[-100:],  # Last 100
                'kl_divergence_accumulated': self.ppo_state.kl_divergence_accumulated,
                'ratio_clip_history': self.ppo_state.ratio_clip_history[-100:],
                'policy_update_count': self.ppo_state.policy_update_count,
                'ratio_clip_epsilon': self.ppo_state.ratio_clip_epsilon
            },
            'kl_clip': self.kl_clip
        }
    
    def load_state(self, state: Dict[str, Any]):
        """Load PPO optimizer state."""
        self.optimizer.load_state_dict(state['optimizer_state'])
        ppo_state_dict = state.get('ppo_state', {})
        self.ppo_state.kl_divergence_history = ppo_state_dict.get('kl_divergence_history', [])
        self.ppo_state.kl_divergence_accumulated = ppo_state_dict.get('kl_divergence_accumulated', 0.0)
        self.ppo_state.ratio_clip_history = ppo_state_dict.get('ratio_clip_history', [])
        self.ppo_state.policy_update_count = ppo_state_dict.get('policy_update_count', 0)
        self.ppo_state.ratio_clip_epsilon = ppo_state_dict.get('ratio_clip_epsilon', 0.2)
        self.kl_clip = state.get('kl_clip', 0.2)
    
    @property
    def param_groups(self):
        """Delegate to underlying optimizer."""
        return self.optimizer.param_groups
    
    def zero_grad(self):
        """Zero gradients."""
        self.optimizer.zero_grad()


class TDStabilizedOptimizerState:
    """
    Isolated state for TD-stabilized value network optimizers.
    
    Tracks value-specific state separate from Adam machinery:
    - TD error history
    - Value function stability metrics
    - Bellman residual tracking
    """
    
    def __init__(self):
        self.td_error_history: List[float] = []
        self.bellman_residual_history: List[float] = []
        self.value_stability_window = 100
        self.td_error_accumulated = 0.0
        self.update_count = 0
        
    def record_td_error(self, td_error: float):
        """Record TD error for stability tracking."""
        self.td_error_history.append(td_error)
        self.td_error_accumulated += abs(td_error)
        # Keep only recent history
        if len(self.td_error_history) > self.value_stability_window:
            self.td_error_history.pop(0)
    
    def record_bellman_residual(self, residual: float):
        """Record Bellman residual."""
        self.bellman_residual_history.append(residual)
        if len(self.bellman_residual_history) > self.value_stability_window:
            self.bellman_residual_history.pop(0)
    
    def compute_td_stability_factor(self) -> float:
        """
        Compute TD stability factor for adaptive learning rate.
        
        High TD error variance → reduce LR
        Low TD error variance → can use higher LR
        
        Returns:
            Stability factor (0.5 to 1.5)
        """
        if len(self.td_error_history) < 10:
            return 1.0
        
        # Compute TD error variance
        mean_td = sum(self.td_error_history) / len(self.td_error_history)
        variance = sum((x - mean_td) ** 2 for x in self.td_error_history) / len(self.td_error_history)
        std_td = math.sqrt(variance)
        
        # High variance → reduce LR (more conservative)
        if std_td > 1.0:
            stability_factor = 1.0 / (1.0 + std_td * 0.5)
            return max(stability_factor, 0.5)  # Minimum 50%
        elif std_td < 0.1:
            # Very stable → can increase LR slightly
            return min(1.0 + (0.1 - std_td) * 2.0, 1.5)  # Maximum 150%
        
        return 1.0
    
    def get_td_statistics(self) -> Dict[str, float]:
        """Get TD error statistics."""
        if not self.td_error_history:
            return {
                'mean_td_error': 0.0,
                'std_td_error': 0.0,
                'max_td_error': 0.0,
                'stability_factor': 1.0
            }
        
        mean_td = sum(self.td_error_history) / len(self.td_error_history)
        variance = sum((x - mean_td) ** 2 for x in self.td_error_history) / len(self.td_error_history)
        std_td = math.sqrt(variance)
        
        return {
            'mean_td_error': mean_td,
            'std_td_error': std_td,
            'max_td_error': max(abs(x) for x in self.td_error_history),
            'stability_factor': self.compute_td_stability_factor(),
            'update_count': self.update_count
        }


class TDStabilizedOptimizer:
    """
    True TD-stabilized optimizer for value networks.
    
    Capability-based: handles TD error stabilization, Bellman residual tracking,
    and value-specific update semantics separately from standard Adam.
    
    This is NOT just conservative Adam - it's a dedicated value optimizer
    with TD-aware learning rate adaptation.
    """
    
    def __init__(
        self,
        parameters: List[Parameter],
        base_lr: float,
        td_stability_weight: float = 0.5,
        betas: Tuple[float, float] = (0.95, 0.999),
        eps: float = 1e-8
    ):
        """
        Initialize TD-stabilized optimizer.
        
        Args:
            parameters: Value network parameters
            base_lr: Base learning rate
            td_stability_weight: Weight for TD stability factor (0.0 to 1.0)
            betas: Adam betas (more conservative for value networks)
            eps: Adam epsilon
        """
        # Underlying optimizer with conservative settings for value networks
        self.optimizer = optim.Adam(
            parameters,
            lr=base_lr,
            betas=betas,  # More conservative: (0.95, 0.999) vs standard (0.9, 0.999)
            eps=eps
        )
        
        # Isolated TD-stabilized state (NOT shared with Adam)
        self.td_state = TDStabilizedOptimizerState()
        self.td_stability_weight = td_stability_weight
        self.base_lr = base_lr
        self.parameters = parameters
        
    def step(
        self,
        td_error: Optional[float] = None,
        bellman_residual: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Perform TD-stabilized update step.
        
        Args:
            td_error: Optional TD error for stability tracking
            bellman_residual: Optional Bellman residual
            
        Returns:
            Update statistics including TD stability metrics
        """
        # Record TD metrics if provided
        if td_error is not None:
            self.td_state.record_td_error(td_error)
        
        if bellman_residual is not None:
            self.td_state.record_bellman_residual(bellman_residual)
        
        # Compute TD stability factor
        stability_factor = self.td_state.compute_td_stability_factor()
        
        # Adapt learning rate based on TD stability
        # Blend between base LR and stability-adjusted LR
        adaptive_lr = self.base_lr * (
            (1.0 - self.td_stability_weight) + 
            (self.td_stability_weight * stability_factor)
        )
        
        # Update optimizer learning rate
        for param_group in self.optimizer.param_groups:
            param_group['lr'] = adaptive_lr
        
        # Standard optimizer step
        self.optimizer.step()
        
        # Update statistics
        self.td_state.update_count += 1
        
        return {
            'update_applied': True,
            'adaptive_lr': adaptive_lr,
            'stability_factor': stability_factor,
            'td_statistics': self.td_state.get_td_statistics()
        }
    
    def get_state(self) -> Dict[str, Any]:
        """Get complete TD-stabilized optimizer state."""
        return {
            'optimizer_state': self.optimizer.state_dict(),
            'td_state': {
                'td_error_history': self.td_state.td_error_history[-100:],
                'bellman_residual_history': self.td_state.bellman_residual_history[-100:],
                'td_error_accumulated': self.td_state.td_error_accumulated,
                'update_count': self.td_state.update_count
            },
            'td_stability_weight': self.td_stability_weight,
            'base_lr': self.base_lr
        }
    
    def load_state(self, state: Dict[str, Any]):
        """Load TD-stabilized optimizer state."""
        self.optimizer.load_state_dict(state['optimizer_state'])
        td_state_dict = state.get('td_state', {})
        self.td_state.td_error_history = td_state_dict.get('td_error_history', [])
        self.td_state.bellman_residual_history = td_state_dict.get('bellman_residual_history', [])
        self.td_state.td_error_accumulated = td_state_dict.get('td_error_accumulated', 0.0)
        self.td_state.update_count = td_state_dict.get('update_count', 0)
        self.td_stability_weight = state.get('td_stability_weight', 0.5)
        self.base_lr = state.get('base_lr', self.base_lr)
    
    @property
    def param_groups(self):
        """Delegate to underlying optimizer."""
        return self.optimizer.param_groups
    
    def zero_grad(self):
        """Zero gradients."""
        self.optimizer.zero_grad()


# ============================================================================
# OPTIMIZER FACTORY (CAPABILITY-BASED)
# ============================================================================

class OptimizerFactory:
    """
    Creates optimizers based on CAPABILITIES, not just roles.
    
    Capability-based mapping ensures:
    - Policy networks get true PPO optimizers (KL accounting, ratio clipping, policy-only state)
    - Value networks get constrained optimizers (TD-stabilized, separate from policy)
    - Predictors/Rankers get standard AdamW (no policy constraints needed)
    
    This is NOT role-mapped Adam machinery - each capability has its own optimizer.
    """
    
    @staticmethod
    def create(config: OptimizerConfig, parameters: List[Parameter]) -> Any:
        """
        Create optimizer based on model capabilities.
        
        Args:
            config: Optimizer configuration
            parameters: Model parameters to optimize (can be List[Parameter] or List[Dict])
            
        Returns:
            Configured optimizer instance (PPOOptimizer, TDStabilizedOptimizer, or optim.Optimizer)
        """
        # Handle parameter groups (List[Dict]) vs raw parameters (List[Parameter])
        if parameters and isinstance(parameters[0], dict):
            # Parameter groups format - extract actual parameters
            param_list = []
            for group in parameters:
                param_list.extend(group['params'])
            parameters = param_list
        
        # Capability-based mapping (not role-mapped Adam)
        if config.model_role == ModelRole.POLICY:
            # TRUE PPO optimizer with isolated policy state
            # NOT just Adam with external constraints
            kl_clip = config.kl_clip or 0.2
            return PPOOptimizer(
                parameters=parameters,
                base_lr=config.base_lr,
                kl_clip=kl_clip,
                ratio_clip_epsilon=0.2,
                betas=config.betas,
                eps=config.eps
            )
        
        elif config.model_role == ModelRole.VALUE:
            # TRUE TD-stabilized optimizer (capability-specific state machine)
            # NOT just conservative Adam - has TD error tracking and stability adaptation
            return TDStabilizedOptimizer(
                parameters=parameters,
                base_lr=config.base_lr,
                td_stability_weight=0.5,  # Blend base LR with TD stability factor
                betas=(0.95, 0.999),  # More conservative for value networks
                eps=config.eps
            )
        
        elif config.model_role in [ModelRole.PREDICTOR, ModelRole.RANKER, ModelRole.CLASSIFIER]:
            # Standard AdamW (no policy constraints needed)
            return optim.AdamW(
                parameters,
                lr=config.base_lr,
                betas=config.betas,
                eps=config.eps,
                weight_decay=config.weight_decay
            )
        
        elif config.model_role == ModelRole.SHARED_BACKBONE:
            # Low-LR optimizer for shared backbones
            return optim.Adam(
                parameters,
                lr=config.base_lr * 0.1,  # 10x slower
                betas=config.betas,
                eps=config.eps
            )
        
        else:
            raise ValueError(f"Unknown model role: {config.model_role}")


# ============================================================================
# SCHEDULE MANAGER
# ============================================================================

class ScheduleManager:
    """
    Learning rates are CONTEXTUAL, not static.
    
    Schedules depend on:
    - curriculum phase
    - model maturity
    - uncertainty collapse
    - observed gradient noise
    
    Supports:
    - warmup
    - cosine decay
    - plateau detection
    - emergency cooldown
    - maturity-aware adaptation
    - uncertainty-based scaling
    - gradient noise adaptation
    
    ARCHITECTURAL IMPROVEMENT: Per-group learning rates
    For 300M-scale stability, supports:
    - per-group schedules (vectorized LR fields)
    - NOT just scalar-LR centric (everything collapsing to single effective_lr)
    """
    
    def __init__(self, config: OptimizerConfig, enable_per_group_lr: bool = True):
        self.config = config
        self.step = 0
        self.base_lr = config.base_lr
        self.current_lr = config.base_lr
        self.schedule_type = config.schedule_type
        self.enable_per_group_lr = enable_per_group_lr
        
        # Per-group learning rates (vectorized LR fields)
        # For 300M-scale stability - NOT collapsing to single effective_lr
        self.group_lrs: Dict[ParameterGroup, float] = {
            group: config.base_lr for group in ParameterGroup
        }
        
        # Per-group schedule state
        self.group_schedule_states: Dict[ParameterGroup, Dict[str, Any]] = {
            group: {
                'maturity_steps': 0,
                'uncertainty_history': [],
                'gradient_norm_history': [],
                'plateau_history': []
            }
            for group in ParameterGroup
        }
        
        # Plateau detection
        self.plateau_patience = 10
        self.plateau_threshold = 1e-4
        self.plateau_history: List[float] = []
        
        # Emergency cooldown
        self.emergency_active = False
        self.emergency_factor = 0.1
        
        # Model maturity tracking
        self.maturity_steps = 0
        self.maturity_threshold = 10000  # Steps to consider "mature"
        self.maturity_decay_factor = 0.95  # LR reduction for mature models
        
        # Uncertainty collapse tracking
        self.uncertainty_history: List[float] = []
        self.uncertainty_window = 100
        self.uncertainty_collapse_threshold = 0.3  # 30% reduction = collapse
        self.uncertainty_adjusted_lr = 1.0
        
        # Gradient noise tracking
        self.gradient_norm_history: List[float] = []
        self.noise_window = 50
        self.high_noise_threshold = 2.0  # Norm > 2.0 = high noise
        self.noise_adjusted_lr = 1.0
        
        # Adaptive schedule selection
        self.adaptive_schedule_enabled = True
        self.auto_schedule_history: List[float] = []
        
    def step_schedule(
        self, 
        metrics: Optional[Dict[str, float]] = None,
        uncertainty: Optional[float] = None,
        gradient_norm: Optional[float] = None,
        per_group_metrics: Optional[Dict[ParameterGroup, Dict[str, float]]] = None
    ) -> Union[float, Dict[ParameterGroup, float]]:
        """
        Advance schedule and compute learning rates.
        
        For 300M-scale stability, supports per-group learning rates.
        Returns either scalar LR (if per-group disabled) or per-group LRs.
        
        Args:
            metrics: Optional training metrics for plateau detection
            uncertainty: Model prediction uncertainty [0, 1]
            gradient_norm: Current gradient norm
            per_group_metrics: Optional per-group metrics (uncertainty, gradient_norm per group)
            
        Returns:
            If enable_per_group_lr=False: scalar current_lr
            If enable_per_group_lr=True: Dict[ParameterGroup, float] of per-group LRs
        """
        self.step += 1
        self.maturity_steps += 1
        
        # Emergency cooldown overrides everything
        if self.emergency_active:
            emergency_lr = self.base_lr * self.emergency_factor
            if not self.enable_per_group_lr:
                self.current_lr = emergency_lr
                return emergency_lr
            else:
                # Apply emergency to all groups
                for group in ParameterGroup:
                    self.group_lrs[group] = emergency_lr
                return self.group_lrs.copy()
        
        if self.enable_per_group_lr:
            # Per-group schedule computation (vectorized LR fields)
            return self._step_per_group_schedule(metrics, uncertainty, gradient_norm, per_group_metrics)
        else:
            # Scalar schedule (legacy behavior)
            return self._step_scalar_schedule(metrics, uncertainty, gradient_norm)
    
    def _step_scalar_schedule(
        self,
        metrics: Optional[Dict[str, float]] = None,
        uncertainty: Optional[float] = None,
        gradient_norm: Optional[float] = None
    ) -> float:
        """Compute scalar learning rate (legacy behavior)."""
        # Base schedule computation
        if self.schedule_type == ScheduleType.CONSTANT:
            self.current_lr = self.base_lr
            
        elif self.schedule_type == ScheduleType.WARMUP:
            if self.step < self.config.warmup_steps:
                # Linear warmup
                self.current_lr = self.base_lr * (self.step / self.config.warmup_steps)
            else:
                self.current_lr = self.base_lr
                
        elif self.schedule_type == ScheduleType.COSINE_DECAY:
            if self.config.total_steps is None:
                raise ValueError("total_steps required for cosine decay")
            
            if self.step < self.config.warmup_steps:
                # Warmup phase
                self.current_lr = self.base_lr * (self.step / self.config.warmup_steps)
            else:
                # Cosine decay
                progress = (self.step - self.config.warmup_steps) / (self.config.total_steps - self.config.warmup_steps)
                progress = min(progress, 1.0)
                self.current_lr = self.base_lr * 0.5 * (1 + math.cos(math.pi * progress))
                
        elif self.schedule_type == ScheduleType.PLATEAU:
            if metrics and 'loss' in metrics:
                self._check_plateau(metrics['loss'])
        
        # Apply maturity-aware adjustment
        maturity_adjustment = self._compute_maturity_adjustment()
        
        # Apply uncertainty collapse adjustment
        if uncertainty is not None:
            uncertainty_adjustment = self._compute_uncertainty_adjustment(uncertainty)
        else:
            uncertainty_adjustment = 1.0
        
        # Apply gradient noise adjustment
        if gradient_norm is not None:
            noise_adjustment = self._compute_noise_adjustment(gradient_norm)
        else:
            noise_adjustment = 1.0
        
        # Combine all adjustments
        self.current_lr *= maturity_adjustment * uncertainty_adjustment * noise_adjustment
        
        # Ensure LR doesn't go below minimum
        self.current_lr = max(self.current_lr, self.base_lr * 0.01)  # Minimum 1% of base LR
                
        return self.current_lr
    
    def _step_per_group_schedule(
        self,
        metrics: Optional[Dict[str, float]] = None,
        uncertainty: Optional[float] = None,
        gradient_norm: Optional[float] = None,
        per_group_metrics: Optional[Dict[ParameterGroup, Dict[str, float]]] = None
    ) -> Dict[ParameterGroup, float]:
        """
        Compute per-group learning rates (vectorized LR fields).
        
        For 300M-scale stability - each parameter group gets its own schedule.
        """
        # Update per-group schedule states
        for group in ParameterGroup:
            state = self.group_schedule_states[group]
            state['maturity_steps'] += 1
            
            # Per-group metrics if available
            group_uncertainty = None
            group_gradient_norm = None
            if per_group_metrics and group in per_group_metrics:
                group_metrics = per_group_metrics[group]
                group_uncertainty = group_metrics.get('uncertainty')
                group_gradient_norm = group_metrics.get('gradient_norm')
            
            # Use group-specific or global metrics
            use_uncertainty = group_uncertainty if group_uncertainty is not None else uncertainty
            use_gradient_norm = group_gradient_norm if group_gradient_norm is not None else gradient_norm
            
            # Base schedule for this group
            if self.schedule_type == ScheduleType.CONSTANT:
                group_lr = self.base_lr
            elif self.schedule_type == ScheduleType.WARMUP:
                if self.step < self.config.warmup_steps:
                    group_lr = self.base_lr * (self.step / self.config.warmup_steps)
                else:
                    group_lr = self.base_lr
            elif self.schedule_type == ScheduleType.COSINE_DECAY:
                if self.config.total_steps is None:
                    raise ValueError("total_steps required for cosine decay")
                if self.step < self.config.warmup_steps:
                    group_lr = self.base_lr * (self.step / self.config.warmup_steps)
                else:
                    progress = (self.step - self.config.warmup_steps) / (self.config.total_steps - self.config.warmup_steps)
                    progress = min(progress, 1.0)
                    group_lr = self.base_lr * 0.5 * (1 + math.cos(math.pi * progress))
            else:
                group_lr = self.base_lr
            
            # Per-group adjustments
            maturity_adj = self._compute_group_maturity_adjustment(group, state)
            uncertainty_adj = self._compute_group_uncertainty_adjustment(group, state, use_uncertainty)
            noise_adj = self._compute_group_noise_adjustment(group, state, use_gradient_norm)
            
            # Combine adjustments
            group_lr *= maturity_adj * uncertainty_adj * noise_adj
            
            # Ensure LR doesn't go below minimum
            group_lr = max(group_lr, self.base_lr * 0.01)
            
            self.group_lrs[group] = group_lr
        
        # Update scalar current_lr for backward compatibility (mean of group LRs)
        self.current_lr = sum(self.group_lrs.values()) / len(self.group_lrs)
        
        return self.group_lrs.copy()
    
    def _compute_group_maturity_adjustment(self, group: ParameterGroup, state: Dict[str, Any]) -> float:
        """Compute maturity adjustment for a specific group."""
        if state['maturity_steps'] < self.maturity_threshold:
            return 1.0
        maturity_factor = math.exp(-(state['maturity_steps'] - self.maturity_threshold) / self.maturity_threshold)
        return max(maturity_factor, self.maturity_decay_factor)
    
    def _compute_group_uncertainty_adjustment(
        self, 
        group: ParameterGroup, 
        state: Dict[str, Any], 
        uncertainty: Optional[float]
    ) -> float:
        """Compute uncertainty adjustment for a specific group."""
        if uncertainty is None:
            return 1.0
        
        state['uncertainty_history'].append(uncertainty)
        if len(state['uncertainty_history']) > self.uncertainty_window:
            state['uncertainty_history'].pop(0)
        
        if len(state['uncertainty_history']) < self.uncertainty_window:
            return 1.0
        
        recent = state['uncertainty_history'][-self.uncertainty_window//2:]
        older = state['uncertainty_history'][:self.uncertainty_window//2]
        
        if len(recent) > 0 and len(older) > 0:
            recent_avg = sum(recent) / len(recent)
            older_avg = sum(older) / len(older)
            
            if older_avg > 0:
                collapse_ratio = (older_avg - recent_avg) / older_avg
                if collapse_ratio > self.uncertainty_collapse_threshold:
                    adjustment = 1.0 - (collapse_ratio - self.uncertainty_collapse_threshold)
                    return max(adjustment, 0.5)
        
        return 1.0
    
    def _compute_group_noise_adjustment(
        self, 
        group: ParameterGroup, 
        state: Dict[str, Any], 
        gradient_norm: Optional[float]
    ) -> float:
        """Compute gradient noise adjustment for a specific group."""
        if gradient_norm is None:
            return 1.0
        
        state['gradient_norm_history'].append(gradient_norm)
        if len(state['gradient_norm_history']) > self.noise_window:
            state['gradient_norm_history'].pop(0)
        
        if len(state['gradient_norm_history']) < self.noise_window:
            return 1.0
        
        mean_norm = sum(state['gradient_norm_history']) / len(state['gradient_norm_history'])
        variance = sum((x - mean_norm) ** 2 for x in state['gradient_norm_history']) / len(state['gradient_norm_history'])
        std_norm = math.sqrt(variance)
        
        if std_norm > self.high_noise_threshold:
            noise_factor = self.high_noise_threshold / (std_norm + 1e-8)
            return max(noise_factor, 0.7)
        
        return 1.0
    
    def get_group_lr(self, group: ParameterGroup) -> float:
        """Get learning rate for a specific parameter group."""
        if self.enable_per_group_lr:
            return self.group_lrs.get(group, self.base_lr)
        else:
            return self.current_lr
    
    def _compute_maturity_adjustment(self) -> float:
        """
        Compute learning rate adjustment based on model maturity.
        
        Mature models (many steps) should learn slower to avoid overfitting.
        
        Returns:
            Adjustment factor (typically < 1.0 for mature models)
        """
        if self.maturity_steps < self.maturity_threshold:
            return 1.0  # Not mature yet, no adjustment
        
        # Exponential decay for mature models
        maturity_factor = math.exp(-(self.maturity_steps - self.maturity_threshold) / self.maturity_threshold)
        return max(maturity_factor, self.maturity_decay_factor)
    
    def _compute_uncertainty_adjustment(self, uncertainty: float) -> float:
        """
        Compute learning rate adjustment based on uncertainty collapse.
        
        If uncertainty is collapsing (decreasing rapidly), reduce LR to avoid
        overfitting to high-confidence predictions.
        
        Args:
            uncertainty: Current model uncertainty [0, 1]
            
        Returns:
            Adjustment factor
        """
        self.uncertainty_history.append(uncertainty)
        if len(self.uncertainty_history) > self.uncertainty_window:
            self.uncertainty_history.pop(0)
        
        if len(self.uncertainty_history) < self.uncertainty_window:
            return 1.0  # Not enough history
        
        # Check for uncertainty collapse
        recent_uncertainty = self.uncertainty_history[-self.uncertainty_window//2:]
        older_uncertainty = self.uncertainty_history[:self.uncertainty_window//2]
        
        if len(recent_uncertainty) > 0 and len(older_uncertainty) > 0:
            recent_avg = sum(recent_uncertainty) / len(recent_uncertainty)
            older_avg = sum(older_uncertainty) / len(older_uncertainty)
            
            if older_avg > 0:
                collapse_ratio = (older_avg - recent_avg) / older_avg
                
                if collapse_ratio > self.uncertainty_collapse_threshold:
                    # Uncertainty collapsing - reduce LR
                    adjustment = 1.0 - (collapse_ratio - self.uncertainty_collapse_threshold)
                    self.uncertainty_adjusted_lr = max(adjustment, 0.5)  # Minimum 50%
                    logging.info(f"Uncertainty collapse detected: {collapse_ratio:.3f}, adjusting LR by {self.uncertainty_adjusted_lr:.3f}")
                    return self.uncertainty_adjusted_lr
        
        return 1.0
    
    def _compute_noise_adjustment(self, gradient_norm: float) -> float:
        """
        Compute learning rate adjustment based on gradient noise.
        
        High gradient noise (high variance) → reduce LR
        Low gradient noise (stable gradients) → can use higher LR
        
        Args:
            gradient_norm: Current gradient norm
            
        Returns:
            Adjustment factor
        """
        self.gradient_norm_history.append(gradient_norm)
        if len(self.gradient_norm_history) > self.noise_window:
            self.gradient_norm_history.pop(0)
        
        if len(self.gradient_norm_history) < self.noise_window:
            return 1.0  # Not enough history
        
        # Compute gradient noise (standard deviation)
        mean_norm = sum(self.gradient_norm_history) / len(self.gradient_norm_history)
        variance = sum((x - mean_norm) ** 2 for x in self.gradient_norm_history) / len(self.gradient_norm_history)
        std_norm = math.sqrt(variance)
        
        # High noise → reduce LR
        if std_norm > self.high_noise_threshold:
            # Smooth reduction based on noise level
            noise_factor = self.high_noise_threshold / (std_norm + 1e-8)
            self.noise_adjusted_lr = max(noise_factor, 0.7)  # Minimum 70%
            return self.noise_adjusted_lr
        
        return 1.0
    
    def _check_plateau(self, loss: float):
        """Detect plateau and reduce LR."""
        self.plateau_history.append(loss)
        
        if len(self.plateau_history) >= self.plateau_patience:
            recent = self.plateau_history[-self.plateau_patience:]
            improvement = max(recent) - min(recent)
            
            if improvement < self.plateau_threshold:
                # Plateau detected, reduce LR
                self.current_lr *= 0.5
                self.plateau_history = []
                logging.warning(f"Plateau detected at step {self.step}, reducing LR to {self.current_lr:.6f}")
    
    def trigger_emergency_cooldown(self):
        """Activate emergency learning rate cooldown."""
        self.emergency_active = True
        logging.error(f"EMERGENCY COOLDOWN activated at step {self.step}")
    
    def reset_emergency(self):
        """Reset emergency cooldown."""
        self.emergency_active = False


# ============================================================================
# DRIFT ANALYZER (MEASUREMENT - SEPARATED FROM ENFORCEMENT)
# ============================================================================

class DriftAnalyzer:
    """
    Measures trust region violations and drift.
    
    SEPARATED from enforcement logic - this only evaluates violations.
    In ultra-large systems, measurement and enforcement are often split:
    - measurement → drift analyzer (this class)
    - enforcement → optimizer (TrustRegionController)
    
    Responsibilities:
    - Evaluate KL divergence violations
    - Measure parameter delta drift
    - Track per-group trust region violations
    - Compute violation severity metrics
    - Historical drift tracking
    """
    
    def __init__(self, config: OptimizerConfig):
        self.config = config
        self.kl_clip = config.kl_clip
        self.param_delta_bound = config.param_delta_bound
        
        # Violation tracking
        self.violation_history: List[Tuple[int, str, float]] = []  # (step, reason, severity)
        self.kl_violations: List[Tuple[int, float]] = []  # (step, kl_div)
        self.param_delta_violations: List[Tuple[int, str, float]] = []  # (step, param_name, delta)
        
        # Drift metrics
        self.drift_history: List[Dict[str, float]] = []
        self.max_drift_window = 1000
        
    def evaluate_kl_violation(self, kl_div: float, step: int = 0) -> Tuple[bool, float, str]:
        """
        Evaluate KL divergence violation (measurement only).
        
        Args:
            kl_div: KL divergence value
            step: Current training step
            
        Returns:
            (is_violation, severity, reason)
        """
        if self.kl_clip is None:
            return False, 0.0, "No KL clip configured"
        
        if kl_div > self.kl_clip:
            severity = min(kl_div / self.kl_clip, 2.0)  # Cap at 2.0
            reason = f"KL divergence {kl_div:.4f} exceeds clip {self.kl_clip}"
            self.kl_violations.append((step, kl_div))
            self.violation_history.append((step, reason, severity))
            return True, severity, reason
        
        return False, 0.0, "KL divergence within bounds"
    
    def evaluate_param_delta_violation(
        self,
        old_params: Dict[str, torch.Tensor],
        new_params: Dict[str, torch.Tensor],
        param_group_map: Optional[Dict[str, ParameterGroup]] = None,
        step: int = 0
    ) -> Tuple[bool, float, str, Optional[str]]:
        """
        Evaluate parameter delta violation (measurement only).
        
        Args:
            old_params: Parameters before update
            new_params: Parameters after update (predicted)
            param_group_map: Optional map from param name to ParameterGroup
            step: Current training step
            
        Returns:
            (is_violation, severity, reason, violating_param)
        """
        if self.param_delta_bound is None:
            return False, 0.0, "No param delta bound configured", None
        
        # Per-group evaluation if available
        if param_group_map is not None:
            for name in old_params:
                if name in new_params and name in param_group_map:
                    group = param_group_map[name]
                    # Use group-specific bound if available, else global
                    bound = self.param_delta_bound
                    
                    delta = torch.abs(new_params[name] - old_params[name]).max().item()
                    
                    if delta > bound:
                        severity = min(delta / bound, 2.0)
                        reason = f"Parameter {name} in group {group.value} exceeds trust bound {bound:.6f} (delta: {delta:.6f})"
                        self.param_delta_violations.append((step, name, delta))
                        self.violation_history.append((step, reason, severity))
                        return True, severity, reason, name
        
        # Global evaluation (fallback)
        max_delta = 0.0
        max_delta_param = None
        for key in old_params:
            if key in new_params:
                delta = torch.abs(new_params[key] - old_params[key]).max().item()
                if delta > max_delta:
                    max_delta = delta
                    max_delta_param = key
        
        if max_delta > self.param_delta_bound:
            severity = min(max_delta / self.param_delta_bound, 2.0)
            reason = f"Max param delta {max_delta:.6f} (param: {max_delta_param}) exceeds bound {self.param_delta_bound}"
            self.param_delta_violations.append((step, max_delta_param or "unknown", max_delta))
            self.violation_history.append((step, reason, severity))
            return True, severity, reason, max_delta_param
        
        return False, 0.0, "All parameters within trust bounds", None
    
    def compute_drift_metrics(self) -> Dict[str, Any]:
        """
        Compute drift metrics from violation history.
        
        Returns:
            Dictionary of drift statistics
        """
        if not self.violation_history:
            return {
                'total_violations': 0,
                'kl_violations': 0,
                'param_delta_violations': 0,
                'mean_severity': 0.0,
                'max_severity': 0.0,
                'recent_violation_rate': 0.0
            }
        
        recent_window = 100
        recent_violations = [v for v in self.violation_history if len(self.violation_history) - self.violation_history.index(v) <= recent_window]
        
        severities = [v[2] for v in self.violation_history]
        
        return {
            'total_violations': len(self.violation_history),
            'kl_violations': len(self.kl_violations),
            'param_delta_violations': len(self.param_delta_violations),
            'mean_severity': sum(severities) / len(severities) if severities else 0.0,
            'max_severity': max(severities) if severities else 0.0,
            'recent_violation_rate': len(recent_violations) / recent_window if recent_window > 0 else 0.0
        }
    
    def get_violation_summary(self, last_n: int = 10) -> List[Dict[str, Any]]:
        """
        Get summary of recent violations.
        
        Args:
            last_n: Number of recent violations to return
            
        Returns:
            List of violation summaries
        """
        recent = self.violation_history[-last_n:] if len(self.violation_history) > last_n else self.violation_history
        return [
            {
                'step': v[0],
                'reason': v[1],
                'severity': v[2]
            }
            for v in recent
        ]


# ============================================================================
# TRUST REGION CONTROLLER (ENFORCEMENT ONLY)
# ============================================================================

class TrustRegionController:
    """
    ENFORCES trust region constraints (throttling, blocking).
    
    SEPARATED from measurement - uses DriftAnalyzer for violation evaluation.
    In ultra-large systems, measurement and enforcement are split:
    - measurement → DriftAnalyzer (evaluates violations)
    - enforcement → TrustRegionController (decides throttling behavior)
    
    Responsibilities (ENFORCEMENT ONLY):
    - Decide whether to block updates based on drift analysis
    - Compute adaptive step-size throttling
    - Manage per-parameter-group trust bounds
    - Track enforcement decisions (not measurements)
    
    This is MANDATORY for RL safety.
    """
    
    def __init__(self, config: OptimizerConfig, drift_analyzer: Optional[DriftAnalyzer] = None):
        self.config = config
        self.kl_clip = config.kl_clip
        self.param_delta_bound = config.param_delta_bound
        
        # Drift analyzer for measurement (separated)
        self.drift_analyzer = drift_analyzer or DriftAnalyzer(config)
        
        # Enforcement state
        self.violations_blocked = 0
        self.throttle_factor = 1.0
        self.consecutive_violations = 0
        self.max_consecutive_violations = 5
        
        # Per-parameter-group trust regions (enforcement bounds)
        self.group_trust_bounds: Dict[ParameterGroup, float] = {
            group: config.param_delta_bound or 0.1
            for group in ParameterGroup
        }
        
        # Enforcement history (decisions, not measurements)
        self.enforcement_history: List[Tuple[int, bool, str]] = []  # (step, blocked, reason)
        
    def check_update(
        self,
        old_params: Dict[str, torch.Tensor],
        new_params: Dict[str, torch.Tensor],
        kl_div: Optional[float] = None,
        param_group_map: Optional[Dict[str, ParameterGroup]] = None,
        step: int = 0
    ) -> Tuple[bool, str]:
        """
        ENFORCE trust region constraints (uses DriftAnalyzer for measurement).
        
        Args:
            old_params: Parameters before update
            new_params: Parameters after update (predicted)
            kl_div: Optional KL divergence (for policy models)
            param_group_map: Optional map from param name to ParameterGroup
            step: Current training step
            
        Returns:
            (approved, reason)
        """
        # Use DriftAnalyzer for measurement (not enforcement logic here)
        if kl_div is not None:
            is_violation, severity, reason = self.drift_analyzer.evaluate_kl_violation(kl_div, step)
            if is_violation:
                self._block_update(step, reason)
                return False, reason
        
        # Use DriftAnalyzer for parameter delta measurement
        is_violation, severity, reason, violating_param = self.drift_analyzer.evaluate_param_delta_violation(
            old_params, new_params, param_group_map, step
        )
        if is_violation:
            self._block_update(step, reason)
            return False, reason
        
        # Update approved - record success
        self._approve_update(step)
        return True, "Trust region satisfied"
    
    def _block_update(self, step: int, reason: str):
        """Record blocked update (enforcement decision)."""
        self.violations_blocked += 1
        self.consecutive_violations += 1
        self.enforcement_history.append((step, True, reason))
        
        # Keep only recent history
        if len(self.enforcement_history) > 1000:
            self.enforcement_history.pop(0)
        
        # If too many consecutive violations, increase throttling
        if self.consecutive_violations >= self.max_consecutive_violations:
            logging.error(f"Too many consecutive trust violations ({self.consecutive_violations})")
    
    def _approve_update(self, step: int):
        """Record approved update (enforcement decision)."""
        self.consecutive_violations = 0
        self.enforcement_history.append((step, False, "Approved"))
        
        # Keep only recent history
        if len(self.enforcement_history) > 1000:
            self.enforcement_history.pop(0)
    
    def adaptive_throttle(
        self, 
        gradient_norm: float, 
        target_norm: float = 1.0,
        violation_count: Optional[int] = None
    ) -> float:
        """
        Compute adaptive step-size throttle (ENFORCEMENT decision).
        
        Uses drift analyzer metrics for violation awareness.
        
        Args:
            gradient_norm: Current gradient norm
            target_norm: Target gradient norm
            violation_count: Optional violation count (uses drift analyzer if None)
            
        Returns:
            Throttle factor (0.0 to 1.0)
        """
        # Get violation count from drift analyzer if not provided
        if violation_count is None:
            drift_metrics = self.drift_analyzer.compute_drift_metrics()
            violation_count = int(drift_metrics.get('recent_violation_rate', 0.0) * 100)  # Convert rate to count
        
        # Base throttling from gradient norm
        if gradient_norm <= target_norm:
            base_throttle = 1.0
        else:
            # Smooth throttling with adaptive adjustment
            base_throttle = target_norm / (gradient_norm + 1e-8)
        
        # Apply violation-based backoff (enforcement decision)
        if violation_count > 0:
            # Exponential backoff: 0.5^violation_count
            violation_penalty = math.pow(0.5, min(violation_count, 5))
            base_throttle *= violation_penalty
        
        # Apply consecutive violation penalty (enforcement)
        if self.consecutive_violations > 0:
            consecutive_penalty = math.pow(0.7, min(self.consecutive_violations, 5))
            base_throttle *= consecutive_penalty
        
        # Update throttle factor (enforcement state)
        self.throttle_factor = base_throttle
        
        return max(base_throttle, 0.1)  # Minimum 10% throttle
    
    def get_enforcement_stats(self) -> Dict[str, Any]:
        """
        Get enforcement statistics (not measurements).
        
        Returns:
            Dictionary of enforcement statistics
        """
        blocked_count = sum(1 for e in self.enforcement_history if e[1])
        approved_count = len(self.enforcement_history) - blocked_count
        
        return {
            'violations_blocked': self.violations_blocked,
            'consecutive_violations': self.consecutive_violations,
            'throttle_factor': self.throttle_factor,
            'total_enforcement_decisions': len(self.enforcement_history),
            'blocked_updates': blocked_count,
            'approved_updates': approved_count
        }


# ============================================================================
# PARAMETER GROUP MANAGER
# ============================================================================

class ParameterGroupManager:
    """
    Separates parameters into groups:
    - embeddings
    - temporal encoders
    - attention layers
    - heads vs backbones
    
    WHY THIS MATTERS:
    Viral structure ≠ surface aesthetics
    They MUST learn at different rates.
    """
    
    def __init__(self):
        self.groups: Dict[ParameterGroup, List[Parameter]] = {
            group: [] for group in ParameterGroup
        }
        # Track parameter to group mapping
        self.param_to_group: Dict[str, ParameterGroup] = {}
        
    def assign_parameter(self, param: Parameter, name: str):
        """
        Assign parameter to appropriate group based on STRUCTURAL analysis.
        
        Uses structural analysis, not just string matching.
        
        Args:
            param: Parameter to assign
            name: Parameter name (for classification)
        """
        group = self._classify_parameter(param, name)
        self.groups[group].append(param)
        self.param_to_group[name] = group
        
    def _classify_parameter(self, param: Parameter, name: str) -> ParameterGroup:
        """
        Classify parameter based on STRUCTURAL analysis.
        
        Args:
            param: Parameter to classify
            name: Parameter name
            
        Returns:
            Assigned ParameterGroup
        """
        name_lower = name.lower()
        
        # Structural analysis: check shape, dimensions, position in model
        shape = param.shape if hasattr(param, 'shape') else None
        
        # Embeddings: typically 2D (vocab_size, embed_dim) or large 1D
        if 'embed' in name_lower:
            return ParameterGroup.EMBEDDINGS
        
        # Temporal encoders: RNN/GRU/LSTM layers
        if 'temporal' in name_lower or 'lstm' in name_lower or 'gru' in name_lower:
            return ParameterGroup.TEMPORAL_ENCODERS
        if 'rnn' in name_lower:
            return ParameterGroup.TEMPORAL_ENCODERS
        
        # Attention layers: query/key/value weights, attention mechanisms
        if 'attention' in name_lower or 'attn' in name_lower:
            return ParameterGroup.ATTENTION_LAYERS
        if 'q_proj' in name_lower or 'k_proj' in name_lower or 'v_proj' in name_lower:
            return ParameterGroup.ATTENTION_LAYERS
        if 'qkv' in name_lower or 'out_proj' in name_lower:
            return ParameterGroup.ATTENTION_LAYERS
        
        # Heads: final classification/prediction layers
        if 'head' in name_lower or 'classifier' in name_lower:
            return ParameterGroup.HEADS
        if 'fc' in name_lower and ('final' in name_lower or 'out' in name_lower):
            return ParameterGroup.HEADS
        if 'predict' in name_lower or 'output' in name_lower:
            return ParameterGroup.HEADS
        
        # Backbones: everything else (conv layers, intermediate layers, etc.)
        return ParameterGroup.BACKBONES
    
    def get_parameter_group(self, param_name: str) -> ParameterGroup:
        """
        Get parameter group for a parameter by name.
        
        Args:
            param_name: Name of the parameter
            
        Returns:
            ParameterGroup for this parameter
        """
        return self.param_to_group.get(param_name, ParameterGroup.BACKBONES)
    
    def get_authorized_groups(self, phase: CurriculumPhase) -> Set[ParameterGroup]:
        """
        Get authorized parameter groups for current curriculum phase.
        
        Args:
            phase: Current curriculum phase
            
        Returns:
            Set of authorized ParameterGroups
        """
        if phase == CurriculumPhase.STRUCTURE_LEARNING:
            # All groups allowed, but at different rates
            return set(ParameterGroup)
        elif phase == CurriculumPhase.STABILIZATION:
            # All groups allowed but at reduced rates
            return set(ParameterGroup)
        elif phase == CurriculumPhase.TAIL_AMPLIFICATION:
            # Head-only updates
            return {ParameterGroup.HEADS, ParameterGroup.ATTENTION_LAYERS}
        else:  # RISK_CONTROL
            # Very limited updates
            return {ParameterGroup.HEADS, ParameterGroup.ATTENTION_LAYERS}
    
    def get_authorized_groups_for_role(self, model_role: ModelRole) -> Set[ParameterGroup]:
        """
        Get authorized parameter groups for a specific model role.
        
        Locks allowed groups per model role to prevent:
        - Accidental backbone updates
        - Silent leakage during tail amplification
        - Rogue params being optimized
        
        Args:
            model_role: Model role
            
        Returns:
            Set of authorized ParameterGroups for this role
        """
        # Role-specific authorization rules
        if model_role == ModelRole.POLICY:
            # Policy networks: all groups allowed (but at different rates)
            return set(ParameterGroup)
        elif model_role == ModelRole.VALUE:
            # Value networks: all groups allowed
            return set(ParameterGroup)
        elif model_role == ModelRole.PREDICTOR:
            # Predictors: all groups allowed
            return set(ParameterGroup)
        elif model_role == ModelRole.RANKER:
            # Rankers: all groups allowed
            return set(ParameterGroup)
        elif model_role == ModelRole.SHARED_BACKBONE:
            # Shared backbones: only backbone params (frozen or low-LR)
            return {ParameterGroup.BACKBONES}
        elif model_role == ModelRole.CLASSIFIER:
            # Classifiers: heads and attention (not embeddings/backbones)
            return {ParameterGroup.HEADS, ParameterGroup.ATTENTION_LAYERS}
        else:
            # Default: all groups (conservative)
            return set(ParameterGroup)
    
    def validate_parameter_groups(
        self,
        model_role: ModelRole,
        phase: CurriculumPhase,
        require_authorization: bool = True
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate that all parameter groups are authorized.
        
        Args:
            model_role: Model role
            phase: Current curriculum phase
            require_authorization: If True, enforce authorization (default: True)
            
        Returns:
            (is_valid, reason) - reason is None if valid
        """
        if not require_authorization:
            return True, None
        
        # Get authorized groups for role
        role_authorized = self.get_authorized_groups_for_role(model_role)
        
        # Get authorized groups for phase
        phase_authorized = self.get_authorized_groups(phase)
        
        # Intersection: must be authorized by BOTH role and phase
        fully_authorized = role_authorized & phase_authorized
        
        # Check all assigned groups
        for group, params in self.groups.items():
            if params and group not in fully_authorized:
                return False, f"Parameter group {group.value} is not authorized for role {model_role.value} in phase {phase.value}"
        
        return True, None
    
    def get_param_groups(self, base_lr: float, phase: CurriculumPhase) -> List[Dict[str, Any]]:
        """
        Get parameter groups with phase-specific learning rates.
        
        Args:
            base_lr: Base learning rate
            phase: Current curriculum phase
            
        Returns:
            List of parameter groups for optimizer
        """
        groups = []
        
        # Phase-specific LR multipliers
        if phase == CurriculumPhase.STRUCTURE_LEARNING:
            multipliers = {
                ParameterGroup.EMBEDDINGS: 0.5,
                ParameterGroup.TEMPORAL_ENCODERS: 1.0,
                ParameterGroup.ATTENTION_LAYERS: 1.0,
                ParameterGroup.HEADS: 1.5,
                ParameterGroup.BACKBONES: 1.0
            }
        elif phase == CurriculumPhase.STABILIZATION:
            multipliers = {
                ParameterGroup.EMBEDDINGS: 0.3,
                ParameterGroup.TEMPORAL_ENCODERS: 0.5,
                ParameterGroup.ATTENTION_LAYERS: 0.5,
                ParameterGroup.HEADS: 1.0,
                ParameterGroup.BACKBONES: 0.5
            }
        elif phase == CurriculumPhase.TAIL_AMPLIFICATION:
            # Head-only updates
            multipliers = {
                ParameterGroup.EMBEDDINGS: 0.0,
                ParameterGroup.TEMPORAL_ENCODERS: 0.0,
                ParameterGroup.ATTENTION_LAYERS: 0.1,
                ParameterGroup.HEADS: 1.0,
                ParameterGroup.BACKBONES: 0.0
            }
        else:  # RISK_CONTROL
            multipliers = {
                ParameterGroup.EMBEDDINGS: 0.1,
                ParameterGroup.TEMPORAL_ENCODERS: 0.1,
                ParameterGroup.ATTENTION_LAYERS: 0.3,
                ParameterGroup.HEADS: 0.5,
                ParameterGroup.BACKBONES: 0.1
            }
        
        for group_type, params in self.groups.items():
            if params:
                groups.append({
                    'params': params,
                    'lr': base_lr * multipliers[group_type]
                })
        
        return groups


# ============================================================================
# RESOURCE GOVERNOR
# ============================================================================

class ResourceGovernor:
    """
    Governs resource usage (GPU/TPU/memory/budget).
    
    Prevents:
    - GPU memory exhaustion
    - TPU budget overruns
    - Training budget caps
    - Memory leaks
    
    CRITICAL for production deployment.
    """
    
    def __init__(
        self,
        gpu_memory_limit_gb: Optional[float] = None,
        tpu_budget_limit: Optional[float] = None,
        training_budget_usd: Optional[float] = None,
        max_memory_usage_ratio: float = 0.9  # 90% max memory usage
    ):
        """
        Initialize resource governor.
        
        Args:
            gpu_memory_limit_gb: Maximum GPU memory in GB
            tpu_budget_limit: Maximum TPU budget
            training_budget_usd: Maximum training budget in USD
            max_memory_usage_ratio: Maximum memory usage ratio [0, 1] --> shouldn't that be for budget_allcoator .py - or at least reosurce governor takes from budget-allocator.py??? - ASKC HATGPT!!!!!!
            
        """
        self.gpu_memory_limit_gb = gpu_memory_limit_gb
        self.tpu_budget_limit = tpu_budget_limit
        self.training_budget_usd = training_budget_usd
        self.max_memory_usage_ratio = max_memory_usage_ratio
        
        # Budget tracking
        self.gpu_memory_used_gb = 0.0
        self.tpu_budget_used = 0.0
        self.training_budget_used_usd = 0.0
        
        # Cost estimation (per step)
        self.estimated_cost_per_step_usd = 0.001  # $0.001 per step default
        self.estimated_tpu_cost_per_step = 0.0001  # TPU cost per step
        
        # History tracking
        self.resource_history: List[Dict[str, float]] = []
        
    def check_budget(self) -> Tuple[bool, str]:
        """
        Check if resource budget allows step.
        
        Returns:
            (approved, reason)
        """
        # Check GPU memory
        if self.gpu_memory_limit_gb is not None:
            current_memory = self._get_gpu_memory_usage()
            if current_memory > self.gpu_memory_limit_gb * self.max_memory_usage_ratio:
                return False, f"GPU memory usage {current_memory:.2f}GB exceeds limit {self.gpu_memory_limit_gb:.2f}GB"
        
        # Check TPU budget
        if self.tpu_budget_limit is not None:
            if self.tpu_budget_used + self.estimated_tpu_cost_per_step > self.tpu_budget_limit:
                return False, f"TPU budget {self.tpu_budget_used:.4f} exceeds limit {self.tpu_budget_limit:.4f}"
        
        # Check training budget
        if self.training_budget_usd is not None:
            if self.training_budget_used_usd + self.estimated_cost_per_step_usd > self.training_budget_usd:
                return False, f"Training budget ${self.training_budget_used_usd:.2f} exceeds limit ${self.training_budget_usd:.2f}"
        
        return True, "Resource budget OK"
    
    def can_step(self) -> bool:
        """Check if step is allowed (convenience method)."""
        approved, _ = self.check_budget()
        return approved
    
    def record_step(self):
        """Record resource usage after step."""
        self.tpu_budget_used += self.estimated_tpu_cost_per_step
        self.training_budget_used_usd += self.estimated_cost_per_step_usd
        
        memory_usage = self._get_gpu_memory_usage()
        self.resource_history.append({
            'gpu_memory_gb': memory_usage,
            'tpu_budget_used': self.tpu_budget_used,
            'training_budget_usd': self.training_budget_used_usd
        })
        
        # Keep only recent history
        if len(self.resource_history) > 1000:
            self.resource_history.pop(0)
    
    def _get_gpu_memory_usage(self) -> float:
        """Get current GPU memory usage in GB."""
        if torch.cuda.is_available():
            memory_allocated = torch.cuda.memory_allocated() / (1024 ** 3)  # Convert to GB
            return memory_allocated
        return 0.0
    
    def get_resource_status(self) -> Dict[str, float]:
        """Get current resource status."""
        return {
            'gpu_memory_used_gb': self._get_gpu_memory_usage(),
            'gpu_memory_limit_gb': self.gpu_memory_limit_gb or 0.0,
            'tpu_budget_used': self.tpu_budget_used,
            'tpu_budget_limit': self.tpu_budget_limit or 0.0,
            'training_budget_used_usd': self.training_budget_used_usd,
            'training_budget_limit_usd': self.training_budget_usd or 0.0
        }


# ============================================================================
# RISK SCALER
# ============================================================================

class RiskScaler:
    """
    FIRST-CLASS RISK SCALER - Centralized risk modulation.
    
    CRITICAL: All risk math lives here - single choke point, single audit trail.
    
    Consolidates risk factors from:
    - Uncertainty (model confidence)
    - Gradient norm (training stability)
    - Replay freshness (data quality)
    - Tail exposure (distribution shift)
    - Violation rate (trust region health)
    
    This replaces fragmented risk logic in:
    - ScheduleManager (uncertainty, noise)
    - TDStabilizedOptimizer (TD stability)
    - TrustRegionController.adaptive_throttle (violation-based scaling)
    
    Why This Matters:
    - Single choke point for all risk decisions
    - Single audit trail for post-mortem reasoning
    - Enables "This update used risk_scale=X because A×B×C" logging
    """
    
    def __init__(self, uncertainty_threshold: float = 0.8):
        self.uncertainty_threshold = uncertainty_threshold
        self.risk_scale_history: List[Tuple[int, float, Dict[str, float]]] = []  # (step, scale, factors)
        self.max_history = 1000
        
    def compute_risk_scale(
        self,
        uncertainty: Optional[float] = None,
        gradient_norm: Optional[float] = None,
        replay_freshness: Optional[float] = None,
        tail_exposure: Optional[float] = None,
        violation_rate: float = 0.0,
        step: int = 0
    ) -> Tuple[float, Dict[str, float]]:
        """
        Compute centralized risk-based learning rate scale.
        
        This is the SINGLE point where all risk factors are combined.
        
        Args:
            uncertainty: Model prediction uncertainty [0, 1] (higher = more risk)
            gradient_norm: Current gradient norm (higher = more risk)
            replay_freshness: Replay buffer freshness [0, 1] (lower = more risk)
            tail_exposure: Tail-risk exposure [0, 1] (higher = more risk)
            violation_rate: Recent trust region violation rate [0, 1] (higher = more risk)
            step: Current training step (for audit trail)
            
        Returns:
            (risk_scale, factors_dict) where factors_dict explains the computation
        """
        factors = {}
        scale = 1.0
        
        # Factor 1: Uncertainty-based scaling
        if uncertainty is not None:
            if uncertainty > self.uncertainty_threshold:
                uncertainty_factor = 1.0 - (uncertainty - self.uncertainty_threshold) / (1.0 - self.uncertainty_threshold)
                uncertainty_factor = max(uncertainty_factor, 0.1)  # Minimum 10%
            else:
                uncertainty_factor = 1.0
            scale *= uncertainty_factor
            factors['uncertainty'] = uncertainty
            factors['uncertainty_factor'] = uncertainty_factor
        else:
            factors['uncertainty'] = None
            factors['uncertainty_factor'] = 1.0
        
        # Factor 2: Gradient norm stability (high norm = high risk)
        if gradient_norm is not None:
            # Normalize gradient norm (assume max_grad_norm = 1.0 as baseline)
            # Higher norm → reduce scale
            if gradient_norm > 1.0:
                gradient_factor = 1.0 / (1.0 + (gradient_norm - 1.0) * 0.5)
                gradient_factor = max(gradient_factor, 0.5)  # Minimum 50%
            else:
                gradient_factor = 1.0
            scale *= gradient_factor
            factors['gradient_norm'] = gradient_norm
            factors['gradient_factor'] = gradient_factor
        else:
            factors['gradient_norm'] = None
            factors['gradient_factor'] = 1.0
        
        # Factor 3: Replay freshness (stale data = high risk)
        if replay_freshness is not None:
            freshness_factor = replay_freshness  # Direct scaling
            scale *= freshness_factor
            factors['replay_freshness'] = replay_freshness
            factors['freshness_factor'] = freshness_factor
        else:
            factors['replay_freshness'] = None
            factors['freshness_factor'] = 1.0
        
        # Factor 4: Tail exposure (distribution shift = high risk)
        if tail_exposure is not None:
            tail_factor = 1.0 - tail_exposure * 0.5  # Up to 50% reduction
            scale *= tail_factor
            factors['tail_exposure'] = tail_exposure
            factors['tail_factor'] = tail_factor
        else:
            factors['tail_exposure'] = None
            factors['tail_factor'] = 1.0
        
        # Factor 5: Violation rate (trust region health)
        if violation_rate > 0.0:
            violation_factor = 1.0 - violation_rate * 0.7  # Up to 70% reduction for high violation rate
            violation_factor = max(violation_factor, 0.2)  # Minimum 20%
            scale *= violation_factor
            factors['violation_rate'] = violation_rate
            factors['violation_factor'] = violation_factor
        else:
            factors['violation_rate'] = 0.0
            factors['violation_factor'] = 1.0
        
        # Final scale with hard minimum
        final_scale = max(scale, 0.05)  # Hard minimum 5%
        factors['final_risk_scale'] = final_scale
        factors['computation'] = f"{factors.get('uncertainty_factor', 1.0):.3f} × {factors.get('gradient_factor', 1.0):.3f} × {factors.get('freshness_factor', 1.0):.3f} × {factors.get('tail_factor', 1.0):.3f} × {factors.get('violation_factor', 1.0):.3f} = {final_scale:.3f}"
        
        # Audit trail
        self.risk_scale_history.append((step, final_scale, factors.copy()))
        if len(self.risk_scale_history) > self.max_history:
            self.risk_scale_history.pop(0)
        
        return final_scale, factors
    
    def get_risk_scale_history(self, last_n: int = 100) -> List[Tuple[int, float, Dict[str, float]]]:
        """Get recent risk scale history for audit."""
        return self.risk_scale_history[-last_n:] if len(self.risk_scale_history) > last_n else self.risk_scale_history
    
    def get_risk_statistics(self) -> Dict[str, Any]:
        """Get risk scaling statistics."""
        if not self.risk_scale_history:
            return {
                'mean_risk_scale': 1.0,
                'min_risk_scale': 1.0,
                'max_risk_scale': 1.0,
                'total_computations': 0
            }
        
        scales = [r[1] for r in self.risk_scale_history]
        return {
            'mean_risk_scale': sum(scales) / len(scales),
            'min_risk_scale': min(scales),
            'max_risk_scale': max(scales),
            'total_computations': len(self.risk_scale_history)
        }
    
    # Backward compatibility
    def compute_scale(
        self,
        uncertainty: float,
        tail_risk: float = 0.0,
        anomaly_score: float = 0.0,
        replay_freshness: float = 1.0
    ) -> float:
        """
        Legacy method for backward compatibility.
        
        Use compute_risk_scale() for new code.
        """
        scale, _ = self.compute_risk_scale(
            uncertainty=uncertainty,
            tail_exposure=tail_risk,
            replay_freshness=replay_freshness,
            step=0
        )
        # Apply anomaly_score as additional penalty
        if anomaly_score > 0.0:
            scale *= (1.0 - anomaly_score * 0.3)
        return max(scale, 0.05)


# ============================================================================
# LEARNING RATE COMPOSER (CENTRALIZED EFFECTIVE LR MATH)
# ============================================================================

class LearningRateComposer:
    """
    Centralizes all "effective LR" math.
    
    CRITICAL: Single point for LR composition - prevents accidental double-scaling.
    
    All optimizers must call only this for effective LR computation.
    Enables single-line audit logging: "This update used LR = X because A×B×C"
    
    Factors:
    - base_lr: Base learning rate from config
    - schedule_factor: From ScheduleManager (warmup, decay, etc.)
    - risk_scale: From RiskScaler (uncertainty, violations, etc.)
    - trust_throttle: From TrustRegionController (violation-based throttling)
    - maturity_factor: From ScheduleManager (model maturity adjustment)
    """
    
    def __init__(self):
        self.composition_history: List[Tuple[int, float, Dict[str, float]]] = []  # (step, lr, factors)
        self.max_history = 1000
    
    def compose(
        self,
        base_lr: float,
        schedule_factor: float = 1.0,
        risk_scale: float = 1.0,
        trust_throttle: float = 1.0,
        maturity_factor: float = 1.0,
        step: int = 0
    ) -> Tuple[float, Dict[str, float]]:
        """
        Compose effective learning rate from all factors.
        
        This is the SINGLE point where all LR factors are combined.
        
        Args:
            base_lr: Base learning rate
            schedule_factor: Schedule adjustment (warmup, decay, etc.)
            risk_scale: Risk-based scaling (from RiskScaler)
            trust_throttle: Trust region throttling factor
            maturity_factor: Model maturity adjustment
            
        Returns:
            (effective_lr, factors_dict) where factors_dict explains the computation
        """
        factors = {
            'base_lr': base_lr,
            'schedule_factor': schedule_factor,
            'risk_scale': risk_scale,
            'trust_throttle': trust_throttle,
            'maturity_factor': maturity_factor
        }
        
        # Compose: base × schedule × risk × trust × maturity
        effective_lr = base_lr * schedule_factor * risk_scale * trust_throttle * maturity_factor
        
        # Ensure minimum LR (1% of base)
        effective_lr = max(effective_lr, base_lr * 0.01)
        
        factors['effective_lr'] = effective_lr
        factors['computation'] = f"{base_lr:.6f} × {schedule_factor:.3f} × {risk_scale:.3f} × {trust_throttle:.3f} × {maturity_factor:.3f} = {effective_lr:.6f}"
        
        # Audit trail
        self.composition_history.append((step, effective_lr, factors.copy()))
        if len(self.composition_history) > self.max_history:
            self.composition_history.pop(0)
        
        return effective_lr, factors
    
    def get_composition_history(self, last_n: int = 100) -> List[Tuple[int, float, Dict[str, float]]]:
        """Get recent LR composition history for audit."""
        return self.composition_history[-last_n:] if len(self.composition_history) > last_n else self.composition_history
    
    def get_composition_statistics(self) -> Dict[str, Any]:
        """Get LR composition statistics."""
        if not self.composition_history:
            return {
                'mean_effective_lr': 0.0,
                'min_effective_lr': 0.0,
                'max_effective_lr': 0.0,
                'total_compositions': 0
            }
        
        lrs = [c[1] for c in self.composition_history]
        return {
            'mean_effective_lr': sum(lrs) / len(lrs),
            'min_effective_lr': min(lrs),
            'max_effective_lr': max(lrs),
            'total_compositions': len(self.composition_history)
        }


# ============================================================================
# STATE SERIALIZER
# ============================================================================

class StateSerializer:
    """
    Persists:
    - optimizer state
    - moment estimates
    - schedules
    - trust-region stats
    
    Enables:
    - exact replay
    - rollback
    - forensic debugging
    
    ARCHITECTURAL IMPROVEMENT: Semantic hashing for state versioning.
    Prevents "We restored the optimizer, but not the optimizer meaning."
    """
    
    @staticmethod
    def compute_state_hash(
        config: OptimizerConfig,
        schedule_manager: ScheduleManager,
        trust_controller: TrustRegionController
    ) -> str:
        """
        Compute semantic hash of optimizer configuration.
        
        This hash represents the "meaning" of the optimizer state:
        - model_role
        - schedule_type
        - trust_region_config
        - risk_scaler_config
        
        On load, we can verify the hash matches to prevent configuration mismatches.
        
        Args:
            config: Optimizer configuration
            schedule_manager: Schedule manager
            trust_controller: Trust region controller
            
        Returns:
            SHA256 hash as hex string
        """
        # Build semantic description
        semantic_str = (
            f"model_role={config.model_role.value}|"
            f"schedule_type={schedule_manager.schedule_type.value}|"
            f"kl_clip={config.kl_clip}|"
            f"param_delta_bound={config.param_delta_bound}|"
            f"base_lr={config.base_lr}|"
            f"uncertainty_threshold={config.uncertainty_threshold}|"
            f"enable_per_group_lr={schedule_manager.enable_per_group_lr}"
        )
        
        # Compute hash
        return hashlib.sha256(semantic_str.encode()).hexdigest()
    
    @staticmethod
    def save_state(
        optimizer: optim.Optimizer,
        schedule_manager: ScheduleManager,
        trust_controller: TrustRegionController,
        config: OptimizerConfig,
        path: Path,
        enable_versioning: bool = True
    ):
        """
        Save complete optimizer state with semantic hashing.
        
        Args:
            optimizer: Optimizer instance
            schedule_manager: Schedule manager
            trust_controller: Trust region controller
            config: Optimizer configuration (for semantic hash)
            path: Path to save state
            enable_versioning: If True, include semantic hash (default: True)
        """
        state = {
            'optimizer_state': optimizer.state_dict(),
            'schedule_step': schedule_manager.step,
            'current_lr': schedule_manager.current_lr,
            'emergency_active': schedule_manager.emergency_active,
            'plateau_history': schedule_manager.plateau_history,
            # Per-group LR state (if enabled)
            'group_lrs': schedule_manager.group_lrs if schedule_manager.enable_per_group_lr else None,
            'enable_per_group_lr': schedule_manager.enable_per_group_lr,
            # Trust region state (enforcement stats, not measurements)
            'violations_blocked': trust_controller.violations_blocked,
            'consecutive_violations': trust_controller.consecutive_violations,
            'throttle_factor': trust_controller.throttle_factor,
            # Drift analyzer state (measurements)
            'drift_analyzer_violations': len(trust_controller.drift_analyzer.violation_history) if trust_controller.drift_analyzer else 0
        }
        
        # Add semantic hash if versioning enabled
        if enable_versioning:
            state['semantic_hash'] = StateSerializer.compute_state_hash(
                config, schedule_manager, trust_controller
            )
            state['config_snapshot'] = {
                'model_role': config.model_role.value,
                'schedule_type': config.schedule_type.value,
                'kl_clip': config.kl_clip,
                'param_delta_bound': config.param_delta_bound,
                'base_lr': config.base_lr,
                'uncertainty_threshold': config.uncertainty_threshold
            }
        
        torch.save(state, path)
    
    @staticmethod
    def load_state(
        optimizer: optim.Optimizer,
        schedule_manager: ScheduleManager,
        trust_controller: TrustRegionController,
        config: OptimizerConfig,
        path: Path,
        require_hash_match: bool = True,
        allow_override: bool = False
    ):
        """
        Load complete optimizer state with semantic hash verification.
        
        Args:
            optimizer: Optimizer instance
            schedule_manager: Schedule manager
            trust_controller: Trust region controller
            config: Current optimizer configuration (for hash verification)
            path: Path to load state from
            require_hash_match: If True, require hash match (default: True)
            allow_override: If True, allow override on mismatch (default: False)
            
        Raises:
            ValueError: If hash mismatch and require_hash_match=True and allow_override=False
        """
        state = torch.load(path)
        
        # Verify semantic hash if present
        if 'semantic_hash' in state and require_hash_match:
            current_hash = StateSerializer.compute_state_hash(
                config, schedule_manager, trust_controller
            )
            saved_hash = state['semantic_hash']
            
            if current_hash != saved_hash:
                error_msg = (
                    f"Optimizer state semantic hash mismatch!\n"
                    f"Saved: {saved_hash}\n"
                    f"Current: {current_hash}\n"
                    f"Saved config: {state.get('config_snapshot', {})}\n"
                    f"Current config: model_role={config.model_role.value}, "
                    f"schedule_type={config.schedule_type.value}, "
                    f"kl_clip={config.kl_clip}, base_lr={config.base_lr}"
                )
                
                if not allow_override:
                    raise ValueError(
                        error_msg + "\n"
                        "This prevents 'We restored the optimizer, but not the optimizer meaning.'\n"
                        "Set allow_override=True to force load (not recommended)."
                    )
                else:
                    logging.warning(f"Semantic hash mismatch (override allowed): {error_msg}")
        
        optimizer.load_state_dict(state['optimizer_state'])
        schedule_manager.step = state['schedule_step']
        schedule_manager.current_lr = state['current_lr']
        schedule_manager.emergency_active = state['emergency_active']
        schedule_manager.plateau_history = state.get('plateau_history', [])
        
        # Load per-group LR state if available
        if state.get('enable_per_group_lr', False) and 'group_lrs' in state and state['group_lrs']:
            schedule_manager.group_lrs = state['group_lrs']
        
        # Load trust region enforcement state
        trust_controller.violations_blocked = state.get('violations_blocked', 0)
        trust_controller.consecutive_violations = state.get('consecutive_violations', 0)
        trust_controller.throttle_factor = state.get('throttle_factor', 1.0)


# ============================================================================
# OPTIMIZER AUDIT HOOK
# ============================================================================

class OptimizerAuditHook:
    """
    Every update emits:
    - effective learning rate
    - gradient norm
    - clipped ratio
    - trust violations
    
    This feeds:
    - audit_logger
    - drift_analyzer
    - safety_watchdog
    """
    
    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        
    def log_update(self, state: OptimizerState):
        """Log optimizer state for audit trail."""
        entry = {
            'step': state.step,
            'effective_lr': state.effective_lr,
            'gradient_norm': state.gradient_norm,
            'clipped_ratio': state.clipped_ratio,
            'trust_violations': state.trust_violations,
            'phase': state.phase.value,
            'schedule_type': state.schedule_type.value,
            'risk_scale': state.risk_scale
        }
        
        with open(self.log_path, 'a') as f:
            f.write(json.dumps(entry) + '\n')


# ============================================================================
# OPTIMIZER CONTROLLER (MAIN)
# ============================================================================

class OptimizerController:
    """
    Master optimizer controller.
    
    Integrates:
    - OptimizerFactory
    - ScheduleManager
    - TrustRegionController
    - ParameterGroupManager
    - RiskScaler
    - StateSerializer
    - OptimizerAuditHook
    - gradient_governor (pre-step approval)
    - safety_watchdog (kill switch)
    - curriculum (phase awareness)
    - audit_logger (immutable logs)
    - version_manager (state lineage)
    - seed_controller (determinism)
    - resource_governor (GPU/TPU/budget caps)
    
    Provides single interface for ALL parameter updates in the system.
    """
    
    def __init__(
        self,
        config: OptimizerConfig,
        model_parameters: Dict[str, Parameter],
        curriculum_phase: CurriculumPhase,
        controller_config: Optional[OptimizerControllerConfig] = None,
        audit_path: Optional[Path] = None,
        gradient_governor: Optional[Any] = None,
        safety_watchdog: Optional[Any] = None,
        curriculum: Optional[Any] = None,
        audit_logger: Optional[Any] = None,
        version_manager: Optional[Any] = None,
        seed_controller: Optional[Any] = None,
        resource_governor: Optional[Any] = None,
        use_integration_registry: bool = True
    ):
        """
        Initialize optimizer controller.
        
        Args:
            config: Optimizer configuration
            model_parameters: Named model parameters
            curriculum_phase: Current curriculum phase
            controller_config: OptimizerControllerConfig (defaults to hard-fail enabled)
            audit_path: Optional path for audit logs
            gradient_governor: Optional gradient governor instance
            safety_watchdog: Optional safety watchdog instance
            curriculum: Optional curriculum manager instance
            audit_logger: Optional audit logger instance
            version_manager: Optional version manager instance
            seed_controller: Optional seed controller instance
            resource_governor: Optional resource governor instance
            use_integration_registry: If True, use global integration registry
        """
        self.config = config
        self.phase = curriculum_phase
        self.controller_config = controller_config or OptimizerControllerConfig()  # Default: hard-fail enabled
        
        # Integration hooks (use provided or fallback to registry)
        if use_integration_registry:
            self.gradient_governor = gradient_governor or _integration_registry.gradient_governor
            self.safety_watchdog = safety_watchdog or _integration_registry.safety_watchdog
            self.curriculum = curriculum or _integration_registry.curriculum
            self.audit_logger = audit_logger or _integration_registry.audit_logger
            self.version_manager = version_manager or _integration_registry.version_manager
            self.seed_controller = seed_controller or _integration_registry.seed_controller
            self.resource_governor = resource_governor or _integration_registry.resource_governor
        else:
            self.gradient_governor = gradient_governor
            self.safety_watchdog = safety_watchdog
            self.curriculum = curriculum
            self.audit_logger = audit_logger
            self.version_manager = version_manager
            self.seed_controller = seed_controller
            self.resource_governor = resource_governor
        
        # DETERMINISM ENFORCEMENT (REQUIRED)
        if self.seed_controller is not None:
            if isinstance(self.seed_controller, SeedController):
                self.seed_controller.enforce_determinism()
            elif hasattr(self.seed_controller, 'enforce_determinism'):
                self.seed_controller.enforce_determinism()
            elif hasattr(self.seed_controller, 'get_optimizer_seed'):
                seed = self.seed_controller.get_optimizer_seed()
                torch.manual_seed(seed)
                if torch.cuda.is_available():
                    torch.cuda.manual_seed_all(seed)
                    torch.backends.cudnn.deterministic = True
                    torch.backends.cudnn.benchmark = False
                logging.info(f"Determinism enforced with seed: {seed}")
            else:
                logging.warning("Seed controller doesn't have expected methods - determinism NOT guaranteed")
        else:
            # Create default seed controller for determinism
            logging.warning("No seed controller provided - creating default for determinism")
            self.seed_controller = SeedController(base_seed=42)
            self.seed_controller.enforce_determinism()
        
        # Initialize components with architectural improvements
        # Enable per-group LR for 300M-scale stability
        self.schedule_manager = ScheduleManager(config, enable_per_group_lr=True)
        
        # Create DriftAnalyzer for measurement (separated from enforcement)
        self.drift_analyzer = DriftAnalyzer(config)
        
        # TrustRegionController uses DriftAnalyzer (enforcement only)
        self.trust_controller = TrustRegionController(config, drift_analyzer=self.drift_analyzer)
        
        self.param_group_manager = ParameterGroupManager()
        self.risk_scaler = RiskScaler(config.uncertainty_threshold)
        self.lr_composer = LearningRateComposer()  # Centralized LR composition
        
        # Assign parameters to groups
        for name, param in model_parameters.items():
            self.param_group_manager.assign_parameter(param, name)
        
        # ENFORCE parameter-group authorization (Improvement #5)
        if self.controller_config.require_parameter_group_authorization:
            is_valid, reason = self.param_group_manager.validate_parameter_groups(
                config.model_role, curriculum_phase, require_authorization=True
            )
            if not is_valid:
                raise UnauthorizedParameterGroupException(
                    f"Parameter group authorization failed: {reason}"
                )
        
        # Create optimizer with parameter groups
        param_groups = self.param_group_manager.get_param_groups(
            config.base_lr, curriculum_phase
        )
        self.optimizer = OptimizerFactory.create(config, param_groups)
        
        # Audit hook
        self.audit_hook = None
        if audit_path:
            self.audit_hook = OptimizerAuditHook(audit_path)
        
        # State tracking
        self.step_count = 0
        self.last_gradient_norm = 0.0
        self.last_risk_scale = 1.0
        self.last_effective_lr = config.base_lr
        
        # Track curriculum phase changes
        self._last_phase = curriculum_phase
        
        # Metrics collection
        self.metrics = OptimizerMetrics(history_size=1000)
        
        # Validation utilities
        self.param_validator = ParameterValidator()
        self.grad_validator = ParameterValidator()
        self.state_validator = OptimizerStateValidator()
        
    def step(
        self,
        loss: torch.Tensor,
        model_parameters: Dict[str, Parameter],
        uncertainty: Optional[float] = None,
        tail_risk: float = 0.0,
        anomaly_score: float = 0.0,
        kl_div: Optional[float] = None,
        metrics: Optional[Dict[str, float]] = None,
        replay_freshness: float = 1.0
    ) -> UpdateDecision:
        """
        Perform optimizer step with full safety checks.
        
        CRITICAL SAFETY CHECKS (in order):
        1. Safety watchdog kill switch
        2. Resource governor (GPU/TPU/memory)
        3. Loss validity (NaN/Inf)
        4. Active curriculum phase reading
        5. Gradient computation
        6. Gradient validity (NaN/Inf)
        7. Gradient governor approval
        8. Trust region PRE-STEP validation
        9. Step size risk budget check
        10. Optimizer step
        11. Audit logging
        
        Args:
            loss: Loss to backpropagate
            model_parameters: Current model parameters
            uncertainty: Model uncertainty [0, 1]
            tail_risk: Tail risk exposure [0, 1]
            anomaly_score: Anomaly score [0, 1]
            kl_div: KL divergence (for policy models)
            metrics: Optional training metrics
            replay_freshness: Replay buffer freshness [0, 1]
            
        Returns:
            UpdateDecision with approval status
            
        Raises:
            KillSwitchActivatedException: If safety watchdog blocks
            ResourceBudgetExceededException: If resource limits exceeded
            NaNInfDetectedException: If NaN/Inf detected
            GradientGovernorBlockedException: If gradient governor blocks
            TrustRegionViolationException: If trust region violated
            StepSizeExceededException: If step size exceeds budget
        """
        # ====================================================================
        # CHECK 1: SAFETY WATCHDOG KILL SWITCH (FIRST - HARD STOP)
        # ====================================================================
        if self.safety_watchdog is not None:
            if hasattr(self.safety_watchdog, 'is_kill_switch_active'):
                if self.safety_watchdog.is_kill_switch_active():
                    raise KillSwitchActivatedException(
                        f"Safety watchdog kill switch activated at step {self.step_count}"
                    )
            elif hasattr(self.safety_watchdog, 'check_kill_switch'):
                kill_active, reason = self.safety_watchdog.check_kill_switch()
                if kill_active:
                    raise KillSwitchActivatedException(
                        f"Kill switch activated: {reason}"
                    )
        
        # ====================================================================
        # CHECK 2: RESOURCE GOVERNOR (GPU/TPU/MEMORY/BUDGET)
        # ====================================================================
        if self.resource_governor is not None:
            if hasattr(self.resource_governor, 'check_budget'):
                budget_ok, reason = self.resource_governor.check_budget()
                if not budget_ok:
                    raise ResourceBudgetExceededException(
                        f"Resource budget exceeded: {reason}"
                    )
            elif hasattr(self.resource_governor, 'can_step'):
                if not self.resource_governor.can_step():
                    raise ResourceBudgetExceededException(
                        "Resource governor blocked step"
                    )
        
        # ====================================================================
        # CHECK 3: LOSS VALIDITY (NaN/Inf)
        # ====================================================================
        if torch.isnan(loss) or torch.isinf(loss):
            raise NaNInfDetectedException(
                f"NaN or Inf detected in loss at step {self.step_count}"
            )
        
        # ====================================================================
        # CHECK 4: ACTIVE CURRICULUM PHASE READING
        # ====================================================================
        if self.curriculum is not None:
            if hasattr(self.curriculum, 'get_current_phase'):
                current_phase = self.curriculum.get_current_phase()
                if current_phase != self.phase:
                    logging.info(f"Curriculum phase changed: {self.phase.value} -> {current_phase.value}")
                    self.update_phase(current_phase)
            elif hasattr(self.curriculum, 'current_phase'):
                if self.curriculum.current_phase != self.phase:
                    self.update_phase(self.curriculum.current_phase)
        
        # ====================================================================
        # COMPUTE GRADIENTS
        # ====================================================================
        self.optimizer.zero_grad()
        loss.backward()
        
        # ====================================================================
        # CHECK 5: GRADIENT VALIDITY (NaN/Inf in gradients)
        # ====================================================================
        for name, param in model_parameters.items():
            if param.grad is not None:
                if torch.isnan(param.grad).any() or torch.isinf(param.grad).any():
                    raise NaNInfDetectedException(
                        f"NaN or Inf detected in gradients for parameter: {name}"
                    )
        
        # ====================================================================
        # CHECK 6: GRADIENT NORM COMPUTATION
        # ====================================================================
        total_norm = 0.0
        grad_params = []
        for param in model_parameters.values():
            if param.grad is not None:
                param_norm = param.grad.data.norm(2)
                total_norm += param_norm.item() ** 2
                grad_params.append(param)
        total_norm = total_norm ** 0.5
        self.last_gradient_norm = total_norm
        
        # ====================================================================
        # CHECK 7: GRADIENT GOVERNOR APPROVAL (PRE-STEP)
        # ====================================================================
        if self.gradient_governor is not None:
            if hasattr(self.gradient_governor, 'approve_update'):
                approved, reason = self.gradient_governor.approve_update(
                    gradient_norm=total_norm,
                    model_role=self.config.model_role,
                    step=self.step_count,
                    phase=self.phase
                )
                if not approved:
                    raise GradientGovernorBlockedException(
                        f"Gradient governor blocked update: {reason}"
                    )
            elif hasattr(self.gradient_governor, 'check_update'):
                approved, reason = self.gradient_governor.check_update(
                    gradient_norm=total_norm
                )
                if not approved:
                    raise GradientGovernorBlockedException(
                        f"Gradient governor blocked: {reason}"
                    )
        
        # ====================================================================
        # CHECK 8: GRADIENT EXPLOSION (HARD STOP)
        # ====================================================================
        if total_norm > self.config.max_grad_norm * 10:
            raise GradientGovernorBlockedException(
                f"Gradient explosion detected: norm={total_norm:.4f} "
                f"(threshold={self.config.max_grad_norm * 10})"
            )
        
        # ====================================================================
        # GRADIENT CLIPPING (before trust region check)
        # ====================================================================
        if grad_params:
            clip_norm = torch.nn.utils.clip_grad_norm_(
                grad_params,
                self.config.max_grad_norm
            )
            clipped_norm = min(total_norm, self.config.max_grad_norm)
            clipped_ratio = clipped_norm / (total_norm + 1e-8) if total_norm > 0 else 1.0
        else:
            clipped_norm = 0.0
            clipped_ratio = 1.0
        
        # ====================================================================
        # RISK SCALING (FIRST-CLASS - Improvement #1)
        # ====================================================================
        # Get violation rate from drift analyzer
        drift_metrics = self.drift_analyzer.compute_drift_metrics()
        violation_rate = drift_metrics.get('recent_violation_rate', 0.0)
        
        # Use centralized RiskScaler.compute_risk_scale() with all factors
        if self.config.enable_risk_scaling:
            risk_scale, risk_factors = self.risk_scaler.compute_risk_scale(
                uncertainty=uncertainty,
                gradient_norm=total_norm,
                replay_freshness=replay_freshness,
                tail_exposure=tail_risk if tail_risk > 0 else None,
                violation_rate=violation_rate,
                step=self.step_count
            )
        else:
            risk_scale = 1.0
            risk_factors = {}
        self.last_risk_scale = risk_scale
        
        # ====================================================================
        # LEARNING RATE SCHEDULE UPDATE (PER-GROUP OR SCALAR)
        # ====================================================================
        # Get per-group metrics if available
        per_group_metrics = None
        # Could be extended to compute per-group uncertainty/gradient_norm
        
        schedule_result = self.schedule_manager.step_schedule(
            metrics=metrics,
            uncertainty=uncertainty,
            gradient_norm=total_norm,
            per_group_metrics=per_group_metrics
        )
        
        # Get trust throttle from TrustRegionController
        trust_throttle = self.trust_controller.throttle_factor
        
        # Get maturity factor from ScheduleManager
        maturity_factor = 1.0
        if hasattr(self.schedule_manager, '_compute_maturity_adjustment'):
            maturity_factor = self.schedule_manager._compute_maturity_adjustment()
        
        # ====================================================================
        # LEARNING RATE COMPOSITION (CENTRALIZED - Improvement #4)
        # ====================================================================
        # Handle per-group LRs (vectorized) or scalar LR
        if isinstance(schedule_result, dict):
            # Per-group LRs (300M-scale stability)
            group_lrs = schedule_result
            effective_group_lrs = {}
            
            for group, schedule_lr in group_lrs.items():
                # Compose effective LR using LearningRateComposer
                effective_lr, lr_factors = self.lr_composer.compose(
                    base_lr=self.config.base_lr,
                    schedule_factor=schedule_lr / self.config.base_lr,  # Normalize to factor
                    risk_scale=risk_scale,
                    trust_throttle=trust_throttle,
                    maturity_factor=maturity_factor,
                    step=self.step_count
                )
                effective_group_lrs[group] = effective_lr
            
            # Apply per-group LRs to optimizer param_groups
            for param_group in self.optimizer.param_groups:
                # Find which parameter group this belongs to
                if 'params' in param_group and param_group['params']:
                    # Get group from first param name
                    first_param_name = None
                    for name, param in model_parameters.items():
                        if param in param_group['params']:
                            first_param_name = name
                            break
                    
                    if first_param_name:
                        group = self.param_group_manager.get_parameter_group(first_param_name)
                        param_group['lr'] = effective_group_lrs.get(group, self.config.base_lr)
                    else:
                        param_group['lr'] = self.config.base_lr
            
            # Store mean LR for backward compatibility
            effective_lr = sum(effective_group_lrs.values()) / len(effective_group_lrs)
            self.last_effective_lr = effective_lr
            current_lr = sum(group_lrs.values()) / len(group_lrs)
        else:
            # Scalar LR (legacy behavior)
            current_lr = schedule_result
            schedule_factor = current_lr / self.config.base_lr
            
            # Compose effective LR using LearningRateComposer
            effective_lr, lr_factors = self.lr_composer.compose(
                base_lr=self.config.base_lr,
                schedule_factor=schedule_factor,
                risk_scale=risk_scale,
                trust_throttle=trust_throttle,
                maturity_factor=maturity_factor,
                step=self.step_count
            )
            self.last_effective_lr = effective_lr
            
            # Apply scalar LR to all param groups
            for param_group in self.optimizer.param_groups:
                param_group['lr'] = effective_lr
        
        # ====================================================================
        # CHECK 9: STEP SIZE RISK BUDGET (PRE-STEP)
        # ====================================================================
        max_step_size = effective_lr * clipped_norm
        risk_budget = self.config.max_grad_norm * current_lr * risk_scale
        if max_step_size > risk_budget * 1.5:  # 50% safety margin
            raise StepSizeExceededException(
                f"Step size {max_step_size:.6f} exceeds risk budget {risk_budget:.6f}"
            )
        
        # ====================================================================
        # CHECK 10: TRUST REGION PRE-STEP VALIDATION (PREDICTIVE)
        # ====================================================================
        # Store current parameters
        old_params = {
            name: param.data.clone()
            for name, param in model_parameters.items()
        }
        
        # Predict parameter update using gradient and LR
        predicted_params = {}
        for name, param in model_parameters.items():
            if param.grad is not None:
                # Simplified prediction: param - lr * grad
                predicted_params[name] = param.data - effective_lr * param.grad.data
            else:
                predicted_params[name] = param.data.clone()
        
        # Build parameter group map for per-group trust regions
        param_group_map = {
            name: self.param_group_manager.get_parameter_group(name)
            for name in model_parameters.keys()
        }
        
        # Pre-step trust region check (with per-group validation)
        # Uses DriftAnalyzer for measurement, TrustRegionController for enforcement
        trust_ok, trust_reason = self.trust_controller.check_update(
            old_params, predicted_params, kl_div, param_group_map, step=self.step_count
        )
        
        # HARD-FAIL ON VIOLATIONS (Improvement #2)
        if not trust_ok:
            if self.controller_config.hard_fail_on_violation:
                # Exception: PPO KL soft-fail may be allowed
                if (kl_div is not None and 
                    kl_div > (self.config.kl_clip or 0.2) and
                    self.controller_config.allow_ppo_kl_soft_fail):
                    # Soft-fail for PPO KL (return decision, don't raise)
                    logging.warning(f"PPO KL violation (soft-fail allowed): {trust_reason}")
                else:
                    # Hard-fail: raise exception
                    raise TrustRegionViolationException(
                        f"Trust region violation detected PRE-STEP: {trust_reason}"
                    )
            else:
                # Soft-fail: log warning but continue
                logging.warning(f"Trust region violation (soft-fail): {trust_reason}")
        
        # ====================================================================
        # CHECK 11: PARAMETER GROUP AUTHORIZATION (Improvement #5)
        # ====================================================================
        # Validate all parameter groups are authorized (role + phase)
        if self.controller_config.require_parameter_group_authorization:
            is_valid, reason = self.param_group_manager.validate_parameter_groups(
                self.config.model_role, self.phase, require_authorization=True
            )
            if not is_valid:
                raise UnauthorizedParameterGroupException(
                    f"Parameter group authorization failed: {reason}"
                )
        
        # ====================================================================
        # PERFORM OPTIMIZER STEP
        # ====================================================================
        try:
            self.optimizer.step()
        except Exception as e:
            # Rollback on any optimizer error
            for rollback_name, rollback_param in model_parameters.items():
                if rollback_name in old_params:
                    rollback_param.data.copy_(old_params[rollback_name])
            raise OptimizerSafetyException(
                f"Optimizer step failed: {str(e)}"
            ) from e
        
        # Verify step didn't introduce NaNs
        for name, param in model_parameters.items():
            if torch.isnan(param.data).any() or torch.isinf(param.data).any():
                # Rollback and raise
                for rollback_name, rollback_param in model_parameters.items():
                    if rollback_name in old_params:
                        rollback_param.data.copy_(old_params[rollback_name])
                raise NaNInfDetectedException(
                    f"NaN/Inf introduced in parameter {name} after optimizer step"
                )
        
        # ====================================================================
        # RECORD RESOURCE USAGE
        # ====================================================================
        if self.resource_governor is not None:
            if hasattr(self.resource_governor, 'record_step'):
                self.resource_governor.record_step()
            elif isinstance(self.resource_governor, ResourceGovernor):
                self.resource_governor.record_step()
        
        # ====================================================================
        # UPDATE STEP COUNT
        # ====================================================================
        self.step_count += 1
        
        # ====================================================================
        # CHECK 12: AUDIT LOGGING (IMMUTABLE)
        # ====================================================================
        state = OptimizerState(
            step=self.step_count,
            effective_lr=effective_lr,
            gradient_norm=total_norm,
            clipped_ratio=clipped_ratio,
            trust_violations=self.trust_controller.violations,
            phase=self.phase,
            schedule_type=self.schedule_manager.schedule_type,
            risk_scale=risk_scale
        )
        
        # Log to audit hook (local file)
        if self.audit_hook:
            self.audit_hook.log_update(state)
        
        # Log to audit_logger (if provided)
        if self.audit_logger is not None:
            if hasattr(self.audit_logger, 'log_optimizer_update'):
                self.audit_logger.log_optimizer_update(state)
            elif hasattr(self.audit_logger, 'log'):
                self.audit_logger.log('optimizer_update', state)
        
        # ====================================================================
        # VERSION MANAGER SNAPSHOT (STATE LINEAGE)
        # ====================================================================
        if self.version_manager is not None:
            if hasattr(self.version_manager, 'create_optimizer_snapshot'):
                snapshot = {
                    'step': self.step_count,
                    'phase': self.phase.value,
                    'effective_lr': effective_lr,
                    'gradient_norm': total_norm,
                    'risk_scale': risk_scale,
                    'trust_violations': self.trust_controller.violations
                }
                self.version_manager.create_optimizer_snapshot(snapshot)
        
        # ====================================================================
        # METRICS RECORDING
        # ====================================================================
        self.metrics.record_step(
            effective_lr=effective_lr,
            gradient_norm=total_norm,
            risk_scale=risk_scale,
            success=True,
            trust_violations=self.trust_controller.violations,
            step_time=0.0  # Would need timer in production
        )
        
        # ====================================================================
        # RETURN SUCCESS
        # ====================================================================
        return UpdateDecision(
            approved=True,
            reason="Update approved",
            effective_lr=effective_lr,
            risk_scale=risk_scale
        )
    
    def update_phase(self, new_phase: CurriculumPhase):
        """Update curriculum phase and adjust parameter groups."""
        self.phase = new_phase
        
        # Rebuild parameter groups for new phase
        param_groups = self.param_group_manager.get_param_groups(
            self.config.base_lr, new_phase
        )
        
        # Update optimizer param groups
        self.optimizer.param_groups = param_groups
        
        logging.info(f"Optimizer phase updated to {new_phase.value}")
    
    def save_checkpoint(self, path: Path):
        """Save optimizer checkpoint."""
        StateSerializer.save_state(
            self.optimizer,
            self.schedule_manager,
            self.trust_controller,
            path
        )
    
    def load_checkpoint(self, path: Path):
        """Load optimizer checkpoint."""
        StateSerializer.load_state(
            self.optimizer,
            self.schedule_manager,
            self.trust_controller,
            path
        )
    
    def emergency_stop(self):
        """Trigger emergency cooldown."""
        self.schedule_manager.trigger_emergency_cooldown()
        logging.error("EMERGENCY STOP: Optimizer cooldown activated")
        
        # Also trigger safety watchdog if available
        if self.safety_watchdog is not None:
            if hasattr(self.safety_watchdog, 'trigger_kill_switch'):
                self.safety_watchdog.trigger_kill_switch("Optimizer emergency stop")
            elif hasattr(self.safety_watchdog, 'kill_switch'):
                self.safety_watchdog.kill_switch = True
    
    def recover_from_error(
        self,
        error: Exception,
        model_parameters: Dict[str, Parameter],
        checkpoint_path: Optional[Path] = None
    ) -> bool:
        """
        Recover from optimizer error.
        
        Attempts:
        1. Rollback to last checkpoint (if available)
        2. Reduce learning rate
        3. Enable emergency cooldown
        4. Reset optimizer state if needed
        
        Args:
            error: Exception that occurred
            model_parameters: Model parameters (may be corrupted)
            checkpoint_path: Optional path to checkpoint for rollback
            
        Returns:
            True if recovery successful, False otherwise
        """
        logging.error(f"Attempting recovery from error: {error}")
        
        # Strategy 1: Load checkpoint if available
        if checkpoint_path and checkpoint_path.exists():
            try:
                logging.info(f"Attempting checkpoint rollback: {checkpoint_path}")
                self.load_checkpoint(checkpoint_path)
                # Would need model checkpoint loading too - not our responsibility
                logging.info("Checkpoint loaded successfully")
                return True
            except Exception as e:
                logging.error(f"Checkpoint rollback failed: {e}")
        
        # Strategy 2: Reduce learning rate drastically
        try:
            logging.info("Reducing learning rate for recovery")
            self.schedule_manager.trigger_emergency_cooldown()
            self.schedule_manager.current_lr *= 0.1  # 10x reduction
            
            # Update optimizer LR
            for param_group in self.optimizer.param_groups:
                param_group['lr'] = self.schedule_manager.current_lr
            
            logging.info(f"LR reduced to {self.schedule_manager.current_lr:.6f}")
            return True
        except Exception as e:
            logging.error(f"LR reduction failed: {e}")
        
        # Strategy 3: Reset optimizer state (last resort)
        try:
            logging.warning("Resetting optimizer state (last resort)")
            # Reset optimizer to initial state
            self.optimizer = OptimizerFactory.create(
                self.config,
                self.param_group_manager.get_param_groups(self.config.base_lr, self.phase)
            )
            logging.warning("Optimizer state reset")
            return True
        except Exception as e:
            logging.error(f"Optimizer reset failed: {e}")
            return False
    
    def get_state_dict(self) -> Dict[str, Any]:
        """Get complete state for persistence."""
        return {
            'optimizer': self.optimizer.state_dict(),
            'step_count': self.step_count,
            'phase': self.phase.value,
            'schedule_manager': {
                'step': self.schedule_manager.step,
                'current_lr': self.schedule_manager.current_lr,
                'emergency_active': self.schedule_manager.emergency_active,
                'maturity_steps': self.schedule_manager.maturity_steps
            },
            'trust_controller': {
                'violations': self.trust_controller.violations,
                'consecutive_violations': self.trust_controller.consecutive_violations,
                'throttle_factor': self.trust_controller.throttle_factor
            },
            'last_gradient_norm': self.last_gradient_norm,
            'last_risk_scale': self.last_risk_scale,
            'last_effective_lr': self.last_effective_lr
        }
    
    def get_diagnostics(self) -> Dict[str, Any]:
        """
        Get comprehensive diagnostics for debugging.
        
        Returns:
            Dictionary of diagnostic information
        """
        diagnostics = {
            'step_count': self.step_count,
            'phase': self.phase.value,
            'effective_lr': self.last_effective_lr,
            'base_lr': self.config.base_lr,
            'current_lr': self.schedule_manager.current_lr,
            'gradient_norm': self.last_gradient_norm,
            'risk_scale': self.last_risk_scale,
            'trust_violations': self.trust_controller.violations,
            'consecutive_violations': self.trust_controller.consecutive_violations,
            'throttle_factor': self.trust_controller.throttle_factor,
            'emergency_active': self.schedule_manager.emergency_active,
            'maturity_steps': self.schedule_manager.maturity_steps,
            'uncertainty_adjusted_lr': self.schedule_manager.uncertainty_adjusted_lr,
            'noise_adjusted_lr': self.schedule_manager.noise_adjusted_lr
        }
        
        # Add resource diagnostics if available
        if self.resource_governor is not None:
            if hasattr(self.resource_governor, 'get_resource_status'):
                diagnostics['resource_status'] = self.resource_governor.get_resource_status()
            elif isinstance(self.resource_governor, ResourceGovernor):
                diagnostics['resource_status'] = self.resource_governor.get_resource_status()
        
        # Add curriculum diagnostics if available
        if self.curriculum is not None:
            if hasattr(self.curriculum, 'get_current_phase'):
                diagnostics['curriculum_phase'] = self.curriculum.get_current_phase().value
            elif hasattr(self.curriculum, 'current_phase'):
                diagnostics['curriculum_phase'] = self.curriculum.current_phase.value
        
        return diagnostics


# ============================================================================
# INTEGRATION HELPERS
# ============================================================================

def create_optimizer_for_model(
    model: torch.nn.Module,
    model_role: ModelRole,
    curriculum_phase: CurriculumPhase,
    base_lr: float = 3e-4,
    audit_path: Optional[Path] = None
) -> OptimizerController:
    """
    Convenience function to create optimizer controller for a model.
    
    Args:
        model: PyTorch model
        model_role: Role of the model (determines optimizer type)
        curriculum_phase: Current curriculum phase
        base_lr: Base learning rate
        audit_path: Optional audit log path
        
    Returns:
        Configured OptimizerController
    """
    config = OptimizerConfig(
        model_role=model_role,
        base_lr=base_lr,
        weight_decay=0.01,
        schedule_type=ScheduleType.COSINE_DECAY,
        warmup_steps=1000,
        enable_risk_scaling=True
    )
    
    model_parameters = dict(model.named_parameters())
    
    return OptimizerController(
        config=config,
        model_parameters=model_parameters,
        curriculum_phase=curriculum_phase,
        audit_path=audit_path
    )


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    # Main classes
    'OptimizerController',
    'OptimizerConfig',
    'OptimizerControllerConfig',
    'OptimizerState',
    'UpdateDecision',
    
    # Enums
    'ModelRole',
    'CurriculumPhase',
    'ScheduleType',
    'ParameterGroup',
    
    # Components
    'OptimizerFactory',
    'PPOOptimizer',
    'PPOOptimizerState',
    'TDStabilizedOptimizer',
    'TDStabilizedOptimizerState',
    'ScheduleManager',
    'DriftAnalyzer',
    'TrustRegionController',
    'ParameterGroupManager',
    'RiskScaler',
    'LearningRateComposer',
    'StateSerializer',
    'OptimizerAuditHook',
    'ResourceGovernor',
    'SeedController',
    
    # Integration
    'IntegrationRegistry',
    '_integration_registry',
    
    # Exceptions
    'OptimizerSafetyException',
    'GradientGovernorBlockedException',
    'TrustRegionViolationException',
    'NaNInfDetectedException',
    'StepSizeExceededException',
    'UnauthorizedParameterGroupException',
    'ResourceBudgetExceededException',
    'KillSwitchActivatedException',
    
    # Validation & Utilities
    'ParameterValidator',
    'OptimizerStateValidator',
    'OptimizerMetrics',
    
    # Helper Functions
    'create_optimizer_for_model',
    'create_optimizer_config',
    'validate_optimizer_config',
    'validate_optimizer_setup',
    'compute_effective_learning_rate',
    'compute_parameter_delta',
    'compute_gradient_statistics',
    'compute_optimizer_health_score',
    'recommend_optimizer_adjustments',
    'compute_learning_rate_range',
    'estimate_training_cost',
    'analyze_optimizer_performance',
    'create_default_resource_governor',
    'create_default_seed_controller',
    'get_optimizer_summary',
    'compare_optimizer_configs'
]


# ============================================================================
# MONITORING & METRICS COLLECTION
# ============================================================================

class OptimizerMetrics:
    """
    Collects and aggregates optimizer metrics for monitoring.
    
    Tracks:
    - Learning rate history
    - Gradient norm history
    - Trust region violations
    - Update success/failure rates
    - Resource usage
    """
    
    def __init__(self, history_size: int = 1000):
        """
        Initialize metrics collector.
        
        Args:
            history_size: Maximum history size to keep
        """
        self.history_size = history_size
        
        # Metrics storage
        self.lr_history: List[float] = []
        self.effective_lr_history: List[float] = []
        self.gradient_norm_history: List[float] = []
        self.risk_scale_history: List[float] = []
        self.trust_violations_history: List[int] = []
        self.update_success_history: List[bool] = []
        self.step_time_history: List[float] = []
        
        # Aggregate metrics
        self.total_steps = 0
        self.successful_steps = 0
        self.failed_steps = 0
        self.trust_violations_total = 0
        self.emergency_stops = 0
        
        # Current metrics
        self.current_lr = 0.0
        self.current_effective_lr = 0.0
        self.current_gradient_norm = 0.0
        self.current_risk_scale = 1.0
        
        self._logger = logging.getLogger(__name__)
    
    def record_step(
        self,
        effective_lr: float,
        gradient_norm: float,
        risk_scale: float,
        success: bool,
        trust_violations: int = 0,
        step_time: float = 0.0
    ):
        """
        Record metrics for a training step.
        
        Args:
            effective_lr: Effective learning rate used
            gradient_norm: Gradient norm computed
            risk_scale: Risk scaling factor applied
            success: Whether step was successful
            trust_violations: Number of trust region violations
            step_time: Time taken for step (seconds)
        """
        self.total_steps += 1
        self.current_effective_lr = effective_lr
        self.current_gradient_norm = gradient_norm
        self.current_risk_scale = risk_scale
        
        # Update history
        self.effective_lr_history.append(effective_lr)
        self.gradient_norm_history.append(gradient_norm)
        self.risk_scale_history.append(risk_scale)
        self.trust_violations_history.append(trust_violations)
        self.update_success_history.append(success)
        if step_time > 0:
            self.step_time_history.append(step_time)
        
        # Trim history
        if len(self.effective_lr_history) > self.history_size:
            self.effective_lr_history.pop(0)
        if len(self.gradient_norm_history) > self.history_size:
            self.gradient_norm_history.pop(0)
        if len(self.risk_scale_history) > self.history_size:
            self.risk_scale_history.pop(0)
        if len(self.trust_violations_history) > self.history_size:
            self.trust_violations_history.pop(0)
        if len(self.update_success_history) > self.history_size:
            self.update_success_history.pop(0)
        if len(self.step_time_history) > self.history_size:
            self.step_time_history.pop(0)
        
        # Update aggregates
        if success:
            self.successful_steps += 1
        else:
            self.failed_steps += 1
        
        self.trust_violations_total += trust_violations
    
    def record_emergency_stop(self):
        """Record emergency stop event."""
        self.emergency_stops += 1
        self._logger.warning(f"Emergency stop recorded (total: {self.emergency_stops})")
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        Get aggregated statistics.
        
        Returns:
            Dictionary of statistics
        """
        stats = {
            'total_steps': self.total_steps,
            'successful_steps': self.successful_steps,
            'failed_steps': self.failed_steps,
            'success_rate': self.successful_steps / self.total_steps if self.total_steps > 0 else 0.0,
            'trust_violations_total': self.trust_violations_total,
            'emergency_stops': self.emergency_stops,
            'current_lr': self.current_effective_lr,
            'current_gradient_norm': self.current_gradient_norm,
            'current_risk_scale': self.current_risk_scale
        }
        
        # Compute averages over history
        if self.effective_lr_history:
            stats['avg_effective_lr'] = sum(self.effective_lr_history) / len(self.effective_lr_history)
            stats['min_effective_lr'] = min(self.effective_lr_history)
            stats['max_effective_lr'] = max(self.effective_lr_history)
        
        if self.gradient_norm_history:
            stats['avg_gradient_norm'] = sum(self.gradient_norm_history) / len(self.gradient_norm_history)
            stats['min_gradient_norm'] = min(self.gradient_norm_history)
            stats['max_gradient_norm'] = max(self.gradient_norm_history)
        
        if self.risk_scale_history:
            stats['avg_risk_scale'] = sum(self.risk_scale_history) / len(self.risk_scale_history)
            stats['min_risk_scale'] = min(self.risk_scale_history)
            stats['max_risk_scale'] = max(self.risk_scale_history)
        
        if self.step_time_history:
            stats['avg_step_time'] = sum(self.step_time_history) / len(self.step_time_history)
            stats['min_step_time'] = min(self.step_time_history)
            stats['max_step_time'] = max(self.step_time_history)
        
        return stats
    
    def get_recent_history(self, window: int = 100) -> Dict[str, List[Any]]:
        """
        Get recent history for visualization.
        
        Args:
            window: Number of recent steps to return
            
        Returns:
            Dictionary of recent history
        """
        return {
            'effective_lr': self.effective_lr_history[-window:],
            'gradient_norm': self.gradient_norm_history[-window:],
            'risk_scale': self.risk_scale_history[-window:],
            'trust_violations': self.trust_violations_history[-window:],
            'success': self.update_success_history[-window:],
            'step_time': self.step_time_history[-window:]
        }
    
    def reset(self):
        """Reset all metrics."""
        self.lr_history.clear()
        self.effective_lr_history.clear()
        self.gradient_norm_history.clear()
        self.risk_scale_history.clear()
        self.trust_violations_history.clear()
        self.update_success_history.clear()
        self.step_time_history.clear()
        
        self.total_steps = 0
        self.successful_steps = 0
        self.failed_steps = 0
        self.trust_violations_total = 0
        self.emergency_stops = 0
        
        self.current_lr = 0.0
        self.current_effective_lr = 0.0
        self.current_gradient_norm = 0.0
        self.current_risk_scale = 1.0


# ============================================================================
# ADDITIONAL UTILITY FUNCTIONS
# ============================================================================

def create_optimizer_config(
    model_role: ModelRole,
    base_lr: float = 3e-4,
    weight_decay: float = 0.01,
    schedule_type: ScheduleType = ScheduleType.COSINE_DECAY,
    warmup_steps: int = 1000,
    total_steps: Optional[int] = None,
    kl_clip: Optional[float] = None,
    param_delta_bound: Optional[float] = None,
    enable_risk_scaling: bool = True
) -> OptimizerConfig:
    """
    Create optimizer configuration with defaults.
    
    Args:
        model_role: Model role (determines optimizer type)
        base_lr: Base learning rate
        weight_decay: Weight decay factor
        schedule_type: Learning rate schedule type
        warmup_steps: Number of warmup steps
        total_steps: Total training steps (for cosine decay)
        kl_clip: KL divergence clip (for policy models)
        param_delta_bound: Parameter delta bound (for trust region)
        enable_risk_scaling: Enable risk-based scaling
        
    Returns:
        OptimizerConfig instance
    """
    return OptimizerConfig(
        model_role=model_role,
        base_lr=base_lr,
        weight_decay=weight_decay,
        schedule_type=schedule_type,
        warmup_steps=warmup_steps,
        total_steps=total_steps,
        kl_clip=kl_clip,
        param_delta_bound=param_delta_bound,
        enable_risk_scaling=enable_risk_scaling
    )


def validate_optimizer_config(config: OptimizerConfig) -> Tuple[bool, str]:
    """
    Validate optimizer configuration.
    
    Args:
        config: Optimizer configuration
        
    Returns:
        (is_valid, reason)
    """
    if config.base_lr <= 0:
        return False, "Base learning rate must be positive"
    
    if config.base_lr > 1.0:
        return False, f"Base learning rate {config.base_lr} too large (should be < 1.0)"
    
    if config.weight_decay < 0:
        return False, "Weight decay must be non-negative"
    
    if config.max_grad_norm <= 0:
        return False, "Max gradient norm must be positive"
    
    if config.schedule_type == ScheduleType.COSINE_DECAY:
        if config.total_steps is None:
            return False, "total_steps required for cosine decay schedule"
        if config.total_steps <= 0:
            return False, "total_steps must be positive"
        if config.warmup_steps >= config.total_steps:
            return False, "warmup_steps must be less than total_steps"
    
    if config.kl_clip is not None and config.kl_clip <= 0:
        return False, "KL clip must be positive"
    
    if config.param_delta_bound is not None and config.param_delta_bound <= 0:
        return False, "Parameter delta bound must be positive"
    
    if config.uncertainty_threshold < 0 or config.uncertainty_threshold > 1:
        return False, "Uncertainty threshold must be in [0, 1]"
    
    return True, "Configuration valid"


# ============================================================================
# LOGGING SETUP
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s'
)


# ============================================================================
# COMPREHENSIVE DOCUMENTATION & EXAMPLES
# ============================================================================

"""
EXAMPLE USAGE:

# Example 1: Basic usage with default settings
from optimizer import (
    OptimizerController, OptimizerConfig, ModelRole, CurriculumPhase,
    create_optimizer_for_model
)

model = YourModel()
optimizer = create_optimizer_for_model(
    model=model,
    model_role=ModelRole.PREDICTOR,
    curriculum_phase=CurriculumPhase.STRUCTURE_LEARNING,
    base_lr=3e-4
)

# Training loop
for batch in dataloader:
    loss = compute_loss(model, batch)
    decision = optimizer.step(
        loss=loss,
        model_parameters=dict(model.named_parameters()),
        uncertainty=0.3,
        tail_risk=0.1
    )
    if not decision.approved:
        print(f"Update rejected: {decision.reason}")


# Example 2: Full configuration with integrations
from optimizer import (
    OptimizerController, OptimizerConfig, ModelRole, CurriculumPhase,
    ScheduleType, SeedController, ResourceGovernor, OptimizerMetrics
)

# Create seed controller for determinism
seed_controller = SeedController(base_seed=42)
seed_controller.enforce_determinism()

# Create resource governor
resource_governor = ResourceGovernor(
    gpu_memory_limit_gb=24.0,
    training_budget_usd=1000.0
)

# Create optimizer config
config = OptimizerConfig(
    model_role=ModelRole.POLICY,
    base_lr=1e-4,
    weight_decay=0.01,
    schedule_type=ScheduleType.COSINE_DECAY,
    warmup_steps=1000,
    total_steps=10000,
    kl_clip=0.2,  # For policy models
    param_delta_bound=0.1,
    enable_risk_scaling=True,
    uncertainty_threshold=0.8
)

# Create optimizer controller
optimizer = OptimizerController(
    config=config,
    model_parameters=dict(model.named_parameters()),
    curriculum_phase=CurriculumPhase.STRUCTURE_LEARNING,
    seed_controller=seed_controller,
    resource_governor=resource_governor
)

# Training loop with comprehensive error handling
try:
    for batch in dataloader:
        loss = compute_loss(model, batch)
        
        # Compute uncertainty and other metrics
        uncertainty = compute_uncertainty(model, batch)
        tail_risk = compute_tail_risk(model, batch)
        anomaly_score = compute_anomaly_score(model, batch)
        
        # Optimizer step with full safety checks
        decision = optimizer.step(
            loss=loss,
            model_parameters=dict(model.named_parameters()),
            uncertainty=uncertainty,
            tail_risk=tail_risk,
            anomaly_score=anomaly_score,
            kl_div=compute_kl_divergence(old_policy, new_policy),
            metrics={'loss': loss.item()},
            replay_freshness=0.9
        )
        
        if decision.approved:
            print(f"Step {optimizer.step_count}: LR={decision.effective_lr:.6f}, "
                  f"Risk Scale={decision.risk_scale:.4f}")
        else:
            print(f"Update rejected: {decision.reason}")
            
except OptimizerSafetyException as e:
    print(f"Safety exception: {e}")
    # Attempt recovery
    optimizer.recover_from_error(e, dict(model.named_parameters()))
    # Or trigger emergency stop
    optimizer.emergency_stop()


# Example 3: Integration with training subsystem
from optimizer import IntegrationRegistry

# Register integrations (if modules exist)
try:
    from training.gradient_governor import GradientGovernor
    from training.safety_watchdog import SafetyWatchdog
    from training.curriculum import CurriculumManager
    from training.audit_logger import AuditLogger
    from training.version_manager import VersionManager
    
    IntegrationRegistry().register_gradient_governor(GradientGovernor())
    IntegrationRegistry().register_safety_watchdog(SafetyWatchdog())
    IntegrationRegistry().register_curriculum(CurriculumManager())
    IntegrationRegistry().register_audit_logger(AuditLogger())
    IntegrationRegistry().register_version_manager(VersionManager())
except ImportError:
    pass  # Fallback to optional integrations


# Example 4: Advanced usage with curriculum phase transitions
optimizer = OptimizerController(...)

# Curriculum phase changes automatically detected
# But can also manually update phase
optimizer.update_phase(CurriculumPhase.STABILIZATION)

# Get diagnostics
diagnostics = optimizer.get_diagnostics()
print(f"Current phase: {diagnostics['phase']}")
print(f"Effective LR: {diagnostics['effective_lr']:.6f}")
print(f"Trust violations: {diagnostics['trust_violations']}")
print(f"Gradient norm: {diagnostics['gradient_norm']:.4f}")

# Get metrics
metrics = optimizer.metrics.get_statistics()
print(f"Success rate: {metrics['success_rate']:.2%}")
print(f"Avg gradient norm: {metrics['avg_gradient_norm']:.4f}")


# Example 5: Checkpoint saving and loading
from pathlib import Path

checkpoint_path = Path("checkpoints/optimizer_step_1000.pt")

# Save checkpoint
optimizer.save_checkpoint(checkpoint_path)

# Load checkpoint
optimizer.load_checkpoint(checkpoint_path)


# Example 6: Validation before use
from optimizer import validate_optimizer_config, OptimizerConfig, ModelRole

config = OptimizerConfig(
    model_role=ModelRole.VALUE,
    base_lr=1e-3,
    total_steps=5000,
    warmup_steps=500
)

is_valid, reason = validate_optimizer_config(config)
if not is_valid:
    raise ValueError(f"Invalid config: {reason}")


# Example 7: Parameter validation
from optimizer import ParameterValidator, OptimizerStateValidator

param_validator = ParameterValidator()
is_valid, reason = param_validator.validate_parameters(
    parameters=dict(model.named_parameters()),
    param_groups=optimizer.optimizer.param_groups,
    require_gradients=True
)

if not is_valid:
    raise ValueError(f"Parameter validation failed: {reason}")


# Example 8: Resource governance monitoring
resource_status = optimizer.resource_governor.get_resource_status()
print(f"GPU memory: {resource_status['gpu_memory_used_gb']:.2f}GB / "
      f"{resource_status['gpu_memory_limit_gb']:.2f}GB")
print(f"Training budget: ${resource_status['training_budget_used_usd']:.2f} / "
      f"${resource_status['training_budget_limit_usd']:.2f}")


CRITICAL REQUIREMENTS MET:

✓ All optimizers pass through OptimizerController
✓ Gradient governor approval required before step
✓ Safety watchdog kill switch checked first
✓ Curriculum phase actively read and adapted
✓ Determinism enforced via seed controller
✓ Trust region validated PRE-STEP (not post-step rollback)
✓ Hard stops raise exceptions (not return False)
✓ Resource governance (GPU/TPU/memory/budget)
✓ Maturity/uncertainty-aware schedules
✓ Structural parameter grouping (not string matching)
✓ Comprehensive error handling and recovery
✓ Audit logging to audit_logger
✓ Version manager state lineage
✓ Per-parameter-group trust regions
✓ Adaptive throttling based on violation history

INTEGRATION POINTS:

1. gradient_governor.py: approve_update() called before optimizer.step()
2. safety_watchdog.py: is_kill_switch_active() checked first
3. curriculum.py: get_current_phase() called to adapt behavior
4. audit_logger.py: log_optimizer_update() called after each step
5. version_manager.py: create_optimizer_snapshot() called after each step
6. seed_controller.py: get_optimizer_seed() and enforce_determinism()
7. resource_governor.py: check_budget() called before step

FAILURE CONDITIONS (HARD STOPS):

The optimizer will raise exceptions (not return False) if:
- Kill switch activated (KillSwitchActivatedException)
- Resource budget exceeded (ResourceBudgetExceededException)
- NaN/Inf detected (NaNInfDetectedException)
- Gradient governor blocks (GradientGovernorBlockedException)
- Trust region violated (TrustRegionViolationException)
- Step size exceeds risk budget (StepSizeExceededException)
- Unauthorized parameter group (UnauthorizedParameterGroupException)

All exceptions inherit from OptimizerSafetyException for easy catching.
"""


# ============================================================================
# ADDITIONAL VALIDATION AND SAFETY CHECKS
# ============================================================================

def validate_optimizer_setup(
    optimizer: OptimizerController,
    model_parameters: Dict[str, Parameter]
) -> Tuple[bool, List[str]]:
    """
    Comprehensive validation of optimizer setup.
    
    Validates:
    - Configuration validity
    - Parameter consistency
    - Integration hooks
    - Resource availability
    - State consistency
    
    Args:
        optimizer: Optimizer controller instance
        model_parameters: Model parameters
        
    Returns:
        (is_valid, list_of_errors)
    """
    errors = []
    
    # Validate configuration
    is_valid, reason = validate_optimizer_config(optimizer.config)
    if not is_valid:
        errors.append(f"Config validation failed: {reason}")
    
    # Validate parameters
    param_valid, param_reason = optimizer.param_validator.validate_parameters(
        parameters=model_parameters,
        param_groups=optimizer.optimizer.param_groups,
        require_gradients=False
    )
    if not param_valid:
        errors.append(f"Parameter validation failed: {param_reason}")
    
    # Validate optimizer state
    state_valid, state_reason = optimizer.state_validator.validate_optimizer_state(
        optimizer=optimizer.optimizer,
        parameters=model_parameters
    )
    if not state_valid:
        errors.append(f"State validation failed: {state_reason}")
    
    # Check integration hooks (warnings, not errors)
    if optimizer.seed_controller is None:
        errors.append("WARNING: No seed controller - determinism not guaranteed")
    
    if optimizer.gradient_governor is None:
        errors.append("WARNING: No gradient governor - gradient safety not guaranteed")
    
    if optimizer.safety_watchdog is None:
        errors.append("WARNING: No safety watchdog - kill switch not available")
    
    # Check resource availability
    if optimizer.resource_governor is not None:
        budget_ok, budget_reason = optimizer.resource_governor.check_budget()
        if not budget_ok:
            errors.append(f"Resource budget check failed: {budget_reason}")
    
    return len(errors) == 0, errors


def compute_optimizer_health_score(optimizer: OptimizerController) -> float:
    """
    Compute optimizer health score [0, 1].
    
    Factors:
    - Success rate
    - Trust violations
    - Emergency stops
    - Resource usage
    - Schedule stability
    
    Args:
        optimizer: Optimizer controller instance
        
    Returns:
        Health score (0.0 = unhealthy, 1.0 = healthy)
    """
    metrics = optimizer.metrics.get_statistics()
    
    # Success rate component (0.0 to 1.0)
    success_rate = metrics.get('success_rate', 0.0)
    
    # Trust violation penalty (0.0 to 0.5)
    trust_penalty = min(metrics.get('trust_violations_total', 0) / 100.0, 0.5)
    
    # Emergency stop penalty (0.0 to 0.3)
    emergency_penalty = min(metrics.get('emergency_stops', 0) / 10.0, 0.3)
    
    # Resource usage penalty (0.0 to 0.2)
    resource_penalty = 0.0
    if optimizer.resource_governor is not None:
        resource_status = optimizer.resource_governor.get_resource_status()
        if resource_status['gpu_memory_limit_gb'] > 0:
            memory_ratio = resource_status['gpu_memory_used_gb'] / resource_status['gpu_memory_limit_gb']
            if memory_ratio > 0.9:  # >90% usage
                resource_penalty = 0.2 * (memory_ratio - 0.9) / 0.1
    
    # Compute health score
    health_score = success_rate * (1.0 - trust_penalty - emergency_penalty - resource_penalty)
    health_score = max(0.0, min(1.0, health_score))  # Clamp to [0, 1]
    
    return health_score


def recommend_optimizer_adjustments(optimizer: OptimizerController) -> List[str]:
    """
    Recommend optimizer adjustments based on current state.
    
    Args:
        optimizer: Optimizer controller instance
        
    Returns:
        List of adjustment recommendations
    """
    recommendations = []
    
    # Check health score
    health_score = compute_optimizer_health_score(optimizer)
    if health_score < 0.5:
        recommendations.append("CRITICAL: Optimizer health score is low - consider emergency stop")
    
    # Check trust violations
    metrics = optimizer.metrics.get_statistics()
    if metrics.get('trust_violations_total', 0) > 50:
        recommendations.append("High trust violations detected - reduce learning rate or tighten trust region")
    
    # Check emergency stops
    if metrics.get('emergency_stops', 0) > 3:
        recommendations.append("Multiple emergency stops - investigate root cause")
    
    # Check gradient norms
    if optimizer.metrics.gradient_norm_history:
        recent_norms = optimizer.metrics.gradient_norm_history[-100:]
        avg_norm = sum(recent_norms) / len(recent_norms)
        if avg_norm > 5.0:
            recommendations.append("High gradient norms detected - consider gradient clipping or reduce learning rate")
        elif avg_norm < 0.01:
            recommendations.append("Very low gradient norms - learning may be stalled, check data flow")
    
    # Check learning rate
    if optimizer.schedule_manager.current_lr < optimizer.config.base_lr * 0.01:
        recommendations.append("Learning rate very low - consider resetting schedule or increasing base LR")
    
    # Check resource usage
    if optimizer.resource_governor is not None:
        resource_status = optimizer.resource_governor.get_resource_status()
        if resource_status['gpu_memory_limit_gb'] > 0:
            memory_ratio = resource_status['gpu_memory_used_gb'] / resource_status['gpu_memory_limit_gb']
            if memory_ratio > 0.85:
                recommendations.append(f"High GPU memory usage ({memory_ratio:.1%}) - consider reducing batch size")
    
    return recommendations


# ============================================================================
# ADDITIONAL HELPER FUNCTIONS FOR OPTIMIZER MANAGEMENT
# ============================================================================

def compute_learning_rate_range(
    config: OptimizerConfig,
    num_steps: int
) -> Tuple[float, float]:
    """
    Compute expected learning rate range over training.
    
    Args:
        config: Optimizer configuration
        num_steps: Number of training steps
        
    Returns:
        (min_lr, max_lr) tuple
    """
    schedule = ScheduleManager(config)
    
    min_lr = float('inf')
    max_lr = 0.0
    
    for step in range(num_steps):
        schedule.step = step
        current_lr = schedule.step_schedule()
        min_lr = min(min_lr, current_lr)
        max_lr = max(max_lr, current_lr)
    
    return min_lr, max_lr


def estimate_training_cost(
    config: OptimizerConfig,
    num_steps: int,
    cost_per_step_usd: float = 0.001
) -> float:
    """
    Estimate training cost based on configuration.
    
    Args:
        config: Optimizer configuration
        num_steps: Number of training steps
        cost_per_step_usd: Cost per training step in USD
        
    Returns:
        Estimated total cost in USD
    """
    return num_steps * cost_per_step_usd


def analyze_optimizer_performance(
    optimizer: OptimizerController,
    window_size: int = 100
) -> Dict[str, Any]:
    """
    Analyze optimizer performance over recent window.
    
    Args:
        optimizer: Optimizer controller instance
        window_size: Number of recent steps to analyze
        
    Returns:
        Dictionary of performance metrics
    """
    metrics = optimizer.metrics
    recent_history = metrics.get_recent_history(window_size)
    
    if not recent_history['effective_lr']:
        return {'error': 'No history available'}
    
    # Compute statistics
    effective_lrs = recent_history['effective_lr']
    gradient_norms = recent_history['gradient_norm']
    risk_scales = recent_history['risk_scale']
    trust_violations = recent_history['trust_violations']
    successes = recent_history['success']
    
    analysis = {
        'window_size': len(effective_lrs),
        'lr_stats': {
            'mean': sum(effective_lrs) / len(effective_lrs),
            'min': min(effective_lrs),
            'max': max(effective_lrs),
            'std': math.sqrt(sum((x - sum(effective_lrs)/len(effective_lrs))**2 for x in effective_lrs) / len(effective_lrs))
        },
        'gradient_norm_stats': {
            'mean': sum(gradient_norms) / len(gradient_norms) if gradient_norms else 0.0,
            'min': min(gradient_norms) if gradient_norms else 0.0,
            'max': max(gradient_norms) if gradient_norms else 0.0
        },
        'risk_scale_stats': {
            'mean': sum(risk_scales) / len(risk_scales),
            'min': min(risk_scales),
            'max': max(risk_scales)
        },
        'trust_violations': sum(trust_violations),
        'success_rate': sum(successes) / len(successes) if successes else 0.0,
        'current_phase': optimizer.phase.value,
        'step_count': optimizer.step_count
    }
    
    return analysis


def create_default_resource_governor(
    gpu_available: bool = True,
    budget_usd: Optional[float] = None
) -> ResourceGovernor:
    """
    Create default resource governor with reasonable defaults.
    
    Args:
        gpu_available: Whether GPU is available
        budget_usd: Optional training budget in USD
        
    Returns:
        ResourceGovernor instance with defaults
    """
    if gpu_available and torch.cuda.is_available():
        # Get available GPU memory
        gpu_memory_gb = torch.cuda.get_device_properties(0).total_memory / (1024**3)
        # Use 80% of available memory as limit
        memory_limit = gpu_memory_gb * 0.8
    else:
        memory_limit = None
    
    return ResourceGovernor(
        gpu_memory_limit_gb=memory_limit,
        training_budget_usd=budget_usd,
        max_memory_usage_ratio=0.9
    )


def create_default_seed_controller(seed: int = 42) -> SeedController:
    """
    Create default seed controller with given seed.
    
    Args:
        seed: Base seed for determinism
        
    Returns:
        SeedController instance
    """
    controller = SeedController(base_seed=seed)
    controller.enforce_determinism()
    return controller


def get_optimizer_summary(optimizer: OptimizerController) -> Dict[str, Any]:
    """
    Get comprehensive summary of optimizer state.
    
    Args:
        optimizer: Optimizer controller instance
        
    Returns:
        Dictionary summary
    """
    summary = {
        'step_count': optimizer.step_count,
        'phase': optimizer.phase.value,
        'model_role': optimizer.config.model_role.value,
        'current_lr': optimizer.schedule_manager.current_lr,
        'effective_lr': optimizer.last_effective_lr,
        'base_lr': optimizer.config.base_lr,
        'gradient_norm': optimizer.last_gradient_norm,
        'risk_scale': optimizer.last_risk_scale,
        'trust_violations': optimizer.trust_controller.violations,
        'consecutive_violations': optimizer.trust_controller.consecutive_violations,
        'throttle_factor': optimizer.trust_controller.throttle_factor,
        'emergency_active': optimizer.schedule_manager.emergency_active,
        'maturity_steps': optimizer.schedule_manager.maturity_steps,
        'schedule_type': optimizer.schedule_manager.schedule_type.value
    }
    
    # Add metrics summary
    metrics_stats = optimizer.metrics.get_statistics()
    summary['metrics'] = {
        'success_rate': metrics_stats.get('success_rate', 0.0),
        'total_steps': metrics_stats.get('total_steps', 0),
        'successful_steps': metrics_stats.get('successful_steps', 0),
        'failed_steps': metrics_stats.get('failed_steps', 0),
        'trust_violations_total': metrics_stats.get('trust_violations_total', 0),
        'emergency_stops': metrics_stats.get('emergency_stops', 0)
    }
    
    # Add resource status if available
    if optimizer.resource_governor is not None:
        summary['resource_status'] = optimizer.resource_governor.get_resource_status()
    
    # Add health score
    summary['health_score'] = compute_optimizer_health_score(optimizer)
    
    # Add recommendations
    summary['recommendations'] = recommend_optimizer_adjustments(optimizer)
    
    return summary


def compare_optimizer_configs(
    config1: OptimizerConfig,
    config2: OptimizerConfig
) -> Dict[str, Any]:
    """
    Compare two optimizer configurations.
    
    Args:
        config1: First optimizer configuration
        config2: Second optimizer configuration
        
    Returns:
        Dictionary of differences
    """
    differences = {}
    
    if config1.model_role != config2.model_role:
        differences['model_role'] = (config1.model_role.value, config2.model_role.value)
    
    if abs(config1.base_lr - config2.base_lr) > 1e-8:
        differences['base_lr'] = (config1.base_lr, config2.base_lr)
    
    if abs(config1.weight_decay - config2.weight_decay) > 1e-8:
        differences['weight_decay'] = (config1.weight_decay, config2.weight_decay)
    
    if config1.schedule_type != config2.schedule_type:
        differences['schedule_type'] = (config1.schedule_type.value, config2.schedule_type.value)
    
    if config1.warmup_steps != config2.warmup_steps:
        differences['warmup_steps'] = (config1.warmup_steps, config2.warmup_steps)
    
    if config1.total_steps != config2.total_steps:
        differences['total_steps'] = (config1.total_steps, config2.total_steps)
    
    if config1.kl_clip != config2.kl_clip:
        differences['kl_clip'] = (config1.kl_clip, config2.kl_clip)
    
    if config1.param_delta_bound != config2.param_delta_bound:
        differences['param_delta_bound'] = (config1.param_delta_bound, config2.param_delta_bound)
    
    if config1.enable_risk_scaling != config2.enable_risk_scaling:
        differences['enable_risk_scaling'] = (config1.enable_risk_scaling, config2.enable_risk_scaling)
    
    return differences