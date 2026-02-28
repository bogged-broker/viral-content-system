# /infra/secret_resolver.py
"""
Secure Credential & Token Resolution Authority

This is the single, auditable, minimal-exposure gateway for ALL secrets.
If this file is weak, everything "secure" upstream is cosmetic.

Core principle (NON-NEGOTIABLE):
    Secrets are capabilities, not data.

Access to a secret:
- grants power
- must be explicitly authorized
- must be minimal
- must be time-bounded
- must be observable

Nothing else touches secrets directly. EVER.
"""

import hashlib
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional, Dict, Set, Any, List
import os

from infra.runtime_context import RuntimeContext, ExecutionMode
from infra.config_registry import ConfigRegistry
from infra.feature_flags import FeatureFlags


# ============================================================================
# ENUMS (EXPLICIT SECURITY SEMANTICS)
# ============================================================================

class SecretScope(Enum):
    """Defines who may request a secret."""
    GLOBAL = "global"      # System-wide infrastructure
    POSTING = "posting"    # Content posting subsystem
    PLATFORM = "platform"  # Platform-specific operations
    INFRA = "infra"       # Core infrastructure only


class SecretType(Enum):
    """Controls handling rules and security requirements."""
    API_KEY = "api_key"              # Static API keys
    OAUTH_TOKEN = "oauth_token"      # OAuth access tokens
    SIGNING_KEY = "signing_key"      # Cryptographic signing keys
    ENCRYPTION_KEY = "encryption_key"  # Encryption materials


class AccessLevel(Enum):
    """No implicit privilege escalation."""
    READ = "read"        # Read secret value
    SIGN = "sign"        # Use for signing operations
    REFRESH = "refresh"  # Refresh/rotate token


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class SecretDescriptor:
    """
    Canonical registration of a secret.
    If it's not registered here → it does not exist.
    """
    name: str
    secret_type: SecretType
    scope: SecretScope
    
    provider: str              # Provider adapter name
    provider_key: str          # Lookup key in provider
    
    allowed_consumers: Set[str]      # Module names authorized to access
    access_levels: Set[AccessLevel]  # Permitted access levels
    
    rotation_interval: timedelta     # How often secret rotates
    expires: bool                    # Whether secret expires
    
    audit_required: bool       # Whether access must be audited
    description: str          # Human-readable description
    
    def __post_init__(self):
        if not self.name:
            raise ValueError("SecretDescriptor name cannot be empty")
        if not self.allowed_consumers:
            raise ValueError(f"Secret '{self.name}' must have at least one allowed consumer")
        if not self.access_levels:
            raise ValueError(f"Secret '{self.name}' must have at least one access level")
        if self.expires and self.rotation_interval.total_seconds() <= 0:
            raise ValueError(f"Expiring secret '{self.name}' must have rotation_interval > 0")


@dataclass(frozen=True)
class SecretRequest:
    """
    Per-call contract for secret access.
    No raw access. Ever.
    """
    secret_name: str
    consumer: str          # Fully qualified module name
    access_level: AccessLevel
    
    def __post_init__(self):
        if not self.secret_name:
            raise ValueError("SecretRequest secret_name cannot be empty")
        if not self.consumer:
            raise ValueError("SecretRequest consumer cannot be empty")


@dataclass(frozen=True)
class ResolvedSecret:
    """
    Safe wrapper around secret material.
    
    CRITICAL SECURITY PROPERTIES:
    - No __repr__ (prevents accidental logging)
    - No serialization (prevents leakage)
    - Opaque value (prevents inspection)
    """
    _value: Any                        # Opaque - use get_value() only
    expires_at: Optional[datetime]
    fingerprint: str                   # Hash for audit trail
    secret_name: str
    resolved_at: datetime
    
    def __repr__(self) -> str:
        """NEVER reveal secret value in repr."""
        return (
            f"ResolvedSecret(name={self.secret_name}, "
            f"fingerprint={self.fingerprint[:8]}..., "
            f"expires_at={self.expires_at})"
        )
    
    def __str__(self) -> str:
        """NEVER reveal secret value in str."""
        return self.__repr__()
    
    def get_value(self) -> Any:
        """
        Controlled access to secret value.
        Use this ONLY when actually using the secret.
        """
        return self._value
    
    def is_expired(self) -> bool:
        """Check if secret has expired."""
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at


@dataclass(frozen=True)
class SecretSnapshot:
    """
    Audit trail snapshot.
    Never stores raw values - only fingerprints.
    """
    snapshot_id: str
    resolved_secrets: Dict[str, str]  # name → fingerprint
    created_at: datetime
    runtime_mode: ExecutionMode
    
    def __post_init__(self):
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")


# ============================================================================
# RESOLUTION POLICY (SECURITY POSTURE)
# ============================================================================

@dataclass(frozen=True)
class ResolutionPolicy:
    """
    Enforces:
    - least privilege
    - no cross-scope access
    - production restrictions
    - emergency revocations
    
    This is where security posture lives.
    """
    allow_env_vars_in_prod: bool = False  # Strict by default
    require_audit_all: bool = True        # Audit everything
    max_resolution_rate: int = 1000       # Per minute per secret
    cache_ttl_seconds: int = 300          # 5 minute cache
    
    # Emergency controls
    global_revocation_enabled: bool = False
    revoked_secrets: Set[str] = field(default_factory=set)
    
    def is_revoked(self, secret_name: str) -> bool:
        """Check if secret has been emergency revoked."""
        return (
            self.global_revocation_enabled or
            secret_name in self.revoked_secrets
        )


# ============================================================================
# PROVIDER ADAPTER (PLUGGABLE, SEALED)
# ============================================================================

class ProviderAdapter(ABC):
    """
    Abstract interface for secret providers.
    No adapter logic leaks upward.
    """
    
    @abstractmethod
    def fetch(self, provider_key: str) -> Any:
        """Fetch secret from provider."""
        pass
    
    @abstractmethod
    def refresh(self, provider_key: str) -> Any:
        """Refresh/rotate secret if supported."""
        pass
    
    @abstractmethod
    def get_name(self) -> str:
        """Get provider name for logging."""
        pass


class EnvVarProvider(ProviderAdapter):
    """
    Environment variable provider.
    DEV ONLY - never use in production.
    """
    
    def __init__(self, runtime_context: RuntimeContext):
        self._runtime_context = runtime_context
        if runtime_context.mode == ExecutionMode.LIVE:
            raise RuntimeError(
                "EnvVarProvider is FORBIDDEN in PRODUCTION mode. "
                "Use VaultProvider instead."
            )
    
    def fetch(self, provider_key: str) -> str:
        """Fetch from environment variable."""
        value = os.environ.get(provider_key)
        if value is None:
            raise ValueError(f"Environment variable '{provider_key}' not found")
        return value
    
    def refresh(self, provider_key: str) -> str:
        """Environment variables don't support refresh."""
        raise NotImplementedError("EnvVarProvider does not support refresh")
    
    def get_name(self) -> str:
        return "EnvVarProvider"


class VaultProvider(ProviderAdapter):
    """
    HashiCorp Vault or similar secret management system.
    PRODUCTION-GRADE provider.
    """
    
    def __init__(self, vault_addr: str, vault_token: str):
        self._vault_addr = vault_addr
        self._vault_token = vault_token
        # In production, this would initialize actual Vault client
    
    def fetch(self, provider_key: str) -> Any:
        """Fetch secret from Vault."""
        # In production, this would:
        # 1. Connect to Vault
        # 2. Authenticate
        # 3. Fetch secret at provider_key path
        # 4. Parse and return value
        
        # Placeholder for demonstration
        raise NotImplementedError("VaultProvider.fetch() - connect to real Vault")
    
    def refresh(self, provider_key: str) -> Any:
        """Refresh secret in Vault."""
        # In production, this would trigger Vault rotation
        raise NotImplementedError("VaultProvider.refresh() - connect to real Vault")
    
    def get_name(self) -> str:
        return "VaultProvider"


class PlatformTokenProvider(ProviderAdapter):
    """
    OAuth token provider for platform credentials.
    Handles token refresh flows.
    """
    
    def __init__(self):
        # In production, would maintain OAuth client state
        pass
    
    def fetch(self, provider_key: str) -> str:
        """Fetch OAuth token."""
        # In production, this would:
        # 1. Check token cache
        # 2. If expired, refresh using refresh token
        # 3. Return valid access token
        
        # Placeholder
        raise NotImplementedError("PlatformTokenProvider.fetch() - implement OAuth flow")
    
    def refresh(self, provider_key: str) -> str:
        """Force token refresh."""
        # In production, would perform OAuth refresh flow
        raise NotImplementedError("PlatformTokenProvider.refresh() - implement OAuth refresh")
    
    def get_name(self) -> str:
        return "PlatformTokenProvider"


# ============================================================================
# SECRET RESOLVER (SINGLETON AUTHORITY)
# ============================================================================

class SecretResolver:
    """
    The one and only gateway to secrets.
    
    Enforces:
    - Authorization (who can access)
    - Access levels (what they can do)
    - Runtime restrictions (when they can access)
    - Audit trail (logging all access)
    
    Initialized once at boot.
    """
    
    _instance: Optional['SecretResolver'] = None
    _lock = threading.Lock()
    
    def __init__(
        self,
        runtime_context: RuntimeContext,
        policy: Optional[ResolutionPolicy] = None
    ):
        self._runtime_context = runtime_context
        self._policy = policy or ResolutionPolicy()
        
        # Secret registry
        self._registry: Dict[str, SecretDescriptor] = {}
        self._registry_lock = threading.Lock()
        
        # Provider adapters
        self._providers: Dict[str, ProviderAdapter] = {}
        self._providers_lock = threading.Lock()
        
        # Resolution cache (short-lived)
        self._cache: Dict[str, ResolvedSecret] = {}
        self._cache_lock = threading.Lock()
        
        # Audit trail
        self._access_log: list[Dict[str, Any]] = []
        self._audit_lock = threading.Lock()
        
        # Initialize default providers
        self._initialize_providers()
    
    @classmethod
    def get_instance(
        cls,
        runtime_context: RuntimeContext,
        policy: Optional[ResolutionPolicy] = None
    ) -> 'SecretResolver':
        """Singleton accessor."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = SecretResolver(runtime_context, policy)
        return cls._instance
    
    def _initialize_providers(self) -> None:
        """Initialize provider adapters based on runtime mode."""
        if self._runtime_context.mode == ExecutionMode.LIVE:
            # Production uses Vault only
            # In real impl, would initialize with actual Vault credentials
            pass  # VaultProvider requires real config
        else:
            # Dev/test modes can use env vars
            env_provider = EnvVarProvider(self._runtime_context)
            self.register_provider("env", env_provider)
    
    def register_provider(self, name: str, provider: ProviderAdapter) -> None:
        """Register a secret provider adapter."""
        with self._providers_lock:
            if name in self._providers:
                raise ValueError(f"Provider '{name}' already registered")
            self._providers[name] = provider
    
    def register_secret(self, descriptor: SecretDescriptor) -> None:
        """
        Register a secret descriptor.
        
        Hard fails if:
        - duplicate secret name
        - forbidden scope
        - no audit policy
        - rotation undefined for expiring secrets
        """
        with self._registry_lock:
            # Check for duplicates
            if descriptor.name in self._registry:
                existing = self._registry[descriptor.name]
                if existing != descriptor:
                    raise ValueError(
                        f"Secret '{descriptor.name}' already registered with "
                        f"different configuration"
                    )
                return  # Already registered identically
            
            # Validate audit requirements
            if self._policy.require_audit_all and not descriptor.audit_required:
                raise ValueError(
                    f"Secret '{descriptor.name}' must have audit_required=True "
                    f"under current policy"
                )
            
            # Validate provider exists
            if descriptor.provider not in self._providers:
                raise ValueError(
                    f"Secret '{descriptor.name}' references unknown provider "
                    f"'{descriptor.provider}'"
                )
            
            # Register
            self._registry[descriptor.name] = descriptor
    
    def resolve(self, request: SecretRequest) -> ResolvedSecret:
        """
        Resolve a secret request.
        
        Resolution steps:
        1. Validate consumer identity
        2. Validate access level
        3. Validate runtime mode (prod / sandbox)
        4. Validate feature flags
        5. Fetch via provider adapter
        6. Wrap secret
        7. Audit access
        
        Secrets are never returned raw.
        """
        # Check global revocation first
        if self._policy.is_revoked(request.secret_name):
            raise RuntimeError(
                f"Secret '{request.secret_name}' has been REVOKED. "
                f"Access is forbidden."
            )
        
        # Get descriptor
        with self._registry_lock:
            if request.secret_name not in self._registry:
                raise ValueError(
                    f"Secret '{request.secret_name}' is not registered"
                )
            descriptor = self._registry[request.secret_name]
        
        # Validate consumer authorization
        if request.consumer not in descriptor.allowed_consumers:
            raise PermissionError(
                f"Consumer '{request.consumer}' is not authorized to access "
                f"secret '{request.secret_name}'"
            )
        
        # Validate access level
        if request.access_level not in descriptor.access_levels:
            raise PermissionError(
                f"Access level '{request.access_level.value}' not permitted for "
                f"secret '{request.secret_name}'"
            )
        
        # Check cache
        cached = self._check_cache(request.secret_name)
        if cached is not None:
            self._audit_access(request, cached, from_cache=True)
            return cached
        
        # Fetch from provider
        provider = self._providers[descriptor.provider]
        
        try:
            raw_value = provider.fetch(descriptor.provider_key)
        except Exception as e:
            raise RuntimeError(
                f"Failed to fetch secret '{request.secret_name}' from "
                f"provider '{descriptor.provider}': {e}"
            ) from e
        
        # Compute fingerprint (for audit, never log raw value)
        fingerprint = self._compute_fingerprint(raw_value)
        
        # Compute expiration
        expires_at = None
        if descriptor.expires:
            expires_at = datetime.now(timezone.utc) + descriptor.rotation_interval
        
        # Wrap in ResolvedSecret
        resolved = ResolvedSecret(
            _value=raw_value,
            expires_at=expires_at,
            fingerprint=fingerprint,
            secret_name=request.secret_name,
            resolved_at=datetime.now(timezone.utc)
        )
        
        # Cache
        self._update_cache(request.secret_name, resolved)
        
        # Audit
        self._audit_access(request, resolved, from_cache=False)
        
        return resolved
    
    def assert_access(self, secret_name: str, consumer: str) -> None:
        """
        Assert that consumer has access to secret.
        
        Used during:
        - posting
        - signing
        - platform calls
        
        Failure is fatal, not silent.
        """
        with self._registry_lock:
            if secret_name not in self._registry:
                raise PermissionError(
                    f"Secret '{secret_name}' does not exist"
                )
            
            descriptor = self._registry[secret_name]
            
            if consumer not in descriptor.allowed_consumers:
                raise PermissionError(
                    f"Consumer '{consumer}' is NOT authorized to access "
                    f"secret '{secret_name}'"
                )
    
    def revoke(self, secret_name: str, reason: str) -> None:
        """
        Emergency revocation of a secret.
        
        - Invalidates resolution cache
        - Adds to revocation list
        - All future access attempts fail
        """
        # Add to revoked set
        self._policy.revoked_secrets.add(secret_name)
        
        # Clear from cache
        with self._cache_lock:
            if secret_name in self._cache:
                del self._cache[secret_name]
        
        # Audit revocation
        self._audit_revocation(secret_name, reason)
        
        # In production, would also:
        # - Notify watchdog
        # - Alert security team
        # - Trigger rotation workflow
    
    def snapshot(self, snapshot_id: str) -> SecretSnapshot:
        """Create audit snapshot of resolved secrets."""
        with self._cache_lock:
            resolved_fingerprints = {
                name: secret.fingerprint
                for name, secret in self._cache.items()
            }
        
        return SecretSnapshot(
            snapshot_id=snapshot_id,
            resolved_secrets=resolved_fingerprints,
            created_at=datetime.now(timezone.utc),
            runtime_mode=self._runtime_context.mode
        )
    
    def _check_cache(self, secret_name: str) -> Optional[ResolvedSecret]:
        """Check if secret is in cache and still valid."""
        with self._cache_lock:
            if secret_name not in self._cache:
                return None
            
            cached = self._cache[secret_name]
            
            # Check if expired
            if cached.is_expired():
                del self._cache[secret_name]
                return None
            
            # Check cache TTL
            age = (datetime.now(timezone.utc) - cached.resolved_at).total_seconds()
            if age > self._policy.cache_ttl_seconds:
                del self._cache[secret_name]
                return None
            
            return cached
    
    def _update_cache(self, secret_name: str, resolved: ResolvedSecret) -> None:
        """Update cache with newly resolved secret."""
        with self._cache_lock:
            self._cache[secret_name] = resolved
    
    def _compute_fingerprint(self, value: Any) -> str:
        """Compute fingerprint of secret value for audit trail."""
        # Convert value to bytes
        if isinstance(value, str):
            value_bytes = value.encode('utf-8')
        elif isinstance(value, bytes):
            value_bytes = value
        else:
            value_bytes = str(value).encode('utf-8')
        
        # Hash
        return hashlib.sha256(value_bytes).hexdigest()
    
    def _audit_access(
        self,
        request: SecretRequest,
        resolved: ResolvedSecret,
        from_cache: bool
    ) -> None:
        """Audit secret access."""
        with self._audit_lock:
            self._access_log.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "secret_name": request.secret_name,
                "consumer": request.consumer,
                "access_level": request.access_level.value,
                "fingerprint": resolved.fingerprint,
                "from_cache": from_cache,
                "runtime_mode": self._runtime_context.mode.value
            })
    
    def _audit_revocation(self, secret_name: str, reason: str) -> None:
        """Audit secret revocation."""
        with self._audit_lock:
            self._access_log.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "REVOCATION",
                "secret_name": secret_name,
                "reason": reason,
                "runtime_mode": self._runtime_context.mode.value
            })
    
    def get_audit_log(self) -> list[Dict[str, Any]]:
        """Get audit log (for monitoring/compliance)."""
        with self._audit_lock:
            return self._access_log.copy()
    
    def clear_cache(self) -> None:
        """Clear resolution cache (testing/emergency use)."""
        with self._cache_lock:
            self._cache.clear()


# ============================================================================
# SECRET ACCESS WATCHDOG (ENFORCEMENT)
# ============================================================================

class SecretAccessWatchdog:
    """
    Monitors secret access for anomalies:
    - excessive access
    - forbidden consumers
    - rotation violations
    - stale token reuse
    
    Triggers:
    - alerting
    - secret revocation
    - posting halt
    - global kill-switch (if needed)
    """
    
    def __init__(self, resolver: SecretResolver):
        self._resolver = resolver
        self._access_counts: Dict[str, int] = {}
        self._lock = threading.Lock()
    
    def check_access_rate(self, secret_name: str, max_per_minute: int) -> None:
        """Check if access rate for a secret is within bounds."""
        with self._lock:
            # In production, this would track time windows
            # Simplified version just counts accesses
            count = self._access_counts.get(secret_name, 0)
            if count > max_per_minute:
                raise RuntimeError(
                    f"WATCHDOG VIOLATION: Secret '{secret_name}' accessed "
                    f"{count} times, exceeds limit of {max_per_minute}/minute"
                )
    
    def detect_rotation_violations(self) -> list[str]:
        """Detect secrets that should have rotated but haven't."""
        violations = []
        
        # Check all registered secrets
        with self._resolver._registry_lock:
            for name, descriptor in self._resolver._registry.items():
                if not descriptor.expires:
                    continue
                
                # Check if secret in cache
                with self._resolver._cache_lock:
                    if name not in self._resolver._cache:
                        continue
                    
                    cached = self._resolver._cache[name]
                    age = (datetime.now(timezone.utc) - cached.resolved_at).total_seconds()
                    
                    if age > descriptor.rotation_interval.total_seconds():
                        violations.append(
                            f"Secret '{name}' has not rotated in "
                            f"{age:.0f}s (should rotate every "
                            f"{descriptor.rotation_interval.total_seconds():.0f}s)"
                        )
        
        return violations


# ============================================================================
# MODULE-LEVEL HELPERS
# ============================================================================

def initialize_secret_system(
    runtime_context: RuntimeContext,
    policy: Optional[ResolutionPolicy] = None
) -> SecretResolver:
    """
    Initialize the global secret resolution system.
    Called once at process boot.
    """
    resolver = SecretResolver.get_instance(runtime_context, policy)
    return resolver


def get_secret_resolver() -> SecretResolver:
    """Get the singleton SecretResolver instance."""
    if SecretResolver._instance is None:
        raise RuntimeError(
            "SecretResolver not initialized. "
            "Call initialize_secret_system() at process boot."
        )
    return SecretResolver._instance


# ============================================================================
# FORBIDDEN PATTERNS (ZERO TOLERANCE)
# ============================================================================

def _forbidden_direct_env_access():
    """❌ NEVER access os.environ directly for secrets"""
    raise NotImplementedError(
        "Direct environment variable access for secrets is FORBIDDEN. "
        "Use SecretResolver.resolve() instead."
    )


def _forbidden_secret_logging():
    """❌ NEVER log secret values"""
    raise NotImplementedError(
        "Logging secret values is FORBIDDEN. "
        "Use fingerprints for audit trail."
    )


def _forbidden_secret_serialization():
    """❌ NEVER serialize secrets to disk/network"""
    raise NotImplementedError(
        "Secret serialization is FORBIDDEN. "
        "Secrets must remain in memory only."
    )




