# SRS Streaming Stack

Self-hosted live streaming: OBS → RTMP → SRS5 → WebRTC → browser.

- **Latency**: <1 second end-to-end (WebRTC)
- **Capacity**: 1000+ concurrent viewers (add edge nodes to scale)
- **Auth**: stream key validation per publisher + time-limited viewer tokens
- **VOD**: auto-records every session → ffmpeg → MP4 → R2 / DO Spaces (optional)
- **Browsers**: Chrome, Firefox, Safari, iOS, Android — no plugin needed

## Stack

| Container | Image | Purpose |
|-----------|-------|---------|
| srs-origin | ossrs/srs:5 | RTMP ingest, WebRTC delivery, HLS fallback, DVR |
| srs-auth | ./auth | Stream key auth, viewer tokens, VOD upload |
| srs-nginx | nginx:alpine | HTTPS termination, player page, API proxy |

## Ports Required

| Port | Protocol | Purpose |
|------|----------|---------|
| 80 | TCP | HTTP → HTTPS redirect |
| 443 | TCP | HTTPS — player + API |
| 1935 | TCP | RTMP — OBS stream ingest |
| 8000 | **UDP** | WebRTC SRTP media — **must be open in firewall** |

---

## Fresh Server Setup

### 1. Prerequisites

Ubuntu 22.04+ with Docker installed:
```bash
curl -fsSL https://get.docker.com | sh
```

Open required ports in your firewall (DigitalOcean → Networking → Firewall):
- TCP: 80, 443, 1935
- UDP: 8000

### 2. Clone the repo

```bash
git clone https://github.com/imlshane/livekit-zinrai.git /opt/livekit
cd /opt/livekit
```

### 3. Get a TLS certificate

```bash
apt install -y certbot
certbot certonly --standalone -d your.domain.com
```

### 4. Configure

**Set your domain in nginx:**
```bash
sed -i 's/livestream.zinrai.live/your.domain.com/g' nginx/nginx.conf
```

**Set your server public IP in SRS config:**
```bash
sed -i 's/209.38.63.157/YOUR_SERVER_IP/g' srs/origin.conf
```

**Create your `.env`:**
```bash
cp .env.example .env
# Edit .env and set SECRET_KEY to a random value:
openssl rand -hex 32   # copy this output into .env as SECRET_KEY=
```

**(Optional) VOD upload to R2 / DO Spaces** — add to `.env`:
```
S3_ENDPOINT=https://xxx.r2.cloudflarestorage.com
S3_BUCKET=streams
S3_ACCESS_KEY=your-access-key
S3_SECRET_KEY=your-secret-key
```
Leave blank to keep MP4 recordings locally inside the Docker volume.

### 5. Build and start

```bash
docker compose build       # builds auth image (~2 min first time)
docker compose up -d
docker compose logs -f     # watch for errors
```

All three containers should show `Up` within 30 seconds.

### 6. Enable autostart on boot (systemd)

```bash
bash scripts/install-service.sh
```

Management commands after install:
```bash
systemctl start   srs-stream
systemctl stop    srs-stream
systemctl restart srs-stream
systemctl status  srs-stream
journalctl -u srs-stream -f   # follow logs
```

---

## How to Stream

### Step 1 — Configure OBS

| Setting | Value |
|---------|-------|
| Service | Custom |
| Server | `rtmp://YOUR_SERVER_IP/live` |
| Stream Key | your stream key (e.g. `stream-key-001`) |
| Encoder | x264, Baseline profile |
| Resolution | 1280×720 |
| Framerate | 30 fps |
| Bitrate | 2500–4000 kbps |

Click **Start Streaming**.

### Step 2 — Get a viewer token

Tokens expire in 1 hour. Generate one per viewing session:

```bash
curl 'https://your.domain.com/api/token?stream_key=YOUR_STREAM_KEY'
```

Response:
```json
{
  "token": "abc123...",
  "stream_key": "stream-key-001",
  "expires_in": 3600,
  "player_url": "/player?stream=stream-key-001&token=abc123..."
}
```

### Step 3 — Open the player

```
https://your.domain.com/player?stream=YOUR_STREAM_KEY&token=YOUR_TOKEN
```

- Video starts automatically (muted — browser autoplay policy)
- Click the **speaker icon** overlay to enable audio
- Status shows: `LIVE — WebRTC`

---

## Publishers

Three test publishers are seeded automatically on first start:

| Username | Stream Key |
|----------|------------|
| test-publisher-1 | `stream-key-001` |
| test-publisher-2 | `stream-key-002` |
| test-publisher-3 | `stream-key-003` |

### Create a new publisher

```bash
curl -X POST https://your.domain.com/api/publishers \
  -H 'Content-Type: application/json' \
  -d '{"username": "alice"}'
```

Response includes a randomly generated `stream_key`. Or supply your own:
```bash
curl -X POST https://your.domain.com/api/publishers \
  -H 'Content-Type: application/json' \
  -d '{"username": "alice", "stream_key": "alices-key"}'
```

### List all publishers

```bash
curl https://your.domain.com/api/publishers
```

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/streams` | GET | List currently live streams |
| `/api/publishers` | GET | List all publishers |
| `/api/publishers` | POST | Create a publisher |
| `/api/token?stream_key=KEY` | GET | Generate a viewer token (1 hr) |
| `/api/health` | GET | Auth service health check |
| `/api/srs/v1/streams` | GET | SRS per-stream stats |
| `/api/srs/v1/clients` | GET | All connected clients |
| `/api/srs/v1/summaries` | GET | System totals |

---

## Daily Operations

### Check live streams

```bash
curl https://your.domain.com/api/streams
```

### View logs

```bash
docker compose logs -f          # all services
docker compose logs -f srs      # SRS only
docker compose logs -f auth     # auth service
docker compose logs -f nginx    # nginx
```

### Restart a single service

```bash
docker compose restart nginx    # reload nginx config
docker compose restart srs      # restart SRS
docker compose restart auth     # restart auth service
```

### Local test helper

```bash
bash scripts/test-local.sh [stream-key]
```

Prints OBS settings, generates a viewer token, and shows live stream status.

---

## TLS Certificate Renewal

Certbot renews automatically. After renewal, reload nginx:
```bash
docker compose restart nginx
```

Automate with cron:
```bash
# /etc/cron.d/certbot-reload
0 3 1 * * root certbot renew --quiet && docker compose -f /opt/livekit/docker-compose.yml restart nginx
```

---

## OS Tuning (before going live)

```bash
# /etc/security/limits.conf
* soft nofile 65535
* hard nofile 65535

# /etc/sysctl.conf
fs.file-max = 200000
net.core.somaxconn = 65535
net.core.rmem_max = 134217728
net.core.wmem_max = 134217728
net.ipv4.tcp_rmem = 4096 87380 134217728
net.ipv4.tcp_wmem = 4096 65536 134217728
net.ipv4.tcp_congestion_control = bbr
net.core.default_qdisc = fq

# Apply without reboot
sysctl -p
```

---

## Scaling: Adding Edge Nodes

Edge nodes pull from origin on demand — no OBS or origin config changes needed.

**On the new edge server**, create `srs/edge.conf`:
```nginx
listen              1935;
max_connections     3000;
daemon              off;
srs_log_tank        console;

rtc_server {
    enabled   on;
    listen    8000;
    candidate EDGE_SERVER_PUBLIC_IP;
}

http_api {
    enabled on;
    listen  1985;
}

vhost __defaultVhost__ {
    cluster {
        mode   remote;
        origin ORIGIN_SERVER_IP;
    }
    rtc {
        enabled     on;
        rtmp_to_rtc on;
    }
}
```

Run SRS with the edge config:
```bash
docker run -d --name srs-edge \
  --network host \
  -v $(pwd)/srs/edge.conf:/usr/local/srs/conf/srs.conf \
  ossrs/srs:5
```

Point your load balancer at all edge nodes. Viewers connect to any edge; origin handles all publishing.
