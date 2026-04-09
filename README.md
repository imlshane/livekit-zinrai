# SRS Streaming Stack

Self-hosted live streaming: OBS → RTMP → SRS → WebRTC → browser.

- **Latency**: <1 second end-to-end (WebRTC)
- **Publishers**: up to 100 simultaneous streams
- **Viewers**: 1000+ concurrent (add edge nodes to scale)
- **Browsers**: Chrome, Firefox, Safari, iOS, Android — no plugin needed
- **Auth**: stream key validation per publisher + time-limited viewer tokens
- **VOD**: auto-records every session → ffmpeg → MP4 → R2 / DO Spaces

## Stack

| Container | Image | Purpose |
|-----------|-------|---------|
| srs-origin | ossrs/srs:5 | RTMP ingest, WebRTC delivery, HLS, DVR |
| srs-auth | ./auth | Stream key auth, viewer tokens, VOD upload |
| srs-nginx | nginx:alpine | HTTPS termination, player page, API proxy |

## Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 80 | TCP | HTTP → HTTPS redirect |
| 443 | TCP | HTTPS — player + API |
| 1935 | TCP | RTMP — OBS stream ingest |
| 1985 | TCP | SRS HTTP API — monitoring (internal) |
| 8000 | **UDP** | WebRTC SRTP media — **must be open in firewall** |

---

## Fresh Server Setup

### 1. Prerequisites

A Linux server (Ubuntu 22.04+) with:
- Docker + Docker Compose v2
- A domain pointing to the server IP
- Ports 80, 443, 1935, 8000/UDP open in the firewall

Install Docker if not present:
```bash
curl -fsSL https://get.docker.com | sh
```

### 2. Clone the repo

```bash
git clone https://github.com/imlshane/livekit-zinrai.git /opt/srs-stream
cd /opt/srs-stream
```

### 3. Get a TLS certificate

```bash
apt install -y certbot
certbot certonly --standalone -d your.domain.com
```

The cert will be at `/etc/letsencrypt/live/your.domain.com/`.

### 4. Configure

**4a. Set your domain in nginx:**

```bash
sed -i 's/livestream.zinrai.live/your.domain.com/g' nginx/nginx.conf
```

**4b. Set your server public IP in SRS config:**

```bash
sed -i 's/209.38.63.157/YOUR_SERVER_PUBLIC_IP/g' srs/origin.conf
```

**4c. Set your secret key:**

```bash
cp .env.example .env
openssl rand -hex 32   # copy this value
nano .env              # paste as SECRET_KEY=
```

**4d. (Optional) VOD upload to R2 / DO Spaces:**

In `.env`:
```
S3_ENDPOINT=https://xxx.r2.cloudflarestorage.com
S3_BUCKET=your-bucket-name
S3_ACCESS_KEY=your-access-key
S3_SECRET_KEY=your-secret-key
```

Leave blank to keep MP4 files locally in the Docker volume.

### 5. Build and start

```bash
docker compose build       # builds auth image (~2 min first time)
docker compose up -d
docker compose logs -f     # watch for errors
```

All three containers should show `Up` within 30 seconds.

### 6. Create publishers

Three test publishers are seeded automatically on first start:

| Username | Stream Key |
|----------|------------|
| test-publisher-1 | `stream-key-001` |
| test-publisher-2 | `stream-key-002` |
| test-publisher-3 | `stream-key-003` |

To create a real publisher:
```bash
curl -X POST https://your.domain.com/api/publishers \
  -H 'Content-Type: application/json' \
  -d '{"username": "alice", "stream_key": "alices-secret-key"}'
```

### 7. Get a viewer token

```bash
curl 'https://your.domain.com/api/token?stream_key=alices-secret-key'
```

Response:
```json
{
  "token": "abc123...",
  "stream_key": "alices-secret-key",
  "expires_in": 3600,
  "player_url": "/player?stream=alices-secret-key&token=abc123..."
}
```

Tokens expire in 1 hour. Generate a new one for each viewing session.

### 8. Configure OBS

| Setting | Value |
|---------|-------|
| Service | Custom |
| Server | `rtmp://your.domain.com/live` |
| Stream Key | Publisher's stream key |
| Encoder | x264, Baseline profile |
| Resolution | 1280×720 |
| Framerate | 30 fps |
| Bitrate | 2500–4000 kbps |

### 9. Watch

Open the player URL in any browser:
```
https://your.domain.com/player?stream=alices-secret-key&token=abc123...
```

- Video starts automatically (muted — browser autoplay policy)
- Click the **speaker icon overlay** to enable audio
- Works on Chrome, Firefox, Safari, iOS, Android

---

## Daily Operations

### Start / stop / restart

```bash
cd /opt/srs-stream
docker compose up -d           # start all
docker compose down            # stop all
docker compose restart         # restart all
docker compose restart srs     # restart SRS only
docker compose restart nginx   # reload nginx config
```

### View logs

```bash
docker compose logs -f          # all services
docker compose logs -f srs      # SRS only
docker compose logs -f auth     # auth service
docker compose logs -f nginx    # nginx
```

### Check live streams

```bash
curl https://your.domain.com/api/streams
```

### Generate viewer token

```bash
curl 'https://your.domain.com/api/token?stream_key=STREAM_KEY'
```

### Check viewer count and bitrate

```bash
curl https://your.domain.com/api/srs/v1/streams     # per-stream stats
curl https://your.domain.com/api/srs/v1/clients     # all connected clients
curl https://your.domain.com/api/srs/v1/summaries   # system totals
```

---

## Autostart on Boot (systemd)

```bash
# Edit WorkingDirectory in the service file to match your install path
sed -i 's|WorkingDirectory=.*|WorkingDirectory=/opt/srs-stream|' srs.service
cp srs.service /etc/systemd/system/

systemctl daemon-reload
systemctl enable srs-stream
systemctl start  srs-stream

# Management
systemctl status  srs-stream
systemctl restart srs-stream
journalctl -u srs-stream -f
```

---

## TLS Certificate Renewal

Certbot auto-renews. After renewal, reload nginx:
```bash
certbot renew --quiet
docker compose restart nginx
```

Automate with cron:
```bash
# /etc/cron.d/certbot-reload
0 3 1 * * root certbot renew --quiet && docker compose -f /opt/srs-stream/docker-compose.yml restart nginx
```

---

## OS Tuning (required before going live)

Apply on every server node before receiving real traffic:

```bash
# /etc/security/limits.conf — add these lines
* soft nofile 65535
* hard nofile 65535

# /etc/sysctl.conf — add these lines
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

When viewer count grows, add edge nodes. They pull from origin on demand — zero config change needed on origin or OBS.

**On the new edge server:**

```bash
git clone https://github.com/imlshane/livekit-zinrai.git /opt/srs-stream
cd /opt/srs-stream
```

Create `srs/edge.conf`:
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

http_server {
    enabled on;
    listen  8080;
}

vhost __defaultVhost__ {
    cluster {
        mode   remote;
        origin ORIGIN_SERVER_PUBLIC_IP;
    }
    rtc {
        enabled     on;
        rtmp_to_rtc on;
    }
}
```

Update `docker-compose.yml` to mount `srs/edge.conf` instead of `srs/origin.conf`, then:
```bash
docker compose up -d srs nginx
```

Add the edge node to your load balancer pool for viewer traffic (port 443). OBS still points to origin directly.

---

## VOD Recording

Every stream session is automatically recorded:

1. SRS writes `.flv` to the DVR volume during the stream
2. When publisher stops, `on_dvr` fires → auth service receives the callback
3. ffmpeg converts `.flv` → `.mp4` (browser-ready, `+faststart`)
4. If S3 is configured → uploads to `recordings/<stream_key>/<file>.mp4`, deletes local copy
5. If S3 is not configured → `.mp4` stays in the Docker volume

---

## API Reference

Base URL: `https://your.domain.com/api/`

| Method | Path | Body / Params | Description |
|--------|------|---------------|-------------|
| GET | `/health` | — | Health check |
| GET | `/streams` | — | Currently live streams |
| GET | `/publishers` | — | All registered publishers |
| POST | `/publishers` | `{"username":"x","stream_key":"y"}` | Create publisher |
| GET | `/token` | `?stream_key=x` | Generate 1-hour viewer token |

SRS monitoring (read-only, proxied from SRS):

| URL | Description |
|-----|-------------|
| `/api/srs/v1/streams` | Active streams: bitrate, frames, client count |
| `/api/srs/v1/clients` | All connected clients with IP and kbps |
| `/api/srs/v1/summaries` | System: CPU, memory, connection totals |

---

## Troubleshooting

**Blank screen / "Waiting for stream..."**
- Check OBS is streaming: `curl https://your.domain.com/api/streams`
- Check stream key in OBS matches a registered publisher key
- Token may be expired — generate a new one

**No audio (video plays silently)**
- Normal — browsers block autoplay with audio by default
- Click the speaker icon on the video to unmute

**OBS disconnects immediately**
- Stream key is not registered → `POST /api/publishers`
- Check auth logs: `docker compose logs auth`

**WebRTC fails / no video after connecting**
- UDP port 8000 must be open in server firewall
- DigitalOcean: Networking → Firewalls → add UDP 8000 inbound rule
- `candidate` in `srs/origin.conf` must be the server's actual public IP

**Certificate errors**
```bash
certbot renew
docker compose restart nginx
```

**Disk full (DVR recordings)**
```bash
# Find the volume path
docker volume inspect livekit_srs-dvr
# Configure S3 in .env so files are uploaded and deleted automatically
```

**Auth service IP changed after restart**

The `http_hooks` in `srs/origin.conf` use a hardcoded container IP (`172.20.0.2`). If the auth container gets a different IP after a full `docker compose down && up`:

```bash
docker inspect srs-auth | grep IPAddress
# Update srs/origin.conf with the new IP, then:
docker compose restart srs
```

To avoid this permanently, assign a fixed IP in `docker-compose.yml`:
```yaml
auth:
  networks:
    default:
      ipv4_address: 172.20.0.2

networks:
  default:
    ipam:
      config:
        - subnet: 172.20.0.0/16
```
