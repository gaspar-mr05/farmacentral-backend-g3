#!/usr/bin/env bash

set -Eeuo pipefail

readonly APP_DIR="/home/integracion/farmacentral-backend"

cd "$APP_DIR"

.venv/bin/python -m pip install --disable-pip-version-check .
.venv/bin/alembic upgrade head

sudo -n /usr/bin/install -m 644 \
    deploy/farmacentral-backend.service \
    /etc/systemd/system/farmacentral-backend.service
sudo -n /usr/bin/install -m 644 \
    deploy/farmacentral-inventory-sync.service \
    /etc/systemd/system/farmacentral-inventory-sync.service
sudo -n /usr/bin/install -m 644 \
    deploy/farmacentral-inventory-sync.timer \
    /etc/systemd/system/farmacentral-inventory-sync.timer
sudo -n /usr/bin/systemctl daemon-reload
sudo -n /usr/bin/systemctl restart farmacentral-backend.service
if sudo -n /usr/bin/systemctl is-enabled --quiet \
    farmacentral-inventory-sync.timer; then
    sudo -n /usr/bin/systemctl restart farmacentral-inventory-sync.timer
fi

for attempt in {1..10}; do
    if curl --fail --silent --show-error http://127.0.0.1:8000/api/health; then
        printf '\nDeployment completed successfully.\n'
        exit 0
    fi
    sleep 1
done

echo "Health check failed after deployment." >&2
exit 1
