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
`/api/session`, `/api/whoami`, and `/api/ranking` over the 5 periods × 5
metrics, by group, with a model filter and with invalid parameters. The
`login_required` guard is ported whole (max age, `user_sessions` registry,
account state re-read, owner rule); a database error answers 503, never a
logout.

Not routable yet, and why:

- **Media sidecars** (`/api/health`, `/api/running-models`, `/api/home`): OCR,
  voice, ASR and image sit on dedicated networks the Rust portal does not
  join, their probes read the containers' state, and ComfyUI's port is
  firewalled to the Flask portal's address. The Rust portal answers `null`
  (unknown) for them — never « stopped ».
- **Admin view of `/api/ranking`**: `quota_pct` needs LiteLLM's `/user/list`
  (master key); it is `null` on the Rust side.
- **Response headers**: Flask adds `nosniff`, CSP, HSTS and friends to every
  answer; the Rust portal does not yet.
- **Dropped sessions**: Flask writes a `session.invalidee` audit line and
  clears the cookie; the Rust portal only refuses the request.

Porting notes that cost a wrong first attempt: LiteLLM listens on 4001 inside
the Docker network and `/health` needs a key since 1.102 (probe
`/health/liveliness`); instance settings are stored as `cfg_<name>` with empty
meaning unset; `password_managed_by` comes from the cumulative
`user_sources.sources`; ties in the ranking follow Postgres' aggregation
order, so the Rust side runs ranking.py's SQL verbatim as a subquery; and
Python's `round()` is ties-to-even on the exact binary value, which Rust's
`{:.n}` formatting reproduces and a multiply-and-round does not.

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
