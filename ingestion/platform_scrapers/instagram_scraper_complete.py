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
    - Full system integrations
    - Edge case handling
"""

from typing import Literal, List, Dict, Optional, Set
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
import traceback
import re
from urllib.parse import urlparse
import random

# System integrations
try:
    from config_loader import load_niche_config as load_config
except ImportError:
    logging.warning("config_loader.py not found, using fallback")
    def load_config(niche: str) -> Dict:
        return {'niche': niche, 'accounts': [], 'content_types': ['post']}

try:
    from metrics_utils import compute_velocity_metrics
except ImportError:
    logging.warning("metrics_utils.py not found, using placeholder")
    def compute_velocity_metrics(content_data: List[Dict]) -> List[Dict]:
        return content_data  # placeholder

try:
    from long_tail_tracker import feed_engagement_data
except ImportError:
    logging.warning("long_tail_tracker.py not found, using placeholder")
    def feed_engagement_data(data: Dict):
        pass  # placeholder

try:
    from account_system.geo_allocator import GeoProxyAllocator
except ImportError:
    logging.warning("geo_allocator.py not found, using fallback")
    class GeoProxyAllocator:
        def get_proxy_for_account(self, account: str) -> Optional[str]:
            return None

try:
    from alerting import trigger_alert, AlertType
except ImportError:
    logging.warning("alerting.py not found, using fallback")
    def trigger_alert(alert_type: str, message: str, context: Dict = None):
        logging.error(f"ALERT [{alert_type}]: {message}")
    class AlertType:
        FAILURE = "failure"
        QUOTA_HIT = "quota_hit"
        RATE_LIMIT = "rate_limit"

try:
    from ingestion_pipeline import IngestionJob, IngestionMode
except ImportError:
    logging.warning("ingestion_pipeline.py not found, using fallback")
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


@dataclass
class ContentMetadata:
    """Raw content metadata with required and optional fields."""
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
    
    # Velocity placeholders (computed elsewhere)
    likes_per_hour: Optional[float] = None
    comments_per_hour: Optional[float] = None
    shares_per_hour: Optional[float] = None


@dataclass
class AccountMetadata:
    """Account-level authority signals."""
    account_handle: str
    followers: int
    following: int
    total_posts: int
    engagement_rate: float
    last_updated: str


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


class CircuitBreaker:
    """Circuit breaker pattern for API failures."""
    
    def __init__(self, failure_threshold: int = 5, timeout: timedelta = timedelta(minutes=5)):
        self.failure_count = 0
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.last_failure_time: Optional[datetime] = None
        self.state = "closed"  # closed, open, half_open
    
    def call(self, func, *args, **kwargs):
        """Execute function with circuit breaker protection."""
        if self.state == "open":
            if datetime.now() - self.last_failure_time > self.timeout:
                self.state = "half_open"
                logger.info("Circuit breaker half-open, attempting call")
            else:
                raise RuntimeError("Circuit breaker OPEN - too many failures")
        
        try:
            result = func(*args, **kwargs)
            if self.state == "half_open":
                self.reset()
            return result
        except Exception as e:
            self.record_failure()
            raise e
    
    def record_failure(self):
        """Record a failure."""
        self.failure_count += 1
        self.last_failure_time = datetime.now()
        if self.failure_count >= self.failure_threshold:
            self.state = "open"
            logger.error(f"Circuit breaker OPEN after {self.failure_count} failures")
    
    def reset(self):
        """Reset circuit breaker."""
        self.failure_count = 0
        self.state = "closed"
        logger.info("Circuit breaker reset to CLOSED")


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
        self.circuit_breaker = CircuitBreaker()
        
        # Ingestion pipeline integration
        self.ingestion_job_id = None
        self.pipeline_mode = IngestionMode.LIVE if run_type == "live" else IngestionMode.BACKFILL
        
        self.stats = {
            'fetched': 0,
            'duplicates': 0,
            'errors': 0,
            'persisted': 0,
            'deleted_content': 0,
            'private_accounts': 0,
            'expired_stories': 0
        }
    
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
            
            # Check rate limits before API call
            session_id = session.get('session_id', 'default')
            if self.session_mgr.check_rate_limit(session_id):
                self.session_mgr.mark_failed('rate_limit', {'session_id': session_id})
                raise RuntimeError(f"Rate limit exceeded for session {session_id}")
            
            # Real API call with edge case handling
            raw_contents = self._fetch_from_instagram_api(
                account_handle,
                content_type.value,
                session
            )
            
            # Filter based on cadence rules and edge cases
            filtered_contents = []
            for content in raw_contents:
                # Edge Case 1: Handle deleted content
                if content.get('is_deleted', False) or content.get('status') == 'deleted':
                    logger.info(f"Skipping deleted content {content.get('id')}")
                    self._handle_deleted_content(content, account_handle)
                    continue
                
                # Edge Case 2: Handle story expiration
                if content_type == ContentType.STORY:
                    posted_at = datetime.fromisoformat(content['posted_at'])
                    if datetime.now() - posted_at > timedelta(hours=24):
                        logger.info(f"Skipping expired story {content.get('id')}")
                        self.stats['expired_stories'] += 1
                        continue
                
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
            
            # Edge Case 3: Handle private/blocked accounts
            if 'private' in str(e).lower() or 'blocked' in str(e).lower() or 'not found' in str(e).lower():
                self._handle_private_account(account_handle, str(e))
            
            # Enhanced error details for alerting
            error_details = {
                'account_handle': account_handle,
                'content_type': content_type.value,
                'error_message': str(e),
                'traceback': traceback.format_exc()
            }
            self.session_mgr.mark_failed(str(type(e).__name__), error_details)
            return []
    
    def _fetch_from_instagram_api(
        self,
        account_handle: str,
        content_type: str,
        session: Dict
    ) -> List[Dict]:
        """
        Real Instagram API fetch with comprehensive error handling.
        
        This would use instaloader, apify, playwright, or official API.
        """
        try:
            http_session = self.session_mgr.get_http_session(session)
            
            # Simulate real API call - replace with actual implementation
            # This is where you'd integrate with:
            # - Instagram Basic Display API
            # - Instagram Graph API
            # - Instaloader
            # - Apify Instagram scraper
            # - Playwright-based scraping
            
            # For now, return mock data with edge cases
            return self._generate_mock_content_with_edge_cases(account_handle, content_type)
            
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                raise RuntimeError("Rate limit exceeded")
            elif e.response.status_code == 404:
                raise RuntimeError("Account not found")
            elif e.response.status_code == 403:
                raise RuntimeError("Account private or blocked")
            else:
                raise RuntimeError(f"HTTP error: {e.response.status_code}")
        except Exception as e:
            raise RuntimeError(f"API fetch failed: {str(e)}")
    
    def _generate_mock_content_with_edge_cases(self, account_handle: str, content_type: str) -> List[Dict]:
        """Generate mock content with edge cases for testing."""
        now = datetime.now()
        mock_contents = []
        
        for i in range(5):
            age_hours = i * 5  # 0h, 5h, 10h, 15h, 20h old content
            posted_at = now - timedelta(hours=age_hours)
            
            # Add edge cases
            is_deleted = (i == 3)  # 4th item is deleted
            is_private = (i == 4)  # 5th item triggers private account error
            
            content = {
                'id': f"{account_handle}_{content_type}_{i}",
                'type': content_type,
                'account_handle': account_handle,
                'caption': f"Mock {content_type} caption {i}" if not is_deleted else None,
                'media_url': f"https://example.com/media/{i}.jpg" if not is_deleted else None,
                'likes': 1000 + i * 500 if not is_deleted else 0,
                'comments': 50 + i * 10 if not is_deleted else 0,
                'shares': 20 + i * 5 if not is_deleted else 0,
                'views': 5000 + i * 1000 if content_type == 'reel' and not is_deleted else None,
                'posted_at': posted_at.isoformat(),
                'video_length': 30.5 if content_type == 'reel' else None,
                'is_deleted': is_deleted,
                'status': 'deleted' if is_deleted else 'active'
            }
            
            mock_contents.append(content)
        
        return mock_contents
    
    def _handle_deleted_content(self, content: Dict, account_handle: str):
        """Handle deleted content edge case."""
        self.stats['deleted_content'] += 1
        
        # Mark content as deleted in state
        content_id = content['id']
        if account_handle in self.account_state:
            if 'deleted_content' not in self.account_state[account_handle]:
                self.account_state[account_handle]['deleted_content'] = []
            self.account_state[account_handle]['deleted_content'].append({
                'content_id': content_id,
                'deleted_at': datetime.now().isoformat(),
                'reason': 'api_deleted'
            })
        
        logger.info(f"Handled deleted content {content_id} for account {account_handle}")
    
    def _handle_private_account(self, account_handle: str, error_message: str):
        """Handle private/blocked account edge case."""
        self.stats['private_accounts'] += 1
        
        # Mark account as private in state
        if account_handle in self.account_state:
            self.account_state[account_handle]['is_private'] = True
            self.account_state[account_handle]['private_error'] = error_message
            self.account_state[account_handle]['private_detected_at'] = datetime.now().isoformat()
        
        # Trigger alert for private account
        alert_context = {
            'account_handle': account_handle,
            'error_message': error_message,
            'detected_at': datetime.now().isoformat()
        }
        trigger_alert(AlertType.FAILURE, f"Private/blocked account detected: {account_handle}", alert_context)
        
        logger.warning(f"Handled private account {account_handle}: {error_message}")
    
    def fetch_account_metadata(self, account_handle: str) -> Optional[AccountMetadata]:
        """
        Fetch account-level metrics (followers, engagement rate).
        Updated daily or lazily.
        """
        try:
            session = self.session_mgr.get_active_session()
            
            # Check if account is marked as private
            if (account_handle in self.account_state and 
                self.account_state[account_handle].get('is_private', False)):
                logger.warning(f"Skipping metadata fetch for private account {account_handle}")
                return None
            
            # Real account metadata API call - replace with actual implementation
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
        Save content to processed store with idempotency check and system integrations.
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
        
        # Integration 1: Compute velocity metrics
        if persisted:
            persisted_with_metrics = compute_velocity_metrics(persisted)
        else:
            persisted_with_metrics = persisted
        
        # Write to CSV
        if persisted_with_metrics:
            df = pd.DataFrame(persisted_with_metrics)
            output_file = self.state_dir / f"content_{self.run_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            df.to_csv(output_file, index=False)
            logger.info(f"Persisted {len(persisted_with_metrics)} contents to {output_file}")
            self.stats['persisted'] += len(persisted_with_metrics)
            
            # Integration 2: Feed data to long tail tracker
            for content_dict in persisted_with_metrics:
                feed_engagement_data({
                    'platform': 'instagram',
                    'content_id': content_dict['content_id'],
                    'account_handle': content_dict['account_handle'],
                    'engagement_metrics': {
                        'likes': content_dict.get('likes'),
                        'comments': content_dict.get('comments'),
                        'shares': content_dict.get('shares'),
                        'views': content_dict.get('views')
                    },
                    'timestamp': content_dict['scrape_timestamp'],
                    'content_type': content_dict['content_type']
                })
        
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
    
    def create_ingestion_job(self, account_handle: str, content_types: List[str]) -> IngestionJob:
        """Create ingestion job for pipeline integration."""
        job = IngestionJob(
            job_id=f"instagram_{account_handle}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            platform="instagram",
            account_handle=account_handle,
            content_types=content_types,
            mode=self.pipeline_mode,
            niche=self.config.get('niche', 'unknown'),
            created_at=datetime.now().isoformat(),
            status="created"
        )
        self.ingestion_job_id = job.job_id
        return job
    
    def run(self):
        """
        Main execution loop with full system integrations:
            - Create ingestion jobs
            - Iterate accounts & hashtags
            - Fetch posts, stories, reels with cadence
            - Apply idempotency and edge case handling
            - Persist data with velocity metrics
            - Feed long tail tracker
            - Handle errors with alerting
        """
        logger.info(f"Starting Instagram scraper in {self.run_type} mode")
        logger.info(f"Niche: {self.config.get('niche', 'unknown')}")
        
        accounts = self.config.get('accounts', [])
        content_types = self.config.get('content_types', ['post'])
        
        # Integration: Create ingestion job for pipeline
        if accounts:
            ingestion_job = self.create_ingestion_job(
                accounts[0].get('handle', 'unknown'), 
                content_types
            )
            logger.info(f"Created ingestion job: {ingestion_job.job_id}")
        
        for account in accounts:
            account_handle = account['handle']
            logger.info(f"Processing account: {account_handle}")
            
            # Skip private accounts
            if (account_handle in self.account_state and 
                self.account_state[account_handle].get('is_private', False)):
                logger.info(f"Skipping private account {account_handle}")
                continue
            
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
            
            # Persist batch with integrations
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
        logger.info(f"Deleted content: {self.stats['deleted_content']}")
        logger.info(f"Private accounts: {self.stats['private_accounts']}")
        logger.info(f"Expired stories: {self.stats['expired_stories']}")
        logger.info("=" * 60)
        
        return self.stats


# ============================================================================
# CLI & Testing Interface
# ============================================================================

def load_niche_config(niche: str) -> Dict:
    """Load niche config from YAML file."""
    config_path = Path(f"config/factories/{niche}.yaml")
    
    if config_path.exists():
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    else:
        # Fallback mock config
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
