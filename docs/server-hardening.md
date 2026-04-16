# Server Hardening Checklist

Items to complete on the SRS server before launch.
Work through these top to bottom — they are ordered by impact.

---

## 1. Fix SRS Port Exposure (Security)

**Risk:** SRS HTTP API (port 1985) and HTTP server (port 8080) are currently
bound to `0.0.0.0` in `docker-compose.yml`. Anyone on the internet can call
`http://YOUR_IP:1985/api/v1/clients` to list all active streams, get publisher
details, and force-disconnect publishers.

**File:** `docker-compose.yml`

```yaml
# Before
ports:
  - "1935:1935"
  - "1985:1985"
  - "8080:8080"
  - "8000:8000/udp"

# After
ports:
  - "1935:1935"             # RTMP ingest — must be public
  - "127.0.0.1:1985:1985"   # SRS API — localhost only
  - "127.0.0.1:8080:8080"   # SRS HTTP — localhost only
  - "8000:8000/udp"         # WebRTC UDP — must be public
```

Apply:
```bash
docker compose down && docker compose up -d
```

Verify port 1985 is no longer publicly reachable:
```bash
# From your local machine (not the server) — should time out or refuse
curl --max-time 5 http://YOUR_SERVER_IP:1985/api/v1/summaries
# Expected: connection refused or timeout
```

---

## 2. File Descriptor Limits

**Risk:** Linux default fd limit is 1024 per process. Each WebRTC viewer
consumes multiple UDP sockets and TCP connections. SRS silently stops
accepting new connections when the limit is hit — viewers get a black screen
with no error anywhere in the logs.

**At 300 concurrent viewers you will hit the default limit.**

### System-wide fix

```bash
cat >> /etc/security/limits.conf << 'EOF'
*    soft nofile 65535
*    hard nofile 65535
root soft nofile 65535
root hard nofile 65535
EOF
```

### Docker daemon fix

```bash
mkdir -p /etc/systemd/system/docker.service.d

cat > /etc/systemd/system/docker.service.d/limits.conf << 'EOF'
[Service]
LimitNOFILE=65535
EOF

systemctl daemon-reload
systemctl restart docker
```

### Per-container fix in `docker-compose.yml`

Add to the `srs` service (and optionally `auth`, `nginx`):

```yaml
srs:
  ulimits:
    nofile:
      soft: 65535
      hard: 65535
```

### Verify after applying

```bash
# Check system limit
ulimit -n
# Expected: 65535

# Check inside running SRS container
docker exec srs-origin sh -c "ulimit -n"
# Expected: 65535

# Check how many fds SRS is currently using
ls /proc/$(docker inspect --format='{{.State.Pid}}' srs-origin)/fd | wc -l
```

---

## 3. UDP Buffer Size

**Risk:** Linux default UDP socket receive buffer is ~200 KB. WebRTC sends
many small UDP packets rapidly. When the kernel buffer fills, packets are
silently dropped — the browser jitter buffer underruns and audio/video
stutters or cuts out. This is also what causes the periodic audio dropout
described elsewhere.

```bash
# Check current values
sysctl net.core.rmem_max
sysctl net.core.wmem_max
# Default is usually 212992 (~200 KB) — too small

# Apply larger buffers
cat >> /etc/sysctl.conf << 'EOF'
# WebRTC UDP buffer tuning
net.core.rmem_max=26214400
net.core.wmem_max=26214400
net.core.rmem_default=1048576
net.core.wmem_default=1048576
net.ipv4.udp_rmem_min=8192
EOF

# Apply immediately (no reboot required)
sysctl -p

# Verify
sysctl net.core.rmem_max
# Expected: 26214400
```

---

## 4. Docker Log Rotation

**Risk:** Docker logs grow without limit by default. SRS and the auth service
produce many log lines. A busy day will fill the disk, which freezes all
running containers simultaneously — no warning, everything dies at once.

### Set globally for all containers

```bash
cat > /etc/docker/daemon.json << 'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "5"
  }
}
EOF

systemctl restart docker
```

This limits each container to 5 log files × 50 MB = 250 MB maximum per
container. Oldest log files are rotated out automatically.

### Verify

```bash
# After restarting containers, inspect log config
docker inspect srs-origin | grep -A 10 '"LogConfig"'
# Should show: "max-size": "50m", "max-file": "5"
```

---

## 5. Container Memory Limits

**Risk:** Without memory limits, one misbehaving container consumes all
available RAM. The Linux OOM killer then randomly terminates processes —
often picking a different container than the one causing the problem.
Result: SRS or Nginx dies while the actual culprit keeps running.

**File:** `docker-compose.yml`

```yaml
services:

  srs:
    mem_limit: 3g
    memswap_limit: 3g

  auth:
    mem_limit: 512m
    memswap_limit: 512m

  nginx:
    mem_limit: 256m
    memswap_limit: 256m
```

Memory guidance:
- `srs`: 3 GB — WebRTC sessions per viewer consume ~2–5 MB each.
  300 viewers × 5 MB = 1.5 GB peak. 3 GB gives safe headroom.
- `auth`: 512 MB — Python FastAPI + SQLite + in-memory token store.
  Well within this limit even at 600 active tokens.
- `nginx`: 256 MB — static proxy, no dynamic content. Very light.

Apply:
```bash
docker compose down && docker compose up -d
```

### Verify

```bash
docker stats --no-stream
# MEM USAGE column should now show limits
```

---

## 6. SRS Persistent Log File

**Risk:** SRS currently logs to console only (`srs_log_tank console`).
Container restarts clear all logs. When something breaks at 2 AM you
have no history to diagnose what happened.

**File:** `srs/origin.conf`

```nginx
# Before
srs_log_tank        console;
srs_log_level       warn;

# After
srs_log_tank        file;
srs_log_file        ./logs/srs.log;
srs_log_level       warn;
```

Ensure the logs volume is mounted in `docker-compose.yml`:

```yaml
srs:
  volumes:
    - ./srs/origin.conf:/usr/local/srs/conf/srs.conf
    - ./srs/logs:/usr/local/srs/logs      # add this line
    - srs-dvr:/dvr
```

```bash
mkdir -p /opt/streaming/srs/logs
docker compose restart srs

# Confirm logs are writing to file
tail -f /opt/streaming/srs/logs/srs.log
```

---

## 7. Auth Service Health Check

**Risk:** If the auth service crashes, Docker does not restart it immediately
(depends on the error type). SRS keeps accepting streams but all HTTP hook
callbacks fail. Depending on SRS hook timeout settings, publishers may be
allowed or denied unpredictably with no visible error.

**File:** `docker-compose.yml`

```yaml
auth:
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
    interval: 15s
    timeout: 5s
    retries: 3
    start_period: 10s
```

The `/health` endpoint already exists in `auth/main.py` and returns:
```json
{ "status": "ok", "active_streams": N, "active_viewers": N }
```

Apply and verify:
```bash
docker compose up -d

# Watch health status
watch -n 5 'docker ps --format "table {{.Names}}\t{{.Status}}"'
# auth container should show: Up X minutes (healthy)
```

---

## 8. Nginx Rate Limiting

**Risk:** Without rate limits a single client or bot can:
- Flood `/api/token` → fills auth service memory with thousands of unused tokens
- Flood `/rtc/v1/play/` → exhausts SRS signaling capacity
- Slow down legitimate viewers

**File:** `nginx/nginx.conf`

Add rate limit zones inside the `http {}` block, before the `server {}` blocks:

```nginx
http {
    # ... existing settings ...

    # Rate limit zones — keyed per client IP
    limit_req_zone  $binary_remote_addr  zone=token_zone:10m  rate=10r/m;
    limit_req_zone  $binary_remote_addr  zone=api_zone:10m    rate=60r/m;
    limit_req_zone  $binary_remote_addr  zone=rtc_zone:10m    rate=5r/m;
    limit_conn_zone $binary_remote_addr  zone=conn_zone:10m;
```

Apply per location inside the `server {}` block:

```nginx
    # Token generation — max 10 per minute per IP
    location /api/token {
        limit_req  zone=token_zone burst=5 nodelay;
        limit_conn conn_zone 10;
        set $auth_backend http://auth:8000;
        rewrite ^/api/(.*)$ /$1 break;
        proxy_pass $auth_backend;
    }

    # WebRTC signaling — max 5 new connections per minute per IP
    location /rtc/v1/play/ {
        limit_req  zone=rtc_zone burst=3 nodelay;
        # ... rest of existing proxy config
    }

    # General API
    location /api/ {
        limit_req  zone=api_zone burst=20 nodelay;
        limit_conn conn_zone 20;
        # ... rest of existing proxy config
    }
```

```bash
docker compose restart nginx
nginx -t  # test config syntax before restarting
```

---

## 9. Player HLS Fallback

**Risk:** For viewers where WebRTC completely fails (corporate UDP blocked,
TURN path unavailable), the player exhausts all 30 retries and shows
"Stream unavailable" with no alternative. The viewer gives up.

HLS is already enabled in SRS (`hls_fragment 2`, `hls_window 6`).
The player just needs to attempt it after WebRTC retries are exhausted.

**File:** `nginx/html/player.html`

Add HLS.js script tag in `<head>`:

```html
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
```

Replace the existing `scheduleRetry` end-condition:

```javascript
// Before
if (retryCount >= MAX_RETRY) {
  setStatus('Stream unavailable. Refresh the page to try again.');
  return;
}

// After
if (retryCount >= MAX_RETRY) {
  tryHlsFallback();
  return;
}
```

Add the fallback function before `connect()`:

```javascript
function tryHlsFallback() {
  var hlsUrl = '/hls/live/' + streamKey + '.m3u8';
  setStatus('Switching to HLS fallback...');

  if (typeof Hls !== 'undefined' && Hls.isSupported()) {
    var hls = new Hls({
      lowLatencyMode:           true,
      liveSyncDurationCount:    2,
      liveMaxLatencyDurationCount: 5,
    });
    hls.loadSource(hlsUrl);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, function () {
      video.muted = false;
      video.play();
      setStatus('LIVE — HLS (higher latency)');
    });
    hls.on(Hls.Events.ERROR, function (_, data) {
      if (data.fatal) {
        setStatus('Stream unavailable. Refresh to try again.');
      }
    });
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    // Safari native HLS
    video.src = hlsUrl;
    video.addEventListener('loadedmetadata', function () { video.play(); });
    setStatus('LIVE — HLS (Safari native)');
  } else {
    setStatus('Stream unavailable. Refresh to try again.');
  }
}
```

---

## Completion Checklist

```
 [ ] 1. SRS ports 1985 + 8080 bound to 127.0.0.1 in docker-compose.yml
 [ ] 2. File descriptor limit set to 65535 — system + Docker + container
 [ ] 3. UDP buffer size increased via sysctl
 [ ] 4. Docker log rotation — 50m × 5 files per container
 [ ] 5. Container memory limits — srs:3g  auth:512m  nginx:256m
 [ ] 6. SRS logging to file — srs/logs/srs.log
 [ ] 7. Auth service healthcheck added to docker-compose.yml
 [ ] 8. Nginx rate limiting on /api/token and /rtc/v1/play/
 [ ] 9. HLS fallback added to player.html
```

Complete items 1–5 before any load test.
Complete all 9 before public launch.

---

*See also: `pre-launch-tasks.md` for TURN server and stream timeout implementation.*
