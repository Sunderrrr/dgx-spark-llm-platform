# Operations

Day-to-day runbook: launching and stopping the model, the sidecars, what the
dashboard numbers mean, and the host services around the stack. Traps and
symptoms are in [troubleshooting](troubleshooting.md); the deeper operating guide
kept on the machine ([`CLAUDE.md`](../CLAUDE.md)) carries the full measurement
history.

> **Golden rule: never stop or restart the served chat model without explicit
> go-ahead.** A restart kills whatever is loading/serving with no warning.
> Auto-resume on boot is fine (it *restores*); deliberately restarting a running
> model is not. Restarting `vllm-runner` KILLS the served model too — the unit
> has no `KillMode`, so the `llama-server` it spawned lives in the
> `vllm-runner.service` cgroup (verified via `/proc/<pid>/cgroup`). Any change to
> `runner.py` that must take effect therefore needs the same downtime window as a
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
A flag absent from the allow-list is refused **before** the spawn;
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
reset only after 600 s of uninterrupted running; a **memory floor** before any
launch (`MIN_FREE_TO_LAUNCH_GIB`, 30) that refuses to start when the previous
model's memory has not been returned; a **runtime guard** that kills its own
llama-server under `CRITICAL_FREE_GIB` (2) before the kernel's blind OOM; all
resumes through the watchdog; unit `RestartSec=30`, `OOMPolicy=continue`.

## Engines and model notes

The runner serves one chat model at a time on one of the engines `vllm`,
`llamacpp`, `ds4`, `exllamav3`. `local:<name>` resolves a local GGUF via `-m`; a
bare repo id uses `-hf` (which can pick the wrong shard).

- **Engine versions are opt-in pseudo-flags**, never a default swap:
  `--vllm-025`, `--vllm-027`, `--vllm-028`, `--vllm-nightly`, `--llama-next`,
  `--llama-k2`, `--llama-prism` pick a specific binary for THAT launch
  (`_BIN_FLAGS` in [`runner.py`](../vllm-runner/runner.py), split per engine into
  `_LLAMA_BIN_FLAGS` / `_VLLM_BIN_FLAGS`, so a llama.cpp launch cannot be pointed
  at a vLLM binary). vLLM **0.28.0** (`/root/venvs/vllm-028`, torch 2.13 cu130,
  sm_121 verified — adds BailingMoeV3) and llama.cpp **0.3.0-dev upstream**
  (`/root/llama-cpp-upstream`, built for GB10 — adds `qwen4exp`) are available
  this way; neither is the default.
- **`llamacpp`** uses `LLAMA_BIN` (env), which comes from `.env` and not from the
  code default. It points at your llama-server build (the
  TurboQuant fork, `bailingmoe3` + `deepseek4`). Both builds are dynamic
  wrappers: each `build*/bin/` folder is self-contained, so you switch from one
  to the other **by the symlink alone** — the path is resolved at each spawn, the
  environment variable only at import. The TurboQuant binary is kept alongside as
  `llama-server.fork-turboquant` (restore it if Ling-3.0 comes back).
- **Ling-3.0-flash (BailingMoeV3)**: custom arch, served via the TurboQuant
  fork; GGUF tensors are `ssm_f`/`ssm_a` (AtomicChat quant), not the upstream
  PR's `ssm_f_a`. Native 256k context; the GGUF under-declares `n_ctx_train`, so
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
  llama.cpp builds), `Q8_0` (27.1 GiB) from `local:qwen38-27b-gguf`, native
  context 262 144 over 4 slots, **52 GB resident out of 121** (hybrid attention
  keeps a small KV cache). With `--mmproj` (`mmproj-Qwen3.8-27B-Uncensored-f16.gguf`,
  931 MB) the server announces `{"vision": true, "video": true}` and correctly
  describes a base64 image through LiteLLM (measured: "It's a red circle" in 2.2 s).
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
  slots); its projector is the same as the Qwen3.8-27B one; and the `--llama-prism`
  pseudo-flag only exists for the runner **after a restart** (`_BIN_FLAGS` is
  built at import) — and that restart kills the served model.
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
  writes `logs/` in its own folder (must belong to `vllmrunner`); `/v1/models`
  lists FOLDER NAMES, so the runner republishes a folder of links
  `~/exl3/model/<catalog name>` at every launch; the DFlash drafter returns 3–4
  tokens per SSE fragment (count fragments and you divide throughput by 3 —
  measure on `usage.completion_tokens`); and two flags are pinned by the runner,
  out of reach of `vllm_args`: `--disable-fetch-requests` (otherwise TabbyAPI
  fetches images by URL itself = SSRF) and `--allowed-origins
  https://no-browser.invalid` (auth disabled + CORS `*`). LOCAL PATCH to
  TabbyAPI (`vllm-runner/patches/tabbyapi-tool-calls-only-when-declared.patch`,
  to reapply after any `git pull` in `/root/tabbyAPI`): TabbyAPI parsed
  `<tool_call>` even with no declared tool. Limits: no `/metrics` (404), 6
  sessions sharing `--cache-size 262144`, ~10 GB available once loaded; a single
  session 17–40 tok/s depending on the DFlash acceptance rate (8 % → 17, 41 % →
  40), 6 simultaneous ~5 tok/s each, ~28 total.
- **`auto-model` alias**: a virtual LiteLLM model (`AUTO_MODEL_NAME`, default
  `auto-model`) that always routes to the **currently-running** chat model.
  Re-pointed on every successful launch (`_point_auto_model()`); the real model
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

## Media sidecars

OCR, video, image, music, voice and dictation run in separate containers, each
with its own docker network and GPU slice, started by the admin or at first use
and **absent from memory when idle** (one GPU cannot hold the chat model and
several media models at once). They are **never exposed as separate public UIs**:
everything is streamed into the portal, and an unloaded page says so instead of
failing.

- **Never put a `--memory` limit on a sidecar.** On unified memory a container
  memory cap *also caps GPU memory* and breaks CUDA model loading.
- The **dictation ASR sidecar is on-demand**: auto-start on first use (memory
  guard ≥ 8 GiB on `MemAvailable`), 60 s start throttle, idle reaper stops it
  after 10 min (file state in `/tmp/cronos-asr-ondemand`, safe across the 4
  gunicorn workers), an admin start pins it for 1 h. After a reboot, an `asr`
  brought back by `restart: unless-stopped` lives until the first dictation +
  10 min idle.
- If a model launch OOMs, **stop a sidecar from Admin** — don't shrink the chat
  model's context. Media errors carry the actual free memory plus « arrête le
  modèle depuis Admin » for the same reason (MiMo holds ~100 of the 121.6 GiB,
  and even a STARTED sidecar may not find the memory to load).
- Host wrappers (`/usr/local/sbin/*-recreate.sh`, `sudoers` fragments) are
  root-owned on the host; their tracked sources live in the sidecar folders —
  see [architecture](architecture.md#frontend-dgx-portal-frontend).

## Web search (SearXNG + crawl4ai)

Two sidecars on the dedicated `web_net`, neither published: **SearXNG** (`:8080`)
turns a question into links, **crawl4ai** (`:11235`) reads the pages (it cannot
search — it only extracts URLs it is given). Rules that are not obvious:

- The SearXNG `settings.yml` lives in `./searxng/` (git-ignored, holds a
  generated secret) and **must** keep `formats: [html, json]`: JSON is off by
  default.
- crawl4ai refuses to listen beyond loopback without a credential; the token is
  generated once into `./secrets/` and mounted at `/run/secrets/api_token`. The
  file must be world-readable (0644) — the container runs unprivileged and cannot
  read a 0600 root-owned mount.
- **crawl4ai is the most exposed component on this box** — it drives a browser
  over pages entirely controlled by third parties. It has no route to litellm,
  postgres or traefik, and `cronos-web-restrict.service` drops every new
  connection from `web_net` to the host. Never attach it to `default`.
- **Every URL is resolved and checked public before it reaches the crawler**
  (`websearch.url_publique`). The network cannot do this: a hostile page can
  redirect anywhere.
- Extraction settings were chosen by measurement (`test_websearch.py`):
  `ignore_links` + `excluded_tags` cut 61–75 % of volume without losing substance.
  `PruningContentFilter` was tried and **rejected** — it strips code blocks.
  **Banners are cleaned line by line, pages are not discarded.**
- The tool phase runs **inside** the SSE generator and emits progress events;
  running it before the response starts left the client with no bytes for tens of
  seconds and the frontend proxy gave up.

## Maintenance mode

An admin toggle that blocks non-admin API/portal traffic **without stopping any
model**, enforced both in the portal and at the edge (Traefik forwardAuth on
`/internal/authcheck/`). Every toggle notifies `ADMIN_EMAIL`; destructive actions
ask for confirmation first.

## Counters and the dashboard

What the home page and the admin panel actually show — the display conventions
are contracts, not decoration:

- **`/api/modelhealth`** exists for the **1 s** dashboard refresh (throughput +
  TTFT); `/api/home` stays at 5 s (it aggregates spend and probes the sidecars).
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
- **The admin panel « qui utilise le modèle » carries AGES, not just a flag.**
  `live` lights up for everyone as soon as the engine works (three accounts
  marked « en direct » with 11 h, 32 min and 4 min of gap, 2026-09-14), so the
  panel returns `derniere_s` (age of the last activity) and `live` requires
  activity of **less than 15 s** within a 30-minute window. The throughput shown
  next to it is the engine's **GLOBAL** throughput — never a per-person one.
- **A request IN FLIGHT has NO identity in LiteLLM or the engine**: SpendLogs
  rows land at request END (44 minutes without a single write while two sessions
  worked), LiteLLM exposes no in-flight endpoint, and `requester_ip_address` is
  Traefik. The only OBSERVED attribution is the **`cronos_inflight` callback**
  (`dgx-portal/litellm_inflight.py`, mounted into the proxy): it records request
  openings/closings in `./runtime/inflight.db`, mounted read-only on the portal
  side (`CRONOS_INFLIGHT_DB`). Rules not to "simplify": the **synchronous** hooks
  are the only ones this LiteLLM version calls (an async-only class gets total
  silence — table created, table empty, no error); `docker compose up -d` does
  not reload a modified mounted file (use `restart`); it is SQLite because the
  proxy image has no PostgreSQL driver; a write error never surfaces (it goes to
  the proxy's error output); the **oldest** request of an account gives the
  displayed age; expired rows are swept after 2 h; a missing file falls back to
  the previous behaviour.
- **`vllm_health()` exposes `slots`** (llama.cpp `/slots`): busy sessions, age of
  the oldest one, and ingestion progress — the only description available DURING
  a request. `/slots` does not exist on vLLM (→ `None`, never 0); the age restarts
  from zero when the portal restarts.
- **Engines without `/metrics` (TabbyAPI)** get their dashboard back from
  LiteLLM's SpendLogs, plus a live decode gauge: `chat_routes` counts the
  streamed characters (`tokens ≈ chars/4`, calibrated later on the exact
  `usage.completion_tokens` of each finished request — measured 3.44 chars/token
  on MiMo) and `stats.debit_decode_live()` divides by the **decode time**, not
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
| `vllm-restrict.service` | iptables: host ports **8000**/**8001** limited to localhost + Docker bridge, and **8188** (ComfyUI) off the LAN |
| `cronos-docker-restrict.service` | DOCKER-USER rules: the published frontend port (**5000** on the host → **3000** in the container) reachable from Traefik's host only, plus **8080**/**8090**. LiteLLM has no published port at all |
| `cronos-web-restrict.service` | Drops new connections from the web-search network to the host |
| `cronos-ocr-restrict.service` | Prevents the OCR container from opening a connection to the portal |
| `cronos-traefik-boot.service` | One-shot at boot: waits for DNS, then restarts Traefik once (avoids the plugin/ACME race that 404s the site after a reboot) |

## Monitoring and backups (host, systemd)

Two systemd timers + two scripts (in [`monitoring/`](../monitoring/)), running **as
root** on the host (access to the docker socket + the 600 `.env`).

- **`cronos-monitor.timer`/`.service`** (every 5 min) → `monitoring/monitor.py`:
  probes the always-up **core** (vllm-runner `:8001`, containers traefik /
  litellm / litellm-postgres / dgx-portal / dgx-portal-frontend) and sends an
  alert email to `ADMIN_EMAIL` when a service goes down, then a recovery email.
  **Data integrity**: a conversations/media-jobs watermark is stored in the sticky
  state; a drop below 40 % of the watermark (database reset — happened on
  04/09/2026, stayed invisible for 3 days) triggers the same alert. **Sticky**
  state in `/var/lib/cronos-monitor/state.json` (1 alert per incident). The media
  sidecars are **on-demand**: not probed, to avoid false positives. The monitor
  also probes the **freshness of the portal dump** (`/var/backups/cronos/portal-*.db`
  < 26 h): a missing/old backup counts as a service down.
- **`cronos-backup.timer`/`.service`** (daily 03:00) → `monitoring/backup.py`:
  SQLite dump of `/app/data/portal.db` (consistent snapshot via `sqlite3.backup`)
  and `pg_dump -Fc` of the LiteLLM database (no password: trust auth inside the
  container). Destination `/var/backups/cronos/`, retention 14 files (`--keep`).
  **These dumps contain the DB data — never pushed.** After the dump, **purge of
  orphan files** from the portal volume (image_files/music_files whose prefix no
  job references any more, 7 days grace; `--no-purge`, `--purge-dry-run`). The
  portal also writes a `.db_initialized` marker: marker present + empty database
  at startup → infra alert. Restoration: `docker cp` of the `.db`/`.dump` then
  open/`pg_restore`.

Health endpoints (portal side): `GET /healthz` — minimal **public** liveness
`{ok, time}` (healthcheck / probe); `GET /api/health` — aggregated **connected**
state (`runner`, `litellm`, `chat`, `video/ocr/voice/image/music` as
`ready`/`on_demand`).

## After a reboot

See [troubleshooting → reboot recovery](troubleshooting.md#after-an-unexpected-reboot):
auto-resume restores the model, `cronos-traefik-boot.service` handles the
Traefik/DNS race, and a short checklist covers the rest.
