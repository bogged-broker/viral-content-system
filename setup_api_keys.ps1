# PowerShell script to set YouTube API keys
# Run this before starting the system: .\setup_api_keys.ps1

$env:YOUTUBE_API_KEYS="YOUR_YOUTUBE_API_KEY_HERE"
$env:YOUTUBE_DATA_DIR="./data/raw/youtube"

Write-Host "=" -NoNewline
Write-Host ("=" * 59) -ForegroundColor Cyan
Write-Host "YouTube API Keys Configured" -ForegroundColor Green
Write-Host "=" -NoNewline
Write-Host ("=" * 59) -ForegroundColor Cyan
Write-Host ""
if ($env:YOUTUBE_API_KEYS -ne "YOUR_YOUTUBE_API_KEY_HERE") {
    Write-Host "API Key: $($env:YOUTUBE_API_KEYS.Substring(0, 20))..." -ForegroundColor Yellow
} else {
    Write-Host "API Key: NOT CONFIGURED (using placeholder)" -ForegroundColor Red
    Write-Host "Please set your actual API key before running the system!" -ForegroundColor Red
}
Write-Host "Data Directory: $env:YOUTUBE_DATA_DIR" -ForegroundColor Yellow
Write-Host ""
Write-Host "You can now run:" -ForegroundColor White
Write-Host "  py -3.11 main.py --mode=full-system" -ForegroundColor Cyan
Write-Host ""
Write-Host "Note: These environment variables are set for this session only." -ForegroundColor Gray
Write-Host "To set permanently, use Windows System Properties > Environment Variables" -ForegroundColor Gray
Write-Host ""
