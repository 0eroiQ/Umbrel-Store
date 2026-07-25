from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET


VIDEO_EXTENSIONS = {
    ".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg",
    ".mts", ".ts", ".webm", ".wmv",
}


class IntegrationError(RuntimeError):
    pass


def json_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    payload: dict | list | None = None,
    timeout: int = 30,
) -> dict | list:
    body = None
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "Plex-Vortexo/0.1",
        **(headers or {}),
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as error:
        try:
            remote = json.loads(error.read().decode("utf-8"))
            detail = remote.get("detail") or remote.get("error")
        except Exception:
            detail = None
        raise IntegrationError(detail or f"Remote service returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise IntegrationError(f"Could not reach remote service: {error}") from error


def plex_owner_token(preferences_path: str) -> str:
    try:
        root = ET.parse(preferences_path).getroot()
    except (OSError, ET.ParseError):
        return ""
    return (root.attrib.get("PlexOnlineToken") or "").strip()


def plex_cloud_headers(token: str) -> dict:
    """Authenticate to Plex cloud without publishing a companion identity."""
    return {
        "Accept": "application/json",
        "X-Plex-Token": token,
    }


def plex_server_headers(token: str) -> dict:
    """Authenticate to the local PMS without overriding its published identity."""
    return {
        "Accept": "application/json",
        "X-Plex-Token": token,
    }


def plex_headers(token: str) -> dict:
    """Compatibility alias for Plex cloud-client requests."""
    return plex_cloud_headers(token)


def plex_account(token: str) -> dict:
    if not token:
        return {}
    payload = json_request(
        "https://plex.tv/api/v2/user",
        headers=plex_cloud_headers(token),
        timeout=10,
    )
    if not isinstance(payload, dict):
        return {}
    return {
        "id": str(payload.get("id") or ""),
        "uuid": str(payload.get("uuid") or ""),
        "email": str(payload.get("email") or "").strip().casefold(),
    }


def discover_metadata(discover_id: str, token: str) -> dict:
    discover_id = normalize_discover_id(discover_id)
    if not discover_id:
        raise IntegrationError("Missing Plex Discover metadata ID")
    params = {
        "includeMeta": "1",
        "includeExternalMetadata": "1",
        "includeUserState": "1",
        "includeGuids": "1",
        "includeImages": "1",
        "includeExternalMedia": "1",
    }
    url = "https://discover.provider.plex.tv/library/metadata/{}?{}".format(
        urllib.parse.quote(discover_id, safe=""),
        urllib.parse.urlencode(params),
    )
    payload = json_request(url, headers=plex_cloud_headers(token))
    container = payload.get("MediaContainer", {}) if isinstance(payload, dict) else {}
    rows = container.get("Metadata") or container.get("Directory") or []
    if not rows:
        raise IntegrationError("Plex Discover metadata was not found")
    return normalize_media(rows[0], fallback_discover_id=discover_id)


def discover_children(path: str, token: str, limit: int = 1000) -> list[dict]:
    if not path.startswith("/"):
        raise IntegrationError("Plex Discover child path is invalid")
    params = {
        "includeMeta": "1",
        "includeExternalMetadata": "1",
        "includeUserState": "1",
        "includeGuids": "1",
        "includeImages": "1",
        "includeExternalMedia": "1",
        "X-Plex-Container-Start": "0",
        "X-Plex-Container-Size": str(max(1, min(limit, 1000))),
    }
    url = "https://discover.provider.plex.tv{}?{}".format(path, urllib.parse.urlencode(params))
    payload = json_request(url, headers=plex_cloud_headers(token))
    container = payload.get("MediaContainer", {}) if isinstance(payload, dict) else {}
    rows = [*(container.get("Directory") or []), *(container.get("Metadata") or [])]
    return [normalize_media(row) for row in rows]


def normalize_discover_id(value: str) -> str:
    value = urllib.parse.unquote(str(value or "")).strip().replace("\\/", "/")
    if "/library/metadata/" in value:
        value = value.split("/library/metadata/", 1)[1].split("/", 1)[0]
    elif value.lower().startswith("plex://"):
        value = value.rstrip("/").rsplit("/", 1)[-1]
    if "://" in value or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        return ""
    return value


def _guid_values(raw: dict) -> list[str]:
    values = [
        raw.get("guid"),
        raw.get("primaryGuid"),
        raw.get("ratingKey"),
        raw.get("key"),
    ]
    for key in ("Guid", "guids"):
        for item in raw.get(key) or []:
            values.append(item.get("id") if isinstance(item, dict) else str(item))
    return [str(value) for value in values if value]


def external_ids(raw: dict) -> tuple[int | None, str]:
    tmdb_id = None
    imdb_id = ""
    for value in _guid_values(raw):
        imdb_match = re.search(r"(tt\d{5,12})", value, re.I)
        if imdb_match and not imdb_id:
            imdb_id = imdb_match.group(1).lower()
        tmdb_match = re.search(r"(?:tmdb://|themoviedb://|tmdb:)(\d+)", value, re.I)
        if tmdb_match and tmdb_id is None:
            tmdb_id = int(tmdb_match.group(1))
    return tmdb_id, imdb_id


def normalize_media(raw: dict, fallback_discover_id: str = "") -> dict:
    tmdb_id, imdb_id = external_ids(raw)
    media_type = str(raw.get("type") or "").lower()
    if media_type in {"series", "tv"}:
        media_type = "show"
    discover_id = ""
    for value in _guid_values(raw):
        discover_id = normalize_discover_id(value)
        if discover_id:
            break
    discover_id = discover_id or fallback_discover_id
    return {
        "discover_id": discover_id,
        "type": media_type or "movie",
        "title": raw.get("title") or raw.get("name") or "Unknown",
        "year": raw.get("year"),
        "summary": raw.get("summary") or "",
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "season": raw.get("parentIndex"),
        "episode": raw.get("index"),
        "parent_title": raw.get("grandparentTitle") or raw.get("parentTitle") or "",
        "key": raw.get("key") or "",
        "rating_key": raw.get("ratingKey") or "",
        "parent_rating_key": raw.get("parentRatingKey") or "",
        "grandparent_rating_key": raw.get("grandparentRatingKey") or "",
        "thumb": raw.get("thumb") or "",
        "view_offset": raw.get("viewOffset") or 0,
        "duration": raw.get("duration") or 0,
        "watched": bool(raw.get("viewCount")),
    }


def normalize_plex_watchlist_item(raw: dict) -> dict | None:
    media = normalize_media(raw)
    if media["type"] not in {"movie", "show"}:
        return None
    if not media["discover_id"] or not (media["imdb_id"] or media["tmdb_id"]):
        return None
    return media


def fetch_plex_watchlist(token: str, limit: int = 100) -> list[dict]:
    """Fetch and normalize the signed-in Plex owner's universal Watchlist."""
    if not token:
        raise IntegrationError("Plex owner token is not available")
    item_limit = max(1, min(int(limit), 1000))
    endpoint = "https://discover.provider.plex.tv/library/sections/watchlist/all"
    items = []
    seen = set()
    start = 0
    while len(items) < item_limit:
        page_size = min(50, item_limit - len(items))
        query = urllib.parse.urlencode(
            {
                "includeAdvanced": "1",
                "includeMeta": "1",
                "includeExternalMedia": "1",
                "includeGuids": "1",
                "X-Plex-Container-Start": str(start),
                "X-Plex-Container-Size": str(page_size),
            }
        )
        payload = json_request(
            f"{endpoint}?{query}",
            headers=plex_cloud_headers(token),
            timeout=20,
        )
        container = payload.get("MediaContainer", {}) if isinstance(payload, dict) else {}
        rows = container.get("Metadata") or []
        if not isinstance(rows, list):
            raise IntegrationError("Plex returned an unsupported Watchlist response")
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            media = normalize_plex_watchlist_item(raw)
            if not media:
                continue
            identity = (
                media["type"],
                str(media.get("tmdb_id") or media.get("imdb_id") or media["discover_id"]),
            )
            if identity in seen:
                continue
            seen.add(identity)
            items.append(media)
            if len(items) >= item_limit:
                break
        start += len(rows)
        try:
            total_size = int(container.get("totalSize", start))
        except (TypeError, ValueError):
            total_size = start
        if not rows or start >= total_size:
            break
    return items


def fetch_streams(manifest_url: str, media: dict, season: int = 0, episode: int = 0) -> list[dict]:
    manifest_url = manifest_url.strip()
    if not manifest_url:
        return []
    manifest = json_request(manifest_url)
    if not isinstance(manifest, dict):
        raise IntegrationError("Stream manifest returned an unsupported response")
    resources = manifest.get("resources") or []
    resource_names = {
        str(item.get("name") if isinstance(item, dict) else item).lower()
        for item in resources
    }
    if resources and "stream" not in resource_names:
        raise IntegrationError("Configured manifest does not provide streams")
    base = manifest_url.rsplit("/manifest.json", 1)[0].rstrip("/")
    imdb_id = media.get("imdb_id") or ""
    tmdb_id = media.get("tmdb_id")
    lookup_ids = [imdb_id] if imdb_id else []
    if tmdb_id:
        lookup_ids.extend([f"tmdb:{tmdb_id}", str(tmdb_id)])
    if not lookup_ids:
        raise IntegrationError("Plex Discover did not provide an IMDb or TMDb ID")
    kind = "series" if media.get("type") in {"show", "episode"} or season else "movie"
    last_error = None
    for lookup_id in lookup_ids:
        if kind == "series":
            if season <= 0 or episode <= 0:
                raise IntegrationError("Choose a season and episode")
            stream_id = f"{lookup_id}:{season}:{episode}"
        else:
            stream_id = lookup_id
        url = "{}/stream/{}/{}.json".format(
            base,
            kind,
            urllib.parse.quote(stream_id, safe=":"),
        )
        try:
            payload = json_request(url)
        except IntegrationError as error:
            last_error = error
            continue
        rows = payload.get("streams") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            return [
                normalize_stream(row, manifest.get("name") or "Vortexo Sources", season, episode)
                for row in rows
                if isinstance(row, dict)
            ]
    if last_error:
        raise last_error
    return []


def normalize_stream(raw: dict, source_name: str, season: int = 0, episode: int = 0) -> dict:
    hints = raw.get("behaviorHints") or {}
    title = str(raw.get("title") or raw.get("name") or hints.get("filename") or "Stream")
    description = str(raw.get("description") or "")
    file_name = str(hints.get("filename") or title)
    searchable = " ".join([title, description, file_name])
    quality = extract_quality(searchable)
    dynamic_range = extract_dynamic_range(searchable)
    codec = extract_codec(searchable)
    audio = extract_audio(searchable)
    size_bytes = hints.get("videoSize") or 0
    try:
        size_gb = round(float(size_bytes) / (1024 ** 3), 2) if size_bytes else parse_size_gb(searchable)
    except (TypeError, ValueError):
        size_gb = parse_size_gb(searchable)
    # Stremio externalUrl entries open a web page and are not media URLs.
    # Only a resolved stream URL is eligible for Play Now.
    url = str(raw.get("url") or "")
    info_hash = str(raw.get("infoHash") or "").lower()
    url_file_idx = None
    if url:
        path_parts = [
            urllib.parse.unquote(part)
            for part in urllib.parse.urlparse(url).path.split("/")
            if part
        ]
        for index, part in enumerate(path_parts):
            if part.casefold() != "torbox" or index + 4 >= len(path_parts):
                continue
            candidate_hash = path_parts[index + 2].lower()
            candidate_file_idx = path_parts[index + 4]
            if re.fullmatch(r"[0-9a-f]{40}", candidate_hash):
                info_hash = info_hash or candidate_hash
                if candidate_file_idx.isdigit():
                    url_file_idx = int(candidate_file_idx)
                break
    torbox_id = (
        raw.get("torboxId")
        or raw.get("torrentId")
        or hints.get("torboxId")
        or hints.get("torrentId")
    )
    magnet = str(raw.get("magnet") or "")
    if not info_hash and magnet:
        match = re.search(r"(?:\?|&)xt=urn:btih:([A-Za-z0-9]+)", magnet, re.I)
        if match:
            info_hash = match.group(1).lower()
    if not magnet and info_hash:
        magnet = f"magnet:?xt=urn:btih:{info_hash}"
    cached = looks_cached(raw, searchable, url)
    return {
        "title": str(raw.get("name") or source_name),
        "label": " • ".join(filter(None, [quality, dynamic_range, codec, audio])) or "Stream",
        "quality": quality,
        "cached": cached,
        "hdr": bool(dynamic_range and dynamic_range != "SDR"),
        "dynamic_range": dynamic_range,
        "codec": codec,
        "audio": audio,
        "size_gb": size_gb,
        "file_name": file_name,
        "source": source_name,
        "seeders": raw.get("seeders") or hints.get("seeders"),
        "url": url,
        "headers": hints.get("proxyHeaders", {}).get("request") or hints.get("headers") or {},
        "info_hash": info_hash,
        "torbox_id": torbox_id,
        "magnet": magnet,
        "file_idx": raw.get("fileIdx") if raw.get("fileIdx") is not None else url_file_idx,
        "can_play_now": bool(url),
        "can_add": bool(torbox_id or magnet or info_hash),
        "season": season or None,
        "episode": episode or None,
    }


def extract_quality(value: str) -> str:
    lower = value.lower()
    for needle, label in (
        ("2160p", "4K"), ("4k", "4K"), ("1440p", "1440p"),
        ("1080p", "1080p"), ("720p", "720p"), ("480p", "480p"),
    ):
        if needle in lower:
            return label
    return ""


def extract_dynamic_range(value: str) -> str:
    upper = value.upper()
    if "DOLBY VISION" in upper or re.search(r"\bDV\b", upper):
        return "Dolby Vision"
    if "HDR10+" in upper:
        return "HDR10+"
    if "HDR10" in upper:
        return "HDR10"
    if re.search(r"\bHDR\b", upper):
        return "HDR"
    return ""


def extract_codec(value: str) -> str:
    upper = value.upper()
    if any(item in upper for item in ("HEVC", "H.265", "H265", "X265")):
        return "HEVC"
    if any(item in upper for item in ("AVC", "H.264", "H264", "X264")):
        return "H.264"
    if "AV1" in upper:
        return "AV1"
    return ""


def extract_audio(value: str) -> str:
    upper = value.upper()
    for needle, label in (
        ("TRUEHD", "TrueHD"), ("ATMOS", "Atmos"), ("DTS-HD", "DTS-HD"),
        ("DTS", "DTS"), ("EAC3", "E-AC-3"), ("DDP", "E-AC-3"),
        ("AAC", "AAC"), ("AC3", "AC-3"),
    ):
        if needle in upper:
            return label
    return ""


def parse_size_gb(value: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(GB|GiB|MB|MiB)\b", value, re.I)
    if not match:
        return 0.0
    number = float(match.group(1))
    return round(number / 1024, 2) if match.group(2).lower().startswith("m") else round(number, 2)


def looks_cached(raw: dict, searchable: str, url: str) -> bool:
    if raw.get("cached") is True:
        return True
    lower = searchable.lower()
    return bool(
        url
        and (
            any(token in lower for token in ("cached", "[tb+]", "[rd+]", "instant"))
            or "torbox" in url.lower()
        )
    )


def stream_sort_key(stream: dict):
    rank = {
        "4K": 5,
        "1440p": 4,
        "1080p": 3,
        "720p": 2,
        "480p": 1,
    }.get(stream.get("quality"), 0)
    return (
        1 if stream.get("can_play_now") and stream.get("cached") else 0,
        1 if stream.get("can_play_now") else 0,
        1 if stream.get("cached") else 0,
        rank,
        float(stream.get("size_gb") or 0),
    )


def deduplicate_streams(streams: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for stream in streams:
        key = (
            stream.get("torbox_id")
            or stream.get("info_hash")
            or stream.get("magnet")
            or stream.get("url")
            or stream.get("file_name")
        )
        key = str(key or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(stream)
    return sorted(unique, key=stream_sort_key, reverse=True)


def select_automatic_stream(
    streams: list[dict],
    profile: str = "best",
    *,
    cached_only: bool = True,
    max_size_gb: float = 0,
) -> dict | None:
    candidates = [
        stream for stream in streams
        if stream.get("can_add") and (stream.get("cached") or not cached_only)
    ]
    if max_size_gb > 0:
        candidates = [
            stream for stream in candidates
            if not stream.get("size_gb") or float(stream["size_gb"]) <= max_size_gb
        ]
    if not candidates:
        return None
    quality_rank = {"4K": 5, "1440p": 4, "1080p": 3, "720p": 2, "480p": 1}
    profile = profile if profile in {"best", "4k", "1080p"} else "best"
    if profile == "1080p":
        preferred = [
            stream for stream in candidates
            if quality_rank.get(stream.get("quality"), 0) <= quality_rank["1080p"]
        ]
        if preferred:
            candidates = preferred

    def selection_key(stream: dict):
        rank = quality_rank.get(stream.get("quality"), 0)
        if profile == "4k":
            profile_rank = 2 if stream.get("quality") == "4K" else 1
        elif profile == "1080p":
            profile_rank = 2 if stream.get("quality") == "1080p" else 1
        else:
            profile_rank = 1
        return (
            1 if stream.get("cached") else 0,
            profile_rank,
            rank,
            float(stream.get("size_gb") or 0),
            int(stream.get("seeders") or 0),
        )

    return max(candidates, key=selection_key)


class TorBoxClient:
    base_url = "https://api.torbox.app/v1/api"

    def __init__(self, api_key: str):
        self.api_key = api_key.strip()

    @property
    def headers(self) -> dict:
        if not self.api_key:
            raise IntegrationError("Add a TorBox API key in Vortexo settings")
        return {"Authorization": f"Bearer {self.api_key}"}

    def health(self) -> dict:
        payload = json_request(f"{self.base_url}/user/me", headers=self.headers, timeout=15)
        return {
            "online": bool(isinstance(payload, dict) and payload.get("success")),
            "detail": payload.get("detail", "Connected") if isinstance(payload, dict) else "Connected",
        }

    def check_cached(self, hashes: list[str]) -> dict:
        hashes = [value.lower() for value in hashes if value]
        if not hashes:
            return {}
        payload = json_request(
            f"{self.base_url}/torrents/checkcached",
            method="POST",
            headers=self.headers,
            payload={"hashes": hashes},
        )
        data = payload.get("data") if isinstance(payload, dict) else {}
        if isinstance(data, list):
            return {hashes[index]: value for index, value in enumerate(data) if index < len(hashes)}
        return data if isinstance(data, dict) else {}

    def create_torrent(self, magnet: str, *, cached_only: bool = False) -> dict:
        if not magnet:
            raise IntegrationError("The selected stream cannot be added to TorBox")
        boundary = "----Vortexo" + uuid.uuid4().hex
        fields = {
            "magnet": magnet,
            "seed": "3",
            "allow_zip": "false",
            "add_only_if_cached": "true" if cached_only else "false",
        }
        body = b""
        for key, value in fields.items():
            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
            body += str(value).encode() + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            f"{self.base_url}/torrents/createtorrent",
            data=body,
            method="POST",
            headers={
                **self.headers,
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "Plex-Vortexo/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read().decode("utf-8"))
                detail = payload.get("detail") or payload.get("error")
            except Exception:
                detail = None
            if error.code == 400 and detail and "duplicate" in detail.lower():
                return {"duplicate": True, "detail": detail}
            raise IntegrationError(detail or f"TorBox returned HTTP {error.code}") from error
        data = payload.get("data") if isinstance(payload, dict) else None
        result = data if isinstance(data, dict) else {}
        result["detail"] = payload.get("detail", "Torrent accepted") if isinstance(payload, dict) else "Torrent accepted"
        return result

    def torrents(self, *, bypass_cache: bool = True) -> list[dict]:
        query = urllib.parse.urlencode({"bypass_cache": str(bypass_cache).lower(), "limit": 1000})
        payload = json_request(
            f"{self.base_url}/torrents/mylist?{query}",
            headers=self.headers,
        )
        data = payload.get("data") if isinstance(payload, dict) else []
        return data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])

    def find_torrent(
        self,
        info_hash: str,
        name: str = "",
        torrent_id: int | str | None = None,
    ) -> dict | None:
        wanted_hash = info_hash.lower().strip()
        wanted_name = normalise_title(name)
        for torrent in self.torrents():
            if torrent_id is not None and str(
                torrent.get("id") or torrent.get("torrent_id") or ""
            ) == str(torrent_id):
                return torrent
            hashes = [
                str(torrent.get("hash") or "").lower(),
                str(torrent.get("info_hash") or "").lower(),
            ]
            if wanted_hash and wanted_hash in hashes:
                return torrent
            if wanted_name and normalise_title(str(torrent.get("name") or "")) == wanted_name:
                return torrent
        return None

    def request_download_url(self, torrent_id: int | str, file_id: int | str) -> str:
        query = urllib.parse.urlencode(
            {"torrent_id": torrent_id, "file_id": file_id, "redirect": "false"}
        )
        payload = json_request(
            f"{self.base_url}/torrents/requestdl?{query}",
            headers=self.headers,
        )
        data = payload.get("data") if isinstance(payload, dict) else ""
        if isinstance(data, dict):
            data = data.get("url") or data.get("download_url") or ""
        if not data:
            raise IntegrationError("TorBox did not return a playable URL")
        return str(data)


def normalise_title(value: str) -> str:
    value = re.sub(r"\{(?:tmdb|tvdb)-[^{}]+\}", " ", value or "", flags=re.I)
    value = re.sub(r"\(\d{4}\)", " ", value)
    value = re.split(
        r"(?:\bS\d{1,2}(?:E\d{1,3})?\b|\b\d{1,2}x\d{1,3}\b|"
        r"\b(?:19|20)\d{2}\b|\b2160p\b|\b1080p\b|\b720p\b|\b4k\b|"
        r"\buhd\b|\bcomplete\b|\bseason\s*\d+\b)",
        value,
        maxsplit=1,
        flags=re.I,
    )[0]
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def release_matches_media(media: dict, torrent: dict, video: dict) -> bool:
    """Fail closed when TorBox returned a release for a different title."""
    requested_title = str(
        (media.get("parent_title") or media.get("title"))
        if media.get("type") == "episode"
        else media.get("title")
        or ""
    ).strip()
    release_text = " ".join(
        (
            str(torrent.get("name") or ""),
            str(video.get("path") or video.get("name") or ""),
        )
    )
    if not requested_title or not release_text.strip():
        return False

    requested_year = str(media.get("year") or "").strip()
    release_years = set(re.findall(r"\b(?:19|20)\d{2}\b", release_text))
    if requested_year and release_years and requested_year not in release_years:
        return False

    def tokens(value: str) -> list[str]:
        value = value.replace("&", " and ")
        words = re.findall(r"[a-z0-9]+", value.casefold())
        significant = [word for word in words if word not in {"a", "an", "the"}]
        return significant or words

    requested_tokens = tokens(requested_title)
    release_tokens = tokens(release_text)
    if not requested_tokens or not release_tokens:
        return False

    # Require the complete requested title in the same order. Extra release
    # metadata may appear between words, but a partial token overlap is unsafe.
    position = 0
    for token in release_tokens:
        if token == requested_tokens[position]:
            position += 1
            if position == len(requested_tokens):
                return True
    return False


def torrent_completed(torrent: dict) -> bool:
    return bool(
        torrent.get("cached")
        or torrent.get("download_finished")
        or str(torrent.get("download_state") or "").lower()
        in {"cached", "completed", "downloaded", "finished", "uploading"}
    )


def torrent_video_files(torrent: dict) -> list[dict]:
    rows = torrent.get("files") or torrent.get("content") or []
    videos = []
    for index, item in enumerate(rows):
        if isinstance(item, str):
            path, file_id, size = item, index, 0
        elif isinstance(item, dict):
            path = str(item.get("name") or item.get("path") or "")
            file_id = item.get("id", item.get("file_id", index))
            size = item.get("size") or item.get("bytes") or 0
        else:
            continue
        if os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS:
            videos.append({"path": path, "file_id": file_id, "size": size})
    return videos


def choose_video_file(torrent: dict, season: int = 0, episode: int = 0, file_idx=None) -> dict | None:
    videos = torrent_video_files(torrent)
    if file_idx is not None:
        for video in videos:
            if str(video["file_id"]) == str(file_idx):
                return video
    if season and episode:
        marker = re.compile(rf"\bS0*{season}E0*{episode}\b|\b0*{season}x0*{episode}\b", re.I)
        for video in videos:
            if marker.search(video["path"]):
                return video
    return max(videos, key=lambda item: int(item.get("size") or 0), default=None)
