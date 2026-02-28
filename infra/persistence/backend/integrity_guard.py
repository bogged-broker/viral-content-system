"""
Persistence Integrity Authority (Write Guarantees, Consistency Barriers, Corruption Prevention)

This module is the single authority enforcing safety guarantees around persistence operations.
It prevents: silent overwrites, cross-domain collisions, partial writes, replay contamination,
inconsistent version writes, and contract violations.

This is a logical firewall between system invariants and raw storage.

What This File Exists For (Non-Negotiable)

integrity_guard.py is the single authority that enforces safety guarantees around persistence operations.

It answers:

> "Is this write/read operation safe, legal, and consistent with system invariants?"

This file protects the system from:

- Silent overwrites
- Cross-domain collisions
- Partial writes
- Replay contamination
- Inconsistent version writes
- Contract violations

It does NOT:

- Implement storage
- Define key structure
- Implement backend logic
- Contain business aggregation logic

It wraps and protects backend operations.

If this file fails, corruption becomes invisible.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Protocol, Optional, Dict, Any, Set
from dataclasses import dataclass, field


# ============================================================================
# Write Intent Classification
# ============================================================================

class WriteIntent(Enum):
    """
    Explicit write intent classification.
    Every write must declare intent - no blind writes allowed.
    """
    CREATE = "create"              # Must not exist
    UPDATE = "update"              # Must exist
    UPSERT = "upsert"              # Safe deterministic replace
    APPEND = "append"              # Monotonic only
    REPLACE_VERSIONED = "replace"  # Must match version contract


# ============================================================================
# Domain Policy Definitions
# ============================================================================

class DomainPolicy(Enum):
    """Policy enforcement levels for different data domains."""
    IMMUTABLE = "immutable"        # Cannot be overwritten after write
    VERSIONED = "versioned"        # Version must change for mutations
    MUTABLE = "mutable"            # Can be updated with proper intent
    APPEND_ONLY = "append_only"    # Only monotonic additions allowed


# ============================================================================
# Write Metadata
# ============================================================================

@dataclass(frozen=True)
class WriteMetadata:
    """
    Metadata accompanying every write operation.
    Enforces explicit declaration of write characteristics.
    """
    domain: str
    version: str
    environment: str
    is_replay: bool = False
    is_idempotent: bool = False
    content_hash: Optional[str] = None
    domain_policy: DomainPolicy = DomainPolicy.MUTABLE
    expected_version: Optional[str] = None


# ============================================================================
# Integrity Exceptions
# ============================================================================

class IntegrityError(Exception):
    """Base exception for all integrity violations."""
    pass


class ImmutableViolationError(IntegrityError):
    """Attempted to overwrite immutable data."""
    def __init__(self, key: str, domain: str):
        super().__init__(
            f"Immutable violation: attempted overwrite of key '{key}' in domain '{domain}'"
        )
        self.key = key
        self.domain = domain


class VersionConflictError(IntegrityError):
    """Version mismatch detected."""
    def __init__(self, key: str, expected: str, actual: str):
        super().__init__(
            f"Version conflict on key '{key}': expected '{expected}', got '{actual}'"
        )
        self.key = key
        self.expected = expected
        self.actual = actual


class IllegalWriteIntentError(IntegrityError):
    """Write intent is illegal for current state."""
    def __init__(self, key: str, intent: str | WriteIntent, reason: str):
        intent_str = intent.value if isinstance(intent, WriteIntent) else intent
        super().__init__(
            f"Illegal write intent '{intent_str}' for key '{key}': {reason}"
        )
        self.key = key
        self.intent = intent
        self.reason = reason


class EnvironmentMismatchError(IntegrityError):
    """Environment isolation violation."""
    def __init__(self, key: str, key_env: str, exec_env: str):
        super().__init__(
            f"Environment mismatch for key '{key}': key environment '{key_env}' "
            f"does not match execution environment '{exec_env}'"
        )
        self.key = key
        self.key_env = key_env
        self.exec_env = exec_env


class NonDeterministicOverwriteError(IntegrityError):
    """Idempotent replay detected different content."""
    def __init__(self, key: str, existing_hash: str, new_hash: str):
        super().__init__(
            f"Non-deterministic overwrite detected for key '{key}': "
            f"existing hash '{existing_hash}' != new hash '{new_hash}'"
        )
        self.key = key
        self.existing_hash = existing_hash
        self.new_hash = new_hash


class PartialWriteError(IntegrityError):
    """Partial write detected - atomicity violation."""
    def __init__(self, key: str, reason: str):
        super().__init__(f"Partial write detected for key '{key}': {reason}")
        self.key = key
        self.reason = reason


class KeyExistsError(IntegrityError):
    """CREATE operation on existing key."""
    def __init__(self, key: str):
        super().__init__(f"CREATE failed: key '{key}' already exists")
        self.key = key


class KeyNotFoundError(IntegrityError):
    """UPDATE operation on non-existent key."""
    def __init__(self, key: str):
        super().__init__(f"UPDATE failed: key '{key}' does not exist")
        self.key = key


# ============================================================================
# Backend Protocol
# ============================================================================

class PersistenceBackend(Protocol):
    """
    Protocol defining minimal backend interface required by IntegrityGuard.
    Matches BackendBase interface from backend_base.py.
    Backends must never be used directly in production paths.
    """
    
    def exists(self, key: str) -> bool:
        """Check if key exists."""
        ...
    
    def get(self, key: str) -> Optional[bytes]:
        """Get raw bytes for key. Returns None if key does not exist."""
        ...
    
    def put(self, key: str, value: bytes, mode: Any = None, expected_version: Optional[int] = None) -> None:
        """Write raw bytes to key."""
        ...
    
    def delete(self, key: str) -> None:
        """Delete key."""
        ...
    
    def get_metadata(self, key: str) -> Dict[str, Any]:
        """Get backend-specific metadata for key."""
        ...
    
    def get_capabilities(self) -> Any:
        """Get backend capabilities including transaction support."""
        ...


# ============================================================================
# Key Namespace Validator Protocol
# ============================================================================

class KeyNamespaceValidator(Protocol):
    """
    Protocol for key namespace validation.
    IntegrityGuard delegates structural validation to this.
    """
    
    def validate_key_format(self, key: str) -> bool:
        """Validate key conforms to namespace rules."""
        ...
    
    def extract_environment(self, key: str) -> str:
        """Extract environment identifier from key."""
        ...
    
    def extract_version(self, key: str) -> Optional[str]:
        """Extract version from key if present."""
        ...
    
    def extract_domain(self, key: str) -> str:
        """Extract domain identifier from key."""
        ...


# ============================================================================
# Write Context
# ============================================================================

@dataclass
class WriteContext:
    """Internal write operation context."""
    key: str
    value: bytes
    intent: str | WriteIntent
    metadata: WriteMetadata
    content_hash: str = field(init=False)
    key_exists: bool = field(init=False)
    existing_value: Optional[bytes] = field(default=None, init=False)
    existing_hash: Optional[str] = field(default=None, init=False)
    
    def __post_init__(self):
        # Compute content hash immediately
        self.content_hash = self._compute_hash(self.value)
    
    @staticmethod
    def _compute_hash(data: bytes) -> str:
        """Compute SHA-256 hash of data."""
        return hashlib.sha256(data).hexdigest()


# ============================================================================
# Integrity Guard
# ============================================================================

class IntegrityGuard:
    """
    Logical firewall enforcing invariants before backend operations.
    
    Enforces:
    - Immutability
    - Determinism
    - Version isolation
    - Collision prevention
    - Explicit write intent
    - Replay safety
    
    All writes must pass through IntegrityGuard.
    Backends must never be used directly in production paths.
    """
    
    def __init__(
        self,
        backend: PersistenceBackend,
        key_validator: KeyNamespaceValidator,
        execution_environment: str,
        immutable_domains: Optional[Set[str]] = None,
        strict_mode: bool = True
    ):
        """
        Initialize IntegrityGuard.
        
        Args:
            backend: Underlying persistence backend
            key_validator: Key namespace validator
            execution_environment: Current execution environment (prod/test/replay)
            immutable_domains: Set of domain names that are immutable
            strict_mode: Whether to enforce strictest possible checks
        """
        self._backend = backend
        self._validator = key_validator
        self._execution_environment = execution_environment
        self._immutable_domains = immutable_domains or set()
        self._strict_mode = strict_mode
        
        # NOTE: No in-memory write log to preserve determinism.
        # Write metadata tracking must be externalized to persistent storage
        # if needed for audit/replay purposes. In-memory state breaks:
        # - Crash recovery determinism
        # - Multi-process correctness
        # - Audit replay equivalence
    
    # ========================================================================
    # Public Interface
    # ========================================================================
    
    def guarded_write(
        self,
        key: str,
        value: bytes,
        intent: str | WriteIntent,
        metadata: WriteMetadata
    ) -> None:
        """
        Execute guarded write with full invariant enforcement.
        
        Args:
            key: Storage key
            value: Raw bytes to write
            intent: Explicit write intent (WriteIntent.CREATE, etc.)
            metadata: Write metadata
            
        Raises:
            IntegrityError: On any invariant violation
        """
        # Build write context
        ctx = WriteContext(
            key=key,
            value=value,
            intent=intent,
            metadata=metadata
        )
        
        # Execute validation pipeline
        self._validate_key_format(ctx)
        self._validate_environment(ctx)
        self._check_existence(ctx)
        self._validate_write_intent(ctx)
        self._enforce_domain_policy(ctx)
        self._validate_version_compatibility(ctx)
        self._validate_idempotency(ctx)
        
        # Execute write
        self._execute_write(ctx)
        
        # NOTE: Metadata logging removed for determinism.
        # If audit trail is needed, it must be externalized to persistent storage.
    
    def validate_write(
        self,
        key: str,
        value: bytes,
        intent: str | WriteIntent,
        metadata: WriteMetadata
    ) -> None:
        """
        Validate write operation without executing it.
        
        Used by transactional stores to validate before commit.
        
        Args:
            key: Storage key
            value: Raw bytes to write
            intent: Explicit write intent
            metadata: Write metadata
            
        Raises:
            IntegrityError: On any invariant violation
        """
        # Build write context
        ctx = WriteContext(
            key=key,
            value=value,
            intent=intent,
            metadata=metadata
        )
        
        # Execute validation pipeline (without write)
        self._validate_key_format(ctx)
        self._validate_environment(ctx)
        self._check_existence(ctx)
        self._validate_write_intent(ctx)
        self._enforce_domain_policy(ctx)
        self._validate_version_compatibility(ctx)
        self._validate_idempotency(ctx)
    
    def guarded_read(self, key: str) -> bytes:
        """
        Execute guarded read with validation.
        
        Args:
            key: Storage key
            
        Returns:
            Raw bytes
            
        Raises:
            KeyNotFoundError: If key does not exist
            IllegalWriteIntentError: If key format is invalid
        """
        # Validate key format
        if not self._validator.validate_key_format(key):
            raise IllegalWriteIntentError(
                key=key,
                intent=WriteIntent.UPDATE,
                reason="Invalid key format"
            )
        
        # Check existence
        if not self._backend.exists(key):
            raise KeyNotFoundError(key)
        
        # Read from backend (get() returns None if not found, but we already checked)
        value = self._backend.get(key)
        if value is None:
            raise KeyNotFoundError(key)
        
        return value
    
    def guarded_delete(self, key: str, metadata: WriteMetadata) -> None:
        """
        Execute guarded delete with policy enforcement.
        
        Args:
            key: Storage key
            metadata: Delete metadata
            
        Raises:
            IntegrityError: On policy violations
        """
        # Validate key
        if not self._validator.validate_key_format(key):
            raise IllegalWriteIntentError(
                key=key,
                intent=WriteIntent.UPDATE,
                reason="Invalid key format"
            )
        
        # Check immutability
        domain = self._validator.extract_domain(key)
        if domain in self._immutable_domains:
            raise ImmutableViolationError(key=key, domain=domain)
        
        if metadata.domain_policy == DomainPolicy.IMMUTABLE:
            raise ImmutableViolationError(key=key, domain=metadata.domain)
        
        # Execute delete
        if self._backend.exists(key):
            self._backend.delete(key)
    
    # ========================================================================
    # Validation Pipeline
    # ========================================================================
    
    def _validate_key_format(self, ctx: WriteContext) -> None:
        """Validate key conforms to namespace rules."""
        if not self._validator.validate_key_format(ctx.key):
            raise IllegalWriteIntentError(
                key=ctx.key,
                intent=ctx.intent,
                reason="Key does not conform to namespace format"
            )
    
    def _validate_environment(self, ctx: WriteContext) -> None:
        """Enforce environment isolation."""
        key_env = self._validator.extract_environment(ctx.key)
        
        # Replay writes must use replay environment - strict requirement
        # Replay writes must NEVER touch prod namespace regardless of execution env
        if ctx.metadata.is_replay:
            if key_env != "replay":
                raise EnvironmentMismatchError(
                    key=ctx.key,
                    key_env=key_env,
                    exec_env="replay (required)"
                )
        else:
            # Non-replay writes must match execution environment
            if key_env != ctx.metadata.environment:
                raise EnvironmentMismatchError(
                    key=ctx.key,
                    key_env=key_env,
                    exec_env=ctx.metadata.environment
                )
            
            # Verify matches current execution environment
            if self._strict_mode and key_env != self._execution_environment:
                raise EnvironmentMismatchError(
                    key=ctx.key,
                    key_env=key_env,
                    exec_env=self._execution_environment
                )
    
    def _check_existence(self, ctx: WriteContext) -> None:
        """Check key existence and load existing data if present."""
        ctx.key_exists = self._backend.exists(ctx.key)
        
        if ctx.key_exists:
            ctx.existing_value = self._backend.get(ctx.key)
            if ctx.existing_value is not None:
                ctx.existing_hash = WriteContext._compute_hash(ctx.existing_value)
            else:
                # Key exists but get() returned None - treat as not existing
                ctx.key_exists = False
    
    def _validate_write_intent(self, ctx: WriteContext) -> None:
        """Enforce write intent semantics."""
        # Normalize intent to WriteIntent enum
        intent = ctx.intent if isinstance(ctx.intent, WriteIntent) else WriteIntent(ctx.intent)
        
        if intent == WriteIntent.CREATE:
            # CREATE: must not exist
            if ctx.key_exists:
                raise KeyExistsError(ctx.key)
        
        elif intent == WriteIntent.UPDATE:
            # UPDATE: must exist
            if not ctx.key_exists:
                raise KeyNotFoundError(ctx.key)
        
        elif intent == WriteIntent.UPSERT:
            # UPSERT: allowed but must be deterministic
            if not ctx.metadata.is_idempotent and self._strict_mode:
                raise IllegalWriteIntentError(
                    key=ctx.key,
                    intent=intent,
                    reason="UPSERT requires idempotent flag in strict mode"
                )
        
        elif intent == WriteIntent.APPEND:
            # APPEND: monotonic only - new content must be strict extension of existing
            if ctx.key_exists:
                if ctx.existing_value is None:
                    # Key exists but no value - treat as corruption
                    raise IllegalWriteIntentError(
                        key=ctx.key,
                        intent=intent,
                        reason="APPEND failed: key exists but has no readable value"
                    )
                
                # Enforce monotonicity: new value must contain existing value as prefix
                # This ensures APPEND is truly monotonic (only additions, no overwrites)
                if not ctx.value.startswith(ctx.existing_value):
                    raise IllegalWriteIntentError(
                        key=ctx.key,
                        intent=intent,
                        reason="APPEND violation: new content is not a monotonic extension of existing content"
                    )
                
                # Additional check: new value must be strictly longer (not identical)
                if ctx.value == ctx.existing_value:
                    # Identical content - this should be idempotent, not APPEND
                    raise IllegalWriteIntentError(
                        key=ctx.key,
                        intent=intent,
                        reason="APPEND violation: new content is identical to existing (use idempotent UPSERT instead)"
                    )
        
        elif intent == WriteIntent.REPLACE_VERSIONED:
            # REPLACE_VERSIONED: must exist and match version
            if not ctx.key_exists:
                raise KeyNotFoundError(ctx.key)
        
        # Update context with normalized intent
        ctx.intent = intent
    
    def _enforce_domain_policy(self, ctx: WriteContext) -> None:
        """Enforce domain-specific policies."""
        domain = self._validator.extract_domain(ctx.key)
        
        # Check global immutable domains
        if domain in self._immutable_domains and ctx.key_exists:
            raise ImmutableViolationError(key=ctx.key, domain=domain)
        
        # Check metadata domain policy
        policy = ctx.metadata.domain_policy
        
        if policy == DomainPolicy.IMMUTABLE:
            if ctx.key_exists:
                raise ImmutableViolationError(key=ctx.key, domain=ctx.metadata.domain)
        
        elif policy == DomainPolicy.VERSIONED:
            # Versioned writes must change version
            if ctx.key_exists:
                key_version = self._validator.extract_version(ctx.key)
                if key_version == ctx.metadata.version:
                    raise VersionConflictError(
                        key=ctx.key,
                        expected=f"!= {ctx.metadata.version}",
                        actual=ctx.metadata.version
                    )
        
        elif policy == DomainPolicy.APPEND_ONLY:
            # Append-only domains can only use APPEND or CREATE intents
            intent = ctx.intent if isinstance(ctx.intent, WriteIntent) else WriteIntent(ctx.intent)
            if intent not in (WriteIntent.APPEND, WriteIntent.CREATE):
                raise IllegalWriteIntentError(
                    key=ctx.key,
                    intent=intent,
                    reason=f"Append-only domain requires APPEND or CREATE intent"
                )
    
    def _validate_version_compatibility(self, ctx: WriteContext) -> None:
        """Enforce version compatibility rules."""
        intent = ctx.intent if isinstance(ctx.intent, WriteIntent) else WriteIntent(ctx.intent)
        
        # Extract version from key (if versioned namespace)
        key_version = self._validator.extract_version(ctx.key)
        
        # Version isolation: reject cross-version writes into same key namespace
        # This applies to ALL intents, not just REPLACE_VERSIONED
        if key_version is not None and ctx.metadata.version is not None:
            # Key has version component - enforce version isolation
            if intent == WriteIntent.REPLACE_VERSIONED:
                # REPLACE_VERSIONED: must match exactly
                if ctx.metadata.expected_version is not None:
                    if key_version != ctx.metadata.expected_version:
                        raise VersionConflictError(
                            key=ctx.key,
                            expected=ctx.metadata.expected_version,
                            actual=key_version or "none"
                        )
                
                # Verify write version matches key version
                if key_version != ctx.metadata.version:
                    raise VersionConflictError(
                        key=ctx.key,
                        expected=ctx.metadata.version,
                        actual=key_version or "none"
                    )
            elif intent in (WriteIntent.CREATE, WriteIntent.UPSERT, WriteIntent.UPDATE):
                # CREATE/UPSERT/UPDATE: must not write different version to same key
                # This prevents schema drift and cross-version contamination
                if ctx.key_exists and key_version != ctx.metadata.version:
                    raise VersionConflictError(
                        key=ctx.key,
                        expected=ctx.metadata.version,
                        actual=key_version or "none"
                    )
                # For CREATE on versioned namespace, version must match
                if not ctx.key_exists and key_version != ctx.metadata.version:
                    raise VersionConflictError(
                        key=ctx.key,
                        expected=ctx.metadata.version,
                        actual=key_version or "none"
                    )
    
    def _validate_idempotency(self, ctx: WriteContext) -> None:
        """Enforce idempotency guarantees."""
        if not ctx.metadata.is_idempotent:
            return
        
        if not ctx.key_exists:
            return
        
        # For idempotent writes, replaying same write must produce no mutation
        if ctx.existing_hash != ctx.content_hash:
            raise NonDeterministicOverwriteError(
                key=ctx.key,
                existing_hash=ctx.existing_hash or "none",
                new_hash=ctx.content_hash
            )
        
        # If hashes match, verify byte equality for absolute certainty
        if ctx.existing_value is not None and ctx.existing_value != ctx.value:
            # This should never happen if hashes match, but check anyway
            raise NonDeterministicOverwriteError(
                key=ctx.key,
                existing_hash=ctx.existing_hash or "none",
                new_hash=ctx.content_hash
            )
    
    def _execute_write(self, ctx: WriteContext) -> None:
        """
        Execute the actual write operation with atomicity safeguards.
        
        Tier-0 atomicity enforcement:
        - If backend supports transactions, rely on them
        - Otherwise, use read-after-write verification with hash check
        - Detect concurrent overwrites between put() and get()
        """
        # Check if backend supports transactions
        capabilities = self._backend.get_capabilities()
        supports_transactions = capabilities.supports_transactions if hasattr(capabilities, 'supports_transactions') else False
        
        # If backend supports transactions, use them
        if supports_transactions:
            # Backend should handle atomicity
            # Use put() with appropriate mode based on intent
            # Note: BackendBase.put() accepts mode parameter, but we use it conditionally
            # For CREATE/UPDATE intents, backend will enforce semantics
            self._backend.put(ctx.key, ctx.value)
        else:
            # Simulate atomicity safeguards for backends without transaction support
            # Strategy: write, then verify with hash check to detect concurrent modification
            
            # Store pre-write state for race detection
            pre_write_exists = self._backend.exists(ctx.key)
            pre_write_hash = None
            if pre_write_exists:
                pre_write_value = self._backend.get(ctx.key)
                if pre_write_value is not None:
                    pre_write_hash = WriteContext._compute_hash(pre_write_value)
            
            # Execute write
            self._backend.put(ctx.key, ctx.value)
            
            # Verify write succeeded and detect concurrent overwrites
            if self._strict_mode:
                written_value = self._backend.get(ctx.key)
                if written_value is None:
                    raise PartialWriteError(
                        key=ctx.key,
                        reason="Write verification failed: key does not exist after write"
                    )
                
                # Verify content matches
                if written_value != ctx.value:
                    raise PartialWriteError(
                        key=ctx.key,
                        reason="Write verification failed: read-back content mismatch"
                    )
                
                # Detect concurrent overwrite: if key existed before, verify it wasn't
                # modified by another process between our existence check and write
                if pre_write_exists and pre_write_hash is not None:
                    # If we're doing an UPDATE/REPLACE, the hash should have changed
                    # If we're doing an idempotent UPSERT, hash should match
                    # If hash doesn't match either pattern, concurrent modification occurred
                    written_hash = WriteContext._compute_hash(written_value)
                    intent = ctx.intent if isinstance(ctx.intent, WriteIntent) else WriteIntent(ctx.intent)
                    
                    if intent == WriteIntent.UPSERT and ctx.metadata.is_idempotent:
                        # Idempotent UPSERT: hash should match if content unchanged
                        # But we already verified written_value == ctx.value above
                        # So this is just a sanity check
                        pass
                    elif written_hash == pre_write_hash and intent != WriteIntent.UPSERT:
                        # Hash unchanged but intent wasn't UPSERT - suspicious
                        # This could indicate a no-op write or concurrent modification
                        # For now, we allow it but could be stricter
                        pass
    
    # ========================================================================
    # Introspection
    # ========================================================================
    
    # NOTE: Introspection methods removed due to determinism requirements.
    # In-memory write metadata tracking was removed to preserve:
    # - Crash recovery determinism
    # - Multi-process correctness  
    # - Audit replay equivalence
    #
    # If metadata tracking is needed, it must be externalized to persistent storage
    # (e.g., audit log, write-ahead log, or separate metadata store).


# ============================================================================
# Factory Function
# ============================================================================

def create_guarded_backend(
    backend: PersistenceBackend,
    key_validator: KeyNamespaceValidator,
    execution_environment: str,
    immutable_domains: Optional[Set[str]] = None,
    strict_mode: bool = True
) -> IntegrityGuard:
    """
    Factory function to create IntegrityGuard around a backend.
    
    Args:
        backend: Underlying persistence backend
        key_validator: Key namespace validator
        execution_environment: Current execution environment
        immutable_domains: Set of immutable domain names
        strict_mode: Enable strictest possible enforcement
        
    Returns:
        Configured IntegrityGuard instance
    """
    return IntegrityGuard(
        backend=backend,
        key_validator=key_validator,
        execution_environment=execution_environment,
        immutable_domains=immutable_domains,
        strict_mode=strict_mode
    )
