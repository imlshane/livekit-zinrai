# gitlive — Claude Code Project Instructions

## Dev Server

| Key | Value |
|-----|-------|
| Host | `104.131.166.150` |
| User | `root` |
| Git folder | `/opt/livekit` |
| Compose file | `/opt/livekit/docker-compose.yml` |

SSH shortcut: `ssh root@104.131.166.150`

## Default Debugging Workflow

When something breaks or needs testing on the server, **always SSH and check directly**
rather than giving instructions — the server credentials are in local memory.

```bash
# Tail all container logs together
ssh root@104.131.166.150 "cd /opt/livekit && docker compose logs -f --tail=50"

# Auth service only (most relevant for stream issues)
ssh root@104.131.166.150 "docker logs srs-auth -f --tail=100"

# Caddy (SSL / routing issues)
ssh root@104.131.166.150 "docker logs srs-caddy -f --tail=50"

# SRS origin (RTMP / WebRTC issues)
ssh root@104.131.166.150 "docker logs srs-origin -f --tail=50"

# Restart + rebuild after code changes
ssh root@104.131.166.150 "cd /opt/livekit && git pull && docker compose up -d --build auth"
```

## Container Names

| Container | Role |
|-----------|------|
| `srs-auth` | FastAPI auth + webhook callbacks (Python) |
| `srs-origin` | SRS media server (RTMP ingest, WebRTC, DVR) |
| `srs-caddy` | Caddy reverse proxy + automatic TLS |

## Key Env Vars (set in `/opt/live/.env`)

- `SERVER_IP` — public IP, used as WebRTC ICE candidate (`CANDIDATE` in SRS)
- `DOMAIN` — current: `devlivestream.zinrai.live`
- `ACME_EMAIL` — for Let's Encrypt cert notifications
- `RECONNECT_GRACE_SEC` — seconds to hold session open after drop (default 30)
- `RECORD_URL` / `RECORD_API_KEY` — recordings platform integration
- `REDIS_URL` — stream analytics

## Branch Strategy

- `main` — production
- `develop` — active dev/test (deployed on dev server)

Always push to `develop` for changes being tested on the dev server.

## Architecture Notes

- SRS fires HTTP webhooks to the auth service (`on_publish`, `on_unpublish`, `on_dvr`, etc.)
- Auth service fixed IP: `172.20.0.2` (referenced in `srs/origin.conf` http_hooks)
- DVR recordings: one `.flv` per RTMP connection → converted to `.mp4` via ffmpeg
- Reconnect grace window (`RECONNECT_GRACE_SEC`): prevents multiple video records when
  OBS drops and reconnects due to network instability
- Full architecture reference: `.claude/streaming-architecture.md`
