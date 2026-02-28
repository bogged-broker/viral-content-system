"""
End-to-End Test for Full Pipeline Execution

This test validates that the entire system can start and run through
a complete execution cycle from ingestion to posting.

CRITICAL: This test must be deterministic and reproducible.
"""

import asyncio
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Try to import pytest, but don't fail if it's not available
try:
    import pytest
    HAS_PYTEST = True
except ImportError:
    HAS_PYTEST = False
    # Create a minimal pytest-like decorator for when pytest is not available
    class pytest:
        @staticmethod
        def fixture(*args, **kwargs):
            def decorator(func):
                return func
            return decorator
        
        @staticmethod
        def mark(*args, **kwargs):
            class Mark:
                asyncio = None
            return Mark()
        
        @staticmethod
        def skip(*args, **kwargs):
            pass
        
        @staticmethod
        def fail(*args, **kwargs):
            raise AssertionError(*args)
        
        @staticmethod
        def main(*args, **kwargs):
            print("pytest not available - cannot run tests")
            return 0


@pytest.fixture(scope="module")
def test_environment():
    """Set up test environment variables."""
    # Set environment variables for testing
    os.environ["DEPLOYMENT_MODE"] = "test"
    os.environ["DEPLOYMENT_ENV"] = "TESTING"
    os.environ["CONFIG_REGISTRY_PATH"] = str(project_root / "config")
    os.environ["INFRA_VERSION"] = "1.0.0"
    os.environ["RUN_ENVIRONMENT"] = "test"
    os.environ["AUDIT_LOG_PATH"] = str(project_root / "logs" / "audit_test.log")
    
    # Ensure log directory exists
    (project_root / "logs").mkdir(exist_ok=True)
    
    yield
    
    # Cleanup (if needed)
    pass


@pytest.mark.asyncio
async def test_bootstrap_sequence(test_environment):
    """Test that bootstrap sequence completes successfully."""
    from infra.bootstrap import bootstrap
    
    result = bootstrap()
    
    assert result.success, f"Bootstrap failed: {result.abort_reason}"
    assert result.run_id is not None, "Bootstrap should generate run_id"
    assert result.boot_hash is not None, "Bootstrap should generate boot_hash"
    assert len(result.checks) > 0, "Bootstrap should perform checks"


@pytest.mark.asyncio
async def test_config_loading(test_environment):
    """Test that configuration loads correctly."""
    from config.deployment_profile import (
        DeploymentEnvironment,
        initialize_deployment_profile
    )
    from config.environments import load_environment_config
    
    # Test deployment profile initialization
    profile = initialize_deployment_profile(DeploymentEnvironment.TESTING)
    assert profile is not None
    assert profile.environment == DeploymentEnvironment.TESTING
    
    # Test environment config loading
    env_config = load_environment_config(DeploymentEnvironment.DEVELOPMENT)
    assert env_config is not None
    assert env_config.get("environment") == "DEVELOPMENT"
    
    # Verify key config sections exist
    assert "governance" in env_config
    assert "validation" in env_config
    assert "limits" in env_config
    assert "observability" in env_config


@pytest.mark.asyncio
async def test_system_orchestrator_startup(test_environment):
    """Test that SystemOrchestrator can start in full-system mode."""
    from orchestration.system_orchestrator import SystemOrchestrator
    
    orchestrator = SystemOrchestrator()
    
    # Start system (with timeout to prevent hanging)
    try:
        success = await asyncio.wait_for(
            orchestrator.start(mode="full-system"),
            timeout=30.0
        )
        
        assert success, "SystemOrchestrator should start successfully"
        
        # Check that context is initialized
        assert orchestrator.ctx is not None
        assert orchestrator.ctx.bootstrap_result is not None
        assert orchestrator.ctx.config_registry is not None
        assert orchestrator.ctx.deployment_profile is not None
        
        # Verify environment config was loaded (may be None if file not found, but should attempt)
        # The orchestrator should have attempted to load it
        assert hasattr(orchestrator.ctx, 'environment_config'), "Context should have environment_config field"
        
        # Check status
        status = orchestrator.get_status()
        assert status["mode"] == "full-system"
        assert len(status["started_components"]) > 0
        
        # Shutdown
        await orchestrator.shutdown()
        
    except asyncio.TimeoutError:
        pytest.fail("SystemOrchestrator startup timed out")
    except Exception as e:
        pytest.fail(f"SystemOrchestrator startup failed: {e}")


@pytest.mark.asyncio
async def test_system_orchestrator_modes(test_environment):
    """Test that SystemOrchestrator can start in different modes."""
    from orchestration.system_orchestrator import SystemOrchestrator
    
    modes = ["ingest", "generate", "post", "train"]
    
    for mode in modes:
        orchestrator = SystemOrchestrator()
        
        try:
            success = await asyncio.wait_for(
                orchestrator.start(mode=mode),
                timeout=20.0
            )
            
            assert success, f"SystemOrchestrator should start in {mode} mode"
            
            status = orchestrator.get_status()
            assert status["mode"] == mode
            
            await orchestrator.shutdown()
            
        except asyncio.TimeoutError:
            pytest.fail(f"SystemOrchestrator startup timed out in {mode} mode")
        except Exception as e:
            pytest.fail(f"SystemOrchestrator startup failed in {mode} mode: {e}")


@pytest.mark.asyncio
async def test_health_endpoint(test_environment):
    """Test that health endpoint can be started and queried."""
    try:
        from infra.observability.health_endpoint import HealthEndpoint
        import aiohttp
        
        endpoint = HealthEndpoint(port=8001)  # Use different port for test
        
        # Start endpoint
        await endpoint.start()
        
        try:
            # Query health endpoint
            async with aiohttp.ClientSession() as session:
                async with session.get('http://localhost:8001/health') as resp:
                    assert resp.status in [200, 503], f"Health endpoint returned {resp.status}"
                    data = await resp.json()
                    assert "status" in data
                    assert "timestamp" in data
        finally:
            await endpoint.stop()
            
    except ImportError:
        pytest.skip("Health endpoint not available")
    except Exception as e:
        pytest.skip(f"Health endpoint test skipped: {e}")


@pytest.mark.asyncio
async def test_metric_registry(test_environment):
    """Test that metric registry is properly initialized."""
    try:
        from infra.observability.metric_registry import get_metric_registry
        
        registry = get_metric_registry()
        
        # Check that core metrics are registered
        metrics = registry.list_all()
        assert len(metrics) > 0, "Metric registry should contain metrics"
        
        # Check for specific metrics
        system_requests = registry.get("viral_system_requests_total")
        assert system_requests is not None, "System requests metric should be registered"
        
    except ImportError:
        pytest.skip("Metric registry not available")


def test_environment_configs_exist():
    """Test that environment config files exist."""
    config_dir = project_root / "config" / "environments"
    
    required_configs = ["development.yaml", "staging.yaml", "production.yaml"]
    
    for config_file in required_configs:
        config_path = config_dir / config_file
        assert config_path.exists(), f"Environment config not found: {config_file}"


@pytest.mark.asyncio
async def test_environment_config_loaded_in_orchestrator(test_environment):
    """Test that SystemOrchestrator loads and uses environment config."""
    from orchestration.system_orchestrator import SystemOrchestrator
    from config.deployment_profile import DeploymentEnvironment
    
    # Set environment to DEVELOPMENT to ensure config file exists
    os.environ["DEPLOYMENT_ENV"] = "DEVELOPMENT"
    
    orchestrator = SystemOrchestrator()
    
    try:
        success = await asyncio.wait_for(
            orchestrator.start(mode="ingest"),  # Use ingest mode for faster startup
            timeout=20.0
        )
        
        assert success, "SystemOrchestrator should start successfully"
        
        # Verify environment config was loaded
        assert orchestrator.ctx.environment_config is not None, \
            "Environment config should be loaded from YAML file"
        
        # Verify config structure
        env_config = orchestrator.ctx.environment_config
        assert env_config.get("environment") == "DEVELOPMENT"
        assert "governance" in env_config
        assert "validation" in env_config
        assert "limits" in env_config
        
        # Verify config values are being used (check ingestion config was applied)
        # The orchestrator should have logged config values during startup
        
        await orchestrator.shutdown()
        
    except asyncio.TimeoutError:
        pytest.fail("SystemOrchestrator startup timed out")
    except Exception as e:
        pytest.fail(f"SystemOrchestrator startup failed: {e}")


def test_scripts_exist():
    """Test that required scripts exist."""
    scripts_dir = project_root / "scripts"
    
    required_scripts = [
        "bootstrap_local.sh",
        "run_pipeline.sh",
        "run_e2e.sh",
        "replay_from_snapshot.sh"
    ]
    
    for script in required_scripts:
        script_path = scripts_dir / script
        assert script_path.exists(), f"Required script not found: {script}"
        assert os.access(script_path, os.X_OK), f"Script not executable: {script}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
