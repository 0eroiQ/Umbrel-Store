import os
from pathlib import Path
import tempfile
import unittest

from orbit.startup import (
    patch_legacy_plex_source,
    prepare_media_directories,
    queue_existing_library_scans,
    rewrite_symlink_target_prefix,
)


LEGACY_SCANNER = '''\
def scan(section_results):
    list_ = []
    for section_response in section_results:
        if len(section_response) == 0:
            ui_print("empty")
            list_ = []
            break
        else:
            list_ += section_response
    if len(list_) == 0:
        ui_print("[plex error]: Your library seems empty. To prevent unwanted behaviour, no further downloads will be started. If your library really is empty, please add at least one media item manually.")
    return list_
'''


def _patched_scan(section_results):
    source, changed = patch_legacy_plex_source(LEGACY_SCANNER)
    if not changed:
        raise AssertionError("fixture was not patched")
    namespace = {"ui_print": lambda _message: None}
    exec(source, namespace)
    return namespace["scan"](section_results)


class FreshInstallTests(unittest.TestCase):
    def prepare(self, root):
        movies = os.path.join(root, "Movies")
        series = os.path.join(root, "TV")
        prepared = prepare_media_directories(movies, series, root)
        self.assertEqual(prepared, [movies, series])
        self.assertTrue(os.path.isdir(movies))
        self.assertTrue(os.path.isdir(series))
        return movies, series

    def test_no_movies_or_series_directories_on_fresh_install(self):
        with tempfile.TemporaryDirectory() as root:
            movies = os.path.join(root, "Movies")
            series = os.path.join(root, "TV")
            self.assertFalse(os.path.exists(movies))
            self.assertFalse(os.path.exists(series))
            self.prepare(root)
            self.assertEqual(_patched_scan([[], []]), [])

    def test_movies_only_does_not_require_a_series_item(self):
        with tempfile.TemporaryDirectory() as root:
            movies, _series = self.prepare(root)
            os.mkdir(os.path.join(movies, "Dune (2021)"))
            movie = {"type": "movie", "title": "Dune"}
            self.assertEqual(_patched_scan([[movie], []]), [movie])

    def test_series_only_does_not_require_a_movie_item(self):
        with tempfile.TemporaryDirectory() as root:
            _movies, series = self.prepare(root)
            os.mkdir(os.path.join(series, "Foundation (2021)"))
            show = {"type": "show", "title": "Foundation"}
            self.assertEqual(_patched_scan([[], [show]]), [show])

    def test_both_existing_directories_may_be_empty(self):
        with tempfile.TemporaryDirectory() as root:
            movies = os.path.join(root, "Movies")
            series = os.path.join(root, "TV")
            os.makedirs(movies)
            os.makedirs(series)
            self.prepare(root)
            self.assertEqual(os.listdir(movies), [])
            self.assertEqual(os.listdir(series), [])
            self.assertEqual(_patched_scan([[], []]), [])
            data = os.path.join(root, "data")
            self.assertEqual(queue_existing_library_scans([movies, series], data), [])
            self.assertTrue(os.path.isfile(os.path.join(data, "library-reconciliation-v2.done")))

    def test_existing_movie_and_series_links_are_queued_once_for_startup_scan(self):
        with tempfile.TemporaryDirectory() as root:
            movies, series = self.prepare(root)
            dune = os.path.join(movies, "Dune (2021) {tmdb-438631}")
            silo = os.path.join(series, "Silo (2023) {tvdb-403245}", "Season 01")
            os.makedirs(dune)
            os.makedirs(silo)
            os.symlink("/not-mounted-yet/Dune.mkv", os.path.join(dune, "Dune.mkv"))
            os.symlink("/not-mounted-yet/Silo.mkv", os.path.join(silo, "Silo S01E01.mkv"))
            data = os.path.join(root, "data")

            queued = queue_existing_library_scans([movies, series], data)

            self.assertEqual(len(queued), 2)
            self.assertTrue(os.path.isfile(os.path.join(dune, ".plex-scan-pending")))
            self.assertTrue(os.path.isfile(os.path.dirname(silo) + "/.plex-scan-pending"))
            os.unlink(os.path.join(dune, ".plex-scan-pending"))
            self.assertEqual(queue_existing_library_scans([movies, series], data), [])
            self.assertFalse(os.path.exists(os.path.join(dune, ".plex-scan-pending")))

    def test_directory_creation_refuses_paths_outside_library_root(self):
        with tempfile.TemporaryDirectory() as root:
            movies = os.path.join(root, "Movies")
            with self.assertRaises(ValueError):
                prepare_media_directories(
                    movies,
                    os.path.join(os.path.dirname(root), "TV"),
                    root,
                )
            self.assertFalse(os.path.exists(movies))

    def test_legacy_scanner_patch_is_idempotent(self):
        patched, changed = patch_legacy_plex_source(LEGACY_SCANNER)
        self.assertTrue(changed)
        again, changed_again = patch_legacy_plex_source(patched)
        self.assertFalse(changed_again)
        self.assertEqual(again, patched)

    def test_existing_orbit_links_are_migrated_to_the_plex_visible_root(self):
        with tempfile.TemporaryDirectory() as root:
            movies, series = self.prepare(root)
            episode_dir = os.path.join(series, "Silo (2023)", "Season 01")
            os.makedirs(episode_dir)
            episode = os.path.join(episode_dir, "Silo - S01E01.mkv")
            os.symlink(
                "/downloads/.vortexo-source/shows/Silo/S01E01.mkv",
                episode,
            )

            changed = rewrite_symlink_target_prefix(
                [movies, series],
                "/downloads/.vortexo-source",
                "/zeroq-media/.vortexo-source",
            )

            self.assertEqual(changed, 1)
            self.assertEqual(
                os.readlink(episode),
                "/zeroq-media/.vortexo-source/shows/Silo/S01E01.mkv",
            )
            self.assertEqual(
                rewrite_symlink_target_prefix(
                    [movies, series],
                    "/downloads/.vortexo-source",
                    "/zeroq-media/.vortexo-source",
                ),
                0,
            )

    def test_symlink_migration_ignores_unrelated_and_missing_libraries(self):
        with tempfile.TemporaryDirectory() as root:
            movies = os.path.join(root, "Movies")
            os.makedirs(movies)
            regular = os.path.join(movies, "notes.txt")
            unrelated = os.path.join(movies, "external.mkv")
            relative = os.path.join(movies, "relative.mkv")
            with open(regular, "w", encoding="utf-8") as handle:
                handle.write("keep")
            os.symlink("/somewhere-else/video.mkv", unrelated)
            os.symlink("../source/video.mkv", relative)

            changed = rewrite_symlink_target_prefix(
                [movies, os.path.join(root, "missing")],
                "/downloads/.vortexo-source",
                "/zeroq-media/.vortexo-source",
            )

            self.assertEqual(changed, 0)
            self.assertEqual(Path(regular).read_text(encoding="utf-8"), "keep")
            self.assertEqual(os.readlink(unrelated), "/somewhere-else/video.mkv")
            self.assertEqual(os.readlink(relative), "../source/video.mkv")

    def test_empty_library_has_a_first_run_empty_state(self):
        app = (Path(__file__).parents[1] / "orbit" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "Your Plex library is empty. Orbit is ready for your first movie or series.",
            app,
        )
        self.assertIn("(data.stats?.total || 0) === 0", app)


if __name__ == "__main__":
    unittest.main()
