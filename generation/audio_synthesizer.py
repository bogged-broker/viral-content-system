"""
/generation/audio_synthesizer.py

Production-grade audio synthesis system for viral content generation at massive scale.

PURPOSE:
    Synthesizes, enhances, and structures audio tracks with emotional alignment,
    retention optimization, and platform-specific tuning for 5M+ baseline engagement
    and 30M-300M repeatable virality.

SCALE TARGETS:
    - 5M+ baseline engagement per video
    - 30M-300M repeatable virality capability
    - 3k-6k videos/day production capacity
    - Sub-5s audio generation latency (cached/parallel)

ARCHITECTURE:
    AudioSynthesizer
    ├── InputValidator          # Contract validation & sanitization
    ├── ScriptParser           # Script → timed phoneme/segment extraction
    ├── EmotionMapper          # Emotional arc → audio intensity/tempo mapping
    ├── VoiceGenerator         # TTS/voice cloning with platform adaptation
    ├── MusicLayerComposer     # Niche-specific dynamic music scoring
    ├── SFXComposer            # Event-aligned sound effects
    ├── Mixer                  # Multi-track audio combining with EQ
    ├── Normalizer             # Loudness/pacing/stereo correction
    ├── OutputFormatter        # File export & metadata packaging
    ├── RLFeedbackHook         # RL agent data emission
    └── AuditLogger            # Full deterministic audit trails

INTEGRATION POINTS:
    - /generation/script_generator.py → script_text input
    - /generation/emotional_arc_engine.py → emotional_arc input
    - /generation/retention_optimizer.py → retention_targets input
    - /models/ml_models/engagement_predictor.py → predictive adaptation
    - /feature_extraction/virality_feature_engine.py → feature conditioning
    - /generation/visual_composer.py → cross-modal timing sync
    - /orchestration/workflow_manager.py → orchestration layer
    - /utils/logger.py → centralized logging

VERSION: 1.2.3
LOC TARGET: 6,300-9,400 (production-scale)
"""

import os
import json
import hashlib
import traceback
from typing import Dict, List, Optional, Tuple, Any, Union
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from datetime import datetime
import numpy as np
import random
import copy
import re
import warnings

# Audio processing imports (production dependencies)
try:
    import librosa
    import soundfile as sf
    from pydub import AudioSegment
    from pydub.effects import normalize, compress_dynamic_range
    AUDIO_LIBS_AVAILABLE = True
except ImportError:
    AUDIO_LIBS_AVAILABLE = False
    warnings.warn("Audio libraries not available. Running in mock mode.")

# TTS & AI imports
try:
    import torch
    import torchaudio
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False

# Internal imports (simulated for standalone execution)
try:
    from utils.logger import ViralLogger
    from config.system_config import SystemConfig
    from models.ml_models.engagement_predictor import EngagementPredictor
except ImportError:
    # Fallback for standalone testing
    class ViralLogger:
        @staticmethod
        def log_info(msg, **kwargs): print(f"[INFO] {msg}", kwargs)
        @staticmethod
        def log_warning(msg, **kwargs): print(f"[WARN] {msg}", kwargs)
        @staticmethod
        def log_error(msg, **kwargs): print(f"[ERROR] {msg}", kwargs)
        @staticmethod
        def log_debug(msg, **kwargs): print(f"[DEBUG] {msg}", kwargs)
    
    class SystemConfig:
        AUDIO_SAMPLE_RATE = 44100
        AUDIO_OUTPUT_DIR = "./output/audio"
        AUDIO_CACHE_DIR = "./cache/audio"
        TTS_MODEL_PATH = "./models/tts"
        MUSIC_LIBRARY_PATH = "./assets/music"
        SFX_LIBRARY_PATH = "./assets/sfx"
    
    class EngagementPredictor:
        @staticmethod
        def predict_trajectory(features): 
            return {"predicted_engagement": 5000000, "confidence": 0.85}


# ============================================================================
# CONSTANTS & CONFIGURATION
# ============================================================================

VERSION = "1.2.3"
MODULE_NAME = "audio_synthesizer"

# Audio quality targets
TARGET_SAMPLE_RATE = 44100
TARGET_BIT_DEPTH = 16
TARGET_CHANNELS = 2  # Stereo

# Retention optimization
HOOK_DURATION_SEC = 5.0  # First 5 seconds optimized
HOOK_VOLUME_BOOST = 1.15  # 15% louder for attention
HOOK_BASS_BOOST_DB = 3.0

# Platform-specific audio specs
PLATFORM_AUDIO_SPECS = {
    "tiktok": {
        "max_duration_sec": 60,
        "sample_rate": 44100,
        "loudness_lufs": -14.0,
        "music_volume_ratio": 0.3,
        "prefer_high_energy": True
    },
    "youtube_shorts": {
        "max_duration_sec": 60,
        "sample_rate": 48000,
        "loudness_lufs": -14.0,
        "music_volume_ratio": 0.25,
        "prefer_high_energy": True
    },
    "instagram_reels": {
        "max_duration_sec": 90,
        "sample_rate": 44100,
        "loudness_lufs": -13.0,
        "music_volume_ratio": 0.35,
        "prefer_high_energy": True
    },
    "youtube_long": {
        "max_duration_sec": 600,
        "sample_rate": 48000,
        "loudness_lufs": -16.0,
        "music_volume_ratio": 0.20,
        "prefer_high_energy": False
    }
}

# Default fallback specs
DEFAULT_PLATFORM_SPEC = {
    "max_duration_sec": 60,
    "sample_rate": 44100,
    "loudness_lufs": -14.0,
    "music_volume_ratio": 0.3,
    "prefer_high_energy": True
}

# Voice profiles (TTS configuration)
VOICE_PROFILES = {
    "energetic_male": {
        "pitch_shift": 0,
        "speed": 1.1,
        "energy": 1.2,
        "model": "tts_energetic_m_v2"
    },
    "calm_female": {
        "pitch_shift": 2,
        "speed": 0.95,
        "energy": 0.85,
        "model": "tts_calm_f_v2"
    },
    "authoritative_male": {
        "pitch_shift": -1,
        "speed": 1.0,
        "energy": 1.0,
        "model": "tts_auth_m_v2"
    },
    "friendly_female": {
        "pitch_shift": 1,
        "speed": 1.05,
        "energy": 1.1,
        "model": "tts_friendly_f_v2"
    },
    "dramatic_narrator": {
        "pitch_shift": -2,
        "speed": 0.9,
        "energy": 1.3,
        "model": "tts_dramatic_v2"
    }
}

# Emotion-to-audio parameter mapping
EMOTION_AUDIO_MAP = {
    "excitement": {"tempo_mult": 1.2, "volume_mult": 1.1, "bass_boost": 2},
    "suspense": {"tempo_mult": 0.9, "volume_mult": 0.95, "bass_boost": 4},
    "joy": {"tempo_mult": 1.15, "volume_mult": 1.05, "bass_boost": 1},
    "sadness": {"tempo_mult": 0.85, "volume_mult": 0.9, "bass_boost": 0},
    "anger": {"tempo_mult": 1.1, "volume_mult": 1.15, "bass_boost": 5},
    "calm": {"tempo_mult": 0.95, "volume_mult": 0.88, "bass_boost": 0},
    "surprise": {"tempo_mult": 1.25, "volume_mult": 1.2, "bass_boost": 3},
    "fear": {"tempo_mult": 1.05, "volume_mult": 0.92, "bass_boost": 2}
}

# Audio quality thresholds (watchdogs)
QUALITY_THRESHOLDS = {
    "max_peak_db": -1.0,  # Prevent clipping
    "min_rms_db": -40.0,  # Prevent silence
    "max_dynamic_range_db": 40.0,
    "stereo_correlation_min": 0.3,  # Ensure stereo width
    "silence_threshold_db": -50.0
}


# ============================================================================
# DATA STRUCTURES
# ============================================================================

class AudioLayerType(Enum):
    """Audio layer types for multi-track synthesis"""
    NARRATION = "narration"
    MUSIC = "music"
    SFX = "sfx"
    AMBIENT = "ambient"


@dataclass
class AudioSegment:
    """Timed audio segment with metadata"""
    start_time: float  # seconds
    end_time: float  # seconds
    text: Optional[str] = None  # For narration segments
    phonemes: Optional[List[str]] = None  # Phoneme breakdown
    emotion: Optional[str] = None  # Emotional tag
    intensity: float = 0.5  # 0-1 emotional intensity
    layer_type: AudioLayerType = AudioLayerType.NARRATION
    
    def __post_init__(self):
        if self.end_time <= self.start_time:
            raise ValueError(f"Invalid segment timing: {self.start_time} -> {self.end_time}")
    
    @property
    def duration(self) -> float:
        return self.end_time - self.start_time


@dataclass
class AudioTrack:
    """Single audio track with metadata"""
    layer_type: AudioLayerType
    file_path: Optional[str] = None
    audio_data: Optional[np.ndarray] = None
    sample_rate: int = TARGET_SAMPLE_RATE
    duration: float = 0.0
    volume: float = 1.0  # Mixing volume multiplier
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        if self.audio_data is not None:
            self.duration = len(self.audio_data) / self.sample_rate


@dataclass
class ValidationResult:
    """Input validation result"""
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    validated_data: Optional[Dict[str, Any]] = None


@dataclass
class AudioSynthesisResult:
    """Complete audio synthesis output"""
    video_id: str
    audio_tracks: Dict[str, str]  # layer_type -> file_path
    mixdown_file: str
    mix_metadata: Dict[str, Any]
    failure_modes: List[str]
    model_version: str = VERSION
    synthesis_timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    processing_time_ms: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return asdict(self)


# ============================================================================
# INPUT VALIDATOR
# ============================================================================

class InputValidator:
    """
    Validates all input contracts for audio synthesis.
    
    RESPONSIBILITIES:
        - Mandatory field validation (video_id, script_text)
        - Optional field defaulting (emotional_arc, retention_targets)
        - Type checking and sanitization
        - Platform spec validation
        - Seed validation for determinism
    
    LOC: ~400-600
    """
    
    def __init__(self):
        self.logger = ViralLogger()
    
    def validate(self, 
                 video_id: str,
                 script_text: str,
                 emotional_arc: Optional[Dict] = None,
                 retention_targets: Optional[Dict] = None,
                 platform_specs: Optional[Dict] = None,
                 voice_profile: Optional[Dict] = None,
                 niche_audio_profile: Optional[Dict] = None,
                 audio_seed: Optional[int] = None) -> ValidationResult:
        """
        Validate all inputs according to strict contracts.
        
        Returns:
            ValidationResult with validation status and sanitized data
        """
        errors = []
        warnings = []
        
        # Mandatory validations
        if not video_id or not isinstance(video_id, str):
            errors.append("video_id must be a non-empty string")
        elif len(video_id) > 128:
            errors.append("video_id exceeds maximum length of 128 characters")
        
        if not script_text or not isinstance(script_text, str):
            errors.append("script_text must be a non-empty string")
        elif len(script_text.strip()) < 10:
            errors.append("script_text too short (minimum 10 characters)")
        elif len(script_text) > 50000:
            errors.append("script_text exceeds maximum length of 50000 characters")
        
        # Optional field validation with defaults
        validated_emotional_arc = self._validate_emotional_arc(emotional_arc, warnings)
        validated_retention_targets = self._validate_retention_targets(retention_targets, warnings)
        validated_platform_specs = self._validate_platform_specs(platform_specs, warnings)
        validated_voice_profile = self._validate_voice_profile(voice_profile, warnings)
        validated_niche_profile = self._validate_niche_profile(niche_audio_profile, warnings)
        validated_seed = self._validate_seed(audio_seed, warnings)
        
        # Return validation result
        is_valid = len(errors) == 0
        validated_data = None
        
        if is_valid:
            validated_data = {
                "video_id": video_id,
                "script_text": script_text.strip(),
                "emotional_arc": validated_emotional_arc,
                "retention_targets": validated_retention_targets,
                "platform_specs": validated_platform_specs,
                "voice_profile": validated_voice_profile,
                "niche_audio_profile": validated_niche_profile,
                "audio_seed": validated_seed
            }
        
        return ValidationResult(
            is_valid=is_valid,
            errors=errors,
            warnings=warnings,
            validated_data=validated_data
        )
    
    def _validate_emotional_arc(self, arc: Optional[Dict], warnings: List[str]) -> Dict:
        """Validate emotional arc or return default"""
        if arc is None:
            warnings.append("emotional_arc not provided, using default neutral arc")
            return self._default_emotional_arc()
        
        if not isinstance(arc, dict):
            warnings.append("emotional_arc invalid type, using default")
            return self._default_emotional_arc()
        
        # Validate required fields
        if "segments" not in arc:
            warnings.append("emotional_arc missing 'segments', using default")
            return self._default_emotional_arc()
        
        return arc
    
    def _validate_retention_targets(self, targets: Optional[Dict], warnings: List[str]) -> Dict:
        """Validate retention targets or return defaults"""
        if targets is None:
            warnings.append("retention_targets not provided, using defaults")
            return self._default_retention_targets()
        
        if not isinstance(targets, dict):
            warnings.append("retention_targets invalid type, using defaults")
            return self._default_retention_targets()
        
        return {
            "hook_duration_sec": targets.get("hook_duration_sec", HOOK_DURATION_SEC),
            "target_watch_time_pct": targets.get("target_watch_time_pct", 0.70),
            "climax_timestamp_sec": targets.get("climax_timestamp_sec", None)
        }
    
    def _validate_platform_specs(self, specs: Optional[Dict], warnings: List[str]) -> Dict:
        """Validate platform specs or return default"""
        if specs is None:
            warnings.append("platform_specs not provided, using default (TikTok)")
            return PLATFORM_AUDIO_SPECS["tiktok"]
        
        if not isinstance(specs, dict):
            warnings.append("platform_specs invalid type, using default")
            return PLATFORM_AUDIO_SPECS["tiktok"]
        
        platform_name = specs.get("platform", "tiktok").lower()
        
        if platform_name in PLATFORM_AUDIO_SPECS:
            return PLATFORM_AUDIO_SPECS[platform_name]
        else:
            warnings.append(f"Unknown platform '{platform_name}', using default")
            return DEFAULT_PLATFORM_SPEC
    
    def _validate_voice_profile(self, profile: Optional[Dict], warnings: List[str]) -> Dict:
        """Validate voice profile or return default"""
        if profile is None:
            warnings.append("voice_profile not provided, using 'energetic_male'")
            return VOICE_PROFILES["energetic_male"]
        
        if not isinstance(profile, dict):
            warnings.append("voice_profile invalid type, using default")
            return VOICE_PROFILES["energetic_male"]
        
        profile_name = profile.get("name", "energetic_male")
        
        if profile_name in VOICE_PROFILES:
            return VOICE_PROFILES[profile_name]
        else:
            warnings.append(f"Unknown voice profile '{profile_name}', using default")
            return VOICE_PROFILES["energetic_male"]
    
    def _validate_niche_profile(self, profile: Optional[Dict], warnings: List[str]) -> Dict:
        """Validate niche audio profile or return defaults"""
        if profile is None:
            warnings.append("niche_audio_profile not provided, using defaults")
            return self._default_niche_profile()
        
        if not isinstance(profile, dict):
            warnings.append("niche_audio_profile invalid type, using defaults")
            return self._default_niche_profile()
        
        return {
            "music_genre": profile.get("music_genre", "electronic"),
            "energy_level": profile.get("energy_level", "high"),
            "sfx_density": profile.get("sfx_density", "medium"),
            "music_presence": profile.get("music_presence", 0.3)
        }
    
    def _validate_seed(self, seed: Optional[int], warnings: List[str]) -> int:
        """Validate audio seed or generate deterministic one"""
        if seed is None:
            warnings.append("audio_seed not provided, generating from timestamp")
            return int(datetime.utcnow().timestamp() * 1000000) % (2**31)
        
        if not isinstance(seed, int):
            warnings.append("audio_seed invalid type, generating new seed")
            return int(datetime.utcnow().timestamp() * 1000000) % (2**31)
        
        return seed % (2**31)  # Ensure within valid range
    
    def _default_emotional_arc(self) -> Dict:
        """Default emotional arc (neutral/gradual build)"""
        return {
            "segments": [
                {"start": 0.0, "end": 0.2, "emotion": "calm", "intensity": 0.5},
                {"start": 0.2, "end": 0.8, "emotion": "excitement", "intensity": 0.7},
                {"start": 0.8, "end": 1.0, "emotion": "joy", "intensity": 0.8}
            ]
        }
    
    def _default_retention_targets(self) -> Dict:
        """Default retention targets"""
        return {
            "hook_duration_sec": HOOK_DURATION_SEC,
            "target_watch_time_pct": 0.70,
            "climax_timestamp_sec": None
        }
    
    def _default_niche_profile(self) -> Dict:
        """Default niche audio profile"""
        return {
            "music_genre": "electronic",
            "energy_level": "high",
            "sfx_density": "medium",
            "music_presence": 0.3
        }


# ============================================================================
# SCRIPT PARSER
# ============================================================================

class ScriptParser:
    """
    Converts script text into timed segments with phoneme breakdown.
    
    RESPONSIBILITIES:
        - Sentence/phrase segmentation
        - Timing estimation based on TTS models
        - Phoneme extraction for fine-grained control
        - Punctuation-based pacing
        - Word emphasis detection
    
    LOC: ~600-900
    """
    
    def __init__(self):
        self.logger = ViralLogger()
        # Average speech rates (words per minute)
        self.wpm_slow = 120
        self.wpm_normal = 150
        self.wpm_fast = 180
    
    def parse(self, 
              script_text: str, 
              voice_profile: Dict,
              target_duration: Optional[float] = None) -> List[AudioSegment]:
        """
        Parse script into timed audio segments.
        
        Args:
            script_text: Raw script text
            voice_profile: Voice configuration affecting speed
            target_duration: Optional target duration to fit
        
        Returns:
            List of AudioSegment objects with timing
        """
        # Clean and normalize script
        script_text = self._clean_script(script_text)
        
        # Split into sentences/phrases
        sentences = self._segment_sentences(script_text)
        
        # Calculate base timing
        speech_speed = voice_profile.get("speed", 1.0)
        wpm = self.wpm_normal * speech_speed
        
        # Create timed segments
        segments = []
        current_time = 0.0
        
        for sentence in sentences:
            # Estimate duration
            word_count = len(sentence.split())
            duration = (word_count / wpm) * 60.0  # Convert WPM to seconds
            
            # Add pause for punctuation
            duration += self._calculate_pause_duration(sentence)
            
            # Create segment
            segment = AudioSegment(
                start_time=current_time,
                end_time=current_time + duration,
                text=sentence,
                phonemes=self._extract_phonemes(sentence),
                layer_type=AudioLayerType.NARRATION
            )
            
            segments.append(segment)
            current_time += duration
        
        # Adjust timing if target duration specified
        if target_duration and len(segments) > 0:
            segments = self._adjust_to_target_duration(segments, target_duration)
        
        self.logger.log_info(
            f"Parsed script into {len(segments)} segments, "
            f"total duration: {current_time:.2f}s",
            module=MODULE_NAME,
            component="ScriptParser"
        )
        
        return segments
    
    def _clean_script(self, text: str) -> str:
        """Clean and normalize script text"""
        # Remove excessive whitespace
        text = re.sub(r'\s+', ' ', text)
        # Normalize quotes
        text = text.replace('"', '"').replace('"', '"')
        # Remove HTML tags if present
        text = re.sub(r'<[^>]+>', '', text)
        return text.strip()
    
    def _segment_sentences(self, text: str) -> List[str]:
        """Split text into sentences/phrases"""
        # Split on sentence-ending punctuation
        sentences = re.split(r'[.!?]+', text)
        
        # Further split long sentences on commas/semicolons
        result = []
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            
            # If sentence is very long, split on commas
            if len(sentence.split()) > 20:
                parts = re.split(r'[,;]+', sentence)
                result.extend([p.strip() for p in parts if p.strip()])
            else:
                result.append(sentence)
        
        return result
    
    def _calculate_pause_duration(self, sentence: str) -> float:
        """Calculate pause duration based on punctuation"""
        if sentence.endswith('!') or sentence.endswith('?'):
            return 0.4  # Longer pause for questions/exclamations
        elif sentence.endswith('.'):
            return 0.3  # Standard pause
        elif ',' in sentence or ';' in sentence:
            return 0.15  # Brief pause within sentence
        return 0.2  # Default pause
    
    def _extract_phonemes(self, text: str) -> List[str]:
        """
        Extract phonemes from text (simplified English phoneme mapping).
        Production version would use proper phoneme library (e.g., phonemizer).
        """
        # Simplified phoneme extraction (mock implementation)
        # Real implementation would use: phonemizer, espeak, or CMU dict
        words = text.lower().split()
        phonemes = []
        
        for word in words:
            # Simple vowel/consonant pattern
            word_phonemes = []
            for char in word:
                if char in 'aeiou':
                    word_phonemes.append(f"V:{char}")
                elif char.isalpha():
                    word_phonemes.append(f"C:{char}")
            phonemes.extend(word_phonemes)
        
        return phonemes
    
    def _adjust_to_target_duration(self, 
                                   segments: List[AudioSegment], 
                                   target_duration: float) -> List[AudioSegment]:
        """Adjust segment timing to fit target duration"""
        current_duration = segments[-1].end_time
        scale_factor = target_duration / current_duration
        
        adjusted = []
        for segment in segments:
            adjusted_segment = AudioSegment(
                start_time=segment.start_time * scale_factor,
                end_time=segment.end_time * scale_factor,
                text=segment.text,
                phonemes=segment.phonemes,
                emotion=segment.emotion,
                intensity=segment.intensity,
                layer_type=segment.layer_type
            )
            adjusted.append(adjusted_segment)
        
        return adjusted


# ============================================================================
# EMOTION MAPPER
# ============================================================================

class EmotionMapper:
    """
    Maps emotional arcs to audio parameters (tempo, volume, effects).
    
    RESPONSIBILITIES:
        - Convert emotional arc segments to audio parameters
        - Dynamic tempo/intensity adjustment
        - Smooth transitions between emotional states
        - Platform-specific emotional tuning
    
    LOC: ~700-1,000
    """
    
    def __init__(self):
        self.logger = ViralLogger()
        self.emotion_map = EMOTION_AUDIO_MAP
    
    def map_emotions(self, 
                     segments: List[AudioSegment],
                     emotional_arc: Dict,
                     platform_specs: Dict) -> List[AudioSegment]:
        """
        Map emotional arc to audio segment parameters.
        
        Args:
            segments: Parsed script segments
            emotional_arc: Emotional trajectory data
            platform_specs: Platform-specific tuning
        
        Returns:
            Segments with emotion parameters applied
        """
        # Extract total duration
        total_duration = segments[-1].end_time if segments else 0.0
        
        # Normalize emotional arc segments
        arc_segments = self._normalize_arc_segments(
            emotional_arc.get("segments", []),
            total_duration
        )
        
        # Apply emotions to segments
        enhanced_segments = []
        for segment in segments:
            # Find matching emotional arc
            emotion_data = self._find_emotion_at_time(
                segment.start_time,
                segment.end_time,
                arc_segments
            )
            
            # Apply emotion
            segment.emotion = emotion_data["emotion"]
            segment.intensity = emotion_data["intensity"]
            
            enhanced_segments.append(segment)
        
        # Apply platform-specific tuning
        if platform_specs.get("prefer_high_energy", False):
            enhanced_segments = self._boost_energy(enhanced_segments)
        
        self.logger.log_info(
            f"Mapped emotions to {len(enhanced_segments)} segments",
            module=MODULE_NAME,
            component="EmotionMapper"
        )
        
        return enhanced_segments
    
    def _normalize_arc_segments(self, 
                                arc_segments: List[Dict], 
                                total_duration: float) -> List[Dict]:
        """Normalize arc segments to absolute timing"""
        normalized = []
        
        for seg in arc_segments:
            # Convert fractional positions to absolute time
            start_time = seg.get("start", 0.0) * total_duration
            end_time = seg.get("end", 1.0) * total_duration
            
            normalized.append({
                "start_time": start_time,
                "end_time": end_time,
                "emotion": seg.get("emotion", "calm"),
                "intensity": seg.get("intensity", 0.5)
            })
        
        return normalized
    
    def _find_emotion_at_time(self, 
                             start_time: float,
                             end_time: float,
                             arc_segments: List[Dict]) -> Dict:
        """Find dominant emotion for a time range"""
        segment_midpoint = (start_time + end_time) / 2.0
        
        # Find overlapping arc segment
        for arc_seg in arc_segments:
            if arc_seg["start_time"] <= segment_midpoint <= arc_seg["end_time"]:
                return {
                    "emotion": arc_seg["emotion"],
                    "intensity": arc_seg["intensity"]
                }
        
        # Default if no match
        return {"emotion": "calm", "intensity": 0.5}
    
    def _boost_energy(self, segments: List[AudioSegment]) -> List[AudioSegment]:
        """Boost energy levels for high-energy platforms"""
        boosted = []
        
        for segment in segments:
            # Increase intensity by 20% (capped at 1.0)
            segment.intensity = min(1.0, segment.intensity * 1.2)
            
            # Prefer high-energy emotions
            if segment.emotion in ["calm", "sadness"]:
                segment.emotion = "excitement"
            
            boosted.append(segment)
        
        return boosted
    
    def get_audio_parameters(self, emotion: str, intensity: float) -> Dict:
        """
        Get audio parameters for emotion/intensity combination.
        
        Returns:
            Dict with tempo_mult, volume_mult, bass_boost, etc.
        """
        base_params = self.emotion_map.get(emotion, self.emotion_map["calm"])
        
        # Scale parameters by intensity
        return {
            "tempo_mult": 1.0 + (base_params["tempo_mult"] - 1.0) * intensity,
            "volume_mult": 1.0 + (base_params["volume_mult"] - 1.0) * intensity,
            "bass_boost": base_params["bass_boost"] * intensity
        }


# ============================================================================
# VOICE GENERATOR
# ============================================================================

class VoiceGenerator:
    """
    TTS voice generation with emotional modulation.
    
    RESPONSIBILITIES:
        - Text-to-speech synthesis
        - Voice profile application
        - Emotional prosody injection
        - Per-segment voice modulation
        - Caching for efficiency
    
    LOC: ~1,200-1,800
    """
    
    def __init__(self, cache_dir: Optional[str] = None):
        self.logger = Vi7:37 PM
ralLogger()
self.cache_dir = cache_dir or SystemConfig.AUDIO_CACHE_DIR
Path(self.cache_dir).mkdir(parents=True, exist_ok=True)

    # Voice model (mocked for standalone)
    self.tts_available = TORCH_AVAILABLE and AUDIO_LIBS_AVAILABLE
    
    if not self.tts_available:
        self.logger.log_warning(
            "TTS libraries not available, using mock voice generation",
            module=MODULE_NAME,
            component="VoiceGenerator"
        )

def generate_narration(self,
                      segments: List[AudioSegment],
                      voice_profile: Dict,
                      audio_seed: int,
                      sample_rate: int = TARGET_SAMPLE_RATE) -> AudioTrack:
    """
    Generate complete narration track from segments.
    
    Args:
        segments: Script segments with timing
        voice_profile: Voice configuration
        audio_seed: Random seed for determinism
        sample_rate: Output sample rate
    
    Returns:
        AudioTrack with complete narration
    """
    # Set deterministic seed
    np.random.seed(audio_seed)
    random.seed(audio_seed)
    
    # Generate audio for each segment
    segment_audio = []
    
    for segment in segments:
        if segment.layer_type != AudioLayerType.NARRATION:
            continue
        
        # Generate or retrieve cached audio
        audio_data = self._generate_segment_audio(
            segment,
            voice_profile,
            sample_rate
        )
        
        segment_audio.append((segment, audio_data))
    
    # Concatenate all segments with proper timing
    full_audio = self._concatenate_segments(segment_audio, sample_rate)
    
    # Create track
    track = AudioTrack(
        layer_type=AudioLayerType.NARRATION,
        audio_data=full_audio,
        sample_rate=sample_rate,
        volume=1.0,
        metadata={
            "voice_profile": voice_profile,
            "segment_count": len(segment_audio),
            "generation_seed": audio_seed
        }
    )
    
    self.logger.log_info(
        f"Generated narration track: {track.duration:.2f}s, "
        f"{len(segment_audio)} segments",
        module=MODULE_NAME,
        component="VoiceGenerator"
    )
    
    return track

def _generate_segment_audio(self,
                            segment: AudioSegment,
                            voice_profile: Dict,
                            sample_rate: int) -> np.ndarray:
    """Generate audio for a single segment"""
    # Generate cache key
    cache_key = self._generate_cache_key(segment, voice_profile)
    cache_path = Path(self.cache_dir) / f"{cache_key}.wav"
    
    # Check cache
    if cache_path.exists():
        try:
            audio_data, _ = sf.read(str(cache_path))
            return audio_data
        except Exception as e:
            self.logger.log_warning(
                f"Cache read failed: {e}",
                module=MODULE_NAME,
                component="VoiceGenerator"
            )
    
    # Generate new audio
    if self.tts_available:
        audio_data = self._synthesize_tts(segment, voice_profile, sample_rate)
    else:
        # Mock audio generation
        audio_data = self._generate_mock_audio(segment, sample_rate)
    
    # Apply emotion modulation
    audio_data = self._apply_emotion_modulation(audio_data, segment, sample_rate)
    
    # Cache result
    try:
        sf.write(str(cache_path), audio_data, sample_rate)
    except Exception as e:
        self.logger.log_warning(
            f"Cache write failed: {e}",
            module=MODULE_NAME,
            component="VoiceGenerator"
        )
    
    return audio_data

def _synthesize_tts(self,
                    segment: AudioSegment,
                    voice_profile: Dict,
                    sample_rate: int) -> np.ndarray:
    """
    Real TTS synthesis (placeholder for production TTS model).
    Production would use: Coqui TTS, Tortoise TTS, or commercial APIs.
    """
    # This is a placeholder - real implementation would use actual TTS
    duration_samples = int(segment.duration * sample_rate)
    
    # Generate synthetic speech-like waveform
    t = np.linspace(0, segment.duration, duration_samples)
    
    # Base frequency modulated by text
    base_freq = 150 + voice_profile.get("pitch_shift", 0) * 10
    
    # Generate formants (simplified speech synthesis)
    audio = np.zeros(duration_samples)
    for formant_freq in [base_freq, base_freq * 2.5, base_freq * 4.5]:
        audio += 0.3 * np.sin(2 * np.pi * formant_freq * t)
    
    # Add noise for realism
    audio += np.random.normal(0, 0.1, duration_samples)
    
    # Apply envelope
    envelope = np.hanning(duration_samples)
    audio *= envelope
    
    # Normalize
    audio = audio / np.max(np.abs(audio)) * 0.8
    
    return audio.astype(np.float32)

def _generate_mock_audio(self, segment: AudioSegment, sample_rate: int) -> np.ndarray:
    """Generate mock audio for testing"""
    duration_samples = int(segment.duration * sample_rate)
    
    # Generate pink noise
    audio = np.random.normal(0, 0.3, duration_samples)
    
    # Apply fade in/out
    fade_samples = int(0.05 * sample_rate)
    fade_in = np.linspace(0, 1, fade_samples)
    fade_out = np.linspace(1, 0, fade_samples)
    
    audio[:fade_samples] *= fade_in
    audio[-fade_samples:] *= fade_out
    
    return audio.astype(np.float32)

def _apply_emotion_modulation(self,
                              audio: np.ndarray,
                              segment: AudioSegment,
                              sample_rate: int) -> np.ndarray:
    """Apply emotional modulation to audio"""
    if segment.emotion is None:
        return audio
    
    # Get emotion parameters
    emotion_map = EmotionMapper()
    params = emotion_map.get_audio_parameters(segment.emotion, segment.intensity)
    
    # Apply volume modulation
    audio = audio * params["volume_mult"]
    
    # Apply pitch shift (simplified)
    if abs(params["tempo_mult"] - 1.0) > 0.05:
        # Time stretching simulation
        target_length = int(len(audio) / params["tempo_mult"])
        indices = np.linspace(0, len(audio) - 1, target_length)
        audio = np.interp(indices, np.arange(len(audio)), audio)
    
    return audio

def _concatenate_segments(self,
                         segment_audio: List[Tuple[AudioSegment, np.ndarray]],
                         sample_rate: int) -> np.ndarray:
    """Concatenate segments with proper timing"""
    if not segment_audio:
        return np.array([], dtype=np.float32)
    
    # Calculate total duration
    total_duration = segment_audio[-1][0].end_time
    total_samples = int(total_duration * sample_rate)
    
    # Initialize output array
    output = np.zeros(total_samples, dtype=np.float32)
    
    # Place each segment at correct position
    for segment, audio in segment_audio:
        start_sample = int(segment.start_time * sample_rate)
        end_sample = start_sample + len(audio)
        
        # Ensure we don't overflow
        end_sample = min(end_sample, total_samples)
        audio_trimmed = audio[:end_sample - start_sample]
        
        output[start_sample:end_sample] = audio_trimmed
    
    return output

def _generate_cache_key(self, segment: AudioSegment, voice_profile: Dict) -> str:
    """Generate deterministic cache key"""
    content = f"{segment.text}_{voice_profile}_{segment.emotion}_{segment.intensity}"
    return hashlib.md5(content.encode()).hexdigest()
============================================================================
MUSIC LAYER COMPOSER
============================================================================
class MusicLayerComposer:
"""
Dynamic music scoring aligned to emotional arcs.

RESPONSIBILITIES:
    - Niche-specific music selection
    - Tempo/intensity synchronization
    - Dynamic music arrangement
    - Platform-specific mixing ratios
    - Music loop/fade management

LOC: ~1,000-1,500
"""

def __init__(self, music_library_path: Optional[str] = None):
    self.logger = ViralLogger()
    self.music_library_path = music_library_path or SystemConfig.MUSIC_LIBRARY_PATH
    
    # Music library index (would be loaded from disk in production)
    self.music_index = self._load_music_index()

def compose_music_track(self,
                       duration: float,
                       emotional_arc: Dict,
                       niche_profile: Dict,
                       platform_specs: Dict,
                       audio_seed: int,
                       sample_rate: int = TARGET_SAMPLE_RATE) -> AudioTrack:
    """
    Compose background music track.
    
    Args:
        duration: Target duration in seconds
        emotional_arc: Emotional trajectory
        niche_profile: Niche-specific music preferences
        platform_specs: Platform audio specs
        audio_seed: Random seed
        sample_rate: Output sample rate
    
    Returns:
        AudioTrack with composed music
    """
    np.random.seed(audio_seed)
    random.seed(audio_seed)
    
    # Select base music track
    music_track = self._select_music(niche_profile, emotional_arc, duration)
    
    # Load and process music
    music_audio = self._load_music_audio(music_track, sample_rate)
    
    # Adjust to target duration
    music_audio = self._adjust_music_duration(music_audio, duration, sample_rate)
    
    # Apply emotional modulation
    music_audio = self._modulate_music_emotion(
        music_audio,
        emotional_arc,
        duration,
        sample_rate
    )
    
    # Apply platform-specific mixing
    volume = platform_specs.get("music_volume_ratio", 0.3)
    
    track = AudioTrack(
        layer_type=AudioLayerType.MUSIC,
        audio_data=music_audio,
        sample_rate=sample_rate,
        volume=volume,
        metadata={
            "music_track": music_track,
            "niche": niche_profile.get("music_genre"),
            "seed": audio_seed
        }
    )
    
    self.logger.log_info(
        f"Composed music track: {track.duration:.2f}s, "
        f"genre: {niche_profile.get('music_genre')}",
        module=MODULE_NAME,
        component="MusicLayerComposer"
    )
    
    return track

def _load_music_index(self) -> Dict:
    """Load music library index"""
    # Mock music library (production would load from database)
    return {
        "electronic": [
            {"name": "energetic_synth_1", "duration": 120, "bpm": 128, "energy": "high"},
            {"name": "ambient_electronic_1", "duration": 180, "bpm": 90, "energy": "medium"}
        ],
        "cinematic": [
            {"name": "epic_orchestra_1", "duration": 150, "bpm": 100, "energy": "high"},
            {"name": "emotional_piano_1", "duration": 200, "bpm": 70, "energy": "low"}
        ],
        "pop": [
            {"name": "upbeat_pop_1", "duration": 180, "bpm": 120, "energy": "high"},
            {"name": "chill_pop_1", "duration": 200, "bpm": 95, "energy": "medium"}
        ]
    }

def _select_music(self,
                 niche_profile: Dict,
                 emotional_arc: Dict,
                 duration: float) -> Dict:
    """Select appropriate music track"""
    genre = niche_profile.get("music_genre", "electronic")
    energy_level = niche_profile.get("energy_level", "high")
    
    # Get tracks for genre
    genre_tracks = self.music_index.get(genre, self.music_index["electronic"])
    
    # Filter by energy level
    suitable_tracks = [
        t for t in genre_tracks
        if t["energy"] == energy_level or energy_level == "medium"
    ]
    
    if not suitable_tracks:
        suitable_tracks = genre_tracks
    
    # Select random track (deterministic due to seeded random)
    track = random.choice(suitable_tracks)
    
    return track

def _load_music_audio(self, music_track: Dict, sample_rate: int) -> np.ndarray:
    """Load music audio file"""
    # Mock music loading (production would load actual audio files)
    duration = music_track["duration"]
    bpm = music_track["bpm"]
    
    # Generate synthetic music-like waveform
    duration_samples = int(duration * sample_rate)
    t = np.linspace(0, duration, duration_samples)
    
    # Generate beat pattern
    beat_freq = bpm / 60.0
    audio = 0.5 * np.sin(2 * np.pi * beat_freq * t)
    
    # Add harmonics
    audio += 0.3 * np.sin(2 * np.pi * beat_freq * 2 * t)
    audio += 0.2 * np.sin(2 * np.pi * beat_freq * 4 * t)
    
    # Add bass
    audio += 0.4 * np.sin(2 * np.pi * (beat_freq / 2) * t)
    
    # Add some texture
    audio += np.random.normal(0, 0.05, duration_samples)
    
    # Normalize
    audio = audio / np.max(np.abs(audio)) * 0.7
    
    return audio.astype(np.float32)

def _adjust_music_duration(self,
                           audio: np.ndarray,
                           target_duration: float,
                           sample_rate: int) -> np.ndarray:
    """Adjust music to target duration (loop or trim)"""
    target_samples = int(target_duration * sample_rate)
    current_samples = len(audio)
    
    if target_samples <= current_samples:
        # Trim with fade out
        trimmed = audio[:target_samples]
        
        # Apply fade out
        fade_samples = int(0.5 * sample_rate)
        fade_out = np.linspace(1, 0, fade_samples)
        trimmed[-fade_samples:] *= fade_out
        
        return trimmed
    else:
        # Loop to extend
        loops_needed = int(np.ceil(target_samples / current_samples))
        looped = np.tile(audio, loops_needed)[:target_samples]
        
        return looped

def _modulate_music_emotion(self,
                            audio: np.ndarray,
                            emotional_arc: Dict,
                            duration: float,
                            sample_rate: int) -> np.ndarray:
    """Apply emotional modulation to music"""
    # Create volume envelope from emotional arc
    arc_segments = emotional_arc.get("segments", [])
    
    if not arc_segments:
        return audio
    
    # Build intensity curve
    intensity_curve = np.ones(len(audio))
    
    for segment in arc_segments:
        start_sample = int(segment.get("start", 0.0) * duration * sample_rate)
        end_sample = int(segment.get("end", 1.0) * duration * sample_rate)
        intensity = segment.get("intensity", 0.5)
        
        # Ensure within bounds
        start_sample = max(0, min(start_sample, len(audio)))
        end_sample = max(0, min(end_sample, len(audio)))
        
        if end_sample > start_sample:
            # Map intensity to volume (0.5-1.2 range)
            volume = 0.5 + (intensity * 0.7)
            intensity_curve[start_sample:end_sample] = volume
    
    # Apply curve with smoothing
    from scipy.ndimage import gaussian_filter1d
    intensity_curve = gaussian_filter1d(intensity_curve, sigma=sample_rate * 0.5)
    
    return audio * intensity_curve
============================================================================
SFX COMPOSER
============================================================================
class SFXComposer:
"""
Sound effects composition aligned to narrative events.

RESPONSIBILITIES:
    - Event-triggered SFX selection
    - Timing synchronization
    - Density control
    - Platform-appropriate SFX mixing

LOC: ~800-1,200
"""

def __init__(self, sfx_library_path: Optional[str] = None):
    self.logger = ViralLogger()
    self.sfx_library_path = sfx_library_path or SystemConfig.SFX_LIBRARY_PATH
    
    # SFX library (mock for standalone)
    self.sfx_library = self._load_sfx_library()

def compose_sfx_track(self,
                     duration: float,
                     segments: List[AudioSegment],
                     niche_profile: Dict,
                     audio_seed: int,
                     sample_rate: int = TARGET_SAMPLE_RATE) -> AudioTrack:
    """
    Compose sound effects track.
    
    Args:
        duration: Target duration
        segments: Script segments for event detection
        niche_profile: Niche SFX preferences
        audio_seed: Random seed
        sample_rate: Output sample rate
    
    Returns:
        AudioTrack with composed SFX
    """
    np.random.seed(audio_seed)
    random.seed(audio_seed)
    
    # Determine SFX density
    sfx_density = niche_profile.get("sfx_density", "medium")
    sfx_events = self._generate_sfx_events(segments, sfx_density)
    
    # Create empty track
    sfx_audio = np.zeros(int(duration * sample_rate), dtype=np.float32)
    
    # Add each SFX
    for event in sfx_events:
        sfx_data = self._load_sfx(event["type"], sample_rate)
        start_sample = int(event["timestamp"] * sample_rate)
        end_sample = start_sample + len(sfx_data)
        
        # Ensure within bounds
        if end_sample > len(sfx_audio):
            sfx_data = sfx_data[:len(sfx_audio) - start_sample]
            end_sample = len(sfx_audio)
        
        # Mix in SFX
        sfx_audio[start_sample:end_sample] += sfx_data * event["volume"]
    
    track = AudioTrack(
        layer_type=AudioLayerType.SFX,
        audio_data=sfx_audio,
        sample_rate=sample_rate,
        volume=0.5,  # SFX typically mixed lower
        metadata={
            "event_count": len(sfx_events),
            "density": sfx_density,
            "seed": audio_seed
        }
    )
    
    self.logger.log_info(
        f"Composed SFX track: {len(sfx_events)} events, density: {sfx_density}",
        module=MODULE_NAME,
        component="SFXComposer"
    )
    
    return track

def _load_sfx_library(self) -> Dict:
    """Load SFX library index"""
    return {
        "whoosh": {"duration": 0.5, "category": "transition"},
        "impact": {"duration": 0.3, "category": "emphasis"},
        "chime": {"duration": 0.8, "category": "notification"},
        "swoosh": {"duration": 0.4, "category": "transition"},
        "pop": {"duration": 0.2, "category": "emphasis"}
    }

def _generate_sfx_events(self,
                        segments: List[AudioSegment],
                        density: str) -> List[Dict]:
    """Generate SFX event list"""
    events = []
    
    # Density mapping
    density_map = {
        "low": 0.2,
        "medium": 0.4,
        "high": 0.6
    }
    
    probability = density_map.get(density, 0.4)
    
    # Add SFX at segment boundaries
    for i, segment in enumerate(segments):
        # Transition SFX between segments
        if i > 0 and random.random() < probability:
            events.append({
                "timestamp": segment.start_time,
                "type": random.choice(["whoosh", "swoosh"]),
                "volume": 0.6
            })
        
        # Emphasis SFX for high-intensity moments
        if segment.intensity and segment.intensity > 0.7:
            if random.random() < probability * 1.5:
                # Add SFX near middle of segment
                mid_time = (segment.start_time + segment.end_time) / 2.0
                events.append({
                    "timestamp": mid_time,
                    "type": random.choice(["impact", "pop"]),
                    "volume": 0.8
                })
    
    return events

def _load_sfx(self, sfx_type: str, sample_rate: int) -> np.ndarray:
    """Load SFX audio"""
    sfx_info = self.sfx_library.get(sfx_type, self.sfx_library["pop"])
    duration = sfx_info["duration"]
    
    # Generate synthetic SFX
    duration_samples = int(duration * sample_rate)
    t = np.linspace(0, duration, duration_samples)
    
    if sfx_type in ["whoosh", "swoosh"]:
        # Frequency sweep
        freq_start = 1000
        freq_end = 200
        freq = np.linspace(freq_start, freq_end, duration_samples)
        audio = np.sin(2 * np.pi * freq * t / sample_rate)
        
        # Apply envelope
        envelope = np.exp(-3 * t / duration)
        audio *= envelope
    
    elif sfx_type in ["impact", "pop"]:
        # Sharp transient
        audio = np.random.normal(0, 1, duration_samples)
        envelope = np.exp(-10 * t / duration)
        audio *= envelope
    
    else:
        # Generic chime
        audio = np.sin(2 * np.pi * 800 * t)
        envelope = np.exp(-2 * t / duration)
        audio *= envelope
    
    # Normalize
    audio = audio / np.max(np.abs(audio)) * 0.8
    
    return audio.astype(np.float32)
============================================================================
MIXER
============================================================================
class Mixer:
"""
Multi-track audio mixing with EQ and dynamics.

RESPONSIBILITIES:
    - Multi-track combination
    - Volume balancing
    - EQ adjustment
    - Stereo field management
    - Cross-track ducking

LOC: ~600-900
"""

def __init__(self):
    self.logger = ViralLogger()

def mix_tracks(self,
               tracks: List[AudioTrack],
               platform_specs: Dict,
               retention_targets: Dict) -> Tuple[np.ndarray, Dict]:
    """
    Mix multiple audio tracks into final output.
    
    Args:
        tracks: List of audio tracks to mix
        platform_specs: Platform audio specifications
        retention_targets: Retention optimization targets
    
    Returns:
        Tuple of (mixed_audio, mix_metadata)
    """
    if not tracks:
        raise ValueError("No tracks provided for mixing")
    
    # Ensure all tracks have same sample rate
    sample_rate = tracks[0].sample_rate
    for track in tracks:
        if track.sample_rate != sample_rate:
            raise ValueError(f"Sample rate mismatch: {track.sample_rate} vs {sample_rate}")
    
    # Find maximum duration
    max_duration = max(track.duration for track in tracks)
    max_samples = int(max_duration * sample_rate)
    
    # Initialize stereo mix
    mixed = np.zeros((max_samples, 2), dtype=np.float32)
    
    # Mix each track
    for track in tracks:
        track_audio = track.audio_data
        
        # Convert mono to stereo if needed
        if track_audio.ndim == 1:
            track_audio = np.stack([track_audio, track_audio], axis=1)
        
        # Pad to match duration
        if len(track_audio) < max_samples:
            pad_length = max_samples - len(track_audio)
            track_audio = np.pad(track_audio, ((0, pad_length), (0, 0)), mode='constant')
        
        # Apply volume
        track_audio *= track.volume
        
        # Mix in
        mixed[:len(track_audio)] += track_audio
    
    # Apply retention hook boost (first 5 seconds)
    mixed = self._apply_hook_boost(mixed, retention_targets, sample_rate)
    
    # Apply platform-specific processing
    mixed = self._apply_platform_eq(mixed, platform_specs, sample_rate)
    
    # Calculate mix metadata
    metadata = self._calculate_mix_metadata(mixed, tracks, sample_rate)
    
    self.logger.log_info(
        f"Mixed {len(tracks)} tracks: {max_duration:.2f}s, "
        f"peak: {metadata['peak_db']:.1f}dB",
        module=MODULE_NAME,
        component="Mixer"
    )
    
    return mixed, metadata

def _apply_hook_boost(self,
                     audio: np.ndarray,
                     retention_targets: Dict,
                     sample_rate: int) -> np.ndarray:
    """Boost first few seconds for retention"""
    hook_duration = retention_targets.get("hook_duration_sec", HOOK_DURATION_SEC)
    hook_samples = int(hook_duration * sample_rate)
    
    if hook_samples > len(audio):
        hook_samples = len(audio)
    
    # Create boost envelope
    boost = np.ones(len(audio))
    boost[:hook_samples] = HOOK_VOLUME_BOOST
    
    # Smooth transition
    transition_samples = int(0.5 * sample_rate)
    if hook_samples + transition_samples < len(audio):
        transition = np.linspace(HOOK_VOLUME_BOOST, 1.0, transition_samples)
        boost[hook_samples:hook_samples + transition_samples] = transition
    
    # Apply boost
    audio *= boost[:, np.newaxis]
    
    return audio

def _apply_platform_eq(self,
                      audio: np.ndarray,
                      platform_specs: Dict,
                      sample_rate: int) -> np.ndarray:
    """Apply platform-specific EQ"""
    # Simple bass boost for high-energy platforms
    if platform_specs.get("prefer_high_energy", False):
        # Apply simple low-pass emphasis (mock EQ)
        # Production would use proper filter design
        from scipy.signal import butter, filtfilt
        
        # Bass boost filter
        nyquist = sample_rate / 2
        low_freq = 100 / nyquist
        b, a = butter(2, low_freq, btype='low')
        
        bass = filtfilt(b, a, audio, axis=0)
        audio += bass * 0.2  # Add 20% bass
    
    return audio

def _calculate_mix_metadata(self,
                            audio: np.ndarray,
                            tracks: List[AudioTrack],
                            sample_rate: int) -> Dict:
    """Calculate mix quality metrics"""
    # Peak level
    peak = np.max(np.abs(audio))
    peak_db = 20 * np.log10(peak) if peak > 0 else -np.inf
    
    # RMS level
    rms = np.sqrt(np.mean(audio ** 2))
    rms_db = 20 * np.log10(rms) if rms > 0 else -np.inf
    
    # Stereo correlation
    if audio.shape[1] == 2:
        left = audio[:, 0]
        right = audio[:, 1]
        correlation = np.corrcoef(left, right)[0, 1]
    else:
        correlation = 1.0
    
    return {
        "peak_db": float(peak_db),
        "rms_db": float(rms_db),
        "stereo_correlation": float(correlation),
        "track_count": len(tracks),
        "sample_rate": sample_rate
    }
============================================================================
NORMALIZER
============================================================================
class Normalizer:
"""
Audio normalization and quality assurance.

RESPONSIBILITIES:
    - Loudness normalization (LUFS)
    - Dynamic range control
    - Clipping prevention
    - Stereo field correction
    - Quality threshold enforcement

LOC: ~600-900
"""

def __init__(self):
    self.logger = ViralLogger()
    self.quality_thresholds = QUALITY_THRESHOLDS

def normalize(self,
              audio: np.ndarray,
              platform_specs: Dict,
              sample_rate: int) -> Tuple[np.ndarray, Dict]:
    """
    Normalize audio to platform specifications.
    
    Args:
        audio: Input audio (stereo)
        platform_specs: Target platform specs
        sample_rate: Sample rate
    
    Returns:
        Tuple of (normalized_audio, normalization_metadata)
    """
    metadata = {}
    
    # Initial quality check
    quality_issues = self._check_quality(audio, sample_rate)
    if quality_issues:
        self.logger.log_warning(
            f"Quality issues detected: {quality_issues}",
            module=MODULE_NAME,
            component="Normalizer"
        )
        metadata["quality_warnings"] = quality_issues
    
    # Peak normalization (prevent clipping)
    audio, peak_reduction = self._normalize_peak(audio)
    metadata["peak_reduction_db"] = peak_reduction
    
    # Loudness normalization (LUFS target)
    target_lufs = platform_specs.get("loudness_lufs", -14.0)
    audio, lufs_adjustment = self._normalize_loudness(audio, target_lufs, sample_rate)
    metadata["lufs_adjustment_db"] = lufs_adjustment
    
    # Dynamic range control (compression)
    audio = self._apply_compression(audio, sample_rate)
    metadata["compression_applied"] = True
    
    # Final limiting (brick wall)
    audio = self._apply_limiter(audio)
    metadata["limiter_applied"] = True
    
    # Final quality check
    final_quality = self._check_quality(audio, sample_rate)
    metadata["final_quality_issues"] =