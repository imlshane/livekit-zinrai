# Zinrai LiveKit Stack

WebRTC live streaming with sub-second latency using LiveKit.

- **Ingest**: OBS → RTMP → LiveKit Ingress → WebRTC
- **Delivery**: LiveKit SFU → browser via WebRTC
- **Capacity**: tested up to 600 concurrent viewers (single $48/mo droplet)
- **Latency**: <1 second end-to-end

## Stack

| Service | Image | Purpose |
|---------|-------|---------|
| redis | redis:7-alpine | LiveKit state / pub-sub |
| livekit | livekit/livekit-server | WebRTC SFU |
| ingress | livekit/ingress | RTMP → WebRTC transcoding |
| nginx | nginx:alpine | TLS termination + player page |

All services run with `network_mode: host` — required on Linux for WebRTC UDP and inter-service communication.

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/imlshane/livekit-zinrai.git
cd livekit-zinrai

# Create real config files from examples
cp livekit.yaml.example  livekit.yaml
cp ingress.yaml.example  ingress.yaml
cp .env.example          .env
```

Edit `livekit.yaml`:
- Set `node_ip` to your server's public IP
- Replace `YOUR_API_KEY` / `YOUR_API_SECRET` with real values
  ```bash
  # Generate a secret:
  openssl rand -hex 32
  ```

Edit `ingress.yaml` with the same key/secret.

Edit `nginx/nginx.conf` — replace `YOUR_DOMAIN` with your domain.

### 2. TLS certificate

```bash
# Install certbot on host
apt install -y certbot

# Get certificate (port 80 must be free)
certbot certonly --standalone -d your.domain.com
```

### 3. Start services

```bash
docker compose up -d
docker compose logs -f
```

### 4. Create RTMP ingress

```bash
export LIVEKIT_API_KEY=your-key
export LIVEKIT_API_SECRET=your-secret
bash scripts/create-ingress.sh test-room
```

Save the `ingress_id` and `stream_key` from the output.

### 5. Generate viewer token

```bash
export LIVEKIT_API_KEY=your-key
export LIVEKIT_API_SECRET=your-secret
python3 scripts/generate-token.py --room test-room --expire-days 30
```

Paste the token into `nginx/html/player.html` as the `TOKEN` value, then reload nginx:

```bash
docker compose restart nginx
```

### 6. Configure OBS

- **Service**: Custom
- **Server**: `rtmp://YOUR_SERVER_IP/live`
- **Stream Key**: `<stream_key from step 4>`
- **Video**: 720p, 30fps, H.264
- **Bitrate**: 2500–4000 kbps

### 7. Watch

Open `https://your.domain.com/player` in a browser.

## Key Config Notes

- **Single-layer preset** (`H264_720P_30FPS_1_LAYER`): prevents LiveKit's congestion control from switching to a blurry lower quality layer
- **`adaptiveStream: false`** in player: locks to highest quality regardless of network
- **`network_mode: host`**: required for all Docker services — containers must share the host network for WebRTC UDP ports (50000–51000)
- **Player token**: viewer-only JWT (canPublish: false). Regenerate when expired.

## Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 80 | TCP | HTTP → HTTPS redirect |
| 443 | TCP | HTTPS + WebSocket (nginx) |
| 1935 | TCP | RTMP ingest (OBS → ingress) |
| 6379 | TCP | Redis (localhost only) |
| 7880 | TCP | LiveKit signal (localhost only) |
| 7881 | TCP | LiveKit RTC over TCP |
| 50000–51000 | UDP | LiveKit WebRTC media |

## Scaling

For more than ~800 concurrent viewers, add a second LiveKit node behind a load balancer. See the livekit-setup.md in the zinrai-stream repo for the Phase 2 dual-node plan.
