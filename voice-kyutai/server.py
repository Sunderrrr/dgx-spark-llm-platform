"""Kyutai TTS 1.6B (en/fr, CC-BY 4.0) for the playground's voice mode.

Third engine of the « voice » container, next to Chatterbox and Qwen3-TTS:
same container name, same voice_net network, same port 8004 — the portal
tells the engines apart by the `engine` field of /api/model-info.

What this one is for: a CONVERSATION. Kyutai's delayed-streams model emits
audio while it reads the text, so /api/speak streams raw PCM as each 80 ms
frame is decoded: the browser starts playing ~0.5 s after the request
(measured on the GB10, 2026-10-13: first audio 0.46 s, RTF 0.56, ~7 GiB).

Only preset voices (precomputed embeddings from kyutai/tts-voices): no
reference upload here, so no user audio is ever decoded by this service.
"""
import asyncio
import logging
import os
import queue
import threading
import time

import numpy as np
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("kyutai-tts")

MODEL_REPO = os.environ.get("KYUTAI_TTS_REPO", "kyutai/tts-1.6b-en_fr")
# A sentence or two per call: the voice mode speaks sentence by sentence.
MAX_TEXT_CHARS = 1000
# Preset French voices (LibriVox readers, « enhanced » cut). The id is what
# the portal and the browser see; the path never leaves this service.
VOICES = {
    "fr-a": "cml-tts/fr/2154_2576_000020-0003_enhanced.wav",
    "fr-b": "cml-tts/fr/1406_1028_000009-0003_enhanced.wav",
    "fr-c": "cml-tts/fr/10087_11650_000028-0002_enhanced.wav",
    "fr-d": "cml-tts/fr/12977_10625_000037-0001_enhanced.wav",
}
DEFAULT_VOICE = "fr-a"

app = FastAPI(title="DGX Kyutai TTS", docs_url=None, redoc_url=None, openapi_url=None)

_tts = None
_conds = {}
_load_error = None
# One model, one GPU stream: concurrent requests wait their turn.
_gpu_lock = threading.Lock()


class _Cancelled(Exception):
    """Raised from the frame callback when the client went away."""


def _load():
    global _tts, _load_error
    try:
        from moshi.models.loaders import CheckpointInfo
        from moshi.models.tts import TTSModel
        t = time.time()
        info = CheckpointInfo.from_hf_repo(MODEL_REPO)
        _tts = TTSModel.from_checkpoint_info(info, n_q=32, temp=0.6, device=torch.device("cuda"))
        for vid, path in VOICES.items():
            _conds[vid] = _tts.make_condition_attributes([_tts.get_voice_path(path)], cfg_coef=2.0)
        # Warm-up: the first generation compiles kernels (~5 s measured);
        # paid here, not by the first sentence of the first conversation.
        for _ in _generate("Bonjour.", DEFAULT_VOICE, threading.Event()):
            pass
        log.info("Kyutai TTS ready in %.1f s (%d voices)", time.time() - t, len(_conds))
    except Exception as e:                       # noqa: BLE001
        _load_error = f"{type(e).__name__}: {e}"
        log.exception("Kyutai TTS failed to load")


def _generate(text, voice, cancel):
    """Yields int16 PCM chunks (bytes) as frames are decoded."""
    out = queue.Queue()

    def on_frame(frame):
        if cancel.is_set():
            raise _Cancelled()
        if (frame != -1).all():
            pcm = _tts.mimi.decode(frame[:, 1:, :]).cpu().numpy()[0, 0]
            out.put((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())

    def run():
        try:
            with _gpu_lock, torch.no_grad():
                entries = _tts.prepare_script([text], padding_between=1)
                with _tts.mimi.streaming(1):
                    _tts.generate([entries], [_conds[voice]], on_frame=on_frame)
        except _Cancelled:
            pass
        except Exception:                        # noqa: BLE001
            log.exception("generation failed")
        finally:
            out.put(None)

    threading.Thread(target=run, daemon=True).start()
    while True:
        chunk = out.get()
        if chunk is None:
            return
        yield chunk


@app.on_event("startup")
async def _startup():
    threading.Thread(target=_load, daemon=True).start()


@app.get("/api/model-info")
async def model_info():
    return JSONResponse({
        "engine": "kyutai", "type": "kyutai", "repo_id": MODEL_REPO,
        "loaded": _tts is not None and _load_error is None,
        "error": _load_error,
        "sample_rate": int(_tts.mimi.sample_rate) if _tts is not None else 24000,
        "voices": list(VOICES), "default_voice": DEFAULT_VOICE,
    })


class SpeakIn(BaseModel):
    text: str
    voice: str = DEFAULT_VOICE


@app.post("/api/speak")
async def speak(body: SpeakIn):
    if _tts is None:
        raise HTTPException(503, _load_error or "model loading")
    text = " ".join(body.text.split())
    if not text:
        raise HTTPException(400, "empty text")
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(413, f"text longer than {MAX_TEXT_CHARS} characters")
    voice = body.voice if body.voice in VOICES else DEFAULT_VOICE
    cancel = threading.Event()
    gen = _generate(text, voice, cancel)

    async def stream():
        loop = asyncio.get_running_loop()
        try:
            while True:
                chunk = await loop.run_in_executor(None, next, gen, None)
                if chunk is None:
                    return
                yield chunk
        finally:
            # Client gone (barge-in, closed tab): stop the GPU work now.
            cancel.set()

    return StreamingResponse(stream(), media_type="application/octet-stream",
                             headers={"X-Sample-Rate": str(int(_tts.mimi.sample_rate)),
                                      "X-Sample-Format": "s16le"})
