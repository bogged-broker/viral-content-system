"""
/infra/__init__.py

Infrastructure Authority Boundary (Master Seal)

WHAT THIS FILE ACTUALLY IS:
    The top-level jurisdiction boundary for all infrastructure capabilities.
    
    Answers: "What infrastructure exists — and what can the application depend on?"
    
    This is the master authority seal.

WHAT THIS FILE IS NOT:
    ❌ Not application code
    ❌ Not business logic
    ❌ Not runtime initialization
    ❌ Not configuration management
    
    Importing this file must have zero side effects.

DESIGN PRINCIPLE:
    > Infrastructure is explicit, blessed capability — not discovered services.
    
    If something is exported here, it is:
    - Stable
    - Tested
    - Contractually sound
    - Architecturally intentional

MASTER CONTRACT:
    This file defines the complete infrastructure surface.
    Every capability is deliberate.
    Every export is a promise.
    
    Changing this file is a major infrastructure change.
"""

# ============================================================================
# INFRASTRUCTURE SUBSYSTEMS
# ============================================================================

# Persistence layer - state durability & evolution
from infra import persistence

# Recovery layer - audit, replay, verification
from infra import recovery


# ============================================================================
# PUBLIC SURFACE (SEALED)
# ============================================================================

__all__ = [
    # Infrastructure subsystems
    "persistence",
    "recovery",
]


# ============================================================================
# VERSION & METADATA
# ============================================================================

__version__ = "1.0.0"
__author__ = "Infrastructure Team"
__status__ = "Production"


# ============================================================================
# ARCHITECTURAL INVARIANTS
# ============================================================================

# Infrastructure layer enforces:
# - Explicit subsystem boundaries
# - No cross-subsystem leakage
# - No implicit dependencies
# - No runtime magic
# - No side effects on import
#
# Infrastructure is authority.
# Everything else is application.