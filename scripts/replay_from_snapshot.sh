#!/bin/bash
# Replay From Snapshot
# Replays system execution from a saved snapshot with deterministic setup
# Usage: ./scripts/replay_from_snapshot.sh [snapshot_path] [run_id]
#   snapshot_path: Path to snapshot file (default: checkpoints/latest_snapshot.json)
#   run_id: Original run ID to replay (optional)

set -e

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# Parse arguments
SNAPSHOT_PATH="${1:-checkpoints/latest_snapshot.json}"
REPLAY_RUN_ID="${2:-}"

if [ ! -f "$SNAPSHOT_PATH" ]; then
    echo "ERROR: Snapshot file not found: $SNAPSHOT_PATH"
    echo ""
    echo "Available snapshots:"
    find checkpoints -name "*.json" -type f 2>/dev/null | head -10 || echo "  (none found)"
    exit 1
fi

echo "=========================================="
echo "Replaying From Snapshot"
echo "=========================================="
echo "Snapshot: $SNAPSHOT_PATH"
if [ -n "$REPLAY_RUN_ID" ]; then
    echo "Run ID: $REPLAY_RUN_ID"
fi
echo ""

# Set environment variables for replay
export DEPLOYMENT_MODE="replay"
export DEPLOYMENT_ENV="DEVELOPMENT"
export CONFIG_REGISTRY_PATH="$PROJECT_ROOT/config"
export INFRA_VERSION="1.0.0"
export RUN_ENVIRONMENT="replay"
export REPLAY_SNAPSHOT="$SNAPSHOT_PATH"
export REPLAY_MODE="true"
if [ -n "$REPLAY_RUN_ID" ]; then
    export REPLAY_RUN_ID="$REPLAY_RUN_ID"
fi

# Run the system in replay mode
python main.py --mode=full-system --log-level=INFO

echo ""
echo "=========================================="
echo "Replay Complete"
echo "=========================================="
