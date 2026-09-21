#!/usr/bin/env bash
# Update one key in .env and restart the bridge in one shot.
#
# Usage:
#   ./scripts/set-secret.sh IG_SESSIONID '73998782341:XXXX:27:YYYY'
#   ./scripts/set-secret.sh IG_PASSWORD 'new-password'
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$ROOT_DIR/.env"

if [ $# -ne 2 ]; then
  echo "Usage: $0 KEY VALUE" >&2
  exit 1
fi

KEY="$1"
VALUE="$2"

if [ ! -f "$ENV_FILE" ]; then
  echo "No .env found at $ENV_FILE (copy .env.example first)" >&2
  exit 1
fi

python3 - "$ENV_FILE" "$KEY" "$VALUE" <<'PYEOF'
import re
import sys

path, key, value = sys.argv[1:4]
with open(path) as f:
    content = f.read()

pattern = rf"^{re.escape(key)}=.*$"
replacement = f"{key}={value}"
if re.search(pattern, content, flags=re.M):
    content = re.sub(pattern, lambda _m: replacement, content, flags=re.M)
else:
    if content and not content.endswith("\n"):
        content += "\n"
    content += replacement + "\n"

with open(path, "w") as f:
    f.write(content)
PYEOF

echo "Updated ${KEY} in .env"
exec "$SCRIPT_DIR/restart.sh"
