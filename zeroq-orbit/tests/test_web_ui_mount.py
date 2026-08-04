import importlib.util
import pathlib
import unittest
from unittest import mock


HOOK = pathlib.Path(__file__).parents[1] / "hooks" / "runtime" / "web_ui.py"
SPEC = importlib.util.spec_from_file_location("orbit_mount_web_ui", HOOK)
WEB_UI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WEB_UI)


class RcloneMountRemoteTests(unittest.TestCase):
    def mount_remote_for(self, mode):
        with mock.patch.object(WEB_UI, "read_config", return_value={"DEBRID_MODE": mode}):
            args = WEB_UI.Mount()._rclone_args()
        self.assertEqual(args[:2], ["rclone", "mount"])
        return args[2]

    def test_alldebrid_mounts_playable_magnets_directory(self):
        self.assertEqual(self.mount_remote_for("alldebrid"), "debrid:magnets")

    def test_alldebrid_mode_is_case_insensitive(self):
        self.assertEqual(self.mount_remote_for("AllDebrid"), "debrid:magnets")

    def test_other_providers_keep_their_existing_root_mount(self):
        for mode in ("webdav", "zurg", "premiumize"):
            with self.subTest(mode=mode):
                self.assertEqual(self.mount_remote_for(mode), "debrid:")


if __name__ == "__main__":
    unittest.main()
