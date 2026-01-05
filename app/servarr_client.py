#!/usr/bin/env python3
"""
servarr_client.py — Sonarr/Radarr API Client for Media Audit

Provides integration with Sonarr and Radarr instances:
- Fetch managed files (episodeFile, movieFile) with paths
- Get quality profiles, custom formats, scores
- Mark files as protected/managed
- Path mapping between container and host paths
- Support for multiple instances

Version: 1.0.0

API Reference:
- Sonarr v3/v4: /api/v3/episodefile, /api/v3/series, /api/v3/qualityprofile
- Radarr v3: /api/v3/moviefile, /api/v3/movie, /api/v3/qualityprofile
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
import urllib.parse
import urllib.error
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

LOG = logging.getLogger("servarr_client")


class ServarrType(Enum):
    """Type of Servarr application."""
    SONARR = "sonarr"
    RADARR = "radarr"


@dataclass
class PathMapping:
    """Maps paths between Servarr container and local host."""
    servarr_path: str  # Path as seen by Sonarr/Radarr (container path)
    local_path: str    # Path as seen by media_audit (host path)
    
    def to_local(self, path: str) -> str:
        """Convert Servarr path to local path."""
        if path.startswith(self.servarr_path):
            return path.replace(self.servarr_path, self.local_path, 1)
        return path
    
    def to_servarr(self, path: str) -> str:
        """Convert local path to Servarr path."""
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
class ManagedFile:
    """A file managed by Sonarr/Radarr."""
    file_id: int
    path: str                      # Path as reported by Servarr
    local_path: str                # Path mapped to local filesystem
    size: int
    quality: str                   # Quality name (e.g., "Bluray-1080p")
    quality_id: int
    custom_format_score: int = 0
    custom_formats: List[str] = field(default_factory=list)
    quality_cutoff_not_met: bool = False
    
    # Media info
    media_id: int = 0              # seriesId or movieId
    media_title: str = ""          # Series/Movie title
    season_number: Optional[int] = None  # For episodes
    episode_number: Optional[int] = None # For episodes
    
    # Profile info
    quality_profile_id: int = 0
    quality_profile_name: str = ""
    upgrade_recommended: bool = False
    upgrade_reason: str = ""
    
    # Link to WebUI
    webui_link: str = ""


@dataclass
class ServarrInstance:
    """Configuration for a Sonarr/Radarr instance."""
    name: str
    url: str
    api_key: str
    app_type: ServarrType
    path_mappings: List[PathMapping] = field(default_factory=list)
    root_folders: List[str] = field(default_factory=list)  # Optional: filter by root
    
    # Runtime data (populated after connection)
    version: str = ""
    is_v4: bool = False
    quality_profiles: Dict[int, QualityProfile] = field(default_factory=dict)
    
    def __post_init__(self):
        # Normalize URL
        self.url = self.url.rstrip("/")
        if not self.url.startswith("http"):
            self.url = f"http://{self.url}"
    
    @classmethod
    def from_dict(cls, data: dict) -> "ServarrInstance":
        """Create instance from dictionary config."""
        app_type = ServarrType(data.get("type", "sonarr").lower())
        
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
        )
    
    def map_path_to_local(self, path: str) -> str:
        """Map a Servarr path to local filesystem path."""
        for pm in self.path_mappings:
            mapped = pm.to_local(path)
            if mapped != path:
                return mapped
        return path
    
    def map_path_to_servarr(self, path: str) -> str:
        """Map a local path to Servarr path."""
        for pm in self.path_mappings:
            mapped = pm.to_servarr(path)
            if mapped != path:
                return mapped
        return path
    
    def get_webui_link(self, media_id: int) -> str:
        """Generate WebUI link for a media item."""
        if self.app_type == ServarrType.SONARR:
            return f"{self.url}/series/{media_id}"
        else:
            return f"{self.url}/movie/{media_id}"


class ServarrClient:
    """HTTP client for Sonarr/Radarr API."""
    
    def __init__(self, instance: ServarrInstance, timeout: int = 30):
        self.instance = instance
        self.timeout = timeout
        self._connected = False
    
    def _mask_api_key(self, url: str) -> str:
        """Mask API key in URL for logging."""
        return re.sub(r'apikey=[^&]+', 'apikey=***', url)
    
    def _request(self, endpoint: str, method: str = "GET", 
                 data: Optional[dict] = None) -> Optional[Any]:
        """Make API request to Servarr."""
        url = f"{self.instance.url}/api/v3/{endpoint}"
        headers = {
            "X-Api-Key": self.instance.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        
        try:
            if data:
                req = urllib.request.Request(
                    url, 
                    data=json.dumps(data).encode("utf-8"),
                    headers=headers,
                    method=method
                )
            else:
                req = urllib.request.Request(url, headers=headers, method=method)
            
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                content = response.read()
                if content:
                    return json.loads(content)
                return None
                
        except urllib.error.HTTPError as e:
            LOG.error(f"[{self.instance.name}] HTTP {e.code} for {endpoint}: {e.reason}")
            return None
        except urllib.error.URLError as e:
            LOG.error(f"[{self.instance.name}] Connection failed: {e.reason}")
            return None
        except json.JSONDecodeError as e:
            LOG.error(f"[{self.instance.name}] Invalid JSON response: {e}")
            return None
        except Exception as e:
            LOG.error(f"[{self.instance.name}] Request error: {e}")
            return None
    
    def connect(self) -> bool:
        """Test connection and get system info."""
        status = self._request("system/status")
        if not status:
            LOG.error(f"[{self.instance.name}] Failed to connect to {self.instance.url}")
            return False
        
        self.instance.version = status.get("version", "unknown")
        # Sonarr v4 has version starting with 4.x
        version_major = int(self.instance.version.split(".")[0]) if self.instance.version else 0
        self.instance.is_v4 = version_major >= 4
        
        LOG.info(f"[{self.instance.name}] Connected: {self.instance.app_type.value} v{self.instance.version}")
        self._connected = True
        
        # Load quality profiles
        self._load_quality_profiles()
        
        return True
    
    def _load_quality_profiles(self):
        """Load quality profiles from the instance."""
        profiles = self._request("qualityprofile")
        if profiles:
            for p in profiles:
                qp = QualityProfile.from_dict(p)
                self.instance.quality_profiles[qp.id] = qp
            LOG.debug(f"[{self.instance.name}] Loaded {len(self.instance.quality_profiles)} quality profiles")
    
    def get_root_folders(self) -> List[dict]:
        """Get configured root folders."""
        return self._request("rootfolder") or []
    
    def get_all_series(self) -> List[dict]:
        """Get all series (Sonarr only)."""
        if self.instance.app_type != ServarrType.SONARR:
            return []
        return self._request("series") or []
    
    def get_all_movies(self) -> List[dict]:
        """Get all movies (Radarr only)."""
        if self.instance.app_type != ServarrType.RADARR:
            return []
        return self._request("movie") or []
    
    def get_episode_files(self, series_id: int) -> List[dict]:
        """Get all episode files for a series (Sonarr only)."""
        if self.instance.app_type != ServarrType.SONARR:
            return []
        return self._request(f"episodefile?seriesId={series_id}") or []
    
    def get_movie_file(self, movie_id: int) -> Optional[dict]:
        """Get movie file for a movie (Radarr only)."""
        if self.instance.app_type != ServarrType.RADARR:
            return None
        files = self._request(f"moviefile?movieId={movie_id}")
        if files and len(files) > 0:
            return files[0]
        return None
    
    def get_queue(self) -> List[dict]:
        """Get current download queue."""
        queue = self._request("queue?pageSize=1000")
        if queue and "records" in queue:
            return queue["records"]
        return []
    
    def rescan_series(self, series_id: int) -> bool:
        """Trigger a rescan for a series (Sonarr only)."""
        if self.instance.app_type != ServarrType.SONARR:
            return False
        result = self._request("command", method="POST", data={
            "name": "RescanSeries",
            "seriesId": series_id
        })
        return result is not None
    
    def rescan_movie(self, movie_id: int) -> bool:
        """Trigger a rescan for a movie (Radarr only)."""
        if self.instance.app_type != ServarrType.RADARR:
            return False
        result = self._request("command", method="POST", data={
            "name": "RescanMovie",
            "movieId": movie_id
        })
        return result is not None
    
    def get_all_managed_files(self) -> Dict[str, ManagedFile]:
        """
        Get all files managed by this instance.
        Returns dict mapping local_path -> ManagedFile
        """
        if not self._connected and not self.connect():
            return {}
        
        files: Dict[str, ManagedFile] = {}
        
        if self.instance.app_type == ServarrType.SONARR:
            files = self._get_sonarr_files()
        else:
            files = self._get_radarr_files()
        
        LOG.info(f"[{self.instance.name}] Found {len(files)} managed files")
        return files
    
    def _get_sonarr_files(self) -> Dict[str, ManagedFile]:
        """Get all episode files from Sonarr."""
        files: Dict[str, ManagedFile] = {}
        
        series_list = self.get_all_series()
        LOG.info(f"[{self.instance.name}] Processing {len(series_list)} series")
        
        for series in series_list:
            series_id = series.get("id", 0)
            series_title = series.get("title", "Unknown")
            quality_profile_id = series.get("qualityProfileId", 0)
            qp = self.instance.quality_profiles.get(quality_profile_id, QualityProfile(id=0, name="Unknown"))
            
            # Filter by root folder if specified
            series_path = series.get("path", "")
            if self.instance.root_folders:
                if not any(series_path.startswith(rf) for rf in self.instance.root_folders):
                    continue
            
            episode_files = self.get_episode_files(series_id)
            
            for ef in episode_files:
                file_id = ef.get("id", 0)
                path = ef.get("path", "")
                local_path = self.instance.map_path_to_local(path)
                
                # Quality info
                quality_obj = ef.get("quality", {}).get("quality", {})
                quality_name = quality_obj.get("name", "Unknown")
                quality_id = quality_obj.get("id", 0)
                
                # Custom formats (v4 only)
                custom_formats = []
                custom_format_score = 0
                if self.instance.is_v4 and "customFormats" in ef:
                    for cf in ef.get("customFormats", []):
                        cf_name = cf.get("name", "")
                        if cf_name:
                            custom_formats.append(cf_name)
                    # Note: Sonarr v4 doesn't always include score on episodeFile
                
                # Cutoff status
                quality_cutoff_not_met = ef.get("qualityCutoffNotMet", False)
                
                # Determine upgrade recommendation
                upgrade_recommended = False
                upgrade_reason = ""
                if quality_cutoff_not_met:
                    upgrade_recommended = True
                    upgrade_reason = "Quality cutoff not met"
                elif qp.upgrade_allowed and qp.min_upgrade_format_score > 0:
                    if custom_format_score < qp.min_upgrade_format_score:
                        upgrade_recommended = True
                        upgrade_reason = f"CF score {custom_format_score} < min {qp.min_upgrade_format_score}"
                
                mf = ManagedFile(
                    file_id=file_id,
                    path=path,
                    local_path=local_path,
                    size=ef.get("size", 0),
                    quality=quality_name,
                    quality_id=quality_id,
                    custom_format_score=custom_format_score,
                    custom_formats=custom_formats,
                    quality_cutoff_not_met=quality_cutoff_not_met,
                    media_id=series_id,
                    media_title=series_title,
                    season_number=ef.get("seasonNumber"),
                    episode_number=None,  # Would need episode lookup for this
                    quality_profile_id=quality_profile_id,
                    quality_profile_name=qp.name,
                    upgrade_recommended=upgrade_recommended,
                    upgrade_reason=upgrade_reason,
                    webui_link=self.instance.get_webui_link(series_id),
                )
                
                files[local_path] = mf
        
        return files
    
    def _get_radarr_files(self) -> Dict[str, ManagedFile]:
        """Get all movie files from Radarr."""
        files: Dict[str, ManagedFile] = {}
        
        movie_list = self.get_all_movies()
        LOG.info(f"[{self.instance.name}] Processing {len(movie_list)} movies")
        
        for movie in movie_list:
            movie_id = movie.get("id", 0)
            movie_title = movie.get("title", "Unknown")
            quality_profile_id = movie.get("qualityProfileId", 0)
            qp = self.instance.quality_profiles.get(quality_profile_id, QualityProfile(id=0, name="Unknown"))
            
            # Filter by root folder if specified
            movie_path = movie.get("path", "")
            if self.instance.root_folders:
                if not any(movie_path.startswith(rf) for rf in self.instance.root_folders):
                    continue
            
            # Check if movie has a file
            if not movie.get("hasFile", False):
                continue
            
            mf_data = movie.get("movieFile")
            if not mf_data:
                mf_data = self.get_movie_file(movie_id)
            
            if not mf_data:
                continue
            
            file_id = mf_data.get("id", 0)
            path = mf_data.get("path", "")
            local_path = self.instance.map_path_to_local(path)
            
            # Quality info
            quality_obj = mf_data.get("quality", {}).get("quality", {})
            quality_name = quality_obj.get("name", "Unknown")
            quality_id = quality_obj.get("id", 0)
            
            # Custom formats and score (Radarr has this)
            custom_formats = []
            custom_format_score = mf_data.get("customFormatScore", 0)
            for cf in mf_data.get("customFormats", []):
                cf_name = cf.get("name", "")
                if cf_name:
                    custom_formats.append(cf_name)
            
            # Cutoff status
            quality_cutoff_not_met = mf_data.get("qualityCutoffNotMet", False)
            
            # Determine upgrade recommendation
            upgrade_recommended = False
            upgrade_reason = ""
            if quality_cutoff_not_met:
                upgrade_recommended = True
                upgrade_reason = "Quality cutoff not met"
            elif qp.upgrade_allowed:
                if qp.cutoff_format_score > 0 and custom_format_score < qp.cutoff_format_score:
                    upgrade_recommended = True
                    upgrade_reason = f"CF score {custom_format_score} < cutoff {qp.cutoff_format_score}"
            
            mf = ManagedFile(
                file_id=file_id,
                path=path,
                local_path=local_path,
                size=mf_data.get("size", 0),
                quality=quality_name,
                quality_id=quality_id,
                custom_format_score=custom_format_score,
                custom_formats=custom_formats,
                quality_cutoff_not_met=quality_cutoff_not_met,
                media_id=movie_id,
                media_title=movie_title,
                quality_profile_id=quality_profile_id,
                quality_profile_name=qp.name,
                upgrade_recommended=upgrade_recommended,
                upgrade_reason=upgrade_reason,
                webui_link=self.instance.get_webui_link(movie_id),
            )
            
            files[local_path] = mf
        
        return files
    
    def get_queue_paths(self) -> Set[str]:
        """Get paths of files currently in download queue."""
        paths = set()
        queue = self.get_queue()
        for item in queue:
            # Queue items have outputPath when downloading
            output_path = item.get("outputPath", "")
            if output_path:
                local_path = self.instance.map_path_to_local(output_path)
                paths.add(local_path)
        return paths


class ServarrManager:
    """
    Manages multiple Sonarr/Radarr instances.
    Aggregates files from all instances for protection checks.
    """
    
    def __init__(self):
        self.instances: List[ServarrInstance] = []
        self.clients: Dict[str, ServarrClient] = {}
        
        # Aggregated data
        self.managed_files: Dict[str, Tuple[ManagedFile, ServarrInstance]] = {}
        self.queue_paths: Set[str] = set()
    
    def add_instance(self, instance: ServarrInstance) -> bool:
        """Add and connect to a Servarr instance."""
        client = ServarrClient(instance)
        if client.connect():
            self.instances.append(instance)
            self.clients[instance.name] = client
            return True
        return False
    
    def add_instance_from_config(self, config: dict) -> bool:
        """Add instance from dictionary config."""
        try:
            instance = ServarrInstance.from_dict(config)
            return self.add_instance(instance)
        except Exception as e:
            LOG.error(f"Failed to add instance from config: {e}")
            return False
    
    def load_all_files(self) -> int:
        """Load all managed files from all instances."""
        self.managed_files.clear()
        self.queue_paths.clear()
        
        for name, client in self.clients.items():
            instance = next(i for i in self.instances if i.name == name)
            
            # Get managed files
            files = client.get_all_managed_files()
            for path, mf in files.items():
                self.managed_files[path] = (mf, instance)
            
            # Get queue paths
            queue_paths = client.get_queue_paths()
            self.queue_paths.update(queue_paths)
        
        LOG.info(f"Total managed files across all instances: {len(self.managed_files)}")
        LOG.info(f"Files in download queue: {len(self.queue_paths)}")
        
        return len(self.managed_files)
    
    def is_managed(self, path: str) -> bool:
        """Check if a path is managed by any Servarr instance."""
        return path in self.managed_files
    
    def is_in_queue(self, path: str) -> bool:
        """Check if a path is in any download queue."""
        return path in self.queue_paths
    
    def get_file_info(self, path: str) -> Optional[Tuple[ManagedFile, ServarrInstance]]:
        """Get file info if managed."""
        return self.managed_files.get(path)
    
    def trigger_rescan(self, path: str) -> bool:
        """Trigger a rescan for the media containing this file."""
        info = self.managed_files.get(path)
        if not info:
            return False
        
        mf, instance = info
        client = self.clients.get(instance.name)
        if not client:
            return False
        
        if instance.app_type == ServarrType.SONARR:
            return client.rescan_series(mf.media_id)
        else:
            return client.rescan_movie(mf.media_id)


def parse_instances_from_env() -> List[dict]:
    """
    Parse Servarr instance configurations from environment variables.
    
    Supports two formats:
    1. JSON: SONARR_INSTANCES_JSON='[{"name":"main","url":"...","api_key":"..."}]'
    2. Numbered: SONARR_1_URL, SONARR_1_APIKEY, SONARR_1_NAME, SONARR_1_PATH_MAP
    
    Returns list of instance config dicts.
    """
    instances = []
    
    # Try JSON format first
    for app_type in ["SONARR", "RADARR"]:
        json_var = f"{app_type}_INSTANCES_JSON"
        if json_var in os.environ:
            try:
                parsed = json.loads(os.environ[json_var])
                for inst in parsed:
                    inst["type"] = app_type.lower()
                    instances.append(inst)
            except json.JSONDecodeError as e:
                LOG.error(f"Failed to parse {json_var}: {e}")
    
    # Try numbered format
    for app_type in ["SONARR", "RADARR"]:
        for i in range(1, 10):
            url_var = f"{app_type}_{i}_URL"
            if url_var in os.environ:
                inst = {
                    "type": app_type.lower(),
                    "name": os.environ.get(f"{app_type}_{i}_NAME", f"{app_type.lower()}_{i}"),
                    "url": os.environ.get(url_var, ""),
                    "api_key": os.environ.get(f"{app_type}_{i}_APIKEY", ""),
                    "path_mappings": [],
                }
                
                # Parse path mappings
                path_map = os.environ.get(f"{app_type}_{i}_PATH_MAP", "")
                if path_map:
                    for mapping in path_map.split(";"):
                        mapping = mapping.strip()
                        if ":" in mapping:
                            inst["path_mappings"].append(mapping)
                
                if inst["url"] and inst["api_key"]:
                    instances.append(inst)
    
    # Legacy single-instance format (for backwards compatibility)
    for app_type in ["SONARR", "RADARR"]:
        url_var = f"{app_type}_URL"
        apikey_var = f"{app_type}_APIKEY"
        if url_var in os.environ and apikey_var in os.environ:
            # Check we haven't already added this via numbered format
            url = os.environ[url_var]
            if not any(i.get("url") == url for i in instances):
                inst = {
                    "type": app_type.lower(),
                    "name": os.environ.get(f"{app_type}_NAME", app_type.lower()),
                    "url": url,
                    "api_key": os.environ[apikey_var],
                    "path_mappings": [],
                }
                
                path_map = os.environ.get(f"{app_type}_PATH_MAP", "")
                if path_map:
                    for mapping in path_map.split(";"):
                        mapping = mapping.strip()
                        if ":" in mapping:
                            inst["path_mappings"].append(mapping)
                
                instances.append(inst)
    
    return instances


def parse_instances_from_cli(sonarr_args: List[str], radarr_args: List[str]) -> List[dict]:
    """
    Parse Servarr instance configurations from CLI arguments.
    
    Format: name=MyName,url=http://...,apikey=...,path_map=remote:local
    
    Returns list of instance config dicts.
    """
    instances = []
    
    for arg in sonarr_args:
        inst = _parse_cli_instance_arg(arg, "sonarr")
        if inst:
            instances.append(inst)
    
    for arg in radarr_args:
        inst = _parse_cli_instance_arg(arg, "radarr")
        if inst:
            instances.append(inst)
    
    return instances


def _parse_cli_instance_arg(arg: str, app_type: str) -> Optional[dict]:
    """Parse a single CLI instance argument."""
    inst = {
        "type": app_type,
        "name": app_type,
        "url": "",
        "api_key": "",
        "path_mappings": [],
    }
    
    for part in arg.split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            
            if key == "name":
                inst["name"] = value
            elif key == "url":
                inst["url"] = value
            elif key in ("apikey", "api_key", "key"):
                inst["api_key"] = value
            elif key in ("path_map", "pathmap"):
                inst["path_mappings"].append(value)
    
    if inst["url"] and inst["api_key"]:
        return inst
    return None


def load_instances_from_json_file(filepath: str) -> List[dict]:
    """Load instance configurations from a JSON file."""
    try:
        with open(filepath, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                # Support single instance or {"instances": [...]}
                if "instances" in data:
                    return data["instances"]
                return [data]
    except Exception as e:
        LOG.error(f"Failed to load instances from {filepath}: {e}")
    return []
