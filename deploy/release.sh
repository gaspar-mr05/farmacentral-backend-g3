#!/usr/bin/env bash

set -Eeuo pipefail

readonly APP_DIR="/home/integracion/farmacentral-backend"

cd "$APP_DIR"
export APP_ENV_FILE="$APP_DIR/.env"

restart_service() {
    local unit="$1"
    if ! sudo -n /usr/bin/systemctl restart "$unit"; then
        printf 'Deployment failed: could not restart %s.\n' "$unit" >&2
        printf 'Check the service error above and the NOPASSWD rule for:\n' >&2
        printf '  /usr/bin/systemctl restart %s\n' "$unit" >&2
        printf 'Check the exact NOPASSWD sudoers rule; sudo -l alone is insufficient.\n' >&2
        return 1
    fi
}

require_active() {
    local unit="$1"
    if ! /usr/bin/systemctl is-active --quiet "$unit"; then
        printf 'Deployment failed: %s is not active after restart.\n' "$unit" >&2
        printf 'Check: systemctl status %s --no-pager\n' "$unit" >&2
        printf 'If the unit is missing, install it with deploy/install-systemd-units.sh.\n' >&2
        return 1
    fi
}

.venv/bin/python -m pip install --disable-pip-version-check .
.venv/bin/alembic upgrade head

restart_service farmacentral-backend.service
restart_service farmacentral-workers.target
require_active farmacentral-worker@process_billing.service
require_active farmacentral-worker@process_trades.service

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
