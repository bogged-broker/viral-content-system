"""
/infra/persistence/backend/persistence_errors.py

Deterministic Persistence Error Authority
(No Silent Corruption, No Ambiguity)

This module is the single source of truth for all persistence-related failure modes.
It answers: "When storage fails, what exactly happened — and is it recoverable?"

CRITICAL PRINCIPLES:
- Typed: All errors are structured, typed classes
- Deterministic: Error messages are stable and reproducible
- Retry-classifiable: Each error has explicit retry semantics
- Non-ambiguous: Clear distinction between error types
- Backend-agnostic: Abstracts backend-specific exceptions

ABSOLUTE INVARIANTS:
1. All persistence errors inherit from PersistenceError
2. No raw exceptions (ValueError, IOError, TimeoutError, etc.) escape backend layer
3. Error messages are deterministic (no timestamps, random IDs, secrets)
4. Retry semantics are explicit (is_retryable, is_fatal, is_user_error)
5. Original exception context is preserved via exception chaining

Error Severity Model:
- Level 1 (Retryable): Temporary failures (timeout, connection drop)
- Level 2 (Deterministic Fail): Non-retryable without state change (integrity, CAS, missing key)
- Level 3 (Fatal Corruption): System-stop class (partial commit, store corruption)

If this file is weak, your system cannot reason about failure.
"""

from __future__ import annotations

from typing import Optional, ClassVar


# ============================================================================
# Root Exception
# ============================================================================

class PersistenceError(Exception):
    """
    Base class for all persistence-layer failures.
    
    All persistence errors inherit from this to enable unified error handling.
    Upstream layers can catch all storage failures via this root exception.
    
    Attributes:
        error_code: Stable machine-readable identifier for telemetry and routing
        is_retryable: Whether this error is safe to retry without state change
        is_fatal: Whether this error indicates unrecoverable corruption
        is_user_error: Whether this error is caused by invalid user input
    """
    # Stable machine-readable identifier for deterministic failure fingerprinting
    error_code: ClassVar[str] = "PERSISTENCE_ERROR"
    
    is_retryable: bool = False
    is_fatal: bool = False
    is_user_error: bool = False
    
    __slots__ = ("backend", "operation", "key", "intent", "message", "_frozen")
    
    def __init__(
        self,
        message: str,
        backend: Optional[str] = None,
        operation: Optional[str] = None,
        key: Optional[str] = None,
        intent: Optional[str] = None,
    ):
        """
        Initialize persistence error with deterministic context.
        
        Args:
            message: Human-readable error description
            backend: Name of the backend (redis, postgres, file, etc.)
            operation: Operation type (GET, SET, DELETE, etc.)
            key: Storage key involved (if applicable)
            intent: Intent marker (if applicable)
        
        Note: Does NOT include timestamps, random IDs, secrets, or full payloads
        """
        # Set attributes (immutable after initialization)
        object.__setattr__(self, "backend", backend)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "intent", intent)
        
        # Build deterministic error message
        parts = []
        if backend:
            parts.append(f"[{backend}]")
        if operation:
            parts.append(f"Operation={operation}")
        if key:
            parts.append(f"Key={key}")
        if intent:
            parts.append(f"Intent={intent}")
        
        context = " ".join(parts)
        full_message = f"{context}: {message}" if context else message
        
        super().__init__(full_message)
        
        # Store original message for programmatic access
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "_frozen", True)
    
    def __setattr__(self, name: str, value: object) -> None:
        """Prevent attribute modification after initialization for immutability."""
        if getattr(self, "_frozen", False):
            raise AttributeError(
                f"'{self.__class__.__name__}' object attributes are immutable after initialization"
            )
        super().__setattr__(name, value)
    
    def __repr__(self) -> str:
        """Deterministic string representation for replay consistency."""
        parts = [f"{self.__class__.__name__}"]
        parts.append(f"error_code={self.error_code}")
        if self.backend:
            parts.append(f"backend={self.backend}")
        if self.operation:
            parts.append(f"operation={self.operation}")
        if self.key:
            parts.append(f"key={self.key}")
        if self.intent:
            parts.append(f"intent={self.intent}")
        parts.append(f"is_retryable={self.is_retryable}")
        parts.append(f"is_fatal={self.is_fatal}")
        parts.append(f"is_user_error={self.is_user_error}")
        parts.append(f"message={self.message}")
        return f"<{' '.join(parts)}>"


# ============================================================================
# Initialization Errors (Fatal - Level 3)
# ============================================================================

class BackendInitializationError(PersistenceError):
    """
    Backend cannot start or initialize.
    
    Raised when:
    - Backend cannot start
    - Configuration is invalid
    - Required environment is missing
    - Storage endpoint is unreachable at startup
    
    This is fatal. No retry possible without environment change.
    """
    error_code: ClassVar[str] = "BACKEND_INITIALIZATION_ERROR"
    is_retryable = False
    is_fatal = True


# ============================================================================
# Connection Errors (Retryable - Level 1)
# ============================================================================

class BackendConnectionError(PersistenceError):
    """
    Network or connection failure to backend.
    
    Raised when:
    - Network failure occurs
    - Socket failure
    - Backend disconnected mid-operation
    
    Retryable depending on policy. May indicate temporary vs permanent condition.
    
    Attributes:
        is_permanent: If True, indicates permanent failure (e.g., DNS misconfig).
                      If False, indicates transient failure. None if unknown.
    """
    error_code: ClassVar[str] = "BACKEND_CONNECTION_ERROR"
    is_retryable = True
    is_fatal = False
    
    __slots__ = ("backend", "operation", "key", "intent", "message", "_frozen", "is_permanent")
    
    def __init__(
        self,
        message: str,
        backend: Optional[str] = None,
        operation: Optional[str] = None,
        key: Optional[str] = None,
        intent: Optional[str] = None,
        is_permanent: Optional[bool] = None,
    ):
        """
        Initialize connection error with permanence signaling.
        
        Args:
            message: Human-readable error description
            backend: Name of the backend (redis, postgres, file, etc.)
            operation: Operation type (GET, SET, DELETE, etc.)
            key: Storage key involved (if applicable)
            intent: Intent marker (if applicable)
            is_permanent: True if permanent failure, False if transient, None if unknown
        """
        super().__init__(message, backend, operation, key, intent)
        object.__setattr__(self, "is_permanent", is_permanent)
    
    def __repr__(self) -> str:
        """Deterministic string representation including permanence signaling."""
        parts = [f"{self.__class__.__name__}"]
        parts.append(f"error_code={self.error_code}")
        if self.backend:
            parts.append(f"backend={self.backend}")
        if self.operation:
            parts.append(f"operation={self.operation}")
        if self.key:
            parts.append(f"key={self.key}")
        if self.intent:
            parts.append(f"intent={self.intent}")
        parts.append(f"is_retryable={self.is_retryable}")
        parts.append(f"is_fatal={self.is_fatal}")
        parts.append(f"is_user_error={self.is_user_error}")
        parts.append(f"is_permanent={self.is_permanent}")
        parts.append(f"message={self.message}")
        return f"<{' '.join(parts)}>"


# ============================================================================
# Timeout Errors (Retryable - Level 1)
# ============================================================================

class BackendTimeoutError(PersistenceError):
    """
    Operation exceeded configured timeout.
    
    Raised when:
    - Operation took longer than allowed timeout
    
    Must never be confused with commit failure.
    Timeout ≠ unknown state unless explicitly flagged in backend.
    
    Attributes:
        state_unknown: If True, indicates that operation state is ambiguous after timeout.
                       Transaction layer must decide rollback vs re-read carefully.
    """
    error_code: ClassVar[str] = "BACKEND_TIMEOUT_ERROR"
    is_retryable = True
    is_fatal = False
    
    __slots__ = ("backend", "operation", "key", "intent", "message", "_frozen", "state_unknown")
    
    def __init__(
        self,
        message: str,
        backend: Optional[str] = None,
        operation: Optional[str] = None,
        key: Optional[str] = None,
        intent: Optional[str] = None,
        state_unknown: bool = False,
    ):
        """
        Initialize timeout error with state ambiguity signaling.
        
        Args:
            message: Human-readable error description
            backend: Name of the backend (redis, postgres, file, etc.)
            operation: Operation type (GET, SET, DELETE, etc.)
            key: Storage key involved (if applicable)
            intent: Intent marker (if applicable)
            state_unknown: True if operation state is ambiguous after timeout
        """
        super().__init__(message, backend, operation, key, intent)
        object.__setattr__(self, "state_unknown", state_unknown)
    
    def __repr__(self) -> str:
        """Deterministic string representation including state ambiguity signaling."""
        parts = [f"{self.__class__.__name__}"]
        parts.append(f"error_code={self.error_code}")
        if self.backend:
            parts.append(f"backend={self.backend}")
        if self.operation:
            parts.append(f"operation={self.operation}")
        if self.key:
            parts.append(f"key={self.key}")
        if self.intent:
            parts.append(f"intent={self.intent}")
        parts.append(f"is_retryable={self.is_retryable}")
        parts.append(f"is_fatal={self.is_fatal}")
        parts.append(f"is_user_error={self.is_user_error}")
        parts.append(f"state_unknown={self.state_unknown}")
        parts.append(f"message={self.message}")
        return f"<{' '.join(parts)}>"


# ============================================================================
# Key Errors (Deterministic Fail - Level 2)
# ============================================================================

class KeyNotFoundError(PersistenceError):
    """
    Read or delete attempted on missing key.
    
    Raised when:
    - Read requested for non-existent key
    - DELETE requested for non-existent key (if not allowed)
    
    Never return None silently. Missing data is not success.
    Non-retryable without state change.
    """
    error_code: ClassVar[str] = "KEY_NOT_FOUND_ERROR"
    is_retryable = False
    is_fatal = False
    is_user_error = True


class KeyAlreadyExistsError(PersistenceError):
    """
    Write conflicts with existing key.
    
    Raised when:
    - CREATE intent conflicts with existing key
    - Attempt to overwrite immutable key
    - CAS conflict due to unexpected existence
    
    Must be deterministic. Non-retryable without state change.
    """
    error_code: ClassVar[str] = "KEY_ALREADY_EXISTS_ERROR"
    is_retryable = False
    is_fatal = False
    is_user_error = True


# ============================================================================
# Integrity Errors (Deterministic Fail - Level 2)
# ============================================================================

class IntegrityViolationError(PersistenceError):
    """
    Write violates integrity constraints.
    
    Raised when:
    - Write violates schema constraints
    - Environment mismatch detected
    - Immutability breach attempted
    - Version conflict occurs
    
    This is NOT a backend failure. This is a rule enforcement failure.
    Non-retryable without state change.
    """
    error_code: ClassVar[str] = "INTEGRITY_VIOLATION_ERROR"
    is_retryable = False
    is_fatal = False
    is_user_error = True


# ============================================================================
# Concurrency Errors (Deterministic Fail - Level 2)
# ============================================================================

class ConcurrencyConflictError(PersistenceError):
    """
    Concurrent access conflict detected.
    
    Raised when:
    - Optimistic concurrency version mismatch
    - Compare-and-set (CAS) failed
    - Transaction key overlap violation
    
    Must never degrade to last-write-wins silently.
    Non-retryable without state change or explicit retry with new version.
    """
    error_code: ClassVar[str] = "CONCURRENCY_CONFLICT_ERROR"
    is_retryable = False
    is_fatal = False
    is_user_error = False


# ============================================================================
# Atomicity Errors (Fatal Corruption - Level 3)
# ============================================================================

class PartialCommitError(PersistenceError):
    """
    Multi-key operation partially applied - CRITICAL SEVERITY.
    
    Raised when:
    - Multi-key operation detected partial application
    - Backend failure occurred during simulated transaction
    - State is now ambiguous
    
    System must treat as possible corruption event.
    Upper layers must escalate and potentially halt.
    """
    error_code: ClassVar[str] = "PARTIAL_COMMIT_ERROR"
    is_retryable = False
    is_fatal = True
    is_user_error = False


# ============================================================================
# Serialization Errors (Deterministic Fail - Level 2)
# ============================================================================

class SerializationError(PersistenceError):
    """
    Value encoding or decoding failure.
    
    Raised when:
    - Value cannot be encoded/decoded
    - Encoding format mismatch
    - Corrupted stored bytes detected
    
    Must not bubble as raw JSON/pickle/encoding errors.
    Non-retryable without fixing the data format.
    """
    error_code: ClassVar[str] = "SERIALIZATION_ERROR"
    is_retryable = False
    is_fatal = False
    is_user_error = True


# ============================================================================
# Configuration Errors (Fatal - Level 3)
# ============================================================================

class BackendConfigurationError(PersistenceError):
    """
    Invalid backend configuration provided.
    
    Raised when:
    - Invalid config options provided
    - Required config missing
    - Unsupported backend mode specified
    
    Different from initialization error (which involves runtime failure).
    This is a static configuration problem.
    """
    error_code: ClassVar[str] = "BACKEND_CONFIGURATION_ERROR"
    is_retryable = False
    is_fatal = True
    is_user_error = True


# ============================================================================
# Fatal Corruption Error (Fatal - Level 3)
# ============================================================================

class PersistentCorruptionError(PersistenceError):
    """
    On-disk or store-level corruption detected - SYSTEM STOP.
    
    Raised when:
    - On-disk or store-level corruption detected
    - Checksums invalid
    - Integrity marker mismatch
    - Commit marker pattern inconsistent
    
    System must treat this as non-recoverable.
    No silent repair. Immediate escalation required.
    """
    error_code: ClassVar[str] = "PERSISTENT_CORRUPTION_ERROR"
    is_retryable = False
    is_fatal = True
    is_user_error = False


# ============================================================================
# Error Translation Contract
# ============================================================================

"""
Backend Implementation Requirements:

Every backend (Redis, Postgres, File, etc.) MUST:

1. Catch ALL native backend exceptions
2. Translate to one of the typed persistence errors defined above
3. Preserve original exception context using 'raise ... from e'

Example Translation Pattern:

    try:
        redis_client.get(key)
    except redis.ConnectionError as e:
        raise BackendConnectionError(
            message="Redis connection lost",
            backend="redis",
            operation="GET",
            key=key
        ) from e
    except redis.TimeoutError as e:
        raise BackendTimeoutError(
            message="Redis operation timeout",
            backend="redis",
            operation="GET",
            key=key
        ) from e

Raw backend exceptions MUST NEVER escape to:
- TransactionalStore
- IntegrityGuard
- Aggregation layer
- Ingestion layer

Failure to translate = failure to maintain deterministic replay semantics.
"""





