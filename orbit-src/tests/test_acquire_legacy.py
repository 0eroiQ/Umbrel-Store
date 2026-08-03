import builtins
import os
import tempfile
import unittest
from types import SimpleNamespace

from orbit.acquire_legacy import (
    acquisition_was_handed_off,
    apply_quality_profile,
    library_has_media_type,
    load_engine_settings,
    prepare_item_metadata,
    provider_quota_failure,
    replacement_scope,
    restrict_replacement_item,
)


class FakeUI:
    def __init__(self):
        self.answer = None

    def load(self):
        self.answer = input("Press Enter to update your settings:")


class FakeItem:
    def __init__(self, media_type):
        self.type = media_type
        self.matches = []

    def match(self, service):
        self.matches.append(service)


class FakePlex:
    def __init__(self):
        self.loaded = []

    def movie(self, guid):
        self.loaded.append(("movie", guid))
        return SimpleNamespace(type="movie", title="Dune", year=2021)

    def show(self, guid):
        self.loaded.append(("show", guid))
        return SimpleNamespace(type="show", title="Foundation", year=2021, Seasons=[])


class AcquireLegacyTests(unittest.TestCase):
    def test_provider_quota_failure_reads_only_current_acquisition_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "plex_debrid.log")
            with open(path, "wb") as handle:
                handle.write(b"[premiumize] error: Your space is full!\n")
            offset = os.path.getsize(path)
            self.assertIsNone(provider_quota_failure(path, offset))

            with open(path, "ab") as handle:
                handle.write(
                    b'[premiumize] error: {"code":"account_limit_reached"}\n'
                    b"[premiumize] error: Your space is full! Please delete old files first!\n"
                )
            result = provider_quota_failure(path, offset)

        self.assertTrue(result["retryable"])
        self.assertEqual(result["retry_after_seconds"], 1800)
        self.assertIn("Premiumize storage is full", result["detail"])

    def test_debrid_handoff_remains_successful_while_mount_is_delayed(self):
        movie = SimpleNamespace(
            existing_releases=["Cabin.Fever.2002.1080p"],
            downloaded_releases=[],
        )
        show = SimpleNamespace(
            existing_releases=[],
            downloaded_releases=[],
            Seasons=[SimpleNamespace(
                existing_releases=[],
                Episodes=[SimpleNamespace(existing_releases=["Silo.S03E01"])],
            )],
        )
        missing = SimpleNamespace(existing_releases=[], downloaded_releases=[])

        self.assertTrue(acquisition_was_handed_off(movie))
        self.assertTrue(acquisition_was_handed_off(show))
        self.assertFalse(acquisition_was_handed_off(missing))

    def test_settings_migration_is_non_interactive_and_restores_input(self):
        fake_ui = FakeUI()
        original_input = builtins.input

        load_engine_settings(fake_ui)

        self.assertEqual(fake_ui.answer, "")
        self.assertIs(builtins.input, original_input)

    def test_episode_replacement_restricts_matched_show(self):
        show = SimpleNamespace(Seasons=[
            SimpleNamespace(index=1, Episodes=[
                SimpleNamespace(index=1), SimpleNamespace(index=2),
            ]),
            SimpleNamespace(index=2, Episodes=[SimpleNamespace(index=1)]),
        ])
        scope = {"scope": "episode", "season_number": 1, "episode_number": 2}
        self.assertTrue(restrict_replacement_item(show, scope))
        self.assertEqual([season.index for season in show.Seasons], [1])
        self.assertEqual([episode.index for episode in show.Seasons[0].Episodes], [2])

    def test_replacement_scope_and_quality_profile(self):
        job = {
            "source": "library-replace",
            "source_ref": '{"scope":"season","season_number":3}',
        }
        self.assertEqual(replacement_scope(job)["season_number"], 3)
        releases = SimpleNamespace(sort=SimpleNamespace(versions=[]))
        apply_quality_profile(releases, "4k")
        rules = releases.sort.versions[0][3]
        self.assertIn(["resolution", "requirement", "==", "2160"], rules)

    def test_both_empty_libraries_load_watchlist_movie_from_plex_cloud(self):
        plex = FakePlex()
        native = FakeItem("movie")
        result = prepare_item_metadata(native, {
            "media_type": "movie",
            "plex_guid": "plex://movie/movie-key",
        }, [], "content.services.plex", plex)
        self.assertEqual(result.title, "Dune")
        self.assertEqual(plex.loaded, [("movie", "plex://movie/movie-key")])
        self.assertEqual(native.matches, [])

    def test_no_series_item_loads_watchlist_show_from_plex_cloud(self):
        plex = FakePlex()
        native = FakeItem("show")
        movie_only = [SimpleNamespace(type="movie")]
        result = prepare_item_metadata(native, {
            "media_type": "show",
            "plex_guid": "plex://show/show-key",
        }, movie_only, "content.services.plex", plex)
        self.assertEqual(result.title, "Foundation")
        self.assertEqual(plex.loaded, [("show", "plex://show/show-key")])

    def test_no_movies_item_loads_watchlist_movie_from_plex_cloud(self):
        plex = FakePlex()
        native = FakeItem("movie")
        series_only = [SimpleNamespace(type="show")]
        result = prepare_item_metadata(native, {
            "media_type": "movie",
            "plex_guid": "plex://movie/movie-key",
        }, series_only, "content.services.plex", plex)
        self.assertEqual(result.title, "Dune")
        self.assertFalse(library_has_media_type(series_only, "movie"))

    def test_movies_only_can_use_local_movie_metadata_without_requiring_series(self):
        plex = FakePlex()
        native = FakeItem("movie")
        movie_only = [SimpleNamespace(type="movie")]
        result = prepare_item_metadata(
            native, {"media_type": "movie"}, movie_only,
            "content.services.plex", plex,
        )
        self.assertIs(result, native)
        self.assertEqual(native.matches, ["content.services.plex"])

    def test_series_only_can_use_local_series_metadata_without_requiring_movies(self):
        plex = FakePlex()
        native = FakeItem("show")
        series_only = [SimpleNamespace(type="show")]
        result = prepare_item_metadata(
            native, {"media_type": "show"}, series_only,
            "content.services.plex", plex,
        )
        self.assertIs(result, native)
        self.assertEqual(native.matches, ["content.services.plex"])


if __name__ == "__main__":
    unittest.main()
