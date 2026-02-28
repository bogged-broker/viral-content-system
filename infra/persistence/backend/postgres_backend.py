"""
PostgreSQL State Backend - Primary ACID State Authority

This is the canonical source of truth for system state.
- Strongly consistent
- Transactional
- Versioned
- Replay-safe

What this IS:     The source of truth for facts
What this is NOT: Cache, event log, snapshot store, schema authority
"""

import hashlib
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import psycopg2.extensions
from psycopg2 import sql, errors as pg_errors


logger = logging.getLogger(__name__)


# ============================================================================
# Core Types
# ============================================================================

@dataclass(frozen=True)
class StateKey:
    """Immutable state key identifier."""
    namespace: str
    entity_id: str
    attribute: str
    
    def __str__(self) -> str:
        return f"{self.namespace}:{self.entity_id}:{self.attribute}"
    
    def to_tuple(self) -> Tuple[str, str, str]:
        return (self.namespace, self.entity_id, self.attribute)


@dataclass(frozen=True)
class StateRecord:
    """Versioned state record."""
    key: StateKey
    payload: bytes
    version: int
    checksum: str
    written_at: datetime


class IsolationLevel(Enum):
    """PostgreSQL isolation levels."""
    READ_COMMITTED = "READ COMMITTED"
    REPEATABLE_READ = "REPEATABLE READ"
    SERIALIZABLE = "SERIALIZABLE"


class BackendError(Exception):
    """Base exception for backend errors."""
    pass


class TransactionError(BackendError):
    """Transaction lifecycle errors."""
    pass


class CorruptionError(BackendError):
    """Data corruption detected - KILL SWITCH."""
    pass


class ConflictError(BackendError):
    """Write conflict detected."""
    pass


class SerializationError(BackendError):
    """Serialization failure."""
    pass


# ============================================================================
# Transaction Handle
# ============================================================================

@dataclass
class TransactionHandle:
    """Handle for an active transaction."""
    tx_id: str
    connection: Any
    isolation_level: IsolationLevel
    started_at: datetime
    thread_id: int
    _committed: bool = False
    _rolled_back: bool = False
    
    @property
    def is_active(self) -> bool:
        """Check if transaction is still active."""
        return not (self._committed or self._rolled_back)
    
    def mark_committed(self) -> None:
        """Mark transaction as committed."""
        if not self.is_active:
            raise TransactionError(f"Transaction {self.tx_id} already finalized")
        self._committed = True
    
    def mark_rolled_back(self) -> None:
        """Mark transaction as rolled back."""
        if not self.is_active:
            raise TransactionError(f"Transaction {self.tx_id} already finalized")
        self._rolled_back = True


# ============================================================================
# State Backend Interface
# ============================================================================

class StateBackend:
    """Abstract state backend interface."""
    
    def get(self, key: StateKey) -> Optional[bytes]:
        """Retrieve state payload for key."""
        raise NotImplementedError
    
    def set(self, key: StateKey, payload: bytes) -> None:
        """Store state payload for key."""
        raise NotImplementedError
    
    def delete(self, key: StateKey) -> None:
        """Delete state for key."""
        raise NotImplementedError
    
    def begin_transaction(self) -> TransactionHandle:
        """Begin a new transaction."""
        raise NotImplementedError
    
    def commit(self, tx: TransactionHandle) -> None:
        """Commit transaction."""
        raise NotImplementedError
    
    def rollback(self, tx: TransactionHandle) -> None:
        """Rollback transaction."""
        raise NotImplementedError


# ============================================================================
# PostgreSQL State Backend
# ============================================================================

class PostgresStateBackend(StateBackend):
    """
    Primary ACID State Authority
    
    Responsibilities:
    1. Provide atomic read/write
    2. Enforce transaction boundaries
    3. Guarantee exactly-once commits
    4. Persist versioned state
    5. Support consistent point-in-time reads
    6. Detect write conflicts
    7. Fail loudly on corruption
    
    Storage Model:
    - Keyed rows (namespace, entity_id, attribute)
    - Version column (optimistic locking)
    - Write timestamp (ordering)
    - Checksum column (corruption detection)
    - Opaque payload (bytes only)
    """
    
    # Schema definition
    DDL_CREATE_TABLE = """
        CREATE TABLE IF NOT EXISTS state_store (
            namespace VARCHAR(255) NOT NULL,
            entity_id VARCHAR(255) NOT NULL,
            attribute VARCHAR(255) NOT NULL,
            payload BYTEA NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            checksum CHAR(64) NOT NULL,
            written_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            PRIMARY KEY (namespace, entity_id, attribute)
        )
    """
    
    DDL_CREATE_INDEXES = [
        "CREATE INDEX IF NOT EXISTS idx_state_written_at ON state_store(written_at)",
        "CREATE INDEX IF NOT EXISTS idx_state_namespace ON state_store(namespace)",
        "CREATE INDEX IF NOT EXISTS idx_state_version ON state_store(version)",
    ]
    
    def __init__(
        self,
        dsn: str,
        isolation_level: IsolationLevel = IsolationLevel.SERIALIZABLE,
        pool_size: int = 10,
        enable_checksum_verification: bool = True,
    ):
        """
        Initialize PostgreSQL backend.
        
        Args:
            dsn: PostgreSQL connection string
            isolation_level: Default isolation level for transactions
            pool_size: Connection pool size
            enable_checksum_verification: Verify checksums on read
        """
        self.dsn = dsn
        self.default_isolation_level = isolation_level
        self.enable_checksum_verification = enable_checksum_verification
        
        # Thread-local storage for transaction contexts
        self._thread_local = threading.local()
        
        # Connection pool
        self._connection_pool: List[Any] = []
        self._pool_lock = threading.Lock()
        self._pool_size = pool_size
        
        # Initialize schema
        self._initialize_schema()
        
        logger.info(
            f"PostgresStateBackend initialized: isolation={isolation_level.value}, "
            f"pool_size={pool_size}, checksum_verification={enable_checksum_verification}"
        )
    
    # ========================================================================
    # Initialization
    # ========================================================================
    
    def _initialize_schema(self) -> None:
        """Initialize database schema."""
        conn = None
        try:
            conn = psycopg2.connect(self.dsn)
            conn.autocommit = True
            
            with conn.cursor() as cursor:
                # Create table
                cursor.execute(self.DDL_CREATE_TABLE)
                
                # Create indexes
                for index_ddl in self.DDL_CREATE_INDEXES:
                    cursor.execute(index_ddl)
            
            logger.info("Schema initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize schema: {e}")
            raise BackendError(f"Schema initialization failed: {e}")
        finally:
            if conn:
                conn.close()
    
    # ========================================================================
    # Connection Management
    # ========================================================================
    
    def _get_connection(self) -> Any:
        """Get a connection from the pool or create a new one."""
        with self._pool_lock:
            if self._connection_pool:
                return self._connection_pool.pop()
        
        # Create new connection
        try:
            conn = psycopg2.connect(self.dsn)
            conn.autocommit = False
            return conn
        except Exception as e:
            raise BackendError(f"Failed to create connection: {e}")
    
    def _return_connection(self, conn: Any) -> None:
        """Return a connection to the pool."""
        with self._pool_lock:
            if len(self._connection_pool) < self._pool_size:
                try:
                    # Ensure clean state
                    conn.rollback()
                    self._connection_pool.append(conn)
                    return
                except Exception:
                    pass
        
        # Pool full or connection bad - close it
        try:
            conn.close()
        except Exception:
            pass
    
    def _get_current_transaction(self) -> Optional[TransactionHandle]:
        """Get the current thread's active transaction."""
        return getattr(self._thread_local, 'transaction', None)
    
    def _set_current_transaction(self, tx: Optional[TransactionHandle]) -> None:
        """Set the current thread's active transaction."""
        self._thread_local.transaction = tx
    
    # ========================================================================
    # Checksum Operations
    # ========================================================================
    
    @staticmethod
    def _compute_checksum(payload: bytes) -> str:
        """Compute SHA-256 checksum of payload."""
        return hashlib.sha256(payload).hexdigest()
    
    def _verify_checksum(self, payload: bytes, expected: str) -> None:
        """
        Verify payload checksum.
        
        KILL SWITCH: Raises CorruptionError on mismatch.
        """
        actual = self._compute_checksum(payload)
        if actual != expected:
            raise CorruptionError(
                f"Checksum mismatch: expected={expected}, actual={actual}"
            )
    
    # ========================================================================
    # Transaction Management
    # ========================================================================
    
    def begin_transaction(
        self,
        isolation_level: Optional[IsolationLevel] = None,
    ) -> TransactionHandle:
        """
        Begin a new transaction.
        
        Args:
            isolation_level: Override default isolation level
            
        Returns:
            Transaction handle
            
        Raises:
            TransactionError: If transaction already active in this thread
        """
        # Check for existing transaction
        existing_tx = self._get_current_transaction()
        if existing_tx and existing_tx.is_active:
            raise TransactionError(
                f"Transaction {existing_tx.tx_id} already active in thread {existing_tx.thread_id}"
            )
        
        # Get connection
        conn = self._get_connection()
        
        # Set isolation level
        iso_level = isolation_level or self.default_isolation_level
        
        try:
            conn.set_isolation_level(
                getattr(
                    psycopg2.extensions,
                    f"ISOLATION_LEVEL_{iso_level.name}"
                )
            )
        except Exception as e:
            self._return_connection(conn)
            raise TransactionError(f"Failed to set isolation level: {e}")
        
        # Create transaction handle
        tx_id = f"tx_{int(time.time() * 1000000)}_{threading.get_ident()}"
        tx = TransactionHandle(
            tx_id=tx_id,
            connection=conn,
            isolation_level=iso_level,
            started_at=datetime.now(timezone.utc),
            thread_id=threading.get_ident(),
        )
        
        self._set_current_transaction(tx)
        
        logger.debug(
            f"Transaction started: {tx_id}, isolation={iso_level.value}, "
            f"thread={tx.thread_id}"
        )
        
        return tx
    
    def commit(self, tx: TransactionHandle) -> None:
        """
        Commit transaction.
        
        Args:
            tx: Transaction handle
            
        Raises:
            TransactionError: If transaction not active or from wrong thread
            SerializationError: On serialization failure
            BackendError: On other commit failures
        """
        # Verify transaction
        current_tx = self._get_current_transaction()
        if current_tx is None or current_tx.tx_id != tx.tx_id:
            raise TransactionError(
                f"Transaction {tx.tx_id} not active in current thread"
            )
        
        if not tx.is_active:
            raise TransactionError(f"Transaction {tx.tx_id} already finalized")
        
        if tx.thread_id != threading.get_ident():
            raise TransactionError(
                f"Transaction {tx.tx_id} cannot be committed from different thread"
            )
        
        # Attempt commit
        try:
            tx.connection.commit()
            tx.mark_committed()
            
            logger.debug(f"Transaction committed: {tx.tx_id}")
            
        except pg_errors.SerializationFailure as e:
            tx.mark_rolled_back()
            raise SerializationError(f"Serialization failure: {e}")
            
        except Exception as e:
            tx.mark_rolled_back()
            raise BackendError(f"Commit failed: {e}")
            
        finally:
            # Clean up
            self._set_current_transaction(None)
            self._return_connection(tx.connection)
    
    def rollback(self, tx: TransactionHandle) -> None:
        """
        Rollback transaction.
        
        Args:
            tx: Transaction handle
            
        Raises:
            TransactionError: If transaction not active or from wrong thread
        """
        # Verify transaction
        current_tx = self._get_current_transaction()
        if current_tx is None or current_tx.tx_id != tx.tx_id:
            raise TransactionError(
                f"Transaction {tx.tx_id} not active in current thread"
            )
        
        if not tx.is_active:
            raise TransactionError(f"Transaction {tx.tx_id} already finalized")
        
        if tx.thread_id != threading.get_ident():
            raise TransactionError(
                f"Transaction {tx.tx_id} cannot be rolled back from different thread"
            )
        
        # Rollback
        try:
            tx.connection.rollback()
            tx.mark_rolled_back()
            
            logger.debug(f"Transaction rolled back: {tx.tx_id}")
            
        finally:
            self._set_current_transaction(None)
            self._return_connection(tx.connection)
    
    @contextmanager
    def transaction(self, isolation_level: Optional[IsolationLevel] = None):
        """
        Context manager for transactions.
        
        Example:
            with backend.transaction():
                backend.set(key, payload)
                backend.set(key2, payload2)
        """
        tx = self.begin_transaction(isolation_level)
        try:
            yield tx
            self.commit(tx)
        except Exception:
            self.rollback(tx)
            raise
    
    # ========================================================================
    # State Operations
    # ========================================================================
    
    def get(self, key: StateKey) -> Optional[bytes]:
        """
        Retrieve state payload for key.
        
        Args:
            key: State key
            
        Returns:
            Payload bytes or None if not found
            
        Raises:
            CorruptionError: If checksum verification fails
            BackendError: On query failure
        """
        tx = self._get_current_transaction()
        
        if tx and tx.is_active:
            conn = tx.connection
        else:
            # Auto-transaction for single read
            conn = self._get_connection()
            try:
                return self._do_get(conn, key)
            finally:
                self._return_connection(conn)
        
        return self._do_get(conn, key)
    
    def _do_get(self, conn: Any, key: StateKey) -> Optional[bytes]:
        """Execute get operation."""
        query = """
            SELECT payload, checksum
            FROM state_store
            WHERE namespace = %s AND entity_id = %s AND attribute = %s
        """
        
        try:
            with conn.cursor() as cursor:
                cursor.execute(query, key.to_tuple())
                row = cursor.fetchone()
                
                if row is None:
                    return None
                
                payload, checksum = row
                payload = bytes(payload)
                
                # Verify checksum if enabled
                if self.enable_checksum_verification:
                    self._verify_checksum(payload, checksum)
                
                return payload
                
        except CorruptionError:
            raise
        except Exception as e:
            raise BackendError(f"Get failed for {key}: {e}")
    
    def set(self, key: StateKey, payload: bytes) -> None:
        """
        Store state payload for key.
        
        Uses optimistic locking via version column.
        
        Args:
            key: State key
            payload: State payload
            
        Raises:
            ConflictError: On version conflict
            TransactionError: If no active transaction
            BackendError: On write failure
        """
        tx = self._get_current_transaction()
        
        if not tx or not tx.is_active:
            raise TransactionError(
                "set() requires an active transaction. Use begin_transaction() or "
                "the transaction() context manager."
            )
        
        self._do_set(tx.connection, key, payload)
    
    def _do_set(self, conn: Any, key: StateKey, payload: bytes) -> None:
        """Execute set operation with optimistic locking."""
        checksum = self._compute_checksum(payload)
        
        # Upsert with version increment
        query = """
            INSERT INTO state_store (namespace, entity_id, attribute, payload, version, checksum)
            VALUES (%s, %s, %s, %s, 1, %s)
            ON CONFLICT (namespace, entity_id, attribute)
            DO UPDATE SET
                payload = EXCLUDED.payload,
                version = state_store.version + 1,
                checksum = EXCLUDED.checksum,
                written_at = NOW()
            WHERE state_store.version = (
                SELECT version FROM state_store
                WHERE namespace = %s AND entity_id = %s AND attribute = %s
            )
        """
        
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    query,
                    (*key.to_tuple(), payload, checksum, *key.to_tuple())
                )
                
                # Check if update succeeded
                if cursor.rowcount == 0:
                    # Conflict detected - another transaction modified this key
                    raise ConflictError(
                        f"Write conflict detected for {key} - version mismatch"
                    )
                    
        except ConflictError:
            raise
        except pg_errors.UniqueViolation as e:
            raise ConflictError(f"Concurrent insert conflict for {key}: {e}")
        except Exception as e:
            raise BackendError(f"Set failed for {key}: {e}")
    
    def delete(self, key: StateKey) -> None:
        """
        Delete state for key.
        
        Args:
            key: State key
            
        Raises:
            TransactionError: If no active transaction
            BackendError: On delete failure
        """
        tx = self._get_current_transaction()
        
        if not tx or not tx.is_active:
            raise TransactionError(
                "delete() requires an active transaction. Use begin_transaction() or "
                "the transaction() context manager."
            )
        
        self._do_delete(tx.connection, key)
    
    def _do_delete(self, conn: Any, key: StateKey) -> None:
        """Execute delete operation."""
        query = """
            DELETE FROM state_store
            WHERE namespace = %s AND entity_id = %s AND attribute = %s
        """
        
        try:
            with conn.cursor() as cursor:
                cursor.execute(query, key.to_tuple())
                
        except Exception as e:
            raise BackendError(f"Delete failed for {key}: {e}")
    
    # ========================================================================
    # Additional Operations
    # ========================================================================
    
    def get_record(self, key: StateKey) -> Optional[StateRecord]:
        """
        Get full state record with metadata.
        
        Args:
            key: State key
            
        Returns:
            StateRecord or None if not found
        """
        tx = self._get_current_transaction()
        
        if tx and tx.is_active:
            conn = tx.connection
        else:
            conn = self._get_connection()
            try:
                return self._do_get_record(conn, key)
            finally:
                self._return_connection(conn)
        
        return self._do_get_record(conn, key)
    
    def _do_get_record(self, conn: Any, key: StateKey) -> Optional[StateRecord]:
        """Execute get_record operation."""
        query = """
            SELECT payload, version, checksum, written_at
            FROM state_store
            WHERE namespace = %s AND entity_id = %s AND attribute = %s
        """
        
        try:
            with conn.cursor() as cursor:
                cursor.execute(query, key.to_tuple())
                row = cursor.fetchone()
                
                if row is None:
                    return None
                
                payload, version, checksum, written_at = row
                payload = bytes(payload)
                
                # Verify checksum if enabled
                if self.enable_checksum_verification:
                    self._verify_checksum(payload, checksum)
                
                return StateRecord(
                    key=key,
                    payload=payload,
                    version=version,
                    checksum=checksum,
                    written_at=written_at,
                )
                
        except CorruptionError:
            raise
        except Exception as e:
            raise BackendError(f"Get record failed for {key}: {e}")
    
    def list_keys(
        self,
        namespace: Optional[str] = None,
        entity_id: Optional[str] = None,
    ) -> List[StateKey]:
        """
        List keys matching criteria.
        
        Args:
            namespace: Filter by namespace
            entity_id: Filter by entity_id
            
        Returns:
            List of matching keys
        """
        conditions = []
        params = []
        
        if namespace:
            conditions.append("namespace = %s")
            params.append(namespace)
        
        if entity_id:
            conditions.append("entity_id = %s")
            params.append(entity_id)
        
        where_clause = " AND ".join(conditions) if conditions else "TRUE"
        
        query = f"""
            SELECT namespace, entity_id, attribute
            FROM state_store
            WHERE {where_clause}
            ORDER BY namespace, entity_id, attribute
        """
        
        conn = self._get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(query, params)
                rows = cursor.fetchall()
                
                return [
                    StateKey(namespace=ns, entity_id=eid, attribute=attr)
                    for ns, eid, attr in rows
                ]
        finally:
            self._return_connection(conn)
    
    # ========================================================================
    # Cleanup
    # ========================================================================
    
    def close(self) -> None:
        """Close all connections and clean up."""
        with self._pool_lock:
            for conn in self._connection_pool:
                try:
                    conn.close()
                except Exception:
                    pass
            self._connection_pool.clear()
        
        logger.info("PostgresStateBackend closed")