"""
/infra/config_registry.py
Versioned, Validated Configuration Authority

WHAT THIS FILE IS:
- Single source of truth for configuration reality
- Immutable registry of what configs exist and their versions
- Structural validator ensuring configuration correctness
- Deterministic resolver for reproducible runs

WHAT THIS FILE IS NOT:
- Environment variable loader (handled by bootstrap)
- YAML parser (input validation only)
- Secrets manager (security layer handles that)
- Feature flags (soft gating is feature_flags.py)
- Dynamic override engine (configs are immutable)

CORE PRINCIPLE:
Configuration is versioned data, not a suggestion.
If config is mutable → experiments are fake.
If config is implicit → audits fail.
If config is global → reproducibility dies.

DEPENDENCIES:
- Created during bootstrap
- Bound to RuntimeContext
- Read-only after initialization
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional, Tuple, List, Dict


# ============================================================================
# ENUMS (EXPLICIT)
# ============================================================================


class ConfigScope(Enum):
    """Controls who may read the config."""
    GLOBAL = "global"        # Available to all systems
    MODEL = "model"          # Model-specific configuration
    AGENT = "agent"          # Agent runtime configuration
    INFRA = "infra"          # Infrastructure-level settings
    EXPERIMENT = "experiment"  # Experimental configurations


class ConfigStability(Enum):
    """Registry enforces usage restrictions based on stability."""
    STABLE = "stable"              # Production-ready
    EXPERIMENTAL = "experimental"  # Under development
    DEPRECATED = "deprecated"      # Scheduled for removal
    FORBIDDEN = "forbidden"        # Must not be used


# ============================================================================
# SCHEMAS (STRUCTURAL CONTRACT)
# ============================================================================


@dataclass(frozen=True)
class ConfigSchema:
    """
    First-class schema definition.
    
    Schemas define the structural contract for configuration data.
    They are versioned independently and enforce type safety.
    """
    name: str
    version: str  # Semantic versioning
    fields: dict[str, str]  # field_name → type_string
    required: set[str]
    defaults: dict[str, Any]
    validators: list[str]  # Invariant rule names
    
    def __post_init__(self):
        """Validate schema structure at creation."""
        # Validate semantic version
        if not self._is_valid_semver(self.version):
            raise ValueError(f"Invalid semantic version: {self.version}")
        
        # Ensure required fields exist in fields
        missing = self.required - set(self.fields.keys())
        if missing:
            raise ValueError(f"Required fields not in schema: {missing}")
        
        # Ensure defaults are only for optional fields
        invalid_defaults = set(self.defaults.keys()) - (set(self.fields.keys()) - self.required)
        if invalid_defaults:
            raise ValueError(f"Defaults provided for required/missing fields: {invalid_defaults}")
    
    @staticmethod
    def _is_valid_semver(version: str) -> bool:
        """Validate semantic versioning format."""
        pattern = r'^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$'
        return re.match(pattern, version) is not None


@dataclass(frozen=True)
class ConfigDefinition:
    """
    Complete configuration definition. A configuration is a named, versioned, validated entity.
    Each configuration is immutable once registered.
    """
    name: str
    version: str
    schema: ConfigSchema
    scope: ConfigScope
    stability: ConfigStability
    data: dict[str, Any]
    checksum: str = field(default="", init=False)
    created_at: datetime = field(default_factory=datetime.utcnow, init=False)
    
    def __post_init__(self):
        """Validate configuration and compute checksum."""
        # Validate version matches schema version
        if self.version != self.schema.version:
            raise ValueError(
                f"Config version {self.version} doesn't match schema version {self.schema.version}"
            )
        
        # Validate all required fields are present
        missing = self.schema.required - set(self.data.keys())
        if missing:
            raise ValueError(f"Missing required fields: {missing}")
        
        # Validate no unknown fields
        unknown = set(self.data.keys()) - set(self.schema.fields.keys())
        if unknown:
            raise ValueError(f"Unknown fields not in schema: {unknown}")
        
        # Apply defaults for missing optional fields
        for field_name, default_value in self.schema.defaults.items():
            if field_name not in self.data:
                # Use object.__setattr__ since dataclass is frozen
                object.__setattr__(self.data, field_name, default_value)
        
        # Compute deterministic checksum
        object.__setattr__(self, 'checksum', self._compute_checksum())
    
    def _compute_checksum(self) -> str:
        """
        Compute deterministic SHA-256 checksum of configuration.
        
        This ensures identical configs produce identical checksums
        regardless of key ordering or timing.
        """
        # Sort keys for deterministic serialization
        sorted_data = sorted(self.data.items())
        content = f"{self.name}:{self.version}:{sorted_data}"
        return hashlib.sha256(content.encode('utf-8')).hexdigest()


# ============================================================================
# VALIDATION RULES
# ============================================================================


class ValidationRule:
    """
    Stateless validation function with metadata.
    
    Rules are pure functions that return True/False.
    They document their invariants and can be composed.
    """
    
    def __init__(
        self,
        name: str,
        predicate: Callable[[dict[str, Any]], bool],
        error_message: str,
        scope: Optional[ConfigScope] = None
    ):
        self.name = name
        self.predicate = predicate
        self.error_message = error_message
        self.scope = scope
    
    def validate(self, data: dict[str, Any]) -> tuple[bool, Optional[str]]:
        """
        Execute validation rule.
        
        Returns:
            (is_valid, error_message_or_none)
        """
        try:
            if self.predicate(data):
                return (True, None)
            else:
                return (False, self.error_message)
        except Exception as e:
            return (False, f"{self.error_message} (Exception: {str(e)})")


# ============================================================================
# BUILT-IN VALIDATION RULES
# ============================================================================


BUILTIN_RULES = {
    "positive_integer": ValidationRule(
        name="positive_integer",
        predicate=lambda d: all(
            isinstance(v, int) and v > 0 
            for v in d.values() if isinstance(v, int)
        ),
        error_message="All integer values must be positive"
    ),
    
    "non_empty_string": ValidationRule(
        name="non_empty_string",
        predicate=lambda d: all(
            isinstance(v, str) and len(v.strip()) > 0
            for v in d.values() if isinstance(v, str)
        ),
        error_message="All string values must be non-empty"
    ),
    
    "valid_path": ValidationRule(
        name="valid_path",
        predicate=lambda d: all(
            isinstance(v, str) and not v.startswith("../") and not ".." in v
            for k, v in d.items() if "path" in k.lower() and isinstance(v, str)
        ),
        error_message="Path values must not contain directory traversal"
    ),
    
    "percentage_range": ValidationRule(
        name="percentage_range",
        predicate=lambda d: all(
            isinstance(v, (int, float)) and 0 <= v <= 100
            for k, v in d.items() 
            if "percent" in k.lower() or "pct" in k.lower()
        ),
        error_message="Percentage values must be between 0 and 100"
    ),
}


# ============================================================================
# CONFIG REGISTRY
# ============================================================================


class ConfigRegistry:
    """
    Immutable registry of validated configurations.
    
    The registry is the single source of truth for:
    - What configurations exist
    - What versions are available
    - What their checksums are
    - Whether they're safe to use
    
    Registry is populated at bootstrap and frozen thereafter.
    """
    
    def __init__(self):
        self._schemas: dict[str, dict[str, ConfigSchema]] = {}  # name → version → schema
        self._configs: dict[str, dict[str, ConfigDefinition]] = {}  # name → version → config
        self._validation_rules: dict[str, ValidationRule] = BUILTIN_RULES.copy()
        self._frozen = False
    
    # ========================================================================
    # REGISTRATION
    # ========================================================================
    
    def register_schema(self, schema: ConfigSchema) -> None:
        """Register a configuration schema."""
        if self._frozen:
            raise RuntimeError("Cannot modify frozen registry")
        
        if schema.name not in self._schemas:
            self._schemas[schema.name] = {}
        
        if schema.version in self._schemas[schema.name]:
            raise ValueError(
                f"Schema {schema.name}@{schema.version} already registered"
            )
        
        # Validate that all referenced validators exist
        missing_validators = set(schema.validators) - set(self._validation_rules.keys())
        if missing_validators:
            raise ValueError(
                f"Schema references unknown validators: {missing_validators}"
            )
        
        self._schemas[schema.name][schema.version] = schema
    
    def register_config(self, config: ConfigDefinition) -> None:
        """
        Register a validated configuration.
        
        This performs full validation including schema compliance
        and custom validation rules.
        """
        if self._frozen:
            raise RuntimeError("Cannot modify frozen registry")
        
        # Ensure schema exists
        if config.schema.name not in self._schemas:
            raise ValueError(f"Schema {config.schema.name} not registered")
        
        if config.schema.version not in self._schemas[config.schema.name]:
            raise ValueError(
                f"Schema version {config.schema.name}@{config.schema.version} not registered"
            )
        
        # Run custom validation rules
        for rule_name in config.schema.validators:
            rule = self._validation_rules[rule_name]
            is_valid, error_msg = rule.validate(config.data)
            if not is_valid:
                raise ValueError(
                    f"Validation failed for {config.name}@{config.version}: {error_msg}"
                )
        
        # Check stability constraints
        if config.stability == ConfigStability.FORBIDDEN:
            raise ValueError(
                f"Cannot register FORBIDDEN configuration: {config.name}@{config.version}"
            )
        
        # Register the config
        if config.name not in self._configs:
            self._configs[config.name] = {}
        
        if config.version in self._configs[config.name]:
            raise ValueError(
                f"Config {config.name}@{config.version} already registered"
            )
        
        self._configs[config.name][config.version] = config
    
    def register_validation_rule(self, rule: ValidationRule) -> None:
        """Register a custom validation rule."""
        if self._frozen:
            raise RuntimeError("Cannot modify frozen registry")
        
        if rule.name in self._validation_rules:
            raise ValueError(f"Validation rule {rule.name} already registered")
        
        self._validation_rules[rule.name] = rule
    
    def freeze(self) -> None:
        """
        Freeze the registry, preventing further modifications.
        
        This should be called after bootstrap completes.
        """
        self._frozen = True
    
    # ========================================================================
    # LOOKUP
    # ========================================================================
    
    def get_config(
        self,
        name: str,
        version: Optional[str] = None,
        allow_experimental: bool = False
    ) -> ConfigDefinition:
        """
        Retrieve a configuration by name and optional version.
        
        Args:
            name: Configuration name
            version: Specific version (uses latest stable if None)
            allow_experimental: Whether to allow experimental configs
        
        Returns:
            ConfigDefinition
        
        Raises:
            KeyError: Configuration not found
            ValueError: Stability constraints violated
        """
        if name not in self._configs:
            raise KeyError(f"Configuration {name} not found in registry")
        
        versions = self._configs[name]
        
        # If version specified, return it directly
        if version is not None:
            if version not in versions:
                raise KeyError(f"Configuration {name}@{version} not found")
            config = versions[version]
        else:
            # Find latest stable version
            stable_versions = [
                v for v, cfg in versions.items()
                if cfg.stability == ConfigStability.STABLE
            ]
            
            if not stable_versions:
                if allow_experimental:
                    # Fall back to latest experimental
                    experimental_versions = [
                        v for v, cfg in versions.items()
                        if cfg.stability == ConfigStability.EXPERIMENTAL
                    ]
                    if not experimental_versions:
                        raise ValueError(f"No usable versions for {name}")
                    version = max(experimental_versions, key=self._semver_key)
                else:
                    raise ValueError(f"No stable versions for {name}")
            else:
                version = max(stable_versions, key=self._semver_key)
            
            config = versions[version]
        
        # Check stability constraints
        if config.stability == ConfigStability.FORBIDDEN:
            raise ValueError(f"Configuration {name}@{version} is FORBIDDEN")
        
        if config.stability == ConfigStability.DEPRECATED:
            # Log warning but allow usage
            print(f"WARNING: Configuration {name}@{version} is DEPRECATED")
        
        if config.stability == ConfigStability.EXPERIMENTAL and not allow_experimental:
            raise ValueError(
                f"Configuration {name}@{version} is EXPERIMENTAL. "
                "Set allow_experimental=True to use."
            )
        
        return config
    
    def list_configs(
        self,
        scope: Optional[ConfigScope] = None,
        stability: Optional[ConfigStability] = None
    ) -> list[str]:
        """
        List all registered configurations matching criteria.
        
        Returns:
            List of "name@version" strings
        """
        results = []
        for name, versions in self._configs.items():
            for version, config in versions.items():
                if scope and config.scope != scope:
                    continue
                if stability and config.stability != stability:
                    continue
                results.append(f"{name}@{version}")
        return sorted(results)
    
    def verify_checksum(self, name: str, version: str, expected_checksum: str) -> bool:
        """
        Verify configuration integrity via checksum.
        
        Critical for reproducibility and tamper detection.
        """
        config = self.get_config(name, version, allow_experimental=True)
        return config.checksum == expected_checksum
    
    # ========================================================================
    # UTILITIES
    # ========================================================================
    
    @staticmethod
    def _semver_key(version: str) -> tuple:
        """Convert semantic version to sortable tuple."""
        # Parse "1.2.3-beta+build" → (1, 2, 3, "beta", "build")
        match = re.match(
            r'^(\d+)\.(\d+)\.(\d+)(?:-([^+]+))?(?:\+(.+))?$',
            version
        )
        if not match:
            raise ValueError(f"Invalid semantic version: {version}")
        
        major, minor, patch, prerelease, build = match.groups()
        return (
            int(major),
            int(minor),
            int(patch),
            prerelease or "",  # Empty string sorts after None for stable
            build or ""
        )
    
    def get_snapshot(self) -> dict[str, str]:
        """
        Get complete snapshot of registry state.
        
        Returns:
            Dict mapping "name@version" → checksum
        
        This enables exact reproduction of experiments.
        """
        snapshot = {}
        for name, versions in self._configs.items():
            for version, config in versions.items():
                snapshot[f"{name}@{version}"] = config.checksum
        return snapshot


# ============================================================================
# GLOBAL REGISTRY INSTANCE
# ============================================================================

# Singleton registry instance
_REGISTRY: Optional[ConfigRegistry] = None


def get_registry() -> ConfigRegistry:
    """Get the global configuration registry."""
    global _REGISTRY
    if _REGISTRY is None:
        raise RuntimeError(
            "ConfigRegistry not initialized. "
            "This should be created during bootstrap."
        )
    return _REGISTRY


def initialize_registry() -> ConfigRegistry:
    """
    Initialize the global registry.
    
    Called once during bootstrap.
    """
    global _REGISTRY
    if _REGISTRY is not None:
        raise RuntimeError("ConfigRegistry already initialized")
    
    _REGISTRY = ConfigRegistry()
    return _REGISTRY







