#!/usr/bin/env python3
"""Check if ingestion is actually fetching real YouTube videos."""
import os
import json
from pathlib import Path
from datetime import datetime, timedelta

print("=" * 60)
print("Checking Ingestion Status - Real YouTube Data")
print("=" * 60)

# Check API key
api_keys = os.getenv("YOUTUBE_API_KEYS", "")
if api_keys:
    keys = [k.strip() for k in api_keys.split(",") if k.strip()]
    print(f"[OK] API Keys configured: {len(keys)} key(s)")
    print(f"     First key: {keys[0][:20]}...")
else:
    print("[X] NO API KEYS - Ingestion cannot fetch real data!")
    print("    Set YOUTUBE_API_KEYS environment variable")

print()

# Check data directory
data_dir = Path(os.getenv("YOUTUBE_DATA_DIR", "./data/raw/youtube"))
print(f"Data directory: {data_dir}")
print(f"Exists: {data_dir.exists()}")

if data_dir.exists():
    # Check for video files
    video_dirs = [
        data_dir / "videos",
        data_dir / "videos" / "search",
        data_dir / "videos" / "trending",
    ]
    
    video_files = []
    for video_dir in video_dirs:
        if video_dir.exists():
            # Find recent JSON files (last 10 minutes)
            recent_time = datetime.now() - timedelta(minutes=10)
            for json_file in video_dir.rglob("*.json"):
                try:
                    mtime = datetime.fromtimestamp(json_file.stat().st_mtime)
                    if mtime > recent_time:
                        video_files.append((json_file, mtime))
                except:
                    pass
    
    if video_files:
        print(f"\n[SUCCESS] Found {len(video_files)} recently ingested video file(s)!")
        print("          Ingestion IS working and fetching real YouTube videos!")
        print("\nRecent files:")
        for file_path, mtime in sorted(video_files, key=lambda x: x[1], reverse=True)[:10]:
            print(f"  {file_path.name} (ingested: {mtime.strftime('%H:%M:%S')})")
            # Try to read and show video ID
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if 'video_id' in data:
                        print(f"    Video ID: {data['video_id']}")
                    if 'title' in data:
                        print(f"    Title: {data['title'][:60]}...")
            except:
                pass
    else:
        print("\n[WAITING] No recent video files found")
        print("          Ingestion may still be starting up...")
        print("          Check again in 1-2 minutes")
        
        # Check for any video files at all
        all_video_files = []
        for video_dir in video_dirs:
            if video_dir.exists():
                for json_file in video_dir.rglob("*.json"):
                    all_video_files.append(json_file)
        
        if all_video_files:
            print(f"\n  Found {len(all_video_files)} total video file(s) (may be old)")
            print("  Most recent:")
            for file_path in sorted(all_video_files, key=lambda p: p.stat().st_mtime, reverse=True)[:3]:
                mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                print(f"    {file_path.name} ({mtime.strftime('%Y-%m-%d %H:%M:%S')})")
        else:
            print("  No video files found at all - ingestion hasn't saved anything yet")
else:
    print(f"\n[WARNING] Data directory doesn't exist: {data_dir}")
    print("          Ingestion will create it when it starts")

# Check for channel files
channel_dir = data_dir / "channels"
if channel_dir.exists():
    channel_files = list(channel_dir.glob("*.json"))
    if channel_files:
        print(f"\n[OK] Found {len(channel_files)} channel metadata file(s)")

# Check for snapshots
snapshot_dir = data_dir / "snapshots"
if snapshot_dir.exists():
    snapshot_files = list(snapshot_dir.rglob("*.json"))
    if snapshot_files:
        print(f"[OK] Found {len(snapshot_files)} snapshot file(s)")

print("\n" + "=" * 60)
print("How to verify ingestion is working:")
print("  1. Check this script output for 'Found X recently ingested video file(s)'")
print("  2. Watch system logs for 'Ingestion cycle X' messages")
print("  3. Check data directory: " + str(data_dir))
print("=" * 60)
