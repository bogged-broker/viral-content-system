"""
/generation/retention_optimizer.py

Maximizes viewer retention across video lifecycle, segment by segment.
Operates after storyboard/emotional arc finalization, before audio/visual composition.

Core Principle: Retention drives virality more than early engagement spikes.

Inputs: emotional arc, script timing, feature signals, historical retention patterns
Outputs: retention-adjusted segment instructions for downstream generators

Does NOT: create visuals/audio, predict virality, rank content, extract features
"""

from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
import json
import logging
from datetime import datetime
import numpy as np
from collections import defaultdict


# ============================================================================
# CONFIGURATION & CONSTANTS
# ============================================================================

class RetentionRiskLevel(Enum):
    """Retention risk categorization"""
    CRITICAL = "critical"  # > 0.7
    HIGH = "high"          # 0.5 - 0.7
    MODERATE = "moderate"  # 0.3 - 0.5
    LOW = "low"            # < 0.3


@dataclass
class RetentionConfig:
    """Configuration for retention optimization"""
    version: str = "1.0.0"
    
    # Risk thresholds
    critical_risk_threshold: float = 0.7
    high_risk_threshold: float = 0.5
    moderate_risk_threshold: float = 0.3
    
    # Adjustment bounds
    max_trim_seconds: float = 5.0
    max_emphasis_boost: float = 0.5
    min_segment_duration: float = 2.0
    
    # Optimization weights
    historical_weight: float = 0.4
    velocity_weight: float = 0.3
    emotional_arc_weight: float = 0.2
    cross_modal_weight: float = 0.1
    
    # Niche-specific
    enable_niche_rules: bool = True
    
    # Validation
    strict_validation: bool = True
    allow_emotional_arc_modification: bool = False  # LOCKED: False


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class SegmentAdjustment:
    """Recommended adjustments for a segment"""
    trim_seconds: float = 0.0
    emphasis_boost: float = 0.0
    pause_reduction: bool = False
    pacing_multiplier: float = 1.0
    visual_intensity_boost: float = 0.0
    audio_emphasis: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "trim_seconds": round(self.trim_seconds, 2),
            "emphasis_boost": round(self.emphasis_boost, 3),
            "pause_reduction": self.pause_reduction,
            "pacing_multiplier": round(self.pacing_multiplier, 3),
            "visual_intensity_boost": round(self.visual_intensity_boost, 3),
            "audio_emphasis": self.audio_emphasis
        }


@dataclass
class SegmentRetentionAnalysis:
    """Analysis results for a single segment"""
    segment_id: str
    start: float
    end: float
    duration: float
    
    # Risk assessment
    retention_risk_score: float
    risk_level: RetentionRiskLevel
    
    # Contributing factors
    historical_drop_prob: float
    velocity_risk: float
    emotional_misalignment: float
    cross_modal_synergy: float
    
    # Recommendations
    suggested_adjustments: SegmentAdjustment
    
    # Metadata
    confidence: float
    niche_adjusted: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "duration": round(self.duration, 2),
            "retention_risk_score": round(self.retention_risk_score, 4),
            "risk_level": self.risk_level.value,
            "contributing_factors": {
                "historical_drop_prob": round(self.historical_drop_prob, 4),
                "velocity_risk": round(self.velocity_risk, 4),
                "emotional_misalignment": round(self.emotional_misalignment, 4),
                "cross_modal_synergy": round(self.cross_modal_synergy, 4)
            },
            "suggested_adjustments": self.suggested_adjustments.to_dict(),
            "confidence": round(self.confidence, 4),
            "niche_adjusted": self.niche_adjusted
        }


@dataclass
class OptimizationOutput:
    """Complete output from retention optimizer"""
    video_id: str
    optimizer_version: str
    timestamp: str
    
    segments: List[SegmentRetentionAnalysis]
    
    validation: Dict[str, bool]
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "video_id": self.video_id,
            "optimizer_version": self.optimizer_version,
            "timestamp": self.timestamp,
            "segments": [s.to_dict() for s in self.segments],
            "validation": self.validation,
            "metadata": self.metadata
        }
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


# ============================================================================
# INPUT VALIDATOR
# ============================================================================

class InputValidator:
    """Validates and sanitizes inputs to retention optimizer"""
    
    def __init__(self, config: RetentionConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)
    
    def validate_all(
        self,
        video_id: str,
        storyboard_segments: List[Dict[str, Any]],
        emotional_arc: Dict[str, Any],
        script_timing: Dict[str, Any],
        feature_bundle: Dict[str, Any],
        historical_retention_curves: Dict[str, Any],
        niche_rules: Dict[str, Any],
        cross_modal_signals: Dict[str, Any]
    ) -> Tuple[bool, List[str]]:
        """
        Validate all inputs
        Returns: (is_valid, error_messages)
        """
        errors = []
        
        # Video ID
        if not video_id or not isinstance(video_id, str):
            errors.append("Invalid video_id")
        
        # Storyboard segments
        if not storyboard_segments or not isinstance(storyboard_segments, list):
            errors.append("storyboard_segments must be non-empty list")
        else:
            for i, seg in enumerate(storyboard_segments):
                if not self._validate_segment(seg):
                    errors.append(f"Invalid segment at index {i}")
        
        # Emotional arc
        if not self._validate_emotional_arc(emotional_arc):
            errors.append("Invalid emotional_arc structure")
        
        # Script timing
        if not self._validate_script_timing(script_timing):
            errors.append("Invalid script_timing structure")
        
        # Feature bundle
        if not isinstance(feature_bundle, dict):
            errors.append("feature_bundle must be dict")
        
        # Historical retention curves
        if not isinstance(historical_retention_curves, dict):
            errors.append("historical_retention_curves must be dict")
        
        # Niche rules
        if not isinstance(niche_rules, dict):
            errors.append("niche_rules must be dict")
        
        # Cross-modal signals
        if not isinstance(cross_modal_signals, dict):
            errors.append("cross_modal_signals must be dict")
        
        is_valid = len(errors) == 0
        return is_valid, errors
    
    def _validate_segment(self, segment: Dict[str, Any]) -> bool:
        """Validate individual segment structure"""
        required_fields = ["segment_id", "start", "end"]
        if not all(f in segment for f in required_fields):
            return False
        
        try:
            start = float(segment["start"])
            end = float(segment["end"])
            if start < 0 or end <= start:
                return False
        except (ValueError, TypeError):
            return False
        
        return True
    
    def _validate_emotional_arc(self, arc: Dict[str, Any]) -> bool:
        """Validate emotional arc structure"""
        if not isinstance(arc, dict):
            return False
        
        # Check for required components
        if "peaks" not in arc and "timeline" not in arc:
            return False
        
        return True
    
    def _validate_script_timing(self, timing: Dict[str, Any]) -> bool:
        """Validate script timing structure"""
        if not isinstance(timing, dict):
            return False
        
        # Should have timing information per segment
        return True


# ============================================================================
# SEGMENT ANALYZER
# ============================================================================

class SegmentAnalyzer:
    """Maps storyboard segments to fine-grained retention units"""
    
    def __init__(self, config: RetentionConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)
    
    def analyze_segments(
        self,
        storyboard_segments: List[Dict[str, Any]],
        script_timing: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Process storyboard segments into analyzable units
        Maintains segment order & timestamps
        """
        analyzed = []
        
        for seg in storyboard_segments:
            segment_id = seg.get("segment_id", f"seg_{len(analyzed)}")
            start = float(seg["start"])
            end = float(seg["end"])
            duration = end - start
            
            # Extract script timing for this segment
            script_words = self._get_segment_script_info(
                segment_id, script_timing
            )
            
            analyzed_seg = {
                "segment_id": segment_id,
                "start": start,
                "end": end,
                "duration": duration,
                "script_words": script_words,
                "original_segment": seg
            }
            
            analyzed.append(analyzed_seg)
        
        return analyzed
    
    def _get_segment_script_info(
        self,
        segment_id: str,
        script_timing: Dict[str, Any]
    ) -> int:
        """Extract script word count for segment"""
        if "segments" in script_timing:
            for seg in script_timing["segments"]:
                if seg.get("segment_id") == segment_id:
                    return seg.get("word_count", 0)
        
        return 0


# ============================================================================
# RETENTION PREDICTOR
# ============================================================================

class RetentionPredictor:
    """Predicts drop-off probabilities per segment"""
    
    def __init__(self, config: RetentionConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)
    
    def predict_segment_retention(
        self,
        segment: Dict[str, Any],
        historical_curves: Dict[str, Any],
        feature_bundle: Dict[str, Any],
        emotional_arc: Dict[str, Any],
        cross_modal_signals: Dict[str, Any],
        segment_index: int,
        total_segments: int
    ) -> Tuple[float, Dict[str, float]]:
        """
        Predict retention risk for a segment
        Returns: (risk_score, factor_contributions)
        """
        
        # Component risk scores
        historical_risk = self._compute_historical_risk(
            segment, historical_curves, segment_index, total_segments
        )
        
        velocity_risk = self._compute_velocity_risk(
            segment, feature_bundle, segment_index
        )
        
        emotional_risk = self._compute_emotional_misalignment(
            segment, emotional_arc
        )
        
        cross_modal_risk = self._compute_cross_modal_risk(
            segment, cross_modal_signals
        )
        
        # Weighted combination
        risk_score = (
            self.config.historical_weight * historical_risk +
            self.config.velocity_weight * velocity_risk +
            self.config.emotional_arc_weight * emotional_risk +
            self.config.cross_modal_weight * cross_modal_risk
        )
        
        # Ensure bounded [0, 1]
        risk_score = np.clip(risk_score, 0.0, 1.0)
        
        factors = {
            "historical_drop_prob": historical_risk,
            "velocity_risk": velocity_risk,
            "emotional_misalignment": emotional_risk,
            "cross_modal_synergy": 1.0 - cross_modal_risk  # Invert for synergy
        }
        
        return risk_score, factors
    
    def _compute_historical_risk(
        self,
        segment: Dict[str, Any],
        historical_curves: Dict[str, Any],
        segment_index: int,
        total_segments: int
    ) -> float:
        """Compute risk based on historical retention curves"""
        
        # Position-based risk (intro/outro typically higher risk)
        position_factor = 0.0
        if segment_index == 0:  # Intro
            position_factor = 0.3
        elif segment_index == total_segments - 1:  # Outro
            position_factor = 0.2
        else:
            # Middle segments: lower base risk
            position_factor = 0.1
        
        # Duration-based risk (longer segments = higher risk)
        duration = segment.get("duration", 0)
        duration_factor = min(duration / 30.0, 0.4)  # Cap at 30s
        
        # Historical pattern matching
        historical_factor = 0.0
        if "average_retention_curve" in historical_curves:
            curve = historical_curves["average_retention_curve"]
            if isinstance(curve, list) and len(curve) > segment_index:
                # Lower retention = higher risk
                retention_at_segment = curve[segment_index]
                historical_factor = 1.0 - retention_at_segment
        
        # Combine factors
        risk = (
            0.4 * position_factor +
            0.3 * duration_factor +
            0.3 * historical_factor
        )
        
        return np.clip(risk, 0.0, 1.0)
    
    def _compute_velocity_risk(
        self,
        segment: Dict[str, Any],
        feature_bundle: Dict[str, Any],
        segment_index: int
    ) -> float:
        """Compute risk based on early engagement velocity"""
        
        # Early segments have more velocity impact
        velocity_relevance = max(0, 1.0 - segment_index * 0.1)
        
        # Extract velocity signals from features
        base_risk = 0.2
        
        if "early_engagement" in feature_bundle:
            engagement = feature_bundle["early_engagement"]
            
            # Low early engagement = higher risk
            if "velocity_score" in engagement:
                velocity = engagement["velocity_score"]
                # Assuming velocity_score is [0, 1], higher is better
                base_risk = 1.0 - velocity
        
        risk = base_risk * velocity_relevance
        return np.clip(risk, 0.0, 1.0)
    
    def _compute_emotional_misalignment(
        self,
        segment: Dict[str, Any],
        emotional_arc: Dict[str, Any]
    ) -> float:
        """
        Compute risk from emotional arc misalignment
        Low engagement during emotional peaks = high risk
        """
        
        segment_start = segment.get("start", 0)
        segment_end = segment.get("end", 0)
        segment_mid = (segment_start + segment_end) / 2.0
        
        # Check if segment aligns with emotional peak
        is_peak = False
        if "peaks" in emotional_arc:
            for peak in emotional_arc["peaks"]:
                peak_time = peak.get("time", -1)
                if segment_start <= peak_time <= segment_end:
                    is_peak = True
                    break
        
        # Check emotional intensity at segment
        intensity = self._get_emotional_intensity(segment_mid, emotional_arc)
        
        # High intensity without peak alignment = risk
        # Low intensity = baseline risk
        if is_peak:
            # Peak aligned = low risk
            risk = 0.1
        else:
            if intensity > 0.7:
                # High intensity without peak = misalignment risk
                risk = 0.5
            else:
                # Normal flow
                risk = 0.2 + (0.2 * (1.0 - intensity))
        
        return np.clip(risk, 0.0, 1.0)
    
    def _get_emotional_intensity(
        self,
        time: float,
        emotional_arc: Dict[str, Any]
    ) -> float:
        """Get emotional intensity at given time"""
        
        if "timeline" in emotional_arc:
            timeline = emotional_arc["timeline"]
            if isinstance(timeline, list):
                for point in timeline:
                    if abs(point.get("time", -1) - time) < 2.0:
                        return point.get("intensity", 0.5)
        
        return 0.5  # Default moderate intensity
    
    def _compute_cross_modal_risk(
        self,
        segment: Dict[str, Any],
        cross_modal_signals: Dict[str, Any]
    ) -> float:
        """
        Compute risk from cross-modal misalignment
        Poor audio-visual-narrative sync = higher risk
        """
        
        segment_id = segment.get("segment_id", "")
        
        # Look for cross-modal synergy scores
        base_synergy = 0.5  # Neutral
        
        if "segment_synergy" in cross_modal_signals:
            synergy_map = cross_modal_signals["segment_synergy"]
            if segment_id in synergy_map:
                base_synergy = synergy_map[segment_id]
        
        # Convert synergy to risk (low synergy = high risk)
        risk = 1.0 - base_synergy
        
        return np.clip(risk, 0.0, 1.0)


# ============================================================================
# ADJUSTMENT RECOMMENDER
# ============================================================================

class AdjustmentRecommender:
    """Suggests actionable segment tweaks to improve retention"""
    
    def __init__(self, config: RetentionConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)
    
    def recommend_adjustments(
        self,
        segment: Dict[str, Any],
        risk_score: float,
        risk_factors: Dict[str, float]
    ) -> SegmentAdjustment:
        """
        Generate adjustment recommendations based on risk analysis
        """
        
        adjustment = SegmentAdjustment()
        
        # Trim recommendations (for high-risk segments)
        if risk_score > self.config.high_risk_threshold:
            duration = segment.get("duration", 0)
            
            # Trim based on risk severity
            trim_ratio = min((risk_score - 0.5) / 0.5, 1.0)
            suggested_trim = min(
                duration * 0.2 * trim_ratio,
                self.config.max_trim_seconds
            )
            
            # Ensure minimum duration
            if duration - suggested_trim >= self.config.min_segment_duration:
                adjustment.trim_seconds = suggested_trim
        
        # Emphasis boost (for moderate-risk segments)
        if self.config.moderate_risk_threshold < risk_score < self.config.high_risk_threshold:
            emphasis_ratio = (risk_score - 0.3) / 0.2
            adjustment.emphasis_boost = min(
                emphasis_ratio * 0.3,
                self.config.max_emphasis_boost
            )
        
        # Pause reduction (for high historical drop-off)
        if risk_factors.get("historical_drop_prob", 0) > 0.5:
            adjustment.pause_reduction = True
        
        # Pacing adjustments
        if risk_score > 0.6:
            # Speed up high-risk segments
            adjustment.pacing_multiplier = 1.1 + (risk_score - 0.6) * 0.5
        elif risk_score < 0.2:
            # Can slow down low-risk segments slightly
            adjustment.pacing_multiplier = 0.95
        
        # Visual intensity boost (for low cross-modal synergy)
        cross_modal_synergy = risk_factors.get("cross_modal_synergy", 0.5)
        if cross_modal_synergy < 0.4:
            adjustment.visual_intensity_boost = 0.3 * (0.4 - cross_modal_synergy)
        
        # Audio emphasis (for emotional misalignment)
        emotional_misalignment = risk_factors.get("emotional_misalignment", 0)
        if emotional_misalignment > 0.5:
            adjustment.audio_emphasis = True
        
        return adjustment


# ============================================================================
# CROSS-MODAL ALIGNER
# ============================================================================

class CrossModalAligner:
    """Ensures emotional arc peaks align with retention targets"""
    
    def __init__(self, config: RetentionConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)
    
    def align_adjustments(
        self,
        segments: List[SegmentRetentionAnalysis],
        emotional_arc: Dict[str, Any]
    ) -> List[SegmentRetentionAnalysis]:
        """
        Adjust recommendations to maintain emotional arc alignment
        NEVER modifies emotional arc itself
        """
        
        # Identify emotional peak segments
        peak_segments = self._identify_peak_segments(segments, emotional_arc)
        
        # Adjust non-peak segments to protect peaks
        for i, seg in enumerate(segments):
            if seg.segment_id in peak_segments:
                # Protect emotional peaks: reduce aggressive adjustments
                if seg.suggested_adjustments.trim_seconds > 2.0:
                    seg.suggested_adjustments.trim_seconds *= 0.5
                
                # Boost emphasis at peaks
                seg.suggested_adjustments.emphasis_boost = max(
                    seg.suggested_adjustments.emphasis_boost,
                    0.2
                )
            else:
                # Non-peak segments can be adjusted more aggressively
                # if they're between peaks
                if self._is_between_peaks(i, peak_segments, segments):
                    # Transition segment: moderate adjustments
                    seg.suggested_adjustments.pacing_multiplier *= 1.05
        
        return segments
    
    def _identify_peak_segments(
        self,
        segments: List[SegmentRetentionAnalysis],
        emotional_arc: Dict[str, Any]
    ) -> set:
        """Identify which segments contain emotional peaks"""
        
        peak_segment_ids = set()
        
        if "peaks" not in emotional_arc:
            return peak_segment_ids
        
        for peak in emotional_arc["peaks"]:
            peak_time = peak.get("time", -1)
            if peak_time < 0:
                continue
            
            # Find segment containing this peak
            for seg in segments:
                if seg.start <= peak_time <= seg.end:
                    peak_segment_ids.add(seg.segment_id)
                    break
        
        return peak_segment_ids
    
    def _is_between_peaks(
        self,
        segment_index: int,
        peak_segments: set,
        segments: List[SegmentRetentionAnalysis]
    ) -> bool:
        """Check if segment is between two emotional peaks"""
        
        has_peak_before = False
        has_peak_after = False
        
        for i in range(segment_index):
            if segments[i].segment_id in peak_segments:
                has_peak_before = True
                break
        
        for i in range(segment_index + 1, len(segments)):
            if segments[i].segment_id in peak_segments:
                has_peak_after = True
                break
        
        return has_peak_before and has_peak_after


# ============================================================================
# NICHE RETENTION INTEGRATOR
# ============================================================================

class NicheRetentionIntegrator:
    """Applies per-niche retention rules"""
    
    def __init__(self, config: RetentionConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)
    
    def apply_niche_rules(
        self,
        segments: List[SegmentRetentionAnalysis],
        niche_rules: Dict[str, Any]
    ) -> List[SegmentRetentionAnalysis]:
        """
        Apply niche-specific retention optimization rules
        Prevents generic adjustments that harm niche-specific retention
        """
        
        if not self.config.enable_niche_rules:
            return segments
        
        niche_type = niche_rules.get("niche_type", "generic")
        
        # Apply niche-specific adjustments
        if niche_type == "educational":
            segments = self._apply_educational_rules(segments, niche_rules)
        elif niche_type == "entertainment":
            segments = self._apply_entertainment_rules(segments, niche_rules)
        elif niche_type == "commentary":
            segments = self._apply_commentary_rules(segments, niche_rules)
        elif niche_type == "storytelling":
            segments = self._apply_storytelling_rules(segments, niche_rules)
        
        # Mark segments as niche-adjusted
        for seg in segments:
            seg.niche_adjusted = True
        
        return segments
    
    def _apply_educational_rules(
        self,
        segments: List[SegmentRetentionAnalysis],
        niche_rules: Dict[str, Any]
    ) -> List[SegmentRetentionAnalysis]:
        """Educational content: retain pacing, boost emphasis"""
        
        for seg in segments:
            # Don't over-trim educational content
            if seg.suggested_adjustments.trim_seconds > 3.0:
                seg.suggested_adjustments.trim_seconds = 3.0
            
            # Moderate pacing changes
            if seg.suggested_adjustments.pacing_multiplier > 1.15:
                seg.suggested_adjustments.pacing_multiplier = 1.15
            
            # Boost visual emphasis for explanations
            if seg.retention_risk_score > 0.4:
                seg.suggested_adjustments.visual_intensity_boost += 0.1
        
        return segments
    
    def _apply_entertainment_rules(
        self,
        segments: List[SegmentRetentionAnalysis],
        niche_rules: Dict[str, Any]
    ) -> List[SegmentRetentionAnalysis]:
        """Entertainment: aggressive pacing, quick cuts"""
        
        for seg in segments:
            # Allow more aggressive trimming
            if seg.retention_risk_score > 0.5:
                seg.suggested_adjustments.trim_seconds *= 1.2
            
            # Faster pacing acceptable
            seg.suggested_adjustments.pacing_multiplier *= 1.1
            
            # Emphasis on audio/visual sync
            if seg.cross_modal_synergy < 0.5:
                seg.suggested_adjustments.audio_emphasis = True
        
        return segments
    
    def _apply_commentary_rules(
        self,
        segments: List[SegmentRetentionAnalysis],
        niche_rules: Dict[str, Any]
    ) -> List[SegmentRetentionAnalysis]:
        """Commentary: protect narrative flow, moderate adjustments"""
        
        for seg in segments:
            # Reduce pause reduction (commentary needs pauses)
            if seg.suggested_adjustments.pause_reduction:
                seg.suggested_adjustments.pause_reduction = False
            
            # Conservative pacing
            if seg.suggested_adjustments.pacing_multiplier > 1.1:
                seg.suggested_adjustments.pacing_multiplier = 1.05
        
        return segments
    
    def _apply_storytelling_rules(
        self,
        segments: List[SegmentRetentionAnalysis],
        niche_rules: Dict[str, Any]
    ) -> List[SegmentRetentionAnalysis]:
        """Storytelling: protect narrative beats, preserve timing"""
        
        for seg in segments:
            # Minimal trimming (preserve story beats)
            if seg.suggested_adjustments.trim_seconds > 2.0:
                seg.suggested_adjustments.trim_seconds = 2.0
            
            # Preserve natural pacing
            seg.suggested_adjustments.pacing_multiplier = min(
                seg.suggested_adjustments.pacing_multiplier,
                1.05
            )
            
            # Boost emphasis at story beats (emotional peaks)
            if seg.emotional_misalignment < 0.2:
                seg.suggested_adjustments.emphasis_boost += 0.15
        
        return segments


# ============================================================================
# OUTPUT FORMATTER
# ============================================================================

class OutputFormatter:
    """Exports optimization results in standardized format"""
    
    def __init__(self, config: RetentionConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)
    
    def format_output(
        self,
        video_id: str,
        segments: List[SegmentRetentionAnalysis],
        validation_results: Dict[str, bool],
        metadata: Optional[Dict[str, Any]] = None
    ) -> OptimizationOutput:
        """Format complete optimization output"""
        
        output = OptimizationOutput(
            video_id=video_id,
            optimizer_version=self.config.version,
            timestamp=datetime.utcnow().isoformat(),
            segments=segments,
            validation=validation_results,
            metadata=metadata or {}
        )
        
        return output


# ============================================================================
# MAIN RETENTION OPTIMIZER
# ============================================================================

class RetentionOptimizer:
    """
    Main retention optimizer class
    
    Maximizes viewer retention across video lifecycle, segment by segment.
    Operates after storyboard/emotional arc finalization.
    """
    
    def __init__(self, config: Optional[RetentionConfig] = None):
        self.config = config or RetentionConfig()
        self.logger = logging.getLogger(__name__)
        
        # Initialize components
        self.validator = InputValidator(self.config)
        self.segment_analyzer = SegmentAnalyzer(self.config)
        self.retention_predictor = RetentionPredictor(self.config)
        self.adjustment_recommender = AdjustmentRecommender(self.config)
        self.cross_modal_aligner = CrossModalAligner(self.config)
        self.niche_integrator = NicheRetentionIntegrator(self.config)
        self.output_formatter = OutputFormatter(self.config)
    
    def optimize(
        self,
        video_id: str,
        storyboard_segments: List[Dict[str, Any]],
        emotional_arc: Dict[str, Any],
        script_timing: Dict[str, Any],
        feature_bundle: Dict[str, Any],
        historical_retention_curves: Dict[str, Any],
        niche_rules: Dict[str, Any],
        cross_modal_signals: Dict[str, Any]
    ) -> OptimizationOutput:
        """
        Main optimization pipeline
        
        Returns: OptimizationOutput with retention-adjusted segment instructions
        """
        
        # Step 1: Validate inputs
        is_valid, errors = self.validator.validate_all(
            video_id=video_id,
            storyboard_segments=storyboard_segments,
            emotional_arc=emotional_arc,
            script_timing=script_timing,
            feature_bundle=feature_bundle,
            historical_retention_curves=historical_retention_curves,
            niche_rules=niche_rules,
            cross_modal_signals=cross_modal_signals
        )
        
        if not is_valid:
            raise ValueError(f"Input validation failed: {', '.join(errors)}")
        
        self.logger.info(f"Optimizing retention for video_id={video_id}")
        
        # Step 2: Analyze segments
        analyzed_segments = self.segment_analyzer.analyze_segments(
            storyboard_segments=storyboard_segments,
            script_timing=script_timing
        )
        
        # Step 3: Predict retention & recommend adjustments
        segment_analyses = []
        total_segments = len(analyzed_segments)
        
        for i, seg in enumerate(analyzed_segments):
            # Predict retention risk
            risk_score, risk_factors = self.retention_predictor.predict_segment_retention(
                segment=seg,
                historical_curves=historical_retention_curves,
                feature_bundle=feature_bundle,
                emotional_arc=emotional_arc,
                cross_modal_signals=cross_modal_signals,
                segment_index=i,
                total_segments=total_segments
            )
            
            # Categorize risk level
            risk_level = self._categorize_risk(risk_score)
            
            # Recommend adjustments
            adjustments = self.adjustment_recommender.recommend_adjustments(
                segment=seg,
                risk_score=risk_score,
                risk_factors=risk_factors
            )
            
            # Compute confidence
            confidence = self._compute_confidence(risk_factors)
            
            # Create analysis object
            analysis = SegmentRetentionAnalysis(
                segment_id=seg["segment_id"],
                start=seg["start"],
                end=seg["end"],
                duration=seg["duration"],
                retention_risk_score=risk_score,
                risk_level=risk_level,
                historical_drop_prob=risk_factors["historical_drop_prob"],
                velocity_risk=risk_factors["velocity_risk"],
                emotional_misalignment=risk_factors["emotional_misalignment"],
                cross_modal_synergy=risk_factors["cross_modal_synergy"],
                suggested_adjustments=adjustments,
                confidence=confidence
            )
            
            segment_analyses.append(analysis)
        
        # Step 4: Cross-modal alignment
        segment_analyses = self.cross_modal_aligner.align_adjustments(
            segments=segment_analyses,
            emotional_arc=emotional_arc
        )
        
        # Step 5: Apply niche-specific rules
        segment_analyses = self.niche_integrator.apply_niche_rules(
            segments=segment_analyses,
            niche_rules=niche_rules
        )
        
        # Step 6: Validate output
        validation_results = self._validate_output(
            segments=segment_analyses,
            emotional_arc=emotional_arc,
            storyboard_segments=storyboard_segments
        )
        
        # Step 7: Format output
        metadata = {
            "total_segments": total_segments,
            "high_risk_segments": sum(1 for s in segment_analyses if s.risk_level in [RetentionRiskLevel.HIGH, RetentionRiskLevel.CRITICAL]),
            "niche_type": niche_rules.get("niche_type", "generic"),
            "config_version": self.config.version
        }
        
        output = self.output_formatter.format_output(
            video_id=video_id,
            segments=segment_analyses,
            validation_results=validation_results,
            metadata=metadata
        )
        
        self.logger.info(f"Optimization complete for video_id={video_id}")
        
        return output
    
    def _categorize_risk(self, risk_score: float) -> RetentionRiskLevel:
        """Categorize risk score into risk level"""
        if risk_score >= self.config.critical_risk_threshold:
            return RetentionRiskLevel.CRITICAL
        elif risk_score >= self.config.high_risk_threshold:
            return RetentionRiskLevel.HIGH
        elif risk_score >= self.config.moderate_risk_threshold:
            return RetentionRiskLevel.MODERATE
        else:
            return RetentionRiskLevel.LOW
    
    def _compute_confidence(self, risk_factors: Dict[str, float]) -> float:
        """Compute confidence in risk assessment"""
        
        # Confidence based on factor variance
        factors = list(risk_factors.values())
        mean = np.mean(factors)
        variance = np.var(factors)
        
        # Low variance = high confidence (factors agree)
        # High variance = low confidence (factors disagree)
        confidence = 1.0 - min(variance, 0.3) / 0.3
        
        return np.clip(confidence, 0.0, 1.0)
    
    def _validate_output(
        self,
        segments: List[SegmentRetentionAnalysis],
        emotional_arc: Dict[str, Any],
        storyboard_segments: List[Dict[str, Any]]
    ) -> Dict[str, bool]:
        """Validate output against invariants"""
        
        validation = {}
        
        # Check pacing integrity
        pacing_ok = True
        for seg in segments:
            adj = seg.suggested_adjustments
            
            # Check trim doesn't violate minimum duration
            if seg.duration - adj.trim_seconds < self.config.min_segment_duration:
                pacing_ok = False
                break
            
            # Check trim doesn't exceed maximum
            if adj.trim_seconds > self.config.max_trim_seconds:
                pacing_ok = False
                break
        
        validation["pacing_ok"] = pacing_ok
        
        # Check retention alignment
        retention_alignment_ok = True
        for seg in segments:
            # Risk scores must be [0, 1]
            if not (0.0 <= seg.retention_risk_score <= 1.0):
                retention_alignment_ok = False
                break
        
        validation["retention_alignment_ok"] = retention_alignment_ok
        
        # Check emotional arc preservation
        emotional_arc_preserved = not self.config.allow_emotional_arc_modification
        validation["emotional_arc_preserved"] = emotional_arc_preserved
        
        # Check segment count matches
        validation["segment_count_ok"] = len(segments) == len(storyboard_segments)
        
        # Check segment order preserved
        order_ok = True
        for i in range(len(segments) - 1):
            if segments[i].end > segments[i + 1].start:
                order_ok = False
                break
        validation["segment_order_ok"] = order_ok
        
        return validation


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def load_retention_optimizer(config_path: Optional[str] = None) -> RetentionOptimizer:
    """Factory function to create RetentionOptimizer instance"""
    
    config = RetentionConfig()
    
    if config_path:
        # Load custom config from file
        try:
            with open(config_path, 'r') as f:
                config_data = json.load(f)
                # Update config with loaded values
                for key, value in config_data.items():
                    if hasattr(config, key):
                        setattr(config, key, value)
        except Exception as e:
            logging.warning(f"Failed to load config from {config_path}: {e}")
    
    return RetentionOptimizer(config)


def optimize_video_retention(
    video_id: str,
    storyboard_segments: List[Dict[str, Any]],
    emotional_arc: Dict[str, Any],
    script_timing: Dict[str, Any],
    feature_bundle: Dict[str, Any],
    historical_retention_curves: Dict[str, Any],
    niche_rules: Dict[str, Any],
    cross_modal_signals: Dict[str, Any],
    config: Optional[RetentionConfig] = None
) -> Dict[str, Any]:
    """
    Convenience function for single video optimization
    Returns: Dictionary representation of OptimizationOutput
    """
    
    optimizer = RetentionOptimizer(config)
    
    output = optimizer.optimize(
        video_id=video_id,
        storyboard_segments=storyboard_segments,
        emotional_arc=emotional_arc,
        script_timing=script_timing,
        feature_bundle=feature_bundle,
        historical_retention_curves=historical_retention_curves,
        niche_rules=niche_rules,
        cross_modal_signals=cross_modal_signals
    )
    
    return output.to_dict()


# ============================================================================
# RL INTEGRATION HOOKS
# ============================================================================

class RLFeedbackIntegrator:
    """
    Integrates RL feedback for continuous improvement
    Only adjusts parameters in next iteration, never current output
    """
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.feedback_history = []
    
    def record_feedback(
        self,
        video_id: str,
        optimization_output: OptimizationOutput,
        actual_retention: Dict[str, Any]
    ):
        """Record RL feedback for learning"""
        
        feedback = {
            "video_id": video_id,
            "timestamp": datetime.utcnow().isoformat(),
            "predicted_risks": [s.retention_risk_score for s in optimization_output.segments],
            "actual_retention": actual_retention,
            "adjustments_applied": [s.suggested_adjustments.to_dict() for s in optimization_output.segments]
        }
        
        self.feedback_history.append(feedback)
        self.logger.info(f"Recorded RL feedback for video_id={video_id}")
    
    def compute_parameter_updates(self) -> Dict[str, float]:
        """
        Compute suggested parameter updates based on feedback history
        Returns: Dictionary of parameter adjustments
        """
        
        if len(self.feedback_history) < 10:
            return {}  # Need minimum feedback for updates
        
        # Analyze prediction accuracy
        # Compute optimal weight adjustments
        # This would be replaced with actual RL algorithm
        
        updates = {
            "historical_weight": 0.0,
            "velocity_weight": 0.0,
            "emotional_arc_weight": 0.0,
            "cross_modal_weight": 0.0
        }
        
        return updates


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Example usage
    logging.basicConfig(level=logging.INFO)
    
    # Sample inputs
    video_id = "test_video_001"
    
    storyboard_segments = [
        {"segment_id": "intro", "start": 0, "end": 5, "description": "Hook"},
        {"segment_id": "main_1", "start": 5, "end": 15, "description": "Setup"},
        {"segment_id": "main_2", "start": 15, "end": 30, "description": "Development"},
        {"segment_id": "outro", "start": 30, "end": 35, "description": "Conclusion"}
    ]
    
    emotional_arc = {
        "peaks": [
            {"time": 7, "intensity": 0.8},
            {"time": 25, "intensity": 0.9}
        ],
        "timeline": [
            {"time": 0, "intensity": 0.5},
            {"time": 10, "intensity": 0.7},
            {"time": 20, "intensity": 0.6},
            {"time": 30, "intensity": 0.5}
        ]
    }
    
    script_timing = {
        "segments": [
            {"segment_id": "intro", "word_count": 25},
            {"segment_id": "main_1", "word_count": 50},
            {"segment_id": "main_2", "word_count": 75},
            {"segment_id": "outro", "word_count": 20}
        ]
    }
    
    feature_bundle = {
        "early_engagement": {"velocity_score": 0.7}
    }
    
    historical_retention_curves = {
        "average_retention_curve": [0.9, 0.75, 0.6, 0.5]
    }
    
    niche_rules = {
        "niche_type": "educational"
    }
    
    cross_modal_signals = {
        "segment_synergy": {
            "intro": 0.6,
            "main_1": 0.7,
            "main_2": 0.8,
            "outro": 0.5
        }
    }
    
    # Run optimization
    result = optimize_video_retention(
        video_id=video_id,
        storyboard_segments=storyboard_segments,
        emotional_arc=emotional_arc,
        script_timing=script_timing,
        feature_bundle=feature_bundle,
        historical_retention_curves=historical_retention_curves,
        niche_rules=niche_rules,
        cross_modal_signals=cross_modal_signals
    )
    
    print(json.dumps(result, indent=2))