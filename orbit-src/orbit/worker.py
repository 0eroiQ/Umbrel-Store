"""Background queue and automatic list coordinator."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .integrations import IntegrationError, fetch_list, fetch_plex_watchlist
from .link_repair import repair_broken_symlinks
from .mount_reconcile import reconcile_mounted_sources
from .plex import (
    cataloged_plex_paths,
    plex_library_sections,
    refresh_plex_paths,
    scan_plex_library,
)
from .store import Store


class Coordinator:
    def __init__(self, store: Store, data_dir: str):
        self.store = store
        self.data_dir = data_dir
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.maintenance_thread: threading.Thread | None = None
        self.last_list_poll = 0.0
        self.last_plex_watchlist_poll = 0.0
        self.last_plex_poll = 0.0
        self.last_link_repair_poll = 0.0
        self.last_mount_reconcile_poll = 0.0
        self.last_pending_scan_poll = 0.0
        self.link_repair_lock = threading.Lock()
        self.last_link_repair = {
            "status": "never",
            "checked": 0,
            "broken": 0,
            "repaired": 0,
            "queued": 0,
            "refreshed_sections": [],
        }

    def start(self):
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(
                target=self._run, name="orbit-coordinator", daemon=True
            )
            self.thread.start()
        if not self.maintenance_thread or not self.maintenance_thread.is_alive():
            self.maintenance_thread = threading.Thread(
                target=self._run_media_handoffs,
                name="orbit-media-handoffs",
                daemon=True,
            )
            self.maintenance_thread.start()

    def stop(self):
        self.stop_event.set()

    def _run(self):
        while not self.stop_event.wait(3):
            try:
                self.process_one()
                interval = int(self.store.get_settings(True).get("list_poll_minutes", "60")) * 60
                if time.monotonic() - self.last_list_poll >= max(300, interval):
                    self.sync_all_lists()
                    self.last_list_poll = time.monotonic()
                settings = self.store.get_settings(True)
                watchlist_interval = int(
                    settings.get("plex_watchlist_poll_minutes", "1")
                ) * 60
                watchlist_enabled = str(
                    settings.get("plex_watchlist_enabled", "false")
                ).lower() in {"1", "true", "yes", "on"}
                if (
                    watchlist_enabled
                    and time.monotonic() - self.last_plex_watchlist_poll
                    >= max(60, watchlist_interval)
                ):
                    try:
                        self.sync_plex_watchlist(settings)
                    except IntegrationError:
                        pass
                    self.last_plex_watchlist_poll = time.monotonic()
                if time.monotonic() - self.last_plex_poll >= 900:
                    try:
                        self.sync_plex_library()
                    except IntegrationError:
                        pass
                    self.last_plex_poll = time.monotonic()
                repair_enabled = str(
                    settings.get("plex_link_repair_enabled", "true")
                ).lower() in {"1", "true", "yes", "on"}
                try:
                    repair_interval = int(
                        settings.get("plex_link_repair_interval_minutes", "5")
                    ) * 60
                except (TypeError, ValueError):
                    repair_interval = 300
                if (
                    repair_enabled
                    and time.monotonic() - self.last_link_repair_poll
                    >= max(300, repair_interval)
                ):
                    self.repair_plex_streams(settings)
                    self.last_link_repair_poll = time.monotonic()
            except Exception:
                # Keep the dashboard alive even when one background operation fails.
                time.sleep(2)

    def _run_media_handoffs(self):
        """Keep mount recovery and Plex scans independent of slow downloads."""
        while not self.stop_event.wait(3):
            try:
                self.service_media_handoffs()
            except Exception:
                # A provider or Plex failure must not stop future retries.
                time.sleep(2)

    def service_media_handoffs(self):
        if time.monotonic() - self.last_mount_reconcile_poll >= 60:
            self.reconcile_mounted_media()
            self.last_mount_reconcile_poll = time.monotonic()
        self.verify_library_handoffs()
        if time.monotonic() - self.last_pending_scan_poll >= 15:
            self.scan_pending_library_paths()
            self.last_pending_scan_poll = time.monotonic()

    def process_one(self):
        job = self.store.next_queued()
        command = os.environ.get("ORBIT_ACQUIRE_COMMAND", "").strip()
        if not job or not command:
            return
        request_path = os.path.join(self.data_dir, "jobs", f"request-{job['id']}.json")
        self.store.export_worker_request(job, request_path)
        self.store.transition(job["id"], "searching", "Searching configured release sources")
        try:
            completed = subprocess.run(
                [*shlex.split(command), request_path],
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
            last_line = (completed.stdout.strip().splitlines() or [""])[-1]
            try:
                result = json.loads(last_line)
            except json.JSONDecodeError:
                result = {"ok": False, "detail": completed.stderr.strip() or last_line or "Acquisition failed"}
            if completed.returncode == 0 and result.get("ok"):
                self.store.transition(job["id"], "library_pending", result.get("detail", "Added to debrid"))
            else:
                self.store.transition(job["id"], "needs_attention", result.get("detail", "Acquisition failed"))
        except subprocess.TimeoutExpired:
            self.store.transition(job["id"], "needs_attention", "Acquisition timed out")

    def verify_library_handoffs(self):
        """Promote requests once their canonical library entry is visible."""
        roots = {
            "movie": os.environ.get("ORBIT_MOVIES_DIR", "/zeroq-media/vortexo/Movies"),
            "show": os.environ.get("ORBIT_TV_DIR", "/zeroq-media/vortexo/TV"),
        }
        pending = [
            item for item in self.store.list_requests(500)
            if item["status"] in {"library_pending", "needs_attention"}
        ]
        ready_paths = []
        if not self.mount_is_healthy():
            return
        for item in pending:
            root = roots[item["media_type"]]
            try:
                names = os.listdir(root)
            except OSError:
                continue
            tmdb_marker = f"{{tmdb-{item['tmdb_id']}}}" if item.get("tmdb_id") else ""
            title_key = re.sub(r"[^a-z0-9]+", "", item["title"].lower())
            matching = [
                name for name in names
                if (tmdb_marker and tmdb_marker in name)
                or (title_key and re.sub(r"[^a-z0-9]+", "", name.lower()).startswith(title_key))
            ]
            playable = any(
                self._folder_has_playable_video(os.path.join(root, name))
                for name in matching
            )
            if playable:
                self.store.transition(
                    item["id"], "ready",
                    "Playable library link verified; Plex scan requested",
                )
                ready_paths.extend((item["media_type"], os.path.join(root, name)) for name in matching)
        if ready_paths:
            self.refresh_plex_paths_if_healthy(ready_paths)

    @staticmethod
    def _library_roots() -> dict[str, str]:
        return {
            "movie": os.environ.get("ORBIT_MOVIES_DIR", "/zeroq-media/vortexo/Movies"),
            "show": os.environ.get("ORBIT_TV_DIR", "/zeroq-media/vortexo/TV"),
        }

    def reconcile_mounted_media(self) -> list[tuple[str, str]]:
        """Recover transfers that became visible after acquisition returned."""
        if not self.mount_is_healthy():
            return []
        roots = self._library_roots()
        try:
            return reconcile_mounted_sources(
                os.environ.get("PD_DOWNLOADS_DIR", "/zeroq-media"),
                {"movie": roots["movie"], "tv": roots["show"]},
                log_fn=lambda message: print(f"[orbit] mount reconciliation: {message}"),
            )
        except (ImportError, OSError) as error:
            print(f"[orbit] mount reconciliation deferred: {error}")
            return []

    def pending_library_scan_paths(self) -> list[tuple[str, str]]:
        pending = []
        for media_type, root in self._library_roots().items():
            try:
                entries = os.listdir(root)
            except OSError:
                continue
            for entry in entries:
                folder = os.path.join(root, entry)
                marker = os.path.join(folder, ".plex-scan-pending")
                if os.path.isdir(folder) and os.path.isfile(marker) and not os.path.islink(marker):
                    pending.append((media_type, folder))
        return pending

    def scan_pending_library_paths(self) -> list[tuple[str, str]]:
        completed = []
        settings = self.store.get_settings(reveal_secrets=True)
        for media_type, folder in self.pending_library_scan_paths():
            refreshed = self.refresh_plex_paths_if_healthy(
                [(media_type, folder)], settings
            )
            if not refreshed:
                continue
            section_paths = [
                (item["section_id"], item["path"])
                for item in refreshed
                if item.get("section_id") and item.get("path")
            ]
            try:
                visible = cataloged_plex_paths(
                    settings.get("plex_url", ""),
                    settings.get("plex_token", ""),
                    section_paths,
                )
            except IntegrationError:
                continue
            if not visible.intersection(section_paths):
                # HTTP 200 only means Plex accepted the refresh request. Keep
                # the durable marker until the catalog exposes a media part so
                # scans dropped while Plex is busy are retried automatically.
                continue
            marker = os.path.join(folder, ".plex-scan-pending")
            try:
                if os.path.isfile(marker) and not os.path.islink(marker):
                    os.unlink(marker)
                completed.append((media_type, folder))
            except OSError:
                continue
        return completed

    @staticmethod
    def _folder_has_playable_video(path: str) -> bool:
        extensions = {
            ".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg",
            ".mpg", ".mts", ".ts", ".webm", ".wmv",
        }
        try:
            for root, _directories, files in os.walk(path):
                for filename in files:
                    candidate = os.path.join(root, filename)
                    if os.path.splitext(filename)[1].lower() in extensions and os.path.exists(candidate):
                        return True
        except OSError:
            return False
        return False

    def mount_is_healthy(self) -> bool:
        base = os.environ.get("ORBIT_MOUNT_API", "http://mount:8080").rstrip("/")
        try:
            with urllib.request.urlopen(base + "/api/status", timeout=10) as response:
                status = json.loads(response.read())
            return bool(status.get("mounted")) and status.get("storage_safety_ok", True) is not False
        except (urllib.error.URLError, TimeoutError, ValueError):
            return False

    @staticmethod
    def _section_ids(settings: dict) -> list[str]:
        return [
            part.strip()
            for part in settings.get("plex_sections", "").split(",")
            if part.strip()
        ]

    def refresh_plex_paths_if_healthy(
        self,
        media_paths: list[tuple[str, str]],
        settings: dict | None = None,
    ) -> list[dict]:
        settings = settings or self.store.get_settings(reveal_secrets=True)
        if not self.mount_is_healthy():
            return []
        if not settings.get("plex_url") or not settings.get("plex_token"):
            return []
        configured_ids = set(self._section_ids(settings))
        try:
            sections = plex_library_sections(
                settings["plex_url"], settings["plex_token"]
            )
        except IntegrationError:
            return []
        roots = {
            "movie": os.environ.get("ORBIT_MOVIES_DIR", "/zeroq-media/vortexo/Movies"),
            "show": os.environ.get("ORBIT_TV_DIR", "/zeroq-media/vortexo/TV"),
        }
        section_paths = []
        for media_type, folder_path in media_paths:
            local_root = os.path.abspath(roots[media_type])
            candidate = os.path.abspath(folder_path)
            try:
                relative = os.path.relpath(candidate, local_root)
            except ValueError:
                continue
            if relative == ".." or relative.startswith(".." + os.sep):
                continue
            for section in sections:
                if section["media_type"] != media_type:
                    continue
                if configured_ids and section["section_id"] not in configured_ids:
                    continue
                for plex_root in section["locations"]:
                    plex_path = (
                        plex_root if relative == "."
                        else os.path.join(plex_root, relative)
                    )
                    section_paths.append((section["section_id"], plex_path))
        if not section_paths:
            return []
        return refresh_plex_paths(
            settings["plex_url"], settings["plex_token"], section_paths
        )

    @staticmethod
    def _library_folder(path: str, library_root: str) -> str:
        root = os.path.abspath(library_root)
        candidate = os.path.abspath(path)
        try:
            relative = os.path.relpath(candidate, root)
        except ValueError:
            return ""
        if relative == ".." or relative.startswith(".." + os.sep):
            return ""
        first = relative.split(os.sep, 1)[0]
        return os.path.join(root, first)

    @staticmethod
    def _version_paths(versions: list[dict]) -> list[tuple[str, bool]]:
        return [
            (str(version.get("file") or ""), bool(version.get("available", True)))
            for version in versions or []
            if version.get("file")
        ]

    def repair_plex_streams(self, settings: dict | None = None) -> dict:
        """Repair broken links, queue missing streams, then clear stale Plex flags."""
        if not self.link_repair_lock.acquire(blocking=False):
            return {
                **self.last_link_repair,
                "status": "running",
                "error": "A Plex stream protection check is already running",
            }
        try:
            return self._repair_plex_streams(settings)
        finally:
            self.link_repair_lock.release()

    def _repair_plex_streams(self, settings: dict | None = None) -> dict:
        settings = settings or self.store.get_settings(reveal_secrets=True)
        if not self.mount_is_healthy():
            self.last_link_repair = {
                **self.last_link_repair,
                "status": "deferred",
                "error": "Debrid mount is offline; Plex scan was not requested",
            }
            return self.last_link_repair
        # TorBox-specific symlink retargeting. Skip silently for non-TorBox
        # providers (Real-Debrid, AllDebrid) where the TorBox API is irrelevant.
        if str(settings.get("debrid_mode", "webdav")).lower() != "webdav":
            self.last_link_repair = {
                **self.last_link_repair,
                "status": "skipped",
                "repaired": 0,
                "scanned": 0,
                "error": "Link repair runs only for TorBox mode",
            }
            return self.last_link_repair
        try:
            max_per_run = max(
                1, min(int(settings.get("plex_link_repair_max_per_run", "10")), 100)
            )
        except (TypeError, ValueError):
            max_per_run = 10
        roots = {
            "movie": os.environ.get("ORBIT_MOVIES_DIR", "/zeroq-media/vortexo/Movies"),
            "show": os.environ.get("ORBIT_TV_DIR", "/zeroq-media/vortexo/TV"),
        }
        inventory_scopes = []
        unavailable_paths: set[str] = set()
        for item in self.store.plex_repair_inventory():
            scopes = []
            if item["media_type"] == "movie":
                scopes.append(("movie", None, None, self._version_paths(item.get("versions", []))))
            else:
                for season in item.get("seasons") or []:
                    for episode in season.get("episodes") or []:
                        scopes.append((
                            "episode",
                            season.get("number"),
                            episode.get("episode_number"),
                            self._version_paths(episode.get("versions", [])),
                        ))
            for scope in scopes:
                paths = scope[3]
                if paths and not any(available for _path, available in paths):
                    unavailable_paths.update(path for path, _available in paths)
                    inventory_scopes.append((item, *scope))
        repaired = repair_broken_symlinks(
            settings.get("torbox_api_key", ""),
            os.environ.get("PD_DOWNLOADS_DIR", "/zeroq-media"),
            roots,
            max_repairs=max_per_run * 10,
            candidate_links=unavailable_paths,
        )
        queued = 0
        stale_paths: list[tuple[str, str]] = []
        for item, scope, season_number, episode_number, paths in inventory_scopes:
            path_states = [
                (path, os.path.exists(path), plex_available)
                for path, plex_available in paths
            ]
            if any(exists for _path, exists, _available in path_states):
                if any(not available for _path, _exists, available in path_states):
                    root = roots[item["media_type"]]
                    folder = next(
                        (
                            self._library_folder(path, root)
                            for path, exists, _available in path_states
                            if exists and self._library_folder(path, root)
                        ),
                        "",
                    )
                    if folder:
                        stale_paths.append((str(item["section_id"]), folder))
                continue
            if queued >= max_per_run:
                continue
            if not (item.get("tmdb_id") or item.get("imdb_id")):
                continue
            label = (
                "Restoring an unavailable movie stream"
                if scope == "movie"
                else f"Restoring unavailable S{int(season_number):02d}E{int(episode_number):02d}"
            )
            _request, created = self.store.queue_library_replacement(
                item,
                scope,
                season_number,
                episode_number,
                "best",
                minimum_retry_seconds=21600,
                detail_override=label,
            )
            queued += int(created)
        refreshed = []
        error = repaired.get("error") or ""
        if stale_paths:
            try:
                if self.mount_is_healthy():
                    refreshed = refresh_plex_paths(
                        settings["plex_url"], settings["plex_token"], stale_paths
                    )
            except IntegrationError as exc:
                error = str(exc)
        self.last_link_repair = {
            "status": "ok" if not error else "attention",
            "checked": repaired["checked"],
            "broken": repaired["broken"],
            "repaired": repaired["repaired"],
            "remaining": len(repaired["remaining"]),
            "queued": queued,
            "refreshed_sections": sorted({
                item["section_id"] for item in refreshed
            }),
            "refreshed_paths": len(refreshed),
            "error": error,
        }
        return self.last_link_repair

    def sync_list(self, source_id: int) -> dict:
        source = self.store.get_list_source(source_id)
        if not source:
            raise IntegrationError("Automatic list not found")
        settings = self.store.get_settings(reveal_secrets=True)
        try:
            items = fetch_list(source, settings)
            added = 0
            skipped_existing = 0
            for item in items:
                if self.store.match_plex_library(item):
                    skipped_existing += 1
                    continue
                item["profile"] = source["profile"]
                _, created = self.store.add_request(item, source=source["kind"], source_ref=str(source["id"]))
                added += int(created)
            self.store.complete_list_sync(source_id)
            return {
                "found": len(items),
                "added": added,
                "skipped_existing": skipped_existing,
            }
        except IntegrationError as error:
            self.store.complete_list_sync(source_id, str(error))
            raise

    def sync_all_lists(self):
        for source in self.store.list_sources():
            if source["enabled"]:
                try:
                    self.sync_list(source["id"])
                except IntegrationError:
                    pass

    def sync_plex_watchlist(
        self,
        settings: dict | None = None,
        retry_failed: bool = False,
    ) -> dict:
        settings = settings or self.store.get_settings(reveal_secrets=True)
        enabled = str(settings.get("plex_watchlist_enabled", "false")).lower() in {
            "1", "true", "yes", "on",
        }
        if not enabled:
            raise IntegrationError("Enable Plex Watchlist imports in Settings")
        try:
            limit = max(
                1, min(int(settings.get("plex_watchlist_max_items", "100")), 1000)
            )
        except (TypeError, ValueError):
            limit = 100
        profile = settings.get("plex_watchlist_profile", "best")
        if profile not in {"best", "1080p", "4k"}:
            profile = "best"
        items = fetch_plex_watchlist(settings.get("plex_token", ""), limit)
        added = 0
        retried = 0
        skipped_existing = 0
        skipped_requested = 0
        for item in items:
            if self.store.match_plex_library(item):
                skipped_existing += 1
                continue
            item["profile"] = profile
            request, created = self.store.add_request(
                item, source="plex-watchlist", source_ref="plex-account"
            )
            added += int(created)
            was_retried = False
            if not created and retry_failed:
                _request, was_retried = self.store.retry_failed_request(
                    request["id"],
                    source="plex-watchlist",
                    source_ref="plex-account",
                    profile=profile,
                )
                retried += int(was_retried)
            skipped_requested += int(not created and not was_retried)
        return {
            "found": len(items),
            "added": added,
            "retried": retried,
            "skipped_existing": skipped_existing,
            "skipped_requested": skipped_requested,
        }

    def sync_plex_library(self) -> dict:
        settings = self.store.get_settings(reveal_secrets=True)
        section_ids = self._section_ids(settings)
        if not settings.get("plex_url") or not settings.get("plex_token") or not section_ids:
            raise IntegrationError("Add the Plex URL, token, and library section IDs in Settings")
        try:
            items = scan_plex_library(
                settings["plex_url"], settings["plex_token"], section_ids
            )
            self.store.replace_plex_library(items)
            completion = self.queue_series_completions(settings)
            return {
                "items": len(items),
                "status": self.store.plex_library_status(),
                "manifests": {"count": len(items), "mode": "virtual"},
                "series_completion": completion,
            }
        except IntegrationError as error:
            self.store.fail_plex_library_sync(str(error))
            raise

    def queue_series_completions(self, settings: dict | None = None) -> dict:
        settings = settings or self.store.get_settings(reveal_secrets=True)
        enabled = str(settings.get("complete_aired_series", "false")).lower() in {
            "1", "true", "yes", "on",
        }
        if not enabled:
            return {"enabled": False, "queued": 0, "daily_limit": 0}
        try:
            daily_limit = max(
                1, min(int(settings.get("series_completion_daily_limit", "25")), 250)
            )
        except (TypeError, ValueError):
            daily_limit = 25
        run_key = datetime.now(timezone.utc).date().isoformat()
        remaining = max(0, daily_limit - self.store.series_completion_count(run_key))
        queued = 0
        for item in self.store.list_series_completion_candidates():
            if queued >= remaining:
                break
            _, created = self.store.queue_series_completion(item, run_key)
            queued += int(created)
        return {
            "enabled": True,
            "queued": queued,
            "daily_limit": daily_limit,
            "remaining_today": max(0, remaining - queued),
        }
