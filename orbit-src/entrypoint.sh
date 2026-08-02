#!/bin/sh
set -eu

mkdir -p "${ORBIT_DATA_DIR:-/data}" "${PD_CONFIG_DIR:-/config}" "${PD_LOG_DIR:-/logs}"

# Prepare the writable library roots and make the bundled Plex scanner treat
# each configured section independently. Failures are reported but never keep
# Orbit's dashboard from starting.
python3 -m orbit.startup || echo "[orbit] fresh-install preparation failed; starting dashboard" >&2

# Orbit 0.5.3 briefly generated one physical manifest per title. The virtual
# database-backed endpoint supersedes that slow cache completely.
rm -rf "${ORBIT_DATA_DIR:-/data}/manifests"

if [ "${ORBIT_ROLE:-server}" = "automation" ]; then
  exec python3 "${PD_ROOT:-/app/plex_debrid}/main.py" \
    --config-dir "${PD_CONFIG_DIR:-/config}" -service
fi

exec python3 -m orbit
