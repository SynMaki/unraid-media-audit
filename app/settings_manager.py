#!/usr/bin/env python3
"""
Settings Manager for Media Audit

Handles persistent configuration storage and retrieval.
Settings are stored in a JSON file and can be edited via the WebUI.
"""

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional
from copy import deepcopy

LOG = logging.getLogger("settings_manager")


# =============================================================================
# DEFAULT CONFIGURATION
# =============================================================================

DEFAULT_SETTINGS = {
    "general": {
        "report_dir": "/reports",
        "roots": ["/media"],
        "delete_under": "/media",
        "ffprobe_scope": "dupes",  # none, dupes, all
        "content_type": "auto",    # auto, anime, series, movie
        "avoid_mode": "if-no-prefer",  # if-no-prefer, strict, report-only
        "avoid_audio_lang": [],
        "allow_delete": False,
        "schedule_enabled": False,
        "schedule_cron": "0 3 * * *",
    },
    "qbittorrent": {
        "enabled": False,
        "host": "",
        "port": 8080,
        "username": "",
        "password": "",
        "path_mappings": [],  # List of {"qbit_path": "/downloads", "local_path": "/media/torrents"}
        "webui_url": "",
    },
    "sonarr_instances": [],
    # Each instance: {
    #     "enabled": True,
    #     "name": "main",
    #     "url": "http://sonarr:8989",
    #     "api_key": "xxx",
    #     "path_mappings": [{"servarr_path": "/tv", "local_path": "/media/Serien"}],
    # }
    "radarr_instances": [],
    # Same structure as sonarr_instances
    "web": {
        "auth_enabled": False,
        "username": "",
        "password": "",
    },
}


# =============================================================================
# SETTINGS MANAGER CLASS
# =============================================================================

class SettingsManager:
    """
    Manages application settings with file persistence.
    
    Settings are loaded from a JSON file and can be updated via the WebUI.
    Changes are automatically persisted to disk.
    """
    
    def __init__(self, config_dir: str = "/config"):
        self.config_dir = Path(config_dir)
        self.config_file = self.config_dir / "settings.json"
        self._lock = threading.RLock()
        self._settings: Dict[str, Any] = {}
        self._load_settings()
    
    def _load_settings(self) -> None:
        """Load settings from file or initialize with defaults."""
        with self._lock:
            # Start with defaults
            self._settings = deepcopy(DEFAULT_SETTINGS)
            
            # Try to load from file
            if self.config_file.exists():
                try:
                    with open(self.config_file, "r", encoding="utf-8") as f:
                        saved = json.load(f)
                    
                    # Deep merge saved settings into defaults
                    self._deep_merge(self._settings, saved)
                    LOG.info(f"Settings loaded from {self.config_file}")
                except Exception as e:
                    LOG.error(f"Failed to load settings: {e}")
            else:
                # First run - check environment variables for initial config
                self._load_from_environment()
                self._save_settings()
                LOG.info("Initialized settings with defaults")
    
    def _deep_merge(self, base: dict, override: dict) -> None:
        """Deep merge override into base dict (modifies base in-place)."""
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value
    
    def _load_from_environment(self) -> None:
        """Load initial settings from environment variables."""
        g = self._settings["general"]
        q = self._settings["qbittorrent"]
        w = self._settings["web"]
        
        # General settings
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
        if os.environ.get("AVOID_MODE"):
            g["avoid_mode"] = os.environ["AVOID_MODE"]
        if os.environ.get("AVOID_AUDIO_LANG"):
            g["avoid_audio_lang"] = [l.strip() for l in os.environ["AVOID_AUDIO_LANG"].split(",") if l.strip()]
        if os.environ.get("ALLOW_DELETE", "").lower() == "true":
            g["allow_delete"] = True
        if os.environ.get("SCHEDULE_ENABLED", "").lower() == "true":
            g["schedule_enabled"] = True
        if os.environ.get("SCHEDULE_CRON"):
            g["schedule_cron"] = os.environ["SCHEDULE_CRON"]
        
        # qBittorrent settings
        if os.environ.get("QBIT_HOST"):
            q["enabled"] = True
            q["host"] = os.environ["QBIT_HOST"]
            q["port"] = int(os.environ.get("QBIT_PORT", "8080"))
            q["username"] = os.environ.get("QBIT_USER", "")
            q["password"] = os.environ.get("QBIT_PASS", "")
            q["webui_url"] = os.environ.get("QBIT_WEBUI_URL", "")
            
            if os.environ.get("QBIT_PATH_MAP"):
                for mapping in os.environ["QBIT_PATH_MAP"].split(";"):
                    mapping = mapping.strip()
                    if ":" in mapping:
                        parts = mapping.split(":", 1)
                        q["path_mappings"].append({
                            "qbit_path": parts[0],
                            "local_path": parts[1]
                        })
        
        # Web auth
        if os.environ.get("WEB_USER") and os.environ.get("WEB_PASS"):
            w["auth_enabled"] = True
            w["username"] = os.environ["WEB_USER"]
            w["password"] = os.environ["WEB_PASS"]
        
        # Sonarr instances from environment
        self._load_servarr_from_env("sonarr")
        self._load_servarr_from_env("radarr")
    
    def _load_servarr_from_env(self, app_type: str) -> None:
        """Load Sonarr/Radarr instances from environment variables."""
        prefix = app_type.upper()
        instances_key = f"{app_type}_instances"
        
        # Check for JSON config
        json_var = f"{prefix}_INSTANCES_JSON"
        if os.environ.get(json_var):
            try:
                instances = json.loads(os.environ[json_var])
                for inst in instances:
                    self._settings[instances_key].append({
                        "enabled": inst.get("enabled", True),
                        "name": inst.get("name", app_type),
                        "url": inst.get("url", ""),
                        "api_key": inst.get("api_key", ""),
                        "path_mappings": self._parse_path_mappings(inst.get("path_mappings", [])),
                    })
                return
            except Exception as e:
                LOG.error(f"Failed to parse {json_var}: {e}")
        
        # Check for single instance config
        url = os.environ.get(f"{prefix}_URL")
        apikey = os.environ.get(f"{prefix}_APIKEY")
        if url and apikey:
            path_mappings = []
            if os.environ.get(f"{prefix}_PATH_MAP"):
                for mapping in os.environ[f"{prefix}_PATH_MAP"].split(";"):
                    mapping = mapping.strip()
                    if ":" in mapping:
                        parts = mapping.split(":", 1)
                        path_mappings.append({
                            "servarr_path": parts[0],
                            "local_path": parts[1]
                        })
            
            self._settings[instances_key].append({
                "enabled": True,
                "name": os.environ.get(f"{prefix}_NAME", app_type.lower()),
                "url": url,
                "api_key": apikey,
                "path_mappings": path_mappings,
            })
        
        # Check for numbered instances (SONARR_1_URL, SONARR_2_URL, etc.)
        for i in range(1, 11):  # Support up to 10 instances
            url = os.environ.get(f"{prefix}_{i}_URL")
            apikey = os.environ.get(f"{prefix}_{i}_APIKEY")
            if url and apikey:
                path_mappings = []
                if os.environ.get(f"{prefix}_{i}_PATH_MAP"):
                    for mapping in os.environ[f"{prefix}_{i}_PATH_MAP"].split(";"):
                        mapping = mapping.strip()
                        if ":" in mapping:
                            parts = mapping.split(":", 1)
                            path_mappings.append({
                                "servarr_path": parts[0],
                                "local_path": parts[1]
                            })
                
                self._settings[instances_key].append({
                    "enabled": True,
                    "name": os.environ.get(f"{prefix}_{i}_NAME", f"{app_type.lower()}-{i}"),
                    "url": url,
                    "api_key": apikey,
                    "path_mappings": path_mappings,
                })
    
    def _parse_path_mappings(self, mappings: list) -> list:
        """Parse path mappings from various formats."""
        result = []
        for m in mappings:
            if isinstance(m, dict):
                result.append(m)
            elif isinstance(m, str) and ":" in m:
                parts = m.split(":", 1)
                result.append({"servarr_path": parts[0], "local_path": parts[1]})
        return result
    
    def _save_settings(self) -> None:
        """Save settings to file."""
        with self._lock:
            try:
                self.config_dir.mkdir(parents=True, exist_ok=True)
                
                # Create a copy without sensitive data exposed in logs
                with open(self.config_file, "w", encoding="utf-8") as f:
                    json.dump(self._settings, f, indent=2)
                
                LOG.info(f"Settings saved to {self.config_file}")
            except Exception as e:
                LOG.error(f"Failed to save settings: {e}")
    
    # =========================================================================
    # PUBLIC API
    # =========================================================================
    
    def get_all(self) -> Dict[str, Any]:
        """Get all settings (with API keys masked for display)."""
        with self._lock:
            result = deepcopy(self._settings)
            # Mask sensitive values for API response
            self._mask_sensitive(result)
            return result
    
    def get_all_raw(self) -> Dict[str, Any]:
        """Get all settings including sensitive values (for internal use)."""
        with self._lock:
            return deepcopy(self._settings)
    
    def _mask_sensitive(self, settings: dict) -> None:
        """Mask sensitive values like passwords and API keys."""
        # Mask qBittorrent password
        if settings.get("qbittorrent", {}).get("password"):
            settings["qbittorrent"]["password"] = "********"
        
        # Mask web password
        if settings.get("web", {}).get("password"):
            settings["web"]["password"] = "********"
        
        # Mask Sonarr API keys
        for inst in settings.get("sonarr_instances", []):
            if inst.get("api_key"):
                inst["api_key"] = inst["api_key"][:4] + "****" + inst["api_key"][-4:] if len(inst["api_key"]) > 8 else "********"
        
        # Mask Radarr API keys
        for inst in settings.get("radarr_instances", []):
            if inst.get("api_key"):
                inst["api_key"] = inst["api_key"][:4] + "****" + inst["api_key"][-4:] if len(inst["api_key"]) > 8 else "********"
    
    def get(self, section: str, key: Optional[str] = None) -> Any:
        """Get a specific setting value."""
        with self._lock:
            if key is None:
                return deepcopy(self._settings.get(section, {}))
            return deepcopy(self._settings.get(section, {}).get(key))
    
    def update(self, section: str, data: Dict[str, Any]) -> bool:
        """Update settings for a section."""
        with self._lock:
            if section not in self._settings:
                LOG.error(f"Unknown settings section: {section}")
                return False
            
            # Handle special case for passwords - don't overwrite with masked values
            if section == "qbittorrent" and data.get("password") == "********":
                data["password"] = self._settings["qbittorrent"].get("password", "")
            if section == "web" and data.get("password") == "********":
                data["password"] = self._settings["web"].get("password", "")
            
            if isinstance(self._settings[section], list):
                self._settings[section] = data
            else:
                self._settings[section].update(data)
            
            self._save_settings()
            return True
    
    def add_instance(self, app_type: str, instance: Dict[str, Any]) -> bool:
        """Add a new Sonarr/Radarr instance."""
        with self._lock:
            key = f"{app_type}_instances"
            if key not in self._settings:
                return False
            
            # Ensure required fields
            if not instance.get("name") or not instance.get("url") or not instance.get("api_key"):
                return False
            
            # Default enabled to True
            if "enabled" not in instance:
                instance["enabled"] = True
            if "path_mappings" not in instance:
                instance["path_mappings"] = []
            
            self._settings[key].append(instance)
            self._save_settings()
            return True
    
    def update_instance(self, app_type: str, index: int, instance: Dict[str, Any]) -> bool:
        """Update an existing Sonarr/Radarr instance."""
        with self._lock:
            key = f"{app_type}_instances"
            if key not in self._settings:
                return False
            
            if index < 0 or index >= len(self._settings[key]):
                return False
            
            # Handle masked API key
            if instance.get("api_key", "").endswith("****"):
                instance["api_key"] = self._settings[key][index].get("api_key", "")
            
            self._settings[key][index] = instance
            self._save_settings()
            return True
    
    def remove_instance(self, app_type: str, index: int) -> bool:
        """Remove a Sonarr/Radarr instance."""
        with self._lock:
            key = f"{app_type}_instances"
            if key not in self._settings:
                return False
            
            if index < 0 or index >= len(self._settings[key]):
                return False
            
            del self._settings[key][index]
            self._save_settings()
            return True
    
    def test_connection(self, app_type: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """Test connection to a Sonarr/Radarr/qBittorrent instance."""
        import urllib.request
        import urllib.error
        import ssl
        
        result = {"success": False, "message": "", "details": {}}
        
        try:
            if app_type in ("sonarr", "radarr"):
                url = config.get("url", "").rstrip("/")
                api_key = config.get("api_key", "")
                
                if not url or not api_key:
                    result["message"] = "URL and API key are required"
                    return result
                
                # Test /api/v3/system/status
                test_url = f"{url}/api/v3/system/status"
                req = urllib.request.Request(test_url)
                req.add_header("X-Api-Key", api_key)
                req.add_header("Accept", "application/json")
                
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                
                with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                    data = json.loads(resp.read().decode())
                    result["success"] = True
                    result["message"] = f"Connected to {app_type.title()}"
                    result["details"] = {
                        "version": data.get("version", "unknown"),
                        "appName": data.get("appName", app_type.title()),
                    }
            
            elif app_type == "qbittorrent":
                host = config.get("host", "")
                port = config.get("port", 8080)
                
                if not host:
                    result["message"] = "Host is required"
                    return result
                
                # Test qBittorrent API
                test_url = f"http://{host}:{port}/api/v2/app/version"
                req = urllib.request.Request(test_url)
                
                with urllib.request.urlopen(req, timeout=10) as resp:
                    version = resp.read().decode().strip()
                    result["success"] = True
                    result["message"] = "Connected to qBittorrent"
                    result["details"] = {"version": version}
            
            else:
                result["message"] = f"Unknown app type: {app_type}"
        
        except urllib.error.HTTPError as e:
            result["message"] = f"HTTP Error: {e.code} {e.reason}"
        except urllib.error.URLError as e:
            result["message"] = f"Connection failed: {e.reason}"
        except Exception as e:
            result["message"] = f"Error: {str(e)}"
        
        return result


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================

_settings_manager: Optional[SettingsManager] = None


def get_settings_manager(config_dir: str = "/config") -> SettingsManager:
    """Get or create the global settings manager instance."""
    global _settings_manager
    if _settings_manager is None:
        _settings_manager = SettingsManager(config_dir)
    return _settings_manager
