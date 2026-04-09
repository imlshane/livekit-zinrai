#!/usr/bin/env bash
# Local test helper — shows OBS settings and generates a viewer token.
# Run after: docker compose up -d
set -euo pipefail

AUTH="http://localhost/api"
STREAM_KEY="${1:-stream-key-001}"

echo ""
echo "┌─────────────────────────────────────────────────────┐"
echo "│         SRS Local Test — Quick Reference            │"
echo "└─────────────────────────────────────────────────────┘"
echo ""
echo "  Stack status:"
docker compose ps 2>/dev/null || echo "  (run from the repo root)"

echo ""
echo "  OBS Settings:"
echo "  ─────────────"
echo "  Service    : Custom"
echo "  Server     : rtmp://localhost/live"
echo "  Stream Key : $STREAM_KEY"
echo ""
echo "  Generating viewer token for stream key: $STREAM_KEY"
RESPONSE=$(curl -sf "$AUTH/token?stream_key=$STREAM_KEY" 2>/dev/null || echo "")

if [ -z "$RESPONSE" ]; then
    echo "  ✗ Auth service not reachable. Is docker compose up?"
    echo "    Try: docker compose logs auth"
    exit 1
fi

PLAYER_URL=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['player_url'])")
TOKEN=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

echo ""
echo "  Viewer token  : $TOKEN"
echo "  Player URL    : http://localhost$PLAYER_URL"
echo ""
echo "  Active streams:"
curl -sf "$AUTH/streams" 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "  (none)"
echo ""
echo "  Live publishers:"
curl -sf "$AUTH/publishers" 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "  (error)"
echo ""
echo "  SRS stats     : http://localhost/api/srs/v1/summaries"
echo "  SRS streams   : http://localhost/api/srs/v1/streams"
echo ""
