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

## Host services — accepted, do NOT flag as vulns

The box is LAN/netbird-only (no public IP; the internet path is Cloudflare →
Traefik on 80/443). A few host services are **deliberately** exposed on
`0.0.0.0` with no firewall DROP and are **used by the operator** — never
recommend adding a firewall drop or disabling them, that breaks the operator's
own access. The specifics are operator-private and live in the machine-local,
git-ignored note [`.machine/security-accepted-risks.md`](.machine/security-accepted-risks.md) —
**do not** copy that inventory into any public doc. Only flag an *unexpected*
new port or an internet-reachable one.

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
- **1 M context on Qwen3.8-Flash-Next needs YaRN, and llama.cpp can do it.** The
  model is 262,144 native; without scaling llama.cpp caps the slot back to that
  while still reserving memory for what you asked (measured: 107 GB resident for
  the same usable context — strictly worse). With
  `--rope-scaling yarn --rope-freq-scale 0.25` (= Qwen's documented factor 4.0;
  `--yarn-orig-ctx` defaults to the training context, so it can be omitted — and
  it is not on the runner's allow-list anyway) the cap lifts: `n_ctx_slot =
  1000192`, quality verified intact on short prompts. Cost: 106 GB resident,
  ~15 GB free — no room left for the OCR/image/video sidecars. At 262,144 it is
  81 GB and 40 GB free. Qwen warns static YaRN degrades SHORT contexts, so enable
  it only when long context is actually needed.
- **`LLAMA_BIN` comes from `.env`, not from the code default.** It points at
  `/root/atomic-llama/build/bin/llama-server`. Since 2026-09-04 that path is a
  **symlink to `/root/llama-cpp-upstream/build/bin/llama-server`** (0.3.0-dev), the
  only build that knows `qwen4exp`. The TurboQuant fork binary is kept beside it as
  `llama-server.fork-turboquant` — restore it if Ling-3.0 comes back, its GGUF needs
  the fork's `ssm_f`/`ssm_a` tensors. The symlink was the way to change the binary
  **without restarting the runner**: the path is resolved at each spawn, the env var
  only at import.
- **Engine versions are opt-in pseudo-flags**, never a default swap: `--vllm-025`,
  `--vllm-027`, `--vllm-028`, `--llama-next` pick a specific binary for THAT launch
  (`_BIN_FLAGS` in `runner.py`). Added 2026-08-29: vLLM **0.28.0**
  (`/root/venvs/vllm-028`, torch 2.13 cu130, sm_121 verified — adds BailingMoeV3, so
  Ling-3.0 is now servable by vLLM too) and llama.cpp **0.3.0-dev upstream**
  (`/root/llama-cpp-upstream`, built for GB10 — adds `qwen4exp`, i.e.
  Qwen3.8-Flash-Next). Neither is the default: the AtomicChat Ling-3.0 GGUF needs the
  TurboQuant fork's `ssm_f`/`ssm_a` tensors, which upstream does not read.
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
- **LiteLLM supprime le `timings` de llama.cpp.** llama.cpp joint bien un objet
  `timings` (`prompt_ms`, `predicted_per_second`…) a son dernier fragment SSE, mais
  il ne survit pas au passage par LiteLLM — verifie sur flux reel. Le TTFT affiché
  est donc **mesuré par le portail** (depart de la requete → premier token du delta),
  ce qui est de toute façon le delai reellement subi. Ne pas rebrancher `timings`.
- **Le debit llama.cpp se lit sur `n_decode_total`, et sur rien d'autre.** Mesure
  faite sur ce serveur : **pendant** une generation, la jauge
  `predicted_tokens_seconds` vaut 0 et `tokens_predicted_total` n'avance pas —
  llama.cpp ne les met a jour qu'a la **fin** de chaque requete. Les lire donnait
  0 tok/s pendant toute la generation puis un chiffre fige entre deux. Seul
  `n_decode_total` avance en continu ; `vllm_health` en fait un `Δdecode / Δtemps`.
  Il compte les pas de decodage de **tous les slots**, donc le chiffre est le debit
  **agrege de toutes les sessions** (mesure : 34 tok/s a 1 session, 20,9 tok/s au
  total a 3 — `n_busy_slots_per_decode` ≈ 1, llama.cpp alterne les slots au lieu de
  les batcher, la concurrence ne multiplie donc pas le debit, elle le partage).
- **llama.cpp n'a aucun compteur de requetes.** `n_decode_total` compte des tokens ;
  l'afficher en « requetes servies » annoncait 39 303 requetes pour 39 303 tokens.
  `'requests'` est donc `None` pour cet engine et l'interface montre « — ».
- **`/api/modelhealth`** existe pour le rafraichissement **1 s** du tableau de bord
  (debit + TTFT). `/api/home` reste a 5 s : il agrege les depenses et sonde les
  sidecars. Le cache de `vllm_health()` est a **1 s** — le relever coute ~1 ms.
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
- **Sessions are server-revocable** via a `user_sessions` registry in SQLite:
  the signed cookie carries only a random `sid`; a row lets an admin kill a
  session at will (`POST /admin/users/<username>/revoke-sessions`), revoke on
  logout, and drop a locked account's sessions instantly (`enabled=0`).
  Sessions predating the registry expire by age only (no mass logout).
- **2FA par passkey (WebAuthn/FIDO2, non-TOTP)** : optionnelle par utilisateur
  (Réglages → Mon compte → Sécurité), clés dans `webauthn_credentials` +
  flag `user_security.enabled`. Portée par choix produit : local + LDAP
  seulement (pas le SSO). La passkey est liée à l'**origine** (`WEBAUTHN_ORIGIN`
  = `https://dgx.cronos.website`) — une clé déclarée là ne fonctionne pas
  depuis `http://dgx.cronos.lan`. `WEBAUTHN_REQUIRE_UV` (défaut off) accepte
  les clés "touch-only" (YubiKey classique) ; l'activer exigerait PIN/biométrie.
  Toute suppression/désactivation exige une re-vérification par mot de passe.
  Défis one-time stockés dans `pending_webauthn`, TTL 5 min.
- **The container is hardened** (cap_drop ALL, no-new-privileges). Even
  `docker exec -u 0` can't override file perms (no CAP_DAC_OVERRIDE) — manual
  edits to the data volume must be done **as the portal user (uid 10001)**,
  not root.
- **`docker exec` heredoc gotcha**: `docker exec ... python3 - << 'PY'` produces
  no output in this harness. Write the script to a file, `docker cp` it in, run it.

---

## Notifications email (SMTP Zoho)

Le portail envoie des emails depuis `no-reply@cronos.website` via **Zoho Mail**
(`smtp.zoho.com:587`, STARTTLS). Le nom d'app affiché chez le destinataire
(Gmail) est **« DGX platform »** (`notify._sender()` construit le « From »).
Les notifications admin sont en **anglais** et rendues en **HTML** (gabarit
commun `_render_html`, bandeau brandé + tableau + pied de page) ; `send_user_email`
écrit à un utilisateur et garde le contenu fourni par l'appelant (FR, portail
FR-first).

`.env` (fichier protégé, à saisir à la main — jamais de secret en clair dans un
fichier suivi) :
```
SMTP_HOST=smtp.zoho.com
SMTP_PORT=587
SMTP_USER=no-reply@cronos.website
SMTP_PASSWORD=<mot de passe d'application Zoho>
SMTP_FROM=DGX platform <no-reply@cronos.website>
ADMIN_EMAIL=<adresse qui reçoit les demandes, ex. ton Gmail>
# Facultatif : URL du dashboard admin pour le bouton « Open the Admin dashboard »
# des emails. S'il est vide, aucun bouton n'est rendu dans le gabarit HTML.
ADMIN_URL=https://dgx.cronos.website/admin
# Fenêtre d'anti-spam pour les demandes « lancer une catégorie média » (secondes).
MEDIA_REQUEST_COOLDOWN_S=1800
```

Sans config SMTP, `notify_*` est un no-op (retourne `False`), rien ne part.

Canaux (tous vers `ADMIN_EMAIL`, sauf `send_user_email` qui écrit à un
utilisateur) :
- **Demande de modèle / budgets** : `notify_email` / `notify_budget_email`,
  appelés par les outils de support (`request_model`, `request_budget`).
- **Bouton maintenance** (Admin) : `notify_maintenance_email`, envoyé à
  `ADMIN_EMAIL` à chaque bascule (destinataires = admins).
- **Bouton « demander un modèle »** sur les pages image/musique/vidéo/OCR/voix :
  `POST /api/model/request` ({category}) → `notify_media_request_email`. La route
  refuse (409) si un modèle de la catégorie tourne déjà (`_media_category_running`,
  mêmes capteurs que `/api/home`), et applique un **cooldown anti-spam**
  (`media_request_cooldown`, 1 demande / user / catégorie par
  `MEDIA_REQUEST_COOLDOWN_S`, défaut 1800 s → 429 sinon). Le bouton est affiché
  aussi dans l'`EmptyState` (aucun modèle + aucun historique).
- **Config email** (Admin) : `GET /admin/email/config` (statut SMTP, sans
  jamais renvoyer le mot de passe) + `POST /admin/email/test`
  (`send_test_email`) — bouton « Envoyer un test » dans l'Admin.
- **Alertes infra** : `notify_infra_alert_email` sur **échec de lancement** d'un
  modèle (chat, OCR, image, musique, voix). Le **moniteur continu** (cœur
  toujours-up) est côté hôte : voir « Monitoring & sauvegardes ».

Le gabarit HTML (`_render_html`) rend un **bouton « Open the Admin dashboard »**
si `ADMIN_URL` est défini ; vide → aucun bouton.

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

## Monitoring & sauvegardes (hôte, systemd)

Deux timers systemd + deux scripts (dans `monitoring/`), qui tournent **en root**
sur l'hôte (accès au socket docker + au `.env` 600).

- **`cronos-monitor.timer`/`.service`** (toutes les 5 min) → `monitoring/monitor.py` :
  sonde le **cœur** toujours-up (vllm-runner `:8001`, conteneurs traefik /
  litellm / litellm-postgres / dgx-portal / dgx-portal-frontend) et envoie un
  email d'alerte à `ADMIN_EMAIL` quand un service tombe, puis un email de
  rétablissement. État **sticky** dans `/var/lib/cronos-monitor/state.json`
  (1 alerte par incident). Les sidecars média (vLLM `:8000`, OCR/voix/musique/
  image/ComfyUI) sont **on-demand** : non sondés pour éviter les faux positifs.
  Le moniteur sonde aussi la **fraîcheur du dump portal** (`/var/backups/cronos/portal-*.db`
  < 26 h) : un backup manquant/ancien compte comme un service down (alerte +
  rétablissement via le même mécanisme sticky).
- **`cronos-backup.timer`/`.service`** (quotidien 03:00) → `monitoring/backup.py` :
  dump SQLite `/app/data/portal.db` (snapshot cohérent via `sqlite3.backup`) et
  `pg_dump -Fc` de la base LiteLLM (sans mot de passe : auth de confiance dans
  le conteneur). Destination `/var/backups/cronos/`, rétention 14 fichiers
  (`--keep`). **Ces dumps contiennent les données de la DB — jamais poussés.**
  Restauration : `docker cp` du `.db`/`.dump` puis ouverture/`pg_restore`.

Endpoints de santé (côté portail) :
- `GET /healthz` — liveness **publique** minimal `{ok, time}` (healthcheck /
  sonde). `GET /api/health` — état agrégé **connecté** (`runner`, `litellm`,
  `chat`, `video/ocr/voice/image/music` en `ready`/`on_demand`).

Notifications email : la route `POST /api/model/request` renvoie `email_sent`
et le bouton Admin/test email reflète un échec d'envoi (`send_test_email` /
`notify_*` retourne `False`, la bascule de maintenance ajoute un flash
« email non envoyé » si le SMTP est configuré mais que l'envoi échoue).

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
