"""Minimal text-to-image server around a diffusers pipeline.

The model folder is mounted read-only on /model (downloaded on the host).
Generation is serialized behind a lock (single GPU); one request returns the
PNG directly. The portal wraps this in its own asynchronous job (thread + DB),
so the sidecar stays deliberately simple.

Deliberately MODEL-AGNOSTIC: the pipeline class is read from model_index.json
by DiffusionPipeline, and the output is normalized, because pipelines do not
all return the same thing (see _premiere_image).
"""
import io
import os
import threading

import torch
from fastapi import FastAPI, Form
from fastapi.responses import JSONResponse, Response
from PIL import Image, ImageFilter

import esrgan

MODEL_DIR = os.environ.get("MODEL_DIR", "/model")
# Default values provided by image-recreate.sh, which picks them per model: a
# distilled model is happy with ~8 steps at guidance 1.0, a full model needs 35
# to 50 with guidance 4 to 6. No value here suits both — hence the environment.
DEFAULT_STEPS = int(os.environ.get("IMAGE_STEPS", "35"))
DEFAULT_GUIDANCE = float(os.environ.get("IMAGE_GUIDANCE", "4.0"))

# Accepted output formats. PNG = historical default; JPEG loses alpha (we
# convert to RGB) but weighs less; WebP keeps alpha and weighs the least. The
# portal already validates the value; we re-normalize here for safety (jpg alias).
FORMAT_PIL = {'png': 'PNG', 'jpeg': 'JPEG', 'jpg': 'JPEG', 'webp': 'WEBP'}
FORMAT_MIME = {'png': 'image/png', 'jpeg': 'image/jpeg', 'jpg': 'image/jpeg', 'webp': 'image/webp'}

# Real-ESRGAN (x4 super-resolution) for « 4K » outputs. Weights mounted
# read-only on /esrgan. Loaded lazily (only once) at the first upscale above
# the threshold; the network is small (~64 MB) and runs on the same GPU as the
# pipeline.
ESRGAN_PATH = os.environ.get("ESRGAN_PATH", "/esrgan/RealESRGAN_x4plus.pth")
ESRGAN_SCALE_THRESHOLD = 1.6  # beyond ~1.6x, prefer ESRGAN over Lanczos
_esrgan = None
_esrgan_error = None
_esrgan_lock = threading.Lock()


def _get_esrgan():
    """Return the Real-ESRGAN network (loaded on demand) or None if unavailable."""
    global _esrgan, _esrgan_error
    if _esrgan is not None:
        return _esrgan
    with _esrgan_lock:
        if _esrgan is not None:
            return _esrgan
        try:
            if not os.path.isfile(ESRGAN_PATH):
                _esrgan_error = f"poids absents: {ESRGAN_PATH}"
            else:
                _esrgan = esrgan.load_esrgan(ESRGAN_PATH, "cuda")
        except Exception as e:  # we degrade cleanly to Lanczos
            _esrgan_error = f"{type(e).__name__}: {e}"
        return _esrgan

app = FastAPI()
_gpu_lock = threading.Lock()
_pipe = None
_load_error = None
_model_name = os.environ.get("MODEL_NAME") or os.path.basename(MODEL_DIR.rstrip("/")) or "image"


def _load_pipeline():
    """Load the pipeline described by model_index.json.

    DiffusionPipeline reads `_class_name` and instantiates the right class: no
    hardcoded class list, a new diffusers model works without touching the code.
    AutoPipelineForText2Image would not do — its mapping table does not know
    recent pipelines (Cosmos3OmniPipeline is one of them).

    Two pitfalls specific to NF4 (bitsandbytes) pre-quantized models:
      - NEVER call .to(dtype) on them, only .to(device) ;
      - the quantization config is already in the repo, above all do not pass
        another one.
    """
    global _pipe, _load_error
    try:
        from diffusers import DiffusionPipeline

        # enable_safety_checker=False avoids requiring cosmos_guardrail, an
        # optional dependency of the Cosmos pipelines. The other pipelines do
        # not know this parameter and raise: we retry without it.
        try:
            pipe = DiffusionPipeline.from_pretrained(
                MODEL_DIR, torch_dtype=torch.bfloat16, enable_safety_checker=False)
        except TypeError:
            pipe = DiffusionPipeline.from_pretrained(MODEL_DIR, torch_dtype=torch.bfloat16)

        # .to("cuda") moves WITHOUT converting the dtype: on a 4-bit model a
        # conversion would break the quantized weights. If accelerate has already
        # sharded the model, the move fails — that is not a load error.
        try:
            pipe = pipe.to("cuda")
        except Exception:
            pass

        try:
            pipe.set_progress_bar_config(disable=True)
        except Exception:
            pass
        globals()["_pipe"] = pipe
    except Exception as e:  # keep the error so /health reports it
        globals()["_load_error"] = f"{type(e).__name__}: {e}"


threading.Thread(target=_load_pipeline, daemon=True).start()


def _premiere_image(out):
    """Grab the first image, whatever the pipeline returns.

    Classic text-to-image pipelines return `.images` (list of PIL.Image). The
    Cosmos 3 pipelines are omni-modal and return `.video`: a list of sequences,
    of which the first holds a single frame in text-to-image mode. Without this
    untangling, generation succeeded on the GPU then failed at save time.
    """
    images = getattr(out, "images", None)
    if images:
        return images[0]
    video = getattr(out, "video", None)
    if video:
        premiere = video[0]
        # Frame sequence, or single frame already unwrapped.
        return premiere[0] if isinstance(premiere, (list, tuple)) else premiere
    if isinstance(out, (list, tuple)) and out:
        return out[0]
    raise RuntimeError("le pipeline n'a renvoye ni .images ni .video")


@app.get("/health")
def health():
    return {"ready": _pipe is not None, "loading": _pipe is None and _load_error is None,
            "error": _load_error, "model": _model_name}


@app.get("/model-info")
def model_info():
    return {"model": _model_name, "ready": _pipe is not None}


def _upscale(image, out_w, out_h):
    """Upscale towards out_w x out_h in high quality.

    Small bump (Full HD, ~1.25x): Lanczos + unsharp is enough. Large bump
    (4K, >1.6x): Real-ESRGAN x4 (detail synthesis) then Lanczos adjustment to
    the exact size. The centered crop is a safety net (identical ratios in
    practice, so a no-op).
    """
    if image.width >= out_w and image.height >= out_h:
        return image
    scale = max(out_w / image.width, out_h / image.height)
    if scale > ESRGAN_SCALE_THRESHOLD:
        net = _get_esrgan()
        if net is not None:
            image = esrgan.upscale_4x(net, image)
    # Final adjustment to the exact size (cover + crop), which also handles the
    # downscale when ESRGAN overshot (4x > target).
    scale = max(out_w / image.width, out_h / image.height)
    w = max(out_w, round(image.width * scale))
    h = max(out_h, round(image.height * scale))
    img = image.resize((w, h), Image.LANCZOS)
    left = (w - out_w) // 2
    top = (h - out_h) // 2
    img = img.crop((left, top, left + out_w, top + out_h))
    return img.filter(ImageFilter.UnsharpMask(radius=2, percent=80, threshold=3))


@app.post("/generate")
def generate(prompt: str = Form(...),
             steps: int = Form(DEFAULT_STEPS),
             guidance: float = Form(DEFAULT_GUIDANCE),
             width: int = Form(1024),
             height: int = Form(1024),
             out_width: int = Form(0),
             out_height: int = Form(0),
             format: str = Form("png")):
    if _pipe is None:
        return JSONResponse({"error": _load_error or "model still loading"}, status_code=503)
    prompt = (prompt or "").strip()[:10000]
    if not prompt:
        return JSONResponse({"error": "empty prompt"}, status_code=400)
    fmt_key = (format or "png").strip().lower()
    fmt = FORMAT_PIL.get(fmt_key, "PNG")
    steps = max(1, min(80, int(steps)))
    width = max(256, min(1536, (int(width) // 8) * 8))
    height = max(256, min(1536, (int(height) // 8) * 8))
    # Output size: if requested and larger than the native generation, we
    # upscale (Lanczos + unsharp). Bounded at 3840 (4K) per side.
    out_width = max(0, min(3840, int(out_width or 0)))
    out_height = max(0, min(3840, int(out_height or 0)))
    try:
        with _gpu_lock:
            with torch.inference_mode():
                # prompt= as a NAMED argument, never positional: pipelines that
                # can also edit an image (Flux2KleinPipeline among others) expect
                # the image in first position, and a positional prompt lands
                # there as image -> « Provide either `prompt` or
                # `prompt_embeds` ». Observed on 24/08 on FLUX.2 Klein 4B.
                out = _pipe(prompt=prompt, num_inference_steps=steps,
                            guidance_scale=float(guidance), width=width, height=height)
            image = _premiere_image(out)
        if out_width and out_height:
            image = _upscale(image, out_width, out_height)
        # JPEG cannot encode an alpha channel: we flatten to RGB first.
        if fmt == "JPEG" and image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGB")
        buf = io.BytesIO()
        image.save(buf, format=fmt)
        return Response(buf.getvalue(), media_type=FORMAT_MIME.get(fmt_key, "image/png"))
    except torch.cuda.OutOfMemoryError:
        torch.cuda.empty_cache()
        return JSONResponse({"error": "GPU out of memory"}, status_code=507)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
