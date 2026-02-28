"""
Backend Policy Enforcement

Static policy rules for backend/environment combinations.
Enforces production safety, replay isolation, and development flexibility.
"""

from typing import Set


class BackendPolicy:
    """
    Static policy rules for backend/environment combinations.
    
    Enforces:
    - Production safety rules
    - Replay isolation requirements
    - Development flexibility
    """
    
    # Backends allowed in production environment
    PROD_ALLOWED_BACKENDS: Set[str] = {"kv", "redis", "s3"}
    
    # Backends allowed in replay environment
    REPLAY_ALLOWED_BACKENDS: Set[str] = {"memory", "kv"}
    
    # Backends allowed in test environment
    TEST_ALLOWED_BACKENDS: Set[str] = {"memory"}
    
    # Backends allowed in dev environment
    DEV_ALLOWED_BACKENDS: Set[str] = {"memory", "kv", "redis", "s3"}
    
    # Backends allowed in staging environment
    STAGING_ALLOWED_BACKENDS: Set[str] = {"kv", "redis", "s3"}
    
    @staticmethod
    def validate_backend_for_environment(
        backend_type: str,
        environment: str
    ) -> None:
        """
        Validate backend is allowed in given environment.
        
        Raises:
            PolicyViolationError: If combination is not allowed
        """
        from infra.persistence.backends.backend_factory_errors import PolicyViolationError
        
        env_policies = {
            "prod": BackendPolicy.PROD_ALLOWED_BACKENDS,
            "replay": BackendPolicy.REPLAY_ALLOWED_BACKENDS,
            "test": BackendPolicy.TEST_ALLOWED_BACKENDS,
            "dev": BackendPolicy.DEV_ALLOWED_BACKENDS,
            "staging": BackendPolicy.STAGING_ALLOWED_BACKENDS,
        }
        
        allowed_backends = env_policies.get(environment)
        if allowed_backends is None:
            raise PolicyViolationError(
                backend_type=backend_type,
                environment=environment,
                reason=f"Unknown environment '{environment}'"
            )
        
        if backend_type not in allowed_backends:
            raise PolicyViolationError(
                backend_type=backend_type,
                environment=environment,
                reason=f"Backend not allowed in {environment}. "
                       f"Allowed: {', '.join(sorted(allowed_backends))}"
            )
