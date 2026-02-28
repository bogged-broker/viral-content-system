#!/usr/bin/env python3
"""Load ingested video files from disk."""
import json
import os
from pathlib import Path
from datetime import datetime, timedelta

def load_ingested_videos(data_dir="./data/raw/youtube", limit=10):
    """Load recently ingested video files."""
    data_path = Path(data_dir)
    videos = []
    
    # Check all video directories
    for mode_dir in [data_path / "videos" / "search", data_path / "videos" / "trending"]:
        if not mode_dir.exists():
            continue
            
        # Get recent files (last hour)
        recent_time = datetime.now() - timedelta(hours=1)
        for json_file in mode_dir.rglob("*.json"):
            try:
                mtime = datetime.fromtimestamp(json_file.stat().st_mtime)
                if mtime > recent_time:
                    with open(json_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        # Convert to format expected by scoring
                        video_data = {
                            'id': data.get('video_id', ''),
                            'video_id': data.get('video_id', ''),
                            'views': data.get('views', 0),
                            'likes': data.get('likes', 0),
                            'comments': data.get('comments', 0),
                            'shares': data.get('shares', 0),
                            'platform': 'youtube',
                            'title': data.get('title', ''),
                            'channel_id': data.get('channel_id', ''),
                            'upload_timestamp': data.get('upload_timestamp', ''),
                            'raw_data': data  # Keep full data for feature extraction
                        }
                        videos.append(video_data)
            except Exception as e:
                continue
    
    # Sort by ingestion time (newest first)
    videos.sort(key=lambda x: x.get('upload_timestamp', ''), reverse=True)
    return videos[:limit]

if __name__ == "__main__":
    data_dir = os.getenv("YOUTUBE_DATA_DIR", "./data/raw/youtube")
    videos = load_ingested_videos(data_dir)
    print(f"Loaded {len(videos)} ingested videos")
    for v in videos[:3]:
        print(f"  {v['video_id']}: {v['title'][:50]}... (views: {v['views']})")
