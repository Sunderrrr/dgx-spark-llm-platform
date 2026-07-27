# dgx-portal-frontend

The web UI for **Cronos**, the DGX Spark self-service LLM platform. Built with
**Next.js 16** (App Router) and Meta's **Astryx** design system (React +
StyleX). It owns the platform's public port (`:5000`) and covers every page:
home, playground, keys, search, ranking, request, support, admin, login.

Flask (`../dgx-portal/`) remains the authority for everything that matters —
LDAP/OIDC auth, sessions, CSRF, the database, budgets, model launching. This
app is a client-rendered UI on top of that JSON API; it holds no business
logic of its own.

## How it talks to Flask

Three different mechanisms, depending on what's being called, because a
naive one-size-fits-all proxy doesn't work here:

- **`proxy.ts`** (Next.js 16's `middleware.ts` replacement) rewrites any
  non-GET/HEAD request to `/login`, `/keys`, or `/request` straight to Flask,
  *before* Next's own router runs. Those three paths collide with real
  Next.js pages, so without this a POST to them would silently hit Next's own
  cached page markup instead of ever reaching Flask — the bug that motivated
  this file existing at all. `proxy.ts` also stamps a fresh nonce into the CSP
  header on every request (`script-src 'nonce-...' 'strict-dynamic'`), which
  is why every page here is forced to render dynamically (see
  `app/layout.tsx`'s `export const dynamic`).
- **`next.config.ts`**'s `rewrites().fallback` catches everything else that
  isn't a known Next.js route — the `/api/*` JSON endpoints, admin action
  routes, etc. — and forwards it to Flask as-is.
- **Dedicated Route Handlers** (`app/playground/chat/route.ts`,
  `app/support/chat/route.ts`, `app/admin/runner/stream/route.ts`) exist
  because both mechanisms above were confirmed — by timing actual SSE chunk
  arrivals — to buffer a streamed response entirely before forwarding it,
  even though Flask streams token-by-token correctly. These manually pipe
  `upstream.body` through a `Response`, which Next.js genuinely streams. See
  `lib/sseProxy.ts` for the shared connect-timeout / idle-timeout logic.

`BACKEND_URL` (env var, default `http://dgx-portal:5000`) is the only place
Flask's address is configured — it's the docker-compose service name
internally, or `http://localhost:5000` for local dev against a Flask
instance running outside Docker.

## Auth model

There's no session state on the Next.js side. Every authenticated fetch goes
out with `credentials: "include"` (see `lib/api.ts`'s `authFetch`) so the
browser attaches Flask's session cookie directly. A `401` means "not logged
in" and redirects to `/login`; a `403` means "logged in, not allowed"
(`ForbiddenError`) and the calling page is expected to render its own
"access denied" state instead — see `app/(app)/admin/page.tsx` for the
pattern. CSRF tokens are fetched once per page load (`lib/useCsrf.ts`) and
sent back as `X-CSRFToken` on every mutating request.

## Development

```bash
npm install
npm run dev          # http://localhost:3000, Turbopack
```

For real data during local dev, either point `BACKEND_URL` at a reachable
Flask instance, or run the whole stack via `docker compose up` from the repo
root and hit the container directly.

```bash
npm run lint          # eslint
npx tsc --noEmit       # typecheck (no bundled `tsc` script)
npm run build          # production build; also type-checks
```

> This Next.js version has real breaking changes from what you might expect
> (`middleware.ts` → `proxy.ts`, CSP nonce plumbing, etc.) — see
> `node_modules/next/dist/docs/` for the version actually installed before
> assuming familiar behavior.

## UI conventions (Astryx)

This app has no StyleX/Tailwind compiler wired in — styling is component
props first, then plain `style={{}}`/`className` with CSS custom properties
(`var(--color-*)`, `var(--spacing-*)`), never raw hex/px. Layout is built
entirely from Astryx components (`AppShell`, `SideNav`, `Layout`, `Stack`,
`Grid`, …) — no bare `<div>` for structure. Before adding UI, `npx astryx
build "<idea>"` finds the closest existing template/block instead of
hand-rolling it. Full conventions are in `AGENTS.md` / `CLAUDE.md`.

The UI text is **French-only** — there's no language toggle (the old Flask
app's FR/EN translation hook was removed along with the Jinja templates it
only ever applied to).

## Structure

```
app/
├── layout.tsx                    # root layout; force-dynamic (required for CSP nonces)
├── theme-provider.tsx             # light/dark, follows the browser by default
├── login/page.tsx                 # LDAP + SSO, both shown directly (no toggle)
├── (app)/                         # authenticated pages, shared AppShell + SideNav
│   ├── layout.tsx                 # nav, theme toggle, logout
│   ├── page.tsx                   # home: server stats, active model, usage chart
│   ├── playground/                # streaming chat against the active model
│   ├── support/                   # streaming chat with the Cronos assistant (tool-calling)
│   ├── keys/, search/, ranking/, request/, admin/
├── playground/chat/route.ts       # SSE proxy (see above)
├── support/chat/route.ts          # SSE proxy
└── admin/runner/stream/route.ts   # SSE proxy (live vLLM launch logs)
lib/
├── api.ts        # authFetch, getJSON, postForm, streamChat, streamSupportChat
├── sseProxy.ts   # shared proxySSE/proxySSEGet used by the route handlers above
├── useCsrf.ts
├── conversations.ts   # localStorage-backed Playground chat history
└── types.ts
proxy.ts           # method-based routing to Flask + nonce CSP (see above)
next.config.ts      # CSP base policy, fallback rewrite, output: standalone
```

## Deployment

Built and run as its own Docker service (`dgx-portal-frontend` in the repo
root's `docker-compose.yml`), non-root, `output: "standalone"`. It owns the
platform's public port; `dgx-portal` (Flask) has no published port and is
only reachable from this container over the internal docker network.
