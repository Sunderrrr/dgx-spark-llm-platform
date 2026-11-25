"""System One API for LiquidAI/d1-3B — the contract the platform already speaks.

`POST /v1/systemone` takes {"model", "state", "questions"} exactly as documented
in docs/api.md (the shape llama.cpp served for Clef), and answers per question:

  noul   → {"type": "noul", "noul": p}
  choice → {"type": "choice", "choice", "confidence", "probabilities"}
  score  → {"type": "score", "score", "confidence", "probabilities", "legend"}

d1's own `system_one()` builds precisely those dicts, so this server is a thin
HTTP layer over it: no prompt engineering, no output tokens, one forward pass.
"""
import json
import logging
import os
import threading
from typing import Any

import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("d1")

MODEL_DIR = os.environ.get("D1_MODEL", "/app/model")
MAX_STATE_CHARS = 60_000          # ~32k tokens of context, minus the questions
MAX_QUESTIONS = 128               # the System One API's own bound

app = FastAPI(title="DGX d1 decision model", docs_url=None, redoc_url=None, openapi_url=None)
_model = None
_load_error: str | None = None
_lock = threading.Lock()          # one GPU: decisions are serialised, not queued badly


def _load() -> None:
    global _model, _load_error
    try:
        from transformers import AutoModel
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.bfloat16 if dev == "cuda" else torch.float32
        log.info("loading %s on %s (%s)…", MODEL_DIR, dev, dtype)
        _model = AutoModel.from_pretrained(MODEL_DIR, trust_remote_code=True, dtype=dtype).to(dev)
        _model.eval()
        log.info("loaded")
    except Exception as exc:                     # noqa: BLE001
        _load_error = f"{type(exc).__name__}: {exc}"
        log.exception("d1 failed to load")


@app.on_event("startup")
async def _startup() -> None:
    threading.Thread(target=_load, daemon=True).start()


@app.get("/health")
def health() -> JSONResponse:
    ok = _model is not None and _load_error is None
    return JSONResponse({"status": "ok" if ok else "loading", "model": "d1-3B",
                         "error": _load_error},
                        status_code=200 if ok else 503)


@app.get("/v1/models")
def models() -> dict:
    return {"object": "list", "data": [{"id": "d1-3b", "object": "model", "owned_by": "liquidai"}]}


@app.post("/v1/systemone")
def system_one(body: dict) -> JSONResponse:
    if _model is None:
        raise HTTPException(status_code=503, detail=_load_error or "model loading")
    questions: Any = body.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise HTTPException(status_code=400, detail="questions must be a non-empty object")
    if len(questions) > MAX_QUESTIONS:
        raise HTTPException(status_code=400, detail=f"at most {MAX_QUESTIONS} questions")
    state = body.get("state")
    if isinstance(state, (dict, list)):
        state = json.dumps(state, ensure_ascii=False)
    if state is None and not body.get("images"):
        raise HTTPException(status_code=400, detail="state or images is required")
    if isinstance(state, str) and len(state) > MAX_STATE_CHARS:
        raise HTTPException(status_code=413, detail="state too long")
    with _lock:
        try:
            out = _model.system_one(state, questions, images=body.get("images") or None)
        except Exception:                        # noqa: BLE001
            log.exception("decision failed")
            raise HTTPException(status_code=500, detail="decision failed")
    # d1 answers {"answers": {...}, "usage": {...}}; keep exactly that, and make
    # sure a client that sends `model` gets the same name back.
    out.setdefault("model", body.get("model") or "d1-3b")
    return JSONResponse(out)
