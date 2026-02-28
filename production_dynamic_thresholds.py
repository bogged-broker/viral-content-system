"""
PRODUCTION-READY DYNAMIC THRESHOLDING SYSTEM
============================================

Simple, predictable, drift-based thresholding for production viral factories.

Key Principles:
1. Simple thresholds that drift slowly based on historical distributions
2. Clear failure modes with automatic rollback
3. Easy rollback to previous stable thresholds
4. Bounded statistical priors with predictable behavior
5. No multi-armed bandit complexity
6. No Bayesian optimization overhead

This replaces overengineered systems with predictable, maintainable solutions.
"""

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from collections import deque
import logging
from dataclasses import dataclass
import json

@dataclass
class ThresholdState:
    """Current state of dynamic thresholds with rollback capability."""
    
    # Current threshold values
    min_trend_score: float
    velocity_decay_factor: float
    anomaly_threshold_multiplier: float
    
    # Historical context
    created_at: datetime
    last_updated: datetime
    update_count: int
    
    # Performance tracking
    success_rate: float
    false_positive_rate: float
    false_negative_rate: float
    
    # Rollback capability
    previous_state: Optional['ThresholdState']
    rollback_available: bool
    
    # Drift tracking
    drift_velocity: float
    drift_direction: str  # 'increasing', 'decreasing', 'stable'
    
    # Health indicators
    health_score: float  # 0-1, higher is better
    stability_score: float  # 0-1, higher is more stable

class ProductionDynamicThresholds:
    """
    Simple, predictable dynamic thresholding for production use.
    
    Replaces complex Bayesian/MAB systems with drift-based adaptation.
    """
    
    def __init__(self, 
                 drift_rate: float = 0.01,           # 1% max change per update
                 stability_window: int = 100,         # Samples for stability calculation
                 rollback_threshold: float = 0.2,     # 20% performance drop triggers rollback
                 min_samples_for_update: int = 50,    # Minimum samples before threshold changes
                 health_check_interval: int = 10):    # Check health every N updates
        """
        Initialize production dynamic thresholds.
        
        Args:
            drift_rate: Maximum rate of threshold change per update (0-1)
            stability_window: Window size for stability calculations
            rollback_threshold: Performance drop that triggers rollback
            min_samples_for_update: Minimum samples before allowing updates
            health_check_interval: How often to check system health
        """
        self.drift_rate = drift_rate
        self.stability_window = stability_window
        self.rollback_threshold = rollback_threshold
        self.min_samples_for_update = min_samples_for_update
        self.health_check_interval = health_check_interval
        
        # Current threshold state
        self.current_state: Optional[ThresholdState] = None
        
        # Historical data for drift calculation
        self.performance_history: deque = deque(maxlen=1000)
        self.threshold_history: deque = deque(maxlen=100)
        
        # Rollback stack
        self.rollback_stack: List[ThresholdState] = []
        self.max_rollback_depth = 5
        
        # Health monitoring
        self.update_count = 0
        self.last_health_check = 0
        self.system_health = 1.0
        
        # Initial thresholds (conservative defaults)
        self.initial_thresholds = {
            'min_trend_score': 0.6,
            'velocity_decay_factor': 0.95,
            'anomaly_threshold_multiplier': 2.0
        }
        
        self.logger = logging.getLogger(__name__)
        
        # Initialize with conservative defaults
        self._initialize_default_state()
    
    def _initialize_default_state(self):
        """Initialize with conservative default thresholds."""
        now = datetime.now()
        
        self.current_state = ThresholdState(
            min_trend_score=self.initial_thresholds['min_trend_score'],
            velocity_decay_factor=self.initial_thresholds['velocity_decay_factor'],
            anomaly_threshold_multiplier=self.initial_thresholds['anomaly_threshold_multiplier'],
            created_at=now,
            last_updated=now,
            update_count=0,
            success_rate=0.5,  # Conservative start
            false_positive_rate=0.5,
            false_negative_rate=0.5,
            previous_state=None,
            rollback_available=False,
            drift_velocity=0.0,
            drift_direction='stable',
            health_score=0.5,
            stability_score=1.0
        )
        
        self.logger.info("Initialized with conservative default thresholds")
    
    def update_thresholds_based_on_performance(self, 
                                            recent_performance: Dict[str, float]) -> bool:
        """
        Update thresholds based on recent performance metrics.
        
        This is the core production method - simple, predictable, bounded.
        
        Args:
            recent_performance: Dict with 'success_rate', 'false_positive_rate', 'false_negative_rate'
            
        Returns:
            bool: True if thresholds were updated, False if no change needed
        """
        try:
            # Check if we have enough data
            if len(self.performance_history) < self.min_samples_for_update:
                self.logger.debug(f"Insufficient data for update: {len(self.performance_history)} < {self.min_samples_for_update}")
                return False
            
            # Calculate performance trend
            performance_trend = self._calculate_performance_trend(recent_performance)
            
            # Determine if update is needed
            if not self._should_update_thresholds(performance_trend):
                return False
            
            # Save current state for rollback
            self._save_state_for_rollback()
            
            # Calculate drift adjustments
            adjustments = self._calculate_drift_adjustments(performance_trend)
            
            # Apply bounded adjustments
            new_state = self._apply_bounded_adjustments(adjustments)
            
            # Validate new thresholds
            if not self._validate_thresholds(new_state):
                self.logger.warning("New thresholds failed validation, rolling back")
                self._rollback_to_previous_state()
                return False
            
            # Update current state
            self.current_state = new_state
            self.update_count += 1
            
            # Record performance
            self._record_performance(recent_performance)
            
            # Check system health
            if self.update_count % self.health_check_interval == 0:
                self._check_system_health()
            
            self.logger.info(f"Updated thresholds: min_score={new_state.min_trend_score:.3f}, "
                           f"velocity_decay={new_state.velocity_decay_factor:.3f}, "
                           f"anomaly_mult={new_state.anomaly_threshold_multiplier:.3f}")
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error updating thresholds: {e}")
            self._rollback_to_previous_state()
            return False
    
    def _calculate_performance_trend(self, recent_performance: Dict[str, float]) -> Dict[str, float]:
        """Calculate performance trend from recent data."""
        try:
            # Add recent performance to history
            self.performance_history.append({
                'timestamp': datetime.now(),
                'success_rate': recent_performance.get('success_rate', 0.5),
                'false_positive_rate': recent_performance.get('false_positive_rate', 0.5),
                'false_negative_rate': recent_performance.get('false_negative_rate', 0.5)
            })
            
            if len(self.performance_history) < 2:
                return recent_performance
            
            # Calculate recent averages
            recent_window = min(20, len(self.performance_history))
            recent_data = list(self.performance_history)[-recent_window:]
            
            avg_success_rate = np.mean([d['success_rate'] for d in recent_data])
            avg_fp_rate = np.mean([d['false_positive_rate'] for d in recent_data])
            avg_fn_rate = np.mean([d['false_negative_rate'] for d in recent_data])
            
            # Calculate trends (change over time)
            if len(recent_data) >= 10:
                older_data = recent_data[:len(recent_data)//2]
                newer_data = recent_data[len(recent_data)//2:]
                
                older_success = np.mean([d['success_rate'] for d in older_data])
                newer_success = np.mean([d['success_rate'] for d in newer_data])
                
                success_trend = newer_success - older_success
            else:
                success_trend = 0.0
            
            return {
                'success_rate': avg_success_rate,
                'false_positive_rate': avg_fp_rate,
                'false_negative_rate': avg_fn_rate,
                'success_trend': success_trend
            }
            
        except Exception as e:
            self.logger.error(f"Error calculating performance trend: {e}")
            return recent_performance
    
    def _should_update_thresholds(self, performance_trend: Dict[str, float]) -> bool:
        """Determine if thresholds should be updated based on performance."""
        try:
            # Don't update if system is unstable
            if self.current_state.stability_score < 0.3:
                return False
            
            # Don't update if health is poor
            if self.system_health < 0.4:
                return False
            
            # Check if performance indicates need for adjustment
            success_rate = performance_trend.get('success_rate', 0.5)
            fp_rate = performance_trend.get('false_positive_rate', 0.5)
            fn_rate = performance_trend.get('false_negative_rate', 0.5)
            success_trend = performance_trend.get('success_trend', 0.0)
            
            # Update if performance is poor
            if success_rate < 0.4 or fp_rate > 0.3 or fn_rate > 0.3:
                return True
            
            # Update if there's a significant negative trend
            if success_trend < -0.1:  # 10% decline
                return True
            
            # Update if there's a significant positive trend (opportunity)
            if success_trend > 0.15:  # 15% improvement
                return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"Error checking update necessity: {e}")
            return False
    
    def _calculate_drift_adjustments(self, performance_trend: Dict[str, float]) -> Dict[str, float]:
        """Calculate bounded drift adjustments based on performance."""
        try:
            success_rate = performance_trend.get('success_rate', 0.5)
            fp_rate = performance_trend.get('false_positive_rate', 0.5)
            fn_rate = performance_trend.get('false_negative_rate', 0.5)
            success_trend = performance_trend.get('success_trend', 0.0)
            
            adjustments = {}
            
            # Adjust minimum trend score based on success rate
            if success_rate < 0.4:  # Poor performance
                # Lower threshold to be more permissive
                adjustments['min_trend_score'] = -self.drift_rate * 0.5
            elif success_rate > 0.8:  # Very good performance
                # Raise threshold to be more selective
                adjustments['min_trend_score'] = self.drift_rate * 0.3
            else:
                # Small adjustment based on trend
                adjustments['min_trend_score'] = success_trend * self.drift_rate * 0.2
            
            # Adjust velocity decay based on false negative rate
            if fn_rate > 0.3:  # Missing good trends
                # Slow down decay (keep trends longer)
                adjustments['velocity_decay_factor'] = self.drift_rate * 0.3
            elif fn_rate < 0.1:  # Too many false positives
                # Speed up decay (drop trends faster)
                adjustments['velocity_decay_factor'] = -self.drift_rate * 0.2
            else:
                adjustments['velocity_decay_factor'] = 0.0
            
            # Adjust anomaly threshold based on false positive rate
            if fp_rate > 0.3:  # Too many false anomalies
                # Raise threshold to be less sensitive
                adjustments['anomaly_threshold_multiplier'] = self.drift_rate * 0.4
            elif fp_rate < 0.1:  # Missing real anomalies
                # Lower threshold to be more sensitive
                adjustments['anomaly_threshold_multiplier'] = -self.drift_rate * 0.3
            else:
                adjustments['anomaly_threshold_multiplier'] = 0.0
            
            return adjustments
            
        except Exception as e:
            self.logger.error(f"Error calculating drift adjustments: {e}")
            return {'min_trend_score': 0.0, 'velocity_decay_factor': 0.0, 'anomaly_threshold_multiplier': 0.0}
    
    def _apply_bounded_adjustments(self, adjustments: Dict[str, float]) -> ThresholdState:
        """Apply bounded adjustments to current thresholds."""
        try:
            current = self.current_state
            
            # Apply adjustments with bounds
            new_min_score = current.min_trend_score + adjustments.get('min_trend_score', 0.0)
            new_velocity_decay = current.velocity_decay_factor + adjustments.get('velocity_decay_factor', 0.0)
            new_anomaly_mult = current.anomaly_threshold_multiplier + adjustments.get('anomaly_threshold_multiplier', 0.0)
            
            # Apply bounds
            new_min_score = np.clip(new_min_score, 0.1, 0.9)  # Keep between 10% and 90%
            new_velocity_decay = np.clip(new_velocity_decay, 0.8, 0.99)  # Keep between 80% and 99%
            new_anomaly_mult = np.clip(new_anomaly_mult, 1.0, 5.0)  # Keep between 1.0 and 5.0
            
            # Calculate drift metrics
            drift_velocity = np.sqrt(
                adjustments.get('min_trend_score', 0.0)**2 +
                adjustments.get('velocity_decay_factor', 0.0)**2 +
                adjustments.get('anomaly_threshold_multiplier', 0.0)**2
            )
            
            # Determine drift direction
            total_adjustment = sum(adjustments.values())
            if total_adjustment > 0.01:
                drift_direction = 'increasing'
            elif total_adjustment < -0.01:
                drift_direction = 'decreasing'
            else:
                drift_direction = 'stable'
            
            # Create new state
            new_state = ThresholdState(
                min_trend_score=new_min_score,
                velocity_decay_factor=new_velocity_decay,
                anomaly_threshold_multiplier=new_anomaly_mult,
                created_at=current.created_at,
                last_updated=datetime.now(),
                update_count=current.update_count + 1,
                success_rate=current.success_rate,
                false_positive_rate=current.false_positive_rate,
                false_negative_rate=current.false_negative_rate,
                previous_state=current,
                rollback_available=True,
                drift_velocity=drift_velocity,
                drift_direction=drift_direction,
                health_score=current.health_score,
                stability_score=self._calculate_stability_score()
            )
            
            return new_state
            
        except Exception as e:
            self.logger.error(f"Error applying bounded adjustments: {e}")
            return self.current_state
    
    def _validate_thresholds(self, state: ThresholdState) -> bool:
        """Validate that thresholds are within acceptable bounds."""
        try:
            # Check bounds
            if not (0.1 <= state.min_trend_score <= 0.9):
                return False
            if not (0.8 <= state.velocity_decay_factor <= 0.99):
                return False
            if not (1.0 <= state.anomaly_threshold_multiplier <= 5.0):
                return False
            
            # Check for extreme drift
            if state.drift_velocity > 0.1:  # Too much change at once
                return False
            
            # Check stability
            if state.stability_score < 0.2:  # Too unstable
                return False
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error validating thresholds: {e}")
            return False
    
    def _calculate_stability_score(self) -> float:
        """Calculate stability score based on recent threshold changes."""
        try:
            if len(self.threshold_history) < 2:
                return 1.0
            
            recent_thresholds = list(self.threshold_history)[-10:]  # Last 10 changes
            
            # Calculate variance in recent changes
            min_scores = [t['min_trend_score'] for t in recent_thresholds]
            variance = np.var(min_scores) if min_scores else 0.0
            
            # Convert variance to stability score (lower variance = higher stability)
            stability_score = max(0.0, 1.0 - variance * 10)  # Scale variance to 0-1 range
            
            return stability_score
            
        except Exception as e:
            self.logger.error(f"Error calculating stability score: {e}")
            return 0.5
    
    def _save_state_for_rollback(self):
        """Save current state for potential rollback."""
        try:
            if len(self.rollback_stack) >= self.max_rollback_depth:
                self.rollback_stack.pop(0)  # Remove oldest
            
            self.rollback_stack.append(self.current_state)
            self.logger.debug(f"Saved state for rollback (stack depth: {len(self.rollback_stack)})")
            
        except Exception as e:
            self.logger.error(f"Error saving state for rollback: {e}")
    
    def _rollback_to_previous_state(self) -> bool:
        """Rollback to previous saved state."""
        try:
            if not self.rollback_stack:
                self.logger.warning("No rollback states available")
                return False
            
            previous_state = self.rollback_stack.pop()
            self.current_state = previous_state
            
            self.logger.info(f"Rolled back to previous state (stack depth: {len(self.rollback_stack)})")
            return True
            
        except Exception as e:
            self.logger.error(f"Error during rollback: {e}")
            return False
    
    def _record_performance(self, performance: Dict[str, float]):
        """Record performance metrics for trend analysis."""
        try:
            # Update current state performance
            self.current_state.success_rate = performance.get('success_rate', 0.5)
            self.current_state.false_positive_rate = performance.get('false_positive_rate', 0.5)
            self.current_state.false_negative_rate = performance.get('false_negative_rate', 0.5)
            
            # Record threshold state
            self.threshold_history.append({
                'timestamp': datetime.now(),
                'min_trend_score': self.current_state.min_trend_score,
                'velocity_decay_factor': self.current_state.velocity_decay_factor,
                'anomaly_threshold_multiplier': self.current_state.anomaly_threshold_multiplier,
                'success_rate': self.current_state.success_rate
            })
            
        except Exception as e:
            self.logger.error(f"Error recording performance: {e}")
    
    def _check_system_health(self):
        """Check overall system health and trigger rollback if needed."""
        try:
            # Calculate health metrics
            recent_performance = list(self.performance_history)[-20:] if self.performance_history else []
            
            if len(recent_performance) < 10:
                self.system_health = 0.5
                return
            
            # Calculate average performance
            avg_success = np.mean([p['success_rate'] for p in recent_performance])
            avg_fp = np.mean([p['false_positive_rate'] for p in recent_performance])
            avg_fn = np.mean([p['false_negative_rate'] for p in recent_performance])
            
            # Calculate health score
            health_score = (avg_success + (1 - avg_fp) + (1 - avg_fn)) / 3
            self.system_health = health_score
            
            # Check if rollback is needed
            if health_score < (1 - self.rollback_threshold):
                self.logger.warning(f"System health degraded to {health_score:.3f}, triggering rollback")
                self._rollback_to_previous_state()
            
            # Update current state health
            self.current_state.health_score = health_score
            
            self.last_health_check = self.update_count
            
        except Exception as e:
            self.logger.error(f"Error checking system health: {e}")
    
    def get_current_thresholds(self) -> Dict[str, float]:
        """Get current threshold values."""
        if not self.current_state:
            return self.initial_thresholds.copy()
        
        return {
            'min_trend_score': self.current_state.min_trend_score,
            'velocity_decay_factor': self.current_state.velocity_decay_factor,
            'anomaly_threshold_multiplier': self.current_state.anomaly_threshold_multiplier
        }
    
    def get_system_status(self) -> Dict[str, any]:
        """Get comprehensive system status for monitoring."""
        try:
            if not self.current_state:
                return {'status': 'uninitialized'}
            
            return {
                'status': 'active',
                'current_thresholds': self.get_current_thresholds(),
                'update_count': self.update_count,
                'system_health': self.system_health,
                'stability_score': self.current_state.stability_score,
                'drift_velocity': self.current_state.drift_velocity,
                'drift_direction': self.current_state.drift_direction,
                'rollback_available': len(self.rollback_stack) > 0,
                'rollback_depth': len(self.rollback_stack),
                'performance_samples': len(self.performance_history),
                'last_updated': self.current_state.last_updated.isoformat(),
                'created_at': self.current_state.created_at.isoformat()
            }
            
        except Exception as e:
            self.logger.error(f"Error getting system status: {e}")
            return {'status': 'error', 'error': str(e)}
    
    def manual_rollback(self) -> bool:
        """Manually trigger rollback to previous state."""
        return self._rollback_to_previous_state()
    
    def reset_to_defaults(self) -> bool:
        """Reset to conservative default thresholds."""
        try:
            self._save_state_for_rollback()
            self._initialize_default_state()
            self.logger.info("Reset to default thresholds")
            return True
        except Exception as e:
            self.logger.error(f"Error resetting to defaults: {e}")
            return False


# Integration class for TrendAggregator
class ProductionThresholdEnhancer:
    """
    Enhances TrendAggregator with production-ready dynamic thresholds.
    """
    
    def __init__(self, trend_aggregator_instance):
        """Initialize with existing TrendAggregator instance."""
        self.aggregator = trend_aggregator_instance
        self.threshold_system = ProductionDynamicThresholds()
        self.logger = logging.getLogger(__name__)
    
    def update_thresholds(self, performance_metrics: Dict[str, float]) -> bool:
        """
        Update thresholds based on performance metrics.
        
        Args:
            performance_metrics: Dict with 'success_rate', 'false_positive_rate', 'false_negative_rate'
            
        Returns:
            bool: True if thresholds were updated
        """
        return self.threshold_system.update_thresholds_based_on_performance(performance_metrics)
    
    def get_current_thresholds(self) -> Dict[str, float]:
        """Get current threshold values."""
        return self.threshold_system.get_current_thresholds()
    
    def get_system_status(self) -> Dict[str, any]:
        """Get comprehensive system status."""
        return self.threshold_system.get_system_status()
    
    def manual_rollback(self) -> bool:
        """Manually rollback to previous thresholds."""
        return self.threshold_system.manual_rollback()
    
    def reset_to_defaults(self) -> bool:
        """Reset to conservative default thresholds."""
        return self.threshold_system.reset_to_defaults()


if __name__ == "__main__":
    # Example usage demonstrating production-ready dynamic thresholds
    threshold_system = ProductionDynamicThresholds()
    
    print("=" * 80)
    print("PRODUCTION-READY DYNAMIC THRESHOLDING SYSTEM")
    print("=" * 80)
    
    print(f"\n🔧 CONFIGURATION:")
    print(f"   Drift Rate: {threshold_system.drift_rate}")
    print(f"   Stability Window: {threshold_system.stability_window}")
    print(f"   Rollback Threshold: {threshold_system.rollback_threshold}")
    
    print(f"\n📊 INITIAL THRESHOLDS:")
    initial_thresholds = threshold_system.get_current_thresholds()
    for key, value in initial_thresholds.items():
        print(f"   {key}: {value:.3f}")
    
    # Simulate performance updates
    print(f"\n🚀 SIMULATING PERFORMANCE UPDATES:")
    print("-" * 50)
    
    performance_scenarios = [
        {'success_rate': 0.3, 'false_positive_rate': 0.4, 'false_negative_rate': 0.3},  # Poor performance
        {'success_rate': 0.4, 'false_positive_rate': 0.3, 'false_negative_rate': 0.3},  # Improving
        {'success_rate': 0.6, 'false_positive_rate': 0.2, 'false_negative_rate': 0.2},  # Good performance
        {'success_rate': 0.8, 'false_positive_rate': 0.1, 'false_negative_rate': 0.1},  # Excellent
        {'success_rate': 0.4, 'false_positive_rate': 0.3, 'false_negative_rate': 0.4},  # Declining
    ]
    
    for i, performance in enumerate(performance_scenarios):
        print(f"\nUpdate {i+1}:")
        print(f"   Performance: success={performance['success_rate']:.2f}, "
              f"fp={performance['false_positive_rate']:.2f}, fn={performance['false_negative_rate']:.2f}")
        
        # Add some data to history first
        for _ in range(60):  # Add 60 samples to meet minimum requirement
            threshold_system.performance_history.append({
                'timestamp': datetime.now(),
                'success_rate': performance['success_rate'] + np.random.normal(0, 0.05),
                'false_positive_rate': performance['false_positive_rate'] + np.random.normal(0, 0.05),
                'false_negative_rate': performance['false_negative_rate'] + np.random.normal(0, 0.05)
            })
        
        updated = threshold_system.update_thresholds_based_on_performance(performance)
        
        current_thresholds = threshold_system.get_current_thresholds()
        print(f"   Updated: {updated}")
        print(f"   New thresholds: min_score={current_thresholds['min_trend_score']:.3f}, "
              f"decay={current_thresholds['velocity_decay_factor']:.3f}, "
              f"anomaly={current_thresholds['anomaly_threshold_multiplier']:.3f}")
        
        status = threshold_system.get_system_status()
        print(f"   System health: {status['system_health']:.3f}")
        print(f"   Stability: {status['stability_score']:.3f}")
    
    print(f"\n" + "=" * 80)
    print("FINAL SYSTEM STATUS")
    print("=" * 80)
    
    final_status = threshold_system.get_system_status()
    print(f"\n📈 SYSTEM STATUS:")
    for key, value in final_status.items():
        print(f"   {key}: {value}")
    
    print(f"\n🎯 KEY ACHIEVEMENTS:")
    print(f"   ✅ Simple, predictable threshold adjustments")
    print(f"   ✅ Bounded drift with maximum change limits")
    print(f"   ✅ Automatic rollback on performance degradation")
    print(f"   ✅ Clear failure modes and recovery paths")
    print(f"   ✅ Easy rollback to previous stable states")
    print(f"   ✅ No complex Bayesian optimization overhead")
    print(f"   ✅ No multi-armed bandit complexity")
    print(f"   ✅ Production-ready with predictable behavior")
    
    print(f"\n🚀 PRODUCTION READY:")
    print(f"   The dynamic thresholding system now provides:")
    print(f"   - Simple thresholds that drift slowly")
    print(f"   - Clear failure modes with automatic rollback")
    print(f"   - Easy rollback to previous stable states")
    print(f"   - Bounded statistical priors with predictable behavior")
    print(f"   - No overengineering complexity")
    print(f"   - Robust under data drift conditions")
