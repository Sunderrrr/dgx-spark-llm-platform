# CLAUDE.md — Cronos operating guide for AI agents

This file is the always-on context for any Claude session working in this repo.
It captures the **non-obvious** rules and the **way we work** — not the
architecture, which lives in [`README.md`](README.md). Read the README once for
the map; keep this file's rules in force at all times.

Cronos is a self-hosted LLM platform on a **single NVIDIA DGX Spark** (GB10 Grace
Blackwell, **128 GB unified memory**, aarch64). One box, multi-user, production —
treat it as live infrastructure, not a scratch project.

---

## 🔴 Golden rules — never break these

1. **Never stop or restart the served chat model without explicit user go-ahead.**
   A restart kills whatever is loading/serving with no warning. Auto-resume on
   boot is fine (it *restores*); deliberately restarting a running model is not.
2. **Never put a `--memory` limit on a sidecar container.** On unified memory, a
   container memory cap *also caps GPU memory* and breaks CUDA model loading. See
   [GB10 gotchas](#gb10-gotchas).
3. **Never restore the Traefik geoblock / fail2ban / breidablik middlewares.**
   They were deliberately removed.
4. **Never echo or commit secrets.** Real credentials go only in `.env` (git-ignored)
   and local-account passwords (hashed in `local_users`). Never print a password
   to output, never write one into a tracked file. The pre-push gate (below)
   is a backstop, not a substitute for care.
5. **`.env` is protected by a hook** — it cannot be edited by tools. Hand edits
   to the user.

---

## How we work — the engineering loop

For any non-trivial change, work like a small, disciplined engineering team
("think like Google"): don't jump from request to code. Move through these phases
and make each one visible in what you write:

1. **Need** — state the actual problem in one or two sentences. What breaks, for
   whom, and how we'll know it's fixed. If the request is ambiguous, resolve it
   before designing (grill if useful).
2. **Options** — enumerate the real candidate solutions (usually 2–4), including
   "do nothing." One line each on the trade-off.
3. **Test the options** — when the choice isn't obvious, *prototype or benchmark*
   the top candidates cheaply before committing. On this box that often means a
   throwaway launch, a `curl`, or a tiny script — measure, don't guess.
4. **Choose** — pick one, say why in a sentence, and note what would make you
   revisit it.
5. **Implement** — build it to match the surrounding code (idiom, comment density,
   French-first UI strings). Small, reviewable commits.
6. **Document** — update the docs the change touches **only if it helps a future
   reader**: `README.md` for user/operator-facing behaviour, this file for new
   rules or gotchas, code comments for the *why*. Don't document for its own sake.
7. **Verify & push** — run the pre-push gate (tests + secret scan). Green → push.
   Red → stop and report; never push around a red gate.

The loop is a default, not a ritual: a one-line fix doesn't need a formal options
table. Scale the ceremony to the risk.

---

## GB10 gotchas

- **Unified memory is shared.** GPU allocations count against the same 128 GB as
  system RAM. Only **one chat model** runs at a time; OCR, video (ComfyUI), ASR
  and voice are separate always-on sidecars, each with its own slice. If a launch
  OOMs, **stop a sidecar from Admin** — don't shrink the chat model's context.
- **`docker run --memory` on a sidecar caps GPU memory too** → CUDA load fails.
  Never set it.
- **Build for GB10** (CUDA 13, sm_121):
  ```
  cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON \
    -DCMAKE_CUDA_ARCHITECTURES=121 -DGGML_CUDA_FA=ON -DGGML_CUDA_GRAPHS=ON -DGGML_NATIVE=ON
  cmake --build build -j --target llama-server
  ```

---

## Docker networking gotchas

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

---

## Web search (SearXNG + crawl4ai)

The playground can search the web. Two sidecars, both on the dedicated
`web_net`, neither published:

- **SearXNG** (`:8080`) turns a question into links. crawl4ai cannot search —
  it only extracts URLs it is given, hence two services. Its `settings.yml`
  lives in `./searxng/` (git-ignored, holds a generated secret) and **must**
  keep `formats: [html, json]`: JSON is off by default.
- **crawl4ai** (`:11235`) reads the pages. It refuses to listen beyond loopback
  without a credential; the token is generated once into `./secrets/` and
  mounted at `/run/secrets/api_token`, the path its entrypoint reads. The file
  must be world-readable (0644) — the container runs unprivileged and cannot
  read a 0600 root-owned mount.

Rules that are not obvious:

- **crawl4ai is the most exposed component on this box** — it drives a browser
  over pages entirely controlled by third parties. It has no route to litellm,
  postgres or traefik, and `cronos-web-restrict.service` drops every new
  connection from `web_net` to the host (before that rule, the host's SSH port
  accepted connections from it). Never attach it to `default`.
- **Every URL is resolved and checked public before it reaches the crawler**
  (`websearch.url_publique`). The network cannot do this: a hostile page can
  redirect anywhere.
- **Extraction settings were chosen by measurement**, see `test_websearch.py`:
  `ignore_links` + `excluded_tags` cut 61–75 % of volume without losing
  substance. `PruningContentFilter` was tried and **rejected** — it strips code
  blocks (`TaskGroup` vanished from the asyncio docs).
- **Banners are cleaned line by line, pages are not discarded.** Judging a page
  by its opening threw away pages that held the answer.
- The tool phase runs **inside** the SSE generator and emits progress events.
  Running it before the response starts left the client with no bytes for tens
  of seconds and the frontend proxy gave up.

---

## Model serving

The **`vllm-runner`** systemd daemon (host, user `vllmrunner`, port `8001`, Bearer
`RUNNER_TOKEN`) owns model lifecycle. Launch via **Admin → Launch** or the API
(see README → Operations). Engines: `vllm`, `llamacpp`, `ds4`.

Key facts and gotchas:

- **`vllm_args` is allowlisted.** `--trust-remote-code` and overrides of critical
  flags are blocked for chat models (only OCR may pass trust-remote-code). Don't
  try to smuggle blocked flags — extend the allowlist deliberately if truly needed.
- **`llamacpp` engine** uses `LLAMA_BIN` (env). For GGUFs with a custom arch we run
  a **TurboQuant fork** build at `/root/atomic-llama/build/bin/llama-server`
  (has `bailingmoe3` + `deepseek4`). `local:<name>` resolves a local GGUF via
  `-m`; a bare repo id uses `-hf` (which can pick the wrong shard).
- **Ling-3.0-flash (BailingMoeV3)**: custom arch, *not* supported by vLLM 0.25.1.
  Served via the TurboQuant llama.cpp fork. GGUF tensors are `ssm_f`/`ssm_a`
  (AtomicChat quant), **not** the upstream PR's `ssm_f_a` — the fork matches the
  published GGUF. Native 256k context; GGUF under-declares `n_ctx_train` so pass
  `--rope-scaling none` (the rope-scaling warning is cosmetic).
- **DeepSeek-V4-Flash-0731**: `deepseek4` arch (hybrid sliding-window + MLA),
  1M native context, **thinking model** → pass
  `--chat-template-kwargs '{"enable_thinking":false}'`.
- **Thinking models**: always disable thinking in the chat template unless the
  user wants visible reasoning.
- **`auto-model` alias**: a virtual LiteLLM model (`AUTO_MODEL_NAME`, default
  `auto-model`) that always routes to the **currently-running** chat model, so
  clients wire it once and never rename on a model switch. Re-pointed on every
  successful launch via `_point_auto_model()` (called from `runner_launch`); the
  real model names stay registered and keep working in parallel. It follows only
  *launches*, not catalog adds. Persisted in the LiteLLM DB, so it survives
  restarts/reboots (resume relaunches the same model).

---

## Auth & users

Login order (`login()` in `dgx-portal/app.py`):
`local_users` (hashed) → **LDAP** → **SSO** (`/api/oauth2-redirect`, `via_sso=True`).

- Each successful login records its source in the `user_sources` table
  (`local` / `ldap` / `sso`, cumulative). The **Users** page (`/users`,
  admin-only) shows every known account with source badges.
- **Local user management** is admin-only: create users, assign groups with
  quota/admin rights, hashed passwords. Budget precedence: user override → group
  → global default (`get_setting('default_key_budget')`).
- **The container is hardened** (cap_drop ALL, no-new-privileges). Even
  `docker exec -u 0` can't override file perms (no CAP_DAC_OVERRIDE) — manual
  edits to the data volume must be done **as the portal user (uid 10001)**,
  not root.
- **`docker exec` heredoc gotcha**: `docker exec ... python3 - << 'PY'` produces
  no output in this harness. Write the script to a file, `docker cp` it in, run it.

---

## Reboot recovery runbook

Auto-resume exists (`runner.py` persists `last_model.json` and relaunches on boot,
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

---

## Tests, gate & CI

- **Backend tests** run in a throwaway Docker image (never touches real data):
  `./dgx-portal/run-tests.sh` (all) or `./dgx-portal/run-tests.sh test_app`.
- **Pre-push gate**: `./scripts/pre-push-check.sh` — runs the test suite and scans
  the diff for secrets. **Green is required before any push.** It's also wired as
  a git `pre-push` hook. Run it with **`< /dev/null`**: as a hook it reads the ref
  lines from stdin, so with stdin left open (e.g. straight after a heredoc in the
  same command) it blocks forever on `read` instead of running anything.
- **CI** (`.github/workflows/ci.yml`) runs the backend tests + frontend
  `tsc --noEmit` + eslint on every push/PR. A green check is the merge bar.
- **Frontend**: `cd dgx-portal-frontend && npx tsc --noEmit && npx eslint .` before
  committing UI changes. Recurring foot-guns: duplicate i18n keys (`TS1117`),
  invalid Astryx `Badge` variants, `wrap="wrap"` is a *string* not a boolean.

---

## i18n contract

UI strings are **French-as-msgid**; English lives in `lib/i18n.tsx`. A missing EN
key silently falls back to French. When you add a `t("…")` string, add its EN
translation in the same commit. Don't add a duplicate key (TS error).

---

## Repo map

See README → *Repository layout*. The pieces you touch most:
`dgx-portal/app.py` (Flask backend), `dgx-portal-frontend/app/(app)/` (Next.js UI),
`vllm-runner/runner.py` (model lifecycle), `systemd/` (host units),
`docker-compose.yml` (the stack).
