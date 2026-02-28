"""
reddit_scraper.py — Full Production Implementation

Purpose: Ingest Reddit content with thread-aware scraping, multi-subreddit coverage,
         velocity tracking, deterministic sampling, and idempotency.
"""

from typing import Literal, List, Dict, Optional, Tuple, Set
import pandas as pd
import time
import hashlib
import os
import json
from datetime import datetime, timedelta
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging
import asyncio
import concurrent.futures
from threading import Lock
import threading
from dataclasses import dataclass
import pyarrow as pa
import pyarrow.parquet as pq

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Schema versioning
SCHEMA_VERSION = "1.2.0"

@dataclass
class CommentSamplingConfig:
    """Configuration for comment re-sampling cadence."""
    initial_depth: int = 10
    revival_threshold_hours: int = 24  # Consider thread revived if comments spike after 24h
    max_resample_age_hours: int = 168  # Don't resample posts older than 7 days
    resample_intervals: List[int] = None  # Hours after creation to resample
    
    def __post_init__(self):
        if self.resample_intervals is None:
            self.resample_intervals = [1, 6, 12, 24, 48, 72, 168]  # Standard cadence

@dataclass
class RequestBudget:
    """Request budgeting per API key."""
    max_requests_per_hour: int = 1000
    max_requests_per_minute: int = 60
    current_hour_requests: int = 0
    current_minute_requests: int = 0
    last_hour_reset: float = 0
    last_minute_reset: float = 0
    lock: Lock = None
    
    def __post_init__(self):
        if self.lock is None:
            self.lock = Lock()

@dataclass
class AlertConfig:
    """Alerting configuration."""
    failure_threshold: int = 5
    failure_window_minutes: int = 10
    rate_limit_threshold: int = 3
    quota_exhaustion_threshold: int = 2


class RedditAPIClient:
    """Handles Reddit API requests with retry logic and key rotation."""
    
    def __init__(self, api_keys: List[str], scraper=None):
        self.api_keys = api_keys
        self.key_index = 0
        self.session = self._create_session()
        self.base_url = "https://oauth.reddit.com"
        self.auth_tokens = {}
        self.failed_keys = {}  # Track cooldown for failed keys
        self.scraper = scraper  # Reference to scraper for budget checking
        
    def _create_session(self) -> requests.Session:
        """Create session with retry strategy."""
        session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session
    
    def _get_active_key(self) -> Optional[str]:
        """Get next available API key, skipping cooldown keys."""
        current_time = time.time()
        attempts = 0
        
        while attempts < len(self.api_keys):
            key = self.api_keys[self.key_index]
            
            # Check if key is in cooldown
            if key in self.failed_keys:
                cooldown_until = self.failed_keys[key]
                if current_time < cooldown_until:
                    logger.warning(f"Key {self.key_index} in cooldown, rotating...")
                    self.key_index = (self.key_index + 1) % len(self.api_keys)
                    attempts += 1
                    continue
                else:
                    # Cooldown expired, remove from failed keys
                    del self.failed_keys[key]
            
            return key
        
        logger.error("All API keys in cooldown!")
        return None
    
    def rotate_key(self, failure: bool = False):
        """Rotate to next API key, optionally marking current as failed."""
        if failure:
            current_key = self.api_keys[self.key_index]
            cooldown_duration = 300  # 5 minutes
            self.failed_keys[current_key] = time.time() + cooldown_duration
            logger.warning(f"Key {self.key_index} failed, cooldown for {cooldown_duration}s")
        
        self.key_index = (self.key_index + 1) % len(self.api_keys)
        logger.info(f"Rotated to key index {self.key_index}")
    
    def _get_auth_token(self, key: str) -> Optional[str]:
        """Get OAuth token for API key, caching for efficiency."""
        if key in self.auth_tokens:
            token_data = self.auth_tokens[key]
            if time.time() < token_data['expires_at']:
                return token_data['token']
        
        # Parse key (format: client_id:client_secret:user_agent)
        try:
            client_id, client_secret, user_agent = key.split(':')
        except ValueError:
            logger.error(f"Invalid API key format: {key}")
            return None
        
        # Request new token
        auth = requests.auth.HTTPBasicAuth(client_id, client_secret)
        data = {'grant_type': 'client_credentials'}
        headers = {'User-Agent': user_agent}
        
        try:
            response = requests.post(
                'https://www.reddit.com/api/v1/access_token',
                auth=auth,
                data=data,
                headers=headers,
                timeout=10
            )
            response.raise_for_status()
            token_info = response.json()
            
            token = token_info['access_token']
            expires_in = token_info.get('expires_in', 3600)
            
            self.auth_tokens[key] = {
                'token': token,
                'expires_at': time.time() + expires_in - 60,  # 60s buffer
                'user_agent': user_agent
            }
            
            return token
        except Exception as e:
            logger.error(f"Failed to get auth token: {e}")
            return None
    
    def request(self, endpoint: str, params: Dict = None) -> Optional[Dict]:
        """
        Make authenticated API request with retry logic and rate limiting.
        
        Args:
            endpoint: Reddit API endpoint (e.g., "/r/python/hot")
            params: Query parameters
            
        Returns:
            API response data or None if failed
        """
        # Check request budget before making request
        if self.scraper and not self.scraper._check_request_budget(self.api_keys[self.key_index]):
            logger.warning(f"Request budget exceeded for key {self.key_index}")
            return None
        
        key = self._get_active_key()
        if not key:
            raise Exception("No available API keys")
        
        token = self._get_auth_token(key)
        if not token:
            self.rotate_key(failure=True)
            return self.request(endpoint, params)  # Retry with next key
        
        user_agent = self.auth_tokens[key]['user_agent']
        headers = {
            'User-Agent': user_agent,
            'Authorization': f'Bearer {token}'
        }
        
        url = f"{self.base_url}{endpoint}"
        
        for attempt in range(3):
            try:
                response = requests.get(url, headers=headers, params=params, timeout=30)
                
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:  # Rate limited
                    retry_after = int(response.headers.get('Retry-After', 60))
                    logger.warning(f"Rate limited, waiting {retry_after}s")
                    time.sleep(retry_after)
                    continue
                else:
                    logger.error(f"API error {response.status_code}: {response.text}")
                    
            except Exception as e:
                logger.error(f"Request failed: {e}")
                
            if attempt < 2:
                time.sleep(2 ** attempt)  # Exponential backoff
        
        return None

    def _check_request_budget(self, key_index: int) -> bool:
        """Check if API key has remaining request budget."""
        budget = RequestBudget()
        current_time = time.time()
        
        with budget.lock:
            # Reset counters if needed
            if current_time - budget.last_hour_reset >= 3600:
                budget.current_hour_requests = 0
                budget.last_hour_reset = current_time
            
            if current_time - budget.last_minute_reset >= 60:
                budget.current_minute_requests = 0
                budget.last_minute_reset = current_time
            
            # Check budget
            if (budget.current_hour_requests >= budget.max_requests_per_hour or
                budget.current_minute_requests >= budget.max_requests_per_minute):
                return False
            
            # Increment counters
            budget.current_hour_requests += 1
            budget.current_minute_requests += 1
            return True


class RedditScraper:
    """
    Full-spec Reddit scraper with deterministic sampling, idempotency,
    backfill/live modes, multi-niche subreddit support, and key rotation.
    """

    def __init__(self, niche_config: Dict, run_type: Literal["live", "backfill"], dry_run: bool = False):
        """
        Initialize scraper.

        Args:
            niche_config: dict with subreddit list, cadence rules, API keys
            run_type: 'live' or 'backfill'
            dry_run: if True, skip persistence (for testing)
        """
        self.config = niche_config
        self.run_type = run_type
        self.dry_run = dry_run
        self.niche = niche_config.get('niche', 'default')
        self.state_dir = Path(f"/data/processed/reddit/{self.niche}")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        
        self.subreddit_state = self._load_subreddit_state()
        api_keys = self._load_api_keys()
        self.api_client = RedditAPIClient(api_keys, scraper=self)
        
        # Idempotency tracking
        self.seen_keys = set()
        self._load_seen_keys()
        
        # Stats tracking
        self.stats = {
            'posts_fetched': 0,
            'comments_fetched': 0,
            'posts_persisted': 0,
            'duplicates_skipped': 0,
            'errors': 0,
            'comment_resamples': 0,
            'thread_revivals': 0,
            'deleted_comments': 0,
            'tombstone_records': 0
        }
        
        # Enhanced configurations
        self.comment_config = CommentSamplingConfig()
        self.request_budgets = {key: RequestBudget() for key in api_keys}
        self.alert_config = AlertConfig()
        
        # Thread revival tracking
        self.comment_history = self._load_comment_history()
        
        # Failure tracking for alerting
        self.failure_history = []
        self.rate_limit_history = []
        
        # Authority metrics versioning with snapshot timestamps
        self.authority_metrics = {
            'version': '1.0',
            'snapshot_timestamp': time.time(),
            'subreddit_authority': {},
            'authority_history': []
        }
        
        # Schema versioning and data integrity checks
        self.schema_version = SCHEMA_VERSION
        self.data_integrity_checks = {
            'required_fields': ['post_id', 'subreddit', 'scrape_timestamp'],
            'field_types': {
                'post_id': str,
                'subreddit': str,
                'scrape_timestamp': (int, float),
                'created_utc': (int, float),
                'score': (int, float),
                'comments': int
            }
        }
        
        # Date partitioning for data organization
        self.date_partitioning_enabled = True
        self.current_date_partition = datetime.utcnow().strftime('%Y-%m-%d')
        
        # Concurrent processing limits
        self.max_concurrent_subreddits = niche_config.get('max_concurrent_subreddits', 3)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_concurrent_subreddits)

    def _load_subreddit_state(self) -> Dict:
        """
        Load last scrape timestamps and post_ids per subreddit.

        Returns:
            Dict[subreddit -> {last_scrape_ts, last_post_id}]
        """
        state_file = self.state_dir / "subreddit_state.json"
        
        if state_file.exists():
            try:
                with open(state_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load subreddit state: {e}")
        
        return {}

    def _save_subreddit_state(self):
        """Persist subreddit state to disk."""
        if self.dry_run:
            return
        
        state_file = self.state_dir / "subreddit_state.json"
        try:
            with open(state_file, 'w') as f:
                json.dump(self.subreddit_state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save subreddit state: {e}")

    def _load_api_keys(self) -> List[str]:
        """
        Load Reddit API keys from config or environment.

        Returns:
            List of keys to rotate (format: client_id:client_secret:user_agent)
        """
        # Try config first
        if 'api_keys' in self.config:
            return self.config['api_keys']
        
        # Fall back to environment
        keys = []
        i = 1
        while True:
            key = os.getenv(f'REDDIT_API_KEY_{i}')
            if not key:
                break
            keys.append(key)
            i += 1
        
        if not keys:
            # Try single key
            key = os.getenv('REDDIT_API_KEY')
            if key:
                keys.append(key)
        
            raise ValueError("No Reddit API keys found in config or environment")
        
        return keys

    def _load_comment_history(self) -> Dict:
        """Load comment history for thread revival detection."""
        history_file = self.state_dir / "comment_history.json"
        
        if history_file.exists():
            try:
                with open(history_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load comment history: {e}")
        
        return {}
    
    def _save_comment_history(self):
        """Persist comment history for thread revival detection."""
        if self.dry_run:
            return
        
        history_file = self.state_dir / "comment_history.json"
        try:
            with open(history_file, 'w') as f:
                json.dump(self.comment_history, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save comment history: {e}")
    
    def _check_request_budget(self, key: str) -> bool:
        """Check if API key has remaining request budget."""
        budget = self.request_budgets[key]
        current_time = time.time()
        
        with budget.lock:
            # Reset counters if needed
            if current_time - budget.last_hour_reset >= 3600:
                budget.current_hour_requests = 0
                budget.last_hour_reset = current_time
            
            if current_time - budget.last_minute_reset >= 60:
                budget.current_minute_requests = 0
                budget.last_minute_reset = current_time
            
            # Check budget
            if (budget.current_hour_requests >= budget.max_requests_per_hour or
                budget.current_minute_requests >= budget.max_requests_per_minute):
                return False
            
            # Increment counters
            budget.current_hour_requests += 1
            budget.current_minute_requests += 1
            return True
    
    def _track_failure(self, failure_type: str, details: Dict):
        """Track failures for alerting."""
        current_time = time.time()
        
        failure_record = {
            'timestamp': current_time,
            'type': failure_type,
            'details': details
        }
        
        self.failure_history.append(failure_record)
        
        # Clean old failures outside window
        window_start = current_time - (self.alert_config.failure_window_minutes * 60)
        self.failure_history = [
            f for f in self.failure_history 
            if f['timestamp'] >= window_start
        ]
        
        # Check alert threshold
        if len(self.failure_history) >= self.alert_config.failure_threshold:
            self._trigger_alert(failure_type, self.failure_history)
    
    def _trigger_alert(self, alert_type: str, failures: List[Dict]):
        """Trigger alert for critical failures."""
        alert_data = {
            'alert_type': alert_type,
            'timestamp': time.time(),
            'niche': self.niche,
            'run_type': self.run_type,
            'failure_count': len(failures),
            'failures': failures[-5:],  # Last 5 failures
        }
        
        logger.critical(f"ALERT TRIGGERED: {alert_type}", extra=alert_data)
        
        # TODO: Integrate with alerting.py system
        # alerting.send_alert(alert_data)

    def _save_seen_keys(self):
        """Persist seen idempotency keys."""
        if self.dry_run:
            return
        
        seen_file = self.state_dir / "seen_keys.json"
        try:
            with open(seen_file, 'w') as f:
                json.dump(list(self.seen_keys), f)
        except Exception as e:
            logger.error(f"Failed to save seen keys: {e}")

    def _load_seen_keys(self):
        """Load previously seen idempotency keys."""
        seen_file = self.state_dir / "seen_keys.json"
        
        if seen_file.exists():
            try:
                with open(seen_file, 'r') as f:
                    self.seen_keys = set(json.load(f))
            except Exception as e:
                logger.error(f"Failed to load seen keys: {e}")

    def _generate_idempotency_key(self, post: Dict, scrape_timestamp: float) -> str:
        """
        Hash-based dedupe key to prevent duplicates.

        Primary: (post_id, scrape_timestamp, run_type)
        Secondary: (post_id, rounded_scrape_window)
        """
        post_id = post['data']['id']
        scrape_window = int(scrape_timestamp // 300)  # 5-minute window
        
        seen_file = self.state_dir / "seen_keys.json"
        try:
            with open(seen_file, 'w') as f:
                json.dump(list(self.seen_keys), f)
        except Exception as e:
            logger.error(f"Failed to save seen keys: {e}")

    def _get_cadence_interval(self, post_age_hours: float) -> int:
        """
        Get sampling interval in seconds based on post age.

        Cadence (deterministic sampling):
            age <= 2h      → every 5–10 min (300-600s)
            2h < age <= 24h → every 30–60 min (1800-3600s)
            1d < age <= 7d → every 6–12h (21600-43200s)
            age > 7d      → every 24–72h (86400-259200s)
        """
        if post_age_hours <= 2:
            return 300  # 5 min
        elif post_age_hours <= 24:
            return 1800  # 30 min
        elif post_age_hours <= 168:  # 7 days
            return 21600  # 6 hours
        else:
            return 86400  # 24 hours

    def _should_scrape_post(self, post: Dict, subreddit: str) -> bool:
        """Determine if post should be scraped based on cadence rules."""
        post_id = post['data']['id']
        created_utc = post['data']['created_utc']
        current_time = time.time()
        post_age_hours = (current_time - created_utc) / 3600
        
        # Get last scrape time for this post
        state_key = f"{subreddit}:{post_id}"
        if state_key in self.subreddit_state:
            last_scrape = self.subreddit_state[state_key].get('last_scrape_ts', 0)
            time_since_scrape = current_time - last_scrape
            
            required_interval = self._get_cadence_interval(post_age_hours)
            
            if time_since_scrape < required_interval:
                return False
        
        return True

    def fetch_posts(self, subreddit: str, limit: int = 100) -> List[Dict]:
        """
        Fetch posts from a subreddit using correct cadence rules.

        Returns:
            List of raw post dicts
        """
        logger.info(f"Fetching posts from r/{subreddit}")
        
        # Get subreddit config
        sub_config = next((s for s in self.config.get('subreddits', []) 
                          if s['name'] == subreddit), {})
        
        post_types = sub_config.get('post_types', ['link', 'text', 'video'])
        
        posts = []
        
        # Fetch from multiple endpoints based on run_type
        endpoints = []
        if self.run_type == 'live':
            endpoints = ['/hot', '/new']
        else:  # backfill
            endpoints = ['/top', '/hot', '/new']
        
        for endpoint in endpoints:
            try:
                data = self.api_client.request(
                    f"/r/{subreddit}{endpoint}",
                    params={'limit': limit}
                )
                
                if not data or 'data' not in data:
                    continue
                
                for post in data['data']['children']:
                    if post['kind'] != 't3':  # Not a post
                        continue
                    
                    # Filter by post type
                    post_hint = post['data'].get('post_hint', 'text')
                    if post_hint not in post_types and 'link' not in post_types:
                        continue
                    
                    # Apply cadence rules
                    if not self._should_scrape_post(post, subreddit):
                        continue
                    
                    posts.append(post)
                
                self.stats['posts_fetched'] += len(posts)
                
            except Exception as e:
                logger.error(f"Failed to fetch {endpoint} posts from r/{subreddit}: {e}")
                self.stats['errors'] += 1
        
        return posts

    def _should_resample_comments(self, post_id: str, post_created_utc: float) -> bool:
        """Determine if comments should be re-sampled based on cadence rules."""
        current_time = time.time()
        post_age_hours = (current_time - post_created_utc) / 3600
        
        # Don't resample posts older than max age
        if post_age_hours > self.comment_config.max_resample_age_hours:
            return False
        
        # Get comment history for this post
        if post_id not in self.comment_history:
            self.comment_history[post_id] = {
                'created_utc': post_created_utc,
                'last_sample_count': 0,
                'sample_timestamps': [],
                'revival_detected': False
            }
        
        post_history = self.comment_history[post_id]
        
        # Check if we're in a resample interval
        for interval_hours in self.comment_config.resample_intervals:
            interval_seconds = interval_hours * 3600
            time_since_creation = current_time - post_created_utc
            
            # Check if we're within this interval window
            if time_since_creation >= interval_seconds and time_since_creation < interval_seconds + 3600:
                # Check if we've already sampled in this interval
                for ts in post_history['sample_timestamps']:
                    if abs(ts - (post_created_utc + interval_seconds)) < 1800:  # 30min window
                        return False
                return True
        
        return False
    
    def _detect_thread_revival(self, post_id: str, current_comment_count: int) -> bool:
        """Detect if thread has revived (significant comment spike after 24h)."""
        if post_id not in self.comment_history:
            return False
        
        post_history = self.comment_history[post_id]
        current_time = time.time()
        post_age_hours = (current_time - post_history['created_utc']) / 3600
        
        # Only check for revival in posts older than 24h
        if post_age_hours < self.comment_config.revival_threshold_hours:
            return False
        
        # Check if we have previous comment counts
        if not post_history['sample_timestamps'] or post_history['last_sample_count'] == 0:
            return False
        
        # Calculate comment growth rate
        previous_samples = len(post_history['sample_timestamps'])
        if previous_samples < 2:
            return False
        
        # Get comment count from 24h ago period
        revival_threshold_time = post_history['created_utc'] + (self.comment_config.revival_threshold_hours * 3600)
        revival_samples = [ts for ts in post_history['sample_timestamps'] if ts >= revival_threshold_time]
        
        if len(revival_samples) < 2:
            return False
        
        # Calculate growth since revival threshold
        growth_rate = (current_comment_count - post_history['last_sample_count']) / max(post_history['last_sample_count'], 1)
        
        # Detect revival: significant growth after 24h
        if growth_rate > 0.5:  # 50% growth after 24h
            if not post_history['revival_detected']:
                post_history['revival_detected'] = True
                self.stats['thread_revivals'] += 1
                logger.info(f"Thread revival detected for post {post_id}: {growth_rate:.1%} growth")
                return True
        
        return False
    
    def fetch_comments_enhanced(self, subreddit: str, post_id: str, post_created_utc: float) -> List[Dict]:
        """Enhanced comment fetching with re-sampling and revival detection."""
        # Check if we should resample comments
        if not self._should_resample_comments(post_id, post_created_utc):
            return []
        
        logger.debug(f"Resampling comments for post {post_id}")
        self.stats['comment_resamples'] += 1
        
        # Fetch comments
        comments = self.fetch_comments(subreddit, post_id)
        
        # Update comment history
        if post_id in self.comment_history:
            self.comment_history[post_id]['last_sample_count'] = len(comments)
            self.comment_history[post_id]['sample_timestamps'].append(time.time())
        
        # Detect thread revival
        self._detect_thread_revival(post_id, len(comments))
        
    def fetch_comments(self, subreddit: str, post_id: str) -> List[Dict]:
        """
        Fetch comments for a given post_id, ensuring full depth is captured.

        Returns:
            List of comment dicts (flattened from tree structure)
        """
        logger.debug(f"Fetching comments for post {post_id}")
        
        try:
            data = self.api_client.request(
                f"/r/{subreddit}/comments/{post_id}",
                params={'limit': 500, 'depth': 10}
            )
            
            if not data or len(data) < 2:
                return []
            
            comments_data = data[1]['data']['children']
            comments = []
            
            def extract_comments(comment_list, depth=0):
                """Recursively extract all comments from tree."""
                for comment in comment_list:
                    if comment['kind'] != 't1':  # Not a comment
                        continue
                    
                    comment_data = comment['data']
                    
                    # Enhanced deleted comment handling with tombstone records
                    author = comment_data.get('author', '[deleted]')
                    if author == '[deleted]':
                        self.stats['deleted_comments'] += 1
                        # Create tombstone record for deleted comments
                        tombstone_record = {
                            'id': comment_data['id'],
                            'post_id': post_id,
                            'author': '[deleted]',
                            'body': '[deleted]',
                            'score': comment_data.get('score', 0),
                            'created_utc': comment_data['created_utc'],
                            'depth': depth,
                            'deleted': True,
                            'tombstone_timestamp': time.time()
                        }
                        comments.append(tombstone_record)
                        self.stats['tombstone_records'] += 1
                    else:
                        comments.append({
                            'id': comment_data['id'],
                            'post_id': post_id,
                            'author': author,
                            'body': comment_data.get('body', ''),
                            'score': comment_data.get('score', 0),
                            'created_utc': comment_data['created_utc'],
                            'depth': depth,
                            'deleted': False
                        })
                    
                    # Recursively process replies
                    if 'replies' in comment_data and comment_data['replies']:
                        if isinstance(comment_data['replies'], dict):
                            reply_children = comment_data['replies']['data']['children']
                            extract_comments(reply_children, depth + 1)
            
            extract_comments(comments_data)
            
            extract_comments(comments_data)
            self.stats['comments_fetched'] += len(comments)
            
            return comments
        
        except Exception as e:
            logger.error(f"Failed to fetch comments for post {post_id}: {e}")
            self.stats['errors'] += 1
            return []

    def fetch_subreddit_metadata(self, subreddit: str) -> Dict:
        """
        Fetch subreddit-level metadata (subscriber count, activity).

        Updated daily or lazily on demand.
        """
        logger.info(f"Fetching metadata for r/{subreddit}")
        
        try:
            data = self.api_client.request(f"/r/{subreddit}/about")
            
            if not data or 'data' not in data:
                return {}
            
            sub_data = data['data']
            
            return {
                'name': sub_data['display_name'],
                'subscribers': sub_data.get('subscribers', 0),
                'active_users': sub_data.get('active_user_count', 0),
                'created_utc': sub_data['created_utc'],
                'public': not sub_data.get('subreddit_type') == 'private',
                'quarantined': sub_data.get('quarantine', False),
                'scraped_at': time.time()
            }
        
        except Exception as e:
            logger.error(f"Failed to fetch metadata for r/{subreddit}: {e}")
            self.stats['errors'] += 1
            return {}

    def _generate_idempotency_key(self, post: Dict, scrape_timestamp: float) -> str:
        """
        Hash-based dedupe key to prevent duplicates.

        Primary: (post_id, scrape_timestamp, run_type)
        Secondary: (post_id, rounded_scrape_window)
        """
        post_id = post['data']['id']
        
        # Round timestamp to nearest hour for windowing
        rounded_ts = int(scrape_timestamp // 3600)
        
        key_string = f"{post_id}_{rounded_ts}_{self.run_type}"
        return hashlib.sha256(key_string.encode()).hexdigest()

    def _extract_post_data(self, post: Dict, subreddit: str, scrape_timestamp: float) -> Dict:
        """Extract structured data from raw post."""
        data = post['data']
        
        return {
            'post_id': data['id'],
            'subreddit': subreddit,
            'author_id': data.get('author', '[deleted]'),
            'scrape_timestamp': scrape_timestamp,
            'created_utc': data['created_utc'],
            'title': data.get('title', ''),
            'text': data.get('selftext', ''),
            'url': data.get('url', ''),
            'media': json.dumps(data.get('media', {})),
            'score': data.get('score', 0),
            'upvotes': data.get('ups', 0),
            'comments': data.get('num_comments', 0),
            'awards': data.get('total_awards_received', 0),
            'post_hint': data.get('post_hint', 'text'),
            'is_video': data.get('is_video', False),
            'over_18': data.get('over_18', False),
            # Placeholders for velocity/acceleration (computed later by metrics_utils)
            'velocity': None,
            'acceleration': None
        }

    def persist_posts(self, posts: List[Dict], subreddit: str):
        """
        Save posts to processed store, applying idempotency check.
        """
        if not posts or self.dry_run:
            if self.dry_run:
                logger.info(f"Dry run: would persist {len(posts)} posts")
            return
        
        scrape_timestamp = time.time()
        valid_posts = []
        
        for post in posts:
            # Generate idempotency key
            idem_key = self._generate_idempotency_key(post, scrape_timestamp)
            
            if idem_key in self.seen_keys:
                self.stats['duplicates_skipped'] += 1
                continue
            
            # Extract structured data
            post_data = self._extract_post_data(post, subreddit, scrape_timestamp)
            
            # Validate required fields (hard requirements)
            required = ['post_id', 'subreddit', 'scrape_timestamp']
            if not all(post_data.get(field) for field in required):
                logger.warning(f"Post missing required fields: {post_data.get('post_id')}")
                continue
            
            # Validate data integrity
            if self._validate_data_integrity(post_data):
                valid_posts.append(post_data)
                self.seen_keys.add(idem_key)
                
                # Update subreddit state
                state_key = f"{subreddit}:{post_data['post_id']}"
                self.subreddit_state[state_key] = {
                    'last_scrape_ts': scrape_timestamp,
                    'last_post_id': post_data['post_id']
                }
        
        if not valid_posts:
            return
        
        # Convert to DataFrame and append to Parquet with date partitioning
        df = pd.DataFrame(valid_posts)
        
        # Add schema version to data
        df['schema_version'] = self.schema_version
        df['authority_metrics_version'] = self.authority_metrics['version']
        
        # Get partitioned file path
        output_file = self._get_partition_path(subreddit, 'posts')
        
        try:
            if output_file.exists():
                existing_df = pd.read_parquet(output_file)
                df = pd.concat([existing_df, df], ignore_index=True)
            
            df.to_parquet(output_file, index=False, engine='pyarrow')
            self.stats['posts_persisted'] += len(valid_posts)
            logger.info(f"Persisted {len(valid_posts)} posts to {output_file}")
        
        except Exception as e:
            logger.error(f"Failed to persist posts: {e}")
            self.stats['errors'] += 1

    def persist_subreddit_metadata(self, metadata: Dict):
        """Save subreddit metadata for RL reward shaping."""
        if not metadata or self.dry_run:
            return
        
        output_file = self.state_dir / "subreddit_metadata.json"
        
        try:
            existing = {}
            if output_file.exists():
                with open(output_file, 'r') as f:
                    existing = json.load(f)
            
            existing[metadata['name']] = metadata
            
            with open(output_file, 'w') as f:
                json.dump(existing, f, indent=2)
        
        except Exception as e:
            logger.error(f"Failed to persist subreddit metadata: {e}")

    def run(self):
        """
        Main execution loop:
            - Rotate API keys if needed
            - Iterate subreddits
            - Fetch posts + comments
            - Persist raw data
            - Fetch/update subreddit metadata
            - Trigger alerts on failures
        """
        logger.info(f"Starting Reddit scraper in {self.run_type} mode for niche: {self.niche}")
        
        subreddits = self.config.get('subreddits', [])
        
        for sub_config in subreddits:
            subreddit = sub_config['name']
            
            try:
                # Fetch subreddit metadata (daily or as needed)
                metadata = self.fetch_subreddit_metadata(subreddit)
                
                # Skip private/quarantined subreddits gracefully
                if metadata.get('quarantined') or not metadata.get('public', True):
                    logger.warning(f"Skipping quarantined/private subreddit: r/{subreddit}")
                    continue
                
                self.persist_subreddit_metadata(metadata)
                
                # Fetch posts
                posts = self.fetch_posts(subreddit, limit=100)
                
                # Fetch comments for each post (optional based on config)
                if sub_config.get('fetch_comments', True):
                    for post in posts:
                        post_id = post['data']['id']
                        comments = self.fetch_comments(subreddit, post_id)
                        # Comments stored separately or aggregated
                        # For now, just tracking stats
                
                # Persist posts
                self.persist_posts(posts, subreddit)
                
                # Rate limiting between subreddits
                time.sleep(2)
            
            except Exception as e:
                logger.error(f"Error processing r/{subreddit}: {e}")
                self.stats['errors'] += 1
                # TODO: Trigger alerting.py on repeated failures
        
        # Save state
        self._save_subreddit_state()
        self._save_seen_keys()
        
        # Log final stats
        logger.info(f"Scraper complete. Stats: {self.stats}")
        
        return self.stats


# Example usage
if __name__ == "__main__":
    # Mock niche config
    niche_config = {
        'niche': 'tech',
        'subreddits': [
            {
                'name': 'technology',
                'post_types': ['link', 'text', 'video'],
                'fetch_comments': True
            },
            {
                'name': 'programming',
                'post_types': ['text', 'link'],
                'fetch_comments': False
            }
        ],
        'api_keys': [
            'client_id_1:client_secret_1:user_agent_1',
            'client_id_2:client_secret_2:user_agent_2'
        ]
    }
    
    # Initialize and run scraper
    scraper = RedditScraper(niche_config, run_type='live', dry_run=False)
    stats = scraper.run()
    
    print(f"\nFinal Statistics:")
    print(f"Posts fetched: {stats['posts_fetched']}")
    print(f"Comments fetched: {stats['comments_fetched']}")
    print(f"Posts persisted: {stats['posts_persisted']}")
    print(f"Duplicates skipped: {stats['duplicates_skipped']}")
    print(f"Errors: {stats['errors']}")