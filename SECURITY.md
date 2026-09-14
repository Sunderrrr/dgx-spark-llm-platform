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
and can reach neither LiteLLM, nor Postgres, nor Traefik — nor each other, with
one deliberate exception: `searxng` and `crawl4ai` share `web_net` (the portal
mediates between them, but at L3 either can open connections to the other).

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
bridge. Spawned engines inherit an **explicit minimal environment** (PATH, HOME,
HF_HOME, PYTHONUNBUFFERED, two vLLM perf knobs) instead of `**os.environ`, so the
root environment's secrets never reach the model process, `/logs` or `/stream`;
`HF_TOKEN` is passed through only when set.

### 2.2 Sidecar control — no docker socket, anywhere

`docker.sock` is mounted **nowhere** in this stack. Sidecar lifecycle goes
through narrowly scoped `sudo` rules (`/etc/sudoers.d/vllmrunner-*`) pointing at
**root-owned wrapper scripts** that fix the image, network, mounts and hardening
flags. The admin only ever controls the trailing arguments, revalidated
host-side.

For the **voice** sidecar there is no free-form argv at all: the single variable
(`repo_id`) is checked against a closed allowlist in both `runner.py` and the
script. The **image** sidecar is the same shape, with per-model inference
settings baked into the wrapper. The **ASR** sidecar (dictation) is the same
shape again: its model id must match a closed `case` list in `asr-recreate.sh`
(`whisper-large-v3-turbo`, `-large-v3`, `-medium`, `-small`), and the dictation
route runs on that one shared ASR model — no LiteLLM key, so it is rate-limited
like the other GPU-heavy routes instead of budget-capped.

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
  **Caveat (checked 2026-09-13): the filter is not armed right now.** The rule is
  keyed to the OCR container's current address on `ocr_net` and is (re)armed by
  `ocr-recreate.sh` at each OCR launch, while the boot-time service is a silent
  no-op when the container is absent — and an exited container leaves the ebtables
  table empty, as today (OCR down for the last 10 days, all chains empty). While
  the sidecar is down nothing can reach the portal from it anyway, but between
  launches the OCR → portal path is *not* filtered. Documented, not repaired:
  whether re-arming belongs in the service itself or stays launch-scoped is the
  operator's call, to settle at the next OCR launch.

Published ports are filtered in `DOCKER-USER` by
`cronos-docker-restrict.service`: the frontend (published `5000→3000`) accepts
Traefik's host only — the unit filters container-side dport `3000`, since
DOCKER-USER sees traffic after DNAT, so a rule on `5000` would match nothing —
and the Traefik dashboard `8080` and the manager agent `8090` are
Netbird/manager-host only. **LiteLLM has no published port at all** (verified
2026-09-13: empty `PortBindings`); it is reachable only over the docker networks.

`GET /metrics` (Prometheus text: CPU, RAM, GPU, temperature, model online) is
for a LAN/Netbird scrape and is **refused with a 403 as soon as the request
carries `Cf-Connecting-Ip`**, i.e. whenever it came through the public edge —
Cloudflare sets that header on every request that traverses it, while a local
scrape arrives without it. This matters because the frontend's catch-all rewrite
(`/:path*` → `dgx-portal:5000`) made every Flask route reachable from
`dgx.cronos.website`, including this one.

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
- **A session is re-checked against the account on every guarded request**
  (`auth.etat_compte`, 2026-09-13). The signed cookie only ever carried a name
  and a role copied at sign-in, so before this, deleting, disabling, blocking or
  demoting an account had **no effect until the cookie expired** (12 h): a
  departed user kept portal access, and a demoted admin kept admin rights. A
  stale session is now dropped on its next request and the event is written to
  the audit log. The role is authoritative for a local account (the portal owns
  `local_users`) and follows the last recorded directory login for LDAP/SSO;
  the code deliberately never *infers* a revocation from an absent record,
  because a missing row is not evidence that an account is gone.
- **Blocking an account works whatever its authentication source**
  (`blocked_users`). Local accounts have `enabled`; LDAP/SSO accounts have no
  local row at all, so the only previous option was revoking their sessions —
  which they simply re-established by logging in again. The refusal is evaluated
  *after* the credentials are verified, so a blocked account cannot be used to
  enumerate accounts, and it is also enforced in the WebAuthn second step: a
  passkey proves identity, it does not grant authorisation.
- **Deleting an account revokes its API access, not just its row.** Deleting a
  local user used to run one `DELETE` on `local_users`; the account's LiteLLM
  keys stayed valid (LiteLLM validates them itself, the portal is not in the API
  path) and its browser session survived up to 12 h. Deletion now revokes the
  sessions, revokes every key, deletes the LiteLLM user envelope and purges the
  personal data (memory, conversations, share links, preferences, passkeys),
  requires an explicit `confirm=DELETE`, refuses self-deletion and refuses to
  remove the last local administrator. If LiteLLM is unreachable the deletion
  still completes — a stuck account is worse — but the response and the audit
  entry name the keys that could **not** be revoked: a silent security failure
  was the defect being fixed. `audit_log` itself is retained on purpose. An
  LDAP/SSO account has no local row at all, so that route cannot reach it:
  `POST /admin/users/<username>/purge` erases its data (memories, conversations,
  share links, preferences, keys) without touching its access — for those
  accounts blocking is the offboarding control, and the interface states the
  difference rather than leaving it to be discovered.
- **Sessions carry their origin.** Each `user_sessions` row records the creation
  time, IP and user-agent, and an account can list and revoke its own sessions
  from Settings → Security (`/api/account/sessions*`) — a stolen cookie is
  otherwise indistinguishable from a legitimate one. The session id is never
  returned in full: only a 12-character fingerprint, and revocation is scoped in
  SQL to the calling account, so guessing another user's identifier cannot close
  their session.
- **Local passwords have a policy** (`local_users.password_policy_error`): 8
  characters minimum, the most common passwords refused, the login name refused
  inside the password, and low-diversity strings refused. No forced rotation and
  no character-class requirement — they push users towards predictable passwords
  without adding entropy. Changing a password (self-service, or by an admin)
  closes every **other** session of that account, which is the point of changing
  it after a compromise; a failure of the current-password check is logged, since
  a burst of those on a live session is a signal, not a typo.

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
- The playground's HTML preview renders model-generated markup in an iframe
  `sandbox`ed **without** `allow-same-origin` (`playground/page.tsx`), so the
  frame's origin stays opaque: generated scripts can neither read session cookies
  nor call the API with the user's rights. No CSP is layered on top — that
  attribute is the control, and adding `allow-same-origin` would void it.

### 2.6 Resource abuse

GPU-heavy routes do not go through a LiteLLM key, so token budgets do not cap
them. A per-account sliding window does, in separate buckets: `rl-media`
(video / OCR / voice / image / music), `rl-support` and `rl-playground`.

The Support assistant, however, *does* bill the account: it calls LiteLLM with
the **user's own key**, not the master key, so token budgets do apply to it and
its usage appears in `SpendLogs` like the playground's. A wrong password during
a security-settings re-verification (`/api/security/remove`,
`/api/security/toggle`) feeds the **same** `login_attempts` counter as `/login`
(`user:<name>`), so an attacker holding a stolen session cannot brute-force the
account password at network speed from the settings page, and the lockout is
shared in both directions.

The assistant's **sensitive actions are confirmed out of band.** Revoking an API
key or starting/stopping the model on the shared GPU used to rely on a prompt
instruction ("always ask the user first"), which is not a control — a model could
call the tool on its own. The chat loop now refuses to execute them: it records a
pending action and the interface renders a Confirmer/Annuler button, with
`/support/confirm` as the only execution path. The token is opaque, bound to the
account, single-use (a conditional `UPDATE ... WHERE status='pending'` decides
which of two concurrent clicks wins) and expires after 10 minutes; critically it is
**never placed in the model's context**, so an indirect prompt injection (an MCP
result, a crawled page, a skill's text) cannot replay it. The pre-existing rule
still stands on top: once third-party tool output has entered a turn, the guarded
tools are refused for the rest of that turn.

Two residual facts about the feedback channel: `support_feedback` has **no
retention** — rows accumulate until someone prunes them — and its only visibility
is the admin-only aggregate `GET /admin/support/feedback` (per-user counts plus
the last 50 entries), so a 👎 comment must be treated as operator-readable data,
not as a private message.

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

The frontend's own dependency tree is kept at its patched versions rather than
its original ones: `next` 16.3.5 (16.3.0 carried the AVIF-decoding RCE
advisories), `sharp` 0.35.4 and `js-yaml` 4.3.2 — `npm audit` reports **0
vulnerabilities** (2026-09-13). Those three were proven unreachable in this
deployment anyway (remote image URLs are rejected, local ones are replayed
in-process without visitor cookies, and every file route is `@login_required`;
output is negotiated as `image/webp`), but the upgrade removes the question.

> **Never set `--memory` on a sidecar.** On unified memory a container memory cap
> also caps GPU memory and breaks CUDA loading. This is an availability control
> that looks like a hardening one, and it must not be "fixed". See `CLAUDE.md`,
> the operating guide kept on the machine (not published).

One host path is mounted into `dgx-portal` **read-only**: `/var/lib/cronos-monitor`,
the host monitor's state file, which holds current incidents and the backup figures
the Admin card displays. The backup directory itself (`/var/backups/cronos`, `0700
root`, dumps `0600`) is deliberately **not** mounted, and the monitor — which runs as
root — copies only the file name, age and count into its world-readable state. So the
container can report *that* a backup exists and how old it is without ever being able
to read one. If the mount is missing the API answers `readable: false`, which the
interface renders as "unreadable" — never as "fine".

### 2.9 Admin actions report what actually happened

Every admin action answers JSON (`{ok, error?, warning?}`) with an honest status
code: 400 refusal, 404 unknown, 409 `needs_confirm` or "last administrator", 502
upstream unreachable, 507 memory guard, 403 forbidden. This replaced
`flash(...)` + `redirect(...)`, whose flashed message no template rendered and
whose HTML body made the Next.js client's `res.json()` throw — the `catch` then
treated the failure as a success, so a refused model stop or an unapplied quota
could be shown as done. A `warning` accompanies a partial success (catalogue
updated, LiteLLM deregistration failed) and is never used in place of an error.

An **uncertain** outcome is reported as such: when the runner does not answer
within its timeout, the response is `202` with `incertain: true` and *no*
infrastructure alert, because an alert would invite a retry that kills a model
still loading. Related availability control: the portal refuses to leave itself
without a local administrator (self-demotion/self-disable, and removal of the last
active local admin or of the group carrying its rights). The role is re-read on
every request, so that loss would be immediate and unrecoverable from the UI.

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
prevents it from opening a connection to the portal — *when armed*, which it
currently is not (see the caveat in [§2.3](#23-network-isolation)).

Residual risk: it still shares a network *segment* with the portal, and the
portal holds the master credentials. A fresh audit of the live URL map
(2026-09-13) counts 153 routes, 11 unauthenticated, every one on the guard test's
documented allowlist (login flow, CSRF, forwardAuth, liveness `/healthz`, the
LAN-scraped `/metrics`) and none returning account or conversation data — so
today there is nothing to reach. The control above exists for the day that stops
being true.

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
  ever travels in a URL, so there is no ongoing leak. LiteLLM's uvicorn access
  log is baked into the digest-pinned image and has no config/env switch, so
  it is *not* silenced (doing so would mean forgoing digest-pinning). The
  residual risk is only keys that already hit the log — rotate them; the
  portal never puts a key in a URL again.
- **HSTS: in place on the portal, still missing on the API (measured
  2026-09-13).** `dgx.cronos.website` answers
  `strict-transport-security: max-age=31536000; includeSubDomains`, but
  `api.cronos.website` sends **no HSTS header at all** — the previous claim that
  HSTS was "resolved" only ever covered the portal. The fix belongs in the
  **Cloudflare zone setting** (SSL/TLS → Edge Certificates → HSTS), which covers
  both hostnames in one toggle; it must *not* be done by adding a Traefik
  middleware to `/opt/traefik/dynamic/routes.yml`, since that file is rewritten
  by `traefik-manager-agent` and a hand edit would silently disappear.
- **Two containers run from a floating `:latest` tag.** Neither is started from
  `docker-compose.yml` (both are `docker run`, `unless-stopped`), so no
  declarative file pins them: `ghcr.io/chr0nzz/traefik-manager-agent` (running
  digest `sha256:39796546cd1584a2ac6381e3e0f9aed4aaad1b8bf3a1cf08ca1fb540f31dbc81`)
  and `ghcr.io/finsys/hawser` (running digest
  `sha256:526a31f81c92ec750e60fc5da0d7551bbcf8441f1effc0b9741ccb2b2b594131`).
  A `:latest` tag only moves when someone pulls, so the exposure is not a silent
  auto-update but an unpinned rebuild: pinning means recreating both containers
  by digest, which for the Traefik config agent is a worse trade than the risk
  it removes. Revisit at the next maintenance window.
- **The account-lifecycle gaps, closed on 2026-09-13.** Reviewing user management
  turned up four holes that had been there from the start, all of the same shape —
  a security-relevant action that only *looked* complete: deleting an account left
  its API keys valid and its session alive; an LDAP/SSO account could not be
  blocked at all; demoting an admin took effect only at cookie expiry; and a quota
  that failed to reach LiteLLM was swallowed silently, leaving an uncapped account
  reported as created. §2.4 describes what replaced each. What remains open by
  choice: the lockout stays a fixed 15-minute window on both the IP and the
  account key rather than growing with repeats — a progressive lockout would let
  anyone extend a known account's outage, so the active lockout is surfaced to the
  admin instead.
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
