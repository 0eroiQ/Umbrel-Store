"""Recover delayed debrid mount handoffs into Orbit's Plex library."""

from __future__ import annotations

import hashlib
import os
import re
import sys
from types import SimpleNamespace


_ID_FOLDER = re.compile(
    r"^(?P<title>.+?)(?: \((?P<year>\d{4})\))? "
    r"\{(?P<provider>tmdb|tvdb)-(?P<guid>\d+)\}$",
    re.IGNORECASE,
)
_TV_MARKER = re.compile(
    r"(?:^|[^a-z0-9])(?:s\d{1,2}(?:e\d{1,3})?|\d{1,2}x\d{1,3}|season[ ._-]*\d{1,2})(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)


def _load_symlinker():
    engine_root = os.environ.get("PD_ROOT", "/app/plex_debrid")
    if engine_root not in sys.path:
        sys.path.insert(0, engine_root)
    import library_symlinker  # type: ignore

    return library_symlinker


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def _library_folders(library_dirs: dict[str, str]) -> list[dict]:
    folders = []
    for kind, library_dir in library_dirs.items():
        try:
            entries = os.listdir(library_dir)
        except OSError:
            continue
        for entry in entries:
            path = os.path.join(library_dir, entry)
            match = _ID_FOLDER.fullmatch(entry)
            if not match or not os.path.isdir(path) or os.path.islink(path):
                continue
            folders.append({
                "kind": kind,
                "path": path,
                "title": match.group("title"),
                "title_key": _normalized(match.group("title")),
                "year": int(match.group("year")) if match.group("year") else None,
                "provider": match.group("provider").lower(),
                "guid": match.group("guid"),
            })
    return folders


def _match_source(source_name: str, folders: list[dict]) -> dict | None:
    source_key = _normalized(source_name)
    source_kind = "tv" if _TV_MARKER.search(source_name) else None
    candidates = []
    for folder in folders:
        title_key = folder["title_key"]
        if (source_kind and folder["kind"] != source_kind) or not title_key:
            continue
        if f" {title_key} " in f" {source_key} ":
            candidates.append(folder)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (len(item["title_key"]), item["kind"] == "movie"),
    )


def _source_marker(folder_path: str, source_name: str) -> str:
    digest = hashlib.sha256(source_name.encode("utf-8")).hexdigest()[:16]
    return os.path.join(folder_path, f".orbit-source-{digest}")


def reconcile_mounted_sources(
    mount_dir: str,
    library_dirs: dict[str, str],
    *,
    symlinker=None,
    log_fn=None,
    max_sources: int = 1000,
) -> list[tuple[str, str]]:
    """Link mounted transfers that appeared after the downloader returned.

    Premiumize and other WebDAV providers may expose a transfer after Orbit's
    bounded immediate check, and may decorate its folder name. Match the
    canonical reserved title folder inside that provider-owned mount and reuse
    the bundled symlinker's safe public link operation.
    """
    symlinker = symlinker or _load_symlinker()
    raw_root = os.path.join(mount_dir, ".vortexo-source")
    folders = _library_folders(library_dirs)
    if not folders:
        return []
    try:
        source_names = sorted(os.listdir(raw_root))[:max_sources]
    except OSError:
        return []

    changed = []
    for source_name in source_names:
        source_path = os.path.join(raw_root, source_name)
        if not (os.path.isdir(source_path) or os.path.isfile(source_path)):
            continue
        folder = _match_source(source_name, folders)
        if not folder:
            continue
        marker = _source_marker(folder["path"], source_name)
        if os.path.isfile(marker) and not os.path.islink(marker):
            continue
        item_type = "show" if folder["kind"] == "tv" else "movie"
        item = SimpleNamespace(
            type=item_type,
            title=folder["title"],
            year=folder["year"],
            EID=[f"{folder['provider']}://{folder['guid']}"],
            Releases=[SimpleNamespace(torrent_name=source_name, title=source_name)],
        )
        linked = symlinker.symlink_item(
            item,
            mount_dir,
            library_dirs,
            log_fn=log_fn,
            mount_attempts=1,
            retry_delay=0,
        )
        if not linked:
            continue
        try:
            with open(marker, "x", encoding="utf-8"):
                pass
        except FileExistsError:
            pass
        except OSError as error:
            if log_fn:
                log_fn(f"could not remember mounted source {source_name!r}: {error}")
        changed.append(("show" if folder["kind"] == "tv" else "movie", folder["path"]))
    return changed
