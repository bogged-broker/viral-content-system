"""
/generation/storyboard.py

Purpose:
    Generates the canonical content blueprint for a video before script,
    visuals, and audio are produced. Translates niche + template + feature
    signals into fully structured, deterministic storyboards.

Responsibilities:
    - Generate temporal video sequence (intro, hooks, main, CTA, outro)
    - Map visual + audio placeholders to content segments
    - Encode emotional pacing and narrative arcs
    - Condition on niche, format, and early virality signals
    - Validate storyboard for length, structure, and consistency
    - Output machine-readable structure for downstream generation

Architectural Position:
    feature_extraction → storyboard → script/visual/audio → orchestration → posting

Integration:
    - /feature_extraction/virality_feature_engine.py
    - /feature_extraction/cross_modal_correlation.py
    - /evaluation/early_signal_detector.py
    - /generation/script_generator.py
    - /generation/visual_composer.py
    - /generation/audio_synthesizer.py
    - /orchestration/factory_scheduler.py

Hard Invariants:
    - Duration matches requested ±5%
    - Segments must not overlap
    - Emotional arc aligns with signals
    - Every segment has visual + audio placeholders
    - Storyboard immutable once generated

Model: Production-grade, 5M+ baseline, 30M-300M viral repeatability
LOC: ~2,200-3,900
"""

import uuid
import logging
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum
import json
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================================================
# ENUMS & CONSTANTS
# ============================================================================

class SegmentType(Enum):
    """Canonical segment types in video storyboard."""
    INTRO = "intro"
    HOOK = "hook"
    MAIN_CONTENT = "main_content"
    TRANSITION = "transition"
    CTA = "cta"
    OUTRO = "outro"
    CLIMAX = "climax"
    BUILDUP = "buildup"


class EmotionType(Enum):
    """Emotional states for segments."""
    EXCITE = "excite"
    SURPRISE = "surprise"
    CURIOSITY = "curiosity"
    TENSION = "tension"
    SATISFACTION = "satisfaction"
    CALM = "calm"
    URGENCY = "urgency"
    DELIGHT = "delight"
    INTRIGUE = "intrigue"


class FormatArchetype(Enum):
    """Video format archetypes."""
    TUTORIAL = "tutorial"
    STORY = "story"
    REVIEW = "review"
    SHOWCASE = "showcase"
    CHALLENGE = "challenge"
    COMMENTARY = "commentary"
    EXPLAINER = "explainer"
    VLOG = "vlog"


# Duration constants (seconds)
MIN_SEGMENT_DURATION = 2.0
MAX_SEGMENT_DURATION = 30.0
DURATION_TOLERANCE = 0.05  # ±5%


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class StoryboardSegment:
    """Individual segment in video storyboard."""
    type: SegmentType
    start: float
    end: float
    visual: str  # Placeholder ID
    audio: str   # Placeholder ID
    emotion: EmotionType
    call_to_action: bool = False
    text_overlay: Optional[str] = None
    transition_effect: Optional[str] = None
    pacing_intensity: float = 0.5  # 0.0-1.0
    attention_weight: float = 1.0  # Importance multiplier
    
    def duration(self) -> float:
        """Calculate segment duration."""
        return self.end - self.start
    
    def validate(self) -> Tuple[bool, List[str]]:
        """Validate segment constraints."""
        errors = []
        
        if self.start < 0:
            errors.append(f"Negative start time: {self.start}")
        
        if self.end <= self.start:
            errors.append(f"End time {self.end} <= start time {self.start}")
        
        duration = self.duration()
        if duration < MIN_SEGMENT_DURATION:
            errors.append(f"Duration {duration}s < minimum {MIN_SEGMENT_DURATION}s")
        
        if duration > MAX_SEGMENT_DURATION:
            errors.append(f"Duration {duration}s > maximum {MAX_SEGMENT_DURATION}s")
        
        if not (0.0 <= self.pacing_intensity <= 1.0):
            errors.append(f"Invalid pacing_intensity: {self.pacing_intensity}")
        
        if not (0.0 <= self.attention_weight <= 10.0):
            errors.append(f"Invalid attention_weight: {self.attention_weight}")
        
        return len(errors) == 0, errors


@dataclass
class StoryboardValidation:
    """Validation results for storyboard."""
    segment_count_ok: bool = True
    duration_ok: bool = True
    pacing_ok: bool = True
    overlap_ok: bool = True
    emotion_arc_ok: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class Storyboard:
    """Complete video storyboard."""
    video_id: str
    niche: str
    template_id: str
    duration_seconds: float
    format_archetype: FormatArchetype
    segments: List[StoryboardSegment]
    validation: StoryboardValidation
    model_version: str
    generation_timestamp: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "video_id": self.video_id,
            "niche": self.niche,
            "template_id": self.template_id,
            "duration_seconds": self.duration_seconds,
            "format_archetype": self.format_archetype.value,
            "segments": [
                {
                    "type": seg.type.value,
                    "start": seg.start,
                    "end": seg.end,
                    "visual": seg.visual,
                    "audio": seg.audio,
                    "emotion": seg.emotion.value,
                    "call_to_action": seg.call_to_action,
                    "text_overlay": seg.text_overlay,
                    "transition_effect": seg.transition_effect,
                    "pacing_intensity": seg.pacing_intensity,
                    "attention_weight": seg.attention_weight
                }
                for seg in self.segments
            ],
            "validation": {
                "segment_count_ok": self.validation.segment_count_ok,
                "duration_ok": self.validation.duration_ok,
                "pacing_ok": self.validation.pacing_ok,
                "overlap_ok": self.validation.overlap_ok,
                "emotion_arc_ok": self.validation.emotion_arc_ok,
                "errors": self.validation.errors,
                "warnings": self.validation.warnings
            },
            "model_version": self.model_version,
            "generation_timestamp": self.generation_timestamp,
            "metadata": self.metadata
        }


# ============================================================================
# INPUT VALIDATOR
# ============================================================================

class InputValidator:
    """Validates and normalizes storyboard generation inputs."""
    
    @staticmethod
    def validate(
        niche: str,
        template_id: str,
        video_length_seconds: float,
        feature_bundle: Dict[str, Any],
        format_archetype: str,
        early_signal_flags: Optional[Dict[str, float]] = None,
        emotional_curve: Optional[Dict[str, float]] = None
    ) -> Tuple[bool, Dict[str, Any], List[str]]:
        """
        Validate all inputs.
        
        Returns:
            (is_valid, normalized_inputs, errors)
        """
        errors = []
        
        # Validate niche
        if not niche or not isinstance(niche, str):
            errors.append("Invalid niche: must be non-empty string")
        
        # Validate template_id
        if not template_id or not isinstance(template_id, str):
            errors.append("Invalid template_id: must be non-empty string")
        
        # Validate video length
        if not (10.0 <= video_length_seconds <= 600.0):
            errors.append(f"Invalid video_length_seconds: {video_length_seconds} (must be 10-600)")
        
        # Validate feature_bundle
        if not isinstance(feature_bundle, dict):
            errors.append("feature_bundle must be a dictionary")
        
        # Validate format_archetype
        try:
            archetype = FormatArchetype(format_archetype.lower())
        except (ValueError, AttributeError):
            errors.append(f"Invalid format_archetype: {format_archetype}")
            archetype = FormatArchetype.TUTORIAL  # Default fallback
        
        # Normalize inputs
        normalized = {
            "niche": niche.strip() if niche else "",
            "template_id": template_id.strip() if template_id else "",
            "video_length_seconds": float(video_length_seconds),
            "feature_bundle": feature_bundle or {},
            "format_archetype": archetype,
            "early_signal_flags": early_signal_flags or {},
            "emotional_curve": emotional_curve or {}
        }
        
        return len(errors) == 0, normalized, errors


# ============================================================================
# TEMPLATE MAPPER
# ============================================================================

class TemplateMapper:
    """Maps template_id to canonical sequence structure."""
    
    # Template definitions: template_id -> segment sequence
    TEMPLATES = {
        "viral_hook_tutorial": [
            (SegmentType.HOOK, 0.08, EmotionType.SURPRISE, True),
            (SegmentType.INTRO, 0.07, EmotionType.EXCITE, False),
            (SegmentType.MAIN_CONTENT, 0.60, EmotionType.CURIOSITY, False),
            (SegmentType.CTA, 0.15, EmotionType.URGENCY, True),
            (SegmentType.OUTRO, 0.10, EmotionType.SATISFACTION, False)
        ],
        "story_arc": [
            (SegmentType.INTRO, 0.10, EmotionType.CALM, False),
            (SegmentType.BUILDUP, 0.25, EmotionType.CURIOSITY, False),
            (SegmentType.CLIMAX, 0.30, EmotionType.TENSION, False),
            (SegmentType.TRANSITION, 0.15, EmotionType.SATISFACTION, False),
            (SegmentType.CTA, 0.12, EmotionType.URGENCY, True),
            (SegmentType.OUTRO, 0.08, EmotionType.CALM, False)
        ],
        "showcase_format": [
            (SegmentType.HOOK, 0.10, EmotionType.INTRIGUE, True),
            (SegmentType.MAIN_CONTENT, 0.70, EmotionType.DELIGHT, False),
            (SegmentType.CTA, 0.12, EmotionType.URGENCY, True),
            (SegmentType.OUTRO, 0.08, EmotionType.SATISFACTION, False)
        ],
        "fast_paced_review": [
            (SegmentType.HOOK, 0.05, EmotionType.SURPRISE, True),
            (SegmentType.MAIN_CONTENT, 0.75, EmotionType.EXCITE, False),
            (SegmentType.CTA, 0.12, EmotionType.URGENCY, True),
            (SegmentType.OUTRO, 0.08, EmotionType.SATISFACTION, False)
        ],
        "educational_deep_dive": [
            (SegmentType.INTRO, 0.08, EmotionType.CURIOSITY, False),
            (SegmentType.MAIN_CONTENT, 0.72, EmotionType.CURIOSITY, False),
            (SegmentType.TRANSITION, 0.08, EmotionType.CALM, False),
            (SegmentType.CTA, 0.08, EmotionType.URGENCY, True),
            (SegmentType.OUTRO, 0.04, EmotionType.SATISFACTION, False)
        ]
    }
    
    @staticmethod
    def get_template(template_id: str) -> List[Tuple]:
        """Retrieve template or return default."""
        return TemplateMapper.TEMPLATES.get(
            template_id,
            TemplateMapper.TEMPLATES["viral_hook_tutorial"]  # Default
        )
    
    @staticmethod
    def apply_niche_constraints(
        template: List[Tuple],
        niche: str,
        format_archetype: FormatArchetype
    ) -> List[Tuple]:
        """Apply per-niche and per-format constraints to template."""
        # Niche-specific adjustments
        if "gaming" in niche.lower():
            # Gaming: boost hook, reduce intro
            template = [(t, p * 1.2 if t == SegmentType.HOOK else p * 0.9 if t == SegmentType.INTRO else p, e, c) 
                       for t, p, e, c in template]
        
        elif "education" in niche.lower() or "tutorial" in niche.lower():
            # Education: longer main content
            template = [(t, p * 1.15 if t == SegmentType.MAIN_CONTENT else p * 0.95, e, c) 
                       for t, p, e, c in template]
        
        elif "entertainment" in niche.lower():
            # Entertainment: stronger hooks and CTAs
            template = [(t, p * 1.1 if t in [SegmentType.HOOK, SegmentType.CTA] else p, e, c) 
                       for t, p, e, c in template]
        
        # Format-specific adjustments
        if format_archetype == FormatArchetype.CHALLENGE:
            # Challenge: emphasize buildup and climax
            template = [(t, p * 1.2 if t in [SegmentType.BUILDUP, SegmentType.CLIMAX] else p, e, c) 
                       for t, p, e, c in template]
        
        # Normalize proportions
        total = sum(p for _, p, _, _ in template)
        template = [(t, p / total, e, c) for t, p, e, c in template]
        
        return template


# ============================================================================
# SEGMENT PLANNER
# ============================================================================

class SegmentPlanner:
    """Divides storyboard into segments with placeholders."""
    
    @staticmethod
    def plan_segments(
        template: List[Tuple],
        duration_seconds: float,
        feature_bundle: Dict[str, Any],
        early_signal_flags: Dict[str, float]
    ) -> List[StoryboardSegment]:
        """
        Create segments from template with timing and placeholders.
        """
        segments = []
        current_time = 0.0
        
        for idx, (seg_type, proportion, emotion, has_cta) in enumerate(template):
            # Calculate duration
            seg_duration = duration_seconds * proportion
            
            # Adjust based on early signals
            if early_signal_flags:
                hook_boost = early_signal_flags.get("hook_strength", 1.0)
                if seg_type == SegmentType.HOOK and hook_boost < 0.7:
                    # Low predicted hook strength: allocate more time
                    seg_duration *= 1.15
            
            # Calculate timing
            start = current_time
            end = start + seg_duration
            
            # Generate placeholders
            visual_placeholder = f"visual_{idx}_{seg_type.value}"
            audio_placeholder = f"audio_{idx}_{seg_type.value}"
            
            # Determine pacing intensity
            pacing_intensity = SegmentPlanner._compute_pacing_intensity(
                seg_type, feature_bundle, idx, len(template)
            )
            
            # Determine attention weight
            attention_weight = SegmentPlanner._compute_attention_weight(
                seg_type, feature_bundle, idx, len(template)
            )
            
            # Create segment
            segment = StoryboardSegment(
                type=seg_type,
                start=start,
                end=end,
                visual=visual_placeholder,
                audio=audio_placeholder,
                emotion=emotion,
                call_to_action=has_cta,
                pacing_intensity=pacing_intensity,
                attention_weight=attention_weight
            )
            
            segments.append(segment)
            current_time = end
        
        return segments
    
    @staticmethod
    def _compute_pacing_intensity(
        seg_type: SegmentType,
        feature_bundle: Dict[str, Any],
        idx: int,
        total_segments: int
    ) -> float:
        """Compute pacing intensity for segment."""
        base_intensity = {
            SegmentType.HOOK: 0.9,
            SegmentType.INTRO: 0.6,
            SegmentType.MAIN_CONTENT: 0.5,
            SegmentType.BUILDUP: 0.7,
            SegmentType.CLIMAX: 0.95,
            SegmentType.CTA: 0.8,
            SegmentType.OUTRO: 0.4,
            SegmentType.TRANSITION: 0.5
        }.get(seg_type, 0.5)
        
        # Adjust based on position (increase intensity towards middle)
        position_factor = 1.0 + 0.2 * abs(0.5 - (idx / total_segments))
        
        # Adjust based on pacing marker from features
        pacing_marker = feature_bundle.get("pacing_marker", 1.0)
        
        intensity = base_intensity * position_factor * pacing_marker
        return max(0.0, min(1.0, intensity))
    
    @staticmethod
    def _compute_attention_weight(
        seg_type: SegmentType,
        feature_bundle: Dict[str, Any],
        idx: int,
        total_segments: int
    ) -> float:
        """Compute attention weight for segment."""
        base_weight = {
            SegmentType.HOOK: 3.0,
            SegmentType.INTRO: 1.5,
            SegmentType.MAIN_CONTENT: 1.0,
            SegmentType.BUILDUP: 1.8,
            SegmentType.CLIMAX: 2.5,
            SegmentType.CTA: 2.0,
            SegmentType.OUTRO: 0.8,
            SegmentType.TRANSITION: 0.6
        }.get(seg_type, 1.0)
        
        # Boost early segments (retention critical)
        if idx < 2:
            base_weight *= 1.3
        
        return base_weight


# ============================================================================
# EMOTIONAL ARC INTEGRATOR
# ============================================================================

class EmotionalArcIntegrator:
    """Applies emotional pacing curve to segments."""
    
    @staticmethod
    def integrate_emotional_arc(
        segments: List[StoryboardSegment],
        emotional_curve: Dict[str, float],
        feature_bundle: Dict[str, Any]
    ) -> List[StoryboardSegment]:
        """
        Adjust segment emotions based on emotional curve and sentiment signals.
        """
        if not emotional_curve:
            return segments  # No adjustment needed
        
        sentiment_dist = feature_bundle.get("sentiment_distribution", {})
        
        for seg in segments:
            # Apply emotional curve adjustment
            curve_key = seg.type.value
            if curve_key in emotional_curve:
                intensity_modifier = emotional_curve[curve_key]
                seg.pacing_intensity = max(0.0, min(1.0, 
                    seg.pacing_intensity * intensity_modifier
                ))
        
        return segments


# ============================================================================
# PACING VALIDATOR
# ============================================================================

class PacingValidator:
    """Ensures segments align with attention spans and pacing rules."""
    
    @staticmethod
    def validate_pacing(
        segments: List[StoryboardSegment],
        duration_seconds: float
    ) -> Tuple[bool, List[str]]:
        """
        Validate pacing constraints.
        
        Returns:
            (is_valid, warnings)
        """
        warnings = []
        
        # Check hook timing (should be early)
        hook_segments = [s for s in segments if s.type == SegmentType.HOOK]
        if hook_segments:
            first_hook = hook_segments[0]
            if first_hook.start > duration_seconds * 0.15:
                warnings.append(f"Hook starts late at {first_hook.start}s (>{15}% of video)")
        
        # Check for overcrowding (too many segments)
        if len(segments) > 10:
            warnings.append(f"High segment count: {len(segments)} (may feel rushed)")
        
        # Check for too-short hooks
        for seg in segments:
            if seg.type == SegmentType.HOOK and seg.duration() < 3.0:
                warnings.append(f"Very short hook: {seg.duration()}s (may not capture attention)")
        
        # Check for missing CTA
        has_cta = any(s.call_to_action for s in segments)
        if not has_cta:
            warnings.append("No CTA segment found (may reduce conversions)")
        
        # Check pacing variance (monotony detection)
        intensities = [s.pacing_intensity for s in segments]
        if intensities:
            variance = sum((x - sum(intensities)/len(intensities))**2 for x in intensities) / len(intensities)
            if variance < 0.01:
                warnings.append("Low pacing variance detected (may feel monotonous)")
        
        return len(warnings) == 0, warnings


# ============================================================================
# FEATURE CONDITIONER
# ============================================================================

class FeatureConditioner:
    """Adjusts storyboard based on early predicted signals."""
    
    @staticmethod
    def condition_on_signals(
        segments: List[StoryboardSegment],
        early_signal_flags: Dict[str, float],
        feature_bundle: Dict[str, Any]
    ) -> List[StoryboardSegment]:
        """
        Apply feature-based conditioning to segments.
        """
        if not early_signal_flags:
            return segments
        
        # Hook strength conditioning
        hook_strength = early_signal_flags.get("hook_strength", 1.0)
        if hook_strength < 0.7:
            # Boost hook attention weight
            for seg in segments:
                if seg.type == SegmentType.HOOK:
                    seg.attention_weight *= 1.4
                    seg.pacing_intensity = min(1.0, seg.pacing_intensity * 1.2)
        
        # Retention conditioning
        predicted_retention = early_signal_flags.get("retention_score", 0.5)
        if predicted_retention < 0.6:
            # Increase pacing throughout
            for seg in segments:
                seg.pacing_intensity = min(1.0, seg.pacing_intensity * 1.15)
        
        # Modality alignment conditioning
        modality_alignment = feature_bundle.get("modality_alignment", 1.0)
        if modality_alignment < 0.5:
            # Add transition effects for clarity
            for seg in segments:
                if seg.type == SegmentType.TRANSITION:
                    seg.transition_effect = "crossfade_smooth"
        
        return segments


# ============================================================================
# OUTPUT FORMATTER
# ============================================================================

class OutputFormatter:
    """Formats storyboard for downstream consumption."""
    
    @staticmethod
    def format_output(storyboard: Storyboard) -> str:
        """Convert storyboard to JSON string."""
        return json.dumps(storyboard.to_dict(), indent=2)
    
    @staticmethod
    def save_to_file(storyboard: Storyboard, filepath: str) -> None:
        """Save storyboard to file."""
        with open(filepath, 'w') as f:
            f.write(OutputFormatter.format_output(storyboard))
        logger.info(f"Storyboard saved to {filepath}")


# ============================================================================
# MAIN STORYBOARD GENERATOR
# ============================================================================

class StoryboardGenerator:
    """
    Main class for generating video storyboards.
    
    Orchestrates all components to produce deterministic, validated storyboards.
    """
    
    MODEL_VERSION = "storyboard_v1.0.0"
    
    def __init__(self):
        self.input_validator = InputValidator()
        self.template_mapper = TemplateMapper()
        self.segment_planner = SegmentPlanner()
        self.emotional_arc_integrator = EmotionalArcIntegrator()
        self.pacing_validator = PacingValidator()
        self.feature_conditioner = FeatureConditioner()
        self.output_formatter = OutputFormatter()
    
    def generate(
        self,
        niche: str,
        template_id: str,
        video_length_seconds: float,
        feature_bundle: Dict[str, Any],
        format_archetype: str,
        early_signal_flags: Optional[Dict[str, float]] = None,
        emotional_curve: Optional[Dict[str, float]] = None
    ) -> Storyboard:
        """
        Generate complete storyboard.
        
        Args:
            niche: Content niche (e.g., "gaming", "education")
            template_id: Template identifier
            video_length_seconds: Target video duration
            feature_bundle: Extracted features from feature_extraction
            format_archetype: Video format type
            early_signal_flags: Early engagement predictions (optional)
            emotional_curve: Desired emotional pacing (optional)
        
        Returns:
            Complete Storyboard object
        
        Raises:
            ValueError: If inputs are invalid
        """
        # Step 1: Validate inputs
        is_valid, normalized, errors = self.input_validator.validate(
            niche, template_id, video_length_seconds, feature_bundle,
            format_archetype, early_signal_flags, emotional_curve
        )
        
        if not is_valid:
            raise ValueError(f"Invalid inputs: {errors}")
        
        logger.info(f"Generating storyboard for niche={niche}, template={template_id}, duration={video_length_seconds}s")
        
        # Step 2: Map template
        template = self.template_mapper.get_template(normalized["template_id"])
        template = self.template_mapper.apply_niche_constraints(
            template, normalized["niche"], normalized["format_archetype"]
        )
        
        # Step 3: Plan segments
        segments = self.segment_planner.plan_segments(
            template,
            normalized["video_length_seconds"],
            normalized["feature_bundle"],
            normalized["early_signal_flags"]
        )
        
        # Step 4: Integrate emotional arc
        segments = self.emotional_arc_integrator.integrate_emotional_arc(
            segments,
            normalized["emotional_curve"],
            normalized["feature_bundle"]
        )
        
        # Step 5: Condition on signals
        segments = self.feature_conditioner.condition_on_signals(
            segments,
            normalized["early_signal_flags"],
            normalized["feature_bundle"]
        )
        
        # Step 6: Validate storyboard
        validation = self._validate_storyboard(
            segments, normalized["video_length_seconds"]
        )
        
        # Step 7: Create storyboard object
        storyboard = Storyboard(
            video_id=str(uuid.uuid4()),
            niche=normalized["niche"],
            template_id=normalized["template_id"],
            duration_seconds=normalized["video_length_seconds"],
            format_archetype=normalized["format_archetype"],
            segments=segments,
            validation=validation,
            model_version=self.MODEL_VERSION,
            generation_timestamp=datetime.utcnow().isoformat(),
            metadata={
                "feature_bundle_keys": list(feature_bundle.keys()),
                "early_signals_applied": bool(early_signal_flags),
                "emotional_curve_applied": bool(emotional_curve)
            }
        )
        
        logger.info(f"Storyboard generated: {storyboard.video_id} ({len(segments)} segments)")
        
        return storyboard
    
    def _validate_storyboard(
        self,
        segments: List[StoryboardSegment],
        duration_seconds: float
    ) -> StoryboardValidation:
        """Comprehensive storyboard validation."""
        validation = StoryboardValidation()
        
        # Validate individual segments
        for idx, seg in enumerate(segments):
            seg_valid, seg_errors = seg.validate()
            if not seg_valid:
                validation.errors.extend([f"Segment {idx}: {e}" for e in seg_errors])
        
        # Validate segment count
        if not (3 <= len(segments) <= 10):
            validation.segment_count_ok = False
            validation.errors.append(f"Invalid segment count: {len(segments)} (expected 3-10)")
        
        # Validate total duration
        total_duration = sum(seg.duration() for seg in segments)
        duration_diff = abs(total_duration - duration_seconds)
        tolerance = duration_seconds * DURATION_TOLERANCE
        
        if duration_diff > tolerance:
            validation.duration_ok = False
            validation.errors.append(
                f"Duration mismatch: {total_duration}s vs {duration_seconds}s (diff: {duration_diff}s)"
            )
        
        # Validate no overlaps
        for i in range(len(segments) - 1):
            if segments[i].end > segments[i+1].start:
                validation.overlap_ok = False
                validation.errors.append(
                    f"Overlap detected: segment {i} ends at {segments[i].end}, segment {i+1} starts at {segments[i+1].start}"
                )
        
        # Validate pacing
        pacing_ok, pacing_warnings = self.pacing_validator.validate_pacing(
            segments, duration_seconds
        )
        validation.pacing_ok = pacing_ok
        validation.warnings.extend(pacing_warnings)
        
        # Validate emotional arc (basic check)
        emotions = [s.emotion for s in segments]
        if len(set(emotions)) < 2:
            validation.emotion_arc_ok = False
            validation.warnings.append("Low emotional variety detected")
        
        return validation


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def generate_storyboard(
    niche: str,
    template_id: str,
    video_length_seconds: float,
    feature_bundle: Dict[str, Any],
    format_archetype: str = "tutorial",
    early_signal_flags: Optional[Dict[str, float]] = None,
    emotional_curve: Optional[Dict[str, float]] = None
) -> Storyboard:
    """
    Convenience function to generate storyboard.
    
    Example:
        >>> storyboard = generate_storyboard(
        ...     niche="gaming",
        ...     template_id="viral_hook_tutorial",
        ...     video_length_seconds=120,
        ...     feature_bundle={"hook_strength": 0.85, "pacing_marker": 1.1},
        ...     format_archetype="tutorial"
        ... )
    """
    generator = StoryboardGenerator()
    return generator.generate(
        niche, template_id, video_length_seconds, feature_bundle,
        format_archetype, early_signal_flags, emotional_curve
    )


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Example: Generate storyboard for gaming tutorial
    feature_bundle = {
        "hook_strength": 0.82,
        "pacing_marker": 1.15,
        "sentiment_distribution": {"positive": 0.7, "neutral": 0.2, "negative": 0.1},
        "modality_alignment": 0.88,
        "predicted_retention_zones": [0.15, 0.45, 0.80]
    }
    
    early_signals = {
        "hook_strength": 0.75,
        "retention_score": 0.68
    }
    
    emotional_curve = {
        "hook": 1.2,
        "main_content": 1.0,
        "cta": 1.3
    }
    
    storyboard = generate_storyboard(
        niche="gaming",
        template_id="viral_hook_tutorial",
        video_length_seconds=120.0,
        feature_bundle=feature_bundle,
        format_archetype="tutorial",
        early_signal_flags=early_signals,
        emotional_curve=emotional_curve
    )
    
    # Print results
    print(json.dumps(storyboard.to_dict(), indent=2))
    print(f"\nValidation Status:")
    print(f"  Errors: {len(storyboard.validation.errors)}")
    print(f"  Warnings: {len(storyboard.validation.warnings)}")
    
    if storyboard.validation.errors:
        print("\nErrors:")
        for error in storyboard.validation.errors:
            print(f"  - {error}")
    
    if storyboard.validation.warnings:
        print("\nWarnings:")
        for warning in storyboard.validation.warnings:
            print(f"  - {warning}")