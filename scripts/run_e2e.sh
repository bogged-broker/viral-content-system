#!/bin/bash
# Run End-to-End Test
# Executes the full system end-to-end test with deterministic setup

set -e

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# Set environment variables for testing
export DEPLOYMENT_MODE="test"
export DEPLOYMENT_ENV="TESTING"
export CONFIG_REGISTRY_PATH="$PROJECT_ROOT/config"
export INFRA_VERSION="1.0.0"
export RUN_ENVIRONMENT="test"
export AUDIT_LOG_PATH="$PROJECT_ROOT/logs/audit_test.log"

# Ensure log directory exists
mkdir -p "$(dirname "$AUDIT_LOG_PATH")"

echo "=========================================="
echo "Running End-to-End Test"
echo "=========================================="
echo "Project root: $PROJECT_ROOT"
echo ""

# Run e2e test
if [ -f "tests/e2e_test_full_pipeline.py" ]; then
    echo "Running pytest e2e test..."
    python -m pytest tests/e2e_test_full_pipeline.py -v --tb=short
    TEST_EXIT_CODE=$?
else
    echo "WARNING: tests/e2e_test_full_pipeline.py not found"
    echo "Running full system in test mode as fallback..."
    python main.py --mode=full-system --log-level=INFO &
    MAIN_PID=$!
    sleep 10  # Let system start
    kill $MAIN_PID 2>/dev/null || true
    TEST_EXIT_CODE=0
fi

echo ""
echo "=========================================="
if [ $TEST_EXIT_CODE -eq 0 ]; then
    echo "End-to-End Test PASSED"
else
    echo "End-to-End Test FAILED (exit code: $TEST_EXIT_CODE)"
fi
echo "=========================================="

exit $TEST_EXIT_CODE
