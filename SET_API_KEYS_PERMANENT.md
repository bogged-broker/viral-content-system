# Setting API Keys Permanently (Windows)

To set your YouTube API key permanently so you don't have to set it every time:

## Method 1: Using System Properties (Recommended)

1. Press `Win + R`
2. Type: `sysdm.cpl` and press Enter
3. Click the **"Advanced"** tab
4. Click **"Environment Variables"** button (bottom)
5. Under **"User variables"** (top section), click **"New..."**
6. Enter:
   - **Variable name:** `YOUTUBE_API_KEYS`
   - **Variable value:** `YOUR_YOUTUBE_API_KEY_HERE`
7. Click **"OK"**
8. Click **"New..."** again
9. Enter:
   - **Variable name:** `YOUTUBE_DATA_DIR`
   - **Variable value:** `./data/raw/youtube`
10. Click **"OK"** on all dialogs
11. **Close and reopen** your terminal/PowerShell for changes to take effect

## Method 2: Using PowerShell (Current User Only)

Run this in PowerShell (as Administrator if needed):

```powershell
[System.Environment]::SetEnvironmentVariable('YOUTUBE_API_KEYS', 'YOUR_YOUTUBE_API_KEY_HERE', 'User')
[System.Environment]::SetEnvironmentVariable('YOUTUBE_DATA_DIR', './data/raw/youtube', 'User')
```

Then restart your terminal.

## Verify It's Set

Open a **new** PowerShell window and run:

```powershell
echo $env:YOUTUBE_API_KEYS
```

You should see your API key.

## Quick Setup Script

Or just run the provided script each time you open a new terminal:

```powershell
.\setup_api_keys.ps1
```

This sets the variables for the current session only.
