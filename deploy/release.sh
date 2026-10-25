#!/usr/bin/env bash

set -Eeuo pipefail

readonly APP_DIR="/home/integracion/farmacentral-backend"

cd "$APP_DIR"

# Fail before migrations if the deployment account lacks either restart grant.
sudo -n -l /usr/bin/systemctl restart farmacentral-backend.service >/dev/null
sudo -n -l /usr/bin/systemctl restart farmacentral-workers.target >/dev/null

.venv/bin/python -m pip install --disable-pip-version-check .
.venv/bin/alembic upgrade head

sudo -n /usr/bin/systemctl restart farmacentral-backend.service
sudo -n /usr/bin/systemctl restart farmacentral-workers.target

for attempt in {1..10}; do
    if curl --fail --silent --show-error --connect-timeout 2 --max-time 6 \
        http://127.0.0.1:8000/api/health/ready; then
        printf '\nDeployment completed successfully.\n'
        exit 0
    fi
    sleep 1
done

echo "Health check failed after deployment." >&2
exit 1
