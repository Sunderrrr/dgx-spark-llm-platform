# Troubleshooting

The known traps, in the order they bite: the GB10's unified memory, docker
networking, and the model runner. Symptom first, cause second. For the full
measurement history, the operating guide kept on the machine
([`CLAUDE.md`](../CLAUDE.md)) is the reference.

| Symptom | Jump to |
|---|---|
| A model launch dies with an out-of-memory error | [GB10 unified memory](#gb10-unified-memory) |
| The box froze and rebooted on its own | [Memory safety](#memory-safety-why-the-box-froze-and-rebooted-on-2026-10-01) |
| The portal reaches nothing, no model is available, nothing is logged | [Docker networking](#docker-networking) |
| A model change seems to have taken the current model down | [The runner](#the-runner) |
| The site 404s after a reboot | [After an unexpected reboot](#after-an-unexpected-reboot) |

## GB10 unified memory

- **Unified memory is shared.** GPU allocations count against the same pool as
  system RAM. "128 GB" is the vendor figure; the kernel reports **121.6 GiB**
  (130.6 GB) usable, and that is the number `free` prints — the measurements in
  these docs quote it ("99 GB out of 121"), so a headroom calculation must not
  mix the two. Only **one chat model** runs at a time; OCR, video (ComfyUI), ASR,
  voice, image and music are separate **on-demand** sidecars (started at first
  use, absent from memory when idle — hence not probed by the monitor). If a
  launch OOMs, **stop a sidecar from Admin** — don't shrink the chat model's
  context.
- **`docker run --memory` on a sidecar caps GPU memory too** → CUDA load fails.
  Never set it. The same goes for a `MemoryMax` on `vllm-runner`: it would be
  **illusory** (see below).
- **Build for GB10** (CUDA 13, sm_121):
  ```
  cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON \
    -DCMAKE_CUDA_ARCHITECTURES=121 -DGGML_CUDA_FA=ON -DGGML_CUDA_GRAPHS=ON -DGGML_NATIVE=ON
  cmake --build build -j --target llama-server
  ```
- **Never compile llama.cpp while a 1M-context model is loaded.** A `cmake -j20`
  + a 40 GB download on top of the ~106 GB of the served model made the **machine
  reboot** (2026-09-07). On unified memory there is no partition: free the model
  first, or throttle to `-j6`.
- **Any heavy build is memory-capped while the model is loaded** — the frontend
  image's `next build` is capped via `ARG NODE_HEAP_MB` (default 2560); a cap
  turns an unbounded risk into a bounded build failure. Raise it with
  `--build-arg NODE_HEAP_MB=…`.
- **Configs near the edge.** earlyoom sends SIGTERM under 3 % available
  (~3.7 GB). Flash-Next at 1M context ran with ~5 GB available — about 1 % above
  that threshold.

## Memory safety: why the box froze and rebooted on 2026-10-01

**On the GB10, GPU memory is invisible to the kernel's OOM killer and to cgroups.**
CUDA allocations are taken from system RAM but appear in no process RSS and in no
cgroup: a model holding 26 GB of GPU memory showed only 2.3 GB of `anon` in the
runner's cgroup (measured). Consequences, all observed on 2026-10-01:

- a `MemoryMax` on `vllm-runner` would be **illusory** — don't add one thinking it
  protects the box;
- when GPU memory runs out, the kernel OOM killer cannot see the culprit and kills
  everything else instead — **443 processes** in one boot, `anon-rss: 0` each, while
  the kernel's Mem-Info showed ~119 GB held outside every category (0.77 GB free).
  The NVIDIA driver then fails (`NV_ERR_NO_MEMORY`), the box freezes, then reboots;
- the only reliable signal is **`MemAvailable`**, which GPU memory does lower.

**The crash loop and how it was broken.** Trigger: the runner auto-resumed a
llama.cpp config sized for a cache split across 8 slots (`--ctx-size 2097152
--parallel 8` without `--kv-unified`, ~2M tokens of KV) — 110 GB, global OOM. It
then LOOPED, across reboots, because:

1. the resume-failure counter lived in RAM and reset on every service restart;
2. `OOMPolicy=stop` restarted the whole runner on every OOM — so the cap was never hit;
3. the startup resume relaunched immediately, on top of GPU memory the driver had not
   yet returned (it frees it with a delay), and `RestartSec=5` left it no time;
4. the counter reset as soon as the model answered HTTP 200 — it answered, then died a
   minute later. "Answers" is not "stable".

Fixes in `runner.py` and the units (do not undo):

- **persisted** failure counter (`resume_failures` next to `last_model.json`), reset
  only after `STABLE_SECONDS` (600 s) of uninterrupted running;
- **memory floor** before ANY launch (`MIN_FREE_TO_LAUNCH_GIB`, 30): wait for the
  previous model's memory, then refuse if still short — a refusal counts as a failure,
  which is what ends a loop instead of feeding it;
- **runtime guard**: the runner kills its OWN llama-server under `CRITICAL_FREE_GIB`
  (2), before the kernel's blind OOM;
- all resumes go through the watchdog (the separate startup resume would race it);
- unit: `RestartSec=30`, `OOMPolicy=continue`;
- **earlyoom** as the global net (`systemd/earlyoom.service.d-cronos.conf`), for model
  launchers OUTSIDE the runner too — an agent harness spawned its own 115 GB
  `llama-server` that day. Two non-obvious settings: `-s 100,100` (by default earlyoom
  also requires LOW SWAP, and GPU memory never swaps — swap was 75 % free during the
  OOM, so the defaults would never have fired) and `--prefer llama-server` (its GPU
  memory isn't in its RSS, so without the preference earlyoom would kill an innocent
  large-RSS process). Victim choice verified with `--dryrun`.

The rest of the memory is consumed elsewhere (containers ~2.9 GiB including
`qbittorrent-nox` 1.7, an idle whisper `asr` holding **2.1 GiB of GPU** for
nothing — `restart=unless-stopped`, so it never frees itself —, the web UI
~1 GiB, the GNOME desktop). Act there before touching the context.

## Docker networking

- **`host.docker.internal` is pinned, don't "fix" it back to `host-gateway`.**
  `host-gateway` resolves to the gateway of whichever interface carries the
  container's default route — and that depends on the *order of attached
  networks*. Attaching one more network has silently flipped it twice
  (`asr_net`, then `web_net`): outbound packets then left as `172.22.x` /
  `172.25.x`, and `vllm-restrict.service` — which only allows `172.19.0.0/16`
  towards the runner and vLLM — dropped them. Symptom: the portal reaches
  nothing, no model is available, and **nothing is logged**. `dgx-portal` now
  pins `host.docker.internal:172.19.0.1`, the address the firewall already
  hardcodes. Compose's `priority:` on networks did *not* fix this when tried.
- **Adding a network to `dgx-portal` is never free.** After any such change,
  check from inside the container that the runner still answers:
  `docker exec dgx-portal python3 -c "import requests;
  print(requests.get('http://host.docker.internal:8001/status', timeout=4).status_code)"`
  (401 = reachable, timeout = you broke the route).
- LiteLLM listens on **4001**, not the upstream default 4000 — the code fallback
  does not answer here (`LITELLM_URL=http://litellm:4001`).
- `docker compose up -d <svc>` does NOT recreate a container whose definition has
  not changed: a modified mounted file stays invisible until `docker compose
  restart <svc>`.

## The runner

- **Restarting `vllm-runner` KILLS the served model.** The unit has no `KillMode`,
  so systemd applies `control-group` — and the `llama-server` spawned by the
  runner lives in the `vllm-runner.service` cgroup (verified via
  `/proc/<pid>/cgroup`). Any change to `runner.py` that must take effect needs
  the same downtime window as a model relaunch — this is not a "zero-downtime"
  operation. New pseudo-flags (`--llama-prism` and friends) exist only after such
  a restart (`_BIN_FLAGS` is built at import).
- **Switching llama.cpp binaries does NOT need a restart**: `LLAMA_BIN` is a
  symlink resolved at each spawn (the env variable only at import). Each
  `build*/bin/` folder is self-contained. On 2026-09-25 the path pointed for one
  day at `build-v050/` (release v0.5.0, `7fe450e`, build 354), then went **back
  to `build/` (0.3.0-dev, `b10793`)** the same evening on a measured A/B — see
  the binary comparison below.
- **`vllm_args` is allowlisted.** A flag absent from the allow-list is refused
  BEFORE the spawn, with the reason. Don't try to smuggle blocked flags — extend
  the allowlist deliberately if truly needed.
- **The runner's log buffer (`_logs`) is too short to diagnose a startup failure**,
  and `_start_process` empties it at every attempt: with 3 auto-resumes, the root
  cause was overwritten before being read. The runner writes **one log per
  startup** in `/var/lib/vllm-runner/logs/<timestamp>-<engine>-<model>.log` (20 MB
  and 12 files maximum). Read it via shell — it is not exposed over HTTP.

### Context and KV-cache pitfalls (llama.cpp)

- **Beyond the NATIVE context you need YaRN, and an ill-suited scale costs you
  anyway.** 262 144 is the native one; without scaling llama.cpp caps the slot at
  this value **while still reserving the requested memory** (measurement: 107 GB
  resident for the same useful context). YaRN must follow the target (0.5 = factor
  2; 0.25 = factor 4 for 1M).
- **`--ctx-size` is the TOTAL when the cache is not unified**: 4 × 256k = 1M.
  Without `--parallel`, llama.cpp picks 4 slots on its own and enables the unified
  cache; with `--parallel` it DISABLES it — you then have to ask for it
  explicitly. **`--kv-unified` changes the meaning of `--ctx-size`**: with it the
  slots share one pool and each can address the WHOLE context (measurement:
  `--ctx-size 524288 --parallel 6 --kv-unified` gives `n_ctx_slot = 524288,
  kv_unified = true`).
- **`--kv-unified-per-slot N`** caps the per-session window when the pool is
  shared; the pool sizes itself to `n_parallel × N` when `--ctx-size` is omitted.
  The portal reads it FIRST in `effective_ctx`, because it is the only source
  describing the PER-SESSION window — the "unified ⇒ whole context" rule would
  have advertised 983040 instead of 196608. The flag is not specific to one
  binary version: rolling back does NOT require changing the catalog arguments.
- **`--ctx-size 262144` with `--n-predict 262144` does NOT give 256k/256k**: the
  reserve is no longer smaller than the window, the cautious heuristic takes over
  (~174k/87k). And a session's window is shared between prompt and generation —
  you need X + Y ≤ the slot window.
- **Do NOT quantize the KV cache.** Two measurements (2026-09-11 then 09-14, same
  protocol, machine idle): `q4_0` drops throughput to **0.2 tok/s** against **34**
  in f16 (~170×), and `q8_0` to **0.8** against 26–28 in f16. The cache does do
  what it is asked (~31 KiB/token in f16, ~15 in q8_0): it is the DECODER that
  re-reads it at every step and pays the quantized path. On this model the cache
  memory is traded away through the window size, not through quantization.
- **4 PARTITIONED 512k slots do not fit (measurement of 2026-09-14).**
  Advertising "256k input + 256k output" per session requires 512k of window per
  session, hence `--ctx-size 2097152 --parallel 4`: **115 GiB used, 5 free** for
  2M tokens of quantized cache (+23 GiB for +1.5M tokens, ~15 KiB/token) — with
  the 0.8 tok/s throughput above. In f16 the same setup would need ~143 GB,
  beyond the 121.6 GiB usable: it **cannot** start. Each extra 256k per slot
  costs ~8 GiB.

### The binary question (v0.5.0 vs 0.3.0-dev)

llama.cpp v0.5.0 sizes the KV cache PER SLOT — `--ctx-size 1048576 --parallel 4`
reserves 4 × 1M tokens (~124 GiB) where the old binary reserved 1M (first attempt
2026-09-24 cost a machine OOM after 13 min of loading). The workaround
(`--kv-unified --kv-unified-per-slot 262144`) kept the service alive but did not
treat the cause: on the evening of 2026-09-25 there were THREE losses of the
model in 30 min (18:08 driver allocation failures `NV_ERR_NO_MEMORY`, 18:42 a
mundane `NetworkManager` allocation triggers the OOM-killer → `llama-server`
killed, `Failed with result 'oom-kill'`).

**Measured A/B, IDENTICAL arguments (`--ctx-size 786432 --parallel 3 --kv-unified
--kv-unified-per-slot 262144`) — only the binary changes:**

| | v0.5.0 (`build-v050`) | **0.3.0-dev (`build`)** |
|---|---|---|
| `/props` | 262144 / 3 slots | 262144 / 3 slots |
| GPU memory | 87 451 MiB | 92 007 MiB |
| **available** | **2.5 GiB** | **19.5 GiB** |
| system swap | 10.2 GiB | 4.0 GiB |
| engine RSS | 21.2 GiB | **1.65 GiB** |
| engine swap | 7.6 GiB | **0.00 GiB** |

**The difference is the TYPE of memory, not its quantity.** v0.5.0 keeps ~21–28
GiB in HOST ANONYMOUS memory: pageable, therefore swapped (12.5 GiB of the model
were), and this is what made `llama-server` the biggest `anon-rss` on the machine
— hence **the OOM-killer's designated first victim**. 0.3.0-dev keeps everything
in the PINNED GPU allocation: RSS 1.65 GiB, zero swap, and the process is no
longer a target. The GPU "costs" 4.5 GiB more, the machine gets 17 back.

**Decision (2026-09-25 evening): back to `build/` (0.3.0-dev) and back up to 4
sessions** — `--ctx-size 1048576 --parallel 4 --kv-unified --kv-unified-per-slot
262144`. Measured after relaunch (42 s of loading): `n_ctx = 262144`, 4 slots,
GPU 100 822 MiB, **10 GiB free**, engine RSS 1.7 GiB — ONE session MORE than with
v0.5.0, with 7.6 GiB more headroom. Client contract unchanged (196608/65536),
verified by a real generation through LiteLLM.

**Method lesson: measure the right number.** v0.5.0 was not "more greedy" with KV
cache — same GGUF, same f16 cache — it placed its memory elsewhere, in a place
the kernel can claim. A low `MemAvailable` WITH a high RSS says this; the same
low `MemAvailable` with a tiny RSS would say the opposite. The GPU memory reported
by `nvidia-smi` is not enough to judge: you need the process's RSS and swap.

## After an unexpected reboot

Auto-resume exists (the runner persists `last_model.json` and relaunches on boot,
capped at 3 tries). Two things still bite after a reboot:

1. **The 404 / down-model symptom** usually means **Traefik raced DNS** at boot
   (plugin fetch from GitHub + Cloudflare DNS-challenge ACME both need DNS, but
   `dockerd` starts the `unless-stopped` container before DNS is up). This is now
   auto-handled by `cronos-traefik-boot.service` (waits for DNS, restarts Traefik
   once). If it still 404s, the manual fix is `docker restart traefik` once DNS
   resolves. The Traefik config under `/opt/traefik` is rewritten by the
   traefik-manager-agent — don't edit it; fixes belong in systemd.
2. **`last_model.json` empty** → nothing to resume (e.g. the model had been
   `/stop`ped before reboot). Relaunch the intended model via Admin → Launch.

Recovery checklist after an unexpected reboot:

```
systemctl status vllm-runner              # daemon up?
curl -s -H "Authorization: Bearer $RUNNER_TOKEN" http://127.0.0.1:8001/status   # model up?
docker ps                                 # litellm, portal, frontend, traefik, sidecars
docker restart traefik                    # if the site 404s
```

## Other observed traps

- **« Aucun modèle actif » during a big generation.** The engine's CONTROL PLANE
  freezes while it prefills (measured with 45k–149k token prompts: `/v1/models`
  answered in ~15 s slices), so a failed probe is **not proof of absence** — the
  last known model list now survives 10 min; a probe that ANSWERS empty still
  clears it.
- **Cloudflare replaces the body of a 502** with its own error page (the same
  JSON as 503 passes through intact): every honest error message died in
  transit. `502` is now banned for user-facing answers.
- **The model has no clock** — the current date and time are injected into the
  system prompt at EVERY request (playground AND support), after the persona
  truncation (a long persona must never cut the clock).
- **LiteLLM RENAMES the reasoning field, it does not drop it**: vLLM returns
  `message.reasoning`, after LiteLLM it is `reasoning_content` (and the original
  remains in `provider_specific_fields.reasoning`). Searching for `reasoning` on
  the LiteLLM output gives a false negative.
