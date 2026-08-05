// Bloque explicitement /internal/* sur le domaine public (dgx.cronos.website).
// Sans cette route, next.config.ts's `fallback` rewrite proxyait n'importe
// quel chemin non reconnu vers Flask tel quel — y compris /internal/authcheck,
// qui n'est censé être appelé QUE par Traefik en interne (forwardAuth sur le
// routeur api.cronos.website, cf. routes.yml), jamais via ce frontend. Une
// route Next.js réelle a priorité sur le rewrite fallback, donc ce handler
// intercepte la requête avant qu'elle n'atteigne Flask.
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
