# Orbit

Orbit is a unified media automation app for Umbrel. It combines manual media
discovery, Plex Watchlist, MDBList and Trakt imports, debrid acquisition, a remote-only mount,
library organization, and Plex refresh into one visible pipeline.

## Features

- TMDb-powered manual movie and TV search
- Existing Plex library inventory with actual resolution, HDR/Dolby Vision,
  codecs, containers, file sizes, and multi-version visibility
- Series, season, and episode drill-down with video, audio, and subtitle track
  inspection
- Scoped replacement searches for a movie, full series, season, or individual
  episode, with Best, 1080p, and 4K targets
- TMDb/IMDb matching that marks Discover results already in Plex
- Explicit 1080p upgrades for lower-quality Plex items
- Optional series completion that fills missing aired episodes for shows already
  present in Plex while ignoring future episodes
- Persistent SQLite request queue and event history
- Plex Watchlist, MDBList, and Trakt imports that skip titles already in Plex;
  MDBList cursor pagination supports collections up to 70,000 titles
- Existing TorBox/Real-Debrid acquisition engine reused as an isolated worker
- Existing WebDAV/zurg FUSE mount reused as a separate least-privilege service
- One authenticated Orbit dashboard and one Umbrel app icon
- Tabbed feature settings for Discovery, Debrid, Plex, Scrapers, and Series
- Configurable Torrentio, Prowlarr, Jackett, Orionoid, Nyaa, and 1337x scrapers
- Remote source media with persistent VFS caching forced off
- Exact-folder Plex scans, automatic/periodic Plex scan suppression, and
  periodic TorBox symlink repair with bounded replacement requests
- One credential-free virtual JSON record per title, containing the Plex
  identity, current quality, media path, and symlink target for instant access

## Architecture

The Umbrel app contains two internal services:

- `server`: Orbit dashboard, API, database, importer, and manual request worker
- `mount`: rclone WebDAV or zurg FUSE mount supervisor

The split is intentional: only the mount receives FUSE/SYS_ADMIN privileges.
Users interact with Orbit as one application.

## Local development

Orbit's core has no third-party Python dependencies:

```sh
ORBIT_DATA_DIR=./data ORBIT_ACQUIRE_COMMAND='' python3 -m orbit
```

Open `http://localhost:8080`. Manual requests remain queued when the acquisition
command is empty, which is useful for UI and API development without live debrid
credentials.

Run tests with:

```sh
python3 -m unittest discover -s tests -v
```

## Safety

Automatic lists use import-only behavior. Removing a title from Plex Watchlist,
MDBList, or Trakt never deletes Plex media, debrid torrents, or library links, and items
already present in Plex are skipped. Plex refresh failures preserve the last
successful inventory. Series completion is opt-in and has a configurable daily
limit; it only monitors shows with at least one episode already in Plex.
Replacement searches keep the current Plex stream available while the new
release is acquired, so a failed search does not remove playable media.
Persistent source-media caching remains disabled; only Orbit state, logs, and
library symlinks are stored locally. Each database-backed media record is
available from `/api/library/{id}/manifest`; direct provider URLs are
deliberately resolved live instead of persisted because they expire and may
contain credentials. Plex continues to play the stable filesystem path and
receives only exact-folder partial scans when that path changes.
