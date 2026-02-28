"""
/infra/persistence/lock_manager.py

Distributed Lock & Lease Authority

This module provides authoritative distributed locking semantics across:
- Distributed workers
- Concurrent workflows
- Migrations
- Snapshots
- Rollouts
- Replays
- Account enforcement

Core principle: A lock without an expiration is a deadlock waiting for a crash.

Locks define ownership.
Leases define time.
Expiration defines safety.
Loss defines failure.
Audit defines reality.
"""

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Callable, Any
import logging
import uuid


logger = logging.getLogger(__name__)


# ============================================================================
# Core Enums
# ============================================================================


class LockScope(Enum):
    """
    Defines the blast radius of a lock.
    
    Too broad → throughput loss
    Too narrow → race conditions
    """
    GLOBAL = "global"
    SCHEMA = "schema"
    SNAPSHOT = "snapshot"
    MIGRATION = "migration"
    WORKFLOW = "workflow"
    ACCOUNT = "account"
    EXPERIMENT = "experiment"


class LockState(Enum):
    """
    Lock lifecycle states.
    
    LOST is not normal — it is an event that must abort execution.
    """
    ACQUIRED = "acquired"
    RELEASED = "released"
    EXPIRED = "expired"
    LOST = "lost"


# ============================================================================
# Core Data Structures
# ============================================================================


@dataclass(frozen=True)
class LockRequest:
    """
    Immutable lock acquisition request.
    
    Rules:
    - TTL required (no infinite locks)
    - Owner explicitly declared (no anonymous locks)
    - Reason is audited
    """
    scope: LockScope
    scope_id: str
    owner_id: str
    ttl_seconds: int
    reason: str
    
    def __post_init__(self):
        """Validate lock request invariants."""
        if self.ttl_seconds <= 0:
            raise ValueError(f"TTL must be positive, got {self.ttl_seconds}")
        if self.ttl_seconds > 86400:  # 24 hours max
            raise ValueError(f"TTL cannot exceed 24 hours, got {self.ttl_seconds}")
        if not self.owner_id:
            raise ValueError("owner_id is required (no anonymous locks)")
        if not self.scope_id:
            raise ValueError("scope_id is required")
        if not self.reason:
            raise ValueError("reason is required for audit trail")
    
    @property
    def lock_key(self) -> str:
        """Generate the unique lock identifier."""
        return f"{self.scope.value}:{self.scope_id}"


@dataclass
class LockHandle:
    """
    Mutable lock state handle.
    
    Handles have:
    - Immutable IDs
    - Mutable state (only via manager)
    - Never user-modified
    """
    lock_id: str
    scope: LockScope
    scope_id: str
    owner_id: str
    acquired_at: int  # Unix timestamp (seconds)
    expires_at: int   # Unix timestamp (seconds)
    state: LockState
    ttl_seconds: int
    reason: str
    
    @property
    def is_valid(self) -> bool:
        """Check if lock is still valid (not expired or lost)."""
        if self.state in (LockState.RELEASED, LockState.EXPIRED, LockState.LOST):
            return False
        current_time = int(time.time())
        return current_time < self.expires_at
    
    @property
    def time_until_expiry(self) -> int:
        """Seconds until lock expires."""
        current_time = int(time.time())
        return max(0, self.expires_at - current_time)
    
    @property
    def lock_key(self) -> str:
        """Get the lock key for this handle."""
        return f"{self.scope.value}:{self.scope_id}"


# ============================================================================
# Lock Backend Abstraction
# ============================================================================


class LockBackend(ABC):
    """
    Abstract lock backend.
    
    Implementations may use:
    - Redis
    - Postgres
    - ZooKeeper
    - etcd
    
    But semantics must not vary.
    """
    
    @abstractmethod
    def acquire(self, request: LockRequest) -> Optional[LockHandle]:
        """
        Attempt to acquire exclusive lock.
        
        Returns:
            LockHandle if acquired, None if already held by another owner
        
        Raises:
            Exception if backend unavailable
        """
        pass
    
    @abstractmethod
    def refresh(self, handle: LockHandle) -> Optional[LockHandle]:
        """
        Refresh/extend lock TTL.
        
        Returns:
            Updated LockHandle if successful, None if lock lost
        """
        pass
    
    @abstractmethod
    def release(self, handle: LockHandle) -> bool:
        """
        Release lock.
        
        Returns:
            True if released, False if already released or not owner
        """
        pass
    
    @abstractmethod
    def check_lock(self, lock_key: str) -> Optional[Dict[str, Any]]:
        """
        Check current lock state.
        
        Returns:
            Lock metadata if held, None if free
        """
        pass


# ============================================================================
# In-Memory Backend (for testing and single-instance deployments)
# ============================================================================


class InMemoryLockBackend(LockBackend):
    """
    In-memory lock backend for testing and single-instance use.
    
    NOT suitable for distributed systems.
    """
    
    def __init__(self):
        self._locks: Dict[str, LockHandle] = {}
        self._lock = threading.Lock()
    
    def acquire(self, request: LockRequest) -> Optional[LockHandle]:
        """Acquire lock in memory."""
        with self._lock:
            lock_key = request.lock_key
            
            # Check if lock exists and is still valid
            if lock_key in self._locks:
                existing = self._locks[lock_key]
                if existing.is_valid:
                    logger.warning(
                        f"Lock {lock_key} already held by {existing.owner_id}, "
                        f"requested by {request.owner_id}"
                    )
                    return None
                else:
                    # Expired, clean it up
                    del self._locks[lock_key]
            
            # Acquire new lock
            current_time = int(time.time())
            handle = LockHandle(
                lock_id=str(uuid.uuid4()),
                scope=request.scope,
                scope_id=request.scope_id,
                owner_id=request.owner_id,
                acquired_at=current_time,
                expires_at=current_time + request.ttl_seconds,
                state=LockState.ACQUIRED,
                ttl_seconds=request.ttl_seconds,
                reason=request.reason
            )
            
            self._locks[lock_key] = handle
            return handle
    
    def refresh(self, handle: LockHandle) -> Optional[LockHandle]:
        """Refresh lock TTL."""
        with self._lock:
            lock_key = handle.lock_key
            
            if lock_key not in self._locks:
                return None
            
            existing = self._locks[lock_key]
            
            # Verify ownership
            if existing.lock_id != handle.lock_id or existing.owner_id != handle.owner_id:
                return None
            
            # Check if expired
            if not existing.is_valid:
                del self._locks[lock_key]
                return None
            
            # Refresh
            current_time = int(time.time())
            existing.expires_at = current_time + handle.ttl_seconds
            
            return existing
    
    def release(self, handle: LockHandle) -> bool:
        """Release lock."""
        with self._lock:
            lock_key = handle.lock_key
            
            if lock_key not in self._locks:
                return False
            
            existing = self._locks[lock_key]
            
            # Verify ownership
            if existing.lock_id != handle.lock_id or existing.owner_id != handle.owner_id:
                logger.warning(
                    f"Cannot release lock {lock_key}: ownership mismatch "
                    f"(expected {handle.owner_id}, got {existing.owner_id})"
                )
                return False
            
            existing.state = LockState.RELEASED
            del self._locks[lock_key]
            return True
    
    def check_lock(self, lock_key: str) -> Optional[Dict[str, Any]]:
        """Check lock state."""
        with self._lock:
            if lock_key not in self._locks:
                return None
            
            handle = self._locks[lock_key]
            
            # Clean up if expired
            if not handle.is_valid:
                del self._locks[lock_key]
                return None
            
            return {
                "lock_id": handle.lock_id,
                "owner_id": handle.owner_id,
                "acquired_at": handle.acquired_at,
                "expires_at": handle.expires_at,
                "ttl_seconds": handle.ttl_seconds,
                "reason": handle.reason
            }


# ============================================================================
# Lock Invariants Enforcement
# ============================================================================


class LockInvariantViolation(Exception):
    """Raised when a lock invariant is violated."""
    pass


class LockInvariants:
    """
    Enforces absolute lock invariants.
    
    Violations result in hard failures + watchdog alerts.
    """
    
    @staticmethod
    def validate_acquisition(request: LockRequest) -> None:
        """Validate lock acquisition request."""
        # No infinite TTLs
        if request.ttl_seconds <= 0 or request.ttl_seconds > 86400:
            raise LockInvariantViolation(
                f"TTL must be between 1 and 86400 seconds, got {request.ttl_seconds}"
            )
        
        # No anonymous locks
        if not request.owner_id:
            raise LockInvariantViolation("owner_id required (no anonymous locks)")
        
        # Audit trail required
        if not request.reason:
            raise LockInvariantViolation("reason required for audit")
    
    @staticmethod
    def validate_handle(handle: LockHandle) -> None:
        """Validate lock handle state."""
        # No expired lock usage
        if not handle.is_valid and handle.state == LockState.ACQUIRED:
            raise LockInvariantViolation(
                f"Cannot use expired lock {handle.lock_id}"
            )
        
        # State consistency
        if handle.state == LockState.ACQUIRED and handle.expires_at <= int(time.time()):
            raise LockInvariantViolation(
                f"Lock {handle.lock_id} marked ACQUIRED but expired"
            )
    
    @staticmethod
    def validate_release(handle: LockHandle, success: bool) -> None:
        """Validate lock release."""
        if not success:
            logger.error(
                f"Failed to release lock {handle.lock_id} for {handle.owner_id}. "
                f"This may indicate ownership loss or backend failure."
            )
            # Alert watchdog
            LockInvariants._alert_watchdog(
                "lock_release_failed",
                {
                    "lock_id": handle.lock_id,
                    "owner_id": handle.owner_id,
                    "lock_key": handle.lock_key
                }
            )
    
    @staticmethod
    def _alert_watchdog(event_type: str, metadata: Dict[str, Any]) -> None:
        """Alert watchdog system of invariant violation."""
        logger.critical(
            f"LOCK INVARIANT VIOLATION: {event_type}",
            extra={"metadata": metadata}
        )
        # In production, this would integrate with actual watchdog/alerting


# ============================================================================
# Lease Renewal
# ============================================================================


class LeaseRenewer:
    """
    Automatic lease renewal for long-running operations.
    
    Rules:
    - Renewal happens before expiration
    - Missed renewal → lock considered LOST
    - Renewed TTL must match original
    """
    
    def __init__(
        self,
        backend: LockBackend,
        renewal_callback: Optional[Callable[[LockHandle, bool], None]] = None
    ):
        self._backend = backend
        self._renewal_callback = renewal_callback
        self._renewals: Dict[str, threading.Thread] = {}
        self._stop_flags: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
    
    def start(self, handle: LockHandle, renewal_interval_ratio: float = 0.5) -> None:
        """
        Start automatic renewal for a lock.
        
        Args:
            handle: Lock to renew
            renewal_interval_ratio: Fraction of TTL to wait between renewals (default 0.5)
        """
        if renewal_interval_ratio <= 0 or renewal_interval_ratio >= 1:
            raise ValueError("renewal_interval_ratio must be between 0 and 1")
        
        with self._lock:
            lock_key = handle.lock_key
            
            if lock_key in self._renewals:
                logger.warning(f"Renewal already started for {lock_key}")
                return
            
            stop_flag = threading.Event()
            self._stop_flags[lock_key] = stop_flag
            
            renewal_thread = threading.Thread(
                target=self._renewal_loop,
                args=(handle, renewal_interval_ratio, stop_flag),
                daemon=True,
                name=f"LeaseRenewer-{lock_key}"
            )
            
            self._renewals[lock_key] = renewal_thread
            renewal_thread.start()
            
            logger.info(
                f"Started lease renewal for {lock_key} "
                f"(interval: {handle.ttl_seconds * renewal_interval_ratio:.1f}s)"
            )
    
    def stop(self, handle: LockHandle) -> None:
        """Stop automatic renewal for a lock."""
        with self._lock:
            lock_key = handle.lock_key
            
            if lock_key not in self._stop_flags:
                return
            
            self._stop_flags[lock_key].set()
            
            if lock_key in self._renewals:
                # Wait for thread to finish (with timeout)
                self._renewals[lock_key].join(timeout=5.0)
                del self._renewals[lock_key]
            
            del self._stop_flags[lock_key]
            
            logger.info(f"Stopped lease renewal for {lock_key}")
    
    def _renewal_loop(
        self,
        handle: LockHandle,
        renewal_interval_ratio: float,
        stop_flag: threading.Event
    ) -> None:
        """Background renewal loop."""
        renewal_interval = handle.ttl_seconds * renewal_interval_ratio
        
        while not stop_flag.is_set():
            # Wait for renewal interval
            if stop_flag.wait(timeout=renewal_interval):
                break
            
            # Attempt renewal
            try:
                refreshed = self._backend.refresh(handle)
                
                if refreshed is None:
                    # Lock lost!
                    logger.error(
                        f"Lock renewal failed for {handle.lock_key}: lock lost. "
                        f"Owner: {handle.owner_id}, Lock ID: {handle.lock_id}"
                    )
                    handle.state = LockState.LOST
                    
                    # Notify callback
                    if self._renewal_callback:
                        self._renewal_callback(handle, False)
                    
                    # Alert watchdog
                    LockInvariants._alert_watchdog(
                        "lock_lost_during_renewal",
                        {
                            "lock_id": handle.lock_id,
                            "owner_id": handle.owner_id,
                            "lock_key": handle.lock_key,
                            "reason": handle.reason
                        }
                    )
                    break
                else:
                    logger.debug(f"Successfully renewed lock {handle.lock_key}")
                    
                    # Notify callback
                    if self._renewal_callback:
                        self._renewal_callback(handle, True)
            
            except Exception as e:
                logger.error(
                    f"Exception during lock renewal for {handle.lock_key}: {e}",
                    exc_info=True
                )
                handle.state = LockState.LOST
                
                if self._renewal_callback:
                    self._renewal_callback(handle, False)
                
                break


# ============================================================================
# Lock Manager (Public Authority)
# ============================================================================


class LockManager:
    """
    Distributed lock manager - the authoritative control gate.
    
    Guarantees:
    - Exclusive ownership
    - Immediate failure on conflict
    - No blocking waits by default
    - Audit emission on all operations
    """
    
    def __init__(
        self,
        backend: LockBackend,
        audit_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None
    ):
        self._backend = backend
        self._audit_callback = audit_callback
        self._lease_renewer = LeaseRenewer(backend)
    
    def acquire_lock(
        self,
        request: LockRequest,
        auto_renew: bool = False,
        renewal_interval_ratio: float = 0.5
    ) -> LockHandle:
        """
        Acquire exclusive distributed lock.
        
        Args:
            request: Lock acquisition request
            auto_renew: Enable automatic lease renewal
            renewal_interval_ratio: Fraction of TTL for renewal interval
        
        Returns:
            LockHandle if acquired
        
        Raises:
            LockInvariantViolation: Invalid request
            RuntimeError: Lock already held or backend unavailable
        """
        # Validate invariants
        LockInvariants.validate_acquisition(request)
        
        # Emit audit event
        self._emit_audit("lock_acquire_attempt", {
            "scope": request.scope.value,
            "scope_id": request.scope_id,
            "owner_id": request.owner_id,
            "ttl_seconds": request.ttl_seconds,
            "reason": request.reason
        })
        
        try:
            # Attempt acquisition
            handle = self._backend.acquire(request)
            
            if handle is None:
                # Lock already held
                self._emit_audit("lock_acquire_failed", {
                    "scope": request.scope.value,
                    "scope_id": request.scope_id,
                    "owner_id": request.owner_id,
                    "reason": "already_held"
                })
                raise RuntimeError(
                    f"Lock {request.lock_key} already held by another owner"
                )
            
            # Success
            self._emit_audit("lock_acquired", {
                "lock_id": handle.lock_id,
                "scope": handle.scope.value,
                "scope_id": handle.scope_id,
                "owner_id": handle.owner_id,
                "acquired_at": handle.acquired_at,
                "expires_at": handle.expires_at,
                "ttl_seconds": handle.ttl_seconds,
                "reason": handle.reason
            })
            
            logger.info(
                f"Acquired lock {handle.lock_key} for {handle.owner_id} "
                f"(TTL: {handle.ttl_seconds}s, expires: {handle.expires_at})"
            )
            
            # Start auto-renewal if requested
            if auto_renew:
                self._lease_renewer.start(handle, renewal_interval_ratio)
            
            return handle
        
        except Exception as e:
            if "already held" not in str(e):
                # Backend failure
                self._emit_audit("lock_acquire_error", {
                    "scope": request.scope.value,
                    "scope_id": request.scope_id,
                    "owner_id": request.owner_id,
                    "error": str(e)
                })
                raise RuntimeError(f"Backend unavailable during lock acquisition: {e}")
            raise
    
    def release_lock(self, handle: LockHandle) -> None:
        """
        Release exclusive lock.
        
        Args:
            handle: Lock to release
        
        Raises:
            LockInvariantViolation: Invalid handle
        """
        # Stop auto-renewal if active
        self._lease_renewer.stop(handle)
        
        # Attempt release
        success = self._backend.release(handle)
        
        # Validate
        LockInvariants.validate_release(handle, success)
        
        # Emit audit
        if success:
            self._emit_audit("lock_released", {
                "lock_id": handle.lock_id,
                "scope": handle.scope.value,
                "scope_id": handle.scope_id,
                "owner_id": handle.owner_id,
                "acquired_at": handle.acquired_at,
                "released_at": int(time.time()),
                "reason": handle.reason
            })
            
            logger.info(f"Released lock {handle.lock_key} for {handle.owner_id}")
        else:
            self._emit_audit("lock_release_failed", {
                "lock_id": handle.lock_id,
                "scope": handle.scope.value,
                "scope_id": handle.scope_id,
                "owner_id": handle.owner_id
            })
    
    def assert_lock_valid(self, handle: LockHandle) -> None:
        """
        Assert that lock is still valid.
        
        Raises:
            LockInvariantViolation: Lock expired or lost
        """
        LockInvariants.validate_handle(handle)
        
        if handle.state == LockState.LOST:
            raise LockInvariantViolation(
                f"Lock {handle.lock_id} has been LOST - execution must abort"
            )
        
        if handle.state in (LockState.RELEASED, LockState.EXPIRED):
            raise LockInvariantViolation(
                f"Lock {handle.lock_id} is {handle.state.value} - cannot use"
            )
        
        if not handle.is_valid:
            handle.state = LockState.EXPIRED
            raise LockInvariantViolation(
                f"Lock {handle.lock_id} has expired - execution must abort"
            )
    
    def refresh_lock(self, handle: LockHandle) -> LockHandle:
        """
        Manually refresh lock TTL.
        
        Returns:
            Updated handle
        
        Raises:
            RuntimeError: Refresh failed (lock lost)
        """
        refreshed = self._backend.refresh(handle)
        
        if refreshed is None:
            handle.state = LockState.LOST
            self._emit_audit("lock_refresh_failed", {
                "lock_id": handle.lock_id,
                "owner_id": handle.owner_id,
                "lock_key": handle.lock_key
            })
            raise RuntimeError(f"Lock {handle.lock_key} lost during refresh")
        
        self._emit_audit("lock_refreshed", {
            "lock_id": handle.lock_id,
            "owner_id": handle.owner_id,
            "lock_key": handle.lock_key,
            "new_expires_at": refreshed.expires_at
        })
        
        return refreshed
    
    def check_lock(self, scope: LockScope, scope_id: str) -> Optional[Dict[str, Any]]:
        """
        Check if a lock is currently held.
        
        Returns:
            Lock metadata if held, None if free
        """
        lock_key = f"{scope.value}:{scope_id}"
        return self._backend.check_lock(lock_key)
    
    def _emit_audit(self, event_type: str, metadata: Dict[str, Any]) -> None:
        """Emit audit event."""
        audit_entry = {
            "event": event_type,
            "timestamp": int(time.time()),
            "metadata": metadata
        }
        
        logger.info(f"AUDIT: {event_type}", extra=audit_entry)
        
        if self._audit_callback:
            self._audit_callback(event_type, metadata)


# ============================================================================
# Context Manager Support
# ============================================================================


class LockContext:
    """
    Context manager for lock acquisition/release.
    
    Usage:
        with LockContext(manager, request) as handle:
            # Protected operation
            ...
        # Lock automatically released
    """
    
    def __init__(
        self,
        manager: LockManager,
        request: LockRequest,
        auto_renew: bool = False
    ):
        self._manager = manager
        self._request = request
        self._auto_renew = auto_renew
        self._handle: Optional[LockHandle] = None
    
    def __enter__(self) -> LockHandle:
        """Acquire lock on context entry."""
        self._handle = self._manager.acquire_lock(self._request, self._auto_renew)
        return self._handle
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Release lock on context exit."""
        if self._handle:
            try:
                self._manager.release_lock(self._handle)
            except Exception as e:
                logger.error(f"Error releasing lock in context manager: {e}")
        return False


# ============================================================================
# Utility Functions
# ============================================================================


def create_lock_manager(
    backend_type: str = "inmemory",
    **backend_kwargs
) -> LockManager:
    """
    Factory function to create lock manager with specified backend.
    
    Args:
        backend_type: Type of backend ("inmemory", "redis", "postgres", etc.)
        **backend_kwargs: Backend-specific configuration
    
    Returns:
        Configured LockManager
    """
    if backend_type == "inmemory":
        backend = InMemoryLockBackend()
    else:
        raise ValueError(f"Unsupported backend type: {backend_type}")
    
    return LockManager(backend)


# ============================================================================
# Module Exports
# ============================================================================


__all__ = [
    # Enums
    "LockScope",
    "LockState",
    # Data structures
    "LockRequest",
    "LockHandle",
    # Backend
    "LockBackend",
    "InMemoryLockBackend",
    # Manager
    "LockManager",
    # Renewal
    "LeaseRenewer",
    # Invariants
    "LockInvariants",
    "LockInvariantViolation",
    # Context manager
    "LockContext",
    # Factory
    "create_lock_manager",
]