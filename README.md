# Media Audit - Docker Container for Unraid

<p align="center">
  <img src="https://img.shields.io/github/license/SynMaki/unraid-media-audit?style=flat-square" alt="License">
  <img src="https://img.shields.io/github/v/release/SynMaki/unraid-media-audit?style=flat-square" alt="Release">
  <img src="https://img.shields.io/github/actions/workflow/status/SynMaki/unraid-media-audit/docker-build.yml?style=flat-square" alt="Build">
</p>

<p align="center">
  <strong>📊 Scan media libraries for duplicates, manage quality upgrades, with qBittorrent protection</strong>
</p>

---

## 🎯 Features

- **Duplicate Detection**: Find duplicate episodes/movies across multiple folders
- **Quality Scoring**: Automatically ranks files by resolution, codec, audio quality, and language
- **Language Preference**: Prioritizes DEU+ENG for films/series, DEU+JPN for anime
- **qBittorrent Integration**: Protects actively seeding files from deletion
- **Hardlink Awareness**: Detects and handles hardlinked files correctly
- **Web UI Dashboard**: Easy-to-use interface for running audits and viewing reports
- **Interactive HTML Reports**: Beautiful, sortable reports with delete commands
- **Safe by Default**: Read-only scanning, no deletions unless explicitly enabled

---

## 📋 Table of Contents

- [Quick Start](#-quick-start)
- [Unraid Installation](#-unraid-installation)
- [Configuration](#-configuration)
- [qBittorrent Setup](#-qbittorrent-setup)
- [Usage](#-usage)
- [Volume Mappings](#-volume-mappings)
- [Environment Variables](#-environment-variables)
- [Security Considerations](#-security-considerations)
- [CLI Usage](#-cli-usage)
- [Troubleshooting](#-troubleshooting)

---

## 🚀 Quick Start

### Using Docker Compose

```bash
# Clone and start
git clone https://github.com/SynMaki/unraid-media-audit.git
cd unraid-media-audit

# Create data directories
mkdir -p data/{reports,config,media}

# Set credentials and start
WEB_USER=admin WEB_PASS=yourpassword docker compose up -d

# Access at http://localhost:8080
```

### Using Pre-built Image from GitHub Container Registry

```bash
docker run -d \
  --name media-audit \
  -p 8080:8080 \
  -e PUID=99 -e PGID=100 \
  -e WEB_USER=admin \
  -e WEB_PASS=changeme \
  -e ROOTS=/media/plexmedia,/media/torrents \
  -e DELETE_UNDER=/media/plexmedia \
  -v /mnt/user/data/media_audit_reports:/reports \
  -v /mnt/user/data/plexmedia:/media/plexmedia:ro \
  -v /mnt/user/data/torrents:/media/torrents:ro \
  ghcr.io/synmaki/unraid-media-audit:latest
```

---

## 🖥️ Unraid Installation

### Method 1: Community Applications (Recommended)

1. Go to **Apps** → Search "Media Audit"
2. Click **Install**
3. Configure the template settings
4. Click **Apply**

### Method 2: Using Template URL

1. Go to **Docker** → **Add Container**
2. Click **Template** dropdown → **Add Template URL**
3. Enter: `https://raw.githubusercontent.com/SynMaki/unraid-media-audit/main/unraid-template.xml`
4. Fill in your passwords and paths
5. Click **Apply**

### Method 3: Manual Setup

1. Go to **Docker** → **Add Container**
2. Toggle to **Advanced View**
3. Fill in the template:

| Setting | Value |
|---------|-------|
| Name | media-audit |
| Repository | `ghcr.io/synmaki/unraid-media-audit:latest` |
| Network Type | Bridge |
| WebUI | `http://[IP]:[PORT:8085]/` |
| Port | 8085 → 8080 |

### Recommended Volume Mappings for Unraid

| Container Path | Host Path | Access |
|----------------|-----------|--------|
| `/reports` | `/mnt/user/appdata/media-audit/reports` | Read/Write |
| `/config` | `/mnt/user/appdata/media-audit/config` | Read/Write |
| `/media/plexmedia` | `/mnt/user/data/plexmedia` | **Read Only** |
| `/media/torrents` | `/mnt/user/data/torrents` | **Read Only** |

⚠️ **Important**: Mount media directories as **read-only** for safety!

---

## ⚙️ Configuration

### Basic Configuration

```yaml
environment:
  # Required for security
  WEB_USER: admin
  WEB_PASS: your-secure-password
  
  # Media paths (container paths)
  ROOTS: /media/plexmedia,/media/torrents
  DELETE_UNDER: /media/plexmedia
  REPORT_DIR: /reports
```

### qBittorrent Integration

```yaml
environment:
  QBIT_HOST: 192.168.1.39         # Your qBittorrent host
  QBIT_PORT: 8081                  # qBittorrent WebUI port
  QBIT_USER: admin                 # qBittorrent username
  QBIT_PASS: yourpassword          # qBittorrent password
  QBIT_PATH_MAP: /downloads:/media/torrents  # Container:Host path mapping
  QBIT_WEBUI_URL: http://192.168.1.39:8081   # Link shown in reports
```

### Path Mapping Explained

qBittorrent runs in a container with its own path structure. The `QBIT_PATH_MAP` tells media-audit how to translate:

```
QBIT_PATH_MAP=/downloads:/media/torrents
             ↑ qBit container path   ↑ media-audit container path
```

For multiple mappings, use semicolons:
```
QBIT_PATH_MAP=/downloads:/media/torrents;/data/completed:/media/completed
```

---

## 📊 Usage

### Web UI

1. Open `http://your-server:8085`
2. Log in with your credentials
3. Click **Start Audit** to begin scanning
4. View the generated HTML report
5. Use the **Delete Script** for manual cleanup

### Report Files

Each audit run creates:
- `report.html` - Interactive visual report
- `summary.json` - Machine-readable summary
- `delete_plan.sh` - Bash script for deleting duplicates
- `*.csv` files - Detailed data exports

---

## 📁 Volume Mappings

### Required Volumes

| Path | Purpose | Access |
|------|---------|--------|
| `/reports` | Persistent report storage | RW |
| `/media/*` | Media directories to scan | **RO** (recommended) |

### Optional Volumes

| Path | Purpose |
|------|---------|
| `/config` | Configuration persistence |

### Unraid Example

```
# Reports (persistent)
/mnt/user/appdata/media-audit/reports → /reports (rw)

# Media (read-only for safety)
/mnt/user/data/plexmedia → /media/plexmedia (ro)
/mnt/user/data/torrents → /media/torrents (ro)
```

---

## 🔧 Environment Variables

### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `PUID` | 99 | User ID (Unraid: 99=nobody) |
| `PGID` | 100 | Group ID (Unraid: 100=users) |
| `UMASK` | 022 | File permission mask |
| `PORT` | 8080 | Web UI port |

### Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `WEB_USER` | (empty) | Web UI username |
| `WEB_PASS` | (empty) | Web UI password |

⚠️ If both are empty, the UI is accessible without authentication!

### Media Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `ROOTS` | /media | Comma-separated paths to scan |
| `DELETE_UNDER` | /media | Only allow deletions under this path |
| `REPORT_DIR` | /reports | Where to store reports |

### qBittorrent Integration

| Variable | Default | Description |
|----------|---------|-------------|
| `QBIT_HOST` | (empty) | qBittorrent hostname/IP |
| `QBIT_PORT` | 8080 | qBittorrent WebUI port |
| `QBIT_USER` | (empty) | qBittorrent username |
| `QBIT_PASS` | (empty) | qBittorrent password |
| `QBIT_PATH_MAP` | (empty) | Path mapping (container:host) |
| `QBIT_WEBUI_URL` | (empty) | URL to show in reports |

### Audit Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `FFPROBE_SCOPE` | dupes | `none`, `dupes`, or `all` |
| `CONTENT_TYPE` | auto | `auto`, `anime`, `series`, `movie` |
| `AVOID_MODE` | if-no-prefer | Language avoidance mode |
| `AVOID_AUDIO_LANG` | (empty) | Languages to avoid (e.g., `spa,fra`) |

### Safety Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `ALLOW_DELETE` | false | Enable deletion via web UI |
| `SCHEDULE_ENABLED` | false | Enable scheduled scans |
| `SCHEDULE_CRON` | 0 3 * * * | Cron expression for scheduling |

---

## 🔒 Security Considerations

### Best Practices

1. **Always set WEB_USER and WEB_PASS** for production use
2. **Mount media directories as read-only** (`ro` flag)
3. **Keep ALLOW_DELETE=false** unless absolutely necessary
4. **Use separate network** if exposing outside local network

### Secrets Management

- Credentials are passed via environment variables
- Sensitive values are hidden from logs
- Never hardcode secrets in docker-compose.yml for production

---

## 💻 CLI Usage

The container also supports CLI usage for scripting/automation:

```bash
# Run a scan directly
docker exec media-audit python /app/media_audit.py \
  --roots /media/plexmedia /media/torrents \
  --report-dir /reports \
  --delete-under /media/plexmedia

# View help
docker exec media-audit python /app/media_audit.py --help
```

### CLI Arguments

```
--roots PATH [PATH ...]    Directories to scan
--report-dir PATH          Where to save reports
--delete-under PATH        Only delete under this path
--ffprobe-scope SCOPE      none, dupes, or all
--content-type TYPE        auto, anime, series, movie
--avoid-mode MODE          if-no-prefer, strict, report-only
--avoid-audio-lang LANGS   Comma-separated language codes
--qbit-host HOST           qBittorrent hostname
--qbit-port PORT           qBittorrent port
--qbit-user USER           qBittorrent username
--qbit-pass PASS           qBittorrent password
--qbit-path-map MAP        Container:Host path mapping
--no-qbit                  Disable qBittorrent integration
--html-report              Generate HTML report (default: on)
--apply --yes              Actually delete files (DANGEROUS!)
```

---

## 🐛 Troubleshooting

### Common Issues

#### "Permission denied" errors
```bash
# Check PUID/PGID match your Unraid user
docker exec media-audit id

# Should show: uid=99(appuser) gid=100(appgroup)
```

#### qBittorrent connection failed
1. Verify qBittorrent WebUI is enabled and accessible
2. Check QBIT_HOST is reachable from container
3. Verify QBIT_PATH_MAP is correct
4. Check qBittorrent authentication bypass settings

#### No files found
1. Check volume mounts are correct
2. Verify paths in ROOTS exist inside container:
   ```bash
   docker exec media-audit ls -la /media/
   ```

#### FFprobe errors
- FFprobe is included in the container
- Check if media files are readable
- Try `FFPROBE_SCOPE=none` to skip media analysis

### Logs

```bash
# View container logs
docker logs media-audit

# Follow live
docker logs -f media-audit

# View audit logs in web UI under "Live Logs"
```

---

## 🔄 Updating

When a new version is released:

```bash
# Pull latest image
docker pull ghcr.io/synmaki/unraid-media-audit:latest

# Restart container (Unraid will auto-update if configured)
docker stop media-audit
docker rm media-audit
# Then recreate with same settings
```

In Unraid: Click on the container → **Check for Updates** → **Apply Update**

---

## 📜 License

This project is open source under the MIT License. See [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- Language scoring optimized for German/English/Japanese content
- qBittorrent integration for torrent seeders
- Inspired by media management needs of Plex/Sonarr/Radarr users
- Built with FastAPI, Python, and Docker
