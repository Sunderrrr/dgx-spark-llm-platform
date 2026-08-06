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
import asyncio
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
# Plafonds anti-bombe de décompression : un FLAC de quelques Ko peut se décoder
# en dizaines de Go en float32 (ratio > 1000x) et déclencher l'OOM killer sur
# une machine à mémoire unifiée. On borne l'octet ET on lit l'en-tête (durée,
# fréquence, canaux) AVANT de décoder — soundfile lit l'en-tête sans allouer.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_SR = 48000
MAX_CH = 2

# docs/openapi fermés : ce service n'est joignable que par dgx-portal sur un
# réseau docker dédié, aucune raison d'exposer un schéma explorable.
app = FastAPI(title="Cronos ASR", docs_url=None, redoc_url=None, openapi_url=None)

_pipe = None
_load_error: str | None = None
# Le threadpool anyio accepte 40 tâches : sans borne, 40 transcriptions
# pourraient frapper le même pipeline transformers (non thread-safe) et 40
# inférences Whisper le même GPU à mémoire unifiée. Un sémaphore à 2 garde un
# peu de débit pour la dictée tout en plafonnant la charge GPU.
_gpu_sem = asyncio.Semaphore(2)


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
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Fichier audio trop volumineux.")

    def _decode():
        # En-tête d'abord : rejette durée/fréquence/canaux hors bornes SANS
        # décoder les échantillons, ce qui coupe court à la bombe de
        # décompression. Puis décodage + rééchantillonnage, le tout hors de la
        # boucle d'évènements (bloquant sur un gros fichier).
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
            # Rééchantillonnage linéaire : suffisant pour de la parole, et évite
            # une dépendance de plus (librosa/resampy) dans cette image.
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
        # Appel bloquant (GPU) : le sortir de la boucle d'évènements, sinon
        # /api/model-info ne répond plus pendant une transcription et le
        # portail conclut que le backend est hors ligne.
        async with _gpu_sem:
            out = await run_in_threadpool(lambda: _pipe({"raw": data, "sampling_rate": TARGET_SR}, **kwargs))
    except Exception:
        # Le détail de l'exception (chemins du conteneur, cache HF, shapes de
        # tenseurs) est journalisé côté serveur mais jamais renvoyé à l'appelant.
        log.exception("Transcription failed")
        raise HTTPException(status_code=500, detail="Échec de la transcription.")

    text = (out.get("text") or "").strip()
    # Sur du non-parlé (silence, souffle, bruit continu), Whisper part en
    # boucle et rend des chaînes du type « . . . . . » ou « Beep! Beep! ».
    # C'est très visible en dictée en direct, où le premier tour tombe souvent
    # avant le premier mot. Un texte sans la moindre lettre ni chiffre n'est
    # pas de la parole : on renvoie vide plutôt que de polluer le champ.
    if not any(c.isalnum() for c in text):
        text = ""
    return JSONResponse({"text": text})
