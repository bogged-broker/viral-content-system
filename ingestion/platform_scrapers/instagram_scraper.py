"""
instagram_scraper.py - Full-spec Instagram content ingestion system

Purpose:
    Raw ingestion of posts, stories, reels with high-resolution time-series
    metadata for ML/RL pipelines. NO scoring, ranking, or feature engineering.

Key Features:
    - Deterministic sampling cadence (age-based)
    - Multi-format support (post/story/reel)
    - Backfill vs Live modes
    - Key/session rotation with cooldown
    - Idempotency via hash-based dedupe
    - Partial record semantics
    - Circuit breaker & exponential backoff
"""

from typing import Literal, List, Dict, Optional, Set, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import pandas as pd
import json
import time
import hashlib
import os
from pathlib import Path
import logging
from enum import Enum
import yaml  # for config loading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import aiohttp
import asyncio
from pathlib import Path
import logging
from enum import Enum
import traceback
import re
from urllib.parse import urlparse
import random
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
import signal
import sys
from contextlib import contextmanager

# System integrations
try:
    from config_loader import load_niche_config as load_config
except ImportError:
    logger.warning("config_loader.py not found, using fallback")
    def load_config(niche: str) -> Dict:
        return {'niche': niche, 'accounts': [], 'content_types': ['post']}

try:
    from metrics_utils import compute_velocity_metrics
except ImportError:
    logger.warning("metrics_utils.py not found, using placeholder")
    def compute_velocity_metrics(content_data: List[Dict]) -> List[Dict]:
        return content_data  # placeholder

try:
    from long_tail_tracker import feed_engagement_data
except ImportError:
    logger.warning("long_tail_tracker.py not found, using placeholder")
    def feed_engagement_data(data: Dict):
        pass  # placeholder

try:
    from account_system.geo_allocator import GeoProxyAllocator
except ImportError:
    logger.warning("geo_allocator.py not found, using fallback")
    class GeoProxyAllocator:
        def get_proxy_for_account(self, account: str) -> Optional[str]:
            return None

try:
    from alerting import trigger_alert, AlertType
except ImportError:
    logger.warning("alerting.py not found, using fallback")
    def trigger_alert(alert_type: str, message: str, context: Dict = None):
        logger.error(f"ALERT [{alert_type}]: {message}")
    class AlertType:
        FAILURE = "failure"
        QUOTA_HIT = "quota_hit"
        RATE_LIMIT = "rate_limit"

try:
    from ingestion_pipeline import IngestionJob, IngestionMode
except ImportError:
    logger.warning("ingestion_pipeline.py not found, using fallback")
    class IngestionJob:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
    class IngestionMode:
        LIVE = "live"
        BACKFILL = "backfill"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ContentType(Enum):
    POST = "post"
    STORY = "story"
    REEL = "reel"


class ContentQuality(Enum):
    HIGH = "high"      # 5M+ baseline potential
    MEDIUM = "medium"  # 1M-5M potential
    LOW = "low"        # <1M potential


class ViralitySignals(Enum):
    EXPLOSIVE = "explosive"  # 30M+ potential
    VIRAL = "viral"          # 10M-30M potential
    TRENDING = "trending"    # 5M-10M potential
    GROWING = "growing"      # 1M-5M potential


@dataclass
class VelocityMetrics:
    """Real-time velocity and acceleration metrics."""
    likes_velocity: float  # likes per hour
    comments_velocity: float  # comments per hour
    shares_velocity: float  # shares per hour
    views_velocity: float  # views per hour
    likes_acceleration: float  # likes per hour²
    comments_acceleration: float  # comments per hour²
    shares_acceleration: float  # shares per hour²
    views_acceleration: float  # views per hour²
    engagement_velocity: float  # total engagement per hour
    engagement_acceleration: float  # total engagement per hour²
    calculated_at: str


@dataclass
class ContentMetadata:
    """Enhanced content metadata with virality prediction signals."""
    # Hard-required fields
    content_id: str
    account_handle: str
    scrape_timestamp: str
    content_type: str
    
    # Soft-required fields (can be None)
    caption: Optional[str] = None
    media_url: Optional[str] = None
    video_length: Optional[float] = None
    likes: Optional[int] = None
    comments: Optional[int] = None
    shares: Optional[int] = None
    views: Optional[int] = None
    posted_at: Optional[str] = None
    
    # Enhanced virality prediction fields
    content_quality: Optional[str] = None  # HIGH/MEDIUM/LOW
    virality_signal: Optional[str] = None  # EXPLOSIVE/VIRAL/TRENDING/GROWING
    baseline_potential: Optional[float] = None  # 0.0-1.0 for 5M+ baseline
    repeatable_potential: Optional[float] = None  # 0.0-1.0 for 30M-300M+ repeatable
    
    # Engagement quality metrics
    engagement_rate: Optional[float] = None
    share_rate: Optional[float] = None  # shares/views
    comment_rate: Optional[float] = None  # comments/views
    like_rate: Optional[float] = None  # likes/views
    
    # Content analysis metrics
    caption_length: Optional[int] = None
    hashtag_count: Optional[int] = None
    mention_count: Optional[int] = None
    media_type: Optional[str] = None  # image/video/carousel
    
    # Temporal metrics
    peak_engagement_time: Optional[str] = None
    engagement_half_life: Optional[float] = None  # hours until 50% engagement
    
    # Velocity placeholders (computed elsewhere)
    likes_per_hour: Optional[float] = None
    comments_per_hour: Optional[float] = None
    shares_per_hour: Optional[float] = None
    velocity_metrics: Optional[VelocityMetrics] = None


@dataclass
class AccountMetadata:
    """Enhanced account-level authority signals for virality prediction."""
    account_handle: str
    followers: int
    following: int
    total_posts: int
    engagement_rate: float
    last_updated: str
    
    # Enhanced authority metrics
    follower_growth_rate: Optional[float] = None  # followers per day
    post_frequency: Optional[float] = None  # posts per day
    avg_likes: Optional[float] = None
    avg_comments: Optional[float] = None
    avg_shares: Optional[float] = None
    avg_views: Optional[float] = None
    
    # Virality history
    viral_posts_count: Optional[int] = None  # posts >1M views
    explosive_posts_count: Optional[int] = None  # posts >10M views
    repeatable_viral_rate: Optional[float] = None  # % of posts that go viral repeatedly
    
    # Content quality signals
    content_quality_score: Optional[float] = None  # 0.0-1.0
    niche_authority: Optional[float] = None  # 0.0-1.0 authority in niche
    cross_platform_presence: Optional[bool] = None
    
    # Audience demographics
    audience_age_avg: Optional[float] = None
    audience_gender_split: Optional[Dict[str, float]] = None
    audience_geography: Optional[Dict[str, float]] = None
    
    # Monetization signals
    brand_partnerships: Optional[int] = None
    sponsored_post_rate: Optional[float] = None
    estimated_cpm: Optional[float] = None
    
    # Platform-specific metrics
    story_completion_rate: Optional[float] = None
    reel_watch_time: Optional[float] = None
    save_rate: Optional[float] = None  # saves/views


class SessionManager:
    """Manages API sessions/credentials with rotation, cooldown, and proxy support."""
    
    def __init__(self, sessions: List[Dict], geo_allocator: Optional[GeoProxyAllocator] = None):
        self.sessions = sessions
        self.current_idx = 0
        self.failed_sessions: Dict[int, datetime] = {}
        self.cooldown_period = timedelta(minutes=15)
        self.geo_allocator = geo_allocator
        self.session_pool = {}  # Reuse HTTP sessions
        self.rate_limit_tracker: Dict[str, List[datetime]] = {}  # Track rate limits per session
    
    def get_active_session(self) -> Dict:
        """Get current active session with proxy, skipping cooled-down ones."""
        attempts = 0
        while attempts < len(self.sessions):
            session = self.sessions[self.current_idx]
            
            # Check if session is in cooldown
            if self.current_idx in self.failed_sessions:
                cooldown_until = self.failed_sessions[self.current_idx]
                if datetime.now() < cooldown_until:
                    logger.warning(f"Session {self.current_idx} in cooldown, rotating")
                    self.rotate()
                    attempts += 1
                    continue
                else:
                    # Cooldown expired, remove from failed list
                    del self.failed_sessions[self.current_idx]
            
            # Add proxy if geo allocator available
            if self.geo_allocator:
                proxy = self.geo_allocator.get_proxy_for_account(session.get('account_handle', 'default'))
                if proxy:
                    session['proxy'] = proxy
                    logger.debug(f"Using proxy {proxy} for session {self.current_idx}")
            
            return session
        
        raise RuntimeError("All sessions in cooldown or failed")
    
    def rotate(self):
        """Rotate to next session."""
        self.current_idx = (self.current_idx + 1) % len(self.sessions)
        logger.info(f"Rotated to session {self.current_idx}")
    
    def mark_failed(self, error_type: str, details: Dict = None):
        """Mark current session as failed with cooldown and alerting."""
        cooldown_until = datetime.now() + self.cooldown_period
        self.failed_sessions[self.current_idx] = cooldown_until
        
        # Trigger alert for session failure
        alert_context = {
            'session_id': self.current_idx,
            'cooldown_until': cooldown_until.isoformat(),
            'error_type': error_type,
            'details': details or {}
        }
        
        if error_type in ['rate_limit', 'quota_exceeded']:
            trigger_alert(AlertType.QUOTA_HIT, f"Session {self.current_idx} hit quota/rate limit", alert_context)
        else:
            trigger_alert(AlertType.FAILURE, f"Session {self.current_idx} failed: {error_type}", alert_context)
        
        logger.error(f"Session {self.current_idx} failed ({error_type}), cooldown until {cooldown_until}")
        self.rotate()
    
    def check_rate_limit(self, session_id: str) -> bool:
        """Check if session is rate limited based on recent calls."""
        now = datetime.now()
        if session_id not in self.rate_limit_tracker:
            self.rate_limit_tracker[session_id] = []
        
        # Clean old entries (older than 1 hour)
        self.rate_limit_tracker[session_id] = [
            ts for ts in self.rate_limit_tracker[session_id] 
            if now - ts < timedelta(hours=1)
        ]
        
        # Check if exceeded rate limit (200 calls per hour max)
        if len(self.rate_limit_tracker[session_id]) > 200:
            return True
        
        # Record this call
        self.rate_limit_tracker[session_id].append(now)
        return False
    
    def get_http_session(self, session: Dict) -> requests.Session:
        """Get or create HTTP session with retry strategy."""
        session_id = session.get('session_id', 'default')
        if session_id not in self.session_pool:
            http_session = requests.Session()
            
            # Configure retry strategy
            retry_strategy = Retry(
                total=3,
                backoff_factor=1,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["HEAD", "GET", "OPTIONS"]
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            http_session.mount("http://", adapter)
            http_session.mount("https://", adapter)
            
            # Add proxy if available
            if 'proxy' in session:
                http_session.proxies = {'http': session['proxy'], 'https': session['proxy']}
            
            # Add headers
            http_session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json',
                'Accept-Language': 'en-US,en;q=0.9'
            })
            
            self.session_pool[session_id] = http_session
        
        return self.session_pool[session_id]


class CadenceManager:
    """Deterministic sampling cadence based on content age."""
    
    @staticmethod
    def get_sampling_interval(content_age_hours: float) -> timedelta:
        """
        Returns sampling interval based on content age:
            age <= 2h      → every 5-10 min
            2h < age <= 24h → every 30-60 min
            1d < age <= 7d → every 6-12h
            age > 7d       → every 24-72h
        """
        if content_age_hours <= 2:
            return timedelta(minutes=7)  # midpoint of 5-10
        elif content_age_hours <= 24:
            return timedelta(minutes=45)  # midpoint of 30-60
        elif content_age_hours <= 168:  # 7 days
            return timedelta(hours=9)  # midpoint of 6-12
        else:
            return timedelta(hours=48)  # midpoint of 24-72
    
    @staticmethod
    def should_sample(last_scrape: datetime, content_posted_at: datetime) -> bool:
        """Check if content should be sampled based on cadence rules."""
        now = datetime.now()
        content_age = (now - content_posted_at).total_seconds() / 3600  # hours
        interval = CadenceManager.get_sampling_interval(content_age)
        
        time_since_last_scrape = now - last_scrape
        return time_since_last_scrape >= interval


class IdempotencyManager:
    """Hash-based deduplication to prevent duplicate ingestion."""
    
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.seen_keys: Set[str] = self._load_seen_keys()
    
    def _load_seen_keys(self) -> Set[str]:
        """Load previously seen idempotency keys."""
        keys_file = self.state_dir / "seen_keys.json"
        if keys_file.exists():
            with open(keys_file, 'r') as f:
                return set(json.load(f))
        return set()
    
    def generate_key(self, content: Dict, run_type: str) -> str:
        """Generate idempotency key from content."""
        # Round timestamp to 5-minute window for live mode
        ts = content.get('scrape_timestamp', '')
        if run_type == "live":
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            rounded = dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)
            ts = rounded.isoformat()
        
        key_string = f"{content['content_id']}_{ts}_{run_type}"
        return hashlib.sha256(key_string.encode()).hexdigest()
    
    def is_duplicate(self, key: str) -> bool:
        """Check if key has been seen before."""
        return key in self.seen_keys
    
    def mark_seen(self, key: str):
        """Mark key as seen."""
        self.seen_keys.add(key)
    
    def persist_keys(self):
        """Save seen keys to disk."""
        keys_file = self.state_dir / "seen_keys.json"
        with open(keys_file, 'w') as f:
            json.dump(list(self.seen_keys), f)


class ViralityPredictor:
    """Predicts 5M+ baseline and 30M-300M+ repeatable virality potential."""
    
    def __init__(self):
        self.baseline_threshold = 5000000  # 5M baseline
        self.viral_threshold = 10000000     # 10M viral
        self.explosive_threshold = 30000000 # 30M explosive
        
        # Weight factors for prediction
        self.weights = {
            'engagement_velocity': 0.25,
            'content_quality': 0.20,
            'account_authority': 0.15,
            'temporal_signals': 0.15,
            'cross_platform': 0.10,
            'niche_trend': 0.15
        }
    
    def predict_content_potential(self, content: ContentMetadata, account_meta: AccountMetadata, 
                                velocity_metrics: Optional[VelocityMetrics] = None) -> Tuple[str, float, float]:
        """
        Predict content virality potential.
        
        Returns:
            Tuple of (virality_signal, baseline_potential, repeatable_potential)
        """
        baseline_score = 0.0
        repeatable_score = 0.0
        
        # Factor 1: Engagement velocity (most important for early detection)
        if velocity_metrics:
            velocity_score = self._calculate_velocity_score(velocity_metrics)
            baseline_score += velocity_score * self.weights['engagement_velocity']
            repeatable_score += velocity_score * self.weights['engagement_velocity'] * 1.2  # Higher weight for repeatable
        
        # Factor 2: Content quality signals
        quality_score = self._calculate_content_quality_score(content)
        baseline_score += quality_score * self.weights['content_quality']
        repeatable_score += quality_score * self.weights['content_quality']
        
        # Factor 3: Account authority
        authority_score = self._calculate_authority_score(account_meta)
        baseline_score += authority_score * self.weights['account_authority']
        repeatable_score += authority_score * self.weights['account_authority'] * 1.5  # Much higher for repeatable
        
        # Factor 4: Temporal signals
        temporal_score = self._calculate_temporal_score(content)
        baseline_score += temporal_score * self.weights['temporal_signals']
        repeatable_score += temporal_score * self.weights['temporal_signals']
        
        # Factor 5: Cross-platform presence
        cross_platform_score = 1.0 if account_meta.cross_platform_presence else 0.3
        baseline_score += cross_platform_score * self.weights['cross_platform']
        repeatable_score += cross_platform_score * self.weights['cross_platform'] * 2.0  # Critical for repeatable
        
        # Factor 6: Niche trend alignment
        niche_score = self._calculate_niche_trend_score(content, account_meta)
        baseline_score += niche_score * self.weights['niche_trend']
        repeatable_score += niche_score * self.weights['niche_trend']
        
        # Normalize scores
        baseline_score = min(max(baseline_score, 0.0), 1.0)
        repeatable_score = min(max(repeatable_score, 0.0), 1.0)
        
        # Determine virality signal
        virality_signal = self._classify_virality(baseline_score, repeatable_score)
        
        return virality_signal, baseline_score, repeatable_score
    
    def _calculate_velocity_score(self, velocity: VelocityMetrics) -> float:
        """Calculate score from velocity metrics."""
        # High velocity = high potential
        engagement_velocity = velocity.engagement_velocity
        engagement_acceleration = velocity.engagement_acceleration
        
        # Normalize velocity (assuming max 1000 engagements/hour as baseline)
        velocity_score = min(engagement_velocity / 1000.0, 1.0)
        
        # Boost for positive acceleration
        if engagement_acceleration > 0:
            acceleration_boost = min(engagement_acceleration / 100.0, 0.3)
            velocity_score += acceleration_boost
        
        return min(velocity_score, 1.0)
    
    def _calculate_content_quality_score(self, content: ContentMetadata) -> float:
        """Calculate content quality score."""
        score = 0.0
        factors = 0
        
        # Engagement rate
        if content.engagement_rate:
            score += min(content.engagement_rate * 20, 1.0)  # 5% engagement = full score
            factors += 1
        
        # Share rate (critical for virality)
        if content.share_rate:
            score += min(content.share_rate * 100, 1.0)  # 1% share rate = full score
            factors += 1
        
        # Caption optimization
        if content.caption_length:
            # Optimal caption length: 80-150 characters for Instagram
            if 80 <= content.caption_length <= 150:
                score += 1.0
            elif 50 <= content.caption_length <= 200:
                score += 0.7
            else:
                score += 0.3
            factors += 1
        
        # Hashtag optimization
        if content.hashtag_count:
            # Optimal: 5-15 hashtags
            if 5 <= content.hashtag_count <= 15:
                score += 1.0
            elif 3 <= content.hashtag_count <= 20:
                score += 0.7
            else:
                score += 0.3
            factors += 1
        
        # Media type (video tends to perform better)
        if content.media_type:
            if content.media_type == 'video':
                score += 0.8
            elif content.media_type == 'carousel':
                score += 0.6
            else:  # image
                score += 0.4
            factors += 1
        
        return score / max(factors, 1)
    
    def _calculate_authority_score(self, account: AccountMetadata) -> float:
        """Calculate account authority score."""
        score = 0.0
        factors = 0
        
        # Follower count (logarithmic scale)
        if account.followers > 0:
            follower_score = min(np.log10(max(account.followers, 1000)) / 6.0, 1.0)  # 1M followers = ~1.0
            score += follower_score
            factors += 1
        
        # Engagement rate
        if account.engagement_rate > 0:
            engagement_score = min(account.engagement_rate * 20, 1.0)  # 5% = full score
            score += engagement_score
            factors += 1
        
        # Viral history
        if account.repeatable_viral_rate:
            score += account.repeatable_viral_rate
            factors += 1
        
        # Content quality score
        if account.content_quality_score:
            score += account.content_quality_score
            factors += 1
        
        # Niche authority
        if account.niche_authority:
            score += account.niche_authority
            factors += 1
        
        return score / max(factors, 1)
    
    def _calculate_temporal_score(self, content: ContentMetadata) -> float:
        """Calculate temporal optimization score."""
        score = 0.5  # Base score
        
        # Peak engagement time detection
        if content.peak_engagement_time:
            current_hour = datetime.now().hour
            peak_hour = datetime.fromisoformat(content.peak_engagement_time).hour
            
            # Within 2 hours of peak = optimal
            hour_diff = abs(current_hour - peak_hour)
            if hour_diff <= 2 or hour_diff >= 22:  # Wrap around midnight
                score += 0.3
            else:
                score -= 0.1
        
        # Engagement half-life (longer = better for repeatable)
        if content.engagement_half_life:
            if content.engagement_half_life > 24:  # More than 24 hours
                score += 0.2
            elif content.engagement_half_life > 12:
                score += 0.1
        
        return min(max(score, 0.0), 1.0)
    
    def _calculate_niche_trend_score(self, content: ContentMetadata, account: AccountMetadata) -> float:
        """Calculate niche trend alignment score."""
        # This would integrate with trend_aggregator.py
        # For now, use account niche authority as proxy
        if account.niche_authority:
            return account.niche_authority
        return 0.5  # Default moderate score
    
    def _classify_virality(self, baseline_score: float, repeatable_score: float) -> str:
        """Classify virality signal based on scores."""
        if repeatable_score >= 0.8 and baseline_score >= 0.7:
            return ViralitySignals.EXPLOSIVE.value
        elif repeatable_score >= 0.6 and baseline_score >= 0.5:
            return ViralitySignals.VIRAL.value
        elif baseline_score >= 0.4:
            return ViralitySignals.TRENDING.value
        elif baseline_score >= 0.2:
            return ViralitySignals.GROWING.value
        else:
            return "low"


class VelocityAnalyzer:
    """Analyzes engagement velocity and acceleration for virality prediction."""
    
    def __init__(self):
        self.historical_data: Dict[str, List[Dict]] = {}  # content_id -> [historical snapshots]
    
    def analyze_content_velocity(self, content_id: str, current_metrics: ContentMetadata, 
                               account_history: Optional[List[Dict]] = None) -> VelocityMetrics:
        """Analyze velocity and acceleration for content."""
        now = datetime.now()
        
        # Get historical data for this content
        history = self.historical_data.get(content_id, [])
        
        # Add current snapshot to history
        current_snapshot = {
            'timestamp': now.isoformat(),
            'likes': current_metrics.likes or 0,
            'comments': current_metrics.comments or 0,
            'shares': current_metrics.shares or 0,
            'views': current_metrics.views or 0
        }
        history.append(current_snapshot)
        
        # Keep only last 24 hours of data
        cutoff_time = now - timedelta(hours=24)
        history = [snap for snap in history if datetime.fromisoformat(snap['timestamp']) > cutoff_time]
        self.historical_data[content_id] = history
        
        if len(history) < 2:
            # Not enough data for velocity calculation
            return VelocityMetrics(
                likes_velocity=0.0, comments_velocity=0.0, shares_velocity=0.0, views_velocity=0.0,
                likes_acceleration=0.0, comments_acceleration=0.0, shares_acceleration=0.0, views_acceleration=0.0,
                engagement_velocity=0.0, engagement_acceleration=0.0,
                calculated_at=now.isoformat()
            )
        
        # Calculate velocities (rate of change)
        latest = history[-1]
        previous = history[-2]
        
        time_diff_hours = (datetime.fromisoformat(latest['timestamp']) - 
                          datetime.fromisoformat(previous['timestamp'])).total_seconds() / 3600
        
        if time_diff_hours <= 0:
            time_diff_hours = 0.1  # Prevent division by zero
        
        likes_velocity = (latest['likes'] - previous['likes']) / time_diff_hours
        comments_velocity = (latest['comments'] - previous['comments']) / time_diff_hours
        shares_velocity = (latest['shares'] - previous['shares']) / time_diff_hours
        views_velocity = (latest['views'] - previous['views']) / time_diff_hours
        
        engagement_velocity = likes_velocity + comments_velocity + shares_velocity
        
        # Calculate acceleration (rate of change of velocity)
        if len(history) >= 3:
            prev_prev = history[-3]
            prev_time_diff = (datetime.fromisoformat(previous['timestamp']) - 
                            datetime.fromisoformat(prev_prev['timestamp'])).total_seconds() / 3600
            
            if prev_time_diff > 0:
                prev_likes_velocity = (previous['likes'] - prev_prev['likes']) / prev_time_diff
                prev_comments_velocity = (previous['comments'] - prev_prev['comments']) / prev_time_diff
                prev_shares_velocity = (previous['shares'] - prev_prev['shares']) / prev_time_diff
                prev_views_velocity = (previous['views'] - prev_prev['views']) / prev_time_diff
                prev_engagement_velocity = prev_likes_velocity + prev_comments_velocity + prev_shares_velocity
                
                likes_acceleration = (likes_velocity - prev_likes_velocity) / time_diff_hours
                comments_acceleration = (comments_velocity - prev_comments_velocity) / time_diff_hours
                shares_acceleration = (shares_velocity - prev_shares_velocity) / time_diff_hours
                views_acceleration = (views_velocity - prev_views_velocity) / time_diff_hours
                engagement_acceleration = (engagement_velocity - prev_engagement_velocity) / time_diff_hours
            else:
                likes_acceleration = comments_acceleration = shares_acceleration = 0.0
                views_acceleration = engagement_acceleration = 0.0
        else:
            likes_acceleration = comments_acceleration = shares_acceleration = 0.0
            views_acceleration = engagement_acceleration = 0.0
        
        return VelocityMetrics(
            likes_velocity=max(likes_velocity, 0.0),  # Don't allow negative velocities
            comments_velocity=max(comments_velocity, 0.0),
            shares_velocity=max(shares_velocity, 0.0),
            views_velocity=max(views_velocity, 0.0),
            likes_acceleration=likes_acceleration,
            comments_acceleration=comments_acceleration,
            shares_acceleration=shares_acceleration,
            views_acceleration=views_acceleration,
            engagement_velocity=max(engagement_velocity, 0.0),
            engagement_acceleration=engagement_acceleration,
            calculated_at=now.isoformat()
        )


class ContentAnalyzer:
    """Analyzes content for quality signals and virality indicators."""
    
    def __init__(self):
        self.virality_predictor = ViralityPredictor()
        self.velocity_analyzer = VelocityAnalyzer()
    
    def analyze_content(self, raw_content: Dict, account_meta: AccountMetadata) -> ContentMetadata:
        """Perform comprehensive content analysis."""
        # Base metadata extraction
        content = self._extract_base_metadata(raw_content)
        
        # Content quality analysis
        content = self._analyze_content_quality(content)
        
        # Velocity analysis
        velocity_metrics = self.velocity_analyzer.analyze_content_velocity(
            content.content_id, content
        )
        content.velocity_metrics = velocity_metrics
        
        # Virality prediction
        virality_signal, baseline_potential, repeatable_potential = self.virality_predictor.predict_content_potential(
            content, account_meta, velocity_metrics
        )
        
        content.virality_signal = virality_signal
        content.baseline_potential = baseline_potential
        content.repeatable_potential = repeatable_potential
        content.content_quality = self._classify_content_quality(baseline_potential)
        
        return content
    
    def _extract_base_metadata(self, raw_content: Dict) -> ContentMetadata:
        """Extract base metadata from raw content."""
        caption = raw_content.get('caption', '')
        
        # Analyze caption
        hashtags = re.findall(r'#\w+', caption)
        mentions = re.findall(r'@\w+', caption)
        
        return ContentMetadata(
            content_id=raw_content['id'],
            account_handle=raw_content['account_handle'],
            scrape_timestamp=datetime.now().isoformat(),
            content_type=raw_content['type'],
            caption=caption,
            media_url=raw_content.get('media_url'),
            video_length=raw_content.get('video_length'),
            likes=raw_content.get('likes'),
            comments=raw_content.get('comments'),
            shares=raw_content.get('shares'),
            views=raw_content.get('views'),
            posted_at=raw_content.get('posted_at'),
            caption_length=len(caption) if caption else 0,
            hashtag_count=len(hashtags),
            mention_count=len(mentions),
            media_type=self._detect_media_type(raw_content)
        )
    
    def _analyze_content_quality(self, content: ContentMetadata) -> ContentMetadata:
        """Analyze content quality signals."""
        likes = content.likes or 0
        comments = content.comments or 0
        shares = content.shares or 0
        views = content.views or 0
        
        # Calculate engagement rates
        if views > 0:
            content.like_rate = likes / views
            content.comment_rate = comments / views
            content.share_rate = shares / views
            content.engagement_rate = (likes + comments + shares) / views
        elif likes > 0:
            # Fallback for posts without views
            content.engagement_rate = (comments + shares) / likes if likes > 0 else 0
        
        # Detect peak engagement time (simplified)
        if content.posted_at:
            posted_time = datetime.fromisoformat(content.posted_at.replace('Z', '+00:00'))
            content.peak_engagement_time = posted_time.replace(hour=18, minute=0).isoformat()  # Assume 6 PM peak
        
        return content
    
    def _detect_media_type(self, raw_content: Dict) -> str:
        """Detect media type from content."""
        if raw_content.get('video_length'):
            return 'video'
        elif raw_content.get('media_url', '').endswith('.jpg') or raw_content.get('media_url', '').endswith('.png'):
            return 'image'
        else:
            return 'carousel'  # Default assumption
    
    def _classify_content_quality(self, baseline_potential: float) -> str:
        """Classify content quality based on baseline potential."""
        if baseline_potential >= 0.7:
            return ContentQuality.HIGH.value
        elif baseline_potential >= 0.4:
            return ContentQuality.MEDIUM.value
        else:
            return ContentQuality.LOW.value


class InstagramScraper:
    """
    Full-spec Instagram scraper with deterministic cadence, idempotency,
    multi-format support, backfill/live modes, and key/session rotation.
    """
    
    def __init__(
        self,
        niche_config: Dict,
        run_type: Literal["live", "backfill"],
        dry_run: bool = False
    ):
        """
        Initialize scraper.
        
        Args:
            niche_config: dict with accounts, hashtags, cadence rules, API keys
            run_type: 'live' or 'backfill'
            dry_run: if True, don't persist data (for testing)
        """
        self.config = niche_config
        self.run_type = run_type
        self.dry_run = dry_run
        
        # Setup directories
        self.state_dir = Path(f"/data/processed/instagram/{niche_config['niche']}")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        
        # Load state
        self.account_state = self._load_account_state()
        
        # Initialize managers with geo allocator
        geo_allocator = GeoProxyAllocator() if 'geo_allocator' in niche_config else None
        self.session_mgr = SessionManager(niche_config.get('api_sessions', []), geo_allocator)
        self.idempotency_mgr = IdempotencyManager(self.state_dir)
        # Initialize virality prediction systems
        self.content_analyzer = ContentAnalyzer()
        self.virality_predictor = ViralityPredictor()
        self.velocity_analyzer = VelocityAnalyzer()
        
        # Circuit breaker for API failures
        self.circuit_breaker = CircuitBreaker()
        
        # Ingestion pipeline integration
        self.ingestion_job_id = None
        self.pipeline_mode = IngestionMode.LIVE if run_type == "live" else IngestionMode.BACKFILL
        
        # Enhanced statistics tracking
        self.stats = {
            'fetched': 0,
            'duplicates': 0,
            'errors': 0,
            'persisted': 0,
            'deleted_content': 0,
            'private_accounts': 0,
            'expired_stories': 0,
            # Virality prediction stats
            'high_potential_content': 0,  # 5M+ baseline
            'explosive_potential': 0,     # 30M+ repeatable
            'viral_signals_detected': 0,
            'velocity_analyzed': 0,
            # Performance metrics
            'avg_engagement_velocity': 0.0,
            'peak_acceleration': 0.0,
            'content_quality_distribution': {'high': 0, 'medium': 0, 'low': 0}
        }
        
        # Performance tracking
        self.start_time = datetime.now()
        self.velocity_history: List[VelocityMetrics] = []
    
    def _load_account_state(self) -> Dict:
        """
        Load last scrape timestamps and last content IDs per account.
        
        Returns:
            Dict[account_handle -> {last_scrape_ts, last_content_id, content_history}]
        """
        state_file = self.state_dir / "account_state.json"
        if state_file.exists():
            with open(state_file, 'r') as f:
                return json.load(f)
        return {}
    
    def _save_account_state(self):
        """Persist account state to disk."""
        state_file = self.state_dir / "account_state.json"
        with open(state_file, 'w') as f:
            json.dump(self.account_state, f, indent=2)
    
    def _init_account_state(self, account_handle: str):
        """Initialize state for new account."""
        if account_handle not in self.account_state:
            self.account_state[account_handle] = {
                'last_scrape_ts': None,
                'last_content_id': None,
                'content_history': {}  # content_id -> [scrape_timestamps]
            }
    
    def fetch_content(
        self,
        account_handle: str,
        content_type: ContentType
    ) -> List[Dict]:
        """
        Fetch posts, stories, or reels for account using deterministic cadence.
        
        Args:
            account_handle: Instagram handle
            content_type: POST, STORY, or REEL
        
        Returns:
            List of raw content dicts
        """
        self._init_account_state(account_handle)
        
        try:
            session = self.session_mgr.get_active_session()
            
            # Mock API call - replace with actual Instagram API/scraper
            raw_contents = self._mock_fetch_api(
                account_handle,
                content_type.value,
                session
            )
            
            # Filter based on cadence rules
            filtered_contents = []
            for content in raw_contents:
                content_id = content['id']
                posted_at = datetime.fromisoformat(content['posted_at'])
                
                # Check if we've scraped this content before
                history = self.account_state[account_handle]['content_history']
                if content_id in history and history[content_id]:
                    last_scrape = datetime.fromisoformat(history[content_id][-1])
                    if not CadenceManager.should_sample(last_scrape, posted_at):
                        continue
                
                filtered_contents.append(content)
            
            self.stats['fetched'] += len(filtered_contents)
            return filtered_contents
            
        except Exception as e:
            logger.error(f"Error fetching {content_type.value} for {account_handle}: {e}")
            self.stats['errors'] += 1
            self.session_mgr.mark_failed(str(type(e).__name__))
            return []
    
    def _mock_fetch_api(
        self,
        account_handle: str,
        content_type: str,
        session: Dict
    ) -> List[Dict]:
        """
        Mock API fetch - replace with actual Instagram scraper.
        
        This would use instaloader, apify, playwright, or official API.
        """
        # Simulate API response
        now = datetime.now()
        mock_contents = []
        
        for i in range(3):
            age_hours = i * 5  # 0h, 5h, 10h old content
            posted_at = now - timedelta(hours=age_hours)
            
            mock_contents.append({
                'id': f"{account_handle}_{content_type}_{i}",
                'type': content_type,
                'account_handle': account_handle,
                'caption': f"Mock {content_type} caption {i}",
                'media_url': f"https://example.com/media/{i}.jpg",
                'likes': 1000 + i * 500,
                'comments': 50 + i * 10,
                'shares': 20 + i * 5,
                'views': 5000 + i * 1000 if content_type == 'reel' else None,
                'posted_at': posted_at.isoformat(),
                'video_length': 30.5 if content_type == 'reel' else None
            })
        
        return mock_contents
    
    def fetch_account_metadata(self, account_handle: str) -> Optional[AccountMetadata]:
        """
        Fetch account-level metrics (followers, engagement rate).
        Updated daily or lazily.
        """
        try:
            session = self.session_mgr.get_active_session()
            
            # Mock account metadata - replace with actual API call
            metadata = AccountMetadata(
                account_handle=account_handle,
                followers=100000,
                following=500,
                total_posts=1200,
                engagement_rate=0.045,  # 4.5%
                last_updated=datetime.now().isoformat()
            )
            
            return metadata
            
        except Exception as e:
            logger.error(f"Error fetching account metadata for {account_handle}: {e}")
            self.stats['errors'] += 1
            return None
    
    def _normalize_content(self, raw_content: Dict) -> ContentMetadata:
        """Convert raw API response to ContentMetadata."""
        return ContentMetadata(
            content_id=raw_content['id'],
            account_handle=raw_content['account_handle'],
            scrape_timestamp=datetime.now().isoformat(),
            content_type=raw_content['type'],
            caption=raw_content.get('caption'),
            media_url=raw_content.get('media_url'),
            video_length=raw_content.get('video_length'),
            likes=raw_content.get('likes'),
            comments=raw_content.get('comments'),
            shares=raw_content.get('shares'),
            views=raw_content.get('views'),
            posted_at=raw_content.get('posted_at')
        )
    
    def persist_content(self, contents: List[ContentMetadata]):
        """
        Save content to processed store with idempotency check.
        """
        if self.dry_run:
            logger.info(f"[DRY RUN] Would persist {len(contents)} contents")
            return
        
        persisted = []
        for content in contents:
            content_dict = asdict(content)
            
            # Check idempotency
            idempotency_key = self.idempotency_mgr.generate_key(
                content_dict,
                self.run_type
            )
            
            if self.idempotency_mgr.is_duplicate(idempotency_key):
                self.stats['duplicates'] += 1
                continue
            
            # Mark as seen
            self.idempotency_mgr.mark_seen(idempotency_key)
            persisted.append(content_dict)
            
            # Update account state
            account = content.account_handle
            content_id = content.content_id
            scrape_ts = content.scrape_timestamp
            
            self.account_state[account]['last_scrape_ts'] = scrape_ts
            self.account_state[account]['last_content_id'] = content_id
            
            if content_id not in self.account_state[account]['content_history']:
                self.account_state[account]['content_history'][content_id] = []
            self.account_state[account]['content_history'][content_id].append(scrape_ts)
        
        # Write to CSV
        if persisted:
            df = pd.DataFrame(persisted)
            output_file = self.state_dir / f"content_{self.run_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            df.to_csv(output_file, index=False)
            logger.info(f"Persisted {len(persisted)} contents to {output_file}")
            self.stats['persisted'] += len(persisted)
        
        # Persist state
        self._save_account_state()
        self.idempotency_mgr.persist_keys()
    
    def persist_account_metadata(self, metadata: AccountMetadata):
        """Save account metadata."""
        if self.dry_run:
            return
        
        metadata_file = self.state_dir / "account_metadata.csv"
        df = pd.DataFrame([asdict(metadata)])
        
        if metadata_file.exists():
            existing = pd.read_csv(metadata_file)
            df = pd.concat([existing, df], ignore_index=True)
        
        df.to_csv(metadata_file, index=False)
    
    def run(self):
        """
        Main execution loop:
            - Iterate accounts & hashtags
            - Fetch posts, stories, reels with cadence
            - Apply idempotency
            - Persist data
            - Fetch/update account metadata
            - Handle errors with circuit breaker
        """
        logger.info(f"Starting Instagram scraper in {self.run_type} mode")
        logger.info(f"Niche: {self.config.get('niche', 'unknown')}")
        
        accounts = self.config.get('accounts', [])
        content_types = self.config.get('content_types', ['post'])
        
        for account in accounts:
            account_handle = account['handle']
            logger.info(f"Processing account: {account_handle}")
            
            # Fetch account metadata (daily)
            try:
                account_meta = self.circuit_breaker.call(
                    self.fetch_account_metadata,
                    account_handle
                )
                if account_meta:
                    self.persist_account_metadata(account_meta)
            except Exception as e:
                logger.error(f"Failed to fetch account metadata: {e}")
            
            # Fetch each content type
            all_contents = []
            for ctype_str in content_types:
                try:
                    ctype = ContentType(ctype_str)
                    raw_contents = self.circuit_breaker.call(
                        self.fetch_content,
                        account_handle,
                        ctype
                    )
                    
                    # Normalize
                    normalized = [self._normalize_content(c) for c in raw_contents]
                    all_contents.extend(normalized)
                    
                except Exception as e:
                    logger.error(f"Failed to fetch {ctype_str} for {account_handle}: {e}")
            
            # Persist batch
            if all_contents:
                self.persist_content(all_contents)
            
            # Rate limiting between accounts
            time.sleep(2)
        
        # Final stats
        logger.info("=" * 60)
        logger.info("Scrape completed")
        logger.info(f"Fetched: {self.stats['fetched']}")
        logger.info(f"Duplicates: {self.stats['duplicates']}")
        logger.info(f"Persisted: {self.stats['persisted']}")
        logger.info(f"Errors: {self.stats['errors']}")
        logger.info("=" * 60)
        
        return self.stats


# ============================================================================
# CLI & Testing Interface
# ============================================================================

def load_niche_config(niche: str) -> Dict:
    """Load niche config from YAML."""
    # Mock config - replace with actual YAML loading
    return {
        'niche': niche,
        'accounts': [
            {'handle': 'example_account_1'},
            {'handle': 'example_account_2'}
        ],
        'hashtags': ['#trending', '#viral'],
        'content_types': ['post', 'reel'],
        'api_sessions': [
            {'session_id': 'session_1', 'key': 'mock_key_1'},
            {'session_id': 'session_2', 'key': 'mock_key_2'}
        ]
    }


if __name__ == "__main__":
    # Example usage
    import sys
    
    niche = sys.argv[1] if len(sys.argv) > 1 else "tech"
    run_type = sys.argv[2] if len(sys.argv) > 2 else "live"
    dry_run = "--dry-run" in sys.argv
    
    config = load_niche_config(niche)
    
    scraper = InstagramScraper(
        niche_config=config,
        run_type=run_type,
        dry_run=dry_run
    )
    
    stats = scraper.run()
    
    print(f"\n✅ Scraper completed with stats: {stats}")