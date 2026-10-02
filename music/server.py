"""Minimal HTTP music generation server (diffusers).

Same posture as the image sidecar: the portal wraps the call in its own
asynchronous job (thread + DB), so this service stays deliberately simple —
one request, one WAV. The model is given by MUSIC_MODEL (HuggingFace id) and
downloaded at startup into the mounted HF cache.
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
# Duration bounds: beyond 5 min the model is not trained for it, and a very
# long generation would monopolize the GPU (shared with chat).
MAX_SECONDS = float(os.environ.get("MUSIC_MAX_SECONDS", "300"))
DEFAULT_SECONDS = float(os.environ.get("MUSIC_DEFAULT_SECONDS", "60"))
# Quantization: "8bit" (default), "4bit" or "none". The model is a stack of
# 6 components (47 GB on disk, ~24 GB in bf16) of which two LLMs alone weigh
# 35.7 GB — quantizing them is enough to fit the whole thing next to the chat
# model on the GB10's unified memory. The small audio modules (vocoder, RVQ
# decoder) stay at full precision: they cost almost nothing and carry the sound
# quality.
QUANT = os.environ.get("MUSIC_QUANT", "8bit").lower()
# The two heavyweights: the global LLM (17.2 GB) and the condition encoder
# (a full Qwen-7B, 18.5 GB) — i.e. 35.7 of the 47 GB of the repo.
QUANT_COMPONENTS = [c.strip() for c in os.environ.get(
    "MUSIC_QUANT_COMPONENTS", "language_model,condition_encoder").split(",") if c.strip()]

app = FastAPI()
_gpu_lock = threading.Lock()   # one generation at a time (single GPU)
_pipe = None
_load_error = None
_sr = 32000


def _load_pipeline():
    global _pipe, _load_error, _sr
    try:
        from diffusers import ModularPipeline
        pipe = ModularPipeline.from_pretrained(MODEL_ID)

        if QUANT in ("8bit", "4bit"):
            # In two passes: the quantization config is passed AS-IS to every
            # component being loaded, so we only apply it to the two big LLMs.
            # (A pipeline-level config fails on the small audio modules:
            # « no attribute quant_method ».)
            from transformers import BitsAndBytesConfig
            bnb = BitsAndBytesConfig(**{f"load_in_{QUANT}": True})
            targets = [c for c in QUANT_COMPONENTS if c in pipe.component_names]
            rest = [c for c in pipe.component_names if c not in targets]
            pipe.load_components(names=targets, quantization_config=bnb, dtype=torch.bfloat16)
            pipe.load_components(names=rest, dtype=torch.bfloat16)
        else:
            pipe.load_components(dtype=torch.bfloat16)

        # bitsandbytes already places its weights on the GPU and refuses a
        # .to(): we move component by component so as not to interrupt the others.
        for name in pipe.component_names:
            comp = getattr(pipe, name, None)
            if comp is not None and hasattr(comp, "to") and not getattr(comp, "is_quantized", False):
                try:
                    comp.to("cuda")
                except Exception:
                    pass

        # /health honesty: diffusers logs a component failure without
        # necessarily raising — we would otherwise serve an incomplete pipeline
        # while announcing "ready" (bug observed: missing vocoder).
        missing = list(getattr(pipe, "null_component_names", []) or [])
        if missing:
            raise RuntimeError("composants non chargés : " + ", ".join(missing))
        _sr = int(getattr(pipe, "sampling_rate", 32000) or 32000)
        globals()["_pipe"] = pipe
    except Exception as e:  # kept for /health, never returned raw to the client
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
        # (channels, samples) model-side → (samples, channels) for soundfile
        data = audio.T.float().cpu().numpy() if hasattr(audio, "cpu") else np.asarray(audio).T
        buf = io.BytesIO()
        sf.write(buf, data, _sr, format="WAV", subtype="PCM_16")
        return Response(buf.getvalue(), media_type="audio/wav")
    except torch.cuda.OutOfMemoryError:
        torch.cuda.empty_cache()
        return JSONResponse({"error": "mémoire GPU insuffisante"}, status_code=507)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
