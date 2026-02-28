"""
Backend Configuration Validators

Validates backend-specific configuration before construction.
All validation logic is isolated here, not in the factory.
"""

from infra.persistence.backends.backend_config_schemas import (
    BackendConfig,
    MemoryBackendConfig,
    KVBackendConfig,
    RedisBackendConfig,
    S3BackendConfig,
)
from infra.persistence.backends.backend_factory_errors import InvalidBackendConfigError


class BackendValidator:
    """
    Validates backend-specific configuration before construction.
    """
    
    @staticmethod
    def validate_memory_config(config: MemoryBackendConfig) -> None:
        """Validate memory backend configuration."""
        if config.max_size_mb is not None:
            if config.max_size_mb <= 0:
                raise InvalidBackendConfigError(
                    "memory",
                    "max_size_mb must be positive"
                )
        
        if config.eviction_policy is not None:
            valid_policies = {"lru", "lfu", "fifo"}
            if config.eviction_policy not in valid_policies:
                raise InvalidBackendConfigError(
                    "memory",
                    f"Invalid eviction_policy. Valid: {', '.join(valid_policies)}"
                )
    
    @staticmethod
    def validate_kv_config(config: KVBackendConfig) -> None:
        """Validate KV backend configuration."""
        if not config.storage_path:
            raise InvalidBackendConfigError(
                "kv",
                "storage_path is required"
            )
        
        # Path validation
        if ".." in config.storage_path:
            raise InvalidBackendConfigError(
                "kv",
                "storage_path contains path traversal"
            )
    
    @staticmethod
    def validate_redis_config(config: RedisBackendConfig) -> None:
        """Validate Redis backend configuration."""
        if not config.host:
            raise InvalidBackendConfigError(
                "redis",
                "host is required"
            )
        
        if config.port <= 0 or config.port > 65535:
            raise InvalidBackendConfigError(
                "redis",
                f"Invalid port {config.port} (must be 1-65535)"
            )
        
        if config.db < 0:
            raise InvalidBackendConfigError(
                "redis",
                "db must be non-negative"
            )
        
        if config.connection_timeout_ms <= 0:
            raise InvalidBackendConfigError(
                "redis",
                "connection_timeout_ms must be positive"
            )
        
        if config.max_retries < 0:
            raise InvalidBackendConfigError(
                "redis",
                "max_retries must be non-negative"
            )
    
    @staticmethod
    def validate_s3_config(config: S3BackendConfig) -> None:
        """Validate S3 backend configuration."""
        if not config.bucket_name:
            raise InvalidBackendConfigError(
                "s3",
                "bucket_name is required"
            )
        
        if not config.region:
            raise InvalidBackendConfigError(
                "s3",
                "region is required"
            )
        
        # Bucket name validation (simplified)
        if len(config.bucket_name) < 3 or len(config.bucket_name) > 63:
            raise InvalidBackendConfigError(
                "s3",
                "bucket_name must be 3-63 characters"
            )
    
    @staticmethod
    def validate_config(config: BackendConfig) -> None:
        """
        Validate backend-specific configuration.
        
        Dispatches to appropriate validator based on config type.
        """
        if isinstance(config, MemoryBackendConfig):
            BackendValidator.validate_memory_config(config)
        elif isinstance(config, KVBackendConfig):
            BackendValidator.validate_kv_config(config)
        elif isinstance(config, RedisBackendConfig):
            BackendValidator.validate_redis_config(config)
        elif isinstance(config, S3BackendConfig):
            BackendValidator.validate_s3_config(config)
        else:
            # For base BackendConfig, only validate required fields
            if not config.backend_type:
                raise InvalidBackendConfigError(
                    config.backend_type,
                    "backend_type is required"
                )
            if not config.environment:
                raise InvalidBackendConfigError(
                    config.backend_type,
                    "environment is required"
                )
            if not config.version:
                raise InvalidBackendConfigError(
                    config.backend_type,
                    "version is required"
                )
