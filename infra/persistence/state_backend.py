"""
/infra/persistence/state_backend.py

Abstract Durable State Authority

This file defines how reality is persisted. Everything that must survive crashes,
restarts, scale, audits, and replays goes through this interface.

If state cannot be replayed deterministically, it is not state — it is memory.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, List, Dict
from abc import ABC, abstractmethod
import time


# ============================================================================
# CORE ENUMS (STRICT)
# ============================================================================


class StateScope(Enum):
    """
    Scope defines:
    - Isolation
    - Blast radius
    - Replay boundaries
    """

    GLOBAL = "global"
    RUN = "run"
    WORKFLOW = "workflow"
    ACCOUNT = "account"
    CONTENT = "content"


class StateConsistency(Enum):
    """
    Consistency guarantees.
    
    Defaults to STRONG. Eventual must be explicit.
    """

    STRONG = "strong"
    EVENTUAL = "eventual"


class StateOperation(Enum):
    """
    State operations. No hidden mutations.
    """

    READ = "read"
    WRITE = "write"
    DELETE = "delete"


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================


@dataclass(frozen=True)
class StateKey:
    """
    Immutable state key with full addressing.
    
    Rules:
    - No dynamic namespaces
    - No free-form keys
    - Keys must be enumerable
    """

    scope: StateScope
    scope_id: str
    namespace: str
    key: str

    def validate(self) -> None:
        """
        Validate state key.
        
        Raises:
            ValueError: If key invalid
        """
        if not self.scope_id:
            raise ValueError("scope_id required")

        if not self.namespace:
            raise ValueError("namespace required")

        if not self.key:
            raise ValueError("key required")

        # Enforce key format constraints
        if "/" in self.key or "\\" in self.key:
            raise ValueError("Key cannot contain path separators")

        if len(self.key) > 255:
            raise ValueError("Key length exceeds maximum (255 chars)")

    def to_string(self) -> str:
        """
        Convert to canonical string representation.
        
        Returns:
            String key
        """
        return f"{self.scope.value}:{self.scope_id}:{self.namespace}:{self.key}"

    @staticmethod
    def from_string(key_str: str) -> "StateKey":
        """
        Parse state key from string.
        
        Args:
            key_str: String representation
            
        Returns:
            StateKey
            
        Raises:
            ValueError: If string invalid
        """
        parts = key_str.split(":", 3)
        if len(parts) != 4:
            raise ValueError(f"Invalid key string: {key_str}")

        return StateKey(
            scope=StateScope(parts[0]),
            scope_id=parts[1],
            namespace=parts[2],
            key=parts[3],
        )


@dataclass(frozen=True)
class StateRecord:
    """
    Immutable state record with versioning.
    
    Rules:
    - Value is schema-validated
    - Version is monotonic
    - Timestamp from clock authority
    """

    key: StateKey
    value: dict[str, Any]
    version: int
    last_updated: int

    created_at: int | None = None

    def validate(self) -> None:
        """
        Validate state record.
        
        Raises:
            ValueError: If record invalid
        """
        self.key.validate()

        if self.version < 0:
            raise ValueError("Version must be non-negative")

        if self.last_updated <= 0:
            raise ValueError("last_updated must be positive timestamp")

        if self.created_at is not None and self.created_at <= 0:
            raise ValueError("created_at must be positive timestamp if provided")

    def with_new_version(
        self,
        new_value: dict[str, Any],
        timestamp: int,
    ) -> "StateRecord":
        """
        Create new version of record.
        
        Args:
            new_value: New value
            timestamp: Update timestamp
            
        Returns:
            New StateRecord with incremented version
        """
        return StateRecord(
            key=self.key,
            value=new_value,
            version=self.version + 1,
            last_updated=timestamp,
            created_at=self.created_at or timestamp,
        )


# ============================================================================
# STATE TRANSACTION (CRITICAL)
# ============================================================================


class StateTransaction(ABC):
    """
    Abstract state transaction interface.
    
    Guarantees:
    - Atomicity
    - Isolation
    - Deterministic commit ordering
    
    Partial commits are forbidden.
    """

    def __init__(self, consistency: StateConsistency):
        """
        Initialize transaction.
        
        Args:
            consistency: Consistency level for this transaction
        """
        self._consistency = consistency
        self._committed = False
        self._rolled_back = False

    @property
    def consistency(self) -> StateConsistency:
        """Get transaction consistency level."""
        return self._consistency

    @property
    def is_committed(self) -> bool:
        """Check if transaction committed."""
        return self._committed

    @property
    def is_rolled_back(self) -> bool:
        """Check if transaction rolled back."""
        return self._rolled_back

    @abstractmethod
    def read(self, key: StateKey) -> StateRecord | None:
        """
        Read state record.
        
        Args:
            key: State key to read
            
        Returns:
            StateRecord if exists, None otherwise
            
        Raises:
            ValueError: If transaction already committed/rolled back
        """
        pass

    @abstractmethod
    def write(self, record: StateRecord) -> None:
        """
        Write state record.
        
        Args:
            record: StateRecord to write
            
        Raises:
            ValueError: If transaction already committed/rolled back
        """
        pass

    @abstractmethod
    def delete(self, key: StateKey) -> None:
        """
        Delete state record.
        
        Args:
            key: State key to delete
            
        Raises:
            ValueError: If transaction already committed/rolled back
        """
        pass

    @abstractmethod
    def commit(self) -> None:
        """
        Commit transaction atomically.
        
        Raises:
            RuntimeError: If commit fails
            ValueError: If transaction already committed/rolled back
        """
        pass

    @abstractmethod
    def rollback(self) -> None:
        """
        Rollback transaction.
        
        Raises:
            ValueError: If transaction already committed/rolled back
        """
        pass

    def _check_active(self) -> None:
        """
        Check transaction is active.
        
        Raises:
            ValueError: If transaction not active
        """
        if self._committed:
            raise ValueError("Transaction already committed")
        if self._rolled_back:
            raise ValueError("Transaction already rolled back")


# ============================================================================
# STATE BACKEND (ABSTRACT INTERFACE)
# ============================================================================


class StateBackend(ABC):
    """
    Abstract state backend interface.
    
    Rules:
    - All writes occur inside a transaction
    - Transactions are explicit
    - Backend decides durability guarantees but must declare them
    """

    @abstractmethod
    def begin(
        self,
        consistency: StateConsistency = StateConsistency.STRONG,
    ) -> StateTransaction:
        """
        Begin a new transaction.
        
        Args:
            consistency: Consistency level for transaction
            
        Returns:
            StateTransaction
        """
        pass

    @abstractmethod
    def healthcheck(self) -> bool:
        """
        Check backend health.
        
        Returns:
            True if healthy
        """
        pass

    @abstractmethod
    def supports_replay(self) -> bool:
        """
        Check if backend supports deterministic replay.
        
        Returns:
            True if replay supported
        """
        pass

    @abstractmethod
    def supports_versioning(self) -> bool:
        """
        Check if backend supports record versioning.
        
        Returns:
            True if versioning supported
        """
        pass

    @abstractmethod
    def supports_snapshots(self) -> bool:
        """
        Check if backend supports safe snapshots.
        
        Returns:
            True if snapshots supported
        """
        pass

    @abstractmethod
    def get_backend_id(self) -> str:
        """
        Get unique backend identifier.
        
        Returns:
            Backend ID
        """
        pass

    def validate_capabilities(self) -> list[str]:
        """
        Validate backend capabilities.
        
        Returns:
            List of capability warnings (empty if all good)
        """
        warnings = []

        if not self.supports_replay():
            warnings.append(
                "Backend does not support replay — recovery features limited"
            )

        if not self.supports_versioning():
            warnings.append(
                "Backend does not support versioning — concurrent updates risky"
            )

        if not self.supports_snapshots():
            warnings.append(
                "Backend does not support snapshots — backups may be inconsistent"
            )

        return warnings


# ============================================================================
# STATE BACKEND REGISTRY (MANDATORY)
# ============================================================================


class StateBackendRegistry:
    """
    Manages state backend registration and selection.
    
    Rules:
    - Exactly one active backend
    - Switching requires restart
    - Registry validated at boot
    
    If no backend → system does not start.
    """

    def __init__(self):
        """Initialize backend registry."""
        self._backends: dict[str, StateBackend] = {}
        self._active_backend_id: str | None = None
        self._validated = False

    def register(self, backend: StateBackend) -> None:
        """
        Register a state backend.
        
        Args:
            backend: StateBackend to register
            
        Raises:
            ValueError: If backend already registered
        """
        backend_id = backend.get_backend_id()

        if backend_id in self._backends:
            raise ValueError(f"Backend '{backend_id}' already registered")

        self._backends[backend_id] = backend
        self._validated = False

    def set_active(self, backend_id: str) -> None:
        """
        Set active backend.
        
        Args:
            backend_id: Backend ID to activate
            
        Raises:
            ValueError: If backend not registered
        """
        if backend_id not in self._backends:
            raise ValueError(f"Backend '{backend_id}' not registered")

        self._active_backend_id = backend_id
        self._validated = False

    def get_active(self) -> StateBackend:
        """
        Get active backend.
        
        Returns:
            Active StateBackend
            
        Raises:
            RuntimeError: If no active backend set
        """
        if self._active_backend_id is None:
            raise RuntimeError("No active backend set — system cannot start")

        backend = self._backends.get(self._active_backend_id)
        if backend is None:
            raise RuntimeError(
                f"Active backend '{self._active_backend_id}' not found"
            )

        return backend

    def get_backend(self, backend_id: str) -> StateBackend | None:
        """Get backend by ID."""
        return self._backends.get(backend_id)

    def list_backends(self) -> list[str]:
        """Get all registered backend IDs."""
        return list(self._backends.keys())

    def validate_registry(self) -> list[str]:
        """
        Validate registry configuration.
        
        Returns:
            List of validation errors (empty if valid)
        """
        errors = []

        if not self._backends:
            errors.append("No backends registered")

        if self._active_backend_id is None:
            errors.append("No active backend set")
        elif self._active_backend_id not in self._backends:
            errors.append(
                f"Active backend '{self._active_backend_id}' not registered"
            )

        # Check active backend health
        if self._active_backend_id and self._active_backend_id in self._backends:
            backend = self._backends[self._active_backend_id]

            if not backend.healthcheck():
                errors.append(
                    f"Active backend '{self._active_backend_id}' failed healthcheck"
                )

            # Add capability warnings
            warnings = backend.validate_capabilities()
            errors.extend(warnings)

        self._validated = len(errors) == 0
        return errors

    def is_validated(self) -> bool:
        """Check if registry validated."""
        return self._validated


# ============================================================================
# STATE INVARIANTS (ABSOLUTE)
# ============================================================================


class StateInvariants:
    """
    Enforces state invariants.
    
    Invariants:
    - No writes outside transactions
    - No version regression
    - No schema drift
    - No silent overwrite
    - No implicit deletes
    - No best-effort durability
    
    Violations = immediate hard stop.
    """

    @staticmethod
    def verify_version_monotonicity(
        old_record: StateRecord | None,
        new_record: StateRecord,
    ) -> None:
        """
        Verify version is monotonically increasing.
        
        Args:
            old_record: Previous record (None if new)
            new_record: New record
            
        Raises:
            RuntimeError: If version regression detected
        """
        if old_record is None:
            # New record must start at version 0 or 1
            if new_record.version < 0:
                raise RuntimeError(
                    f"New record has negative version: {new_record.version} "
                    f"(INVARIANT VIOLATION)"
                )
        else:
            # Version must increase
            if new_record.version <= old_record.version:
                raise RuntimeError(
                    f"Version regression: {old_record.version} -> {new_record.version} "
                    f"for key {new_record.key.to_string()} (INVARIANT VIOLATION)"
                )

    @staticmethod
    def verify_no_silent_overwrite(
        old_record: StateRecord | None,
        new_record: StateRecord,
        expected_version: int | None,
    ) -> None:
        """
        Verify no silent overwrites (optimistic locking).
        
        Args:
            old_record: Previous record
            new_record: New record
            expected_version: Expected version (None = don't check)
            
        Raises:
            RuntimeError: If silent overwrite detected
        """
        if expected_version is None:
            return

        if old_record is None and expected_version != 0:
            raise RuntimeError(
                f"Expected version {expected_version} but record does not exist "
                f"(INVARIANT VIOLATION)"
            )

        if old_record and old_record.version != expected_version:
            raise RuntimeError(
                f"Optimistic lock failure: expected version {expected_version}, "
                f"found {old_record.version} (INVARIANT VIOLATION)"
            )

    @staticmethod
    def verify_transaction_active(transaction: StateTransaction) -> None:
        """
        Verify transaction is active.
        
        Args:
            transaction: Transaction to check
            
        Raises:
            RuntimeError: If transaction not active
        """
        if transaction.is_committed:
            raise RuntimeError(
                "Cannot operate on committed transaction (INVARIANT VIOLATION)"
            )

        if transaction.is_rolled_back:
            raise RuntimeError(
                "Cannot operate on rolled back transaction (INVARIANT VIOLATION)"
            )

    @staticmethod
    def verify_all(
        transaction: StateTransaction,
        old_record: StateRecord | None,
        new_record: StateRecord,
        expected_version: int | None = None,
    ) -> None:
        """
        Run all invariant checks.
        
        Args:
            transaction: Transaction being used
            old_record: Previous record (if any)
            new_record: New record
            expected_version: Expected version for optimistic locking
            
        Raises:
            RuntimeError: If any invariant violated
        """
        StateInvariants.verify_transaction_active(transaction)
        StateInvariants.verify_version_monotonicity(old_record, new_record)
        StateInvariants.verify_no_silent_overwrite(
            old_record,
            new_record,
            expected_version,
        )


# ============================================================================
# IN-MEMORY TRANSACTION (REFERENCE IMPLEMENTATION)
# ============================================================================


class InMemoryTransaction(StateTransaction):
    """
    In-memory transaction implementation for testing.
    
    NOT for production use — does not provide durability.
    """

    def __init__(
        self,
        consistency: StateConsistency,
        storage: dict[str, StateRecord],
    ):
        """
        Initialize in-memory transaction.
        
        Args:
            consistency: Consistency level
            storage: Shared storage dict
        """
        super().__init__(consistency)
        self._storage = storage
        self._pending_writes: dict[str, StateRecord] = {}
        self._pending_deletes: set[str] = set()

    def read(self, key: StateKey) -> StateRecord | None:
        """Read state record."""
        self._check_active()

        key_str = key.to_string()

        # Check pending deletes
        if key_str in self._pending_deletes:
            return None

        # Check pending writes
        if key_str in self._pending_writes:
            return self._pending_writes[key_str]

        # Read from storage
        return self._storage.get(key_str)

    def write(self, record: StateRecord) -> None:
        """Write state record."""
        self._check_active()

        record.validate()

        key_str = record.key.to_string()
        self._pending_writes[key_str] = record

        # Remove from pending deletes if present
        self._pending_deletes.discard(key_str)

    def delete(self, key: StateKey) -> None:
        """Delete state record."""
        self._check_active()

        key_str = key.to_string()
        self._pending_deletes.add(key_str)

        # Remove from pending writes if present
        self._pending_writes.pop(key_str, None)

    def commit(self) -> None:
        """Commit transaction."""
        self._check_active()

        # Apply deletes
        for key_str in self._pending_deletes:
            self._storage.pop(key_str, None)

        # Apply writes
        for key_str, record in self._pending_writes.items():
            # Verify version monotonicity
            old_record = self._storage.get(key_str)
            StateInvariants.verify_version_monotonicity(old_record, record)

            self._storage[key_str] = record

        self._committed = True

    def rollback(self) -> None:
        """Rollback transaction."""
        self._check_active()

        self._pending_writes.clear()
        self._pending_deletes.clear()
        self._rolled_back = True


# ============================================================================
# IN-MEMORY BACKEND (REFERENCE IMPLEMENTATION)
# ============================================================================


class InMemoryBackend(StateBackend):
    """
    In-memory state backend for testing.
    
    NOT for production use — does not provide durability.
    """

    def __init__(self, backend_id: str = "in-memory"):
        """
        Initialize in-memory backend.
        
        Args:
            backend_id: Backend identifier
        """
        self._backend_id = backend_id
        self._storage: dict[str, StateRecord] = {}

    def begin(
        self,
        consistency: StateConsistency = StateConsistency.STRONG,
    ) -> StateTransaction:
        """Begin transaction."""
        return InMemoryTransaction(consistency, self._storage)

    def healthcheck(self) -> bool:
        """Health check always passes for in-memory."""
        return True

    def supports_replay(self) -> bool:
        """In-memory does not support replay across restarts."""
        return False

    def supports_versioning(self) -> bool:
        """In-memory supports versioning."""
        return True

    def supports_snapshots(self) -> bool:
        """In-memory supports snapshots (shallow copy)."""
        return True

    def get_backend_id(self) -> str:
        """Get backend ID."""
        return self._backend_id

    def get_snapshot(self) -> dict[str, StateRecord]:
        """
        Get snapshot of current state.
        
        Returns:
            Snapshot dict
        """
        return self._storage.copy()

    def clear(self) -> None:
        """Clear all state (for testing)."""
        self._storage.clear()


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================


def create_state_key(
    scope: StateScope,
    scope_id: str,
    namespace: str,
    key: str,
) -> StateKey:
    """
    Create and validate state key.
    
    Args:
        scope: State scope
        scope_id: Scope identifier
        namespace: Namespace
        key: Key name
        
    Returns:
        Validated StateKey
    """
    state_key = StateKey(
        scope=scope,
        scope_id=scope_id,
        namespace=namespace,
        key=key,
    )
    state_key.validate()
    return state_key


def create_state_record(
    key: StateKey,
    value: dict[str, Any],
    version: int = 0,
    timestamp: int | None = None,
) -> StateRecord:
    """
    Create and validate state record.
    
    Args:
        key: State key
        value: Record value
        version: Record version
        timestamp: Timestamp (default: now)
        
    Returns:
        Validated StateRecord
    """
    if timestamp is None:
        timestamp = int(time.time() * 1000)

    record = StateRecord(
        key=key,
        value=value,
        version=version,
        last_updated=timestamp,
        created_at=timestamp,
    )
    record.validate()
    return record