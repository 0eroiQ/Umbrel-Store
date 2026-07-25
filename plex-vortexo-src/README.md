# Plex Vortexo companion

This image supplies the private Plex Web enhancement used by the
`zeroq-plex` Umbrel package. It does not modify Plex Media Server or Plex's
hashed web bundles.

## Roles

- `VORTEXO_ROLE=gateway` starts the owner-authenticated API on loopback and an
  unprivileged Nginx gateway on port `32500`. Nginx proxies Plex on `32400` and
  injects the local JavaScript and CSS only into `/web/index.html`.
- `VORTEXO_ROLE=mount` starts the privileged rclone supervisor on loopback port
  `32501`. The gateway API uses loopback port `32502`. The supervisor mounts
  TorBox WebDAV read-only at
  `/downloads/.vortexo-source` and refuses any mount it did not create.

Settings, signed playback URLs, resume progress, and library jobs live in the
private SQLite data directory. Browser responses never contain the TorBox key,
Plex token, magnet, info hash, manifest request headers, or raw signed URL.

## API

The owner session is established by `PUT /vortexo/api/session`. The gateway
accepts the Plex Web token only when Plex confirms it belongs to the same
account as the owner token in `Preferences.xml`, then sets an HTTP-only cookie.

- `GET /vortexo/api/status`
- `GET|PUT /vortexo/api/settings`
- `GET /vortexo/api/watchlist`
- `POST /vortexo/api/watchlist/sync`
- `GET /vortexo/api/discover/{id}`
- `GET /vortexo/api/discover/{id}/episodes`
- `POST /vortexo/api/streams`
- `POST /vortexo/api/play`
- `POST /vortexo/api/progress`
- `POST /vortexo/api/library-jobs`
- `GET /vortexo/api/library-jobs/{id}`

## Native Plex clients

The optional Plex Watchlist coordinator runs entirely on the server. It reads
the owner's universal Watchlist at a configurable interval (one minute by
default), skips titles already present in a local Plex library or an active
Vortexo job, and selects a cached addable release using the saved Best, 4K, or
1080p profile and maximum-size limit. Automatic imports reject mismatched
titles and years, trailers, samples, obvious pre-release captures, and
undersized quality claims. When enabled, automatic upgrades add a safer
higher-quality media version without deleting the current version. Movies are
acquired directly. A newly
requested TV show safely starts with its first regular episode rather than
silently acquiring an entire series.

Every selected release uses the same persistent library-job state machine as
the manual Add to Plex action. Completed media is ordinary Plex library media,
so it appears in native Plex clients without modifying those clients. Removing
a title from the Plex Watchlist never deletes an existing file or Plex item.

The recoverable library audit reports every managed symlink and moves only
high-confidence bad links into a timestamped `.quarantine` directory with a
JSONL restoration manifest:

```sh
python3 -m vortexo.audit \
  --source-root /downloads/.vortexo-source \
  --movies-root /downloads/vortexo/Movies \
  --tv-root /downloads/vortexo/TV \
  --library-root /downloads/vortexo \
  --quarantine
```

## Verification

Run:

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q vortexo
node --check web/plex-vortexo.js
sh -n entrypoint.sh
```

The image workflow publishes `main` and commit-SHA tags. The Umbrel updater
resolves the published `main` tag to an immutable digest before release.

## Live handoff

Do not overlap this mount with Orbit. Stop Orbit's mount role first, prove
`.vortexo-source` is no longer a mountpoint and is empty, and only then start
Plex Vortexo. The pre-start hook and mount supervisor repeat these checks and
will not detach a foreign mount.

Keep the previous Plex package and the stopped Orbit installation available
until a cached playback and an Add to Plex job both pass through Plex
confirmation. Port `32400` remains the unchanged native-client fallback.
