"""
/config/config_resolver.py

Deterministic Configuration Layer Resolution
(Base → Env → Overrides)

This file is where configuration stops being "what was provided"
and becomes what the system will actually run with.

CRITICAL PRINCIPLES:
- Deterministic: Same inputs → byte-identical output
- Explicit: Field-by-field structured merge, not dict merge
- Immutable: No input mutation, returns new frozen object
- Type-strict: No coercion, exact type matching required
- Version-locked: All layers must have identical version
- No environment reading: Operates only on provided SystemConfig objects

ABSOLUTE INVARIANTS:
1. Same base + env + overrides → identical resolved config
2. None in higher layer does NOT erase lower layer
3. All layers must have identical version
4. No unknown fields in higher layers
5. Type must match exactly (no coercion)
6. Input objects never mutated
7. Output is new frozen SystemConfig
8. Deterministic across machines, time, processes, Python versions

Without this file:
- Nondeterministic deployments
- Replay hash drift
- Environment-specific behavior
- Hidden defaults
- Silent overrides

With this file:
- Deterministic, sealed, audit-safe configuration resolution
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, MISSING
from typing import Any, Dict, Mapping, Optional, TypeVar, Union, get_type_hints, get_origin, get_args
from types import MappingProxyType
import logging

# Import configuration errors - FAIL HARD if import fails
# No fallback logic allowed - violates determinism and boundary law
from .config_errors import ConfigurationError

# Import SystemConfig - FAIL HARD if import fails
# No fallback logic allowed - violates determinism and boundary law
from .config_types import SystemConfig


# ============================================================================
# Resolution Errors
# ============================================================================


class ConfigResolutionError(ConfigurationError):
    """
    Raised when configuration resolution fails.
    
    This is a FATAL error that must halt system startup.
    Resolution failures mean the system cannot determine its configuration.
    
    Failure conditions:
    - Version mismatch
    - Type mismatch
    - Unknown field in higher layer
    - Attempt to erase required field
    - Structural shape mismatch
    """
    
    def __init__(
        self,
        message: str,
        field_path: Optional[str] = None,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ):
        """
        Initialize resolution error.
        
        Args:
            message: Human-readable error description
            field_path: Dot-separated field path (if applicable)
            environment: Deployment environment
            config_version: Configuration schema version
        """
        self.field_path = field_path
        
        full_message = "Resolution error"
        if field_path:
            full_message += f" at '{field_path}'"
        full_message += f": {message}"
        
        super().__init__(
            full_message,
            error_type="ConfigResolutionError",
            environment=environment,
            config_version=config_version,
            failing_field=field_path,
        )


class VersionMismatch(ConfigResolutionError):
    """
    Raised when configuration layers have mismatched versions.
    
    All layers must have identical version.
    No implicit migration allowed.
    """
    
    def __init__(
        self,
        base_version: str,
        layer_version: str,
        layer_name: str,
        environment: Optional[str] = None,
    ):
        super().__init__(
            message=(
                f"Version mismatch: base={base_version}, {layer_name}={layer_version}. "
                f"All layers must have identical versions."
            ),
            field_path="version",
            environment=environment,
            config_version=base_version,
        )
        self.base_version = base_version
        self.layer_version = layer_version
        self.layer_name = layer_name


class TypeMismatch(ConfigResolutionError):
    """
    Raised when override value type does not match base type.
    
    Type strictness: No string-to-int coercion. No float-to-int correction.
    No truthy interpretation. Mismatch → fail.
    """
    
    def __init__(
        self,
        field_path: str,
        expected_type: type,
        actual_type: type,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ):
        super().__init__(
            message=(
                f"Type mismatch: expected {expected_type.__name__}, "
                f"got {actual_type.__name__}"
            ),
            field_path=field_path,
            environment=environment,
            config_version=config_version,
        )
        self.expected_type = expected_type
        self.actual_type = actual_type


class UnknownField(ConfigResolutionError):
    """
    Raised when layer contains field not present in base.
    
    If env or override contains field not present in base type:
    Hard failure. No tolerance.
    """
    
    def __init__(
        self,
        field_path: str,
        layer_name: str,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ):
        super().__init__(
            message=(
                f"Unknown field in {layer_name} layer. "
                f"Field not present in base config."
            ),
            field_path=field_path,
            environment=environment,
            config_version=config_version,
        )
        self.layer_name = layer_name


class StructuralMismatch(ConfigResolutionError):
    """
    Raised when configuration structures are incompatible.
    
    Structural shape mismatch between layers.
    """
    
    def __init__(
        self,
        field_path: str,
        message: str,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ):
        super().__init__(
            message=message,
            field_path=field_path,
            environment=environment,
            config_version=config_version,
        )


# ============================================================================
# Resolved Configuration with Provenance
# ============================================================================


@dataclass(frozen=True)
class ResolvedConfig:
    """
    Resolved configuration with optional provenance tracking.
    
    Provenance records for each field which layer provided final value.
    This is extremely valuable for audit transparency.
    
    CRITICAL: The identity hash must be computed from config only —
    never provenance.
    
    Attributes:
        config: Final resolved SystemConfig
        provenance: Immutable mapping of field_path -> source layer
    """
    config: SystemConfig
    provenance: Mapping[str, str]
    
    def __post_init__(self) -> None:
        """Ensure provenance is immutable."""
        # Convert dict to MappingProxyType if needed
        if isinstance(self.provenance, dict):
            object.__setattr__(
                self,
                'provenance',
                MappingProxyType(self.provenance)
            )


# ============================================================================
# Configuration Resolver
# ============================================================================


class ConfigResolver:
    """
    Deterministic configuration layer resolver.
    
    This is the judge. It takes multiple claims about reality
    and decides what the system will actually believe.
    
    It does not guess. It does not forgive. It does not improvise.
    It determines the final universe.
    
    Resolution Order (IMMUTABLE):
        1. Base config (required)
        2. Environment layer (optional)
        3. Explicit overrides (optional)
    
    Precedence: Overrides > Environment > Base
    Hard rule. Never reordered.
    
    DETERMINISM LAW:
    Given same base, same env layer, same override layer:
    The resolved SystemConfig must be:
    - byte-identical
    - equal under equality comparison
    - produce identical config hash
    
    Across: Machines, Time, Processes, Python patch versions
    
    No ordering leakage allowed.
    """
    
    def __init__(
        self,
        track_provenance: bool = False,
        logger: Optional[logging.Logger] = None,
    ):
        """
        Initialize resolver.
        
        Args:
            track_provenance: If True, track which layer provided each field
            logger: Optional logger for structured logging
        """
        self.track_provenance = track_provenance
        self._provenance: Dict[str, str] = {}
        self._logger = logger or logging.getLogger(__name__)
    
    def resolve(
        self,
        base: SystemConfig,
        env: Optional[SystemConfig] = None,
        overrides: Optional[SystemConfig] = None,
        environment: Optional[str] = None,
    ) -> SystemConfig:
        """
        Resolve configuration layers into single canonical config.
        
        DETERMINISTIC: Same inputs always produce identical output.
        No time/env/RNG dependence.
        
        Rules:
        - Base is mandatory
        - Env and overrides optional
        - Returns brand new frozen SystemConfig
        - Never mutates input
        
        Args:
            base: Base configuration (required)
            env: Environment-specific configuration (optional)
            overrides: Explicit override configuration (optional)
            environment: Deployment environment (for error context)
            
        Returns:
            Fully resolved SystemConfig (new frozen object)
            
        Raises:
            ConfigResolutionError: If resolution fails
            VersionMismatch: If layers have different versions
            TypeMismatch: If override type doesn't match base
            UnknownField: If layer has field not in base
            StructuralMismatch: If structures are incompatible
            
        Critical Invariants:
            - Never mutates inputs
            - Deterministic (same inputs → identical output)
            - Returns new frozen object
            - No partial resolution allowed
        """
        self._logger.debug(
            f"Resolving configuration: has_env={env is not None}, "
            f"has_overrides={overrides is not None}, environment={environment}"
        )
        
        # Validate inputs are frozen (if they're dataclasses)
        if is_dataclass(base):
            if not base.__dataclass_params__.frozen:
                raise ConfigResolutionError(
                    message="Base config must be frozen dataclass",
                    environment=environment,
                )
        
        if env is not None and is_dataclass(env):
            if not env.__dataclass_params__.frozen:
                raise ConfigResolutionError(
                    message="Environment config must be frozen dataclass",
                    environment=environment,
                )
        
        if overrides is not None and is_dataclass(overrides):
            if not overrides.__dataclass_params__.frozen:
                raise ConfigResolutionError(
                    message="Override config must be frozen dataclass",
                    environment=environment,
                )
        
        # Reset provenance tracking
        if self.track_provenance:
            self._provenance = {}
        
        # Enforce version locking across all layers
        base_version = self._validate_versions(
            base, env, overrides, environment
        )
        
        # Resolve recursively
        resolved = self._resolve_recursive(
            base=base,
            env=env,
            overrides=overrides,
            field_path="",
            environment=environment,
            config_version=base_version,
        )
        
        self._logger.info(
            f"Configuration resolved successfully: "
            f"version={base_version}, environment={environment}"
        )
        
        return resolved
    
    def resolve_with_provenance(
        self,
        base: SystemConfig,
        env: Optional[SystemConfig] = None,
        overrides: Optional[SystemConfig] = None,
        environment: Optional[str] = None,
    ) -> ResolvedConfig:
        """
        Resolve configuration and return with provenance tracking.
        
        Provenance records which layer provided each field value.
        This is extremely valuable for audit transparency.
        
        CRITICAL: The identity hash must be computed from config only —
        never provenance.
        
        Args:
            base: Base configuration (required)
            env: Environment-specific configuration (optional)
            overrides: Explicit override configuration (optional)
            environment: Deployment environment (for error context)
        
        Returns:
            ResolvedConfig with config and provenance mapping
        """
        # Enable provenance tracking
        original_tracking = self.track_provenance
        self.track_provenance = True
        
        try:
            config = self.resolve(base, env, overrides, environment=environment)
            return ResolvedConfig(
                config=config,
                provenance=dict(self._provenance)
            )
        finally:
            self.track_provenance = original_tracking
    
    def _validate_versions(
        self,
        base: SystemConfig,
        env: Optional[SystemConfig],
        overrides: Optional[SystemConfig],
        environment: Optional[str] = None,
    ) -> str:
        """
        Validate all layers have identical version.
        
        Version Locking: All layers must have identical version.
        If versions mismatch: Hard failure. No implicit migration.
        
        Args:
            base: Base configuration
            env: Environment configuration (optional)
            overrides: Override configuration (optional)
            environment: Deployment environment (for error context)
        
        Returns:
            Base version string
        
        Raises:
            VersionMismatch: If versions differ
        """
        # Check if config has version field
        if not hasattr(base, 'version'):
            raise ConfigResolutionError(
                message="Base config missing version field (required for version locking)",
                field_path="version",
                environment=environment,
            )
        
        base_version = getattr(base, 'version', None)
        if base_version is None or base_version == "":
            raise ConfigResolutionError(
                message="Base config version is None or empty (required for version locking)",
                field_path="version",
                environment=environment,
            )
        
        # Validate env layer version
        if env is not None:
            if not hasattr(env, 'version'):
                raise ConfigResolutionError(
                    message="Environment config missing version field",
                    field_path="version",
                    environment=environment,
                    config_version=base_version,
                )
            
            env_version = getattr(env, 'version', None)
            if env_version is not None and env_version != base_version:
                self._logger.error(
                    f"Version mismatch: base={base_version}, env={env_version}"
                )
                raise VersionMismatch(
                    base_version, env_version, "environment", environment
                )
        
        # Validate overrides layer version
        if overrides is not None:
            if not hasattr(overrides, 'version'):
                raise ConfigResolutionError(
                    message="Override config missing version field",
                    field_path="version",
                    environment=environment,
                    config_version=base_version,
                )
            
            override_version = getattr(overrides, 'version', None)
            if override_version is not None and override_version != base_version:
                self._logger.error(
                    f"Version mismatch: base={base_version}, "
                    f"overrides={override_version}"
                )
                raise VersionMismatch(
                    base_version, override_version, "overrides", environment
                )
        
        self._logger.debug(f"Version validation passed: version={base_version}")
        return base_version
    
    def _resolve_recursive(
        self,
        base: Any,
        env: Optional[Any],
        overrides: Optional[Any],
        field_path: str,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ) -> Any:
        """
        Recursively resolve configuration objects.
        
        DETERMINISTIC: Field-by-field structured merge, not dict merge.
        Iterates over dataclass fields explicitly.
        Resolves nested dataclasses recursively.
        Validates types per field.
        Reconstructs new dataclass.
        
        NOT ALLOWED:
        - Convert to dict and deep merge via generic recursion
        - Rely on __dict__ introspection dynamically
        - JSON dump and merge
        - Use third-party deep merge libraries
        
        Args:
            base: Base value (required)
            env: Environment value (optional)
            overrides: Override value (optional)
            field_path: Current field path for error reporting
            environment: Deployment environment (for error context)
            config_version: Configuration schema version (for error context)
            
        Returns:
            Resolved value (new frozen object)
        """
        # If base is not a dataclass, use simple value resolution
        if not is_dataclass(base):
            return self._resolve_value(
                base, env, overrides, field_path, environment, config_version
            )
        
        # Base is a dataclass - resolve field by field
        # Preserve declared order for deterministic processing
        base_field_list = list(fields(base))
        base_fields = {f.name: f for f in base_field_list}
        resolved_fields = {}
        
        # Explicitly reject non-dataclass higher-layer objects when base is dataclass
        if env is not None:
            if not is_dataclass(env):
                raise StructuralMismatch(
                    field_path=field_path,
                    message=(
                        f"Environment layer must be dataclass when base is dataclass. "
                        f"Got type: {type(env).__name__}"
                    ),
                    environment=environment,
                    config_version=config_version,
                )
            env_field_names = {f.name for f in fields(env)}
            unknown_env_fields = env_field_names - base_fields.keys()
            if unknown_env_fields:
                for unknown_field in sorted(unknown_env_fields):
                    current_path = (
                        f"{field_path}.{unknown_field}" if field_path else unknown_field
                    )
                    raise UnknownField(
                        current_path, "environment", environment, config_version
                    )
        
        if overrides is not None:
            if not is_dataclass(overrides):
                raise StructuralMismatch(
                    field_path=field_path,
                    message=(
                        f"Override layer must be dataclass when base is dataclass. "
                        f"Got type: {type(overrides).__name__}"
                    ),
                    environment=environment,
                    config_version=config_version,
                )
            override_field_names = {f.name for f in fields(overrides)}
            unknown_override_fields = override_field_names - base_fields.keys()
            if unknown_override_fields:
                for unknown_field in sorted(unknown_override_fields):
                    current_path = (
                        f"{field_path}.{unknown_field}" if field_path else unknown_field
                    )
                    raise UnknownField(
                        current_path, "overrides", environment, config_version
                    )
        
        # Resolve each field deterministically (sorted order)
        for field_def in base_field_list:
            field_name = field_def.name
            current_path = f"{field_path}.{field_name}" if field_path else field_name
            
            # Get values from each layer
            base_value = getattr(base, field_name)
            env_value = (
                getattr(env, field_name, None)
                if env is not None and is_dataclass(env) and hasattr(env, field_name)
                else None
            )
            override_value = (
                getattr(overrides, field_name, None)
                if overrides is not None
                and is_dataclass(overrides)
                and hasattr(overrides, field_name)
                else None
            )
            
            # Resolve this field recursively
            resolved_value = self._resolve_field(
                field_name=field_name,
                field_path=current_path,
                base_value=base_value,
                env_value=env_value,
                override_value=override_value,
                field_def=field_def,
                base_dataclass=type(base),
                environment=environment,
                config_version=config_version,
            )
            
            resolved_fields[field_name] = resolved_value
        
        # Reconstruct dataclass with resolved fields
        # This creates a new frozen object
        return type(base)(**resolved_fields)
    
    def _resolve_field(
        self,
        field_name: str,
        field_path: str,
        base_value: Any,
        env_value: Optional[Any],
        override_value: Optional[Any],
        field_def: Optional[Any] = None,
        base_dataclass: Optional[type] = None,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ) -> Any:
        """
        Resolve a single field following precedence rules.
        
        Precedence: Overrides > Environment > Base
        Hard rule. Never reordered.
        
        Rule 1: Explicit Values Only
        A value may override only if explicitly set.
        A "None" in higher layer MUST NOT erase lower layer.
        
        Example:
        Base.logging.level = "INFO"
        Env.logging.level = None
        Result: "INFO" (env did not explicitly change)
        
        Args:
            field_name: Field name
            field_path: Dot-separated field path
            base_value: Base layer value (required)
            env_value: Environment layer value (optional)
            override_value: Override layer value (optional)
            environment: Deployment environment (for error context)
            config_version: Configuration schema version (for error context)
        
        Returns:
            Resolved field value
        """
        # Validate None values in override layers are only allowed for Optional fields
        # This prevents schema drift where None is set in higher layers for non-Optional fields
        if override_value is None and base_value is not None:
            is_optional = self._check_field_optionality(
                field_def, base_dataclass, field_path, environment, config_version
            )
            if not is_optional:
                raise ConfigResolutionError(
                    message=(
                        f"Cannot set None in override layer for non-Optional field. "
                        f"Field '{field_path}' is not Optional."
                    ),
                    field_path=field_path,
                    environment=environment,
                    config_version=config_version,
                )
        
        if env_value is None and base_value is not None:
            is_optional = self._check_field_optionality(
                field_def, base_dataclass, field_path, environment, config_version
            )
            if not is_optional:
                raise ConfigResolutionError(
                    message=(
                        f"Cannot set None in environment layer for non-Optional field. "
                        f"Field '{field_path}' is not Optional."
                    ),
                    field_path=field_path,
                    environment=environment,
                    config_version=config_version,
                )
        
        # Determine final value following precedence
        # Explicit override takes precedence (if not None)
        if override_value is not None:
            final_value = override_value
            source = "overrides"
        # Environment layer next (if not None)
        elif env_value is not None:
            final_value = env_value
            source = "environment"
        # Base layer fallback
        else:
            final_value = base_value
            source = "base"
        
        # Track provenance if enabled
        if self.track_provenance:
            self._provenance[field_path] = source
        
        # Type validation - ensure override matches base type
        # Only validate if value changed from base
        if final_value is not base_value:
            self._validate_type_match(
                field_path=field_path,
                base_value=base_value,
                override_value=final_value,
                field_def=field_def,
                base_dataclass=base_dataclass,
                environment=environment,
                config_version=config_version,
            )
        
        # If value is a dataclass, resolve recursively
        if is_dataclass(final_value):
            # Determine which layers to pass down for recursive resolution
            # Use identity comparison (is/is not) instead of equality to avoid
            # skipping nested overrides when values are equal by equality but not identity
            recursive_base = base_value if is_dataclass(base_value) else final_value
            recursive_env = (
                env_value
                if env_value is not None
                and is_dataclass(env_value)
                and env_value is not final_value
                else None
            )
            recursive_overrides = (
                override_value
                if override_value is not None
                and is_dataclass(override_value)
                and override_value is not final_value
                else None
            )
            
            return self._resolve_recursive(
                base=recursive_base,
                env=recursive_env,
                overrides=recursive_overrides,
                field_path=field_path,
                environment=environment,
                config_version=config_version,
            )
        
        # For non-dataclass values, also need to handle None override validation
        # when override_value is None but base_value is not
        if override_value is None and base_value is not None:
            # Check if field is actually Optional
            is_optional = self._check_field_optionality(
                field_def, base_dataclass, field_path, environment, config_version
            )
            
            if not is_optional:
                raise ConfigResolutionError(
                    message=(
                        f"Cannot override non-None value with None. "
                        f"Field '{field_path}' is not Optional."
                    ),
                    field_path=field_path,
                    environment=environment,
                    config_version=config_version,
                )
        
        return final_value
    
    def _resolve_value(
        self,
        base: Any,
        env: Optional[Any],
        overrides: Optional[Any],
        field_path: str,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ) -> Any:
        """
        Resolve a simple (non-dataclass) value.
        
        Precedence: Overrides > Environment > Base
        None does not erase lower layers.
        
        Rule: Explicit Values Only
        A value may override only if explicitly set.
        A "None" in higher layer MUST NOT erase lower layer.
        
        Args:
            base: Base value (required)
            env: Environment value (optional)
            overrides: Override value (optional)
            field_path: Current field path for error reporting
            environment: Deployment environment (for error context)
            config_version: Configuration schema version (for error context)
        
        Returns:
            Resolved value
        """
        # Explicit override takes precedence (if not None)
        if overrides is not None:
            if self.track_provenance:
                self._provenance[field_path] = "overrides"
            return overrides
        
        # Environment layer next (if not None)
        if env is not None:
            if self.track_provenance:
                self._provenance[field_path] = "environment"
            return env
        
        # Base layer fallback
        if self.track_provenance:
            self._provenance[field_path] = "base"
        return base
    
    def _is_optional_type(self, field_type: Any) -> bool:
        """
        Check if a type hint represents an Optional type.
        
        Args:
            field_type: Type hint to check
            
        Returns:
            True if the type is Optional (Union[T, None] or Optional[T])
        """
        # Handle direct Optional type
        if field_type is type(None):
            return True
        
        # Get origin for Union types (Optional[T] is Union[T, None])
        origin = get_origin(field_type)
        if origin is Union:
            args = get_args(field_type)
            # Check if None is in the union args
            return type(None) in args
        
        return False
    
    def _check_field_optionality(
        self,
        field_def: Optional[Any],
        base_dataclass: Optional[type],
        field_path: str,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ) -> bool:
        """
        Check if a field is Optional by inspecting its type hint.
        
        Args:
            field_def: Field definition
            base_dataclass: Base dataclass type
            field_path: Field path for error context
            environment: Deployment environment (for error context)
            config_version: Configuration schema version (for error context)
            
        Returns:
            True if field is Optional, False otherwise
        """
        if field_def is None or base_dataclass is None:
            return False
        
        try:
            type_hints = get_type_hints(base_dataclass)
            field_type_hint = type_hints.get(field_def.name)
            if field_type_hint is not None:
                return self._is_optional_type(field_type_hint)
        except Exception:
            # If we can't get type hints, be conservative and assume not optional
            return False
        
        return False
    
    def _validate_type_match(
        self,
        field_path: str,
        base_value: Any,
        override_value: Any,
        field_def: Optional[Any] = None,
        base_dataclass: Optional[type] = None,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ) -> None:
        """
        Validate override value type matches base value type exactly.
        
        Type Strictness: Override type must match exactly.
        No string-to-int coercion. No float-to-int correction.
        No truthy interpretation. Mismatch → fail.
        
        Args:
            field_path: Dot-separated field path
            base_value: Base layer value
            override_value: Override layer value
            field_def: Field definition (for checking Optional type)
            base_dataclass: Base dataclass type (for getting type hints)
            environment: Deployment environment (for error context)
            config_version: Configuration schema version (for error context)
        
        Raises:
            TypeMismatch: If types don't match exactly
            ConfigResolutionError: If None override attempted on non-optional field
        """
        base_type = type(base_value)
        override_type = type(override_value)
        
        # Both None is OK
        if base_value is None and override_value is None:
            return
        
        # One None, one not - verify field optionality before allowing
        if base_value is None or override_value is None:
            # Check if field is actually Optional by inspecting type hints
            is_optional = self._check_field_optionality(
                field_def, base_dataclass, field_path, environment, config_version
            )
            
            if base_value is not None and override_value is None:
                # Override is None, base is not - only allow if field is Optional
                if not is_optional:
                    raise ConfigResolutionError(
                        message=(
                            f"Cannot override non-None value with None. "
                            f"Field '{field_path}' is not Optional."
                        ),
                        field_path=field_path,
                        environment=environment,
                        config_version=config_version,
                    )
                return
            
            if override_value is not None and base_value is None:
                # Base is None, override is not - validate override type is acceptable
                # This is allowed (optional field being set)
                return
        
        # For dataclasses, check if they're the same class
        if is_dataclass(base_value) and is_dataclass(override_value):
            if type(base_value) != type(override_value):
                self._logger.error(
                    f"Type mismatch: path={field_path}, "
                    f"base_type={base_type.__name__}, "
                    f"override_type={override_type.__name__}"
                )
                raise TypeMismatch(
                    field_path, base_type, override_type, environment, config_version
                )
            return
        
        # For primitives, types must match exactly (use type() == type(), not isinstance)
        # Special case: bool is subclass of int, but we treat them as distinct
        if base_type == bool or override_type == bool:
            if base_type != override_type:
                self._logger.error(
                    f"Type mismatch (bool/int): path={field_path}, "
                    f"base_type={base_type.__name__}, "
                    f"override_type={override_type.__name__}"
                )
                raise TypeMismatch(
                    field_path, base_type, override_type, environment, config_version
                )
            return
        
        # For other types, exact type equality required (not isinstance)
        if base_type != override_type:
            self._logger.error(
                f"Type mismatch: path={field_path}, "
                f"base_type={base_type.__name__}, "
                f"override_type={override_type.__name__}"
            )
            raise TypeMismatch(
                field_path, base_type, override_type, environment, config_version
            )


# ============================================================================
# Convenience Functions
# ============================================================================


def resolve_config(
    base: SystemConfig,
    env: Optional[SystemConfig] = None,
    overrides: Optional[SystemConfig] = None,
    environment: Optional[str] = None,
) -> SystemConfig:
    """
    Convenience function for config resolution.
    
    DETERMINISTIC: Same inputs always produce identical output.
    
    Args:
        base: Base configuration (required)
        env: Environment configuration (optional)
        overrides: Override configuration (optional)
        environment: Deployment environment (for error context)
    
    Returns:
        Resolved SystemConfig (new frozen object)
    """
    resolver = ConfigResolver()
    return resolver.resolve(base, env, overrides, environment=environment)


def resolve_config_with_provenance(
    base: SystemConfig,
    env: Optional[SystemConfig] = None,
    overrides: Optional[SystemConfig] = None,
    environment: Optional[str] = None,
) -> ResolvedConfig:
    """
    Convenience function for config resolution with provenance.
    
    Provenance records which layer provided each field value.
    This is extremely valuable for audit transparency.
    
    CRITICAL: The identity hash must be computed from config only —
    never provenance.
    
    Args:
        base: Base configuration (required)
        env: Environment configuration (optional)
        overrides: Override configuration (optional)
        environment: Deployment environment (for error context)
    
    Returns:
        ResolvedConfig with config and provenance mapping
    """
    resolver = ConfigResolver(track_provenance=True)
    return resolver.resolve_with_provenance(base, env, overrides, environment=environment)


# ============================================================================
# Public API
# ============================================================================

__all__ = [
    "ConfigResolver",
    "ResolvedConfig",
    "ConfigResolutionError",
    "VersionMismatch",
    "TypeMismatch",
    "UnknownField",
    "StructuralMismatch",
    "resolve_config",
    "resolve_config_with_provenance",
]