#!/usr/bin/env python3
"""
servarr_client.py — Enhanced Sonarr/Radarr API Client for Media Audit

Provides robust integration with Sonarr and Radarr instances:
- Fetch managed files (episodeFile, movieFile) with paths
- Queue monitoring for in-progress downloads
- Protection with detailed evidence/reasoning
- Graceful degradation on API failures
- Caching to minimize API calls
- Support for multiple instances

Version: 2.0.0

API Reference Documentation:
- Sonarr API v3/v4: https://sonarr.tv/docs/api/
  - GET /api/v3/episodefile?seriesId={id} - Episode files for a series
  - GET /api/v3/series - All series
  - GET /api/v3/queue - Download queue
  - GET /api/v3/qualityprofile - Quality profiles
  - GET /api/v3/system/status - System status/version
  
- Radarr API v3: https://radarr.video/docs/api/
  - GET /api/v3/moviefile?movieId={id} - Movie file
  - GET /api/v3/movie - All movies
  - GET /api/v3/queue - Download queue
  - GET /api/v3/qualityprofile - Quality profiles
  - GET /api/v3/system/status - System status/version

- Python libraries: devopsarr/sonarr-py, devopsarr/radarr-py, pyarr
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import time
import urllib.request
import urllib.parse
import urllib.error
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from datetime import datetime

LOG = logging.getLogger("servarr_client")


# =============================================================================
# ENUMS AND CONSTANTS
# =============================================================================

class ServarrType(Enum):
    """Type of Servarr application."""
    SONARR = "sonarr"
    RADARR = "radarr"


class ProtectionReason(Enum):
    """Why a file is protected from deletion."""
    MANAGED_EPISODE = "managed_by_sonarr"
    MANAGED_MOVIE = "managed_by_radarr"
    IN_QUEUE = "in_download_queue"
    SEEDING = "seeding_in_qbittorrent"
    UNKNOWN = "protection_check_failed"


class ConnectionStatus(Enum):
    """Connection status for an instance."""
    CONNECTED = "connected"
    FAILED = "failed"
    NOT_TESTED = "not_tested"
    AUTH_ERROR = "auth_error"
    TIMEOUT = "timeout"


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class PathMapping:
    """Maps paths between Servarr container and local host."""
    servarr_path: str
    local_path: str
    
    def to_local(self, path: str) -> str:
        if path.startswith(self.servarr_path):
            return path.replace(self.servarr_path, self.local_path, 1)
        return path
    
    def to_servarr(self, path: str) -> str:
        if path.startswith(self.local_path):
            return path.replace(self.local_path, self.servarr_path, 1)
        return path


@dataclass
class QualityProfile:
    """Quality profile configuration from Servarr."""
    id: int
    name: str
    upgrade_allowed: bool = True
    cutoff: int = 0
    cutoff_format_score: int = 0
    min_format_score: int = 0
    min_upgrade_format_score: int = 0
    
    @classmethod
    def from_dict(cls, data: dict) -> "QualityProfile":
        return cls(
            id=data.get("id", 0),
            name=data.get("name", "Unknown"),
            upgrade_allowed=data.get("upgradeAllowed", True),
            cutoff=data.get("cutoff", 0),
            cutoff_format_score=data.get("cutoffFormatScore", 0),
            min_format_score=data.get("minFormatScore", 0),
            min_upgrade_format_score=data.get("minUpgradeFormatScore", 0),
        )


@dataclass
class ProtectionEvidence:
    """Detailed evidence for why a file is protected."""
    reason: ProtectionReason
    instance_name: str = ""
    instance_type: str = ""
    file_id: Optional[int] = None
    media_id: Optional[int] = None
    media_title: str = ""
    quality: str = ""
    quality_profile: str = ""
    custom_formats: List[str] = field(default_factory=list)
    custom_format_score: int = 0
    webui_link: str = ""
    queue_status: str = ""
    queue_title: str = ""
    torrent_name: str = ""
    torrent_hash: str = ""
    torrent_state: str = ""
    torrent_ratio: float = 0.0
    torrent_category: str = ""
    torrent_tags: List[str] = field(default_factory=list)
    qbit_webui_link: str = ""
    error_message: str = ""
    
    def to_dict(self) -> dict:
        return {k: (v.value if isinstance(v, Enum) else v) 
                for k, v in asdict(self).items() 
                if v and (not isinstance(v, list) or v)}
    
    def get_summary(self) -> str:
        if self.reason == ProtectionReason.MANAGED_EPISODE:
            return f"Sonarr/{self.instance_name}: {self.media_title} ({self.quality})"
        elif self.reason == ProtectionReason.MANAGED_MOVIE:
            return f"Radarr/{self.instance_name}: {self.media_title} ({self.quality})"
        elif self.reason == ProtectionReason.IN_QUEUE:
            return f"Queue/{self.instance_name}: {self.queue_title} ({self.queue_status})"
        elif self.reason == ProtectionReason.SEEDING:
            return f"qBit: {self.torrent_name} (ratio: {self.torrent_ratio:.2f})"
        elif self.reason == ProtectionReason.UNKNOWN:
            return f"Check failed: {self.error_message}"
        return str(self.reason.value)


@dataclass 
class ManagedFile:
    """A file managed by Sonarr/Radarr with full metadata."""
    file_id: int
    path: str
    local_path: str
    size: int
    quality: str
    quality_id: int
    custom_format_score: int = 0
    custom_formats: List[str] = field(default_factory=list)
    quality_cutoff_not_met: bool = False
    media_id: int = 0
    media_title: str = ""
    season_number: Optional[int] = None
    episode_numbers: List[int] = field(default_factory=list)
    quality_profile_id: int = 0
    quality_profile_name: str = ""
    upgrade_recommended: bool = False
    upgrade_reason: str = ""
    webui_link: str = ""
    instance_name: str = ""
    instance_type: str = ""


@dataclass
class ServarrInstance:
    """Configuration for a Sonarr/Radarr instance."""
    name: str
    url: str
    api_key: str
    app_type: ServarrType
    path_mappings: List[PathMapping] = field(default_factory=list)
    root_folders: List[str] = field(default_factory=list)
    webui_url: str = ""
    enabled: bool = True
    timeout: int = 30
    retries: int = 2
    version: str = ""
    is_v4: bool = False
    quality_profiles: Dict[int, QualityProfile] = field(default_factory=dict)
    connection_status: ConnectionStatus = ConnectionStatus.NOT_TESTED
    last_error: str = ""
    
    def __post_init__(self):
        self.url = self.url.rstrip("/")
        if not self.url.startswith("http"):
            self.url = f"http://{self.url}"
        if not self.webui_url:
            self.webui_url = self.url
    
    @classmethod
    def from_dict(cls, data: dict, app_type: Optional[ServarrType] = None) -> "ServarrInstance":
        if app_type is None:
            app_type = ServarrType(data.get("type", data.get("app_type", "sonarr")).lower())
        
        path_mappings = []
        for pm in data.get("path_mappings", []):
            if isinstance(pm, dict):
                path_mappings.append(PathMapping(
                    servarr_path=pm.get("servarr_path", pm.get("remote", "")),
                    local_path=pm.get("local_path", pm.get("local", ""))
                ))
            elif isinstance(pm, str) and ":" in pm:
                parts = pm.split(":", 1)
                path_mappings.append(PathMapping(parts[0], parts[1]))
        
        return cls(
            name=data.get("name", "default"),
            url=data.get("url", ""),
            api_key=data.get("api_key", data.get("apikey", "")),
            app_type=app_type,
            path_mappings=path_mappings,
            root_folders=data.get("root_folders", []),
            webui_url=data.get("webui_url", ""),
            enabled=data.get("enabled", True),
            timeout=data.get("timeout", 30),
            retries=data.get("retries", 2),
        )
    
    def map_path_to_local(self, path: str) -> str:
        for pm in self.path_mappings:
            mapped = pm.to_local(path)
            if mapped != path:
                return mapped
        return path
    
    def map_path_to_servarr(self, path: str) -> str:
        for pm in self.path_mappings:
            mapped = pm.to_servarr(path)
            if mapped != path:
                return mapped
        return path
    
    def get_webui_link(self, media_id: int) -> str:
        base = self.webui_url or self.url
        if self.app_type == ServarrType.SONARR:
            return f"{base}/series/{media_id}"
        else:
            return f"{base}/movie/{media_id}"
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "url": self.url,
            "api_key": self.api_key,
            "app_type": self.app_type.value,
            "enabled": self.enabled,
            "path_mappings": [{"servarr_path": pm.servarr_path, "local_path": pm.local_path} 
                             for pm in self.path_mappings],
            "webui_url": self.webui_url,
        }


@dataclass
class InstanceStatus:
    """Status report for a Servarr instance."""
    name: str
    app_type: str
    url: str
    status: str
    version: str = ""
    error: str = ""
    managed_files_count: int = 0
    queue_items_count: int = 0
    quality_profiles_count: int = 0


# =============================================================================
# SERVARR CLIENT
# =============================================================================

class ServarrClient:
    """HTTP client for Sonarr/Radarr API with error handling and retries."""
    
    def __init__(self, instance: ServarrInstance):
        self.instance = instance
        self._connected = False
        self._cache: Dict[str, Any] = {}
        self._cache_time: Dict[str, float] = {}
        self._cache_ttl = 300
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE
    
    def _get_cached(self, key: str) -> Optional[Any]:
        if key in self._cache:
            if time.time() - self._cache_time.get(key, 0) < self._cache_ttl:
                return self._cache[key]
        return None
    
    def _set_cached(self, key: str, value: Any):
        self._cache[key] = value
        self._cache_time[key] = time.time()
    
    def _request(self, endpoint: str, method: str = "GET", 
                 data: Optional[dict] = None, use_cache: bool = True) -> Optional[Any]:
        cache_key = f"{method}:{endpoint}"
        if use_cache and method == "GET":
            cached = self._get_cached(cache_key)
            if cached is not None:
                return cached
        
        url = f"{self.instance.url}/api/v3/{endpoint}"
        headers = {
            "X-Api-Key": self.instance.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        
        last_error = None
        for attempt in range(self.instance.retries + 1):
            try:
                if data:
                    req = urllib.request.Request(
                        url, data=json.dumps(data).encode("utf-8"),
                        headers=headers, method=method
                    )
                else:
                    req = urllib.request.Request(url, headers=headers, method=method)
                
                ctx = self._ssl_ctx if url.startswith("https") else None
                
                with urllib.request.urlopen(req, timeout=self.instance.timeout, context=ctx) as response:
                    content = response.read()
                    if content:
                        result = json.loads(content)
                        if use_cache and method == "GET":
                            self._set_cached(cache_key, result)
                        return result
                    return None
                    
            except urllib.error.HTTPError as e:
                last_error = f"HTTP {e.code}: {e.reason}"
                if e.code == 401:
                    self.instance.connection_status = ConnectionStatus.AUTH_ERROR
                    self.instance.last_error = "Invalid API key"
                    return None
                elif e.code == 404:
                    return None
                    
            except urllib.error.URLError as e:
                last_error = f"Connection failed: {e.reason}"
                if "timed out" in str(e.reason).lower():
                    self.instance.connection_status = ConnectionStatus.TIMEOUT
                    
            except json.JSONDecodeError as e:
                last_error = f"Invalid JSON: {e}"
                
            except Exception as e:
                last_error = str(e)
            
            if attempt < self.instance.retries:
                time.sleep(1 * (attempt + 1))
        
        self.instance.last_error = last_error or "Unknown error"
        return None
    
    def connect(self) -> bool:
        if not self.instance.enabled:
            self.instance.connection_status = ConnectionStatus.FAILED
            self.instance.last_error = "Instance disabled"
            return False
        
        status = self._request("system/status", use_cache=False)
        if not status:
            self.instance.connection_status = ConnectionStatus.FAILED
            LOG.error(f"[{self.instance.name}] Failed to connect to {self.instance.url}")
            return False
        
        self.instance.version = status.get("version", "unknown")
        version_major = int(self.instance.version.split(".")[0]) if self.instance.version else 0
        self.instance.is_v4 = version_major >= 4
        self.instance.connection_status = ConnectionStatus.CONNECTED
        self.instance.last_error = ""
        
        LOG.info(f"[{self.instance.name}] Connected: {self.instance.app_type.value} v{self.instance.version}")
        self._connected = True
        self._load_quality_profiles()
        return True
    
    def _load_quality_profiles(self):
        profiles = self._request("qualityprofile")
        if profiles:
            self.instance.quality_profiles.clear()
            for p in profiles:
                qp = QualityProfile.from_dict(p)
                self.instance.quality_profiles[qp.id] = qp
    
    def get_status(self) -> InstanceStatus:
        return InstanceStatus(
            name=self.instance.name,
            app_type=self.instance.app_type.value,
            url=self.instance.url,
            status=self.instance.connection_status.value,
            version=self.instance.version,
            error=self.instance.last_error,
            quality_profiles_count=len(self.instance.quality_profiles),
        )
    
    def get_all_series(self) -> List[dict]:
        if self.instance.app_type != ServarrType.SONARR:
            return []
        return self._request("series") or []
    
    def get_all_movies(self) -> List[dict]:
        if self.instance.app_type != ServarrType.RADARR:
            return []
        return self._request("movie") or []
    
    def get_episode_files(self, series_id: int) -> List[dict]:
        if self.instance.app_type != ServarrType.SONARR:
            return []
        return self._request(f"episodefile?seriesId={series_id}") or []
    
    def get_movie_file(self, movie_id: int) -> Optional[dict]:
        if self.instance.app_type != ServarrType.RADARR:
            return None
        files = self._request(f"moviefile?movieId={movie_id}")
        if files and len(files) > 0:
            return files[0]
        return None
    
    def get_queue(self) -> List[dict]:
        all_records = []
        page = 1
        while True:
            queue = self._request(f"queue?page={page}&pageSize=100", use_cache=False)
            if not queue or "records" not in queue:
                break
            records = queue["records"]
            all_records.extend(records)
            if len(all_records) >= queue.get("totalRecords", 0):
                break
            page += 1
            if page > 20:
                break
        return all_records
    
    def get_all_managed_files(self) -> Dict[str, ManagedFile]:
        if not self._connected and not self.connect():
            return {}
        
        files: Dict[str, ManagedFile] = {}
        try:
            if self.instance.app_type == ServarrType.SONARR:
                files = self._get_sonarr_files()
            else:
                files = self._get_radarr_files()
            LOG.info(f"[{self.instance.name}] Found {len(files)} managed files")
        except Exception as e:
            LOG.error(f"[{self.instance.name}] Failed to get managed files: {e}")
            self.instance.last_error = str(e)
        return files
    
    def _get_sonarr_files(self) -> Dict[str, ManagedFile]:
        """Get all episode files from Sonarr."""
        files: Dict[str, ManagedFile] = {}
        series_list = self.get_all_series()
        
        LOG.info(f"[{self.instance.name}] Processing {len(series_list)} series from Sonarr")
        episode_file_count = 0
        skipped_root_folder = 0
        
        for series in series_list:
            series_id = series.get("id", 0)
            series_title = series.get("title", "Unknown")
            qp_id = series.get("qualityProfileId", 0)
            qp = self.instance.quality_profiles.get(qp_id, QualityProfile(id=0, name="Unknown"))
            
            series_path = series.get("path", "")
            
            # Filter by root folders if configured
            if self.instance.root_folders:
                if not any(series_path.startswith(rf) for rf in self.instance.root_folders):
                    skipped_root_folder += 1
                    continue
            
            episode_files = self.get_episode_files(series_id)
            for ef in episode_files:
                path = ef.get("path", "")
                if not path:
                    continue
                
                episode_file_count += 1
                
                # Map Sonarr path to local path
                local_path = self.instance.map_path_to_local(path)
                
                # Log first few path mappings for debugging
                if episode_file_count <= 5:
                    LOG.debug(f"[{self.instance.name}] Sonarr path: {path}")
                    LOG.debug(f"[{self.instance.name}] Local path:  {local_path}")
                    if path == local_path:
                        LOG.debug(f"[{self.instance.name}] ⚠️ No path mapping applied!")
                
                quality_obj = ef.get("quality", {}).get("quality", {})
                custom_formats = [cf.get("name", "") for cf in ef.get("customFormats", []) if cf.get("name")]
                cutoff_not_met = ef.get("qualityCutoffNotMet", False)
                cf_score = ef.get("customFormatScore", 0)
                
                upgrade_recommended = False
                upgrade_reason = ""
                if cutoff_not_met:
                    upgrade_recommended = True
                    upgrade_reason = "Quality cutoff not met"
                
                mf = ManagedFile(
                    file_id=ef.get("id", 0), path=path, local_path=local_path,
                    size=ef.get("size", 0), quality=quality_obj.get("name", "Unknown"),
                    quality_id=quality_obj.get("id", 0), custom_format_score=cf_score,
                    custom_formats=custom_formats, quality_cutoff_not_met=cutoff_not_met,
                    media_id=series_id, media_title=series_title,
                    season_number=ef.get("seasonNumber"), quality_profile_id=qp_id,
                    quality_profile_name=qp.name, upgrade_recommended=upgrade_recommended,
                    upgrade_reason=upgrade_reason,
                    webui_link=self.instance.get_webui_link(series_id),
                    instance_name=self.instance.name, instance_type=self.instance.app_type.value,
                )
                files[local_path] = mf
        
        LOG.info(f"[{self.instance.name}] Loaded {len(files)} episode files from {len(series_list) - skipped_root_folder} series")
        if skipped_root_folder > 0:
            LOG.debug(f"[{self.instance.name}] Skipped {skipped_root_folder} series (not in root folders)")
        
        return files
    
    def _get_radarr_files(self) -> Dict[str, ManagedFile]:
        files: Dict[str, ManagedFile] = {}
        movie_list = self.get_all_movies()
        
        for movie in movie_list:
            if not movie.get("hasFile", False):
                continue
            
            movie_id = movie.get("id", 0)
            movie_title = movie.get("title", "Unknown")
            qp_id = movie.get("qualityProfileId", 0)
            qp = self.instance.quality_profiles.get(qp_id, QualityProfile(id=0, name="Unknown"))
            
            movie_path = movie.get("path", "")
            if self.instance.root_folders:
                if not any(movie_path.startswith(rf) for rf in self.instance.root_folders):
                    continue
            
            mf_data = movie.get("movieFile") or self.get_movie_file(movie_id)
            if not mf_data:
                continue
            
            path = mf_data.get("path", "")
            if not path:
                continue
            
            local_path = self.instance.map_path_to_local(path)
            quality_obj = mf_data.get("quality", {}).get("quality", {})
            custom_formats = [cf.get("name", "") for cf in mf_data.get("customFormats", []) if cf.get("name")]
            cf_score = mf_data.get("customFormatScore", 0)
            cutoff_not_met = mf_data.get("qualityCutoffNotMet", False)
            
            upgrade_recommended = cutoff_not_met
            upgrade_reason = "Quality cutoff not met" if cutoff_not_met else ""
            
            mf = ManagedFile(
                file_id=mf_data.get("id", 0), path=path, local_path=local_path,
                size=mf_data.get("size", 0), quality=quality_obj.get("name", "Unknown"),
                quality_id=quality_obj.get("id", 0), custom_format_score=cf_score,
                custom_formats=custom_formats, quality_cutoff_not_met=cutoff_not_met,
                media_id=movie_id, media_title=movie_title,
                quality_profile_id=qp_id, quality_profile_name=qp.name,
                upgrade_recommended=upgrade_recommended, upgrade_reason=upgrade_reason,
                webui_link=self.instance.get_webui_link(movie_id),
                instance_name=self.instance.name, instance_type=self.instance.app_type.value,
            )
            files[local_path] = mf
        return files
    
    def get_queue_paths(self) -> Dict[str, ProtectionEvidence]:
        paths: Dict[str, ProtectionEvidence] = {}
        for item in self.get_queue():
            output_path = item.get("outputPath", "")
            if output_path:
                local_path = self.instance.map_path_to_local(output_path)
                paths[local_path] = ProtectionEvidence(
                    reason=ProtectionReason.IN_QUEUE,
                    instance_name=self.instance.name,
                    instance_type=self.instance.app_type.value,
                    queue_status=item.get("status", "unknown"),
                    queue_title=item.get("title", ""),
                )
        return paths


# =============================================================================
# SERVARR MANAGER
# =============================================================================

class ServarrManager:
    """Manages multiple Sonarr/Radarr instances with unified protection checking."""
    
    def __init__(self):
        self.instances: List[ServarrInstance] = []
        self.clients: Dict[str, ServarrClient] = {}
        self.managed_files: Dict[str, Tuple[ManagedFile, ServarrInstance]] = {}
        self.queue_evidence: Dict[str, ProtectionEvidence] = {}
        self.load_timestamp: Optional[datetime] = None
        self.instance_stats: Dict[str, dict] = {}
    
    def add_instance(self, instance: ServarrInstance) -> bool:
        if not instance.enabled:
            LOG.info(f"[{instance.name}] Skipping disabled instance")
            return False
        
        client = ServarrClient(instance)
        success = client.connect()
        
        self.instances.append(instance)
        self.clients[instance.name] = client
        self.instance_stats[instance.name] = {
            "connected": success, "error": instance.last_error if not success else "",
            "files_count": 0, "queue_count": 0,
        }
        return success
    
    def add_instance_from_config(self, config: dict, app_type: Optional[ServarrType] = None) -> bool:
        try:
            if not config.get("url") or not config.get("api_key"):
                return False
            instance = ServarrInstance.from_dict(config, app_type)
            return self.add_instance(instance)
        except Exception as e:
            LOG.error(f"Failed to add instance from config: {e}")
            return False
    
    def load_all_files(self) -> int:
        """Load all managed files and queue items from all connected instances."""
        self.managed_files.clear()
        self.queue_evidence.clear()
        
        LOG.info(f"Loading files from {len(self.clients)} Sonarr/Radarr instances...")
        
        for name, client in self.clients.items():
            instance = client.instance
            LOG.info(f"[{name}] Processing {instance.app_type.value} instance: {instance.url}")
            
            if instance.connection_status != ConnectionStatus.CONNECTED:
                LOG.info(f"[{name}] Attempting connection...")
                if not client.connect():
                    LOG.warning(f"[{name}] Connection failed: {instance.last_error}")
                    continue
                LOG.info(f"[{name}] Connected successfully")
            
            try:
                LOG.info(f"[{name}] Loading managed files...")
                files = client.get_all_managed_files()
                LOG.info(f"[{name}] Found {len(files)} managed files")
                
                # Log first few paths for debugging
                sample_paths = list(files.keys())[:3]
                for p in sample_paths:
                    LOG.debug(f"[{name}] Sample file: {p}")
                
                for path, mf in files.items():
                    self.managed_files[path] = (mf, instance)
                self.instance_stats[name]["files_count"] = len(files)
            except Exception as e:
                LOG.error(f"[{name}] Error loading files: {e}")
                import traceback
                LOG.debug(traceback.format_exc())
            
            try:
                LOG.info(f"[{name}] Loading download queue...")
                queue = client.get_queue_paths()
                LOG.info(f"[{name}] Found {len(queue)} items in queue")
                self.queue_evidence.update(queue)
                self.instance_stats[name]["queue_count"] = len(queue)
            except Exception as e:
                LOG.error(f"[{name}] Error loading queue: {e}")
        
        self.load_timestamp = datetime.now()
        LOG.info(f"=== Servarr Summary: {len(self.managed_files)} total managed files, {len(self.queue_evidence)} in queue ===")
        return len(self.managed_files)
    
    def is_managed(self, path: str) -> bool:
        return path in self.managed_files
    
    def is_in_queue(self, path: str) -> bool:
        return path in self.queue_evidence
    
    def get_protection_evidence(self, path: str) -> Optional[ProtectionEvidence]:
        if path in self.queue_evidence:
            return self.queue_evidence[path]
        
        if path in self.managed_files:
            mf, instance = self.managed_files[path]
            reason = (ProtectionReason.MANAGED_EPISODE 
                     if instance.app_type == ServarrType.SONARR 
                     else ProtectionReason.MANAGED_MOVIE)
            
            return ProtectionEvidence(
                reason=reason, instance_name=instance.name,
                instance_type=instance.app_type.value, file_id=mf.file_id,
                media_id=mf.media_id, media_title=mf.media_title,
                quality=mf.quality, quality_profile=mf.quality_profile_name,
                custom_formats=mf.custom_formats,
                custom_format_score=mf.custom_format_score,
                webui_link=mf.webui_link,
            )
        return None
    
    def get_file_info(self, path: str) -> Optional[Tuple[ManagedFile, ServarrInstance]]:
        return self.managed_files.get(path)
    
    def get_summary(self) -> dict:
        return {
            "total_instances": len(self.instances),
            "connected_instances": sum(1 for i in self.instances 
                                       if i.connection_status == ConnectionStatus.CONNECTED),
            "total_managed_files": len(self.managed_files),
            "total_queue_items": len(self.queue_evidence),
            "load_timestamp": self.load_timestamp.isoformat() if self.load_timestamp else None,
            "instances": self.instance_stats,
        }
    
    def get_instance_statuses(self) -> List[InstanceStatus]:
        statuses = []
        for name, client in self.clients.items():
            status = client.get_status()
            stats = self.instance_stats.get(name, {})
            status.managed_files_count = stats.get("files_count", 0)
            status.queue_items_count = stats.get("queue_count", 0)
            statuses.append(status)
        return statuses


# =============================================================================
# CLI PARSING HELPERS
# =============================================================================

def parse_instance_from_cli_arg(arg: str, app_type: ServarrType) -> Optional[ServarrInstance]:
    """Parse: name=X,url=Y,apikey=Z[,path_map=A:B]"""
    parts = {}
    path_maps = []
    
    for item in arg.split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if key == "path_map":
                if ":" in value:
                    pm_parts = value.split(":", 1)
                    path_maps.append(PathMapping(pm_parts[0], pm_parts[1]))
            else:
                parts[key] = value
    
    if not parts.get("url") or not parts.get("apikey"):
        return None
    
    return ServarrInstance(
        name=parts.get("name", app_type.value),
        url=parts["url"], api_key=parts["apikey"],
        app_type=app_type, path_mappings=path_maps,
    )


def parse_instances_from_env(app_type: ServarrType) -> List[ServarrInstance]:
    """Parse instances from environment variables."""
    instances = []
    prefix = app_type.value.upper()
    
    json_var = f"{prefix}_INSTANCES_JSON"
    if os.environ.get(json_var):
        try:
            configs = json.loads(os.environ[json_var])
            for cfg in configs:
                inst = ServarrInstance.from_dict(cfg, app_type)
                if inst.url and inst.api_key:
                    instances.append(inst)
            return instances
        except Exception as e:
            LOG.error(f"Failed to parse {json_var}: {e}")
    
    url = os.environ.get(f"{prefix}_URL")
    apikey = os.environ.get(f"{prefix}_APIKEY")
    if url and apikey:
        path_maps = []
        if os.environ.get(f"{prefix}_PATH_MAP"):
            for pm in os.environ[f"{prefix}_PATH_MAP"].split(";"):
                if ":" in pm:
                    parts = pm.split(":", 1)
                    path_maps.append(PathMapping(parts[0], parts[1]))
        
        instances.append(ServarrInstance(
            name=os.environ.get(f"{prefix}_NAME", app_type.value.lower()),
            url=url, api_key=apikey, app_type=app_type,
            path_mappings=path_maps,
            webui_url=os.environ.get(f"{prefix}_WEBUI_URL", ""),
        ))
    
    for i in range(1, 11):
        url = os.environ.get(f"{prefix}_{i}_URL")
        apikey = os.environ.get(f"{prefix}_{i}_APIKEY")
        if url and apikey:
            path_maps = []
            if os.environ.get(f"{prefix}_{i}_PATH_MAP"):
                for pm in os.environ[f"{prefix}_{i}_PATH_MAP"].split(";"):
                    if ":" in pm:
                        parts = pm.split(":", 1)
                        path_maps.append(PathMapping(parts[0], parts[1]))
            
            instances.append(ServarrInstance(
                name=os.environ.get(f"{prefix}_{i}_NAME", f"{app_type.value.lower()}-{i}"),
                url=url, api_key=apikey, app_type=app_type,
                path_mappings=path_maps,
            ))
    
    return instances
