#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="dedibox3"
REMOTE_DIR="/home/martin/dockers/auto-oat"

echo "→ Syncing files..."
# --delete is on, so anything not excluded and not in the repo gets REMOVED on the
# server. Two categories must never be touched:
#   identity.env  — the Infisical machine identity. Server-only and gitignored;
#                   deleting it means the container can't fetch any secrets.
#   data/         — quality.db / community.db live here.
# And two must never be uploaded:
#   .env, .env~   — local secrets ('.env' alone does NOT match the '.env~' backup)
#   .git/         — full history; no reason for it to sit on the server
rsync -av --delete \
  --exclude='.env' \
  --exclude='.env.*' \
  --exclude='.env~' \
  --exclude='identity.env' \
  --exclude='data/' \
  --exclude='.git/' \
  --exclude='.claude/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='deploy.sh' \
  ./ "${REMOTE_HOST}:${REMOTE_DIR}/"

echo "→ Rebuilding and restarting container..."
ssh "${REMOTE_HOST}" "cd ${REMOTE_DIR} && docker compose up -d --build"

echo "✓ Done. Tailing logs for 10s..."
ssh "${REMOTE_HOST}" "cd ${REMOTE_DIR} && docker compose logs --tail 20 -f" &
LOG_PID=$!
sleep 10
kill $LOG_PID 2>/dev/null
