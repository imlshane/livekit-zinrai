#!/usr/bin/env bash
# Install srs.service as a systemd unit so the stack starts automatically
# on boot and can be managed with systemctl.
#
# Run as root from your deploy directory (e.g. /opt/livekit):
#   bash scripts/install-service.sh

set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_FILE="$INSTALL_DIR/srs.service"
SYSTEMD_DEST="/etc/systemd/system/srs-stream.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "✗ Run as root: sudo bash scripts/install-service.sh"
  exit 1
fi

# Patch WorkingDirectory in service file to match actual install path
sed "s|WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|" \
  "$SERVICE_FILE" > "$SYSTEMD_DEST"

systemctl daemon-reload
systemctl enable srs-stream
systemctl start  srs-stream

echo ""
echo "✓ srs-stream.service installed and started."
echo ""
echo "  systemctl status  srs-stream   # check status"
echo "  systemctl restart srs-stream   # restart all containers"
echo "  systemctl stop    srs-stream   # stop all containers"
echo "  systemctl start   srs-stream   # start all containers"
echo "  journalctl -u srs-stream -f    # follow logs"
