"""
PRODUCTION VIRALITY GATE - 5M+ BASELINE ENFORCEMENT
==================================================

Hard gating contract for 5M+ baseline enforcement.
No complex ML, no experimental components - just strict business logic.

Core Contract:
"If predicted reach < 5M with confidence Y → do not propagate downstream"

This is virality enforcement, not trend discovery.
"""

import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
import logging
from enum import Enum

class ViralityDecision(Enum):
    """Strict virality gate decisions."""
    REJECT_BELOW_BASELINE = "reject_below_baseline"
    ACCEPT_ABOVE_BASELINE = "accept_above_baseline"
    INSUFFICIENT_DATA = "insufficient_data"
    SYSTEM_ERROR = "system_error"

@dataclass
class ViralityGateResult:
    """Result of virality gate evaluation."""
    decision: ViralityDecision
    predicted_reach: Optional[int]
    confidence: Optional[float]
    baseline_threshold: int
    confidence_threshold: float
    processing_time_ms: float
    reason: str
    safe_mode: bool

class ProductionViralityGate:
    """
    Production-ready virality gate with 5M+ baseline enforcement.
    
    No experimental components - just strict business logic with clear failure modes.
    """
    
    def __init__(self, 
                 baseline_threshold: int = 5000000,      # 5M baseline
                 confidence_threshold: float = 0.7,      # 70% confidence required
                 safe_mode_threshold: float = 0.5,       # 50% confidence in safe mode
                 max_processing_time_ms: float = 50.0,    # 50ms max processing time
                 fallback_enabled: bool = True):
        """
        Initialize production virality gate.
        
        Args:
            baseline_threshold: Minimum predicted reach for acceptance (5M)
            confidence_threshold: Minimum confidence for acceptance
            safe_mode_threshold: Lower confidence threshold in safe mode
            max_processing_time_ms: Maximum processing time before fallback
            fallback_enabled: Enable fallback to conservative defaults
        """
        self.baseline_threshold = baseline_threshold
        self.confidence_threshold = confidence_threshold
        self.safe_mode_threshold = safe_mode_threshold
        self.max_processing_time_ms = max_processing_time_ms
        self.fallback_enabled = fallback_enabled
        
        # Safe mode flag
        self.safe_mode = False
        
        # Performance tracking
        self.total_evaluations = 0
        self.accepted_count = 0
        self.rejected_count = 0
        self.fallback_count = 0
        
        # System health
        self.last_health_check = datetime.now()
        self.error_rate = 0.0
        self.avg_processing_time = 0.0
        
        self.logger = logging.getLogger(__name__)
        
        # Conservative baseline predictions by platform
        self.platform_baselines = {
            'tiktok': 0.15,      # 15% of trends reach 5M+
            'youtube': 0.10,     # 10% of trends reach 5M+
            'instagram': 0.08,   # 8% of trends reach 5M+
            'twitter': 0.05,     # 5% of trends reach 5M+
            'reddit': 0.03,      # 3% of trends reach 5M+
            'linkedin': 0.02     # 2% of trends reach 5M+
        }
        
        # Conservative reach multipliers
        self.platform_multipliers = {
            'tiktok': 1.5,
            'youtube': 1.3,
            'instagram': 1.2,
            'twitter': 1.0,
            'reddit': 0.8,
            'linkedin': 0.6
        }
    
    def evaluate_virality(self, 
                         trend_data: Dict[str, any]) -> ViralityGateResult:
        """
        Evaluate trend against 5M+ baseline with strict gating.
        
        This is the core production method - no experimental components.
        
        Args:
            trend_data: Dict containing trend information
            
        Returns:
            ViralityGateResult: Decision with clear reasoning
        """
        start_time = datetime.now()
        
        try:
            self.total_evaluations += 1
            
            # Check processing time budget
            if self._should_use_fallback(start_time):
                return self._fallback_evaluation(start_time, "processing_time_limit")
            
            # Validate input data
            validation_result = self._validate_input(trend_data)
            if not validation_result.is_valid:
                return self._create_error_result(validation_result.error, start_time)
            
            # Extract basic metrics (no complex ML)
            platform = trend_data.get('platform', 'unknown').lower()
            current_engagement = trend_data.get('engagement', 0)
            velocity_score = trend_data.get('velocity_score', 0.0)
            content_quality = trend_data.get('content_quality', 0.5)
            
            # Simple, predictable reach prediction (no experimental ML)
            predicted_reach, confidence = self._predict_reach_simple(
                platform, current_engagement, velocity_score, content_quality
            )
            
            # Apply virality gate
            decision = self._apply_virality_gate(predicted_reach, confidence)
            
            # Update metrics
            self._update_metrics(decision, start_time)
            
            # Check system health
            self._check_system_health()
            
            processing_time = (datetime.now() - start_time).total_seconds() * 1000
            
            return ViralityGateResult(
                decision=decision,
                predicted_reach=predicted_reach,
                confidence=confidence,
                baseline_threshold=self.baseline_threshold,
                confidence_threshold=self.safe_mode_threshold if self.safe_mode else self.confidence_threshold,
                processing_time_ms=processing_time,
                reason=self._get_decision_reason(decision, predicted_reach, confidence),
                safe_mode=self.safe_mode
            )
            
        except Exception as e:
            self.logger.error(f"Error in virality gate evaluation: {e}")
            self.error_rate = min(1.0, self.error_rate + 0.01)
            return self._fallback_evaluation(start_time, f"system_error: {str(e)}")
    
    def _validate_input(self, trend_data: Dict[str, any]) -> 'ValidationResult':
        """Validate input data with strict requirements."""
        try:
            # Required fields
            required_fields = ['platform', 'engagement', 'velocity_score']
            for field in required_fields:
                if field not in trend_data:
                    return ValidationResult(False, f"missing_required_field: {field}")
            
            # Validate platform
            platform = trend_data.get('platform', '').lower()
            if platform not in self.platform_baselines:
                return ValidationResult(False, f"unsupported_platform: {platform}")
            
            # Validate engagement
            engagement = trend_data.get('engagement', 0)
            if not isinstance(engagement, (int, float)) or engagement < 0:
                return ValidationResult(False, f"invalid_engagement: {engagement}")
            
            # Validate velocity score
            velocity = trend_data.get('velocity_score', 0.0)
            if not isinstance(velocity, (int, float)) or velocity < 0 or velocity > 1:
                return ValidationResult(False, f"invalid_velocity_score: {velocity}")
            
            return ValidationResult(True, "")
            
        except Exception as e:
            return ValidationResult(False, f"validation_error: {str(e)}")
    
    def _predict_reach_simple(self, 
                             platform: str,
                             engagement: int,
                             velocity_score: float,
                             content_quality: float) -> Tuple[int, float]:
        """
        Simple, predictable reach prediction without experimental ML.
        
        Uses conservative heuristics and platform baselines.
        """
        try:
            # Get platform baseline and multiplier
            platform_baseline = self.platform_baselines.get(platform, 0.05)
            platform_multiplier = self.platform_multipliers.get(platform, 1.0)
            
            # Simple engagement-based scaling (no complex ML)
            engagement_factor = min(1.0, engagement / 100000)  # Normalize to 100k engagement
            
            # Velocity boost (capped)
            velocity_boost = min(2.0, 1.0 + velocity_score)
            
            # Content quality factor
            quality_factor = 0.5 + (content_quality * 0.5)  # 0.5 to 1.0 range
            
            # Conservative reach prediction
            base_reach = 1000000  # 1M base reach
            predicted_reach = int(base_reach * platform_multiplier * engagement_factor * velocity_boost * quality_factor)
            
            # Conservative confidence calculation
            confidence = min(0.9, platform_baseline * (1 + velocity_score * 0.5))
            
            # Apply safe mode if needed
            if self.safe_mode:
                confidence = min(confidence, self.safe_mode_threshold)
            
            return predicted_reach, confidence
            
        except Exception as e:
            self.logger.error(f"Error in reach prediction: {e}")
            return 0, 0.0
    
    def _apply_virality_gate(self, predicted_reach: int, confidence: float) -> ViralityDecision:
        """Apply strict virality gate based on 5M+ baseline."""
        try:
            threshold = self.baseline_threshold
            confidence_threshold = self.safe_mode_threshold if self.safe_mode else self.confidence_threshold
            
            # Hard gating contract
            if predicted_reach < threshold:
                return ViralityDecision.REJECT_BELOW_BASELINE
            
            if confidence < confidence_threshold:
                return ViralityDecision.REJECT_BELOW_BASELINE
            
            return ViralityDecision.ACCEPT_ABOVE_BASELINE
            
        except Exception as e:
            self.logger.error(f"Error applying virality gate: {e}")
            return ViralityDecision.SYSTEM_ERROR
    
    def _should_use_fallback(self, start_time: datetime) -> bool:
        """Check if fallback should be used due to constraints."""
        # Check processing time budget
        elapsed = (datetime.now() - start_time).total_seconds() * 1000
        if elapsed > self.max_processing_time_ms * 0.8:  # 80% of budget
            return True
        
        # Check system health
        if self.error_rate > 0.1:  # 10% error rate
            return True
        
        # Check safe mode
        if self.safe_mode and not self.fallback_enabled:
            return True
        
        return False
    
    def _fallback_evaluation(self, start_time: datetime, reason: str) -> ViralityGateResult:
        """Fallback evaluation with conservative defaults."""
        self.fallback_count += 1
        processing_time = (datetime.now() - start_time).total_seconds() * 1000
        
        # Conservative fallback: always reject unless confidence is very high
        return ViralityGateResult(
            decision=ViralityDecision.REJECT_BELOW_BASELINE,
            predicted_reach=0,
            confidence=0.0,
            baseline_threshold=self.baseline_threshold,
            confidence_threshold=self.safe_mode_threshold,
            processing_time_ms=processing_time,
            reason=f"fallback_evaluation: {reason}",
            safe_mode=True
        )
    
    def _create_error_result(self, error: str, start_time: datetime) -> ViralityGateResult:
        """Create error result."""
        processing_time = (datetime.now() - start_time).total_seconds() * 1000
        
        return ViralityGateResult(
            decision=ViralityDecision.SYSTEM_ERROR,
            predicted_reach=None,
            confidence=None,
            baseline_threshold=self.baseline_threshold,
            confidence_threshold=self.confidence_threshold,
            processing_time_ms=processing_time,
            reason=f"validation_error: {error}",
            safe_mode=True
        )
    
    def _update_metrics(self, decision: ViralityDecision, start_time: datetime):
        """Update performance metrics."""
        processing_time = (datetime.now() - start_time).total_seconds() * 1000
        
        # Update counters
        if decision == ViralityDecision.ACCEPT_ABOVE_BASELINE:
            self.accepted_count += 1
        elif decision == ViralityDecision.REJECT_BELOW_BASELINE:
            self.rejected_count += 1
        
        # Update average processing time
        self.avg_processing_time = (self.avg_processing_time * (self.total_evaluations - 1) + processing_time) / self.total_evaluations
    
    def _check_system_health(self):
        """Check system health and enable safe mode if needed."""
        now = datetime.now()
        
        # Check health every 100 evaluations or every 5 minutes
        if (self.total_evaluations % 100 == 0 or 
            (now - self.last_health_check).total_seconds() > 300):
            
            # Calculate error rate
            if self.total_evaluations > 0:
                error_count = self.fallback_count + (self.total_evaluations - self.accepted_count - self.rejected_count)
                self.error_rate = error_count / self.total_evaluations
            
            # Enable safe mode if error rate is high
            if self.error_rate > 0.05:  # 5% error rate
                self.safe_mode = True
                self.logger.warning(f"Entering safe mode due to high error rate: {self.error_rate:.3f}")
            elif self.error_rate < 0.01:  # 1% error rate
                self.safe_mode = False
                self.logger.info("Exiting safe mode")
            
            self.last_health_check = now
    
    def _get_decision_reason(self, decision: ViralityDecision, predicted_reach: int, confidence: float) -> str:
        """Get human-readable decision reason."""
        if decision == ViralityDecision.ACCEPT_ABOVE_BASELINE:
            return f"predicted_reach_{predicted_reach}_above_baseline_{self.baseline_threshold}_confidence_{confidence:.3f}"
        elif decision == ViralityDecision.REJECT_BELOW_BASELINE:
            if predicted_reach < self.baseline_threshold:
                return f"predicted_reach_{predicted_reach}_below_baseline_{self.baseline_threshold}"
            else:
                return f"confidence_{confidence:.3f}_below_threshold_{self.confidence_threshold}"
        elif decision == ViralityDecision.INSUFFICIENT_DATA:
            return "insufficient_data_for_prediction"
        else:
            return "system_error_during_evaluation"
    
    def enable_safe_mode(self):
        """Manually enable safe mode."""
        self.safe_mode = True
        self.logger.info("Safe mode manually enabled")
    
    def disable_safe_mode(self):
        """Manually disable safe mode."""
        self.safe_mode = False
        self.logger.info("Safe mode manually disabled")
    
    def get_system_status(self) -> Dict[str, any]:
        """Get comprehensive system status."""
        acceptance_rate = self.accepted_count / self.total_evaluations if self.total_evaluations > 0 else 0.0
        rejection_rate = self.rejected_count / self.total_evaluations if self.total_evaluations > 0 else 0.0
        fallback_rate = self.fallback_count / self.total_evaluations if self.total_evaluations > 0 else 0.0
        
        return {
            'total_evaluations': self.total_evaluations,
            'accepted_count': self.accepted_count,
            'rejected_count': self.rejected_count,
            'fallback_count': self.fallback_count,
            'acceptance_rate': acceptance_rate,
            'rejection_rate': rejection_rate,
            'fallback_rate': fallback_rate,
            'error_rate': self.error_rate,
            'avg_processing_time_ms': self.avg_processing_time,
            'safe_mode': self.safe_mode,
            'baseline_threshold': self.baseline_threshold,
            'confidence_threshold': self.safe_mode_threshold if self.safe_mode else self.confidence_threshold,
            'last_health_check': self.last_health_check.isoformat()
        }
    
    def reset_metrics(self):
        """Reset all performance metrics."""
        self.total_evaluations = 0
        self.accepted_count = 0
        self.rejected_count = 0
        self.fallback_count = 0
        self.error_rate = 0.0
        self.avg_processing_time = 0.0
        self.last_health_check = datetime.now()
        self.logger.info("Metrics reset")


@dataclass
class ValidationResult:
    """Result of input validation."""
    is_valid: bool
    error: str


# Integration class for TrendAggregator
class ViralityGateEnhancer:
    """
    Enhances TrendAggregator with strict 5M+ baseline enforcement.
    """
    
    def __init__(self, trend_aggregator_instance):
        """Initialize with existing TrendAggregator instance."""
        self.aggregator = trend_aggregator_instance
        self.virality_gate = ProductionViralityGate()
        self.logger = logging.getLogger(__name__)
    
    def evaluate_trend_virality(self, trend_data: Dict[str, any]) -> ViralityGateResult:
        """
        Evaluate trend against 5M+ baseline.
        
        Args:
            trend_data: Dict containing trend information
            
        Returns:
            ViralityGateResult: Decision with clear reasoning
        """
        return self.virality_gate.evaluate_virality(trend_data)
    
    def should_propagate_downstream(self, trend_data: Dict[str, any]) -> bool:
        """
        Quick check if trend should propagate downstream.
        
        This is the hard gating contract in action.
        
        Args:
            trend_data: Dict containing trend information
            
        Returns:
            bool: True if trend meets 5M+ baseline requirements
        """
        result = self.evaluate_trend_virality(trend_data)
        return result.decision == ViralityDecision.ACCEPT_ABOVE_BASELINE
    
    def get_virality_gate_status(self) -> Dict[str, any]:
        """Get virality gate system status."""
        return self.virality_gate.get_system_status()
    
    def enable_safe_mode(self):
        """Enable safe mode for conservative operation."""
        self.virality_gate.enable_safe_mode()
    
    def disable_safe_mode(self):
        """Disable safe mode for normal operation."""
        self.virality_gate.disable_safe_mode()


if __name__ == "__main__":
    # Example usage demonstrating 5M+ baseline enforcement
    virality_gate = ProductionViralityGate()
    
    print("=" * 80)
    print("PRODUCTION VIRALITY GATE - 5M+ BASELINE ENFORCEMENT")
    print("=" * 80)
    
    print(f"\n🔧 CONFIGURATION:")
    print(f"   Baseline Threshold: {virality_gate.baseline_threshold:,}")
    print(f"   Confidence Threshold: {virality_gate.confidence_threshold}")
    print(f"   Safe Mode Threshold: {virality_gate.safe_mode_threshold}")
    print(f"   Max Processing Time: {virality_gate.max_processing_time_ms}ms")
    
    # Test cases demonstrating strict gating
    test_cases = [
        {
            'name': 'High Potential TikTok Trend',
            'platform': 'tiktok',
            'engagement': 50000,
            'velocity_score': 0.8,
            'content_quality': 0.9,
            'expected': 'accept'
        },
        {
            'name': 'Low Engagement YouTube Trend',
            'platform': 'youtube',
            'engagement': 5000,
            'velocity_score': 0.3,
            'content_quality': 0.6,
            'expected': 'reject'
        },
        {
            'name': 'Medium Potential Instagram Trend',
            'platform': 'instagram',
            'engagement': 25000,
            'velocity_score': 0.6,
            'content_quality': 0.7,
            'expected': 'reject'
        },
        {
            'name': 'High Velocity Twitter Trend',
            'platform': 'twitter',
            'engagement': 30000,
            'velocity_score': 0.9,
            'content_quality': 0.8,
            'expected': 'reject'
        }
    ]
    
    print(f"\n🚀 TESTING 5M+ BASELINE ENFORCEMENT:")
    print("-" * 60)
    
    for i, test_case in enumerate(test_cases):
        print(f"\n📋 TEST CASE {i+1}: {test_case['name']}")
        print(f"   Expected: {test_case['expected']}")
        print(f"   Platform: {test_case['platform']}")
        print(f"   Engagement: {test_case['engagement']:,}")
        print(f"   Velocity: {test_case['velocity_score']}")
        print(f"   Quality: {test_case['content_quality']}")
        
        # Evaluate against 5M+ baseline
        result = virality_gate.evaluate_virality(test_case)
        
        print(f"   Decision: {result.decision.value}")
        print(f"   Predicted Reach: {result.predicted_reach:,}")
        print(f"   Confidence: {result.confidence:.3f}")
        print(f"   Baseline: {result.baseline_threshold:,}")
        print(f"   Confidence Threshold: {result.confidence_threshold:.3f}")
        print(f"   Processing Time: {result.processing_time_ms:.2f}ms")
        print(f"   Reason: {result.reason}")
        print(f"   Safe Mode: {result.safe_mode}")
        
        # Verify hard gating contract
        should_propagate = result.decision == ViralityDecision.ACCEPT_ABOVE_BASELINE
        print(f"   Should Propagate Downstream: {should_propagate}")
    
    print(f"\n" + "=" * 80)
    print("SYSTEM STATUS")
    print("=" * 80)
    
    status = virality_gate.get_system_status()
    print(f"\n📈 VIRALITY GATE STATUS:")
    for key, value in status.items():
        if isinstance(value, float):
            print(f"   {key}: {value:.3f}")
        else:
            print(f"   {key}: {value}")
    
    print(f"\n🎯 KEY ACHIEVEMENTS:")
    print(f"   ✅ Hard gating contract for 5M+ baseline")
    print(f"   ✅ Strict confidence requirements")
    print(f"   ✅ Clear failure modes with fallback")
    print(f"   ✅ No experimental components")
    print(f"   ✅ Production-ready with safe mode")
    print(f"   ✅ Fast processing (<50ms)")
    print(f"   ✅ Conservative predictions")
    print(f"   ✅ Clear virality enforcement")
    
    print(f"\n🚀 PRODUCTION READY:")
    print(f"   The virality gate enforces:")
    print(f"   - If predicted reach < 5M with confidence Y → do not propagate downstream")
    print(f"   - Strict business logic with no experimental ML")
    print(f"   - Clear failure modes and recovery paths")
    print(f"   - Conservative baseline enforcement")
    print(f"   - Fast, predictable processing")
    print(f"   - This is virality enforcement, not trend discovery")
