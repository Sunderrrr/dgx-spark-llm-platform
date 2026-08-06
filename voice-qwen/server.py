"""Service HTTP minimal autour de Qwen3-TTS (clonage de voix zero-shot).

Qwen ne fournit pas de serveur : vLLM-Omni ne fait pour l'instant que de
l'inférence offline, et le seul serveur amont est une démo Gradio. On expose
donc nous-mêmes le strict nécessaire pour dgx-portal.

Différence volontaire avec le service Chatterbox : UN SEUL appel multipart
(/clone) au lieu de « uploader la référence puis générer ». Chatterbox
gardait le clip de référence sur disque sans jamais le supprimer (il a fallu
lui ajouter une purge par TTL) ; ici l'audio ne quitte jamais la mémoire.
"""
import asyncio
import io
import logging
import os
import re

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("qwen3-tts")

MODEL_ID = os.environ.get("QWEN_TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-Base")
# Chatterbox refusait tout clip <= 5 s ; Qwen annonce un clonage dès 3 s. On
# garde une borne basse explicite pour renvoyer un message clair plutôt que de
# laisser le modèle produire n'importe quoi.
MIN_REF_SECONDS = float(os.environ.get("QWEN_TTS_MIN_REF_SECONDS", "3"))
MAX_REF_SECONDS = float(os.environ.get("QWEN_TTS_MAX_REF_SECONDS", "90"))
# Anti-bombe de décompression (cf. asr/server.py) : borne l'octet et lit
# l'en-tête avant de décoder l'échantillon de référence.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_SR = 48000
MAX_CH = 2
# Borne dure du texte à lire : au-delà, une seule séquence autorégressive tient
# le verrou GPU plusieurs minutes (le portail abandonne à 180 s mais la
# génération, non annulable, continue). Le portail plafonne déjà à 2000.
MAX_TEXT_CHARS = 3000
GEN_TIMEOUT_S = float(os.environ.get("QWEN_TTS_GEN_TIMEOUT", "240"))

app = FastAPI(title="Cronos Qwen3-TTS", docs_url=None, redoc_url=None, openapi_url=None)

_model = None
_languages: dict[str, str] = {}
_load_error: str | None = None
# Un seul modèle sur un seul GPU : deux générations simultanées se marcheraient
# dessus. Le verrou les met en file plutôt que de les laisser échouer.
_gpu_lock = asyncio.Lock()


def _attn_impl() -> str:
    """flash_attention_2 est recommandé mais ne se compile pas partout (ARM64
    notamment) — on ne le prend que s'il est réellement importable, sinon
    l'attention native PyTorch, qui donne le même résultat en un peu plus lent."""
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
        log.info("Loading %s (attn=%s)…", MODEL_ID, impl)
        _model = Qwen3TTSModel.from_pretrained(
            MODEL_ID,
            device_map="cuda:0" if torch.cuda.is_available() else "cpu",
            dtype=torch.bfloat16,
            attn_implementation=impl,
        )
        # Qwen attend le NOM de la langue, en minuscules ("french"), pas un
        # code ISO. On construit code -> nom pour que le portail garde des
        # codes en interne. Tout nom inconnu est ignoré plutôt que tronqué à
        # ses deux premières lettres, qui produisait des codes faux ("sp"
        # pour spanish, "ge" pour german…).
        names = list(_model.get_supported_languages())
        _languages = {_ISO[n.lower()]: n for n in names if n.lower() in _ISO}
        skipped = [n for n in names if n.lower() not in _ISO]
        if skipped:
            log.warning("Langues sans code ISO connu, ignorées : %s", skipped)
        log.info("Loaded. %d languages: %s", len(_languages), ", ".join(sorted(_languages)))
    except Exception as exc:  # pragma: no cover - dépend du runtime CUDA
        _load_error = str(exc)
        log.exception("Model failed to load")


# Qwen renvoie les noms en minuscules. « auto » n'est pas une langue mais la
# détection automatique — on la garde et on s'en sert par défaut, c'est plus
# robuste qu'imposer un choix à l'utilisateur.
_ISO = {
    "auto": "auto",
    "chinese": "zh", "english": "en", "japanese": "ja", "korean": "ko",
    "german": "de", "french": "fr", "russian": "ru", "portuguese": "pt",
    "spanish": "es", "italian": "it",
}


# Un texte long envoyé d'un bloc est généré en une seule séquence
# autorégressive, dont le coût croît bien plus vite que linéairement : mesuré
# ici, ~330 caractères prennent 17 s alors qu'un discours de ~1400 dépassait
# 6 minutes et faisait expirer la requête côté portail. On découpe donc comme
# le fait le serveur Chatterbox, en respectant les fins de phrase.
_CHUNK_TARGET = 250
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…:;])\s+|\n+")


# Borne DURE par morceau : un texte sans aucune ponctuation (donc une seule
# « phrase » pour _SENTENCE_SPLIT) restait entier et repartait en une séquence
# autorégressive interminable. Au-delà de cette longueur on coupe, de préférence
# sur un espace proche, pour garantir que chaque morceau reste borné.
_CHUNK_HARD_MAX = 400


def _hard_split(part: str) -> list[str]:
    out = []
    while len(part) > _CHUNK_HARD_MAX:
        cut = part.rfind(" ", 0, _CHUNK_HARD_MAX)
        if cut <= 0:
            cut = _CHUNK_HARD_MAX  # mot unique démesuré : on coupe net
        out.append(part[:cut].strip())
        part = part[cut:].strip()
    if part:
        out.append(part)
    return out


def _chunk_text(text: str) -> list[str]:
    pieces, cur = [], ""
    for part in (p.strip() for p in _SENTENCE_SPLIT.split(text) if p and p.strip()):
        for sub in _hard_split(part):
            if cur and len(cur) + 1 + len(sub) > _CHUNK_TARGET:
                pieces.append(cur)
                cur = sub
            else:
                cur = f"{cur} {sub}".strip()
    if cur:
        pieces.append(cur)
    return pieces or [text]


def _join(wavs: list, sr: int):
    """Concatène les morceaux avec une courte pause, comme entre deux phrases."""
    if len(wavs) == 1:
        return np.asarray(wavs[0], dtype=np.float32)
    gap = np.zeros(int(0.18 * sr), dtype=np.float32)
    out = []
    for i, w in enumerate(wavs):
        out.append(np.asarray(w, dtype=np.float32).squeeze())
        if i < len(wavs) - 1:
            out.append(gap)
    return np.concatenate(out)


@app.get("/api/model-info")
def model_info() -> JSONResponse:
    return JSONResponse({
        "loaded": _model is not None,
        "type": MODEL_ID.rsplit("/", 1)[-1],
        "engine": "qwen3-tts",
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
    # Transcription du clip de référence. Fournie => qualité maximale ;
    # absente => x_vector_only_mode, qui n'utilise que l'empreinte du locuteur
    # (Qwen documente une qualité moindre dans ce cas).
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
        # En-tête d'abord (durée/fréquence/canaux) puis décodage, hors boucle
        # d'évènements : un fichier piégé ne peut ni exploser la RAM ni bloquer
        # le service.
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

    # Repli sur la détection automatique plutôt que sur l'anglais : une langue
    # inconnue générait sinon du français lu avec une phonétique anglaise.
    lang_name = _languages.get(language) or _languages.get("auto") or _languages.get("en")
    transcript = ref_text.strip()
    chunks = _chunk_text(text)

    def _run():
        # On construit le prompt de référence UNE fois puis on génère tous les
        # morceaux d'un coup : Qwen ne réextrait pas les features de la voix à
        # chaque appel, et surtout aucune séquence n'est très longue.
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
        # generate_voice_clone est bloquant (GPU) : l'appeler directement dans
        # cette coroutine gelait toute la boucle d'évènements, au point que
        # /api/model-info ne répondait plus pendant une génération — le portail
        # concluait alors « service injoignable » et l'admin voyait le backend
        # hors ligne. On l'exécute donc dans un thread, sous verrou.
        async with _gpu_lock:
            # Borne dure : une génération partie en vrille ne garde pas le verrou
            # GPU indéfiniment (le portail a déjà abandonné à 180 s de son côté).
            wavs, out_sr = await asyncio.wait_for(run_in_threadpool(_run), timeout=GEN_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Génération trop longue, réessaie avec un texte plus court.")
    except Exception:
        # Détail journalisé côté serveur, jamais renvoyé au client (chemins,
        # cache HF, internals torch/qwen).
        log.exception("Generation failed")
        raise HTTPException(status_code=500, detail="Échec de la génération.")

    audio_out = _join(list(wavs), out_sr)
    buf = io.BytesIO()
    sf.write(buf, audio_out, out_sr, format="WAV", subtype="PCM_16")
    return Response(content=buf.getvalue(), media_type="audio/wav")
