#!/usr/bin/env python3
"""web_ui.py — browser config UI + rclone mount supervisor for Debrid Mount.

Single stdlib-only file (no pip deps). Runs as the container's foreground
process on :8080 (what the Umbrel app_proxy hits). It:
  - serves the dashboard SPA + JSON API
  - reads/writes debrid.env (shell KEY='VALUE' format, same as before)
  - validates TorBox WebDAV creds via `rclone lsd` before mounting
  - manages the rclone mount as a child subprocess (mount/unmount/restart)

Replaces the old `rclone serve http /status` + exec-mount pattern: the UI
stays up forever (so app_proxy always has a server), and the mount is a
child it controls.
"""
import json
import os
import shlex
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG_DIR = os.environ.get("DEBRID_CONFIG_DIR", "/config")
STATUS_DIR = os.environ.get("DEBRID_STATUS_DIR", "/status")
MOUNTPOINT = os.environ.get("DEBRID_MOUNTPOINT", "/downloads/.vortexo-source")
HOST_MOUNT_PATH = os.environ.get(
    "DEBRID_HOST_MOUNT_PATH", "/data/zeroq-media/.vortexo-source")
RAM_CACHE_DIR = os.environ.get("DEBRID_RAM_CACHE_DIR", "/rclone-cache")
CONFIG_FILE = os.path.join(CONFIG_DIR, "debrid.env")
RCLONE_CONFIG = os.path.join(CONFIG_DIR, "rclone.conf")
ZURG_CONFIG = os.path.join(CONFIG_DIR, "zurg.yml")
RCLONE_LOG = os.path.join(STATUS_DIR, "rclone.log")
HOST_SAFETY_MARKER = os.path.join(STATUS_DIR, "host-storage-safe")
LEGACY_RCLONE_CACHE = os.path.join(CONFIG_DIR, "rclone-cache")
LISTEN_PORT = int(os.environ.get("DEBRID_WEB_PORT", "8080"))
CACHE_MODE_KEY = "DEBRID_RCLONE_VFS_CACHE_MODE"
STREAM_BUFFER_SIZE = "32M"
STREAM_READ_WAIT = "250ms"
STARTUP_SAFETY_ERROR = None

# Config keys we manage, with friendly metadata for the UI.
# (env_key, label, control, default, help)
CONFIG_SCHEMA = [
    ("DEBRID_MODE", "Debrid Mode", "select", "webdav",
     {"options": ["webdav", "zurg", "alldebrid", "premiumize"],
      "help": "webdav = TorBox direct. zurg = Real-Debrid. alldebrid = AllDebrid WebDAV. premiumize = Premiumize WebDAV."}),
    # TorBox WebDAV
    ("DEBRID_WEBDAV_URL", "WebDAV URL", "text", "https://webdav.torbox.app",
     {"show_if": {"DEBRID_MODE": "webdav"}, "help": "TorBox's WebDAV endpoint."}),
    ("DEBRID_WEBDAV_VENDOR", "WebDAV Vendor", "text", "other",
     {"show_if": {"DEBRID_MODE": "webdav"}, "help": "rclone vendor: 'other' for TorBox."}),
    ("DEBRID_WEBDAV_USER", "TorBox WebDAV Username", "text", "",
     {"show_if": {"DEBRID_MODE": "webdav"},
      "help": 'Use your TorBox email, or "torbox" when using an API key.'}),
    ("DEBRID_WEBDAV_PASS", "TorBox WebDAV Password", "password", "",
     {"show_if": {"DEBRID_MODE": "webdav"},
      "help": "Use your TorBox account password, or your API key."}),
    # Real-Debrid via zurg
    ("DEBRID_ZURG_TOKEN", "Real-Debrid API Token", "password", "",
     {"show_if": {"DEBRID_MODE": "zurg"},
      "help": "real-debrid.com → Account → Get my API token."}),
    ("DEBRID_ZURG_PORT", "Zurg Port", "text", "9999",
     {"show_if": {"DEBRID_MODE": "zurg"}}),
    # AllDebrid WebDAV
    ("DEBRID_ALLDEBRID_APIKEY", "AllDebrid API Key", "password", "",
     {"show_if": {"DEBRID_MODE": "alldebrid"},
      "help": "AllDebrid API key (alldebrid.com → Settings → API key). Used as the WebDAV username."}),
    # Premiumize WebDAV
    ("DEBRID_PREMIUMIZE_APIKEY", "Premiumize API Key", "password", "",
     {"show_if": {"DEBRID_MODE": "premiumize"},
      "help": "Premiumize API key (premiumize.me → Account). Used as WebDAV password."}),
    ("DEBRID_PREMIUMIZE_CUSTOMERID", "Premiumize Customer ID", "text", "",
     {"show_if": {"DEBRID_MODE": "premiumize"},
      "help": "Premiumize customer ID. Used as WebDAV username."}),
    # Rclone tuning. Persistent file caching is intentionally disabled: the
    # debrid provider remains the source of truth and media streams directly.
    (CACHE_MODE_KEY, "Persistent Media Cache", "select", "off",
     {"options": ["off"],
      "help": "Disabled. Source media streams directly from the debrid WebDAV account and is not retained on Umbrel disk."}),
    ("DEBRID_RCLONE_DIR_CACHE_TIME", "Dir Cache Time", "text", "1m",
     {"help": "e.g. 1m. How long directory listings are cached."}),
    ("DEBRID_RCLONE_LOG_LEVEL", "Log Level", "select", "INFO",
     {"options": ["ERROR", "NOTICE", "INFO", "DEBUG"]}),
]

# Sample config written on first run.
SAMPLE_CONFIG = """\
# Debrid Mount config
#
# Default mode is direct WebDAV, which works for TorBox:
DEBRID_MODE='webdav'
DEBRID_WEBDAV_URL='https://webdav.torbox.app'
DEBRID_WEBDAV_VENDOR='other'
DEBRID_WEBDAV_USER=''
DEBRID_WEBDAV_PASS=''
#
# Real-Debrid via zurg is also supported:
# DEBRID_MODE='zurg'
# DEBRID_ZURG_TOKEN=''
# DEBRID_ZURG_PORT='9999'
#
# Rclone tuning (persistent source-media caching is disabled):
DEBRID_RCLONE_VFS_CACHE_MODE='off'
DEBRID_RCLONE_DIR_CACHE_TIME='1m'
DEBRID_RCLONE_LOG_LEVEL='INFO'
"""


# --------------------------------------------------------------------------
# debrid.env read/write (shell KEY='VALUE' format)
# --------------------------------------------------------------------------
_lock = threading.Lock()


def write_sample_config():
    """Seed debrid.env on first run so the UI has defaults to show."""
    if os.path.isfile(CONFIG_FILE):
        return
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as fh:
        fh.write(SAMPLE_CONFIG)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


def read_config():
    """Parse debrid.env into a {KEY: value} dict. Strips surrounding quotes."""
    cfg = {}
    if not os.path.isfile(CONFIG_FILE):
        return cfg
    with open(CONFIG_FILE, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            # Strip surrounding single or double quotes.
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            cfg[key] = val
    return cfg


def write_config(updates):
    """Merge updates into debrid.env, preserving the shell KEY='VALUE' format."""
    with _lock:
        cfg = read_config()
        cfg.update(updates)
        # This package guarantees that debrid source media is not retained in
        # persistent local storage. Never trust stale or submitted cache values.
        cfg[CACHE_MODE_KEY] = "off"
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as fh:
            fh.write("# Debrid Mount config (managed by the Web UI)\n")
            for key, label, control, default, meta in CONFIG_SCHEMA:
                val = cfg.get(key, default)
                fh.write("{}='{}'\n".format(key, val))
        os.replace(tmp, CONFIG_FILE)
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass


def config_for_ui():
    """Return the schema + current values, grouped for the UI."""
    cfg = read_config()
    fields = []
    for key, label, control, default, meta in CONFIG_SCHEMA:
        value = "off" if key == CACHE_MODE_KEY else cfg.get(key, default)
        fields.append({
            "key": key, "label": label, "control": control,
            "value": value,
            "options": meta.get("options"),
            "show_if": meta.get("show_if"),
            "help": meta.get("help"),
        })
    return {"fields": fields}


def enforce_no_local_media_config():
    """Migrate stale full/minimal/writes cache settings while keeping creds."""
    cfg = read_config()
    obsolete_keys = {
        "DEBRID_RCLONE_VFS_CACHE_MAX_SIZE",
        "DEBRID_RCLONE_VFS_CACHE_MAX_AGE",
    }
    updates = {}
    if cfg.get(CACHE_MODE_KEY, "off").lower() != "off":
        updates[CACHE_MODE_KEY] = "off"
    if cfg.get("DEBRID_RCLONE_DIR_CACHE_TIME", "10s") == "10s":
        updates["DEBRID_RCLONE_DIR_CACHE_TIME"] = "1m"
    if updates or obsolete_keys.intersection(cfg):
        write_config(updates)


def _remove_tree_no_follow(path, root_device):
    """Remove one cache tree without following links or crossing filesystems."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        os.unlink(path)
        return
    if info.st_dev != root_device:
        raise RuntimeError("refusing to cross a filesystem boundary")
    with os.scandir(path) as entries:
        for entry in entries:
            _remove_tree_no_follow(entry.path, root_device)
    os.rmdir(path)


def purge_legacy_rclone_cache():
    """Delete only the retired persistent VFS cache, never a link target."""
    if not os.path.lexists(LEGACY_RCLONE_CACHE):
        return
    info = os.lstat(LEGACY_RCLONE_CACHE)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        os.unlink(LEGACY_RCLONE_CACHE)
        return
    mounted_paths = _mountpoints_at_or_below(LEGACY_RCLONE_CACHE)
    if mounted_paths:
        raise RuntimeError("refusing to delete a mounted legacy cache")
    _remove_tree_no_follow(LEGACY_RCLONE_CACHE, info.st_dev)


def _decode_mount_path(value):
    return (value.replace("\\040", " ")
            .replace("\\011", "\t")
            .replace("\\134", "\\"))


def _mountinfo_entries():
    """Return [(mount_path, filesystem_type)] from this mount namespace."""
    entries = []
    try:
        with open("/proc/self/mountinfo") as fh:
            for line in fh:
                left, separator, right = line.rstrip().partition(" - ")
                if not separator:
                    continue
                fields = left.split()
                fs_fields = right.split()
                if len(fields) < 5 or not fs_fields:
                    continue
                entries.append((_decode_mount_path(fields[4]), fs_fields[0]))
    except OSError:
        return []
    return entries


def _mountpoints_at_or_below(path):
    """Find exact or nested mounts, including same-device bind mounts."""
    target = os.path.abspath(path).rstrip("/") or "/"
    prefix = target.rstrip("/") + "/"
    return [mount_path for mount_path, _ in _mountinfo_entries()
            if mount_path == target or mount_path.startswith(prefix)]


def _mount_filesystem_type(path):
    """Return the filesystem type for the longest mount containing path."""
    target = os.path.realpath(path)
    best_mount = ""
    best_type = None
    for mount_path, filesystem_type in _mountinfo_entries():
        prefix = mount_path.rstrip("/") + "/"
        if ((target == mount_path or target.startswith(prefix))
                and len(mount_path) >= len(best_mount)):
            best_mount = mount_path
            best_type = filesystem_type
    return best_type


def safety_status():
    legacy_cache_present = os.path.lexists(LEGACY_RCLONE_CACHE)
    host_storage_safe = False
    try:
        with open(HOST_SAFETY_MARKER) as marker:
            host_storage_safe = marker.read().strip() == os.path.dirname(HOST_MOUNT_PATH)
    except OSError:
        pass
    return {
        "ok": (STARTUP_SAFETY_ERROR is None
               and not legacy_cache_present
               and host_storage_safe),
        "legacy_cache_present": legacy_cache_present,
        "host_storage_safe": host_storage_safe,
        "error": STARTUP_SAFETY_ERROR,
    }


def ensure_ram_only_cache_dir():
    """Fail closed unless rclone's cache directory is backed by tmpfs."""
    os.makedirs(RAM_CACHE_DIR, mode=0o700, exist_ok=True)
    if _mount_filesystem_type(RAM_CACHE_DIR) != "tmpfs":
        raise RuntimeError("rclone cache directory is not RAM-backed tmpfs")


def ensure_empty_mountpoint():
    """Never hide pre-existing local files behind the remote mount."""
    os.makedirs(MOUNTPOINT, exist_ok=True)
    with os.scandir(MOUNTPOINT) as entries:
        if next(entries, None) is not None:
            raise RuntimeError("mountpoint contains local files")


# --------------------------------------------------------------------------
# rclone config generation (moved here from mount.sh)
# --------------------------------------------------------------------------
def write_rclone_config():
    """Generate rclone.conf from debrid.env. Returns True on success."""
    cfg = read_config()
    mode = cfg.get("DEBRID_MODE", "webdav").lower()
    if mode == "zurg":
        port = cfg.get("DEBRID_ZURG_PORT", "9999")
        body = "[debrid]\ntype = webdav\nurl = http://127.0.0.1:{}/dav\nvendor = other\n".format(port)
    elif mode == "alldebrid":
        # AllDebrid WebDAV: API key as username, dummy password "eeeee".
        apikey = cfg.get("DEBRID_ALLDEBRID_APIKEY", "")
        if not apikey:
            return False
        obscured = _rclone_obscure("eeeee")
        body = ("[debrid]\ntype = webdav\nurl = https://webdav.debrid.it/\nvendor = other\n"
                "user = {}\npass = {}\n".format(apikey, obscured))
    elif mode == "premiumize":
        # Premiumize WebDAV: customer_id as username, API key as password.
        apikey = cfg.get("DEBRID_PREMIUMIZE_APIKEY", "")
        customer_id = cfg.get("DEBRID_PREMIUMIZE_CUSTOMERID", "")
        if not apikey:
            return False
        obscured = _rclone_obscure(apikey)
        body = ("[debrid]\ntype = webdav\nurl = https://webdav.premiumize.me\nvendor = other\n"
                "user = {}\npass = {}\n".format(customer_id, obscured))
    else:
        user = cfg.get("DEBRID_WEBDAV_USER", "")
        password = cfg.get("DEBRID_WEBDAV_PASS", "")
        if not user or not password:
            return False
        # rclone stores passwords obscured (not encrypted — rclone's own scheme).
        obscured = _rclone_obscure(password)
        url = cfg.get("DEBRID_WEBDAV_URL", "https://webdav.torbox.app")
        vendor = cfg.get("DEBRID_WEBDAV_VENDOR", "other")
        body = ("[debrid]\ntype = webdav\nurl = {}\nvendor = {}\n"
                "user = {}\npass = {}\n".format(url, vendor, user, obscured))
    with _lock:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(RCLONE_CONFIG, "w") as fh:
            fh.write(body)
        try:
            os.chmod(RCLONE_CONFIG, 0o600)
        except OSError:
            pass
    return True


def _rclone_obscure(password):
    """Run `rclone obscure <password>` to get rclone's obfuscated form."""
    try:
        out = subprocess.run(["rclone", "obscure", password],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return password  # fallback: store plaintext (rclone accepts it with a warning)


def write_zurg_config():
    """Generate zurg.yml for Real-Debrid mode."""
    cfg = read_config()
    token = cfg.get("DEBRID_ZURG_TOKEN", "")
    if not token:
        return False
    port = cfg.get("DEBRID_ZURG_PORT", "9999")
    body = (
        "zurg: v1\n"
        "token: {}\n"
        "host: \"0.0.0.0\"\n"
        "port: {}\n"
        "check_for_changes_every_secs: 10\n"
        "enable_repair: true\n"
        "auto_delete_rar_torrents: true\n"
        "directories:\n"
        "  shows:\n"
        "    group_order: 20\n"
        "    group: media\n"
        "    filters:\n"
        "      - has_episodes: true\n"
        "  movies:\n"
        "    group_order: 30\n"
        "    group: media\n"
        "    only_show_the_biggest_file: true\n"
        "    filters:\n"
        "      - regex: /.*/\n"
    ).format(token, port)
    with _lock:
        with open(ZURG_CONFIG, "w") as fh:
            fh.write(body)
        try:
            os.chmod(ZURG_CONFIG, 0o600)
        except OSError:
            pass
    return True


# --------------------------------------------------------------------------
# rclone mount subprocess supervisor
# --------------------------------------------------------------------------
class Mount:
    def __init__(self):
        self.proc = None
        self.zurg_proc = None
        self._lock = threading.Lock()

    @property
    def is_mounted(self):
        # Check /proc/self/mountinfo for the mountpoint.
        try:
            with open("/proc/self/mountinfo") as fh:
                return (" " + MOUNTPOINT + " ") in fh.read()
        except OSError:
            return False

    def status(self):
        safety = safety_status()
        last_error = STARTUP_SAFETY_ERROR
        if not self.is_mounted and self.proc and self.proc.poll() is not None:
            log_lines = [line.strip() for line in tail(RCLONE_LOG, 20).splitlines() if line.strip()]
            if log_lines:
                last_error = log_lines[-1]
        return {
            "mounted": self.is_mounted,
            "mode": read_config().get("DEBRID_MODE", "webdav"),
            "vfs_cache_mode": "off",
            "persistent_media_cache": safety["legacy_cache_present"],
            "storage_safety_ok": safety["ok"],
            "storage_safety_error": safety["error"],
            "last_error": last_error,
        }

    def _rclone_args(self):
        cfg = read_config()
        return [
            "rclone", "mount", "debrid:", MOUNTPOINT,
            "--config", RCLONE_CONFIG,
            "--allow-other",
            "--read-only",
            "--dir-cache-time", cfg.get("DEBRID_RCLONE_DIR_CACHE_TIME", "10s"),
            "--vfs-cache-mode", "off",
            # Plex probes and reopens remote media before starting a stream.
            # Keep one bounded in-memory reader for the probe cycle and tolerate
            # Plex's short out-of-order read bursts before opening another range.
            "--buffer-size", STREAM_BUFFER_SIZE,
            "--vfs-read-wait", STREAM_READ_WAIT,
            "--attr-timeout", "30s",
            "--max-read-ahead", "4M",
            "--no-checksum",
            "--no-modtime",
            "--vfs-fast-fingerprint",
            # Some rclone subsystems still consult cache-dir even with VFS file
            # caching off. Keep it on a small tmpfs, never persistent storage.
            "--cache-dir", RAM_CACHE_DIR,
            "--poll-interval", "0",
            "--umask", "002",
            "--uid", "1000",
            "--gid", "1000",
            "--log-level", cfg.get("DEBRID_RCLONE_LOG_LEVEL", "INFO"),
        ]

    def _prepare(self):
        """FUSE prep (moved from mount.sh): allow_other, clean stale mount, mkdir."""
        if not safety_status()["host_storage_safe"]:
            raise RuntimeError("host storage safety hook did not complete")
        # enable user_allow_other in fuse.conf
        fuse_conf = "/etc/fuse.conf"
        if os.path.isfile(fuse_conf) and os.access(fuse_conf, os.W_OK):
            with open(fuse_conf) as fh:
                existing = fh.read()
            if "user_allow_other" not in existing:
                with open(fuse_conf, "a") as fh:
                    fh.write("user_allow_other\n")
        # unmount stale
        if self.is_mounted:
            subprocess.run(["fusermount3", "-uz", MOUNTPOINT], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["fusermount", "-uz", MOUNTPOINT], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if self.is_mounted:
            raise RuntimeError("stale FUSE mount could not be removed")
        purge_legacy_rclone_cache()
        ensure_empty_mountpoint()
        ensure_ram_only_cache_dir()

    def mount(self):
        with self._lock:
            if self.is_mounted:
                return False, "already mounted"
            cfg = read_config()
            mode = cfg.get("DEBRID_MODE", "webdav").lower()
            # Generate configs.
            if mode == "zurg":
                if not write_zurg_config():
                    return False, "missing Real-Debrid token"
            elif mode == "alldebrid":
                if not write_rclone_config():
                    return False, "missing AllDebrid API key"
                credential_result = test_webdav(
                    cfg.get("DEBRID_ALLDEBRID_APIKEY", ""),
                    "eeeee",
                    "https://webdav.debrid.it/",
                    "other",
                )
                if not credential_result.get("valid"):
                    return False, "AllDebrid WebDAV test failed: {}".format(
                        credential_result.get("error") or "authentication failed")
            elif mode == "premiumize":
                if not write_rclone_config():
                    return False, "missing Premiumize API key"
                credential_result = test_webdav(
                    cfg.get("DEBRID_PREMIUMIZE_CUSTOMERID", ""),
                    cfg.get("DEBRID_PREMIUMIZE_APIKEY", ""),
                    "https://webdav.premiumize.me",
                    "other",
                )
                if not credential_result.get("valid"):
                    return False, "Premiumize WebDAV test failed: {}".format(
                        credential_result.get("error") or "authentication failed")
            else:
                if not write_rclone_config():
                    return False, "missing TorBox WebDAV username/password"
                credential_result = test_webdav(
                    cfg.get("DEBRID_WEBDAV_USER", ""),
                    cfg.get("DEBRID_WEBDAV_PASS", ""),
                    cfg.get("DEBRID_WEBDAV_URL"),
                    cfg.get("DEBRID_WEBDAV_VENDOR", "other"),
                )
                if not credential_result.get("valid"):
                    return False, "TorBox WebDAV test failed: {}".format(
                        credential_result.get("error") or "authentication failed")
            try:
                self._prepare()
            except Exception as exc:
                global STARTUP_SAFETY_ERROR
                STARTUP_SAFETY_ERROR = str(exc)
                return False, "mount safety check failed: {}".format(exc)
            STARTUP_SAFETY_ERROR = None
            # Start zurg first if needed.
            if mode == "zurg":
                self.zurg_proc = subprocess.Popen(
                    ["zurg", "-c", ZURG_CONFIG],
                    stdout=open(RCLONE_LOG, "ab"), stderr=subprocess.STDOUT)
                # wait for zurg to be ready
                if not _wait_for_zurg(cfg.get("DEBRID_ZURG_PORT", "9999")):
                    return False, "zurg did not become ready"
                write_rclone_config()  # now writes the localhost:dav url
            log_fh = open(RCLONE_LOG, "ab")
            self.proc = subprocess.Popen(self._rclone_args(), stdout=log_fh,
                                         stderr=subprocess.STDOUT)
            # Give it a moment to either mount or crash.
            time.sleep(3)
            if self.proc.poll() is not None:
                return False, "rclone exited immediately — check creds / log"
            return True, "mounting"

    def unmount(self):
        with self._lock:
            msg_parts = []
            if self.is_mounted:
                subprocess.run(["fusermount3", "-uz", MOUNTPOINT], timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["fusermount", "-uz", MOUNTPOINT], timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                msg_parts.append("unmounted")
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                msg_parts.append("rclone stopped")
            self.proc = None
            if self.zurg_proc and self.zurg_proc.poll() is None:
                self.zurg_proc.terminate()
                self.zurg_proc = None
                msg_parts.append("zurg stopped")
            return True, ", ".join(msg_parts) if msg_parts else "not running"

    def restart(self):
        self.unmount()
        time.sleep(1)
        return self.mount()


def _wait_for_zurg(port, timeout=60):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen("http://127.0.0.1:{}/dav/version.txt".format(port), timeout=2)
            return True
        except Exception:
            time.sleep(1)
    return False


mount = Mount()


# --------------------------------------------------------------------------
# Credential test (rclone lsd)
# --------------------------------------------------------------------------
def test_webdav(user, password, url=None, vendor="other"):
    """Validate WebDAV creds by running `rclone lsd` against a temp config."""
    if not user or not password:
        return {"valid": False, "error": "username and password required"}
    obscured = _rclone_obscure(password)
    url = url or "https://webdav.torbox.app"
    tmp_conf = os.path.join(CONFIG_DIR, "test-rclone.conf")
    with open(tmp_conf, "w") as fh:
        fh.write("[debrid]\ntype = webdav\nurl = {}\nvendor = {}\nuser = {}\npass = {}\n".format(
            url, vendor, user, obscured))
    try:
        result = subprocess.run(
            ["rclone", "lsd", "debrid:/", "--config", tmp_conf, "--timeout", "30s"],
            capture_output=True, text=True, timeout=45)
        if result.returncode == 0:
            dirs = [l for l in result.stdout.strip().split("\n") if l.strip()]
            return {"valid": True, "dirs": len(dirs), "sample": dirs[:5]}
        return {"valid": False, "error": _clean_rclone_error(result.stderr)}
    except subprocess.TimeoutExpired:
        return {"valid": False, "error": "timed out connecting to TorBox WebDAV"}
    except Exception as e:
        return {"valid": False, "error": str(e)}
    finally:
        try:
            os.remove(tmp_conf)
        except OSError:
            pass


def _clean_rclone_error(stderr):
    """Extract the useful line from rclone's verbose error output."""
    for line in stderr.split("\n"):
        line = line.strip()
        if line and ("error" in line.lower() or "failed" in line.lower()
                     or "401" in line or "403" in line or "denied" in line.lower()):
            return line[:200]
    return (stderr.strip().split("\n")[-1] if stderr.strip() else "unknown error")[:200]


def tail(path, lines=100):
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as fh:
            return b"".join(fh.readlines()[-lines:]).decode("utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Debrid Mount</title>
<style>
:root{--bg:#020617;--card:rgba(15,23,42,.72);--border:rgba(255,255,255,.16);--text:#eef2ff;--muted:#94a3b8;--accent:#0ea5e9;--good:#84cc16;--bad:#ef4444}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;font:15px/1.5 ui-sans-serif,system-ui,sans-serif;color:var(--text);background:radial-gradient(circle at top left,#164e63,#020617 52%,#111827);padding-bottom:90px}
header{display:flex;align-items:center;justify-content:space-between;max-width:760px;margin:0 auto;padding:28px 22px 0}
.brand h1{margin:0;font-size:26px;letter-spacing:-.03em}.brand .sub{color:var(--muted);font-size:13px}
main{max-width:760px;margin:0 auto;padding:22px;display:grid;gap:18px}
.card{border:1px solid var(--border);border-radius:18px;background:var(--card);padding:22px;box-shadow:0 18px 50px rgba(0,0,0,.28)}
.card h2{margin:0 0 10px;font-size:17px}.muted{color:var(--muted)}.small{font-size:12px}
label{display:flex;flex-direction:column;gap:6px;margin-bottom:14px}
input,select{background:rgba(2,6,23,.6);border:1px solid var(--border);color:var(--text);padding:10px 12px;border-radius:10px;font:inherit;width:100%}
input:focus,select:focus{outline:none;border-color:var(--accent)}
.btn{background:rgba(14,165,233,.16);color:var(--text);border:1px solid var(--border);padding:10px 18px;border-radius:10px;font:inherit;cursor:pointer}
.btn:hover{background:rgba(14,165,233,.3)}
.btn--good{background:rgba(132,204,22,.18);border-color:rgba(132,204,22,.32)}.btn--good:hover{background:rgba(132,204,22,.32)}
.btn--bad{background:rgba(239,68,68,.16);border-color:rgba(239,68,68,.32)}
.btn--ghost{background:transparent}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.pill{display:inline-block;padding:6px 14px;border-radius:999px;font-size:13px;font-weight:600;border:1px solid var(--border)}
.pill--good{color:#d9f99d;background:rgba(132,204,22,.16)}.pill--bad{color:#fecaca;background:rgba(239,68,68,.16)}.pill--unknown{color:var(--muted)}
.savebar{position:fixed;bottom:0;left:0;right:0;display:flex;align-items:center;justify-content:center;gap:14px;padding:14px 22px;background:rgba(2,6,23,.92);border-top:1px solid var(--border);backdrop-filter:blur(8px);z-index:50}
.savebar.hidden{display:none}
.log{background:rgba(2,6,23,.7);border:1px solid var(--border);border-radius:10px;padding:14px;font:12px/1.5 ui-monospace,monospace;max-height:240px;overflow:auto;white-space:pre-wrap;color:#cbd5e1}
.ok{color:var(--good)}.err{color:var(--bad)}
</style></head><body>
<header><div class="brand"><h1>Debrid Mount</h1><span class="sub">TorBox / Real-Debrid filesystem</span></div>
<span id="pill" class="pill pill--unknown">checking…</span></header>
<main>
<section class="card"><h2>Status</h2>
<p class="muted" id="state">—</p>
<div class="row"><button class="btn btn--good" onclick="act('mount')">Mount</button>
<button class="btn btn--bad" onclick="act('unmount')">Unmount</button>
<button class="btn" onclick="act('restart')">Restart</button>
<button class="btn btn--ghost" onclick="loadLog()">Refresh log</button></div>
<pre class="log" id="log">loading…</pre></section>
<section class="card"><h2>Configuration</h2><div id="fields"></div></section>
</main>
<div class="savebar hidden" id="savebar"><span class="muted">Unsaved changes</span>
<button class="btn" onclick="save(false)">Save</button>
<button class="btn btn--good" onclick="save(true)">Save &amp; Mount</button></div>
<script>
const api=(p,o)=>fetch(p,o).then(r=>r.json());
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let schema=[],vals={},dirty=false;
function markDirty(){dirty=true;document.getElementById('savebar').classList.remove('hidden')}
function shouldShow(f){if(!f.show_if)return true;const[k,v]=Object.entries(f.show_if)[0];return vals[k]===v}
function render(){document.getElementById('fields').innerHTML=schema.filter(shouldShow).map(f=>{
  const v=esc(vals[f.key]??'');
  if(f.control==='select'){const o=(f.options||[]).map(x=>`<option value="${x}" ${vals[f.key]===x?'selected':''}>${x}</option>`).join('');return `<label>${esc(f.label)}<select data-key="${f.key}">${o}</select>${f.help?`<span class="muted small">${esc(f.help)}</span>`:''}</label>`}
  const t=f.control==='password'?'password':'text';
  const test=f.control==='password'&&f.key==='DEBRID_WEBDAV_PASS'?`<button type="button" class="btn btn--ghost" onclick="testCreds()">Test</button><span id="test-result"></span>`:'';
  return `<label>${esc(f.label)}<div class="row"><input type="${t}" data-key="${f.key}" value="${v}">${test}</div>${f.help?`<span class="muted small">${esc(f.help)}</span>`:''}</label>`}).join('');
  bind();}
function bind(){document.querySelectorAll('[data-key]').forEach(el=>el.addEventListener('input',()=>{snapshot();markDirty();if(el.tagName==='SELECT')render()}))}
function snapshot(){document.querySelectorAll('[data-key]').forEach(el=>vals[el.dataset.key]=el.value)}
async function load(){const c=await api('/api/config');schema=c.fields;c.fields.forEach(f=>vals[f.key]=f.value);render();refresh();}
async function save(andMount){snapshot();const r=await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(vals)});if(r.ok){document.getElementById('savebar').classList.add('hidden');dirty=false;if(andMount){const m=await api('/api/mount/restart',{method:'POST'});alert(m.message||'mounting')}refresh();}else alert('Save failed')}
async function act(a){const r=await api('/api/mount/'+a,{method:'POST'});refresh();}
async function testCreds(){snapshot();const r=await api('/api/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user:vals.DEBRID_WEBDAV_USER,password:vals.DEBRID_WEBDAV_PASS,url:vals.DEBRID_WEBDAV_URL,vendor:vals.DEBRID_WEBDAV_VENDOR})});const el=document.getElementById('test-result');if(r.valid)el.innerHTML=`<span class="ok">✓ ${r.dirs} folders found</span>`;else el.innerHTML=`<span class="err">✗ ${esc(r.error||'failed')}</span>`}
async function refresh(){try{const s=await api('/api/status');const p=document.getElementById('pill'),st=document.getElementById('state');if(s.mounted){p.className='pill pill--good';p.textContent='mounted';st.textContent=`mounted (${s.mode})`}else{p.className='pill pill--bad';p.textContent='not mounted';st.textContent='not mounted'}loadLog()}catch(e){}}
async function loadLog(){try{const r=await api('/api/log');document.getElementById('log').textContent=r.lines||'(empty)'}catch(e){}}
load();setInterval(refresh,5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "debrid-mount-ui/1.0"
    def log_message(self, *a): pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n: return {}
        try: return json.loads(self.rfile.read(n).decode())
        except: return None

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            body = PAGE_HTML.encode()
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path == "/api/health":
            safety = safety_status()
            return self._json(safety, 200 if safety["ok"] else 503)
        if path == "/api/config":
            return self._json(config_for_ui())
        if path == "/api/status":
            return self._json({**mount.status(), "configured": _is_configured()})
        if path == "/api/log":
            return self._json({"lines": tail(RCLONE_LOG, 100)})
        return self._json({"error":"not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/config":
            body = self._read_json()
            if body is None: return self._json({"error":"invalid json"}, 400)
            write_config(body); return self._json({"ok": True})
        if path == "/api/test":
            body = self._read_json() or {}
            return self._json(test_webdav(body.get("user",""), body.get("password",""),
                                          body.get("url"), body.get("vendor","other")))
        if path == "/api/mount/mount":
            ok, msg = mount.mount(); return self._json({"ok": ok, "message": msg, **mount.status()})
        if path == "/api/mount/unmount":
            ok, msg = mount.unmount(); return self._json({"ok": ok, "message": msg, **mount.status()})
        if path == "/api/mount/restart":
            ok, msg = mount.restart(); return self._json({"ok": ok, "message": msg, **mount.status()})
        return self._json({"error":"not found"}, 404)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


def _is_configured():
    cfg = read_config()
    mode = cfg.get("DEBRID_MODE", "webdav").lower()
    if mode == "zurg":
        return bool(cfg.get("DEBRID_ZURG_TOKEN"))
    if mode == "alldebrid":
        return bool(cfg.get("DEBRID_ALLDEBRID_APIKEY"))
    if mode == "premiumize":
        return bool(cfg.get("DEBRID_PREMIUMIZE_APIKEY"))
    return bool(cfg.get("DEBRID_WEBDAV_USER") and cfg.get("DEBRID_WEBDAV_PASS"))


def main():
    global STARTUP_SAFETY_ERROR
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(STATUS_DIR, exist_ok=True)
    write_sample_config()
    try:
        enforce_no_local_media_config()
        # Run even when credentials are not configured so old media cache data
        # is removed as soon as the updated package starts.
        purge_legacy_rclone_cache()
    except Exception as e:
        STARTUP_SAFETY_ERROR = str(e)
        print("[web_ui] local media cache cleanup failed: {}".format(e), file=sys.stderr)
    # Auto-mount on boot if already configured.
    if _is_configured():
        for attempt in range(1, 7):
            try:
                ok, message = mount.mount()
            except Exception as e:
                ok, message = False, str(e)
            if ok or message == "already mounted":
                print("[web_ui] auto-mount ready: {}".format(message), flush=True)
                break
            print(
                "[web_ui] auto-mount attempt {} failed: {}".format(attempt, message),
                file=sys.stderr,
                flush=True,
            )
            if "host storage safety hook did not complete" not in message or attempt == 6:
                break
            time.sleep(2)
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print("[web_ui] listening on :{}".format(LISTEN_PORT), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        mount.unmount()
        srv.server_close()


if __name__ == "__main__":
    main()
