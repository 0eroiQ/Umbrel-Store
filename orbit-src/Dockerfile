FROM ghcr.io/zeroq-bit/plex_debrid:main@sha256:31a73be14c251c4773a34ca6d17d91e85a5fdc193823ad76a165617c87ef841c

COPY orbit /app/orbit
COPY entrypoint.sh /app/orbit-entrypoint.sh

ENV PYTHONPATH=/app \
    ORBIT_DATA_DIR=/data \
    ORBIT_PORT=8080 \
    PD_ROOT=/app/plex_debrid \
    PD_CONFIG_DIR=/config \
    PD_LOG_DIR=/logs \
    ORBIT_ACQUIRE_COMMAND="python3 -m orbit.acquire_legacy"

EXPOSE 8080
ENTRYPOINT ["/bin/sh", "/app/orbit-entrypoint.sh"]
