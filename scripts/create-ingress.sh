#!/usr/bin/env bash
# Create a LiveKit RTMP ingress via REST API.
# Run this on the server after docker compose up.
#
# Usage:
#   LIVEKIT_API_KEY=xxx LIVEKIT_API_SECRET=yyy ./create-ingress.sh [room-name]
#
# Outputs: ingress_id and stream_key — save these!

set -euo pipefail

ROOM="${1:-test-room}"
API_KEY="${LIVEKIT_API_KEY:?Set LIVEKIT_API_KEY}"
API_SECRET="${LIVEKIT_API_SECRET:?Set LIVEKIT_API_SECRET}"
LIVEKIT_URL="http://127.0.0.1:7880"

# Generate admin JWT (publish + admin grants)
python3 - <<EOF
import hmac, hashlib, base64, json, time, os

def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

key    = "${API_KEY}"
secret = "${API_SECRET}"
header  = {"alg": "HS256", "typ": "JWT"}
payload = {
    "iss": key,
    "sub": "admin",
    "iat": int(time.time()),
    "exp": int(time.time()) + 600,
    "video": {"roomCreate": True, "ingressAdmin": True},
}
h = b64url(json.dumps(header, separators=(',',':')).encode())
p = b64url(json.dumps(payload, separators=(',',':')).encode())
sig = hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
print(f"{h}.{p}.{b64url(sig)}")
EOF
) | read -r JWT

echo "Calling LiveKit ingress API..."
curl -s -X POST "${LIVEKIT_URL}/twirp/livekit.Ingress/CreateIngress" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${JWT}" \
  -d "{
    \"input_type\": 0,
    \"room_name\": \"${ROOM}\",
    \"participant_identity\": \"stream-host\",
    \"participant_name\": \"Stream Host\",
    \"enable_transcoding\": true,
    \"video\": {
      \"preset\": 6
    }
  }" | python3 -m json.tool

echo ""
echo "Note: preset 6 = H264_720P_30FPS_1_LAYER (single layer, no quality switching)"
echo "Save the ingress_id and stream_key from the output above."
echo "OBS RTMP URL: rtmp://YOUR_SERVER_IP/live"
echo "OBS Stream Key: <stream_key from above>"
