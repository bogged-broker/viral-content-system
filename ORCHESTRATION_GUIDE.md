# System Orchestration Guide

This document explains the new orchestration infrastructure that provides deterministic, mode-based system execution.

## Overview

The system now has a **single authoritative entry point** through `SystemOrchestrator` that controls all system components. This solves the problem of "how do I actually run this end-to-end?"

## Architecture

### Core Components

1. **SystemOrchestrator** (`/orchestration/system_orchestrator.py`)
   - Single authoritative runtime flow controller
   - Mode-based execution (ingest, generate, post, train, stress-test, full-system)
   - Deterministic startup sequence
   - Component lifecycle management

2. **LifecycleManager** (`/orchestration/lifecycle_manager.py`)
   - Manages component lifecycle states
   - Health checks
   - Graceful shutdown

3. **StartupSequence** (`/orchestration/startup_sequence.py`)
   - Defines deterministic startup order
   - Dependency resolution
   - Phase-based execution

4. **ShutdownManager** (`/orchestration/shutdown_manager.py`)
   - Graceful shutdown orchestration
   - Timeout handling
   - Resource cleanup

## Execution Modes

The system supports the following execution modes:

- **`ingest`**: Start ingestion loops only
- **`generate`**: Start content generation only
- **`post`**: Start posting system only
- **`train`**: Start ML training only
- **`stress-test`**: Stress testing mode
- **`full-system`**: Start everything (default)

## Usage

### Command Line

```bash
# Run full system
python main.py --mode=full-system

# Run only ingestion
python main.py --mode=ingest

# Run only generation
python main.py --mode=generate

# Run with interactive mode
python main.py --mode=full-system --interactive

# Set log level
python main.py --mode=full-system --log-level=DEBUG
```

### Programmatic Usage

```python
from orchestration.system_orchestrator import SystemOrchestrator

orchestrator = SystemOrchestrator()
await orchestrator.start(mode="full-system")

# Wait for shutdown
await orchestrator.wait_for_shutdown()

# Or shutdown manually
await orchestrator.shutdown()
```

## Startup Sequence

The system follows this deterministic startup order:

1. **Bootstrap** - System infrastructure validation
2. **Config Load** - Load configuration registry, deployment profile, feature flags
3. **Infra Init** - Initialize clock, ID generator, persistence, logging
4. **Lineage Validate** - Validate lineage integrity (replay guard, invariants, merkle check)
5. **Governance Lock** - Acquire governance lock (if structural mutation required)
6. **Pipeline Load** - Load pipelines (ingestion, transform, aggregation, computation)
7. **Component Start** - Start components based on execution mode
8. **Monitoring Start** - Start health monitors, safety watchdog, runtime stress monitor

## Scripts

The `scripts/` directory contains execution scripts:

### Linux/Mac

- `bootstrap_local.sh` - Bootstrap local environment
- `run_pipeline.sh [mode]` - Run pipeline in specified mode
- `run_e2e.sh` - Run end-to-end test
- `replay_from_snapshot.sh [snapshot_path]` - Replay from snapshot

### Windows

- `bootstrap_local.bat` - Bootstrap local environment
- `run_pipeline.bat [mode]` - Run pipeline in specified mode
- `run_e2e.bat` - Run end-to-end test

## Testing

End-to-end tests are located in `/tests/e2e_test_full_pipeline.py`:

```bash
# Run e2e tests
pytest tests/e2e_test_full_pipeline.py -v

# Or use the script
./scripts/run_e2e.sh
```

## Example: Full System Startup

```python
import asyncio
from orchestration.system_orchestrator import SystemOrchestrator

async def main():
    orchestrator = SystemOrchestrator()
    
    # Start in full-system mode
    success = await orchestrator.start(mode="full-system")
    
    if not success:
        print("Failed to start system")
        return
    
    # Get status
    status = orchestrator.get_status()
    print(f"Mode: {status['mode']}")
    print(f"Components: {status['started_components']}")
    
    # Wait for shutdown signal
    await orchestrator.wait_for_shutdown()

if __name__ == "__main__":
    asyncio.run(main())
```

## Component Lifecycle

Components go through these states:

1. **UNINITIALIZED** - Component not yet registered
2. **INITIALIZING** - Component initialization in progress
3. **INITIALIZED** - Component initialized but not started
4. **STARTING** - Component startup in progress
5. **RUNNING** - Component running normally
6. **STOPPING** - Component shutdown in progress
7. **STOPPED** - Component stopped
8. **FAILED** - Component in error state

## Graceful Shutdown

The system supports graceful shutdown:

1. Components are stopped in reverse order of startup
2. Each component has a timeout for shutdown
3. Global timeout prevents infinite shutdown
4. State is persisted before shutdown

## Integration with Existing Code

The orchestrator integrates with existing components:

- **Bootstrap**: Uses `infra.bootstrap.bootstrap()`
- **Config**: Uses `infra.config_registry` and `config.deployment_profile`
- **Infrastructure**: Uses `infra.clock`, `infra.id_generator`
- **Components**: Loads ingestion, feature extraction, scoring, generation, posting

## Troubleshooting

### System won't start

1. Check bootstrap output: `python main.py --mode=full-system --log-level=DEBUG`
2. Verify dependencies: `pip install -r requirements.txt`
3. Check configuration: Ensure config files exist in `config/`

### Components not starting

1. Check component logs
2. Verify component dependencies are available
3. Check mode - some components only start in specific modes

### Shutdown hangs

1. Check for blocking operations in components
2. Increase shutdown timeout in `ShutdownManager`
3. Check for deadlocks in component shutdown handlers

## Next Steps

1. **Configure Environment**: Set up `.env` file with API keys
2. **Run Bootstrap**: `./scripts/bootstrap_local.sh` (or `.bat` on Windows)
3. **Test System**: `./scripts/run_e2e.sh`
4. **Run Full System**: `python main.py --mode=full-system`

## Architecture Benefits

✅ **Deterministic Execution**: Clear startup/shutdown sequence
✅ **Mode Separation**: Run only what you need
✅ **Reproducibility**: Same inputs → same execution
✅ **Operational Clarity**: Clear entry point and execution flow
✅ **Testing Surface**: E2E tests verify full pipeline
✅ **Production Ready**: Graceful shutdown, health monitoring

This addresses the core issue: **"How do I actually run this end-to-end?"** - Now you have a clear answer.
