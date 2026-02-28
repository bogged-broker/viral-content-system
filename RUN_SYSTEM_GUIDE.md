# Complete Guide: Running the Viral Content System End-to-End

This guide provides step-by-step instructions to run the entire viral content system pipeline from start to finish.

## 📋 Table of Contents

1. [Prerequisites](#prerequisites)
2. [Installation](#installation)
3. [Environment Setup](#environment-setup)
4. [Schema Files Reference](#schema-files-reference)
5. [Running the System](#running-the-system)
6. [Execution Modes](#execution-modes)
7. [Troubleshooting](#troubleshooting)
8. [Full Pipeline Flow](#full-pipeline-flow)

---

## Prerequisites

### System Requirements

- **Python**: 3.11 or higher (required)
- **Operating System**: Windows 10+, Linux, or macOS
- **RAM**: Minimum 8GB (16GB+ recommended)
- **Disk Space**: At least 10GB free space
- **Internet**: Required for API access and package installation

### Required Software

- Python 3.11+ installed and accessible via `py -3.11` or `python3.11`
- pip (Python package manager)
- Git (for cloning if needed)

---

## Installation

### Step 1: Navigate to Project Directory

```bash
cd c:\Users\bluep\Downloads\viralcontentsystem
```

### Step 2: Install Python Dependencies

```bash
# Using Python 3.11
py -3.11 -m pip install -r requirements.txt

# Or if python3.11 is in PATH
python3.11 -m pip install -r requirements.txt
```

### Step 3: Verify Installation

```bash
py -3.11 -c "import pandas, numpy, torch; print('Dependencies OK')"
```

---

## Environment Setup

### Required Environment Variables

The system requires these environment variables to be set before running:

#### Windows (PowerShell)

```powershell
# Set environment variables for current session
$env:DEPLOYMENT_MODE="sandbox"
$env:CONFIG_REGISTRY_PATH="c:\Users\bluep\Downloads\viralcontentsystem\config"
$env:INFRA_VERSION="1.0.0"
$env:RUN_ENVIRONMENT="local"

# Optional: Set data directory
$env:DATA_DIR="c:\Users\bluep\Downloads\viralcontentsystem\data"
```

#### Windows (Command Prompt)

```cmd
set DEPLOYMENT_MODE=sandbox
set CONFIG_REGISTRY_PATH=c:\Users\bluep\Downloads\viralcontentsystem\config
set INFRA_VERSION=1.0.0
set RUN_ENVIRONMENT=local
set DATA_DIR=c:\Users\bluep\Downloads\viralcontentsystem\data
```

#### Linux/macOS

```bash
export DEPLOYMENT_MODE="sandbox"
export CONFIG_REGISTRY_PATH="./config"
export INFRA_VERSION="1.0.0"
export RUN_ENVIRONMENT="local"
export DATA_DIR="./data"
```

### Environment Variables Explained

| Variable | Required | Description | Default |
|----------|----------|-------------|---------|
| `DEPLOYMENT_MODE` | **Yes** | Deployment environment: `sandbox`, `staging`, `production`, `test` | None |
| `CONFIG_REGISTRY_PATH` | **Yes** | Path to configuration directory | None |
| `INFRA_VERSION` | **Yes** | Infrastructure version identifier | None |
| `RUN_ENVIRONMENT` | **Yes** | Logical runtime environment name | None |
| `DATA_DIR` | No | Path to data directory | `./data` |

### Creating a Setup Script (Optional)

#### Windows (setup_env.ps1)

```powershell
# setup_env.ps1
$env:DEPLOYMENT_MODE="sandbox"
$env:CONFIG_REGISTRY_PATH="c:\Users\bluep\Downloads\viralcontentsystem\config"
$env:INFRA_VERSION="1.0.0"
$env:RUN_ENVIRONMENT="local"
$env:DATA_DIR="c:\Users\bluep\Downloads\viralcontentsystem\data"

Write-Host "Environment variables set successfully!"
Write-Host "DEPLOYMENT_MODE: $env:DEPLOYMENT_MODE"
Write-Host "CONFIG_REGISTRY_PATH: $env:CONFIG_REGISTRY_PATH"
```

#### Linux/macOS (setup_env.sh)

```bash
#!/bin/bash
# setup_env.sh
export DEPLOYMENT_MODE="sandbox"
export CONFIG_REGISTRY_PATH="./config"
export INFRA_VERSION="1.0.0"
export RUN_ENVIRONMENT="local"
export DATA_DIR="./data"

echo "Environment variables set successfully!"
echo "DEPLOYMENT_MODE: $DEPLOYMENT_MODE"
echo "CONFIG_REGISTRY_PATH: $CONFIG_REGISTRY_PATH"
```

Make executable:
```bash
chmod +x setup_env.sh
```

---

## Schema Files Reference

The system uses JSON Schema files for validation and type checking. All schema files are located in `posting/schemas/`:

### Schema Files

1. **`posting/schemas/post_intent.schema.json`**
   - Defines the structure for post intent requests
   - Used when creating content to post

2. **`posting/schemas/post_result.schema.json`**
   - Defines the structure for post execution results
   - Contains success/failure information

3. **`posting/schemas/posting_state.schema.json`**
   - Defines the state machine for posting operations
   - Tracks posting lifecycle

4. **`posting/schemas/account_state.schema.json`**
   - Defines account state structure
   - Tracks account health and status

5. **`posting/schemas/platform_response.schema.json`**
   - Defines platform API response structure
   - Standardizes responses from YouTube, TikTok, etc.

6. **`posting/schemas/platform_policy.schema.json`**
   - Defines platform-specific policies
   - Rate limits, posting rules, etc.

7. **`posting/schemas/error_taxonomy.schema.json`**
   - Defines error classification structure
   - Categorizes errors for handling

8. **`posting/schemas/state_transition.schema.json`**
   - Defines valid state transitions
   - Ensures state machine integrity

9. **`posting/contracts/invariant_violation.schema.json`**
   - Defines invariant violation structure
   - Used for safety and validation

### Schema Validation

The system automatically validates data against these schemas. If validation fails, the operation will be rejected with detailed error messages.

---

## Running the System

### Basic Execution

#### Step 1: Set Environment Variables

```powershell
# Windows PowerShell
$env:DEPLOYMENT_MODE="sandbox"
$env:CONFIG_REGISTRY_PATH="c:\Users\bluep\Downloads\viralcontentsystem\config"
$env:INFRA_VERSION="1.0.0"
$env:RUN_ENVIRONMENT="local"
```

#### Step 2: Run the System

```bash
# Full system (default)
py -3.11 main.py --mode=full-system

# Or simply
py -3.11 main.py
```

### Command Line Options

```bash
py -3.11 main.py [OPTIONS]

Options:
  --mode MODE           Execution mode (default: full-system)
                        Choices: ingest, generate, post, train, stress-test, full-system
  
  --interactive         Run in interactive mode after startup
  
  --log-level LEVEL     Logging level (default: INFO)
                        Choices: DEBUG, INFO, WARNING, ERROR, CRITICAL
```

### Examples

```bash
# Run full system
py -3.11 main.py --mode=full-system

# Run only ingestion
py -3.11 main.py --mode=ingest

# Run only content generation
py -3.11 main.py --mode=generate

# Run only posting
py -3.11 main.py --mode=post

# Run with interactive mode
py -3.11 main.py --mode=full-system --interactive

# Run with debug logging
py -3.11 main.py --mode=full-system --log-level=DEBUG
```

---

## Execution Modes

### 1. Full System Mode (`--mode=full-system`)

Runs all components together:
- ✅ Bootstrap & validation
- ✅ Configuration loading
- ✅ Factory Manager initialization
- ✅ Ingestion pipeline
- ✅ Feature extraction
- ✅ Content generation
- ✅ Evaluation
- ✅ Posting system

**Use when**: You want to run the complete pipeline end-to-end.

### 2. Ingestion Mode (`--mode=ingest`)

Runs only the ingestion pipeline:
- ✅ Data ingestion from platforms
- ✅ Content parsing
- ✅ Metadata extraction
- ❌ No content generation
- ❌ No posting

**Use when**: You only want to collect data from platforms.

### 3. Generation Mode (`--mode=generate`)

Runs only content generation:
- ✅ Script generation
- ✅ Storyboard creation
- ✅ Audio synthesis
- ✅ Visual composition
- ❌ No ingestion
- ❌ No posting

**Use when**: You want to generate content without posting.

### 4. Posting Mode (`--mode=post`)

Runs only the posting system:
- ✅ Post dispatcher
- ✅ Platform posting
- ✅ State management
- ❌ No ingestion
- ❌ No generation

**Use when**: You want to post existing content.

### 5. Training Mode (`--mode=train`)

Runs ML/RL training:
- ✅ Model training
- ✅ Evaluation
- ✅ Checkpointing
- ❌ No production pipeline

**Use when**: You want to train or retrain models.

### 6. Stress Test Mode (`--mode=stress-test`)

Runs stress testing:
- ✅ Load testing
- ✅ Performance benchmarking
- ✅ Failure injection
- ❌ No production operations

**Use when**: You want to test system under load.

---

## Full Pipeline Flow

### End-to-End Execution Steps

When running in `full-system` mode, the pipeline executes in this order:

```
1. BOOTSTRAP
   ├── Environment validation
   ├── Clock verification
   ├── Identity lock
   ├── Config verification
   ├── Safety arming
   └── Final seal

2. CONFIGURATION
   ├── Load config files
   ├── Validate schemas
   └── Initialize components

3. FACTORY MANAGER
   ├── Initialize factories
   ├── Allocate resources
   └── Start worker pools

4. INGESTION PIPELINE
   ├── Platform adapters
   ├── Content fetching
   ├── Metadata extraction
   └── Data storage

5. FEATURE EXTRACTION
   ├── Virality features
   ├── Engagement patterns
   └── Sentiment analysis

6. CONTENT GENERATION
   ├── Storyboard creation
   ├── Script generation
   ├── Audio synthesis
   └── Visual composition

7. EVALUATION
   ├── Viral score calculation
   ├── Performance metrics
   └── Quality assessment

8. POSTING SYSTEM
   ├── Post dispatcher
   ├── Platform posting
   └── State tracking
```

### Expected Output

When running successfully, you should see:

```
============================================================
VIRAL CONTENT SYSTEM - Starting...
============================================================
Mode: full-system
============================================================

[BOOTSTRAP] System bring-up initiated
[BOOTSTRAP] Deployment mode: sandbox
[BOOTSTRAP] Python: 3.11.8

[BOOTSTRAP] Phase: ENVIRONMENT
           [OK] deployment_mode
           [OK] required_env_vars
           [OK] debug_flags
           [OK] python_version

[BOOTSTRAP] Phase: CLOCK
           [OK] monotonic_clock
           [OK] system_clock
           [OK] replay_mode

[BOOTSTRAP] Phase: IDENTITY
           [OK] run_identity

[BOOTSTRAP] Phase: CONFIG
           [OK] config_registry_path
           [OK] config_versioning

[BOOTSTRAP] Phase: SAFETY
           [OK] safety_systems
           [OK] audit_logging

[BOOTSTRAP] Phase: FINAL_SEAL
           [OK] final_seal

[BOOTSTRAP] [OK] ALL PHASES COMPLETE
[BOOTSTRAP] Run ID: 20260228_164913_3a6efa53
[BOOTSTRAP] Boot hash: c0b39d084d84a04a

[OK] Bootstrap successful

[2/3] Loading configuration...
[OK] Configuration loaded

[3/3] Initializing Factory Manager...
[OK] Factory Manager initialized

============================================================
SYSTEM RUNNING
============================================================
Mode: full-system
Press Ctrl+C to shutdown gracefully
============================================================
```

---

## Troubleshooting

### Common Issues

#### 1. Python Version Error

**Error**: `'type' object is not subscriptable`

**Solution**: Ensure you're using Python 3.11+:
```bash
py -3.11 --version  # Should show 3.11.x
```

#### 2. Missing Environment Variables

**Error**: `DEPLOYMENT_MODE environment variable not set`

**Solution**: Set all required environment variables (see [Environment Setup](#environment-setup))

#### 3. Import Errors

**Error**: `cannot import name 'X' from 'Y'`

**Solution**: 
1. Reinstall dependencies: `py -3.11 -m pip install -r requirements.txt --force-reinstall`
2. Check Python version compatibility
3. Verify all files are present

#### 4. Config File Not Found

**Error**: `No config file found in config/`

**Solution**: 
- This is a warning, not an error
- System will use defaults
- To use custom config, create `config/config.json`

#### 5. Bootstrap Failures

**Error**: `Bootstrap FAILED: <reason>`

**Solution**: 
- Check environment variables are set correctly
- Verify `CONFIG_REGISTRY_PATH` points to valid directory
- Ensure `config/versions.json` exists
- Check Python version is 3.11+

#### 6. Feature Extraction Warnings

**Warning**: `FeatureEdge CONSTRUCTION FAILED`

**Solution**: 
- This is expected for some edge cases
- System will continue with available features
- Not a blocking error

### Debug Mode

Run with debug logging to see detailed information:

```bash
py -3.11 main.py --mode=full-system --log-level=DEBUG
```

### Verification Steps

1. **Check Python Version**:
   ```bash
   py -3.11 --version
   ```

2. **Verify Environment Variables**:
   ```powershell
   # Windows PowerShell
   $env:DEPLOYMENT_MODE
   $env:CONFIG_REGISTRY_PATH
   $env:INFRA_VERSION
   $env:RUN_ENVIRONMENT
   ```

3. **Test Imports**:
   ```bash
   py -3.11 -c "from infra.bootstrap import bootstrap; print('OK')"
   ```

4. **Check Config Directory**:
   ```bash
   # Verify config directory exists
   ls config/  # Linux/macOS
   dir config\  # Windows
   ```

---

## Quick Start Checklist

- [ ] Python 3.11+ installed
- [ ] Dependencies installed (`pip install -r requirements.txt`)
- [ ] Environment variables set
- [ ] Config directory exists (`config/`)
- [ ] Run: `py -3.11 main.py --mode=full-system`

---

## Additional Resources

### Configuration Files

- `config/versions.json` - Version metadata
- `config/config.json` - Main configuration (optional)
- `platform_config.yaml` - Platform settings
- `long_tail_config.yaml` - Long-tail tracking config

### Log Files

Logs are written to:
- `./logs/pipeline/` - Pipeline logs
- `./logs/audit/` - Audit logs
- Console output (stdout/stderr)

### Output Directories

- `./output/videos/` - Generated videos
- `./data/` - Processed data
- `./checkpoints/` - Training checkpoints

---

## Support

If you encounter issues not covered in this guide:

1. Check the error message carefully
2. Run with `--log-level=DEBUG` for detailed logs
3. Verify all prerequisites are met
4. Check that environment variables are set correctly
5. Review the bootstrap output for specific phase failures

---

## Summary

To run the system end-to-end:

1. **Install**: `py -3.11 -m pip install -r requirements.txt`
2. **Set Environment**: Export required variables
3. **Run**: `py -3.11 main.py --mode=full-system`
4. **Monitor**: Watch console output for status
5. **Shutdown**: Press Ctrl+C for graceful shutdown

The system will execute the complete pipeline from bootstrap through posting, with all components validated and initialized.

---

**Last Updated**: 2026-02-28  
**System Version**: 1.0.0  
**Python Requirement**: 3.11+
