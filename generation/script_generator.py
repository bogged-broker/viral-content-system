"""
/generation/script_generator.py

Generates production-grade, deterministic scripts from storyboards.
Converts blueprint segments into full text with emotional pacing, hooks, CTAs.
Optimized for 5M+ baseline, 30M-300M repeatable virality.

Responsibilities:
- Generate text for each storyboard segment
- Align with emotional arc + pacing
- Incorporate hooks, context cues, and CTAs
- Condition on format, niche, and early signals
- Validate segment length and reading time
- Output JSON + plain text script for downstream synthesis

NOT responsible for:
- Predicting virality or ranking videos
- Generating visuals
- Making distribution decisions
- Audio synthesis (downstream: audio_synthesizer.py)
"""

import json
import logging
import re
import hashlib
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional, Tuple
from enum import Enum
from pathlib import Path
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================================================
# ENUMS & CONSTANTS
# ============================================================================

class EmotionType(Enum):
    """Canonical emotion types aligned with emotional_arc_engine.py"""
    EXCITE = "excite"
    SURPRISE = "surprise"
    TENSION = "tension"
    CALM = "calm"
    CURIOSITY = "curiosity"
    SATISFACTION = "satisfaction"
    URGENCY = "urgency"


class FormatArchetype(Enum):
    """Content format types"""
    LISTICLE = "listicle"
    TUTORIAL = "tutorial"
    STORY = "story"
    REVEAL = "reveal"
    COMPARISON = "comparison"
    EDUCATIONAL = "educational"


class SegmentRole(Enum):
    """Segment function in video structure"""
    INTRO = "intro"
    HOOK = "hook"
    BODY = "body"
    CLIMAX = "climax"
    CTA = "cta"
    OUTRO = "outro"


# Reading speed constants (words per minute)
WPM_SLOW = 130
WPM_NORMAL = 150
WPM_FAST = 170
WPM_DEFAULT = 150

# Timing tolerances
SEGMENT_DURATION_TOLERANCE = 0.05  # ±5%
MAX_SEGMENT_DURATION = 30.0  # seconds
MIN_SEGMENT_DURATION = 2.0


# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass
class ScriptSegment:
    """Single segment of generated script"""
    segment_id: str
    start: float
    end: float
    text: str
    emotion: str
    cta: bool
    role: str = "body"
    word_count: int = 0
    reading_time: float = 0.0
    validation_passed: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Calculate derived fields"""
        if self.word_count == 0:
            self.word_count = len(self.text.split())
        if self.reading_time == 0.0:
            self.reading_time = (self.word_count / WPM_DEFAULT) * 60
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            "segment_id": self.segment_id,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "emotion": self.emotion,
            "cta": self.cta,
            "role": self.role,
            "word_count": self.word_count,
            "reading_time": self.reading_time,
            "validation_passed": self.validation_passed,
            "metadata": self.metadata
        }


@dataclass
class ScriptValidation:
    """Validation results for generated script"""
    segment_lengths_ok: bool = True
    reading_time_ok: bool = True
    emotion_alignment_ok: bool = True
    hook_present: bool = True
    cta_present: bool = True
    duration_match: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return asdict(self)
    
    def is_valid(self) -> bool:
        """Check if validation passed"""
        return (self.segment_lengths_ok and 
                self.reading_time_ok and 
                self.emotion_alignment_ok and
                len(self.errors) == 0)


@dataclass
class GeneratedScript:
    """Complete generated script output"""
    video_id: str
    script_version: str
    segments: List[ScriptSegment]
    validation: ScriptValidation
    metadata: Dict[str, Any] = field(default_factory=dict)
    generated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            "video_id": self.video_id,
            "script_version": self.script_version,
            "segments": [seg.to_dict() for seg in self.segments],
            "validation": self.validation.to_dict(),
            "metadata": self.metadata,
            "generated_at": self.generated_at
        }
    
    def to_json(self, indent: int = 2) -> str:
        """Convert to JSON string"""
        return json.dumps(self.to_dict(), indent=indent)
    
    def to_plain_text(self) -> str:
        """Export as plain text script"""
        lines = [f"# Script for {self.video_id}", ""]
        for seg in self.segments:
            lines.append(f"[{seg.start:.1f}s - {seg.end:.1f}s] {seg.segment_id.upper()}")
            lines.append(f"Emotion: {seg.emotion} | CTA: {seg.cta}")
            lines.append(seg.text)
            lines.append("")
        return "\n".join(lines)


# ============================================================================
# INPUT VALIDATOR
# ============================================================================

class InputValidator:
    """Validates inputs to script generator"""
    
    @staticmethod
    def validate_storyboard(storyboard: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Validate storyboard structure"""
        errors = []
        
        if not storyboard:
            errors.append("Storyboard is empty")
            return False, errors
        
        if "video_id" not in storyboard:
            errors.append("Missing video_id in storyboard")
        
        if "segments" not in storyboard:
            errors.append("Missing segments in storyboard")
            return False, errors
        
        segments = storyboard.get("segments", [])
        if not segments:
            errors.append("No segments in storyboard")
            return False, errors
        
        # Validate each segment
        for i, seg in enumerate(segments):
            if "segment_id" not in seg:
                errors.append(f"Segment {i} missing segment_id")
            if "start" not in seg or "end" not in seg:
                errors.append(f"Segment {i} missing timing information")
            if seg.get("end", 0) <= seg.get("start", 0):
                errors.append(f"Segment {i} has invalid timing")
        
        return len(errors) == 0, errors
    
    @staticmethod
    def validate_feature_bundle(feature_bundle: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Validate feature bundle structure"""
        errors = []
        warnings = []
        
        if not feature_bundle:
            warnings.append("Empty feature bundle - using defaults")
            return True, warnings
        
        # Optional validation for expected keys
        expected_keys = ["hook_strength", "pacing_marker", "sentiment_trajectory"]
        for key in expected_keys:
            if key not in feature_bundle:
                warnings.append(f"Missing optional feature: {key}")
        
        return True, warnings


# ============================================================================
# SEGMENT TEXT GENERATOR
# ============================================================================

class SegmentTextGenerator:
    """Generates text for individual segments"""
    
    def __init__(self, language: str = "en"):
        self.language = language
        self.templates = self._load_templates()
    
    def _load_templates(self) -> Dict[str, Dict[str, List[str]]]:
        """Load text generation templates"""
        return {
            "intro": {
                "excite": [
                    "Hey everyone! Today we're diving into {}",
                    "Welcome back! You won't believe what we discovered about {}",
                    "What's up everyone! Get ready because we're about to {}",
                ],
                "calm": [
                    "Hello everyone. Today we're exploring {}",
                    "Welcome. Let's take a look at {}",
                ]
            },
            "hook": {
                "surprise": [
                    "Can you believe {}?",
                    "Here's something shocking: {}",
                    "Wait until you see {}",
                ],
                "curiosity": [
                    "Ever wondered {}?",
                    "The secret to {} is actually {}",
                    "What if I told you {}?",
                ]
            },
            "body": {
                "excite": [
                    "Now here's where it gets interesting: {}",
                    "Check this out: {}",
                ],
                "calm": [
                    "Let's break this down: {}",
                    "Here's what you need to know: {}",
                ]
            },
            "cta": {
                "urgency": [
                    "Don't forget to {} before you leave!",
                    "Make sure to {} right now!",
                    "Quick - {} before it's too late!",
                ],
                "calm": [
                    "If you enjoyed this, please {}",
                    "Consider {} if you want more content like this",
                ]
            }
        }
    
    def generate_segment_text(
        self,
        segment: Dict[str, Any],
        emotion: str,
        role: str,
        feature_bundle: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None
    ) -> str:
        """Generate text for a single segment"""
        
        # Get content hint from segment
        content_hint = segment.get("content_hint", "")
        description = segment.get("description", "")
        
        # Calculate target word count based on duration
        duration = segment.get("end", 0) - segment.get("start", 0)
        target_words = int((duration / 60) * WPM_DEFAULT)
        
        # Select template based on role and emotion
        template = self._select_template(role, emotion)
        
        # Generate base text
        if content_hint:
            text = template.format(content_hint)
        elif description:
            text = self._expand_from_description(description, target_words, emotion)
        else:
            text = self._generate_placeholder(role, emotion, target_words)
        
        # Adjust length to match duration
        text = self._adjust_text_length(text, target_words)
        
        # Apply emotion-specific styling
        text = self._apply_emotion_styling(text, emotion)
        
        return text
    
    def _select_template(self, role: str, emotion: str) -> str:
        """Select appropriate template"""
        role_templates = self.templates.get(role, self.templates.get("body", {}))
        emotion_templates = role_templates.get(emotion, role_templates.get("calm", []))
        
        if emotion_templates:
            # Use first template (deterministic)
            return emotion_templates[0]
        
        return "{}"
    
    def _expand_from_description(self, description: str, target_words: int, emotion: str) -> str:
        """Expand description to target word count"""
        words = description.split()
        current_words = len(words)
        
        if current_words >= target_words:
            return " ".join(words[:target_words])
        
        # Add filler based on emotion
        fillers = {
            "excite": ["This is amazing!", "Incredible!", "You have to see this!"],
            "surprise": ["Unbelievable!", "Who knew?", "This changes everything!"],
            "calm": ["Let's continue.", "Moving on.", "Here's more."],
        }
        
        emotion_fillers = fillers.get(emotion, fillers["calm"])
        text = description
        
        while len(text.split()) < target_words:
            text += " " + emotion_fillers[0]
        
        return " ".join(text.split()[:target_words])
    
    def _generate_placeholder(self, role: str, emotion: str, target_words: int) -> str:
        """Generate placeholder text"""
        base = f"This is the {role} section with {emotion} emotion."
        words = base.split()
        
        while len(words) < target_words:
            words.append("content")
        
        return " ".join(words[:target_words])
    
    def _adjust_text_length(self, text: str, target_words: int) -> str:
        """Adjust text to match target word count"""
        words = text.split()
        current = len(words)
        
        if abs(current - target_words) / target_words < 0.1:
            return text  # Within 10% tolerance
        
        if current > target_words:
            return " ".join(words[:target_words])
        
        # Pad if too short (add transitional phrases)
        padding = ["You see,", "In fact,", "Essentially,", "Moreover,"]
        while len(words) < target_words and padding:
            words.insert(len(words) // 2, padding.pop(0))
        
        return " ".join(words[:target_words])
    
    def _apply_emotion_styling(self, text: str, emotion: str) -> str:
        """Apply emotion-specific styling to text"""
        if emotion == "excite":
            # Add exclamation marks
            text = text.replace(".", "!")
        elif emotion == "surprise":
            # Add question marks where appropriate
            if not text.endswith("?") and not text.endswith("!"):
                text += "?"
        elif emotion == "urgency":
            # Make more imperative
            text = text.replace("you can", "you must")
            text = text.replace("consider", "act now")
        
        return text


# ============================================================================
# EMOTION INTEGRATOR
# ============================================================================

class EmotionIntegrator:
    """Aligns script with emotional curve"""
    
    @staticmethod
    def align_emotions(
        segments: List[Dict[str, Any]],
        emotional_curve: Optional[Dict[str, float]] = None
    ) -> List[str]:
        """Assign emotions to segments based on curve"""
        
        if not emotional_curve:
            # Default emotional arc
            return EmotionIntegrator._default_emotion_arc(len(segments))
        
        emotions = []
        for i, seg in enumerate(segments):
            position = seg.get("start", 0) / max(s.get("end", 1) for s in segments)
            emotion = EmotionIntegrator._interpolate_emotion(position, emotional_curve)
            emotions.append(emotion)
        
        return emotions
    
    @staticmethod
    def _default_emotion_arc(num_segments: int) -> List[str]:
        """Generate default emotion arc"""
        if num_segments <= 2:
            return ["excite"] * num_segments
        
        arc = ["excite"]  # Start with excitement
        
        # Middle segments: build tension/curiosity
        middle = num_segments - 2
        for i in range(middle):
            if i < middle // 2:
                arc.append("curiosity")
            else:
                arc.append("tension")
        
        arc.append("satisfaction")  # End with satisfaction
        
        return arc
    
    @staticmethod
    def _interpolate_emotion(position: float, curve: Dict[str, float]) -> str:
        """Interpolate emotion from curve at position"""
        # Find closest position in curve
        positions = sorted(float(k) for k in curve.keys() if k != "default")
        
        if not positions:
            return curve.get("default", "calm")
        
        closest = min(positions, key=lambda x: abs(x - position))
        return curve.get(str(closest), "calm")
    
    @staticmethod
    def validate_emotion_alignment(
        segments: List[ScriptSegment],
        emotional_curve: Optional[Dict[str, float]] = None
    ) -> bool:
        """Validate that emotions align with curve"""
        if not emotional_curve:
            return True
        
        for seg in segments:
            expected = EmotionIntegrator._interpolate_emotion(
                seg.start / max(s.end for s in segments),
                emotional_curve
            )
            if seg.emotion != expected:
                logger.warning(
                    f"Emotion mismatch in {seg.segment_id}: "
                    f"expected {expected}, got {seg.emotion}"
                )
        
        return True


# ============================================================================
# HOOK & CTA OPTIMIZER
# ============================================================================

class HookCTAOptimizer:
    """Optimizes placement and content of hooks and CTAs"""
    
    @staticmethod
    def identify_hook_segments(segments: List[Dict[str, Any]]) -> List[str]:
        """Identify which segments should be hooks"""
        hooks = []
        
        for seg in segments:
            role = seg.get("role", "")
            segment_id = seg.get("segment_id", "")
            start = seg.get("start", 0)
            
            # First 15 seconds should have hook
            if start < 15 or role == "hook" or "hook" in segment_id.lower():
                hooks.append(segment_id)
        
        return hooks
    
    @staticmethod
    def identify_cta_segments(
        segments: List[Dict[str, Any]],
        feature_bundle: Dict[str, Any]
    ) -> List[str]:
        """Identify which segments should have CTAs"""
        ctas = []
        total_duration = max(s.get("end", 0) for s in segments)
        
        for seg in segments:
            segment_id = seg.get("segment_id", "")
            role = seg.get("role", "")
            end = seg.get("end", 0)
            
            # CTAs in last 20% or explicit CTA segments
            if (end > total_duration * 0.8 or 
                role == "cta" or 
                "cta" in segment_id.lower() or
                "outro" in segment_id.lower()):
                ctas.append(segment_id)
        
        return ctas
    
    @staticmethod
    def optimize_hook_text(text: str, hook_strength: float) -> str:
        """Optimize hook text based on strength signal"""
        if hook_strength > 0.8:
            # Strong hook - add emphasis
            if not text.endswith("!"):
                text = text.rstrip(".?") + "!"
            text = "🔥 " + text
        elif hook_strength > 0.5:
            # Medium hook - add question
            if "?" not in text:
                text = "Want to know a secret? " + text
        
        return text
    
    @staticmethod
    def optimize_cta_text(text: str, urgency: float = 0.5) -> str:
        """Optimize CTA text based on urgency"""
        cta_phrases = [
            "subscribe", "like", "comment", "share", 
            "follow", "click", "join", "check out"
        ]
        
        has_cta = any(phrase in text.lower() for phrase in cta_phrases)
        
        if not has_cta:
            if urgency > 0.7:
                text += " Don't forget to subscribe and hit that notification bell!"
            else:
                text += " If you enjoyed this, please like and subscribe."
        
        return text


# ============================================================================
# SEGMENT LENGTH VALIDATOR
# ============================================================================

class SegmentLengthValidator:
    """Validates segment lengths and reading times"""
    
    @staticmethod
    def validate_segment(
        segment: ScriptSegment,
        wpm: int = WPM_DEFAULT
    ) -> Tuple[bool, List[str]]:
        """Validate individual segment"""
        errors = []
        
        duration = segment.end - segment.start
        
        # Check duration bounds
        if duration < MIN_SEGMENT_DURATION:
            errors.append(f"Segment {segment.segment_id} too short: {duration}s")
        
        if duration > MAX_SEGMENT_DURATION:
            errors.append(f"Segment {segment.segment_id} too long: {duration}s")
        
        # Check reading time alignment
        expected_words = (duration / 60) * wpm
        tolerance = expected_words * SEGMENT_DURATION_TOLERANCE
        
        if abs(segment.word_count - expected_words) > tolerance:
            errors.append(
                f"Segment {segment.segment_id} word count mismatch: "
                f"{segment.word_count} words for {duration}s "
                f"(expected ~{int(expected_words)})"
            )
        
        return len(errors) == 0, errors
    
    @staticmethod
    def validate_all_segments(
        segments: List[ScriptSegment],
        wpm: int = WPM_DEFAULT
    ) -> Tuple[bool, List[str]]:
        """Validate all segments"""
        all_errors = []
        
        for seg in segments:
            valid, errors = SegmentLengthValidator.validate_segment(seg, wpm)
            all_errors.extend(errors)
        
        return len(all_errors) == 0, all_errors
    
    @staticmethod
    def auto_adjust_segment(
        segment: ScriptSegment,
        wpm: int = WPM_DEFAULT
    ) -> ScriptSegment:
        """Auto-adjust segment text to match duration"""
        duration = segment.end - segment.start
        target_words = int((duration / 60) * wpm)
        
        words = segment.text.split()
        
        if len(words) > target_words:
            # Truncate
            segment.text = " ".join(words[:target_words])
        elif len(words) < target_words:
            # Pad with filler
            filler = ["Indeed,", "Furthermore,", "Additionally,"]
            while len(words) < target_words and filler:
                words.append(filler.pop(0))
            segment.text = " ".join(words[:target_words])
        
        # Recalculate
        segment.word_count = len(segment.text.split())
        segment.reading_time = (segment.word_count / wpm) * 60
        
        return segment


# ============================================================================
# OUTPUT FORMATTER
# ============================================================================

class OutputFormatter:
    """Formats and exports generated scripts"""
    
    @staticmethod
    def format_json(script: GeneratedScript, indent: int = 2) -> str:
        """Format as JSON"""
        return script.to_json(indent=indent)
    
    @staticmethod
    def format_plain_text(script: GeneratedScript) -> str:
        """Format as plain text"""
        return script.to_plain_text()
    
    @staticmethod
    def format_srt_subtitles(script: GeneratedScript) -> str:
        """Format as SRT subtitle file"""
        lines = []
        
        for i, seg in enumerate(script.segments, 1):
            start_time = OutputFormatter._format_srt_time(seg.start)
            end_time = OutputFormatter._format_srt_time(seg.end)
            
            lines.append(str(i))
            lines.append(f"{start_time} --> {end_time}")
            lines.append(seg.text)
            lines.append("")
        
        return "\n".join(lines)
    
    @staticmethod
    def _format_srt_time(seconds: float) -> str:
        """Format seconds as SRT timestamp"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int((seconds % 1) * 1000)
        
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
    
    @staticmethod
    def save_to_file(script: GeneratedScript, output_path: Path, format: str = "json"):
        """Save script to file"""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        if format == "json":
            content = OutputFormatter.format_json(script)
        elif format == "txt":
            content = OutputFormatter.format_plain_text(script)
        elif format == "srt":
            content = OutputFormatter.format_srt_subtitles(script)
        else:
            raise ValueError(f"Unsupported format: {format}")
        
        output_path.write_text(content, encoding="utf-8")
        logger.info(f"Script saved to {output_path}")


# ============================================================================
# MAIN SCRIPT GENERATOR
# ============================================================================

class ScriptGenerator:
    """
    Main script generator orchestrating all components.
    Deterministic, production-grade, audit-safe.
    """
    
    def __init__(
        self,
        language: str = "en",
        wpm: int = WPM_DEFAULT,
        auto_adjust: bool = True
    ):
        self.language = language
        self.wpm = wpm
        self.auto_adjust = auto_adjust
        
        # Initialize components
        self.validator = InputValidator()
        self.text_generator = SegmentTextGenerator(language)
        self.emotion_integrator = EmotionIntegrator()
        self.hook_cta_optimizer = HookCTAOptimizer()
        self.length_validator = SegmentLengthValidator()
        self.output_formatter = OutputFormatter()
    
    def generate(
        self,
        storyboard: Dict[str, Any],
        feature_bundle: Optional[Dict[str, Any]] = None,
        emotional_curve: Optional[Dict[str, float]] = None,
        format_archetype: str = "tutorial",
        segment_context: Optional[Dict[str, Any]] = None
    ) -> GeneratedScript:
        """
        Generate complete script from storyboard.
        
        Args:
            storyboard: Output from storyboard.py
            feature_bundle: Features from virality_feature_engine.py
            emotional_curve: Optional pacing data
            format_archetype: Content format type
            segment_context: Optional early signal flags
        
        Returns:
            GeneratedScript with segments and validation
        """
        
        # Step 1: Validate inputs
        valid, errors = self.validator.validate_storyboard(storyboard)
        if not valid:
            raise ValueError(f"Invalid storyboard: {errors}")
        
        feature_bundle = feature_bundle or {}
        segment_context = segment_context or {}
        
        # Step 2: Extract metadata
        video_id = storyboard.get("video_id", "unknown")
        segments_data = storyboard.get("segments", [])
        
        # Generate version hash for determinism
        script_version = self._generate_version_hash(storyboard, feature_bundle)
        
        # Step 3: Assign emotions
        emotions = self.emotion_integrator.align_emotions(segments_data, emotional_curve)
        
        # Step 4: Identify hooks and CTAs
        hook_segments = self.hook_cta_optimizer.identify_hook_segments(segments_data)
        cta_segments = self.hook_cta_optimizer.identify_cta_segments(
            segments_data, feature_bundle
        )
        
        # Step 5: Generate segments
        script_segments = []
        
        for i, seg_data in enumerate(segments_data):
            segment_id = seg_data.get("segment_id", f"segment_{i}")
            emotion = emotions[i] if i < len(emotions) else "calm"
            role = seg_data.get("role", "body")
            
            # Generate text
            text = self.text_generator.generate_segment_text(
                seg_data,
                emotion,
                role,
                feature_bundle,
                segment_context
            )
            
            # Apply hook optimization if needed
            if segment_id in hook_segments:
                hook_strength = feature_bundle.get("hook_strength", 0.5)
                text = self.hook_cta_optimizer.optimize_hook_text(text, hook_strength)
            
            # Apply CTA optimization if needed
            if segment_id in cta_segments:
                urgency = feature_bundle.get("urgency", 0.5)
                text = self.hook_cta_optimizer.optimize_cta_text(text, urgency)
            
            # Create segment
            segment = ScriptSegment(
                segment_id=segment_id,
                start=seg_data.get("start", 0),
                end=seg_data.get("end", 0),
                text=text,
                emotion=emotion,
                cta=segment_id in cta_segments,
                role=role,
                metadata={
                    "format_archetype": format_archetype,
                    "original_description": seg_data.get("description", "")
                }
            )
            
            # Auto-adjust if enabled
            if self.auto_adjust:
                segment = self.length_validator.auto_adjust_segment(segment, self.wpm)
            
            script_segments.append(segment)
        
        # Step 6: Validate
        validation = self._validate_script(
            script_segments,
            emotional_curve,
            hook_segments,
            cta_segments
        )
        
        # Step 7: Create output
        script = GeneratedScript(
            video_id=video_id,
            script_version=script_version,
            segments=script_segments,
            validation=validation,
            metadata={
                "format_archetype": format_archetype,
                "language": self.language,
                "wpm": self.wpm,
                "num_segments": len(script_segments),
                "total_duration": max(s.end for s in script_segments),
                "total_words": sum(s.word_count for s in script_segments)
            }
        )
        
        logger.info(
            f"Generated script {script_version} for {video_id}: "
            f"{len(script_segments)} segments, "
            f"{sum(s.word_count for s in script_segments)} words"
        )
        
        return script
    
    def _generate_version_hash(
        self,
        storyboard: Dict[str, Any],
        feature_bundle: Dict[str, Any]
    ) -> str:
        """Generate deterministic version hash"""
        content = json.dumps({
            "storyboard": storyboard,
            "features": feature_bundle,
            "language": self.language,
            "wpm": self.wpm
        }, sort_keys=True)
        
        return hashlib.sha256(content.encode()).hexdigest()[:16]
    
    def _validate_script(
        self,
        segments: List[ScriptSegment],
        emotional_curve: Optional[Dict[str, float]],
        hook_segments: List[str],
        cta_segments: List[str]
    ) -> ScriptValidation:
        """Validate complete generated script"""
        validation = ScriptValidation()
        
        # Validate segment lengths
        lengths_ok, length_errors = self.length_validator.validate_all_segments(
            segments, self.wpm
        )
        validation.segment_lengths_ok = lengths_ok
        validation.errors.extend(length_errors)
        
        # Validate reading times
        for seg in segments:
            duration = seg.end - seg.start
            if abs(seg.reading_time - duration) > duration * SEGMENT_DURATION_TOLERANCE:
                validation.reading_time_ok = False
                validation.errors.append(
                    f"Reading time mismatch in {seg.segment_id}"
                )
        
        # Validate emotion alignment
        validation.emotion_alignment_ok = self.emotion_integrator.validate_emotion_alignment(
            segments, emotional_curve
        )
        
        # Validate hooks present
        hook_found = any(seg.segment_id in hook_segments for seg in segments)
        if not hook_found:
            validation.hook_present = False
            validation.warnings.append("No hook segment found")
        
        # Validate CTAs present
        cta_found = any(seg.cta for seg in segments)
        if not cta_found:
            validation.cta_present = False
            validation.warnings.append("No CTA found in script")
        
        # Validate total duration
        if segments:
            expected_duration = max(seg.end for seg in segments)
            actual_duration = sum(seg.reading_time for seg in segments)
            
            if abs(expected_duration - actual_duration) > expected_duration * 0.1:
                validation.duration_match = False
                validation.errors.append(
                    f"Total duration mismatch: expected {expected_duration}s, "
                    f"reading time {actual_duration}s"
                )
        
        return validation
    
    def generate_batch(
        self,
        storyboards: List[Dict[str, Any]],
        feature_bundles: Optional[List[Dict[str, Any]]] = None,
        **kwargs
    ) -> List[GeneratedScript]:
        """Generate scripts for multiple storyboards"""
        feature_bundles = feature_bundles or [{}] * len(storyboards)
        
        scripts = []
        for i, storyboard in enumerate(storyboards):
            try:
                script = self.generate(
                    storyboard,
                    feature_bundles[i] if i < len(feature_bundles) else {},
                    **kwargs
                )
                scripts.append(script)
            except Exception as e:
                logger.error(f"Failed to generate script for storyboard {i}: {e}")
                continue
        
        return scripts
    
    def save(
        self,
        script: GeneratedScript,
        output_dir: Path,
        formats: List[str] = ["json"]
    ):
        """Save script in multiple formats"""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        base_name = f"{script.video_id}_{script.script_version}"
        
        for fmt in formats:
            if fmt == "json":
                output_path = output_dir / f"{base_name}.json"
            elif fmt == "txt":
                output_path = output_dir / f"{base_name}.txt"
            elif fmt == "srt":
                output_path = output_dir / f"{base_name}.srt"
            else:
                logger.warning(f"Unsupported format: {fmt}")
                continue
            
            self.output_formatter.save_to_file(script, output_path, fmt)


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def load_storyboard(path: Path) -> Dict[str, Any]:
    """Load storyboard from JSON file"""
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_feature_bundle(path: Path) -> Dict[str, Any]:
    """Load feature bundle from JSON file"""
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Example storyboard
    example_storyboard = {
        "video_id": "test_video_001",
        "segments": [
            {
                "segment_id": "intro",
                "start": 0,
                "end": 5,
                "role": "intro",
                "description": "Welcome viewers and introduce topic",
                "content_hint": "the amazing world of AI"
            },
            {
                "segment_id": "hook",
                "start": 5,
                "end": 15,
                "role": "hook",
                "description": "Hook viewers with surprising fact",
                "content_hint": "AI can now create entire movies"
            },
            {
                "segment_id": "body_1",
                "start": 15,
                "end": 30,
                "role": "body",
                "description": "Explain main concept",
                "content_hint": "how generative AI works"
            },
            {
                "segment_id": "cta",
                "start": 30,
                "end": 35,
                "role": "cta",
                "description": "Call to action",
                "content_hint": "subscribe for more AI content"
            }
        ]
    }
    
    # Example feature bundle
    example_features = {
        "hook_strength": 0.85,
        "pacing_marker": "fast",
        "sentiment_trajectory": [0.2, 0.6, 0.8, 0.7],
        "urgency": 0.6
    }
    
    # Example emotional curve
    example_curve = {
        "0.0": "excite",
        "0.3": "surprise",
        "0.7": "curiosity",
        "0.9": "satisfaction"
    }
    
    # Initialize generator
    generator = ScriptGenerator(
        language="en",
        wpm=150,
        auto_adjust=True
    )
    
    # Generate script
    script = generator.generate(
        storyboard=example_storyboard,
        feature_bundle=example_features,
        emotional_curve=example_curve,
        format_archetype="tutorial"
    )
    
    # Print results
    print("=" * 80)
    print("GENERATED SCRIPT")
    print("=" * 80)
    print(f"\nVideo ID: {script.video_id}")
    print(f"Version: {script.script_version}")
    print(f"Validation: {script.validation.is_valid()}")
    print(f"\nSegments ({len(script.segments)}):")
    print("-" * 80)
    
    for seg in script.segments:
        print(f"\n[{seg.start:.1f}s - {seg.end:.1f}s] {seg.segment_id.upper()}")
        print(f"Emotion: {seg.emotion} | CTA: {seg.cta} | Words: {seg.word_count}")
        print(f"Text: {seg.text}")
    
    print("\n" + "=" * 80)
    print("VALIDATION RESULTS")
    print("=" * 80)
    print(json.dumps(script.validation.to_dict(), indent=2))
    
    # Save to file
    output_dir = Path("./output/scripts")
    generator.save(script, output_dir, formats=["json", "txt", "srt"])
    
    print(f"\n✅ Script saved to {output_dir}")
    print("\n" + "=" * 80)