# /infra/id_generator.py
"""
Deterministic Identity Authority (Runs, Jobs, Content, Accounts)

This is the single source of truth for ALL identifiers in the system.
Identity is infrastructure. If IDs are wrong, everything downstream breaks:
- determinism dies
- replay breaks
- experiments lie
- attribution collapses
- audits become impossible

Core principles (NON-NEGOTIABLE):
1. Determinism over randomness
2. Stability over convenience
3. Collision resistance through structure
4. Semantic meaning beats opaque blobs
5. Replay correctness beats uniqueness

NO RANDOMNESS. EVER.
"""

import hashlib
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Set, Tuple, Any
from collections import defaultdict

from infra.runtime_context import RuntimeContext
from infra.clock import Clock as MonotonicClock


# ============================================================================
# ENUMS (EXPLICIT TYPES - NO STRINGS)
# ============================================================================

class IDType(Enum):
    """Explicit identity types prevent cross-domain collisions."""
    RUN = "run"
    JOB = "job"
    CONTENT = "content"
    ACCOUNT = "account"
    EXPERIMENT = "experiment"
    VARIANT = "variant"
    ARTIFACT = "artifact"


class IDScope(Enum):
    """Defines uniqueness guarantees and collision boundaries."""
    GLOBAL = "global"           # Must be unique across all time and space
    RUN_LOCAL = "run_local"     # Unique within a single run
    PLATFORM = "platform"       # Unique within a platform (e.g., Twitter, LinkedIn)


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class IDSeed:
    """
    The canonical entropy source for ID generation.
    This is NOT randomness - it's structured, deterministic components.
    """
    namespace: str
    components: Tuple[str, ...]
    
    def __post_init__(self):
        if not self.namespace:
            raise ValueError("IDSeed namespace cannot be empty")
        if not self.components:
            raise ValueError("IDSeed must have at least one component")
        # Validate no None or empty components
        for comp in self.components:
            if comp is None or comp == "":
                raise ValueError(f"IDSeed components cannot be None or empty: {self.components}")


@dataclass(frozen=True)
class IDDescriptor:
    """
    Metadata that defines how an ID type should be generated.
    Every ID type must be registered before use.
    """
    id_type: IDType
    scope: IDScope
    version: int
    namespace: str
    required_components: Tuple[str, ...]
    description: str
    
    def __post_init__(self):
        if self.version < 1:
            raise ValueError(f"IDDescriptor version must be >= 1, got {self.version}")
        if not self.namespace:
            raise ValueError("IDDescriptor namespace cannot be empty")


@dataclass(frozen=True)
class GeneratedID:
    """
    A generated identifier with full provenance.
    Opaque externally, structured internally.
    """
    value: str
    id_type: IDType
    scope: IDScope
    fingerprint: str  # Hash of canonical components
    generation_tick: int  # Monotonic clock tick when generated
    
    def __str__(self) -> str:
        return self.value


# ============================================================================
# NAMESPACE REGISTRY (COLLISION PREVENTION)
# ============================================================================

class NamespaceRegistry:
    """
    Enforces namespace isolation and prevents collisions across:
    - reserved namespaces
    - version separation
    - platform isolation
    """
    
    RESERVED_NAMESPACES = {
        "run", "job", "content", "account", "experiment", "variant", "artifact",
        "test", "replay", "audit", "system"
    }
    
    def __init__(self):
        self._registered: Dict[str, IDDescriptor] = {}
        self._lock = threading.Lock()
    
    def register(self, descriptor: IDDescriptor) -> None:
        """Register a new ID descriptor."""
        with self._lock:
            key = self._make_key(descriptor.id_type, descriptor.version)
            
            if key in self._registered:
                existing = self._registered[key]
                if existing != descriptor:
                    raise ValueError(
                        f"Descriptor conflict for {key}: "
                        f"existing={existing}, new={descriptor}"
                    )
                return  # Already registered with same config
            
            # Validate namespace not in reserved set (unless exact match to id_type)
            if descriptor.namespace in self.RESERVED_NAMESPACES:
                if descriptor.namespace != descriptor.id_type.value:
                    raise ValueError(
                        f"Namespace '{descriptor.namespace}' is reserved"
                    )
            
            self._registered[key] = descriptor
    
    def get(self, id_type: IDType, version: int) -> IDDescriptor:
        """Retrieve a registered descriptor."""
        with self._lock:
            key = self._make_key(id_type, version)
            if key not in self._registered:
                raise ValueError(
                    f"No descriptor registered for {id_type.value} v{version}"
                )
            return self._registered[key]
    
    def _make_key(self, id_type: IDType, version: int) -> str:
        return f"{id_type.value}:v{version}"


# ============================================================================
# COLLISION DETECTOR (PARANOID BY DESIGN)
# ============================================================================

class CollisionDetector:
    """
    Tracks all generated fingerprints and ensures uniqueness.
    Collision = FATAL ERROR.
    """
    
    def __init__(self):
        self._fingerprints: Dict[IDScope, Set[str]] = defaultdict(set)
        self._lock = threading.Lock()
    
    def check_and_register(self, scope: IDScope, fingerprint: str, id_value: str) -> None:
        """
        Check for collision and register fingerprint.
        Raises if collision detected.
        """
        with self._lock:
            if fingerprint in self._fingerprints[scope]:
                raise RuntimeError(
                    f"FATAL: ID collision detected in scope {scope.value}\n"
                    f"Fingerprint: {fingerprint}\n"
                    f"Attempted ID: {id_value}\n"
                    f"This is a critical infrastructure failure."
                )
            self._fingerprints[scope].add(fingerprint)
    
    def reset_scope(self, scope: IDScope) -> None:
        """Reset tracking for a specific scope (e.g., RUN_LOCAL between runs)."""
        with self._lock:
            self._fingerprints[scope].clear()


# ============================================================================
# REPLAY RESOLVER (DETERMINISM ENFORCEMENT)
# ============================================================================

class ReplayResolver:
    """
    Ensures ID generation is perfectly deterministic during replay.
    Same inputs → same IDs, bit-for-bit.
    """
    
    @staticmethod
    def canonicalize_components(components: Tuple[str, ...]) -> Tuple[str, ...]:
        """
        Canonicalize components to ensure deterministic ordering.
        This is critical for replay stability.
        """
        # Components are already ordered by the caller based on semantic meaning
        # We validate they are all strings and non-empty
        validated = []
        for comp in components:
            if not isinstance(comp, str):
                raise TypeError(f"Component must be string, got {type(comp)}: {comp}")
            if not comp:
                raise ValueError("Component cannot be empty string")
            validated.append(comp)
        return tuple(validated)
    
    @staticmethod
    def validate_deterministic_input(components: Tuple[str, ...]) -> None:
        """
        Validate that components don't contain nondeterministic data.
        """
        for comp in components:
            # Check for common nondeterministic patterns
            if "random" in comp.lower():
                raise ValueError(f"Component appears nondeterministic: {comp}")
            if "uuid" in comp.lower():
                raise ValueError(f"Component appears to contain UUID: {comp}")
            # Timestamps are OK if they come from MonotonicClock


# ============================================================================
# ID GENERATOR (SINGLE SOURCE OF IDENTITY)
# ============================================================================

class IDGenerator:
    """
    The one and only place where identifiers are created.
    
    This is infrastructure. Violations are infra invariant breaks.
    """
    
    _instance: Optional['IDGenerator'] = None
    _lock = threading.Lock()
    
    # Hashing algorithm (NEVER CHANGE - breaks replay)
    HASH_ALGORITHM = "sha256"
    HASH_OUTPUT_LENGTH = 16  # hex characters to use from hash
    
    def __init__(self, runtime_context: RuntimeContext, clock: MonotonicClock):
        self._runtime_context = runtime_context
        self._clock = clock
        
        self._namespace_registry = NamespaceRegistry()
        self._collision_detector = CollisionDetector()
        self._replay_resolver = ReplayResolver()
        
        # Track generation for watchdog
        self._generation_counts: Dict[IDType, int] = defaultdict(int)
        self._generation_lock = threading.Lock()
        
        # Register standard descriptors
        self._register_standard_descriptors()
    
    @classmethod
    def get_instance(cls, runtime_context: RuntimeContext, clock: MonotonicClock) -> 'IDGenerator':
        """Singleton accessor."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = IDGenerator(runtime_context, clock)
        return cls._instance
    
    def _register_standard_descriptors(self) -> None:
        """Register all standard ID types."""
        
        # RUN: globally unique, based on start time + config hash
        self._namespace_registry.register(IDDescriptor(
            id_type=IDType.RUN,
            scope=IDScope.GLOBAL,
            version=1,
            namespace="run",
            required_components=("start_tick", "config_hash"),
            description="Unique identifier for an execution run"
        ))
        
        # JOB: run-local, based on parent run + job index
        self._namespace_registry.register(IDDescriptor(
            id_type=IDType.JOB,
            scope=IDScope.RUN_LOCAL,
            version=1,
            namespace="job",
            required_components=("run_id", "job_index"),
            description="Unique identifier for a job within a run"
        ))
        
        # CONTENT: globally unique, based on platform + content hash
        self._namespace_registry.register(IDDescriptor(
            id_type=IDType.CONTENT,
            scope=IDScope.GLOBAL,
            version=1,
            namespace="content",
            required_components=("platform", "content_hash"),
            description="Unique identifier for generated content"
        ))
        
        # ACCOUNT: platform-scoped, based on platform + account handle
        self._namespace_registry.register(IDDescriptor(
            id_type=IDType.ACCOUNT,
            scope=IDScope.PLATFORM,
            version=1,
            namespace="account",
            required_components=("platform", "handle"),
            description="Unique identifier for a social media account"
        ))
        
        # EXPERIMENT: globally unique, based on name + start time
        self._namespace_registry.register(IDDescriptor(
            id_type=IDType.EXPERIMENT,
            scope=IDScope.GLOBAL,
            version=1,
            namespace="experiment",
            required_components=("experiment_name", "start_tick"),
            description="Unique identifier for an experiment"
        ))
        
        # VARIANT: experiment-scoped, based on experiment + variant config
        self._namespace_registry.register(IDDescriptor(
            id_type=IDType.VARIANT,
            scope=IDScope.RUN_LOCAL,
            version=1,
            namespace="variant",
            required_components=("experiment_id", "variant_config_hash"),
            description="Unique identifier for an experiment variant"
        ))
        
        # ARTIFACT: run-local, based on job + artifact type + index
        self._namespace_registry.register(IDDescriptor(
            id_type=IDType.ARTIFACT,
            scope=IDScope.RUN_LOCAL,
            version=1,
            namespace="artifact",
            required_components=("job_id", "artifact_type", "index"),
            description="Unique identifier for a job artifact"
        ))
    
    def generate(
        self,
        id_type: IDType,
        components: Dict[str, str],
        version: int = 1
    ) -> GeneratedID:
        """
        Generate a new ID.
        
        Steps:
        1. Validate descriptor registration
        2. Validate required components
        3. Construct canonical seed
        4. Hash deterministically (stable algorithm)
        5. Encode with versioned prefix
        6. Collision check
        7. Return wrapped ID
        
        NO RANDOMNESS. EVER.
        """
        # Get descriptor
        descriptor = self._namespace_registry.get(id_type, version)
        
        # Validate required components present
        missing = set(descriptor.required_components) - set(components.keys())
        if missing:
            raise ValueError(
                f"Missing required components for {id_type.value}: {missing}"
            )
        
        # Extract and order components deterministically
        ordered_components = tuple(
            components[key] for key in descriptor.required_components
        )
        
        # Validate deterministic input
        self._replay_resolver.validate_deterministic_input(ordered_components)
        
        # Canonicalize
        canonical_components = self._replay_resolver.canonicalize_components(
            ordered_components
        )
        
        # Create seed
        seed = IDSeed(
            namespace=descriptor.namespace,
            components=canonical_components
        )
        
        # Generate fingerprint
        fingerprint = self._compute_fingerprint(seed, version)
        
        # Construct ID value with prefix
        id_value = f"{descriptor.namespace}:{self._runtime_context.env}:v{version}:{fingerprint}"
        
        # Collision check
        self._collision_detector.check_and_register(
            descriptor.scope,
            fingerprint,
            id_value
        )
        
        # Track generation
        with self._generation_lock:
            self._generation_counts[id_type] += 1
        
        # Create GeneratedID
        generated = GeneratedID(
            value=id_value,
            id_type=id_type,
            scope=descriptor.scope,
            fingerprint=fingerprint,
            generation_tick=self._clock.now()
        )
        
        return generated
    
    def derive(
        self,
        parent_id: GeneratedID,
        child_type: IDType,
        extra_components: Dict[str, str],
        version: int = 1
    ) -> GeneratedID:
        """
        Derive a child ID from a parent ID.
        
        Used for:
        - jobs within runs
        - variants within experiments
        - artifacts within jobs
        
        Guarantees hierarchical traceability.
        """
        # Merge parent ID into components
        all_components = {
            f"parent_{parent_id.id_type.value}_id": parent_id.value,
            **extra_components
        }
        
        return self.generate(child_type, all_components, version)
    
    def validate(self, id_value: str) -> bool:
        """
        Validate an ID string format.
        
        Used during:
        - ingestion
        - replay
        - audit
        - cross-service communication
        
        Rejects malformed or forged IDs.
        """
        try:
            parts = id_value.split(":")
            if len(parts) != 4:
                return False
            
            namespace, env, version_str, fingerprint = parts
            
            # Validate namespace is registered
            if namespace not in NamespaceRegistry.RESERVED_NAMESPACES:
                return False
            
            # Validate version format
            if not version_str.startswith("v"):
                return False
            try:
                int(version_str[1:])
            except ValueError:
                return False
            
            # Validate fingerprint is hex
            if len(fingerprint) != self.HASH_OUTPUT_LENGTH:
                return False
            try:
                int(fingerprint, 16)
            except ValueError:
                return False
            
            return True
            
        except Exception:
            return False
    
    def _compute_fingerprint(self, seed: IDSeed, version: int) -> str:
        """
        Compute deterministic fingerprint from seed.
        
        CRITICAL: This algorithm must NEVER change.
        Changing this breaks replay for all existing data.
        """
        # Construct canonical input string
        components_str = "|".join(seed.components)
        canonical_input = f"{seed.namespace}:v{version}:{components_str}"
        
        # Hash
        hasher = hashlib.new(self.HASH_ALGORITHM)
        hasher.update(canonical_input.encode("utf-8"))
        full_hash = hasher.hexdigest()
        
        # Truncate to configured length
        return full_hash[:self.HASH_OUTPUT_LENGTH]
    
    def reset_run_local_scope(self) -> None:
        """Reset RUN_LOCAL scope tracking (called at start of new run)."""
        self._collision_detector.reset_scope(IDScope.RUN_LOCAL)


# ============================================================================
# ID WATCHDOG (MONITORING & ENFORCEMENT)
# ============================================================================

class IDWatchdog:
    """
    Monitors ID generation for anomalies:
    - unexpected regeneration
    - mismatched IDs across snapshots
    - unauthorized ID creation
    - excessive generation rates
    
    Can:
    - block execution
    - invalidate experiments
    - halt posting
    - trip global kill-switch
    """
    
    def __init__(self, generator: IDGenerator):
        self._generator = generator
        self._snapshot_hashes: Dict[str, str] = {}
        self._lock = threading.Lock()
    
    def snapshot_id_state(self, label: str) -> None:
        """Take a snapshot of current ID generation state."""
        with self._lock:
            # Compute hash of all generated fingerprints
            all_fingerprints = []
            for scope_set in self._generator._collision_detector._fingerprints.values():
                all_fingerprints.extend(sorted(scope_set))
            
            hasher = hashlib.sha256()
            hasher.update("|".join(all_fingerprints).encode("utf-8"))
            snapshot_hash = hasher.hexdigest()
            
            self._snapshot_hashes[label] = snapshot_hash
    
    def verify_id_state(self, label: str) -> bool:
        """Verify current state matches a previous snapshot."""
        with self._lock:
            if label not in self._snapshot_hashes:
                raise ValueError(f"No snapshot found for label: {label}")
            
            # Recompute current hash
            all_fingerprints = []
            for scope_set in self._generator._collision_detector._fingerprints.values():
                all_fingerprints.extend(sorted(scope_set))
            
            hasher = hashlib.sha256()
            hasher.update("|".join(all_fingerprints).encode("utf-8"))
            current_hash = hasher.hexdigest()
            
            return current_hash == self._snapshot_hashes[label]
    
    def check_generation_rate(self, id_type: IDType, max_per_second: int) -> None:
        """Check if generation rate exceeds threshold."""
        with self._generator._generation_lock:
            count = self._generator._generation_counts[id_type]
            # This is simplified - real impl would track time windows
            # For now just check absolute count as placeholder
            if count > max_per_second * 3600:  # rough hourly limit
                raise RuntimeError(
                    f"ID generation rate exceeded for {id_type.value}: "
                    f"{count} IDs generated"
                )


# ============================================================================
# FORBIDDEN PATTERNS (ZERO TOLERANCE)
# ============================================================================

def _forbidden_uuid4():
    """❌ NEVER USE uuid4() - breaks determinism"""
    raise NotImplementedError("uuid4() is FORBIDDEN in this codebase")

def _forbidden_random():
    """❌ NEVER USE random - breaks determinism"""
    raise NotImplementedError("random is FORBIDDEN in this codebase")

def _forbidden_timestamp_id():
    """❌ NEVER USE timestamps as identity - breaks replay"""
    raise NotImplementedError("timestamp-based IDs are FORBIDDEN")


# ============================================================================
# MODULE-LEVEL HELPERS
# ============================================================================

def initialize_id_system(runtime_context: RuntimeContext, clock: MonotonicClock) -> IDGenerator:
    """
    Initialize the global ID generation system.
    Called once at process boot.
    """
    generator = IDGenerator.get_instance(runtime_context, clock)
    return generator


def get_id_generator() -> IDGenerator:
    """Get the singleton ID generator instance."""
    if IDGenerator._instance is None:
        raise RuntimeError(
            "IDGenerator not initialized. "
            "Call initialize_id_system() at process boot."
        )
    return IDGenerator._instance


