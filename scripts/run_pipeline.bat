@echo off
REM Run Pipeline (Windows)
REM Executes the full pipeline end-to-end

setlocal

REM Parse mode argument (default: full-system)
set MODE=%1
if "%MODE%"=="" set MODE=full-system

echo ==========================================
echo Running Pipeline - Mode: %MODE%
echo ==========================================
echo.

REM Activate virtual environment if it exists
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

REM Run the pipeline
python main.py --mode=%MODE%

echo.
echo ==========================================
echo Pipeline Complete
echo ==========================================
