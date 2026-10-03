"""Minimal HTTP transcription service (Whisper), for dictation.

Why self-hosted rather than the browser's SpeechRecognition API:
Chrome sends the audio to its own servers to recognize it by default. On a
platform whose whole point is that nothing leaves the machine, that is not
acceptable. Chrome 139+ has an "on-device" mode but it depends on the browser
and the platform; here transcription runs on the local GPU, for all browsers.

We stay on transformers + torch cu130, the only stack validated on this GB10
(aarch64, sm_121) — faster-whisper/CTranslate2 has no wheel for it.
"""
import asyncio
import io
import logging
import os
import threading
import time

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("asr")

# turbo: quality close to large-v3 for a fraction of the time and memory, which
# matters on a machine already running chat, OCR, video and voice.
MODEL_ID = os.environ.get("ASR_MODEL", "openai/whisper-large-v3-turbo")
TARGET_SR = 16000  # Whisper works at 16 kHz
MAX_SECONDS = float(os.environ.get("ASR_MAX_SECONDS", "300"))
# Anti-decompression-bomb caps: a few-KB FLAC can decode to tens of GB in
# float32 (ratio > 1000x) and trigger the OOM killer on a unified-memory
# machine. We bound the bytes AND read the header (duration, rate, channels)
# BEFORE decoding — soundfile reads the header without allocating.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_SR = 48000
MAX_CH = 2

# docs/openapi closed: this service is reachable only by dgx-portal on a
# dedicated docker network, no reason to expose an explorable schema.
app = FastAPI(title="Cronos ASR", docs_url=None, redoc_url=None, openapi_url=None)

_pipe = None
_load_error: str | None = None
# A load failure at STARTUP is not final: measured 2026-10-03, one CUDA OOM
# (unified memory is shared with the chat model) left `_pipe` None FOREVER —
# /api/transcribe answered 503 without ever retrying, so the portal's honest
# « libère de la mémoire, puis relance la dictée » could not work by
# construction. The load is now retried on demand (single-flight, throttled).
_load_lock = threading.Lock()
_last_attempt: float = 0.0
_RETRY_S = 10.0
# The anyio threadpool accepts 40 tasks: unbounded, 40 transcriptions could hit
# the same transformers pipeline (not thread-safe) and 40 Whisper inferences the
# same unified-memory GPU. A semaphore of 2 keeps some throughput for dictation
# while capping the GPU load.
_gpu_sem = asyncio.Semaphore(2)


def _charger() -> None:
    """Load the weights (startup, and retried on demand below)."""
    global _pipe, _load_error, _last_attempt
    _last_attempt = time.monotonic()
    try:
        from transformers import pipeline

        cuda = torch.cuda.is_available()
        log.info("Loading %s (cuda=%s)…", MODEL_ID, cuda)
        _pipe = pipeline(
            "automatic-speech-recognition",
            model=MODEL_ID,
            device="cuda:0" if cuda else "cpu",
            torch_dtype=torch.float16 if cuda else torch.float32,
            # Splits long recordings: Whisper only takes 30 s per pass,
            # without this everything beyond that is silently cut off.
            chunk_length_s=30,
        )
        log.info("Loaded.")
    except Exception as exc:  # pragma: no cover - depends on the CUDA runtime
        _load_error = str(exc)
        log.exception("Model failed to load")


def _charger_si_besoin() -> bool:
    """True when the model is loaded; retries a FAILED load on demand.

    Single-flight (a second caller waits for the attempt to finish) and
    throttled to one attempt per `_RETRY_S`: the dictation polls every second,
    and a failing CUDA allocation is not made cheaper by hammering it.
    """
    global _pipe, _last_attempt
    if _pipe is not None:
        return True
    with _load_lock:
        if _pipe is not None:
            return True
        if time.monotonic() - _last_attempt < _RETRY_S:
            return False
        _charger()
        return _pipe is not None


@app.on_event("startup")
def _load() -> None:
    _charger()


@app.get("/api/model-info")
def model_info() -> JSONResponse:
    return JSONResponse({
        "loaded": _pipe is not None,
        "type": MODEL_ID.rsplit("/", 1)[-1],
        "engine": "whisper",
        "device": "cuda" if torch.cuda.is_available() else "cpu",
        "error": _load_error,
    })


@app.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    # Forcing the language stops Whisper from "detecting" English on a short
    # French sentence, its classic mistake.
    language: str = Form(""),
) -> JSONResponse:
    # run_in_threadpool: the load holds its lock ~15 s and would otherwise
    # freeze the WHOLE sidecar event loop, /api/model-info included (the
    # portal's probes then time out during a perfectly healthy load).
    if not await run_in_threadpool(_charger_si_besoin):
        raise HTTPException(status_code=503,
                            detail=_load_error or "Modèle non chargé.")

    raw = await audio.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Fichier audio trop volumineux.")

    def _decode():
        # Header first: reject out-of-bounds duration/rate/channels WITHOUT
        # decoding the samples, which cuts the decompression bomb short. Then
        # decoding + resampling, all off the event loop (blocking on a big file).
        bio = io.BytesIO(raw)
        try:
            info = sf.info(bio)
        except Exception:
            raise HTTPException(status_code=400, detail="Audio illisible.")
        if info.samplerate > MAX_SR or info.channels > MAX_CH:
            raise HTTPException(status_code=400, detail="Format audio non supporté.")
        dur = info.frames / float(info.samplerate or 1)
        if dur < 0.3:
            raise HTTPException(status_code=400, detail="Enregistrement trop court.")
        if dur > MAX_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=f"Enregistrement trop long ({dur:.0f}s, maximum {MAX_SECONDS:.0f}s).")
        bio.seek(0)
        d, sr = sf.read(bio, dtype="float32", always_2d=True)
        d = d.mean(axis=1)  # mono
        if sr != TARGET_SR:
            # Linear resampling: good enough for speech, and avoids one more
            # dependency (librosa/resampy) in this image.
            n = int(round(len(d) * TARGET_SR / sr))
            d = np.interp(
                np.linspace(0, len(d) - 1, n, dtype=np.float64),
                np.arange(len(d), dtype=np.float64),
                d.astype(np.float64),
            ).astype(np.float32)
        return d

    data = await run_in_threadpool(_decode)

    kwargs = {}
    if language:
        kwargs["generate_kwargs"] = {"language": language}

    try:
        # Blocking (GPU) call: keep it off the event loop, otherwise
        # /api/model-info stops answering during a transcription and the portal
        # concludes the backend is offline.
        async with _gpu_sem:
            out = await run_in_threadpool(lambda: _pipe({"raw": data, "sampling_rate": TARGET_SR}, **kwargs))
    except Exception:
        # The exception detail (container paths, HF cache, tensor shapes) is
        # logged server-side but never returned to the caller.
        log.exception("Transcription failed")
        raise HTTPException(status_code=500, detail="Échec de la transcription.")

    text = (out.get("text") or "").strip()
    # On non-speech (silence, breath, continuous noise), Whisper loops and
    # returns strings like « . . . . . » or « Beep! Beep! ». It is very visible in
    # live dictation, where the first round often lands before the first word. A
    # text without a single letter or digit is not speech: we return empty rather
    # than polluting the field.
    if not any(c.isalnum() for c in text):
        text = ""
    return JSONResponse({"text": text})
