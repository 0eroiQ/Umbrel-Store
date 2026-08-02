import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from orbit import plex


class PlexInventoryTests(unittest.TestCase):
    def test_library_sections_include_paths_visible_inside_plex(self):
        root = ET.fromstring("""
        <MediaContainer>
          <Directory key="1" type="movie" title="Movies">
            <Location path="/zeroq-media/vortexo/Movies"/>
          </Directory>
          <Directory key="2" type="show" title="TV Shows">
            <Location path="/zeroq-media/vortexo/TV"/>
          </Directory>
        </MediaContainer>
        """)
        with patch.object(plex, "_plex_xml", return_value=root):
            sections = plex.plex_library_sections("http://plex", "token")
        self.assertEqual(sections, [
            {
                "section_id": "1", "media_type": "movie",
                "locations": ["/zeroq-media/vortexo/Movies"],
            },
            {
                "section_id": "2", "media_type": "show",
                "locations": ["/zeroq-media/vortexo/TV"],
            },
        ])

    def test_movie_versions_include_real_quality_and_ids(self):
        section = ET.fromstring("""
        <MediaContainer>
          <Video ratingKey="101" type="movie" title="Dune" year="2021">
            <Guid id="tmdb://438631"/><Guid id="imdb://tt1160419"/>
            <Media videoResolution="4k" videoDynamicRange="DOVI" videoCodec="hevc"
                   audioCodec="eac3" container="mkv" bitrate="22000" width="3840" height="2160">
              <Part size="123456789" file="/movies/Dune.mkv"/>
            </Media>
            <Media videoResolution="1080" videoDynamicRange="SDR" videoCodec="h264"
                   audioCodec="aac" container="mp4" height="1080">
              <Part size="456789"/>
            </Media>
          </Video>
        </MediaContainer>
        """)
        with patch.object(plex, "_plex_xml", return_value=section):
            items = plex.scan_plex_library("http://plex", "token", ["4"])
        self.assertEqual(items[0]["tmdb_id"], 438631)
        self.assertEqual(items[0]["imdb_id"], "tt1160419")
        self.assertEqual(items[0]["quality"], "4K Dolby Vision · 1080p")
        self.assertFalse(items[0]["upgrade_available"])

    def test_show_quality_is_aggregated_from_episodes(self):
        shows = ET.fromstring("""
        <MediaContainer>
          <Directory ratingKey="201" type="show" title="Foundation" year="2021">
            <Guid id="tmdb://93740"/>
          </Directory>
        </MediaContainer>
        """)
        episodes = ET.fromstring("""
        <MediaContainer>
          <Video ratingKey="202" type="episode" grandparentRatingKey="201"
                 index="1" title="The Emperor's Peace" duration="3600000"
                 originallyAvailableAt="2021-09-24">
            <attr />
            <Media videoResolution="720" videoCodec="h264" audioCodec="aac" container="mkv">
              <Part size="1000">
                <Stream id="10" streamType="1" codec="h264" width="1280" height="720"
                        displayTitle="720p H.264" selected="1"/>
                <Stream id="11" streamType="2" codec="aac" language="English"
                        channels="6"/>
                <Stream id="12" streamType="3" codec="srt" language="English"
                        forced="1"/>
              </Part>
            </Media>
          </Video>
          <Video ratingKey="203" type="episode" grandparentRatingKey="201"
                 parentIndex="2" index="1" title="In Seldon's Shadow">
            <Media videoResolution="720" videoCodec="h264" audioCodec="aac" container="mkv">
              <Part size="1200"/>
            </Media>
          </Video>
        </MediaContainer>
        """)
        with patch.object(plex, "_plex_xml", side_effect=[shows, episodes]):
            items = plex.scan_plex_library("http://plex", "token", ["5"])
        self.assertEqual(items[0]["media_type"], "show")
        self.assertEqual(items[0]["episode_count"], 2)
        self.assertEqual(items[0]["quality"], "720p")
        self.assertTrue(items[0]["upgrade_available"])
        self.assertEqual(len(items[0]["versions"]), 1)
        self.assertEqual(items[0]["seasons"][0]["title"], "Specials")
        self.assertEqual(items[0]["seasons"][1]["title"], "Season 2")
        episode = items[0]["seasons"][0]["episodes"][0]
        self.assertEqual(episode["title"], "The Emperor's Peace")
        self.assertEqual(episode["episode_number"], 1)
        self.assertEqual(episode["duration"], 3600000)
        self.assertEqual(
            [stream["type"] for stream in episode["versions"][0]["streams"]],
            ["video", "audio", "subtitle"],
        )
        self.assertTrue(episode["versions"][0]["streams"][0]["selected"])
        self.assertTrue(episode["versions"][0]["streams"][2]["forced"])

    def test_stream_metadata_fills_missing_media_resolution(self):
        node = ET.fromstring("""
        <Video ratingKey="301" type="movie" title="Archive">
          <Media audioCodec="eac3">
            <Part size="1000">
              <Stream streamType="1" codec="hevc" width="1920" height="1080"
                      colorTrc="smpte2084"/>
              <Stream streamType="2" codec="eac3"/>
            </Part>
          </Media>
        </Video>
        """)
        versions = plex._media_versions(node)
        self.assertEqual(versions[0]["resolution"], "1080p")
        self.assertEqual(versions[0]["dynamic_range"], "HDR")
        self.assertEqual(versions[0]["video_codec"], "HEVC")

    def test_plex_part_unavailable_flag_is_preserved(self):
        node = ET.fromstring("""
        <Video ratingKey="401" type="movie" title="Missing">
          <Media videoResolution="1080">
            <Part file="/downloads/vortexo/Movies/Missing.mkv" exists="0"/>
          </Media>
        </Video>
        """)
        versions = plex._media_versions(node)
        self.assertFalse(versions[0]["available"])
        self.assertEqual(
            versions[0]["file"],
            "/downloads/vortexo/Movies/Missing.mkv",
        )

    def test_partial_refresh_only_requests_distinct_exact_paths(self):
        with patch.object(plex, "_plex_command") as command:
            refreshed = plex.refresh_plex_paths(
                "http://plex",
                "token",
                [
                    ("4", "/movies/Dune"),
                    ("4", "/movies/Dune"),
                    ("5", "/shows/Foundation"),
                ],
            )
        self.assertEqual(len(refreshed), 2)
        self.assertEqual(
            command.call_args_list[0].args[3],
            {"path": "/movies/Dune"},
        )
        self.assertEqual(
            command.call_args_list[1].args[3],
            {"path": "/shows/Foundation"},
        )

    def test_remote_library_preferences_stop_background_full_scans(self):
        with patch.object(plex, "_plex_command") as command:
            result = plex.configure_plex_remote_library("http://plex", "token")
        self.assertFalse(result["automatic_scans"])
        self.assertFalse(result["periodic_scans"])
        self.assertEqual(command.call_args.kwargs["method"], "PUT")
        self.assertEqual(command.call_args.args[2], "/:/prefs")
        self.assertEqual(command.call_args.args[3]["autoEmptyTrash"], "0")

    def test_cancel_scan_uses_plex_refresh_delete(self):
        with patch.object(plex, "_plex_command") as command:
            cancelled = plex.cancel_plex_scans(
                "http://plex", "token", ["4", "5"]
            )
        self.assertEqual(cancelled, ["4", "5"])
        self.assertEqual(command.call_count, 2)
        self.assertEqual(command.call_args.kwargs["method"], "DELETE")


if __name__ == "__main__":
    unittest.main()
