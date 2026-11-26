"""Minimal HTTP service around Qwen3-TTS (zero-shot voice cloning).

Qwen ships no server: vLLM-Omni currently only does offline inference, and the
only upstream server is a Gradio demo. So we expose the bare minimum ourselves
for dgx-portal.

Deliberate difference from the Chatterbox service: ONE multipart call (/clone)
instead of « upload the reference then generate ». Chatterbox kept the
reference clip on disk without ever deleting it (a TTL purge had to be added);
here the audio never leaves memory.
"""
import asyncio
import io
import logging
import os
import re
import threading

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("qwen3-tts")

MODEL_ID = os.environ.get("QWEN_TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-Base")
# Chatterbox refused any clip <= 5 s; Qwen claims cloning from 3 s. We keep an
# explicit lower bound to return a clear message rather than let the model
# produce anything.
MIN_REF_SECONDS = float(os.environ.get("QWEN_TTS_MIN_REF_SECONDS", "3"))
MAX_REF_SECONDS = float(os.environ.get("QWEN_TTS_MAX_REF_SECONDS", "90"))
# Anti-decompression-bomb (see asr/server.py): bound the bytes and read the
# header before decoding the reference sample.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_SR = 48000
MAX_CH = 2
# Hard bound on the text to read: beyond it, a single autoregressive sequence
# holds the GPU lock for minutes (the portal gives up at 180 s but the
# generation, which cannot be cancelled, goes on). The portal already caps at
# 2000.
MAX_TEXT_CHARS = 3000
GEN_TIMEOUT_S = float(os.environ.get("QWEN_TTS_GEN_TIMEOUT", "240"))

# Single-speaker fine-tune (Qwen3-TTS-12Hz-*-Base): when SET, the engine loads
# that local checkpoint instead of the catalog repo id and speaks with SPEAKER —
# no reference clip at inference. Trained with the official finetuning/ recipe.
FINETUNE_DIR = os.environ.get("QWEN_TTS_FINETUNE_DIR", "").strip()
SPEAKER = os.environ.get("QWEN_TTS_SPEAKER", "").strip() or "speaker"

app = FastAPI(title="DGX Qwen3-TTS", docs_url=None, redoc_url=None, openapi_url=None)

_model = None
_languages: dict[str, str] = {}
_load_error: str | None = None
# One model on one GPU: two simultaneous generations would step on each other.
# The lock queues them instead of letting them fail.
#
# It is a THREAD lock, acquired inside the worker, and not an asyncio.Lock held
# across the await: `asyncio.wait_for` cancels the future but not the thread
# running the blocking model call, so an asyncio lock was released at the
# timeout while the generation kept the GPU — the next request then ran
# concurrently on the same model (audit 2026-11-26). A thread lock is released
# only when the call really returns.
_gpu_lock = threading.Lock()


def _exclusive(fn, *args):
    """Run `fn` holding the GPU: the lock lives inside the worker thread."""
    with _gpu_lock:
        return fn(*args)


def _attn_impl() -> str:
    """flash_attention_2 is recommended but does not compile everywhere (ARM64
    notably) — we take it only if it really imports, otherwise native PyTorch
    attention, which gives the same result a bit more slowly."""
    try:
        import flash_attn  # noqa: F401
        return "flash_attention_2"
    except Exception:
        return "sdpa"


@app.on_event("startup")
def _load() -> None:
    global _model, _languages, _load_error
    try:
        from qwen_tts import Qwen3TTSModel

        impl = _attn_impl()
        source = FINETUNE_DIR or MODEL_ID
        log.info("Loading %s (attn=%s)%s…", source, impl,
                 f", fine-tuned speaker « {SPEAKER} »" if FINETUNE_DIR else "")
        _model = Qwen3TTSModel.from_pretrained(
            source,
            device_map="cuda:0" if torch.cuda.is_available() else "cpu",
            dtype=torch.bfloat16,
            attn_implementation=impl,
        )
        # Qwen expects the language NAME, lowercase ("french"), not an ISO
        # code. We build code -> name so the portal keeps codes internally. Any
        # unknown name is ignored rather than truncated to its first two letters,
        # which produced wrong codes ("sp" for spanish, "ge" for german…).
        names = list(_model.get_supported_languages())
        _languages = {_ISO[n.lower()]: n for n in names if n.lower() in _ISO}
        skipped = [n for n in names if n.lower() not in _ISO]
        if skipped:
            log.warning("Languages without a known ISO code, skipped: %s", skipped)
        log.info("Loaded. %d languages: %s", len(_languages), ", ".join(sorted(_languages)))
        if FINETUNE_DIR:
            # Warm-up: the first generation of a process pays CUDA/cudnn kernel
            # setup (seconds). Paying it HERE means the first sentence of a
            # conversation starts like the others.
            _t = __import__("time").time()
            _speak("Bonjour.", None)
            log.info("Warm-up done in %.1f s", __import__("time").time() - _t)
    except Exception as exc:  # pragma: no cover - depends on the CUDA runtime
        _load_error = str(exc)
        log.exception("Model failed to load")


# Qwen returns the names in lowercase. « auto » is not a language but automatic
# detection — we keep it and use it by default, it is more robust than imposing
# a choice on the user.
_ISO = {
    "auto": "auto",
    "chinese": "zh", "english": "en", "japanese": "ja", "korean": "ko",
    "german": "de", "french": "fr", "russian": "ru", "portuguese": "pt",
    "spanish": "es", "italian": "it",
}


# Long text sent as one block is generated as a single autoregressive sequence,
# whose cost grows much faster than linearly: measured here, ~330 characters
# take 17 s while a ~1400 speech exceeded 6 minutes and made the request expire
# on the portal side. So we split like the Chatterbox server does, respecting
# sentence ends.
_CHUNK_TARGET = 250
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…:;])\s+|\n+")


# HARD bound per piece: a text without any punctuation (thus a single
# « phrase » for _SENTENCE_SPLIT) stayed whole and went back into an endless
# autoregressive sequence. Beyond this length we cut, preferably on a nearby
# space, to guarantee every piece stays bounded.
_CHUNK_HARD_MAX = 400


def _hard_split(part: str) -> list[str]:
    out = []
    while len(part) > _CHUNK_HARD_MAX:
        cut = part.rfind(" ", 0, _CHUNK_HARD_MAX)
        if cut <= 0:
            cut = _CHUNK_HARD_MAX  # a single oversized word: hard cut
        out.append(part[:cut].strip())
        part = part[cut:].strip()
    if part:
        out.append(part)
    return out


def _chunk_text(text: str) -> list[str]:
    chunks, cur = [], ""
    for part in (p.strip() for p in _SENTENCE_SPLIT.split(text) if p and p.strip()):
        for sub in _hard_split(part):
            if cur and len(cur) + 1 + len(sub) > _CHUNK_TARGET:
                chunks.append(cur)
                cur = sub
            else:
                cur = f"{cur} {sub}".strip()
    if cur:
        chunks.append(cur)
    return chunks or [text]


def _join(wavs: list, sr: int):
    """Concatenate the pieces with a short pause, as between two sentences."""
    if len(wavs) == 1:
        return np.asarray(wavs[0], dtype=np.float32)
    gap = np.zeros(int(0.18 * sr), dtype=np.float32)
    out = []
    for i, w in enumerate(wavs):
        out.append(np.asarray(w, dtype=np.float32).squeeze())
        if i < len(wavs) - 1:
            out.append(gap)
    return np.concatenate(out)


def _trim_silence(wav, sr: int, thresh: float = 0.012):
    """Drop leading/trailing silence: every generated piece adds some, and it is
    paid twice — once to generate it, once to listen to it."""
    import numpy as _np
    if wav is None or len(wav) == 0:
        return wav
    loud = _np.abs(wav) > thresh
    if not loud.any():
        return wav
    idx = _np.flatnonzero(loud)
    keep = max(0, idx[0] - int(0.02 * sr)), min(len(wav), idx[-1] + int(0.05 * sr))
    return wav[keep[0]:keep[1]]


def _speak(text: str, language: str | None):
    """Audio for one sentence: the fine-tuned speaker, or the clone path."""
    if FINETUNE_DIR:
        wavs, sr = _model.generate_custom_voice(text=text, speaker=SPEAKER, language=language)
    else:  # pragma: no cover - the clone path is exercised through /clone
        raise HTTPException(status_code=400, detail="no fine-tuned speaker loaded")
    return _trim_silence(wavs[0], sr), sr


@app.get("/api/model-info")
def model_info() -> JSONResponse:
    return JSONResponse({
        "loaded": _model is not None,
        "type": MODEL_ID.rsplit("/", 1)[-1],
        "engine": "qwen3-tts",
        # Only a fine-tuned checkpoint can answer a sentence on its own, which is
        # what the playground voice mode needs (/api/speak).
        "conversation": bool(FINETUNE_DIR),
        "voices": [SPEAKER] if (FINETUNE_DIR and _model is not None) else [],
        "default_voice": SPEAKER if (FINETUNE_DIR and _model is not None) else None,
        "sample_rate": 24000,
        "device": "cuda" if torch.cuda.is_available() else "cpu",
        "supported_languages": _languages,
        "min_reference_seconds": MIN_REF_SECONDS,
        "max_reference_seconds": MAX_REF_SECONDS,
        "error": _load_error,
    })


@app.post("/clone")
async def clone(
    reference: UploadFile = File(...),
    text: str = Form(...),
    language: str = Form("en"),
    # Transcription of the reference clip. Provided => maximum quality;
    # absent => x_vector_only_mode, which only uses the speaker fingerprint
    # (Qwen documents lower quality in that case).
    ref_text: str = Form(""),
) -> Response:
    if _model is None:
        raise HTTPException(status_code=503, detail=_load_error or "Modèle non chargé.")

    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(status_code=400, detail="Texte à lire trop long.")

    raw = await reference.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Audio de référence trop volumineux.")

    def _decode_ref():
        # Header first (duration/rate/channels) then decoding, off the event
        # loop: a trapped file can neither blow up the RAM nor block the
        # service.
        bio = io.BytesIO(raw)
        try:
            info = sf.info(bio)
        except Exception:
            raise HTTPException(status_code=400, detail="Audio de référence illisible (WAV/MP3 attendu).")
        if info.samplerate > MAX_SR or info.channels > MAX_CH:
            raise HTTPException(status_code=400, detail="Format audio non supporté.")
        dur = info.frames / float(info.samplerate or 1)
        if dur < MIN_REF_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=f"Échantillon trop court ({dur:.1f}s) — au moins {MIN_REF_SECONDS:.0f}s requises.")
        if dur > MAX_REF_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=f"Échantillon trop long ({dur:.1f}s) — maximum {MAX_REF_SECONDS:.0f}s.")
        bio.seek(0)
        a, s = sf.read(bio, dtype="float32", always_2d=True)
        return a.mean(axis=1), s  # mono

    audio, sr = await run_in_threadpool(_decode_ref)

    # Fall back to automatic detection rather than English: an unknown language
    # would otherwise generate French read with an English phonetics.
    lang_name = _languages.get(language) or _languages.get("auto") or _languages.get("en")
    transcript = ref_text.strip()
    chunks = _chunk_text(text)

    def _run():
        # Build the reference prompt ONCE then generate all pieces at once: Qwen
        # does not re-extract the voice features at every call, and above all no
        # sequence is very long.
        prompt = _model.create_voice_clone_prompt(
            ref_audio=(audio, sr),
            ref_text=transcript or None,
            x_vector_only_mode=not transcript,
        )
        return _model.generate_voice_clone(
            text=chunks,
            language=[lang_name] * len(chunks),
            voice_clone_prompt=prompt,
        )

    try:
        # generate_voice_clone is blocking (GPU): calling it directly in this
        # coroutine froze the whole event loop, to the point that /api/model-info
        # stopped answering during a generation — the portal then concluded
        # « service injoignable » and the admin saw the backend offline. So we
        # run it in a thread, under lock.
        # The lock lives in the worker (_exclusive): a client-facing timeout must
        # never release the GPU while the blocking call is still running.
        wavs, out_sr = await asyncio.wait_for(run_in_threadpool(_exclusive, _run), timeout=GEN_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Génération trop longue, réessaie avec un texte plus court.")
    except Exception:
        # Detail logged server-side, never returned to the client (paths,
        # HF cache, torch/qwen internals).
        log.exception("Generation failed")
        raise HTTPException(status_code=500, detail="Échec de la génération.")

    audio_out = _join(list(wavs), out_sr)
    buf = io.BytesIO()
    sf.write(buf, audio_out, out_sr, format="WAV", subtype="PCM_16")
    return Response(content=buf.getvalue(), media_type="audio/wav")


# This Qwen API has no audio streaming (generate_custom_voice always returns the
# whole sentence; `non_streaming_mode=False` only simulates streaming TEXT
# input), and the model generates at about real time (RTF ~1 measured on the
# GB10). The only lever on latency is therefore GRANULARITY: a sentence is cut
# into clauses at every comma/colon/semicolon and each clause is streamed as
# soon as it is ready. The player schedules them in order, so the voice trails
# the text by one CLAUSE instead of one sentence.
# The model generates about one second of audio per second of compute (RTF ~1
# measured), so a piece of audio is READY roughly when the previous one finishes
# playing. Pieces must therefore be SMALL AND UNIFORM: one big piece after a
# small one is what produced « three words, then three seconds of silence ».
# 36 characters is ~2 s of speech.
PIECE_MAX_CHARS = 36
# Every piece has the SAME size, and 36 characters is the measured compromise.
# This model generates ~1 s of audio per second of compute, so generation and
# playback advance at the same speed and the buffer stays at one piece. Pieces
# that are too small pay a fixed per-piece cost (prompt encode + tail) that
# makes generation SLOWER than playback: at 28 characters the buffer drained and
# silences of 1.3-2 s appeared; at 16 it was 2 s. At 36 the worst silence
# measured end to end is 0. The price is the first sound, which is one piece's
# generation time (~2-3 s). Small pieces give a faster start but gaps; big ones
# the opposite. Only a faster engine removes both.
CLAUSE_GAP_S = 0.05


def _split_clauses(text: str) -> list[str]:
    """Cut into small, uniform pieces (~2 s of speech) so playback never waits.

    Splitting at punctuation alone is not enough: a clause can be 8 s long, and
    the model needs 8 s to generate it. Every piece is therefore capped at
    PIECE_MAX_CHARS, cut at a space when possible.
    """
    import re as _re
    rough = [p.strip() for p in _re.split(r"(?<=[,;:!?])\s+", text) if p.strip()]
    out: list[str] = []
    for part in rough:
        while len(part) > PIECE_MAX_CHARS:
            cut = part.rfind(" ", 0, PIECE_MAX_CHARS)
            cut = cut if cut > PIECE_MAX_CHARS // 2 else PIECE_MAX_CHARS
            out.append(part[:cut].strip())
            part = part[cut:].strip()
        if part:
            out.append(part)
    # A very short tail glued to the previous piece costs nothing and sounds
    # better than a two-word burst.
    if len(out) >= 2 and len(out[-1]) < 12:
        out[-2] = f"{out[-2]} {out[-1]}"
        out.pop()
    return out or [text]


@app.post("/api/speak")
async def speak(body: dict) -> Response:
    """One sentence -> raw PCM s16le, streamed clause by clause.

    The portal sends ONE sentence per call and prefetches the next while the
    current one plays, so answering clause by clause is what keeps the voice
    close to the text on screen (RTF ~1.2 for the 1.7B fine-tune).
    """
    if not FINETUNE_DIR:
        raise HTTPException(status_code=404, detail="no fine-tuned speaker loaded")
    text = " ".join(str(body.get("text", "")).split())
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    if len(text) > 1000:
        raise HTTPException(status_code=413, detail="text longer than 1000 characters")
    pieces = _split_clauses(text)

    async def stream():
        import numpy as _np
        loop = asyncio.get_running_loop()
        for i, piece in enumerate(pieces):
            if True:
                try:
                    wav, sr = await asyncio.wait_for(
                        loop.run_in_executor(None, _exclusive, _speak, piece, None), timeout=GEN_TIMEOUT_S)
                except asyncio.TimeoutError:
                    log.error("speak timed out on %r", piece[:40])
                    return
                except Exception:
                    log.exception("Speak failed")
                    return
            if i:
                yield (_np.zeros(int(CLAUSE_GAP_S * sr), dtype=_np.float32) * 32767).astype("<i2").tobytes()
            yield (_np.clip(wav, -1, 1) * 32767).astype("<i2").tobytes()

    return StreamingResponse(stream(), media_type="application/octet-stream",
                             headers={"X-Sample-Rate": "24000", "X-Sample-Format": "s16le"})


@app.post("/tts")
async def tts(body: dict) -> Response:
    """Raw PCM/wav for the portal's /v1/audio/speech route."""
    if not FINETUNE_DIR:
        raise HTTPException(status_code=404, detail="no fine-tuned speaker loaded")
    text = " ".join(str(body.get("text", "")).split())
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(status_code=413, detail="Texte à lire trop long.")
    # One autoregressive call over up to MAX_TEXT_CHARS took minutes (the file's
    # own measurement: ~1400 characters exceeded 6 min), so the whole request used
    # to time out and leave an orphaned generation. Chunk like /clone does: each
    # piece finishes well inside GEN_TIMEOUT_S and the audio is concatenated.
    pieces = _chunk_text(text)
    try:
        chunks_out = []
        for piece in pieces:
            one, sr = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, _exclusive, _speak, piece, None),
                timeout=GEN_TIMEOUT_S)
            chunks_out.append(one)
        wav = chunks_out[0] if len(chunks_out) == 1 else np.concatenate(chunks_out)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Génération trop longue.")
    except Exception:
        log.exception("TTS failed")
        raise HTTPException(status_code=500, detail="Échec de la génération.")
    fmt = str(body.get("output_format", "wav")).lower()
    if fmt == "pcm":
        import numpy as _np
        return Response(content=(_np.clip(wav, -1, 1) * 32767).astype("<i2").tobytes(),
                        media_type="application/octet-stream",
                        headers={"X-Sample-Rate": str(sr), "X-Sample-Format": "s16le"})
    buf = io.BytesIO()
    sf.write(buf, wav, sr, format="WAV", subtype="PCM_16")
    if fmt == "wav":
        return Response(content=buf.getvalue(), media_type="audio/wav")
    import subprocess
    mp3 = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "wav", "-i", "pipe:0",
                          "-f", "mp3", "-b:a", "128k", "pipe:1"],
                         input=buf.getvalue(), capture_output=True, timeout=60, check=False)
    if mp3.returncode != 0:
        return Response(content=buf.getvalue(), media_type="audio/wav")
    return Response(content=mp3.stdout, media_type="audio/mpeg")
