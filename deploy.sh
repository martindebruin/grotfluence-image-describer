#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="dedibox1"
REMOTE_DIR="/home/martin/dockers/auto-oat.martindebruin.com"

echo "→ Syncing files..."
rsync -av --delete \
  --exclude='.env' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='deploy.sh' \
  --exclude='data/' \
  ./ "${REMOTE_HOST}:${REMOTE_DIR}/"

echo "→ Rebuilding and restarting container..."
ssh "${REMOTE_HOST}" "cd ${REMOTE_DIR} && docker compose up -d --build"

echo "✓ Done. Tailing logs for 10s..."
ssh "${REMOTE_HOST}" "cd ${REMOTE_DIR} && docker compose logs --tail 20 -f" &
LOG_PID=$!
sleep 10
kill $LOG_PID 2>/dev/null
