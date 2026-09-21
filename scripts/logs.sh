#!/usr/bin/env bash
# Tail the bridge's logs. docker-compose.yml present -> that's the deployment, full
# stop; only falls back to systemd if this checkout has no docker-compose.yml at all.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

if [ -f "$ROOT_DIR/docker-compose.yml" ] && command -v docker >/dev/null 2>&1; then
  DC=(docker compose -f "$ROOT_DIR/docker-compose.yml")
  if ! "${DC[@]}" ps >/dev/null 2>&1; then
    DC=(sudo "${DC[@]}")
  fi
  "${DC[@]}" logs -f --tail=100 instabridge
elif systemctl --user list-unit-files instabridge.service >/dev/null 2>&1; then
  journalctl --user -u instabridge.service -f -n 100
else
  echo "No docker-compose.yml and no instabridge.service found." >&2
  exit 1
fi
