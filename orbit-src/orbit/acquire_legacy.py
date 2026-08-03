"""Dispatch one Orbit request through the bundled plex_debrid engine.

This module runs in a short-lived subprocess so the legacy engine's global
settings cannot corrupt Orbit's long-running control plane.
"""

from __future__ import annotations

import builtins
import datetime
import json
import os
import sys
from types import SimpleNamespace


class OrbitWatchlist:
    autoremove = "none"

    @staticmethod
    def remove(*_args, **_kwargs):
        return None


def replacement_scope(job: dict) -> dict | None:
    if job.get("source") != "library-replace":
        return None
    try:
        value = json.loads(job.get("source_ref") or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def restrict_replacement_item(item, scope: dict) -> bool:
    """Limit a matched legacy show to the requested season or episode."""
    target = scope.get("scope")
    if target in {"movie", "series"}:
        return True
    try:
        season_number = int(scope.get("season_number"))
    except (TypeError, ValueError):
        return False
    seasons = [
        season for season in getattr(item, "Seasons", [])
        if int(getattr(season, "index", -1)) == season_number
    ]
    if not seasons:
        return False
    if target == "episode":
        try:
            episode_number = int(scope.get("episode_number"))
        except (TypeError, ValueError):
            return False
        seasons[0].Episodes = [
            episode for episode in getattr(seasons[0], "Episodes", [])
            if int(getattr(episode, "index", -1)) == episode_number
        ]
        if not seasons[0].Episodes:
            return False
    elif target != "season":
        return False
    item.Seasons = seasons
    return True


def apply_quality_profile(releases, profile: str) -> None:
    if profile not in {"1080p", "4k"}:
        return
    resolution = "2160" if profile == "4k" else "1080"
    releases.sort.versions = [[
        f"{'4K' if profile == '4k' else '1080p'} replacement",
        [["retries", "<=", "48"], ["media type", "all", ""]],
        "true",
        [
            ["cache status", "requirement", "cached", ""],
            ["resolution", "requirement", "==", resolution],
            ["size", "preference", "highest", ""],
            ["seeders", "preference", "highest", ""],
            ["size", "requirement", ">=", "0.1"],
        ],
    ]]


def load_engine_settings(ui) -> None:
    """Apply legacy settings migrations without prompting a background worker."""
    original_input = builtins.input
    builtins.input = lambda *_args, **_kwargs: ""
    try:
        ui.load()
    finally:
        builtins.input = original_input


def library_has_media_type(library, media_type: str) -> bool:
    """Return whether Plex has a usable metadata exemplar for this request."""
    expected = "show" if media_type == "show" else "movie"
    return any(getattr(item, "type", None) == expected for item in (library or []))


def acquisition_was_handed_off(item) -> bool:
    """Detect a debrid add even when its WebDAV path is not visible yet."""
    seen = set()

    def visit(node) -> bool:
        if node is None or id(node) in seen:
            return False
        seen.add(id(node))
        if getattr(node, "existing_releases", None):
            return True
        if getattr(node, "downloaded_releases", None):
            return True
        return any(
            visit(child)
            for attribute in ("Seasons", "Episodes")
            for child in (getattr(node, attribute, None) or [])
        )

    return visit(item)


def prepare_item_metadata(item, job: dict, library, matching_service: str, plex):
    """Resolve metadata without requiring an existing item in every Plex section.

    Plex Watchlist rows already carry a canonical ``plex://`` GUID. Loading that
    cloud item directly works even when the local Movies and TV libraries are
    empty. Other sources retain the legacy local-library match when an exemplar
    of the requested media type exists.
    """
    if matching_service != "content.services.plex":
        item.match(matching_service)
        return item

    media_type = "show" if job.get("media_type") == "show" else "movie"
    plex_guid = str(job.get("plex_guid") or "")
    expected_prefix = f"plex://{media_type}/"
    if plex_guid.startswith(expected_prefix):
        try:
            factory = plex.show if media_type == "show" else plex.movie
            resolved = factory(plex_guid)
            if resolved is not None:
                return resolved
        except Exception:
            # The native request still contains title/year/IDs, so movies and
            # other metadata-capable sources can continue without cloud data.
            pass

    if library_has_media_type(library, media_type):
        item.match(matching_service)
    return item


def main() -> int:
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "detail": "missing request file"}))
        return 2
    request_path = sys.argv[1]
    with open(request_path, "r", encoding="utf-8") as handle:
        job = json.load(handle)

    engine_root = os.environ.get("PD_ROOT", "/app/plex_debrid")
    config_dir = os.environ.get("PD_CONFIG_DIR", "/config")
    sys.path.insert(0, engine_root)

    # The legacy engine initializes its package graph from ui. Importing
    # content first leaves content.services partially initialized.
    import ui  # type: ignore
    import content  # type: ignore
    import releases  # type: ignore
    from content.services import overseerr, plex, trakt  # type: ignore
    from ui.ui_print import set_log_dir  # type: ignore

    ui.config_dir = config_dir
    ui.service_mode = True
    set_log_dir(config_dir)
    load_engine_settings(ui)
    apply_quality_profile(releases, job.get("profile") or "best")

    media = SimpleNamespace(
        id=job["id"],
        status=3,
        imdbId=job.get("imdb_id") or None,
        tmdbId=job.get("tmdb_id") or None,
        tvdbId=None,
    )
    root = SimpleNamespace(
        type="tv" if job["media_type"] == "show" else "movie",
        title=job["title"],
        year=job.get("year"),
        updatedAt=datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        media=media,
    )
    item = overseerr.show(root) if job["media_type"] == "show" else overseerr.movie(root)

    matching_service = None
    if plex.users:
        matching_service = "content.services.plex"
    elif trakt.users:
        matching_service = "content.services.trakt"
    if not matching_service:
        print(json.dumps({"ok": False, "detail": "Connect Plex or Trakt before adding media"}))
        return 3

    libraries = content.classes.library()
    library_factory = next(iter(libraries), None)
    if library_factory is None:
        print(json.dumps({"ok": False, "detail": "Configure a Plex or Trakt library service"}))
        return 4
    # Empty is a valid, readable library state on a fresh install. The first
    # acquisition seeds it; a non-empty Movies section does not require Series
    # content (and vice versa).
    library = library_factory() or []

    item = prepare_item_metadata(item, job, library, matching_service, plex)
    item.watchlist = OrbitWatchlist
    scope = replacement_scope(job)
    if scope is not None and not restrict_replacement_item(item, scope):
        print(json.dumps({
            "ok": False,
            "detail": "The selected season or episode is no longer available in Plex metadata",
        }))
        return 4

    if library and job.get("source") == "series-monitor" and item.complete(library):
        print(json.dumps({
            "ok": True,
            "status": "ready",
            "detail": "Series is caught up; future unaired episodes were ignored",
            "paths": [],
        }))
        return 0

    item.download(library=[] if scope is not None else library)
    releases = getattr(item, "downloaded_releases", [])
    if not releases:
        if acquisition_was_handed_off(item):
            print(json.dumps({
                "ok": True,
                "detail": "Acquired; waiting for the mounted source",
                "paths": [],
            }))
            return 0
        print(json.dumps({"ok": False, "detail": "No suitable cached release was acquired"}))
        return 6
    print(json.dumps({"ok": True, "detail": "Acquired and handed to the library", "paths": releases}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
