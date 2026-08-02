"""Orbit HTTP API and static dashboard."""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .integrations import IntegrationError, search_tmdb
from .manifests import build_media_manifest
from .plex import cancel_plex_scans, configure_plex_remote_library, fetch_plex_artwork
from .store import Store
from .worker import Coordinator


DATA_DIR = os.environ.get("ORBIT_DATA_DIR", "/data")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
PORT = int(os.environ.get("ORBIT_PORT", "8080"))
MOUNT_API = os.environ.get("ORBIT_MOUNT_API", "http://mount:8080")
LEGACY_CONFIG = os.environ.get("PD_CONFIG_DIR", "/config")
VERSION = "0.5.4"
SECRET_KEYS = {
    "tmdb_api_key", "mdblist_api_key", "trakt_client_id", "torbox_api_key",
    "webdav_password", "realdebrid_api_key", "alldebrid_api_key", "plex_token",
    "prowlarr_api_key", "jackett_api_key", "orionoid_api_key",
}
DEFAULT_VERSION = [
    "1080p SDR",
    [["retries", "<=", "48"], ["media type", "all", ""]],
    "true",
    [
        ["cache status", "requirement", "cached", ""],
        ["resolution", "requirement", "<=", "1080"],
        ["resolution", "preference", "highest", ""],
        ["title", "requirement", "exclude", "([^A-Z0-9]|HD|HQ)(CAM|T(ELE)?(S(YNC)?|C(INE)?)|ADS|HINDI)([^A-Z0-9]|RIP|$)"],
        ["title", "requirement", "exclude", "(3D|DO?VI?|HDR)"],
        ["size", "preference", "highest", ""],
        ["seeders", "preference", "highest", ""],
        ["size", "requirement", ">=", "0.1"],
    ],
]

store = Store(os.path.join(DATA_DIR, "orbit.db"))
coordinator = Coordinator(store, DATA_DIR)


def _remote_json(url: str, method: str = "GET", payload: dict | None = None, timeout: int = 4):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except Exception as error:
        return {"ok": False, "error": str(error)}


def _sync_mount_settings(settings: dict):
    mode = settings.get("debrid_mode", "webdav")
    webdav_user = settings.get("webdav_username", "")
    webdav_pass = settings.get("webdav_password", "")
    # TorBox officially supports API-key WebDAV authentication with the fixed
    # username "torbox". Prefer it when available so SSO accounts and changed
    # account passwords do not leave the mount offline.
    if mode == "webdav" and settings.get("torbox_api_key"):
        webdav_user = "torbox"
        webdav_pass = settings["torbox_api_key"]
    # AllDebrid exposes a public WebDAV endpoint (webdav.debrid.it). Auth uses
    # the API key as the username and a dummy password ("eeeee").
    if mode == "alldebrid" and settings.get("alldebrid_api_key"):
        webdav_user = settings["alldebrid_api_key"]
        webdav_pass = "eeeee"
    payload = {
        "DEBRID_MODE": mode,
        "DEBRID_WEBDAV_URL": (
            "https://webdav.debrid.it/" if mode == "alldebrid"
            else settings.get("webdav_url", "https://webdav.torbox.app")
        ),
        "DEBRID_WEBDAV_VENDOR": "other",
        "DEBRID_WEBDAV_USER": webdav_user,
        "DEBRID_WEBDAV_PASS": webdav_pass,
        "DEBRID_ZURG_TOKEN": settings.get("realdebrid_api_key", ""),
        "DEBRID_ALLDEBRID_APIKEY": settings.get("alldebrid_api_key", ""),
        "DEBRID_RCLONE_VFS_CACHE_MODE": "off",
        "DEBRID_RCLONE_DIR_CACHE_TIME": "1m",
        "DEBRID_RCLONE_LOG_LEVEL": "INFO",
    }
    configured = _remote_json(MOUNT_API + "/api/config", "POST", payload)
    if configured.get("ok") is not True:
        return configured
    return _remote_json(MOUNT_API + "/api/mount/restart", "POST", {})


def _sync_legacy_settings(settings: dict):
    """Merge Orbit's core connections into plex_debrid settings.json."""
    path = os.path.join(LEGACY_CONFIG, "settings.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            legacy = json.load(handle)
    except (OSError, json.JSONDecodeError):
        legacy = {}
    provider = {
        "zurg": "Real Debrid",
        "alldebrid": "All Debrid",
    }.get(settings.get("debrid_mode"), "TorBox")
    plex_sections = [part.strip() for part in settings.get("plex_sections", "").split(",") if part.strip()]
    scrapers = [
        name for name in ("torrentio", "prowlarr", "jackett", "orionoid", "nyaa", "1337x")
        if str(settings.get(f"scraper_{name}", "true" if name == "torrentio" else "false")).lower()
        in {"1", "true", "yes", "on"}
    ]
    legacy.update({
        "Debrid Services": [provider],
        "TorBox API Key": settings.get("torbox_api_key", ""),
        "Real Debrid API Key": settings.get("realdebrid_api_key", ""),
        "All Debrid API Key": settings.get("alldebrid_api_key", ""),
        "Content Services": ["Plex"] if settings.get("plex_token") else [],
        "Plex users": [[settings.get("plex_username", "Orbit"), settings.get("plex_token", "")]] if settings.get("plex_token") else [],
        "Plex server address": settings.get("plex_url", ""),
        "Plex library refresh": plex_sections,
        "Plex library check": plex_sections,
        "Plex library partial scan": "true",
        "Plex library refresh delay": "0",
        "Library collection service": ["Plex Library"],
        # Orbit requests scans only after the mount and final link resolve.
        # This prevents transient FUSE outages from marking whole libraries
        # unavailable while the acquisition subprocess is finishing.
        "Library update services": [],
        "Library ignore services": ["Plex Discover Watch Status"],
        "Sources": scrapers or ["torrentio"],
        "Torrentio Scraper Parameters": settings.get("torrentio_url", "")
        or "https://torrentio.strem.fun/sort=qualitysize|qualityfilter=480p,scr,cam/manifest.json",
        "Prowlarr Base URL": settings.get("prowlarr_url", "http://127.0.0.1:9696"),
        "Prowlarr API Key": settings.get("prowlarr_api_key", ""),
        "Jackett Base URL": settings.get("jackett_url", "http://127.0.0.1:9117"),
        "Jackett API Key": settings.get("jackett_api_key", ""),
        "Orionoid API Key": settings.get("orionoid_api_key", ""),
        "Versions": legacy.get("Versions") or [DEFAULT_VERSION],
        "Symlinker Enabled": "true",
        "Symlinker Mount Path": "/downloads",
        "Symlinker Movies Library": "/downloads/vortexo/Movies",
        "Symlinker TV Library": "/downloads/vortexo/TV",
        "Show Menu on Startup": "false",
        "Debug printing": "true",
        "Log to file": "true",
    })
    os.makedirs(LEGACY_CONFIG, exist_ok=True)
    temporary = path + ".orbit.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(legacy, handle, indent=2)
    os.replace(temporary, path)


class Handler(BaseHTTPRequestHandler):
    server_version = "Orbit/0.1"

    def log_message(self, *_args):
        return

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, body: bytes, content_type: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _static(self, requested: str):
        filename = "index.html" if requested in ("", "/") else requested.lstrip("/")
        safe = posixpath.normpath(filename)
        if safe.startswith("../"):
            return self._json({"error": "not found"}, 404)
        path = os.path.join(STATIC_DIR, safe)
        if not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        with open(path, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/api/health":
            return self._json({"ok": True, "name": "Orbit", "version": VERSION})
        if path == "/api/dashboard":
            result = store.dashboard()
            result["mount"] = _remote_json(MOUNT_API + "/api/status")
            result["plex_library"] = store.plex_library_status()
            result["link_repair"] = coordinator.last_link_repair
            return self._json(result)
        if path == "/api/settings":
            return self._json(store.get_settings())
        if path == "/api/search":
            settings = store.get_settings(reveal_secrets=True)
            try:
                results = search_tmdb(
                    (query.get("q") or [""])[0], settings.get("tmdb_api_key", ""),
                    (query.get("type") or ["multi"])[0],
                )
                for item in results:
                    plex_item = store.match_plex_library(item)
                    if plex_item:
                        item["plex"] = {
                            "quality": plex_item["quality"],
                            "upgrade_available": plex_item["upgrade_available"],
                            "episode_count": plex_item["episode_count"],
                            "plex_rating_key": plex_item["plex_rating_key"],
                        }
                return self._json({"results": results})
            except IntegrationError as error:
                return self._json({"error": str(error)}, 400)
        if path == "/api/requests":
            return self._json({"requests": store.list_requests()})
        if path.startswith("/api/requests/") and path.endswith("/events"):
            try:
                request_id = int(path.split("/")[3])
            except (IndexError, ValueError):
                return self._json({"error": "invalid request"}, 400)
            return self._json({"events": store.events(request_id)})
        if path == "/api/lists":
            return self._json({"lists": store.list_sources()})
        if path.startswith("/api/library/") and path.endswith("/manifest"):
            try:
                item_id = int(path.split("/")[3])
            except (IndexError, ValueError):
                return self._json({"error": "invalid library item"}, 400)
            item = store.get_plex_library_item(item_id)
            if not item:
                return self._json({"error": "library item not found"}, 404)
            return self._json(build_media_manifest(item))
        if path.startswith("/api/library/") and path.count("/") == 3:
            try:
                item_id = int(path.split("/")[3])
            except (IndexError, ValueError):
                return self._json({"error": "invalid library item"}, 400)
            item = store.get_plex_library_item(item_id)
            if not item:
                return self._json({"error": "library item not found"}, 404)
            return self._json({"item": item})
        if path.startswith("/api/library/") and path.endswith("/artwork"):
            try:
                item_id = int(path.split("/")[3])
            except (IndexError, ValueError):
                return self._json({"error": "invalid library item"}, 400)
            item = store.get_plex_library_item(item_id)
            if not item:
                return self._json({"error": "library item not found"}, 404)
            settings = store.get_settings(reveal_secrets=True)
            try:
                artwork, content_type = fetch_plex_artwork(
                    settings.get("plex_url", ""),
                    settings.get("plex_token", ""),
                    item.get("thumb", ""),
                )
                return self._bytes(artwork, content_type)
            except IntegrationError as error:
                return self._json({"error": str(error)}, 404)
        if path == "/api/library":
            try:
                limit = int((query.get("limit") or ["120"])[0])
                offset = int((query.get("offset") or ["0"])[0])
            except ValueError:
                return self._json({"error": "invalid library page"}, 400)
            filters = {
                "query": (query.get("q") or [""])[0],
                "media_type": (query.get("type") or [""])[0],
                "quality": (query.get("quality") or [""])[0],
                "status": (query.get("status") or [""])[0],
            }
            return self._json({
                "items": store.list_plex_library(
                    **filters,
                    sort=(query.get("sort") or ["title"])[0],
                    limit=limit,
                    offset=offset,
                ),
                "stats": store.plex_library_stats(**filters),
                "sync": store.plex_library_status(),
            })
        if path.startswith("/api/mount/"):
            suffix = path.removeprefix("/api/mount")
            return self._json(_remote_json(MOUNT_API + "/api" + suffix))
        if path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        return self._static(path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._body()
        if body is None:
            return self._json({"error": "invalid JSON"}, 400)
        if path == "/api/settings":
            allowed = {
                "tmdb_api_key", "mdblist_api_key", "trakt_client_id", "list_poll_minutes",
                "debrid_mode", "torbox_api_key", "realdebrid_api_key", "alldebrid_api_key", "webdav_url",
                "webdav_username", "webdav_password", "plex_url", "plex_token", "plex_username",
                "plex_sections", "complete_aired_series", "series_completion_daily_limit",
                "plex_watchlist_enabled", "plex_watchlist_poll_minutes",
                "plex_watchlist_profile", "plex_watchlist_max_items",
                "plex_link_repair_enabled", "plex_link_repair_interval_minutes",
                "plex_link_repair_max_per_run",
                "scraper_torrentio", "scraper_prowlarr", "scraper_jackett",
                "scraper_orionoid", "scraper_nyaa", "scraper_1337x",
                "torrentio_url", "prowlarr_url", "prowlarr_api_key",
                "jackett_url", "jackett_api_key", "orionoid_api_key",
            }
            values = {key: value for key, value in body.items() if key in allowed}
            scraper_keys = {key for key in allowed if key.startswith("scraper_")}
            if scraper_keys.intersection(body):
                scraper_values = {
                    key: values.get(key, "false") for key in scraper_keys
                }
                if not any(
                    str(value).lower() in {"1", "true", "yes", "on"}
                    for value in scraper_values.values()
                ):
                    return self._json({"error": "Enable at least one scraper"}, 400)
                values.update(scraper_values)
            previous = store.get_settings(reveal_secrets=True)
            debrid_keys = {
                "debrid_mode", "torbox_api_key", "realdebrid_api_key",
                "alldebrid_api_key", "webdav_url", "webdav_username", "webdav_password",
            }
            debrid_defaults = {
                "debrid_mode": "webdav",
                "webdav_url": "https://webdav.torbox.app",
            }
            debrid_changed = any(
                key in values
                and values[key] != "••••••••"
                and str(values[key]) != str(
                    previous.get(key, debrid_defaults.get(key, ""))
                )
                for key in debrid_keys
            )
            store.set_settings(values, SECRET_KEYS)
            revealed = store.get_settings(reveal_secrets=True)
            _sync_legacy_settings(revealed)
            plex_protection = {}
            protection_enabled = str(
                revealed.get("plex_link_repair_enabled", "true")
            ).lower() in {"1", "true", "yes", "on"}
            if (
                protection_enabled
                and revealed.get("plex_url")
                and revealed.get("plex_token")
            ):
                try:
                    plex_protection = configure_plex_remote_library(
                        revealed["plex_url"], revealed["plex_token"]
                    )
                except IntegrationError as error:
                    plex_protection = {"error": str(error)}
            if debrid_changed:
                mount_result = _sync_mount_settings(revealed)
            else:
                mount_result = {
                    **_remote_json(MOUNT_API + "/api/status"),
                    "unchanged": True,
                }
            return self._json({
                "ok": True,
                "mount": mount_result,
                "plex_protection": plex_protection,
            })
        if path == "/api/requests":
            if not body.get("title") or not (body.get("tmdb_id") or body.get("imdb_id")):
                return self._json({"error": "title and a metadata ID are required"}, 400)
            plex_item = store.match_plex_library(body)
            is_upgrade = bool(body.get("upgrade"))
            if plex_item and not is_upgrade:
                return self._json({
                    "error": f"Already in Plex at {plex_item['quality']}",
                    "plex": plex_item,
                }, 409)
            if is_upgrade and (not plex_item or not plex_item["upgrade_available"]):
                return self._json({"error": "This Plex item does not need a 1080p upgrade"}, 409)
            if is_upgrade:
                body["profile"] = "1080p"
            request, created = store.add_request(
                body, source="manual-upgrade" if is_upgrade else "manual"
            )
            return self._json({"request": request, "created": created}, 201 if created else 200)
        if path == "/api/lists":
            if body.get("kind") not in ("mdblist", "trakt") or not body.get("name") or not body.get("url"):
                return self._json({"error": "name, type and list URL are required"}, 400)
            try:
                source = store.add_list_source(body)
            except Exception as error:
                return self._json({"error": str(error)}, 409)
            created = source.pop("created", True)
            return self._json(
                {"list": source, "created": created}, 201 if created else 200
            )
        if path.startswith("/api/lists/") and path.endswith("/sync"):
            try:
                source_id = int(path.split("/")[3])
                result = coordinator.sync_list(source_id)
                return self._json({"ok": True, **result})
            except (IndexError, ValueError):
                return self._json({"error": "invalid list"}, 400)
            except IntegrationError as error:
                return self._json({"error": str(error)}, 400)
        if path == "/api/library/sync":
            try:
                return self._json({"ok": True, **coordinator.sync_plex_library()})
            except IntegrationError as error:
                return self._json({"error": str(error)}, 400)
        if path == "/api/library/repair":
            try:
                return self._json({
                    "ok": True,
                    **coordinator.repair_plex_streams(),
                })
            except IntegrationError as error:
                return self._json({"error": str(error)}, 400)
        if path == "/api/library/cancel-scan":
            settings = store.get_settings(reveal_secrets=True)
            try:
                cancelled = cancel_plex_scans(
                    settings.get("plex_url", ""),
                    settings.get("plex_token", ""),
                    coordinator._section_ids(settings),
                )
                return self._json({"ok": True, "cancelled_sections": cancelled})
            except IntegrationError as error:
                return self._json({"error": str(error)}, 400)
        if path == "/api/plex-watchlist/sync":
            try:
                return self._json({
                    "ok": True,
                    **coordinator.sync_plex_watchlist(),
                })
            except IntegrationError as error:
                return self._json({"error": str(error)}, 400)
        if path.startswith("/api/library/") and path.endswith("/replace"):
            try:
                item_id = int(path.split("/")[3])
            except (IndexError, ValueError):
                return self._json({"error": "invalid library item"}, 400)
            item = store.get_plex_library_item(item_id)
            if not item:
                return self._json({"error": "library item not found"}, 404)
            try:
                request, created = store.queue_library_replacement(
                    item,
                    str(body.get("scope") or ""),
                    body.get("season_number"),
                    body.get("episode_number"),
                    str(body.get("profile") or "best"),
                )
            except (TypeError, ValueError) as error:
                return self._json({"error": str(error)}, 400)
            return self._json(
                {"request": request, "created": created},
                201 if created else 200,
            )
        if path == "/api/mount/restart":
            settings = store.get_settings(reveal_secrets=True)
            return self._json(_sync_mount_settings(settings))
        if path.startswith("/api/mount/"):
            suffix = path.removeprefix("/api/mount")
            return self._json(_remote_json(MOUNT_API + "/api/mount" + suffix, "POST", body))
        return self._json({"error": "not found"}, 404)


def run():
    coordinator.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Orbit listening on :{PORT}", flush=True)
    try:
        server.serve_forever()
    finally:
        coordinator.stop()
        server.server_close()
