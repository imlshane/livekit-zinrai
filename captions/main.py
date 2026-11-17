"""
Captions Service
----------------
Per-stream live speech-to-text. Pulls the stream's audio from SRS (HTTP-FLV),
segments it on silence, transcribes with faster-whisper (language auto-detected)
and POSTs each finished cue to the auth service, which fans it out to viewers.

  POST /start {stream_key}   — begin captioning (idempotent)
  POST /stop  {stream_key}   — stop captioning
  GET  /health
"""

import asyncio
import logging
import os
import time

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("captions")

SRS_HTTP_URL   = os.environ.get("SRS_HTTP_URL", "http://srs:8080").rstrip("/")
AUTH_URL       = os.environ.get("AUTH_URL", "http://auth:8000").rstrip("/")
CAPTION_SECRET = os.environ.get("CAPTION_SECRET", "")
WHISPER_MODEL  = os.environ.get("WHISPER_MODEL", "small")
WHISPER_CPU_THREADS = int(os.environ.get("WHISPER_CPU_THREADS", "2"))
MAX_CONCURRENT_STREAMS = int(os.environ.get("MAX_CONCURRENT_STREAMS", "2"))
IDLE_TIMEOUT   = int(os.environ.get("IDLE_TIMEOUT_SEC", "60"))   # give up if the stream stays gone this long

SAMPLE_RATE    = 16000
FRAME_MS       = 30
FRAME_BYTES    = SAMPLE_RATE * FRAME_MS // 1000 * 2              # s16le mono
SPEECH_RMS     = float(os.environ.get("SPEECH_RMS", "350"))      # int16 RMS above this = speech
END_SILENCE_MS = 500
MAX_SEGMENT_S  = 6
MIN_SEGMENT_S  = 0.4
PREROLL_FRAMES = 6

app = FastAPI()
tasks: dict[str, asyncio.Task] = {}
_model = None
_infer_sem = asyncio.Semaphore(MAX_CONCURRENT_STREAMS)


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        log.info(f"Loading Whisper model '{WHISPER_MODEL}' (int8, {WHISPER_CPU_THREADS} threads)")
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8",
                              cpu_threads=WHISPER_CPU_THREADS)
    return _model


def _transcribe(pcm: bytes) -> tuple[str, str]:
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    segments, info = _get_model().transcribe(
        audio, beam_size=1, vad_filter=True, condition_on_previous_text=False,
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    return text, info.language


async def _post_cue(client: httpx.AsyncClient, stream_key: str, cue_id: int, text: str, lang: str) -> None:
    try:
        await client.post(
            f"{AUTH_URL}/internal/caption",
            json={"stream_key": stream_key, "id": cue_id, "text": text, "lang": lang, "t": time.time()},
            headers={"X-Internal-Secret": CAPTION_SECRET},
        )
    except Exception as e:
        log.warning(f"cue post failed stream={stream_key}: {e}")


async def _flush(client, stream_key: str, seq: list, pcm: bytes) -> None:
    if len(pcm) < MIN_SEGMENT_S * SAMPLE_RATE * 2:
        return
    async with _infer_sem:
        text, lang = await asyncio.get_running_loop().run_in_executor(None, _transcribe, pcm)
    if text:
        seq[0] += 1
        await _post_cue(client, stream_key, seq[0], text, lang)


async def _caption_loop(stream_key: str) -> None:
    url = f"{SRS_HTTP_URL}/live/{stream_key}.flv?internal={CAPTION_SECRET}"
    seq = [int(time.time())]            # unique-ish across restarts
    last_ok = time.time()
    async with httpx.AsyncClient(timeout=10) as client:
        while time.time() - last_ok < IDLE_TIMEOUT:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-loglevel", "error", "-i", url,
                "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            log.info(f"caption: ffmpeg attached stream={stream_key}")
            buf, preroll, speaking, silence = bytearray(), [], False, 0
            try:
                while True:
                    frame = await proc.stdout.readexactly(FRAME_BYTES)
                    last_ok = time.time()
                    rms = float(np.sqrt(np.mean(np.frombuffer(frame, dtype=np.int16).astype(np.float32) ** 2)))
                    if rms >= SPEECH_RMS:
                        if not speaking:
                            speaking = True
                            for f in preroll:
                                buf += f
                        buf += frame
                        silence = 0
                    elif speaking:
                        buf += frame
                        silence += 1
                    else:
                        preroll.append(frame)
                        preroll = preroll[-PREROLL_FRAMES:]

                    too_long = len(buf) >= MAX_SEGMENT_S * SAMPLE_RATE * 2
                    if speaking and (silence * FRAME_MS >= END_SILENCE_MS or too_long):
                        pcm, buf, speaking, silence, preroll = bytes(buf), bytearray(), False, 0, []
                        asyncio.create_task(_flush(client, stream_key, seq, pcm))
            except asyncio.IncompleteReadError:
                pass                      # ffmpeg exited — stream gone or dropped
            finally:
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()
            await asyncio.sleep(2)        # retry while within IDLE_TIMEOUT (OBS reconnect window)
    log.info(f"caption: idle timeout, stopping stream={stream_key}")
    tasks.pop(stream_key, None)


class StreamReq(BaseModel):
    stream_key: str


@app.post("/start")
async def start(body: StreamReq):
    t = tasks.get(body.stream_key)
    if t and not t.done():
        return {"status": "already_running"}
    tasks[body.stream_key] = asyncio.create_task(_caption_loop(body.stream_key))
    log.info(f"caption: started stream={body.stream_key}")
    return {"status": "started"}


@app.post("/stop")
async def stop(body: StreamReq):
    t = tasks.pop(body.stream_key, None)
    if t:
        t.cancel()
    return {"status": "stopped"}


@app.on_event("startup")
async def warm_model():
    asyncio.get_running_loop().run_in_executor(None, _get_model)


@app.get("/health")
def health():
    return {"ok": True, "streams": list(tasks)}
