"""
feature_registry.py

Feature Registry for Sentiment Analysis System
Provides centralized feature definitions and registration
"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Set, Any, Tuple
import numpy as np
from numpy.typing import NDArray

class FeatureModality(Enum):
    """Feature modality types"""
    TEXT = "text"
    AUDIO = "audio"
    VISUAL = "visual"
    CROSS_MODAL = "cross_modal"

class FeatureStability(Enum):
    """Feature stability levels"""
    STABLE = "stable"
    VOLATILE = "volatile"
    EXPERIMENTAL = "experimental"

class ConsumerType(Enum):
    """Feature consumer types"""
    SENTIMENT = "sentiment"
    VIRALITY = "virality"
    ENGAGEMENT = "engagement"
    QUALITY = "quality"

@dataclass(frozen=True)
class FeatureDefinition:
    """Feature definition with metadata"""
    name: str
    modality: FeatureModality
    stability: FeatureStability
    consumer_types: Set[ConsumerType]
    description: str
    data_type: str
    shape: Optional[Tuple[int, ...]] = None
    range: Optional[Tuple[float, float]] = None

class FeatureRegistry:
    """Central registry for all features"""
    
    def __init__(self):
        self._features: Dict[str, FeatureDefinition] = {}
        self._register_core_features()
    
    def _register_core_features(self):
        """Register core sentiment analysis features"""
        
        # Text Features
        self.register(FeatureDefinition(
            name="sentiment_score",
            modality=FeatureModality.TEXT,
            stability=FeatureStability.STABLE,
            consumer_types={ConsumerType.SENTIMENT},
            description="Overall sentiment polarity score",
            data_type="float",
            range=(-1.0, 1.0)
        ))
        
        self.register(FeatureDefinition(
            name="sentiment_confidence",
            modality=FeatureModality.TEXT,
            stability=FeatureStability.STABLE,
            consumer_types={ConsumerType.SENTIMENT},
            description="Confidence in sentiment prediction",
            data_type="float",
            range=(0.0, 1.0)
        ))
        
        self.register(FeatureDefinition(
            name="word_count",
            modality=FeatureModality.TEXT,
            stability=FeatureStability.STABLE,
            consumer_types={ConsumerType.SENTIMENT, ConsumerType.ENGAGEMENT},
            description="Number of words in text",
            data_type="int",
            range=(0, None)
        ))
        
        self.register(FeatureDefinition(
            name="text_length",
            modality=FeatureModality.TEXT,
            stability=FeatureStability.STABLE,
            consumer_types={ConsumerType.SENTIMENT},
            description="Length of text in characters",
            data_type="int",
            range=(0, None)
        ))
        
        # Audio Features
        self.register(FeatureDefinition(
            name="spectral_centroid",
            modality=FeatureModality.AUDIO,
            stability=FeatureStability.STABLE,
            consumer_types={ConsumerType.SENTIMENT},
            description="Spectral centroid of audio",
            data_type="float",
            range=(0.0, 8000.0)
        ))
        
        self.register(FeatureDefinition(
            name="mfcc_features",
            modality=FeatureModality.AUDIO,
            stability=FeatureStability.STABLE,
            consumer_types={ConsumerType.SENTIMENT},
            description="MFCC coefficients",
            data_type="array",
            shape=(13, None)
        ))
        
        self.register(FeatureDefinition(
            name="rms_energy",
            modality=FeatureModality.AUDIO,
            stability=FeatureStability.STABLE,
            consumer_types={ConsumerType.SENTIMENT, ConsumerType.ENGAGEMENT},
            description="RMS energy of audio signal",
            data_type="float",
            range=(0.0, 1.0)
        ))
        
        self.register(FeatureDefinition(
            name="zero_crossing_rate",
            modality=FeatureModality.AUDIO,
            stability=FeatureStability.STABLE,
            consumer_types={ConsumerType.SENTIMENT},
            description="Zero crossing rate",
            data_type="float",
            range=(0.0, 1.0)
        ))
        
        # Visual Features
        self.register(FeatureDefinition(
            name="brightness_histogram",
            modality=FeatureModality.VISUAL,
            stability=FeatureStability.STABLE,
            consumer_types={ConsumerType.SENTIMENT},
            description="Brightness distribution",
            data_type="array",
            shape=(256,)
        ))
        
        self.register(FeatureDefinition(
            name="color_histogram",
            modality=FeatureModality.VISUAL,
            stability=FeatureStability.STABLE,
            consumer_types={ConsumerType.SENTIMENT},
            description="Color distribution",
            data_type="array",
            shape=(3, 256)
        ))
        
        self.register(FeatureDefinition(
            name="edge_density",
            modality=FeatureModality.VISUAL,
            stability=FeatureStability.STABLE,
            consumer_types={ConsumerType.SENTIMENT, ConsumerType.QUALITY},
            description="Edge density in image",
            data_type="float",
            range=(0.0, 1.0)
        ))
        
        self.register(FeatureDefinition(
            name="contrast_ratio_sequence",
            modality=FeatureModality.VISUAL,
            stability=FeatureStability.VOLATILE,
            consumer_types={ConsumerType.SENTIMENT},
            description="Sequence of contrast ratios",
            data_type="array",
            shape=(None,)
        ))
        
        self.register(FeatureDefinition(
            name="luminance_variance",
            modality=FeatureModality.VISUAL,
            stability=FeatureStability.STABLE,
            consumer_types={ConsumerType.SENTIMENT},
            description="Variance in luminance",
            data_type="float",
            range=(0.0, 1.0)
        ))
        
        # Cross-Modal Features
        self.register(FeatureDefinition(
            name="audiovisual_sync",
            modality=FeatureModality.CROSS_MODAL,
            stability=FeatureStability.EXPERIMENTAL,
            consumer_types={ConsumerType.SENTIMENT},
            description="Audio-visual synchronization",
            data_type="float",
            range=(0.0, 1.0)
        ))
    
    def register(self, feature: FeatureDefinition) -> None:
        """Register a new feature"""
        self._features[feature.name] = feature
    
    def get(self, name: str) -> Optional[FeatureDefinition]:
        """Get feature definition by name"""
        return self._features.get(name)
    
    def list_by_modality(self, modality: FeatureModality) -> List[FeatureDefinition]:
        """List all features of a specific modality"""
        return [f for f in self._features.values() if f.modality == modality]
    
    def list_by_consumer(self, consumer: ConsumerType) -> List[FeatureDefinition]:
        """List all features for a specific consumer"""
        return [f for f in self._features.values() if consumer in f.consumer_types]
    
    def validate_feature_data(self, name: str, data: Any) -> Tuple[bool, Optional[str]]:
        """Validate feature data against definition"""
        feature = self.get(name)
        if not feature:
            return False, f"Unknown feature: {name}"
        
        # Type validation
        if feature.data_type == "float":
            if not isinstance(data, (float, np.floating)):
                return False, f"Expected float, got {type(data)}"
        elif feature.data_type == "int":
            if not isinstance(data, (int, np.integer)):
                return False, f"Expected int, got {type(data)}"
        elif feature.data_type == "array":
            if not isinstance(data, (np.ndarray, list)):
                return False, f"Expected array, got {type(data)}"
        
        # Range validation
        if feature.range and feature.range[0] is not None and feature.range[1] is not None:
            if isinstance(data, (int, float, np.number)):
                if not (feature.range[0] <= float(data) <= feature.range[1]):
                    return False, f"Value {data} outside range {feature.range}"
        
        return True, None
    
    def get_all_features(self) -> Dict[str, FeatureDefinition]:
        """Get all registered features"""
        return self._features.copy()

# Global registry instance
feature_registry = FeatureRegistry()
