#!/usr/bin/env bash
# Post one clip. Schedule it with cron (see README) on a computer with a normal home connection.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .venv/bin/activate ] && source .venv/bin/activate
[ -f .env ] && set -a && source .env && set +a
mkdir -p logs
{
  echo "=== $(date -u +%FT%TZ)"
  python -m pip install --quiet --upgrade yt-dlp   # YouTube changes often; stale yt-dlp is the #1 cause of failures
  python -m motivate run "$@"
} >> logs/run.log 2>&1
