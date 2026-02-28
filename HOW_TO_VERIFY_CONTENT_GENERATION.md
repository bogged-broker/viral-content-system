# How to Verify Content Generation

## What I Just Changed

1. **Generation cycle limit**: Set to **10 cycles maximum** (will stop after 10 generations)
2. **Enhanced logging**: Now shows detailed info about each generated item
3. **Content summary**: Shows summary when generation completes

## How to Know if Content is Being Created

### Method 1: Watch the Logs (Real-time)

When you run the system, watch for these log messages:

```
🎬 Generation cycle 1: Creating content...
  ✓ Generated 1 content items from trends
    [1] script: item_1_1
        Title: How to Make Viral Content...
        Script preview: [Scene 1] Welcome to this tutorial...
```

**What to look for:**
- ✅ `✓ Generated X content items` = Content is being created
- ✅ `Script preview:` = Shows actual generated script text
- ✅ `Title:` = Shows generated content titles
- ⚠️ `⚠️ No trends available` = Waiting for real data (may need more time)

### Method 2: Check the Summary at the End

After 10 cycles, you'll see:

```
============================================================
Generation loop completed: 10 cycles, 5 items generated
============================================================
Generated content summary:
  [1] script
      Title: How to Make Viral Content
      ID: item_1_1
  [2] script
      Title: Top 10 Tips for...
```

### Method 3: Use the Check Script

Run this to see what content files were created:

```powershell
py -3.11 check_generated_content.py
```

This will:
- Check common output directories
- Show generated files
- Display previews of generated content
- List recent JSON files that might contain generated content

### Method 4: Check Data Directories

Look in these directories for generated content:

```powershell
# Check for generated scripts
Get-ChildItem -Recurse -Filter "*.json" | Where-Object {$_.LastWriteTime -gt (Get-Date).AddMinutes(-10)} | Select-Object FullName, LastWriteTime

# Check generation output directory
Get-ChildItem -Path "./generation/output" -Recurse -ErrorAction SilentlyContinue

# Check data directory
Get-ChildItem -Path "./data/generated" -Recurse -ErrorAction SilentlyContinue
```

## Understanding the Output

### Real Content Generation (Good!)
```
🎬 Generation cycle 1: Creating content...
  ✓ Generated 1 content items from trends
    [1] script: item_1_1
        Title: How to Make Viral Content in 2025
        Script preview: [Scene 1] Welcome! Today we're going to learn...
```

This means:
- ✅ Real data was ingested
- ✅ Content was scored
- ✅ Trends were identified
- ✅ Content was generated from those trends

### Waiting for Data (Normal at Start)
```
🎬 Generation cycle 1: Creating content...
  ⚠️  No trends available for generation (waiting for scored content)
```

This means:
- System is running but needs more time
- Ingestion needs to fetch real YouTube data first
- Scoring needs to process that data
- Then generation can use the trends

**Solution:** Wait a few minutes for the pipeline to process data.

### Mock Mode (No Real Data)
```
🎬 Generation cycle 1: Creating content...
  ✓ Generated script with 3 scenes
        Script preview: [Scene 1] Introduction...
```

This means:
- Using fallback script generator
- No real trends available
- Check if API keys are configured

## Current Settings

- **Max generation cycles**: 10 (will stop automatically)
- **Cycle interval**: 10 seconds between generations
- **Logging level**: INFO (shows all generation details)

## To See More Detail

If you want even more detail, you can check the full logs. The system logs everything to the console, so just watch the output while it runs.

## Troubleshooting

### "No trends available"
- **Cause**: System needs time to ingest and score real data
- **Solution**: Wait 2-5 minutes for the pipeline to process

### "No content was generated"
- **Cause**: No real data was ingested (check API keys)
- **Solution**: Verify `YOUTUBE_API_KEYS` is set correctly

### Generation stops immediately
- **Cause**: May have hit an error
- **Solution**: Check error messages in logs

## Next Steps

After the 10 cycles complete:
1. Check the summary output
2. Run `check_generated_content.py` to see files
3. Review logs for any errors
4. If content was generated, you'll see it in the summary!
