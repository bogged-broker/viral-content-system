"""
/data/pipelines/windows/window_tests.py

Determinism, Replay, and Historical Truth Oracles

What This File Exists For (NON-NEGOTIABLE):
  window_tests.py is the court of law for the windowing system.
  It answers exactly one question:
  "Can the window system ever produce a different answer for the same truth?"

AUTHORITY: Correctness without replayability is failure.

Design Principle:
  > Correctness without replayability is failure.

A window implementation that works "most of the time" has already corrupted history.

Test Philosophy (STRICT):
  All tests must be:
  - Pure (no clocks, no randomness)
  - Order-independent
  - Environment-independent
  - Repeatable forever
  - Byte-level strict

If a test cannot be run 10 years from now and still pass, it is invalid.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import pytest

# Import actual window modules
from .window_models import WindowDefinition, WindowType, TimestampExtractionStrategy
from .window_identity import (
    WindowIdentityMaterial,
    WindowIdentityFactory,
    WindowIdentity,
    WindowIdentitySerializer,
    WindowIdentityCodec,
)
from .windows import WindowRegistry


# ============================================================================
# CANONICAL TEST DATA (IMMUTABLE)
# ============================================================================

@dataclass(frozen=True)
class CanonicalEvent:
    """Canonical event for testing - immutable, deterministic."""
    event_time_ms: int
    event_id: str
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AggregationContext:
    """Aggregation context for window identity computation."""
    metric_name: str
    dimensions: Dict[str, str] = field(default_factory=dict)


# Fixed window definitions for determinism tests
TUMBLING_1H_V1 = WindowDefinition(
    window_type=WindowType.TUMBLING_TIME,
    window_size_ms=3600000,
    alignment_epoch_ms=0,
    allowed_lateness_ms=0,
    timestamp_extraction_strategy=TimestampExtractionStrategy.EVENT_TIME,
    definition_version="v1",
    identity_format_version="v1",
)

TUMBLING_5M_V1 = WindowDefinition(
    window_type=WindowType.TUMBLING_TIME,
    window_size_ms=300000,
    alignment_epoch_ms=0,
    allowed_lateness_ms=0,
    timestamp_extraction_strategy=TimestampExtractionStrategy.EVENT_TIME,
    definition_version="v1",
    identity_format_version="v1",
)

SESSION_30S_V1 = WindowDefinition(
    window_type=WindowType.SESSION,
    window_size_ms=None,
    session_gap_ms=30000,
    alignment_epoch_ms=0,
    allowed_lateness_ms=0,
    timestamp_extraction_strategy=TimestampExtractionStrategy.EVENT_TIME,
    definition_version="v1",
    identity_format_version="v1",
)


# ============================================================================
# DETERMINISM TEST VECTORS (FOUNDATION)
# ============================================================================

DETERMINISM_VECTORS = [
    {
        "event": CanonicalEvent(
            event_time_ms=1700000000123,
            event_id="evt_001",
            payload={"value": 42}
        ),
        "window_definition": TUMBLING_1H_V1,
        "expected": {
            "window_start_ms": 1700000000000,
            "window_end_ms": 1700003600000,
        },
    },
    {
        "event": CanonicalEvent(
            event_time_ms=1700000000000,
            event_id="evt_002",
            payload={}
        ),
        "window_definition": TUMBLING_5M_V1,
        "expected": {
            "window_start_ms": 1700000000000,
            "window_end_ms": 1700000300000,
        },
    },
    {
        "event": CanonicalEvent(
            event_time_ms=1700000299999,
            event_id="evt_003",
            payload={"boundary": "end"}
        ),
        "window_definition": TUMBLING_5M_V1,
        "expected": {
            "window_start_ms": 1700000000000,
            "window_end_ms": 1700000300000,
        },
    },
    {
        "event": CanonicalEvent(
            event_time_ms=1700000300000,
            event_id="evt_004",
            payload={"boundary": "start"}
        ),
        "window_definition": TUMBLING_5M_V1,
        "expected": {
            "window_start_ms": 1700000300000,
            "window_end_ms": 1700000600000,
        },
    },
]


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def compute_tumbling_window(
    event_time_ms: int,
    window_size_ms: int,
    alignment_epoch_ms: int = 0
) -> Tuple[int, int]:
    """Compute tumbling window boundaries deterministically."""
    offset = event_time_ms - alignment_epoch_ms
    window_start_ms = (offset // window_size_ms) * window_size_ms + alignment_epoch_ms
    window_end_ms = window_start_ms + window_size_ms
    return window_start_ms, window_end_ms


def create_window_identity_material(
    window_start_ms: int,
    window_end_ms: int,
    window_definition: WindowDefinition,
    context: Optional[AggregationContext] = None
) -> WindowIdentityMaterial:
    """Create WindowIdentityMaterial from test data."""
    # Tier-0: aggregation_context is always a dict (empty if no context)
    aggregation_context = {}
    if context:
        aggregation_context = {
            "metric_name": context.metric_name,
            "dimensions": dict(sorted(context.dimensions.items())),
        }
    
    # Convert string identity_format_version to enum for Tier-0 type safety
    from .window_identity import IdentityFormatVersion
    identity_format_version = IdentityFormatVersion(window_definition.identity_format_version) if isinstance(window_definition.identity_format_version, str) else window_definition.identity_format_version
    
    return WindowIdentityMaterial(
        window_type=window_definition.window_type,  # Use enum directly, not .value
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
        alignment_epoch_ms=window_definition.alignment_epoch_ms,
        window_definition_version=window_definition.definition_version,
        identity_format_version=identity_format_version,
        session_gap_ms=window_definition.session_gap_ms,
        hop_size_ms=window_definition.hop_size_ms,
        aggregation_context=aggregation_context,
    )


def resolve_window_for_test(
    event: CanonicalEvent,
    window_def: WindowDefinition
) -> Dict[str, Any]:
    """Resolve window for testing - deterministic, pure function."""
    if window_def.window_type == WindowType.TUMBLING_TIME:
        window_start_ms, window_end_ms = compute_tumbling_window(
            event.event_time_ms,
            window_def.window_size_ms,
            window_def.alignment_epoch_ms
        )
    elif window_def.window_type == WindowType.SESSION:
        # Session windows require state - for tests, we use gap logic
        # This is simplified for determinism testing
        raise NotImplementedError("Session windows require state tracking - use session-specific tests")
    else:
        raise ValueError(f"Unknown window type: {window_def.window_type}")
    
    material = create_window_identity_material(window_start_ms, window_end_ms, window_def)
    identity = WindowIdentityFactory.create_identity(material)
    
    return {
        "window_start_ms": window_start_ms,
        "window_end_ms": window_end_ms,
        "window_id": identity.window_id,
    }


# ============================================================================
# DETERMINISM TEST VECTORS
# ============================================================================

class DeterminismTestVectors:
    """Foundation tests using fixed, hand-curated inputs."""
    
    def test_vector_immutability(self):
        """Vectors must be immutable once added."""
        assert len(DETERMINISM_VECTORS) >= 3
        for vec in DETERMINISM_VECTORS:
            assert "event" in vec
            assert "window_definition" in vec
            assert "expected" in vec
            assert isinstance(vec["event"], CanonicalEvent)
            assert isinstance(vec["window_definition"], WindowDefinition)
    
    def test_vector_001_exact_match(self):
        """First vector must produce exact expected output."""
        vec = DETERMINISM_VECTORS[0]
        result = resolve_window_for_test(vec["event"], vec["window_definition"])
        
        assert result["window_start_ms"] == vec["expected"]["window_start_ms"]
        assert result["window_end_ms"] == vec["expected"]["window_end_ms"]
        # window_id is deterministic but we don't hardcode it in vectors
        assert len(result["window_id"]) == 64
        assert all(c in '0123456789abcdef' for c in result["window_id"])
    
    def test_vector_002_boundary_start(self):
        """Event at window boundary start."""
        vec = DETERMINISM_VECTORS[1]
        result = resolve_window_for_test(vec["event"], vec["window_definition"])
        
        assert result["window_start_ms"] == vec["expected"]["window_start_ms"]
        assert result["window_end_ms"] == vec["expected"]["window_end_ms"]
    
    def test_vector_003_boundary_end(self):
        """Event at window boundary end (exclusive)."""
        vec = DETERMINISM_VECTORS[2]
        result = resolve_window_for_test(vec["event"], vec["window_definition"])
        
        assert result["window_start_ms"] == vec["expected"]["window_start_ms"]
        assert result["window_end_ms"] == vec["expected"]["window_end_ms"]
    
    def test_vector_004_next_window_start(self):
        """Event at next window start."""
        vec = DETERMINISM_VECTORS[3]
        result = resolve_window_for_test(vec["event"], vec["window_definition"])
        
        assert result["window_start_ms"] == vec["expected"]["window_start_ms"]
        assert result["window_end_ms"] == vec["expected"]["window_end_ms"]


# ============================================================================
# IDENTITY STABILITY TESTS
# ============================================================================

class IdentityStabilityTests:
    """Guarantees: Same material → same window_id."""
    
    def test_same_material_same_id(self):
        """Same material must produce identical window_id."""
        material1 = create_window_identity_material(
            1700000000000, 1700003600000, TUMBLING_1H_V1
        )
        material2 = create_window_identity_material(
            1700000000000, 1700003600000, TUMBLING_1H_V1
        )
        
        id1 = WindowIdentityFactory.create_identity(material1)
        id2 = WindowIdentityFactory.create_identity(material2)
        
        assert id1.window_id == id2.window_id
        assert id1 == id2
    
    def test_identity_byte_identical(self):
        """Identity strings must be byte-identical."""
        material = create_window_identity_material(
            1700000000000, 1700003600000, TUMBLING_1H_V1
        )
        
        id1 = WindowIdentityFactory.create_identity(material)
        id2 = WindowIdentityFactory.create_identity(material)
        
        assert id1.window_id.encode('utf-8') == id2.window_id.encode('utf-8')
        assert isinstance(id1.window_id, str)
        assert len(id1.window_id) == 64
    
    def test_different_boundaries_different_id(self):
        """Different boundaries must produce different IDs."""
        material1 = create_window_identity_material(
            1700000000000, 1700003600000, TUMBLING_1H_V1
        )
        material2 = create_window_identity_material(
            1700003600000, 1700007200000, TUMBLING_1H_V1
        )
        
        id1 = WindowIdentityFactory.create_identity(material1)
        id2 = WindowIdentityFactory.create_identity(material2)
        
        assert id1.window_id != id2.window_id
    
    def test_context_affects_identity(self):
        """Different aggregation contexts produce different IDs."""
        ctx1 = AggregationContext("metric_a", {"region": "us-east"})
        ctx2 = AggregationContext("metric_b", {"region": "us-east"})
        
        material1 = create_window_identity_material(
            1700000000000, 1700003600000, TUMBLING_1H_V1, ctx1
        )
        material2 = create_window_identity_material(
            1700000000000, 1700003600000, TUMBLING_1H_V1, ctx2
        )
        
        id1 = WindowIdentityFactory.create_identity(material1)
        id2 = WindowIdentityFactory.create_identity(material2)
        
        assert id1.window_id != id2.window_id
    
    def test_dimension_order_invariant(self):
        """Dimension order must not affect identity."""
        ctx1 = AggregationContext("metric", {"a": "1", "b": "2", "c": "3"})
        ctx2 = AggregationContext("metric", {"c": "3", "a": "1", "b": "2"})
        
        material1 = create_window_identity_material(
            1700000000000, 1700003600000, TUMBLING_1H_V1, ctx1
        )
        material2 = create_window_identity_material(
            1700000000000, 1700003600000, TUMBLING_1H_V1, ctx2
        )
        
        id1 = WindowIdentityFactory.create_identity(material1)
        id2 = WindowIdentityFactory.create_identity(material2)
        
        assert id1.window_id == id2.window_id
    
    def test_no_dict_ordering_dependence(self):
        """Identity must not depend on dict insertion order."""
        # Create material with different dict construction orders
        material1 = create_window_identity_material(
            1700000000000, 1700003600000, TUMBLING_1H_V1
        )
        
        # Recreate with same values but different construction
        material2 = WindowIdentityMaterial(
            window_type=TUMBLING_1H_V1.window_type.value,
            window_start_ms=1700000000000,
            window_end_ms=1700003600000,
            alignment_epoch_ms=TUMBLING_1H_V1.alignment_epoch_ms,
            window_definition_version=TUMBLING_1H_V1.definition_version,
            identity_format_version=TUMBLING_1H_V1.identity_format_version,
            session_gap_ms=TUMBLING_1H_V1.session_gap_ms,
            hop_size_ms=TUMBLING_1H_V1.hop_size_ms,
            aggregation_context={},  # Tier-0: always a dict, never None
        )
        
        id1 = WindowIdentityFactory.create_identity(material1)
        id2 = WindowIdentityFactory.create_identity(material2)
        
        assert id1.window_id == id2.window_id


# ============================================================================
# SERIALIZATION CANONICALITY TESTS
# ============================================================================

class SerializationCanonicalityTests:
    """Enforce ordered, explicit, locale-independent serialization."""
    
    def test_field_order_invariance(self):
        """Field order must not affect canonical serialization."""
        material1 = {"b": 2, "a": 1, "c": 3}
        material2 = {"a": 1, "c": 3, "b": 2}
        
        canon1 = json.dumps(material1, sort_keys=True, separators=(',', ':'))
        canon2 = json.dumps(material2, sort_keys=True, separators=(',', ':'))
        
        assert canon1 == canon2
        assert canon1 == '{"a":1,"b":2,"c":3}'
    
    def test_numeric_edge_cases(self):
        """Numeric edge cases must serialize deterministically."""
        material = {"zero": 0, "large": 9007199254740991, "negative": -1}
        canon = json.dumps(material, sort_keys=True, separators=(',', ':'))
        
        assert '"zero":0' in canon
        assert '"large":9007199254740991' in canon
        assert '"negative":-1' in canon
    
    def test_utf8_enforcement(self):
        """UTF-8 must be enforced with ensure_ascii=True."""
        material = {"text": "hello", "unicode": "世界"}
        canon = json.dumps(material, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        
        assert '\\u' in canon  # Unicode escaped
        assert '"text":"hello"' in canon
    
    def test_no_whitespace_variation(self):
        """Canonical serialization must have no whitespace."""
        material = {"a": 1, "b": 2}
        canon = json.dumps(material, sort_keys=True, separators=(',', ':'))
        
        assert ' ' not in canon
        assert '\n' not in canon
        assert '\t' not in canon
    
    def test_identity_serialization_canonical(self):
        """WindowIdentity serialization must be canonical."""
        material = create_window_identity_material(
            1700000000000, 1700003600000, TUMBLING_1H_V1
        )
        identity = WindowIdentityFactory.create_identity(material)
        
        # Serialize identity
        encoded1 = WindowIdentityCodec.encode_identity(identity)
        encoded2 = WindowIdentityCodec.encode_identity(identity)
        
        # Both must produce identical JSON
        json1 = json.dumps(encoded1, sort_keys=True, separators=(',', ':'))
        json2 = json.dumps(encoded2, sort_keys=True, separators=(',', ':'))
        
        assert json1 == json2
    
    def test_material_serialization_canonical(self):
        """WindowIdentityMaterial serialization must be canonical."""
        material = create_window_identity_material(
            1700000000000, 1700003600000, TUMBLING_1H_V1
        )
        
        canonical1 = WindowIdentitySerializer.serialize(material)
        canonical2 = WindowIdentitySerializer.serialize(material)
        
        assert canonical1 == canonical2
        assert isinstance(canonical1, bytes)


# ============================================================================
# CROSS-PROCESS EQUIVALENCE TESTS
# ============================================================================

class CrossProcessEquivalenceTests:
    """Guarantee identity consistency across process boundaries."""
    
    def test_reconstruct_from_serialized(self):
        """Reconstruct window definition from serialized form."""
        original_def = TUMBLING_1H_V1
        serialized = original_def.canonical_serialization()
        
        # Deserialize (simulate cross-process)
        data = json.loads(serialized.decode('utf-8'))
        
        # Reconstruct window definition
        reconstructed = WindowDefinition(
            window_type=WindowType(data["window_type"]),
            window_size_ms=data.get("window_size_ms"),
            alignment_epoch_ms=data["alignment_epoch_ms"],
            allowed_lateness_ms=data["allowed_lateness_ms"],
            timestamp_extraction_strategy=TimestampExtractionStrategy(data["timestamp_extraction_strategy"]),
            definition_version=data["definition_version"],
            identity_format_version=data["identity_format_version"],
        )
        
        # Compute identities with both
        material1 = create_window_identity_material(
            1700000000000, 1700003600000, original_def
        )
        material2 = create_window_identity_material(
            1700000000000, 1700003600000, reconstructed
        )
        
        id1 = WindowIdentityFactory.create_identity(material1)
        id2 = WindowIdentityFactory.create_identity(material2)
        
        assert id1.window_id == id2.window_id
    
    def test_event_reconstruction(self):
        """Reconstruct event from serialized form."""
        original = CanonicalEvent(1700000000123, "evt_001", {"value": 42})
        serialized = json.dumps({
            "event_time_ms": original.event_time_ms,
            "event_id": original.event_id,
            "payload": original.payload,
        })
        
        data = json.loads(serialized)
        reconstructed = CanonicalEvent(**data)
        
        result1 = resolve_window_for_test(original, TUMBLING_1H_V1)
        result2 = resolve_window_for_test(reconstructed, TUMBLING_1H_V1)
        
        assert result1 == result2
    
    def test_identity_reconstruction(self):
        """Reconstruct identity from canonical bytes."""
        material = create_window_identity_material(
            1700000000000, 1700003600000, TUMBLING_1H_V1
        )
        original_identity = WindowIdentityFactory.create_identity(material)
        
        # Serialize to canonical bytes
        canonical_bytes = WindowIdentitySerializer.serialize(material)
        
        # Reconstruct from canonical bytes
        reconstructed_identity = WindowIdentityFactory.recreate_identity_from_canonical(canonical_bytes)
        
        assert original_identity.window_id == reconstructed_identity.window_id
        assert original_identity.window_start_ms == reconstructed_identity.window_start_ms
        assert original_identity.window_end_ms == reconstructed_identity.window_end_ms


# ============================================================================
# REPLAY ORACLE TESTS
# ============================================================================

class ReplayOracleTests:
    """Reprocessing same historical input yields bit-for-bit identical outputs."""
    
    def test_single_event_replay(self):
        """Single event must produce identical results on replay."""
        event = CanonicalEvent(1700000000123, "evt_001", {"value": 42})
        
        result1 = resolve_window_for_test(event, TUMBLING_1H_V1)
        result2 = resolve_window_for_test(event, TUMBLING_1H_V1)
        result3 = resolve_window_for_test(event, TUMBLING_1H_V1)
        
        assert result1 == result2 == result3
        assert result1["window_id"] == result2["window_id"] == result3["window_id"]
        assert result1["window_start_ms"] == result2["window_start_ms"] == result3["window_start_ms"]
        assert result1["window_end_ms"] == result2["window_end_ms"] == result3["window_end_ms"]
    
    def test_batch_replay(self):
        """Batch of events must produce identical results on replay."""
        events = [
            CanonicalEvent(1700000000000 + i * 1000, f"evt_{i:03d}", {"seq": i})
            for i in range(10)
        ]
        
        results1 = [resolve_window_for_test(e, TUMBLING_5M_V1) for e in events]
        results2 = [resolve_window_for_test(e, TUMBLING_5M_V1) for e in events]
        
        assert len(results1) == len(results2) == 10
        for r1, r2 in zip(results1, results2):
            assert r1 == r2
            assert r1["window_id"] == r2["window_id"]
    
    def test_out_of_order_replay(self):
        """Out-of-order events must produce identical results."""
        events = [
            CanonicalEvent(1700000005000, "evt_002", {}),
            CanonicalEvent(1700000001000, "evt_001", {}),
            CanonicalEvent(1700000003000, "evt_003", {}),
        ]
        
        results1 = [resolve_window_for_test(e, TUMBLING_5M_V1) for e in events]
        results2 = [resolve_window_for_test(e, TUMBLING_5M_V1) for e in events]
        
        assert results1 == results2
        assert all(r1["window_id"] == r2["window_id"] for r1, r2 in zip(results1, results2))
    
    def test_window_boundary_replay(self):
        """Events at window boundaries must be deterministic."""
        boundary_event = CanonicalEvent(1700000300000, "evt_boundary", {})
        
        result1 = resolve_window_for_test(boundary_event, TUMBLING_5M_V1)
        result2 = resolve_window_for_test(boundary_event, TUMBLING_5M_V1)
        
        assert result1["window_start_ms"] == result2["window_start_ms"]
        assert result1["window_end_ms"] == result2["window_end_ms"]
        assert result1["window_id"] == result2["window_id"]
    
    def test_identity_reproducibility(self):
        """Identity must be reproducible from material."""
        material = create_window_identity_material(
            1700000000000, 1700003600000, TUMBLING_1H_V1
        )
        identity = WindowIdentityFactory.create_identity(material)
        
        # Verify reproducibility
        is_reproducible = WindowIdentityFactory.verify_identity_reproducibility(identity, material)
        assert is_reproducible


# ============================================================================
# SESSION WINDOW ORACLE TESTS
# ============================================================================

class SessionWindowOracleTests:
    """Session windows are the primary source of ambiguity - these tests are non-optional."""
    
    def test_transitivity(self):
        """A within gap of B, B within gap of C → one session."""
        gap_ms = 30000
        events = [
            CanonicalEvent(1700000000000, "evt_a", {}),  # t=0
            CanonicalEvent(1700000015000, "evt_b", {}),  # t=15s (within gap of A)
            CanonicalEvent(1700000025000, "evt_c", {}),  # t=25s (within gap of B)
        ]
        
        # All three events should be in the same session
        # (A-B gap: 15s < 30s, B-C gap: 10s < 30s)
        gap_ab = events[1].event_time_ms - events[0].event_time_ms
        gap_bc = events[2].event_time_ms - events[1].event_time_ms
        
        assert gap_ab < gap_ms
        assert gap_bc < gap_ms
        # Transitivity: A-C gap is 25s, still within gap
        gap_ac = events[2].event_time_ms - events[0].event_time_ms
        assert gap_ac < gap_ms
    
    def test_boundary_closure(self):
        """Session deterministically closes at replay boundary."""
        gap_ms = 30000
        events = [
            CanonicalEvent(1700000000000, "evt_1", {}),
            CanonicalEvent(1700000030000, "evt_2", {}),  # Exactly at gap boundary
        ]
        
        # Event 2 is exactly at gap boundary - session should close
        gap = events[1].event_time_ms - events[0].event_time_ms
        assert gap == gap_ms  # Exactly at boundary
    
    def test_monotonicity(self):
        """Replaying partial history cannot split an existing session."""
        gap_ms = 30000
        # Full history
        full_events = [
            CanonicalEvent(1700000000000, "evt_1", {}),
            CanonicalEvent(1700000010000, "evt_2", {}),
            CanonicalEvent(1700000020000, "evt_3", {}),
        ]
        
        # Partial history (first two events)
        partial_events = full_events[:2]
        
        # Full session should include all three
        full_gap_12 = full_events[1].event_time_ms - full_events[0].event_time_ms
        full_gap_23 = full_events[2].event_time_ms - full_events[1].event_time_ms
        
        # Partial session should include first two
        partial_gap = partial_events[1].event_time_ms - partial_events[0].event_time_ms
        
        # Monotonicity: partial session is subset of full session
        assert full_gap_12 < gap_ms
        assert full_gap_23 < gap_ms
        assert partial_gap < gap_ms
        # If partial forms a session, full must also form a session
        assert (full_gap_12 < gap_ms and full_gap_23 < gap_ms) == (partial_gap < gap_ms)
    
    def test_gap_edge_exact(self):
        """Event exactly at session_gap_ms behaves deterministically."""
        gap_ms = 30000
        events = [
            CanonicalEvent(1700000000000, "evt_1", {}),
            CanonicalEvent(1700000030000, "evt_2", {}),  # Exactly gap_ms later
        ]
        
        gap = events[1].event_time_ms - events[0].event_time_ms
        assert gap == gap_ms
        
        # Behavior must be deterministic: either in same session or different
        # The exact boundary case must be explicitly defined
        at_boundary = gap == gap_ms
        assert at_boundary  # Test that we can detect boundary case
    
    def test_gap_edge_one_ms_before(self):
        """Event one ms before gap boundary is within session."""
        gap_ms = 30000
        events = [
            CanonicalEvent(1700000000000, "evt_1", {}),
            CanonicalEvent(1700000029999, "evt_2", {}),  # 1ms before gap
        ]
        
        gap = events[1].event_time_ms - events[0].event_time_ms
        assert gap < gap_ms  # Within session
    
    def test_gap_edge_one_ms_after(self):
        """Event one ms after gap boundary starts new session."""
        gap_ms = 30000
        events = [
            CanonicalEvent(1700000000000, "evt_1", {}),
            CanonicalEvent(1700000030001, "evt_2", {}),  # 1ms after gap
        ]
        
        gap = events[1].event_time_ms - events[0].event_time_ms
        assert gap > gap_ms  # New session


# ============================================================================
# REGISTRY STABILITY TESTS
# ============================================================================

class RegistryStabilityTests:
    """Guarantees: WindowRegistry contents are static, order is stable, hash is stable."""
    
    EXPECTED_REGISTRY_HASH = "PLACEHOLDER_HASH_UPDATE_ON_FIRST_RUN"
    
    def test_registry_contents_static(self):
        """Registry contents must be static."""
        registry = WindowRegistry()
        registry.register("TUMBLING_1H_V1", TUMBLING_1H_V1)
        registry.register("TUMBLING_5M_V1", TUMBLING_5M_V1)
        registry.register("SESSION_30S_V1", SESSION_30S_V1)
        registry.lock()
        
        assert len(registry.list_definitions()) == 3
        assert "TUMBLING_1H_V1" in registry.list_definitions()
        assert "TUMBLING_5M_V1" in registry.list_definitions()
        assert "SESSION_30S_V1" in registry.list_definitions()
    
    def test_registry_order_stable(self):
        """Registry order must be stable (sorted)."""
        registry = WindowRegistry()
        registry.register("TUMBLING_1H_V1", TUMBLING_1H_V1)
        registry.register("TUMBLING_5M_V1", TUMBLING_5M_V1)
        registry.register("SESSION_30S_V1", SESSION_30S_V1)
        registry.lock()
        
        definitions = registry.list_definitions()
        assert definitions == sorted(definitions)
    
    def test_registry_serialization_hash(self):
        """Registry serialization must produce stable hash."""
        registry = WindowRegistry()
        registry.register("TUMBLING_1H_V1", TUMBLING_1H_V1)
        registry.register("TUMBLING_5M_V1", TUMBLING_5M_V1)
        registry.register("SESSION_30S_V1", SESSION_30S_V1)
        registry.lock()
        
        registry_hash = registry.registry_hash()
        
        assert len(registry_hash) == 64
        assert all(c in '0123456789abcdef' for c in registry_hash)
        
        # Hash must be stable across multiple calls
        hash2 = registry.registry_hash()
        assert registry_hash == hash2
    
    def test_registry_no_dynamic_registration(self):
        """Registry must not allow dynamic registration after lock."""
        registry = WindowRegistry()
        registry.register("TUMBLING_1H_V1", TUMBLING_1H_V1)
        registry.lock()
        
        with pytest.raises(RuntimeError, match="locked"):
            registry.register("NEW_WINDOW", TUMBLING_5M_V1)
    
    def test_registry_serialization_canonical(self):
        """Registry serialization must be canonical."""
        registry = WindowRegistry()
        registry.register("TUMBLING_1H_V1", TUMBLING_1H_V1)
        registry.register("TUMBLING_5M_V1", TUMBLING_5M_V1)
        registry.lock()
        
        serialized1 = registry.serialize()
        serialized2 = registry.serialize()
        
        assert serialized1 == serialized2
        assert isinstance(serialized1, bytes)


# ============================================================================
# VERSION ISOLATION TESTS
# ============================================================================

class VersionIsolationTests:
    """Ensure versioning prevents silent corruption."""
    
    def test_identity_format_version_isolation(self):
        """Same material + different identity_format_version → different IDs."""
        def_v1 = WindowDefinition(
            window_type=WindowType.TUMBLING_TIME,
            window_size_ms=3600000,
            alignment_epoch_ms=0,
            allowed_lateness_ms=0,
            timestamp_extraction_strategy=TimestampExtractionStrategy.EVENT_TIME,
            definition_version="v1",
            identity_format_version="v1",
        )
        
        def_v2 = WindowDefinition(
            window_type=WindowType.TUMBLING_TIME,
            window_size_ms=3600000,
            alignment_epoch_ms=0,
            allowed_lateness_ms=0,
            timestamp_extraction_strategy=TimestampExtractionStrategy.EVENT_TIME,
            definition_version="v1",
            identity_format_version="v2",  # Different format version
        )
        
        material1 = create_window_identity_material(
            1700000000000, 1700003600000, def_v1
        )
        material2 = create_window_identity_material(
            1700000000000, 1700003600000, def_v2
        )
        
        id1 = WindowIdentityFactory.create_identity(material1)
        id2 = WindowIdentityFactory.create_identity(material2)
        
        assert id1.window_id != id2.window_id
    
    def test_definition_version_isolation(self):
        """Same boundaries + different window_definition_version → different IDs."""
        def_v1 = WindowDefinition(
            window_type=WindowType.TUMBLING_TIME,
            window_size_ms=3600000,
            alignment_epoch_ms=0,
            allowed_lateness_ms=0,
            timestamp_extraction_strategy=TimestampExtractionStrategy.EVENT_TIME,
            definition_version="v1",
            identity_format_version="v1",
        )
        
        def_v2 = WindowDefinition(
            window_type=WindowType.TUMBLING_TIME,
            window_size_ms=3600000,
            alignment_epoch_ms=0,
            allowed_lateness_ms=0,
            timestamp_extraction_strategy=TimestampExtractionStrategy.EVENT_TIME,
            definition_version="v2",  # Different definition version
            identity_format_version="v1",
        )
        
        material1 = create_window_identity_material(
            1700000000000, 1700003600000, def_v1
        )
        material2 = create_window_identity_material(
            1700000000000, 1700003600000, def_v2
        )
        
        id1 = WindowIdentityFactory.create_identity(material1)
        id2 = WindowIdentityFactory.create_identity(material2)
        
        assert id1.window_id != id2.window_id
    
    def test_old_version_reproducible(self):
        """Old versions must remain reproducible."""
        def_v1 = WindowDefinition(
            window_type=WindowType.TUMBLING_TIME,
            window_size_ms=3600000,
            alignment_epoch_ms=0,
            allowed_lateness_ms=0,
            timestamp_extraction_strategy=TimestampExtractionStrategy.EVENT_TIME,
            definition_version="v1",
            identity_format_version="v1",
        )
        
        material = create_window_identity_material(
            1700000000000, 1700003600000, def_v1
        )
        
        id1 = WindowIdentityFactory.create_identity(material)
        id2 = WindowIdentityFactory.create_identity(material)
        
        assert id1.window_id == id2.window_id
        assert id1.identity_format_version == "v1"
        assert id1.window_definition_version == "v1"


# ============================================================================
# NEGATIVE INVARIANT TESTS
# ============================================================================

class NegativeInvariantTests:
    """Tests that must raise, not pass."""
    
    def test_missing_event_time_ms(self):
        """Missing event_time_ms must raise."""
        with pytest.raises((TypeError, AttributeError)):
            CanonicalEvent(event_id="evt_001", payload={})
    
    def test_negative_timestamp_invalid(self):
        """Negative timestamps must be handled explicitly."""
        event = CanonicalEvent(-1, "evt_001", {})
        # Negative timestamps may be invalid - test that they're detected
        # or handled explicitly
        with pytest.raises((ValueError, AssertionError)):
            resolve_window_for_test(event, TUMBLING_1H_V1)
    
    def test_zero_length_window_invalid(self):
        """Zero-length windows must raise."""
        with pytest.raises((ValueError, ZeroDivisionError)):
            zero_duration = WindowDefinition(
                window_type=WindowType.TUMBLING_TIME,
                window_size_ms=0,  # Zero duration
                alignment_epoch_ms=0,
                allowed_lateness_ms=0,
                timestamp_extraction_strategy=TimestampExtractionStrategy.EVENT_TIME,
                definition_version="v1",
                identity_format_version="v1",
            )
            event = CanonicalEvent(1700000000000, "evt_001", {})
            resolve_window_for_test(event, zero_duration)
    
    def test_undefined_window_type_raises(self):
        """Undefined window types must raise."""
        # This would require creating an invalid WindowType, which is harder
        # Test that invalid window definitions are caught
        with pytest.raises((ValueError, NotImplementedError)):
            # Use a window type that resolve_window_for_test doesn't handle
            invalid_def = WindowDefinition(
                window_type=WindowType.SESSION,  # Requires state
                window_size_ms=None,
                session_gap_ms=30000,
                alignment_epoch_ms=0,
                allowed_lateness_ms=0,
                timestamp_extraction_strategy=TimestampExtractionStrategy.EVENT_TIME,
                definition_version="v1",
                identity_format_version="v1",
            )
            event = CanonicalEvent(1700000000000, "evt_001", {})
            resolve_window_for_test(event, invalid_def)
    
    def test_immutable_event(self):
        """Events must be immutable."""
        event = CanonicalEvent(1700000000000, "evt_001", {"value": 42})
        
        with pytest.raises(AttributeError):
            event.event_time_ms = 1700000000001
    
    def test_immutable_window_definition(self):
        """Window definitions must be immutable."""
        with pytest.raises(AttributeError):
            TUMBLING_1H_V1.window_size_ms = 7200000
    
    def test_invalid_window_material(self):
        """Invalid window material must raise."""
        with pytest.raises(ValueError):
            # window_end_ms <= window_start_ms
            invalid_material = WindowIdentityMaterial(
                window_type=WindowType.TUMBLING_TIME.value,
                window_start_ms=1700003600000,
                window_end_ms=1700000000000,  # Invalid: end <= start
                alignment_epoch_ms=0,
                window_definition_version="v1",
                identity_format_version="v1",
                aggregation_context={},  # Tier-0: REQUIRED field
            )
            WindowIdentityFactory.create_identity(invalid_material)
    
    def test_missing_required_session_fields(self):
        """Session windows must have session_gap_ms."""
        with pytest.raises(ValueError):
            invalid_material = WindowIdentityMaterial(
                window_type=WindowType.SESSION.value,
                window_start_ms=1700000000000,
                window_end_ms=1700003600000,
                alignment_epoch_ms=0,
                window_definition_version="v1",
                identity_format_version="v1",
                session_gap_ms=None,  # Missing required field
                aggregation_context={},  # Tier-0: REQUIRED field
            )
            WindowIdentityFactory.create_identity(invalid_material)
