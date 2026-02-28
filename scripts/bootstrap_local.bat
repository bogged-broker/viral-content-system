@echo off
REM Bootstrap Local Environment (Windows)
REM Sets up local development environment and validates prerequisites

echo ==========================================
echo Bootstrap Local Environment
echo ==========================================

REM Check Python version
echo [1/5] Checking Python version...
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python 3.8+
    exit /b 1
)
python --version
echo ✓ Python found

REM Check if virtual environment exists
echo [2/5] Setting up virtual environment...
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
)

REM Activate virtual environment
call venv\Scripts\activate.bat
echo ✓ Virtual environment activated

REM Install dependencies
echo [3/5] Installing dependencies...
if exist "requirements.txt" (
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    echo ✓ Dependencies installed
) else (
    echo WARNING: requirements.txt not found
)

REM Create necessary directories
echo [4/5] Creating necessary directories...
if not exist "data" mkdir data
if not exist "logs" mkdir logs
if not exist "checkpoints" mkdir checkpoints
if not exist "processed" mkdir processed
echo ✓ Directories created

REM Validate environment
echo [5/5] Validating environment...
python -c "from infra.bootstrap import bootstrap; result = bootstrap(); exit(0 if result.success else 1)" 2>nul
if errorlevel 1 (
    echo WARNING: Bootstrap validation had issues
) else (
    echo ✓ Bootstrap validation passed
)

echo.
echo ==========================================
echo Bootstrap Complete!
echo ==========================================
echo.
echo Next steps:
echo   1. Configure environment variables in .env
echo   2. Set up API keys
echo   3. Run: scripts\run_e2e.bat
echo.
