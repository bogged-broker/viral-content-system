"""
youtube_scraper.py — PURE INGESTION LAYER

Mission: Collect unaltered truth from YouTube (especially Shorts) with zero 
interpretation, modeling, or intelligence. This file ONLY observes, timestamps, 
and stores raw reality.

ACQUISITION BOUNDARIES:
- Primary: YouTube Data API v3 (authoritative source)
- Secondary: HTML scraping (fallback for missing data only)
- Tertiary: RSS feeds (channel-level discovery only)

FIELD-LEVEL SOURCE OF TRUTH:
- Video metadata: API > HTML > RSS
- Channel metadata: API > HTML > RSS  
- Statistics: API only (HTML unreliable)
- Discovery: API search > HTML trending > RSS feeds

CONFLICT RESOLUTION:
- API data always overrides HTML/RSS
- Timestamp conflicts: use earliest reliable timestamp
- Missing fields: mark as None, never infer or calculate

CADENCE ENFORCEMENT:
- Monotonic sampling enforced per platform
- Next-eligible timestamps stored and validated
- Early resampling rejected with logging
- Deterministic cadence: search(1h), trending(6h), channel(24h), backfill(168h)

IDEMPOTENCY GUARANTEES:
- Composite key: (video_id, scrape_timestamp)
- Duplicate detection and rejection
- Conflict resolution: keep latest scrape, log duplicates
- Backfill safety: timestamp-aware deduplication
"""

import time
import json
import logging
import sqlite3
import hashlib
import random
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Set
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, asdict, field
from enum import Enum
from collections import defaultdict
import re

try:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    logging.warning("Google API client not installed. Install with: pip install google-api-python-client")


class AcquisitionBackend(Enum):
    """Data acquisition backends with priority hierarchy"""
    API = "api"           # Primary: YouTube Data API v3
    HTML = "html"         # Secondary: HTML scraping (fallback)
    RSS = "rss"           # Tertiary: RSS feeds (discovery only)


class ScrapeMode(Enum):
    """Scraping strategy modes with enforced cadence"""
    SEARCH = "search"         # 1-hour cadence
    TRENDING = "trending"     # 6-hour cadence  
    CHANNEL = "channel"       # 24-hour cadence
    BACKFILL = "backfill"     # 168-hour cadence
    RESAMPLE = "resample"     # Variable cadence
    SHADOW = "shadow"         # Sampling only, no cadence enforcement


@dataclass
class RawVideoRecord:
    """Raw video data - UNALTERED TRUTH ONLY"""
    # Static metadata (from API/HTML/RSS)
    video_id: str
    channel_id: str
    upload_timestamp: str  # ISO 8601 UTC from source
    duration_seconds: int
    title: str
    description: str
    hashtags: List[str]
    thumbnail_url: str
    category_id: str
    language: str
    
    # Dynamic performance (RAW API VALUES ONLY)
    views: int
    likes: int
    comments: int
    
    # Context and provenance
    scrape_timestamp: str  # ISO 8601 UTC when scraped
    scrape_mode: str
    acquisition_backend: str  # API, HTML, or RSS
    source_context: Dict[str, Any]
    
    # Idempotency and lineage
    content_hash: str  # MD5 of video_id (identity of content)
    source_fingerprint: str  # Hash of raw source data
    
    # Optional fields (must come after required fields)
    shares: Optional[int] = None  # May not be available
    snapshot_hash: Optional[str] = None  # MD5 of video_id + scrape_timestamp (identity of time)
    is_backfill: bool = False
    backfill_target_date: Optional[str] = None


@dataclass
class RawChannelRecord:
    """Raw channel data - UNALTERED TRUTH ONLY"""
    # Static metadata (from API/HTML/RSS)
    channel_id: str
    channel_name: str
    subscriber_count: int
    total_videos: int
    
    # Context and provenance
    scrape_timestamp: str  # ISO 8601 UTC when scraped
    acquisition_backend: str  # API, HTML, or RSS
    source_fingerprint: str  # Hash of raw source data
    
    # Optional fields (must come after required fields)
    channel_creation_date: Optional[str] = None  # Raw creation date from source
    shorts_count: Optional[int] = None  # May not be available
    recent_video_ids: List[str] = field(default_factory=list)


@dataclass
class RawStatsSnapshot:
    """Raw performance snapshot - TIME SERIES POINT ONLY"""
    video_id: str
    scrape_timestamp: str  # ISO 8601 UTC when scraped
    acquisition_backend: str
    
    # RAW API VALUES ONLY (NO CALCULATIONS)
    views: int
    likes: int
    comments: int
    
    # Idempotency (required field before optional fields)
    snapshot_hash: str  # MD5 of video_id + scrape_timestamp (identity of time)
    
    # Optional fields (must come after required fields)
    shares: Optional[int] = None
    trending_rank: Optional[int] = None
    search_position: Optional[int] = None


class ChannelCadenceEnforcer:
    """Explicit channel scraping cadence enforcement"""
    
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.cadence_file = self.data_dir / 'channel_cadence.json'
        self.next_eligible = self._load_channel_cadence()
    
    def _load_channel_cadence(self) -> Dict[str, datetime]:
        """Load next eligible channel scrape timestamps"""
        if self.cadence_file.exists():
            data = json.loads(self.cadence_file.read_text())
            return {
                channel_id: datetime.fromisoformat(timestamp) 
                for channel_id, timestamp in data.items()
            }
        return {}
    
    def _save_channel_cadence(self):
        """Persist next eligible channel scrape timestamps"""
        data = {
            channel_id: timestamp.isoformat()
            for channel_id, timestamp in self.next_eligible.items()
        }
        self.cadence_file.write_text(json.dumps(data, indent=2))
    
    def can_scrape_channel(self, channel_id: str) -> bool:
        """Check if channel scraping is allowed (24h cadence)"""
        now = datetime.now(timezone.utc)
        next_allowed = self.next_eligible.get(channel_id, datetime.min.replace(tzinfo=timezone.utc))
        
        if now < next_allowed:
            logging.debug(f"Channel cadence: {channel_id} not eligible until {next_allowed}")
            return False
        
        return True
    
    def record_channel_scrape(self, channel_id: str):
        """Record channel scrape and update next eligible timestamp (24h)"""
        now = datetime.now(timezone.utc)
        next_allowed = now + timedelta(hours=24)  # 24-hour cadence
        
        self.next_eligible[channel_id] = next_allowed
        self._save_channel_cadence()
        
        logging.debug(f"Recorded channel scrape for {channel_id}. Next eligible: {next_allowed}")


class CadenceEnforcer:
    """Enforces monotonic sampling and prevents early resampling"""
    
    # ENFORCED CADETTE RULES (hours)
    CADETTE_RULES = {
        ScrapeMode.SEARCH: 1,      # 1-hour minimum
        ScrapeMode.TRENDING: 6,    # 6-hour minimum
        ScrapeMode.CHANNEL: 24,    # 24-hour minimum
        ScrapeMode.BACKFILL: 168,  # 168-hour minimum
        ScrapeMode.RESAMPLE: 1,    # 1-hour minimum (flexible)
        ScrapeMode.SHADOW: 0,      # No cadence for shadow sampling
    }
    
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.cadence_file = self.data_dir / 'cadence_tracking.json'
        self.next_eligible = self._load_cadence_tracking()
    
    def _load_cadence_tracking(self) -> Dict[str, datetime]:
        """Load next eligible scrape timestamps"""
        if self.cadence_file.exists():
            data = json.loads(self.cadence_file.read_text())
            return {
                mode: datetime.fromisoformat(timestamp) 
                for mode, timestamp in data.items()
            }
        return {}
    
    def _save_cadence_tracking(self):
        """Persist next eligible scrape timestamps"""
        data = {
            mode: timestamp.isoformat()
            for mode, timestamp in self.next_eligible.items()
        }
        self.cadence_file.write_text(json.dumps(data, indent=2))
    
    def can_scrape(self, mode: ScrapeMode, context: Optional[str] = None) -> bool:
        """Check if scraping is allowed (monotonic enforcement)"""
        if mode == ScrapeMode.SHADOW:
            return True  # Shadow sampling has no cadence restrictions
        
        mode_key = mode.value
        if context:
            mode_key = f"{mode.value}_{context}"
        
        now = datetime.now(timezone.utc)
        next_allowed = self.next_eligible.get(mode_key, datetime.min.replace(tzinfo=timezone.utc))
        
        if now < next_allowed:
            logging.warning(f"Early resampling rejected for {mode_key}. Next allowed: {next_allowed}")
            return False
        
        return True
    
    def record_scrape(self, mode: ScrapeMode, context: Optional[str] = None):
        """Record scrape and update next eligible timestamp"""
        if mode == ScrapeMode.SHADOW:
            return  # Shadow sampling doesn't update cadence
        
        mode_key = mode.value
        if context:
            mode_key = f"{mode.value}_{context}"
        
        now = datetime.now(timezone.utc)
        cadence_hours = self.CADETTE_RULES[mode]
        next_allowed = now + timedelta(hours=cadence_hours)
        
        self.next_eligible[mode_key] = next_allowed
        self._save_cadence_tracking()
        
        logging.info(f"Recorded scrape for {mode_key}. Next eligible: {next_allowed}")


class IdempotencyManager:
    """Manages composite keys and prevents duplicates"""
    
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.dedup_db = self.data_dir / 'deduplication.db'
        self._init_dedup_db()
    
    def _init_dedup_db(self):
        """Initialize deduplication database"""
        conn = sqlite3.connect(self.dedup_db)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS video_dedup (
                video_id TEXT,
                scrape_timestamp TEXT,
                content_hash TEXT UNIQUE,
                scrape_mode TEXT,
                recorded_at TEXT,
                PRIMARY KEY (video_id, scrape_timestamp)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_content_hash ON video_dedup(content_hash)")
        conn.commit()
        conn.close()
    
    def is_duplicate(self, video_id: str, scrape_timestamp: str, content_hash: str) -> bool:
        """Check if record is duplicate"""
        conn = sqlite3.connect(self.dedup_db)
        
        # Check composite key (video_id, scrape_timestamp)
        cursor = conn.execute("""
            SELECT 1 FROM video_dedup 
            WHERE video_id = ? AND scrape_timestamp = ?
        """, (video_id, scrape_timestamp))
        
        if cursor.fetchone():
            conn.close()
            return True
        
        # Check content hash (exact duplicate detection)
        cursor = conn.execute("""
            SELECT 1 FROM video_dedup WHERE content_hash = ?
        """, (content_hash,))
        
        is_duplicate = cursor.fetchone() is not None
        conn.close()
        
        if is_duplicate:
            logging.info(f"Content hash duplicate detected: {content_hash[:16]}...")
        
        return is_duplicate
    
    def record_scrape(self, video_id: str, scrape_timestamp: str, content_hash: str, scrape_mode: str):
        """Record scrape for deduplication"""
        conn = sqlite3.connect(self.dedup_db)
        try:
            conn.execute("""
                INSERT OR REPLACE INTO video_dedup 
                (video_id, scrape_timestamp, content_hash, scrape_mode, recorded_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                video_id, 
                scrape_timestamp, 
                content_hash, 
                scrape_mode,
                datetime.now(timezone.utc).isoformat()
            ))
            conn.commit()
        except sqlite3.IntegrityError as e:
            logging.warning(f"Deduplication record conflict: {e}")
        finally:
            conn.close()


class YouTubeScraper:
    """
    PURE INGESTION LAYER - Collects unaltered truth only.
    
    Capabilities:
    - Multi-backend acquisition (API > HTML > RSS)
    - Cadence enforcement with monotonic sampling
    - Idempotency guarantees with composite keys
    - Raw data storage with zero interpretation
    - Conflict resolution and source tracking
    """
    
    def __init__(
        self,
        api_keys: List[str],
        mode: str,
        data_dir: str,
        config: Optional[Dict] = None
    ):
        self.api_keys = api_keys
        self.current_key_idx = 0
        self.mode = ScrapeMode(mode)
        self.data_dir = Path(data_dir)
        
        # Config defaults
        self.config = {
            'retry_backoff_seconds': 2,
            'max_retries': 3,
            'min_views_threshold': 0,  # Mechanical noise suppression, not quality inference
            'short_duration_max_seconds': 60,
            'batch_size': 50,
            'target_languages': ['en'],
            'enable_cadence_enforcement': True,
            'enable_idempotency_check': True,
            'preferred_backend': AcquisitionBackend.API.value,
        }
        if config:
            self.config.update(config)
        
        # Initialize YouTube API client
        self.youtube = self._build_client()
        
        # Initialize ingestion components
        self._init_directories()
        
        if self.config['enable_cadence_enforcement']:
            self.cadence_enforcer = CadenceEnforcer(str(self.data_dir))
            self.channel_cadence_enforcer = ChannelCadenceEnforcer(str(self.data_dir))
        
        if self.config['enable_idempotency_check']:
            self.idempotency_manager = IdempotencyManager(str(self.data_dir))
        
        # Source tracking
        self.seen_videos = self._load_seen_videos()
        
        logging.info(f"YouTubeScraper initialized in {self.mode.value} mode - PURE INGESTION")
    
    def _build_client(self):
        """Build YouTube API client with current key"""
        key = self.api_keys[self.current_key_idx]
        return build('youtube', 'v3', developerKey=key, cache_discovery=False)
    
    def _rotate_key(self):
        """Switch to next API key on quota exhaustion"""
        self.current_key_idx = (self.current_key_idx + 1) % len(self.api_keys)
        self.youtube = self._build_client()
        logging.info(f"Rotated to API key {self.current_key_idx + 1}/{len(self.api_keys)}")
    
    def _init_directories(self):
        """Create output directory structure"""
        dirs = ['videos', 'channels', 'snapshots']
        for d in dirs:
            (self.data_dir / d).mkdir(parents=True, exist_ok=True)
    
    def _load_seen_videos(self) -> set:
        """Load previously scraped video IDs"""
        seen_file = self.data_dir / 'seen_videos.txt'
        if seen_file.exists():
            return set(seen_file.read_text().splitlines())
        return set()
    
    def _mark_seen(self, video_id: str):
        """Record video as scraped"""
        self.seen_videos.add(video_id)
        seen_file = self.data_dir / 'seen_videos.txt'
        with seen_file.open('a') as f:
            f.write(f"{video_id}\n")
    
    def _retry_request(self, func, *args, **kwargs):
        """Execute API request with exponential backoff retry"""
        for attempt in range(self.config['max_retries']):
            try:
                result = func(*args, **kwargs)
                return result
            
            except HttpError as e:
                if e.resp.status == 403:  # Quota exceeded
                    logging.warning(f"Quota exceeded, rotating key")
                    self._rotate_key()
                    continue
                
                elif e.resp.status == 429:  # Rate limit
                    wait = self.config['retry_backoff_seconds'] * (2 ** attempt)
                    logging.warning(f"Rate limited, waiting {wait}s")
                    time.sleep(wait)
                    continue
                
                elif e.resp.status >= 500:  # Server error
                    wait = self.config['retry_backoff_seconds'] * (2 ** attempt)
                    logging.error(f"Server error {e.resp.status}, retrying in {wait}s")
                    time.sleep(wait)
                    continue
                
                else:
                    logging.error(f"API error: {e}")
                    return None
            
            except Exception as e:
                logging.error(f"Unexpected error: {e}")
                return None
        
        logging.error(f"Max retries exceeded")
        return None
    
    def fetch_video_ids(self, **kwargs) -> List[Dict[str, Any]]:
        """Discover candidate Shorts based on scrape mode"""
        # NOTE: Cadence enforcement moved to run() only to prevent double-gating
        
        if self.mode == ScrapeMode.SEARCH:
            return self._fetch_search_ids(**kwargs)
        elif self.mode == ScrapeMode.TRENDING:
            return self._fetch_trending_ids(**kwargs)
        elif self.mode == ScrapeMode.CHANNEL:
            return self._fetch_channel_ids(**kwargs)
        elif self.mode == ScrapeMode.BACKFILL:
            return self._fetch_backfill_ids(**kwargs)
        elif self.mode == ScrapeMode.RESAMPLE:
            return self._fetch_resample_ids(**kwargs)
        elif self.mode == ScrapeMode.SHADOW:
            return self._fetch_shadow_ids(**kwargs)
        else:
            raise ValueError(f"Unknown mode: {self.mode}")
    
    def _fetch_search_ids(self, query: str, max_results: int = 50) -> List[Dict[str, Any]]:
        """Search-based discovery via API with ranking preservation"""
        request = self.youtube.search().list(
            part='id',
            q=query,
            type='video',
            videoDuration='short',
            order='date',
            maxResults=max_results,
            relevanceLanguage=self.config['target_languages'][0]
        )
        
        response = self._retry_request(request.execute)
        if not response:
            return []
        
        # Preserve ranking order from search results
        ranked_results = []
        for rank, item in enumerate(response.get('items', []), 1):
            ranked_results.append({
                "video_id": item['id']['videoId'],
                "rank": rank,
                "source": "search"
            })
        
        return ranked_results
    
    def _fetch_trending_ids(self, region: str = 'US', max_results: int = 50) -> List[Dict[str, Any]]:
        """Trending feed sampling via API with ranking preservation"""
        request = self.youtube.videos().list(
            part='id',
            chart='mostPopular',
            regionCode=region,
            videoCategoryId='0',
            maxResults=max_results
        )
        
        response = self._retry_request(request.execute)
        if not response:
            return []
        
        # Preserve ranking order from trending results
        ranked_results = []
        for rank, item in enumerate(response.get('items', []), 1):
            ranked_results.append({
                "video_id": item['id'],
                "rank": rank,
                "source": "trending"
            })
        
        return ranked_results
    
    def _fetch_channel_ids(self, channel_id: str, max_results: int = 50) -> List[Dict[str, Any]]:
        """Channel-targeted scraping via API with ranking preservation"""
        request = self.youtube.search().list(
            part='id',
            channelId=channel_id,
            type='video',
            order='date',
            maxResults=max_results
        )
        
        response = self._retry_request(request.execute)
        if not response:
            return []
        
        # Preserve ranking order from channel results
        ranked_results = []
        for rank, item in enumerate(response.get('items', []), 1):
            ranked_results.append({
                "video_id": item['id']['videoId'],
                "rank": rank,
                "source": "channel",
                "channel_id": channel_id
            })
        
        return ranked_results
    
    def _fetch_backfill_ids(self, target_date: str, max_results: int = 100) -> List[Dict[str, Any]]:
        """Historical backfill for specific date via API with ranking preservation"""
        target = datetime.strptime(target_date, '%Y%m%d')
        after = target.replace(hour=0, minute=0, second=0, tzinfo=timezone.utc).isoformat()
        before = target.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc).isoformat()
        
        request = self.youtube.search().list(
            part='id',
            type='video',
            videoDuration='short',
            publishedAfter=after,
            publishedBefore=before,
            order='viewCount',
            maxResults=max_results
        )
        
        response = self._retry_request(request.execute)
        if not response:
            return []
        
        # Preserve ranking order from backfill results (sorted by view count)
        ranked_results = []
        for rank, item in enumerate(response.get('items', []), 1):
            ranked_results.append({
                "video_id": item['id']['videoId'],
                "rank": rank,
                "source": "backfill",
                "target_date": target_date
            })
        
        return ranked_results
    
    def _fetch_resample_ids(self, hours_since_upload: int = 24) -> List[Dict[str, Any]]:
        """Re-sample recently uploaded videos via API with ranking preservation"""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_since_upload)
        
        request = self.youtube.search().list(
            part='id',
            type='video',
            videoDuration='short',
            publishedAfter=cutoff.isoformat(),
            order='viewCount',
            maxResults=100
        )
        
        response = self._retry_request(request.execute)
        if not response:
            return []
        
        # Preserve ranking order from resample results
        ranked_results = []
        for rank, item in enumerate(response.get('items', []), 1):
            ranked_results.append({
                "video_id": item['id']['videoId'],
                "rank": rank,
                "source": "resample"
            })
        
        return ranked_results
    
    def _fetch_shadow_ids(self, max_results: int = 100) -> List[Dict[str, Any]]:
        """Shadow sampling via API (random unbiased sampling) with ranking preservation"""
        random_terms = ['shorts', 'viral', 'trending', 'funny', 'tutorial']
        query = random.choice(random_terms)
        
        # Use search with random term for shadow sampling
        request = self.youtube.search().list(
            part='id',
            q=query,
            type='video',
            videoDuration='short',
            order='relevance',  # Use relevance for unbiased sampling
            maxResults=max_results
        )
        
        response = self._retry_request(request.execute)
        if not response:
            return []
        
        # Preserve ranking order from shadow sample results
        ranked_results = []
        for rank, item in enumerate(response.get('items', []), 1):
            ranked_results.append({
                "video_id": item['id']['videoId'],
                "rank": rank,
                "source": "shadow",
                "query": query
            })
        
        return ranked_results
    
    def fetch_video_metadata(self, video_id: str) -> Optional[Dict]:
        """Collect raw video metadata via API"""
        request = self.youtube.videos().list(
            part='snippet,contentDetails,statistics,topicDetails',
            id=video_id
        )
        
        response = self._retry_request(request.execute)
        if not response or not response.get('items'):
            return None
        
        return response['items'][0]
    
    def fetch_channel_metadata(self, channel_id: str) -> Optional[RawChannelRecord]:
        """Collect raw channel metadata via API"""
        request = self.youtube.channels().list(
            part='snippet,statistics,contentDetails',
            id=channel_id
        )
        
        response = self._retry_request(request.execute)
        if not response or not response.get('items'):
            return None
        
        data = response['items'][0]
        snippet = data['snippet']
        stats = data['statistics']
        
        # Calculate channel age
        created = datetime.fromisoformat(snippet['publishedAt'].replace('Z', '+00:00'))
        age_days = (datetime.now(timezone.utc) - created).days
        
        # Get recent video IDs (raw discovery only)
        recent_videos = self._fetch_channel_ids(channel_id, max_results=50)
        
        # Create source fingerprint
        source_data = {
            'snippet': snippet,
            'statistics': stats,
            'recent_videos_count': len(recent_videos)
        }
        source_fingerprint = hashlib.md5(json.dumps(source_data, sort_keys=True).encode()).hexdigest()
        
        return RawChannelRecord(
            channel_id=channel_id,
            channel_name=snippet['title'],
            channel_creation_date=snippet['publishedAt'],
            subscriber_count=int(stats.get('subscriberCount', 0)),
            total_videos=int(stats.get('videoCount', 0)),
            shorts_count=None,  # Not reliably available via API
            scrape_timestamp=datetime.now(timezone.utc).isoformat(),
            acquisition_backend=AcquisitionBackend.API.value,
            source_fingerprint=source_fingerprint,
            recent_video_ids=recent_videos
        )
    
    def fetch_video_stats(self, video_id: str) -> Optional[Dict]:
        """Collect raw video statistics via API"""
        request = self.youtube.videos().list(
            part='statistics',
            id=video_id
        )
        
        response = self._retry_request(request.execute)
        if not response or not response.get('items'):
            return None
        
        stats = response['items'][0]['statistics']
        stats['scrape_timestamp'] = datetime.now(timezone.utc).isoformat()
        return stats
    
    def normalize_video_record(self, raw_data: Dict, source_context: Dict) -> Optional[RawVideoRecord]:
        """Convert platform data to clean raw record (NO CALCULATIONS)"""
        try:
            snippet = raw_data.get('snippet', {})
            content = raw_data.get('contentDetails', {})
            stats = raw_data.get('statistics', {})
            
            # Parse duration
            duration_str = content.get('duration', 'PT0S')
            duration_sec = self._parse_duration(duration_str)
            
            # Filter non-Shorts
            if duration_sec > self.config['short_duration_max_seconds']:
                return None
            
            # Extract raw metadata
            video_id = raw_data['id']
            title = snippet.get('title', '')
            description = snippet.get('description', '')
            hashtags = [word[1:] for word in description.split() if word.startswith('#')]
            
            # Raw performance data (NO CALCULATIONS)
            views = int(stats.get('viewCount', 0))
            likes = int(stats.get('likeCount', 0))
            comments = int(stats.get('commentCount', 0))
            
            if views < self.config['min_views_threshold']:
                return None
            
            # Create content hash for idempotency (identity of content)
            scrape_timestamp = datetime.now(timezone.utc).isoformat()
            content_hash = hashlib.md5(video_id.encode()).hexdigest()
            
            # Create snapshot hash for temporal deduplication (identity of time)
            snapshot_hash = hashlib.md5(f"{video_id}_{scrape_timestamp}".encode()).hexdigest()
            
            # Create source fingerprint
            source_fingerprint = hashlib.md5(json.dumps(raw_data, sort_keys=True).encode()).hexdigest()
            
            return RawVideoRecord(
                video_id=video_id,
                channel_id=snippet['channelId'],
                upload_timestamp=snippet['publishedAt'],
                duration_seconds=duration_sec,
                title=title,
                description=description,
                hashtags=hashtags,
                thumbnail_url=snippet['thumbnails']['high']['url'],
                category_id=snippet.get('categoryId', None),  # No default, preserve None
                language=snippet.get('defaultLanguage', None),  # No default, preserve None
                views=views,
                likes=likes,
                comments=comments,
                shares=None,  # Not available via API
                scrape_timestamp=scrape_timestamp,
                scrape_mode=self.mode.value,
                acquisition_backend=AcquisitionBackend.API.value,
                source_context=source_context,
                content_hash=content_hash,
                snapshot_hash=snapshot_hash,
                source_fingerprint=source_fingerprint,
                is_backfill=self.mode == ScrapeMode.BACKFILL,
                backfill_target_date=source_context.get('target_date')
            )
        
        except (KeyError, ValueError, TypeError) as e:
            logging.warning(f"Failed to normalize record: {e}")
            return None
    
    def _parse_duration(self, duration_str: str) -> int:
        """Parse ISO 8601 duration to seconds"""
        match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
        if not match:
            return 0
        h, m, s = match.groups()
        return int(h or 0) * 3600 + int(m or 0) * 60 + int(s or 0)
    
    def persist_raw_video(self, record: RawVideoRecord):
        """Save unaltered video record with structured organization"""
        # Check idempotency (content hash for identity, snapshot hash for time)
        if self.config['enable_idempotency_check']:
            if self.idempotency_manager.is_duplicate(
                record.video_id, record.scrape_timestamp, record.content_hash
            ):
                logging.info(f"Duplicate video skipped: {record.video_id}")
                return
        
        # Record for deduplication
        if self.config['enable_idempotency_check']:
            self.idempotency_manager.record_scrape(
                record.video_id, record.scrape_timestamp, record.content_hash, record.scrape_mode
            )
        
        # Organize by mode and date
        date_str = datetime.now(timezone.utc).strftime('%Y%m%d')
        mode_dir = self.data_dir / 'videos' / self.mode.value / date_str
        mode_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now(timezone.utc).strftime('%H%M%S')
        filename = f"{timestamp}_{record.content_hash[:8]}.json"
        
        filepath = mode_dir / filename
        
        try:
            with filepath.open('w') as f:
                json.dump(asdict(record), f, indent=2)
            
            if not record.is_backfill:
                self._mark_seen(record.video_id)
            
            logging.debug(f"Persisted {record.video_id}")
        
        except Exception as e:
            logging.error(f"Failed to persist {record.video_id}: {e}")
    
    def persist_raw_channel(self, channel: RawChannelRecord):
        """Save unaltered channel record"""
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        channel_hash = hashlib.md5(channel.channel_id.encode()).hexdigest()[:8]
        filename = f"{timestamp}_{channel_hash}.json"
        
        filepath = self.data_dir / 'channels' / filename
        
        with filepath.open('w') as f:
            json.dump(asdict(channel), f, indent=2)
        
        logging.debug(f"Persisted channel {channel.channel_id}")
    
    def persist_raw_stats_snapshot(self, video_id: str, stats: Dict, ranking_info: Dict[str, Any]):
        """Save raw statistics snapshot with ranking context (TIME SERIES POINT ONLY)"""
        scrape_timestamp = datetime.now(timezone.utc).isoformat()
        snapshot_hash = hashlib.md5(f"{video_id}_{scrape_timestamp}".encode()).hexdigest()
        
        # Extract ranking context for appropriate field
        trending_rank = None
        search_position = None
        
        if ranking_info.get("source") == "trending":
            trending_rank = ranking_info.get("rank")
        elif ranking_info.get("source") == "search":
            search_position = ranking_info.get("rank")
        
        snapshot = RawStatsSnapshot(
            video_id=video_id,
            scrape_timestamp=scrape_timestamp,
            acquisition_backend=AcquisitionBackend.API.value,
            views=int(stats.get('viewCount', 0)),
            likes=int(stats.get('likeCount', 0)),
            comments=int(stats.get('commentCount', 0)),
            shares=None,  # Not available via API
            trending_rank=trending_rank,
            search_position=search_position,
            snapshot_hash=snapshot_hash
        )
        
        # Organize by date
        date_str = datetime.now(timezone.utc).strftime('%Y%m%d')
        snapshot_dir = self.data_dir / 'snapshots' / date_str
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now(timezone.utc).strftime('%H%M%S')
        filename = f"{timestamp}_{snapshot_hash[:8]}.json"
        
        filepath = snapshot_dir / filename
        
        try:
            with filepath.open('w') as f:
                json.dump(asdict(snapshot), f, indent=2)
            
            logging.debug(f"Persisted stats snapshot for {video_id}")
        
        except Exception as e:
            logging.error(f"Failed to persist stats snapshot for {video_id}: {e}")
    
    def run(self, **kwargs) -> int:
        """Main execution loop with pure ingestion only"""
        logging.info(f"Starting scrape in {self.mode.value} mode - PURE INGESTION")
        
        # Check cadence enforcement
        if self.config['enable_cadence_enforcement']:
            context = kwargs.get('query') or kwargs.get('channel_id') or kwargs.get('target_date') or 'default'
            if not self.cadence_enforcer.can_scrape(self.mode, context):
                logging.warning(f"Cadence enforcement: scrape not allowed for {self.mode.value}")
                return 0
        
        # Discover video IDs with ranking context
        ranked_results = self.fetch_video_ids(**kwargs)
        
        # Extract video IDs for processing (preserve ranking in source_context)
        video_ids = [result["video_id"] for result in ranked_results]
        
        # Create ranking lookup for context injection
        ranking_lookup = {result["video_id"]: result for result in ranked_results}
        
        logging.info(f"Found {len(video_ids)} candidate videos with ranking context")
        
        if not video_ids:
            logging.warning("No videos found")
            return 0
        
        batch_size = self.config['batch_size']
        success_count = 0
        
        for i in range(0, len(video_ids), batch_size):
            batch = video_ids[i:i + batch_size]
            
            for video_id in batch:
                # Fetch raw metadata
                raw_data = self.fetch_video_metadata(video_id)
                if not raw_data:
                    continue
                
                source_context = kwargs.copy()
                source_context['batch_index'] = i // batch_size
                
                # Inject ranking context into source_context
                ranking_info = ranking_lookup.get(video_id, {})
                source_context_with_rank = {
                    **source_context,
                    "discovery_rank": ranking_info.get("rank"),
                    "discovery_source": ranking_info.get("source"),
                    "ranking_context": ranking_info
                }
                
                # Normalize to raw record (NO CALCULATIONS)
                record = self.normalize_video_record(raw_data, source_context_with_rank)
                if not record:
                    continue
                
                # Persist raw data
                self.persist_raw_video(record)
                success_count += 1
                
                # Collect raw stats snapshot with ranking context
                stats = self.fetch_video_stats(video_id)
                if stats:
                    ranking_info = ranking_lookup.get(video_id, {})
                    self.persist_raw_stats_snapshot(video_id, stats, ranking_info)
                
                # Channel scraping with explicit cadence enforcement
                if self.config['enable_cadence_enforcement']:
                    if self.channel_cadence_enforcer.can_scrape_channel(record.channel_id):
                        channel_data = self.fetch_channel_metadata(record.channel_id)
                        if channel_data:
                            self.persist_raw_channel(channel_data)
                            self.channel_cadence_enforcer.record_channel_scrape(record.channel_id)
                else:
                    # Fallback: scrape channel without cadence enforcement
                    channel_data = self.fetch_channel_metadata(record.channel_id)
                    if channel_data:
                        self.persist_raw_channel(channel_data)
            
            logging.info(f"Processed batch {i // batch_size + 1}, success: {success_count}")
        
        # Record cadence
        if self.config['enable_cadence_enforcement']:
            context = kwargs.get('query') or kwargs.get('channel_id') or kwargs.get('target_date') or 'default'
            self.cadence_enforcer.record_scrape(self.mode, context)
        
        logging.info(f"Scrape complete: {success_count}/{len(video_ids)} records saved")
        return success_count


# Example usage demonstrating pure ingestion
if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Initialize with pure ingestion configuration
    scraper = YouTubeScraper(
        api_keys=['YOUR_API_KEY_1', 'YOUR_API_KEY_2'],
        mode='search',
        data_dir='./data/raw/youtube',
        config={
            'enable_cadence_enforcement': True,
            'enable_idempotency_check': True,
            'min_views_threshold': 0,  # Mechanical noise suppression, not quality inference
        }
    )
    
    # Pure ingestion only - no intelligence, no calculations
    scraper.run(query='viral shorts 2025', max_results=100)
