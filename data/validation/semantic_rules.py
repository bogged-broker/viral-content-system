"""
/data/validation/semantic_rules.py

Cross-Field Semantic Validation Specification
(Business-Level Consistency Enforcement)

---

1️⃣ Purpose

This file defines:

- Cross-field logical consistency checks
- Interdependent field legality
- Conditional presence requirements
- Temporal ordering correctness
- Logical implication enforcement
- Domain rule coherence
- Version-conditional semantic behavior

These rules operate on:

> Relationships between fields inside a single artifact or payload.

They do not:

- Access lineage graph
- Query historical state
- Apply migrations
- Validate compatibility matrices
- Enforce hashing invariants

This layer answers:

> "Even if every field is structurally correct — does the object logically make sense?"

---

2️⃣ Architectural Position

Validation layers stack as:

1. field_rules.py → atomic structure
2. semantic_rules.py → internal logical correctness
3. invariants.py → system-wide truths
4. compatibility_guards.py → cross-version legality
5. deterministic_checks.py → replay safety

Semantic rules sit strictly between structure and invariants.

They enforce coherence before system-level constraints.

---

3️⃣ Design Principles

Semantic rules must be:

- Deterministic
- Side-effect free
- Context-aware
- Version-aware
- Isolated (no cross-artifact queries)
- Stateless
- Idempotent

Given identical:

- input_data
- ValidationContext

They must produce identical violations.

---

4️⃣ What Semantic Rules Enforce

This layer enforces patterns like:

Temporal Logic
- published_at >= created_at
- expires_at > created_at

Conditional Requirements
- If status == "published" → published_at must exist
- If deleted == True → deleted_at must exist

Logical Implications
- If is_flagged == True → moderation_level must exist
- If archived == True → active == False

Mutually Exclusive States
- deleted == True AND published == True may be illegal
- draft == True AND archived == True may be illegal

Numeric Relationships
- likes <= views
- shares <= impressions
- engagement_rate <= 1.0

Version Conditional Logic
- In v2+, retention metrics required if engagement_count present
- In v3+, status enum expanded and old combinations prohibited

All semantic rules operate purely within one artifact.

---

5️⃣ Rule Interface

Each rule must implement the contract:

class SemanticRule:
    rule_id: str
    severity: SeverityLevel

    def evaluate(self, input_data, context) -> ValidationViolation | None:
        ...

Must not:

- Throw business exceptions
- Access external state
- Use randomness
- Call datetime.now()
- Modify input_data

---

6️⃣ Example: Temporal Ordering Rule

class PublishedAfterCreationRule:
    rule_id = "SEMANTIC_PUBLISH_TIME_ORDER"
    severity = SeverityLevel.ERROR

    def evaluate(self, input_data, context):
        created = input_data.get("created_at")
        published = input_data.get("published_at")

        if created is None or published is None:
            return None

        if published < created:
            message = "published_at must be >= created_at"
            return build_violation(
                self.rule_id,
                message,
                SeverityLevel.ERROR,
                field_path="published_at",
            )

        return None

This rule:

- Does not modify values.
- Does not auto-correct.
- Does not round timestamps.
- Does not infer missing fields.

It only evaluates coherence.

---

7️⃣ Example: Conditional Presence Rule

class StatusRequiresPublishTimeRule:
    rule_id = "SEMANTIC_STATUS_REQUIRES_PUBLISH_TIME"
    severity = SeverityLevel.ERROR

    def evaluate(self, input_data, context):
        status = input_data.get("status")
        published = input_data.get("published_at")

        if status == "published" and published is None:
            message = "published artifacts require published_at"
            return build_violation(
                self.rule_id,
                message,
                self.severity,
                field_path="published_at",
            )
        return None

No cross-record queries. No registry access.

---

8️⃣ Example: Logical Mutual Exclusion Rule

class DeletedCannotBePublishedRule:
    rule_id = "SEMANTIC_DELETED_CANNOT_BE_PUBLISHED"
    severity = SeverityLevel.CRITICAL

    def evaluate(self, input_data, context):
        if input_data.get("deleted") and input_data.get("status") == "published":
            return build_violation(
                self.rule_id,
                "Deleted artifacts cannot be published",
                self.severity,
                field_path="status",
            )
        return None

This may be CRITICAL because it represents state contradiction.

---

9️⃣ Version-Aware Semantic Rule

Semantic logic may branch on:

context.artifact_version

Example:

class RetentionRequiredInV2Rule:
    rule_id = "SEMANTIC_RETENTION_REQUIRED_V2_PLUS"
    severity = SeverityLevel.ERROR

    def evaluate(self, input_data, context):
        version = context.artifact_version

        if version and version >= "2":
            if input_data.get("engagement_count") and not input_data.get("retention_rate"):
                return build_violation(
                    self.rule_id,
                    "retention_rate required when engagement_count present in v2+",
                    self.severity,
                    field_path="retention_rate",
                )
        return None

Version comparison must use pre-parsed deterministic version representation.
Never string lexicographic comparisons in production.

---

🔟 Composition of Semantic Rules

This file must expose:

SEMANTIC_RULES = [
    PublishedAfterCreationRule(),
    StatusRequiresPublishTimeRule(),
    DeletedCannotBePublishedRule(),
    RetentionRequiredInV2Rule(),
    ...
]

Rules must:

- Be static at import time.
- Deterministically ordered (or sorted by orchestrator).
- Not dynamically generated from registry.

Dynamic generation breaks replay determinism unless fingerprinted.

---

1️⃣1️⃣ Separation From Invariants

Semantic rules:

- Concern logical correctness inside artifact.

Invariant rules:

- Concern system-wide truths like:
  - hash matches canonical JSON
  - ID immutability
  - lineage parent existence
  - fingerprint match

Semantic rules must not:

- Compute hashes
- Validate derivation lineage
- Query external constraints

---

1️⃣2️⃣ Determinism Requirements

Semantic rule violations must:

- Use stable message strings.
- Use fixed ordering of message components.
- Avoid repr() of unordered collections.
- Avoid float formatting instability.
- Avoid time-based calculations.
- Not depend on locale.

Message text must be literal.

Field paths must be stable.

Violation hash must depend only on:

rule_id + message + field_path

---

1️⃣3️⃣ Fail-Fast Behavior

Semantic rules must not implement fail-fast internally.

They return violations.

validators.py handles fail-fast globally.

---

1️⃣4️⃣ Security Role

This layer blocks:

- Logical state contradictions.
- Invalid lifecycle states.
- Temporal paradoxes.
- Metric inconsistency exploitation.
- Status mismatch attacks.
- Flag-state misuse.

It prevents structural compliance from masking logical corruption.

---

1️⃣5️⃣ Testing Requirements

- Temporal ordering violation detection.
- Conditional required-field detection.
- Mutual exclusion conflict detection.
- Version-conditional enforcement.
- Multiple simultaneous violations.
- Strict mode interaction.
- Deterministic fingerprint equality across runs.
- High-volume nested semantic test cases.

---

1️⃣6️⃣ What Semantic Rules Must NOT Do

❌ Migrate values
❌ Normalize timestamps
❌ Coerce missing fields
❌ Add derived fields
❌ Query database
❌ Inspect lineage graph
❌ Check compatibility matrix
❌ Modify registry
❌ Consult governance locks

Those belong elsewhere.

---

1️⃣7️⃣ Absolute Definition

/data/validation/semantic_rules.py is:

> The deterministic cross-field logical enforcement layer that ensures internally
> coherent state within an artifact, rejecting structural-valid but logically
> inconsistent data before it interacts with invariant, compatibility, or lineage systems.

Structure makes something shaped. Semantics make it meaningful. This file ensures that meaning is not contradictory.
"""

from __future__ import annotations

from typing import Any, Optional, List

from .validation_contract import (
    ValidationRule,
    ValidationViolation,
    ValidationContext,
    SeverityLevel,
    ValidationScope,
    ScopeDefinition,
    compute_violation_hash,
)

# Import canonical version model for Tier-0 version comparison
from ..versioning.model import SchemaVersion


__all__ = [
    "BaseSemanticRule",
    "PublishedAfterCreationRule",
    "ExpiresAfterCreationRule",
    "StatusRequiresPublishTimeRule",
    "DeletedRequiresDeletedAtRule",
    "DeletedCannotBePublishedRule",
    "ArchivedImpliesInactiveRule",
    "FlaggedRequiresModerationLevelRule",
    "LikesLessThanOrEqualViewsRule",
    "SharesLessThanOrEqualImpressionsRule",
    "EngagementRateWithinBoundsRule",
    "RetentionRequiredInV2PlusRule",
    "DraftCannotBeArchivedRule",
    "SEMANTIC_RULES",
]


# ============================================================================
# Base Semantic Rule
# ============================================================================


class BaseSemanticRule(ValidationRule):
    """
    Base class for cross-field semantic validation rules.
    
    All semantic rules operate on relationships between fields within a single artifact.
    No cross-artifact knowledge. No system state. No lineage queries.
    Only internal logical coherence.
    
    Attributes:
        rule_id: Unique identifier for this rule
        description: Human-readable description
        severity: Severity level for violations
        applies_to: Scope definition for when rule applies
    """
    
    def evaluate(
        self,
        input_data: Any,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """
        Evaluate semantic rule against input data.
        
        Canonical interface: evaluate(input_data, context) -> ValidationViolation | None
        
        Base implementation ensures input is dict-like and delegates to _evaluate_semantic.
        
        Args:
            input_data: Data to validate (must be dict-like for semantic rules)
            context: Validation context
            
        Returns:
            ValidationViolation if rule fails, None if passes
        """
        # Semantic rules operate on dict-like structures
        if not isinstance(input_data, dict):
            # Non-dict input is structural corruption - surface as violation
            return ValidationViolation(
                rule_id=self.rule_id,
                message=f"Semantic rule requires dict input, got {type(input_data).__name__}",
                severity=SeverityLevel.ERROR,
                field_path=None,
            )
        
        return self._evaluate_semantic(input_data, context)
    
    def _evaluate_semantic(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """
        Evaluate semantic relationship between fields.
        
        Must be implemented by subclasses.
        
        Args:
            input_data: Dict-like data to validate
            context: Validation context
            
        Returns:
            ValidationViolation if rule fails, None if passes
        """
        raise NotImplementedError


# ============================================================================
# Temporal Ordering Rules
# ============================================================================


class PublishedAfterCreationRule(BaseSemanticRule):
    """
    Validates that published_at must be >= created_at.
    
    Temporal ordering correctness. Does not modify values. Does not auto-correct.
    Does not round timestamps. Does not infer missing fields.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="SEMANTIC_PUBLISH_TIME_ORDER",
            description="published_at must be >= created_at",
            severity=SeverityLevel.ERROR,
            applies_to=ScopeDefinition(
                scope=ValidationScope.ARTIFACT,
            ),
        )
    
    def _evaluate_semantic(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check temporal ordering of published_at and created_at."""
        created = input_data.get("created_at")
        published = input_data.get("published_at")
        
        # If either field is missing, let other rules handle that
        if created is None or published is None:
            return None
        
        # Both must be comparable (typically datetime or timestamp)
        try:
            if published < created:
                message = "published_at must be >= created_at"
                violation = ValidationViolation(
                    rule_id=self.rule_id,
                    message=message,
                    severity=self.severity,
                    field_path="published_at",
                )
                hash_value = compute_violation_hash(violation)
                object.__setattr__(violation, "deterministic_hash", hash_value)
                return violation
        except TypeError:
            # Incomparable types - let type rules handle that
            return None
        
        return None


class ExpiresAfterCreationRule(BaseSemanticRule):
    """
    Validates that expires_at must be > created_at.
    
    Expiration must be in the future relative to creation.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="SEMANTIC_EXPIRES_TIME_ORDER",
            description="expires_at must be > created_at",
            severity=SeverityLevel.ERROR,
            applies_to=ScopeDefinition(
                scope=ValidationScope.ARTIFACT,
            ),
        )
    
    def _evaluate_semantic(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check temporal ordering of expires_at and created_at."""
        created = input_data.get("created_at")
        expires = input_data.get("expires_at")
        
        # If either field is missing, let other rules handle that
        if created is None or expires is None:
            return None
        
        # Both must be comparable
        try:
            if expires <= created:
                message = "expires_at must be > created_at"
                violation = ValidationViolation(
                    rule_id=self.rule_id,
                    message=message,
                    severity=self.severity,
                    field_path="expires_at",
                )
                hash_value = compute_violation_hash(violation)
                object.__setattr__(violation, "deterministic_hash", hash_value)
                return violation
        except TypeError:
            # Incomparable types - let type rules handle that
            return None
        
        return None


# ============================================================================
# Conditional Presence Rules
# ============================================================================


class StatusRequiresPublishTimeRule(BaseSemanticRule):
    """
    Validates that if status == "published", then published_at must exist.
    
    Published artifacts require a publication timestamp.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="SEMANTIC_STATUS_REQUIRES_PUBLISH_TIME",
            description="published artifacts require published_at",
            severity=SeverityLevel.ERROR,
            applies_to=ScopeDefinition(
                scope=ValidationScope.ARTIFACT,
            ),
        )
    
    def _evaluate_semantic(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if published status requires published_at."""
        status = input_data.get("status")
        published = input_data.get("published_at")
        
        if status == "published" and published is None:
            message = "published artifacts require published_at"
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="published_at",
            )
            hash_value = compute_violation_hash(violation)
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


class DeletedRequiresDeletedAtRule(BaseSemanticRule):
    """
    Validates that if deleted == True, then deleted_at must exist.
    
    Deleted artifacts require a deletion timestamp.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="SEMANTIC_DELETED_REQUIRES_DELETED_AT",
            description="deleted artifacts require deleted_at",
            severity=SeverityLevel.ERROR,
            applies_to=ScopeDefinition(
                scope=ValidationScope.ARTIFACT,
            ),
        )
    
    def _evaluate_semantic(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if deleted flag requires deleted_at."""
        deleted = input_data.get("deleted")
        deleted_at = input_data.get("deleted_at")
        
        if deleted is True and deleted_at is None:
            message = "deleted artifacts require deleted_at"
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="deleted_at",
            )
            hash_value = compute_violation_hash(violation)
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


# ============================================================================
# Logical Implication Rules
# ============================================================================


class ArchivedImpliesInactiveRule(BaseSemanticRule):
    """
    Validates that if archived == True, then active == False.
    
    Archived artifacts cannot be active.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="SEMANTIC_ARCHIVED_IMPLIES_INACTIVE",
            description="archived artifacts must have active == False",
            severity=SeverityLevel.ERROR,
            applies_to=ScopeDefinition(
                scope=ValidationScope.ARTIFACT,
            ),
        )
    
    def _evaluate_semantic(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if archived implies inactive."""
        archived = input_data.get("archived")
        active = input_data.get("active")
        
        if archived is True and active is True:
            message = "archived artifacts must have active == False"
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="active",
            )
            hash_value = compute_violation_hash(violation)
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


class FlaggedRequiresModerationLevelRule(BaseSemanticRule):
    """
    Validates that if is_flagged == True, then moderation_level must exist.
    
    Flagged content requires a moderation level.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="SEMANTIC_FLAGGED_REQUIRES_MODERATION_LEVEL",
            description="flagged artifacts require moderation_level",
            severity=SeverityLevel.ERROR,
            applies_to=ScopeDefinition(
                scope=ValidationScope.ARTIFACT,
            ),
        )
    
    def _evaluate_semantic(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if flagged requires moderation level."""
        is_flagged = input_data.get("is_flagged")
        moderation_level = input_data.get("moderation_level")
        
        if is_flagged is True and moderation_level is None:
            message = "flagged artifacts require moderation_level"
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="moderation_level",
            )
            hash_value = compute_violation_hash(violation)
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


# ============================================================================
# Mutual Exclusion Rules
# ============================================================================


class DeletedCannotBePublishedRule(BaseSemanticRule):
    """
    Validates that deleted == True and status == "published" cannot coexist.
    
    This may be CRITICAL because it represents state contradiction.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="SEMANTIC_DELETED_CANNOT_BE_PUBLISHED",
            description="deleted artifacts cannot be published",
            severity=SeverityLevel.CRITICAL,
            applies_to=ScopeDefinition(
                scope=ValidationScope.ARTIFACT,
            ),
        )
    
    def _evaluate_semantic(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if deleted and published are mutually exclusive."""
        deleted = input_data.get("deleted")
        status = input_data.get("status")
        
        if deleted is True and status == "published":
            message = "deleted artifacts cannot be published"
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="status",
            )
            hash_value = compute_violation_hash(violation)
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


class DraftCannotBeArchivedRule(BaseSemanticRule):
    """
    Validates that draft == True and archived == True cannot coexist.
    
    Draft artifacts cannot be archived.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="SEMANTIC_DRAFT_CANNOT_BE_ARCHIVED",
            description="draft artifacts cannot be archived",
            severity=SeverityLevel.ERROR,
            applies_to=ScopeDefinition(
                scope=ValidationScope.ARTIFACT,
            ),
        )
    
    def _evaluate_semantic(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if draft and archived are mutually exclusive."""
        draft = input_data.get("draft")
        archived = input_data.get("archived")
        
        if draft is True and archived is True:
            message = "draft artifacts cannot be archived"
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="archived",
            )
            hash_value = compute_violation_hash(violation)
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


# ============================================================================
# Numeric Relationship Rules
# ============================================================================


class LikesLessThanOrEqualViewsRule(BaseSemanticRule):
    """
    Validates that likes <= views.
    
    Engagement metrics must be logically consistent.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="SEMANTIC_LIKES_LEQ_VIEWS",
            description="likes must be <= views",
            severity=SeverityLevel.ERROR,
            applies_to=ScopeDefinition(
                scope=ValidationScope.ARTIFACT,
            ),
        )
    
    def _evaluate_semantic(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if likes <= views."""
        likes = input_data.get("likes")
        views = input_data.get("views")
        
        # If either field is missing, let other rules handle that
        if likes is None or views is None:
            return None
        
        # Both must be numeric
        if not isinstance(likes, (int, float)) or not isinstance(views, (int, float)):
            # Wrong type - let type rules handle that
            return None
        
        if likes > views:
            message = "likes must be <= views"
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="likes",
            )
            hash_value = compute_violation_hash(violation)
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


class SharesLessThanOrEqualImpressionsRule(BaseSemanticRule):
    """
    Validates that shares <= impressions.
    
    Distribution metrics must be logically consistent.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="SEMANTIC_SHARES_LEQ_IMPRESSIONS",
            description="shares must be <= impressions",
            severity=SeverityLevel.ERROR,
            applies_to=ScopeDefinition(
                scope=ValidationScope.ARTIFACT,
            ),
        )
    
    def _evaluate_semantic(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if shares <= impressions."""
        shares = input_data.get("shares")
        impressions = input_data.get("impressions")
        
        # If either field is missing, let other rules handle that
        if shares is None or impressions is None:
            return None
        
        # Both must be numeric
        if not isinstance(shares, (int, float)) or not isinstance(impressions, (int, float)):
            # Wrong type - let type rules handle that
            return None
        
        if shares > impressions:
            message = "shares must be <= impressions"
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="shares",
            )
            hash_value = compute_violation_hash(violation)
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


class EngagementRateWithinBoundsRule(BaseSemanticRule):
    """
    Validates that engagement_rate is within [0.0, 1.0].
    
    Rate metrics must be normalized.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="SEMANTIC_ENGAGEMENT_RATE_BOUNDS",
            description="engagement_rate must be within [0.0, 1.0]",
            severity=SeverityLevel.ERROR,
            applies_to=ScopeDefinition(
                scope=ValidationScope.ARTIFACT,
            ),
        )
    
    def _evaluate_semantic(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if engagement_rate is within bounds."""
        engagement_rate = input_data.get("engagement_rate")
        
        # If field is missing, let other rules handle that
        if engagement_rate is None:
            return None
        
        # Must be numeric
        if not isinstance(engagement_rate, (int, float)):
            # Wrong type - let type rules handle that
            return None
        
        if engagement_rate < 0.0 or engagement_rate > 1.0:
            message = "engagement_rate must be within [0.0, 1.0]"
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="engagement_rate",
            )
            hash_value = compute_violation_hash(violation)
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


# ============================================================================
# Version-Conditional Rules
# ============================================================================


class RetentionRequiredInV2PlusRule(BaseSemanticRule):
    """
    Validates that in v2+, retention_rate is required when engagement_count is present.
    
    Version comparison must use pre-parsed deterministic version representation.
    Never string lexicographic comparisons in production.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="SEMANTIC_RETENTION_REQUIRED_V2_PLUS",
            description="retention_rate required when engagement_count present in v2+",
            severity=SeverityLevel.ERROR,
            applies_to=ScopeDefinition(
                scope=ValidationScope.ARTIFACT,
            ),
        )
    
    def _evaluate_semantic(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if v2+ requires retention_rate when engagement_count is present."""
        # Check version - must use canonical version object comparison, not string lexicographic
        version_str = context.artifact_version
        if version_str is None:
            return None
        
        # Parse version using canonical SchemaVersion model
        # Supports both semantic versioning (e.g., "2.0.0", "2.1.0") and integer versions (e.g., "2")
        try:
            # Try parsing as semantic version first (canonical Tier-0 approach)
            parsed_version = SchemaVersion.parse(version_str)
        except (ValueError, TypeError):
            # Fallback: try parsing as simple integer version (e.g., "2" -> 2.0.0)
            try:
                version_int = int(version_str)
                if version_int < 0:
                    return None
                # Convert integer to semantic version (e.g., "2" -> "2.0.0")
                parsed_version = SchemaVersion(major=version_int, minor=0, patch=0)
            except (ValueError, TypeError):
                # Invalid version format - cannot determine if required
                return None
        
        # Version >= 2.0.0 (canonical comparison using SchemaVersion)
        v2_minimum = SchemaVersion(major=2, minor=0, patch=0)
        if parsed_version >= v2_minimum:
            engagement_count = input_data.get("engagement_count")
            retention_rate = input_data.get("retention_rate")
            
            # If engagement_count is present, retention_rate must also be present
            if engagement_count is not None and retention_rate is None:
                message = "retention_rate required when engagement_count present in v2+"
                violation = ValidationViolation(
                    rule_id=self.rule_id,
                    message=message,
                    severity=self.severity,
                    field_path="retention_rate",
                )
                hash_value = compute_violation_hash(violation)
                object.__setattr__(violation, "deterministic_hash", hash_value)
                return violation
        
        return None


# ============================================================================
# Rule Registry
# ============================================================================

# Static rule registry
# Rules must be deterministically ordered
# This list is sorted by rule_id in validators.py, but we maintain
# a stable order here for clarity

SEMANTIC_RULES: list[ValidationRule] = [
    # Temporal ordering rules
    PublishedAfterCreationRule(),
    ExpiresAfterCreationRule(),
    
    # Conditional presence rules
    StatusRequiresPublishTimeRule(),
    DeletedRequiresDeletedAtRule(),
    
    # Logical implication rules
    ArchivedImpliesInactiveRule(),
    FlaggedRequiresModerationLevelRule(),
    
    # Mutual exclusion rules
    DeletedCannotBePublishedRule(),
    DraftCannotBeArchivedRule(),
    
    # Numeric relationship rules
    LikesLessThanOrEqualViewsRule(),
    SharesLessThanOrEqualImpressionsRule(),
    EngagementRateWithinBoundsRule(),
    
    # Version-conditional rules
    RetentionRequiredInV2PlusRule(),
]
