import json
import os
import tempfile
import unittest
from unittest.mock import patch

from orbit import __version__

_SERVER_DATA = tempfile.TemporaryDirectory()
os.environ["ORBIT_DATA_DIR"] = _SERVER_DATA.name
from orbit import server


def tearDownModule():
    _SERVER_DATA.cleanup()


class ServerSettingsTests(unittest.TestCase):
    def test_health_version_uses_package_release(self):
        self.assertEqual(server.VERSION, __version__)

    def test_scraper_settings_are_written_to_legacy_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(server, "LEGACY_CONFIG", directory):
                server._sync_legacy_settings({
                    "debrid_mode": "webdav",
                    "torbox_api_key": "torbox-token",
                    "plex_token": "plex-token",
                    "plex_username": "Orbit",
                    "plex_url": "http://plex:32400",
                    "plex_sections": "4,5",
                    "scraper_torrentio": "true",
                    "scraper_prowlarr": "true",
                    "scraper_jackett": "false",
                    "scraper_orionoid": "false",
                    "scraper_nyaa": "true",
                    "scraper_1337x": "false",
                    "torrentio_url": "https://torrentio.example/manifest.json",
                    "prowlarr_url": "http://prowlarr:9696",
                    "prowlarr_api_key": "prowlarr-token",
                })
            with open(os.path.join(directory, "settings.json"), encoding="utf-8") as handle:
                legacy = json.load(handle)

        self.assertEqual(legacy["Sources"], ["torrentio", "prowlarr", "nyaa"])
        self.assertEqual(
            legacy["Torrentio Scraper Parameters"],
            "https://torrentio.example/manifest.json",
        )
        self.assertEqual(legacy["Prowlarr Base URL"], "http://prowlarr:9696")
        self.assertEqual(legacy["Prowlarr API Key"], "prowlarr-token")
        self.assertEqual(legacy["Library update services"], [])
        self.assertEqual(legacy["Symlinker Mount Path"], "/zeroq-media")

    def test_symlinker_uses_the_configured_plex_visible_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(server, "LEGACY_CONFIG", directory), patch.dict(
                os.environ,
                {
                    "PD_DOWNLOADS_DIR": "/shared-media",
                    "ORBIT_MOVIES_DIR": "/shared-media/vortexo/Movies",
                    "ORBIT_TV_DIR": "/shared-media/vortexo/TV",
                },
            ):
                server._sync_legacy_settings({})
            with open(os.path.join(directory, "settings.json"), encoding="utf-8") as handle:
                legacy = json.load(handle)

        self.assertEqual(legacy["Symlinker Mount Path"], "/shared-media")
        self.assertEqual(
            legacy["Symlinker Movies Library"],
            "/shared-media/vortexo/Movies",
        )
        self.assertEqual(
            legacy["Symlinker TV Library"],
            "/shared-media/vortexo/TV",
        )


if __name__ == "__main__":
    unittest.main()
