import os
import tempfile
import unittest
from unittest import mock

from vortexo.integrations import IntegrationError
from vortexo.service import VortexoService


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = self.temporary.name
        self.data = os.path.join(root, "data")
        self.source = os.path.join(root, "source")
        self.movies = os.path.join(root, "library", "Movies")
        self.tv = os.path.join(root, "library", "TV")
        self.preferences = os.path.join(root, "Preferences.xml")
        os.makedirs(self.source)
        os.makedirs(self.movies)
        os.makedirs(self.tv)
        with open(self.preferences, "w", encoding="utf-8") as handle:
            handle.write('<Preferences PlexOnlineToken="owner-token"/>')
        self.environment = mock.patch.dict(
            os.environ,
            {
                "VORTEXO_DATA_DIR": self.data,
                "VORTEXO_SOURCE_ROOT": self.source,
                "VORTEXO_MOVIES_ROOT": self.movies,
                "VORTEXO_TV_ROOT": self.tv,
                "VORTEXO_PLEX_PREFERENCES": self.preferences,
                "VORTEXO_DISABLE_AUTOMATION": "1",
            },
        )
        self.environment.start()
        self.service = VortexoService()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def _source_file(self, name="Memento.2000.1080p.mkv"):
        path = os.path.join(self.source, name)
        with open(path, "wb") as handle:
            handle.write(b"video")
        return path

    def test_owner_session_requires_exact_local_token(self):
        with mock.patch(
            "vortexo.service.plex_account",
            side_effect=[
                {"id": "owner", "uuid": "", "email": ""},
                {"id": "someone-else", "uuid": "", "email": ""},
            ],
        ):
            with self.assertRaises(PermissionError):
                self.service.establish_session("not-owner")
        session = self.service.establish_session("owner-token")
        self.assertTrue(self.service.valid_session(session))

    def test_distinct_plex_web_token_is_accepted_for_same_owner_account(self):
        with mock.patch(
            "vortexo.service.plex_account",
            side_effect=[
                {"id": "owner-account", "uuid": "", "email": ""},
                {"id": "owner-account", "uuid": "", "email": ""},
            ],
        ):
            session = self.service.establish_session("owner-web-token")
        self.assertTrue(self.service.valid_session(session))

    def test_movie_link_preserves_versions_and_is_idempotent(self):
        source = self._source_file()
        media = {"type": "movie", "title": "Memento", "year": 2000}
        first, existed = self.service._link_media(
            media, {"quality": "1080p", "info_hash": "abcdef123"}, source
        )
        self.assertFalse(existed)
        self.assertTrue(os.path.islink(first))
        repeated, existed = self.service._link_media(
            media, {"quality": "1080p", "info_hash": "abcdef123"}, source
        )
        self.assertTrue(existed)
        self.assertEqual(first, repeated)

        second_source = self._source_file("Memento.2000.2160p.mkv")
        second, existed = self.service._link_media(
            media, {"quality": "4K", "info_hash": "different987"}, second_source
        )
        self.assertFalse(existed)
        self.assertNotEqual(first, second)
        self.assertTrue(os.path.lexists(first))

    def test_library_job_fails_before_mount_or_link_when_release_title_is_wrong(self):
        media = {
            "discover_id": "spider-man",
            "type": "movie",
            "title": "Spider-Man Brand New Day",
            "year": 2026,
        }
        stream = {
            "info_hash": "wrong-release",
            "file_name": "Spider-Man.Brand.New.Day.2026.mkv",
        }
        job, _ = self.service.store.create_or_get_job(
            "spider-man|wrong-release",
            media["discover_id"],
            "wrong-stream",
            {"media": media, "stream": stream},
        )
        torrent = {
            "id": 42,
            "name": "Marvel Studios Iron Man 2008 1080p WEB-DL",
            "files": [
                {
                    "id": 0,
                    "name": "Marvel Studios Iron Man 2008 1080p WEB-DL.mkv",
                    "size": 1000,
                }
            ],
        }
        with mock.patch("vortexo.service.TorBoxClient") as client_type:
            client_type.return_value.find_torrent.return_value = torrent
            with mock.patch.object(
                self.service, "_wait_for_torrent", return_value=torrent
            ):
                with mock.patch.object(self.service, "_wait_for_mount_file") as mount:
                    with mock.patch.object(self.service, "_link_media") as link:
                        self.service._run_library_job(job["id"])

        failed = self.service.store.job(job["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("does not match", failed["detail"])
        mount.assert_not_called()
        link.assert_not_called()

    def test_episode_link_uses_exact_season_episode(self):
        source = self._source_file("Show.S02E04.mkv")
        linked, _ = self.service._link_media(
            {
                "type": "episode",
                "title": "Episode",
                "parent_title": "Show",
                "season": 2,
                "episode": 4,
            },
            {"quality": "4K", "info_hash": "episodehash"},
            source,
        )
        self.assertIn(os.path.join("Show", "Season 02"), linked)
        self.assertIn("S02E04", os.path.basename(linked))

    def test_refuses_source_outside_torbox_mount(self):
        outside = os.path.join(self.temporary.name, "outside.mkv")
        with open(outside, "wb") as handle:
            handle.write(b"video")
        with self.assertRaisesRegex(IntegrationError, "outside"):
            self.service._link_media(
                {"type": "movie", "title": "Unsafe"},
                {"quality": "4K", "info_hash": "abc"},
                outside,
            )

    def test_player_session_returns_local_url_and_never_raw_source(self):
        public = self.service.store.save_streams(
            "discover",
            [
                {
                    "url": "https://signed.example/private.mp4",
                    "file_name": "Movie.mp4",
                    "codec": "H.264",
                    "audio": "AAC",
                    "can_play_now": True,
                }
            ],
        )[0]
        response = self.service.create_play_session(
            {"discover_id": "discover", "stream_id": public["id"]}
        )
        self.assertEqual(response["mode"], "direct")
        self.assertTrue(response["play_url"].startswith("/vortexo/play/"))
        self.assertNotIn("signed.example", str(response))

    def test_progress_marks_complete_at_ninety_percent(self):
        with mock.patch.object(self.service, "_mark_discover_watched") as watched:
            with mock.patch.object(
                self.service, "_job_rating_key_for_discover", return_value=""
            ):
                saved = self.service.save_progress(
                    {"discover_id": "discover", "position_ms": 90, "duration_ms": 100}
                )
        self.assertTrue(saved["completed"])
        watched.assert_called_once_with("discover")

    def test_invalid_torbox_key_is_not_persisted(self):
        with mock.patch(
            "vortexo.service.TorBoxClient.health",
            side_effect=IntegrationError("TorBox rejected the API key"),
        ):
            with self.assertRaisesRegex(IntegrationError, "rejected"):
                self.service.update_settings({"torbox_api_key": "invalid-secret"})
        self.assertNotIn("torbox_api_key", self.service.store.settings())

    def test_status_reports_component_health_without_secrets(self):
        self.service.store.update_settings(
            {
                "torbox_api_key": "private-key",
                "stream_manifest_urls": ["https://sources.example/manifest.json"],
            }
        )

        def json_response(url, **_kwargs):
            if url.endswith("/health"):
                return {"online": True, "detail": "Mount online"}
            if "sources.example" in url:
                return {"resources": [{"name": "stream", "types": ["movie"]}]}
            raise AssertionError(url)

        response = mock.MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with mock.patch("vortexo.service.json_request", side_effect=json_response):
            with mock.patch("urllib.request.urlopen", return_value=response) as open_url:
                with mock.patch(
                    "vortexo.service.TorBoxClient.health",
                    return_value={"online": True, "detail": "Connected"},
                ):
                    status = self.service.public_status()
        self.assertTrue(status["plex"]["online"])
        self.assertTrue(status["torbox"]["online"])
        self.assertTrue(status["source_lookup"]["online"])
        self.assertTrue(status["mount"]["online"])
        self.assertNotIn("private-key", str(status))
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("X-plex-token"), "owner-token")
        self.assertIsNone(request.get_header("X-plex-product"))
        self.assertIsNone(request.get_header("X-plex-version"))
        self.assertIsNone(request.get_header("X-plex-platform"))

    def test_plex_confirmation_requires_exact_linked_episode_version(self):
        def response(url, **_kwargs):
            if url.endswith("/allLeaves"):
                return {
                    "MediaContainer": {
                        "Metadata": [
                            {
                                "ratingKey": "episode-key",
                                "parentIndex": 2,
                                "index": 4,
                            }
                        ]
                    }
                }
            if url.endswith("/library/metadata/episode-key"):
                return {
                    "MediaContainer": {
                        "Metadata": [
                            {
                                "Media": [
                                    {
                                        "Part": [
                                            {
                                                "file": (
                                                    "/downloads/vortexo/TV/Show/Season 02/"
                                                    "Show - S02E04 - 4K.mkv"
                                                )
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                }
            raise AssertionError(url)

        with mock.patch("vortexo.service.json_request", side_effect=response):
            episode_key = self.service._episode_rating_key("show-key", 2, 4)
            self.assertEqual(episode_key, "episode-key")
            self.assertTrue(
                self.service._plex_item_contains_file(
                    episode_key,
                    "/downloads/vortexo/TV/Show/Season 02/Show - S02E04 - 4K.mkv",
                )
            )

    def test_watchlist_sync_queues_one_cached_release_idempotently(self):
        self.service.store.update_settings(
            {
                "torbox_api_key": "private-key",
                "stream_manifest_urls": ["https://sources.example/manifest.json"],
                "plex_watchlist_enabled": True,
                "plex_watchlist_profile": "best",
                "plex_watchlist_cached_only": True,
            }
        )
        media = {
            "discover_id": "movie-discover",
            "type": "movie",
            "title": "Memento",
            "year": 2000,
            "tmdb_id": 77,
            "imdb_id": "tt0209144",
        }
        stream = {
            "info_hash": "cached-release",
            "magnet": "magnet:?xt=urn:btih:cached-release",
            "quality": "4K",
            "size_gb": 50,
            "cached": True,
            "can_add": True,
            "file_name": "Memento.2000.2160p.mkv",
        }
        with mock.patch(
            "vortexo.service.fetch_plex_watchlist", return_value=[media]
        ):
            with mock.patch.object(self.service, "_plex_has_media", return_value=False):
                with mock.patch.object(
                    self.service, "_lookup_streams", return_value=([stream], [])
                ):
                    with mock.patch.object(self.service, "_start_library_job"):
                        first = self.service.sync_watchlist()
                        second = self.service.sync_watchlist()
        self.assertEqual(first["queued"], 1)
        self.assertEqual(second["queued"], 0)
        self.assertEqual(second["skipped_active"], 1)
        item = self.service.store.watchlist_item("movie:77")
        self.assertEqual(item["status"], "selected")
        payload = self.service.store.job_payload(item["job_id"])
        self.assertEqual(payload["source"], "plex_watchlist")
        self.assertNotIn("private-key", str(self.service.watchlist_public()))

    def test_watchlist_skips_titles_already_in_plex(self):
        self.service.store.update_settings(
            {
                "torbox_api_key": "private-key",
                "stream_manifest_urls": ["https://sources.example/manifest.json"],
                "plex_watchlist_enabled": True,
            }
        )
        media = {
            "discover_id": "movie-discover",
            "type": "movie",
            "title": "Memento",
            "tmdb_id": 77,
            "imdb_id": "tt0209144",
        }
        with mock.patch(
            "vortexo.service.fetch_plex_watchlist", return_value=[media]
        ):
            with mock.patch.object(self.service, "_plex_has_media", return_value=True):
                with mock.patch.object(self.service, "_lookup_streams") as lookup:
                    result = self.service.sync_watchlist()
        self.assertEqual(result["skipped_existing"], 1)
        self.assertEqual(result["queued"], 0)
        lookup.assert_not_called()
        self.assertEqual(
            self.service.store.watchlist_item("movie:77")["status"],
            "already_in_plex",
        )

    def test_watchlist_auto_upgrade_adds_only_a_safer_higher_quality_version(self):
        self.service.store.update_settings(
            {
                "torbox_api_key": "private-key",
                "stream_manifest_urls": ["https://sources.example/manifest.json"],
                "plex_watchlist_enabled": True,
                "plex_watchlist_profile": "best",
                "plex_watchlist_cached_only": True,
                "plex_watchlist_auto_upgrade": True,
            }
        )
        media = {
            "discover_id": "movie-discover",
            "type": "movie",
            "title": "Memento",
            "year": 2000,
            "tmdb_id": 77,
            "imdb_id": "tt0209144",
        }
        source = self._source_file()
        self.service._link_media(
            media,
            {"quality": "1080p", "info_hash": "existing-1080p"},
            source,
        )
        upgrade = {
            "info_hash": "safe-4k",
            "magnet": "magnet:?xt=urn:btih:safe-4k",
            "quality": "4K",
            "size_gb": 20,
            "cached": True,
            "can_add": True,
            "file_name": "Memento.2000.2160p.WEB-DL.mkv",
        }
        with mock.patch(
            "vortexo.service.fetch_plex_watchlist", return_value=[media]
        ):
            with mock.patch.object(self.service, "_plex_has_media", return_value=True):
                with mock.patch.object(
                    self.service, "_lookup_streams", return_value=([upgrade], [])
                ):
                    with mock.patch.object(self.service, "_start_library_job"):
                        result = self.service.sync_watchlist()
        self.assertEqual(result["queued"], 1)
        item = self.service.store.watchlist_item("movie:77")
        payload = self.service.store.job_payload(item["job_id"])
        self.assertEqual(payload["source"], "plex_watchlist_upgrade")
        self.assertEqual(payload["stream"]["quality"], "4K")

    def test_watchlist_show_safely_targets_first_regular_episode(self):
        show = {
            "discover_id": "show-discover",
            "type": "show",
            "title": "Severance",
            "tmdb_id": 95396,
            "imdb_id": "tt11280740",
        }
        episodes = [
            {
                "discover_id": "special",
                "type": "episode",
                "title": "Special",
                "season": 0,
                "episode": 1,
            },
            {
                "discover_id": "episode-one",
                "type": "episode",
                "title": "Good News About Hell",
                "season": 1,
                "episode": 1,
            },
        ]
        with mock.patch.object(self.service, "episodes", return_value=episodes):
            lookup, target, season, episode = self.service._watchlist_target(show)
        self.assertEqual(lookup["discover_id"], "show-discover")
        self.assertEqual(target["discover_id"], "episode-one")
        self.assertEqual(target["parent_title"], "Severance")
        self.assertEqual((season, episode), (1, 1))


if __name__ == "__main__":
    unittest.main()
