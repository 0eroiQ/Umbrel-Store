import io
import json
import unittest
import urllib.error
from unittest import mock

from vortexo.integrations import (
    IntegrationError,
    choose_video_file,
    deduplicate_streams,
    external_ids,
    fetch_plex_watchlist,
    fetch_streams,
    json_request,
    normalize_discover_id,
    normalize_media,
    normalize_stream,
    plex_cloud_headers,
    plex_server_headers,
    release_matches_media,
    select_automatic_stream,
)


class DiscoverIdentityTests(unittest.TestCase):
    def test_local_plex_headers_do_not_override_server_identity(self):
        headers = plex_server_headers("owner-token")
        self.assertEqual(headers["X-Plex-Token"], "owner-token")
        for name in (
            "X-Plex-Product",
            "X-Plex-Version",
            "X-Plex-Client-Identifier",
            "X-Plex-Platform",
        ):
            self.assertNotIn(name, headers)

    def test_cloud_headers_do_not_publish_companion_identity(self):
        headers = plex_cloud_headers("owner-token")
        self.assertEqual(headers["X-Plex-Token"], "owner-token")
        for name in (
            "X-Plex-Product",
            "X-Plex-Version",
            "X-Plex-Client-Identifier",
            "X-Plex-Platform",
        ):
            self.assertNotIn(name, headers)

    def test_normalizes_discover_routes_and_rejects_remote_ids(self):
        self.assertEqual(
            normalize_discover_id("%2Flibrary%2Fmetadata%2F5d776d1796b655001fe3f324"),
            "5d776d1796b655001fe3f324",
        )
        self.assertEqual(
            normalize_discover_id("plex://movie/5d776d1796b655001fe3f324"),
            "5d776d1796b655001fe3f324",
        )
        self.assertEqual(normalize_discover_id("https://example.test/item"), "")

    def test_extracts_imdb_tmdb_and_episode_identity(self):
        raw = {
            "ratingKey": "5d776d1796b655001fe3f324",
            "type": "episode",
            "title": "Episode",
            "grandparentTitle": "Show",
            "parentIndex": 2,
            "index": 4,
            "grandparentRatingKey": "show-discover-id",
            "Guid": [{"id": "imdb://tt0209144"}, {"id": "tmdb://77"}],
        }
        self.assertEqual(external_ids(raw), (77, "tt0209144"))
        media = normalize_media(raw)
        self.assertEqual(media["discover_id"], "5d776d1796b655001fe3f324")
        self.assertEqual((media["season"], media["episode"]), (2, 4))
        self.assertEqual(media["grandparent_rating_key"], "show-discover-id")

    def test_plex_watchlist_paginates_and_preserves_discover_identity(self):
        responses = [
            {
                "MediaContainer": {
                    "totalSize": 2,
                    "Metadata": [
                        {
                            "ratingKey": "movie-discover",
                            "type": "movie",
                            "title": "Memento",
                            "year": 2000,
                            "Guid": [
                                {"id": "imdb://tt0209144"},
                                {"id": "tmdb://77"},
                            ],
                        }
                    ],
                }
            },
            {
                "MediaContainer": {
                    "totalSize": 2,
                    "Metadata": [
                        {
                            "ratingKey": "show-discover",
                            "type": "show",
                            "title": "Severance",
                            "Guid": [{"id": "tmdb://95396"}],
                        }
                    ],
                }
            },
        ]
        with mock.patch(
            "vortexo.integrations.json_request", side_effect=responses
        ) as request:
            items = fetch_plex_watchlist("owner-token", 2)
        self.assertEqual(
            [(item["discover_id"], item["type"]) for item in items],
            [("movie-discover", "movie"), ("show-discover", "show")],
        )
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            request.call_args_list[0].kwargs["headers"]["X-Plex-Token"],
            "owner-token",
        )


class StreamNormalizationTests(unittest.TestCase):
    def test_normalizes_quality_hdr_codec_audio_and_size(self):
        stream = normalize_stream(
            {
                "name": "Memento.2000.2160p.DV.HEVC.TrueHD",
                "description": "cached 53.2 GB",
                "url": "https://stream.example/video.mkv",
                "infoHash": "ABC123",
                "behaviorHints": {"filename": "Memento.2000.2160p.DV.HEVC.TrueHD.mkv"},
            },
            "AIOStreams",
        )
        self.assertEqual(stream["quality"], "4K")
        self.assertEqual(stream["dynamic_range"], "Dolby Vision")
        self.assertEqual(stream["codec"], "HEVC")
        self.assertEqual(stream["audio"], "TrueHD")
        self.assertEqual(stream["size_gb"], 53.2)
        self.assertTrue(stream["cached"])
        self.assertTrue(stream["can_play_now"])
        self.assertTrue(stream["can_add"])

    def test_external_web_page_is_not_treated_as_playable_media(self):
        stream = normalize_stream(
            {"externalUrl": "https://example.test/watch", "infoHash": "abc"},
            "Torrentio",
        )
        self.assertFalse(stream["can_play_now"])
        self.assertTrue(stream["can_add"])

    def test_torbox_item_id_can_be_added_without_exposing_it_as_media_url(self):
        stream = normalize_stream(
            {"torrentId": 42, "name": "Existing TorBox item"},
            "TorBox",
        )
        self.assertTrue(stream["can_add"])
        self.assertFalse(stream["can_play_now"])
        self.assertEqual(stream["torbox_id"], 42)

    def test_extracts_info_hash_from_magnet(self):
        stream = normalize_stream(
            {"magnet": "magnet:?xt=urn:btih:ABCDEF123456"},
            "Source",
        )
        self.assertEqual(stream["info_hash"], "abcdef123456")

    def test_extracts_torbox_hash_and_file_index_from_torrentio_url(self):
        info_hash = "a" * 40
        stream = normalize_stream(
            {
                "url": (
                    "https://torrentio.strem.fun/private-config/torbox/"
                    f"private-account-token/{info_hash}/Movie.1080p.mkv/5/"
                    "Movie.1080p.mkv"
                ),
                "behaviorHints": {"filename": "Movie.1080p.mkv"},
            },
            "Torrentio TB",
        )
        self.assertEqual(stream["info_hash"], info_hash)
        self.assertEqual(stream["file_idx"], 5)
        self.assertEqual(stream["magnet"], f"magnet:?xt=urn:btih:{info_hash}")
        self.assertTrue(stream["can_add"])

    def test_does_not_extract_hash_from_unrelated_stream_url(self):
        stream = normalize_stream(
            {
                "url": f"https://stream.example/{'b' * 40}/Movie.mkv",
                "behaviorHints": {"filename": "Movie.mkv"},
            },
            "Source",
        )
        self.assertFalse(stream["info_hash"])
        self.assertFalse(stream["can_add"])

    def test_deduplicates_by_info_hash_and_sorts_cached_playable_first(self):
        rows = [
            {
                "info_hash": "same",
                "url": "https://one",
                "file_name": "duplicate-one",
                "can_play_now": True,
                "cached": False,
                "quality": "4K",
                "size_gb": 80,
            },
            {
                "info_hash": "same",
                "url": "https://two",
                "file_name": "duplicate-two",
                "can_play_now": True,
                "cached": True,
                "quality": "1080p",
                "size_gb": 10,
            },
            {
                "info_hash": "best",
                "url": "https://best",
                "file_name": "best",
                "can_play_now": True,
                "cached": True,
                "quality": "4K",
                "size_gb": 50,
            },
        ]
        result = deduplicate_streams(rows)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["info_hash"], "best")

    def test_selects_exact_episode_before_largest_file(self):
        torrent = {
            "files": [
                {"id": 1, "name": "Show.S01E01.mkv", "size": 10_000},
                {"id": 2, "name": "Show.S01E02.mkv", "size": 5_000},
            ]
        }
        self.assertEqual(choose_video_file(torrent, 1, 2)["file_id"], 2)

    def test_rejects_release_for_a_different_movie_before_linking(self):
        self.assertFalse(
            release_matches_media(
                {
                    "type": "movie",
                    "title": "Spider-Man Brand New Day",
                    "year": 2026,
                },
                {
                    "name": (
                        "www.IJIndex.org - Marvel Studios Iron Man 2008 "
                        "1080p MA WEB-DL DDP5 1 H 264-SARVO"
                    )
                },
                {
                    "path": (
                        "Marvel Studios Iron Man 2008 1080p MA WEB-DL "
                        "DDP5 1 H 264-SARVO.mkv"
                    )
                },
            )
        )

    def test_accepts_matching_movie_and_episode_releases(self):
        self.assertTrue(
            release_matches_media(
                {
                    "type": "movie",
                    "title": "Spider-Man: Brand New Day",
                    "year": 2026,
                },
                {"name": "Spider-Man.Brand.New.Day.2026.1080p.WEB-DL"},
                {"path": "Spider-Man.Brand.New.Day.2026.1080p.mkv"},
            )
        )
        self.assertTrue(
            release_matches_media(
                {
                    "type": "episode",
                    "title": "Woe's Hollow",
                    "parent_title": "Severance",
                    "season": 2,
                    "episode": 4,
                },
                {"name": "Severance Season 2"},
                {"path": "Severance.S02E04.1080p.mkv"},
            )
        )

    def test_automatic_stream_selection_is_cached_profile_and_size_aware(self):
        rows = [
            {
                "info_hash": "4k-large",
                "quality": "4K",
                "size_gb": 120,
                "cached": True,
                "can_add": True,
            },
            {
                "info_hash": "4k-fit",
                "quality": "4K",
                "size_gb": 60,
                "cached": True,
                "can_add": True,
            },
            {
                "info_hash": "1080p-fit",
                "quality": "1080p",
                "size_gb": 20,
                "cached": True,
                "can_add": True,
            },
            {
                "info_hash": "uncached",
                "quality": "4K",
                "size_gb": 40,
                "cached": False,
                "can_add": True,
            },
        ]
        self.assertEqual(
            select_automatic_stream(rows, "4k", max_size_gb=80)["info_hash"],
            "4k-fit",
        )
        self.assertEqual(
            select_automatic_stream(rows, "1080p", max_size_gb=80)["info_hash"],
            "1080p-fit",
        )
        self.assertIsNone(
            select_automatic_stream([rows[-1]], "best", cached_only=True)
        )

    def test_manifest_lookup_uses_movie_and_series_stremio_routes(self):
        calls = []

        def response(url, **_kwargs):
            calls.append(url)
            if url.endswith("/manifest.json"):
                return {"name": "Source", "resources": [{"name": "stream"}]}
            return {"streams": [{"url": "https://stream.example/video.mp4"}]}

        with mock.patch("vortexo.integrations.json_request", side_effect=response):
            fetch_streams(
                "https://source.example/config/manifest.json",
                {"type": "movie", "imdb_id": "tt0209144"},
            )
            fetch_streams(
                "https://source.example/config/manifest.json",
                {"type": "show", "imdb_id": "tt1234567"},
                2,
                4,
            )
        self.assertIn(
            "https://source.example/config/stream/movie/tt0209144.json", calls
        )
        self.assertIn(
            "https://source.example/config/stream/series/tt1234567:2:4.json", calls
        )


class RemoteErrorTests(unittest.TestCase):
    def test_remote_error_returns_detail_without_request_secrets(self):
        error = urllib.error.HTTPError(
            "https://api.example.test/private?token=secret",
            429,
            "rate limited",
            {},
            io.BytesIO(json.dumps({"detail": "TorBox rate limit reached"}).encode()),
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(IntegrationError, "TorBox rate limit reached"):
                json_request("https://api.example.test/private?token=secret")


if __name__ == "__main__":
    unittest.main()
