#!/usr/bin/env python3
"""
media_audit.py — Unraid Plex/Sonarr/Torrents Audit (dry-run by default)

Version: 3.4.3
Changelog:
- 2026-01-07 [AI] v3.4.3 - Report Deletion from Web UI
  - NEW: Delete reports directly from the web dashboard
  - NEW: Delete button with confirmation dialog for each report
  - NEW: Secure deletion with path validation to prevent exploits

- 2026-01-07 [AI] v3.4.2 - Torrent Ratio Display
  - NEW: Show torrent ratio for seeding files in HTML report
  - NEW: Ratio column in CSV exports (missing_hardlinks.csv, all_seeding.csv, seeding_not_in_arr.csv)
  - NEW: Ratio displayed in "Reason" column for seeding files (e.g., "SEEDING: TorrentName (Ratio: 1.25)")
  - Helps decide which seeding files can be safely deleted (high ratio = already shared enough)

- 2026-01-07 [AI] v3.4.1 - Missing Hardlinks Detection
  - NEW: Missing Hardlinks report - shows files that are in BOTH Sonarr AND qBittorrent but WITHOUT hardlink (wasted space!)
  - NEW: Seeding overview section in HTML report
  - NEW: CSV exports: missing_hardlinks.csv, all_seeding.csv, seeding_not_in_arr.csv
  - NEW: Summary stats: missing_hardlinks_count, missing_hardlinks_wasted_gb, seeding_with_hardlink, seeding_not_in_arr

- 2026-01-07 [AI] v3.4.0 - Path Mapping Fix & Report Improvements
  - FIX: qBittorrent now uses content_path for accurate file location
  - FIX: Added category support for qBittorrent path mapping
  - FIX: Improved path mapping logging for debugging
  - FIX: webapp now reads servarr_path field for qBittorrent path mappings
  - NEW: Sonarr/Radarr status section in HTML report
  - NEW: Source column shows Seeding/Arr-Managed status with badges
  - NEW: Filter button for Arr-Managed files
  - NEW: Detailed logging for path mapping issues

- 2025-01-05 [AI] v3.0.0 - Sonarr/Radarr Integration
  - NEW: Multiple Sonarr/Radarr instance support
  - NEW: Files managed by Arr are protected from deletion (PROTECTED)
  - NEW: Path mapping for container vs host paths
  - NEW: Custom format scores displayed in reports
  - NEW: Upgrade recommendations from Arr quality profiles
  - NEW: Report columns for arr_* metadata
  - NEW: Optional --arr-rescan to trigger rescans after deletion

- 2025-01-04 [AI] v2.3.0 - HTML Report & ffprobe improvements
  - NEW: Interactive HTML report with sortable tables and statistics
  - NEW: Better ffprobe error handling (graceful fallback to filename-only)
  - NEW: ffprobe dependency check with helpful error message
  - NEW: --html-report option to generate standalone HTML report
  - NEW: Space savings calculation and visualization
  - IMPROVED: Summary now includes ffprobe error statistics
  - IMPROVED: Report now shows potential disk space to reclaim

- 2025-01-04 [AI] v2.2.0 - Fixed language detection patterns
  - Detect [DE+JA], [DE+EN], [JA+EN], [DE+JA+EN] bracket patterns
  - Detect Multi-German, German-JAP patterns
  - Release group inference for language detection

- 2025-01-04 [AI] v2.1.0 - Multilang scoring & anime support
- 2025-01-04 [AI] v2.0.0 - Major refactor for robustness

Language Scoring:
  Films/Serien: DEU+ENG (+200) > DEU (+150) > ENG (+100)
  Anime: DEU+JPN (+200) > DEU (+150) > JPN+DEU_subs (+120) > JPN (+100) > ENG (+50)
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import html
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "3.4.3"

# =============================================================================
# CONFIGURATION
# =============================================================================

class ContentType(Enum):
    AUTO = "auto"
    ANIME = "anime"
    SERIES = "series"
    MOVIE = "movie"


class AvoidMode(Enum):
    IF_NO_PREFER = "if-no-prefer"
    STRICT = "strict"
    REPORT_ONLY = "report-only"


@dataclass(frozen=True)
class Config:
    VIDEO_EXTS: frozenset = frozenset({".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".wmv"})

    DEFAULT_ROOTS: Tuple[str, ...] = (
        "/mnt/user/data/plexmedia",
        "/mnt/user/data/torrents",
    )

    # Scoring weights
    SCORE_RES_4K: int = 400
    SCORE_RES_1080: int = 300
    SCORE_RES_720: int = 200
    SCORE_RES_480: int = 120
    SCORE_RES_OTHER: int = 60

    SCORE_SRC_BLURAY: int = 80
    SCORE_SRC_WEBDL: int = 60
    SCORE_SRC_WEBRIP: int = 50
    SCORE_SRC_HDTV: int = 40

    SCORE_CODEC_HEVC: int = 20
    SCORE_CODEC_AVC: int = 10

    SCORE_AUDIO_TRUEHD: int = 20
    SCORE_AUDIO_ATMOS: int = 20
    SCORE_AUDIO_DTSHD: int = 15
    SCORE_AUDIO_DTS: int = 10
    SCORE_AUDIO_EAC3: int = 10
    SCORE_AUDIO_AC3: int = 8
    SCORE_AUDIO_AAC: int = 5

    SCORE_CH_8PLUS: int = 10
    SCORE_CH_6PLUS: int = 8
    SCORE_CH_2PLUS: int = 2

    SCORE_LANG_DUAL_DEU_ENG: int = 200
    SCORE_LANG_DEU_ONLY: int = 150
    SCORE_LANG_ENG_ONLY: int = 100
    
    SCORE_LANG_DUAL_DEU_JPN: int = 200
    SCORE_LANG_ANIME_DEU: int = 150
    SCORE_LANG_JPN_DEU_SUBS: int = 120
    SCORE_LANG_JPN_ONLY: int = 100
    SCORE_LANG_ANIME_ENG: int = 50
    
    SCORE_AVOID_LANG_PENALTY: int = -40

    PROTECTED_PATHS: Tuple[str, ...] = (
        "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64",
        "/mnt", "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys",
        "/tmp", "/usr", "/var",
    )

    KNOWN_LANG_CODES: frozenset = frozenset({
        "eng", "deu", "ger", "jpn", "fra", "fre", "spa", "ita", "por",
        "rus", "zho", "chi", "kor", "ara", "hin", "und", "mul",
    })


CFG = Config()


# =============================================================================
# QBITTORRENT API CLIENT
# =============================================================================

class QBittorrentClient:
    """Simple qBittorrent WebUI API client using only stdlib.
    
    API Reference: https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)
    
    IMPORTANT: qBittorrent requires Referer/Origin headers matching the host to prevent CSRF.
    """
    
    def __init__(self, host: str, port: int, username: str = "", password: str = ""):
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}/api/v2"
        self.origin_url = f"http://{host}:{port}"
        self.username = username
        self.password = password
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )
        self._logged_in = False
    
    def _request(self, endpoint: str, data: Optional[dict] = None) -> Optional[bytes]:
        """Make API request with proper headers for CSRF protection."""
        url = f"{self.base_url}/{endpoint}"
        try:
            if data:
                data_encoded = urllib.parse.urlencode(data).encode('utf-8')
                req = urllib.request.Request(url, data=data_encoded)
            else:
                req = urllib.request.Request(url)
            
            # CRITICAL: qBittorrent requires these headers to prevent CSRF
            req.add_header("Referer", self.origin_url)
            req.add_header("Origin", self.origin_url)
            
            with self.opener.open(req, timeout=15) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            LOG.warning(f"qBittorrent HTTP error {e.code}: {e.reason} for {endpoint}")
            return None
        except urllib.error.URLError as e:
            LOG.warning(f"qBittorrent connection failed: {e.reason}")
            return None
        except Exception as e:
            LOG.debug(f"qBittorrent request failed: {e}")
            return None
    
    def login(self) -> bool:
        """Authenticate with qBittorrent."""
        # First check if API is accessible
        result = self._request("app/version")
        if result:
            LOG.info(f"qBittorrent version: {result.decode('utf-8', errors='ignore')}")
        
        if self.username:
            data = {"username": self.username, "password": self.password}
            result = self._request("auth/login", data)
            if result:
                response_text = result.decode('utf-8', errors='ignore')
                if "Ok" in response_text:
                    self._logged_in = True
                    LOG.info("qBittorrent login successful")
                    return True
                elif "Fails" in response_text:
                    LOG.error("qBittorrent login failed - check username/password")
                    return False
        
        # Try accessing without explicit login
        result = self._request("torrents/info?limit=1")
        if result:
            self._logged_in = True
            return True
            
        return False
    
    def get_torrents(self) -> List[dict]:
        """Get all torrents."""
        if not self._logged_in and not self.login():
            return []
        
        result = self._request("torrents/info")
        if result:
            try:
                torrents = json.loads(result)
                LOG.info(f"Found {len(torrents)} torrents in qBittorrent")
                return torrents
            except json.JSONDecodeError:
                LOG.error("Failed to parse qBittorrent response")
                return []
        return []
    
    def get_torrent_files(self, torrent_hash: str) -> List[dict]:
        """Get files for a specific torrent."""
        if not self._logged_in and not self.login():
            return []
        
        result = self._request(f"torrents/files?hash={torrent_hash}")
        if result:
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                return []
        return []
    
    def delete_torrent(self, torrent_hash: str, delete_files: bool = False) -> bool:
        """Delete a torrent (optionally with files)."""
        if not self._logged_in and not self.login():
            return False
        
        data = {
            "hashes": torrent_hash,
            "deleteFiles": "true" if delete_files else "false"
        }
        result = self._request("torrents/delete", data)
        return result is not None
    
    def get_categories(self) -> Dict[str, dict]:
        """Get all categories with their save paths."""
        if not self._logged_in and not self.login():
            return {}

        result = self._request("torrents/categories")
        if result:
            try:
                categories = json.loads(result)
                LOG.info(f"qBittorrent categories: {list(categories.keys())}")
                return categories
            except json.JSONDecodeError:
                return {}
        return {}

    def get_all_torrent_files_with_inodes(self, path_mappings: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, dict], Dict[Tuple[int, int], dict], Dict[str, dict]]:
        """
        Get all files from ALL torrents.

        Uses content_path for accurate file location (handles categories properly).

        Args:
            path_mappings: Dict mapping qBittorrent container paths to local scanner paths
                           e.g. {"/data/torrents": "/media/torrents"}

        Returns:
          - path_map: {absolute_file_path: info}
          - inode_map: {(dev, inode): info} for hardlink matching
          - filename_map: {filename_lower: info} for fallback matching
        """
        path_map = {}
        inode_map = {}
        filename_map = {}  # Fallback: match by filename only
        torrents = self.get_torrents()

        if not torrents:
            LOG.warning("No torrents returned from qBittorrent")
            return {}, {}, {}

        # Get categories for debugging
        categories = self.get_categories()
        if categories:
            for cat_name, cat_info in categories.items():
                LOG.debug(f"Category '{cat_name}': savePath={cat_info.get('savePath', 'N/A')}")

        path_mappings = path_mappings or {}
        inode_errors = 0
        inode_success = 0
        
        # Count torrent states for debugging
        state_counts = {}
        for t in torrents:
            s = t.get("state", "unknown")
            state_counts[s] = state_counts.get(s, 0) + 1
        
        LOG.info(f"qBittorrent torrent states: {state_counts}")
        
        # Which states count as "seeding" / active?
        # See: https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)#torrent-management
        SEEDING_STATES = {
            "uploading",      # Currently uploading
            "stalledUP",      # Seeding but no peers
            "queuedUP",       # Queued for seeding
            "forcedUP",       # Forced seeding
            "seeding",        # Legacy state name
        }
        
        # Also protect downloading torrents (incomplete)
        DOWNLOADING_STATES = {
            "downloading",
            "stalledDL", 
            "queuedDL",
            "forcedDL",
            "metaDL",
            "allocating",
            "checkingDL",
        }
        
        ACTIVE_STATES = SEEDING_STATES | DOWNLOADING_STATES
        
        seeding_count = sum(1 for t in torrents if t.get("state") in SEEDING_STATES)
        downloading_count = sum(1 for t in torrents if t.get("state") in DOWNLOADING_STATES)
        LOG.info(f"Active torrents: {seeding_count} seeding, {downloading_count} downloading")
        
        # Log first few paths for debugging
        first_paths_logged = 0
        skipped_states = 0

        # Log path mappings for debugging
        if path_mappings:
            LOG.info(f"qBittorrent path mappings configured: {len(path_mappings)}")
            for qbit_path, local_path in path_mappings.items():
                LOG.info(f"  Mapping: '{qbit_path}' -> '{local_path}'")
        else:
            LOG.warning("No qBittorrent path mappings configured!")

        # Log first torrent details for debugging
        if torrents:
            t = torrents[0]
            LOG.info(f"First torrent example:")
            LOG.info(f"  name: {t.get('name', 'N/A')}")
            LOG.info(f"  save_path: {t.get('save_path', 'N/A')}")
            LOG.info(f"  content_path: {t.get('content_path', 'N/A')}")
            LOG.info(f"  category: {t.get('category', '(none)')}")
            LOG.info(f"  state: {t.get('state', 'N/A')}")

        for torrent in torrents:
            torrent_hash = torrent.get("hash", "")
            torrent_name = torrent.get("name", "")
            state = torrent.get("state", "")
            save_path = torrent.get("save_path", "").rstrip("/")
            content_path = torrent.get("content_path", "").rstrip("/")  # Full path to content
            category = torrent.get("category", "")

            # Skip completed/paused torrents that aren't seeding
            # NOTE: We process ALL states but mark the state in info for later filtering
            is_active = state in ACTIVE_STATES

            # Determine the base path for files
            # content_path is more accurate as it includes the torrent name folder for multi-file torrents
            # save_path is just the parent directory
            base_path = content_path if content_path else save_path

            # Apply path mapping (qBittorrent container path -> local scanner path)
            host_base_path = base_path
            mapping_applied = False
            matched_mapping = None
            for container_path, host_path in path_mappings.items():
                # Normalize paths for comparison
                container_path_normalized = container_path.rstrip("/")
                if base_path.startswith(container_path_normalized):
                    host_base_path = base_path.replace(container_path_normalized, host_path.rstrip("/"), 1)
                    mapping_applied = True
                    matched_mapping = f"{container_path} -> {host_path}"
                    break

            # Get files in this torrent
            files = self.get_torrent_files(torrent_hash)

            # For single-file torrents, content_path IS the file path
            # For multi-file torrents, content_path is the directory
            is_single_file = len(files) == 1 and not files[0].get("name", "").count("/")

            for f in files:
                file_name = f.get("name", "")
                if not file_name:
                    continue

                # Skip sample files
                file_lower = file_name.lower()
                if "/sample/" in file_lower or file_lower.startswith("sample") or "-sample." in file_lower:
                    continue

                # Skip non-video files
                if not any(file_lower.endswith(ext) for ext in ('.mkv', '.mp4', '.avi', '.m4v', '.ts', '.wmv', '.mov')):
                    continue

                # Calculate the absolute file path
                if is_single_file:
                    # For single-file torrents, content_path is the complete file path
                    container_file_path = content_path
                else:
                    # For multi-file torrents, append file name to save_path (not content_path!)
                    # file_name already contains the relative path from save_path
                    container_file_path = os.path.join(save_path, file_name)

                # Apply path mapping to the container file path
                abs_path = container_file_path
                for container_path, host_path in path_mappings.items():
                    container_path_normalized = container_path.rstrip("/")
                    if container_file_path.startswith(container_path_normalized):
                        abs_path = container_file_path.replace(container_path_normalized, host_path.rstrip("/"), 1)
                        mapping_applied = True
                        break

                info = {
                    "torrent_hash": torrent_hash,
                    "torrent_name": torrent_name,
                    "state": state,
                    "is_active": is_active,
                    "category": category,
                    "save_path": save_path,
                    "content_path": content_path,
                    "torrent_file_name": file_name,
                    "torrent_path": abs_path,
                    "container_path": container_file_path,
                    "mapping_applied": mapping_applied,
                    "ratio": torrent.get("ratio", 0.0),  # Share ratio
                }

                path_map[abs_path] = info

                # Add to filename map (just the basename, lowercase)
                basename = os.path.basename(file_name).lower()
                filename_map[basename] = info

                # Log first few paths for debugging
                if first_paths_logged < 5:
                    LOG.info(f"Torrent file #{first_paths_logged + 1}: state={state} active={is_active} category='{category}'")
                    LOG.info(f"  qBit container path: {container_file_path}")
                    LOG.info(f"  Mapped local path:   {abs_path}")
                    LOG.info(f"  Mapping applied:     {mapping_applied}")
                    first_paths_logged += 1

                # Get inode for hardlink matching
                try:
                    st = os.stat(abs_path)
                    inode_key = (st.st_dev, st.st_ino)
                    inode_map[inode_key] = info
                    inode_success += 1
                except OSError as e:
                    inode_errors += 1
                    # Log first few errors for debugging
                    if inode_errors <= 5:
                        LOG.warning(f"Cannot stat torrent file: {abs_path}")
                        LOG.warning(f"  (container was: {container_file_path})")
        
        LOG.info(f"Indexed {len(path_map)} torrent files, {inode_success} with inodes, {inode_errors} stat errors")
        LOG.info(f"Filename map has {len(filename_map)} unique filenames for fallback matching")
        
        if inode_success == 0 and inode_errors > 0:
            LOG.warning("No torrent files could be stat'd - check --qbit-path-map settings!")
            LOG.warning("Example: --qbit-path-map '/downloads:/mnt/user/data/torrents'")
        
        return path_map, inode_map, filename_map


def get_seeding_files_with_inodes(qbit_host: str, qbit_port: int, qbit_user: str, qbit_pass: str,
                                  path_mappings: Optional[Dict[str, str]] = None
                                  ) -> Tuple[Dict[str, dict], Dict[Tuple[int, int], dict], Dict[str, dict]]:
    """Connect to qBittorrent and get all files with inode info for hardlink matching."""
    LOG.info(f"Attempting qBittorrent connection to http://{qbit_host}:{qbit_port}")
    client = QBittorrentClient(qbit_host, qbit_port, qbit_user, qbit_pass)
    if not client.login():
        LOG.error(f"Could not connect to qBittorrent at {qbit_host}:{qbit_port}")
        LOG.error("Check: 1) Is qBittorrent running? 2) Is WebUI enabled? 3) Check username/password")
        return {}, {}, {}
    
    return client.get_all_torrent_files_with_inodes(path_mappings)


# Keep old function for backwards compatibility
def get_seeding_files(qbit_host: str, qbit_port: int, qbit_user: str, qbit_pass: str,
                      path_filter: Optional[str] = None) -> Dict[str, dict]:
    """Connect to qBittorrent and get all seeding files (path-based only)."""
    path_map, _, _ = get_seeding_files_with_inodes(qbit_host, qbit_port, qbit_user, qbit_pass)
    return path_map

# =============================================================================
# ISO 639 LANGUAGE CODE MAPPING
# =============================================================================

ISO639_MAP: Dict[str, str] = {
    "en": "eng", "de": "deu", "ja": "jpn", "jp": "jpn",
    "ger": "deu", "german": "deu", "deutsch": "deu",
    "english": "eng", "eng": "eng",
    "japanese": "jpn", "jpn": "jpn", "jap": "jpn",
    "french": "fra", "fra": "fra", "fre": "fra",
    "spanish": "spa", "spa": "spa",
    "italian": "ita", "ita": "ita",
    "portuguese": "por", "por": "por",
    "russian": "rus", "rus": "rus",
    "chinese": "zho", "zho": "zho", "chi": "zho",
    "korean": "kor", "kor": "kor",
    "deu": "deu", "ara": "ara", "hin": "hin", "und": "und", "mul": "mul",
}


def normalize_lang_code(code: str) -> str:
    return ISO639_MAP.get(code.strip().lower(), code.strip().lower())


# =============================================================================
# LOGGING
# =============================================================================

def setup_logging(debug: bool = False, log_dir: str = "/config/logs") -> logging.Logger:
    """Setup centralized logging to file AND console."""
    from datetime import datetime
    from pathlib import Path
    
    # Create log directory
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_file = Path(log_dir) / f"media_audit_{datetime.now().strftime('%Y%m%d')}.log"
    
    # Get root logger for media_audit
    logger = logging.getLogger("media_audit")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.handlers = []  # Clear existing handlers
    
    # File handler - detailed logging
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)
    
    # Console handler - info level
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if debug else logging.INFO)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(ch)
    
    logger.info(f"Logging initialized - file: {log_file}")
    return logger

# Initialize with basic config, will be reconfigured in main()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("media_audit")

# =============================================================================
# REGEX PATTERNS
# =============================================================================

EP_RE = re.compile(r"(?i)\bS(?P<s>\d{1,2})E(?P<e>\d{1,3})\b")
ABS_RE = re.compile(r"\s-\s(?P<abs>\d{3,4})\s-\s")
RES_RE = re.compile(r"(?i)\b(?P<res>2160|1080|720|576|480|360)p\b")
RELGRP_RE = re.compile(r"-(?P<grp>[A-Za-z0-9][A-Za-z0-9._]+)$")
BRACKET_RE = re.compile(r"\[([^\]]+)\]")
SEASON_RE = re.compile(r"(?i)season\s*(\d+)")

# Language patterns
BRACKET_LANG_COMBO_RE = re.compile(
    r"\[(?P<langs>(?:DE|EN|JA|JP|GER|ENG|JPN|JAP)(?:\+(?:DE|EN|JA|JP|GER|ENG|JPN|JAP))*)\]",
    re.IGNORECASE
)
BRACKET_SINGLE_LANG_RE = re.compile(r"\[(?P<lang>DE|EN|JA|JP|GER|ENG|JPN|JAP)\]", re.IGNORECASE)
MULTI_GERMAN_RE = re.compile(r"(?i)\bMulti[- ]?German\b")
GERMAN_JAP_RE = re.compile(r"(?i)\bGerman[- ]?(?:JAP|JPN)\b")
GERMAN_DL_RE = re.compile(r"(?i)\bGerman\s+DL\b")
DL_RE = re.compile(r"(?i)\bDL\b|\bDual[- ]?(?:Audio|Language)?\b|\bMulti\b")

# Anime detection
ANIME_PATH_PATTERNS = [
    re.compile(r"(?i)/anime/"),
    re.compile(r"(?i)/animes?/"),
    re.compile(r"(?i)\banime\b"),
]
ANIME_NAME_PATTERNS = [
    re.compile(r"(?i)\[.*?(fansub|horriblesubs|erai-raws|subsplease|abj|gertv).*?\]"),
    re.compile(r"(?i)-(?:Erai-raws|HorribleSubs|SubsPlease|ABJ|GERTv)"),
]

# Release groups
JAPANESE_RELEASE_GROUPS = re.compile(
    r"(?i)-(?:Erai-raws|HorribleSubs|SubsPlease|Tsundere-Raws|"
    r"Anime-Land|Anime-Raws|Ohys-Raws|LowPower-Raws|NC-Raws|"
    r"Tsundere|Judas|BakedFish|DDY|SallySubs)(?:\.mkv)?$"
)
GERMAN_ANIME_GROUPS = re.compile(
    r"(?i)-(?:ABJ|GERTv|German-Anime|GerAnime|ANiME-RG|"
    r"TVS|FilmPalast|PL3X|TELEPOOL)(?:\.mkv)?$"
)

# Source/codec hints
SOURCE_HINTS = [
    ("bluray", re.compile(r"(?i)\bblu[- ]?ray\b|\bbdrip\b|\bremux\b")),
    ("webdl",  re.compile(r"(?i)\bweb[- ]?dl\b")),
    ("webrip", re.compile(r"(?i)\bweb[- ]?rip\b")),
    ("hdtv",   re.compile(r"(?i)\bhdtv\b")),
]
CODEC_HINTS = [
    ("hevc", re.compile(r"(?i)\bhevc\b|\bx265\b|\bh\.?265\b|\bh265\b")),
    ("avc",  re.compile(r"(?i)\bh\.?264\b|\bx264\b|\bavc\b|\bh264\b")),
]
AUDIO_HINTS = [
    ("truehd", re.compile(r"(?i)\btruehd\b")),
    ("atmos",  re.compile(r"(?i)\batmos\b")),
    ("dtshd",  re.compile(r"(?i)\bdts[- ]?hd\b|\bdtshd\b")),
    ("dts",    re.compile(r"(?i)\bdts\b")),
    ("eac3",   re.compile(r"(?i)\be-?ac3\b|\beac3\b")),
    ("ac3",    re.compile(r"(?i)\bac3\b")),
    ("aac",    re.compile(r"(?i)\baac\b")),
    ("flac",   re.compile(r"(?i)\bflac\b")),
]

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def format_size(size_bytes: int) -> str:
    """Format bytes to human-readable string."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


def safe_rel(p: Path, roots: List[Path]) -> str:
    for r in roots:
        try:
            return str(p.relative_to(r))
        except ValueError:
            continue
    return str(p)


def validate_lang_codes(codes: List[str]) -> List[str]:
    validated = []
    for code in codes:
        normalized = normalize_lang_code(code.strip())
        if normalized:
            validated.append(normalized)
    return validated


def is_path_safe_for_deletion(path: Path, delete_under: Path) -> bool:
    try:
        resolved = path.resolve()
        delete_resolved = delete_under.resolve()
    except (OSError, RuntimeError):
        return False
    try:
        resolved.relative_to(delete_resolved)
    except ValueError:
        return False
    resolved_str = str(resolved)
    for protected in CFG.PROTECTED_PATHS:
        if resolved_str == protected or resolved_str.startswith(protected + "/"):
            if protected == "/mnt" and str(delete_resolved).startswith("/mnt/"):
                continue
            return False
    return True


def check_unraid_path_consistency(roots: List[Path]) -> List[str]:
    warnings = []
    has_user, has_disk, has_cache = False, False, False
    disk_pattern = re.compile(r"^/mnt/disk\d+")
    for root in roots:
        root_str = str(root)
        if root_str.startswith("/mnt/user"):
            has_user = True
        elif disk_pattern.match(root_str):
            has_disk = True
        elif root_str.startswith("/mnt/cache"):
            has_cache = True
    if has_user and has_disk:
        warnings.append("WARNING: Mixed /mnt/user and /mnt/diskX paths - hardlinks may break!")
    if has_user and has_cache:
        warnings.append("WARNING: Mixed /mnt/user and /mnt/cache paths!")
    return warnings


def detect_content_type(path: Path, absolute_ep: Optional[int]) -> ContentType:
    path_str = str(path).lower()
    name_str = path.name
    for pattern in ANIME_PATH_PATTERNS:
        if pattern.search(path_str):
            return ContentType.ANIME
    for pattern in ANIME_NAME_PATTERNS:
        if pattern.search(name_str):
            return ContentType.ANIME
    if absolute_ep is not None:
        return ContentType.ANIME
    if "/filme/" in path_str or "/movies/" in path_str:
        return ContentType.MOVIE
    return ContentType.SERIES


# =============================================================================
# LANGUAGE EXTRACTION
# =============================================================================

def extract_languages_from_filename(filename: str, is_anime: bool = False) -> Set[str]:
    langs: Set[str] = set()
    
    # Bracket combos [DE+JA]
    for match in BRACKET_LANG_COMBO_RE.finditer(filename):
        combo = match.group("langs")
        for code in combo.split("+"):
            normalized = normalize_lang_code(code)
            if normalized:
                langs.add(normalized)
    
    # Single brackets [JA]
    for match in BRACKET_SINGLE_LANG_RE.finditer(filename):
        normalized = normalize_lang_code(match.group("lang"))
        if normalized:
            langs.add(normalized)
    
    # Multi-German
    if MULTI_GERMAN_RE.search(filename):
        langs.add("deu")
        if is_anime or "anime" in filename.lower():
            if re.search(r"(?i)\b(?:JA|JAP|JPN|Japanese)\b", filename):
                langs.add("jpn")
        else:
            if re.search(r"(?i)\b(?:EN|ENG|English)\b", filename):
                langs.add("eng")
    
    # German-JAP
    if GERMAN_JAP_RE.search(filename):
        langs.add("deu")
        langs.add("jpn")
    
    # German DL
    if GERMAN_DL_RE.search(filename):
        langs.add("deu")
        if is_anime or re.search(r"(?i)\b(?:JA|JAP|JPN)\b", filename):
            langs.add("jpn")
        else:
            langs.add("eng")
    
    # Release group inference
    if not langs and JAPANESE_RELEASE_GROUPS.search(filename):
        langs.add("jpn")
    
    if GERMAN_ANIME_GROUPS.search(filename):
        langs.add("deu")
        if is_anime and "jpn" not in langs:
            if re.search(r"(?i)\b(?:JA|JAP|JPN)\b", filename):
                langs.add("jpn")
    
    # Standalone keywords
    if not langs:
        if re.search(r"(?i)\bGERMAN\b", filename):
            langs.add("deu")
        if re.search(r"(?i)\b(?:ENGLISH|\.ENG\.)\b", filename):
            langs.add("eng")
        if re.search(r"(?i)\b(?:JAPANESE|\.JPN\.)\b", filename):
            langs.add("jpn")
    
    # DL marker
    if DL_RE.search(filename) and "deu" in langs and len(langs) == 1:
        langs.add("jpn" if is_anime else "eng")
    
    return langs


def extract_subtitle_languages_from_filename(filename: str) -> Set[str]:
    subs: Set[str] = set()
    if re.search(r"(?i)(?:german|ger|deu)[._-]?subs?", filename):
        subs.add("deu")
    if re.search(r"(?i)(?:english|eng)[._-]?subs?", filename):
        subs.add("eng")
    if re.search(r"(?i)(?:japanese|jpn|jap)[._-]?subs?", filename):
        subs.add("jpn")
    return subs


# =============================================================================
# TEXT EXTRACTION FUNCTIONS
# =============================================================================

def extract_episode_key(text: str) -> Optional[Tuple[int, int]]:
    m = EP_RE.search(text)
    return (int(m.group("s")), int(m.group("e"))) if m else None


def extract_absolute_ep(text: str) -> Optional[int]:
    m = ABS_RE.search(text)
    if m:
        try:
            return int(m.group("abs"))
        except ValueError:
            pass
    return None


def extract_resolution(text: str) -> Optional[int]:
    m = RES_RE.search(text)
    return int(m.group("res")) if m else None


def match_first(text: str, patterns) -> Optional[str]:
    for key, rx in patterns:
        if rx.search(text):
            return key
    return None


def collect_many(text: str, patterns) -> List[str]:
    return sorted(set(key for key, rx in patterns if rx.search(text)))


def parse_sonarr_show_from_filename(filename: str) -> Optional[str]:
    m = re.match(r"^(?P<show>.+?)\s*-\s*S\d{2}E\d{2,3}\b", Path(filename).name, re.IGNORECASE)
    return m.group("show").strip() if m else None


def guess_show_name(path: Path) -> Optional[str]:
    show = parse_sonarr_show_from_filename(path.name)
    if show:
        return show
    parts = path.parts
    for token in ("Serien", "Anime", "Filme", "4k"):
        if token in parts:
            i = parts.index(token)
            if i + 1 < len(parts):
                return parts[i + 1]
    if "sonarr" in parts:
        i = parts.index("sonarr")
        if i + 1 < len(parts):
            return parts[i + 1]
    return path.parent.name or None


def parse_bracket_tokens(name: str) -> dict:
    tokens = BRACKET_RE.findall(name)
    out = {"br_source": None, "br_resolution": None, "br_video_codec": None,
           "br_audio_codec": None, "br_audio_channels": None, "br_audio_hints": []}
    for t in tokens:
        if not out["br_source"]:
            out["br_source"] = match_first(t, SOURCE_HINTS)
        if not out["br_resolution"]:
            out["br_resolution"] = extract_resolution(t)
        if not out["br_video_codec"]:
            out["br_video_codec"] = match_first(t, CODEC_HINTS)
        m = re.match(r"(?i)^(?P<ac>[A-Za-z0-9]+)\s+(?P<ch>\d(?:\.\d)?)$", t.strip())
        if m:
            ac = m.group("ac").lower()
            if ac in ("eac3", "e-ac3"):
                ac = "eac3"
            out["br_audio_codec"] = out["br_audio_codec"] or ac
            out["br_audio_channels"] = out["br_audio_channels"] or m.group("ch")
        out["br_audio_hints"].extend(collect_many(t, AUDIO_HINTS))
    out["br_audio_hints"] = sorted(set(out["br_audio_hints"]))
    return out


def parse_release_group(stem: str) -> Optional[str]:
    m = RELGRP_RE.search(stem)
    return m.group("grp") if m else None


# =============================================================================
# FFPROBE FUNCTIONS (Improved with dependency diagnosis)
# =============================================================================

def check_ffprobe_dependencies() -> Tuple[bool, Optional[str], Optional[str], List[str]]:
    """
    Check if ffprobe is available and working.
    Checks in order: 1) Same directory as script, 2) System PATH
    Returns: (is_working, path, error_message, missing_libraries)
    """
    # First check for ffprobe in the same directory as the script
    script_dir = Path(__file__).parent.resolve()
    local_ffprobe = script_dir / "ffprobe"
    
    if local_ffprobe.exists() and os.access(local_ffprobe, os.X_OK):
        exe = str(local_ffprobe)
    else:
        exe = shutil.which("ffprobe")
    
    if not exe:
        return False, None, "ffprobe not found (checked script directory and PATH)", []
    
    if not os.access(exe, os.X_OK):
        return False, exe, "ffprobe is not executable", []
    
    # Check for missing shared libraries using ldd
    missing_libs = []
    try:
        result = subprocess.run(
            ["ldd", exe],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "not found" in line:
                    # Extract library name
                    lib = line.split()[0] if line.split() else line
                    missing_libs.append(lib)
    except Exception:
        pass  # ldd might not be available
    
    if missing_libs:
        return False, exe, f"Missing libraries: {', '.join(missing_libs[:5])}", missing_libs
    
    # Test run to check for runtime library issues
    try:
        result = subprocess.run(
            [exe, "-version"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            error = result.stderr.strip() if result.stderr else "Unknown error"
            # Extract library name from error message
            lib_match = re.search(r"lib\w+\.so\.\d+", error)
            if lib_match:
                missing_libs.append(lib_match.group())
            return False, exe, f"ffprobe test failed: {error[:200]}", missing_libs
        return True, exe, None, []
    except subprocess.TimeoutExpired:
        return False, exe, "ffprobe test timed out", []
    except Exception as e:
        return False, exe, f"ffprobe test error: {e}", []


def suggest_library_packages(missing_libs: List[str]) -> List[str]:
    """Map missing library names to un-get package names."""
    # Common library to package mappings for Unraid/Slackware
    lib_to_pkg = {
        "libopenal": "openal-soft",
        "libcaca": "libcaca",
        "libcdio": "libcdio",
        "libcdio_paranoia": "libcdio-paranoia",
        "libcdio_cdda": "libcdio",
        "libass": "libass",
        "libfribidi": "fribidi",
        "libfreetype": "freetype",
        "libfontconfig": "fontconfig",
        "libharfbuzz": "harfbuzz",
        "libgnutls": "gnutls",
        "libnettle": "nettle",
        "libhogweed": "nettle",
        "libxml2": "libxml2",
        "libbz2": "bzip2",
        "liblzma": "xz",
        "libzstd": "zstd",
        "libpng": "libpng",
        "libjpeg": "libjpeg-turbo",
        "libwebp": "libwebp",
        "libopus": "opus",
        "libvorbis": "libvorbis",
        "libogg": "libogg",
        "libtheora": "libtheora",
        "libmp3lame": "lame",
        "libx264": "x264",
        "libx265": "x265",
        "libvpx": "libvpx",
        "libaom": "aom",
        "libdav1d": "dav1d",
        "libsvtav1": "svt-av1",
        "libdrm": "libdrm",
        "libva": "libva",
        "libvdpau": "libvdpau",
        "libplacebo": "libplacebo",
        "libshaderc": "shaderc",
        "libvulkan": "vulkan-sdk",
        "libSDL2": "SDL2",
        "libpulse": "pulseaudio",
        "libasound": "alsa-lib",
        "libsoxr": "soxr",
        "librubberband": "rubberband",
        "libvidstab": "vid.stab",
        "libzmq": "zeromq",
        "libsrt": "srt",
        "librist": "librist",
        "libssh": "libssh",
        "libgme": "game-music-emu",
        "libmodplug": "libmodplug",
        "libopenmpt": "libopenmpt",
        "libchromaprint": "chromaprint",
        "libbs2b": "libbs2b",
        "libbluray": "libbluray",
        "libaribb24": "aribb24",
        "liblensfun": "lensfun",
        "libtesseract": "tesseract",
        "liblept": "leptonica",
    }
    
    packages = set()
    for lib in missing_libs:
        # Try to match library name
        lib_base = lib.split(".so")[0]
        for lib_pattern, pkg in lib_to_pkg.items():
            if lib_pattern in lib_base:
                packages.add(pkg)
                break
        else:
            # Try removing 'lib' prefix and use as package name
            if lib_base.startswith("lib"):
                packages.add(lib_base[3:])
    
    return sorted(packages)


def print_ffprobe_fix_instructions(missing_libs: List[str], error_msg: str):
    """Print helpful instructions for fixing ffprobe issues."""
    print("\n" + "="*70)
    print("⚠️  FFPROBE DEPENDENCY ISSUE DETECTED")
    print("="*70)
    print(f"\nError: {error_msg}")
    
    if missing_libs:
        print(f"\nMissing libraries ({len(missing_libs)}):")
        for lib in missing_libs[:10]:
            print(f"  • {lib}")
        if len(missing_libs) > 10:
            print(f"  ... and {len(missing_libs) - 10} more")
        
        packages = suggest_library_packages(missing_libs)
        if packages:
            print(f"\n📦 Suggested packages to install:")
            print(f"   un-get install {' '.join(packages)}")
    
    print("\n🔧 Quick fix commands:")
    print("   # Find all missing libraries:")
    print("   ldd /usr/bin/ffprobe | grep 'not found'")
    print("")
    print("   # Install common ffmpeg dependencies:")
    print("   un-get install libcaca libcdio libcdio-paranoia openal-soft libass fribidi")
    print("")
    print("   # Test if ffprobe works:")
    print("   ffprobe -version")
    print("")
    print("💡 The script will continue with filename-only analysis (no ffprobe).")
    print("   Language detection from filenames still works!")
    print("="*70 + "\n")


def ffprobe_available() -> Optional[str]:
    is_working, exe, _, _ = check_ffprobe_dependencies()
    return exe if is_working else None


def run_ffprobe(file_path: Path, timeout_s: int = 30) -> Optional[dict]:
    exe = ffprobe_available()
    if not exe:
        return None

    cmd = [exe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(file_path)]
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = proc.communicate(timeout=timeout_s)
        if proc.returncode != 0:
            return {"_error": (stderr or "").strip()[:500]}
        return json.loads(stdout)
    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
            proc.communicate()
        return {"_error": f"timeout>{timeout_s}s"}
    except json.JSONDecodeError:
        return {"_error": "invalid json"}
    except Exception as e:
        return {"_error": str(e)[:200]}


def parse_ffprobe_meta(fp: Optional[dict]) -> dict:
    out = {"video_height": None, "video_codec": None, "audio_codecs": [], "audio_langs": [],
           "subtitle_langs": [], "audio_channels_max": None, "ffprobe_error": None}
    if not fp:
        return out
    if "_error" in fp:
        out["ffprobe_error"] = fp["_error"]
        return out
    streams = fp.get("streams") or []
    for s in streams:
        if s.get("codec_type") == "video" and out["video_height"] is None:
            out["video_height"] = s.get("height")
            out["video_codec"] = s.get("codec_name")
    max_ch = None
    for s in streams:
        ctype, codec = s.get("codec_type"), s.get("codec_name")
        lang = normalize_lang_code((s.get("tags") or {}).get("language", "und"))
        if ctype == "audio":
            if codec:
                out["audio_codecs"].append(codec)
            out["audio_langs"].append(lang)
            ch = s.get("channels")
            if isinstance(ch, int):
                max_ch = ch if max_ch is None else max(max_ch, ch)
        elif ctype == "subtitle":
            out["subtitle_langs"].append(lang)
    out["audio_codecs"] = sorted(set(out["audio_codecs"]))
    out["audio_langs"] = sorted(set(out["audio_langs"]))
    out["subtitle_langs"] = sorted(set(out["subtitle_langs"]))
    out["audio_channels_max"] = max_ch
    return out


# =============================================================================
# DATA MODEL
# =============================================================================

@dataclass
class MediaFile:
    path: str
    root: str
    relpath: str
    size: int
    mtime: float
    dev: int
    inode: int
    nlink: int
    show: Optional[str]
    season: Optional[int]
    episode: Optional[int]
    absolute_ep: Optional[int]
    name_resolution: Optional[int]
    name_source: Optional[str]
    name_video_codec: Optional[str]
    name_audio_hints: List[str]
    name_lang_hints: List[str]
    name_sub_hints: List[str]
    release_group: Optional[str]
    video_height: Optional[int]
    video_codec: Optional[str]
    audio_codecs: List[str]
    audio_langs: List[str]
    subtitle_langs: List[str]
    audio_channels_max: Optional[int]
    ffprobe_error: Optional[str]
    content_type: str
    score: int
    lang_score_reason: str
    note: str
    # qBittorrent seeding info
    is_seeding: bool = False
    torrent_hash: Optional[str] = None
    torrent_name: Optional[str] = None
    torrent_file_name: Optional[str] = None  # Original filename in torrent
    torrent_ratio: Optional[float] = None  # Share ratio from qBittorrent
    
    # Servarr (Sonarr/Radarr) integration
    arr_managed: bool = False
    arr_app: Optional[str] = None  # "sonarr" or "radarr"
    arr_instance: Optional[str] = None  # Instance name
    arr_media_id: Optional[int] = None  # seriesId or movieId
    arr_file_id: Optional[int] = None  # episodeFileId or movieFileId
    arr_title: Optional[str] = None  # Series/Movie title
    arr_quality: Optional[str] = None  # Quality name from Arr
    arr_custom_format_score: Optional[int] = None
    arr_custom_formats: Optional[str] = None  # Comma-separated
    arr_quality_profile: Optional[str] = None
    arr_cutoff_not_met: bool = False
    arr_upgrade_recommended: bool = False
    arr_upgrade_reason: Optional[str] = None
    arr_link: Optional[str] = None  # WebUI link (without API key)
    arr_in_queue: bool = False  # File is in download/import queue


# =============================================================================
# SCORING
# =============================================================================

def calculate_language_score(audio_langs: Set[str], subtitle_langs: Set[str],
                             content_type: ContentType, avoid_mode: AvoidMode,
                             avoid_langs: Set[str]) -> Tuple[int, str]:
    score, reason = 0, ""
    has_deu = "deu" in audio_langs or "ger" in audio_langs
    has_eng = "eng" in audio_langs
    has_jpn = "jpn" in audio_langs
    has_deu_subs = "deu" in subtitle_langs or "ger" in subtitle_langs

    if content_type == ContentType.ANIME:
        if has_deu and has_jpn:
            score, reason = CFG.SCORE_LANG_DUAL_DEU_JPN, "DEU+JPN dual-audio (best)"
        elif has_deu and has_eng:
            score, reason = CFG.SCORE_LANG_DUAL_DEU_ENG - 20, "DEU+ENG dual-audio"
        elif has_deu:
            score, reason = CFG.SCORE_LANG_ANIME_DEU, "DEU audio"
        elif has_jpn and has_deu_subs:
            score, reason = CFG.SCORE_LANG_JPN_DEU_SUBS, "JPN + DEU subs"
        elif has_jpn:
            score, reason = CFG.SCORE_LANG_JPN_ONLY, "JPN audio"
        elif has_eng:
            score, reason = CFG.SCORE_LANG_ANIME_ENG, "ENG audio (fallback)"
        else:
            reason = "no preferred language"
    else:
        if has_deu and has_eng:
            score, reason = CFG.SCORE_LANG_DUAL_DEU_ENG, "DEU+ENG dual-audio (best)"
        elif has_deu:
            score, reason = CFG.SCORE_LANG_DEU_ONLY, "DEU audio"
        elif has_eng:
            score, reason = CFG.SCORE_LANG_ENG_ONLY, "ENG audio"
        else:
            reason = "no preferred language"

    if avoid_langs and (audio_langs & avoid_langs):
        if avoid_mode == AvoidMode.STRICT:
            score += CFG.SCORE_AVOID_LANG_PENALTY
            reason += " [PENALTY]"
        elif avoid_mode == AvoidMode.IF_NO_PREFER and score <= 0:
            score += CFG.SCORE_AVOID_LANG_PENALTY
            reason += " [PENALTY]"
    return score, reason


def quality_score(m: MediaFile, content_type_override: ContentType,
                  avoid_mode: AvoidMode, avoid_langs: List[str]) -> Tuple[int, str]:
    score = 0
    h = m.video_height or m.name_resolution
    if h:
        if h >= 2160: score += CFG.SCORE_RES_4K
        elif h >= 1080: score += CFG.SCORE_RES_1080
        elif h >= 720: score += CFG.SCORE_RES_720
        elif h >= 480: score += CFG.SCORE_RES_480
        else: score += CFG.SCORE_RES_OTHER

    src = (m.name_source or "").lower()
    score += {"bluray": CFG.SCORE_SRC_BLURAY, "webdl": CFG.SCORE_SRC_WEBDL,
              "webrip": CFG.SCORE_SRC_WEBRIP, "hdtv": CFG.SCORE_SRC_HDTV}.get(src, 0)

    vcodec = (m.video_codec or m.name_video_codec or "").lower()
    if vcodec in ("hevc", "h265", "x265"): score += CFG.SCORE_CODEC_HEVC
    elif vcodec in ("h264", "x264", "avc"): score += CFG.SCORE_CODEC_AVC

    audio_all = set(a.lower() for a in (m.audio_codecs or []) + (m.name_audio_hints or []))
    for hint, pts in [("truehd", CFG.SCORE_AUDIO_TRUEHD), ("atmos", CFG.SCORE_AUDIO_ATMOS),
                      ("dtshd", CFG.SCORE_AUDIO_DTSHD), ("dts", CFG.SCORE_AUDIO_DTS),
                      ("eac3", CFG.SCORE_AUDIO_EAC3), ("ac3", CFG.SCORE_AUDIO_AC3), ("aac", CFG.SCORE_AUDIO_AAC)]:
        if hint in audio_all: score += pts

    ch = m.audio_channels_max
    if isinstance(ch, int):
        if ch >= 8: score += CFG.SCORE_CH_8PLUS
        elif ch >= 6: score += CFG.SCORE_CH_6PLUS
        elif ch >= 2: score += CFG.SCORE_CH_2PLUS

    audio_langs = set(normalize_lang_code(l) for l in (m.audio_langs or []) + (m.name_lang_hints or []))
    subtitle_langs = set(normalize_lang_code(l) for l in (m.subtitle_langs or []) + (m.name_sub_hints or []))
    ctype = content_type_override if content_type_override != ContentType.AUTO else ContentType(m.content_type)
    lang_score, lang_reason = calculate_language_score(audio_langs, subtitle_langs, ctype, avoid_mode, set(avoid_langs))
    return score + lang_score, lang_reason


# =============================================================================
# SCANNING & BUILDING
# =============================================================================

def iter_media_files(root: Path, exts: frozenset) -> Iterable[Path]:
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False) and Path(entry.path).suffix.lower() in exts:
                            yield Path(entry.path)
                    except OSError:
                        pass
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            pass


def build_base_record(p: Path, root: Path, roots: List[Path], ctype_override: ContentType) -> MediaFile:
    st = p.stat()
    ep = extract_episode_key(p.name) or extract_episode_key(str(p))
    abs_ep = extract_absolute_ep(p.name)
    show = guess_show_name(p)
    br = parse_bracket_tokens(p.name)
    ctype = ctype_override if ctype_override != ContentType.AUTO else detect_content_type(p, abs_ep)
    is_anime = ctype == ContentType.ANIME
    stem = p.stem
    relgrp = parse_release_group(stem)

    return MediaFile(
        path=str(p), root=str(root), relpath=safe_rel(p, roots),
        size=st.st_size, mtime=st.st_mtime,
        dev=getattr(st, "st_dev", 0), inode=getattr(st, "st_ino", 0), nlink=getattr(st, "st_nlink", 1),
        show=show, season=ep[0] if ep else None, episode=ep[1] if ep else None, absolute_ep=abs_ep,
        name_resolution=br["br_resolution"] or extract_resolution(p.name),
        name_source=br["br_source"] or match_first(p.name, SOURCE_HINTS),
        name_video_codec=br["br_video_codec"] or match_first(p.name, CODEC_HINTS),
        name_audio_hints=sorted(set(br["br_audio_hints"] + collect_many(p.name, AUDIO_HINTS))),
        name_lang_hints=sorted(extract_languages_from_filename(p.name, is_anime)),
        name_sub_hints=sorted(extract_subtitle_languages_from_filename(p.name)),
        release_group=relgrp,
        video_height=None, video_codec=None, audio_codecs=[], audio_langs=[],
        subtitle_langs=[], audio_channels_max=None, ffprobe_error=None,
        content_type=ctype.value, score=0, lang_score_reason="", note="filename-only"
    )


# =============================================================================
# CACHE
# =============================================================================

class CacheManager:
    def __init__(self, path: Path):
        self._path, self._lock = path, threading.Lock()
        self._cache = self._load()

    def _load(self) -> Dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except:
            return {}

    def save(self):
        with self._lock:
            try:
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(self._path)
            except OSError as e:
                LOG.error(f"Cache save failed: {e}")

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            return self._cache.get(key)

    def set(self, key: str, value: dict):
        with self._lock:
            self._cache[key] = value


def should_reuse_cache(cached: dict, st: os.stat_result) -> bool:
    return (cached.get("size") == st.st_size and cached.get("mtime") == st.st_mtime and
            cached.get("inode") == st.st_ino and cached.get("dev") == st.st_dev and
            cached.get("ffprobe_raw") is not None)


def enrich_with_ffprobe(m: MediaFile, cache: CacheManager, timeout: int,
                        ctype_override: ContentType, avoid_mode: AvoidMode, avoid_langs: List[str]) -> MediaFile:
    try:
        st = Path(m.path).stat()
    except OSError:
        return m
    cached = cache.get(m.path) or {}
    if should_reuse_cache(cached, st):
        fp_raw = cached["ffprobe_raw"]
    else:
        fp_raw = run_ffprobe(Path(m.path), timeout)
    fp = parse_ffprobe_meta(fp_raw)
    m.video_height, m.video_codec = fp["video_height"], fp["video_codec"]
    m.audio_codecs, m.audio_langs = fp["audio_codecs"], fp["audio_langs"]
    m.subtitle_langs, m.audio_channels_max = fp["subtitle_langs"], fp["audio_channels_max"]
    m.ffprobe_error, m.note = fp["ffprobe_error"], "ffprobe"
    m.score, m.lang_score_reason = quality_score(m, ctype_override, avoid_mode, avoid_langs)
    cache.set(m.path, {"size": st.st_size, "mtime": st.st_mtime, "inode": st.st_ino,
                       "dev": st.st_dev, "ffprobe_raw": fp_raw, "score": m.score})
    return m


# =============================================================================
# CSV/REPORT WRITING
# =============================================================================

def write_csv(path: Path, rows: List[dict], fieldnames: List[str]):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def fmt_mtime(ts: float) -> str:
    try:
        return _dt.datetime.fromtimestamp(ts).isoformat(sep=" ", timespec="seconds")
    except:
        return ""


def flatten_file_row(m: MediaFile) -> dict:
    d = asdict(m)
    for k in ["name_audio_hints", "name_lang_hints", "name_sub_hints", "audio_codecs", "audio_langs", "subtitle_langs"]:
        d[k] = ",".join(d.get(k) or [])
    d["mtime_human"] = fmt_mtime(m.mtime)
    return d


def generate_delete_script(path: Path, candidates: List[dict]):
    with path.open("w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env bash\nset -euo pipefail\n\n")
        f.write(f"# Generated by media_audit.py v{VERSION}\n# Review before running!\n\n")
        for d in candidates:
            f.write(f'rm -f -- {shlex.quote(d["path"])}  # {d["reason"][:60]}\n')
    try:
        os.chmod(path, 0o755)
    except:
        pass


# =============================================================================
# HTML REPORT GENERATOR (NEW in v2.3.0)
# =============================================================================

def generate_html_report(
    report_run: Path,
    summary: dict,
    media: List[MediaFile],
    episode_rows: List[dict],
    delete_candidates: List[dict],
    season_conflicts: List[dict],
    hardlinked: Dict[Tuple[int, int], List[MediaFile]],
    qbit_webui_url: Optional[str] = None,
) -> Path:
    """Generate a comprehensive HTML report."""
    
    # Get qBittorrent WebUI URL from environment if not provided
    if qbit_webui_url is None:
        qbit_webui_url = os.environ.get("QBIT_WEBUI_URL", "")
        if not qbit_webui_url:
            # Try to construct from host/port if available
            qbit_host = os.environ.get("QBIT_HOST", "localhost")
            qbit_port = os.environ.get("QBIT_PORT", "8080")
            if qbit_host and qbit_host != "localhost":
                qbit_webui_url = f"http://{qbit_host}:{qbit_port}"
    
    # Calculate statistics
    total_size = sum(m.size for m in media)
    delete_size = sum(int(d.get("size", 0)) for d in delete_candidates if d.get("size"))
    
    # Recalculate delete_size from media if not in candidates
    if delete_size == 0:
        delete_paths = set(d["path"] for d in delete_candidates)
        delete_size = sum(m.size for m in media if m.path in delete_paths)
    
    anime_count = sum(1 for m in media if m.content_type == "anime")
    series_count = sum(1 for m in media if m.content_type == "series")
    movie_count = sum(1 for m in media if m.content_type == "movie")
    
    # Language distribution
    lang_dist = {}
    for m in media:
        langs = set(m.audio_langs or m.name_lang_hints or [])
        for lang in langs:
            lang_dist[lang] = lang_dist.get(lang, 0) + 1
    
    # Resolution distribution
    res_dist = {}
    for m in media:
        res = m.video_height or m.name_resolution or 0
        if res >= 2160:
            key = "4K"
        elif res >= 1080:
            key = "1080p"
        elif res >= 720:
            key = "720p"
        elif res > 0:
            key = "SD"
        else:
            key = "Unknown"
        res_dist[key] = res_dist.get(key, 0) + 1
    
    # ffprobe errors
    ffprobe_errors = sum(1 for m in media if m.ffprobe_error)
    
    # Group duplicates by show for better display
    dupe_by_show = {}
    for row in episode_rows:
        show = row.get("show", "Unknown")
        if show not in dupe_by_show:
            dupe_by_show[show] = []
        dupe_by_show[show].append(row)
    
    # Build HTML parts separately to avoid nested f-strings
    def build_delete_rows():
        rows = []
        for d in delete_candidates[:500]:
            path_escaped = html.escape(d['path'])
            filename = html.escape(d['path'].split('/')[-1])
            score = d.get('score', 'N/A')
            best_score = d.get('best_score', 'N/A')
            res = d.get('res', 'N/A')
            langs = html.escape(str(d.get('audio_langs', '')))
            rows.append(f'<tr><td class="path" title="{path_escaped}">{filename}</td><td>{score}</td><td>{best_score}</td><td>{res}</td><td>{langs}</td></tr>')
        return '\n'.join(rows)
    
    def build_res_bars():
        bars = []
        colors = {'4K': '#00d26a', '1080p': '#6bcbff', '720p': '#ffd93d', 'SD': '#ff6b6b', 'Unknown': '#888'}
        for k, v in sorted(res_dist.items(), key=lambda x: -x[1]):
            width = min(100, v * 100 // max(1, len(media)))
            color = colors.get(k, '#888')
            bars.append(f'<div class="bar"><span class="bar-label">{k}</span><div class="bar-fill" style="width: {width}px; background: {color};"></div><span class="bar-value">{v:,}</span></div>')
        return '\n'.join(bars)
    
    def build_lang_bars():
        bars = []
        for k, v in sorted(lang_dist.items(), key=lambda x: -x[1])[:5]:
            width = min(100, v * 100 // max(1, len(media)))
            bars.append(f'<div class="bar"><span class="bar-label">{k.upper()}</span><div class="bar-fill" style="width: {width}px; background: #6bcbff;"></div><span class="bar-value">{v:,}</span></div>')
        return '\n'.join(bars)
    
    def build_show_sections():
        # Build show list with JSON data for lazy loading
        sections = []
        for show, rows in sorted(dupe_by_show.items(), key=lambda x: -len(x[1])):
            show_escaped = html.escape(show)
            show_id = re.sub(r'[^a-zA-Z0-9]', '_', show)
            row_count = len(rows)
            delete_count = sum(1 for r in rows if r.get('keep') != 'YES')
            
            sections.append(f'''
            <div class="show-header" data-show="{show_escaped}" onclick="toggleShow('{show_id}')">
                <span class="show-title">{show_escaped}</span>
                <span class="show-stats">
                    <span class="badge badge-total">{row_count} files</span>
                    <span class="badge badge-delete">{delete_count} to delete</span>
                </span>
            </div>
            <div class="show-content" id="show-{show_id}">
                <div class="show-actions">
                    <button class="btn btn-danger" onclick="showDeleteCommand('{show_escaped}')">🗑️ Show Delete Command</button>
                    <button class="btn btn-info" onclick="copyDeleteCommand('{show_escaped}')">📋 Copy Command</button>
                </div>
                <table class="show-table"><thead>
                    <tr><th>S</th><th>E</th><th>Keep</th><th>Score</th><th>Res</th><th>Source</th><th>Reason</th><th>Path</th></tr>
                </thead><tbody id="tbody-{show_id}"></tbody></table>
                <div class="show-more" id="more-{show_id}" style="display:none;">
                    <button class="btn btn-secondary" onclick="loadMore('{show_id}')">📥 Load more...</button>
                </div>
            </div>''')
        return '\n'.join(sections)
    
    def build_show_data_json():
        """Build JSON data for all shows for lazy loading."""
        media_by_path = {m.path: m for m in media}
        show_data = {}
        for show, rows in dupe_by_show.items():
            show_data[show] = []
            for r in rows:
                path = str(r.get('path', ''))
                m = media_by_path.get(path)
                
                # Score breakdown
                score_details = {}
                if m:
                    res = m.video_height or m.name_resolution or 0
                    if res >= 2160: score_details['resolution'] = f"4K (+{CFG.SCORE_RES_4K})"
                    elif res >= 1080: score_details['resolution'] = f"1080p (+{CFG.SCORE_RES_1080})"
                    elif res >= 720: score_details['resolution'] = f"720p (+{CFG.SCORE_RES_720})"
                    else: score_details['resolution'] = f"{res}p"
                    
                    src = (m.name_source or "").lower()
                    src_scores = {"bluray": CFG.SCORE_SRC_BLURAY, "webdl": CFG.SCORE_SRC_WEBDL, 
                                  "webrip": CFG.SCORE_SRC_WEBRIP, "hdtv": CFG.SCORE_SRC_HDTV}
                    if src in src_scores:
                        score_details['source'] = f"{src.upper()} (+{src_scores[src]})"
                    
                    vcodec = (m.video_codec or m.name_video_codec or "").lower()
                    if vcodec in ("hevc", "h265", "x265"): 
                        score_details['codec'] = f"HEVC (+{CFG.SCORE_CODEC_HEVC})"
                    elif vcodec in ("h264", "x264", "avc"): 
                        score_details['codec'] = f"AVC (+{CFG.SCORE_CODEC_AVC})"
                    
                    score_details['language'] = m.lang_score_reason or "unknown"
                    score_details['audio_langs'] = ','.join(m.audio_langs or m.name_lang_hints or [])
                    score_details['sub_langs'] = ','.join(m.subtitle_langs or m.name_sub_hints or [])
                
                # Find best score for comparison
                best_path = r.get('best_path', '')
                best_score = next((br.get('score', 0) for br in rows if br.get('path') == best_path), 0)
                current_score = r.get('score', 0)
                keep = r.get('keep', 'no')
                
                # Build reason - include seeding status
                is_seeding = r.get('is_seeding', False)
                torrent_name = r.get('torrent_name', '')
                torrent_ratio = r.get('torrent_ratio', 0.0)

                if keep == 'YES':
                    reason = "✓ Best version"
                elif is_seeding:
                    ratio_str = f" (Ratio: {torrent_ratio:.2f})" if torrent_ratio else ""
                    reason = f"🌱 SEEDING{ratio_str}"
                    if torrent_name:
                        reason = f"🌱 SEEDING: {torrent_name[:25]}{ratio_str}"
                else:
                    reasons = []
                    diff = best_score - current_score
                    if diff > 0:
                        reasons.append(f"Score -{diff}")
                    if m:
                        best_m = media_by_path.get(best_path)
                        if best_m:
                            best_res = best_m.video_height or best_m.name_resolution or 0
                            curr_res = m.video_height or m.name_resolution or 0
                            if curr_res < best_res:
                                reasons.append(f"Res: {curr_res}p<{best_res}p")
                            if m.lang_score_reason and m.lang_score_reason != best_m.lang_score_reason:
                                reasons.append(m.lang_score_reason)
                    reason = " | ".join(reasons) if reasons else "Duplicate"
                
                show_data[show].append({
                    'season': r.get('season', ''),
                    'episode': r.get('episode', ''),
                    'keep': "SEED" if is_seeding and keep != 'YES' else keep,
                    'score': current_score,
                    'best_score': best_score,
                    'res': r.get('res', ''),
                    'path': path,
                    'filename': path.split('/')[-1],
                    'reason': reason,
                    'score_details': score_details,
                    'is_seeding': is_seeding,
                    'torrent_name': torrent_name,
                    'torrent_file_name': r.get('torrent_file_name', ''),
                    'torrent_ratio': torrent_ratio
                })
        return json.dumps(show_data, ensure_ascii=False)
    
    def build_delete_commands_json():
        """Build delete commands for each show (excluding seeding files)."""
        commands = {}
        for show, rows in dupe_by_show.items():
            # Exclude seeding files from delete commands!
            delete_paths = [r['path'] for r in rows 
                           if r.get('keep') != 'YES' and not r.get('is_seeding', False)]
            if delete_paths:
                escaped = [shlex.quote(p) for p in delete_paths]
                cmd = "rm -f \\\n  " + " \\\n  ".join(escaped)
                commands[show] = {'count': len(delete_paths), 'command': cmd}
        return json.dumps(commands, ensure_ascii=False)
    
    def build_season_conflicts():
        if not season_conflicts:
            return ''
        rows = []
        for c in season_conflicts:
            show = html.escape(str(c.get('show', '')))
            season = c.get('season', '')
            folders = html.escape(str(c.get('folders', '')))
            recommended = html.escape(str(c.get('recommended', '')))
            rows.append(f'<tr><td>{show}</td><td>{season}</td><td style="color: var(--accent-red);">{folders}</td><td style="color: var(--accent-green);">{recommended}</td></tr>')
        return f'''
        <div class="section">
            <h2>⚠️ Season Folder Conflicts ({len(season_conflicts)})</h2>
            <table>
                <tr><th>Show</th><th>Season</th><th>Conflicting Folders</th><th>Recommended</th></tr>
                {''.join(rows)}
            </table>
        </div>
        '''
    
    def build_warnings():
        parts = []
        if ffprobe_errors > 0:
            parts.append(f'<div class="warning">⚠️ ffprobe errors on {ffprobe_errors} files - using filename-only analysis for those</div>')
        for w in summary.get("path_warnings", []):
            parts.append(f'<div class="warning">⚠️ {html.escape(w)}</div>')
        return '\n'.join(parts)
    
    # Pre-build all dynamic content
    delete_rows_html = build_delete_rows()
    res_bars_html = build_res_bars()
    lang_bars_html = build_lang_bars()
    show_sections_html = build_show_sections()
    show_data_json = build_show_data_json()
    delete_commands_json = build_delete_commands_json()
    season_conflicts_html = build_season_conflicts()
    warnings_html = build_warnings()
    
    # Calculate bar widths for content type
    anime_width = min(100, anime_count * 100 // max(1, len(media)))
    series_width = min(100, series_count * 100 // max(1, len(media)))
    movie_width = min(100, movie_count * 100 // max(1, len(media)))
    
    # More candidates text
    more_candidates = f'<p style="color: var(--text-secondary); margin-top: 10px;">Showing first 500 of {len(delete_candidates)} candidates</p>' if len(delete_candidates) > 500 else ''
    
    # Config values
    roots_str = ', '.join(summary.get('roots', []))
    delete_under_str = summary.get('delete_under', 'N/A')
    ffprobe_scope_str = summary.get('ffprobe_scope', 'N/A')
    ffprobe_found_str = '✅ Yes' if summary.get('ffprobe_found') else '❌ No'
    avoid_mode_str = summary.get('avoid_mode', 'N/A')
    lang_scoring_films = summary.get('language_scoring', {}).get('films_series', 'N/A')
    lang_scoring_anime = summary.get('language_scoring', {}).get('anime', 'N/A')
    
    timestamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    html_content = f'''<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Media Audit Report</title>
    <style>
        :root {{
            --bg-primary: #1a1a2e;
            --bg-secondary: #16213e;
            --bg-card: #0f3460;
            --text-primary: #eee;
            --text-secondary: #aaa;
            --accent-green: #00d26a;
            --accent-red: #ff6b6b;
            --accent-yellow: #ffd93d;
            --accent-blue: #6bcbff;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.6;
            padding: 20px;
        }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        h1 {{ 
            text-align: center; 
            margin-bottom: 30px;
            background: linear-gradient(135deg, var(--accent-blue), var(--accent-green));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            font-size: 2.5rem;
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        .stat-card {{
            background: var(--bg-card);
            border-radius: 12px;
            padding: 20px;
            text-align: center;
            box-shadow: 0 4px 6px rgba(0,0,0,0.3);
        }}
        .stat-card h3 {{ color: var(--text-secondary); font-size: 0.9rem; margin-bottom: 10px; }}
        .stat-card .value {{ font-size: 2rem; font-weight: bold; }}
        .stat-card .value.green {{ color: var(--accent-green); }}
        .stat-card .value.red {{ color: var(--accent-red); }}
        .stat-card .value.yellow {{ color: var(--accent-yellow); }}
        .stat-card .value.blue {{ color: var(--accent-blue); }}
        .section {{
            background: var(--bg-secondary);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
        }}
        .section h2 {{
            color: var(--accent-blue);
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid var(--bg-card);
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.85rem;
        }}
        th, td {{
            padding: 10px 8px;
            text-align: left;
            border-bottom: 1px solid var(--bg-card);
        }}
        th {{
            background: var(--bg-card);
            color: var(--accent-blue);
            position: sticky;
            top: 0;
            cursor: pointer;
        }}
        th:hover {{ background: var(--bg-primary); }}
        tr:hover {{ background: var(--bg-card); }}
        .keep-yes {{ color: var(--accent-green); font-weight: bold; }}
        .keep-no {{ color: var(--accent-red); }}
        .keep-seed {{ color: var(--accent-yellow); font-weight: bold; }}
        .keep-arr {{ color: var(--accent-orange); font-weight: bold; }}
        .source-badge {{
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 0.7rem;
            font-weight: 500;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 160px;
        }}
        .source-badge.seeding {{
            background: rgba(255, 230, 109, 0.2);
            color: var(--accent-yellow);
            border: 1px solid var(--accent-yellow);
        }}
        .source-badge.arr-managed {{
            background: rgba(255, 107, 129, 0.2);
            color: var(--accent-orange);
            border: 1px solid var(--accent-orange);
        }}
        .source-badge.arr-upgrade {{
            background: rgba(0, 184, 148, 0.2);
            color: var(--accent-green);
            border: 1px solid var(--accent-green);
        }}
        .torrent-badge {{
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 0.7rem;
            font-weight: 500;
        }}
        .torrent-badge.seeding {{
            background: rgba(255, 230, 109, 0.2);
            color: var(--accent-yellow);
            border: 1px solid var(--accent-yellow);
        }}
        .torrent-badge.not-seeding {{
            background: transparent;
            color: var(--text-secondary);
        }}
        .filter-buttons {{
            display: flex;
            gap: 10px;
            margin-bottom: 15px;
            flex-wrap: wrap;
        }}
        .filter-btn {{
            padding: 8px 16px;
            border: none;
            border-radius: 20px;
            cursor: pointer;
            font-size: 0.85rem;
            transition: all 0.2s;
            background: var(--bg-card);
            color: var(--text-primary);
        }}
        .filter-btn:hover {{ background: var(--bg-primary); }}
        .filter-btn.active {{ background: var(--accent-blue); color: var(--bg-primary); }}
        .filter-btn.active-yellow {{ background: var(--accent-yellow); color: var(--bg-primary); }}
        .filter-btn.active-orange {{ background: var(--accent-orange); color: var(--bg-primary); }}
        .filter-btn.active-red {{ background: var(--accent-red); color: white; }}
        .filter-btn.active-green {{ background: var(--accent-green); color: var(--bg-primary); }}
        .qbit-link {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 8px 16px;
            background: linear-gradient(135deg, #2980b9, #3498db);
            color: white;
            text-decoration: none;
            border-radius: 8px;
            font-size: 0.85rem;
            transition: all 0.2s;
        }}
        .qbit-link:hover {{ transform: translateY(-2px); box-shadow: 0 4px 12px rgba(52, 152, 219, 0.4); }}
        .path {{ 
            max-width: 400px; 
            overflow: hidden; 
            text-overflow: ellipsis; 
            white-space: nowrap;
            font-family: monospace;
            font-size: 0.75rem;
        }}
        .warning {{ 
            background: rgba(255, 107, 107, 0.2); 
            border-left: 4px solid var(--accent-red);
            padding: 10px 15px;
            margin: 10px 0;
            border-radius: 0 8px 8px 0;
        }}
        .info {{
            background: rgba(107, 203, 255, 0.2);
            border-left: 4px solid var(--accent-blue);
            padding: 10px 15px;
            margin: 10px 0;
            border-radius: 0 8px 8px 0;
        }}
        .filter-input {{
            width: 100%;
            padding: 10px;
            margin-bottom: 15px;
            border: none;
            border-radius: 8px;
            background: var(--bg-card);
            color: var(--text-primary);
            font-size: 1rem;
        }}
        .filter-input:focus {{ outline: 2px solid var(--accent-blue); }}
        .mini-chart {{
            background: var(--bg-card);
            border-radius: 8px;
            padding: 15px;
            min-width: 150px;
            display: inline-block;
            margin: 10px;
            vertical-align: top;
        }}
        .mini-chart h4 {{ font-size: 0.8rem; color: var(--text-secondary); margin-bottom: 10px; }}
        .bar {{
            display: flex;
            align-items: center;
            margin: 5px 0;
        }}
        .bar-label {{ width: 60px; font-size: 0.75rem; }}
        .bar-fill {{
            height: 16px;
            border-radius: 4px;
            margin-right: 8px;
        }}
        .bar-value {{ font-size: 0.75rem; color: var(--text-secondary); }}
        .collapsible {{
            cursor: pointer;
            padding: 10px;
            background: var(--bg-card);
            border-radius: 8px;
            margin: 5px 0;
        }}
        .collapsible:hover {{ background: var(--bg-primary); }}
        .content {{ display: none; padding: 10px; }}
        .content.show {{ display: block; }}
        .show-header {{ cursor: pointer; padding: 12px 15px; background: var(--bg-card); border-radius: 8px; margin: 5px 0; display: flex; justify-content: space-between; align-items: center; transition: all 0.2s; }}
        .show-header:hover {{ background: var(--bg-primary); transform: translateX(5px); }}
        .show-header.active {{ background: var(--accent-blue); color: var(--bg-primary); }}
        .show-title {{ font-weight: bold; }}
        .show-stats {{ display: flex; gap: 10px; }}
        .badge {{ padding: 3px 10px; border-radius: 12px; font-size: 0.75rem; font-weight: bold; }}
        .badge-total {{ background: var(--accent-blue); color: var(--bg-primary); }}
        .badge-delete {{ background: var(--accent-red); color: white; }}
        .show-content {{ display: none; padding: 15px; background: rgba(15, 52, 96, 0.5); border-radius: 0 0 8px 8px; margin-top: -5px; margin-bottom: 10px; }}
        .show-content.active {{ display: block; }}
        .show-actions {{ margin-bottom: 15px; display: flex; gap: 10px; flex-wrap: wrap; }}
        .btn {{ padding: 8px 16px; border: none; border-radius: 6px; cursor: pointer; font-size: 0.85rem; }}
        .btn-danger {{ background: var(--accent-red); color: white; }}
        .btn-info {{ background: var(--accent-blue); color: var(--bg-primary); }}
        .btn-secondary {{ background: var(--bg-card); color: var(--text-primary); }}
        .show-more {{ text-align: center; padding: 10px; }}
        .score-cell {{ cursor: pointer; text-decoration: underline dotted; }}
        .score-cell:hover {{ color: var(--accent-blue); }}
        .reason-cell {{ max-width: 250px; font-size: 0.7rem; color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
        .reason-cell.keep {{ color: var(--accent-green); }}
        .modal {{ display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 1000; justify-content: center; align-items: center; }}
        .modal.active {{ display: flex; }}
        .modal-content {{ background: var(--bg-secondary); border-radius: 12px; padding: 25px; max-width: 800px; width: 90%; max-height: 80vh; overflow-y: auto; position: relative; }}
        .modal-close {{ position: absolute; top: 10px; right: 15px; font-size: 1.5rem; cursor: pointer; color: var(--text-secondary); }}
        .modal h3 {{ color: var(--accent-blue); margin-bottom: 15px; }}
        .command-box {{ background: var(--bg-primary); border: 1px solid var(--bg-card); border-radius: 8px; padding: 15px; font-family: monospace; font-size: 0.75rem; white-space: pre-wrap; word-break: break-all; max-height: 400px; overflow-y: auto; }}
        .score-breakdown {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
        .score-item {{ background: var(--bg-card); padding: 10px; border-radius: 6px; }}
        .score-item label {{ display: block; font-size: 0.75rem; color: var(--text-secondary); }}
        .copy-success {{ position: fixed; bottom: 20px; right: 20px; background: var(--accent-green); color: var(--bg-primary); padding: 10px 20px; border-radius: 8px; display: none; z-index: 1001; }}
        .copy-success.show {{ display: block; }}
        @media (max-width: 768px) {{
            .stats-grid {{ grid-template-columns: repeat(2, 1fr); }}
            .path {{ max-width: 200px; }}
            .score-breakdown {{ grid-template-columns: 1fr; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 Media Audit Report</h1>
        <p style="text-align: center; color: var(--text-secondary); margin-bottom: 30px;">
            Generated: {timestamp} | Version: {VERSION}
        </p>

        <!-- Summary Stats -->
        <div class="stats-grid">
            <div class="stat-card">
                <h3>📁 Total Files</h3>
                <div class="value blue">{summary.get("scanned_files", 0):,}</div>
            </div>
            <div class="stat-card">
                <h3>💾 Total Size</h3>
                <div class="value blue">{format_size(total_size)}</div>
            </div>
            <div class="stat-card">
                <h3>🔄 Duplicate Groups</h3>
                <div class="value yellow">{summary.get("episode_duplicate_groups", 0):,}</div>
            </div>
            <div class="stat-card">
                <h3>🗑️ Delete Candidates</h3>
                <div class="value red">{summary.get("delete_candidates_count", 0):,}</div>
            </div>
            <div class="stat-card">
                <h3>💰 Space to Reclaim</h3>
                <div class="value green">{format_size(delete_size)}</div>
            </div>
            <div class="stat-card">
                <h3>🌱 Seeding (Protected)</h3>
                <div class="value yellow">{summary.get("seeding_files_total", 0):,}</div>
            </div>
            <div class="stat-card">
                <h3>🔗 Hardlinks</h3>
                <div class="value blue">{summary.get("hardlink_groups", 0):,}</div>
            </div>
        </div>
        
        <!-- qBittorrent Status -->
        <div class="section" style="background: linear-gradient(135deg, rgba(255, 230, 109, 0.1), rgba(107, 203, 255, 0.1));">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:15px;">
                <div>
                    <h3 style="color:var(--accent-yellow);margin-bottom:8px;">🌱 qBittorrent Integration</h3>
                    <p style="color:var(--text-secondary);font-size:0.9rem;">
                        <strong style="color:var(--accent-yellow);">{summary.get("seeding_files_total", 0):,}</strong> Dateien in Torrents erkannt &nbsp;|&nbsp;
                        <strong style="color:var(--accent-green);">{summary.get("seeding_files_protected", 0):,}</strong> vor Löschung geschützt
                    </p>
                    <p style="color:var(--text-secondary);font-size:0.8rem;margin-top:5px;">
                        ⚠️ Geschützte Dateien können nur über qBittorrent gelöscht werden
                    </p>
                </div>
                {f'<a href="{qbit_webui_url}" target="_blank" class="qbit-link">📥 qBittorrent öffnen</a>' if qbit_webui_url else ''}
            </div>
        </div>

        <!-- Sonarr/Radarr Status -->
        <div class="section" style="background: linear-gradient(135deg, rgba(255, 107, 129, 0.1), rgba(107, 203, 255, 0.1));">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:15px;">
                <div>
                    <h3 style="color:var(--accent-orange);margin-bottom:8px;">📺 Sonarr/Radarr Integration</h3>
                    <p style="color:var(--text-secondary);font-size:0.9rem;">
                        <strong style="color:var(--accent-orange);">{summary.get("arr_managed_files", 0):,}</strong> Dateien von Arr verwaltet &nbsp;|&nbsp;
                        <strong style="color:var(--accent-green);">{summary.get("arr_protected", 0):,}</strong> vor Löschung geschützt &nbsp;|&nbsp;
                        <strong style="color:var(--accent-yellow);">{summary.get("arr_upgrade_recommended", 0):,}</strong> Upgrade empfohlen
                    </p>
                    <p style="color:var(--text-secondary);font-size:0.8rem;margin-top:5px;">
                        ℹ️ Von Arr verwaltete Dateien werden automatisch geschützt. Upgrades werden von Sonarr/Radarr verwaltet.
                    </p>
                </div>
            </div>
        </div>

        <!-- Missing Hardlinks Warning -->
        <div class="section" style="background: linear-gradient(135deg, rgba(255, 71, 87, 0.15), rgba(255, 165, 2, 0.1));">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:15px;">
                <div>
                    <h3 style="color:var(--accent-red);margin-bottom:8px;">⚠️ Missing Hardlinks (Platzverschwendung)</h3>
                    <p style="color:var(--text-secondary);font-size:0.9rem;">
                        <strong style="color:var(--accent-red);">{summary.get("missing_hardlinks_count", 0):,}</strong> Dateien ohne Hardlink &nbsp;|&nbsp;
                        <strong style="color:var(--accent-red);">{summary.get("missing_hardlinks_wasted_gb", 0):.1f} GB</strong> verschwendet &nbsp;|&nbsp;
                        <strong style="color:var(--accent-yellow);">{summary.get("seeding_not_in_arr", 0):,}</strong> Torrents nicht in Arr
                    </p>
                    <p style="color:var(--text-secondary);font-size:0.8rem;margin-top:5px;">
                        Diese Dateien sind sowohl in Sonarr/Radarr als auch in qBittorrent, aber OHNE Hardlink - sie belegen 2x Speicherplatz!
                    </p>
                </div>
            </div>
            <details style="margin-top:15px;">
                <summary style="cursor:pointer;color:var(--accent-yellow);">📋 Seeding Übersicht ({summary.get("seeding_files_total", 0):,} Dateien)</summary>
                <div style="margin-top:10px;padding:10px;background:var(--bg-primary);border-radius:8px;">
                    <p><strong>🌱 Gesamt seedend:</strong> {summary.get("seeding_files_total", 0):,}</p>
                    <p><strong>🔗 Mit Hardlink:</strong> {summary.get("seeding_with_hardlink", 0):,}</p>
                    <p><strong>⚠️ Ohne Hardlink (in Arr):</strong> {summary.get("missing_hardlinks_count", 0):,}</p>
                    <p><strong>❓ Nicht in Arr:</strong> {summary.get("seeding_not_in_arr", 0):,}</p>
                    <p style="margin-top:10px;font-size:0.85rem;color:var(--text-secondary);">
                        CSV-Reports: <code>missing_hardlinks.csv</code>, <code>all_seeding.csv</code>, <code>seeding_not_in_arr.csv</code>
                    </p>
                </div>
            </details>
        </div>

        <!-- Warnings -->
        {warnings_html}

        <!-- Content Type & Language Distribution -->
        <div class="section">
            <h2>📈 Distribution Overview</h2>
            <div class="mini-chart">
                <h4>Content Type</h4>
                <div class="bar">
                    <span class="bar-label">Anime</span>
                    <div class="bar-fill" style="width: {anime_width}px; background: #e056fd;"></div>
                    <span class="bar-value">{anime_count:,}</span>
                </div>
                <div class="bar">
                    <span class="bar-label">Series</span>
                    <div class="bar-fill" style="width: {series_width}px; background: #0984e3;"></div>
                    <span class="bar-value">{series_count:,}</span>
                </div>
                <div class="bar">
                    <span class="bar-label">Movies</span>
                    <div class="bar-fill" style="width: {movie_width}px; background: #00b894;"></div>
                    <span class="bar-value">{movie_count:,}</span>
                </div>
            </div>
            <div class="mini-chart">
                <h4>Resolution</h4>
                {res_bars_html}
            </div>
            <div class="mini-chart">
                <h4>Languages (Top 5)</h4>
                {lang_bars_html}
            </div>
        </div>

        <!-- Delete Candidates -->
        <div class="section">
            <h2>🗑️ Delete Candidates ({len(delete_candidates)})</h2>
            <div class="info">These files are marked for potential deletion. The "best" version will be kept.</div>
            <input type="text" class="filter-input" id="deleteFilter" placeholder="🔍 Filter by path..." onkeyup="filterTable('deleteTable', this.value)">
            <div style="max-height: 500px; overflow-y: auto;">
                <table id="deleteTable">
                    <thead>
                        <tr>
                            <th onclick="sortTable('deleteTable', 0)">Path</th>
                            <th onclick="sortTable('deleteTable', 1)">Score</th>
                            <th onclick="sortTable('deleteTable', 2)">Best Score</th>
                            <th onclick="sortTable('deleteTable', 3)">Resolution</th>
                            <th onclick="sortTable('deleteTable', 4)">Languages</th>
                        </tr>
                    </thead>
                    <tbody>
                        {delete_rows_html}
                    </tbody>
                </table>
            </div>
            {more_candidates}
        </div>

        <!-- Episode Duplicates by Show -->
        <div class="section">
            <h2>📺 Episode Duplicates by Show ({len(dupe_by_show)} shows)</h2>
            <div class="info">💡 Click show to expand. Click <strong>Score</strong> for breakdown. Use <strong>Show Delete Command</strong> for terminal command.</div>
            
            <!-- Filter Buttons -->
            <div class="filter-buttons">
                <button class="filter-btn active" onclick="filterByStatus('all', this)">📁 Alle</button>
                <button class="filter-btn" onclick="filterByStatus('seeding', this)">🌱 Seeding</button>
                <button class="filter-btn" onclick="filterByStatus('arr', this)">📺 Arr-Managed</button>
                <button class="filter-btn" onclick="filterByStatus('deletable', this)">🗑️ Löschbar</button>
                <button class="filter-btn" onclick="filterByStatus('keep', this)">✓ Behalten</button>
            </div>
            
            <input type="text" class="filter-input" id="showFilter" placeholder="🔍 Filter shows..." onkeyup="filterShows(this.value)">
            <div id="showList">
                {show_sections_html}
            </div>
        </div>

        <!-- Season Conflicts -->
        {season_conflicts_html}

        <!-- Config Summary -->
        <div class="section">
            <h2>⚙️ Configuration</h2>
            <pre style="background: var(--bg-card); padding: 15px; border-radius: 8px; overflow-x: auto; font-size: 0.8rem;">
Roots: {roots_str}
Delete Under: {delete_under_str}
FFprobe Scope: {ffprobe_scope_str}
FFprobe Found: {ffprobe_found_str}
Avoid Mode: {avoid_mode_str}

Language Scoring:
  Films/Series: {lang_scoring_films}
  Anime: {lang_scoring_anime}
            </pre>
        </div>

        <!-- Footer -->
        <p style="text-align: center; color: var(--text-secondary); margin-top: 30px; padding: 20px;">
            Generated by <strong>media_audit.py v{VERSION}</strong>
        </p>
    </div>
    
    <!-- Modal -->
    <div class="modal" id="modal" onclick="closeModal(event)">
        <div class="modal-content" onclick="event.stopPropagation()">
            <span class="modal-close" onclick="closeModal()">&times;</span>
            <div id="modalBody"></div>
        </div>
    </div>
    <div class="copy-success" id="copySuccess">✅ Copied!</div>

    <script>
        const showData = {show_data_json};
        const deleteCommands = {delete_commands_json};
        const loadedRows = {{}};
        const INITIAL_LOAD = 30, LOAD_MORE = 100;

        function toggleShow(showId) {{
            const content = document.getElementById('show-' + showId);
            const header = content.previousElementSibling;
            if (content.classList.contains('active')) {{
                content.classList.remove('active');
                header.classList.remove('active');
            }} else {{
                content.classList.add('active');
                header.classList.add('active');
                if (!loadedRows[showId]) {{
                    loadedRows[showId] = 0;
                    loadShowRows(showId, header.getAttribute('data-show'), INITIAL_LOAD);
                }}
            }}
        }}

        function loadShowRows(showId, showName, count) {{
            const tbody = document.getElementById('tbody-' + showId);
            const moreBtn = document.getElementById('more-' + showId);
            const data = showData[showName] || [];
            const start = loadedRows[showId], end = Math.min(start + count, data.length);
            for (let i = start; i < end; i++) {{
                const r = data[i], tr = document.createElement('tr');
                tr.setAttribute('data-status', r.keep === 'YES' ? 'keep' : (r.is_seeding ? 'seeding' : (r.arr_managed ? 'arr' : 'deletable')));
                const keepClass = r.keep === 'YES' ? 'keep-yes' : (r.keep === 'SEED' ? 'keep-seed' : (r.arr_managed ? 'keep-arr' : 'keep-no'));
                const reasonClass = r.keep === 'YES' ? 'reason-cell keep' : 'reason-cell';
                // Build source cell showing seeding, arr-managed, or plain status
                let sourceCell = '<span class="source-badge">—</span>';
                if (r.is_seeding) {{
                    const tName = (r.torrent_name || 'Torrent').substring(0, 18);
                    sourceCell = '<span class="source-badge seeding" title="Seeding: ' + esc(r.torrent_name || '') + '">🌱 ' + esc(tName) + '</span>';
                }} else if (r.arr_managed) {{
                    const arrName = r.arr_instance || r.arr_app || 'Arr';
                    const arrInfo = r.arr_quality ? arrName + ' (' + r.arr_quality + ')' : arrName;
                    const arrTitle = 'Managed by ' + (r.arr_app || 'Arr') + ': ' + (r.arr_title || '') + (r.arr_upgrade_recommended ? ' [UPGRADE]' : '');
                    sourceCell = '<span class="source-badge arr-managed" title="' + esc(arrTitle) + '">📺 ' + esc(arrInfo.substring(0,18)) + '</span>';
                    if (r.arr_upgrade_recommended) {{
                        sourceCell = '<span class="source-badge arr-upgrade" title="' + esc(arrTitle) + '">⬆️ ' + esc(arrInfo.substring(0,18)) + '</span>';
                    }}
                }}
                tr.innerHTML = '<td>' + r.season + '</td><td>' + r.episode + '</td><td class="' + keepClass + '">' + r.keep + '</td><td class="score-cell" onclick="showScoreDetails(\\'' + esc(showName) + '\\',' + i + ')">' + r.score + '</td><td>' + r.res + '</td><td style="max-width:180px;">' + sourceCell + '</td><td class="' + reasonClass + '" title="' + esc(r.reason) + '">' + esc(r.reason).substring(0,30) + '</td><td class="path" title="' + esc(r.path) + '">' + esc(r.filename) + '</td>';
                tbody.appendChild(tr);
            }}
            loadedRows[showId] = end;
            moreBtn.style.display = end < data.length ? 'block' : 'none';
            if (end < data.length) moreBtn.querySelector('button').textContent = '📥 Load more (' + (data.length - end) + ')';
        }}

        function loadMore(showId) {{
            const header = document.querySelector('[onclick="toggleShow(\\'' + showId + '\\')"]');
            loadShowRows(showId, header.getAttribute('data-show'), LOAD_MORE);
        }}

        function showScoreDetails(showName, idx) {{
            const d = showData[showName][idx], s = d.score_details || {{}};
            let torrentInfo = '';
            if (d.is_seeding) {{
                torrentInfo = '<div style="margin-top:15px;padding:15px;background:linear-gradient(135deg, rgba(255, 230, 109, 0.2), rgba(255, 230, 109, 0.1));border:1px solid var(--accent-yellow);border-radius:8px;"><div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;"><span style="font-size:1.5rem;">🌱</span><strong style="color:var(--accent-yellow);">In Torrent (geschützt)</strong></div><div style="font-size:0.85rem;color:var(--text-secondary);"><strong>Torrent:</strong> ' + esc(d.torrent_name || 'N/A') + '<br><strong>Datei:</strong> ' + esc(d.torrent_file_name || d.filename) + '</div><a href="http://192.168.1.39:8081" target="_blank" style="display:inline-block;margin-top:10px;padding:6px 12px;background:var(--accent-yellow);color:var(--bg-primary);border-radius:6px;text-decoration:none;font-size:0.8rem;">📥 In qBittorrent öffnen</a></div>';
            }}
            document.getElementById('modalBody').innerHTML = '<h3>📊 Score: ' + d.score + '</h3><p style="word-break:break-all;color:var(--text-secondary);margin-bottom:15px;">' + esc(d.filename) + '</p><div class="score-breakdown"><div class="score-item"><label>Resolution</label>' + (s.resolution||'N/A') + '</div><div class="score-item"><label>Source</label>' + (s.source||'N/A') + '</div><div class="score-item"><label>Codec</label>' + (s.codec||'N/A') + '</div><div class="score-item"><label>Language</label>' + (s.language||'N/A') + '</div><div class="score-item"><label>Audio</label>' + (s.audio_langs||'N/A') + '</div><div class="score-item"><label>Subtitles</label>' + (s.sub_langs||'N/A') + '</div></div><div style="margin-top:15px;padding:10px;background:var(--bg-card);border-radius:6px;"><strong>Reason:</strong> ' + esc(d.reason) + '</div>' + torrentInfo;
            document.getElementById('modal').classList.add('active');
        }}

        function showDeleteCommand(showName) {{
            const cmd = deleteCommands[showName];
            if (!cmd) {{ alert('No files to delete'); return; }}
            document.getElementById('modalBody').innerHTML = '<h3>🗑️ Delete ' + cmd.count + ' files</h3><p style="color:var(--text-secondary);margin-bottom:10px;">' + esc(showName) + '</p><div class="command-box" id="cmdBox">' + esc(cmd.command) + '</div><div style="margin-top:15px;display:flex;gap:10px;"><button class="btn btn-info" onclick="copyCmd()">📋 Copy</button><button class="btn btn-secondary" onclick="downloadCmd(\\'' + esc(showName) + '\\')">💾 Download .sh</button></div><div class="warning" style="margin-top:15px;">⚠️ Review before running!</div>';
            document.getElementById('modal').classList.add('active');
        }}

        function copyDeleteCommand(showName) {{
            const cmd = deleteCommands[showName];
            if (cmd) navigator.clipboard.writeText(cmd.command).then(() => {{ document.getElementById('copySuccess').classList.add('show'); setTimeout(() => document.getElementById('copySuccess').classList.remove('show'), 2000); }});
        }}

        function copyCmd() {{
            navigator.clipboard.writeText(document.getElementById('cmdBox').textContent).then(() => {{ document.getElementById('copySuccess').classList.add('show'); setTimeout(() => document.getElementById('copySuccess').classList.remove('show'), 2000); }});
        }}

        function downloadCmd(showName) {{
            const cmd = deleteCommands[showName];
            if (!cmd) return;
            const blob = new Blob(['#!/bin/bash\\n# Delete: ' + showName + '\\nset -euo pipefail\\n\\n' + cmd.command], {{type:'text/plain'}});
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'delete_' + showName.replace(/[^a-z0-9]/gi,'_') + '.sh';
            a.click();
        }}

        function closeModal(e) {{ if (!e || e.target.id === 'modal') document.getElementById('modal').classList.remove('active'); }}
        function filterShows(q) {{ q = q.toLowerCase(); document.querySelectorAll('.show-header').forEach(h => {{ const m = h.getAttribute('data-show').toLowerCase().includes(q); h.style.display = m ? '' : 'none'; h.nextElementSibling.style.display = m && h.classList.contains('active') ? 'block' : 'none'; }}); }}
        function esc(t) {{ if (!t) return ''; const d = document.createElement('div'); d.textContent = t; return d.innerHTML; }}
        function filterTable(tid, q) {{ q = q.toLowerCase(); Array.from(document.getElementById(tid).rows).slice(1).forEach(r => r.style.display = r.textContent.toLowerCase().includes(q) ? '' : 'none'); }}
        function sortTable(tid, col) {{ const t = document.getElementById(tid), rows = Array.from(t.rows).slice(1), dir = t.getAttribute('data-dir') === 'asc' ? 'desc' : 'asc'; t.setAttribute('data-dir', dir); rows.sort((a,b) => {{ const av = a.cells[col].textContent, bv = b.cells[col].textContent, an = parseFloat(av), bn = parseFloat(bv); return !isNaN(an) && !isNaN(bn) ? (dir==='asc'?an-bn:bn-an) : (dir==='asc'?av.localeCompare(bv):bv.localeCompare(av)); }}); rows.forEach(r => t.tBodies[0].appendChild(r)); }}
        
        let currentFilter = 'all';
        function filterByStatus(status, btn) {{
            currentFilter = status;
            document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active', 'active-yellow', 'active-red', 'active-green', 'active-orange'));
            if (status === 'seeding') btn.classList.add('active-yellow');
            else if (status === 'arr') btn.classList.add('active-orange');
            else if (status === 'deletable') btn.classList.add('active-red');
            else if (status === 'keep') btn.classList.add('active-green');
            else btn.classList.add('active');

            document.querySelectorAll('tr[data-status]').forEach(row => {{
                if (status === 'all') row.style.display = '';
                else row.style.display = row.getAttribute('data-status') === status ? '' : 'none';
            }});
        }}
        
        document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeModal(); }});
    </script>
</body>
</html>'''

    html_path = report_run / "report.html"
    html_path.write_text(html_content, encoding="utf-8")
    return html_path


# =============================================================================
# MAIN FUNCTIONS
# =============================================================================

def scan_media_files(roots: List[Path], max_files: int, ctype: ContentType,
                     avoid_mode: AvoidMode, avoid_langs: List[str]) -> List[MediaFile]:
    media = []
    count = 0
    sample_count = 0
    for root in roots:
        for p in iter_media_files(root, CFG.VIDEO_EXTS):
            try:
                # Skip sample files entirely
                path_lower = str(p).lower()
                if "/sample/" in path_lower or "/samples/" in path_lower:
                    sample_count += 1
                    continue
                name_lower = p.name.lower()
                if name_lower.startswith("sample") or "-sample." in name_lower or ".sample." in name_lower:
                    sample_count += 1
                    continue
                
                m = build_base_record(p, root, roots, ctype)
                m.score, m.lang_score_reason = quality_score(m, ctype, avoid_mode, avoid_langs)
                media.append(m)
                count += 1
                if max_files and count >= max_files:
                    if sample_count > 0:
                        LOG.info(f"Skipped {sample_count} sample files")
                    return media
            except OSError:
                pass
    if sample_count > 0:
        LOG.info(f"Skipped {sample_count} sample files")
    return media


def enrich_media_with_ffprobe(media: List[MediaFile], indices: List[int], cache: CacheManager,
                              timeout: int, workers: int, ctype: ContentType,
                              avoid_mode: AvoidMode, avoid_langs: List[str]):
    if not indices:
        return
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(enrich_with_ffprobe, media[i], cache, timeout, ctype, avoid_mode, avoid_langs): i for i in indices}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                media[i] = fut.result()
            except Exception as e:
                media[i].ffprobe_error = str(e)
                media[i].note = "ffprobe-error"


def generate_reports(media, ep_groups, hardlinked, delete_under, avoid_mode, avoid_langs,
                     include_hardlinked, report_run, ffprobe_scope, fp_bin, roots, path_warnings,
                     generate_html: bool) -> Tuple[List[dict], dict]:
    episode_rows, delete_candidates = [], []
    ep_dupes = {k: v for k, v in ep_groups.items() if len(v) > 1}
    seeding_protected = 0
    arr_protected = 0

    for (show, s, e), idxs in sorted(ep_dupes.items(), key=lambda kv: (kv[0][0].lower(), kv[0][1], kv[0][2])):
        items = [media[i] for i in idxs]
        best = max(items, key=lambda m: (m.score, m.video_height or m.name_resolution or 0, m.mtime, m.size))
        for m in sorted(items, key=lambda x: (-x.score, -(x.video_height or x.name_resolution or 0))):
            keep = m.path == best.path
            
            # Add seeding and arr info to row
            row_note = m.note
            if m.is_seeding:
                row_note = f"SEEDING:{m.torrent_name}" if m.torrent_name else "SEEDING"
            elif m.arr_managed:
                row_note = f"ARR:{m.arr_app}:{m.arr_instance}" if m.arr_instance else f"ARR:{m.arr_app}"
            
            episode_rows.append({
                "show": show, "season": s, "episode": e, "absolute_ep": m.absolute_ep or "",
                "content_type": m.content_type, "best_path": best.path, "path": m.path,
                "score": m.score, "lang_reason": m.lang_score_reason, "size": m.size,
                "nlink": m.nlink, "res": m.video_height or m.name_resolution or "",
                "source": m.name_source or "", "vcodec": m.video_codec or m.name_video_codec or "",
                "audio_langs": ",".join(m.audio_langs or m.name_lang_hints),
                "sub_langs": ",".join(m.subtitle_langs or m.name_sub_hints or []),
                "release_group": m.release_group or "", "keep": "YES" if keep else "no",
                "note": row_note, "ffprobe_error": m.ffprobe_error or "",
                "is_seeding": m.is_seeding, "torrent_name": m.torrent_name or "",
                "torrent_file_name": m.torrent_file_name or "",
                "torrent_ratio": m.torrent_ratio or 0.0,
                # Servarr fields
                "arr_managed": m.arr_managed, "arr_app": m.arr_app or "",
                "arr_instance": m.arr_instance or "", "arr_media_id": m.arr_media_id or "",
                "arr_file_id": m.arr_file_id or "", "arr_title": m.arr_title or "",
                "arr_quality": m.arr_quality or "", "arr_custom_format_score": m.arr_custom_format_score or "",
                "arr_custom_formats": m.arr_custom_formats or "",
                "arr_quality_profile": m.arr_quality_profile or "",
                "arr_cutoff_not_met": m.arr_cutoff_not_met,
                "arr_upgrade_recommended": m.arr_upgrade_recommended,
                "arr_upgrade_reason": m.arr_upgrade_reason or "",
                "arr_link": m.arr_link or "", "arr_in_queue": m.arr_in_queue
            })
            
            # Only add to delete candidates if NOT seeding AND NOT arr_managed (unless keep)
            if not keep and is_path_safe_for_deletion(Path(m.path), delete_under):
                # Skip seeding files
                if m.is_seeding:
                    seeding_protected += 1
                    continue
                # Skip Arr-managed files (they are the "active" file in Sonarr/Radarr)
                if m.arr_managed:
                    arr_protected += 1
                    continue
                # Skip files in download queue
                if m.arr_in_queue:
                    arr_protected += 1
                    continue
                if m.nlink == 1 or include_hardlinked:
                    delete_candidates.append({
                        "path": m.path, "reason": f"duplicate; best={best.path}",
                        "score": m.score, "best_score": best.score, "size": m.size,
                        "lang_reason": m.lang_score_reason, "nlink": m.nlink,
                        "res": m.video_height or m.name_resolution or "",
                        "audio_langs": ",".join(m.audio_langs or m.name_lang_hints),
                        # Include arr info in delete candidates for reference
                        "arr_managed": m.arr_managed, "arr_app": m.arr_app or "",
                        "arr_instance": m.arr_instance or ""
                    })
    
    if seeding_protected > 0:
        LOG.info(f"Protected {seeding_protected} seeding files from deletion")
    if arr_protected > 0:
        LOG.info(f"Protected {arr_protected} Sonarr/Radarr managed files from deletion")

    # Season conflicts
    season_conflicts = []
    season_map: Dict[str, Dict[int, set]] = {}
    for m in media:
        p = Path(m.path)
        if not is_path_safe_for_deletion(p, delete_under):
            continue
        show = guess_show_name(p)
        if not show:
            continue
        for part in p.parts:
            if part.lower().startswith("season"):
                mm = SEASON_RE.search(part)
                if mm:
                    season_map.setdefault(show, {}).setdefault(int(mm.group(1)), set()).add(part)
                break
    for show, seasons in season_map.items():
        for sn, folders in seasons.items():
            if len(folders) > 1:
                season_conflicts.append({"show": show, "season": sn, "folders": " | ".join(sorted(folders)), "recommended": f"Season {sn:02d}"})

    # Language flags
    lang_rows = []
    for m in media:
        langs = set(l.lower() for l in (m.audio_langs or []) + (m.name_lang_hints or []))
        subs = set(l.lower() for l in (m.subtitle_langs or []) + (m.name_sub_hints or []))
        lang_rows.append({
            "path": m.path, "show": m.show or "", "season": m.season or "", "episode": m.episode or "",
            "content_type": m.content_type, "res": m.video_height or m.name_resolution or "",
            "audio_langs": ",".join(sorted(langs)), "sub_langs": ",".join(sorted(subs)),
            "is_multilang": "YES" if len(langs) > 1 else "no",
            "has_deu": "YES" if "deu" in langs or "ger" in langs else "no",
            "has_eng": "YES" if "eng" in langs else "no",
            "has_jpn": "YES" if "jpn" in langs else "no",
            "lang_score_reason": m.lang_score_reason, "note": m.note
        })

    # Hardlinks
    hl_rows = [{"dev": dev, "inode": ino, "nlink": mm.nlink, "size": mm.size, "path": mm.path}
               for (dev, ino), items in sorted(hardlinked.items()) for mm in items]

    # Missing Hardlinks Detection
    # Files that are BOTH arr_managed AND is_seeding BUT have nlink=1
    # This means the file exists twice (once in Plex library, once in torrent folder) without hardlink
    missing_hardlinks = []
    for m in media:
        if m.arr_managed and m.is_seeding and m.nlink == 1:
            missing_hardlinks.append({
                "path": m.path,
                "size": m.size,
                "ratio": m.torrent_ratio or 0.0,
                "arr_app": m.arr_app or "",
                "arr_instance": m.arr_instance or "",
                "arr_title": m.arr_title or "",
                "torrent_name": m.torrent_name or "",
                "nlink": m.nlink,
                "wasted_space": m.size,  # File exists 2x, so this space is wasted
            })

    # Also find seeding files without arr management (could be orphaned torrents)
    seeding_not_arr = []
    for m in media:
        if m.is_seeding and not m.arr_managed:
            seeding_not_arr.append({
                "path": m.path,
                "size": m.size,
                "ratio": m.torrent_ratio or 0.0,
                "torrent_name": m.torrent_name or "",
                "torrent_hash": m.torrent_hash or "",
                "nlink": m.nlink,
            })

    # All seeding files for reference
    all_seeding = []
    for m in media:
        if m.is_seeding:
            all_seeding.append({
                "path": m.path,
                "size": m.size,
                "ratio": m.torrent_ratio or 0.0,
                "torrent_name": m.torrent_name or "",
                "arr_managed": m.arr_managed,
                "arr_app": m.arr_app or "",
                "arr_title": m.arr_title or "",
                "nlink": m.nlink,
                "has_hardlink": m.nlink > 1,
            })

    missing_hardlink_size = sum(m["wasted_space"] for m in missing_hardlinks)
    LOG.info(f"Missing hardlinks: {len(missing_hardlinks)} files ({missing_hardlink_size / (1024**3):.2f} GB wasted)")
    LOG.info(f"Seeding files not in Arr: {len(seeding_not_arr)}")

    # Write CSVs
    file_rows = [flatten_file_row(m) for m in media]
    write_csv(report_run / "files.csv", file_rows, list(file_rows[0].keys()) if file_rows else ["path"])
    write_csv(report_run / "episode_duplicates.csv", episode_rows, list(episode_rows[0].keys()) if episode_rows else ["show"])
    write_csv(report_run / "delete_candidates.csv", delete_candidates, list(delete_candidates[0].keys()) if delete_candidates else ["path"])
    write_csv(report_run / "season_folder_conflicts.csv", season_conflicts, list(season_conflicts[0].keys()) if season_conflicts else ["show"])
    write_csv(report_run / "language_flags.csv", lang_rows, list(lang_rows[0].keys()) if lang_rows else ["path"])
    write_csv(report_run / "hardlinks.csv", hl_rows, list(hl_rows[0].keys()) if hl_rows else ["dev"])
    write_csv(report_run / "missing_hardlinks.csv", missing_hardlinks, list(missing_hardlinks[0].keys()) if missing_hardlinks else ["path"])
    write_csv(report_run / "all_seeding.csv", all_seeding, list(all_seeding[0].keys()) if all_seeding else ["path"])
    write_csv(report_run / "seeding_not_in_arr.csv", seeding_not_arr, list(seeding_not_arr[0].keys()) if seeding_not_arr else ["path"])

    # Summary
    ffprobe_errors = sum(1 for m in media if m.ffprobe_error)
    seeding_files = sum(1 for m in media if m.is_seeding)
    seeding_with_hardlink = sum(1 for m in media if m.is_seeding and m.nlink > 1)
    arr_managed_files = sum(1 for m in media if m.arr_managed)
    arr_upgrade_recommended = sum(1 for m in media if m.arr_upgrade_recommended)
    summary = {
        "scanned_files": len(media), "episode_duplicate_groups": len(ep_dupes),
        "dupe_files_total": len(set(i for idxs in ep_dupes.values() for i in idxs)),
        "hardlink_groups": len(hardlinked), "hardlinked_paths": sum(len(v) for v in hardlinked.values()),
        "season_folder_conflicts": len(season_conflicts), "delete_candidates_count": len(delete_candidates),
        "seeding_files_protected": seeding_protected, "seeding_files_total": seeding_files,
        "seeding_with_hardlink": seeding_with_hardlink,
        "missing_hardlinks_count": len(missing_hardlinks),
        "missing_hardlinks_wasted_gb": round(missing_hardlink_size / (1024**3), 2),
        "seeding_not_in_arr": len(seeding_not_arr),
        "arr_managed_files": arr_managed_files, "arr_protected": arr_protected,
        "arr_upgrade_recommended": arr_upgrade_recommended,
        "ffprobe_scope": ffprobe_scope, "ffprobe_found": bool(fp_bin), "ffprobe_errors": ffprobe_errors,
        "avoid_mode": avoid_mode.value, "avoid_audio_langs": avoid_langs,
        "roots": [str(r) for r in roots], "delete_under": str(delete_under),
        "report_run": str(report_run), "path_warnings": path_warnings, "version": VERSION,
        "language_detection": "Bracket patterns + Release groups",
        "language_scoring": {"films_series": "DEU+ENG > DEU > ENG", "anime": "DEU+JPN > DEU > JPN+DEU_subs > JPN > ENG"}
    }
    (report_run / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    generate_delete_script(report_run / "delete_plan.sh", delete_candidates)

    # HTML Report
    if generate_html:
        html_path = generate_html_report(report_run, summary, media, episode_rows, delete_candidates, season_conflicts, hardlinked)
        LOG.info(f"HTML report generated: {html_path}")

    return delete_candidates, summary


def execute_deletions(candidates, delete_under, include_hardlinked, verbose) -> int:
    deleted = 0
    for d in candidates:
        p = Path(d["path"])
        if not p.exists() or not is_path_safe_for_deletion(p, delete_under):
            continue
        try:
            st = p.stat()
            if st.st_nlink > 1 and not include_hardlinked:
                continue
            p.unlink()
            deleted += 1
            if verbose:
                LOG.info(f"Deleted: {p}")
        except OSError as e:
            LOG.warning(f"Delete failed {p}: {e}")
    return deleted


def parse_args():
    ap = argparse.ArgumentParser(description=f"Media Audit v{VERSION} - Find duplicates and quality issues",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    
    # Environment variable driven defaults for Unraid/Docker compatibility
    default_roots = os.environ.get("ROOTS", "/mnt/user/data/plexmedia,/mnt/user/data/torrents").split(",")
    default_roots = [r.strip() for r in default_roots if r.strip()]
    
    ap.add_argument("--roots", nargs="*", default=default_roots if default_roots else list(CFG.DEFAULT_ROOTS))
    ap.add_argument("--report-dir", default=os.environ.get("REPORT_DIR", "/mnt/user/data/media_audit_reports"))
    ap.add_argument("--max-files", type=int, default=int(os.environ.get("MAX_FILES", "0")))
    ap.add_argument("--ffprobe-scope", choices=["none", "dupes", "all"], default=os.environ.get("FFPROBE_SCOPE", "dupes"))
    ap.add_argument("--ffprobe-timeout", type=int, default=int(os.environ.get("FFPROBE_TIMEOUT", "30")))
    ap.add_argument("--ffprobe-workers", type=int, default=int(os.environ.get("FFPROBE_WORKERS", "2")))
    ap.add_argument("--avoid-audio-lang", default=os.environ.get("AVOID_AUDIO_LANG", ""))
    ap.add_argument("--avoid-mode", choices=["if-no-prefer", "strict", "report-only"], default=os.environ.get("AVOID_MODE", "if-no-prefer"))
    ap.add_argument("--content-type", choices=["auto", "anime", "series", "movie"], default=os.environ.get("CONTENT_TYPE", "auto"))
    ap.add_argument("--apply", action="store_true", default=os.environ.get("APPLY", "").lower() == "true")
    ap.add_argument("--yes", action="store_true", default=os.environ.get("YES", "").lower() == "true")
    ap.add_argument("--delete-under", default=os.environ.get("DELETE_UNDER", "/mnt/user/data/plexmedia"))
    ap.add_argument("--include-hardlinked", action="store_true")
    ap.add_argument("--html-report", action="store_true", default=True, help="Generate HTML report (default: enabled)")
    ap.add_argument("--no-html-report", action="store_false", dest="html_report")
    # qBittorrent integration - Safe defaults (no hardcoded credentials)
    ap.add_argument("--qbit-host", default=os.environ.get("QBIT_HOST", "localhost"), help="qBittorrent host")
    ap.add_argument("--qbit-port", type=int, default=int(os.environ.get("QBIT_PORT", "8080")), help="qBittorrent WebUI port")
    ap.add_argument("--qbit-user", default=os.environ.get("QBIT_USER", ""), help="qBittorrent username")
    ap.add_argument("--qbit-pass", default=os.environ.get("QBIT_PASS", ""), help="qBittorrent password")
    ap.add_argument("--qbit-path-map", action="append", default=[], metavar="CONTAINER:HOST",
                    help="Map qBittorrent container path to host path. Default: /data/torrents:/mnt/user/data/torrents")
    ap.add_argument("--protect-seeding", action="store_true", default=True, 
                    help="Protect files being seeded (default: enabled)")
    ap.add_argument("--no-protect-seeding", action="store_false", dest="protect_seeding")
    ap.add_argument("--no-qbit", action="store_true", help="Disable qBittorrent check")
    ap.add_argument("--verbose", "-v", action="store_true")
    
    # Sonarr/Radarr integration
    ap.add_argument("--sonarr", action="append", default=[], metavar="CONFIG",
                    help="Add Sonarr instance: name=NAME,url=URL,apikey=KEY,path_map=REMOTE:LOCAL")
    ap.add_argument("--radarr", action="append", default=[], metavar="CONFIG",
                    help="Add Radarr instance: name=NAME,url=URL,apikey=KEY,path_map=REMOTE:LOCAL")
    ap.add_argument("--sonarr-config", default=os.environ.get("SONARR_CONFIG", ""),
                    help="Path to JSON file with Sonarr instance configs")
    ap.add_argument("--radarr-config", default=os.environ.get("RADARR_CONFIG", ""),
                    help="Path to JSON file with Radarr instance configs")
    ap.add_argument("--no-servarr", action="store_true", 
                    default=os.environ.get("NO_SERVARR", "").lower() == "true",
                    help="Disable Sonarr/Radarr integration entirely")
    ap.add_argument("--protect-arr-managed", action="store_true", default=True,
                    help="Protect files managed by Sonarr/Radarr (default: enabled)")
    ap.add_argument("--no-protect-arr-managed", action="store_false", dest="protect_arr_managed")
    ap.add_argument("--arr-rescan", action="store_true", default=False,
                    help="Trigger Sonarr/Radarr rescan after deletion (requires --apply --yes)")
    
    return ap.parse_args()


def main() -> int:
    global LOG
    args = parse_args()
    
    # Setup centralized logging (file + console)
    log_dir = os.environ.get("CONFIG_DIR", "/config") + "/logs"
    LOG = setup_logging(debug=args.verbose, log_dir=log_dir)
    
    LOG.info("=" * 60)
    LOG.info(f"media_audit.py v{VERSION} - Starting audit")
    LOG.info("=" * 60)

    ctype = ContentType(args.content_type)
    avoid_mode = AvoidMode(args.avoid_mode)
    roots = [Path(r).resolve() for r in args.roots]

    for r in roots:
        if not r.exists():
            LOG.error(f"Root not found: {r}")
            return 2

    path_warnings = check_unraid_path_consistency(roots)
    for w in path_warnings:
        LOG.warning(w)

    delete_under = Path(args.delete_under).resolve()
    report_base = Path(args.report_dir).resolve()
    report_run = report_base / f"run-{now_stamp()}"
    report_run.mkdir(parents=True, exist_ok=True)

    cache = CacheManager(report_base / "cache.json")
    avoid_langs = validate_lang_codes([x.strip() for x in args.avoid_audio_lang.split(",") if x.strip()])

    # Check ffprobe
    is_working, fp_bin, fp_error, missing_libs = check_ffprobe_dependencies()
    if args.ffprobe_scope != "none":
        if not is_working:
            print_ffprobe_fix_instructions(missing_libs, fp_error or "Unknown error")
            LOG.warning("Falling back to filename-only analysis.")
            ffprobe_scope = "none"
        else:
            ffprobe_scope = args.ffprobe_scope
            LOG.info(f"ffprobe available: {fp_bin}")
    else:
        ffprobe_scope = "none"

    # Scan
    LOG.info(f"Scanning: {[str(r) for r in roots]}")
    media = scan_media_files(roots, args.max_files, ctype, avoid_mode, avoid_langs)
    LOG.info(f"Scanned {len(media)} files")
    
    # Check qBittorrent for seeding files
    # Strategy: 
    # 1. Get all torrent file paths from qBittorrent
    # 2. Since we already scan /mnt/user/data/torrents, match via hardlinks we already found
    torrent_path_map: Dict[str, dict] = {}
    torrent_inode_map: Dict[Tuple[int, int], dict] = {}
    torrent_filename_map: Dict[str, dict] = {}
    seeding_count = 0
    
    if args.protect_seeding and not args.no_qbit:
        LOG.info(f"Connecting to qBittorrent at {args.qbit_host}:{args.qbit_port}")
        
        # Parse path mappings - add default if none specified
        path_mappings = {}
        qbit_path_maps = args.qbit_path_map if args.qbit_path_map else ["/data/torrents:/mnt/user/data/torrents"]
        for mapping in qbit_path_maps:
            if ':' in mapping:
                parts = mapping.split(':', 1)
                path_mappings[parts[0]] = parts[1]
                LOG.info(f"Path mapping: {parts[0]} -> {parts[1]}")
        
        torrent_path_map, torrent_inode_map, torrent_filename_map = get_seeding_files_with_inodes(
            args.qbit_host, args.qbit_port, args.qbit_user, args.qbit_pass, path_mappings
        )
        
        if torrent_path_map:
            matched_by_inode = 0
            matched_by_path = 0
            matched_by_hardlink = 0
            
            # Build a map of inodes from our scanned media (including torrent folder!)
            # This lets us find hardlinks between plex and torrent folders
            media_by_inode: Dict[Tuple[int, int], List[MediaFile]] = {}
            for m in media:
                key = (m.dev, m.inode)
                media_by_inode.setdefault(key, []).append(m)
            
            # First pass: find which of OUR scanned files are in torrents
            for m in media:
                if "/sample" in m.path.lower() or "sample." in m.path.lower():
                    continue
                
                torrent_info = None
                
                # 1. Direct path match (for files in torrent folder)
                if m.path in torrent_path_map:
                    torrent_info = torrent_path_map[m.path]
                    matched_by_path += 1
                
                # 2. Try inode match
                if not torrent_info:
                    inode_key = (m.dev, m.inode)
                    if inode_key in torrent_inode_map:
                        torrent_info = torrent_inode_map[inode_key]
                        matched_by_inode += 1
                
                if torrent_info:
                    # Only protect files from ACTIVE torrents (seeding or downloading)
                    is_active = torrent_info.get("is_active", False)
                    state = torrent_info.get("state", "unknown")
                    
                    if is_active:
                        # This file is in an active torrent - protect it!
                        m.is_seeding = True
                        m.torrent_hash = torrent_info.get("torrent_hash")
                        m.torrent_name = torrent_info.get("torrent_name")
                        m.torrent_file_name = torrent_info.get("torrent_file_name")
                        m.torrent_ratio = torrent_info.get("ratio")
                        seeding_count += 1

                        LOG.debug(f"Protected (active torrent {state}): {m.path}")

                        # Also mark all hardlinks to this file!
                        inode_key = (m.dev, m.inode)
                        if inode_key in media_by_inode:
                            for linked in media_by_inode[inode_key]:
                                if not linked.is_seeding:
                                    linked.is_seeding = True
                                    linked.torrent_hash = torrent_info.get("torrent_hash")
                                    linked.torrent_name = torrent_info.get("torrent_name")
                                    linked.torrent_file_name = torrent_info.get("torrent_file_name")
                                    linked.torrent_ratio = torrent_info.get("ratio")
                                    matched_by_hardlink += 1
                                    seeding_count += 1
                                    LOG.debug(f"Protected (hardlink to active torrent): {linked.path}")
                    else:
                        LOG.debug(f"Matched but not protected (state={state}): {m.path}")
            
            LOG.info(f"Matched {seeding_count} files to ACTIVE torrents (path:{matched_by_path}, inode:{matched_by_inode}, hardlink:{matched_by_hardlink})")
        else:
            LOG.warning("No torrent files found or qBittorrent connection failed")

    # ==========================================================================
    # Sonarr/Radarr Integration
    # ==========================================================================
    servarr_manager = None
    arr_protected_count = 0
    
    if not args.no_servarr:
        # Try to import servarr_client (optional module)
        try:
            from servarr_client import (
                ServarrManager, ServarrInstance, ServarrType,
                parse_instances_from_env, parse_instance_from_cli_arg,
            )
            
            servarr_manager = ServarrManager()
            
            # Load from environment variables
            sonarr_instances = parse_instances_from_env(ServarrType.SONARR)
            radarr_instances = parse_instances_from_env(ServarrType.RADARR)
            
            for inst in sonarr_instances:
                if servarr_manager.add_instance(inst):
                    LOG.info(f"Connected to Sonarr instance: {inst.name} ({inst.url})")
                else:
                    LOG.warning(f"Failed to connect to Sonarr: {inst.name} - {inst.last_error}")
            
            for inst in radarr_instances:
                if servarr_manager.add_instance(inst):
                    LOG.info(f"Connected to Radarr instance: {inst.name} ({inst.url})")
                else:
                    LOG.warning(f"Failed to connect to Radarr: {inst.name} - {inst.last_error}")
            
            # Load from CLI arguments
            for sonarr_arg in args.sonarr:
                inst = parse_instance_from_cli_arg(sonarr_arg, ServarrType.SONARR)
                if inst:
                    if servarr_manager.add_instance(inst):
                        LOG.info(f"Connected to Sonarr (CLI): {inst.name}")
                    else:
                        LOG.warning(f"Failed to connect to Sonarr (CLI): {inst.name}")
            
            for radarr_arg in args.radarr:
                inst = parse_instance_from_cli_arg(radarr_arg, ServarrType.RADARR)
                if inst:
                    if servarr_manager.add_instance(inst):
                        LOG.info(f"Connected to Radarr (CLI): {inst.name}")
                    else:
                        LOG.warning(f"Failed to connect to Radarr (CLI): {inst.name}")
            
            # Load all managed files
            if servarr_manager.instances:
                LOG.info("Loading managed files from Sonarr/Radarr instances...")
                managed_count = servarr_manager.load_all_files()
                LOG.info(f"Total managed files loaded: {managed_count}")
                
                # Match scanned media to Servarr managed files
                for m in media:
                    info = servarr_manager.get_file_info(m.path)
                    if info:
                        mf, instance = info
                        m.arr_managed = True
                        m.arr_app = instance.app_type.value
                        m.arr_instance = instance.name
                        m.arr_media_id = mf.media_id
                        m.arr_file_id = mf.file_id
                        m.arr_title = mf.media_title
                        m.arr_quality = mf.quality
                        m.arr_custom_format_score = mf.custom_format_score
                        m.arr_custom_formats = ",".join(mf.custom_formats) if mf.custom_formats else None
                        m.arr_quality_profile = mf.quality_profile_name
                        m.arr_cutoff_not_met = mf.quality_cutoff_not_met
                        m.arr_upgrade_recommended = mf.upgrade_recommended
                        m.arr_upgrade_reason = mf.upgrade_reason
                        m.arr_link = mf.webui_link
                        arr_protected_count += 1
                    
                    # Check if file is in queue
                    if servarr_manager.is_in_queue(m.path):
                        m.arr_in_queue = True
                
                LOG.info(f"Matched {arr_protected_count} files to Sonarr/Radarr managed files")
                LOG.info(f"Files in download queue: {len(servarr_manager.queue_evidence)}")
            else:
                LOG.info("No Sonarr/Radarr instances configured")
                servarr_manager = None
        
        except ImportError as e:
            LOG.warning(f"Servarr integration not available: {e}")
            import traceback
            traceback.print_exc()
            servarr_manager = None
        except Exception as e:
            LOG.warning(f"Servarr integration error: {e}")
            import traceback
            traceback.print_exc()
            servarr_manager = None

    # Group episodes
    ep_groups: Dict[Tuple[str, int, int], List[int]] = {}
    for idx, m in enumerate(media):
        if m.show and m.season is not None and m.episode is not None:
            ep_groups.setdefault((m.show, m.season, m.episode), []).append(idx)

    dupe_indices = sorted(set(i for idxs in ep_groups.values() if len(idxs) > 1 for i in idxs))

    # ffprobe enrichment
    if ffprobe_scope == "all":
        indices = list(range(len(media)))
    elif ffprobe_scope == "dupes":
        indices = dupe_indices
    else:
        indices = []

    if indices and fp_bin and is_working:
        LOG.info(f"Running ffprobe on {len(indices)} files...")
        enrich_media_with_ffprobe(media, indices, cache, args.ffprobe_timeout, args.ffprobe_workers, ctype, avoid_mode, avoid_langs)
        cache.save()

    # Hardlinks
    hl_groups: Dict[Tuple[int, int], List[MediaFile]] = {}
    for m in media:
        hl_groups.setdefault((m.dev, m.inode), []).append(m)
    hardlinked = {k: v for k, v in hl_groups.items() if len(v) > 1}

    # Reports
    LOG.info("Generating reports...")
    candidates, summary = generate_reports(
        media, ep_groups, hardlinked, delete_under, avoid_mode, avoid_langs,
        args.include_hardlinked, report_run, ffprobe_scope, fp_bin, roots, path_warnings,
        args.html_report
    )

    print(json.dumps(summary, indent=2))
    print(f"\n📊 Reports saved to: {report_run}")
    if args.html_report:
        print(f"🌐 HTML Report: {report_run}/report.html")

    # Deletions
    if args.apply:
        if not args.yes:
            LOG.error("--apply requires --yes")
            return 3
        if str(delete_under.resolve()) in CFG.PROTECTED_PATHS:
            LOG.error(f"Protected path: {delete_under}")
            return 4
        deleted = execute_deletions(candidates, delete_under, args.include_hardlinked, args.verbose)
        LOG.info(f"Deleted {deleted} files")
        print(f"DONE: deleted={deleted}")
        
        # Trigger Arr rescans if requested
        if args.arr_rescan and servarr_manager and deleted > 0:
            LOG.info("Triggering Sonarr/Radarr rescans...")
            rescan_ids = set()
            for d in candidates:
                info = servarr_manager.get_file_info(d.get("path", ""))
                if info:
                    mf, instance = info
                    rescan_ids.add((instance.name, mf.media_id, instance.app_type.value))
            
            for instance_name, media_id, app_type in rescan_ids:
                client = servarr_manager.clients.get(instance_name)
                if client:
                    if app_type == "sonarr":
                        if client.rescan_series(media_id):
                            LOG.info(f"Triggered rescan for series {media_id} on {instance_name}")
                    else:
                        if client.rescan_movie(media_id):
                            LOG.info(f"Triggered rescan for movie {media_id} on {instance_name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
