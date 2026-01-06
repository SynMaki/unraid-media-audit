#!/usr/bin/env python3
"""
Unit tests for servarr_client.py

Tests path mapping, instance parsing, and protection logic.
"""

import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from servarr_client import (
    PathMapping,
    ServarrInstance,
    ServarrType,
    QualityProfile,
    ManagedFile,
    ServarrManager,
    parse_instances_from_env,
    parse_instance_from_cli_arg,
)


class TestPathMapping(unittest.TestCase):
    """Test path mapping between Servarr and local paths."""
    
    def test_to_local_basic(self):
        """Test basic path conversion to local."""
        pm = PathMapping("/tv", "/media/plexmedia/Serien")
        result = pm.to_local("/tv/Show Name/Season 01/episode.mkv")
        self.assertEqual(result, "/media/plexmedia/Serien/Show Name/Season 01/episode.mkv")
    
    def test_to_local_no_match(self):
        """Test path conversion when no match."""
        pm = PathMapping("/tv", "/media/plexmedia/Serien")
        result = pm.to_local("/movies/Movie Name/movie.mkv")
        self.assertEqual(result, "/movies/Movie Name/movie.mkv")
    
    def test_to_servarr_basic(self):
        """Test basic path conversion to Servarr."""
        pm = PathMapping("/tv", "/media/plexmedia/Serien")
        result = pm.to_servarr("/media/plexmedia/Serien/Show Name/episode.mkv")
        self.assertEqual(result, "/tv/Show Name/episode.mkv")
    
    def test_to_servarr_no_match(self):
        """Test path conversion when no match."""
        pm = PathMapping("/tv", "/media/plexmedia/Serien")
        result = pm.to_servarr("/other/path/file.mkv")
        self.assertEqual(result, "/other/path/file.mkv")


class TestServarrInstance(unittest.TestCase):
    """Test Servarr instance configuration."""
    
    def test_from_dict_basic(self):
        """Test creating instance from dict."""
        config = {
            "name": "main",
            "url": "http://localhost:8989",
            "api_key": "testkey123",
            "type": "sonarr",
        }
        instance = ServarrInstance.from_dict(config)
        
        self.assertEqual(instance.name, "main")
        self.assertEqual(instance.url, "http://localhost:8989")
        self.assertEqual(instance.api_key, "testkey123")
        self.assertEqual(instance.app_type, ServarrType.SONARR)
    
    def test_from_dict_with_path_mappings(self):
        """Test creating instance with path mappings."""
        config = {
            "name": "main",
            "url": "http://localhost:8989",
            "api_key": "testkey123",
            "type": "sonarr",
            "path_mappings": [
                {"servarr_path": "/tv", "local_path": "/media/Serien"},
                "/anime:/media/Anime"  # String format
            ]
        }
        instance = ServarrInstance.from_dict(config)
        
        self.assertEqual(len(instance.path_mappings), 2)
        self.assertEqual(instance.path_mappings[0].servarr_path, "/tv")
        self.assertEqual(instance.path_mappings[0].local_path, "/media/Serien")
        self.assertEqual(instance.path_mappings[1].servarr_path, "/anime")
        self.assertEqual(instance.path_mappings[1].local_path, "/media/Anime")
    
    def test_url_normalization(self):
        """Test URL normalization."""
        # With trailing slash
        instance = ServarrInstance(
            name="test", url="http://localhost:8989/", api_key="key",
            app_type=ServarrType.SONARR
        )
        self.assertEqual(instance.url, "http://localhost:8989")
        
        # Without http
        instance = ServarrInstance(
            name="test", url="localhost:8989", api_key="key",
            app_type=ServarrType.SONARR
        )
        self.assertEqual(instance.url, "http://localhost:8989")
    
    def test_map_path_to_local(self):
        """Test path mapping to local filesystem."""
        instance = ServarrInstance(
            name="test", url="http://localhost:8989", api_key="key",
            app_type=ServarrType.SONARR,
            path_mappings=[
                PathMapping("/tv", "/media/Serien"),
                PathMapping("/anime", "/media/Anime"),
            ]
        )
        
        result = instance.map_path_to_local("/tv/Show/episode.mkv")
        self.assertEqual(result, "/media/Serien/Show/episode.mkv")
        
        result = instance.map_path_to_local("/anime/Show/episode.mkv")
        self.assertEqual(result, "/media/Anime/Show/episode.mkv")
        
        result = instance.map_path_to_local("/unknown/path.mkv")
        self.assertEqual(result, "/unknown/path.mkv")
    
    def test_get_webui_link_sonarr(self):
        """Test WebUI link generation for Sonarr."""
        instance = ServarrInstance(
            name="test", url="http://localhost:8989", api_key="key",
            app_type=ServarrType.SONARR
        )
        link = instance.get_webui_link(123)
        self.assertEqual(link, "http://localhost:8989/series/123")
    
    def test_get_webui_link_radarr(self):
        """Test WebUI link generation for Radarr."""
        instance = ServarrInstance(
            name="test", url="http://localhost:7878", api_key="key",
            app_type=ServarrType.RADARR
        )
        link = instance.get_webui_link(456)
        self.assertEqual(link, "http://localhost:7878/movie/456")


class TestParseInstancesFromCLI(unittest.TestCase):
    """Test CLI argument parsing."""
    
    def test_parse_single_arg(self):
        """Test parsing single CLI argument."""
        result = parse_instance_from_cli_arg(
            "name=main,url=http://localhost:8989,apikey=testkey123",
            ServarrType.SONARR
        )
        
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "main")
        self.assertEqual(result.url, "http://localhost:8989")
        self.assertEqual(result.api_key, "testkey123")
        self.assertEqual(result.app_type, ServarrType.SONARR)
    
    def test_parse_with_path_map(self):
        """Test parsing CLI argument with path mapping."""
        result = parse_instance_from_cli_arg(
            "name=main,url=http://localhost:8989,apikey=key,path_map=/tv:/media/Serien",
            ServarrType.SONARR
        )
        
        self.assertIsNotNone(result)
        self.assertEqual(len(result.path_mappings), 1)
        self.assertEqual(result.path_mappings[0].servarr_path, "/tv")
        self.assertEqual(result.path_mappings[0].local_path, "/media/Serien")
    
    def test_parse_invalid_no_url(self):
        """Test parsing fails without URL."""
        result = parse_instance_from_cli_arg("name=main,apikey=key", ServarrType.SONARR)
        self.assertIsNone(result)
    
    def test_parse_invalid_no_key(self):
        """Test parsing fails without API key."""
        result = parse_instance_from_cli_arg("name=main,url=http://localhost:8989", ServarrType.SONARR)
        self.assertIsNone(result)


class TestParseInstancesFromEnv(unittest.TestCase):
    """Test environment variable parsing."""
    
    def test_parse_single_instance_env(self):
        """Test parsing single instance from env."""
        with patch.dict(os.environ, {
            "SONARR_URL": "http://localhost:8989",
            "SONARR_APIKEY": "testkey123",
        }, clear=False):
            instances = parse_instances_from_env(ServarrType.SONARR)
        
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].url, "http://localhost:8989")
        self.assertEqual(instances[0].api_key, "testkey123")
    
    def test_parse_numbered_instances(self):
        """Test parsing numbered instances from env."""
        with patch.dict(os.environ, {
            "SONARR_1_URL": "http://sonarr1:8989",
            "SONARR_1_APIKEY": "key1",
            "SONARR_1_NAME": "main",
            "SONARR_2_URL": "http://sonarr2:8989",
            "SONARR_2_APIKEY": "key2",
            "SONARR_2_NAME": "anime",
        }, clear=False):
            instances = parse_instances_from_env(ServarrType.SONARR)
        
        self.assertGreaterEqual(len(instances), 2)
    
    def test_parse_json_instances(self):
        """Test parsing JSON instances from env."""
        json_config = json.dumps([
            {"name": "main", "url": "http://sonarr:8989", "api_key": "key1"},
            {"name": "anime", "url": "http://sonarr-anime:8989", "api_key": "key2"},
        ])
        
        with patch.dict(os.environ, {
            "SONARR_INSTANCES_JSON": json_config,
        }, clear=False):
            instances = parse_instances_from_env(ServarrType.SONARR)
        
        self.assertEqual(len(instances), 2)


class TestQualityProfile(unittest.TestCase):
    """Test quality profile parsing."""
    
    def test_from_dict_basic(self):
        """Test creating quality profile from dict."""
        data = {
            "id": 1,
            "name": "HD-1080p",
            "upgradeAllowed": True,
            "cutoff": 7,
            "cutoffFormatScore": 10000,
            "minFormatScore": 0,
            "minUpgradeFormatScore": 1,
        }
        qp = QualityProfile.from_dict(data)
        
        self.assertEqual(qp.id, 1)
        self.assertEqual(qp.name, "HD-1080p")
        self.assertTrue(qp.upgrade_allowed)
        self.assertEqual(qp.cutoff, 7)
        self.assertEqual(qp.cutoff_format_score, 10000)
    
    def test_from_dict_defaults(self):
        """Test quality profile defaults."""
        data = {"id": 1, "name": "Basic"}
        qp = QualityProfile.from_dict(data)
        
        self.assertEqual(qp.id, 1)
        self.assertEqual(qp.name, "Basic")
        self.assertTrue(qp.upgrade_allowed)  # Default
        self.assertEqual(qp.cutoff_format_score, 0)


class TestServarrManager(unittest.TestCase):
    """Test Servarr manager functionality."""
    
    def test_is_managed_empty(self):
        """Test is_managed returns False when no files loaded."""
        manager = ServarrManager()
        self.assertFalse(manager.is_managed("/some/path.mkv"))
    
    def test_is_in_queue_empty(self):
        """Test is_in_queue returns False when no queue loaded."""
        manager = ServarrManager()
        self.assertFalse(manager.is_in_queue("/some/path.mkv"))
    
    def test_get_file_info_empty(self):
        """Test get_file_info returns None when no files loaded."""
        manager = ServarrManager()
        self.assertIsNone(manager.get_file_info("/some/path.mkv"))


class TestManagedFile(unittest.TestCase):
    """Test ManagedFile dataclass."""
    
    def test_basic_creation(self):
        """Test creating a managed file."""
        mf = ManagedFile(
            file_id=1,
            path="/tv/Show/episode.mkv",
            local_path="/media/Serien/Show/episode.mkv",
            size=1000000000,
            quality="Bluray-1080p",
            quality_id=7,
            custom_format_score=50,
            custom_formats=["x264", "DTS"],
            quality_cutoff_not_met=False,
            media_id=123,
            media_title="Test Show",
        )
        
        self.assertEqual(mf.file_id, 1)
        self.assertEqual(mf.quality, "Bluray-1080p")
        self.assertEqual(mf.custom_format_score, 50)
        self.assertEqual(mf.custom_formats, ["x264", "DTS"])
        self.assertFalse(mf.upgrade_recommended)


if __name__ == "__main__":
    unittest.main()
