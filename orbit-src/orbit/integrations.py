"""Network adapters for metadata search and automatic list imports."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import __version__


class IntegrationError(RuntimeError):
    pass


def _json_request(url: str, headers: dict | None = None, timeout: int = 20):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Orbit/0.1", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8")).get("error")
        except Exception:
            detail = None
        raise IntegrationError(detail or f"Remote service returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise IntegrationError(f"Could not reach remote service: {error}") from error


def search_tmdb(query: str, api_key: str, media_type: str = "multi") -> list[dict]:
    if not api_key:
        raise IntegrationError("Add a TMDb API key in Settings before searching")
    if not query.strip():
        return []
    endpoint_type = media_type if media_type in ("movie", "tv") else "multi"
    url = "https://api.themoviedb.org/3/search/{}?{}".format(
        endpoint_type,
        urllib.parse.urlencode({"api_key": api_key, "query": query.strip(), "include_adult": "false"}),
    )
    payload = _json_request(url)
    results = []
    for item in payload.get("results", []):
        kind = item.get("media_type") or endpoint_type
        if kind not in ("movie", "tv"):
            continue
        date = item.get("release_date") or item.get("first_air_date") or ""
        results.append({
            "tmdb_id": item.get("id"),
            "media_type": "show" if kind == "tv" else "movie",
            "title": item.get("title") or item.get("name") or "Unknown",
            "year": int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None,
            "overview": item.get("overview") or "",
            "poster_path": item.get("poster_path") or "",
            "popularity": item.get("popularity") or 0,
        })
    return results[:30]


def _mdblist_api_url(list_url: str) -> str:
    parsed = urllib.parse.urlparse(list_url.strip())
    if parsed.netloc in ("api.mdblist.com",):
        return list_url.rstrip("/")
    match = re.match(r"^/lists/([^/]+)/([^/?#]+)", parsed.path)
    if parsed.netloc in ("mdblist.com", "www.mdblist.com") and match:
        user, slug = match.groups()
        return f"https://api.mdblist.com/lists/{user}/{slug}/items"
    raise IntegrationError("Use an MDBList list URL such as https://mdblist.com/lists/user/list-name")


def _normalise_item(item: dict) -> dict | None:
    nested = item.get("movie") or item.get("show") or item
    ids = nested.get("ids") or item.get("ids") or {}
    raw_type = item.get("mediatype") or item.get("media_type") or item.get("type")
    if item.get("show") is not None:
        raw_type = "show"
    elif item.get("movie") is not None:
        raw_type = "movie"
    media_type = "show" if str(raw_type).lower() in ("show", "tv", "tvshow", "series") else "movie"
    tmdb_id = nested.get("tmdb_id") or nested.get("tmdbid") or ids.get("tmdb")
    imdb_id = nested.get("imdb_id") or nested.get("imdbid") or ids.get("imdb") or ""
    title = nested.get("title") or nested.get("name")
    if not title or (not tmdb_id and not imdb_id):
        return None
    year = nested.get("year")
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    return {
        "media_type": media_type,
        "title": title,
        "year": year,
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "poster_path": nested.get("poster_path") or nested.get("poster") or "",
        "overview": nested.get("overview") or nested.get("description") or "",
    }


def fetch_mdblist(list_url: str, api_key: str, limit: int = 100) -> list[dict]:
    if not api_key:
        raise IntegrationError("Add an MDBList API key in Settings")
    item_limit = max(1, min(limit, 1000))
    parsed = urllib.parse.urlparse(_mdblist_api_url(list_url))
    query = urllib.parse.parse_qs(parsed.query)
    query.update({"apikey": [api_key], "limit": [str(item_limit)]})
    endpoint = urllib.parse.urlunparse(parsed._replace(
        query=urllib.parse.urlencode(query, doseq=True)
    ))
    payload = _json_request(endpoint)
    if isinstance(payload, dict):
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raw_items = [
                *(payload.get("movies") or []),
                *(payload.get("shows") or []),
            ]
    else:
        raw_items = payload
    if not isinstance(raw_items, list):
        raise IntegrationError("MDBList returned an unsupported list response")
    items = []
    for raw in raw_items[:item_limit]:
        normalised = _normalise_item(raw)
        if normalised:
            items.append(normalised)
    return items


def fetch_trakt(list_url: str, client_id: str, limit: int = 100) -> list[dict]:
    if not client_id:
        raise IntegrationError("Add a Trakt client ID in Settings")
    parsed = urllib.parse.urlparse(list_url.strip())
    match = re.match(r"^/users/([^/]+)/lists/([^/?#]+)", parsed.path)
    if parsed.netloc not in ("trakt.tv", "www.trakt.tv") or not match:
        raise IntegrationError("Use a Trakt list URL such as https://trakt.tv/users/user/lists/list-name")
    user, slug = match.groups()
    url = f"https://api.trakt.tv/users/{user}/lists/{slug}/items/movies,shows?extended=full&limit={limit}"
    payload = _json_request(url, {
        "trakt-api-version": "2",
        "trakt-api-key": client_id,
    })
    items = []
    for raw in payload[: max(1, min(limit, 1000))]:
        normalised = _normalise_item(raw)
        if normalised:
            items.append(normalised)
    return items


def _normalise_plex_watchlist_item(item: dict) -> dict | None:
    media_type = "show" if item.get("type") in ("show", "series") else "movie"
    if item.get("type") not in ("movie", "show", "series"):
        return None
    tmdb_id = None
    imdb_id = ""
    for guid in item.get("Guid") or []:
        value = guid.get("id", "") if isinstance(guid, dict) else str(guid)
        if value.startswith("tmdb://"):
            raw_id = value.removeprefix("tmdb://").split("?", 1)[0]
            if raw_id.isdigit():
                tmdb_id = int(raw_id)
        elif value.startswith("imdb://"):
            imdb_id = value.removeprefix("imdb://").split("?", 1)[0]
    title = item.get("title")
    if not title or (not tmdb_id and not imdb_id):
        return None
    try:
        year = int(item["year"]) if item.get("year") else None
    except (TypeError, ValueError):
        year = None
    return {
        "media_type": media_type,
        "title": title,
        "year": year,
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "poster_path": item.get("thumb") or "",
        "overview": item.get("summary") or "",
    }


def fetch_plex_watchlist(token: str, limit: int = 100) -> list[dict]:
    """Fetch the signed-in Plex account's universal movie/show watchlist."""
    if not token:
        raise IntegrationError("Add a Plex token in Settings")
    item_limit = max(1, min(limit, 1000))
    endpoint = "https://discover.provider.plex.tv/library/sections/watchlist/all"
    headers = {
        "X-Plex-Token": token,
        "X-Plex-Product": "Orbit",
        "X-Plex-Version": __version__,
        "X-Plex-Client-Identifier": "orbit-umbrel",
    }
    items = []
    seen = set()
    start = 0
    while len(items) < item_limit:
        page_size = min(50, item_limit - len(items))
        url = "{}?{}".format(endpoint, urllib.parse.urlencode({
            "includeAdvanced": "1",
            "includeMeta": "1",
            "includeExternalMedia": "1",
            "includeGuids": "1",
            "X-Plex-Container-Start": str(start),
            "X-Plex-Container-Size": str(page_size),
        }))
        payload = _json_request(url, headers)
        container = payload.get("MediaContainer", {}) if isinstance(payload, dict) else {}
        raw_items = container.get("Metadata") or []
        if not isinstance(raw_items, list):
            raise IntegrationError("Plex returned an unsupported watchlist response")
        for raw in raw_items:
            normalised = _normalise_plex_watchlist_item(raw)
            if not normalised:
                continue
            identity = (
                normalised["media_type"],
                normalised.get("tmdb_id") or normalised.get("imdb_id"),
            )
            if identity not in seen:
                seen.add(identity)
                items.append(normalised)
                if len(items) >= item_limit:
                    break
        start += len(raw_items)
        try:
            total_size = int(container.get("totalSize", start))
        except (TypeError, ValueError):
            total_size = start
        if not raw_items or len(raw_items) < page_size or start >= total_size:
            break
    return items


def fetch_list(source: dict, settings: dict) -> list[dict]:
    if source["kind"] == "mdblist":
        return fetch_mdblist(source["url"], settings.get("mdblist_api_key", ""), source["max_items"])
    return fetch_trakt(source["url"], settings.get("trakt_client_id", ""), source["max_items"])
