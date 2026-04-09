#!/usr/bin/env bash
# Install livekit.service as a systemd unit so the stack
# starts automatically on boot and can be managed with systemctl.
#
# Run from /opt/livekit after git clone:
#   bash scripts/install-service.sh

set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_FILE="$INSTALL_DIR/livekit.service"
SYSTEMD_DEST="/etc/systemd/system/livekit.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "✗ Run as root: sudo bash scripts/install-service.sh"
  exit 1
fi

# Patch WorkingDirectory in service file to match actual install path
sed "s|WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|" \
  "$SERVICE_FILE" > "$SYSTEMD_DEST"

systemctl daemon-reload
systemctl enable livekit
systemctl start  livekit

echo ""
echo "✓ livekit.service installed and started."
echo ""
echo "  systemctl status  livekit   # check status"
echo "  systemctl restart livekit   # restart all containers"
echo "  systemctl stop    livekit   # stop all containers"
echo "  systemctl start   livekit   # start all containers"
echo "  journalctl -u livekit -f    # follow logs"
