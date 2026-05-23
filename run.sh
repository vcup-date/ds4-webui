#!/bin/sh
# ds4-web launcher
#   - creates a venv if missing
#   - installs requirements if missing
#   - starts the server on 127.0.0.1:8810 (override with DS4_WEB_PORT)
#   - opens the default browser at it (suppress with DS4_WEB_NO_OPEN=1)

set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$DIR"

PORT="${DS4_WEB_PORT:-8810}"
HOST="${DS4_WEB_HOST:-127.0.0.1}"

if [ ! -d .venv ]; then
    echo "creating venv..."
    python3 -m venv .venv
fi

# Always make sure deps are present; this is idempotent.
. .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

if [ -z "$DS4_WEB_NO_OPEN" ] && command -v open >/dev/null 2>&1; then
    (sleep 1.0 && open "http://${HOST}:${PORT}") &
fi

exec uvicorn server:app --host "$HOST" --port "$PORT" --log-level info
