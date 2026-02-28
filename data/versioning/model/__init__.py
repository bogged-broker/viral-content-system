"""
/data/versioning/model/__init__.py

Pure semantic versioning domain primitives.

This module defines immutable, deterministic version modeling objects.

NO migration logic.
NO compatibility enforcement logic.
NO governance logic.
NO IO.

If you need operational behavior, import from /data/versioning/.

---

ARCHITECTURAL ROLE

This file is the mathematical foundation boundary for schema evolution.

Think of it as:

/data/versioning/
  ├── model/          ← Pure semantic primitives (THIS LAYER)
  │   ├── version.py
  │   ├── version_range.py
  │   ├── version_graph.py
  │   ├── semantic_policy.py
  │   └── __init__.py  ← Pure semantic export boundary
  ├── evolution/      ← Migration planning & execution
  ├── compatibility/  ← Compatibility enforcement
  ├── governance/     ← Lock checks & policy enforcement
  └── runtime/        ← Runtime version resolution

Nothing in model/ may import:
- evolution/
- compatibility/
- governance/
- runtime/

If that ever happens, the architecture is compromised.

---

DESIGN PRINCIPLES

1. Deterministic: All operations are side-effect-free and deterministic.
2. Immutable: All objects are immutable after construction.
3. Fully Typed: Complete type annotations for all public APIs.
4. Hash Stable: Objects are hashable with stable hash values.
5. Comparable: Version objects support total ordering.
6. Serialization-Safe: All objects can be safely serialized/deserialized.

---

WHAT LIVES IN model/

1. SchemaVersion
   - Comparable with total ordering
   - Pre-release handling
   - Channel handling (optional)
   - Immutable and hashable
   - Deterministic string representation

2. VersionRange
   - Inclusive/exclusive bounds
   - Intersection logic
   - Membership test
   - Empty range detection

3. VersionGraph
   - Pure DAG model
   - No migration functions
   - No state mutation
   - Topological sort
   - Cycle detection

4. SemanticVersionPolicy
   - Defines category rules:
     * PATCH: non-breaking internal fix
     * MINOR: backward compatible
     * MAJOR: breaking
   - Does NOT enforce them operationally
   - Enforcement belongs to compatibility/

---

WHAT MUST NEVER ENTER THIS LAYER

🚫 Migration execution
🚫 Compatibility enforcement
🚫 Governance lock checks
🚫 Rollout phase logic
🚫 Database inspection
🚫 Environment flags
🚫 Feature flags

If any of that appears, the system is decaying.

---

PRODUCTION MATURITY SIGNALS

A production-grade model/__init__.py:

✓ Has explicit __all__
✓ Has strong documentation header
✓ Has zero accidental re-exports
✓ Has no runtime global state
✓ Has no IO
✓ Has no conditional imports
✓ Has no environment dependencies
✓ Is stable across years

If this file stays stable for 3+ years, your architecture is healthy.
"""

from __future__ import annotations

from typing import Final

# ============================================================================
# PURE SEMANTIC MODEL IMPORTS
# ============================================================================

# Import only the stable semantic primitives from model submodules.
# These are the ONLY exports allowed from this boundary.

from .version import SchemaVersion
from .version_range import VersionRange
from .version_graph import VersionGraph
from .semantic_policy import SemanticVersionPolicy

# ============================================================================
# PUBLIC SURFACE (SEALED)
# ============================================================================

__all__: Final = [
    "SchemaVersion",
    "VersionRange",
    "VersionGraph",
    "SemanticVersionPolicy",
]

# ============================================================================
# ARCHITECTURAL ENFORCEMENT NOTES
# ============================================================================

# This module exports ONLY the four semantic primitives listed above.
#
# Do NOT export:
# - Parsers
# - Internal helper classes
# - Graph builders
# - Diff utilities
# - Transient validators
#
# Only stable semantic objects.
#
# CI Enforcement Rule:
# - Nothing outside /data/versioning/model/ may import internals of
#   /data/versioning/model/.
# - Nothing inside /data/versioning/model/ may import anything outside
#   this folder.
#
# This keeps the mathematical core sealed and provable.
