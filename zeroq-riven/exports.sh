export APP_ZEROQ_RIVEN_URL="http://zeroq-riven_server_1:8080"
export APP_ZEROQ_RIVEN_DATA_DIR="${EXPORTS_APP_DIR}/data/riven"
export APP_ZEROQ_RIVEN_MOUNT_DIR="${UMBREL_ROOT}/home/Downloads/riven"
export APP_ZEROQ_RIVEN_PORT="8080"
export APP_ZEROQ_RIVEN_WEBDAV_PORT="8081"
export APP_ZEROQ_RIVEN_FTP_PORT="2121"

primary_local_ipv4="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ { for (i = 1; i <= NF; i++) if ($i == "src") { print $(i+1); exit } }')"

if [[ -z "${primary_local_ipv4}" ]]; then
  local_ipv4s="$(hostname --all-ip-addresses 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+$' || true)"
  primary_local_ipv4="$(printf '%s\n' "${local_ipv4s}" | grep '^192\\.168\\.' | head -n 1)"
fi

if [[ -z "${primary_local_ipv4}" && -n "${local_ipv4s:-}" ]]; then
  primary_local_ipv4="$(printf '%s\n' "${local_ipv4s}" | grep '^10\\.' | head -n 1)"
fi

if [[ -z "${primary_local_ipv4}" && -n "${local_ipv4s:-}" ]]; then
  primary_local_ipv4="$(printf '%s\n' "${local_ipv4s}" | head -n 1)"
fi

if [[ -n "${primary_local_ipv4}" ]]; then
  export APP_ZEROQ_RIVEN_LOCAL_IPV4="${primary_local_ipv4}"
  export APP_ZEROQ_RIVEN_LOCAL_URL="http://${primary_local_ipv4}:${APP_ZEROQ_RIVEN_PORT}"
  export APP_ZEROQ_RIVEN_WEBDAV_LOCAL_URL="http://${primary_local_ipv4}:${APP_ZEROQ_RIVEN_WEBDAV_PORT}"
  export APP_ZEROQ_RIVEN_FTP_LOCAL_URL="ftp://${primary_local_ipv4}:${APP_ZEROQ_RIVEN_FTP_PORT}"
else
  export APP_ZEROQ_RIVEN_LOCAL_IPV4="127.0.0.1"
  export APP_ZEROQ_RIVEN_LOCAL_URL="http://${DEVICE_DOMAIN_NAME}:${APP_ZEROQ_RIVEN_PORT}"
  export APP_ZEROQ_RIVEN_WEBDAV_LOCAL_URL="http://${DEVICE_DOMAIN_NAME}:${APP_ZEROQ_RIVEN_WEBDAV_PORT}"
  export APP_ZEROQ_RIVEN_FTP_LOCAL_URL="ftp://${DEVICE_DOMAIN_NAME}:${APP_ZEROQ_RIVEN_FTP_PORT}"
fi
