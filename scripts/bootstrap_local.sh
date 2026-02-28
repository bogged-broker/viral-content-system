#!/bin/bash
# Bootstrap Local Environment
# Sets up local development environment and validates prerequisites
# This script ensures deterministic local setup

set -e

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "=========================================="
echo "Bootstrap Local Environment"
echo "=========================================="
echo "Project root: $PROJECT_ROOT"
echo ""

# Check Python version
echo "[1/7] Checking Python version..."
python_version=$(python3 --version 2>&1 | awk '{print $2}')
required_version="3.11"
if ! python3 -c "import sys; exit(0 if sys.version_info >= (3, 11) else 1)"; then
    echo "ERROR: Python 3.11+ required. Found: $python_version"
    exit 1
fi
echo "✓ Python $python_version"

# Check if virtual environment exists
echo "[2/7] Setting up virtual environment..."
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate
echo "✓ Virtual environment activated"

# Install dependencies
echo "[3/7] Installing dependencies..."
if [ -f "requirements.txt" ]; then
    pip install --upgrade pip
    pip install -r requirements.txt
    echo "✓ Dependencies installed"
else
    echo "WARNING: requirements.txt not found"
fi

# Create necessary directories
echo "[4/7] Creating necessary directories..."
mkdir -p data
mkdir -p logs
mkdir -p checkpoints
mkdir -p processed
mkdir -p config/environments
echo "✓ Directories created"

# Set up environment variables for development
echo "[5/7] Setting up environment variables..."
export DEPLOYMENT_MODE="sandbox"
export DEPLOYMENT_ENV="DEVELOPMENT"
export CONFIG_REGISTRY_PATH="$PROJECT_ROOT/config"
export INFRA_VERSION="1.0.0"
export RUN_ENVIRONMENT="local"
export AUDIT_LOG_PATH="$PROJECT_ROOT/logs/audit_dev.log"
echo "✓ Environment variables set"

# Validate environment config files exist
echo "[6/7] Validating environment configs..."
if [ ! -f "config/environments/development.yaml" ]; then
    echo "WARNING: config/environments/development.yaml not found"
else
    echo "✓ Development config exists"
fi
if [ ! -f "config/environments/staging.yaml" ]; then
    echo "WARNING: config/environments/staging.yaml not found"
else
    echo "✓ Staging config exists"
fi
if [ ! -f "config/environments/production.yaml" ]; then
    echo "WARNING: config/environments/production.yaml not found"
else
    echo "✓ Production config exists"
fi

# Validate bootstrap
echo "[7/7] Validating bootstrap..."
python3 -c "
import sys
import os
os.environ['DEPLOYMENT_MODE'] = 'sandbox'
os.environ['CONFIG_REGISTRY_PATH'] = '$PROJECT_ROOT/config'
os.environ['INFRA_VERSION'] = '1.0.0'
os.environ['RUN_ENVIRONMENT'] = 'local'
try:
    from infra.bootstrap import bootstrap
    result = bootstrap()
    if result.success:
        print('✓ Bootstrap validation passed')
        print(f'  Run ID: {result.run_id}')
    else:
        print(f'✗ Bootstrap validation failed: {result.abort_reason}')
        print(f'  Aborted at: {result.aborted_at.value if result.aborted_at else \"unknown\"}')
        sys.exit(1)
except ImportError as e:
    print(f'WARNING: Bootstrap not available: {e}')
except Exception as e:
    print(f'ERROR: Validation failed: {e}')
    import traceback
    traceback.print_exc()
    sys.exit(1)
"

echo ""
echo "=========================================="
echo "Bootstrap Complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "  1. Configure API keys in .env (if needed)"
echo "  2. Run full system: ./scripts/run_pipeline.sh full-system"
echo "  3. Run e2e test: ./scripts/run_e2e.sh"
echo "  4. Run specific mode: ./scripts/run_pipeline.sh ingest"
echo ""
