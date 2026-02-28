"""
redis_backend.py - Ephemeral & Coordination State Backend

Location: /infra/persistence/backends/redis_backend.py

Purpose:
    Fast, volatile, non-authoritative coordination storage.
    
    Used for:
        - Locks
        - Cursors
        - Temporary workflow state
        - Throttling markers
        - Inflight deduplication

What this backend IS:
    ✓ Fast
    ✓ Volatile
    ✓ Non-authoritative
    ✓ Coordination-focused

What this backend is NOT:
    ❌ Source of truth
    ❌ Snapshot target
    ❌ Migration target
    ❌ Audit storage

If Redis data disappears — system must survive.

Core Responsibilities:
    1. Support fast reads/writes
    2. Enforce TTL policies
    3. Provide best-effort atomic ops
    4. Integrate with watchdogs
    5. Fail gracefully

Determinism Rules (CRITICAL):
    - Redis state is never replayed
    - Redis is never snapshotted
    - Redis is never migrated
    
    Any logic requiring determinism must not depend on Redis values.

Failure Semantics:
    - Timeout → Degrade
    - Eviction → Recover
    - Restart → Recover
    - Data loss → Acceptable

This is allowed chaos — by design.

Mental Model:
    Redis remembers things you're okay forgetting.
"""

import json
import time
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict, Any, List, Set
from contextlib import contextmanager


# ============================================================================
# REDIS EXCEPTIONS
# ============================================================================

class RedisBackendError(Exception):
    """Base exception for Redis backend errors."""
    pass


class RedisConnectionError(RedisBackendError):
    """Redis connection failed."""
    pass


class RedisTimeoutError(RedisBackendError):
    """Redis operation timed out."""
    pass


class RedisKeyNotFoundError(RedisBackendError):
    """Key not found in Redis."""
    pass


# ============================================================================
# STATE KEY (Minimal for standalone)
# ============================================================================

@dataclass
class StateKey:
    """State key identifier."""
    key: str
    namespace: Optional[str] = None
    
    def to_redis_key(self) -> str:
        """Convert to Redis key format."""
        if self.namespace:
            return f"{self.namespace}:{self.key}"
        return self.key


# ============================================================================
# STATE BACKEND INTERFACE (Minimal for standalone)
# ============================================================================

class StateBackend(ABC):
    """Abstract state backend interface."""
    
    @abstractmethod
    def get(self, key: str) -> Optional[bytes]:
        """Get value."""
        pass
    
    @abstractmethod
    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        """Set value with optional TTL."""
        pass
    
    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete value."""
        pass
    
    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check if key exists."""
        pass


# ============================================================================
# IN-MEMORY REDIS SIMULATION (For Testing/Standalone)
# ============================================================================

class InMemoryRedis:
    """
    In-memory Redis simulation for testing.
    
    In production, this would be replaced with actual Redis client (redis-py).
    """
    
    @dataclass
    class Entry:
        value: bytes
        expires_at: Optional[float] = None
    
    def __init__(self):
        self._data: Dict[str, 'InMemoryRedis.Entry'] = {}
        self._lock = threading.Lock()
    
    def get(self, key: str) -> Optional[bytes]:
        """Get value."""
        with self._lock:
            self._evict_expired()
            entry = self._data.get(key)
            if entry is None:
                return None
            if entry.expires_at and time.time() > entry.expires_at:
                del self._data[key]
                return None
            return entry.value
    
    def set(
        self,
        key: str,
        value: bytes,
        ex: Optional[int] = None,
        nx: bool = False,
        xx: bool = False
    ) -> bool:
        """
        Set value with options.
        
        Args:
            key: Key to set
            value: Value to store
            ex: Expire in seconds
            nx: Only set if not exists
            xx: Only set if exists
        
        Returns:
            True if set, False otherwise
        """
        with self._lock:
            self._evict_expired()
            
            exists = key in self._data
            
            if nx and exists:
                return False
            if xx and not exists:
                return False
            
            expires_at = None
            if ex is not None:
                expires_at = time.time() + ex
            
            self._data[key] = self.Entry(value=value, expires_at=expires_at)
            return True
    
    def delete(self, *keys: str) -> int:
        """Delete keys. Returns number deleted."""
        with self._lock:
            count = 0
            for key in keys:
                if key in self._data:
                    del self._data[key]
                    count += 1
            return count
    
    def exists(self, *keys: str) -> int:
        """Check if keys exist. Returns count."""
        with self._lock:
            self._evict_expired()
            count = 0
            for key in keys:
                entry = self._data.get(key)
                if entry and (not entry.expires_at or time.time() <= entry.expires_at):
                    count += 1
            return count
    
    def incr(self, key: str) -> int:
        """Increment value."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                value = 1
            else:
                try:
                    value = int(entry.value.decode()) + 1
                except (ValueError, UnicodeDecodeError):
                    raise ValueError(f"Value at {key} is not an integer")
            
            self._data[key] = self.Entry(value=str(value).encode())
            return value
    
    def expire(self, key: str, seconds: int) -> bool:
        """Set expiration on key."""
        with self._lock:
            if key not in self._data:
                return False
            self._data[key].expires_at = time.time() + seconds
            return True
    
    def ttl(self, key: str) -> int:
        """Get TTL in seconds. -1 if no expiry, -2 if not exists."""
        with self._lock:
            self._evict_expired()
            entry = self._data.get(key)
            if entry is None:
                return -2
            if entry.expires_at is None:
                return -1
            remaining = entry.expires_at - time.time()
            return max(0, int(remaining))
    
    def keys(self, pattern: str = "*") -> List[str]:
        """List keys matching pattern."""
        with self._lock:
            self._evict_expired()
            
            if pattern == "*":
                return list(self._data.keys())
            
            # Simple prefix matching
            if pattern.endswith("*"):
                prefix = pattern[:-1]
                return [k for k in self._data.keys() if k.startswith(prefix)]
            
            return [k for k in self._data.keys() if k == pattern]
    
    def _evict_expired(self):
        """Evict expired entries."""
        now = time.time()
        expired = [
            key for key, entry in self._data.items()
            if entry.expires_at and now > entry.expires_at
        ]
        for key in expired:
            del self._data[key]
    
    def flushdb(self):
        """Clear all data."""
        with self._lock:
            self._data.clear()
    
    def ping(self) -> bool:
        """Check connection."""
        return True


# ============================================================================
# REDIS STATE BACKEND - Core Implementation
# ============================================================================

class RedisStateBackend(StateBackend):
    """
    Redis-backed ephemeral state storage.
    
    Enforces:
        - TTL policies
        - Fast reads/writes
        - Best-effort atomics
        - Graceful degradation
    
    Failure Semantics:
        - Timeout → Degrade (return None, continue)
        - Eviction → Recover (rebuild from source)
        - Restart → Recover (data loss acceptable)
        - Data loss → Acceptable (not source of truth)
    """
    
    # Default TTL for ephemeral data (1 hour)
    DEFAULT_TTL = 3600
    
    # Operation timeout (1 second)
    OPERATION_TIMEOUT = 1.0
    
    def __init__(
        self,
        redis_client: Optional[Any] = None,
        default_ttl: int = DEFAULT_TTL,
        key_prefix: str = "state:",
        fail_open: bool = True
    ):
        """
        Initialize Redis backend.
        
        Args:
            redis_client: Redis client instance (or None for in-memory)
            default_ttl: Default TTL in seconds
            key_prefix: Prefix for all keys
            fail_open: If True, degrade gracefully on failures
        """
        self.redis = redis_client or InMemoryRedis()
        self.default_ttl = default_ttl
        self.key_prefix = key_prefix
        self.fail_open = fail_open
        
        # Statistics
        self._stats = {
            "hits": 0,
            "misses": 0,
            "sets": 0,
            "deletes": 0,
            "errors": 0,
            "timeouts": 0
        }
        self._lock = threading.Lock()
    
    def get(self, key: str) -> Optional[bytes]:
        """
        Get value from Redis.
        
        Args:
            key: Key to retrieve
        
        Returns:
            Value bytes or None if not found
        """
        redis_key = self._make_key(key)
        
        try:
            value = self.redis.get(redis_key)
            
            if value is None:
                self._stats["misses"] += 1
            else:
                self._stats["hits"] += 1
            
            return value
            
        except Exception as e:
            self._stats["errors"] += 1
            
            if self.fail_open:
                # Degrade gracefully
                return None
            else:
                raise RedisBackendError(f"Redis get failed: {e}")
    
    def set(
        self,
        key: str,
        value: bytes,
        ttl: Optional[int] = None
    ) -> None:
        """
        Set value in Redis with TTL.
        
        Args:
            key: Key to set
            value: Value to store
            ttl: TTL in seconds (None = default)
        """
        redis_key = self._make_key(key)
        ttl = ttl if ttl is not None else self.default_ttl
        
        try:
            self.redis.set(redis_key, value, ex=ttl)
            self._stats["sets"] += 1
            
        except Exception as e:
            self._stats["errors"] += 1
            
            if not self.fail_open:
                raise RedisBackendError(f"Redis set failed: {e}")
    
    def delete(self, key: str) -> None:
        """
        Delete key from Redis.
        
        Args:
            key: Key to delete
        """
        redis_key = self._make_key(key)
        
        try:
            self.redis.delete(redis_key)
            self._stats["deletes"] += 1
            
        except Exception as e:
            self._stats["errors"] += 1
            
            if not self.fail_open:
                raise RedisBackendError(f"Redis delete failed: {e}")
    
    def exists(self, key: str) -> bool:
        """
        Check if key exists.
        
        Args:
            key: Key to check
        
        Returns:
            True if exists
        """
        redis_key = self._make_key(key)
        
        try:
            return self.redis.exists(redis_key) > 0
        except Exception as e:
            self._stats["errors"] += 1
            
            if self.fail_open:
                return False
            else:
                raise RedisBackendError(f"Redis exists failed: {e}")
    
    def set_if_not_exists(
        self,
        key: str,
        value: bytes,
        ttl: Optional[int] = None
    ) -> bool:
        """
        Set value only if key doesn't exist (atomic).
        
        Args:
            key: Key to set
            value: Value to store
            ttl: TTL in seconds
        
        Returns:
            True if set, False if already exists
        """
        redis_key = self._make_key(key)
        ttl = ttl if ttl is not None else self.default_ttl
        
        try:
            result = self.redis.set(redis_key, value, ex=ttl, nx=True)
            if result:
                self._stats["sets"] += 1
            return result
            
        except Exception as e:
            self._stats["errors"] += 1
            
            if self.fail_open:
                return False
            else:
                raise RedisBackendError(f"Redis setnx failed: {e}")
    
    def increment(self, key: str, delta: int = 1) -> int:
        """
        Increment counter (atomic).
        
        Args:
            key: Counter key
            delta: Amount to increment
        
        Returns:
            New value
        """
        redis_key = self._make_key(key)
        
        try:
            if delta == 1:
                return self.redis.incr(redis_key)
            else:
                # Multiple increments (not atomic across deltas != 1)
                for _ in range(abs(delta)):
                    if delta > 0:
                        value = self.redis.incr(redis_key)
                    else:
                        value = self.redis.decr(redis_key)
                return value
                
        except Exception as e:
            self._stats["errors"] += 1
            
            if self.fail_open:
                return 0
            else:
                raise RedisBackendError(f"Redis incr failed: {e}")
    
    def set_ttl(self, key: str, ttl: int) -> bool:
        """
        Set TTL on existing key.
        
        Args:
            key: Key to expire
            ttl: TTL in seconds
        
        Returns:
            True if set, False if key doesn't exist
        """
        redis_key = self._make_key(key)
        
        try:
            return self.redis.expire(redis_key, ttl)
        except Exception as e:
            self._stats["errors"] += 1
            
            if self.fail_open:
                return False
            else:
                raise RedisBackendError(f"Redis expire failed: {e}")
    
    def get_ttl(self, key: str) -> int:
        """
        Get remaining TTL.
        
        Args:
            key: Key to check
        
        Returns:
            TTL in seconds, -1 if no expiry, -2 if not exists
        """
        redis_key = self._make_key(key)
        
        try:
            return self.redis.ttl(redis_key)
        except Exception as e:
            self._stats["errors"] += 1
            
            if self.fail_open:
                return -2
            else:
                raise RedisBackendError(f"Redis ttl failed: {e}")
    
    def list_keys(self, pattern: str = "*") -> List[str]:
        """
        List keys matching pattern.
        
        WARNING: Expensive operation, use sparingly.
        
        Args:
            pattern: Key pattern
        
        Returns:
            List of matching keys (without prefix)
        """
        redis_pattern = self._make_key(pattern)
        
        try:
            keys = self.redis.keys(redis_pattern)
            # Remove prefix from results
            prefix_len = len(self.key_prefix)
            return [k[prefix_len:] for k in keys]
            
        except Exception as e:
            self._stats["errors"] += 1
            
            if self.fail_open:
                return []
            else:
                raise RedisBackendError(f"Redis keys failed: {e}")
    
    def flush_all(self) -> None:
        """
        Flush all data.
        
        WARNING: Use only in testing.
        """
        try:
            self.redis.flushdb()
        except Exception as e:
            if not self.fail_open:
                raise RedisBackendError(f"Redis flush failed: {e}")
    
    def health_check(self) -> bool:
        """
        Check Redis connection health.
        
        Returns:
            True if healthy
        """
        try:
            return self.redis.ping()
        except Exception:
            return False
    
    def get_stats(self) -> dict:
        """Get backend statistics."""
        with self._lock:
            stats = dict(self._stats)
            
            # Add computed metrics
            total_ops = stats["hits"] + stats["misses"]
            if total_ops > 0:
                stats["hit_rate"] = stats["hits"] / total_ops
            else:
                stats["hit_rate"] = 0.0
            
            return stats
    
    def _make_key(self, key: str) -> str:
        """Add prefix to key."""
        return f"{self.key_prefix}{key}"


# ============================================================================
# LOCK STORE - Distributed Locking
# ============================================================================

class RedisLockStore:
    """
    Distributed lock implementation using Redis.
    
    Best-effort only - not guaranteed in all failure scenarios.
    """
    
    def __init__(self, backend: RedisStateBackend, default_timeout: int = 30):
        self.backend = backend
        self.default_timeout = default_timeout
    
    def acquire(
        self,
        lock_id: str,
        owner_id: str,
        timeout: Optional[int] = None
    ) -> bool:
        """
        Acquire lock.
        
        Args:
            lock_id: Lock identifier
            owner_id: Owner identifier
            timeout: Lock timeout in seconds
        
        Returns:
            True if acquired
        """
        timeout = timeout if timeout is not None else self.default_timeout
        
        lock_key = f"lock:{lock_id}"
        lock_value = json.dumps({
            "owner": owner_id,
            "acquired_at": time.time()
        }).encode()
        
        return self.backend.set_if_not_exists(lock_key, lock_value, ttl=timeout)
    
    def release(self, lock_id: str, owner_id: str) -> bool:
        """
        Release lock.
        
        Args:
            lock_id: Lock identifier
            owner_id: Owner identifier (must match)
        
        Returns:
            True if released
        """
        lock_key = f"lock:{lock_id}"
        
        # Check ownership
        current = self.backend.get(lock_key)
        if current is None:
            return False
        
        try:
            lock_data = json.loads(current.decode())
            if lock_data.get("owner") != owner_id:
                return False  # Not the owner
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
        
        # Release
        self.backend.delete(lock_key)
        return True
    
    def is_locked(self, lock_id: str) -> bool:
        """Check if lock is currently held."""
        return self.backend.exists(f"lock:{lock_id}")
    
    @contextmanager
    def lock(self, lock_id: str, owner_id: str, timeout: Optional[int] = None):
        """
        Context manager for locks.
        
        Usage:
            with lock_store.lock("my_lock", "worker_1"):
                # Critical section
                do_work()
        """
        acquired = self.acquire(lock_id, owner_id, timeout)
        
        if not acquired:
            raise RedisBackendError(f"Failed to acquire lock: {lock_id}")
        
        try:
            yield
        finally:
            self.release(lock_id, owner_id)


# ============================================================================
# CURSOR STORE - Workflow Cursors
# ============================================================================

class RedisCursorStore:
    """
    Temporary cursor storage for workflow state.
    """
    
    def __init__(self, backend: RedisStateBackend, default_ttl: int = 3600):
        self.backend = backend
        self.default_ttl = default_ttl
    
    def save_cursor(
        self,
        workflow_id: str,
        cursor_data: dict,
        ttl: Optional[int] = None
    ) -> None:
        """Save workflow cursor."""
        cursor_key = f"cursor:{workflow_id}"
        cursor_value = json.dumps(cursor_data).encode()
        
        self.backend.set(cursor_key, cursor_value, ttl or self.default_ttl)
    
    def load_cursor(self, workflow_id: str) -> Optional[dict]:
        """Load workflow cursor."""
        cursor_key = f"cursor:{workflow_id}"
        cursor_value = self.backend.get(cursor_key)
        
        if cursor_value is None:
            return None
        
        try:
            return json.loads(cursor_value.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
    
    def delete_cursor(self, workflow_id: str) -> None:
        """Delete workflow cursor."""
        cursor_key = f"cursor:{workflow_id}"
        self.backend.delete(cursor_key)


# ============================================================================
# THROTTLE STORE - Rate Limiting
# ============================================================================

class RedisThrottleStore:
    """
    Rate limiting using Redis counters.
    """
    
    def __init__(self, backend: RedisStateBackend):
        self.backend = backend
    
    def check_throttle(
        self,
        throttle_id: str,
        limit: int,
        window_seconds: int
    ) -> bool:
        """
        Check if throttle limit exceeded.
        
        Args:
            throttle_id: Throttle identifier
            limit: Maximum count in window
            window_seconds: Time window in seconds
        
        Returns:
            True if under limit, False if throttled
        """
        throttle_key = f"throttle:{throttle_id}"
        
        # Get current count
        current = self.backend.get(throttle_key)
        
        if current is None:
            # First request in window
            count = 1
        else:
            try:
                count = int(current.decode()) + 1
            except (ValueError, UnicodeDecodeError):
                count = 1
        
        # Check limit
        if count > limit:
            return False
        
        # Increment counter
        self.backend.set(throttle_key, str(count).encode(), ttl=window_seconds)
        
        return True
    
    def reset_throttle(self, throttle_id: str) -> None:
        """Reset throttle counter."""
        throttle_key = f"throttle:{throttle_id}"
        self.backend.delete(throttle_key)


# ============================================================================
# DEDUPE STORE - Inflight Deduplication
# ============================================================================

class RedisDedupeStore:
    """
    Inflight deduplication using Redis sets.
    """
    
    def __init__(self, backend: RedisStateBackend, default_ttl: int = 300):
        self.backend = backend
        self.default_ttl = default_ttl
    
    def mark_inflight(
        self,
        request_id: str,
        ttl: Optional[int] = None
    ) -> bool:
        """
        Mark request as inflight.
        
        Args:
            request_id: Request identifier
            ttl: TTL in seconds
        
        Returns:
            True if newly marked, False if already inflight
        """
        dedupe_key = f"inflight:{request_id}"
        
        return self.backend.set_if_not_exists(
            dedupe_key,
            b"1",
            ttl or self.default_ttl
        )
    
    def clear_inflight(self, request_id: str) -> None:
        """Clear inflight marker."""
        dedupe_key = f"inflight:{request_id}"
        self.backend.delete(dedupe_key)
    
    def is_inflight(self, request_id: str) -> bool:
        """Check if request is inflight."""
        dedupe_key = f"inflight:{request_id}"
        return self.backend.exists(dedupe_key)


# ============================================================================
# FACTORY
# ============================================================================

def create_redis_backend(
    redis_client: Optional[Any] = None,
    default_ttl: int = 3600,
    key_prefix: str = "state:",
    fail_open: bool = True
) -> RedisStateBackend:
    """
    Create Redis backend.
    
    Args:
        redis_client: Redis client (None = in-memory)
        default_ttl: Default TTL in seconds
        key_prefix: Key prefix
        fail_open: Degrade gracefully on failures
    
    Returns:
        RedisStateBackend
    """
    return RedisStateBackend(
        redis_client=redis_client,
        default_ttl=default_ttl,
        key_prefix=key_prefix,
        fail_open=fail_open
    )


# ============================================================================
# USAGE EXAMPLES
# ============================================================================

if __name__ == "__main__":
    print("Redis Backend Demo")
    print("=" * 60)
    
    # Create backend (in-memory for demo)
    backend = create_redis_backend(
        default_ttl=60,
        key_prefix="demo:",
        fail_open=True
    )
    
    # Basic operations
    print("\n1. Basic Operations")
    backend.set("user:123", b"user_data")
    value = backend.get("user:123")
    print(f"✓ Set and retrieved: {value}")
    
    # TTL
    print("\n2. TTL Management")
    backend.set("temp:data", b"expires_soon", ttl=5)
    ttl = backend.get_ttl("temp:data")
    print(f"✓ TTL: {ttl} seconds")
    
    # Atomic operations
    print("\n3. Atomic Set-If-Not-Exists")
    result1 = backend.set_if_not_exists("unique:key", b"value1")
    result2 = backend.set_if_not_exists("unique:key", b"value2")
    print(f"✓ First set: {result1}")
    print(f"✓ Second set: {result2} (already exists)")
    
    # Counters
    print("\n4. Atomic Counters")
    backend.increment("counter:requests")
    backend.increment("counter:requests")
    count = backend.increment("counter:requests")
    print(f"✓ Request count: {count}")
    
    # Locks
    print("\n5. Distributed Locks")
    lock_store = RedisLockStore(backend, default_timeout=30)
    
    acquired = lock_store.acquire("workflow:123", "worker:1")
    print(f"✓ Lock acquired: {acquired}")
    
    # Try to acquire same lock
    acquired2 = lock_store.acquire("workflow:123", "worker:2")
    print(f"✓ Second acquire: {acquired2} (lock held)")
    
    # Release lock
    released = lock_store.release("workflow:123", "worker:1")
    print(f"✓ Lock released: {released}")
    
    # Context manager
    print("\n6. Lock Context Manager")
    try:
        with lock_store.lock("critical:section", "worker:1", timeout=10):
            print("✓ In critical section")
    except Exception as e:
        print(f"✗ Lock failed: {e}")
    
    # Cursors
    print("\n7. Workflow Cursors")
    cursor_store = RedisCursorStore(backend)
    
    cursor_store.save_cursor("wf_123", {"step": 5, "state": "running"})
    cursor = cursor_store.load_cursor("wf_123")
    print(f"✓ Cursor loaded: {cursor}")
    
    # Throttling
    print("\n8. Rate Limiting")
    throttle_store = RedisThrottleStore(backend)
    
    # Allow 3 requests per 10 seconds
    allowed1 = throttle_store.check_throttle("api:user_123", limit=3, window_seconds=10)
    allowed2 = throttle_store.check_throttle("api:user_123", limit=3, window_seconds=10)
    allowed3 = throttle_store.check_throttle("api:user_123", limit=3, window_seconds=10)
    allowed4 = throttle_store.check_throttle("api:user_123", limit=3, window_seconds=10)
    
    print(f"✓ Request 1: {allowed1}")
    print(f"✓ Request 2: {allowed2}")
    print(f"✓ Request 3: {allowed3}")
    print(f"✓ Request 4: {allowed4} (throttled)")
    
    # Deduplication
    print("\n9. Inflight Deduplication")
    dedupe_store = RedisDedupeStore(backend)
    
    new1 = dedupe_store.mark_inflight("req_001")
    new2 = dedupe_store.mark_inflight("req_001")
    
    print(f"✓ First request: {new1} (new)")
    print(f"✓ Second request: {new2} (duplicate)")
    
    dedupe_store.clear_inflight("req_001")
    new3 = dedupe_store.mark_inflight("req_001")
    print(f"✓ After clear: {new3} (new again)")
    
    # Statistics
    print("\n10. Backend Statistics")
    stats = backend.get_stats()
    print(f"✓ Stats:")
    print(f"    Hits: {stats['hits']}")
    print(f"    Misses: {stats['misses']}")
    print(f"    Sets: {stats['sets']}")
    print(f"    Hit Rate: {stats['hit_rate']:.2%}")
    
    # Health check
    print("\n11. Health Check")
    healthy = backend.health_check()
    print(f"✓ Redis healthy: {healthy}")
    
    print("\n" + "=" * 60)
    print("Redis remembers things you're okay forgetting.")
    print("Ephemeral. Volatile. Non-authoritative. By design.")