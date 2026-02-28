# Quick Guide: Verify Real YouTube Ingestion

## How to Know if It's Actually Fetching Real Videos

### Method 1: Check Ingestion Status Script

While the system is running, open a **NEW** terminal and run:

```powershell
$env:YOUTUBE_API_KEYS="YOUR_YOUTUBE_API_KEY_HERE"
$env:YOUTUBE_DATA_DIR="./data/raw/youtube"
py -3.11 check_ingestion_status.py
```

**What to look for:**
- ✅ `[OK] API Keys configured: 1 key(s)` = API key is set
- ✅ `[SUCCESS] Found X recently ingested video file(s)!` = **REAL VIDEOS ARE BEING FETCHED!**
- ⏳ `[WAITING] No recent video files found` = Still starting up (wait 1-2 minutes)

### Method 2: Watch the Logs

Look for these messages in the system output:

**Good signs (real ingestion):**
```
📥 Ingestion cycle 1: Status: running | Videos ingested: 5
📥 Ingestion cycle 2: Status: running | Videos ingested: 12
```

**Waiting (normal at start):**
```
📥 Ingestion cycle 1: Pipeline running (checking for data...)
   ⏳ Still waiting for YouTube API to return videos...
```

### Method 3: Check Data Files Directly

```powershell
# Check if video files are being created
Get-ChildItem -Path "./data/raw/youtube/videos" -Recurse -Filter "*.json" | 
    Sort-Object LastWriteTime -Descending | 
    Select-Object -First 5 Name, LastWriteTime
```

If you see recent files (last few minutes), **ingestion is working!**

### Method 4: Check File Contents

```powershell
# Look at a recent video file
$latest = Get-ChildItem -Path "./data/raw/youtube/videos" -Recurse -Filter "*.json" | 
    Sort-Object LastWriteTime -Descending | 
    Select-Object -First 1

if ($latest) {
    Write-Host "Latest video file: $($latest.FullName)"
    $content = Get-Content $latest.FullName | ConvertFrom-Json
    Write-Host "Video ID: $($content.video_id)"
    Write-Host "Title: $($content.title)"
    Write-Host "Views: $($content.views)"
}
```

## Timeline Expectations

- **0-30 seconds**: System starting up, no data yet (NORMAL)
- **30-60 seconds**: Ingestion should start fetching videos
- **1-2 minutes**: First video files should appear
- **2-5 minutes**: Multiple videos ingested, scoring can begin
- **5+ minutes**: Full pipeline running with real data

## Troubleshooting

### "NO API KEYS" in check script
- **Fix**: Set environment variable in the SAME terminal session
- Run: `$env:YOUTUBE_API_KEYS="YOUR_YOUTUBE_API_KEY_HERE"`

### "No video files found" after 2+ minutes
- Check if API key is valid (test with YouTube API directly)
- Check system logs for API errors
- Verify API quota hasn't been exceeded

### "Videos ingested: 0" in logs
- Ingestion may be waiting for cadence (1-hour minimum between searches)
- Check if scheduler is running: `Status: running`
- Wait a bit longer - first fetch can take time

## Quick Test Command

Run this to see everything at once:

```powershell
$env:YOUTUBE_API_KEYS="YOUR_YOUTUBE_API_KEY_HERE"
$env:YOUTUBE_DATA_DIR="./data/raw/youtube"
py -3.11 check_ingestion_status.py
```

If you see `[SUCCESS] Found X recently ingested video file(s)!` - **IT'S WORKING!** 🎉
