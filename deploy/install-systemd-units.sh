#!/usr/bin/env bash

set -Eeuo pipefail

readonly DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SYSTEMD_DIR="/etc/systemd/system"

for unit in farmacentral-backend.service farmacentral-inventory-sync.service \
    farmacentral-inventory-sync.timer farmacentral-worker@.service \
    farmacentral-workers.target; do
    sudo /usr/bin/install -m 0644 "$DEPLOY_DIR/$unit" "$SYSTEMD_DIR/$unit"
done
sudo /usr/bin/systemctl daemon-reload

# A target reload does not necessarily stop instances removed from Wants.
# Stop the legacy names once so they cannot compete for the same advisory locks.
for legacy in reconcile_billing reconcile_trades; do
    sudo /usr/bin/systemctl stop "farmacentral-worker@${legacy}.service" || true
done

printf 'Farmacentral systemd units installed. Restart services to activate them.\n'
