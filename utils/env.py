"""
Explicit Environment Introspection

Controlled boundary between runtime environment and deterministic system core.
Environment variables read once, validated, and frozen. No ambient state leakage.

CORE LAW: Environment variables may only be read once, validated once, and frozen.
After freeze: No further reads from os.environ, no mutation, no dynamic lookup.

REPLAY SAFETY: load_environment() blocked during replay context.
Use load_environment_for_replay() for deterministic replay execution.
"""

import os
from dataclasses import dataclass
from typing import Optional

from utils.errors import GuardViolation, EnvironmentConfigurationError


__all__ = [
    'Environment',
    'load_environment',
    'require_env_var',
    'get_env_bool',
    'get_env_int',
    'freeze_environment',
    'get_frozen_environment',
    'is_environment_frozen',
]


# ============================================================================
# GLOBAL FREEZE STATE
# ============================================================================

_frozen_environment: Optional['Environment'] = None
_environment_frozen: bool = False
_replay_context_active: bool = False


# ============================================================================
# ENVIRONMENT MODEL
# ============================================================================

@dataclass(frozen=True)
class Environment:
    """Immutable environment snapshot."""
    environment_name: str
    debug: bool
    region: str
    timezone: str
    replay_mode: bool
    service_version: str
    
    def __post_init__(self):
        """Validate all fields are non-empty where required."""
        if not self.environment_name or not self.environment_name.strip():
            raise EnvironmentConfigurationError(
                "environment_name cannot be empty",
                code="ENV_EMPTY_FIELD",
                details={"field": "environment_name"}
            )
        
        if not self.region or not self.region.strip():
            raise EnvironmentConfigurationError(
                "region cannot be empty",
                code="ENV_EMPTY_FIELD",
                details={"field": "region"}
            )
        
        if not self.timezone or not self.timezone.strip():
            raise EnvironmentConfigurationError(
                "timezone cannot be empty",
                code="ENV_EMPTY_FIELD",
                details={"field": "timezone"}
            )
        
        if not self.service_version or not self.service_version.strip():
            raise EnvironmentConfigurationError(
                "service_version cannot be empty",
                code="ENV_EMPTY_FIELD",
                details={"field": "service_version"}
            )
    
    def to_dict(self) -> dict:
        """Serialize to canonical dictionary."""
        return {
            'environment_name': self.environment_name,
            'debug': self.debug,
            'region': self.region,
            'timezone': self.timezone,
            'replay_mode': self.replay_mode,
            'service_version': self.service_version,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Environment':
        """Deserialize from canonical dictionary."""
        return cls(
            environment_name=data['environment_name'],
            debug=data['debug'],
            region=data['region'],
            timezone=data['timezone'],
            replay_mode=data['replay_mode'],
            service_version=data['service_version'],
        )


# ============================================================================
# ENVIRONMENT VARIABLE READERS
# ============================================================================

def require_env_var(name: str) -> str:
    """
    Read required environment variable.
    
    Guarantees variable exists, is non-empty, and not whitespace-only.
    Failure is hard stop.
    """
    if not isinstance(name, str):
        raise TypeError(f"Environment variable name must be str, got {type(name).__name__}")
    
    if not name.strip():
        raise ValueError("Environment variable name cannot be empty")
    
    if _environment_frozen:
        raise GuardViolation(
            "Cannot read environment variables after freeze. "
            "Environment must be loaded before freeze.",
            code="ENV_READ_AFTER_FREEZE"
        )
    
    if name not in os.environ:
        raise EnvironmentConfigurationError(
            f"Required environment variable '{name}' not found. "
            f"Environment access must be explicit.",
            code="ENV_MISSING_VAR",
            details={"var_name": name}
        )
    
    value = os.environ[name].strip()
    
    if not value:
        raise EnvironmentConfigurationError(
            f"Required environment variable '{name}' is empty or whitespace-only. "
            f"Invalid configuration.",
            code="ENV_EMPTY_VAR",
            details={"var_name": name}
        )
    
    return value


def get_env_bool(name: str) -> bool:
    """
    Read boolean environment variable with strict validation.
    
    Acceptable values ONLY (case-insensitive): "true" or "false".
    Ambiguity forbidden (no "1", "0", "yes", "no", "on", "off").
    """
    value = require_env_var(name)
    normalized = value.lower().strip()
    
    if normalized == 'true':
        return True
    elif normalized == 'false':
        return False
    else:
        raise EnvironmentConfigurationError(
            f"Environment variable '{name}' must be 'true' or 'false' "
            f"(case-insensitive), got: '{value}'. "
            f"Ambiguous boolean values are forbidden.",
            code="ENV_INVALID_BOOL",
            details={"var_name": name, "value": value}
        )


def get_env_int(name: str) -> int:
    """
    Read integer environment variable with strict validation.
    
    Decimal integers only. No floats, hex, or underscore formatting.
    """
    value = require_env_var(name)
    
    # Reject hex
    if value.lower().startswith('0x'):
        raise EnvironmentConfigurationError(
            f"Environment variable '{name}' contains hex notation: '{value}'. "
            f"Only decimal integers allowed.",
            code="ENV_INVALID_INT_HEX",
            details={"var_name": name, "value": value}
        )
    
    # Reject underscore formatting
    if '_' in value:
        raise EnvironmentConfigurationError(
            f"Environment variable '{name}' contains underscores: '{value}'. "
            f"Only plain decimal integers allowed.",
            code="ENV_INVALID_INT_UNDERSCORE",
            details={"var_name": name, "value": value}
        )
    
    # Reject floats
    if '.' in value:
        raise EnvironmentConfigurationError(
            f"Environment variable '{name}' contains decimal point: '{value}'. "
            f"Only integers allowed.",
            code="ENV_INVALID_INT_FLOAT",
            details={"var_name": name, "value": value}
        )
    
    try:
        return int(value)
    except ValueError as e:
        raise EnvironmentConfigurationError(
            f"Environment variable '{name}' is not a valid integer: '{value}'",
            code="ENV_INVALID_INT",
            details={"var_name": name, "value": value}
        ) from e


# ============================================================================
# ENVIRONMENT LOADING
# ============================================================================

def load_environment() -> Environment:
    """
    Load and validate environment configuration from os.environ.
    
    Required vars: SERVICE_ENV, SERVICE_REGION, SERVICE_VERSION, SERVICE_TIMEZONE,
    SERVICE_DEBUG, SERVICE_REPLAY. All validated and normalized.
    """
    global _replay_context_active
    
    if _environment_frozen:
        raise GuardViolation(
            "Environment already frozen. Cannot load environment multiple times. "
            "Use get_frozen_environment() to access existing environment.",
            code="ENV_ALREADY_FROZEN"
        )
    
    if _replay_context_active:
        raise GuardViolation(
            "Cannot load live environment during replay. "
            "Use load_environment_for_replay() instead.",
            code="ENV_REPLAY_CONTEXT_ACTIVE"
        )
    
    # Read all required variables with validation
    environment_name = require_env_var('SERVICE_ENV')
    region = require_env_var('SERVICE_REGION')
    service_version = require_env_var('SERVICE_VERSION')
    timezone = require_env_var('SERVICE_TIMEZONE')
    
    # Validate timezone
    validate_timezone(timezone)
    
    debug = get_env_bool('SERVICE_DEBUG')
    replay_mode = get_env_bool('SERVICE_REPLAY')
    
    # Validate combinations (reject invalid critical combinations)
    if replay_mode and debug:
        raise EnvironmentConfigurationError(
            "Invalid configuration: replay_mode and debug cannot both be true. "
            "Replay must run in production mode for determinism.",
            code="ENV_INVALID_COMBINATION",
            details={"replay_mode": str(replay_mode), "debug": str(debug)}
        )
    
    # Create immutable environment
    env = Environment(
        environment_name=environment_name,
        debug=debug,
        region=region,
        timezone=timezone,
        replay_mode=replay_mode,
        service_version=service_version,
    )
    
    return env


def load_environment_for_replay(
    environment_name: str,
    region: str,
    timezone: str,
    service_version: str,
) -> Environment:
    """
    Load synthetic environment for replay mode.
    
    Reconstructs exact execution environment without reading live os.environ.
    Returns Environment with replay_mode=True, debug=False.
    """
    global _replay_context_active
    
    if _environment_frozen:
        raise GuardViolation(
            "Environment already frozen. Cannot load replay environment.",
            code="ENV_ALREADY_FROZEN"
        )
    
    # Mark replay context as active to prevent accidental live loads
    _replay_context_active = True
    
    # Validate timezone
    validate_timezone(timezone)
    
    # Replay always has: replay_mode=True, debug=False
    env = Environment(
        environment_name=environment_name,
        debug=False,
        region=region,
        timezone=timezone,
        replay_mode=True,
        service_version=service_version,
    )
    
    return env


# ============================================================================
# FREEZE ENFORCEMENT
# ============================================================================

def freeze_environment(env: Environment) -> None:
    """
    Freeze environment to prevent further loading.
    
    After freeze: No further load_environment() calls allowed.
    Environment becomes immutable system context.
    """
    global _frozen_environment, _environment_frozen
    
    if _environment_frozen:
        raise GuardViolation(
            "Environment already frozen. Cannot freeze multiple times.",
            code="ENV_ALREADY_FROZEN"
        )
    
    if not isinstance(env, Environment):
        raise TypeError(
            f"Expected Environment instance, got {type(env).__name__}"
        )
    
    _frozen_environment = env
    _environment_frozen = True


def get_frozen_environment() -> Environment:
    """Get frozen environment instance."""
    if not _environment_frozen:
        raise GuardViolation(
            "Environment not frozen. Call freeze_environment() first.",
            code="ENV_NOT_FROZEN"
        )
    
    return _frozen_environment


def is_environment_frozen() -> bool:
    """Check if environment is frozen."""
    return _environment_frozen


def _enable_os_environ_guard() -> None:
    """
    Optional: Monkeypatch os.environ to block access after freeze.
    
    Provides hard sandboxing to prevent ambient state leakage.
    Must be called after freeze_environment(). Modifies global os.environ.
    """
    if not _environment_frozen:
        raise GuardViolation(
            "Cannot enable os.environ guard before environment is frozen.",
            code="ENV_GUARD_BEFORE_FREEZE"
        )
    
    _original_environ = os.environ
    
    class FrozenEnvironDict(dict):
        """Blocked environ dict that raises on access after freeze."""
        def __getitem__(self, key):
            raise GuardViolation(
                f"Direct os.environ access blocked after freeze. "
                f"Use get_frozen_environment() instead.",
                code="ENV_DIRECT_ACCESS_BLOCKED"
            )
        
        def get(self, key, default=None):
            raise GuardViolation(
                f"Direct os.environ access blocked after freeze. "
                f"Use get_frozen_environment() instead.",
                code="ENV_DIRECT_ACCESS_BLOCKED"
            )
        
        def __contains__(self, key):
            raise GuardViolation(
                f"Direct os.environ access blocked after freeze. "
                f"Use get_frozen_environment() instead.",
                code="ENV_DIRECT_ACCESS_BLOCKED"
            )
    
    # Replace os.environ with guarded version
    import sys
    sys.modules['os'].environ = FrozenEnvironDict()


def _reset_freeze_for_testing() -> None:
    """
    Reset freeze state for testing only.
    
    Hard guard prevents use outside test contexts. Forbidden in production.
    """
    global _frozen_environment, _environment_frozen, _replay_context_active
    
    # Hard guard: only allow in test/debug contexts
    # Check if we're in a test environment (PYTEST_CURRENT_TEST or unittest)
    import sys
    is_test_context = (
        'pytest' in sys.modules or
        'unittest' in sys.modules or
        'PYTEST_CURRENT_TEST' in os.environ or
        sys.argv[0].endswith('pytest') or
        'test' in sys.argv[0].lower()
    )
    
    if not is_test_context:
        raise GuardViolation(
            "_reset_freeze_for_testing() can only be called in test contexts. "
            "This function is forbidden in production.",
            code="ENV_TEST_ONLY_FUNCTION"
        )
    
    _frozen_environment = None
    _environment_frozen = False
    _replay_context_active = False


# ============================================================================
# VALIDATION HELPERS
# ============================================================================

def validate_timezone(timezone: str) -> None:
    """Validate timezone string is recognized IANA timezone identifier."""
    if not timezone or not timezone.strip():
        raise EnvironmentConfigurationError(
            "Timezone cannot be empty",
            code="ENV_INVALID_TIMEZONE",
            details={"timezone": timezone}
        )
    
    # Basic validation - common timezones
    # In production, integrate with zoneinfo for comprehensive validation
    common_timezones = {
        'UTC',
        'America/New_York',
        'America/Chicago',
        'America/Denver',
        'America/Los_Angeles',
        'Europe/London',
        'Europe/Paris',
        'Asia/Tokyo',
        'Asia/Shanghai',
        'Australia/Sydney',
    }
    
    # Allow common timezones and Etc/* patterns
    if timezone not in common_timezones and not timezone.startswith('Etc/'):
        # For strict validation, reject unknown timezones
        # In production, validate against zoneinfo.available_timezones()
        raise EnvironmentConfigurationError(
            f"Timezone '{timezone}' is not recognized. "
            f"Use a standard IANA timezone identifier.",
            code="ENV_INVALID_TIMEZONE",
            details={"timezone": timezone}
        )