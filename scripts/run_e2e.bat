@echo off
REM Run End-to-End Test (Windows)
REM Executes the full system end-to-end test

setlocal

echo ==========================================
echo Running End-to-End Test
echo ==========================================
echo.

REM Activate virtual environment if it exists
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

REM Run e2e test
if exist "tests\e2e_test_full_pipeline.py" (
    python -m pytest tests\e2e_test_full_pipeline.py -v
) else (
    echo Running full system in test mode...
    python main.py --mode=full-system --log-level=INFO
)

echo.
echo ==========================================
echo End-to-End Test Complete
echo ==========================================
