#!/bin/bash
# Smart SRS restart — waits for zero active streams before restarting.
# Checks every 5 minutes for up to 1 hour. Skips if no quiet window found.

LOG=/var/log/zinrai-srs-restart.log
MAX_WAIT_MIN=60
INTERVAL_MIN=5
attempts=$((MAX_WAIT_MIN / INTERVAL_MIN))

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

log "Smart restart triggered — checking for active streams..."

for i in $(seq 1 $attempts); do
    stream_count=$(curl -sf http://localhost:1985/api/v1/streams/ \
        | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('streams', [])))" 2>/dev/null)

    if [ -z "$stream_count" ]; then
        log "WARNING: Could not reach SRS API — skipping restart (attempt $i/$attempts)"
        exit 1
    fi

    log "Active streams: $stream_count (attempt $i/$attempts)"

    if [ "$stream_count" -eq 0 ]; then
        log "No active streams — restarting srs-origin..."
        docker restart srs-origin >> "$LOG" 2>&1
        sleep 10
        status=$(docker inspect -f '{{.State.Status}}' srs-origin 2>/dev/null)
        log "srs-origin status after restart: $status"
        log "Restart complete."
        exit 0
    fi

    if [ "$i" -lt "$attempts" ]; then
        log "Streams active — waiting ${INTERVAL_MIN}m before next check..."
        sleep $((INTERVAL_MIN * 60))
    fi
done

log "No quiet window found in ${MAX_WAIT_MIN} minutes — skipping restart."
exit 0
