# Viral Content System

An autonomous, production-grade content generation system that ingests data from multiple platforms, extracts virality features, scores content potential, and generates optimized content at scale.

## Overview

This system is designed to operate as a complete pipeline for viral content creation:

1. **Ingestion** - Fetches real data from YouTube, Instagram, TikTok, and other platforms
2. **Feature Extraction** - Analyzes content to extract virality signals (engagement patterns, sentiment, visual features)
3. **Scoring** - Computes viral potential scores using ML models and trend analysis
4. **Generation** - Creates optimized content based on top-performing trends
5. **Posting** - Manages multi-platform content distribution with safety checks

## Key Components

### Ingestion Pipeline
- Fetches real content from multiple platforms via APIs
- Supports YouTube, Instagram, TikTok, Reddit
- Handles rate limiting, retries, and data validation

### Feature Extraction Engine
- Extracts multimodal features (text, video, audio, engagement)
- Builds dependency graphs for feature computation
- Tracks feature lineage and versioning

### Scoring System
- Computes viral potential scores using ML models
- Tracks trends and velocity signals
- Supports real-time and batch scoring

### Content Generation
- Generates scripts, visuals, and audio
- Optimizes for retention and engagement
- Uses RL agents to learn from performance

### Posting System
- Multi-platform posting with safety checks
- Account health monitoring
- Cadence management and risk evaluation

### Infrastructure
- Observability: Prometheus metrics, Grafana dashboards, health endpoints
- Persistence: Multiple backends (filesystem, memory, Redis, Postgres)
- Recovery: Automatic failure detection and repair
- Lineage: Complete data lineage tracking for reproducibility

## Getting Started

### Prerequisites

- Python 3.11+
- YouTube API key (optional, for real data ingestion)

### Installation

```bash
pip install -r requirements.txt
```

### Configuration

Set your API keys via environment variables:

```powershell
$env:YOUTUBE_API_KEYS="YOUR_API_KEY_HERE"
$env:YOUTUBE_DATA_DIR="./data/raw/youtube"
```

Or use the setup script:

```powershell
.\setup_api_keys.ps1
```

### Running the System

**Full system (all components):**
```bash
python main.py --mode=full-system
```

**Individual components:**
```bash
python main.py --mode=ingest      # Only ingestion
python main.py --mode=generate    # Only generation
python main.py --mode=post        # Only posting
python main.py --mode=train       # Only training
```

## Architecture

The system follows a modular, pipeline-based architecture:

- **Orchestration Layer**: `SystemOrchestrator` manages component lifecycle and data flow
- **Data Flow**: Ingestion → Feature Extraction → Scoring → Generation → Posting
- **Observability**: Built-in metrics, health checks, and tracing
- **Recovery**: Automatic failure detection and repair strategies
- **Lineage**: Complete data lineage for reproducibility and debugging

## Environment Configuration

The system supports multiple environments via YAML configs:

- `config/environments/development.yaml`
- `config/environments/staging.yaml`
- `config/environments/production.yaml`

## License

See `license` file for details.
