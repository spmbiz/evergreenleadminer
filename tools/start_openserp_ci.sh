#!/usr/bin/env bash
set -euo pipefail
PORT="${OPENSERP_PORT:-7000}"
IMAGE="${OPENSERP_IMAGE:-karust/openserp:latest}"
docker rm -f hospitality-openserp >/dev/null 2>&1 || true
if ! docker pull --platform linux/amd64 "$IMAGE"; then
  exit 2
fi
if ! docker run -d --name hospitality-openserp --platform linux/amd64 \
  -p "127.0.0.1:${PORT}:7000" "$IMAGE" serve -a 0.0.0.0 -p 7000 >/dev/null; then
  exit 3
fi
for _ in $(seq 1 20); do
  if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo "OPENSERP_READY=http://127.0.0.1:${PORT}"
    exit 0
  fi
  sleep 1
done
docker logs --tail 80 hospitality-openserp >&2 || true
exit 4
