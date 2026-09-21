#!/usr/bin/env bash
# Restart the bridge. docker-compose.yml present -> that's the deployment, full stop;
# only falls back to systemd if this checkout has no docker-compose.yml at all.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

if [ -f "$ROOT_DIR/docker-compose.yml" ] && command -v docker >/dev/null 2>&1; then
  DC=(docker compose -f "$ROOT_DIR/docker-compose.yml")
  if ! "${DC[@]}" ps >/dev/null 2>&1; then
    DC=(sudo "${DC[@]}")
  fi
  # --force-recreate matters here: compose otherwise skips recreating a container
  # whose image/config look unchanged, so a plain .env edit (e.g. via set-secret.sh)
  # would silently keep running with the OLD environment. Pull first so a redeploy
  # always picks up whatever CI most recently published to ghcr.io.
  "${DC[@]}" pull
  "${DC[@]}" up -d --force-recreate
  echo "Restarted via docker compose"
elif systemctl --user list-unit-files instabridge.service >/dev/null 2>&1; then
  systemctl --user restart instabridge.service
  echo "Restarted via systemd"
else
  echo "No docker-compose.yml and no instabridge.service found — nothing to restart." >&2
  exit 1
fi
