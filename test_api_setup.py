#!/usr/bin/env python3
"""Test if API keys are properly configured."""
import os
import sys

print("=" * 60)
print("Testing YouTube API Key Configuration")
print("=" * 60)

api_keys = os.getenv("YOUTUBE_API_KEYS", "").split(",")
api_keys = [k.strip() for k in api_keys if k.strip()]

if api_keys:
    print(f"[OK] Found {len(api_keys)} API key(s)")
    print(f"  First key: {api_keys[0][:20]}...")
    print("")
    print("SUCCESS: Real data mode will be enabled!")
    print("The system will fetch real YouTube data.")
else:
    print("[X] No API keys found")
    print("")
    print("WARNING: System will run in MOCK MODE")
    print("Set YOUTUBE_API_KEYS environment variable to enable real data.")

data_dir = os.getenv("YOUTUBE_DATA_DIR", "./data/raw/youtube")
print(f"\nData directory: {data_dir}")

print("=" * 60)
