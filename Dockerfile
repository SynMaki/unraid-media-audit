# =============================================================================
# Media Audit Docker Image
# Unraid-friendly container with web UI for media auditing
# =============================================================================

FROM python:3.11-slim-bookworm

# Labels for Unraid Community Applications
LABEL maintainer="Media Audit Project" \
      org.opencontainers.image.title="Media Audit" \
      org.opencontainers.image.description="Scan media libraries for duplicates with qBittorrent integration" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.source="https://github.com/your-repo/media-audit"

# =============================================================================
# Install system dependencies
# =============================================================================

RUN apt-get update && apt-get install -y --no-install-recommends \
    # ffprobe/ffmpeg for media analysis
    ffmpeg \
    # Useful utilities
    curl \
    ca-certificates \
    gosu \
    tini \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# =============================================================================
# Create non-root user structure (for PUID/PGID support)
# =============================================================================

# Create a placeholder user that will be modified at runtime
RUN groupadd -g 1000 appgroup && \
    useradd -u 1000 -g appgroup -m -s /bin/bash appuser && \
    mkdir -p /app /config /reports /media && \
    chown -R appuser:appgroup /app /config /reports

# =============================================================================
# Install Python dependencies
# =============================================================================

WORKDIR /app

# Copy requirements first for better caching
COPY app/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# =============================================================================
# Copy application files
# =============================================================================

COPY app/ /app/
COPY entrypoint.sh /entrypoint.sh

# Make scripts executable
RUN chmod +x /entrypoint.sh && \
    chmod +x /app/media_audit.py && \
    chown -R appuser:appgroup /app

# =============================================================================
# Environment variables with safe defaults
# =============================================================================

# Web server settings
ENV HOST=0.0.0.0 \
    PORT=8080

# Authentication (empty = no auth required, for local network use)
ENV WEB_USER="" \
    WEB_PASS=""

# Report and media paths
ENV REPORT_DIR=/reports \
    ROOTS=/media \
    DELETE_UNDER=/media

# qBittorrent settings (empty = disabled)
ENV QBIT_HOST="" \
    QBIT_PORT=8080 \
    QBIT_USER="" \
    QBIT_PASS="" \
    QBIT_PATH_MAP="" \
    QBIT_WEBUI_URL=""

# Sonarr/Radarr settings (empty = disabled)
# For multiple instances, use JSON or numbered format:
# - SONARR_INSTANCES_JSON='[{"name":"main","url":"http://...","api_key":"..."}]'
# - SONARR_1_URL, SONARR_1_APIKEY, SONARR_1_NAME, SONARR_1_PATH_MAP
# - RADARR_1_URL, RADARR_1_APIKEY, etc.
ENV SONARR_URL="" \
    SONARR_APIKEY="" \
    SONARR_NAME="sonarr" \
    SONARR_PATH_MAP="" \
    RADARR_URL="" \
    RADARR_APIKEY="" \
    RADARR_NAME="radarr" \
    RADARR_PATH_MAP="" \
    NO_SERVARR=false

# Audit settings
ENV FFPROBE_SCOPE=dupes \
    CONTENT_TYPE=auto \
    AVOID_MODE=if-no-prefer \
    AVOID_AUDIO_LANG=""

# Safety settings
ENV ALLOW_DELETE=false \
    SCHEDULE_ENABLED=false \
    SCHEDULE_CRON="0 3 * * *"

# User ID mapping (Unraid pattern)
ENV PUID=99 \
    PGID=100 \
    UMASK=022

# =============================================================================
# Expose ports and volumes
# =============================================================================

EXPOSE 8080

# Volumes
VOLUME ["/config", "/reports", "/media"]

# =============================================================================
# Entrypoint and command
# =============================================================================

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["python", "-m", "webapp.main"]
