#!/bin/sh
set -eu

mkdir -p "${ORBIT_DATA_DIR:-/data}" "${PD_CONFIG_DIR:-/config}" "${PD_LOG_DIR:-/logs}"

# Patch plex_debrid: remove the "library seems empty" safety check that blocks
# downloads on fresh installs. This check prevents the first download from ever
# running when the Plex library has no content yet (chicken-and-egg problem).
PLEX_PY="${PD_ROOT:-/app/plex_debrid}/content/services/plex.py"
if [ -f "$PLEX_PY" ]; then
  sed -i 's/Your library seems empty. To prevent/# patched: allow empty library for first download/' "$PLEX_PY" 2>/dev/null || true
  sed -i 's/if len(library) == 0:/if False:  # patched: skip empty library check/' "$PLEX_PY" 2>/dev/null || true
fi

# Orbit 0.5.3 briefly generated one physical manifest per title. The virtual
# database-backed endpoint supersedes that slow cache completely.
rm -rf "${ORBIT_DATA_DIR:-/data}/manifests"

if [ "${ORBIT_ROLE:-server}" = "automation" ]; then
  exec python3 "${PD_ROOT:-/app/plex_debrid}/main.py" \
    --config-dir "${PD_CONFIG_DIR:-/config}" -service
fi

exec python3 -m orbit
