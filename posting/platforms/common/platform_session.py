"""
/common/platform_session.py

Tier-0: Platform Session Management
Provides reliable, crash-resistant, multi-account session handling for all platforms.

Core Responsibilities:
- Session lifecycle management (create, maintain, refresh)
- Multi-worker safety (no token/session sharing) with distributed locking
- Integration with auth_manager for credential handling
- Platform limits awareness and rate limiting
- Error reporting & telemetry with anomaly detection integration
- Circuit breaker pattern for repeated failures
- Background health monitoring

Critical Rules:
- All poster modules MUST use PlatformSession - no direct API calls
- Sessions are isolated per (platform, account) combination
- Automatic recovery from expired tokens/cookies
- Thread-safe and process-safe session pools with distributed locking
"""

import threading
import time
import os
import socket
import uuid
import hashlib
import json
from pathlib import Path
from typing import Dict, Optional, Any, Callable, List, Set
from datetime import datetime, timedelta
from collections import defaultdict, deque
from contextlib import contextmanager
from enum import Enum
import logging

# Optional imports from other Tier-0 modules
try:
    from common.auth_manager import AuthManager
except ImportError:
    AuthManager = None

try:
    from common.platform_limits import PlatformLimits
except ImportError:
    PlatformLimits = None

try:
    from monitoring.anomaly_detector import AnomalyDetector, AnomalySignal, AnomalyType, AnomalySeverity
except ImportError:
    AnomalyDetector = None
    AnomalySignal = None
    AnomalyType = None
    AnomalySeverity = None

logger = logging.getLogger(__name__)


# ============================================================================
# EXCEPTIONS
# ============================================================================

class PlatformSessionError(Exception):
    """Base exception for all session-related errors."""
    pass


class SessionExpiredError(PlatformSessionError):
    """Raised when a session has expired and needs refresh."""
    pass


class SessionConflictError(PlatformSessionError):
    """Raised when multi-worker conflict is detected."""
    pass


class SessionAuthenticationError(PlatformSessionError):
    """Raised when authentication/refresh fails."""
    pass


class SessionCircuitBreakerOpen(PlatformSessionError):
    """Raised when circuit breaker is open due to repeated failures."""
    pass


# ============================================================================
# CIRCUIT BREAKER
# ============================================================================

class CircuitBreakerState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreaker:
    """
    Circuit breaker pattern for session failures.
    Prevents cascading failures by opening circuit after repeated errors.
    """
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        success_threshold: int = 2
    ):
        """
        Initialize circuit breaker.
        
        Args:
            failure_threshold: Number of failures before opening circuit
            recovery_timeout: Seconds to wait before attempting recovery
            success_threshold: Successful calls needed to close circuit
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold
        
        self._state = CircuitBreakerState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = None
        self._lock = threading.RLock()
    
    def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with circuit breaker protection.
        
        Raises:
            SessionCircuitBreakerOpen: If circuit is open
        """
        with self._lock:
            if self._state == CircuitBreakerState.OPEN:
                if self._should_attempt_recovery():
                    self._state = CircuitBreakerState.HALF_OPEN
                    self._success_count = 0
                    logger.info("Circuit breaker entering HALF_OPEN state")
                else:
                    raise SessionCircuitBreakerOpen(
                        f"Circuit breaker is OPEN. Last failure: {self._last_failure_time}"
                    )
            
            try:
                result = func(*args, **kwargs)
                self._on_success()
                return result
            except Exception as e:
                self._on_failure()
                raise
    
    def _should_attempt_recovery(self) -> bool:
        """Check if enough time has passed to attempt recovery."""
        if not self._last_failure_time:
            return True
        elapsed = time.time() - self._last_failure_time
        return elapsed >= self.recovery_timeout
    
    def _on_success(self):
        """Handle successful call."""
        with self._lock:
            if self._state == CircuitBreakerState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._state = CircuitBreakerState.CLOSED
                    self._failure_count = 0
                    logger.info("Circuit breaker CLOSED after recovery")
            elif self._state == CircuitBreakerState.CLOSED:
                # Reset failure count on success
                self._failure_count = 0
    
    def _on_failure(self):
        """Handle failed call."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            
            if self._state == CircuitBreakerState.HALF_OPEN:
                # Any failure in half-open immediately opens circuit
                self._state = CircuitBreakerState.OPEN
                logger.warning("Circuit breaker OPENED after failure in HALF_OPEN")
            elif self._failure_count >= self.failure_threshold:
                self._state = CircuitBreakerState.OPEN
                logger.warning(f"Circuit breaker OPENED after {self._failure_count} failures")
    
    def get_state(self) -> CircuitBreakerState:
        """Get current circuit breaker state."""
        return self._state
    
    def reset(self):
        """Manually reset circuit breaker to closed state."""
        with self._lock:
            self._state = CircuitBreakerState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None


# ============================================================================
# DISTRIBUTED LOCK MANAGER
# ============================================================================

class DistributedLock:
    """
    Distributed lock for multi-process session management.
    Uses file-based locking with automatic expiration.
    """
    
    def __init__(
        self,
        lock_storage_path: str = "/tmp/platform_session_locks",
        lock_timeout_seconds: float = 300.0
    ):
        """
        Initialize distributed lock manager.
        
        Args:
            lock_storage_path: Directory for lock files
            lock_timeout_seconds: Lock expiration time
        """
        self.lock_storage_path = Path(lock_storage_path)
        self.lock_storage_path.mkdir(parents=True, exist_ok=True)
        self.lock_timeout = lock_timeout_seconds
        self.worker_id = self._generate_worker_id()
        self._local_locks: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
    
    def _generate_worker_id(self) -> str:
        """Generate unique worker ID."""
        hostname = socket.gethostname()
        pid = os.getpid()
        unique = str(uuid.uuid4())[:8]
        return f"{hostname}-{pid}-{unique}"
    
    def _get_lock_file_path(self, key: str) -> Path:
        """Get file path for lock."""
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        return self.lock_storage_path / f"lock_{key_hash}.json"
    
    def acquire(self, key: str, timeout: float = 5.0) -> bool:
        """
        Acquire distributed lock.
        
        Args:
            key: Lock key (typically session_id)
            timeout: Maximum time to wait for lock
            
        Returns:
            True if acquired, False if timeout
        """
        lock_file = self._get_lock_file_path(key)
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            with self._lock:
                # Check if lock exists and is expired
                if lock_file.exists():
                    try:
                        with open(lock_file, 'r') as f:
                            lock_data = json.load(f)
                        
                        expires_at = lock_data.get('expires_at', 0)
                        if time.time() < expires_at:
                            # Lock held by another worker
                            if lock_data.get('worker_id') != self.worker_id:
                                time.sleep(0.1)  # Brief wait before retry
                                continue
                        else:
                            # Lock expired, remove it
                            lock_file.unlink(missing_ok=True)
                    except (json.JSONDecodeError, IOError):
                        # Corrupted lock file, remove it
                        lock_file.unlink(missing_ok=True)
                
                # Acquire lock
                try:
                    lock_data = {
                        'worker_id': self.worker_id,
                        'acquired_at': time.time(),
                        'expires_at': time.time() + self.lock_timeout
                    }
                    
                    # Atomic write using temp file
                    temp_file = lock_file.with_suffix('.tmp')
                    with open(temp_file, 'w') as f:
                        json.dump(lock_data, f)
                    temp_file.replace(lock_file)
                    
                    self._local_locks[key] = lock_data
                    logger.debug(f"[{key}] Distributed lock acquired by {self.worker_id}")
                    return True
                    
                except Exception as e:
                    logger.warning(f"[{key}] Failed to acquire lock: {e}")
                    time.sleep(0.1)
                    continue
        
        return False
    
    def release(self, key: str):
        """Release distributed lock."""
        lock_file = self._get_lock_file_path(key)
        
        with self._lock:
            if key in self._local_locks:
                # Verify we own the lock
                if lock_file.exists():
                    try:
                        with open(lock_file, 'r') as f:
                            lock_data = json.load(f)
                        if lock_data.get('worker_id') == self.worker_id:
                            lock_file.unlink(missing_ok=True)
                            logger.debug(f"[{key}] Distributed lock released")
                    except (json.JSONDecodeError, IOError):
                        pass
                
                del self._local_locks[key]
    
    def refresh(self, key: str) -> bool:
        """Refresh lock expiration time."""
        lock_file = self._get_lock_file_path(key)
        
        with self._lock:
            if key not in self._local_locks:
                return False
            
            if not lock_file.exists():
                return False
            
            try:
                with open(lock_file, 'r') as f:
                    lock_data = json.load(f)
                
                if lock_data.get('worker_id') != self.worker_id:
                    return False
                
                # Refresh expiration
                lock_data['expires_at'] = time.time() + self.lock_timeout
                temp_file = lock_file.with_suffix('.tmp')
                with open(temp_file, 'w') as f:
                    json.dump(lock_data, f)
                temp_file.replace(lock_file)
                
                self._local_locks[key] = lock_data
                return True
            except Exception as e:
                logger.warning(f"[{key}] Failed to refresh lock: {e}")
                return False


# ============================================================================
# PLATFORM SESSION
# ============================================================================

class PlatformSession:
    """
    Maintains one session per (platform, account) combination.
    
    Responsibilities:
    - Automatic refresh on expiry
    - Expose API client for poster modules
    - Track session health and metrics
    - Circuit breaker protection
    - Rate limiting integration
    """
    
    def __init__(
        self,
        platform_name: str,
        account_id: str,
        auth_manager=None,
        limits_manager=None,
        anomaly_detector=None,
        enable_circuit_breaker: bool = True
    ):
        """
        Initialize a platform session.
        
        Args:
            platform_name: Name of platform (youtube, tiktok, instagram, etc.)
            account_id: Unique account identifier
            auth_manager: AuthManager instance (optional)
            limits_manager: PlatformLimits instance (optional)
            anomaly_detector: AnomalyDetector instance (optional)
            enable_circuit_breaker: Enable circuit breaker protection
        """
        self.platform_name = platform_name.lower()
        self.account_id = account_id
        self.session_id = f"{self.platform_name}:{self.account_id}"
        
        # Session state
        self._client = None
        self._token = None
        self._token_expiry = None
        self._last_refresh = None
        self._is_authenticated = False
        self._lock = threading.RLock()
        
        # Dependencies
        self._auth_manager = auth_manager
        self._limits_manager = limits_manager
        self._anomaly_detector = anomaly_detector
        
        # Circuit breaker
        self._circuit_breaker = CircuitBreaker() if enable_circuit_breaker else None
        
        # Metrics
        self._refresh_count = 0
        self._auth_failures = 0
        self._consecutive_failures = 0
        self._last_error = None
        self._created_at = datetime.utcnow()
        self._last_successful_call = None
        
        # Rate limiting tracking
        self._last_api_call_time = None
        self._api_call_count = 0
        self._rate_limit_window_start = time.time()
        
        logger.info(f"[{self.session_id}] Session initialized")
    
    def authenticate(self) -> bool:
        """
        Authenticate session and obtain initial token.
        
        Returns:
            bool: True if authentication succeeded
            
        Raises:
            SessionAuthenticationError: If authentication fails
            SessionCircuitBreakerOpen: If circuit breaker is open
        """
        if self._circuit_breaker:
            return self._circuit_breaker.call(self._authenticate_internal)
        return self._authenticate_internal()
    
    def _authenticate_internal(self) -> bool:
        """Internal authentication logic."""
        with self._lock:
            try:
                logger.info(f"[{self.session_id}] Authenticating...")
                
                # Check rate limits before authentication
                if self._limits_manager:
                    if not self._limits_manager.can_authenticate(self.platform_name, self.account_id):
                        raise SessionAuthenticationError(
                            f"Rate limit exceeded for authentication: {self.session_id}"
                        )
                
                # Request token from auth_manager
                if self._auth_manager:
                    auth_result = self._auth_manager.get_token(
                        self.platform_name, 
                        self.account_id
                    )
                    self._token = auth_result.get('token')
                    expires_in = auth_result.get('expires_in', 3600)
                    self._token_expiry = datetime.utcnow() + timedelta(seconds=expires_in)
                else:
                    # Fallback for testing/demo
                    self._token = f"mock_token_{self.session_id}_{int(time.time())}"
                    self._token_expiry = datetime.utcnow() + timedelta(hours=1)
                
                # Initialize platform API client
                self._client = self._create_api_client()
                
                self._is_authenticated = True
                self._last_refresh = datetime.utcnow()
                self._refresh_count += 1
                self._auth_failures = 0
                self._consecutive_failures = 0
                self._last_successful_call = datetime.utcnow()
                
                logger.info(f"[{self.session_id}] Authentication successful")
                return True
                
            except Exception as e:
                self._auth_failures += 1
                self._consecutive_failures += 1
                self._last_error = str(e)
                logger.error(f"[{self.session_id}] Authentication failed: {e}")
                
                # Emit anomaly signal
                self._emit_anomaly_signal("auth_failure", 1.0, 0.0)
                
                raise SessionAuthenticationError(f"Auth failed for {self.session_id}: {e}")
    
    def is_active(self) -> bool:
        """
        Check if session is currently active and valid.
        
        Returns:
            bool: True if session is active and not expired
        """
        with self._lock:
            if not self._is_authenticated or not self._token:
                return False
            
            # Check circuit breaker
            if self._circuit_breaker and self._circuit_breaker.get_state() == CircuitBreakerState.OPEN:
                return False
            
            # Check token expiry
            if self._token_expiry:
                now = datetime.utcnow()
                # Consider expired if within 5 minutes of expiry (buffer)
                if now >= (self._token_expiry - timedelta(minutes=5)):
                    logger.warning(f"[{self.session_id}] Token expired or expiring soon")
                    return False
            
            return True
    
    def refresh(self, force: bool = False) -> bool:
        """
        Refresh session token/cookies.
        
        Args:
            force: Force refresh even if session appears active
            
        Returns:
            bool: True if refresh succeeded
            
        Raises:
            SessionAuthenticationError: If refresh fails
            SessionCircuitBreakerOpen: If circuit breaker is open
        """
        if self._circuit_breaker:
            return self._circuit_breaker.call(self._refresh_internal, force)
        return self._refresh_internal(force)
    
    def _refresh_internal(self, force: bool = False) -> bool:
        """Internal refresh logic."""
        with self._lock:
            if not force and self.is_active():
                logger.debug(f"[{self.session_id}] Session active, skipping refresh")
                return True
            
            logger.info(f"[{self.session_id}] Refreshing session (force={force})")
            
            try:
                # Check rate limits
                if self._limits_manager:
                    if not self._limits_manager.can_refresh_token(self.platform_name, self.account_id):
                        raise SessionAuthenticationError(
                            f"Rate limit exceeded for token refresh: {self.session_id}"
                        )
                
                # Use auth_manager to refresh token
                if self._auth_manager:
                    refresh_result = self._auth_manager.refresh_token(
                        self.platform_name,
                        self.account_id,
                        self._token
                    )
                    self._token = refresh_result.get('token')
                    expires_in = refresh_result.get('expires_in', 3600)
                    self._token_expiry = datetime.utcnow() + timedelta(seconds=expires_in)
                else:
                    # Fallback for testing
                    self._token = f"refreshed_token_{self.session_id}_{int(time.time())}"
                    self._token_expiry = datetime.utcnow() + timedelta(hours=1)
                
                # Recreate API client with new token
                self._client = self._create_api_client()
                
                self._is_authenticated = True
                self._last_refresh = datetime.utcnow()
                self._refresh_count += 1
                self._auth_failures = 0
                self._consecutive_failures = 0
                self._last_successful_call = datetime.utcnow()
                
                logger.info(f"[{self.session_id}] Refresh successful (count: {self._refresh_count})")
                return True
                
            except Exception as e:
                self._auth_failures += 1
                self._consecutive_failures += 1
                self._last_error = str(e)
                self._is_authenticated = False
                logger.error(f"[{self.session_id}] Refresh failed: {e}")
                
                # Emit anomaly signal
                self._emit_anomaly_signal("refresh_failure", 1.0, 0.0)
                
                raise SessionAuthenticationError(f"Refresh failed for {self.session_id}: {e}")
    
    def get_client(self) -> Any:
        """
        Get the platform API client for making requests.
        
        Returns:
            API client object (platform-specific)
            
        Raises:
            SessionExpiredError: If session is not active
            SessionCircuitBreakerOpen: If circuit breaker is open
        """
        with self._lock:
            # Check rate limits
            if self._limits_manager:
                if not self._limits_manager.can_make_api_call(self.platform_name, self.account_id):
                    raise PlatformSessionError(
                        f"Rate limit exceeded for API calls: {self.session_id}"
                    )
            
            if not self.is_active():
                logger.warning(f"[{self.session_id}] Session inactive, attempting refresh")
                try:
                    self.refresh()
                except Exception as e:
                    raise SessionExpiredError(f"Session expired and refresh failed: {e}")
            
            if not self._client:
                raise PlatformSessionError(f"No client available for {self.session_id}")
            
            # Track API call
            self._last_api_call_time = time.time()
            self._api_call_count += 1
            
            return self._client
    
    def release(self):
        """
        Release session resources (optional for pooling).
        Does not invalidate the session, just marks it as available.
        """
        with self._lock:
            logger.debug(f"[{self.session_id}] Session released")
    
    def get_metrics(self) -> Dict[str, Any]:
        """
        Get session metrics for telemetry.
        
        Returns:
            dict: Session metrics including refresh count, failures, etc.
        """
        with self._lock:
            uptime = (datetime.utcnow() - self._created_at).total_seconds()
            time_since_refresh = None
            if self._last_refresh:
                time_since_refresh = (datetime.utcnow() - self._last_refresh).total_seconds()
            
            circuit_breaker_state = None
            if self._circuit_breaker:
                circuit_breaker_state = self._circuit_breaker.get_state().value
            
            return {
                'session_id': self.session_id,
                'platform': self.platform_name,
                'account_id': self.account_id,
                'is_active': self.is_active(),
                'is_authenticated': self._is_authenticated,
                'refresh_count': self._refresh_count,
                'auth_failures': self._auth_failures,
                'consecutive_failures': self._consecutive_failures,
                'uptime_seconds': uptime,
                'time_since_refresh_seconds': time_since_refresh,
                'last_error': self._last_error,
                'circuit_breaker_state': circuit_breaker_state,
                'created_at': self._created_at.isoformat(),
                'last_refresh': self._last_refresh.isoformat() if self._last_refresh else None,
                'last_successful_call': self._last_successful_call.isoformat() if self._last_successful_call else None
            }
    
    def _create_api_client(self) -> Any:
        """
        Create platform-specific API client.
        
        Returns:
            Platform API client instance
        """
        # Mock implementation - in production, this would return actual API clients
        # e.g., YouTube API client, TikTok API client, etc.
        
        class MockAPIClient:
            def __init__(self, platform, token):
                self.platform = platform
                self.token = token
                self.authenticated = True
            
            def __repr__(self):
                return f"<APIClient platform={self.platform} token={self.token[:20]}...>"
        
        return MockAPIClient(self.platform_name, self._token)
    
    def _emit_anomaly_signal(self, metric_name: str, metric_value: float, baseline_value: float):
        """Emit anomaly signal to anomaly detector if available."""
        if not self._anomaly_detector or not AnomalySignal:
            return
        
        try:
            signal = AnomalySignal(
                source="platform_sessions",
                timestamp=time.time(),
                platform=self.platform_name,
                account_id=self.account_id,
                intent_id=None,
                metric_name=metric_name,
                metric_value=metric_value,
                baseline_value=baseline_value,
                confidence=0.8
            )
            # Anomaly detector would process this signal
            # In a full implementation, we'd call: self._anomaly_detector.add_signal(signal)
            logger.debug(f"[{self.session_id}] Emitted anomaly signal: {metric_name}")
        except Exception as e:
            logger.warning(f"[{self.session_id}] Failed to emit anomaly signal: {e}")


# ============================================================================
# SESSION POOL
# ============================================================================

class SessionPool:
    """
    Manages multiple PlatformSession instances across workers.
    
    Responsibilities:
    - Atomic acquire/release to prevent race conditions
    - Health check to remove dead/expired sessions
    - Integration with platform_limits for rate safety
    - Distributed locking for multi-process safety
    - Background health monitoring
    """
    
    def __init__(
        self,
        auth_manager=None,
        limits_manager=None,
        anomaly_detector=None,
        enable_distributed_locking: bool = True,
        lock_storage_path: str = "/tmp/platform_session_locks",
        health_check_interval: float = 300.0  # 5 minutes
    ):
        """
        Initialize session pool.
        
        Args:
            auth_manager: AuthManager instance
            limits_manager: PlatformLimits instance
            anomaly_detector: AnomalyDetector instance
            enable_distributed_locking: Enable distributed locks for multi-process
            lock_storage_path: Path for distributed lock files
            health_check_interval: Seconds between background health checks
        """
        self._sessions: Dict[str, PlatformSession] = {}
        self._session_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._pool_lock = threading.RLock()
        self._auth_manager = auth_manager
        self._limits_manager = limits_manager
        self._anomaly_detector = anomaly_detector
        
        # Distributed locking
        self._distributed_lock = DistributedLock(lock_storage_path) if enable_distributed_locking else None
        
        # Pool metrics
        self._acquire_count = 0
        self._release_count = 0
        self._conflict_count = 0
        self._distributed_conflict_count = 0
        
        # Background health check
        self._health_check_interval = health_check_interval
        self._health_check_thread = None
        self._shutdown_event = threading.Event()
        self._start_background_health_check()
        
        logger.info("SessionPool initialized")
    
    def _start_background_health_check(self):
        """Start background thread for periodic health checks."""
        if self._health_check_interval > 0:
            self._health_check_thread = threading.Thread(
                target=self._background_health_check_loop,
                daemon=True
            )
            self._health_check_thread.start()
            logger.info(f"Background health check started (interval: {self._health_check_interval}s)")
    
    def _background_health_check_loop(self):
        """Background thread for periodic health checks."""
        while not self._shutdown_event.is_set():
            try:
                time.sleep(self._health_check_interval)
                if not self._shutdown_event.is_set():
                    self.health_check()
            except Exception as e:
                logger.error(f"Background health check error: {e}")
    
    def acquire(
        self,
        platform_name: str,
        account_id: str,
        timeout: float = 10.0
    ) -> PlatformSession:
        """
        Acquire a session for the given platform and account.
        
        Args:
            platform_name: Platform identifier
            account_id: Account identifier
            timeout: Maximum time to wait for lock (seconds)
            
        Returns:
            PlatformSession: Active session ready for use
            
        Raises:
            SessionConflictError: If session is already in use
            SessionAuthenticationError: If session cannot be authenticated
        """
        session_id = f"{platform_name.lower()}:{account_id}"
        
        # Try distributed lock first (for multi-process safety)
        distributed_lock_acquired = False
        if self._distributed_lock:
            distributed_lock_acquired = self._distributed_lock.acquire(session_id, timeout=timeout)
            if not distributed_lock_acquired:
                self._distributed_conflict_count += 1
                logger.warning(f"[{session_id}] Distributed lock conflict - held by another process")
                raise SessionConflictError(
                    f"Session {session_id} is locked by another process"
                )
        
        # Acquire thread-level lock
        session_lock = self._session_locks[session_id]
        acquired = session_lock.acquire(blocking=False)
        
        if not acquired:
            if distributed_lock_acquired:
                self._distributed_lock.release(session_id)
            self._conflict_count += 1
            logger.warning(f"[{session_id}] Session conflict - already in use by another thread")
            raise SessionConflictError(f"Session {session_id} is already in use")
        
        try:
            with self._pool_lock:
                # Get or create session
                if session_id not in self._sessions:
                    logger.info(f"[{session_id}] Creating new session")
                    session = PlatformSession(
                        platform_name,
                        account_id,
                        self._auth_manager,
                        self._limits_manager,
                        self._anomaly_detector
                    )
                    session.authenticate()
                    self._sessions[session_id] = session
                else:
                    session = self._sessions[session_id]
                    logger.debug(f"[{session_id}] Reusing existing session")
                
                # Ensure session is active
                if not session.is_active():
                    logger.info(f"[{session_id}] Session inactive, refreshing")
                    try:
                        session.refresh()
                    except Exception as e:
                        logger.error(f"[{session_id}] Failed to refresh during acquire: {e}")
                        # Remove dead session
                        del self._sessions[session_id]
                        raise SessionAuthenticationError(f"Session refresh failed: {e}")
                
                self._acquire_count += 1
                logger.info(f"[{session_id}] Session acquired (total acquires: {self._acquire_count})")
                
                return session
                
        except Exception as e:
            # Release locks on error
            session_lock.release()
            if distributed_lock_acquired and self._distributed_lock:
                self._distributed_lock.release(session_id)
            logger.error(f"[{session_id}] Failed to acquire session: {e}")
            raise
    
    def release(self, session: PlatformSession) -> None:
        """
        Release a session back to the pool.
        
        Args:
            session: PlatformSession to release
        """
        session_id = session.session_id
        session_lock = self._session_locks[session_id]
        
        try:
            session.release()
            self._release_count += 1
            logger.info(f"[{session_id}] Session released (total releases: {self._release_count})")
        finally:
            session_lock.release()
            # Release distributed lock
            if self._distributed_lock:
                self._distributed_lock.release(session_id)
    
    def health_check(self) -> Dict[str, Any]:
        """
        Validate all sessions in pool and remove dead/expired ones.
        
        Returns:
            dict: Health check results including active, expired, and removed sessions
        """
        with self._pool_lock:
            logger.info("Running session pool health check")
            
            active_sessions = []
            expired_sessions = []
            removed_sessions = []
            circuit_breaker_open = []
            
            for session_id, session in list(self._sessions.items()):
                try:
                    # Check circuit breaker
                    if session._circuit_breaker:
                        if session._circuit_breaker.get_state() == CircuitBreakerState.OPEN:
                            circuit_breaker_open.append(session_id)
                    
                    if session.is_active():
                        active_sessions.append(session_id)
                    else:
                        expired_sessions.append(session_id)
                        # Attempt to refresh
                        try:
                            session.refresh()
                            logger.info(f"[{session_id}] Refreshed expired session")
                            active_sessions.append(session_id)
                            expired_sessions.remove(session_id)
                        except Exception as e:
                            logger.warning(f"[{session_id}] Cannot refresh, removing: {e}")
                            removed_sessions.append(session_id)
                            del self._sessions[session_id]
                            
                except Exception as e:
                    logger.error(f"[{session_id}] Health check error: {e}")
                    removed_sessions.append(session_id)
                    del self._sessions[session_id]
            
            results = {
                'total_sessions': len(self._sessions),
                'active_sessions': len(active_sessions),
                'expired_sessions': len(expired_sessions),
                'removed_sessions': len(removed_sessions),
                'circuit_breaker_open': len(circuit_breaker_open),
                'active_session_ids': active_sessions,
                'expired_session_ids': expired_sessions,
                'removed_session_ids': removed_sessions,
                'circuit_breaker_open_ids': circuit_breaker_open,
                'timestamp': datetime.utcnow().isoformat()
            }
            
            logger.info(
                f"Health check complete: {results['total_sessions']} total, "
                f"{results['active_sessions']} active, {results['removed_sessions']} removed, "
                f"{results['circuit_breaker_open']} circuit breakers open"
            )
            
            return results
    
    def get_pool_metrics(self) -> Dict[str, Any]:
        """
        Get overall pool metrics for telemetry.
        
        Returns:
            dict: Pool metrics including acquire/release counts, conflicts, etc.
        """
        with self._pool_lock:
            return {
                'total_sessions': len(self._sessions),
                'acquire_count': self._acquire_count,
                'release_count': self._release_count,
                'conflict_count': self._conflict_count,
                'distributed_conflict_count': self._distributed_conflict_count,
                'session_details': [
                    session.get_metrics() 
                    for session in self._sessions.values()
                ]
            }
    
    def close_all(self):
        """Close all sessions in the pool. Used for cleanup/shutdown."""
        self._shutdown_event.set()
        
        if self._health_check_thread:
            self._health_check_thread.join(timeout=5.0)
        
        with self._pool_lock:
            logger.info(f"Closing all {len(self._sessions)} sessions")
            for session_id in list(self._sessions.keys()):
                try:
                    session = self._sessions[session_id]
                    session.release()
                    del self._sessions[session_id]
                    # Release distributed lock
                    if self._distributed_lock:
                        self._distributed_lock.release(session_id)
                except Exception as e:
                    logger.error(f"[{session_id}] Error closing session: {e}")
            
            logger.info("All sessions closed")


# ============================================================================
# SESSION VALIDATOR
# ============================================================================

class SessionValidator:
    """
    Validates session consistency, expiry, and multi-worker conflicts.
    
    Raises alerts to monitoring and anomaly detection for invalid states.
    """
    
    @staticmethod
    def validate_session(session: PlatformSession) -> Dict[str, Any]:
        """
        Validate a session's state.
        
        Args:
            session: PlatformSession to validate
            
        Returns:
            dict: Validation results with status and any issues
        """
        issues = []
        warnings = []
        
        # Check authentication status
        if not session._is_authenticated:
            issues.append("Session not authenticated")
        
        # Check token expiry
        if session._token_expiry:
            now = datetime.utcnow()
            time_until_expiry = (session._token_expiry - now).total_seconds()
            
            if time_until_expiry < 0:
                issues.append(f"Token expired {abs(time_until_expiry):.0f}s ago")
            elif time_until_expiry < 300:  # 5 minutes
                warnings.append(f"Token expires in {time_until_expiry:.0f}s")
        
        # Check for excessive auth failures
        if session._auth_failures > 3:
            issues.append(f"Excessive auth failures: {session._auth_failures}")
        
        # Check circuit breaker
        if session._circuit_breaker:
            if session._circuit_breaker.get_state() == CircuitBreakerState.OPEN:
                issues.append("Circuit breaker is OPEN")
            elif session._circuit_breaker.get_state() == CircuitBreakerState.HALF_OPEN:
                warnings.append("Circuit breaker is HALF_OPEN")
        
        # Check client availability
        if session._is_authenticated and not session._client:
            issues.append("Authenticated but no client available")
        
        # Check last refresh time
        if session._last_refresh:
            time_since_refresh = (datetime.utcnow() - session._last_refresh).total_seconds()
            if time_since_refresh > 3600:  # 1 hour
                warnings.append(f"No refresh in {time_since_refresh/60:.0f} minutes")
        
        # Check consecutive failures
        if session._consecutive_failures > 5:
            issues.append(f"High consecutive failures: {session._consecutive_failures}")
        
        is_valid = len(issues) == 0
        status = "valid" if is_valid else "invalid"
        
        result = {
            'session_id': session.session_id,
            'status': status,
            'is_valid': is_valid,
            'issues': issues,
            'warnings': warnings,
            'timestamp': datetime.utcnow().isoformat()
        }
        
        if issues:
            logger.warning(f"[{session.session_id}] Validation failed: {issues}")
        
        return result
    
    @staticmethod
    def validate_pool(pool: SessionPool) -> Dict[str, Any]:
        """
        Validate all sessions in a pool.
        
        Args:
            pool: SessionPool to validate
            
        Returns:
            dict: Validation results for all sessions
        """
        logger.info("Validating session pool")
        
        results = []
        invalid_count = 0
        warning_count = 0
        
        with pool._pool_lock:
            for session in pool._sessions.values():
                validation = SessionValidator.validate_session(session)
                results.append(validation)
                
                if not validation['is_valid']:
                    invalid_count += 1
                if validation['warnings']:
                    warning_count += 1
        
        summary = {
            'total_sessions': len(results),
            'valid_sessions': len(results) - invalid_count,
            'invalid_sessions': invalid_count,
            'sessions_with_warnings': warning_count,
            'validations': results,
            'timestamp': datetime.utcnow().isoformat()
        }
        
        logger.info(f"Pool validation complete: {summary['valid_sessions']}/{summary['total_sessions']} valid")
        
        return summary


# ============================================================================
# METRICS EMITTER
# ============================================================================

class MetricsEmitter:
    """
    Exposes session health metrics for platform_telemetry.py.
    
    Metrics include:
    - Active sessions
    - Expired sessions  
    - Failed refresh attempts
    - Session conflicts prevented
    - Circuit breaker states
    """
    
    def __init__(self, pool: SessionPool):
        """
        Initialize metrics emitter.
        
        Args:
            pool: SessionPool to monitor
        """
        self.pool = pool
        logger.info("MetricsEmitter initialized")
    
    def emit_metrics(self) -> Dict[str, Any]:
        """
        Emit current session metrics.
        
        Returns:
            dict: Complete metrics snapshot
        """
        pool_metrics = self.pool.get_pool_metrics()
        health_check = self.pool.health_check()
        validation = SessionValidator.validate_pool(self.pool)
        
        metrics = {
            'timestamp': datetime.utcnow().isoformat(),
            'pool': {
                'total_sessions': pool_metrics['total_sessions'],
                'acquire_count': pool_metrics['acquire_count'],
                'release_count': pool_metrics['release_count'],
                'conflict_count': pool_metrics['conflict_count'],
                'distributed_conflict_count': pool_metrics['distributed_conflict_count']
            },
            'health': {
                'active_sessions': health_check['active_sessions'],
                'expired_sessions': health_check['expired_sessions'],
                'removed_sessions': health_check['removed_sessions'],
                'circuit_breaker_open': health_check['circuit_breaker_open']
            },
            'validation': {
                'valid_sessions': validation['valid_sessions'],
                'invalid_sessions': validation['invalid_sessions'],
                'sessions_with_warnings': validation['sessions_with_warnings']
            },
            'sessions': pool_metrics['session_details']
        }
        
        logger.debug(f"Metrics emitted: {metrics['pool']['total_sessions']} sessions")
        
        return metrics
    
    def get_session_failures(self) -> Dict[str, int]:
        """
        Get count of sessions with authentication failures.
        
        Returns:
            dict: Session ID to failure count mapping
        """
        failures = {}
        
        with self.pool._pool_lock:
            for session_id, session in self.pool._sessions.items():
                if session._auth_failures > 0:
                    failures[session_id] = session._auth_failures
        
        return failures
    
    def get_conflict_metrics(self) -> Dict[str, Any]:
        """
        Get metrics about session conflicts.
        
        Returns:
            dict: Conflict-related metrics
        """
        return {
            'total_conflicts': self.pool._conflict_count,
            'distributed_conflicts': self.pool._distributed_conflict_count,
            'timestamp': datetime.utcnow().isoformat()
        }
    
    def get_circuit_breaker_metrics(self) -> Dict[str, Any]:
        """
        Get metrics about circuit breaker states.
        
        Returns:
            dict: Circuit breaker metrics
        """
        circuit_breaker_stats = {
            'open': 0,
            'half_open': 0,
            'closed': 0
        }
        
        with self.pool._pool_lock:
            for session in self.pool._sessions.values():
                if session._circuit_breaker:
                    state = session._circuit_breaker.get_state()
                    circuit_breaker_stats[state.value] = circuit_breaker_stats.get(state.value, 0) + 1
        
        return {
            'circuit_breaker_stats': circuit_breaker_stats,
            'timestamp': datetime.utcnow().isoformat()
        }


# ============================================================================
# USAGE EXAMPLES
# ============================================================================

if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    print("=== PlatformSession Examples ===\n")
    
    # Example 1: Basic session usage
    print("1. Basic Session Usage:")
    session = PlatformSession("youtube", "acct_123")
    session.authenticate()
    print(f"   Is active: {session.is_active()}")
    client = session.get_client()
    print(f"   Client: {client}")
    print(f"   Metrics: {session.get_metrics()}\n")
    
    # Example 2: Session pool with multiple platforms
    print("2. Session Pool Usage:")
    pool = SessionPool()
    
    # Acquire sessions for different platforms
    yt_session = pool.acquire("youtube", "acct_001")
    print(f"   Acquired YouTube session: {yt_session.session_id}")
    
    tt_session = pool.acquire("tiktok", "acct_002")
    print(f"   Acquired TikTok session: {tt_session.session_id}")
    
    # Release sessions
    pool.release(yt_session)
    pool.release(tt_session)
    print("   Sessions released\n")
    
    # Example 3: Health check
    print("3. Health Check:")
    health = pool.health_check()
    print(f"   Total sessions: {health['total_sessions']}")
    print(f"   Active sessions: {health['active_sessions']}\n")
    
    # Example 4: Metrics emission
    print("4. Metrics Emission:")
    emitter = MetricsEmitter(pool)
    metrics = emitter.emit_metrics()
    print(f"   Pool metrics: {metrics['pool']}")
    print(f"   Health metrics: {metrics['health']}\n")
    
    # Example 5: Validation
    print("5. Session Validation:")
    validation = SessionValidator.validate_pool(pool)
    print(f"   Valid sessions: {validation['valid_sessions']}/{validation['total_sessions']}")
    
    # Example 6: Circuit breaker metrics
    print("6. Circuit Breaker Metrics:")
    cb_metrics = emitter.get_circuit_breaker_metrics()
    print(f"   Circuit breaker stats: {cb_metrics['circuit_breaker_stats']}\n")
    
    # Cleanup
    pool.close_all()
    print("\n=== Examples Complete ===")