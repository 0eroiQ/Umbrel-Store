from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .integrations import (
    IntegrationError,
    TorBoxClient,
    choose_video_file,
    deduplicate_streams,
    discover_children,
    discover_metadata,
    fetch_plex_watchlist,
    fetch_streams,
    json_request,
    normalise_title,
    plex_account,
    plex_cloud_headers,
    plex_owner_token,
    plex_server_headers,
    select_automatic_stream,
    torrent_completed,
)
from .store import Store


TERMINAL_JOB_STATES = {"plex_confirmed", "already_in_plex", "failed"}
VIDEO_EXTENSIONS = {
    ".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg",
    ".mts", ".ts", ".webm", ".wmv",
}


def _safe_name(value: str, fallback: str = "Media") -> str:
    value = re.sub(r"[\x00-\x1f/:*?\"<>|]+", " ", value or "")
    value = " ".join(value.split()).strip(" .")
    return value[:180] or fallback


def _inside(path: str, root: str) -> bool:
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _json_bytes(value) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class VortexoService:
    def __init__(self):
        self.data_dir = os.path.abspath(os.environ.get("VORTEXO_DATA_DIR", "/data/vortexo"))
        self.plex_preferences = os.environ.get(
            "VORTEXO_PLEX_PREFERENCES",
            "/plex-config/Library/Application Support/Plex Media Server/Preferences.xml",
        )
        self.plex_base_url = os.environ.get("VORTEXO_PLEX_URL", "http://127.0.0.1:32400").rstrip("/")
        self.source_root = os.path.abspath(
            os.environ.get("VORTEXO_SOURCE_ROOT", "/downloads/.vortexo-source")
        )
        self.movies_root = os.path.abspath(
            os.environ.get("VORTEXO_MOVIES_ROOT", "/downloads/vortexo/Movies")
        )
        self.tv_root = os.path.abspath(
            os.environ.get("VORTEXO_TV_ROOT", "/downloads/vortexo/TV")
        )
        self.mount_api = os.environ.get("VORTEXO_MOUNT_API", "http://127.0.0.1:32501").rstrip("/")
        self.store = Store(self.data_dir)
        self._sessions: dict[str, float] = {}
        self._sessions_lock = threading.RLock()
        self._job_threads: dict[str, threading.Thread] = {}
        self._job_lock = threading.RLock()
        self._transcodes: dict[str, subprocess.Popen] = {}
        self._transcode_lock = threading.RLock()
        self._watchlist_lock = threading.Lock()
        self._watchlist_stop = threading.Event()
        self._watchlist_thread: threading.Thread | None = None
        self.transcode_root = os.path.join(self.data_dir, "transcode")
        shutil.rmtree(self.transcode_root, ignore_errors=True)
        os.makedirs(self.transcode_root, mode=0o700, exist_ok=True)
        for job in self.store.resumable_jobs():
            self._start_library_job(job["id"])
        if os.environ.get("VORTEXO_DISABLE_AUTOMATION", "").lower() not in {
            "1", "true", "yes", "on",
        }:
            self._watchlist_thread = threading.Thread(
                target=self._watchlist_loop,
                name="vortexo-watchlist",
                daemon=True,
            )
            self._watchlist_thread.start()

    @property
    def owner_token(self) -> str:
        return plex_owner_token(self.plex_preferences)

    def establish_session(self, plex_token: str) -> str:
        owner = self.owner_token
        if not owner:
            raise IntegrationError("Plex owner token is not available yet")
        candidate = plex_token.strip()
        if not candidate:
            raise PermissionError("Plex owner session required")
        if not hmac.compare_digest(candidate, owner):
            owner_account = plex_account(owner)
            candidate_account = plex_account(candidate)
            same_owner = any(
                owner_account.get(field)
                and hmac.compare_digest(
                    str(owner_account[field]), str(candidate_account.get(field) or "")
                )
                for field in ("id", "uuid", "email")
            )
            if not same_owner:
                raise PermissionError("Plex owner session required")
        session_id = uuid.uuid4().hex
        with self._sessions_lock:
            self._sessions[session_id] = time.time() + 8 * 3600
            self._purge_sessions()
        return session_id

    def _purge_sessions(self):
        now = time.time()
        expired = [key for key, expires_at in self._sessions.items() if expires_at <= now]
        for key in expired:
            self._sessions.pop(key, None)

    def valid_session(self, session_id: str) -> bool:
        with self._sessions_lock:
            self._purge_sessions()
            expires_at = self._sessions.get(session_id)
            if not expires_at:
                return False
            self._sessions[session_id] = time.time() + 8 * 3600
            return True

    def public_status(self) -> dict:
        settings = self.store.settings()
        mount = {"online": False, "detail": "Mount supervisor unavailable"}
        plex = {"online": False, "detail": "Plex owner session is not ready"}
        torbox = {"online": False, "detail": "TorBox is not configured"}
        source_lookup = {"online": False, "detail": "Vortexo Sources is not configured"}
        owner_token = self.owner_token
        if owner_token:
            try:
                request = urllib.request.Request(
                    f"{self.plex_base_url}/identity",
                    headers=plex_server_headers(owner_token),
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    plex = {
                        "online": 200 <= getattr(response, "status", 200) < 300,
                        "detail": "Plex Media Server is available",
                    }
            except (urllib.error.URLError, TimeoutError):
                plex = {"online": False, "detail": "Plex Media Server is unavailable"}
        if settings.get("torbox_api_key"):
            try:
                torbox = TorBoxClient(settings["torbox_api_key"]).health()
            except IntegrationError as error:
                torbox = {"online": False, "detail": str(error)}
        manifests = settings.get("stream_manifest_urls") or []
        if manifests:
            try:
                manifest = json_request(manifests[0], timeout=8)
                resources = manifest.get("resources") if isinstance(manifest, dict) else []
                supports_streams = any(
                    str(item.get("name") if isinstance(item, dict) else item).lower() == "stream"
                    for item in (resources or [])
                )
                source_lookup = {
                    "online": supports_streams,
                    "detail": (
                        f"{len(manifests)} stream source configured"
                        if supports_streams
                        else "Configured source does not expose streams"
                    ),
                }
            except IntegrationError as error:
                source_lookup = {"online": False, "detail": str(error)}
        try:
            remote = json_request(f"{self.mount_api}/health", timeout=3)
            if isinstance(remote, dict):
                mount = remote
        except IntegrationError:
            pass
        return {
            "configured": bool(settings.get("torbox_api_key"))
            and bool(settings.get("stream_manifest_urls")),
            "torbox_configured": bool(settings.get("torbox_api_key")),
            "sources_configured": bool(settings.get("stream_manifest_urls")),
            "plex_ready": plex["online"],
            "plex": plex,
            "torbox": torbox,
            "source_lookup": source_lookup,
            "mount": mount,
            "watchlist": self.watchlist_public(include_items=False),
            "version": "0.1.0",
        }

    def settings_public(self) -> dict:
        settings = self.store.settings()
        return {
            "torbox_configured": bool(settings.get("torbox_api_key")),
            "stream_manifest_urls": list(settings.get("stream_manifest_urls") or []),
            "webdav_url": settings.get("webdav_url", "https://webdav.torbox.app"),
            "plex_watchlist_enabled": bool(settings.get("plex_watchlist_enabled", False)),
            "plex_watchlist_poll_minutes": int(
                settings.get("plex_watchlist_poll_minutes", 1)
            ),
            "plex_watchlist_profile": settings.get("plex_watchlist_profile", "best"),
            "plex_watchlist_max_items": int(
                settings.get("plex_watchlist_max_items", 100)
            ),
            "plex_watchlist_cached_only": bool(
                settings.get("plex_watchlist_cached_only", True)
            ),
            "plex_watchlist_max_size_gb": float(
                settings.get("plex_watchlist_max_size_gb", 80)
            ),
            "plex_watchlist_show_mode": settings.get(
                "plex_watchlist_show_mode", "first_episode"
            ),
            "watchlist": self.watchlist_public(include_items=False),
        }

    def update_settings(self, body: dict) -> dict:
        current = self.store.settings()
        values = {}
        if "torbox_api_key" in body:
            key = str(body.get("torbox_api_key") or "").strip()
            if key:
                values["torbox_api_key"] = key
            elif body.get("clear_torbox_api_key"):
                values["torbox_api_key"] = ""
        if "stream_manifest_urls" in body:
            raw_urls = body.get("stream_manifest_urls")
            if isinstance(raw_urls, str):
                raw_urls = [line.strip() for line in raw_urls.splitlines()]
            urls = []
            for value in raw_urls or []:
                value = str(value).strip()
                parsed = urllib.parse.urlparse(value)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise IntegrationError("Use a valid HTTP or HTTPS stream manifest URL")
                if value not in urls:
                    urls.append(value)
            values["stream_manifest_urls"] = urls
        if "webdav_url" in body:
            value = str(body.get("webdav_url") or "").strip()
            parsed = urllib.parse.urlparse(value)
            if parsed.scheme != "https" or not parsed.netloc:
                raise IntegrationError("TorBox WebDAV must use a valid HTTPS URL")
            values["webdav_url"] = value.rstrip("/")
        if "plex_watchlist_enabled" in body:
            values["plex_watchlist_enabled"] = bool(body.get("plex_watchlist_enabled"))
        if "plex_watchlist_poll_minutes" in body:
            poll_minutes = int(body.get("plex_watchlist_poll_minutes") or 1)
            if poll_minutes not in {1, 5, 15, 30, 60}:
                raise IntegrationError("Choose a supported Plex Watchlist interval")
            values["plex_watchlist_poll_minutes"] = poll_minutes
        if "plex_watchlist_profile" in body:
            profile = str(body.get("plex_watchlist_profile") or "best").lower()
            if profile not in {"best", "4k", "1080p"}:
                raise IntegrationError("Choose Best, 4K, or 1080p for Watchlist quality")
            values["plex_watchlist_profile"] = profile
        if "plex_watchlist_max_items" in body:
            max_items = int(body.get("plex_watchlist_max_items") or 100)
            if max_items < 1 or max_items > 1000:
                raise IntegrationError("Plex Watchlist item limit must be between 1 and 1000")
            values["plex_watchlist_max_items"] = max_items
        if "plex_watchlist_cached_only" in body:
            values["plex_watchlist_cached_only"] = bool(
                body.get("plex_watchlist_cached_only")
            )
        if "plex_watchlist_max_size_gb" in body:
            max_size_gb = float(body.get("plex_watchlist_max_size_gb") or 0)
            if max_size_gb < 0 or max_size_gb > 1000:
                raise IntegrationError("Watchlist maximum size must be between 0 and 1000 GB")
            values["plex_watchlist_max_size_gb"] = max_size_gb
        if "plex_watchlist_show_mode" in body:
            show_mode = str(body.get("plex_watchlist_show_mode") or "first_episode")
            if show_mode != "first_episode":
                raise IntegrationError("Only safe first-episode TV imports are currently supported")
            values["plex_watchlist_show_mode"] = show_mode
        if not values:
            return self.settings_public()
        updated = {**current, **values}
        if values.get("torbox_api_key"):
            TorBoxClient(updated["torbox_api_key"]).health()
        self.store.update_settings(values)
        if {"torbox_api_key", "webdav_url"}.intersection(values):
            try:
                json_request(f"{self.mount_api}/restart", method="POST", payload={}, timeout=5)
            except IntegrationError:
                # Saving configuration remains valid; mount health reports the real problem.
                pass
        return self.settings_public()

    def _watchlist_loop(self):
        if self._watchlist_stop.wait(10):
            return
        while not self._watchlist_stop.is_set():
            settings = self.store.settings()
            if settings.get("plex_watchlist_enabled", False):
                interval = max(
                    60,
                    int(settings.get("plex_watchlist_poll_minutes", 1)) * 60,
                )
                status = self.store.watchlist_status()
                last_run = max(
                    int(status.get("started_at") or 0),
                    int(status.get("completed_at") or 0),
                )
                if int(time.time()) - last_run >= interval:
                    try:
                        self.sync_watchlist()
                    except IntegrationError:
                        pass
            self._watchlist_stop.wait(15)

    def watchlist_public(self, *, include_items: bool = True) -> dict:
        settings = self.store.settings()
        result = {
            "enabled": bool(settings.get("plex_watchlist_enabled", False)),
            "poll_minutes": int(settings.get("plex_watchlist_poll_minutes", 1)),
            "profile": settings.get("plex_watchlist_profile", "best"),
            "cached_only": bool(settings.get("plex_watchlist_cached_only", True)),
            "show_mode": settings.get("plex_watchlist_show_mode", "first_episode"),
            "sync": self.store.watchlist_status(),
        }
        if include_items:
            result["items"] = self.store.watchlist_items(
                int(settings.get("plex_watchlist_max_items", 100))
            )
        return result

    @staticmethod
    def _watchlist_identity(media: dict) -> str:
        external_id = (
            media.get("tmdb_id")
            or media.get("imdb_id")
            or media.get("discover_id")
        )
        return f"{media.get('type') or 'movie'}:{external_id}"

    def _plex_has_media(self, media: dict) -> bool:
        title = str(media.get("parent_title") or media.get("title") or "")
        if not title:
            return False
        query = urllib.parse.urlencode({"query": title, "includeGuids": "1"})
        payload = json_request(
            f"{self.plex_base_url}/search?{query}",
            headers=plex_server_headers(self.owner_token),
            timeout=20,
        )
        rows = payload.get("MediaContainer", {}).get("Metadata") or []
        imdb_id = str(media.get("imdb_id") or "").lower()
        tmdb_id = str(media.get("tmdb_id") or "")
        for item in rows:
            key = str(item.get("key") or "")
            if not (item.get("librarySectionID") or key.startswith("/library/metadata/")):
                continue
            if self._matches_plex_identity(item, title, imdb_id, tmdb_id):
                return True
        return False

    def _watchlist_target(self, media: dict) -> tuple[dict, dict, int, int]:
        if media.get("type") != "show":
            return media, media, 0, 0
        episodes = [
            episode for episode in self.episodes(str(media.get("discover_id") or ""))
            if int(episode.get("season") or 0) > 0
            and int(episode.get("episode") or 0) > 0
        ]
        if not episodes:
            raise IntegrationError("Plex Discover did not return a regular TV episode")
        target = dict(episodes[0])
        target["type"] = "episode"
        target["parent_title"] = media.get("title") or target.get("parent_title")
        target["imdb_id"] = target.get("imdb_id") or media.get("imdb_id")
        target["tmdb_id"] = target.get("tmdb_id") or media.get("tmdb_id")
        season = int(target.get("season") or 0)
        episode = int(target.get("episode") or 0)
        return media, target, season, episode

    def sync_watchlist(self) -> dict:
        if not self._watchlist_lock.acquire(blocking=False):
            raise IntegrationError("Plex Watchlist sync is already running")
        counts = {
            "found": 0,
            "queued": 0,
            "skipped_existing": 0,
            "skipped_active": 0,
            "unavailable": 0,
        }
        self.store.begin_watchlist_sync()
        try:
            settings = self.store.settings()
            if not settings.get("plex_watchlist_enabled", False):
                raise IntegrationError("Enable Plex Watchlist automation in Vortexo settings")
            if not settings.get("torbox_api_key") or not settings.get("stream_manifest_urls"):
                raise IntegrationError("Connect TorBox and Vortexo Sources before Watchlist sync")
            limit = max(1, min(int(settings.get("plex_watchlist_max_items", 100)), 1000))
            items = fetch_plex_watchlist(self.owner_token, limit)
            counts["found"] = len(items)
            retry_delay = int(os.environ.get("VORTEXO_WATCHLIST_RETRY_SECONDS", "900"))
            now = int(time.time())
            for media in items:
                identity = self._watchlist_identity(media)
                record = self.store.upsert_watchlist_item(identity, media)
                job = self.store.job(str(record.get("job_id") or "")) if record.get("job_id") else None
                if job and job.get("status") not in TERMINAL_JOB_STATES:
                    counts["skipped_active"] += 1
                    continue
                if job and job.get("status") in {"plex_confirmed", "already_in_plex"}:
                    self.store.update_watchlist_item(
                        identity, job["status"], job["detail"], job_id=job["id"]
                    )
                    counts["skipped_existing"] += 1
                    continue
                if (
                    int(record.get("next_retry_at") or 0) > now
                    and record.get("status") in {"failed", "no_cached_release"}
                ):
                    counts["skipped_active"] += 1
                    continue
                try:
                    if self._plex_has_media(media):
                        self.store.update_watchlist_item(
                            identity,
                            "already_in_plex",
                            "Already available in a Plex library",
                        )
                        counts["skipped_existing"] += 1
                        continue
                    lookup_media, target_media, season, episode = self._watchlist_target(media)
                    streams, warnings = self._lookup_streams(
                        lookup_media, season, episode
                    )
                    selected = select_automatic_stream(
                        streams,
                        str(settings.get("plex_watchlist_profile", "best")),
                        cached_only=bool(
                            settings.get("plex_watchlist_cached_only", True)
                        ),
                        max_size_gb=float(
                            settings.get("plex_watchlist_max_size_gb", 80)
                        ),
                    )
                    if not selected:
                        detail = (
                            "No cached addable release matched the Watchlist profile"
                            if settings.get("plex_watchlist_cached_only", True)
                            else "No addable release matched the Watchlist profile"
                        )
                        if warnings:
                            detail = warnings[0]
                        self.store.update_watchlist_item(
                            identity,
                            "no_cached_release",
                            detail,
                            next_retry_at=now + retry_delay,
                            increment_attempts=True,
                        )
                        counts["unavailable"] += 1
                        continue
                    stored_stream = self.store.save_streams(
                        str(target_media.get("discover_id") or media["discover_id"]),
                        [selected],
                        ttl=24 * 3600,
                    )[0]
                    job, created = self._create_library_job_for_media(
                        str(target_media.get("discover_id") or media["discover_id"]),
                        target_media,
                        stored_stream["id"],
                        source="plex_watchlist",
                        retry_failed=True,
                    )
                    self.store.update_watchlist_item(
                        identity,
                        job.get("status") or "selected",
                        job.get("detail") or "Queued from Plex Watchlist",
                        job_id=job.get("id"),
                        increment_attempts=created,
                    )
                    counts["queued"] += int(created)
                    counts["skipped_active"] += int(not created)
                except IntegrationError as error:
                    self.store.update_watchlist_item(
                        identity,
                        "failed",
                        str(error),
                        next_retry_at=now + retry_delay,
                        increment_attempts=True,
                    )
                    counts["unavailable"] += 1
            detail = (
                f"Found {counts['found']}; queued {counts['queued']}; "
                f"already in Plex {counts['skipped_existing']}; "
                f"waiting {counts['skipped_active']}; unavailable {counts['unavailable']}"
            )
            self.store.complete_watchlist_sync("completed", detail, counts)
            return {**counts, "status": "completed", "detail": detail}
        except IntegrationError as error:
            self.store.complete_watchlist_sync("failed", str(error), counts)
            raise
        finally:
            self._watchlist_lock.release()

    def media(self, discover_id: str) -> dict:
        return discover_metadata(discover_id, self.owner_token)

    def episodes(self, discover_id: str) -> list[dict]:
        media = self.media(discover_id)
        if media["type"] == "episode":
            return [media]
        if media["type"] != "show":
            return []
        path = media.get("key") or f"/library/metadata/{media['discover_id']}/children"
        if not path.endswith("/children"):
            path = path.rstrip("/") + "/children"
        seasons = discover_children(path, self.owner_token)
        episodes = []
        for season in seasons:
            if season.get("type") == "episode":
                episodes.append(season)
                continue
            season_path = season.get("key") or ""
            if not season_path:
                continue
            if not season_path.endswith("/children"):
                season_path = season_path.rstrip("/") + "/children"
            for episode in discover_children(season_path, self.owner_token):
                if episode.get("type") == "episode":
                    episodes.append(episode)
        return sorted(
            episodes,
            key=lambda item: (int(item.get("season") or 0), int(item.get("episode") or 0)),
        )

    def streams(self, body: dict) -> dict:
        discover_id = str(body.get("discover_id") or "")
        media = self.media(discover_id)
        lookup_media = media
        if media.get("type") == "episode" and media.get("grandparent_rating_key"):
            lookup_media = self.media(str(media["grandparent_rating_key"]))
        season = int(body.get("season") or media.get("season") or 0)
        episode = int(body.get("episode") or media.get("episode") or 0)
        playback_discover_id = str(
            body.get("episode_discover_id") or media.get("discover_id") or discover_id
        )
        streams, errors = self._lookup_streams(lookup_media, season, episode)
        public = self.store.save_streams(media["discover_id"], streams)
        return {
            "matched": bool(media.get("imdb_id") or media.get("tmdb_id")),
            "available": bool(public),
            "media": media,
            "playback_discover_id": playback_discover_id,
            "streams": public,
            "warnings": errors,
        }

    def _lookup_streams(
        self,
        media: dict,
        season: int = 0,
        episode: int = 0,
    ) -> tuple[list[dict], list[str]]:
        settings = self.store.settings()
        manifests = settings.get("stream_manifest_urls") or []
        if not manifests:
            raise IntegrationError("Add a Vortexo Sources manifest URL first")
        all_streams = []
        errors = []
        for manifest_url in manifests:
            try:
                all_streams.extend(fetch_streams(manifest_url, media, season, episode))
            except IntegrationError as error:
                errors.append(str(error))
        streams = deduplicate_streams(all_streams)
        api_key = settings.get("torbox_api_key", "")
        if api_key:
            hashes = [stream.get("info_hash") for stream in streams if stream.get("info_hash")]
            if hashes:
                try:
                    cached = TorBoxClient(api_key).check_cached(hashes)
                    for stream in streams:
                        info_hash = stream.get("info_hash")
                        if info_hash and cached.get(info_hash):
                            stream["cached"] = True
                except IntegrationError as error:
                    errors.append(str(error))
        streams = deduplicate_streams(streams)
        return streams, errors

    def _resolve_stream_url(self, stream: dict) -> str:
        if stream.get("url"):
            return str(stream["url"])
        settings = self.store.settings()
        client = TorBoxClient(settings.get("torbox_api_key", ""))
        torrent = client.find_torrent(
            stream.get("info_hash", ""),
            stream.get("file_name", ""),
            stream.get("torbox_id"),
        )
        if not torrent:
            raise IntegrationError("This release is not ready in TorBox")
        video = choose_video_file(
            torrent,
            int(stream.get("season") or 0),
            int(stream.get("episode") or 0),
            stream.get("file_idx"),
        )
        if not video:
            raise IntegrationError("TorBox release has no matching video file")
        torrent_id = torrent.get("id") or torrent.get("torrent_id")
        return client.request_download_url(torrent_id, video["file_id"])

    def create_play_session(self, body: dict) -> dict:
        stream_id = str(body.get("stream_id") or "")
        stream = self.store.stream(stream_id)
        if not stream:
            raise IntegrationError("Stream selection expired; search again")
        url = self._resolve_stream_url(stream)
        suffix = os.path.splitext(urllib.parse.urlparse(url).path or stream.get("file_name", ""))[1].lower()
        incompatible = (
            suffix not in {".mp4", ".m4v", ".mov", ".webm"}
            or stream.get("codec") in {"HEVC", "AV1"}
            or stream.get("audio") in {"TrueHD", "DTS", "DTS-HD"}
        )
        payload = {
            "url": url,
            "headers": stream.get("headers") or {},
            "mode": "hls" if incompatible else "direct",
            "file_name": stream.get("file_name") or "TorBox stream",
        }
        session_id = self.store.create_play_session(
            str(body.get("discover_id") or ""),
            stream_id,
            payload,
        )
        return {
            "session_id": session_id,
            "mode": payload["mode"],
            "play_url": (
                f"/vortexo/play/{session_id}/master.m3u8"
                if incompatible
                else f"/vortexo/play/{session_id}/direct"
            ),
            "resume": self.store.progress(str(body.get("discover_id") or "")),
        }

    def save_progress(self, body: dict) -> dict:
        discover_id = str(body.get("discover_id") or "")
        if not discover_id:
            raise IntegrationError("Missing Discover ID")
        position_ms = int(body.get("position_ms") or 0)
        duration_ms = int(body.get("duration_ms") or 0)
        completed = bool(duration_ms > 0 and position_ms / duration_ms >= 0.9)
        saved = self.store.save_progress(discover_id, position_ms, duration_ms, completed)
        if completed and self.owner_token:
            self._mark_discover_watched(discover_id)
        local_rating_key = self._job_rating_key_for_discover(discover_id)
        if local_rating_key:
            self._send_local_timeline(local_rating_key, position_ms, duration_ms, completed)
        return saved

    def _mark_discover_watched(self, discover_id: str):
        params = {
            "key": discover_id,
            "identifier": "tv.plex.provider.discover",
        }
        url = "https://discover.provider.plex.tv/actions/scrobble?" + urllib.parse.urlencode(params)
        try:
            json_request(
                url,
                method="PUT",
                headers=plex_cloud_headers(self.owner_token),
                payload={},
            )
        except IntegrationError:
            pass

    def _send_local_timeline(
        self,
        rating_key: str,
        position_ms: int,
        duration_ms: int,
        completed: bool,
    ):
        params = {
            "ratingKey": rating_key,
            "key": f"/library/metadata/{rating_key}",
            "state": "stopped" if completed else "playing",
            "time": max(0, position_ms),
            "duration": max(0, duration_ms),
        }
        try:
            timeline = urllib.request.Request(
                f"{self.plex_base_url}/:/timeline?{urllib.parse.urlencode(params)}",
                headers=plex_server_headers(self.owner_token),
            )
            urllib.request.urlopen(timeline, timeout=10).read()
            if completed:
                scrobble_params = urllib.parse.urlencode(
                    {
                        "key": rating_key,
                        "identifier": "com.plexapp.plugins.library",
                    }
                )
                scrobble = urllib.request.Request(
                    f"{self.plex_base_url}/:/scrobble?{scrobble_params}",
                    headers=plex_server_headers(self.owner_token),
                )
                urllib.request.urlopen(scrobble, timeout=10).read()
        except (urllib.error.URLError, TimeoutError):
            pass

    def _job_rating_key_for_discover(self, discover_id: str) -> str:
        with self.store.connection() as db:
            row = db.execute(
                """
                SELECT plex_rating_key FROM library_jobs
                WHERE discover_id=? AND plex_rating_key IS NOT NULL
                ORDER BY updated_at DESC LIMIT 1
                """,
                (discover_id,),
            ).fetchone()
        return row["plex_rating_key"] if row else ""

    def create_library_job(self, body: dict) -> tuple[dict, bool]:
        stream_id = str(body.get("stream_id") or "")
        discover_id = str(body.get("discover_id") or "")
        media = self.media(discover_id)
        if body.get("season"):
            media["season"] = int(body["season"])
        if body.get("episode"):
            media["episode"] = int(body["episode"])
        return self._create_library_job_for_media(
            discover_id,
            media,
            stream_id,
            source="manual",
            retry_failed=False,
        )

    def _create_library_job_for_media(
        self,
        discover_id: str,
        media: dict,
        stream_id: str,
        *,
        source: str,
        retry_failed: bool,
    ) -> tuple[dict, bool]:
        stream = self.store.stream(stream_id)
        if not stream:
            raise IntegrationError("Stream selection expired; search again")
        if not stream.get("can_add"):
            raise IntegrationError("This stream is playback-only and cannot be added to Plex")
        dedupe_key = "|".join(
            [
                discover_id,
                str(media.get("season") or 0),
                str(media.get("episode") or 0),
                str(
                    stream.get("torbox_id")
                    or stream.get("info_hash")
                    or stream.get("magnet")
                    or stream.get("file_name")
                ),
            ]
        )
        job, created = self.store.create_or_get_job(
            dedupe_key,
            discover_id,
            stream_id,
            {"media": media, "stream": stream, "source": source},
        )
        if not created and retry_failed and job.get("status") == "failed":
            retried = self.store.retry_job(
                job["id"], "Retrying release from Plex Watchlist"
            )
            if retried:
                job = retried
                created = True
        if created:
            self._start_library_job(job["id"])
        return job, created

    def _start_library_job(self, job_id: str):
        with self._job_lock:
            existing = self._job_threads.get(job_id)
            if existing and existing.is_alive():
                return
            thread = threading.Thread(
                target=self._run_library_job,
                args=(job_id,),
                name=f"vortexo-library-{job_id[:8]}",
                daemon=True,
            )
            self._job_threads[job_id] = thread
        thread.start()

    def _run_library_job(self, job_id: str):
        try:
            payload = self.store.job_payload(job_id)
            if not payload:
                return
            media = payload["media"]
            stream = payload["stream"]
            settings = self.store.settings()
            client = TorBoxClient(settings.get("torbox_api_key", ""))
            torrent = client.find_torrent(
                stream.get("info_hash", ""),
                stream.get("file_name", ""),
                stream.get("torbox_id"),
            )
            if torrent:
                self.store.transition(
                    job_id,
                    "torbox_accepted",
                    "Release already exists in TorBox",
                )
            else:
                result = client.create_torrent(stream.get("magnet") or "")
                self.store.transition(
                    job_id,
                    "torbox_accepted",
                    result.get("detail") or "TorBox accepted the release",
                )
            torrent = self._wait_for_torrent(client, stream, job_id)
            self.store.transition(job_id, "debrid_ready", "TorBox release is ready")
            video = choose_video_file(
                torrent,
                int(media.get("season") or 0),
                int(media.get("episode") or 0),
                stream.get("file_idx"),
            )
            if not video:
                raise IntegrationError("No matching video file was found in the TorBox release")
            source_path = self._wait_for_mount_file(
                str(torrent.get("name") or ""),
                video["path"],
                job_id,
            )
            self.store.transition(job_id, "mount_visible", "Exact TorBox file is visible")
            link_path, existed = self._link_media(media, stream, source_path)
            self.store.transition(
                job_id,
                "linked",
                (
                    "This exact TorBox version is already linked"
                    if existed
                    else "Added a new Plex media version"
                ),
                payload_updates={"link_path": link_path},
            )
            section_id = self._refresh_plex(media, os.path.dirname(link_path))
            self.store.transition(
                job_id,
                "plex_scan_requested",
                "Plex is scanning the exact media folder",
                payload_updates={"section_id": section_id},
            )
            rating_key = self._wait_for_plex(media, link_path)
            self.store.transition(
                job_id,
                "already_in_plex" if existed else "plex_confirmed",
                (
                    "This exact TorBox version is already in Plex"
                    if existed
                    else "Plex confirmed the media version"
                ),
                plex_rating_key=rating_key,
            )
        except Exception as error:
            detail = str(error) if isinstance(error, IntegrationError) else "Library job failed"
            self.store.transition(job_id, "failed", detail)
        finally:
            final = self.store.job(job_id)
            if final and final.get("status") in TERMINAL_JOB_STATES:
                retry_at = (
                    int(time.time())
                    + int(os.environ.get("VORTEXO_WATCHLIST_RETRY_SECONDS", "900"))
                    if final["status"] == "failed"
                    else 0
                )
                self.store.update_watchlist_for_job(
                    job_id,
                    final["status"],
                    final["detail"],
                    next_retry_at=retry_at,
                )
            with self._job_lock:
                self._job_threads.pop(job_id, None)

    def _wait_for_torrent(self, client: TorBoxClient, stream: dict, job_id: str) -> dict:
        deadline = time.time() + int(os.environ.get("VORTEXO_TORBOX_WAIT_SECONDS", "7200"))
        while time.time() < deadline:
            torrent = client.find_torrent(
                stream.get("info_hash", ""),
                stream.get("file_name", ""),
                stream.get("torbox_id"),
            )
            if torrent and torrent_completed(torrent):
                return torrent
            state = str((torrent or {}).get("download_state") or "waiting")
            self.store.transition(job_id, "torbox_accepted", f"TorBox state: {state}")
            time.sleep(15)
        raise IntegrationError("Timed out waiting for TorBox to finish the release")

    def _wait_for_mount_file(self, torrent_name: str, relative_path: str, job_id: str) -> str:
        deadline = time.time() + int(os.environ.get("VORTEXO_MOUNT_WAIT_SECONDS", "1800"))
        candidates = [
            os.path.join(self.source_root, torrent_name, relative_path),
            os.path.join(self.source_root, relative_path),
            os.path.join(self.source_root, torrent_name, os.path.basename(relative_path)),
        ]
        while time.time() < deadline:
            for candidate in candidates:
                if os.path.isfile(candidate) and _inside(candidate, self.source_root):
                    return candidate
            # TorBox names can be normalized differently in WebDAV. Search only
            # the matching top-level folder, never the full mount indiscriminately.
            wanted = normalise_title(torrent_name)
            try:
                folders = os.listdir(self.source_root)
            except OSError:
                folders = []
            for folder in folders:
                if normalise_title(folder) != wanted:
                    continue
                root = os.path.join(self.source_root, folder)
                for current, directories, files in os.walk(root):
                    directories.sort()
                    for filename in sorted(files):
                        candidate = os.path.join(current, filename)
                        if (
                            filename == os.path.basename(relative_path)
                            and os.path.splitext(filename)[1].lower() in VIDEO_EXTENSIONS
                            and _inside(candidate, self.source_root)
                        ):
                            return candidate
            self.store.transition(job_id, "debrid_ready", "Waiting for the exact WebDAV file")
            time.sleep(10)
        raise IntegrationError("TorBox finished, but the exact file did not appear in WebDAV")

    def _link_media(self, media: dict, stream: dict, source_path: str) -> tuple[str, bool]:
        if not _inside(source_path, self.source_root) or not os.path.isfile(source_path):
            raise IntegrationError("Refusing a media source outside the TorBox mount")
        suffix = os.path.splitext(source_path)[1].lower()
        if suffix not in VIDEO_EXTENSIONS:
            raise IntegrationError("Refusing a non-video TorBox source")
        fingerprint = (stream.get("info_hash") or hashlib.sha256(source_path.encode()).hexdigest())[:8]
        quality = _safe_name(stream.get("quality") or "TorBox")
        if media.get("type") in {"show", "episode"} or media.get("season"):
            title = _safe_name(media.get("parent_title") or media.get("title"))
            season = int(media.get("season") or 0)
            episode = int(media.get("episode") or 0)
            folder = os.path.join(self.tv_root, title, f"Season {season:02d}")
            filename = f"{title} - S{season:02d}E{episode:02d} - {quality} - {fingerprint}{suffix}"
            root = self.tv_root
        else:
            title = _safe_name(media.get("title"))
            year = f" ({media['year']})" if media.get("year") else ""
            folder = os.path.join(self.movies_root, f"{title}{year}")
            filename = f"{title}{year} - {quality} - {fingerprint}{suffix}"
            root = self.movies_root
        if not _inside(folder, root):
            raise IntegrationError("Refusing an unsafe Plex library destination")
        os.makedirs(folder, mode=0o775, exist_ok=True)
        link_path = os.path.join(folder, filename)
        if os.path.islink(link_path):
            if os.path.realpath(link_path) == os.path.realpath(source_path):
                return link_path, True
            raise IntegrationError("A different symlink already uses the target filename")
        if os.path.exists(link_path):
            raise IntegrationError("Refusing to replace an existing Plex file")
        temporary = f"{link_path}.vortexo-{uuid.uuid4().hex}"
        try:
            os.symlink(source_path, temporary)
            if not os.path.isfile(temporary):
                raise IntegrationError("New TorBox link did not resolve")
            os.replace(temporary, link_path)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
        return link_path, False

    def _plex_sections(self) -> list[dict]:
        url = f"{self.plex_base_url}/library/sections"
        payload = json_request(url, headers=plex_server_headers(self.owner_token))
        container = payload.get("MediaContainer", {}) if isinstance(payload, dict) else {}
        return container.get("Directory") or []

    def _refresh_plex(self, media: dict, folder: str) -> str:
        wanted_type = "show" if media.get("type") in {"show", "episode"} or media.get("season") else "movie"
        section = next(
            (item for item in self._plex_sections() if item.get("type") == wanted_type),
            None,
        )
        if not section:
            raise IntegrationError(f"No Plex {wanted_type} library section is configured")
        section_id = str(section.get("key") or "")
        query = urllib.parse.urlencode({"path": folder})
        try:
            request = urllib.request.Request(
                f"{self.plex_base_url}/library/sections/{section_id}/refresh?{query}",
                headers=plex_server_headers(self.owner_token),
            )
            urllib.request.urlopen(request, timeout=20).read()
        except (urllib.error.URLError, TimeoutError) as error:
            raise IntegrationError(f"Plex folder scan failed: {error}") from error
        return section_id

    @staticmethod
    def _matches_plex_identity(item: dict, title: str, imdb_id: str, tmdb_id: str) -> bool:
        values = [str(item.get("guid") or "")]
        values.extend(
            str(entry.get("id") or "")
            for entry in item.get("Guid") or []
            if isinstance(entry, dict)
        )
        joined = " ".join(values).lower()
        return bool(
            (imdb_id and imdb_id in joined)
            or (tmdb_id and re.search(rf"tmdb(?:://|:){re.escape(tmdb_id)}\b", joined))
            or str(item.get("title") or "").casefold() == title.casefold()
        )

    def _episode_rating_key(self, show_rating_key: str, season: int, episode: int) -> str:
        payload = json_request(
            f"{self.plex_base_url}/library/metadata/{show_rating_key}/allLeaves",
            headers=plex_server_headers(self.owner_token),
            timeout=20,
        )
        rows = payload.get("MediaContainer", {}).get("Metadata") or []
        for item in rows:
            if (
                int(item.get("parentIndex") or 0) == season
                and int(item.get("index") or 0) == episode
            ):
                return str(item.get("ratingKey") or "")
        return ""

    def _plex_item_contains_file(self, rating_key: str, link_path: str) -> bool:
        payload = json_request(
            f"{self.plex_base_url}/library/metadata/{rating_key}",
            headers=plex_server_headers(self.owner_token),
            timeout=20,
        )
        rows = payload.get("MediaContainer", {}).get("Metadata") or []
        expected = os.path.normpath(link_path)
        for item in rows:
            for media in item.get("Media") or []:
                for part in media.get("Part") or []:
                    if os.path.normpath(str(part.get("file") or "")) == expected:
                        return True
        return False

    def _wait_for_plex(self, media: dict, link_path: str) -> str:
        deadline = time.time() + int(os.environ.get("VORTEXO_PLEX_WAIT_SECONDS", "600"))
        title = str(media.get("parent_title") or media.get("title") or "")
        wanted_imdb = str(media.get("imdb_id") or "").lower()
        wanted_tmdb = str(media.get("tmdb_id") or "")
        season = int(media.get("season") or 0)
        episode = int(media.get("episode") or 0)
        while time.time() < deadline:
            query = urllib.parse.urlencode(
                {
                    "query": title,
                    "includeGuids": "1",
                }
            )
            try:
                payload = json_request(
                    f"{self.plex_base_url}/search?{query}",
                    headers=plex_server_headers(self.owner_token),
                    timeout=20,
                )
            except IntegrationError:
                time.sleep(10)
                continue
            rows = payload.get("MediaContainer", {}).get("Metadata") or []
            for item in rows:
                if not self._matches_plex_identity(item, title, wanted_imdb, wanted_tmdb):
                    continue
                rating_key = str(item.get("ratingKey") or "")
                if season and episode:
                    if item.get("type") == "episode":
                        if (
                            int(item.get("parentIndex") or 0) != season
                            or int(item.get("index") or 0) != episode
                        ):
                            continue
                    else:
                        try:
                            rating_key = self._episode_rating_key(
                                rating_key, season, episode
                            )
                        except IntegrationError:
                            rating_key = ""
                if not rating_key:
                    continue
                try:
                    if self._plex_item_contains_file(rating_key, link_path):
                        return rating_key
                except IntegrationError:
                    continue
            time.sleep(10)
        raise IntegrationError(
            "Plex scan completed, but the exact linked media version was not confirmed"
        )

    def ensure_hls(self, session_id: str) -> str:
        session = self.store.play_session(session_id)
        if not session:
            raise IntegrationError("Playback session expired")
        output_dir = os.path.join(self.transcode_root, session_id)
        playlist = os.path.join(output_dir, "master.m3u8")
        if os.path.isfile(playlist):
            return playlist
        with self._transcode_lock:
            process = self._transcodes.get(session_id)
            if process is None or process.poll() is not None:
                shutil.rmtree(output_dir, ignore_errors=True)
                os.makedirs(output_dir, mode=0o700)
                source = f"http://127.0.0.1:32502/vortexo/play/{session_id}/source"
                command = [
                    "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
                    "-i", source,
                    "-map", "0:v:0", "-map", "0:a:0?",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                    "-c:a", "aac", "-b:a", "192k",
                    "-f", "hls", "-hls_time", "4", "-hls_list_size", "0",
                    "-hls_playlist_type", "event",
                    "-hls_flags", "append_list+independent_segments",
                    "-hls_segment_filename", os.path.join(output_dir, "segment-%06d.ts"),
                    playlist,
                ]
                process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._transcodes[session_id] = process
        deadline = time.time() + 20
        while time.time() < deadline:
            if os.path.isfile(playlist):
                return playlist
            if process.poll() is not None:
                break
            time.sleep(0.25)
        raise IntegrationError("The browser-compatible stream could not be prepared")


class VortexoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PlexVortexo/0.1"

    @property
    def service(self) -> VortexoService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, format, *args):
        # Never log query strings, headers, cookies, or request bodies.
        safe_path = urllib.parse.urlsplit(self.path).path
        print(f'[gateway] {self.command} {safe_path} {args[1] if len(args) > 1 else ""}', flush=True)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > 1024 * 1024:
            raise IntegrationError("Request body is too large")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IntegrationError("Invalid JSON request") from error
        return value if isinstance(value, dict) else {}

    def _cookie(self, name: str) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get(name)
        return morsel.value if morsel else ""

    def _authorised(self) -> bool:
        return self.service.valid_session(self._cookie("vortexo_session"))

    def _send_json(self, value, status: int = 200, headers: dict | None = None):
        body = _json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, item in (headers or {}).items():
            self.send_header(key, item)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, detail: str):
        self._send_json({"error": detail}, status)

    def _require_owner(self) -> bool:
        if self._authorised():
            return True
        self._error(HTTPStatus.UNAUTHORIZED, "Plex owner session required")
        return False

    def do_HEAD(self):
        self._dispatch()

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def _dispatch(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/health":
                self._send_json({"online": True, "service": "gateway"})
                return
            if path == "/vortexo/api/session" and self.command == "PUT":
                session_id = self.service.establish_session(str(self._body().get("plex_token") or ""))
                self._send_json(
                    {"authenticated": True},
                    headers={
                        "Set-Cookie": (
                            f"vortexo_session={session_id}; Path=/vortexo/; "
                            "HttpOnly; SameSite=Strict; Max-Age=28800"
                        )
                    },
                )
                return
            if path.startswith("/vortexo/play/"):
                self._handle_play(path)
                return
            if not path.startswith("/vortexo/api/"):
                self._error(HTTPStatus.NOT_FOUND, "Not found")
                return
            if not self._require_owner():
                return
            self._handle_api(path)
        except PermissionError as error:
            self._error(HTTPStatus.FORBIDDEN, str(error))
        except IntegrationError as error:
            self._error(HTTPStatus.BAD_GATEWAY, str(error))
        except (ValueError, TypeError) as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
        except BrokenPipeError:
            return
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Vortexo request failed")

    def _handle_api(self, path: str):
        if path == "/vortexo/api/status" and self.command == "GET":
            self._send_json(self.service.public_status())
            return
        if path == "/vortexo/api/settings":
            if self.command == "GET":
                self._send_json(self.service.settings_public())
            elif self.command == "PUT":
                self._send_json(self.service.update_settings(self._body()))
            else:
                self._error(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")
            return
        if path == "/vortexo/api/watchlist" and self.command == "GET":
            self._send_json(self.service.watchlist_public())
            return
        if path == "/vortexo/api/watchlist/sync" and self.command == "POST":
            self._send_json(self.service.sync_watchlist())
            return
        match = re.fullmatch(r"/vortexo/api/discover/([^/]+)", path)
        if match and self.command == "GET":
            self._send_json(self.service.media(match.group(1)))
            return
        match = re.fullmatch(r"/vortexo/api/discover/([^/]+)/episodes", path)
        if match and self.command == "GET":
            self._send_json({"episodes": self.service.episodes(match.group(1))})
            return
        if path == "/vortexo/api/streams" and self.command == "POST":
            self._send_json(self.service.streams(self._body()))
            return
        if path == "/vortexo/api/play" and self.command == "POST":
            self._send_json(self.service.create_play_session(self._body()))
            return
        if path == "/vortexo/api/progress" and self.command == "POST":
            self._send_json(self.service.save_progress(self._body()))
            return
        if path == "/vortexo/api/library-jobs" and self.command == "POST":
            job, created = self.service.create_library_job(self._body())
            self._send_json({"job": job, "created": created}, HTTPStatus.ACCEPTED if created else 200)
            return
        match = re.fullmatch(r"/vortexo/api/library-jobs/([a-f0-9]+)", path)
        if match and self.command == "GET":
            job = self.service.store.job(match.group(1))
            if not job:
                self._error(HTTPStatus.NOT_FOUND, "Library job not found")
            else:
                self._send_json({"job": job})
            return
        self._error(HTTPStatus.NOT_FOUND, "Not found")

    def _handle_play(self, path: str):
        match = re.fullmatch(
            r"/vortexo/play/([a-f0-9]+)/(direct|source|master\.m3u8|segment-\d+\.ts)",
            path,
        )
        if not match:
            self._error(HTTPStatus.NOT_FOUND, "Playback resource not found")
            return
        session_id, resource = match.groups()
        session = self.service.store.play_session(session_id)
        if not session:
            self._error(HTTPStatus.GONE, "Playback session expired")
            return
        if resource in {"direct", "source"}:
            self._proxy_source(session)
            return
        if resource == "master.m3u8":
            playlist = self.service.ensure_hls(session_id)
            self._serve_file(playlist, "application/vnd.apple.mpegurl")
            return
        path_on_disk = os.path.join(self.service.transcode_root, session_id, resource)
        self._serve_file(path_on_disk, "video/mp2t")

    def _proxy_source(self, session: dict):
        headers = {
            "User-Agent": "Plex-Vortexo/0.1",
            **{str(key): str(value) for key, value in (session.get("headers") or {}).items()},
        }
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        request = urllib.request.Request(session["url"], headers=headers, method="GET")
        try:
            response = urllib.request.urlopen(request, timeout=45)
        except urllib.error.HTTPError as error:
            self._error(error.code, "TorBox stream request failed")
            return
        status = getattr(response, "status", 200)
        self.send_response(status)
        for name in (
            "Content-Type", "Content-Length", "Content-Range", "Accept-Ranges",
            "Cache-Control", "Last-Modified", "ETag",
        ):
            value = response.headers.get(name)
            if value:
                self.send_header(name, value)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command == "HEAD":
            response.close()
            return
        try:
            shutil.copyfileobj(response, self.wfile, length=1024 * 1024)
        finally:
            response.close()

    def _serve_file(self, path: str, content_type: str | None = None):
        if not os.path.isfile(path):
            self._error(HTTPStatus.NOT_FOUND, "Playback segment not ready")
            return
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", content_type or mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            with open(path, "rb") as handle:
                shutil.copyfileobj(handle, self.wfile)


def serve():
    host = os.environ.get("VORTEXO_API_HOST", "127.0.0.1")
    port = int(os.environ.get("VORTEXO_API_PORT", "32502"))
    server = ThreadingHTTPServer((host, port), VortexoHandler)
    server.daemon_threads = True
    server.service = VortexoService()  # type: ignore[attr-defined]
    print(f"[gateway] Vortexo API listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    serve()
