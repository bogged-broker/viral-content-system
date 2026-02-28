"""
/generation/visual_composer.py

Purpose:
Creates exact visual plans for each video segment based on storyboard, script,
and feature signals. Outputs structured visual instructions for automated rendering.

Architecture: feature_extraction → generation → orchestration → posting
Position: generation/visual_composer.py
"""

from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
import json
import logging
from datetime import datetime


# ============================================================================
# CONFIGURATION & ENUMS
# ============================================================================

class SceneType(Enum):
    """Supported scene types"""
    INTRO = "intro"
    HOOK = "hook"
    BODY = "body"
    TRANSITION = "transition"
    CTA = "cta"
    OUTRO = "outro"


class VisualEffect(Enum):
    """Available visual effects"""
    ZOOM_IN = "zoom_in"
    ZOOM_OUT = "zoom_out"
    PAN_LEFT = "pan_left"
    PAN_RIGHT = "pan_right"
    SHAKE = "shake"
    BLUR = "blur"
    GLOW = "glow"
    VIGNETTE = "vignette"


class TransitionType(Enum):
    """Scene transition types"""
    FADE = "fade"
    CUT = "cut"
    SWIPE = "swipe"
    ZOOM = "zoom"
    DISSOLVE = "dissolve"
    SLIDE = "slide"


class EmotionType(Enum):
    """Emotional states for scenes"""
    EXCITEMENT = "excitement"
    CURIOSITY = "curiosity"
    SURPRISE = "surprise"
    CALM = "calm"
    TENSION = "tension"
    SATISFACTION = "satisfaction"


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class VisualElement:
    """Individual visual element within a scene"""
    type: str  # clip, overlay_text, graphic, emoji, effect
    params: Dict[str, Any]
    start_offset: float = 0.0
    duration: Optional[float] = None
    z_index: int = 0


@dataclass
class Transition:
    """Transition between scenes"""
    type: TransitionType
    duration: float
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Scene:
    """Complete scene specification"""
    scene_id: str
    scene_type: SceneType
    start: float
    end: float
    visual_elements: List[VisualElement]
    transitions: List[Transition]
    emotion: EmotionType
    script_alignment: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class VisualPlan:
    """Complete visual composition for a video"""
    video_id: str
    composer_version: str
    scenes: List[Scene]
    total_duration: float
    validation: Dict[str, bool]
    metadata: Dict[str, Any]
    generated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


# ============================================================================
# INPUT VALIDATOR
# ============================================================================

class InputValidator:
    """Validates all inputs before visual composition"""
    
    @staticmethod
    def validate_storyboard(storyboard: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Validate storyboard structure"""
        errors = []
        
        required_fields = ["segments", "total_duration", "niche"]
        for field in required_fields:
            if field not in storyboard:
                errors.append(f"Missing required field: {field}")
        
        if "segments" in storyboard:
            for i, seg in enumerate(storyboard["segments"]):
                if "start" not in seg or "end" not in seg:
                    errors.append(f"Segment {i} missing timing info")
        
        return len(errors) == 0, errors
    
    @staticmethod
    def validate_script(script: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Validate script structure"""
        errors = []
        
        if "segments" not in script:
            errors.append("Script missing segments")
        
        if "total_duration" not in script:
            errors.append("Script missing total_duration")
        
        return len(errors) == 0, errors
    
    @staticmethod
    def validate_features(features: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Validate feature bundle"""
        errors = []
        
        expected_keys = ["modality_alignment", "retention_curve", "narrative_intensity"]
        for key in expected_keys:
            if key not in features:
                errors.append(f"Missing feature: {key}")
        
        return len(errors) == 0, errors


# ============================================================================
# SCENE MAPPER
# ============================================================================

class SceneMapper:
    """Maps storyboard segments to visual scenes"""
    
    def __init__(self, logger: logging.Logger):
        self.logger = logger
    
    def map_segments_to_scenes(
        self,
        storyboard: Dict[str, Any],
        script: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Convert storyboard segments into scene definitions"""
        scenes = []
        
        storyboard_segments = storyboard.get("segments", [])
        script_segments = script.get("segments", [])
        
        for i, sb_seg in enumerate(storyboard_segments):
            scene_type = self._determine_scene_type(sb_seg, i, len(storyboard_segments))
            
            script_seg = script_segments[i] if i < len(script_segments) else {}
            
            scene = {
                "scene_id": sb_seg.get("id", f"scene_{i}"),
                "scene_type": scene_type,
                "start": sb_seg["start"],
                "end": sb_seg["end"],
                "storyboard_content": sb_seg.get("content", ""),
                "script_content": script_seg.get("text", ""),
                "emphasis": sb_seg.get("emphasis", "normal"),
                "shot_type": sb_seg.get("shot_type", "medium"),
            }
            
            scenes.append(scene)
        
        self.logger.info(f"Mapped {len(scenes)} scenes from storyboard")
        return scenes
    
    def _determine_scene_type(self, segment: Dict[str, Any], index: int, total: int) -> SceneType:
        """Determine scene type based on position and content"""
        if index == 0:
            return SceneType.HOOK
        elif index == total - 1:
            return SceneType.OUTRO
        elif "cta" in segment.get("content", "").lower():
            return SceneType.CTA
        elif segment.get("is_transition", False):
            return SceneType.TRANSITION
        else:
            return SceneType.BODY


# ============================================================================
# SCRIPT TIMING ALIGNER
# ============================================================================

class ScriptTimingAligner:
    """Ensures scene durations match script timing"""
    
    def __init__(self, tolerance: float = 0.05):
        self.tolerance = tolerance  # 5% tolerance
    
    def align_timing(
        self,
        scenes: List[Dict[str, Any]],
        script: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Adjust scene timings to match script requirements"""
        script_segments = script.get("segments", [])
        aligned_scenes = []
        
        for i, scene in enumerate(scenes):
            aligned_scene = scene.copy()
            
            if i < len(script_segments):
                script_duration = script_segments[i].get("duration", 0)
                scene_duration = scene["end"] - scene["start"]
                
                if abs(script_duration - scene_duration) / scene_duration > self.tolerance:
                    # Adjust end time to match script
                    aligned_scene["end"] = aligned_scene["start"] + script_duration
                    aligned_scene["timing_adjusted"] = True
            
            aligned_scenes.append(aligned_scene)
        
        return aligned_scenes
    
    def calculate_pacing_score(self, scenes: List[Dict[str, Any]]) -> float:
        """Calculate overall pacing quality score"""
        if not scenes:
            return 0.0
        
        durations = [s["end"] - s["start"] for s in scenes]
        avg_duration = sum(durations) / len(durations)
        variance = sum((d - avg_duration) ** 2 for d in durations) / len(durations)
        
        # Lower variance = better pacing (normalized)
        pacing_score = 1.0 / (1.0 + variance)
        return min(1.0, pacing_score)


# ============================================================================
# AESTHETIC ENGINE
# ============================================================================

class AestheticEngine:
    """Applies niche-specific visual styling"""
    
    def __init__(self):
        self.niche_rules = self._load_niche_rules()
    
    def _load_niche_rules(self) -> Dict[str, Dict[str, Any]]:
        """Define aesthetic rules per niche"""
        return {
            "tech": {
                "color_palette": ["#00ff41", "#0066cc", "#000000"],
                "font_family": "Roboto Mono",
                "transition_preference": TransitionType.CUT,
                "overlay_style": "minimal",
            },
            "lifestyle": {
                "color_palette": ["#ffb6c1", "#ffffff", "#ffd700"],
                "font_family": "Playfair Display",
                "transition_preference": TransitionType.FADE,
                "overlay_style": "elegant",
            },
            "education": {
                "color_palette": ["#4a90e2", "#ffffff", "#f5a623"],
                "font_family": "Open Sans",
                "transition_preference": TransitionType.SLIDE,
                "overlay_style": "informative",
            },
            "entertainment": {
                "color_palette": ["#ff0080", "#00ffff", "#ffff00"],
                "font_family": "Impact",
                "transition_preference": TransitionType.SWIPE,
                "overlay_style": "dynamic",
            },
        }
    
    def apply_aesthetic(
        self,
        scene: Dict[str, Any],
        niche: str,
        format_archetype: str
    ) -> Dict[str, Any]:
        """Apply niche-specific aesthetics to a scene"""
        rules = self.niche_rules.get(niche, self.niche_rules["education"])
        
        scene["aesthetic"] = {
            "color_palette": rules["color_palette"],
            "font_family": rules["font_family"],
            "overlay_style": rules["overlay_style"],
            "visual_density": self._calculate_visual_density(format_archetype),
        }
        
        return scene
    
    def _calculate_visual_density(self, format_archetype: str) -> str:
        """Determine how busy the visual composition should be"""
        density_map = {
            "short_form": "high",
            "explainer": "medium",
            "storytelling": "low",
            "tutorial": "medium",
        }
        return density_map.get(format_archetype, "medium")


# ============================================================================
# DYNAMIC ELEMENT INTEGRATOR
# ============================================================================

class DynamicElementIntegrator:
    """Adds animated graphics, text overlays, and emphasis elements"""
    
    def integrate_elements(
        self,
        scene: Dict[str, Any],
        feature_bundle: Dict[str, Any]
    ) -> List[VisualElement]:
        """Generate dynamic visual elements for a scene"""
        elements = []
        
        # Add base clip
        elements.append(VisualElement(
            type="clip",
            params={"source": f"stock_{scene['scene_id']}.mp4"},
            z_index=0
        ))
        
        # Add text overlay if script content exists
        if scene.get("script_content"):
            elements.append(self._create_text_overlay(scene))
        
        # Add emphasis elements for hooks and CTAs
        if scene["scene_type"] in [SceneType.HOOK, SceneType.CTA]:
            elements.extend(self._create_emphasis_elements(scene))
        
        # Add retention-boosting graphics at predicted drop points
        if self._should_add_retention_boost(scene, feature_bundle):
            elements.append(self._create_retention_graphic(scene))
        
        return elements
    
    def _create_text_overlay(self, scene: Dict[str, Any]) -> VisualElement:
        """Create text overlay element"""
        font_size = 36 if scene["scene_type"] == SceneType.HOOK else 28
        
        return VisualElement(
            type="overlay_text",
            params={
                "text": scene["script_content"][:100],
                "font_size": font_size,
                "position": "center",
                "animation": "fade_in",
            },
            z_index=10
        )
    
    def _create_emphasis_elements(self, scene: Dict[str, Any]) -> List[VisualElement]:
        """Create visual emphasis for important moments"""
        elements = []
        
        # Add arrow or highlight
        elements.append(VisualElement(
            type="graphic",
            params={
                "shape": "arrow",
                "color": "#ff0000",
                "position": "bottom_center",
                "animation": "bounce",
            },
            z_index=15
        ))
        
        # Add emoji for emotional emphasis
        if scene["scene_type"] == SceneType.HOOK:
            elements.append(VisualElement(
                type="emoji",
                params={
                    "emoji": "🔥",
                    "size": 48,
                    "position": "top_right",
                    "animation": "pulse",
                },
                z_index=20
            ))
        
        return elements
    
    def _should_add_retention_boost(
        self,
        scene: Dict[str, Any],
        features: Dict[str, Any]
    ) -> bool:
        """Determine if retention-boosting graphics are needed"""
        retention_curve = features.get("retention_curve", {})
        scene_time = scene["start"]
        
        # Check if this scene is at a predicted retention dip
        predicted_retention = retention_curve.get(int(scene_time), 1.0)
        return predicted_retention < 0.7
    
    def _create_retention_graphic(self, scene: Dict[str, Any]) -> VisualElement:
        """Create attention-grabbing graphic"""
        return VisualElement(
            type="graphic",
            params={
                "shape": "burst",
                "color": "#ffff00",
                "size": 64,
                "position": "center",
                "animation": "explosion",
            },
            duration=0.5,
            z_index=25
        )


# ============================================================================
# TRANSITION MANAGER
# ============================================================================

class TransitionManager:
    """Manages scene transitions for continuity and retention"""
    
    def create_transitions(
        self,
        scenes: List[Dict[str, Any]],
        niche_rules: Dict[str, Any]
    ) -> Dict[str, List[Transition]]:
        """Generate appropriate transitions between scenes"""
        transitions = {}
        
        for i in range(len(scenes) - 1):
            current_scene = scenes[i]
            next_scene = scenes[i + 1]
            
            transition = self._select_transition(
                current_scene,
                next_scene,
                niche_rules
            )
            
            transitions[current_scene["scene_id"]] = [transition]
        
        return transitions
    
    def _select_transition(
        self,
        current: Dict[str, Any],
        next_scene: Dict[str, Any],
        rules: Dict[str, Any]
    ) -> Transition:
        """Select appropriate transition based on scene types"""
        
        # Fast cuts for high energy
        if current["scene_type"] == SceneType.HOOK:
            return Transition(
                type=TransitionType.CUT,
                duration=0.1
            )
        
        # Smooth fades for emotional moments
        if next_scene["scene_type"] == SceneType.OUTRO:
            return Transition(
                type=TransitionType.FADE,
                duration=0.8
            )
        
        # Default to niche preference
        default_type = rules.get("transition_preference", TransitionType.CUT)
        return Transition(
            type=default_type,
            duration=0.3
        )


# ============================================================================
# EMOTION INTEGRATOR
# ============================================================================

class EmotionIntegrator:
    """Aligns visual tone with emotional curve"""
    
    def integrate_emotion(
        self,
        scenes: List[Dict[str, Any]],
        emotional_curve: Dict[str, float]
    ) -> List[Dict[str, Any]]:
        """Add emotional context to each scene"""
        
        for scene in scenes:
            scene_time = int(scene["start"])
            emotion_intensity = emotional_curve.get(scene_time, 0.5)
            
            scene["emotion"] = self._map_emotion(
                scene["scene_type"],
                emotion_intensity
            )
            scene["emotion_intensity"] = emotion_intensity
        
        return scenes
    
    def _map_emotion(self, scene_type: SceneType, intensity: float) -> EmotionType:
        """Map scene type and intensity to emotion"""
        
        if scene_type == SceneType.HOOK:
            return EmotionType.EXCITEMENT if intensity > 0.7 else EmotionType.CURIOSITY
        elif scene_type == SceneType.CTA:
            return EmotionType.EXCITEMENT
        elif scene_type == SceneType.OUTRO:
            return EmotionType.SATISFACTION
        else:
            if intensity > 0.7:
                return EmotionType.SURPRISE
            elif intensity < 0.3:
                return EmotionType.CALM
            else:
                return EmotionType.CURIOSITY


# ============================================================================
# OUTPUT FORMATTER
# ============================================================================

class OutputFormatter:
    """Formats visual plan for downstream consumption"""
    
    def format_output(
        self,
        video_id: str,
        scenes: List[Scene],
        validation: Dict[str, bool],
        metadata: Dict[str, Any]
    ) -> VisualPlan:
        """Create final visual plan output"""
        
        total_duration = max(s.end for s in scenes) if scenes else 0.0
        
        return VisualPlan(
            video_id=video_id,
            composer_version="1.0.0",
            scenes=scenes,
            total_duration=total_duration,
            validation=validation,
            metadata=metadata
        )
    
    def to_json(self, visual_plan: VisualPlan) -> str:
        """Serialize visual plan to JSON"""
        
        def serialize(obj):
            if isinstance(obj, (SceneType, TransitionType, EmotionType, VisualEffect)):
                return obj.value
            elif isinstance(obj, (Scene, VisualElement, Transition, VisualPlan)):
                return obj.__dict__
            return str(obj)
        
        return json.dumps(visual_plan, default=serialize, indent=2)


# ============================================================================
# MAIN VISUAL COMPOSER
# ============================================================================

class VisualComposer:
    """
    Main orchestrator for visual composition.
    
    Responsibility: Create complete visual plans from storyboards and scripts.
    Does NOT: Generate audio, post content, or predict virality.
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.logger = logging.getLogger(__name__)
        
        # Initialize components
        self.validator = InputValidator()
        self.scene_mapper = SceneMapper(self.logger)
        self.timing_aligner = ScriptTimingAligner()
        self.aesthetic_engine = AestheticEngine()
        self.element_integrator = DynamicElementIntegrator()
        self.transition_manager = TransitionManager()
        self.emotion_integrator = EmotionIntegrator()
        self.output_formatter = OutputFormatter()
    
    def compose(
        self,
        video_id: str,
        storyboard: Dict[str, Any],
        script: Dict[str, Any],
        feature_bundle: Dict[str, Any],
        emotional_curve: Dict[str, float],
        format_archetype: str,
        niche_visual_rules: Optional[Dict[str, Any]] = None,
        segment_context: Optional[Dict[str, Any]] = None
    ) -> VisualPlan:
        """
        Main composition method - creates complete visual plan.
        
        Returns:
            VisualPlan: Complete visual specification for rendering
        """
        
        self.logger.info(f"Starting visual composition for video_id: {video_id}")
        
        # Step 1: Validate inputs
        validation_results = self._validate_inputs(
            storyboard, script, feature_bundle
        )
        
        if not all(validation_results.values()):
            raise ValueError(f"Input validation failed: {validation_results}")
        
        # Step 2: Map storyboard to scenes
        scene_defs = self.scene_mapper.map_segments_to_scenes(storyboard, script)
        
        # Step 3: Align timing with script
        aligned_scenes = self.timing_aligner.align_timing(scene_defs, script)
        
        # Step 4: Apply aesthetics
        niche = storyboard.get("niche", "education")
        styled_scenes = [
            self.aesthetic_engine.apply_aesthetic(s, niche, format_archetype)
            for s in aligned_scenes
        ]
        
        # Step 5: Integrate emotional curve
        emotional_scenes = self.emotion_integrator.integrate_emotion(
            styled_scenes, emotional_curve
        )
        
        # Step 6: Create visual elements
        scenes_with_elements = []
        for scene_def in emotional_scenes:
            elements = self.element_integrator.integrate_elements(
                scene_def, feature_bundle
            )
            
            scene = Scene(
                scene_id=scene_def["scene_id"],
                scene_type=scene_def["scene_type"],
                start=scene_def["start"],
                end=scene_def["end"],
                visual_elements=elements,
                transitions=[],
                emotion=scene_def["emotion"],
                script_alignment={
                    "content": scene_def.get("script_content", ""),
                    "timing_adjusted": scene_def.get("timing_adjusted", False),
                },
                metadata=scene_def.get("aesthetic", {})
            )
            
            scenes_with_elements.append(scene)
        
        # Step 7: Add transitions
        niche_rules = niche_visual_rules or {}
        transitions_map = self.transition_manager.create_transitions(
            [s.__dict__ for s in scenes_with_elements],
            niche_rules
        )
        
        for scene in scenes_with_elements:
            if scene.scene_id in transitions_map:
                scene.transitions = transitions_map[scene.scene_id]
        
        # Step 8: Validate output
        output_validation = self._validate_output(scenes_with_elements, storyboard)
        
        # Step 9: Format and return
        metadata = {
            "niche": niche,
            "format_archetype": format_archetype,
            "total_scenes": len(scenes_with_elements),
            "pacing_score": self.timing_aligner.calculate_pacing_score(
                [s.__dict__ for s in scenes_with_elements]
            ),
        }
        
        visual_plan = self.output_formatter.format_output(
            video_id, scenes_with_elements, output_validation, metadata
        )
        
        self.logger.info(
            f"Visual composition complete: {len(scenes_with_elements)} scenes, "
            f"{visual_plan.total_duration:.2f}s total duration"
        )
        
        return visual_plan
    
    def _validate_inputs(
        self,
        storyboard: Dict[str, Any],
        script: Dict[str, Any],
        features: Dict[str, Any]
    ) -> Dict[str, bool]:
        """Validate all inputs"""
        
        sb_valid, sb_errors = self.validator.validate_storyboard(storyboard)
        sc_valid, sc_errors = self.validator.validate_script(script)
        ft_valid, ft_errors = self.validator.validate_features(features)
        
        if not sb_valid:
            self.logger.error(f"Storyboard validation errors: {sb_errors}")
        if not sc_valid:
            self.logger.error(f"Script validation errors: {sc_errors}")
        if not ft_valid:
            self.logger.error(f"Feature validation errors: {ft_errors}")
        
        return {
            "storyboard_valid": sb_valid,
            "script_valid": sc_valid,
            "features_valid": ft_valid,
        }
    
    def _validate_output(
        self,
        scenes: List[Scene],
        storyboard: Dict[str, Any]
    ) -> Dict[str, bool]:
        """Validate output quality"""
        
        # Check scene durations match storyboard ±5%
        storyboard_duration = storyboard.get("total_duration", 0)
        actual_duration = max(s.end for s in scenes) if scenes else 0
        duration_ok = abs(actual_duration - storyboard_duration) / storyboard_duration < 0.05
        
        # Check pacing alignment
        pacing_ok = all(s.duration > 0 for s in scenes)
        
        # Check visual style consistency
        style_ok = all(s.metadata for s in scenes)
        
        return {
            "scene_durations_ok": duration_ok,
            "pacing_alignment_ok": pacing_ok,
            "visual_style_ok": style_ok,
        }


# ============================================================================
# FACTORY & CONVENIENCE FUNCTIONS
# ============================================================================

def create_visual_composer(config: Optional[Dict[str, Any]] = None) -> VisualComposer:
    """Factory function to create VisualComposer instance"""
    return VisualComposer(config)


def compose_visual_plan(
    video_id: str,
    storyboard: Dict[str, Any],
    script: Dict[str, Any],
    feature_bundle: Dict[str, Any],
    **kwargs
) -> VisualPlan:
    """
    Convenience function for one-shot visual composition.
    
    Args:
        video_id: Unique identifier for video
        storyboard: Storyboard from storyboard.py
        script: Generated script from script_generator.py
        feature_bundle: Feature signals from virality_feature_engine.py
        **kwargs: Additional parameters (emotional_curve, format_archetype, etc.)
    
    Returns:
        VisualPlan: Complete visual specification
    """
    composer = create_visual_composer()
    
    emotional_curve = kwargs.get("emotional_curve", {})
    format_archetype = kwargs.get("format_archetype", "short_form")
    niche_rules = kwargs.get("niche_visual_rules")
    segment_context = kwargs.get("segment_context")
    
    return composer.compose(
        video_id=video_id,
        storyboard=storyboard,
        script=script,
        feature_bundle=feature_bundle,
        emotional_curve=emotional_curve,
        format_archetype=format_archetype,
        niche_visual_rules=niche_rules,
        segment_context=segment_context
    )


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    # Example usage
    logging.basicConfig(level=logging.INFO)
    
    # Mock inputs
    mock_storyboard = {
        "niche": "tech",
        "total_duration": 30.0,
        "segments": [
            {"id": "hook", "start": 0, "end": 5, "content": "Hook content"},
            {"id": "body1", "start": 5, "end": 20, "content": "Main content"},
            {"id": "outro", "start": 20, "end": 30, "content": "CTA"},
        ]
    }
    
    mock_script = {
        "total_duration": 30.0,
        "segments": [
            {"duration": 5, "text": "Welcome to this video!"},
            {"duration": 15, "text": "Here's the main point..."},
            {"duration": 10, "text": "Don't forget to subscribe!"},
        ]
    }
    
    mock_features = {
        "modality_alignment": 0.85,
        "retention_curve": {0: 1.0, 5: 0.9, 10: 0.75, 15: 0.8, 20: 0.9},
        "narrative_intensity": 0.7,
    }
    
    # Compose visual plan
    plan = compose_visual_plan(
        video_id="test_001",
        storyboard=mock_storyboard,
        script=mock_script,
        feature_bundle=mock_features,
        emotional_curve={0: 0.9, 10: 0.6, 20: 0.8},
        format_archetype="short_form"
    )
    
    # Output JSON
    composer = create_visual_composer()
    print(composer.output_formatter.to_json(plan))