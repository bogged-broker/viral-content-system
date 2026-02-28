"""
/posting/platforms/common/posting_errors.py

Canonical Platform Error Normalization Authority

This is the SINGLE authoritative translation layer between platform responses
and system-wide failure semantics. Every platform error MUST pass through this
file before affecting retries, state mutations, or anomaly detection.

Tier-0 Critical: If this file is wrong, your system appears healthy while
silently destroying accounts.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any, Union, List
import time
import re
from collections import defaultdict


# ============================================================================
# CANONICAL ERROR TAXONOMY (EXHAUSTIVE - NO EXTENSIONS WITHOUT APPROVAL)
# ============================================================================

class CanonicalErrorCode(Enum):
    """
    Finite, exhaustive error taxonomy.
    NO ad-hoc strings. NO platform-specific codes. NO free-form handling.
    """
    AUTH_EXPIRED = "auth_expired"
    AUTH_REVOKED = "auth_revoked"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    PLATFORM_OUTAGE = "platform_outage"
    PLATFORM_TIMEOUT = "platform_timeout"
    DUPLICATE_CONTENT = "duplicate_content"
    CONTENT_POLICY_VIOLATION = "content_policy_violation"
    ACCOUNT_SUSPENDED = "account_suspended"
    ACCOUNT_THROTTLED = "account_throttled"
    SHADOW_SUPPRESSION = "shadow_suppression"
    UNKNOWN_FAILURE = "unknown_failure"


class RetryDisposition(Enum):
    """
    Explicit retry semantics. AMBIGUOUS errors are NEVER retried automatically.
    """
    RETRYABLE = "retryable"
    TERMINAL = "terminal"
    AMBIGUOUS = "ambiguous"


class ErrorSeverity(Enum):
    """
    Systemic risk classification. Affects anomaly detection, kill switches,
    and trust signal decay rate.
    """
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ============================================================================
# CANONICAL POSTING ERROR (IMMUTABLE CONTRACT)
# ============================================================================

@dataclass(frozen=True)
class CanonicalPostingError:
    """
    Immutable, canonical representation of a platform failure.
    
    HARD RULES:
    - Never infers success
    - Never downgraded after classification
    - Single source of truth for error semantics
    """
    platform: str
    account_id: str
    
    code: CanonicalErrorCode
    severity: ErrorSeverity
    retry: RetryDisposition
    
    raw_error: str
    http_status: Optional[int] = None
    
    is_suppression_signal: bool = False
    is_kill_switch_candidate: bool = False
    
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate invariants on construction."""
        ErrorInvariantValidator.validate(self)


# ============================================================================
# PLATFORM ERROR MAPPER (STRICT TRANSLATION)
# ============================================================================

class PlatformErrorMapper:
    """
    Maps raw platform artifacts into canonical signals.
    
    NO guessing. NO inferring success. NO swallowing errors.
    """
    
    # HTTP status -> canonical code mapping
    HTTP_STATUS_MAP = {
        401: CanonicalErrorCode.AUTH_EXPIRED,
        403: CanonicalErrorCode.AUTH_REVOKED,
        429: CanonicalErrorCode.RATE_LIMITED,
        500: CanonicalErrorCode.PLATFORM_OUTAGE,
        502: CanonicalErrorCode.PLATFORM_OUTAGE,
        503: CanonicalErrorCode.PLATFORM_OUTAGE,
        504: CanonicalErrorCode.PLATFORM_TIMEOUT,
    }
    
    # Platform-specific error patterns (regex)
    PLATFORM_PATTERNS = {
        'twitter': {
            r'duplicate.*status': CanonicalErrorCode.DUPLICATE_CONTENT,
            r'user.*suspend': CanonicalErrorCode.ACCOUNT_SUSPENDED,
            r'rate.*limit': CanonicalErrorCode.RATE_LIMITED,
            r'token.*expired': CanonicalErrorCode.AUTH_EXPIRED,
            r'token.*revoked': CanonicalErrorCode.AUTH_REVOKED,
            r'automated.*behavior': CanonicalErrorCode.ACCOUNT_THROTTLED,
        },
        'linkedin': {
            r'quota.*exceeded': CanonicalErrorCode.QUOTA_EXCEEDED,
            r'access.*denied': CanonicalErrorCode.AUTH_REVOKED,
            r'throttle': CanonicalErrorCode.RATE_LIMITED,
            r'duplicate': CanonicalErrorCode.DUPLICATE_CONTENT,
        },
        'facebook': {
            r'user.*request.*limit': CanonicalErrorCode.RATE_LIMITED,
            r'session.*expired': CanonicalErrorCode.AUTH_EXPIRED,
            r'access.*token.*invalid': CanonicalErrorCode.AUTH_REVOKED,
            r'spam': CanonicalErrorCode.ACCOUNT_THROTTLED,
            r'duplicate': CanonicalErrorCode.DUPLICATE_CONTENT,
        },
    }
    
    @classmethod
    def map_http_error(
        cls,
        platform: str,
        account_id: str,
        status_code: int,
        response_body: str = "",
        headers: Optional[Dict[str, str]] = None
    ) -> CanonicalPostingError:
        """Map HTTP error to canonical form."""
        
        # Try direct status code mapping first
        if status_code in cls.HTTP_STATUS_MAP:
            code = cls.HTTP_STATUS_MAP[status_code]
        else:
            # Try pattern matching on response body
            code = cls._match_error_pattern(platform, response_body)
        
        # Determine severity and retry disposition
        severity = cls._determine_severity(code, status_code)
        retry = cls._determine_retry_disposition(code)
        
        # Check for special signals
        is_suppression = cls._is_suppression_signal(code, status_code, response_body)
        is_kill_switch = cls._is_kill_switch_candidate(code)
        
        return CanonicalPostingError(
            platform=platform,
            account_id=account_id,
            code=code,
            severity=severity,
            retry=retry,
            raw_error=f"HTTP {status_code}: {response_body[:500]}",
            http_status=status_code,
            is_suppression_signal=is_suppression,
            is_kill_switch_candidate=is_kill_switch,
            metadata={
                'response_body': response_body[:1000],
                'headers': headers or {}
            }
        )
    
    @classmethod
    def map_sdk_error(
        cls,
        platform: str,
        account_id: str,
        exception: Exception,
        error_code: Optional[str] = None
    ) -> CanonicalPostingError:
        """Map SDK exception to canonical form."""
        
        error_msg = str(exception)
        
        # Try to extract HTTP status from exception
        http_status = cls._extract_http_status(exception)
        
        # Pattern match on error message
        code = cls._match_error_pattern(platform, error_msg)
        
        # Check for timeout exceptions
        if isinstance(exception, TimeoutError) or 'timeout' in error_msg.lower():
            code = CanonicalErrorCode.PLATFORM_TIMEOUT
        
        severity = cls._determine_severity(code, http_status)
        retry = cls._determine_retry_disposition(code)
        
        is_suppression = False
        is_kill_switch = cls._is_kill_switch_candidate(code)
        
        return CanonicalPostingError(
            platform=platform,
            account_id=account_id,
            code=code,
            severity=severity,
            retry=retry,
            raw_error=f"SDK Error: {error_msg[:500]}",
            http_status=http_status,
            is_suppression_signal=is_suppression,
            is_kill_switch_candidate=is_kill_switch,
            metadata={
                'exception_type': type(exception).__name__,
                'error_code': error_code,
                'error_message': error_msg[:1000]
            }
        )
    
    @classmethod
    def map_timeout(
        cls,
        platform: str,
        account_id: str,
        timeout_seconds: float,
        operation: str = "post"
    ) -> CanonicalPostingError:
        """Map timeout event to canonical form."""
        
        return CanonicalPostingError(
            platform=platform,
            account_id=account_id,
            code=CanonicalErrorCode.PLATFORM_TIMEOUT,
            severity=ErrorSeverity.HIGH,
            retry=RetryDisposition.AMBIGUOUS,  # Timeout = ambiguous outcome
            raw_error=f"Timeout after {timeout_seconds}s during {operation}",
            http_status=None,
            is_suppression_signal=False,
            is_kill_switch_candidate=False,
            metadata={
                'timeout_seconds': timeout_seconds,
                'operation': operation
            }
        )
    
    @classmethod
    def map_unknown(
        cls,
        platform: str,
        account_id: str,
        error_description: str,
        context: Optional[Dict[str, Any]] = None
    ) -> CanonicalPostingError:
        """
        Map unknown/undocumented failure to canonical form.
        
        CRITICAL: Unknown errors are ALWAYS marked as:
        - Code: UNKNOWN_FAILURE
        - Retry: AMBIGUOUS
        - Severity: HIGH
        """
        
        return CanonicalPostingError(
            platform=platform,
            account_id=account_id,
            code=CanonicalErrorCode.UNKNOWN_FAILURE,
            severity=ErrorSeverity.HIGH,
            retry=RetryDisposition.AMBIGUOUS,
            raw_error=error_description[:500],
            http_status=None,
            is_suppression_signal=False,
            is_kill_switch_candidate=False,
            metadata=context or {}
        )
    
    @classmethod
    def _match_error_pattern(cls, platform: str, error_text: str) -> CanonicalErrorCode:
        """Pattern match error text to canonical code."""
        
        error_lower = error_text.lower()
        patterns = cls.PLATFORM_PATTERNS.get(platform.lower(), {})
        
        for pattern, code in patterns.items():
            if re.search(pattern, error_lower):
                return code
        
        # Fallback to generic patterns
        if 'auth' in error_lower or 'token' in error_lower:
            if 'expired' in error_lower:
                return CanonicalErrorCode.AUTH_EXPIRED
            if 'revoked' in error_lower or 'invalid' in error_lower:
                return CanonicalErrorCode.AUTH_REVOKED
        
        if 'rate' in error_lower or 'throttle' in error_lower:
            return CanonicalErrorCode.RATE_LIMITED
        
        if 'duplicate' in error_lower:
            return CanonicalErrorCode.DUPLICATE_CONTENT
        
        if 'suspend' in error_lower or 'banned' in error_lower:
            return CanonicalErrorCode.ACCOUNT_SUSPENDED
        
        if 'quota' in error_lower:
            return CanonicalErrorCode.QUOTA_EXCEEDED
        
        if 'policy' in error_lower or 'violation' in error_lower:
            return CanonicalErrorCode.CONTENT_POLICY_VIOLATION
        
        return CanonicalErrorCode.UNKNOWN_FAILURE
    
    @classmethod
    def _extract_http_status(cls, exception: Exception) -> Optional[int]:
        """Try to extract HTTP status from exception."""
        
        # Common patterns in SDK exceptions
        if hasattr(exception, 'status_code'):
            return exception.status_code
        if hasattr(exception, 'status'):
            return exception.status
        if hasattr(exception, 'response') and hasattr(exception.response, 'status_code'):
            return exception.response.status_code
        
        return None
    
    @classmethod
    def _determine_severity(
        cls,
        code: CanonicalErrorCode,
        http_status: Optional[int]
    ) -> ErrorSeverity:
        """Determine error severity based on code and context."""
        
        CRITICAL_CODES = {
            CanonicalErrorCode.AUTH_REVOKED,
            CanonicalErrorCode.ACCOUNT_SUSPENDED,
        }
        
        HIGH_CODES = {
            CanonicalErrorCode.PLATFORM_OUTAGE,
            CanonicalErrorCode.PLATFORM_TIMEOUT,
            CanonicalErrorCode.UNKNOWN_FAILURE,
            CanonicalErrorCode.QUOTA_EXCEEDED,
        }
        
        MEDIUM_CODES = {
            CanonicalErrorCode.RATE_LIMITED,
            CanonicalErrorCode.ACCOUNT_THROTTLED,
            CanonicalErrorCode.SHADOW_SUPPRESSION,
        }
        
        if code in CRITICAL_CODES:
            return ErrorSeverity.CRITICAL
        if code in HIGH_CODES:
            return ErrorSeverity.HIGH
        if code in MEDIUM_CODES:
            return ErrorSeverity.MEDIUM
        
        return ErrorSeverity.LOW
    
    @classmethod
    def _determine_retry_disposition(cls, code: CanonicalErrorCode) -> RetryDisposition:
        """Determine if error is retryable."""
        
        RETRYABLE_CODES = {
            CanonicalErrorCode.RATE_LIMITED,
            CanonicalErrorCode.PLATFORM_OUTAGE,
        }
        
        TERMINAL_CODES = {
            CanonicalErrorCode.AUTH_REVOKED,
            CanonicalErrorCode.DUPLICATE_CONTENT,
            CanonicalErrorCode.CONTENT_POLICY_VIOLATION,
            CanonicalErrorCode.ACCOUNT_SUSPENDED,
        }
        
        # AUTH_EXPIRED is retryable only with token refresh
        AMBIGUOUS_CODES = {
            CanonicalErrorCode.AUTH_EXPIRED,
            CanonicalErrorCode.PLATFORM_TIMEOUT,
            CanonicalErrorCode.UNKNOWN_FAILURE,
            CanonicalErrorCode.SHADOW_SUPPRESSION,
            CanonicalErrorCode.ACCOUNT_THROTTLED,
        }
        
        if code in RETRYABLE_CODES:
            return RetryDisposition.RETRYABLE
        if code in TERMINAL_CODES:
            return RetryDisposition.TERMINAL
        
        return RetryDisposition.AMBIGUOUS
    
    @classmethod
    def _is_suppression_signal(
        cls,
        code: CanonicalErrorCode,
        http_status: Optional[int],
        response_body: str
    ) -> bool:
        """Detect shadow suppression patterns."""
        
        # Explicit suppression codes
        if code == CanonicalErrorCode.SHADOW_SUPPRESSION:
            return True
        
        # "Success" with suspicious patterns
        if http_status == 200:
            response_lower = response_body.lower()
            if any(pattern in response_lower for pattern in [
                'review', 'pending', 'delayed', 'visibility limited'
            ]):
                return True
        
        return False
    
    @classmethod
    def _is_kill_switch_candidate(cls, code: CanonicalErrorCode) -> bool:
        """Determine if error should trigger kill switch evaluation."""
        
        KILL_SWITCH_CODES = {
            CanonicalErrorCode.AUTH_REVOKED,
            CanonicalErrorCode.ACCOUNT_SUSPENDED,
            CanonicalErrorCode.PLATFORM_OUTAGE,
            CanonicalErrorCode.QUOTA_EXCEEDED,
        }
        
        return code in KILL_SWITCH_CODES


# ============================================================================
# ERROR CLASSIFIER (SINGLE ENTRY POINT)
# ============================================================================

class ErrorClassifier:
    """
    Single entry point for error classification.
    
    RULES:
    - Exactly one canonical classification
    - Unknown errors -> UNKNOWN_FAILURE, AMBIGUOUS, HIGH severity
    - Never swallow errors
    - Never return success
    """
    
    @staticmethod
    def classify(
        platform: str,
        account_id: str,
        raw_error: Union[Exception, Dict[str, Any], str],
        context: Optional[Dict[str, Any]] = None
    ) -> CanonicalPostingError:
        """
        Classify any raw error into canonical form.
        
        Args:
            platform: Platform identifier
            account_id: Account that experienced the error
            raw_error: Raw error artifact (exception, response, etc)
            context: Additional context for classification
            
        Returns:
            CanonicalPostingError
        """
        
        context = context or {}
        
        # HTTP error (dict with status_code)
        if isinstance(raw_error, dict):
            return PlatformErrorMapper.map_http_error(
                platform=platform,
                account_id=account_id,
                status_code=raw_error.get('status_code', 500),
                response_body=raw_error.get('body', ''),
                headers=raw_error.get('headers')
            )
        
        # SDK exception
        if isinstance(raw_error, Exception):
            # Check for timeout
            if isinstance(raw_error, TimeoutError):
                return PlatformErrorMapper.map_timeout(
                    platform=platform,
                    account_id=account_id,
                    timeout_seconds=context.get('timeout_seconds', 30.0),
                    operation=context.get('operation', 'post')
                )
            
            return PlatformErrorMapper.map_sdk_error(
                platform=platform,
                account_id=account_id,
                exception=raw_error,
                error_code=context.get('error_code')
            )
        
        # String description
        if isinstance(raw_error, str):
            return PlatformErrorMapper.map_unknown(
                platform=platform,
                account_id=account_id,
                error_description=raw_error,
                context=context
            )
        
        # Unknown error type
        return PlatformErrorMapper.map_unknown(
            platform=platform,
            account_id=account_id,
            error_description=f"Unknown error type: {type(raw_error)}",
            context={'raw_error': str(raw_error), **context}
        )


# ============================================================================
# SUPPRESSION SIGNAL EXTRACTOR
# ============================================================================

class SuppressionSignalExtractor:
    """
    Detects shadow suppression patterns that don't fail posts but indicate
    trust decay or visibility reduction.
    
    Feeds: anomaly_detector.py, trust_signal_recorder.py
    """
    
    @staticmethod
    def extract_from_success_response(
        platform: str,
        account_id: str,
        response: Dict[str, Any],
        post_id: Optional[str]
    ) -> Optional[CanonicalPostingError]:
        """
        Analyze successful post response for suppression signals.
        
        Returns CanonicalPostingError if suppression detected, None otherwise.
        """
        
        # Missing post ID despite "success"
        if not post_id and response.get('success'):
            return CanonicalPostingError(
                platform=platform,
                account_id=account_id,
                code=CanonicalErrorCode.SHADOW_SUPPRESSION,
                severity=ErrorSeverity.MEDIUM,
                retry=RetryDisposition.AMBIGUOUS,
                raw_error="Success response without post ID",
                http_status=200,
                is_suppression_signal=True,
                is_kill_switch_candidate=False,
                metadata={'response': response}
            )
        
        # Delayed visibility flags
        visibility_flags = ['pending', 'review', 'delayed', 'limited']
        response_str = str(response).lower()
        
        if any(flag in response_str for flag in visibility_flags):
            return CanonicalPostingError(
                platform=platform,
                account_id=account_id,
                code=CanonicalErrorCode.SHADOW_SUPPRESSION,
                severity=ErrorSeverity.MEDIUM,
                retry=RetryDisposition.AMBIGUOUS,
                raw_error=f"Visibility delay indicators in response",
                http_status=200,
                is_suppression_signal=True,
                is_kill_switch_candidate=False,
                metadata={'response': response}
            )
        
        return None
    
    @staticmethod
    def extract_from_metrics(
        platform: str,
        account_id: str,
        post_id: str,
        impressions: int,
        hours_since_post: float
    ) -> Optional[CanonicalPostingError]:
        """
        Detect suppression from abnormally low engagement metrics.
        
        Returns CanonicalPostingError if suppression suspected, None otherwise.
        """
        
        # Zero impressions after reasonable time period
        if hours_since_post >= 2.0 and impressions == 0:
            return CanonicalPostingError(
                platform=platform,
                account_id=account_id,
                code=CanonicalErrorCode.SHADOW_SUPPRESSION,
                severity=ErrorSeverity.MEDIUM,
                retry=RetryDisposition.TERMINAL,
                raw_error=f"Zero impressions after {hours_since_post:.1f} hours",
                http_status=None,
                is_suppression_signal=True,
                is_kill_switch_candidate=False,
                metadata={
                    'post_id': post_id,
                    'impressions': impressions,
                    'hours_since_post': hours_since_post
                }
            )
        
        return None


# ============================================================================
# KILL-SWITCH SIGNAL EXTRACTOR
# ============================================================================

class KillSwitchSignalExtractor:
    """
    Identifies error patterns that should trigger kill switch evaluation.
    
    Feeds: kill_switches.py
    """
    
    @staticmethod
    def should_trigger_kill_switch(
        errors: list[CanonicalPostingError],
        window_seconds: float = 300.0
    ) -> Dict[str, Any]:
        """
        Analyze error pattern for kill switch conditions.
        
        Returns:
            {
                'trigger': bool,
                'reason': str,
                'severity': ErrorSeverity,
                'affected_accounts': set
            }
        """
        
        if not errors:
            return {'trigger': False}
        
        now = time.time()
        recent_errors = [
            e for e in errors
            if now - e.timestamp <= window_seconds
        ]
        
        if not recent_errors:
            return {'trigger': False}
        
        # Count by error code
        code_counts = defaultdict(int)
        affected_accounts = set()
        
        for error in recent_errors:
            code_counts[error.code] += 1
            affected_accounts.add(error.account_id)
        
        # CRITICAL: Auth revoked across multiple accounts
        if code_counts[CanonicalErrorCode.AUTH_REVOKED] >= 3:
            return {
                'trigger': True,
                'reason': f"Auth revoked across {len(affected_accounts)} accounts",
                'severity': ErrorSeverity.CRITICAL,
                'affected_accounts': affected_accounts,
                'error_code': CanonicalErrorCode.AUTH_REVOKED
            }
        
        # CRITICAL: Account suspensions
        if code_counts[CanonicalErrorCode.ACCOUNT_SUSPENDED] >= 2:
            return {
                'trigger': True,
                'reason': f"Multiple account suspensions ({code_counts[CanonicalErrorCode.ACCOUNT_SUSPENDED]})",
                'severity': ErrorSeverity.CRITICAL,
                'affected_accounts': affected_accounts,
                'error_code': CanonicalErrorCode.ACCOUNT_SUSPENDED
            }
        
        # HIGH: Platform outage
        if code_counts[CanonicalErrorCode.PLATFORM_OUTAGE] >= 5:
            return {
                'trigger': True,
                'reason': "Platform outage detected",
                'severity': ErrorSeverity.HIGH,
                'affected_accounts': affected_accounts,
                'error_code': CanonicalErrorCode.PLATFORM_OUTAGE
            }
        
        # HIGH: Repeated quota violations
        if code_counts[CanonicalErrorCode.QUOTA_EXCEEDED] >= 4:
            return {
                'trigger': True,
                'reason': "Repeated quota violations",
                'severity': ErrorSeverity.HIGH,
                'affected_accounts': affected_accounts,
                'error_code': CanonicalErrorCode.QUOTA_EXCEEDED
            }
        
        return {'trigger': False}


# ============================================================================
# ERROR INVARIANT VALIDATOR (NON-OPTIONAL)
# ============================================================================

class ErrorInvariantValidator:
    """
    Enforces critical invariants on error classification.
    
    Violation → System halt.
    """
    
    @staticmethod
    def validate(error: CanonicalPostingError) -> None:
        """
        Validate error invariants. Raises ValueError if violated.
        """
        
        # AUTH errors must never be retryable without refresh
        if error.code == CanonicalErrorCode.AUTH_EXPIRED:
            if error.retry == RetryDisposition.RETRYABLE:
                raise ValueError(
                    "AUTH_EXPIRED cannot be RETRYABLE without token refresh"
                )
        
        if error.code == CanonicalErrorCode.AUTH_REVOKED:
            if error.retry != RetryDisposition.TERMINAL:
                raise ValueError(
                    "AUTH_REVOKED must be TERMINAL"
                )
        
        # CONTENT_POLICY violations must be terminal
        if error.code == CanonicalErrorCode.CONTENT_POLICY_VIOLATION:
            if error.retry != RetryDisposition.TERMINAL:
                raise ValueError(
                    "CONTENT_POLICY_VIOLATION must be TERMINAL"
                )
        
        # DUPLICATE_CONTENT must be terminal
        if error.code == CanonicalErrorCode.DUPLICATE_CONTENT:
            if error.retry != RetryDisposition.TERMINAL:
                raise ValueError(
                    "DUPLICATE_CONTENT must be TERMINAL"
                )
        
        # SHADOW_SUPPRESSION must never be blindly retried
        if error.code == CanonicalErrorCode.SHADOW_SUPPRESSION:
            if error.retry == RetryDisposition.RETRYABLE:
                raise ValueError(
                    "SHADOW_SUPPRESSION cannot be RETRYABLE"
                )
        
        # UNKNOWN_FAILURE must always be ambiguous
        if error.code == CanonicalErrorCode.UNKNOWN_FAILURE:
            if error.retry != RetryDisposition.AMBIGUOUS:
                raise ValueError(
                    "UNKNOWN_FAILURE must be AMBIGUOUS"
                )
            if error.severity not in {ErrorSeverity.HIGH, ErrorSeverity.CRITICAL}:
                raise ValueError(
                    "UNKNOWN_FAILURE must be HIGH or CRITICAL severity"
                )
        
        # Account suspension must be critical
        if error.code == CanonicalErrorCode.ACCOUNT_SUSPENDED:
            if error.severity != ErrorSeverity.CRITICAL:
                raise ValueError(
                    "ACCOUNT_SUSPENDED must be CRITICAL severity"
                )
            if error.retry != RetryDisposition.TERMINAL:
                raise ValueError(
                    "ACCOUNT_SUSPENDED must be TERMINAL"
                )


# ============================================================================
# PUBLIC API
# ============================================================================

__all__ = [
    'CanonicalErrorCode',
    'ErrorSeverity',
    'RetryDisposition',
    'CanonicalPostingError',
    'ErrorClassifier',
    'SuppressionSignalExtractor',
    'KillSwitchSignalExtractor',
    'ErrorInvariantValidator',
]