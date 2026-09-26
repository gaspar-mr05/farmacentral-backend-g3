#!/usr/bin/env bash

set -Eeuo pipefail

readonly APP_DIR="/opt/farmacentral-backend"

cd "$APP_DIR"

.venv/bin/python -m pip install --disable-pip-version-check .
.venv/bin/alembic upgrade head
sudo -n /usr/bin/systemctl restart farmacentral-backend.service

for attempt in {1..10}; do
    if curl --fail --silent --show-error http://127.0.0.1:8000/api/health; then
        printf '\nDeployment completed successfully.\n'
        exit 0
    fi
    sleep 1
done

echo "Health check failed after deployment." >&2
exit 1
