"""
Environment-specific configuration loader.

Loads YAML configuration files based on deployment environment.
"""

import os
import yaml
from pathlib import Path
from typing import Dict, Any, Optional
from config.deployment_profile import DeploymentEnvironment


def load_environment_config(environment: DeploymentEnvironment) -> Dict[str, Any]:
    """
    Load environment-specific configuration from YAML file.
    
    Args:
        environment: Deployment environment to load config for
        
    Returns:
        Dictionary containing environment-specific configuration
        
    Raises:
        FileNotFoundError: If environment config file not found
        yaml.YAMLError: If YAML parsing fails
    """
    # Get config directory
    config_dir = Path(__file__).parent
    env_name = environment.value.lower()
    config_file = config_dir / f"{env_name}.yaml"
    
    if not config_file.exists():
        raise FileNotFoundError(
            f"Environment config file not found: {config_file}"
        )
    
    with open(config_file, 'r') as f:
        config = yaml.safe_load(f)
    
    # Validate environment matches
    if config.get('environment') != environment.value:
        raise ValueError(
            f"Config file environment mismatch: expected {environment.value}, "
            f"got {config.get('environment')}"
        )
    
    return config


def get_environment_config_path(environment: DeploymentEnvironment) -> Path:
    """
    Get path to environment-specific configuration file.
    
    Args:
        environment: Deployment environment
        
    Returns:
        Path to environment config file
    """
    config_dir = Path(__file__).parent
    env_name = environment.value.lower()
    return config_dir / f"{env_name}.yaml"


__all__ = [
    'load_environment_config',
    'get_environment_config_path',
]
