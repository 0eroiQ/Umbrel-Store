import os
import tempfile
import unittest
from pathlib import Path

from vortexo.audit import audit_library, quarantine


class LibraryAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.library = root / "vortexo"
        self.movies = self.library / "Movies"
        self.tv = self.library / "TV"
        self.source = root / ".vortexo-source"
        self.movies.mkdir(parents=True)
        self.tv.mkdir(parents=True)
        self.source.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def _link(self, folder, link_name, target_name, size=2 * 1024 ** 3):
        source = self.source / target_name
        source.parent.mkdir(parents=True, exist_ok=True)
        with source.open("wb") as handle:
            handle.truncate(size)
        destination = self.movies / folder / link_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(source, destination)
        return destination

    def test_audit_separates_safe_ambiguous_and_high_confidence_bad_links(self):
        safe = self._link(
            "Memento (2000)",
            "Memento (2000) - 1080p - safe.mkv",
            "Memento.2000.1080p.WEB-DL.mkv",
        )
        wrong = self._link(
            "Soulm8te (2026)",
            "Soulm8te (2026) - 720p - wrong.mkv",
            "Soulmates.2025.720p.WEBRip.Hindi.mkv",
        )
        ambiguous = self._link(
            "Green Room (2016)",
            "Green Room (2016) - 1080p - alternate-year.mkv",
            "Green.Room.2015.1080p.mkv",
        )
        rows = {
            row["path"]: row
            for row in audit_library(self.source, self.movies, self.tv)
        }
        safe_key = str(safe.parent.resolve() / safe.name)
        wrong_key = str(wrong.parent.resolve() / wrong.name)
        ambiguous_key = str(ambiguous.parent.resolve() / ambiguous.name)
        self.assertEqual(rows[safe_key]["classification"], "safe")
        self.assertTrue(rows[wrong_key]["high_confidence"])
        self.assertEqual(
            rows[ambiguous_key]["classification"],
            "ambiguous_year",
        )
        self.assertFalse(rows[ambiguous_key]["high_confidence"])

    def test_quarantine_moves_only_high_confidence_bad_symlinks(self):
        wrong = self._link(
            "Minions & Monsters (2026)",
            "Minions & Monsters (2026) - 1080p - wrong.mkv",
            "Mimoni a monstra.mkv",
            size=5 * 1024 ** 3,
        )
        trailer = self._link(
            "Movie (2026)",
            "Movie (2026) - 1080p - trailer.mkv",
            "Movie.2026.Official.Trailer.1080p.mkv",
        )
        rows = audit_library(self.source, self.movies, self.tv)
        root, moved = quarantine(rows, self.library)
        self.assertEqual(len(moved), 2)
        self.assertFalse(wrong.is_symlink())
        self.assertFalse(trailer.is_symlink())
        self.assertTrue((root / "Movies" / wrong.parent.name / wrong.name).is_symlink())
        self.assertTrue((root / "manifest.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
