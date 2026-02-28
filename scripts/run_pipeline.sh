#!/bin/bash
# Run Pipeline
# Executes the full pipeline end-to-end with deterministic setup
# Usage: ./scripts/run_pipeline.sh [mode] [environment]
#   mode: ingest, generate, post, train, stress-test, full-system (default: full-system)
#   environment: development, staging, production (default: development)

set -e

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

# Parse arguments
MODE="${1:-full-system}"
ENV="${2:-development}"

# Validate mode
VALID_MODES=("ingest" "generate" "post" "train" "stress-test" "full-system")
if [[ ! " ${VALID_MODES[@]} " =~ " ${MODE} " ]]; then
    echo "ERROR: Invalid mode: $MODE"
    echo "Valid modes: ${VALID_MODES[*]}"
    exit 1
fi

# Validate environment
VALID_ENVS=("development" "staging" "production")
if [[ ! " ${VALID_ENVS[@]} " =~ " ${ENV} " ]]; then
    echo "ERROR: Invalid environment: $ENV"
    echo "Valid environments: ${VALID_ENVS[*]}"
    exit 1
fi

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# Set environment variables
export DEPLOYMENT_MODE="sandbox"
export DEPLOYMENT_ENV="${ENV^^}"  # Uppercase
export CONFIG_REGISTRY_PATH="$PROJECT_ROOT/config"
export INFRA_VERSION="1.0.0"
export RUN_ENVIRONMENT="$ENV"
export AUDIT_LOG_PATH="$PROJECT_ROOT/logs/audit_${ENV}.log"

# Ensure log directory exists
mkdir -p "$(dirname "$AUDIT_LOG_PATH")"

echo "=========================================="
echo "Running Pipeline"
echo "=========================================="
echo "Mode: $MODE"
echo "Environment: $ENV"
echo "Project root: $PROJECT_ROOT"
echo ""

# Run the pipeline
python main.py --mode="$MODE" --log-level=INFO

echo ""
echo "=========================================="
echo "Pipeline Complete"
echo "=========================================="
