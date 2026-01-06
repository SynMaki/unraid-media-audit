#!/bin/bash
# =============================================================================
# Media Audit Container Entrypoint
# Handles PUID/PGID mapping for Unraid compatibility
# =============================================================================

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() {
    echo -e "${GREEN}[media-audit]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[media-audit]${NC} $1"
}

error() {
    echo -e "${RED}[media-audit]${NC} $1"
}

# =============================================================================
# PUID/PGID handling (LinuxServer.io pattern for Unraid)
# =============================================================================

PUID=${PUID:-99}
PGID=${PGID:-100}
UMASK=${UMASK:-022}

log "Starting Media Audit container..."
log "User UID: ${PUID}"
log "Group GID: ${PGID}"
log "Umask: ${UMASK}"

# Set umask
umask ${UMASK}

# Modify the appuser to match PUID/PGID
if [ "$(id -u appuser)" != "${PUID}" ]; then
    log "Updating user ID to ${PUID}..."
    usermod -o -u "${PUID}" appuser 2>/dev/null || true
fi

if [ "$(id -g appgroup)" != "${PGID}" ]; then
    log "Updating group ID to ${PGID}..."
    groupmod -o -g "${PGID}" appgroup 2>/dev/null || true
fi

# Ensure ownership of app directories
log "Setting ownership on /app, /config, /reports..."
chown -R appuser:appgroup /app /config 2>/dev/null || true
chown -R appuser:appgroup /reports 2>/dev/null || true

# =============================================================================
# Configuration validation
# =============================================================================

log "Configuration:"
log "  Report Directory: ${REPORT_DIR:-/reports}"
log "  Media Roots: ${ROOTS:-/media}"
log "  Delete Under: ${DELETE_UNDER:-/media}"
log "  FFprobe Scope: ${FFPROBE_SCOPE:-dupes}"

if [ -n "${QBIT_HOST}" ]; then
    log "  qBittorrent: ${QBIT_HOST}:${QBIT_PORT:-8080}"
    if [ -n "${QBIT_WEBUI_URL}" ]; then
        log "  qBittorrent WebUI: ${QBIT_WEBUI_URL}"
    fi
else
    log "  qBittorrent: Not configured"
fi

if [ -n "${WEB_USER}" ] && [ -n "${WEB_PASS}" ]; then
    log "  Authentication: Enabled"
else
    warn "  Authentication: DISABLED (set WEB_USER and WEB_PASS for security)"
fi

if [ "${ALLOW_DELETE}" = "true" ]; then
    warn "  Delete Mode: ENABLED (deletions can be applied)"
else
    log "  Delete Mode: Safe (read-only scanning)"
fi

# =============================================================================
# Check ffprobe availability
# =============================================================================

if command -v ffprobe &> /dev/null; then
    FFPROBE_VERSION=$(ffprobe -version 2>&1 | head -n1)
    log "  FFprobe: ${FFPROBE_VERSION}"
else
    error "  FFprobe: NOT FOUND - media analysis will be limited!"
fi

# =============================================================================
# Create required directories
# =============================================================================

mkdir -p "${REPORT_DIR:-/reports}" 2>/dev/null || true
mkdir -p "${CONFIG_DIR:-/config}/logs" 2>/dev/null || true
chown appuser:appgroup "${REPORT_DIR:-/reports}" 2>/dev/null || true
chown -R appuser:appgroup "${CONFIG_DIR:-/config}" 2>/dev/null || true

log "  Log directory: ${CONFIG_DIR:-/config}/logs"

# =============================================================================
# Sensitive data protection
# =============================================================================

# Clear sensitive environment variables from logs/ps output
# (They're still available to the Python process)
if [ -n "${QBIT_PASS}" ]; then
    log "  qBittorrent password: [HIDDEN]"
fi

if [ -n "${WEB_PASS}" ]; then
    log "  Web password: [HIDDEN]"
fi

# =============================================================================
# Execute command
# =============================================================================

log "Starting application..."

# If we're running as root, switch to appuser
if [ "$(id -u)" = "0" ]; then
    # Run the command as the appuser
    exec gosu appuser "$@"
else
    # Already running as non-root
    exec "$@"
fi
