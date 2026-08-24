"""Minimal text-to-image server around a diffusers Krea-2 pipeline.

The model directory is mounted read-only at /model (downloaded on the host).
Generation is serialised behind a lock (single GPU); a request returns the PNG
directly. The portal wraps this in its own async job (thread + DB), so the
sidecar itself stays dead-simple.
"""
import io
import os
import threading

import torch
from fastapi import FastAPI, Form
from fastapi.responses import JSONResponse, Response

MODEL_DIR = os.environ.get("MODEL_DIR", "/model")
# Turbo is distilled for few-step inference; Raw needs many more steps.
DEFAULT_STEPS = int(os.environ.get("IMAGE_STEPS", "8"))
# Krea-2-Turbo is distilled for few-step inference: high CFG oversaturates it
# (tested — 3.5 gave lurid colours, 1.0 is natural). Keep guidance low.
DEFAULT_GUIDANCE = float(os.environ.get("IMAGE_GUIDANCE", "1.0"))

app = FastAPI()
_gpu_lock = threading.Lock()
_pipe = None
_load_error = None
_model_name = os.environ.get("MODEL_NAME") or os.path.basename(MODEL_DIR.rstrip("/")) or "krea-2"


def _load_pipeline():
    global _pipe, _load_error
    try:
        # from_pretrained reads model_index.json to pick the right pipeline class
        # (Krea2Pipeline / Krea2Turbo…); AutoPipeline covers both.
        try:
            from diffusers import AutoPipelineForText2Image as _Auto
            pipe = _Auto.from_pretrained(MODEL_DIR, torch_dtype=torch.bfloat16)
        except Exception:
            from diffusers import Krea2Pipeline as _Krea
            pipe = _Krea.from_pretrained(MODEL_DIR, torch_dtype=torch.bfloat16)
        pipe = pipe.to("cuda")
        try:
            pipe.set_progress_bar_config(disable=True)
        except Exception:
            pass
        globals()["_pipe"] = pipe
    except Exception as e:  # keep the error so /health can report it
        globals()["_load_error"] = f"{type(e).__name__}: {e}"


threading.Thread(target=_load_pipeline, daemon=True).start()


@app.get("/health")
def health():
    return {"ready": _pipe is not None, "loading": _pipe is None and _load_error is None,
            "error": _load_error, "model": _model_name}


@app.get("/model-info")
def model_info():
    return {"model": _model_name, "ready": _pipe is not None}


@app.post("/generate")
def generate(prompt: str = Form(...),
             steps: int = Form(DEFAULT_STEPS),
             guidance: float = Form(DEFAULT_GUIDANCE),
             width: int = Form(1024),
             height: int = Form(1024)):
    if _pipe is None:
        return JSONResponse({"error": _load_error or "model still loading"}, status_code=503)
    prompt = (prompt or "").strip()[:10000]
    if not prompt:
        return JSONResponse({"error": "empty prompt"}, status_code=400)
    steps = max(1, min(80, int(steps)))
    width = max(256, min(1536, (int(width) // 8) * 8))
    height = max(256, min(1536, (int(height) // 8) * 8))
    try:
        with _gpu_lock:
            with torch.inference_mode():
                out = _pipe(prompt, num_inference_steps=steps, guidance_scale=float(guidance),
                            width=width, height=height)
            image = out.images[0]
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return Response(buf.getvalue(), media_type="image/png")
    except torch.cuda.OutOfMemoryError:
        torch.cuda.empty_cache()
        return JSONResponse({"error": "GPU out of memory"}, status_code=507)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
