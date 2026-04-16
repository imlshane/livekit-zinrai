# Edge Server Scaling

How to distribute WebRTC viewer load across multiple SRS edge servers.

---

## How It Works

```
OBS × 6  ──RTMP──►  Origin Server  (livestream.zinrai.live)
                     │  SRS origin
                     │  Auth service
                     │  DVR recording
                     │  Nginx (entry point + load balancer)
                     │
                     │  RTMP pull via private VPC
                     │  (one connection per active stream per edge)
                     │
              ┌──────┴──────┐
          Edge 1         Edge 2         ← add more as needed
          SRS edge        SRS edge
          ~200 viewers    ~200 viewers
              │               │
         Viewers UDP     Viewers UDP
         (direct to       (direct to
          edge IP)         edge IP)
```

**Key behaviour:**
- Origin Nginx receives all viewer HTTP traffic and routes signaling to edges
- Each edge pulls the RTMP stream from origin on first viewer request
- Edge converts RTMP → WebRTC locally and manages its own SRTP sessions
- After signaling, WebRTC UDP goes directly to the edge's public IP
- Adding an edge = +200–300 viewer capacity at the same origin load

**What stays on origin only:**
- OBS RTMP ingest
- Auth service (token validation, on_publish/unpublish hooks)
- DVR recording
- Player page and token API
- SQLite / Redis

**What each edge runs:**
- SRS in edge/cluster mode (pulls RTMP from origin)
- Nginx (HTTPS proxy for signaling, health endpoint)
- Nothing else

---

## When to Add an Edge

Add a new edge when you observe either of these on the origin server:

```bash
# SRS CPU above 70% sustained during streams
docker stats --no-stream srs-origin

# Active viewer client count approaching per-server limit
curl -s http://127.0.0.1:1985/api/v1/summaries | python3 -m json.tool | grep nb_conn
```

Rough capacity guide per server:

| Server size | Stable WebRTC viewers |
|---|---|
| c-2 (2 vCPU) | ~150 |
| c-4 (4 vCPU) | ~300 |
| c-8 (8 vCPU) | ~600 |

Two c-4 edges = ~600 viewers total, origin protected from all viewer load.

---

## Part 1 — Changes to Origin Server

### 1.1 Expose Auth Service on Private VPC

Edges need to reach the auth service for `on_play` / `on_stop` hooks.
Auth currently binds only inside Docker. Expose it on the private VPC IP.

**File:** `docker-compose.yml`

```yaml
auth:
  ports:
    - "ORIGIN_PRIVATE_IP:8000:8000"   # replace with actual VPC IP e.g. 10.0.0.5
```

After this change, edges can call `http://10.0.0.5:8000/on_play` over the
private VPC. This port is not reachable from the public internet.

### 1.2 Update Origin Nginx to Load-Balance Edges

When you have edges, origin Nginx distributes `/rtc/v1/play/` signaling
across them instead of handling it locally.

**File:** `nginx/nginx.conf`

Add an `upstream` block and update the `/rtc/v1/play/` location.
Start with only origin in the upstream. Add edges one by one.

```nginx
http {

    # ── SRS edge pool ────────────────────────────────────────────────────────
    # Start with origin only. Add edge private IPs as you launch them.
    # SRS signaling API listens on port 1985.
    upstream srs_edges {
        least_conn;                         # route to least-loaded edge
        server 127.0.0.1:1985;              # origin itself (remove after first edge added)
        # server EDGE1_PRIVATE_IP:1985;     # uncomment when edge 1 is ready
        # server EDGE2_PRIVATE_IP:1985;     # uncomment when edge 2 is ready

        keepalive 32;
    }

    server {
        # ... existing server block ...

        # Update this location to use the upstream pool
        location /rtc/v1/play/ {
            proxy_pass            http://srs_edges;
            proxy_http_version    1.1;
            proxy_set_header      Connection "";
            proxy_set_header      Host $host;
            proxy_set_header      X-Real-IP $remote_addr;
            add_header            Access-Control-Allow-Origin *;
            add_header            Access-Control-Allow-Methods "POST, OPTIONS";
            add_header            Access-Control-Allow-Headers "Content-Type";
            if ($request_method = OPTIONS) { return 204; }
        }
    }
}
```

Apply:
```bash
docker compose exec nginx nginx -s reload
```

---

## Part 2 — Edge Server Setup

Repeat this for each edge you add.

### 2.1 Provision Edge Droplet

```
Type    : CPU-Optimized
Size    : c-4  (4 vCPU, 8 GB RAM)  ← handles ~300 WebRTC viewers
OS      : Ubuntu 22.04 LTS
Region  : Same region as origin (required for private VPC)
VPC     : Same VPC as origin droplet
```

Note the edge's public IP and private VPC IP.
- Public IP  → used as WebRTC ICE candidate (viewers connect UDP here)
- Private IP → used for RTMP pull from origin (free, fast)

### 2.2 Initial Server Setup

```bash
ssh root@EDGE_PUBLIC_IP

apt update && apt upgrade -y
apt install -y curl ufw

# Apply hardening from server-hardening.md
cat >> /etc/security/limits.conf << 'EOF'
* soft nofile 65535
* hard nofile 65535
EOF

cat > /etc/sysctl.d/99-webrtc.conf << 'EOF'
net.core.rmem_max=26214400
net.core.wmem_max=26214400
net.core.rmem_default=1048576
net.core.wmem_default=1048576
EOF
sysctl -p /etc/sysctl.d/99-webrtc.conf

# Install Docker
curl -fsSL https://get.docker.com | sh
systemctl enable docker

cat > /etc/docker/daemon.json << 'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "5" }
}
EOF
systemctl restart docker
```

### 2.3 Firewall

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 8000/udp     # WebRTC media — viewers connect here directly
ufw enable
```

Note: No port 1935 needed on edge. Edge pulls RTMP from origin via private VPC
outbound — no inbound RTMP.

### 2.4 Directory Structure

```bash
mkdir -p /opt/edge/{srs/conf,srs/logs,nginx/conf,nginx/html}
```

### 2.5 SRS Edge Config

```bash
cat > /opt/edge/srs/conf/edge.conf << 'EOF'
listen              1935;
max_connections     500;
daemon              off;
srs_log_tank        file;
srs_log_file        ./logs/srs.log;
srs_log_level       warn;

# WebRTC — edge public IP as ICE candidate
# Viewers connect their UDP directly to this IP
rtc_server {
    enabled     on;
    listen      8000;
    candidate   EDGE_PUBLIC_IP;    # ← replace with actual edge public IP
}

http_api {
    enabled     on;
    listen      1985;
    crossdomain on;
}

http_server {
    enabled     on;
    listen      8080;
    dir         ./objs/nginx;
    crossdomain on;
}

vhost __defaultVhost__ {

    # Cluster — pull RTMP from origin via private VPC
    cluster {
        mode    remote;
        origin  ORIGIN_PRIVATE_IP:1935;    # ← replace with origin private VPC IP
    }

    # Convert pulled RTMP → WebRTC for viewers
    rtc {
        enabled     on;
        rtmp_to_rtc on;
        nack        on;
        twcc        on;
    }

    # Notify origin auth service of viewer join/leave
    # Auth service is only on origin — edge calls it over private VPC
    http_hooks {
        enabled     on;
        on_play     http://ORIGIN_PRIVATE_IP:8000/on_play;   # ← replace
        on_stop     http://ORIGIN_PRIVATE_IP:8000/on_stop;   # ← replace
    }
}
EOF
```

Replace the three placeholder values:
- `EDGE_PUBLIC_IP` — the edge droplet's public IP
- `ORIGIN_PRIVATE_IP` — the origin droplet's private VPC IP (e.g. 10.0.0.5)

### 2.6 Edge Nginx Config

Edge Nginx only needs to proxy signaling and serve a health endpoint.
SSL termination happens at the origin Nginx — edge receives plain HTTP
from the private VPC.

```bash
cat > /opt/edge/nginx/conf/nginx.conf << 'EOF'
worker_processes      auto;
worker_rlimit_nofile  65535;

events {
    worker_connections 4096;
    use                epoll;
    multi_accept       on;
}

http {
    sendfile    on;
    tcp_nopush  on;
    tcp_nodelay on;

    # ── Edge HTTP server ──────────────────────────────────────────────────
    # Origin Nginx proxies here over private VPC (plain HTTP, no SSL needed)
    server {
        listen 80;

        # Health check — origin Nginx checks this before routing traffic here
        location /health {
            return 200 "ok\n";
            add_header Content-Type text/plain;
        }

        # WebRTC signaling — forward to local SRS
        location /rtc/v1/play/ {
            proxy_pass         http://127.0.0.1:1985;
            proxy_http_version 1.1;
            proxy_set_header   Host $host;
            proxy_set_header   X-Real-IP $http_x_real_ip;
        }

        # SRS stats — for monitoring
        location /api/srs/ {
            proxy_pass       http://127.0.0.1:1985/api/;
            proxy_set_header Host $host;
        }
    }
}
EOF
```

### 2.7 Edge Docker Compose

```bash
cat > /opt/edge/docker-compose.yml << 'EOF'
services:

  srs:
    image: ossrs/srs:5
    container_name: srs-edge
    restart: unless-stopped
    network_mode: host         # use host network — needed for WebRTC UDP candidate IP
    volumes:
      - ./srs/conf/edge.conf:/usr/local/srs/conf/srs.conf
      - ./srs/logs:/usr/local/srs/logs
    command: ./objs/srs -c conf/srs.conf
    ulimits:
      nofile:
        soft: 65535
        hard: 65535
    mem_limit: 5g
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:1985/api/v1/summaries"]
      interval: 30s
      timeout: 5s
      retries: 3

  nginx:
    image: nginx:alpine
    container_name: edge-nginx
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./nginx/conf/nginx.conf:/etc/nginx/nginx.conf:ro
    mem_limit: 128m
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost/health"]
      interval: 15s
      timeout: 3s
      retries: 3
EOF
```

> **Why `network_mode: host`:** SRS needs to bind to the actual server IP
> for the WebRTC UDP candidate to work correctly. Bridge networking causes
> SRS to see container IPs instead of the real server IP, breaking ICE.

### 2.8 Start the Edge

```bash
cd /opt/edge
docker compose up -d
docker compose ps
# Both containers should show healthy within 30 seconds

# Verify SRS edge is running and can reach origin
curl http://localhost:1985/api/v1/summaries
# Should return: {"code":0,"data":{"ok":true,...}}
```

### 2.9 Register Edge in Origin Nginx

On the **origin server**, update `nginx/nginx.conf` upstream block:

```nginx
upstream srs_edges {
    least_conn;
    # server 127.0.0.1:1985;            # remove origin from pool once edge is live
    server EDGE1_PRIVATE_IP:1985;        # ← add this line
    keepalive 32;
}
```

Also add a health check for the edge:
```nginx
upstream srs_edges {
    least_conn;
    server EDGE1_PRIVATE_IP:1985 max_fails=3 fail_timeout=10s;
    keepalive 32;
}
```

Reload:
```bash
docker compose exec nginx nginx -s reload
```

---

## Part 3 — Verify Edge is Working

### Step 1 — Confirm edge pulls stream from origin

Start an OBS stream. Then on the edge server:

```bash
# Should show 0 streams before anyone watches from this edge
curl http://localhost:1985/api/v1/streams

# Connect a viewer to the player page
# Within a few seconds, edge should show the stream being pulled
curl http://localhost:1985/api/v1/streams
# Should now show the stream with clients connected
```

### Step 2 — Confirm viewer connects to edge IP

In the viewer's browser DevTools:
- Open `chrome://webrtc-internals`
- Find the active connection
- Check `selectedCandidatePairId` → local candidate should be the edge public IP

### Step 3 — Confirm load balancing is distributing

On origin, watch Nginx access log while opening player in two browser tabs:

```bash
docker compose logs -f nginx | grep rtc
# Each /rtc/v1/play/ request should alternate between edges
# (least_conn distributes based on active connections)
```

---

## Part 4 — Adding More Edges

Each additional edge is identical to Part 2. Steps:

```
1. Provision new droplet (same spec, same VPC)
2. Copy /opt/edge/ setup — only change EDGE_PUBLIC_IP in edge.conf
3. Start docker compose
4. Add new edge private IP to origin Nginx upstream block
5. Reload origin Nginx
```

No player changes. No origin SRS changes. No auth changes.

Capacity after each addition:

| Edges | Total viewer capacity |
|---|---|
| 0 (origin only) | ~300 |
| 1 edge | ~300 + 300 = 600 |
| 2 edges | ~900 |
| 3 edges | ~1200 |

---

## Part 5 — Monitoring Multiple Servers

Run this on origin to check all servers at once. Replace IPs as needed.

```bash
# /opt/scripts/check-all.sh
#!/bin/bash

SERVERS=(
  "origin:127.0.0.1"
  "edge1:EDGE1_PRIVATE_IP"
  "edge2:EDGE2_PRIVATE_IP"
)

for entry in "${SERVERS[@]}"; do
  name="${entry%%:*}"
  ip="${entry##*:}"
  result=$(curl -s --max-time 3 "http://${ip}:1985/api/v1/summaries" 2>/dev/null)
  clients=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['data']['nb_conn_clients'])" 2>/dev/null || echo "unreachable")
  echo "${name}: ${clients} clients"
done
```

```bash
chmod +x /opt/scripts/check-all.sh
watch -n 10 /opt/scripts/check-all.sh
```

---

## Quick Reference

```
Origin role    : RTMP ingest, auth, DVR, Nginx load balancer
Edge role      : WebRTC viewer delivery only
RTMP pull      : Edge → Origin via private VPC (auto, on first viewer)
UDP media      : Viewer → Edge directly (bypasses Nginx/LB)
Auth hooks     : Edge → Origin auth service via private VPC
Add edge       : Provision droplet → deploy → add to Nginx upstream → reload
Remove edge    : Remove from Nginx upstream → reload → shut down droplet
```

---

*See also: `pre-launch-tasks.md` for TURN server and stream timeout.*
*See also: `server-hardening.md` for per-server hardening steps (apply to each edge).*
