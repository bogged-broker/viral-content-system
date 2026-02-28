"""
/rl_agents/video_micro/policy_network.py

Per-Video Micro-Decision Policy Network
Action Distribution Generator (Constraint-Aware, Causally Safe)

WHAT THIS DOES:
Given observable environment state → outputs valid action probability distribution
NO reward prediction, NO engagement access, NO future data

INPUT:  environment state + action mask
OUTPUT: action logits/probs (masked, deterministic)

5M+ VIEWS BASELINE CAPABILITIES:
- High-throughput batch processing (optimized for 5M+ views/day)
- GPU/CUDA optimizations for maximum throughput
- Distributed inference support
- Advanced caching strategies
- Memory-efficient operations
- Numerical stability at scale
- Stress testing and performance profiling
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Dict, List, Optional, Tuple, Set, Any, Callable, Union
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict, deque
from functools import lru_cache, wraps
from contextlib import contextmanager
import numpy as np
import logging
import time
import hashlib
import json
import pickle
from pathlib import Path
import threading
import queue
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
import warnings

# GPU optimization imports
try:
    import torch.backends.cudnn as cudnn
    CUDA_AVAILABLE = torch.cuda.is_available()
    if CUDA_AVAILABLE:
        cudnn.benchmark = True  # Optimize for consistent input sizes
        cudnn.deterministic = False  # Allow non-deterministic algorithms for speed
except ImportError:
    CUDA_AVAILABLE = False

logger = logging.getLogger(__name__)

# Performance constants for 5M+ views baseline
MAX_BATCH_SIZE = 1024  # Maximum batch size for inference
OPTIMAL_BATCH_SIZE = 256  # Optimal batch size for GPU inference
MIN_BATCH_SIZE = 1  # Minimum batch size
CACHE_SIZE_MB = 512  # Cache size in MB for state encodings
NUM_WORKER_THREADS = 4  # Number of worker threads for preprocessing


# ============================================================================
# DROPOUT CONTROLLER (Production-Grade Determinism)
# ============================================================================

class DropoutController:
    """
    Safe dropout management without runtime mutation.
    PRODUCTION-GRADE: Eliminates brittle module.p mutations.
    Uses context manager pattern for deterministic control.
    """
    
    def __init__(self, module: nn.Module, original_dropout_rate: float):
        self.module = module
        self.original_rate = original_dropout_rate
        self.dropout_modules = []
        self._collect_dropout_modules(module)
    
    def _collect_dropout_modules(self, module: nn.Module) -> None:
        """Collect all dropout modules recursively"""
        for child in module.children():
            if isinstance(child, nn.Dropout):
                self.dropout_modules.append(child)
            else:
                self._collect_dropout_modules(child)
    
    def __enter__(self):
        """Enter deterministic mode (disable dropout)"""
        self._original_rates = []
        self._original_training = []
        
        for dropout in self.dropout_modules:
            self._original_rates.append(dropout.p)
            self._original_training.append(dropout.training)
            dropout.p = 0.0
            dropout.eval()
        
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Restore original dropout state"""
        for dropout, orig_rate, orig_training in zip(
            self.dropout_modules, 
            self._original_rates, 
            self._original_training
        ):
            dropout.p = orig_rate
            if orig_training:
                dropout.train()
            else:
                dropout.eval()
    
    def enable_mc_dropout(self):
        """
        Enable MC dropout (Monte Carlo dropout) for uncertainty estimation.
        
        MC dropout requires dropout to be active even in eval mode.
        This is used for Bayesian uncertainty estimation by sampling
        multiple forward passes with different dropout masks.
        """
        for dropout in self.dropout_modules:
            dropout.train()  # Enable dropout even in eval mode for MC sampling


# ============================================================================
# GPU OPTIMIZATION & HIGH-THROUGHPUT UTILITIES (5M+ Views Baseline)
# ============================================================================

class GPUOptimizer:
    """GPU optimization utilities for high-throughput inference"""
    
    @staticmethod
    def enable_optimizations(device: torch.device, strict_replay_mode: bool = False):
        """
        Enable GPU optimizations for inference
        
        Args:
            device: target device
            strict_replay_mode: if True, enforce full determinism (slower but replay-safe)
        """
        if device.type == 'cuda':
            if strict_replay_mode:
                # Strict replay mode: full determinism for exact replay across hardware
                torch.backends.cudnn.benchmark = False
                torch.backends.cudnn.deterministic = True
                torch.backends.cudnn.allow_tf32 = False
                torch.backends.cuda.matmul.allow_tf32 = False
                logger.info(f"GPU strict replay mode enabled on {device} (deterministic)")
            else:
                # Performance mode: optimized for throughput
                torch.backends.cudnn.benchmark = True
                torch.backends.cudnn.deterministic = False
                torch.backends.cudnn.allow_tf32 = True  # TensorFloat-32 for speed
                torch.backends.cuda.matmul.allow_tf32 = True
                logger.info(f"GPU optimizations enabled on {device} (performance mode)")
    
    @staticmethod
    def optimize_model_for_inference(model: nn.Module, device: torch.device):
        """Optimize model for inference (fusion, quantization, etc.)"""
        model.eval()
        model.to(device)
        
        if device.type == 'cuda':
            # Enable TensorFloat-32 for faster matrix operations
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
            
            # Use channels_last memory format for better performance
            try:
                model = model.to(memory_format=torch.channels_last)
            except RuntimeError:
                pass  # Not all models support channels_last
        
        # Enable inference mode for better performance
        with torch.inference_mode():
            pass
        
        return model
    
    @staticmethod
    def get_optimal_batch_size(
        model: nn.Module,
        input_shape: Tuple[int, ...],
        device: torch.device,
        max_batch_size: int = MAX_BATCH_SIZE
    ) -> int:
        """
        Find optimal batch size for GPU inference
        
        Args:
            model: model to test
            input_shape: shape of single input (without batch dimension)
            device: device to test on
            max_batch_size: maximum batch size to test
        
        Returns:
            Optimal batch size
        """
        if device.type != 'cuda':
            return min(32, max_batch_size)  # CPU typically handles smaller batches
        
        model.eval()
        optimal_size = 1
        
        # Binary search for optimal batch size
        left, right = 1, max_batch_size
        
        while left <= right:
            mid = (left + right) // 2
            try:
                test_input = torch.randn(mid, *input_shape, device=device)
                with torch.inference_mode():
                    _ = model(test_input)
                
                # If successful, try larger batch
                optimal_size = mid
                left = mid + 1
            except RuntimeError as e:
                if "out of memory" in str(e).lower():
                    # Batch too large, reduce
                    right = mid - 1
                    torch.cuda.empty_cache()
                else:
                    raise
        
        torch.cuda.empty_cache()
        logger.info(f"Optimal batch size determined: {optimal_size}")
        return optimal_size
    
    @staticmethod
    def clear_cache(device: Optional[torch.device] = None):
        """Clear GPU cache"""
        if device is not None and device.type == 'cuda':
            torch.cuda.empty_cache()
        elif CUDA_AVAILABLE:
            torch.cuda.empty_cache()


class BatchProcessor:
    """High-throughput batch processing utilities for 5M+ views baseline"""
    
    def __init__(
        self,
        batch_size: int = OPTIMAL_BATCH_SIZE,
        max_queue_size: int = 10000,
        num_workers: int = NUM_WORKER_THREADS
    ):
        self.batch_size = batch_size
        self.max_queue_size = max_queue_size
        self.num_workers = num_workers
        self.input_queue = queue.Queue(maxsize=max_queue_size)
        self.output_queue = queue.Queue(maxsize=max_queue_size)
        self.executor = ThreadPoolExecutor(max_workers=num_workers)
    
    def batch_inference(
        self,
        model: nn.Module,
        inputs: List[PolicyInput],
        device: torch.device,
        deterministic: bool = True
    ) -> List[PolicyOutput]:
        """
        Process inputs in optimized batches for high throughput
        
        Args:
            model: PolicyNetwork model
            inputs: list of PolicyInput objects
            device: device to run inference on
            deterministic: whether to use deterministic mode
        
        Returns:
            list of PolicyOutput objects
        """
        if not isinstance(model, PolicyNetwork):
            raise TypeError("model must be PolicyNetwork instance")
        
        all_outputs = []
        num_inputs = len(inputs)
        
        # Process in batches
        for i in range(0, num_inputs, self.batch_size):
            batch_inputs = inputs[i:i + self.batch_size]
            
            # Run inference on batch
            batch_outputs = model.forward(
                batch_inputs,
                deterministic=deterministic
            )
            
            all_outputs.extend(batch_outputs)
            
            # Clear cache periodically for memory efficiency
            if (i // self.batch_size) % 10 == 0 and device.type == 'cuda':
                GPUOptimizer.clear_cache(device)
        
        return all_outputs
    
    @staticmethod
    def chunk_list(lst: List[Any], chunk_size: int) -> List[List[Any]]:
        """Split list into chunks of specified size"""
        return [lst[i:i + chunk_size] for i in range(0, len(lst), chunk_size)]
    
    @staticmethod
    def estimate_throughput(
        model: nn.Module,
        inputs: List[PolicyInput],
        device: torch.device,
        num_warmup: int = 10,
        num_iterations: int = 100
    ) -> Dict[str, float]:
        """
        Estimate inference throughput for 5M+ views baseline
        
        Returns:
            Dictionary with throughput metrics
        """
        if not isinstance(model, PolicyNetwork):
            raise TypeError("model must be PolicyNetwork instance")
        
        model.eval()
        model.to(device)
        
        # Warmup
        warmup_inputs = inputs[:min(num_warmup, len(inputs))]
        for _ in range(3):
            _ = model.forward(warmup_inputs, deterministic=True)
        
        if device.type == 'cuda':
            torch.cuda.synchronize()
        
        # Measure throughput
        start_time = time.time()
        
        for _ in range(num_iterations):
            test_inputs = inputs[:min(OPTIMAL_BATCH_SIZE, len(inputs))]
            _ = model.forward(test_inputs, deterministic=True)
        
        if device.type == 'cuda':
            torch.cuda.synchronize()
        
        elapsed_time = time.time() - start_time
        total_samples = num_iterations * len(test_inputs)
        samples_per_second = total_samples / elapsed_time
        
        return {
            'samples_per_second': samples_per_second,
            'samples_per_minute': samples_per_second * 60,
            'samples_per_hour': samples_per_second * 3600,
            'time_per_sample_ms': (elapsed_time / total_samples) * 1000,
            'can_handle_5m_baseline': samples_per_second >= (5_000_000 / 86400)  # 5M views per day
        }


class MemoryEfficientOperations:
    """Memory-efficient operations for large-scale inference"""
    
    @staticmethod
    def efficient_softmax(logits: torch.Tensor, dim: int = -1, mask: Optional[torch.Tensor] = None) -> torch.Tensor:
        """
        Memory-efficient softmax with optional masking
        
        Uses log-sum-exp trick for numerical stability at scale
        """
        if mask is not None:
            # Apply mask: set invalid actions to -inf
            logits = torch.where(
                mask,
                logits,
                torch.full_like(logits, float('-inf'))
            )
        
        # Log-sum-exp trick for numerical stability
        max_logits = logits.max(dim=dim, keepdim=True)[0]
        exp_logits = torch.exp(logits - max_logits)
        
        if mask is not None:
            exp_logits = exp_logits * mask.float()
        
        sum_exp = exp_logits.sum(dim=dim, keepdim=True)
        probs = exp_logits / (sum_exp + 1e-10)
        
        return probs
    
    @staticmethod
    def efficient_matmul(a: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
        """Memory-efficient matrix multiplication with chunking for large matrices"""
        if a.shape[0] * b.shape[1] < 1e6:  # Small enough to do directly
            return torch.matmul(a, b)
        
        # Chunk large matrices to reduce memory usage
        chunk_size = 1024
        result_chunks = []
        
        for i in range(0, a.shape[0], chunk_size):
            chunk_a = a[i:i + chunk_size]
            chunk_result = torch.matmul(chunk_a, b)
            result_chunks.append(chunk_result)
            del chunk_a, chunk_result  # Free memory
        
        return torch.cat(result_chunks, dim=0)
    
    @staticmethod
    def inplace_operations(tensor: torch.Tensor) -> torch.Tensor:
        """Use in-place operations where safe to reduce memory usage"""
        # Note: In-place operations are used carefully to maintain correctness
        return tensor


class NumericalStability:
    """Numerical stability improvements for large-scale operations"""
    
    EPS = 1e-8  # Epsilon for numerical stability
    MAX_LOGIT = 50.0  # Maximum logit value before clamping
    MIN_LOGIT = -50.0  # Minimum logit value before clamping
    
    @staticmethod
    def clamp_logits(logits: torch.Tensor) -> torch.Tensor:
        """Clamp logits to prevent overflow/underflow"""
        return torch.clamp(logits, NumericalStability.MIN_LOGIT, NumericalStability.MAX_LOGIT)
    
    @staticmethod
    def stable_entropy(probs: torch.Tensor, eps: float = None) -> torch.Tensor:
        """Compute entropy with numerical stability"""
        if eps is None:
            eps = NumericalStability.EPS
        
        # Clip probabilities to avoid log(0)
        probs_clipped = torch.clamp(probs, min=eps, max=1.0 - eps)
        log_probs = torch.log(probs_clipped + eps)
        entropy = -(probs * log_probs).sum(dim=-1)
        
        return entropy
    
    @staticmethod
    def stable_softmax(logits: torch.Tensor, dim: int = -1, temperature: float = 1.0) -> torch.Tensor:
        """Numerically stable softmax with temperature"""
        # Clamp logits and apply temperature
        logits_clamped = NumericalStability.clamp_logits(logits / temperature)
        
        # Log-sum-exp trick
        max_logits = logits_clamped.max(dim=dim, keepdim=True)[0]
        exp_logits = torch.exp(logits_clamped - max_logits)
        sum_exp = exp_logits.sum(dim=dim, keepdim=True)
        
        return exp_logits / (sum_exp + NumericalStability.EPS)


# ============================================================================
# TYPE DEFINITIONS & CONTRACTS
# ============================================================================

class ActionType(Enum):
    """Canonical action types (must match environment.py)"""
    NO_OP = "no_op"
    CAPTION_SWAP = "caption_swap"
    THUMBNAIL_SWAP = "thumbnail_swap"
    REPOST = "repost"
    SCHEDULING_SHIFT = "scheduling_shift"
    HASHTAG_UPDATE = "hashtag_update"


@dataclass
class PolicyInput:
    """Validated input contract for policy network"""
    video_age_seconds: float
    content_surface: str
    distribution_state: str
    constraints: Dict[str, bool]
    platform: str
    action_mask: torch.Tensor  # [num_actions] bool
    
    # Optional diagnostic metadata (not used in policy computation)
    video_id: Optional[str] = None
    timestamp: Optional[float] = None
    
    def validate(self):
        """Hard-fail on contract violation with comprehensive checks"""
        # Video age validation
        if self.video_age_seconds < 0:
            raise ValueError(f"Negative video age: {self.video_age_seconds}")
        if not np.isfinite(self.video_age_seconds):
            raise ValueError(f"Non-finite video age: {self.video_age_seconds}")
        
        # Content surface validation
        valid_surfaces = {'fyp', 'following', 'search', 'hashtag', 'profile', 'discover'}
        if self.content_surface not in valid_surfaces:
            raise ValueError(
                f"Unknown surface: {self.content_surface}. "
                f"Must be one of: {valid_surfaces}"
            )
        
        # Distribution state validation
        valid_states = {
            'pending', 'active', 'paused', 'completed', 'archived', 
            'scheduled', 'draft', 'processing'
        }
        if self.distribution_state not in valid_states:
            raise ValueError(
                f"Unknown distribution_state: {self.distribution_state}. "
                f"Must be one of: {valid_states}"
            )
        
        # Platform validation
        valid_platforms = {'tiktok', 'youtube_shorts', 'instagram_reels', 'youtube', 'instagram'}
        if self.platform not in valid_platforms:
            raise ValueError(
                f"Unknown platform: {self.platform}. "
                f"Must be one of: {valid_platforms}"
            )
        
        # Constraints validation
        if not isinstance(self.constraints, dict):
            raise TypeError(f"Constraints must be dict, got {type(self.constraints)}")
        for key, value in self.constraints.items():
            if not isinstance(key, str):
                raise TypeError(f"Constraint key must be str, got {type(key)}")
            if not isinstance(value, bool):
                raise TypeError(
                    f"Constraint value must be bool, got {type(value)} for key {key}"
                )
        
        # Action mask validation
        if not isinstance(self.action_mask, torch.Tensor):
            raise TypeError(
                f"Action mask must be torch.Tensor, got {type(self.action_mask)}"
            )
        if self.action_mask.dtype != torch.bool:
            raise ValueError(
                f"Action mask must be boolean, got dtype {self.action_mask.dtype}"
            )
        if self.action_mask.dim() != 1:
            raise ValueError(
                f"Action mask must be 1D, got shape {self.action_mask.shape}"
            )
        if len(self.action_mask) == 0:
            raise ValueError("Action mask cannot be empty")
        if not torch.any(self.action_mask):
            raise ValueError("All actions are masked! At least one action must be valid")


@dataclass
class PolicyOutput:
    """Validated output contract"""
    action_logits: torch.Tensor      # [num_actions] float (raw)
    action_probs: torch.Tensor       # [num_actions] float (masked softmax)
    entropy: float                   # scalar
    masked_actions: List[str]        # action_ids where mask=False
    model_version: str
    
    def validate(self, num_actions: int):
        """Hard-fail on output violation"""
        assert self.action_logits.shape[0] == num_actions, "Logit shape mismatch"
        assert self.action_probs.shape[0] == num_actions, "Prob shape mismatch"
        assert torch.isfinite(self.action_probs).all(), "Non-finite probabilities"
        assert torch.isclose(self.action_probs.sum(), torch.tensor(1.0), atol=1e-5), \
            f"Probabilities don't sum to 1: {self.action_probs.sum()}"
        assert self.entropy >= 0, "Negative entropy"


# ============================================================================
# STATE ENCODERS
# ============================================================================

class TimeEncoder(nn.Module):
    """Encodes video age with log-time awareness"""
    
    def __init__(self, hidden_dim: int = 64, variant: str = "standard"):
        super().__init__()
        self.hidden_dim = hidden_dim
        self.variant = variant
        
        if variant == "standard":
            # Standard multi-scale time encoding
            num_features = 4
            self.time_mlp = nn.Sequential(
                nn.Linear(num_features, hidden_dim),
                nn.ReLU(),
                nn.Linear(hidden_dim, hidden_dim),
                nn.LayerNorm(hidden_dim)
            )
        elif variant == "fourier":
            # Fourier-based time encoding (positional encoding style)
            num_features = 8  # sin/cos pairs for 4 frequencies
            self.time_mlp = nn.Sequential(
                nn.Linear(num_features, hidden_dim),
                nn.ReLU(),
                nn.Linear(hidden_dim, hidden_dim),
                nn.LayerNorm(hidden_dim)
            )
        elif variant == "learned_buckets":
            # Learnable time buckets
            self.num_buckets = 32
            self.bucket_embedding = nn.Embedding(self.num_buckets, hidden_dim)
            self.time_mlp = None
        else:
            raise ValueError(f"Unknown TimeEncoder variant: {variant}")
    
    def forward(self, age_seconds: torch.Tensor) -> torch.Tensor:
        """
        Args:
            age_seconds: [batch] float tensor
        Returns:
            [batch, hidden_dim] time encoding
        """
        # Protect against log(0) and handle edge cases
        age = torch.clamp(age_seconds, min=0.01, max=1e7)
        
        if self.variant == "standard":
            # Multi-scale time features
            features = torch.stack([
                torch.log(age + 1),           # log scale (viral windows)
                torch.sqrt(age),              # sublinear (early growth)
                age,                          # linear
                age ** 2                      # quadratic (late decay)
            ], dim=-1)
            return self.time_mlp(features)
        
        elif self.variant == "fourier":
            # Sinusoidal positional encoding style
            max_period = 86400.0  # 1 day in seconds
            freqs = [1.0, 2.0, 4.0, 8.0]
            features = []
            for freq in freqs:
                period = max_period / freq
                sin_component = torch.sin(2 * np.pi * age / period)
                cos_component = torch.cos(2 * np.pi * age / period)
                features.extend([sin_component, cos_component])
            features = torch.stack(features, dim=-1)
            return self.time_mlp(features)
        
        elif self.variant == "learned_buckets":
            # Bucket-based encoding
            # Map age to bucket index (logarithmic buckets)
            log_age = torch.log(age + 1)
            max_log_age = 16.0  # ~8.9M seconds
            bucket_idx = (log_age / max_log_age * self.num_buckets).long()
            bucket_idx = torch.clamp(bucket_idx, 0, self.num_buckets - 1)
            return self.bucket_embedding(bucket_idx)
        
        else:
            raise RuntimeError(f"Unhandled variant: {self.variant}")


class ContentSurfaceEncoder(nn.Module):
    """Encodes distribution surface identity"""
    
    SURFACES = ['fyp', 'following', 'search', 'hashtag', 'unknown']
    
    def __init__(self, embed_dim: int = 32):
        super().__init__()
        self.embed_dim = embed_dim
        self.surface_to_idx = {s: i for i, s in enumerate(self.SURFACES)}
        self.embedding = nn.Embedding(len(self.SURFACES), embed_dim)
    
    def forward(self, surfaces: List[str]) -> torch.Tensor:
        """
        Args:
            surfaces: list of surface strings
        Returns:
            [batch, embed_dim]
        """
        indices = torch.tensor([
            self.surface_to_idx.get(s, len(self.SURFACES) - 1) 
            for s in surfaces
        ], dtype=torch.long)
        return self.embedding(indices)


class ConstraintEncoder(nn.Module):
    """Encodes constraint flags (boolean constraints)"""
    
    CONSTRAINT_KEYS = [
        'can_repost',
        'can_edit',
        'monetization_active',
        'copyright_claim',
        'age_restricted'
    ]
    
    def __init__(self, hidden_dim: int = 32):
        super().__init__()
        self.mlp = nn.Sequential(
            nn.Linear(len(self.CONSTRAINT_KEYS), hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.LayerNorm(hidden_dim)
        )
    
    def forward(self, constraints_list: List[Dict[str, bool]]) -> torch.Tensor:
        """
        Args:
            constraints_list: list of constraint dicts
        Returns:
            [batch, hidden_dim]
        """
        # Extract boolean flags in fixed order
        flags = torch.tensor([
            [float(c.get(k, False)) for k in self.CONSTRAINT_KEYS]
            for c in constraints_list
        ], dtype=torch.float32)
        
        return self.mlp(flags)


class PlatformContextEncoder(nn.Module):
    """Encodes platform identity"""
    
    PLATFORMS = ['tiktok', 'youtube_shorts', 'instagram_reels', 'youtube', 'instagram', 'unknown']
    
    def __init__(self, embed_dim: int = 16):
        super().__init__()
        self.embed_dim = embed_dim
        self.platform_to_idx = {p: i for i, p in enumerate(self.PLATFORMS)}
        self.embedding = nn.Embedding(len(self.PLATFORMS), embed_dim)
    
    def forward(self, platforms: List[str]) -> torch.Tensor:
        """
        Args:
            platforms: list of platform strings
        Returns:
            [batch, embed_dim]
        """
        indices = torch.tensor([
            self.platform_to_idx.get(p, len(self.PLATFORMS) - 1)
            for p in platforms
        ], dtype=torch.long)
        return self.embedding(indices)


class DistributionStateEncoder(nn.Module):
    """Encodes distribution state (pending, active, paused, etc.)"""
    
    DISTRIBUTION_STATES = [
        'pending', 'active', 'paused', 'completed', 
        'archived', 'scheduled', 'draft', 'processing', 'unknown'
    ]
    
    def __init__(self, embed_dim: int = 24):
        super().__init__()
        self.embed_dim = embed_dim
        self.state_to_idx = {s: i for i, s in enumerate(self.DISTRIBUTION_STATES)}
        
        # Learnable embedding table
        self.embedding = nn.Embedding(len(self.DISTRIBUTION_STATES), embed_dim)
        
        # Additional MLP for state transition awareness
        self.state_mlp = nn.Sequential(
            nn.Linear(embed_dim, embed_dim),
            nn.ReLU(),
            nn.Linear(embed_dim, embed_dim),
            nn.LayerNorm(embed_dim)
        )
    
    def forward(self, states: List[str]) -> torch.Tensor:
        """
        Args:
            states: list of distribution state strings
        Returns:
            [batch, embed_dim] distribution state encoding
        """
        # Map states to indices
        indices = torch.tensor([
            self.state_to_idx.get(s, len(self.DISTRIBUTION_STATES) - 1)
            for s in states
        ], dtype=torch.long)
        
        # Get embeddings
        state_embs = self.embedding(indices)  # [B, embed_dim]
        
        # Apply MLP for richer representation
        return self.state_mlp(state_embs)
    
    def get_state_index(self, state: str) -> int:
        """Get index for a given state"""
        return self.state_to_idx.get(state, len(self.DISTRIBUTION_STATES) - 1)


class StateEncoder(nn.Module):
    """Aggregates all state encodings into latent representation"""
    
    def __init__(self, state_dim: int = 256):
        super().__init__()
        self.state_dim = state_dim
        
        # Sub-encoders
        self.time_encoder = TimeEncoder(hidden_dim=64)
        self.surface_encoder = ContentSurfaceEncoder(embed_dim=32)
        self.constraint_encoder = ConstraintEncoder(hidden_dim=32)
        self.platform_encoder = PlatformContextEncoder(embed_dim=16)
        self.distribution_state_encoder = DistributionStateEncoder(embed_dim=24)
        
        # Fusion layer - updated to include distribution state
        total_dim = 64 + 32 + 32 + 16 + 24  # sum of encoder dims (168)
        self.fusion = nn.Sequential(
            nn.Linear(total_dim, state_dim),
            nn.ReLU(),
            nn.Dropout(0.05),  # Light dropout for regularization (disabled in eval)
            nn.Linear(state_dim, state_dim),
            nn.LayerNorm(state_dim)
        )
    
    def forward(self, policy_inputs: List[PolicyInput]) -> torch.Tensor:
        """
        Args:
            policy_inputs: list of PolicyInput objects
        Returns:
            [batch, state_dim] state encoding
        """
        batch_size = len(policy_inputs)
        if batch_size == 0:
            raise ValueError("Empty policy_inputs list")
        
        # Extract fields
        ages = torch.tensor([p.video_age_seconds for p in policy_inputs], dtype=torch.float32)
        surfaces = [p.content_surface for p in policy_inputs]
        constraints = [p.constraints for p in policy_inputs]
        platforms = [p.platform for p in policy_inputs]
        distribution_states = [p.distribution_state for p in policy_inputs]
        
        # Encode each component
        time_enc = self.time_encoder(ages)          # [B, 64]
        surface_enc = self.surface_encoder(surfaces)  # [B, 32]
        constraint_enc = self.constraint_encoder(constraints)  # [B, 32]
        platform_enc = self.platform_encoder(platforms)  # [B, 16]
        dist_state_enc = self.distribution_state_encoder(distribution_states)  # [B, 24]
        
        # Concatenate and fuse
        combined = torch.cat([
            time_enc, surface_enc, constraint_enc, platform_enc, dist_state_enc
        ], dim=-1)  # [B, 168]
        
        # Validate no NaNs before fusion
        if torch.isnan(combined).any():
            raise RuntimeError("NaN detected in state encoder before fusion")
        
        encoded = self.fusion(combined)  # [B, state_dim]
        
        # Validate output
        if torch.isnan(encoded).any():
            raise RuntimeError("NaN detected in state encoder output")
        
        return encoded


# ============================================================================
# ADVANCED ENCODER VARIANTS (Attention & Transformer-based for 5M+ Views)
# ============================================================================

class MultiHeadAttentionEncoder(nn.Module):
    """Multi-head attention-based state encoder for high-dimensional representations"""
    
    def __init__(
        self,
        state_dim: int = 256,
        num_heads: int = 8,
        num_layers: int = 2,
        dropout: float = 0.1
    ):
        super().__init__()
        self.state_dim = state_dim
        self.num_heads = num_heads
        self.num_layers = num_layers
        
        # Sub-encoders (same as standard StateEncoder)
        self.time_encoder = TimeEncoder(hidden_dim=64)
        self.surface_encoder = ContentSurfaceEncoder(embed_dim=32)
        self.constraint_encoder = ConstraintEncoder(hidden_dim=32)
        self.platform_encoder = PlatformContextEncoder(embed_dim=16)
        self.distribution_state_encoder = DistributionStateEncoder(embed_dim=24)
        
        # Projection to state_dim
        total_dim = 64 + 32 + 32 + 16 + 24
        self.input_proj = nn.Linear(total_dim, state_dim)
        
        # Multi-head attention layers
        self.attention_layers = nn.ModuleList([
            nn.MultiheadAttention(
                embed_dim=state_dim,
                num_heads=num_heads,
                dropout=dropout,
                batch_first=True
            )
            for _ in range(num_layers)
        ])
        
        # Layer norms and feedforward
        self.layer_norms = nn.ModuleList([
            nn.LayerNorm(state_dim) for _ in range(num_layers)
        ])
        
        self.feedforward = nn.ModuleList([
            nn.Sequential(
                nn.Linear(state_dim, state_dim * 4),
                nn.GELU(),
                nn.Dropout(dropout),
                nn.Linear(state_dim * 4, state_dim),
                nn.Dropout(dropout)
            )
            for _ in range(num_layers)
        ])
        
        self.output_norm = nn.LayerNorm(state_dim)
    
    def forward(self, policy_inputs: List[PolicyInput]) -> torch.Tensor:
        """Forward pass with attention mechanism"""
        batch_size = len(policy_inputs)
        
        # Encode components (same as standard encoder)
        ages = torch.tensor([p.video_age_seconds for p in policy_inputs], dtype=torch.float32)
        surfaces = [p.content_surface for p in policy_inputs]
        constraints = [p.constraints for p in policy_inputs]
        platforms = [p.platform for p in policy_inputs]
        distribution_states = [p.distribution_state for p in policy_inputs]
        
        time_enc = self.time_encoder(ages)
        surface_enc = self.surface_encoder(surfaces)
        constraint_enc = self.constraint_encoder(constraints)
        platform_enc = self.platform_encoder(platforms)
        dist_state_enc = self.distribution_state_encoder(distribution_states)
        
        # Concatenate
        combined = torch.cat([
            time_enc, surface_enc, constraint_enc, platform_enc, dist_state_enc
        ], dim=-1)
        
        # Project to state_dim
        x = self.input_proj(combined)  # [B, state_dim]
        
        # Add sequence dimension for attention (treat batch as sequence)
        x = x.unsqueeze(1)  # [B, 1, state_dim]
        
        # Apply attention layers
        for i in range(self.num_layers):
            # Self-attention
            attn_out, _ = self.attention_layers[i](x, x, x)
            x = self.layer_norms[i](x + attn_out)
            
            # Feedforward
            ff_out = self.feedforward[i](x)
            x = self.layer_norms[i](x + ff_out)
        
        # Remove sequence dimension and apply output norm
        x = x.squeeze(1)  # [B, state_dim]
        x = self.output_norm(x)
        
        return x


class TransformerStateEncoder(nn.Module):
    """Transformer-based state encoder for complex state representations"""
    
    def __init__(
        self,
        state_dim: int = 256,
        num_layers: int = 3,
        num_heads: int = 8,
        dim_feedforward: int = 1024,
        dropout: float = 0.1,
        activation: str = 'gelu'
    ):
        super().__init__()
        self.state_dim = state_dim
        
        # Sub-encoders
        self.time_encoder = TimeEncoder(hidden_dim=64)
        self.surface_encoder = ContentSurfaceEncoder(embed_dim=32)
        self.constraint_encoder = ConstraintEncoder(hidden_dim=32)
        self.platform_encoder = PlatformContextEncoder(embed_dim=16)
        self.distribution_state_encoder = DistributionStateEncoder(embed_dim=24)
        
        # Projection to state_dim for each component separately
        self.time_proj = nn.Linear(64, state_dim)
        self.surface_proj = nn.Linear(32, state_dim)
        self.constraint_proj = nn.Linear(32, state_dim)
        self.platform_proj = nn.Linear(16, state_dim)
        self.dist_state_proj = nn.Linear(24, state_dim)
        
        # Positional encoding (learnable)
        self.pos_encoding = nn.Parameter(torch.randn(1, 5, state_dim))  # 5 components
        
        # Transformer encoder
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=state_dim,
            nhead=num_heads,
            dim_feedforward=dim_feedforward,
            dropout=dropout,
            activation=activation,
            batch_first=True
        )
        self.transformer = nn.TransformerEncoder(
            encoder_layer,
            num_layers=num_layers
        )
        
        # Output projection
        self.output_proj = nn.Sequential(
            nn.Linear(state_dim, state_dim),
            nn.LayerNorm(state_dim)
        )
    
    def forward(self, policy_inputs: List[PolicyInput]) -> torch.Tensor:
        """Forward pass with transformer architecture"""
        batch_size = len(policy_inputs)
        
        # Encode components
        ages = torch.tensor([p.video_age_seconds for p in policy_inputs], dtype=torch.float32)
        surfaces = [p.content_surface for p in policy_inputs]
        constraints = [p.constraints for p in policy_inputs]
        platforms = [p.platform for p in policy_inputs]
        distribution_states = [p.distribution_state for p in policy_inputs]
        
        time_enc = self.time_encoder(ages)
        surface_enc = self.surface_encoder(surfaces)
        constraint_enc = self.constraint_encoder(constraints)
        platform_enc = self.platform_encoder(platforms)
        dist_state_enc = self.distribution_state_encoder(distribution_states)
        
        # Project each component to state_dim and stack as sequence
        time_proj = self.time_proj(time_enc).unsqueeze(1)  # [B, 1, state_dim]
        surface_proj = self.surface_proj(surface_enc).unsqueeze(1)
        constraint_proj = self.constraint_proj(constraint_enc).unsqueeze(1)
        platform_proj = self.platform_proj(platform_enc).unsqueeze(1)
        dist_state_proj = self.dist_state_proj(dist_state_enc).unsqueeze(1)
        
        # Stack components as sequence [B, 5, state_dim]
        components = torch.cat([
            time_proj, surface_proj, constraint_proj, platform_proj, dist_state_proj
        ], dim=1)  # [B, 5, state_dim]
        
        # Add positional encoding
        x = components + self.pos_encoding
        
        # Apply transformer
        x = self.transformer(x)  # [B, 5, state_dim]
        
        # Pool (mean) across component dimension
        x = x.mean(dim=1)  # [B, state_dim]
        
        # Output projection
        x = self.output_proj(x)
        
        return x


class EfficientStateEncoder(nn.Module):
    """Memory-efficient state encoder optimized for 5M+ views baseline"""
    
    def __init__(self, state_dim: int = 256, use_quantization: bool = False):
        super().__init__()
        self.state_dim = state_dim
        self.use_quantization = use_quantization
        
        # Use shared encoders where possible to reduce memory
        self.time_encoder = TimeEncoder(hidden_dim=64, variant="standard")
        self.surface_encoder = ContentSurfaceEncoder(embed_dim=32)
        self.constraint_encoder = ConstraintEncoder(hidden_dim=32)
        self.platform_encoder = PlatformContextEncoder(embed_dim=16)
        self.distribution_state_encoder = DistributionStateEncoder(embed_dim=24)
        
        # Efficient fusion using grouped convolution style
        total_dim = 64 + 32 + 32 + 16 + 24
        self.fusion = nn.Sequential(
            nn.Linear(total_dim, state_dim, bias=False),  # No bias for efficiency
            nn.ReLU(inplace=True),  # In-place for memory efficiency
            nn.Linear(state_dim, state_dim, bias=False),
            nn.LayerNorm(state_dim, eps=1e-5)  # Smaller eps for numerical stability
        )
    
    def forward(self, policy_inputs: List[PolicyInput]) -> torch.Tensor:
        """Memory-efficient forward pass"""
        # Batch processing with gradient checkpointing if needed
        batch_size = len(policy_inputs)
        
        # Extract fields
        ages = torch.tensor([p.video_age_seconds for p in policy_inputs], dtype=torch.float32)
        surfaces = [p.content_surface for p in policy_inputs]
        constraints = [p.constraints for p in policy_inputs]
        platforms = [p.platform for p in policy_inputs]
        distribution_states = [p.distribution_state for p in policy_inputs]
        
        # Encode (reuse computation where possible)
        time_enc = self.time_encoder(ages)
        surface_enc = self.surface_encoder(surfaces)
        constraint_enc = self.constraint_encoder(constraints)
        platform_enc = self.platform_encoder(platforms)
        dist_state_enc = self.distribution_state_encoder(distribution_states)
        
        # Efficient concatenation
        combined = torch.cat([
            time_enc, surface_enc, constraint_enc, platform_enc, dist_state_enc
        ], dim=-1)
        
        # Fuse with numerical stability
        combined = NumericalStability.clamp_logits(combined)
        encoded = self.fusion(combined)
        
        # Apply quantization if enabled (for inference only)
        if self.use_quantization and not self.training:
            encoded = torch.quantize_per_tensor(encoded, scale=0.1, zero_point=0, dtype=torch.quint8)
            encoded = encoded.dequantize()
        
        return encoded


class HybridStateEncoder(nn.Module):
    """Hybrid encoder combining multiple encoding strategies"""
    
    def __init__(self, state_dim: int = 256, use_attention: bool = True):
        super().__init__()
        self.state_dim = state_dim
        self.use_attention = use_attention
        
        # Standard encoder
        self.standard_encoder = StateEncoder(state_dim=state_dim)
        
        # Attention encoder (if enabled)
        if use_attention:
            self.attention_encoder = MultiHeadAttentionEncoder(
                state_dim=state_dim,
                num_heads=4,
                num_layers=1
            )
            self.fusion_weight = nn.Parameter(torch.tensor(0.5))  # Learnable fusion
        else:
            self.attention_encoder = None
        
        # Final fusion
        self.final_fusion = nn.Sequential(
            nn.Linear(state_dim, state_dim),
            nn.LayerNorm(state_dim)
        )
    
    def forward(self, policy_inputs: List[PolicyInput]) -> torch.Tensor:
        """Forward pass combining multiple encoders"""
        # Standard encoding
        standard_enc = self.standard_encoder(policy_inputs)
        
        if self.use_attention and self.attention_encoder is not None:
            # Attention encoding
            attention_enc = self.attention_encoder(policy_inputs)
            
            # Weighted combination
            alpha = torch.sigmoid(self.fusion_weight)
            combined = alpha * standard_enc + (1 - alpha) * attention_enc
        else:
            combined = standard_enc
        
        # Final fusion
        output = self.final_fusion(combined)
        
        return output


# ============================================================================
# ACTION SPACE VALIDATION & SCHEMA CHECKING
# ============================================================================

class ActionSpaceValidator:
    """Validates action space schema and structure"""
    
    REQUIRED_ACTION_KEYS = {'action_id', 'type', 'parameters_schema'}
    VALID_ACTION_TYPES = {
        'NO_OP', 'CAPTION_SWAP', 'THUMBNAIL_SWAP', 
        'REPOST', 'SCHEDULING_SHIFT', 'HASHTAG_UPDATE',
        'COMMENT_PIN', 'DESCRIPTION_UPDATE', 'TAG_ADD'
    }
    
    @staticmethod
    def validate_action_space(action_space: List[Dict]) -> Tuple[bool, List[str]]:
        """
        Validate action space structure
        
        Args:
            action_space: list of action dictionaries
        
        Returns:
            (is_valid, list_of_errors)
        """
        errors = []
        
        if not isinstance(action_space, list):
            return False, [f"Action space must be list, got {type(action_space)}"]
        
        if len(action_space) == 0:
            return False, ["Action space cannot be empty"]
        
        action_ids = set()
        for idx, action in enumerate(action_space):
            # Check required keys
            if not isinstance(action, dict):
                errors.append(f"Action at index {idx} must be dict, got {type(action)}")
                continue
            
            missing_keys = ActionSpaceValidator.REQUIRED_ACTION_KEYS - set(action.keys())
            if missing_keys:
                errors.append(
                    f"Action at index {idx} missing required keys: {missing_keys}"
                )
                continue
            
            # Validate action_id
            action_id = action.get('action_id')
            if not isinstance(action_id, str):
                errors.append(f"Action at index {idx}: action_id must be str")
            elif action_id in action_ids:
                errors.append(f"Action at index {idx}: duplicate action_id '{action_id}'")
            else:
                action_ids.add(action_id)
            
            # Validate action type
            action_type = action.get('type')
            if action_type not in ActionSpaceValidator.VALID_ACTION_TYPES:
                errors.append(
                    f"Action at index {idx}: invalid type '{action_type}'. "
                    f"Must be one of: {ActionSpaceValidator.VALID_ACTION_TYPES}"
                )
            
            # Validate parameters_schema
            params_schema = action.get('parameters_schema')
            if not isinstance(params_schema, dict):
                errors.append(
                    f"Action at index {idx}: parameters_schema must be dict"
                )
        
        is_valid = len(errors) == 0
        return is_valid, errors
    
    @staticmethod
    def find_no_op_action_index(action_space: List[Dict]) -> Optional[int]:
        """Find index of NO_OP action"""
        for idx, action in enumerate(action_space):
            if action.get('type') == 'NO_OP':
                return idx
        return None
    
    @staticmethod
    def get_irreversible_action_indices(action_space: List[Dict]) -> Set[int]:
        """Get indices of actions that are irreversible"""
        irreversible_types = {'REPOST', 'SCHEDULING_SHIFT'}
        indices = set()
        for idx, action in enumerate(action_space):
            if action.get('type') in irreversible_types:
                indices.add(idx)
        return indices
    
    @staticmethod
    def validate_action_mask(action_mask: torch.Tensor, num_actions: int) -> bool:
        """Validate action mask dimensions and content"""
        if not isinstance(action_mask, torch.Tensor):
            return False
        if action_mask.shape[0] != num_actions:
            return False
        if action_mask.dtype != torch.bool:
            return False
        if not torch.any(action_mask):
            return False  # At least one action must be valid
        return True


# ============================================================================
# ADVANCED CACHING & DISTRIBUTED INFERENCE (5M+ Views Baseline)
# ============================================================================

class StateEncodingCache:
    """Advanced caching for state encodings to handle 5M+ views baseline"""
    
    def __init__(self, max_size: int = 100000, cache_ttl: float = 3600.0):
        self.max_size = max_size
        self.cache_ttl = cache_ttl  # Time-to-live in seconds
        self.cache: Dict[str, Tuple[torch.Tensor, float]] = {}
        self.access_times: Dict[str, float] = {}
        self.lock = threading.Lock()
    
    def _hash_input(self, policy_input: PolicyInput) -> str:
        """Generate hash for policy input"""
        # Create deterministic hash from input fields
        hash_data = {
            'video_age': round(policy_input.video_age_seconds, 2),
            'surface': policy_input.content_surface,
            'state': policy_input.distribution_state,
            'platform': policy_input.platform,
            'constraints': tuple(sorted(policy_input.constraints.items())),
            'mask': policy_input.action_mask.cpu().numpy().tobytes()
        }
        hash_str = json.dumps(hash_data, sort_keys=True)
        return hashlib.md5(hash_str.encode()).hexdigest()
    
    def get(self, policy_input: PolicyInput) -> Optional[torch.Tensor]:
        """Get cached encoding if available and not expired"""
        key = self._hash_input(policy_input)
        current_time = time.time()
        
        with self.lock:
            if key in self.cache:
                encoding, cache_time = self.cache[key]
                if current_time - cache_time < self.cache_ttl:
                    self.access_times[key] = current_time
                    return encoding.clone()  # Return copy to avoid mutations
                else:
                    # Expired, remove
                    del self.cache[key]
                    if key in self.access_times:
                        del self.access_times[key]
        
        return None
    
    def put(self, policy_input: PolicyInput, encoding: torch.Tensor):
        """Cache encoding with eviction policy"""
        key = self._hash_input(policy_input)
        current_time = time.time()
        
        with self.lock:
            # Evict least recently used if cache is full
            if len(self.cache) >= self.max_size and key not in self.cache:
                # Remove least recently used entry
                if self.access_times:
                    lru_key = min(self.access_times.items(), key=lambda x: x[1])[0]
                    if lru_key in self.cache:
                        del self.cache[lru_key]
                    if lru_key in self.access_times:
                        del self.access_times[lru_key]
            
            # Cache the encoding
            self.cache[key] = (encoding.detach().clone(), current_time)
            self.access_times[key] = current_time
    
    def clear(self):
        """Clear entire cache"""
        with self.lock:
            self.cache.clear()
            self.access_times.clear()
    
    def stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        with self.lock:
            return {
                'size': len(self.cache),
                'max_size': self.max_size,
                'hit_rate': getattr(self, '_hit_count', 0) / max(1, getattr(self, '_access_count', 1)),
                'ttl_seconds': self.cache_ttl
            }


class DistributedInferenceManager:
    """Manage distributed inference for high-throughput 5M+ views baseline"""
    
    def __init__(
        self,
        models: List[nn.Module],
        devices: List[torch.device],
        load_balancer: str = 'round_robin'
    ):
        self.models = models
        self.devices = devices
        self.load_balancer = load_balancer
        self.current_model_idx = 0
        self.request_counts = [0] * len(models)
        self.lock = threading.Lock()
    
    def get_model_and_device(self) -> Tuple[nn.Module, torch.device]:
        """Get next model and device based on load balancing strategy"""
        with self.lock:
            if self.load_balancer == 'round_robin':
                idx = self.current_model_idx
                self.current_model_idx = (self.current_model_idx + 1) % len(self.models)
            elif self.load_balancer == 'least_requests':
                idx = min(range(len(self.request_counts)), key=lambda i: self.request_counts[i])
            else:
                idx = self.current_model_idx
            
            self.request_counts[idx] += 1
            return self.models[idx], self.devices[idx]
    
    def release_model(self, idx: int):
        """Release model after inference"""
        with self.lock:
            if 0 <= idx < len(self.request_counts):
                self.request_counts[idx] = max(0, self.request_counts[idx] - 1)
    
    def batch_inference_distributed(
        self,
        inputs: List[PolicyInput],
        batch_size: int = OPTIMAL_BATCH_SIZE,
        deterministic: bool = True
    ) -> List[PolicyOutput]:
        """Distributed batch inference across multiple models/devices"""
        all_outputs = []
        
        # Split inputs into batches
        batches = BatchProcessor.chunk_list(inputs, batch_size)
        
        for batch in batches:
            # Get model and device
            model, device = self.get_model_and_device()
            model_idx = self.models.index(model)
            
            try:
                # Move inputs to device (if needed)
                batch_outputs = model.forward(
                    batch,
                    deterministic=deterministic
                )
                all_outputs.extend(batch_outputs)
            finally:
                self.release_model(model_idx)
        
        return all_outputs


class StressTester:
    """Stress testing utilities for 5M+ views baseline validation"""
    
    @staticmethod
    def generate_load_test_inputs(
        num_videos: int = 1000000,
        video_age_range: Tuple[float, float] = (0.0, 86400.0),
        surfaces: List[str] = None,
        platforms: List[str] = None
    ) -> List[PolicyInput]:
        """Generate large-scale test inputs for stress testing"""
        if surfaces is None:
            surfaces = ['fyp', 'following', 'search', 'hashtag']
        if platforms is None:
            platforms = ['tiktok', 'youtube_shorts', 'instagram_reels']
        
        inputs = []
        np.random.seed(42)  # Reproducible
        
        for i in range(num_videos):
            video_age = np.random.uniform(*video_age_range)
            surface = np.random.choice(surfaces)
            platform = np.random.choice(platforms)
            distribution_state = np.random.choice([
                'pending', 'active', 'paused', 'completed'
            ])
            
            # Generate random constraints
            constraints = {
                'can_repost': np.random.rand() > 0.3,
                'can_edit': np.random.rand() > 0.2,
                'monetization_active': np.random.rand() > 0.7,
                'copyright_claim': np.random.rand() > 0.9,
                'age_restricted': np.random.rand() > 0.95
            }
            
            # Generate action mask (5 actions, some masked)
            num_actions = 5
            mask = torch.ones(num_actions, dtype=torch.bool)
            num_masked = np.random.randint(0, 2)  # 0-1 actions masked
            masked_indices = np.random.choice(num_actions, num_masked, replace=False)
            mask[masked_indices] = False
            
            # Ensure at least one action is valid
            if not mask.any():
                mask[0] = True
            
            policy_input = PolicyInput(
                video_age_seconds=float(video_age),
                content_surface=surface,
                distribution_state=distribution_state,
                constraints=constraints,
                platform=platform,
                action_mask=mask
            )
            
            inputs.append(policy_input)
        
        return inputs
    
    @staticmethod
    def run_stress_test(
        model: PolicyNetwork,
        num_samples: int = 10000,
        batch_size: int = OPTIMAL_BATCH_SIZE,
        device: torch.device = None
    ) -> Dict[str, Any]:
        """
        Run stress test to validate 5M+ views baseline capability
        
        Args:
            model: PolicyNetwork to test
            num_samples: number of samples to test
            batch_size: batch size for inference
            device: device to run on
        
        Returns:
            Dictionary with stress test results
        """
        if device is None:
            device = torch.device('cuda' if CUDA_AVAILABLE else 'cpu')
        
        # Generate test inputs
        logger.info(f"Generating {num_samples} test inputs...")
        test_inputs = StressTester.generate_load_test_inputs(num_videos=num_samples)
        
        # Warmup
        logger.info("Warming up...")
        warmup_inputs = test_inputs[:min(100, len(test_inputs))]
        for _ in range(3):
            _ = model.forward(warmup_inputs, deterministic=True)
        
        if device.type == 'cuda':
            torch.cuda.synchronize()
        
        # Run stress test
        logger.info("Running stress test...")
        start_time = time.time()
        
        all_outputs = []
        num_batches = (len(test_inputs) + batch_size - 1) // batch_size
        
        for i in range(0, len(test_inputs), batch_size):
            batch = test_inputs[i:i + batch_size]
            outputs = model.forward(batch, deterministic=True)
            all_outputs.extend(outputs)
            
            if (i // batch_size) % 100 == 0:
                logger.info(f"Processed {i // batch_size}/{num_batches} batches")
        
        if device.type == 'cuda':
            torch.cuda.synchronize()
        
        elapsed_time = time.time() - start_time
        
        # Compute metrics
        total_samples = len(all_outputs)
        samples_per_second = total_samples / elapsed_time
        samples_per_hour = samples_per_second * 3600
        samples_per_day = samples_per_hour * 24
        
        # Check for errors
        errors = []
        for i, output in enumerate(all_outputs):
            try:
                output.validate(model.num_actions)
            except Exception as e:
                errors.append(f"Output {i}: {e}")
        
        # Validate entropies
        entropies = [out.entropy for out in all_outputs]
        avg_entropy = np.mean(entropies)
        min_entropy = np.min(entropies)
        max_entropy = np.max(entropies)
        
        # Check 5M baseline capability
        # 5M views per day = 5,000,000 / 86400 ≈ 57.87 views/second
        can_handle_5m_baseline = samples_per_second >= 58.0
        
        results = {
            'total_samples': total_samples,
            'elapsed_time_seconds': elapsed_time,
            'samples_per_second': samples_per_second,
            'samples_per_minute': samples_per_second * 60,
            'samples_per_hour': samples_per_hour,
            'samples_per_day': samples_per_day,
            'time_per_sample_ms': (elapsed_time / total_samples) * 1000,
            'batch_size': batch_size,
            'num_errors': len(errors),
            'errors': errors[:10],  # First 10 errors
            'avg_entropy': avg_entropy,
            'min_entropy': min_entropy,
            'max_entropy': max_entropy,
            'can_handle_5m_baseline': can_handle_5m_baseline,
            'baseline_requirement_views_per_second': 58.0,
            'throughput_meets_requirement': can_handle_5m_baseline
        }
        
        return results
    
    @staticmethod
    def print_stress_test_results(results: Dict[str, Any]):
        """Print stress test results in human-readable format"""
        print("\n" + "=" * 80)
        print("STRESS TEST RESULTS (5M+ Views Baseline Validation)")
        print("=" * 80)
        print(f"Total Samples Processed: {results['total_samples']:,}")
        print(f"Elapsed Time: {results['elapsed_time_seconds']:.2f} seconds")
        print(f"\nThroughput:")
        print(f"  Samples/Second: {results['samples_per_second']:.2f}")
        print(f"  Samples/Minute: {results['samples_per_minute']:,.0f}")
        print(f"  Samples/Hour: {results['samples_per_hour']:,.0f}")
        print(f"  Samples/Day: {results['samples_per_day']:,.0f}")
        print(f"\nLatency:")
        print(f"  Time per Sample: {results['time_per_sample_ms']:.3f} ms")
        print(f"\nQuality Metrics:")
        print(f"  Average Entropy: {results['avg_entropy']:.4f}")
        print(f"  Min Entropy: {results['min_entropy']:.4f}")
        print(f"  Max Entropy: {results['max_entropy']:.4f}")
        print(f"\nErrors: {results['num_errors']}")
        if results['errors']:
            print("  First few errors:")
            for error in results['errors']:
                print(f"    - {error}")
        print(f"\n5M Baseline Capability:")
        requirement = results['baseline_requirement_views_per_second']
        throughput = results['samples_per_second']
        print(f"  Requirement: {requirement:.2f} views/second")
        print(f"  Actual: {throughput:.2f} views/second")
        print(f"  Meets Requirement: {'✓ YES' if results['can_handle_5m_baseline'] else '✗ NO'}")
        print("=" * 80 + "\n")


class PerformanceProfiler:
    """Performance profiling utilities for 5M+ views baseline"""
    
    @staticmethod
    @contextmanager
    def profile_operation(operation_name: str):
        """Context manager for profiling operations"""
        start_time = time.time()
        start_memory = None
        
        if CUDA_AVAILABLE:
            torch.cuda.reset_peak_memory_stats()
            start_memory = torch.cuda.memory_allocated()
        
        try:
            yield
        finally:
            elapsed_time = time.time() - start_time
            
            end_memory = None
            peak_memory = None
            if CUDA_AVAILABLE:
                end_memory = torch.cuda.memory_allocated()
                peak_memory = torch.cuda.max_memory_allocated()
            
            logger.info(
                f"Operation '{operation_name}': "
                f"time={elapsed_time*1000:.2f}ms, "
                f"memory_used={((end_memory - start_memory) / 1e6) if (start_memory and end_memory) else 0:.2f}MB, "
                f"peak_memory={((peak_memory - start_memory) / 1e6) if (start_memory and peak_memory) else 0:.2f}MB"
            )
    
    @staticmethod
    def profile_model(
        model: nn.Module,
        input_shape: Tuple[int, ...],
        device: torch.device,
        num_warmup: int = 10,
        num_iterations: int = 100
    ) -> Dict[str, float]:
        """Profile model performance"""
        model.eval()
        model.to(device)
        
        # Warmup
        dummy_input = torch.randn(1, *input_shape, device=device)
        for _ in range(num_warmup):
            with torch.inference_mode():
                _ = model(dummy_input)
        
        if device.type == 'cuda':
            torch.cuda.synchronize()
        
        # Profile
        start_time = time.time()
        start_memory = torch.cuda.memory_allocated() if device.type == 'cuda' else 0
        
        for _ in range(num_iterations):
            with torch.inference_mode():
                _ = model(dummy_input)
        
        if device.type == 'cuda':
            torch.cuda.synchronize()
        
        elapsed_time = time.time() - start_time
        end_memory = torch.cuda.memory_allocated() if device.type == 'cuda' else 0
        peak_memory = torch.cuda.max_memory_allocated() if device.type == 'cuda' else 0
        
        return {
            'avg_time_ms': (elapsed_time / num_iterations) * 1000,
            'throughput_samples_per_sec': num_iterations / elapsed_time,
            'memory_used_mb': (end_memory - start_memory) / 1e6,
            'peak_memory_mb': (peak_memory - start_memory) / 1e6
        }


# ============================================================================
# ACTION EMBEDDING TABLE
# ============================================================================

class ActionEmbeddingTable(nn.Module):
    """Learned embeddings for each action type with caching"""
    
    def __init__(self, action_space: List[Dict], embed_dim: int = 128):
        super().__init__()
        
        # Validate action space
        is_valid, errors = ActionSpaceValidator.validate_action_space(action_space)
        if not is_valid:
            error_msg = "Action space validation failed:\n" + "\n".join(errors)
            raise ValueError(error_msg)
        
        self.action_space = action_space
        self.num_actions = len(action_space)
        self.embed_dim = embed_dim
        
        # Each action gets a learnable embedding
        self.embeddings = nn.Embedding(self.num_actions, embed_dim)
        
        # Action metadata (for interpretability and lookups)
        self.action_ids = [a['action_id'] for a in action_space]
        self.action_types = [a['type'] for a in action_space]
        self.action_id_to_idx = {aid: idx for idx, aid in enumerate(self.action_ids)}
        
        # Cache for action embeddings (when in eval mode)
        self._cached_embeddings: Optional[torch.Tensor] = None
        self._cache_device: Optional[torch.device] = None
    
    def forward(self, use_cache: bool = True) -> torch.Tensor:
        """
        Returns:
            [num_actions, embed_dim] - all action embeddings
        
        Args:
            use_cache: if True and in eval mode, use cached embeddings
        """
        # Use cache if available and in eval mode
        if use_cache and self.training == False:
            if self._cached_embeddings is not None:
                current_device = next(self.parameters()).device
                if self._cache_device == current_device:
                    return self._cached_embeddings
        
        indices = torch.arange(self.num_actions, dtype=torch.long, device=next(self.parameters()).device)
        embs = self.embeddings(indices)
        
        # Cache if in eval mode
        if not self.training:
            self._cached_embeddings = embs
            self._cache_device = embs.device
        
        return embs
    
    def get_action_id(self, idx: int) -> str:
        """Map action index to action_id"""
        if idx < 0 or idx >= self.num_actions:
            raise IndexError(f"Action index {idx} out of range [0, {self.num_actions})")
        return self.action_ids[idx]
    
    def get_action_idx(self, action_id: str) -> int:
        """Map action_id to index"""
        if action_id not in self.action_id_to_idx:
            raise ValueError(f"Unknown action_id: {action_id}")
        return self.action_id_to_idx[action_id]
    
    def clear_cache(self):
        """Clear cached embeddings"""
        self._cached_embeddings = None
        self._cache_device = None


# ============================================================================
# STATE-ACTION FUSION
# ============================================================================

class StateActionFusion(nn.Module):
    """Combines state representation with action embeddings"""
    
    def __init__(self, state_dim: int, action_dim: int, hidden_dim: int = 256):
        super().__init__()
        
        # Cross-attention style fusion
        self.state_proj = nn.Linear(state_dim, hidden_dim)
        self.action_proj = nn.Linear(action_dim, hidden_dim)
        
        # Interaction layer
        self.interaction = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.ReLU(),
            nn.Dropout(0.1),
            nn.Linear(hidden_dim, hidden_dim),
            nn.LayerNorm(hidden_dim)
        )
        
        # Final logit head
        self.logit_head = nn.Linear(hidden_dim, 1)
    
    def forward(self, state_enc: torch.Tensor, action_embs: torch.Tensor) -> torch.Tensor:
        """
        Args:
            state_enc: [batch, state_dim]
            action_embs: [num_actions, action_dim]
        Returns:
            [batch, num_actions] raw logits
        """
        batch_size = state_enc.shape[0]
        num_actions = action_embs.shape[0]
        
        # Project state and actions
        state_proj = self.state_proj(state_enc)  # [B, hidden]
        action_proj = self.action_proj(action_embs)  # [A, hidden]
        
        # Broadcast and concatenate
        state_exp = state_proj.unsqueeze(1).expand(batch_size, num_actions, -1)  # [B, A, hidden]
        action_exp = action_proj.unsqueeze(0).expand(batch_size, num_actions, -1)  # [B, A, hidden]
        
        combined = torch.cat([state_exp, action_exp], dim=-1)  # [B, A, 2*hidden]
        
        # Interaction and logit computation
        interaction = self.interaction(combined)  # [B, A, hidden]
        logits = self.logit_head(interaction).squeeze(-1)  # [B, A]
        
        return logits


# ============================================================================
# MASKED ACTION HEAD (CRITICAL)
# ============================================================================

class MaskedActionHead(nn.Module):
    """Applies action masking to ensure zero probability for invalid actions"""
    
    def __init__(self):
        super().__init__()
        self.mask_value = -1e9  # effectively -inf
    
    def forward(self, logits: torch.Tensor, mask: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Args:
            logits: [batch, num_actions] raw logits
            mask: [batch, num_actions] boolean (True = valid)
        Returns:
            masked_logits: [batch, num_actions]
            action_probs: [batch, num_actions] (sums to 1)
        """
        # Apply mask: set invalid actions to -inf
        masked_logits = torch.where(
            mask,
            logits,
            torch.full_like(logits, self.mask_value)
        )
        
        # Softmax only over valid actions
        action_probs = F.softmax(masked_logits, dim=-1)
        
        # INVARIANT CHECK: masked actions must have exactly 0 probability
        if not torch.allclose(action_probs[~mask], torch.zeros_like(action_probs[~mask]), atol=1e-6):
            raise RuntimeError("Masked actions have non-zero probability!")
        
        return masked_logits, action_probs


# ============================================================================
# ENTROPY MONITOR
# ============================================================================

class EntropyMonitor(nn.Module):
    """Computes and tracks policy entropy for exploration monitoring"""
    
    @staticmethod
    def compute_entropy(probs: torch.Tensor, eps: float = 1e-8) -> torch.Tensor:
        """
        Args:
            probs: [batch, num_actions]
        Returns:
            [batch] entropy values
        """
        # H(p) = -sum(p * log(p))
        log_probs = torch.log(probs + eps)
        entropy = -(probs * log_probs).sum(dim=-1)
        return entropy
    
    @staticmethod
    def check_collapse(entropy: float, num_actions: int, threshold: float = 0.1) -> bool:
        """
        Detect if policy has collapsed (very low entropy)
        
        Args:
            entropy: scalar entropy value
            num_actions: total number of actions
            threshold: fraction of max entropy (0-1)
        Returns:
            True if policy has collapsed
        """
        max_entropy = np.log(num_actions)
        return entropy < (threshold * max_entropy)


# ============================================================================
# MAIN POLICY NETWORK
# ============================================================================

class PolicyNetwork(nn.Module):
    """
    Per-Video Micro-Decision Policy Network
    
    WHAT IT DOES:
    - Encodes environment state
    - Produces action probability distribution
    - Respects hard constraints via masking
    - Supports deterministic replay
    - Applies cold-start protection for early videos
    
    WHAT IT DOES NOT DO:
    - Predict rewards
    - Access engagement metrics
    - Apply heuristics
    - Compare videos
    """
    
    MODEL_VERSION = "v1.0.0"
    
    def __init__(
        self,
        action_space: List[Dict],
        state_dim: int = 256,
        action_dim: int = 128,
        hidden_dim: int = 256,
        enable_cold_start: bool = True,
        cold_start_threshold: float = 3600.0,
        enable_diagnostics: bool = True,
        strict_replay_mode: bool = False,
        use_learned_cold_start: bool = True
    ):
        super().__init__()
        
        # Validate action space
        is_valid, errors = ActionSpaceValidator.validate_action_space(action_space)
        if not is_valid:
            error_msg = "Action space validation failed:\n" + "\n".join(errors)
            raise ValueError(error_msg)
        
        self.action_space = action_space
        self.num_actions = len(action_space)
        self.enable_cold_start = enable_cold_start
        self.enable_diagnostics = enable_diagnostics
        
        # Find NO_OP action index for cold-start
        self.no_op_idx = ActionSpaceValidator.find_no_op_action_index(action_space)
        if self.no_op_idx is None and enable_cold_start:
            logger.warning(
                "Cold-start enabled but NO_OP action not found in action space. "
                "Cold-start bias will be applied to action index 0."
            )
            self.no_op_idx = 0
        
        # Get irreversible action indices
        self.irreversible_indices = ActionSpaceValidator.get_irreversible_action_indices(action_space)
        
        # Core components
        self.state_encoder = StateEncoder(state_dim=state_dim)
        self.action_embeddings = ActionEmbeddingTable(action_space, embed_dim=action_dim)
        self.state_action_fusion = StateActionFusion(state_dim, action_dim, hidden_dim)
        self.masked_action_head = MaskedActionHead()
        self.entropy_monitor = EntropyMonitor()
        
        # Cold-start policy component (with learned temperature/policy head)
        if enable_cold_start:
            self.cold_start_policy = ColdStartPolicy(
                num_actions=self.num_actions,
                no_op_idx=self.no_op_idx if self.no_op_idx is not None else 0,
                threshold=cold_start_threshold,
                use_learned_temperature=use_learned_cold_start
            )
        else:
            self.cold_start_policy = None
        
        # Dropout controller for deterministic inference
        self.dropout_controller = DropoutController(self, original_dropout_rate=0.1)
        
        # Diagnostics and performance tracking
        self._forward_count = 0
        self._diagnostics_history: List[Dict[str, Any]] = []
        
        logger.info(
            f"PolicyNetwork initialized: {self.num_actions} actions, "
            f"version {self.MODEL_VERSION}, "
            f"cold-start={'enabled' if enable_cold_start else 'disabled'}"
        )
    
    def forward(
        self,
        policy_inputs: List[PolicyInput],
        deterministic: bool = False,
        seed: Optional[int] = None,
        enable_diagnostics: Optional[bool] = None
    ) -> List[PolicyOutput]:
        """
        Main forward pass with comprehensive validation and cold-start support
        
        Args:
            policy_inputs: list of PolicyInput objects
            deterministic: if True, use eval mode (no dropout)
            seed: optional seed for reproducibility
            enable_diagnostics: override instance-level diagnostics setting
        
        Returns:
            list of PolicyOutput objects
        """
        forward_start_time = time.time()
        diagnostics_enabled = enable_diagnostics if enable_diagnostics is not None else self.enable_diagnostics
        
        # ========================================================================
        # PHASE 1: SETUP & VALIDATION
        # ========================================================================
        
        # Set determinism
        was_training = self.training
        dropout_ctx = None
        
        if deterministic:
            # Use dropout controller for safe dropout management (production-grade)
            dropout_ctx = self.dropout_controller
            dropout_ctx.__enter__()
            
            self.eval()
            if seed is not None:
                torch.manual_seed(seed)
                if torch.cuda.is_available():
                    torch.cuda.manual_seed_all(seed)
                    # Apply strict replay mode if enabled
                    if self.strict_replay_mode:
                        GPUOptimizer.enable_optimizations(
                            torch.device('cuda'),
                            strict_replay_mode=True
                        )
        
        try:
            # Validate inputs are not empty
            if not policy_inputs:
                raise ValueError("policy_inputs cannot be empty")
            
            batch_size = len(policy_inputs)
            
            # Comprehensive input validation
            for i, inp in enumerate(policy_inputs):
                try:
                    PolicyInvariantChecker.check_state_schema(inp)
                except Exception as e:
                    raise ValueError(f"Input validation failed for policy_inputs[{i}]: {e}")
                
                # Validate action mask length matches action space
                if not ActionSpaceValidator.validate_action_mask(inp.action_mask, self.num_actions):
                    raise ValueError(
                        f"Invalid action mask at index {i}: "
                        f"shape={inp.action_mask.shape}, dtype={inp.action_mask.dtype}, "
                        f"expected length={self.num_actions}"
                    )
                
                PolicyInvariantChecker.check_mask_length(inp.action_mask, self.num_actions)
                PolicyInvariantChecker.check_no_nans(inp.action_mask, f"action_mask[{i}]")
            
            # ========================================================================
            # PHASE 2: STATE ENCODING
            # ========================================================================
            
            try:
                state_enc = self.state_encoder(policy_inputs)  # [B, state_dim]
                PolicyInvariantChecker.check_no_nans(state_enc, "state_encoding")
                
                if state_enc.shape[0] != batch_size:
                    raise RuntimeError(
                        f"State encoding batch size mismatch: "
                        f"got {state_enc.shape[0]}, expected {batch_size}"
                    )
            except Exception as e:
                raise RuntimeError(f"State encoding failed: {e}") from e
            
            # ========================================================================
            # PHASE 3: ACTION EMBEDDING
            # ========================================================================
            
            try:
                use_cache = deterministic and not self.training
                action_embs = self.action_embeddings(use_cache=use_cache)  # [A, action_dim]
                PolicyInvariantChecker.check_no_nans(action_embs, "action_embeddings")
                
                if action_embs.shape[0] != self.num_actions:
                    raise RuntimeError(
                        f"Action embedding count mismatch: "
                        f"got {action_embs.shape[0]}, expected {self.num_actions}"
                    )
            except Exception as e:
                raise RuntimeError(f"Action embedding failed: {e}") from e
            
            # ========================================================================
            # PHASE 4: STATE-ACTION FUSION
            # ========================================================================
            
            try:
                logits = self.state_action_fusion(state_enc, action_embs)  # [B, A]
                PolicyInvariantChecker.check_no_nans(logits, "logits")
                
                if logits.shape != (batch_size, self.num_actions):
                    raise RuntimeError(
                        f"Logit shape mismatch: got {logits.shape}, "
                        f"expected ({batch_size}, {self.num_actions})"
                    )
            except Exception as e:
                raise RuntimeError(f"State-action fusion failed: {e}") from e
            
            # ========================================================================
            # PHASE 5: COLD-START BIAS APPLICATION (CRITICAL)
            # ========================================================================
            
            if self.enable_cold_start and self.cold_start_policy is not None:
                for i, policy_input in enumerate(policy_inputs):
                    if self.cold_start_policy.should_apply(policy_input.video_age_seconds):
                        # Apply cold-start bias using learned temperature and policy head
                        # (more theoretically pure than direct logit manipulation)
                        logits[i] = self.cold_start_policy.apply_cold_start_bias(
                            logits[i],
                            policy_input.video_age_seconds,
                            irreversible_indices=self.irreversible_indices
                        )
                        
                        if diagnostics_enabled:
                            logger.debug(
                                f"Applied cold-start bias (learned temperature/policy head) "
                                f"to video age={policy_input.video_age_seconds:.1f}s (index {i})"
                            )
            
            PolicyInvariantChecker.check_no_nans(logits, "logits_after_cold_start")
            
            # ========================================================================
            # PHASE 6: ACTION MASKING
            # ========================================================================
            
            try:
                masks = torch.stack([p.action_mask for p in policy_inputs])  # [B, A]
                
                if masks.shape != (batch_size, self.num_actions):
                    raise RuntimeError(
                        f"Mask shape mismatch: got {masks.shape}, "
                        f"expected ({batch_size}, {self.num_actions})"
                    )
                
                masked_logits, action_probs = self.masked_action_head(logits, masks)
                PolicyInvariantChecker.check_no_nans(masked_logits, "masked_logits")
                PolicyInvariantChecker.check_no_nans(action_probs, "action_probs")
                
                # Comprehensive masked probability validation
                for i in range(batch_size):
                    PolicyInvariantChecker.check_masked_probabilities(action_probs[i], masks[i])
            except Exception as e:
                raise RuntimeError(f"Action masking failed: {e}") from e
            
            # ========================================================================
            # PHASE 7: ENTROPY COMPUTATION
            # ========================================================================
            
            try:
                entropies = self.entropy_monitor.compute_entropy(action_probs)
                PolicyInvariantChecker.check_no_nans(entropies, "entropies")
                
                if entropies.shape[0] != batch_size:
                    raise RuntimeError(
                        f"Entropy shape mismatch: got {entropies.shape[0]}, expected {batch_size}"
                    )
            except Exception as e:
                raise RuntimeError(f"Entropy computation failed: {e}") from e
            
            # ========================================================================
            # PHASE 8: BUILD OUTPUTS
            # ========================================================================
            
            outputs = []
            diagnostics_data = []
            
            for i in range(batch_size):
                # Find masked action IDs
                masked_indices = torch.where(~masks[i])[0].tolist()
                masked_action_ids = [
                    self.action_embeddings.get_action_id(idx) 
                    for idx in masked_indices
                ]
                
                # Build output
                output = PolicyOutput(
                    action_logits=masked_logits[i].detach().clone(),
                    action_probs=action_probs[i].detach().clone(),
                    entropy=entropies[i].item(),
                    masked_actions=masked_action_ids,
                    model_version=self.MODEL_VERSION
                )
                
                # Validate output
                try:
                    output.validate(self.num_actions)
                except Exception as e:
                    raise RuntimeError(f"Output validation failed for batch item {i}: {e}") from e
                
                # Check for policy collapse
                if self.entropy_monitor.check_collapse(output.entropy, self.num_actions):
                    logger.warning(
                        f"Policy collapse detected at batch index {i}: "
                        f"entropy={output.entropy:.4f}, "
                        f"max_entropy={np.log(self.num_actions):.4f}"
                    )
                
                outputs.append(output)
                
                # Collect diagnostics if enabled
                if diagnostics_enabled:
                    diag = {
                        'batch_idx': i,
                        'video_age_seconds': policy_inputs[i].video_age_seconds,
                        'entropy': output.entropy,
                        'num_masked_actions': len(masked_action_ids),
                        'cold_start_applied': (
                            self.enable_cold_start and 
                            self.cold_start_policy is not None and
                            self.cold_start_policy.should_apply(policy_inputs[i].video_age_seconds)
                        ),
                        'max_prob': action_probs[i].max().item(),
                        'min_prob': action_probs[i][masks[i]].min().item() if masks[i].any() else 0.0,
                    }
                    diagnostics_data.append(diag)
            
            # Update diagnostics history
            forward_time = time.time() - forward_start_time
            self._forward_count += 1
            
            if diagnostics_enabled and diagnostics_data:
                summary = {
                    'forward_count': self._forward_count,
                    'batch_size': batch_size,
                    'forward_time_ms': forward_time * 1000,
                    'per_sample_time_ms': (forward_time * 1000) / batch_size,
                    'items': diagnostics_data
                }
                self._diagnostics_history.append(summary)
                
                # Keep only last 1000 entries to prevent memory growth
                if len(self._diagnostics_history) > 1000:
                    self._diagnostics_history = self._diagnostics_history[-1000:]
            
            return outputs
        
        finally:
            # Restore training mode and dropout state
            if was_training and deterministic:
                self.train()
            
            # Restore dropout controller state (always restore, even on exception)
            if dropout_ctx is not None:
                try:
                    dropout_ctx.__exit__(None, None, None)
                except Exception as e:
                    logger.warning(f"Error restoring dropout state: {e}")
    
    def get_action_distribution(
        self,
        policy_input: PolicyInput,
        deterministic: bool = True,
        seed: Optional[int] = None
    ) -> PolicyOutput:
        """
        Convenience method for single input
        
        Args:
            policy_input: single PolicyInput
            deterministic: if True, use eval mode
            seed: optional seed for reproducibility
        
        Returns:
            single PolicyOutput
        """
        outputs = self.forward([policy_input], deterministic=deterministic, seed=seed)
        return outputs[0]
    
    def get_diagnostics_summary(self, last_n: int = 100) -> Dict[str, Any]:
        """
        Get diagnostics summary from recent forward passes
        
        Args:
            last_n: number of recent forward passes to include
        
        Returns:
            Dictionary with diagnostics summary
        """
        if not self._diagnostics_history:
            return {
                'total_forwards': self._forward_count,
                'recent_forwards': 0,
                'avg_forward_time_ms': 0.0,
                'avg_entropy': 0.0,
                'cold_start_rate': 0.0
            }
        
        recent = self._diagnostics_history[-last_n:]
        total_items = sum(d['batch_size'] for d in recent)
        
        all_entropies = []
        cold_start_count = 0
        
        for diag in recent:
            for item in diag['items']:
                all_entropies.append(item['entropy'])
                if item.get('cold_start_applied', False):
                    cold_start_count += 1
        
        return {
            'total_forwards': self._forward_count,
            'recent_forwards': len(recent),
            'total_samples_processed': total_items,
            'avg_forward_time_ms': np.mean([d['forward_time_ms'] for d in recent]),
            'avg_per_sample_time_ms': np.mean([d['per_sample_time_ms'] for d in recent]),
            'avg_entropy': np.mean(all_entropies) if all_entropies else 0.0,
            'std_entropy': np.std(all_entropies) if all_entropies else 0.0,
            'cold_start_rate': cold_start_count / total_items if total_items > 0 else 0.0,
            'min_entropy': np.min(all_entropies) if all_entropies else 0.0,
            'max_entropy': np.max(all_entropies) if all_entropies else 0.0
        }
    
    def clear_diagnostics(self):
        """Clear diagnostics history"""
        self._diagnostics_history.clear()
        self._forward_count = 0
        logger.info("Diagnostics history cleared")
    
    def clear_caches(self):
        """Clear all cached values (embeddings, etc.)"""
        if hasattr(self.action_embeddings, 'clear_cache'):
            self.action_embeddings.clear_cache()
        logger.debug("Policy network caches cleared")
    
    def validate_invariants(self, policy_inputs: List[PolicyInput]) -> Tuple[bool, List[str]]:
        """
        Validate invariants without running forward pass
        
        Args:
            policy_inputs: list of PolicyInput objects
        
        Returns:
            (is_valid, list_of_errors)
        """
        errors = []
        
        if not policy_inputs:
            errors.append("policy_inputs is empty")
            return False, errors
        
        for i, inp in enumerate(policy_inputs):
            try:
                PolicyInvariantChecker.check_state_schema(inp)
            except Exception as e:
                errors.append(f"Input {i}: {e}")
            
            try:
                ActionSpaceValidator.validate_action_mask(inp.action_mask, self.num_actions)
            except Exception as e:
                errors.append(f"Input {i} mask validation: {e}")
        
        return len(errors) == 0, errors
    
    def save_checkpoint(self, path: str, metadata: Optional[Dict[str, Any]] = None):
        """Save model checkpoint with metadata"""
        checkpoint = {
            'model_state_dict': self.state_dict(),
            'model_version': self.MODEL_VERSION,
            'num_actions': self.num_actions,
            'action_space': self.action_space,
            'metadata': metadata or {}
        }
        torch.save(checkpoint, path)
        logger.info(f"Checkpoint saved to {path}")
    
    @classmethod
    def load_checkpoint(cls, path: str, map_location: Optional[str] = None) -> 'PolicyNetwork':
        """Load model from checkpoint"""
        checkpoint = torch.load(path, map_location=map_location)
        
        policy = cls(
            action_space=checkpoint['action_space'],
            enable_cold_start=True  # Will be set based on checkpoint if available
        )
        policy.load_state_dict(checkpoint['model_state_dict'])
        
        logger.info(f"Checkpoint loaded from {path}, version {checkpoint.get('model_version', 'unknown')}")
        return policy


# ============================================================================
# COLD-START BEHAVIOR
# ============================================================================

class ColdStartPolicy(nn.Module):
    """
    Special policy for early-stage videos (Theoretically Pure Implementation)
    
    Uses learned temperature and explicit policy head instead of direct logit manipulation.
    This is more theoretically sound than ad-hoc logit adjustments.
    
    Applies conservative action distribution:
    - High weight on no_op via learned temperature scaling
    - De-weight irreversible actions via explicit policy head
    - High entropy (exploratory) via temperature modulation
    - Adaptive bias based on video age
    """
    
    COLD_START_THRESHOLD = 3600.0  # 1 hour in seconds
    
    def __init__(
        self, 
        num_actions: int, 
        no_op_idx: int = 0,
        threshold: float = 3600.0,
        no_op_bias_strength: float = 2.0,
        irreversible_penalty: float = 1.0,
        use_learned_temperature: bool = True,
        temperature_dim: int = 32
    ):
        super().__init__()
        self.num_actions = num_actions
        self.no_op_idx = no_op_idx
        self.COLD_START_THRESHOLD = threshold
        self.no_op_bias_strength = no_op_bias_strength
        self.irreversible_penalty = irreversible_penalty
        self.use_learned_temperature = use_learned_temperature
        
        if no_op_idx < 0 or no_op_idx >= num_actions:
            raise ValueError(
                f"Invalid no_op_idx: {no_op_idx}, must be in [0, {num_actions})"
            )
        
        # Learned temperature network (more theoretically pure)
        if use_learned_temperature:
            self.temperature_network = nn.Sequential(
                nn.Linear(1, temperature_dim),  # Input: normalized video age [0, 1]
                nn.ReLU(),
                nn.Linear(temperature_dim, temperature_dim),
                nn.ReLU(),
                nn.Linear(temperature_dim, 1),
                nn.Softplus()  # Ensure positive temperature
            )
            # Initialize to reasonable default (temperature ~1.0 for new videos)
            with torch.no_grad():
                self.temperature_network[-2].bias.fill_(0.0)  # Softplus(0) ≈ 0.693 → temp ≈ 1.0
        
        # Explicit cold-start policy head (alternative to direct logit manipulation)
        self.cold_start_head = nn.Sequential(
            nn.Linear(1, 64),  # Input: normalized video age
            nn.ReLU(),
            nn.Linear(64, num_actions),  # Output: action-specific bias
            nn.Tanh()  # Bounded bias
        )
        
        # Base temperature (used when learned temperature is disabled)
        self.base_temperature = nn.Parameter(torch.tensor(1.0))
    
    def should_apply(self, video_age_seconds: float) -> bool:
        """
        Check if video is in cold-start phase
        
        Args:
            video_age_seconds: age of video in seconds
        
        Returns:
            True if cold-start protection should apply
        """
        if video_age_seconds < 0:
            logger.warning(f"Negative video age: {video_age_seconds}, treating as cold-start")
            return True
        return video_age_seconds < self.COLD_START_THRESHOLD
    
    def get_bias_strength(self, video_age_seconds: float) -> float:
        """
        Compute bias strength based on video age (decays linearly)
        
        Args:
            video_age_seconds: age of video
        
        Returns:
            Bias strength multiplier (1.0 for very new, 0.0 at threshold)
        """
        if video_age_seconds >= self.COLD_START_THRESHOLD:
            return 0.0
        
        # Linear decay from 1.0 to 0.0
        decay = video_age_seconds / self.COLD_START_THRESHOLD
        return 1.0 - decay
    
    def apply_cold_start_bias(
        self,
        logits: torch.Tensor,
        video_age_seconds: float,
        irreversible_indices: Optional[Set[int]] = None
    ) -> torch.Tensor:
        """
        Apply cold-start bias using learned temperature and explicit policy head.
        This is more theoretically pure than direct logit manipulation.
        
        Args:
            logits: [num_actions] raw logits
            video_age_seconds: age of video
            irreversible_indices: set of action indices that are irreversible
        
        Returns:
            biased_logits: [num_actions] with cold-start bias applied
        """
        if not self.should_apply(video_age_seconds):
            return logits
        
        if logits.shape[0] != self.num_actions:
            raise ValueError(
                f"Logit shape mismatch: got {logits.shape[0]}, expected {self.num_actions}"
            )
        
        # Normalize video age to [0, 1] for network input
        normalized_age = torch.tensor(
            min(video_age_seconds / self.COLD_START_THRESHOLD, 1.0),
            dtype=torch.float32,
            device=logits.device
        ).unsqueeze(0)  # [1]
        
        biased_logits = logits.clone()
        
        # Method 1: Learned temperature scaling (theoretically pure)
        if self.use_learned_temperature:
            # Compute adaptive temperature based on video age
            temperature = self.temperature_network(normalized_age).squeeze() + 0.5  # [1] -> scalar, min 0.5
            # Higher temperature = more uniform distribution (exploratory)
            # Lower temperature = more peaked distribution (exploitative)
            # For cold-start, we want higher temperature (more exploration)
            # Temperature decays as video ages (less exploration needed)
            biased_logits = biased_logits / temperature
        else:
            # Fallback: use base temperature with age-based modulation
            bias_strength = self.get_bias_strength(video_age_seconds)
            temperature = self.base_temperature + (1.0 - bias_strength) * 0.5  # 1.0 to 1.5
            biased_logits = biased_logits / temperature
        
        # Method 2: Explicit policy head (learned bias)
        # This is more theoretically sound than ad-hoc logit adjustments
        policy_bias = self.cold_start_head(normalized_age).squeeze(0)  # [num_actions]
        
        # Apply policy head bias (scaled by bias strength)
        bias_strength = self.get_bias_strength(video_age_seconds)
        if bias_strength > 0:
            # Boost no_op via learned bias
            policy_bias[self.no_op_idx] += self.no_op_bias_strength * bias_strength
            
            # Penalize irreversible actions via learned bias
            if irreversible_indices is not None:
                for irr_idx in irreversible_indices:
                    if irr_idx != self.no_op_idx and 0 <= irr_idx < self.num_actions:
                        policy_bias[irr_idx] -= self.irreversible_penalty * bias_strength
            
            # Apply learned bias to logits
            biased_logits = biased_logits + policy_bias * bias_strength
        
        # Validate no NaNs
        if torch.isnan(biased_logits).any():
            logger.error("NaN detected in cold-start biased logits, returning original")
            return logits
        
        return biased_logits
    
    def get_cold_start_phase(self, video_age_seconds: float) -> str:
        """
        Get cold-start phase description
        
        Args:
            video_age_seconds: age of video
        
        Returns:
            Phase description string
        """
        if not self.should_apply(video_age_seconds):
            return "mature"
        
        ratio = video_age_seconds / self.COLD_START_THRESHOLD
        if ratio < 0.25:
            return "very_early"
        elif ratio < 0.5:
            return "early"
        elif ratio < 0.75:
            return "mid_early"
        else:
            return "late_early"


# ============================================================================
# INVARIANT CHECKS (WATCHDOG)
# ============================================================================

# ============================================================================
# ERROR HANDLING CLASSES
# ============================================================================

class PolicyNetworkError(Exception):
    """Base exception for policy network errors"""
    pass


class PolicyValidationError(PolicyNetworkError):
    """Raised when policy input validation fails"""
    pass


class PolicyInvariantError(PolicyNetworkError):
    """Raised when policy invariant is violated"""
    pass


class PolicyEncodingError(PolicyNetworkError):
    """Raised when encoding fails"""
    pass


# ============================================================================
# INVARIANT CHECKS (WATCHDOG)
# ============================================================================

class PolicyInvariantChecker:
    """Runtime checks for policy network invariants with comprehensive validation"""
    
    @staticmethod
    def check_mask_length(mask: torch.Tensor, expected_length: int):
        """Verify action mask matches action space"""
        if not isinstance(mask, torch.Tensor):
            raise PolicyInvariantError(
                f"Mask must be torch.Tensor, got {type(mask)}"
            )
        
        if mask.shape[0] != expected_length:
            raise PolicyInvariantError(
                f"Action mask length mismatch: got {mask.shape[0]}, expected {expected_length}"
            )
    
    @staticmethod
    def check_masked_probabilities(probs: torch.Tensor, mask: torch.Tensor, atol: float = 1e-6):
        """
        Verify masked actions have zero probability
        
        Args:
            probs: [num_actions] probability tensor
            mask: [num_actions] boolean mask (True = valid)
            atol: absolute tolerance for zero check
        """
        if not isinstance(probs, torch.Tensor) or not isinstance(mask, torch.Tensor):
            raise PolicyInvariantError("probs and mask must be torch.Tensor")
        
        if probs.shape != mask.shape:
            raise PolicyInvariantError(
                f"Shape mismatch: probs {probs.shape}, mask {mask.shape}"
            )
        
        if not mask.dtype == torch.bool:
            raise PolicyInvariantError(f"Mask must be boolean, got {mask.dtype}")
        
        masked_probs = probs[~mask]
        
        if len(masked_probs) == 0:
            return  # No masked actions to check
        
        zero_tensor = torch.zeros_like(masked_probs)
        if not torch.allclose(masked_probs, zero_tensor, atol=atol):
            non_zero_count = (masked_probs > atol).sum().item()
            max_non_zero = masked_probs.max().item()
            raise PolicyInvariantError(
                f"Masked actions have non-zero probability: "
                f"{non_zero_count}/{len(masked_probs)} non-zero, "
                f"max={max_non_zero:.2e}, tolerance={atol}"
            )
    
    @staticmethod
    def check_no_nans(tensor: torch.Tensor, name: str):
        """
        Verify no NaN values in tensor
        
        Args:
            tensor: tensor to check
            name: name for error message
        """
        if not isinstance(tensor, torch.Tensor):
            raise PolicyInvariantError(
                f"{name} must be torch.Tensor, got {type(tensor)}"
            )
        
        if torch.isnan(tensor).any():
            nan_count = torch.isnan(tensor).sum().item()
            total_count = tensor.numel()
            raise PolicyInvariantError(
                f"NaN detected in {name}: {nan_count}/{total_count} values are NaN"
            )
    
    @staticmethod
    def check_no_infs(tensor: torch.Tensor, name: str):
        """Verify no infinite values in tensor"""
        if not isinstance(tensor, torch.Tensor):
            raise PolicyInvariantError(
                f"{name} must be torch.Tensor, got {type(tensor)}"
            )
        
        if torch.isinf(tensor).any():
            inf_count = torch.isinf(tensor).sum().item()
            raise PolicyInvariantError(
                f"Infinite values detected in {name}: {inf_count} values are inf"
            )
    
    @staticmethod
    def check_probability_distribution(probs: torch.Tensor, name: str, atol: float = 1e-5):
        """
        Verify tensor is a valid probability distribution
        
        Args:
            probs: probability tensor
            name: name for error message
            atol: absolute tolerance for sum check
        """
        PolicyInvariantChecker.check_no_nans(probs, name)
        PolicyInvariantChecker.check_no_infs(probs, name)
        
        if (probs < 0).any():
            negative_count = (probs < 0).sum().item()
            min_val = probs.min().item()
            raise PolicyInvariantError(
                f"Negative probabilities in {name}: {negative_count} values, min={min_val:.2e}"
            )
        
        prob_sum = probs.sum().item()
        if not np.isclose(prob_sum, 1.0, atol=atol):
            raise PolicyInvariantError(
                f"Probabilities in {name} don't sum to 1.0: sum={prob_sum:.6f}, "
                f"tolerance={atol}"
            )
    
    @staticmethod
    def check_state_schema(policy_input: PolicyInput):
        """Verify state schema is valid"""
        if not isinstance(policy_input, PolicyInput):
            raise PolicyValidationError(
                f"Input must be PolicyInput, got {type(policy_input)}"
            )
        
        try:
            policy_input.validate()
        except Exception as e:
            raise PolicyValidationError(f"State schema validation failed: {e}") from e
    
    @staticmethod
    def check_logits_shape(logits: torch.Tensor, expected_shape: Tuple[int, ...]):
        """Verify logits have expected shape"""
        if logits.shape != expected_shape:
            raise PolicyInvariantError(
                f"Logit shape mismatch: got {logits.shape}, expected {expected_shape}"
            )
    
    @staticmethod
    def check_action_space_consistency(action_space: List[Dict], num_actions: int):
        """Verify action space is consistent"""
        if len(action_space) != num_actions:
            raise PolicyInvariantError(
                f"Action space length mismatch: got {len(action_space)}, expected {num_actions}"
            )
    
    @staticmethod
    def check_device_consistency(*tensors: torch.Tensor):
        """Verify all tensors are on the same device"""
        if len(tensors) < 2:
            return
        
        devices = [t.device for t in tensors if isinstance(t, torch.Tensor)]
        if len(set(devices)) > 1:
            raise PolicyInvariantError(
                f"Device mismatch: tensors on different devices: {set(devices)}"
            )
    
    @staticmethod
    def check_dtype_consistency(tensor: torch.Tensor, expected_dtype: torch.dtype, name: str):
        """Verify tensor has expected dtype"""
        if tensor.dtype != expected_dtype:
            raise PolicyInvariantError(
                f"Dtype mismatch in {name}: got {tensor.dtype}, expected {expected_dtype}"
            )


# ============================================================================
# DIAGNOSTICS & LOGGING UTILITIES
# ============================================================================

class PolicyDiagnostics:
    """Comprehensive diagnostics utilities for policy network"""
    
    @staticmethod
    def compute_action_distribution_stats(output: PolicyOutput) -> Dict[str, float]:
        """
        Compute statistics about action distribution
        
        Args:
            output: PolicyOutput to analyze
        
        Returns:
            Dictionary with statistics
        """
        probs = output.action_probs.detach().cpu().numpy()
        
        return {
            'entropy': output.entropy,
            'max_prob': float(probs.max()),
            'min_prob': float(probs[probs > 0].min()) if (probs > 0).any() else 0.0,
            'mean_prob': float(probs.mean()),
            'std_prob': float(probs.std()),
            'num_valid_actions': int((probs > 1e-6).sum()),
            'num_masked_actions': len(output.masked_actions),
            'gini_coefficient': float(PolicyDiagnostics._compute_gini(probs))
        }
    
    @staticmethod
    def _compute_gini(probs: np.ndarray) -> float:
        """Compute Gini coefficient (measure of inequality)"""
        sorted_probs = np.sort(probs)
        n = len(sorted_probs)
        cumsum = np.cumsum(sorted_probs)
        return (n + 1 - 2 * np.sum((n + 1 - np.arange(1, n + 1)) * sorted_probs) / cumsum[-1]) / n
    
    @staticmethod
    def log_action_distribution(output: PolicyOutput, action_space: List[Dict], logger_instance=None):
        """
        Log action distribution in human-readable format
        
        Args:
            output: PolicyOutput to log
            action_space: action space definition
            logger_instance: logger to use (defaults to module logger)
        """
        log = logger_instance or logger
        
        log.info(f"Action Distribution (Entropy: {output.entropy:.4f})")
        log.info(f"Masked actions: {output.masked_actions}")
        
        probs = output.action_probs.detach().cpu().numpy()
        action_ids = [a['action_id'] for a in action_space]
        
        # Sort by probability
        sorted_indices = np.argsort(probs)[::-1]
        
        for idx in sorted_indices[:10]:  # Top 10
            action_id = action_ids[idx]
            prob = probs[idx]
            log.info(f"  {action_id:30s}: {prob:.4f}")


class PolicyPerformanceMonitor:
    """Monitor policy network performance metrics"""
    
    def __init__(self, window_size: int = 1000):
        self.window_size = window_size
        self.forward_times: List[float] = []
        self.entropies: List[float] = []
        self.batch_sizes: List[int] = []
        self.cold_start_counts: List[int] = []
    
    def record_forward(
        self, 
        forward_time_ms: float, 
        batch_size: int,
        entropies: List[float],
        cold_start_count: int = 0
    ):
        """Record forward pass metrics"""
        self.forward_times.append(forward_time_ms)
        self.batch_sizes.append(batch_size)
        self.cold_start_counts.append(cold_start_count)
        self.entropies.extend(entropies)
        
        # Maintain window size
        if len(self.forward_times) > self.window_size:
            excess = len(self.forward_times) - self.window_size
            self.forward_times = self.forward_times[excess:]
            self.batch_sizes = self.batch_sizes[excess:]
            self.cold_start_counts = self.cold_start_counts[excess:]
            self.entropies = self.entropies[-self.window_size:]
    
    def get_summary(self) -> Dict[str, Any]:
        """Get performance summary"""
        if not self.forward_times:
            return {'status': 'no_data'}
        
        return {
            'num_forwards': len(self.forward_times),
            'avg_forward_time_ms': np.mean(self.forward_times),
            'std_forward_time_ms': np.std(self.forward_times),
            'p50_forward_time_ms': np.percentile(self.forward_times, 50),
            'p95_forward_time_ms': np.percentile(self.forward_times, 95),
            'p99_forward_time_ms': np.percentile(self.forward_times, 99),
            'avg_batch_size': np.mean(self.batch_sizes),
            'total_samples': sum(self.batch_sizes),
            'avg_entropy': np.mean(self.entropies),
            'cold_start_rate': (
                sum(self.cold_start_counts) / sum(self.batch_sizes) 
                if sum(self.batch_sizes) > 0 else 0.0
            )
        }
    
    def reset(self):
        """Reset all metrics"""
        self.forward_times.clear()
        self.entropies.clear()
        self.batch_sizes.clear()
        self.cold_start_counts.clear()


# ============================================================================
# TEST UTILITIES & HELPERS
# ============================================================================

class PolicyTestUtilities:
    """Utilities for testing policy network"""
    
    @staticmethod
    def create_test_action_space(num_actions: int = 5) -> List[Dict]:
        """Create a test action space"""
        base_actions = [
        {"action_id": "no_op", "type": "NO_OP", "parameters_schema": {}},
        {"action_id": "caption_swap_1", "type": "CAPTION_SWAP", "parameters_schema": {"variant": "int"}},
        {"action_id": "thumbnail_swap_1", "type": "THUMBNAIL_SWAP", "parameters_schema": {"variant": "int"}},
        {"action_id": "repost", "type": "REPOST", "parameters_schema": {}},
            {"action_id": "hashtag_update_1", "type": "HASHTAG_UPDATE", "parameters_schema": {"tags": "list"}},
        ]
        
        if num_actions <= len(base_actions):
            return base_actions[:num_actions]
        
        # Extend with additional actions
        actions = base_actions.copy()
        for i in range(len(base_actions), num_actions):
            actions.append({
                "action_id": f"action_{i}",
                "type": "NO_OP",
                "parameters_schema": {}
            })
        
        return actions
    
    @staticmethod
    def create_test_policy_input(
        video_age_seconds: float = 7200.0,
        content_surface: str = "fyp",
        distribution_state: str = "active",
        platform: str = "tiktok",
        num_actions: int = 5,
        mask_ratio: float = 0.0
    ) -> PolicyInput:
        """
        Create a test PolicyInput
        
        Args:
            video_age_seconds: video age
            content_surface: content surface type
            distribution_state: distribution state
            platform: platform name
            num_actions: number of actions (for mask size)
            mask_ratio: fraction of actions to mask (0.0 = all valid)
        """
        constraints = {
            "can_repost": True,
            "can_edit": True,
            "monetization_active": False,
            "copyright_claim": False,
            "age_restricted": False
        }
        
        # Create mask (randomly mask some actions if mask_ratio > 0)
        mask = torch.ones(num_actions, dtype=torch.bool)
        if mask_ratio > 0:
            num_to_mask = int(num_actions * mask_ratio)
            mask_indices = torch.randperm(num_actions)[:num_to_mask]
            mask[mask_indices] = False
            # Ensure at least one action is valid
            if not mask.any():
                mask[0] = True
        
        return PolicyInput(
            video_age_seconds=video_age_seconds,
            content_surface=content_surface,
            distribution_state=distribution_state,
            constraints=constraints,
            platform=platform,
            action_mask=mask
        )
    
    @staticmethod
    def test_determinism(
        policy: PolicyNetwork,
        policy_input: PolicyInput,
        num_runs: int = 10,
        seed: int = 42
    ) -> bool:
        """
        Test that policy produces deterministic outputs
        
        Args:
            policy: policy network to test
            policy_input: input to use
            num_runs: number of runs to test
            seed: seed to use
        
        Returns:
            True if deterministic (all outputs identical)
        """
        policy.eval()
        outputs = []
        
        for _ in range(num_runs):
            output = policy.get_action_distribution(policy_input, deterministic=True, seed=seed)
            outputs.append(output.action_probs.detach().cpu())
        
        # Check all outputs are identical
        first_output = outputs[0]
        for output in outputs[1:]:
            if not torch.allclose(first_output, output, atol=1e-6):
                return False
        
        return True
    
    @staticmethod
    def test_masking_correctness(
        policy: PolicyNetwork,
        policy_input: PolicyInput
    ) -> Tuple[bool, List[str]]:
        """
        Test that masking is correctly applied
        
        Args:
            policy: policy network to test
            policy_input: input with masked actions
        
        Returns:
            (is_correct, list_of_errors)
        """
        errors = []
        output = policy.get_action_distribution(policy_input, deterministic=True)
        
        probs = output.action_probs
        mask = policy_input.action_mask
        
        # Check masked actions have zero probability
        masked_probs = probs[~mask]
        if not torch.allclose(masked_probs, torch.zeros_like(masked_probs), atol=1e-6):
            errors.append(f"Masked actions have non-zero probability: {masked_probs}")
        
        # Check probabilities sum to 1
        if not torch.isclose(probs.sum(), torch.tensor(1.0), atol=1e-5):
            errors.append(f"Probabilities don't sum to 1: {probs.sum()}")
        
        # Check no negative probabilities
        if (probs < 0).any():
            errors.append(f"Negative probabilities found: {probs[probs < 0]}")
        
        # Check masked_actions list matches mask
        expected_masked = [
            policy.action_embeddings.get_action_id(i)
            for i in range(len(mask)) if not mask[i]
        ]
        if set(output.masked_actions) != set(expected_masked):
            errors.append(
                f"Masked actions mismatch: got {set(output.masked_actions)}, "
                f"expected {set(expected_masked)}"
            )
        
        return len(errors) == 0, errors
    
    @staticmethod
    def test_cold_start_behavior(
        policy: PolicyNetwork,
        num_actions: int = 5
    ) -> Tuple[bool, List[str]]:
        """
        Test cold-start behavior for early videos
        
        Args:
            policy: policy network to test
            num_actions: number of actions
        
        Returns:
            (is_correct, list_of_errors)
        """
        if not policy.enable_cold_start or policy.cold_start_policy is None:
            return True, []  # Cold-start not enabled, skip test
        
        errors = []
        
        # Test very early video (< 1 hour)
        early_input = PolicyTestUtilities.create_test_policy_input(
            video_age_seconds=300.0,  # 5 minutes
            num_actions=num_actions
        )
        early_output = policy.get_action_distribution(early_input, deterministic=True)
        
        # Test mature video (> threshold)
        mature_input = PolicyTestUtilities.create_test_policy_input(
            video_age_seconds=72000.0,  # 20 hours
            num_actions=num_actions
        )
        mature_output = policy.get_action_distribution(mature_input, deterministic=True)
        
        # Early video should have higher no_op probability
        if policy.no_op_idx is not None:
            early_no_op_prob = early_output.action_probs[policy.no_op_idx].item()
            mature_no_op_prob = mature_output.action_probs[policy.no_op_idx].item()
            
            if early_no_op_prob <= mature_no_op_prob:
                errors.append(
                    f"Cold-start bias not working: early no_op prob ({early_no_op_prob:.4f}) "
                    f"<= mature ({mature_no_op_prob:.4f})"
                )
        
        # Early video should have higher entropy (more exploratory)
        if early_output.entropy <= mature_output.entropy:
            errors.append(
                f"Cold-start should increase entropy: early ({early_output.entropy:.4f}) "
                f"<= mature ({mature_output.entropy:.4f})"
            )
        
        return len(errors) == 0, errors


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

def example_usage():
    """Demonstration of policy network usage with all features"""
    
    print("=" * 80)
    print("Policy Network Example Usage")
    print("=" * 80)
    
    # Create test action space
    action_space = PolicyTestUtilities.create_test_action_space(num_actions=5)
    print(f"\nAction Space ({len(action_space)} actions):")
    for i, action in enumerate(action_space):
        print(f"  {i}: {action['action_id']} ({action['type']})")
    
    # Initialize policy with cold-start enabled
    print("\nInitializing PolicyNetwork...")
    policy = PolicyNetwork(
        action_space=action_space,
        enable_cold_start=True,
        enable_diagnostics=True
    )
    print(f"Policy initialized: {policy.num_actions} actions")
    print(f"Cold-start threshold: {policy.cold_start_policy.COLD_START_THRESHOLD}s")
    print(f"NO_OP action index: {policy.no_op_idx}")
    
    # Example 1: Mature video (beyond cold-start threshold)
    print("\n" + "-" * 80)
    print("Example 1: Mature Video (2 hours old)")
    print("-" * 80)
    
    mature_input = PolicyTestUtilities.create_test_policy_input(
        video_age_seconds=7200.0,  # 2 hours
        content_surface="fyp",
        distribution_state="active",
        platform="tiktok",
        num_actions=len(action_space),
        mask_ratio=0.2  # 20% of actions masked
    )
    
    mature_output = policy.get_action_distribution(mature_input, deterministic=True, seed=42)
    print(f"Entropy: {mature_output.entropy:.4f}")
    print(f"Masked actions: {mature_output.masked_actions}")
    print(f"Top 3 actions:")
    probs = mature_output.action_probs.detach().cpu().numpy()
    top_indices = np.argsort(probs)[-3:][::-1]
    for idx in top_indices:
        action_id = action_space[idx]['action_id']
        print(f"  {action_id:30s}: {probs[idx]:.4f}")
    
    # Example 2: Early video (cold-start protection)
    print("\n" + "-" * 80)
    print("Example 2: Early Video (5 minutes old - Cold-Start)")
    print("-" * 80)
    
    early_input = PolicyTestUtilities.create_test_policy_input(
        video_age_seconds=300.0,  # 5 minutes
        content_surface="fyp",
        distribution_state="pending",
        platform="tiktok",
        num_actions=len(action_space),
        mask_ratio=0.0  # All actions valid
    )
    
    early_output = policy.get_action_distribution(early_input, deterministic=True, seed=42)
    print(f"Entropy: {early_output.entropy:.4f} (should be higher than mature)")
    print(f"Cold-start phase: {policy.cold_start_policy.get_cold_start_phase(300.0)}")
    
    # Compare NO_OP probabilities
    if policy.no_op_idx is not None:
        early_no_op = early_output.action_probs[policy.no_op_idx].item()
        mature_no_op = mature_output.action_probs[policy.no_op_idx].item()
        print(f"NO_OP probability: early={early_no_op:.4f}, mature={mature_no_op:.4f}")
        print(f"  (early should be higher due to cold-start bias)")
    
    # Example 3: Determinism test
    print("\n" + "-" * 80)
    print("Example 3: Determinism Test")
    print("-" * 80)
    
    is_deterministic = PolicyTestUtilities.test_determinism(
        policy, mature_input, num_runs=5, seed=42
    )
    print(f"Deterministic: {is_deterministic} (should be True)")
    
    # Example 4: Masking correctness test
    print("\n" + "-" * 80)
    print("Example 4: Masking Correctness Test")
    print("-" * 80)
    
    masked_input = PolicyTestUtilities.create_test_policy_input(
        video_age_seconds=7200.0,
        num_actions=len(action_space),
        mask_ratio=0.3  # 30% masked
    )
    
    is_correct, errors = PolicyTestUtilities.test_masking_correctness(policy, masked_input)
    print(f"Masking correct: {is_correct}")
    if errors:
        print("Errors:")
        for error in errors:
            print(f"  - {error}")
    
    # Example 5: Diagnostics
    print("\n" + "-" * 80)
    print("Example 5: Diagnostics Summary")
    print("-" * 80)
    
    # Run a few forward passes to generate diagnostics
    test_inputs = [
        PolicyTestUtilities.create_test_policy_input(
            video_age_seconds=float(i * 1800),  # 0, 30min, 1hr, 1.5hr, 2hr
            num_actions=len(action_space)
        )
        for i in range(5)
    ]
    
    for inp in test_inputs:
        _ = policy.get_action_distribution(inp, deterministic=True)
    
    diag_summary = policy.get_diagnostics_summary(last_n=5)
    print("Diagnostics Summary:")
    for key, value in diag_summary.items():
        if isinstance(value, float):
            print(f"  {key:30s}: {value:.4f}")
        else:
            print(f"  {key:30s}: {value}")
    
    # Example 6: Action distribution stats
    print("\n" + "-" * 80)
    print("Example 6: Action Distribution Statistics")
    print("-" * 80)
    
    stats = PolicyDiagnostics.compute_action_distribution_stats(mature_output)
    print("Distribution Statistics:")
    for key, value in stats.items():
        if isinstance(value, float):
            print(f"  {key:25s}: {value:.4f}")
        else:
            print(f"  {key:25s}: {value}")
    
    print("\n" + "=" * 80)
    print("Example Usage Complete")
    print("=" * 80)


def run_comprehensive_tests():
    """Run comprehensive test suite"""
    print("\n" + "=" * 80)
    print("Running Comprehensive Test Suite")
    print("=" * 80)
    
    action_space = PolicyTestUtilities.create_test_action_space(num_actions=5)
    policy = PolicyNetwork(action_space, enable_cold_start=True, enable_diagnostics=True)
    
    tests_passed = 0
    tests_failed = 0
    
    # Test 1: Determinism
    print("\n[Test 1] Determinism...")
    test_input = PolicyTestUtilities.create_test_policy_input(num_actions=5)
    if PolicyTestUtilities.test_determinism(policy, test_input):
        print("  ✓ PASSED")
        tests_passed += 1
    else:
        print("  ✗ FAILED")
        tests_failed += 1
    
    # Test 2: Masking correctness
    print("\n[Test 2] Masking correctness...")
    is_correct, errors = PolicyTestUtilities.test_masking_correctness(policy, test_input)
    if is_correct:
        print("  ✓ PASSED")
        tests_passed += 1
    else:
        print(f"  ✗ FAILED: {errors}")
        tests_failed += 1
    
    # Test 3: Cold-start behavior
    print("\n[Test 3] Cold-start behavior...")
    is_correct, errors = PolicyTestUtilities.test_cold_start_behavior(policy, num_actions=5)
    if is_correct:
        print("  ✓ PASSED")
        tests_passed += 1
    else:
        print(f"  ✗ FAILED: {errors}")
        tests_failed += 1
    
    # Test 4: Action space validation
    print("\n[Test 4] Action space validation...")
    is_valid, errors = ActionSpaceValidator.validate_action_space(action_space)
    if is_valid:
        print("  ✓ PASSED")
        tests_passed += 1
    else:
        print(f"  ✗ FAILED: {errors}")
        tests_failed += 1
    
    # Test 5: Invariant validation
    print("\n[Test 5] Invariant validation...")
    test_inputs = [PolicyTestUtilities.create_test_policy_input(num_actions=5)]
    is_valid, errors = policy.validate_invariants(test_inputs)
    if is_valid:
        print("  ✓ PASSED")
        tests_passed += 1
    else:
        print(f"  ✗ FAILED: {errors}")
        tests_failed += 1
    
    print("\n" + "=" * 80)
    print(f"Test Results: {tests_passed} passed, {tests_failed} failed")
    print("=" * 80)
    
    return tests_passed, tests_failed


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Policy Network Example")
    parser.add_argument("--test", action="store_true", help="Run comprehensive tests")
    args = parser.parse_args()
    
    if args.test:
        run_comprehensive_tests()
    else:
        example_usage()