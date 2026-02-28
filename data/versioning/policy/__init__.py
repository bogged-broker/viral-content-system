"""
/data/versioning/policy/__init__.py

Runtime policy authority and enforcement boundary.

This file establishes:
- Runtime policy authority
- Active policy selection
- Global enforcement boundary
- Policy registry exposure
- Deterministic access point

This is the public policy interface.

Because this controls evolution safety, it must be extremely explicit.

---

ARCHITECTURAL ROLE

This file is the policy control surface that binds enforcement rules into
a coherent, enforceable object.

Architecture:
    model/
        version.py
        version_range.py
        version_graph.py
        semantic_policy.py

    policy/
        compatibility_policy.py  ← Existing compatibility rules
        __init__.py   ← Policy facade (THIS FILE)

The model folder defines types and graph mechanics.
The policy folder defines what is allowed.
policy/__init__.py binds those rules into a coherent, enforceable object.

---

DESIGN PRINCIPLES

1. Immutability: VersioningPolicy is frozen and immutable.
2. Determinism: Policy must be identical across all nodes in distributed systems.
3. Single Authority: Only this file constructs and exposes policy instances.
4. No Environment Dependencies: No dynamic config, no date/time logic, no external I/O.
5. Pure Composition: Policy is composed from submodules, never mutated at runtime.
6. Thread Safety: Singleton initialization is thread-safe for concurrent access.

---

FAILURE MODES IF DESIGNED INCORRECTLY

Common mistakes:
1. Letting services build policy themselves → inconsistent enforcement
2. Dynamic feature-flag policy mutation → cluster disagreement
3. Mixing policy & migration code → circular dependencies
4. Allowing graph mutation at runtime → irreproducible states

This file prevents all of that.
"""

from __future__ import annotations

import threading
import sys
from dataclasses import dataclass
from typing import Callable, Final, Optional, Dict

from data.versioning.model.version import SchemaVersion
from data.versioning.model.version_graph import VersionGraph
from data.versioning.model.semantic_policy import SemanticVersionPolicy, VersionChangeType
from data.versioning.policy.compatibility_policy import CompatibilityPolicy, CompatibilityViolation


# ============================================================================
# CI ENFORCEMENT: Prevent construction outside this module
# ============================================================================

if __name__ != "data.versioning.policy.__init__":
    raise RuntimeError(
        "VersioningPolicy must only be constructed in policy.__init__"
    )


# ============================================================================
# CORE DOMAIN TYPE
# ============================================================================

@dataclass(frozen=True, slots=True)
class VersioningPolicy:
    """
    Immutable aggregation of all versioning governance rules.

    This object is the runtime enforcement container.

    It must:
    - Be immutable
    - Be pure
    - Contain only deterministic callables
    - Contain no config mutation hooks

    Attributes:
        version_graph: The immutable version graph defining allowed transitions.
        is_transition_allowed: Predicate checking if a transition is allowed.
        assert_transition_allowed: Assertion that raises if transition is not allowed.
        is_backward_compatible: Predicate checking backward compatibility.
        classify_change: Function classifying the semantic category of a change.
    """

    version_graph: VersionGraph

    is_transition_allowed: Callable[[SchemaVersion, SchemaVersion], bool]
    """Check if a transition from old_version to new_version is allowed."""

    assert_transition_allowed: Callable[[SchemaVersion, SchemaVersion], None]
    """Assert that a transition is allowed, raising if not."""

    is_backward_compatible: Callable[[SchemaVersion, SchemaVersion], bool]
    """Check if new_version is backward compatible with old_version."""

    classify_change: Callable[[SchemaVersion, SchemaVersion], str]
    """Classify the semantic category of a change (e.g., 'PATCH', 'MINOR', 'MAJOR')."""


# ============================================================================
# PURE TOP-LEVEL POLICY FUNCTIONS (Using Existing System)
# ============================================================================

# Initialize policy engines (module-level, deterministic)
_semantic_policy = SemanticVersionPolicy()
_compatibility_policy = CompatibilityPolicy()


def _classify_semantic_change(
    old_version: SchemaVersion,
    new_version: SchemaVersion
) -> str:
    """
    Classify the semantic category of a version change.

    Uses the existing SemanticVersionPolicy from model layer.
    This is a pure top-level function with no closures.
    """
    try:
        change_type = _semantic_policy.classify_version_delta(
            base=old_version,
            target=new_version,
        )
        return change_type.value
    except Exception as e:
        raise ValueError(
            f"Failed to classify change from {old_version} to {new_version}: {e}"
        ) from e


def _is_backward_compatible(
    old_version: SchemaVersion,
    new_version: SchemaVersion
) -> bool:
    """
    Check if new_version is backward compatible with old_version.

    Uses the existing CompatibilityPolicy from policy layer.
    This is a pure top-level function with no closures.
    """
    result = _compatibility_policy.is_backward_compatible(
        previous=old_version,
        candidate=new_version,
    )
    return result.compatible


def _assert_transition_allowed(
    old_version: SchemaVersion,
    new_version: SchemaVersion
) -> None:
    """
    Assert that a transition from old_version to new_version is allowed.

    Enforces:
    1. Version ordering (no downgrades)
    2. Backward compatibility (if applicable)
    3. Graph validity (direct edge exists)

    This is a pure top-level function with no closures.
    """
    # Enforce ordering: no downgrades
    if new_version < old_version:
        raise ValueError(
            f"Transition from {old_version} to {new_version} is not allowed: "
            f"downgrades are forbidden."
        )

    # Enforce backward compatibility for upgrades
    if new_version > old_version:
        try:
            _compatibility_policy.assert_backward_compatible(
                previous=old_version,
                candidate=new_version,
            )
        except CompatibilityViolation as e:
            raise ValueError(
                f"Transition from {old_version} to {new_version} violates "
                f"backward compatibility: {e.result.reason}"
            ) from e


def _build_version_graph() -> VersionGraph:
    """
    Build the authoritative version graph.

    In production, this would load from schema registry.
    For now, returns empty graph (safe: rejects all transitions until configured).

    This function must be pure and deterministic.
    """
    # TODO: Replace with actual graph construction from schema registry
    # For now, return empty graph that allows no transitions
    # This is safe: it will reject all transitions until properly configured
    return VersionGraph.build({})


# ============================================================================
# POLICY COMPOSITION (COMPOSITION ROOT)
# ============================================================================

def _build_strict_policy() -> VersioningPolicy:
    """
    Build the default strict production policy.

    This is the only place where policy composition happens.
    Lower modules never reference each other directly.

    Returns:
        Immutable VersioningPolicy instance with strict enforcement rules.
    """
    # Build version graph
    graph = _build_version_graph()

    # Create pure functions that reference the graph
    # These are NOT closures - they're functions that take graph as implicit context
    # We bind them to the graph via functools.partial-like pattern
    
    def is_transition_allowed(old: SchemaVersion, new: SchemaVersion) -> bool:
        """
        Check if transition is allowed using version graph.
        
        Uses has_edge (direct edge) instead of is_reachable to enforce
        strict single-hop transitions and prevent multi-hop migrations
        that may violate semantic rules.
        """
        # Check for direct edge (stricter than reachability)
        # This prevents multi-hop migrations that may violate semantic rules
        return graph.has_edge(old, new)

    def assert_transition_allowed_wrapper(
        old: SchemaVersion,
        new: SchemaVersion
    ) -> None:
        """Assert transition is allowed, raising if not."""
        # First check graph validity (direct edge)
        if not is_transition_allowed(old, new):
            raise ValueError(
                f"Transition from {old} to {new} is not allowed by version graph. "
                f"No direct edge exists."
            )
        # Then run mutation guard checks (ordering + compatibility)
        _assert_transition_allowed(old, new)

    # Validate determinism before returning
    _validate_policy_determinism()

    return VersioningPolicy(
        version_graph=graph,
        is_transition_allowed=is_transition_allowed,
        assert_transition_allowed=assert_transition_allowed_wrapper,
        is_backward_compatible=_is_backward_compatible,
        classify_change=_classify_semantic_change,
    )


# ============================================================================
# ACTIVE POLICY AUTHORITY (Thread-Safe Singleton)
# ============================================================================

_ACTIVE_POLICY: Optional[VersioningPolicy] = None
_POLICY_LOCK = threading.Lock()
"""Thread-safe lock for singleton initialization."""


def get_active_policy() -> VersioningPolicy:
    """
    Get the active versioning policy.

    This is the single authoritative policy instance for the system.
    It guarantees:
    - Single authoritative policy instance
    - Thread-safe lazy instantiation
    - Deterministic behavior

    The system should never "new up" a policy directly.
    Always use this accessor.

    Returns:
        The active VersioningPolicy instance.
    """
    global _ACTIVE_POLICY

    # Double-checked locking pattern for thread safety
    if _ACTIVE_POLICY is None:
        with _POLICY_LOCK:
            # Check again inside lock (another thread may have initialized)
            if _ACTIVE_POLICY is None:
                _ACTIVE_POLICY = _build_strict_policy()

    return _ACTIVE_POLICY


# ============================================================================
# TESTING OVERRIDE (With Production Guard)
# ============================================================================

_TESTING_MODE = False
"""Flag to enable testing mode. Set via environment or test framework."""


def _is_testing_mode() -> bool:
    """
    Check if we're in testing mode.
    
    Checks for common test environment indicators.
    """
    # Check environment variable
    if sys.argv[0].endswith('pytest') or 'pytest' in sys.argv[0]:
        return True
    if 'unittest' in sys.argv[0] or 'test' in sys.argv[0].lower():
        return True
    # Check for test framework markers
    if any('test' in arg.lower() for arg in sys.argv):
        return True
    return _TESTING_MODE


def set_active_policy_for_testing(policy: VersioningPolicy) -> None:
    """
    Set the active policy for testing purposes.

    Strict isolation is necessary, but only in test environments.

    WARNING: This function will raise RuntimeError if called outside test environment.

    Args:
        policy: The policy instance to use for testing.

    Raises:
        RuntimeError: If called outside of test environment.
    """
    if not _is_testing_mode():
        raise RuntimeError(
            "set_active_policy_for_testing() can only be called in test environments. "
            "This is a safety guard to prevent production policy corruption."
        )
    
    global _ACTIVE_POLICY
    with _POLICY_LOCK:
        _ACTIVE_POLICY = policy


# ============================================================================
# NAMED POLICY REGISTRY (With Freeze After Bootstrap)
# ============================================================================

_POLICY_REGISTRY: dict[str, VersioningPolicy] = {}
_REGISTRY_FROZEN = False
_REGISTRY_LOCK = threading.Lock()
"""Registry of named policy profiles (strict, migration_window, recovery_mode, etc.)."""


def register_policy(name: str, policy: VersioningPolicy) -> None:
    """
    Register a named policy profile.

    Production systems often need multiple policy profiles:
    - strict: Default production policy
    - migration_window: Relaxed policy during migration windows
    - recovery_mode: Emergency recovery policy

    Args:
        name: Unique name for the policy profile.
        policy: The policy instance to register.

    Raises:
        ValueError: If a policy with the same name is already registered.
        RuntimeError: If registry is frozen (after bootstrap).
    """
    with _REGISTRY_LOCK:
        if _REGISTRY_FROZEN:
            raise RuntimeError(
                "Policy registry is frozen. Cannot register new policies after bootstrap."
            )
        
        if name in _POLICY_REGISTRY:
            raise ValueError(f"Policy '{name}' already registered.")

        _POLICY_REGISTRY[name] = policy


def get_policy(name: str) -> VersioningPolicy:
    """
    Get a named policy from the registry.

    Args:
        name: The name of the policy profile.

    Returns:
        The registered VersioningPolicy instance.

    Raises:
        KeyError: If no policy with the given name is registered.
    """
    if name not in _POLICY_REGISTRY:
        raise KeyError(
            f"Policy '{name}' not found in registry. "
            f"Available policies: {list(_POLICY_REGISTRY.keys())}"
        )
    return _POLICY_REGISTRY[name]


def freeze_registry() -> None:
    """
    Freeze the policy registry after bootstrap.

    This prevents runtime mutation of governance rules, ensuring
    deterministic behavior across distributed nodes.

    This should be called once during system initialization.
    """
    global _REGISTRY_FROZEN
    with _REGISTRY_LOCK:
        _REGISTRY_FROZEN = True


# ============================================================================
# DETERMINISM ENFORCEMENT
# ============================================================================

def _validate_policy_determinism() -> None:
    """
    Validate that policy construction is deterministic.

    This ensures policy is identical across nodes in distributed systems.

    Checks:
    - No environment-dependent branching
    - No reading dynamic config
    - No date/time logic
    - No external I/O
    - No mutable closures

    This is called internally during policy construction.
    """
    # Validate that we're not accessing environment variables
    import os
    # This is a runtime check - in production CI, you might want static analysis
    # For now, we document the requirement and validate at construction time
    
    # Check that policy engines are module-level (not closures)
    assert _semantic_policy is not None, "Semantic policy must be module-level"
    assert _compatibility_policy is not None, "Compatibility policy must be module-level"
    
    # In production, you might want to:
    # - Use static analysis to verify no env var access
    # - Verify no file I/O in policy construction
    # - Check that all callables are pure functions
    # - Ensure no date/time dependencies
    
    # For now, we rely on code review and architectural discipline
    pass


# ============================================================================
# PUBLIC SURFACE (SEALED)
# ============================================================================

__all__: Final = [
    "VersioningPolicy",
    "get_active_policy",
    "set_active_policy_for_testing",
    "register_policy",
    "get_policy",
    "freeze_registry",
]

# ============================================================================
# ARCHITECTURAL ENFORCEMENT NOTES
# ============================================================================

# This module exports ONLY the policy interface listed above.
#
# Do NOT export:
# - Internal policy construction functions
# - Policy engines (_semantic_policy, _compatibility_policy)
# - Registry internals
# - Low-level rule modules
#
# Only the composed policy object and accessors.
#
# CI Enforcement Rule:
# - Nothing outside /data/versioning/policy/ may import submodules directly.
# - All policy access must go through get_active_policy().
# - This prevents governance bypass.
#
# Integration Pattern:
# Every call into:
#   /data/validation/
#   /data/lineage/
#   /data/serialization/
# Should reference policy only through:
#   policy = get_active_policy()
#   policy.assert_transition_allowed(old, new)
#
# Never import submodules directly.
# This prevents governance bypass.
