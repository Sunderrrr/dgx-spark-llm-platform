"""Personal voice: Fish Audio S2 Pro cloning the operator's voice, for the voice mode.

Fourth engine of the « voice » container (same name, voice_net network, port
8004 as Chatterbox, Qwen3-TTS and Kyutai). It speaks the playground's voice
mode contract that only Kyutai spoke before:
  GET  /api/model-info  → engine, loaded, voices, default_voice, sample_rate,
                          and `conversation: true` (what the portal checks)
  POST /api/speak       → {text, voice} → raw PCM s16le mono, streamed
plus `POST /tts` for the OpenAI-shaped `/v1/audio/speech` route.

The voice is a reference clip + its transcript installed by install-voice.sh
and mounted read-only at /app/references/voice (see s2engine.py for why it is
not a fine-tune). No user audio is ever decoded here.

Latency. The fish-speech PyTorch engine returns audio per text chunk (~200
bytes), so one sentence comes back in one piece. The portal already sends ONE
sentence per call; on top of that a long sentence is split once at its first
comma, so the first clause plays while the rest is generated.
"""
import asyncio
import io
import logging
import os
import re
import subprocess
import threading

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

import s2engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("s2pro-voice")

REPO = os.environ.get("S2PRO_CHECKPOINT", "fishaudio/s2-pro")
# Eager by default: on the GB10 torch.compile (Inductor) was still compiling for
# sm_121 after 20 minutes without emitting a single audio frame (measured
# 2026-11-25). S2PRO_COMPILE=1 opts back in.
COMPILE = os.environ.get("S2PRO_COMPILE", "0") == "1"
SEED = int(os.environ["S2PRO_SEED"]) if os.environ.get("S2PRO_SEED") else None
REF_ID = "voice"                        # /app/references/voice, mounted by the recreate script
MAX_TEXT_CHARS = 1000                   # /api/speak: a sentence or two, like Kyutai
MAX_TTS_CHARS = 3000                    # /tts: same bound as the Qwen3-TTS engine
GEN_TIMEOUT_S = float(os.environ.get("S2PRO_GEN_TIMEOUT", "240"))
FIRST_SPLIT_MIN = 60                    # below this, a sentence is generated in one go
CLAUSE_GAP_S = 0.08

app = FastAPI(title="DGX S2 Pro voice", docs_url=None, redoc_url=None, openapi_url=None)

_voice: s2engine.S2Voice | None = None
_speaker = "voice"
_load_error: str | None = None


def _speaker_name() -> str:
    try:
        import json
        with open(os.path.join("references", REF_ID, "VOICE.json"), encoding="utf-8") as f:
            return str(json.load(f).get("speaker") or "voice")[:16].lower()
    except (OSError, ValueError):
        return "voice"


def _load() -> None:
    global _voice, _speaker, _load_error
    try:
        ref_dir = os.path.join("references", REF_ID)
        if not os.path.isdir(ref_dir) or not any(n.endswith(".lab") for n in os.listdir(ref_dir)):
            raise RuntimeError(f"no reference voice mounted at {ref_dir}")
        voice = s2engine.load(s2engine.resolve_checkpoint(REPO), COMPILE, log=log.warning)
        # Warm-up WITH the reference: encodes and caches it, and pays the
        # first-call costs here instead of on the first sentence of a conversation.
        voice.synth("Bonjour.", REF_ID, SEED)
        _voice, _speaker = voice, _speaker_name()
        log.info("S2 Pro voice « %s » ready in %.1f s (compile=%s, %d Hz)",
                 _speaker, voice.load_s, voice.compiled, voice.sample_rate)
    except Exception as e:                       # noqa: BLE001
        _load_error = f"{type(e).__name__}: {e}"
        log.exception("S2 Pro voice failed to load")


def _split_first_clause(text: str) -> list[str]:
    """[text] or [first clause, rest] — see the module docstring."""
    if len(text) < FIRST_SPLIT_MIN:
        return [text]
    m = re.search(r"[,;:]\s+", text[20:])
    if not m:
        return [text]
    cut = 20 + m.end()
    head, tail = text[:cut].strip(), text[cut:].strip()
    return [head, tail] if tail else [text]


def _pcm16(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()


def _clean(text: str) -> str:
    return " ".join(text.split())


@app.on_event("startup")
async def _startup():
    threading.Thread(target=_load, daemon=True).start()


@app.get("/api/model-info")
async def model_info():
    loaded = _voice is not None and _load_error is None
    return JSONResponse({
        "engine": "fish-s2-pro", "type": "s2-pro", "repo_id": "custom-voice",
        "conversation": True,
        "loaded": loaded,
        "error": _load_error,
        "sample_rate": _voice.sample_rate if _voice else 44100,
        "voices": [_speaker] if loaded else [], "default_voice": _speaker if loaded else None,
        "compiled": _voice.compiled if _voice else None,
        "load_seconds": _voice.load_s if _voice else None,
    })


class SpeakIn(BaseModel):
    text: str
    voice: str = ""


@app.post("/api/speak")
async def speak(body: SpeakIn):
    if _voice is None:
        raise HTTPException(503, _load_error or "model loading")
    text = _clean(body.text)
    if not text:
        raise HTTPException(400, "empty text")
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(413, f"text longer than {MAX_TEXT_CHARS} characters")
    pieces = _split_first_clause(text)
    sr = _voice.sample_rate
    gap = _pcm16(np.zeros(int(CLAUSE_GAP_S * sr), dtype=np.float32))

    async def stream():
        loop = asyncio.get_running_loop()
        for i, piece in enumerate(pieces):
            try:
                audio = await loop.run_in_executor(None, _voice.synth, piece, REF_ID, SEED)
            except Exception:                    # noqa: BLE001
                log.exception("generation failed")
                return
            if i:
                yield gap
            yield _pcm16(audio)
        # A client that went away (barge-in) raises GeneratorExit at the next
        # yield: the second half is then never generated.

    return StreamingResponse(stream(), media_type="application/octet-stream",
                             headers={"X-Sample-Rate": str(sr), "X-Sample-Format": "s16le"})


class TtsIn(BaseModel):
    text: str
    output_format: str = "wav"
    voice: str = ""


@app.post("/tts")
async def tts(body: TtsIn):
    if _voice is None:
        raise HTTPException(503, _load_error or "model loading")
    text = _clean(body.text)
    if not text:
        raise HTTPException(400, "empty text")
    if len(text) > MAX_TTS_CHARS:
        raise HTTPException(413, f"text longer than {MAX_TTS_CHARS} characters")
    fmt = (body.output_format or "wav").lower()
    if fmt not in ("wav", "mp3", "pcm"):
        fmt = "wav"
    sr = _voice.sample_rate
    try:
        # fish-speech chunks long text itself (chunk_length): one call.
        audio = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _voice.synth, text, REF_ID, SEED),
            timeout=GEN_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(504, "generation too long, try a shorter text")
    except Exception:                            # noqa: BLE001
        log.exception("generation failed")
        raise HTTPException(500, "generation failed")
    if fmt == "pcm":
        return Response(_pcm16(audio), media_type="application/octet-stream",
                        headers={"X-Sample-Rate": str(sr), "X-Sample-Format": "s16le"})
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
    if fmt == "wav":
        return Response(buf.getvalue(), media_type="audio/wav")
    mp3 = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "wav", "-i", "pipe:0", "-f", "mp3", "-b:a", "128k", "pipe:1"],
                         input=buf.getvalue(), capture_output=True, timeout=60, check=False)
    if mp3.returncode != 0:
        return Response(buf.getvalue(), media_type="audio/wav")
    return Response(mp3.stdout, media_type="audio/mpeg")
