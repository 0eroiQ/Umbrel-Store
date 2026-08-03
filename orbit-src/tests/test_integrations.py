import unittest
from unittest.mock import patch

from orbit import __version__
from orbit import integrations


class IntegrationTests(unittest.TestCase):
    def test_tmdb_search_normalises_movie_and_show(self):
        payload = {"results": [
            {"id": 1, "media_type": "movie", "title": "One", "release_date": "2026-02-03"},
            {"id": 2, "media_type": "tv", "name": "Two", "first_air_date": "2025-01-02"},
            {"id": 3, "media_type": "person", "name": "Skip"},
        ]}
        with patch.object(integrations, "_json_request", return_value=payload):
            results = integrations.search_tmdb("test", "key")
        self.assertEqual([(item["media_type"], item["title"], item["year"]) for item in results], [
            ("movie", "One", 2026), ("show", "Two", 2025),
        ])

    def test_mdblist_url_becomes_authenticated_items_endpoint(self):
        with patch.object(integrations, "_json_request", return_value=[{
            "mediatype": "movie", "title": "Dune", "year": 2021,
            "imdb_id": "tt1160419", "tmdb_id": 438631,
        }]) as request:
            items = integrations.fetch_mdblist(
                "https://mdblist.com/lists/example/great-films", "secret", 25
            )
        self.assertEqual(items[0]["tmdb_id"], 438631)
        parsed = integrations.urllib.parse.urlparse(request.call_args.args[0])
        self.assertEqual(parsed.path, "/lists/example/great-films/items")
        self.assertEqual(
            integrations.urllib.parse.parse_qs(parsed.query),
            {"apikey": ["secret"], "limit": ["25"]},
        )

    def test_mdblist_current_response_buckets_are_combined(self):
        payload = {
            "movies": [{
                "title": "Dune", "mediatype": "movie",
                "ids": {"tmdb": 438631, "imdb": "tt1160419"},
            }],
            "shows": [{
                "title": "Severance", "mediatype": "show",
                "ids": {"tmdb": 95396, "imdb": "tt11280740"},
            }],
        }
        with patch.object(integrations, "_json_request", return_value=payload):
            items = integrations.fetch_mdblist(
                "https://mdblist.com/lists/example/great-films", "secret", 25
            )
        self.assertEqual(
            [(item["media_type"], item["title"]) for item in items],
            [("movie", "Dune"), ("show", "Severance")],
        )

    def test_mdblist_cursor_paginates_more_than_ten_thousand_items(self):
        def page(start, size, next_cursor="", has_more=False):
            return {
                "items": [
                    {
                        "mediatype": "movie", "title": f"Movie {item_id}",
                        "tmdb_id": item_id,
                    }
                    for item_id in range(start, start + size)
                ],
                "pagination": {
                    "has_more": has_more,
                    "next_cursor": next_cursor or None,
                },
            }

        responses = [
            page(start, 1000, f"cursor-{index + 1}", True)
            for index, start in enumerate(range(1, 10001, 1000))
        ]
        responses.append(page(10001, 25))
        with patch.object(
            integrations, "_json_request", side_effect=responses
        ) as request:
            items = integrations.fetch_mdblist(
                "https://mdblist.com/lists/example/boxsets", "secret", 10025
            )

        self.assertEqual(len(items), 10025)
        self.assertEqual(len(request.call_args_list), 11)
        first_query = integrations.urllib.parse.parse_qs(
            integrations.urllib.parse.urlparse(request.call_args_list[0].args[0]).query
        )
        second_query = integrations.urllib.parse.parse_qs(
            integrations.urllib.parse.urlparse(request.call_args_list[1].args[0]).query
        )
        last_query = integrations.urllib.parse.parse_qs(
            integrations.urllib.parse.urlparse(request.call_args_list[-1].args[0]).query
        )
        self.assertEqual(first_query["limit"], ["1000"])
        self.assertNotIn("cursor", first_query)
        self.assertEqual(second_query["cursor"], ["cursor-1"])
        self.assertEqual(last_query["limit"], ["25"])
        self.assertEqual(last_query["cursor"], ["cursor-10"])

    def test_trakt_nested_items_are_normalised(self):
        with patch.object(integrations, "_json_request", return_value=[{
            "show": {"title": "Severance", "year": 2022, "ids": {"tmdb": 95396, "imdb": "tt11280740"}}
        }]):
            items = integrations.fetch_trakt(
                "https://trakt.tv/users/example/lists/favourites", "client", 50
            )
        self.assertEqual(items[0]["media_type"], "show")
        self.assertEqual(items[0]["tmdb_id"], 95396)

    def test_plex_watchlist_uses_saved_token_and_normalises_guids(self):
        payload = {"MediaContainer": {
            "totalSize": 2,
            "Metadata": [
                {
                    "type": "movie", "title": "Dune", "year": 2021,
                    "guid": "plex://movie/5d7768256f4521001e739c01",
                    "Guid": [{"id": "tmdb://438631"}, {"id": "imdb://tt1160419"}],
                },
                {
                    "type": "show", "title": "Severance", "year": 2022,
                    "ratingKey": "5e163c1d98f1f1003e3e4892",
                    "Guid": [{"id": "tmdb://95396"}],
                },
            ],
        }}
        with patch.object(integrations, "_json_request", return_value=payload) as request:
            items = integrations.fetch_plex_watchlist("plex-token", 25)
        self.assertEqual(
            [(item["media_type"], item["tmdb_id"]) for item in items],
            [("movie", 438631), ("show", 95396)],
        )
        self.assertEqual(items[0]["plex_guid"], "plex://movie/5d7768256f4521001e739c01")
        self.assertEqual(items[1]["plex_guid"], "plex://show/5e163c1d98f1f1003e3e4892")
        self.assertEqual(request.call_args.args[1]["X-Plex-Token"], "plex-token")
        self.assertEqual(request.call_args.args[1]["X-Plex-Version"], __version__)
        parsed = integrations.urllib.parse.urlparse(request.call_args.args[0])
        query = integrations.urllib.parse.parse_qs(parsed.query)
        self.assertEqual(query["X-Plex-Container-Size"], ["25"])
        self.assertEqual(query["includeGuids"], ["1"])

    def test_plex_watchlist_requires_a_token(self):
        with self.assertRaisesRegex(integrations.IntegrationError, "Plex token"):
            integrations.fetch_plex_watchlist("", 25)


if __name__ == "__main__":
    unittest.main()
