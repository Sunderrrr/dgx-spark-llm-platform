# Cronos — Self-Hosted LLM Platform

A self-hosted LLM inference platform running on a single **NVIDIA DGX Spark**
(GB10 Grace Blackwell, 128 GB unified memory, aarch64). It turns one GPU box into
a small multi-user AI service with an OpenAI-compatible API, per-user keys and
budgets, a self-service web portal, and an AI support assistant that can act on
your behalf.

It provides:

- an **OpenAI-compatible API** (LiteLLM) protected by per-user keys with token budgets,
  including a virtual **`auto-model`** that always routes to whatever chat model is
  currently loaded, so clients never need editing when the admin swaps models;
- a **self-service portal** where each user (LDAP or SSO) creates keys, tries models
  in an in-browser **playground**, requests models, and tracks consumption;
- **admin user management** — a dedicated page to create local accounts, group them,
  and set per-group / per-user quotas and rights, with each account's auth source
  (local / LDAP / SSO) shown at a glance;
- **Cronos**, an AI support assistant that answers questions *and* performs
  self-service actions (create a key, request budget, request a model…);
- a **runner** that launches/stops one chat model on the GPU on demand —
  vLLM, llama.cpp or ds4 engine, chosen per model — and auto-resumes it after
  a crash or reboot;
- always-on media sidecars — **OCR**, **video**, **image**, **music**, **voice
  cloning** and **dictation** — served alongside the main chat model and streamed
  into the portal, never exposed as separate public UIs;
- an admin **maintenance mode** that blocks non-admin API/portal traffic without
  stopping any model, enforced both in the portal and at the edge (Traefik);
- a UI available in **English and French**, switchable per account from Settings
  → Appearance (English by default).

![Cronos portal — home dashboard](assets/dashboard.png)

---

## Contents

- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Configuration (`.env`)](#configuration-env)
- [Authentication](#authentication)
- [Token budget model](#token-budget-model)
- [Using the API](#using-the-api)
- [Portal features](#portal-features)
- [Screenshots](#screenshots)
- [Operations](#operations)
- [Security](#security)
- [Repository layout](#repository-layout)
- [License](#license)

Two companion documents:

| Document | For |
|---|---|
| [`SECURITY.md`](SECURITY.md) | threat model, controls, accepted risks — operators and auditors |

> An operating guide named `CLAUDE.md` (golden rules, GB10 gotchas, reboot runbook,
> test gate) is kept **on the machine**, read there by the operator and by the agents
> working in this checkout. It is deliberately **not published**, so the bare
> `` `CLAUDE.md` `` mentions you will see in comments and elsewhere in this README
> point at a file that exists on the box but not in this repository.

---

## Architecture

```mermaid
flowchart LR
  U[Users] -->|https://dgx.cronos.website| CF[Cloudflare + Traefik]
  U -->|https://api.cronos.website/v1| CF
  CF -->|forwardAuth: maintenance gate| AC[/internal/authcheck/]
  AC -->|checked against| P
  CF -->|:5000| F[dgx-portal-frontend]
  CF -->|api.cronos.website · no published port| L[LiteLLM]
  F -->|internal docker network| P[dgx-portal]
  P -->|LDAP / OIDC auth| IDP[Authentik · LDAP outpost + OIDC]
  P -->|issues keys + budgets| L
  P -->|:8001 · Bearer token| R[vllm-runner]
  R -->|launch / stop| V[vLLM · :8000]
  R -->|sudo, scoped: docker start/stop/recreate| S[Media sidecars]
  R -->|sudo, scoped: systemctl start/stop| CU[ComfyUI · MiniMax H3]
  L -->|:8000| V
  L --> PG[(Postgres)]
  P -->|streamed, per-sidecar network| S
  P -->|/prompt, /history, /view| CU
```

> **The backend is modular.** `dgx-portal` is not one big Flask file: it is a
> shared core (config, database, auth, guards), a set of clients for everything
> it talks to (LiteLLM, vLLM, ComfyUI, the sidecars, MCP, web search), and one
> route blueprint per feature. [`app.py`](dgx-portal/app.py) is a wiring facade —
> it builds the Flask app, registers the blueprints and boots the schema. See
> [Repository layout](#repository-layout).

### Components

| Component | Role | Port | Runs as |
|---|---|---|---|
| **litellm** | OpenAI-compatible gateway: per-user keys, budgets, token accounting | `4001` | Docker container |
| **litellm-postgres** | LiteLLM database (keys, spend logs) | `5432` (internal) | Docker container |
| **dgx-portal-frontend** | The UI (Next.js + Astryx): login, home, playground, media pages, support, find-a-model, leaderboard, admin, users (keys and memory live in the Settings dialog) | `5000` | Docker container (non-root) |
| **dgx-portal** | Backend (Flask): LDAP/OIDC auth, sessions, JSON API, business logic | internal only | Docker container (non-root) |
| **vllm-runner** | Daemon driving **one** chat model process (start/stop/logs; engines `vllm` / `llamacpp` / `ds4`) with auto-resume, plus scoped start/stop/recreate of every media sidecar | `8001` | systemd service on the host |
| **vLLM** | OpenAI-compatible inference server (the main chat engine) | `8000` | process spawned by the runner |
| **OCR container** | vLLM serving an OCR-capable VLM (baidu/Unlimited-OCR by default, chandra-ocr-2 also supported), swappable via an admin catalog | internal only | Docker container, own network + GPU slice |
| **ComfyUI** | Video generation graph engine (MiniMax H3 in **NVFP4** — the Blackwell-native 4-bit format the GB10 supports; measured on disk: 11.7 GiB ≈ 12.5 GB per UNET, two UNET variants) | `8188`, host-restricted | systemd service on the host |
| **Image container** | Text-to-image (diffusers). FLUX.2 Klein 4B by default, 35 diffusion steps per image (`IMAGE_STEPS`) | internal only | Docker container, own network + GPU slice |
| **Music container** | Text-to-music (diffusers, MiniMax-Music3 & co) | internal only | Docker container, own network + GPU slice |
| **ASR container** | Whisper (`large-v3-turbo` by default) for Playground dictation | internal only | Docker container, own network + GPU slice |
| **Voice container** | Zero-shot voice cloning. Two interchangeable engines: **Qwen3-TTS** (default, Apache 2.0, 10 languages, 3 s cloning) or **Chatterbox** (MIT) | internal only | Docker container, own network + GPU slice |
| **SearXNG + crawl4ai** | Web search for the playground: SearXNG finds links, crawl4ai reads the pages | internal only | Docker containers, shared `web_net` |

> Only one **chat** model runs on the GPU at a time (launching another replaces the
> current one) — the media sidecars are separate, always-addressable backends running
> alongside it, each with its own GPU memory budget. On a single 128 GB unified-memory
> box that budget is genuinely shared: if a launch fails with an out-of-memory error,
> stop a sidecar from **Admin** rather than shrinking the chat model.

The UI used to be server-rendered Jinja templates served directly by Flask on
`:5000`. It's now a separate Next.js/Astryx frontend that owns `:5000` and
talks to Flask over the internal docker network for everything — auth, data,
and even the streaming chat endpoints (proxied through dedicated Next.js
Route Handlers so token-by-token streaming isn't buffered). Flask itself no
longer has a published port.

---

## Quick start

Prerequisites: a DGX Spark (or any CUDA host), a reachable LDAP directory
(Authentik's LDAP outpost here) plus an OIDC provider, and outbound internet for
pulling images and model weights.

```bash
# One-shot bootstrap: installs Docker, Python/pipx, vLLM, clones the repo,
# generates .env, installs the systemd units and brings the stack up.
curl -fsSL https://raw.githubusercontent.com/Sunderrrr/dgx-spark-llm-platform/master/install.sh | sudo bash
```

Or manually:

```bash
git clone https://github.com/Sunderrrr/dgx-spark-llm-platform.git
cd dgx-spark-llm-platform
sudo ./install.sh          # installs packages + systemd units, generates .env
#   → then fill the remaining secrets in .env (LDAP/OIDC/SMTP/Discord)
docker compose up -d       # frontend + backend + gateway + database
```

Then open the portal (`http://<host>:5000`, or your HTTPS domain behind Traefik),
go to **Admin**, and launch a model from the catalog.

---

## Configuration (`.env`)

`docker-compose.yml` injects these into `dgx-portal` / `litellm`. `install.sh`
(via `setup.sh`) generates the random secrets; fill in the rest. See `.env.example`.
The backend reads them all in one place, [`config.py`](dgx-portal/config.py).

| Variable | Purpose |
|---|---|
| `WEBUI_SECRET_KEY` | Flask session signing key |
| `LITELLM_MASTER_KEY` | LiteLLM master key (gateway admin) |
| `POSTGRES_PASSWORD` | LiteLLM database password |
| `LDAP_BIND_PW` | Password of the LDAP bind account (`LDAP_BIND_DN`) used to look users and groups up |
| `RUNNER_TOKEN` | Bearer token between `dgx-portal` and `vllm-runner` (also used for the sidecar control routes) |
| `OCR_URL` | Internal URL of the OCR vLLM container (default `http://ocr:8000/v1`) |
| `VOICE_URL` | Internal URL of the voice container (default `http://voice:8004`) |
| `ASR_URL` | Internal URL of the Whisper transcription container (default `http://asr:8006`) |
| `IMAGE_URL` | Internal URL of the text-to-image container (default `http://image:8007`) |
| `MUSIC_URL` | Internal URL of the text-to-music container (default `http://music:8008`) |
| `COMFYUI_URL` | Internal URL of the ComfyUI video backend (default `http://host.docker.internal:8188`) |
| `PUBLIC_API_URL` | Public API URL shown to users (default `https://api.cronos.website/v1`) |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | Authentik `dgx-spark` OIDC app |
| `OIDC_METADATA_URL` / `OIDC_REDIRECT_URI` / `OIDC_LOGOUT_URL` | OIDC endpoints |
| `OIDC_ADMIN_GROUP` | Group granting the admin role (default `adm_cronos`) |
| `SESSION_COOKIE_SECURE` | `1` behind an HTTPS proxy (Traefik), `0` for plain-HTTP LAN |
| `KEY_MAX_BUDGET` / `KEY_BUDGET_DURATION` | Default per-account budget (default 200 M tokens / week) |
| `DISCORD_WEBHOOK_URL`, `SMTP_*`, `ADMIN_EMAIL` | Request notifications |

> `.env` is **gitignored** — no secret is committed. `.env.example` holds only placeholders.
>
> `docker-compose.yml` reads `BACKEND_URL` to reach Flask internally; the default
> (`http://dgx-portal:5000`) matches the compose service name and rarely needs changing.
> Note that `host.docker.internal` is **pinned to a fixed address** there rather than
> using `host-gateway` — see the networking gotcha in `CLAUDE.md` (local guide,
> kept on the machine and not published).

---

## Authentication

Two methods, handled by [`auth.py`](dgx-portal/auth.py):

- **OIDC SSO (Authentik)** — primary. "Sign in with Cronos SSO". Flow:
  `/login/sso` → Authentik → `/api/oauth2-redirect`. Admin comes from the `groups`
  claim (`adm_cronos`), falling back to an LDAP lookup by username if absent.
- **LDAP (Authentik)** — username/password fallback: direct bind, injection-escaped,
  empty-password binds rejected, with brute-force lockout (6 fails / 15 min)
  persisted in SQLite so it survives a redeploy and is shared across workers.
  The counter is keyed **per IP and per username**: rotating source IPs can't
  dodge the threshold, since all attempts against one account feed the same
  counter regardless of origin.
- **Passkey 2FA (WebAuthn/FIDO2)** — optional per account (Settings ▸ Security),
  local and LDAP accounts only: registration and login challenges are single-use,
  and removing a passkey or switching 2FA off requires a password
  re-verification.

Local accounts managed by an admin ([`local_users.py`](dgx-portal/local_users.py))
[local_users.py](dgx-portal/local_users.py) sit between the two: a hashed,
admin-managed account system checked before LDAP/SSO.

**Account state is re-read on every request, never frozen at login.** A session
carries a name and a role copied at sign-in; each guarded request re-checks the
account before serving, so deleting, disabling, blocking or demoting an account
takes effect immediately instead of at cookie expiry (`SESSION_MAX_AGE`, 12 h by
default). For a **local** account the portal is the authority on the role, since
it owns `local_users`; for a directory account the role follows the last login
recorded — LDAP cannot be queried per request, and the portal never *infers* a
demotion from a record that is merely absent.

**Blocking** (`Users → Bloquer`, `blocked_users` table) refuses an account at
login whatever its authentication source, and revokes its open sessions. It is
the only lever that works for an **LDAP/SSO** account, which has no local row to
disable — before it, the only option was revoking sessions, and the account
simply logged in again. It is reversible and leaves the local account untouched.

**Deleting an account is a deprovisioning, not a row removal.** It revokes the
sessions, revokes every API key on LiteLLM, deletes the LiteLLM user envelope
(budget *and* accumulated spend — otherwise an account re-created under the same
name inherits the previous one's spend) and purges the personal data: memory,
conversations, share links, preferences, passkeys, media jobs. It requires an
explicit `confirm=DELETE`, refuses self-deletion and refuses to remove the last
local administrator. If LiteLLM is unreachable the deletion still goes through —
better an account gone from the portal than a stuck one — but the response **and**
the audit trail name the keys that could not be revoked. `audit_log` is kept on
purpose: it is the record of admin actions and outlives the account.

Sessions are **server-revocable**: the signed cookie carries only a random
`sid`, and a `user_sessions` row (same SQLite) records its creation time, IP and
user-agent. An admin can kill any active session (`POST
/admin/users/<username>/revoke-sessions`), and every account sees **its own**
sessions under **Settings → Security** and can close one or all the others
(`GET /api/account/sessions`, `POST /api/account/sessions/revoke`). Sessions
predating the registry expire by age only, so the migration logs nobody out.

A local account can also **change its own password** (Settings → Security,
`POST /api/account/password`): it requires the current password, enforces the
policy (`local_users.password_policy_error` — 8 characters minimum, common
passwords and the login name refused), and closes every *other* session while
keeping the current one. Directory accounts are told their password lives in the
directory rather than shown a form that cannot work.

Session hardening: `HttpOnly` + `SameSite=Lax` cookies + `Secure` behind TLS.
`ProxyFix` trusts Traefik's `X-Forwarded-*` headers.

---

## Token budget model

Budgets are enforced **per account** (a LiteLLM *user*), shared across all of that
user's keys — creating extra keys does not raise the cap. Pricing is set at model
registration time by [`litellm_client.py`](dgx-portal/litellm_client.py), not in
`litellm/config.yaml`: prompt and generated tokens both count 1 budget unit each.

Default: **200,000,000 tokens/week** per account (`KEY_MAX_BUDGET` /
`KEY_BUDGET_DURATION`, editable in **Admin → token limit**, no restart). Admins
are uncapped. Over budget → HTTP `429 budget_exceeded`; the portal shows a banner
once an account passes 85%. Approving a token request grants a preset or custom
amount as a time-limited boost that stacks and reverts to the exact base cap at
expiry.

---

## Using the API

OpenAI-compatible endpoint: **`https://api.cronos.website/v1`**. Every call needs a
key issued from the portal (`Authorization: Bearer sk-…`).

```bash
curl https://api.cronos.website/v1/chat/completions \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto-model","messages":[{"role":"user","content":"Hello!"}]}'
```

Claude-native clients can use the Anthropic-compatible
**`POST https://api.cronos.website/v1/messages`** instead — same keys, same
budgets.

### The `auto-model` alias

Because the admin swaps the running chat model from time to time, hard-coding a
model name means editing every client on each swap. Instead, point clients at the
virtual model **`auto-model`**: it is a LiteLLM alias that always routes to the
chat model currently loaded, re-pointed automatically on every launch. Wire it
once and never touch your config again — the real model names stay registered and
keep working in parallel if you'd rather pin to a specific one.

The **Settings → API keys** panel generates ready-to-paste snippets for Claude
Code, OpenCode, Hermes Agent, Codex CLI, Aider, Continue.dev, Cursor, LangChain,
the Python SDK, cURL and env vars — key and endpoint pre-filled, with
**`auto-model` selected by default** while every named model stays selectable.

> For **OpenCode**, the config uses a dedicated `dgx-cronos` provider (not `openai`)
> so it won't clash with an official OpenAI account.

---

## Portal features

| Feature | Where | In one line |
|---|---|---|
| [API keys](#api-keys) | Settings ▸ API keys | create/revoke keys, see spend, copy integration snippets |
| [Account security](#authentication--accounts) | Settings ▸ Security | passkeys, active sessions (IP, device), change your own password |
| [Playground](#playground) | `/playground` | streaming chat with the active model, attachments, dictation, web search |
| [Memory](#memory) | Settings ▸ Memory | knowledge graph of what the assistant knows about you — on by default, opt out any time |
| [Media pages](#media-pages) | `/ocr` `/video` `/image` `/music` `/voice` | OCR, video, image, music and voice cloning |
| [Support](#support-cronos) | `/support` | an assistant that can act on your account |
| [Find a model](#find-a-model) | `/search` | live Hugging Face search, GB10-tested first |
| [Request a model](#request-a-model) | `/request` | ask an admin for a model or more tokens |
| [Home](#home) | `/` | running backends, live server state, your usage |
| [Leaderboard](#leaderboard) | `/ranking` | weighted spend ranking |
| [Admin](#admin) | `/admin` | models, sidecars, catalog, quotas, maintenance |
| [Users](#users) | `/users` | accounts, groups, quotas, auth sources, blocking, detail view |

### API keys

Create/revoke keys, see per-key spend and the shared account budget, request more
tokens; integration snippets per tool with `auto-model` pre-selected. Reached from
the sidebar gear or the home page's "My API keys" button — there is no standalone
`/keys` page.

### Playground

In-browser streaming chat with the active model; no client setup. Streamed
Markdown, a collapsible reasoning trace for thinking models, attachments, a live
context meter, per-message copy/regenerate, and a resizable **document panel**:
long answers get an "Open as document" button that pops the content into a wide,
side-by-side reading pane.

Includes **dictation** — a mic button transcribes what you say into the composer.
Deliberately self-hosted (Whisper on the GPU) rather than the browser's
`SpeechRecognition` API, which in Chrome ships the audio to Google's servers.

Two effects hang off the composer: its border lights up while the model is
working, and a colour glow rises from the field while dictation is listening
(both from [libraries.dev](https://libraries.dev), MIT). They are decorative —
`pointer-events: none`, and motion is dropped under `prefers-reduced-motion`.

**Web search** is available on explicit request ("search the web for…"): SearXNG
finds the links, crawl4ai reads the pages, and the progress of each step is shown
live. See [`websearch_tools.py`](dgx-portal/websearch_tools.py) and the rules in
`CLAUDE.md` (local guide, kept on the machine and not published).

Conversations can be **pinned** (per browser) and **shared** — a read-only
snapshot served at a `/c/<token>` link, visible to logged-in users. On request
("draw…"), the model can also generate an image through the image sidecar, the
same tool mechanism as web search.

### Memory

A knowledge graph of what the assistant has learned about you, built as you
chat. Facts are
stored as triples (subject, relation, object) in SQLite rather than as a flat
list, so "what do you know about X?" resolves to a node's neighbourhood instead of
injecting everything; traversal is a recursive CTE, so no graph database is
involved. Writing the same subject+relation again supersedes the older fact.

**On by default** — it is personal data, so the page shows every stored fact, a
switch in Settings ▸ Memory turns recording off, and nobody else can read your
graph, admins included.

### Media pages

- **OCR** — extract text from an image or scan, streamed token-by-token, with a
  toggle to visualize every detected region as bounding boxes over the source
  image; keeps your last 20 results.
- **Video** — turn a text description, with or without a reference image, into a
  short video with synced audio (MiniMax H3); keeps your last 10 results.
- **Image** — text-to-image; the backend runs 35 diffusion steps per image by
  default (`IMAGE_STEPS`).
- **Music** — text-to-music, same shape as the image page.
- **Voice** — record a sample straight from your microphone (1 min max, with
  playback before you commit) or upload one, give it any text, and get that text
  read back in that voice — zero-shot, a few seconds per generation. The page
  adapts to whichever engine is loaded. Browser recordings are converted to 24 kHz
  mono WAV client-side because `MediaRecorder` only emits WebM/Opus, which neither
  engine accepts.

All media pages show a clear empty state when their backend is not running,
instead of letting you submit into a dead end.

### Support (Cronos)

An AI assistant that sees your keys (masked), budget, the model catalog and server
status, and can **act for you**: create a key, revoke one, request budget, request
a model (admins also get launch/stop). Actions are always scoped server-side to the
logged-in user; impactful ones require in-chat confirmation. It can also call MCP
servers and skills you configure in Settings — with the guardrail that once
third-party tool output has entered the conversation, privileged tools are refused
for the rest of the turn.

It runs on **your** API key, hence on your token budget (the same rule as the
playground): if the account has no key yet, the assistant answers by asking you to
create one on *My API keys* rather than silently doing nothing, and a spent budget
stops it until the quota resets.

Sensitive actions — revoking a key, launching or stopping the model on the GPU —
are **never executed on the model's word**: it files a request, the chat shows a
**Confirmer / Annuler** button, and only your click runs it (single-use token,
expires after 10 minutes, bound to your account). The assistant also keeps your
last conversation server-side so a page reload does not lose it, remembers durable
facts about you when the memory feature is on, sees your recent platform actions
when diagnosing a problem, and offers 👍/👎 feedback on its answers (collected
at `GET /admin/support/feedback`, admin-only).

### Find a model

Live search over the **whole** Hugging Face Hub (no local cache): every query is a
fresh call to `huggingface.co/api/models`, so the catalogue can never be stale.
With no query typed the page lists the most-downloaded models, which is the honest
way to "browse the catalogue".

**No filter is applied unless you ask for one.** Both filters used to be on by
default and quietly hid models that exist: a task filter (`text-generation`) and
the `gb10` tag, which only a handful of models carry. A model tagged solely
`image-text-to-text` — `ornith-ai/Ornith-1.5-9B`, reported in real use — was
therefore unfindable, and the page looked broken. The GB10 tag is now an opt-in
switch, and a `GB10` badge marks the models that carry it. Pagination uses Hugging
Face's own `Link: rel="next"` header, so "Load more" only appears when there
really is a next page.

Each result says which engine would serve it (GGUF → llama.cpp, safetensors →
vLLM) and flags a **gated** repository: those need a Hugging Face token and fail
*after* you press Launch, so it is worth knowing before. A token configured in
`./secrets/hf_token` (read-only, 0600, owned by the portal user) is sent as a
bearer header: gated and private repositories then show up, and the anonymous rate
limit (500 requests / 5 min per IP) stops being a concern for a search box that
queries on every keystroke. The token's value never leaves the server — only
whether one is configured, which the page says when none is.

An empty result is explained rather than left bare: Hugging Face matches the
**repository name**, not the description, so a typo (`orith1.5` for `Ornith-1.5`)
returns nothing — the page says so and offers a link to Hugging Face itself. When
the GB10 filter returns nothing it also checks without the filter, because a model
that exists but isn't tagged is not a model that doesn't exist. And if Hugging
Face itself doesn't answer, that is reported as such (502) rather than shown as an
empty result list.

### Request a model

A short form to ask an admin for a model that isn't in the catalog, or for more
tokens. Requests land in Admin with the requester and their reason. A duplicate
request is refused with a message that says so — the form used to announce
"Request sent!" whether or not anything had been recorded.

### Home

Every backend the API can serve, each card labelled with what it does and the chat
card advertising the `auto-model` tip; a **Server status** panel with live CPU/RAM/GPU
and the active model's health (throughput, sessions, TTFT, requests served — `—` on
llama.cpp, which keeps no request counter — plus in/out context); a **Media services**
block that only appears while an OCR, video or voice sidecar is running; and your own
hourly token usage over the last 24 h. A backend that is not running is labelled
"Available from the app, not exposed via the API."

### Leaderboard

Ranks users by weighted spend (day/week/month), colorblind-safe palette, from
LiteLLM's Postgres spend logs. Each row carries the account's avatar next to its
name (the generated one unless a brand logo was chosen); unattributed API keys
get none, because they are not accounts.

### Admin

Launch/stop models, live vLLM logs, add/edit/remove catalog models (chat, OCR and
voice), start/stop each sidecar independently of the chat model, set the default
budget, approve token/model requests, and a **maintenance mode** toggle that blocks
non-admin traffic everywhere without stopping any backend. Per-user consumption is
a **search box** — look up a single account to see its LiteLLM quota/spend plus its
media usage (untracked by LiteLLM, since none of it goes through a public API key).

A **platform status** card sits at the top of the page: free disk space, the age
of the last portal backup, the incidents the host monitor currently has open, the
served model with the uptime the portal *observed*, and the portal's own counters.
It answers the questions you ask during an incident without a shell on the host.
The backup figures come from the host monitor's state file (mounted read-only)
because `/var/backups/cronos` is root-only — deliberately, those dumps contain the
whole database. When a figure cannot be read the card says so instead of showing a
reassuring green dot.

Actions report **what actually happened**. A refused stop, a quota LiteLLM
rejected or a model that failed to register is an error message with a matching
HTTP status, never a green toast; a launch whose runner did not answer in time is
reported as *uncertain* ("check the state before relaunching") rather than as a
failure, because retrying would kill a model that is still loading. Destructive
actions — stopping the served model, launching another one, deleting a catalog
entry, toggling maintenance — ask for confirmation first, and the buttons are
locked while a request is in flight so a double-click cannot fire twice.

### Users

A dedicated page to manage accounts: create users with a hashed password, assign
them to **groups** carrying a default quota and admin right, override a per-user
quota, enable/disable, toggle admin, or reset a password. It lists every known
account with a badge for each authentication source — **Local**, **LDAP**, **SSO**
or **External** — recorded per login and cumulative, plus the current state:
blocked, or locked out after too many failures.

Each account opens a **detail view** (`GET /admin/users/<username>/detail`):
effective role and budget, the LiteLLM spend, the API keys (alias, creation date,
spend — never the key itself), how many memories and conversations it holds, its
active sessions with IP and user-agent, and the admin actions it performed. From
there an admin can **block/unblock** an account, delete it, or — for an account
that only exists in the directory — **purge its data**. Deleting and purging both
ask for the word `DELETE` to be typed after showing what will be lost.

The two are not interchangeable, and the interface says so: **deleting** a local
account removes its access *and* erases its data, while **purging** an LDAP/SSO
account erases its data only — that account can sign in again and will simply
start from an empty account. Removing access is what **blocking** is for.

The portal refuses to leave itself without a local administrator: an admin cannot
disable or demote **themselves** (the role is re-read on every request, so the loss
of access would be immediate and there would be no session left to undo it), and the
**last** active local admin cannot be removed — not by deleting the account, not by
disabling it, and not by deleting the group that carries its rights. Admin rights
come from `local_users.is_admin` *or* from the group, which is why removing a group
is checked too.

---

## Screenshots

| Playground — streaming chat, dictation and web search | Support — the Cronos assistant, budget- and status-aware |
|---|---|
| ![Playground](assets/playground.png) | ![Support](assets/support.png) |

| OCR — text extracted live from a scanned document | Video generation — text- or image-driven, MiniMax H3 |
|---|---|
| ![OCR](assets/ocr.png) | ![Video](assets/video.png) |

| Image generation — FLUX.2 Klein | Memory — the knowledge graph the assistant keeps about you |
|---|---|
| ![Image](assets/image.png) | ![Memory](assets/memory.png) |

| Find a model — Hugging Face catalog search | Request a model |
|---|---|
| ![Find a model](assets/search.png) | ![Request a model](assets/request.png) |

![Admin — the platform status card, the unified backend row, and live model logs](assets/admin.png)

> **Refreshing these screenshots** — nothing here is hand-made: the whole set is
> captured by `scripts/screenshots.py`, in **English** and in the **dark theme**,
> with the dedicated **`demo`** account (a plain, non-admin account created by
> `scripts/create-demo-account.py` — a screenshot must never expose a real
> person's dashboard). One command per step:
>
> ```bash
> python scripts/screenshots.py            # capture (1600×1000 at 2× → 3200×2000)
> python scripts/screenshots.py --verify   # size, dark theme, expected content, privacy
> python scripts/screenshots.py --install  # copy the fresh files into assets/
> ```
>
> Run them from the repository root. Beyond Python 3 they need `playwright`
> (plus `playwright install chromium`), `pillow` and `tesseract`, which `--verify`
> uses to read the text of each PNG.
>
> The verification is not decorative: it OCRs each final PNG and **fails** if an
> email address, an API key, a private IP or another account's name is visible.
> `/admin` carries the notification address near the top and the per-user budget
> requests further down, so that page is captured at a fixed scroll position —
> and `/admin`, like `/users`, needs `is_admin` granted to the demo account for
> the few minutes of the shoot (the script refuses and deletes the file rather
> than publishing an "Administrators only" page). The five media pages are the
> one exception: they are only worth shooting **while one of those models is
> loaded**, since an idle page just says "no model is available" — the script
> skips them by default and keeps the published image. Two ways round that:
> name the page (`python scripts/screenshots.py ocr`), or use `--media`, which
> shoots every media page whose backend is currently up — it asks `/api/health`
> first, so it can never capture an idle page. Start a sidecar from Admin, run
> `--media`, then `--verify --install`.
>
> **Two pages are absent from the gallery above: Voice cloning and Music
> generation.** Their published images showed nothing but the empty state
> ("Ask an admin to start a voice model to use this page"), which 2026-08-29
> shipped and a caption quietly promised otherwise. An image of an error screen
> is worse than no image, so both files are withdrawn until a capture can be
> taken with their backend actually loaded — the refresh pass above reinstates
> them in one command. `--verify` now refuses that empty state by name, which is
> how the two were found; it also keeps the three media pages that *do* show real
> content (OCR, video, image) honest.
>
> | Page | Route | File |
> |---|---|---|
> | Home dashboard | `/` | `assets/dashboard.png` |
> | Playground | `/playground` | `assets/playground.png` |
> | Support assistant | `/support` | `assets/support.png` |
> | OCR | `/ocr` | `assets/ocr.png` |
> | Voice cloning | `/voice` | `assets/voice.png` — *withdrawn, see above* |
> | Video generation | `/video` | `assets/video.png` |
> | Image generation | `/image` | `assets/image.png` |
> | Music generation | `/music` | `assets/music.png` — *withdrawn, see above* |
> | Memory | Settings ▸ Memory (or `/memory`) | `assets/memory.png` |
> | Find a model | `/search` | `assets/search.png` |
> | Request a model | `/request` | `assets/request.png` |
> | Admin | `/admin` | `assets/admin.png` |
>
> The **Users** page (`/users`) and the **Leaderboard** (`/ranking`) are deliberately
> *not* published here — they list internal usernames (and, for the leaderboard,
> per-user consumption), so `assets/users.png` and `assets/ranking.png` are
> gitignored (they are still refreshed locally, for the operator's own use).

---

## Operations

Day-to-day runbook — reboot recovery, the test gate, model-serving gotchas — lives
in `CLAUDE.md`, the operating guide kept on the machine (not published). The
essentials:

### Launch a model

Via the portal (**Admin → Launch**) or the runner API directly:

```bash
curl -H "Authorization: Bearer $RUNNER_TOKEN" -H "Content-Type: application/json" \
  -d '{"hf_model_id":"deepreinforce-ai/Ornith-1.0-35B-FP8","model_name":"ornith-35b-fp8",
       "vllm_args":"--enable-auto-tool-choice --tool-call-parser qwen3_coder --dtype bfloat16 --max-model-len 262144 --gpu-memory-utilization 0.7 --max-num-seqs 8"}' \
  http://127.0.0.1:8001/launch
```

`vllm_args` is validated against a strict allowlist — see [`SECURITY.md`](SECURITY.md#21-model-launching--a-strict-allowlist-not-a-denylist).

### Auto-resume

The runner persists the last successful launch (`/var/lib/vllm-runner/last_model.json`)
and **relaunches it** after a process crash, a service restart or a reboot. A manual
`/stop` clears that state (no resume). Capped at 3 consecutive attempts.

### Tests

```bash
./dgx-portal/run-tests.sh        # backend suite, in a throwaway image
./scripts/pre-push-check.sh      # tests + secret scan — green is required to push
```

### systemd services

| Unit | Role |
|---|---|
| `vllm-runner.service` | The runner daemon (non-root `vllmrunner` user) |
| `vllm-restrict.service` | iptables: host ports **8000**/**8001** limited to localhost + Docker bridge |
| `cronos-docker-restrict.service` | DOCKER-USER rules: **4001** to LAN+VPN, **5000** to Traefik only |
| `cronos-web-restrict.service` | Drops new connections from the web-search network to the host |
| `cronos-ocr-restrict.service` | Prevents the OCR container from opening a connection to the portal |
| `cronos-traefik-boot.service` | One-shot at boot: waits for DNS, then restarts Traefik once (avoids the plugin/ACME race that 404s the site after a reboot) |

---

## Security

The gateway refuses any call without a valid key and enforces budgets; vLLM and the
runner are firewalled to localhost plus the docker bridge, and the runner
allowlists every launch flag, requires a Bearer token and runs non-root. No
`docker.sock` is mounted by any sidecar — they are driven through scoped `sudo`
on root-owned wrapper scripts, each on its own docker network with no route to
LiteLLM, Postgres or Traefik. The one deliberate exception is the `hawser`
control-plane agent, which mounts the socket for the runner but requires a
`HAWSER_TOKEN` and is IP-firewalled to two admin hosts (see
[`SECURITY.md`](SECURITY.md)). The portal adds LDAP/SSO auth, hardened cookies,
CSRF, a persisted brute-force lockout and a per-request nonce CSP, and a test
fails the build if any route loses its authentication guard.

**Full threat model, the complete list of controls, and the risks knowingly
accepted (starting with OCR, the one sidecar allowed to execute third-party code)
are in [`SECURITY.md`](SECURITY.md).**

### Exposing the API publicly

Path: `api.cronos.website` (**Cloudflare, proxied**) → **Traefik** →
`http://dgx.cronos.lan:4001` (LiteLLM, internal HTTP — TLS terminated at the proxy).
Only route to `4001`, never `8000`/`8001`. Consider a per-key rate limit (rpm/tpm) in
LiteLLM and a Cloudflare rate rule before opening to the internet — budgets cap
tokens/day, not request rate on a single GPU.

---

## Repository layout

```
.
├── install.sh                 # one-shot host bootstrap (packages + systemd + .env)
├── setup.sh                   # generates .env with random secrets
├── docker-compose.yml         # postgres + litellm + portal + frontend + search sidecars
├── .env.example               # placeholders (no real secrets)
├── README.md · SECURITY.md
├── litellm/config.yaml        # models, token pricing, model_info
├── dgx-portal/                # Flask backend — see the module map below
├── dgx-portal-frontend/       # Next.js + Astryx UI (owns the public port 5000)
├── vllm-runner/runner.py      # model lifecycle daemon + scoped sidecar control
├── monitoring/                # host monitors: health alerts + daily DB dumps (systemd timers)
├── ocr/ · voice/ · voice-qwen/ · asr/ · image-gen/   # sidecar images and host wrappers
└── systemd/                   # host units (runner, firewalls, ComfyUI, recreate scripts)
```

### Backend module map (`dgx-portal/`)

The backend was a single 7 200-line file; it is now a shared core, a set of
clients, and one blueprint per feature. [`app.py`](dgx-portal/app.py) is a wiring
facade of ~1 300 lines: it creates the Flask app, sets security headers and the CSP,
keeps the handful of routes that other modules reach by name (`index`, `login`,
`logout`, the OAuth callbacks), registers every blueprint and boots the schema.

**Shared core** — imports nothing from the rest, so nothing can create a cycle:

| Module | Holds |
|---|---|
| [`config.py`](dgx-portal/config.py) | every environment-derived constant, in one place |
| [`db.py`](dgx-portal/db.py) | SQLite access, persisted settings, schema and migrations, the LiteLLM Postgres connector |
| [`auth.py`](dgx-portal/auth.py) | `login_required` / `admin_required`, CSRF, LDAP, brute-force lockout, session lifetime |
| [`guards.py`](dgx-portal/guards.py) | maintenance and rate-limit guards, shared SSE helpers, upload validation |

**Clients and adapters** — one module per thing the portal talks to:

| Module | Talks to |
|---|---|
| [`litellm_client.py`](dgx-portal/litellm_client.py) | LiteLLM: keys, budgets, accounts, model registration |
| [`vllm_health.py`](dgx-portal/vllm_health.py) | the engine: served models, health, throughput, context window, HF search |
| [`sidecars.py`](dgx-portal/sidecars.py) | the runner: launch/stop/logs, and every sidecar readiness probe |
| [`comfyui_client.py`](dgx-portal/comfyui_client.py) | ComfyUI, for video |
| [`mcp_client.py`](dgx-portal/mcp_client.py) | user-configured MCP servers |
| [`websearch.py`](dgx-portal/websearch.py) · [`websearch_tools.py`](dgx-portal/websearch_tools.py) | SearXNG + crawl4ai, and the tool layer exposing them to the model |
| [`notify.py`](dgx-portal/notify.py) · [`discord_notify.py`](dgx-portal/discord_notify.py) · [`announcements.py`](dgx-portal/announcements.py) | mail, Discord webhook and DMs, platform announcements |
| [`stats.py`](dgx-portal/stats.py) · [`local_users.py`](dgx-portal/local_users.py) · [`support.py`](dgx-portal/support.py) | consumption aggregates · local accounts · the Support assistant's tools |

**Route blueprints** — no `url_prefix`, so every path is unchanged:

| Module | Routes |
|---|---|
| [`chat_routes.py`](dgx-portal/chat_routes.py) | `/playground/chat`, `/support/chat` — SSE streaming |
| [`admin_routes.py`](dgx-portal/admin_routes.py) | `/admin/*` — models, sidecars, accounts, quotas, maintenance |
| [`conversation_routes.py`](dgx-portal/conversation_routes.py) · [`settings_routes.py`](dgx-portal/settings_routes.py) · [`memory_routes.py`](dgx-portal/memory_routes.py) | history · user settings · memory graph |
| [`ocr_routes.py`](dgx-portal/ocr_routes.py) · [`voice_routes.py`](dgx-portal/voice_routes.py) · [`asr_routes.py`](dgx-portal/asr_routes.py) | OCR · voice cloning · dictation |
| [`image_routes.py`](dgx-portal/image_routes.py) · [`music_routes.py`](dgx-portal/music_routes.py) · [`video_routes.py`](dgx-portal/video_routes.py) | the generation pages |
| [`preview_routes.py`](dgx-portal/preview_routes.py) · [`discord_routes.py`](dgx-portal/discord_routes.py) | sandboxed HTML preview · Discord account linking |
| [`webauthn_routes.py`](dgx-portal/webauthn_routes.py) | `/api/security/*` — passkey registration, login challenge, 2FA toggle |

Also under `dgx-portal/`: `workflows/` (ComfyUI API-format templates for video),
`tests/`, `requirements.txt` and a non-root `Dockerfile` with no published port.

### Frontend (`dgx-portal-frontend/`)

Next.js 16 + Astryx. `app/(app)/` holds the pages, `lib/` the data helpers and the
i18n dictionary, `proxy.ts` the per-request nonce CSP and method-based routing, and
`lib/sseProxy.ts` the streaming relay to Flask. It has its own `README.md` and
`AGENTS.md` — start there for UI work.

> The `/usr/local/sbin/*-recreate.sh` wrappers and `/etc/sudoers.d/vllmrunner-*`
> live on the host (root-owned). Their tracked sources are `ocr/ocr-recreate.sh`,
> `voice/voice-recreate.sh`, `asr/asr-recreate.sh`, `voice-qwen/voice-qwen-recreate.sh`,
> `systemd/image-recreate.sh` and `systemd/ocr-restrict.sh`; install with e.g.
> `install -o root -g root -m 0755 ocr/ocr-recreate.sh /usr/local/sbin/`.

## License

Licensed under MIT.
