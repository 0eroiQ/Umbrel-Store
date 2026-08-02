import os
import tempfile
import unittest
from unittest.mock import patch

from orbit.store import Store
from orbit.worker import Coordinator


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.temp.name, "orbit.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_secrets_are_masked_but_preserved(self):
        self.store.set_settings({"tmdb_api_key": "secret", "list_poll_minutes": "60"}, {"tmdb_api_key"})
        self.assertEqual(self.store.get_settings()["tmdb_api_key"], "••••••••")
        self.store.set_settings({"tmdb_api_key": "••••••••"}, {"tmdb_api_key"})
        self.assertEqual(self.store.get_settings(True)["tmdb_api_key"], "secret")

    def test_overlapping_sources_do_not_duplicate_media(self):
        item = {"media_type": "movie", "title": "Dune", "tmdb_id": 438631, "year": 2021}
        first, created = self.store.add_request(item, source="manual")
        second, duplicated = self.store.add_request(item, source="mdblist", source_ref="1")
        self.assertTrue(created)
        self.assertFalse(duplicated)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.list_requests()), 1)

    def test_existing_watchlist_request_receives_plex_cloud_guid_for_retry(self):
        item = {
            "media_type": "show", "title": "Foundation",
            "tmdb_id": 93740, "year": 2021,
        }
        first, created = self.store.add_request(
            item, source="plex-watchlist", source_ref="plex-account"
        )
        enriched, duplicated = self.store.add_request({
            **item, "plex_guid": "plex://show/show-key",
        }, source="plex-watchlist", source_ref="plex-account")
        self.assertTrue(created)
        self.assertFalse(duplicated)
        self.assertEqual(first["id"], enriched["id"])
        self.assertEqual(enriched["plex_guid"], "plex://show/show-key")
        with tempfile.TemporaryDirectory() as output:
            path = os.path.join(output, "request.json")
            self.store.export_worker_request(enriched, path)
            with open(path, encoding="utf-8") as handle:
                self.assertIn('"plex_guid": "plex://show/show-key"', handle.read())

    def test_transition_records_visible_timeline(self):
        item, _ = self.store.add_request({"media_type": "show", "title": "Foundation", "tmdb_id": 93740})
        self.store.transition(item["id"], "searching", "Finding a cached release")
        self.store.transition(item["id"], "library_pending", "Waiting for mount")
        events = self.store.events(item["id"])
        self.assertEqual([event["state"] for event in events], ["queued", "searching", "library_pending"])

    def test_list_source_defaults_to_safe_import_only(self):
        source = self.store.add_list_source({
            "name": "New releases", "kind": "mdblist",
            "url": "https://mdblist.com/lists/example/new-releases",
        })
        self.assertEqual(source["mode"], "import_only")
        self.assertEqual(source["enabled"], 1)
        self.assertTrue(source["created"])

    def test_reconnecting_list_updates_existing_source(self):
        first = self.store.add_list_source({
            "name": "New releases", "kind": "mdblist",
            "url": "https://mdblist.com/lists/example/new-releases/",
            "profile": "best", "max_items": 100,
        })
        second = self.store.add_list_source({
            "name": "New Releases On Stremio", "kind": "mdblist",
            "url": "https://mdblist.com/lists/example/new-releases",
            "profile": "1080p", "max_items": 1000,
        })
        self.assertEqual(first["id"], second["id"])
        self.assertFalse(second["created"])
        self.assertEqual(second["name"], "New Releases On Stremio")
        self.assertEqual(second["profile"], "1080p")
        self.assertEqual(second["max_items"], 1000)
        self.assertEqual(len(self.store.list_sources()), 1)

    def test_library_link_promotes_request_to_ready(self):
        movies = os.path.join(self.temp.name, "Movies")
        television = os.path.join(self.temp.name, "TV")
        dune = os.path.join(movies, "Dune (2021) {tmdb-438631}")
        os.makedirs(dune)
        with open(os.path.join(dune, "Dune.mkv"), "wb") as handle:
            handle.write(b"video")
        os.makedirs(television)
        item, _ = self.store.add_request({
            "media_type": "movie", "title": "Dune", "tmdb_id": 438631, "year": 2021,
        })
        self.store.transition(item["id"], "library_pending", "Acquired")
        coordinator = Coordinator(self.store, self.temp.name)
        with patch.dict(os.environ, {"ORBIT_MOVIES_DIR": movies, "ORBIT_TV_DIR": television}), \
                patch.object(coordinator, "mount_is_healthy", return_value=True), \
                patch.object(coordinator, "refresh_plex_paths_if_healthy", return_value=[]):
            coordinator.verify_library_handoffs()
        self.assertEqual(self.store.list_requests()[0]["status"], "ready")

    def test_delayed_movie_link_recovers_previous_attention_state(self):
        movies = os.path.join(self.temp.name, "Movies")
        television = os.path.join(self.temp.name, "TV")
        cabin = os.path.join(movies, "Cabin Fever (2003) {tmdb-11547}")
        os.makedirs(cabin)
        os.makedirs(television)
        with open(os.path.join(cabin, "Cabin Fever.mkv"), "wb") as handle:
            handle.write(b"video")
        item, _ = self.store.add_request({
            "media_type": "movie", "title": "Cabin Fever",
            "tmdb_id": 11547, "year": 2003,
        })
        self.store.transition(
            item["id"], "needs_attention", "No suitable cached release was acquired"
        )
        coordinator = Coordinator(self.store, self.temp.name)
        with patch.dict(os.environ, {"ORBIT_MOVIES_DIR": movies, "ORBIT_TV_DIR": television}), \
                patch.object(coordinator, "mount_is_healthy", return_value=True), \
                patch.object(coordinator, "refresh_plex_paths_if_healthy", return_value=[{
                    "section_id": "1", "path": cabin,
                }]):
            coordinator.verify_library_handoffs()
        self.assertEqual(self.store.list_requests()[0]["status"], "ready")

    def test_pending_movie_and_series_scans_retry_and_clear_independently(self):
        movies = os.path.join(self.temp.name, "Movies")
        television = os.path.join(self.temp.name, "TV")
        dune = os.path.join(movies, "Dune (2021) {tmdb-438631}")
        silo = os.path.join(television, "Silo (2023) {tvdb-403245}")
        os.makedirs(dune)
        os.makedirs(silo)
        for folder in (dune, silo):
            with open(os.path.join(folder, ".plex-scan-pending"), "w", encoding="utf-8"):
                pass
        coordinator = Coordinator(self.store, self.temp.name)

        def refresh(paths):
            return [] if paths[0][0] == "movie" else [{"section_id": "2"}]

        with patch.dict(os.environ, {"ORBIT_MOVIES_DIR": movies, "ORBIT_TV_DIR": television}), \
                patch.object(coordinator, "refresh_plex_paths_if_healthy", side_effect=refresh):
            completed = coordinator.scan_pending_library_paths()

        self.assertEqual(completed, [("show", silo)])
        self.assertTrue(os.path.isfile(os.path.join(dune, ".plex-scan-pending")))
        self.assertFalse(os.path.exists(os.path.join(silo, ".plex-scan-pending")))

    def test_partial_scan_uses_plex_visible_root_and_matching_section_type(self):
        coordinator = Coordinator(self.store, self.temp.name)
        movies = os.path.join(self.temp.name, "local", "Movies")
        television = os.path.join(self.temp.name, "local", "TV")
        show_folder = os.path.join(television, "Silo (2023)")
        with patch.dict(os.environ, {
            "ORBIT_MOVIES_DIR": movies,
            "ORBIT_TV_DIR": television,
        }), patch.object(coordinator, "mount_is_healthy", return_value=True), \
                patch("orbit.worker.plex_library_sections", return_value=[
                    {
                        "section_id": "1", "media_type": "movie",
                        "locations": ["/zeroq-media/vortexo/Movies"],
                    },
                    {
                        "section_id": "2", "media_type": "show",
                        "locations": ["/zeroq-media/vortexo/TV"],
                    },
                ]), patch("orbit.worker.refresh_plex_paths", return_value=[{
                    "section_id": "2",
                    "path": "/zeroq-media/vortexo/TV/Silo (2023)",
                }]) as refresh:
            result = coordinator.refresh_plex_paths_if_healthy(
                [("show", show_folder)],
                {"plex_url": "http://plex", "plex_token": "token", "plex_sections": "1,2"},
            )
        self.assertEqual(result[0]["section_id"], "2")
        refresh.assert_called_once_with(
            "http://plex", "token",
            [("2", "/zeroq-media/vortexo/TV/Silo (2023)")],
        )

    def test_plex_watchlist_skips_owned_and_duplicate_titles(self):
        self.store.replace_plex_library([{
            "plex_rating_key": "101", "section_id": "4", "media_type": "movie",
            "title": "Dune", "year": 2021, "tmdb_id": 438631,
            "quality": "4K", "versions": [],
        }])
        self.store.add_request({
            "media_type": "show", "title": "Severance", "year": 2022,
            "tmdb_id": 95396,
        })
        watchlist = [
            {
                "media_type": "movie", "title": "Dune", "year": 2021,
                "tmdb_id": 438631,
            },
            {
                "media_type": "show", "title": "Severance", "year": 2022,
                "tmdb_id": 95396,
            },
            {
                "media_type": "movie", "title": "Arrival", "year": 2016,
                "tmdb_id": 329865,
            },
        ]
        coordinator = Coordinator(self.store, self.temp.name)
        with patch("orbit.worker.fetch_plex_watchlist", return_value=watchlist):
            result = coordinator.sync_plex_watchlist({
                "plex_watchlist_enabled": "true",
                "plex_watchlist_max_items": "100",
                "plex_watchlist_profile": "1080p",
                "plex_token": "token",
            })
        self.assertEqual(result, {
            "found": 3,
            "added": 1,
            "retried": 0,
            "skipped_existing": 1,
            "skipped_requested": 1,
        })
        arrival = next(item for item in self.store.list_requests() if item["title"] == "Arrival")
        self.assertEqual(arrival["source"], "plex-watchlist")
        self.assertEqual(arrival["profile"], "1080p")

    def test_manual_watchlist_sync_retries_failure_without_duplicating_active_retry(self):
        watchlist = [{
            "media_type": "movie", "title": "Arrival", "year": 2016,
            "tmdb_id": 329865,
        }]
        request, _ = self.store.add_request(
            watchlist[0], source="plex-watchlist", source_ref="plex-account"
        )
        self.store.transition(
            request["id"], "needs_attention",
            "The configured media library could not be read",
        )
        coordinator = Coordinator(self.store, self.temp.name)
        settings = {
            "plex_watchlist_enabled": "true",
            "plex_watchlist_max_items": "100",
            "plex_watchlist_profile": "4k",
            "plex_token": "token",
        }

        with patch("orbit.worker.fetch_plex_watchlist", return_value=watchlist):
            scheduled = coordinator.sync_plex_watchlist(settings)
            manual = coordinator.sync_plex_watchlist(settings, retry_failed=True)
            repeated = coordinator.sync_plex_watchlist(settings, retry_failed=True)

        self.assertEqual(scheduled["retried"], 0)
        self.assertEqual(scheduled["skipped_requested"], 1)
        self.assertEqual(manual["retried"], 1)
        self.assertEqual(manual["skipped_requested"], 0)
        self.assertEqual(repeated["retried"], 0)
        self.assertEqual(repeated["skipped_requested"], 1)
        current = self.store.list_requests()[0]
        self.assertEqual(current["status"], "queued")
        self.assertEqual(current["profile"], "4k")
        self.assertEqual(
            [event["state"] for event in self.store.events(request["id"])],
            ["queued", "needs_attention", "queued"],
        )

    def test_manual_watchlist_sync_does_not_retry_another_source(self):
        watchlist = [{
            "media_type": "show", "title": "Severance", "year": 2022,
            "tmdb_id": 95396,
        }]
        request, _ = self.store.add_request(watchlist[0], source="manual")
        self.store.transition(request["id"], "needs_attention", "No cached release")
        coordinator = Coordinator(self.store, self.temp.name)
        with patch("orbit.worker.fetch_plex_watchlist", return_value=watchlist):
            result = coordinator.sync_plex_watchlist({
                "plex_watchlist_enabled": "true",
                "plex_watchlist_max_items": "100",
                "plex_watchlist_profile": "best",
                "plex_token": "token",
            }, retry_failed=True)

        self.assertEqual(result["retried"], 0)
        self.assertEqual(result["skipped_requested"], 1)
        self.assertEqual(self.store.list_requests()[0]["status"], "needs_attention")

    def test_stream_protection_queues_missing_movie_without_scanning_offline_path(self):
        missing = os.path.join(self.temp.name, "Movies", "Missing.mkv")
        self.store.replace_plex_library([{
            "plex_rating_key": "111",
            "section_id": "4",
            "media_type": "movie",
            "title": "Missing",
            "year": 2024,
            "tmdb_id": 123456,
            "quality": "1080p",
            "versions": [{
                "resolution": "1080p",
                "file": missing,
                "available": False,
            }],
        }])
        coordinator = Coordinator(self.store, self.temp.name)
        with patch.object(coordinator, "mount_is_healthy", return_value=True), \
                patch("orbit.worker.repair_broken_symlinks", return_value={
                    "checked": 1,
                    "broken": 1,
                    "repaired": 0,
                    "remaining": [missing],
                    "error": "",
                }), \
                patch("orbit.worker.refresh_plex_paths", return_value=[]) as refresh:
            result = coordinator.repair_plex_streams({
                "torbox_api_key": "token",
                "plex_link_repair_max_per_run": "10",
                "plex_sections": "4,5",
            })
        self.assertEqual(result["queued"], 1)
        self.assertEqual(self.store.list_requests()[0]["source"], "library-replace")
        refresh.assert_not_called()

    def test_plex_library_is_replaceable_searchable_and_matchable(self):
        self.store.replace_plex_library([{
            "plex_rating_key": "101",
            "section_id": "4",
            "media_type": "movie",
            "title": "Dune",
            "year": 2021,
            "tmdb_id": 438631,
            "imdb_id": "tt1160419",
            "quality": "720p",
            "versions": [{"resolution": "720p", "dynamic_range": "SDR"}],
            "upgrade_available": True,
        }])
        results = self.store.list_plex_library("dun")
        self.assertEqual(results[0]["quality"], "720p")
        self.assertTrue(results[0]["upgrade_available"])
        match = self.store.match_plex_library({
            "media_type": "movie", "title": "Dune", "tmdb_id": 438631,
        })
        self.assertEqual(match["plex_rating_key"], "101")
        self.assertEqual(self.store.plex_library_status()["item_count"], 1)

    def test_failed_plex_refresh_preserves_last_inventory(self):
        self.store.replace_plex_library([{
            "plex_rating_key": "101", "section_id": "4", "media_type": "movie",
            "title": "Dune", "quality": "1080p", "versions": [],
        }])
        self.store.fail_plex_library_sync("Plex unavailable")
        self.assertEqual(len(self.store.list_plex_library()), 1)
        self.assertEqual(self.store.plex_library_status()["last_error"], "Plex unavailable")

    def test_library_manager_filters_sorts_pages_and_reports_totals(self):
        self.store.replace_plex_library([
            {
                "plex_rating_key": "101", "section_id": "4", "media_type": "movie",
                "title": "Dune", "year": 2021, "quality": "4K HDR",
                "versions": [], "upgrade_available": False,
            },
            {
                "plex_rating_key": "102", "section_id": "5", "media_type": "show",
                "title": "Foundation", "year": 2021, "quality": "720p",
                "versions": [], "upgrade_available": True, "episode_count": 20,
                "seasons": [{"number": 1, "title": "Season 1", "episode_count": 10, "quality": "720p"}],
            },
            {
                "plex_rating_key": "103", "section_id": "4", "media_type": "movie",
                "title": "Arrival", "year": 2016, "quality": "1080p",
                "versions": [], "upgrade_available": False,
            },
        ])
        self.assertEqual(
            [item["title"] for item in self.store.list_plex_library(sort="year", limit=2)],
            ["Dune", "Foundation"],
        )
        self.assertEqual(
            self.store.list_plex_library(status="upgrade")[0]["title"], "Foundation"
        )
        self.assertEqual(
            self.store.list_plex_library(quality="4k")[0]["title"], "Dune"
        )
        self.assertEqual(
            self.store.list_plex_library(limit=1, offset=1)[0]["title"], "Dune"
        )
        stats = self.store.plex_library_stats(media_type="movie")
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["movies"], 2)
        self.assertEqual(stats["shows"], 1)
        self.assertEqual(stats["filtered"], 2)
        show = self.store.get_plex_library_item(
            self.store.list_plex_library(media_type="show")[0]["id"]
        )
        self.assertEqual(show["seasons"][0]["episode_count"], 10)

    def test_automatic_list_skips_titles_already_in_plex(self):
        self.store.replace_plex_library([{
            "plex_rating_key": "101", "section_id": "4", "media_type": "movie",
            "title": "Dune", "year": 2021, "tmdb_id": 438631,
            "quality": "1080p", "versions": [],
        }])
        source = self.store.add_list_source({
            "name": "New releases", "kind": "mdblist",
            "url": "https://mdblist.com/lists/example/new-releases",
        })
        coordinator = Coordinator(self.store, self.temp.name)
        with patch("orbit.worker.fetch_list", return_value=[
            {"media_type": "movie", "title": "Dune", "year": 2021, "tmdb_id": 438631},
            {"media_type": "movie", "title": "Arrival", "year": 2016, "tmdb_id": 329865},
        ]):
            result = coordinator.sync_list(source["id"])
        self.assertEqual(result["skipped_existing"], 1)
        self.assertEqual(result["added"], 1)
        self.assertEqual(self.store.list_requests()[0]["title"], "Arrival")

    def test_library_replacements_are_scoped_and_retry_safe(self):
        self.store.replace_plex_library([{
            "plex_rating_key": "201", "section_id": "5", "media_type": "show",
            "title": "Foundation", "year": 2021, "tmdb_id": 93740,
            "quality": "1080p", "versions": [], "episode_count": 2,
            "seasons": [{
                "number": 1, "title": "Season 1", "episode_count": 2,
                "quality": "1080p",
                "episodes": [
                    {"episode_number": 1, "title": "The Emperor's Peace", "versions": []},
                    {"episode_number": 2, "title": "Preparing to Live", "versions": []},
                ],
            }],
        }])
        item = self.store.list_plex_library(media_type="show")[0]
        detailed = self.store.get_plex_library_item(item["id"])
        self.assertNotIn("episodes", item["seasons"][0])
        self.assertEqual(len(detailed["seasons"][0]["episodes"]), 2)

        request, created = self.store.queue_library_replacement(
            detailed, "episode", 1, 2, "1080p"
        )
        duplicate, duplicated = self.store.queue_library_replacement(
            detailed, "episode", 1, 2, "1080p"
        )
        season, season_created = self.store.queue_library_replacement(
            detailed, "season", 1, profile="best"
        )

        self.assertTrue(created)
        self.assertFalse(duplicated)
        self.assertEqual(request["id"], duplicate["id"])
        self.assertTrue(season_created)
        self.assertNotEqual(request["media_key"], season["media_key"])
        scope = __import__("json").loads(request["source_ref"])
        self.assertEqual(scope["episode_number"], 2)
        self.assertEqual(request["source"], "library-replace")

        with self.assertRaisesRegex(ValueError, "not in the current Plex inventory"):
            self.store.queue_library_replacement(detailed, "episode", 1, 99)

    def test_series_completion_only_queues_owned_identified_shows(self):
        self.store.replace_plex_library([
            {
                "plex_rating_key": "201", "section_id": "5", "media_type": "show",
                "title": "Foundation", "year": 2021, "tmdb_id": 93740,
                "quality": "1080p", "versions": [], "episode_count": 10,
            },
            {
                "plex_rating_key": "202", "section_id": "5", "media_type": "show",
                "title": "Not Started", "year": 2026, "tmdb_id": 200002,
                "quality": "Quality unavailable", "versions": [], "episode_count": 0,
            },
            {
                "plex_rating_key": "203", "section_id": "4", "media_type": "movie",
                "title": "Dune", "year": 2021, "tmdb_id": 438631,
                "quality": "1080p", "versions": [],
            },
            {
                "plex_rating_key": "204", "section_id": "5", "media_type": "show",
                "title": "Unidentified", "year": 2020,
                "quality": "720p", "versions": [], "episode_count": 3,
            },
        ])
        candidates = self.store.list_series_completion_candidates()
        self.assertEqual([item["title"] for item in candidates], ["Foundation"])
        request, created = self.store.queue_series_completion(candidates[0], "2026-07-24")
        self.assertTrue(created)
        self.assertEqual(request["source"], "series-monitor")
        self.assertEqual(request["source_ref"], "2026-07-24")
        self.assertEqual(request["status"], "queued")

    def test_series_completion_is_idempotent_per_day_and_requeues_later(self):
        item = {
            "title": "Foundation", "year": 2021, "tmdb_id": 93740,
            "thumb": "/library/metadata/201/thumb/1",
        }
        first, created = self.store.queue_series_completion(item, "2026-07-24")
        self.assertTrue(created)
        duplicate, duplicated = self.store.queue_series_completion(item, "2026-07-24")
        self.assertFalse(duplicated)
        self.assertEqual(first["id"], duplicate["id"])
        self.store.transition(first["id"], "ready", "Series is caught up")
        later, requeued = self.store.queue_series_completion(item, "2026-07-25")
        self.assertTrue(requeued)
        self.assertEqual(later["id"], first["id"])
        self.assertEqual(later["status"], "queued")
        self.assertEqual(later["source_ref"], "2026-07-25")

    def test_series_completion_respects_active_requests_and_daily_limit(self):
        self.store.replace_plex_library([
            {
                "plex_rating_key": str(key), "section_id": "5", "media_type": "show",
                "title": title, "year": 2021, "tmdb_id": tmdb_id,
                "quality": "1080p", "versions": [], "episode_count": 2,
            }
            for key, title, tmdb_id in (
                (301, "Foundation", 93740),
                (302, "Silo", 125988),
            )
        ])
        active, _ = self.store.add_request({
            "media_type": "show", "title": "Foundation", "tmdb_id": 93740,
        })
        coordinator = Coordinator(self.store, self.temp.name)
        with patch("orbit.worker.datetime") as clock:
            clock.now.return_value = __import__("datetime").datetime(
                2026, 7, 24, tzinfo=__import__("datetime").timezone.utc
            )
            first = coordinator.queue_series_completions({
                "complete_aired_series": "true",
                "series_completion_daily_limit": "1",
            })
            second = coordinator.queue_series_completions({
                "complete_aired_series": "true",
                "series_completion_daily_limit": "1",
            })
        self.assertEqual(first["queued"], 1)
        self.assertEqual(second["queued"], 0)
        requests = {item["title"]: item for item in self.store.list_requests()}
        self.assertEqual(requests["Foundation"]["id"], active["id"])
        self.assertEqual(requests["Foundation"]["source"], "manual")
        self.assertEqual(requests["Silo"]["source"], "series-monitor")


if __name__ == "__main__":
    unittest.main()
