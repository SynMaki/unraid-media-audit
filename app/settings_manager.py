#!/usr/bin/env python3
"""
settings_manager.py — WebUI Settings Manager for Media Audit v3.2.0

Handles JSON-based configuration with:
- Proper qBittorrent authentication (login + CSRF)
- Sonarr/Radarr connection testing with root folder discovery
- File-based logging to /config/logs/
- Environment variable migration on first run

qBittorrent API Reference:
- https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)
- Login: POST /api/v2/auth/login with username/password form data
- Requires Referer header matching the host
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import urllib.request
import urllib.error
import urllib.parse
import http.cookiejar
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional


def setup_logging(config_dir: str) -> logging.Logger:
    """Setup file + console logging."""
    log_dir = Path(config_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    
    log_file = log_dir / f"media_audit_{datetime.now().strftime('%Y%m%d')}.log"
    
    # Create logger
    logger = logging.getLogger("media_audit")
    logger.setLevel(logging.DEBUG)
    
    # Clear existing handlers
    logger.handlers = []
    
    # File handler (detailed)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)
    
    # Console handler (info+)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(ch)
    
    return logger


LOG = logging.getLogger("media_audit.settings")


DEFAULT_SETTINGS = {
    "general": {
        "report_dir": "/reports",
        "roots": ["/media"],
        "delete_under": "/media",
        "ffprobe_scope": "dupes",
        "content_type": "auto",
        "avoid_mode": "if-no-prefer",
        "avoid_audio_lang": [],
    },
    "qbittorrent": {
        "enabled": False,
        "host": "",
        "port": 8080,
        "username": "",
        "password": "",
        "path_mappings": [],
        "webui_url": "",
        "tag_duplicates": False,
    },
    "sonarr_instances": [],
    "radarr_instances": [],
    "web": {
        "auth_enabled": False,
        "username": "",
        "password": "",
    },
}


class QBittorrentClient:
    """qBittorrent API client with proper authentication."""
    
    def __init__(self, host: str, port: int, username: str = "", password: str = ""):
        self.base_url = f"http://{host}:{port}"
        self.username = username
        self.password = password
        self._sid: Optional[str] = None
        
        # Setup cookie jar and opener
        self._cookie_jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar)
        )
    
    def _request(self, endpoint: str, method: str = "GET", 
                 data: Optional[Dict] = None, timeout: int = 10) -> Optional[str]:
        """Make authenticated request to qBittorrent."""
        url = f"{self.base_url}{endpoint}"
        
        headers = {
            "Referer": self.base_url,
            "Origin": self.base_url,
        }
        
        try:
            if data:
                encoded = urllib.parse.urlencode(data).encode("utf-8")
                req = urllib.request.Request(url, data=encoded, headers=headers, method="POST")
            else:
                req = urllib.request.Request(url, headers=headers, method=method)
            
            with self._opener.open(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
                
        except urllib.error.HTTPError as e:
            LOG.error(f"qBittorrent HTTP {e.code}: {e.reason} for {endpoint}")
            raise
        except Exception as e:
            LOG.error(f"qBittorrent request failed: {e}")
            raise
    
    def login(self) -> bool:
        """Authenticate with qBittorrent. Returns True on success."""
        try:
            result = self._request("/api/v2/auth/login", data={
                "username": self.username,
                "password": self.password,
            })
            
            if result and result.strip().lower() == "ok.":
                # Extract SID from cookies
                for cookie in self._cookie_jar:
                    if cookie.name == "SID":
                        self._sid = cookie.value
                        break
                LOG.info(f"qBittorrent login successful")
                return True
            else:
                LOG.error(f"qBittorrent login failed: {result}")
                return False
                
        except urllib.error.HTTPError as e:
            if e.code == 403:
                LOG.error("qBittorrent login forbidden - check credentials and WebUI settings")
            raise
    
    def get_version(self) -> str:
        """Get qBittorrent version."""
        return self._request("/api/v2/app/version") or "unknown"
    
    def get_torrents(self) -> List[Dict]:
        """Get all torrents."""
        result = self._request("/api/v2/torrents/info")
        if result:
            return json.loads(result)
        return []
    
    def test_connection(self) -> Dict[str, Any]:
        """Test connection and return status."""
        try:
            # First try without auth (some setups don't require it)
            try:
                version = self._request("/api/v2/app/version", timeout=5)
                if version:
                    return {
                        "success": True,
                        "message": "Connected (no auth required)",
                        "details": {"version": version.strip()}
                    }
            except urllib.error.HTTPError as e:
                if e.code != 403:
                    raise
                # 403 means we need to login
            
            # Try with authentication
            if not self.username:
                return {
                    "success": False,
                    "message": "Authentication required - please provide username/password"
                }
            
            if self.login():
                version = self.get_version()
                torrents = self.get_torrents()
                return {
                    "success": True,
                    "message": f"Connected with {len(torrents)} torrents",
                    "details": {
                        "version": version.strip(),
                        "torrent_count": len(torrents),
                    }
                }
            else:
                return {
                    "success": False,
                    "message": "Login failed - check username/password"
                }
                
        except urllib.error.HTTPError as e:
            return {
                "success": False,
                "message": f"HTTP Error: {e.code} {e.reason}"
            }
        except urllib.error.URLError as e:
            return {
                "success": False,
                "message": f"Connection failed: {e.reason}"
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Error: {str(e)}"
            }


class ServarrTestClient:
    """Test client for Sonarr/Radarr."""
    
    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE
    
    def _request(self, endpoint: str) -> Optional[Any]:
        """Make API request."""
        url = f"{self.url}/api/v3/{endpoint}"
        req = urllib.request.Request(url)
        req.add_header("X-Api-Key", self.api_key)
        req.add_header("Accept", "application/json")
        
        ctx = self._ssl_ctx if self.url.startswith("https") else None
        
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            LOG.error(f"Servarr request failed: {e}")
            raise
    
    def test_connection(self, app_type: str) -> Dict[str, Any]:
        """Test connection and get root folders."""
        try:
            # Get system status
            status = self._request("system/status")
            version = status.get("version", "unknown")
            
            # Get root folders for path suggestions
            root_folders = self._request("rootfolder")
            roots = [rf.get("path", "") for rf in root_folders if rf.get("path")]
            
            # Get series/movie count
            if app_type == "sonarr":
                items = self._request("series")
                item_type = "series"
            else:
                items = self._request("movie")
                item_type = "movies"
            
            return {
                "success": True,
                "message": f"Connected - {len(items)} {item_type}",
                "details": {
                    "version": version,
                    "root_folders": roots,
                    f"{item_type}_count": len(items),
                }
            }
            
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return {"success": False, "message": "Invalid API key"}
            return {"success": False, "message": f"HTTP {e.code}: {e.reason}"}
        except urllib.error.URLError as e:
            return {"success": False, "message": f"Connection failed: {e.reason}"}
        except Exception as e:
            return {"success": False, "message": f"Error: {str(e)}"}


class SettingsManager:
    """Thread-safe settings management with JSON persistence."""
    
    def __init__(self, config_dir: str = "/config"):
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.settings_file = self.config_dir / "settings.json"
        self._lock = RLock()
        self._settings: Dict[str, Any] = {}
        self._load()
    
    def _load(self):
        """Load settings from file or create defaults."""
        with self._lock:
            if self.settings_file.exists():
                try:
                    with open(self.settings_file, "r", encoding="utf-8") as f:
                        self._settings = json.load(f)
                    LOG.info(f"Loaded settings from {self.settings_file}")
                    self._migrate_if_needed()
                except Exception as e:
                    LOG.error(f"Failed to load settings: {e}")
                    self._settings = deepcopy(DEFAULT_SETTINGS)
            else:
                LOG.info("No settings file found, creating from environment/defaults")
                self._settings = deepcopy(DEFAULT_SETTINGS)
                self._import_from_env()
                self._save()
    
    def _save(self) -> bool:
        """Save settings to file."""
        try:
            with open(self.settings_file, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, indent=2, ensure_ascii=False)
            LOG.debug("Settings saved")
            return True
        except Exception as e:
            LOG.error(f"Failed to save settings: {e}")
            return False
    
    def _migrate_if_needed(self):
        """Ensure all default keys exist."""
        changed = False
        for section, defaults in DEFAULT_SETTINGS.items():
            if section not in self._settings:
                self._settings[section] = deepcopy(defaults)
                changed = True
            elif isinstance(defaults, dict):
                for key, value in defaults.items():
                    if key not in self._settings[section]:
                        self._settings[section][key] = deepcopy(value)
                        changed = True
        if changed:
            self._save()
    
    def _import_from_env(self):
        """Import settings from environment variables."""
        LOG.info("Importing settings from environment variables")
        
        # General
        g = self._settings["general"]
        if os.environ.get("REPORT_DIR"):
            g["report_dir"] = os.environ["REPORT_DIR"]
        if os.environ.get("ROOTS"):
            g["roots"] = [r.strip() for r in os.environ["ROOTS"].split(",") if r.strip()]
        if os.environ.get("DELETE_UNDER"):
            g["delete_under"] = os.environ["DELETE_UNDER"]
        if os.environ.get("FFPROBE_SCOPE"):
            g["ffprobe_scope"] = os.environ["FFPROBE_SCOPE"]
        if os.environ.get("CONTENT_TYPE"):
            g["content_type"] = os.environ["CONTENT_TYPE"]
        
        # qBittorrent
        qb = self._settings["qbittorrent"]
        if os.environ.get("QBIT_HOST"):
            qb["enabled"] = True
            qb["host"] = os.environ["QBIT_HOST"]
            qb["port"] = int(os.environ.get("QBIT_PORT", "8080"))
            qb["username"] = os.environ.get("QBIT_USER", "")
            qb["password"] = os.environ.get("QBIT_PASS", "")
            
            if os.environ.get("QBIT_PATH_MAP"):
                for mapping in os.environ["QBIT_PATH_MAP"].split(";"):
                    if ":" in mapping:
                        qp, lp = mapping.split(":", 1)
                        qb["path_mappings"].append({"qbit_path": qp, "local_path": lp})
        
        # Sonarr
        if os.environ.get("SONARR_URL") and os.environ.get("SONARR_APIKEY"):
            self._settings["sonarr_instances"].append({
                "enabled": True,
                "name": os.environ.get("SONARR_NAME", "sonarr"),
                "url": os.environ["SONARR_URL"],
                "api_key": os.environ["SONARR_APIKEY"],
                "webui_url": os.environ.get("SONARR_WEBUI_URL", ""),
                "path_mappings": [],
            })
        
        # Radarr
        if os.environ.get("RADARR_URL") and os.environ.get("RADARR_APIKEY"):
            self._settings["radarr_instances"].append({
                "enabled": True,
                "name": os.environ.get("RADARR_NAME", "radarr"),
                "url": os.environ["RADARR_URL"],
                "api_key": os.environ["RADARR_APIKEY"],
                "webui_url": os.environ.get("RADARR_WEBUI_URL", ""),
                "path_mappings": [],
            })
        
        # Web auth
        if os.environ.get("AUTH_USER") and os.environ.get("AUTH_PASS"):
            self._settings["web"]["auth_enabled"] = True
            self._settings["web"]["username"] = os.environ["AUTH_USER"]
            self._settings["web"]["password"] = os.environ["AUTH_PASS"]
    
    def get_all(self) -> Dict[str, Any]:
        """Get all settings with masked sensitive data."""
        with self._lock:
            result = deepcopy(self._settings)
            
            # Mask qBittorrent password
            if result.get("qbittorrent", {}).get("password"):
                result["qbittorrent"]["password_masked"] = "********"
            
            # Mask Sonarr/Radarr API keys
            for inst in result.get("sonarr_instances", []):
                if inst.get("api_key"):
                    k = inst["api_key"]
                    inst["api_key_masked"] = f"{k[:4]}...{k[-4:]}" if len(k) > 8 else "****"
            for inst in result.get("radarr_instances", []):
                if inst.get("api_key"):
                    k = inst["api_key"]
                    inst["api_key_masked"] = f"{k[:4]}...{k[-4:]}" if len(k) > 8 else "****"
            
            # Mask web password
            if result.get("web", {}).get("password"):
                result["web"]["password_masked"] = "********"
            
            return result
    
    def get_all_raw(self) -> Dict[str, Any]:
        """Get all settings including sensitive data (for internal use)."""
        with self._lock:
            return deepcopy(self._settings)
    
    def get(self, section: str, key: Optional[str] = None) -> Any:
        """Get a setting or section."""
        with self._lock:
            if section not in self._settings:
                return None
            if key is None:
                return deepcopy(self._settings[section])
            return deepcopy(self._settings[section].get(key))
    
    def update(self, section: str, data: Dict[str, Any]) -> bool:
        """Update a settings section."""
        with self._lock:
            if section not in self._settings:
                return False
            
            if isinstance(self._settings[section], dict):
                for key, value in data.items():
                    self._settings[section][key] = value
            else:
                self._settings[section] = data
            
            LOG.info(f"Updated settings section: {section}")
            return self._save()
    
    def add_instance(self, app_type: str, instance: Dict[str, Any]) -> bool:
        """Add a Sonarr/Radarr instance."""
        with self._lock:
            key = f"{app_type}_instances"
            if key not in self._settings:
                return False
            
            # Ensure required fields
            instance.setdefault("enabled", True)
            instance.setdefault("name", f"{app_type}-{len(self._settings[key]) + 1}")
            instance.setdefault("path_mappings", [])
            
            self._settings[key].append(instance)
            LOG.info(f"Added {app_type} instance: {instance.get('name')}")
            return self._save()
    
    def update_instance(self, app_type: str, index: int, instance: Dict[str, Any]) -> bool:
        """Update a Sonarr/Radarr instance."""
        with self._lock:
            key = f"{app_type}_instances"
            if key not in self._settings:
                return False
            if index < 0 or index >= len(self._settings[key]):
                return False
            
            # Preserve API key if not provided
            if not instance.get("api_key") and self._settings[key][index].get("api_key"):
                instance["api_key"] = self._settings[key][index]["api_key"]
            
            self._settings[key][index] = instance
            LOG.info(f"Updated {app_type} instance {index}: {instance.get('name')}")
            return self._save()
    
    def remove_instance(self, app_type: str, index: int) -> bool:
        """Remove a Sonarr/Radarr instance."""
        with self._lock:
            key = f"{app_type}_instances"
            if key not in self._settings:
                return False
            if index < 0 or index >= len(self._settings[key]):
                return False
            
            removed = self._settings[key].pop(index)
            LOG.info(f"Removed {app_type} instance: {removed.get('name')}")
            return self._save()
    
    def test_connection(self, app_type: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """Test connection to qBittorrent/Sonarr/Radarr."""
        LOG.info(f"Testing {app_type} connection")
        
        if app_type == "qbittorrent":
            client = QBittorrentClient(
                host=config.get("host", ""),
                port=config.get("port", 8080),
                username=config.get("username", ""),
                password=config.get("password", ""),
            )
            result = client.test_connection()
            
        elif app_type in ("sonarr", "radarr"):
            url = config.get("url", "")
            api_key = config.get("api_key", "")
            
            if not url:
                return {"success": False, "message": "URL is required"}
            if not api_key:
                return {"success": False, "message": "API key is required"}
            
            client = ServarrTestClient(url, api_key)
            result = client.test_connection(app_type)
        else:
            return {"success": False, "message": f"Unknown app type: {app_type}"}
        
        LOG.info(f"Test result for {app_type}: {result.get('message')}")
        return result


# Global instance
_manager: Optional[SettingsManager] = None


def get_settings_manager(config_dir: str = "/config") -> SettingsManager:
    """Get or create the global settings manager."""
    global _manager
    if _manager is None:
        # Setup logging first
        setup_logging(config_dir)
        _manager = SettingsManager(config_dir)
    return _manager
