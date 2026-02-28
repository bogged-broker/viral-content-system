#!/usr/bin/env python3
"""Test YouTube API directly to verify it's working."""
import os
import sys

api_key = os.getenv("YOUTUBE_API_KEYS", "").split(",")[0].strip()

if not api_key:
    print("[ERROR] No API key found in YOUTUBE_API_KEYS")
    sys.exit(1)

print("=" * 60)
print("Testing YouTube API Directly")
print("=" * 60)
print(f"API Key: {api_key[:20]}...")
print()

try:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    
    print("Building YouTube API client...")
    youtube = build('youtube', 'v3', developerKey=api_key, cache_discovery=False)
    
    print("Searching for videos (test query: 'viral shorts')...")
    request = youtube.search().list(
        part='id,snippet',
        q='viral shorts',
        type='video',
        videoDuration='short',
        maxResults=3
    )
    
    response = request.execute()
    
    if response.get('items'):
        print(f"\n[SUCCESS] API is working! Found {len(response['items'])} videos")
        print("\nSample videos:")
        for i, item in enumerate(response['items'], 1):
            video_id = item['id']['videoId']
            title = item['snippet']['title']
            # Handle Unicode in titles
        try:
            title_preview = title[:60]
        except:
            title_preview = str(title)[:60]
        print(f"  [{i}] {video_id}: {title_preview}...")
        print("\n✓ YouTube API is accessible and working!")
        print("  Ingestion should be able to fetch videos.")
    else:
        print("\n[WARNING] API call succeeded but no videos returned")
        
except ImportError as e:
    print(f"\n[ERROR] Missing dependency: {e}")
    print("  Install with: pip install google-api-python-client")
    sys.exit(1)
except HttpError as e:
    print(f"\n[ERROR] YouTube API error: {e}")
    if e.resp.status == 403:
        print("  Possible causes:")
        print("  - API key is invalid")
        print("  - API key quota exceeded")
        print("  - YouTube Data API v3 not enabled")
    elif e.resp.status == 400:
        print("  Possible causes:")
        print("  - Invalid request parameters")
    sys.exit(1)
except Exception as e:
    print(f"\n[ERROR] Unexpected error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("=" * 60)
