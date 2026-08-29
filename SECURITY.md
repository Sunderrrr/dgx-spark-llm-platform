# Security — Cronos

Security posture of the Cronos platform: what it trusts, what it enforces, and
what it knowingly accepts. Audience is operators and auditors; it is meant to be
read on its own, without the [README](README.md).

One sentence of context: Cronos runs on a **single NVIDIA DGX Spark** (GB10,
128 GB unified memory, aarch64). One box, multi-user, production. There is no
second machine to fall back on, and every sidecar shares the same memory pool —
which is why several controls below are about *resource* abuse, not just access.

- [1. Threat model and trust boundaries](#1-threat-model-and-trust-boundaries)
- [2. Controls in place](#2-controls-in-place)
- [3. Accepted risks](#3-accepted-risks)
- [4. Reporting a vulnerability](#4-reporting-a-vulnerability)

---

## 1. Threat model and trust boundaries

### The request path

```
client → Cloudflare → Traefik → dgx-portal-frontend (:5000) → dgx-portal (Flask)
                        │                                        ├→ LiteLLM (:4001) → Postgres
                        │                                        ├→ vllm-runner (:8001) → vLLM (:8000)
                        └─ forwardAuth → /internal/authcheck      └→ sidecars (OCR, voice, ASR, image, music, video, web search)
```

TLS terminates at Cloudflare/Traefik; everything behind it is plain HTTP on
docker networks. `dgx-portal` has **no published port** — only the Next.js
frontend is reachable from outside, and it talks to Flask over the internal
network.

### Who is trusted with what

| Boundary | Trusted for | Not trusted for |
|---|---|---|
| Anonymous internet | nothing | — |
| Authenticated user (LDAP / SSO / local) | their own keys, budget, conversations, media jobs | anything scoped to another account, any admin action |
| API key holder | inference within the account budget | portal routes, admin routes |
| **Admin** (`adm_cronos`) | model lifecycle, sidecar lifecycle, accounts, quotas, maintenance | — (see [accepted risks](#3-accepted-risks)) |
| `vllm-runner` (host daemon) | spawning vLLM, scoped `sudo` on sidecar wrappers | arbitrary host commands, docker socket |
| Third-party model code (OCR, HF repos) | nothing | — it is treated as hostile |

### The multi-homing question

`dgx-portal` is deliberately attached to **every** sidecar network — it is the
only component that talks to all of them. That makes it the one process whose
compromise matters most: it holds `LITELLM_MASTER_KEY`, `RUNNER_TOKEN`,
`LDAP_BIND_PW`, `OIDC_CLIENT_SECRET`, `SECRET_KEY` and `SMTP_PASSWORD` in its
environment.

The sidecars themselves are **not** multi-homed: each sits on its own network
and can reach neither LiteLLM, nor Postgres, nor Traefik, nor each other.

---

## 2. Controls in place

### 2.1 Model launching — a strict allowlist, not a denylist

`vllm-runner/runner.py` validates every flag of `vllm_args` against a closed
allowlist per engine (`vllm`, `llamacpp`, `ds4`, OCR). Anything not on the list
is refused. The exclusions are deliberate, and each is commented in the source:

| Blocked | Why |
|---|---|
| `--trust-remote-code` | executes arbitrary Python from the HF repo (blocked for chat; see [OCR](#31-ocr-executes-third-party-code-by-design)) |
| `--download-dir`, `--chat-template`, `--tokenizer` | arbitrary file read, and Jinja2 template injection through the chat template |
| `--model`, `--host`, `--port`, `--api-key` | forced by the runner; overriding them would move or expose the endpoint |

The daemon runs **non-root** (`vllmrunner`), requires a Bearer token compared with
`hmac.compare_digest`, and its ports are firewalled to localhost plus the docker
bridge.

### 2.2 Sidecar control — no docker socket, anywhere

`docker.sock` is mounted **nowhere** in this stack. Sidecar lifecycle goes
through narrowly scoped `sudo` rules (`/etc/sudoers.d/vllmrunner-*`) pointing at
**root-owned wrapper scripts** that fix the image, network, mounts and hardening
flags. The admin only ever controls the trailing arguments, revalidated
host-side.

For the **voice** sidecar there is no free-form argv at all: the single variable
(`repo_id`) is checked against a closed allowlist in both `runner.py` and the
script. The **image** sidecar is the same shape, with per-model inference
settings baked into the wrapper.

**The one exception is `hawser`** (`ghcr.io/finsys/hawser`, systemd
`hawser-restrict.service`), the Docker control-plane agent used to drive the
runner. It *does* mount `/var/run/docker.sock` (plus host `/opt` and `/root`) —
this is deliberate, not a sidecar, and it is scoped down two ways: its HTTP API
(`0.0.0.0:2376`) is firewalled to exactly two admin hosts and it requires a
bearer `HAWSER_TOKEN` before it will act. A compromise of `hawser` is host-root
equivalent, so its token stays a protected secret and its allowlist stays
minimal (see §3.2).

### 2.3 Network isolation

Each sidecar has its own docker network — `ocr_net`, `voice_net`, `asr_net`,
`image_net`, `music_net`, `web_net` — shared only with `dgx-portal`. Third-party
model code therefore has no L3 route to LiteLLM, Postgres or Traefik.

Two host-level rules complete this, because a docker network does not isolate
the *host*:

- `cronos-web-restrict.service` drops every new connection from `web_net` to the
  host. Before it, the host's SSH port accepted connections from the crawler.
- `cronos-ocr-restrict.service` prevents the OCR container from opening a
  connection to the portal. It uses **ebtables**, not iptables: `br_netfilter` is
  not loaded, so traffic between two containers on the same bridge never
  traverses iptables. Only the portal's listening port is dropped — the filter is
  stateless, and blocking everything would kill the responses of the legitimate
  portal → OCR flow.

Published ports are filtered in `DOCKER-USER`: `4001` (API) from the LAN and the
Netbird VPN, `5000` (frontend) from Traefik only.

### 2.4 Session and request integrity

- Cookies: `HttpOnly` + `SameSite=Lax` + `Secure` behind TLS.
- CSRF token per session, compared on bytes with `hmac.compare_digest` (a exotic
  token yields a rejection, not a 500). Session and CSRF token are regenerated at
  login.
- Brute-force lockout persisted in SQLite — so it is shared across gunicorn
  workers and survives a redeploy. Two classic bypasses closed at once.
- `Cf-Connecting-Ip` is validated as a real IP address before being used as the
  lockout key, so the counter cannot be poisoned with arbitrary values.
- Per-request nonce CSP on `script-src` (`dgx-portal-frontend/proxy.ts`), plus
  `nosniff`, `X-Frame-Options: DENY` and `Referrer-Policy`.
- **A test enforces the invariant**: `GardeDesRoutesTest` walks Flask's URL map
  and fails if any route lacks a `login_required` / `admin_required` guard,
  except an explicitly documented public list (login flow, CSRF token,
  forwardAuth). It reads the live route table, not the source, so a route
  registered any other way is still caught.

### 2.5 Input validation

- `MAX_CONTENT_LENGTH` (16 MB) is enforced **before** multipart parsing, so an
  unauthenticated POST cannot stream gigabytes to disk ahead of the auth checks.
- LDAP: `escape_rdn` + `escape_filter_chars` + a username regex; empty-password
  binds are rejected (some directories treat them as successful "unauthenticated"
  binds).
- Audio sidecars read the **header before decoding** (`sf.info()`), bounding
  duration, sample rate and channel count. A small crafted FLAC can otherwise
  decompress into tens of gigabytes — on a shared unified-memory pool that takes
  down the whole box, not just the sidecar.
- Web search resolves every URL and checks it is public **before** the crawler
  sees it (`websearch.url_publique`). The network cannot do this on its own: a
  hostile page can redirect anywhere.

### 2.6 Resource abuse

GPU-heavy routes do not go through a LiteLLM key, so token budgets do not cap
them. A per-account sliding window does, in separate buckets: `rl-media`
(video / OCR / voice / image / music), `rl-support` and `rl-playground`.

### 2.7 Maintenance mode

A database flag, enforced twice. Inside the portal, chat and media routes check
it and let admins through. For the public API, a Traefik `forwardAuth`
middleware calls `/internal/authcheck` on every request: an immediate 200 when
maintenance is off (no lookup, no cost), and when it is on, only API keys
resolving to an admin account pass — everyone else gets a 503 before the request
reaches LiteLLM.

### 2.8 Container hardening

Sidecars run with `--cap-drop ALL --no-new-privileges`, non-root where the
upstream image allows it, models mounted read-only. `dgx-portal` and
`dgx-portal-frontend` are non-root with dropped capabilities. Base images are
digest-pinned.

> **Never set `--memory` on a sidecar.** On unified memory a container memory cap
> also caps GPU memory and breaks CUDA loading. This is an availability control
> that looks like a hardening one, and it must not be "fixed". See
> [`CLAUDE.md`](CLAUDE.md).

---

## 3. Accepted risks

These are choices, not oversights.

### 3.1 OCR executes third-party code by design

The OCR container is the only place where `--trust-remote-code` is allowed: the
OCR models that need it genuinely require it. It is therefore the one surface
running arbitrary Python from a Hugging Face repository.

Mitigations: only an admin can point the catalogue at a repo; the container is
alone on `ocr_net`; it has a dedicated HF cache isolated from the runner's;
`cap-drop ALL` and `no-new-privileges` apply; and `cronos-ocr-restrict.service`
prevents it from opening a connection to the portal.

Residual risk: it still shares a network *segment* with the portal, and the
portal holds the master credentials. An audit of all 108 routes found 7
unauthenticated ones, all by design and none returning data — so today there is
nothing to reach. The control above exists for the day that stops being true.

### 3.2 "Admin-only means safe"

Several controls reduce to "only an admin can do this". Since the runner holds
scoped `sudo`, a compromised admin account is effectively host compromise. This
is normal for a platform of this shape, but it means the number of admin
accounts is a security parameter — keep it small.

### 3.3 Known open items

- **LiteLLM logs API keys in clear.** The portal used to call
  `GET /key/info?key=sk-…`, and LiteLLM's access log records the full URL, so
  `docker logs litellm` exposed usable keys. The portal side is fixed — it now
  reads LiteLLM's database directly, keyed by the SHA-256 of the key, and no key
  ever travels in a URL. Any other client calling that endpoint would leak
  again; silencing LiteLLM's access log is the remaining hardening.
- **HSTS (resolved).** The Cloudflare edge previously overrode the origin's HSTS
  with `max-age=0`; it is now `max-age=31536000; includeSubDomains` (set in the
  Cloudflare dashboard, verified on `/` and `/login`).
- **The historical monolith.** `dgx-portal/app.py` was a single 7 200-line file
  holding auth, budgets, admin and media proxying, which made it hard to
  guarantee no route had lost a guard. It is now a wiring facade over a shared
  core and route blueprints (see [README → Repository layout](README.md#repository-layout)),
  and the route-guard test above makes the invariant explicit rather than
  assumed.

---

## 4. Reporting a vulnerability

This is a personal, self-hosted deployment; there is no bug-bounty programme.
Open a GitHub issue for anything non-sensitive, and contact the repository owner
directly for anything that should not be public.
