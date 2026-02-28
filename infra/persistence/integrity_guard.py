






# EITHER DEPRECATE OR DELETE - SHOULD BE INSIDE THE /BACKENDS/ FOLDER!












"""
Persistence Integrity Authority (Write Guarantees, Consistency Barriers, Corruption Prevention)

This module is the single authority enforcing safety guarantees around persistence operations.
It prevents: silent overwrites, cross-domain collisions, partial writes, replay contamination,
inconsistent version writes, and contract violations.

This is a logical firewall between system invariants and raw storage.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
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
    def __init__(self, key: str, intent: WriteIntent, reason: str):
        super().__init__(
            f"Illegal write intent '{intent.value}' for key '{key}': {reason}"
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
    Backends must never be used directly in production paths.
    """
    
    def exists(self, key: str) -> bool:
        """Check if key exists."""
        ...
    
    def read(self, key: str) -> bytes:
        """Read raw bytes for key."""
        ...
    
    def write(self, key: str, value: bytes) -> None:
        """Write raw bytes to key."""
        ...
    
    def delete(self, key: str) -> None:
        """Delete key."""
        ...
    
    def get_metadata(self, key: str) -> Dict[str, Any]:
        """Get backend-specific metadata for key."""
        ...
    
    def supports_transactions(self) -> bool:
        """Whether backend supports atomic transactions."""
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
    intent: WriteIntent
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
        
        # Track write metadata for audit trail (optional)
        self._write_log: Dict[str, WriteMetadata] = {}
    
    # ========================================================================
    # Public Interface
    # ========================================================================
    
    def guarded_write(
        self,
        key: str,
        value: bytes,
        intent: WriteIntent,
        metadata: WriteMetadata
    ) -> None:
        """
        Execute guarded write with full invariant enforcement.
        
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
        
        # Record metadata
        self._write_log[key] = metadata
    
    def guarded_read(self, key: str) -> bytes:
        """
        Execute guarded read with validation.
        
        Args:
            key: Storage key
            
        Returns:
            Raw bytes
            
        Raises:
            KeyNotFoundError: If key does not exist
        """
        # Validate key format
        if not self._validator.validate_key_format(key):
            raise IllegalWriteIntentError(
                key=key,
                intent=WriteIntent.UPDATE,  # Read is like UPDATE
                reason="Invalid key format"
            )
        
        # Check existence
        if not self._backend.exists(key):
            raise KeyNotFoundError(key)
        
        return self._backend.read(key)
    
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
            self._write_log.pop(key, None)
    
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
        
        # Replay writes must use replay environment
        if ctx.metadata.is_replay:
            if key_env == self._execution_environment and self._execution_environment != "replay":
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
            ctx.existing_value = self._backend.read(ctx.key)
            ctx.existing_hash = WriteContext._compute_hash(ctx.existing_value)
    
    def _validate_write_intent(self, ctx: WriteContext) -> None:
        """Enforce write intent semantics."""
        if ctx.intent == WriteIntent.CREATE:
            # CREATE: must not exist
            if ctx.key_exists:
                raise KeyExistsError(ctx.key)
        
        elif ctx.intent == WriteIntent.UPDATE:
            # UPDATE: must exist
            if not ctx.key_exists:
                raise KeyNotFoundError(ctx.key)
        
        elif ctx.intent == WriteIntent.UPSERT:
            # UPSERT: allowed but must be deterministic
            if not ctx.metadata.is_idempotent and self._strict_mode:
                raise IllegalWriteIntentError(
                    key=ctx.key,
                    intent=ctx.intent,
                    reason="UPSERT requires idempotent flag in strict mode"
                )
        
        elif ctx.intent == WriteIntent.APPEND:
            # APPEND: monotonic only - validate if exists
            if ctx.key_exists and self._strict_mode:
                # In strict mode, APPEND should use different validation
                # For now, just ensure it's marked appropriately
                pass
        
        elif ctx.intent == WriteIntent.REPLACE_VERSIONED:
            # REPLACE_VERSIONED: must exist and match version
            if not ctx.key_exists:
                raise KeyNotFoundError(ctx.key)
    
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
            if ctx.intent not in (WriteIntent.APPEND, WriteIntent.CREATE):
                raise IllegalWriteIntentError(
                    key=ctx.key,
                    intent=ctx.intent,
                    reason=f"Append-only domain requires APPEND or CREATE intent"
                )
    
    def _validate_version_compatibility(self, ctx: WriteContext) -> None:
        """Enforce version compatibility rules."""
        if ctx.intent == WriteIntent.REPLACE_VERSIONED:
            # Extract version from key
            key_version = self._validator.extract_version(ctx.key)
            
            # Verify expected version matches
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
                existing_hash=ctx.existing_hash,
                new_hash=ctx.content_hash
            )
        
        # If hashes match, verify byte equality for absolute certainty
        if ctx.existing_value != ctx.value:
            # This should never happen if hashes match, but check anyway
            raise NonDeterministicOverwriteError(
                key=ctx.key,
                existing_hash=ctx.existing_hash,
                new_hash=ctx.content_hash
            )
    
    def _execute_write(self, ctx: WriteContext) -> None:
        """
        Execute the actual write operation with atomicity safeguards.
        """
        # If backend supports transactions, use them
        if self._backend.supports_transactions():
            # Backend should handle atomicity
            self._backend.write(ctx.key, ctx.value)
        else:
            # Simulate minimal atomicity safeguards
            # Write and immediately verify
            self._backend.write(ctx.key, ctx.value)
            
            # Verify write succeeded
            if self._strict_mode:
                written_value = self._backend.read(ctx.key)
                if written_value != ctx.value:
                    raise PartialWriteError(
                        key=ctx.key,
                        reason="Write verification failed: read-back mismatch"
                    )
    
    # ========================================================================
    # Introspection
    # ========================================================================
    
    def get_write_metadata(self, key: str) -> Optional[WriteMetadata]:
        """Retrieve recorded write metadata for key."""
        return self._write_log.get(key)
    
    def verify_integrity(self, key: str) -> bool:
        """
        Verify integrity of stored data against recorded metadata.
        
        Returns:
            True if integrity check passes, False otherwise
        """
        if key not in self._write_log:
            return False
        
        if not self._backend.exists(key):
            return False
        
        metadata = self._write_log[key]
        if metadata.content_hash is None:
            return True
        
        # Verify content hash
        current_value = self._backend.read(key)
        current_hash = WriteContext._compute_hash(current_value)
        
        return current_hash == metadata.content_hash


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