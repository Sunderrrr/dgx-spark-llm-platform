"""Serveur HTTP minimal de génération musicale (diffusers).

Même posture que le sidecar image : le portail encapsule l'appel dans son
propre job asynchrone (thread + DB), donc ce service reste volontairement
simple — une requête, un WAV. Le modèle est indiqué par MUSIC_MODEL (id
HuggingFace) et téléchargé au démarrage dans le cache HF monté.
"""
import io
import os
import threading

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, Form
from fastapi.responses import JSONResponse, Response

MODEL_ID = os.environ.get("MUSIC_MODEL", "MiniMaxAI/MiniMax-Music3")
# Bornes de durée : au-delà de 5 min le modèle n'est pas entraîné, et une
# génération très longue monopoliserait le GPU (partagé avec le chat).
MAX_SECONDS = float(os.environ.get("MUSIC_MAX_SECONDS", "300"))
DEFAULT_SECONDS = float(os.environ.get("MUSIC_DEFAULT_SECONDS", "60"))
# Quantification : "8bit" (défaut), "4bit" ou "none". Le modèle est un
# empilement de 6 composants (47 Go sur disque, ~24 Go en bf16) dont deux LLM
# pèsent à eux seuls 35,7 Go — les quantifier suffit à faire tenir l'ensemble
# à côté du modèle de chat sur la mémoire unifiée du GB10. Les petits modules
# audio (vocodeur, décodeur RVQ) restent pleine précision : ils ne coûtent
# presque rien et portent la qualité du son.
QUANT = os.environ.get("MUSIC_QUANT", "8bit").lower()
# Les deux poids lourds : le LLM global (17,2 Go) et l'encodeur de condition
# (un Qwen-7B complet, 18,5 Go) — soit 35,7 des 47 Go du dépôt.
QUANT_COMPONENTS = [c.strip() for c in os.environ.get(
    "MUSIC_QUANT_COMPONENTS", "language_model,condition_encoder").split(",") if c.strip()]

app = FastAPI()
_gpu_lock = threading.Lock()   # une génération à la fois (GPU unique)
_pipe = None
_load_error = None
_sr = 32000


def _load_pipeline():
    global _pipe, _load_error, _sr
    try:
        from diffusers import ModularPipeline
        pipe = ModularPipeline.from_pretrained(MODEL_ID)

        if QUANT in ("8bit", "4bit"):
            # En deux passes : la config de quantification est transmise TELLE
            # QUELLE à chaque composant chargé, donc on ne l'applique qu'aux
            # deux gros LLM. (Une config au niveau pipeline échoue sur les
            # petits modules audio : « no attribute quant_method ».)
            from transformers import BitsAndBytesConfig
            bnb = BitsAndBytesConfig(**{f"load_in_{QUANT}": True})
            targets = [c for c in QUANT_COMPONENTS if c in pipe.component_names]
            rest = [c for c in pipe.component_names if c not in targets]
            pipe.load_components(names=targets, quantization_config=bnb, dtype=torch.bfloat16)
            pipe.load_components(names=rest, dtype=torch.bfloat16)
        else:
            pipe.load_components(dtype=torch.bfloat16)

        # bitsandbytes pose déjà ses poids sur le GPU et refuse un .to() : on
        # déplace composant par composant pour ne pas interrompre les autres.
        for name in pipe.component_names:
            comp = getattr(pipe, name, None)
            if comp is not None and hasattr(comp, "to") and not getattr(comp, "is_quantized", False):
                try:
                    comp.to("cuda")
                except Exception:
                    pass

        # Honnêteté de /health : diffusers journalise l'échec d'un composant
        # sans forcément lever, on refuserait sinon de servir un pipeline
        # incomplet en annonçant « prêt » (bug observé : vocodeur manquant).
        missing = list(getattr(pipe, "null_component_names", []) or [])
        if missing:
            raise RuntimeError("composants non chargés : " + ", ".join(missing))
        _sr = int(getattr(pipe, "sampling_rate", 32000) or 32000)
        globals()["_pipe"] = pipe
    except Exception as e:  # conservé pour /health, jamais renvoyé brut au client
        globals()["_load_error"] = f"{type(e).__name__}: {e}"


threading.Thread(target=_load_pipeline, daemon=True).start()


@app.get("/health")
def health():
    return {"ready": _pipe is not None,
            "loading": _pipe is None and _load_error is None,
            "error": _load_error, "model": MODEL_ID, "quant": QUANT}


@app.get("/model-info")
def model_info():
    return {"model": MODEL_ID, "ready": _pipe is not None, "sampling_rate": _sr}


@app.post("/generate")
def generate(prompt: str = Form(...),
             lyrics: str = Form(""),
             duration: float = Form(DEFAULT_SECONDS),
             seed: int = Form(-1)):
    if _pipe is None:
        return JSONResponse({"error": _load_error or "modèle en cours de chargement"},
                            status_code=503)
    prompt = (prompt or "").strip()[:4000]
    if not prompt:
        return JSONResponse({"error": "description musicale requise"}, status_code=400)
    lyrics = (lyrics or "").strip()[:10000]
    duration = max(5.0, min(MAX_SECONDS, float(duration)))

    try:
        with _gpu_lock:
            gen = None
            if seed is not None and int(seed) >= 0:
                gen = torch.Generator("cuda").manual_seed(int(seed))
            with torch.inference_mode():
                out = _pipe(prompt=prompt, lyrics=lyrics, audio_duration=duration,
                            generator=gen, output="audios")
            audio = out[0]
        # (canaux, échantillons) côté modèle → (échantillons, canaux) pour soundfile
        data = audio.T.float().cpu().numpy() if hasattr(audio, "cpu") else np.asarray(audio).T
        buf = io.BytesIO()
        sf.write(buf, data, _sr, format="WAV", subtype="PCM_16")
        return Response(buf.getvalue(), media_type="audio/wav")
    except torch.cuda.OutOfMemoryError:
        torch.cuda.empty_cache()
        return JSONResponse({"error": "mémoire GPU insuffisante"}, status_code=507)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
