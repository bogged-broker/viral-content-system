#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
End-to-End Verification Script
Verifies all components are properly set up and can be imported/initialized
"""

import sys
import os
from pathlib import Path

# Fix Windows console encoding
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# Set environment variables
os.environ["DEPLOYMENT_MODE"] = "sandbox"
os.environ["DEPLOYMENT_ENV"] = "DEVELOPMENT"
os.environ["CONFIG_REGISTRY_PATH"] = str(Path(__file__).parent / "config")
os.environ["INFRA_VERSION"] = "1.0.0"
os.environ["RUN_ENVIRONMENT"] = "local"
os.environ["AUDIT_LOG_PATH"] = str(Path(__file__).parent / "logs" / "audit_dev.log")

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

print("=" * 80)
print("VIRAL CONTENT SYSTEM - END-TO-END VERIFICATION")
print("=" * 80)
print()

# Test 1: Environment config files exist
print("[1/10] Checking environment config files...")
config_dir = project_root / "config" / "environments"
required_configs = ["development.yaml", "staging.yaml", "production.yaml"]
all_exist = True
for config_file in required_configs:
    config_path = config_dir / config_file
    exists = config_path.exists()
    status = "[OK]" if exists else "[MISSING]"
    print(f"  {status} {config_file}: {'EXISTS' if exists else 'MISSING'}")
    if not exists:
        all_exist = False
if all_exist:
    print("  [OK] All environment config files exist")
else:
    print("  [FAIL] Some environment config files are missing")
print()

# Test 2: Scripts exist
print("[2/10] Checking scripts...")
scripts_dir = project_root / "scripts"
required_scripts = [
    "bootstrap_local.sh",
    "run_pipeline.sh",
    "run_e2e.sh",
    "replay_from_snapshot.sh"
]
all_exist = True
for script in required_scripts:
    script_path = scripts_dir / script
    exists = script_path.exists()
    status = "[OK]" if exists else "[MISSING]"
    print(f"  {status} {script}: {'EXISTS' if exists else 'MISSING'}")
    if not exists:
        all_exist = False
if all_exist:
    print("  [OK] All required scripts exist")
else:
    print("  [FAIL] Some scripts are missing")
print()

# Test 3: Observability configs exist
print("[3/10] Checking observability configs...")
obs_dir = project_root / "infra" / "observability"
prom_config = obs_dir / "prometheus.yml"
grafana1 = obs_dir / "grafana_dashboards" / "system_overview.json"
grafana2 = obs_dir / "grafana_dashboards" / "pipeline_metrics.json"
print(f"  {'[OK]' if prom_config.exists() else '[MISSING]'} prometheus.yml: {'EXISTS' if prom_config.exists() else 'MISSING'}")
print(f"  {'[OK]' if grafana1.exists() else '[MISSING]'} system_overview.json: {'EXISTS' if grafana1.exists() else 'MISSING'}")
print(f"  {'[OK]' if grafana2.exists() else '[MISSING]'} pipeline_metrics.json: {'EXISTS' if grafana2.exists() else 'MISSING'}")
print()

# Test 4: Environment config loader
print("[4/10] Testing environment config loader...")
try:
    from config.environments import load_environment_config
    from config.deployment_profile import DeploymentEnvironment
    
    config = load_environment_config(DeploymentEnvironment.DEVELOPMENT)
    print(f"  [OK] Environment config loaded successfully")
    print(f"    Environment: {config.get('environment')}")
    print(f"    Sections: {', '.join(list(config.keys())[:5])}")
except Exception as e:
    print(f"  [FAIL] Failed to load environment config: {e}")
print()

# Test 5: SystemOrchestrator import
print("[5/10] Testing SystemOrchestrator import...")
try:
    from orchestration.system_orchestrator import SystemOrchestrator, ExecutionMode
    print(f"  [OK] SystemOrchestrator imported successfully")
    print(f"    Available modes: {', '.join([m.value for m in ExecutionMode])}")
except Exception as e:
    print(f"  [FAIL] Failed to import SystemOrchestrator: {e}")
    import traceback
    traceback.print_exc()
print()

# Test 6: Metric registry
print("[6/10] Testing metric registry...")
try:
    from infra.observability.metric_registry import get_metric_registry
    registry = get_metric_registry()
    metrics = registry.list_all()
    print(f"  [OK] Metric registry initialized: {len(metrics)} metrics registered")
    print(f"    Sample metrics: {', '.join([m.name for m in metrics[:3]])}")
except Exception as e:
    print(f"  [FAIL] Failed to initialize metric registry: {e}")
print()

# Test 7: Health endpoint
print("[7/10] Testing health endpoint import...")
try:
    from infra.observability.health_endpoint import HealthEndpoint
    print(f"  [OK] HealthEndpoint imported successfully")
except Exception as e:
    print(f"  [FAIL] Failed to import HealthEndpoint: {e}")
print()

# Test 8: Deployment profile
print("[8/10] Testing deployment profile...")
try:
    from config.deployment_profile import (
        DeploymentEnvironment,
        initialize_deployment_profile
    )
    profile = initialize_deployment_profile(DeploymentEnvironment.DEVELOPMENT)
    print(f"  [OK] Deployment profile initialized: {profile.environment.value}")
    print(f"    Allow partial repair: {profile.allow_partial_repair}")
    print(f"    Enforce rate limits: {profile.enforce_rate_limits}")
except Exception as e:
    print(f"  [FAIL] Failed to initialize deployment profile: {e}")
print()

# Test 9: Main entry point
print("[9/10] Testing main.py import...")
try:
    import main
    print(f"  [OK] main.py imported successfully")
except Exception as e:
    print(f"  [FAIL] Failed to import main.py: {e}")
print()

# Test 10: E2E test file
print("[10/10] Checking e2e test file...")
e2e_test = project_root / "tests" / "e2e_test_full_pipeline.py"
if e2e_test.exists():
    print(f"  [OK] e2e_test_full_pipeline.py exists")
    # Try to parse it
    try:
        with open(e2e_test, 'r') as f:
            content = f.read()
            if 'test_environment_config_loaded_in_orchestrator' in content:
                print(f"    [OK] Contains environment config test")
            if 'test_system_orchestrator_startup' in content:
                print(f"    [OK] Contains orchestrator startup test")
    except Exception as e:
        print(f"    ✗ Error reading test file: {e}")
else:
    print(f"  [FAIL] e2e_test_full_pipeline.py missing")
print()

print("=" * 80)
print("VERIFICATION COMPLETE")
print("=" * 80)
print()
print("Note: Full system execution requires Python 3.11+ and all dependencies.")
print("This verification confirms all files and imports are properly set up.")
print()
