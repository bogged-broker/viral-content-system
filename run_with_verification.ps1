# Run full pipeline with API keys and verification
$env:YOUTUBE_API_KEYS="YOUR_YOUTUBE_API_KEY_HERE"
$env:YOUTUBE_DATA_DIR="./data/raw/youtube"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "FULL PIPELINE - REAL DATA ONLY" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "API Key: Configured" -ForegroundColor Yellow
Write-Host "Data Dir: $env:YOUTUBE_DATA_DIR" -ForegroundColor Yellow
Write-Host ""

Write-Host "Step 1: Verifying API key setup..." -ForegroundColor Yellow
py -3.11 check_ingestion_status.py

Write-Host ""
Write-Host "Step 2: Starting full pipeline..." -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

py -3.11 main.py --mode=full-system
