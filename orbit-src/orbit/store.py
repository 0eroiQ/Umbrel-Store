"""SQLite-backed state for Orbit requests, list imports, and settings."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._migrate()

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self):
        with self._lock, self.connection() as db:
            db.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    secret INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_key TEXT NOT NULL,
                    media_type TEXT NOT NULL CHECK(media_type IN ('movie', 'show')),
                    title TEXT NOT NULL,
                    year INTEGER,
                    tmdb_id INTEGER,
                    imdb_id TEXT,
                    plex_guid TEXT NOT NULL DEFAULT '',
                    poster_path TEXT,
                    overview TEXT,
                    source TEXT NOT NULL DEFAULT 'manual',
                    source_ref TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    status_detail TEXT NOT NULL DEFAULT '',
                    profile TEXT NOT NULL DEFAULT 'best',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(media_key, source, source_ref)
                );
                CREATE TABLE IF NOT EXISTS request_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
                    state TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS requests_status
                    ON requests(status);
                CREATE TABLE IF NOT EXISTS list_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('mdblist', 'trakt')),
                    url TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    mode TEXT NOT NULL DEFAULT 'import_only',
                    profile TEXT NOT NULL DEFAULT 'best',
                    max_items INTEGER NOT NULL DEFAULT 100,
                    last_sync_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(kind, url)
                );
                CREATE TABLE IF NOT EXISTS plex_library (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plex_rating_key TEXT NOT NULL,
                    section_id TEXT NOT NULL,
                    media_type TEXT NOT NULL CHECK(media_type IN ('movie', 'show')),
                    title TEXT NOT NULL,
                    year INTEGER,
                    tmdb_id INTEGER,
                    imdb_id TEXT NOT NULL DEFAULT '',
                    thumb TEXT NOT NULL DEFAULT '',
                    quality TEXT NOT NULL DEFAULT 'Quality unavailable',
                    versions_json TEXT NOT NULL DEFAULT '[]',
                    seasons_json TEXT NOT NULL DEFAULT '[]',
                    upgrade_available INTEGER NOT NULL DEFAULT 0,
                    episode_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    UNIQUE(section_id, plex_rating_key)
                );
                CREATE INDEX IF NOT EXISTS plex_library_tmdb
                    ON plex_library(media_type, tmdb_id);
                CREATE INDEX IF NOT EXISTS plex_library_imdb
                    ON plex_library(imdb_id);
                CREATE TABLE IF NOT EXISTS plex_library_sync (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    status TEXT NOT NULL DEFAULT 'never',
                    item_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    synced_at TEXT
                );
                INSERT OR IGNORE INTO plex_library_sync(id) VALUES (1);
                """
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(plex_library)").fetchall()
            }
            if "seasons_json" not in columns:
                db.execute(
                    "ALTER TABLE plex_library ADD COLUMN seasons_json TEXT NOT NULL DEFAULT '[]'"
                )
            request_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(requests)").fetchall()
            }
            if "plex_guid" not in request_columns:
                db.execute(
                    "ALTER TABLE requests ADD COLUMN plex_guid TEXT NOT NULL DEFAULT ''"
                )

    def get_settings(self, reveal_secrets: bool = False) -> dict:
        with self.connection() as db:
            rows = db.execute("SELECT key, value, secret FROM settings").fetchall()
        result = {}
        for row in rows:
            value = row["value"]
            if row["secret"] and value and not reveal_secrets:
                value = "••••••••"
            result[row["key"]] = value
        return result

    def set_settings(self, values: dict, secret_keys: set[str]):
        now = utc_now()
        with self._lock, self.connection() as db:
            for key, value in values.items():
                if key in secret_keys and value == "••••••••":
                    continue
                db.execute(
                    """INSERT INTO settings(key, value, secret, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                         secret=excluded.secret, updated_at=excluded.updated_at""",
                    (key, str(value), int(key in secret_keys), now),
                )

    def add_request(self, item: dict, source: str = "manual", source_ref: str = "") -> tuple[dict, bool]:
        media_type, tmdb_id, imdb_id, media_key = self._request_identity(item)
        now = utc_now()
        with self._lock, self.connection() as db:
            # One title should enter the acquisition pipeline once even when it
            # appears in several automatic lists or is also requested manually.
            existing = db.execute(
                "SELECT * FROM requests WHERE media_key=? ORDER BY id DESC LIMIT 1",
                (media_key,),
            ).fetchone()
            if existing:
                plex_guid = str(item.get("plex_guid") or "")
                if plex_guid and not existing["plex_guid"]:
                    db.execute(
                        "UPDATE requests SET plex_guid=?, updated_at=? WHERE id=?",
                        (plex_guid, now, existing["id"]),
                    )
                    existing = db.execute(
                        "SELECT * FROM requests WHERE id=?", (existing["id"],)
                    ).fetchone()
                return dict(existing), False
            cursor = db.execute(
                """INSERT INTO requests
                   (media_key, media_type, title, year, tmdb_id, imdb_id,
                    plex_guid, poster_path, overview, source, source_ref,
                    profile, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    media_key, media_type, item.get("title") or item.get("name") or "Unknown",
                    item.get("year"), tmdb_id, imdb_id,
                    item.get("plex_guid") or "",
                    item.get("poster_path") or "", item.get("overview") or "",
                    source, source_ref, item.get("profile") or "best", now, now,
                ),
            )
            request_id = cursor.lastrowid
            db.execute(
                "INSERT INTO request_events(request_id, state, detail, created_at) VALUES (?, 'queued', ?, ?)",
                (request_id, f"Added from {source}", now),
            )
            row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            return dict(row), True

    @staticmethod
    def _request_identity(item: dict) -> tuple[str, object, str, str]:
        media_type = "show" if item.get("media_type") in ("tv", "show") else "movie"
        tmdb_id = item.get("tmdb_id") or item.get("id")
        imdb_id = str(item.get("imdb_id") or "")
        media_key = (
            f"tmdb:{media_type}:{tmdb_id}"
            if tmdb_id else f"imdb:{media_type}:{imdb_id}"
        )
        return media_type, tmdb_id, imdb_id, media_key

    def import_list_items(
        self,
        items: list[dict],
        source: str,
        source_ref: str,
        profile: str,
    ) -> dict:
        """Import a large automatic list in one transaction.

        Loading request and Plex identities once avoids opening two SQLite
        connections per item, which made 10,000+ item lists impractical.
        """
        now = utc_now()
        candidates = []
        seen = set()
        for item in items:
            media_type, tmdb_id, imdb_id, media_key = self._request_identity(item)
            if media_key in seen or (not tmdb_id and not imdb_id):
                continue
            seen.add(media_key)
            candidates.append((item, media_type, tmdb_id, imdb_id, media_key))

        added = 0
        skipped_existing = 0
        skipped_requested = 0
        with self._lock, self.connection() as db:
            request_keys = {
                row[0] for row in db.execute("SELECT media_key FROM requests")
            }
            plex_rows = db.execute(
                "SELECT media_type, tmdb_id, imdb_id, title, year FROM plex_library"
            ).fetchall()
            plex_tmdb = {
                (row["media_type"], str(row["tmdb_id"]))
                for row in plex_rows if row["tmdb_id"] is not None
            }
            plex_imdb = {
                str(row["imdb_id"]) for row in plex_rows if row["imdb_id"]
            }
            plex_titles: dict[tuple[str, str], set[object]] = {}
            for row in plex_rows:
                plex_titles.setdefault(
                    (row["media_type"], str(row["title"]).casefold()), set()
                ).add(row["year"])

            for item, media_type, tmdb_id, imdb_id, media_key in candidates:
                if media_key in request_keys:
                    skipped_requested += 1
                    continue
                years = plex_titles.get(
                    (media_type, str(item.get("title") or item.get("name") or "").casefold()),
                    set(),
                )
                year = item.get("year")
                in_plex = (
                    (tmdb_id is not None and (media_type, str(tmdb_id)) in plex_tmdb)
                    or (bool(imdb_id) and imdb_id in plex_imdb)
                    or bool(years and (year is None or year in years or None in years))
                )
                if in_plex:
                    skipped_existing += 1
                    continue
                cursor = db.execute(
                    """INSERT INTO requests
                       (media_key, media_type, title, year, tmdb_id, imdb_id,
                        plex_guid, poster_path, overview, source, source_ref,
                        profile, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        media_key, media_type,
                        item.get("title") or item.get("name") or "Unknown",
                        year, tmdb_id, imdb_id,
                        item.get("poster_path") or "", item.get("overview") or "",
                        source, source_ref, profile, now, now,
                    ),
                )
                db.execute(
                    """INSERT INTO request_events(request_id, state, detail, created_at)
                       VALUES (?, 'queued', ?, ?)""",
                    (cursor.lastrowid, f"Added from {source}", now),
                )
                request_keys.add(media_key)
                added += 1
        return {
            "added": added,
            "skipped_existing": skipped_existing,
            "skipped_requested": skipped_requested,
        }

    def list_handoff_candidates(self) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                """SELECT * FROM requests
                   WHERE status IN ('library_pending', 'needs_attention')
                   ORDER BY id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def recover_startup_states(self) -> dict:
        """Make request states truthful after an interrupted Orbit process."""
        now = utc_now()
        with self._lock, self.connection() as db:
            interrupted = db.execute(
                "SELECT id FROM requests WHERE status='searching'"
            ).fetchall()
            caught_up = db.execute(
                """SELECT id FROM requests
                   WHERE status='library_pending'
                     AND status_detail LIKE 'Series is caught up%'"""
            ).fetchall()
            for row in interrupted:
                detail = "Search was interrupted by an Orbit restart; library recovery will recheck it"
                db.execute(
                    "UPDATE requests SET status='needs_attention', status_detail=?, updated_at=? WHERE id=?",
                    (detail, now, row["id"]),
                )
                db.execute(
                    """INSERT INTO request_events(request_id, state, detail, created_at)
                       VALUES (?, 'needs_attention', ?, ?)""",
                    (row["id"], detail, now),
                )
            for row in caught_up:
                detail = "Series is caught up; future unaired episodes were ignored"
                db.execute(
                    "UPDATE requests SET status='ready', status_detail=?, updated_at=? WHERE id=?",
                    (detail, now, row["id"]),
                )
                db.execute(
                    """INSERT INTO request_events(request_id, state, detail, created_at)
                       VALUES (?, 'ready', ?, ?)""",
                    (row["id"], detail, now),
                )
        return {"interrupted": len(interrupted), "caught_up": len(caught_up)}

    def series_completion_count(self, run_key: str) -> int:
        with self.connection() as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM requests WHERE source='series-monitor' AND source_ref=?",
                (run_key,),
            ).fetchone()[0])

    def list_series_completion_candidates(self, limit: int = 1000) -> list[dict]:
        """Return owned series, oldest monitor check first."""
        with self.connection() as db:
            rows = db.execute(
                """SELECT p.*,
                          (
                            SELECT r.updated_at FROM requests r
                            WHERE r.media_key = CASE
                              WHEN p.tmdb_id IS NOT NULL
                                THEN 'tmdb:show:' || p.tmdb_id
                              ELSE 'imdb:show:' || p.imdb_id
                            END
                            ORDER BY r.id DESC LIMIT 1
                          ) AS request_updated_at
                   FROM plex_library p
                   WHERE p.media_type='show' AND p.episode_count > 0
                     AND (p.tmdb_id IS NOT NULL OR p.imdb_id != '')
                   ORDER BY request_updated_at IS NOT NULL, request_updated_at,
                            p.title COLLATE NOCASE
                   LIMIT ?""",
                (max(1, min(limit, 5000)),),
            ).fetchall()
        return [self._decode_library_row(row) for row in rows]

    def queue_series_completion(self, item: dict, run_key: str) -> tuple[dict, bool]:
        """Queue or requeue one owned show for an aired-episode check."""
        tmdb_id = item.get("tmdb_id")
        imdb_id = item.get("imdb_id") or ""
        media_key = f"tmdb:show:{tmdb_id}" if tmdb_id else f"imdb:show:{imdb_id}"
        now = utc_now()
        active_states = {"queued", "searching", "library_pending"}
        with self._lock, self.connection() as db:
            existing = db.execute(
                "SELECT * FROM requests WHERE media_key=? ORDER BY id DESC LIMIT 1",
                (media_key,),
            ).fetchone()
            if existing:
                if existing["status"] in active_states:
                    return dict(existing), False
                if existing["source"] == "series-monitor" and existing["source_ref"] == run_key:
                    return dict(existing), False
                db.execute(
                    """UPDATE requests
                       SET title=?, year=?, tmdb_id=?, imdb_id=?, poster_path=?,
                           source='series-monitor', source_ref=?, status='queued',
                           status_detail='', profile='best', updated_at=?
                       WHERE id=?""",
                    (
                        item.get("title") or "Unknown", item.get("year"), tmdb_id,
                        imdb_id, item.get("thumb") or item.get("poster_path") or "",
                        run_key, now, existing["id"],
                    ),
                )
                db.execute(
                    """INSERT INTO request_events(request_id, state, detail, created_at)
                       VALUES (?, 'queued', 'Checking for missing aired episodes', ?)""",
                    (existing["id"], now),
                )
                row = db.execute(
                    "SELECT * FROM requests WHERE id=?", (existing["id"],)
                ).fetchone()
                return dict(row), True
            cursor = db.execute(
                """INSERT INTO requests
                   (media_key, media_type, title, year, tmdb_id, imdb_id,
                    poster_path, overview, source, source_ref, profile,
                    created_at, updated_at)
                   VALUES (?, 'show', ?, ?, ?, ?, ?, '', 'series-monitor', ?,
                           'best', ?, ?)""",
                (
                    media_key, item.get("title") or "Unknown", item.get("year"),
                    tmdb_id, imdb_id, item.get("thumb") or item.get("poster_path") or "",
                    run_key, now, now,
                ),
            )
            db.execute(
                """INSERT INTO request_events(request_id, state, detail, created_at)
                   VALUES (?, 'queued', 'Checking for missing aired episodes', ?)""",
                (cursor.lastrowid, now),
            )
            row = db.execute(
                "SELECT * FROM requests WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            return dict(row), True

    def list_requests(self, limit: int = 100) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_plex_library(self, items: list[dict]):
        now = utc_now()
        with self._lock, self.connection() as db:
            db.execute("DELETE FROM plex_library")
            for item in items:
                db.execute(
                    """INSERT INTO plex_library
                       (plex_rating_key, section_id, media_type, title, year,
                        tmdb_id, imdb_id, thumb, quality, versions_json, seasons_json,
                        upgrade_available, episode_count, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item["plex_rating_key"], item["section_id"], item["media_type"],
                        item["title"], item.get("year"), item.get("tmdb_id"),
                        item.get("imdb_id") or "", item.get("thumb") or "",
                        item.get("quality") or "Quality unavailable",
                        json.dumps(item.get("versions") or []),
                        json.dumps(item.get("seasons") or []),
                        int(bool(item.get("upgrade_available"))),
                        int(item.get("episode_count") or 0), now,
                    ),
                )
            db.execute(
                """UPDATE plex_library_sync
                   SET status='ready', item_count=?, last_error='', synced_at=?
                   WHERE id=1""",
                (len(items), now),
            )

    def fail_plex_library_sync(self, error: str):
        with self._lock, self.connection() as db:
            db.execute(
                "UPDATE plex_library_sync SET status='error', last_error=? WHERE id=1",
                (error,),
            )

    def plex_library_status(self) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM plex_library_sync WHERE id=1").fetchone()
        return dict(row) if row else {
            "status": "never", "item_count": 0, "last_error": "", "synced_at": None
        }

    @staticmethod
    def _library_where(
        query: str = "", media_type: str = "", quality: str = "", status: str = ""
    ) -> tuple[str, list]:
        where = []
        values = []
        if query.strip():
            where.append("LOWER(title) LIKE ?")
            values.append(f"%{query.strip().lower()}%")
        if media_type in ("movie", "show"):
            where.append("media_type=?")
            values.append(media_type)
        quality_clauses = {
            "4k": "quality LIKE '4K%'",
            "1080p": "quality LIKE '1080p%'",
            "720p": "quality LIKE '720p%'",
            "sd": "(quality LIKE 'SD%' OR quality LIKE '576p%')",
            "unknown": "(quality='Quality unavailable' OR quality LIKE 'Unknown%')",
        }
        if quality in quality_clauses:
            where.append(quality_clauses[quality])
        if status == "upgrade":
            where.append("upgrade_available=1")
        elif status == "healthy":
            where.append("upgrade_available=0")
        elif status == "unknown":
            where.append("(quality='Quality unavailable' OR quality LIKE 'Unknown%')")
        return (f"WHERE {' AND '.join(where)}" if where else "", values)

    @staticmethod
    def _decode_library_row(row: sqlite3.Row, detailed: bool = True) -> dict:
        item = dict(row)
        item["versions"] = json.loads(item.pop("versions_json") or "[]")
        item["seasons"] = json.loads(item.pop("seasons_json") or "[]")
        if not detailed:
            item["versions"] = [
                {key: value for key, value in version.items() if key != "streams"}
                for version in item["versions"]
            ]
            item["seasons"] = [
                {key: value for key, value in season.items() if key != "episodes"}
                for season in item["seasons"]
            ]
        item["upgrade_available"] = bool(item["upgrade_available"])
        return item

    def list_plex_library(
        self,
        query: str = "",
        media_type: str = "",
        quality: str = "",
        status: str = "",
        sort: str = "title",
        limit: int = 120,
        offset: int = 0,
    ) -> list[dict]:
        clause, values = self._library_where(query, media_type, quality, status)
        orders = {
            "title": "title COLLATE NOCASE, year DESC",
            "year": "year IS NULL, year DESC, title COLLATE NOCASE",
            "recent": "updated_at DESC, title COLLATE NOCASE",
            "quality": """CASE
                WHEN quality LIKE '4K%' THEN 5
                WHEN quality LIKE '1080p%' THEN 4
                WHEN quality LIKE '720p%' THEN 3
                WHEN quality LIKE '576p%' THEN 2
                WHEN quality LIKE 'SD%' THEN 1 ELSE 0 END DESC,
                title COLLATE NOCASE""",
        }
        values.extend((max(1, min(limit, 500)), max(0, offset)))
        with self.connection() as db:
            rows = db.execute(
                f"""SELECT * FROM plex_library {clause}
                    ORDER BY {orders.get(sort, orders["title"])} LIMIT ? OFFSET ?""",
                values,
            ).fetchall()
        return [self._decode_library_row(row, detailed=False) for row in rows]

    def plex_library_stats(
        self, query: str = "", media_type: str = "", quality: str = "", status: str = ""
    ) -> dict:
        clause, values = self._library_where(query, media_type, quality, status)
        with self.connection() as db:
            filtered = db.execute(
                f"SELECT COUNT(*) FROM plex_library {clause}", values
            ).fetchone()[0]
            row = db.execute(
                """SELECT
                    COUNT(*) AS total,
                    SUM(media_type='movie') AS movies,
                    SUM(media_type='show') AS shows,
                    SUM(quality LIKE '4K%') AS four_k,
                    SUM(quality LIKE '1080p%') AS full_hd,
                    SUM(upgrade_available=1) AS upgrades,
                    SUM(quality='Quality unavailable' OR quality LIKE 'Unknown%') AS unknown
                   FROM plex_library"""
            ).fetchone()
        result = {key: int(row[key] or 0) for key in row.keys()}
        result["filtered"] = int(filtered)
        return result

    def get_plex_library_item(self, item_id: int) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM plex_library WHERE id=?", (item_id,)
            ).fetchone()
        return self._decode_library_row(row) if row else None

    def plex_repair_inventory(self) -> list[dict]:
        """Return detailed Plex rows for local path availability checks."""
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM plex_library ORDER BY id"
            ).fetchall()
        return [self._decode_library_row(row) for row in rows]

    def plex_section_ids(self, media_type: str = "") -> list[str]:
        with self.connection() as db:
            if media_type in {"movie", "show"}:
                rows = db.execute(
                    """SELECT DISTINCT section_id FROM plex_library
                       WHERE media_type=? ORDER BY section_id""",
                    (media_type,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT DISTINCT section_id FROM plex_library ORDER BY section_id"
                ).fetchall()
        return [str(row["section_id"]) for row in rows]

    def queue_library_replacement(
        self,
        item: dict,
        scope: str,
        season_number: int | None = None,
        episode_number: int | None = None,
        profile: str = "best",
        minimum_retry_seconds: int = 0,
        detail_override: str = "",
    ) -> tuple[dict, bool]:
        """Queue a safe, scoped replacement search for an existing Plex item."""
        if scope not in {"movie", "series", "season", "episode"}:
            raise ValueError("Choose movie, series, season, or episode replacement")
        if item.get("media_type") == "movie" and scope != "movie":
            raise ValueError("Movies only support whole-title replacement")
        if item.get("media_type") == "show" and scope == "movie":
            raise ValueError("Series replacement needs a series, season, or episode scope")
        if scope in {"season", "episode"} and season_number is None:
            raise ValueError("Choose a season")
        if scope == "episode" and episode_number is None:
            raise ValueError("Choose an episode")
        if profile not in {"best", "1080p", "4k"}:
            raise ValueError("Choose a supported quality target")
        selected_season = None
        if scope in {"season", "episode"}:
            selected_season = next(
                (
                    season for season in item.get("seasons") or []
                    if int(season.get("number", -1)) == int(season_number)
                ),
                None,
            )
            if selected_season is None:
                raise ValueError("That season is not in the current Plex inventory")
        if scope == "episode" and not any(
            int(episode.get("episode_number", -1)) == int(episode_number)
            for episode in selected_season.get("episodes") or []
        ):
            raise ValueError("That episode is not in the current Plex inventory")

        tmdb_id = item.get("tmdb_id")
        imdb_id = item.get("imdb_id") or ""
        media_type = item["media_type"]
        identity = f"tmdb:{media_type}:{tmdb_id}" if tmdb_id else f"imdb:{media_type}:{imdb_id}"
        scope_key = scope
        if season_number is not None:
            scope_key += f":s{int(season_number):02d}"
        if episode_number is not None:
            scope_key += f":e{int(episode_number):02d}"
        media_key = f"{identity}:replace:{scope_key}"
        source_ref = json.dumps({
            "scope": scope,
            "season_number": season_number,
            "episode_number": episode_number,
            "plex_rating_key": item.get("plex_rating_key") or "",
        }, separators=(",", ":"))
        if detail_override:
            detail = detail_override
        elif scope == "movie":
            detail = "Finding a replacement movie stream"
        elif scope == "series":
            detail = "Finding replacement streams for the full series"
        elif scope == "season":
            detail = f"Finding replacement streams for Season {season_number}"
        else:
            detail = (
                f"Finding a replacement stream for "
                f"S{int(season_number):02d}E{int(episode_number):02d}"
            )
        now = utc_now()
        active_states = {"queued", "searching", "library_pending"}
        with self._lock, self.connection() as db:
            existing = db.execute(
                "SELECT * FROM requests WHERE media_key=? ORDER BY id DESC LIMIT 1",
                (media_key,),
            ).fetchone()
            if existing and existing["status"] in active_states:
                return dict(existing), False
            if existing:
                if minimum_retry_seconds > 0:
                    try:
                        updated = datetime.fromisoformat(existing["updated_at"])
                        age = (datetime.now(timezone.utc) - updated).total_seconds()
                    except (TypeError, ValueError):
                        age = minimum_retry_seconds
                    if age < minimum_retry_seconds:
                        return dict(existing), False
                db.execute(
                    """UPDATE requests
                       SET status='queued', status_detail=?, profile=?, source_ref=?,
                           updated_at=?
                       WHERE id=?""",
                    (detail, profile, source_ref, now, existing["id"]),
                )
                request_id = existing["id"]
            else:
                cursor = db.execute(
                    """INSERT INTO requests
                       (media_key, media_type, title, year, tmdb_id, imdb_id,
                        poster_path, overview, source, source_ref, status_detail,
                        profile, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, '', 'library-replace', ?, ?,
                               ?, ?, ?)""",
                    (
                        media_key, media_type, item.get("title") or "Unknown",
                        item.get("year"), tmdb_id, imdb_id, item.get("thumb") or "",
                        source_ref, detail, profile, now, now,
                    ),
                )
                request_id = cursor.lastrowid
            db.execute(
                """INSERT INTO request_events(request_id, state, detail, created_at)
                   VALUES (?, 'queued', ?, ?)""",
                (request_id, detail, now),
            )
            row = db.execute(
                "SELECT * FROM requests WHERE id=?", (request_id,)
            ).fetchone()
        return dict(row), True

    def match_plex_library(self, item: dict) -> dict | None:
        media_type = "show" if item.get("media_type") in ("tv", "show") else "movie"
        tmdb_id = item.get("tmdb_id") or item.get("id")
        imdb_id = item.get("imdb_id") or ""
        with self.connection() as db:
            row = None
            if tmdb_id:
                row = db.execute(
                    """SELECT * FROM plex_library
                       WHERE media_type=? AND tmdb_id=? LIMIT 1""",
                    (media_type, tmdb_id),
                ).fetchone()
            if row is None and imdb_id:
                row = db.execute(
                    "SELECT * FROM plex_library WHERE imdb_id=? LIMIT 1", (imdb_id,)
                ).fetchone()
            if row is None and item.get("title"):
                row = db.execute(
                    """SELECT * FROM plex_library
                       WHERE media_type=? AND LOWER(title)=LOWER(?)
                         AND (year=? OR ? IS NULL OR year IS NULL)
                       ORDER BY year IS NULL LIMIT 1""",
                    (media_type, item["title"], item.get("year"), item.get("year")),
                ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["versions"] = json.loads(result.pop("versions_json") or "[]")
        result["seasons"] = json.loads(result.pop("seasons_json") or "[]")
        result["upgrade_available"] = bool(result["upgrade_available"])
        return result

    def next_queued(self) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM requests WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        return dict(row) if row else None

    def transition(self, request_id: int, state: str, detail: str = ""):
        now = utc_now()
        with self._lock, self.connection() as db:
            db.execute(
                "UPDATE requests SET status=?, status_detail=?, updated_at=? WHERE id=?",
                (state, detail, now, request_id),
            )
            db.execute(
                "INSERT INTO request_events(request_id, state, detail, created_at) VALUES (?, ?, ?, ?)",
                (request_id, state, detail, now),
            )

    def retry_failed_request(
        self,
        request_id: int,
        source: str,
        source_ref: str,
        profile: str,
    ) -> tuple[dict, bool]:
        """Requeue an explicitly retried failure without disturbing other sources.

        Scheduled imports continue to deduplicate terminal failures. This method
        is reserved for an explicit user-triggered sync, so a failed acquisition
        cannot be requeued automatically every polling interval.
        """
        now = utc_now()
        detail = f"Retry requested from {source}"
        with self._lock, self.connection() as db:
            existing = db.execute(
                "SELECT * FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if (
                existing is None
                or existing["status"] != "needs_attention"
                or existing["source"] != source
                or existing["source_ref"] != source_ref
            ):
                return (dict(existing), False) if existing is not None else ({}, False)
            db.execute(
                """UPDATE requests
                   SET status='queued', status_detail=?, profile=?, updated_at=?
                   WHERE id=?""",
                (detail, profile, now, request_id),
            )
            db.execute(
                """INSERT INTO request_events(request_id, state, detail, created_at)
                   VALUES (?, 'queued', ?, ?)""",
                (request_id, detail, now),
            )
            row = db.execute(
                "SELECT * FROM requests WHERE id=?", (request_id,)
            ).fetchone()
        return dict(row), True

    def events(self, request_id: int) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT state, detail, created_at FROM request_events WHERE request_id=? ORDER BY id",
                (request_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_list_source(self, data: dict) -> dict:
        now = utc_now()
        kind = str(data.get("kind", "mdblist")).strip().lower()
        url = str(data["url"]).strip().rstrip("/")
        max_supported = 70000 if kind == "mdblist" else 1000
        max_items = max(1, min(int(data.get("max_items", 100)), max_supported))
        with self._lock, self.connection() as db:
            existing = db.execute(
                """SELECT * FROM list_sources
                   WHERE kind=? AND (url=? OR rtrim(url, '/')=?)
                   ORDER BY id LIMIT 1""",
                (kind, url, url),
            ).fetchone()
            if existing is not None:
                db.execute(
                    """UPDATE list_sources
                       SET name=?, url=?, enabled=?, mode=?, profile=?, max_items=?
                       WHERE id=?""",
                    (
                        data["name"], url, int(data.get("enabled", True)),
                        data.get("mode", "import_only"), data.get("profile", "best"),
                        max_items, existing["id"],
                    ),
                )
                row = db.execute(
                    "SELECT * FROM list_sources WHERE id=?", (existing["id"],)
                ).fetchone()
                result = dict(row)
                result["created"] = False
                return result
            cursor = db.execute(
                """INSERT INTO list_sources(name, kind, url, enabled, mode, profile, max_items, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data["name"], kind, url,
                    int(data.get("enabled", True)), data.get("mode", "import_only"),
                    data.get("profile", "best"), max_items, now,
                ),
            )
            row = db.execute("SELECT * FROM list_sources WHERE id=?", (cursor.lastrowid,)).fetchone()
        result = dict(row)
        result["created"] = True
        return result

    def list_sources(self) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM list_sources ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def get_list_source(self, source_id: int) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM list_sources WHERE id=?", (source_id,)).fetchone()
        return dict(row) if row else None

    def complete_list_sync(self, source_id: int, error: str = ""):
        with self._lock, self.connection() as db:
            db.execute(
                "UPDATE list_sources SET last_sync_at=?, last_error=? WHERE id=?",
                (utc_now(), error, source_id),
            )

    def dashboard(self) -> dict:
        with self.connection() as db:
            counts = {
                row["status"]: row["count"]
                for row in db.execute("SELECT status, COUNT(*) AS count FROM requests GROUP BY status")
            }
            sources = db.execute("SELECT COUNT(*) FROM list_sources WHERE enabled=1").fetchone()[0]
        return {"requests": counts, "active_lists": sources}

    def export_worker_request(self, request: dict, path: str):
        """Atomically expose one request to the acquisition worker."""
        payload = {key: request.get(key) for key in (
            "id", "media_type", "title", "year", "tmdb_id", "imdb_id", "profile",
            "plex_guid", "source", "source_ref",
        )}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(temporary, path)
