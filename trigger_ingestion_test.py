#!/usr/bin/env python3
"""Test triggering YouTube ingestion directly."""
import os
import asyncio
import sys
from pathlib import Path

api_key = os.getenv("YOUTUBE_API_KEYS", "").split(",")[0].strip()
data_dir = os.getenv("YOUTUBE_DATA_DIR", "./data/raw/youtube")

if not api_key:
    print("[ERROR] Set YOUTUBE_API_KEYS environment variable")
    sys.exit(1)

print("=" * 60)
print("Testing Direct YouTube Ingestion")
print("=" * 60)
print(f"API Key: {api_key[:20]}...")
print(f"Data Dir: {data_dir}")
print()

try:
    from ingestion.platform_scrapers.youtube_scraper import YouTubeScraper
    
    print("Creating YouTube scraper...")
    from ingestion.platform_scrapers.youtube_scraper import ScrapeMode
    
    scraper = YouTubeScraper(
        api_keys=[api_key],
        mode=ScrapeMode.SEARCH.value,  # Use enum value
        data_dir=data_dir
    )
    
    print("Running scraper with query 'viral shorts'...")
    print("This should fetch and save real YouTube videos...")
    print()
    
    # Run the scraper directly
    count = scraper.run(query='viral shorts', max_results=5)
    
    print()
    print("=" * 60)
    if count > 0:
        print(f"[SUCCESS] Ingested {count} videos!")
        print(f"Check {data_dir}/videos/search/ for saved files")
        
        # Check for saved files
        video_dir = Path(data_dir) / "videos" / "search"
        if video_dir.exists():
            files = list(video_dir.rglob("*.json"))
            if files:
                print(f"\nFound {len(files)} video file(s):")
                for f in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:3]:
                    print(f"  {f.name}")
    else:
        print("[WARNING] No videos were ingested")
        print("Check logs above for errors")
    print("=" * 60)
    
except Exception as e:
    print(f"[ERROR] {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
