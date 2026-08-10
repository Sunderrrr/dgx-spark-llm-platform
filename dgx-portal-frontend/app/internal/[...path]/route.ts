// Explicitly blocks /internal/* on the public domain (dgx.cronos.website).
// Without this route, next.config.ts's `fallback` rewrite would proxy any
// unrecognized path to Flask as-is — including /internal/authcheck,
// which is only ever meant to be called BY Traefik internally (forwardAuth on
// the api.cronos.website router, cf. routes.yml), never via this frontend. A
// real Next.js route takes precedence over the fallback rewrite, so this handler
// intercepts the request before it reaches Flask.
function blocked() {
  return new Response(null, { status: 404 });
}

export const GET = blocked;
export const POST = blocked;
export const PUT = blocked;
export const PATCH = blocked;
export const DELETE = blocked;
export const HEAD = blocked;
export const OPTIONS = blocked;
