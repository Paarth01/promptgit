#!/bin/sh
# Runs as the docker-compose 'seed' service. Waits for the API to be up,
# bootstraps a throwaway admin API key directly against the DB (breaking
# the chicken-and-egg problem of needing a key to issue a key), then runs
# the Phase 5 demo seed script through the real authenticated API surface —
# so `docker-compose up` alone produces a running (and, once the hold period
# clears, auto-promoted/completed) experiment with no manual steps.
set -e

echo "[seed] waiting for API health check..."
python3 - <<'PYEOF'
import time
import urllib.request

for attempt in range(60):
    try:
        urllib.request.urlopen("http://api:8000/health", timeout=2)
        print("[seed] API is up")
        break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit("[seed] API never became healthy after 60s")
PYEOF

echo "[seed] bootstrapping admin API key..."
KEY_OUTPUT=$(python3 /app/scripts/create_api_key.py --name "seed-bootstrap-$(date +%s)" --role admin --created-by "docker-compose-seed")
echo "$KEY_OUTPUT"
API_KEY=$(echo "$KEY_OUTPUT" | grep "API key (shown once" | sed 's/.*: //')

if [ -z "$API_KEY" ]; then
    echo "[seed] failed to parse bootstrapped API key, aborting"
    exit 1
fi

echo "[seed] running demo seed script..."
python3 /app/scripts/seed_demo.py --api http://api:8000 --api-key "$API_KEY" --target-sample-size 150

echo "[seed] done. The experiment is running; it will auto-promote once its"
echo "[seed] hold period clears (see HOLD_PERIOD_HOURS) and the metrics"
echo "[seed] worker's next processing round runs."
