# Operations

Day-to-day runbook: launching and stopping the model, the sidecars, what the
dashboard numbers mean, and the host services around the stack. Traps and
symptoms are in [troubleshooting](troubleshooting.md). The deeper operating guide
kept on the machine ([`CLAUDE.md`](../CLAUDE.md)) carries the full measurement
history.

> **Golden rule: never stop or restart the served chat model without explicit
> go-ahead.** A restart kills whatever is loading/serving with no warning.
> Auto-resume on boot is fine (it *restores*). Deliberately restarting a running
> model is not. Restarting `vllm-runner` KILLS the served model too — the unit
> has no `KillMode`, so the `llama-server` it spawned lives in the
> `vllm-runner.service` cgroup (verified via `/proc/<pid>/cgroup`). Any change to
> the runner daemon that must take effect therefore needs the same downtime window as a
> model relaunch.

## Launch a model

Via the portal (**Admin → Launch**) or the runner API directly:

```bash
curl -H "Authorization: Bearer $RUNNER_TOKEN" -H "Content-Type: application/json" \
  -d '{"hf_model_id":"deepreinforce-ai/Ornith-1.0-35B-FP8","model_name":"ornith-35b-fp8",
       "vllm_args":"--enable-auto-tool-choice --tool-call-parser qwen3_coder --dtype bfloat16 --max-model-len 262144 --gpu-memory-utilization 0.7 --max-num-seqs 8"}' \
  http://127.0.0.1:8001/launch
```

`vllm_args` is validated against a strict allowlist — see [`SECURITY.md`](../SECURITY.md#21-model-launching--a-strict-allowlist-not-a-denylist).
A flag absent from the allow-list is refused **before** the spawn.
`--trust-remote-code` and overrides of critical flags are blocked for chat models
(only OCR may pass trust-remote-code). The catalog carries the launch arguments
(`model_configs.vllm_args`), so an Admin → Launch always sends the line that was
validated — update it when the arguments change, or a relaunch sends back the old
configuration.

## Auto-resume

The runner persists the last successful launch (`/var/lib/vllm-runner/last_model.json`)
and **relaunches it** after a process crash, a service restart or a reboot. A manual
`/stop` clears that state (no resume). Capped at 3 consecutive attempts.

The resume machinery is memory-aware on purpose (the 2026-10-01 crash loop — see
[troubleshooting → memory safety](troubleshooting.md#memory-safety-why-the-box-froze-and-rebooted-on-2026-10-01)):
a **persisted** failure counter (`resume_failures` next to `last_model.json`),
reset only after 600 s of uninterrupted running. A **memory floor** before any
launch (`MIN_FREE_TO_LAUNCH_GIB`, 30) refuses to start when the previous
model's memory has not been returned. A **runtime guard** kills its own
llama-server under `CRITICAL_FREE_GIB` (2) before the kernel's blind OOM. All
resumes go through the watchdog. The unit sets `RestartSec=30`, `OOMPolicy=continue`.

## Engines and model notes

The runner serves one chat model at a time on one of the engines `vllm`,
`llamacpp`, `ds4`, `exllamav3`. `local:<name>` resolves a local GGUF via `-m`. A
bare repo id uses `-hf` (which can pick the wrong shard).

- **Engine versions are opt-in pseudo-flags**, never a default swap:
  `--llama-v080` and `--vllm-032` pick a specific binary for THAT launch
  (flag table in [`vllm-runner-rs/src/flags.rs`](../vllm-runner-rs/src/flags.rs),
  split per engine into `LLAMA_BIN_FLAGS` / `VLLM_BIN_FLAGS`, so a llama.cpp
  launch cannot be pointed at a vLLM binary).
  The **superseded builds were deleted on 2026-10-29** (37 GB back):
  `atomic-llama`, `llama-cpp-upstream`, `llama-cpp-k2horizon`, `llama-prism`,
  `venvs/vllm025`, `venvs/vllm-028`, `venvs/vllm-next`, `venvs/vllm-nightly`.
  Their pseudo-flags (`--llama-k2`, `--llama-next`, `--llama-prism`,
  `--llama-atomic`, `--vllm-025/027/028/nightly`) were removed **from the runner
  and from the parity fixture**: a flag whose binary is gone fails a launch with
  a misleading message, so it must not exist. `--llama-clef` is NOT one of them
  — the decisions engine has its own binary (`llama-b11936`) and needs
  `--mmproj`, a newer llama.cpp than the chat one.
- **`llamacpp`** uses `LLAMA_BIN` (env), which comes from `.env` and not from the
  code default. It points at your llama-server build (the
  TurboQuant fork, `bailingmoe3` + `deepseek4`). Both builds are dynamic
  wrappers: each `build*/bin/` folder is self-contained, so you switch from one
  to the other **by the symlink alone** — the path is resolved at each spawn, the
  environment variable only at import. The TurboQuant binary is kept alongside as
  `llama-server.fork-turboquant` (restore it if Ling-3.0 comes back).
- **Ling-3.0-flash (BailingMoeV3)**: custom arch, served via the TurboQuant
  fork. GGUF tensors are `ssm_f`/`ssm_a` (AtomicChat quant), not the upstream
  PR's `ssm_f_a`. Native 256k context. The GGUF under-declares `n_ctx_train`, so
  pass `--rope-scaling none` (the rope-scaling warning is cosmetic).
- **DeepSeek-V4-Flash-0731**: `deepseek4` arch (hybrid sliding-window + MLA), 1M
  native context, **thinking model** → pass
  `--chat-template-kwargs '{"enable_thinking":false}'`. In general, always
  disable thinking in the chat template unless the user wants visible reasoning.
- **Qwen3.8-Flash-Next IS multimodal, but served without vision.** Without
  `--mmproj`, `llama-server` announces `{"vision": false}` and answers HTTP 500
  `image input is not supported`. The projector exists (`mmproj-…-F16.gguf`,
  907 MB) but its HF repo is gated. `--mmproj` is on the allow-list with the same
  rule as `--chat-template-file`: FILE NAME ONLY, resolved next to the weights —
  no absolute path, no `..`, no sub-folder.
- **Qwen3.8-27B-Uncensored: GGUF + vision.** Arch `qwen35` (known to the three
  llama.cpp builds), `Q8_0` from `local:qwen38-27b-turbo-fable-cold-fusion-q8`
  (DavidAU *TURBO-Fable-Cold-Fusion-735-882-Heretic-Uncensored-NEO-CODER-MAX*),
  native context 262 144 over 4 slots, **52 GB resident out of 121** (hybrid
  attention keeps a small KV cache). With `--mmproj` (`mmproj-F16.gguf`, 931 MB,
  same repo) the server announces a multimodal model and describes a base64 image
  through LiteLLM correctly (measured on this box: it reads a screenshot of the
  decisions page as « une configuration de décision avec trois questions »).
- **Flash Next IQ3_S oversubscribes memory at 3 × 262 144, and it costs 3.5×.**
  Measured on 2026-10-28, same prompt, same binary: with
  `--ctx-size 786432 --parallel 3 --kv-unified --kv-unified-per-slot 262144`
  the engine allocates 83.6 GiB on the GPU, leaves 21 GiB free and decodes at
  **5.5 tok/s** (893 tokens in 2 min 41) — the 786 432-token KV pool (~30 GiB at
  ~39 KiB/token) does not fit next to the 83.86 GiB of weights, so it spills to
  host memory and every decoded token reads it back. With **2 slots**
  (`--ctx-size 524288 --parallel 2`, same 262 144 per-slot window) the GPU holds
  73 GiB, 38 GiB stay free and it decodes at **19.1 tok/s**. The advertised
  contract is unchanged (196 608 input / 65 536 output per session); the price
  is one session fewer. Symptom before the fix: the `reasoning`, `web search` and
  `image tool` journeys all failed the same way — the stream never ended within
  their timeout.
- **`-MTP` GGUFs load but their MTP block is ignored by llama.cpp.** The repo
  offers both plain and `-MTP` quants; the MTP file declares
  `qwen35.nextn_predict_layers = 1` and `block_count = 65`, and the engine warns
  `unused tensor blk.64.nextn.* -- ignoring` — no speculative decoding, no
  speedup, 15 dead tensors. The measured facts, not a guess: the file loads and
  answers, but the 65th block is unused. `nextn` exists in the binary only for
  `bailingmoe3`.
- **DFlash is a vLLM feature and does not follow a GGUF move.** `z-lab/Qwen3.8-27B-DFlash2`
  is safetensors only (3.8 GB, no GGUF) and was driven by
  `--speculative-config {"method":"dflash"}` on the vLLM engine; llama.cpp's
  speculative path wants a GGUF draft (`--spec-draft-*`). The 27B's DFlash is
  therefore **not** re-wired when the weights become a GGUF — the alias keeps its
  name, the draft is lost, and MTP was the only native replacement (ignored, see
  above).
- **Ternary-Bonsai-2-27B (Q2 + vision)** is the first model that requires a
  third-party binary: **402 of its 851 tensors carry a ggml type our builds do
  not know** (id 142), so loading it fails and a "just to see" attempt costs the
  memory of the served model. Know it BEFORE loading: HTTP `Range` request on the
  GGUF (32 MiB is enough) + read the header (`general.architecture`, type
  projection), then `strings`/`nm` on the candidate binary's `.so` files. It runs
  on the Prism ML fork build (`ubuntu-vulkan-arm64`, `b10743`,
  `/root/llama-prism/…`, overridable via `LLAMA_BIN_PRISM`), via `--llama-prism`.
  Three pitfalls: that build is older and ignores `--kv-unified-per-slot` (use
  `--ctx-size 1048576 --parallel 4` without `--kv-unified` for 4 private 256k
  slots). Its projector is the same as the Qwen3.8-27B one. The `--llama-prism`
  pseudo-flag only exists for the runner **after a restart** (`_BIN_FLAGS` is
  built at import), and that restart kills the served model.
- **K2-Horizon**: entry **removed from the catalog** and `Q8_0` weights (37.1 GiB)
  **deleted** on 2026-09-13. Serving it again requires re-downloading them, and
  the `k2_horizon` arch is in no llama.cpp build of this repository (fork only,
  opt-in `--llama-k2`). Its reasoning is separate (`reasoning_content`) despite
  the home-made `<ifm|think>` tags, but a per-request `reasoning_effort` leaks
  `</ifm|think_faster>`: only use the Reasoning button.
- **`exllamav3` (TabbyAPI), added 2026-10-01 for MiMo-V2.6-Flash-RL.** The model
  (309B) only exists in a complete version that fits on the GB10 as EXL3 2.27 bpw
  (`benthecarman/MiMo-V2.6-Flash-RL-exl3`, 85.28 GiB). No aarch64 wheel:
  exllamav3 1.5.3 is COMPILED in `/root/venvs/exl3` (torch 2.13+cu130,
  `TORCH_CUDA_ARCH_LIST=12.1`), TabbyAPI in `/root/tabbyAPI`. Pitfalls: TabbyAPI
  writes `logs/` in its own folder (must belong to `vllmrunner`). `/v1/models`
  lists FOLDER NAMES, so the runner republishes a folder of links
  `~/exl3/model/<catalog name>` at every launch. The DFlash drafter returns 3–4
  tokens per SSE fragment (count fragments and you divide throughput by 3 —
  measure on `usage.completion_tokens`). Two flags are pinned by the runner,
  out of reach of `vllm_args`: `--disable-fetch-requests` (otherwise TabbyAPI
  fetches images by URL itself = SSRF) and `--allowed-origins
  https://no-browser.invalid` (auth disabled + CORS `*`). LOCAL PATCH to
  TabbyAPI (`vllm-runner/patches/tabbyapi-tool-calls-only-when-declared.patch`,
  to reapply after any `git pull` in `/root/tabbyAPI`): TabbyAPI parsed
  `<tool_call>` even with no declared tool. Limits: no `/metrics` (404), 6
  sessions sharing `--cache-size 262144`, ~10 GB available once loaded. A single
  session runs 17–40 tok/s depending on the DFlash acceptance rate (8 % → 17,
  41 % → 40). 6 simultaneous ~5 tok/s each, ~28 total.
- **`auto-model` alias**: a virtual LiteLLM model (`AUTO_MODEL_NAME`, default
  `auto-model`) that always routes to the **currently-running** chat model.
  Re-pointed on every successful launch (`_point_auto_model()`). The real model
  names stay registered and keep working in parallel. It follows only *launches*,
  not catalog adds. Persisted in the LiteLLM DB, so it survives restarts/reboots.

### Model context — how the advertised window is computed

**What the portal DISPLAYS as "input / output context" is computed, and the
formula decides everything** (`ctx_split`, single source shared with LiteLLM):
`input = --ctx-size − --n-predict` and `output = --n-predict`. Consequence:
**changing `--ctx-size` alone is not enough** to get the advertised pair. State as
of **2026-09-14**, "path D" chosen (4 PRIVATE 256k contexts, advertised 196608 /
65536): `--ctx-size 1048576 --parallel 4 --n-predict 65536 --n-gpu-layers 999
--flash-attn on --rope-scaling none` → engine log `n_slots = 4, n_ctx_slot =
262144, kv_unified = 'false'`, **109 GiB used, ~3 free**, **25.6 tok/s** solo
(unchanged versus 26–28 tok/s unified: partitioning costs ONLY memory). Verified
under real load: 4 simultaneous requests → 4 slots busy, ~13.7 tok/s each, ~55
combined. Clients see `max_input_tokens` equal to the advertised input —
**196608** today, the REAL limit of a prompt.

A session's window is **shared** between its prompt and its generation
(llama.cpp): announcing "X input + Y output" describes a convention, not two
independent budgets — you need X + Y ≤ the slot window. The memory knobs and the
pitfalls behind these numbers (`--kv-unified`, YaRN, KV-cache quantization, the
v0.5.0 binary A/B) are in
[troubleshooting → the runner](troubleshooting.md#the-runner).


### `--lazy-mode on`: what it fixes, and why it is not optional here

`qwen4exp` pins `per_layer_token_embd.weight` (iq4_nl, ~27 GiB) OUTSIDE the GPU:
its CUDA `get_rows` kernel requires `ne[0] % QK_K == 0`, and that tensor does not
match — so the loader puts it in **anonymous host memory** (RSS 28 GiB, 18 GiB
free). Forcing it onto CUDA0 with `--override-tensor` was measured twice: 74 GiB
of weights + a 20 GiB KV pool do not fit, and earlyoom kills `llama-server`.

The upstream answer (issue **ggml-org/llama.cpp#29932**, still open) is
`-lzm on` / **`--lazy-mode on`**: the tensor's rows are read **on demand from the
mapped file** instead of being kept resident. Measured on this box, same
arguments otherwise:

| | v0.8.0 | + `--lazy-mode on` |
|---|---|---|
| anonymous RSS | 27.2 GiB | **0.4 GiB** |
| free memory | 18 GiB | **42 GiB** |
| load time | 72 s | 16 s |
| decode | 18.3 tok/s | 17.2 tok/s |

~10 % of throughput for 26 GiB back. It is part of the served model's arguments
(`model_configs.vllm_args`); without it the box runs with 27 GiB of pageable
memory that the kernel OOM killer CAN see (unlike GPU allocations).

## The decision model

A decision model is not a chat model: Cloudflare **Clef** reads a state and a
schema of typed questions and returns probabilities in **one forward pass**. It
has no chat endpoint, so it cannot be served on `:8000` without replacing the
chat model — and it must not. The runner therefore keeps a **second**
`llama-server`, on its own port (`8002` by default), started and stopped
explicitly:

| Route (runner) | Effect |
|---|---|
| `GET /decisions/status` | state, port, pid, memory left — what the page shows |
| `POST /decisions/start` | spawn it, after the memory floor check |
| `POST /decisions/stop` | kill **that** process only |
| `GET /decisions/logs` | its startup journal, capped like the chat model's |

Rules, all of them measured or structural:

- **It is never started automatically.** No auto-resume, no boot start: it costs
  ~17.65 GB of the unified pool, and the chat model already holds most of it.
- **The memory floor applies** (`MIN_FREE_TO_LAUNCH_GIB`, 30 GiB by default): a
  start below it is **refused**, with the number in the message. The fix is to
  stop a media sidecar — never to shrink the chat model.
- **The runtime guard is the same**: the runner kills this process (only this
  one) if `MemAvailable` falls under `CRITICAL_FREE_GIB`.
- **LiteLLM reaches it on the docker bridge**, like `:8000`: `vllm-restrict.service`
  admits `8002` from `172.19.0.0/16` and loopback, and drops it from the VPN.
  Without that port admitted, the portal sees the engine and LiteLLM does not.
- **No streaming.** A decision is one pass; LiteLLM documents the decision routes
  as non-streamed.
- **Concurrency is not promised.** llama-server's slot behaviour for a decision
  model was not measured on this box; assume one request at a time until you have
  measured otherwise.

### Activating it (manual, not done by an update)

Nothing below happens on its own: no weight is downloaded by the platform.

1. **Get a `llama-server` that knows the architecture and the endpoint.** Clef
   landed in llama.cpp around **b11430**; the chat binary on this box is older
   and knows neither the `clef` architecture nor `/v1/systemone` (checked with
   `strings`: no `systemone` at all). The project publishes prebuilt aarch64
   CUDA builds, and they work on the GB10: **b11936**
   (`llama-b11936-bin-ubuntu-cuda-13.4-arm64.tar.gz` plus its
   `cudart-…-cuda-13.4-arm64.tar.gz` runtime, unpacked side by side) runs on the
   r580 driver **without `LD_LIBRARY_PATH`** — the binary carries an `$ORIGIN`
   rpath. Measured end to end on this box: a real request came back with
   *facturation* at 0.9899, confidence 0.98, and an urgency score of 2.49 on 3.
   Keep it **separate** from the chat binary (`LLAMA_BIN_DECISIONS`): upgrading
   the one serving chat is the risk this split removes.
2. **Download the quant** into the models directory, under the slug you will
   reference:
   ```bash
   mkdir -p "$MODELS_DIR/clef-q4km"
   hf download bartowski/Cloudflare_clef-GGUF \
     --include "Cloudflare_clef-Q4_K_M.gguf" --local-dir "$MODELS_DIR/clef-q4km"
   ```
   `DECISIONS_MODEL=local:clef-q4km` resolves the first `*.gguf` shard of that
   folder, the same resolver the chat models use.
3. **Set the keys** — in `/etc/vllm-runner/paths.env` for the runner (which
   needs `DECISIONS_MODEL`, `LLAMA_BIN_DECISIONS`, `DECISIONS_PORT`), and in
   `.env` for the portal if you want it to know the declaration before the
   runner reports it. See [configuration](configuration.md#configuration).
4. **Restart `vllm-runner`** so it reads them — the environment is read at
   startup. This **kills the chat model**: do it in a window, it is not a
   zero-downtime change.
5. **Admit the port**: install/restart `vllm-restrict.service` (the tracked unit
   now lists `8002`). A container that cannot reach it gets a timeout, not an
   error message.
6. **Start it** from Admin → Décisions (or `POST /decisions/start` on the runner
   with the token). A successful start **registers the LiteLLM route** with
   `mode: decisions`; a refused start registers nothing.
7. **Verify end to end**, with a user key:
   ```bash
   curl https://api.example.org/v1/systemone \
     -H "Authorization: Bearer sk-…" -H "Content-Type: application/json" \
     -d '{"model":"clef-q4km","state":"test","questions":{"ok":{"type":"noul","instructions":"Is this a test?"}}}'
   ```
   `noul` must come back as a probability. A 400 naming the model means the route
   is not registered; a 503 means the engine is not running.

Optional: the repository also ships a multimodal projector
(`mmproj-Cloudflare_clef-f16.gguf`). The page does **not** offer images in this
version: adding it means passing `--mmproj` in `DECISIONS_ARGS` and teaching the
page to send `files`. Until the whole path is validated, treat the served model
as **text-only**.

## Concurrent sessions (the served chat model)

The advertised contract is **per session**: `input = --ctx-size − --n-predict`,
`output = --n-predict`. How many sessions coexist is `--parallel`, and the KV
pool is `--kv-unified` + `--kv-unified-per-slot N`.

Current configuration: `--ctx-size 786432 --parallel 3 --kv-unified
--kv-unified-per-slot 262144` → **3 sessions of 262 144**, 196 608 in / 65 536
out each, ~28-32 GiB free, 16.2 tok/s solo (17.2 with two sessions: the third
costs ~6 %).

**Measured limit**: three sessions and the image/video sidecars do not fit
together on 121.6 GiB. With `--parallel 3` the FLUX sidecar (~15 GB) drove the
driver into `NV_ERR_NO_MEMORY` and the served model was killed (2026-10-29,
18:34). The memory guard now refuses to start a sidecar rather than let that
happen, and the portal answers `image_service_off` with the free memory. To have
images AND video alongside chat, go back to `--parallel 2 --ctx-size 524288`.

## Media sidecars

OCR, video, image, voice and dictation run in separate containers, each
with its own docker network and GPU slice, started by the admin or at first use
and **absent from memory when idle** (one GPU cannot hold the chat model and
several media models at once). They are **never exposed as separate public UIs**:
everything is streamed into the portal, and an unloaded page says so instead of
failing.

- **Never put a `--memory` limit on a sidecar.** On unified memory a container
  memory cap *also caps GPU memory* and breaks CUDA model loading.
- The **dictation ASR sidecar is on-demand**: auto-start on first use (memory
  guard ≥ 8 GiB on `MemAvailable`), 60 s start throttle, idle reaper stops it
  after 10 min (file state in `/tmp/dgx-asr-ondemand`, safe across the 4
  gunicorn workers), an admin start pins it for 1 h. After a reboot, an `asr`
  brought back by `restart: unless-stopped` lives until the first dictation +
  10 min idle.
- If a model launch OOMs, **stop a sidecar from Admin** — don't shrink the chat
  model's context. Media errors carry the actual free memory plus « arrête le
  modèle depuis Admin » for the same reason (MiMo holds ~100 of the 121.6 GiB,
  and even a STARTED sidecar may not find the memory to load).
- Host wrappers (`/usr/local/sbin/*-recreate.sh`, `sudoers` fragments) are
  root-owned on the host. Their tracked sources live in the sidecar folders —
  see [architecture](architecture.md#frontend-dgx-portal-frontend).

## Web search (SearXNG + crawl4ai)

Two sidecars on the dedicated `web_net`, neither published: **SearXNG** (`:8080`)
turns a question into links, **crawl4ai** (`:11235`) reads the pages (it cannot
search — it only extracts URLs it is given). Rules that are not obvious:

- The SearXNG `settings.yml` lives in `./searxng/` (git-ignored, holds a
  generated secret) and **must** keep `formats: [html, json]`: JSON is off by
  default.
- crawl4ai refuses to listen beyond loopback without a credential. The token is
  generated once into `./secrets/` and mounted at `/run/secrets/api_token`. The
  file must be world-readable (0644) — the container runs unprivileged and cannot
  read a 0600 root-owned mount.
- **crawl4ai is the most exposed component on this box** — it drives a browser
  over pages entirely controlled by third parties. It has no route to litellm,
  postgres or traefik, and `dgx-web-restrict.service` drops every new
  connection from `web_net` to the host. Never attach it to `default`.
- **Every URL is resolved and checked public before it reaches the crawler**
  (`websearch.public_url`). The network cannot do this: a hostile page can
  redirect anywhere.
- Extraction settings were chosen by measurement (`test_websearch.py`):
  `ignore_links` + `excluded_tags` cut 61–75 % of volume without losing substance.
  `PruningContentFilter` was tried and **rejected** — it strips code blocks.
  **Banners are cleaned line by line, pages are not discarded.**
- The tool phase runs **inside** the SSE generator and emits progress events.
  Running it before the response starts left the client with no bytes for tens of
  seconds and the frontend proxy gave up.

## Maintenance mode

An admin toggle that blocks non-admin API/portal traffic **without stopping any
model**, enforced both in the portal and at the edge (Traefik forwardAuth on
`/internal/authcheck/`). Every toggle notifies the alert recipient (Admin →
Config). Destructive actions ask for confirmation first.

## Counters and the dashboard

What the home page and the admin panel actually show — the display conventions
are contracts, not decoration:

- **`/api/modelhealth`** exists for the **1 s** dashboard refresh (throughput +
  TTFT). `/api/home` stays at 5 s (it aggregates spend and probes the sidecars).
  The `vllm_health()` cache is at 1 s.
- **The llama.cpp throughput is read from `n_decode_total`, and from nothing
  else**: during a generation `predicted_tokens_seconds` is 0 and
  `tokens_predicted_total` does not advance (llama.cpp updates them only at the
  END of each request). The displayed value is `Δn_decode_total / Δt ×
  requests_processing` — the factor is NOT optional: one decode step produces one
  token PER active slot, and without it the dashboard divided the real throughput
  by the number of sessions (measurement of 2026-09-11: **17.4 displayed for 69.5
  tok/s actually delivered** with 4 sessions). Pitfall:
  `n_busy_slots_per_decode` looks made for this but is an average since startup —
  unusable.
- **llama.cpp has no request counter**: displaying `n_decode_total` as "served
  requests" announced 39 303 requests for 39 303 tokens. `'requests'` is `None`
  for this engine and the UI shows « — ».
- **LiteLLM strips llama.cpp's `timings`** (verified on a real stream) — the TTFT
  shown is measured by the portal (request start → first token), which is in any
  case the delay actually endured.
- **A throughput of 0 does NOT mean "engine idle": most often it is PREFILL**
  (2026-09-14: 4 requests in flight, `n_decode_total` advancing by a single step
  in 6 s, prefill at 248 tok/s). Hence the input counters (`tokens_prompt`,
  `tps_prefill`) on the home page.
- **Cumulative counters and their exact averages**: `tokens_predicted_total /
  tokens_predicted_seconds_total` is a RATIO of two engine counters (158 729 /
  11 921.9 = **13.3 tok/s** on this server), insensitive to probe frequency. vLLM
  publishes no generation seconds: `tps_moyen` stays `None`, we invent nothing.
  `prompt_tokens_seconds` is a gauge of the **LAST** prefill (0 when idle) — so
  `tps_prefill` is `None` when nothing has just been ingested. The decode average
  is hidden below 300 s of cumulative generation: after a counter reset it would
  read 47 tokens / 192 s = 0.2 tok/s, worse than nothing.
- **The "generated tokens" total survives engine counter resets**
  (`model_counters`, SQLite): the last seen value is archived when the counter
  moves backwards. Measurement of 2026-09-14: a drop is **NOT necessarily a
  relaunch** — `tokens_predicted_total` went from 158 864 to 47 with the same pid
  (7 h 56, `NRestarts=0`): a KV cache reset gives the same signature. For an
  accounting figure, the reference is LiteLLM (`SUM(completion_tokens)`).
- **The admin panel « qui utilise le modèle » carries AGES, far more than a flag.**
  `live` lights up for everyone as soon as the engine works (three accounts
  marked « en direct » with 11 h, 32 min and 4 min of gap, 2026-09-14), so the
  panel returns `derniere_s` (age of the last activity) and `live` requires
  activity of **less than 15 s** within a 30-minute window. The throughput shown
  next to it is the engine's **GLOBAL** throughput — never a per-person one.
- **A request IN FLIGHT has NO identity in LiteLLM or the engine**: SpendLogs
  rows land at request END (44 minutes without a single write while two sessions
  worked), LiteLLM exposes no in-flight endpoint, and `requester_ip_address` is
  Traefik. The only OBSERVED attribution is the **`dgx_inflight` callback**
  (`litellm/litellm_inflight.py`, mounted into the proxy): it records request
  openings/closings in `./runtime/inflight.db`, mounted read-only on the portal
  side (`DGX_INFLIGHT_DB`). Rules not to "simplify": the **synchronous** hooks
  are the only ones this LiteLLM version calls (an async-only class gets total
  silence — table created, table empty, no error). `docker compose up -d` does
  not reload a modified mounted file (use `restart`). It is SQLite because the
  proxy image has no PostgreSQL driver. A write error never surfaces (it goes to
  the proxy's error output). The **oldest** request of an account gives the
  displayed age. Expired rows are swept after 2 h. A missing file falls back to
  the previous behaviour.
- **`vllm_health()` exposes `slots`** (llama.cpp `/slots`): busy sessions, age of
  the oldest one, and ingestion progress — the only description available DURING
  a request. `/slots` does not exist on vLLM (→ `None`, never 0). The age restarts
  from zero when the portal restarts.
- **Engines without `/metrics` (TabbyAPI)** get their dashboard back from
  LiteLLM's SpendLogs, plus a live decode gauge: `chat_routes` counts the
  streamed characters (`tokens ≈ chars/4`, calibrated later on the exact
  `usage.completion_tokens` of each finished request — measured 3.44 chars/token
  on MiMo) and `stats.live_decode_rate()` divides by the **decode time**, not
  the request age. The prefill rate = `usage.prompt_tokens / measured TTFT`.
- **LiteLLM does not throttle throughput** (verified 2026-09-11: 33.0 tok/s
  non-stream and 33.3 streaming through the proxy against 32.2 direct). A lower
  perceived throughput comes from elsewhere: the playground measures FROM THE
  FIRST TOKEN (TTFT excluded), and concurrency (4 sessions drop to ~18 tok/s each
  for 70 combined).

## systemd services

| Unit | Role |
|---|---|
| `vllm-runner.service` | The runner daemon (non-root `vllmrunner` user) |
| `vllm-restrict.service` | iptables: host ports **8000**/**8001**/**8188** limited to localhost + Docker bridge, and dropped on the VPN interface in the `raw` table (where the VPN's own ACCEPT cannot pre-empt it) |
| `dgx-docker-restrict.service` | DOCKER-USER rules: each Docker-published port listed in `/etc/default/dgx-firewall` (site file, template `systemd/dgx-firewall.example`) only accepts the sources listed there; IPv6 keeps loopback only. Lists the CONTAINER port (the web UI published 5000→3000 is **3000**) |
| `dgx-web-restrict.service` | Drops new connections from the web-search network to the host |
| `dgx-ocr-restrict.service` | Prevents the OCR container from opening a connection to the portal |
| `dgx-traefik-boot.service` | One-shot at boot: waits for DNS, then restarts Traefik once (avoids the plugin/ACME race that 404s the site after a reboot) |

## Monitoring and backups (host, systemd)

Systemd timers + scripts (in [`monitoring/`](../monitoring/) and [`scripts/`](../scripts/)), running **as
root** on the host (access to the docker socket + the 600 `.env`).

- **`dgx-monitor.timer`/`.service`** (every 5 min) → `monitoring/monitor.py`:
  probes the always-up **core** (`vllm-runner` `:8001`, containers `traefik` /
  `litellm` / `litellm-postgres` / `portal-rs` / `dgx-portal-frontend`) and sends an
  alert email to the alert recipient (Admin → Config) when a service goes down,
  then a recovery email. The SMTP settings are read through the portal
  container and kept in a root-only copy (`/var/lib/dgx-monitor/smtp.json`), so
  the alert still goes out when the portal itself is down.
  **Data integrity**: a conversations/media-jobs watermark is stored in the sticky
  state. A drop below 40 % of the watermark (database reset — happened on
  04/09/2026, stayed invisible for 3 days) triggers the same alert. **Sticky**
  state in `/var/lib/dgx-monitor/state.json` (1 alert per incident). The media
  sidecars are **on-demand**: not probed, to avoid false positives. The monitor
  also probes the **freshness of the last backup** (`/var/backups/dgx/state.json`,
  < 26 h): a missing/old backup counts as a service down.
- **`dgx-backup.timer`/`.service`** (daily 03:00) → `scripts/backup.sh`:
  consistent SQLite copy of the portal database, `pg_dump` of the LiteLLM
  database, `docker-compose.yml`, `.env` and `secrets/`, archived. **Admin →
  Stockage** chooses where it goes (local `/var/backups/dgx/archives`, 0700,
  the S3 bucket of Admin → Config, or both) and how many archives each keeps
  (default 7). The upload runs inside the portal (`portal-rs/src/backups.rs`: the
  bucket keys never leave it) and the archive is **read back from the bucket**
  before the run counts as a success; a failed upload keeps the archive locally
  and the run reports the failure. It writes `/var/backups/dgx/state.json`,
  which the monitor and the Admin panel read, and runs the **orphan sweep** of
  the generated files (`portal-rs/src/media_store.rs`). **The archive contains `.env`
  and the databases — the bucket must be private.**
  The portal also writes a `.db_initialized` marker: marker present + empty
  database at startup → infra alert. Restoration: download the archive, then
  `docker cp` of the `.db`/SQL dump and `pg_restore`/`psql`.
  **Timeout trap, measured 2026-10-29**: the S3 client used ONE 60 s timeout for
  every operation, uploads included. The archive grew from 38 to 65 MB, the
  upload took longer than a minute, and the backup failed with the useless
  message « error sending request ». Transfers now get **900 s**, metadata calls
  keep 60 s, and the error names the cause (timeout with its budget, unreachable
  endpoint, or a plain failure). If a backup fails again, read the message before
  suspecting the network.
- **`dgx-logs.timer`/`.service`** (daily 03:30) → `scripts/logs-to-s3.sh`:
  ships the platform logs to `DGX_S3_LOGS`.
- **`dgx-validation.path`/`.service`** → `scripts/validate.sh`, started when the
  Admin's « run the validation » button writes `runtime/validation/demande.txt`.

Health endpoints (portal side): `GET /healthz` gives minimal **public** liveness
`{ok, time}` (healthcheck / probe). `GET /api/health` gives aggregated
**connected** state (`runner`, `litellm`, `chat`, `video/ocr/voice/image` as
`ready`/`on_demand`).

## After a reboot

See [troubleshooting → reboot recovery](troubleshooting.md#after-an-unexpected-reboot):
auto-resume restores the model, `dgx-traefik-boot.service` handles the
Traefik/DNS race, and a short checklist covers the rest.

## Updating a sidecar image (the tag is not obvious)

The recreate scripts are what the runner calls, and they name the image the
container will run — not the tag you may have built by hand:

- `asr-recreate.sh` → **`whisper-asr:turbo-cu130`** (build with
  `docker build -f asr/Dockerfile -t whisper-asr:turbo-cu130 asr/`)
- `image-recreate.sh` → **`ai-platform-image-gen`** (build with
  `docker build -f image-gen/Dockerfile -t ai-platform-image-gen image-gen/`)

A rebuild under another tag changes nothing: the running container keeps the old
image, and the version check (`docker exec asr python3 -c
'import torch; print(torch.__version__)'`) is the only way to know. Measured on
2026-10-29: the ASR test passed on a container still running torch 2.10 while the
new image (2.14.1) sat unused, because the container had not been recreated.
After building, recreate with the script (`HOME=/root bash
asr/asr-recreate.sh openai/whisper-large-v3-turbo`,
`sudo /usr/local/sbin/image-recreate.sh black-forest-labs/FLUX.2-klein-4B`).
