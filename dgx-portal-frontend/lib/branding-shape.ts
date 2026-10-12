// The deployment identity's shape, shared by the server (root layout) and the
// client (lib/branding.tsx). It lives outside the "use client" module: a
// function exported from one cannot be CALLED on the server.

export type Branding = {
  /** Instance name shown top left. « DGX » is only the default. */
  name: string;
  /** URL of the uploaded logo, or null for the built-in favicon. */
  logo: string | null;
};

export const DEFAULT_BRANDING: Branding = { name: "DGX", logo: null };

/** Shape /api/config's answer into a Branding. */
export function brandingFromConfig(d: { branding?: { name?: string; logo?: string | null } } | null): Branding {
  return { name: d?.branding?.name || DEFAULT_BRANDING.name, logo: d?.branding?.logo || null };
}
