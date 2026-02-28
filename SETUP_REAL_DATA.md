# Setting Up Real Data Ingestion

## Current Status: MOCK MODE

The system is currently running in **MOCK MODE** because no API keys are configured. This means:
- ❌ No real YouTube data is being fetched
- ❌ Scoring uses test/fake data
- ❌ Feature extraction has no real content to process
- ❌ Generation has no real trends to work with

## To Enable Real Data

### 1. Get YouTube API Keys

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing
3. Enable "YouTube Data API v3"
4. Create credentials (API Key)
5. Copy your API key(s)

### 2. Set Environment Variables

**Windows (PowerShell):**
```powershell
$env:YOUTUBE_API_KEYS="your_api_key_1,your_api_key_2"
$env:YOUTUBE_DATA_DIR="./data/raw/youtube"
```

**Windows (Command Prompt):**
```cmd
set YOUTUBE_API_KEYS=your_api_key_1,your_api_key_2
set YOUTUBE_DATA_DIR=./data/raw/youtube
```

**Linux/Mac:**
```bash
export YOUTUBE_API_KEYS="your_api_key_1,your_api_key_2"
export YOUTUBE_DATA_DIR="./data/raw/youtube"
```

### 3. Run the System

```bash
py -3.11 main.py --mode=full-system
```

You should now see:
- ✅ "YouTube scraper configured with X API key(s)"
- ✅ Real data being ingested
- ✅ Real scores computed from actual video metrics
- ✅ Real features extracted from actual content

## Verifying Real Data

Check the logs for:
- `✓ YouTube scraper configured with X API key(s)` (not "⚠️ NO YOUTUBE API KEYS")
- `✓ Computed scores for X items` (not "⚠️ MOCK SCORE")
- `✓ Extracted features from X items` (not "No data available")

## Data Storage

Real data will be stored in:
- `./data/raw/youtube/videos/` - Raw video records
- `./data/raw/youtube/channels/` - Channel metadata
- `./data/raw/youtube/snapshots/` - Time-series performance snapshots

## API Quota Limits

YouTube Data API v3 has daily quotas:
- Default: 10,000 units per day
- Each search request: ~100 units
- Each video details request: ~1 unit

Monitor your quota usage in Google Cloud Console.
