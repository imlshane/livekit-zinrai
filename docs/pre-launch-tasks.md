# Pre-Launch Tasks

Two items to complete before going live.

---

## Task 1 — TURN Server (Coturn)

### Why

WebRTC uses direct UDP between SRS and the viewer's browser.
Viewers behind corporate firewalls, hotel WiFi, or strict NAT (~10–30% of users)
cannot establish a direct UDP path — they get a black screen with no error.

A TURN server relays the media for those viewers as a fallback.
Without it those users are silently locked out.

### Why a Separate Droplet (Not Same Server, Not App Platform)

**App Platform** — not suitable. TURN requires raw UDP ports (3478 and relay
range 49152–65535/udp). App Platform only exposes HTTP/HTTPS. Not possible.

**Same server** — works but is risky. If 30% of 600 viewers need TURN relay,
that is ~180 viewers × 2.5 Mbps = ~450 Mbps extra outbound on the same machine
that is already running SRS ingest. This competes with your streams.

**Separate small droplet** — correct choice. TURN is pure network relay with
near-zero CPU. A 1 vCPU / 1 GB droplet handles this load easily and keeps
your SRS server untouched.

### Droplet Spec

```
Type    : Basic (shared CPU is fine — TURN is not CPU-bound)
Size    : s-1vcpu-1gb  ($6/mo)
OS      : Ubuntu 22.04 LTS
Region  : Same region as your SRS droplet
```

### Step 1 — Provision Droplet

Create the droplet in DigitalOcean. Note the public IP — referred to as
`TURN_SERVER_IP` below.

### Step 2 — Install Coturn

```bash
ssh root@TURN_SERVER_IP

apt update && apt upgrade -y
apt install -y coturn certbot

# Enable coturn service
echo "TURNSERVER_ENABLED=1" >> /etc/default/coturn
```

### Step 3 — SSL Certificate for TURN

TURN over TLS (port 5349) is required for browsers that block plain UDP.

```bash
# DNS: add an A record   turn.yourdomain.com → TURN_SERVER_IP
# Wait for DNS to propagate, then:

certbot certonly --standalone \
  -d turn.yourdomain.com \
  --non-interactive \
  --agree-tos \
  --email your@email.com
```

### Step 4 — Configure Coturn

```bash
cat > /etc/turnserver.conf << 'EOF'
# Network
listening-port=3478
tls-listening-port=5349
listening-ip=0.0.0.0
external-ip=TURN_SERVER_IP

# Auth
fingerprint
lt-cred-mech
user=streaming:REPLACE_WITH_STRONG_PASSWORD
realm=turn.yourdomain.com

# SSL
cert=/etc/letsencrypt/live/turn.yourdomain.com/fullchain.pem
pkey=/etc/letsencrypt/live/turn.yourdomain.com/privkey.pem

# Relay port range
min-port=49152
max-port=65535

# Logging
log-file=/var/log/turnserver.log
verbose
EOF

systemctl enable coturn
systemctl start coturn
systemctl status coturn
```

### Step 5 — Firewall on TURN Droplet

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 3478/tcp
ufw allow 3478/udp
ufw allow 5349/tcp
ufw allow 5349/udp
ufw allow 49152:65535/udp
ufw enable
```

### Step 6 — Test TURN is Working

Use the Trickle ICE tool to verify before touching the player:
Open https://webrtc.github.io/samples/src/content/peerconnection/trickle-ice/

```
STUN or TURN URI : turn:turn.yourdomain.com:3478
Username         : streaming
Password         : REPLACE_WITH_STRONG_PASSWORD
```

Click "Gather candidates" — you should see candidates with type `relay`.
If relay candidates appear, TURN is working correctly.

### Step 7 — Update Player

File: `nginx/html/player.html`

Find the `RTCPeerConnection` config and replace:

```javascript
// Before
pc = new RTCPeerConnection({
  iceServers: [{ urls: 'stun:stun.l.google.com:19302' }],
  bundlePolicy: 'max-bundle',
});
```

```javascript
// After
pc = new RTCPeerConnection({
  iceServers: [
    { urls: 'stun:stun.l.google.com:19302' },
    {
      urls:       ['turn:turn.yourdomain.com:3478', 'turns:turn.yourdomain.com:5349'],
      username:   'streaming',
      credential: 'REPLACE_WITH_STRONG_PASSWORD',
    },
  ],
  bundlePolicy: 'max-bundle',
  iceTransportPolicy: 'all',   // 'all' = try direct first, fall back to TURN
});
```

### Step 8 — SSL Auto-Renewal for TURN Cert

```bash
# Add renewal hook to restart coturn after cert renewal
cat > /etc/letsencrypt/renewal-hooks/post/restart-coturn.sh << 'EOF'
#!/bin/bash
systemctl restart coturn
EOF
chmod +x /etc/letsencrypt/renewal-hooks/post/restart-coturn.sh

# Test renewal
certbot renew --dry-run
```

### Verify End-to-End

1. Connect a device on a mobile hotspot (avoids corporate NAT)
2. Open browser DevTools → Network → filter `rtc`
3. You should see ICE candidates negotiated
4. Open `chrome://webrtc-internals` → check active candidate pair type
   - `host` = direct connection (no TURN needed)
   - `relay` = going through TURN (working correctly for restricted networks)

---

## Task 2 — Stream Timeout + Max 6 Concurrent Streams

### Why

Without a timeout:
- OBS hangs or freezes → stream slot occupied forever
- DVR file grows until disk is full
- That publisher's stream key is locked out until manual intervention

Without a hard cap at 6:
- More than 6 simultaneous streams = server bandwidth exceeded

### What to Change

**File:** `auth/main.py`

#### Change 1 — Lower MAX_PUBLISHERS to 6

```python
# Line 54 — change from:
MAX_PUBLISHERS = 100

# To:
MAX_PUBLISHERS = 6
```

This enforces the limit at stream start. A 7th OBS connection attempting
to publish will be rejected with a clear error in the SRS logs.

#### Change 2 — Add MAX_STREAM_DURATION_SEC constant

Add below MAX_PUBLISHERS:

```python
MAX_STREAM_DURATION_SEC = 7200   # 2 hours — hard stop
STREAM_WARN_BEFORE_SEC  = 600    # warn 10 minutes before hard stop
```

#### Change 3 — Add force_stop_stream function

Add after the `srs_deny` function (around line 241):

```python
async def force_stop_stream(stream_key: str, reason: str = "timeout") -> None:
    """
    Kick the publisher via the SRS HTTP API.
    SRS will fire on_unpublish automatically after the kick.
    """
    try:
        info      = active_streams.get(stream_key, {})
        client_id = info.get("client_id")

        if not client_id:
            log.warning(f"force_stop: no client_id for {stream_key} — removing from state only")
            active_streams.pop(stream_key, None)
            return

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.delete(f"{SRS_API_URL}/api/v1/clients/{client_id}")

        if resp.is_success or resp.status_code == 404:
            log.info(f"🛑 Force-stopped stream={stream_key} client={client_id} reason={reason}")
        else:
            log.warning(f"force_stop: SRS returned {resp.status_code} for client {client_id}")

    except Exception as e:
        log.error(f"force_stop failed for {stream_key}: {e}")
        # Remove from state anyway so the slot is freed
        active_streams.pop(stream_key, None)
```

#### Change 4 — Add stream watchdog + disk guard tasks

Add after the `force_stop_stream` function:

```python
async def stream_watchdog() -> None:
    """
    Runs every 60 seconds.
    - Warns at (MAX_STREAM_DURATION_SEC - STREAM_WARN_BEFORE_SEC)
    - Hard-stops at MAX_STREAM_DURATION_SEC
    """
    while True:
        await asyncio.sleep(60)
        now = time.time()

        for stream_key, info in list(active_streams.items()):
            elapsed_sec = now - info.get("started_at", now)
            elapsed_min = int(elapsed_sec // 60)
            warn_at     = MAX_STREAM_DURATION_SEC - STREAM_WARN_BEFORE_SEC

            if elapsed_sec >= MAX_STREAM_DURATION_SEC:
                log.warning(
                    f"🛑 Hard timeout: stream={stream_key} elapsed={elapsed_min}min — force stopping"
                )
                await force_stop_stream(stream_key, reason="timeout")

            elif elapsed_sec >= warn_at:
                remaining = int((MAX_STREAM_DURATION_SEC - elapsed_sec) // 60)
                log.warning(
                    f"⚠️  Timeout warning: stream={stream_key} elapsed={elapsed_min}min "
                    f"— will force-stop in ~{remaining} min"
                )


async def ghost_stream_reconciler() -> None:
    """
    Runs every 30 seconds.
    Compares auth in-memory state vs SRS live streams.
    Removes orphaned entries (OBS dropped without triggering on_unpublish).
    """
    while True:
        await asyncio.sleep(30)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{SRS_API_URL}/api/v1/streams")

            if not resp.is_success:
                continue

            srs_live    = {s["name"] for s in resp.json().get("streams", [])}
            auth_active = set(active_streams.keys())
            ghosts      = auth_active - srs_live

            for ghost in ghosts:
                log.warning(f"👻 Ghost stream removed: {ghost} (in auth state but not in SRS)")
                active_streams.pop(ghost, None)

        except Exception as e:
            log.warning(f"Reconciler error (non-fatal): {e}")


async def dvr_disk_guard() -> None:
    """
    Runs every 5 minutes.
    Logs a warning when DVR disk usage exceeds 70% and an error above 85%.
    """
    import shutil
    while True:
        await asyncio.sleep(300)
        try:
            usage = shutil.disk_usage(DVR_PATH)
            pct   = usage.used / usage.total * 100
            free_gb = usage.free / 1024 ** 3

            if pct > 85:
                log.error(
                    f"🚨 DVR disk critical: {pct:.1f}% used, {free_gb:.1f} GB free — "
                    f"clean up {DVR_PATH} immediately"
                )
            elif pct > 70:
                log.warning(
                    f"⚠️  DVR disk warning: {pct:.1f}% used, {free_gb:.1f} GB free"
                )
        except Exception as e:
            log.warning(f"Disk guard error: {e}")
```

#### Change 5 — Start all background tasks in lifespan

In the `lifespan` function, replace the existing `cleanup_tokens` task section:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    db = get_db()
    try:
        seed_test_data(db)
    finally:
        db.close()

    await restore_active_streams()

    if RECORD_URL:
        log.info(f"📡 Recording platform push enabled → {RECORD_URL}")
    else:
        log.warning("⚠️  RECORD_URL not set — stream events will NOT be pushed")

    # Start all background tasks
    tasks = [
        asyncio.create_task(cleanup_tokens()),
        asyncio.create_task(stream_watchdog()),
        asyncio.create_task(ghost_stream_reconciler()),
        asyncio.create_task(dvr_disk_guard()),
    ]

    yield

    for t in tasks:
        t.cancel()
```

Keep the existing `cleanup_tokens` function as-is, just ensure all four tasks
are started and cancelled together.

### Verify After Implementation

**Test max 6 limit:**
1. Start 6 OBS streams simultaneously using stream-key-001 through stream-key-006
2. Attempt a 7th — auth should return `code: 403` and OBS shows connection refused
3. Check logs: `docker compose logs auth | grep "Max publisher"`

**Test timeout:**
1. Temporarily set `MAX_STREAM_DURATION_SEC = 120` (2 minutes for testing)
2. Start a stream, wait 2 minutes
3. Verify stream is force-kicked and `on_unpublish` fires in logs
4. Restore to `7200` after confirming

**Test ghost detection:**
1. Start a stream
2. `docker compose restart srs` while stream is running
3. Within 30 seconds the reconciler should log the ghost removal

---

*Created: 2026-04-13*
