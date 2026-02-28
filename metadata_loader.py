"""
metadata_loader.py — ARTIFACT LOADING LAYER (BLUEPRINT COMPLIANT)

PURPOSE:
Handle ALL file system operations and artifact loading.
This file does NOT parse or validate metadata - only loads raw artifacts.

SCALE TARGET: 10k-50k items/day
LATENCY TARGET: <50ms local, <200ms distributed
"""

import json
import hashlib
from pathlib import Path
from typing import Dict, Optional, Any, Tuple
import logging

logger = logging.getLogger(__name__)

class MetadataLoader:
    """
    Artifact loading layer for metadata processing.
    
    Responsibilities:
    1. Locate required files
    2. Load JSON artifacts
    3. Compute checksums
    4. Validate file existence
    5. Return raw dictionaries
    
    This file NEVER parses or validates metadata - only loads artifacts.
    """
    
    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        
        # Input directories (raw ingestion artifacts)
        self.raw_video_dir = self.data_root / "raw" / "video"
        self.raw_audio_dir = self.data_root / "raw" / "audio"
        self.raw_scrape_dir = self.data_root / "raw" / "scrape"
    
    def load(self, platform: str, video_id: str) -> tuple[dict, Optional[dict], dict]:
        """
        Load all required artifacts for a content item.
        
        Args:
            platform: Platform identifier
            video_id: Unique video identifier
            
        Returns:
            Tuple of (video_manifest, audio_manifest_or_None, scrape_metadata)
            
        Raises:
            FileNotFoundError: If required artifacts are missing
        """
        # Load required artifacts
        video_manifest = self._load_video_manifest(platform, video_id)
        scrape_metadata = self._load_scrape_metadata(platform, video_id)
        
        # Audio is optional for some platforms
        audio_manifest = None
        if platform not in ['reddit']:
            try:
                audio_manifest = self._load_audio_manifest(platform, video_id)
            except FileNotFoundError:
                # Audio is optional - log and continue
                logger.warning(
                    "Audio manifest missing (optional)",
                    extra={
                        "platform": platform,
                        "video_id": video_id
                    }
                )
        
        return video_manifest, audio_manifest, scrape_metadata
    
    def compute_checksums(self, platform: str, video_id: str) -> Dict[str, Optional[str]]:
        """
        Compute checksums for all source artifacts.
        
        Args:
            platform: Platform identifier
            video_id: Unique video identifier
            
        Returns:
            Dictionary of checksums by artifact type
        """
        checksums = {}
        
        try:
            checksums['video'] = self._compute_file_checksum(
                self.raw_video_dir / platform / video_id / "video_manifest.json"
            )
        except FileNotFoundError:
            checksums['video'] = None
        
        try:
            checksums['scrape'] = self._compute_file_checksum(
                self.raw_scrape_dir / platform / video_id / "scrape_metadata.json"
            )
        except FileNotFoundError:
            checksums['scrape'] = None
        
        # Audio is optional
        try:
            checksums['audio'] = self._compute_file_checksum(
                self.raw_audio_dir / platform / video_id / "audio_manifest.json"
            )
        except FileNotFoundError:
            checksums['audio'] = None
        
        return checksums
    
    def _load_video_manifest(self, platform: str, video_id: str) -> dict:
        """Load video manifest from file"""
        path = self.raw_video_dir / platform / video_id / "video_manifest.json"
        if not path.exists():
            raise FileNotFoundError(f"Video manifest missing: {path}")
        return self._load_json(path)
    
    def _load_audio_manifest(self, platform: str, video_id: str) -> dict:
        """Load audio manifest from file"""
        path = self.raw_audio_dir / platform / video_id / "audio_manifest.json"
        if not path.exists():
            raise FileNotFoundError(f"Audio manifest missing: {path}")
        return self._load_json(path)
    
    def _load_scrape_metadata(self, platform: str, video_id: str) -> dict:
        """Load scrape metadata from file"""
        path = self.raw_scrape_dir / platform / video_id / "scrape_metadata.json"
        if not path.exists():
            raise FileNotFoundError(f"Scrape metadata missing: {path}")
        return self._load_json(path)
    
    def _load_json(self, path: Path) -> dict:
        """Load JSON file with error handling"""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {path} — {e}")
        except Exception as e:
            raise RuntimeError(f"Failed to load {path}: {e}")
    
    def _compute_file_checksum(self, path: Path) -> str:
        """Compute SHA256 checksum of file"""
        if not path.exists():
            raise FileNotFoundError(f"File does not exist: {path}")
        
        try:
            with open(path, 'rb') as f:
                content = f.read()
                return hashlib.sha256(content).hexdigest()
        except Exception as e:
            raise RuntimeError(f"Failed to compute checksum for {path}: {e}")
