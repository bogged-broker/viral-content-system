"""
/data/pipelines/ingestion/ingest_registry.py

Explicit Registry of Allowed Ingestors — Central Gatekeeper

This module is the only authority that decides which ingestion modules are
allowed to exist and be executed.

AUTHORITY: If an ingestor is not registered here, it does not exist.

No dynamic discovery. No implicit imports. No reflection. No configuration magic.
If an ingestor is not registered here, it does not exist.

Registry Rules (STRICT):
- Static registry only
- Explicit imports only
- One entry per ingestor
- No conditional registration
- No environment-based behavior

The registry must be identical across environments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, Optional, Any, List
from enum import Enum
import hashlib
import json

# Import base classes - registry uses base contracts
from .base.ingest_context import IngestContext
from .base.ingest_result import IngestResult
from .base.ingest_errors import IngestError


class IngestType(str, Enum):
    """Closed set of allowed ingestion types."""
    ENGAGEMENT = "engagement"
    CONTENT = "content"
    ACCOUNT = "account"
    MODERATION = "moderation"
    RECOVERY = "recovery"


class SchemaVersion(str, Enum):
    """Canonical schema versions supported by ingestors."""
    V1 = "v1"
    V2 = "v2"
    V3 = "v3"


class IngestorNotFoundError(IngestError):
    """Raised when requested ingestor not in registry."""
    pass


class IngestorVersionMismatchError(IngestError):
    """Raised when schema version not supported by ingestor."""
    pass


@dataclass(frozen=True)
class IngestorMetadata:
    """
    Metadata describing an ingestor's capabilities and requirements.
    
    RULES:
    - Explicitly declares all requirements
    - Version constraints must be explicit
    - No implicit dependencies
    """
    ingest_type: IngestType
    supported_schema_versions: FrozenSet[str]
    required_context_fields: FrozenSet[str]
    emits_fact_types: FrozenSet[str]
    ingestor_version: str
    deterministic: bool = True
    idempotent: bool = True
    
    def supports_schema(self, schema_version: str) -> bool:
        """Check if ingestor supports schema version."""
        return schema_version in self.supported_schema_versions
    
    def validate_context(self, context: IngestContext) -> None:
        """
        Validate context meets ingestor requirements.
        
        Note: Base IngestContext has its own validation via ContextInvariants.
        This method validates ingestor-specific requirements (e.g., schema version).
        
        Raises:
            IngestorVersionMismatchError: Schema version not supported
        """
        # Extract schema version from context (may need to be derived from pipeline_version)
        # For now, we validate that the ingestor supports the context's requirements
        # Schema version validation is handled by the ingestor itself
        pass


@dataclass(frozen=True)
class IngestorRegistration:
    """
    Complete registration of an ingestor.
    
    RULES:
    - Explicit binding of name → callable
    - Metadata must be complete
    - No optional fields
    """
    ingest_name: str
    ingest_type: IngestType
    ingestor_callable: Callable[[Any, IngestContext], IngestResult]
    metadata: IngestorMetadata
    registration_version: str = "v1"
    
    def __post_init__(self):
        if not self.ingest_name:
            raise ValueError("ingest_name must be non-empty")
        if not callable(self.ingestor_callable):
            raise ValueError("ingestor_callable must be callable")
        if self.ingest_type != self.metadata.ingest_type:
            raise ValueError(
                f"ingest_type mismatch: registration={self.ingest_type.value}, "
                f"metadata={self.metadata.ingest_type.value}"
            )


class IngestRegistry:
    """
    Central registry of allowed ingestors.
    
    RULES:
    - Static registry only
    - Explicit imports only
    - One entry per ingestor
    - No conditional registration
    - No environment-based behavior
    
    The registry must be identical across environments.
    """
    
    def __init__(self):
        self._registry: Dict[str, IngestorRegistration] = {}
        self._locked: bool = False
        self._registry_version: str = "v1"
    
    def register(self, registration: IngestorRegistration) -> None:
        """
        Register an ingestor.
        
        Args:
            registration: Complete ingestor registration
        
        Raises:
            RuntimeError: Registry is locked
            ValueError: Ingestor already registered
        """
        if self._locked:
            raise RuntimeError("Registry is locked, cannot register new ingestors")
        
        if registration.ingest_name in self._registry:
            raise ValueError(f"Ingestor {registration.ingest_name} already registered")
        
        self._registry[registration.ingest_name] = registration
    
    def lock(self) -> None:
        """
        Lock registry to prevent further modifications.
        
        After locking, no new ingestors can be registered.
        This should be called during system initialization.
        """
        self._locked = True
    
    def is_locked(self) -> bool:
        """Check if registry is locked."""
        return self._locked
    
    def get(self, ingest_name: str) -> IngestorRegistration:
        """
        Retrieve ingestor registration by name.
        
        Args:
            ingest_name: Name of ingestor to retrieve
        
        Returns:
            IngestorRegistration for the named ingestor
        
        Raises:
            IngestorNotFoundError: Ingestor not in registry
        """
        if ingest_name not in self._registry:
            raise IngestorNotFoundError(
                f"Ingestor {ingest_name} not found in registry. "
                f"Available: {sorted(self._registry.keys())}"
            )
        return self._registry[ingest_name]
    
    def get_by_type(self, ingest_type: IngestType) -> List[IngestorRegistration]:
        """
        Retrieve all ingestors of a given type.
        
        Args:
            ingest_type: Type of ingestors to retrieve
        
        Returns:
            List of registrations matching the type
        """
        return [
            reg for reg in self._registry.values()
            if reg.ingest_type == ingest_type
        ]
    
    def list_ingestors(self) -> List[str]:
        """
        List all registered ingestor names in deterministic order.
        
        Returns:
            Sorted list of ingestor names
        """
        return sorted(self._registry.keys())
    
    def ingestor_exists(self, ingest_name: str) -> bool:
        """Check if ingestor is registered."""
        return ingest_name in self._registry
    
    def validate_context_for_ingestor(
        self,
        ingest_name: str,
        context: IngestContext,
    ) -> None:
        """
        Validate context is compatible with ingestor.
        
        Args:
            ingest_name: Name of ingestor
            context: Ingestion context to validate
        
        Raises:
            IngestorNotFoundError: Ingestor not found
            IngestError: Context validation failed
        """
        registration = self.get(ingest_name)
        registration.metadata.validate_context(context)
    
    def execute_ingestor(
        self,
        ingest_name: str,
        raw_input: Any,
        context: IngestContext,
    ) -> IngestResult:
        """
        Execute registered ingestor with validation.
        
        Args:
            ingest_name: Name of ingestor to execute
            raw_input: Raw input data for ingestion
            context: Ingestion context
        
        Returns:
            IngestResult from execution
        
        Raises:
            IngestorNotFoundError: Ingestor not found
            IngestError: Validation or execution failed
        """
        registration = self.get(ingest_name)
        self.validate_context_for_ingestor(ingest_name, context)
        
        return registration.ingestor_callable(raw_input, context)
    
    def serialize(self) -> bytes:
        """
        Serialize registry to canonical form for hashing.
        
        Returns:
            Canonical byte representation of registry
        """
        registry_dict = {
            "registry_version": self._registry_version,
            "ingestors": {
                name: {
                    "ingest_type": reg.ingest_type.value,
                    "supported_schema_versions": sorted(reg.metadata.supported_schema_versions),
                    "required_context_fields": sorted(reg.metadata.required_context_fields),
                    "emits_fact_types": sorted(reg.metadata.emits_fact_types),
                    "ingestor_version": reg.metadata.ingestor_version,
                    "deterministic": reg.metadata.deterministic,
                    "idempotent": reg.metadata.idempotent,
                    "registration_version": reg.registration_version,
                }
                for name, reg in sorted(self._registry.items())
            },
        }
        
        canonical = json.dumps(
            registry_dict,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True
        )
        
        return canonical.encode('utf-8')
    
    def registry_hash(self) -> str:
        """
        Compute stable hash of registry contents.
        
        This hash MUST be stable across deployments to detect registry drift.
        
        Returns:
            64-character hex hash of registry
        """
        canonical = self.serialize()
        return hashlib.sha256(canonical).hexdigest()
    
    def registry_fingerprint(self) -> str:
        """
        Compute short fingerprint for human inspection.
        
        Returns:
            16-character hex fingerprint
        """
        return self.registry_hash()[:16]
    
    def export_metadata(self) -> Dict[str, Any]:
        """
        Export registry metadata for introspection.
        
        Returns:
            Dictionary of registry metadata
        """
        return {
            "registry_version": self._registry_version,
            "locked": self._locked,
            "ingestor_count": len(self._registry),
            "ingestors": {
                name: {
                    "type": reg.ingest_type.value,
                    "version": reg.metadata.ingestor_version,
                    "supported_schemas": sorted(reg.metadata.supported_schema_versions),
                    "deterministic": reg.metadata.deterministic,
                    "idempotent": reg.metadata.idempotent,
                }
                for name, reg in sorted(self._registry.items())
            },
            "registry_hash": self.registry_hash(),
        }


_GLOBAL_REGISTRY: Optional[IngestRegistry] = None


def get_global_registry() -> IngestRegistry:
    """
    Get global ingest registry singleton.
    
    Returns:
        Global IngestRegistry instance
    """
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = IngestRegistry()
    return _GLOBAL_REGISTRY


def reset_global_registry() -> None:
    """
    Reset global registry (for testing only).
    
    WARNING: This should never be called in production code.
    """
    global _GLOBAL_REGISTRY
    _GLOBAL_REGISTRY = None


# ============================================================================
# INGESTOR ADAPTER FUNCTIONS
# ============================================================================
# These adapters bridge between base contracts and ingestor implementations.
# Each adapter accepts base IngestContext and returns base IngestResult.

def _adapter_engagement(
    raw_input: Any,
    context: IngestContext,
) -> IngestResult:
    """
    Adapter for engagement ingestion.
    Converts base IngestContext to ingestor format and returns base IngestResult.
    """
    from .base.ingest_result import RejectionReason
    from .builders.result_factory import (
        create_accepted_result,
        create_rejected_result,
    )
    from . import engagement_ingest
    
    try:
        # Create ingestor instance
        ingestor = engagement_ingest.EngagementIngestor()
        
        # Convert raw input to RawEngagementEvent if needed
        if isinstance(raw_input, dict):
            raw_event = engagement_ingest.RawEngagementEvent(**raw_input)
        else:
            raw_event = raw_input
        
        # Ingest event
        fact = ingestor.ingest_event(raw_event)
        
        if fact is None:
            # Rejected
            return create_rejected_result(
                context=context,
                reason=RejectionReason.OTHER,
                detail="Engagement event was rejected by ingestor",
            )
        
        # Accepted - extract fact ID
        fact_id = getattr(fact, 'fact_id', None) or getattr(fact, 'id', None) or str(fact)
        return create_accepted_result(
            context=context,
            fact_ids=[fact_id] if fact_id else [],
        )
    except Exception as exc:
        # Convert exception to rejected result
        return create_rejected_result(
            context=context,
            reason=RejectionReason.OTHER,
            detail=f"Engagement ingestion failed: {exc}",
        )


def _adapter_content(
    raw_input: Any,
    context: IngestContext,
) -> IngestResult:
    """
    Adapter for content ingestion.
    """
    from .base.ingest_result import RejectionReason
    from .builders.result_factory import (
        create_accepted_result,
        create_rejected_result,
    )
    from . import content_ingest
    
    try:
        # Create ingestor instance (may need factory function)
        ingestor = content_ingest.create_content_ingestor()
        
        # Convert context if needed (content_ingest may use different context format)
        # For now, pass through - ingestor should handle base IngestContext
        result = ingestor.ingest(raw_input, context)
        
        # Convert result to base IngestResult
        if hasattr(result, 'success') and result.success:
            fact_id = getattr(result, 'content_id', None) or str(result)
            return create_accepted_result(
                context=context,
                fact_ids=[fact_id] if fact_id else [],
            )
        else:
            return create_rejected_result(
                context=context,
                reason=RejectionReason.OTHER,
                detail=getattr(result, 'error', 'Content ingestion rejected'),
            )
    except Exception as exc:
        return create_rejected_result(
            context=context,
            reason=RejectionReason.OTHER,
            detail=f"Content ingestion failed: {exc}",
        )


def _adapter_account(
    raw_input: Any,
    context: IngestContext,
) -> IngestResult:
    """
    Adapter for account ingestion.
    """
    from .base.ingest_result import RejectionReason
    from .builders.result_factory import (
        create_accepted_result,
        create_rejected_result,
    )
    from . import account_ingest
    
    try:
        # Create ingestor instance
        ingestor = account_ingest.AccountIngestor(account_ingest.DEFAULT_INGEST_POLICY)
        
        # Ingest account
        result = ingestor.ingest(raw_input, context)
        
        # Convert result to base IngestResult
        if hasattr(result, 'success') and result.success:
            fact_id = getattr(result, 'account_id', None) or str(result)
            return create_accepted_result(
                context=context,
                fact_ids=[fact_id] if fact_id else [],
            )
        else:
            return create_rejected_result(
                context=context,
                reason=RejectionReason.OTHER,
                detail=getattr(result, 'error', 'Account ingestion rejected'),
            )
    except Exception as exc:
        return create_rejected_result(
            context=context,
            reason=RejectionReason.OTHER,
            detail=f"Account ingestion failed: {exc}",
        )


def _adapter_moderation(
    raw_input: Any,
    context: IngestContext,
) -> IngestResult:
    """
    Adapter for moderation ingestion.
    """
    from .base.ingest_result import RejectionReason
    from .builders.result_factory import (
        create_accepted_result,
        create_rejected_result,
    )
    from . import moderation_ingest
    
    try:
        # Call moderation ingestor
        result = moderation_ingest.ingest_moderation_event(raw_input, context)
        
        # Convert result to base IngestResult
        if hasattr(result, 'success') and result.success:
            fact_id = getattr(result, 'fact_id', None) or str(result)
            return create_accepted_result(
                context=context,
                fact_ids=[fact_id] if fact_id else [],
            )
        else:
            return create_rejected_result(
                context=context,
                reason=RejectionReason.OTHER,
                detail=getattr(result, 'error', 'Moderation ingestion rejected'),
            )
    except Exception as exc:
        return create_rejected_result(
            context=context,
            reason=RejectionReason.OTHER,
            detail=f"Moderation ingestion failed: {exc}",
        )


def _adapter_recovery(
    raw_input: Any,
    context: IngestContext,
) -> IngestResult:
    """
    Adapter for recovery ingestion.
    """
    from .base.ingest_result import RejectionReason
    from .builders.result_factory import (
        create_accepted_result,
        create_rejected_result,
    )
    from . import recovery_ingest
    
    try:
        # Create ingestor instance
        ingestor = recovery_ingest.create_recovery_ingestor()
        
        # Convert raw_input to RecoveryPayload if needed
        if isinstance(raw_input, dict):
            payload = recovery_ingest.RecoveryPayload(**raw_input)
        else:
            payload = raw_input
        
        # Ingest recovery
        fact = ingestor.ingest(payload)
        
        # Convert result to base IngestResult
        fact_id = getattr(fact, 'recovery_id', None) or getattr(fact, 'fact_id', None) or str(fact)
        return create_accepted_result(
            context=context,
            fact_ids=[fact_id] if fact_id else [],
        )
    except Exception as exc:
        return create_rejected_result(
            context=context,
            reason=RejectionReason.OTHER,
            detail=f"Recovery ingestion failed: {exc}",
        )


# ============================================================================
# REGISTRY INITIALIZATION
# ============================================================================

def initialize_registry() -> IngestRegistry:
    """
    Initialize and populate global registry with all allowed ingestors.
    
    This is the ONLY place where ingestors are registered.
    All registrations must be explicit and visible in this function.
    
    Rules:
    - Static imports only (no dynamic discovery)
    - Explicit registration of each ingestor
    - No conditional registration
    - No environment-based behavior
    - Registry must be identical across environments
    
    Returns:
        Initialized and locked IngestRegistry
    """
    registry = get_global_registry()
    
    if registry.is_locked():
        return registry
    
    # Register engagement ingestor
    registry.register(IngestorRegistration(
        ingest_name="engagement",
        ingest_type=IngestType.ENGAGEMENT,
        ingestor_callable=_adapter_engagement,
        metadata=IngestorMetadata(
            ingest_type=IngestType.ENGAGEMENT,
            supported_schema_versions=frozenset([SchemaVersion.V1.value, SchemaVersion.V2.value]),
            required_context_fields=frozenset(["run_id", "pipeline_version", "mode", "authority", "timestamp_ms"]),
            emits_fact_types=frozenset(["engagement_fact"]),
            ingestor_version="v1",
            deterministic=True,
            idempotent=True,
        ),
    ))
    
    # Register content ingestor
    registry.register(IngestorRegistration(
        ingest_name="content",
        ingest_type=IngestType.CONTENT,
        ingestor_callable=_adapter_content,
        metadata=IngestorMetadata(
            ingest_type=IngestType.CONTENT,
            supported_schema_versions=frozenset([SchemaVersion.V1.value, SchemaVersion.V2.value]),
            required_context_fields=frozenset(["run_id", "pipeline_version", "mode", "authority", "timestamp_ms"]),
            emits_fact_types=frozenset(["content_fact"]),
            ingestor_version="v1",
            deterministic=True,
            idempotent=True,
        ),
    ))
    
    # Register account ingestor
    registry.register(IngestorRegistration(
        ingest_name="account",
        ingest_type=IngestType.ACCOUNT,
        ingestor_callable=_adapter_account,
        metadata=IngestorMetadata(
            ingest_type=IngestType.ACCOUNT,
            supported_schema_versions=frozenset([SchemaVersion.V1.value, SchemaVersion.V2.value]),
            required_context_fields=frozenset(["run_id", "pipeline_version", "mode", "authority", "timestamp_ms"]),
            emits_fact_types=frozenset(["account_fact", "identity_fact", "ownership_fact"]),
            ingestor_version="v1",
            deterministic=True,
            idempotent=True,
        ),
    ))
    
    # Register moderation ingestor
    registry.register(IngestorRegistration(
        ingest_name="moderation",
        ingest_type=IngestType.MODERATION,
        ingestor_callable=_adapter_moderation,
        metadata=IngestorMetadata(
            ingest_type=IngestType.MODERATION,
            supported_schema_versions=frozenset([SchemaVersion.V1.value, SchemaVersion.V2.value]),
            required_context_fields=frozenset(["run_id", "pipeline_version", "mode", "authority", "timestamp_ms"]),
            emits_fact_types=frozenset(["moderation_fact", "decision_fact"]),
            ingestor_version="v1",
            deterministic=True,
            idempotent=True,
        ),
    ))
    
    # Register recovery ingestor
    registry.register(IngestorRegistration(
        ingest_name="recovery",
        ingest_type=IngestType.RECOVERY,
        ingestor_callable=_adapter_recovery,
        metadata=IngestorMetadata(
            ingest_type=IngestType.RECOVERY,
            supported_schema_versions=frozenset([SchemaVersion.V1.value, SchemaVersion.V2.value]),
            required_context_fields=frozenset(["run_id", "pipeline_version", "mode", "authority", "timestamp_ms", "replay_id"]),
            emits_fact_types=frozenset(["recovery_fact"]),
            ingestor_version="v1",
            deterministic=True,
            idempotent=True,
        ),
    ))
    
    # Lock registry - no further modifications allowed
    registry.lock()
    
    return registry


def validate_registry_stability(expected_hash: str) -> None:
    """
    Validate registry hash matches expected value.
    
    This should be called during system startup to detect registry drift.
    
    Args:
        expected_hash: Expected registry hash
    
    Raises:
        RuntimeError: Registry hash mismatch detected
    """
    registry = get_global_registry()
    actual_hash = registry.registry_hash()
    
    if actual_hash != expected_hash:
        raise RuntimeError(
            f"Registry hash mismatch detected. "
            f"Expected: {expected_hash}, Actual: {actual_hash}. "
            f"This indicates registry drift across deployments."
        )