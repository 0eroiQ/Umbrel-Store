"""Plex library inventory and media-quality inspection."""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .integrations import IntegrationError


PLEX_HEADERS = {
    "Accept": "application/xml",
    "User-Agent": "Orbit/0.2.0",
    "X-Plex-Product": "Orbit",
    "X-Plex-Client-Identifier": "zeroq-orbit",
}


def _plex_xml(base_url: str, token: str, path: str, params: dict | None = None) -> ET.Element:
    if not base_url or not token:
        raise IntegrationError("Add the Plex server URL and token in Settings")
    query = urllib.parse.urlencode(params or {})
    url = f"{base_url.rstrip('/')}{path}"
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(
        url,
        headers={**PLEX_HEADERS, "X-Plex-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return ET.fromstring(response.read())
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise IntegrationError("Plex rejected the saved token") from error
        raise IntegrationError(f"Plex returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, ET.ParseError) as error:
        raise IntegrationError(f"Could not read the Plex library: {error}") from error


def _metadata_ids(node: ET.Element) -> tuple[int | None, str]:
    values = [node.get("guid", "")]
    values.extend(guid.get("id", "") for guid in node.findall("./Guid"))
    tmdb_id = None
    imdb_id = ""
    for value in values:
        tmdb = re.search(r"(?:tmdb|themoviedb)(?:://|/)(\d+)", value, re.I)
        imdb = re.search(r"(tt\d{5,})", value, re.I)
        if tmdb and tmdb_id is None:
            tmdb_id = int(tmdb.group(1))
        if imdb and not imdb_id:
            imdb_id = imdb.group(1)
    return tmdb_id, imdb_id


def _normalise_resolution(media: ET.Element) -> str:
    raw = (media.get("videoResolution") or "").lower()
    stream = media.find(".//Stream[@streamType='1']")
    width = int(media.get("width") or (stream.get("width") if stream is not None else "") or 0)
    height = int(media.get("height") or (stream.get("height") if stream is not None else "") or 0)
    if raw in ("4k", "2160") or width >= 3000 or height >= 2000:
        return "4K"
    if raw in ("1080", "1080p") or height >= 1000:
        return "1080p"
    if raw in ("720", "720p") or height >= 700:
        return "720p"
    if raw in ("576", "576p"):
        return "576p"
    if raw in ("480", "480p", "sd") or height:
        return "SD"
    return "Unknown"


def _dynamic_range(media: ET.Element) -> str:
    stream = media.find(".//Stream[@streamType='1']")
    stream_detail = " ".join(
        stream.get(name, "") if stream is not None else ""
        for name in ("displayTitle", "extendedDisplayTitle", "colorTrc", "DOVIPresent")
    )
    value = f"{media.get('videoDynamicRange') or ''} {stream_detail}".upper()
    if "DOVI" in value or "DOLBY" in value:
        return "Dolby Vision"
    if "HDR" in value or "SMPTE2084" in value:
        return "HDR"
    return "SDR"


def _media_versions(node: ET.Element) -> list[dict]:
    versions = []
    for media in node.findall("./Media"):
        part = media.find("./Part")
        video_stream = media.find(".//Stream[@streamType='1']")
        audio_stream = media.find(".//Stream[@streamType='2']")
        try:
            size = int((part.get("size") if part is not None else "") or 0)
        except ValueError:
            size = 0
        try:
            bitrate = int(media.get("bitrate") or 0)
        except ValueError:
            bitrate = 0
        streams = []
        for stream in media.findall(".//Stream"):
            stream_type = {
                "1": "video",
                "2": "audio",
                "3": "subtitle",
            }.get(stream.get("streamType", ""), "other")
            streams.append({
                "id": stream.get("id") or "",
                "type": stream_type,
                "codec": (stream.get("codec") or "").upper(),
                "language": stream.get("language") or stream.get("languageCode") or "",
                "title": (
                    stream.get("extendedDisplayTitle")
                    or stream.get("displayTitle")
                    or stream.get("title")
                    or ""
                ),
                "channels": stream.get("channels") or "",
                "width": stream.get("width") or "",
                "height": stream.get("height") or "",
                "selected": stream.get("selected") == "1",
                "forced": stream.get("forced") == "1",
            })
        versions.append({
            "resolution": _normalise_resolution(media),
            "dynamic_range": _dynamic_range(media),
            "video_codec": (
                (video_stream.get("codec") if video_stream is not None else "")
                or media.get("videoCodec") or ""
            ).upper(),
            "audio_codec": (
                (audio_stream.get("codec") if audio_stream is not None else "")
                or media.get("audioCodec") or ""
            ).upper(),
            "container": (media.get("container") or "").upper(),
            "bitrate": bitrate,
            "size": size,
            "file": part.get("file", "") if part is not None else "",
            "available": (
                part is not None
                and part.get("exists", "1") != "0"
                and part.get("accessible", "1") != "0"
            ),
            "streams": streams,
        })
    return versions


def _quality_rank(resolution: str) -> int:
    return {"Unknown": 0, "SD": 1, "576p": 2, "720p": 3, "1080p": 4, "4K": 5}.get(
        resolution, 0
    )


def quality_summary(versions: list[dict]) -> str:
    labels = []
    for version in sorted(
        versions,
        key=lambda item: (
            _quality_rank(item.get("resolution", "Unknown")),
            item.get("dynamic_range", "SDR") != "SDR",
        ),
        reverse=True,
    ):
        resolution = version.get("resolution") or "Unknown"
        dynamic_range = version.get("dynamic_range") or "SDR"
        label = f"{resolution} {dynamic_range}" if dynamic_range != "SDR" else resolution
        if label not in labels:
            labels.append(label)
    return " · ".join(labels) if labels else "Quality unavailable"


def upgrade_available(versions: list[dict]) -> bool:
    best = max((_quality_rank(item.get("resolution", "Unknown")) for item in versions), default=0)
    return 0 < best < _quality_rank("1080p")


def _distinct_qualities(versions: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for version in versions:
        key = tuple(
            version.get(name, "")
            for name in ("resolution", "dynamic_range", "video_codec", "audio_codec", "container")
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(version)
    return result


def _library_item(node: ET.Element, section_id: str, versions: list[dict] | None = None) -> dict:
    media_type = "show" if node.get("type") == "show" else "movie"
    tmdb_id, imdb_id = _metadata_ids(node)
    resolved_versions = versions if versions is not None else _media_versions(node)
    try:
        year = int(node.get("year") or 0) or None
    except ValueError:
        year = None
    return {
        "plex_rating_key": node.get("ratingKey") or "",
        "section_id": str(section_id),
        "media_type": media_type,
        "title": node.get("title") or "Unknown",
        "year": year,
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "thumb": node.get("thumb") or "",
        "quality": quality_summary(resolved_versions),
        "versions": resolved_versions,
        "upgrade_available": upgrade_available(resolved_versions),
        "episode_count": 0,
        "seasons": [],
    }


def scan_plex_library(base_url: str, token: str, section_ids: list[str]) -> list[dict]:
    if not section_ids:
        raise IntegrationError("Add at least one Plex movie or TV section ID in Settings")
    items = []
    for section_id in section_ids:
        section = _plex_xml(
            base_url,
            token,
            f"/library/sections/{urllib.parse.quote(str(section_id))}/all",
            {"includeGuids": "1"},
        )
        section_nodes = list(section.findall("./Video")) + list(section.findall("./Directory"))
        shows = {
            node.get("ratingKey"): _library_item(node, str(section_id), [])
            for node in section_nodes
            if node.get("type") == "show" and node.get("ratingKey")
        }
        for node in section_nodes:
            if node.get("type") == "movie":
                items.append(_library_item(node, str(section_id)))
        if shows:
            episodes = _plex_xml(
                base_url,
                token,
                f"/library/sections/{urllib.parse.quote(str(section_id))}/all",
                {"type": "4", "includeGuids": "1"},
            )
            for episode in episodes.findall("./Video"):
                show = shows.get(episode.get("grandparentRatingKey"))
                if not show:
                    continue
                episode_versions = _media_versions(episode)
                show["versions"].extend(episode_versions)
                show["episode_count"] += 1
                try:
                    season_number = int(episode.get("parentIndex") or 0)
                except ValueError:
                    season_number = 0
                season = next(
                    (item for item in show["seasons"] if item["number"] == season_number),
                    None,
                )
                if season is None:
                    season = {
                        "number": season_number,
                        "title": "Specials" if season_number == 0 else f"Season {season_number}",
                        "episode_count": 0,
                        "versions": [],
                        "episodes": [],
                    }
                    show["seasons"].append(season)
                season["episode_count"] += 1
                season["versions"].extend(episode_versions)
                try:
                    episode_number = int(episode.get("index") or 0)
                except ValueError:
                    episode_number = 0
                try:
                    duration = int(episode.get("duration") or 0)
                except ValueError:
                    duration = 0
                season["episodes"].append({
                    "plex_rating_key": episode.get("ratingKey") or "",
                    "season_number": season_number,
                    "episode_number": episode_number,
                    "title": episode.get("title") or f"Episode {episode_number}",
                    "summary": episode.get("summary") or "",
                    "aired_at": episode.get("originallyAvailableAt") or "",
                    "duration": duration,
                    "thumb": episode.get("thumb") or "",
                    "quality": quality_summary(episode_versions),
                    "versions": episode_versions,
                })
            for show in shows.values():
                show["versions"] = _distinct_qualities(show["versions"])
                show["quality"] = quality_summary(show["versions"])
                show["upgrade_available"] = upgrade_available(show["versions"])
                for season in show["seasons"]:
                    season["versions"] = _distinct_qualities(season["versions"])
                    season["quality"] = quality_summary(season["versions"])
                    season.pop("versions")
                    season["episodes"].sort(key=lambda item: item["episode_number"])
                show["seasons"].sort(key=lambda item: item["number"])
                items.append(show)
    return items


def plex_library_sections(base_url: str, token: str) -> list[dict]:
    """Return Plex section types and the paths visible inside Plex itself."""
    root = _plex_xml(base_url, token, "/library/sections")
    sections = []
    for node in root.findall("./Directory"):
        section_id = str(node.get("key") or "")
        media_type = str(node.get("type") or "")
        locations = [
            str(location.get("path") or "")
            for location in node.findall("./Location")
            if location.get("path")
        ]
        if section_id and media_type in {"movie", "show"} and locations:
            sections.append({
                "section_id": section_id,
                "media_type": media_type,
                "locations": locations,
            })
    return sections


def _plex_command(
    base_url: str,
    token: str,
    path: str,
    params: dict | None = None,
    method: str = "GET",
) -> None:
    if not base_url or not token:
        raise IntegrationError("Add the Plex server URL and token in Settings")
    query = urllib.parse.urlencode(params or {})
    url = f"{base_url.rstrip('/')}{path}"
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(
        url,
        headers={**PLEX_HEADERS, "X-Plex-Token": token},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise IntegrationError("Plex rejected the saved token") from error
        raise IntegrationError(f"Plex returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise IntegrationError(f"Could not update the Plex library: {error}") from error


def refresh_plex_paths(
    base_url: str, token: str, section_paths: list[tuple[str, str]]
) -> list[dict]:
    """Request partial scans only for exact library folders that changed."""
    refreshed = []
    seen = set()
    for section_id, folder_path in section_paths:
        pair = (str(section_id), str(folder_path))
        if not pair[0] or not pair[1] or pair in seen:
            continue
        seen.add(pair)
        _plex_command(
            base_url,
            token,
            f"/library/sections/{urllib.parse.quote(pair[0])}/refresh",
            {"path": pair[1]},
        )
        refreshed.append({"section_id": pair[0], "path": pair[1]})
    return refreshed


def cancel_plex_scans(base_url: str, token: str, section_ids: list[str]) -> list[str]:
    """Cancel currently running Plex library scans."""
    cancelled = []
    for section_id in section_ids:
        _plex_command(
            base_url,
            token,
            f"/library/sections/{urllib.parse.quote(str(section_id))}/refresh",
            method="DELETE",
        )
        cancelled.append(str(section_id))
    return cancelled


def configure_plex_remote_library(base_url: str, token: str) -> dict:
    """Make Orbit the scan owner and keep unavailable items in Plex trash."""
    preferences = {
        "FSEventLibraryUpdatesEnabled": "0",
        "FSEventLibraryPartialScanEnabled": "0",
        "ScheduledLibraryUpdatesEnabled": "0",
        "autoEmptyTrash": "0",
    }
    _plex_command(base_url, token, "/:/prefs", preferences, method="PUT")
    return {
        "automatic_scans": False,
        "periodic_scans": False,
        "automatic_empty_trash": False,
    }


def fetch_plex_artwork(base_url: str, token: str, thumb: str) -> tuple[bytes, str]:
    if not base_url or not token:
        raise IntegrationError("Add the Plex server URL and token in Settings")
    if not thumb.startswith("/") or thumb.startswith("//"):
        raise IntegrationError("This Plex item has no usable artwork")
    params = urllib.parse.urlencode({
        "url": thumb,
        "width": 360,
        "height": 540,
        "minSize": 1,
        "upscale": 1,
    })
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/photo/:/transcode?{params}",
        headers={**PLEX_HEADERS, "X-Plex-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read(), response.headers.get_content_type()
    except urllib.error.HTTPError as error:
        raise IntegrationError(f"Plex artwork returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise IntegrationError(f"Could not read Plex artwork: {error}") from error
