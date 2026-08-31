"""Serveur texte-vers-image minimal autour d'un pipeline diffusers.

Le dossier du modele est monte en lecture seule sur /model (telecharge sur
l'hote). La generation est serialisee derriere un verrou (GPU unique) ; une
requete renvoie le PNG directement. Le portail enveloppe ca dans son propre job
asynchrone (fil + base), donc le sidecar reste volontairement simple.

Volontairement AGNOSTIQUE du modele : la classe de pipeline est lue dans
model_index.json par DiffusionPipeline, et la sortie est normalisee, parce que
les pipelines ne rendent pas tous la meme chose (cf. _premiere_image).
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
# Valeurs par defaut fournies par image-recreate.sh, qui les choisit selon le
# modele : un modele distille se contente de ~8 etapes a guidage 1.0, un modele
# complet en demande 35 a 50 avec un guidage de 4 a 6. Aucune valeur ici ne
# convient aux deux — d'ou le passage par l'environnement.
DEFAULT_STEPS = int(os.environ.get("IMAGE_STEPS", "35"))
DEFAULT_GUIDANCE = float(os.environ.get("IMAGE_GUIDANCE", "4.0"))

# Formats de sortie acceptés. PNG = défaut historique ; JPEG perd l'alpha (on
# convertit en RGB) mais pèse moins ; WebP garde l'alpha et pèse le moins. Le
# portail valide déjà la valeur ; on re-normalise ici par sécurité (alias jpg).
FORMAT_PIL = {'png': 'PNG', 'jpeg': 'JPEG', 'jpg': 'JPEG', 'webp': 'WEBP'}
FORMAT_MIME = {'png': 'image/png', 'jpeg': 'image/jpeg', 'jpg': 'image/jpeg', 'webp': 'image/webp'}

# Real-ESRGAN (super-résolution x4) pour les sorties « 4K ». Poids montés en
# lecture seule sur /esrgan. Chargé paresseusement (une seule fois) au premier
# upscale qui dépasse le seuil ; le réseau est petit (~64 Mo) et tourne sur le
# même GPU que le pipeline.
ESRGAN_PATH = os.environ.get("ESRGAN_PATH", "/esrgan/RealESRGAN_x4plus.pth")
ESRGAN_SCALE_THRESHOLD = 1.6  # au-delà de ~1.6x, on préfère ESRGAN à Lanczos
_esrgan = None
_esrgan_error = None
_esrgan_lock = threading.Lock()


def _get_esrgan():
    """Renvoie le réseau Real-ESRGAN (chargé à la demande) ou None si indisponible."""
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
        except Exception as e:  # on dégrade proprement vers Lanczos
            _esrgan_error = f"{type(e).__name__}: {e}"
        return _esrgan

app = FastAPI()
_gpu_lock = threading.Lock()
_pipe = None
_load_error = None
_model_name = os.environ.get("MODEL_NAME") or os.path.basename(MODEL_DIR.rstrip("/")) or "image"


def _load_pipeline():
    """Charge le pipeline decrit par model_index.json.

    DiffusionPipeline lit `_class_name` et instancie la bonne classe : pas de
    liste de classes en dur, un nouveau modele diffusers marche sans toucher au
    code. AutoPipelineForText2Image ne convenait pas — sa table de correspondance
    ne connait pas les pipelines recents (Cosmos3OmniPipeline en fait partie).

    Deux pieges specifiques aux modeles pre-quantifies NF4 (bitsandbytes) :
      - ne JAMAIS appeler .to(dtype) dessus, seulement .to(device) ;
      - la configuration de quantification est deja dans le depot, il ne faut
        surtout pas en passer une autre.
    """
    global _pipe, _load_error
    try:
        from diffusers import DiffusionPipeline

        # enable_safety_checker=False evite d'exiger cosmos_guardrail, une
        # dependance optionnelle des pipelines Cosmos. Les autres pipelines ne
        # connaissent pas ce parametre et levent : on retente sans.
        try:
            pipe = DiffusionPipeline.from_pretrained(
                MODEL_DIR, torch_dtype=torch.bfloat16, enable_safety_checker=False)
        except TypeError:
            pipe = DiffusionPipeline.from_pretrained(MODEL_DIR, torch_dtype=torch.bfloat16)

        # .to("cuda") deplace SANS convertir le dtype : sur un modele 4 bits une
        # conversion casserait les poids quantifies. Si accelerate a deja reparti
        # le modele, le deplacement echoue — ce n'est pas une erreur de chargement.
        try:
            pipe = pipe.to("cuda")
        except Exception:
            pass

        try:
            pipe.set_progress_bar_config(disable=True)
        except Exception:
            pass
        globals()["_pipe"] = pipe
    except Exception as e:  # on garde l'erreur pour que /health la rapporte
        globals()["_load_error"] = f"{type(e).__name__}: {e}"


threading.Thread(target=_load_pipeline, daemon=True).start()


def _premiere_image(out):
    """Recupere la premiere image, quel que soit ce que rend le pipeline.

    Les pipelines texte-vers-image classiques renvoient `.images` (liste de
    PIL.Image). Les pipelines Cosmos 3 sont omnimodaux et renvoient `.video` :
    une liste de sequences, dont la premiere contient une seule frame en mode
    texte-vers-image. Sans ce demelage, la generation reussissait cote GPU puis
    echouait a l'enregistrement.
    """
    images = getattr(out, "images", None)
    if images:
        return images[0]
    video = getattr(out, "video", None)
    if video:
        premiere = video[0]
        # Sequence de frames, ou frame unique deja deballee.
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
    """Agrandit vers out_w x out_h en haute qualité.

    Petite montée (Full HD, ~1.25x) : Lanczos + unsharp suffit. Grande montée
    (4K, >1.6x) : Real-ESRGAN x4 (synthèse de détail) puis ajustement Lanczos à
    la taille exacte. Le crop centré est un filet de sécurité (ratios identiques
    en pratique, donc no-op).
    """
    if image.width >= out_w and image.height >= out_h:
        return image
    scale = max(out_w / image.width, out_h / image.height)
    if scale > ESRGAN_SCALE_THRESHOLD:
        net = _get_esrgan()
        if net is not None:
            image = esrgan.upscale_4x(net, image)
    # Ajustement final à la taille exacte (cover + crop), qui gère aussi la
    # redescente quand ESRGAN a sur-dimensionné (4x > cible).
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
    # Taille de sortie : si demandée et plus grande que la génération native, on
    # upscale (Lanczos + unsharp). Borné à 3840 (4K) par côté.
    out_width = max(0, min(3840, int(out_width or 0)))
    out_height = max(0, min(3840, int(out_height or 0)))
    try:
        with _gpu_lock:
            with torch.inference_mode():
                # prompt= en argument NOMME, jamais positionnel : les pipelines qui
                # savent aussi editer une image (Flux2KleinPipeline entre autres)
                # attendent l'image en premiere position, et un prompt positionnel
                # y atterrit comme image -> « Provide either `prompt` or
                # `prompt_embeds` ». Constate le 24/08 sur FLUX.2 Klein 4B.
                out = _pipe(prompt=prompt, num_inference_steps=steps,
                            guidance_scale=float(guidance), width=width, height=height)
            image = _premiere_image(out)
        if out_width and out_height:
            image = _upscale(image, out_width, out_height)
        # JPEG ne sait pas coder un canal alpha : on aplatit sur RGB avant.
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
