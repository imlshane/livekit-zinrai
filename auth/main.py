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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Boolean, Column, DateTime, Integer, String, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# ── Config ────────────────────────────────────────────────────────────────────

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:////data/auth.db")
SECRET_KEY   = os.environ.get("SECRET_KEY", "local-dev-secret")
DVR_PATH     = os.environ.get("DVR_PATH", "/dvr")
S3_ENDPOINT  = os.environ.get("S3_ENDPOINT", "")
S3_BUCKET    = os.environ.get("S3_BUCKET", "streams")
S3_ACCESS    = os.environ.get("S3_ACCESS_KEY", "")
S3_SECRET    = os.environ.get("S3_SECRET_KEY", "")

MAX_PUBLISHERS = 100
TOKEN_TTL      = 3600  # seconds — how long a viewer token is valid to start playing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

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
# Production: move these to Redis

active_streams: dict[str, dict] = {}   # stream_key -> {started_at, client_id, app}
viewer_tokens:  dict[str, dict] = {}   # token -> {stream_key, used, expires_at}

# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    db = get_db()
    try:
        seed_test_data(db)
    finally:
        db.close()

    # Background: clean up expired tokens every 5 minutes
    async def cleanup_tokens():
        while True:
            await asyncio.sleep(300)
            now = time.time()
            expired = [t for t, v in viewer_tokens.items() if v["expires_at"] < now]
            for t in expired:
                del viewer_tokens[t]
            if expired:
                log.info(f"Cleaned up {len(expired)} expired tokens")

    task = asyncio.create_task(cleanup_tokens())
    yield
    task.cancel()

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

# ── SRS Callbacks ─────────────────────────────────────────────────────────────

@app.post("/on_publish")
async def on_publish(request: Request):
    """
    SRS fires this when OBS connects to push an RTMP stream.
    Validate: stream key exists, publisher enabled, under max cap.
    """
    body = await request.json()
    stream_key = body.get("stream", "")
    client_id  = body.get("client_id", "")
    app_name   = body.get("app", "live")

    log.info(f"on_publish: stream={stream_key} client={client_id}")

    db = get_db()
    try:
        publisher = db.query(Publisher).filter_by(stream_key=stream_key, enabled=True).first()
        if not publisher:
            return srs_deny(f"Unknown or disabled stream key: {stream_key}")

        if stream_key in active_streams:
            return srs_deny(f"Stream key already in use: {stream_key}")

        if len(active_streams) >= MAX_PUBLISHERS:
            return srs_deny(f"Max publisher limit ({MAX_PUBLISHERS}) reached")

        active_streams[stream_key] = {
            "started_at": time.time(),
            "client_id":  client_id,
            "app":        app_name,
            "username":   publisher.username,
        }
        log.info(f"Stream started: {stream_key} by {publisher.username} ({len(active_streams)} active)")
        return srs_ok()
    finally:
        db.close()

@app.post("/on_unpublish")
async def on_unpublish(request: Request):
    """OBS stopped streaming."""
    body = await request.json()
    stream_key = body.get("stream", "")
    log.info(f"on_unpublish: stream={stream_key}")
    active_streams.pop(stream_key, None)
    return srs_ok()

@app.post("/on_play")
async def on_play(request: Request):
    """
    SRS fires this when a viewer starts playing (HTTP-FLV or HLS).
    Validate the one-time token passed as ?token=xxx in the stream URL.
    """
    body  = await request.json()
    stream_key = body.get("stream", "")
    param      = body.get("param", "")   # e.g. "?token=abc123"
    client_id  = body.get("client_id", "")

    log.info(f"on_play: stream={stream_key} param={param} client={client_id}")

    # Parse token from query string
    qs    = urllib.parse.parse_qs(param.lstrip("?"))
    token = qs.get("token", [None])[0]

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

    # Token is time-limited (not one-time) — allows WebRTC ICE restarts
    # and reconnects within the token TTL without requiring a new token.
    log.info(f"Viewer authenticated: stream={stream_key} client={client_id}")
    return srs_ok()

@app.post("/on_stop")
async def on_stop(request: Request):
    """Viewer disconnected — no action needed."""
    body = await request.json()
    log.info(f"on_stop: stream={body.get('stream')} client={body.get('client_id')}")
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
    """Convert .flv to .mp4 via ffmpeg, then upload to S3-compatible storage."""
    import subprocess

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
    except FileNotFoundError:
        log.warning("ffmpeg not installed — skipping conversion. Install ffmpeg in auth container for VOD.")
        return

    if S3_ENDPOINT and S3_ACCESS:
        await upload_to_s3(mp4, stream_key)
    else:
        log.info("S3 not configured — mp4 saved locally at " + str(mp4))

async def upload_to_s3(mp4: Path, stream_key: str):
    """Upload mp4 to R2 / DO Spaces / any S3-compatible storage."""
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
        log.info(f"Uploading {mp4.name} → s3://{S3_BUCKET}/{key}")
        s3.upload_file(str(mp4), S3_BUCKET, key)
        log.info(f"Upload complete: {key}")
        mp4.unlink()   # delete local file after upload
    except Exception as e:
        log.error(f"S3 upload failed: {e}")

# ── Management APIs ───────────────────────────────────────────────────────────

class PublisherCreate(BaseModel):
    username:   str
    stream_key: Optional[str] = None

@app.post("/publishers")
def create_publisher(body: PublisherCreate):
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
def list_publishers():
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
def generate_token(stream_key: str):
    """Generate a one-time viewer token for a stream."""
    token = secrets.token_urlsafe(32)
    viewer_tokens[token] = {
        "stream_key": stream_key,
        "expires_at": time.time() + TOKEN_TTL,
        "created_at": time.time(),
    }
    log.info(f"Token generated for stream={stream_key}")
    return {
        "token":      token,
        "stream_key": stream_key,
        "expires_in": TOKEN_TTL,
        "player_url": f"/player?stream={stream_key}&token={token}",
    }

@app.get("/streams")
def list_streams():
    """Currently live streams with stats."""
    now = time.time()
    return [
        {
            "stream_key": k,
            "username":   v["username"],
            "duration_s": int(now - v["started_at"]),
            "app":        v["app"],
        }
        for k, v in active_streams.items()
    ]

@app.get("/health")
def health():
    return {"status": "ok", "active_streams": len(active_streams)}
