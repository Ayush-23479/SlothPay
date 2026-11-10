#!/bin/sh
# Start script for Render (free plan has no pre-deploy step, so migrations run on every start;
# they are safe to repeat). Kept as a file so Render does not have to parse a quoted
# one-line command. Render sets $PORT (default 10000).
set -e

python -m alembic upgrade head

exec uvicorn backend.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-10000}" \
  --proxy-headers \
  --forwarded-allow-ips='*'