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
`/api/ranking/activity`, not `/api/rankings`). An entry may name one method
first (`POST /keys`), end with `$` to cover that exact path only
(`GET /api/admin$` leaves `/api/admin/users` on Flask), and use `*` for any
one segment (`POST /admin/users/*/unblock`). The matcher is compiled into the
frontend: changing it (not the list) needs a frontend rebuild. The switch was checked live on
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
- **`/api/video/status`**: it asks ComfyUI and persists the result.

### Phase 6 — first session routes, routed (`/api/csrf`, `/logout`)

Sessions are server-side since phase 0, so Rust can write them:
`portal-rs/src/session_store.rs` keeps the format of
`dgx-portal/session_store.py` (token `token_urlsafe(32)`, row keyed by its
SHA-256, data as sorted compact JSON, 12 h with an account and 2 h without,
the 2 % sweep) and Flask's cookie attributes (`Secure; HttpOnly; Path=/;
SameSite=Lax`, no Expires; the deletion with `Max-Age=0`).
- `/api/csrf` creates the token lazily, as Flask does: an existing token
  comes back with no write and no cookie.
- `/logout` checks CSRF, revokes the `sid` in `user_sessions`, deletes the
  row and the cookie, then redirects to `/login` — or to the identity
  provider's end-session after an SSO login.
Checked: a Rust-minted session is read by Flask (its login keeps the
token), and the whole flow through the proxy — token from Rust, login on
Flask, logout on Rust, 401 afterwards.

The session guard now drops what Flask drops: an expired or revoked
session loses its row at the first refused request, and an account found
blocked or disabled leaves the `session.invalidee` audit line (checked: a
demo session revoked from another one gets 401 and its row is gone). The
Set-Cookie that deletes the cookie itself is not sent; the token then names
nothing.

Not ported: the conversion of a legacy signed cookie (a `.` in the value).
Those predate phase 0 and are past their 12 h; `/api/csrf` opens a new
session for one.

The login itself (`/login`, local + LDAP + SSO, WebAuthn, the lockout
counter, the setup token) stays on Flask.

### Phase 3 — image generation, routed (`/api/image/generate`, `cancel`, `delete`)

The job runs in a tokio task: one sidecar call per variation, the
cancellation checked before each, each image saved through
`media_store::save` (the bucket when Admin → Stockage says so — S3 PUT
signed like the reads — else the local volume, and the local volume when the
bucket refuses). Guards in the same order as Flask: CSRF, session,
maintenance (admins pass), the `rl-media` bucket (the same SQL row as
Flask's, so both count against one window), then the in-flight bound of 3
jobs per account. The form is read urlencoded or multipart (the page sends
`FormData`).

Not tested live: a generation on the demo account would trim its history
(79 jobs, the route keeps the 20 newest). The worker is covered by an
integration test instead (fake sidecar, scratch database: a whole batch, then
a cancel during the first image), and every refusal was compared with Flask.

Two Flask defects found on the way:
- `_image_cancelled` read `row['status']` on a bare tuple: the exception was
  swallowed, so « Arrêter » never stopped a batch and the final status
  overwrote « cancelled ». Fixed on both sides (test in
  `test_image_tools.CancelFlagTest`); Flask still runs the playground image
  tool's jobs.
- a refused job slot (429) left its row « running » forever: Rust takes the
  slot before inserting the row.

### Phase 4 — API key actions, routed (`POST /keys`)

A routing entry may now name ONE method (`POST /keys` in
`migration/rust-routes.txt`): the proxy (`lib/backend.ts`) then moves that
method only, so a path that also answers GET elsewhere can move its writes
alone; the inventory shows « Rust (POST) ».

Create (the account envelope ensured first, then `/key/generate` with the
account's `user_id`), revoke and rename (LiteLLM first, the key in the body,
never in a URL) are ported. The budget request notifies the admin by Discord
and e-mail: Rust relays that one action to Flask unchanged (cookie, CSRF,
body), so the notifications stay where they live. Compared on the demo
account: two keys created (one per backend) work against LiteLLM, the
duplicate rename answers 409, revoked keys get 401 from LiteLLM afterwards,
every refusal identical, the relay identical on a refused budget request.

`GET /api/security` (the passkey state and the account's keys) moves the
same way: its sub-paths are all WebAuthn POSTs, which stay on Flask.
Compared on the demo account, which has no passkey: identical.

### Phase 7 — the playground chat, routed (`/playground/chat`)

`portal-rs/src/chat/` is the port of `chat_routes.playground_chat` and of the
modules it drives, one file per concern:
- `history.rs` — messages parsed and bounded, images kept for a model that
  sees, outdated versions of long files replaced by a line, whole oldest turns
  dropped to fit the window, `<think>` stripped from replayed turns, API shape;
- `reasoning.rs` — the « should it think » heuristic;
- `triggers.rs` — when a tool is offered (search, image, video, document,
  edit, past conversations), the Python regexes verbatim with their guards;
- `gauges.rs` — the in-flight row, live decode gauge, TTFT average,
  chars/token calibration, prefill rate, the SpendLogs quota guard;
- `memory_turn.rs` — memory injected as data, facts extracted after a turn;
- `tools/` — web search (SearXNG + crawl4ai behind the public-address guard,
  page reads restricted to the round's own results), image / video / document
  tools (on-demand start under the memory guard, heartbeats while waiting),
  file edits (with a port of `difflib.SequenceMatcher` for the disguised
  rewrite refusal), skills and past-conversation search;
- `phase.rs` — the non-streamed tool rounds, every step told to the client,
  findings re-injected as text;
- `playground.rs` — the route: `: ouverture` before any work, notices, the
  reader task (retry without `reasoning_effort` on a template 500), the relay
  with `: attente` and `dgx_queue` while waiting, the end-of-turn checks
  (no finish_reason, invented image addresses, memory extraction).

Every pure piece is checked against the Python functions themselves on
generated fixtures (`portal-rs/tests/fixtures/{reasoning,chat_history,
chat_triggers,file_tools,chat_library,web_clean}.json`). End to end on the
demo account, with the served model, both backends gave the same flow on:
a plain turn (same 290 prompt tokens, so the same system prompt, memory and
clock), auto reasoning, web search (steps and sources), a file edit (file sent
back, summary as the answer), an invoked skill (same 352 prompt tokens), an
image generated by the tool, a joined document read by OCR started on demand
(same 565 prompt tokens). A client leaving mid-stream drops the upstream
request (engine slot freed) and the in-flight row.

portal-rs joined `web_net` and mounts the crawl4ai token (read-only), as
dgx-portal; the runner and engine stay reachable from it (checked).

### The admin relay

Some routes have an admin branch that cannot be compared with Flask: no
admin session may be used for tests. `portal-rs/src/relay.rs` serves them
for every other account and hands an ADMIN session's request to Flask
unchanged (method, path, query, cookie, CSRF, body), streaming Flask's answer
back (SSE included; checked against a fake upstream). Covered:
`/api/home`, `/system/stats`, `/api/ranking*`, `/support/chat`,
`/support/confirm`. The first three were already ported and compared for the
demo account (identical); they are now routed, admins staying on Flask.

### The parity bench: admin routes and writes

No real admin session may be used for tests, and a write compared live would
change real data. `scripts/parity-bench/` runs the stack's two images side by
side on an internal Docker network (`up.sh`): a synthetic database made by
Flask's own schema plus `seed.py` (an owner, an admin through a group, plain,
disabled and directory accounts, sessions with known tokens), and a fake
upstream (`fake.py`) standing in for the runner, the engines, LiteLLM and the
sidecars. Each backend has its own copy of the database. `bench.py` sends the
same request to both and compares status, redirect and JSON; for a write it
first restores both databases from the seed, then diffs every table
(timestamps masked). Nothing leaves the bench network.

Routed after the bench found them identical:

- admin reads: `/api/admin`, `/admin/audit`, `/admin/support/feedback`,
  `/admin/email/config`, `/admin/consumption`, `/admin/runner/logs`,
  `/admin/sidecar-logs/<kind>`;
- admin writes on the database only: `/admin/announce` (the card, then the
  Discord DMs of `portal-rs/src/discord.rs`), `/admin/settings` (the global
  key cap), the OCR and voice catalogs (add, delete),
  `/admin/users/<u>/revoke-sessions`, `/unblock` and `/block` (sessions cut,
  API keys revoked at LiteLLM — the fake upstream refuses one key, to compare
  the failure report too).

- budgets: `/admin/users/<u>/budget/set` (the exact cap) and
  `/admin/budget/approve|reject/<id>` (permanent raise, temporary grant,
  stacking on a running grant, LiteLLM refusing the write).

- groups: `/admin/groups/create` and `/delete/<name>` (case-insensitive
  merge, the last-local-admin refusals, the local members' quota sync and the
  directory members' right and group quota recomputed);
- local accounts: `/admin/users/create`, `/update/<id>`, `/delete/<id>` and
  `/admin/users/<u>/purge` (keys, sessions, LiteLLM envelope, then every
  attached table). An update that sets a password is handed to Flask, which
  e-mails the account about it; Rust hashes new passwords as werkzeug does
  (scrypt, N=32768, r=8, p=1).

- e-mail: `portal-rs/src/mailer.rs` sends notify.py's template (HTML and the
  plain-text part) through the Admin → Config SMTP account with STARTTLS;
  `/admin/maintenance/toggle` (the operator's e-mail, the warning when it
  fails) and `/admin/email/test` use it. Unlike Python's `smtplib`, it
  verifies the server certificate (system roots, or `SSL_CERT_FILE`). The
  bench's fake upstream runs an SMTP server with STARTTLS behind a bench CA
  and records each message (sender, envelope, subject, text part): Rust's
  and Flask's are identical.

- users' requests (`portal-rs/src/handlers/user_requests.rs`): `POST /request`
  (a model to add: Discord webhook and e-mail, the warning when neither
  went out), `/api/model/request` (a media model wanted, with its cooldown)
  and the budget request of `POST /keys`, no longer relayed to Flask.

- the sidecars' manual start and stop, `/admin/<kind>/start|stop` for
  asr, image, ocr, video and voice (memory guard, the runner's reason on a
  failure, the dictation pin). Listed one by one in the routes file: a
  `/admin/*/stop` entry would also catch `/admin/model/stop`.

- the chat models' catalog: `/admin/model/add`, `/edit/<id>` and
  `/delete/<id>` (`portal-rs/src/litellm_models.rs` creates or refreshes the
  LiteLLM route with the context split of the engine args, and puts the
  previous route back when LiteLLM refuses the new one; the delete refuses
  the served model and keeps weights another entry shares).

- the launches (`portal-rs/src/launches.rs`): `/admin/model/launch` and
  `/stop` (a refusal carries the runner's reason; a timeout is a doubt —
  202, no alert, never retried; the LiteLLM route and `auto-model` follow an
  accepted launch, and a background follow-up announces the model once it
  serves or alerts when it never does), `/admin/ocr/catalog/launch`,
  `/admin/voice/catalog/launch` and `/admin/image/launch` (memory guard,
  infra alert on a failure). Only a launch's network failure differs: the
  message carries reqwest's error text, not Python's.

- `/admin/branding` (the instance's name and logo) and `/admin/runner/stream`
  (the runner's SSE log stream, relayed without its routine access lines);
- `POST /admin/storage$` (where new media go, the backups' destination and
  retention). Reading the storage page (an S3 listing, the host's backup
  state file, mounted in Flask only) and the media migration (a Flask
  background job) stay on Flask.

- Discord linking, `/discord/link` and `/discord/callback` (the OAuth2 state
  kept in the server session and single use). Flask's Discord address is
  fixed, so the bench compares every outcome that needs no network, and runs
  the full exchange on the Rust side against the fake (`DISCORD_API`).

- `/api/account/delete` (one's own account: a local account goes entirely on
  its password, checked under the account's failure counter shared with
  `/login`; a directory account's data is purged). An LDAP account's
  password can only be checked by Flask: that request is handed over.

- the directory (`portal-rs/src/ldap.rs`, the `ldap3` crate): an LDAP
  account's password check (sign-in, account deletion), its e-mail address
  (the password-change notice, a model request's « available » e-mail), and
  the background snapshot of every account's state and groups (read at start,
  then every two minutes when asked; the LDAP groups synced with it). Routed
  with it: `POST /login` for LDAP accounts (only a blocked account and the
  passkey second factor are still relayed), `/api/admin/users`,
  `/admin/users/<u>/detail`, `/admin/platform` (portal-rs now mounts the
  host monitor's and the backups' state read-only, as dgx-portal does),
  `/admin/update/<id>`, `/api/account/password`, the admin's password reset
  and the LDAP account's self-deletion (no longer relayed), `/admin/config`
  and its « Tester » (LDAP, SSO, SMTP, Discord, S3: `s3.rs` gained a signed
  ListObjectsV2), `/admin/validate` (+ `/state`) and `GET /admin/storage`.
  The bench runs a real OpenLDAP (osixia/openldap, seeded by
  `scripts/parity-bench/ldap.ldif`, memberOf overlay).
- Two Flask behaviours Rust does not copy: each gunicorn worker keeps its own
  directory snapshot (the Users page flips between states while they age),
  and its own validation state (the panel's polling can land on a worker that
  never started the run). portal-rs is one process: one snapshot, one state.

- the SSO sign-in, `/login/sso` and `/api/oauth2-redirect`
  (`portal-rs/src/sso.rs`: the authorization-code flow, the id_token checked
  against the provider's JWKS — signature, issuer, audience, expiry, nonce —
  then the identity resolved by the directory); `/api/setup` (the owner of a
  fresh instance); `/admin/storage/migrate` (its lock is the same flock as
  Flask's, on the data volume). The bench's fake plays an OIDC provider
  (authlib signs its RS256 id_tokens) and a minimal S3;
  `scripts/parity-bench/sso_check.py` and `migrate_check.py` run the flows
  bench.py cannot (random state and nonce, a background move).

Passkeys (`portal-rs/src/webauthn.rs`, `handlers/passkeys.rs`): the five
`/api/security/*` writes and the second factor of `POST /login` (a password
that passes answers `webauthn_required` and the challenge; the session opens
on `verify-login`). Verified with a software authenticator
(`scripts/parity-bench/wa_driver.py`, run inside the Flask image for cbor2
and cryptography): 20 steps identical on both backends, the database
identical after each run, and a key created by Flask verified by Rust (the
production case) with the replay and a forged signature refused.

Scope, on purpose: the algorithms are the ones py_webauthn accepts by default
(ES256, RS256, EdDSA); the attestations are `none`, `packed` (self, or with
an `x5c` certificate, not chain-checked — Flask does not check it either
without configured roots) and `fido-u2f`. tpm, apple and android-key are
refused with a message: a key of those formats must be re-enrolled on a
`none` authenticator, which is what the registration asks for. The clone
check (the counter must grow) is the same.

`/internal/authcheck` is ported too (`handlers/internal.rs`, 6 cases identical
on the bench, calls from the trusted bridge and from loopback), but Traefik's
forwardAuth still points at `dgx-portal:5000` in `/opt/traefik` (outside this
repository): the switch is a Traefik configuration change, not made here.

Left on Flask (3 routes): the root page `/` (a Next page; Flask's answer is a
redirect target only) and `/api/latency` (it measures the Flask process
itself), and `/internal/authcheck` until Traefik points at portal-rs.

The fake upstream records the calls it receives (the probes left out), and
`bench.py` compares each backend's sequence of upstream calls as well: a
write that answers the same but deletes another key would not pass.

Found by the bench and fixed:

- A session whose role changed (a group granted or withdrew the admin right)
  was read with the new role by Rust, but the session was not rewritten and
  the `session.role_rafraichi` audit line not written, as Flask does.
- The router's own 405 had an empty body; it now carries werkzeug's page.
- The Rust connection enforces foreign keys (sqlx's default), Flask's does
  not: forgetting a memory fact removed the orphan node's aliases in Rust
  (cascade) and left them in Flask, where they kept their
  `(username, alias_norm)` key from any later node. Flask now deletes them.
- `/admin/branding` checked the logo's format before its `-` (remove)
  value, so the removal always answered 400; both now let it through.
- Flask accepted `nan` as a budget amount (`nan <= 0` and `nan > 1e12` are
  both false) and sent it to LiteLLM as the account's cap; both backends now
  refuse it (`not v > 0`).
- The sidecar probes (0.3 s) timed out on a cold start: they built an HTTP
  client per call. They now share one, built before serving.
- The fake upstream's first version listened with Python's default backlog (5):
  the console's concurrent probes lost SYNs and waited 1 s. A bench artifact
  (the sidecars run uvicorn, backlog 2048), fixed in `fake.py`.

Nothing of this phase is left on Flask.

### Phase 6 — local sign-in, routed (`POST /login`)

`portal-rs/src/login.rs` signs a LOCAL account in: werkzeug's `scrypt:` and
`pbkdf2:` hashes checked (fixture from `generate_password_hash`), the lock
keys shared with Flask (`<ip>|<user>`, `user:<user>`), the source recorded,
then `_apply_session` — a NEW session token, the client's CSRF token kept,
a fresh `sid` in `user_sessions`, `?next=` restricted to a local path.
Everything else is handed to Flask unchanged: a directory account, a wrong
password (Flask tries the directory and counts the failure once), a blocked
account (its refusal and audit line), an account with a passkey (the second
factor). An unknown account still pays a scrypt round in Rust, so the
response time tells nothing either way. Compared with Flask on the demo
account, directly and through the proxy: success, CSRF kept, old token
dead, `next` honoured and an open redirect refused, wrong password and
unknown account (same 401 and counter), malformed name, a success resetting
the counters. `GET /login` stays the Next page.

### Phase 7 — the Support assistant, routed (`/support/chat`, `/support/confirm`)

`chat/support.rs` and `chat/support_tools.rs` port the non-admin Support:
system prompt and FAQ with the instance name, the account context (budget,
masked keys, today's use, catalogue, runner, services, requests, last
actions), memory as data, the self-service tools plus the user's MCP tools,
past-conversation search and skills. Each turn streams; a `<think>` written
into the content is held back until it closes; tool calls are gathered from
the deltas, four rounds at most, then a last turn without tools. Sensitive
actions are only proposed (`dgx_confirm`, single-use token, executed by
`/support/confirm`); after an MCP or skill result, they and key creation are
refused for the turn. The budget and model requests go through Flask's own
forms (`/keys`, `/request`) with the user's cookie, so the admin
notifications (Discord, e-mail) stay where they live. A completed turn is
saved as the thread (last 40) and feeds the memory.

Compared on the demo account: empty request (400), a context question (same
keys), key creation, revocation proposed then confirmed (a replay answers
404), malformed confirm token, an MCP call (same events, same upstream
failure), and an MCP read followed by a request — both backends end on the
same `model_replied_error` 400: an MCP result has no size bound and can
overflow the window (pre-existing, noted for later). The thread is saved. Not
run live: the budget/model requests (they notify the admin).

### Phase 7 — MCP servers, routed (`/mcp`)

Registering, updating, toggling and deleting the viewer's MCP servers. The
live check (`initialize` + `tools/list`, JSON-RPC over HTTP, SSE answers
read) goes through the skills import's SSRF guard: https only, every resolved
address public, the first one pinned for the connection, no redirect. The
`rl-mcp` bucket bounds the outbound connections. Compared with Flask on the
demo account: every refusal identical (missing fields, name, http, internal
host, private address, unknown host, a server answering 405), and a real
key-less server (DeepWiki) registered through both, 3 tools each; update,
key set then cleared, toggle and delete behave as Flask's. Flask keeps a
120 s per-process tools cache that a Rust update does not clear — the same
staleness Flask's other workers already have.

### Phase 3 — OCR extraction, routed (`/api/ocr/extract`)

The image is stored first, then the OCR container's SSE answer is relayed
line by line from a tokio task (a `: ouverture` comment goes out before any
work, as in Flask, for the proxy's 15 s connect delay); the job is written
at the end — with the partial text if the client left, the image dropped if
nothing came. chandra's layout prompt is compiled in verbatim. Tested on the
demo account with chandra-ocr-2 started for the test: the same invoice image
gives the same HTML through both backends, both jobs land in the history
with their image.

### Phase 3 — dictation and voice mode, routed (`/api/transcribe`, `/api/voice/chat/start`, `/api/voice/speak`)

The dictation sidecar's on-demand state is FILE state (idle clock, in-flight
markers, pin, stop lock) that Flask's idle reaper reads. It moved from the
Flask container's `/tmp` to the data volume both portals mount
(`ASR_ONDEMAND_DIR=/app/data/.asr-ondemand`): Rust records activity and
in-flight calls into the same files, the reaper stays in Flask
(`src/asr_ondemand.rs`). The automatic start keeps its guards: 8 GiB of
MemAvailable, the 60 s throttle after a success, the audit line.

Tested end to end on the demo account: Kyutai speaks a sentence through
Rust (same PCM length and headers as Flask), that audio wrapped as WAV is
transcribed by both backends word for word, and Rust's on-demand start of the
stopped ASR container answered « starting » until the model loaded. The voice
mode's start returns the same body as Flask.

### Phase 3 — video, routed (`/api/video/generate`, `status`, `cancel`, `/video/file`)

The ComfyUI client is ported: the two MiniMax H3 graphs (compiled in from
`dgx-portal/workflows/`, text-only or with a reference image uploaded
first), the history/queue reading, the cancel (queued: deleted by id;
running: interrupt), and the result chain of `/video/file` — the stored
copy, else ComfyUI's output directory (now mounted read-only into portal-rs
too, traversal refused), else pulled from ComfyUI and cached. Local files
are served with a byte range (Flask's `send_file(conditional=True)`), which
the earlier media routes lacked for the volume.

Tested end to end on the demo account: a real 2 s text-to-video generated
through Rust (151 s), cached to the bucket, served identically to Flask with
and without `Range`; a second job cancelled while running (ComfyUI's queue
emptied). Refusals compared with Flask: identical.

Found on the way:
- Flask's cancel required « running », a status no row ever has (jobs are
  inserted « pending » and only their end is written back): every cancel
  answered « Ce job n'est plus actif. ». Fixed on both sides, tested.
- `comfyui-relay.service` (172.19.0.1:8188 → ComfyUI) was disabled and
  stopped on the host: the portal could not reach ComfyUI at all. Started
  for the test; enabling it at boot is the operator's call.

### Phase 3 — voice cloning, routed (`/api/voice/generate`)

Both cloning protocols are ported (Qwen3-TTS: one multipart POST;
Chatterbox: upload the reference under a random name, then generate by
that name, the refusal read from the upload's body whatever its status),
the language fallback, the WAV duration (`wave`'s rules: PCM only) and the
history trim that removes the evicted clips' files. Live, the loaded engine
is Kyutai, which cannot clone: only the refusals were compared with Flask
(identical); the two protocols are covered by a test against fake sidecars.

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

### Phase 7 — the plain parts of the chat pages, routed

`/api/support/thread` (the kept Support thread), `/support/thread/clear`,
`/support/feedback` (vote parsed as Python's `int()`, fields as `str()`,
audit line) and `/api/playground/data` (served models, context windows from
`effective_ctx`, vision read from each entry's arguments, `has_key`).
Compared with Flask on the demo account, written rows included. The chat
streams themselves (`/playground/chat`, `/support/chat`), titles and
summaries stay on Flask.

`/api/playground/title` and `/api/playground/summarize` are routed too:
one non-streamed completion on the viewer's newest key, behind the chat's
guards (maintenance, then their own `rl-titre` / `rl-resume` buckets in the
shared SQL table). Python's `str()` of a non-text content is its `repr()`,
reproduced. Compared with Flask on the deterministic cases (identical) and
against the served model (both answer, e.g. « Tarte aux pommes »).

`/api/voice/chat/status` (can the voice mode speak: Kyutai engine loaded,
its voices, the ASR state) is routed with them; the voice chat's start and
speak calls drive sidecars and stay on Flask. `/api/security` cannot move
alone: routing is by prefix, and its sub-paths are the WebAuthn writes.

The HTML preview (`/playground/preview`, create + show) is routed too: the
page goes out with its own `sandbox` policy (opaque origin, `default-src
'none'`, `form-action 'none'`), the error spy grafted after `<head>`, and
`nosniff` / `no-store`; per-account and global caps purged in the same
transaction as the insert. Byte-identical to Flask on the bench, headers
included.

CSRF refusals now carry Flask's exact 400 page (« CSRF token manquant ou
invalide. »), on every Rust write route.

### Phase 4 — the small writes, routed

`/api/onboarding/done`, `/discord/unlink` (302 to `/?discord=unlinked`) and
`/skills` (a skill written in the interface: create, update, delete; refusals
answer 200 with `ok: false` and the delete 204, as Flask does). Compared on
the demo account, its state restored afterwards.

The CSRF check reads the token from the `X-CSRFToken` header OR a form's
`csrf_token` field, as Flask's `_csrf_protect` does, on every write — the first
version of the shared helper only read the header and refused a form token
(found by this comparison, fixed for every write).

`/mcp`, `POST /keys` and `POST /request` were left out of this step and moved
later (their pages keep GET on Next through method entries, see above). The
Discord OAuth link and callback moved later too (see the parity bench). Still
on Flask: nothing of this phase (the password change moved with the LDAP block).

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
