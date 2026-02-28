"""
Instagram Reels / Feed Poster (Production-Grade, Audit-Safe)

This module implements Instagram-specific posting execution that:
- Executes single deterministic posting attempts
- Detects soft suppression signals
- Normalizes failures into global taxonomy
- Preserves audit-grade execution records

CRITICAL: This file does NOT decide whether/when to post.
It ONLY executes, classifies outcomes, and reports truthfully.
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Tuple
from enum import Enum
from datetime import datetime, timedelta

# Base imports (assumed to exist in base_poster.py)
from .common.base_poster import (
    BasePoster,
    PostIntent,
    ExecutionResult,
    PosterFailureType,
    ExecutionMetrics
)
from .common.auth_manager import AuthManager
from .common.platform_session import PlatformSession


logger = logging.getLogger(__name__)


# ============================================================================
# INSTAGRAM INTENT EXTENSION
# ============================================================================

@dataclass(frozen=True)
class InstagramPostIntent(PostIntent):
    """Instagram-specific posting intent with strict validation."""
    
    caption: str
    hashtags: List[str]
    
    media_path: str
    thumbnail_path: Optional[str] = None
    
    is_reel: bool = True
    music_asset_id: Optional[str] = None
    
    share_to_feed: bool = False
    comments_enabled: bool = True
    
    # Instagram-specific constraints
    MAX_CAPTION_LENGTH: int = field(default=2200, init=False, repr=False)
    MAX_HASHTAGS: int = field(default=30, init=False, repr=False)
    MAX_REEL_DURATION_SEC: int = field(default=90, init=False, repr=False)
    MIN_REEL_DURATION_SEC: int = field(default=3, init=False, repr=False)
    
    def validate(self) -> None:
        """Validate Instagram-specific intent invariants."""
        errors = []
        
        # Caption validation
        if not self.caption or not self.caption.strip():
            errors.append("Caption cannot be empty")
        
        if len(self.caption) > self.MAX_CAPTION_LENGTH:
            errors.append(f"Caption exceeds {self.MAX_CAPTION_LENGTH} chars")
        
        # Hashtag validation
        if len(self.hashtags) > self.MAX_HASHTAGS:
            errors.append(f"Hashtag count exceeds {self.MAX_HASHTAGS}")
        
        for tag in self.hashtags:
            if not tag.startswith('#'):
                errors.append(f"Invalid hashtag format: {tag}")
        
        # Media validation
        if not self.media_path:
            errors.append("media_path is required")
        
        # Reel-specific validation
        if self.is_reel:
            if self.music_asset_id and not isinstance(self.music_asset_id, str):
                errors.append("music_asset_id must be string or None")
            
            # Note: Actual duration check would require media inspection
            # This is a placeholder for file-level validation
        
        if errors:
            raise ValueError(f"Instagram intent validation failed: {'; '.join(errors)}")


# ============================================================================
# INSTAGRAM RATE LIMITER
# ============================================================================

class InstagramRateLimiter:
    """Pre-emptive rate limiting to prevent account penalties."""
    
    def __init__(self):
        self.posts_per_hour_limit = 5
        self.posts_per_day_limit = 25
        self.min_post_interval_sec = 180  # 3 minutes
        
        self._hourly_window: List[datetime] = []
        self._daily_window: List[datetime] = []
        self._last_post_time: Optional[datetime] = None
    
    def can_post(self) -> tuple[bool, Optional[str]]:
        """Check if posting is allowed under rate limits."""
        now = datetime.utcnow()
        
        # Clean old entries
        self._clean_windows(now)
        
        # Check minimum interval
        if self._last_post_time:
            elapsed = (now - self._last_post_time).total_seconds()
            if elapsed < self.min_post_interval_sec:
                wait_sec = self.min_post_interval_sec - elapsed
                return False, f"Min interval not met, wait {wait_sec:.0f}s"
        
        # Check hourly limit
        if len(self._hourly_window) >= self.posts_per_hour_limit:
            return False, f"Hourly limit ({self.posts_per_hour_limit}) reached"
        
        # Check daily limit
        if len(self._daily_window) >= self.posts_per_day_limit:
            return False, f"Daily limit ({self.posts_per_day_limit}) reached"
        
        return True, None
    
    def record_post(self) -> None:
        """Record a successful post attempt."""
        now = datetime.utcnow()
        self._hourly_window.append(now)
        self._daily_window.append(now)
        self._last_post_time = now
    
    def _clean_windows(self, now: datetime) -> None:
        """Remove expired entries from rate limit windows."""
        hour_ago = now - timedelta(hours=1)
        day_ago = now - timedelta(days=1)
        
        self._hourly_window = [t for t in self._hourly_window if t > hour_ago]
        self._daily_window = [t for t in self._daily_window if t > day_ago]


# ============================================================================
# INSTAGRAM VISIBILITY PROBE
# ============================================================================

class InstagramVisibilityProbe:
    """Detect soft suppression via visibility checks."""
    
    def __init__(self, session: PlatformSession):
        self.session = session
        self.probe_delay_sec = 120  # Wait 2 minutes before probing
        self.min_expected_impressions = 1  # At least 1 impression expected
    
    def check_visibility(self, media_id: str) -> tuple[bool, Dict[str, Any]]:
        """
        Check if posted content is actually visible/distributed.
        
        Returns:
            (is_visible, probe_data)
        """
        time.sleep(self.probe_delay_sec)
        
        try:
            # Fetch media insights
            insights = self._fetch_insights(media_id)
            
            impressions = insights.get('impressions', 0)
            reach = insights.get('reach', 0)
            is_on_explore = insights.get('is_on_explore', False)
            
            probe_data = {
                'impressions': impressions,
                'reach': reach,
                'is_on_explore': is_on_explore,
                'probe_timestamp': datetime.utcnow().isoformat()
            }
            
            # Visibility criteria
            is_visible = (
                impressions >= self.min_expected_impressions or
                reach > 0 or
                is_on_explore
            )
            
            return is_visible, probe_data
            
        except Exception as e:
            logger.error(f"Visibility probe failed: {e}")
            # Cannot determine visibility = assume suppressed for safety
            return False, {'error': str(e)}
    
    def _fetch_insights(self, media_id: str) -> Dict[str, Any]:
        """Fetch insights from Instagram API."""
        endpoint = f"/v1/media/{media_id}/insights"
        response = self.session.get(endpoint, params={
            'metric': 'impressions,reach,shares'
        })
        
        if response.status_code != 200:
            raise RuntimeError(f"Insights fetch failed: {response.status_code}")
        
        data = response.json()
        return data.get('data', {})


# ============================================================================
# INSTAGRAM ERROR MAPPER
# ============================================================================

class InstagramErrorMapper:
    """Map Instagram-specific errors to global failure taxonomy."""
    
    ERROR_MAP = {
        # Auth errors
        'OAuthException': PosterFailureType.AUTH_ERROR,
        'challenge_required': PosterFailureType.AUTH_ERROR,
        'checkpoint_required': PosterFailureType.AUTH_ERROR,
        
        # Rate limits
        'rate_limit_error': PosterFailureType.RATE_LIMIT,
        'spam_detected': PosterFailureType.RATE_LIMIT,
        'too_many_requests': PosterFailureType.RATE_LIMIT,
        
        # Platform rejection
        'media_rejected': PosterFailureType.PLATFORM_REJECTION,
        'upload_failed': PosterFailureType.PLATFORM_REJECTION,
        'invalid_media_type': PosterFailureType.PLATFORM_REJECTION,
        
        # Network
        'network_error': PosterFailureType.NETWORK_ERROR,
        'timeout': PosterFailureType.NETWORK_ERROR,
        'connection_error': PosterFailureType.NETWORK_ERROR,
    }
    
    @classmethod
    def map_error(cls, error_code: str, error_message: str) -> PosterFailureType:
        """Map Instagram error to standard failure type."""
        
        # Direct mapping
        if error_code in cls.ERROR_MAP:
            return cls.ERROR_MAP[error_code]
        
        # Message-based fallback
        msg_lower = error_message.lower()
        
        if any(term in msg_lower for term in ['auth', 'login', 'credential']):
            return PosterFailureType.AUTH_ERROR
        
        if any(term in msg_lower for term in ['rate', 'limit', 'spam', 'too many']):
            return PosterFailureType.RATE_LIMIT
        
        if any(term in msg_lower for term in ['reject', 'invalid', 'not allowed']):
            return PosterFailureType.PLATFORM_REJECTION
        
        if any(term in msg_lower for term in ['timeout', 'network', 'connection']):
            return PosterFailureType.NETWORK_ERROR
        
        # Unknown error
        return PosterFailureType.UNKNOWN


# ============================================================================
# INSTAGRAM POSTER WATCHDOG
# ============================================================================

class InstagramPosterWatchdog:
    """Monitor and enforce Instagram posting invariants."""
    
    def __init__(self):
        self.shadow_suppression_threshold = 3  # 3 consecutive suppressions
        self.suppression_count = 0
        self.violations: List[str] = []
    
    def check_visibility_probe_executed(self, probe_data: Optional[Dict]) -> None:
        """Ensure visibility probe was actually executed."""
        if probe_data is None:
            self.violations.append("Visibility probe skipped")
            raise RuntimeError("WATCHDOG VIOLATION: Visibility probe must execute")
    
    def check_false_positive_success(
        self,
        result: ExecutionResult,
        probe_passed: bool
    ) -> None:
        """Detect false positive success claims."""
        if result.success and not probe_passed:
            self.violations.append("False positive: claimed success without visibility")
            raise RuntimeError("WATCHDOG VIOLATION: Cannot claim success without visibility")
    
    def record_shadow_suppression(self) -> None:
        """Track shadow suppression events."""
        self.suppression_count += 1
        
        if self.suppression_count >= self.shadow_suppression_threshold:
            logger.critical(
                f"WATCHDOG ALERT: {self.suppression_count} consecutive shadow suppressions"
            )
            # This would trigger account halt in production
    
    def reset_suppression_count(self) -> None:
        """Reset suppression counter on successful visible post."""
        self.suppression_count = 0


# ============================================================================
# INSTAGRAM POSTER (MAIN)
# ============================================================================

class InstagramPoster(BasePoster):
    """Instagram-specific posting execution adapter."""
    
    def __init__(
        self,
        auth_manager: AuthManager,
        session: PlatformSession
    ):
        super().__init__(auth_manager, session)
        self.rate_limiter = InstagramRateLimiter()
        self.visibility_probe = InstagramVisibilityProbe(session)
        self.watchdog = InstagramPosterWatchdog()
        
        # Container processing config
        self.max_processing_polls = 20
        self.processing_poll_interval_sec = 5
        self.processing_timeout_sec = 300  # 5 minutes
    
    def _platform_execute(self, intent: PostIntent) -> ExecutionResult:
        """
        Execute Instagram posting attempt.
        
        This is the ONLY method InstagramPoster implements.
        All failure interpretation deferred to base layer.
        """
        if not isinstance(intent, InstagramPostIntent):
            return ExecutionResult(
                success=False,
                failure_type=PosterFailureType.INVALID_INTENT,
                error_message="Intent must be InstagramPostIntent",
                metadata={}
            )
        
        start_time = time.time()
        metadata: Dict[str, Any] = {
            'platform': 'instagram',
            'is_reel': intent.is_reel,
            'share_to_feed': intent.share_to_feed
        }
        
        try:
            # Validate intent
            intent.validate()
            
            # Pre-emptive rate limit check
            can_post, limit_reason = self.rate_limiter.can_post()
            if not can_post:
                return ExecutionResult(
                    success=False,
                    failure_type=PosterFailureType.RATE_LIMIT,
                    error_message=f"Rate limit: {limit_reason}",
                    metadata=metadata
                )
            
            # Phase 1: Upload media container
            container_id = self._upload_media(intent, metadata)
            metadata['container_id'] = container_id
            
            # Phase 2: Publish container
            media_id = self._publish_container(container_id, intent, metadata)
            metadata['media_id'] = media_id
            
            # Phase 3: Poll processing status
            processing_complete = self._poll_processing_status(
                media_id,
                metadata
            )
            
            if not processing_complete:
                return ExecutionResult(
                    success=False,
                    failure_type=PosterFailureType.UNKNOWN,
                    error_message="Media processing timeout",
                    metadata=metadata,
                    metrics=ExecutionMetrics(
                        duration_ms=int((time.time() - start_time) * 1000)
                    )
                )
            
            # Phase 4: Visibility probe (MANDATORY)
            visibility_passed, probe_data = self._detect_soft_failure(media_id)
            metadata['visibility_probe'] = probe_data
            metadata['visibility_probe_passed'] = visibility_passed
            
            # Watchdog: ensure probe executed
            self.watchdog.check_visibility_probe_executed(probe_data)
            
            # Determine final result
            if visibility_passed:
                self.rate_limiter.record_post()
                self.watchdog.reset_suppression_count()
                
                result = ExecutionResult(
                    success=True,
                    post_id=media_id,
                    metadata=metadata,
                    metrics=ExecutionMetrics(
                        duration_ms=int((time.time() - start_time) * 1000)
                    )
                )
            else:
                # Shadow suppression detected
                self.watchdog.record_shadow_suppression()
                
                result = ExecutionResult(
                    success=False,
                    failure_type=PosterFailureType.SHADOW_SUPPRESSION,
                    error_message="Content posted but not distributed",
                    metadata=metadata,
                    metrics=ExecutionMetrics(
                        duration_ms=int((time.time() - start_time) * 1000)
                    )
                )
            
            # Watchdog: prevent false positive success
            self.watchdog.check_false_positive_success(result, visibility_passed)
            
            return result
            
        except ValueError as e:
            # Intent validation failure
            return ExecutionResult(
                success=False,
                failure_type=PosterFailureType.INVALID_INTENT,
                error_message=str(e),
                metadata=metadata
            )
            
        except Exception as e:
            # Map Instagram errors to standard taxonomy
            failure_type = InstagramErrorMapper.map_error(
                error_code=getattr(e, 'code', ''),
                error_message=str(e)
            )
            
            return ExecutionResult(
                success=False,
                failure_type=failure_type,
                error_message=str(e),
                metadata=metadata,
                metrics=ExecutionMetrics(
                    duration_ms=int((time.time() - start_time) * 1000)
                )
            )
    
    def _upload_media(
        self,
        intent: InstagramPostIntent,
        metadata: Dict[str, Any]
    ) -> str:
        """
        Phase 1: Upload media and create container.
        
        Returns:
            container_id for subsequent publish
        """
        upload_start = time.time()
        
        endpoint = "/v1/media/upload"
        
        # Prepare upload payload
        payload = {
            'caption': intent.caption,
            'media_type': 'REELS' if intent.is_reel else 'FEED',
        }
        
        if intent.is_reel and intent.music_asset_id:
            payload['audio_name'] = intent.music_asset_id
        
        # Upload media file
        with open(intent.media_path, 'rb') as media_file:
            files = {'file': media_file}
            
            if intent.thumbnail_path:
                with open(intent.thumbnail_path, 'rb') as thumb_file:
                    files['thumbnail'] = thumb_file
                    response = self.session.post(endpoint, data=payload, files=files)
            else:
                response = self.session.post(endpoint, data=payload, files=files)
        
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Upload failed: {response.status_code} {response.text}")
        
        data = response.json()
        container_id = data.get('container_id')
        
        if not container_id:
            raise RuntimeError("No container_id returned from upload")
        
        metadata['upload_latency_ms'] = int((time.time() - upload_start) * 1000)
        
        return container_id
    
    def _publish_container(
        self,
        container_id: str,
        intent: InstagramPostIntent,
        metadata: Dict[str, Any]
    ) -> str:
        """
        Phase 2: Publish the uploaded container.
        
        Returns:
            media_id of published content
        """
        publish_start = time.time()
        
        endpoint = "/v1/media/publish"
        payload = {
            'creation_id': container_id,
            'share_to_feed': intent.share_to_feed,
            'comments_disabled': not intent.comments_enabled
        }
        
        response = self.session.post(endpoint, json=payload)
        
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Publish failed: {response.status_code} {response.text}")
        
        data = response.json()
        media_id = data.get('id')
        
        if not media_id:
            raise RuntimeError("No media_id returned from publish")
        
        metadata['publish_latency_ms'] = int((time.time() - publish_start) * 1000)
        
        return media_id
    
    def _poll_processing_status(
        self,
        media_id: str,
        metadata: Dict[str, Any]
    ) -> bool:
        """
        Phase 3: Poll for media processing completion.
        
        Returns:
            True if processing complete, False if timeout
        """
        poll_start = time.time()
        
        for attempt in range(self.max_processing_polls):
            elapsed = time.time() - poll_start
            
            if elapsed > self.processing_timeout_sec:
                metadata['processing_timeout'] = True
                return False
            
            # Check status
            endpoint = f"/v1/media/{media_id}/status"
            response = self.session.get(endpoint)
            
            if response.status_code != 200:
                logger.warning(f"Status check failed: {response.status_code}")
                time.sleep(self.processing_poll_interval_sec)
                continue
            
            data = response.json()
            status = data.get('status', '')
            
            if status == 'FINISHED':
                metadata['processing_latency_ms'] = int((time.time() - poll_start) * 1000)
                return True
            
            if status == 'ERROR':
                raise RuntimeError(f"Media processing error: {data.get('error_message')}")
            
            # Still processing, wait and retry
            time.sleep(self.processing_poll_interval_sec)
        
        metadata['processing_timeout'] = True
        return False
    
    def _detect_soft_failure(self, media_id: str) -> tuple[bool, Dict[str, Any]]:
        """
        Phase 4: Detect shadow suppression via visibility probe.
        
        Returns:
            (visibility_passed, probe_data)
        """
        return self.visibility_probe.check_visibility(media_id)


# ============================================================================
# MODULE CONSTANTS
# ============================================================================

__version__ = "1.0.0"
__author__ = "Production Systems Team"

# Expose primary interface
__all__ = [
    'InstagramPoster',
    'InstagramPostIntent',
    'InstagramRateLimiter',
    'InstagramVisibilityProbe',
    'InstagramErrorMapper',
    'InstagramPosterWatchdog'
]