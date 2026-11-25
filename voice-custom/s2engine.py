"""Fish Audio S2 Pro, loaded once, speaking with one reference voice.

Shared by server.py (the voice engine) and reference/test_voice.py (comparing
candidate references on the DGX), so both measure exactly the same thing.

The voice is not trained into the weights: S2 Pro clones it in context from a
reference clip and its exact transcript, which is what the model is built and
ranked for. Fish Audio's own fine-tuning notes advise AGAINST fine-tuning this
RL-aligned checkpoint (it shifts the model distribution) and say their LoRA
learns the speaking style, not the timbre — the timbre still comes from the
reference. So the « personal voice » is a well-chosen reference, encoded once
and cached (use_memory_cache), not a checkpoint.

fish-speech locates a reference by id under ./references/<id>/ (audio + .lab):
callers chdir to the directory holding `references/`.
"""
import os
import sys
import threading
import time

import numpy as np

FISH_DIR = os.environ.get("FISH_SPEECH_DIR", "/opt/fish-speech")
if FISH_DIR not in sys.path:
    sys.path.insert(0, FISH_DIR)


def resolve_checkpoint(repo_or_dir: str) -> str:
    """A local directory as is; a Hub id through the (mounted) HF cache."""
    if os.path.isdir(repo_or_dir):
        return repo_or_dir
    from huggingface_hub import snapshot_download
    return snapshot_download(repo_or_dir, allow_patterns=[
        "*.json", "*.safetensors", "codec.pth", "*.jinja", "*.tiktoken", "LICENSE.md"])


class S2Voice:
    """Thread-safe: one GPU, one generation at a time."""

    def __init__(self, checkpoint: str, compile: bool, device: str = "cuda"):
        from tools.server.model_manager import ModelManager
        t = time.time()
        self.compiled = compile
        self.manager = ModelManager(
            mode="tts", device=device, half=False, compile=compile,
            llama_checkpoint_path=checkpoint, decoder_checkpoint_path=None,
            decoder_config_name="modded_dac_vq")
        self.engine = self.manager.tts_inference_engine
        dec = self.engine.decoder_model
        self.sample_rate = int(dec.spec_transform.sample_rate if hasattr(dec, "spec_transform") else dec.sample_rate)
        self.lock = threading.Lock()
        self.load_s = round(time.time() - t, 1)

    def synth(self, text: str, reference_id: str, seed: int | None = None) -> np.ndarray:
        """Float32 mono audio at self.sample_rate; raises on a model error."""
        from fish_speech.utils.schema import ServeTTSRequest
        req = ServeTTSRequest(text=text, reference_id=reference_id, use_memory_cache="on",
                              format="wav", streaming=False, seed=seed)
        with self.lock:
            for res in self.engine.inference(req):
                if res.code == "error":
                    raise RuntimeError(str(res.error))
                if res.code == "final":
                    return np.asarray(res.audio[1], dtype=np.float32).squeeze()
        raise RuntimeError("no audio generated")


def load(checkpoint: str, compile: bool, log=print) -> S2Voice:
    """torch.compile when asked, falling back to eager if it cannot build here
    (Triton on a new GPU generation is the usual suspect): slower, but speaking."""
    if compile:
        try:
            return S2Voice(checkpoint, compile=True)
        except Exception as e:                   # noqa: BLE001
            log(f"torch.compile failed ({type(e).__name__}: {e}); retrying without it")
    return S2Voice(checkpoint, compile=False)
