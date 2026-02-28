# GOOGLE-LEVEL: Immutable Audio Extractor Container
# FFmpeg version pinned for reproducibility: 4.4.2
FROM ubuntu:22.04

# Environment variables for build reproducibility
ENV DEBIAN_FRONTEND=noninteractive
ENV FFMPEG_VERSION=4.4.2
ENV BUILD_HASH=unknown
ENV CONTAINER_IMAGE_DIGEST=unknown
ENV PYTHON_VERSION=3.10

# Build metadata for immutable builds
LABEL build.hash="${BUILD_HASH}"
LABEL ffmpeg.version="${FFMPEG_VERSION}"
LABEL container.image.digest="${CONTAINER_IMAGE_DIGEST}"
LABEL build.timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LABEL extractor.version="1.0.0"

# Install system dependencies
RUN apt-get update && apt-get install -y \
    python3.10 \
    python3.10-pip \
    python3.10-dev \
    build-essential \
    pkg-config \
    libavformat-dev \
    libavcodec-dev \
    libavutil-dev \
    libswscale-dev \
    libavfilter-dev \
    libavdevice-dev \
    wget \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install FFmpeg with pinned version
RUN wget -O ffmpeg.tar.bz2 "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.bz2" \
    && tar -xjf ffmpeg.tar.bz2 \
    && cd "ffmpeg-${FFMPEG_VERSION}" \
    && ./configure \
        --prefix=/usr/local \
        --enable-gpl \
        --enable-version3 \
        --enable-nonfree \
        --enable-libmp3lame \
        --enable-libopus \
        --enable-libvorbis \
        --enable-libx264 \
        --enable-libx265 \
        --disable-doc \
        --disable-programs \
    && make -j$(nproc) \
    && make install \
    && cd .. \
    && rm -rf "ffmpeg-${FFMPEG_VERSION}" ffmpeg.tar.bz2

# Verify FFmpeg installation
RUN ffmpeg -version

# Create application directory
WORKDIR /app

# Copy Python requirements
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy application code
COPY audio_extractor.py .
COPY video_downloader.py .
COPY factory_manager.py .
COPY factory_registry.py .
COPY factory_metrics.py .
COPY instagram_scraper.py .

# Create data directories
RUN mkdir -p /app/data/raw/video /app/data/raw/audio /app/data/storage /app/data/quarantine

# Set environment variables for container mode
ENV CONTAINER_MODE=true
ENV POD_NAME=audio-extractor
ENV POD_NAMESPACE=default
ENV POD_UID=1000

# Health check endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8088/healthz || exit 1

# Expose ports
EXPOSE 8088 8000

# Run as non-root user
RUN useradd -m -u ${POD_UID} extractor
USER extractor

# Start command
CMD ["python3", "audio_extractor.py", "--serve"]
