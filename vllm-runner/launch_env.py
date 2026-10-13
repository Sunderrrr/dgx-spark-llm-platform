"""Environment of a model process the runner spawns.

Kept apart from runner.py (process lifecycle): every variable here is a
measured workaround for one engine or one GB10 quirk, and each one says why.
"""
import os


def build(engine, model_env, hf_home):
    """Return the environment for one launch of `engine`.

    Explicit minimal env rather than **os.environ: avoids leaking the full
    root environment (assorted secrets) into /logs and /stream.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        "HOME": os.environ.get("HOME", "/root"),
        "HF_HOME": hf_home,
        "PYTHONUNBUFFERED": "1",
        # FlashInfer ships its kernels in a companion package (`flashinfer-cubin`)
        # whose version trails the Python one (0.6.13 vs 0.7.0.post1 — there is
        # no 0.7 cubin on PyPI). Their version gate is checked at IMPORT and
        # kills vLLM outright, while the kernels we use on GB10 are the CUTLASS
        # paths, not those cubins. Bypass the check rather than pin a stale
        # flashinfer-python (that dragged torch back to 2.10 and broke vLLM).
        "FLASHINFER_DISABLE_VERSION_CHECK": "1",
        # DeepGEMM E8M0 breaks FP8 MoE on Blackwell/GB10 ("Unknown SF
        # transformation") and degrades accuracy (vLLM partially auto-disables
        # it) → we turn it off entirely, CUTLASS fallback.
        "VLLM_USE_DEEP_GEMM": "0",
        # FlashInfer JIT-compiles its NVFP4 kernels at startup. Without MAX_JOBS
        # it passes no -j to ninja, which spawns ~nproc+2 `cicc` compilers of
        # ~3 GB each — on top of the already-loaded weights, that triggers the
        # OOM killer and the model dies at init (seen on Leanstral and Nemotron).
        # 4 jobs ≈ 12 GB peak: compilation a bit slower, but only once (kernels
        # are then cached).
        "MAX_JOBS": os.environ.get("MAX_JOBS", "4"),
        # Keep the engine's internal sockets on loopback (single-node box).
        # Otherwise they listen on whatever address the hostname resolves to,
        # a VPN interface for instance, outside any firewall rule.
        # VLLM_HOST_IP covers vLLM's own ZMQ/TCP queues (shm_broadcast…);
        # GLOO_SOCKET_IFNAME covers torch.distributed's Gloo groups, which
        # ignore VLLM_HOST_IP (measured: 6 EngineCore ports on the VPN address
        # with it alone, none with both).
        "VLLM_HOST_IP": "127.0.0.1",
        "GLOO_SOCKET_IFNAME": "lo",
    }
    env.update(model_env)
    if os.environ.get("HF_TOKEN"):
        env["HF_TOKEN"] = os.environ["HF_TOKEN"]
    if engine == "ds4":
        # KV cache packed in FP8 → ~7 GiB saved at 1M context (see model card).
        env["DS4_KV_TURBO"] = "1"
    return env
