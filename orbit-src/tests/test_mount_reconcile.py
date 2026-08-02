import os
import tempfile
import unittest

from orbit.mount_reconcile import reconcile_mounted_sources


class FakeSymlinker:
    def symlink_item(
        self, item, mount_dir, library_dirs, log_fn=None,
        mount_attempts=1, retry_delay=0,
    ):
        kind = "tv" if item.type == "show" else "movie"
        library = library_dirs[kind]
        folder = next(
            os.path.join(library, name)
            for name in os.listdir(library)
            if item.EID[0].split("://", 1)[1] in name
        )
        source = os.path.join(
            mount_dir, ".vortexo-source", item.Releases[0].torrent_name, "video.mkv"
        )
        destination = os.path.join(folder, "video.mkv")
        if not os.path.lexists(destination):
            os.symlink(source, destination)
        with open(os.path.join(folder, ".plex-scan-pending"), "a", encoding="utf-8"):
            pass
        return folder


class MountReconcileTests(unittest.TestCase):
    def test_premiumize_prefix_recovers_reserved_movie_folder(self):
        with tempfile.TemporaryDirectory() as root:
            raw = os.path.join(root, ".vortexo-source")
            movies = os.path.join(root, "Movies")
            television = os.path.join(root, "TV")
            source_name = "www.UIndex.org - Cabin Fever 2002 Remastered 1080p"
            source = os.path.join(raw, source_name)
            folder = os.path.join(movies, "Cabin Fever (2003) {tmdb-11547}")
            os.makedirs(source)
            os.makedirs(folder)
            os.makedirs(television)
            with open(os.path.join(source, "video.mkv"), "wb") as handle:
                handle.write(b"video")

            changed = reconcile_mounted_sources(
                root,
                {"movie": movies, "tv": television},
                symlinker=FakeSymlinker(),
            )

            self.assertEqual(changed, [("movie", folder)])
            self.assertTrue(os.path.islink(os.path.join(folder, "video.mkv")))
            self.assertTrue(os.path.isfile(os.path.join(folder, ".plex-scan-pending")))
            self.assertEqual(reconcile_mounted_sources(
                root,
                {"movie": movies, "tv": television},
                symlinker=FakeSymlinker(),
            ), [])

    def test_series_source_does_not_require_any_movie_folder(self):
        with tempfile.TemporaryDirectory() as root:
            raw = os.path.join(root, ".vortexo-source")
            movies = os.path.join(root, "Movies")
            television = os.path.join(root, "TV")
            source_name = "Silo.S03E01.MULTI.1080p.WEB.H264"
            source = os.path.join(raw, source_name)
            folder = os.path.join(television, "Silo (2023) {tvdb-403245}")
            os.makedirs(source)
            os.makedirs(folder)
            os.makedirs(movies)
            with open(os.path.join(source, "video.mkv"), "wb") as handle:
                handle.write(b"video")

            changed = reconcile_mounted_sources(
                root,
                {"movie": movies, "tv": television},
                symlinker=FakeSymlinker(),
            )

            self.assertEqual(changed, [("show", folder)])


if __name__ == "__main__":
    unittest.main()
