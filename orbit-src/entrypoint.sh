#!/bin/sh
set -eu

mkdir -p "${ORBIT_DATA_DIR:-/data}" "${PD_CONFIG_DIR:-/config}" "${PD_LOG_DIR:-/logs}"

# Patch plex_debrid: remove the "library seems empty" safety check that blocks
# downloads on fresh installs. The check is in plex.py's library.__new__ as
# "if len(list_) == 0:" — we disable it so the first download can seed the library.
PLEX_PY="${PD_ROOT:-/app/plex_debrid}/content/services/plex.py"
if [ -f "$PLEX_PY" ] && ! grep -q "orbit-patched" "$PLEX_PY" 2>/dev/null; then
  python3 -c "
import re
path = '$PLEX_PY'
with open(path) as f:
    code = f.read()
# The actual check uses 'list_' not 'library': if len(list_) == 0:
code = re.sub(r'if\s+len\s*\(\s*list_\s*\)\s*==\s*0\s*:', 'if False:  # orbit-patched: allow empty library', code)
code = code.replace('Your library seems empty. To prevent', 'orbit-patched: empty library allowed,')
with open(path, 'w') as f:
    f.write(code)
" 2>/dev/null || true
fi

# Orbit 0.5.3 briefly generated one physical manifest per title. The virtual
# database-backed endpoint supersedes that slow cache completely.
rm -rf "${ORBIT_DATA_DIR:-/data}/manifests"

if [ "${ORBIT_ROLE:-server}" = "automation" ]; then
  exec python3 "${PD_ROOT:-/app/plex_debrid}/main.py" \
    --config-dir "${PD_CONFIG_DIR:-/config}" -service
fi

exec python3 -m orbit
