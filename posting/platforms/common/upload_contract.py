"""
/platforms/common/upload_contract.py
/common/upload_contract.py

Canonical upload interface for cross-platform content posting.

Tier-0 Critical: Enforces consistent contract for all poster modules,
ensuring predictable behavior for state management, idempotency,
reconciliation, and anomaly detection.

Purpose:
    Enforce a consistent, platform-agnostic contract for uploads so that all
    posters behave predictably, and downstream state, telemetry, and anomaly
    detection systems remain accurate.

    This file decouples poster modules from platform-specific quirks and ensures
    Tier-0 guarantees like idempotency, retry correctness, and telemetry alignment.

What This File Is Not:
    ❌ Not a dispatcher — it defines the interface, but does not execute posts
    ❌ Not a platform API adapter — the implementation lives in _poster.py modules
    ❌ Not a retry engine — the dispatcher and posting_state_store handle retries
    ❌ Not telemetry — it emits standard outputs for metrics, but metrics logic is elsewhere

Tier-0 Role:
    - Ensures all poster modules implement the same expected behavior
    - Guarantees consistent output for posting_state_store.py, idempotency.py, reconciliation.py
    - Prevents platform-specific quirks from breaking downstream logic
    - Makes cross-platform monitoring and anomaly detection reliable

    Without it, each poster could return inconsistent results, corrupt state logs,
    or misinform kill switches.

Core Responsibilities:
    1. Standardized Upload API
       - Defines required fields for every post (content, metadata, visibility, scheduling)
    
    2. Consistent Return Values
       - Every upload must return a normalized UploadResult object
    
    3. Retry and Failure Semantics
       - Standardized distinction between retryable and terminal failures
    
    4. Extensibility
       - Optional fields handled consistently
       - New platforms can implement without changing core pipeline
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Protocol, Union
from datetime import datetime
from enum import Enum
import logging

logger = logging.getLogger(__name__)


# ============================================================================
# CONSTANTS
# ============================================================================

# Common error codes for consistent classification
class ErrorCode:
    """Standard error codes for cross-platform error classification."""
    # Network/Infrastructure
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    
    # Rate Limiting
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    
    # Authentication
    AUTH_FAILED = "AUTH_FAILED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    ACCOUNT_SUSPENDED = "ACCOUNT_SUSPENDED"
    
    # Content Validation
    INVALID_CONTENT_FORMAT = "INVALID_CONTENT_FORMAT"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    CONTENT_POLICY_VIOLATION = "CONTENT_POLICY_VIOLATION"
    
    # Metadata
    INVALID_METADATA = "INVALID_METADATA"
    TITLE_TOO_LONG = "TITLE_TOO_LONG"
    DESCRIPTION_TOO_LONG = "DESCRIPTION_TOO_LONG"
    INVALID_VISIBILITY = "INVALID_VISIBILITY"
    
    # Platform-Specific (400-level)
    BAD_REQUEST = "BAD_REQUEST"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    
    # Unknown
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


# ============================================================================
# ERROR HIERARCHY
# ============================================================================

class UploadError(Exception):
    """
    Base exception for all upload-related errors.
    
    All platform-specific exceptions should be converted to this hierarchy
    for consistent downstream handling.
    """
    
    def __init__(
        self,
        message: str,
        platform: Optional[str] = None,
        error_code: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ):
        self.message = message
        self.platform = platform
        self.error_code = error_code or ErrorCode.UNKNOWN_ERROR
        self.metadata = metadata or {}
        super().__init__(self.message)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize error for logging and state store."""
        return {
            "message": self.message,
            "platform": self.platform,
            "error_code": self.error_code,
            "metadata": self.metadata,
            "error_type": self.__class__.__name__
        }


class RetryableUploadError(UploadError):
    """
    Indicates a temporary failure that can be retried.
    
    These errors signal to the dispatcher that the upload should be retried
    after a delay. The dispatcher and posting_state_store handle retry logic.
    
    Examples:
        - Network timeouts
        - Rate limiting (429)
        - Temporary server errors (503, 502, 504)
        - Platform maintenance windows
        - Temporary service unavailability
    
    Attributes:
        retry_after: Recommended seconds to wait before retry (None = use default)
    """
    
    def __init__(
        self,
        message: str,
        platform: Optional[str] = None,
        retry_after: Optional[int] = None,
        error_code: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ):
        super().__init__(message, platform, error_code, metadata)
        self.retry_after = retry_after  # Seconds to wait before retry
    
    @classmethod
    def from_rate_limit(
        cls,
        platform: str,
        retry_after: int,
        message: Optional[str] = None
    ) -> "RetryableUploadError":
        """Factory for rate limit errors."""
        return cls(
            message or f"Rate limit exceeded on {platform}",
            platform=platform,
            retry_after=retry_after,
            error_code=ErrorCode.RATE_LIMIT_EXCEEDED
        )
    
    @classmethod
    def from_network_error(
        cls,
        platform: str,
        message: Optional[str] = None
    ) -> "RetryableUploadError":
        """Factory for network-related errors."""
        return cls(
            message or f"Network error on {platform}",
            platform=platform,
            error_code=ErrorCode.NETWORK_ERROR
        )


class TerminalUploadError(UploadError):
    """
    Indicates a permanent failure that should not be retried.
    
    These errors signal to the dispatcher that the upload has failed permanently
    and should be sent to dead-letter queue or reported directly.
    
    Examples:
        - Invalid credentials (401, 403)
        - Content policy violations
        - File format not supported
        - Account suspended or banned
        - Invalid metadata (malformed request that won't succeed on retry)
        - Authentication failures
    
    Attributes:
        error_code: Platform-specific or standardized error code for classification
    """
    
    def __init__(
        self,
        message: str,
        platform: Optional[str] = None,
        error_code: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ):
        super().__init__(message, platform, error_code, metadata)
    
    @classmethod
    def from_auth_failure(
        cls,
        platform: str,
        message: Optional[str] = None
    ) -> "TerminalUploadError":
        """Factory for authentication failures."""
        return cls(
            message or f"Authentication failed on {platform}",
            platform=platform,
            error_code=ErrorCode.AUTH_FAILED
        )
    
    @classmethod
    def from_policy_violation(
        cls,
        platform: str,
        violation_reason: str,
        message: Optional[str] = None
    ) -> "TerminalUploadError":
        """Factory for content policy violations."""
        return cls(
            message or f"Content policy violation on {platform}: {violation_reason}",
            platform=platform,
            error_code=ErrorCode.CONTENT_POLICY_VIOLATION,
            metadata={"violation_reason": violation_reason}
        )
    
    @classmethod
    def from_invalid_content(
        cls,
        platform: str,
        reason: str,
        message: Optional[str] = None
    ) -> "TerminalUploadError":
        """Factory for invalid content format/size errors."""
        return cls(
            message or f"Invalid content for {platform}: {reason}",
            platform=platform,
            error_code=ErrorCode.INVALID_CONTENT_FORMAT,
            metadata={"reason": reason}
        )


# ============================================================================
# UPLOAD RESULT
# ============================================================================

class UploadStatus(Enum):
    """Normalized upload outcome states."""
    SUCCESS = "success"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    PENDING = "pending"  # For async/scheduled uploads


@dataclass
class UploadResult:
    """
    Standardized output from any platform upload operation.
    
    All poster modules must return this exact structure to ensure
    consistent handling by state store, idempotency, and reconciliation.
    """
    
    # Core fields (required)
    success: bool
    status: UploadStatus
    platform: str
    attempt_number: int
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    # Success fields
    remote_post_id: Optional[str] = None  # Platform's unique post ID
    remote_url: Optional[str] = None      # Direct link to posted content
    
    # Failure fields
    error_code: Optional[str] = None      # Platform-specific error code
    error_message: Optional[str] = None   # Human-readable error
    
    # Metadata (extensible)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # Retry context
    retry_after: Optional[int] = None     # Seconds until retry allowed
    is_retryable: bool = False
    
    def __post_init__(self):
        """
        Validate result consistency.
        
        Ensures UploadResult maintains invariants required by downstream systems.
        """
        # Success invariants
        if self.success and not self.remote_post_id:
            raise ValueError(
                "Successful uploads must have remote_post_id. "
                f"Platform: {self.platform}, Attempt: {self.attempt_number}"
            )
        
        if self.success and self.status != UploadStatus.SUCCESS:
            raise ValueError(
                f"Successful uploads must have SUCCESS status. "
                f"Got: {self.status.value}, Platform: {self.platform}"
            )
        
        # Failure invariants
        if not self.success and self.status == UploadStatus.SUCCESS:
            raise ValueError(
                f"Failed uploads cannot have SUCCESS status. "
                f"Platform: {self.platform}, Attempt: {self.attempt_number}"
            )
        
        # Retryability invariants
        if self.is_retryable and self.status == UploadStatus.FAILED_TERMINAL:
            raise ValueError(
                f"Terminal failures cannot be retryable. "
                f"Platform: {self.platform}, Attempt: {self.attempt_number}"
            )
        
        if not self.is_retryable and self.status == UploadStatus.FAILED_RETRYABLE:
            raise ValueError(
                f"Retryable failures must have is_retryable=True. "
                f"Platform: {self.platform}, Attempt: {self.attempt_number}"
            )
        
        # Platform requirement
        if not self.platform:
            raise ValueError("Platform identifier is required")
        
        # Attempt number requirement
        if self.attempt_number < 1:
            raise ValueError(f"Attempt number must be >= 1, got {self.attempt_number}")
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize for state store and logging.
        
        Returns a dictionary representation suitable for:
        - posting_state_store.py persistence
        - idempotency.py state tracking
        - reconciliation.py evidence gathering
        - anomaly_detector.py pattern analysis
        """
        result = {
            "success": self.success,
            "status": self.status.value,
            "platform": self.platform,
            "attempt_number": self.attempt_number,
            "timestamp": self.timestamp.isoformat(),
            "remote_post_id": self.remote_post_id,
            "remote_url": self.remote_url,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "metadata": self.metadata,
            "retry_after": self.retry_after,
            "is_retryable": self.is_retryable,
        }
        # Remove None values for cleaner serialization
        return {k: v for k, v in result.items() if v is not None}
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UploadResult":
        """Deserialize from state store."""
        # Convert timestamp string back to datetime
        if "timestamp" in data and isinstance(data["timestamp"], str):
            data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        
        # Convert status string back to enum
        if "status" in data and isinstance(data["status"], str):
            data["status"] = UploadStatus(data["status"])
        
        return cls(**data)
    
    @classmethod
    def success_result(
        cls,
        platform: str,
        remote_post_id: str,
        attempt_number: int,
        remote_url: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> "UploadResult":
        """Factory for successful uploads."""
        return cls(
            success=True,
            status=UploadStatus.SUCCESS,
            platform=platform,
            attempt_number=attempt_number,
            remote_post_id=remote_post_id,
            remote_url=remote_url,
            metadata=metadata or {},
            is_retryable=False
        )
    
    @classmethod
    def retryable_failure(
        cls,
        platform: str,
        attempt_number: int,
        error_message: str,
        error_code: Optional[str] = None,
        retry_after: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> "UploadResult":
        """Factory for retryable failures."""
        return cls(
            success=False,
            status=UploadStatus.FAILED_RETRYABLE,
            platform=platform,
            attempt_number=attempt_number,
            error_code=error_code,
            error_message=error_message,
            retry_after=retry_after,
            metadata=metadata or {},
            is_retryable=True
        )
    
    @classmethod
    def terminal_failure(
        cls,
        platform: str,
        attempt_number: int,
        error_message: str,
        error_code: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> "UploadResult":
        """Factory for terminal failures."""
        return cls(
            success=False,
            status=UploadStatus.FAILED_TERMINAL,
            platform=platform,
            attempt_number=attempt_number,
            error_code=error_code,
            error_message=error_message,
            metadata=metadata or {},
            is_retryable=False
        )


# ============================================================================
# CONTENT & METADATA CONTRACTS
# ============================================================================

@dataclass
class UploadContent:
    """
    Standardized content package for uploads.
    
    Platform-specific fields should go in optional_fields dict.
    """
    
    # Primary content
    file_path: str              # Path to video/image file
    content_type: str           # "video", "image", "text"
    
    # Standard metadata
    title: str
    description: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    
    # Visibility settings
    visibility: str = "public"  # "public", "unlisted", "private"
    
    # Scheduling
    scheduled_time: Optional[datetime] = None
    
    # Optional attachments
    thumbnail_path: Optional[str] = None
    subtitle_path: Optional[str] = None
    
    # Platform-specific extensions
    optional_fields: Dict[str, Any] = field(default_factory=dict)
    
    def validate_basic(self) -> bool:
        """
        Basic validation all platforms should enforce.
        
        Raises:
            ValueError: If required fields are missing or invalid
        
        Returns:
            True if valid
        """
        if not self.file_path or not self.file_path.strip():
            raise ValueError("file_path is required and cannot be empty")
        
        if not self.title or len(self.title.strip()) == 0:
            raise ValueError("title is required and cannot be empty")
        
        if self.visibility not in ["public", "unlisted", "private"]:
            raise ValueError(
                f"Invalid visibility: {self.visibility}. "
                "Must be one of: public, unlisted, private"
            )
        
        if self.content_type not in ["video", "image", "text"]:
            raise ValueError(
                f"Invalid content_type: {self.content_type}. "
                "Must be one of: video, image, text"
            )
        
        return True
    
    def normalize_metadata(self) -> "UploadContent":
        """
        Normalize metadata fields for consistency.
        
        - Trims whitespace from title and description
        - Normalizes tags (lowercase, deduplicate, trim)
        - Ensures visibility is lowercase
        """
        normalized = UploadContent(
            file_path=self.file_path,
            content_type=self.content_type,
            title=self.title.strip() if self.title else "",
            description=self.description.strip() if self.description else None,
            tags=list(set(tag.strip().lower() for tag in self.tags if tag.strip())),
            visibility=self.visibility.lower(),
            scheduled_time=self.scheduled_time,
            thumbnail_path=self.thumbnail_path,
            subtitle_path=self.subtitle_path,
            optional_fields=self.optional_fields.copy()
        )
        return normalized


# ============================================================================
# BASE UPLOADER (ABSTRACT CONTRACT)
# ============================================================================

class BaseUploader(ABC):
    """
    Abstract base class that all platform poster modules must implement.
    
    Enforces consistent behavior across YouTube, TikTok, Instagram, etc.
    This is the contract that ensures all posters behave identically from the
    dispatcher and state store perspective.
    
    Implementation Pattern:
        class YouTubePoster(BaseUploader):
            def __init__(self, config):
                super().__init__("youtube", config)
                self.api_client = YouTubeAPIClient(config)
            
            def upload(self, content, attempt_number=1, **kwargs):
                # 1. Validate content
                self.validate_content(content)
                
                # 2. Preprocess metadata
                content = self.preprocess_metadata(content)
                
                # 3. Call platform API
                try:
                    remote_id = self.api_client.upload_video(content.file_path, {
                        "title": content.title,
                        "description": content.description,
                        "tags": content.tags,
                        "visibility": content.visibility
                    })
                    
                    # 4. Return success result
                    result = UploadResult.success_result(
                        platform=self.platform_name,
                        remote_post_id=remote_id,
                        attempt_number=attempt_number
                    )
                    
                except TemporaryPlatformError as e:
                    # 5. Raise retryable error for temporary failures
                    raise RetryableUploadError(
                        str(e),
                        platform=self.platform_name,
                        retry_after=e.retry_after if hasattr(e, 'retry_after') else None
                    )
                
                except PermanentPlatformError as e:
                    # 6. Raise terminal error for permanent failures
                    raise TerminalUploadError(
                        str(e),
                        platform=self.platform_name,
                        error_code=getattr(e, 'error_code', None)
                    )
                
                # 7. Optional post-upload hook
                self.post_upload_hook(result)
                
                return result
    
    Critical Guarantees:
        - All implementations return identical UploadResult structure
        - Error classification is consistent (retryable vs terminal)
        - Validation and preprocessing hooks are standardized
        - Downstream systems can rely on predictable behavior
    """
    
    def __init__(self, platform_name: str, config: Optional[Dict[str, Any]] = None):
        """
        Initialize uploader.
        
        Args:
            platform_name: Identifier for this platform (e.g., "youtube", "tiktok")
            config: Platform-specific configuration dictionary
        """
        if not platform_name or not platform_name.strip():
            raise ValueError("platform_name is required and cannot be empty")
        
        self.platform_name = platform_name.lower().strip()
        self.config = config or {}
        self._logger = logging.getLogger(f"{__name__}.{self.platform_name}")
    
    @abstractmethod
    def upload(
        self,
        content: UploadContent,
        attempt_number: int = 1,
        **kwargs
    ) -> UploadResult:
        """
        Execute upload to platform.
        
        This is the core method that all poster implementations must provide.
        It performs the actual upload operation and returns a standardized result.
        
        CRITICAL REQUIREMENTS:
        1. MUST return normalized UploadResult (never None or platform-specific types)
        2. MUST raise RetryableUploadError for temporary failures (network, rate limits, etc.)
        3. MUST raise TerminalUploadError for permanent failures (auth, policy violations, etc.)
        4. MUST include attempt_number in the result
        5. MUST include remote_post_id on success
        6. MUST handle all exceptions and convert to UploadError hierarchy
        
        Implementation Checklist:
        □ Validate content (use validate_content() or custom logic)
        □ Preprocess metadata (use preprocess_metadata() or custom logic)
        □ Call platform API with error handling
        □ Convert platform response to UploadResult
        □ Call post_upload_hook() with result
        □ Return UploadResult
        
        Args:
            content: Standardized content package (UploadContent)
            attempt_number: Current retry attempt (1-indexed, from dispatcher)
            **kwargs: Platform-specific overrides (use sparingly, document well)
        
        Returns:
            UploadResult: Normalized result with success/failure details
        
        Raises:
            RetryableUploadError: Temporary failure that dispatcher should retry
                - Network timeouts, rate limits, service unavailable
                - Include retry_after if platform provides it
            
            TerminalUploadError: Permanent failure that should not be retried
                - Invalid credentials, content policy violations
                - Unsupported formats, account suspension
            
            ValueError: If content validation fails (should be caught and converted)
        
        Example:
            try:
                # Validate
                if not self.validate_content(content):
                    raise TerminalUploadError("Content validation failed", ...)
                
                # Preprocess
                content = self.preprocess_metadata(content)
                
                # Upload
                remote_id = await self._upload_to_platform(content)
                
                # Success
                result = UploadResult.success_result(
                    platform=self.platform_name,
                    remote_post_id=remote_id,
                    attempt_number=attempt_number
                )
                
            except RateLimitError as e:
                raise RetryableUploadError.from_rate_limit(
                    self.platform_name,
                    retry_after=e.retry_after
                )
            
            except AuthError as e:
                raise TerminalUploadError.from_auth_failure(
                    self.platform_name,
                    str(e)
                )
            
            except Exception as e:
                # Unknown error - default to retryable with caution
                self._logger.warning(f"Unexpected error: {e}")
                raise RetryableUploadError(
                    f"Unexpected error during upload: {str(e)}",
                    platform=self.platform_name
                )
            
            finally:
                # Post-upload hook
                if 'result' in locals():
                    self.post_upload_hook(result)
                else:
                    # Create failure result for hook
                    # (if exception wasn't converted)
                    pass
            
            return result
        """
        pass
    
    def validate_content(self, content: UploadContent) -> bool:
        """
        Pre-upload validation hook.
        
        Override to add platform-specific validation rules beyond basic checks.
        Default implementation validates basic required fields via content.validate_basic().
        
        Platform implementations should:
        - Call super().validate_content(content) first
        - Add platform-specific checks (file size, format, metadata limits)
        - Raise ValueError or TerminalUploadError for violations
        
        Args:
            content: Content to validate
        
        Returns:
            True if valid (otherwise raises exception)
        
        Raises:
            ValueError: If content fails basic validation
            TerminalUploadError: If content fails platform-specific validation
                (recommended for better error classification)
        
        Example Override:
            def validate_content(self, content: UploadContent) -> bool:
                # Basic validation first
                super().validate_content(content)
                
                # Platform-specific: check file size
                limits = self.get_platform_limits()
                file_size_mb = os.path.getsize(content.file_path) / (1024 * 1024)
                if limits.get("max_file_size_mb") and file_size_mb > limits["max_file_size_mb"]:
                    raise TerminalUploadError.from_invalid_content(
                        self.platform_name,
                        f"File size {file_size_mb:.1f}MB exceeds limit {limits['max_file_size_mb']}MB"
                    )
                
                # Platform-specific: check title length
                if len(content.title) > limits.get("max_title_length", float('inf')):
                    raise TerminalUploadError(
                        f"Title too long: {len(content.title)} chars (max {limits['max_title_length']})",
                        platform=self.platform_name,
                        error_code=ErrorCode.TITLE_TOO_LONG
                    )
                
                return True
        """
        # Basic validation
        content.validate_basic()
        
        # Subclasses can add platform-specific validation
        return True
    
    def preprocess_metadata(self, content: UploadContent) -> UploadContent:
        """
        Pre-upload metadata transformation hook.
        
        Override to normalize platform-specific fields before upload.
        This hook runs AFTER validate_content() but BEFORE the actual upload.
        
        Common use cases:
        - Tag normalization (formatting, deduplication, limits)
        - Title/description truncation to platform limits
        - Character encoding normalization
        - URL/normalization of relative paths
        - Default value injection for optional fields
        
        Args:
            content: Original content object
        
        Returns:
            UploadContent: Processed content (can be same object or new instance)
        
        Example Override:
            def preprocess_metadata(self, content: UploadContent) -> UploadContent:
                # Call base normalization
                content = content.normalize_metadata()
                
                # Platform-specific: truncate title to 100 chars
                limits = self.get_platform_limits()
                if len(content.title) > limits.get("max_title_length", 100):
                    content.title = content.title[:97] + "..."
                    self._logger.info(f"Truncated title to {len(content.title)} chars")
                
                # Platform-specific: limit tags to 5
                if len(content.tags) > 5:
                    content.tags = content.tags[:5]
                    self._logger.info("Limited tags to 5 items")
                
                # Platform-specific: add default category
                if "category" not in content.optional_fields:
                    content.optional_fields["category"] = "Entertainment"
                
        return content
        
        Note:
            This method should be idempotent - calling it multiple times should
            produce the same result. Do not mutate the original object unless
            you're sure no other code references it.
        """
        # Default: just normalize basic metadata
        return content.normalize_metadata()
    
    def post_upload_hook(self, result: UploadResult) -> None:
        """
        Post-upload processing hook.
        
        Called after upload completes (success or failure). Use for:
        - Platform-specific logging/metrics
        - Triggering anomaly detection signals
        - Updating internal caches/state
        - Emitting telemetry events
        
        This hook should NOT:
        - Modify the UploadResult (it's already returned)
        - Raise exceptions (they will be logged but won't affect result)
        - Block for extended periods (use async operations if needed)
        
        Args:
            result: Final upload outcome (UploadResult)
        
        Example Override:
            def post_upload_hook(self, result: UploadResult) -> None:
                # Platform-specific metrics
                if result.success:
                    self._metrics.increment("uploads.success", tags={"platform": self.platform_name})
                    self._logger.info(f"Upload successful: {result.remote_post_id}")
                else:
                    self._metrics.increment("uploads.failure", tags={
                        "platform": self.platform_name,
                        "error_code": result.error_code or "unknown"
                    })
                
                # Trigger anomaly detection for failures
                if not result.success and result.is_retryable:
                    self._anomaly_detector.record_retryable_failure(
                        platform=self.platform_name,
                        error_code=result.error_code
                    )
        """
        # Default: no-op (platforms can override)
        pass
    
    def get_platform_limits(self) -> Dict[str, Any]:
        """
        Platform-specific limits and constraints.
        
        Override to provide metadata for validation and anomaly detection.
        
        Returns:
            Dict with keys like:
                - max_file_size_mb
                - max_title_length
                - max_description_length
                - supported_formats
                - rate_limit_per_day
        """
        return {
            "max_file_size_mb": None,
            "max_title_length": None,
            "max_description_length": None,
            "supported_formats": [],
            "rate_limit_per_day": None,
        }


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def classify_error(
    exception: Exception,
    platform: str,
    attempt_number: int = 1
) -> UploadResult:
    """
    Convert platform exceptions into standardized UploadResult.
    
    Helper function for poster modules to handle exceptions consistently.
    This ensures all errors are converted to the UploadResult format expected
    by downstream systems.
    
    Args:
        exception: Caught exception (any type)
        platform: Platform name
        attempt_number: Current attempt number (default: 1)
    
    Returns:
        UploadResult representing the failure
    
    Example Usage:
        try:
            remote_id = api_client.upload(...)
            return UploadResult.success_result(platform, remote_id, attempt_number)
        except Exception as e:
            # Convert to UploadResult
            return classify_error(e, platform, attempt_number)
    """
    if isinstance(exception, RetryableUploadError):
        return UploadResult.retryable_failure(
            platform=platform,
            attempt_number=attempt_number,
            error_message=exception.message,
            error_code=exception.error_code,
            retry_after=exception.retry_after,
            metadata=exception.metadata
        )
    
    if isinstance(exception, TerminalUploadError):
        return UploadResult.terminal_failure(
            platform=platform,
            attempt_number=attempt_number,
            error_message=exception.message,
            error_code=exception.error_code,
            metadata=exception.metadata
        )
    
    if isinstance(exception, UploadError):
        # Base UploadError - treat as retryable by default
        return UploadResult.retryable_failure(
            platform=platform,
            attempt_number=attempt_number,
            error_message=exception.message,
            error_code=exception.error_code,
            metadata=exception.metadata
        )
    
    # Unknown exception - default to retryable with caution
    # Log for investigation but allow retry in case it's transient
    logger.warning(
        f"Unknown exception type on {platform}: {type(exception).__name__}: {exception}",
        exc_info=True
    )
    
    return UploadResult.retryable_failure(
        platform=platform,
        attempt_number=attempt_number,
        error_message=f"Unexpected error: {str(exception)}",
        error_code=ErrorCode.UNKNOWN_ERROR,
        metadata={"exception_type": type(exception).__name__}
    )


def is_retryable_error(exception: Exception) -> bool:
    """
    Determine if an exception represents a retryable failure.
    
    Args:
        exception: Exception to check
    
    Returns:
        True if error is retryable, False if terminal
    """
    if isinstance(exception, RetryableUploadError):
        return True
    if isinstance(exception, TerminalUploadError):
        return False
    if isinstance(exception, UploadError):
        # Base UploadError defaults to retryable
        return True
    # Unknown exceptions default to retryable (conservative approach)
    return True


def classify_http_status(
    status_code: int,
    platform: str,
    message: Optional[str] = None,
    retry_after: Optional[int] = None
) -> UploadError:
    """
    Classify HTTP status codes into UploadError hierarchy.
    
    Helper function for poster modules to convert HTTP responses
    into standardized errors.
    
    Args:
        status_code: HTTP status code (e.g., 429, 401, 503)
        platform: Platform name
        message: Optional error message
        retry_after: Optional retry delay in seconds (for 429)
    
    Returns:
        UploadError (RetryableUploadError or TerminalUploadError)
    
    Status Code Classification:
        Retryable (2xx/3xx not here, 429, 5xx):
            429: Rate limit exceeded
            500: Internal server error
            502: Bad gateway
            503: Service unavailable
            504: Gateway timeout
        
        Terminal (4xx, except 429):
            400: Bad request
            401: Unauthorized
            403: Forbidden
            404: Not found
            413: Payload too large
            415: Unsupported media type
    """
    # Retryable errors (5xx, 429)
    if status_code == 429:
        return RetryableUploadError.from_rate_limit(
            platform=platform,
            retry_after=retry_after,
            message=message
        )
    elif status_code >= 500:
        error_code = {
            500: ErrorCode.SERVICE_UNAVAILABLE,
            502: ErrorCode.NETWORK_ERROR,
            503: ErrorCode.SERVICE_UNAVAILABLE,
            504: ErrorCode.NETWORK_TIMEOUT
        }.get(status_code, ErrorCode.SERVICE_UNAVAILABLE)
        return RetryableUploadError(
            message or f"Server error {status_code} on {platform}",
            platform=platform,
            error_code=error_code
        )
    
    # Terminal errors (4xx, except 429)
    elif status_code == 401:
        return TerminalUploadError.from_auth_failure(
            platform=platform,
            message=message or f"Unauthorized on {platform}"
        )
    elif status_code == 403:
        return TerminalUploadError(
            message or f"Forbidden on {platform}",
            platform=platform,
            error_code=ErrorCode.FORBIDDEN
        )
    elif status_code == 404:
        return TerminalUploadError(
            message or f"Not found on {platform}",
            platform=platform,
            error_code=ErrorCode.NOT_FOUND
        )
    elif status_code == 413:
        return TerminalUploadError.from_invalid_content(
            platform=platform,
            reason=f"File too large (HTTP {status_code})",
            message=message
        )
    elif status_code == 415:
        return TerminalUploadError.from_invalid_content(
            platform=platform,
            reason=f"Unsupported format (HTTP {status_code})",
            message=message
        )
    elif status_code >= 400:
        return TerminalUploadError(
            message or f"Client error {status_code} on {platform}",
            platform=platform,
            error_code=ErrorCode.BAD_REQUEST
        )
    
    # Unknown status codes default to retryable (conservative)
    return RetryableUploadError(
        message or f"Unknown HTTP status {status_code} on {platform}",
        platform=platform,
        error_code=ErrorCode.UNKNOWN_ERROR
    )


def validate_file_path(file_path: str, must_exist: bool = True) -> bool:
    """
    Validate file path for upload.
    
    Args:
        file_path: Path to file
        must_exist: If True, file must exist
    
    Returns:
        True if valid
    
    Raises:
        ValueError: If path is invalid
        FileNotFoundError: If file doesn't exist (and must_exist=True)
    """
    import os
    
    if not file_path or not file_path.strip():
        raise ValueError("file_path is required and cannot be empty")
    
    if must_exist:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        if not os.path.isfile(file_path):
            raise ValueError(f"Path is not a file: {file_path}")
        if os.path.getsize(file_path) == 0:
            raise ValueError(f"File is empty: {file_path}")
    
    return True


def truncate_text(text: str, max_length: int, suffix: str = "...") -> str:
    """
    Truncate text to maximum length with suffix.
    
    Args:
        text: Text to truncate
        max_length: Maximum length (including suffix)
        suffix: Suffix to append if truncated
    
    Returns:
        Truncated text
    """
    if not text:
        return text
    
    if len(text) <= max_length:
        return text
    
    if len(suffix) >= max_length:
        return suffix[:max_length]
    
    return text[:max_length - len(suffix)] + suffix


# ============================================================================
# USAGE EXAMPLES
# ============================================================================

"""
COMPREHENSIVE USAGE EXAMPLES
=============================

1. Implementing a Platform Poster:
   --------------------------------
   from posting.common.upload_contract import (
       BaseUploader, UploadContent, UploadResult,
       RetryableUploadError, TerminalUploadError, ErrorCode
   )
   import requests
   
   class YouTubePoster(BaseUploader):
       def __init__(self, config):
           super().__init__("youtube", config)
           self.api_client = YouTubeAPIClient(config["api_key"])
       
       def upload(self, content: UploadContent, attempt_number: int = 1, **kwargs) -> UploadResult:
           try:
               # Validate
               self.validate_content(content)
               
               # Preprocess
               content = self.preprocess_metadata(content)
               
               # Upload via API
               response = self.api_client.upload_video(
                   file_path=content.file_path,
                   title=content.title,
                   description=content.description,
                   tags=content.tags,
                   visibility=content.visibility
               )
               
               # Success
               result = UploadResult.success_result(
                   platform=self.platform_name,
                   remote_post_id=response["id"],
                   attempt_number=attempt_number,
                   remote_url=response.get("url"),
                   metadata={"upload_duration": response.get("duration")}
               )
               
           except requests.exceptions.Timeout as e:
               raise RetryableUploadError(
                   f"Upload timeout: {str(e)}",
                   platform=self.platform_name,
                   error_code=ErrorCode.NETWORK_TIMEOUT
               )
           
           except requests.exceptions.HTTPError as e:
               # Use helper to classify HTTP status
               raise classify_http_status(
                   status_code=e.response.status_code,
                   platform=self.platform_name,
                   message=str(e),
                   retry_after=e.response.headers.get("Retry-After")
               )
           
           except ValueError as e:
               # Validation errors are terminal
               raise TerminalUploadError.from_invalid_content(
                   platform=self.platform_name,
                   reason=str(e)
               )
           
           finally:
               if 'result' in locals():
                   self.post_upload_hook(result)
           
           return result
       
       def validate_content(self, content: UploadContent) -> bool:
           # Basic validation
           super().validate_content(content)
           
           # YouTube-specific: check file size (128GB max)
           import os
           file_size_gb = os.path.getsize(content.file_path) / (1024 ** 3)
           if file_size_gb > 128:
               raise TerminalUploadError.from_invalid_content(
                   self.platform_name,
                   f"File too large: {file_size_gb:.1f}GB (max 128GB)"
               )
           
           # YouTube-specific: title max 100 chars
           if len(content.title) > 100:
               raise TerminalUploadError(
                   f"Title too long: {len(content.title)} chars (max 100)",
                   platform=self.platform_name,
                   error_code=ErrorCode.TITLE_TOO_LONG
               )
           
           return True
       
       def preprocess_metadata(self, content: UploadContent) -> UploadContent:
           # Normalize metadata
           content = content.normalize_metadata()
           
           # Truncate title if needed
           content.title = truncate_text(content.title, 100)
           
           # Limit tags to 10
           if len(content.tags) > 10:
               content.tags = content.tags[:10]
               self._logger.info("Limited tags to 10 items")
           
           return content
       
       def post_upload_hook(self, result: UploadResult) -> None:
           # Emit metrics
           if result.success:
               self._logger.info(f"YouTube upload successful: {result.remote_post_id}")
           else:
               self._logger.warning(
                   f"YouTube upload failed: {result.error_code} - {result.error_message}"
               )


2. Using UploadContent:
   --------------------
   from posting.common.upload_contract import UploadContent
   from datetime import datetime, timedelta
   
   # Create content for upload
   content = UploadContent(
       file_path="/path/to/video.mp4",
       content_type="video",
       title="My Awesome Video",
       description="This is a great video about...",
       tags=["entertainment", "viral", "funny"],
       visibility="public",
       scheduled_time=datetime.utcnow() + timedelta(hours=2),  # Schedule for 2 hours from now
       thumbnail_path="/path/to/thumbnail.jpg",
       optional_fields={
           "category": "Entertainment",
           "language": "en"
       }
   )
   
   # Validate
   content.validate_basic()
   
   # Normalize metadata
   normalized = content.normalize_metadata()


3. Handling Upload Results:
   -------------------------
   from posting.common.upload_contract import (
       UploadResult, classify_error, is_retryable_error
   )
   
   # In dispatcher
   try:
       result = poster.upload(content, attempt_number=1)
       
       if result.success:
           state_store.log_success(result)
           idempotency.record_upload(result.remote_post_id)
       else:
           if result.is_retryable:
               # Schedule retry
               scheduler.retry_upload(content, delay=result.retry_after)
           else:
               # Send to dead-letter queue
               dead_letter_queue.add(result)
   
   except RetryableUploadError as e:
       # Retry after delay
       scheduler.retry_upload(content, delay=e.retry_after)
   
   except TerminalUploadError as e:
       # Terminal failure - no retry
       logger.error(f"Terminal upload failure: {e.message}")
       dead_letter_queue.add(content)
   
   except Exception as e:
       # Convert to UploadResult
       result = classify_error(e, platform="youtube", attempt_number=1)
       if is_retryable_error(e):
           scheduler.retry_upload(content)
       else:
           dead_letter_queue.add(result)


4. Error Classification:
   ----------------------
   from posting.common.upload_contract import (
       classify_http_status, ErrorCode
   )
   
   # Convert HTTP error to UploadError
   try:
       response = requests.post(...)
       response.raise_for_status()
   except requests.exceptions.HTTPError as e:
       error = classify_http_status(
           status_code=e.response.status_code,
           platform="youtube",
           message=str(e),
           retry_after=e.response.headers.get("Retry-After")
       )
       raise error  # Re-raise as UploadError


5. Integration with Dispatcher:
   -----------------------------
   from posting.common.upload_contract import (
       BaseUploader, UploadContent, UploadResult,
       RetryableUploadError, TerminalUploadError
   )
   
   class PostDispatcher:
       def __init__(self, poster: BaseUploader, state_store, idempotency_store):
           self.poster = poster
           self.state_store = state_store
           self.idempotency_store = idempotency_store
       
       def dispatch(self, content: UploadContent, attempt_number: int = 1):
           # Check idempotency
           if self.idempotency_store.is_duplicate(content):
               return UploadResult.terminal_failure(
                   platform=self.poster.platform_name,
                   attempt_number=attempt_number,
                   error_message="Duplicate upload detected",
                   error_code="DUPLICATE"
               )
           
           # Execute upload
           try:
               result = self.poster.upload(content, attempt_number=attempt_number)
               
               # Log to state store
               self.state_store.record_upload(result)
               
               # Record idempotency
               if result.success:
                   self.idempotency_store.record_upload(result.remote_post_id)
               
               return result
           
           except RetryableUploadError as e:
               # Create result for retry
               result = UploadResult.retryable_failure(
                   platform=self.poster.platform_name,
                   attempt_number=attempt_number,
                   error_message=e.message,
                   error_code=e.error_code,
                   retry_after=e.retry_after,
                   metadata=e.metadata
               )
               self.state_store.record_upload(result)
               return result
           
           except TerminalUploadError as e:
               # Create result for dead-letter
               result = UploadResult.terminal_failure(
                   platform=self.poster.platform_name,
                   attempt_number=attempt_number,
                   error_message=e.message,
                   error_code=e.error_code,
                   metadata=e.metadata
               )
               self.state_store.record_upload(result)
               return result
"""


# ============================================================================
# PROTOCOL DEFINITIONS (for type checking and integration)
# ============================================================================

class UploaderProtocol(Protocol):
    """
    Protocol definition for uploader implementations.
    
    Allows type checking without requiring inheritance from BaseUploader
    (useful for duck typing and testing).
    """
    
    platform_name: str
    
    def upload(
        self,
        content: UploadContent,
        attempt_number: int = 1,
        **kwargs
    ) -> UploadResult:
        """Execute upload and return normalized result."""
        ...
    
    def validate_content(self, content: UploadContent) -> bool:
        """Validate content before upload."""
        ...
    
    def preprocess_metadata(self, content: UploadContent) -> UploadContent:
        """Preprocess metadata before upload."""
        ...
    
    def post_upload_hook(self, result: UploadResult) -> None:
        """Post-upload hook for logging/metrics."""
        ...


# ============================================================================
# TIER-0 GUARANTEES AND INTERACTION MATRIX
# ============================================================================

"""
TIER-0 GUARANTEES PROVIDED BY THIS CONTRACT:

1. Cross-Platform Consistency
   - All posters return identical UploadResult structure
   - Downstream systems (state store, idempotency, reconciliation) always
     receive predictable output
   - Prevents platform-specific quirks from breaking downstream logic

2. Normalized Error Handling
   - Retryable vs terminal failures are explicit and enforced
   - Dispatcher knows exactly when to retry (RetryableUploadError)
   - Dead-letter queue receives only terminal failures (TerminalUploadError)
   - Error codes enable pattern detection and classification

3. Extensibility Without Breaking Changes
   - New platforms implement BaseUploader without touching core pipeline
   - Optional fields in UploadContent.optional_fields for platform-specific data
   - Hooks (validate_content, preprocess_metadata, post_upload_hook) allow
     platform customization without breaking contract

4. Audit Trail Consistency
   - Every upload has timestamp, attempt number, platform identifier
   - State store can track exact execution history
   - Reconciliation can verify canonical truth using consistent structure
   - UploadResult.to_dict() ensures serialization consistency

5. Anomaly Detection Support
   - Standardized error_code and error_message enable pattern detection
   - Metadata field for platform-specific context without breaking schema
   - Consistent structure for cross-platform anomaly analysis
   - Failure classification enables automatic kill switch triggers

TIER-0 INTERACTION MATRIX:
==========================

| Caller / File                    | Allowed Interaction                      | Implementation              |
|----------------------------------|------------------------------------------|-----------------------------|
| _poster.py modules               | Implements BaseUploader                  | Must implement upload()     |
|                                  | Returns UploadResult                     | Must raise UploadError      |
|                                  |                                          | hierarchy on failure        |
|----------------------------------|------------------------------------------|-----------------------------|
| post_dispatcher.py               | Calls poster.upload() after checks       | Receives UploadResult       |
|                                  | Handles RetryableUploadError (retries)   | Passes to state store       |
|                                  | Handles TerminalUploadError (dead-letter)|                             |
|----------------------------------|------------------------------------------|-----------------------------|
| posting_state_store.py           | Receives normalized UploadResult         | Logs via result.to_dict()  |
|                                  | Tracks state transitions                 | Uses result.status          |
|                                  |                                          | Uses result.remote_post_id  |
|----------------------------------|------------------------------------------|-----------------------------|
| idempotency.py                   | Uses UploadResult to confirm execution   | Checks result.remote_post_id|
|                                  | Prevents duplicate uploads               | Uses result.success         |
|----------------------------------|------------------------------------------|-----------------------------|
| reconciliation.py                | Uses UploadResult to determine truth     | Compares result.status      |
|                                  | Verifies canonical state                 | Uses result.timestamp       |
|                                  |                                          | Uses result.metadata        |
|----------------------------------|------------------------------------------|-----------------------------|
| anomaly_detector.py              | Observes UploadResult for patterns       | Analyzes result.error_code  |
|                                  | Detects systemic failures                | Uses result.metadata        |
|                                  | Triggers kill switches                   | Patterns on failure types   |
|----------------------------------|------------------------------------------|-----------------------------|
| kill_switches.py                 | Can be triggered on error patterns       | Receives UploadResult       |
|                                  | Monitors failure rates                   | Escalates on systemic issues|
|----------------------------------|------------------------------------------|-----------------------------|

ARCHITECTURAL FLOW:
===================

_base_poster.py → BaseUploader(upload_contract) → _poster.py → post_dispatcher.py
                                                                    ↓
                                                           posting_state_store.py
                                                                    ↓
                                                ┌───────────────────┼───────────────────┐
                                                ↓                   ↓                   ↓
                                        idempotency.py    reconciliation.py    anomaly_detector.py
                                                                                         ↓
                                                                                kill_switches.py

WHY THIS FILE IS CRITICAL:
==========================

Without upload_contract.py:
- Each poster could return inconsistent result structures
- Dispatcher couldn't reliably distinguish retryable vs terminal failures
- State store might receive malformed or missing data
- Reconciliation couldn't compare results across platforms
- Anomaly detection would lack consistent patterns to detect
- Kill switches couldn't reliably trigger on systemic issues

With upload_contract.py:
✅ All posters behave identically from dispatcher perspective
✅ Downstream systems receive guaranteed structure
✅ Retry logic works correctly across all platforms
✅ Cross-platform monitoring and anomaly detection reliable
✅ New platforms can be added without breaking existing logic

This file locks the contract between poster modules and the dispatcher/state store,
ensuring all posting actions are predictable, auditable, and safe across platforms.
"""