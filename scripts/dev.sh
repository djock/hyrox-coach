#!/usr/bin/env bash
# Local dev launcher.
#
# secrets/secrets.env is stored RAW because docker compose reads it with
# `format: raw`; quoting it there would put literal quotes inside the container.
# But bcrypt hashes contain `$`, so `source`-ing the file would expand them away
# and every login would fail with a confusing 401. `read` does no expansion, so
# the values arrive exactly as written -- same bytes the container sees.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f secrets/secrets.env ]]; then
  while IFS='=' read -r key value; do
    [[ -z "$key" || "$key" == \#* ]] && continue
    export "$key=$value"
  done < secrets/secrets.env
fi

exec .venv/bin/uvicorn --factory hyrox.app:create_app --reload --port "${PORT:-8099}" "$@"
