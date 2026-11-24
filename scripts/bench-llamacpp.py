#!/usr/bin/env python3
"""A/B benchmark for one llama.cpp configuration: same prompt, same budget, 3 runs.

The engine's own `timings` is the reference (predicted_per_second), not the portal's
display, and MemAvailable is recorded at rest and after the runs so a speed figure is
always read next to the memory it cost.
"""
import json
import re
import statistics
import threading
import subprocess
import sys
import time
import urllib.error
import urllib.request

RUNNER = "http://127.0.0.1:8001"
ENGINE = "http://127.0.0.1:8000"
ENV = dict(re.findall(r"^([A-Z0-9_]+)=(.*)$", open("/root/ai-platform/.env", encoding="utf-8").read(), re.M))
TOKEN = ENV["RUNNER_TOKEN"]
SHORT = "Raconte en trois phrases ce qu'est un serveur d'inférence."
LONG = ("Voici un document technique. " * 300) + "\nRésume-le en deux phrases."


def mem_available() -> float:
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return round(int(line.split()[1]) / 1048576, 1)
    return -1.0


class Watchdog:
    """Kills llama-server under a memory floor, and records the lowest MemAvailable seen.

    Added after the 2026-11-24 crash: a `--ubatch-size 2048 --batch-size 4096` run on
    top of a 2M-token pool drove MemAvailable to zero in two minutes, the kernel OOM
    killer (blind to GPU memory) took netbird and the desktop, and the driver logged
    NV_ERR_NO_MEMORY. A benchmark on this box must be able to abort itself.
    """

    FLOOR_GIB = 12.0

    def __init__(self) -> None:
        self.low = 999.0
        self.tripped = False
        self._stop = False
        self._t = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> "Watchdog":
        self._t.start()
        return self

    def stop(self) -> None:
        self._stop = True

    def _loop(self) -> None:
        while not self._stop:
            m = mem_available()
            self.low = min(self.low, m)
            if m < self.FLOOR_GIB:
                self.tripped = True
                subprocess.run(["pkill", "-f", "llama-server -m"], check=False)
                return
            time.sleep(1.0)


def wait_memory_back(floor_gib: float = 100.0, timeout_s: int = 120) -> bool:
    """Waits until the previous model's memory is really available again."""
    for _ in range(timeout_s):
        if mem_available() >= floor_gib:
            return True
        time.sleep(2)
    return False


def launch(args: str, name: str) -> bool:
    body = {"hf_model_id": "local:inkling-2-mini-abliterated", "model_name": name,
            "vllm_args": args, "engine": "llamacpp"}
    req = urllib.request.Request(f"{RUNNER}/launch", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            out = json.load(r)
        print(f"  launch -> {out.get('status')}", flush=True)
        if out.get("status") == "refused":
            print("  refusé:", out.get("message") or out.get("reason"))
            return False
    except urllib.error.HTTPError as e:
        print(f"  launch refused ({e.code}):", e.read().decode()[:200])
        return False
    except Exception as e:  # noqa: BLE001 - reported, not swallowed
        print("  launch error:", e)
        return False
    for _ in range(120):
        time.sleep(5)
        try:
            with urllib.request.urlopen(f"{ENGINE}/health", timeout=4) as r:
                if "ok" in r.read().decode():
                    return True
        except Exception:  # noqa: BLE001 - still loading
            pass
    print("  never loaded")
    return False


def run(prompt: str, n_predict: int) -> dict:
    body = {"model": "x", "messages": [{"role": "user", "content": prompt}],
            "max_tokens": n_predict, "temperature": 0.7,
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(f"{ENGINE}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.load(r)
    t = d.get("timings") or {}
    return {"decode_tps": round(t.get("predicted_per_second") or 0, 1),
            "prefill_tps": round(t.get("prompt_per_second") or 0, 1),
            "tokens": t.get("predicted_n"), "prompt": t.get("prompt_n")}


def bench(label: str, args: str, runs: int = 3) -> dict:
    print(f"\n=== {label} ===", flush=True)
    print(f"  args: {args}", flush=True)
    if not wait_memory_back():
        print(f"  the previous engine has not returned its memory yet ({mem_available()} GiB free)")
        return {"label": label, "error": "memory-not-returned"}
    dog = Watchdog().start()
    try:
        ok = launch(args, "inkling-2-mini-abliterated")
    finally:
        dog.stop()
    if dog.tripped:
        print(f"  STOPPED by the watchdog (lowest {dog.low} GiB, floor {Watchdog.FLOOR_GIB} GiB)")
        return {"label": label, "error": "watchdog", "low": dog.low}
    if not ok:
        print(f"  the engine did not start (lowest {dog.low} GiB seen)")
        return {"label": label, "error": "launch", "low": dog.low}
    time.sleep(3)
    dog = Watchdog().start()
    rest = mem_available()
    short, long_ = [], []
    for _ in range(runs):
        short.append(run(SHORT, 256))
    for _ in range(2):
        long_.append(run(LONG, 128))
    after = mem_available()
    dog.stop()
    if dog.tripped:
        return {"label": label, "error": "watchdog", "low": dog.low}
    dec = statistics.median(r["decode_tps"] for r in short)
    pre = statistics.median(r["prefill_tps"] for r in long_)
    print(f"  decode {dec:.1f} tok/s (median of {runs}) | prefill {pre:.0f} tok/s"
          f" | free RAM {rest} -> {after} GiB | lowest seen {dog.low} GiB", flush=True)
    return {"label": label, "args": args, "decode": dec, "prefill": pre,
            "free_rest": rest, "free_after": after, "low": dog.low, "runs": short}


def main() -> int:
    base = ("--ctx-size 2097152 --parallel 4 --n-predict 393216 --n-gpu-layers 999 "
            "--flash-attn on --jinja --rope-scaling none "
            "--cache-type-k q4_0 --cache-type-v q4_0")
    configs = [("BASE (current)", base)]
    maybe_parallel2 = base.replace("--parallel 4", "--parallel 2")
    extras = {
        # (label, args) — the order `bench()` takes
        "swa-full": ("swa-full", f"{base} --swa-full"),
        # --batch-size 4096 --ubatch-size 2048 is GONE on purpose: on 2026-11-24 it
        # drove MemAvailable to zero in two minutes on top of the 2M-token pool, the
        # kernel OOM killer took netbird and the desktop, and the driver answered
        # NV_ERR_NO_MEMORY. Prefill tuning stops at 1024 here.
        "ub1024": ("ubatch 1024", f"{base} --batch-size 2048 --ubatch-size 1024"),
        "nommap": ("no-mmap", f"{base} --no-mmap"),
        "k8v4": ("K q8_0 / V q4_0", f"{base} --cache-type-k q8_0"),
        "p2": ("2 slots of 1M", maybe_parallel2),
        "t8": ("8 threads", f"{base} --threads 8 --threads-batch 8"),
    }
    for key in sys.argv[1:]:
        if key in extras:
            configs.append(extras[key])
        else:
            print(f"unknown variant: {key}", file=sys.stderr)
    out = [bench(label, args) for label, args in configs]
    print("\n=== RÉCAPITULATIF ===")
    for r in out:
        if "error" in r:
            print(f"  {r['label']:<28} ÉCHEC")
        else:
            print(f"  {r['label']:<28} {r['decode']:>6.1f} tok/s  préfill {r['prefill']:>6.0f}"
                  f"  RAM {r['free_rest']}→{r['free_after']} Go")
    json.dump(out, open("/tmp/bench-llamacpp.json", "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
