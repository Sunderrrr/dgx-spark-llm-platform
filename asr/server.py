"""Service HTTP minimal de transcription (Whisper), pour la dictée.

Pourquoi auto-hébergé plutôt que l'API SpeechRecognition du navigateur :
Chrome envoie par défaut l'audio à ses serveurs pour le reconnaître. Sur une
plateforme dont tout l'intérêt est que rien ne sorte de la machine, ce n'est
pas acceptable. Le mode « on-device » de Chrome 139+ existe mais dépend du
navigateur et de la plateforme ; ici la transcription tourne sur le GPU local,
pour tous les navigateurs.

On reste sur transformers + torch cu130, la seule pile validée sur ce GB10
(aarch64, sm_121) — faster-whisper/CTranslate2 n'y a pas de roue.
"""
import io
import logging
import os

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("asr")

# turbo : qualité proche de large-v3 pour une fraction du temps et de la
# mémoire, ce qui compte sur une machine qui fait déjà tourner le chat, l'OCR,
# la vidéo et la voix.
MODEL_ID = os.environ.get("ASR_MODEL", "openai/whisper-large-v3-turbo")
TARGET_SR = 16000  # Whisper travaille en 16 kHz
MAX_SECONDS = float(os.environ.get("ASR_MAX_SECONDS", "300"))

app = FastAPI(title="Cronos ASR")

_pipe = None
_load_error: str | None = None


@app.on_event("startup")
def _load() -> None:
    global _pipe, _load_error
    try:
        from transformers import pipeline

        cuda = torch.cuda.is_available()
        log.info("Loading %s (cuda=%s)…", MODEL_ID, cuda)
        _pipe = pipeline(
            "automatic-speech-recognition",
            model=MODEL_ID,
            device="cuda:0" if cuda else "cpu",
            torch_dtype=torch.float16 if cuda else torch.float32,
            # Découpe les enregistrements longs : Whisper ne prend que 30 s
            # par passe, sans ça tout ce qui dépasse est silencieusement coupé.
            chunk_length_s=30,
        )
        log.info("Loaded.")
    except Exception as exc:  # pragma: no cover - dépend du runtime CUDA
        _load_error = str(exc)
        log.exception("Model failed to load")


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
    # Forcer la langue évite que Whisper « détecte » de l'anglais sur une
    # phrase française courte, son erreur classique.
    language: str = Form(""),
) -> JSONResponse:
    if _pipe is None:
        raise HTTPException(status_code=503, detail=_load_error or "Modèle non chargé.")

    raw = await audio.read()
    try:
        data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Audio illisible.")
    data = data.mean(axis=1)  # mono

    seconds = len(data) / float(sr)
    if seconds < 0.3:
        raise HTTPException(status_code=400, detail="Enregistrement trop court.")
    if seconds > MAX_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"Enregistrement trop long ({seconds:.0f}s, maximum {MAX_SECONDS:.0f}s).")

    if sr != TARGET_SR:
        # Rééchantillonnage linéaire : suffisant pour de la parole, et évite
        # une dépendance de plus (librosa/resampy) dans cette image.
        n = int(round(len(data) * TARGET_SR / sr))
        data = np.interp(
            np.linspace(0, len(data) - 1, n, dtype=np.float64),
            np.arange(len(data), dtype=np.float64),
            data.astype(np.float64),
        ).astype(np.float32)

    kwargs = {}
    if language:
        kwargs["generate_kwargs"] = {"language": language}

    try:
        # Appel bloquant (GPU) : le sortir de la boucle d'évènements, sinon
        # /api/model-info ne répond plus pendant une transcription et le
        # portail conclut que le backend est hors ligne.
        out = await run_in_threadpool(lambda: _pipe({"raw": data, "sampling_rate": TARGET_SR}, **kwargs))
    except Exception as exc:
        log.exception("Transcription failed")
        raise HTTPException(status_code=500, detail=f"Échec de la transcription : {exc}")

    return JSONResponse({"text": (out.get("text") or "").strip()})
