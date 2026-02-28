"""
/platforms/common/auth_manager.py

Tier-0 Production Auth Manager
================================
Core credential and token authority for all platform posting infrastructure.

Purpose:
    Secure lifecycle management of credentials and tokens across all platform accounts.
    Ensures every account has valid credentials, refreshes tokens safely, and rotates/revokes
    compromised credentials without impacting Tier-0 execution.

Tier-0 Role:
    - Ensures uninterrupted posting execution with valid credentials
    - Prevents silent auth failures that corrupt state or metrics
    - Supports multi-account, multi-platform safety
    - Integrates with kill switches for systemic auth failure detection

Integration Points:
    - platform_session.py: requests and refreshes tokens
    - _poster.py modules: uses tokens for dispatch
    - posting_state_store.py & idempotency.py: safe replay when auth fails
    - anomaly_detector.py & kill_switches.py: triggers alerts on repeated failures
"""

import time
import threading
import hashlib
import hmac
import json
from typing import Dict, Optional, List, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from cryptography.fernet import Fernet
import logging

logger = logging.getLogger(__name__)

# Optional imports for integration
try:
    from monitoring.anomaly_detector import AnomalyDetector, AnomalySignal, AnomalyType, AnomalySeverity
except ImportError:
    AnomalyDetector = None
    AnomalySignal = None
    AnomalyType = None
    AnomalySeverity = None

try:
    from kill_switches import KillSwitchManager, KillScope, KillReason
except ImportError:
    KillSwitchManager = None
    KillScope = None
    KillReason = None


# ============================================================================
# EXCEPTIONS
# ============================================================================

class AuthError(Exception):
    """Base exception for authentication errors."""
    pass


class TokenExpiredError(AuthError):
    """Token has expired and needs refresh."""
    pass


class TokenConflictError(AuthError):
    """Token conflict detected across workers/processes."""
    pass


class CredentialNotFoundError(AuthError):
    """Credential not found for platform/account."""
    pass


class TokenValidationError(AuthError):
    """Token validation failed."""
    pass


# ============================================================================
# DATA STRUCTURES
# ============================================================================

class TokenState(Enum):
    """Token lifecycle states."""
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"
    REFRESHING = "refreshing"
    FAILED = "failed"


@dataclass
class Credential:
    """Encrypted credential storage."""
    platform: str
    account_id: str
    encrypted_secret: bytes
    created_at: float
    updated_at: float
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Token:
    """Token metadata and state."""
    platform: str
    account_id: str
    token_value: str
    issued_at: float
    expires_at: float
    state: TokenState
    refresh_count: int = 0
    last_validated: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        """Check if token is expired."""
        return time.time() >= self.expires_at

    def needs_refresh(self, buffer_seconds: int = 300) -> bool:
        """Check if token needs refresh (within buffer window)."""
        return time.time() >= (self.expires_at - buffer_seconds)


# ============================================================================
# CREDENTIAL STORE
# ============================================================================

class CredentialStore:
    """
    Secure storage for platform credentials.
    
    Responsibilities:
        - Encrypt credentials at rest (AES via Fernet)
        - Atomic read/write operations
        - Per-platform credential isolation
        - Audit trail for all operations
    """

    def __init__(self, encryption_key: Optional[bytes] = None):
        """
        Initialize credential store.
        
        Args:
            encryption_key: Fernet encryption key. If None, generates new key.
                          In production, load from secure key management service.
        """
        self._lock = threading.RLock()
        self._credentials: Dict[Tuple[str, str], Credential] = {}
        
        # Initialize encryption
        if encryption_key is None:
            encryption_key = Fernet.generate_key()
            logger.warning("Generated new encryption key - store securely!")
        
        self._cipher = Fernet(encryption_key)
        
        # Audit log
        self._audit_log: List[Dict[str, Any]] = []
        
        logger.info("CredentialStore initialized")

    def add_credential(
        self,
        platform: str,
        account_id: str,
        secret: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Add or update credential for platform/account.
        
        Args:
            platform: Platform identifier (youtube, tiktok, etc)
            account_id: Account identifier
            secret: Raw secret/refresh token to encrypt
            metadata: Optional metadata (e.g., scopes, account name)
        
        Returns:
            True if added/updated successfully
        """
        with self._lock:
            try:
                encrypted = self._cipher.encrypt(secret.encode('utf-8'))
                now = time.time()
                
                key = (platform, account_id)
                is_update = key in self._credentials
                
                credential = Credential(
                    platform=platform,
                    account_id=account_id,
                    encrypted_secret=encrypted,
                    created_at=self._credentials[key].created_at if is_update else now,
                    updated_at=now,
                    metadata=metadata or {}
                )
                
                self._credentials[key] = credential
                
                self._audit(
                    "credential_updated" if is_update else "credential_added",
                    platform=platform,
                    account_id=account_id
                )
                
                logger.info(f"Credential {'updated' if is_update else 'added'}: {platform}/{account_id}")
                return True
                
            except Exception as e:
                logger.error(f"Failed to add credential for {platform}/{account_id}: {e}")
                raise AuthError(f"Credential storage failed: {e}")

    def get_credential(self, platform: str, account_id: str) -> str:
        """
        Retrieve and decrypt credential.
        
        Args:
            platform: Platform identifier
            account_id: Account identifier
        
        Returns:
            Decrypted credential secret
        
        Raises:
            CredentialNotFoundError: If credential doesn't exist
        """
        with self._lock:
            key = (platform, account_id)
            
            if key not in self._credentials:
                raise CredentialNotFoundError(f"No credential for {platform}/{account_id}")
            
            try:
                credential = self._credentials[key]
                decrypted = self._cipher.decrypt(credential.encrypted_secret)
                
                self._audit(
                    "credential_accessed",
                    platform=platform,
                    account_id=account_id
                )
                
                return decrypted.decode('utf-8')
                
            except Exception as e:
                logger.error(f"Failed to decrypt credential for {platform}/{account_id}: {e}")
                raise AuthError(f"Credential decryption failed: {e}")

    def update_credential(
        self,
        platform: str,
        account_id: str,
        new_secret: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Update existing credential.
        
        Args:
            platform: Platform identifier
            account_id: Account identifier
            new_secret: New secret to encrypt
            metadata: Optional updated metadata
        
        Returns:
            True if updated successfully
        """
        return self.add_credential(platform, account_id, new_secret, metadata)

    def revoke_credential(self, platform: str, account_id: str) -> bool:
        """
        Revoke (delete) credential.
        
        Args:
            platform: Platform identifier
            account_id: Account identifier
        
        Returns:
            True if revoked successfully
        """
        with self._lock:
            key = (platform, account_id)
            
            if key not in self._credentials:
                logger.warning(f"Attempted to revoke non-existent credential: {platform}/{account_id}")
                return False
            
            del self._credentials[key]
            
            self._audit(
                "credential_revoked",
                platform=platform,
                account_id=account_id
            )
            
            logger.info(f"Credential revoked: {platform}/{account_id}")
            return True

    def list_accounts(self, platform: Optional[str] = None) -> List[Tuple[str, str]]:
        """
        List all accounts with credentials.
        
        Args:
            platform: Optional platform filter
        
        Returns:
            List of (platform, account_id) tuples
        """
        with self._lock:
            if platform:
                return [key for key in self._credentials.keys() if key[0] == platform]
            return list(self._credentials.keys())

    def _audit(self, event: str, **kwargs):
        """Record audit event."""
        entry = {
            "timestamp": time.time(),
            "event": event,
            **kwargs
        }
        self._audit_log.append(entry)
        
        # Keep last 10000 entries
        if len(self._audit_log) > 10000:
            self._audit_log = self._audit_log[-10000:]


# ============================================================================
# TOKEN MANAGER
# ============================================================================

class TokenManager:
    """
    Token lifecycle manager.
    
    Responsibilities:
        - Issue new tokens from credentials
        - Automatic token refresh before expiry
        - Token validation and health checks
        - Token revocation for compromised credentials
        - Integration with platform_session.py
    """

    def __init__(
        self,
        credential_store: CredentialStore,
        default_token_ttl: int = 3600,
        refresh_buffer: int = 300,
        metrics_emitter: Optional['MetricsEmitter'] = None,
        anomaly_detector: Optional[Any] = None,
        kill_switch_manager: Optional[Any] = None
    ):
        """
        Initialize token manager.
        
        Args:
            credential_store: CredentialStore instance
            default_token_ttl: Default token time-to-live in seconds
            refresh_buffer: Seconds before expiry to trigger refresh
            metrics_emitter: Optional MetricsEmitter for tracking
            anomaly_detector: Optional AnomalyDetector for failure alerts
            kill_switch_manager: Optional KillSwitchManager for systemic failures
        """
        self._store = credential_store
        self._default_ttl = default_token_ttl
        self._refresh_buffer = refresh_buffer
        
        self._lock = threading.RLock()
        self._tokens: Dict[Tuple[str, str], Token] = {}
        
        # Failure tracking for anomaly detection
        self._failure_counts: Dict[Tuple[str, str], int] = {}
        self._last_failures: Dict[Tuple[str, str], float] = {}
        
        # Integration components
        self._metrics_emitter = metrics_emitter
        self._anomaly_detector = anomaly_detector
        self._kill_switch_manager = kill_switch_manager
        
        # Multi-worker conflict tracking
        self._token_versions: Dict[Tuple[str, str], int] = {}
        self._worker_tokens: Dict[Tuple[str, str], Dict[str, str]] = {}  # worker_id -> token
        self._conflict_detection_enabled = True
        
        logger.info("TokenManager initialized")

    def request_token(
        self,
        platform: str,
        account_id: str,
        force_refresh: bool = False
    ) -> str:
        """
        Request token for platform/account. Issues new token if needed.
        
        Args:
            platform: Platform identifier
            account_id: Account identifier
            force_refresh: Force new token even if current is valid
        
        Returns:
            Active token string
        
        Raises:
            AuthError: If token cannot be obtained
        """
        with self._lock:
            key = (platform, account_id)
            
            # Check existing token
            if not force_refresh and key in self._tokens:
                token = self._tokens[key]
                
                if token.state == TokenState.ACTIVE and not token.needs_refresh(self._refresh_buffer):
                    logger.debug(f"Using existing token for {platform}/{account_id}")
                    return token.token_value
            
            # Issue new token
            return self._issue_token(platform, account_id)

    def refresh_token(
        self,
        platform: str,
        account_id: str,
        old_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Force refresh token for platform/account.
        
        Args:
            platform: Platform identifier
            account_id: Account identifier
            old_token: Optional old token (for validation/compatibility)
        
        Returns:
            Dict with 'token' and 'expires_in' keys (compatible with platform_sessions.py)
        """
        start_time = time.time()
        
        with self._lock:
            key = (platform, account_id)
            
            # Mark current token as refreshing
            if key in self._tokens:
                self._tokens[key].state = TokenState.REFRESHING
            
            try:
                new_token = self._issue_token(platform, account_id, is_refresh=True)
                
                # Record metrics
                if self._metrics_emitter:
                    latency_ms = (time.time() - start_time) * 1000
                    self._metrics_emitter.record_token_refreshed(latency_ms)
                
                return {
                    'token': new_token,
                    'expires_in': self._default_ttl
                }
            except Exception as e:
                # Record failure
                if self._metrics_emitter:
                    self._metrics_emitter.record_validation_failed()
                raise

    def validate_token(
        self,
        platform: str,
        account_id: str,
        worker_id: Optional[str] = None
    ) -> bool:
        """
        Validate token is active and not expired.
        
        Args:
            platform: Platform identifier
            account_id: Account identifier
            worker_id: Optional worker identifier for conflict detection
        
        Returns:
            True if token is valid
        
        Raises:
            TokenConflictError: If multi-worker conflict detected
        """
        with self._lock:
            key = (platform, account_id)
            
            if key not in self._tokens:
                return False
            
            token = self._tokens[key]
            token.last_validated = time.time()
            
            # Multi-worker conflict detection
            if self._conflict_detection_enabled and worker_id:
                if key not in self._worker_tokens:
                    self._worker_tokens[key] = {}
                
                # Check if another worker has a different token version
                stored_worker_token = self._worker_tokens[key].get(worker_id)
                current_version = token.metadata.get('version', 0)
                
                if stored_worker_token and stored_worker_token != token.token_value:
                    # Conflict detected - token was refreshed by another worker
                    logger.error(
                        f"Token conflict detected for {platform}/{account_id}: "
                        f"worker {worker_id} has stale token (version mismatch)"
                    )
                    raise TokenConflictError(
                        f"Token conflict: worker {worker_id} has stale token for {platform}/{account_id}"
                    )
                
                # Update worker token tracking
                self._worker_tokens[key][worker_id] = token.token_value
            
            is_valid = (
                token.state == TokenState.ACTIVE and
                not token.is_expired()
            )
            
            if not is_valid:
                logger.warning(f"Token validation failed for {platform}/{account_id}: state={token.state}")
                self._record_failure(platform, account_id)
                
                # Record metrics
                if self._metrics_emitter:
                    self._metrics_emitter.record_validation_failed()
            
            return is_valid

    def revoke_token(self, platform: str, account_id: str) -> bool:
        """
        Revoke token (e.g., for compromised credentials).
        
        Args:
            platform: Platform identifier
            account_id: Account identifier
        
        Returns:
            True if revoked successfully
        """
        with self._lock:
            key = (platform, account_id)
            
            if key not in self._tokens:
                logger.warning(f"Attempted to revoke non-existent token: {platform}/{account_id}")
                return False
            
            self._tokens[key].state = TokenState.REVOKED
            
            # Record metrics
            if self._metrics_emitter:
                self._metrics_emitter.record_token_revoked()
            
            logger.warning(f"Token revoked: {platform}/{account_id}")
            return True

    def get_active_token(self, platform: str, account_id: str) -> str:
        """
        Get active token, refreshing if necessary.
        
        Args:
            platform: Platform identifier
            account_id: Account identifier
        
        Returns:
            Active token string
        """
        return self.request_token(platform, account_id)
    
    def get_token(self, platform: str, account_id: str) -> Dict[str, Any]:
        """
        Get token in format compatible with platform_sessions.py.
        
        Args:
            platform: Platform identifier
            account_id: Account identifier
        
        Returns:
            Dict with 'token' and 'expires_in' keys
        """
        token_value = self.request_token(platform, account_id)
        
        # Get expiry info
        key = (platform, account_id)
        expires_in = self._default_ttl
        if key in self._tokens:
            token = self._tokens[key]
            expires_in = max(0, int(token.expires_at - time.time()))
        
        return {
            'token': token_value,
            'expires_in': expires_in
        }

    def _issue_token(
        self,
        platform: str,
        account_id: str,
        is_refresh: bool = False
    ) -> str:
        """
        Internal: Issue new token from credential.
        
        In production, this would call platform-specific OAuth/API token endpoints.
        For now, generates deterministic token from credential.
        """
        try:
            # Get credential
            credential = self._store.get_credential(platform, account_id)
            
            # Generate token (in production: call platform API)
            now = time.time()
            token_value = self._generate_token(platform, account_id, credential, now)
            
            key = (platform, account_id)
            refresh_count = 0
            
            if is_refresh and key in self._tokens:
                refresh_count = self._tokens[key].refresh_count + 1
            
            # Increment token version for multi-worker conflict detection
            self._token_versions[key] = self._token_versions.get(key, 0) + 1
            
            token = Token(
                platform=platform,
                account_id=account_id,
                token_value=token_value,
                issued_at=now,
                expires_at=now + self._default_ttl,
                state=TokenState.ACTIVE,
                refresh_count=refresh_count,
                last_validated=now,
                metadata={'version': self._token_versions[key]}
            )
            
            self._tokens[key] = token
            
            # Record metrics
            if self._metrics_emitter:
                if is_refresh:
                    # Latency will be recorded by caller
                    pass
                else:
                    self._metrics_emitter.record_token_issued()
            
            # Reset failure count on success
            if key in self._failure_counts:
                del self._failure_counts[key]
            
            logger.info(f"Token {'refreshed' if is_refresh else 'issued'} for {platform}/{account_id} (refresh_count={refresh_count})")
            return token_value
            
        except Exception as e:
            logger.error(f"Failed to issue token for {platform}/{account_id}: {e}")
            self._record_failure(platform, account_id)
            raise AuthError(f"Token issuance failed: {e}")

    def _generate_token(
        self,
        platform: str,
        account_id: str,
        credential: str,
        timestamp: float
    ) -> str:
        """
        Generate deterministic token.
        
        In production: Replace with actual platform OAuth/API calls.
        """
        # Use HMAC for deterministic token generation
        message = f"{platform}:{account_id}:{timestamp}".encode('utf-8')
        token = hmac.new(
            credential.encode('utf-8'),
            message,
            hashlib.sha256
        ).hexdigest()
        
        return f"tok_{platform}_{token[:32]}"

    def _record_failure(self, platform: str, account_id: str):
        """Record authentication failure for anomaly detection and kill switches."""
        key = (platform, account_id)
        self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
        self._last_failures[key] = time.time()
        
        failure_count = self._failure_counts[key]
        
        # Emit anomaly signal
        if self._anomaly_detector and AnomalySignal:
            try:
                signal = AnomalySignal(
                    metric_name="auth_failure_count",
                    metric_value=failure_count,
                    timestamp=time.time(),
                    platform=platform,
                    account_id=account_id,
                    source="auth_manager",
                    severity=AnomalySeverity.HIGH if failure_count >= 5 else AnomalySeverity.MEDIUM,
                    metadata={
                        "failure_count": failure_count,
                        "last_failure": self._last_failures[key]
                    }
                )
                self._anomaly_detector.ingest_signal(signal)
            except Exception as e:
                logger.warning(f"Failed to emit anomaly signal: {e}")
        
        # Trigger kill switch for systemic failures
        if failure_count >= 10 and self._kill_switch_manager and KillScope and KillReason:
            try:
                logger.error(f"Systemic auth failure detected for {platform}/{account_id}: engaging kill switch")
                self._kill_switch_manager.engage_kill(
                    scope=KillScope.ACCOUNT,
                    reason=KillReason.AUTHENTICATION_FAILURE,
                    engaged_by="auth_manager",
                    target=account_id,
                    metadata={
                        "platform": platform,
                        "failure_count": failure_count,
                        "last_failure": self._last_failures[key]
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to engage kill switch: {e}")
        
        if failure_count >= 5:
            logger.error(f"Multiple auth failures detected for {platform}/{account_id}: count={failure_count}")

    def get_failure_stats(self) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """Get failure statistics for anomaly detection."""
        with self._lock:
            return {
                key: {
                    "failure_count": self._failure_counts.get(key, 0),
                    "last_failure": self._last_failures.get(key)
                }
                for key in set(self._failure_counts.keys()) | set(self._last_failures.keys())
            }


# ============================================================================
# TOKEN VALIDATOR
# ============================================================================

class TokenValidator:
    """
    Advanced token validation and health checks.
    
    Responsibilities:
        - Ensure tokens not expired or reused incorrectly
        - Check multi-worker consistency
        - Alert monitoring on repeated failures
    """

    def __init__(self, token_manager: TokenManager):
        """Initialize validator."""
        self._manager = token_manager
        self._validation_cache: Dict[Tuple[str, str], Tuple[bool, float]] = {}
        self._lock = threading.RLock()

    def validate(
        self,
        platform: str,
        account_id: str,
        use_cache: bool = True,
        cache_ttl: int = 60
    ) -> bool:
        """
        Validate token with caching.
        
        Args:
            platform: Platform identifier
            account_id: Account identifier
            use_cache: Use cached validation result
            cache_ttl: Cache time-to-live in seconds
        
        Returns:
            True if token is valid
        """
        with self._lock:
            key = (platform, account_id)
            
            # Check cache
            if use_cache and key in self._validation_cache:
                is_valid, cached_at = self._validation_cache[key]
                if time.time() - cached_at < cache_ttl:
                    return is_valid
            
            # Perform validation
            is_valid = self._manager.validate_token(platform, account_id)
            
            # Update cache
            self._validation_cache[key] = (is_valid, time.time())
            
            return is_valid

    def validate_batch(
        self,
        accounts: List[Tuple[str, str]]
    ) -> Dict[Tuple[str, str], bool]:
        """
        Validate multiple tokens in batch.
        
        Args:
            accounts: List of (platform, account_id) tuples
        
        Returns:
            Dict mapping account to validation result
        """
        results = {}
        for platform, account_id in accounts:
            results[(platform, account_id)] = self.validate(platform, account_id)
        return results


# ============================================================================
# METRICS EMITTER
# ============================================================================

class MetricsEmitter:
    """
    Emit auth-related metrics for platform_telemetry.py
    
    Metrics:
        - Tokens issued/refreshed
        - Failed validations
        - Revocations
        - Time-to-refresh latency
    """

    def __init__(self, token_manager: TokenManager):
        """Initialize metrics emitter."""
        self._manager = token_manager
        self._metrics: Dict[str, Any] = {
            "tokens_issued": 0,
            "tokens_refreshed": 0,
            "validations_failed": 0,
            "tokens_revoked": 0,
            "refresh_latencies": []
        }
        self._lock = threading.RLock()

    def record_token_issued(self):
        """Record token issuance."""
        with self._lock:
            self._metrics["tokens_issued"] += 1

    def record_token_refreshed(self, latency_ms: float):
        """Record token refresh with latency."""
        with self._lock:
            self._metrics["tokens_refreshed"] += 1
            self._metrics["refresh_latencies"].append(latency_ms)
            
            # Keep last 1000 latencies
            if len(self._metrics["refresh_latencies"]) > 1000:
                self._metrics["refresh_latencies"] = self._metrics["refresh_latencies"][-1000:]

    def record_validation_failed(self):
        """Record validation failure."""
        with self._lock:
            self._metrics["validations_failed"] += 1

    def record_token_revoked(self):
        """Record token revocation."""
        with self._lock:
            self._metrics["tokens_revoked"] += 1

    def get_metrics(self) -> Dict[str, Any]:
        """Get all metrics snapshot."""
        with self._lock:
            metrics = self._metrics.copy()
            
            # Calculate latency stats
            if metrics["refresh_latencies"]:
                latencies = metrics["refresh_latencies"]
                metrics["avg_refresh_latency_ms"] = sum(latencies) / len(latencies)
                metrics["max_refresh_latency_ms"] = max(latencies)
            else:
                metrics["avg_refresh_latency_ms"] = 0
                metrics["max_refresh_latency_ms"] = 0
            
            # Add failure stats from token manager
            metrics["failure_stats"] = self._manager.get_failure_stats()
            
            return metrics

    def reset_metrics(self):
        """Reset all metrics (for testing or periodic reset)."""
        with self._lock:
            self._metrics = {
                "tokens_issued": 0,
                "tokens_refreshed": 0,
                "validations_failed": 0,
                "tokens_revoked": 0,
                "refresh_latencies": []
            }


# ============================================================================
# UNIFIED AUTH MANAGER (TIER-0 API)
# ============================================================================

class AuthManager:
    """
    Unified Tier-0 Auth Manager - Main API for all authentication operations.
    
    This class wraps CredentialStore, TokenManager, TokenValidator, and MetricsEmitter
    to provide a single, cohesive interface for all auth operations.
    
    Integration Points:
        - platform_session.py: Uses get_token(), refresh_token()
        - anomaly_detector.py: Receives signals on repeated failures
        - kill_switches.py: Triggers account/platform kills on systemic failures
    """
    
    def __init__(
        self,
        encryption_key: Optional[bytes] = None,
        default_token_ttl: int = 3600,
        refresh_buffer: int = 300,
        anomaly_detector: Optional[Any] = None,
        kill_switch_manager: Optional[Any] = None
    ):
        """
        Initialize unified auth manager.
        
        Args:
            encryption_key: Fernet encryption key for credentials
            default_token_ttl: Default token time-to-live in seconds
            refresh_buffer: Seconds before expiry to trigger refresh
            anomaly_detector: Optional AnomalyDetector instance
            kill_switch_manager: Optional KillSwitchManager instance
        """
        # Initialize components
        self._credential_store = CredentialStore(encryption_key)
        self._metrics_emitter = MetricsEmitter(None)  # Will be set after token_mgr
        self._token_manager = TokenManager(
            self._credential_store,
            default_token_ttl=default_token_ttl,
            refresh_buffer=refresh_buffer,
            metrics_emitter=self._metrics_emitter,
            anomaly_detector=anomaly_detector,
            kill_switch_manager=kill_switch_manager
        )
        self._metrics_emitter._manager = self._token_manager  # Link back
        self._token_validator = TokenValidator(self._token_manager)
        
        # Store integration components
        self._anomaly_detector = anomaly_detector
        self._kill_switch_manager = kill_switch_manager
        
        logger.info("AuthManager initialized (Tier-0)")
    
    # CredentialStore methods
    def add_credential(
        self,
        platform: str,
        account_id: str,
        secret: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Add or update credential."""
        return self._credential_store.add_credential(platform, account_id, secret, metadata)
    
    def get_credential(self, platform: str, account_id: str) -> str:
        """Get decrypted credential."""
        return self._credential_store.get_credential(platform, account_id)
    
    def update_credential(
        self,
        platform: str,
        account_id: str,
        new_secret: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Update existing credential."""
        return self._credential_store.update_credential(platform, account_id, new_secret, metadata)
    
    def revoke_credential(self, platform: str, account_id: str) -> bool:
        """Revoke credential."""
        return self._credential_store.revoke_credential(platform, account_id)
    
    def list_accounts(self, platform: Optional[str] = None) -> List[Tuple[str, str]]:
        """List all accounts with credentials."""
        return self._credential_store.list_accounts(platform)
    
    # TokenManager methods (compatible with platform_sessions.py)
    def get_token(self, platform: str, account_id: str) -> Dict[str, Any]:
        """
        Get token in format compatible with platform_sessions.py.
        
        Returns:
            Dict with 'token' and 'expires_in' keys
        """
        return self._token_manager.get_token(platform, account_id)
    
    def request_token(
        self,
        platform: str,
        account_id: str,
        force_refresh: bool = False
    ) -> str:
        """Request token (returns token string)."""
        return self._token_manager.request_token(platform, account_id, force_refresh)
    
    def refresh_token(
        self,
        platform: str,
        account_id: str,
        old_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Refresh token (compatible with platform_sessions.py).
        
        Returns:
            Dict with 'token' and 'expires_in' keys
        """
        return self._token_manager.refresh_token(platform, account_id, old_token)
    
    def validate_token(self, platform: str, account_id: str) -> bool:
        """Validate token is active and not expired."""
        return self._token_manager.validate_token(platform, account_id)
    
    def revoke_token(self, platform: str, account_id: str) -> bool:
        """Revoke token."""
        return self._token_manager.revoke_token(platform, account_id)
    
    def get_active_token(self, platform: str, account_id: str) -> str:
        """Get active token, refreshing if necessary."""
        return self._token_manager.get_active_token(platform, account_id)
    
    # TokenValidator methods
    def validate(self, platform: str, account_id: str, use_cache: bool = True) -> bool:
        """Validate token with caching."""
        return self._token_validator.validate(platform, account_id, use_cache)
    
    def validate_batch(self, accounts: List[Tuple[str, str]]) -> Dict[Tuple[str, str], bool]:
        """Validate multiple tokens in batch."""
        return self._token_validator.validate_batch(accounts)
    
    # Metrics methods
    def get_metrics(self) -> Dict[str, Any]:
        """Get all auth metrics."""
        return self._metrics_emitter.get_metrics()
    
    def get_failure_stats(self) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """Get failure statistics for anomaly detection."""
        return self._token_manager.get_failure_stats()
    
    # Direct access to components (for advanced usage)
    @property
    def credential_store(self) -> CredentialStore:
        """Get credential store instance."""
        return self._credential_store
    
    @property
    def token_manager(self) -> TokenManager:
        """Get token manager instance."""
        return self._token_manager
    
    @property
    def token_validator(self) -> TokenValidator:
        """Get token validator instance."""
        return self._token_validator
    
    @property
    def metrics_emitter(self) -> MetricsEmitter:
        """Get metrics emitter instance."""
        return self._metrics_emitter


# ============================================================================
# TIER-0 EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    print("=" * 80)
    print("auth_manager.py - Tier-0 Production Auth Example")
    print("=" * 80)
    
    # Initialize unified AuthManager
    auth_mgr = AuthManager()
    
    # Add credentials
    print("\n[1] Adding credentials...")
    auth_mgr.add_credential("youtube", "acct_123", "secret_refresh_token_abc")
    auth_mgr.add_credential("tiktok", "acct_456", "secret_refresh_token_xyz")
    
    # Request tokens (using unified API)
    print("\n[2] Requesting tokens...")
    yt_token_result = auth_mgr.get_token("youtube", "acct_123")
    tt_token_result = auth_mgr.get_token("tiktok", "acct_456")
    print(f"YouTube token: {yt_token_result['token'][:50]}... (expires in {yt_token_result['expires_in']}s)")
    print(f"TikTok token: {tt_token_result['token'][:50]}... (expires in {tt_token_result['expires_in']}s)")
    
    # Validate tokens
    print("\n[3] Validating tokens...")
    yt_valid = auth_mgr.validate("youtube", "acct_123")
    tt_valid = auth_mgr.validate("tiktok", "acct_456")
    print(f"YouTube valid: {yt_valid}")
    print(f"TikTok valid: {tt_valid}")
    
    # Refresh token
    print("\n[4] Refreshing YouTube token...")
    new_yt_token_result = auth_mgr.refresh_token("youtube", "acct_123")
    print(f"New YouTube token: {new_yt_token_result['token'][:50]}... (expires in {new_yt_token_result['expires_in']}s)")
    
    # List accounts
    print("\n[5] Listing accounts...")
    accounts = auth_mgr.list_accounts()
    print(f"Accounts: {accounts}")
    
    # Get metrics
    print("\n[6] Metrics snapshot...")
    metrics_data = auth_mgr.get_metrics()
    print(json.dumps(metrics_data, indent=2, default=str))
    
    # Get failure stats
    print("\n[7] Failure statistics...")
    failure_stats = auth_mgr.get_failure_stats()
    print(json.dumps(failure_stats, indent=2, default=str))
    
    print("\n" + "=" * 80)
    print("Tier-0 Auth Manager operational ✓")
    print("=" * 80)