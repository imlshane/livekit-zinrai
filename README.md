# SRS Streaming Stack

WebRTC-free live streaming: OBS → RTMP → SRS → HTTP-FLV → browser.

- **Latency**: ~1 second end-to-end
- **Capacity**: 100 publishers, 1000+ viewers
- **Auth**: stream key validation + one-time viewer tokens
- **VOD**: DVR recording → ffmpeg → MP4 → R2 / DO Spaces

## Stack

| Service | Image | Purpose |
|---------|-------|---------|
| srs | ossrs/srs:5 | RTMP ingest + HTTP-FLV / HLS delivery + DVR |
| auth | ./auth | Stream key auth, viewer tokens, VOD upload |
| nginx | nginx:alpine | Reverse proxy, player page, TLS (prod) |

## Local Testing

### 1. Start the stack

```bash
docker compose up -d
docker compose logs -f
```

### 2. Get OBS settings and a viewer token

```bash
bash scripts/test-local.sh
# or for a specific stream key:
bash scripts/test-local.sh stream-key-002
```

Three test publishers are pre-seeded:

| Username | Stream Key |
|----------|------------|
| test-publisher-1 | `stream-key-001` |
| test-publisher-2 | `stream-key-002` |
| test-publisher-3 | `stream-key-003` |

### 3. Configure OBS

- **Service**: Custom
- **Server**: `rtmp://localhost/live`
- **Stream Key**: `stream-key-001` (or any seeded key)
- **Video**: 720p, 30fps, H.264
- **Bitrate**: 2500–4000 kbps

### 4. Watch

Open the player URL printed by `test-local.sh`:
```
http://localhost/player?stream=stream-key-001&token=<generated-token>
```

## API Reference

All endpoints are available at `http://localhost/api/`.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/streams` | Currently live streams |
| GET | `/publishers` | All registered publishers |
| POST | `/publishers` | Create publisher `{"username":"x","stream_key":"y"}` |
| GET | `/token?stream_key=x` | Generate one-time viewer token |

SRS stats:
| URL | Description |
|-----|-------------|
| `http://localhost/api/srs/v1/streams` | Active streams with bitrate |
| `http://localhost/api/srs/v1/clients` | All connected clients |
| `http://localhost/api/srs/v1/summaries` | System stats |

## Ports

| Port | Purpose |
|------|---------|
| 80 | HTTP — nginx (player + proxy) |
| 1935 | RTMP — OBS ingest |
| 1985 | SRS HTTP API (direct, for debugging) |

## Production Checklist

Before going live, apply OS tuning on every node:

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

# Apply immediately (no reboot)
sysctl -p
```

Add TLS to `nginx/nginx.conf` and set `DOMAIN` / `SECRET_KEY` in `.env`.

## Architecture (Multi-Node Production)

```
Publishers (OBS) → Floating IP → Origin SRS #1 (active)
                                       ↓ fallback
                                 Origin SRS #2 (standby)
                                       ↓ pull
                              Edge SRS → DO Load Balancer → Viewers

Auth Service (shared, behind LB)
DVR Worker (on origin nodes)
```

See architecture planning notes for full multi-node setup.
