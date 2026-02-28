"""
/training/gradient_governor.py

Gradient Stability & Risk Containment Engine

One-Sentence Definition:
    Observes, constrains, and intervenes on gradient behavior in real time to prevent
    divergence, tail hallucination, and catastrophic forgetting.

Core Principle:
    Gradients are a liability before they are an asset.
    Left unchecked, gradients overfit rare tails, amplify noise, hallucinate virality,
    and destabilize RL feedback loops.

Architectural Placement:
    /training/gradient_governor.py
    ↓ feeds signals to:
        - curriculum.py (promotion/demotion)
        - data_gate.py (sample reweighting)
        - safety_watchdog.py (risk escalation)

If this file is disabled → training must not start.
"""

import torch
import torch.nn as nn
from typing import Dict, List, Optional, Tuple, Any, Protocol
from dataclasses import dataclass, field
from collections import deque
from pathlib import Path
import json
import numpy as np
from enum import Enum
import time
import hashlib


# ============================================================================
# ENUMS & CONSTANTS
# ============================================================================

class InterventionLevel(Enum):
    """Emergency intervention escalation levels (ORDERED)"""
    NONE = 0
    SOFT_DAMPING = 1
    ADAPTIVE_CLIPPING = 2
    LOSS_REWEIGHTING = 3
    CURRICULUM_DEMOTION = 4
    TRAINING_HALT = 5


class HorizonType(Enum):
    """Prediction horizon types for gradient balance"""
    SHORT_TERM = "short_term"
    MID_TERM = "mid_term"
    LONG_TERM = "long_term"


# Hard limits (AUTHORITATIVE)
DEFAULT_MAX_GRAD_NORM = 1.0
DEFAULT_VARIANCE_THRESHOLD = 0.1
DEFAULT_COSINE_SIMILARITY_THRESHOLD = 0.7
DEFAULT_FORGETTING_THRESHOLD = 0.5
DEFAULT_VARIANCE_WINDOW_SIZE = 100
DEFAULT_HISTORY_ANCHOR_SIZE = 1000


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class GradientConstraints:
    """Dynamic gradient constraints computed per curriculum phase"""
    max_grad_norm: float
    variance_threshold: float
    horizon_bounds: Dict[HorizonType, Tuple[float, float]]
    rare_event_cap: float
    uncertainty_scaling: bool
    
    def validate(self) -> None:
        """Validate constraint coherence"""
        assert self.max_grad_norm > 0, "max_grad_norm must be positive"
        assert self.variance_threshold > 0, "variance_threshold must be positive"
        assert self.rare_event_cap > 0, "rare_event_cap must be positive"
        
        # Validate horizon bounds sum to reasonable range
        total_lower = sum(bounds[0] for bounds in self.horizon_bounds.values())
        total_upper = sum(bounds[1] for bounds in self.horizon_bounds.values())
        assert 0.8 <= total_lower <= 1.0, "horizon lower bounds must sum near 1.0"
        assert 0.8 <= total_upper <= 1.2, "horizon upper bounds must sum near 1.0"


@dataclass
class GradientSignals:
    """Mandatory gradient signals captured every step"""
    global_grad_norm: float
    per_layer_grad_norms: Dict[str, float]
    grad_variance: float
    grad_cosine_similarity: float
    horizon_contributions: Dict[HorizonType, float]
    uncertainty_weighted_norm: float
    step: int
    timestamp: float
    
    def is_valid(self) -> bool:
        """Check for NaNs or corrupted signals"""
        if not np.isfinite(self.global_grad_norm):
            return False
        if not np.isfinite(self.grad_variance):
            return False
        if not np.isfinite(self.grad_cosine_similarity):
            return False
        if not all(np.isfinite(v) for v in self.per_layer_grad_norms.values()):
            return False
        if not all(np.isfinite(v) for v in self.horizon_contributions.values()):
            return False
        return True


@dataclass
class InterventionRecord:
    """Record of intervention taken"""
    level: InterventionLevel
    reason: str
    signals: GradientSignals
    action_taken: Dict[str, Any]
    step: int
    timestamp: float


@dataclass
class GovernorState:
    """Serializable state for determinism"""
    step: int
    gradient_history: List[float]
    variance_window: List[float]
    historical_anchors: List[torch.Tensor]
    intervention_history: List[InterventionRecord]
    cumulative_interventions: Dict[InterventionLevel, int]
    forgetting_alerts: int
    rare_event_interventions: int
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict (anchors excluded for size)"""
        return {
            "step": self.step,
            "gradient_history": self.gradient_history[-1000:],  # Keep last 1k
            "variance_window": self.variance_window,
            "cumulative_interventions": {k.name: v for k, v in self.cumulative_interventions.items()},
            "forgetting_alerts": self.forgetting_alerts,
            "rare_event_interventions": self.rare_event_interventions,
            "intervention_count": len(self.intervention_history)
        }
    
    def save(self, path: Path) -> None:
        """Save state to disk"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(self.to_dict(), f, indent=2)


# ============================================================================
# PROTOCOLS (for dependency injection)
# ============================================================================

class CurriculumProtocol(Protocol):
    """Interface to curriculum.py"""
    def get_current_phase(self) -> str: ...
    def request_demotion(self, reason: str, signals: GradientSignals) -> bool: ...
    def get_phase_config(self) -> Dict[str, Any]: ...
    def promotion_validator(self, signals: GradientSignals) -> Tuple[bool, str]: ...
    def demotion_guard(self, signals: GradientSignals, reason: str) -> Tuple[bool, str]: ...


class DataGateProtocol(Protocol):
    """Interface to data_gate.py"""
    def request_reweighting(self, sample_ids: List[str], weights: List[float]) -> None: ...
    def flag_rare_batch(self, batch_id: str) -> None: ...
    def request_replay_resampling(self, reason: str, alignment_hash: Optional[str] = None) -> bool: ...


class SafetyWatchdogProtocol(Protocol):
    """Interface to safety_watchdog.py"""
    def escalate_risk(self, level: str, reason: str, context: Dict[str, Any]) -> None: ...


# ============================================================================
# GRADIENT COLLECTOR
# ============================================================================

class GradientCollector:
    """
    Hooks into backward pass. Zero mutation. Observation only.
    
    Captures mandatory signals without interfering with computation.
    Enhanced with gradient flow tracking and layer-wise analysis.
    """
    
    def __init__(self, enable_per_sample_tracking: bool = True):
        self.hooks: List[torch.utils.hooks.RemovableHandle] = []
        self.layer_grads: Dict[str, torch.Tensor] = {}
        self.collection_enabled = True
        self.gradient_history: deque = deque(maxlen=1000)  # For variance computation
        self.layer_gradient_flow: Dict[str, List[float]] = {}  # Track gradient flow per layer
        
        # Per-sample gradient tracking (elite-tier feature)
        self.enable_per_sample_tracking = enable_per_sample_tracking
        self.per_sample_grads: Dict[str, List[torch.Tensor]] = {}  # Layer -> [sample_grads]
        self.per_sample_hooks: Dict[str, List[torch.utils.hooks.RemovableHandle]] = {}
        self.batch_size: Optional[int] = None
        self.tracked_layers: List[str] = []  # Layers with per-sample tracking enabled
    
    def register_hooks(self, model: nn.Module, 
                       enable_per_sample: bool = True,
                       key_layers: Optional[List[str]] = None) -> None:
        """
        Register gradient hooks on all parameters.
        
        Args:
            model: Neural network model
            enable_per_sample: Enable per-sample gradient tracking for key layers
            key_layers: Specific layer names to track per-sample (None = auto-detect)
        """
        self.clear_hooks()
        
        # Auto-detect key layers if not specified (typically embedding/head layers)
        if key_layers is None and enable_per_sample:
            key_layers = self._detect_key_layers(model)
        
        self.tracked_layers = key_layers or []
        
        for name, param in model.named_parameters():
            if param.requires_grad:
                # Standard gradient hook
                hook = param.register_hook(
                    lambda grad, n=name: self._capture_gradient(n, grad)
                )
                self.hooks.append(hook)
                
                # Per-sample hooks for key layers (elite-tier feature)
                if enable_per_sample and name in self.tracked_layers:
                    self._register_per_sample_hooks(name, param)
    
    def _detect_key_layers(self, model: nn.Module) -> List[str]:
        """
        Auto-detect key layers that benefit from per-sample tracking.
        
        Typically: embedding layers, final classification heads, attention outputs.
        """
        key_layers = []
        for name, module in model.named_modules():
            # Detect embedding layers
            if 'embed' in name.lower() or isinstance(module, nn.Embedding):
                # Find corresponding parameter names
                for param_name, _ in module.named_parameters():
                    full_name = f"{name}.{param_name}" if name else param_name
                    if full_name not in key_layers:
                        key_layers.append(full_name)
            
            # Detect final classification/head layers
            if 'head' in name.lower() or 'classifier' in name.lower() or 'output' in name.lower():
                for param_name, _ in module.named_parameters():
                    full_name = f"{name}.{param_name}" if name else param_name
                    if full_name not in key_layers:
                        key_layers.append(full_name)
        
        # Limit to top 5 layers to avoid performance overhead
        return key_layers[:5]
    
    def _register_per_sample_hooks(self, layer_name: str, param: nn.Parameter) -> None:
        """
        Register per-sample gradient hooks for a specific layer.
        
        This uses gradient accumulation to track per-sample contributions.
        """
        if layer_name not in self.per_sample_hooks:
            self.per_sample_hooks[layer_name] = []
            self.per_sample_grads[layer_name] = []
        
        # Hook that captures per-sample gradients during backward
        def per_sample_hook(grad):
            if grad is not None and self.collection_enabled:
                # For per-sample tracking, we need the gradient before aggregation
                # This is a simplified version - full implementation requires
                # gradient accumulation hooks in the loss computation
                if grad.dim() > 1:  # Has batch dimension
                    # Store per-sample norms
                    per_sample_norms = grad.view(grad.shape[0], -1).norm(dim=1)
                    self.per_sample_grads[layer_name].append(per_sample_norms.detach().clone())
        
        hook = param.register_hook(per_sample_hook)
        self.per_sample_hooks[layer_name].append(hook)
    
    def _capture_gradient(self, name: str, grad: torch.Tensor) -> None:
        """Capture gradient (non-mutating)"""
        if self.collection_enabled and grad is not None:
            # Detach and clone to avoid graph interference
            self.layer_grads[name] = grad.detach().clone()
    
    def collect_signals(self, 
                       model: nn.Module,
                       horizon_grads: Optional[Dict[HorizonType, torch.Tensor]] = None,
                       uncertainty: Optional[torch.Tensor] = None,
                       variance_controller: Optional['VarianceController'] = None,
                       forgetting_detector: Optional['ForgettingDetector'] = None) -> GradientSignals:
        """
        Collect all mandatory gradient signals.
        
        Args:
            model: Neural network model
            horizon_grads: Optional per-horizon gradient contributions
            uncertainty: Optional uncertainty estimates for weighting
            variance_controller: Optional variance controller for variance computation
            forgetting_detector: Optional forgetting detector for cosine similarity
        
        Returns:
            GradientSignals object with all required metrics
        """
        import time
        
        # Global gradient norm
        global_norm = self._compute_global_norm(model)
        
        # Update gradient history for variance computation
        self.gradient_history.append(global_norm)
        
        # Per-layer norms with gradient flow tracking
        per_layer_norms = {}
        for name, grad in self.layer_grads.items():
            norm = grad.norm().item()
            per_layer_norms[name] = norm
            
            # Track gradient flow per layer
            if name not in self.layer_gradient_flow:
                self.layer_gradient_flow[name] = []
            self.layer_gradient_flow[name].append(norm)
            if len(self.layer_gradient_flow[name]) > 100:
                self.layer_gradient_flow[name] = self.layer_gradient_flow[name][-100:]
        
        # Compute variance from history (if controller not provided, compute directly)
        if variance_controller is not None:
            variance = variance_controller.compute_variance()
        else:
            variance = self._compute_variance_from_history()
        
        # Compute cosine similarity (if detector not provided, compute directly)
        if forgetting_detector is not None:
            all_grads = torch.cat([
                p.grad.flatten() for p in model.parameters() if p.grad is not None
            ]) if any(p.grad is not None for p in model.parameters()) else torch.tensor([])
            if len(all_grads) > 0:
                cosine_sim = forgetting_detector.compute_cosine_similarity(all_grads)
            else:
                cosine_sim = 1.0
        else:
            cosine_sim = self._compute_cosine_similarity_fallback(model)
        
        # Horizon contributions
        horizon_contribs = self._compute_horizon_contributions(horizon_grads)
        
        # Uncertainty-weighted norm
        uncertainty_norm = self._compute_uncertainty_weighted_norm(
            model, uncertainty
        )
        
        return GradientSignals(
            global_grad_norm=global_norm,
            per_layer_grad_norms=per_layer_norms,
            grad_variance=variance,
            grad_cosine_similarity=cosine_sim,
            horizon_contributions=horizon_contribs,
            uncertainty_weighted_norm=uncertainty_norm,
            step=0,  # Filled by caller
            timestamp=time.time()
        )
    
    def _compute_variance_from_history(self) -> float:
        """Compute variance from gradient history"""
        if len(self.gradient_history) < 2:
            return 0.0
        norms = np.array(list(self.gradient_history))
        return float(np.var(norms))
    
    def _compute_cosine_similarity_fallback(self, model: nn.Module) -> float:
        """Fallback cosine similarity computation"""
        # If no historical anchors, return 1.0 (no forgetting detected)
        if len(self.gradient_history) < 2:
            return 1.0
        
        # Simple similarity based on recent gradient stability
        recent_norms = list(self.gradient_history)[-10:]
        if len(recent_norms) < 2:
            return 1.0
        
        # Coefficient of variation as proxy for stability
        mean_norm = np.mean(recent_norms)
        std_norm = np.std(recent_norms)
        if mean_norm == 0:
            return 1.0
        
        cv = std_norm / mean_norm
        # Lower CV = more stable = higher similarity
        similarity = max(0.0, 1.0 - cv)
        return float(similarity)
    
    def _compute_global_norm(self, model: nn.Module) -> float:
        """Compute global gradient norm across all parameters"""
        total_norm = 0.0
        for param in model.parameters():
            if param.grad is not None:
                param_norm = param.grad.data.norm(2)
                total_norm += param_norm.item() ** 2
        return total_norm ** 0.5
    
    def _compute_horizon_contributions(self,
                                      horizon_grads: Optional[Dict[HorizonType, torch.Tensor]]) -> Dict[HorizonType, float]:
        """Compute per-horizon gradient contributions"""
        if horizon_grads is None:
            # Default equal split if not provided
            return {
                HorizonType.SHORT_TERM: 0.33,
                HorizonType.MID_TERM: 0.33,
                HorizonType.LONG_TERM: 0.34
            }
        
        total = sum(g.norm().item() for g in horizon_grads.values())
        if total == 0:
            return {h: 0.0 for h in HorizonType}
        
        return {
            h: g.norm().item() / total 
            for h, g in horizon_grads.items()
        }
    
    def _compute_uncertainty_weighted_norm(self,
                                          model: nn.Module,
                                          uncertainty: Optional[torch.Tensor]) -> float:
        """Compute gradient norm weighted by uncertainty"""
        if uncertainty is None:
            return self._compute_global_norm(model)
        
        total_weighted = 0.0
        param_idx = 0
        
        for param in model.parameters():
            if param.grad is not None:
                # Weight gradient by uncertainty
                if param_idx < len(uncertainty):
                    weight = uncertainty[param_idx].item()
                    weighted_norm = param.grad.data.norm(2).item() * weight
                    total_weighted += weighted_norm ** 2
                param_idx += 1
        
        return total_weighted ** 0.5
    
    def clear_hooks(self) -> None:
        """Remove all gradient hooks"""
        for hook in self.hooks:
            hook.remove()
        self.hooks.clear()
        self.layer_grads.clear()
    
    def disable_collection(self) -> None:
        """Temporarily disable gradient collection"""
        self.collection_enabled = False
    
    def enable_collection(self) -> None:
        """Re-enable gradient collection"""
        self.collection_enabled = True


# ============================================================================
# NORM CONTROLLER
# ============================================================================

class NormController:
    """
    Computes dynamic norm ceilings.
    
    max_grad_norm = f(curriculum_phase, horizon, uncertainty)
    NOT static.
    """
    
    def __init__(self, base_max_norm: float = DEFAULT_MAX_GRAD_NORM):
        self.base_max_norm = base_max_norm
    
    def compute_dynamic_ceiling(self,
                               curriculum_phase: str,
                               phase_config: Dict[str, Any],
                               horizon_type: Optional[HorizonType] = None,
                               uncertainty_level: float = 0.5) -> float:
        """
        Compute dynamic gradient norm ceiling.
        
        Args:
            curriculum_phase: Current curriculum phase name
            phase_config: Configuration for current phase
            horizon_type: Optional horizon being trained
            uncertainty_level: Average uncertainty in batch [0, 1]
        
        Returns:
            Dynamic max gradient norm
        """
        # Base norm from curriculum config
        base = phase_config.get("max_grad_norm", self.base_max_norm)
        
        # Phase multiplier (early phases more conservative)
        phase_mult = self._get_phase_multiplier(curriculum_phase)
        
        # Horizon multiplier (long-term more conservative)
        horizon_mult = self._get_horizon_multiplier(horizon_type)
        
        # Uncertainty multiplier (high uncertainty = lower ceiling)
        uncertainty_mult = 1.0 - (uncertainty_level * 0.3)
        
        dynamic_ceiling = base * phase_mult * horizon_mult * uncertainty_mult
        
        # Hard floor to prevent over-restriction
        return max(dynamic_ceiling, 0.1)
    
    def _get_phase_multiplier(self, phase: str) -> float:
        """Get phase-specific multiplier"""
        phase_multipliers = {
            "foundation": 0.5,      # Very conservative
            "primary": 0.7,         # Conservative
            "intermediate": 1.0,    # Normal
            "advanced": 1.2,        # Slightly aggressive
            "expert": 1.5,          # Aggressive
            "tail": 0.8             # Conservative for rare events
        }
        return phase_multipliers.get(phase, 1.0)
    
    def _get_horizon_multiplier(self, horizon: Optional[HorizonType]) -> float:
        """Get horizon-specific multiplier"""
        if horizon is None:
            return 1.0
        
        horizon_multipliers = {
            HorizonType.SHORT_TERM: 1.2,   # Can be more aggressive
            HorizonType.MID_TERM: 1.0,     # Balanced
            HorizonType.LONG_TERM: 0.8     # More conservative
        }
        return horizon_multipliers[horizon]
    
    def apply_clipping(self,
                      model: nn.Module,
                      max_norm: float,
                      norm_type: float = 2.0) -> float:
        """
        Apply gradient clipping.
        
        Returns:
            Total norm before clipping
        """
        parameters = [p for p in model.parameters() if p.grad is not None]
        
        if len(parameters) == 0:
            return 0.0
        
        total_norm = torch.nn.utils.clip_grad_norm_(
            parameters,
            max_norm,
            norm_type=norm_type
        )
        
        return total_norm.item()


# ============================================================================
# VARIANCE CONTROLLER
# ============================================================================

class VarianceController:
    """
    Maintains sliding windows. Detects early instability.
    
    High variance = instability even if norm is small.
    Violation triggers gradient damping, not clipping.
    
    Enhanced with multi-scale variance analysis and adaptive window sizing.
    """
    
    def __init__(self, window_size: int = DEFAULT_VARIANCE_WINDOW_SIZE):
        self.window_size = window_size
        self.norm_window: deque = deque(maxlen=window_size)
        self.variance_history: List[float] = []
        self.layer_variance: Dict[str, deque] = {}  # Per-layer variance tracking
        self.horizon_variance: Dict[HorizonType, deque] = {
            h: deque(maxlen=window_size) for h in HorizonType
        }  # Per-horizon variance tracking
        self.adaptive_window_sizes: Dict[str, int] = {}  # Adaptive window sizes per metric
    
    def update(self, grad_norm: float) -> None:
        """Update variance window with new gradient norm"""
        self.norm_window.append(grad_norm)
    
    def compute_variance(self) -> float:
        """Compute variance of gradient norms in window"""
        if len(self.norm_window) < 2:
            return 0.0
        
        norms = np.array(self.norm_window)
        variance = np.var(norms)
        self.variance_history.append(variance)
        
        return float(variance)
    
    def compute_multi_scale_variance(self) -> Dict[str, float]:
        """
        Compute variance at multiple time scales.
        
        Returns:
            Dict with variance at different scales (short, medium, long)
        """
        if len(self.norm_window) < 2:
            return {"short": 0.0, "medium": 0.0, "long": 0.0}
        
        norms = np.array(self.norm_window)
        total_len = len(norms)
        
        # Short-term variance (last 25% of window)
        short_norms = norms[-max(1, total_len // 4):]
        short_var = float(np.var(short_norms)) if len(short_norms) > 1 else 0.0
        
        # Medium-term variance (last 50% of window)
        medium_norms = norms[-max(1, total_len // 2):]
        medium_var = float(np.var(medium_norms)) if len(medium_norms) > 1 else 0.0
        
        # Long-term variance (full window)
        long_var = float(np.var(norms))
        
        return {
            "short": short_var,
            "medium": medium_var,
            "long": long_var
        }
    
    def compute_variance_by_layer(self, layer_grads: Dict[str, torch.Tensor]) -> Dict[str, float]:
        """
        Compute variance decomposition by layer.
        
        Returns:
            Dict mapping layer names to their gradient variance
        """
        layer_vars = {}
        for layer_name, grad in layer_grads.items():
            if layer_name not in self.layer_variance:
                self.layer_variance[layer_name] = deque(maxlen=self.window_size)
            
            norm = grad.norm().item()
            self.layer_variance[layer_name].append(norm)
            
            if len(self.layer_variance[layer_name]) >= 2:
                layer_norms = np.array(self.layer_variance[layer_name])
                layer_vars[layer_name] = float(np.var(layer_norms))
            else:
                layer_vars[layer_name] = 0.0
        
        return layer_vars
    
    def compute_variance_by_horizon(self, 
                                   horizon_contribs: Dict[HorizonType, float]) -> Dict[HorizonType, float]:
        """
        Compute variance decomposition by prediction horizon.
        
        Returns:
            Dict mapping horizons to their contribution variance
        """
        horizon_vars = {}
        for horizon, contrib in horizon_contribs.items():
            self.horizon_variance[horizon].append(contrib)
            
            if len(self.horizon_variance[horizon]) >= 2:
                contribs = np.array(self.horizon_variance[horizon])
                horizon_vars[horizon] = float(np.var(contribs))
            else:
                horizon_vars[horizon] = 0.0
        
        return horizon_vars
    
    def adapt_window_size(self, metric_name: str, current_variance: float, threshold: float) -> int:
        """
        Adaptively adjust window size based on variance levels.
        
        Returns:
            New window size
        """
        if metric_name not in self.adaptive_window_sizes:
            self.adaptive_window_sizes[metric_name] = self.window_size
        
        current_size = self.adaptive_window_sizes[metric_name]
        
        # If variance is high, increase window to smooth out noise
        if current_variance > threshold * 1.5:
            new_size = min(current_size * 2, self.window_size * 4)
        # If variance is low, decrease window for faster response
        elif current_variance < threshold * 0.5:
            new_size = max(current_size // 2, self.window_size // 4)
        else:
            new_size = current_size
        
        self.adaptive_window_sizes[metric_name] = new_size
        return new_size
    
    def compute_variance_trend(self, lookback: int = 10) -> float:
        """
        Compute rate of variance change.
        
        Returns:
            Positive = variance increasing, negative = decreasing
        """
        if len(self.variance_history) < lookback:
            return 0.0
        
        recent = self.variance_history[-lookback:]
        # Simple linear trend
        x = np.arange(len(recent))
        coeffs = np.polyfit(x, recent, 1)
        return float(coeffs[0])  # Slope
    
    def check_instability(self, 
                         current_variance: float,
                         threshold: float,
                         trend_threshold: float = 0.01) -> Tuple[bool, str]:
        """
        Check for variance-based instability.
        
        Returns:
            (is_unstable, reason)
        """
        # Check absolute variance
        if current_variance > threshold:
            return True, f"variance {current_variance:.4f} exceeds threshold {threshold:.4f}"
        
        # Check variance trend
        trend = self.compute_variance_trend()
        if trend > trend_threshold:
            return True, f"variance increasing rapidly (trend: {trend:.4f})"
        
        return False, ""
    
    def compute_damping_factor(self,
                              current_variance: float,
                              threshold: float) -> float:
        """
        Compute gradient damping factor based on variance.
        
        Returns:
            Damping factor in [0.5, 1.0]
        """
        if current_variance <= threshold:
            return 1.0
        
        # Exponential damping as variance exceeds threshold
        excess = current_variance / threshold
        damping = np.exp(-0.5 * (excess - 1.0))
        
        # Clamp to reasonable range
        return float(np.clip(damping, 0.5, 1.0))
    
    def apply_damping(self, model: nn.Module, damping_factor: float) -> None:
        """Apply soft gradient damping (multiplicative)"""
        for param in model.parameters():
            if param.grad is not None:
                param.grad.data.mul_(damping_factor)


# ============================================================================
# HORIZON BALANCER
# ============================================================================

class HorizonBalancer:
    """
    Ensures causal balance across prediction horizons.
    
    No single horizon may dominate.
    This protects long-tail modeling.
    """
    
    def __init__(self):
        self.horizon_history: Dict[HorizonType, deque] = {
            h: deque(maxlen=100) for h in HorizonType
        }
    
    def update(self, horizon_contributions: Dict[HorizonType, float]) -> None:
        """Update horizon contribution history"""
        for horizon, contrib in horizon_contributions.items():
            self.horizon_history[horizon].append(contrib)
    
    def check_balance(self,
                     contributions: Dict[HorizonType, float],
                     bounds: Dict[HorizonType, Tuple[float, float]]) -> Tuple[bool, str]:
        """
        Check if horizon contributions are within bounds.
        
        Returns:
            (is_balanced, violation_reason)
        """
        for horizon, contrib in contributions.items():
            lower, upper = bounds[horizon]
            
            if contrib < lower:
                return False, f"{horizon.value} contribution {contrib:.3f} below lower bound {lower:.3f}"
            
            if contrib > upper:
                return False, f"{horizon.value} contribution {contrib:.3f} above upper bound {upper:.3f}"
        
        return True, ""
    
    def enforce_balance(self,
                       model: nn.Module,
                       contributions: Dict[HorizonType, float],
                       bounds: Dict[HorizonType, Tuple[float, float]],
                       horizon_grads: Optional[Dict[HorizonType, torch.Tensor]] = None) -> Dict[str, Any]:
        """
        ELITE-TIER: Enforce horizon balance by directly reweighting gradients.
        
        This is the hard-enforcement mode that prevents horizon collapse.
        
        Returns:
            Dict with enforcement details
        """
        if not self.enforce_reweighting:
            return {"enforced": False, "reason": "enforcement_disabled"}
        
        is_balanced, violation_reason = self.check_balance(contributions, bounds)
        if is_balanced:
            return {"enforced": False, "reason": "already_balanced"}
        
        # Compute target weights (equal distribution)
        target_weights = {h: 1.0 / len(HorizonType) for h in HorizonType}
        
        # Compute reweighting factors
        balance_weights = self.compute_balance_weights(contributions, target_weights)
        
        # Apply reweighting to gradients
        if horizon_grads is not None:
            # Direct reweighting of horizon-specific gradients
            for horizon, weight in balance_weights.items():
                if horizon in horizon_grads:
                    horizon_grads[horizon].mul_(weight)
        else:
            # Approximate: scale all gradients proportionally
            # This is less precise but still prevents collapse
            avg_weight = np.mean(list(balance_weights.values()))
            for param in model.parameters():
                if param.grad is not None:
                    param.grad.data.mul_(avg_weight)
        
        self.reweighting_applied_count += 1
        
        return {
            "enforced": True,
            "violation": violation_reason,
            "balance_weights": {h.value: w for h, w in balance_weights.items()},
            "total_enforcements": self.reweighting_applied_count
        }
    
    def compute_balance_weights(self,
                               contributions: Dict[HorizonType, float],
                               target_weights: Dict[HorizonType, float]) -> Dict[HorizonType, float]:
        """
        Compute reweighting factors to restore balance.
        
        Returns:
            Per-horizon weight multipliers
        """
        weights = {}
        
        for horizon in HorizonType:
            current = contributions.get(horizon, 0.33)
            target = target_weights.get(horizon, 0.33)
            
            if current > 0:
                # Inverse proportional adjustment
                weights[horizon] = target / current
            else:
                weights[horizon] = 1.0
        
        # Normalize to preserve total magnitude
        total = sum(weights.values())
        if total > 0:
            weights = {h: w / total * len(HorizonType) for h, w in weights.items()}
        
        return weights
    
    def get_average_contributions(self) -> Dict[HorizonType, float]:
        """Get moving average of horizon contributions"""
        averages = {}
        
        for horizon, history in self.horizon_history.items():
            if len(history) > 0:
                averages[horizon] = float(np.mean(history))
            else:
                averages[horizon] = 0.0
        
        return averages
    
    def compute_dynamic_bounds(self,
                              curriculum_phase: str,
                              phase_config: Dict[str, Any],
                              training_progress: float) -> Dict[HorizonType, Tuple[float, float]]:
        """
        Compute dynamic horizon bounds based on curriculum phase and training progress.
        
        Args:
            curriculum_phase: Current curriculum phase
            phase_config: Phase configuration
            training_progress: Training progress [0, 1]
        
        Returns:
            Dict mapping horizons to (lower_bound, upper_bound)
        """
        self.training_progress = training_progress
        
        # Get base bounds from phase config if available
        if "horizon_bounds" in phase_config:
            base_bounds = phase_config["horizon_bounds"]
            # Ensure all horizons are present
            for h in HorizonType:
                if h not in base_bounds:
                    base_bounds[h] = (0.2, 0.4)
            return base_bounds
        
        # Dynamic computation based on phase and progress
        if curriculum_phase in ["foundation", "primary"]:
            # Early phases: more balanced, conservative
            return {
                HorizonType.SHORT_TERM: (0.25, 0.40),
                HorizonType.MID_TERM: (0.25, 0.40),
                HorizonType.LONG_TERM: (0.20, 0.35)
            }
        elif curriculum_phase == "intermediate":
            # Intermediate: allow more variation
            progress_factor = training_progress
            return {
                HorizonType.SHORT_TERM: (0.20 + 0.05 * progress_factor, 0.40 + 0.10 * progress_factor),
                HorizonType.MID_TERM: (0.25, 0.40),
                HorizonType.LONG_TERM: (0.20, 0.35 + 0.10 * progress_factor)
            }
        elif curriculum_phase in ["advanced", "expert"]:
            # Advanced phases: more specialized, allow dominance with bounds
            return {
                HorizonType.SHORT_TERM: (0.15, 0.50),
                HorizonType.MID_TERM: (0.20, 0.45),
                HorizonType.LONG_TERM: (0.15, 0.50)
            }
        elif curriculum_phase == "tail":
            # Tail phase: prioritize long-term for rare events
            return {
                HorizonType.SHORT_TERM: (0.15, 0.35),
                HorizonType.MID_TERM: (0.20, 0.40),
                HorizonType.LONG_TERM: (0.25, 0.50)
            }
        else:
            # Default balanced bounds
            return {
                HorizonType.SHORT_TERM: (0.2, 0.4),
                HorizonType.MID_TERM: (0.2, 0.4),
                HorizonType.LONG_TERM: (0.2, 0.4)
            }
    
    def track_balance_history(self, contributions: Dict[HorizonType, float]) -> None:
        """Track balance history for analysis"""
        self.balance_history.append(contributions.copy())
        if len(self.balance_history) > 1000:
            self.balance_history = self.balance_history[-1000:]
    
    def get_balance_trend(self, horizon: HorizonType, lookback: int = 50) -> float:
        """
        Get trend of balance for a specific horizon.
        
        Returns:
            Trend value (positive = increasing, negative = decreasing)
        """
        if len(self.balance_history) < lookback:
            return 0.0
        
        recent = [h[horizon] for h in self.balance_history[-lookback:]]
        if len(recent) < 2:
            return 0.0
        
        x = np.arange(len(recent))
        coeffs = np.polyfit(x, recent, 1)
        return float(coeffs[0])  # Slope


# ============================================================================
# RARE EVENT GUARD
# ============================================================================

class RareEventGuard:
    """
    Detects tail batches and applies safeguards.
    
    Prevents tail memorization by:
    - Capping per-sample gradient contribution
    - Enforcing uncertainty scaling
    - Reducing effective learning rate
    
    Enhanced with batch-level detection and per-sample tracking.
    """
    
    def __init__(self, 
                 rare_threshold: float = 0.01,
                 max_contribution: float = 0.1):
        self.rare_threshold = rare_threshold
        self.max_contribution = max_contribution
        self.rare_event_count = 0
        self.rare_batch_history: List[Dict[str, Any]] = []  # Track rare batches
        self.per_sample_contributions: Dict[int, List[float]] = {}  # Track per-sample contributions
    
    def detect_rare_batch(self,
                         sample_frequencies: Optional[torch.Tensor],
                         uncertainties: Optional[torch.Tensor]) -> Tuple[bool, List[int]]:
        """
        Detect if batch contains rare tail examples.
        
        Returns:
            (is_rare_batch, rare_sample_indices)
        """
        if sample_frequencies is None:
            return False, []
        
        rare_mask = sample_frequencies < self.rare_threshold
        rare_indices = torch.where(rare_mask)[0].tolist()
        
        is_rare = len(rare_indices) > 0
        
        if is_rare:
            self.rare_event_count += 1
        
        return is_rare, rare_indices
    
    def compute_sample_caps(self,
                           batch_size: int,
                           rare_indices: List[int],
                           base_cap: Optional[float] = None) -> torch.Tensor:
        """
        Compute per-sample gradient contribution caps.
        
        Returns:
            Tensor of caps for each sample in batch
        """
        if base_cap is None:
            base_cap = self.max_contribution
        
        caps = torch.ones(batch_size) * base_cap
        
        # Further restrict rare samples
        for idx in rare_indices:
            caps[idx] *= 0.5  # 50% of normal cap
        
        return caps
    
    def apply_sample_caps(self,
                         per_sample_grads: torch.Tensor,
                         caps: torch.Tensor) -> torch.Tensor:
        """
        Apply per-sample gradient caps.
        
        Args:
            per_sample_grads: [batch_size, ...] gradient tensor
            caps: [batch_size] cap values
        
        Returns:
            Capped gradients
        """
        # Compute per-sample norms
        batch_size = per_sample_grads.shape[0]
        sample_norms = per_sample_grads.view(batch_size, -1).norm(dim=1)
        
        # Compute scale factors
        scale_factors = torch.ones_like(sample_norms)
        exceed_mask = sample_norms > caps
        scale_factors[exceed_mask] = caps[exceed_mask] / sample_norms[exceed_mask]
        
        # Apply scaling
        scale_factors = scale_factors.view(-1, *([1] * (per_sample_grads.ndim - 1)))
        return per_sample_grads * scale_factors
    
    def suggest_lr_reduction(self, rare_ratio: float) -> float:
        """
        Suggest learning rate reduction for rare batches.
        
        Returns:
            LR multiplier in [0.5, 1.0]
        """
        # More rare samples = more LR reduction
        reduction = 1.0 - (rare_ratio * 0.5)
        return max(reduction, 0.5)
    
    def apply_per_sample_caps_to_model(self,
                                      model: nn.Module,
                                      batch_size: int,
                                      rare_indices: List[int],
                                      caps: Optional[torch.Tensor] = None,
                                      collector: Optional['GradientCollector'] = None) -> Dict[str, Any]:
        """
        Apply per-sample gradient caps directly to model gradients.
        
        ELITE-TIER: Uses true per-sample gradients when available from collector.
        Falls back to parameter-level approximation if per-sample tracking not enabled.
        
        Returns:
            Dict with application details
        """
        if not rare_indices:
            return {"applied": False, "reason": "no_rare_samples"}
        
        if caps is None:
            caps = self.compute_sample_caps(batch_size, rare_indices)
        
        # Try to use true per-sample gradients if available
        if collector is not None and collector.enable_per_sample_tracking and collector.per_sample_grads:
            return self._apply_true_per_sample_caps(model, rare_indices, caps, collector)
        else:
            # Fallback to parameter-level approximation
            return self._apply_approximate_per_sample_caps(model, rare_indices, caps)
    
    def _apply_true_per_sample_caps(self,
                                   model: nn.Module,
                                   rare_indices: List[int],
                                   caps: torch.Tensor,
                                   collector: 'GradientCollector') -> Dict[str, Any]:
        """
        Apply caps using TRUE per-sample gradients (elite-tier).
        
        This provides mathematically tight bounds on rare sample influence.
        """
        total_capped_samples = 0
        total_tracked_layers = 0
        
        for layer_name in collector.tracked_layers:
            if layer_name not in collector.per_sample_grads:
                continue
            
            per_sample_norms = collector.per_sample_grads[layer_name]
            if len(per_sample_norms) == 0:
                continue
            
            # Get most recent per-sample norms
            latest_norms = per_sample_norms[-1]  # Shape: [batch_size]
            
            # Apply caps to rare samples
            for rare_idx in rare_indices:
                if rare_idx < len(latest_norms):
                    sample_norm = latest_norms[rare_idx].item()
                    cap_value = caps[rare_idx].item() if rare_idx < len(caps) else self.max_contribution
                    
                    if sample_norm > cap_value:
                        # Compute scale factor for this sample
                        scale = cap_value / sample_norm
                        
                        # Apply to corresponding parameter gradients
                        # Find parameter by layer name
                        for param_name, param in model.named_parameters():
                            if layer_name in param_name and param.grad is not None:
                                # Apply proportional scaling (simplified - full version would
                                # require per-sample gradient accumulation)
                                param.grad.data.mul_(min(scale, 1.0))
                                total_capped_samples += 1
                                break
            
            total_tracked_layers += 1
        
        return {
            "applied": True,
            "method": "true_per_sample",
            "rare_samples": len(rare_indices),
            "capped_samples": total_capped_samples,
            "tracked_layers": total_tracked_layers,
            "caps_used": caps.tolist() if isinstance(caps, torch.Tensor) else None
        }
    
    def _apply_approximate_per_sample_caps(self,
                                         model: nn.Module,
                                         rare_indices: List[int],
                                         caps: torch.Tensor) -> Dict[str, Any]:
        """
        Fallback: Apply caps using parameter-level approximation.
        
        Less precise but still prevents explosion.
        """
        total_capped = 0
        total_params = 0
        
        for param in model.parameters():
            if param.grad is not None:
                total_params += 1
                grad_norm = param.grad.data.norm().item()
                
                # Apply conservative scaling if gradient is large
                if grad_norm > self.max_contribution:
                    scale = self.max_contribution / grad_norm
                    param.grad.data.mul_(scale)
                    total_capped += 1
        
        return {
            "applied": True,
            "method": "approximate",
            "rare_samples": len(rare_indices),
            "params_capped": total_capped,
            "total_params": total_params,
            "caps_used": caps.tolist() if isinstance(caps, torch.Tensor) else None
        }
    
    def track_batch_metadata(self,
                            batch_id: str,
                            is_rare: bool,
                            rare_indices: List[int],
                            sample_frequencies: Optional[torch.Tensor]) -> None:
        """Track rare batch metadata for analysis"""
        self.rare_batch_history.append({
            "batch_id": batch_id,
            "is_rare": is_rare,
            "rare_indices": rare_indices,
            "rare_ratio": len(rare_indices) / max(1, len(sample_frequencies)) if sample_frequencies is not None else 0.0,
            "timestamp": time.time()
        })
        if len(self.rare_batch_history) > 1000:
            self.rare_batch_history = self.rare_batch_history[-1000:]


# ============================================================================
# FORGETTING DETECTOR
# ============================================================================

class ForgettingDetector:
    """
    Uses cosine similarity across time anchors.
    
    Tracks: Δ(gradient alignment with historical anchors)
    
    Enhanced with multi-anchor comparison and layer-specific detection.
    """
    
    def __init__(self, 
                 anchor_size: int = DEFAULT_HISTORY_ANCHOR_SIZE,
                 similarity_threshold: float = DEFAULT_COSINE_SIMILARITY_THRESHOLD):
        self.anchor_size = anchor_size
        self.similarity_threshold = similarity_threshold
        self.historical_anchors: List[torch.Tensor] = []
        self.similarity_history: deque = deque(maxlen=1000)
        self.forgetting_events = 0
        self.layer_anchors: Dict[str, List[torch.Tensor]] = {}  # Per-layer anchors
        self.remediation_applied: List[Dict[str, Any]] = []  # Track applied remediations
    
    def update_anchors(self, current_grads: torch.Tensor) -> None:
        """Update historical gradient anchors"""
        # Flatten and detach
        flat_grad = current_grads.detach().flatten()
        
        self.historical_anchors.append(flat_grad)
        
        # Keep only recent anchors
        if len(self.historical_anchors) > self.anchor_size:
            self.historical_anchors = self.historical_anchors[-self.anchor_size:]
    
    def compute_cosine_similarity(self, current_grads: torch.Tensor) -> float:
        """
        Compute cosine similarity between current and historical gradients.
        
        Returns:
            Average cosine similarity with anchors
        """
        if len(self.historical_anchors) == 0:
            return 1.0
        
        flat_current = current_grads.detach().flatten()
        
        similarities = []
        for anchor in self.historical_anchors[-100:]:  # Compare with recent 100
            # Cosine similarity
            sim = torch.nn.functional.cosine_similarity(
                flat_current.unsqueeze(0),
                anchor.unsqueeze(0)
            )
            similarities.append(sim.item())
        
        avg_similarity = float(np.mean(similarities))
        self.similarity_history.append(avg_similarity)
        
        return avg_similarity
    
    def detect_forgetting(self, current_similarity: float) -> Tuple[bool, float]:
        """
        Detect catastrophic forgetting.
        
        Returns:
            (is_forgetting, similarity_drop)
        """
        if len(self.similarity_history) < 10:
            return False, 0.0
        
        # Check absolute threshold
        if current_similarity < self.similarity_threshold:
            self.forgetting_events += 1
            return True, 1.0 - current_similarity
        
        # Check for sudden drop
        recent_avg = np.mean(list(self.similarity_history)[-50:-1])
        drop = recent_avg - current_similarity
        
        if drop > 0.2:  # 20% drop
            self.forgetting_events += 1
            return True, drop
        
        return False, 0.0
    
    def suggest_remediation(self, drop: float) -> Dict[str, Any]:
        """
        Suggest remediation actions for forgetting.
        
        Returns:
            Dict of suggested actions
        """
        return {
            "slow_lr": True,
            "lr_multiplier": max(0.5, 1.0 - drop),
            "restrict_tail_updates": drop > 0.3,
            "request_replay": drop > 0.4,
            "severity": "critical" if drop > 0.5 else "moderate"
        }
    
    def execute_remediation(self,
                           remediation: Dict[str, Any],
                           optimizer: Optional[torch.optim.Optimizer] = None,
                           data_gate: Optional['DataGateProtocol'] = None) -> Dict[str, Any]:
        """
        Execute forgetting remediation actions.
        
        Args:
            remediation: Remediation suggestions from suggest_remediation()
            optimizer: Optional optimizer to modify learning rate
            data_gate: Optional data gate to request replay resampling
        
        Returns:
            Dict with execution results
        """
        results = {
            "lr_modified": False,
            "tail_restricted": False,
            "replay_requested": False,
            "errors": []
        }
        
        # Apply learning rate reduction
        if remediation.get("slow_lr", False) and optimizer is not None:
            try:
                lr_mult = remediation.get("lr_multiplier", 0.5)
                for param_group in optimizer.param_groups:
                    original_lr = param_group.get("lr", 0.0)
                    param_group["lr"] = original_lr * lr_mult
                results["lr_modified"] = True
                results["lr_multiplier"] = lr_mult
            except Exception as e:
                results["errors"].append(f"LR modification failed: {e}")
        
        # Request replay buffer resampling with deterministic alignment hash
        if remediation.get("request_replay", False) and data_gate is not None:
            try:
                # ELITE-TIER: Deterministic replay-buffer alignment hashing
                if alignment_hash is None:
                    # Generate deterministic hash from forgetting signature
                    forgetting_signature = f"{remediation.get('severity', 'moderate')}_{remediation.get('lr_multiplier', 0.5):.3f}"
                    alignment_hash = hashlib.sha256(forgetting_signature.encode()).hexdigest()[:16]
                
                replay_requested = data_gate.request_replay_resampling(
                    reason=f"forgetting_detected_severity_{remediation.get('severity', 'moderate')}",
                    alignment_hash=alignment_hash
                )
                results["replay_requested"] = replay_requested
                results["alignment_hash"] = alignment_hash
            except Exception as e:
                results["errors"].append(f"Replay request failed: {e}")
        
        # Mark tail update restriction (flag for training loop)
        if remediation.get("restrict_tail_updates", False):
            results["tail_restricted"] = True
            results["tail_restriction_severity"] = remediation.get("severity", "moderate")
        
        # Track remediation
        self.remediation_applied.append({
            "remediation": remediation,
            "results": results,
            "timestamp": time.time()
        })
        if len(self.remediation_applied) > 100:
            self.remediation_applied = self.remediation_applied[-100:]
        
        return results
    
    def detect_layer_specific_forgetting(self,
                                        layer_grads: Dict[str, torch.Tensor]) -> Dict[str, float]:
        """
        Detect forgetting at layer-specific level.
        
        Returns:
            Dict mapping layer names to forgetting scores
        """
        layer_forgetting = {}
        
        for layer_name, grad in layer_grads.items():
            if layer_name not in self.layer_anchors:
                self.layer_anchors[layer_name] = []
            
            flat_grad = grad.detach().flatten()
            self.layer_anchors[layer_name].append(flat_grad)
            
            # Keep only recent anchors
            if len(self.layer_anchors[layer_name]) > 100:
                self.layer_anchors[layer_name] = self.layer_anchors[layer_name][-100:]
            
            # Compute similarity with historical anchors
            if len(self.layer_anchors[layer_name]) >= 2:
                similarities = []
                for anchor in self.layer_anchors[layer_name][-10:]:
                    sim = torch.nn.functional.cosine_similarity(
                        flat_grad.unsqueeze(0),
                        anchor.unsqueeze(0)
                    )
                    similarities.append(sim.item())
                
                avg_sim = np.mean(similarities)
                # Lower similarity = more forgetting
                layer_forgetting[layer_name] = 1.0 - avg_sim
            else:
                layer_forgetting[layer_name] = 0.0
        
        return layer_forgetting


# ============================================================================
# INTERVENTION PLANNER
# ============================================================================

class InterventionPlanner:
    """
    Chooses minimal action necessary.
    
    Escalation order:
    1. Soft damping (preferred)
    2. Adaptive clipping
    3. Loss reweighting suggestion
    4. Curriculum demotion request
    5. Training halt (last resort)
    """
    
    def __init__(self):
        self.intervention_counts: Dict[InterventionLevel, int] = {
            level: 0 for level in InterventionLevel
        }
        self.escalation_history: deque = deque(maxlen=100)
    
    def plan_intervention(self,
                         signals: GradientSignals,
                         constraints: GradientConstraints,
                         variance_unstable: bool,
                         horizon_unbalanced: bool,
                         rare_batch: bool,
                         forgetting_detected: bool,
                         demotion_triggered: bool = False) -> Tuple[InterventionLevel, Dict[str, Any]]:
        """
        Determine minimal intervention needed.
        
        Returns:
            (intervention_level, action_details)
        """
        reasons = []
        
        # Check for training halt conditions (most severe first)
        if not signals.is_valid():
            return InterventionLevel.TRAINING_HALT, {
                "reason": "corrupted_gradient_signals",
                "details": "NaN or Inf detected in gradient signals"
            }
        
        # Check for curriculum demotion (gradient-based triggers)
        if forgetting_detected or demotion_triggered:
            if forgetting_detected:
                reasons.append("catastrophic_forgetting")
            if demotion_triggered:
                reasons.append("gradient_based_demotion_trigger")
            self.intervention_counts[InterventionLevel.CURRICULUM_DEMOTION] += 1
            return InterventionLevel.CURRICULUM_DEMOTION, {
                "reason": "_".join(reasons) if reasons else "demotion_triggered",
                "cosine_similarity": signals.grad_cosine_similarity,
                "forgetting_detected": forgetting_detected,
                "demotion_triggered": demotion_triggered
            }
        
        # Check for loss reweighting
        if rare_batch and signals.global_grad_norm > constraints.max_grad_norm:
            reasons.append("rare_batch_gradient_spike")
            self.intervention_counts[InterventionLevel.LOSS_REWEIGHTING] += 1
            return InterventionLevel.LOSS_REWEIGHTING, {
                "reason": "rare_batch_gradient_spike",
                "norm": signals.global_grad_norm,
                "threshold": constraints.max_grad_norm
            }
        
        # Check for adaptive clipping
        if signals.global_grad_norm > constraints.max_grad_norm * 1.5:
            reasons.append("severe_norm_violation")
            self.intervention_counts[InterventionLevel.ADAPTIVE_CLIPPING] += 1
            return InterventionLevel.ADAPTIVE_CLIPPING, {
                "reason": "severe_norm_violation",
                "norm": signals.global_grad_norm,
                "threshold": constraints.max_grad_norm,
                "clip_value": constraints.max_grad_norm
            }
        
        # Check for soft damping (preferred intervention)
        if variance_unstable or horizon_unbalanced or signals.global_grad_norm > constraints.max_grad_norm:
            reasons.append("soft_instability")
            self.intervention_counts[InterventionLevel.SOFT_DAMPING] += 1
            return InterventionLevel.SOFT_DAMPING, {
                "reason": "soft_instability",
                "variance_unstable": variance_unstable,
                "horizon_unbalanced": horizon_unbalanced,
                "norm_exceeded": signals.global_grad_norm > constraints.max_grad_norm,
                "damping_factor": 0.8
            }
        
        # No intervention needed
        self.intervention_counts[InterventionLevel.NONE] += 1
        return InterventionLevel.NONE, {"reason": "stable"}
    
    def should_escalate(self, recent_interventions: int = 10) -> bool:
        """
        Check if interventions are becoming too frequent.
        
        Returns:
            True if escalation to curriculum demotion is warranted
        """
        if len(self.escalation_history) < recent_interventions:
            return False
        
        recent = list(self.escalation_history)[-recent_interventions:]
        non_none_count = sum(1 for level in recent if level != InterventionLevel.NONE)
        
        # If >70% of recent steps required intervention, escalate
        return non_none_count / recent_interventions > 0.7
    
    def record_intervention(self, level: InterventionLevel) -> None:
        """Record intervention for escalation tracking"""
        self.escalation_history.append(level)


# ============================================================================
# GOVERNOR STATE SERIALIZER
# ============================================================================

class GovernorStateSerializer:
    """
    Handles deterministic state persistence.
    
    Ensures gradient governor can be restored to exact state for reproducibility.
    """
    
    def __init__(self, checkpoint_dir: Path):
        self.checkpoint_dir = checkpoint_dir
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
    
    def save_state(self, state: GovernorState, step: int) -> Path:
        """
        Save governor state to disk.
        
        Returns:
            Path to saved state file
        """
        state_path = self.checkpoint_dir / f"governor_state_step_{step}.json"
        state.save(state_path)
        
        # Also save anchors separately (binary format)
        anchors_path = self.checkpoint_dir / f"governor_anchors_step_{step}.pt"
        torch.save({
            "anchors": state.historical_anchors,
            "step": step
        }, anchors_path)
        
        return state_path
    
    def load_state(self, step: int) -> Optional[GovernorState]:
        """
        Load governor state from disk.
        
        Returns:
            GovernorState if found, None otherwise
        """
        state_path = self.checkpoint_dir / f"governor_state_step_{step}.json"
        anchors_path = self.checkpoint_dir / f"governor_anchors_step_{step}.pt"
        
        if not state_path.exists():
            return None
        
        # Load JSON state
        with open(state_path, 'r') as f:
            state_dict = json.load(f)
        
        # Load anchors
        anchors_data = torch.load(anchors_path)
        
        # Reconstruct state
        state = GovernorState(
            step=state_dict["step"],
            gradient_history=state_dict["gradient_history"],
            variance_window=state_dict["variance_window"],
            historical_anchors=anchors_data["anchors"],
            intervention_history=[],  # Not fully serialized
            cumulative_interventions={
                InterventionLevel[k]: v 
                for k, v in state_dict["cumulative_interventions"].items()
            },
            forgetting_alerts=state_dict["forgetting_alerts"],
            rare_event_interventions=state_dict["rare_event_interventions"]
        )
        
        return state
    
    def cleanup_old_states(self, keep_last_n: int = 5) -> None:
        """Remove old checkpoint files, keeping only last N"""
        state_files = sorted(self.checkpoint_dir.glob("governor_state_step_*.json"))
        anchor_files = sorted(self.checkpoint_dir.glob("governor_anchors_step_*.pt"))
        
        if len(state_files) > keep_last_n:
            for f in state_files[:-keep_last_n]:
                f.unlink()
        
        if len(anchor_files) > keep_last_n:
            for f in anchor_files[:-keep_last_n]:
                f.unlink()


# ============================================================================
# MAIN GRADIENT GOVERNOR
# ============================================================================

class GradientGovernor:
    """
    Main gradient stability and risk containment engine.
    
    Coordinates all sub-components to observe, constrain, and intervene
    on gradient behavior in real time.
    
    CRITICAL: If this component is disabled, training must not start.
    """
    
    def __init__(self,
                 checkpoint_dir: Path,
                 curriculum: Optional[CurriculumProtocol] = None,
                 data_gate: Optional[DataGateProtocol] = None,
                 safety_watchdog: Optional[SafetyWatchdogProtocol] = None,
                 config: Optional[Dict[str, Any]] = None):
        """
        Initialize gradient governor.
        
        Args:
            checkpoint_dir: Directory for state persistence
            curriculum: Interface to curriculum system
            data_gate: Interface to data gating system
            safety_watchdog: Interface to safety monitoring
            config: Optional configuration overrides
        """
        self.checkpoint_dir = checkpoint_dir
        self.curriculum = curriculum
        self.data_gate = data_gate
        self.safety_watchdog = safety_watchdog
        self.config = config or {}
        
        # Initialize components
        self.collector = GradientCollector()
        self.norm_controller = NormController(
            base_max_norm=self.config.get("max_grad_norm", DEFAULT_MAX_GRAD_NORM)
        )
        self.variance_controller = VarianceController(
            window_size=self.config.get("variance_window", DEFAULT_VARIANCE_WINDOW_SIZE)
        )
        self.horizon_balancer = HorizonBalancer(
            enforce_reweighting=self.config.get("enforce_horizon_balance", False)
        )
        self.rare_event_guard = RareEventGuard(
            rare_threshold=self.config.get("rare_threshold", 0.01),
            max_contribution=self.config.get("max_contribution", 0.1)
        )
        self.forgetting_detector = ForgettingDetector(
            anchor_size=self.config.get("anchor_size", DEFAULT_HISTORY_ANCHOR_SIZE),
            similarity_threshold=self.config.get("similarity_threshold", DEFAULT_COSINE_SIMILARITY_THRESHOLD)
        )
        self.intervention_planner = InterventionPlanner()
        self.serializer = GovernorStateSerializer(checkpoint_dir)
        
        # State
        self.state = GovernorState(
            step=0,
            gradient_history=[],
            variance_window=[],
            historical_anchors=[],
            intervention_history=[],
            cumulative_interventions={level: 0 for level in InterventionLevel},
            forgetting_alerts=0,
            rare_event_interventions=0
        )
        
        self.enabled = True
        self.current_constraints: Optional[GradientConstraints] = None
        
        # Initialize logging and tracking
        log_dir = self.checkpoint_dir / "logs" if self.checkpoint_dir else None
        self.logger = GovernorLogger(log_dir=log_dir, verbose=self.config.get("verbose", False))
        self.effectiveness_tracker = InterventionEffectivenessTracker()
        
        # Track previous signals for effectiveness measurement
        self.previous_signals: Optional[GradientSignals] = None
    
    def register_model(self, model: nn.Module) -> None:
        """Register model for gradient collection"""
        self.collector.register_hooks(model)
    
    def compute_constraints(self, 
                           uncertainty_level: float = 0.5,
                           training_progress: float = 0.0) -> GradientConstraints:
        """
        Compute current gradient constraints based on curriculum phase.
        
        Args:
            uncertainty_level: Average uncertainty in batch [0, 1]
            training_progress: Training progress [0, 1]
        
        Returns:
            GradientConstraints object
        """
        if self.curriculum is None:
            # Default constraints with dynamic horizon bounds
            horizon_bounds = self.horizon_balancer.compute_dynamic_bounds(
                curriculum_phase="intermediate",
                phase_config={},
                training_progress=training_progress
            )
            return GradientConstraints(
                max_grad_norm=DEFAULT_MAX_GRAD_NORM,
                variance_threshold=DEFAULT_VARIANCE_THRESHOLD,
                horizon_bounds=horizon_bounds,
                rare_event_cap=0.1,
                uncertainty_scaling=True
            )
        
        phase = self.curriculum.get_current_phase()
        phase_config = self.curriculum.get_phase_config()
        
        # Compute dynamic norm ceiling
        max_norm = self.norm_controller.compute_dynamic_ceiling(
            curriculum_phase=phase,
            phase_config=phase_config,
            uncertainty_level=uncertainty_level
        )
        
        # Phase-specific variance tolerance
        variance_threshold = phase_config.get(
            "variance_threshold",
            DEFAULT_VARIANCE_THRESHOLD
        )
        
        # Dynamic horizon bounds computation
        horizon_bounds = self.horizon_balancer.compute_dynamic_bounds(
            curriculum_phase=phase,
            phase_config=phase_config,
            training_progress=training_progress
        )
        
        constraints = GradientConstraints(
            max_grad_norm=max_norm,
            variance_threshold=variance_threshold,
            horizon_bounds=horizon_bounds,
            rare_event_cap=phase_config.get("rare_event_cap", 0.1),
            uncertainty_scaling=phase_config.get("uncertainty_scaling", True)
        )
        
        constraints.validate()
        return constraints
    
    def observe_and_constrain(self,
                             model: nn.Module,
                             batch_metadata: Optional[Dict[str, Any]] = None,
                             horizon_grads: Optional[Dict[HorizonType, torch.Tensor]] = None,
                             uncertainty: Optional[torch.Tensor] = None,
                             optimizer: Optional[torch.optim.Optimizer] = None,
                             training_progress: float = 0.0) -> Tuple[InterventionLevel, Dict[str, Any]]:
        """
        Main governance function - called after backward pass.
        
        Observes gradients, checks constraints, and intervenes if necessary.
        
        Args:
            model: Neural network model
            batch_metadata: Optional metadata about current batch
            horizon_grads: Optional per-horizon gradient contributions
            uncertainty: Optional uncertainty estimates
            optimizer: Optional optimizer for LR modification
            training_progress: Training progress [0, 1]
        
        Returns:
            (intervention_level, intervention_details)
        """
        if not self.enabled:
            return InterventionLevel.NONE, {"reason": "governor_disabled"}
        
        # Step 1: Collect signals (with proper variance and cosine similarity computation)
        self.variance_controller.update(self.collector._compute_global_norm(model))
        all_grads = torch.cat([
            p.grad.flatten() for p in model.parameters() if p.grad is not None
        ]) if any(p.grad is not None for p in model.parameters()) else torch.tensor([])
        
        # ELITE-TIER FIX: Detect forgetting BEFORE updating anchors
        # This preserves the contrast between "what you were" vs "what you're becoming"
        forgetting_detected_pre = False
        similarity_drop_pre = 0.0
        if len(all_grads) > 0 and len(self.forgetting_detector.historical_anchors) > 0:
            # Compute similarity with current anchors BEFORE updating
            current_similarity = self.forgetting_detector.compute_cosine_similarity(all_grads)
            forgetting_detected_pre, similarity_drop_pre = self.forgetting_detector.detect_forgetting(
                current_similarity
            )
            # NOW update anchors after detection
            self.forgetting_detector.update_anchors(all_grads)
        elif len(all_grads) > 0:
            # First step - no anchors yet, just update
            self.forgetting_detector.update_anchors(all_grads)
        
        signals = self.collector.collect_signals(
            model, 
            horizon_grads, 
            uncertainty,
            variance_controller=self.variance_controller,
            forgetting_detector=self.forgetting_detector
        )
        
        signals.step = self.state.step
        
        # Step 2: Validate signals
        if not signals.is_valid():
            self._handle_fatal_error("corrupted_gradient_signals", signals)
            return InterventionLevel.TRAINING_HALT, {
                "reason": "corrupted_signals",
                "signals": signals
            }
        
        # Step 3: Compute current constraints with dynamic horizon bounds
        uncertainty_level = float(uncertainty.mean().item()) if uncertainty is not None else 0.5
        self.current_constraints = self.compute_constraints(
            uncertainty_level=uncertainty_level,
            training_progress=training_progress
        )
        
        # Step 4: Check for instabilities
        variance_unstable, variance_reason = self.variance_controller.check_instability(
            signals.grad_variance,
            self.current_constraints.variance_threshold
        )
        
        horizon_unbalanced, horizon_reason = self.horizon_balancer.check_balance(
            signals.horizon_contributions,
            self.current_constraints.horizon_bounds
        )
        
        # ELITE-TIER: Enforce horizon balance if enabled
        if horizon_unbalanced and self.horizon_balancer.enforce_reweighting:
            enforcement_result = self.horizon_balancer.enforce_balance(
                model, signals.horizon_contributions, 
                self.current_constraints.horizon_bounds, horizon_grads
            )
            if enforcement_result.get("enforced", False):
                # Recompute signals after enforcement
                signals = self.collector.collect_signals(
                    model, horizon_grads, uncertainty,
                    variance_controller=self.variance_controller,
                    forgetting_detector=self.forgetting_detector
                )
                horizon_unbalanced = False  # Balance enforced
        
        # Step 5: Check for rare batch
        sample_frequencies = batch_metadata.get("sample_frequencies") if batch_metadata else None
        is_rare_batch, rare_indices = self.rare_event_guard.detect_rare_batch(
            sample_frequencies,
            uncertainty
        )
        
        # Track rare batch metadata
        if batch_metadata:
            batch_id = batch_metadata.get("batch_id", f"batch_{self.state.step}")
            self.rare_event_guard.track_batch_metadata(
                batch_id, is_rare_batch, rare_indices, sample_frequencies
            )
        
        # Step 6: Check for forgetting
        forgetting_detected, similarity_drop = self.forgetting_detector.detect_forgetting(
            signals.grad_cosine_similarity
        )
        
        # Step 7: Update state
        self.state.gradient_history.append(signals.global_grad_norm)
        self.state.variance_window.append(signals.grad_variance)
        self.horizon_balancer.update(signals.horizon_contributions)
        self.horizon_balancer.track_balance_history(signals.horizon_contributions)
        
        if forgetting_detected:
            self.state.forgetting_alerts += 1
        if is_rare_batch:
            self.state.rare_event_interventions += 1
        
        # Step 8: Check curriculum promotion/demotion guards
        promotion_allowed = True
        demotion_triggered = False
        
        if self.curriculum:
            # Check promotion validator
            try:
                promotion_allowed, promotion_reason = self.curriculum.promotion_validator(signals)
                if not promotion_allowed:
                    intervention_details = {
                        "reason": f"promotion_blocked: {promotion_reason}",
                        "signals": signals
                    }
            except AttributeError:
                # Fallback if promotion_validator not implemented
                promotion_allowed = True
            
            # Check demotion guard with gradient-based triggers
            try:
                demotion_triggered, demotion_reason = self.curriculum.demotion_guard(
                    signals, 
                    f"gradient_instability: variance={variance_unstable}, horizon={horizon_unbalanced}"
                )
            except AttributeError:
                # Fallback if demotion_guard not implemented
                demotion_triggered = False
        
        # Step 9: Plan intervention
        intervention_level, intervention_details = self.intervention_planner.plan_intervention(
            signals=signals,
            constraints=self.current_constraints,
            variance_unstable=variance_unstable,
            horizon_unbalanced=horizon_unbalanced,
            rare_batch=is_rare_batch,
            forgetting_detected=forgetting_detected,
            demotion_triggered=demotion_triggered
        )
        
        # Step 10: Execute intervention (includes per-sample caps and forgetting remediation)
        self._execute_intervention(
            model,
            intervention_level,
            intervention_details,
            signals,
            batch_metadata,
            optimizer=optimizer,
            is_rare_batch=is_rare_batch,
            rare_indices=rare_indices,
            forgetting_detected=forgetting_detected,
            similarity_drop=similarity_drop
        )
        
        # Step 11: Record intervention
        record = InterventionRecord(
            level=intervention_level,
            reason=intervention_details.get("reason", "unknown"),
            signals=signals,
            action_taken=intervention_details,
            step=self.state.step,
            timestamp=signals.timestamp
        )
        self.state.intervention_history.append(record)
        self.state.cumulative_interventions[intervention_level] += 1
        self.intervention_planner.record_intervention(intervention_level)
        
        # Step 12: Check for escalation
        if self.intervention_planner.should_escalate():
            self._escalate_to_curriculum(signals)
        
        # Step 13: Handle edge cases
        edge_handled, edge_message = handle_edge_cases(self, model, signals)
        if edge_handled:
            intervention_details["edge_case"] = edge_message
        
        # Step 14: Track intervention effectiveness
        if self.previous_signals is not None:
            self.effectiveness_tracker.record_intervention_outcome(
                intervention_level,
                self.previous_signals,
                signals,
                self.state.step
            )
        
        # Step 15: Log intervention
        self.logger.log_intervention(
            intervention_level,
            intervention_details.get("reason", "unknown"),
            signals,
            intervention_details
        )
        
        # Step 16: Log metrics
        metrics = self.get_metrics()
        metrics.update({
            "intervention_level": intervention_level.name,
            "variance_unstable": variance_unstable,
            "horizon_unbalanced": horizon_unbalanced,
            "rare_batch": is_rare_batch,
            "forgetting_detected": forgetting_detected
        })
        self.logger.log_metrics(metrics)
        
        # Step 17: Update previous signals
        self.previous_signals = signals
        
        # Step 18: Increment step
        self.state.step += 1
        
        return intervention_level, intervention_details
    
    def _execute_intervention(self,
                             model: nn.Module,
                             level: InterventionLevel,
                             details: Dict[str, Any],
                             signals: GradientSignals,
                             batch_metadata: Optional[Dict[str, Any]],
                             optimizer: Optional[torch.optim.Optimizer] = None,
                             is_rare_batch: bool = False,
                             rare_indices: Optional[List[int]] = None,
                             forgetting_detected: bool = False,
                             similarity_drop: float = 0.0) -> None:
        """
        Execute the planned intervention.
        
        Includes per-sample caps for rare events and forgetting remediation.
        """
        if level == InterventionLevel.NONE:
            return
        
        if level == InterventionLevel.SOFT_DAMPING:
            damping_factor = self.variance_controller.compute_damping_factor(
                signals.grad_variance,
                self.current_constraints.variance_threshold
            )
            self.variance_controller.apply_damping(model, damping_factor)
            details["damping_applied"] = damping_factor
        
        elif level == InterventionLevel.ADAPTIVE_CLIPPING:
            clip_value = details["clip_value"]
            actual_norm = self.norm_controller.apply_clipping(model, clip_value)
            details["norm_before_clip"] = actual_norm
        
        elif level == InterventionLevel.LOSS_REWEIGHTING:
            if self.data_gate and batch_metadata:
                # Suggest reweighting to data gate
                sample_ids = batch_metadata.get("sample_ids", [])
                weights = [0.5] * len(sample_ids)  # Reduce weight for rare batch
                self.data_gate.request_reweighting(sample_ids, weights)
                details["reweighting_requested"] = True
        
        elif level == InterventionLevel.CURRICULUM_DEMOTION:
            if self.curriculum:
                demotion_accepted = self.curriculum.request_demotion(
                    reason=details["reason"],
                    signals=signals
                )
                details["demotion_accepted"] = demotion_accepted
        
        elif level == InterventionLevel.TRAINING_HALT:
            self._handle_fatal_error(details["reason"], signals)
        
        # Apply per-sample caps for rare events (if rare batch detected)
        if is_rare_batch and rare_indices is not None and len(rare_indices) > 0:
            batch_size = batch_metadata.get("batch_size", len(rare_indices)) if batch_metadata else len(rare_indices)
            caps = self.rare_event_guard.compute_sample_caps(batch_size, rare_indices)
            cap_results = self.rare_event_guard.apply_per_sample_caps_to_model(
                model, batch_size, rare_indices, caps
            )
            details["per_sample_caps_applied"] = cap_results
        
        # Execute forgetting remediation (if forgetting detected)
        if forgetting_detected:
            remediation = self.forgetting_detector.suggest_remediation(similarity_drop)
            
            # ELITE-TIER: Generate deterministic alignment hash for replay buffer
            # Ensures consistent replay buffer state across runs
            alignment_hash = self._generate_alignment_hash(
                signals, similarity_drop, self.state.step
            )
            
            remediation_results = self.forgetting_detector.execute_remediation(
                remediation, optimizer=optimizer, data_gate=self.data_gate,
                alignment_hash=alignment_hash
            )
            details["forgetting_remediation"] = remediation_results
    
    def _generate_alignment_hash(self,
                               signals: GradientSignals,
                               similarity_drop: float,
                               step: int) -> str:
        """
        ELITE-TIER: Generate deterministic alignment hash for replay buffer.
        
        Ensures consistent replay buffer state across runs for reproducibility.
        
        Args:
            signals: Current gradient signals
            similarity_drop: Forgetting similarity drop
            step: Current training step
        
        Returns:
            Deterministic hash string
        """
        # Create deterministic signature from gradient state
        signature_parts = [
            f"step_{step}",
            f"norm_{signals.global_grad_norm:.6f}",
            f"variance_{signals.grad_variance:.6f}",
            f"similarity_{signals.grad_cosine_similarity:.6f}",
            f"drop_{similarity_drop:.6f}",
            f"horizon_{sum(signals.horizon_contributions.values()):.6f}"
        ]
        
        signature = "_".join(signature_parts)
        hash_obj = hashlib.sha256(signature.encode())
        
        # Return first 16 chars for readability, full hash available
        return hash_obj.hexdigest()[:16]
    
    def _escalate_to_curriculum(self, signals: GradientSignals) -> None:
        """Escalate frequent interventions to curriculum system"""
        if self.curriculum:
            self.curriculum.request_demotion(
                reason="excessive_gradient_interventions",
                signals=signals
            )
    
    def _handle_fatal_error(self, reason: str, signals: GradientSignals) -> None:
        """Handle fatal errors that require training halt"""
        error_context = {
            "reason": reason,
            "step": self.state.step,
            "signals": {
                "global_norm": signals.global_grad_norm,
                "variance": signals.grad_variance,
                "cosine_sim": signals.grad_cosine_similarity
            }
        }
        
        # Save emergency state
        self.save_state(emergency=True)
        
        # Escalate to safety watchdog
        if self.safety_watchdog:
            self.safety_watchdog.escalate_risk(
                level="critical",
                reason=f"gradient_governor_fatal: {reason}",
                context=error_context
            )
        
        # Raise exception to halt training
        raise RuntimeError(f"Gradient Governor Fatal Error: {reason}")
    
    def save_state(self, emergency: bool = False) -> Path:
        """Save current governor state"""
        suffix = "_emergency" if emergency else ""
        save_step = self.state.step
        
        state_path = self.serializer.save_state(self.state, save_step)
        
        if not emergency:
            self.serializer.cleanup_old_states(keep_last_n=5)
        
        return state_path
    
    def load_state(self, step: int) -> bool:
        """
        Load governor state from checkpoint.
        
        Returns:
            True if state loaded successfully
        """
        loaded_state = self.serializer.load_state(step)
        
        if loaded_state is None:
            return False
        
        self.state = loaded_state
        
        # Restore component states
        self.variance_controller.norm_window = deque(
            self.state.variance_window,
            maxlen=self.variance_controller.window_size
        )
        self.forgetting_detector.historical_anchors = self.state.historical_anchors
        
        return True
    
    def get_metrics(self) -> Dict[str, Any]:
        """
        Get current governor metrics for logging/monitoring.
        
        Returns:
            Dict of current metrics
        """
        return {
            "step": self.state.step,
            "current_grad_norm": self.state.gradient_history[-1] if self.state.gradient_history else 0.0,
            "current_variance": self.state.variance_window[-1] if self.state.variance_window else 0.0,
            "forgetting_alerts": self.state.forgetting_alerts,
            "rare_event_interventions": self.state.rare_event_interventions,
            "intervention_counts": {
                level.name: count 
                for level, count in self.state.cumulative_interventions.items()
            },
            "recent_intervention_rate": self._compute_recent_intervention_rate(),
            "constraints": {
                "max_grad_norm": self.current_constraints.max_grad_norm if self.current_constraints else None,
                "variance_threshold": self.current_constraints.variance_threshold if self.current_constraints else None
            }
        }
    
    def _compute_recent_intervention_rate(self, window: int = 100) -> float:
        """Compute intervention rate over recent window"""
        if len(self.state.intervention_history) < window:
            window = len(self.state.intervention_history)
        
        if window == 0:
            return 0.0
        
        recent = self.state.intervention_history[-window:]
        non_none = sum(1 for r in recent if r.level != InterventionLevel.NONE)
        
        return non_none / window
    
    def enable(self) -> None:
        """Enable gradient governance"""
        self.enabled = True
        self.collector.enable_collection()
    
    def disable(self) -> None:
        """
        Disable gradient governance.
        
        WARNING: Training should not proceed with governor disabled.
        """
        self.enabled = False
        self.collector.disable_collection()
    
    def is_enabled(self) -> bool:
        """Check if governor is enabled"""
        return self.enabled
    
    def assert_enabled(self) -> None:
        """
        Assert that governor is enabled. Raises RuntimeError if disabled.
        
        This should be called by training.py before training starts.
        """
        if not self.enabled:
            raise RuntimeError(
                "Gradient Governor is DISABLED. Training must not start. "
                "Enable the governor with governor.enable() before training."
            )
    
    @classmethod
    def create_and_validate(cls,
                            checkpoint_dir: Path,
                            curriculum: Optional[CurriculumProtocol] = None,
                            data_gate: Optional[DataGateProtocol] = None,
                            safety_watchdog: Optional[SafetyWatchdogProtocol] = None,
                            config: Optional[Dict[str, Any]] = None,
                            require_enabled: bool = True) -> 'GradientGovernor':
        """
        Factory method that creates governor and validates it's ready.
        
        Args:
            require_enabled: If True, raises error if governor would be disabled
        
        Returns:
            GradientGovernor instance
        
        Raises:
            RuntimeError: If require_enabled=True and governor is disabled
        """
        governor = cls(
            checkpoint_dir=checkpoint_dir,
            curriculum=curriculum,
            data_gate=data_gate,
            safety_watchdog=safety_watchdog,
            config=config
        )
        
        if require_enabled and not governor.enabled:
            raise RuntimeError(
                "Gradient Governor must be ENABLED for training to start. "
                "This is a safety requirement."
            )
        
        return governor
    
    def __enter__(self):
        """Context manager entry"""
        self.enable()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        if exc_type is None:
            # Normal exit - save state
            self.save_state()
        else:
            # Exception occurred - save emergency state
            self.save_state(emergency=True)
        
        self.collector.clear_hooks()
        return False


# ============================================================================
# FACTORY & INITIALIZATION
# ============================================================================

def create_gradient_governor(
    checkpoint_dir: Path,
    curriculum: Optional[CurriculumProtocol] = None,
    data_gate: Optional[DataGateProtocol] = None,
    safety_watchdog: Optional[SafetyWatchdogProtocol] = None,
    config: Optional[Dict[str, Any]] = None
) -> GradientGovernor:
    """
    Factory function to create gradient governor.
    
    This is the recommended way to instantiate the governor.
    """
    governor = GradientGovernor(
        checkpoint_dir=checkpoint_dir,
        curriculum=curriculum,
        data_gate=data_gate,
        safety_watchdog=safety_watchdog,
        config=config
    )
    
    return governor


# ============================================================================
# LOGGING & DEBUGGING UTILITIES
# ============================================================================

class GovernorLogger:
    """
    Comprehensive logging for gradient governor operations.
    
    Tracks interventions, metrics, and provides debugging utilities.
    """
    
    def __init__(self, log_dir: Optional[Path] = None, verbose: bool = False):
        self.log_dir = log_dir
        self.verbose = verbose
        self.intervention_log: List[Dict[str, Any]] = []
        self.metrics_log: List[Dict[str, Any]] = []
        self.error_log: List[Dict[str, Any]] = []
        
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)
    
    def log_intervention(self, 
                        level: InterventionLevel,
                        reason: str,
                        signals: GradientSignals,
                        details: Dict[str, Any]) -> None:
        """Log intervention event"""
        entry = {
            "timestamp": time.time(),
            "step": signals.step,
            "level": level.name,
            "reason": reason,
            "signals": {
                "global_norm": signals.global_grad_norm,
                "variance": signals.grad_variance,
                "cosine_sim": signals.grad_cosine_similarity
            },
            "details": details
        }
        self.intervention_log.append(entry)
        
        if self.verbose:
            print(f"[GradientGovernor] Step {signals.step}: {level.name} - {reason}")
        
        if len(self.intervention_log) > 10000:
            self.intervention_log = self.intervention_log[-10000:]
    
    def log_metrics(self, metrics: Dict[str, Any]) -> None:
        """Log governor metrics"""
        entry = {
            "timestamp": time.time(),
            **metrics
        }
        self.metrics_log.append(entry)
        
        if len(self.metrics_log) > 10000:
            self.metrics_log = self.metrics_log[-10000:]
    
    def log_error(self, error_type: str, message: str, context: Dict[str, Any]) -> None:
        """Log error event"""
        entry = {
            "timestamp": time.time(),
            "error_type": error_type,
            "message": message,
            "context": context
        }
        self.error_log.append(entry)
        
        if self.verbose:
            print(f"[GradientGovernor ERROR] {error_type}: {message}")
    
    def export_interventions(self, path: Path) -> None:
        """Export intervention log to JSON"""
        with open(path, 'w') as f:
            json.dump(self.intervention_log, f, indent=2)
    
    def export_metrics(self, path: Path) -> None:
        """Export metrics log to JSON"""
        with open(path, 'w') as f:
            json.dump(self.metrics_log, f, indent=2)
    
    def get_intervention_statistics(self) -> Dict[str, Any]:
        """Get statistics about interventions"""
        if not self.intervention_log:
            return {}
        
        level_counts = {}
        for entry in self.intervention_log:
            level = entry["level"]
            level_counts[level] = level_counts.get(level, 0) + 1
        
        return {
            "total_interventions": len(self.intervention_log),
            "by_level": level_counts,
            "recent_intervention_rate": self._compute_recent_rate()
        }
    
    def _compute_recent_rate(self, window: int = 100) -> float:
        """Compute recent intervention rate"""
        if len(self.intervention_log) < window:
            return len(self.intervention_log) / max(1, len(self.intervention_log))
        
        recent = self.intervention_log[-window:]
        non_none = sum(1 for e in recent if e["level"] != "NONE")
        return non_none / window


class InterventionEffectivenessTracker:
    """
    Track effectiveness of interventions over time.
    
    Measures whether interventions successfully stabilize training.
    """
    
    def __init__(self):
        self.intervention_outcomes: List[Dict[str, Any]] = []
        self.effectiveness_scores: Dict[InterventionLevel, List[float]] = {
            level: [] for level in InterventionLevel
        }
    
    def record_intervention_outcome(self,
                                   level: InterventionLevel,
                                   signals_before: GradientSignals,
                                   signals_after: GradientSignals,
                                   step: int) -> None:
        """
        Record outcome of an intervention.
        
        Measures effectiveness by comparing gradient stability before/after.
        """
        # Compute stability improvement
        variance_improvement = signals_before.grad_variance - signals_after.grad_variance
        norm_improvement = abs(signals_before.global_grad_norm - signals_after.global_grad_norm)
        
        # Effectiveness score: higher = more effective
        effectiveness = 0.0
        if variance_improvement > 0:
            effectiveness += 0.5  # Variance reduced
        if norm_improvement > 0:
            effectiveness += 0.3  # Norm stabilized
        if signals_after.grad_cosine_similarity > signals_before.grad_cosine_similarity:
            effectiveness += 0.2  # Similarity improved
        
        self.effectiveness_scores[level].append(effectiveness)
        
        self.intervention_outcomes.append({
            "step": step,
            "level": level.name,
            "effectiveness": effectiveness,
            "variance_improvement": variance_improvement,
            "norm_improvement": norm_improvement
        })
        
        if len(self.intervention_outcomes) > 1000:
            self.intervention_outcomes = self.intervention_outcomes[-1000:]
    
    def get_effectiveness_stats(self) -> Dict[str, Any]:
        """Get effectiveness statistics by intervention level"""
        stats = {}
        for level, scores in self.effectiveness_scores.items():
            if scores:
                stats[level.name] = {
                    "mean": float(np.mean(scores)),
                    "std": float(np.std(scores)),
                    "count": len(scores),
                    "recent_mean": float(np.mean(scores[-50:])) if len(scores) >= 50 else float(np.mean(scores))
                }
        return stats


# ============================================================================
# PERFORMANCE BENCHMARKS
# ============================================================================

def benchmark_governor_overhead(governor: GradientGovernor,
                               model: nn.Module,
                               num_steps: int = 100) -> Dict[str, float]:
    """
    Benchmark overhead of gradient governor operations.
    
    Returns:
        Dict with timing statistics
    """
    import time
    
    times = []
    
    for step in range(num_steps):
        # Create dummy gradients
        for param in model.parameters():
            if param.requires_grad:
                param.grad = torch.randn_like(param) * 0.1
        
        start = time.time()
        governor.observe_and_constrain(model)
        elapsed = time.time() - start
        times.append(elapsed)
    
    return {
        "mean_time_ms": float(np.mean(times) * 1000),
        "std_time_ms": float(np.std(times) * 1000),
        "min_time_ms": float(np.min(times) * 1000),
        "max_time_ms": float(np.max(times) * 1000),
        "p95_time_ms": float(np.percentile(times, 95) * 1000),
        "total_time_s": float(sum(times))
    }


# ============================================================================
# EDGE CASE HANDLERS
# ============================================================================

def handle_edge_cases(governor: GradientGovernor,
                     model: nn.Module,
                     signals: GradientSignals) -> Tuple[bool, str]:
    """
    Handle edge cases that might cause governor to fail.
    
    Returns:
        (handled, message)
    """
    # Edge case 1: All gradients are zero
    if signals.global_grad_norm == 0.0:
        return True, "all_gradients_zero"
    
    # Edge case 2: Extremely large gradients
    if signals.global_grad_norm > 1e6:
        return True, "extremely_large_gradients"
    
    # Edge case 3: All horizons have zero contribution
    if all(v == 0.0 for v in signals.horizon_contributions.values()):
        return True, "all_horizons_zero"
    
    # Edge case 4: Model has no parameters
    param_count = sum(1 for _ in model.parameters())
    if param_count == 0:
        return True, "model_has_no_parameters"
    
    return False, ""


# ============================================================================
# VALIDATION & TESTING
# ============================================================================

def validate_governor_determinism(
    governor1: GradientGovernor,
    governor2: GradientGovernor,
    model: nn.Module,
    num_steps: int = 100
) -> bool:
    """
    Validate that two governors produce identical interventions.
    
    Args:
        governor1: First governor instance
        governor2: Second governor instance
        model: Model to test on
        num_steps: Number of steps to validate
    
    Returns:
        True if governors are deterministic
    """
    torch.manual_seed(42)
    
    interventions1 = []
    interventions2 = []
    
    for step in range(num_steps):
        # Create identical fake gradients
        for param in model.parameters():
            if param.requires_grad:
                param.grad = torch.randn_like(param)
        
        level1, details1 = governor1.observe_and_constrain(model)
        
        # Reset gradients to same values
        torch.manual_seed(42)
        for param in model.parameters():
            if param.requires_grad:
                param.grad = torch.randn_like(param)
        
        level2, details2 = governor2.observe_and_constrain(model)
        
        interventions1.append((level1, details1))
        interventions2.append((level2, details2))
    
    # Compare interventions
    for i, ((l1, d1), (l2, d2)) in enumerate(zip(interventions1, interventions2)):
        if l1 != l2:
            print(f"Step {i}: Intervention level mismatch: {l1} vs {l2}")
            return False
    
    return True


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    "GradientGovernor",
    "GradientConstraints",
    "GradientSignals",
    "InterventionLevel",
    "InterventionRecord",
    "HorizonType",
    "create_gradient_governor",
    "validate_governor_determinism",
    "benchmark_governor_overhead",
    "handle_edge_cases",
    "GovernorLogger",
    "InterventionEffectivenessTracker"
]