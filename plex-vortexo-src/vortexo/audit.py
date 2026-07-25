from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

from .integrations import (
    AUTOMATIC_EPISODE_MIN_SIZE_GB,
    AUTOMATIC_MOVIE_MIN_SIZE_GB,
    EXTRA_VIDEO_PATTERN,
    POOR_RELEASE_PATTERN,
    extract_quality,
)


def _tokens(value: str) -> list[str]:
    value = value.replace("&", " and ")
    words = re.findall(r"[a-z0-9]+", value.casefold())
    return [word for word in words if word not in {"a", "an", "the"}] or words


def _title_matches(requested: str, release: str) -> bool:
    wanted = _tokens(requested)
    available = _tokens(release)
    if not wanted or not available:
        return False
    position = 0
    for token in available:
        if token == wanted[position]:
            position += 1
            if position == len(wanted):
                return True
    return False


def _movie_identity(path: Path, movies_root: Path) -> dict | None:
    try:
        folder = path.relative_to(movies_root).parts[0]
    except (ValueError, IndexError):
        return None
    year_match = re.search(r"\(((?:19|20)\d{2})\)", folder)
    title = re.sub(r"\{(?:tmdb|tvdb)-[^{}]+\}", " ", folder, flags=re.I)
    title = re.sub(r"\s*\((?:19|20)\d{2}\)\s*", " ", title)
    return {
        "type": "movie",
        "title": " ".join(title.split()),
        "year": year_match.group(1) if year_match else "",
    }


def _episode_identity(path: Path, tv_root: Path) -> dict | None:
    try:
        parts = path.relative_to(tv_root).parts
    except ValueError:
        return None
    if len(parts) < 3:
        return None
    marker = re.search(r"\bS(\d{1,2})E(\d{1,3})\b", path.name, re.I)
    if not marker:
        return None
    return {
        "type": "episode",
        "title": parts[0],
        "season": int(marker.group(1)),
        "episode": int(marker.group(2)),
    }


def classify_link(
    path: Path,
    *,
    source_root: Path,
    movies_root: Path,
    tv_root: Path,
) -> dict:
    source_root = source_root.resolve()
    movies_root = movies_root.resolve()
    tv_root = tv_root.resolve()
    path = path.parent.resolve() / path.name
    media = (
        _movie_identity(path, movies_root)
        if path.is_relative_to(movies_root)
        else _episode_identity(path, tv_root)
    )
    target = os.readlink(path)
    resolved = Path(os.path.realpath(path))
    result = {
        "path": str(path),
        "target": target,
        "resolved_target": str(resolved),
        "media": media or {},
        "classification": "ambiguous",
        "reason": "Could not derive a safe media identity",
        "high_confidence": False,
    }
    try:
        resolved.relative_to(source_root)
    except ValueError:
        result.update(
            classification="unsafe_target",
            reason="Symlink points outside the TorBox source mount",
            high_confidence=True,
        )
        return result
    if not media or not media.get("title"):
        return result

    requested_title = str(media["title"])
    release_text = target
    if (
        EXTRA_VIDEO_PATTERN.search(release_text)
        and not EXTRA_VIDEO_PATTERN.search(requested_title)
    ):
        result.update(
            classification="extra_video",
            reason="Target is a trailer, teaser, sample, featurette, or extra",
            high_confidence=True,
        )
        return result
    if POOR_RELEASE_PATTERN.search(release_text):
        result.update(
            classification="poor_release",
            reason="Target is marked as a CAM, telesync, screener, or workprint",
            high_confidence=True,
        )
        return result

    requested_year = str(media.get("year") or "")
    release_years = set(re.findall(r"\b(?:19|20)\d{2}\b", release_text))
    year_conflict = bool(
        requested_year and release_years and requested_year not in release_years
    )
    title_match = _title_matches(requested_title, release_text)
    requested_tokens = set(_tokens(requested_title))
    release_tokens = set(_tokens(release_text))
    overlap = requested_tokens.intersection(release_tokens)
    if not title_match and (year_conflict or not overlap):
        result.update(
            classification="wrong_title",
            reason="Target title does not match the Plex destination",
            high_confidence=True,
        )
        return result
    if title_match and year_conflict and len(requested_tokens) == 1:
        source_relative = resolved.relative_to(source_root)
        release_heading = source_relative.parts[0] if source_relative.parts else ""
        heading_before_year = re.split(r"\b(?:19|20)\d{2}\b", release_heading, maxsplit=1)[0]
        extra_heading_tokens = set(_tokens(heading_before_year)) - requested_tokens
        if extra_heading_tokens:
            result.update(
                classification="wrong_title_year",
                reason="Single-word title expanded to a different release with a conflicting year",
                high_confidence=True,
            )
            return result

    quality = extract_quality(path.name)
    try:
        size_gb = path.stat().st_size / (1024 ** 3)
    except OSError:
        size_gb = 0
    minimums = (
        AUTOMATIC_EPISODE_MIN_SIZE_GB
        if media.get("type") == "episode"
        else AUTOMATIC_MOVIE_MIN_SIZE_GB
    )
    if quality and size_gb and size_gb < minimums.get(quality, 0):
        result.update(
            classification="undersized_release",
            reason=f"{quality} target is only {size_gb:.2f} GB",
            high_confidence=True,
        )
        return result
    if not title_match:
        result.update(
            classification="ambiguous_title",
            reason="Title only partially matches; manual review required",
        )
        return result
    if year_conflict:
        result.update(
            classification="ambiguous_year",
            reason="Title matches but release year differs; manual review required",
        )
        return result
    result.update(
        classification="safe",
        reason="Title, year, feature type, and minimum quality checks passed",
    )
    return result


def audit_library(source_root: Path, movies_root: Path, tv_root: Path) -> list[dict]:
    rows = []
    for root in (movies_root, tv_root):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                rows.append(
                    classify_link(
                        path,
                        source_root=source_root,
                        movies_root=movies_root,
                        tv_root=tv_root,
                    )
                )
    return rows


def quarantine(rows: list[dict], library_root: Path) -> tuple[Path, list[dict]]:
    library_root = library_root.resolve()
    quarantine_root = library_root / ".quarantine" / time.strftime("%Y%m%d-%H%M%S")
    moved = []
    for row in rows:
        if not row.get("high_confidence"):
            continue
        source = Path(row["path"])
        relative = source.relative_to(library_root)
        destination = quarantine_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"Refusing to replace quarantine entry: {destination}")
        os.replace(source, destination)
        moved.append({**row, "quarantined_path": str(destination)})
    if moved:
        manifest = quarantine_root / "manifest.jsonl"
        with manifest.open("w", encoding="utf-8") as handle:
            for row in moved:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
    return quarantine_root, moved


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit Plex-TorBox symlinks and quarantine only high-confidence bad links."
    )
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--movies-root", type=Path, required=True)
    parser.add_argument("--tv-root", type=Path, required=True)
    parser.add_argument("--library-root", type=Path, required=True)
    parser.add_argument("--quarantine", action="store_true")
    args = parser.parse_args()
    rows = audit_library(
        args.source_root.resolve(),
        args.movies_root.resolve(),
        args.tv_root.resolve(),
    )
    candidates = [row for row in rows if row["classification"] != "safe"]
    for row in candidates:
        print(json.dumps(row, sort_keys=True))
    moved = []
    quarantine_root = None
    if args.quarantine:
        quarantine_root, moved = quarantine(rows, args.library_root.resolve())
    summary = {
        "scanned": len(rows),
        "safe": sum(row["classification"] == "safe" for row in rows),
        "high_confidence_bad": sum(row["high_confidence"] for row in rows),
        "ambiguous": sum(
            row["classification"].startswith("ambiguous") for row in rows
        ),
        "quarantined": len(moved),
        "quarantine_root": str(quarantine_root) if quarantine_root else "",
    }
    print(json.dumps({"summary": summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
