# Step-by-Step Guide: Getting YouTube API Keys

This guide will walk you through getting YouTube Data API v3 keys to enable real data ingestion.

## Prerequisites
- A Google account (Gmail account)
- Access to Google Cloud Console

---

## Step 1: Go to Google Cloud Console

1. Open your web browser
2. Navigate to: **https://console.cloud.google.com/**
3. Sign in with your Google account if prompted

---

## Step 2: Create a New Project (or Select Existing)

1. At the top of the page, click the **project dropdown** (it may show "Select a project" or an existing project name)
2. Click **"NEW PROJECT"** button
3. Enter a project name:
   - Example: `viral-content-system`
   - Or: `youtube-data-scraper`
4. Click **"CREATE"**
5. Wait a few seconds for the project to be created
6. Select your new project from the dropdown at the top

**Note:** If you already have a project you want to use, just select it from the dropdown.

---

## Step 3: Enable YouTube Data API v3

1. In the left sidebar, click **"APIs & Services"** → **"Library"**
   - (Or search for "API Library" in the top search bar)
2. In the search box, type: **"YouTube Data API v3"**
3. Click on **"YouTube Data API v3"** from the results
4. Click the blue **"ENABLE"** button
5. Wait for it to enable (may take 10-30 seconds)

---

## Step 4: Create API Credentials

1. After enabling the API, you'll see a page with API details
2. Click **"CREATE CREDENTIALS"** button (top right, or in the middle of the page)
3. A popup will appear asking "What credentials do you need?"
4. Select:
   - **"Which API are you using?"** → Choose **"YouTube Data API v3"**
   - **"Where will you be calling the API from?"** → Choose **"Other UI (e.g. Windows, CLI tool)"**
   - **"What data will you be accessing?"** → Choose **"Public data"**
5. Click **"NEXT"**
6. Click **"CREATE API KEY"**

---

## Step 5: Copy Your API Key

1. A popup will appear showing your API key
2. **IMPORTANT:** Copy the API key immediately - it looks like:
   ```
   AIzaSyBxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
3. Click **"CLOSE"** (don't click "RESTRICT KEY" yet - we'll do that later)

---

## Step 6: (Optional but Recommended) Restrict Your API Key

This prevents others from using your key if it gets exposed.

1. Go to **"APIs & Services"** → **"Credentials"** (in left sidebar)
2. Find your API key in the list and click on it
3. Under **"API restrictions"**:
   - Select **"Restrict key"**
   - Choose **"YouTube Data API v3"** from the dropdown
4. Under **"Application restrictions"**:
   - Select **"None"** (for now, since we're running from command line)
   - Or **"IP addresses"** if you know your IP
5. Click **"SAVE"** at the bottom

---

## Step 7: Set Up Multiple API Keys (Optional but Recommended)

YouTube API has quota limits. Having multiple keys helps:
- **Default quota:** 10,000 units per day per key
- **Each search:** ~100 units
- **Each video details:** ~1 unit

To create additional keys:
1. Go to **"APIs & Services"** → **"Credentials"**
2. Click **"+ CREATE CREDENTIALS"** → **"API key"**
3. Repeat Step 6 to restrict it
4. Copy the new key

---

## Step 8: Configure Your System

Now that you have your API key(s), set them up in your system:

### Option A: Set Environment Variable (Temporary - Current Session Only)

**Windows PowerShell:**
```powershell
$env:YOUTUBE_API_KEYS="AIzaSyBxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
$env:YOUTUBE_DATA_DIR="./data/raw/youtube"
```

**Windows Command Prompt:**
```cmd
set YOUTUBE_API_KEYS=AIzaSyBxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
set YOUTUBE_DATA_DIR=./data/raw/youtube
```

**If you have multiple keys (comma-separated):**
```powershell
$env:YOUTUBE_API_KEYS="key1,key2,key3"
```

### Option B: Set Environment Variable Permanently (Recommended)

**Windows:**
1. Press `Win + R`
2. Type `sysdm.cpl` and press Enter
3. Click **"Advanced"** tab
4. Click **"Environment Variables"**
5. Under **"User variables"**, click **"New"**
6. Variable name: `YOUTUBE_API_KEYS`
7. Variable value: `your_api_key_here` (or `key1,key2` for multiple)
8. Click **"OK"** on all dialogs
9. **Restart your terminal/PowerShell** for changes to take effect

**Linux/Mac:**
Add to `~/.bashrc` or `~/.zshrc`:
```bash
export YOUTUBE_API_KEYS="your_api_key_here"
export YOUTUBE_DATA_DIR="./data/raw/youtube"
```

Then run: `source ~/.bashrc` (or `source ~/.zshrc`)

---

## Step 9: Verify Your Setup

1. Open a **new** terminal/PowerShell window
2. Run:
   ```powershell
   echo $env:YOUTUBE_API_KEYS
   ```
   (or `echo %YOUTUBE_API_KEYS%` in CMD)
3. You should see your API key printed

---

## Step 10: Test Your API Key

Test if your key works:

**Windows PowerShell:**
```powershell
$apiKey = $env:YOUTUBE_API_KEYS.Split(",")[0]
Invoke-WebRequest -Uri "https://www.googleapis.com/youtube/v3/search?part=snippet&q=test&key=$apiKey" | Select-Object -ExpandProperty Content
```

If it works, you'll see JSON data. If you see an error, check:
- Is the API key correct?
- Is YouTube Data API v3 enabled?
- Did you wait a few minutes after enabling? (Sometimes there's a delay)

---

## Step 11: Run Your System

Now run your system with real data:

```powershell
py -3.11 main.py --mode=full-system
```

You should see:
- ✅ `✓ YouTube scraper configured with X API key(s)` (instead of warning)
- ✅ Real data being ingested
- ✅ Real scores computed

---

## Troubleshooting

### "API key not valid"
- Double-check you copied the entire key
- Make sure YouTube Data API v3 is enabled
- Wait 5-10 minutes after enabling the API

### "Quota exceeded"
- You've hit the daily limit (10,000 units)
- Wait 24 hours, or create additional API keys
- Check quota usage: **APIs & Services** → **Dashboard** → **YouTube Data API v3**

### "Access denied"
- Check if you restricted the API key too much
- Try creating a new unrestricted key for testing

### Environment variable not found
- Make sure you set it in the same terminal session
- Or set it permanently and restart your terminal
- Verify with: `echo $env:YOUTUBE_API_KEYS`

---

## Security Best Practices

1. **Never commit API keys to Git**
   - Add to `.gitignore`: `*.env`, `config/api_keys.txt`
   
2. **Use environment variables** (not hardcoded in files)

3. **Restrict API keys** to only YouTube Data API v3

4. **Rotate keys** if you suspect they're compromised

5. **Monitor usage** in Google Cloud Console

---

## Next Steps

Once your API keys are configured:
1. The system will automatically start fetching real YouTube data
2. Check `./data/raw/youtube/` for ingested data
3. Monitor logs for real scores and features
4. See `SETUP_REAL_DATA.md` for more details

---

## Need Help?

- Google Cloud Console Help: https://cloud.google.com/docs
- YouTube Data API Docs: https://developers.google.com/youtube/v3
- API Quota Info: https://developers.google.com/youtube/v3/getting-started#quota
