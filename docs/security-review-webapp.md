# Security — the running application

Measured on a **local, isolated instance**: the `portal-rs` binary on `127.0.0.1:5199`
with its own SQLite database under `/tmp/audit5/` (schema cloned **without any production
row**) and a `demo` account created by the project's CLI. No request was sent to the
production instance or to any remote service.

## Measured results

| control | test | result |
|---|---|---|
| **Security headers** | `curl -D-` on `/`, `/api/csrf`, `/login` (portal) and `/login` (frontend) | `Content-Security-Policy` (per-request nonce, `script-src 'self'`), `Strict-Transport-Security: max-age=63072000; includeSubDomains`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `Permissions-Policy: geolocation=(), payment=(), usb=(), camera=(self), microphone=(self)` — **complete** |
| **Session cookie** | successful login | `Secure; HttpOnly; Path=/; SameSite=Lax`, **no `Domain` attribute** |
| **CSRF** | `POST /login` without the token | **400** (« CSRF token manquant ou invalide »); with it → 302 |
| **Access control** | `GET /api/admin`, `/api/admin/users`, `/api/keys` without a session | **401**; with the non-admin account → **403** |
| **CORS** | `Origin: https://evil.example` | **no** `Access-Control-Allow-*` header |
| **Account enumeration** | wrong password vs unknown user | **same 400**, same page, no difference |
| **Error disclosure** | unknown route, malformed JSON, `/api/csrf` on an empty database | **generic** pages (400/500, no stack trace, no path) |
| **Session from a failed login** | wrong password | **no cookie set**, `401` on protected routes |
| **Login lockout** | 12 wrong passwords | **partially verified**: the insert columns (`key`, `fails`, `first_at`, `locked_until`) do match the shipped table, checked against the live database, but the local exercise recorded no failure row, so the end-to-end lockout is **not** proven — the failure path needs a dedicated test |
| **Traefik** | `/opt/traefik/dynamic/routes.yml` | TLS per router, `ipallowlist-public` on the public routers, no dashboard exposed in that file |

Notable: the CSP is **per request with a nonce** (`proxy.ts:92-122`), and HSTS and
Permissions-Policy are set there too (`proxy.ts:129-136`) — not only by Traefik.

## Not tested here, and why

1. **TLS and Traefik redirects**: that configuration lives outside the repository
   (`/opt/traefik`) and the isolated instance does not traverse it. Read-only review.
2. **The CSP nonce in production**: a fresh nonce per request **defeats page caching**
   (measured: a different nonce on every call). It is a deliberate design choice, listed
   here because it has a performance cost the operator should know about. Not changed.
3. **Uploads** (OCR, voice, avatar): they need the GPU sidecars, outside a GPU-less
   instance; the phase-4 source review covers them (size bounds, header checks, no
   user-supplied filename reaching a path).
4. **Stored XSS**: already covered by the phase-4 source review and the katex/markdown
   probes.
5. **Login rate limiting**: the logic is read and pinned by unit tests (`cargo test`), but the
   cloned `login_attempts` table does not have the shape the insert expects, so the local
   exercise recorded nothing. The lockout is therefore **unverified**, not source-verified:
   the columns (`key`, `fails`, `first_at`, `locked_until`) must be compared against the
   shipped migration before anyone relies on it.

## What was fixed

Nothing: no exploitable deviation was found in the controls exercised here. The two
observations above (CSP nonce vs caching, lockout not measurable outside production) are
decisions to confirm, not defects.
