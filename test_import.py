#!/usr/bin/env python3
"""Test if system can import successfully."""
import sys
sys.path.insert(0, '.')

try:
    print("Testing imports...")
    from orchestration.system_orchestrator import SystemOrchestrator
    print("OK: SystemOrchestrator imported")
    print("SUCCESS: System can import!")
    sys.exit(0)
except Exception as e:
    print(f"ERROR: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
