# Media Audit - Docker Container for Unraid

<p align="center">
  <img src="https://img.shields.io/badge/version-3.4.1-blue?style=flat-square" alt="Version">
  <img src="https://img.shields.io/github/license/SynMaki/unraid-media-audit?style=flat-square" alt="License">
</p>

<p align="center">
  <strong>Scan media libraries for duplicates, detect missing hardlinks, with Sonarr/Radarr/qBittorrent integration</strong>
</p>

---

## What's New in v3.4.1

- **Missing Hardlinks Detection**: Find files that exist in BOTH Sonarr AND qBittorrent but WITHOUT hardlink = wasted disk space!
- **Seeding Overview**: See all seeding files with hardlink status
- **Improved Path Mapping**: Better logging and debugging for path mapping issues
- **New CSV Reports**: `missing_hardlinks.csv`, `all_seeding.csv`, `seeding_not_in_arr.csv`

---

## Features

- **Duplicate Detection**: Find duplicate episodes/movies across multiple folders
- **Missing Hardlinks Detection**: Find files that are in Sonarr AND seeding, but without hardlink (2x disk space!)
- **Quality Scoring**: Automatically ranks files by resolution, codec, audio quality, and language
- **Language Preference**: Prioritizes DEU+ENG for films/series, DEU+JPN for anime
- **Sonarr/Radarr Integration**: Protects files managed by your *arr apps from deletion
- **qBittorrent Integration**: Protects actively seeding files from deletion
- **Multiple Instance Support**: Connect to multiple Sonarr/Radarr instances
- **Hardlink Awareness**: Detects and handles hardlinked files correctly
- **Web UI Dashboard**: Easy-to-use interface for running audits and viewing reports
- **Interactive HTML Reports**: Beautiful, sortable reports with delete commands
- **Safe by Default**: Read-only scanning, no deletions unless explicitly enabled

---

## Quick Start

### Using Docker Compose

```bash
git clone https://github.com/SynMaki/unraid-media-audit.git
cd unraid-media-audit
docker compose up -d
# Access at http://localhost:8080
```

### Build from GitHub

```bash
docker build -t media-audit:latest https://github.com/SynMaki/unraid-media-audit.git
```

---

## Configuration via settings.json

The recommended way to configure Media Audit is via the Web UI settings page, which creates a `settings.json` file.

### Example settings.json

```json
{
  "general": {
    "report_dir": "/reports",
    "roots": ["/media/plexmedia", "/media/torrents"],
    "delete_under": "/media/plexmedia",
    "ffprobe_scope": "dupes",
    "content_type": "auto",
    "allow_delete": false
  },
  "qbittorrent": {
    "enabled": true,
    "host": "192.168.1.39",
    "port": 8081,
    "username": "admin",
    "password": "yourpassword",
    "path_mappings": [
      {
        "servarr_path": "/data/torrents",
        "local_path": "/media/torrents"
      }
    ],
    "webui_url": "http://192.168.1.39:8081"
  },
  "sonarr_instances": [
    {
      "enabled": true,
      "name": "Sonarr",
      "url": "http://192.168.1.39:8989/",
      "api_key": "your-api-key",
      "path_mappings": [
        {
          "servarr_path": "/data/plexmedia",
          "local_path": "/media/plexmedia"
        }
      ]
    },
    {
      "enabled": true,
      "name": "Sonarr Anime",
      "url": "http://192.168.1.39:8990/",
      "api_key": "your-api-key",
      "path_mappings": [
        {
          "servarr_path": "/data/plexmedia",
          "local_path": "/media/plexmedia"
        }
      ]
    }
  ],
  "radarr_instances": [
    {
      "enabled": true,
      "name": "Radarr",
      "url": "http://192.168.1.39:7878/",
      "api_key": "your-api-key",
      "path_mappings": [
        {
          "servarr_path": "/data/plexmedia",
          "local_path": "/media/plexmedia"
        }
      ]
    }
  ],
  "web": {
    "auth_enabled": true,
    "username": "admin",
    "password": "yourpassword"
  }
}
```

---

## Path Mapping Explained

Path mappings are **critical** for matching files between different containers.

### The Problem

Each container sees different paths:
- **qBittorrent** sees: `/data/torrents/sonarr/Show/episode.mkv`
- **Sonarr** sees: `/data/plexmedia/Serien/Show/episode.mkv`
- **Media Audit** sees: `/media/torrents/...` and `/media/plexmedia/...`

### The Solution

Configure path_mappings to translate paths:

```json
"path_mappings": [
  {
    "servarr_path": "/data/torrents",   // What qBittorrent/Sonarr sees
    "local_path": "/media/torrents"      // What Media Audit sees
  }
]
```

### How to Find the Correct Paths

1. **qBittorrent**: Settings > Downloads > Default Save Path
2. **Sonarr/Radarr**: Settings > Media Management > Root Folders
3. **Media Audit**: Check your Docker volume mounts

### Example Mapping

| Container | Internal Path | Media Audit Path | Mapping |
|-----------|--------------|------------------|---------|
| qBittorrent | `/data/torrents` | `/media/torrents` | `/data/torrents` -> `/media/torrents` |
| Sonarr | `/data/plexmedia/Serien` | `/media/plexmedia/Serien` | `/data/plexmedia` -> `/media/plexmedia` |
| Radarr | `/data/plexmedia/Filme` | `/media/plexmedia/Filme` | `/data/plexmedia` -> `/media/plexmedia` |

---

## Missing Hardlinks Detection

### What is it?

When you download a file via qBittorrent and import it to Sonarr, ideally a **hardlink** is created. This means:
- The file appears in both locations (`/torrents/` and `/plexmedia/`)
- But only uses disk space **once**

If NO hardlink exists, the file uses disk space **twice**!

### How Media Audit Detects This

A file with:
- `arr_managed = true` (Sonarr knows about it)
- `is_seeding = true` (qBittorrent is seeding it)
- `nlink = 1` (only one hardlink = NO hardlink to torrent!)

= **PROBLEM: File exists twice, wasting disk space!**

### Report Output

The HTML report shows:
```
Missing Hardlinks (Platzverschwendung)
- 42 Dateien ohne Hardlink
- 125.5 GB verschwendet
- 15 Torrents nicht in Arr
```

### CSV Reports

- `missing_hardlinks.csv` - Files without hardlink (in Arr AND seeding)
- `all_seeding.csv` - All seeding files with status
- `seeding_not_in_arr.csv` - Torrents not managed by Sonarr/Radarr

---

## Volume Mappings for Unraid

| Container Path | Host Path | Access |
|----------------|-----------|--------|
| `/reports` | `/mnt/user/appdata/media-audit/reports` | Read/Write |
| `/config` | `/mnt/user/appdata/media-audit/config` | Read/Write |
| `/media/plexmedia` | `/mnt/user/data/plexmedia` | **Read Only** |
| `/media/torrents` | `/mnt/user/data/torrents` | **Read Only** |

**Important**: Mount media directories as read-only for safety!

---

## Environment Variables

### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `PUID` | 99 | User ID (Unraid: 99=nobody) |
| `PGID` | 100 | Group ID (Unraid: 100=users) |
| `PORT` | 8080 | Web UI port |
| `WEB_USER` | (empty) | Web UI username |
| `WEB_PASS` | (empty) | Web UI password |

### Media Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `ROOTS` | /media | Comma-separated paths to scan |
| `DELETE_UNDER` | /media | Only allow deletions under this path |
| `REPORT_DIR` | /reports | Where to store reports |

### qBittorrent

| Variable | Description |
|----------|-------------|
| `QBIT_HOST` | qBittorrent hostname/IP |
| `QBIT_PORT` | qBittorrent WebUI port |
| `QBIT_USER` | qBittorrent username |
| `QBIT_PASS` | qBittorrent password |

### Sonarr/Radarr

| Variable | Description |
|----------|-------------|
| `SONARR_URL` | Sonarr base URL |
| `SONARR_APIKEY` | Sonarr API key |
| `RADARR_URL` | Radarr base URL |
| `RADARR_APIKEY` | Radarr API key |

---

## Report Files

Each audit run creates:

| File | Description |
|------|-------------|
| `report.html` | Interactive visual report |
| `summary.json` | Machine-readable summary |
| `delete_plan.sh` | Bash script for deleting duplicates |
| `files.csv` | All scanned files |
| `episode_duplicates.csv` | Duplicate episodes |
| `missing_hardlinks.csv` | Files without hardlink (wasted space!) |
| `all_seeding.csv` | All seeding files |
| `seeding_not_in_arr.csv` | Orphaned torrents |
| `hardlinks.csv` | Hardlink groups |

---

## Troubleshooting

### Path Mapping Issues

Check the logs for path mapping output:

```
qBittorrent path mappings configured: 1
  Mapping: '/data/torrents' -> '/media/torrents'
First torrent example:
  save_path: /data/torrents/sonarr
  content_path: /data/torrents/sonarr/Show.Name
```

If you see "Cannot stat torrent file", the mapping is incorrect!

### qBittorrent Connection Failed

1. Verify WebUI is enabled in qBittorrent
2. Check host is reachable from container
3. Verify credentials are correct
4. Check if "Bypass authentication for localhost" is enabled

### Sonarr/Radarr Not Matching

1. Check path_mappings in settings.json
2. Verify API key is correct
3. Look for "No path mapping applied!" warnings in logs

### View Logs

```bash
docker logs media-audit
docker logs -f media-audit  # Follow live
```

---

## Changelog

### v3.4.1 (2026-01-07)
- NEW: Missing Hardlinks detection
- NEW: Seeding overview in HTML report
- NEW: CSV exports for seeding analysis
- NEW: Summary stats for hardlink status

### v3.4.0 (2026-01-07)
- FIX: qBittorrent content_path support
- FIX: Category support for path mapping
- FIX: webapp reads servarr_path field
- NEW: Sonarr/Radarr section in HTML report
- NEW: Source column with badges
- NEW: Detailed path mapping logging

### v3.0.0 (2025-01-05)
- NEW: Sonarr/Radarr integration
- NEW: Multiple instance support
- NEW: Custom format scores

---

## License

MIT License - See [LICENSE](LICENSE) file for details.

---

## Acknowledgments

- Built for German/English/Japanese media collections
- qBittorrent integration for torrent seeders
- Designed for Plex/Sonarr/Radarr users on Unraid
- Built with FastAPI, Python, and Docker
