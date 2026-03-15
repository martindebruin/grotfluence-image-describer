#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="/opt/dockers/auto-oat.martindebruin.com/.env"
WEB_PASSWORD=$(grep '^WEB_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)

curl -s -u "martin:${WEB_PASSWORD}" -X POST https://auto-oat.martindebruin.com/admin/backfill-vision
echo ""
echo "Backfill started — following logs (Ctrl+C to stop watching):"
docker logs -f $(docker ps -q --filter name=image-describer) 2>&1 | grep --line-buffered backfill
