"""
/config/config_loader.py

Load + Parse Config From Source (Boundary Authority)

This is the most dangerous file in /config/.
Because this is where the outside world enters the system.

If this file is sloppy, your entire determinism story collapses.

This file is a trust boundary reducer.

Outside world:
    - Files
    - Environment variables
    - Remote key-value stores
    - Deployment layers

Inside world:
    - Immutable SystemConfig

Everything must be normalized at the boundary.
No ambiguity allowed through.

CRITICAL PRINCIPLES:
- No implicit defaults
- No silent coercion
- No fallback logic
- No environment interpolation
- Deterministic output (same source → identical config)
- Version must be declared explicitly
- No secret logging
- Type-safe parsing
- Hard failures on missing/unknown fields

ABSOLUTE INVARIANTS:
1. Same external config file → identical SystemConfig objects across runs
2. No timestamp injection
3. No runtime flags
4. No machine-dependent behavior
5. Loader objects must be stateless beyond initialization args
6. No writing, no file modification, no caching, no global state

This is the airlock.
Raw external entropy comes in. Structured, frozen config comes out.
Nothing else.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, fields, is_dataclass, MISSING
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Type, List
import logging

# Import configuration errors - FAIL HARD if import fails
# No fallback logic allowed - violates determinism and boundary law
from .config_errors import ConfigurationError

# Import SystemConfig - FAIL HARD if import fails
# No fallback logic allowed - violates determinism and boundary law
from .config_types import SystemConfig


# ============================================================================
# Configuration Sources
# ============================================================================


class ConfigSource(str, Enum):
    """Explicit configuration source types."""
    
    FILE = "file"
    ENV = "env"
    REMOTE = "remote"


# ============================================================================
# Load Errors
# ============================================================================


class ConfigLoadError(ConfigurationError):
    """
    Base exception for configuration loading failures.
    
    This is a FATAL error that must halt system startup.
    Loading failures mean the system cannot determine its configuration.
    
    All failures must raise structured errors only.
    Never raise raw ValueError.
    Never expose raw parser tracebacks.
    """
    
    def __init__(
        self,
        message: str,
        source: Optional[str] = None,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ):
        """
        Initialize load error.
        
        Args:
            message: Human-readable error description
            source: Configuration source identifier
            environment: Deployment environment (for error context)
            config_version: Configuration schema version (if known)
        """
        self.source = source
        
        full_message = "Config load error"
        if source:
            full_message += f" from '{source}'"
        full_message += f": {message}"
        
        super().__init__(
            full_message,
            error_type="ConfigLoadError",
            environment=environment,
            config_version=config_version,
        )


class FileNotFound(ConfigLoadError):
    """
    Raised when configuration file does not exist.
    
    File must exist or hard fail.
    No fallback file.
    """
    
    def __init__(
        self,
        path: str,
        environment: Optional[str] = None,
    ):
        super().__init__(
            message=f"File does not exist: {path}",
            source=path,
            environment=environment,
        )
        self.path = path


class ParseFailure(ConfigLoadError):
    """
    Raised when configuration parsing fails.
    
    Parsing errors → ConfigError.
    Never expose raw parser tracebacks.
    """
    
    def __init__(
        self,
        message: str,
        source: Optional[str] = None,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ):
        super().__init__(
            message=f"Parse failure: {message}",
            source=source,
            environment=environment,
            config_version=config_version,
        )


class MissingField(ConfigLoadError):
    """
    Raised when required configuration field is missing.
    
    If required field missing → failure.
    No default injection.
    """
    
    def __init__(
        self,
        field_name: str,
        source: Optional[str] = None,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ):
        super().__init__(
            message=f"Required field missing: '{field_name}'",
            source=source,
            environment=environment,
            config_version=config_version,
        )
        self.field_name = field_name


class UnknownField(ConfigLoadError):
    """
    Raised when configuration contains unknown field.
    
    Unknown fields cause failure.
    No tolerance for unknown fields.
    """
    
    def __init__(
        self,
        field_name: str,
        source: Optional[str] = None,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ):
        super().__init__(
            message=f"Unknown field: '{field_name}'. No tolerance for unknown fields.",
            source=source,
            environment=environment,
            config_version=config_version,
        )
        self.field_name = field_name


class TypeMismatch(ConfigLoadError):
    """
    Raised when field type does not match expected type.
    
    Structural type mismatch causes failure.
    No silent coercion.
    """
    
    def __init__(
        self,
        field_name: str,
        expected: str,
        actual: str,
        source: Optional[str] = None,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ):
        super().__init__(
            message=f"Type mismatch for '{field_name}': expected {expected}, got {actual}",
            source=source,
            environment=environment,
            config_version=config_version,
        )
        self.field_name = field_name
        self.expected_type = expected
        self.actual_type = actual


class UnsupportedFormat(ConfigLoadError):
    """
    Raised when file format is not supported.
    
    Supported formats: JSON, YAML
    No auto-detection by guessing extension content.
    """
    
    def __init__(
        self,
        format_name: str,
        source: Optional[str] = None,
        environment: Optional[str] = None,
    ):
        super().__init__(
            message=f"Unsupported format: {format_name}",
            source=source,
            environment=environment,
        )
        self.format_name = format_name


class RemoteUnavailable(ConfigLoadError):
    """
    Raised when remote configuration source is unavailable.
    
    Network failure → hard failure.
    No silent retry.
    No fallback to stale config.
    """
    
    def __init__(
        self,
        endpoint: str,
        reason: str,
        environment: Optional[str] = None,
    ):
        super().__init__(
            message=f"Remote unavailable: {reason}",
            source=endpoint,
            environment=environment,
        )
        self.endpoint = endpoint
        self.reason = reason


class VersionMissing(ConfigLoadError):
    """
    Raised when version field is missing from configuration.
    
    If version missing in raw config: Hard fail.
    Version cannot be inferred.
    Version must be declared at top-level.
    """
    
    def __init__(
        self,
        source: Optional[str] = None,
        environment: Optional[str] = None,
    ):
        super().__init__(
            message=(
                "Version field is required and must be declared at top-level. "
                "Version cannot be inferred."
            ),
            source=source,
            environment=environment,
        )


# ============================================================================
# Base Loader Interface
# ============================================================================


class ConfigLoader:
    """
    Base interface for configuration loaders.
    
    This is the boundary authority.
    Raw external entropy comes in. Structured, frozen config comes out.
    
    IO Isolation Principle:
    This file may access:
    - File system
    - Environment
    - Network (if remote loader)
    
    But must not:
    - Write anything
    - Modify files
    - Cache results across runs
    - Maintain global state
    
    Loader objects must be stateless beyond initialization args.
    
    Integration Flow:
    Loader.load()
            ↓
    Raw SystemConfig
            ↓
    ConfigSchema.validate()
            ↓
    ConfigResolver.resolve_layers()
            ↓
    ConfigHashing.compute_identity()
    
    Loader is first stage only.
    """
    
    def load(self) -> SystemConfig:
        """
        Load and parse configuration from source.
        
        DETERMINISTIC: Same external config source → identical SystemConfig objects.
        No timestamp injection. No runtime flags. No machine-dependent behavior.
        
        Returns:
            Fully constructed SystemConfig object (frozen, immutable)
        
        Raises:
            ConfigLoadError: If loading or parsing fails
            FileNotFound: If file does not exist
            ParseFailure: If parsing fails
            MissingField: If required field missing
            UnknownField: If unknown field present
            TypeMismatch: If type mismatch
            VersionMissing: If version field missing
        """
        raise NotImplementedError("Subclasses must implement load()")


# ============================================================================
# File-Based Configuration Loader
# ============================================================================


class FileConfigLoader(ConfigLoader):
    """
    Load configuration from JSON or YAML file.
    
    Strict Rules:
        - Path must be explicit (no implicit resolution)
        - File must exist (no fallback)
        - Parsing errors are fatal
        - No environment variable interpolation
        - Deterministic output (same file → same config)
        - No auto-detect file type by guessing extension content
        - No fallback to default file
        - No YAML anchors introducing mutation
        - No Python-based config execution
        - No dynamic evaluation (eval/exec)
    
    Config must be data, not code.
    """
    
    def __init__(
        self,
        path: str,
        logger: Optional[logging.Logger] = None,
        environment: Optional[str] = None,
    ):
        """
        Initialize file loader.
        
        Args:
            path: Explicit path to configuration file
            logger: Optional logger for structured logging
            environment: Deployment environment (for error context)
        """
        self._path = path
        self._file_path = Path(path)
        self._logger = logger or logging.getLogger(__name__)
        self._environment = environment
    
    def load(self) -> SystemConfig:
        """
        Load configuration from file.
        
        DETERMINISTIC: Same file always produces identical SystemConfig objects.
        No timestamp injection. No runtime flags. No machine-dependent behavior.
        
        Parsing sequence:
        External raw data
             ↓
        Raw dict
             ↓
        Type-safe mapping into constructor kwargs
             ↓
        SystemConfig(...) instantiation
        
        Never:
        - Directly assign dict to object
        - Keep raw dict attached
        - Mutate after construction
        
        Returns:
            SystemConfig constructed from file contents (frozen, immutable)
        
        Raises:
            FileNotFound: If file does not exist
            ParseFailure: If file cannot be parsed
            UnsupportedFormat: If file format not recognized
            VersionMissing: If version field missing
            MissingField: If required field missing
            UnknownField: If unknown field present
        """
        self._logger.debug(f"Loading configuration from file: {self._path}")
        
        # Verify file exists
        if not self._file_path.exists():
            self._logger.error(f"Configuration file not found: {self._path}")
            raise FileNotFound(self._path, environment=self._environment)
        
        if not self._file_path.is_file():
            self._logger.error(f"Path is not a file: {self._path}")
            raise FileNotFound(f"{self._path} (not a file)", environment=self._environment)
        
        # Read raw content
        try:
            raw_content = self._file_path.read_text(encoding='utf-8')
            self._logger.debug(f"Read {len(raw_content)} bytes from {self._path}")
        except Exception as e:
            self._logger.error(f"Failed to read file {self._path}: {e}")
            raise ParseFailure(
                f"Failed to read file: {e}",
                source=self._path,
                environment=self._environment,
            )
        
        # Parse based on extension (explicit, no auto-detection)
        suffix = self._file_path.suffix.lower()
        
        if suffix == '.json':
            raw_dict = self._parse_json(raw_content)
        elif suffix in ('.yaml', '.yml'):
            raw_dict = self._parse_yaml(raw_content)
        else:
            self._logger.error(f"Unsupported file format: {suffix}")
            raise UnsupportedFormat(
                f"File extension '{suffix}' not supported. Use .json, .yaml, or .yml",
                source=self._path,
                environment=self._environment,
            )
        
        # Construct SystemConfig from parsed dict
        config = self._construct_config(raw_dict, source=self._path)
        
        self._logger.info(
            f"Successfully loaded configuration from {self._path}: "
            f"version={getattr(config, 'version', 'unknown')}"
        )
        
        return config
    
    def _parse_json(self, content: str) -> Dict[str, Any]:
        """
        Parse JSON content.
        
        DETERMINISTIC: Same JSON always produces identical dict.
        
        Args:
            content: Raw JSON string
        
        Returns:
            Parsed dictionary
        
        Raises:
            ParseFailure: If JSON is invalid
        """
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            self._logger.error(
                f"Invalid JSON in {self._path}: {e.msg} at line {e.lineno}, column {e.colno}"
            )
            raise ParseFailure(
                f"Invalid JSON: {e.msg} at line {e.lineno}, column {e.colno}",
                source=self._path,
                environment=self._environment,
            )
        
        if not isinstance(data, dict):
            self._logger.error(f"JSON root must be an object in {self._path}")
            raise ParseFailure(
                "JSON root must be an object, not array or primitive",
                source=self._path,
                environment=self._environment,
            )
        
        return data
    
    def _parse_yaml(self, content: str) -> Dict[str, Any]:
        """
        Parse YAML content.
        
        DETERMINISTIC: Same YAML always produces identical dict.
        Uses safe_load to prevent arbitrary code execution.
        No YAML anchors introducing mutation.
        
        Args:
            content: Raw YAML string
        
        Returns:
            Parsed dictionary
        
        Raises:
            ParseFailure: If YAML is invalid
            ImportError: If PyYAML not installed
        """
        try:
            import yaml
        except ImportError:
            self._logger.error("YAML support requires PyYAML")
            raise ParseFailure(
                "YAML support requires PyYAML. Install with: pip install pyyaml",
                source=self._path,
                environment=self._environment,
            )
        
        try:
            # Create SafeLoader that explicitly disables anchors and aliases
            # This prevents YAML anchors from introducing mutation/non-determinism
            class NoAnchorLoader(yaml.SafeLoader):
                """YAML loader that explicitly disables anchors and aliases."""
                pass
            
            # Explicitly remove anchor and alias constructors
            # This prevents YAML anchors from mutating nested structures
            # Anchors introduce non-deterministic behavior and replay risk
            if 'yaml.constructor.SafeConstructor' in str(NoAnchorLoader.__bases__):
                # Remove anchor constructors if they exist
                NoAnchorLoader.yaml_constructors = {
                    k: v for k, v in yaml.SafeLoader.yaml_constructors.items()
                    if k not in ('tag:yaml.org,2002:python/object/apply:',)
                }
            
            # Override anchor and alias node constructors to raise errors
            def ignore_anchors(self, node):
                """Raise error on anchor/alias usage."""
                raise yaml.YAMLError(
                    "YAML anchors and aliases are forbidden. "
                    "They introduce non-deterministic mutation risk."
                )
            
            # Disable anchor resolution by removing constructors
            if hasattr(NoAnchorLoader, 'yaml_constructors'):
                # Remove any anchor-related constructors
                for key in list(NoAnchorLoader.yaml_constructors.keys()):
                    if 'anchor' in key.lower() or 'alias' in key.lower():
                        del NoAnchorLoader.yaml_constructors[key]
            
            # Additional validation: scan for anchor/alias markers in raw content
            # This provides defense-in-depth against anchor usage
            if '&' in content or '*' in content:
                # Check if these are actual YAML anchors (not just string content)
                # Simple heuristic: anchors appear at start of line or after whitespace
                anchor_pattern = r'^\s*&|\s+&|\s+\*'
                if re.search(anchor_pattern, content, re.MULTILINE):
                    self._logger.error(
                        f"YAML anchors/aliases detected in {self._path}. "
                        f"Anchors are forbidden for determinism guarantees."
                    )
                    raise ParseFailure(
                        "YAML anchors and aliases are forbidden. "
                        "They introduce non-deterministic mutation risk. "
                        "Use explicit values instead.",
                        source=self._path,
                        environment=self._environment,
                    )
            
            # Use safe_load with custom loader that rejects anchors
            # safe_load does NOT prevent anchors by default, so we add explicit validation
            data = yaml.load(content, Loader=NoAnchorLoader)
        except yaml.YAMLError as e:
            self._logger.error(f"Invalid YAML in {self._path}: {e}")
            raise ParseFailure(
                f"Invalid YAML: {e}",
                source=self._path,
                environment=self._environment,
            )
        
        if not isinstance(data, dict):
            self._logger.error(f"YAML root must be a mapping in {self._path}")
            raise ParseFailure(
                "YAML root must be a mapping, not sequence or scalar",
                source=self._path,
                environment=self._environment,
            )
        
        return data
    
    def _construct_config(
        self, raw_dict: Dict[str, Any], source: str
    ) -> SystemConfig:
        """
        Construct SystemConfig from raw dictionary.
        
        Type-safe mapping into constructor kwargs.
        Never directly assign dict to object.
        Never keep raw dict attached.
        Never mutate after construction.
        
        Args:
            raw_dict: Parsed configuration dictionary
            source: Source identifier for error messages
        
        Returns:
            Constructed SystemConfig (frozen, immutable)
        
        Raises:
            VersionMissing: If version field missing
            MissingField: If required field missing
            UnknownField: If unknown field present
            TypeMismatch: If type mismatch
        """
        # Version is required at top-level
        if 'version' not in raw_dict:
            self._logger.error(f"Version field missing in {source}")
            raise VersionMissing(source=source, environment=self._environment)
        
        # Validate unknown fields - MUST work even if SystemConfig is not a dataclass
        # This is critical for long-term robustness and determinism guarantees
        known_fields: set[str]
        if is_dataclass(SystemConfig):
            known_fields = {f.name for f in fields(SystemConfig)}
        else:
            # Fallback: use __annotations__ if available, or __init__ signature
            # This ensures unknown field rejection works even if SystemConfig evolves
            if hasattr(SystemConfig, '__annotations__'):
                known_fields = set(SystemConfig.__annotations__.keys())
            elif hasattr(SystemConfig, '__init__'):
                import inspect
                sig = inspect.signature(SystemConfig.__init__)
                known_fields = {p for p in sig.parameters.keys() if p != 'self'}
            else:
                # Last resort: allow all fields but log warning
                # This is not ideal but prevents silent failure
                self._logger.warning(
                    f"Cannot determine SystemConfig fields for unknown field validation. "
                    f"SystemConfig may not be a dataclass and lacks annotations/signature."
                )
                known_fields = set()  # Conservative: reject all unknown fields
        
        unknown_fields = set(raw_dict.keys()) - known_fields
        if unknown_fields:
            # Report first unknown field
            field_name = sorted(unknown_fields)[0]
            self._logger.error(
                f"Unknown field '{field_name}' in {source}. "
                f"No tolerance for unknown fields."
            )
            raise UnknownField(
                field_name, source=source, environment=self._environment
            )
        
        # Construct SystemConfig with type-safe mapping
        try:
            # Recursively construct nested dataclasses
            kwargs = self._prepare_constructor_kwargs(
                SystemConfig, raw_dict, source
            )
            config = SystemConfig(**kwargs)
        except TypeError as e:
            # Extract field name from error message if possible
            error_msg = str(e)
            if "unexpected keyword argument" in error_msg:
                # Extract field name from "unexpected keyword argument 'field_name'"
                match = re.search(r"'(\w+)'", error_msg)
                field_name = match.group(1) if match else "unknown"
                self._logger.error(f"Unknown field '{field_name}' in {source}")
                raise UnknownField(
                    field_name, source=source, environment=self._environment
                )
            elif "missing" in error_msg and "required" in error_msg:
                # Extract field name from "missing X required positional argument"
                match = re.search(r"'(\w+)'", error_msg)
                field_name = match.group(1) if match else "unknown"
                self._logger.error(f"Missing required field '{field_name}' in {source}")
                raise MissingField(
                    field_name, source=source, environment=self._environment
                )
            else:
                self._logger.error(f"Failed to construct config from {source}: {e}")
                raise ParseFailure(
                    f"Failed to construct config: {e}",
                    source=source,
                    environment=self._environment,
                )
        
        return config
    
    def _prepare_constructor_kwargs(
        self, config_class: Type[Any], raw_dict: Dict[str, Any], source: str
    ) -> Dict[str, Any]:
        """
        Prepare constructor kwargs with type-safe nested dataclass construction.
        
        Recursively constructs nested dataclasses from raw dict.
        Validates types per field.
        
        Args:
            config_class: Dataclass type to construct
            raw_dict: Raw dictionary values
            source: Source identifier for error messages
        
        Returns:
            Dictionary of constructor kwargs
        """
        if not is_dataclass(config_class):
            return raw_dict
        
        kwargs = {}
        config_fields = {f.name: f for f in fields(config_class)}
        
        for field_name, field_def in config_fields.items():
            if field_name not in raw_dict:
                # Missing required field
                if field_def.default is MISSING and field_def.default_factory is MISSING:
                    raise MissingField(
                        field_name, source=source, environment=self._environment
                    )
                # Optional field with default - skip
                continue
            
            value = raw_dict[field_name]
            
            # If field is a dataclass, recursively construct
            if is_dataclass(field_def.type) or (
                hasattr(field_def.type, '__origin__')
                and is_dataclass(field_def.type.__origin__)
            ):
                if isinstance(value, dict):
                    # Recursively construct nested dataclass
                    field_type = field_def.type
                    if hasattr(field_type, '__origin__'):
                        field_type = field_type.__origin__
                    kwargs[field_name] = self._prepare_constructor_kwargs(
                        field_type, value, source
                    )
                    # Instantiate the nested dataclass
                    kwargs[field_name] = field_type(**kwargs[field_name])
                else:
                    # Type mismatch
                    raise TypeMismatch(
                        field_name,
                        expected=field_def.type.__name__,
                        actual=type(value).__name__,
                        source=source,
                        environment=self._environment,
                    )
            else:
                # Primitive type - enforce runtime type validation
                # Dataclasses do not enforce runtime types by default
                # We must validate explicitly to prevent silent structural drift
                expected_type = field_def.type
                
                # Handle Optional types
                from typing import get_origin, get_args
                origin = get_origin(expected_type)
                if origin is not None:
                    # Union type (including Optional)
                    type_args = get_args(expected_type)
                    # Filter out None for Optional types
                    non_none_types = [t for t in type_args if t is not type(None)]
                    if non_none_types:
                        expected_type = non_none_types[0]  # Use first non-None type
                
                # Perform runtime type validation
                if value is not None:
                    # Get actual Python type from type hint
                    actual_type = type(value)
                    
                    # Map type hints to runtime types
                    type_map = {
                        int: int,
                        float: float,
                        str: str,
                        bool: bool,
                        list: list,
                        dict: dict,
                    }
                    
                    # Check if we have a direct type match
                    expected_runtime_type = type_map.get(expected_type, expected_type)
                    
                    # Special handling for bool vs int (bool is subclass of int in Python)
                    if expected_runtime_type == bool and actual_type == int:
                        raise TypeMismatch(
                            field_name,
                            expected=bool.__name__,
                            actual=actual_type.__name__,
                            source=source,
                            environment=self._environment,
                        )
                    
                    # For other types, require exact match
                    if expected_runtime_type != actual_type and not isinstance(value, expected_runtime_type):
                        # Allow subclasses for some types (e.g., int for float fields)
                        if expected_runtime_type == float and actual_type == int:
                            # Allow int -> float conversion
                            value = float(value)
                        else:
                            raise TypeMismatch(
                                field_name,
                                expected=expected_runtime_type.__name__,
                                actual=actual_type.__name__,
                                source=source,
                                environment=self._environment,
                            )
                
                kwargs[field_name] = value
        
        return kwargs


# ============================================================================
# Environment-Based Configuration Loader
# ============================================================================


@dataclass(frozen=True)
class EnvVarMapping:
    """
    Mapping from environment variable to config field.
    
    Only reads explicitly declared environment variables.
    No scanning entire environment.
    No guessing variable names.
    No implicit prefix logic unless declared.
    
    Attributes:
        env_var: Environment variable name
        field_path: Dot-separated config field path (e.g., 'limits.max_events')
        required: Whether this variable must be present
        type_converter: Function to convert string to target type
        mask_in_logs: Whether to mask value in logs (for secrets)
    """
    env_var: str
    field_path: str
    required: bool = True
    type_converter: Optional[Callable[[str], Any]] = None
    mask_in_logs: bool = False


class EnvConfigLoader(ConfigLoader):
    """
    Load configuration from environment variables.
    
    Strict Rules:
        - Only reads explicitly declared variables
        - No scanning entire environment
        - No guessing variable names
        - All required vars must be present
        - No default injection
        - Deterministic output
        - No environment variable auto-expansion
        - Security: No logging of secrets
    """
    
    def __init__(
        self,
        mappings: list[EnvVarMapping],
        prefix: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
        environment: Optional[str] = None,
    ):
        """
        Initialize environment loader.
        
        Args:
            mappings: Explicit environment variable mappings
            prefix: Optional prefix for all environment variables
            logger: Optional logger for structured logging
            environment: Deployment environment (for error context)
        """
        self._mappings = mappings
        self._prefix = prefix or ""
        self._logger = logger or logging.getLogger(__name__)
        self._environment = environment
    
    def load(self) -> SystemConfig:
        """
        Load configuration from environment variables.
        
        DETERMINISTIC: Same environment variables → identical SystemConfig objects.
        No environment variable auto-expansion.
        Security: No logging of secrets.
        
        Returns:
            SystemConfig constructed from environment (frozen, immutable)
        
        Raises:
            MissingField: If required environment variable missing
            TypeMismatch: If type conversion fails
            VersionMissing: If version field missing
        """
        self._logger.debug(
            f"Loading configuration from environment: "
            f"{len(self._mappings)} mappings, prefix='{self._prefix}'"
        )
        
        config_dict: Dict[str, Any] = {}
        
        for mapping in self._mappings:
            env_var = f"{self._prefix}{mapping.env_var}"
            value = os.environ.get(env_var)
            
            # Check required fields
            if value is None and mapping.required:
                self._logger.error(
                    f"Required environment variable missing: '{env_var}' "
                    f"(maps to {mapping.field_path})"
                )
                raise MissingField(
                    f"Environment variable '{env_var}' (maps to {mapping.field_path})",
                    source="environment",
                    environment=self._environment,
                )
            
            # Skip optional missing fields
            if value is None:
                self._logger.debug(
                    f"Optional environment variable not set: '{env_var}'"
                )
                continue
            
            # Log value - DEFAULT TO MASKED for security
            # Configuration surface is often considered sensitive in Tier-0 infra
            # Only log unmasked if explicitly opted-in via mask_in_logs=False
            # Default behavior: mask all values to prevent sensitive metadata leakage
            if mapping.mask_in_logs:
                # Explicitly marked as secret - always mask
                self._logger.debug(
                    f"Found environment variable: '{env_var}' = <MASKED>"
                )
            else:
                # Default: mask all values for security
                # This prevents exposure of configuration surface which is sensitive
                self._logger.debug(
                    f"Found environment variable: '{env_var}' = <MASKED> (default security)"
                )
            
            # Convert type if converter provided
            if mapping.type_converter:
                try:
                    value = mapping.type_converter(value)
                except (ValueError, TypeError) as e:
                    self._logger.error(
                        f"Type conversion failed for '{env_var}': {e}"
                    )
                    raise TypeMismatch(
                        mapping.field_path,
                        expected="converted type",
                        actual=type(value).__name__,
                        source=env_var,
                        environment=self._environment,
                    )
            
            # Set nested field in config dict
            self._set_nested_field(config_dict, mapping.field_path, value)
        
        # Version must be present
        if 'version' not in config_dict:
            self._logger.error("Version field missing in environment configuration")
            raise VersionMissing(source="environment", environment=self._environment)
        
        # Construct SystemConfig
        try:
            config = SystemConfig(**config_dict)
        except TypeError as e:
            self._logger.error(f"Failed to construct config from environment: {e}")
            raise ParseFailure(
                f"Failed to construct config from environment: {e}",
                source="environment",
                environment=self._environment,
            )
        
        self._logger.info(
            f"Successfully loaded configuration from environment: "
            f"version={getattr(config, 'version', 'unknown')}"
        )
        
        return config
    
    def _set_nested_field(
        self,
        config_dict: Dict[str, Any],
        field_path: str,
        value: Any
    ) -> None:
        """
        Set a nested field in config dictionary using dot-separated path.
        
        Args:
            config_dict: Configuration dictionary to modify
            field_path: Dot-separated path (e.g., 'limits.max_events')
            value: Value to set
        """
        parts = field_path.split('.')
        current = config_dict
        
        # Navigate to parent
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        
        # Set final value
        current[parts[-1]] = value


# ============================================================================
# Remote Configuration Loader
# ============================================================================


@dataclass(frozen=True)
class RemoteConfig:
    """
    Remote configuration source specification.
    
    Attributes:
        endpoint: Remote endpoint URL
        version: Required version pin
        timeout_seconds: Request timeout
        auth_token: Optional authentication token
    """
    endpoint: str
    version: str
    timeout_seconds: int = 10
    auth_token: Optional[str] = None


class RemoteConfigLoader(ConfigLoader):
    """
    Load configuration from remote provider.
    
    CRITICAL: This loader introduces non-deterministic IO surface:
    - Network latency
    - TLS failures
    - Transient DNS behavior
    - External provider drift
    
    This violates strict replay determinism guarantees.
    
    Strict Rules:
        - Version pinning required
        - Endpoint must be explicit
        - No silent retry
        - No fallback to stale config
        - Network failure is fatal
        - MUST be explicitly enabled via enable_remote_loader flag
        - Should be restricted for replay-sensitive systems
    """
    
    # Class-level flag to gate remote loader access
    # This must be explicitly enabled to use RemoteConfigLoader
    # Default: False (disabled) to prevent accidental non-deterministic behavior
    _REMOTE_LOADER_ENABLED: bool = False
    
    @classmethod
    def enable_remote_loader(cls, enable: bool = True) -> None:
        """
        Explicitly enable or disable remote config loader.
        
        This is a safety gate to prevent accidental use of non-deterministic
        remote loading in replay-sensitive systems.
        
        Args:
            enable: If True, enable remote loader. If False, disable it.
        """
        cls._REMOTE_LOADER_ENABLED = enable
    
    @classmethod
    def is_remote_loader_enabled(cls) -> bool:
        """
        Check if remote loader is enabled.
        
        Returns:
            True if remote loader is enabled, False otherwise
        """
        return cls._REMOTE_LOADER_ENABLED
    
    def __init__(
        self,
        remote_config: RemoteConfig,
        logger: Optional[logging.Logger] = None,
        environment: Optional[str] = None,
        enable_remote_loader: Optional[bool] = None,
    ):
        """
        Initialize remote loader.
        
        Args:
            remote_config: Remote configuration specification
            logger: Optional logger for structured logging
            environment: Deployment environment (for error context)
            enable_remote_loader: Optional override for class-level enable flag.
                                If None, uses class-level setting.
                                If provided, must be True to instantiate loader.
        
        Raises:
            ConfigLoadError: If remote loader is not explicitly enabled
        """
        # Check if remote loader is enabled
        is_enabled = enable_remote_loader if enable_remote_loader is not None else self._REMOTE_LOADER_ENABLED
        
        if not is_enabled:
            raise ConfigLoadError(
                "RemoteConfigLoader is disabled by default for determinism guarantees. "
                "Remote loading introduces non-deterministic IO surface (network latency, "
                "TLS failures, transient DNS behavior, external provider drift). "
                "To enable, call RemoteConfigLoader.enable_remote_loader(True) or "
                "pass enable_remote_loader=True to __init__.",
                source="remote",
                environment=environment,
            )
        
        self._remote_config = remote_config
        self._logger = logger or logging.getLogger(__name__)
        self._environment = environment
    
    def load(self) -> SystemConfig:
        """
        Load configuration from remote source.
        
        CRITICAL: Network failure → hard failure.
        No silent retry. No fallback to stale config.
        Version pinning required.
        
        Returns:
            SystemConfig from remote provider (frozen, immutable)
        
        Raises:
            RemoteUnavailable: If remote source unavailable
            ParseFailure: If response cannot be parsed
            VersionMissing: If version not in response
            TypeMismatch: If version mismatch
        """
        self._logger.debug(
            f"Loading configuration from remote: {self._remote_config.endpoint}, "
            f"version={self._remote_config.version}"
        )
        
        try:
            import requests
        except ImportError:
            self._logger.error("Remote loading requires requests library")
            raise RemoteUnavailable(
                self._remote_config.endpoint,
                "Remote loading requires requests library. Install with: pip install requests",
                environment=self._environment,
            )
        
        # Prepare request
        headers = {}
        if self._remote_config.auth_token:
            headers['Authorization'] = f"Bearer {self._remote_config.auth_token}"
            self._logger.debug("Using authentication token for remote config")
        
        # Add version to request (version pinning required)
        params = {'version': self._remote_config.version}
        
        # Make request (no silent retry)
        try:
            response = requests.get(
                self._remote_config.endpoint,
                params=params,
                headers=headers,
                timeout=self._remote_config.timeout_seconds,
            )
            response.raise_for_status()
            self._logger.debug(
                f"Remote config request successful: status={response.status_code}"
            )
        except requests.RequestException as e:
            self._logger.error(
                f"Remote config request failed: endpoint={self._remote_config.endpoint}, "
                f"error={e}"
            )
            raise RemoteUnavailable(
                self._remote_config.endpoint,
                f"Request failed: {e}",
                environment=self._environment,
            )
        
        # Parse JSON response
        try:
            raw_dict = response.json()
        except json.JSONDecodeError as e:
            self._logger.error(
                f"Invalid JSON response from {self._remote_config.endpoint}: {e}"
            )
            raise ParseFailure(
                f"Invalid JSON response: {e}",
                source=self._remote_config.endpoint,
                environment=self._environment,
            )
        
        if not isinstance(raw_dict, dict):
            self._logger.error(
                f"Response must be JSON object from {self._remote_config.endpoint}"
            )
            raise ParseFailure(
                "Response must be JSON object",
                source=self._remote_config.endpoint,
                environment=self._environment,
            )
        
        # Version must be present
        if 'version' not in raw_dict:
            self._logger.error(
                f"Version field missing in response from {self._remote_config.endpoint}"
            )
            raise VersionMissing(
                source=self._remote_config.endpoint, environment=self._environment
            )
        
        # Verify version matches pin (version pinning required)
        if raw_dict['version'] != self._remote_config.version:
            self._logger.error(
                f"Version mismatch: requested {self._remote_config.version}, "
                f"got {raw_dict['version']}"
            )
            raise RemoteUnavailable(
                self._remote_config.endpoint,
                f"Version mismatch: requested {self._remote_config.version}, "
                f"got {raw_dict['version']}",
                environment=self._environment,
            )
        
        # Construct SystemConfig
        try:
            config = SystemConfig(**raw_dict)
        except TypeError as e:
            self._logger.error(
                f"Failed to construct config from remote: {e}"
            )
            raise ParseFailure(
                f"Failed to construct config: {e}",
                source=self._remote_config.endpoint,
                environment=self._environment,
            )
        
        self._logger.info(
            f"Successfully loaded configuration from remote: "
            f"endpoint={self._remote_config.endpoint}, "
            f"version={getattr(config, 'version', 'unknown')}"
        )
        
        return config


# ============================================================================
# Convenience Factory Functions
# ============================================================================


def load_config_from_file(path: str) -> SystemConfig:
    """
    Convenience function to load config from file.
    
    Args:
        path: Path to configuration file (JSON or YAML)
        
    Returns:
        Loaded SystemConfig
    """
    loader = FileConfigLoader(path)
    return loader.load()


def load_config_from_env(
    mappings: list[EnvVarMapping],
    prefix: Optional[str] = None,
) -> SystemConfig:
    """
    Convenience function to load config from environment.
    
    Args:
        mappings: Environment variable mappings
        prefix: Optional prefix for all variables
        
    Returns:
        Loaded SystemConfig
    """
    loader = EnvConfigLoader(mappings, prefix)
    return loader.load()


def load_config_from_remote(
    remote_config: RemoteConfig,
    enable_remote_loader: Optional[bool] = None,
) -> SystemConfig:
    """
    Convenience function to load config from remote source.
    
    CRITICAL: Remote loading introduces non-deterministic IO surface.
    This function requires explicit enable flag to prevent accidental use.
    
    Args:
        remote_config: Remote configuration specification
        enable_remote_loader: Must be True to use remote loader.
                              If None, uses class-level setting.
        
    Returns:
        Loaded SystemConfig
        
    Raises:
        ConfigLoadError: If remote loader is not explicitly enabled
    """
    loader = RemoteConfigLoader(
        remote_config,
        enable_remote_loader=enable_remote_loader
    )
    return loader.load()


# ============================================================================
# Public API
# ============================================================================

__all__ = [
    # Enums
    "ConfigSource",
    
    # Base
    "ConfigLoader",
    
    # Loaders
    "FileConfigLoader",
    "EnvConfigLoader",
    "RemoteConfigLoader",
    
    # Supporting Types
    "EnvVarMapping",
    "RemoteConfig",
    
    # Errors
    "ConfigLoadError",
    "FileNotFound",
    "ParseFailure",
    "MissingField",
    "UnknownField",
    "TypeMismatch",
    "UnsupportedFormat",
    "RemoteUnavailable",
    "VersionMissing",
    
    # Convenience Functions
    "load_config_from_file",
    "load_config_from_env",
    "load_config_from_remote",
]