#!/usr/bin/env bash

set -Eeuo pipefail

readonly DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SYSTEMD_DIR="/etc/systemd/system"

sudo /usr/bin/install -m 0644 \
    "$DEPLOY_DIR/farmacentral-worker@.service" \
    "$SYSTEMD_DIR/farmacentral-worker@.service"
sudo /usr/bin/install -m 0644 \
    "$DEPLOY_DIR/farmacentral-workers.target" \
    "$SYSTEMD_DIR/farmacentral-workers.target"
sudo /usr/bin/systemctl daemon-reload

# A target reload does not necessarily stop instances removed from Wants.
# Stop the legacy names once so they cannot compete for the same advisory locks.
for legacy in reconcile_billing reconcile_trades; do
    sudo /usr/bin/systemctl stop "farmacentral-worker@${legacy}.service" || true
done

printf 'Farmacentral worker units installed. Restart the target to activate them.\n'
