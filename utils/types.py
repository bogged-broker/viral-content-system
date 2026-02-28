"""
Shared type aliases (structural only, no semantics).

Centralized vocabulary layer for cross-module structural types.
No validation, no logic, no domain coupling. Import discipline: typing, typing_extensions, collections.abc only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import FrozenSet, Tuple, Union

try:
    from typing import TypeAlias
except ImportError:
    from typing_extensions import TypeAlias

from typing_extensions import Protocol, TypeVar, runtime_checkable


# Primitive canonical aliases (readability only, no validation)
EpochMillis: TypeAlias = int
VersionString: TypeAlias = str
JSONString: TypeAlias = str
HashString: TypeAlias = str
Identifier: TypeAlias = str


# ID branding (structural only, prevents cross-domain mixing at type-check time)
ContentID: TypeAlias = str
AccountID: TypeAlias = str
WindowID: TypeAlias = str
ComputationID: TypeAlias = str
RunID: TypeAlias = str
ReplayID: TypeAlias = str


# JSON structural types (float excluded for deterministic serialization)
JSONPrimitive: TypeAlias = Union[str, int, bool, None]
JSONValue: TypeAlias = Union[
    JSONPrimitive,
    Sequence["JSONValue"],
    Mapping[str, "JSONValue"],
]
JSONObject: TypeAlias = Mapping[str, JSONValue]


# Canonical mapping aliases
StrMap: TypeAlias = Mapping[str, str]
StrIntMap: TypeAlias = Mapping[str, int]
StrAnyMap: TypeAlias = Mapping[str, object]


# Immutable structural patterns
StringTuple: TypeAlias = Tuple[str, ...]
IntTuple: TypeAlias = Tuple[int, ...]
StrFrozenSet: TypeAlias = FrozenSet[str]


# Generic structural contracts
T = TypeVar("T")

@runtime_checkable
class Serializable(Protocol):
    """Structural protocol for canonical JSON serialization."""
    def to_canonical(self) -> JSONObject:
        ...


__all__ = [
    "EpochMillis",
    "VersionString",
    "JSONString",
    "HashString",
    "Identifier",
    "ContentID",
    "AccountID",
    "WindowID",
    "ComputationID",
    "RunID",
    "ReplayID",
    "JSONPrimitive",
    "JSONValue",
    "JSONObject",
    "StrMap",
    "StrIntMap",
    "StrAnyMap",
    "StringTuple",
    "IntTuple",
    "StrFrozenSet",
    "T",
    "Serializable",
]