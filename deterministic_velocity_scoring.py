"""
DETERMINISTIC VELOCITY SCORING SYSTEM
=====================================

Mathematically-grounded, inspectable velocity equations that replace
abstracted velocity functions with explicit, reproducible calculations.

Core Equation:
V(t) = α × Δmetric/Δtime_smoothed + β × cross_platform_norm + γ × niche_norm

Where:
- Δmetric/Δtime_smoothed = EMA(Δviews/Δtime, α_ema)
- cross_platform_norm = (V_raw - μ_platform) / σ_platform
- niche_norm = (V_raw - μ_niche) / σ_niche
"""

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from collections import defaultdict, deque
import logging

class DeterministicVelocityScorer:
    """
    Mathematically precise velocity scoring with explicit equations.
    
    Every velocity score can be traced back to:
    1. Exact Δmetric/Δtime calculation
    2. EMA smoothing parameters
    3. Cross-platform normalization factors
    4. Per-niche normalization factors
    5. Final weighted combination
    
    No black boxes - fully inspectable mathematics.
    """
    
    def __init__(self, 
                 ema_alpha: float = 0.3,
                 platform_window_hours: int = 24,
                 niche_window_hours: int = 48,
                 min_data_points: int = 3):
        """
        Initialize deterministic velocity scorer.
        
        Args:
            ema_alpha: EMA smoothing factor (0.1-0.9, lower = smoother)
            platform_window_hours: Hours for platform normalization window
            niche_window_hours: Hours for niche normalization window  
            min_data_points: Minimum data points required for velocity calculation
        """
        self.ema_alpha = ema_alpha
        self.platform_window_hours = platform_window_hours
        self.niche_window_hours = niche_window_hours
        self.min_data_points = min_data_points
        
        # Storage for normalization data
        self.platform_history = defaultdict(lambda: deque(maxlen=1000))
        self.niche_history = defaultdict(lambda: deque(maxlen=1000))
        self.ema_values = defaultdict(dict)  # trend_id -> {metric: ema_value}
        
        # Velocity equation weights (sum to 1.0)
        self.velocity_weights = {
            'delta_metric': 0.5,      # Primary: Δmetric/Δtime
            'cross_platform': 0.3,     # Platform normalization
            'niche_norm': 0.2          # Niche normalization
        }
        
        self.logger = logging.getLogger(__name__)
    
    def compute_deterministic_velocity(self, 
                                     trend_id: str,
                                     current_metrics: Dict[str, float],
                                     platform: str,
                                     niche: str,
                                     timestamp: datetime) -> Dict[str, float]:
        """
        Compute mathematically precise velocity score.
        
        Core Equation:
        V(t) = α × Δmetric/Δtime_smoothed + β × cross_platform_norm + γ × niche_norm
        
        Args:
            trend_id: Unique trend identifier
            current_metrics: Dict with 'views', 'engagement_rate', 'likes', 'shares'
            platform: Platform name (tiktok, youtube, etc.)
            niche: Content niche/category
            timestamp: Current timestamp
            
        Returns:
            Dict with complete velocity breakdown and final score
        """
        try:
            # Step 1: Calculate Δmetric/Δtime for each metric
            delta_metrics = self._calculate_delta_metrics(trend_id, current_metrics, timestamp)
            
            if not delta_metrics:
                return self._insufficient_data_response(trend_id)
            
            # Step 2: Apply EMA smoothing to Δmetrics
            smoothed_deltas = self._apply_ema_smoothing(trend_id, delta_metrics)
            
            # Step 3: Compute cross-platform normalization
            platform_norm = self._compute_platform_normalization(platform, smoothed_deltas)
            
            # Step 4: Compute per-niche normalization  
            niche_norm = self._compute_niche_normalization(niche, smoothed_deltas)
            
            # Step 5: Combine using explicit weighted equation
            final_velocity = (
                self.velocity_weights['delta_metric'] * smoothed_deltas['views'] +
                self.velocity_weights['cross_platform'] * platform_norm +
                self.velocity_weights['niche_norm'] * niche_norm
            )
            
            # Step 6: Store for future normalization
            self._update_history(trend_id, current_metrics, platform, niche, timestamp)
            
            # Step 7: Return complete breakdown for inspection
            return {
                'trend_id': trend_id,
                'timestamp': timestamp.isoformat(),
                'platform': platform,
                'niche': niche,
                'final_velocity_score': float(np.clip(final_velocity, 0, 1)),
                
                # Component breakdown (inspectable mathematics)
                'delta_metrics': delta_metrics,
                'smoothed_deltas': smoothed_deltas,
                'platform_normalization': float(platform_norm),
                'niche_normalization': float(niche_norm),
                
                # Equation parameters
                'velocity_weights': self.velocity_weights.copy(),
                'ema_alpha': self.ema_alpha,
                
                # Normalization context
                'platform_stats': self._get_platform_stats(platform),
                'niche_stats': self._get_niche_stats(niche),
                
                # Data quality metrics
                'data_points_used': len(self._get_trend_history(trend_id)),
                'time_span_hours': self._get_time_span_hours(trend_id),
                
                # Debug: Why this score?
                'velocity_explanation': self._generate_velocity_explanation(
                    delta_metrics, smoothed_deltas, platform_norm, niche_norm, final_velocity
                )
            }
            
        except Exception as e:
            self.logger.error(f"Error computing deterministic velocity for {trend_id}: {e}")
            return self._error_response(trend_id, str(e))
    
    def _calculate_delta_metrics(self, 
                                trend_id: str, 
                                current_metrics: Dict[str, float], 
                                timestamp: datetime) -> Dict[str, float]:
        """
        Calculate exact Δmetric/Δtime for each metric.
        
        Δmetric/Δtime = (metric_current - metric_previous) / (time_current - time_previous)
        
        Returns:
            Dict of delta values per metric
        """
        history = self._get_trend_history(trend_id)
        
        if len(history) < self.min_data_points:
            return {}
        
        # Get most recent data point
        previous_data = history[-1]
        previous_timestamp = previous_data['timestamp']
        previous_metrics = previous_data['metrics']
        
        # Calculate time delta in hours
        time_delta = (timestamp - previous_timestamp).total_seconds() / 3600.0
        if time_delta <= 0:
            return {}  # Invalid time progression
        
        # Calculate Δmetric/Δtime for each metric
        delta_metrics = {}
        for metric in ['views', 'engagement_rate', 'likes', 'shares']:
            current_value = current_metrics.get(metric, 0)
            previous_value = previous_metrics.get(metric, 0)
            
            delta_value = current_value - previous_value
            delta_per_hour = delta_value / time_delta
            
            delta_metrics[metric] = delta_per_hour
        
        return delta_metrics
    
    def _apply_ema_smoothing(self, 
                            trend_id: str, 
                            delta_metrics: Dict[str, float]) -> Dict[str, float]:
        """
        Apply Exponential Moving Average smoothing to delta metrics.
        
        EMA(t) = α × Δ(t) + (1-α) × EMA(t-1)
        
        Args:
            trend_id: Trend identifier
            delta_metrics: Current delta values
            
        Returns:
            Smoothed delta values
        """
        smoothed_deltas = {}
        
        for metric, delta_value in delta_metrics.items():
            # Get previous EMA value
            previous_ema = self.ema_values[trend_id].get(metric, 0)
            
            # Apply EMA formula
            current_ema = (self.ema_alpha * delta_value + 
                          (1 - self.ema_alpha) * previous_ema)
            
            # Store for next iteration
            self.ema_values[trend_id][metric] = current_ema
            
            smoothed_deltas[metric] = current_ema
        
        return smoothed_deltas
    
    def _compute_platform_normalization(self, 
                                      platform: str, 
                                      smoothed_deltas: Dict[str, float]) -> float:
        """
        Compute cross-platform normalization.
        
        Z-score normalization: (V_raw - μ_platform) / σ_platform
        
        Args:
            platform: Platform name
            smoothed_deltas: Smoothed delta values
            
        Returns:
            Platform-normalized velocity (0-1 scale)
        """
        platform_history = list(self.platform_history[platform])
        
        if len(platform_history) < 10:  # Insufficient data for normalization
            return 0.5  # Neutral value
        
        # Extract recent platform delta values for views metric
        recent_platform_deltas = [
            entry['delta_metrics']['views'] 
            for entry in platform_history[-100:]  # Last 100 entries
            if 'delta_metrics' in entry and 'views' in entry['delta_metrics']
        ]
        
        if len(recent_platform_deltas) < 5:
            return 0.5
        
        # Calculate platform statistics
        platform_mean = np.mean(recent_platform_deltas)
        platform_std = np.std(recent_platform_deltas)
        
        if platform_std == 0:
            return 0.5
        
        # Z-score normalization
        current_delta = smoothed_deltas['views']
        z_score = (current_delta - platform_mean) / platform_std
        
        # Convert Z-score to 0-1 scale using sigmoid
        normalized = 1 / (1 + np.exp(-z_score))
        
        return float(normalized)
    
    def _compute_niche_normalization(self, 
                                   niche: str, 
                                   smoothed_deltas: Dict[str, float]) -> float:
        """
        Compute per-niche normalization.
        
        Similar to platform normalization but within niche context.
        
        Args:
            niche: Niche name
            smoothed_deltas: Smoothed delta values
            
        Returns:
            Niche-normalized velocity (0-1 scale)
        """
        niche_history = list(self.niche_history[niche])
        
        if len(niche_history) < 10:  # Insufficient data for normalization
            return 0.5  # Neutral value
        
        # Extract recent niche delta values for views metric
        recent_niche_deltas = [
            entry['delta_metrics']['views']
            for entry in niche_history[-100:]  # Last 100 entries
            if 'delta_metrics' in entry and 'views' in entry['delta_metrics']
        ]
        
        if len(recent_niche_deltas) < 5:
            return 0.5
        
        # Calculate niche statistics
        niche_mean = np.mean(recent_niche_deltas)
        niche_std = np.std(recent_niche_deltas)
        
        if niche_std == 0:
            return 0.5
        
        # Z-score normalization
        current_delta = smoothed_deltas['views']
        z_score = (current_delta - niche_mean) / niche_std
        
        # Convert Z-score to 0-1 scale using sigmoid
        normalized = 1 / (1 + np.exp(-z_score))
        
        return float(normalized)
    
    def _update_history(self, 
                        trend_id: str,
                        metrics: Dict[str, float],
                        platform: str,
                        niche: str,
                        timestamp: datetime):
        """Update history storage for future calculations."""
        
        # Update trend history
        if not hasattr(self, 'trend_history'):
            self.trend_history = defaultdict(lambda: deque(maxlen=1000))
        
        self.trend_history[trend_id].append({
            'timestamp': timestamp,
            'metrics': metrics.copy()
        })
        
        # Update platform history
        self.platform_history[platform].append({
            'timestamp': timestamp,
            'trend_id': trend_id,
            'delta_metrics': self._calculate_delta_metrics(trend_id, metrics, timestamp)
        })
        
        # Update niche history
        self.niche_history[niche].append({
            'timestamp': timestamp,
            'trend_id': trend_id,
            'delta_metrics': self._calculate_delta_metrics(trend_id, metrics, timestamp)
        })
    
    def _get_trend_history(self, trend_id: str) -> List[Dict]:
        """Get trend history."""
        if not hasattr(self, 'trend_history'):
            return []
        return list(self.trend_history[trend_id])
    
    def _get_time_span_hours(self, trend_id: str) -> float:
        """Calculate time span in hours for trend data."""
        history = self._get_trend_history(trend_id)
        if len(history) < 2:
            return 0.0
        
        first_time = history[0]['timestamp']
        last_time = history[-1]['timestamp']
        return (last_time - first_time).total_seconds() / 3600.0
    
    def _get_platform_stats(self, platform: str) -> Dict[str, float]:
        """Get platform normalization statistics."""
        platform_history = list(self.platform_history[platform])
        
        if len(platform_history) < 10:
            return {'mean': 0.0, 'std': 0.0, 'count': 0}
        
        deltas = [
            entry['delta_metrics']['views']
            for entry in platform_history[-100:]
            if 'delta_metrics' in entry and 'views' in entry['delta_metrics']
        ]
        
        return {
            'mean': float(np.mean(deltas)) if deltas else 0.0,
            'std': float(np.std(deltas)) if deltas else 0.0,
            'count': len(deltas)
        }
    
    def _get_niche_stats(self, niche: str) -> Dict[str, float]:
        """Get niche normalization statistics."""
        niche_history = list(self.niche_history[niche])
        
        if len(niche_history) < 10:
            return {'mean': 0.0, 'std': 0.0, 'count': 0}
        
        deltas = [
            entry['delta_metrics']['views']
            for entry in niche_history[-100:]
            if 'delta_metrics' in entry and 'views' in entry['delta_metrics']
        ]
        
        return {
            'mean': float(np.mean(deltas)) if deltas else 0.0,
            'std': float(np.std(deltas)) if deltas else 0.0,
            'count': len(deltas)
        }
    
    def _generate_velocity_explanation(self, 
                                    delta_metrics: Dict[str, float],
                                    smoothed_deltas: Dict[str, float],
                                    platform_norm: float,
                                    niche_norm: float,
                                    final_velocity: float) -> str:
        """
        Generate human-readable explanation of velocity calculation.
        
        This answers: "Why did this trend score 0.82 instead of 0.63?"
        """
        explanation_parts = []
        
        # Delta metrics contribution
        views_delta = delta_metrics.get('views', 0)
        views_smoothed = smoothed_deltas.get('views', 0)
        delta_contribution = self.velocity_weights['delta_metric'] * views_smoothed
        
        explanation_parts.append(
            f"Views delta: {views_delta:.1f}/hour → EMA smoothed: {views_smoothed:.1f} "
            f"(weight: {self.velocity_weights['delta_metric']}, contribution: {delta_contribution:.3f})"
        )
        
        # Platform normalization contribution
        platform_contribution = self.velocity_weights['cross_platform'] * platform_norm
        explanation_parts.append(
            f"Platform normalization: {platform_norm:.3f} "
            f"(weight: {self.velocity_weights['cross_platform']}, contribution: {platform_contribution:.3f})"
        )
        
        # Niche normalization contribution
        niche_contribution = self.velocity_weights['niche_norm'] * niche_norm
        explanation_parts.append(
            f"Niche normalization: {niche_norm:.3f} "
            f"(weight: {self.velocity_weights['niche_norm']}, contribution: {niche_contribution:.3f})"
        )
        
        # Final equation
        explanation_parts.append(
            f"Final: {delta_contribution:.3f} + {platform_contribution:.3f} + {niche_contribution:.3f} = {final_velocity:.3f}"
        )
        
        return " | ".join(explanation_parts)
    
    def _insufficient_data_response(self, trend_id: str) -> Dict[str, float]:
        """Response when insufficient data for velocity calculation."""
        return {
            'trend_id': trend_id,
            'final_velocity_score': 0.0,
            'status': 'insufficient_data',
            'data_points_available': len(self._get_trend_history(trend_id)),
            'min_data_points_required': self.min_data_points,
            'explanation': f'Need at least {self.min_data_points} data points, have {len(self._get_trend_history(trend_id))}'
        }
    
    def _error_response(self, trend_id: str, error_msg: str) -> Dict[str, float]:
        """Response when calculation fails."""
        return {
            'trend_id': trend_id,
            'final_velocity_score': 0.0,
            'status': 'error',
            'error': error_msg,
            'explanation': f'Calculation failed: {error_msg}'
        }
    
    def get_velocity_equation(self) -> str:
        """
        Return the exact velocity equation being used.
        
        This makes the system completely transparent and inspectable.
        """
        return (
            "V(t) = α × Δmetric/Δtime_smoothed + β × cross_platform_norm + γ × niche_norm\n"
            f"Where: α={self.velocity_weights['delta_metric']}, "
            f"β={self.velocity_weights['cross_platform']}, "
            f"γ={self.velocity_weights['niche_norm']}\n"
            f"EMA smoothing: α_ema={self.ema_alpha}"
        )
    
    def inspect_velocity_calculation(self, trend_id: str) -> Dict[str, any]:
        """
        Provide complete inspection of a velocity calculation.
        
        This allows RL systems to understand exactly why a score was computed.
        """
        history = self._get_trend_history(trend_id)
        
        if not history:
            return {'error': 'No history available for inspection'}
        
        latest_data = history[-1]
        platform = latest_data.get('platform', 'unknown')
        niche = latest_data.get('niche', 'unknown')
        
        return {
            'trend_id': trend_id,
            'data_points': len(history),
            'time_span_hours': self._get_time_span_hours(trend_id),
            'platform': platform,
            'niche': niche,
            'platform_stats': self._get_platform_stats(platform),
            'niche_stats': self._get_niche_stats(niche),
            'current_ema_values': self.ema_values.get(trend_id, {}),
            'velocity_equation': self.get_velocity_equation(),
            'velocity_weights': self.velocity_weights.copy()
        }


# Integration class to add deterministic velocity to TrendAggregator
class TrendAggregatorVelocityEnhancer:
    """
    Enhances TrendAggregator with deterministic velocity scoring.
    """
    
    def __init__(self, trend_aggregator_instance):
        """Initialize with existing TrendAggregator instance."""
        self.aggregator = trend_aggregator_instance
        self.velocity_scorer = DeterministicVelocityScorer()
        self.logger = logging.getLogger(__name__)
    
    def compute_deterministic_velocity_score(self, 
                                          trend_id: str,
                                          metrics: Dict[str, float],
                                          platform: str,
                                          niche: str,
                                          timestamp: datetime) -> Dict[str, float]:
        """
        Compute deterministic velocity score for a trend.
        
        This replaces any abstracted velocity calculations with explicit mathematics.
        """
        return self.velocity_scorer.compute_deterministic_velocity(
            trend_id, metrics, platform, niche, timestamp
        )
    
    def get_velocity_inspection_report(self, trend_id: str) -> Dict[str, any]:
        """
        Get complete inspection report for velocity calculation.
        
        RL systems can use this to understand why a score was computed.
        """
        return self.velocity_scorer.inspect_velocity_calculation(trend_id)
    
    def get_velocity_equation(self) -> str:
        """Get the exact velocity equation being used."""
        return self.velocity_scorer.get_velocity_equation()


if __name__ == "__main__":
    # Example usage demonstrating deterministic velocity scoring
    scorer = DeterministicVelocityScorer()
    
    print("=== DETERMINISTIC VELOCITY SCORING SYSTEM ===")
    print(scorer.get_velocity_equation())
    print()
    
    # Simulate trend data over time
    trend_id = "test_trend_001"
    platform = "tiktok"
    niche = "entertainment"
    
    # Create time series data
    base_time = datetime.now() - timedelta(hours=10)
    
    for i in range(10):
        timestamp = base_time + timedelta(hours=i)
        metrics = {
            'views': 1000 + i * 500 + np.random.randint(-100, 200),
            'engagement_rate': 0.05 + i * 0.01 + np.random.uniform(-0.01, 0.01),
            'likes': 50 + i * 25,
            'shares': 10 + i * 5
        }
        
        result = scorer.compute_deterministic_velocity(
            trend_id, metrics, platform, niche, timestamp
        )
        
        print(f"Hour {i}: Velocity = {result['final_velocity_score']:.3f}")
        print(f"  Explanation: {result['velocity_explanation']}")
        print()
    
    # Final inspection
    print("=== FINAL INSPECTION ===")
    inspection = scorer.inspect_velocity_calculation(trend_id)
    for key, value in inspection.items():
        print(f"{key}: {value}")
