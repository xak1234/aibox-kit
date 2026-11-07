#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
docker info >/dev/null
docker compose up --build -d
printf '\nterminal.. is starting. Your pairing token:\n'
for attempt in {1..30}; do
  if docker compose exec -T terminal cat /data/token 2>/dev/null; then
    printf '\n\nOpen http://localhost:8765 and paste this token into Runtime settings.\n'
    exit 0
  fi
  sleep 1
done
printf '\nStartup is taking longer than expected. Run: docker compose logs terminal\n'
exit 1
