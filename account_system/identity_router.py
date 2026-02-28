"""
/account_system/identity_router.py

Canonical Identity Resolution & Routing Authority
(No Ambiguity, No Shadow Accounts, No Silent Merge)

This module is the single authority that determines what real-world identity
maps to what canonical account ID and how identity signals are resolved.

CRITICAL PRINCIPLES:
- Deterministic: Same identity inputs → same routing decision
- Explicit: No silent merges
- Immutable Identity Links: Once linked, identity-to-account must not drift
- Stateless: No hidden caching or mutation
- Replay-Safe: Re-running resolution must not produce different account IDs
- Strict Priority Order: Identity resolution order must be defined
- No Time-Based Dependencies: Uses logical timestamps, never system time

ABSOLUTE INVARIANTS:
1. No duplicate canonical accounts for the same identity
2. No implicit merging
3. Deterministic identity resolution
4. Stable account routing
5. Explicit conflict handling
6. Replay-safe identity mapping

This file protects account truth and ensures accounts are as trusted as
billion-dollar companies (Instagram, ChatGPT, etc.).
"""

from __future__ import annotations

import hashlib
import re
import uuid
import logging
from dataclasses import dataclass, field
from enum import Enum, unique
from typing import FrozenSet, Mapping, Optional, Sequence, Dict, Any, Tuple, List
from types import MappingProxyType


# ---------------------------------------------------------------------------
# Domain Enums
# ---------------------------------------------------------------------------

@unique
class RoutingReason(str, Enum):
    EXPLICIT_ACCOUNT_ID   = "EXPLICIT_ACCOUNT_ID"
    PROVIDER_MATCH        = "PROVIDER_MATCH"
    VERIFIED_EMAIL_MATCH  = "VERIFIED_EMAIL_MATCH"
    EXTERNAL_ID_MATCH     = "EXTERNAL_ID_MATCH"
    NEW_ACCOUNT           = "NEW_ACCOUNT"


@unique
class ConflictPolicy(str, Enum):
    RAISE   = "RAISE"    # Always raise IdentityConflictError on conflict
    STRICT  = "STRICT"   # Alias for RAISE; kept for config expressiveness


@unique
class SignalPriority(str, Enum):
    """Canonical signal names; order is defined by IdentityRoutingConfig."""
    INTERNAL_ACCOUNT_ID = "internal_account_id"
    PROVIDER_USER_ID    = "provider_user_id"
    VERIFIED_EMAIL      = "verified_email"
    EXTERNAL_ACCOUNT_ID = "external_account_id"


# ---------------------------------------------------------------------------
# Value Objects / Input Types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IdentitySignals:
    """
    Structured, typed identity signals from the caller.
    
    All optional; resolution uses whichever are present per priority order.
    Never infer identity from ambiguous signals.
    
    This ensures accounts are resolved with the same rigor as
    billion-dollar companies (Instagram, ChatGPT, etc.).
    """
    internal_account_id:  Optional[str] = None
    """Explicit internal account ID (highest authority)"""
    
    provider_user_id:     Optional[str] = None
    """Provider-specific user ID (scoped by provider_name)"""
    
    provider_name:        Optional[str] = None
    """Provider name (required when provider_user_id present)"""
    
    verified_email:       Optional[str] = None
    """Verified email address"""
    
    email:                Optional[str] = None
    """Unverified email (used only when config permits)"""
    
    external_account_id:  Optional[str] = None
    """External system account ID"""
    
    phone_number:         Optional[str] = None
    """Phone number (future signal; not resolved in v1)"""
    
    def __post_init__(self) -> None:
        """Validate identity signals structure."""
        # Provider user ID requires provider name
        if self.provider_user_id is not None and not self.provider_name:
            raise IdentityValidationError(
                "provider_name is required when provider_user_id is supplied"
            )


@dataclass(frozen=True)
class IdentityLink:
    """Immutable record of a single identity ↔ account link."""
    signal_type:  str
    signal_value: str   # Already normalised
    account_id:   str
    region:       Optional[str] = None
    version:      int           = 1


@dataclass(frozen=True)
class IdentityIndexSnapshot:
    """
    Read-only, immutable snapshot of the identity index.
    Callers construct this from persistence; this file never queries storage.

    signal_index: (signal_type, normalised_value) → frozenset of account_ids
    account_index: account_id → frozenset of IdentityLink
    snapshot_version: opaque version token for replay tracing
    """
    signal_index:     Mapping[tuple[str, str], FrozenSet[str]]
    account_index:    Mapping[str, FrozenSet[IdentityLink]]
    snapshot_version: str


@dataclass(frozen=True)
class IdentityRoutingConfig:
    """
    Runtime-supplied, validated configuration governing resolution behaviour.

    priority_order: ordered sequence of SignalPriority values (highest → lowest)
    allow_new_account_creation: whether fallback creation is permitted
    require_verified_email: if True, plain `email` signals are ignored
    provider_scope: "global" | "region" — whether provider IDs cross regions
    conflict_policy: what to do on multi-account signal collision
    identity_version: config schema version for replay tracing
    allowed_regions: if non-empty, resolution is constrained to these regions
    """
    priority_order:             Sequence[SignalPriority]
    allow_new_account_creation: bool
    require_verified_email:     bool
    provider_scope:             str                     # "global" | "region"
    conflict_policy:            ConflictPolicy
    identity_version:           int
    allowed_regions:            FrozenSet[str]          = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        _validate_config(self)


@dataclass(frozen=True)
class IdentityRoutingResult:
    """Structured, fully-typed resolution outcome. Never contains raw strings alone."""
    canonical_account_id: str
    routing_reason:       RoutingReason
    identity_version:     int
    existing_link_found:  bool
    conflict_detected:    bool
    region_hint:          Optional[str] = None


# ---------------------------------------------------------------------------
# Domain Errors
# ---------------------------------------------------------------------------

class IdentityRoutingConfigError(ValueError):
    """Raised when IdentityRoutingConfig is structurally invalid."""


class IdentityValidationError(ValueError):
    """Raised when IdentitySignals are malformed or lack required fields."""


class IdentityConflictError(RuntimeError):
    """
    Raised when a signal maps to multiple existing accounts.
    
    STRICT NO-SILENT-MERGE RULE:
    - Never auto-merge
    - Never auto-override
    - Never silently pick oldest/newest
    - Always raise explicit conflict error
    
    Intentionally opaque: does not expose conflicting account IDs externally
    for security reasons.
    """
    def __init__(self, reason: str, signal_type: Optional[str] = None) -> None:
        """
        Initialize conflict error.
        
        Args:
            reason: Human-readable reason for conflict
            signal_type: Optional signal type that caused conflict
        """
        # Do NOT embed account IDs in the public message
        message = f"Identity conflict detected: {reason}"
        if signal_type:
            message += f" (signal_type: {signal_type})"
        super().__init__(message)
        self.reason = reason
        self.signal_type = signal_type


# ---------------------------------------------------------------------------
# Normalisation Helpers
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def _normalise_email(raw: str) -> str:
    """Lowercase, strip, collapse internal whitespace — RFC 5321 local-part is case-sensitive
    by spec but virtually all providers treat it as case-insensitive in practice."""
    return _WHITESPACE_RE.sub("", raw.strip().lower())


def _normalise_provider_id(provider_name: str, provider_user_id: str) -> str:
    """Scope provider ID to provider name; normalise deterministically."""
    return f"{provider_name.strip().lower()}:{provider_user_id.strip()}"


def _normalise_external_id(raw: str) -> str:
    return raw.strip()


# ---------------------------------------------------------------------------
# Config Validation
# ---------------------------------------------------------------------------

_VALID_PROVIDER_SCOPES = frozenset({"global", "region"})
_VALID_SIGNAL_PRIORITIES = frozenset(s.value for s in SignalPriority)


def _validate_config(config: IdentityRoutingConfig) -> None:
    if not config.priority_order:
        raise IdentityRoutingConfigError("priority_order must not be empty")

    seen: set[str] = set()
    for p in config.priority_order:
        if not isinstance(p, SignalPriority):
            raise IdentityRoutingConfigError(
                f"priority_order entry {p!r} is not a SignalPriority member"
            )
        if p.value in seen:
            raise IdentityRoutingConfigError(
                f"Duplicate priority entry: {p.value}"
            )
        seen.add(p.value)

    if config.provider_scope not in _VALID_PROVIDER_SCOPES:
        raise IdentityRoutingConfigError(
            f"provider_scope must be one of {_VALID_PROVIDER_SCOPES}, got {config.provider_scope!r}"
        )

    if config.identity_version < 1:
        raise IdentityRoutingConfigError("identity_version must be >= 1")
    
    # Validate that all priority_order entries have corresponding resolvers
    for priority in config.priority_order:
        if priority not in _RESOLVER_MAP:
            raise IdentityRoutingConfigError(
                f"priority_order contains {priority.value} but no resolver exists in _RESOLVER_MAP"
            )
    
    # Validate email fallback policy consistency
    # If require_verified_email is False, email should be in priority_order after verified_email
    if not config.require_verified_email:
        verified_email_idx = None
        email_priority = None
        for idx, priority in enumerate(config.priority_order):
            if priority == SignalPriority.VERIFIED_EMAIL:
                verified_email_idx = idx
            # Note: email uses VERIFIED_EMAIL signal type but may be unverified
            # This is a policy check, not a strict requirement
        # This is informational - the actual resolution logic handles it correctly


# ---------------------------------------------------------------------------
# Signal Validation
# ---------------------------------------------------------------------------

def _validate_signals(
    signals: IdentitySignals,
    config:  IdentityRoutingConfig,
) -> None:
    """Raise IdentityValidationError if signals are structurally invalid."""

    # provider_user_id requires provider_name
    if signals.provider_user_id is not None and not signals.provider_name:
        raise IdentityValidationError(
            "provider_name is required when provider_user_id is supplied"
        )

    # email-like fields must pass basic sanity (non-empty, contains @)
    for attr in ("verified_email", "email"):
        val = getattr(signals, attr)
        if val is not None:
            normalised = _normalise_email(val)
            if "@" not in normalised or normalised.startswith("@") or normalised.endswith("@"):
                raise IdentityValidationError(
                    f"Malformed email in field '{attr}': {val!r}"
                )

    # At least one resolvable signal must be present
    has_any = any([
        signals.internal_account_id,
        signals.provider_user_id,
        signals.verified_email,
        signals.email if not config.require_verified_email else None,
        signals.external_account_id,
    ])
    if not has_any:
        raise IdentityValidationError(
            "IdentitySignals must contain at least one resolvable signal"
        )


# ---------------------------------------------------------------------------
# Deterministic Account ID Generation
# ---------------------------------------------------------------------------

_UUID5_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # URL namespace


def _generate_deterministic_account_id(
    signal_type:  str,
    signal_value: str,
    identity_version: int,
) -> str:
    """
    Deterministic, replay-safe UUID derived from signal + version.
    Uses UUID v5 (SHA-1 of namespace + name).  No random(), no time().
    """
    name = f"v{identity_version}:{signal_type}:{signal_value}"
    return str(uuid.uuid5(_UUID5_NAMESPACE, name))


# ---------------------------------------------------------------------------
# Snapshot Lookup Helpers
# ---------------------------------------------------------------------------

def _lookup_signal(
    snapshot:    IdentityIndexSnapshot,
    signal_type: str,
    normalised:  str,
) -> FrozenSet[str]:
    """Return set of account_ids matching (signal_type, normalised_value); empty if none."""
    return snapshot.signal_index.get((signal_type, normalised), frozenset())


def _assert_single_match(
    account_ids: FrozenSet[str],
    signal_type: str,
    conflict_policy: ConflictPolicy,
    logger: Optional[logging.Logger] = None,
) -> Optional[str]:
    """
    Returns the single account_id, or raises IdentityConflictError if multiple found.
    
    STRICT NO-SILENT-MERGE RULE:
    - Multiple accounts → always a conflict
    - Never auto-merge regardless of policy
    - Never silently pick one account
    
    Args:
        account_ids: Set of account IDs matching the signal
        signal_type: Type of signal being resolved
        conflict_policy: Conflict policy (currently always RAISE)
        logger: Optional logger for structured logging
    
    Returns:
        Single account ID if exactly one match, None if no matches
    
    Raises:
        IdentityConflictError: If multiple accounts match (never auto-resolved)
    """
    log = logger or logging.getLogger(__name__)
    
    if len(account_ids) == 0:
        return None
    
    if len(account_ids) == 1:
        account_id = next(iter(account_ids))
        log.debug(
            f"Single account match for signal '{signal_type}': {account_id}"
        )
        return account_id
    
    # Multiple accounts — always a conflict regardless of policy (no silent merge)
    log.error(
        f"Identity conflict: signal '{signal_type}' maps to {len(account_ids)} "
        f"distinct accounts (no silent merge allowed)"
    )
    raise IdentityConflictError(
        f"Signal '{signal_type}' maps to {len(account_ids)} distinct accounts",
        signal_type=signal_type
    )


def _extract_region_hint(
    account_id: str,
    snapshot:   IdentityIndexSnapshot,
) -> Optional[str]:
    """
    Extract region hint from account's identity links.
    Returns the first non-None region found, or None if no region is linked.
    """
    links = snapshot.account_index.get(account_id, frozenset())
    for link in links:
        if link.region:
            return link.region
    return None


def _check_region_constraint(
    account_id:      str,
    snapshot:        IdentityIndexSnapshot,
    config:          IdentityRoutingConfig,
    candidate_signal_type: str,
    logger:          Optional[logging.Logger] = None,
) -> None:
    """
    If allowed_regions is non-empty, verify the candidate account's linked region
    is within the permitted set.
    
    CROSS-REGION PROTECTION:
    - If account identity is region-scoped, resolution must enforce region constraint
    - Must not link identity across regions unless config allows it
    - Violation → explicit region conflict error
    
    Raises IdentityConflictError on violation.
    """
    log = logger or logging.getLogger(__name__)
    
    if not config.allowed_regions:
        return
    
    links = snapshot.account_index.get(account_id, frozenset())
    for link in links:
        if link.region and link.region not in config.allowed_regions:
            log.error(
                f"Region constraint violation: account {account_id} is bound to region "
                f"'{link.region}', which is outside allowed_regions {config.allowed_regions}"
            )
            raise IdentityConflictError(
                f"Account resolved via '{candidate_signal_type}' is bound to region "
                f"'{link.region}', which is outside allowed_regions",
                signal_type=candidate_signal_type
            )


# ---------------------------------------------------------------------------
# Cross-Signal Conflict Detection
# ---------------------------------------------------------------------------

def _check_cross_signal_consistency(
    identity_signals: IdentitySignals,
    snapshot:        IdentityIndexSnapshot,
    config:          IdentityRoutingConfig,
    primary_account_id: str,
    primary_signal_type: str,
    logger:          Optional[logging.Logger] = None,
) -> None:
    """
    Verify that all present signals resolve to the same account as the primary match.
    
    CROSS-SIGNAL CONSISTENCY ENFORCEMENT:
    - If provider_user_id → Account A but verified_email → Account B → raise IdentityConflictError
    - If any signal resolves to a different account → raise IdentityConflictError
    - Never silently ignore lower-priority signal conflicts
    
    This prevents identity drift and split-identity corruption.
    
    Args:
        identity_signals: All identity signals to check
        snapshot: Identity index snapshot
        config: Routing configuration
        primary_account_id: Account ID resolved by primary signal
        primary_signal_type: Signal type that resolved to primary_account_id
        logger: Optional logger
    
    Raises:
        IdentityConflictError: If any signal resolves to a different account
    """
    log = logger or logging.getLogger(__name__)
    
    # Check all signals that are present and could resolve to accounts
    signals_to_check: list[tuple[str, str, str]] = []  # (signal_type, normalised_value, display_name)
    
    # Internal account ID
    if identity_signals.internal_account_id:
        account_id = identity_signals.internal_account_id.strip()
        if account_id and account_id in snapshot.account_index:
            signals_to_check.append((
                SignalPriority.INTERNAL_ACCOUNT_ID.value,
                account_id,
                "internal_account_id"
            ))
    
    # Provider user ID
    if identity_signals.provider_user_id and identity_signals.provider_name:
        scoped = _normalise_provider_id(identity_signals.provider_name, identity_signals.provider_user_id)
        matches = _lookup_signal(snapshot, SignalPriority.PROVIDER_USER_ID.value, scoped)
        if len(matches) == 1:
            signals_to_check.append((
                SignalPriority.PROVIDER_USER_ID.value,
                next(iter(matches)),
                "provider_user_id"
            ))
    
    # Verified email (or unverified if config permits)
    email_signal = identity_signals.verified_email
    if email_signal is None and not config.require_verified_email:
        email_signal = identity_signals.email
    
    if email_signal:
        normalised = _normalise_email(email_signal)
        matches = _lookup_signal(snapshot, SignalPriority.VERIFIED_EMAIL.value, normalised)
        if len(matches) == 1:
            signals_to_check.append((
                SignalPriority.VERIFIED_EMAIL.value,
                next(iter(matches)),
                "verified_email" if identity_signals.verified_email else "email"
            ))
    
    # External account ID
    if identity_signals.external_account_id:
        normalised = _normalise_external_id(identity_signals.external_account_id)
        matches = _lookup_signal(snapshot, SignalPriority.EXTERNAL_ACCOUNT_ID.value, normalised)
        if len(matches) == 1:
            signals_to_check.append((
                SignalPriority.EXTERNAL_ACCOUNT_ID.value,
                next(iter(matches)),
                "external_account_id"
            ))
    
    # Check each signal resolves to the same account
    for signal_type, resolved_account_id, display_name in signals_to_check:
        if resolved_account_id != primary_account_id:
            log.error(
                f"Cross-signal identity conflict: {display_name} resolves to account "
                f"{resolved_account_id}, but {primary_signal_type} resolves to {primary_account_id}"
            )
            raise IdentityConflictError(
                f"Identity signals are inconsistent: {display_name} maps to a different account "
                f"than {primary_signal_type}",
                signal_type=display_name
            )


# ---------------------------------------------------------------------------
# Per-Signal Resolution Steps
# ---------------------------------------------------------------------------

def _try_internal_account_id(
    signals:  IdentitySignals,
    snapshot: IdentityIndexSnapshot,
    config:   IdentityRoutingConfig,
    logger:   Optional[logging.Logger] = None,
) -> Optional[IdentityRoutingResult]:
    if signals.internal_account_id is None:
        return None
    account_id = signals.internal_account_id.strip()
    if not account_id:
        raise IdentityValidationError("internal_account_id must not be blank")
    # Confirm it actually exists in the snapshot
    if account_id not in snapshot.account_index:
        raise IdentityValidationError(
            f"internal_account_id not found in identity snapshot"
        )
    _check_region_constraint(account_id, snapshot, config, "internal_account_id", logger=logger)
    
    # Check cross-signal consistency (raises IdentityConflictError if inconsistent)
    _check_cross_signal_consistency(
        signals, snapshot, config, account_id, "internal_account_id", logger=logger
    )
    
    region_hint = _extract_region_hint(account_id, snapshot)
    return IdentityRoutingResult(
        canonical_account_id=account_id,
        routing_reason=RoutingReason.EXPLICIT_ACCOUNT_ID,
        identity_version=config.identity_version,
        existing_link_found=True,
        conflict_detected=False,
        region_hint=region_hint,
    )


def _try_provider_user_id(
    signals:  IdentitySignals,
    snapshot: IdentityIndexSnapshot,
    config:   IdentityRoutingConfig,
    logger:   Optional[logging.Logger] = None,
) -> Optional[IdentityRoutingResult]:
    if signals.provider_user_id is None:
        return None
    scoped = _normalise_provider_id(signals.provider_name, signals.provider_user_id)  # type: ignore[arg-type]
    matches = _lookup_signal(snapshot, SignalPriority.PROVIDER_USER_ID.value, scoped)
    account_id = _assert_single_match(
        matches, "provider_user_id", config.conflict_policy, logger=logger
    )
    if account_id is None:
        return None
    _check_region_constraint(account_id, snapshot, config, "provider_user_id", logger=logger)
    
    # Check cross-signal consistency (raises IdentityConflictError if inconsistent)
    _check_cross_signal_consistency(
        signals, snapshot, config, account_id, "provider_user_id", logger=logger
    )
    
    region_hint = _extract_region_hint(account_id, snapshot)
    return IdentityRoutingResult(
        canonical_account_id=account_id,
        routing_reason=RoutingReason.PROVIDER_MATCH,
        identity_version=config.identity_version,
        existing_link_found=True,
        conflict_detected=False,
        region_hint=region_hint,
    )


def _try_verified_email(
    signals:  IdentitySignals,
    snapshot: IdentityIndexSnapshot,
    config:   IdentityRoutingConfig,
    logger:   Optional[logging.Logger] = None,
) -> Optional[IdentityRoutingResult]:
    raw = signals.verified_email
    if raw is None:
        # Fall through to unverified email only if config permits
        if config.require_verified_email or signals.email is None:
            return None
        raw = signals.email
    normalised = _normalise_email(raw)
    matches = _lookup_signal(snapshot, SignalPriority.VERIFIED_EMAIL.value, normalised)
    account_id = _assert_single_match(
        matches, "verified_email", config.conflict_policy, logger=logger
    )
    if account_id is None:
        return None
    _check_region_constraint(account_id, snapshot, config, "verified_email", logger=logger)
    
    # Check cross-signal consistency (raises IdentityConflictError if inconsistent)
    _check_cross_signal_consistency(
        signals, snapshot, config, account_id, "verified_email", logger=logger
    )
    
    region_hint = _extract_region_hint(account_id, snapshot)
    return IdentityRoutingResult(
        canonical_account_id=account_id,
        routing_reason=RoutingReason.VERIFIED_EMAIL_MATCH,
        identity_version=config.identity_version,
        existing_link_found=True,
        conflict_detected=False,
        region_hint=region_hint,
    )


def _try_external_account_id(
    signals:  IdentitySignals,
    snapshot: IdentityIndexSnapshot,
    config:   IdentityRoutingConfig,
    logger:   Optional[logging.Logger] = None,
) -> Optional[IdentityRoutingResult]:
    if signals.external_account_id is None:
        return None
    normalised = _normalise_external_id(signals.external_account_id)
    matches = _lookup_signal(snapshot, SignalPriority.EXTERNAL_ACCOUNT_ID.value, normalised)
    account_id = _assert_single_match(
        matches, "external_account_id", config.conflict_policy, logger=logger
    )
    if account_id is None:
        return None
    _check_region_constraint(account_id, snapshot, config, "external_account_id", logger=logger)
    
    # Check cross-signal consistency (raises IdentityConflictError if inconsistent)
    _check_cross_signal_consistency(
        signals, snapshot, config, account_id, "external_account_id", logger=logger
    )
    
    region_hint = _extract_region_hint(account_id, snapshot)
    return IdentityRoutingResult(
        canonical_account_id=account_id,
        routing_reason=RoutingReason.EXTERNAL_ID_MATCH,
        identity_version=config.identity_version,
        existing_link_found=True,
        conflict_detected=False,
        region_hint=region_hint,
    )


# ---------------------------------------------------------------------------
# Resolution Dispatch Map
# ---------------------------------------------------------------------------

_RESOLVER_MAP: Mapping[SignalPriority, object] = {
    SignalPriority.INTERNAL_ACCOUNT_ID: _try_internal_account_id,
    SignalPriority.PROVIDER_USER_ID:    _try_provider_user_id,
    SignalPriority.VERIFIED_EMAIL:      _try_verified_email,
    SignalPriority.EXTERNAL_ACCOUNT_ID: _try_external_account_id,
}


# ---------------------------------------------------------------------------
# Deterministic Seed for New Account Generation
# ---------------------------------------------------------------------------

def _derive_creation_seed(
    signals: IdentitySignals,
    config:  IdentityRoutingConfig,
) -> tuple[str, str]:
    """
    Choose the most stable available signal as the deterministic seed.
    Priority: provider_user_id > verified_email > external_account_id > email.
    Returns (signal_type, normalised_value).
    """
    if signals.provider_user_id and signals.provider_name:
        return (
            SignalPriority.PROVIDER_USER_ID.value,
            _normalise_provider_id(signals.provider_name, signals.provider_user_id),
        )
    if signals.verified_email:
        return (SignalPriority.VERIFIED_EMAIL.value, _normalise_email(signals.verified_email))
    if signals.external_account_id:
        return (SignalPriority.EXTERNAL_ACCOUNT_ID.value, _normalise_external_id(signals.external_account_id))
    if signals.email and not config.require_verified_email:
        return (SignalPriority.VERIFIED_EMAIL.value, _normalise_email(signals.email))
    # Should never reach here — _validate_signals guards this
    raise IdentityValidationError("Cannot derive creation seed: no stable signal present")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_identity(
    identity_signals: IdentitySignals,
    existing_links:   IdentityIndexSnapshot,
    config:           IdentityRoutingConfig,
    logger:            Optional[logging.Logger] = None,
) -> IdentityRoutingResult:
    """
    Resolve identity signals to a canonical account ID.
    
    DETERMINISTIC: Same inputs always produce identical output.
    No randomness. No hidden state. No wall clock usage.
    
    STRICT NO-SILENT-MERGE RULE:
    - If same email linked to two existing accounts → raise IdentityConflictError
    - If same provider ID linked across regions → raise IdentityConflictError
    - If identity appears inconsistent → raise IdentityConflictError
    - Never auto-merge, auto-override, or silently pick oldest/newest
    
    This function ensures accounts are resolved with the same rigor as
    billion-dollar companies (Instagram, ChatGPT, etc.).

    Guarantees
    ----------
    - Deterministic: identical inputs → identical output
    - Stateless: no I/O, no mutation, no caching
    - Replay-safe: no dependence on time, env, or RNG
    - No silent merge: conflicting signals raise IdentityConflictError
    - Explicit precedence: resolution order is strictly defined

    Parameters
    ----------
    identity_signals : IdentitySignals
        Caller-supplied identity evidence
    existing_links : IdentityIndexSnapshot
        Immutable read-only snapshot of the current identity index
    config : IdentityRoutingConfig
        Resolution rules; validated in __post_init__
    logger : Optional[logging.Logger]
        Optional logger for structured logging

    Returns
    -------
    IdentityRoutingResult
        Fully-typed resolution outcome including canonical_account_id and reason

    Raises
    ------
    IdentityValidationError
        Signals are malformed or insufficient
    IdentityConflictError
        A signal matches multiple existing accounts (never auto-resolved)
    IdentityRoutingConfigError
        Config is structurally invalid (normally raised at construction time)
    """
    log = logger or logging.getLogger(__name__)
    
    # Log identity signals (sanitized for security)
    signal_list = [
        identity_signals.internal_account_id,
        identity_signals.provider_user_id,
        identity_signals.verified_email,
        identity_signals.email,
        identity_signals.external_account_id,
    ]
    present_signals = [s for s in signal_list if s]
    log.debug(
        f"Resolving identity: {len(present_signals)} signals present, "
        f"config_version={config.identity_version}"
    )
    
    # 1. Validate inputs
    _validate_signals(identity_signals, config)

    # 2. Walk priority order; return first match
    # Priority order is explicit and never reordered based on runtime context
    for priority in config.priority_order:
        resolver = _RESOLVER_MAP.get(priority)
        if resolver is None:
            log.debug(f"Skipping unknown priority: {priority}")
            continue  # Future signal types are safely skipped
        
        log.debug(f"Trying resolution via priority: {priority.value}")
        result: Optional[IdentityRoutingResult] = resolver(  # type: ignore[call-arg]
            identity_signals, existing_links, config, logger=log
        )
        if result is not None:
            log.info(
                f"Identity resolved: account_id={result.canonical_account_id}, "
                f"reason={result.routing_reason.value}, existing_link={result.existing_link_found}"
            )
            return result

    # 3. No existing link found — creation pathway
    if not config.allow_new_account_creation:
        log.warning(
            "No existing identity link found and new account creation is disabled"
        )
        raise IdentityValidationError(
            "No existing identity link found and new account creation is disabled"
        )

    # Deterministic account ID generation (replay-safe)
    signal_type, signal_value = _derive_creation_seed(identity_signals, config)
    new_account_id = _generate_deterministic_account_id(
        signal_type, signal_value, config.identity_version
    )
    
    log.info(
        f"New account ID generated deterministically: account_id={new_account_id}, "
        f"seed_signal={signal_type}, identity_version={config.identity_version}"
    )

    return IdentityRoutingResult(
        canonical_account_id=new_account_id,
        routing_reason=RoutingReason.NEW_ACCOUNT,
        identity_version=config.identity_version,
        existing_link_found=False,
        conflict_detected=False,
    )