# Rust migration of the portal

The portal backend (Flask, `dgx-portal/`) and the model runner
(`vllm-runner/`) move to Rust **route by route**, with no cut-over day. This
file is the plan and its rules; [rust-routes.md](rust-routes.md) is the live
inventory (generated, one row per route, where it is served today).

## What moves, what stays

| Component | Today | Target |
|---|---|---|
| Portal API — 175 routes, ~22 000 lines, 905 unit tests | Python / Flask | **Rust** |
| Model runner — ~1 500 lines | Python / Flask | **Rust** |
| Host monitor — ~400 lines | Python | **Rust** |
| Frontend — Next.js / React | TypeScript | stays TypeScript |
| LiteLLM in-flight callback | Python plugin | stays (runs inside LiteLLM) |
| Media sidecars, vLLM | Python / PyTorch | stay |

Expected resource gain, measured before starting (2026-10-14): about 0.25 GB
of RAM and 1-2 % of one core — the model engines hold the memory. The aims are
a single static binary, strict typing and fewer runtime dependencies; each
phase is measured against them (RSS, CPU, p50/p95 latency of its routes).

## Rules of the cohabitation

- **Same contracts.** A route keeps its URL, methods, status codes and JSON
  body. Server messages stay byte-identical: the UI recognises French
  messages to translate them (`tServer`).
- **Same data.** Both services use the portal's SQLite database (WAL mode,
  busy timeout) and LiteLLM's Postgres read-only. One side owns the schema
  migrations at a time: Python until phase 8, then Rust (`sqlx migrate`).
- **Same sessions.** The `session` cookie is an opaque token; the session
  data is the JSON in `web_sessions`, keyed by `sha256(token)` hex, with
  `expires_at` in epoch seconds (`dgx-portal/session_store.py`). Rust reads
  and writes that row; it never needs Flask's cookie format. The CSRF token is
  the session's `csrf` value, sent back in the `X-CSRFToken` header.
- **Switch by prefix.** `migration/rust-routes.txt` lists the URL prefixes
  served by Rust; the frontend's proxy routes them there, everything else to
  Flask. Going back is removing a line and redeploying.
- **The tests are the arbiter.** The end-to-end journeys (`tests/e2e/`) and
  `scripts/validate.sh` do not change: a phase is done when the validation is
  green with its prefixes on Rust. Before a read route switches, the
  comparison bench (`scripts/compare-backends.py`) replays it on both
  services and reports any difference.

## Moving a prefix to Rust

1. Run the comparison bench on the prefix's routes (Flask as A, Rust as B):
   `python3 scripts/compare-backends.py --a <flask> --b <rust> --phase N --user … --password-file …`.
   It replays only GET routes without side effects, and confirms a difference
   on a second call before reporting it.
2. Add the prefix to `migration/rust-routes.txt` and set `DGX_RUST_URL` in
   `.env` (the Rust portal's address on the stack's Docker network).
3. Recreate the frontend (`docker compose up -d dgx-portal-frontend`): it reads
   both at start (`dgx-portal-frontend/lib/backend.ts`) and routes the prefix
   to Rust for every method, page data and SSE relays included.
4. Regenerate the inventory (`python3 scripts/routes-inventory.py`) and run
   `scripts/validate.sh`. Rolling back is the same in reverse.

A prefix covers itself and its sub-paths (`/api/ranking` covers
`/api/ranking/activity`, not `/api/rankings`). The switch was checked live on
2026-10-14 with a throwaway stand-in on an unused prefix: GET and POST reached
it, a look-alike path and the rest of the portal stayed on Flask.

## Phases

| # | Phase | Scope | Notes |
|---|---|---|---|
| 0 | Preparation | server-side sessions, route inventory, comparison bench, proxy switch | done in Python; useful on its own |
| 1 | Runner | `vllm-runner` (process lifecycle, scoped sudo) | isolated; restarting it stops the served model — maintenance window |
| 2 | Stateless reads | health, home, leaderboard, statistics | first portal routes; shadow-compared |
| 3 | Media | image, video, OCR, voice, object storage | file and S3 paths (`media_store`) |
| 4 | User data | settings, conversations, skills, memory, keys, MCP | |
| 5 | Admin | models, accounts, configuration, storage | |
| 6 | Authentication | local, LDAP, SSO, WebAuthn, CSRF | dedicated security review |
| 7 | Chat | playground and Support SSE relays, tools, web search, MCP | hardest: heartbeat, cancellation, structured notices |
| 8 | Clean-up | remove Flask/gunicorn, Rust owns the schema | after two weeks without incident |

Decision point after phase 2: compare the Rust service with Flask on the same
routes. The mixed state is stable — the migration can stop at any phase.

### Phase 2 — state (bench against live Flask, demo account)

Identical on every case replayed: `/api/config`, `/.well-known/security.txt`,
`/api/session`, `/api/whoami`, `/api/ranking` over the 5 periods × 5
metrics, by group, with a model filter and with invalid parameters, and the
rest of the leaderboard — `activity` (3 periods × 2 scopes), `records`,
`user/<name>` (own detail; someone else's is 404), `export.csv` (404 for a
non-admin) and `POST privacy` (the CSRF check runs first: 400 without a
valid token, even with no session, as in Flask), `/api/modelhealth`, and
`/api/home` and `/api/running-models` with the media sidecars (portal-rs
joins the sidecar networks like dgx-portal, the portal it replaces, with
`default` keeping the default route), `/usage/hourly`, `/system/stats`
(the runner's status, the active users for an admin), `/metrics` (the
Cloudflare guard included), `/api/branding/logo`, `/api/search` (Hugging Face
outage answered 503, unknown task 400), `/docs` (byte-identical to Flask's
page) and the root page (302 to the login
page when signed out, 204 when signed in). The
`login_required` guard is ported whole (max age, `user_sessions` registry,
account state re-read, owner rule); a database error answers 503, never a
logout. The security headers (`nosniff`, CSP, HSTS…) match Flask's.

Not routable yet, and why:

- **Admin views, ported but never compared**: the bench account is not an
  admin, so `active_users` on `/api/home`, another account's ranking detail
  and `export.csv` have only been checked to answer 404 to a non-admin.
  `quota_pct` on `/api/ranking` needs LiteLLM's `/user/list` and is `null` on
  the Rust side.
- **`/api/latency` is not ported on purpose**: it is an in-memory ring of this
  process's own request durations. A Rust port would report the Rust
  process's latencies under the same name, which is a different measurement;
  the Rust route answers 501 until the routes move and the measurement moves
  with them.
- **Flask-only writes**: `vllm_health` archives the engine's token counter
  (`model_counters`) and `_active_users` sweeps stale `inflight_requests`.
  The Rust side computes the same figures READ-ONLY — two probers writing one
  row would take a later reading for a counter reset and archive it twice —
  so these writes must move to Rust together with the routes, not before.
- **Dropped sessions**: Flask writes a `session.invalidee` audit line and
  clears the cookie; the Rust portal only refuses the request.

**Routed to Rust since 2026-10-14** (`migration/rust-routes.txt`): `/api/config`,
`/healthz`, `/api/session`, `/api/whoami`, `/api/health`, `/api/modelhealth`,
`/api/running-models`, `/api/search`, `/.well-known/security.txt`,
`/api/branding/logo`, `/usage/hourly`, `/metrics`, `/docs`, and the phase 3
read-only media routes (see below). Each prefix covers
only itself. Checked from the frontend's own traffic: these reach `portal-rs`;
`/api/home`, `/api/ranking`, `/api/latency` and look-alike paths stay on Flask.
Held back: `/api/home`, `/system/stats` and `/api/ranking`, whose ADMIN
branches are ported but not compared (the bench account is not an admin).
Rollback: empty the file and restart the frontend (`docker restart
dgx-portal-frontend`: it reads the file at start, and `compose up` does not
recreate it when only the file changed).

### Phase 3 — state (media, read-only part)

Routed: the histories of image, OCR, video and voice, the image job status
(404 for another account's job), `/api/voice/info` and
`/api/transcribe/available`. Identical to Flask on the bench; the image
history was compared on 56 real jobs, OCR, video and voice on empty
histories only (the demo account owns none), so their content is unverified.

Not ported, on purpose:
- **Anything that creates or acts on a job** (generate, extract, speak,
  transcribe, cancel, delete, the voice chat): they write rows, call sidecars
  and spend budget. They move as whole flows, with their guards.
- **`/video/file`**: on a cache miss it pulls the file from ComfyUI and
  stores it into the bucket — a write, it moves with the video flow.
- **`/api/video/status`**: it asks ComfyUI and persists the result.

### Phase 3 — stored files, routed (`/image/file`, `/ocr/image`, `/voice/audio`)

`portal-rs/src/s3.rs` is a minimal S3 reader (SigV4, path-style GET only):
the files are read from the local volume first, then from the bucket with
the browser's `Range` passed through (206 + `Content-Range`, streamed).
Compared with Flask on the demo account: 4 jobs × 3 indexes × 4 Range forms
plus the error cases, 72 of 72 identical (status, headers, SHA-256 of the
body). OCR and voice files: the demo account owns none, so only their
refusals were compared.

Two things botocore does silently, and a bare client must do itself:
- **the region redirect**: with the region setting empty, the signature
  scope says `us-east-1`; Garage refuses it with `AuthorizationHeaderMalformed`
  and names the region it expects. The client re-signs once with that
  region and remembers it per endpoint.
- **a User-Agent**: the proxy in front of the bucket rejects anonymous
  clients (Cloudflare's error 1010) — a 403 that looks like a bad key.

These are page paths for `login_required`: signed out, Flask answers 302 to
`/login?next=<path>` (not 401), and its error pages carry werkzeug's HTML
body. `auth::html_status` now writes those bodies for 400/401/404/500, which
also fixes the empty 401 body the routes moved earlier returned.

### Phase 4 — the small writes, routed

`/api/onboarding/done`, `/discord/unlink` (302 to `/?discord=unlinked`) and
`/skills` (a skill written in the interface: create, update, delete; refusals
answer 200 with `ok: false` and the delete 204, as Flask does). Compared on
the demo account, its state restored afterwards.

The CSRF check reads the token from the `X-CSRFToken` header OR a form's
`csrf_token` field, as Flask's `_csrf_protect` does, on every write — the first
version of the shared helper only read the header and refused a form token
(found by this comparison, fixed for every write).

Still on Flask in phase 4, on purpose: `/mcp` (a live MCP connection, part of
the MCP client of phase 7), `/keys` and `/request` (Next pages share their
paths, see the routes file; `/keys` also issues LiteLLM keys and `/request`
sends e-mails), the Discord OAuth link and callback, and the account password
and deletion (phase 6).

### Skills: routed (`/api/skills`, its 7 routes)

List, detail, download, enable, sync, and the .zip and Git imports
(`portal-rs/src/skillstore.rs`, routes in `handlers/skill_routes.rs`). The
security-sensitive parts are checked against outputs of the Python code
itself (`tests/fixtures/skills_ref.json`, `skills_zip.json`): the SSRF address
rule of `mcp_client._is_blocked_ip` on 6 947 addresses (Python 3.12's
`ipaddress` ranges and exceptions, IPv4-mapped IPv6 included), the zip member
path rule, the SKILL.md front matter, the Git URL forms, and the archive
reading on trapped archives (path traversal, absolute and drive paths, `.git`,
symlink, binary, oversized and non-UTF-8 members, wrapper folder, nested
skill, sub-folder, 52 skills, not-a-zip). A download resolves the host once,
refuses it if ANY address is internal, and pins the first one (no DNS
rebinding), with no redirect and a 25 MB cap. Compared on the demo account
(27 checks identical, its skills removed afterwards): imports, replace,
detail, download, enable, sync, ten SSRF refusals (`localtest.me`, which
resolves to 127.0.0.1, included), and a real import of a public GitHub
folder then its sync. One known gap: the zip crate exposes the Unix mode only
for archives made on Unix or DOS, so a symlink flagged in an archive claiming
another system would be read as a plain text file (never followed).

### Memory: routed (`/api/memory`, its 11 routes)

The graph read, the settings, add / correct / forget a fact, the purge, the
JSON and Markdown export and import (`portal-rs/src/memory.rs`). Three
pieces are checked against outputs of the Python functions themselves:
`_mem_norm` (1 527 cases, `tests/fixtures/mem_norm.json`), `_md_parse` and
`_split_sentences` (1 203 generated documents and 600 sentences,
`tests/fixtures/memory_md.json`; the sentence split is written by hand, Rust's
regex has no look-around; `str.splitlines()` is reproduced with its 11 line
boundaries). The routes were compared on the demo account, whose memory was
snapshotted raw and restored exactly afterwards: reads, both exports, every
write and its error paths, the expiry of a fact on an explicit relation, both
imports, the switches and the purge. One deliberate difference: Flask sends
`text/markdown; charset=utf-8; charset=utf-8` for the Markdown export (a
duplicated parameter); the Rust side sends it once. The chat keeps writing
memory on the Flask side, on the same tables with the same normalization.

### Phase 4 — state (user data, read-only part)

Ported and compared (bench, demo account): `/api/pending-count` and the
conversation reads `/api/conversations` and `/api/conversations/<id>` (routed:
their prefix holds only GET routes, the writes live under `/conversations`).
Also routed: `/api/keys` (the viewer's keys in clear, with LiteLLM's figures
read from its database; the budget reset date of a key goes out as an HTTP
date, as Flask's JSON encoding does; its only route is GET, the writes are
under `/keys`), and `/api/discord/status` (the configured branch is not exercised: no
Discord is set up on the bench). Also routed: the conversation writes `POST /conversations` (save, delete) and
`POST /conversations/share`. The save stores the SAME text as Flask: the
messages are reshaped as Flask does (user/assistant only, bounded content,
flags, file edits, image data URLs, the last 60) and serialized like Python's
`json.dumps(..., ensure_ascii=False)` (`, ` and `: ` separators, insertion
order — serde_json runs with `preserve_order`). Compared on the demo account:
the stored text was byte-identical for a payload with Unicode, control
characters, hidden messages, duplicate file names and a rejected image; every
error identical; share tokens are 32-character `token_urlsafe(24)`; the test
data was removed afterwards. Also routed: the two preference writes `/settings/appearance` and
`/settings/avatar` (CSRF first; allow-lists first, so a refusal writes nothing;
compared on the demo account, values restored afterwards). Not routed: the
account's password and deletion (phase 6, with its security review).
Also routed: `/c/<token>`, a shared conversation (the page is byte-identical to
Flask's for the demo account's share; unknown token 404; every view counts,
as in Flask; signed-out visitors are sent to the login page). The share
creation (`/conversations/share`) stays on Flask. Also routed:
`/api/account/sessions` and its revoke POST (CSRF first; the
viewer's own sessions, the 12-character identifiers, the origin filled in only
where missing; the audit line written in Flask's format, the 5 000-row
trimming included; the error paths identical). Checked on two throwaway
sessions of the demo account: each backend revoked one, both gone from the
listing, both audit rows present. `/api/settings` (the settings page's data: MCP servers, skills,
preferences, the account block, the 182-day activity and the real quotas;
identical to Flask on the demo account, which has 13 MCP servers and activity
to compare; the page's writes live under other prefixes), `/api/notifications`
and `/api/announcements`, with their
mark-as-seen POSTs (CSRF checked first, as Flask does). Also ported:
`/api/skills`. Not routed:
`/api/notifications`, `/api/announcements` and `/api/skills` have write routes
under the same prefix (`/seen` POST, skill import, sync, enable), which the
Rust portal does not serve yet; a prefix would capture them. Verify before
routing any prefix: no route under it may need another method.

Porting notes that cost a wrong first attempt: LiteLLM listens on 4001 inside
the Docker network and `/health` needs a key since 1.102 (probe
`/health/liveliness`); instance settings are stored as `cfg_<name>` with empty
meaning unset; `password_managed_by` comes from the cumulative
`user_sources.sources`; ties in the ranking follow Postgres' aggregation
order, so the Rust side runs ranking.py's SQL verbatim as a subquery; and
Python's `round()` is ties-to-even on the exact binary value, which Rust's
`{:.n}` formatting reproduces and a multiply-and-round does not. The
instantaneous throughput and the age of the engine's tasks keep state per
process (each gunicorn worker has its own): they never match across backends
and the bench ignores them (`tps`, `oldest_s`, `last_s`, `since_s`).

## Stack

`axum` + `tokio` (HTTP), `sqlx` (SQLite, Postgres), `reqwest` (LiteLLM,
sidecars, SSE), `serde`, `tracing`, `ldap3`, `openidconnect`, `webauthn-rs`,
`aws-sdk-s3` or `rusty-s3`, `lettre` (SMTP).

## Carried-over rules to check at each phase

SSRF filters (`mcp_client._is_blocked_ip`, `websearch.public_url`), the
Support's guarded tools and their refusal after untrusted content, the CSP of
previews, the « a failed probe is not proof of absence » rules (model list,
accounts), 503 instead of 502 for upstream failures (Cloudflare replaces 502
bodies), and every limit in `guards.py`.
