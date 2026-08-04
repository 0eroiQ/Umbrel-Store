import builtins
import json
import os
import tempfile
import unittest
import unittest.mock
from types import SimpleNamespace

from orbit.acquire_legacy import (
    acquisition_was_handed_off,
    apply_quality_profile,
    library_has_media_type,
    install_alldebrid_compatibility,
    install_torbox_compatibility,
    load_engine_settings,
    prepare_item_metadata,
    provider_download_wait,
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
    def test_alldebrid_compatibility_uses_supported_upload_for_selected_release(self):
        calls = []
        service = SimpleNamespace(
            short="AD",
            post=lambda url, data: (
                calls.append((url, data))
                or SimpleNamespace(data=SimpleNamespace(magnets=[SimpleNamespace(id=42, ready=True)]))
            ),
            ui_print=lambda message: calls.append(message),
        )
        install_alldebrid_compatibility(service)
        invalid = SimpleNamespace(hash="short", cached=[], download=["bad"], title="Bad")
        selected = SimpleNamespace(
            hash="a" * 40, cached=[], download=["magnet:?xt=urn:btih:" + "a" * 40],
            title="Selected Movie",
        )
        element = SimpleNamespace(Releases=[invalid, selected])

        service.check(element)
        self.assertEqual(element.Releases, [selected])
        self.assertEqual(selected.cached, ["AD"])
        self.assertTrue(service.download(element))
        self.assertEqual(calls[0], (
            "https://api.alldebrid.com/v4/magnet/upload",
            {"magnets[]": selected.download[0]},
        ))

    def test_uncached_alldebrid_upload_pauses_queue_until_download_is_ready(self):
        service = SimpleNamespace(
            short="AD",
            post=lambda _url, _data: SimpleNamespace(
                data=SimpleNamespace(magnets=[SimpleNamespace(id=42, ready=False)])
            ),
            ui_print=lambda _message: None,
        )
        install_alldebrid_compatibility(service)
        release = SimpleNamespace(
            hash="a" * 40, cached=[], download=["magnet:?xt=urn:btih:" + "a" * 40],
            title="Downloading Movie",
        )
        episode = SimpleNamespace(Releases=[release])
        show = SimpleNamespace(Seasons=[SimpleNamespace(Episodes=[episode])])

        self.assertTrue(service.download(episode))
        result = provider_download_wait(show)

        self.assertTrue(result["retryable"])
        self.assertEqual(result["retry_after_seconds"], 300)
        self.assertIn("paused the recommendation queue", result["detail"])

    def test_alldebrid_obsolete_endpoint_pauses_instead_of_burning_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "plex_debrid.log")
            with open(path, "wb") as handle:
                handle.write(
                    b"[alldebrid] error 404: Endpoint doesn't exist\n"
                )
            result = provider_quota_failure(path)

        self.assertTrue(result["retryable"])
        self.assertEqual(result["retry_after_seconds"], 1800)
        self.assertIn("obsolete API endpoint", result["detail"])

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


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.content = json.dumps(payload if payload is not None else {}).encode()


class FakeSession:
    def __init__(self, get_responses=None, post_responses=None):
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])
        self.get_calls = []
        self.post_calls = []

    def get(self, url, headers=None, timeout=None):
        self.get_calls.append((url, timeout))
        result = self.get_responses.pop(0) if self.get_responses else FakeResponse(200)
        if isinstance(result, Exception):
            raise result
        return result

    def post(self, url, headers=None, data=None, timeout=None):
        self.post_calls.append((url, data, timeout))
        result = self.post_responses.pop(0) if self.post_responses else FakeResponse(200)
        if isinstance(result, Exception):
            raise result
        return result


def fake_torbox(session, download=None):
    service = SimpleNamespace(
        API_BASE="https://api.torbox.app/v1/api",
        session=session,
        messages=[],
        _auth_header=lambda: "Bearer key",
        logerror=lambda response: None,
        download=download or (lambda element, *args, **kwargs: False),
    )
    service.ui_print = service.messages.append
    return service


class TorboxCompatibilityTests(unittest.TestCase):
    def setUp(self):
        patcher = unittest.mock.patch("orbit.acquire_legacy.time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_requests_are_bounded_and_retry_degraded_statuses(self):
        session = FakeSession([
            FakeResponse(504),
            FakeResponse(200, {"data": {"id": 7}}),
        ])
        service = fake_torbox(session)
        install_torbox_compatibility(service)

        result = service.get("https://api.torbox.app/v1/api/torrents/mylist")

        self.assertEqual(result.data.id, 7)
        self.assertEqual(len(session.get_calls), 2)
        # A hung request must never consume the whole readiness budget.
        self.assertTrue(all(timeout == 15 for _url, timeout in session.get_calls))

    def test_readiness_polls_a_single_torrent_by_id(self):
        session = FakeSession([
            FakeResponse(200, {"data": {"id": 42, "download_present": True}}),
        ])
        service = fake_torbox(session)
        install_torbox_compatibility(service)

        self.assertTrue(service._wait_until_ready(42))
        url = session.get_calls[0][0]
        self.assertIn("id=42", url)

    def test_readiness_stops_early_on_a_failed_torrent(self):
        session = FakeSession([
            FakeResponse(200, {"data": {"id": 42, "download_state": "error"}}),
        ])
        service = fake_torbox(session)
        install_torbox_compatibility(service)

        self.assertFalse(service._wait_until_ready(42))
        self.assertEqual(len(session.get_calls), 1)

    def test_lost_create_response_recovers_the_torrent_id_by_hash(self):
        info_hash = "a" * 40
        session = FakeSession(
            get_responses=[
                FakeResponse(200, {"data": [{"id": 99, "hash": info_hash.upper()}]}),
            ],
            post_responses=[FakeResponse(504)],
        )
        service = fake_torbox(session)
        install_torbox_compatibility(service)

        response = service.post(
            "https://api.torbox.app/v1/api/torrents/createtorrent",
            data={"magnet": f"magnet:?xt=urn:btih:{info_hash}&dn=Example"},
        )

        # TorBox accepted the add; only the reply was lost. Never re-POST.
        self.assertEqual(response.data.torrent_id, 99)
        self.assertEqual(len(session.post_calls), 1)

    def test_degraded_api_raises_a_retryable_wait_instead_of_failing(self):
        session = FakeSession([FakeResponse(504)] * 3)
        service = fake_torbox(session)

        def download(element, *args, **kwargs):
            service.get("https://api.torbox.app/v1/api/torrents/mylist")
            return False

        service.download = download
        install_torbox_compatibility(service)

        element = SimpleNamespace()
        self.assertFalse(service.download(element))

        wait = provider_download_wait(element)
        self.assertIsNotNone(wait)
        self.assertTrue(wait["retryable"])
        self.assertIn("TorBox", wait["detail"])

    def test_healthy_failure_is_not_reported_as_retryable(self):
        session = FakeSession([FakeResponse(200, {"data": []})])
        service = fake_torbox(session)

        def download(element, *args, **kwargs):
            service.get("https://api.torbox.app/v1/api/torrents/mylist")
            return False

        service.download = download
        install_torbox_compatibility(service)

        element = SimpleNamespace()
        self.assertFalse(service.download(element))
        self.assertIsNone(provider_download_wait(element))


if __name__ == "__main__":
    unittest.main()
