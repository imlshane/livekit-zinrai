"""
SRS Auth Service
----------------
Handles all SRS HTTP callbacks and exposes management APIs.

SRS callbacks (POST):
  /on_publish    — validate stream key before allowing OBS to go live
  /on_unpublish  — mark stream inactive when OBS stops
  /on_play       — validate one-time viewer token before allowing playback
  /on_stop       — viewer disconnected
  /on_dvr        — DVR file written, trigger mp4 conversion + upload

Management APIs (GET/POST):
  /publishers          — list all publishers
  /publishers          — POST create publisher
  /token               — GET generate one-time viewer token
  /streams             — GET currently live streams
  /health              — GET health check
"""

import asyncio
import logging
import os
import secrets
import time
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import APIKeyHeader
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
from sqlalchemy import Boolean, Column, DateTime, Integer, String, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# ── Config ────────────────────────────────────────────────────────────────────

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:////data/auth.db")
SECRET_KEY   = os.environ.get("SECRET_KEY", "local-dev-secret")
DVR_PATH     = os.environ.get("DVR_PATH", "/dvr")
HLS_PATH     = os.environ.get("HLS_PATH", "/hls")
S3_ENDPOINT  = os.environ.get("S3_ENDPOINT", "")
S3_BUCKET    = os.environ.get("S3_BUCKET", "streams")
S3_ACCESS    = os.environ.get("S3_ACCESS_KEY", "")
S3_SECRET    = os.environ.get("S3_SECRET_KEY", "")

# Recordings platform — push stream events in real-time
RECORD_URL     = os.environ.get("RECORD_URL", "").rstrip("/")      # e.g. https://devstreamapp.zinrai.live
RECORD_API_KEY = os.environ.get("RECORD_API_KEY", "")              # x-api-key header value
SRS_API_URL    = os.environ.get("SRS_API_URL", "http://srs:1985")  # SRS internal HTTP API
REDIS_URL            = os.environ.get("REDIS_URL", "")
REDIS_PREFIX         = os.environ.get("REDIS_PREFIX", "zinrai:live:")
MANAGEMENT_API_KEY   = os.environ.get("MANAGEMENT_API_KEY", "")   # recording server + internal ops
VIEWER_TOKEN_API_KEY = os.environ.get("VIEWER_TOKEN_API_KEY", "")  # LMS / frontend token generation only

MAX_PUBLISHERS          = 6
MAX_STREAM_DURATION_SEC = 7200   # 2 hours — hard stop
STREAM_WARN_BEFORE_SEC  = 600    # warn 10 minutes before hard stop
TOKEN_TTL               = 7200   # seconds — matches 2h max stream duration
RECONNECT_GRACE_SEC     = int(os.environ.get("RECONNECT_GRACE_SEC", "30"))  # window to treat a reconnect as session resume

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── API Key Auth ───────────────────────────────────────────────────────────────

_api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)

async def require_api_key(key: str = Depends(_api_key_header)) -> None:
    """Dependency for internal management endpoints (recording server, ops)."""
    if not MANAGEMENT_API_KEY:
        return
    if key != MANAGEMENT_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

async def require_viewer_token_key(key: str = Depends(_api_key_header)) -> None:
    """Dependency for /token endpoint — separate key for LMS/frontend, rotatable independently."""
    active_key = VIEWER_TOKEN_API_KEY or MANAGEMENT_API_KEY  # fall back to management key if not set
    if not active_key:
        return
    if key != active_key:
        raise HTTPException(status_code=401, detail="Invalid or missing viewer token API key")

# ── Database ──────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass

class Publisher(Base):
    __tablename__ = "publishers"
    id         = Column(Integer, primary_key=True)
    username   = Column(String, unique=True, nullable=False)
    stream_key = Column(String, unique=True, nullable=False)
    enabled    = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, _):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()

SessionLocal = sessionmaker(bind=engine)

def get_db() -> Session:
    return SessionLocal()

def seed_test_data(db: Session):
    """Seed 3 test publishers for local development."""
    test_publishers = [
        {"username": "test-publisher-1", "stream_key": "stream-key-001"},
        {"username": "test-publisher-2", "stream_key": "stream-key-002"},
        {"username": "test-publisher-3", "stream_key": "stream-key-003"},
    ]
    for p in test_publishers:
        exists = db.query(Publisher).filter_by(username=p["username"]).first()
        if not exists:
            db.add(Publisher(**p))
    db.commit()
    log.info("Test publishers seeded (stream-key-001, stream-key-002, stream-key-003)")

# ── In-memory state ───────────────────────────────────────────────────────────

active_streams: dict[str, dict] = {}   # stream_key → {started_at, client_id, app, username}
viewer_tokens:  dict[str, dict] = {}   # token → {stream_key, viewer_id, used, expires_at}
active_viewers: dict[str, dict] = {}   # client_id → {stream_key, viewer_id, token, joined_at, ip_address}

# FIFO queue of final analytics per stream_key.
# Keyed by stream_key, each entry is a list so rapid restarts (same key within seconds)
# don't overwrite each other.  on_unpublish appends; on_dvr/convert_and_upload pops oldest.
stream_final_stats: dict[str, list[dict]] = {}

# Reconnect grace window state — stream_key → pending reconnect info.
# Populated by on_unpublish; cleared by reconnect (on_publish) or grace timer expiry.
pending_reconnect: dict[str, dict] = {}

# DVR chunk coordination — tracks converted MP4 parts for sessions with mid-stream drops.
session_dvr_chunks:  dict[str, list] = {}  # stream_key → [Path, ...]
session_dvr_pending: dict[str, int]  = {}  # stream_key → outstanding FLV conversions

# ── Redis ─────────────────────────────────────────────────────────────────────

redis_client = None   # set in lifespan if REDIS_URL is configured


async def init_redis() -> None:
    global redis_client
    if not REDIS_URL:
        log.warning("REDIS_URL not set — stream analytics will not be tracked in Redis")
        return
    try:
        import redis.asyncio as aioredis
        redis_client = aioredis.from_url(
            REDIS_URL, decode_responses=True, ssl_cert_reqs=None
        )
        await redis_client.ping()
        log.info(f"Redis connected: {REDIS_URL}")
    except Exception as e:
        log.error(f"Redis connection failed: {e} — continuing without Redis")
        redis_client = None


async def close_redis() -> None:
    global redis_client
    if redis_client:
        await redis_client.aclose()
        redis_client = None


async def r_stream_start(stream_key: str, client_id: str, username: str) -> None:
    if not redis_client:
        return
    try:
        now = time.time()
        pipe = redis_client.pipeline()
        pipe.set(f"{REDIS_PREFIX}stream:{stream_key}:status",     "live")
        pipe.set(f"{REDIS_PREFIX}stream:{stream_key}:started_at", now)
        pipe.set(f"{REDIS_PREFIX}stream:{stream_key}:client_id",  client_id)
        pipe.set(f"{REDIS_PREFIX}stream:{stream_key}:username",   username)
        pipe.set(f"{REDIS_PREFIX}stream:{stream_key}:views",      0)
        pipe.set(f"{REDIS_PREFIX}stream:{stream_key}:watch_seconds", 0)
        pipe.delete(f"{REDIS_PREFIX}stream:{stream_key}:unique_viewers")
        pipe.delete(f"{REDIS_PREFIX}stream:{stream_key}:sessions")
        await pipe.execute()
    except Exception as e:
        log.warning(f"Redis r_stream_start failed: {e}")


async def r_stream_end(stream_key: str) -> dict:
    """Reads final analytics, marks status=ended. Returns stats dict."""
    if not redis_client:
        return {"started_at": time.time(), "total_views": 0, "unique_viewers": 0, "total_watch_seconds": 0}
    try:
        pipe = redis_client.pipeline()
        pipe.get(f"{REDIS_PREFIX}stream:{stream_key}:started_at")
        pipe.get(f"{REDIS_PREFIX}stream:{stream_key}:views")
        pipe.get(f"{REDIS_PREFIX}stream:{stream_key}:watch_seconds")
        pipe.pfcount(f"{REDIS_PREFIX}stream:{stream_key}:unique_viewers")
        results = await pipe.execute()

        await redis_client.set(f"{REDIS_PREFIX}stream:{stream_key}:status", "ended")

        stats = {
            "started_at":          float(results[0] or time.time()),
            "total_views":         int(results[1] or 0),
            "total_watch_seconds": int(results[2] or 0),
            "unique_viewers":      int(results[3] or 0),
        }
        log.info(f"r_stream_end {stream_key}: views={stats['total_views']} unique={stats['unique_viewers']} watch={stats['total_watch_seconds']}s")
        return stats
    except Exception as e:
        log.warning(f"Redis r_stream_end failed: {e}")
        return {"started_at": time.time(), "total_views": 0, "unique_viewers": 0, "total_watch_seconds": 0}


async def r_stream_stats_only(stream_key: str) -> dict:
    """Read current analytics without marking the stream as ended (used during grace window)."""
    if not redis_client:
        return {"started_at": time.time(), "total_views": 0, "unique_viewers": 0, "total_watch_seconds": 0}
    try:
        pipe = redis_client.pipeline()
        pipe.get(f"{REDIS_PREFIX}stream:{stream_key}:started_at")
        pipe.get(f"{REDIS_PREFIX}stream:{stream_key}:views")
        pipe.get(f"{REDIS_PREFIX}stream:{stream_key}:watch_seconds")
        pipe.pfcount(f"{REDIS_PREFIX}stream:{stream_key}:unique_viewers")
        results = await pipe.execute()
        return {
            "started_at":          float(results[0] or time.time()),
            "total_views":         int(results[1] or 0),
            "total_watch_seconds": int(results[2] or 0),
            "unique_viewers":      int(results[3] or 0),
        }
    except Exception as e:
        log.warning(f"r_stream_stats_only failed: {e}")
        return {"started_at": time.time(), "total_views": 0, "unique_viewers": 0, "total_watch_seconds": 0}


async def r_stream_resume(stream_key: str, client_id: str, username: str, original_started_at: float) -> None:
    """Re-initialise Redis live state for a reconnected stream, preserving the original start time
    and accumulated analytics counters."""
    if not redis_client:
        return
    try:
        pipe = redis_client.pipeline()
        pipe.set(f"{REDIS_PREFIX}stream:{stream_key}:status",     "live")
        pipe.set(f"{REDIS_PREFIX}stream:{stream_key}:started_at", original_started_at)
        pipe.set(f"{REDIS_PREFIX}stream:{stream_key}:client_id",  client_id)
        pipe.set(f"{REDIS_PREFIX}stream:{stream_key}:username",   username)
        # views / watch_seconds / unique_viewers are left intact — they accumulated from before
        await pipe.execute()
    except Exception as e:
        log.warning(f"Redis r_stream_resume failed: {e}")


async def r_viewer_join(stream_key: str, viewer_id: str, client_id: str, is_reconnect: bool = False) -> None:
    if not redis_client:
        return
    try:
        pipe = redis_client.pipeline()
        if not is_reconnect:
            pipe.incr(f"{REDIS_PREFIX}stream:{stream_key}:views")
        pipe.pfadd(f"{REDIS_PREFIX}stream:{stream_key}:unique_viewers", viewer_id)
        pipe.hset(f"{REDIS_PREFIX}stream:{stream_key}:sessions", client_id, time.time())
        await pipe.execute()
    except Exception as e:
        log.warning(f"Redis r_viewer_join failed: {e}")


async def r_viewer_leave(stream_key: str, client_id: str, watch_seconds: int) -> None:
    if not redis_client:
        return
    try:
        pipe = redis_client.pipeline()
        pipe.incrby(f"{REDIS_PREFIX}stream:{stream_key}:watch_seconds", watch_seconds)
        pipe.hdel(f"{REDIS_PREFIX}stream:{stream_key}:sessions", client_id)
        await pipe.execute()
    except Exception as e:
        log.warning(f"Redis r_viewer_leave failed: {e}")


# ── Recordings Platform Push ──────────────────────────────────────────────────

async def push_event(path: str, payload: dict, retries: int = 3) -> None:
    """
    Fire-and-forget POST to the recordings platform.
    Retries up to `retries` times with exponential backoff.
    Never raises — failures are logged but don't block SRS callbacks.
    """
    if not RECORD_URL or not RECORD_API_KEY:
        return

    url = f"{RECORD_URL}{path}"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": RECORD_API_KEY,
    }

    for attempt in range(1, retries + 1):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.is_success:
                    log.info(f"✅ push_event {path} → {resp.status_code}")
                    return
                log.warning(f"⚠️  push_event {path} attempt {attempt}: HTTP {resp.status_code} — {resp.text[:200]}")
        except Exception as e:
            log.warning(f"⚠️  push_event {path} attempt {attempt}: {e}")

        if attempt < retries:
            await asyncio.sleep(2 ** attempt)  # 2s, 4s backoff

    log.error(f"❌ push_event {path} failed after {retries} attempts")


def fire(path: str, payload: dict) -> None:
    """Schedule push_event as a background task (non-blocking)."""
    asyncio.create_task(push_event(path, payload))


async def _register_video_task(stream_key: str, username: str, educator_id: Optional[str], app_name: str) -> None:
    """Register a new Video record in the recordings platform and cache the video_id in Redis."""
    if not RECORD_URL or not RECORD_API_KEY:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{RECORD_URL}/stream/register",
                json={
                    "stream_key":    stream_key,
                    "educator_name": username,
                    "educator_id":   educator_id,
                    "app":           app_name,
                    "started_at":    time.time(),
                },
                headers={"x-api-key": RECORD_API_KEY, "Content-Type": "application/json"},
            )
        if resp.is_success:
            video_id = resp.json().get("video_id")
            if video_id and redis_client:
                await redis_client.set(f"{REDIS_PREFIX}stream:{stream_key}:video_id", video_id, ex=86400)
                await redis_client.set(f"{REDIS_PREFIX}video:{video_id}:stream_key", stream_key, ex=86400)
            log.info(f"Video registered: {video_id} for stream {stream_key}")
        else:
            log.warning(f"Video registration failed: HTTP {resp.status_code} — {resp.text[:200]}")
    except Exception as e:
        log.error(f"Video registration error for {stream_key}: {e}")


async def _grace_window_expire(stream_key: str) -> None:
    """
    Fires RECONNECT_GRACE_SEC after an on_unpublish.
    If the publisher hasn't reconnected by then, the session is truly over:
    finalise Redis state, push sessions/end, and kick off chunk assembly if all
    DVR conversions are already done.
    """
    await asyncio.sleep(RECONNECT_GRACE_SEC)

    if stream_key not in pending_reconnect:
        return  # reconnect already cancelled us

    pending_reconnect.pop(stream_key, None)

    final_stats = await r_stream_end(stream_key)
    duration_s  = int(time.time() - final_stats["started_at"])

    stream_final_stats.setdefault(stream_key, []).append({
        "total_views":         final_stats["total_views"],
        "unique_viewers":      final_stats["unique_viewers"],
        "total_watch_seconds": final_stats["total_watch_seconds"],
        "started_at":          final_stats["started_at"],
    })

    fire("/stream-analytics/sessions/end", {
        "stream_key":          stream_key,
        "total_views":         final_stats["total_views"],
        "unique_viewers":      final_stats["unique_viewers"],
        "total_watch_seconds": final_stats["total_watch_seconds"],
        "peak_concurrent":     0,
    })

    log.info(
        f"Grace window expired — session finalised: {stream_key} "
        f"duration={duration_s}s views={final_stats['total_views']}"
    )

    # If all DVR conversions already completed while we were in the grace window,
    # assemble the final MP4 now (no more on_dvr will arrive).
    if session_dvr_pending.get(stream_key, 0) == 0 and stream_key in session_dvr_chunks:
        log.info(f"All DVR chunks pre-converted — assembling now: {stream_key}")
        asyncio.create_task(_assemble_and_finalise(stream_key))


async def concat_mp4_parts(parts: list, output: Path) -> bool:
    """Concatenate MP4 files using the ffmpeg concat demuxer. Returns True on success."""
    concat_list = output.parent / f"_concat_{int(time.time())}.txt"
    try:
        concat_list.write_text("\n".join(f"file '{p.resolve()}'" for p in parts) + "\n")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy", "-movflags", "+faststart",
            str(output),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error(f"concat_mp4_parts failed: {stderr.decode()[-500:]}")
            return False
        log.info(f"Concatenated {len(parts)} parts → {output.name}")
        return True
    except Exception as e:
        log.error(f"concat_mp4_parts error: {e}")
        return False
    finally:
        concat_list.unlink(missing_ok=True)


async def _assemble_and_finalise(stream_key: str) -> None:
    """
    Concat all stashed DVR chunks for stream_key into one {video_id}.mp4,
    then push /stream/finalize.  Called either from convert_and_upload (when the
    last pending conversion finishes) or from _grace_window_expire (when all
    conversions beat the grace timer).
    """
    chunks = session_dvr_chunks.pop(stream_key, [])
    session_dvr_pending.pop(stream_key, None)

    if not chunks:
        return

    video_id = None
    if redis_client:
        try:
            video_id = await redis_client.get(f"{REDIS_PREFIX}stream:{stream_key}:video_id")
        except Exception:
            pass

    base_dir = chunks[0].parent

    if len(chunks) == 1:
        mp4 = chunks[0]
    else:
        final_mp4 = base_dir / f"{video_id or stream_key}-assembled.mp4"
        ok = await concat_mp4_parts(chunks, final_mp4)
        if ok:
            for c in chunks:
                c.unlink(missing_ok=True)
            mp4 = final_mp4
        else:
            mp4 = chunks[-1]
            for c in chunks[:-1]:
                c.unlink(missing_ok=True)

    if video_id:
        named_mp4 = mp4.parent / f"{video_id}.mp4"
        mp4.rename(named_mp4)
        mp4 = named_mp4
        log.info(f"Assembled MP4 renamed to {mp4.name}")
    else:
        log.warning(f"No video_id in Redis for {stream_key} — assembled file stays as {mp4.name}")

    try:
        generate_vod_m3u8(stream_key)
    except Exception as e:
        log.warning(f"VOD m3u8 generation failed for {stream_key}: {e}")

    # Wait for stats (stashed by _grace_window_expire or _grace_window_expire→on_unpublish)
    for _ in range(10):
        if stream_key in stream_final_stats:
            break
        await asyncio.sleep(0.5)

    queue = stream_final_stats.get(stream_key, [])
    session_stats = queue.pop(0) if queue else {}
    if not queue:
        stream_final_stats.pop(stream_key, None)

    if video_id and RECORD_URL:
        asyncio.create_task(push_event(f"/stream/finalize/{video_id}", {
            "total_views":         session_stats.get("total_views", 0),
            "unique_viewers":      session_stats.get("unique_viewers", 0),
            "total_watch_seconds": session_stats.get("total_watch_seconds", 0),
            "started_at":          session_stats.get("started_at"),
        }))
    elif not RECORD_URL:
        log.info(f"RECORD_URL not set — skipping finalize for {stream_key}")


def generate_vod_m3u8(stream_key: str, app: str = "live") -> Optional[Path]:
    """Build a complete VOD m3u8 from all .ts files and save it in the same HLS dir.

    SRS stores HLS files flat: {HLS_PATH}/{app}/{stream_key}-{seq}.ts
    (no per-stream subdirectory).
    """
    hls_dir = Path(HLS_PATH) / app
    if not hls_dir.exists():
        return None
    ts_files = sorted(
        hls_dir.glob(f"{stream_key}-*.ts"),
        key=lambda f: int(f.stem.rsplit("-", 1)[1]),
    )
    if not ts_files:
        return None
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-TARGETDURATION:2",
    ]
    for ts in ts_files:
        lines.append("#EXTINF:2.000,")
        lines.append(ts.name)
    lines.append("#EXT-X-ENDLIST")
    vod = hls_dir / f"{stream_key}-vod.m3u8"
    vod.write_text("\n".join(lines) + "\n")
    log.info(f"VOD m3u8 generated: {vod} ({len(ts_files)} segments)")
    return vod


async def restore_active_streams() -> None:
    """
    On startup, query the SRS API to rebuild active_streams from live publisher clients.
    Prevents zero-state after an auth service restart while streams are running.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            resp = await client.get(f"{SRS_API_URL}/api/v1/clients/")
            if not resp.is_success:
                log.warning(f"⚠️  Could not query SRS API for state recovery: HTTP {resp.status_code}")
                return

            data = resp.json()
            clients = data.get("clients", [])

        db = get_db()
        try:
            restored = 0
            for c in clients:
                if not c.get("publish"):
                    continue  # skip viewers, only care about publishers

                stream_key = c.get("name", "")
                client_id  = c.get("id", "")
                if not stream_key or stream_key in active_streams:
                    continue

                # Look up publisher username from DB
                publisher = db.query(Publisher).filter_by(stream_key=stream_key, enabled=True).first()
                if not publisher:
                    continue

                active_streams[stream_key] = {
                    "started_at": time.time(),  # exact start unknown after restart
                    "client_id":  client_id,
                    "app":        "live",
                    "username":   publisher.username,
                }
                log.info(f"♻️  Restored stream: {stream_key} by {publisher.username} (client={client_id})")
                restored += 1
        finally:
            db.close()

        if restored:
            log.info(f"♻️  State recovery complete — {restored} active stream(s) restored")
        else:
            log.info("♻️  State recovery: no active publisher streams found in SRS")

    except Exception as e:
        log.warning(f"⚠️  State recovery failed (non-fatal): {e}")


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    db = get_db()
    try:
        seed_test_data(db)
    finally:
        db.close()

    await init_redis()

    # Restore active_streams from SRS API (handles auth restarts while streams are live)
    await restore_active_streams()

    if MANAGEMENT_API_KEY:
        log.info("Management API key configured — internal endpoints are protected")
    else:
        log.warning("MANAGEMENT_API_KEY not set — management endpoints are unprotected")

    if VIEWER_TOKEN_API_KEY:
        log.info("Viewer token API key configured — /token endpoint uses separate key")
    else:
        log.warning("VIEWER_TOKEN_API_KEY not set — /token falls back to MANAGEMENT_API_KEY")

    if RECORD_URL:
        log.info(f"📡 Recording platform push enabled → {RECORD_URL}")
    else:
        log.warning("⚠️  RECORD_URL not set — stream events will NOT be pushed to recording platform")

    # Background: clean up expired tokens every 5 minutes
    async def cleanup_tokens():
        while True:
            await asyncio.sleep(300)
            now = time.time()
            to_remove = [
                t for t, v in viewer_tokens.items()
                if v["expires_at"] < now                                      # naturally expired
                or (v.get("used") and (now - (v.get("used_at") or 0)) > 10800)  # used + 3h retention
            ]
            for t in to_remove:
                del viewer_tokens[t]
            if to_remove:
                log.info(f"Cleaned up {len(to_remove)} tokens ({sum(1 for t in to_remove if viewer_tokens.get(t, {}).get('used'))} used, rest expired)")

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
    await close_redis()

app = FastAPI(title="SRS Auth Service", lifespan=lifespan)

# ── SRS Callback helpers ──────────────────────────────────────────────────────

def srs_ok():
    """SRS expects 0 = allow, non-zero = deny."""
    return JSONResponse({"code": 0, "data": None})

def srs_deny(reason: str):
    log.warning(f"Denied: {reason}")
    return JSONResponse({"code": 403, "data": reason}, status_code=200)
    # Note: SRS reads the `code` field, not HTTP status, for allow/deny.
    # Return HTTP 200 always; SRS checks response body code != 0 to deny.


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
            log.info(f"Force-stopped stream={stream_key} client={client_id} reason={reason}")
        else:
            log.warning(f"force_stop: SRS returned {resp.status_code} for client {client_id}")

    except Exception as e:
        log.error(f"force_stop failed for {stream_key}: {e}")
        # Remove from state anyway so the slot is freed
        active_streams.pop(stream_key, None)


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
                    f"Hard timeout: stream={stream_key} elapsed={elapsed_min}min — force stopping"
                )
                await force_stop_stream(stream_key, reason="timeout")

            elif elapsed_sec >= warn_at:
                remaining = int((MAX_STREAM_DURATION_SEC - elapsed_sec) // 60)
                log.warning(
                    f"Timeout warning: stream={stream_key} elapsed={elapsed_min}min "
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
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
                resp = await client.get(f"{SRS_API_URL}/api/v1/streams/")

            if not resp.is_success:
                continue

            srs_live    = {s["name"] for s in resp.json().get("streams", [])}
            auth_active = set(active_streams.keys())
            ghosts      = auth_active - srs_live

            for ghost in ghosts:
                # on_unpublish already moved this into pending_reconnect — not a ghost
                if ghost in pending_reconnect:
                    continue
                log.warning(f"Ghost stream removed: {ghost} (in auth state but not in SRS)")
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
            usage   = shutil.disk_usage(DVR_PATH)
            pct     = usage.used / usage.total * 100
            free_gb = usage.free / 1024 ** 3

            if pct > 85:
                log.error(
                    f"DVR disk critical: {pct:.1f}% used, {free_gb:.1f} GB free — "
                    f"clean up {DVR_PATH} immediately"
                )
            elif pct > 70:
                log.warning(
                    f"DVR disk warning: {pct:.1f}% used, {free_gb:.1f} GB free"
                )
        except Exception as e:
            log.warning(f"Disk guard error: {e}")


# ── Educator stream-key validation ────────────────────────────────────────────

async def lookup_educator_by_stream_key(stream_key: str) -> Optional[dict]:
    """
    GET {RECORD_URL}/educators/by-stream-key/{stream_key} using the configured API key.
    Returns the educator dict on success, or None if not found / inactive / unreachable.
    Falls back to None (caller should check local DB) when RECORD_URL is not set.
    """
    if not RECORD_URL or not RECORD_API_KEY:
        return None

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{RECORD_URL}/educators/by-stream-key/{stream_key}",
                headers={"accept": "application/json", "x-api-key": RECORD_API_KEY},
            )
        if resp.status_code == 200:
            return resp.json()   # full educator object
        if resp.status_code == 404:
            log.warning(f"lookup_educator: stream_key '{stream_key}' not found in recordings platform")
        else:
            log.warning(f"lookup_educator: unexpected status {resp.status_code} for '{stream_key}'")
    except Exception as e:
        log.error(f"lookup_educator: request failed for '{stream_key}': {e}")

    return None


# ── SRS Callbacks ─────────────────────────────────────────────────────────────

@app.post("/on_publish")
async def on_publish(request: Request):
    """
    SRS fires this when OBS connects to push an RTMP stream.
    Validates the stream key against the recordings platform (educator must exist and be active).
    Falls back to local Publisher DB when RECORD_URL is not configured.
    """
    body = await request.json()
    stream_key = body.get("stream", "")
    client_id  = body.get("client_id", "")
    app_name   = body.get("app", "live")

    log.info(f"on_publish: stream={stream_key} client={client_id}")

    # ── Validate stream key against recordings platform ──────────────────────
    if not RECORD_URL or not RECORD_API_KEY:
        return srs_deny("Recordings platform not configured — all streams are blocked")

    educator = await lookup_educator_by_stream_key(stream_key)
    if not educator:
        return srs_deny(f"Stream key not registered in recordings platform: {stream_key}")
    if not educator.get("is_active", False):
        return srs_deny(f"Educator is inactive, stream not allowed: {stream_key}")

    username = educator.get("name", stream_key)
    log.info(f"Educator validated: '{username}' (id={educator.get('id')})")

    # ── Reconnect within grace window — resume the same session ─────────────────
    recon = pending_reconnect.pop(stream_key, None)
    if recon:
        recon["grace_task"].cancel()

        active_streams[stream_key] = {
            "started_at": recon["started_at"],   # preserve original session start time
            "client_id":  client_id,
            "app":        app_name,
            "username":   username,
            "event_id":   educator.get("event_id"),
        }

        # Increment pending DVR count — this new connection will produce another FLV file
        session_dvr_pending[stream_key] = session_dvr_pending.get(stream_key, 0) + 1

        asyncio.create_task(r_stream_resume(stream_key, client_id, username, recon["started_at"]))

        log.info(
            f"Stream RECONNECTED (grace window): {stream_key} by '{username}' "
            f"— reusing session, video_id={recon.get('video_id')}"
        )
        return srs_ok()

    # ── New session ───────────────────────────────────────────────────────────────
    if stream_key in active_streams:
        return srs_deny(f"Stream key already in use: {stream_key}")

    if len(active_streams) >= MAX_PUBLISHERS:
        return srs_deny(f"Max publisher limit ({MAX_PUBLISHERS}) reached")

    # Clear any stale DVR state from a previous (fully ended) session with the same key
    session_dvr_pending.pop(stream_key, None)
    session_dvr_chunks.pop(stream_key, None)

    active_streams[stream_key] = {
        "started_at": time.time(),
        "client_id":  client_id,
        "app":        app_name,
        "username":   username,
        "event_id":   educator.get("event_id"),
    }
    # Track that this initial connection will produce one DVR file
    session_dvr_pending[stream_key] = 1

    log.info(f"Stream started: {stream_key} by '{username}' ({len(active_streams)} active)")

    asyncio.create_task(r_stream_start(stream_key, client_id, username))

    # Cache event_id → stream_key so /stream-token can resolve without a DB call
    if redis_client and educator.get("event_id"):
        asyncio.create_task(
            redis_client.set(f"{REDIS_PREFIX}event:{educator['event_id']}:stream_key", stream_key, ex=TOKEN_TTL)
        )

    # Register a Video record in recordings platform — video_id cached in Redis for on_dvr
    if RECORD_URL:
        asyncio.create_task(_register_video_task(stream_key, username, educator.get("id"), app_name))

    # Push stream.started to recordings platform (non-blocking)
    fire("/stream-analytics/sessions/start", {
        "stream_key":    stream_key,
        "srs_client_id": client_id,
    })

    return srs_ok()


@app.post("/on_unpublish")
async def on_unpublish(request: Request):
    """OBS stopped streaming (or dropped due to network instability)."""
    body = await request.json()
    stream_key = body.get("stream", "")
    log.info(f"on_unpublish: stream={stream_key}")

    active_streams.pop(stream_key, {})

    # Close any still-open viewer sessions in memory (abrupt exits)
    abrupt_exits = [cid for cid, v in list(active_viewers.items()) if v["stream_key"] == stream_key]
    for cid in abrupt_exits:
        active_viewers.pop(cid, None)

    # Snapshot current analytics without marking the session ended — the publisher
    # may reconnect within the grace window and we want to preserve the session.
    stats = await r_stream_stats_only(stream_key)

    # Look up the video_id so a reconnect can reuse it without a new /stream/register call.
    video_id = None
    if redis_client:
        try:
            video_id = await redis_client.get(f"{REDIS_PREFIX}stream:{stream_key}:video_id")
        except Exception:
            pass

    grace_task = asyncio.create_task(_grace_window_expire(stream_key))
    pending_reconnect[stream_key] = {
        "video_id":   video_id,
        "started_at": stats["started_at"],
        "stats":      stats,
        "grace_task": grace_task,
    }

    log.info(
        f"Stream disconnected: {stream_key} — "
        f"grace window {RECONNECT_GRACE_SEC}s started (video_id={video_id})"
    )
    return srs_ok()


@app.post("/on_play")
async def on_play(request: Request):
    """
    SRS fires this when a viewer starts playing (WebRTC).
    Validate the one-time token passed as ?token=xxx in the stream URL.
    """
    body      = await request.json()
    stream_key = body.get("stream", "")
    param      = body.get("param", "")   # e.g. "?token=abc123"
    client_id  = body.get("client_id", "")
    ip_address = body.get("ip", "")

    log.info(f"on_play: stream={stream_key} param={param} client={client_id}")

    # Parse token and browser session id from query string
    qs    = urllib.parse.parse_qs(param.lstrip("?"))
    token = qs.get("token", [None])[0]
    sid   = qs.get("sid",   [None])[0]

    if not token:
        return srs_deny("Missing viewer token")

    entry = viewer_tokens.get(token)
    if not entry:
        return srs_deny("Invalid token")

    if entry["expires_at"] < time.time():
        viewer_tokens.pop(token, None)
        return srs_deny("Token expired")

    if entry["stream_key"] and entry["stream_key"] != stream_key:
        return srs_deny(f"Token not valid for stream {stream_key}")

    is_reconnect = False
    if entry.get("used"):
        # Allow reconnects from the same browser tab (same IP + same session id)
        bound_ip  = entry.get("bound_ip")
        bound_sid = entry.get("bound_sid")
        if ip_address != bound_ip:
            log.warning(f"on_play DENIED: token reuse from different IP {ip_address} (bound to {bound_ip})")
            return srs_deny("Token already claimed by another viewer")
        if sid and bound_sid and sid != bound_sid:
            log.warning(f"on_play DENIED: token reuse from different session {sid} (bound to {bound_sid})")
            return srs_deny("Token already claimed by another session")
        is_reconnect = True
    else:
        # First use — bind token to this IP and browser session
        entry["used"]      = True
        entry["used_at"]   = time.time()
        entry["bound_ip"]  = ip_address
        entry["bound_sid"] = sid

    viewer_id    = entry.get("viewer_id") or f"anon-{client_id}"
    is_anonymous = not bool(entry.get("viewer_id"))

    # Track in memory for watch_seconds calculation on on_stop
    active_viewers[client_id] = {
        "stream_key":  stream_key,
        "viewer_id":   viewer_id,
        "token":       token,
        "joined_at":   time.time(),
        "ip_address":  ip_address,
    }

    log.info(f"Viewer {'reconnected' if is_reconnect else 'authenticated'}: stream={stream_key} viewer={viewer_id} client={client_id} ip={ip_address}")

    await r_viewer_join(stream_key, viewer_id, client_id, is_reconnect=is_reconnect)

    # Push viewer.joined to recordings platform (non-blocking)
    fire("/stream-analytics/viewers/join", {
        "stream_key":  stream_key,
        "viewer_id":   viewer_id,
        "is_anonymous": is_anonymous,
        "token":       token,
        "ip_address":  ip_address or None,
        "user_agent":  None,  # not available from SRS callbacks
    })

    return srs_ok()


@app.post("/on_stop")
async def on_stop(request: Request):
    """Viewer disconnected — record watch time."""
    body      = await request.json()
    stream_key = body.get("stream", "")
    client_id  = body.get("client_id", "")

    log.info(f"on_stop: stream={stream_key} client={client_id}")

    viewer = active_viewers.pop(client_id, None)
    if viewer:
        watch_seconds = int(time.time() - viewer["joined_at"])
        viewer_id     = viewer["viewer_id"]

        log.info(f"Viewer left: stream={stream_key} viewer={viewer_id} watched={watch_seconds}s")

        asyncio.create_task(r_viewer_leave(stream_key, client_id, watch_seconds))

        # Push viewer.left to recordings platform (non-blocking)
        fire("/stream-analytics/viewers/leave", {
            "stream_key":   stream_key,
            "viewer_id":    viewer_id,
            "watch_seconds": watch_seconds,
        })

    return srs_ok()


@app.post("/on_dvr")
async def on_dvr(request: Request):
    """
    SRS fires this when a DVR session file is complete (.flv).
    Trigger async conversion to .mp4 and upload to S3/R2.
    """
    body     = await request.json()
    flv_path = body.get("file", "")
    stream   = body.get("stream", "")
    log.info(f"on_dvr: stream={stream} file={flv_path}")

    if flv_path:
        asyncio.create_task(convert_and_upload(flv_path, stream))

    return srs_ok()

# ── DVR Worker ────────────────────────────────────────────────────────────────

async def convert_and_upload(flv_path: str, stream_key: str):
    """
    Convert .flv → .mp4 via ffmpeg.

    For sessions with network drops the same stream_key may produce multiple FLV
    files (one per RTMP connection).  We stash each converted chunk and only
    concat + finalise once the session is truly over (grace window expired and all
    pending conversions are done).
    """
    flv = Path(flv_path)
    if not flv.exists():
        log.error(f"DVR file not found: {flv_path}")
        return

    mp4 = flv.with_suffix(".mp4")
    log.info(f"Converting {flv} → {mp4}")

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(flv),
            "-c:v", "copy", "-c:a", "copy",
            "-movflags", "+faststart",
            str(mp4),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error(f"ffmpeg failed: {stderr.decode()[-500:]}")
            return
        log.info(f"Conversion done: {mp4} ({mp4.stat().st_size // 1024 // 1024} MB)")
        flv.unlink()
    except FileNotFoundError:
        log.warning("ffmpeg not installed — skipping conversion. Install ffmpeg in auth container.")
        return

    # Decrement the outstanding-conversion counter for this logical session
    remaining = max(0, session_dvr_pending.get(stream_key, 1) - 1)
    session_dvr_pending[stream_key] = remaining

    # If the session is still live or in the grace window, stash this chunk.
    # _grace_window_expire / the final convert_and_upload call will assemble everything.
    session_active = stream_key in active_streams or stream_key in pending_reconnect

    if session_active or remaining > 0:
        chunk_name = mp4.parent / f"{mp4.stem}-part{int(time.time())}.mp4"
        mp4.rename(chunk_name)
        session_dvr_chunks.setdefault(stream_key, []).append(chunk_name)
        log.info(
            f"DVR chunk stashed ({remaining} conversion(s) still pending, "
            f"session_active={session_active}): {chunk_name.name}"
        )
        return

    # Session is fully over and this is the last conversion — assemble now.
    prior_chunks = session_dvr_chunks.pop(stream_key, [])
    session_dvr_pending.pop(stream_key, None)
    all_parts = prior_chunks + [mp4]

    if len(all_parts) > 1:
        video_id_for_name = None
        if redis_client:
            try:
                video_id_for_name = await redis_client.get(f"{REDIS_PREFIX}stream:{stream_key}:video_id")
            except Exception:
                pass
        final_mp4 = mp4.parent / f"{video_id_for_name or stream_key}-assembled.mp4"
        ok = await concat_mp4_parts(all_parts, final_mp4)
        if ok:
            for part in all_parts:
                part.unlink(missing_ok=True)
            mp4 = final_mp4
        else:
            log.warning(f"MP4 concat failed for {stream_key} — using last chunk only")
            for part in prior_chunks:
                part.unlink(missing_ok=True)

    # Rename to {video_id}.mp4
    video_id = None
    if redis_client:
        try:
            video_id = await redis_client.get(f"{REDIS_PREFIX}stream:{stream_key}:video_id")
        except Exception:
            pass
    if video_id:
        named_mp4 = mp4.parent / f"{video_id}.mp4"
        mp4.rename(named_mp4)
        mp4 = named_mp4
        log.info(f"Renamed to {mp4.name}")
    else:
        log.warning(f"No video_id in Redis for {stream_key} — file stays as {mp4.name}")

    # Generate a complete VOD m3u8 — non-fatal if HLS dir is empty or write fails
    try:
        generate_vod_m3u8(stream_key)
    except Exception as e:
        log.warning(f"VOD m3u8 generation failed for {stream_key}: {e}")

    # Pop analytics stashed by _grace_window_expire.
    # Grace window expiry sets stream_final_stats; on_dvr fires after on_unpublish so
    # the stats may not be ready yet — wait up to RECONNECT_GRACE_SEC + 5s.
    wait_limit = RECONNECT_GRACE_SEC + 5
    waited = 0
    while stream_key not in stream_final_stats and waited < wait_limit:
        await asyncio.sleep(0.5)
        waited += 0.5
    queue = stream_final_stats.get(stream_key, [])
    session_stats = queue.pop(0) if queue else {}
    if not queue:
        stream_final_stats.pop(stream_key, None)
    if not session_stats:
        log.warning(f"convert_and_upload {stream_key}: stream_final_stats empty — stats will be 0")

    # Finalize the Video record — recordings worker will pick it up and upload
    if video_id and RECORD_URL:
        asyncio.create_task(push_event(f"/stream/finalize/{video_id}", {
            "total_views":         session_stats.get("total_views", 0),
            "unique_viewers":      session_stats.get("unique_viewers", 0),
            "total_watch_seconds": session_stats.get("total_watch_seconds", 0),
            "started_at":          session_stats.get("started_at"),
        }))
    elif not RECORD_URL:
        log.info(f"RECORD_URL not set — skipping finalize for {stream_key}")

async def upload_hls_to_r2(stream_key: str, app: str = "live") -> Optional[str]:
    """Upload all HLS segments + m3u8 for a stream to R2. Returns the m3u8 key, or None on failure."""
    hls_dir = Path(HLS_PATH) / app / stream_key
    if not hls_dir.exists():
        log.warning(f"HLS dir not found: {hls_dir}")
        return None

    files = list(hls_dir.glob("*.ts")) + list(hls_dir.glob("*.m3u8"))
    if not files:
        log.warning(f"No HLS files found in {hls_dir}")
        return None

    try:
        import boto3
        from botocore.config import Config

        s3 = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            aws_access_key_id=S3_ACCESS,
            aws_secret_access_key=S3_SECRET,
            config=Config(signature_version="s3v4"),
        )
        prefix = f"hls-live/{stream_key}"
        for f in files:
            key = f"{prefix}/{f.name}"
            content_type = "application/vnd.apple.mpegurl" if f.suffix == ".m3u8" else "video/mp2t"
            s3.upload_file(str(f), S3_BUCKET, key, ExtraArgs={"ContentType": content_type})

        m3u8_key = f"{prefix}/{stream_key}.m3u8"
        log.info(f"HLS uploaded to R2: {prefix}/ ({len(files)} files)")
        return m3u8_key
    except Exception as e:
        log.error(f"HLS R2 upload failed for {stream_key}: {e}")
        return None


async def upload_to_r2(mp4: Path, stream_key: str) -> Optional[str]:
    """Upload mp4 to R2. Returns the R2 object key, or None on failure."""
    try:
        import boto3
        from botocore.config import Config

        s3 = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            aws_access_key_id=S3_ACCESS,
            aws_secret_access_key=S3_SECRET,
            config=Config(signature_version="s3v4"),
        )
        key = f"recordings/{stream_key}/{mp4.name}"
        log.info(f"Uploading {mp4.name} → r2://{S3_BUCKET}/{key}")
        s3.upload_file(str(mp4), S3_BUCKET, key)
        log.info(f"R2 upload complete: {key}")
        mp4.unlink()   # delete local file after upload
        return key
    except Exception as e:
        log.error(f"R2 upload failed: {e}")
        return None

# ── File serving for recordings worker ───────────────────────────────────────

@app.get("/dvr-files/{video_id}")
async def serve_dvr_file(video_id: str, request: Request):
    """Serve the converted MP4 for a stream session. Protected by management API key."""
    if request.headers.get("x-api-key") != MANAGEMENT_API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    for search_dir in [Path(DVR_PATH) / "live", Path(DVR_PATH)]:
        f = search_dir / f"{video_id}.mp4"
        if f.exists():
            return FileResponse(str(f), media_type="video/mp4", filename=f"{video_id}.mp4")
    raise HTTPException(status_code=404, detail="DVR file not found")


@app.get("/hls-archive/{stream_key}/{filename}")
async def serve_hls_file(stream_key: str, filename: str, request: Request):
    """Serve a single HLS segment or m3u8 for archival upload. Protected by management API key.

    SRS stores files flat: {HLS_PATH}/live/{stream_key}-{seq}.ts (no subdirectory per stream).
    The stream_key path segment is for URL namespacing only.
    """
    if request.headers.get("x-api-key") != MANAGEMENT_API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    f = Path(HLS_PATH) / "live" / filename
    if not f.exists():
        raise HTTPException(status_code=404, detail="HLS file not found")
    ct = "application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/mp2t"
    return FileResponse(str(f), media_type=ct)


@app.delete("/stream/source/{video_id}")
async def delete_stream_source(video_id: str, request: Request):
    """Delete DVR MP4 and HLS segments after successful upload by the recordings worker."""
    if request.headers.get("x-api-key") != MANAGEMENT_API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

    deleted = []

    # Delete MP4
    for search_dir in [Path(DVR_PATH) / "live", Path(DVR_PATH)]:
        f = search_dir / f"{video_id}.mp4"
        if f.exists():
            f.unlink()
            deleted.append(f"dvr/{f.name}")

    # Look up stream_key from Redis to find the HLS dir
    stream_key = None
    if redis_client:
        stream_key = await redis_client.get(f"{REDIS_PREFIX}video:{video_id}:stream_key")
    if stream_key:
        # SRS stores files flat: /hls/live/{stream_key}-*.ts — delete all matching files
        hls_live = Path(HLS_PATH) / "live"
        for f in hls_live.glob(f"{stream_key}*"):
            f.unlink(missing_ok=True)
            deleted.append(f"hls/live/{f.name}")
        await redis_client.delete(f"{REDIS_PREFIX}video:{video_id}:stream_key", f"{REDIS_PREFIX}stream:{stream_key}:video_id")

    log.info(f"Source files deleted for {video_id}: {deleted}")
    return {"deleted": deleted}


# ── Public viewer token endpoint ─────────────────────────────────────────────

@app.get("/stream-token")
async def get_stream_token(event_id: str, viewer_id: Optional[str] = None):
    """
    Public endpoint — no API key required.
    Resolves event_id → stream_key via Redis → returns a one-time viewer token.
    Only works while the stream is live (Redis key exists).
    """
    if not redis_client:
        raise HTTPException(status_code=503, detail="Redis not configured.")

    stream_key = await redis_client.get(f"{REDIS_PREFIX}event:{event_id}:stream_key")
    if not stream_key:
        raise HTTPException(status_code=404, detail="No live stream found for this event.")

    token = secrets.token_urlsafe(32)
    viewer_tokens[token] = {
        "stream_key": stream_key,
        "viewer_id":  viewer_id,
        "expires_at": time.time() + TOKEN_TTL,
        "created_at": time.time(),
        "used":       False,
        "used_at":    None,
    }
    log.info(f"Token issued for event={event_id} stream={stream_key} viewer={viewer_id or 'anonymous'}")

    return {
        "token":      token,
        "expires_in": TOKEN_TTL,
        "player_url": f"/player?token={token}",
    }


# ── WebRTC proxy — hides stream_key from browser ─────────────────────────────

class PlayRequest(BaseModel):
    sdp:   str
    token: str
    sid:   Optional[str] = None

@app.post("/play")
async def webrtc_play(body: PlayRequest, request: Request):
    """
    Public endpoint — no API key required.
    Browser posts { sdp, token } — stream_key is never exposed to the client.
    Resolves token → stream_key, forwards SDP offer to SRS, returns SDP answer.
    """
    entry = viewer_tokens.get(body.token)
    if not entry:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    if entry["expires_at"] < time.time():
        viewer_tokens.pop(body.token, None)
        raise HTTPException(status_code=401, detail="Token expired.")

    stream_key = entry["stream_key"]
    sid = body.sid or secrets.token_hex(8)
    client_ip = request.headers.get("x-real-ip") or request.client.host

    srs_host = os.environ.get("SRS_PUBLIC_HOST", "livestream.zinrai.live")
    stream_url = f"webrtc://{srs_host}/live/{stream_key}?token={body.token}&sid={sid}"

    log.info(f"WebRTC proxy: calling SRS {SRS_API_URL}/rtc/v1/play/ streamurl={stream_url}")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{SRS_API_URL}/rtc/v1/play/",
                json={"sdp": body.sdp, "streamurl": stream_url},
            )
        result = resp.json()
        log.info(f"SRS /rtc/v1/play/ response: http={resp.status_code} code={result.get('code')} data={result.get('data', '')}")
    except Exception as e:
        log.error(f"SRS /rtc/v1/play/ proxy error: {e}")
        raise HTTPException(status_code=502, detail="Stream server unreachable.")

    if result.get("code") == 401 or result.get("code") == 403:
        raise HTTPException(status_code=403, detail="Stream access denied.")

    if result.get("code") != 0:
        raise HTTPException(status_code=404, detail="Stream not live yet.")

    log.info(f"WebRTC play proxied: stream={stream_key} ip={client_ip} sid={sid}")
    return {"sdp": result["sdp"]}


# ── Management APIs ───────────────────────────────────────────────────────────

class PublisherCreate(BaseModel):
    username:   str
    stream_key: Optional[str] = None

@app.post("/publishers")
def create_publisher(body: PublisherCreate, _: None = Depends(require_api_key)):
    db = get_db()
    try:
        count = db.query(Publisher).count()
        if count >= MAX_PUBLISHERS:
            raise HTTPException(400, f"Max {MAX_PUBLISHERS} publishers reached")

        stream_key = body.stream_key or secrets.token_hex(16)
        pub = Publisher(username=body.username, stream_key=stream_key)
        db.add(pub)
        db.commit()
        db.refresh(pub)
        log.info(f"Publisher created: {pub.username} key={pub.stream_key}")
        return {"id": pub.id, "username": pub.username, "stream_key": pub.stream_key}
    except Exception as e:
        db.rollback()
        raise HTTPException(400, str(e))
    finally:
        db.close()

@app.get("/publishers")
def list_publishers(_: None = Depends(require_api_key)):
    db = get_db()
    try:
        pubs = db.query(Publisher).all()
        return [
            {
                "id":         p.id,
                "username":   p.username,
                "stream_key": p.stream_key,
                "enabled":    p.enabled,
                "live":       p.stream_key in active_streams,
            }
            for p in pubs
        ]
    finally:
        db.close()

@app.get("/token")
def generate_token(stream_key: str, viewer_id: Optional[str] = None, _: None = Depends(require_viewer_token_key)):
    """
    Generate a viewer token for a stream.
    Pass viewer_id if the viewer is a known platform user — used for unique viewer deduplication.
    If viewer_id is omitted the viewer is treated as anonymous.
    """
    token = secrets.token_urlsafe(32)
    viewer_tokens[token] = {
        "stream_key": stream_key,
        "viewer_id":  viewer_id,    # None = anonymous
        "expires_at": time.time() + TOKEN_TTL,
        "created_at": time.time(),
        "used":       False,
        "used_at":    None,
    }
    log.info(f"Token generated for stream={stream_key} viewer_id={viewer_id or 'anonymous'}")
    return {
        "token":      token,
        "expires_in": TOKEN_TTL,
        "player_url": f"/player?token={token}",
    }

@app.get("/streams")
def list_streams(_: None = Depends(require_api_key)):
    """Currently live streams with stats."""
    now = time.time()
    # Count concurrent viewers per stream from in-memory state
    viewers_per_stream: dict[str, int] = {}
    for v in active_viewers.values():
        sk = v["stream_key"]
        viewers_per_stream[sk] = viewers_per_stream.get(sk, 0) + 1

    return [
        {
            "stream_key":         k,
            "username":           v["username"],
            "event_id":           v.get("event_id"),
            "duration_s":         int(now - v["started_at"]),
            "app":                v["app"],
            "concurrent_viewers": viewers_per_stream.get(k, 0),
        }
        for k, v in active_streams.items()
    ]

@app.get("/stats/{stream_key}")
async def stream_stats(stream_key: str, _: None = Depends(require_api_key)):
    """Live analytics for a stream from Redis."""
    if not redis_client:
        raise HTTPException(503, "Redis not configured")
    try:
        pipe = redis_client.pipeline()
        pipe.get(f"{REDIS_PREFIX}stream:{stream_key}:status")
        pipe.get(f"{REDIS_PREFIX}stream:{stream_key}:started_at")
        pipe.get(f"{REDIS_PREFIX}stream:{stream_key}:views")
        pipe.get(f"{REDIS_PREFIX}stream:{stream_key}:watch_seconds")
        pipe.pfcount(f"{REDIS_PREFIX}stream:{stream_key}:unique_viewers")
        pipe.hlen(f"{REDIS_PREFIX}stream:{stream_key}:sessions")
        results = await pipe.execute()

        if not results[0]:
            raise HTTPException(404, f"No data for stream {stream_key}")

        return {
            "stream_key":          stream_key,
            "status":              results[0],
            "started_at":          float(results[1] or 0),
            "total_views":         int(results[2] or 0),
            "total_watch_seconds": int(results[3] or 0),
            "unique_viewers":      int(results[4] or 0),
            "concurrent_viewers":  int(results[5] or 0),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/health")
def health():
    return {
        "status":         "ok",
        "active_streams": len(active_streams),
        "active_viewers": len(active_viewers),
        "record_url":     RECORD_URL or "not configured",
        "redis":          "connected" if redis_client else "not configured",
    }
